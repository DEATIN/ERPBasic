"""Транзакционная защита команд. Commit принадлежит запросу или фоновому заданию Frappe."""

import hashlib
import json
import re

import frappe
from frappe.utils import now_datetime


class OperationConflict(frappe.ValidationError):
    http_status_code = 409


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def run(company, operation, key, payload, execute, actor=None):
    """Вызывается только после текущей проверки разрешений на бизнес-операцию."""
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", key):
        frappe.throw("Нужен устойчивый ключ операции длиной 8–128 символов.")
    actor = actor or frappe.session.user
    name = hashlib.sha256(canonical_json([company, actor, operation, key]).encode()).hexdigest()
    digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    timestamp = now_datetime()
    # Узкий внутренний журнал: пользовательские документы сохраняются только через контроллеры.
    # UPSERT + FOR UPDATE сериализуют повторы в разных процессах, а rollback убирает незавершённую запись.
    frappe.db.sql(
        """INSERT INTO `tabERP Basic Operation`
           (name, creation, modified, owner, modified_by, docstatus, idx, company, actor,
            operation, request_hash, state)
           VALUES (%s,%s,%s,%s,%s,0,0,%s,%s,%s,%s,'Processing')
           ON DUPLICATE KEY UPDATE name=name""",
        (name, timestamp, timestamp, actor, actor, company, actor, operation, digest),
    )
    record = frappe.db.sql(
        "SELECT request_hash, state, response FROM `tabERP Basic Operation` WHERE name=%s FOR UPDATE",
        (name,), as_dict=True,
    )[0]
    if record.request_hash != digest:
        frappe.throw("Этот ключ операции уже использован с другим содержимым.", OperationConflict)
    if record.state == "Completed":
        return json.loads(record.response)
    previous = getattr(frappe.local, "erp_basic_request_id", None)
    frappe.local.erp_basic_request_id = name
    try:
        result = execute()
        encoded = canonical_json(result)
        frappe.db.sql(
            """UPDATE `tabERP Basic Operation` SET state='Completed', response=%s, modified=%s
               WHERE name=%s""", (encoded, now_datetime(), name),
        )
        return json.loads(encoded)
    finally:
        frappe.local.erp_basic_request_id = previous
