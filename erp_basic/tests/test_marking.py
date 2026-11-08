"""Приёмка маркировки на настоящих таблицах и native контроллерах ERPNext.

Запуск только на изолированном Site с allow_tests. Искусственные сканы здесь
не являются кодами СУЗ; ни одна проверка не отправляет их во внешний контур.
"""

import json
import unittest
import uuid

import frappe
from frappe.utils import nowdate
from frappe.utils.password import decrypt

from erp_basic import commands
from erp_basic.idempotency import OperationConflict
from erp_basic.marking import migration, outbox, packages, service, storage
from erp_basic.marking.codes import GS, parse_code
from erp_basic.tests import fixtures


MANAGER = "marking.manager@example.invalid"
OPERATOR = "marking.operator@example.invalid"
OTHER = "marking.other@example.invalid"
UNSCOPED = "marking.unscoped@example.invalid"
INTEGRATION = "marking.integration@example.invalid"
GTIN = "09506000134352"
OTHER_GTIN = "09506000134369"
PREFIX = storage.PREFIX


def key():
    return "marking-test-" + uuid.uuid4().hex


def synthetic_code(serial="TEST-SERIAL-001", gtin=GTIN, short=False):
    """Fixture для теста; это не генератор реальных КМ."""
    ki = "01" + gtin + "21" + serial
    return ki + GS + "93DEMO" if short else ki + GS + "91DEMO" + GS + "92NOT-A-REAL-SIGNATURE="


def setup_marking():
    env = fixtures.setup()
    fixtures.make_user(MANAGER, ["ERP Basic Marking Manager", "Stock User", "Sales User"], [env["company"]])
    fixtures.make_user(OPERATOR, ["ERP Basic Marking User", "Stock User", "Sales User"], [env["company"]])
    fixtures.make_user(OTHER, ["ERP Basic Marking User", "Stock User", "Sales User"], [env["other_company"]])
    fixtures.make_user(UNSCOPED, ["ERP Basic Marking User", "Stock User"], [])
    fixtures.make_user(
        INTEGRATION, ["ERP Basic Integration", "ERP Basic Marking Manager", "Stock User"], [env["company"]]
    )
    frappe.db.commit()
    return env


class TestMarkingService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = setup_marking()

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.db.savepoint("marking_test")
        self.company = self.env["company"]
        self.item = fixtures.item("MARKING").name
        self.warehouse = self.env["warehouse"]
        frappe.set_user(MANAGER)
        service.configure(self.company, "Preparation", "Unconfigured", {}, key())

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.db.rollback(save_point="marking_test")
        frappe.local.document_cache = {}
        frappe.db.value_cache = {}
        for user in (MANAGER, OPERATOR, OTHER, UNSCOPED, INTEGRATION):
            frappe.clear_cache(user=user)

    def job(self, quantity=1, profile="test-pu-v1", operation="receive", role="wholesale", **options):
        return service.create_job(
            options.pop("company", self.company), role, profile, operation,
            [{"item_code": self.item, "gtin": GTIN, "quantity": quantity}], key(), **options
        )

    def scan(self, job, raw=None, **options):
        return service.scan_code(
            self.company, job["name"], raw or synthetic_code(),
            options.pop("revision", job["revision"]), options.pop("idempotency_key", key()), **options
        )

    def rejected_call(self, error, function, *args, **kwargs):
        """Отказ запроса откатывается как настоящий неуспешный HTTP-запрос."""
        savepoint = "rejected_" + uuid.uuid4().hex[:12]
        frappe.db.savepoint(savepoint)
        try:
            with self.assertRaises(error) as raised:
                function(*args, **kwargs)
            return raised.exception
        finally:
            frappe.db.rollback(save_point=savepoint)

    def native_order(self, quantity=1):
        frappe.set_user("Administrator")
        answer = commands.save_document(
            "orders", self.company,
            {"customer": self.env["customer"], "transaction_date": nowdate(), "delivery_date": nowdate(),
             "selling_price_list": "ERP Basic Selling",
             "items": [{"item_code": self.item, "qty": quantity, "rate": 100, "warehouse": self.warehouse}]},
            idempotency_key=key(),
        )
        frappe.set_user(MANAGER)
        return answer["document"]

    def native_receipt(self, quantity=1):
        frappe.set_user("Administrator")
        answer = commands.save_document(
            "receipts", self.company,
            {"supplier": self.env["supplier"], "posting_date": nowdate(),
             "buying_price_list": "ERP Basic Buying",
             "items": [{"item_code": self.item, "qty": quantity, "rate": 100,
                        "warehouse": self.warehouse}]}, idempotency_key=key(),
        )
        frappe.set_user(MANAGER)
        return answer["document"]

    def test_roles_company_scope_and_integration_service_are_enforced(self):
        for user in ("sales.erp-basic@example.invalid", UNSCOPED, INTEGRATION):
            frappe.set_user(user)
            self.rejected_call(frappe.PermissionError, self.job)
        frappe.set_user("observer.erp-basic@example.invalid")
        self.assertEqual(service.workspace(self.company, "wholesale")["company"], self.company)
        self.rejected_call(frappe.PermissionError, self.job)
        frappe.set_user(OPERATOR)
        self.assertTrue(service.permissions()["scan"])
        self.assertFalse(service.permissions()["configure"])
        self.rejected_call(
            frappe.PermissionError, service.configure, self.company, "Test", "Clientora", {}, key()
        )
        self.assertEqual(self.job()["job"]["status"], "Draft")

    def test_revoked_company_blocks_saved_idempotent_response(self):
        arguments = (self.company, "wholesale", "test-pu-v1", "receive",
                     [{"item_code": self.item, "gtin": GTIN, "quantity": 1}], key())
        first = service.create_job(*arguments)
        self.assertEqual(service.create_job(*arguments), first)
        frappe.set_user("Administrator")
        frappe.db.delete("User Permission", {"user": MANAGER, "allow": "Company", "for_value": self.company})
        frappe.clear_cache(user=MANAGER)
        frappe.set_user(MANAGER)
        self.rejected_call(frappe.PermissionError, service.create_job, *arguments)

    def test_preparation_and_test_configuration_do_not_enable_live(self):
        preparation = service.get_setup(self.company)
        self.assertEqual(preparation["environment"], "Preparation")
        self.assertFalse(preparation["readiness"]["ready"])
        self.assertTrue(preparation["readiness"]["blockers"])
        configured = service.configure(self.company, "Test", "Clientora", {"roles": ["retail"]}, key())
        self.assertEqual(configured["environment"], "Test")
        self.assertFalse(configured["readiness"]["external_allowed"])
        self.rejected_call(frappe.ValidationError, service.configure, self.company, "Live", "ERPBasic", {}, key())
        self.rejected_call(
            frappe.ValidationError, service.configure, self.company, "Test", "ERPBasic", {"api_key": "secret"}, key()
        )
        old = frappe.conf.get("allow_tests")
        try:
            frappe.conf.allow_tests = 0
            self.assertEqual(service.get_setup(self.company)["environment"], "Preparation")
            self.rejected_call(
                frappe.ValidationError, service.configure, self.company, "Test", "ERPBasic", {}, key()
            )
        finally:
            frappe.conf.allow_tests = old

    def test_create_replay_and_changed_payload_use_one_job(self):
        before = frappe.db.count(PREFIX + "Job", {"company": self.company})
        request_key = key()
        arguments = (self.company, "wholesale", "test-pu-v1", "receive",
                     [{"item_code": self.item, "gtin": GTIN, "quantity": 1}], request_key)
        first = service.create_job(*arguments)
        self.assertEqual(service.create_job(*arguments), first)
        changed = list(arguments)
        changed[4] = [{"item_code": self.item, "gtin": GTIN, "quantity": 2}]
        self.rejected_call(OperationConflict, service.create_job, *changed)
        self.assertEqual(frappe.db.count(PREFIX + "Job", {"company": self.company}), before + 1)

    def test_scan_replay_device_event_and_second_key_do_not_duplicate_quantity(self):
        initial = self.job()["job"]
        device, event = str(uuid.uuid4()), str(uuid.uuid4())
        request_key = key()
        arguments = (self.company, initial["name"], synthetic_code(), 0, request_key)
        event_options = {"device_id": device, "event_id": event, "sequence": 1}
        accepted = service.scan_code(*arguments, **event_options)
        self.assertEqual(accepted["scan"]["result"], "Accepted")
        self.assertEqual(service.scan_code(*arguments, **event_options), accepted)
        replay = service.scan_code(*arguments[:-1], key(), **event_options)
        self.assertEqual(replay["scan"]["name"], accepted["scan"]["name"])
        self.assertEqual(replay["job"]["accepted_count"], 1)
        self.assertEqual(replay["job"]["revision"], 1)
        self.assertEqual(frappe.db.count(PREFIX + "Scan", {"job": initial["name"]}), 1)
        self.assertEqual(frappe.db.count(PREFIX + "Code", {"item_code": self.item}), 1)
        duplicate = self.scan(replay["job"], device_id=device, event_id=str(uuid.uuid4()), sequence=2)
        self.assertEqual(duplicate["scan"]["result"], "Duplicate")
        self.assertEqual(duplicate["job"]["accepted_count"], 1)
        self.assertEqual(frappe.db.count(PREFIX + "Scan", {"job": initial["name"]}), 2)

    def test_scan_key_event_and_sequence_conflicts_preserve_reserve(self):
        initial = self.job(quantity=2)["job"]
        device, event = str(uuid.uuid4()), str(uuid.uuid4())
        request_key = key()
        first = self.scan(initial, idempotency_key=request_key, device_id=device, event_id=event, sequence=1)
        self.rejected_call(
            OperationConflict, service.scan_code, self.company, initial["name"], synthetic_code("DIFFERENT"),
            0, request_key, device_id=device, event_id=event, sequence=1,
        )
        self.rejected_call(
            OperationConflict, service.scan_code, self.company, initial["name"], synthetic_code("DIFFERENT"),
            1, key(), device_id=device, event_id=event, sequence=1,
        )
        self.rejected_call(
            OperationConflict, service.scan_code, self.company, initial["name"], synthetic_code("NEXT"),
            1, key(), device_id=device, event_id=str(uuid.uuid4()), sequence=3,
        )
        self.assertEqual(service.get_job(self.company, initial["name"])["job"]["accepted_count"], 1)
        self.assertEqual(frappe.db.count(PREFIX + "Scan", {"job": initial["name"]}), 1)
        self.assertEqual(first["job"]["revision"], 1)

    def test_gtin_mismatch_and_bad_scan_are_persisted_rejections(self):
        initial = self.job()["job"]
        wrong = self.scan(initial, synthetic_code(gtin=OTHER_GTIN))
        self.assertEqual(wrong["scan"]["result"], "Rejected")
        self.assertEqual(wrong["job"]["accepted_count"], 0)
        self.assertEqual(wrong["job"]["rejected_count"], 1)
        self.assertIn("GTIN", wrong["scan"]["reason"])
        invalid = self.scan(wrong["job"], "malformed\x00private")
        self.assertEqual(invalid["scan"]["result"], "Rejected")
        self.assertEqual(invalid["job"]["rejected_count"], 2)
        self.assertEqual(frappe.db.count(PREFIX + "Scan", {"job": initial["name"], "result": "Rejected"}), 2)
        self.assertEqual(frappe.db.count(PREFIX + "Code", {"item_code": self.item}), 0)
        self.assertNotIn("private", frappe.as_json(service.get_job(self.company, initial["name"])))

    def test_reservation_conflict_and_cancellation_release_one_instance(self):
        one = self.job()["job"]
        two = self.job()["job"]
        first = self.scan(one)
        second = self.scan(two)
        self.assertEqual(second["scan"]["result"], "Conflict")
        self.assertEqual(second["job"]["accepted_count"], 0)
        cancelled = service.cancel_job(self.company, one["name"], first["job"]["revision"], "Тест отмены", key())
        self.assertEqual(cancelled["job"]["status"], "Cancelled")
        accepted = self.scan(second["job"])
        self.assertEqual(accepted["scan"]["result"], "Accepted")
        identity = parse_code(synthetic_code(), "gs1-91-92-v1").identity
        self.assertEqual(frappe.db.get_value(PREFIX + "Code", identity, "reserved_job"), two["name"])
        self.assertEqual(frappe.db.count(PREFIX + "Code", {"item_code": self.item}), 1)

    def test_site_identity_does_not_duplicate_into_other_company_or_leak_owner(self):
        own = self.job()["job"]
        self.scan(own)
        frappe.set_user(OTHER)
        foreign = self.job(company=self.env["other_company"])["job"]
        rejected = service.scan_code(self.env["other_company"], foreign["name"], synthetic_code(), 0, key())
        self.assertEqual(rejected["scan"]["result"], "Rejected")
        self.assertEqual(rejected["job"]["accepted_count"], 0)
        self.assertNotIn(self.company, json.dumps(rejected))
        self.assertNotIn(own["name"], json.dumps(rejected))
        self.assertEqual(frappe.db.count(PREFIX + "Code", {"item_code": self.item}), 1)
        self.rejected_call(frappe.PermissionError, service.get_job, self.env["other_company"], own["name"])
        self.rejected_call(frappe.PermissionError, service.get_job, self.company, own["name"])

    def test_expected_chunks_are_deduplicated_and_reconciled(self):
        initial = self.job(quantity=3)["job"]
        codes = [synthetic_code("EXPECTED-" + str(index)) for index in range(3)]
        kis = [parse_code(code, "gs1-91-92-v1").canonical_ki for code in codes]
        first = service.import_expected_codes(self.company, initial["name"], kis[:2], 0, key())
        request_key = key()
        second = service.import_expected_codes(self.company, initial["name"], kis[1:], 1, request_key)
        replay = service.import_expected_codes(self.company, initial["name"], kis[1:], 1, request_key)
        self.assertEqual(second, replay)
        self.assertEqual(first["discrepancy"]["missing_count"], 2)
        self.assertEqual(second["discrepancy"]["missing_count"], 3)
        self.assertEqual(frappe.db.count(PREFIX + "Expected", {"job": initial["name"]}), 3)
        extra = self.scan(second["job"], synthetic_code("NOT-EXPECTED"))
        self.assertEqual(extra["scan"]["result"], "Rejected")
        current = extra["job"]
        for code in codes:
            current = self.scan(current, code)["job"]
        completed = service.complete_job(self.company, initial["name"], current["revision"], key())
        self.assertEqual(completed["job"]["status"], "Prepared")
        self.assertEqual(completed["discrepancy"]["missing_count"], 0)
        self.assertEqual(completed["discrepancy"]["extra_count"], 0)
        self.assertFalse(completed["readiness"]["ready"])
        self.assertFalse(completed["job"]["native_posted"])

    def test_expected_chunks_validate_before_scanning_and_roll_back_overflow(self):
        initial = self.job()["job"]
        kis = [parse_code(synthetic_code("OVER-" + str(index)), "gs1-91-92-v1").canonical_ki
               for index in range(2)]
        self.rejected_call(
            frappe.ValidationError, service.import_expected_codes, self.company, initial["name"], kis, 0, key()
        )
        self.assertEqual(frappe.db.count(PREFIX + "Expected", {"job": initial["name"]}), 0)
        self.rejected_call(
            frappe.ValidationError, service.import_expected_codes,
            self.company, initial["name"], [kis[0]] * 1001, 0, key(),
        )
        scanned = self.scan(initial)
        self.rejected_call(
            frappe.ValidationError, service.import_expected_codes,
            self.company, initial["name"], kis[:1], scanned["job"]["revision"], key(),
        )

    def test_quantity_mode_never_creates_invented_unit_codes(self):
        initial = self.job(quantity=4, profile="test-osu-v1")["job"]
        bad_scan = self.scan(initial, synthetic_code(short=True))
        self.assertEqual(bad_scan["scan"]["result"], "Rejected")
        first = service.record_quantity(self.company, initial["name"], GTIN, 3, 1, key())
        self.rejected_call(
            frappe.ValidationError, service.complete_job, self.company, initial["name"], 2, key()
        )
        self.rejected_call(
            frappe.ValidationError, service.record_quantity, self.company, initial["name"], GTIN, 5, 2, key()
        )
        finished = service.record_quantity(self.company, initial["name"], GTIN, 4, 2, key())
        self.assertEqual(first["job"]["accepted_count"], 3)
        self.assertEqual(finished["job"]["accepted_count"], 4)
        completed = service.complete_job(self.company, initial["name"], 3, key())
        self.assertEqual(completed["job"]["status"], "Prepared")
        self.assertEqual(frappe.db.count(PREFIX + "Code", {"item_code": self.item}), 0)
        self.assertEqual(frappe.db.count(PREFIX + "Expected", {"job": initial["name"]}), 0)

    def test_sensitive_scan_is_encrypted_and_not_exposed_in_journals(self):
        initial = self.job()["job"]
        raw = synthetic_code("PRIVATE-SERIAL")
        accepted = self.scan(initial, raw)
        identity = parse_code(raw, "gs1-91-92-v1").identity
        stored = frappe.db.get_value(PREFIX + "Code", identity, "*", as_dict=True)
        self.assertNotEqual(stored.raw_encrypted, raw)
        self.assertEqual(decrypt(stored.raw_encrypted), raw)
        self.assertEqual(stored.external_state, "Unverified")
        self.assertFalse(stored.external_checked_at)
        visible = frappe.as_json([accepted, service.get_job(self.company, initial["name"]),
                                  service.workspace(self.company, "wholesale")])
        operation_responses = json.dumps(frappe.db.sql(
            "SELECT response FROM `tabERP Basic Operation` WHERE company=%s AND operation LIKE 'marking.%%'",
            (self.company,),
        ))
        for text in (visible, operation_responses):
            self.assertNotIn(raw, text)
            self.assertNotIn("NOT-A-REAL-SIGNATURE", text)
            self.assertNotIn("PRIVATE-SERIAL", text)

    def test_same_ki_changed_crypto_is_a_conflict_not_another_instance(self):
        initial = self.job()["job"]
        first = self.scan(initial)
        changed = self.scan(first["job"], synthetic_code().replace("NOT-A-REAL-SIGNATURE", "ALTERED"))
        self.assertEqual(changed["scan"]["result"], "Conflict")
        self.assertEqual(changed["job"]["accepted_count"], 1)
        prefixed = self.scan(changed["job"], "]d2" + synthetic_code())
        self.assertEqual(prefixed["scan"]["result"], "Duplicate")
        self.assertEqual(frappe.db.count(PREFIX + "Code", {"item_code": self.item}), 1)

    def test_package_tree_single_parent_and_opening_history(self):
        job = self.job(quantity=2)["job"]
        codes = [synthetic_code("PACKAGE-" + str(index)) for index in range(2)]
        for code in codes:
            job = self.scan(job, code)["job"]
        first = packages.create_package(self.company, job["name"], "Учебный короб A", job["revision"], key())
        job = service.get_job(self.company, job["name"])["job"]
        second = packages.create_package(self.company, job["name"], "Учебный короб B", job["revision"], key())
        first, second = first["package"], second["package"]
        identity = parse_code(codes[0], "gs1-91-92-v1").identity
        request_key = key()
        added = packages.change_member(
            self.company, first["name"], "code", identity, "add", 0, "Учебная укладка", request_key
        )
        self.assertEqual(packages.change_member(
            self.company, first["name"], "code", identity, "add", 0, "Учебная укладка", request_key
        ), added)
        self.rejected_call(
            OperationConflict, packages.change_member,
            self.company, second["name"], "code", identity, "add", 0, "Проверка дубля", key(),
        )
        parent = packages.change_member(
            self.company, second["name"], "package", first["name"], "add", 0, "Вложенный короб", key()
        )
        self.rejected_call(
            frappe.ValidationError, packages.change_member,
            self.company, first["name"], "package", second["name"], "add", 1, "Проверка цикла", key(),
        )
        self.assertEqual(packages.get_package(self.company, first["name"])["package"]["parent_package"],
                         second["name"])
        self.assertFalse(parent["package"]["parent_package"])
        packages.change_member(
            self.company, second["name"], "package", first["name"], "remove", 1, "Частичное вскрытие", key()
        )
        packages.change_member(
            self.company, first["name"], "code", identity, "remove", 1, "Извлечение экземпляра", key()
        )
        moved = packages.change_member(
            self.company, second["name"], "code", identity, "add", 2, "Переупаковка экземпляра", key()
        )
        sealed = packages.change_state(
            self.company, second["name"], "Sealed", moved["package"]["revision"], "Закрытие короба", key()
        )
        self.rejected_call(
            frappe.ValidationError, packages.change_member,
            self.company, second["name"], "code", identity, "remove", sealed["package"]["revision"],
            "Нельзя без вскрытия", key(),
        )
        self.assertEqual(frappe.db.get_value(PREFIX + "Code", identity, "package"), second["name"])
        self.assertFalse(sealed["regulatory_confirmed"])
        self.assertEqual(sealed["namespace"], "erp-basic-internal-package-v1")
        self.assertGreaterEqual(frappe.db.count(
            PREFIX + "History", {"job": job["name"], "action": "PackageCompositionChanged"}
        ), 5)
        frappe.set_user(OTHER)
        self.rejected_call(
            frappe.PermissionError, packages.get_package, self.env["other_company"], second["name"]
        )

    def test_migration_preview_writes_nothing_and_masks_codes(self):
        source = "migration-" + uuid.uuid4().hex
        raw = synthetic_code("IMP-" + uuid.uuid4().hex[:12])
        rows = [{"external_id": "fixture-1", "raw_code": raw, "item_code": self.item,
                 "warehouse": self.warehouse, "batch_no": "DEMO-IMPORT", "expiry_date": "2099-12-31"}]
        doctypes = [PREFIX + kind for kind in ("Code", "Import", "Job", "Scan", "History", "Operation")]
        doctypes += ["ERP Basic Operation", "Stock Ledger Entry", "GL Entry", "Payment Entry"]
        before = {doctype: frappe.db.count(doctype, {"company": self.company}) for doctype in doctypes}
        preview = migration.preview(self.company, source, "test-pu-v1", rows)
        self.assertTrue(preview["valid"])
        self.assertEqual(preview["ready"], 1)
        self.assertEqual(preview["existing"], 0)
        self.assertEqual(preview["native_movements"], 0)
        self.assertEqual(preview["external_operations"], 0)
        self.assertFalse(preview["regulatory_confirmed"])
        self.assertNotIn("NOT-A-REAL-SIGNATURE", frappe.as_json(preview))
        self.assertNotIn(parse_code(raw, "gs1-91-92-v1").serial, frappe.as_json(preview))
        for doctype, count in before.items():
            self.assertEqual(frappe.db.count(doctype, {"company": self.company}), count, doctype)
        invalid = migration.preview(self.company, source, "test-pu-v1", rows + [{**rows[0], "external_id": "fixture-2"}])
        self.assertFalse(invalid["valid"])
        self.assertEqual(invalid["rows"][1]["status"], "Invalid")
        self.assertEqual(frappe.db.count(PREFIX + "Import", {"company": self.company}), before[PREFIX + "Import"])

    def test_migration_apply_quarantines_without_ledgers_and_replays_one_identity(self):
        source = "migration-" + uuid.uuid4().hex
        raw = synthetic_code("IMP-" + uuid.uuid4().hex[:12])
        rows = [{"external_id": "fixture-1", "raw_code": raw, "item_code": self.item,
                 "warehouse": self.warehouse, "batch_no": "DEMO-IMPORT", "expiry_date": "2099-12-31"}]
        before = {doctype: frappe.db.count(doctype, {"company": self.company})
                  for doctype in ("Stock Ledger Entry", "GL Entry", "Payment Entry", PREFIX + "Operation")}
        plan = migration.preview(self.company, source, "test-pu-v1", rows)
        request_key = key()
        applied = migration.apply(self.company, source, "test-pu-v1", rows, plan["plan_hash"], request_key)
        self.assertTrue(applied["applied"])
        self.assertEqual(migration.apply(self.company, source, "test-pu-v1", rows, plan["plan_hash"], request_key), applied)
        existing = migration.preview(self.company, source, "test-pu-v1", rows)
        self.assertEqual(existing["existing"], 1)
        self.assertEqual(existing["ready"], 0)
        second_key = migration.apply(self.company, source, "test-pu-v1", rows, existing["plan_hash"], key())
        self.assertEqual(second_key["existing"], 1)
        self.assertEqual(frappe.db.count(PREFIX + "Import", {"company": self.company, "source_id": source}), 1)
        self.assertEqual(frappe.db.count(PREFIX + "Code", {"item_code": self.item}), 1)
        code = frappe.db.get_value(PREFIX + "Code", parse_code(raw, "gs1-91-92-v1").identity, "*", as_dict=True)
        self.assertEqual(code.local_state, "Quarantine")
        self.assertEqual(code.external_state, "Unverified")
        self.assertFalse(code.external_checked_at)
        self.assertFalse(code.reserved_job)
        self.assertFalse(code.package)
        self.assertEqual(code.warehouse, self.warehouse)
        self.assertEqual(code.batch_no, "DEMO-IMPORT")
        self.assertEqual(str(code.expiry_date), "2099-12-31")
        self.assertNotEqual(code.raw_encrypted, raw)
        self.assertEqual(decrypt(code.raw_encrypted), raw)
        for doctype, count in before.items():
            self.assertEqual(frappe.db.count(doctype, {"company": self.company}), count, doctype)

    def test_migration_rejects_changed_plan_source_collision_and_metadata_overwrite(self):
        source = "migration-" + uuid.uuid4().hex
        raw = synthetic_code("IMP-" + uuid.uuid4().hex[:12])
        rows = [{"external_id": "fixture-1", "raw_code": raw, "item_code": self.item,
                 "warehouse": self.warehouse, "batch_no": "DEMO-ORIGINAL"}]
        plan = migration.preview(self.company, source, "test-pu-v1", rows)
        self.rejected_call(OperationConflict, migration.apply, self.company, source, "test-pu-v1", rows,
                           "0" * 64, key())
        self.assertEqual(frappe.db.count(PREFIX + "Code", {"item_code": self.item}), 0)
        request_key = key()
        migration.apply(self.company, source, "test-pu-v1", rows, plan["plan_hash"], request_key)
        changed = [{**rows[0], "batch_no": "DEMO-CHANGED"}]
        changed_plan = migration.preview(self.company, source, "test-pu-v1", changed)
        self.assertFalse(changed_plan["valid"])
        self.rejected_call(OperationConflict, migration.apply, self.company, source, "test-pu-v1", changed,
                           changed_plan["plan_hash"], request_key)
        self.rejected_call(OperationConflict, migration.apply, self.company, source, "test-pu-v1", changed,
                           changed_plan["plan_hash"], key())
        original = parse_code(raw, "gs1-91-92-v1").identity
        self.assertEqual(frappe.db.get_value(PREFIX + "Code", original, "batch_no"), "DEMO-ORIGINAL")
        replacement = [{**rows[0], "raw_code": synthetic_code("DIFFERENT-IMPORT") }]
        collision = migration.preview(self.company, source, "test-pu-v1", replacement)
        self.assertFalse(collision["valid"])
        self.rejected_call(OperationConflict, migration.apply, self.company, source, "test-pu-v1", replacement,
                           collision["plan_hash"], key())
        self.assertEqual(frappe.db.count(PREFIX + "Code", {"item_code": self.item}), 1)
        self.assertEqual(frappe.db.count(PREFIX + "Import", {"company": self.company, "source_id": source}), 1)

    def test_plain_doctype_save_cannot_change_controlled_state(self):
        initial = self.job()["job"]
        frappe.set_user("Administrator")
        document = frappe.get_doc(PREFIX + "Job", initial["name"])
        document.status = "Posted"
        self.rejected_call(frappe.PermissionError, document.save)
        self.assertEqual(frappe.db.get_value(PREFIX + "Job", initial["name"], "status"), "Draft")

    def test_completion_is_local_only_and_does_not_post_native_order(self):
        order = self.native_receipt()
        created = service.create_job(
            self.company, "wholesale", "test-pu-v1", "receive",
            [{"item_code": self.item, "gtin": GTIN, "quantity": 1, "reference_line": order["items"][0]["name"]}],
            key(), reference_kind="receipts", reference_name=order["name"], warehouse=self.warehouse,
        )
        scanned = self.scan(created["job"])
        counts = {doctype: frappe.db.count(doctype, {"company": self.company})
                  for doctype in ("Stock Ledger Entry", "GL Entry", "Payment Entry")}
        prepared = service.complete_job(self.company, created["job"]["name"], scanned["job"]["revision"], key())
        self.assertEqual(prepared["job"]["status"], "Prepared")
        self.assertEqual(prepared["job"]["external_state"], "Unverified")
        self.assertFalse(prepared["job"]["native_posted"])
        self.assertEqual(frappe.db.get_value("Purchase Receipt", order["name"], "docstatus"), 0)
        for doctype, count in counts.items():
            self.assertEqual(frappe.db.count(doctype, {"company": self.company}), count)

    def test_one_native_line_cannot_confirm_two_independent_code_sets(self):
        receipt = self.native_receipt()
        line = {"item_code": self.item, "gtin": GTIN, "quantity": 1,
                "reference_line": receipt["items"][0]["name"]}
        self.rejected_call(
            frappe.ValidationError, service.create_job, self.company, "wholesale", "test-pu-v1", "receive",
            [line, {**line, "gtin": OTHER_GTIN}], key(),
            reference_kind="receipts", reference_name=receipt["name"],
        )
        first = service.create_job(
            self.company, "wholesale", "test-pu-v1", "receive", [line], key(),
            reference_kind="receipts", reference_name=receipt["name"],
        )["job"]
        self.rejected_call(
            OperationConflict, service.create_job, self.company, "wholesale", "test-pu-v1", "receive", [line], key(),
            reference_kind="receipts", reference_name=receipt["name"],
        )
        service.cancel_job(self.company, first["name"], 0, "Повторная проверка основания", key())
        replacement = service.create_job(
            self.company, "wholesale", "test-pu-v1", "receive", [line], key(),
            reference_kind="receipts", reference_name=receipt["name"],
        )["job"]
        self.assertNotEqual(replacement["name"], first["name"])
        self.assertEqual(frappe.db.count(PREFIX + "Job", {
            "reference_doctype": "Purchase Receipt", "reference_name": receipt["name"],
            "status": ["!=", "Cancelled"],
        }), 1)
        self.assertEqual(frappe.db.count("Stock Ledger Entry", {"voucher_no": receipt["name"]}), 0)

    def test_inventory_operation_cannot_receive_an_already_received_unit_again(self):
        receipt = self.native_receipt()
        first = service.create_job(
            self.company, "wholesale", "test-pu-v1", "receive",
            [{"item_code": self.item, "gtin": GTIN, "quantity": 1,
              "reference_line": receipt["items"][0]["name"]}], key(),
            reference_kind="receipts", reference_name=receipt["name"], warehouse=self.warehouse,
        )["job"]
        raw = synthetic_code("INV-" + uuid.uuid4().hex[:12])
        first = self.scan(first, raw)["job"]
        service.complete_job(self.company, first["name"], first["revision"], key())
        frappe.set_user("Administrator")
        frappe.get_doc("Purchase Receipt", receipt["name"]).submit()
        code_id = parse_code(raw, "gs1-91-92-v1").identity
        self.assertEqual(frappe.db.get_value(PREFIX + "Code", code_id, "local_state"), "Received")
        second_receipt = self.native_receipt()
        inventory = service.create_job(
            self.company, "wholesale", "test-pu-v1", "inventory",
            [{"item_code": self.item, "gtin": GTIN, "quantity": 1,
              "reference_line": second_receipt["items"][0]["name"]}], key(),
            reference_kind="receipts", reference_name=second_receipt["name"], warehouse=self.warehouse,
        )["job"]
        scanned = self.scan(inventory, raw)
        self.assertEqual(scanned["scan"]["result"], "Accepted")
        service.complete_job(self.company, inventory["name"], scanned["job"]["revision"], key())
        frappe.set_user("Administrator")
        native = frappe.get_doc("Purchase Receipt", second_receipt["name"])
        self.rejected_call(frappe.ValidationError, native.submit)
        self.assertEqual(frappe.db.get_value("Purchase Receipt", native.name, "docstatus"), 0)
        self.assertEqual(frappe.db.count("Stock Ledger Entry", {"voucher_no": native.name}), 0)
        self.assertEqual(frappe.db.count("GL Entry", {"voucher_no": native.name}), 0)
        self.assertEqual(frappe.db.count("Stock Ledger Entry", {"item_code": self.item}), 1)
        self.assertEqual(frappe.db.get_value("Bin", {"item_code": self.item, "warehouse": self.warehouse}, "actual_qty"), 1)
        self.assertEqual(frappe.db.count(PREFIX + "Code", {"item_code": self.item}), 1)
        self.assertEqual(frappe.db.get_value(PREFIX + "Code", code_id, "local_state"), "Received")
        self.assertEqual(frappe.db.get_value(PREFIX + "Job", inventory["name"], "status"), "Prepared")

    def test_changed_native_document_invalidates_preparation(self):
        order = self.native_receipt()
        created = service.create_job(
            self.company, "wholesale", "test-pu-v1", "receive",
            [{"item_code": self.item, "gtin": GTIN, "quantity": 1, "reference_line": order["items"][0]["name"]}],
            key(), reference_kind="receipts", reference_name=order["name"],
        )
        scanned = self.scan(created["job"])
        frappe.set_user("Administrator")
        native = frappe.get_doc("Purchase Receipt", order["name"])
        native.items[0].qty = 2
        native.items[0].received_qty = 2
        native.save()
        frappe.set_user(MANAGER)
        self.rejected_call(
            OperationConflict, service.complete_job,
            self.company, created["job"]["name"], scanned["job"]["revision"], key(),
        )
        self.assertEqual(frappe.db.get_value(PREFIX + "Job", created["job"]["name"], "status"), "Draft")

    def test_native_receipt_posts_physical_fact_without_regulatory_confirmation(self):
        receipt = self.native_receipt()
        created = service.create_job(
            self.company, "wholesale", "test-pu-v1", "receive",
            [{"item_code": self.item, "gtin": GTIN, "quantity": 1,
              "reference_line": receipt["items"][0]["name"]}],
            key(), reference_kind="receipts", reference_name=receipt["name"], warehouse=self.warehouse,
        )
        scanned = self.scan(created["job"])
        service.complete_job(self.company, created["job"]["name"], scanned["job"]["revision"], key())
        frappe.set_user("Administrator")
        native = frappe.get_doc("Purchase Receipt", receipt["name"])
        native.submit()
        identity = parse_code(synthetic_code(), "gs1-91-92-v1").identity
        code = frappe.db.get_value(PREFIX + "Code", identity, "*", as_dict=True)
        self.assertEqual(code.local_state, "Received")
        self.assertEqual(code.warehouse, self.warehouse)
        self.assertFalse(code.reserved_job)
        self.assertEqual(code.external_state, "Unverified")
        self.assertFalse(code.external_checked_at)
        posted = service.get_job(self.company, created["job"]["name"])["job"]
        self.assertEqual(posted["status"], "Posted")
        self.assertTrue(posted["native_posted"])
        self.assertEqual(frappe.db.count("Stock Ledger Entry", {"voucher_no": native.name}), 1)
        self.rejected_call(frappe.ValidationError, native.cancel)
        self.assertEqual(frappe.db.get_value("Purchase Receipt", native.name, "docstatus"), 1)

    def test_enforce_blocks_native_delivery_before_any_ledger_effect(self):
        receipt = self.native_receipt(quantity=2)
        frappe.set_user("Administrator")
        frappe.get_doc("Purchase Receipt", receipt["name"]).submit()
        frappe.set_user(MANAGER)
        service.enroll_product(self.company, self.item, GTIN, "test-pu-v1", "Enforce", key())
        frappe.set_user("Administrator")
        delivery = commands.save_document(
            "deliveries", self.company,
            {"customer": self.env["customer"], "posting_date": nowdate(),
             "selling_price_list": "ERP Basic Selling",
             "items": [{"item_code": self.item, "qty": 1, "rate": 100,
                        "warehouse": self.warehouse}]}, idempotency_key=key(),
        )["document"]
        native = frappe.get_doc("Delivery Note", delivery["name"])
        error = self.rejected_call(frappe.ValidationError, native.submit)
        self.assertIn("маркированного", str(error))
        self.assertEqual(frappe.db.get_value("Delivery Note", native.name, "docstatus"), 0)
        self.assertEqual(frappe.db.count("Stock Ledger Entry", {"voucher_no": native.name}), 0)
        self.assertEqual(frappe.db.count("GL Entry", {"voucher_no": native.name}), 0)
        self.assertEqual(frappe.db.get_value(
            "Bin", {"item_code": self.item, "warehouse": self.warehouse}, "actual_qty"
        ), 2)


QUEUE_COMPANY = "ERP Marking Queue Test"
QUEUE_USER = "marking.queue@example.invalid"


class TestMarkingOutbox(unittest.TestCase):
    """Отдельный Company: настоящие worker commit не используют savepoint suite."""

    @classmethod
    def setUpClass(cls):
        if not frappe.conf.get("allow_tests"):
            frappe.throw("Нужен изолированный Site с allow_tests.")
        frappe.set_user("Administrator")
        if not frappe.db.exists("Company", QUEUE_COMPANY):
            frappe.get_doc({"doctype": "Company", "company_name": QUEUE_COMPANY, "abbr": "EMQT",
                            "country": "Russian Federation", "default_currency": "RUB",
                            "chart_of_accounts": "Standard", "enable_perpetual_inventory": 1}).insert()
        fixtures.make_user(QUEUE_USER, ["ERP Basic Marking Manager", "Stock User"], [QUEUE_COMPANY])
        frappe.db.commit()

    def setUp(self):
        # Только память этого allow_tests-процесса и единственный test-simulator-v1;
        # site_config и запрет реальных внешних адаптеров не изменяются.
        previous_pause = frappe.conf.get("erp_basic_disable_external_calls")
        self.addCleanup(setattr, frappe.conf, "erp_basic_disable_external_calls", previous_pause)
        frappe.conf.erp_basic_disable_external_calls = 0
        frappe.set_user("Administrator")
        self.item = fixtures.item("MARK-QUEUE").name
        self.raw = synthetic_code("Q-" + uuid.uuid4().hex[:16])
        frappe.set_user(QUEUE_USER)
        service.configure(QUEUE_COMPANY, "Test", "Unconfigured", {}, key())
        created = service.create_job(
            QUEUE_COMPANY, "manufacturer", "test-pu-v1", "manufacture",
            [{"item_code": self.item, "gtin": GTIN, "quantity": 1}], key(),
        )
        self.job = created["job"]["name"]
        service.scan_code(QUEUE_COMPANY, self.job, self.raw, 0, key())
        service.complete_job(QUEUE_COMPANY, self.job, 1, key())
        frappe.db.commit()

    def tearDown(self):
        # Этот выделенный тестовый Company принадлежит только данному suite.
        # Чистим его технические записи напрямую после настоящих worker commit;
        # производственные команды удаления истории при этом не появляются.
        frappe.set_user("Administrator")
        frappe.db.rollback()
        frappe.db.set_value("User", QUEUE_USER, "enabled", 1, update_modified=False)
        for kind in ("Device", "Scan", "Expected", "History", "Package", "Operation", "Code", "Job", "Setup"):
            frappe.db.delete(PREFIX + kind, {"company": QUEUE_COMPANY})
        frappe.db.delete("ERP Basic Operation", {"company": QUEUE_COMPANY})
        frappe.delete_doc("Item", self.item, ignore_permissions=True)
        frappe.db.commit()
        frappe.clear_cache(user=QUEUE_USER)

    def test_unknown_is_not_repeated_and_reconciliation_keeps_the_business_key(self):
        queued = outbox.enqueue(QUEUE_COMPANY, self.job, "commission", "unknown", key())
        original = frappe.db.get_value(PREFIX + "Operation", queued["name"], "business_key")
        frappe.db.commit()
        unknown = outbox.dispatch_one(queued["name"])
        self.assertEqual(unknown["state"], "Unknown")
        self.assertEqual(unknown["attempts"], 1)
        again = outbox.dispatch_one(queued["name"])
        self.assertEqual(again["state"], "Unknown")
        self.assertEqual(again["attempts"], 1)
        code_id = parse_code(self.raw, "gs1-91-92-v1").identity
        self.assertEqual(frappe.db.get_value(PREFIX + "Code", code_id, "reserved_job"), self.job)
        with self.assertRaises(OperationConflict):
            service.cancel_job(QUEUE_COMPANY, self.job, 2, "Неизвестная отправка", key())
        frappe.db.rollback()
        confirmed = outbox.reconcile_one(queued["name"])
        self.assertEqual(confirmed["state"], "Accepted")
        self.assertEqual(confirmed["attempts"], 1)
        self.assertEqual(frappe.db.get_value(PREFIX + "Operation", queued["name"], "business_key"), original)
        result = json.loads(frappe.db.get_value(PREFIX + "Operation", queued["name"], "result"))
        self.assertTrue(result["test_only"])
        self.assertFalse(result["regulatory_confirmed"])
        self.assertFalse(result["fiscalized"])
        self.assertEqual(frappe.db.get_value(PREFIX + "Code", code_id, "reserved_job"), self.job)
        self.assertEqual(frappe.db.get_value(PREFIX + "Code", code_id, "external_state"), "Unverified")
        self.assertEqual(frappe.db.count("Stock Ledger Entry", {"company": QUEUE_COMPANY}), 0)
        self.assertEqual(frappe.db.count("GL Entry", {"company": QUEUE_COMPANY}), 0)

    def test_business_key_deduplicates_new_transport_key_and_rejects_changed_snapshot(self):
        one = outbox.enqueue(QUEUE_COMPANY, self.job, "commission", "accept", key())
        two = outbox.enqueue(QUEUE_COMPANY, self.job, "commission", "accept", key())
        self.assertEqual(one["name"], two["name"])
        self.assertEqual(frappe.db.count(PREFIX + "Operation", {"job": self.job}), 1)
        frappe.db.commit()
        with self.assertRaises(OperationConflict):
            outbox.enqueue(QUEUE_COMPANY, self.job, "commission", "reject", key())
        frappe.db.rollback()
        accepted = outbox.dispatch_one(one["name"])
        self.assertEqual(accepted["state"], "Accepted")
        self.assertEqual(outbox.dispatch_one(one["name"])["attempts"], 1)

    def test_restored_or_revoked_environment_does_not_send(self):
        queued = outbox.enqueue(QUEUE_COMPANY, self.job, "commission", "accept", key())
        frappe.db.commit()
        frappe.conf.erp_basic_disable_external_calls = 1
        paused = outbox.dispatch_one(queued["name"])
        self.assertEqual(paused["state"], "Ready")
        self.assertEqual(paused["attempts"], 0)
        frappe.conf.erp_basic_disable_external_calls = 0
        previous = frappe.conf.get("allow_tests")
        try:
            frappe.conf.allow_tests = 0
            result = outbox.dispatch_one(queued["name"])
            self.assertEqual(result["state"], "Ready")
            self.assertEqual(result["attempts"], 0)
        finally:
            frappe.conf.allow_tests = previous
        frappe.set_user("Administrator")
        frappe.db.set_value("User", QUEUE_USER, "enabled", 0, update_modified=False)
        frappe.db.commit()
        result = outbox.dispatch_one(queued["name"])
        self.assertEqual(result["state"], "Ready")
        self.assertEqual(result["attempts"], 0)

    def test_expired_dispatch_lease_becomes_unknown_without_second_attempt(self):
        queued = outbox.enqueue(QUEUE_COMPANY, self.job, "commission", "accept", key())
        storage.update("Operation", queued["name"], {"state": "Dispatching", "attempts": 1,
                       "lease_token": str(uuid.uuid4()), "lease_until": "2000-01-01 00:00:00"})
        frappe.db.commit()
        result = outbox.dispatch_one(queued["name"])
        self.assertEqual(result["state"], "Unknown")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(frappe.db.count(PREFIX + "History", {"job": self.job, "action": "LeaseExpired"}), 1)
        outbox.dispatch_one(queued["name"])
        self.assertEqual(frappe.db.count(PREFIX + "History", {"job": self.job, "action": "LeaseExpired"}), 1)


def run():
    if not frappe.conf.get("allow_tests"):
        frappe.throw("Нужен изолированный Site с allow_tests.")
    suite = unittest.TestSuite([
        unittest.defaultTestLoader.loadTestsFromTestCase(TestMarkingService),
        unittest.defaultTestLoader.loadTestsFromTestCase(TestMarkingOutbox),
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    frappe.set_user("Administrator")
    frappe.db.rollback()
    if not result.wasSuccessful():
        raise AssertionError(
            f"Проверки маркировки не пройдены: {len(result.failures)} неудач, {len(result.errors)} ошибок"
        )
    return {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
            "database": frappe.db.sql("SELECT VERSION()")[0][0], "external_calls": False}
