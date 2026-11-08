"""Строгий разбор трёх явно выбранных форм GS1 element string.

Профили описывают только синтаксис. Они не подтверждают выдачу кода СУЗ,
криптографию, товарную группу, собственника или разрешение продажи.
AI 01 — N14 с контрольной цифрой; AI 21 — X1..20; AI 91/92/93 — X1..90.
Источник синтаксиса: https://ref.gs1.org/tools/gs1-barcode-syntax-resource/
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from types import MappingProxyType

GS = "\x1d"
IDENTITY_NAMESPACE = "gs1:01+21:v1"
MAX_SCAN_LENGTH = 256

# GS1 AI encodable character set 82; пробел, Unicode и иные ASCII запрещены.
_CSET82 = frozenset(
    "!\"%&'()*+,-./0123456789:;<=>?ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz"
)
_PROFILE_FIELDS = MappingProxyType(
    {
        "gs1-ki-v1": (),
        "gs1-91-92-v1": ("91", "92"),
        "gs1-93-v1": ("93",),
    }
)


class CodeError(ValueError):
    """Ошибка с машинным кодом и сообщением без исходного КМ."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ParsedCode:
    """Исходный скан и идентичность экземпляра; секретные поля скрыты в repr.

    ``normalized`` отличается от ``raw`` только разрешённым снятием ``]d2``.
    ``identity`` стабилен между профилями и криптохвостами; Company в него
    намеренно не входит. Границы доступа к записи проверяет сервер отдельно.
    """

    raw: str = field(repr=False)
    normalized: str = field(repr=False)
    profile_id: str
    namespace: str
    gtin: str
    serial: str = field(repr=False)
    canonical_ki: str = field(repr=False)
    identity: str
    crypto_fields: tuple[tuple[str, str], ...] = field(repr=False)

    def as_dict(self, *, include_sensitive: bool = False) -> dict:
        """Серверное представление; полные КМ и хвосты включаются явно.

        КИ и серийный номер также требуют проверки доступа. Это не готовый
        публичный ответ API; сервер маскирует их по правам пользователя.
        """
        if type(include_sensitive) is not bool:
            raise CodeError("invalid_option", "Признак выдачи исходного кода должен быть логическим.")
        result = {
            "profile_id": self.profile_id,
            "namespace": self.namespace,
            "gtin": self.gtin,
            "serial": self.serial,
            "canonical_ki": self.canonical_ki,
            "identity": self.identity,
            "crypto_ais": [ai for ai, _value in self.crypto_fields],
            "syntax_valid": True,
            "authenticity_verified": False,
        }
        if include_sensitive:
            result.update(
                raw=self.raw,
                normalized=self.normalized,
                crypto_fields=dict(self.crypto_fields),
            )
        return result


def parser_profiles() -> tuple[str, ...]:
    """Закрытый список поддержанных синтаксических профилей."""
    return tuple(_PROFILE_FIELDS)


def validate_gtin(gtin: str) -> str:
    """Проверить N14 и Mod10, сохраняя ведущие нули.

    Проверка назначения GTIN владельцу и его наличия в НК сюда не входит.
    """
    if not isinstance(gtin, str) or len(gtin) != 14 or not all("0" <= c <= "9" for c in gtin):
        raise CodeError("invalid_gtin", "GTIN должен содержать ровно 14 цифр ASCII.")
    weighted_sum = sum(
        int(digit) * (3 if position % 2 == 0 else 1)
        for position, digit in enumerate(reversed(gtin[:-1]))
    )
    if (10 - weighted_sum % 10) % 10 != int(gtin[-1]):
        raise CodeError("invalid_gtin_checksum", "Контрольная цифра GTIN не совпадает.")
    return gtin


def parse_code(
    raw: str,
    profile_id: str,
    *,
    allow_symbology_identifier: bool = False,
) -> ParsedCode:
    """Разобрать скан без догадок о разделителях, профиле и товарной группе.

    Перед каждым следующим переменным AI нужен настоящий GS (ASCII 29).
    HRI вида ``(01)…(21)…`` и текстовые ``<GS>``/``\\u001d`` не преобразуются.
    Цифры ``91``/``92`` внутри серийного номера не считаются границей поля.
    KI-профиль предназначен для явно переданных КИ, а не для угадывания КМ.
    """
    if not isinstance(profile_id, str) or profile_id not in _PROFILE_FIELDS:
        raise CodeError("unsupported_profile", "Профиль формата кода не поддерживается.")
    if type(allow_symbology_identifier) is not bool:
        raise CodeError("invalid_option", "Разрешение префикса сканера должно быть логическим.")
    if not isinstance(raw, str) or not raw:
        raise CodeError("invalid_input", "Передайте непустую строку сканирования.")
    if len(raw) > MAX_SCAN_LENGTH:
        raise CodeError("scan_too_long", "Скан превышает допустимую длину выбранных форматов.")

    normalized = raw
    if normalized.startswith("]"):
        if not normalized.startswith("]d2"):
            raise CodeError("unsupported_symbology", "Префикс сканера не поддерживается.")
        if not allow_symbology_identifier:
            raise CodeError("symbology_not_enabled", "Префикс ]d2 не разрешён настройкой сканера.")
        normalized = normalized[3:]

    if normalized.startswith("("):
        raise CodeError("unsupported_representation", "Нужен исходный скан, а не текст с AI в скобках.")
    if any(character != GS and character not in _CSET82 for character in normalized):
        raise CodeError(
            "invalid_characters",
            "Скан содержит недопустимые символы, пробелы или управляющие последовательности.",
        )

    expected_fields = _PROFILE_FIELDS[profile_id]
    parts = normalized.split(GS)
    if len(parts) != 1 + len(expected_fields) or any(not part for part in parts):
        raise CodeError(
            "invalid_structure",
            "Состав полей не соответствует профилю. Проверьте передачу разделителей GS сканером.",
        )

    first = parts[0]
    if len(first) < 19 or not first.startswith("01") or first[16:18] != "21":
        raise CodeError("invalid_structure", "Ожидаются AI 01 с GTIN14 и затем AI 21 с серийным номером.")
    gtin = validate_gtin(first[2:16])
    serial = first[18:]
    if not 1 <= len(serial) <= 20:
        raise CodeError("invalid_serial_length", "Серийный номер AI 21 должен содержать от 1 до 20 символов.")

    crypto_fields = []
    for expected_ai, part in zip(expected_fields, parts[1:]):
        if not part.startswith(expected_ai):
            raise CodeError("unexpected_ai", "Идентификаторы или порядок дополнительных полей не совпадают.")
        value = part[2:]
        if not 1 <= len(value) <= 90:
            raise CodeError(
                "invalid_field_length", "Дополнительное поле должно содержать от 1 до 90 символов."
            )
        crypto_fields.append((expected_ai, value))

    canonical_ki = f"01{gtin}21{serial}"
    identity_input = json.dumps(
        [IDENTITY_NAMESPACE, gtin, serial], ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")
    identity = hashlib.sha256(identity_input).hexdigest()
    return ParsedCode(
        raw=raw,
        normalized=normalized,
        profile_id=profile_id,
        namespace=IDENTITY_NAMESPACE,
        gtin=gtin,
        serial=serial,
        canonical_ki=canonical_ki,
        identity=identity,
        crypto_fields=tuple(crypto_fields),
    )
