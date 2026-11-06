"""WSGI для изолированного dev/test стенда, со штатной раздачей assets Frappe."""

import os
import re

import frappe.app


fixed_site = os.environ.get("ERP_BASIC_TEST_SITE")
if fixed_site:
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,100}", fixed_site) or ".." in fixed_site:
        raise RuntimeError("Некорректный ERP_BASIC_TEST_SITE")
    # Аналог явного site= в frappe.app.serve; только локальный single-site test listener.
    frappe.app._site = fixed_site

application = frappe.app.application_with_statics()
