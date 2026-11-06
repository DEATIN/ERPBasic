"""Администрирование сотрудников одного Site штатными User и User Permission."""

import frappe

from erp_basic import access, idempotency


ROLE_SETS = {
    "owner": ["System Manager", "Sales Manager", "Purchase Manager", "Stock Manager", "Accounts Manager"],
    "administrator": ["System Manager"],
    "sales": ["Sales User"],
    "purchasing": ["Purchase User"],
    "warehouse": ["Stock User"],
    "finance": ["Accounts User"],
    "observer": ["ERP Basic Observer"],
    "integration": ["ERP Basic Integration", "Sales User"],
}


@frappe.whitelist(methods=["POST"])
def invite(email, full_name, role, companies, idempotency_key):
    access.require_administrator()
    companies = frappe.parse_json(companies) if isinstance(companies, str) else companies
    if role not in ROLE_SETS or not isinstance(companies, list) or not companies:
        frappe.throw("Укажите роль и хотя бы одну организацию.")
    companies = sorted(set(companies))
    for company in companies:
        access.require_company(company)
    email = frappe.utils.validate_email_address(email, throw=True).lower()

    def execute():
        if frappe.db.exists("User", email):
            frappe.throw("Пользователь уже существует. Измените доступ в управлении пользователями.")
        user = frappe.get_doc({"doctype": "User", "email": email, "first_name": full_name,
                               "enabled": 1, "user_type": "System User", "send_welcome_email": 1,
                               "roles": [{"role": name} for name in ["ERP Basic User", *ROLE_SETS[role]]]})
        user.insert()
        for company in companies:
            frappe.get_doc({"doctype": "User Permission", "user": email, "allow": "Company",
                            "for_value": company, "apply_to_all_doctypes": 1}).insert()
        return {"user": email, "role": role, "companies": companies, "invitation": "queued"}

    return idempotency.run(companies[0], "invite", idempotency_key,
                           {"email": email, "full_name": full_name, "role": role, "companies": companies}, execute)


@frappe.whitelist(methods=["POST"])
def disable(user):
    access.require_administrator()
    if user in ("Administrator", "Guest", frappe.session.user):
        frappe.throw("Эту учётную запись здесь блокировать нельзя.")
    employee = frappe.get_doc("User", user)
    employee.enabled = 0
    employee.save()
    frappe.sessions.clear_sessions(user=user, force=True)
    return {"user": user, "enabled": False}


@frappe.whitelist(methods=["POST"])
def end_sessions(user=None):
    access.require_user()
    user = user or frappe.session.user
    if user != frappe.session.user:
        access.require_administrator()
    frappe.sessions.clear_sessions(user=user, force=True)
    return {"user": user, "sessions_ended": True}
