from __future__ import annotations

from typing import Any


COMPUTER_USE_BENCHMARK_SUITE_NAME = "computer_use_browser_desktop"
COMPUTER_USE_BENCHMARK_SCENARIO_NAMES = (
    "browser_execution_task_replay_behavior",
    "browser_runtime_audit",
    "native_desktop_shell_behavior",
    "desktop_notification_action_replay_behavior",
    "cross_surface_notification_controls_behavior",
    "cross_surface_continuity_behavior",
    "workflow_boundary_blocked_surface_behavior",
)


def computer_use_benchmark_dimensions() -> list[dict[str, str]]:
    return [
        {
            "name": "browser_task_replay",
            "label": "Browser task replay",
            "summary": "Browser-backed tasks should leave deterministic extract/html/screenshot receipts instead of collapsing into one opaque browse result.",
        },
        {
            "name": "desktop_action_replay",
            "label": "Desktop action replay",
            "summary": "Desktop notification actions should remain replayable across enqueue, inspect, dismiss, daemon poll, and acknowledgement seams.",
        },
        {
            "name": "cross_surface_continuity",
            "label": "Cross-surface continuity",
            "summary": "Browser and desktop surfaces should share one inspectable continuity snapshot instead of diverging into separate operator narratives.",
        },
        {
            "name": "operator_receipts",
            "label": "Operator receipts",
            "summary": "Operators should be able to inspect benchmark posture, failure taxonomy, and replay policy through dedicated computer-use surfaces.",
        },
        {
            "name": "ci_regression_gating",
            "label": "CI regression gating",
            "summary": "Browser and desktop execution proof should live in a named deterministic suite that can gate regressions.",
        },
    ]


def computer_use_failure_taxonomy() -> list[dict[str, str]]:
    return [
        {
            "name": "browser_action_receipt_gap",
            "severity": "high",
            "summary": "Browser task replay does not preserve action-specific receipts for extract, html, or screenshot execution.",
        },
        {
            "name": "desktop_notification_action_loss",
            "severity": "high",
            "summary": "Desktop notification actions lose continuity across enqueue, dismiss, daemon handoff, or acknowledgement.",
        },
        {
            "name": "cross_surface_continuity_drift",
            "severity": "medium",
            "summary": "Browser and desktop continuity surfaces disagree about queued notifications, interventions, or follow-through state.",
        },
        {
            "name": "hidden_execution_failure_receipt",
            "severity": "medium",
            "summary": "Computer-use benchmark posture or failure taxonomy is not visible through operator-facing proof surfaces.",
        },
        {
            "name": "ungated_computer_use_regression",
            "severity": "medium",
            "summary": "Browser and desktop execution regressions are no longer pinned by a named deterministic suite.",
        },
    ]


def computer_use_benchmark_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suite": COMPUTER_USE_BENCHMARK_SUITE_NAME,
        "browser_task_replay_policy": "extract_html_and_screenshot_actions_require_distinct_audit_receipts",
        "desktop_action_replay_policy": "enqueue_dismiss_poll_and_ack_must_remain_cross_surface_replayable",
        "cross_surface_continuity_policy": "browser_and_desktop_share_one_operator_visible_continuity_snapshot",
        "operator_visibility": "benchmark_proof_plus_computer_use_receipts_visible",
        "receipt_surfaces": [
            "/api/operator/benchmark-proof",
            "/api/operator/computer-use-benchmark",
            "/api/operator/background-sessions",
            "/api/operator/workflow-orchestration",
            "/api/observer/continuity",
        ],
        "ci_gate_mode": "required_benchmark_suite",
    }


def _computer_use_failure_report(summary: Any) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append(
            {
                "type": "benchmark_regression",
                "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
                "summary": str(getattr(result, "error", "") or "Computer-use benchmark scenario failed."),
                "reason": "deterministic_eval_failure",
            }
        )
    return failures[:8]


async def _run_computer_use_benchmark_suite():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([COMPUTER_USE_BENCHMARK_SUITE_NAME])


async def build_computer_use_benchmark_report() -> dict[str, Any]:
    summary = await _run_computer_use_benchmark_suite()
    failure_report = _computer_use_failure_report(summary)
    healthy = summary.failed == 0
    benchmark_posture = (
        "ci_gated_operator_visible"
        if healthy
        else "ci_regressions_detected_operator_visible"
    )
    degraded_state = "regressions_detected"
    return {
        "summary": {
            "suite_name": COMPUTER_USE_BENCHMARK_SUITE_NAME,
            "benchmark_posture": benchmark_posture,
            "operator_status": "browser_desktop_receipts_visible",
            "scenario_count": len(COMPUTER_USE_BENCHMARK_SCENARIO_NAMES),
            "dimension_count": len(computer_use_benchmark_dimensions()),
            "failure_mode_count": len(computer_use_failure_taxonomy()),
            "active_failure_count": summary.failed,
            "browser_replay_state": (
                "extract_html_and_screenshot_receipts_visible"
                if healthy
                else degraded_state
            ),
            "desktop_action_state": (
                "dismiss_poll_and_ack_receipts_visible"
                if healthy
                else degraded_state
            ),
            "cross_surface_receipt_state": (
                "continuity_and_operator_receipts_visible"
                if healthy
                else degraded_state
            ),
        },
        "scenario_names": list(COMPUTER_USE_BENCHMARK_SCENARIO_NAMES),
        "dimensions": computer_use_benchmark_dimensions(),
        "failure_taxonomy": computer_use_failure_taxonomy(),
        "failure_report": failure_report,
        "policy": computer_use_benchmark_policy_payload(),
        "latest_run": {
            "total": summary.total,
            "passed": summary.passed,
            "failed": summary.failed,
            "duration_ms": summary.duration_ms,
        },
    }
