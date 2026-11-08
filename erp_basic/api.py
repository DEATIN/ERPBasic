"""Версионируемый HTTP-контракт ERP Basic. Успешные POST фиксирует Frappe."""

import csv
import io

import frappe
from frappe.utils import cint

from erp_basic import access, commands
from erp_basic.schema import KINDS, fields_for, get_spec


@frappe.whitelist(allow_guest=True, methods=["GET"])
def session():
    if frappe.session.user == "Guest":
        return {"authenticated": False}
    from frappe.sessions import get_csrf_token
    from frappe.utils import get_system_timezone, nowdate
    from erp_basic.marking.service import capabilities as marking_capabilities

    user = access.require_user()
    companies = [{"name": name, "default_currency": frappe.db.get_value("Company", name, "default_currency")}
                 for name in access.allowed_companies()]
    return {"authenticated": True, "user": user,
            "full_name": frappe.db.get_value("User", user, "full_name"), "csrf_token": get_csrf_token(),
            "companies": companies, "roles": frappe.get_roles(),
            "marking": marking_capabilities(),
            "business_date": nowdate(), "time_zone": get_system_timezone(),
            "capabilities": {kind: commands.capabilities(spec["doctype"]) for kind, spec in KINDS.items()}}


def page_bounds(start, page_length):
    start, page_length = cint(start), cint(page_length)
    if start < 0 or page_length < 1 or page_length > 100:
        frappe.throw("Размер страницы должен быть от 1 до 100, смещение — неотрицательным.")
    return start, page_length


@frappe.whitelist(methods=["GET"])
def list_documents(kind, company, start=0, page_length=25, search=""):
    access.require_company(company)
    selected = get_spec(kind)
    commands.permission(selected["doctype"], "read")
    start, page_length = page_bounds(start, page_length)
    meta = frappe.get_meta(selected["doctype"])
    filters = {"company": company} if meta.has_field("company") else {}
    if not isinstance(search, str) or len(search) > 150:
        frappe.throw("Поисковая строка слишком длинная.")
    # Frappe экранирует параметры. Поиск не даёт клиенту задать произвольный SQL/DocType.
    or_filters = {field: ["like", "%" + search + "%"] for field in ("name", *selected["search"])} if search else None
    rows = frappe.get_list(selected["doctype"], fields=fields_for(kind), filters=filters,
                           or_filters=or_filters, order_by="modified desc, name desc", start=start,
                           page_length=page_length + 1)
    has_more = len(rows) > page_length
    rows = rows[:page_length]
    # Денежные и количественные поля передаются десятичной строкой с точностью метаданных.
    for row in rows:
        shell = frappe.get_doc({"doctype": selected["doctype"], **row})
        for field in tuple(row):
            row[field] = commands._value(shell, field)
    return {"rows": rows, "start": start, "page_length": page_length, "has_more": has_more}


@frappe.whitelist(methods=["GET"])
def get_document(kind, name, company):
    return commands.result(kind, company, commands.checked_document(kind, name, company))


@frappe.whitelist(methods=["POST"])
def save_document(kind, company, document, expected_modified=None, idempotency_key=None):
    return commands.save_document(kind, company, document, expected_modified, idempotency_key)


@frappe.whitelist(methods=["POST"])
def submit_document(kind, name, company, expected_modified, idempotency_key):
    return commands.transition(kind, name, company, expected_modified, idempotency_key, "submit")


@frappe.whitelist(methods=["POST"])
def cancel_document(kind, name, company, expected_modified, idempotency_key):
    return commands.transition(kind, name, company, expected_modified, idempotency_key, "cancel")


@frappe.whitelist(methods=["POST"])
def create_related(kind, name, target_kind, company, expected_modified, idempotency_key):
    return commands.create_related(kind, name, target_kind, company, expected_modified, idempotency_key)


@frappe.whitelist(methods=["POST"])
def create_return(kind, name, company, expected_modified, idempotency_key):
    return commands.create_return(kind, name, company, expected_modified, idempotency_key)


@frappe.whitelist(methods=["GET"])
def payment_template(kind, name, company, bank_account=None):
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    if kind not in ("orders", "invoices", "purchaseOrders", "purchaseInvoices"):
        frappe.throw("Для этого основания оплата не предусмотрена.")
    source = commands.checked_document(kind, name, company)
    commands.permission("Payment Entry", "create")
    if source.docstatus != 1:
        frappe.throw("Сначала проведите документ-основание.")
    if bank_account:
        account = frappe.get_doc("Account", bank_account)
        account.check_permission("read")
        if account.company != company or account.is_group:
            frappe.throw("Нужен счёт выбранной организации.", frappe.PermissionError)
    doc = get_payment_entry(source.doctype, source.name, bank_account=bank_account)
    # Это несохранённый шаблон; номер банковского документа заполняет сотрудник.
    data = commands.serialize("payments", doc)
    data["name"] = None
    for row in data.get("references", []):
        row["name"] = None
    return {"document": data, "saved": False}


@frappe.whitelist(methods=["GET"])
def list_audit(company, reference_name=None, start=0, page_length=25):
    access.require_company(company)
    commands.permission("ERP Basic Audit Event", "read")
    start, page_length = page_bounds(start, page_length)
    filters = {"company": company}
    if reference_name:
        filters["reference_name"] = reference_name
    rows = frappe.get_list("ERP Basic Audit Event", filters=filters,
                           fields=["name", "creation", "company", "actor", "reference_doctype", "reference_name",
                                   "action", "outcome", "request_id", "before_snapshot", "after_snapshot"],
                           start=start, page_length=page_length + 1, order_by="creation desc, name desc")
    return {"rows": rows[:page_length], "start": start, "page_length": page_length,
            "has_more": len(rows) > page_length}


@frappe.whitelist(methods=["GET"])
def stock_balance(company, warehouse=None, start=0, page_length=25):
    access.require_company(company)
    commands.permission("Stock Ledger Entry", "read")
    start, page_length = page_bounds(start, page_length)
    warehouses = frappe.get_list("Warehouse", filters={"company": company, "is_group": 0}, pluck="name", limit_page_length=0)
    if warehouse:
        if warehouse not in warehouses:
            frappe.throw("Нет доступа к складу.", frappe.PermissionError)
        warehouses = [warehouse]
    if not warehouses:
        return {"rows": [], "has_more": False, "start": start, "page_length": page_length}
    # Bin — штатная проекция ERPNext. Компания отбирается через разрешённые склады.
    rows = frappe.get_list("Bin", filters={"warehouse": ["in", warehouses]},
                           fields=["item_code", "warehouse", "actual_qty", "reserved_qty", "reserved_stock",
                                   "valuation_rate", "stock_value"], order_by="item_code, warehouse",
                           start=start, page_length=page_length + 1)
    for row in rows:
        for field in ("actual_qty", "reserved_qty", "reserved_stock", "valuation_rate", "stock_value"):
            if row.get(field) is not None:
                row[field] = str(row[field])
    return {"rows": rows[:page_length], "has_more": len(rows) > page_length,
            "start": start, "page_length": page_length}


@frappe.whitelist(methods=["GET"])
def export_page(kind, company, start=0, page_length=100, search=""):
    selected = get_spec(kind)
    commands.permission(selected["doctype"], "export")
    page = list_documents(kind, company, start, page_length, search)
    buffer = io.StringIO(newline="")
    columns = fields_for(kind)
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for row in page["rows"]:
        values = []
        for field in columns:
            value = "" if row.get(field) is None else str(row[field])
            if value.startswith(("=", "+", "-", "@", "\t", "\r")):
                value = "'" + value
            values.append(value)
        writer.writerow(values)
    return {"csv": buffer.getvalue(), "has_more": page["has_more"],
            "next_start": page["start"] + page["page_length"]}


LOOKUPS = {
    "accounts": ("Account", ["name", "account_name", "account_type", "account_currency"], {"is_group": 0}),
    "uoms": ("UOM", ["name", "must_be_whole_number"], {"enabled": 1}),
    "itemGroups": ("Item Group", ["name"], {"is_group": 0}),
    "customerGroups": ("Customer Group", ["name"], {"is_group": 0}),
    "supplierGroups": ("Supplier Group", ["name"], {"is_group": 0}),
    "territories": ("Territory", ["name"], {"is_group": 0}),
    "priceLists": ("Price List", ["name", "currency", "buying", "selling"], {"enabled": 1}),
    "stockEntryTypes": ("Stock Entry Type", ["name", "purpose"],
                        {"purpose": ["in", ["Material Receipt", "Material Issue", "Material Transfer"]]}),
}


@frappe.whitelist(methods=["GET"])
def lookup(kind, company, search="", start=0, page_length=25):
    access.require_company(company)
    if kind not in LOOKUPS:
        frappe.throw("Неизвестный справочник подбора.")
    doctype, fields, base_filters = LOOKUPS[kind]
    commands.permission(doctype, "read")
    start, page_length = page_bounds(start, page_length)
    if not isinstance(search, str) or len(search) > 150:
        frappe.throw("Поисковая строка слишком длинная.")
    filters = dict(base_filters)
    if frappe.get_meta(doctype).has_field("company"):
        filters["company"] = company
    if search:
        filters["name"] = ["like", "%" + search + "%"]
    rows = frappe.get_list(doctype, fields=fields, filters=filters, order_by="name",
                           start=start, page_length=page_length + 1)
    return {"rows": rows[:page_length], "has_more": len(rows) > page_length,
            "start": start, "page_length": page_length}


@frappe.whitelist(methods=["GET"])
def ledger(company, ledger_type="stock", start=0, page_length=25, voucher_no=None):
    access.require_company(company)
    start, page_length = page_bounds(start, page_length)
    if ledger_type == "stock":
        doctype = "Stock Ledger Entry"
        fields = ["name", "posting_date", "posting_time", "item_code", "warehouse", "actual_qty",
                  "qty_after_transaction", "valuation_rate", "stock_value_difference", "voucher_type", "voucher_no"]
    elif ledger_type == "general":
        doctype = "GL Entry"
        fields = ["name", "posting_date", "account", "party_type", "party", "debit", "credit", "voucher_type", "voucher_no"]
    else:
        frappe.throw("Неизвестный регистр.")
    commands.permission(doctype, "read")
    filters = {"company": company, "is_cancelled": 0}
    if voucher_no:
        filters["voucher_no"] = voucher_no
    rows = frappe.get_list(doctype, fields=fields, filters=filters, start=start, page_length=page_length + 1,
                           order_by="posting_date desc, creation desc")
    for row in rows:
        shell = frappe.get_doc({"doctype": doctype, **row})
        for field in tuple(row):
            row[field] = commands._value(shell, field)
    return {"rows": rows[:page_length], "has_more": len(rows) > page_length,
            "start": start, "page_length": page_length}


@frappe.whitelist(methods=["GET"])
def health():
    access.require_user()
    frappe.db.sql("SELECT 1")
    return {"status": "ok", "contract_version": 1}
