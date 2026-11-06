"""Первичная организация после создания Site; пароль администратора задаёт bench new-site."""

import frappe
from frappe.utils import getdate, nowdate

from erp_basic.access import require_administrator


@frappe.whitelist(methods=["POST"])
def create_company(name, abbreviation, currency="RUB", country="Russian Federation", inventory_account=None):
    require_administrator()
    from erpnext.setup.setup_wizard.operations.install_fixtures import install
    from frappe.utils.nestedset import get_root_of

    if not get_root_of("Item Group"):
        install(country)
    year = getdate(nowdate()).year
    if not frappe.db.exists("Fiscal Year", str(year)):
        frappe.get_doc({"doctype": "Fiscal Year", "year": str(year), "year_start_date": f"{year}-01-01",
                        "year_end_date": f"{year}-12-31"}).insert()
    if frappe.db.exists("Company", name):
        company = frappe.get_doc("Company", name, for_update=True)
        if company.default_currency != currency or company.abbr != abbreviation or company.country != country:
            frappe.throw("Существующая организация имеет другие реквизиты.")
    else:
        company = frappe.get_doc({"doctype": "Company", "company_name": name, "abbr": abbreviation,
                                  "default_currency": currency, "country": country, "chart_of_accounts": "Standard",
                                  "enable_perpetual_inventory": 1}).insert()
    if not company.default_inventory_account:
        candidates = frappe.get_list("Account", filters={"company": company.name, "account_type": "Stock", "is_group": 0},
                                      pluck="name", page_length=2)
        account = inventory_account or (candidates[0] if len(candidates) == 1 else None)
        if not account:
            frappe.throw("Укажите inventory_account: в плане счетов нет единственного счёта запасов.")
        account_doc = frappe.get_doc("Account", account)
        account_doc.check_permission("read")
        if account_doc.company != company.name or account_doc.is_group or account_doc.account_type != "Stock":
            frappe.throw("Нужен счёт запасов выбранной организации.")
        company.default_inventory_account = account
        company.save()
    if not frappe.db.get_single_value("Global Defaults", "default_company"):
        frappe.db.set_single_value("Global Defaults", "default_company", company.name)
        frappe.db.set_single_value("Global Defaults", "default_currency", currency)
    # Установка справочников закончена, администратор может использовать /erp и полный Desk.
    frappe.db.set_single_value("System Settings", "setup_complete", 1)
    return {"company": company.name, "currency": company.default_currency,
            "inventory_account": company.default_inventory_account,
            "warehouses": frappe.get_list("Warehouse", filters={"company": company.name},
                                           fields=["name", "is_group"], page_length=100)}
