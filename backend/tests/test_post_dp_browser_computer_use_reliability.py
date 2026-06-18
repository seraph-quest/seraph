import asyncio
from copy import deepcopy

from src.extensions.post_dp_browser_computer_use_reliability import (
    BROWSER_COMPUTER_USE_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES,
    BROWSER_CREDENTIALED_RECOVERY_V2_SCENARIO_NAMES,
    BROWSER_HOSTILE_PAGE_SAFETY_V2_SCENARIO_NAMES,
    BROWSER_LIVE_PROVIDER_RELIABILITY_V2_SCENARIO_NAMES,
    BROWSER_PROVIDER_DEGRADATION_V2_SCENARIO_NAMES,
    BROWSER_SESSION_BOUNDARY_ENFORCEMENT_V3_SCENARIO_NAMES,
    BROWSER_SITE_DRIFT_RECOVERY_V3_SCENARIO_NAMES,
    POST_DP_BROWSER_COMPUTER_USE_RELIABILITY_BLOCKED_CLAIMS,
    POST_DP_BROWSER_COMPUTER_USE_RELIABILITY_CLAIM_BOUNDARY,
    POST_DP_BROWSER_COMPUTER_USE_RELIABILITY_SCENARIO_NAMES,
    REQUIRED_DW_BOUNDARIES,
    REQUIRED_DW_DEGRADATION_CASES,
    REQUIRED_DW_HOSTILE_CASES,
    REQUIRED_DW_PROVIDER_MODES,
    REQUIRED_DW_RECOVERY_CASES,
    REQUIRED_DW_SITE_DRIFT_CASES,
    build_post_dp_browser_computer_use_reliability_contract,
    build_post_dp_browser_computer_use_reliability_report,
    validate_post_dp_browser_computer_use_reliability_contract,
)


def _all_receipts(contract):
    return [
        *contract["provider_reliability"],
        *contract["session_boundary_enforcement"],
        *contract["credentialed_recovery"],
        *contract["site_drift_recovery"],
        *contract["hostile_page_safety"],
        *contract["provider_degradation"],
        contract["false_claim_scan"],
    ]


def test_post_dp_browser_computer_use_reliability_contract_blocks_overclaims_and_duplicates():
    contract = build_post_dp_browser_computer_use_reliability_contract()
    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["operator_status"] == "post_dp_browser_computer_use_reliability_receipts_visible"
    assert summary["claim_boundary"] == POST_DP_BROWSER_COMPUTER_USE_RELIABILITY_CLAIM_BOUNDARY
    assert summary["safe_browser_automation_claim_allowed"] is False
    assert summary["full_browser_parity_claim_allowed"] is False
    assert summary["openclaw_class_browser_reach_claim_allowed"] is False
    assert summary["production_ready_claim_allowed"] is False
    assert summary["reference_systems_exceeded_claim_allowed"] is False
    assert summary["prior_do_boundary_visible"] is True
    assert set(POST_DP_BROWSER_COMPUTER_USE_RELIABILITY_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert set(POST_DP_BROWSER_COMPUTER_USE_RELIABILITY_BLOCKED_CLAIMS) <= set(policy["not_claimed"])
    assert {item["predecessor_issue"] for item in policy["non_duplicate_delta_matrix"]} == {
        "#496",
        "#511",
        "#529",
        "#546",
        "#561",
        "#563",
    }
    assert all(item["dw_delta"] for item in policy["non_duplicate_delta_matrix"])


def test_post_dp_browser_computer_use_reliability_receipts_cover_provider_boundaries_and_recovery():
    contract = build_post_dp_browser_computer_use_reliability_contract()
    summary = contract["summary"]

    assert summary["required_provider_modes_covered"] is True
    assert set(REQUIRED_DW_PROVIDER_MODES) <= {item["provider_mode"] for item in contract["provider_reliability"]}
    assert summary["provider_identity_visible"] is True
    assert summary["provider_degradation_operator_visible"] is True
    assert summary["silent_fallback_blocked"] is True
    assert summary["required_boundaries_covered"] is True
    assert set(REQUIRED_DW_BOUNDARIES) <= {item["boundary"] for item in contract["session_boundary_enforcement"]}
    assert summary["all_boundaries_enforced"] is True
    assert summary["existing_session_unpartitioned_blocked"] is True
    assert summary["boundary_leak_count"] == 0
    assert summary["credentialed_recovery_cases_covered"] is True
    assert set(REQUIRED_DW_RECOVERY_CASES) <= {item["recovery_case"] for item in contract["credentialed_recovery"]}
    assert summary["credentialed_recovery_preserves_partitions"] is True
    assert summary["credentialed_recovery_fails_closed"] is True
    assert summary["site_drift_cases_covered"] is True
    assert set(REQUIRED_DW_SITE_DRIFT_CASES) <= {item["drift_case"] for item in contract["site_drift_recovery"]}
    assert summary["site_drift_preserves_approval_audit_partition"] is True
    assert summary["hostile_cases_covered"] is True
    assert set(REQUIRED_DW_HOSTILE_CASES) <= {item["hostile_case"] for item in contract["hostile_page_safety"]}
    assert summary["hostile_cases_fail_closed"] is True
    assert summary["provider_degradation_cases_covered"] is True
    assert set(REQUIRED_DW_DEGRADATION_CASES) <= {
        item["degradation_case"] for item in contract["provider_degradation"]
    }
    assert summary["provider_degradation_fails_closed"] is True


def test_post_dp_browser_computer_use_reliability_artifacts_are_provenanced_and_redacted():
    contract = build_post_dp_browser_computer_use_reliability_contract()
    summary = contract["summary"]

    assert summary["artifact_provenance_complete"] is True
    assert summary["artifact_secret_scan_clean"] is True
    assert summary["safe_receipts_redacted"] is True
    for item in _all_receipts(contract):
        assert item["run_id"] == "batch-dw-browser-reliability-2026-06-12"
        assert item["artifact_handle"].startswith("seraph://receipts/batch-dw/")
        assert len(item["artifact_body_digest"]) == 64
        assert item["artifact_secret_scan_status"] == "passed"
        assert item["raw_artifact_body_exposed"] is False
        receipt = item["safe_receipt"]
        assert receipt["redaction_layer"] == "post_dp_browser_computer_use_reliability_v1"
        assert receipt["redaction_degraded"] is False
        assert receipt["contains_secret"] is False
        assert receipt["contains_cookie"] is False
        assert receipt["contains_raw_dom"] is False
        assert receipt["contains_screenshot"] is False
        assert receipt["contains_clipboard_content"] is False
        assert receipt["contains_downloaded_filename"] is False
        assert receipt["contains_private_page_content"] is False
        assert receipt["contains_profile_dir"] is False
        assert receipt["contains_download_path"] is False
        assert len(receipt["evidence_body_digest"]) == 64
        assert len(receipt["sanitized_payload_digest"]) == 64
        assert receipt["tamper_evident_digest"] != receipt["evidence_body_digest"]


def test_post_dp_browser_computer_use_reliability_negative_validator_catches_missing_proof():
    contract = build_post_dp_browser_computer_use_reliability_contract()
    broken = deepcopy(contract)
    broken["provider_reliability"][0].pop("artifact_body_digest")
    broken["provider_reliability"][1]["silent_fallback_allowed"] = True
    broken["credentialed_recovery"][0]["session_partition_preserved"] = False
    broken["session_boundary_enforcement"][0]["safe_receipt"]["contains_raw_dom"] = True

    validation = validate_post_dp_browser_computer_use_reliability_contract(broken)

    assert validation["passes"] is False
    assert validation["regressions_detected"] is True
    assert validation["failure_count"] >= 4
    assert broken["provider_reliability"][0]["receipt_id"] in validation["missing_required_receipt_fields"]
    assert broken["provider_reliability"][1]["receipt_id"] in validation["silent_fallback_failures"]
    assert broken["credentialed_recovery"][0]["receipt_id"] in validation["stale_partition_failures"]
    assert broken["session_boundary_enforcement"][0]["receipt_id"] in validation["redaction_failures"]


def test_post_dp_browser_computer_use_reliability_false_claim_scan_is_command_backed():
    contract = build_post_dp_browser_computer_use_reliability_contract()
    false_claim_scan = contract["false_claim_scan"]

    assert false_claim_scan["command_executed"] is True
    assert false_claim_scan["command_exit_code"] == 0
    assert false_claim_scan["scan_clean"] is True
    assert false_claim_scan["forbidden_hit_count"] == 0
    assert set(POST_DP_BROWSER_COMPUTER_USE_RELIABILITY_BLOCKED_CLAIMS) <= set(
        false_claim_scan["blocked_claims_checked"]
    )
    assert false_claim_scan["claim_lift_allowed"] is False


def test_post_dp_browser_computer_use_reliability_report_runs_all_gates():
    report = asyncio.run(build_post_dp_browser_computer_use_reliability_report())
    summary = report["summary"]

    assert summary["benchmark_posture"] == "post_dp_browser_computer_use_reliability_ci_gated_operator_visible"
    assert summary["active_failure_count"] == 0
    assert summary["scenario_count"] == (
        len(POST_DP_BROWSER_COMPUTER_USE_RELIABILITY_SCENARIO_NAMES)
        + len(BROWSER_LIVE_PROVIDER_RELIABILITY_V2_SCENARIO_NAMES)
        + len(BROWSER_SESSION_BOUNDARY_ENFORCEMENT_V3_SCENARIO_NAMES)
        + len(BROWSER_CREDENTIALED_RECOVERY_V2_SCENARIO_NAMES)
        + len(BROWSER_SITE_DRIFT_RECOVERY_V3_SCENARIO_NAMES)
        + len(BROWSER_HOSTILE_PAGE_SAFETY_V2_SCENARIO_NAMES)
        + len(BROWSER_PROVIDER_DEGRADATION_V2_SCENARIO_NAMES)
        + len(BROWSER_COMPUTER_USE_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES)
    )
    assert report["latest_run"]["failed"] == 0
    assert report["failure_report"]["regression_detected"] is False
