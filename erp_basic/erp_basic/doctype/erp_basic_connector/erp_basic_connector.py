import hashlib
from frappe.model.document import Document
from erp_basic.integration import validate_connector

class ERPBasicConnector(Document):
    def autoname(self):
        self.name = hashlib.sha256((self.source_system + ":" + self.source_tenant.lower()).encode()).hexdigest()

    def validate(self):
        validate_connector(self)
