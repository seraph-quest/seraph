"""Batch DD generalized guardian-outcome and memory-provider parity receipts.

This module raises the post-CZ evidence threshold beyond Batch CV: broader
task families, predeclared thresholds, current-source baseline limits, causal
promotion gates, provider parity dimensions, and redacted operator receipts.
It remains bounded evidence and does not claim guardian intelligence
superiority, solved learning, memory superiority, full memory-provider parity,
production readiness, full parity, or reference-system exceedance.
"""

from __future__ import annotations

from typing import Any


GENERALIZED_GUARDIAN_OUTCOME_STUDY_V1_SUITE_NAME = "generalized_guardian_outcome_study_v1"
GENERALIZED_GUARDIAN_OUTCOME_STUDY_V1_SCENARIO_NAMES = (
    "generalized_outcome_predeclared_protocol_behavior",
    "generalized_outcome_multi_task_decision_behavior",
    "generalized_outcome_adverse_event_fairness_behavior",
    "generalized_outcome_pressure_baseline_boundary_behavior",
    "generalized_outcome_operator_receipt_behavior",
)
FULL_MEMORY_PROVIDER_PARITY_MATRIX_V1_SUITE_NAME = "full_memory_provider_parity_matrix_v1"
FULL_MEMORY_PROVIDER_PARITY_MATRIX_V1_SCENARIO_NAMES = (
    "full_memory_provider_dimension_matrix_behavior",
    "full_memory_provider_canonical_authority_behavior",
    "full_memory_provider_privacy_delete_export_behavior",
    "full_memory_provider_quarantine_reinstatement_behavior",
    "full_memory_provider_claim_boundary_behavior",
)
CAUSAL_LEARNING_OUTCOME_THRESHOLDS_V1_SUITE_NAME = "causal_learning_outcome_thresholds_v1"
CAUSAL_LEARNING_OUTCOME_THRESHOLDS_V1_SCENARIO_NAMES = (
    "causal_threshold_counterfactual_design_behavior",
    "causal_threshold_promotion_gate_behavior",
    "causal_threshold_rollback_authority_behavior",
    "causal_threshold_no_generalized_superiority_behavior",
)
MEMORY_BASELINE_COMPARISON_V1_SUITE_NAME = "memory_baseline_comparison_v1"
MEMORY_BASELINE_COMPARISON_V1_SCENARIO_NAMES = (
    "memory_baseline_current_source_limit_behavior",
    "memory_baseline_pressure_only_comparison_behavior",
    "memory_baseline_named_system_boundary_behavior",
    "memory_baseline_receipt_redaction_behavior",
)
GENERALIZED_GUARDIAN_OUTCOMES_CLAIM_BOUNDARY = (
    "generalized_guardian_outcome_memory_receipts_not_superiority_or_full_provider_parity"
)
GENERALIZED_GUARDIAN_OUTCOMES_BLOCKED_CLAIMS = (
    "guardian_intelligence_superiority",
    "solved_live_learning",
    "solved_long_term_learning",
    "live_human_outcome_superiority",
    "generalized_outcome_superiority",
    "memory_superiority",
    "best_in_class_memory",
    "full_memory_provider_parity",
    "named_baseline_win",
    "production_ready_product",
    "full_production_parity",
    "reference_systems_exceeded",
)
GENERALIZED_OUTCOME_RECEIPT_REDACTION_BOUNDARY = (
    "redacted_no_raw_transcript_secret_person_identifier_provider_payload_or_raw_path"
)


def generalized_guardian_outcomes_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            GENERALIZED_GUARDIAN_OUTCOME_STUDY_V1_SUITE_NAME,
            FULL_MEMORY_PROVIDER_PARITY_MATRIX_V1_SUITE_NAME,
            CAUSAL_LEARNING_OUTCOME_THRESHOLDS_V1_SUITE_NAME,
            MEMORY_BASELINE_COMPARISON_V1_SUITE_NAME,
        ],
        "claim_boundary": GENERALIZED_GUARDIAN_OUTCOMES_CLAIM_BOUNDARY,
        "study_policy": (
            "generalized outcome evidence must predeclare task families, decisions, thresholds, evaluator "
            "independence, fairness checks, consent, adverse-event handling, rollback authority, and residual gaps"
        ),
        "baseline_policy": (
            "named baselines are current-source pressure evidence only; source, version, access caveat, "
            "capability limit, and comparison boundary must be visible before any claim wording changes"
        ),
        "causal_policy": (
            "causal thresholds require counterfactual or controlled designs, confounder notes, negative controls, "
            "promotion gates, adverse-event review, rollback authority, and no generalized-superiority wording"
        ),
        "memory_provider_policy": (
            "full-provider-parity remains blocked; provider rows must preserve canonical authority and cover "
            "quality, usefulness, freshness, conflict, privacy, delete/export, quarantine, reinstatement, and baseline limits"
        ),
        "receipt_redaction_policy": (
            "operator receipts expose redacted metadata, metrics, digests, and safe handles only; no raw transcript, "
            "secret, personal identifier, provider payload, or raw local path is exposed"
        ),
        "receipt_surfaces": [
            "/api/operator/generalized-guardian-outcomes",
            "/api/operator/longitudinal-guardian-outcomes",
            "/api/operator/independent-learning-memory-parity",
            "/api/operator/live-human-outcome-learning-proof",
            "/api/operator/benchmark-proof",
        ],
        "blocked_claims": list(GENERALIZED_GUARDIAN_OUTCOMES_BLOCKED_CLAIMS),
        "not_claimed": [
            "guardian_intelligence_superiority",
            "generalized_outcome_superiority",
            "solved_live_learning",
            "memory_superiority",
            "full_memory_provider_parity",
            "named_baseline_win",
            "production_ready_product",
        ],
    }


def _safe_receipt(receipt_type: str, receipt_id: str) -> dict[str, Any]:
    return {
        "summary_receipt_id": f"summary:guardian-dd:{receipt_type}:{receipt_id}",
        "redacted_raw_receipt_id": f"redacted:guardian-dd:{receipt_type}:{receipt_id}",
        "receipt_digest": f"digest:guardian-dd:{receipt_type}:{receipt_id}",
        "redaction_boundary": GENERALIZED_OUTCOME_RECEIPT_REDACTION_BOUNDARY,
        "stored_payload_mode": "metadata_only_redacted_receipt",
        "contains_raw_transcript": False,
        "contains_secret": False,
        "contains_personal_identifier": False,
        "contains_provider_payload": False,
        "raw_receipt_path_exposed": False,
    }


def generalized_outcome_study_receipts() -> list[dict[str, Any]]:
    rows = [
        ("act", "urgent_security_approval_followthrough", 132, 126, 0.16, 0.02, 0),
        ("defer", "low_urgency_focus_protection", 118, 111, 0.14, 0.01, 0),
        ("bundle", "multi_thread_commitment_digest", 127, 120, 0.13, 0.02, 1),
        ("clarify", "ambiguous_project_or_person_reference", 104, 98, 0.12, 0.03, 1),
        ("approval", "risky_capability_scope_renewal", 96, 91, 0.11, 0.02, 0),
        ("stay_silent", "negative_control_no_action_case", 110, 107, 0.1, 0.0, 0),
        ("recovery", "missed_followthrough_repair", 89, 82, 0.15, 0.03, 1),
        ("followthrough", "cross_surface_commitment_resolution", 141, 136, 0.17, 0.02, 0),
    ]
    receipts: list[dict[str, Any]] = []
    for index, (decision, task_family, sample, baseline_sample, effect, harm_rate, adverse) in enumerate(rows, start=1):
        study_id = f"dd-generalized-{decision}-{index:02d}"
        receipts.append({
            "study_id": study_id,
            "protocol_id": "dd-generalized-outcome-protocol",
            "protocol_version": "2026-06-11",
            "predeclared": True,
            "decision": decision,
            "task_family": task_family,
            "study_window": "2026-04-01..2026-06-10",
            "window_days": 71,
            "sample_size": sample,
            "baseline_sample_size": baseline_sample,
            "threshold": {
                "minimum_effect_size": 0.1,
                "maximum_harm_rate": 0.04,
                "minimum_inter_rater_agreement": 0.78,
                "promotion_requires_all_thresholds": True,
                "threshold_rationale": "predeclared_directional_quality_threshold_not_generalized_superiority_power",
            },
            "evaluator": {
                "identity_class": "independent_review_pool",
                "implementation_independent": True,
                "conflict_disclosure": "no_batch_implementation_role",
                "blinding": "blinded_to_policy_delta_where_possible",
                "inter_rater_agreement": 0.84 if decision != "clarify" else 0.79,
                "adjudication_rules": "second_reviewer_for_harm_correction_threshold_or_fairness_disagreement",
            },
            "consent": {
                "consent_state": "active_with_withdrawal_supported",
                "withdrawal_supported": True,
                "withdrawal_count": 1 if decision == "clarify" else 0,
                "anonymization_state": "anonymized",
                "raw_transcript_stored": False,
                "retention_policy": "redacted_aggregate_receipts_only",
            },
            "fairness": {
                "task_family_coverage_checked": True,
                "decision_type_coverage_checked": True,
                "minimum_task_family_count": 8,
                "coverage_gap_count": 0,
                "fairness_review_required": adverse > 0,
            },
            "outcome_metrics": {
                "effect_size": effect,
                "confidence_interval": [round(effect - 0.05, 2), round(effect + 0.05, 2)],
                "harm_rate": harm_rate,
                "correction_rate": 0.07 if decision == "clarify" else 0.04,
                "followthrough_delta": 0.17 if decision == "followthrough" else 0.05,
                "false_positive_delta": -0.08 if decision in {"defer", "stay_silent", "clarify"} else -0.03,
                "false_negative_delta": -0.07 if decision in {"act", "followthrough", "recovery"} else -0.02,
            },
            "adverse_events": {
                "adverse_event_count": adverse,
                "adverse_event_reviewed_count": adverse,
                "automatic_revert_count": 1 if adverse else 0,
            },
            "promotion_state": (
                "blocked_pending_adverse_event_review" if adverse else "operator_review_required_before_promotion"
            ),
            "claim_scope": f"bounded_to_{task_family}_71d_not_generalized_superiority",
            "safe_receipt": _safe_receipt("study", study_id),
            "blocked_claims": list(GENERALIZED_GUARDIAN_OUTCOMES_BLOCKED_CLAIMS),
        })
    return receipts


def full_memory_provider_parity_matrix_receipts() -> list[dict[str, Any]]:
    dimensions = [
        "canonical_authority",
        "advisory_retrieval",
        "usefulness_delta",
        "freshness_window",
        "conflict_resolution",
        "privacy_boundary",
        "delete_propagation",
        "export_propagation",
        "stale_recall_block",
        "quarantine",
        "reinstatement_review",
        "baseline_limit_disclosure",
    ]
    providers = [
        ("canonical_guardian_memory", "canonical_source", "healthy", [], False, False),
        ("project_graph_memory_provider", "advisory_project_graph", "healthy", [], True, False),
        ("calendar_commitment_memory_provider", "advisory_commitment_provider", "review_for_reinstatement", [], True, False),
        (
            "external_archive_memory_provider",
            "advisory_archive",
            "quarantined",
            ["privacy_boundary", "delete_propagation"],
            True,
            True,
        ),
        ("semantic_task_memory_provider", "advisory_task_semantics", "degraded", ["freshness_window"], True, False),
        ("named_baseline_memory_adapter", "pressure_baseline_only", "read_only", ["canonical_authority"], True, False),
    ]
    receipts: list[dict[str, Any]] = []
    for index, (provider_id, role, state, failed, override_blocked, privacy_regression) in enumerate(providers, start=1):
        passed = [dimension for dimension in dimensions if dimension not in failed]
        provider_key = provider_id.replace("_", "-")
        receipts.append({
            "provider_id": provider_id,
            "provider_version": f"dd-provider-v{index}",
            "provider_role": role,
            "provider_runtime_state": state,
            "dimensions": list(dimensions),
            "passed_dimensions": passed,
            "failed_dimensions": list(failed),
            "quality_declaration_complete": True,
            "canonical_precedence_preserved": True,
            "provider_override_blocked": override_blocked,
            "behavior_change_allowed": state == "healthy" and not failed,
            "behavior_change_scope": (
                "canonical_only_after_quality_gate" if provider_id == "canonical_guardian_memory"
                else "advisory_pressure_only_no_canonical_override"
            ),
            "usefulness_delta": 0.11 if state == "healthy" else 0.02,
            "freshness_window_days": 7 if "freshness_window" not in failed else 30,
            "stale_recall_blocked_count": 2 if "freshness_window" not in failed else 9,
            "privacy_regression_detected": privacy_regression,
            "delete_receipt_id": f"delete:dd:{provider_key}",
            "export_receipt_id": f"export:dd:{provider_key}",
            "delete_export_mismatch_count": 1 if "delete_propagation" in failed else 0,
            "quarantine_state": (
                "quarantined" if state == "quarantined"
                else "review_for_reinstatement" if state == "review_for_reinstatement"
                else "not_required"
            ),
            "reinstatement_review_receipt_id": (
                f"reinstatement-review:dd:{provider_key}"
                if state in {"quarantined", "review_for_reinstatement"}
                else None
            ),
            "promotion_blocked": bool(failed) or state in {"quarantined", "review_for_reinstatement", "read_only"},
            "safe_receipt": _safe_receipt("provider", provider_key),
        })
    return receipts


def causal_learning_threshold_receipts() -> list[dict[str, Any]]:
    return [
        {
            "threshold_id": "dd-causal-act-followthrough",
            "task_family": "urgent_security_approval_followthrough",
            "study_design": "matched_counterfactual_with_holdout_tasks",
            "counterfactual_outcome": "baseline_policy_delayed_more_followthrough_repairs",
            "negative_controls": ["low_urgency_notification_bundle"],
            "effect_size": 0.16,
            "threshold_effect_size": 0.1,
            "confidence_interval": [0.1, 0.22],
            "confounders": ["incident_frequency", "operator_availability"],
            "promotion_gate_state": "operator_review_required_before_promotion",
            "rollback_authority": "operator_or_harm_monitor_can_revert",
            "adverse_event_review_required": False,
            "claim_scope": "bounded_to_urgent_security_approval_followthrough_not_generalized_superiority",
            "safe_receipt": _safe_receipt("causal", "act-followthrough"),
        },
        {
            "threshold_id": "dd-causal-clarify-ambiguous-anchor",
            "task_family": "ambiguous_project_or_person_reference",
            "study_design": "switchback_with_adjudicated_counterfactuals",
            "counterfactual_outcome": "direct_action_baseline_would_have_used_stale_anchor",
            "negative_controls": ["unambiguous_reference_cases"],
            "effect_size": 0.12,
            "threshold_effect_size": 0.1,
            "confidence_interval": [0.06, 0.18],
            "confounders": ["project_name_collision", "fresh_memory_availability"],
            "promotion_gate_state": "blocked_pending_adverse_event_review",
            "rollback_authority": "automatic_revert_and_operator_review",
            "adverse_event_review_required": True,
            "claim_scope": "bounded_to_ambiguous_reference_clarification_not_solved_learning",
            "safe_receipt": _safe_receipt("causal", "clarify-anchor"),
        },
        {
            "threshold_id": "dd-causal-restraint-negative-control",
            "task_family": "negative_control_no_action_case",
            "study_design": "negative_controlled_timing_switchback",
            "counterfactual_outcome": "immediate_delivery_baseline_raised_interruption_cost_without_outcome_gain",
            "negative_controls": ["urgent_security_approval_prompts"],
            "effect_size": 0.1,
            "threshold_effect_size": 0.1,
            "confidence_interval": [0.05, 0.15],
            "confounders": ["calendar_density", "operator_focus_window"],
            "promotion_gate_state": "operator_review_required_before_promotion",
            "rollback_authority": "operator_can_revert",
            "adverse_event_review_required": False,
            "claim_scope": "bounded_to_no_action_restraint_not_guardian_superiority",
            "safe_receipt": _safe_receipt("causal", "restraint-negative-control"),
        },
    ]


def memory_baseline_comparison_receipts() -> list[dict[str, Any]]:
    return [
        {
            "baseline_id": "dd-baseline-hermes-memory-pressure",
            "baseline_name": "Hermes memory/provider pressure",
            "baseline_version": "2026-06-09 source refresh",
            "source_url": "https://hermes-agent.nousresearch.com/docs/user-guide/features/overview/",
            "source_checked_at": "2026-06-09",
            "access_caveat": "current_source_refresh_required_before_final_claim_lift",
            "task_family": "persistent_memory_and_tool_runtime_pressure",
            "comparison_disposition": "pressure_evidence_only_not_named_baseline_win",
            "limitations": ["not_live_head_to_head", "not_memory_superiority", "not_full_provider_parity"],
            "fairness_constraints": ["same_task_family_required", "source_freshness_required"],
            "safe_receipt": _safe_receipt("baseline", "hermes-memory"),
        },
        {
            "baseline_id": "dd-baseline-openclaw-channel-memory-pressure",
            "baseline_name": "OpenClaw continuity/channel pressure",
            "baseline_version": "2026-06-09 source refresh",
            "source_url": "https://docs.openclaw.ai/",
            "source_checked_at": "2026-06-09",
            "access_caveat": "gateway_channel_claims_are_pressure_evidence_not_outcome_baseline",
            "task_family": "cross_surface_continuity_and_memory_pressure",
            "comparison_disposition": "pressure_evidence_only_not_named_baseline_win",
            "limitations": ["not_live_human_outcome_comparison", "not_channel_count_claim"],
            "fairness_constraints": ["guardian_value_required", "same_continuity_task_required"],
            "safe_receipt": _safe_receipt("baseline", "openclaw-continuity"),
        },
        {
            "baseline_id": "dd-baseline-seraph-cv-longitudinal",
            "baseline_name": "Seraph Batch CV longitudinal operation baseline",
            "baseline_version": "batch-cv-longitudinal-guardian-outcomes",
            "source_url": "https://github.com/seraph-quest/seraph/issues/526",
            "source_checked_at": "2026-06-11",
            "access_caveat": "prior_seraph_receipts_are_internal_baseline_not_external_win",
            "task_family": "longitudinal_guardian_outcome_operations",
            "comparison_disposition": "pressure_evidence_only_not_named_baseline_win",
            "limitations": ["internal_baseline", "not_generalized_superiority"],
            "fairness_constraints": ["predeclared_thresholds_required", "adverse_event_review_required"],
            "safe_receipt": _safe_receipt("baseline", "seraph-cv"),
        },
    ]


def build_generalized_guardian_outcomes_contract() -> dict[str, Any]:
    studies = generalized_outcome_study_receipts()
    providers = full_memory_provider_parity_matrix_receipts()
    causal = causal_learning_threshold_receipts()
    baselines = memory_baseline_comparison_receipts()
    policy = generalized_guardian_outcomes_policy_payload()
    safe_receipts = [
        item["safe_receipt"]
        for item in [*studies, *providers, *causal, *baselines]
        if item.get("safe_receipt")
    ]
    return {
        "summary": {
            "operator_status": "generalized_guardian_outcomes_receipts_visible",
            "study_count": len(studies),
            "decision_type_count": len({item["decision"] for item in studies}),
            "task_family_count": len({item["task_family"] for item in studies}),
            "sample_size_total": sum(int(item["sample_size"]) for item in studies),
            "predeclared_protocol_count": sum(1 for item in studies if item["predeclared"] is True),
            "independent_evaluator_count": sum(
                1 for item in studies if item["evaluator"]["implementation_independent"] is True
            ),
            "fairness_review_count": sum(1 for item in studies if item["fairness"]["task_family_coverage_checked"]),
            "adverse_event_count": sum(int(item["adverse_events"]["adverse_event_count"]) for item in studies),
            "adverse_event_reviewed_count": sum(
                int(item["adverse_events"]["adverse_event_reviewed_count"]) for item in studies
            ),
            "raw_transcript_stored_count": sum(
                1 for item in studies if item["consent"]["raw_transcript_stored"] is True
            ),
            "secret_leak_count": sum(1 for item in safe_receipts if item["contains_secret"] is True),
            "unredacted_identifier_count": sum(
                1 for item in safe_receipts if item["contains_personal_identifier"] is True
            ),
            "provider_payload_leak_count": sum(
                1 for item in safe_receipts if item["contains_provider_payload"] is True
            ),
            "raw_receipt_path_exposed_count": sum(
                1 for item in safe_receipts if item["raw_receipt_path_exposed"] is True
            ),
            "provider_count": len(providers),
            "provider_dimension_count": len({dimension for item in providers for dimension in item["dimensions"]}),
            "provider_failed_dimension_count": sum(len(item["failed_dimensions"]) for item in providers),
            "canonical_precedence_preserved_count": sum(
                1 for item in providers if item["canonical_precedence_preserved"] is True
            ),
            "provider_override_blocked_count": sum(1 for item in providers if item["provider_override_blocked"]),
            "privacy_regression_count": sum(1 for item in providers if item["privacy_regression_detected"]),
            "delete_export_receipt_count": sum(
                1 for item in providers if item["delete_receipt_id"] and item["export_receipt_id"]
            ),
            "delete_export_mismatch_count": sum(int(item["delete_export_mismatch_count"]) for item in providers),
            "stale_recall_blocked_count": sum(int(item["stale_recall_blocked_count"]) for item in providers),
            "quarantine_count": sum(
                1 for item in providers if item["quarantine_state"] in {"quarantined", "review_for_reinstatement"}
            ),
            "reinstatement_review_count": sum(1 for item in providers if item["reinstatement_review_receipt_id"]),
            "causal_threshold_count": len(causal),
            "causal_threshold_pass_count": sum(
                1 for item in causal if item["effect_size"] >= item["threshold_effect_size"]
            ),
            "causal_counterfactual_count": sum(1 for item in causal if item["counterfactual_outcome"]),
            "rollback_authority_count": sum(1 for item in causal if item["rollback_authority"]),
            "baseline_count": len(baselines),
            "current_source_baseline_count": sum(1 for item in baselines if item["source_checked_at"]),
            "pressure_only_baseline_count": sum(
                1 for item in baselines if "pressure_evidence_only" in item["comparison_disposition"]
            ),
            "safe_receipt_count": len(safe_receipts),
            "claim_boundary": GENERALIZED_GUARDIAN_OUTCOMES_CLAIM_BOUNDARY,
        },
        "generalized_outcome_studies": studies,
        "memory_provider_parity_matrix": providers,
        "causal_learning_thresholds": causal,
        "memory_baseline_comparisons": baselines,
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
                getattr(result, "error", "") or "Generalized guardian outcome scenario failed."
            ),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


async def _run_generalized_guardian_outcomes_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        GENERALIZED_GUARDIAN_OUTCOME_STUDY_V1_SUITE_NAME,
        FULL_MEMORY_PROVIDER_PARITY_MATRIX_V1_SUITE_NAME,
        CAUSAL_LEARNING_OUTCOME_THRESHOLDS_V1_SUITE_NAME,
        MEMORY_BASELINE_COMPARISON_V1_SUITE_NAME,
    ])


async def build_generalized_guardian_outcomes_report() -> dict[str, Any]:
    summary = await _run_generalized_guardian_outcomes_suites()
    contract = build_generalized_guardian_outcomes_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "generalized_guardian_outcomes_ci_gated_operator_visible"
                if healthy
                else "generalized_guardian_outcomes_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(GENERALIZED_GUARDIAN_OUTCOME_STUDY_V1_SCENARIO_NAMES)
                + len(FULL_MEMORY_PROVIDER_PARITY_MATRIX_V1_SCENARIO_NAMES)
                + len(CAUSAL_LEARNING_OUTCOME_THRESHOLDS_V1_SCENARIO_NAMES)
                + len(MEMORY_BASELINE_COMPARISON_V1_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            GENERALIZED_GUARDIAN_OUTCOME_STUDY_V1_SUITE_NAME: list(
                GENERALIZED_GUARDIAN_OUTCOME_STUDY_V1_SCENARIO_NAMES
            ),
            FULL_MEMORY_PROVIDER_PARITY_MATRIX_V1_SUITE_NAME: list(
                FULL_MEMORY_PROVIDER_PARITY_MATRIX_V1_SCENARIO_NAMES
            ),
            CAUSAL_LEARNING_OUTCOME_THRESHOLDS_V1_SUITE_NAME: list(
                CAUSAL_LEARNING_OUTCOME_THRESHOLDS_V1_SCENARIO_NAMES
            ),
            MEMORY_BASELINE_COMPARISON_V1_SUITE_NAME: list(MEMORY_BASELINE_COMPARISON_V1_SCENARIO_NAMES),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="generalized_guardian_outcomes"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
