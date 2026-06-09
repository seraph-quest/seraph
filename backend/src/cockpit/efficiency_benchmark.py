from __future__ import annotations

from typing import Any


COCKPIT_EFFICIENCY_BENCHMARK_SUITE_NAME = "cockpit_operator_efficiency_benchmark"
COCKPIT_EFFICIENCY_BENCHMARK_SCENARIO_NAMES = (
    "cockpit_efficiency_task_fixture_behavior",
    "cockpit_efficiency_threshold_behavior",
    "cockpit_efficiency_receipt_coverage_behavior",
    "cockpit_efficiency_baseline_claim_boundary_behavior",
    "operator_cockpit_efficiency_benchmark_surface_behavior",
)


_TASKS: tuple[dict[str, Any], ...] = (
    {
        "task": "inspect",
        "surface": "operator_terminal",
        "target": "active_work_item",
        "initial_state": "active_work_visible_with_status_and_thread",
        "max_actions": 2,
        "max_seconds": 12,
        "receipt": "active_work_inspection_receipt",
        "success_condition": "operator_can_identify_current_state_and_next_safe_control",
        "measured_counters": ["action_count", "scripted_seconds", "receipt_visible"],
        "confidence_signal": "state_summary_and_next_control_visible",
    },
    {
        "task": "approve",
        "surface": "approval_lane",
        "target": "pending_approval",
        "initial_state": "pending_approval_visible_with_risk_and_scope",
        "max_actions": 2,
        "max_seconds": 10,
        "receipt": "approval_decision_receipt",
        "success_condition": "approval_decision_records_target_scope_and_receipt",
        "measured_counters": ["action_count", "scripted_seconds", "receipt_visible", "risk_visible"],
        "confidence_signal": "approval_scope_and_risk_visible_before_action",
    },
    {
        "task": "deny",
        "surface": "approval_lane",
        "target": "pending_approval",
        "initial_state": "pending_approval_visible_with_denial_reason_entry",
        "max_actions": 2,
        "max_seconds": 10,
        "receipt": "denial_reason_receipt",
        "success_condition": "denial_records_reason_without_mutating_target",
        "measured_counters": ["action_count", "scripted_seconds", "receipt_visible", "risk_visible"],
        "confidence_signal": "denial_reason_and_target_scope_visible",
    },
    {
        "task": "pause",
        "surface": "workflow_supervision",
        "target": "running_workflow",
        "initial_state": "running_workflow_visible_with_pause_control",
        "max_actions": 3,
        "max_seconds": 15,
        "receipt": "pause_control_receipt",
        "success_condition": "pause_control_leaves_no_fire_or_checkpoint_receipt",
        "measured_counters": ["action_count", "scripted_seconds", "receipt_visible", "blocked_state_visible"],
        "confidence_signal": "pause_effect_and_resume_path_visible",
    },
    {
        "task": "resume",
        "surface": "workflow_supervision",
        "target": "paused_workflow",
        "initial_state": "paused_workflow_visible_with_resume_plan",
        "max_actions": 3,
        "max_seconds": 15,
        "receipt": "resume_control_receipt",
        "success_condition": "resume_plan_is_visible_before_continuation",
        "measured_counters": ["action_count", "scripted_seconds", "receipt_visible", "lineage_visible"],
        "confidence_signal": "resume_plan_and_checkpoint_visible",
    },
    {
        "task": "retry",
        "surface": "workflow_supervision",
        "target": "failed_step",
        "initial_state": "failed_step_visible_with_error_and_retry_control",
        "max_actions": 3,
        "max_seconds": 18,
        "receipt": "retry_recovery_receipt",
        "success_condition": "retry_path_preserves_failure_context_and_recovery_receipt",
        "measured_counters": ["action_count", "scripted_seconds", "receipt_visible", "error_visible"],
        "confidence_signal": "failure_cause_visible_before_retry",
    },
    {
        "task": "repair",
        "surface": "active_triage",
        "target": "degraded_capability",
        "initial_state": "degraded_capability_visible_with_repair_hint",
        "max_actions": 4,
        "max_seconds": 25,
        "receipt": "repair_plan_receipt",
        "success_condition": "repair_plan_lists_blocker_owner_and_safe_next_step",
        "measured_counters": ["action_count", "scripted_seconds", "receipt_visible", "degraded_state_visible"],
        "confidence_signal": "blocker_and_safe_repair_path_visible",
    },
    {
        "task": "branch",
        "surface": "workflow_debugger",
        "target": "checkpoint_candidate",
        "initial_state": "checkpoint_candidate_visible_with_lineage",
        "max_actions": 4,
        "max_seconds": 25,
        "receipt": "branch_draft_receipt",
        "success_condition": "branch_draft_keeps_source_checkpoint_and_claim_boundary",
        "measured_counters": ["action_count", "scripted_seconds", "receipt_visible", "lineage_visible"],
        "confidence_signal": "source_checkpoint_and_branch_boundary_visible",
    },
    {
        "task": "compare",
        "surface": "output_history",
        "target": "related_outputs",
        "initial_state": "related_outputs_visible_with_versions",
        "max_actions": 3,
        "max_seconds": 20,
        "receipt": "comparison_draft_receipt",
        "success_condition": "comparison_identifies_current_candidate_and_prior_baseline",
        "measured_counters": ["action_count", "scripted_seconds", "receipt_visible", "lineage_visible"],
        "confidence_signal": "baseline_and_candidate_visible_together",
    },
    {
        "task": "revoke",
        "surface": "presence_and_connector_inventory",
        "target": "paired_connector_or_channel",
        "initial_state": "paired_identity_visible_with_mutation_boundary",
        "max_actions": 4,
        "max_seconds": 25,
        "receipt": "revocation_boundary_receipt",
        "success_condition": "revoked_identity_fails_closed_and_leaves_boundary_receipt",
        "measured_counters": ["action_count", "scripted_seconds", "receipt_visible", "boundary_visible"],
        "confidence_signal": "revocation_scope_and_followup_boundary_visible",
    },
    {
        "task": "audit",
        "surface": "activity_ledger",
        "target": "recent_high_risk_action",
        "initial_state": "recent_high_risk_action_visible_with_actor_and_time",
        "max_actions": 3,
        "max_seconds": 20,
        "receipt": "audit_drilldown_receipt",
        "success_condition": "audit_drilldown_links_actor_target_time_and_followup",
        "measured_counters": ["action_count", "scripted_seconds", "receipt_visible", "risk_visible"],
        "confidence_signal": "actor_target_and_time_visible",
    },
)


def cockpit_efficiency_scripted_tasks() -> list[dict[str, Any]]:
    return [dict(task) for task in _TASKS]


def cockpit_efficiency_dimensions() -> list[dict[str, str]]:
    return [
        {
            "name": "scripted_operator_tasks",
            "label": "Scripted operator tasks",
            "summary": "Inspect, approve, deny, pause, resume, retry, repair, branch, compare, revoke, and audit flows are measured from fixed fixtures.",
        },
        {
            "name": "action_budget",
            "label": "Action budget",
            "summary": "Every task carries a maximum action count so cockpit density is tied to operator path length.",
        },
        {
            "name": "time_budget",
            "label": "Time budget",
            "summary": "Every task carries a maximum completion-time threshold for repeatable efficiency scoring.",
        },
        {
            "name": "error_detectability",
            "label": "Error detectability",
            "summary": "Blocked, degraded, failed, and risky states must remain visible before the operator takes a destructive action.",
        },
        {
            "name": "receipt_coverage",
            "label": "Receipt coverage",
            "summary": "Efficiency proof requires each fast path to leave a receipt that can be audited after the action.",
        },
    ]


def cockpit_efficiency_failure_taxonomy() -> list[dict[str, str]]:
    return [
        {
            "name": "missing_scripted_task_fixture",
            "severity": "high",
            "summary": "One required cockpit control path lacks a deterministic task fixture.",
        },
        {
            "name": "action_budget_exceeded",
            "severity": "medium",
            "summary": "A required cockpit task takes more actions than its path-length budget allows.",
        },
        {
            "name": "time_budget_exceeded",
            "severity": "medium",
            "summary": "A required cockpit task exceeds its repeatable completion-time threshold.",
        },
        {
            "name": "error_state_hidden",
            "severity": "high",
            "summary": "An operator can act before seeing the failed, blocked, degraded, risky, or stale state.",
        },
        {
            "name": "receipt_missing_after_action",
            "severity": "high",
            "summary": "A fast cockpit action completes without an approval, workflow, audit, activity, or control receipt.",
        },
        {
            "name": "unsupported_superiority_claim",
            "severity": "medium",
            "summary": "The benchmark implies competitor superiority or live usability proof without source-dated evidence.",
        },
    ]


def cockpit_efficiency_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suite": COCKPIT_EFFICIENCY_BENCHMARK_SUITE_NAME,
        "measurement_policy": "scripted_tasks_require_action_time_error_and_receipt_metrics",
        "baseline_policy": "baseline_is_current_seraph_fixture_not_competitor_superiority_claim",
        "competitor_claim_policy": "competitor_informed_expectations_require_source_dated_evidence_before_public_claims",
        "claim_boundary": "deterministic_operator_efficiency_fixture_not_live_multi_operator_usability_study",
        "receipt_surfaces": [
            "/api/operator/benchmark-proof",
            "/api/operator/cockpit-efficiency-benchmark",
            "/api/operator/m7-cockpit",
            "/api/operator/control-plane",
            "/api/operator/workflow-orchestration",
            "/api/activity/ledger",
        ],
        "ci_gate_mode": "required_benchmark_suite",
    }


def cockpit_efficiency_scorecard() -> dict[str, Any]:
    tasks = cockpit_efficiency_scripted_tasks()
    return {
        "baseline": "current_seraph_fixture",
        "task_count": len(tasks),
        "max_actions_total": sum(int(task["max_actions"]) for task in tasks),
        "max_seconds_total": sum(int(task["max_seconds"]) for task in tasks),
        "required_tasks": [task["task"] for task in tasks],
        "receipt_count": len({task["receipt"] for task in tasks}),
        "baseline_scope": "develop_branch_at_batch_start_plus_shipped_cockpit_density_and_benchmark_visibility_work",
        "no_regression_rule": "each_scripted_task_must_stay_within_action_and_time_budget_and_keep_receipt_visible",
        "confidence_measurement_boundary": "confidence_affordance_proxy_not_operator_reported_confidence",
        "error_detectability_requirements": [
            "blocked_state_visible_before_action",
            "degraded_state_visible_before_repair",
            "risk_level_visible_before_approval",
            "lineage_visible_before_branch_or_compare",
        ],
    }


def _cockpit_efficiency_failure_report(summary: Any) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append(
            {
                "type": "benchmark_regression",
                "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
                "summary": str(getattr(result, "error", "") or "Cockpit efficiency benchmark scenario failed."),
                "reason": "deterministic_eval_failure",
            }
        )
    return failures[:8]


async def _run_cockpit_efficiency_benchmark_suite():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([COCKPIT_EFFICIENCY_BENCHMARK_SUITE_NAME])


async def build_cockpit_efficiency_benchmark_report() -> dict[str, Any]:
    summary = await _run_cockpit_efficiency_benchmark_suite()
    failure_report = _cockpit_efficiency_failure_report(summary)
    healthy = summary.failed == 0
    degraded_state = "regressions_detected"
    return {
        "summary": {
            "suite_name": COCKPIT_EFFICIENCY_BENCHMARK_SUITE_NAME,
            "benchmark_posture": (
                "cockpit_efficiency_ci_gated_operator_visible"
                if healthy
                else "cockpit_efficiency_ci_regressions_detected_operator_visible"
            ),
            "operator_status": "cockpit_efficiency_receipts_visible",
            "scenario_count": len(COCKPIT_EFFICIENCY_BENCHMARK_SCENARIO_NAMES),
            "dimension_count": len(cockpit_efficiency_dimensions()),
            "failure_mode_count": len(cockpit_efficiency_failure_taxonomy()),
            "active_failure_count": summary.failed,
            "scripted_task_state": "inspect_to_audit_paths_measured" if healthy else degraded_state,
            "threshold_state": "action_and_time_budgets_visible" if healthy else degraded_state,
            "error_detectability_state": "blocked_degraded_risky_and_lineage_states_visible" if healthy else degraded_state,
            "receipt_coverage_state": "all_scripted_tasks_have_receipts" if healthy else degraded_state,
            "claim_boundary": cockpit_efficiency_policy_payload()["claim_boundary"],
        },
        "scenario_names": list(COCKPIT_EFFICIENCY_BENCHMARK_SCENARIO_NAMES),
        "dimensions": cockpit_efficiency_dimensions(),
        "failure_taxonomy": cockpit_efficiency_failure_taxonomy(),
        "scripted_tasks": cockpit_efficiency_scripted_tasks(),
        "scorecard": cockpit_efficiency_scorecard(),
        "failure_report": failure_report,
        "policy": cockpit_efficiency_policy_payload(),
        "latest_run": {
            "total": summary.total,
            "passed": summary.passed,
            "failed": summary.failed,
            "duration_ms": summary.duration_ms,
        },
    }
