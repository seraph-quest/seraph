import asyncio

from src.extensions.production_marketplace_security import (
    HOSTILE_ECOSYSTEM_PACKAGE_DRILLS_SCENARIO_NAMES,
    INDEPENDENT_PACKAGE_SECURITY_REVIEW_SCENARIO_NAMES,
    MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_SCENARIO_NAMES,
    PACKAGE_NETWORK_INCIDENT_OPERATIONS_SCENARIO_NAMES,
    PRODUCTION_MARKETPLACE_SECURITY_BLOCKED_CLAIMS,
    PRODUCTION_MARKETPLACE_SECURITY_CLAIM_BOUNDARY,
    PUBLISHER_TRUST_VULNERABILITY_HANDLING_SCENARIO_NAMES,
    build_production_marketplace_security_contract,
    build_production_marketplace_security_report,
)


def test_production_marketplace_security_contract_exposes_co_receipts_and_boundary():
    contract = build_production_marketplace_security_contract()
    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["operator_status"] == "production_marketplace_security_receipts_visible"
    assert summary["independent_package_review_count"] == 4
    assert summary["hostile_drill_count"] == 8
    assert summary["package_network_incident_count"] == 6
    assert summary["publisher_vulnerability_review_count"] == 5
    assert summary["rollback_quarantine_diagnostic_count"] == 7
    assert summary["receipt_matrix_count"] == 30
    assert summary["claim_boundary"] == PRODUCTION_MARKETPLACE_SECURITY_CLAIM_BOUNDARY
    assert summary["production_secure_marketplace_claim_allowed"] is False
    assert set(PRODUCTION_MARKETPLACE_SECURITY_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "/api/operator/production-marketplace-security" in policy["receipt_surfaces"]


def test_independent_package_reviews_include_reviewer_digest_sbom_and_scanner_freshness():
    contract = build_production_marketplace_security_contract()

    reviews = contract["independent_package_reviews"]

    assert {item["reviewer_independence"] for item in reviews} >= {
        "independent_external_fixture",
        "separate_reviewer_from_publisher",
    }
    assert all(item["package_digest"].startswith("sha256:") for item in reviews)
    assert all(item["sbom_digest"].startswith("sha256:sbom-") for item in reviews)
    assert all(item["dependency_graph_digest"].startswith("sha256:deps-") for item in reviews)
    assert all(item["database_freshness_at"] == "2026-06-10" for item in reviews)
    assert all(item["raw_receipt_location"].startswith("artifacts/operator-co/") for item in reviews)
    suspicious = next(item for item in reviews if item["package_id"] == "marketplace.suspicious-exporter")
    assert suspicious["signature_status"] == "missing"
    assert suspicious["operator_action"] == "deny_and_quarantine"


def test_hostile_drills_and_package_network_incidents_fail_closed():
    contract = build_production_marketplace_security_contract()

    drills = {item["drill_class"]: item for item in contract["hostile_drills"]}
    incidents = {item["package_network_incident_class"]: item for item in contract["package_network_incidents"]}

    assert drills["unsigned_artifact"]["decision"] == "blocked"
    assert drills["dependency_confusion"]["decision"] == "blocked"
    assert drills["permission_creep"]["decision"] == "quarantined"
    assert drills["compromised_key"]["key_state"] == "revoked"
    assert all(item["fails_closed"] is True for item in drills.values())
    assert incidents["private_network_ssrf"]["private_network_decision"] == "deny_private_network"
    assert incidents["redirect_to_private_network"]["private_network_decision"] == "deny_redirect_chain"
    assert incidents["dns_private_resolution"]["resolved_addresses"][-1] == "169.254.169.254"
    assert incidents["secret_ref_injection"]["secret_ref_policy"] == "destination_host_mismatch_denied"
    assert incidents["workspace_escape_attempt"]["workspace_egress_decision"] == "path_traversal_denied"


def test_publisher_vulnerability_and_rollback_diagnostics_keep_denials_visible():
    contract = build_production_marketplace_security_contract()

    publisher_reviews = {
        item["receipt_id"]: item for item in contract["publisher_vulnerability_reviews"]
    }
    diagnostics = {item["lifecycle_action"]: item for item in contract["rollback_quarantine_diagnostics"]}

    assert publisher_reviews["co-publisher-unknown-revoked"]["operator_action"] == "deny_and_quarantine"
    assert publisher_reviews["co-vulnerability-stale-db-negative"]["operator_action"] == "deny_until_rescan"
    assert publisher_reviews["co-vulnerability-stale-db-negative"]["database_freshness_at"] == "2026-03-01"
    assert diagnostics["update"]["state"] == "rolled_back"
    assert diagnostics["quarantine"]["quarantine_state"] == "quarantined"
    assert diagnostics["reentry_review"]["reentry_decision"] == "denied"
    assert diagnostics["rollback"]["state"] == "durable_restore_point_verified"


def test_production_marketplace_security_report_runs_all_batch_co_suites():
    payload = asyncio.run(build_production_marketplace_security_report())

    assert payload["summary"]["benchmark_posture"] == "production_marketplace_security_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(INDEPENDENT_PACKAGE_SECURITY_REVIEW_SCENARIO_NAMES)
        + len(HOSTILE_ECOSYSTEM_PACKAGE_DRILLS_SCENARIO_NAMES)
        + len(PACKAGE_NETWORK_INCIDENT_OPERATIONS_SCENARIO_NAMES)
        + len(PUBLISHER_TRUST_VULNERABILITY_HANDLING_SCENARIO_NAMES)
        + len(MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_SCENARIO_NAMES)
    )
    assert payload["summary"]["active_failure_count"] == 0
    assert payload["failure_report"] == []
    assert payload["policy"]["claim_boundary"] == PRODUCTION_MARKETPLACE_SECURITY_CLAIM_BOUNDARY
