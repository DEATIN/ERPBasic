"""Права действуют также при обращении к штатному REST API и отчётам Frappe."""

import frappe


def require_user():
    user = frappe.session.user
    if user == "Guest":
        frappe.throw("Войдите в систему.", frappe.AuthenticationError)
    return user


def is_administrator(user=None):
    user = user or frappe.session.user
    return user == "Administrator" or "System Manager" in frappe.get_roles(user)


def allowed_companies(user=None):
    user = user or frappe.session.user
    if user == "Guest":
        return []
    # Проверка системных прав не вызывает permission hook Company повторно.
    if is_administrator(user):
        return frappe.db.sql("SELECT name FROM `tabCompany` ORDER BY name", pluck=True)
    # Отсутствие Company User Permission означает запрет, а не доступ ко всем компаниям.
    return frappe.db.sql(
        """SELECT DISTINCT for_value FROM `tabUser Permission`
           WHERE user=%s AND allow='Company' AND apply_to_all_doctypes=1 ORDER BY for_value""",
        (user,), pluck=True,
    )


def require_company(company, user=None):
    require_user()
    if not isinstance(company, str) or company not in allowed_companies(user):
        frappe.throw("Нет доступа к выбранной организации.", frappe.PermissionError)
    return company


def require_administrator():
    require_user()
    if not is_administrator():
        frappe.throw("Операция доступна администратору этого контура.", frappe.PermissionError)


def document_permission(doc, user=None, permission_type=None, **kwargs):
    user = user or frappe.session.user
    if is_administrator(user):
        return None
    company = doc.name if doc.doctype == "Company" else doc.get("company")
    if doc.doctype == "ERP Basic Audit Event" and not company:
        return None if allowed_companies(user) else False
    if not company or company not in allowed_companies(user):
        return False
    # None сохраняет штатную проверку роли и типа действия; разрешения не расширяются.
    return None


def shared_permission(doc, user=None, permission_type=None, **kwargs):
    return None if allowed_companies(user) else False


def shared_query(user=None):
    return "" if allowed_companies(user) else "1=0"


def _company_query(doctype, user=None, shared=False):
    user = user or frappe.session.user
    if is_administrator(user):
        return ""
    companies = allowed_companies(user)
    if not companies:
        return "1=0"
    # Имена таблиц задаются только константами приложения; значения экранирует драйвер.
    field = "name" if doctype == "Company" else "company"
    column = f"`tab{doctype}`.`{field}`"
    values = ", ".join(frappe.db.escape(value) for value in companies)
    condition = f"{column} IN ({values})"
    if shared:
        condition = f"({condition} OR {column} IS NULL OR {column}='')"
    return condition


def company_query(user=None): return _company_query("Company", user)
def warehouse_query(user=None): return _company_query("Warehouse", user)
def quotation_query(user=None): return _company_query("Quotation", user)
def sales_order_query(user=None): return _company_query("Sales Order", user)
def delivery_note_query(user=None): return _company_query("Delivery Note", user)
def sales_invoice_query(user=None): return _company_query("Sales Invoice", user)
def purchase_order_query(user=None): return _company_query("Purchase Order", user)
def purchase_receipt_query(user=None): return _company_query("Purchase Receipt", user)
def purchase_invoice_query(user=None): return _company_query("Purchase Invoice", user)
def payment_entry_query(user=None): return _company_query("Payment Entry", user)
def journal_entry_query(user=None): return _company_query("Journal Entry", user)
def stock_entry_query(user=None): return _company_query("Stock Entry", user)
def stock_reconciliation_query(user=None): return _company_query("Stock Reconciliation", user)
def gl_entry_query(user=None): return _company_query("GL Entry", user)
def stock_ledger_entry_query(user=None): return _company_query("Stock Ledger Entry", user)
def audit_event_query(user=None): return _company_query("ERP Basic Audit Event", user, shared=True)
