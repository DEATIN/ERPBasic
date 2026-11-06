"""Контрольный перенос JSON v2: архив истории, справочники и согласованный начальный срез."""

from decimal import Decimal, InvalidOperation
import hashlib
import re

import frappe
from frappe.utils import getdate

from erp_basic import access
from erp_basic.idempotency import OperationConflict, canonical_json


SECTIONS = ("items", "customers", "suppliers", "orders", "quotes", "invoices", "purchaseOrders", "receipts",
            "stockDocs", "payments", "expenses")
DOCUMENTS = {"orders": "Sales Order", "quotes": "Quotation", "purchaseOrders": "Purchase Order"}
UOMS = {"шт.": "Nos", "компл.": "Set", "усл.": "Nos", "час": "Hour"}


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _number(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or abs(number) > Decimal("1000000000000"):
            raise InvalidOperation
        return number
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Нужно конечное десятичное число") from None


def _context(company, warehouse, source_id):
    access.require_administrator()
    access.require_company(company)
    if not isinstance(source_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{3,80}", source_id):
        frappe.throw("Задайте постоянный source_id из 3–80 латинских букв, цифр и ._:-")
    target = frappe.get_doc("Warehouse", warehouse)
    target.check_permission("write")
    if target.company != company or target.is_group:
        frappe.throw("Нужен обычный склад выбранной организации.", frappe.PermissionError)


def _payload(payload):
    payload = frappe.parse_json(payload) if isinstance(payload, str) else payload
    if not isinstance(payload, dict) or len(canonical_json(payload)) > 8_000_000:
        frappe.throw("Нужен JSON-объект размером не более 8 МБ.")
    return payload


@frappe.whitelist(methods=["POST"])
def plan(payload, company, warehouse, source_id, cutoff_date, uom_mapping=None, financial=None):
    """Dry run: только чтение. Исторические движения никогда не суммируются с конечным остатком."""
    _context(company, warehouse, source_id)
    payload = _payload(payload)
    uom_mapping = frappe.parse_json(uom_mapping) if isinstance(uom_mapping, str) else (uom_mapping or UOMS)
    financial = frappe.parse_json(financial) if isinstance(financial, str) else financial
    errors, warnings = [], []
    if not isinstance(uom_mapping, dict):
        frappe.throw("Карта единиц измерения должна быть объектом JSON.")
    if payload.get("schema") != 2:
        errors.append({"path": "schema", "reason": "Поддерживается экспорт erpbasic-v2 со schema=2"})
    if not isinstance(payload.get("company"), str):
        errors.append({"path": "company", "reason": "В исходнике отсутствует название компании"})
    try:
        if not cutoff_date:
            raise ValueError
        cutoff_date = getdate(cutoff_date).isoformat()
    except Exception:
        errors.append({"path": "cutoff_date", "reason": "Нужна дата начального среза"})
    indexes = {}
    totals = {"stock_qty": Decimal(0), "stock_value": Decimal(0)}
    for section in SECTIONS:
        rows = payload.get(section)
        indexes[section] = {}
        if not isinstance(rows, list) or len(rows) > 5000:
            errors.append({"path": section, "reason": "Ожидается массив до 5000 записей"})
            continue
        for index, row in enumerate(rows):
            path = f"{section}[{index}]"
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or len(row["id"]) > 140:
                errors.append({"path": path, "reason": "Нет корректного постоянного id"})
                continue
            if row["id"] in indexes[section]:
                errors.append({"path": path, "reason": "Повторный id внутри раздела"})
            indexes[section][row["id"]] = row
            if section in ("customers", "suppliers") and not row.get("name"):
                errors.append({"path": path + ".name", "reason": "Нет названия контрагента"})
    for item_id, row in indexes["items"].items():
        try:
            if not row.get("name") or row.get("type") not in ("Товар", "Услуга"):
                raise ValueError("Нужны название и тип Товар/Услуга")
            unit = uom_mapping.get(row.get("unit"))
            if not unit or not frappe.db.exists("UOM", unit):
                raise ValueError("Единица не сопоставлена со штатной UOM")
            qty, cost, price = (_number(row.get(field, 0)) for field in ("qty", "cost", "price"))
            if min(qty, cost, price) < 0:
                raise ValueError("Отрицательные остатки и цены не поддерживаются")
            if row["type"] == "Товар":
                totals["stock_qty"] += qty
                totals["stock_value"] += qty * cost
            elif qty:
                raise ValueError("Услуга не должна иметь складского остатка")
        except ValueError as error:
            errors.append({"path": "items." + item_id, "reason": str(error)})
    for section in ("orders", "quotes", "invoices", "purchaseOrders", "receipts", "stockDocs"):
        for external_id, row in indexes[section].items():
            try:
                if _number(row.get("discount", 0)) < 0 or _number(row.get("discount", 0)) > 100:
                    raise ValueError("Скидка должна быть от 0 до 100 процентов")
            except ValueError as error:
                errors.append({"path": section + "." + external_id + ".discount", "reason": str(error)})
            if section != "stockDocs":
                parties = "suppliers" if section in ("purchaseOrders", "receipts") else "customers"
                if row.get("partner") not in indexes[parties]:
                    errors.append({"path": section + "." + external_id, "reason": "Не найден контрагент"})
            if not isinstance(row.get("lines"), list) or not row["lines"]:
                errors.append({"path": section + "." + external_id, "reason": "Нет строк документа"})
                continue
            for index, line in enumerate(row["lines"]):
                try:
                    if line.get("item") not in indexes["items"]:
                        raise ValueError("Не найдена номенклатура")
                    if _number(line.get("qty")) < 0 or _number(line.get("rate", 0)) < 0:
                        raise ValueError("Отрицательные строки требуют отдельной проверки возврата")
                except (ValueError, AttributeError) as error:
                    errors.append({"path": f"{section}.{external_id}.lines[{index}]", "reason": str(error)})
            if row.get("source") and row["source"] not in indexes["orders"] and row["source"] not in indexes["purchaseOrders"]:
                errors.append({"path": section + "." + external_id + ".source", "reason": "Не найден документ-основание"})
    for external_id, row in indexes["payments"].items():
        if row.get("source") not in {**indexes["orders"], **indexes["purchaseOrders"], **indexes["invoices"]}:
            errors.append({"path": "payments." + external_id, "reason": "Не найдено основание оплаты"})
    currency = frappe.db.get_value("Company", company, "default_currency")
    if financial is not None:
        if not isinstance(financial, dict) or financial.get("currency") != currency or financial.get("reviewed") is not True:
            errors.append({"path": "financial", "reason": "Нужен проверенный финансовый срез в валюте компании"})
        elif not isinstance(financial.get("entries"), list) or len(financial["entries"]) > 5000:
            errors.append({"path": "financial.entries", "reason": "Ожидается массив до 5000 начальных балансов"})
        else:
            for index, entry in enumerate(financial["entries"]):
                try:
                    if not isinstance(entry, dict):
                        raise ValueError("Начальный баланс должен быть объектом JSON")
                    kind = entry["kind"]
                    if kind not in ("receivable", "payable", "customer_advance", "supplier_advance", "cash", "bank"):
                        raise ValueError("Неизвестный вид начального баланса")
                    if _number(entry["amount"]) <= 0:
                        raise ValueError("Сумма должна быть положительной")
                    if kind not in ("cash", "bank"):
                        party_kind = "suppliers" if kind in ("payable", "supplier_advance") else "customers"
                        if entry.get("party_id") not in indexes[party_kind]:
                            raise ValueError("Не найден контрагент финансового среза")
                    else:
                        default = "default_cash_account" if kind == "cash" else "default_bank_account"
                        name = entry.get("account") or frappe.db.get_value("Company", company, default)
                        account = frappe.db.get_value("Account", name,
                            ["company", "is_group", "account_type", "account_currency"], as_dict=True) if name else None
                        expected_type = "Cash" if kind == "cash" else "Bank"
                        if not account or account.company != company or account.is_group or account.account_type != expected_type:
                            raise ValueError("Нужен существующий денежный счёт выбранного типа и компании")
                        if account.account_currency != currency:
                            raise ValueError("Начальный срез поддерживает денежные счета только в валюте компании")
                except (ValueError, KeyError, TypeError) as error:
                    errors.append({"path": f"financial.entries[{index}]", "reason": str(error)})
    else:
        warnings.append("Финансовый срез не подтверждён: задолженность и авансы не будут выдуманы из статусов и суммы заказа.")
    warnings.extend([
        "Конечные количества из items размещаются на выбранном складе; исходный JSON не содержит достоверного распределения по складам.",
        "Все исходные документы, состояния, связи и history сохраняются в архиве. Проведённые демонстрационные движения повторно не проводятся.",
        "Нативными черновиками становятся только исходные черновики предложений и заказов; частично исполненные заказы требуют сверки открытых строк.",
        "reserved из демонстрации сохраняется для сверки, но не становится подтверждённым складским резервом без заказов и правил резервирования.",
    ])
    source_hash = _digest(payload)
    run_id = _digest([company, source_id, source_hash, warehouse, cutoff_date, uom_mapping, financial])
    return {"valid": not errors, "run_id": run_id, "source_id": source_id, "source_hash": source_hash,
            "schema": 2, "strategy": "opening", "company": company, "warehouse": warehouse,
            "cutoff_date": cutoff_date, "currency": currency, "errors": errors, "warnings": warnings,
            "counts": {section: len(indexes[section]) for section in SECTIONS},
            "expected": {key: str(value) for key, value in totals.items()},
            "financial_reviewed": isinstance(financial, dict) and financial.get("reviewed") is True,
            "uom_mapping": uom_mapping}


def _mapping_name(company, source_id, section, old_id):
    return _digest([company, source_id, section, old_id])


def _native_name(prefix, section, old_id):
    # Исходный ID сохраняется в mapping. Хэш избегает ограничения 140 знаков,
    # запрещённых символов и коллизий ID, различающихся только регистром в MariaDB.
    return prefix + _digest([section, old_id])


def _archive(report, section, row, native=None):
    name = _mapping_name(report["company"], report["source_id"], section, row["id"])
    doc = frappe.get_doc({"doctype": "ERP Basic Migration Mapping", "company": report["company"],
                          "migration_run": report["run_id"], "source_id": report["source_id"], "entity_kind": section,
                          "external_id": row["id"], "source_hash": _digest(row), "source_payload": canonical_json(row),
                          "target_doctype": native.doctype if native else None, "target_name": native.name if native else None})
    doc.flags.erp_basic_migration_internal = True
    doc.insert(set_name=name)
    return {"source": row["id"], "kind": section, "archive": doc.name,
            "doctype": native.doctype if native else None, "name": native.name if native else None}


@frappe.whitelist(methods=["POST"])
def apply(payload, company, warehouse, source_id, cutoff_date, plan_hash, accept_warnings=False,
          uom_mapping=None, financial=None):
    report = plan(payload, company, warehouse, source_id, cutoff_date, uom_mapping, financial)
    if not report["valid"] or report["run_id"] != plan_hash:
        frappe.throw("Исправьте ошибки dry run и передайте его актуальный run_id.", OperationConflict)
    if not frappe.utils.cint(accept_warnings):
        frappe.throw("Нужно явно принять описанные в dry run ограничения начального среза.")
    payload = _payload(payload)
    financial = frappe.parse_json(financial) if isinstance(financial, str) else financial
    # Общая блокировка импорта одной Company; строки Source ID также имеют уникальный первичный ключ.
    frappe.get_doc("Company", company, for_update=True)
    existing = frappe.db.get_value("ERP Basic Migration Run", report["run_id"], ["state", "report"], as_dict=True)
    if existing and existing.state == "Completed":
        return frappe.parse_json(existing.report)
    if frappe.db.exists("ERP Basic Migration Run", {"company": company, "source_id": source_id}):
        frappe.throw("Этот source_id уже перенесён с другим содержимым. Повторный начальный срез запрещён.", OperationConflict)
    run = frappe.get_doc({"doctype": "ERP Basic Migration Run", "company": company, "source_id": source_id,
                          "source_hash": report["source_hash"], "cutoff_date": report["cutoff_date"],
                          "warehouse": warehouse, "state": "Running", "source_payload": canonical_json(payload),
                          "report": canonical_json(report)})
    run.flags.erp_basic_migration_internal = True
    run.insert(set_name=report["run_id"])
    prefix = "LB-" + _digest([company, source_id])[:10] + "-"
    maps, all_mappings = {}, []
    from frappe.utils.nestedset import get_root_of
    group = "ERP Basic Import"
    for doctype, field, parentfield, parent in (
        ("Customer Group", "customer_group_name", "parent_customer_group", "All Customer Groups"),
        ("Supplier Group", "supplier_group_name", "parent_supplier_group", "All Supplier Groups"),
        ("Item Group", "item_group_name", "parent_item_group", "All Item Groups"),
    ):
        if not frappe.db.exists(doctype, group):
            frappe.get_doc({"doctype": doctype, field: group, parentfield: get_root_of(doctype) or parent, "is_group": 0}).insert()
    for section, doctype, title_field in (("customers", "Customer", "customer_name"), ("suppliers", "Supplier", "supplier_name")):
        maps[section] = {}
        for row in payload[section]:
            values = {"doctype": doctype, title_field: row["name"], "tax_id": row.get("inn", ""),
                      "default_currency": report["currency"]}
            if section == "customers":
                values.update(customer_type="Company", customer_group=group, territory=get_root_of("Territory"))
            else:
                values.update(supplier_type="Company", supplier_group=group)
            doc = frappe.get_doc(values).insert(set_name=_native_name(prefix, section, row["id"]))
            maps[section][row["id"]] = doc.name
            all_mappings.append(_archive(report, section, row, doc))
            _contact_and_address(doc, row)
    price_list = f"ERP Basic {report['currency']} Selling"
    if not frappe.db.exists("Price List", price_list):
        frappe.get_doc({"doctype": "Price List", "price_list_name": price_list, "selling": 1,
                        "currency": report["currency"]}).insert()
    maps["items"] = {}
    stock_rows = []
    for row in payload["items"]:
        doc = frappe.get_doc({"doctype": "Item", "item_code": _native_name(prefix, "items", row["id"]), "item_name": row["name"],
                              "item_group": group, "stock_uom": report["uom_mapping"][row["unit"]],
                              "is_stock_item": int(row["type"] == "Товар"), "is_sales_item": 1, "is_purchase_item": 1,
                              "valuation_method": "Moving Average"}).insert()
        maps["items"][row["id"]] = doc.name
        frappe.get_doc({"doctype": "Item Price", "item_code": doc.name, "price_list": price_list,
                        "price_list_rate": float(_number(row.get("price", 0))), "currency": report["currency"]}).insert()
        all_mappings.append(_archive(report, "items", row, doc))
        if doc.is_stock_item and _number(row.get("qty", 0)):
            stock_rows.append({"item_code": doc.name, "warehouse": warehouse, "qty": float(_number(row["qty"])),
                               "valuation_rate": float(_number(row["cost"])),
                               "allow_zero_valuation_rate": int(_number(row["cost"]) == 0)})
    stock_document = None
    if stock_rows:
        from erpnext.stock.doctype.stock_reconciliation.stock_reconciliation import get_difference_account
        stock_document = frappe.get_doc({"doctype": "Stock Reconciliation", "company": company,
                                         "purpose": "Opening Stock", "posting_date": cutoff_date, "items": stock_rows,
                                         "expense_account": get_difference_account("Opening Stock", company)}).insert()
        stock_document.submit()
    for section in SECTIONS:
        if section in ("items", "customers", "suppliers"):
            continue
        maps[section] = {}
        for row in payload[section]:
            native = None
            if section in DOCUMENTS and row.get("state") == "draft":
                native = _draft(report, section, row, maps, price_list)
                maps[section][row["id"]] = native.name
            all_mappings.append(_archive(report, section, row, native))
    opening = _opening_journal(report, financial, maps) if financial else None
    report.update(state="Completed", mappings=all_mappings,
                  stock_document=stock_document.name if stock_document else None,
                  opening_journal=opening.name if opening else None,
                  accounting_scope="stock_and_reviewed_financial" if financial else "stock_only_financial_pending")
    report["reconciliation"] = _reconcile_stock(report, stock_rows)
    if report["reconciliation"]["differences"]:
        frappe.throw("Сверка начальных остатков не сошлась; вся миграция откатывается.")
    frappe.db.set_value("ERP Basic Migration Run", run.name,
                        {"state": "Completed", "report": canonical_json(report)})
    return report


def _contact_and_address(parent, row):
    links = [{"link_doctype": parent.doctype, "link_name": parent.name}]
    if row.get("contact") or row.get("email") or row.get("phone"):
        contact = frappe.get_doc({"doctype": "Contact", "first_name": row.get("contact") or row["name"], "links": links})
        if row.get("email"):
            contact.append("email_ids", {"email_id": row["email"], "is_primary": 1})
        if row.get("phone"):
            contact.append("phone_nos", {"phone": row["phone"], "is_primary_phone": 1})
        contact.insert()
    if row.get("address"):
        frappe.get_doc({"doctype": "Address", "address_title": row["name"], "address_type": "Billing",
                        "address_line1": row["address"], "city": row.get("city") or "Не указан",
                        "country": "Russian Federation", "links": links}).insert()


def _draft(report, section, source, maps, price_list):
    values = {"doctype": DOCUMENTS[section], "company": report["company"], "currency": report["currency"],
              "conversion_rate": 1, "transaction_date": source.get("date") or report["cutoff_date"],
              "terms": f"Начальный перенос {report['source_id']}; исходный документ {source['id']}",
              "additional_discount_percentage": float(_number(source.get("discount", 0))), "apply_discount_on": "Net Total"}
    if section == "purchaseOrders":
        values.update(supplier=maps["suppliers"][source["partner"]], schedule_date=source.get("delivery") or report["cutoff_date"])
    else:
        values["selling_price_list"] = price_list
        values["customer" if section == "orders" else "party_name"] = maps["customers"][source["partner"]]
        if section == "quotes":
            values["quotation_to"] = "Customer"
            values["valid_till"] = source.get("delivery") or report["cutoff_date"]
        else:
            values["delivery_date"] = source.get("delivery") or report["cutoff_date"]
    values["items"] = [{"item_code": maps["items"][row["item"]], "qty": float(_number(row["qty"])),
                        "rate": float(_number(row["rate"])), "warehouse": report["warehouse"],
                        "delivery_date": source.get("delivery") or report["cutoff_date"],
                        "schedule_date": source.get("delivery") or report["cutoff_date"]} for row in source["lines"]]
    return frappe.get_doc(values).insert()


def _opening_journal(report, financial, maps):
    if not financial.get("entries"):
        return None
    company = frappe.get_doc("Company", report["company"])
    temporary = frappe.db.get_value("Account", {"company": company.name, "account_type": "Temporary", "is_group": 0}, "name")
    if not temporary:
        temporary = frappe.db.get_value("Account", {"company": company.name, "account_name": "Temporary Opening", "is_group": 0}, "name")
    if not temporary:
        frappe.throw("Для начального финансового среза нужен штатный Temporary Opening account.")
    rows = []
    for entry in financial["entries"]:
        kind, amount = entry["kind"], _number(entry["amount"])
        debit = kind in ("receivable", "supplier_advance", "cash", "bank")
        values = {"debit_in_account_currency" if debit else "credit_in_account_currency": float(amount)}
        if kind in ("cash", "bank"):
            values["account"] = entry.get("account") or company.get("default_cash_account" if kind == "cash" else "default_bank_account")
        else:
            supplier = kind in ("payable", "supplier_advance")
            values.update(account=company.default_payable_account if supplier else company.default_receivable_account,
                          party_type="Supplier" if supplier else "Customer",
                          party=maps["suppliers" if supplier else "customers"][entry["party_id"]])
        rows.append(values)
        rows.append({"account": temporary, "credit_in_account_currency" if debit else "debit_in_account_currency": float(amount)})
    journal = frappe.get_doc({"doctype": "Journal Entry", "company": company.name, "voucher_type": "Opening Entry",
                              "is_opening": "Yes", "posting_date": report["cutoff_date"],
                              "user_remark": "Проверенный начальный срез " + report["source_id"], "accounts": rows}).insert()
    journal.submit()
    return journal


def _reconcile_stock(report, rows):
    differences, actual_qty, actual_value = [], Decimal(0), Decimal(0)
    for row in rows:
        actual = frappe.db.get_value("Bin", {"item_code": row["item_code"], "warehouse": report["warehouse"]},
                                     ["actual_qty", "stock_value"], as_dict=True)
        expected_qty = _number(row["qty"])
        expected_value = expected_qty * _number(row["valuation_rate"])
        if not actual or abs(_number(actual.actual_qty) - expected_qty) > Decimal("0.000001") or abs(_number(actual.stock_value) - expected_value) > Decimal("0.01"):
            differences.append(row["item_code"])
        if actual:
            actual_qty += _number(actual.actual_qty)
            actual_value += _number(actual.stock_value)
    return {"actual_stock_qty": str(actual_qty), "actual_stock_value": str(actual_value), "differences": differences}


@frappe.whitelist(methods=["GET"])
def archive(company, mapping_name):
    access.require_administrator()
    access.require_company(company)
    mapping = frappe.get_doc("ERP Basic Migration Mapping", mapping_name)
    mapping.check_permission("read")
    if mapping.company != company:
        frappe.throw("Архив принадлежит другой организации.", frappe.PermissionError)
    return {"source": frappe.parse_json(mapping.source_payload), "target_doctype": mapping.target_doctype,
            "target_name": mapping.target_name, "migration_run": mapping.migration_run}
