"""Зафиксированный исходник и результат контрольной миграции."""

import frappe
from frappe.model.document import Document


class ERPBasicMigrationRun(Document):
    def validate(self):
        if not self.is_new() or not self.flags.erp_basic_migration_internal:
            frappe.throw("Запуск миграции создаётся только сервисом переноса.", frappe.PermissionError)

    def on_trash(self):
        frappe.throw("Удаление истории миграции запрещено.", frappe.PermissionError)

    def before_rename(self, *args, **kwargs):
        frappe.throw("Переименование истории миграции запрещено.", frappe.PermissionError)
