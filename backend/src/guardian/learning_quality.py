from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


GUARDIAN_LEARNING_QUALITY_SUITE_NAME = "guardian_world_model_learning_quality_v2"
GUARDIAN_LEARNING_QUALITY_SCENARIO_NAMES = (
    "guardian_learning_multi_signal_arbitration_behavior",
    "guardian_learning_stale_conflict_suppression_behavior",
    "guardian_learning_salience_confidence_calibration_behavior",
    "guardian_learning_false_positive_negative_accounting_behavior",
    "guardian_learning_live_replay_receipts_behavior",
    "operator_guardian_learning_quality_surface_behavior",
)
GUARDIAN_LEARNING_QUALITY_CLAIM_BOUNDARY = (
    "deterministic_fixture_learning_quality_receipts_not_live_human_outcome_or_adaptive_engine_claim"
)


@dataclass(frozen=True)
class GuardianLearningReplayCase:
    scenario_name: str
    source_mix: tuple[str, ...]
    live_signal: str
    durable_signal: str
    selected_action: str
    selected_bias: str
    stale_evidence_policy: str
    conflict_policy: str
    salience_level: str
    confidence_level: str
    false_positive_label: str
    false_negative_label: str
    operator_receipt: str
    evidence_ids: tuple[str, ...]

    def to_receipt(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["claim_boundary"] = GUARDIAN_LEARNING_QUALITY_CLAIM_BOUNDARY
        payload["quality_gate"] = {
            "multi_signal_required": len(self.source_mix) >= 2,
            "evidence_ids_present": bool(self.evidence_ids),
            "outcome_labels_visible": self.false_positive_label != "missing"
            and self.false_negative_label != "missing",
            "operator_receipt_visible": bool(self.operator_receipt),
        }
        return payload


def guardian_learning_quality_dimensions() -> list[dict[str, str]]:
    return [
        {
            "name": "multi_signal_arbitration",
            "label": "Multi-signal arbitration",
            "summary": "Learning quality should compare live observer/intervention signals, procedural memory, and recent execution state before selecting a bias.",
        },
        {
            "name": "stale_conflict_suppression",
            "label": "Stale and conflicting evidence suppression",
            "summary": "Stale or conflicting evidence should degrade confidence, defer, clarify, or suppress rather than silently personalizing action.",
        },
        {
            "name": "salience_confidence_calibration",
            "label": "Salience and confidence calibration",
            "summary": "High salience should not override weak evidence unless confidence, urgency, and trust posture are visible.",
        },
        {
            "name": "outcome_accounting",
            "label": "False-positive and false-negative accounting",
            "summary": "Intervention receipts should label both over-interruption risk and missed-useful-action risk.",
        },
        {
            "name": "operator_replay_receipts",
            "label": "Operator replay receipts",
            "summary": "Operators should inspect why learning selected, suppressed, or deferred a signal without reading benchmark code.",
        },
    ]


def guardian_learning_quality_failure_taxonomy() -> list[dict[str, str]]:
    return [
        {
            "name": "stale_signal_overrides_fresh_context",
            "severity": "high",
            "summary": "Durable or provider memory overrides fresh live context even though it is stale or weakly grounded.",
        },
        {
            "name": "conflict_hidden_from_operator",
            "severity": "high",
            "summary": "Live and durable learning disagree, but the operator cannot see the arbitration reason.",
        },
        {
            "name": "false_positive_unlabeled",
            "severity": "medium",
            "summary": "The guardian acts without labeling the risk of interrupting when action might be wrong.",
        },
        {
            "name": "false_negative_unlabeled",
            "severity": "medium",
            "summary": "The guardian defers without labeling the risk of missing a useful intervention.",
        },
        {
            "name": "benchmark_without_replay_receipt",
            "severity": "medium",
            "summary": "Learning quality is tested but not exposed through an operator-readable receipt surface.",
        },
    ]


def guardian_learning_quality_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suite": GUARDIAN_LEARNING_QUALITY_SUITE_NAME,
        "arbitration_policy": "compare_live_observer_feedback_procedural_memory_execution_and_reach_receipts",
        "stale_evidence_policy": "stale_or_conflicting_evidence_degrades_confidence_before_action",
        "outcome_accounting_policy": "false_positive_false_negative_usefulness_timing_and_trust_labels_required",
        "operator_visibility": "learning_replay_receipts_arbitration_reasons_and_claim_boundaries_visible",
        "receipt_surfaces": [
            "/api/operator/guardian-learning-quality",
            "/api/operator/benchmark-proof",
            "/api/operator/m8-guardian-brain",
            "/api/operator/guardian-state",
        ],
        "claim_boundary": GUARDIAN_LEARNING_QUALITY_CLAIM_BOUNDARY,
        "ci_gate_mode": "required_benchmark_suite",
    }


def build_guardian_learning_quality_replay_cases() -> tuple[GuardianLearningReplayCase, ...]:
    return (
        GuardianLearningReplayCase(
            scenario_name="guardian_learning_multi_signal_arbitration_behavior",
            source_mix=("live_feedback", "procedural_memory", "observer_context", "workflow_receipt"),
            live_signal="available_window_supports_direct_follow_through",
            durable_signal="recent_thread_prefers_existing_context",
            selected_action="act",
            selected_bias="direct_existing_thread_follow_through",
            stale_evidence_policy="not_applicable_fresh_sources_align",
            conflict_policy="aligned_multi_source_support",
            salience_level="high",
            confidence_level="grounded",
            false_positive_label="managed_by_grounded_sources",
            false_negative_label="managed_by_high_salience",
            operator_receipt="multi_signal_arbitration_receipt",
            evidence_ids=("live:advisory:available:2", "procedural:thread:existing", "workflow:repair:fresh"),
        ),
        GuardianLearningReplayCase(
            scenario_name="guardian_learning_stale_conflict_suppression_behavior",
            source_mix=("live_feedback", "procedural_memory", "memory_provider"),
            live_signal="negative_recent_outcomes_for_interruptions",
            durable_signal="stale_provider_suggests_direct_nudge",
            selected_action="defer",
            selected_bias="suppress_stale_direct_nudge",
            stale_evidence_policy="stale_provider_evidence_suppressed",
            conflict_policy="fresh_negative_live_outcome_overrides_stale_direct_guidance",
            salience_level="medium",
            confidence_level="partial",
            false_positive_label="high_if_direct_nudge_sent",
            false_negative_label="low_because_urgency_medium",
            operator_receipt="stale_conflict_suppression_receipt",
            evidence_ids=("live:advisory:not_helpful:3", "provider:direct_nudge:stale", "memory:project_anchor:fresh"),
        ),
        GuardianLearningReplayCase(
            scenario_name="guardian_learning_salience_confidence_calibration_behavior",
            source_mix=("observer_context", "guardian_world_model", "recent_execution"),
            live_signal="blocked_work_salience_high",
            durable_signal="world_model_confidence_partial",
            selected_action="clarify",
            selected_bias="high_salience_but_partial_confidence_clarify_first",
            stale_evidence_policy="no_stale_override",
            conflict_policy="salience_does_not_force_action_without_grounded_confidence",
            salience_level="high",
            confidence_level="partial",
            false_positive_label="low_after_clarification",
            false_negative_label="managed_by_high_salience_receipt",
            operator_receipt="salience_confidence_calibration_receipt",
            evidence_ids=("observer:salience:high", "world_model:confidence:partial", "execution:blocker:fresh"),
        ),
        GuardianLearningReplayCase(
            scenario_name="guardian_learning_false_positive_negative_accounting_behavior",
            source_mix=("m8_guardian_brain", "intervention_feedback", "reach_receipt"),
            live_signal="native_reach_available_but_user_cost_high",
            durable_signal="prior_low_urgency_bundle_was_helpful",
            selected_action="bundle",
            selected_bias="bundle_low_urgency_high_cost",
            stale_evidence_policy="bundle_bias_recent_enough",
            conflict_policy="no_conflict_cost_dominates",
            salience_level="low",
            confidence_level="grounded",
            false_positive_label="low_because_no_immediate_interrupt",
            false_negative_label="managed_by_bundle_follow_up",
            operator_receipt="false_positive_negative_accounting_receipt",
            evidence_ids=("m8:stay_silent_or_bundle", "feedback:bundle_helpful", "reach:native:paired"),
        ),
        GuardianLearningReplayCase(
            scenario_name="guardian_learning_live_replay_receipts_behavior",
            source_mix=("fake_live_provider", "operator_replay", "benchmark_receipt"),
            live_signal="time_anchored_fake_feedback_replay",
            durable_signal="fixed_fixture_expected_policy",
            selected_action="defer",
            selected_bias="replay_only_no_live_claim",
            stale_evidence_policy="fixture_time_anchor_required",
            conflict_policy="claim_boundary_blocks_live_superiority_claim",
            salience_level="medium",
            confidence_level="grounded",
            false_positive_label="visible",
            false_negative_label="visible",
            operator_receipt="fixture_replay_receipt",
            evidence_ids=("fixture:2026-05-11:advisory", "operator:receipt:learning-quality"),
        ),
        GuardianLearningReplayCase(
            scenario_name="operator_guardian_learning_quality_surface_behavior",
            source_mix=("operator_surface", "benchmark_proof", "guardian_state"),
            live_signal="operator_can_inspect_learning_quality",
            durable_signal="benchmark_policy_links_claim_boundary",
            selected_action="inspect",
            selected_bias="operator_visible_learning_receipts",
            stale_evidence_policy="operator_receipt_marks_stale_conflict_state",
            conflict_policy="operator_receipt_marks_arbitration_reason",
            salience_level="n/a",
            confidence_level="operator_visible",
            false_positive_label="operator_visible",
            false_negative_label="operator_visible",
            operator_receipt="operator_guardian_learning_quality_surface",
            evidence_ids=("api:/operator/guardian-learning-quality", "api:/operator/benchmark-proof"),
        ),
    )


def build_guardian_learning_quality_replay() -> dict[str, Any]:
    receipts = [case.to_receipt() for case in build_guardian_learning_quality_replay_cases()]
    actions = sorted({receipt["selected_action"] for receipt in receipts})
    confidence_levels = sorted({receipt["confidence_level"] for receipt in receipts})
    return {
        "summary": {
            "suite_name": GUARDIAN_LEARNING_QUALITY_SUITE_NAME,
            "replay_posture": "deterministic_fixture_learning_receipt_catalog",
            "scenario_count": len(GUARDIAN_LEARNING_QUALITY_SCENARIO_NAMES),
            "receipt_count": len(receipts),
            "dimension_count": len(guardian_learning_quality_dimensions()),
            "failure_mode_count": len(guardian_learning_quality_failure_taxonomy()),
            "action_count": len(actions),
            "confidence_levels": confidence_levels,
            "operator_status": "guardian_learning_quality_receipts_visible",
            "quality_state": "multi_signal_stale_conflict_salience_confidence_and_outcome_labels_visible",
        },
        "scenario_names": list(GUARDIAN_LEARNING_QUALITY_SCENARIO_NAMES),
        "dimensions": guardian_learning_quality_dimensions(),
        "failure_taxonomy": guardian_learning_quality_failure_taxonomy(),
        "receipts": receipts,
        "policy": guardian_learning_quality_policy_payload(),
    }


def _guardian_learning_quality_failure_report(summary: Any) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append(
            {
                "type": "benchmark_regression",
                "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
                "summary": str(getattr(result, "error", "") or "Guardian learning quality scenario failed."),
                "reason": "deterministic_eval_failure",
            }
        )
    return failures[:8]


async def _run_guardian_learning_quality_benchmark_suite():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([GUARDIAN_LEARNING_QUALITY_SUITE_NAME])


async def build_guardian_learning_quality_report() -> dict[str, Any]:
    summary = await _run_guardian_learning_quality_benchmark_suite()
    replay = build_guardian_learning_quality_replay()
    failure_report = _guardian_learning_quality_failure_report(summary)
    benchmark_posture = (
        "guardian_learning_quality_ci_gated_operator_visible"
        if summary.failed == 0
        else "guardian_learning_quality_regressions_detected_operator_visible"
    )
    return {
        **replay,
        "summary": {
            **replay["summary"],
            "benchmark_posture": benchmark_posture,
            "active_failure_count": summary.failed,
        },
        "failure_report": failure_report,
        "latest_run": {
            "total": summary.total,
            "passed": summary.passed,
            "failed": summary.failed,
            "duration_ms": summary.duration_ms,
        },
    }
