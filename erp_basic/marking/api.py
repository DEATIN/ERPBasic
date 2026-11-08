"""HTTP-контракт маркировки v1; команды только POST, все полномочия проверяются в сервисах."""

import frappe
from erp_basic.marking import service, packages, outbox


@frappe.whitelist(methods=["GET"])
def workspace(company, role, start=0, page_length=25):
    return service.workspace(company, role, start, page_length)


@frappe.whitelist(methods=["GET"])
def get_job(company, name, start=0, page_length=25):
    return service.get_job(company, name, start, page_length)


@frappe.whitelist(methods=["GET"])
def get_discrepancy(company, name, start=0, page_length=25):
    service.authorize(company)
    start, page_length = service.paging(start, page_length)
    return service.discrepancy(service._job(company, name), start, page_length)


@frappe.whitelist(methods=["GET"])
def get_setup(company):
    return service.get_setup(company)


@frappe.whitelist(methods=["POST"])
def configure(company, environment, fiscal_owner, passport, idempotency_key):
    return service.configure(company, environment, fiscal_owner, passport, idempotency_key)


@frappe.whitelist(methods=["POST"])
def create_job(company, role, profile, operation, lines, idempotency_key,
               reference_kind=None, reference_name=None, warehouse=None):
    return service.create_job(company, role, profile, operation, lines, idempotency_key,
                              reference_kind, reference_name, warehouse)


@frappe.whitelist(methods=["POST"])
def scan_code(company, name, raw_code, expected_revision, idempotency_key, device_id=None, event_id=None, sequence=None):
    return service.scan_code(company, name, raw_code, expected_revision, idempotency_key, device_id, event_id, sequence)


@frappe.whitelist(methods=["POST"])
def record_quantity(company, name, gtin, quantity, expected_revision, idempotency_key):
    return service.record_quantity(company, name, gtin, quantity, expected_revision, idempotency_key)


@frappe.whitelist(methods=["POST"])
def complete_job(company, name, expected_revision, idempotency_key):
    return service.complete_job(company, name, expected_revision, idempotency_key)


@frappe.whitelist(methods=["POST"])
def cancel_job(company, name, expected_revision, reason, idempotency_key):
    return service.cancel_job(company, name, expected_revision, reason, idempotency_key)


@frappe.whitelist(methods=["POST"])
def import_expected_codes(company, name, codes, expected_revision, idempotency_key):
    return service.import_expected_codes(company, name, codes, expected_revision, idempotency_key)


@frappe.whitelist(methods=["POST"])
def enroll_product(company, item_code, gtin, profile, enforcement, idempotency_key):
    return service.enroll_product(company, item_code, gtin, profile, enforcement, idempotency_key)


@frappe.whitelist(methods=["GET"])
def get_package(company, name, start=0, page_length=25):
    return packages.get_package(company, name, start, page_length)


@frappe.whitelist(methods=["POST"])
def create_package(company, job, label, expected_revision, idempotency_key):
    return packages.create_package(company, job, label, expected_revision, idempotency_key)


@frappe.whitelist(methods=["POST"])
def change_package_member(company, name, member_type, member_name, action, expected_revision, reason, idempotency_key):
    return packages.change_member(company, name, member_type, member_name, action, expected_revision, reason, idempotency_key)


@frappe.whitelist(methods=["POST"])
def change_package_state(company, name, status, expected_revision, reason, idempotency_key):
    return packages.change_state(company, name, status, expected_revision, reason, idempotency_key)


@frappe.whitelist(methods=["POST"])
def enqueue_test_operation(company, name, operation, simulation, idempotency_key):
    return outbox.enqueue(company, name, operation, simulation, idempotency_key)


@frappe.whitelist(methods=["POST"])
def request_reconciliation(company, name, idempotency_key):
    return outbox.request_reconciliation(company, name, idempotency_key)


@frappe.whitelist(methods=["POST"])
def preview_migration(company, source_id, profile, rows):
    from erp_basic.marking.migration import preview
    return preview(company, source_id, profile, rows)


@frappe.whitelist(methods=["POST"])
def apply_migration(company, source_id, profile, rows, plan_hash, idempotency_key):
    from erp_basic.marking.migration import apply
    return apply(company, source_id, profile, rows, plan_hash, idempotency_key)


@frappe.whitelist(methods=["POST"])
def queue_projection(company, name, idempotency_key):
    from erp_basic.marking.projection import queue_current
    return queue_current(company, name, idempotency_key)
