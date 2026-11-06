"""Изоляция двух самостоятельных Site через настоящий HTTP.

Одинаковые логин и название Company намеренно существуют в обеих базах.
Первый Site содержит уникальные документ, проводку и закрытый файл; второй
готовится отдельно, без восстановления резервной копии первого Site.
"""

import hashlib
import json
import os
from pathlib import Path
import secrets
import unittest
from urllib.parse import quote, urlparse

import frappe
from frappe.utils import nowdate
import requests

from erp_basic import commands
from erp_basic.tests import fixtures
from erp_basic.tests.test_http import HTTPClient


USER = "tenant.erp-basic@example.invalid"
_CONFIG = None


def _guard():
    if not frappe.conf.get("allow_tests"):
        frappe.throw("Проверка разделения Site разрешена только в изолированном Site с allow_tests.")


def _database_fingerprint():
    return hashlib.sha256(str(frappe.conf.db_name).encode("utf-8")).hexdigest()


def _identity():
    env = fixtures.setup()
    fixtures.make_user(USER, ["Sales User", "Stock User"], [env["company"]])
    password = secrets.token_urlsafe(32)
    user = frappe.get_doc("User", USER)
    user.new_password = password
    user.send_welcome_email = 0
    user.save()
    frappe.db.commit()
    return env, {"site": frappe.local.site, "database_fingerprint": _database_fingerprint(),
                 "user": USER, "password": password, "company": env["company"]}


def prepare_peer(credentials_path):
    """Выполнить через bench на втором чистом Site до запуска run на первом."""
    _guard()
    path = Path(credentials_path).resolve()
    if path.is_relative_to(Path(__file__).resolve().parents[2]):
        raise ValueError("Случайные учётные данные должны храниться вне репозитория.")
    _, credentials = _identity()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(credentials, stream)
    return {"prepared": True, "site": frappe.local.site, "credentials_mode": "0600"}


def _prepare_primary():
    env, credentials = _identity()
    item = fixtures.item(prefix="TENANT", stock=True).name
    env["item"] = item
    draft = commands.save_document("orders", env["company"], {
        "customer": env["customer"], "transaction_date": nowdate(), "delivery_date": nowdate(),
        "selling_price_list": "ERP Basic Selling",
        "items": [{"item_code": item, "qty": "1", "rate": "83.00", "warehouse": env["warehouse"]}],
    }, idempotency_key="tenant-" + secrets.token_hex(20))
    env["order"] = draft["document"]["name"]
    env["marker"] = "PRIVATE-TENANT-" + secrets.token_hex(20)
    attachment = frappe.get_doc({
        "doctype": "File", "file_name": "tenant-" + secrets.token_hex(12) + ".txt",
        "content": env["marker"], "is_private": 1,
        "attached_to_doctype": "Sales Order", "attached_to_name": env["order"],
    }).insert()
    env["private_url"] = attachment.file_url
    receipt = commands.save_document("stockDocs", env["company"], {
        "stock_entry_type": "Material Receipt", "posting_date": nowdate(),
        "items": [{"item_code": item, "qty": "3", "basic_rate": "83.00", "t_warehouse": env["warehouse"]}],
    }, idempotency_key="tenant-" + secrets.token_hex(20))["document"]
    posted = commands.transition("stockDocs", receipt["name"], env["company"],
                                 expected_modified=receipt["modified"], idempotency_key="tenant-" + secrets.token_hex(20), action="submit")
    env["voucher"] = posted["document"]["name"]
    frappe.db.commit()
    return env, credentials


class TestTenantHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _CONFIG:
            raise RuntimeError("Проверку необходимо запускать через test_http_tenant.run на живых Site.")
        cls.config = _CONFIG
        cls.env = _CONFIG["env"]

    def setUp(self):
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            try:
                client.close()
            except requests.RequestException:
                pass
        frappe.db.rollback()

    def message(self, response):
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("message", body)
        self.assertNotIn("exc_type", body)
        return body["message"]

    def client(self, peer=False):
        config = {"base_url": self.config["base_url"], "host": self.config["peer_host" if peer else "host"]}
        client = HTTPClient(config)
        self.clients.append(client)
        credentials = self.config["peer_credentials" if peer else "credentials"]
        login = client.request("POST", "/api/method/login", values={"usr": credentials["user"], "pwd": credentials["password"]})
        self.assertEqual(login.status_code, 200)
        session = self.message(client.call("session"))
        self.assertTrue(session["authenticated"])
        self.assertEqual(session["user"], USER)
        self.assertIn(self.env["company"], [row["name"] for row in session["companies"]])
        client.csrf = session["csrf_token"]
        return client

    def test_cookie_первого_site_не_авторизует_одноимённого_пользователя_на_втором(self):
        own = self.client()
        sid = own.session.cookies.get("sid")
        self.assertTrue(sid and sid != "Guest")
        transferred = HTTPClient({"base_url": self.config["base_url"], "host": self.config["peer_host"]})
        self.clients.append(transferred)
        transferred.session.cookies.set("sid", sid)
        session = self.message(transferred.call("session"))
        self.assertFalse(session["authenticated"], "Сессия первого Site не должна существовать во втором")
        denied = transferred.call("get_document", params={"kind": "orders", "company": self.env["company"], "name": self.env["order"]})
        self.assertIn(denied.status_code, (401, 403))
        self.assertTrue(self.message(own.call("session"))["authenticated"], "Проверка чужого cookie не должна повредить исходную сессию")

    def test_документ_списки_и_закрытый_файл_не_переходят_между_базами_site(self):
        own, peer = self.client(), self.client(peer=True)
        params = {"kind": "orders", "company": self.env["company"], "name": self.env["order"]}
        self.assertEqual(self.message(own.call("get_document", params=params))["document"]["name"], self.env["order"])
        unavailable = peer.call("get_document", params=params)
        self.assertIn(unavailable.status_code, (403, 404))
        native = peer.request("GET", "/api/resource/Sales%20Order/" + quote(self.env["order"], safe=""))
        self.assertIn(native.status_code, (403, 404))
        listing = self.message(peer.call("list_documents", params={"kind": "orders", "company": self.env["company"], "search": self.env["order"]}))
        self.assertEqual(listing["rows"], [])
        native_list = peer.request("GET", "/api/resource/Sales%20Order", params={
            "filters": json.dumps({"name": self.env["order"]}), "fields": json.dumps(["name", "company"]),
        })
        self.assertEqual(native_list.status_code, 200)
        self.assertEqual(native_list.json().get("data"), [])
        own_file = own.request("GET", self.env["private_url"])
        self.assertEqual(own_file.status_code, 200)
        self.assertEqual(own_file.text, self.env["marker"])
        peer_file = peer.request("GET", self.env["private_url"])
        self.assertIn(peer_file.status_code, (403, 404))
        self.assertNotIn(self.env["marker"], peer_file.text)

    def test_складской_регистр_и_общий_rpc_не_раскрывают_проводку_другого_site(self):
        own, peer = self.client(), self.client(peer=True)
        params = {"company": self.env["company"], "ledger_type": "stock", "voucher_no": self.env["voucher"]}
        own_rows = self.message(own.call("ledger", params=params))["rows"]
        self.assertTrue(own_rows, "Первый Site должен содержать настоящую складскую проводку")
        self.assertEqual({row["item_code"] for row in own_rows}, {self.env["item"]})
        self.assertEqual(self.message(peer.call("ledger", params=params))["rows"], [])
        generic = peer.request("GET", "/api/method/frappe.desk.query_report.run", params={
            "report_name": "Stock Ledger", "filters": json.dumps({"company": self.env["company"], "voucher_no": self.env["voucher"]}),
        })
        self.assertEqual(generic.status_code, 403)
        self.assertNotIn(self.env["item"], generic.text)


def run(peer_host, peer_credentials_path, base_url="http://127.0.0.1:18000"):
    """Запуск на Site A; Site B уже создан отдельно и подготовлен prepare_peer."""
    global _CONFIG
    _guard()
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.username or parsed.password:
        raise ValueError("Проверка двух Site требует явного loopback HTTP-сервера.")
    with open(peer_credentials_path, encoding="utf-8") as stream:
        peer = json.load(stream)
    if os.stat(peer_credentials_path).st_mode & 0o077:
        raise ValueError("Файл пароля второго Site должен иметь права 0600.")
    if peer_host == frappe.local.site or peer_host != peer["site"] or peer["database_fingerprint"] == _database_fingerprint():
        raise ValueError("Проверка требует два разных Site с разными базами данных.")
    env, credentials = _prepare_primary()
    if peer["company"] != env["company"] or peer["user"] != credentials["user"]:
        raise ValueError("Названия Company и логин должны совпадать: изоляция обеспечивается Site, а не разными правами.")
    _CONFIG = {"base_url": base_url.rstrip("/"), "host": frappe.local.site, "peer_host": peer_host,
               "credentials": credentials, "peer_credentials": peer, "env": env}
    try:
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(TestTenantHTTP))
    finally:
        _CONFIG = None
        frappe.set_user("Administrator")
    if not result.wasSuccessful():
        raise RuntimeError(f"Изоляция Site: failures={len(result.failures)}, errors={len(result.errors)}")
    return {"tests": result.testsRun, "failures": 0, "errors": 0, "transport": "настоящий HTTP",
            "sites": [frappe.local.site, peer_host], "separate_databases": True, "mocks": False}
