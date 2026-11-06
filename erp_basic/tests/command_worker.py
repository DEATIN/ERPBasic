"""Отдельное соединение СУБД для конкурентных приёмочных испытаний."""

import json
from pathlib import Path
import sys
import time

import frappe


def main(request_path):
    request = json.loads(Path(request_path).read_text())
    frappe.init(site=request["site"], sites_path=request["sites_path"])
    frappe.connect()
    if not frappe.conf.get("allow_tests"):
        raise RuntimeError("Для worker нужен изолированный тестовый Site")
    frappe.set_user(request.get("user", "Administrator"))
    while time.time() < request["start_at"]:
        time.sleep(0.02)
    try:
        from erp_basic.commands import transition

        result = transition(**request["params"])
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
