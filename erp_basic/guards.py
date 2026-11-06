"""Инварианты учёта на уровне DocType, включая стандартные входы Frappe."""

import frappe
from erp_basic.access import allowed_companies, is_administrator, require_company

STOCK_DOCUMENTS = {"Delivery Note", "Purchase Receipt", "Stock Entry", "Stock Reconciliation"}
SCOPED_LINKS = {
    "Warehouse", "Account", "Cost Center", "Project", "Sales Order", "Purchase Order", "Delivery Note",
    "Purchase Receipt", "Sales Invoice", "Purchase Invoice", "Payment Entry", "Stock Entry",
    "Sales Taxes and Charges Template", "Purchase Taxes and Charges Template",
}


def validate_document(doc, event=None):
    if frappe.flags.in_install or frappe.flags.in_migrate:
        return
    if doc.meta.has_field("company"):
        require_company(doc.get("company"))
    elif not allowed_companies():
        frappe.throw("Пользователю не назначена организация.", frappe.PermissionError)
    if doc.doctype == "Item" and not frappe.conf.get("erp_basic_full_erpnext"):
        if doc.get("allow_negative_stock"):
            frappe.throw("В Basic отрицательные остатки запрещены и для отдельных товаров.")
        if doc.get("is_stock_item"):
            if doc.get("valuation_method") not in (None, "", "Moving Average"):
                frappe.throw("В Basic используется скользящая средняя стоимость.")
            doc.valuation_method = "Moving Average"
    validate_company_links(doc)
    if doc.doctype == "Sales Invoice" and doc.get("update_stock"):
        if any(row.get("delivery_note") for row in doc.get("items", [])):
            frappe.throw("Отгрузка уже отражена накладной. Повторное списание через счёт запрещено.")
    if doc.doctype == "Purchase Invoice" and doc.get("update_stock"):
        if any(row.get("purchase_receipt") for row in doc.get("items", [])):
            frappe.throw("Приёмка уже отражена поступлением. Повторное оприходование запрещено.")


def validate_company_links(doc):
    company = doc.get("company")
    if not company:
        return
    checked = {}
    for row in [doc, *doc.get_all_children()]:
        for df in row.meta.get_link_fields():
            value = row.get(df.fieldname)
            if not value or df.options not in SCOPED_LINKS:
                continue
            key = (df.options, value)
            if key not in checked:
                checked[key] = frappe.db.get_value(df.options, value, "company")
            if checked[key] and checked[key] != company:
                frappe.throw("Связанный объект принадлежит другой организации.", frappe.PermissionError)
    if doc.doctype == "Payment Entry":
        for ref in doc.get("references") or []:
            if ref.reference_doctype not in {"Sales Order", "Purchase Order", "Sales Invoice", "Purchase Invoice", "Journal Entry"}:
                frappe.throw("Недопустимое основание оплаты.")
            source = frappe.get_doc(ref.reference_doctype, ref.reference_name)
            source.check_permission("read")
            if source.get("company") != company:
                frappe.throw("Оплата связана с документом другой организации.", frappe.PermissionError)


def lock_stock_items(doc):
    moves_stock = doc.doctype in STOCK_DOCUMENTS or (
        doc.doctype in {"Sales Invoice", "Purchase Invoice"} and doc.get("update_stock")
    )
    if not moves_stock:
        return
    # В одной транзакции можно сначала создать склад, затем провести документ (например, импорт).
    # Native cache карты счетов не должен сохранять перечень складов от предыдущей команды.
    account_maps = frappe.flags.get("warehouse_account_map")
    if account_maps and doc.get("company"):
        account_maps.pop(doc.company, None)
    items = sorted({row.item_code for row in doc.get("items", []) if row.get("item_code")})
    if items:
        placeholders = ",".join(["%s"] * len(items))
        # Консервативная блокировка Item защищает также первое движение до создания Bin.
        # Все документы захватывают блокировки в одном порядке; движения создаёт ERPNext.
        frappe.db.sql(
            f"SELECT name FROM `tabItem` WHERE name IN ({placeholders}) ORDER BY name FOR UPDATE", items
        )


def before_submit(doc, event=None):
    require_isolation()
    validate_document(doc)
    lock_stock_items(doc)


def before_cancel(doc, event=None):
    require_isolation()
    validate_document(doc)
    lock_stock_items(doc)


def require_isolation():
    if frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.get("erp_basic_isolation_checked"):
        return
    if frappe.db.db_type != "mariadb" or frappe.db.sql("SELECT @@tx_isolation")[0][0] != "READ-COMMITTED":
        frappe.throw("Для проведения нужна MariaDB с READ-COMMITTED. Проверьте настройки окружения.")
    frappe.flags.erp_basic_isolation_checked = True
