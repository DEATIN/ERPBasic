"""Команды используют обычные права и контроллеры ERPNext в транзакции вызывающего кода."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from copy import deepcopy

import frappe
from frappe.utils import cint, get_datetime

from erp_basic import access, idempotency
from erp_basic.schema import KINDS, ROW_READ_ONLY, fields_for, get_spec


def permission(doctype, action, doc=None):
    if not frappe.has_permission(doctype, action, doc=doc):
        frappe.throw("Недостаточно прав для этой операции.", frappe.PermissionError)


def checked_document(kind, name, company, action="read", *, for_update=False):
    access.require_company(company)
    selected = get_spec(kind)
    doc = frappe.get_doc(selected["doctype"], name, for_update=for_update)
    if doc.meta.has_field("company") and doc.get("company") != company:
        frappe.throw("Документ не принадлежит выбранной организации.", frappe.PermissionError)
    doc.check_permission(action)
    return doc


def capabilities(doctype, doc=None):
    return {action: bool(frappe.has_permission(doctype, action, doc=doc))
            for action in ("read", "create", "write", "submit", "cancel", "export", "import")}


def _value(doc, field):
    value = doc.get(field)
    df = doc.meta.get_field(field)
    if value is not None and df and df.fieldtype in ("Currency", "Float", "Percent"):
        precision = doc.precision(field)
        if precision is None:
            precision = 2 if df.fieldtype == "Currency" else 6
        return format(Decimal(str(value)).quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP), "f")
    return value


def serialize(kind, doc):
    selected = get_spec(kind)
    data = {field: _value(doc, field) for field in fields_for(kind)}
    if "remarks" in selected["fields"] and not doc.meta.has_field("remarks") and doc.meta.has_field("terms"):
        data["remarks"] = doc.get("terms")
    for table, writable in selected["children"].items():
        data[table] = []
        for row in doc.get(table) or []:
            fields = tuple(dict.fromkeys((*writable, *ROW_READ_ONLY)))
            data[table].append({field: _value(row, field) for field in fields
                                if field == "name" or row.meta.has_field(field)})
    return data


def result(kind, company, doc):
    doc.check_permission("read")
    return {"kind": kind, "company": company, "document": serialize(kind, doc),
            "permissions": capabilities(doc.doctype, doc)}


def _validate_fields(values, allowed, meta):
    if not isinstance(values, dict):
        frappe.throw("Документ и строки должны быть объектами JSON.")
    unknown = {key for key in values if key not in allowed or (key != "name" and not meta.has_field(key))}
    if unknown:
        frappe.throw("Нельзя изменять поля: " + ", ".join(sorted(unknown)))
    for key, value in values.items():
        df = meta.get_field(key)
        if isinstance(value, (dict, list)) or (isinstance(value, str) and len(value) > 10000):
            frappe.throw(f"Недопустимое значение поля {key}.")
        if df and df.fieldtype in ("Currency", "Float", "Percent", "Int", "Check") and value not in (None, ""):
            if isinstance(value, bool) and df.fieldtype == "Check":
                values[key] = int(value)
                continue
            try:
                number = Decimal(str(value))
                if not number.is_finite() or abs(number) > Decimal("1000000000000"):
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                frappe.throw(f"Нужно конечное десятичное число в поле {key}.")
    return values


def _apply(kind, doc, payload):
    selected = get_spec(kind)
    allowed = {"name", "company", *selected["fields"], *selected["children"]}
    unknown = set(payload) - allowed
    if unknown:
        frappe.throw("Нельзя изменять поля: " + ", ".join(sorted(unknown)))
    if payload.get("company") and payload["company"] != doc.get("company"):
        frappe.throw("Организацию документа нельзя подменить.", frappe.PermissionError)
    values = {key: value for key, value in payload.items() if key in selected["fields"]}
    allowed_fields = list(selected["fields"])
    if "remarks" in values and not doc.meta.has_field("remarks") and doc.meta.has_field("terms"):
        values["terms"] = values.pop("remarks")
        allowed_fields.append("terms")
    doc.update(_validate_fields(values, allowed_fields, doc.meta))
    for table, writable in selected["children"].items():
        if table not in payload:
            continue
        rows = payload[table]
        if not isinstance(rows, list) or len(rows) > 500:
            frappe.throw("Таблица должна содержать не более 500 строк.")
        child_type = doc.meta.get_field(table).options
        child_meta = frappe.get_meta(child_type)
        existing = {row.name: row for row in doc.get(table) or []}
        seen = set()
        merged = []
        for row in rows:
            _validate_fields(row, writable, child_meta)
            if row.get("name") and (row["name"] not in existing or row["name"] in seen):
                frappe.throw("Строка не принадлежит редактируемому документу.", frappe.PermissionError)
            seen.add(row.get("name"))
            # Сохраняем native связи, налоги и измерения, не показанные упрощённой формой.
            preserved = existing[row["name"]].as_dict() if row.get("name") else {}
            values = {**preserved, **row}
            if kind == "receipts" and "qty" in row:
                # Штатная buying validation пересчитает Received = Accepted + Rejected.
                values["received_qty"] = 0
            merged.append(values)
        doc.set(table, merged)


def _check_version(doc, expected_modified):
    if not expected_modified or get_datetime(doc.modified) != get_datetime(expected_modified):
        frappe.throw("Документ уже изменён. Обновите данные перед сохранением.", idempotency.OperationConflict)


def _refresh_permissions(response, kind, company, action):
    # Даже сохранённый ответ не обходит актуальные членство, роль или права на объект.
    doc = checked_document(kind, response["document"]["name"], company, action)
    doc.check_permission("read")
    response["permissions"] = capabilities(doc.doctype, doc)
    return response


def save_document(kind, company, document, expected_modified=None, idempotency_key=None):
    access.require_company(company)
    selected = get_spec(kind)
    if isinstance(document, str):
        document = frappe.parse_json(document)
    if not isinstance(document, dict):
        frappe.throw("Нужен объект документа.")
    document = deepcopy(document)
    action = "write" if document.get("name") else "create"
    if action == "write":
        checked_document(kind, document["name"], company, "write")
    else:
        permission(selected["doctype"], "create")

    def execute():
        if action == "write":
            doc = checked_document(kind, document["name"], company, "write", for_update=True)
            _check_version(doc, expected_modified)
            if cint(doc.docstatus) != 0:
                frappe.throw("Проведённый или отменённый документ нельзя сохранить как черновик.")
        else:
            doc = frappe.new_doc(selected["doctype"])
            if doc.meta.has_field("company"):
                doc.company = company
            if doc.meta.has_field("currency") and not document.get("currency"):
                # Новый документ не наследует валюту глобальной компании другого контура.
                doc.currency = frappe.db.get_value("Company", company, "default_currency")
                if doc.meta.has_field("conversion_rate"):
                    doc.conversion_rate = 1
        _apply(kind, doc, document)
        # JSON использует десятичные строки; штатные контроллеры ожидают native числовые типы.
        for row in [doc, *doc.get_all_children()]:
            row._fix_numeric_types()
        from erp_basic.guards import validate_company_links
        validate_company_links(doc)
        if kind == "quotes" and not doc.quotation_to:
            doc.quotation_to = "Customer"
        if action == "write":
            doc.save()
        else:
            doc.insert()
        return result(kind, company, doc)

    response = idempotency.run(company, "save:" + kind, idempotency_key,
                               {"document": document, "expected_modified": expected_modified}, execute)
    return _refresh_permissions(response, kind, company, "write" if action == "write" else "read")


def transition(kind, name, company, expected_modified, idempotency_key, action):
    if action not in ("submit", "cancel") or get_spec(kind)["master"]:
        frappe.throw("Для справочника проведение и отмена не предусмотрены.")
    checked_document(kind, name, company, action)

    def execute():
        doc = checked_document(kind, name, company, action, for_update=True)
        _check_version(doc, expected_modified)
        expected_status = 0 if action == "submit" else 1
        if cint(doc.docstatus) != expected_status:
            frappe.throw("Документ уже обработан или имеет недопустимое состояние.", idempotency.OperationConflict)
        getattr(doc, action)()
        return result(kind, company, doc)

    response = idempotency.run(company, action + ":" + kind, idempotency_key,
                               {"name": name, "expected_modified": expected_modified}, execute)
    return _refresh_permissions(response, kind, company, action)


MAPPERS = {
    ("quotes", "orders"): "erpnext.selling.doctype.quotation.quotation.make_sales_order",
    ("orders", "deliveries"): "erpnext.selling.doctype.sales_order.sales_order.make_delivery_note",
    ("orders", "invoices"): "erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
    ("purchaseOrders", "receipts"): "erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt",
    ("purchaseOrders", "purchaseInvoices"): "erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_invoice",
    ("deliveries", "invoices"): "erpnext.stock.doctype.delivery_note.delivery_note.make_sales_invoice",
    ("receipts", "purchaseInvoices"): "erpnext.stock.doctype.purchase_receipt.purchase_receipt.make_purchase_invoice",
}


def create_related(kind, name, target_kind, company, expected_modified, idempotency_key):
    checked_document(kind, name, company)
    selected = get_spec(target_kind)
    permission(selected["doctype"], "create")
    if (kind, target_kind) not in MAPPERS:
        frappe.throw("Это преобразование документов не поддерживается.")

    def execute():
        source = checked_document(kind, name, company, for_update=True)
        _check_version(source, expected_modified)
        if source.docstatus != 1:
            frappe.throw("Документ-основание должен быть проведён.")
        doc = frappe.get_attr(MAPPERS[(kind, target_kind)])(name)
        if target_kind in ("invoices", "purchaseInvoices"):
            doc.update_stock = 0
        doc.insert()
        return result(target_kind, company, doc)

    response = idempotency.run(company, f"related:{kind}:{target_kind}", idempotency_key,
                               {"name": name, "expected_modified": expected_modified}, execute)
    return _refresh_permissions(response, target_kind, company, "read")


def create_return(kind, name, company, expected_modified, idempotency_key):
    if kind not in ("deliveries", "receipts", "invoices", "purchaseInvoices"):
        frappe.throw("Возврат доступен для отгрузки, приёмки или счёта.")
    selected = get_spec(kind)
    checked_document(kind, name, company)
    permission(selected["doctype"], "create")

    def execute():
        from erpnext.controllers.sales_and_purchase_return import make_return_doc

        source = checked_document(kind, name, company, for_update=True)
        _check_version(source, expected_modified)
        if source.docstatus != 1 or source.get("is_return"):
            frappe.throw("Нужен исходный проведённый документ.")
        doc = make_return_doc(source.doctype, source.name)
        doc.insert()
        return result(kind, company, doc)

    response = idempotency.run(company, "return:" + kind, idempotency_key,
                               {"name": name, "expected_modified": expected_modified}, execute)
    return _refresh_permissions(response, kind, company, "read")
