import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.workflows.post_dp_durable_orchestration import (
    POST_DP_DURABLE_ORCHESTRATION_BLOCKED_CLAIMS,
    POST_DP_DURABLE_ORCHESTRATION_CLAIM_BOUNDARY,
    build_post_dp_durable_orchestration_contract,
    build_post_dp_durable_orchestration_report,
)


def test_post_dp_durable_orchestration_contract_exposes_redacted_recovery_packets():
    contract = build_post_dp_durable_orchestration_contract()
    summary = contract["summary"]
    policy = contract["policy"]
    packets = contract["recovery_packets"]

    assert summary["operator_status"] == "post_dp_durable_orchestration_gap_closure_visible"
    assert summary["packet_count"] >= 2
    assert summary["ready_recovery_count"] >= 1
    assert summary["blocked_recovery_count"] >= 1
    assert summary["metadata_preservation_count"] >= 1
    assert summary["handoff_block_count"] >= 1
    assert summary["transition_block_count"] >= 1
    assert summary["deduped_trigger_count"] >= 1
    assert summary["trigger_external_action_allowed_count"] == 0
    assert summary["side_effect_reconciliation_count"] >= 1
    assert summary["duplicate_suppression_count"] >= 1
    assert summary["guardian_restraint_count"] >= 2
    assert summary["unsafe_recovery_refusal_count"] >= 1
    assert summary["all_raw_payloads_redacted"] is True
    assert summary["claim_boundary"] == POST_DP_DURABLE_ORCHESTRATION_CLAIM_BOUNDARY
    assert set(POST_DP_DURABLE_ORCHESTRATION_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "/api/operator/post-dp-durable-orchestration" in policy["operator_surfaces"]
    assert "/api/operator/benchmark-proof" in policy["operator_surfaces"]
    assert all(packet["raw_payloads_redacted"] is True for packet in packets)
    assert all(packet["guardian_recovery"]["authority_expanded"] is False for packet in packets)
    assert all("approval_context_digest" in packet for packet in packets)
    assert all("approval_context" not in packet for packet in packets)
    assert all("run_identity" not in packet for packet in packets)
    assert all("workflow_name" not in packet for packet in packets)
    assert all("lease_id" not in packet for packet in packets)
    assert all("lease_owner" not in packet for packet in packets)
    assert all(packet["run_handle"].startswith("workflow-run:") for packet in packets)
    assert summary["replay_window_count"] >= 2
    serialized = json.dumps(contract, sort_keys=True)
    assert "dq_release_long_work" not in serialized
    assert "release-hardening-long-work" not in serialized
    assert "worker-a" not in serialized
    assert "dq-lease-worker-a" not in serialized
    assert "session:unrelated:run" not in serialized


@pytest.mark.asyncio
async def test_post_dp_durable_orchestration_report_keeps_receipt_story_on_pass():
    summary = SimpleNamespace(total=17, passed=17, failed=0, duration_ms=91, results=[])

    with patch(
        "src.workflows.post_dp_durable_orchestration._run_post_dp_durable_orchestration_suites",
        AsyncMock(return_value=summary),
    ):
        report = await build_post_dp_durable_orchestration_report()

    assert report["summary"]["benchmark_posture"] == "post_dp_durable_orchestration_ci_gated_operator_visible"
    assert report["summary"]["suite_count"] == 5
    assert report["summary"]["scenario_count"] == 17
    assert report["summary"]["trigger_external_action_allowed_count"] == 0
    assert report["summary"]["evidence_mode"] == "deterministic_fixture_receipts"
    assert report["summary"]["claim_boundary"] == POST_DP_DURABLE_ORCHESTRATION_CLAIM_BOUNDARY
    assert report["failure_report"] == []
    assert report["policy"]["claim_boundary"] == POST_DP_DURABLE_ORCHESTRATION_CLAIM_BOUNDARY
    serialized = json.dumps(report, sort_keys=True)
    assert "dq_release_long_work" not in serialized
    assert "release-hardening-long-work" not in serialized
    assert "worker-a" not in serialized
    assert "dq-lease-worker-a" not in serialized


@pytest.mark.asyncio
async def test_post_dp_durable_orchestration_report_degrades_on_failures():
    summary = SimpleNamespace(total=17, passed=16, failed=1, duration_ms=101, results=[])

    with patch(
        "src.workflows.post_dp_durable_orchestration._run_post_dp_durable_orchestration_suites",
        AsyncMock(return_value=summary),
    ):
        report = await build_post_dp_durable_orchestration_report()

    assert report["summary"]["benchmark_posture"] == "post_dp_durable_orchestration_regressions_detected"
    assert report["failure_report"][0]["suite"] == "post_dp_durable_orchestration_v1"


@pytest.mark.asyncio
async def test_post_dp_durable_orchestration_report_ignores_unrelated_persisted_runs():
    summary = SimpleNamespace(total=17, passed=17, failed=0, duration_ms=91, results=[])
    unrelated_run = {
        "run_identity": "session:unrelated:run",
        "workflow_name": "unrelated",
        "status": "completed",
        "metadata": {"orchestration_v2": {"revision": 1}},
        "approval_context": {"risk_level": "low"},
    }

    with (
        patch(
            "src.workflows.post_dp_durable_orchestration._run_post_dp_durable_orchestration_suites",
            AsyncMock(return_value=summary),
        ),
        patch(
            "src.workflows.post_dp_durable_orchestration.workflow_state_repository.list_runs",
            AsyncMock(return_value=[unrelated_run]),
        ),
    ):
        report = await build_post_dp_durable_orchestration_report()

    assert report["summary"]["persisted_run_count"] == 1
    assert report["summary"]["persisted_dq_run_count"] == 0
    assert report["summary"]["evidence_mode"] == "deterministic_fixture_receipts"
    assert report["summary"]["packet_count"] >= 2
    assert report["persisted_contract"]["summary"]["packet_count"] == 0
    assert report["persisted_contract"]["durable_workflow_v2_contract"]["summary"]["run_count"] == 0
    serialized_persisted = json.dumps(report["persisted_contract"], sort_keys=True)
    assert "dq_release_long_work" not in serialized_persisted
    assert "release-hardening-long-work" not in serialized_persisted
    assert "worker-a" not in serialized_persisted
    assert "dq-lease-worker-a" not in serialized_persisted
    assert "session:unrelated:run" not in serialized_persisted
