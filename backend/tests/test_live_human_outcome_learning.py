"""Tests for Batch CF live human-outcome and causal guardian-learning receipts."""

import asyncio

from src.guardian.live_human_outcome_learning import (
    LIVE_HUMAN_OUTCOME_LEARNING_BLOCKED_CLAIMS,
    LIVE_HUMAN_OUTCOME_LEARNING_CLAIM_BOUNDARY,
    build_live_human_outcome_learning_contract,
    build_live_human_outcome_learning_report,
)


def test_live_human_outcome_learning_contract_exposes_recorded_live_study_receipts():
    contract = build_live_human_outcome_learning_contract()
    summary = contract["summary"]
    outcomes = {item["outcome"] for item in contract["study_receipts"]}

    assert summary["operator_status"] == "live_human_outcome_learning_receipts_visible"
    assert summary["study_mode"] == "recorded_live_anonymized"
    assert summary["outcome_cohort_count"] == 7
    assert summary["typed_outcome_count"] == 7
    assert outcomes >= {"accepted", "ignored", "corrected", "deferred", "harmful", "helpful", "followthrough"}
    assert summary["consented_cohort_count"] == summary["outcome_cohort_count"]
    assert summary["anonymized_cohort_count"] == summary["outcome_cohort_count"]
    assert summary["bias_limitation_count"] == summary["outcome_cohort_count"]


def test_live_human_outcome_learning_contract_bounds_causal_claims():
    contract = build_live_human_outcome_learning_contract()
    summary = contract["summary"]
    causal = contract["causal_attribution"]

    assert summary["causal_attribution_count"] == 4
    assert summary["bounded_causal_claim_count"] == 4
    assert all(item["claim_scope"].startswith("bounded_to_") for item in causal)
    assert all(item["counterfactual_outcome"] for item in causal)
    assert all(item["confounders"] for item in causal)
    assert all(item["learning_change"]["reversible"] is True for item in causal)


def test_live_human_outcome_learning_contract_monitors_provider_regressions():
    contract = build_live_human_outcome_learning_contract()
    summary = contract["summary"]
    monitors = contract["memory_provider_monitors"]

    assert summary["provider_monitor_count"] == 4
    assert summary["provider_quarantine_count"] == 2
    assert summary["stale_decay_monitor_count"] == 4
    assert summary["privacy_regression_count"] == 1
    assert any(
        item["quarantine_state"] == "quarantined"
        and item["behavior_change_allowed"] is False
        for item in monitors
    )


def test_live_human_outcome_learning_contract_preserves_claim_boundaries():
    contract = build_live_human_outcome_learning_contract()
    policy = contract["policy"]

    assert policy["claim_boundary"] == LIVE_HUMAN_OUTCOME_LEARNING_CLAIM_BOUNDARY
    assert set(LIVE_HUMAN_OUTCOME_LEARNING_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "guardian_intelligence_superiority" in policy["not_claimed"]
    assert "solved_live_learning" in policy["not_claimed"]
    assert "/api/operator/live-human-outcome-learning-proof" in policy["receipt_surfaces"]
    assert "/api/operator/benchmark-proof" in policy["receipt_surfaces"]


def test_live_human_outcome_learning_report_exposes_ci_gated_posture():
    report = asyncio.run(build_live_human_outcome_learning_report())

    assert report["summary"]["benchmark_posture"] == "live_human_outcome_learning_ci_gated_operator_visible"
    assert report["summary"]["scenario_count"] == 15
    assert report["summary"]["active_failure_count"] == 0
    assert report["latest_run"]["failed"] == 0
    assert report["policy"]["claim_boundary"] == LIVE_HUMAN_OUTCOME_LEARNING_CLAIM_BOUNDARY
