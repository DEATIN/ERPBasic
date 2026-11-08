"""Транзакционные проекции v2: отдельные версии заданий и неизменность v1/учёта/фискального владельца."""

import json
import unittest
import uuid

import frappe
from frappe.utils import nowdate

from erp_basic import commands, integration
from erp_basic.marking import projection, service, storage
from erp_basic.tests import fixtures


def key():
    return uuid.uuid4().hex


class TestProjection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = fixtures.setup()
        cls.user = "marking-projection.erp-basic@example.invalid"
        fixtures.make_user(cls.user, ["Sales User", "ERP Basic Integration"], [cls.env["company"]])
        frappe.db.commit()

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.db.savepoint("marking_projection")
        self.company = self.env["company"]
        self.items = [fixtures.item("MARK-PROJ").name for _ in range(2)]
        self.order = commands.save_document("orders", self.company, {
            "customer": self.env["customer"], "transaction_date": nowdate(), "delivery_date": nowdate(),
            "selling_price_list": "ERP Basic Selling", "items": [{"item_code": item, "qty": 1, "rate": 100,
                                                                    "warehouse": self.env["warehouse"]} for item in self.items],
        }, idempotency_key=key())["document"]
        self.connector = frappe.get_doc({"doctype": integration.CONNECTOR, "company": self.company,
            "source_system": "clientora", "source_tenant": str(uuid.uuid4()), "integration_user": self.user,
            "enabled": 1, "outbound_enabled": 0, "endpoint": "https://clientora.example.invalid/integrations/erp/v1/events/test",
            "remote_token": uuid.uuid4().hex, "marking_enabled": 1,
            "marking_endpoint": "https://clientora.example.invalid/integrations/erp/v2/marking/test"}).insert()
        self.order_id = str(uuid.uuid4())
        integration.set_mapping(self.company, self.connector.name, "order", self.order_id, "Sales Order", self.order["name"])

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.db.rollback(save_point="marking_projection")
        frappe.clear_cache(user=self.user)

    def create(self, index=0, role="manufacturer", operation="manufacture"):
        row = self.order["items"][index]
        return service.create_job(self.company, role, "test-pu-v1", operation,
            [{"item_code": row["item_code"], "gtin": "09506000134352", "quantity": 1, "reference_line": row["name"]}],
            key(), reference_kind="orders", reference_name=self.order["name"])["job"]

    def messages(self):
        rows = frappe.get_all(integration.OUTBOX, filters={"connector": self.connector.name}, fields=["payload"], order_by="creation")
        return [json.loads(row.payload) for row in rows]

    def test_projection_does_not_change_v1_version_or_native_document(self):
        job = self.create()
        first = self.messages()[0]
        self.assertEqual(first["schema_version"], 2)
        self.assertEqual(first["event_type"], "marking.summary_changed")
        self.assertEqual(first["object_id"], self.order_id)
        self.assertEqual(first["payload"]["marking"]["job_id"], job["name"])
        self.assertTrue(first["payload"]["marking"]["test_only"])
        raw = "010950600013435221TEST" + uuid.uuid4().hex[:12] + "\x1d91TEST\x1d92NOT-A-SIGNATURE"
        service.scan_code(self.company, job["name"], raw, 0, key())
        self.assertEqual(len(self.messages()), 1, "Скан не создаёт отдельное сообщение CRM")
        service.complete_job(self.company, job["name"], 1, key())
        messages = self.messages()
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[-1]["object_version"], 3)
        encoded = json.dumps(messages)
        self.assertNotIn("NOT-A-SIGNATURE", encoded)
        self.assertNotIn("raw_code", encoded)
        self.assertEqual(frappe.db.get_value("Sales Order", self.order["name"], "docstatus"), 0)
        self.assertEqual(frappe.db.get_value("Sales Order", self.order["name"], "grand_total"), 200)
        mapping = integration.mapping_name(self.connector.name, "order", self.order_id)
        self.assertEqual(frappe.db.get_value(integration.MAPPING, mapping, "local_version"), 0)
        self.assertEqual(frappe.db.count("Stock Ledger Entry", {"item_code": self.items[0]}), 0)

    def test_independent_job_versions_and_idempotent_queue(self):
        first, second = self.create(0), self.create(1)
        messages = self.messages()
        self.assertEqual([message["object_version"] for message in messages], [1, 1])
        self.assertEqual(len({message["payload"]["marking"]["job_id"] for message in messages}), 2)
        projection.queue_current(self.company, first["name"], key())
        projection.queue_current(self.company, second["name"], key())
        self.assertEqual(len(self.messages()), 2)

    def test_ambiguous_order_mapping_blocks_new_and_pending_projection_without_rolling_back_job(self):
        first = self.create(0)
        pending = self.messages()[0]
        self.assertTrue(integration._outgoing_access(self.connector, pending))
        integration.set_mapping(self.company, self.connector.name, "order", str(uuid.uuid4()),
                                "Sales Order", self.order["name"])
        self.assertFalse(integration._outgoing_access(self.connector, pending),
                         "Сообщение Pending не выбирает одного из двух адресатов")
        second = self.create(1)
        self.assertEqual(second["status"], "Draft")
        self.assertTrue(frappe.db.exists(storage.PREFIX + "Job", second["name"]))
        self.assertEqual(len(self.messages()), 1, "Неоднозначный connector не получает новую проекцию")
        history = frappe.get_all(storage.PREFIX + "History", filters={
            "company": self.company, "job": second["name"], "action": "ProjectionBlocked",
        }, fields=["details"])
        self.assertEqual(len(history), 1, "Один отказ на connector, не отдельный отказ на каждое соответствие")
        details = json.loads(history[0].details)
        self.assertEqual(details["reason"], "ambiguous_order_mapping")
        self.assertEqual(details["connector"], self.connector.name)
        self.assertEqual(details["mapping_count"], 2)
        replay = projection.queue_current(self.company, first["name"], key())
        self.assertEqual(replay["queued"], [])
        self.assertEqual(len(self.messages()), 1)

    def test_v2_opt_in_and_current_service_access(self):
        self.create()
        message = self.messages()[0]
        self.assertTrue(integration._outgoing_access(self.connector, message))
        self.connector.marking_enabled = 0
        self.connector.save()
        self.assertFalse(integration._outgoing_access(self.connector, message))
        self.connector.marking_enabled = 1
        self.connector.save()
        frappe.db.delete("User Permission", {"user": self.user, "allow": "Company", "for_value": self.company})
        self.assertFalse(integration._outgoing_access(self.connector, message))

    def test_fiscal_owner_and_sale_id_are_fixed_at_creation(self):
        service.configure(self.company, "Preparation", "Clientora", {}, key())
        job = self.create(role="retail", operation="sale")
        service.configure(self.company, "Preparation", "ERPBasic", {}, key())
        current = service.get_job(self.company, job["name"])["job"]
        self.assertEqual(current["fiscal_owner"], "Clientora")
        self.assertEqual(current["sale_id"], job["sale_id"])
        self.assertEqual(str(uuid.UUID(current["sale_id"])), current["sale_id"])


def run():
    if not frappe.conf.get("allow_tests"):
        frappe.throw("Требуется изолированный Site с allow_tests.")
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(TestProjection))
    if not result.wasSuccessful():
        raise AssertionError("Marking projection tests failed")
    return {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
            "mocks": False, "scope": "real MariaDB; transport to Clientora tested separately"}
