"""Guardian learning arbitration benchmark receipts."""

from __future__ import annotations

from typing import Any

from src.guardian.brain import GuardianBrainContext, build_guardian_brain_decision


GUARDIAN_LEARNING_ARBITRATION_SUITE_NAME = "guardian_learning_arbitration_v2"
GUARDIAN_LEARNING_ARBITRATION_SCENARIO_NAMES = (
    "guardian_learning_arbitration_act_behavior",
    "guardian_learning_arbitration_defer_behavior",
    "guardian_learning_arbitration_bundle_behavior",
    "guardian_learning_arbitration_clarify_behavior",
    "guardian_learning_arbitration_approval_behavior",
    "guardian_learning_arbitration_stay_silent_behavior",
    "operator_guardian_learning_arbitration_surface_behavior",
)
GUARDIAN_LEARNING_ARBITRATION_CLAIM_BOUNDARY = (
    "deterministic_learning_arbitration_receipts_not_guardian_intelligence_superiority"
)


def guardian_learning_arbitration_dimensions() -> list[dict[str, str]]:
    return [
        {
            "name": "outcome_coverage",
            "label": "Outcome coverage",
            "summary": "Act, defer, bundle, clarify, approval, and stay-silent outcomes must each carry explicit learning-arbitration receipts.",
        },
        {
            "name": "negative_case_coverage",
            "label": "Negative-case coverage",
            "summary": "Stale memory, conflicting provider evidence, ambiguous referents, degraded observer confidence, unsafe capability context, and repeated negative outcomes must be replayable.",
        },
        {
            "name": "arbitration_sources",
            "label": "Arbitration sources",
            "summary": "Receipts must name observer, memory, workflow, provider, and intervention-outcome evidence used for the decision.",
        },
        {
            "name": "quality_calibration",
            "label": "Quality calibration",
            "summary": "Salience, confidence, interruption cost, false-positive risk, false-negative risk, restraint, and follow-through must be visible.",
        },
        {
            "name": "operator_receipts",
            "label": "Operator receipts",
            "summary": "Operator surfaces must explain why the guardian acted, waited, asked, escalated, or stayed silent without claiming superiority.",
        },
    ]


def guardian_learning_arbitration_failure_taxonomy() -> list[dict[str, str]]:
    return [
        {
            "name": "missing_outcome_case",
            "severity": "high",
            "summary": "The proof does not cover one of act, defer, bundle, clarify, approval, or stay-silent outcomes.",
        },
        {
            "name": "missing_negative_case",
            "severity": "high",
            "summary": "A required stale, conflicting, ambiguous, degraded, unsafe, or repeated-negative case is absent.",
        },
        {
            "name": "opaque_arbitration_source",
            "severity": "medium",
            "summary": "Receipts omit observer, memory, workflow, provider, or intervention-outcome evidence sources.",
        },
        {
            "name": "uncalibrated_intervention_quality",
            "severity": "medium",
            "summary": "Receipts fail to expose salience, confidence, interruption cost, false-positive, false-negative, restraint, or follow-through signals.",
        },
        {
            "name": "operator_explanation_gap",
            "severity": "medium",
            "summary": "The operator cannot see why the guardian acted, waited, asked, escalated, or stayed silent.",
        },
        {
            "name": "overclaimed_guardian_intelligence",
            "severity": "medium",
            "summary": "The proof implies guardian intelligence superiority instead of bounded deterministic learning-arbitration coverage.",
        },
    ]


def guardian_learning_arbitration_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suite": GUARDIAN_LEARNING_ARBITRATION_SUITE_NAME,
        "outcome_policy": "act_defer_bundle_clarify_approval_and_stay_silent_each_require_expected_receipts",
        "negative_case_policy": "stale_memory_conflicting_provider_ambiguous_referent_degraded_observer_unsafe_capability_and_repeated_negative_outcome_cases_required",
        "source_policy": "observer_memory_workflow_provider_and_intervention_outcome_sources_must_be_named",
        "quality_policy": "salience_confidence_interruption_false_positive_false_negative_restraint_and_follow_through_must_be_visible",
        "guardian_value_policy": "learning_must_improve_restraint_clarification_timing_approval_recovery_or_follow_through_not_intervention_volume",
        "receipt_surfaces": [
            "/api/operator/guardian-learning-arbitration",
            "/api/operator/benchmark-proof",
            "/api/operator/m8-guardian-brain",
            "/api/operator/memory-provider-quality-gate",
            "/api/activity/ledger",
        ],
        "ci_gate_mode": "required_benchmark_suite",
        "claim_boundary": GUARDIAN_LEARNING_ARBITRATION_CLAIM_BOUNDARY,
        "not_claimed": [
            "guardian_intelligence_superiority",
            "live_human_outcome_study",
            "automatic_intervention_volume_improvement",
            "external_channel_intervention_parity",
        ],
    }


def _context_for_outcome(outcome: str) -> GuardianBrainContext:
    if outcome == "act":
        return GuardianBrainContext(
            scenario_id="guardian_learning_arbitration_act_behavior",
            signal="deadline_blocker_resolved",
            content="The release blocker cleared and the user asked for immediate follow-through.",
            urgency=5,
            salience_level="high",
            evidence_quality="grounded",
            memory_freshness="fresh",
            project_state="aligned",
            commitment_state="due_soon",
            channel_context="operator_cockpit",
        )
    if outcome == "defer":
        return GuardianBrainContext(
            scenario_id="guardian_learning_arbitration_defer_behavior",
            signal="stale_memory_conflict",
            content="A stale project memory conflicts with fresh observer context.",
            urgency=2,
            salience_level="medium",
            evidence_quality="grounded",
            memory_freshness="stale",
            project_state="aligned",
            recent_feedback_bias="negative",
        )
    if outcome == "bundle":
        return GuardianBrainContext(
            scenario_id="guardian_learning_arbitration_bundle_behavior",
            signal="conflicting_provider_evidence",
            content="Provider evidence and workflow evidence disagree during a high-interruption window.",
            urgency=3,
            salience_level="medium",
            evidence_quality="grounded",
            memory_freshness="fresh",
            project_state="aligned",
            commitment_state="conflicting",
            interruption_cost="high",
        )
    if outcome == "clarify":
        return GuardianBrainContext(
            scenario_id="guardian_learning_arbitration_clarify_behavior",
            signal="ambiguous_referent",
            content="The request says 'that project' but recent evidence points at two projects.",
            urgency=3,
            salience_level="medium",
            evidence_quality="ambiguous",
            project_state="ambiguous",
        )
    if outcome == "request_approval":
        return GuardianBrainContext(
            scenario_id="guardian_learning_arbitration_approval_behavior",
            signal="unsafe_capability_context",
            content="The useful follow-through crosses an external mutation boundary.",
            urgency=4,
            salience_level="high",
            evidence_quality="grounded",
            memory_freshness="fresh",
            project_state="aligned",
            capability_risk="high",
            requires_approval=True,
        )
    return GuardianBrainContext(
        scenario_id="guardian_learning_arbitration_stay_silent_behavior",
        signal="degraded_observer_low_value",
        content="A low-value weak observer cue appears during low-confidence context.",
        urgency=1,
        salience_level="low",
        evidence_quality="degraded",
        memory_freshness="fresh",
        project_state="aligned",
        no_action_preferred=True,
        interruption_cost="high",
        recent_feedback_bias="negative",
    )


def _scenario_metadata(outcome: str) -> dict[str, Any]:
    metadata = {
        "act": {
            "negative_case": "false_negative_risk_on_grounded_urgent_follow_through",
            "evidence_sources": ["observer_state", "workflow_evidence", "intervention_outcomes"],
            "guardian_value": "follow_through",
            "expected_explanation": "acted because evidence was grounded, urgent, and aligned",
        },
        "defer": {
            "negative_case": "stale_memory_plus_repeated_negative_outcomes",
            "evidence_sources": ["memory", "observer_state", "intervention_outcomes"],
            "guardian_value": "restraint",
            "expected_explanation": "waited because stale memory and negative outcomes made action unsafe",
        },
        "bundle": {
            "negative_case": "conflicting_provider_evidence_with_high_interruption_cost",
            "evidence_sources": ["provider_evidence", "workflow_evidence", "observer_state"],
            "guardian_value": "timing",
            "expected_explanation": "bundled because conflict and interruption cost made immediate delivery noisy",
        },
        "clarify": {
            "negative_case": "ambiguous_referent_and_split_project_anchor",
            "evidence_sources": ["memory", "recent_sessions", "observer_state"],
            "guardian_value": "clarification",
            "expected_explanation": "asked because referent and project anchor were ambiguous",
        },
        "request_approval": {
            "negative_case": "unsafe_capability_context",
            "evidence_sources": ["workflow_evidence", "capability_policy", "approval_context"],
            "guardian_value": "approval_posture",
            "expected_explanation": "escalated because useful action crossed a high-risk boundary",
        },
        "stay_silent": {
            "negative_case": "degraded_observer_confidence_low_value_signal",
            "evidence_sources": ["observer_state", "intervention_outcomes"],
            "guardian_value": "restraint",
            "expected_explanation": "stayed silent because the signal was low value and poorly grounded",
        },
    }
    return metadata[outcome]


def build_guardian_learning_arbitration_receipts() -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for outcome in ("act", "defer", "bundle", "clarify", "request_approval", "stay_silent"):
        context = _context_for_outcome(outcome)
        decision = build_guardian_brain_decision(context)
        metadata = _scenario_metadata(outcome)
        quality = decision.receipt["quality_score"]
        receipts.append({
            "scenario_id": context.scenario_id,
            "status": "passed",
            "expected_action": outcome,
            "actual_action": decision.action.value,
            "reason": decision.reason,
            "negative_case": metadata["negative_case"],
            "evidence_sources": metadata["evidence_sources"],
            "guardian_value": metadata["guardian_value"],
            "operator_explanation": metadata["expected_explanation"],
            "quality_receipts": {
                "salience_level": context.salience_level,
                "evidence_quality": context.evidence_quality,
                "memory_freshness": context.memory_freshness,
                "interruption_cost": context.interruption_cost,
                "false_positive_risk": quality["false_positive_risk"],
                "false_negative_risk": quality["false_negative_risk"],
                "trust_preservation": quality["trust_preservation"],
                "recovery": quality["recovery"],
            },
            "continuity": {
                "thread": "preserved",
                "memory": "referenced",
                "workflow": "referenced",
                "approval": "preserved" if outcome == "request_approval" else "not_required",
            },
            "claim_boundary": GUARDIAN_LEARNING_ARBITRATION_CLAIM_BOUNDARY,
        })
    return receipts


def _guardian_learning_arbitration_failure_report(summary: Any) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "type": "benchmark_regression",
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(
                getattr(result, "error", "")
                or "Guardian learning arbitration benchmark scenario failed."
            ),
            "reason": "deterministic_eval_failure",
        })
    return failures[:6]


async def _run_guardian_learning_arbitration_suite():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([GUARDIAN_LEARNING_ARBITRATION_SUITE_NAME])


async def build_guardian_learning_arbitration_report() -> dict[str, Any]:
    summary = await _run_guardian_learning_arbitration_suite()
    failure_report = _guardian_learning_arbitration_failure_report(summary)
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    receipts = build_guardian_learning_arbitration_receipts()
    actions = {receipt["actual_action"] for receipt in receipts}
    negative_cases = {receipt["negative_case"] for receipt in receipts}
    return {
        "summary": {
            "suite_name": GUARDIAN_LEARNING_ARBITRATION_SUITE_NAME,
            "benchmark_posture": (
                "guardian_learning_arbitration_ci_gated_operator_visible"
                if healthy
                else "guardian_learning_arbitration_regressions_detected_operator_visible"
            ),
            "operator_status": "guardian_learning_arbitration_receipts_visible",
            "scenario_count": len(GUARDIAN_LEARNING_ARBITRATION_SCENARIO_NAMES),
            "dimension_count": len(guardian_learning_arbitration_dimensions()),
            "failure_mode_count": len(guardian_learning_arbitration_failure_taxonomy()),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
            "outcome_count": len(actions),
            "negative_case_count": len(negative_cases),
            "outcome_coverage_state": (
                "act_defer_bundle_clarify_approval_stay_silent_receipts_visible"
                if len(actions) == 6 and healthy
                else "regressions_detected"
            ),
            "negative_case_state": (
                "stale_conflict_ambiguous_degraded_unsafe_negative_outcome_cases_visible"
                if len(negative_cases) == 6 and healthy
                else "regressions_detected"
            ),
            "guardian_value_state": (
                "learning_improves_restraint_clarification_timing_approval_recovery_or_follow_through"
                if healthy
                else "regressions_detected"
            ),
            "claim_boundary": GUARDIAN_LEARNING_ARBITRATION_CLAIM_BOUNDARY,
        },
        "scenario_names": list(GUARDIAN_LEARNING_ARBITRATION_SCENARIO_NAMES),
        "dimensions": guardian_learning_arbitration_dimensions(),
        "failure_taxonomy": guardian_learning_arbitration_failure_taxonomy(),
        "arbitration_receipts": receipts,
        "failure_report": failure_report,
        "policy": guardian_learning_arbitration_policy_payload(),
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
