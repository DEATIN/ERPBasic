"""Опциональный контроль маркированных товаров на native проведении ERPNext."""

import frappe

from erp_basic.idempotency import OperationConflict
from erp_basic.marking import storage as db
from erp_basic.marking.service import reference_snapshot, profile, validate_enrollment


def _moves_stock(doc):
    return doc.doctype in {"Delivery Note", "Purchase Receipt", "Stock Entry", "Stock Reconciliation"} or (
        doc.doctype in {"Sales Invoice", "Purchase Invoice"} and doc.get("update_stock"))


def _outgoing(doc):
    if doc.doctype in {"Delivery Note", "Sales Invoice"}:
        return not doc.get("is_return")
    if doc.doctype in {"Purchase Receipt", "Purchase Invoice"}:
        return bool(doc.get("is_return"))
    if doc.doctype == "Stock Entry":
        return any(row.get("s_warehouse") for row in doc.get("items", []))
    return doc.doctype == "Stock Reconciliation"


def before_submit(doc, event=None):
    if frappe.flags.in_install or frappe.flags.in_migrate or not _moves_stock(doc):
        return
    # Неподключённые товары продолжают пользоваться существующим ERPNext без новых ограничений.
    items = {row.item_code for row in doc.get("items", []) if row.get("item_code")}
    if not items:
        return
    enrolled = frappe.get_all(db.PREFIX + "Product", filters={"company": doc.company, "enabled": 1,
                              "enforcement": "Enforce", "item_code": ["in", sorted(items)]},
                              fields=["item_code", "profile"])
    controlled_items = {row.item_code for row in enrolled}
    outgoing = _outgoing(doc)
    if doc.doctype == "Stock Entry":
        outgoing = any(row.get("s_warehouse") and row.item_code in controlled_items for row in doc.get("items", []))
    if enrolled and outgoing:
        frappe.throw("Расход маркированного товара заблокирован: промышленный профиль и подтверждённый адаптер ещё не внедрены. "
                     "Локальная подготовка и успешный симулятор не разрешают отгрузку или продажу.")
    jobs = frappe.db.sql("SELECT * FROM `tabERP Basic Marking Job` WHERE company=%s AND reference_doctype=%s "
                         "AND reference_name=%s AND status IN ('Draft','Prepared') ORDER BY name FOR UPDATE",
                         (doc.company, doc.doctype, doc.name), as_dict=True)
    claimed = set()
    for job in jobs:
        if job.status != "Prepared":
            frappe.throw("Завершите или отмените открытое маркировочное задание до проведения его основания.", OperationConflict)
        validate_enrollment(doc.company, job.profile, db.unpack(job.lines, []))
        if job.reference_snapshot != reference_snapshot(doc):
            frappe.throw("Состав документа изменился после подготовки маркировки.", OperationConflict)
        if job.operation not in {"receive", "return", "manufacture"}:
            frappe.throw("Расход с маркировочным заданием требует подтверждённого внешнего допуска.")
        if job.operation == "receive" and not (
                doc.doctype in {"Purchase Receipt", "Purchase Invoice"} and not doc.get("is_return")
                or doc.doctype == "Stock Entry" and doc.get("purpose") == "Material Receipt"):
            frappe.throw("Задание приёмки связывается с положительным поступлением или складским приходом.")
        if job.operation == "manufacture" and not (
                doc.doctype == "Stock Entry" and doc.get("purpose") in {"Manufacture", "Material Receipt"}):
            frappe.throw("Физический выпуск подтверждается штатным производственным или приходным Stock Entry.")
        if job.operation == "return" and not (doc.doctype in {"Delivery Note", "Sales Invoice"}
                                              and doc.get("is_return") and doc.get("return_against")):
            frappe.throw("Для возврата нужен штатный возврат покупателя со ссылкой на исходный расход.")
        bound = {line["reference_line"] for line in db.unpack(job.lines, [])}
        if claimed & bound:
            frappe.throw("На строку документа приходится несколько маркировочных заданий. Нужна сверка состава.", OperationConflict)
        claimed.update(bound)
        if doc.doctype == "Stock Entry":
            if any(row.get("s_warehouse") or not row.get("t_warehouse") for row in doc.items if row.name in bound):
                frappe.throw("Подготовленное задание может подтвердить только приход готовой продукции; расход требует внешнего допуска.")
        elif _outgoing(doc):
            frappe.throw("Подготовка маркировки не разрешает расходное движение.")
        if frappe.db.exists(db.PREFIX + "Operation", {"job": job.name, "state": ["!=", "Rejected"]}):
            frappe.throw("Сначала разберите внешнюю операцию связанного задания; резерв экземпляров сохраняется.")
        if frappe.db.exists(db.PREFIX + "Package", {"job": job.name, "status": ["!=", "Disassembled"]}):
            frappe.throw("Перед учётным приходом разберите внутренние учебные упаковки; промышленная агрегация ещё не подключена.")
        if profile(job.profile)["mode"] == "unit":
            allowed_state = "Dispatched" if job.operation == "return" else "Observed"
            counts = frappe.db.sql("SELECT item_code,gtin,local_state,COUNT(*) AS quantity FROM `tabERP Basic Marking Code` "
                                   "WHERE company=%s AND reserved_job=%s GROUP BY item_code,gtin,local_state",
                                   (doc.company, job.name), as_dict=True)
            actual = {(row.item_code, row.gtin): row.quantity for row in counts if row.local_state == allowed_state}
            expected = {(row["item_code"], row["gtin"]): row["quantity"] for row in db.unpack(job.lines, [])}
            if len(actual) != len(counts) or actual != expected:
                frappe.throw("Резерв или исходное физическое состояние кодов не совпадает с приходом. Нужна сверка.", OperationConflict)


def on_submit(doc, event=None):
    if frappe.flags.in_install or frappe.flags.in_migrate or not _moves_stock(doc):
        return
    jobs = frappe.db.sql("SELECT name,operation,`lines` FROM `tabERP Basic Marking Job` WHERE company=%s AND reference_doctype=%s "
                         "AND reference_name=%s AND status='Prepared' ORDER BY name FOR UPDATE",
                         (doc.company, doc.doctype, doc.name), as_dict=True)
    for job in jobs:
        # Это только факт native проведения: приход физического товара не подтверждает право продажи.
        rows = {row.name: row for row in doc.get("items", [])}
        for line in db.unpack(job.lines, []):
            row = rows[line["reference_line"]]
            warehouse = row.get("warehouse") or row.get("t_warehouse")
            frappe.db.sql("UPDATE `tabERP Basic Marking Code` SET local_state=%s,warehouse=%s,reserved_job=NULL,"
                           "modified=NOW(),modified_by=%s WHERE company=%s AND reserved_job=%s AND item_code=%s AND gtin=%s",
                           ("Quarantine" if job.operation == "return" else "Received", warehouse, frappe.session.user,
                            doc.company, job.name, line["item_code"], line["gtin"]))
        current = db.lock("Job", job.name)
        db.update("Job", job.name, {"status": "Posted", "native_posted": 1, "revision": current.revision + 1})
        db.history(doc.company, job.name, "NativePosted", {"doctype": doc.doctype, "name": doc.name,
                                                          "regulatory_confirmed": False})


def before_cancel(doc, event=None):
    if not _moves_stock(doc):
        return
    if frappe.db.exists(db.PREFIX + "Job", {"company": doc.company, "reference_doctype": doc.doctype,
                                          "reference_name": doc.name, "native_posted": 1}):
        frappe.throw("Документ связан с проведённой маркировочной сверкой. Нужен отдельный документ возврата и проверка кодов; "
                     "отмена не должна стирать прослеживаемость.")
