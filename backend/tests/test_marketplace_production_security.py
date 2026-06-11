import asyncio
import json

from src.extensions.marketplace_production_security import (
    ECOSYSTEM_SUPPLY_CHAIN_OPERATIONS_V1_SCENARIO_NAMES,
    HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V2_SCENARIO_NAMES,
    MARKETPLACE_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES,
    MARKETPLACE_PRODUCTION_SECURITY_BLOCKED_CLAIMS,
    MARKETPLACE_PRODUCTION_SECURITY_CLAIM_BOUNDARY,
    MARKETPLACE_SECURITY_CERTIFICATION_TRACK_V1_SCENARIO_NAMES,
    PRODUCTION_SECURE_MARKETPLACE_LIVE_OPS_V2_SCENARIO_NAMES,
    PUBLISHER_TRUST_VULNERABILITY_OPS_V1_SCENARIO_NAMES,
    REQUIRED_HOSTILE_V2_DRILLS,
    REQUIRED_MARKETPLACE_LIVE_OPS,
    REQUIRED_SUPPLY_CHAIN_FIELDS,
    build_marketplace_production_security_contract,
    build_marketplace_production_security_report,
)


def test_marketplace_production_security_contract_exposes_dn_boundary_and_gates():
    contract = build_marketplace_production_security_contract()
    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["operator_status"] == "marketplace_production_security_receipts_visible"
    assert summary["claim_boundary"] == MARKETPLACE_PRODUCTION_SECURITY_CLAIM_BOUNDARY
    assert summary["certification_track_review_count"] >= 4
    assert summary["live_ops_receipt_count"] >= len(REQUIRED_MARKETPLACE_LIVE_OPS)
    assert summary["supply_chain_operation_count"] >= 6
    assert summary["hostile_gauntlet_v2_count"] >= len(REQUIRED_HOSTILE_V2_DRILLS)
    assert summary["publisher_vulnerability_ops_count"] >= 5
    assert summary["production_secure_marketplace_claim_allowed"] is False
    assert summary["third_party_package_security_solved_claim_allowed"] is False
    assert summary["formal_certification_claim_allowed"] is False
    assert summary["full_marketplace_parity_claim_allowed"] is False
    assert set(MARKETPLACE_PRODUCTION_SECURITY_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "/api/operator/marketplace-production-security" in policy["receipt_surfaces"]


def test_marketplace_production_security_supply_chain_blocks_risky_packages():
    contract = build_marketplace_production_security_contract()
    summary = contract["summary"]
    supply_chain = {item["package_id"]: item for item in contract["supply_chain_operations"]}
    promoted = [
        item for item in supply_chain.values()
        if item["promotion_decision"] in {"allow_promote", "staged_rollout"}
    ]

    assert summary["required_supply_chain_fields_visible"] is True
    assert summary["promoted_package_proof_complete"] is True
    assert summary["blocked_or_held_risky_package_count"] >= 4
    assert summary["critical_high_denied_count"] >= 3
    assert all(set(REQUIRED_SUPPLY_CHAIN_FIELDS) <= set(item["operator_visible_fields"]) for item in supply_chain.values())
    assert all(item["signature_status"] == "verified" for item in promoted)
    assert all(item["publisher_key_state"] == "active" for item in promoted)
    assert all(item["revocation_status"] == "not_revoked" for item in promoted)
    assert all(item["freshness_status"] == "current" for item in promoted)
    assert all(item["quarantine"]["state"] == "not_quarantined" for item in promoted)
    assert supply_chain["marketplace.suspicious-exporter"]["promotion_decision"] == "deny_and_quarantine"
    assert supply_chain["marketplace.suspicious-exporter"]["signature_status"] == "missing"
    assert supply_chain["marketplace.analytics-export"]["publisher_key_state"] == "revoked"
    assert supply_chain["marketplace.analytics-export"]["enforcement"]["status"] == "denied"
    assert supply_chain["marketplace.legacy-connector"]["freshness_status"] == "stale"


def test_marketplace_production_security_hostile_and_publisher_ops_fail_closed():
    contract = build_marketplace_production_security_contract()
    summary = contract["summary"]
    hostile = {item["drill_class"]: item for item in contract["hostile_package_lifecycle_gauntlet_v2"]}
    publishers = {item["publisher_id"]: item for item in contract["publisher_trust_vulnerability_ops"]}

    assert summary["required_hostile_v2_drills_covered"] is True
    assert summary["hostile_gauntlet_v2_fail_closed"] is True
    assert set(REQUIRED_HOSTILE_V2_DRILLS) <= set(hostile)
    assert hostile["private_network_ssrf"]["private_network_decision"] == "denied"
    assert hostile["dns_rebind_redirect"]["dns_rebind_decision"] == "denied"
    assert hostile["workspace_escape"]["workspace_egress_decision"] == "denied"
    assert hostile["malicious_update"]["enforcement"]["status"] == "denied"
    assert all(item["runtime_contribution_allowed"] is False for item in hostile.values())
    assert all(item["quarantine"]["state"] == "quarantined" for item in hostile.values())
    assert summary["scanner_freshness_computed"] is True
    assert publishers["pub.revoked.analytics"]["operator_action"] == "deny_and_quarantine"
    assert publishers["pub.verified.legacy"]["operator_action"] == "deny_until_rescan"
    assert all(item["critical_high_blocked"] is True for item in publishers.values())


def test_marketplace_production_security_false_claim_scan_and_redaction():
    contract = build_marketplace_production_security_contract()
    summary = contract["summary"]
    claim_scan = contract["marketplace_false_claim_scan"]
    encoded = json.dumps(contract, sort_keys=True)

    assert summary["false_claim_scan_clean"] is True
    assert claim_scan["forbidden_hit_count"] == 0
    assert set(MARKETPLACE_PRODUCTION_SECURITY_BLOCKED_CLAIMS) <= set(claim_scan["blocked_claims_checked"])
    assert claim_scan["claim_lift_allowed"] is False
    assert summary["safe_receipts_redacted"] is True
    assert "/Users/" not in encoded
    assert "file://" not in encoded
    assert ".env" not in encoded
    assert "id_rsa" not in encoded
    assert "sk-" not in encoded
    for group in (
        contract["certification_track_reviews"],
        contract["live_ops_receipts_v2"],
        contract["supply_chain_operations"],
        contract["hostile_package_lifecycle_gauntlet_v2"],
        contract["publisher_trust_vulnerability_ops"],
        [claim_scan],
    ):
        for item in group:
            receipt = item["safe_receipt"]
            assert receipt["contains_secret"] is False
            assert receipt["contains_private_path"] is False
            assert receipt["contains_raw_package_path"] is False
            assert receipt["contains_raw_transcript"] is False
            assert receipt["raw_receipt_path_exposed"] is False
            assert receipt["workspace_dir_exposed"] is False
            assert receipt["package_path_exposed"] is False
            assert receipt["redaction"] == "metadata_only_receipt_handle"
            assert receipt["redaction_layer"] == "marketplace_production_security_v1"
            assert receipt["redaction_degraded"] is False
            assert len(receipt["evidence_body_digest"]) == 64
            assert receipt["sanitized_payload_digest"] == receipt["evidence_body_digest"]
            assert len(receipt["tamper_evident_digest"]) == 64
            assert receipt["tamper_evident_digest"] != receipt["evidence_body_digest"]


def test_marketplace_production_security_report_runs_all_batch_dn_suites():
    payload = asyncio.run(build_marketplace_production_security_report())

    assert payload["summary"]["benchmark_posture"] == "marketplace_production_security_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(MARKETPLACE_SECURITY_CERTIFICATION_TRACK_V1_SCENARIO_NAMES)
        + len(PRODUCTION_SECURE_MARKETPLACE_LIVE_OPS_V2_SCENARIO_NAMES)
        + len(ECOSYSTEM_SUPPLY_CHAIN_OPERATIONS_V1_SCENARIO_NAMES)
        + len(HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V2_SCENARIO_NAMES)
        + len(PUBLISHER_TRUST_VULNERABILITY_OPS_V1_SCENARIO_NAMES)
        + len(MARKETPLACE_FALSE_CLAIM_SCAN_V1_SCENARIO_NAMES)
    )
    assert payload["summary"]["active_failure_count"] == 0
    assert payload["failure_report"] == []
    assert payload["scenario_names"]["marketplace_security_certification_track_v1"] == list(
        MARKETPLACE_SECURITY_CERTIFICATION_TRACK_V1_SCENARIO_NAMES
    )
