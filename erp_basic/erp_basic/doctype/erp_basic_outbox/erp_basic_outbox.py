from frappe.model.document import Document
from erp_basic.integration import deny_transport_edit

class ERPBasicOutbox(Document):
    def validate(self):
        deny_transport_edit(self)

    def on_trash(self):
        deny_transport_edit(self)
