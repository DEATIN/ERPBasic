from frappe.model.document import Document
from erp_basic.integration import mapping_name, validate_mapping

class ERPBasicExternalMapping(Document):
    def autoname(self):
        self.name = mapping_name(self.connector, self.external_type, self.external_id.lower())

    def validate(self):
        validate_mapping(self)
