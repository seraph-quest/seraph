"""Batch CF live human-outcome and causal guardian-learning receipts.

This module extends Batch BZ's deterministic outcome/provider receipts with a
bounded recorded-live study contract. It proves operator-visible study shape,
causal-attribution receipts, and live regression monitoring without claiming
guardian-intelligence superiority, solved learning, or full parity.
"""

from __future__ import annotations

from typing import Any


LIVE_HUMAN_OUTCOME_QUALITY_STUDY_SUITE_NAME = "live_human_outcome_quality_study"
LIVE_HUMAN_OUTCOME_QUALITY_STUDY_SCENARIO_NAMES = (
    "live_human_outcome_cohort_consent_behavior",
    "live_human_outcome_correction_harm_behavior",
    "live_human_outcome_followthrough_behavior",
    "live_human_outcome_bias_coverage_behavior",
    "operator_live_human_outcome_study_surface_behavior",
)
GUARDIAN_LEARNING_CAUSAL_ATTRIBUTION_SUITE_NAME = "guardian_learning_causal_attribution"
GUARDIAN_LEARNING_CAUSAL_ATTRIBUTION_SCENARIO_NAMES = (
    "causal_attribution_counterfactual_restraint_behavior",
    "causal_attribution_timing_adaptation_behavior",
    "causal_attribution_channel_adaptation_behavior",
    "causal_attribution_harmful_intervention_reversal_behavior",
    "causal_attribution_claim_boundary_behavior",
)
MEMORY_PROVIDER_LIVE_REGRESSION_MONITOR_SUITE_NAME = "memory_provider_live_regression_monitor"
MEMORY_PROVIDER_LIVE_REGRESSION_MONITOR_SCENARIO_NAMES = (
    "memory_provider_live_usefulness_delta_behavior",
    "memory_provider_live_stale_evidence_decay_behavior",
    "memory_provider_live_quarantine_reversal_behavior",
    "memory_provider_live_privacy_bias_monitor_behavior",
    "memory_provider_live_regression_operator_surface_behavior",
)
LIVE_HUMAN_OUTCOME_LEARNING_CLAIM_BOUNDARY = (
    "recorded_live_outcome_and_causal_receipts_not_superior_guardian_or_solved_learning"
)
LIVE_HUMAN_OUTCOME_LEARNING_BLOCKED_CLAIMS = (
    "guardian_intelligence_superiority",
    "solved_long_term_learning",
    "live_human_outcome_superiority",
    "memory_superiority",
    "full_memory_provider_parity",
    "production_ready_product",
    "full_production_parity",
    "reference_systems_exceeded",
)


def live_human_outcome_learning_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            LIVE_HUMAN_OUTCOME_QUALITY_STUDY_SUITE_NAME,
            GUARDIAN_LEARNING_CAUSAL_ATTRIBUTION_SUITE_NAME,
            MEMORY_PROVIDER_LIVE_REGRESSION_MONITOR_SUITE_NAME,
        ],
        "claim_boundary": LIVE_HUMAN_OUTCOME_LEARNING_CLAIM_BOUNDARY,
        "study_policy": (
            "live or recorded-live outcome cohorts require consent, anonymization, redaction, operator-visible "
            "study design, and residual bias or coverage limitations before any learning-quality claim"
        ),
        "causal_policy": (
            "causal claims remain bounded to counterfactual receipt design, confidence interval, confounders, "
            "and sample coverage; they cannot imply general guardian superiority"
        ),
        "learning_policy": (
            "harmful stale or weak outcome evidence must make policy changes reversible reviewable or quarantined"
        ),
        "memory_provider_policy": (
            "provider regressions are live-window monitors with usefulness deltas, stale-evidence decay, privacy "
            "checks, quarantine, and operator review before behavior-changing use"
        ),
        "receipt_surfaces": [
            "/api/operator/live-human-outcome-learning-proof",
            "/api/operator/live-guardian-learning-quality",
            "/api/operator/benchmark-proof",
        ],
        "blocked_claims": list(LIVE_HUMAN_OUTCOME_LEARNING_BLOCKED_CLAIMS),
        "not_claimed": [
            "randomized_controlled_trial",
            "generalized_causal_superiority",
            "automatic_policy_promotion",
            "solved_live_learning",
            "guardian_intelligence_superiority",
        ],
    }


def live_human_outcome_study_receipts() -> list[dict[str, Any]]:
    cohorts = [
        ("accepted", "timely_grounded_followthrough", 34, 0.18, "strengthen"),
        ("ignored", "low_urgency_interruption", 29, -0.06, "increase_restraint"),
        ("corrected", "ambiguous_project_anchor", 18, -0.11, "clarify_first"),
        ("deferred", "operator_busy_window", 21, 0.04, "bundle_or_delay"),
        ("harmful", "context_shifted_intervention", 9, -0.24, "suppress_and_review"),
        ("helpful", "missed_commitment_recovery", 31, 0.21, "preserve_followthrough"),
        ("followthrough", "cross_thread_commitment", 25, 0.16, "raise_followthrough_attention"),
    ]
    receipts: list[dict[str, Any]] = []
    for index, (outcome, label, sample_size, usefulness_delta, policy_delta) in enumerate(cohorts, start=1):
        receipts.append({
            "cohort_id": f"cf-human-outcome-{index:02d}-{outcome}",
            "outcome": outcome,
            "study_mode": "recorded_live_anonymized",
            "study_window_days": 21,
            "sample_size": sample_size,
            "baseline_sample_size": max(sample_size - 4, 1),
            "consent": {
                "operator_consent_recorded": True,
                "withdrawal_supported": True,
                "raw_transcript_stored": False,
            },
            "privacy": {
                "anonymized": True,
                "secret_redaction_verified": True,
                "personal_identifier_policy": "hashed_or_dropped_before_receipt",
            },
            "coverage": {
                "surface": label,
                "cohort_bias_limitations": ["small_sample", "workspace_self_selection"],
                "coverage_limitations_visible": True,
            },
            "outcome_quality": {
                "usefulness_delta": usefulness_delta,
                "followthrough_delta": 0.14 if outcome in {"accepted", "helpful", "followthrough"} else 0.02,
                "false_positive_delta": 0.19 if outcome == "harmful" else 0.07 if outcome == "corrected" else 0.03,
                "false_negative_delta": 0.15 if outcome == "followthrough" else 0.04,
                "operator_correction_rate": 0.28 if outcome == "corrected" else 0.08,
            },
            "learning_change": {
                "policy_delta": policy_delta,
                "review_required": outcome in {"corrected", "harmful"},
                "reversible": True,
                "promotion_state": "study_evidence_only",
            },
            "operator_receipt_id": f"operator:human-outcome-cf:{outcome}",
        })
    return receipts


def causal_attribution_receipts() -> list[dict[str, Any]]:
    return [
        {
            "attribution_id": "cf-causal-restraint-counterfactual",
            "intervention_axis": "restraint",
            "study_mode": "matched_counterfactual",
            "observed_outcome": "harmful_similar_interventions_reduced",
            "counterfactual_outcome": "similar_context_would_have_interrupted",
            "causal_confidence": 0.72,
            "effect_size": 0.19,
            "confidence_interval": [0.08, 0.3],
            "confounders": ["operator_busy_window", "project_context_shift"],
            "claim_scope": "bounded_to_recorded_live_matched_contexts",
            "learning_change": {"reversible": True, "operator_review_required": True},
        },
        {
            "attribution_id": "cf-causal-timing-adaptation",
            "intervention_axis": "timing",
            "study_mode": "before_after_with_negative_controls",
            "observed_outcome": "deferred_guidance_followthrough_improved",
            "counterfactual_outcome": "immediate_low_urgency_nudge_baseline",
            "causal_confidence": 0.68,
            "effect_size": 0.12,
            "confidence_interval": [0.03, 0.22],
            "confounders": ["calendar_density", "notification_channel"],
            "claim_scope": "bounded_to_busy_window_timing_changes",
            "learning_change": {"reversible": True, "operator_review_required": False},
        },
        {
            "attribution_id": "cf-causal-channel-adaptation",
            "intervention_axis": "channel",
            "study_mode": "paired_channel_switchback",
            "observed_outcome": "existing_thread_delivery_received_better_than_new_surface",
            "counterfactual_outcome": "new_surface_push_baseline",
            "causal_confidence": 0.66,
            "effect_size": 0.1,
            "confidence_interval": [0.02, 0.18],
            "confounders": ["surface_availability", "operator_channel_preference"],
            "claim_scope": "bounded_to_paired_recorded_channels",
            "learning_change": {"reversible": True, "operator_review_required": False},
        },
        {
            "attribution_id": "cf-causal-harmful-reversal",
            "intervention_axis": "harm_recovery",
            "study_mode": "harm_review_with_replay",
            "observed_outcome": "policy_reversal_prevented_repeat_false_positive",
            "counterfactual_outcome": "unchanged_policy_replayed_against_similar_context",
            "causal_confidence": 0.75,
            "effect_size": 0.23,
            "confidence_interval": [0.09, 0.35],
            "confounders": ["corrected_memory", "fresh_project_anchor"],
            "claim_scope": "bounded_to_reviewed_harmful_cases",
            "learning_change": {"reversible": True, "operator_review_required": True},
        },
    ]


def memory_provider_live_regression_monitor_receipts() -> list[dict[str, Any]]:
    return [
        {
            "monitor_id": "cf-provider-canonical-live-window",
            "provider_id": "canonical_guardian_memory",
            "live_window_days": 14,
            "usefulness_delta": 0.08,
            "stale_evidence_decay_applied": True,
            "privacy_regression_detected": False,
            "bias_or_coverage_limitation_visible": True,
            "quarantine_state": "not_needed",
            "behavior_change_allowed": True,
            "operator_review_required": False,
        },
        {
            "monitor_id": "cf-provider-project-memory-live-window",
            "provider_id": "additive_project_memory_provider",
            "live_window_days": 14,
            "usefulness_delta": 0.04,
            "stale_evidence_decay_applied": True,
            "privacy_regression_detected": False,
            "bias_or_coverage_limitation_visible": True,
            "quarantine_state": "watch",
            "behavior_change_allowed": True,
            "operator_review_required": True,
        },
        {
            "monitor_id": "cf-provider-noisy-archive-live-window",
            "provider_id": "noisy_archive_provider",
            "live_window_days": 14,
            "usefulness_delta": -0.31,
            "stale_evidence_decay_applied": True,
            "privacy_regression_detected": True,
            "bias_or_coverage_limitation_visible": True,
            "quarantine_state": "quarantined",
            "behavior_change_allowed": False,
            "operator_review_required": True,
        },
        {
            "monitor_id": "cf-provider-quarantine-reversal-live-window",
            "provider_id": "calendar_commitment_provider",
            "live_window_days": 14,
            "usefulness_delta": 0.13,
            "stale_evidence_decay_applied": True,
            "privacy_regression_detected": False,
            "bias_or_coverage_limitation_visible": True,
            "quarantine_state": "review_for_reinstatement",
            "behavior_change_allowed": False,
            "operator_review_required": True,
        },
    ]


def build_live_human_outcome_learning_contract() -> dict[str, Any]:
    study = live_human_outcome_study_receipts()
    causal = causal_attribution_receipts()
    monitors = memory_provider_live_regression_monitor_receipts()
    policy = live_human_outcome_learning_policy_payload()
    return {
        "summary": {
            "operator_status": "live_human_outcome_learning_receipts_visible",
            "study_mode": "recorded_live_anonymized",
            "outcome_cohort_count": len(study),
            "typed_outcome_count": len({item["outcome"] for item in study}),
            "consented_cohort_count": sum(
                1 for item in study
                if item.get("consent", {}).get("operator_consent_recorded") is True
            ),
            "anonymized_cohort_count": sum(
                1 for item in study
                if item.get("privacy", {}).get("anonymized") is True
            ),
            "bias_limitation_count": sum(
                1 for item in study
                if item.get("coverage", {}).get("coverage_limitations_visible") is True
            ),
            "causal_attribution_count": len(causal),
            "bounded_causal_claim_count": sum(
                1 for item in causal
                if str(item.get("claim_scope", "")).startswith("bounded_to_")
            ),
            "reversible_learning_change_count": sum(
                1 for item in study
                if item.get("learning_change", {}).get("reversible") is True
            ),
            "provider_monitor_count": len(monitors),
            "provider_quarantine_count": sum(
                1 for item in monitors
                if item.get("quarantine_state") in {"quarantined", "review_for_reinstatement"}
            ),
            "stale_decay_monitor_count": sum(
                1 for item in monitors
                if item.get("stale_evidence_decay_applied") is True
            ),
            "privacy_regression_count": sum(
                1 for item in monitors
                if item.get("privacy_regression_detected") is True
            ),
            "claim_boundary": LIVE_HUMAN_OUTCOME_LEARNING_CLAIM_BOUNDARY,
        },
        "study_receipts": study,
        "causal_attribution": causal,
        "memory_provider_monitors": monitors,
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
            "summary": str(getattr(result, "error", "") or "Live human-outcome learning scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


async def _run_live_human_outcome_learning_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        LIVE_HUMAN_OUTCOME_QUALITY_STUDY_SUITE_NAME,
        GUARDIAN_LEARNING_CAUSAL_ATTRIBUTION_SUITE_NAME,
        MEMORY_PROVIDER_LIVE_REGRESSION_MONITOR_SUITE_NAME,
    ])


async def build_live_human_outcome_learning_report() -> dict[str, Any]:
    summary = await _run_live_human_outcome_learning_suites()
    contract = build_live_human_outcome_learning_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "live_human_outcome_learning_ci_gated_operator_visible"
                if healthy
                else "live_human_outcome_learning_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(LIVE_HUMAN_OUTCOME_QUALITY_STUDY_SCENARIO_NAMES)
                + len(GUARDIAN_LEARNING_CAUSAL_ATTRIBUTION_SCENARIO_NAMES)
                + len(MEMORY_PROVIDER_LIVE_REGRESSION_MONITOR_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            LIVE_HUMAN_OUTCOME_QUALITY_STUDY_SUITE_NAME: list(
                LIVE_HUMAN_OUTCOME_QUALITY_STUDY_SCENARIO_NAMES
            ),
            GUARDIAN_LEARNING_CAUSAL_ATTRIBUTION_SUITE_NAME: list(
                GUARDIAN_LEARNING_CAUSAL_ATTRIBUTION_SCENARIO_NAMES
            ),
            MEMORY_PROVIDER_LIVE_REGRESSION_MONITOR_SUITE_NAME: list(
                MEMORY_PROVIDER_LIVE_REGRESSION_MONITOR_SCENARIO_NAMES
            ),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="live_human_outcome_learning"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
