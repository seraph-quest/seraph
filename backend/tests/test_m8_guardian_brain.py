from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.guardian.brain import build_m8_guardian_brain_receipts
from src.guardian.benchmark import build_m8_guardian_brain_benchmark_report


def test_m8_guardian_brain_receipts_cover_required_actions_and_capability_boundaries():
    receipts = build_m8_guardian_brain_receipts()
    by_scenario = {receipt["scenario_id"]: receipt for receipt in receipts}

    assert {receipt["action"] for receipt in receipts} == {
        "act",
        "bundle",
        "clarify",
        "defer",
        "request_approval",
        "stay_silent",
    }
    assert by_scenario["m8_capability_choice_act_behavior"]["selected_capability"]["id"] == "guardian.thread_continue"
    assert by_scenario["m8_ambiguous_evidence_clarify_behavior"]["selected_capability"] is None
    assert by_scenario["m8_stale_memory_defer_behavior"]["inputs"]["memory_freshness"] == "stale"
    assert by_scenario["m8_risky_capability_approval_behavior"]["selected_capability"]["requires_approval"] is True
    assert by_scenario["m8_risky_capability_approval_behavior"]["action"] == "request_approval"
    assert by_scenario["m8_no_action_restraint_behavior"]["selected_capability"] is None
    assert all("secret" not in str(receipt).lower() for receipt in receipts)
    assert all(receipt["operator_correction"]["can_correct_action"] is True for receipt in receipts)


@pytest.mark.asyncio
async def test_m8_guardian_brain_benchmark_report_stays_ci_gated_when_suite_passes():
    summary = SimpleNamespace(
        total=7,
        passed=7,
        failed=0,
        duration_ms=95,
        results=[
            SimpleNamespace(
                name="m8_capability_choice_act_behavior",
                passed=True,
                error=None,
            )
        ],
    )

    with patch(
        "src.guardian.benchmark._run_m8_guardian_brain_benchmark_suite",
        AsyncMock(return_value=summary),
    ):
        report = await build_m8_guardian_brain_benchmark_report()

    assert report["summary"]["suite_name"] == "m8_guardian_intervention_quality"
    assert report["summary"]["benchmark_posture"] == "m8_ci_gated_operator_visible"
    assert report["summary"]["active_failure_count"] == 0
    assert report["summary"]["decision_surface_state"] == "act_defer_bundle_clarify_approval_and_silence_receipts_visible"
    assert report["summary"]["quality_score_state"] == "timing_usefulness_false_positive_false_negative_trust_and_recovery_visible"
    assert report["policy"]["approval_policy"] == "high_risk_capability_use_requires_operator_approval_receipt"
    assert "/api/operator/m8-guardian-brain" in report["policy"]["receipt_surfaces"]
    assert report["failure_report"] == []


@pytest.mark.asyncio
async def test_m8_guardian_brain_benchmark_report_reflects_suite_failures():
    summary = SimpleNamespace(
        total=7,
        passed=6,
        failed=1,
        duration_ms=120,
        results=[
            SimpleNamespace(
                name="m8_risky_capability_approval_behavior",
                passed=False,
                error="approval receipt missing",
            )
        ],
    )

    with patch(
        "src.guardian.benchmark._run_m8_guardian_brain_benchmark_suite",
        AsyncMock(return_value=summary),
    ):
        report = await build_m8_guardian_brain_benchmark_report()

    assert report["summary"]["benchmark_posture"] == "m8_ci_regressions_detected_operator_visible"
    assert report["summary"]["active_failure_count"] == 1
    assert report["latest_run"]["failed"] == 1
    assert report["failure_report"][0]["scenario_name"] == "m8_risky_capability_approval_behavior"
    assert report["failure_report"][0]["type"] == "benchmark_regression"
