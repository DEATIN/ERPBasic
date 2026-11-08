"""Изолированный процесс для гонок маркировки; доступен только тестовому Site."""

import json
from pathlib import Path
import sys
import time

import frappe


def main(path):
    request = json.loads(Path(path).read_text())
    frappe.init(site=request["site"], sites_path=request["sites_path"])
    frappe.connect()
    if not frappe.conf.get("allow_tests"):
        raise RuntimeError("Worker требует allow_tests")
    frappe.set_user("Administrator")
    from erp_basic.marking import service, outbox
    operations = {"scan": service.scan_code, "dispatch": outbox.dispatch_one, "reconcile": outbox.reconcile_one}
    if request["operation"] in {"dispatch", "reconcile"}:
        operation = frappe.db.get_value("ERP Basic Marking Operation", request["params"]["name"],
                                        ["provider", "environment"], as_dict=True)
        if not operation or operation.provider != "test-simulator-v1" or operation.environment != "Test":
            raise RuntimeError("Тестовый worker не допускает других адаптеров")
        # Только этот test process, после проверки адаптера без сети. Site config остаётся закрытым.
        frappe.conf.erp_basic_disable_external_calls = 0
    while time.time() < request["start_at"]:
        time.sleep(0.02)
    try:
        result = operations[request["operation"]](**request["params"])
        frappe.db.commit()
        if request.get("drop_response"):
            return
        response = {"ok": True, "result": result}
    except Exception as error:
        frappe.db.rollback()
        response = {"ok": False, "error": type(error).__name__}
    finally:
        frappe.destroy()
    Path(request["output"]).write_text(json.dumps(response, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main(sys.argv[1])
