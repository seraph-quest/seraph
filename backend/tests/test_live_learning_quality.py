"""Tests for Batch BZ live guardian-learning and provider outcome receipts."""

import asyncio

from src.guardian.live_learning_quality import (
    LIVE_GUARDIAN_LEARNING_QUALITY_BLOCKED_CLAIMS,
    LIVE_GUARDIAN_LEARNING_QUALITY_CLAIM_BOUNDARY,
    build_live_guardian_learning_quality_contract,
    build_live_guardian_learning_quality_report,
)


def test_live_guardian_learning_quality_contract_exposes_outcome_receipts():
    contract = build_live_guardian_learning_quality_contract()
    summary = contract["summary"]
    outcomes = {item["outcome"] for item in contract["outcome_cohorts"]}

    assert summary["operator_status"] == "live_guardian_learning_quality_receipts_visible"
    assert summary["outcome_cohort_count"] == 8
    assert summary["typed_outcome_count"] == 8
    assert outcomes == {
        "accepted",
        "ignored",
        "corrected",
        "deferred",
        "harmful",
        "helpful",
        "channel_shifted",
        "followthrough",
    }
    assert summary["policy_delta_count"] >= 4
    assert summary["false_positive_receipt_count"] >= 1
    assert summary["false_negative_receipt_count"] >= 1
    assert summary["stale_evidence_decay_count"] >= 1


def test_live_guardian_learning_quality_contract_preserves_claim_boundaries():
    contract = build_live_guardian_learning_quality_contract()
    policy = contract["policy"]

    assert policy["claim_boundary"] == LIVE_GUARDIAN_LEARNING_QUALITY_CLAIM_BOUNDARY
    assert set(LIVE_GUARDIAN_LEARNING_QUALITY_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "guardian_intelligence_superiority" in policy["not_claimed"]
    assert "external_memory_provider_parity" in policy["not_claimed"]
    assert "/api/operator/live-guardian-learning-quality" in policy["receipt_surfaces"]
    assert "/api/operator/benchmark-proof" in policy["receipt_surfaces"]


def test_live_guardian_learning_quality_contract_preserves_canonical_memory_controls():
    contract = build_live_guardian_learning_quality_contract()
    summary = contract["summary"]
    reconciliation = contract["canonical_reconciliation"]

    assert summary["canonical_precedence_preserved"] is True
    assert summary["delete_export_receipts_visible"] is True
    assert summary["provider_quarantine_count"] == 1
    assert reconciliation["canonical_precedence"]["provider_override_blocked"] is True
    assert reconciliation["provider_assisted_retrieval"]["changed_behavior_only_after_canonical_match"] is True
    assert reconciliation["advisory_writeback"]["state"] == "review_required"
    assert reconciliation["delete_export"]["provider_delete_does_not_delete_canonical_without_review"] is True
    assert reconciliation["quarantine"]["degrades_to_canonical_memory"] is True


def test_live_guardian_learning_quality_provider_regressions_gate_behavior_change():
    contract = build_live_guardian_learning_quality_contract()
    providers = contract["provider_maturity"]
    regressions = contract["provider_regressions"]

    assert contract["summary"]["provider_regression_count"] == 4
    assert contract["summary"]["provider_regressions_passed"] is True
    assert all(item["passed"] is True for item in regressions)
    assert any(
        item["behavior_change"]["changed_action"] is True
        and item["behavior_change"]["canonical_precedence"] is True
        for item in providers
    )
    assert any(
        item["role"] == "quarantined_provider"
        and item["behavior_change"]["changed_action"] is False
        for item in providers
    )


def test_live_guardian_learning_quality_report_exposes_ci_gated_posture():
    report = asyncio.run(build_live_guardian_learning_quality_report())

    assert report["summary"]["benchmark_posture"] == "live_guardian_learning_quality_ci_gated_operator_visible"
    assert report["summary"]["scenario_count"] == 28
    assert report["summary"]["active_failure_count"] == 0
    assert report["latest_run"]["failed"] == 0
    assert report["policy"]["claim_boundary"] == LIVE_GUARDIAN_LEARNING_QUALITY_CLAIM_BOUNDARY
