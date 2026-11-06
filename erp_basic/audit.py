"""Минимальный неизменяемый бизнес-аудит, не содержащий секреты и полные карточки клиентов."""

import uuid
import frappe
from erp_basic.idempotency import canonical_json

FIELDS = (
    "name", "company", "docstatus", "status", "customer", "supplier", "party", "posting_date",
    "transaction_date", "grand_total", "outstanding_amount", "paid_amount", "received_amount", "purpose",
    "item_code", "item_name", "stock_uom", "is_stock_item", "disabled", "customer_name", "supplier_name",
    "warehouse_name", "parent_warehouse", "is_group", "account",
)
ROW_FIELDS = ("item_code", "qty", "rate", "amount", "warehouse", "s_warehouse", "t_warehouse",
              "valuation_rate", "basic_rate")


def snapshot(doc):
    result = {field: doc.get(field) for field in FIELDS if doc.get(field) is not None}
    if doc.get("items"):
        result["items"] = [{field: row.get(field) for field in ROW_FIELDS if row.get(field) is not None}
                           for row in doc.items]
    for table, fields in (
        ("references", ("reference_doctype", "reference_name", "allocated_amount")),
        ("accounts", ("account", "party_type", "party", "debit_in_account_currency", "credit_in_account_currency")),
    ):
        if doc.get(table):
            result[table] = [{field: row.get(field) for field in fields} for row in doc.get(table)]
    return result


def skip_event(doc, event):
    if frappe.flags.in_install or frappe.flags.in_migrate:
        return True
    if event == "on_update" and (doc.flags.in_insert or getattr(doc, "_action", None) in {"submit", "cancel"}):
        return True
    return False


def record_event(doc, event):
    if skip_event(doc, event):
        return
    before = doc.get_doc_before_save()
    entry = frappe.get_doc({
        "doctype": "ERP Basic Audit Event",
        "company": doc.get("company"),
        "actor": frappe.session.user,
        "reference_doctype": doc.doctype,
        "reference_name": doc.name,
        "action": event,
        "request_id": getattr(frappe.local, "erp_basic_request_id", None) or str(uuid.uuid4()),
        "before_snapshot": canonical_json(snapshot(before)) if before else "{}",
        "after_snapshot": canonical_json(snapshot(doc)),
    })
    entry.flags.erp_basic_internal_audit = True
    entry.insert(ignore_permissions=True)
    # Это единственный внутренний путь создания аудита; пользователь не получает право изменения журнала.
