import asyncio
import json

from src.extensions.marketplace_security_corpus import (
    CONTINUOUS_VULNERABILITY_MONITORING_SCENARIO_NAMES,
    MARKETPLACE_SECURITY_CORPUS_BLOCKED_CLAIMS,
    MARKETPLACE_SECURITY_CORPUS_CLAIM_BOUNDARY,
    MARKETPLACE_SECURITY_CORPUS_SCENARIO_NAMES,
    PUBLISHER_TRUST_OPERATIONS_SCENARIO_NAMES,
    build_marketplace_security_corpus_contract,
    build_marketplace_security_corpus_report,
)


def test_marketplace_security_corpus_contract_exposes_cx_receipts_and_boundary():
    contract = build_marketplace_security_corpus_contract()
    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["operator_status"] == "marketplace_security_corpus_receipts_visible"
    assert summary["corpus_package_count"] == 8
    assert summary["package_family_count"] == 8
    assert summary["continuous_monitor_count"] == 5
    assert summary["scanner_source_count"] == 4
    assert summary["publisher_operation_count"] == 5
    assert summary["lifecycle_operation_count"] == 6
    assert summary["package_network_boundary_count"] == 5
    assert summary["claim_boundary"] == MARKETPLACE_SECURITY_CORPUS_CLAIM_BOUNDARY
    assert summary["production_secure_marketplace_claim_allowed"] is False
    assert set(MARKETPLACE_SECURITY_CORPUS_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "/api/operator/marketplace-security-corpus" in policy["receipt_surfaces"]
    assert "/api/operator/benchmark-proof" in policy["receipt_surfaces"]


def test_registry_corpus_includes_provenance_signature_sbom_and_review_state():
    contract = build_marketplace_security_corpus_contract()
    corpus = {item["package_id"]: item for item in contract["registry_corpus"]}

    assert all(item["package_digest"].startswith("sha256:cx-") for item in corpus.values())
    assert all(item["sbom_digest"].startswith("sha256:cx-sbom-") for item in corpus.values())
    assert all(item["dependency_graph_digest"].startswith("sha256:cx-deps-") for item in corpus.values())
    assert all(item["review_state"] for item in corpus.values())
    assert all(item["package_count_claim_allowed"] is False for item in corpus.values())
    assert corpus["marketplace.suspicious-exporter"]["signature_status"] == "missing"
    assert corpus["marketplace.suspicious-exporter"]["operator_action"] == "deny_and_quarantine"
    assert corpus["marketplace.legacy-connector"]["operator_action"] == "deny_until_rescan"
    assert corpus["marketplace.legacy-connector"]["database_freshness_at"] == "2026-03-01"


def test_continuous_monitoring_covers_freshness_waivers_and_critical_blocks():
    contract = build_marketplace_security_corpus_contract()
    monitors = {item["monitor_id"]: item for item in contract["continuous_monitoring"]}

    assert monitors["cx-monitor-osv-current"]["database_freshness_at"] == "2026-06-10"
    assert monitors["cx-monitor-nvd-current"]["waiver_state"] == "current"
    assert monitors["cx-monitor-stale-db-negative"]["waiver_state"] == "expired"
    assert monitors["cx-monitor-stale-db-negative"]["operator_action"] == "deny_until_rescan"
    assert monitors["cx-monitor-critical-unwaived"]["finding_state"] == "critical_unwaived"
    assert monitors["cx-monitor-critical-unwaived"]["operator_action"] == "deny_and_quarantine"
    assert all(item["remediation_sla_hours"] is not None for item in monitors.values())


def test_publisher_network_and_lifecycle_receipts_are_redacted_and_diagnostic():
    contract = build_marketplace_security_corpus_contract()
    publishers = {item["publisher_id"]: item for item in contract["publisher_trust_operations"]}
    operations = {item["operation"]: item for item in contract["lifecycle_diagnostics"]}
    network = {item["boundary_class"]: item for item in contract["package_network_boundaries"]}
    encoded = json.dumps(contract)

    assert publishers["pub.verified.neurion"]["operator_action"] == "allow_reviewed_install"
    assert publishers["pub.verified.seraph-labs"]["operator_action"] == "hold_for_canary"
    assert publishers["pub.unverified.unknown"]["operator_action"] == "deny_and_quarantine"
    assert operations["rollback"]["state"] == "rolled_back"
    assert operations["quarantine"]["diagnostics_visible"] is True
    assert operations["reentry_review"]["recovery_receipt_visible"] is True
    assert network["secret_ref_injection"]["secret_ref_policy"] == "redacted_and_rotated"
    assert network["workspace_escape_attempt"]["workspace_egress_decision"] == "path_traversal_denied"
    assert all(item["decision"].startswith("deny") for item in network.values())
    assert contract["summary"]["safe_receipts_redacted"] is True
    assert "/Users/" not in encoded
    assert "sk-" not in encoded


def test_marketplace_security_corpus_report_runs_all_batch_cx_suites():
    payload = asyncio.run(build_marketplace_security_corpus_report())

    assert payload["summary"]["benchmark_posture"] == "marketplace_security_corpus_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(MARKETPLACE_SECURITY_CORPUS_SCENARIO_NAMES)
        + len(CONTINUOUS_VULNERABILITY_MONITORING_SCENARIO_NAMES)
        + len(PUBLISHER_TRUST_OPERATIONS_SCENARIO_NAMES)
    )
    assert payload["summary"]["active_failure_count"] == 0
    assert payload["failure_report"] == []
    assert payload["policy"]["claim_boundary"] == MARKETPLACE_SECURITY_CORPUS_CLAIM_BOUNDARY
