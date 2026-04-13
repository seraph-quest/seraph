from __future__ import annotations

from typing import Any


WORKFLOW_ENDURANCE_BENCHMARK_SUITE_NAME = "workflow_endurance_and_repair"
WORKFLOW_ENDURANCE_BENCHMARK_SCENARIO_NAMES = (
    "workflow_anticipatory_repair_behavior",
    "workflow_condensation_fidelity_behavior",
    "workflow_backup_branch_surface_behavior",
    "workflow_multi_session_endurance_behavior",
)


def workflow_endurance_benchmark_dimensions() -> list[dict[str, str]]:
    return [
        {
            "name": "anticipatory_repair",
            "label": "Anticipatory repair",
            "summary": "Seraph should prepare repair and branch options before obvious workflow failure points instead of only reacting after a breakage.",
        },
        {
            "name": "backup_branching",
            "label": "Backup branching",
            "summary": "Long-running workflows should preserve checkpoint-backed branch options that stay explicit, inspectable, and operator-selectable.",
        },
        {
            "name": "condensation_fidelity",
            "label": "Condensation fidelity",
            "summary": "State compaction should keep enough steps, recovery paths, and output lineage to preserve trustworthy continuation.",
        },
        {
            "name": "multi_session_endurance",
            "label": "Multi-session endurance",
            "summary": "Workflow orchestration should retain queue state, branch continuity, and recovery posture across multiple active sessions.",
        },
        {
            "name": "ci_regression_gating",
            "label": "CI regression gating",
            "summary": "Workflow endurance and anticipatory repair should live in a named deterministic benchmark suite that can gate regressions.",
        },
    ]


def workflow_endurance_failure_taxonomy() -> list[dict[str, str]]:
    return [
        {
            "name": "late_only_recovery",
            "severity": "high",
            "summary": "Seraph surfaces repair only after the workflow fails even though a checkpoint-backed backup branch was already available.",
        },
        {
            "name": "hidden_backup_branch",
            "severity": "high",
            "summary": "A reusable checkpoint exists but operators cannot see or choose the safer backup branch path.",
        },
        {
            "name": "compaction_fidelity_loss",
            "severity": "medium",
            "summary": "Long-context workflow compaction drops too much recovery truth, branch lineage, or output history to continue safely.",
        },
        {
            "name": "session_queue_drift",
            "severity": "medium",
            "summary": "Multi-session orchestration loses queue posture or attention ordering when several workflows compete for follow-through.",
        },
        {
            "name": "ungated_endurance_regression",
            "severity": "medium",
            "summary": "Workflow endurance and anticipatory repair behavior are no longer pinned by a named deterministic suite.",
        },
    ]


def workflow_endurance_benchmark_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suite": WORKFLOW_ENDURANCE_BENCHMARK_SUITE_NAME,
        "anticipatory_repair_policy": "prepare_repair_and_backup_branch_before_obvious_failure_points",
        "backup_branch_policy": "checkpoint_backed_branch_receipts_must_remain_operator_selectable",
        "condensation_fidelity_policy": "compaction_must_preserve_recovery_paths_and_output_lineage",
        "operator_visibility": "workflow_orchestration_and_benchmark_visible",
        "ci_gate_mode": "required_benchmark_suite",
    }


def _workflow_endurance_failure_report(summary: Any) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append(
            {
                "type": "benchmark_regression",
                "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
                "summary": str(getattr(result, "error", "") or "Workflow endurance benchmark scenario failed."),
                "reason": "deterministic_eval_failure",
            }
        )
    return failures[:6]


async def _run_workflow_endurance_benchmark_suite():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([WORKFLOW_ENDURANCE_BENCHMARK_SUITE_NAME])


def _workflow_endurance_summary_states(healthy: bool) -> dict[str, str]:
    if healthy:
        return {
            "anticipatory_repair_state": "checkpoint_and_pre_repair_visible",
            "condensation_fidelity_state": "recovery_paths_and_output_history_retained",
            "branch_continuity_state": "backup_branch_operator_selectable",
        }
    return {
        "anticipatory_repair_state": "regressions_detected",
        "condensation_fidelity_state": "regressions_detected",
        "branch_continuity_state": "regressions_detected",
    }


async def build_workflow_endurance_benchmark_report() -> dict[str, Any]:
    summary = await _run_workflow_endurance_benchmark_suite()
    failure_report = _workflow_endurance_failure_report(summary)
    healthy = summary.failed == 0
    benchmark_posture = (
        "ci_gated_operator_visible"
        if healthy
        else "ci_regressions_detected_operator_visible"
    )
    return {
        "summary": {
            "suite_name": WORKFLOW_ENDURANCE_BENCHMARK_SUITE_NAME,
            "benchmark_posture": benchmark_posture,
            "operator_status": "workflow_orchestration_visible",
            "scenario_count": len(WORKFLOW_ENDURANCE_BENCHMARK_SCENARIO_NAMES),
            "dimension_count": len(workflow_endurance_benchmark_dimensions()),
            "failure_mode_count": len(workflow_endurance_failure_taxonomy()),
            "active_failure_count": summary.failed,
            **_workflow_endurance_summary_states(healthy),
        },
        "scenario_names": list(WORKFLOW_ENDURANCE_BENCHMARK_SCENARIO_NAMES),
        "dimensions": workflow_endurance_benchmark_dimensions(),
        "failure_taxonomy": workflow_endurance_failure_taxonomy(),
        "failure_report": failure_report,
        "policy": workflow_endurance_benchmark_policy_payload(),
        "latest_run": {
            "total": summary.total,
            "passed": summary.passed,
            "failed": summary.failed,
            "duration_ms": summary.duration_ms,
        },
    }
