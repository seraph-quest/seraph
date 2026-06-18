from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.workflows.benchmark import build_workflow_endurance_benchmark_report
from src.workflows.endurance_canary import build_live_workflow_endurance_canary_report


@pytest.mark.asyncio
async def test_workflow_endurance_benchmark_report_degrades_summary_states_on_failures():
    summary = SimpleNamespace(
        total=4,
        passed=3,
        failed=1,
        duration_ms=110,
        results=[
            SimpleNamespace(
                name="workflow_condensation_fidelity_behavior",
                passed=False,
                error="condensation fidelity regressed",
            )
        ],
    )

    with patch(
        "src.workflows.benchmark._run_workflow_endurance_benchmark_suite",
        AsyncMock(return_value=summary),
    ):
        report = await build_workflow_endurance_benchmark_report()

    assert report["summary"]["benchmark_posture"] == "ci_regressions_detected_operator_visible"
    assert report["summary"]["active_failure_count"] == 1
    assert report["summary"]["anticipatory_repair_state"] == "regressions_detected"
    assert report["summary"]["condensation_fidelity_state"] == "regressions_detected"
    assert report["summary"]["branch_continuity_state"] == "regressions_detected"
    assert report["failure_report"][0]["scenario_name"] == "workflow_condensation_fidelity_behavior"


@pytest.mark.asyncio
async def test_workflow_endurance_benchmark_report_keeps_healthy_summary_states_on_pass():
    summary = SimpleNamespace(
        total=4,
        passed=4,
        failed=0,
        duration_ms=88,
        results=[
            SimpleNamespace(
                name="workflow_anticipatory_repair_behavior",
                passed=True,
                error=None,
            )
        ],
    )

    with patch(
        "src.workflows.benchmark._run_workflow_endurance_benchmark_suite",
        AsyncMock(return_value=summary),
    ):
        report = await build_workflow_endurance_benchmark_report()

    assert report["summary"]["benchmark_posture"] == "ci_gated_operator_visible"
    assert report["summary"]["anticipatory_repair_state"] == "checkpoint_and_pre_repair_visible"
    assert report["summary"]["condensation_fidelity_state"] == "recovery_paths_and_output_history_retained"
    assert report["summary"]["branch_continuity_state"] == "backup_branch_operator_selectable"
    assert report["failure_report"] == []


@pytest.mark.asyncio
async def test_live_workflow_endurance_canary_report_keeps_receipt_story_on_pass():
    summary = SimpleNamespace(
        total=4,
        passed=4,
        failed=0,
        duration_ms=84,
        results=[
            SimpleNamespace(
                name="live_workflow_canary_protocol_behavior",
                passed=True,
                error=None,
            )
        ],
    )

    with patch(
        "src.workflows.endurance_canary._run_live_workflow_endurance_canary_suite",
        AsyncMock(return_value=summary),
    ):
        report = await build_live_workflow_endurance_canary_report()

    assert report["summary"]["benchmark_posture"] == "live_workflow_canary_ci_gated_operator_visible"
    assert report["summary"]["session_count"] >= 2
    assert report["summary"]["failure_injection_count"] == 1
    assert report["summary"]["recovery_action_count"] == 1
    assert report["summary"]["trust_boundary_block_count"] == 1
    assert report["operator_story"]["failure_recovery_visible"] is True
    assert report["operator_story"]["approval_preservation_visible"] is True
    assert report["operator_story"]["trust_boundary_fail_closed_visible"] is True
    assert report["policy"]["claim_boundary"] == "audit_projected_replayable_canary_not_durable_workflow_engine"


@pytest.mark.asyncio
async def test_live_workflow_endurance_canary_report_degrades_on_failures():
    summary = SimpleNamespace(
        total=4,
        passed=3,
        failed=1,
        duration_ms=111,
        results=[
            SimpleNamespace(
                name="live_workflow_canary_failure_recovery_behavior",
                passed=False,
                error="canary recovery receipt regressed",
            )
        ],
    )

    with patch(
        "src.workflows.endurance_canary._run_live_workflow_endurance_canary_suite",
        AsyncMock(return_value=summary),
    ):
        report = await build_live_workflow_endurance_canary_report()

    assert report["summary"]["benchmark_posture"] == "live_workflow_canary_regressions_detected_operator_visible"
    assert report["summary"]["active_failure_count"] == 1
    assert report["failure_report"][0]["scenario_name"] == "live_workflow_canary_failure_recovery_behavior"
