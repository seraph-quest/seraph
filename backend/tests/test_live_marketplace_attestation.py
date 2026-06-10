import asyncio

from src.extensions.live_marketplace_attestation import (
    LIVE_MARKETPLACE_ATTESTATION_BLOCKED_CLAIMS,
    LIVE_MARKETPLACE_ATTESTATION_CLAIM_BOUNDARY,
    MARKETPLACE_OPERATIONS_INCIDENT_DRILL_SCENARIO_NAMES,
    PUBLISHER_REVIEW_AND_PACKAGE_TRUST_SCENARIO_NAMES,
    THIRD_PARTY_MARKETPLACE_ATTESTATION_SCENARIO_NAMES,
    build_live_marketplace_attestation_contract,
    build_live_marketplace_attestation_report,
)


def test_live_marketplace_attestation_contract_exposes_attestation_and_boundary():
    contract = build_live_marketplace_attestation_contract()
    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["operator_status"] == "live_marketplace_attestation_receipts_visible"
    assert summary["attested_package_count"] == 4
    assert summary["recorded_live_operation_count"] == 6
    assert summary["publisher_review_count"] == 4
    assert summary["claim_boundary"] == LIVE_MARKETPLACE_ATTESTATION_CLAIM_BOUNDARY
    assert set(LIVE_MARKETPLACE_ATTESTATION_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "/api/operator/live-marketplace-attestation-proof" in policy["receipt_surfaces"]


def test_live_marketplace_attestation_receipts_include_fail_closed_incidents():
    contract = build_live_marketplace_attestation_contract()

    operations = {item["operation_id"]: item for item in contract["operations"]}
    suspicious = next(
        item for item in contract["third_party_attestations"]
        if item["package_id"] == "marketplace.suspicious-exporter"
    )

    assert suspicious["signature_status"] == "missing"
    assert suspicious["publisher_verification"] == "unverified"
    assert suspicious["compatibility"] == "blocked"
    assert operations["cg-malicious-exporter"]["state"] == "quarantined"
    assert operations["cg-malicious-exporter"]["fails_closed"] is True
    assert operations["cg-failed-update-recovery"]["state"] == "rolled_back"
    assert operations["cg-quarantine-reentry-review"]["state"] == "reentry_denied"


def test_live_marketplace_publisher_reviews_explain_trust_and_stale_review():
    contract = build_live_marketplace_attestation_contract()
    publishers = {item["publisher_id"]: item for item in contract["publisher_reviews"]}

    assert publishers["pub.verified.seraph-labs"]["key_rotation_status"] == "rotated_with_receipt"
    assert publishers["pub.unverified.unknown"]["review_state"] == "stale_or_missing"
    assert publishers["pub.unverified.unknown"]["operator_action"] == "deny_and_quarantine"
    assert all(item["trust_explanation"] for item in publishers.values())


def test_live_marketplace_attestation_report_runs_all_batch_cg_suites():
    payload = asyncio.run(build_live_marketplace_attestation_report())

    assert payload["summary"]["benchmark_posture"] == "live_marketplace_attestation_ci_gated_operator_visible"
    assert payload["summary"]["scenario_count"] == (
        len(THIRD_PARTY_MARKETPLACE_ATTESTATION_SCENARIO_NAMES)
        + len(MARKETPLACE_OPERATIONS_INCIDENT_DRILL_SCENARIO_NAMES)
        + len(PUBLISHER_REVIEW_AND_PACKAGE_TRUST_SCENARIO_NAMES)
    )
    assert payload["summary"]["active_failure_count"] == 0
    assert payload["failure_report"] == []
    assert payload["policy"]["claim_boundary"] == LIVE_MARKETPLACE_ATTESTATION_CLAIM_BOUNDARY
