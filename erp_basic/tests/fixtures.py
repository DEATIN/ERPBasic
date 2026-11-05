"""Набор только для изолированного Site с allow_tests; без общих паролей."""

import frappe
from frappe.utils import getdate, nowdate


COMPANY = "ERP Basic Test"
OTHER_COMPANY = "ERP Basic Other"


def setup():
    if not frappe.conf.get("allow_tests"):
        frappe.throw("Тестовые данные разрешены только в Site с allow_tests.")
    frappe.set_user("Administrator")
    if not frappe.db.exists("Item Group", "All Item Groups"):
        from erpnext.setup.setup_wizard.operations.install_fixtures import install
        install("Russian Federation")
    year = getdate(nowdate()).year
    for number in (year - 1, year, year + 1):
        if not frappe.db.exists("Fiscal Year", str(number)):
            frappe.get_doc({"doctype": "Fiscal Year", "year": str(number),
                            "year_start_date": f"{number}-01-01", "year_end_date": f"{number}-12-31"}).insert()
    for company, abbr in ((COMPANY, "EBT"), (OTHER_COMPANY, "EBO")):
        if not frappe.db.exists("Company", company):
            frappe.get_doc({"doctype": "Company", "company_name": company, "abbr": abbr,
                            "country": "Russian Federation", "default_currency": "RUB", "chart_of_accounts": "Standard",
                            "enable_perpetual_inventory": 1}).insert()
    for doctype, name, values in (
        ("Item Group", "ERP Basic Test", {"item_group_name": "ERP Basic Test", "parent_item_group": "All Item Groups"}),
        ("Customer Group", "ERP Basic Test", {"customer_group_name": "ERP Basic Test", "parent_customer_group": "All Customer Groups"}),
        ("Supplier Group", "ERP Basic Test", {"supplier_group_name": "ERP Basic Test", "parent_supplier_group": "All Supplier Groups"}),
        ("Price List", "ERP Basic Selling", {"price_list_name": "ERP Basic Selling", "selling": 1, "currency": "RUB"}),
        ("Price List", "ERP Basic Buying", {"price_list_name": "ERP Basic Buying", "buying": 1, "currency": "RUB"}),
        ("Customer", "ERP Basic Customer", {"customer_name": "ERP Basic Customer", "customer_type": "Company",
                                            "customer_group": "ERP Basic Test", "territory": "All Territories", "default_currency": "RUB"}),
        ("Supplier", "ERP Basic Supplier", {"supplier_name": "ERP Basic Supplier", "supplier_type": "Company",
                                            "supplier_group": "ERP Basic Test", "default_currency": "RUB"}),
    ):
        if not frappe.db.exists(doctype, name):
            frappe.get_doc({"doctype": doctype, **values}).insert()
    names = {}
    for company in (COMPANY, OTHER_COMPANY):
        names[company] = frappe.db.get_value("Warehouse", {"company": company, "warehouse_name": "Stores"}, "name")
        if not names[company]:
            warehouse = frappe.get_doc({"doctype": "Warehouse", "warehouse_name": "Stores", "company": company})
            warehouse.insert()
            names[company] = warehouse.name
    make_user("sales.erp-basic@example.invalid", ["Sales User"], [COMPANY])
    make_user("stock.erp-basic@example.invalid", ["Stock User"], [COMPANY])
    make_user("observer.erp-basic@example.invalid", ["ERP Basic Observer"], [COMPANY])
    make_user("other.erp-basic@example.invalid", ["Sales User", "Stock User"], [OTHER_COMPANY])
    make_user("unscoped.erp-basic@example.invalid", ["Sales User", "Stock User"], [])
    frappe.db.commit()
    return {"company": COMPANY, "other_company": OTHER_COMPANY, "warehouse": names[COMPANY],
            "other_warehouse": names[OTHER_COMPANY], "customer": "ERP Basic Customer", "supplier": "ERP Basic Supplier"}


def make_user(email, roles, companies):
    if not frappe.db.exists("User", email):
        frappe.get_doc({"doctype": "User", "email": email, "first_name": email.split(".")[0],
                        "enabled": 1, "send_welcome_email": 0, "user_type": "System User",
                        "roles": [{"role": role} for role in ["ERP Basic User", *roles]]}).insert()
    for company in companies:
        if not frappe.db.exists("User Permission", {"user": email, "allow": "Company", "for_value": company}):
            frappe.get_doc({"doctype": "User Permission", "user": email, "allow": "Company",
                            "for_value": company, "apply_to_all_doctypes": 1}).insert()
    return email


def item(prefix="EB", *, stock=True):
    name = prefix + "-" + frappe.generate_hash(length=10)
    return frappe.get_doc({"doctype": "Item", "item_code": name, "item_name": name,
                           "item_group": "ERP Basic Test", "stock_uom": "Nos", "is_stock_item": int(stock),
                           "valuation_method": "Moving Average", "is_sales_item": 1, "is_purchase_item": 1}).insert()
