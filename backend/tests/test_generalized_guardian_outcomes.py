import asyncio

from src.guardian.generalized_guardian_outcomes import (
    GENERALIZED_GUARDIAN_OUTCOMES_BLOCKED_CLAIMS,
    GENERALIZED_GUARDIAN_OUTCOMES_CLAIM_BOUNDARY,
    build_generalized_guardian_outcomes_contract,
    build_generalized_guardian_outcomes_report,
)


def test_generalized_outcome_study_predeclares_broad_decision_protocol():
    contract = build_generalized_guardian_outcomes_contract()
    summary = contract["summary"]
    studies = contract["generalized_outcome_studies"]

    assert summary["study_count"] == 8
    assert summary["predeclared_protocol_count"] == summary["study_count"]
    assert summary["decision_type_count"] == 8
    assert summary["task_family_count"] == 8
    assert summary["sample_size_total"] >= 800
    assert {
        "act",
        "defer",
        "bundle",
        "clarify",
        "approval",
        "stay_silent",
        "recovery",
        "followthrough",
    } <= {item["decision"] for item in studies}
    assert all(item["evaluator"]["implementation_independent"] is True for item in studies)
    assert all(item["consent"]["withdrawal_supported"] is True for item in studies)


def test_generalized_outcome_study_reviews_adverse_events_and_redacts_receipts():
    contract = build_generalized_guardian_outcomes_contract()
    summary = contract["summary"]
    receipts = [
        item["safe_receipt"]
        for group in (
            contract["generalized_outcome_studies"],
            contract["memory_provider_parity_matrix"],
            contract["causal_learning_thresholds"],
            contract["memory_baseline_comparisons"],
        )
        for item in group
    ]

    assert summary["adverse_event_count"] >= 1
    assert summary["adverse_event_reviewed_count"] == summary["adverse_event_count"]
    assert summary["raw_transcript_stored_count"] == 0
    assert summary["secret_leak_count"] == 0
    assert summary["unredacted_identifier_count"] == 0
    assert summary["provider_payload_leak_count"] == 0
    assert summary["raw_receipt_path_exposed_count"] == 0
    assert all(receipt["contains_raw_transcript"] is False for receipt in receipts)
    assert all(receipt["contains_secret"] is False for receipt in receipts)
    assert all(receipt["contains_provider_payload"] is False for receipt in receipts)
    assert all(receipt["raw_receipt_path_exposed"] is False for receipt in receipts)


def test_full_memory_provider_matrix_preserves_canonical_authority_and_blocks_unsafe_rows():
    contract = build_generalized_guardian_outcomes_contract()
    summary = contract["summary"]
    providers = contract["memory_provider_parity_matrix"]

    assert summary["provider_count"] >= 6
    assert summary["provider_dimension_count"] >= 12
    assert summary["canonical_precedence_preserved_count"] == summary["provider_count"]
    assert summary["provider_override_blocked_count"] >= 5
    assert summary["provider_failed_dimension_count"] >= 1
    assert summary["privacy_regression_count"] >= 1
    assert summary["delete_export_receipt_count"] == summary["provider_count"]
    assert summary["quarantine_count"] >= 2
    assert summary["reinstatement_review_count"] >= 2
    assert any(
        item["provider_runtime_state"] == "quarantined" and item["behavior_change_allowed"] is False
        for item in providers
    )


def test_causal_thresholds_require_counterfactuals_promotion_gates_and_rollback():
    contract = build_generalized_guardian_outcomes_contract()
    summary = contract["summary"]
    causal = contract["causal_learning_thresholds"]

    assert summary["causal_threshold_count"] == 3
    assert summary["causal_threshold_pass_count"] == summary["causal_threshold_count"]
    assert summary["causal_counterfactual_count"] == summary["causal_threshold_count"]
    assert summary["rollback_authority_count"] == summary["causal_threshold_count"]
    assert all(item["confounders"] for item in causal)
    assert all(item["negative_controls"] for item in causal)
    assert all(
        "operator_review_required" in item["promotion_gate_state"] or "blocked" in item["promotion_gate_state"]
        for item in causal
    )


def test_memory_baselines_are_current_source_limited_pressure_evidence_only():
    contract = build_generalized_guardian_outcomes_contract()
    summary = contract["summary"]
    baselines = contract["memory_baseline_comparisons"]

    assert summary["baseline_count"] == 3
    assert summary["current_source_baseline_count"] == summary["baseline_count"]
    assert summary["pressure_only_baseline_count"] == summary["baseline_count"]
    assert all(item["source_url"] and item["source_checked_at"] for item in baselines)
    assert all("not_named_baseline_win" in item["comparison_disposition"] for item in baselines)
    assert all("access_caveat" in item for item in baselines)


def test_generalized_guardian_outcomes_policy_keeps_completion_claims_blocked():
    contract = build_generalized_guardian_outcomes_contract()
    policy = contract["policy"]

    assert policy["claim_boundary"] == GENERALIZED_GUARDIAN_OUTCOMES_CLAIM_BOUNDARY
    assert set(GENERALIZED_GUARDIAN_OUTCOMES_BLOCKED_CLAIMS) <= set(policy["blocked_claims"])
    assert "full_memory_provider_parity" in policy["blocked_claims"]
    assert "generalized_outcome_superiority" in policy["blocked_claims"]
    assert "/api/operator/generalized-guardian-outcomes" in policy["receipt_surfaces"]
    assert "/api/operator/benchmark-proof" in policy["receipt_surfaces"]


def test_generalized_guardian_outcomes_report_exposes_ci_gated_posture():
    report = asyncio.run(build_generalized_guardian_outcomes_report())

    assert report["summary"]["operator_status"] == "generalized_guardian_outcomes_receipts_visible"
    assert report["summary"]["benchmark_posture"] == "generalized_guardian_outcomes_ci_gated_operator_visible"
    assert report["summary"]["active_failure_count"] == 0
    assert report["summary"]["scenario_count"] == 18
    assert report["latest_run"]["failed"] == 0
