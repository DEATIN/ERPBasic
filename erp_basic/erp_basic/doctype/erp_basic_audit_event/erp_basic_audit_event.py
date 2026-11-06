"""Добавляемый журнал: обычные API не могут создавать или исправлять события."""

import frappe
from frappe.model.document import Document


class ERPBasicAuditEvent(Document):
    def validate(self):
        if not self.is_new() or not self.flags.erp_basic_internal_audit:
            frappe.throw("События аудита создаются только учётной операцией.", frappe.PermissionError)

    def on_trash(self):
        frappe.throw("Удаление событий аудита запрещено.", frappe.PermissionError)

    def before_rename(self, *args, **kwargs):
        frappe.throw("Переименование событий аудита запрещено.", frappe.PermissionError)
