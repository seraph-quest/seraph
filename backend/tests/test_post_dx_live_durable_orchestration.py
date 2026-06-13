import json
from unittest.mock import AsyncMock, patch

import pytest

from src.workflows.post_dx_live_durable_orchestration import (
    POST_DX_LIVE_DURABLE_ORCHESTRATION_BLOCKED_CLAIMS,
    POST_DX_LIVE_DURABLE_ORCHESTRATION_CLAIM_BOUNDARY,
    POST_DX_LIVE_DURABLE_ORCHESTRATION_CLAIM_SCAN_COMMAND,
    build_post_dx_live_durable_orchestration_contract,
    build_post_dx_live_durable_orchestration_report,
)


def test_post_dx_live_durable_orchestration_contract_exposes_bounded_receipts():
    contract = build_post_dx_live_durable_orchestration_contract()
    summary = contract["summary"]
    policy = contract["policy"]

    assert summary["operator_status"] == "post_dx_live_durable_orchestration_visible"
    assert summary["recorded_live_window_count"] >= 2
    assert summary["accelerated_fixture_window_count"] >= 1
    assert summary["total_window_hours"] >= 432
    assert summary["all_windows_with_residual_risk"] is True
    assert summary["all_failovers_within_budget"] is True
    assert summary["restart_preservation_count"] >= 3
    assert summary["all_handoffs_revision_guarded"] is True
    assert summary["handoff_fail_closed_count"] >= 3
    assert summary["all_side_effects_have_idempotency"] is True
    assert summary["all_duplicate_attempts_suppressed"] is True
    assert summary["manual_repair_required_count"] >= 1
    assert summary["operator_control_count"] >= 9
    assert summary["false_claim_scan_count"] >= 3
    assert summary["all_recorded_live_windows_have_stored_provenance"] is True
    assert summary["all_false_claim_scans_command_backed"] is True
    assert summary["all_gate_checks_passed"] is True
    assert summary["claim_boundary"] == POST_DX_LIVE_DURABLE_ORCHESTRATION_CLAIM_BOUNDARY
    assert all(contract["gate_checks"].values())
    assert set(POST_DX_LIVE_DURABLE_ORCHESTRATION_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "/api/operator/post-dx-live-durable-orchestration" in policy["operator_surfaces"]
    assert "crash_proof_orchestration" in policy["not_claimed"]
    assert "full_parity_or_reference_system_exceedance" in policy["not_claimed"]
    assert "batch_dq" in contract["receipt_index"]["predecessor_claim_boundaries"]
    assert "batch_di" in contract["receipt_index"]["predecessor_claim_boundaries"]


def test_post_dx_live_durable_orchestration_receipts_keep_residual_risk_and_redaction():
    contract = build_post_dx_live_durable_orchestration_contract()
    serialized = json.dumps(contract, sort_keys=True)

    assert all(item["residual_risk"] for item in contract["recorded_live_window_receipts"])
    assert all(item["source_receipt_handle"] for item in contract["recorded_live_window_receipts"])
    assert all(item["runtime_fetch_performed"] is False for item in contract["recorded_live_window_receipts"])
    assert all(
        item["command"] == POST_DX_LIVE_DURABLE_ORCHESTRATION_CLAIM_SCAN_COMMAND
        and item["command_exit_code"] == 0
        and item["forbidden_hit_count"] == 0
        for item in contract["false_claim_scan_receipts"]
    )
    assert all(item["raw_receipt_handle"].startswith("receipt://batch-dy/") for item in contract["crash_restart_failover_receipts"])
    assert all(item["redacted_receipt_handle"].startswith("receipt://batch-dy/") for item in contract["side_effect_reconciliation_v6_receipts"])
    assert all("idempotency_key_digest" in item for item in contract["side_effect_reconciliation_v6_receipts"])
    assert "operator_confirmation_required_before_retry" in serialized
    assert "unconditional_exactly_once" in serialized
    assert "raw-idempotency-key" not in serialized


@pytest.mark.asyncio
async def test_post_dx_live_durable_orchestration_report_keeps_receipt_story_on_pass():
    latest = {
        "scenario_count": 23,
        "passed": 23,
        "failed": 0,
        "suite_names": ["post_dx_live_durable_orchestration_v1"],
    }

    with patch(
        "src.workflows.post_dx_live_durable_orchestration._run_post_dx_live_durable_orchestration_suites",
        AsyncMock(return_value=latest),
    ):
        report = await build_post_dx_live_durable_orchestration_report()

    assert report["summary"]["benchmark_posture"] == (
        "bounded_post_dx_live_durable_orchestration_parity_proof"
    )
    assert report["summary"]["suite_count"] == 1
    assert report["summary"]["scenario_count"] == 23
    assert report["summary"]["failed"] == 0
    assert report["failure_report"] == []
    assert report["policy"]["claim_boundary"] == POST_DX_LIVE_DURABLE_ORCHESTRATION_CLAIM_BOUNDARY


@pytest.mark.asyncio
async def test_post_dx_live_durable_orchestration_report_degrades_on_failures():
    latest = {
        "scenario_count": 23,
        "passed": 22,
        "failed": 1,
        "suite_names": ["post_dx_live_durable_orchestration_v1"],
    }

    with patch(
        "src.workflows.post_dx_live_durable_orchestration._run_post_dx_live_durable_orchestration_suites",
        AsyncMock(return_value=latest),
    ):
        report = await build_post_dx_live_durable_orchestration_report()

    assert report["summary"]["benchmark_posture"] == (
        "post_dx_live_durable_orchestration_regressions_detected"
    )
    assert report["failure_report"][0]["suite"] == "post_dx_live_durable_orchestration_v1"
