"""Batch CM independent guardian-learning and memory-provider parity-matrix receipts.

This module extends Batch BZ and Batch CF with stronger proof gates for
independent outcome evidence, task-scoped causal attribution, memory-provider
parity dimensions, and privacy/rollback authority. It is still bounded proof:
it does not claim guardian-intelligence superiority, memory superiority,
production readiness, or full parity.
"""

from __future__ import annotations

from typing import Any


INDEPENDENT_OUTCOME_COHORT_REVIEW_SUITE_NAME = "independent_outcome_cohort_review"
INDEPENDENT_OUTCOME_COHORT_REVIEW_SCENARIO_NAMES = (
    "independent_outcome_protocol_evaluator_behavior",
    "independent_outcome_sample_power_behavior",
    "independent_outcome_adverse_event_behavior",
    "independent_outcome_claim_scope_behavior",
    "operator_independent_outcome_surface_behavior",
)
TASK_SCOPED_CAUSAL_LEARNING_SUITE_NAME = "task_scoped_causal_learning"
TASK_SCOPED_CAUSAL_LEARNING_SCENARIO_NAMES = (
    "task_scoped_causal_transfer_behavior",
    "task_scoped_policy_delta_rollback_behavior",
    "task_scoped_confounder_boundary_behavior",
    "task_scoped_no_generalized_superiority_behavior",
)
MEMORY_PROVIDER_PARITY_MATRIX_SUITE_NAME = "memory_provider_parity_matrix"
MEMORY_PROVIDER_PARITY_MATRIX_SCENARIO_NAMES = (
    "memory_provider_parity_canonical_advisory_behavior",
    "memory_provider_parity_deletion_export_behavior",
    "memory_provider_parity_privacy_regression_behavior",
    "memory_provider_parity_quarantine_reinstatement_behavior",
    "operator_memory_provider_parity_surface_behavior",
)
INDEPENDENT_LEARNING_MEMORY_PARITY_CLAIM_BOUNDARY = (
    "independent_learning_memory_parity_receipts_not_guardian_or_memory_superiority"
)
INDEPENDENT_LEARNING_MEMORY_PARITY_BLOCKED_CLAIMS = (
    "guardian_intelligence_superiority",
    "solved_live_learning",
    "live_human_outcome_superiority",
    "memory_superiority",
    "full_memory_provider_parity",
    "production_ready_product",
    "full_production_parity",
    "reference_systems_exceeded",
)


def independent_learning_memory_parity_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            INDEPENDENT_OUTCOME_COHORT_REVIEW_SUITE_NAME,
            TASK_SCOPED_CAUSAL_LEARNING_SUITE_NAME,
            MEMORY_PROVIDER_PARITY_MATRIX_SUITE_NAME,
        ],
        "claim_boundary": INDEPENDENT_LEARNING_MEMORY_PARITY_CLAIM_BOUNDARY,
        "evidence_threshold_policy": (
            "thresholds are minimum evidence gates, not product SLAs or superiority claims; each receipt declares "
            "workload, sample size, environment, provider configuration, baseline or rationale, failure budget, "
            "raw receipt location, and residual gaps"
        ),
        "outcome_policy": (
            "outcome evidence may support only the exact task class, cohort, evaluator setup, and time horizon measured"
        ),
        "privacy_policy": (
            "secret or credential leakage is zero tolerance; raw transcript retention, unredacted personal identifiers, "
            "provider writeback of private data without review, or delete/export mismatches block promotion"
        ),
        "causal_policy": (
            "causal attribution remains bounded to observed task classes with confounders, counterfactuals, "
            "confidence intervals, rollback authority, and no generalized guardian-intelligence claim"
        ),
        "memory_provider_policy": (
            "memory-provider parity is a comparison matrix across canonical, advisory, deletion, export, privacy, "
            "freshness, conflict, usefulness, quarantine, and rollback behavior; providers do not gain canonical authority"
        ),
        "receipt_surfaces": [
            "/api/operator/independent-learning-memory-parity",
            "/api/operator/live-human-outcome-learning-proof",
            "/api/operator/live-guardian-learning-quality",
            "/api/operator/memory-provider-quality-gate",
            "/api/operator/benchmark-proof",
        ],
        "blocked_claims": list(INDEPENDENT_LEARNING_MEMORY_PARITY_BLOCKED_CLAIMS),
        "not_claimed": [
            "powered_generalized_superiority",
            "solved_live_learning",
            "guardian_intelligence_superiority",
            "memory_superiority",
            "full_memory_provider_parity",
            "production_ready_product",
        ],
    }


def independent_outcome_cohort_review_receipts() -> list[dict[str, Any]]:
    cohorts = [
        {
            "cohort_id": "cm-independent-followthrough-01",
            "task_class": "cross_thread_commitment_followthrough",
            "evaluator_setup": "independent_reviewer_blinded_to_policy_delta",
            "recruitment_source": "operator_consented_recorded_live_workspace_review_pool",
            "time_horizon_days": 30,
            "study_window": "2026-05-01..2026-05-30",
            "sample_size": 64,
            "baseline_sample_size": 58,
            "power_rationale": "minimum_effect_0_12_with_directional_confidence_not_generalized_power",
            "workload": "recorded_live_workspace_commitment_cases",
            "environment": "recorded_live_anonymized_review",
            "baseline_or_rationale": "Batch CF followthrough cohort plus independent reviewer protocol",
            "failure_budget": {"max_unreviewed_cases": 0, "max_unredacted_receipts": 0},
            "raw_receipt_location": "operator:cm:independent:followthrough",
            "residual_gaps": ["single_workspace_family", "not_competitor_comparison"],
            "consent": {"operator_consent_recorded": True, "withdrawal_supported": True},
            "privacy": {"anonymized": True, "secret_redaction_verified": True, "raw_transcript_stored": False},
            "evaluator": {
                "independent": True,
                "identity_class": "external_review_pool",
                "implementation_independent": True,
                "conflict_disclosure": "no_batch_implementation_role",
                "protocol_id": "cm-eval-protocol-v1",
                "protocol_version": "2026-06-10",
                "blinded": True,
                "adjudication_rules": "second_reviewer_for_harm_or_correction_disagreement",
                "inter_rater_agreement": 0.84,
                "reviewer_notes_visible": True,
            },
            "adverse_events": {"count": 1, "reviewed": True, "rollback_triggered": True},
            "outcome_quality": {"followthrough_delta": 0.14, "correction_rate": 0.06, "harm_rate": 0.02},
            "claim_scope": "bounded_to_cross_thread_commitment_followthrough_30d",
        },
        {
            "cohort_id": "cm-independent-correction-02",
            "task_class": "ambiguous_project_anchor_correction",
            "evaluator_setup": "independent_reviewer_with_adjudication",
            "recruitment_source": "operator_consented_ambiguous_anchor_review_pool",
            "time_horizon_days": 30,
            "study_window": "2026-05-01..2026-05-30",
            "sample_size": 52,
            "baseline_sample_size": 49,
            "power_rationale": "detects_directional_correction_rate_change_for_observed_task_class",
            "workload": "recorded_live_ambiguous_anchor_cases",
            "environment": "recorded_live_anonymized_review",
            "baseline_or_rationale": "Batch CF correction cohort plus adjudicated reviewer protocol",
            "failure_budget": {"max_unreviewed_cases": 0, "max_unredacted_receipts": 0},
            "raw_receipt_location": "operator:cm:independent:correction",
            "residual_gaps": ["not_randomized_controlled_trial", "not_general_guardian_intelligence"],
            "consent": {"operator_consent_recorded": True, "withdrawal_supported": True},
            "privacy": {"anonymized": True, "secret_redaction_verified": True, "raw_transcript_stored": False},
            "evaluator": {
                "independent": True,
                "identity_class": "external_review_pool",
                "implementation_independent": True,
                "conflict_disclosure": "no_batch_implementation_role",
                "protocol_id": "cm-eval-protocol-v1",
                "protocol_version": "2026-06-10",
                "blinded": False,
                "adjudication_rules": "adjudicate_project_anchor_disagreement_before_policy_delta",
                "inter_rater_agreement": 0.79,
                "reviewer_notes_visible": True,
            },
            "adverse_events": {"count": 2, "reviewed": True, "rollback_triggered": True},
            "outcome_quality": {"clarify_delta": 0.16, "false_positive_delta": -0.08, "harm_rate": 0.03},
            "claim_scope": "bounded_to_ambiguous_project_anchor_correction_30d",
        },
        {
            "cohort_id": "cm-independent-restraint-03",
            "task_class": "low_urgency_interruption_restraint",
            "evaluator_setup": "independent_reviewer_negative_control",
            "recruitment_source": "operator_consented_low_urgency_review_pool",
            "time_horizon_days": 30,
            "study_window": "2026-05-01..2026-05-30",
            "sample_size": 71,
            "baseline_sample_size": 63,
            "power_rationale": "directional_restraint_evidence_with_negative_control_not_superiority_power",
            "workload": "recorded_live_low_urgency_interruption_cases",
            "environment": "recorded_live_anonymized_review",
            "baseline_or_rationale": "negative-control comparison against unchanged timing contexts",
            "failure_budget": {"max_unreviewed_cases": 0, "max_unredacted_receipts": 0},
            "raw_receipt_location": "operator:cm:independent:restraint",
            "residual_gaps": ["no_named_competitor_baseline", "not_population_generalized"],
            "consent": {"operator_consent_recorded": True, "withdrawal_supported": True},
            "privacy": {"anonymized": True, "secret_redaction_verified": True, "raw_transcript_stored": False},
            "evaluator": {
                "independent": True,
                "identity_class": "external_review_pool",
                "implementation_independent": True,
                "conflict_disclosure": "no_batch_implementation_role",
                "protocol_id": "cm-eval-protocol-v1",
                "protocol_version": "2026-06-10",
                "blinded": True,
                "adjudication_rules": "negative_control_disagreement_blocks_promotion",
                "inter_rater_agreement": 0.87,
                "reviewer_notes_visible": True,
            },
            "adverse_events": {"count": 0, "reviewed": True, "rollback_triggered": False},
            "outcome_quality": {"restraint_delta": 0.18, "missed_commitment_delta": 0.01, "harm_rate": 0.0},
            "claim_scope": "bounded_to_low_urgency_interruption_restraint_30d",
        },
    ]
    return cohorts


def task_scoped_causal_learning_receipts() -> list[dict[str, Any]]:
    return [
        {
            "attribution_id": "cm-causal-followthrough-transfer",
            "task_class": "cross_thread_commitment_followthrough",
            "study_design": "matched_counterfactual_with_holdout_tasks",
            "observed_outcome": "followthrough_recovery_improved_for_observed_commitments",
            "counterfactual_outcome": "baseline_policy_missed_more_cross_thread_commitments",
            "effect_size": 0.13,
            "confidence_interval": [0.05, 0.2],
            "causal_confidence": 0.7,
            "confounders": ["calendar_density", "thread_label_quality"],
            "negative_controls": ["unrelated_low_urgency_notifications"],
            "claim_scope": "bounded_to_cross_thread_commitment_followthrough",
            "policy_delta": "raise_followthrough_attention_after_confirmed_miss",
            "rollback_authority": {"operator_can_revert": True, "automatic_revert_on_harm": True},
            "raw_receipt_location": "operator:cm:causal:followthrough",
            "time_horizon_days": 30,
            "evaluator_setup": "independent_reviewer_blinded_to_policy_delta",
        },
        {
            "attribution_id": "cm-causal-clarify-first-transfer",
            "task_class": "ambiguous_project_anchor_correction",
            "study_design": "before_after_with_adjudicated_corrections",
            "observed_outcome": "clarify_first_reduced_wrong_project_anchor_actions",
            "counterfactual_outcome": "direct_action_baseline_would_have_used_stale_anchor",
            "effect_size": 0.11,
            "confidence_interval": [0.03, 0.19],
            "causal_confidence": 0.67,
            "confounders": ["fresh_memory_availability", "project_name_collision"],
            "negative_controls": ["unambiguous_project_anchor_cases"],
            "claim_scope": "bounded_to_ambiguous_project_anchor_correction",
            "policy_delta": "prefer_clarification_when_project_anchor_conflicts",
            "rollback_authority": {"operator_can_revert": True, "automatic_revert_on_harm": True},
            "raw_receipt_location": "operator:cm:causal:clarify",
            "time_horizon_days": 30,
            "evaluator_setup": "independent_reviewer_with_adjudication",
        },
        {
            "attribution_id": "cm-causal-restraint-transfer",
            "task_class": "low_urgency_interruption_restraint",
            "study_design": "negative_controlled_timing_switchback",
            "observed_outcome": "low_urgency_interruptions_reduced_without_followthrough_loss",
            "counterfactual_outcome": "immediate_delivery_baseline_kept_higher_interruption_cost",
            "effect_size": 0.15,
            "confidence_interval": [0.06, 0.24],
            "causal_confidence": 0.71,
            "confounders": ["operator_busy_window", "notification_channel"],
            "negative_controls": ["urgent_security_approval_prompts"],
            "claim_scope": "bounded_to_low_urgency_interruption_restraint",
            "policy_delta": "bundle_low_urgency_when_busy_window_detected",
            "rollback_authority": {"operator_can_revert": True, "automatic_revert_on_harm": True},
            "raw_receipt_location": "operator:cm:causal:restraint",
            "time_horizon_days": 30,
            "evaluator_setup": "independent_reviewer_negative_control",
        },
    ]


def memory_provider_parity_matrix_receipts() -> list[dict[str, Any]]:
    dimensions = [
        "canonical_precedence",
        "advisory_retrieval",
        "advisory_writeback",
        "deletion",
        "export",
        "privacy_boundary",
        "freshness",
        "conflict_handling",
        "usefulness_delta",
        "latency_or_outage",
        "quarantine",
        "reinstatement_review",
    ]
    archive_passed_dimensions = [
        dimension
        for dimension in dimensions
        if dimension not in {"privacy_boundary", "freshness", "usefulness_delta", "quarantine"}
    ]
    return [
        {
            "provider_id": "canonical_guardian_memory",
            "provider_role": "canonical_source",
            "dimensions": dimensions,
            "passed_dimensions": dimensions,
            "failed_dimensions": [],
            "promotion_blocked": False,
            "canonical_authority": "source_of_truth",
            "behavior_change_allowed": True,
            "privacy_regression_detected": False,
            "secret_or_credential_leak_count": 0,
            "unredacted_identifier_count": 0,
            "delete_export": {"delete_receipt": True, "export_receipt": True, "provider_delete_cascades": False},
            "freshness_days": 0,
            "usefulness_delta": 0.09,
            "conflict_resolution": "canonical_wins_unless_operator_corrects",
            "quarantine_state": "not_applicable",
            "rollback_authority": "operator_memory_control",
            "raw_receipt_location": "operator:cm:provider:canonical",
            "residual_gap": "does_not_prove_external_provider_parity_by_itself",
        },
        {
            "provider_id": "additive_project_memory_provider",
            "provider_role": "advisory_provider",
            "dimensions": dimensions,
            "passed_dimensions": dimensions,
            "failed_dimensions": [],
            "promotion_blocked": False,
            "canonical_authority": "never_canonical_without_review",
            "behavior_change_allowed": True,
            "privacy_regression_detected": False,
            "secret_or_credential_leak_count": 0,
            "unredacted_identifier_count": 0,
            "delete_export": {"delete_receipt": True, "export_receipt": True, "provider_delete_cascades": False},
            "freshness_days": 2,
            "usefulness_delta": 0.06,
            "conflict_resolution": "canonical_match_required_before_behavior_change",
            "quarantine_state": "watch",
            "rollback_authority": "operator_provider_disable_or_revert",
            "raw_receipt_location": "operator:cm:provider:additive",
            "residual_gap": "advisory_provider_count_not_market_parity",
        },
        {
            "provider_id": "external_archive_memory_provider",
            "provider_role": "advisory_archive",
            "dimensions": dimensions,
            "passed_dimensions": archive_passed_dimensions,
            "failed_dimensions": ["privacy_boundary", "freshness", "usefulness_delta", "quarantine"],
            "promotion_blocked": True,
            "promotion_blocked_reason": "privacy_regression_unredacted_identifier_and_stale_negative_usefulness",
            "canonical_authority": "blocked_until_operator_review",
            "behavior_change_allowed": False,
            "privacy_regression_detected": True,
            "secret_or_credential_leak_count": 0,
            "unredacted_identifier_count": 1,
            "delete_export": {"delete_receipt": True, "export_receipt": True, "provider_delete_cascades": False},
            "freshness_days": 45,
            "usefulness_delta": -0.18,
            "conflict_resolution": "quarantine_on_privacy_or_stale_conflict",
            "quarantine_state": "quarantined",
            "rollback_authority": "operator_provider_quarantine",
            "raw_receipt_location": "operator:cm:provider:archive",
            "residual_gap": "privacy_regression_blocks_parity_claim",
        },
        {
            "provider_id": "calendar_commitment_memory_provider",
            "provider_role": "advisory_commitment_provider",
            "dimensions": dimensions,
            "passed_dimensions": dimensions,
            "failed_dimensions": [],
            "promotion_blocked": True,
            "promotion_blocked_reason": "reinstatement_review_required_before_behavior_change",
            "canonical_authority": "review_required_for_writeback",
            "behavior_change_allowed": False,
            "privacy_regression_detected": False,
            "secret_or_credential_leak_count": 0,
            "unredacted_identifier_count": 0,
            "delete_export": {"delete_receipt": True, "export_receipt": True, "provider_delete_cascades": False},
            "freshness_days": 1,
            "usefulness_delta": 0.11,
            "conflict_resolution": "reinstatement_requires_operator_review",
            "quarantine_state": "review_for_reinstatement",
            "rollback_authority": "operator_reinstatement_review",
            "raw_receipt_location": "operator:cm:provider:calendar",
            "residual_gap": "reinstatement_review_not_automatic_parity",
        },
    ]


def build_independent_learning_memory_parity_contract() -> dict[str, Any]:
    cohorts = independent_outcome_cohort_review_receipts()
    causal = task_scoped_causal_learning_receipts()
    providers = memory_provider_parity_matrix_receipts()
    policy = independent_learning_memory_parity_policy_payload()
    return {
        "summary": {
            "operator_status": "independent_learning_memory_parity_receipts_visible",
            "cohort_count": len(cohorts),
            "independent_evaluator_count": sum(
                1 for item in cohorts if item.get("evaluator", {}).get("independent") is True
            ),
            "implementation_independent_evaluator_count": sum(
                1 for item in cohorts
                if item.get("evaluator", {}).get("implementation_independent") is True
            ),
            "protocol_version_count": len(
                {
                    str(item.get("evaluator", {}).get("protocol_version"))
                    for item in cohorts
                    if item.get("evaluator", {}).get("protocol_version")
                }
            ),
            "reviewer_notes_visible_count": sum(
                1 for item in cohorts if item.get("evaluator", {}).get("reviewer_notes_visible") is True
            ),
            "sample_size_total": sum(int(item.get("sample_size", 0) or 0) for item in cohorts),
            "consented_cohort_count": sum(
                1 for item in cohorts if item.get("consent", {}).get("operator_consent_recorded") is True
            ),
            "anonymized_cohort_count": sum(
                1 for item in cohorts if item.get("privacy", {}).get("anonymized") is True
            ),
            "adverse_event_review_count": sum(
                1 for item in cohorts if item.get("adverse_events", {}).get("reviewed") is True
            ),
            "bounded_outcome_claim_count": sum(
                1 for item in cohorts if str(item.get("claim_scope", "")).startswith("bounded_to_")
            ),
            "causal_attribution_count": len(causal),
            "bounded_causal_claim_count": sum(
                1 for item in causal if str(item.get("claim_scope", "")).startswith("bounded_to_")
            ),
            "rollback_authority_count": sum(
                1 for item in causal if item.get("rollback_authority", {}).get("operator_can_revert") is True
            ),
            "provider_count": len(providers),
            "provider_parity_dimension_count": len(providers[0]["dimensions"]) if providers else 0,
            "provider_privacy_regression_count": sum(
                1 for item in providers if item.get("privacy_regression_detected") is True
            ),
            "provider_failed_dimension_count": sum(
                len(item.get("failed_dimensions", []) or []) for item in providers
            ),
            "provider_promotion_blocked_count": sum(
                1 for item in providers if item.get("promotion_blocked") is True
            ),
            "secret_or_credential_leak_count": sum(
                int(item.get("secret_or_credential_leak_count", 0) or 0) for item in providers
            ),
            "unredacted_identifier_count": sum(
                int(item.get("unredacted_identifier_count", 0) or 0) for item in providers
            ),
            "provider_quarantine_count": sum(
                1 for item in providers
                if item.get("quarantine_state") in {"quarantined", "review_for_reinstatement"}
            ),
            "provider_canonical_override_blocked_count": sum(
                1 for item in providers if item.get("canonical_authority") != "source_of_truth"
            ),
            "delete_export_receipt_count": sum(
                1 for item in providers
                if item.get("delete_export", {}).get("delete_receipt") is True
                and item.get("delete_export", {}).get("export_receipt") is True
            ),
            "claim_boundary": INDEPENDENT_LEARNING_MEMORY_PARITY_CLAIM_BOUNDARY,
        },
        "independent_outcome_cohorts": cohorts,
        "task_scoped_causal_attribution": causal,
        "memory_provider_parity_matrix": providers,
        "policy": policy,
    }


def _failure_report(summary: Any, *, suite_name: str) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "suite": suite_name,
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(
                getattr(result, "error", "") or "Independent learning memory-provider parity-matrix scenario failed."
            ),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


async def _run_independent_learning_memory_parity_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        INDEPENDENT_OUTCOME_COHORT_REVIEW_SUITE_NAME,
        TASK_SCOPED_CAUSAL_LEARNING_SUITE_NAME,
        MEMORY_PROVIDER_PARITY_MATRIX_SUITE_NAME,
    ])


async def build_independent_learning_memory_parity_report() -> dict[str, Any]:
    summary = await _run_independent_learning_memory_parity_suites()
    contract = build_independent_learning_memory_parity_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "independent_learning_memory_parity_ci_gated_operator_visible"
                if healthy
                else "independent_learning_memory_parity_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(INDEPENDENT_OUTCOME_COHORT_REVIEW_SCENARIO_NAMES)
                + len(TASK_SCOPED_CAUSAL_LEARNING_SCENARIO_NAMES)
                + len(MEMORY_PROVIDER_PARITY_MATRIX_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            INDEPENDENT_OUTCOME_COHORT_REVIEW_SUITE_NAME: list(
                INDEPENDENT_OUTCOME_COHORT_REVIEW_SCENARIO_NAMES
            ),
            TASK_SCOPED_CAUSAL_LEARNING_SUITE_NAME: list(TASK_SCOPED_CAUSAL_LEARNING_SCENARIO_NAMES),
            MEMORY_PROVIDER_PARITY_MATRIX_SUITE_NAME: list(MEMORY_PROVIDER_PARITY_MATRIX_SCENARIO_NAMES),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="independent_learning_memory_parity"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
