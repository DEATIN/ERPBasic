"""Установка не создаёт пользователей с общим паролем и не загружает демонстрационные проводки."""

import frappe


def after_install():
    for role in ("ERP Basic User", "ERP Basic Audit Reader", "ERP Basic Integration", "ERP Basic Observer"):
        if not frappe.db.exists("Role", role):
            frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert()
    frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 0)
    frappe.db.set_single_value("Stock Settings", "valuation_method", "Moving Average")
    frappe.db.set_single_value("System Settings", "allow_login_using_mobile_number", 0)
    frappe.db.set_single_value("System Settings", "allow_login_using_user_name", 0)
    frappe.db.set_single_value("System Settings", "allow_consecutive_login_attempts", 5)
    frappe.db.set_single_value("System Settings", "allow_login_after_fail", 300)
    frappe.db.set_single_value("System Settings", "time_zone", "Europe/Moscow")
    frappe.db.set_single_value("System Settings", "currency_precision", 2)
    frappe.db.set_single_value("System Settings", "float_precision", 6)
    from frappe.permissions import add_permission
    from erp_basic.schema import KINDS
    for doctype in {spec["doctype"] for spec in KINDS.values()} | {"Company"}:
        if not frappe.db.exists("Custom DocPerm", {"parent": doctype, "role": "ERP Basic Observer"}):
            add_permission(doctype, "ERP Basic Observer", ptype="read")
    after_migrate()


def after_migrate():
    if frappe.db.table_exists("ERP Basic Operation"):
        frappe.db.add_index("ERP Basic Operation", ["company", "creation"], "erp_basic_operation_company")
    if frappe.db.table_exists("ERP Basic Audit Event"):
        frappe.db.add_index("ERP Basic Audit Event", ["company", "creation"], "erp_basic_audit_company")
