"""ERP-сторона разрешённого локального HTTP испытания с настоящей Clientora/PostgreSQL.

Читает только отдельный 0600 файл синтетического стенда. Вызов:
bench --site <allow_tests site> execute erp_basic.tests.marking_clientora.run
  --kwargs '{"shared_path":"/absolute/runtime/clientora-marking-e2e.json"}'
"""

import json
import os
from pathlib import Path
import tempfile
import uuid
from urllib.parse import urlsplit

import frappe
from frappe.utils import nowdate

from erp_basic import commands, integration
from erp_basic.marking import service
from erp_basic.tests import fixtures


def _deliver_all(connector):
    # Трогаем только созданный этим испытанием connector, а не общую очередь соседних тестов.
    names = frappe.db.sql("SELECT name FROM `tabERP Basic Outbox` WHERE connector=%s AND status='Pending' "
                          "ORDER BY creation,name", (connector.name,), pluck=True)
    messages = []
    for name in names:
        claim = integration._claim_delivery(name)
        assert claim, "Исходящее событие должно быть доступно worker"
        frappe.db.commit()
        envelope = json.loads(claim.payload)
        status, code, detail, result = integration._deliver(connector, envelope)
        integration._update(integration.OUTBOX, name, {"status": status, "last_error": code,
                            "error_message": detail, "result": json.dumps(result), "lease_token": "", "lease_until": None},
                            lease=claim.lease_token)
        frappe.db.commit()
        assert status == "Sent", {"status": status, "code": code, "message": detail}
        messages.append(envelope)
    return messages


def run(shared_path):
    if not frappe.conf.get("allow_tests"):
        raise RuntimeError("Межсерверное испытание разрешено только на allow_tests Site")
    path = Path(shared_path).resolve()
    if path.is_relative_to(Path(__file__).resolve().parents[2]) or path.stat().st_mode & 0o077:
        raise ValueError("Файл стенда должен быть вне репозитория с правами 0600")
    shared = json.loads(path.read_text())
    for field in ("v1_endpoint", "v2_endpoint"):
        address = urlsplit(shared[field])
        if address.scheme != "http" or address.hostname != "127.0.0.1" or address.port != 29010:
            raise ValueError("Этот driver допускает только отдельный loopback listener тестового стенда")
    if shared["company"] != "ERP Basic Marking E2E":
        raise ValueError("Нужна выделенная тестовая Company")
    fixtures.setup()
    frappe.set_user("Administrator")
    previous_local, previous_pause = frappe.conf.get("erp_basic_allow_local_exchange"), frappe.conf.get("erp_basic_disable_external_calls")
    frappe.conf.erp_basic_allow_local_exchange = 1
    frappe.conf.erp_basic_disable_external_calls = 0
    connector = None
    try:
        company = shared["company"]
        if not frappe.db.exists("Company", company):
            frappe.get_doc({"doctype": "Company", "company_name": company, "abbr": "EME2E", "country": "Russian Federation",
                            "default_currency": "RUB", "chart_of_accounts": "Standard", "enable_perpetual_inventory": 1}).insert()
        warehouse = frappe.db.get_value("Warehouse", {"company": company, "warehouse_name": "Stores"}, "name")
        user = fixtures.make_user("marking-e2e.erp-basic@example.invalid", ["ERP Basic Integration", "Sales User"], [company])
        item = fixtures.item("MARK-HTTP-E2E").name
        order = commands.save_document("orders", company, {"customer": "ERP Basic Customer", "transaction_date": nowdate(),
            "delivery_date": nowdate(), "selling_price_list": "ERP Basic Selling",
            "items": [{"item_code": item, "qty": 1, "rate": 125, "warehouse": warehouse}]},
            idempotency_key=uuid.uuid4().hex)["document"]
        connector = frappe.get_doc({"doctype": integration.CONNECTOR, "company": company, "source_system": "clientora",
            "source_tenant": shared["org_id"], "local_tenant": shared["source_tenant"], "integration_user": user,
            "enabled": 1, "outbound_enabled": 1, "endpoint": shared["v1_endpoint"],
            "marking_enabled": 1, "marking_endpoint": shared["v2_endpoint"], "remote_token": shared["incoming_token"]}).insert()
        integration.set_mapping(company, connector.name, "order", shared["order_id"], "Sales Order", order["name"])
        mapping = integration._locked_mapping(integration.mapping_name(connector.name, "order", shared["order_id"]))
        integration._queue_document(connector, mapping, frappe.get_doc("Sales Order", order["name"]))
        frappe.db.commit()
        v1 = _deliver_all(connector)
        assert len(v1) == 1 and v1[0]["schema_version"] == 1
        line = order["items"][0]
        service.configure(company, "Test", "Clientora", {}, uuid.uuid4().hex)
        job = service.create_job(company, "manufacturer", "test-pu-v1", "manufacture", [
            {"item_code": item, "gtin": "09506000134352", "quantity": 1, "reference_line": line["name"]}],
            uuid.uuid4().hex, reference_kind="orders", reference_name=order["name"])["job"]
        raw = "010950600013435221TEST" + uuid.uuid4().hex[:12] + "\x1d91TEST\x1d92NOT-A-SIGNATURE"
        service.scan_code(company, job["name"], raw, 0, uuid.uuid4().hex)
        service.complete_job(company, job["name"], 1, uuid.uuid4().hex)
        frappe.db.commit()
        v2 = _deliver_all(connector)
        assert len(v2) == 2 and all(value["schema_version"] == 2 for value in v2)
        replay = integration._deliver(connector, v2[-1])
        assert replay[0] == "Sent"
        assert frappe.db.get_value("Sales Order", order["name"], "docstatus") == 0
        assert frappe.db.count("Stock Ledger Entry", {"item_code": item}) == 0
        assert frappe.db.count("GL Entry", {"company": company}) == 0
        shared.update(erp_reference=order["name"], job_id=job["name"], site=frappe.local.site,
                      marking_version=3, message_ids=[value["message_id"] for value in v1 + v2],
                      erp_verified=True, http_replay_verified=True, external_regulatory_transmission=False)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            os.chmod(stream.name, 0o600)
            json.dump(shared, stream, ensure_ascii=False)
            staged = stream.name
        os.replace(staged, path)
        return {"passed": True, "transport": "ERP Basic Outbox → real HTTP → Clientora", "v1_messages": 1,
                "v2_messages": 2, "same_uuid_replay": True, "stock_movements": 0, "financial_entries": 0,
                "external_regulatory_transmission": False, "secrets_logged": False}
    finally:
        if connector:
            frappe.set_user("Administrator")
            frappe.db.set_value(integration.CONNECTOR, connector.name, {"outbound_enabled": 0, "marking_enabled": 0})
            frappe.db.commit()
        frappe.conf.erp_basic_allow_local_exchange = previous_local
        frappe.conf.erp_basic_disable_external_calls = previous_pause
