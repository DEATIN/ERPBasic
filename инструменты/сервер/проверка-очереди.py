#!/usr/bin/env python3
"""Настоящий Frappe RQ worker читает Company из MariaDB через Redis queue."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import time


def run(root, site):
    spec = importlib.util.spec_from_file_location("queue_runtime", Path(__file__).with_name("окружение.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    env = module.environment(root)
    os.environ.update(env)
    os.chdir(root / "bench/sites")
    import frappe
    from frappe.utils.background_jobs import enqueue
    frappe.init(site=site)
    frappe.connect()
    try:
        if not frappe.conf.allow_tests:
            raise RuntimeError("Проверка очереди разрешена только в Site с allow_tests")
        frappe.set_user("Administrator")
        expected = frappe.db.count("Company")
        job = enqueue("frappe.client.get_count", queue="short", doctype="Company", cache=False,
                      timeout=60, is_async=True, job_id="erp-basic-queue-" + secrets.token_hex(12))
        if job.get_status(refresh=True) != "queued":
            raise RuntimeError("Задание не было помещено в реальную очередь")
        start = time.monotonic()
        subprocess.run([str(root / "tools/bin/bench"), "worker", "--queue", "short", "--burst"],
                       cwd=root / "bench", env=env, check=True, timeout=120)
        if job.get_status(refresh=True) != "finished" or job.result != expected:
            raise RuntimeError("Worker не вернул ожидаемый результат чтения настоящей MariaDB")
        report = {"passed": True, "transport": "Redis RQ + separate worker + MariaDB",
                  "company_count": expected, "duration_seconds": round(time.monotonic() - start, 3)}
        module.write_json(root / "reports/queue.json", report)
        job.delete()
        print(json.dumps(report, ensure_ascii=False))
        return report
    finally:
        frappe.destroy()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--site", default="erp-basic.localhost")
    args = parser.parse_args()
    run(args.root.resolve(), args.site)
