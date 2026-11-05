"""Явный контракт упрощённого интерфейса над штатными DocType ERPNext."""

import frappe


ITEM_FIELDS = (
    "name", "item_code", "qty", "rate", "uom", "conversion_factor", "warehouse",
    "delivery_date", "schedule_date", "sales_order", "so_detail", "against_sales_order",
    "delivery_note", "dn_detail", "purchase_order", "purchase_order_item",
    "purchase_receipt", "purchase_receipt_item", "pr_detail", "prevdoc_docname", "quotation_item",
    "expense_account", "cost_center",
)
COMMON = ("currency", "conversion_rate", "remarks", "set_warehouse", "additional_discount_percentage", "apply_discount_on")
READ_ONLY = (
    "modified", "docstatus", "status", "owner", "creation", "company", "title", "grand_total",
    "base_grand_total", "net_total", "total_taxes_and_charges", "outstanding_amount", "rounded_total",
    "total_qty", "per_delivered", "per_received", "per_billed", "advance_paid", "total_allocated_amount",
    "unallocated_amount", "difference_amount", "is_return", "return_against",
    "paid_from_account_currency", "paid_to_account_currency",
)
ROW_READ_ONLY = (
    "item_name", "description", "amount", "base_amount", "net_rate", "net_amount", "stock_qty",
    "stock_uom", "valuation_rate", "incoming_rate", "outgoing_rate", "stock_value_difference",
    "delivered_qty", "received_qty", "billed_amt", "actual_qty", "current_qty", "current_valuation_rate",
    "outstanding_amount", "total_amount",
)


def spec(doctype, fields=(), children=None, *, master=False, label="", search=()):
    return {"doctype": doctype, "fields": tuple(fields), "children": children or {}, "master": master,
            "label": label or doctype, "search": tuple(search)}


KINDS = {
    "quotes": spec("Quotation", ("quotation_to", "party_name", "transaction_date", "valid_till",
                                  "selling_price_list", *COMMON), {"items": ITEM_FIELDS}, search=("party_name",)),
    "orders": spec("Sales Order", ("customer", "transaction_date", "delivery_date", "selling_price_list",
                                    *COMMON), {"items": ITEM_FIELDS}, search=("customer",)),
    "deliveries": spec("Delivery Note", ("customer", "posting_date", "posting_time", "set_posting_time",
                                         "selling_price_list", *COMMON), {"items": ITEM_FIELDS}, search=("customer",)),
    "invoices": spec("Sales Invoice", ("customer", "posting_date", "posting_time", "set_posting_time",
                                        "due_date", "update_stock", "selling_price_list", *COMMON),
                     {"items": ITEM_FIELDS}, search=("customer",)),
    "purchaseOrders": spec("Purchase Order", ("supplier", "transaction_date", "schedule_date",
                                               "buying_price_list", *COMMON), {"items": ITEM_FIELDS}, search=("supplier",)),
    "receipts": spec("Purchase Receipt", ("supplier", "posting_date", "posting_time", "set_posting_time",
                                           "buying_price_list", *COMMON), {"items": ITEM_FIELDS}, search=("supplier",)),
    "purchaseInvoices": spec("Purchase Invoice", ("supplier", "posting_date", "posting_time", "set_posting_time",
                                                   "due_date", "update_stock", "bill_no", "bill_date",
                                                   "buying_price_list", *COMMON),
                             {"items": ITEM_FIELDS}, search=("supplier",)),
    "stockDocs": spec("Stock Entry", ("stock_entry_type", "purpose", "posting_date", "posting_time",
                                       "set_posting_time", "from_warehouse", "to_warehouse", "remarks"),
                      {"items": ("name", "item_code", "qty", "uom", "conversion_factor", "s_warehouse",
                                 "t_warehouse", "basic_rate", "expense_account", "cost_center")}),
    "inventory": spec("Stock Reconciliation", ("posting_date", "posting_time", "purpose", "expense_account",
                                                "cost_center"),
                      {"items": ("name", "item_code", "warehouse", "qty", "valuation_rate")}),
    "payments": spec("Payment Entry", ("payment_type", "posting_date", "party_type", "party", "paid_from",
                                        "paid_to", "paid_amount", "received_amount", "source_exchange_rate",
                                        "target_exchange_rate", "reference_no", "reference_date", "remarks"),
                     {"references": ("name", "reference_doctype", "reference_name", "allocated_amount")}, search=("party",)),
    "items": spec("Item", ("item_code", "item_name", "item_group", "stock_uom", "is_stock_item",
                            "is_sales_item", "is_purchase_item", "description", "disabled"), master=True,
                  search=("item_code", "item_name")),
    "customers": spec("Customer", ("customer_name", "customer_type", "customer_group", "territory",
                                    "tax_id", "default_currency", "disabled"), master=True, search=("customer_name",)),
    "suppliers": spec("Supplier", ("supplier_name", "supplier_type", "supplier_group", "tax_id",
                                    "default_currency", "disabled"), master=True, search=("supplier_name",)),
    "warehouses": spec("Warehouse", ("warehouse_name", "parent_warehouse", "account", "is_group", "disabled"),
                       master=True, search=("warehouse_name",)),
}


def get_spec(kind):
    if kind not in KINDS:
        frappe.throw("Неизвестный вид документа.")
    return KINDS[kind]


def fields_for(kind, *, rows=False):
    selected = get_spec(kind)
    meta = frappe.get_meta(selected["doctype"])
    fields = ("name", *selected["fields"], *READ_ONLY)
    return list(dict.fromkeys(field for field in fields if field == "name" or meta.has_field(field)
                             or field in ("modified", "owner", "creation", "docstatus")))
