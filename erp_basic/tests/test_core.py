"""Содержательные приёмочные тесты на настоящих таблицах и контроллерах ERPNext."""

import unittest
from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.utils import nowdate

from erp_basic import api, audit, commands
from erp_basic.idempotency import OperationConflict
from erp_basic.tests import fixtures


def key():
    return "test-" + frappe.generate_hash(length=24)


class TestCore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = fixtures.setup()

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.db.savepoint("core_test")
        self.company = self.env["company"]
        self.warehouse = self.env["warehouse"]

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.db.rollback(save_point="core_test")

    def save(self, kind, values, company=None):
        return commands.save_document(kind, company or self.company, values, idempotency_key=key())

    def submit(self, result, idempotency_key=None):
        doc = result["document"]
        return commands.transition(result["kind"], doc["name"], result["company"], doc["modified"],
                                    idempotency_key or key(), "submit")

    def receipt(self, item_code, qty, rate):
        return self.submit(self.save("receipts", {"supplier": self.env["supplier"], "posting_date": nowdate(),
                                                 "buying_price_list": "ERP Basic Buying",
                                                 "items": [{"item_code": item_code, "qty": qty, "rate": rate,
                                                            "warehouse": self.warehouse}]}))

    def stock(self, item_code):
        return frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": self.warehouse},
                                    ["actual_qty", "valuation_rate", "stock_value"], as_dict=True)

    def order(self, item_code, qty=4, rate=300, company=None):
        return self.save("orders", {"customer": self.env["customer"], "transaction_date": nowdate(),
                                    "delivery_date": nowdate(), "selling_price_list": "ERP Basic Selling",
                                    "items": [{"item_code": item_code, "qty": qty, "rate": rate,
                                               "warehouse": self.warehouse}]}, company)

    def test_receipt_delivery_invoice_payment_and_return(self):
        item = fixtures.item().name
        self.receipt(item, 10, 100)
        self.receipt(item, 10, 200)
        self.assertAlmostEqual(self.stock(item).actual_qty, 20)
        self.assertAlmostEqual(self.stock(item).valuation_rate, 150)
        self.assertAlmostEqual(self.stock(item).stock_value, 3000)
        order = self.submit(self.order(item))
        delivery = commands.create_related("orders", order["document"]["name"], "deliveries", self.company,
                                           order["document"]["modified"], key())
        delivery = self.submit(delivery)
        self.assertAlmostEqual(self.stock(item).actual_qty, 16)
        self.assertAlmostEqual(self.stock(item).stock_value, 2400)
        invoice = commands.create_related("deliveries", delivery["document"]["name"], "invoices", self.company,
                                          delivery["document"]["modified"], key())
        invoice = self.submit(invoice)
        self.assertEqual(Decimal(invoice["document"]["grand_total"]), Decimal("1200"))
        self.assertAlmostEqual(self.stock(item).actual_qty, 16, msg="Счёт не повторяет складское движение")
        cash = frappe.db.get_value("Company", self.company, "default_cash_account")
        payment = api.payment_template("invoices", invoice["document"]["name"], self.company, cash)["document"]
        fields = commands.get_spec("payments")
        values = {field: payment[field] for field in fields["fields"] if payment.get(field) is not None}
        values.update({"paid_amount": "600.00", "received_amount": "600.00", "reference_no": "LOCAL-TEST",
                       "reference_date": nowdate(), "references": [{"reference_doctype": "Sales Invoice",
                       "reference_name": invoice["document"]["name"], "allocated_amount": "600.00"}]})
        self.submit(self.save("payments", values))
        self.assertAlmostEqual(frappe.db.get_value("Sales Invoice", invoice["document"]["name"], "outstanding_amount"), 600)
        returned = commands.create_return("deliveries", delivery["document"]["name"], self.company,
                                           frappe.db.get_value("Delivery Note", delivery["document"]["name"], "modified"), key())
        self.submit(returned)
        self.assertAlmostEqual(self.stock(item).actual_qty, 20)
        self.assertAlmostEqual(self.stock(item).stock_value, 3000)
        self.assertTrue(frappe.db.exists("GL Entry", {"voucher_no": invoice["document"]["name"]}))

    def test_same_key_same_result_and_changed_payload_conflict(self):
        item = fixtures.item(stock=False).name
        values = {"customer": self.env["customer"], "transaction_date": nowdate(), "delivery_date": nowdate(),
                  "selling_price_list": "ERP Basic Selling", "items": [{"item_code": item, "qty": 1, "rate": 75}]}
        token = key()
        first = commands.save_document("orders", self.company, values, idempotency_key=token)
        second = commands.save_document("orders", self.company, values, idempotency_key=token)
        self.assertEqual(first["document"]["name"], second["document"]["name"])
        with self.assertRaises(OperationConflict):
            commands.save_document("orders", self.company, {**values, "remarks": "changed"}, idempotency_key=token)
        submit_key = key()
        posted = self.submit(first, submit_key)
        repeated = self.submit(first, submit_key)
        self.assertEqual(posted["document"], repeated["document"])
        with self.assertRaises(OperationConflict):
            self.submit(posted)

    def test_permissions_lists_role_and_company_substitution(self):
        item = fixtures.item(stock=False).name
        order = self.order(item)
        name = order["document"]["name"]
        frappe.set_user("other.erp-basic@example.invalid")
        with self.assertRaises(frappe.PermissionError):
            api.get_document("orders", name, self.company)
        with self.assertRaises(frappe.PermissionError):
            frappe.get_doc("Sales Order", name).check_permission("read")
        self.assertFalse(frappe.get_list("Sales Order", filters={"name": name}))
        frappe.set_user("unscoped.erp-basic@example.invalid")
        self.assertFalse(frappe.get_list("Sales Order"))
        frappe.set_user("observer.erp-basic@example.invalid")
        self.assertEqual(api.get_document("orders", name, self.company)["document"]["name"], name)
        with self.assertRaises(frappe.PermissionError):
            self.submit(order)
        frappe.set_user("Administrator")
        service = fixtures.make_user("restricted.exchange@example.invalid",
                                     ["ERP Basic Integration", "Sales User"], [self.company])
        frappe.set_user(service)
        self.assertFalse(commands.capabilities("Sales Order")["create"])
        self.assertFalse(commands.capabilities("Sales Order")["submit"])
        with self.assertRaises(frappe.PermissionError):
            self.submit(order)
        frappe.set_user("Administrator")
        with self.assertRaises(frappe.PermissionError):
            commands.save_document("orders", self.company,
                                   {"company": self.env["other_company"], "customer": self.env["customer"]}, idempotency_key=key())

    def test_server_rejects_totals_and_other_company_links(self):
        item = fixtures.item().name
        with self.assertRaises(frappe.ValidationError):
            self.save("orders", {"grand_total": 1})
        with self.assertRaises(frappe.PermissionError):
            self.save("orders", {"customer": self.env["customer"], "transaction_date": nowdate(), "delivery_date": nowdate(),
                                  "items": [{"item_code": item, "qty": 1, "rate": 1,
                                             "warehouse": self.env["other_warehouse"]}]})

    def test_audit_before_after_and_immutability(self):
        item = fixtures.item(stock=False).name
        order = self.order(item)
        name = order["document"]["name"]
        updated = commands.save_document("orders", self.company, {"name": name, "remarks": "reviewed"},
                                         order["document"]["modified"], key())
        self.assertEqual(frappe.db.get_value("Sales Order", name, "terms"), "reviewed")
        self.assertEqual(updated["document"]["remarks"], "reviewed")
        self.submit(updated)
        events = frappe.get_list("ERP Basic Audit Event", filters={"reference_name": name}, fields=["name", "action", "before_snapshot", "after_snapshot"])
        self.assertEqual({entry.action for entry in events}, {"after_insert", "on_update", "on_submit"})
        submitted = next(entry for entry in events if entry.action == "on_submit")
        self.assertEqual(frappe.parse_json(submitted.before_snapshot)["docstatus"], 0)
        self.assertEqual(frappe.parse_json(submitted.after_snapshot)["docstatus"], 1)
        event = frappe.get_doc("ERP Basic Audit Event", submitted.name)
        event.action = "forged"
        with self.assertRaises(frappe.PermissionError):
            event.save()
        with self.assertRaises(frappe.PermissionError):
            event.delete()

    def test_failure_after_native_posting_rolls_back_every_effect(self):
        item = fixtures.item().name
        draft = self.save("stockDocs", {"stock_entry_type": "Material Receipt", "posting_date": nowdate(),
                                        "items": [{"item_code": item, "qty": 5, "basic_rate": 100,
                                                   "t_warehouse": self.warehouse}]})
        frappe.db.savepoint("before_fault")
        original = audit.record_event

        def fail_after_posting(doc, event):
            if doc.name == draft["document"]["name"] and event == "on_submit":
                self.assertTrue(frappe.db.exists("Stock Ledger Entry", {"voucher_no": doc.name}))
                raise RuntimeError("Controlled failure after native stock posting")
            return original(doc, event)

        with patch("erp_basic.audit.record_event", side_effect=fail_after_posting):
            with self.assertRaises(RuntimeError):
                self.submit(draft)
        frappe.db.rollback(save_point="before_fault")
        self.assertEqual(frappe.db.get_value("Stock Entry", draft["document"]["name"], "docstatus"), 0)
        self.assertFalse(frappe.db.exists("Stock Ledger Entry", {"voucher_no": draft["document"]["name"]}))
        self.assertFalse(frappe.db.exists("GL Entry", {"voucher_no": draft["document"]["name"]}))

    def test_transfer_reconciliation_writeoff_and_cancel(self):
        item = fixtures.item("TRANSFER").name
        self.receipt(item, 10, 100)
        warehouse = frappe.get_doc({"doctype": "Warehouse", "warehouse_name": "Transfer " + key(),
                                    "company": self.company,
                                    "account": frappe.db.get_value("Warehouse", self.warehouse, "account")}).insert().name
        transfer = self.submit(self.save("stockDocs", {"stock_entry_type": "Material Transfer", "posting_date": nowdate(),
                              "items": [{"item_code": item, "qty": 3, "s_warehouse": self.warehouse, "t_warehouse": warehouse}]}))
        self.assertAlmostEqual(self.stock(item).actual_qty, 7)
        self.assertAlmostEqual(frappe.db.get_value("Bin", {"item_code": item, "warehouse": warehouse}, "stock_value"), 300)
        self.submit(self.save("stockDocs", {"stock_entry_type": "Material Issue", "posting_date": nowdate(),
                                           "items": [{"item_code": item, "qty": 1, "s_warehouse": self.warehouse}]}))
        inventory = self.submit(self.save("inventory", {"posting_date": nowdate(), "purpose": "Stock Reconciliation",
                            "items": [{"item_code": item, "warehouse": self.warehouse, "qty": 8, "valuation_rate": 125}]}))
        self.assertAlmostEqual(self.stock(item).actual_qty, 8)
        self.assertAlmostEqual(self.stock(item).stock_value, 1000)
        cancelled = commands.transition("inventory", inventory["document"]["name"], self.company,
                                        inventory["document"]["modified"], key(), "cancel")
        self.assertEqual(cancelled["document"]["docstatus"], 2)
        self.assertAlmostEqual(self.stock(item).actual_qty, 6)
        self.assertAlmostEqual(self.stock(item).stock_value, 600)
        self.assertEqual(frappe.db.count("Stock Ledger Entry", {"voucher_no": transfer["document"]["name"], "is_cancelled": 0}), 2)

    def test_partial_purchase_source_links_and_supplier_return(self):
        item = fixtures.item("PURCHASE").name
        order = self.submit(self.save("purchaseOrders", {"supplier": self.env["supplier"], "transaction_date": nowdate(),
                    "schedule_date": nowdate(), "buying_price_list": "ERP Basic Buying",
                    "items": [{"item_code": item, "qty": 10, "rate": 100, "warehouse": self.warehouse}]}))
        receipt = commands.create_related("purchaseOrders", order["document"]["name"], "receipts", self.company,
                                           order["document"]["modified"], key())
        row = receipt["document"]["items"][0]
        receipt = commands.save_document("receipts", self.company,
                    {"name": receipt["document"]["name"], "items": [{"name": row["name"], "item_code": item,
                     "qty": 4, "rate": 100, "warehouse": self.warehouse}]}, receipt["document"]["modified"], key())
        native = frappe.get_doc("Purchase Receipt", receipt["document"]["name"])
        self.assertEqual(native.items[0].purchase_order, order["document"]["name"], "Редактирование не стирает исходную связь")
        receipt = self.submit(receipt)
        self.assertAlmostEqual(frappe.db.get_value("Purchase Order", order["document"]["name"], "per_received"), 40)
        invoice = commands.create_related("receipts", receipt["document"]["name"], "purchaseInvoices", self.company,
                                           receipt["document"]["modified"], key())
        invoice = self.submit(invoice)
        self.assertAlmostEqual(self.stock(item).actual_qty, 4)
        self.assertEqual(Decimal(invoice["document"]["grand_total"]), Decimal(400))
        returned = commands.create_return("receipts", receipt["document"]["name"], self.company,
                        frappe.db.get_value("Purchase Receipt", receipt["document"]["name"], "modified"), key())
        self.submit(returned)
        self.assertAlmostEqual(self.stock(item).actual_qty, 0)
        self.assertAlmostEqual(self.stock(item).stock_value, 0)

    def test_unit_conversion_and_duplicate_line_shortage(self):
        item = fixtures.item("UOM")
        unit = "ERP Basic Pack"
        if not frappe.db.exists("UOM", unit):
            frappe.get_doc({"doctype": "UOM", "uom_name": unit, "must_be_whole_number": 1}).insert()
        item.append("uoms", {"uom": unit, "conversion_factor": 5})
        item.save()
        self.submit(self.save("receipts", {"supplier": self.env["supplier"], "posting_date": nowdate(),
                         "buying_price_list": "ERP Basic Buying", "items": [{"item_code": item.name, "qty": 2,
                         "rate": 500, "uom": unit, "conversion_factor": 5, "warehouse": self.warehouse}]}))
        self.assertAlmostEqual(self.stock(item.name).actual_qty, 10)
        self.assertAlmostEqual(self.stock(item.name).stock_value, 1000)
        draft = self.save("stockDocs", {"stock_entry_type": "Material Issue", "posting_date": nowdate(),
                             "items": [{"item_code": item.name, "qty": 6, "s_warehouse": self.warehouse},
                                       {"item_code": item.name, "qty": 6, "s_warehouse": self.warehouse}]})
        frappe.db.savepoint("before_shortage")
        with self.assertRaises(frappe.ValidationError):
            self.submit(draft)
        frappe.db.rollback(save_point="before_shortage")
        self.assertAlmostEqual(self.stock(item.name).actual_qty, 10)
        self.assertEqual(frappe.db.get_value("Stock Entry", draft["document"]["name"], "docstatus"), 0)


def run():
    if not frappe.conf.get("allow_tests"):
        frappe.throw("Тесты разрешены только в отдельном тестовом Site.")
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestCore)
    report = unittest.TextTestRunner(verbosity=2).run(suite)
    if not report.wasSuccessful():
        raise RuntimeError(f"Серверные тесты: failures={len(report.failures)}, errors={len(report.errors)}")
    return {"tests": report.testsRun, "failures": 0, "errors": 0, "database": frappe.db.sql("SELECT VERSION()")[0][0]}
