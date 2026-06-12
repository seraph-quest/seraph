import asyncio

from src.extensions.post_dp_marketplace_lifecycle_gap_closure import (
    HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V3_SCENARIO_NAMES,
    MARKETPLACE_LIFECYCLE_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES,
    MARKETPLACE_LIFECYCLE_OPERATIONS_V3_SCENARIO_NAMES,
    MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_V2_SCENARIO_NAMES,
    MARKETPLACE_SECURE_HOST_AUDIT_INTEGRATION_V1_SCENARIO_NAMES,
    MARKETPLACE_VULNERABILITY_MONITORING_V2_SCENARIO_NAMES,
    PACKAGE_REVIEW_WAIVER_POLICY_V2_SCENARIO_NAMES,
    POST_DP_CAPABILITY_MARKETPLACE_LIFECYCLE_GAP_CLOSURE_SCENARIO_NAMES,
    POST_DP_MARKETPLACE_LIFECYCLE_BLOCKED_CLAIMS,
    POST_DP_MARKETPLACE_LIFECYCLE_CLAIM_BOUNDARY,
    REQUIRED_DIAGNOSTIC_CAUSES,
    REQUIRED_LIFECYCLE_OPERATIONS,
    REQUIRED_LIFECYCLE_RECEIPT_FIELDS,
    build_post_dp_marketplace_lifecycle_contract,
    build_post_dp_marketplace_lifecycle_report,
)


def test_post_dp_marketplace_lifecycle_contract_closes_dv_acceptance_scope():
    contract = build_post_dp_marketplace_lifecycle_contract()
    summary = contract["summary"]

    assert summary["operator_status"] == "post_dp_marketplace_lifecycle_gap_closure_receipts_visible"
    assert summary["claim_boundary"] == POST_DP_MARKETPLACE_LIFECYCLE_CLAIM_BOUNDARY
    assert summary["required_lifecycle_operations_covered"] is True
    assert summary["required_lifecycle_receipt_fields_visible"] is True
    assert summary["diagnostic_causes_covered"] is True
    assert summary["failed_update_and_rollback_diagnostics_visible"] is True
    assert summary["high_critical_denied_without_valid_waiver"] is True
    assert summary["explicit_waiver_scope_and_expiry_visible"] is True
    assert summary["expired_or_out_of_scope_waivers_denied"] is True
    assert summary["vulnerability_monitoring_fail_closed"] is True
    assert summary["hostile_gauntlet_v3_fail_closed"] is True
    assert summary["secure_host_permissions_integrated"] is True
    assert summary["operator_audit_receipts_visible"] is True
    assert summary["safe_receipts_redacted"] is True
    assert summary["false_claim_scan_clean"] is True
    assert summary["production_secure_marketplace_claim_allowed"] is False
    assert summary["third_party_package_security_solved_claim_allowed"] is False
    assert summary["full_marketplace_parity_claim_allowed"] is False
    assert set(POST_DP_MARKETPLACE_LIFECYCLE_BLOCKED_CLAIMS) <= set(contract["policy"]["blocked_claims"])
    claim_scan = contract["marketplace_lifecycle_false_claim_scan_v2"]
    assert claim_scan["command_executed"] is True
    assert claim_scan["command_exit_code"] == 0
    assert claim_scan["scan_clean"] is True
    assert claim_scan["forbidden_hit_count"] == 0
    assert claim_scan["stdout_digest"]
    assert claim_scan["stderr_digest"]

    assert set(REQUIRED_LIFECYCLE_OPERATIONS) <= {
        item["operation"] for item in contract["lifecycle_operations_v3"]
    }
    assert all(
        set(REQUIRED_LIFECYCLE_RECEIPT_FIELDS) <= set(item["operator_visible_fields"])
        for item in contract["lifecycle_operations_v3"]
    )
    assert set(REQUIRED_DIAGNOSTIC_CAUSES) <= {
        item["cause_class"] for item in contract["rollback_quarantine_diagnostics_v2"]
    }


def test_post_dp_marketplace_lifecycle_enforces_waivers_and_secure_host_audit():
    contract = build_post_dp_marketplace_lifecycle_contract()
    reviews = contract["package_review_waiver_policy_v2"]
    secure_host = contract["secure_host_audit_integration_v1"]

    assert all(
        item["decision"] != "allow_runtime_contribution"
        for item in reviews
        if item["severity"] in {"critical", "high"} and item["waiver_state"] != "scoped_current"
    )
    scoped = next(item for item in reviews if item["waiver_state"] == "scoped_current")
    assert scoped["waiver_id"]
    assert scoped["finding_id"]
    assert scoped["package_version"]
    assert scoped["package_digest"]
    assert scoped["publisher_id"]
    assert scoped["waiver_scope_explicit"] is True
    assert scoped["waiver_scope"]["package_id"] == scoped["package_id"]
    assert scoped["scope_digest"]
    assert scoped["allowed_operations"] == ["update"]
    assert scoped["permission_boundaries"]["network"] == ["public"]
    assert scoped["runtime_profile"] == "extension_package_quarantine_first_runtime"
    assert scoped["compensating_controls"] == ["staged_rollout", "operator_retest_before_reentry"]
    assert scoped["reviewer_id"] == "reviewer.marketplace.dv.2026q2"
    assert scoped["approved_at"] == "2026-06-12"
    assert scoped["waiver_expires_at"] == "2026-07-15"
    assert scoped["waiver_expired"] is False
    assert all(item["secure_host"]["permission_delta_reviewed"] is True for item in secure_host)
    assert all(item["secure_host"]["credential_scope_rechecked"] is True for item in secure_host)
    assert all(item["secure_host"]["egress_policy_rechecked"] is True for item in secure_host)
    assert all(item["operator_audit_receipt"] and item["audit_chain_digest"] for item in secure_host)


def test_post_dp_marketplace_lifecycle_report_runs_all_dv_suites():
    payload = asyncio.run(build_post_dp_marketplace_lifecycle_report())

    assert payload["summary"]["benchmark_posture"] == (
        "post_dp_marketplace_lifecycle_gap_closure_ci_gated_operator_visible"
    )
    assert payload["summary"]["scenario_count"] == (
        len(POST_DP_CAPABILITY_MARKETPLACE_LIFECYCLE_GAP_CLOSURE_SCENARIO_NAMES)
        + len(MARKETPLACE_LIFECYCLE_OPERATIONS_V3_SCENARIO_NAMES)
        + len(PACKAGE_REVIEW_WAIVER_POLICY_V2_SCENARIO_NAMES)
        + len(MARKETPLACE_VULNERABILITY_MONITORING_V2_SCENARIO_NAMES)
        + len(HOSTILE_PACKAGE_LIFECYCLE_GAUNTLET_V3_SCENARIO_NAMES)
        + len(MARKETPLACE_ROLLBACK_QUARANTINE_DIAGNOSTICS_V2_SCENARIO_NAMES)
        + len(MARKETPLACE_SECURE_HOST_AUDIT_INTEGRATION_V1_SCENARIO_NAMES)
        + len(MARKETPLACE_LIFECYCLE_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES)
    )
    assert payload["latest_run"]["failed"] == 0
    assert payload["failure_report"]["failure_count"] == 0
