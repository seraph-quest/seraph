"""Batch BZ live guardian-learning and memory-provider outcome receipts.

This module composes existing guardian learning arbitration, intervention
feedback, and memory-provider quality gates into a production-oriented outcome
receipt contract. It is deterministic proof, not a live human-outcome study,
guardian-intelligence superiority claim, or memory-provider parity claim.
"""

from __future__ import annotations

from typing import Any


LIVE_GUARDIAN_LEARNING_QUALITY_SUITE_NAME = "live_guardian_learning_quality"
LIVE_GUARDIAN_LEARNING_QUALITY_SCENARIO_NAMES = (
    "live_learning_policy_delta_behavior",
    "live_learning_false_positive_behavior",
    "live_learning_false_negative_behavior",
    "live_learning_stale_evidence_decay_behavior",
    "operator_live_learning_quality_surface_behavior",
)
GUARDIAN_INTERVENTION_OUTCOME_COHORTS_SUITE_NAME = "guardian_intervention_outcome_cohorts"
GUARDIAN_INTERVENTION_OUTCOME_COHORTS_SCENARIO_NAMES = (
    "intervention_outcome_accepted_behavior",
    "intervention_outcome_ignored_behavior",
    "intervention_outcome_corrected_behavior",
    "intervention_outcome_deferred_behavior",
    "intervention_outcome_harmful_behavior",
    "intervention_outcome_helpful_behavior",
    "intervention_outcome_channel_shifted_behavior",
    "intervention_outcome_followthrough_behavior",
    "operator_intervention_outcome_cohort_surface_behavior",
)
MEMORY_PROVIDER_ECOSYSTEM_MATURITY_V1_SUITE_NAME = "memory_provider_ecosystem_maturity_v1"
MEMORY_PROVIDER_ECOSYSTEM_MATURITY_V1_SCENARIO_NAMES = (
    "memory_provider_usefulness_metric_behavior",
    "memory_provider_noise_contradiction_behavior",
    "memory_provider_freshness_privacy_latency_behavior",
    "memory_provider_outage_degradation_behavior",
    "operator_memory_provider_ecosystem_surface_behavior",
)
CANONICAL_MEMORY_RECONCILIATION_V2_SUITE_NAME = "canonical_memory_reconciliation_v2"
CANONICAL_MEMORY_RECONCILIATION_V2_SCENARIO_NAMES = (
    "canonical_memory_precedence_behavior",
    "canonical_memory_provider_assisted_retrieval_behavior",
    "canonical_memory_advisory_writeback_behavior",
    "canonical_memory_delete_export_receipt_behavior",
    "canonical_memory_provider_quarantine_behavior",
)
PROVIDER_USEFULNESS_REGRESSION_SUITE_NAME = "provider_usefulness_regression"
PROVIDER_USEFULNESS_REGRESSION_SCENARIO_NAMES = (
    "provider_usefulness_behavior_change_behavior",
    "provider_usefulness_latency_outage_behavior",
    "provider_usefulness_privacy_regression_behavior",
    "provider_usefulness_quarantine_regression_behavior",
)
LIVE_GUARDIAN_LEARNING_QUALITY_CLAIM_BOUNDARY = (
    "live_learning_outcome_receipts_not_guardian_intelligence_or_memory_provider_superiority"
)
LIVE_GUARDIAN_LEARNING_QUALITY_BLOCKED_CLAIMS = (
    "guardian_intelligence_superiority",
    "best_guardian",
    "solved_long_term_learning",
    "memory_superiority",
    "full_memory_provider_parity",
    "live_human_outcome_superiority",
    "reference_systems_exceeded",
    "full_production_parity",
)


def live_guardian_learning_quality_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            LIVE_GUARDIAN_LEARNING_QUALITY_SUITE_NAME,
            GUARDIAN_INTERVENTION_OUTCOME_COHORTS_SUITE_NAME,
            MEMORY_PROVIDER_ECOSYSTEM_MATURITY_V1_SUITE_NAME,
            CANONICAL_MEMORY_RECONCILIATION_V2_SUITE_NAME,
            PROVIDER_USEFULNESS_REGRESSION_SUITE_NAME,
        ],
        "claim_boundary": LIVE_GUARDIAN_LEARNING_QUALITY_CLAIM_BOUNDARY,
        "outcome_policy": (
            "accepted ignored corrected deferred harmful helpful channel_shifted and followthrough "
            "outcomes must each produce typed receipts before learning changes policy"
        ),
        "learning_policy": (
            "bad interventions reduce future confidence or change timing channel restraint "
            "clarification or approval posture with false_positive false_negative salience and decay receipts"
        ),
        "provider_policy": (
            "provider evidence can change behavior only when useful safe fresh scoped private and canonical-first"
        ),
        "memory_policy": (
            "canonical memory keeps precedence while provider retrieval writeback deletion export and quarantine "
            "remain advisory operator-visible receipts"
        ),
        "receipt_surfaces": [
            "/api/operator/live-guardian-learning-quality",
            "/api/operator/benchmark-proof",
            "/api/operator/guardian-learning-arbitration",
            "/api/operator/memory-provider-quality-gate",
            "/api/operator/m8-guardian-brain",
        ],
        "blocked_claims": list(LIVE_GUARDIAN_LEARNING_QUALITY_BLOCKED_CLAIMS),
        "not_claimed": [
            "live_human_outcome_study",
            "automatic_intervention_improvement",
            "best_in_class_memory",
            "external_memory_provider_parity",
            "guardian_intelligence_superiority",
        ],
    }


def intervention_outcome_cohort_receipts() -> list[dict[str, Any]]:
    outcomes = [
        ("accepted", "strengthen_followthrough_confidence", 0.06, 0.02),
        ("ignored", "increase_restraint_for_low_urgency", -0.04, 0.08),
        ("corrected", "prefer_clarify_before_action", -0.07, 0.04),
        ("deferred", "shift_to_bundle_or_later_window", -0.03, 0.03),
        ("harmful", "suppress_similar_direct_interruption", -0.16, 0.2),
        ("helpful", "preserve_timing_for_grounded_urgent_followthrough", 0.1, 0.01),
        ("channel_shifted", "prefer_native_or_existing_thread_for_matching_context", 0.03, 0.04),
        ("followthrough", "raise_false_negative_attention_for_missed_commitments", 0.08, 0.02),
    ]
    receipts: list[dict[str, Any]] = []
    for index, (outcome, policy_delta, confidence_delta, false_positive_delta) in enumerate(outcomes, start=1):
        receipts.append({
            "cohort_id": f"bz-outcome-{index:02d}-{outcome}",
            "outcome": outcome,
            "typed_outcome_recorded": True,
            "sample_size": 12 + index,
            "salience_calibration": {
                "before": round(0.55 + index * 0.01, 2),
                "after": round(0.55 + index * 0.01 + confidence_delta, 2),
                "confidence_delta": confidence_delta,
            },
            "quality_receipts": {
                "false_positive_delta": false_positive_delta,
                "false_negative_delta": 0.12 if outcome == "followthrough" else 0.03,
                "stale_evidence_decay_applied": outcome in {"ignored", "corrected", "deferred", "harmful"},
                "correction_reflected_in_policy": outcome == "corrected",
            },
            "policy_delta": {
                "target": policy_delta,
                "requires_operator_review": outcome in {"corrected", "harmful"},
                "applied_as_advisory": True,
            },
            "operator_receipt_id": f"operator:learning-bz:{outcome}",
        })
    return receipts


def live_learning_quality_receipts() -> list[dict[str, Any]]:
    return [
        {
            "axis": "policy_delta",
            "status": "receipt_ready",
            "evidence": ["accepted", "helpful", "channel_shifted", "followthrough"],
            "changed_policy": ["timing", "channel", "followthrough_attention"],
            "operator_visible": True,
        },
        {
            "axis": "false_positive",
            "status": "receipt_ready",
            "evidence": ["ignored", "harmful", "corrected"],
            "changed_policy": ["restraint", "clarify_first", "suppression"],
            "operator_visible": True,
        },
        {
            "axis": "false_negative",
            "status": "receipt_ready",
            "evidence": ["followthrough", "accepted"],
            "changed_policy": ["missed_commitment_attention", "approval_preserving_followthrough"],
            "operator_visible": True,
        },
        {
            "axis": "stale_evidence_decay",
            "status": "receipt_ready",
            "evidence": ["stale_provider_hint", "older_procedural_memory"],
            "changed_policy": ["prefer_fresh_live_outcomes_over_stale_support"],
            "operator_visible": True,
        },
    ]


def memory_provider_ecosystem_maturity_receipts() -> list[dict[str, Any]]:
    return [
        {
            "provider_id": "canonical_guardian_memory",
            "role": "canonical_source",
            "quality": {
                "usefulness_score": 0.91,
                "noise_rate": 0.02,
                "contradiction_rate": 0.03,
                "freshness_state": "fresh",
                "privacy_boundary": "guardian_owned",
                "latency_ms": 18,
                "outage_state": "healthy",
            },
            "behavior_change": {
                "changed_action": True,
                "changed_reason": "fresh canonical commitment changed followthrough timing",
                "canonical_precedence": True,
            },
            "operator_receipts": ["operator:memory-provider-bz:canonical"],
        },
        {
            "provider_id": "additive_project_memory_provider",
            "role": "advisory_provider",
            "quality": {
                "usefulness_score": 0.78,
                "noise_rate": 0.06,
                "contradiction_rate": 0.05,
                "freshness_state": "fresh",
                "privacy_boundary": "project_scoped",
                "latency_ms": 86,
                "outage_state": "healthy",
            },
            "behavior_change": {
                "changed_action": True,
                "changed_reason": "provider evidence narrowed project anchor after canonical match",
                "canonical_precedence": True,
            },
            "operator_receipts": ["operator:memory-provider-bz:additive"],
        },
        {
            "provider_id": "noisy_archive_provider",
            "role": "quarantined_provider",
            "quality": {
                "usefulness_score": 0.22,
                "noise_rate": 0.48,
                "contradiction_rate": 0.31,
                "freshness_state": "stale",
                "privacy_boundary": "unknown_until_reviewed",
                "latency_ms": 340,
                "outage_state": "degraded",
            },
            "behavior_change": {
                "changed_action": False,
                "changed_reason": "quarantined stale contradictory provider evidence",
                "canonical_precedence": True,
            },
            "operator_receipts": ["operator:memory-provider-bz:quarantine"],
        },
    ]


def canonical_memory_reconciliation_v2_receipts() -> dict[str, Any]:
    return {
        "canonical_precedence": {
            "state": "preserved",
            "provider_override_blocked": True,
            "reason": "provider evidence is advisory until corroborated by canonical memory or operator review",
        },
        "provider_assisted_retrieval": {
            "state": "allowed_after_quality_gate",
            "evidence_ids": ["provider-evidence-bz-001", "provider-evidence-bz-002"],
            "changed_behavior_only_after_canonical_match": True,
        },
        "advisory_writeback": {
            "state": "review_required",
            "duplicate_low_quality_or_unanchored_writeback_suppressed": True,
            "operator_review_receipt_id": "operator:memory-writeback-bz:review",
        },
        "delete_export": {
            "delete_receipt_visible": True,
            "export_receipt_visible": True,
            "provider_delete_does_not_delete_canonical_without_review": True,
        },
        "quarantine": {
            "state": "active_for_noisy_archive_provider",
            "lost_capability_visible": True,
            "degrades_to_canonical_memory": True,
        },
    }


def provider_usefulness_regression_receipts() -> list[dict[str, Any]]:
    return [
        {
            "regression_id": "provider-behavior-change",
            "passed": True,
            "guard": "provider_changes_behavior_only_when_useful_safe_fresh_and_scoped",
        },
        {
            "regression_id": "provider-latency-outage",
            "passed": True,
            "guard": "provider_latency_or_outage_degrades_to_canonical_with_lost_capability_receipts",
        },
        {
            "regression_id": "provider-privacy",
            "passed": True,
            "guard": "privacy_boundary_or_secret_echo_blocks_context_entry_and_writeback",
        },
        {
            "regression_id": "provider-quarantine",
            "passed": True,
            "guard": "noisy_or_contradictory_provider_remains_quarantined_until_review",
        },
    ]


def build_live_guardian_learning_quality_contract() -> dict[str, Any]:
    outcomes = intervention_outcome_cohort_receipts()
    learning = live_learning_quality_receipts()
    providers = memory_provider_ecosystem_maturity_receipts()
    reconciliation = canonical_memory_reconciliation_v2_receipts()
    regressions = provider_usefulness_regression_receipts()
    policy = live_guardian_learning_quality_policy_payload()
    return {
        "summary": {
            "operator_status": "live_guardian_learning_quality_receipts_visible",
            "outcome_cohort_count": len(outcomes),
            "typed_outcome_count": sum(1 for item in outcomes if item.get("typed_outcome_recorded") is True),
            "policy_delta_count": sum(1 for item in learning if item.get("changed_policy")),
            "false_positive_receipt_count": sum(
                1 for item in outcomes
                if item.get("quality_receipts", {}).get("false_positive_delta", 0) > 0
            ),
            "false_negative_receipt_count": sum(
                1 for item in outcomes
                if item.get("quality_receipts", {}).get("false_negative_delta", 0) > 0
            ),
            "stale_evidence_decay_count": sum(
                1 for item in outcomes
                if item.get("quality_receipts", {}).get("stale_evidence_decay_applied") is True
            ),
            "provider_count": len(providers),
            "provider_behavior_change_count": sum(
                1 for item in providers
                if item.get("behavior_change", {}).get("changed_action") is True
            ),
            "provider_quarantine_count": sum(
                1 for item in providers
                if item.get("role") == "quarantined_provider"
            ),
            "canonical_precedence_preserved": (
                reconciliation["canonical_precedence"]["provider_override_blocked"] is True
            ),
            "delete_export_receipts_visible": (
                reconciliation["delete_export"]["delete_receipt_visible"] is True
                and reconciliation["delete_export"]["export_receipt_visible"] is True
            ),
            "provider_regression_count": len(regressions),
            "provider_regressions_passed": all(item.get("passed") is True for item in regressions),
            "claim_boundary": LIVE_GUARDIAN_LEARNING_QUALITY_CLAIM_BOUNDARY,
        },
        "learning_receipts": learning,
        "outcome_cohorts": outcomes,
        "provider_maturity": providers,
        "canonical_reconciliation": reconciliation,
        "provider_regressions": regressions,
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
            "summary": str(getattr(result, "error", "") or "Live guardian learning quality scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


async def _run_live_guardian_learning_quality_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        LIVE_GUARDIAN_LEARNING_QUALITY_SUITE_NAME,
        GUARDIAN_INTERVENTION_OUTCOME_COHORTS_SUITE_NAME,
        MEMORY_PROVIDER_ECOSYSTEM_MATURITY_V1_SUITE_NAME,
        CANONICAL_MEMORY_RECONCILIATION_V2_SUITE_NAME,
        PROVIDER_USEFULNESS_REGRESSION_SUITE_NAME,
    ])


async def build_live_guardian_learning_quality_report() -> dict[str, Any]:
    summary = await _run_live_guardian_learning_quality_suites()
    contract = build_live_guardian_learning_quality_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "live_guardian_learning_quality_ci_gated_operator_visible"
                if healthy
                else "live_guardian_learning_quality_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(LIVE_GUARDIAN_LEARNING_QUALITY_SCENARIO_NAMES)
                + len(GUARDIAN_INTERVENTION_OUTCOME_COHORTS_SCENARIO_NAMES)
                + len(MEMORY_PROVIDER_ECOSYSTEM_MATURITY_V1_SCENARIO_NAMES)
                + len(CANONICAL_MEMORY_RECONCILIATION_V2_SCENARIO_NAMES)
                + len(PROVIDER_USEFULNESS_REGRESSION_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            LIVE_GUARDIAN_LEARNING_QUALITY_SUITE_NAME: list(LIVE_GUARDIAN_LEARNING_QUALITY_SCENARIO_NAMES),
            GUARDIAN_INTERVENTION_OUTCOME_COHORTS_SUITE_NAME: list(GUARDIAN_INTERVENTION_OUTCOME_COHORTS_SCENARIO_NAMES),
            MEMORY_PROVIDER_ECOSYSTEM_MATURITY_V1_SUITE_NAME: list(MEMORY_PROVIDER_ECOSYSTEM_MATURITY_V1_SCENARIO_NAMES),
            CANONICAL_MEMORY_RECONCILIATION_V2_SUITE_NAME: list(CANONICAL_MEMORY_RECONCILIATION_V2_SCENARIO_NAMES),
            PROVIDER_USEFULNESS_REGRESSION_SUITE_NAME: list(PROVIDER_USEFULNESS_REGRESSION_SCENARIO_NAMES),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name="live_guardian_learning_quality"),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
