"""Tests for Batch DU post-DP operator debugging/recovery-control receipts."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.cockpit.post_dp_operator_debugging_recovery import (
    AUTHORITY_TRANSFER_INTEGRITY_V2_SCENARIO_NAMES,
    AUTHORITY_TRANSFER_INTEGRITY_V2_SUITE_NAME,
    DENSE_LONG_WORK_DEBUGGING_V2_SCENARIO_NAMES,
    DENSE_LONG_WORK_DEBUGGING_V2_SUITE_NAME,
    OPERATOR_AUDIT_ACCESSIBILITY_V2_SCENARIO_NAMES,
    OPERATOR_AUDIT_ACCESSIBILITY_V2_SUITE_NAME,
    OPERATOR_CONTROL_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES,
    OPERATOR_CONTROL_FALSE_CLAIM_SCAN_V2_SUITE_NAME,
    OPERATOR_EFFORT_REDUCTION_V2_SCENARIO_NAMES,
    OPERATOR_EFFORT_REDUCTION_V2_SUITE_NAME,
    OPERATOR_RECOVERY_SLO_V3_SCENARIO_NAMES,
    OPERATOR_RECOVERY_SLO_V3_SUITE_NAME,
    POST_DP_OPERATOR_DEBUGGING_RECOVERY_CONTROL_BLOCKED_CLAIMS,
    POST_DP_OPERATOR_DEBUGGING_RECOVERY_CONTROL_CLAIM_BOUNDARY,
    POST_DP_OPERATOR_DEBUGGING_RECOVERY_CONTROL_SCENARIO_NAMES,
    POST_DP_OPERATOR_DEBUGGING_RECOVERY_CONTROL_SUITE_NAME,
    POST_DP_OPERATOR_DEBUGGING_RECOVERY_CONTROL_SURFACE,
    REQUIRED_DU_OPERATOR_CONTROLS,
    build_post_dp_operator_debugging_recovery_contract,
    build_post_dp_operator_debugging_recovery_report,
)


def test_post_dp_operator_debugging_recovery_covers_required_control_matrix():
    contract = build_post_dp_operator_debugging_recovery_contract()
    summary = contract["summary"]
    controls = contract["operator_recovery_slo_v3_receipts"]
    flow_receipts = contract["operator_recovery_control_flow_receipts"]

    assert summary["operator_status"] == "post_dp_operator_debugging_recovery_control_receipts_visible"
    assert summary["claim_boundary"] == POST_DP_OPERATOR_DEBUGGING_RECOVERY_CONTROL_CLAIM_BOUNDARY
    assert summary["required_controls_visible"] is True
    assert summary["control_count"] == len(REQUIRED_DU_OPERATOR_CONTROLS)
    assert set(REQUIRED_DU_OPERATOR_CONTROLS) == {item["action"] for item in controls}
    assert all(item["authority_visible"] is True for item in controls)
    assert all(item["receipt_after_action"].startswith("operator-control-du:") for item in controls)
    assert all(item["recovery_correctness_check"] for item in controls)
    assert all(item["affected_artifacts_visible"] is True for item in controls)
    assert all(item["recovery_options_visible"] is True for item in controls)
    assert summary["all_exercised_control_flows_passed"] is True
    assert summary["exercised_control_flow_count"] == len(controls)
    assert summary["stale_approval_exercise_count"] >= 1
    assert summary["broadened_scope_denial_exercise_count"] >= 1
    assert summary["unsafe_denial_receipt_exercise_count"] >= 1
    assert summary["audit_write_exercise_count"] == len(flow_receipts)
    assert all(item["flow_passed"] is True for item in flow_receipts)
    assert all(item["operator_authority_checked"] is True for item in flow_receipts)
    assert all(len(item["audit_digest"]) == 64 for item in flow_receipts)


def test_post_dp_operator_debugging_recovery_exposes_debugging_receipts():
    contract = build_post_dp_operator_debugging_recovery_contract()
    summary = contract["summary"]
    debugging = contract["dense_long_work_debugging_v2_receipts"]

    assert summary["debugging_receipt_count"] >= 4
    assert summary["root_cause_visible_count"] == len(debugging)
    assert summary["affected_artifact_receipt_count"] == len(debugging)
    assert summary["recovery_options_visible_count"] == len(debugging)
    assert summary["stale_approval_fail_closed_count"] >= 1
    assert summary["unsafe_denial_block_count"] >= 1
    assert {
        "validation_timeout_after_provider_delay",
        "artifact_digest_mismatch_after_handoff",
        "approval_scope_stale_after_context_shift",
        "denial_without_reason_or_receipt_requested",
    } <= {item["root_cause"] for item in debugging}
    assert all(item["recovery_option_count"] >= 3 for item in debugging)
    assert any(item["stale_approval_detected"] is True for item in debugging)
    assert any(item["unsafe_denial_without_receipt_blocked"] is True for item in debugging)


def test_post_dp_operator_debugging_recovery_authority_transfer_fails_closed():
    contract = build_post_dp_operator_debugging_recovery_contract()
    summary = contract["summary"]
    authority = contract["authority_transfer_integrity_v2_receipts"]

    assert summary["authority_transfer_count"] >= 6
    assert summary["authority_transfer_fail_closed"] is True
    assert summary["broadened_scope_fail_closed_count"] == len(authority)
    assert {item["transfer_type"] for item in authority} >= {
        "handoff",
        "takeover",
        "replay",
        "rollback",
        "quarantine",
        "revoke",
    }
    assert all(item["scope_renewal_required"] is True for item in authority)
    assert all(item["checkpoint_digest_required"] is True for item in authority)
    assert all(item["stale_approval_fails_closed"] is True for item in authority)
    assert all(item["broadened_scope_fails_closed"] is True for item in authority)
    assert all(item["approval_reuse_allowed"] is False for item in authority)


def test_post_dp_operator_debugging_recovery_accessibility_and_redaction_receipts():
    contract = build_post_dp_operator_debugging_recovery_contract()
    summary = contract["summary"]
    accessibility = contract["operator_audit_accessibility_v2_receipts"]
    serialized = json.dumps(contract, sort_keys=True)

    assert summary["keyboard_receipt_count"] == len(accessibility)
    assert summary["accessibility_receipt_count"] == len(accessibility)
    assert summary["audit_digest_chain_linked"] is True
    assert summary["safe_receipts_redacted"] is True
    assert all(item["keyboard_only_path_complete"] is True for item in accessibility)
    assert all(item["focus_order_stable"] is True for item in accessibility)
    assert all(item["screen_reader_label"] for item in accessibility)
    assert all(item["accessibility_blockers"] == [] for item in accessibility)
    assert all(item["safe_redaction_verified"] is True for item in accessibility)
    assert "/Users/" not in serialized
    assert "file://" not in serialized
    assert ".env" not in serialized
    assert "secret://" not in serialized


def test_post_dp_operator_debugging_recovery_blocks_claims_and_records_real_scan_evidence():
    contract = build_post_dp_operator_debugging_recovery_contract()
    summary = contract["summary"]
    policy = contract["policy"]
    scans = contract["false_claim_scan_receipts"]

    assert set(POST_DP_OPERATOR_DEBUGGING_RECOVERY_CONTROL_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert summary["solved_operator_control_claim_allowed"] is False
    assert summary["best_cockpit_claim_allowed"] is False
    assert summary["production_ready_claim_allowed"] is False
    assert summary["full_parity_claim_allowed"] is False
    assert summary["false_claim_scan_clean"] is True
    assert summary["real_false_claim_command_evidence"] is True
    assert len(scans) == len(OPERATOR_CONTROL_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES)
    for scan in scans:
        evidence = scan["command_evidence"]
        assert scan["validation_command"] == "python3 scripts/check_strategy_claims.py"
        assert scan["forbidden_hit_count"] == 0
        assert scan["claim_lift_allowed"] is False
        assert evidence["executed"] is True
        assert evidence["executed_argv"] == ["python3", "scripts/check_strategy_claims.py"]
        assert evidence["returncode"] == 0
        assert evidence["timed_out"] is False
        assert evidence["timeout_seconds"] == 15
        assert scan["du_scope_evidence"]["match_count"] == 0
        assert evidence["safe_redaction"]["raw_stdout_exposed"] is False
        assert evidence["safe_redaction"]["raw_stderr_exposed"] is False
        assert len(evidence["stdout_sha256"]) == 64
        assert len(evidence["stderr_sha256"]) == 64


def test_post_dp_operator_debugging_recovery_policy_uses_requested_names():
    contract = build_post_dp_operator_debugging_recovery_contract()
    policy = contract["policy"]

    assert policy["operator_surface"] == POST_DP_OPERATOR_DEBUGGING_RECOVERY_CONTROL_SURFACE
    assert POST_DP_OPERATOR_DEBUGGING_RECOVERY_CONTROL_SURFACE in policy["receipt_surfaces"]
    assert policy["claim_boundary"] == POST_DP_OPERATOR_DEBUGGING_RECOVERY_CONTROL_CLAIM_BOUNDARY
    assert policy["benchmark_suites"] == [
        POST_DP_OPERATOR_DEBUGGING_RECOVERY_CONTROL_SUITE_NAME,
        DENSE_LONG_WORK_DEBUGGING_V2_SUITE_NAME,
        OPERATOR_RECOVERY_SLO_V3_SUITE_NAME,
        OPERATOR_EFFORT_REDUCTION_V2_SUITE_NAME,
        AUTHORITY_TRANSFER_INTEGRITY_V2_SUITE_NAME,
        OPERATOR_AUDIT_ACCESSIBILITY_V2_SUITE_NAME,
        OPERATOR_CONTROL_FALSE_CLAIM_SCAN_V2_SUITE_NAME,
    ]


def test_post_dp_operator_debugging_recovery_report_exposes_scenario_names():
    run_summary = SimpleNamespace(total=25, passed=25, failed=0, duration_ms=93, results=[])

    with patch(
        "src.cockpit.post_dp_operator_debugging_recovery._run_post_dp_operator_debugging_recovery_suites",
        AsyncMock(return_value=run_summary),
    ):
        report = asyncio.run(build_post_dp_operator_debugging_recovery_report())

    assert (
        report["summary"]["benchmark_posture"]
        == "post_dp_operator_debugging_recovery_control_ci_gated_operator_visible"
    )
    assert report["summary"]["scenario_count"] == (
        len(POST_DP_OPERATOR_DEBUGGING_RECOVERY_CONTROL_SCENARIO_NAMES)
        + len(DENSE_LONG_WORK_DEBUGGING_V2_SCENARIO_NAMES)
        + len(OPERATOR_RECOVERY_SLO_V3_SCENARIO_NAMES)
        + len(OPERATOR_EFFORT_REDUCTION_V2_SCENARIO_NAMES)
        + len(AUTHORITY_TRANSFER_INTEGRITY_V2_SCENARIO_NAMES)
        + len(OPERATOR_AUDIT_ACCESSIBILITY_V2_SCENARIO_NAMES)
        + len(OPERATOR_CONTROL_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES)
    )
    assert report["latest_run"]["failed"] == 0
    assert report["scenario_names"][POST_DP_OPERATOR_DEBUGGING_RECOVERY_CONTROL_SUITE_NAME] == list(
        POST_DP_OPERATOR_DEBUGGING_RECOVERY_CONTROL_SCENARIO_NAMES
    )
    assert report["scenario_names"][DENSE_LONG_WORK_DEBUGGING_V2_SUITE_NAME] == list(
        DENSE_LONG_WORK_DEBUGGING_V2_SCENARIO_NAMES
    )
    assert report["scenario_names"][OPERATOR_RECOVERY_SLO_V3_SUITE_NAME] == list(
        OPERATOR_RECOVERY_SLO_V3_SCENARIO_NAMES
    )
    assert report["scenario_names"][OPERATOR_EFFORT_REDUCTION_V2_SUITE_NAME] == list(
        OPERATOR_EFFORT_REDUCTION_V2_SCENARIO_NAMES
    )
    assert report["scenario_names"][AUTHORITY_TRANSFER_INTEGRITY_V2_SUITE_NAME] == list(
        AUTHORITY_TRANSFER_INTEGRITY_V2_SCENARIO_NAMES
    )
    assert report["scenario_names"][OPERATOR_AUDIT_ACCESSIBILITY_V2_SUITE_NAME] == list(
        OPERATOR_AUDIT_ACCESSIBILITY_V2_SCENARIO_NAMES
    )
    assert report["scenario_names"][OPERATOR_CONTROL_FALSE_CLAIM_SCAN_V2_SUITE_NAME] == list(
        OPERATOR_CONTROL_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES
    )
