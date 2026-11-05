#!/usr/bin/env python3
"""CI: настоящие MariaDB/Redis, чистый Site, SQL/HTTP тесты и полный restore drill."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(root, site):
    runtime_module = load("ci_runtime", "окружение.py")
    runtime = runtime_module.Runtime(root)
    env = runtime.env
    os.environ.update(env)
    started = time.monotonic()
    records = []
    def checked(name, command, *, cwd=None):
        begin = time.monotonic()
        subprocess.run(list(map(str, command)), cwd=cwd or root / "bench", env=env, check=True)
        records.append({"name": name, "duration_seconds": round(time.monotonic() - begin, 3), "passed": True})
    try:
        runtime.start_databases()
        runtime_module.create_site(root, site, testing=True)
        bench = root / "tools/bin/bench"
        checked("assets", [bench, "build", "--production", "--apps", "frappe,erpnext,erp_basic"])
        checked("schema", [bench, "--site", site, "migrate"])
        checked("business", [bench, "--site", site, "execute", "erp_basic.tests.test_core.run"])
        checked("concurrency", [bench, "--site", site, "execute", "erp_basic.tests.test_concurrency.run"])
        checked("integration", [bench, "--site", site, "execute", "erp_basic.tests.test_integration.run"])
        checked("migration", [bench, "--site", site, "execute", "erp_basic.tests.test_migration.run"])
        runtime.start_application(workers=False)
        checked("http", [bench, "--site", site, "execute", "erp_basic.tests.test_http.run",
                         "--kwargs", json.dumps({"base_url": f"http://127.0.0.1:{runtime.ports['http']}", "host": site})])
        checked("queue", [root / "bench/env/bin/python", HERE / "проверка-очереди.py",
                          "--root", root, "--site", site])
        # Тесты закончились, burst worker остановился; scheduler не запускался.
        restore = load("ci_restore", "проверка-восстановления.py")
        restore_report = restore.run(root, site)
        report = {"passed": True, "environment": runtime.health(), "checks": records, "restore": restore_report,
                  "duration_seconds": round(time.monotonic() - started, 3)}
        runtime_module.write_json(root / "reports/server-ci.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        runtime.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--site", default="ci.erp-basic.localhost")
    args = parser.parse_args()
    main(args.root.resolve(), args.site)
