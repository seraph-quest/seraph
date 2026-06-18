from __future__ import annotations

from typing import Any


M7_OPERATOR_COCKPIT_BENCHMARK_SUITE_NAME = "m7_operator_cockpit_legibility"
M7_OPERATOR_COCKPIT_BENCHMARK_SCENARIO_NAMES = (
    "operator_cockpit_receipt_legibility_behavior",
    "operator_fast_control_availability_behavior",
    "operator_control_plane_handoff_legibility_behavior",
    "operator_m7_cockpit_benchmark_surface_behavior",
)


def m7_operator_cockpit_benchmark_dimensions() -> list[dict[str, str]]:
    return [
        {
            "name": "receipt_legibility",
            "label": "Operator-readable receipts",
            "summary": "Approval, integration, tool-failure, and routing receipts should expose plain summaries, status, time, and thread labels.",
        },
        {
            "name": "fast_controls",
            "label": "Fast control availability",
            "summary": "Active approvals, blocked workflows, and continuity follow-ups should carry enabled-state and control-mode metadata for continuation or repair controls.",
        },
        {
            "name": "control_plane_handoff",
            "label": "Control-plane handoff",
            "summary": "Governance roles, runtime posture, usage pressure, and handoff queues should be inspectable in one operator payload.",
        },
        {
            "name": "trust_boundary_clarity",
            "label": "Trust-boundary clarity",
            "summary": "Blocked workflow controls should preserve trust-boundary reasons before an operator resumes or retries work.",
        },
        {
            "name": "ci_regression_gating",
            "label": "CI regression gating",
            "summary": "Cockpit legibility should live in a named deterministic suite that can catch missing receipts or controls.",
        },
    ]


def m7_operator_cockpit_failure_taxonomy() -> list[dict[str, str]]:
    return [
        {
            "name": "opaque_receipt",
            "severity": "high",
            "summary": "A review or runtime receipt lacks a readable summary, status, timestamp, or thread label.",
        },
        {
            "name": "missing_fast_control",
            "severity": "high",
            "summary": "An approval, blocked workflow, or continuity follow-up is visible but lacks enabled-state or control-mode metadata for continuation or repair affordances.",
        },
        {
            "name": "hidden_trust_boundary",
            "severity": "high",
            "summary": "A blocked workflow can be resumed without showing the approval-context or trust-boundary reason first.",
        },
        {
            "name": "fragmented_control_plane",
            "severity": "medium",
            "summary": "Governance, role, usage, runtime, and handoff state drift back into separate unreadable surfaces.",
        },
        {
            "name": "ungated_cockpit_legibility_regression",
            "severity": "medium",
            "summary": "Cockpit control legibility is no longer pinned by a named deterministic benchmark suite.",
        },
    ]


def m7_operator_cockpit_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suite": M7_OPERATOR_COCKPIT_BENCHMARK_SUITE_NAME,
        "receipt_legibility_policy": "operator_receipts_must_expose_summary_status_timestamp_and_thread_context",
        "fast_control_policy": "active_handoff_items_must_carry_labeled_continue_or_repair_controls",
        "trust_boundary_policy": "blocked_workflow_controls_must_preserve_boundary_reason_before_resume",
        "operator_visibility": "benchmark_proof_plus_control_plane_plus_cockpit_receipts_visible",
        "receipt_surfaces": [
            "/api/operator/benchmark-proof",
            "/api/operator/m7-cockpit-legibility-benchmark",
            "/api/operator/control-plane",
            "/api/operator/timeline",
            "/api/activity/ledger",
        ],
        "ci_gate_mode": "required_benchmark_suite",
        "claim_boundary": "deterministic_operator_surface_receipts_not_live_external_usability_study",
    }


def _m7_operator_cockpit_failure_report(summary: Any) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append(
            {
                "type": "benchmark_regression",
                "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
                "summary": str(getattr(result, "error", "") or "M7 cockpit legibility benchmark scenario failed."),
                "reason": "deterministic_eval_failure",
            }
        )
    return failures[:6]


async def _run_m7_operator_cockpit_benchmark_suite():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([M7_OPERATOR_COCKPIT_BENCHMARK_SUITE_NAME])


async def build_m7_operator_cockpit_benchmark_report() -> dict[str, Any]:
    summary = await _run_m7_operator_cockpit_benchmark_suite()
    failure_report = _m7_operator_cockpit_failure_report(summary)
    healthy = summary.failed == 0
    benchmark_posture = (
        "m7_ci_gated_operator_visible"
        if healthy
        else "m7_ci_regressions_detected_operator_visible"
    )
    degraded_state = "regressions_detected"
    return {
        "summary": {
            "suite_name": M7_OPERATOR_COCKPIT_BENCHMARK_SUITE_NAME,
            "benchmark_posture": benchmark_posture,
            "operator_status": "m7_cockpit_legibility_visible",
            "scenario_count": len(M7_OPERATOR_COCKPIT_BENCHMARK_SCENARIO_NAMES),
            "dimension_count": len(m7_operator_cockpit_benchmark_dimensions()),
            "failure_mode_count": len(m7_operator_cockpit_failure_taxonomy()),
            "active_failure_count": summary.failed,
            "receipt_legibility_state": "summary_status_time_and_thread_visible" if healthy else degraded_state,
            "fast_control_state": "continue_repair_and_handoff_controls_visible" if healthy else degraded_state,
            "control_plane_state": "governance_usage_runtime_and_handoff_visible" if healthy else degraded_state,
            "trust_boundary_state": "blocked_controls_preserve_boundary_reason" if healthy else degraded_state,
        },
        "scenario_names": list(M7_OPERATOR_COCKPIT_BENCHMARK_SCENARIO_NAMES),
        "dimensions": m7_operator_cockpit_benchmark_dimensions(),
        "failure_taxonomy": m7_operator_cockpit_failure_taxonomy(),
        "failure_report": failure_report,
        "policy": m7_operator_cockpit_policy_payload(),
        "latest_run": {
            "total": summary.total,
            "passed": summary.passed,
            "failed": summary.failed,
            "duration_ms": summary.duration_ms,
        },
    }
