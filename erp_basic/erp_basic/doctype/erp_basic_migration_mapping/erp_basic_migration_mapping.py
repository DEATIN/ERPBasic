"""Неизменяемая карта старых идентификаторов и исходной истории."""

import frappe
from frappe.model.document import Document


class ERPBasicMigrationMapping(Document):
    def validate(self):
        if not self.is_new() or not self.flags.erp_basic_migration_internal:
            frappe.throw("Карта миграции создаётся только сервисом переноса.", frappe.PermissionError)

    def on_trash(self):
        frappe.throw("Удаление карты миграции запрещено.", frappe.PermissionError)

    def before_rename(self, *args, **kwargs):
        frappe.throw("Переименование карты миграции запрещено.", frappe.PermissionError)
