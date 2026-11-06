"""Подготовка и независимая SQL-проверка настоящего браузерного сценария.

Случайный пароль хранится только в указанном файле 0600 вне репозитория.
Вызывающий CI удаляет файл в finally; значения секрета не возвращаются и не печатаются.
"""

from decimal import Decimal
import json
import os
from pathlib import Path
import secrets

import frappe
from frappe.utils import nowdate

from erp_basic.tests import fixtures


USER = "browser.erp-basic@example.invalid"


def _allow_tests():
    if not frappe.conf.get("allow_tests"):
        frappe.throw("Браузерные проверки разрешены только в изолированном Site с allow_tests.")


def prepare(credentials_path):
    _allow_tests()
    path = Path(credentials_path).resolve()
    repo = Path(__file__).resolve().parents[2]
    if path.is_relative_to(repo):
        raise ValueError("Секрет браузерной проверки должен храниться вне репозитория.")
    env = fixtures.setup()
    fixtures.make_user(USER, ["Sales User"], [env["company"]])
    password = secrets.token_urlsafe(32)
    user = frappe.get_doc("User", USER)
    user.new_password = password
    user.send_welcome_email = 0
    user.save()
    item = fixtures.item(prefix="BROWSER", stock=False).name
    frappe.db.commit()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"version": 1, "site": frappe.local.site, "user": USER, "password": password,
                   "company": env["company"], "customer": env["customer"], "item": item,
                   "price_list": "ERP Basic Selling", "business_date": nowdate(),
                   "expected_total": "251.00"}, stream)
    return {"prepared": True, "site": frappe.local.site, "company": env["company"], "credentials_mode": "0600"}


def verify(credentials_path, result_path):
    """Сверить результат UI с документом в БД через новый bench-процесс."""
    _allow_tests()
    with open(credentials_path, encoding="utf-8") as stream:
        credentials = json.load(stream)
    with open(result_path, encoding="utf-8") as stream:
        result = json.load(stream)
    assert credentials["site"] == frappe.local.site == result["site"], "Проверяется тот же Site"
    assert result["mocks"] is False and result["docstatus"] == 1
    document = frappe.get_doc("Sales Order", result["name"])
    assert document.company == credentials["company"] == result["company"]
    assert document.owner == credentials["user"]
    assert document.docstatus == 1, "Документ действительно проведён в СУБД"
    assert Decimal(str(document.grand_total)) == Decimal(credentials["expected_total"])
    assert len(document.items) == 1 and document.items[0].item_code == credentials["item"]
    assert document.items[0].qty == 2 and document.items[0].rate == 125.5
    assert frappe.db.count("Sales Order Item", {"item_code": credentials["item"]}) == 1, "Повторный вход не создаёт дубль"
    return {"verified": True, "transport": "Chromium → HTTP → ERPNext → SQL", "name": document.name,
            "docstatus": document.docstatus, "grand_total": "251.00", "mocks": False}
