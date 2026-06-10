"""Tests for Batch CM independent learning and memory-provider parity-matrix receipts."""

import asyncio

from src.guardian.independent_learning_memory_parity import (
    INDEPENDENT_LEARNING_MEMORY_PARITY_BLOCKED_CLAIMS,
    INDEPENDENT_LEARNING_MEMORY_PARITY_CLAIM_BOUNDARY,
    build_independent_learning_memory_parity_contract,
    build_independent_learning_memory_parity_report,
)


def test_independent_learning_memory_parity_contract_exposes_independent_cohort_gates():
    contract = build_independent_learning_memory_parity_contract()
    summary = contract["summary"]
    cohorts = contract["independent_outcome_cohorts"]

    assert summary["operator_status"] == "independent_learning_memory_parity_receipts_visible"
    assert summary["cohort_count"] == 3
    assert summary["independent_evaluator_count"] == summary["cohort_count"]
    assert summary["implementation_independent_evaluator_count"] == summary["cohort_count"]
    assert summary["sample_size_total"] >= 150
    assert summary["consented_cohort_count"] == summary["cohort_count"]
    assert summary["anonymized_cohort_count"] == summary["cohort_count"]
    assert summary["adverse_event_review_count"] == summary["cohort_count"]
    assert summary["bounded_outcome_claim_count"] == summary["cohort_count"]
    assert all(item["recruitment_source"] for item in cohorts)
    assert all(item["study_window"] for item in cohorts)
    assert all(item["evaluator"]["protocol_version"] for item in cohorts)
    assert all(item["evaluator"]["reviewer_notes_visible"] for item in cohorts)


def test_independent_learning_memory_parity_contract_bounds_causal_claims():
    contract = build_independent_learning_memory_parity_contract()
    summary = contract["summary"]
    causal = contract["task_scoped_causal_attribution"]

    assert summary["causal_attribution_count"] == 3
    assert summary["bounded_causal_claim_count"] == 3
    assert summary["rollback_authority_count"] == 3
    assert all(item["claim_scope"].startswith("bounded_to_") for item in causal)
    assert all(item["counterfactual_outcome"] for item in causal)
    assert all(item["confounders"] for item in causal)
    assert all(item["task_class"] for item in causal)
    assert all(item["time_horizon_days"] == 30 for item in causal)


def test_independent_learning_memory_parity_contract_scopes_provider_parity_matrix():
    contract = build_independent_learning_memory_parity_contract()
    summary = contract["summary"]
    providers = contract["memory_provider_parity_matrix"]

    assert summary["provider_count"] == 4
    assert summary["provider_parity_dimension_count"] >= 10
    assert summary["provider_canonical_override_blocked_count"] == 3
    assert summary["delete_export_receipt_count"] == summary["provider_count"]
    assert summary["provider_privacy_regression_count"] == 1
    assert summary["provider_failed_dimension_count"] >= 1
    assert summary["provider_promotion_blocked_count"] == 2
    assert summary["secret_or_credential_leak_count"] == 0
    assert summary["provider_quarantine_count"] == 2
    regressed = next(item for item in providers if item["privacy_regression_detected"] is True)
    assert "privacy_boundary" in regressed["failed_dimensions"]
    assert "privacy_boundary" not in regressed["passed_dimensions"]
    assert regressed["promotion_blocked"] is True
    assert any(
        item["quarantine_state"] == "quarantined"
        and item["behavior_change_allowed"] is False
        for item in providers
    )


def test_independent_learning_memory_parity_contract_preserves_claim_boundaries():
    contract = build_independent_learning_memory_parity_contract()
    policy = contract["policy"]

    assert policy["claim_boundary"] == INDEPENDENT_LEARNING_MEMORY_PARITY_CLAIM_BOUNDARY
    assert set(INDEPENDENT_LEARNING_MEMORY_PARITY_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "guardian_intelligence_superiority" in policy["not_claimed"]
    assert "memory_superiority" in policy["not_claimed"]
    assert "full_memory_provider_parity" in policy["not_claimed"]
    assert "/api/operator/independent-learning-memory-parity" in policy["receipt_surfaces"]
    assert "/api/operator/benchmark-proof" in policy["receipt_surfaces"]


def test_independent_learning_memory_parity_report_exposes_ci_gated_posture():
    report = asyncio.run(build_independent_learning_memory_parity_report())

    assert report["summary"]["benchmark_posture"] == "independent_learning_memory_parity_ci_gated_operator_visible"
    assert report["summary"]["scenario_count"] == 14
    assert report["summary"]["active_failure_count"] == 0
    assert report["latest_run"]["failed"] == 0
    assert report["policy"]["claim_boundary"] == INDEPENDENT_LEARNING_MEMORY_PARITY_CLAIM_BOUNDARY
