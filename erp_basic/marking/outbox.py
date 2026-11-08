"""Долговечная очередь: бизнес-ключ, аренда попытки и обязательная сверка неизвестного результата."""

import uuid

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

from erp_basic.idempotency import OperationConflict, canonical_json, run
from erp_basic.marking import storage as db
from erp_basic.marking.policy import REGULATORY_OPERATIONS
from erp_basic.marking.providers import provider_for, UncertainResult
from erp_basic.marking.service import authorize, _job, setup


def _summary(value):
    return {key: value.get(key) for key in ("name", "job", "operation", "state", "environment", "external_id",
                                          "attempts", "reason", "creation", "modified")}


def enqueue(company, name, operation, simulation, idempotency_key):
    authorize(company, "configure")
    _job(company, name)
    if operation not in REGULATORY_OPERATIONS or simulation not in {"accept", "reject", "unknown", "partial"}:
        frappe.throw("Неизвестный тестовый сценарий операции.")
    if not frappe.conf.get("allow_tests") or setup(company).environment != "Test":
        frappe.throw("Отправка закрыта: нет подтверждённого промышленного адаптера. Симулятор доступен только на тестовом Site.")
    payload = {"name": name, "operation": operation, "simulation": simulation}

    def execute():
        job = _job(company, name, locked=True)
        if job.status != "Prepared" or job.environment != "Test":
            frappe.throw("Тестовая передача требует подготовленного задания тестового контура.")
        snapshot = {"job": name, "revision": job.revision, "profile": job.profile,
                    "quantity": job.accepted_count, "lines": db.unpack(job.lines, []), "test_only": True,
                    "sale_id": job.sale_id, "fiscal_owner": job.fiscal_owner, "operation": operation}
        business_key = db.identity(company, name, operation)
        digest = db.identity(snapshot, simulation)
        record = db.ensure("Operation", business_key, {"company": company, "job": name,
                           "operation": operation, "business_key": business_key, "payload_hash": digest,
                           "payload": canonical_json(snapshot), "provider": "test-simulator-v1", "environment": "Test",
                           "state": "Ready", "attempts": 0, "simulation": simulation})
        if record.payload_hash != digest:
            frappe.throw("Бизнес-операция уже создана с другим снимком. Сначала разберите её результат.", OperationConflict)
        db.history(company, name, "TestOperationQueued", {"operation_id": record.name, "operation": operation})
        return _summary(record)

    return run(company, "marking.enqueue", idempotency_key, payload, execute)


def _provider(value):
    # Перенесённый на production backup не возобновляет даже тестовую отправку.
    if frappe.conf.get("erp_basic_disable_external_calls"):
        raise ValueError("Внешние операции отключены оператором Site.")
    return provider_for(value.provider, allow_tests=bool(frappe.conf.get("allow_tests")),
                        environment=setup(value.company).environment)


def _check_owner(value):
    if not frappe.db.get_value("User", value.owner, "enabled"):
        raise frappe.PermissionError("Инициатор операции отключён.")
    previous = frappe.session.user
    try:
        frappe.set_user(value.owner)
        authorize(value.company, "configure")
        _job(value.company, value.job)
    finally:
        frappe.set_user(previous)


def _finish(name, token, result=None, uncertain=False):
    value = db.lock("Operation", name)
    if value.lease_token != token or value.state != "Dispatching":
        frappe.db.commit()
        return _summary(value)
    if uncertain:
        values = {"state": "Unknown", "reason": "Ответ не подтверждён. Нужна сверка по прежнему бизнес-ключу; резерв сохранён."}
    else:
        if not isinstance(result, dict) or result.get("state") not in {"Accepted", "Rejected", "Partial"}:
            values = {"state": "Unknown", "reason": "Адаптер вернул неподдержанный результат; требуется сверка."}
        else:
            values = {"state": result["state"], "external_id": result.get("external_id"),
                      "result": canonical_json(result), "reason": result.get("reason")}
    db.update("Operation", name, {**values, "lease_token": None, "lease_until": None})
    db.history(value.company, value.job, "TestOperationResult", {"operation_id": name, "state": values["state"],
                                                               "regulatory_confirmed": False})
    # Здесь намеренно нет stock/GL, создания чеков, изменения legal owner или освобождения reserved_job.
    frappe.db.commit()
    return _summary(frappe.db.get_value(db.PREFIX + "Operation", name, "*", as_dict=True))


def dispatch_one(name):
    """Только фоновая точка входа, не whitelisted RPC. Транзакция аренды завершается ДО внешнего вызова."""
    value = db.lock("Operation", name)
    if not value:
        frappe.db.commit()
        return None
    if value.state == "Dispatching" and value.lease_until and get_datetime(value.lease_until) <= now_datetime():
        db.update("Operation", name, {"state": "Unknown", "reason": "Аренда истекла; запрос мог быть принят. Нужна сверка.",
                                      "lease_token": None, "lease_until": None})
        db.history(value.company, value.job, "LeaseExpired", {"operation_id": name, "reservation_released": False})
        frappe.db.commit()
        return _summary(frappe.db.get_value(db.PREFIX + "Operation", name, "*", as_dict=True))
    if value.state != "Ready":
        frappe.db.commit()
        return _summary(value)
    try:
        adapter = _provider(value)
        _check_owner(value)
    except (ValueError, frappe.PermissionError):
        db.update("Operation", name, {"reason": "Отправка приостановлена: проверьте текущий контур, организацию и права инициатора."})
        frappe.db.commit()
        return _summary(value)
    token = str(uuid.uuid4())
    db.update("Operation", name, {"state": "Dispatching", "lease_token": token,
                                  "lease_until": add_to_date(now_datetime(), seconds=60), "attempts": value.attempts + 1})
    frappe.db.commit()
    try:
        result = adapter.send(business_key=value.business_key, payload=db.unpack(value.payload), simulation=value.simulation)
    except (UncertainResult, TimeoutError, ConnectionError):
        return _finish(name, token, uncertain=True)
    except Exception:
        # Ошибка неизвестного происхождения после начала передачи тоже не разрешает повторную отправку.
        _finish(name, token, uncertain=True)
        raise
    return _finish(name, token, result=result)


def reconcile_one(name):
    """Фоновая сверка прежней операции; не создаёт нового платного/фискального запроса."""
    value = db.lock("Operation", name)
    if not value or value.state != "Unknown":
        frappe.db.commit()
        return _summary(value) if value else None
    adapter = _provider(value)
    _check_owner(value)
    token = str(uuid.uuid4())
    db.update("Operation", name, {"state": "Dispatching", "lease_token": token,
                                  "lease_until": add_to_date(now_datetime(), seconds=60)})
    frappe.db.commit()
    try:
        result = adapter.reconcile(business_key=value.business_key, payload=db.unpack(value.payload), simulation=value.simulation)
    except (UncertainResult, TimeoutError, ConnectionError):
        return _finish(name, token, uncertain=True)
    except Exception:
        _finish(name, token, uncertain=True)
        raise
    return _finish(name, token, result=result)


def request_reconciliation(company, name, idempotency_key):
    authorize(company, "configure")
    value = frappe.db.get_value(db.PREFIX + "Operation", name, "*", as_dict=True)
    if not value or value.company != company:
        frappe.throw("Операция недоступна для этой организации.", frappe.PermissionError)
    _job(company, value.job)
    _provider(value)

    def execute():
        current = db.lock("Operation", name)
        if current.state != "Unknown":
            frappe.throw("Сверка доступна для неизвестного результата.")
        frappe.enqueue("erp_basic.marking.outbox.reconcile_one", name=name, queue="short", enqueue_after_commit=True)
        db.history(company, value.job, "ReconciliationRequested", {"operation_id": name})
        return _summary(current)

    return run(company, "marking.request_reconciliation", idempotency_key, {"name": name}, execute)


def dispatch_pending():
    if not frappe.conf.get("allow_tests") or frappe.conf.get("erp_basic_disable_external_calls"):
        return
    names = frappe.db.sql("SELECT name FROM `tabERP Basic Marking Operation` WHERE state='Ready' "
                          "OR (state='Dispatching' AND lease_until<NOW()) ORDER BY creation LIMIT 25", pluck=True)
    for name in names:
        frappe.enqueue("erp_basic.marking.outbox.dispatch_one", name=name, queue="short", deduplicate=True,
                       job_id="marking:" + name)
