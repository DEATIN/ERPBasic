"""Расширение штатных документов без изменения исходников ERPNext."""

app_name = "erp_basic"
app_title = "ERP Basic"
app_publisher = "ERP Basic"
app_description = "Серверный управленческий учёт малого бизнеса"
app_email = "support@clientora.ru"
app_license = "UNLICENSED"
required_apps = ["erpnext"]

after_install = "erp_basic.install.after_install"
after_migrate = "erp_basic.install.after_migrate"
auth_hooks = ["erp_basic.boundary.check_request"]

_company_documents = {
    "Company": "company", "Warehouse": "warehouse", "Quotation": "quotation",
    "Sales Order": "sales_order", "Delivery Note": "delivery_note", "Sales Invoice": "sales_invoice",
    "Purchase Order": "purchase_order", "Purchase Receipt": "purchase_receipt",
    "Purchase Invoice": "purchase_invoice", "Payment Entry": "payment_entry",
    "Stock Entry": "stock_entry", "Stock Reconciliation": "stock_reconciliation",
    "GL Entry": "gl_entry", "Stock Ledger Entry": "stock_ledger_entry",
    "Journal Entry": "journal_entry",
    "ERP Basic Audit Event": "audit_event",
}
_shared_documents = ["Item", "Customer", "Supplier", "Contact", "Address"]
has_permission = {name: "erp_basic.access.document_permission" for name in _company_documents}
has_permission.update({name: "erp_basic.access.shared_permission" for name in _shared_documents})
has_permission["File"] = "erp_basic.boundary.file_permission"
permission_query_conditions = {
    name: f"erp_basic.access.{method}_query" for name, method in _company_documents.items()
}
permission_query_conditions.update({name: "erp_basic.access.shared_query" for name in _shared_documents})

_business_documents = [
    "Quotation", "Sales Order", "Delivery Note", "Sales Invoice", "Purchase Order", "Purchase Receipt",
    "Purchase Invoice", "Payment Entry", "Stock Entry", "Stock Reconciliation", "Item", "Customer", "Supplier",
    "Journal Entry",
]
doc_events = {
    name: {
        "validate": "erp_basic.guards.validate_document",
        "before_submit": "erp_basic.guards.before_submit",
        "before_cancel": "erp_basic.guards.before_cancel",
        **{event: ["erp_basic.audit.record_event", "erp_basic.integration.document_event"]
           for event in ("after_insert", "on_update", "on_submit", "on_cancel")},
    } for name in _business_documents
}
doc_events["File"] = {"validate": "erp_basic.boundary.protect_attachment"}

scheduler_events = {"cron": {"* * * * *": ["erp_basic.integration.dispatch_pending"]}}
