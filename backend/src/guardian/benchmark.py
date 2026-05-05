from __future__ import annotations

from typing import Any

from src.guardian.brain import (
    M8_GUARDIAN_BRAIN_BENCHMARK_SCENARIO_NAMES,
    M8_GUARDIAN_BRAIN_BENCHMARK_SUITE_NAME,
    build_m8_guardian_brain_receipts,
)


GUARDIAN_USER_MODEL_BENCHMARK_SUITE_NAME = "guardian_user_model_restraint"
GUARDIAN_USER_MODEL_BENCHMARK_SCENARIO_NAMES = (
    "guardian_user_model_continuity_behavior",
    "guardian_clarification_restraint_behavior",
    "guardian_judgment_behavior",
    "operator_guardian_state_surface_behavior",
)


def guardian_user_model_benchmark_dimensions() -> list[dict[str, str]]:
    return [
        {
            "name": "persistent_user_model",
            "label": "Persistent user model",
            "summary": "Seraph should preserve explicit user-model evidence across memory, learning, and continuity instead of inferring preferences transiently per turn.",
        },
        {
            "name": "ambiguity_aware_clarification",
            "label": "Ambiguity-aware clarification",
            "summary": "When project anchors or referents are weak, Seraph should clarify before acting instead of guessing.",
        },
        {
            "name": "guardian_restraint",
            "label": "Guardian restraint",
            "summary": "User modeling should tighten action posture and delivery restraint rather than silently broadening personalization authority.",
        },
        {
            "name": "operator_receipts",
            "label": "Operator receipts",
            "summary": "Operators should be able to inspect facet evidence, watchpoints, restraint reasons, and action posture directly.",
        },
        {
            "name": "ci_regression_gating",
            "label": "CI regression gating",
            "summary": "User-model and clarification behavior should live in a named deterministic suite that can gate regressions.",
        },
    ]


def guardian_user_model_failure_taxonomy() -> list[dict[str, str]]:
    return [
        {
            "name": "overconfident_referent_resolution",
            "severity": "high",
            "summary": "Seraph chooses a target or meaning even though project-anchor evidence or referents remain ambiguous.",
        },
        {
            "name": "silent_personalization_override",
            "severity": "high",
            "summary": "Personalization evidence bypasses the canonical guardian world model and silently changes action policy.",
        },
        {
            "name": "hidden_user_model_drift",
            "severity": "medium",
            "summary": "User-model evidence changes over time without operator-visible receipts showing why.",
        },
        {
            "name": "missing_restraint_receipt",
            "severity": "medium",
            "summary": "Clarify, wait, or abstain decisions are made without explicit reasons or watchpoints.",
        },
        {
            "name": "ungated_ambiguity_regression",
            "severity": "medium",
            "summary": "Changes to user-model or ambiguity logic are not pinned by a named deterministic benchmark suite.",
        },
    ]


def guardian_user_model_benchmark_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suite": GUARDIAN_USER_MODEL_BENCHMARK_SUITE_NAME,
        "canonical_authority": "guardian_world_model",
        "clarify_before_action_policy": "required_on_high_ambiguity",
        "personalization_override_policy": "forbidden_without_canonical_receipt",
        "operator_visibility": "facet_evidence_watchpoints_and_restraint_receipts",
        "ci_gate_mode": "required_benchmark_suite",
    }


def _guardian_user_model_failure_report(summary: Any) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append(
            {
                "type": "benchmark_regression",
                "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
                "summary": str(getattr(result, "error", "") or "Benchmark scenario failed."),
                "reason": "deterministic_eval_failure",
            }
        )
    return failures[:6]


async def _run_guardian_user_model_benchmark_suite():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([GUARDIAN_USER_MODEL_BENCHMARK_SUITE_NAME])


async def build_guardian_user_model_benchmark_report() -> dict[str, Any]:
    summary = await _run_guardian_user_model_benchmark_suite()
    failure_report = _guardian_user_model_failure_report(summary)
    benchmark_posture = (
        "ci_gated_operator_visible"
        if summary.failed == 0
        else "ci_regressions_detected_operator_visible"
    )
    return {
        "summary": {
            "suite_name": GUARDIAN_USER_MODEL_BENCHMARK_SUITE_NAME,
            "benchmark_posture": benchmark_posture,
            "operator_status": "guardian_state_visible",
            "scenario_count": len(GUARDIAN_USER_MODEL_BENCHMARK_SCENARIO_NAMES),
            "dimension_count": len(guardian_user_model_benchmark_dimensions()),
            "failure_mode_count": len(guardian_user_model_failure_taxonomy()),
            "active_failure_count": summary.failed,
            "clarification_policy_state": "required_on_high_ambiguity",
            "restraint_policy_state": "clarify_or_wait_before_unverified_personalization",
        },
        "scenario_names": list(GUARDIAN_USER_MODEL_BENCHMARK_SCENARIO_NAMES),
        "dimensions": guardian_user_model_benchmark_dimensions(),
        "failure_taxonomy": guardian_user_model_failure_taxonomy(),
        "failure_report": failure_report,
        "policy": guardian_user_model_benchmark_policy_payload(),
        "latest_run": {
            "total": summary.total,
            "passed": summary.passed,
            "failed": summary.failed,
            "duration_ms": summary.duration_ms,
        },
    }


def m8_guardian_brain_benchmark_dimensions() -> list[dict[str, str]]:
    return [
        {
            "name": "capability_choice",
            "label": "Capability choice",
            "summary": "Guardian judgment should select the safest useful capability lane instead of blindly acting with the broadest tool.",
        },
        {
            "name": "clarification_and_restraint",
            "label": "Clarification and restraint",
            "summary": "Ambiguous evidence, stale memory, and low-value nudges should clarify, defer, bundle, or stay silent with explicit reasons.",
        },
        {
            "name": "trust_and_approval",
            "label": "Trust and approval",
            "summary": "Risky capability use must surface approval requirements before crossing privileged boundaries.",
        },
        {
            "name": "operator_correctability",
            "label": "Operator correctability",
            "summary": "Every decision receipt should expose why the action was chosen and how the operator can correct action or capability selection.",
        },
        {
            "name": "quality_scores",
            "label": "Quality scores",
            "summary": "Reports should expose timing, usefulness, false-positive, false-negative, trust-preservation, and recovery judgments.",
        },
    ]


def m8_guardian_brain_failure_taxonomy() -> list[dict[str, str]]:
    return [
        {
            "name": "overeager_action",
            "severity": "high",
            "summary": "The guardian acts when evidence is ambiguous, stale, low-value, or contradicted by interruption cost.",
        },
        {
            "name": "missing_approval_gate",
            "severity": "high",
            "summary": "A high-risk capability is treated as direct action instead of an approval-gated proposal.",
        },
        {
            "name": "hidden_capability_choice",
            "severity": "medium",
            "summary": "The selected and rejected capability lanes are not visible to the operator.",
        },
        {
            "name": "no_action_without_receipt",
            "severity": "medium",
            "summary": "The guardian stays silent or defers without a traceable reason and correction hook.",
        },
        {
            "name": "benchmark_theater",
            "severity": "medium",
            "summary": "The benchmark report does not exercise the same deterministic decision contract exposed through operator surfaces.",
        },
    ]


def m8_guardian_brain_benchmark_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suite": M8_GUARDIAN_BRAIN_BENCHMARK_SUITE_NAME,
        "milestone_contract": "m8_guardian_brain_and_intervention_quality_ship_as_one_ready_pr",
        "decision_policy": "memory_project_commitments_channel_risk_cost_and_preferences_shape_action_or_restraint",
        "action_policy": "act_defer_bundle_clarify_request_approval_or_stay_silent",
        "approval_policy": "high_risk_capability_use_requires_operator_approval_receipt",
        "operator_visibility": "decision_capability_reason_scores_and_correction_hooks_visible",
        "receipt_surfaces": [
            "/api/operator/m8-guardian-brain",
            "/api/operator/m8-guardian-intervention-benchmark",
            "/api/operator/benchmark-proof",
            "/api/operator/guardian-state",
        ],
        "claim_boundary": "deterministic_guardian_judgment_receipts_not_live_superiority_claim",
        "ci_gate_mode": "required_benchmark_suite",
    }


def _m8_guardian_brain_failure_report(summary: Any) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append(
            {
                "type": "benchmark_regression",
                "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
                "summary": str(getattr(result, "error", "") or "M8 guardian intervention benchmark scenario failed."),
                "reason": "deterministic_eval_failure",
            }
        )
    return failures[:8]


async def _run_m8_guardian_brain_benchmark_suite():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([M8_GUARDIAN_BRAIN_BENCHMARK_SUITE_NAME])


async def build_m8_guardian_brain_benchmark_report() -> dict[str, Any]:
    summary = await _run_m8_guardian_brain_benchmark_suite()
    receipts = build_m8_guardian_brain_receipts()
    failure_report = _m8_guardian_brain_failure_report(summary)
    benchmark_posture = (
        "m8_ci_gated_operator_visible"
        if summary.failed == 0
        else "m8_ci_regressions_detected_operator_visible"
    )
    actions = {str(receipt.get("action")) for receipt in receipts}
    return {
        "summary": {
            "suite_name": M8_GUARDIAN_BRAIN_BENCHMARK_SUITE_NAME,
            "benchmark_posture": benchmark_posture,
            "operator_status": "m8_guardian_brain_receipts_visible",
            "scenario_count": len(M8_GUARDIAN_BRAIN_BENCHMARK_SCENARIO_NAMES),
            "dimension_count": len(m8_guardian_brain_benchmark_dimensions()),
            "failure_mode_count": len(m8_guardian_brain_failure_taxonomy()),
            "active_failure_count": summary.failed,
            "decision_surface_state": "act_defer_bundle_clarify_approval_and_silence_receipts_visible",
            "capability_choice_state": "selected_and_rejected_capability_lanes_visible",
            "restraint_state": "stale_ambiguous_conflicting_and_low_value_cases_do_not_silently_act",
            "quality_score_state": "timing_usefulness_false_positive_false_negative_trust_and_recovery_visible",
            "action_count": len(actions),
        },
        "scenario_names": list(M8_GUARDIAN_BRAIN_BENCHMARK_SCENARIO_NAMES),
        "dimensions": m8_guardian_brain_benchmark_dimensions(),
        "failure_taxonomy": m8_guardian_brain_failure_taxonomy(),
        "decision_receipts": receipts,
        "failure_report": failure_report,
        "policy": m8_guardian_brain_benchmark_policy_payload(),
        "latest_run": {
            "total": summary.total,
            "passed": summary.passed,
            "failed": summary.failed,
            "duration_ms": summary.duration_ms,
        },
    }
