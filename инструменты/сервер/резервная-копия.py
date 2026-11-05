#!/usr/bin/env python3
"""Полная зашифрованная копия Frappe Site и восстановление в новый закрытый Site.

Ключ AES и site/common config хранятся отдельно (0600). В архив попадают SQL,
оба каталога вложений, версии приложений и snapshot исходников ERPBasic.
Запускайте Python из bench/env; БД должна быть доступна в том же namespace.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import gzip
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


REPO = Path(__file__).resolve().parents[2]
MAGIC = b"ERP-BASIC-BACKUP-1\n"
CRITICAL_DOCTYPES = (
    "Company", "Warehouse", "Account", "Cost Center", "Fiscal Year", "Bin", "File",
    "Journal Entry", "Payment Ledger Entry", "Version", "User", "User Permission",
    "Role", "Role Profile", "Custom DocPerm", "Contact", "Address",
    "Item", "Customer", "Supplier", "Quotation", "Sales Order",
    "Delivery Note", "Sales Invoice", "Purchase Order", "Purchase Receipt", "Purchase Invoice",
    "Payment Entry", "Stock Entry", "Stock Reconciliation", "GL Entry", "Stock Ledger Entry",
    "ERP Basic Operation", "ERP Basic Audit Event", "ERP Basic External Mapping",
    "ERP Basic Connector", "ERP Basic Inbox", "ERP Basic Outbox",
    "ERP Basic Migration Run", "ERP Basic Migration Mapping",
)


def load_runtime():
    spec = importlib.util.spec_from_file_location("erp_runtime", Path(__file__).with_name("окружение.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()


def source_digest():
    values = {}
    paths = list((REPO / "erp_basic").rglob("*")) + [REPO / "pyproject.toml", REPO / "package.json"]
    for path in sorted(paths):
        if path.is_file() and "__pycache__" not in path.parts and "tests" not in path.parts and path.suffix != ".pyc":
            values[str(path.relative_to(REPO))] = digest(path)
    return hashlib.sha256(canonical(values)).hexdigest(), values


def versions(bench):
    output = {}
    for app in ("frappe", "erpnext", "erp_basic"):
        module = importlib.import_module(app)
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=bench / "apps" / app,
                                capture_output=True, text=True, check=True)
        output[app] = {"version": module.__version__, "commit": result.stdout.strip()}
    output["erp_basic"]["source_sha256"] = source_digest()[0]
    return output


def files_snapshot(site):
    snapshot = {}
    for visibility in ("public", "private"):
        folder = site / visibility / "files"
        folder.mkdir(parents=True, exist_ok=True)
        for file in sorted(folder.rglob("*")):
            if file.is_symlink():
                raise RuntimeError("Вложения с symlink нельзя включать в резервную копию")
            if file.is_file():
                snapshot[str(file.relative_to(site))] = {"bytes": file.stat().st_size, "sha256": digest(file)}
    return snapshot


def database_snapshot():
    import frappe
    snapshot = {}
    pending = list(CRITICAL_DOCTYPES)
    visited = set()
    while pending:
        doctype = pending.pop()
        if doctype in visited:
            continue
        visited.add(doctype)
        if frappe.db.table_exists(doctype):
            table = ("tab" + doctype).replace("`", "``")
            rows = frappe.db.sql(f"SELECT * FROM `{table}` ORDER BY name", as_dict=True)
            if doctype == "ERP Basic Connector":
                for row in rows:
                    row["outbound_enabled"] = 0  # обязательная политика изолированного restore
            snapshot[doctype] = {"rows": len(rows), "sha256": hashlib.sha256(canonical(rows)).hexdigest()}
            pending.extend(field.options for field in frappe.get_meta(doctype).get_table_fields())
    # Проверяем расшифровку всех Password-полей, не сохраняя plaintext в отчёте.
    from frappe.utils.password import get_decrypted_password
    rows = frappe.db.sql("SELECT doctype, name, fieldname FROM `__Auth` WHERE encrypted=1 ORDER BY doctype,name,fieldname",
                         as_dict=True)
    passwords = {}
    for row in rows:
        plaintext = get_decrypted_password(row.doctype, row.name, row.fieldname, raise_exception=True)
        passwords[json.dumps([row.doctype, row.name, row.fieldname])] = hashlib.sha256(plaintext.encode()).hexdigest()
    return {"documents": snapshot, "encrypted_passwords": passwords,
            "doctype_count": frappe.db.count("DocType"), "patch_count": frappe.db.count("Patch Log")}


def open_site(root, site):
    import frappe
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,100}", site) or ".." in site:
        raise ValueError("Некорректное имя Site")
    os.chdir(root / "bench/sites")
    frappe.init(site=site)
    frappe.connect()
    frappe.set_user("Administrator")
    return frappe


def dump_database(root, destination, temporary):
    """Штатный mariadb-dump с паролем в 0600 config, без секрета в argv/логах."""
    import frappe
    def quote(value):
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    credentials = temporary / "dump-client.cnf"
    credentials.write_text("[client]\n" + "\n".join(f"{key}={quote(value)}" for key, value in {
        "user": frappe.conf.db_name, "password": frappe.conf.db_password,
        "host": frappe.conf.db_host or "127.0.0.1", "port": frappe.conf.db_port or 3306,
        "protocol": "TCP",
    }.items()) + "\n")
    credentials.chmod(0o600)
    command = [str(root / "vendor/mariadb/bin/mariadb-dump"), f"--defaults-file={credentials}",
               "--single-transaction", "--quick", "--skip-lock-tables", "--hex-blob",
               "--routines", "--events", "--default-character-set=utf8mb4", frappe.conf.db_name]
    error_path = temporary / "dump-error.log"
    try:
        with error_path.open("wb") as error, gzip.open(destination, "wb") as compressed:
            header = f"-- begin frappe metadata\n-- [frappe]\n-- version = {frappe.__version__}\n-- branch = version-15\n-- end frappe metadata\n\n"
            compressed.write(header.encode())
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=error)
            try:
                shutil.copyfileobj(process.stdout, compressed, 1024 * 1024)
                if process.wait() != 0:
                    raise RuntimeError("mariadb-dump не завершился; резервная копия не опубликована")
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=15)
    finally:
        credentials.unlink(missing_ok=True)


def encrypt(source, destination, key):
    nonce = secrets.token_bytes(12)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(MAGIC)
    with source.open("rb") as src, destination.open("wb") as dest:
        destination.chmod(0o600)
        dest.write(MAGIC + nonce)
        while chunk := src.read(1024 * 1024):
            dest.write(encryptor.update(chunk))
        dest.write(encryptor.finalize())
        dest.write(encryptor.tag)


def decrypt(source, destination, key):
    with source.open("rb") as src:
        if src.read(len(MAGIC)) != MAGIC:
            raise RuntimeError("Неизвестный формат резервной копии")
        nonce = src.read(12)
        src.seek(-16, 2)
        tag = src.read(16)
        remaining = source.stat().st_size - len(MAGIC) - 12 - 16
        src.seek(len(MAGIC) + 12)
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(MAGIC)
        try:
            with destination.open("wb") as dest:
                destination.chmod(0o600)
                while remaining > 0:
                    chunk = src.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise RuntimeError("Оборванный архив")
                    remaining -= len(chunk)
                    dest.write(decryptor.update(chunk))
                dest.write(decryptor.finalize())
        except Exception:
            destination.unlink(missing_ok=True)
            raise RuntimeError("Проверка подлинности архива не пройдена; восстановление не начато") from None


def backup(root, site, directory, key_directory, *, quiesced=False):
    if not quiesced:
        raise RuntimeError("Остановите запись/воркеры данного Site и укажите --quiesced; см. infra/README.md")
    directory = directory.resolve()
    key_directory = key_directory.resolve()
    if directory == key_directory or directory in key_directory.parents or key_directory in directory.parents:
        raise ValueError("Каталог ключей должен храниться отдельно от каталога резервных копий")
    for path in (directory, key_directory):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
    runtime = load_runtime()
    started = time.monotonic()
    frappe = open_site(root, site)
    site_path = root / "bench/sites" / site
    original = json.loads((site_path / "site_config.json").read_text())
    common = json.loads((root / "bench/sites/common_site_config.json").read_text())
    secured = {**original, "maintenance_mode": 1, "pause_scheduler": 1}
    runtime.write_json(site_path / "site_config.json", secured)
    frappe.conf.update(maintenance_mode=1, pause_scheduler=1)
    from frappe.utils.password import get_encryption_key
    encryption_key = get_encryption_key()
    original["encryption_key"] = encryption_key
    backup_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    final_directory = directory / backup_id
    key_file = key_directory / (backup_id + ".key.json")
    completed = False
    try:
        from rq import Worker
        from frappe.utils.background_jobs import get_redis_conn
        if any(worker.get_state() == "busy" for worker in Worker.all(connection=get_redis_conn())):
            raise RuntimeError("Есть выполняющийся RQ job. Остановите воркеры до копирования")
        files = files_snapshot(site_path)
        database = database_snapshot()
        applications = versions(root / "bench")
        with tempfile.TemporaryDirectory(prefix="backup-private-", dir=directory) as folder:
            temporary = Path(folder)
            payload = temporary / "payload"
            payload.mkdir(mode=0o700)
            dump_database(root, payload / "database.sql.gz", temporary)
            for relative in files:
                target = payload / "attachments" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(site_path / relative, target)
                if digest(target) != files[relative]["sha256"]:
                    raise RuntimeError("Вложение изменилось во время копирования")
            _, source_files = source_digest()
            with tarfile.open(payload / "erp_basic-source.tar.gz", "w:gz") as source:
                for relative in source_files:
                    source.add(REPO / relative, arcname=relative, recursive=False)
            if files_snapshot(site_path) != files or database_snapshot() != database or versions(root / "bench") != applications:
                raise RuntimeError("Site или код менялся во время копирования; копия не опубликована")
            manifest = {"format": 1, "backup_id": backup_id, "source_site": site,
                        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "applications": applications, "files": files, "database": database,
                        "sql_sha256": digest(payload / "database.sql.gz"),
                        "source_sha256": digest(payload / "erp_basic-source.tar.gz"), "quiesced": True}
            runtime.write_json(payload / "manifest.json", manifest)
            packed = temporary / "payload.tar.gz"
            with tarfile.open(packed, "w:gz") as archive:
                archive.add(payload, arcname="payload")
            final_directory.mkdir(mode=0o700)
            key = secrets.token_bytes(32)
            encrypted = final_directory / "backup.tar.gz.aes256gcm"
            encrypt(packed, encrypted, key)
            key_record = {"format": 1, "backup_id": backup_id,
                          "aes256_key_base64": base64.b64encode(key).decode(),
                          "frappe_encryption_key": encryption_key,
                          "site_config": original, "common_site_config": common}
            runtime.write_json(key_file, key_record)
            public_manifest = {"format": 1, "backup_id": backup_id, "created_at": manifest["created_at"],
                               "ciphertext_sha256": digest(encrypted), "ciphertext_bytes": encrypted.stat().st_size,
                               "duration_seconds": round(time.monotonic() - started, 3),
                               "key_filename": key_file.name, "verified": True}
            runtime.write_json(final_directory / "manifest.json", public_manifest)
            completed = True
            print(json.dumps({"backup": str(final_directory), "key_file": str(key_file), **public_manifest}, indent=2))
            return final_directory, key_file
    finally:
        runtime.write_json(site_path / "site_config.json", original)
        frappe.destroy()
        if not completed:
            key_file.unlink(missing_ok=True)
            if final_directory.exists():
                shutil.rmtree(final_directory)


def restore(root, site, backup_directory, key_file):
    runtime = load_runtime()
    if not runtime.SITE_PATTERN.fullmatch(site) or ".." in site:
        raise ValueError("Некорректное имя Site")
    site_path = root / "bench/sites" / site
    if site_path.exists():
        raise RuntimeError("Восстановление разрешено только в новый Site; существующие данные не изменены")
    if key_file.stat().st_mode & 0o077:
        raise RuntimeError("Ключ доступен группе/другим пользователям; установите chmod 600")
    keys = json.loads(key_file.read_text())
    outer = json.loads((backup_directory / "manifest.json").read_text())
    encrypted = backup_directory / "backup.tar.gz.aes256gcm"
    if keys["backup_id"] != outer["backup_id"] or digest(encrypted) != outer["ciphertext_sha256"]:
        raise RuntimeError("Ключ/архив не соответствуют manifest; восстановление не начато")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="restore-private-", dir=root) as folder:
        temporary = Path(folder)
        packed = temporary / "payload.tar.gz"
        decrypt(encrypted, packed, base64.b64decode(keys["aes256_key_base64"], validate=True))
        with tarfile.open(packed) as archive:
            for member in archive.getmembers():
                if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                    raise RuntimeError("Архив содержит недопустимые ссылки или специальные файлы")
            archive.extractall(temporary, filter="data")
        payload = temporary / "payload"
        manifest = json.loads((payload / "manifest.json").read_text())
        if manifest["applications"] != versions(root / "bench"):
            raise RuntimeError("Версии или исходники приложений отличаются; восстановите соответствующий код до БД")
        if digest(payload / "database.sql.gz") != manifest["sql_sha256"]:
            raise RuntimeError("Не совпал checksum SQL")
        if digest(payload / "erp_basic-source.tar.gz") != manifest["source_sha256"]:
            raise RuntimeError("Не совпал checksum исходников")
        if site == manifest["source_site"]:
            raise RuntimeError("Нельзя восстанавливать испытательную копию поверх исходного Site")
        config = {**keys["site_config"], "db_name": "_ebr_" + secrets.token_hex(8),
                  "db_password": secrets.token_urlsafe(24), "db_type": "mariadb", "db_host": "127.0.0.1",
                  "db_port": runtime.runtime_config(root)["ports"]["mariadb"],
                  "encryption_key": keys["frappe_encryption_key"], "host_name": f"http://{site}",
                  "maintenance_mode": 1, "pause_scheduler": 1, "mute_emails": 1,
                  "erp_basic_disable_external_calls": 1, "allow_tests": False, "enable_telemetry": 0}
        local_ports = runtime.runtime_config(root)["ports"]
        config.update(redis_cache=f"redis://127.0.0.1:{local_ports['redis_cache']}",
                      redis_queue=f"redis://127.0.0.1:{local_ports['redis_queue']}",
                      redis_socketio=f"redis://127.0.0.1:{local_ports['redis_queue']}")
        for field in ("db_socket", "db_ssl_ca", "db_ssl_cert", "db_ssl_key"):
            config.pop(field, None)
        # Никакие исходящие jobs не должны стартовать даже между импортом SQL и миграцией.
        site_path.mkdir(mode=0o700)
        runtime.write_json(site_path / "site_config.json", config)
        os.chdir(root / "bench/sites")
        import frappe
        from frappe.commands.site import _restore
        frappe.init(site=site)
        credentials = json.loads((root / "credentials.json").read_text())
        restored_admin_password = secrets.token_urlsafe(32)
        _restore(site=site, sql_file_path=str(payload / "database.sql.gz"),
                 db_root_username="root", db_root_password=credentials["db_root_password"],
                 admin_password=restored_admin_password, verbose=False, install_app=[], force=False)
        # Framework может обновить DB password; сохраняем его, вновь применяя ограничения.
        restored_config = json.loads((site_path / "site_config.json").read_text())
        for field in ("maintenance_mode", "pause_scheduler", "mute_emails", "erp_basic_disable_external_calls"):
            restored_config[field] = 1
        restored_config["encryption_key"] = keys["frappe_encryption_key"]
        runtime.write_json(site_path / "site_config.json", restored_config)
        for relative, expected in manifest["files"].items():
            target = site_path / relative
            if not target.resolve().is_relative_to(site_path.resolve()):
                raise RuntimeError("Недопустимый путь вложения")
            source = payload / "attachments" / relative
            if digest(source) != expected["sha256"]:
                raise RuntimeError("Не совпал checksum вложения")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if relative.startswith("private/"):
                target.chmod(0o600)
        frappe.destroy()
        frappe = open_site(root, site)
        try:
            from frappe.utils.password import update_password
            update_password("Administrator", restored_admin_password, logout_all_sessions=True)
            if frappe.db.table_exists("ERP Basic Connector"):
                frappe.db.sql("UPDATE `tabERP Basic Connector` SET outbound_enabled=0")
            frappe.db.commit()
            actual_database = database_snapshot()
            actual_files = files_snapshot(site_path)
            if actual_database != manifest["database"] or actual_files != manifest["files"]:
                raise RuntimeError("Восстановленные документы/вложения/Password-поля отличаются от копии")
            if not all(frappe.conf.get(field) for field in (
                "maintenance_mode", "pause_scheduler", "mute_emails", "erp_basic_disable_external_calls"
            )):
                raise RuntimeError("Восстановленный Site не находится в закрытом режиме")
        finally:
            frappe.destroy()
        secret_file = root / "restore-secrets" / (site + ".json")
        runtime.write_json(secret_file, {"admin_password": restored_admin_password})
        secret_file.parent.chmod(0o700)
        report = {"backup_id": manifest["backup_id"], "site": site, "source_site": manifest["source_site"],
                  "duration_seconds": round(time.monotonic() - started, 3), "database_verified": True,
                  "verified_doctypes": len(actual_database["documents"]),
                  "verified_encrypted_passwords": len(actual_database["encrypted_passwords"]),
                  "public_files": sum(name.startswith("public/") for name in actual_files),
                  "private_files": sum(name.startswith("private/") for name in actual_files),
                  "attachments_verified": True, "external_calls_disabled": True,
                  "scheduler_disabled": True, "maintenance_mode": True,
                  "isolation": "new Frappe Site and new database on the same local MariaDB instance",
                  "application_versions": manifest["applications"]}
        runtime.write_json(backup_directory / f"restore-{site}.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO.parent / "erp-runtime")
    sub = parser.add_subparsers(dest="command", required=True)
    save = sub.add_parser("backup")
    save.add_argument("site")
    save.add_argument("--output", type=Path, required=True)
    save.add_argument("--key-dir", type=Path, required=True)
    save.add_argument("--quiesced", action="store_true")
    recover = sub.add_parser("restore")
    recover.add_argument("site")
    recover.add_argument("--backup", type=Path, required=True)
    recover.add_argument("--key-file", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    runtime = load_runtime()
    os.environ.update(runtime.environment(root))
    os.umask(0o077)
    if args.command == "backup":
        backup(root, args.site, args.output, args.key_dir, quiesced=args.quiesced)
    else:
        restore(root, args.site, args.backup.resolve(), args.key_file.resolve())


if __name__ == "__main__":
    main()
