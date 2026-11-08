#!/usr/bin/env python3
"""CI: настоящие MariaDB/Redis, чистый Site, SQL/HTTP тесты и полный restore drill."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time
import uuid


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(root, site, peer_site, *, browser_tests=False, use_running_services=False):
    if site == peer_site:
        raise ValueError("Основной и второй Site должны иметь разные имена и базы")
    runtime_module = load("ci_runtime", "окружение.py")
    runtime = runtime_module.Runtime(root)
    env = runtime.env
    os.environ.update(env)
    started = time.monotonic()
    records = []
    report = {"passed": False, "checks": records, "browser_requested": browser_tests,
              "external_marking_services": {
                  "verified": False,
                  "reason": "CI проверяет локальные SQL/HTTP/UI и тестовый симулятор; реальные ЧЗ, ЭДО и ККТ не участвуют.",
              }}
    private_paths = [root / "run" / f"{kind}-{uuid.uuid4().hex}.json" for kind in
                     ("tenant-credentials", "browser-credentials", "marking-browser-credentials")]
    tenant_credentials, browser_credentials, marking_browser_credentials = private_paths
    def checked(name, command, *, cwd=None, extra_env=None, json_result_path=None):
        begin = time.monotonic()
        success = False
        try:
            if json_result_path:
                json_result_path.unlink(missing_ok=True)
            completed = subprocess.run(list(map(str, command)), cwd=cwd or root / "bench", env=env | (extra_env or {}),
                                       stdout=subprocess.PIPE if json_result_path else None, text=True)
            if json_result_path:
                print(completed.stdout, end="")
            completed.check_returncode()
            result = json.loads(completed.stdout) if json_result_path else None
            if json_result_path:
                runtime_module.write_json(json_result_path, result)
            success = True
            return result
        finally:
            records.append({"name": name, "duration_seconds": round(time.monotonic() - begin, 3), "passed": success})
    try:
        if not use_running_services:
            runtime.start_databases()
        report["environment"] = runtime.health()
        checked("marking_domain", [root / "bench/env/bin/python", "-m", "unittest", "discover",
                                   "-s", "tests", "-p", "test_marking_domain.py"], cwd=REPO)
        runtime_module.create_site(root, site, testing=True)
        runtime_module.create_site(root, peer_site, testing=True)
        bench = root / "tools/bin/bench"
        checked("initial_company", [root / "bench/env/bin/python", HERE / "проверка-настройки.py",
                                    "--root", root, "--site", peer_site])
        checked("assets", [bench, "build", "--production", "--apps", "frappe,erpnext,erp_basic"])
        checked("schema", [bench, "--site", site, "migrate"])
        checked("peer_schema", [bench, "--site", peer_site, "migrate"])
        checked("business", [bench, "--site", site, "execute", "erp_basic.tests.test_core.run"])
        checked("concurrency", [bench, "--site", site, "execute", "erp_basic.tests.test_concurrency.run"])
        checked("integration", [bench, "--site", site, "execute", "erp_basic.tests.test_integration.run"])
        checked("migration", [bench, "--site", site, "execute", "erp_basic.tests.test_migration.run"])
        checked("marking", [bench, "--site", site, "execute", "erp_basic.tests.test_marking.run"])
        checked("marking_projection", [bench, "--site", site, "execute", "erp_basic.tests.test_marking_projection.run"])
        checked("marking_concurrency", [bench, "--site", site, "execute", "erp_basic.tests.test_marking_concurrency.run"])
        report["marking_load"] = checked("marking_load", [bench, "--site", site, "execute", "erp_basic.tests.test_marking_load.run",
                                                         "--kwargs", json.dumps({"count": 1000})],
                                         json_result_path=root / "reports/marking-load.json")
        if not use_running_services:
            runtime.start_application(workers=False)
        checked("http", [bench, "--site", site, "execute", "erp_basic.tests.test_http.run",
                         "--kwargs", json.dumps({"base_url": f"http://127.0.0.1:{runtime.ports['http']}", "host": site})])
        checked("marking_http", [bench, "--site", site, "execute", "erp_basic.tests.test_marking_http.run",
                                 "--kwargs", json.dumps({"base_url": f"http://127.0.0.1:{runtime.ports['http']}", "host": site})])
        checked("tenant_prepare", [bench, "--site", peer_site, "execute", "erp_basic.tests.test_http_tenant.prepare_peer",
                                   "--kwargs", json.dumps({"credentials_path": str(tenant_credentials)})])
        checked("tenant_http", [bench, "--site", site, "execute", "erp_basic.tests.test_http_tenant.run",
                                "--kwargs", json.dumps({"base_url": f"http://127.0.0.1:{runtime.ports['http']}",
                                                         "peer_host": peer_site,
                                                         "peer_credentials_path": str(tenant_credentials)})])
        tenant_credentials.unlink()
        if browser_tests:
            browser_result = root / "reports/browser-live.json"
            browser_result.unlink(missing_ok=True)
            checked("browser_prepare", [bench, "--site", site, "execute", "erp_basic.tests.browser.prepare",
                                        "--kwargs", json.dumps({"credentials_path": str(browser_credentials)})])
            checked("browser_ui", ["node", "--test", REPO / "tests/server-live.test.cjs"], cwd=REPO,
                    extra_env={"ERP_BASIC_LIVE_URL": f"http://{site}:{runtime.ports['http']}",
                               "ERP_BASIC_LIVE_CREDENTIALS": str(browser_credentials),
                               "ERP_BASIC_LIVE_RESULT": str(browser_result),
                               "ERP_BASIC_SCREENSHOTS": str(root / "reports/browser")})
            checked("browser_database", [bench, "--site", site, "execute", "erp_basic.tests.browser.verify",
                                         "--kwargs", json.dumps({"credentials_path": str(browser_credentials),
                                                                  "result_path": str(browser_result)})])
            runtime_module.write_json(marking_browser_credentials, json.loads(browser_credentials.read_text()))
            browser_credentials.unlink()
            report["browser"] = json.loads(browser_result.read_text())
            marking_browser_result = root / "reports/browser-marking-live.json"
            marking_browser_result.unlink(missing_ok=True)
            checked("marking_browser_prepare", [bench, "--site", site, "execute", "erp_basic.tests.marking_browser.prepare",
                                                "--kwargs", json.dumps({"credentials_path": str(marking_browser_credentials)})])
            checked("marking_browser_ui", ["node", "--test", REPO / "tests/server-marking-live.test.cjs"], cwd=REPO,
                    extra_env={"ERP_BASIC_LIVE_URL": f"http://{site}:{runtime.ports['http']}",
                               "ERP_BASIC_LIVE_CREDENTIALS": str(marking_browser_credentials),
                               "ERP_BASIC_LIVE_RESULT": str(marking_browser_result),
                               "ERP_BASIC_SCREENSHOTS": str(root / "reports/browser-marking")})
            checked("marking_browser_database", [bench, "--site", site, "execute", "erp_basic.tests.marking_browser.verify",
                                                 "--kwargs", json.dumps({"credentials_path": str(marking_browser_credentials),
                                                                          "result_path": str(marking_browser_result)})])
            marking_browser_credentials.unlink()
            report["marking_browser"] = json.loads(marking_browser_result.read_text())
        else:
            report["browser"] = {"passed": False, "reason": "Не запрошен --browser-tests; это не результат браузерной проверки"}
            report["marking_browser"] = {"passed": False, "reason": "Не запрошен --browser-tests; это не результат браузерной проверки маркировки"}
        checked("queue", [root / "bench/env/bin/python", HERE / "проверка-очереди.py",
                          "--root", root, "--site", site])
        # Тесты закончились, burst worker остановился; scheduler не запускался.
        restore = load("ci_restore", "проверка-восстановления.py")
        restore_report = restore.run(root, site)
        report.update(passed=True, restore=restore_report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        for path in private_paths:
            path.unlink(missing_ok=True)
        report["duration_seconds"] = round(time.monotonic() - started, 3)
        runtime_module.write_json(root / "reports/server-ci.json", report)
        if not use_running_services:
            runtime.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--site", default="ci.erp-basic.localhost")
    parser.add_argument("--peer-site", default="erp-basic-tenant.localhost")
    parser.add_argument("--browser-tests", action="store_true", help="Обязательный живой Chromium → API → SQL сценарий")
    parser.add_argument("--use-running-services", action="store_true",
                        help="Только для доверенной job queue: использовать уже работающие БД/HTTP, не останавливая их")
    args = parser.parse_args()
    main(args.root.resolve(), args.site, args.peer_site, browser_tests=args.browser_tests,
         use_running_services=args.use_running_services)
