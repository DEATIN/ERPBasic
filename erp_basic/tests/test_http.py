"""Проверки настоящего HTTP на отдельном запущенном Frappe Site.

Маршруты, контроллеры, база данных и права не подменяются. Подготовка использует
штатные документы ERPNext в Site с allow_tests. Случайные пароли записываются
во временный файл 0600 вне репозитория, который удаляется по завершении.
Пароли, CSRF-токены и cookies не попадают в отчёт.
"""

import json
import os
from pathlib import Path
import secrets
import tempfile
import unittest
from urllib.parse import quote, urlparse

import frappe
from frappe.utils import nowdate
import requests

from erp_basic import commands
from erp_basic.tests import fixtures


_CONFIG = None
_USERS = {
    "sales": "sales.erp-basic@example.invalid",
    "observer": "observer.erp-basic@example.invalid",
    "other": "other.erp-basic@example.invalid",
    "unscoped": "unscoped.erp-basic@example.invalid",
    "service": "service.http.erp-basic@example.invalid",
}


def _key():
    return "http-" + secrets.token_hex(20)


def _prepare(credentials_path):
    env = fixtures.setup()
    frappe.set_user("Administrator")
    fixtures.make_user(_USERS["service"], ["Sales User", "ERP Basic Integration"], [env["company"]])
    credentials = {}
    for role, email in _USERS.items():
        password = secrets.token_urlsafe(32)
        user = frappe.get_doc("User", email)
        user.new_password = password
        user.send_welcome_email = 0
        user.save()
        credentials[role] = {"user": email, "password": password}
    item = fixtures.item(prefix="HTTP", stock=False).name
    env["item"] = item
    env["pack_uom"] = "ERP Basic HTTP Pack"
    for unit in (env["pack_uom"], "Kg"):
        if not frappe.db.exists("UOM", unit):
            frappe.get_doc({"doctype": "UOM", "uom_name": unit}).insert()
    packed_item = fixtures.item(prefix="HTTP-PACK", stock=False)
    packed_item.description = "Упаковка для проверки смены номенклатуры через HTTP"
    packed_item.append("uoms", {"uom": env["pack_uom"], "conversion_factor": 5})
    packed_item.sales_uom = env["pack_uom"]
    packed_item.save()
    env["packed_item"] = packed_item.name
    weight_name = "HTTP-KG-" + secrets.token_hex(5)
    weighted_item = frappe.get_doc({"doctype": "Item", "item_code": weight_name, "item_name": weight_name,
                                   "description": "Весовая номенклатура для проверки HTTP",
                                   "item_group": "ERP Basic Test", "stock_uom": "Kg", "is_stock_item": 0,
                                   "is_sales_item": 1, "is_purchase_item": 1}).insert()
    env["weighted_item"] = weighted_item.name
    env["weighted_description"] = weighted_item.description
    order = commands.save_document("orders", env["company"], {
        "customer": env["customer"], "transaction_date": nowdate(), "delivery_date": nowdate(),
        "selling_price_list": "ERP Basic Selling",
        "items": [{"item_code": item, "qty": "1", "rate": "75.00"}],
    }, idempotency_key=_key())
    env["private_order"] = order["document"]["name"]
    env["private_marker"] = "PRIVATE-HTTP-" + secrets.token_hex(16)
    attachment = frappe.get_doc({
        "doctype": "File", "file_name": "http-private-" + secrets.token_hex(8) + ".txt",
        "content": env["private_marker"], "is_private": 1,
        "attached_to_doctype": "Sales Order", "attached_to_name": env["private_order"],
    }).insert()
    env["private_url"] = attachment.file_url
    frappe.db.commit()
    descriptor = os.open(credentials_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(credentials, stream)
    return env


class HTTPClient:
    def __init__(self, config):
        self.base_url = config["base_url"]
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"Host": config["host"], "Accept": "application/json"})
        self.csrf = ""

    def request(self, method, path, *, values=None, params=None, csrf=None):
        headers = {}
        if method != "GET" and (csrf is not None or self.csrf):
            headers["X-Frappe-CSRF-Token"] = self.csrf if csrf is None else csrf
        return self.session.request(method, self.base_url + path, json=values, params=params,
                                    headers=headers, timeout=(5, 30), allow_redirects=False)

    def call(self, method, *, values=None, params=None, verb="GET", csrf=None):
        return self.request(verb, "/api/method/erp_basic.api." + method,
                            values=values, params=params, csrf=csrf)

    def close(self):
        try:
            self.request("POST", "/api/method/logout", values={})
        finally:
            self.session.close()


class TestHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _CONFIG:
            raise RuntimeError("Запустите test_http.run() на отдельном рабочем Site; сервер не подменяется фикстурой.")
        cls.config = _CONFIG
        cls.env = _CONFIG["env"]
        with open(_CONFIG["credentials_path"], encoding="utf-8") as stream:
            cls.credentials = json.load(stream)

    def setUp(self):
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            try:
                client.close()
            except requests.RequestException:
                pass
        frappe.db.rollback()

    def client(self, role="sales"):
        client = HTTPClient(self.config)
        self.clients.append(client)
        credentials = self.credentials[role]
        login = client.request("POST", "/api/method/login", values={
            "usr": credentials["user"], "pwd": credentials["password"],
        })
        self.assertEqual(login.status_code, 200, "Вход через настоящий HTTP должен завершиться успешно")
        session = self.message(client.call("session"))
        self.assertTrue(session["authenticated"])
        self.assertEqual(session["user"], credentials["user"])
        self.assertTrue(session["csrf_token"], "Авторизованная сессия должна выдавать CSRF-токен")
        self.assertRegex(session["business_date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(session["time_zone"], frappe.utils.get_system_timezone())
        client.csrf = session["csrf_token"]
        return client

    def message(self, response):
        self.assertEqual(response.status_code, 200, "HTTP-операция должна завершиться успешно")
        data = response.json()
        self.assertIn("message", data)
        self.assertNotIn("exc_type", data)
        return data["message"]

    def values(self):
        return {"customer": self.env["customer"], "transaction_date": nowdate(),
                "delivery_date": nowdate(), "selling_price_list": "ERP Basic Selling",
                "items": [{"item_code": self.env["item"], "qty": "2", "rate": "125.50"}]}

    def create(self, client):
        return self.message(client.call("save_document", verb="POST", values={
            "kind": "orders", "company": self.env["company"], "document": self.values(),
            "idempotency_key": _key(),
        }))

    def test_страница_erp_и_серверные_ресурсы_отдаются_настоящим_http(self):
        client = HTTPClient(self.config)
        self.clients.append(client)
        page = client.request("GET", "/erp")
        self.assertEqual(page.status_code, 200)
        self.assertIn('/assets/erp_basic/server-app.js', page.text)
        self.assertIn('/assets/erp_basic/server-api.js', page.text)
        self.assertNotIn('assets/app.js', page.text)
        self.assertNotIn('erpbasic-v2', page.text)
        for resource, content_type in (("server-api.js", "javascript"), ("server-app.js", "javascript"),
                                       ("server-app.css", "text/css"), ("erp-base.css", "text/css"),
                                       ("themes.css", "text/css"), ("theme.js", "javascript")):
            with self.subTest(resource=resource):
                asset = client.request("GET", "/assets/erp_basic/" + resource)
                self.assertEqual(asset.status_code, 200)
                self.assertIn(content_type, asset.headers.get("Content-Type", ""))
                self.assertTrue(asset.content)

    def test_вход_csrf_и_выход_используют_реальные_сессии(self):
        anonymous = HTTPClient(self.config)
        self.clients.append(anonymous)
        self.assertFalse(self.message(anonymous.call("session"))["authenticated"])
        protected = anonymous.call("list_documents", params={"kind": "orders", "company": self.env["company"]})
        self.assertIn(protected.status_code, (401, 403))
        client = self.client()
        values = {"kind": "orders", "company": self.env["company"], "document": self.values(),
                  "idempotency_key": _key()}
        rejected = client.call("save_document", verb="POST", values=values, csrf="invalid-csrf-token")
        self.assertIn(rejected.status_code, (400, 403), "Подменённый CSRF-токен не должен позволять запись документа")
        self.assertEqual(rejected.json().get("exc_type"), "CSRFTokenError")
        saved = self.message(client.call("save_document", verb="POST", values=values))
        self.assertTrue(saved["document"]["name"])
        logout = client.request("POST", "/api/method/logout", values={})
        self.assertEqual(logout.status_code, 200)
        self.assertFalse(self.message(client.call("session"))["authenticated"])

    def test_запись_проведение_повтор_и_новый_вход_сохраняют_документ(self):
        client = self.client()
        token = _key()
        arguments = {"kind": "orders", "company": self.env["company"], "document": self.values(),
                     "idempotency_key": token}
        first = self.message(client.call("save_document", verb="POST", values=arguments))
        repeated = self.message(client.call("save_document", verb="POST", values=arguments))
        self.assertEqual(first["document"]["name"], repeated["document"]["name"])
        self.assertEqual(first["document"]["grand_total"], "251.00")
        posted_args = {"kind": "orders", "company": self.env["company"],
                       "name": first["document"]["name"], "expected_modified": first["document"]["modified"],
                       "idempotency_key": _key()}
        posted = self.message(client.call("submit_document", verb="POST", values=posted_args))
        replay = self.message(client.call("submit_document", verb="POST", values=posted_args))
        self.assertEqual(posted["document"]["docstatus"], 1)
        self.assertEqual(posted["document"], replay["document"])
        reloaded = HTTPClient(self.config)
        self.clients.append(reloaded)
        reloaded.session.cookies.update(client.session.cookies)
        self.assertTrue(self.message(reloaded.call("session"))["authenticated"])
        found = self.message(reloaded.call("get_document", params={"kind": "orders", "company": self.env["company"],
                                                                    "name": posted["document"]["name"]}))
        self.assertEqual(found["document"]["docstatus"], 1)
        self.assertEqual(found["document"]["grand_total"], "251.00")
        independent = self.client()
        found_again = self.message(independent.call("get_document", params={"kind": "orders", "company": self.env["company"],
                                                                           "name": posted["document"]["name"]}))
        self.assertEqual(found_again["document"]["name"], first["document"]["name"])
        frappe.db.rollback()
        self.assertEqual(frappe.db.get_value("Sales Order", first["document"]["name"], "docstatus"), 1)

    def test_ручной_выбор_товара_и_единицы_пересчитывает_native_количество(self):
        client = self.client()
        values = self.values()
        values["items"] = [{"item_code": self.env["packed_item"], "qty": "2", "rate": "125.50",
                            "uom": None, "conversion_factor": None}]
        saved = self.message(client.call("save_document", verb="POST", values={
            "kind": "orders", "company": self.env["company"], "document": values, "idempotency_key": _key(),
        }))["document"]
        row = saved["items"][0]
        self.assertEqual(row["uom"], self.env["pack_uom"], "Единицу продаж выбирает native Item, а не браузер")
        self.assertEqual(float(row["conversion_factor"]), 5)
        self.assertEqual(float(row["stock_qty"]), 10)
        # Смена единицы той же сохранённой строки не сохраняет прежний коэффициент 5.
        updated = self.message(client.call("save_document", verb="POST", values={
            "kind": "orders", "company": self.env["company"], "expected_modified": saved["modified"],
            "document": {"name": saved["name"], "items": [{"name": row["name"], "item_code": self.env["packed_item"],
                          "qty": "2", "rate": "125.50", "uom": "Nos", "conversion_factor": None}]},
            "idempotency_key": _key(),
        }))["document"]
        self.assertEqual(float(updated["items"][0]["conversion_factor"]), 1)
        self.assertEqual(float(updated["items"][0]["stock_qty"]), 2)
        # Возвращаем упаковку, затем меняем номенклатуру с иной складской единицей.
        packed = self.message(client.call("save_document", verb="POST", values={
            "kind": "orders", "company": self.env["company"], "expected_modified": updated["modified"],
            "document": {"name": saved["name"], "items": [{"name": row["name"], "item_code": self.env["packed_item"],
                          "qty": "2", "rate": "125.50", "uom": self.env["pack_uom"], "conversion_factor": None}]},
            "idempotency_key": _key(),
        }))["document"]
        self.assertEqual(float(packed["items"][0]["conversion_factor"]), 5)
        changed = self.message(client.call("save_document", verb="POST", values={
            "kind": "orders", "company": self.env["company"], "expected_modified": packed["modified"],
            "document": {"name": saved["name"], "items": [{"name": row["name"], "item_code": self.env["weighted_item"],
                          "qty": "2", "rate": "125.50", "uom": None, "conversion_factor": None}]},
            "idempotency_key": _key(),
        }))["document"]
        self.assertEqual(changed["items"][0]["uom"], "Kg")
        self.assertEqual(float(changed["items"][0]["conversion_factor"]), 1)
        self.assertEqual(float(changed["items"][0]["stock_qty"]), 2)
        frappe.db.rollback()
        native = frappe.get_doc("Sales Order", saved["name"])
        self.assertEqual(native.items[0].stock_uom, "Kg")
        self.assertEqual(native.items[0].stock_qty, 2)
        self.assertEqual(native.items[0].item_name, self.env["weighted_item"])
        self.assertEqual(native.items[0].description, self.env["weighted_description"])

    def test_чужая_организация_недоступна_через_api_resource_и_отчёт(self):
        other = self.client("other")
        company = self.env["company"]
        name = self.env["private_order"]
        for method, params in (
            ("get_document", {"kind": "orders", "company": company, "name": name}),
            ("list_documents", {"kind": "orders", "company": company}),
            ("stock_balance", {"company": company}),
            ("ledger", {"company": company, "ledger_type": "stock"}),
        ):
            with self.subTest(endpoint=method):
                response = other.call(method, params=params)
                self.assertEqual(response.status_code, 403)
                self.assertNotIn(self.env["private_marker"], response.text)
        native = other.request("GET", "/api/resource/Sales%20Order/" + quote(name, safe=""))
        self.assertIn(native.status_code, (403, 404))
        listing = other.request("GET", "/api/resource/Sales%20Order", params={
            "filters": json.dumps({"company": company}), "fields": json.dumps(["name", "company"]),
        })
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json().get("data"), [])
        generic = other.request("GET", "/api/method/frappe.desk.query_report.run", params={
            "report_name": "Stock Balance", "filters": json.dumps({"company": company}),
        })
        self.assertEqual(generic.status_code, 403, "Общий RPC отчётов не должен обходить границы Basic")

    def test_наблюдатель_не_проводит_а_пользователь_без_организации_не_читает_списки(self):
        creator = self.client()
        draft = self.create(creator)["document"]
        observer = self.client("observer")
        read = self.message(observer.call("get_document", params={"kind": "orders", "company": self.env["company"], "name": draft["name"]}))
        self.assertEqual(read["document"]["name"], draft["name"])
        denied = observer.call("submit_document", verb="POST", values={"kind": "orders", "company": self.env["company"],
                                                                       "name": draft["name"], "expected_modified": draft["modified"],
                                                                       "idempotency_key": _key()})
        self.assertEqual(denied.status_code, 403)
        unscoped = self.client("unscoped")
        session = self.message(unscoped.call("session"))
        self.assertEqual(session["companies"], [])
        self.assertEqual(unscoped.call("list_documents", params={"kind": "orders", "company": self.env["company"]}).status_code, 403)
        native = unscoped.request("GET", "/api/resource/Sales%20Order")
        self.assertEqual(native.status_code, 403)
        frappe.db.rollback()
        self.assertEqual(frappe.db.get_value("Sales Order", draft["name"], "docstatus"), 0)

    def test_docshare_не_расширяет_company_через_resource_списки_и_файлы(self):
        from frappe.share import add, remove

        company = self.env["company"]
        name = self.env["private_order"]
        path = self.env["private_url"]
        file_name = frappe.db.get_value("File", {"file_url": path, "attached_to_name": name}, "name")
        shares = [("Sales Order", name), ("Company", company), ("File", file_name)]
        user = _USERS["other"]
        frappe.set_user("Administrator")
        try:
            for doctype, document in shares:
                add(doctype, document, user=user, read=1, notify=0)
            frappe.db.commit()
            other = self.client("other")
            self.assertEqual(self.message(other.call("session"))["companies"][0]["name"], self.env["other_company"])
            for doctype, document in shares[:2]:
                with self.subTest(resource=doctype):
                    response = other.request("GET", "/api/resource/" + quote(doctype, safe="") + "/" + quote(document, safe=""))
                    self.assertEqual(response.status_code, 403, "DocShare не должен заменять членство в Company")
            for filters in (
                {},
                {"filters": json.dumps({"company": company})},
                {"filters": json.dumps([["company", "=", company]])},
                {"or_filters": json.dumps([["name", "=", name]])},
            ):
                with self.subTest(filters=filters):
                    listing = other.request("GET", "/api/resource/Sales%20Order", params={
                        "fields": json.dumps(["name", "company"]), **filters,
                    })
                    self.assertEqual(listing.status_code, 200)
                    self.assertNotIn(name, [row["name"] for row in listing.json()["data"]])
            companies = other.request("GET", "/api/resource/Company", params={"fields": json.dumps(["name"])})
            self.assertEqual(companies.status_code, 200)
            self.assertNotIn(company, [row["name"] for row in companies.json()["data"]])
            with_slash = other.request("GET", "/api/resource/Sales%20Order/", params={
                "filters": json.dumps({"company": company}), "fields": json.dumps(["name", "company"]),
            })
            self.assertEqual(with_slash.status_code, 200)
            self.assertEqual(with_slash.json()["data"], [])
            for params in ({}, {"fid": file_name}):
                forbidden = other.request("GET", path, params=params)
                self.assertEqual(forbidden.status_code, 403)
                self.assertNotIn(self.env["private_marker"], forbidden.text)
            own_other = self.message(other.call("save_document", verb="POST", values={
                "kind": "orders", "company": self.env["other_company"], "document": self.values(),
                "idempotency_key": _key(),
            }))["document"]
            file_count = frappe.db.count("File", {"file_url": path})
            for source in (
                {"file_url": path},
                {"file_url": path.replace("/private/files/", "/private/files/./")},
                {"file_url": quote(path, safe="")},
                {"library_file_name": file_name},
            ):
                with self.subTest(source=source):
                    rebound = other.request("POST", "/api/method/upload_file", values={
                        "doctype": "Sales Order", "docname": own_other["name"], "is_private": 1, **source,
                    })
                    self.assertEqual(rebound.status_code, 403,
                                     "Чужой shared blob нельзя перепривязать к своей Company через upload")
            frappe.db.rollback()
            self.assertEqual(frappe.db.count("File", {"file_url": path}), file_count)
            remove("Sales Order", name, user)
            frappe.db.commit()
            self.assertEqual(other.request("GET", path).status_code, 403,
                             "Прямой share File также не заменяет доступ к Company родителя")
            own = self.client()
            native = own.request("GET", "/api/resource/Sales%20Order/" + quote(name, safe=""))
            self.assertEqual(native.status_code, 200)
            self.assertEqual(native.json()["data"]["name"], name)
            trailing = own.request("GET", "/api/resource/Sales%20Order/" + quote(name, safe="") + "/")
            self.assertEqual(trailing.status_code, 200)
            self.assertEqual(trailing.json()["data"]["name"], name)
            available = own.request("GET", path)
            self.assertEqual(available.status_code, 200)
            self.assertEqual(available.text, self.env["private_marker"])
        finally:
            frappe.set_user("Administrator")
            for doctype, document in shares:
                remove(doctype, document, user)
            frappe.db.commit()

    def test_прямая_запись_resource_и_run_method_не_обходят_ключ_операции(self):
        client = self.client()
        draft = self.create(client)["document"]
        resource = "/api/resource/Sales%20Order/" + quote(draft["name"], safe="")
        native_values = {"doctype": "Sales Order", "company": self.env["company"], **self.values()}
        post = client.request("POST", "/api/resource/Sales%20Order", values=native_values)
        self.assertEqual(post.status_code, 403)
        put = client.request("PUT", resource, values={"delivery_date": "2099-12-31"})
        self.assertEqual(put.status_code, 403)
        run = client.request("GET", resource, params={"run_method": "submit"})
        self.assertEqual(run.status_code, 403)
        delete = client.request("DELETE", resource)
        self.assertEqual(delete.status_code, 403)
        service = self.client("service")
        before_count = frappe.db.count("Sales Order", {"company": self.env["company"]})
        for method, arguments in (
            ("save_document", {"kind": "orders", "company": self.env["company"], "document": self.values(),
                               "idempotency_key": _key()}),
            ("save_document", {"kind": "orders", "company": self.env["company"],
                               "document": {"name": draft["name"], "delivery_date": "2099-12-31"},
                               "expected_modified": draft["modified"], "idempotency_key": _key()}),
            ("submit_document", {"kind": "orders", "company": self.env["company"], "name": draft["name"],
                                 "expected_modified": draft["modified"], "idempotency_key": _key()}),
        ):
            with self.subTest(service_method=method):
                denied = service.call(method, verb="POST", values=arguments)
                self.assertEqual(denied.status_code, 403, "Служебная роль не должна обходить inbox через API сотрудника")
        frappe.db.rollback()
        self.assertEqual(frappe.db.count("Sales Order", {"company": self.env["company"]}), before_count)
        self.assertTrue(frappe.db.exists("Sales Order", draft["name"]))
        self.assertEqual(frappe.db.get_value("Sales Order", draft["name"], "docstatus"), 0)
        self.assertNotEqual(str(frappe.db.get_value("Sales Order", draft["name"], "delivery_date")), "2099-12-31")

    def test_закрытое_вложение_требует_доступа_к_родительскому_документу(self):
        own = self.client()
        other = self.client("other")
        path = self.env["private_url"]
        self.assertTrue(path.startswith("/private/files/"))
        available = own.request("GET", path)
        self.assertEqual(available.status_code, 200)
        self.assertEqual(available.text, self.env["private_marker"])
        hidden_rpc = own.request("GET", path, params={
            "cmd": "frappe.client.get", "doctype": "Sales Order", "name": self.env["private_order"],
        })
        self.assertEqual(hidden_rpc.status_code, 403, "Доступный файл не должен открывать запрещённый RPC через cmd")
        hidden_write = own.request("POST", path, values={
            "cmd": "frappe.client.set_value", "doctype": "Sales Order", "name": self.env["private_order"],
            "fieldname": "terms", "value": "FORBIDDEN-FILE-RPC",
        })
        self.assertEqual(hidden_write.status_code, 403)
        frappe.db.rollback()
        self.assertNotEqual(frappe.db.get_value("Sales Order", self.env["private_order"], "terms"), "FORBIDDEN-FILE-RPC")
        delegated = own.request("POST", "/api/method/upload_file", values={
            "doctype": "Sales Order", "docname": self.env["private_order"],
            "method": "frappe.auth.get_logged_user", "is_private": 1,
        })
        self.assertEqual(delegated.status_code, 403, "Upload не должен делегировать произвольный whitelisted method")
        service = self.client("service")
        service_upload = service.request("POST", "/api/method/upload_file", values={
            "doctype": "Sales Order", "docname": self.env["private_order"], "file_url": path, "is_private": 1,
        })
        self.assertEqual(service_upload.status_code, 403, "Служебная роль изменяет данные только через контракт обмена")
        reused = own.request("POST", "/api/method/upload_file", values={
            "doctype": "Sales Order", "docname": self.env["private_order"],
            "file_url": path.replace("/private/files/", "/private/files/./"), "is_private": 1,
        })
        self.assertEqual(reused.status_code, 200, "Свои приватные вложения можно повторно прикреплять")
        self.assertEqual(reused.json()["message"]["file_url"], path)
        forbidden = other.request("GET", path)
        self.assertIn(forbidden.status_code, (403, 404))
        self.assertNotIn(self.env["private_marker"], forbidden.text)
        metadata = other.request("GET", "/api/resource/File", params={"filters": json.dumps({"file_url": path})})
        self.assertIn(metadata.status_code, (403, 404), "Общий доступ к File не должен раскрывать метаданные закрытого вложения")


def run(base_url="http://127.0.0.1:18000", host=None):
    """Запуск через bench execute; нужен отдельно запущенный настоящий HTTP-сервер."""
    global _CONFIG
    if not frappe.conf.get("allow_tests"):
        frappe.throw("HTTP-проверки разрешены только в изолированном Site с allow_tests.")
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.username or parsed.password:
        raise ValueError("HTTP-проверки требуют явно указанного тестового сервера на loopback.")
    host = host or frappe.local.site
    with tempfile.TemporaryDirectory(prefix="erp-basic-http-") as directory:
        credentials_path = str(Path(directory) / "credentials.json")
        env = _prepare(credentials_path)
        _CONFIG = {"base_url": base_url.rstrip("/"), "host": host, "env": env, "credentials_path": credentials_path}
        try:
            result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(TestHTTP))
        finally:
            _CONFIG = None
            frappe.set_user("Administrator")
    if not result.wasSuccessful():
        raise RuntimeError(f"Проверки настоящего HTTP: failures={len(result.failures)}, errors={len(result.errors)}")
    return {"tests": result.testsRun, "failures": 0, "errors": 0, "transport": "настоящий HTTP", "mocks": False}
