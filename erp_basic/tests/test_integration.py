"""Проверки интеграции ERPNext на изолированном Site с MariaDB.

Запуск: bench --site <test-site> execute erp_basic.tests.test_integration.run.
Сценарий обмена двух настоящих серверов запускается отдельно;
инструкции находятся в docs/clientora-integration.md.
"""

import copy
import json
import secrets
import threading
import unittest
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import frappe
from frappe.utils import getdate, nowdate

from erp_basic import commands, integration
from erp_basic.idempotency import OperationConflict

COMPANY = "ERP Exchange Test"
USER = "exchange.erp-basic@example.invalid"
PRICE_LIST = "ERP Exchange Selling"


def key():
    return "exchange-test-" + uuid.uuid4().hex


def setup_exchange():
    if not frappe.conf.get("allow_tests"):
        frappe.throw("Нужен изолированный Site с allow_tests.")
    frappe.set_user("Administrator")
    if not frappe.db.exists("Item Group", "All Item Groups"):
        from erpnext.setup.setup_wizard.operations.install_fixtures import install
        install("Russian Federation")
    year = getdate(nowdate()).year
    for number in (year - 1, year, year + 1):
        if not frappe.db.exists("Fiscal Year", str(number)):
            frappe.get_doc({"doctype": "Fiscal Year", "year": str(number),
                            "year_start_date": f"{number}-01-01", "year_end_date": f"{number}-12-31"}).insert()
    if not frappe.db.exists("Company", COMPANY):
        frappe.get_doc({"doctype": "Company", "company_name": COMPANY, "abbr": "EEX",
                        "country": "Russian Federation", "default_currency": "RUB",
                        "chart_of_accounts": "Standard", "enable_perpetual_inventory": 1}).insert()
    if not frappe.db.exists("Price List", PRICE_LIST):
        frappe.get_doc({"doctype": "Price List", "price_list_name": PRICE_LIST,
                        "selling": 1, "currency": "RUB"}).insert()
    for doctype, name, field, parent in (
            ("Customer Group", "ERP Exchange Customers", "customer_group_name", "All Customer Groups"),
            ("Item Group", "ERP Exchange Items", "item_group_name", "All Item Groups")):
        if not frappe.db.exists(doctype, name):
            frappe.get_doc({"doctype": doctype, field: name, "parent_" + doctype.lower().replace(" ", "_"): parent}).insert()
    if not frappe.db.exists("Customer", "ERP Exchange Customer"):
        frappe.get_doc({"doctype": "Customer", "customer_name": "ERP Exchange Customer",
                        "customer_type": "Company", "customer_group": "ERP Exchange Customers",
                        "territory": "All Territories", "default_currency": "RUB",
                        "default_price_list": PRICE_LIST}).insert()
    if not frappe.db.exists("Item", "ERP-EXCHANGE-SERVICE"):
        frappe.get_doc({"doctype": "Item", "item_code": "ERP-EXCHANGE-SERVICE",
                        "item_name": "ERP Exchange Service", "item_group": "ERP Exchange Items",
                        "stock_uom": "Nos", "is_stock_item": 0, "is_sales_item": 1}).insert()
    if not frappe.db.exists("Item Price", {"item_code": "ERP-EXCHANGE-SERVICE", "price_list": PRICE_LIST}):
        frappe.get_doc({"doctype": "Item Price", "item_code": "ERP-EXCHANGE-SERVICE",
                        "price_list": PRICE_LIST, "price_list_rate": 125, "currency": "RUB"}).insert()
    if not frappe.db.exists("User", USER):
        frappe.get_doc({"doctype": "User", "email": USER, "first_name": "Exchange",
                        "enabled": 1, "send_welcome_email": 0, "user_type": "System User",
                        "roles": [{"role": role} for role in ("ERP Basic Integration", "ERP Basic User", "Sales User")]}).insert()
    if not frappe.db.exists("User Permission", {"user": USER, "allow": "Company", "for_value": COMPANY}):
        frappe.get_doc({"doctype": "User Permission", "user": USER, "allow": "Company", "for_value": COMPANY,
                        "apply_to_all_doctypes": 1}).insert()
    frappe.db.commit()
    return {"company": COMPANY, "user": USER, "customer": "ERP Exchange Customer",
            "item": "ERP-EXCHANGE-SERVICE", "price_list": PRICE_LIST}


def make_connector(source_tenant=None, customer_id=None, offering_id=None):
    source_tenant = source_tenant or str(uuid.uuid4())
    customer_id, offering_id = customer_id or str(uuid.uuid4()), offering_id or str(uuid.uuid4())
    connector = frappe.get_doc({"doctype": integration.CONNECTOR, "company": COMPANY,
                               "source_system": "clientora", "source_tenant": source_tenant,
                               "integration_user": USER, "selling_price_list": PRICE_LIST,
                               "enabled": 1, "outbound_enabled": 0}).insert()
    for external_type, external_id, doctype, name in (
            ("customer", customer_id, "Customer", "ERP Exchange Customer"),
            ("offering", offering_id, "Item", "ERP-EXCHANGE-SERVICE")):
        integration.set_mapping(COMPANY, connector.name, external_type, external_id, doctype, name)
    return connector, customer_id, offering_id


def envelope(connector, customer_id, offering_id, *, version=1, object_id=None, quantity="2"):
    return {"schema_version": 1, "message_id": str(uuid.uuid4()), "source": "clientora",
            "source_tenant": connector.source_tenant, "event_type": "order.proposed",
            "object_type": "order", "object_id": object_id or str(uuid.uuid4()), "object_version": version,
            "occurred_at": datetime.now(timezone.utc).isoformat(), "correlation_id": str(uuid.uuid4()),
            "causation_id": None, "payload": {"customer_id": customer_id, "currency": "RUB",
            "delivery_date": nowdate(), "items": [{"offering_id": offering_id, "quantity": quantity}],
            "crm_status": "accepted", "quoted_total_minor": 1}}


class TestIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = setup_exchange()

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.db.savepoint("exchange_test")
        self.connector, self.customer, self.offering = make_connector()
        self.message = envelope(self.connector, self.customer, self.offering)
        frappe.set_user(USER)

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.db.rollback(save_point="exchange_test")
        frappe.clear_document_cache("Customer", self.env["customer"])
        frappe.local.document_cache = {}
        frappe.db.value_cache = {}
        frappe.clear_cache(user=USER)

    def test_native_prices_duplicate_and_body_mismatch(self):
        first = integration.receive(self.message)
        self.assertTrue(first["accepted"], first)
        self.assertEqual(first["status"], "Applied")
        native = frappe.get_doc("Sales Order", first["erp_name"])
        self.assertEqual(native.docstatus, 0)
        self.assertEqual(native.grand_total, 250)
        self.assertEqual(native.items[0].rate, 125)
        self.assertEqual(integration.receive(copy.deepcopy(self.message)), first)
        self.assertEqual(frappe.db.count(integration.INBOX, {"connector": self.connector.name}), 1)
        self.assertEqual(frappe.db.count(integration.OUTBOX, {"connector": self.connector.name}), 1)
        changed = copy.deepcopy(self.message)
        changed["payload"]["items"][0]["quantity"] = "3"
        with self.assertRaises(OperationConflict):
            integration.receive(changed)
        native.reload()
        self.assertEqual(native.items[0].qty, 2)

    def test_missing_mapping_is_durable_and_resumable(self):
        self.message["payload"]["customer_id"] = str(uuid.uuid4())
        answer = integration.receive(self.message)
        self.assertEqual(answer["status"], "Needs Mapping", answer)
        mapping = frappe.db.get_value(integration.MAPPING,
            integration.mapping_name(self.connector.name, "order", self.message["object_id"]), "erp_name")
        self.assertFalse(mapping)
        self.assertFalse(frappe.db.exists(integration.OUTBOX, {"connector": self.connector.name}))
        frappe.set_user("Administrator")
        integration.set_mapping(COMPANY, self.connector.name, "customer", self.message["payload"]["customer_id"],
                                "Customer", self.env["customer"])
        name = integration._message_name(self.connector.name, self.message["message_id"])
        result = integration.retry_inbox(COMPANY, name)
        self.assertEqual(result["status"], "Applied", result)
        self.assertEqual(frappe.db.count(integration.INBOX, {"connector": self.connector.name}), 1)

    def test_stale_and_same_version_conflict(self):
        self.message["object_version"] = 3
        first = integration.receive(self.message)
        self.assertEqual(first["status"], "Applied", first)
        older = envelope(self.connector, self.customer, self.offering, version=2,
                         object_id=self.message["object_id"], quantity="9")
        self.assertEqual(integration.receive(older)["status"], "Stale")
        changed = envelope(self.connector, self.customer, self.offering, version=3,
                           object_id=self.message["object_id"], quantity="8")
        self.assertEqual(integration.receive(changed)["status"], "Conflict")
        self.assertEqual(frappe.db.get_value("Sales Order Item", {"parent": first["erp_name"]}, "qty"), 2)

    def test_duplicate_version_cannot_bypass_document_user_permission(self):
        first = integration.receive(self.message)
        self.assertEqual(first["status"], "Applied", first)
        frappe.set_user("Administrator")
        other = frappe.get_doc({"doctype": "Customer", "customer_name": "Другой клиент " + uuid.uuid4().hex[:10],
                               "customer_type": "Company", "customer_group": "ERP Exchange Customers",
                               "territory": "All Territories"}).insert()
        frappe.get_doc({"doctype": "User Permission", "user": USER, "allow": "Customer",
                        "for_value": other.name, "apply_to_all_doctypes": 1}).insert()
        frappe.clear_cache(user=USER)
        frappe.set_user(USER)
        native = frappe.get_doc("Sales Order", first["erp_name"])
        self.assertFalse(frappe.has_permission("Sales Order", "read", doc=native))
        replay = copy.deepcopy(self.message)
        for fresh_uuid in (False, True):
            if fresh_uuid:
                replay["message_id"] = str(uuid.uuid4())
            with self.subTest(fresh_uuid=fresh_uuid), self.assertRaises(frappe.PermissionError):
                integration.receive(replay)

    def test_native_edit_and_submitted_order_are_not_overwritten(self):
        first = integration.receive(self.message)
        self.assertEqual(first["status"], "Applied", first)
        frappe.set_user("Administrator")
        native = frappe.get_doc("Sales Order", first["erp_name"])
        native.items[0].rate = 150
        native.save()
        frappe.set_user(USER)
        newer = envelope(self.connector, self.customer, self.offering, version=2,
                         object_id=self.message["object_id"], quantity="4")
        self.assertEqual(integration.receive(newer)["status"], "Conflict")
        native.reload()
        self.assertEqual(native.items[0].rate, 150)
        frappe.set_user("Administrator")
        commands.transition("orders", native.name, COMPANY, str(native.modified), key(), "submit")
        frappe.set_user(USER)
        newer["message_id"] = str(uuid.uuid4())
        self.assertEqual(integration.receive(newer)["status"], "Conflict")
        native.reload()
        self.assertEqual(native.docstatus, 1)
        self.assertEqual(native.items[0].qty, 2)
        self.assertGreaterEqual(frappe.db.count(integration.OUTBOX, {"connector": self.connector.name}), 2)

    def test_invalid_system_fields_and_cross_tenant_source(self):
        bad = copy.deepcopy(self.message)
        bad["payload"]["company"] = "Other Company"
        self.assertEqual(integration.receive(bad)["error"]["code"], "invalid_payload")
        self.assertFalse(frappe.db.exists(integration.INBOX, {"connector": self.connector.name}))
        bad = copy.deepcopy(self.message)
        bad["source_tenant"] = str(uuid.uuid4())
        with self.assertRaises(frappe.PermissionError):
            integration.receive(bad)

    def test_revoked_company_blocks_even_saved_response(self):
        first = integration.receive(self.message)
        self.assertEqual(first["status"], "Applied", first)
        frappe.set_user("Administrator")
        frappe.db.delete("User Permission", {"user": USER, "allow": "Company", "for_value": COMPANY})
        frappe.clear_cache(user=USER)
        frappe.set_user(USER)
        with self.assertRaises(frappe.PermissionError):
            integration.receive(self.message)

    def test_native_validation_rolls_back_business_and_outbox(self):
        frappe.set_user("Administrator")
        customer = frappe.get_doc("Customer", self.env["customer"])
        customer.disabled = 1
        customer.save()
        count = frappe.db.count("Sales Order", {"company": COMPANY})
        frappe.set_user(USER)
        answer = integration.receive(self.message)
        self.assertEqual(answer["status"], "Rejected", answer)
        self.assertEqual(frappe.db.count("Sales Order", {"company": COMPANY}), count)
        self.assertFalse(frappe.db.exists(integration.OUTBOX, {"connector": self.connector.name}))
        self.assertEqual(frappe.db.count(integration.INBOX, {"connector": self.connector.name, "status": "Rejected"}), 1)

    def test_customer_master_is_not_rewritten_and_journal_has_no_secrets(self):
        before = frappe.db.get_value("Customer", self.env["customer"], ["customer_name", "tax_id"], as_dict=True)
        answer = integration.receive(self.message)
        self.assertEqual(answer["status"], "Applied", answer)
        after = frappe.db.get_value("Customer", self.env["customer"], ["customer_name", "tax_id"], as_dict=True)
        self.assertEqual(before, after)
        view = integration.journal(COMPANY, connector=self.connector.name, start=0, page_length=1)
        self.assertEqual(view["incoming_total"], 1)
        self.assertEqual(view["outgoing_total"], 1)
        self.assertTrue(view["can_retry"])
        self.assertFalse(view["can_manage_mappings"])
        self.assertNotIn("payload", view["incoming"][0])
        self.assertNotIn("remote_token", json.dumps(view, default=str))

    def test_crm_workflow_change_does_not_rewrite_submitted_accounting(self):
        first = integration.receive(self.message)
        self.assertEqual(first["status"], "Applied", first)
        frappe.set_user("Administrator")
        native = frappe.get_doc("Sales Order", first["erp_name"])
        commands.transition("orders", native.name, COMPANY, str(native.modified), key(), "submit")
        native.reload()
        modified = str(native.modified)
        frappe.set_user(USER)
        changed = copy.deepcopy(self.message)
        changed["message_id"] = str(uuid.uuid4())
        changed["object_version"] = 2
        changed["payload"]["crm_status"] = "completed"
        result = integration.receive(changed)
        self.assertEqual(result["status"], "Applied", result)
        native.reload()
        self.assertEqual(native.docstatus, 1)
        self.assertEqual(str(native.modified), modified)

    def test_explicit_conflict_resolution_keeps_erp_and_is_idempotent(self):
        first = integration.receive(self.message)
        self.assertEqual(first["status"], "Applied", first)
        frappe.set_user("Administrator")
        native = frappe.get_doc("Sales Order", first["erp_name"])
        native.items[0].rate = 150
        native.save()
        frappe.set_user(USER)
        changed = envelope(self.connector, self.customer, self.offering, version=2,
                           object_id=self.message["object_id"], quantity="4")
        self.assertEqual(integration.receive(changed)["status"], "Conflict")
        name = integration._message_name(self.connector.name, changed["message_id"])
        frappe.set_user("Administrator")
        native.reload()
        parameters = (COMPANY, name, "keep_erp", str(native.modified), "Проверено владельцем: оставляем цену и количество ERP.")
        resolved = integration.resolve_conflict(*parameters)
        self.assertEqual(resolved["status"], "Stale")
        self.assertEqual(integration.resolve_conflict(*parameters), resolved)
        native.reload()
        self.assertEqual((native.items[0].qty, native.items[0].rate), (2, 150))
        frappe.set_user(USER)
        self.assertEqual(integration.receive(changed), resolved)


class TestOutgoingRevocation(unittest.TestCase):
    def test_current_service_rights_are_checked_before_any_http(self):
        setup_exchange()
        frappe.set_user("Administrator")
        previous_local = frappe.conf.get("erp_basic_allow_local_exchange")
        previous_pause = frappe.conf.get("erp_basic_disable_external_calls")
        frappe.conf.erp_basic_allow_local_exchange = 1
        frappe.conf.erp_basic_disable_external_calls = 0
        roles = [row.role for row in frappe.get_doc("User", USER).roles]
        received = []

        class Probe(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(self.path)
                self.send_response(503)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Probe)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connector, customer, offering = make_connector()
        connector.outbound_enabled = 1
        connector.endpoint = "http://127.0.0.1:" + str(server.server_port) + "/erp-events"
        connector.remote_token = secrets.token_urlsafe(40)
        connector.save()
        message = envelope(connector, customer, offering)
        frappe.set_user(USER)
        first = integration.receive(message)
        self.assertEqual(first["status"], "Applied", first)
        native_name = first["erp_name"]
        frappe.set_user("Administrator")
        frappe.db.commit()
        name = frappe.db.get_value(integration.OUTBOX, {"connector": connector.name}, "name")

        def restore():
            frappe.set_user("Administrator")
            user = frappe.get_doc("User", USER)
            user.enabled = 1
            user.set("roles", [{"role": value} for value in roles])
            user.save()
            if not frappe.db.exists("User Permission", {"user": USER, "allow": "Company", "for_value": COMPANY}):
                frappe.get_doc({"doctype": "User Permission", "user": USER, "allow": "Company",
                                "for_value": COMPANY, "apply_to_all_doctypes": 1}).insert()
            frappe.clear_cache(user=USER)
            frappe.db.commit()

        try:
            for revoked in ("Company", "Integration role", "Native read", "Enabled"):
                with self.subTest(revoked=revoked):
                    restore()
                    if revoked == "Company":
                        permission = frappe.db.get_value("User Permission",
                            {"user": USER, "allow": "Company", "for_value": COMPANY}, "name")
                        frappe.delete_doc("User Permission", permission)
                    else:
                        user = frappe.get_doc("User", USER)
                        if revoked == "Enabled":
                            user.enabled = 0
                        else:
                            removed = "ERP Basic Integration" if revoked == "Integration role" else "Sales User"
                            user.set("roles", [{"role": value} for value in roles if value != removed])
                        user.save()
                    frappe.clear_cache(user=USER)
                    frappe.db.commit()
                    integration.dispatch_pending()
                    record = frappe.db.get_value(integration.OUTBOX, name, ["status", "last_error"], as_dict=True)
                    self.assertEqual(record.status, "Dead Letter")
                    self.assertEqual(record.last_error, "access_revoked")
                    self.assertEqual(received, [], "Отозванные права не должны допускать даже попытку HTTP.")
                    restore()
                    self.assertEqual(integration.retry_outbox(COMPANY, name)["status"], "Pending")
                    frappe.db.commit()
        finally:
            restore()
            frappe.db.delete(integration.OUTBOX, {"connector": connector.name})
            frappe.db.delete(integration.INBOX, {"connector": connector.name})
            frappe.db.delete(integration.MAPPING, {"connector": connector.name})
            frappe.delete_doc(integration.CONNECTOR, connector.name)
            frappe.delete_doc("Sales Order", native_name)
            frappe.db.commit()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            frappe.conf.erp_basic_allow_local_exchange = previous_local
            frappe.conf.erp_basic_disable_external_calls = previous_pause


def run():
    suite = unittest.TestSuite([
        unittest.defaultTestLoader.loadTestsFromTestCase(TestIntegration),
        unittest.defaultTestLoader.loadTestsFromTestCase(TestOutgoingRevocation),
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    frappe.db.rollback()
    if not result.wasSuccessful():
        raise AssertionError(f"Проверки обмена не пройдены: {len(result.failures)} неудач, {len(result.errors)} ошибок")
    return {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors)}


def prepare_http_fixture(config_path):
    """Создание тестовых API-пользователей для двух серверов без вывода ключей."""
    import os
    from pathlib import Path
    from frappe.core.doctype.user.user import generate_keys
    from frappe.installer import update_site_config

    setup_exchange()
    path = Path(config_path)
    config = json.loads(path.read_text())
    for key_name in ("org_id", "customer_id", "offering_id", "erp_source_tenant"):
        integration._uuid(config[key_name], key_name)
    callback = config["clientora_url"].rstrip("/") + "/integrations/erp/v1/events/" + config["org_id"]
    if not callback.startswith("http://127.0.0.1:") or len(config["callback_token"]) < 32:
        frappe.throw("Тестовые данные разрешены только для изолированных локальных HTTP-сервисов.")
    update_site_config("erp_basic_allow_local_exchange", 1)
    update_site_config("erp_basic_disable_external_calls", 0)
    frappe.conf.erp_basic_allow_local_exchange = 1
    frappe.conf.erp_basic_disable_external_calls = 0
    name = frappe.db.get_value(integration.CONNECTOR, {"source_system": "clientora",
                                                     "source_tenant": config["org_id"]}, "name")
    if name:
        connector = frappe.get_doc(integration.CONNECTOR, name)
    else:
        connector = frappe.get_doc({"doctype": integration.CONNECTOR, "company": COMPANY,
                                   "source_system": "clientora", "source_tenant": config["org_id"],
                                   "local_tenant": config["erp_source_tenant"], "integration_user": USER,
                                   "selling_price_list": PRICE_LIST})
    connector.enabled, connector.outbound_enabled = 1, 1
    connector.endpoint, connector.remote_token = callback, config["callback_token"]
    connector.save() if name else connector.insert()
    for external_type, external_id, doctype, native in (
            ("customer", config["customer_id"], "Customer", "ERP Exchange Customer"),
            ("offering", config["offering_id"], "Item", "ERP-EXCHANGE-SERVICE")):
        integration.set_mapping(COMPANY, connector.name, external_type, external_id, doctype, native)
    reviewer = "reviewer.erp-exchange@example.invalid"
    if not frappe.db.exists("User", reviewer):
        frappe.get_doc({"doctype": "User", "email": reviewer, "first_name": "Exchange reviewer",
                        "enabled": 1, "send_welcome_email": 0, "user_type": "System User",
                        "roles": [{"role": name} for name in ("ERP Basic User", "Sales User")]}).insert()
    if not frappe.db.exists("User Permission", {"user": reviewer, "allow": "Company", "for_value": COMPANY}):
        frappe.get_doc({"doctype": "User Permission", "user": reviewer, "allow": "Company",
                        "for_value": COMPANY, "apply_to_all_doctypes": 1}).insert()
    config["erp_credentials"], config["reviewer_credentials"] = generate_keys(USER), generate_keys(reviewer)
    config["erp_company"], config["erp_connector"] = COMPANY, connector.name
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as target:
        json.dump(config, target)
    path.chmod(0o600)
    frappe.db.commit()
    return {"configured": True, "company": COMPANY, "connector": connector.name}
