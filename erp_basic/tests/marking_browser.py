"""Данные и независимая SQL-проверка живого рабочего места маркировки."""

import json
from pathlib import Path
import uuid

import frappe

from erp_basic.tests import fixtures
from erp_basic.tests.browser import USER, _allow_tests
from erp_basic.marking import service, storage
from erp_basic.marking.codes import parse_code


def prepare(credentials_path):
    _allow_tests()
    path = Path(credentials_path).resolve()
    if path.is_relative_to(Path(__file__).resolve().parents[2]) or path.stat().st_mode & 0o077:
        raise ValueError("Credentials должны быть вне репозитория с правами 0600")
    credentials = json.loads(path.read_text())
    frappe.set_user("Administrator")
    user = frappe.get_doc("User", USER)
    user.add_roles("ERP Basic Marking User", "Stock User")
    company = credentials["company"]
    service.configure(company, "Test", "ERPBasic", {}, "browser-config-" + uuid.uuid4().hex)
    item = fixtures.item("MARK-BROWSER").name
    gtin = "09506000134352"
    codes = ["01" + gtin + "21TEST" + uuid.uuid4().hex[:12] + "\x1d91TEST\x1d92NOT-A-SIGNATURE" for _ in range(2)]
    credentials["marking"] = {"company": company, "item_code": item, "gtin": gtin, "profile": "test-pu-v1",
                               "role": "wholesale", "operation": "receive", "raw_codes": codes,
                               "ki_codes": [parse_code(code, "gs1-91-92-v1").canonical_ki for code in codes]}
    frappe.db.commit()
    path.write_text(json.dumps(credentials, ensure_ascii=False))
    return {"prepared": True, "credentials_mode": "0600", "test_only": True}


def verify(credentials_path, result_path):
    _allow_tests()
    credentials = json.loads(Path(credentials_path).read_text())
    result = json.loads(Path(result_path).read_text())
    fixture = credentials["marking"]
    assert result["site"] == credentials["site"] == frappe.local.site
    assert result["mocks"] is False
    job = frappe.get_doc(storage.PREFIX + "Job", result["name"])
    assert job.company == fixture["company"] and job.owner == credentials["user"]
    assert job.status == "Prepared" and job.accepted_count == 2 and job.environment == "Test"
    assert job.native_posted == 0 and job.rejected_count >= 1
    assert frappe.db.count(storage.PREFIX + "Scan", {"job": job.name, "result": "Accepted"}) == 2
    assert frappe.db.count(storage.PREFIX + "Scan", {"job": job.name, "result": "Duplicate"}) >= 1
    for raw in fixture["raw_codes"]:
        code = frappe.get_doc(storage.PREFIX + "Code", parse_code(raw, "gs1-91-92-v1").identity)
        assert code.reserved_job == job.name and code.external_state == "Unverified"
        assert code.raw_encrypted != raw
    assert frappe.db.count("Stock Ledger Entry", {"item_code": fixture["item_code"]}) == 0
    return {"verified": True, "transport": "Chromium → HTTP → маркировка → SQL", "name": job.name,
            "accepted_count": 2, "status": "Prepared", "mocks": False, "external_transmission": False}
