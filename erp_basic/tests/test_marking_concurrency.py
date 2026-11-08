"""Гонки сканов и фоновых операций на разных процессах и реальной MariaDB."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

import frappe

from erp_basic.marking import service, outbox, storage
from erp_basic.marking.codes import parse_code
from erp_basic.tests import fixtures

GTIN = "09506000134352"


def key():
    return "mark-race-" + uuid.uuid4().hex


def raw():
    return "01" + GTIN + "21TEST" + uuid.uuid4().hex[:12] + "\x1d91TEST\x1d92NOT-A-SIGNATURE"


def workers(requests):
    frappe.db.commit()
    with tempfile.TemporaryDirectory(prefix="marking-race-") as directory:
        folder, children = Path(directory), []
        start_at = time.time() + 1.5
        for index, request in enumerate(requests):
            path, output = folder / f"{index}.json", folder / f"{index}.result.json"
            path.write_text(json.dumps({"site": frappe.local.site,
                                        "sites_path": str(Path(frappe.local.sites_path).resolve()),
                                        "start_at": start_at, "output": str(output), **request}))
            children.append((subprocess.Popen([sys.executable, "-m", "erp_basic.tests.marking_worker", str(path)],
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True), output))
        results = []
        for process, output in children:
            stdout, stderr = process.communicate(timeout=50)
            if process.returncode:
                raise RuntimeError("Marking worker failed: " + stderr[-2000:])
            results.append(json.loads(output.read_text()) if output.exists() else {"response_lost": True})
    frappe.db.rollback()
    return results


def run():
    env = fixtures.setup()
    company = env["company"]
    service.configure(company, "Test", "ERPBasic", {}, key())
    item = fixtures.item("MARK-RACE").name

    def job():
        return service.create_job(company, "wholesale", "test-pu-v1", "receive",
                                   [{"item_code": item, "gtin": GTIN, "quantity": 1}], key())["job"]["name"]

    def params(name, code=None):
        return {"company": company, "name": name, "raw_code": code or raw(), "expected_revision": 0,
                "idempotency_key": key(), "device_id": str(uuid.uuid4()), "event_id": str(uuid.uuid4()), "sequence": 1}

    same = job()
    data = params(same)
    results = workers([{"operation": "scan", "params": data} for _ in range(4)])
    assert all(result.get("ok") for result in results), results
    assert frappe.db.count(storage.PREFIX + "Scan", {"job": same}) == 1
    assert frappe.db.get_value(storage.PREFIX + "Job", same, "accepted_count") == 1

    repeated_event = job()
    data = params(repeated_event)
    results = workers([{"operation": "scan", "params": {**data, "idempotency_key": key()}} for _ in range(2)])
    assert all(result.get("ok") for result in results), results
    assert frappe.db.count(storage.PREFIX + "Scan", {"job": repeated_event}) == 1

    first, second, code = job(), job(), raw()
    results = workers([{"operation": "scan", "params": params(name, code)} for name in (first, second)])
    assert all(result.get("ok") for result in results), results
    assert sorted(result["result"]["scan"]["result"] for result in results) == ["Accepted", "Conflict"], results
    identity = parse_code(code, "gs1-91-92-v1").identity
    assert frappe.db.count(storage.PREFIX + "Code", {"name": identity}) == 1
    assert frappe.db.get_value(storage.PREFIX + "Code", identity, "reserved_job") in {first, second}

    first, second = job(), job()
    data = params(first)
    results = workers([{"operation": "scan", "params": {**data, "name": name, "idempotency_key": key()}}
                       for name in (first, second)])
    assert sum(bool(result.get("ok")) for result in results) == 1, results
    assert next(result["error"] for result in results if not result.get("ok")) == "OperationConflict"

    lost = job()
    data = params(lost)
    assert workers([{"operation": "scan", "params": data, "drop_response": True}])[0]["response_lost"]
    assert workers([{"operation": "scan", "params": data}])[0]["ok"]
    assert frappe.db.count(storage.PREFIX + "Scan", {"job": lost}) == 1

    service.complete_job(company, lost, 1, key())
    operation = outbox.enqueue(company, lost, "commission", "unknown", key())
    results = workers([{"operation": "dispatch", "params": {"name": operation["name"]}} for _ in range(2)])
    assert all(result.get("ok") for result in results), results
    assert frappe.db.get_value(storage.PREFIX + "Operation", operation["name"], "state") == "Unknown"
    assert frappe.db.get_value(storage.PREFIX + "Operation", operation["name"], "attempts") == 1
    code_id = parse_code(data["raw_code"], "gs1-91-92-v1").identity
    assert frappe.db.get_value(storage.PREFIX + "Code", code_id, "reserved_job") == lost
    results = workers([{"operation": "reconcile", "params": {"name": operation["name"]}} for _ in range(2)])
    assert all(result.get("ok") for result in results), results
    assert frappe.db.get_value(storage.PREFIX + "Operation", operation["name"], "state") == "Accepted"
    assert frappe.db.get_value(storage.PREFIX + "Operation", operation["name"], "attempts") == 1
    assert frappe.db.get_value(storage.PREFIX + "Code", code_id, "reserved_job") == lost
    assert frappe.db.get_value(storage.PREFIX + "Code", code_id, "external_state") == "Unverified"

    lease = job()
    data = params(lease)
    service.scan_code(**data)
    service.complete_job(company, lease, 1, key())
    operation = outbox.enqueue(company, lease, "commission", "accept", key())
    storage.update("Operation", operation["name"], {"state": "Dispatching", "lease_token": str(uuid.uuid4()),
                                                   "lease_until": "2000-01-01 00:00:00", "attempts": 1})
    results = workers([{"operation": "dispatch", "params": {"name": operation["name"]}}])
    assert results[0]["ok"] and results[0]["result"]["state"] == "Unknown", results
    assert frappe.db.get_value(storage.PREFIX + "Code", parse_code(data["raw_code"], "gs1-91-92-v1").identity,
                               "reserved_job") == lease
    frappe.db.commit()
    return {"scenarios": 7, "database": frappe.db.sql("SELECT VERSION()")[0][0], "mocks": False,
            "parallel_http_key": "passed", "event_different_keys": "passed", "exclusive_reservation": "passed",
            "event_payload_conflict": "passed", "committed_response_lost": "passed", "unknown_reconciliation": "passed",
            "expired_lease_keeps_reservation": "passed", "external_transmission": False}
