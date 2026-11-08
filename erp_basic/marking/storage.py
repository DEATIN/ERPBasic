"""Закрытое хранилище маркировки. Все бизнес-записи проходят через проверенные команды."""

import hashlib
import json

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from erp_basic.idempotency import canonical_json

PREFIX = "ERP Basic Marking "
TABLES = {"Setup", "Product", "Job", "Code", "Scan", "Device", "History", "Expected", "Package", "Operation", "Import"}


def native_permission(doc, ptype=None, user=None, **kwargs):
    # В том числе в full ERPNext: DocShare не открывает исходные коды или служебный outbox.
    from erp_basic.access import is_administrator
    return None if is_administrator(user) else False


class ControlledDocument(Document):
    def validate(self):
        if not (self.flags.erp_basic_marking_internal or frappe.flags.in_install or frappe.flags.in_migrate):
            frappe.throw("Маркировка изменяется только через рабочие операции.", frappe.PermissionError)

    def on_trash(self):
        frappe.throw("История маркировки не удаляется. Используйте отмену операции.", frappe.PermissionError)

    def before_rename(self, *args, **kwargs):
        frappe.throw("Идентификатор маркировки неизменяем.", frappe.PermissionError)


def identity(*parts):
    return hashlib.sha256(canonical_json(parts).encode()).hexdigest()


def table(kind):
    if kind not in TABLES:
        raise ValueError("Unknown marking table")
    return "`tab" + PREFIX + kind + "`"


def insert(kind, values, name=None):
    document = frappe.get_doc({"doctype": PREFIX + kind, **values})
    document.flags.erp_basic_marking_internal = True
    document.insert(ignore_permissions=True, set_name=name)
    if kind == "Job":
        from erp_basic.marking.projection import queue_for_job
        queue_for_job(document.name)
    return document


def lock(kind, name):
    rows = frappe.db.sql(f"SELECT * FROM {table(kind)} WHERE name=%s FOR UPDATE", (name,), as_dict=True)
    return rows[0] if rows else None


def ensure(kind, name, values):
    """INSERT + FOR UPDATE: работает и при одновременном первом скане одного кода."""
    fields = {"name": name, "owner": frappe.session.user, "modified_by": frappe.session.user,
              "creation": now_datetime(), "modified": now_datetime(), "docstatus": 0, "idx": 0, **values}
    allowed = {field.fieldname for field in frappe.get_meta(PREFIX + kind).fields}
    allowed |= {"name", "owner", "modified_by", "creation", "modified", "docstatus", "idx"}
    if not set(fields) <= allowed:
        raise ValueError("Unknown marking field")
    columns = ",".join(f"`{field}`" for field in fields)
    placeholders = ",".join(["%s"] * len(fields))
    frappe.db.sql(f"INSERT INTO {table(kind)} ({columns}) VALUES ({placeholders}) "
                  "ON DUPLICATE KEY UPDATE name=name", tuple(fields.values()))
    return lock(kind, name)


def update(kind, name, values):
    allowed = {field.fieldname for field in frappe.get_meta(PREFIX + kind).fields}
    if not set(values) <= allowed:
        raise ValueError("Unknown marking field")
    values = {**values, "modified": now_datetime(), "modified_by": frappe.session.user}
    columns = ",".join(f"`{field}`=%s" for field in values)
    frappe.db.sql(f"UPDATE {table(kind)} SET {columns} WHERE name=%s", (*values.values(), name))
    if kind == "Job" and "status" in values:
        from erp_basic.marking.projection import queue_for_job
        queue_for_job(name)


def history(company, job, action, details=None):
    # Только безопасные реквизиты; полный КМ, ключи и крипточасти сюда не передаются.
    return insert("History", {"company": company, "job": job, "actor": frappe.session.user,
                              "action": action, "details": canonical_json(details or {}),
                              "request_id": getattr(frappe.local, "erp_basic_request_id", None)})


def unpack(value, default=None):
    return json.loads(value) if value else (default if default is not None else {})
