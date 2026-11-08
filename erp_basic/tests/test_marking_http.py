"""Маркировка через живой HTTP: сессии, CSRF, роли, scope и закрытые реестры.

Нет подмены HTTP, БД или контроллеров. Пароли случайные, только временный файл
0600 вне репозитория. Промышленные адаптеры и внешние запросы не включаются.
"""

import json
import os
from pathlib import Path
import secrets
import tempfile
import unittest
import uuid
from urllib.parse import quote, urlparse

import frappe
import requests

from erp_basic.marking import service
from erp_basic.marking.codes import parse_code
from erp_basic.tests import fixtures
from erp_basic.tests.test_http import HTTPClient
from erp_basic.tests.test_marking import GTIN, synthetic_code


_CONFIG = None
_USERS = {role: "http.marking." + role + "@example.invalid"
          for role in ("manager", "operator", "observer", "other", "service")}


def key():
    return "marking-http-" + secrets.token_hex(20)


def _prepare(credentials_path):
    env = fixtures.setup()
    role_sets = {
        "manager": ["ERP Basic Marking Manager", "Stock User", "Sales User"],
        "operator": ["ERP Basic Marking User", "Stock User"],
        "observer": ["ERP Basic Observer"],
        "other": ["ERP Basic Marking Manager", "Stock User"],
        "service": ["ERP Basic Marking Manager", "ERP Basic Integration", "Stock User"],
    }
    credentials = {}
    for role, email in _USERS.items():
        company = env["other_company"] if role == "other" else env["company"]
        fixtures.make_user(email, role_sets[role], [company])
        password = secrets.token_urlsafe(32)
        user = frappe.get_doc("User", email)
        user.new_password = password
        user.send_welcome_email = 0
        user.save()
        credentials[role] = {"user": email, "password": password}
    env["item"] = fixtures.item("HTTP-MARKING").name
    service.configure(env["company"], "Preparation", "Unconfigured", {}, key())
    created = service.create_job(
        env["company"], "wholesale", "test-pu-v1", "receive",
        [{"item_code": env["item"], "gtin": GTIN, "quantity": 1}], key(),
    )
    env["private_job"] = created["job"]["name"]
    env["private_marker"] = "PRIVATE-MARKING-" + secrets.token_hex(16)
    attachment = frappe.get_doc({
        "doctype": "File", "file_name": "marking-private-" + secrets.token_hex(8) + ".txt",
        "content": env["private_marker"], "is_private": 1,
        "attached_to_doctype": "ERP Basic Marking Job", "attached_to_name": env["private_job"],
    }).insert()
    env["private_file"] = attachment.name
    env["private_url"] = attachment.file_url
    frappe.db.commit()
    descriptor = os.open(credentials_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(credentials, stream)
    return env


class TestMarkingHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _CONFIG:
            raise RuntimeError("Нужен test_marking_http.run() и настоящий HTTP-сервер.")
        cls.config, cls.env = _CONFIG, _CONFIG["env"]
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
        frappe.set_user("Administrator")
        frappe.db.rollback()

    def client(self, role="operator", anonymous=False):
        client = HTTPClient(self.config)
        self.clients.append(client)
        if anonymous:
            return client
        credentials = self.credentials[role]
        response = client.request("POST", "/api/method/login", values={
            "usr": credentials["user"], "pwd": credentials["password"],
        })
        self.assertEqual(response.status_code, 200, "Обычный вход в изолированный HTTP Site")
        session = self.message(client.call("session"))
        self.assertTrue(session["authenticated"])
        self.assertEqual(session["user"], credentials["user"])
        client.csrf = session["csrf_token"]
        self.assertTrue(client.csrf)
        return client

    def call(self, client, method, *, values=None, params=None, verb="GET", csrf=None):
        return client.request(verb, "/api/method/erp_basic.marking.api." + method,
                              values=values, params=params, csrf=csrf)

    def message(self, response):
        self.assertEqual(response.status_code, 200, "HTTP-операция маркировки должна пройти")
        body = response.json()
        self.assertIn("message", body)
        self.assertNotIn("exc_type", body)
        return body["message"]

    def arguments(self, quantity=1, profile="test-pu-v1"):
        return {"company": self.env["company"], "role": "wholesale", "profile": profile,
                "operation": "receive", "lines": [{"item_code": self.env["item"], "gtin": GTIN,
                                                     "quantity": quantity}], "idempotency_key": key()}

    def create(self, client, **options):
        return self.message(self.call(client, "create_job", verb="POST", values=self.arguments(**options)))

    def scan_arguments(self, job):
        return {"company": self.env["company"], "name": job["name"],
                "raw_code": synthetic_code("H-" + secrets.token_hex(8)),
                "expected_revision": job["revision"], "idempotency_key": key(),
                "device_id": str(uuid.uuid4()), "event_id": str(uuid.uuid4()), "sequence": 1}

    def test_guest_csrf_and_method_boundaries(self):
        guest = self.client(anonymous=True)
        denied = self.call(guest, "workspace", params={"company": self.env["company"], "role": "wholesale"})
        self.assertIn(denied.status_code, (401, 403))
        operator = self.client()
        arguments = self.arguments()
        csrf = self.call(operator, "create_job", verb="POST", values=arguments, csrf="invalid-csrf-token")
        self.assertIn(csrf.status_code, (400, 403))
        self.assertEqual(csrf.json().get("exc_type"), "CSRFTokenError")
        wrong_method = self.call(operator, "create_job", params=arguments)
        self.assertIn(wrong_method.status_code, (403, 405))
        created = self.message(self.call(operator, "create_job", verb="POST", values=arguments))
        self.assertEqual(created["job"]["status"], "Draft")

    def test_roles_and_integration_user_do_not_gain_marking_commands(self):
        observer = self.client("observer")
        visible = self.message(self.call(observer, "workspace", params={
            "company": self.env["company"], "role": "wholesale",
        }))
        self.assertFalse(visible["permissions"]["scan"])
        self.assertEqual(self.call(observer, "create_job", verb="POST", values=self.arguments()).status_code, 403)
        operator = self.client()
        config = {"company": self.env["company"], "environment": "Preparation", "fiscal_owner": "Unconfigured",
                  "passport": {}, "idempotency_key": key()}
        self.assertEqual(self.call(operator, "configure", verb="POST", values=config).status_code, 403)
        manager = self.client("manager")
        self.assertEqual(self.message(self.call(manager, "configure", verb="POST", values=config))["environment"],
                         "Preparation")
        service_user = self.client("service")
        self.assertEqual(self.call(service_user, "workspace", params={
            "company": self.env["company"], "role": "wholesale",
        }).status_code, 403)
        self.assertEqual(self.call(service_user, "create_job", verb="POST", values=self.arguments()).status_code, 403)

    def test_scan_replay_payload_conflict_and_reloaded_session(self):
        client = self.client()
        job = self.create(client)["job"]
        arguments = self.scan_arguments(job)
        first = self.message(self.call(client, "scan_code", verb="POST", values=arguments))
        self.assertEqual(first["scan"]["result"], "Accepted")
        self.assertEqual(self.message(self.call(client, "scan_code", verb="POST", values=arguments)), first)
        second_key = {**arguments, "idempotency_key": key()}
        replay = self.message(self.call(client, "scan_code", verb="POST", values=second_key))
        self.assertEqual(replay["scan"]["name"], first["scan"]["name"])
        self.assertEqual(replay["job"]["accepted_count"], 1)
        conflict = self.call(client, "scan_code", verb="POST", values={
            **arguments, "raw_code": synthetic_code("DIFFERENT-HTTP"),
        })
        self.assertEqual(conflict.status_code, 409)
        reloaded = self.client()
        response = self.call(reloaded, "get_job", params={"company": self.env["company"], "name": job["name"]})
        found = self.message(response)
        self.assertEqual(len(found["scans"]), 1)
        self.assertEqual(found["job"]["accepted_count"], 1)
        self.assertNotIn("NOT-A-REAL-SIGNATURE", response.text)

    def test_foreign_company_cannot_read_or_append_to_known_job(self):
        own = self.client()
        job = self.create(own)["job"]
        other = self.client("other")
        for company in (self.env["company"], self.env["other_company"]):
            response = self.call(other, "get_job", params={"company": company, "name": job["name"]})
            self.assertEqual(response.status_code, 403)
            scan = self.scan_arguments(job)
            scan["company"] = company
            self.assertEqual(self.call(other, "scan_code", verb="POST", values=scan).status_code, 403)
        current = self.message(self.call(own, "get_job", params={"company": self.env["company"], "name": job["name"]}))
        self.assertEqual(current["job"]["accepted_count"], 0)
        self.assertEqual(current["scans"], [])

    def test_native_rest_and_generic_rpc_cannot_read_or_modify_registry(self):
        client = self.client()
        job = self.create(client)["job"]
        arguments = self.scan_arguments(job)
        self.message(self.call(client, "scan_code", verb="POST", values=arguments))
        code_id = parse_code(arguments["raw_code"], "gs1-91-92-v1").identity
        for kind, name in (("Job", job["name"]), ("Code", code_id)):
            doctype = "ERP Basic Marking " + kind
            path = "/api/resource/" + quote(doctype, safe="") + "/" + name
            self.assertEqual(client.request("GET", path).status_code, 403)
            self.assertEqual(client.request("PUT", path, values={"status": "Posted"}).status_code, 403)
            denied = client.request("GET", "/api/method/frappe.client.get", params={"doctype": doctype, "name": name})
            self.assertEqual(denied.status_code, 403)
            self.assertNotIn("NOT-A-REAL-SIGNATURE", denied.text)

    def test_full_erp_mode_and_docshare_do_not_open_raw_codes(self):
        from frappe.installer import update_site_config
        from frappe.share import add, remove

        client = self.client()
        job = self.create(client)["job"]
        arguments = self.scan_arguments(job)
        self.message(self.call(client, "scan_code", verb="POST", values=arguments))
        code_id = parse_code(arguments["raw_code"], "gs1-91-92-v1").identity
        shares = [("ERP Basic Marking Job", job["name"]), ("ERP Basic Marking Code", code_id)]
        file_roles = ("other", "service")
        previous = frappe.conf.get("erp_basic_full_erpnext", 0)
        frappe.set_user("Administrator")
        try:
            for doctype, name in shares:
                add(doctype, name, user=_USERS["operator"], read=1, notify=0)
            for role in file_roles:
                add("File", self.env["private_file"], user=_USERS[role], read=1, notify=0)
            frappe.db.commit()
            update_site_config("erp_basic_full_erpnext", 1)
            native_item = client.request("GET", "/api/method/frappe.client.get", params={
                "doctype": "Item", "name": self.env["item"],
            })
            self.assertEqual(native_item.status_code, 200, "Запрос действительно использует full ERPNext")
            for doctype, name in shares:
                path = "/api/resource/" + quote(doctype, safe="") + "/" + name
                self.assertEqual(client.request("GET", path).status_code, 403)
                direct = client.request("GET", "/api/method/frappe.client.get", params={"doctype": doctype, "name": name})
                self.assertEqual(direct.status_code, 403)
            for role in file_roles:
                foreign = self.client(role)
                denied = foreign.request("GET", self.env["private_url"])
                self.assertIn(denied.status_code, (403, 404))
                self.assertNotIn(self.env["private_marker"], denied.text)
                download = foreign.request("GET", "/api/method/frappe.handler.download_file", params={
                    "file_url": self.env["private_url"],
                })
                self.assertIn(download.status_code, (403, 404))
                self.assertNotIn(self.env["private_marker"], download.text)
                archive = foreign.request("GET", "/api/method/frappe.core.api.file.zip_files", params={
                    "files": json.dumps([self.env["private_file"]]),
                })
                self.assertIn(archive.status_code, (403, 404))
                copied = foreign.request("POST", "/api/method/upload_file", values={
                    "library_file_name": self.env["private_file"], "is_private": 1,
                })
                self.assertEqual(copied.status_code, 403)
                duplicate = foreign.request("POST", "/api/resource/File", values={
                    "file_url": self.env["private_url"], "is_private": 1,
                })
                self.assertEqual(duplicate.status_code, 403)
        finally:
            update_site_config("erp_basic_full_erpnext", previous)
            for doctype, name in shares:
                remove(doctype, name, _USERS["operator"])
            for role in file_roles:
                remove("File", self.env["private_file"], _USERS[role])
            frappe.db.commit()

    def test_private_attachment_share_preserves_company_and_service_boundaries(self):
        from frappe.share import add, remove

        frappe.set_user("Administrator")
        users = [_USERS[role] for role in ("operator", "other", "service")]
        try:
            for user in users:
                add("File", self.env["private_file"], user=user, read=1, notify=0)
            frappe.db.commit()
            own = self.client()
            available = own.request("GET", self.env["private_url"])
            self.assertEqual(available.status_code, 200)
            self.assertEqual(available.text, self.env["private_marker"])
            for role in ("other", "service"):
                client = self.client(role)
                denied = client.request("GET", self.env["private_url"])
                self.assertIn(denied.status_code, (403, 404))
                self.assertNotIn(self.env["private_marker"], denied.text)
        finally:
            for user in users:
                remove("File", self.env["private_file"], user)
            frappe.db.commit()

    def test_revocation_is_checked_before_returning_saved_response(self):
        client = self.client()
        arguments = self.arguments()
        first = self.message(self.call(client, "create_job", verb="POST", values=arguments))
        frappe.set_user("Administrator")
        permission = frappe.get_doc("User Permission", {"user": _USERS["operator"], "allow": "Company",
                                                        "for_value": self.env["company"]}).as_dict()
        try:
            frappe.db.delete("User Permission", {"name": permission["name"]})
            frappe.clear_cache(user=_USERS["operator"])
            frappe.db.commit()
            denied = self.call(client, "create_job", verb="POST", values=arguments)
            self.assertEqual(denied.status_code, 403)
        finally:
            frappe.get_doc({"doctype": "User Permission", "user": _USERS["operator"], "allow": "Company",
                            "for_value": self.env["company"], "apply_to_all_doctypes": 1}).insert()
            frappe.clear_cache(user=_USERS["operator"])
            frappe.db.commit()
        found = self.message(self.call(client, "get_job", params={
            "company": self.env["company"], "name": first["job"]["name"],
        }))
        self.assertEqual(found["job"]["name"], first["job"]["name"])

    def test_revoked_item_scope_closes_job_cached_scan_and_shared_attachment(self):
        from frappe.share import add, remove

        client = self.client()
        job = self.create(client)["job"]
        arguments = self.scan_arguments(job)
        self.message(self.call(client, "scan_code", verb="POST", values=arguments))
        frappe.set_user("Administrator")
        other_item = fixtures.item("HTTP-MARK-SCOPE").name
        permission_name = None
        try:
            add("File", self.env["private_file"], user=_USERS["operator"], read=1, notify=0)
            permission = frappe.get_doc({"doctype": "User Permission", "user": _USERS["operator"],
                                         "allow": "Item", "for_value": other_item,
                                         "apply_to_all_doctypes": 1}).insert()
            permission_name = permission.name
            frappe.clear_cache(user=_USERS["operator"])
            frappe.db.commit()
            denied = self.call(client, "get_job", params={"company": self.env["company"], "name": job["name"]})
            self.assertEqual(denied.status_code, 403)
            replay = self.call(client, "scan_code", verb="POST", values=arguments)
            self.assertEqual(replay.status_code, 403)
            workspace = self.message(self.call(client, "workspace", params={
                "company": self.env["company"], "role": "wholesale",
            }))
            self.assertNotIn(job["name"], {row["name"] for row in workspace["jobs"]})
            attachment = client.request("GET", self.env["private_url"])
            self.assertIn(attachment.status_code, (403, 404))
            self.assertNotIn(self.env["private_marker"], attachment.text)
        finally:
            if permission_name:
                frappe.db.delete("User Permission", {"name": permission_name})
            remove("File", self.env["private_file"], _USERS["operator"])
            frappe.clear_cache(user=_USERS["operator"])
            frappe.db.commit()
        visible = self.message(self.call(client, "get_job", params={"company": self.env["company"], "name": job["name"]}))
        self.assertEqual(visible["job"]["accepted_count"], 1)

    def test_quantity_preparation_does_not_allow_real_transmission(self):
        client = self.client("manager")
        job = self.create(client, quantity=2, profile="test-osu-v1")["job"]
        counted = self.message(self.call(client, "record_quantity", verb="POST", values={
            "company": self.env["company"], "name": job["name"], "gtin": GTIN, "quantity": 2,
            "expected_revision": 0, "idempotency_key": key(),
        }))
        prepared = self.message(self.call(client, "complete_job", verb="POST", values={
            "company": self.env["company"], "name": job["name"],
            "expected_revision": counted["job"]["revision"], "idempotency_key": key(),
        }))
        self.assertEqual(prepared["job"]["status"], "Prepared")
        self.assertFalse(prepared["readiness"]["external_allowed"])
        self.assertFalse(prepared["job"]["native_posted"])
        denied = self.call(client, "enqueue_test_operation", verb="POST", values={
            "company": self.env["company"], "name": job["name"], "operation": "transfer",
            "simulation": "accept", "idempotency_key": key(),
        })
        self.assertIn(denied.status_code, (400, 403, 409, 417, 422))


def run(base_url="http://127.0.0.1:18000", host=None):
    global _CONFIG
    if not frappe.conf.get("allow_tests"):
        frappe.throw("HTTP-проверки разрешены только в изолированном Site с allow_tests.")
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.username or parsed.password:
        raise ValueError("Нужен явно указанный loopback HTTP-сервер.")
    host = host or frappe.local.site
    if host != frappe.local.site:
        raise ValueError("HTTP Host должен совпадать с подготовленным тестовым Site.")
    with tempfile.TemporaryDirectory(prefix="erp-marking-http-") as directory:
        credentials_path = str(Path(directory) / "credentials.json")
        env = _prepare(credentials_path)
        _CONFIG = {"base_url": base_url.rstrip("/"), "host": host, "env": env,
                   "credentials_path": credentials_path}
        try:
            result = unittest.TextTestRunner(verbosity=2).run(
                unittest.defaultTestLoader.loadTestsFromTestCase(TestMarkingHTTP)
            )
        finally:
            _CONFIG = None
            frappe.set_user("Administrator")
    if not result.wasSuccessful():
        raise AssertionError(f"HTTP маркировки: failures={len(result.failures)}, errors={len(result.errors)}")
    return {"tests": result.testsRun, "failures": 0, "errors": 0,
            "transport": "настоящий HTTP", "mocks": False, "external_calls": False}
