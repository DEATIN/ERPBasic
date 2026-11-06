"""Закрытый HTTP-контракт Basic не открывает произвольные RPC полного ERPNext."""

from urllib.parse import unquote

import frappe

from erp_basic import access
from erp_basic.schema import KINDS


PUBLIC_METHODS = {
    "login", "logout", "frappe.auth.get_logged_user", "erp_basic.api.session",
    "frappe.core.doctype.user.user.reset_password", "frappe.core.doctype.user.user.update_password",
}


def check_request():
    # auth_hooks выполняются после аутентификации API token и cookie в Frappe 15.
    request = getattr(frappe.local, "request", None)
    if not request or access.is_administrator():
        return
    integration_user = "ERP Basic Integration" in frappe.get_roles()
    if frappe.conf.get("erp_basic_full_erpnext") and not integration_user:
        # Включается оператором Site при переходе к полному ERPNext после проверки его ролей.
        return
    path = unquote(request.path)
    method = frappe.form_dict.get("cmd")
    if not method and path.startswith("/api/method/"):
        method = path[len("/api/method/"):]
    if method:
        if method in PUBLIC_METHODS:
            return
        access.require_user()
        if method.startswith("erp_basic."):
            if integration_user and request.method != "GET" and not method.startswith("erp_basic.integration."):
                frappe.throw("Служебный пользователь изменяет данные только через контракт обмена.", frappe.PermissionError)
            return
        if method == "upload_file":
            if not access.allowed_companies():
                frappe.throw("Нет доступных организаций.", frappe.PermissionError)
            return
        frappe.throw("Этот метод недоступен в серверном режиме Basic.", frappe.PermissionError)
    if path.startswith("/api/resource/"):
        access.require_user()
        resource = path[len("/api/resource/"):].split("/")
        doctype = resource[0]
        # Произвольный document RPC и запись через resource не обходят ключи наших команд.
        if request.method != "GET" or "run_method" in frappe.form_dict:
            frappe.throw("Изменяйте учётные документы через команды ERP Basic.", frappe.PermissionError)
        if len(resource) == 1:
            for key in ("limit", "limit_page_length"):
                if key in frappe.form_dict and not 1 <= frappe.utils.cint(frappe.form_dict[key]) <= 100:
                    frappe.throw("Размер страницы должен быть от 1 до 100.")
        allowed = {spec["doctype"] for spec in KINDS.values()} | {"Company", "GL Entry", "Stock Ledger Entry"}
        if doctype not in allowed or not access.allowed_companies():
            frappe.throw("Нет доступа к этому ресурсу.", frappe.PermissionError)
        return
    if path.startswith("/api/"):
        frappe.throw("Неизвестный маршрут API.", frappe.PermissionError)


def protect_attachment(doc, event=None):
    if frappe.flags.in_install or frappe.flags.in_migrate or not doc.attached_to_doctype:
        return
    allowed = {spec["doctype"] for spec in KINDS.values()}
    if doc.attached_to_doctype not in allowed:
        if not access.is_administrator():
            frappe.throw("Вложение должно быть связано с учётным документом.", frappe.PermissionError)
        return
    parent = frappe.get_doc(doc.attached_to_doctype, doc.attached_to_name)
    parent.check_permission("write")
    if parent.get("company"):
        access.require_company(parent.company)
    if not doc.is_private:
        frappe.throw("Вложения учётных документов должны быть приватными.", frappe.PermissionError)


def file_permission(doc, ptype=None, user=None, **kwargs):
    if doc.attached_to_doctype and doc.attached_to_name:
        selected = {spec["doctype"] for spec in KINDS.values()}
        if doc.attached_to_doctype in selected:
            parent = frappe.get_doc(doc.attached_to_doctype, doc.attached_to_name)
            if not frappe.has_permission(parent.doctype, "read", doc=parent, user=user):
                return False
    return None
