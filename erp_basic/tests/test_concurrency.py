"""Процессы конкурируют в реальной MariaDB; память и соединения не общие."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import frappe
from frappe.utils import nowdate

from erp_basic import commands
from erp_basic.tests import fixtures


def token():
    return "race-" + frappe.generate_hash(length=24)


def params(result, key=None):
    return {"kind": result["kind"], "name": result["document"]["name"], "company": result["company"],
            "expected_modified": str(result["document"]["modified"]), "idempotency_key": key or token(),
            "action": "submit"}


def workers(requests):
    # Родитель фиксирует только подготовку. Каждая команда самостоятельно commit/rollback.
    frappe.db.commit()
    site = frappe.local.site
    sites_path = str(Path(frappe.local.sites_path).resolve())
    with tempfile.TemporaryDirectory(prefix="erp-basic-race-") as directory:
        root = Path(directory)
        start_at = time.time() + 1.5
        children = []
        for index, request in enumerate(requests):
            path = root / f"{index}.json"
            output = root / f"{index}.result.json"
            data = {"site": site, "sites_path": sites_path, "start_at": start_at, "output": str(output), **request}
            path.write_text(json.dumps(data, default=str))
            children.append((subprocess.Popen([sys.executable, "-m", "erp_basic.tests.command_worker", str(path)],
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True), output))
        results = []
        for child, output in children:
            stdout, stderr = child.communicate(timeout=50)
            if child.returncode:
                raise RuntimeError(f"Worker failed: {stderr[-2500:]}")
            results.append(json.loads(output.read_text()) if output.exists() else {"response_lost": True})
    frappe.db.rollback()
    return results


def run():
    env = fixtures.setup()
    frappe.set_user("Administrator")
    isolation = frappe.db.sql("SELECT @@tx_isolation")[0][0]
    assert isolation == "READ-COMMITTED", "Стенд и рабочий профиль должны использовать READ-COMMITTED"
    company, warehouse = env["company"], env["warehouse"]

    def draft(item, purpose, qty, rate=None):
        row = {"item_code": item, "qty": qty}
        row["t_warehouse" if purpose == "Material Receipt" else "s_warehouse"] = warehouse
        if rate is not None:
            row["basic_rate"] = rate
        return commands.save_document("stockDocs", company, {"stock_entry_type": purpose,
                                      "posting_date": nowdate(), "items": [row]}, idempotency_key=token())

    def count_sle(name):
        return frappe.db.count("Stock Ledger Entry", {"voucher_no": name, "is_cancelled": 0})

    same_item = fixtures.item("RACE-SAME").name
    same = draft(same_item, "Material Receipt", 5, 100)
    same_params = params(same)
    results = workers([{"params": same_params} for _ in range(4)])
    assert all(result.get("ok") for result in results), results
    assert len({json.dumps(result["result"]["document"], sort_keys=True) for result in results}) == 1
    assert count_sle(same["document"]["name"]) == 1

    separate_item = fixtures.item("RACE-KEYS").name
    separate = draft(separate_item, "Material Receipt", 5, 100)
    results = workers([{"params": params(separate)} for _ in range(2)])
    assert sum(bool(result.get("ok")) for result in results) == 1, results
    assert count_sle(separate["document"]["name"]) == 1

    issue_a = draft(same_item, "Material Issue", 4)
    issue_b = draft(same_item, "Material Issue", 4)
    results = workers([{"params": params(issue_a)}, {"params": params(issue_b)}])
    assert sum(bool(result.get("ok")) for result in results) == 1, results
    assert frappe.db.get_value("Bin", {"item_code": same_item, "warehouse": warehouse}, "actual_qty") == 1
    assert sum(count_sle(doc["document"]["name"]) for doc in (issue_a, issue_b)) == 1

    lost_item = fixtures.item("RACE-LOST").name
    lost = draft(lost_item, "Material Receipt", 3, 120)
    lost_params = params(lost)
    result = workers([{"params": lost_params, "drop_response": True}])[0]
    assert result["response_lost"]
    repeated = workers([{"params": lost_params}])[0]
    assert repeated["ok"], repeated
    assert count_sle(lost["document"]["name"]) == 1
    assert frappe.db.get_value("Bin", {"item_code": lost_item, "warehouse": warehouse}, "actual_qty") == 3
    return {"scenarios": 4, "database": frappe.db.sql("SELECT VERSION()")[0][0], "isolation": isolation,
            "parallel_repeat": "passed", "different_keys": "passed", "oversell_5_two_by_4": "passed",
            "committed_response_lost": "passed"}
