"""Версионируемые учебные профили, не являющиеся допуском в ГИС МТ.

Выбор профиля выполняется из закрытого списка. Эти профили не представляют
товарные группы. Их нельзя повысить до production передачей флага из клиента.
"""

from __future__ import annotations

from copy import deepcopy
from types import MappingProxyType

REFERENCE_DATE = "2026-11-08"

# Это имена намерений, а не внешние endpoints или универсальный lifecycle ЧЗ.
REGULATORY_OPERATIONS = frozenset(
    {
        "issue_codes",
        "application_report",
        "commission",
        "transfer",
        "withdrawal",
        "retail_sale",
        "return_to_circulation",
        "relabel",
        "fiscalize",
        "sign_document",
        "send_document",
    }
)

_LOCAL_OPERATIONS = (
    "scan",
    "receive",
    "reconcile",
    "reserve",
    "release",
    "prepare_document",
    "import_preview",
    "export_preview",
    "print_preview",
    "record_print_result",
    "audit",
    "simulate",
)

_PROFILES = MappingProxyType(
    {
        "test-pu-v1": {
            "id": "test-pu-v1",
            "label": "Учебный поэкземплярный профиль v1",
            "version": 1,
            "mode": "unit",
            "parser_profile": "gs1-91-92-v1",
            "operations": _LOCAL_OPERATIONS + ("aggregate", "disaggregate"),
            "capabilities": {
                "unit_tracking": True,
                "quantity_tracking": True,
                "requires_unit_codes": True,
                "aggregation": True,
                "emission": False,
                "external_transmission": False,
                "retail_permission_checks": False,
                "fiscalization": False,
                "regulatory_return": False,
            },
        },
        "test-osu-v1": {
            "id": "test-osu-v1",
            "label": "Учебный количественный профиль v1",
            "version": 1,
            "mode": "quantity",
            "parser_profile": "gs1-93-v1",
            "operations": _LOCAL_OPERATIONS,
            "capabilities": {
                "unit_tracking": False,
                "quantity_tracking": True,
                "requires_unit_codes": False,
                "aggregation": False,
                "emission": False,
                "external_transmission": False,
                "retail_permission_checks": False,
                "fiscalization": False,
                "regulatory_return": False,
            },
        },
    }
)

# Не подтверждённые предпосылки промышленного внедрения; ни одна из них
# не может быть удовлетворена булевым флагом этой функции.
_LIVE_BLOCKERS = (
    ("test_profile_only", "Профиль учебный и не подтверждает правила реальной товарной группы."),
    (
        "commodity_rules_unverified",
        "Не закреплены ассортимент, товарная группа, роли, даты применимости и редакции правил.",
    ),
    (
        "external_contracts_unverified",
        "Не проверены разрешённые спецификации, схемы и тестовые ответы НК, СУЗ, ГИС МТ и ЭДО.",
    ),
    (
        "signing_unverified",
        "Не подтверждены УКЭП, полномочия подписанта, МЧД при её необходимости и способ подписи.",
    ),
    (
        "devices_unverified",
        "Не проведены испытания выбранных сканеров, принтеров и применимого кассового контура.",
    ),
    (
        "acceptance_missing",
        "Нет приёмочного протокола реальной операции с сопоставленными внешними квитанциями.",
    ),
)


class PolicyError(ValueError):
    """Безопасная ошибка выбора учебного профиля."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def get_profile(profile_id: str) -> dict:
    """Вернуть независимое описание; изменения клиентом не меняют реестр."""
    if not isinstance(profile_id, str) or profile_id not in _PROFILES:
        raise PolicyError("unsupported_profile", "Бизнес-профиль маркировки не поддерживается.")
    result = deepcopy(_PROFILES[profile_id])
    result.update(
        test_only=True,
        live_supported=False,
        commodity_group=None,
        reference_date=REFERENCE_DATE,
        status="test_only",
    )
    result["operations"] = list(result["operations"])
    return result


def list_profiles() -> list[dict]:
    return [get_profile(profile_id) for profile_id in _PROFILES]


def readiness(profile_id: str, deployment: str = "live", operation: str = "scan") -> dict:
    """Проверить локальный учебный сценарий; реальное действие всегда закрыто.

    ``deployment`` задаёт доверенная конфигурация сервера, не тело HTTP-запроса.
    Готовность не заменяет проверку прав, Company и состояния документа.
    ``ready`` относится только к указанной внутренней операции; наличие кода
    и успешный синтаксис никогда не означают разрешение продажи.
    """
    profile = get_profile(profile_id)
    if not isinstance(deployment, str) or deployment not in {"test", "demo", "live"}:
        raise PolicyError("invalid_deployment", "Режим должен быть test, demo или live.")
    if not isinstance(operation, str) or (
        operation not in profile["operations"] and operation not in REGULATORY_OPERATIONS
    ):
        raise PolicyError("unsupported_operation", "Операция не поддерживается выбранным профилем.")

    live_blockers = [{"code": code, "message": message} for code, message in _LIVE_BLOCKERS]
    blockers = []
    if deployment == "live":
        blockers.extend(live_blockers)
    elif operation in REGULATORY_OPERATIONS:
        blockers.append(
            {
                "code": "external_operation_unavailable",
                "message": "Учебный профиль не выполняет внешние или юридически значимые операции.",
            }
        )

    return {
        "profile_id": profile["id"],
        "profile_version": profile["version"],
        "mode": profile["mode"],
        "deployment": deployment,
        "operation": operation,
        "ready": not blockers,
        "test_only": True,
        "live_ready": False,
        "external_allowed": False,
        "blockers": blockers,
        "live_blockers": live_blockers,
        "reference_date": REFERENCE_DATE,
    }
