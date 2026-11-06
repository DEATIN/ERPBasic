#!/usr/bin/env python3
"""Настоящая первичная настройка и её повтор на отдельном пустом тестовом Site."""

import argparse
import json
import os
from pathlib import Path


def run(root, site):
    import frappe
    from erp_basic.setup import create_company

    os.chdir(root / "bench/sites")
    frappe.init(site=site, sites_path=str(root / "bench/sites"))
    frappe.connect()
    try:
        if not frappe.conf.get("allow_tests"):
            raise RuntimeError("Проверка разрешена только на тестовом Site с allow_tests")
        if frappe.db.count("Company"):
            raise RuntimeError("Для первичной настройки нужен отдельный Site без организаций")
        frappe.set_user("Administrator")
        values = {"name": "ERP Basic Bootstrap Check", "abbreviation": "EBBC",
                  "currency": "RUB", "country": "Russian Federation"}
        first = create_company(**values)
        frappe.db.commit()
        tables = ("Company", "Account", "Warehouse", "Cost Center", "Fiscal Year", "Item Group")
        before = {doctype: frappe.db.count(doctype) for doctype in tables}
        second = create_company(**values)
        frappe.db.commit()
        after = {doctype: frappe.db.count(doctype) for doctype in tables}
        assert first == second, "Повтор настройки должен вернуть ту же организацию и счета"
        assert before == after, "Повтор не должен дублировать справочники ERPNext"
        assert after["Company"] == 1
        account = frappe.get_doc("Account", second["inventory_account"])
        assert account.company == values["name"] and account.account_type == "Stock" and not account.is_group
        assert second["currency"] == "RUB" and second["warehouses"]
        assert frappe.db.get_single_value("Global Defaults", "default_company") == values["name"]
        assert frappe.db.get_single_value("System Settings", "setup_complete")
        report = {"passed": True, "site": site, "company": second["company"],
                  "inventory_account_type": account.account_type, "repeat_created_rows": 0,
                  "native_row_counts": after}
        print(json.dumps(report, ensure_ascii=False))
        return report
    finally:
        frappe.destroy()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--site", required=True)
    args = parser.parse_args()
    run(args.root.resolve(), args.site)
