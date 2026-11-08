"""Компактная проекция подготовки маркировки в Clientora. Не команда оплаты, склада или чека."""

from collections import Counter
from datetime import datetime, timezone
import uuid

import frappe

from erp_basic import integration
from erp_basic.idempotency import canonical_json, run
from erp_basic.marking import storage as db
from erp_basic.marking.service import authorize, _job, profile, readiness_for, can_read_job


def summary(job):
    selected = profile(job.profile)
    return {"job_id": job.name, "job_version": job.revision + 1, "role": job.role, "operation": job.operation,
            "status": job.status, "profile_id": job.profile, "profile_version": selected["version"],
            "mode": selected["mode"], "test_only": True, "environment": job.environment,
            "expected_quantity": job.expected_quantity, "accepted_count": job.accepted_count,
            "rejected_count": job.rejected_count, "fiscal_owner": job.fiscal_owner or "", "sale_id": job.sale_id or "",
            "external_state": "Unverified", "external_checked_at": None, "external_source": None,
            "blockers": [row["code"] for row in readiness_for(job.company, job.profile)["blockers"]],
            "lines": [{key: row.get(key) for key in ("reference_line", "item_code", "gtin", "quantity", "accepted_quantity")}
                      for row in db.unpack(job.lines, [])]}


def queue_for_job(name):
    """Вызывается в той же транзакции, что создание/переход Job; HTTP выполняет существующий outbox."""
    if frappe.flags.in_install or frappe.flags.in_migrate:
        return []
    job = frappe.db.get_value(db.PREFIX + "Job", name, "*", as_dict=True)
    # Первая версия: только явно связанный Sales Order с существующим соответствием CRM.
    if not job or job.reference_doctype != "Sales Order" or not job.reference_name:
        return []
    mappings = frappe.db.sql("SELECT m.name,m.connector,m.external_id,m.erp_name FROM `tabERP Basic External Mapping` m "
                             "JOIN `tabERP Basic Connector` c ON c.name=m.connector WHERE m.company=%s "
                             "AND m.external_type='order' AND m.erp_doctype='Sales Order' AND m.erp_name=%s "
                             "AND c.enabled=1 AND c.marking_enabled=1 ORDER BY m.name",
                             (job.company, job.reference_name), as_dict=True)
    mapping_counts = Counter(mapping.connector for mapping in mappings)
    blocked_connectors = set()
    queued = []
    for mapping in mappings:
        if mapping_counts[mapping.connector] != 1:
            if mapping.connector not in blocked_connectors:
                db.history(job.company, job.name, "ProjectionBlocked", {
                    "reason": "ambiguous_order_mapping", "connector": mapping.connector,
                    "mapping_count": mapping_counts[mapping.connector], "job_revision": job.revision,
                })
                blocked_connectors.add(mapping.connector)
            continue
        connector = frappe.get_doc(integration.CONNECTOR, mapping.connector)
        event_key = db.identity("marking.summary_changed.v2", connector.name, job.name, job.revision)
        message_id = str(uuid.uuid5(integration.NAMESPACE, event_key))
        name = integration._message_name(connector.name, message_id)
        if frappe.db.exists(integration.OUTBOX, name):
            queued.append(name)
            continue
        payload = {"clientora_order_id": mapping.external_id, "company": job.company,
                   "erp_doctype": "Sales Order", "erp_name": job.reference_name, "sales_order": job.reference_name,
                   "marking": summary(job)}
        envelope = {"schema_version": 2, "message_id": message_id, "source": "erp_basic",
                    "source_tenant": connector.local_tenant, "event_type": "marking.summary_changed",
                    "object_type": "order", "object_id": mapping.external_id, "object_version": job.revision + 1,
                    "occurred_at": datetime.now(timezone.utc).isoformat(), "correlation_id": str(uuid.uuid4()),
                    "causation_id": None, "payload": payload}
        integration._insert(integration.OUTBOX, name, {
            "company": job.company, "connector": connector.name, "message_id": message_id,
            "object_type": "order", "object_id": mapping.external_id, "object_version": job.revision + 1,
            "payload_hash": integration._digest(envelope), "payload": canonical_json(envelope), "status": "Pending",
            "event_key": event_key, "attempts": 0, "cycle_attempts": 0, "retry_count": 0,
            "next_attempt_at": frappe.utils.now_datetime(), "correlation_id": envelope["correlation_id"],
            "erp_doctype": "Sales Order", "erp_name": job.reference_name,
        })
        queued.append(name)
    return queued


def queue_current(company, name, idempotency_key):
    authorize(company, "configure")
    _job(company, name)

    def execute():
        job = _job(company, name, locked=True)
        queued = queue_for_job(job.name)
        return {"queued": queued, "summary": summary(job), "delivered": False}

    return run(company, "marking.queue_projection", idempotency_key, {"name": name}, execute)


def outgoing_access(connector, envelope):
    payload = envelope.get("payload", {})
    data = payload.get("marking", {})
    if (not connector.get("marking_enabled") or not connector.get("marking_endpoint")
            or envelope.get("schema_version") != 2 or envelope.get("event_type") != "marking.summary_changed"
            or envelope.get("object_type") != "order" or payload.get("erp_doctype") != "Sales Order"
            or payload.get("sales_order") != payload.get("erp_name")
            or payload.get("clientora_order_id") != envelope.get("object_id")):
        return False
    job = frappe.db.get_value(db.PREFIX + "Job", data.get("job_id"), "*", as_dict=True)
    if not job or job.company != connector.company or job.reference_doctype != "Sales Order" or job.reference_name != payload.get("erp_name"):
        return False
    mapping = frappe.db.get_value(integration.MAPPING,
                                   integration.mapping_name(connector.name, "order", envelope.get("object_id")),
                                   ["company", "erp_doctype", "erp_name"], as_dict=True)
    if not mapping or mapping.company != connector.company or mapping.erp_doctype != "Sales Order" or mapping.erp_name != job.reference_name:
        return False
    # Повторная доставка также закрывается, если после постановки сообщения
    # один Sales Order связали с несколькими заказами CRM этого подключения.
    if frappe.db.count(integration.MAPPING, {"company": connector.company, "connector": connector.name,
                                           "external_type": "order", "erp_doctype": "Sales Order",
                                           "erp_name": job.reference_name}) != 1:
        return False
    return can_read_job(job, user=connector.integration_user)
