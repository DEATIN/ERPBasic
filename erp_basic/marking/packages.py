"""Внутренние упаковки со строгим составом. Не являются подтверждённой агрегацией ГИС МТ."""

import frappe

from erp_basic.idempotency import OperationConflict, run
from erp_basic.marking import storage as db
from erp_basic.marking.service import authorize, _job, _revision, integer, paging, profile


def _company_lock(company):
    # Все изменения дерева Company сериализованы, в том числе вложение двух новых упаковок друг в друга.
    frappe.db.sql("SELECT name FROM `tabCompany` WHERE name=%s FOR UPDATE", (company,))


def _mutable_job(company, name):
    job = _job(company, name, locked=True)
    if job.status not in {"Draft", "Prepared"}:
        frappe.throw("Задание упаковки закрыто.")
    if frappe.db.exists(db.PREFIX + "Operation", {"job": name, "state": ["!=", "Rejected"]}):
        frappe.throw("Состав зафиксирован внешней операцией. Сначала завершите её сверку.", OperationConflict)
    return job


def _package(company, name):
    value = db.lock("Package", name)
    if not value or value.company != company:
        frappe.throw("Упаковка недоступна для этой организации.", frappe.PermissionError)
    _job(company, value.job)
    return value


def _ancestors(company, value):
    chain, seen = [], {value.name}
    parent = value.parent_package
    while parent:
        if parent in seen or len(chain) >= 32:
            frappe.throw("Цикл или слишком глубокое дерево упаковок.", OperationConflict)
        seen.add(parent)
        row = _package(company, parent)
        chain.append(row)
        parent = row.parent_package
    return chain


def get_package(company, name, start=0, page_length=25):
    authorize(company)
    start, page_length = paging(start, page_length)
    value = frappe.db.get_value(db.PREFIX + "Package", name, "*", as_dict=True)
    if not value or value.company != company:
        frappe.throw("Упаковка недоступна для этой организации.", frappe.PermissionError)
    _job(company, value.job)
    # По отдельности ограничены и дочерние упаковки, и экземпляры.
    codes = frappe.db.sql("SELECT name,gtin FROM `tabERP Basic Marking Code` WHERE company=%s AND package=%s "
                          "ORDER BY name LIMIT %s OFFSET %s", (company, name, page_length + 1, start), as_dict=True)
    children = frappe.db.sql("SELECT name,label,status,revision FROM `tabERP Basic Marking Package` "
                             "WHERE company=%s AND parent_package=%s ORDER BY name LIMIT %s OFFSET %s",
                             (company, name, page_length + 1, start), as_dict=True)
    return {"package": {key: value.get(key) for key in ("name", "label", "status", "revision", "parent_package", "job")},
            "codes": codes[:page_length], "children": children[:page_length], "start": start,
            "page_length": page_length, "has_more": len(codes) > page_length or len(children) > page_length,
            "namespace": "erp-basic-internal-package-v1", "regulatory_confirmed": False}


def create_package(company, job, label, expected_revision, idempotency_key):
    authorize(company, "write")
    _job(company, job)
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
        frappe.throw("Укажите внутреннюю этикетку длиной до 80 символов.")
    payload = {"job": job, "label": label, "expected_revision": expected_revision}

    def execute():
        _company_lock(company)
        value = _mutable_job(company, job)
        _revision(value, expected_revision)
        if value.status not in {"Draft", "Prepared"} or not profile(value.profile)["capabilities"]["aggregation"]:
            frappe.throw("Задание не допускает внутреннюю упаковку экземпляров.")
        package = db.insert("Package", {"company": company, "job": job, "label": label, "revision": 0, "status": "Open"})
        db.update("Job", job, {"revision": value.revision + 1})
        db.history(company, job, "InternalPackageCreated", {"package": package.name})
        return get_package(company, package.name)

    return run(company, "marking.create_package", idempotency_key, payload, execute)


def change_member(company, name, member_type, member_name, action, expected_revision, reason, idempotency_key):
    authorize(company, "write")
    get_package(company, name)
    if member_type not in {"code", "package"} or action not in {"add", "remove"}:
        frappe.throw("Неизвестное изменение состава упаковки.")
    if not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 240:
        frappe.throw("Укажите причину упаковки, вскрытия или переупаковки.")
    payload = {"name": name, "member_type": member_type, "member_name": member_name, "action": action,
               "expected_revision": expected_revision, "reason": reason}

    def execute():
        _company_lock(company)
        value = _package(company, name)
        if value.revision != integer(expected_revision):
            frappe.throw("Состав упаковки изменился. Обновите данные.", OperationConflict)
        if value.status != "Open" or value.parent_package:
            frappe.throw("Сначала откройте упаковку и извлеките её из внешней упаковки.")
        job = _mutable_job(company, value.job)
        if member_type == "code":
            member = db.lock("Code", member_name)
            if not member or member.company != company or member.reserved_job != value.job:
                frappe.throw("Экземпляр должен быть принят и зарезервирован этим заданием.", frappe.PermissionError)
            parent = member.package
        else:
            member = _package(company, member_name)
            if member.name == name or member.job != value.job or member.status == "Disassembled":
                frappe.throw("Упаковки должны относиться к одному заданию и не совпадать.")
            chain = _ancestors(company, value)
            if member.name in {row.name for row in chain}:
                frappe.throw("Нельзя создать цикл упаковок.", OperationConflict)
            # Проверяется и потомок: размещение A в B, где B уже находится внутри A.
            if name in {row.name for row in _ancestors(company, member)} and action == "add":
                frappe.throw("Упаковка уже входит в это дерево.", OperationConflict)
            parent = member.parent_package
        if action == "add" and parent:
            frappe.throw("У экземпляра или упаковки уже есть активный родитель.", OperationConflict)
        if action == "remove" and parent != name:
            frappe.throw("Элемент не входит в эту упаковку.", OperationConflict)
        db.update("Code" if member_type == "code" else "Package", member.name,
                  {"package" if member_type == "code" else "parent_package": name if action == "add" else None})
        db.update("Package", name, {"revision": value.revision + 1})
        db.update("Job", value.job, {"revision": job.revision + 1})
        db.history(company, value.job, "PackageCompositionChanged", {**payload, "regulatory_confirmed": False})
        return get_package(company, name)

    return run(company, "marking.change_package_member", idempotency_key, payload, execute)


def change_state(company, name, status, expected_revision, reason, idempotency_key):
    authorize(company, "write")
    get_package(company, name)
    if status not in {"Open", "Sealed", "Disassembled"} or not isinstance(reason, str) or not 3 <= len(reason) <= 240:
        frappe.throw("Укажите допустимое состояние и причину изменения.")
    payload = {"name": name, "status": status, "expected_revision": expected_revision, "reason": reason}

    def execute():
        _company_lock(company)
        value = _package(company, name)
        job = _mutable_job(company, value.job)
        if value.revision != integer(expected_revision):
            frappe.throw("Состав упаковки изменился.", OperationConflict)
        if value.parent_package or value.status == "Disassembled":
            frappe.throw("Сначала извлеките упаковку из внешней; разобранная упаковка не переиспользуется.")
        has_codes = frappe.db.exists(db.PREFIX + "Code", {"package": name})
        has_children = frappe.db.exists(db.PREFIX + "Package", {"parent_package": name})
        if status == "Sealed" and not (has_codes or has_children):
            frappe.throw("Пустую упаковку нельзя закрыть.")
        if status == "Disassembled" and (has_codes or has_children):
            frappe.throw("Для расформирования сначала извлеките все экземпляры и вложенные упаковки.")
        db.update("Package", name, {"status": status, "revision": value.revision + 1})
        db.update("Job", value.job, {"revision": job.revision + 1})
        db.history(company, value.job, "PackageStateChanged", payload)
        return get_package(company, name)

    return run(company, "marking.change_package_state", idempotency_key, payload, execute)
