#!/usr/bin/env python3
"""Настоящее backup/restore испытание на Site с allow_tests, без подмен БД/шифрования."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import secrets


HERE = Path(__file__).resolve().parent


def module(filename, name):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def run(root, site):
    runtime = module("окружение.py", "restore_runtime")
    copies = module("резервная-копия.py", "restore_backup")
    os.environ.update(runtime.environment(root))
    os.umask(0o077)
    frappe = copies.open_site(root, site)
    if not frappe.conf.allow_tests:
        raise RuntimeError("Испытание разрешено только в Site с allow_tests=1")
    marker = secrets.token_hex(8)
    probe_user = f"restore-{marker}@erp-basic.invalid"
    probe_files = []
    backup_directory = root / "backups"
    key_directory = root / "backup-keys"
    try:
        frappe.flags.mute_emails = True
        frappe.get_doc({"doctype": "User", "email": probe_user, "first_name": "Restore probe",
                        "enabled": 0, "send_welcome_email": 0}).insert(ignore_permissions=True)
        from frappe.utils.password import set_encrypted_password
        set_encrypted_password("User", probe_user, secrets.token_urlsafe(32), "api_secret")
        for private in (0, 1):
            record = frappe.get_doc({"doctype": "File", "file_name": f"restore-{marker}-{private}.txt",
                                     "content": f"ERP Basic restore marker {marker} private={private}\n",
                                     "is_private": private, "attached_to_doctype": "User",
                                     "attached_to_name": probe_user}).insert(ignore_permissions=True)
            probe_files.append(record.name)
        frappe.db.commit()
        frappe.destroy()
        archive, key = copies.backup(root, site, backup_directory, key_directory, quiesced=True)
        report = copies.restore(root, f"restore-{marker}.localhost", archive, key)
        if not (report["public_files"] >= 1 and report["private_files"] >= 1
                and report["verified_encrypted_passwords"] >= 1):
            raise RuntimeError("Испытание не подтвердило оба вида файлов и Password-поле")
        output = root / "reports/restore.json"
        runtime.write_json(output, report)
        print(json.dumps({"restore_drill": "passed", "report": str(output)}, ensure_ascii=False))
        return report
    finally:
        frappe.destroy()
        frappe = copies.open_site(root, site)
        try:
            for name in probe_files:
                if frappe.db.exists("File", name):
                    frappe.delete_doc("File", name, ignore_permissions=True)
            if frappe.db.exists("User", probe_user):
                frappe.delete_doc("User", probe_user, ignore_permissions=True)
            frappe.db.commit()
        finally:
            frappe.destroy()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--site", default="erp-basic.localhost")
    args = parser.parse_args()
    run(args.root.resolve(), args.site)
