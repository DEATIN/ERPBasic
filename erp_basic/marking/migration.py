"""Контрольный перенос наблюдений о КМ. Не эмиссия, не ввод в оборот и не начальное складское движение."""

import re

import frappe
from frappe.utils import getdate
from frappe.utils.password import encrypt

from erp_basic.idempotency import OperationConflict, run
from erp_basic.marking import storage as db
from erp_basic.marking.codes import CodeError, parse_code
from erp_basic.marking.service import authorize, profile, structured


def _observation_conflict(existing, row):
    for field in ("warehouse", "batch_no", "expiry_date"):
        if field in row and str(existing.get(field) or "") != str(row[field] or ""):
            return True
    return False


def _plan(company, source_id, profile_id, rows):
    if not isinstance(source_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{3,80}", source_id):
        frappe.throw("Нужен постоянный source_id из 3–80 латинских букв, цифр и ._:-")
    selected = profile(profile_id)
    rows = structured(rows, list, 300000)
    if not 1 <= len(rows) <= 500:
        frappe.throw("Контрольный перенос выполняется порциями от 1 до 500 строк.")
    results, parsed, external_ids, identities = [], [], set(), set()
    for number, row in enumerate(rows, 1):
        item = None
        reason = ""
        if not isinstance(row, dict) or not set(row) <= {"external_id", "raw_code", "item_code", "warehouse", "batch_no", "expiry_date"}:
            results.append({"row": number, "status": "Invalid", "reason": "Неподдержанные поля строки"})
            continue
        source = row.get("external_id")
        if not isinstance(source, str) or not 1 <= len(source) <= 140 or source in external_ids:
            results.append({"row": number, "status": "Invalid", "reason": "Нужен уникальный внешний ID строки"})
            continue
        external_ids.add(source)
        try:
            code = parse_code(row.get("raw_code"), selected["parser_profile"], allow_symbology_identifier=True)
        except CodeError as error:
            results.append({"row": number, "status": "Invalid", "reason": str(error)})
            continue
        item_code = row.get("item_code")
        if isinstance(item_code, str):
            item = frappe.db.get_value("Item", item_code, ["disabled", "is_stock_item"], as_dict=True)
        if not item or item.disabled or not item.is_stock_item:
            reason = "Не найден действующий складской товар"
        elif not frappe.has_permission("Item", "read", doc=frappe.get_doc("Item", item_code)):
            frappe.throw("Нет доступа к товару переноса.", frappe.PermissionError)
        warehouse = row.get("warehouse")
        if warehouse:
            info = frappe.db.get_value("Warehouse", warehouse, ["company", "is_group", "disabled"], as_dict=True)
            if not info or info.company != company or info.is_group or info.disabled:
                reason = "Недоступный склад организации"
            elif not frappe.has_permission("Warehouse", "read", doc=frappe.get_doc("Warehouse", warehouse)):
                frappe.throw("Нет доступа к складу переноса.", frappe.PermissionError)
        if row.get("batch_no") and (not isinstance(row["batch_no"], str) or len(row["batch_no"]) > 140):
            reason = "Некорректная партия"
        if row.get("expiry_date"):
            try:
                if not isinstance(row["expiry_date"], str) or str(getdate(row["expiry_date"])) != row["expiry_date"]:
                    reason = "Дата годности должна быть YYYY-MM-DD"
            except (ValueError, TypeError):
                reason = "Некорректная дата годности"
        if code.identity in identities:
            reason = "Один экземпляр повторён в исходной порции"
        identities.add(code.identity)
        existing = frappe.db.get_value(db.PREFIX + "Code", code.identity,
                                        ["company", "item_code", "raw_fingerprint", "warehouse", "batch_no", "expiry_date"], as_dict=True)
        if existing and (existing.company != company or existing.item_code != item_code or
                         existing.raw_fingerprint != db.identity(code.normalized)):
            reason = "Код уже зарегистрирован с несовместимыми реквизитами; требуется сверка"
        elif existing and _observation_conflict(existing, row):
            reason = "Склад, партия или срок годности отличаются от реестра; нужна явная сверка источников"
        mapping = db.identity(company, source_id, source)
        digest = db.identity(row, profile_id)
        previous = frappe.db.get_value(db.PREFIX + "Import", mapping, "source_hash")
        if previous and previous != digest:
            reason = "Внешний ID уже перенесён с другим содержимым"
        status = "Invalid" if reason else "Existing" if previous else "Ready"
        results.append({"row": number, "status": status, "code_masked": f"{code.gtin} · …{code.serial[-4:]}", "reason": reason})
        if not reason:
            parsed.append((row, code, mapping, digest))
    report = {"valid": not any(row["status"] == "Invalid" for row in results), "source_id": source_id,
              "plan_hash": db.identity(company, source_id, profile_id, rows), "rows": results,
              "ready": sum(row["status"] == "Ready" for row in results),
              "existing": sum(row["status"] == "Existing" for row in results),
              "native_movements": 0, "external_operations": 0, "regulatory_confirmed": False}
    return report, parsed


def preview(company, source_id, profile_id, rows):
    authorize(company, "configure")
    return _plan(company, source_id, profile_id, rows)[0]


def apply(company, source_id, profile_id, rows, plan_hash, idempotency_key):
    authorize(company, "configure")
    rows = structured(rows, list, 300000)
    _plan(company, source_id, profile_id, rows)  # Текущие права обязательны и до идемпотентного повтора.
    payload = {"source_id": source_id, "profile": profile_id, "rows": rows, "plan_hash": plan_hash}

    def execute():
        report, parsed = _plan(company, source_id, profile_id, rows)
        if plan_hash != report["plan_hash"] or not report["valid"]:
            frappe.throw("План переноса изменился или содержит ошибки. Повторите предварительную проверку.", OperationConflict)
        for row, code, mapping, digest in sorted(parsed, key=lambda value: value[1].identity):
            registered = db.ensure("Code", code.identity, {"company": company, "namespace": code.namespace,
                                  "gtin": code.gtin, "serial": code.serial, "canonical_ki": code.canonical_ki,
                                  "raw_encrypted": encrypt(code.raw), "raw_fingerprint": db.identity(code.normalized),
                                  "parser_profile": code.profile_id, "item_code": row["item_code"],
                                  "warehouse": row.get("warehouse"), "batch_no": row.get("batch_no"),
                                  "expiry_date": row.get("expiry_date"), "local_state": "Quarantine",
                                  "external_state": "Unverified"})
            if registered.company != company or registered.item_code != row["item_code"] or registered.raw_fingerprint != db.identity(code.normalized):
                frappe.throw("Регистрация кода изменилась после предварительной проверки. Повторите сверку.", OperationConflict)
            if _observation_conflict(registered, row):
                frappe.throw("Реквизиты места хранения или партии изменились после предварительной проверки.", OperationConflict)
            record = db.ensure("Import", mapping, {"company": company, "source_id": source_id,
                               "external_id": row["external_id"], "code": code.identity, "source_hash": digest,
                               "profile": profile_id})
            if record.source_hash != digest:
                frappe.throw("Внешний ID перенесён с другим содержимым.", OperationConflict)
        db.history(company, None, "CodeObservationsImported", {"source_id": source_id, "count": len(parsed),
                                                               "plan_hash": plan_hash, "regulatory_confirmed": False})
        return {**report, "applied": True}

    return run(company, "marking.migration.apply", idempotency_key, payload, execute)
