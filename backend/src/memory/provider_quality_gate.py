from __future__ import annotations

from typing import Any

from src.memory.providers import memory_provider_quality_gate_policy_payload


MEMORY_PROVIDER_QUALITY_GATE_SUITE_NAME = "memory_provider_quality_gate"
MEMORY_PROVIDER_QUALITY_GATE_SCENARIO_NAMES = (
    "memory_provider_quality_gate_contract_behavior",
    "memory_provider_quality_gate_improvement_behavior",
    "memory_provider_quality_gate_suppression_behavior",
    "operator_memory_provider_quality_gate_surface_behavior",
)


def memory_provider_quality_gate_dimensions() -> list[dict[str, str]]:
    return [
        {
            "name": "provider_declarations",
            "label": "Provider declarations",
            "summary": "Provider evidence must declare provenance, confidence, privacy boundary, freshness, conflict behavior, and suppression rules before it can enter guardian context.",
        },
        {
            "name": "quality_improvement",
            "label": "Quality improvement",
            "summary": "High-confidence, topic-relevant provider evidence can augment recall only after the quality gate passes.",
        },
        {
            "name": "noisy_stale_conflict_suppression",
            "label": "Noisy, stale, and conflict suppression",
            "summary": "Low-confidence, stale, unsafe, or authority-drifting provider evidence is suppressed before guardian context assembly.",
        },
        {
            "name": "operator_controls",
            "label": "Operator controls",
            "summary": "Operators can inspect provider posture and use memory correction, pin, forget, and audit controls without granting providers canonical authority.",
        },
    ]


def memory_provider_quality_gate_failure_taxonomy() -> list[dict[str, str]]:
    return [
        {
            "name": "missing_provider_declaration",
            "severity": "high",
            "summary": "Provider evidence enters context without provenance, confidence, privacy, freshness, conflict, or suppression metadata.",
        },
        {
            "name": "noisy_provider_evidence_in_context",
            "severity": "high",
            "summary": "Low-confidence or low-score provider evidence reaches guardian context instead of being suppressed.",
        },
        {
            "name": "provider_authority_drift",
            "severity": "high",
            "summary": "External provider evidence claims canonical authority or overrides guardian-owned memory.",
        },
        {
            "name": "unsafe_provider_privacy_boundary",
            "severity": "high",
            "summary": "Secret or credential-scoped provider evidence reaches guardian context or operator receipts.",
        },
        {
            "name": "operator_control_gap",
            "severity": "medium",
            "summary": "Operators cannot inspect, correct, pin, forget, or audit provider-derived memory posture.",
        },
    ]


def _memory_provider_quality_gate_failure_report(summary: Any) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append(
            {
                "type": "benchmark_regression",
                "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
                "summary": str(getattr(result, "error", "") or "Memory provider quality-gate scenario failed."),
                "reason": "deterministic_eval_failure",
            }
        )
    return failures[:6]


async def _run_memory_provider_quality_gate_suite():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([MEMORY_PROVIDER_QUALITY_GATE_SUITE_NAME])


async def build_memory_provider_quality_gate_report() -> dict[str, Any]:
    summary = await _run_memory_provider_quality_gate_suite()
    failure_report = _memory_provider_quality_gate_failure_report(summary)
    healthy = summary.failed == 0
    return {
        "summary": {
            "suite_name": MEMORY_PROVIDER_QUALITY_GATE_SUITE_NAME,
            "benchmark_posture": (
                "memory_provider_quality_gate_ci_gated_operator_visible"
                if healthy
                else "memory_provider_quality_gate_regressions_detected_operator_visible"
            ),
            "operator_status": "memory_provider_quality_gate_visible",
            "scenario_count": len(MEMORY_PROVIDER_QUALITY_GATE_SCENARIO_NAMES),
            "dimension_count": len(memory_provider_quality_gate_dimensions()),
            "failure_mode_count": len(memory_provider_quality_gate_failure_taxonomy()),
            "active_failure_count": summary.failed,
            "declaration_state": "required_provider_declarations_visible" if healthy else "regressions_detected",
            "quality_state": "provider_evidence_quality_gated" if healthy else "regressions_detected",
            "suppression_state": "noisy_stale_conflicting_or_unsafe_provider_evidence_suppressed"
            if healthy
            else "regressions_detected",
            "operator_control_state": "inspect_correct_pin_forget_and_audit_surfaces_visible"
            if healthy
            else "regressions_detected",
            "claim_boundary": "deterministic_provider_quality_gate_not_live_external_memory_provider_superiority",
        },
        "scenario_names": list(MEMORY_PROVIDER_QUALITY_GATE_SCENARIO_NAMES),
        "dimensions": memory_provider_quality_gate_dimensions(),
        "failure_taxonomy": memory_provider_quality_gate_failure_taxonomy(),
        "failure_report": failure_report,
        "policy": memory_provider_quality_gate_policy_payload(),
        "latest_run": {
            "total": summary.total,
            "passed": summary.passed,
            "failed": summary.failed,
            "duration_ms": summary.duration_ms,
        },
    }
