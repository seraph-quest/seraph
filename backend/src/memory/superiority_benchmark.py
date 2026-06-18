from __future__ import annotations

from typing import Any


M6_MEMORY_SUPERIORITY_BENCHMARK_SUITE_NAME = "m6_memory_superiority"
M6_MEMORY_SUPERIORITY_BENCHMARK_SCENARIO_NAMES = (
    "m6_long_horizon_recall_behavior",
    "m6_contradiction_handling_behavior",
    "m6_stale_memory_override_behavior",
    "m6_source_trust_privacy_boundary_behavior",
    "m6_provider_quality_behavior",
    "m6_behavior_change_receipts_behavior",
    "operator_m6_memory_superiority_benchmark_surface_behavior",
)


def m6_memory_superiority_benchmark_dimensions() -> list[dict[str, str]]:
    return [
        {
            "name": "long_horizon_recall",
            "label": "Long-horizon recall",
            "summary": "Seraph should recover weeks-old workflow, approval, artifact, audit, and session receipts by shared work reference instead of relying on the latest chat turn.",
        },
        {
            "name": "contradiction_handling",
            "label": "Contradiction handling",
            "summary": "Current guardian truth should outrank lower-ranked contradictory memories and expose the suppression receipt.",
        },
        {
            "name": "stale_memory_override",
            "label": "Stale-memory override",
            "summary": "Fresh canonical or provider-backed project evidence should survive while stale provider evidence is suppressed with diagnostics.",
        },
        {
            "name": "source_trust_privacy_boundary",
            "label": "Source trust and privacy boundary",
            "summary": "External memory providers should stay advisory, preserve guardian authority, and keep configured secrets out of operator-visible receipts.",
        },
        {
            "name": "provider_quality",
            "label": "Provider quality",
            "summary": "Provider augmentation should report usefulness, freshness, topic match, authority, and degradation state rather than silently injecting context.",
        },
        {
            "name": "behavior_change_receipts",
            "label": "Behavior-change receipts",
            "summary": "Feedback-derived procedural memory should alter same-session guardian behavior only through explicit learned-rule and diagnostic receipts.",
        },
    ]


def m6_memory_superiority_failure_taxonomy() -> list[dict[str, str]]:
    return [
        {
            "name": "long_horizon_recall_gap",
            "severity": "high",
            "summary": "Relevant older workflow, approval, artifact, audit, or session evidence is missed for an active work reference.",
        },
        {
            "name": "contradiction_leak",
            "severity": "high",
            "summary": "Stale contradictory memory survives next to fresher guardian truth without a suppression receipt.",
        },
        {
            "name": "stale_override_failure",
            "severity": "high",
            "summary": "Old provider or stale structured evidence overrides fresher canonical state.",
        },
        {
            "name": "provider_authority_or_privacy_drift",
            "severity": "high",
            "summary": "External provider evidence becomes authoritative, or provider configuration leaks secret material into operator receipts.",
        },
        {
            "name": "hidden_provider_quality",
            "severity": "medium",
            "summary": "Provider context changes behavior without usefulness, freshness, authority, topic-match, or degradation diagnostics.",
        },
        {
            "name": "unreceipted_behavior_change",
            "severity": "medium",
            "summary": "Learned procedural memory changes delivery or action posture without explicit behavior-change receipts.",
        },
    ]


def m6_memory_superiority_benchmark_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suite": M6_MEMORY_SUPERIORITY_BENCHMARK_SUITE_NAME,
        "milestone_contract": "m6_memory_superiority_ships_as_one_ready_pr",
        "canonical_authority": "guardian_memory_and_world_model",
        "provider_authority": "external_advisory_only",
        "privacy_policy": "provider_config_and_secret_values_never_surface_in_operator_receipts",
        "stale_override_policy": "fresh_canonical_or_focused_provider_evidence_overrides_stale_memory",
        "contradiction_policy": "current_ranked_truth_suppresses_lower_ranked_contradictions",
        "behavior_change_policy": "feedback_changes_behavior_only_with_procedural_memory_receipts",
        "quality_receipt_policy": "provider_usefulness_freshness_authority_topic_and_degradation_must_be_visible",
        "receipt_surfaces": [
            "/api/operator/benchmark-proof",
            "/api/operator/m6-memory-superiority-benchmark",
            "/api/operator/memory-benchmark",
            "/api/operator/guardian-state",
        ],
        "ci_gate_mode": "required_benchmark_suite",
    }


def _m6_memory_superiority_failure_report(summary: Any) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append(
            {
                "type": "benchmark_regression",
                "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
                "summary": str(getattr(result, "error", "") or "M6 memory superiority benchmark scenario failed."),
                "reason": "deterministic_eval_failure",
            }
        )
    return failures[:6]


async def _run_m6_memory_superiority_benchmark_suite():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([M6_MEMORY_SUPERIORITY_BENCHMARK_SUITE_NAME])


async def build_m6_memory_superiority_benchmark_report() -> dict[str, Any]:
    summary = await _run_m6_memory_superiority_benchmark_suite()
    failure_report = _m6_memory_superiority_failure_report(summary)
    benchmark_posture = (
        "m6_ci_gated_operator_visible"
        if summary.failed == 0
        else "m6_regressions_detected_operator_visible"
    )
    return {
        "summary": {
            "suite_name": M6_MEMORY_SUPERIORITY_BENCHMARK_SUITE_NAME,
            "benchmark_posture": benchmark_posture,
            "operator_status": "m6_memory_superiority_receipts_visible",
            "scenario_count": len(M6_MEMORY_SUPERIORITY_BENCHMARK_SCENARIO_NAMES),
            "dimension_count": len(m6_memory_superiority_benchmark_dimensions()),
            "failure_mode_count": len(m6_memory_superiority_failure_taxonomy()),
            "active_failure_count": summary.failed,
            "long_horizon_recall_state": "workflow_approval_artifact_audit_session_receipts_ranked",
            "contradiction_state": "lower_ranked_contradictions_suppressed",
            "stale_override_state": "fresh_canonical_or_focused_provider_evidence_wins",
            "source_trust_privacy_state": "guardian_authority_external_advisory_no_secret_receipts",
            "provider_quality_state": "usefulness_and_degradation_receipts_visible",
            "behavior_change_receipt_state": "procedural_memory_receipts_required",
        },
        "scenario_names": list(M6_MEMORY_SUPERIORITY_BENCHMARK_SCENARIO_NAMES),
        "dimensions": m6_memory_superiority_benchmark_dimensions(),
        "failure_taxonomy": m6_memory_superiority_failure_taxonomy(),
        "failure_report": failure_report,
        "policy": m6_memory_superiority_benchmark_policy_payload(),
        "latest_run": {
            "total": summary.total,
            "passed": summary.passed,
            "failed": summary.failed,
            "duration_ms": summary.duration_ms,
        },
    }
