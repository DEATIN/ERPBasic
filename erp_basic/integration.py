"""Надёжный обмен Clientora с документами ERPNext в пределах одной Company.

SQL применяется только к журналу транспорта. Учётные документы сохраняются
через команды и стандартные контроллеры с текущими правами пользователя.
Сетевые запросы выполняют отдельные обработчики очереди.
Описание контракта: docs/clientora-integration.md.
"""

import hashlib
import ipaddress
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import frappe
from frappe.utils import get_datetime, now_datetime

from erp_basic.access import is_administrator, require_administrator, require_company, require_user
from erp_basic.idempotency import OperationConflict, canonical_json

CONNECTOR = "ERP Basic Connector"
OUTBOX = "ERP Basic Outbox"
INBOX = "ERP Basic Inbox"
MAPPING = "ERP Basic External Mapping"
NAMESPACE = uuid.UUID("b773c49e-6dce-4d84-8916-bbf3708ca0e3")
MAX_MESSAGE_BYTES = 200_000
MAX_ATTEMPTS = 8
MAPPING_TYPES = {"customer": "Customer", "offering": "Item", "order": "Sales Order"}
NATIVE_EVENTS = {"Sales Order", "Delivery Note", "Sales Invoice", "Payment Entry"}


def document_event(doc, event=None):
    """Обработчик события отключён на время установки и миграции Site."""
    if frappe.flags.in_install or frappe.flags.in_migrate:
        return
    return _document_event(doc, event)


def dispatch_pending(limit=20):
    """Задание планировщика учитывает установку и запрет обмена при восстановлении."""
    if frappe.flags.in_install or frappe.flags.in_migrate or frappe.conf.get("erp_basic_disable_external_calls"):
        return {"paused": True, "processed": 0}
    return _dispatch_pending(limit)


def mapping_name(connector, external_type, external_id):
    return hashlib.sha256(canonical_json([connector, external_type, external_id]).encode()).hexdigest()


def validate_connector(doc):
    require_administrator()
    require_company(doc.company)
    if doc.source_system != "clientora":
        frappe.throw("Поддерживается источник clientora.")
    _uuid(doc.source_tenant, "source_tenant")
    if not doc.local_tenant:
        doc.local_tenant = str(uuid.uuid4())
    _uuid(doc.local_tenant, "local_tenant")
    _validate_service_user(doc)
    if doc.selling_price_list:
        price_list = frappe.db.get_value("Price List", doc.selling_price_list, ["enabled", "selling"], as_dict=True)
        if not price_list or not price_list.enabled or not price_list.selling:
            frappe.throw("Выберите активный прайс-лист продаж ERP.")
    if doc.endpoint:
        _endpoint(doc.endpoint)
    if doc.get("marking_endpoint"):
        _endpoint(doc.marking_endpoint)
    if doc.get("marking_enabled") and (not doc.enabled or not doc.get("marking_endpoint")
                                       or not doc.get_password("remote_token", raise_exception=False)):
        frappe.throw("Для проекций маркировки нужны включённое подключение, отдельный адрес v2 и служебный ключ.")
    if doc.outbound_enabled and (not doc.enabled or not doc.endpoint or not doc.get_password("remote_token", raise_exception=False)):
        frappe.throw("Для отправки нужны активное подключение, адрес и служебный ключ.")
    old = doc.get_doc_before_save()
    if old and any(doc.get(key) != old.get(key) for key in ("company", "source_system", "source_tenant", "local_tenant")):
        frappe.throw("Контур существующего подключения менять нельзя. Создайте новое подключение.")


def validate_mapping(doc):
    require_administrator()
    connector = frappe.get_doc(CONNECTOR, doc.connector)
    require_company(connector.company)
    if doc.company != connector.company or MAPPING_TYPES.get(doc.external_type) != doc.erp_doctype:
        frappe.throw("Тип или организация соответствия не совпадает с подключением.")
    _uuid(doc.external_id, "external_id")
    native = frappe.get_doc(doc.erp_doctype, doc.erp_name)
    native.check_permission("read")
    if native.meta.has_field("company") and native.company != connector.company:
        frappe.throw("Документ принадлежит другой организации.", frappe.PermissionError)
    old = doc.get_doc_before_save()
    if old and any(doc.get(key) != old.get(key) for key in ("connector", "company", "external_type", "external_id")):
        frappe.throw("Идентификаторы соответствия неизменяемы.")
    if old and old.remote_version and old.erp_name != doc.erp_name:
        frappe.throw("Уже обработанный объект нельзя связать с другим документом.")
    for key in ("remote_version", "remote_hash", "business_hash", "local_version", "native_modified", "last_result"):
        before = old.get(key) if old else None
        if (doc.get(key) or None) != (before or None):
            frappe.throw("Версии и результат соответствия изменяет только транспортный сервис.")


def deny_transport_edit(doc, event=None):
    frappe.throw("Журнал обмена изменяется только транспортным сервисом.", frappe.PermissionError)


def _uuid(value, field):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        frappe.throw("Неверный UUID: " + field)
    return value.lower()


def _validate_service_user(connector):
    from erp_basic.access import allowed_companies

    user = connector.integration_user
    roles = frappe.get_roles(user)
    if not frappe.db.get_value("User", user, "enabled") or is_administrator(user):
        frappe.throw("Назначьте отдельного активного служебного пользователя без System Manager.")
    if "ERP Basic Integration" not in roles or connector.company not in allowed_companies(user):
        frappe.throw("Служебному пользователю нужны роль ERP Basic Integration и разрешение Company.")


def _endpoint(value):
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        frappe.throw("Неверный адрес обмена.")
    host = parsed.hostname
    if not host or parsed.username or parsed.password or parsed.query or parsed.fragment or any(c.isspace() for c in value):
        frappe.throw("Укажите адрес обмена без параметров и учётных данных.")
    local = host in {"127.0.0.1", "localhost", "::1"}
    local_allowed = local and frappe.conf.get("erp_basic_allow_local_exchange")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local_allowed):
        frappe.throw("Обмен требует HTTPS; локальный HTTP включается только настройкой тестового Site.")
    if port and port != 443 and not local_allowed:
        frappe.throw("Нестандартный порт разрешён только локальному тестовому обмену.")
    allowed = frappe.conf.get("erp_basic_exchange_allowed_hosts") or []
    try:
        private = not ipaddress.ip_address(host).is_global
    except ValueError:
        private = host == "localhost" or host.endswith(".localhost")
    if private and not local_allowed and host not in allowed:
        frappe.throw("Внутренний адрес обмена должен быть явно разрешён в настройках Site.")
    return value


def _document_event(doc, event=None):
    if doc.doctype not in NATIVE_EVENTS or getattr(frappe.local, "erp_basic_incoming_exchange", None) or not doc.get("company"):
        return
    if not frappe.db.table_exists(CONNECTOR):
        return
    order_names = set()
    if doc.doctype == "Sales Order":
        order_names.add(doc.name)
    for row in doc.get("items") or []:
        order = row.get("sales_order") or row.get("against_sales_order")
        if order:
            order_names.add(order)
    if doc.doctype == "Payment Entry":
        for row in doc.get("references") or []:
            if row.reference_doctype == "Sales Order":
                order_names.add(row.reference_name)
            elif row.reference_doctype == "Sales Invoice":
                order_names.update(frappe.db.sql(
                    "SELECT DISTINCT sales_order FROM `tabSales Invoice Item` WHERE parent=%s AND COALESCE(sales_order,'')<>''",
                    (row.reference_name,), pluck=True,
                ))
    if not order_names:
        return
    placeholders = ",".join(["%s"] * len(order_names))
    rows = frappe.db.sql(
        f"""SELECT m.name, m.connector FROM `tabERP Basic External Mapping` m
            JOIN `tabERP Basic Connector` c ON c.name=m.connector
            WHERE m.company=%s AND m.external_type='order' AND m.erp_doctype='Sales Order'
              AND m.erp_name IN ({placeholders}) AND c.enabled=1 ORDER BY m.name""",
        [doc.company, *sorted(order_names)], as_dict=True,
    )
    for row in rows:
        connector = frappe.get_doc(CONNECTOR, row.connector)
        mapping = _locked_mapping(row.name)
        _queue_document(connector, mapping, doc)
        if doc.doctype != "Sales Order":
            order = frappe.get_doc("Sales Order", mapping.erp_name)
            if order.company == connector.company:
                _queue_document(connector, mapping, order)


def _dispatch_pending(limit=20):
    limit = max(1, min(int(limit), 100))
    ids = frappe.db.sql(
        """SELECT o.name FROM `tabERP Basic Outbox` o
           JOIN `tabERP Basic Connector` c ON c.name=o.connector
           WHERE c.enabled=1 AND c.outbound_enabled=1 AND
             ((o.status IN ('Pending','Retry') AND o.next_attempt_at<=%s)
              OR (o.status='Sending' AND o.lease_until<=%s))
           ORDER BY o.next_attempt_at, o.creation LIMIT %s""",
        (now_datetime(), now_datetime(), limit), pluck=True,
    )
    processed = 0
    for name in ids:
        claim = _claim_delivery(name)
        if not claim:
            continue
        # Резервирование зафиксировано до HTTP. Сбой на любой границе приводит
        # к повтору того же UUID, который распознаёт входящий журнал получателя.
        frappe.db.commit()
        connector = frappe.get_doc(CONNECTOR, claim.connector)
        if frappe.conf.get("erp_basic_disable_external_calls") or not connector.enabled or not connector.outbound_enabled:
            _update(OUTBOX, name, {"status": "Retry", "lease_token": "", "lease_until": None,
                                 "next_attempt_at": now_datetime()}, lease=claim.lease_token)
            frappe.db.commit()
            continue
        status, code, detail, result = _deliver(connector, json.loads(claim.payload))
        fields = {"status": status, "last_error": code, "error_message": detail,
                  "result": canonical_json(result), "lease_token": "", "lease_until": None}
        if status == "Sent":
            fields["delivered_at"] = now_datetime()
        elif status == "Retry":
            if claim.cycle_attempts >= MAX_ATTEMPTS:
                fields["status"] = "Dead Letter"
            else:
                delay = min(3600, 5 * 2 ** claim.cycle_attempts) + int(name[:2], 16) % 7
                fields["next_attempt_at"] = now_datetime() + timedelta(seconds=delay)
        _update(OUTBOX, name, fields, lease=claim.lease_token)
        frappe.db.commit()
        processed += 1
    return {"paused": False, "processed": processed}


class ExchangeProblem(Exception):
    def __init__(self, code, detail, state="Rejected"):
        self.code, self.detail, self.state = code, detail, state
        super().__init__(detail)


class ExchangeBusy(frappe.ValidationError):
    http_status_code = 503


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _message_name(connector, message_id):
    return _digest([connector, message_id])


def _insert(table, name, values):
    """Внутренняя запись журнала; учётные документы этим путём не изменяются."""
    if table not in {INBOX, OUTBOX, MAPPING}:
        raise ValueError("Неподдерживаемая таблица журнала обмена")
    now = now_datetime()
    data = {"name": name, "creation": now, "modified": now, "owner": frappe.session.user,
            "modified_by": frappe.session.user, "docstatus": 0, "idx": 0, **values}
    columns = list(data)
    quoted = ",".join("`" + key + "`" for key in columns)
    placeholders = ",".join(["%s"] * len(columns))
    frappe.db.sql(
        f"INSERT INTO `tab{table}` ({quoted}) VALUES ({placeholders}) ON DUPLICATE KEY UPDATE name=name",
        [data[key] for key in columns],
    )


def _update(table, name, values, lease=None):
    if table not in {INBOX, OUTBOX, MAPPING}:
        raise ValueError("Неподдерживаемая таблица журнала обмена")
    values = {**values, "modified": now_datetime(), "modified_by": frappe.session.user}
    assignments = ",".join("`" + key + "`=%s" for key in values)
    condition = " AND lease_token=%s AND status='Sending'" if lease is not None else ""
    frappe.db.sql(
        f"UPDATE `tab{table}` SET {assignments} WHERE name=%s{condition}",
        [*values.values(), name, *([lease] if lease is not None else [])],
    )


def _locked_mapping(name):
    rows = frappe.db.sql("SELECT * FROM `tabERP Basic External Mapping` WHERE name=%s FOR UPDATE",
                         (name,), as_dict=True)
    return rows[0] if rows else None


def _ensure_order_mapping(connector, external_id):
    name = mapping_name(connector.name, "order", external_id)
    _insert(MAPPING, name, {"company": connector.company, "connector": connector.name,
                           "external_type": "order", "external_id": external_id,
                           "erp_doctype": "Sales Order", "erp_name": "", "remote_version": 0,
                           "local_version": 0, "remote_hash": "", "native_modified": ""})
    return _locked_mapping(name)


def _resolve(connector, external_type, external_id):
    row = frappe.db.get_value(MAPPING, mapping_name(connector.name, external_type, external_id),
                              ["company", "erp_doctype", "erp_name"], as_dict=True)
    if not row or not row.erp_name:
        raise ExchangeProblem("mapping_required", "Настройте соответствие " + external_type + ": " + external_id,
                              "Needs Mapping")
    if row.company != connector.company or row.erp_doctype != MAPPING_TYPES[external_type]:
        raise ExchangeProblem("mapping_scope_conflict", "Соответствие относится к другому контуру.", "Conflict")
    native = frappe.get_doc(row.erp_doctype, row.erp_name)
    native.check_permission("read")
    return row.erp_name


def _bounded_text(value, field, maximum=140):
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ExchangeProblem("invalid_envelope", "Неверное поле " + field)
    return value


def _validate_envelope(envelope):
    allowed = {"schema_version", "message_id", "source", "source_tenant", "event_type", "object_type",
               "object_id", "object_version", "occurred_at", "correlation_id", "causation_id", "payload"}
    if not isinstance(envelope, dict) or set(envelope) - allowed or len(canonical_json(envelope).encode()) > MAX_MESSAGE_BYTES:
        raise ExchangeProblem("invalid_envelope", "Неверный формат или размер сообщения.")
    if envelope.get("schema_version") != 1 or type(envelope.get("schema_version")) is not int:
        raise ExchangeProblem("schema_not_supported", "Поддерживается версия схемы 1.")
    if (envelope.get("source"), envelope.get("event_type"), envelope.get("object_type")) != (
            "clientora", "order.proposed", "order"):
        raise ExchangeProblem("event_not_supported", "Поддерживается только Clientora order.proposed.")
    for key in ("message_id", "source_tenant", "object_id", "correlation_id"):
        _uuid(envelope.get(key), key)
    if envelope.get("causation_id") is not None:
        _uuid(envelope["causation_id"], "causation_id")
    if type(envelope.get("object_version")) is not int or not 1 <= envelope["object_version"] <= 2_147_483_647:
        raise ExchangeProblem("invalid_version", "Нужна положительная версия объекта.")
    try:
        stamp = datetime.fromisoformat(_bounded_text(envelope.get("occurred_at"), "occurred_at", 40).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError
    except ValueError as error:
        raise ExchangeProblem("invalid_time", "Время события должно включать часовой пояс.") from error
    payload = envelope.get("payload")
    payload_fields = {"customer_id", "branch_id", "currency", "delivery_date", "items", "crm_status", "quoted_total_minor"}
    if not isinstance(payload, dict) or set(payload) - payload_fields:
        raise ExchangeProblem("invalid_payload", "В сообщении есть недопустимые поля документа.")
    _uuid(payload.get("customer_id"), "customer_id")
    if payload.get("branch_id") is not None:
        _uuid(payload["branch_id"], "branch_id")
    currency = _bounded_text(payload.get("currency"), "currency", 3)
    if len(currency) != 3 or not currency.isascii() or not currency.isupper() or not currency.isalpha():
        raise ExchangeProblem("invalid_currency", "Неверный код валюты.")
    try:
        from datetime import date
        date.fromisoformat(_bounded_text(payload.get("delivery_date"), "delivery_date", 10))
    except ValueError as error:
        raise ExchangeProblem("invalid_date", "Нужна дата исполнения YYYY-MM-DD.") from error
    if "crm_status" in payload:
        _bounded_text(payload["crm_status"], "crm_status", 40)
    if "quoted_total_minor" in payload and (type(payload["quoted_total_minor"]) is not int or not 0 <= payload["quoted_total_minor"] <= 10**15):
        raise ExchangeProblem("invalid_amount", "Неверная предложенная сумма CRM.")
    items = payload.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise ExchangeProblem("invalid_items", "Нужно от 1 до 100 строк заказа.")
    for item in items:
        if not isinstance(item, dict) or set(item) != {"offering_id", "quantity"}:
            raise ExchangeProblem("invalid_items", "Строка содержит только offering_id и quantity.")
        _uuid(item.get("offering_id"), "offering_id")
        if not isinstance(item.get("quantity"), str) or len(item["quantity"]) > 30:
            raise ExchangeProblem("invalid_quantity", "Количество передаётся десятичной строкой.")
        try:
            quantity = Decimal(item["quantity"])
            if not quantity.is_finite() or not Decimal("0") < quantity <= Decimal("1000000000"):
                raise InvalidOperation
        except InvalidOperation as error:
            raise ExchangeProblem("invalid_quantity", "Количество должно быть положительным конечным числом.") from error
    return envelope


def _incoming_connector(envelope):
    user = require_user()
    if "ERP Basic Integration" not in frappe.get_roles(user):
        frappe.throw("Нужна роль ERP Basic Integration.", frappe.PermissionError)
    if not isinstance(envelope, dict):
        frappe.throw("Неверный формат сообщения.")
    rows = frappe.db.sql(
        """SELECT name FROM `tabERP Basic Connector` WHERE enabled=1
           AND integration_user=%s AND source_system=%s AND source_tenant=%s""",
        (user, envelope.get("source"), envelope.get("source_tenant")), pluck=True,
    )
    if len(rows) != 1:
        frappe.throw("Подключение не разрешено этому служебному пользователю.", frappe.PermissionError)
    connector = frappe.get_doc(CONNECTOR, rows[0])
    require_company(connector.company)
    _validate_service_user(connector)
    if not frappe.has_permission("Sales Order", "read"):
        frappe.throw("Нет текущих прав чтения учётного заказа.", frappe.PermissionError)
    return connector


def _set_http_status(code):
    if getattr(frappe.local, "response", None) is not None:
        frappe.local.response["http_status_code"] = code


def _read_order(connector, name):
    """Каждый повтор использует текущие права на конкретный учётный документ."""
    native = frappe.get_doc("Sales Order", name)
    native.check_permission("read")
    if native.company != connector.company:
        frappe.throw("Документ относится к другому контуру.", frappe.PermissionError)
    return native


@frappe.whitelist(methods=["POST"])
def receive(envelope):
    """Тело API-запроса Frappe: {\"envelope\": <сообщение с версией>}."""
    require_user()
    if isinstance(envelope, str):
        try:
            envelope = json.loads(envelope)
        except ValueError:
            frappe.throw("Неверный JSON.")
    connector = _incoming_connector(envelope)
    try:
        _validate_envelope(envelope)
    except ExchangeProblem as error:
        _set_http_status(400)
        return {"accepted": False, "status": "Rejected", "error": {"code": error.code, "message": error.detail}}
    return _process_incoming(connector, envelope)


def _process_incoming(connector, envelope):
    name = _message_name(connector.name, envelope["message_id"])
    fingerprint = _digest(envelope)
    _insert(INBOX, name, {
        "company": connector.company, "connector": connector.name, "message_id": envelope["message_id"],
        "object_type": "order", "object_id": envelope["object_id"], "object_version": envelope["object_version"],
        "payload_hash": fingerprint, "payload": canonical_json(envelope), "status": "Pending", "attempts": 0,
        "correlation_id": envelope["correlation_id"], "causation_id": envelope.get("causation_id"),
    })
    inbox = frappe.db.sql("SELECT * FROM `tabERP Basic Inbox` WHERE name=%s FOR UPDATE", (name,), as_dict=True)[0]
    if inbox.payload_hash != fingerprint:
        frappe.throw("UUID сообщения уже использован с другим содержимым.", OperationConflict)
    if inbox.status in {"Applied", "Stale"}:
        answer = json.loads(inbox.result)
        if answer.get("erp_name"):
            if answer.get("erp_doctype") != "Sales Order" or answer.get("company") != connector.company:
                frappe.throw("Документ относится к другому контуру.", frappe.PermissionError)
            _read_order(connector, answer["erp_name"])
        else:
            native_name = frappe.db.get_value(MAPPING, mapping_name(connector.name, "order", envelope["object_id"]), "erp_name")
            if native_name:
                _read_order(connector, native_name)
        return answer
    _update(INBOX, name, {"attempts": (inbox.attempts or 0) + 1})
    mapping_id = mapping_name(connector.name, "order", envelope["object_id"])
    known = frappe.db.sql("SELECT erp_name FROM `tabERP Basic External Mapping` WHERE name=%s",
                         (mapping_id,), pluck=True)
    known_native = known[0] if known else ""
    if known_native:
        # Стандартное сохранение сначала блокирует учётный документ, затем
        # соответствие из обработчика события. Такой же порядок исключает
        # взаимную блокировку с параллельным сохранением или проведением.
        frappe.db.sql("SELECT name FROM `tabSales Order` WHERE name=%s AND company=%s FOR UPDATE",
                      (known_native, connector.company))
    mapping = _ensure_order_mapping(connector, envelope["object_id"])
    if (mapping.erp_name or "") != (known_native or ""):
        # Пока ожидали соответствие, завершился первый импорт. Полный повтор
        # запроса возьмёт блокировку уже известного учётного документа.
        frappe.throw("Первичная обработка заказа завершилась параллельно. Повторите сообщение.", ExchangeBusy)
    if mapping.erp_name:
        _read_order(connector, mapping.erp_name)
    object_hash = _digest({key: envelope[key] for key in ("event_type", "object_type", "object_id", "object_version", "payload")})
    if envelope["object_version"] < mapping.remote_version:
        return _finish_inbox(name, envelope, "Stale", {"reason": "newer_version_already_applied",
                                                       "accepted_version": mapping.remote_version})
    if envelope["object_version"] == mapping.remote_version:
        if mapping.remote_hash != object_hash:
            return _fail_inbox(name, envelope, ExchangeProblem("version_conflict", "Та же версия содержит другие данные.", "Conflict"))
        result = json.loads(mapping.last_result or "{}")
        state = "Stale" if result.get("status") == "Stale" else "Applied"
        return _finish_inbox(name, envelope, state, {**result, "duplicate": True})
    savepoint = "erp_exchange_" + uuid.uuid4().hex
    frappe.db.savepoint(savepoint)
    previous = getattr(frappe.local, "erp_basic_incoming_exchange", None)
    frappe.local.erp_basic_incoming_exchange = {"connector": connector.name, "message_id": envelope["message_id"]}
    try:
        payload = envelope["payload"]
        customer = _resolve(connector, "customer", payload["customer_id"])
        items = [{"item_code": _resolve(connector, "offering", item["offering_id"]), "qty": item["quantity"],
                  "delivery_date": payload["delivery_date"]} for item in payload["items"]]
        document = {"customer": customer, "currency": payload["currency"], "delivery_date": payload["delivery_date"],
                    "items": items}
        if connector.selling_price_list:
            document["selling_price_list"] = connector.selling_price_list
        business_hash = _digest(document)
        expected_modified = None
        unchanged_business = False
        if mapping.erp_name:
            native = frappe.get_doc("Sales Order", mapping.erp_name)
            native.check_permission("read")
            unchanged_business = mapping.business_hash == business_hash
            if native.company != connector.company or (native.docstatus != 0 and not unchanged_business):
                raise ExchangeProblem("document_locked", "Учётный документ уже проведён или относится к другому контуру.", "Conflict")
            if not unchanged_business:
                native.check_permission("write")
                document["name"] = native.name
                expected_modified = mapping.native_modified
                if not expected_modified:
                    raise ExchangeProblem("mapping_review_required", "Проверьте вручную связанный существующий заказ.", "Conflict")
        if not unchanged_business:
            from erp_basic.commands import save_document
            answer = save_document(kind="orders", company=connector.company, document=document,
                                   expected_modified=expected_modified,
                                   idempotency_key="clientora:" + connector.name + ":" + envelope["message_id"])
            native = frappe.get_doc("Sales Order", answer["document"]["name"])
        result = {"erp_doctype": "Sales Order", "erp_name": native.name, "docstatus": native.docstatus,
                  "native_status": native.status, "company": connector.company,
                  "currency": native.currency, "grand_total": str(native.grand_total)}
        _update(MAPPING, mapping.name, {
            "erp_name": native.name, "remote_version": envelope["object_version"], "remote_hash": object_hash,
            "business_hash": business_hash,
            "native_modified": mapping.native_modified if unchanged_business else str(native.modified),
            "last_result": canonical_json(result),
        })
        mapping = _locked_mapping(mapping.name)
        _queue_document(connector, mapping, native, correlation_id=envelope["correlation_id"],
                        causation_id=envelope["message_id"])
        return _finish_inbox(name, envelope, "Applied", result)
    except ExchangeProblem as error:
        frappe.db.rollback(save_point=savepoint)
        return _fail_inbox(name, envelope, error)
    except (frappe.ValidationError, frappe.PermissionError, frappe.DoesNotExistError) as error:
        frappe.db.rollback(save_point=savepoint)
        from frappe.utils import strip_html
        conflict = isinstance(error, (frappe.TimestampMismatchError, OperationConflict))
        detail = strip_html(str(error))[:300] or "Учётный документ не прошёл серверную проверку."
        return _fail_inbox(name, envelope, ExchangeProblem("native_conflict" if conflict else "native_validation",
                                                          detail, "Conflict" if conflict else "Rejected"))
    finally:
        frappe.local.erp_basic_incoming_exchange = previous


def _finish_inbox(name, envelope, state, details):
    result = {**details, "message_id": envelope["message_id"], "accepted": True, "status": state}
    _update(INBOX, name, {"status": state, "result": canonical_json(result), "last_error": "", "error_message": "",
                         "processed_at": now_datetime(), "erp_doctype": details.get("erp_doctype"),
                         "erp_name": details.get("erp_name")})
    return result


def _fail_inbox(name, envelope, error):
    result = {"message_id": envelope["message_id"], "accepted": False, "status": error.state,
              "error": {"code": error.code, "message": error.detail}}
    _update(INBOX, name, {"status": error.state, "result": canonical_json(result), "last_error": error.code,
                         "error_message": error.detail, "processed_at": now_datetime()})
    _set_http_status(409 if error.state == "Conflict" else 422)
    return result


def _queue_document(connector, mapping, doc, correlation_id=None, causation_id=None, force_key=None):
    changed = {field: str(doc.get(field)) for field in (
        "status", "currency", "grand_total", "outstanding_amount", "paid_amount", "received_amount",
        "per_delivered", "per_billed") if doc.get(field) is not None}
    event_key = _digest([connector.name, mapping.name, doc.doctype, doc.name, str(doc.modified),
                         doc.docstatus, changed, force_key])
    if frappe.db.exists(OUTBOX, {"event_key": event_key}):
        return
    version = (mapping.local_version or 0) + 1
    message_id = str(uuid.uuid5(NAMESPACE, event_key))
    payload = {"clientora_order_id": mapping.external_id, "erp_doctype": doc.doctype, "erp_name": doc.name,
               "sales_order": mapping.erp_name, "company": connector.company, "docstatus": doc.docstatus,
               "status": doc.get("status") or ("Cancelled" if doc.docstatus == 2 else "Submitted" if doc.docstatus == 1 else "Draft")}
    for field in ("currency", "grand_total", "outstanding_amount", "paid_amount", "received_amount",
                  "per_delivered", "per_billed"):
        if doc.get(field) is not None:
            payload[field] = str(doc.get(field))
    envelope = {"schema_version": 1, "message_id": message_id, "source": "erp_basic",
                "source_tenant": connector.local_tenant, "event_type": "document.state_changed",
                "object_type": "order", "object_id": mapping.external_id, "object_version": version,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "correlation_id": correlation_id or str(uuid.uuid4()), "causation_id": causation_id,
                "payload": payload}
    _insert(OUTBOX, _message_name(connector.name, message_id), {
        "company": connector.company, "connector": connector.name, "message_id": message_id,
        "object_type": "order", "object_id": mapping.external_id, "object_version": version,
        "payload_hash": _digest(envelope), "payload": canonical_json(envelope), "status": "Pending",
        "event_key": event_key, "attempts": 0, "cycle_attempts": 0, "retry_count": 0,
        "next_attempt_at": now_datetime(), "correlation_id": envelope["correlation_id"],
        "causation_id": causation_id, "erp_doctype": doc.doctype, "erp_name": doc.name,
    })
    _update(MAPPING, mapping.name, {"local_version": version})
    mapping.local_version = version


def _claim_delivery(name):
    rows = frappe.db.sql("SELECT * FROM `tabERP Basic Outbox` WHERE name=%s FOR UPDATE", (name,), as_dict=True)
    if not rows:
        return None
    item = rows[0]
    now = now_datetime()
    if item.status not in {"Pending", "Retry", "Sending"}:
        return None
    if item.status == "Sending" and item.lease_until and get_datetime(item.lease_until) > now:
        return None
    if item.status != "Sending" and item.next_attempt_at and get_datetime(item.next_attempt_at) > now:
        return None
    cycle = (item.cycle_attempts or 0) + 1
    if cycle > MAX_ATTEMPTS:
        _update(OUTBOX, name, {"status": "Dead Letter", "last_error": "attempts_exhausted",
                             "error_message": "Проверьте подключение и повторите отправку из журнала."})
        frappe.db.commit()
        return None
    token = uuid.uuid4().hex
    _update(OUTBOX, name, {"status": "Sending", "lease_token": token, "lease_until": now + timedelta(seconds=90),
                         "attempts": (item.attempts or 0) + 1, "cycle_attempts": cycle})
    item.lease_token, item.cycle_attempts = token, cycle
    return item


def _deliver(connector, envelope):
    import requests

    try:
        if not _outgoing_access(connector, envelope):
            return ("Dead Letter", "access_revoked",
                    "Отправка остановлена: проверьте активность служебного пользователя, его роль, Company и право чтения документа.", {})
        marking = envelope.get("schema_version") == 2 and envelope.get("event_type") == "marking.summary_changed"
        endpoint = connector.get("marking_endpoint") if marking else connector.endpoint
        _endpoint(endpoint)
        secret = connector.get_password("remote_token", raise_exception=False)
        if not secret:
            return "Dead Letter", "credentials_missing", "Настройте служебный ключ Clientora.", {}
        with requests.post(endpoint, json={"envelope": envelope},
                           headers={"Authorization": "Bearer " + secret, "Accept": "application/json"},
                           timeout=(3, 10), allow_redirects=False, stream=True) as response:
            raw = bytearray()
            for chunk in response.iter_content(8192):
                raw.extend(chunk)
                if len(raw) > MAX_MESSAGE_BYTES:
                    return "Retry", "invalid_response", "Ответ Clientora превышает допустимый размер.", {}
            try:
                body = json.loads(raw)
                answer = body.get("message", body) if isinstance(body, dict) else {}
                if not isinstance(answer, dict):
                    answer = {}
            except (ValueError, UnicodeDecodeError):
                answer = {}
            if response.status_code == 200 and answer.get("message_id") == envelope["message_id"] and answer.get("status") in {"Applied", "Stale"} and answer.get("accepted") is True:
                return "Sent", "", "", {"status": answer["status"], "message_id": answer["message_id"]}
            if response.status_code in {401, 403}:
                return "Dead Letter", "authentication_failed", "Проверьте ключ и разрешённую организацию Clientora.", {}
            if response.status_code == 409:
                return "Conflict", "remote_conflict", "Clientora отклонила конфликтующее событие. Проверьте журнал получателя.", {}
            if response.status_code in {400, 404, 405, 410, 422}:
                return "Dead Letter", "remote_rejected", "Проверьте адрес, версию контракта и соответствие заказа в Clientora.", {}
            if 300 <= response.status_code < 400:
                return "Dead Letter", "redirect_rejected", "Укажите конечный адрес обмена без перенаправления.", {}
            return "Retry", "remote_unavailable", "Clientora пока не подтвердила обработку. Повтор запланирован.", {}
    except requests.RequestException:
        return "Retry", "network_unavailable", "Clientora недоступна. Событие сохранено для повторной отправки.", {}
    except frappe.ValidationError:
        return "Dead Letter", "invalid_endpoint", "Проверьте разрешённый адрес подключения.", {}


def _outgoing_access(connector, envelope):
    """Сохранённое сообщение не обходит отозванные права фоновой интеграции."""
    try:
        _validate_service_user(connector)
        payload = envelope.get("payload", {})
        if (envelope.get("source") != "erp_basic" or envelope.get("source_tenant") != connector.local_tenant
                or payload.get("company") != connector.company or payload.get("erp_doctype") not in NATIVE_EVENTS):
            return False
        if envelope.get("schema_version") == 2:
            from erp_basic.marking.projection import outgoing_access
            if not outgoing_access(connector, envelope):
                return False
        native = frappe.get_doc(payload["erp_doctype"], payload["erp_name"])
        return native.company == connector.company and bool(frappe.has_permission(
            native.doctype, "read", doc=native, user=connector.integration_user))
    except (frappe.ValidationError, frappe.PermissionError, frappe.DoesNotExistError, KeyError):
        return False


def _management_connector(company, connector):
    require_company(company)
    item = frappe.get_doc(CONNECTOR, connector)
    if item.company != company:
        frappe.throw("Подключение не найдено.", frappe.PermissionError)
    if not is_administrator() and not (frappe.session.user == item.integration_user and
                                       "ERP Basic Integration" in frappe.get_roles()):
        frappe.throw("Журнал доступен администратору и служебному пользователю.", frappe.PermissionError)
    return item


@frappe.whitelist(methods=["GET"])
def journal(company, connector=None, start=0, page_length=50, limit=None):
    require_company(company)
    try:
        limit = max(1, min(int(limit if limit is not None else page_length), 100))
        start = max(0, min(int(start), 100_000))
    except (TypeError, ValueError):
        frappe.throw("Параметры страницы журнала должны быть целыми числами.")
    if connector:
        _management_connector(company, connector)
        names = [connector]
    elif is_administrator():
        names = frappe.db.sql("SELECT name FROM `tabERP Basic Connector` WHERE company=%s", (company,), pluck=True)
    else:
        if "ERP Basic Integration" not in frappe.get_roles():
            frappe.throw("Нет доступа к журналу обмена.", frappe.PermissionError)
        names = frappe.db.sql("SELECT name FROM `tabERP Basic Connector` WHERE company=%s AND integration_user=%s",
                             (company, frappe.session.user), pluck=True)
    result = {"incoming": [], "outgoing": [], "start": start, "page_length": limit,
              "incoming_total": 0, "outgoing_total": 0, "incoming_has_more": False,
              "outgoing_has_more": False, "has_more": False, "can_retry": True,
              "can_manage_mappings": is_administrator()}
    if not names:
        return result
    placeholders = ",".join(["%s"] * len(names))
    for table, direction in ((INBOX, "incoming"), (OUTBOX, "outgoing")):
        result[direction + "_total"] = frappe.db.sql(
            f"SELECT COUNT(*) FROM `tab{table}` WHERE company=%s AND connector IN ({placeholders})",
            [company, *names],
        )[0][0]
        additional = ",next_attempt_at,delivered_at" if table == OUTBOX else ",processed_at"
        rows = frappe.db.sql(
            f"""SELECT name,connector,message_id,object_id,object_version,status,attempts,creation,modified,
                last_error,error_message,erp_doctype,erp_name{additional} FROM `tab{table}`
                WHERE company=%s AND connector IN ({placeholders})
                ORDER BY creation DESC,name DESC LIMIT %s OFFSET %s""",
            [company, *names, limit, start], as_dict=True,
        )
        for row in rows:
            row["can_retry"] = row.status in {"Needs Mapping", "Conflict", "Rejected", "Dead Letter", "Retry", "Paused"}
            row["action"] = ("configure_mapping" if row.status == "Needs Mapping" else
                             "review_conflict" if row.status == "Conflict" else
                             "retry" if row.status in {"Rejected", "Dead Letter", "Retry", "Paused"} else None)
        result[direction] = rows
        result[direction + "_has_more"] = start + len(rows) < result[direction + "_total"]
        result["has_more"] |= result[direction + "_has_more"]
    return result


@frappe.whitelist(methods=["POST"])
def set_mapping(company, connector, external_type, external_id, erp_doctype, erp_name):
    require_administrator()
    item = _management_connector(company, connector)
    _uuid(external_id, "external_id")
    name = mapping_name(item.name, external_type, external_id)
    if frappe.db.exists(MAPPING, name):
        doc = frappe.get_doc(MAPPING, name)
        doc.erp_doctype, doc.erp_name = erp_doctype, erp_name
        doc.save()
    else:
        doc = frappe.get_doc({"doctype": MAPPING, "company": company, "connector": connector,
                             "external_type": external_type, "external_id": external_id,
                             "erp_doctype": erp_doctype, "erp_name": erp_name}).insert()
    return {"mapping": doc.name, "external_type": external_type, "external_id": external_id,
            "erp_doctype": erp_doctype, "erp_name": erp_name}


@frappe.whitelist(methods=["POST"])
def retry_inbox(company, name):
    require_company(company)
    row = frappe.db.get_value(INBOX, name, ["company", "connector", "payload"], as_dict=True)
    if not row or row.company != company:
        frappe.throw("Событие не найдено.", frappe.DoesNotExistError)
    connector = _management_connector(company, row.connector)
    if not connector.enabled:
        frappe.throw("Подключение отключено.")
    _validate_service_user(connector)
    return _process_incoming(connector, _validate_envelope(json.loads(row.payload)))


@frappe.whitelist(methods=["POST"])
def retry_outbox(company, name):
    require_company(company)
    rows = frappe.db.sql("SELECT * FROM `tabERP Basic Outbox` WHERE name=%s AND company=%s FOR UPDATE",
                         (name, company), as_dict=True)
    if not rows:
        frappe.throw("Событие не найдено.", frappe.DoesNotExistError)
    row = rows[0]
    _management_connector(company, row.connector)
    if row.status in {"Sent", "Pending"}:
        return {"name": name, "status": row.status}
    if row.status == "Sending" and row.lease_until and get_datetime(row.lease_until) > now_datetime():
        frappe.throw("Событие ещё обрабатывается.", OperationConflict)
    _update(OUTBOX, name, {"status": "Pending", "next_attempt_at": now_datetime(), "cycle_attempts": 0,
                         "retry_count": (row.retry_count or 0) + 1, "lease_token": "", "lease_until": None})
    return {"name": name, "status": "Pending"}


@frappe.whitelist(methods=["POST"])
def resolve_conflict(company, name, decision, expected_modified, reason):
    """Администратор явно сохраняет выбор ERP или разрешает обновление черновика."""
    require_administrator()
    require_company(company)
    if decision not in {"keep_erp", "rebase_draft"} or not isinstance(reason, str) or not 8 <= len(reason.strip()) <= 300:
        frappe.throw("Выберите решение и укажите причину длиной 8–300 символов.")
    rows = frappe.db.sql("SELECT * FROM `tabERP Basic Inbox` WHERE name=%s AND company=%s FOR UPDATE",
                         (name, company), as_dict=True)
    if not rows:
        frappe.throw("Событие не найдено.", frappe.DoesNotExistError)
    record = rows[0]
    connector = _management_connector(company, record.connector)
    approval_hash = _digest([decision, expected_modified, reason])
    saved = json.loads(record.result or "{}")
    if saved.get("resolution"):
        if saved["resolution"]["request_hash"] != approval_hash:
            frappe.throw("Это решение уже сохранено с другими параметрами.", OperationConflict)
        return saved
    if record.status != "Conflict":
        frappe.throw("Выберите конфликтующее событие.", OperationConflict)
    message = _validate_envelope(json.loads(record.payload))
    identity = mapping_name(connector.name, "order", message["object_id"])
    native_name = frappe.db.get_value(MAPPING, identity, "erp_name")
    if not native_name:
        frappe.throw("Сначала настройте соответствие документа.")
    native = frappe.get_doc("Sales Order", native_name, for_update=True)
    native.check_permission("read")
    if native.company != company or not expected_modified or get_datetime(native.modified) != get_datetime(expected_modified):
        frappe.throw("Учётный документ изменился. Откройте текущую версию.", OperationConflict)
    mapping = _locked_mapping(identity)
    if not mapping or mapping.erp_name != native.name:
        frappe.throw("Соответствие изменилось. Повторите проверку.", OperationConflict)
    if decision == "rebase_draft":
        if native.docstatus != 0:
            frappe.throw("Повторное применение разрешено только к черновику.", OperationConflict)
        native.check_permission("write")
        _update(MAPPING, mapping.name, {"native_modified": str(native.modified)})
        answer = _process_incoming(connector, message)
    else:
        values = {"native_modified": str(native.modified)}
        if message["object_version"] > mapping.remote_version:
            values.update({"remote_version": message["object_version"],
                           "remote_hash": _digest({key: message[key] for key in
                              ("event_type", "object_type", "object_id", "object_version", "payload")}),
                           "last_result": canonical_json({"status": "Stale", "reason": "administrator_kept_erp",
                              "erp_doctype": native.doctype, "erp_name": native.name, "company": company,
                              "docstatus": native.docstatus, "currency": native.currency,
                              "grand_total": str(native.grand_total)})})
        _update(MAPPING, mapping.name, values)
        answer = _finish_inbox(name, message, "Stale", {"reason": "administrator_kept_erp",
                              "erp_doctype": native.doctype, "erp_name": native.name,
                              "company": company, "docstatus": native.docstatus})
        _queue_document(connector, _locked_mapping(identity), native,
                        correlation_id=message["correlation_id"], causation_id=message["message_id"],
                        force_key="resolution:" + approval_hash)
    if answer.get("accepted"):
        answer["resolution"] = {"decision": decision, "reason": reason.strip(), "request_hash": approval_hash,
                                "at": datetime.now(timezone.utc).isoformat()}
        _update(INBOX, name, {"result": canonical_json(answer)})
    return answer


@frappe.whitelist(methods=["POST"])
def reconcile(company, connector, idempotency_key, after="", limit=50):
    """Повторная сверка снимков через очередь без изменения учётных документов."""
    item = _management_connector(company, connector)
    limit = max(1, min(int(limit), 100))
    from erp_basic.idempotency import run

    def execute():
        rows = frappe.db.sql(
            """SELECT name, erp_name FROM `tabERP Basic External Mapping`
               WHERE company=%s AND connector=%s AND external_type='order' AND erp_name<>'' AND name>%s
               ORDER BY name LIMIT %s""", (company, connector, after, limit), as_dict=True,
        )
        for row in rows:
            native = frappe.get_doc("Sales Order", row.erp_name)
            native.check_permission("read")
            mapping = _locked_mapping(row.name)
            _queue_document(item, mapping, native, force_key="reconcile:" + idempotency_key)
        return {"queued": len(rows), "next": rows[-1].name if len(rows) == limit else None}
    return run(company, "integration.reconcile", idempotency_key,
               {"connector": connector, "after": after, "limit": limit}, execute)
