from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.evals.harness import EvalResult, EvalSummary
from src.guardian.learning_quality import (
    GUARDIAN_LEARNING_QUALITY_CLAIM_BOUNDARY,
    GUARDIAN_LEARNING_QUALITY_SCENARIO_NAMES,
    GUARDIAN_LEARNING_QUALITY_SUITE_NAME,
    build_guardian_learning_quality_replay,
    build_guardian_learning_quality_report,
    guardian_learning_quality_policy_payload,
)


def test_guardian_learning_quality_replay_surfaces_core_receipts():
    replay = build_guardian_learning_quality_replay()
    receipts = {receipt["scenario_name"]: receipt for receipt in replay["receipts"]}

    assert replay["summary"]["suite_name"] == GUARDIAN_LEARNING_QUALITY_SUITE_NAME
    assert replay["summary"]["scenario_count"] == len(GUARDIAN_LEARNING_QUALITY_SCENARIO_NAMES)
    assert replay["summary"]["quality_state"] == (
        "multi_signal_stale_conflict_salience_confidence_and_outcome_labels_visible"
    )
    assert receipts["guardian_learning_multi_signal_arbitration_behavior"]["selected_action"] == "act"
    assert receipts["guardian_learning_stale_conflict_suppression_behavior"]["selected_action"] == "defer"
    assert receipts["guardian_learning_stale_conflict_suppression_behavior"]["stale_evidence_policy"] == (
        "stale_provider_evidence_suppressed"
    )
    calibration = receipts["guardian_learning_salience_confidence_calibration_behavior"]
    assert calibration["salience_level"] == "high"
    assert calibration["confidence_level"] == "partial"
    assert calibration["selected_action"] == "clarify"
    accounting = receipts["guardian_learning_false_positive_negative_accounting_behavior"]
    assert accounting["false_positive_label"] != "missing"
    assert accounting["false_negative_label"] != "missing"
    assert all(receipt["claim_boundary"] == GUARDIAN_LEARNING_QUALITY_CLAIM_BOUNDARY for receipt in receipts.values())


@pytest.mark.asyncio
async def test_guardian_learning_quality_report_reflects_suite_status():
    benchmark_summary = EvalSummary(
        results=[
            EvalResult(
                name=name,
                category="guardian",
                description="Guardian learning quality fixture",
                passed=True,
                duration_ms=1,
            )
            for name in GUARDIAN_LEARNING_QUALITY_SCENARIO_NAMES
        ],
        duration_ms=len(GUARDIAN_LEARNING_QUALITY_SCENARIO_NAMES),
    )

    with patch(
        "src.guardian.learning_quality._run_guardian_learning_quality_benchmark_suite",
        AsyncMock(return_value=benchmark_summary),
    ):
        report = await build_guardian_learning_quality_report()

    assert report["summary"]["benchmark_posture"] == "guardian_learning_quality_ci_gated_operator_visible"
    assert report["summary"]["active_failure_count"] == 0
    assert report["latest_run"]["passed"] == len(GUARDIAN_LEARNING_QUALITY_SCENARIO_NAMES)
    assert report["policy"]["claim_boundary"] == GUARDIAN_LEARNING_QUALITY_CLAIM_BOUNDARY
    assert "/api/operator/guardian-learning-quality" in report["policy"]["receipt_surfaces"]


def test_guardian_learning_quality_policy_is_bounded():
    policy = guardian_learning_quality_policy_payload()

    assert policy["ci_gate_mode"] == "required_benchmark_suite"
    assert policy["claim_boundary"] == GUARDIAN_LEARNING_QUALITY_CLAIM_BOUNDARY
    assert policy["stale_evidence_policy"] == "stale_or_conflicting_evidence_degrades_confidence_before_action"
