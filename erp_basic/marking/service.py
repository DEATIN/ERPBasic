"""Подготовка маркировочных операций без фиктивных регуляторных подтверждений."""

import json
import re
import uuid

import frappe
from frappe.utils.password import encrypt

from erp_basic.access import is_administrator, require_company, require_user
from erp_basic.idempotency import OperationConflict, canonical_json, run
from erp_basic.schema import get_spec
from erp_basic.marking import storage as db
from erp_basic.marking.codes import CodeError, parse_code, validate_gtin
from erp_basic.marking.policy import PolicyError, get_profile, list_profiles, readiness

ROLES = ("manufacturer", "wholesale", "retail")
OPERATIONS = {
    "manufacturer": {"manufacture", "aggregate", "dispatch", "return", "inventory"},
    "wholesale": {"receive", "dispatch", "aggregate", "return", "inventory"},
    "retail": {"receive", "sale", "return", "inventory"},
}
WRITE_ROLES = {"ERP Basic Marking User", "ERP Basic Marking Manager"}
READ_ROLES = WRITE_ROLES | {"ERP Basic Audit Reader", "ERP Basic Observer"}
PASSPORT_FIELDS = {"inn", "kpp", "sites", "warehouses", "stores", "production_sites", "roles",
                   "commodity_groups", "tn_ved", "okpd2", "edi_provider", "signer", "mchd",
                   "crypto_provider", "cash_software", "kkt", "fiscal_storage", "ofd", "ts_piot",
                   "local_module", "os", "scanners", "printers", "label_sizes", "expected_load",
                   "offline_requirements", "test_environment", "rules_sources", "acceptance_evidence"}


def capabilities():
    require_user()
    roles = set(frappe.get_roles())
    admin = is_administrator()
    return {"read": admin or bool(roles & READ_ROLES), "write": admin or bool(roles & WRITE_ROLES),
            "configure": admin or "ERP Basic Marking Manager" in roles}


def authorize(company, action="read"):
    require_company(company)
    if not capabilities()[action]:
        frappe.throw("Нет роли для этой операции маркировки.", frappe.PermissionError)
    # Сервис обмена не становится кассиром/подписантом через добавление другой роли.
    if "ERP Basic Integration" in frappe.get_roles() and not is_administrator():
        frappe.throw("Сервис обмена не выполняет операции маркировки.", frappe.PermissionError)


def permissions():
    caps = capabilities()
    return {"create": caps["write"], "scan": caps["write"], "complete": caps["write"],
            "configure": caps["configure"], "import": caps["write"]}


def integer(value, minimum=0, maximum=1000000):
    if isinstance(value, bool) or not re.fullmatch(r"\d{1,9}", str(value)):
        frappe.throw("Количество и версия должны быть целыми неотрицательными числами.")
    result = int(value)
    if not minimum <= result <= maximum:
        frappe.throw("Значение вне допустимого диапазона.")
    return result


def paging(start=0, page_length=25):
    return integer(start, maximum=10000000), integer(page_length, minimum=1, maximum=100)


def structured(value, expected_type, maximum=65536):
    if isinstance(value, str):
        if len(value.encode()) > maximum:
            frappe.throw("Слишком большой пакет данных; разделите его на части.")
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            frappe.throw("Некорректный JSON.")
    if not isinstance(value, expected_type) or len(canonical_json(value).encode()) > maximum:
        frappe.throw("Неверная структура или слишком большой пакет данных.")
    return value


def profile(profile_id):
    try:
        return get_profile(profile_id)
    except PolicyError as error:
        frappe.throw(str(error))


def setup(company):
    value = frappe.db.get_value(db.PREFIX + "Setup", db.identity(company), "*", as_dict=True)
    value = value or frappe._dict(company=company, environment="Preparation", fiscal_owner="Unconfigured", passport="{}")
    # Даже после переноса backup тестовый контур не включается на рабочем Site.
    if value.environment == "Test" and not frappe.conf.get("allow_tests"):
        value.environment = "Preparation"
    return value


def readiness_for(company, profile_id="test-pu-v1"):
    config = setup(company)
    result = readiness(profile_id)
    return {"ready": False, "mode": config.environment, "external_allowed": False,
            "blockers": result["live_blockers"], "reference_date": result["reference_date"]}


def get_setup(company):
    authorize(company)
    config = setup(company)
    return {"company": company, "environment": config.environment, "fiscal_owner": config.fiscal_owner,
            "passport": db.unpack(config.passport), "readiness": readiness_for(company),
            "permissions": permissions()}


def configure(company, environment, fiscal_owner, passport, idempotency_key):
    authorize(company, "configure")
    if environment not in {"Preparation", "Test"}:
        frappe.throw("Промышленный контур требует подтверждённого профиля и адаптера.")
    if environment == "Test" and not frappe.conf.get("allow_tests"):
        frappe.throw("Тестовый адаптер доступен только на изолированном Site с allow_tests.")
    if fiscal_owner not in {"Unconfigured", "ERPBasic", "Clientora"}:
        frappe.throw("Неизвестный владелец фискального документа.")
    passport = structured(passport, dict, 16384)
    if not set(passport) <= PASSPORT_FIELDS:
        frappe.throw("Паспорт содержит неподдержанные поля. Ключи и пароли в паспорте не хранятся.")
    if any(not isinstance(value, (str, list)) for value in passport.values()):
        frappe.throw("Паспорт принимает текст и списки текста; секреты задаются вне документа.")
    if any(isinstance(value, list) and any(not isinstance(part, str) for part in value)
           for value in passport.values()):
        frappe.throw("Элементы списков паспорта должны быть строками.")
    payload = {"environment": environment, "fiscal_owner": fiscal_owner, "passport": passport}

    def execute():
        db.ensure("Setup", db.identity(company), {"company": company, "environment": "Preparation",
                                                  "fiscal_owner": "Unconfigured", "passport": "{}"})
        db.update("Setup", db.identity(company), {**payload, "passport": canonical_json(passport)})
        db.history(company, None, "ConfigurationChanged", {"environment": environment, "fiscal_owner": fiscal_owner})
        return get_setup(company)

    return run(company, "marking.configure", idempotency_key, payload, execute)


def _job(company, name, locked=False):
    if not isinstance(name, str) or not 1 <= len(name) <= 140:
        frappe.throw("Некорректный идентификатор задания.")
    value = db.lock("Job", name) if locked else frappe.db.get_value(db.PREFIX + "Job", name, "*", as_dict=True)
    if not value or value.company != company:
        frappe.throw("Задание недоступно для этой организации.", frappe.PermissionError)
    if not can_read_job(value):
        frappe.throw("Отозван доступ к основанию или товару задания.", frappe.PermissionError)
    return value


def can_read_job(value, user=None):
    """Права источника заново проверяются и до возврата сохранённого идемпотентного результата."""
    if value.warehouse and not frappe.has_permission("Warehouse", "read", doc=frappe.get_doc("Warehouse", value.warehouse), user=user):
        return False
    if value.reference_doctype:
        if not frappe.db.exists(value.reference_doctype, value.reference_name):
            return False
        reference = frappe.get_doc(value.reference_doctype, value.reference_name)
        if reference.company != value.company or not frappe.has_permission(reference.doctype, "read", doc=reference, user=user):
            return False
    for line in db.unpack(value.lines, []):
        if not frappe.db.exists("Item", line["item_code"]):
            return False
        item = frappe.get_doc("Item", line["item_code"])
        if not frappe.has_permission("Item", "read", doc=item, user=user):
            return False
    return True


def job_summary(value):
    fields = ("name", "company", "role", "profile", "operation", "status", "revision", "reference_doctype",
              "reference_name", "warehouse", "expected_quantity", "accepted_count", "rejected_count",
              "environment", "fiscal_owner", "sale_id", "creation", "modified", "expectations_loaded", "native_posted")
    result = {field: value.get(field) for field in fields}
    result["lines"] = db.unpack(value.lines, [])
    result["mode"] = profile(value.profile)["mode"]
    result["external_state"] = "Unverified"
    return result


def workspace(company, role, start=0, page_length=25):
    authorize(company)
    if role not in ROLES:
        frappe.throw("Неизвестное рабочее место.")
    start, page_length = paging(start, page_length)
    rows = frappe.db.sql("SELECT * FROM `tabERP Basic Marking Job` WHERE company=%s AND role=%s "
                         "ORDER BY creation DESC,name DESC LIMIT %s OFFSET %s",
                         (company, role, page_length + 1, start), as_dict=True)
    environment = setup(company).environment
    return {"company": company, "roles": list(ROLES),
            "profiles": [{**p, "environment": environment} for p in list_profiles()],
            "permissions": permissions(), "readiness": readiness_for(company),
            "jobs": [job_summary(row) for row in rows[:page_length] if can_read_job(row)],
            "start": start, "page_length": page_length, "has_more": len(rows) > page_length}


def _scan_summary(scan):
    return {field: scan.get(field) for field in ("name", "result", "code_masked", "reason", "creation", "sequence")}


def discrepancy(value, start=0, page_length=25):
    if not value.expectations_loaded:
        return {"enabled": False, "missing_count": 0, "extra_count": 0, "missing": []}
    condition = ("e.job=%s AND NOT EXISTS (SELECT 1 FROM `tabERP Basic Marking Scan` s "
                 "WHERE s.job=e.job AND s.code=e.code_identity AND s.result='Accepted')")
    missing_count = frappe.db.sql("SELECT COUNT(*) FROM `tabERP Basic Marking Expected` e WHERE " + condition,
                                  (value.name,))[0][0]
    missing = frappe.db.sql("SELECT e.code_masked,e.gtin FROM `tabERP Basic Marking Expected` e WHERE " + condition +
                            " ORDER BY e.name LIMIT %s OFFSET %s", (value.name, page_length, start), as_dict=True)
    extra_count = frappe.db.sql("SELECT COUNT(*) FROM `tabERP Basic Marking Scan` s WHERE s.job=%s "
                                "AND s.result='Accepted' AND NOT EXISTS (SELECT 1 FROM `tabERP Basic Marking Expected` e "
                                "WHERE e.job=s.job AND e.code_identity=s.code)", (value.name,))[0][0]
    return {"enabled": True, "missing_count": missing_count, "extra_count": extra_count, "missing": missing,
            "start": start, "page_length": page_length, "has_more": missing_count > start + page_length}


def get_job(company, name, start=0, page_length=25):
    authorize(company)
    start, page_length = paging(start, page_length)
    value = _job(company, name)
    scans = frappe.db.sql("SELECT name,result,code_masked,reason,creation,sequence FROM `tabERP Basic Marking Scan` "
                          "WHERE company=%s AND job=%s ORDER BY creation DESC,name DESC LIMIT %s OFFSET %s",
                          (company, name, page_length + 1, start), as_dict=True)
    history = frappe.db.sql("SELECT name,action,actor,creation,details FROM `tabERP Basic Marking History` "
                            "WHERE company=%s AND job=%s ORDER BY creation DESC,name DESC LIMIT 25",
                            (company, name), as_dict=True)
    operations = frappe.db.sql("SELECT name,operation,state,environment,external_id,reason,attempts FROM "
                               "`tabERP Basic Marking Operation` WHERE company=%s AND job=%s "
                               "ORDER BY creation DESC LIMIT 25", (company, name), as_dict=True)
    return {"job": job_summary(value), "scans": scans[:page_length], "history": history,
            "operations": operations, "discrepancy": discrepancy(value), "start": start, "page_length": page_length,
            "has_more": len(scans) > page_length, "permissions": permissions(),
            "readiness": readiness_for(company, value.profile)}


def _revision(value, expected_revision):
    if value.revision != integer(expected_revision):
        frappe.throw("Задание изменилось. Обновите его и продолжите очередь с текущей версией.", OperationConflict)


def _draft(value):
    if value.status != "Draft":
        frappe.throw("Состав можно изменять только в открытом задании.", OperationConflict)


def _lines(value):
    lines = structured(value, list)
    if not 1 <= len(lines) <= 100:
        frappe.throw("Задание должно содержать от 1 до 100 товарных строк.")
    result, seen = [], set()
    for line in lines:
        if not isinstance(line, dict) or set(line) - {"item_code", "gtin", "quantity", "reference_line"}:
            frappe.throw("Неверные поля товарной строки.")
        try:
            gtin = validate_gtin(line.get("gtin"))
        except CodeError as error:
            frappe.throw(str(error))
        if gtin in seen:
            frappe.throw("Один GTIN должен быть в одной строке задания. Разделите партии на отдельные задания.")
        seen.add(gtin)
        item = frappe.get_doc("Item", line.get("item_code"))
        item.check_permission("read")
        if item.disabled or not item.is_stock_item:
            frappe.throw("Для маркировки нужен действующий складской товар.")
        quantity = integer(line.get("quantity"), minimum=1, maximum=100000)
        result.append({"item_code": item.name, "gtin": gtin, "quantity": quantity,
                       "accepted_quantity": 0, "reference_line": line.get("reference_line")})
    if sum(line["quantity"] for line in result) > 100000:
        frappe.throw("В одном задании допускается не более 100 000 единиц.")
    return result


def reference_snapshot(doc):
    return canonical_json({"doctype": doc.doctype, "name": doc.name, "company": doc.company,
                           "is_return": doc.get("is_return"), "update_stock": doc.get("update_stock"),
                           "purpose": doc.get("purpose"), "items": [
                               {field: row.get(field) for field in ("name", "item_code", "qty", "stock_qty", "warehouse",
                                                                   "s_warehouse", "t_warehouse", "batch_no", "serial_no")}
                               for row in doc.get("items", [])]})


def validate_enrollment(company, profile_id, lines):
    for line in lines:
        enrolled = frappe.db.get_value(db.PREFIX + "Product", db.identity(company, line["item_code"]),
                                       ["enabled", "gtin", "profile"], as_dict=True)
        if enrolled and enrolled.enabled and (enrolled.gtin != line["gtin"] or enrolled.profile != profile_id):
            frappe.throw("GTIN или профиль задания не совпадает с подключённой карточкой товара. Проверьте сопоставление.", OperationConflict)


def lock_reference(value):
    if value.reference_doctype:
        # doctype берётся из закрытого серверного набора, не из параметра запроса.
        allowed = {"Sales Order", "Delivery Note", "Sales Invoice", "Purchase Receipt", "Purchase Invoice",
                   "Stock Entry", "Stock Reconciliation", "Purchase Order"}
        if value.reference_doctype not in allowed:
            frappe.throw("Неподдержанное основание маркировки.")
        frappe.db.sql(f"SELECT name FROM `tab{value.reference_doctype}` WHERE name=%s FOR UPDATE", (value.reference_name,))


def require_unposted_stock_reference(doc):
    from erp_basic.marking.guards import _moves_stock
    if _moves_stock(doc) and doc.docstatus != 0:
        frappe.throw("Маркировочное задание создаётся и завершается до проведения складского основания. Для уже проведённого документа нужна отдельная сверка.", OperationConflict)


def _reference(company, kind, name, lines):
    if not kind and not name:
        return None, None
    if not kind or not name or kind not in {"orders", "deliveries", "invoices", "receipts", "purchaseInvoices",
                                           "stockDocs", "inventory", "purchaseOrders"}:
        frappe.throw("Нужны поддержанный тип и документ основания.")
    doctype = get_spec(kind)["doctype"]
    doc = frappe.get_doc(doctype, name)
    if doc.company != company:
        frappe.throw("Основание относится к другой организации.", frappe.PermissionError)
    doc.check_permission("read")
    if doc.docstatus == 2:
        frappe.throw("Основание отменено.")
    require_unposted_stock_reference(doc)
    rows = {row.name: row for row in doc.get("items", [])}
    references = [line.get("reference_line") for line in lines]
    if any(not isinstance(value, str) for value in references) or len(set(references)) != len(references):
        frappe.throw("Каждая строка основания может встречаться в задании только один раз.")
    for line in lines:
        row = rows.get(line.get("reference_line"))
        if not row or row.item_code != line["item_code"] or abs(row.get("stock_qty") or row.qty) != line["quantity"]:
            frappe.throw("Укажите точную строку основания с тем же товаром и количеством в складских единицах.")
    return doctype, reference_snapshot(doc)


def create_job(company, role, profile_id, operation, lines, idempotency_key,
               reference_kind=None, reference_name=None, warehouse=None):
    authorize(company, "write")
    selected = profile(profile_id)
    if role not in OPERATIONS or operation not in OPERATIONS[role]:
        frappe.throw("Операция недоступна в выбранном рабочем месте.")
    if operation == "aggregate" and not selected["capabilities"]["aggregation"]:
        frappe.throw("В количественном профиле агрегация экземпляров не поддерживается.")
    lines = _lines(lines)
    validate_enrollment(company, profile_id, lines)
    if warehouse:
        doc = frappe.get_doc("Warehouse", warehouse)
        if doc.company != company:
            frappe.throw("Склад относится к другой организации.", frappe.PermissionError)
        doc.check_permission("read")
        if doc.is_group or doc.disabled:
            frappe.throw("Нужен действующий склад, а не группа.")
    doctype, snapshot = _reference(company, reference_kind, reference_name, lines)
    payload = {"role": role, "profile": profile_id, "operation": operation, "lines": lines,
               "reference_kind": reference_kind, "reference_name": reference_name, "warehouse": warehouse}

    def execute():
        config = setup(company)
        if doctype:
            # Одна строка native документа не может подтвердить две независимые партии КИ.
            frappe.db.sql(f"SELECT name FROM `tab{doctype}` WHERE name=%s FOR UPDATE", (reference_name,))
            current = frappe.get_doc(doctype, reference_name)
            require_unposted_stock_reference(current)
            if reference_snapshot(current) != snapshot:
                frappe.throw("Основание изменилось во время создания задания.", OperationConflict)
            active = frappe.db.sql("SELECT `lines` FROM `tabERP Basic Marking Job` WHERE company=%s "
                                   "AND reference_doctype=%s AND reference_name=%s AND status!='Cancelled' LIMIT 1001",
                                   (company, doctype, reference_name), as_dict=True)
            claimed = {line["reference_line"] for line in lines}
            if len(active) > 1000 or any(claimed & {line.get("reference_line") for line in db.unpack(row.lines, [])}
                                         for row in active):
                frappe.throw("Строка основания уже связана с активным заданием маркировки.", OperationConflict)
        value = db.insert("Job", {"company": company, "role": role, "profile": profile_id, "operation": operation,
                                 "status": "Draft", "revision": 0, "environment": config.environment,
                                 "reference_doctype": doctype, "reference_name": reference_name,
                                 "reference_snapshot": snapshot, "warehouse": warehouse,
                                 "expected_quantity": sum(line["quantity"] for line in lines),
                                 "accepted_count": 0, "rejected_count": 0, "lines": canonical_json(lines),
                                 "fiscal_owner": config.fiscal_owner if operation in {"sale", "return"} else "",
                                 "sale_id": str(uuid.uuid4()) if operation == "sale" else ""})
        db.history(company, value.name, "Created", {"role": role, "operation": operation, "profile": profile_id,
                                                   "environment": config.environment, "reference": reference_name})
        return get_job(company, value.name)

    return run(company, "marking.create_job", idempotency_key, payload, execute)


def _uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value.lower():
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        frappe.throw("Идентификатор устройства и события должен быть UUID.")
    return value.lower()


def scan_code(company, name, raw_code, expected_revision, idempotency_key,
              device_id=None, event_id=None, sequence=None):
    authorize(company, "write")
    _job(company, name)
    # Ограничение транспортного пакета не является попыткой поправить сырой DataMatrix.
    if not isinstance(raw_code, str) or len(raw_code) > 2048:
        frappe.throw("Слишком большой или неверный результат сканирования.")
    if any(value is not None for value in (device_id, event_id, sequence)):
        device_id, event_id = _uuid(device_id), _uuid(event_id)
        sequence = integer(sequence, minimum=1, maximum=1000000)
    event_name = db.identity(company, frappe.session.user, device_id, event_id) if event_id else None
    semantic_hash = db.identity(name, raw_code, sequence)
    payload = {"name": name, "raw_code": raw_code, "expected_revision": expected_revision,
               "device_id": device_id, "event_id": event_id, "sequence": sequence}

    def execute():
        _job(company, name)  # Проверить область до захвата события устройства.
        if event_name:
            existing = db.ensure("Scan", event_name, {"company": company, "job": name, "actor": frappe.session.user,
                                 "device_id": device_id, "event_id": event_id, "sequence": sequence,
                                 "event_hash": semantic_hash, "result": "Pending"})
            if existing.event_hash != semantic_hash or existing.job != name:
                frappe.throw("Событие устройства уже записано с другим содержимым.", OperationConflict)
            if existing.result != "Pending":
                value = _job(company, name, locked=True)
                return {"job": job_summary(value), "scan": _scan_summary(existing)}
        value = _job(company, name, locked=True)
        _revision(value, expected_revision)
        _draft(value)
        if int(value.accepted_count) + int(value.rejected_count) >= 200000:
            frappe.throw("Достигнут предел событий задания; закройте его и создайте следующее.")
        device_name = None
        if event_name:
            device_name = db.identity(company, frappe.session.user, device_id, name)
            device = db.ensure("Device", device_name, {"company": company, "job": name, "actor": frappe.session.user,
                                                       "device_id": device_id, "last_sequence": 0})
            if sequence != device.last_sequence + 1:
                frappe.throw("Нарушен порядок сканов устройства. Восстановите очередь с первого неподтверждённого события.",
                             OperationConflict)
        lines = db.unpack(value.lines, [])
        validate_enrollment(company, value.profile, lines)
        selected = profile(value.profile)
        result, reason, parsed, line_index, code = "Rejected", "", None, None, None
        try:
            parsed = parse_code(raw_code, selected["parser_profile"], allow_symbology_identifier=True)
        except CodeError as error:
            reason = str(error)
        if parsed:
            line_index = next((index for index, line in enumerate(lines) if line["gtin"] == parsed.gtin), None)
            if line_index is None:
                reason = "GTIN отсутствует в товарных строках задания. Проверьте товар или основание."
            elif selected["mode"] == "quantity":
                reason = "Количественный профиль принимает фактическое количество строки, а не список экземпляров."
            else:
                line = lines[line_index]
                # Без Company в ключе: один экземпляр не может появиться второй активной копией в другом Company.
                if value.operation in {"dispatch", "sale", "aggregate"}:
                    code = db.lock("Code", parsed.identity)
                else:
                    code = db.ensure("Code", parsed.identity, {"company": company, "namespace": parsed.namespace,
                                     "gtin": parsed.gtin, "serial": parsed.serial, "canonical_ki": parsed.canonical_ki,
                                     "raw_encrypted": encrypt(parsed.raw), "parser_profile": selected["parser_profile"],
                                     "raw_fingerprint": db.identity(parsed.normalized),
                                     "item_code": line["item_code"], "local_state": "Observed",
                                     "external_state": "Unverified"})
                if not code or code.company != company:
                    reason = "Код недоступен в реестре этой организации. Нужна сверка поступления или принадлежности."
                    code = None
                elif code.item_code != line["item_code"]:
                    reason = "Код уже связан с другим товаром. Проверьте сопоставление GTIN."
                elif code.raw_fingerprint != db.identity(parsed.normalized):
                    result, reason = "Conflict", "У того же КИ другой полный код маркировки. Нужна проверка исходного кода."
                elif code.warehouse and not frappe.has_permission("Warehouse", "read", doc=frappe.get_doc("Warehouse", code.warehouse)):
                    reason = "Нет доступа к месту хранения экземпляра. Обратитесь к ответственному за склад."
                elif code.reserved_job == name:
                    result, reason = "Duplicate", "Этот экземпляр уже принят в задание; количество не изменилось."
                elif code.reserved_job:
                    result, reason = "Conflict", "Экземпляр зарезервирован другим заданием. Завершите или отмените его."
                elif code.package and value.operation in {"dispatch", "sale", "return"}:
                    result, reason = "Conflict", "Экземпляр находится в упаковке. Сначала зарегистрируйте её вскрытие."
                elif value.operation in {"receive", "manufacture"} and code.local_state != "Observed":
                    result, reason = "Conflict", "Экземпляр уже отражён в учёте или карантине. Повторный приход требует отдельной сверки основания."
                elif value.operation == "return" and code.local_state != "Dispatched":
                    result, reason = "Conflict", "Для возврата экземпляра нужен подтверждённый исходный расход и отдельное основание возврата."
                elif line["accepted_quantity"] >= line["quantity"]:
                    reason = "Количество по строке уже набрано. Проверьте лишний экземпляр."
                elif value.expectations_loaded and not frappe.db.exists(db.PREFIX + "Expected", db.identity(name, parsed.identity)):
                    reason = "Код отсутствует в загруженном составе поставки. Зарегистрируйте расхождение с поставщиком."
                else:
                    db.update("Code", code.name, {"reserved_job": name})
                    result, reason = "Accepted", "Синтаксис и состав проверены; внешнее состояние и продажа не подтверждены."
                    line["accepted_quantity"] += 1
        scan_values = {"company": company, "job": name, "actor": frappe.session.user,
                                  "device_id": device_id, "event_id": event_id, "sequence": sequence,
                                  "event_hash": semantic_hash, "code": code.name if code else None,
                                  "code_masked": f"{parsed.gtin} · …{parsed.serial[-4:]}" if parsed else "Нераспознанный код",
                                  "result": result, "reason": reason, "line_index": line_index,
                                  "request_revision": value.revision}
        if event_name:
            db.update("Scan", event_name, scan_values)
            scan = db.lock("Scan", event_name)
        else:
            scan = db.insert("Scan", scan_values)
        changes = {"revision": value.revision + 1, "lines": canonical_json(lines),
                   "accepted_count": value.accepted_count + int(result == "Accepted"),
                   "rejected_count": value.rejected_count + int(result != "Accepted")}
        db.update("Job", name, changes)
        if device_name:
            db.update("Device", device_name, {"last_sequence": sequence})
        # Scan — неизменяемый первичный журнал каждого события, без дублирования сотен тысяч History.
        return {"job": job_summary(_job(company, name)), "scan": _scan_summary(scan)}

    return run(company, "marking.scan_code", idempotency_key, payload, execute)


def record_quantity(company, name, gtin, quantity, expected_revision, idempotency_key):
    authorize(company, "write")
    _job(company, name)
    quantity = integer(quantity, maximum=100000)
    payload = {"name": name, "gtin": gtin, "quantity": quantity, "expected_revision": expected_revision}

    def execute():
        value = _job(company, name, locked=True)
        _revision(value, expected_revision)
        _draft(value)
        if profile(value.profile)["mode"] != "quantity":
            frappe.throw("Этот профиль требует сканирования экземпляров.")
        lines = db.unpack(value.lines, [])
        validate_enrollment(company, value.profile, lines)
        line = next((row for row in lines if row["gtin"] == gtin), None)
        if not line or quantity > line["quantity"]:
            frappe.throw("Товар отсутствует в задании либо фактическое количество превышает ожидаемое.")
        line["accepted_quantity"] = quantity
        db.update("Job", name, {"revision": value.revision + 1, "lines": canonical_json(lines),
                                "accepted_count": sum(row["accepted_quantity"] for row in lines)})
        db.history(company, name, "QuantityRecorded", {"gtin": gtin, "quantity": quantity})
        return get_job(company, name)

    return run(company, "marking.record_quantity", idempotency_key, payload, execute)


def import_expected_codes(company, name, codes, expected_revision, idempotency_key):
    authorize(company, "write")
    _job(company, name)
    codes = structured(codes, list, 300000)
    if not 1 <= len(codes) <= 1000:
        frappe.throw("Загружайте состав частями от 1 до 1000 кодов.")
    payload = {"name": name, "codes": codes, "expected_revision": expected_revision}

    def execute():
        value = _job(company, name, locked=True)
        _revision(value, expected_revision)
        _draft(value)
        if value.accepted_count or value.rejected_count or profile(value.profile)["mode"] != "unit":
            frappe.throw("Состав экземпляров загружается до начала сканирования поэкземплярного задания.")
        expected = {row["gtin"]: row["quantity"] for row in db.unpack(value.lines, [])}
        existing = dict(frappe.db.sql("SELECT gtin,COUNT(*) FROM `tabERP Basic Marking Expected` WHERE job=%s GROUP BY gtin",
                                      (name,)))
        parsed = []
        for raw in codes:
            try:
                # В ЭДО может передаваться именно КИ, без криптохвоста; формат выбран явно.
                code = parse_code(raw, "gs1-ki-v1", allow_symbology_identifier=False)
            except CodeError as error:
                frappe.throw(str(error))
            if code.gtin not in expected:
                frappe.throw("GTIN состава отсутствует в задании.")
            parsed.append(code)
        added = 0
        for code in parsed:
            key = db.identity(name, code.identity)
            if frappe.db.exists(db.PREFIX + "Expected", key):
                continue
            existing[code.gtin] = existing.get(code.gtin, 0) + 1
            if existing[code.gtin] > expected[code.gtin]:
                frappe.throw("Состав поставки превышает ожидаемое количество строки.")
            db.insert("Expected", {"company": company, "job": name, "code_identity": code.identity,
                                   "gtin": code.gtin, "code_masked": f"{code.gtin} · …{code.serial[-4:]}"}, name=key)
            added += 1
        db.update("Job", name, {"revision": value.revision + 1, "expectations_loaded": 1})
        db.history(company, name, "ExpectedCodesImported", {"added": added, "chunk_size": len(codes)})
        return get_job(company, name)

    return run(company, "marking.import_expected_codes", idempotency_key, payload, execute)


def complete_job(company, name, expected_revision, idempotency_key):
    authorize(company, "write")
    initial = _job(company, name)
    payload = {"name": name, "expected_revision": expected_revision}

    def execute():
        lock_reference(initial)  # Единый порядок: native основание → маркировочное задание.
        value = _job(company, name, locked=True)
        _revision(value, expected_revision)
        _draft(value)
        validate_enrollment(company, value.profile, db.unpack(value.lines, []))
        if any(row["quantity"] != row["accepted_quantity"] for row in db.unpack(value.lines, [])):
            frappe.throw("Ожидаемое и фактическое количество не совпадают. Завершите сверку.")
        if value.expectations_loaded:
            check = discrepancy(value)
            count = frappe.db.count(db.PREFIX + "Expected", {"job": name})
            if check["missing_count"] or check["extra_count"] or count != value.expected_quantity:
                frappe.throw("Состав поставки не совпадает с заданием и принятыми экземплярами.")
        if value.reference_doctype:
            doc = frappe.get_doc(value.reference_doctype, value.reference_name)
            doc.check_permission("read")
            require_unposted_stock_reference(doc)
            if doc.docstatus == 2 or reference_snapshot(doc) != value.reference_snapshot:
                frappe.throw("Основание изменилось. Отмените подготовку и создайте задание из актуального документа.", OperationConflict)
        db.update("Job", name, {"status": "Prepared", "revision": value.revision + 1})
        db.history(company, name, "LocallyPrepared", {"external_confirmed": False, "native_posted": False})
        return get_job(company, name)

    return run(company, "marking.complete_job", idempotency_key, payload, execute)


def cancel_job(company, name, expected_revision, reason, idempotency_key):
    authorize(company, "write")
    _job(company, name)
    if not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 240:
        frappe.throw("Укажите причину отмены длиной 3–240 символов.")
    payload = {"name": name, "expected_revision": expected_revision, "reason": reason}

    def execute():
        value = _job(company, name, locked=True)
        _revision(value, expected_revision)
        if value.status not in {"Draft", "Prepared"}:
            frappe.throw("Отмена доступна для открытого или подготовленного задания.")
        if frappe.db.exists(db.PREFIX + "Operation", {"job": name, "state": ["not in", ["Rejected"]]}):
            frappe.throw("Есть внешняя операция с неотменённым результатом. Сначала завершите сверку; резерв сохраняется.",
                         OperationConflict)
        if frappe.db.exists(db.PREFIX + "Package", {"job": name, "status": ["!=", "Disassembled"]}):
            frappe.throw("Сначала разберите внутренние упаковки задания.")
        frappe.db.sql("UPDATE `tabERP Basic Marking Code` SET reserved_job=NULL WHERE company=%s AND reserved_job=%s",
                       (company, name))
        db.update("Job", name, {"status": "Cancelled", "revision": value.revision + 1})
        db.history(company, name, "Cancelled", {"reason": reason})
        return get_job(company, name)

    return run(company, "marking.cancel_job", idempotency_key, payload, execute)


def enroll_product(company, item_code, gtin, profile_id, enforcement, idempotency_key):
    authorize(company, "configure")
    _lines([{"item_code": item_code, "gtin": gtin, "quantity": 1}])
    profile(profile_id)
    if enforcement not in {"Preparation", "Enforce"}:
        frappe.throw("Неизвестный режим контроля проведения.")
    payload = {"item_code": item_code, "gtin": gtin, "profile": profile_id, "enforcement": enforcement}

    def execute():
        name = db.identity(company, item_code)
        previous = db.ensure("Product", name, {"company": company, **payload, "enabled": 1})
        if previous.enforcement == "Enforce" and enforcement != "Enforce":
            frappe.throw("Контроль маркированного расхода нельзя отключить рабочей командой настройки.")
        db.update("Product", name, {**payload, "enabled": 1})
        db.history(company, None, "ProductEnrolled", payload)
        return {"name": name, **payload, "live_ready": False,
                "note": "Enforce блокирует расход до внедрения и испытания реального профиля и адаптера."}

    return run(company, "marking.enroll_product", idempotency_key, payload, execute)
