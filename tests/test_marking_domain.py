"""Проверки синтаксиса на искусственных строках, не являющихся кодами СУЗ.

Ни один fixture не применяется к реальной продукции, не печатается и не
передаётся внешнему сервису. Валидная контрольная цифра не означает выдачу GTIN.
"""

from dataclasses import FrozenInstanceError
import unittest

from erp_basic.marking.codes import (
    GS,
    IDENTITY_NAMESPACE,
    MAX_SCAN_LENGTH,
    CodeError,
    parse_code,
    parser_profiles,
    validate_gtin,
)
from erp_basic.marking.policy import (
    REGULATORY_OPERATIONS,
    PolicyError,
    get_profile,
    list_profiles,
    readiness,
)


# Синтетические тестовые данные; не утверждение о карточке в НК или эмиссии.
GTIN = "09506000134352"
OTHER_GTIN = "09506000134369"
SERIAL = "DEMO-00aA91"
KI = "01" + GTIN + "21" + SERIAL
KM_LONG = KI + GS + "91DEMO" + GS + "92NOT-A-CRYPTO-SIGNATURE="
KM_SHORT = KI + GS + "93TEST"


class MarkingParserTests(unittest.TestCase):
    def assert_code_error(self, code, value, profile="gs1-91-92-v1", **options):
        with self.assertRaises(CodeError) as raised:
            parse_code(value, profile, **options)
        self.assertEqual(code, raised.exception.code)
        return raised.exception

    def test_explicit_long_profile_preserves_exact_input(self):
        parsed = parse_code(KM_LONG, "gs1-91-92-v1")
        self.assertEqual(parsed.raw, KM_LONG)
        self.assertEqual(parsed.normalized, KM_LONG)
        self.assertEqual(parsed.gtin, GTIN)
        self.assertEqual(parsed.serial, SERIAL)
        self.assertEqual(parsed.canonical_ki, KI)
        self.assertEqual(parsed.namespace, IDENTITY_NAMESPACE)
        self.assertEqual(parsed.crypto_fields, (("91", "DEMO"), ("92", "NOT-A-CRYPTO-SIGNATURE=")))

    def test_identity_is_independent_of_crypto_tail_and_parser_profile(self):
        variants = (
            parse_code(KI, "gs1-ki-v1"),
            parse_code(KM_LONG, "gs1-91-92-v1"),
            parse_code(KI + GS + "91OTHER" + GS + "92ALTERNATE", "gs1-91-92-v1"),
            parse_code(KM_SHORT, "gs1-93-v1"),
        )
        self.assertEqual(len({code.identity for code in variants}), 1)
        self.assertEqual(len({code.canonical_ki for code in variants}), 1)
        self.assertEqual(len(variants[0].identity), 64)

    def test_case_and_gtin_are_parts_of_identity(self):
        parsed = parse_code(KI, "gs1-ki-v1")
        lower_case = parse_code(KI.replace(SERIAL, SERIAL.lower()), "gs1-ki-v1")
        other_product = parse_code(KI.replace(GTIN, OTHER_GTIN), "gs1-ki-v1")
        self.assertEqual(len({parsed.identity, lower_case.identity, other_product.identity}), 3)
        self.assertEqual(parsed.gtin[0], "0")

    def test_explicit_ki_does_not_guess_ai_inside_serial(self):
        serial = "00a9192b93"
        parsed = parse_code("01" + GTIN + "21" + serial, "gs1-ki-v1")
        self.assertEqual(parsed.serial, serial)
        self.assertEqual(parsed.crypto_fields, ())

    def test_symbology_identifier_needs_explicit_scanner_setting(self):
        raw = "]d2" + KM_LONG
        self.assert_code_error("symbology_not_enabled", raw)
        parsed = parse_code(raw, "gs1-91-92-v1", allow_symbology_identifier=True)
        self.assertEqual(parsed.raw, raw)
        self.assertEqual(parsed.normalized, KM_LONG)
        self.assertEqual(parsed.identity, parse_code(KM_LONG, "gs1-91-92-v1").identity)

    def test_other_and_repeated_prefixes_are_rejected(self):
        for prefix in ("]D2", "]d1", "]C1"):
            with self.subTest(prefix=prefix):
                self.assert_code_error(
                    "unsupported_symbology", prefix + KM_LONG, allow_symbology_identifier=True
                )
        self.assert_code_error("invalid_characters", "]d2]d2" + KM_LONG, allow_symbology_identifier=True)

    def test_human_readable_does_not_turn_into_scanner_data(self):
        self.assert_code_error(
            "unsupported_representation",
            "(01)" + GTIN + "(21)" + SERIAL + "(91)DEMO(92)TEST",
        )
        for replacement in ("<GS>", "(GS)", "FNC1", "\\u001d", "\\x1d"):
            with self.subTest(replacement=replacement), self.assertRaises(CodeError):
                parse_code(KM_LONG.replace(GS, replacement), "gs1-91-92-v1")

    def test_supported_punctuation_is_preserved(self):
        punctuation = "!\"%&'()*+,-./:;<=>?_"
        self.assertLessEqual(len(punctuation), 20)
        parsed = parse_code("01" + GTIN + "21" + punctuation, "gs1-ki-v1")
        self.assertEqual(parsed.serial, punctuation)

    def test_every_ascii_char_outside_cset82_is_rejected(self):
        permitted = "!\"%&'()*+,-./0123456789:;<=>?ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz"
        self.assertEqual(len(set(permitted)), 82)
        for ordinal in range(128):
            char = chr(ordinal)
            if char in permitted:
                continue
            with self.subTest(ordinal=ordinal), self.assertRaises(CodeError):
                parse_code("01" + GTIN + "21A" + char, "gs1-ki-v1")

    def test_unicode_whitespace_and_control_data_are_not_normalized(self):
        for raw in (
            " " + KM_LONG,
            KM_LONG + " ",
            KM_LONG + "\r\n",
            KM_LONG + "\x00",
            KM_LONG.replace("DEMO", "ДЕМО"),
            KM_LONG.replace("0", "０", 1),
            KM_LONG.replace(GS, "\t"),
            KM_LONG.replace(GS, "\u00a0"),
        ):
            with self.subTest(raw=repr(raw)):
                self.assert_code_error("invalid_characters", raw)

    def test_invalid_types_and_options_are_not_coerced(self):
        for value in (None, 123, b"scan", {}, [], ""):
            with self.subTest(value=value):
                self.assert_code_error("invalid_input", value)
        for option in (1, 0, "false", "true", None):
            with self.subTest(option=option):
                self.assert_code_error("invalid_option", KM_LONG, allow_symbology_identifier=option)
        for profile in (None, {}, [], "auto", "milk", "GS1-93-V1", "gs1-91-92-v1 "):
            with self.subTest(profile=profile):
                self.assert_code_error("unsupported_profile", KM_LONG, profile)

    def test_gtin_check_digit_and_ascii_width(self):
        self.assertEqual(validate_gtin(GTIN), GTIN)
        self.assertEqual(validate_gtin(OTHER_GTIN), OTHER_GTIN)
        self.assert_code_error("invalid_gtin_checksum", KM_LONG.replace(GTIN, GTIN[:-1] + "3"))
        self.assert_code_error("invalid_gtin", KM_LONG.replace(GTIN, "A" + GTIN[1:]))
        for gtin in (GTIN[1:], GTIN + "0", int(GTIN), "９" * 14, None):
            with self.subTest(gtin=gtin), self.assertRaises(CodeError):
                validate_gtin(gtin)

    def test_serial_length_boundaries(self):
        for serial in ("A", "A" * 20):
            parsed = parse_code("01" + GTIN + "21" + serial, "gs1-ki-v1")
            self.assertEqual(parsed.serial, serial)
        self.assert_code_error("invalid_serial_length", "01" + GTIN + "21" + "A" * 21, "gs1-ki-v1")
        with self.assertRaises(CodeError):
            parse_code("01" + GTIN + "21", "gs1-ki-v1")

    def test_additional_field_length_boundaries(self):
        for length in (1, 90):
            raw = KI + GS + "91" + "X" * length + GS + "92" + "Y" * length
            self.assertEqual(len(parse_code(raw, "gs1-91-92-v1").crypto_fields[1][1]), length)
            short = parse_code(KI + GS + "93" + "Z" * length, "gs1-93-v1")
            self.assertEqual(len(short.crypto_fields[0][1]), length)
        for raw in (KI + GS + "91" + GS + "92A", KI + GS + "91A" + GS + "92", KI + GS + "93"):
            profile = "gs1-93-v1" if raw.endswith("93") else "gs1-91-92-v1"
            self.assert_code_error("invalid_field_length", raw, profile)
        self.assert_code_error("invalid_field_length", KI + GS + "93" + "X" * 91, "gs1-93-v1")

    def test_raw_size_is_bounded_before_parsing(self):
        self.assert_code_error("scan_too_long", "X" * (MAX_SCAN_LENGTH + 1))

    def test_missing_duplicate_trailing_and_extra_separators_are_rejected(self):
        malformed = (
            KM_LONG.replace(GS, ""),
            KM_LONG.replace(GS, "", 1),
            KM_LONG + GS,
            GS + KM_LONG,
            KM_LONG.replace(GS, GS + GS, 1),
            KM_LONG + GS + "93UNEXPECTED",
            "01" + GTIN + GS + "21" + SERIAL + GS + "91D" + GS + "92T",
        )
        for raw in malformed:
            with self.subTest(raw=repr(raw)):
                self.assert_code_error("invalid_structure", raw)

    def test_wrong_field_order_duplicates_and_profile_are_rejected(self):
        for raw in (
            KI + GS + "92A" + GS + "91B",
            KI + GS + "91A" + GS + "91B",
            KI + GS + "93A" + GS + "92B",
        ):
            with self.subTest(raw=repr(raw)):
                self.assert_code_error("unexpected_ai", raw)
        self.assert_code_error("invalid_structure", KM_SHORT)
        self.assert_code_error("invalid_structure", KM_LONG, "gs1-93-v1")
        self.assert_code_error("invalid_structure", KM_SHORT, "gs1-ki-v1")
        self.assert_code_error("invalid_structure", KM_LONG.replace("21" + SERIAL, "10" + SERIAL))

    def test_unknown_grammars_do_not_trigger_an_automatic_parser(self):
        for value in (
            "https://example.invalid/01/" + GTIN + "/21/" + SERIAL,
            "00" + "0" * 18,
            "93TOBACCO-STYLE",
            "QR:DEMO",
        ):
            with self.subTest(value=value), self.assertRaises(CodeError):
                parse_code(value, "gs1-ki-v1")
        self.assertEqual(parser_profiles(), ("gs1-ki-v1", "gs1-91-92-v1", "gs1-93-v1"))

    def test_result_is_immutable_and_sensitive_fields_are_opt_in(self):
        parsed = parse_code(KM_LONG, "gs1-91-92-v1")
        with self.assertRaises(FrozenInstanceError):
            parsed.gtin = OTHER_GTIN
        safe = parsed.as_dict()
        for field in ("raw", "normalized", "crypto_fields"):
            self.assertNotIn(field, safe)
        self.assertNotIn("NOT-A-CRYPTO-SIGNATURE", repr(parsed))
        self.assertNotIn(SERIAL, repr(parsed))
        self.assertTrue(safe["syntax_valid"])
        self.assertFalse(safe["authenticity_verified"])
        self.assertEqual(parsed.as_dict(include_sensitive=True)["raw"], KM_LONG)
        with self.assertRaises(CodeError):
            parsed.as_dict(include_sensitive="false")

    def test_error_does_not_leak_scanned_data(self):
        raw = KM_LONG + "PRIVATE-SECRET" + "\x00"
        error = self.assert_code_error("invalid_characters", raw)
        self.assertNotIn("PRIVATE-SECRET", str(error))
        self.assertNotIn(SERIAL, str(error))


class MarkingPolicyTests(unittest.TestCase):
    def test_profiles_are_explicit_teaching_modes_not_commodity_groups(self):
        profiles = list_profiles()
        self.assertEqual([profile["id"] for profile in profiles], ["test-pu-v1", "test-osu-v1"])
        self.assertEqual([profile["mode"] for profile in profiles], ["unit", "quantity"])
        self.assertEqual([profile["parser_profile"] for profile in profiles], ["gs1-91-92-v1", "gs1-93-v1"])
        for profile in profiles:
            self.assertTrue(profile["test_only"])
            self.assertFalse(profile["live_supported"])
            self.assertIsNone(profile["commodity_group"])
            self.assertFalse(profile["capabilities"]["fiscalization"])
            self.assertFalse(profile["capabilities"]["external_transmission"])

    def test_profile_reads_cannot_mutate_the_allowlist(self):
        editable = get_profile("test-pu-v1")
        editable["operations"].append("commission")
        editable["capabilities"]["external_transmission"] = True
        editable["live_supported"] = True
        fresh = get_profile("test-pu-v1")
        self.assertNotIn("commission", fresh["operations"])
        self.assertFalse(fresh["capabilities"]["external_transmission"])
        self.assertFalse(fresh["live_supported"])

    def test_local_operations_require_an_explicit_test_or_demo_mode(self):
        self.assertFalse(readiness("test-pu-v1")["ready"])
        for mode in ("test", "demo"):
            for profile in list_profiles():
                for operation in profile["operations"]:
                    result = readiness(profile["id"], mode, operation)
                    with self.subTest(mode=mode, profile=profile["id"], operation=operation):
                        self.assertTrue(result["ready"])
                        self.assertFalse(result["external_allowed"])
                        self.assertFalse(result["live_ready"])
                        self.assertTrue(result["live_blockers"])

    def test_live_mode_is_blocked_for_every_published_operation(self):
        for profile in list_profiles():
            for operation in list(profile["operations"]) + sorted(REGULATORY_OPERATIONS):
                result = readiness(profile["id"], "live", operation)
                with self.subTest(profile=profile["id"], operation=operation):
                    self.assertFalse(result["ready"])
                    self.assertFalse(result["external_allowed"])
                    self.assertFalse(result["live_ready"])
                    self.assertIn("test_profile_only", [item["code"] for item in result["blockers"]])

    def test_test_mode_cannot_masquerade_as_real_regulatory_operation(self):
        for profile in list_profiles():
            for mode in ("test", "demo"):
                for operation in REGULATORY_OPERATIONS:
                    result = readiness(profile["id"], mode, operation)
                    with self.subTest(profile=profile["id"], mode=mode, operation=operation):
                        self.assertFalse(result["ready"])
                        self.assertEqual(result["blockers"][0]["code"], "external_operation_unavailable")

    def test_unknown_profile_deployment_and_operation_fail_closed(self):
        for profile in ("milk", "test-pu-v2", None, [], {}):
            with self.subTest(profile=profile), self.assertRaises(PolicyError):
                get_profile(profile)
        for mode in ("production", "false", "TEST", True, None, {}, []):
            with self.subTest(mode=mode), self.assertRaises(PolicyError):
                readiness("test-pu-v1", mode)
        for operation in ("sell", "", True, None, {}, []):
            with self.subTest(operation=operation), self.assertRaises(PolicyError):
                readiness("test-pu-v1", "test", operation)
        with self.assertRaises(PolicyError):
            readiness("test-osu-v1", "test", "aggregate")


if __name__ == "__main__":
    unittest.main()
