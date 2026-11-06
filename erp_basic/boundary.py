"""Закрытый HTTP-контракт Basic не открывает произвольные RPC полного ERPNext."""

from urllib.parse import unquote

import frappe

from erp_basic import access
from erp_basic.schema import KINDS


PUBLIC_METHODS = {
    "login", "logout", "frappe.auth.get_logged_user", "erp_basic.api.session",
    "frappe.core.doctype.user.user.reset_password", "frappe.core.doctype.user.user.update_password",
}


def _company_field(doctype):
    if doctype == "Company":
        return "name"
    return "company" if frappe.get_meta(doctype).has_field("company") else None


def _check_resource_scope(doctype, name=None):
    field = _company_field(doctype)
    if not field:
        return  # Общие справочники принадлежат Site; native права остаются обязательными.
    if name is not None:
        access.require_company(frappe.db.get_value(doctype, name, field))
        return
    from frappe.types.filter import Filters

    # Native DocShare объединяется с permission hooks через OR. Обязательный фильтр
    # запроса применяется через AND вне этого выражения и не расширяется share.
    try:
        filters = Filters(frappe.parse_json(frappe.form_dict.get("filters") or []), doctype=doctype)
    except (TypeError, ValueError):
        frappe.throw("Некорректный фильтр списка.")
    filters.append((doctype, field, "in", access.allowed_companies()))
    frappe.form_dict.filters = filters


def _readable_private_file(path, name=None):
    access.require_user()
    companies = access.allowed_companies()
    if not companies:
        frappe.throw("Нет доступа к этому вложению.", frappe.PermissionError)
    filters = {"file_url": path, "is_private": 1}
    if name:
        filters["name"] = str(name)
    # Здесь читаются только связи для проверки доступа, содержимое отдаёт native Frappe.
    files = frappe.db.get_values("File", filters,
                                ["name", "attached_to_doctype", "attached_to_name"], as_dict=True)
    for row in files:
        if row.attached_to_doctype and row.attached_to_name:
            field = _company_field(row.attached_to_doctype)
            if field and frappe.db.get_value(row.attached_to_doctype, row.attached_to_name, field) not in companies:
                continue
        file = frappe.get_doc("File", row.name)
        if file.is_downloadable():
            return row.name
    frappe.throw("Нет доступа к этому вложению.", frappe.PermissionError)


def _check_upload_source(integration_user):
    if integration_user or frappe.form_dict.get("method"):
        frappe.throw("Загрузка файла не предоставляет доступ к другим командам.", frappe.PermissionError)
    source_name = frappe.form_dict.get("library_file_name")
    source_url = frappe.form_dict.get("file_url")
    if source_name:
        if not isinstance(source_name, str):
            frappe.throw("Некорректное вложение.", frappe.PermissionError)
        source_url = frappe.db.get_value("File", source_name, "file_url")
        if not source_url:
            frappe.throw("Нет доступа к этому вложению.", frappe.PermissionError)
    if source_url:
        if not isinstance(source_url, str):
            frappe.throw("Некорректный адрес вложения.", frappe.PermissionError)
        # File.before_insert декодирует и нормализует URL. Проверяем тот же адрес
        # до переиспользования blob; переданный URL затем обрабатывает native File.
        source = frappe.get_doc({"doctype": "File", "file_url": unquote(source_url)})
        source.normalize_file_url()
        if not source.file_url.startswith("/private/files/"):
            frappe.throw("Повторно прикрепляйте только приватные вложения этого контура.", frappe.PermissionError)
        _readable_private_file(source.file_url, source_name)


def check_request():
    # auth_hooks выполняются после аутентификации API token и cookie в Frappe 15.
    request = getattr(frappe.local, "request", None)
    if not request or access.is_administrator():
        return
    integration_user = "ERP Basic Integration" in frappe.get_roles()
    if frappe.conf.get("erp_basic_full_erpnext") and not integration_user:
        # Включается оператором Site при переходе к полному ERPNext после проверки его ролей.
        return
    # Werkzeug уже декодировал путь; native router использует именно эту строку.
    path = request.path
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
            _check_upload_source(integration_user)
            return
        frappe.throw("Этот метод недоступен в серверном режиме Basic.", frappe.PermissionError)
    if path.startswith("/private/files/"):
        # У одного URL бывает несколько File. Native download должен использовать
        # ту же доступную связь, а не другую запись с более широким DocShare.
        frappe.form_dict.fid = _readable_private_file(path, frappe.form_dict.get("fid"))
        return
    if path.startswith("/api/resource/"):
        access.require_user()
        # Произвольный document RPC и запись через resource не обходят ключи наших команд.
        if request.method != "GET" or "run_method" in frappe.form_dict:
            frappe.throw("Изменяйте учётные документы через команды ERP Basic.", frappe.PermissionError)
        from frappe.api import API_URL_MAP

        # Используем тот же разбор имени, percent encoding и завершающего /, что и Frappe.
        _, arguments = API_URL_MAP.bind_to_environ(request.environ).match()
        doctype = arguments["doctype"]
        if "name" not in arguments:
            for key in ("limit", "limit_page_length"):
                if key in frappe.form_dict and not 1 <= frappe.utils.cint(frappe.form_dict[key]) <= 100:
                    frappe.throw("Размер страницы должен быть от 1 до 100.")
        allowed = {spec["doctype"] for spec in KINDS.values()} | {"Company", "GL Entry", "Stock Ledger Entry"}
        if doctype not in allowed or not access.allowed_companies():
            frappe.throw("Нет доступа к этому ресурсу.", frappe.PermissionError)
        _check_resource_scope(doctype, arguments.get("name"))
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
