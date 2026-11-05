"""Перенос настоящего demo v2 в нативные документы и сверка без повторных движений."""

from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import frappe
from frappe.utils import nowdate

from erp_basic import migration
from erp_basic.tests import fixtures


def run():
    env = fixtures.setup()
    frappe.set_user("Administrator")
    source = frappe.parse_json(Path(frappe.get_app_path("erp_basic", "tests", "fixtures", "demo-v2.json")).read_text())
    source_id = "demo-test-" + frappe.generate_hash(length=12)
    financial = {"reviewed": True, "currency": "RUB", "entries": [
        {"kind": "receivable", "party_id": "C-001", "amount": "1000.00"},
        {"kind": "customer_advance", "party_id": "C-002", "amount": "200.00"},
        {"kind": "payable", "party_id": "S-001", "amount": "300.00"},
        {"kind": "supplier_advance", "party_id": "S-002", "amount": "50.00"},
        {"kind": "cash", "amount": "100.00"},
    ]}
    args = {"company": env["company"], "warehouse": env["warehouse"], "source_id": source_id,
            "cutoff_date": nowdate(), "financial": financial}
    before = frappe.db.count("ERP Basic Migration Run")
    report = migration.plan(source, **args)
    assert report["valid"], report["errors"]
    assert frappe.db.count("ERP Basic Migration Run") == before, "Dry run не должен писать данные"
    assert Decimal(report["expected"]["stock_qty"]) == Decimal(99)
    assert Decimal(report["expected"]["stock_value"]) == Decimal(62340)
    invalid = deepcopy(source)
    invalid["items"].append(deepcopy(invalid["items"][0]))
    assert not migration.plan(invalid, **args)["valid"]
    invalid = deepcopy(source)
    invalid["orders"][0]["lines"][0]["item"] = "UNKNOWN"
    assert not migration.plan(invalid, **args)["valid"]
    result = migration.apply(source, **args, plan_hash=report["run_id"], accept_warnings=1)
    counts = {doctype: frappe.db.count(doctype) for doctype in
              ("ERP Basic Migration Mapping", "Stock Ledger Entry", "GL Entry", "Item", "Customer", "Sales Order")}
    repeated = migration.apply(source, **args, plan_hash=report["run_id"], accept_warnings=1)
    assert result == repeated
    assert counts == {doctype: frappe.db.count(doctype) for doctype in counts}, "Повтор не создаёт записей"
    assert result["reconciliation"]["differences"] == []
    assert Decimal(result["reconciliation"]["actual_stock_value"]) == Decimal("62340")
    assert len(result["mappings"]) == sum(len(source[section]) for section in migration.SECTIONS)
    payment_map = next(row for row in result["mappings"] if row["kind"] == "payments")
    assert payment_map["doctype"] is None, "Демонстрационная оплата сохраняется как архив, не банковский факт"
    archived = migration.archive(env["company"], payment_map["archive"])["source"]
    assert archived == next(row for row in source["payments"] if row["id"] == payment_map["source"])
    journal = frappe.get_doc("Journal Entry", result["opening_journal"])
    assert journal.docstatus == 1 and journal.is_opening == "Yes"
    customer_map = next(row for row in result["mappings"] if row["kind"] == "customers" and row["source"] == "C-001")
    balance = frappe.db.sql("""SELECT SUM(debit-credit) FROM `tabGL Entry`
        WHERE voucher_no=%s AND party=%s AND is_cancelled=0""", (journal.name, customer_map["name"]))[0][0]
    assert Decimal(str(balance)) == Decimal("1000")
    # Полный ERPNext открывает те же объекты; включение интерфейса не переносит регистры.
    stock_id = result["stock_document"]
    baseline = frappe.db.sql("""SELECT name, item_code, warehouse, actual_qty, stock_value_difference
                                FROM `tabStock Ledger Entry` WHERE voucher_no=%s ORDER BY name""", (stock_id,))
    original_full = frappe.conf.get("erp_basic_full_erpnext")
    frappe.conf.erp_basic_full_erpnext = 1
    native = frappe.get_doc("Stock Reconciliation", stock_id)
    native.check_permission("read")
    assert native.name == stock_id and native.docstatus == 1
    assert baseline == frappe.db.sql("""SELECT name, item_code, warehouse, actual_qty, stock_value_difference
                                       FROM `tabStock Ledger Entry` WHERE voucher_no=%s ORDER BY name""", (stock_id,))
    frappe.conf.erp_basic_full_erpnext = original_full
    frappe.db.commit()
    return {"scenarios": 5, "run_id": report["run_id"], "archived_records": len(result["mappings"]),
            "stock_qty": result["reconciliation"]["actual_stock_qty"],
            "stock_value": result["reconciliation"]["actual_stock_value"], "repeat": "no_duplicates",
            "reviewed_receivable": "1000.00", "full_erpnext_same_ids_and_ledger": True}
