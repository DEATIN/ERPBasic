"""Опубликованный синтетический замер Python → MariaDB, отдельно от UI/ЧЗ/физического сканера."""

import math
import platform
import time
import uuid

import frappe

from erp_basic.marking import service, storage
from erp_basic.tests import fixtures


def percentile(values, fraction):
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] * 1000, 3)


def run(count=1000):
    if not frappe.conf.get("allow_tests"):
        frappe.throw("Нагрузочная проверка разрешена только в изолированном тестовом Site.")
    count = service.integer(count, minimum=100, maximum=10000)
    env = fixtures.setup()
    company, item, gtin = env["company"], fixtures.item("MARK-LOAD").name, "09506000134352"
    job = service.create_job(company, "wholesale", "test-pu-v1", "receive",
                              [{"item_code": item, "gtin": gtin, "quantity": count}], uuid.uuid4().hex)["job"]["name"]
    device, session_id = str(uuid.uuid4()), uuid.uuid4().hex[:8]
    requests = [{"company": company, "name": job, "raw_code": "01" + gtin + "21TEST" + session_id + str(number).zfill(6) +
                  "\x1d91TEST\x1d92NOT-A-SIGNATURE", "expected_revision": number - 1,
                  "device_id": device, "event_id": str(uuid.uuid5(uuid.UUID(device), str(number))),
                  "sequence": number, "idempotency_key": uuid.uuid4().hex} for number in range(1, count + 1)]
    frappe.db.commit()
    durations, started = [], time.perf_counter()
    for index, params in enumerate(requests):
        begin = time.perf_counter()
        result = service.scan_code(**params)
        frappe.db.commit()  # Каждый ACK подтверждает отдельную устойчивую транзакцию.
        durations.append(time.perf_counter() - begin)
        assert result["scan"]["result"] == "Accepted"
        if index == count // 2:
            frappe.db.close()
            frappe.db.connect()
            assert frappe.db.get_value(storage.PREFIX + "Job", job, "accepted_count") == index + 1
    for params in requests[-10:]:
        repeated = service.scan_code(**{**params, "idempotency_key": uuid.uuid4().hex})
        assert repeated["job"]["accepted_count"] == count
        frappe.db.commit()
    page_times = []
    for _ in range(20):
        begin = time.perf_counter()
        response = service.get_job(company, job)
        page_times.append(time.perf_counter() - begin)
        assert len(response["scans"]) == 25 and response["has_more"]
    assert frappe.db.count(storage.PREFIX + "Scan", {"job": job}) == count
    assert frappe.db.get_value(storage.PREFIX + "Job", job, "accepted_count") == count
    assert frappe.db.count("Stock Ledger Entry", {"item_code": item}) == 0
    report = {"passed": True, "scope": "Python service + commit → local MariaDB; no HTTP/browser/hardware/ЧЗ",
              "events": count, "distinct_items": 1, "lost_events": 0, "unexpected_duplicates": 0,
              "reconnected_at": count // 2 + 1, "event_replays": 10,
              "scan_commit_ms": {label: percentile(durations, fraction) for label, fraction in (("p50", .5), ("p95", .95), ("p99", .99))},
              "job_page_ms": {label: percentile(page_times, fraction) for label, fraction in (("p50", .5), ("p95", .95), ("p99", .99))},
              "duration_seconds": round(time.perf_counter() - started, 3), "python": platform.python_version(),
              "machine": platform.machine(), "database": frappe.db.sql("SELECT VERSION()")[0][0],
              "million_code_registry_verified": False, "external_transmission": False}
    frappe.db.commit()
    return report
