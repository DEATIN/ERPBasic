"""Узкое расширение File: DocShare не обходит область маркировки при ZIP/download/reuse."""

import frappe
from frappe.core.doctype.file.file import File

from erp_basic.access import is_administrator


class ERPBasicFile(File):
    def before_insert(self):
        # Проверить источник до native before_insert, который может скопировать существующий blob.
        if not is_administrator() and self.file_url and self.file_url.startswith("/private/files/"):
            from erp_basic.boundary import _readable_private_file
            _readable_private_file(self.file_url)
        return super().before_insert()

    def _marking_readable(self):
        if self.attached_to_doctype and self.attached_to_doctype.startswith("ERP Basic Marking "):
            from erp_basic.boundary import file_permission
            return file_permission(self, user=frappe.session.user) is not False
        return True

    def is_downloadable(self):
        return self._marking_readable() and super().is_downloadable()

    def get_content(self):
        # Native File.zip_files использует module-level has_permission; этот final read также проверяет scope.
        if not self._marking_readable():
            frappe.throw("Нет доступа к вложению маркировки.", frappe.PermissionError)
        return super().get_content()
