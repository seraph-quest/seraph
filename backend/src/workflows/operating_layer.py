"""M5 operating-layer receipts for jobs, routines, workflows, and delegation."""

from __future__ import annotations

from collections import Counter
from typing import Any

from src.tools.delegate_task_tool import infer_delegation_approval_context


M5_OPERATING_LAYER_BENCHMARK_SUITE_NAME = "m5_jobs_routines_workflows_delegation"
M5_OPERATING_LAYER_BENCHMARK_SCENARIO_NAMES = (
    "m5_operating_layer_payload_behavior",
    "scheduled_job_run_history_behavior",
    "scheduled_job_pause_resume_no_fire_behavior",
    "delegation_trust_partition_receipt_behavior",
    "operator_m5_benchmark_surface_behavior",
)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _normalize_text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def _trigger_label(job: dict[str, Any]) -> str:
    trigger_spec = _as_dict(job.get("trigger_spec"))
    cron = _normalize_text(trigger_spec.get("cron"))
    timezone = _normalize_text(trigger_spec.get("timezone"), "UTC")
    trigger_type = _normalize_text(job.get("trigger_type"), "cron")
    if trigger_type == "cron" and cron:
        return f"cron {cron} {timezone}"
    return trigger_type


def _action_label(job: dict[str, Any]) -> str:
    action_type = _normalize_text(job.get("action_type"), "unknown")
    action_spec = _as_dict(job.get("action_spec"))
    if action_type == "run_workflow":
        return f"workflow {_normalize_text(action_spec.get('workflow_name'), 'unknown')}"
    if action_type == "deliver_message":
        return f"message {_normalize_text(action_spec.get('intervention_type'), 'advisory')}"
    return action_type


def _routine_entry(job: dict[str, Any], run_history: list[dict[str, Any]]) -> dict[str, Any]:
    action_spec = _as_dict(job.get("action_spec"))
    trigger_spec = _as_dict(job.get("trigger_spec"))
    job_id = _normalize_text(job.get("id"))
    runs = [
        run for run in run_history
        if _normalize_text(run.get("scheduled_job_id")) == job_id
    ]
    latest_run = runs[0] if runs else None
    action_type = _normalize_text(job.get("action_type"), "unknown")
    enabled = bool(job.get("enabled"))
    return {
        "id": job_id,
        "name": _normalize_text(job.get("name"), "Scheduled job"),
        "status": "active" if enabled else "paused",
        "enabled": enabled,
        "routine_kind": "cron_workflow" if action_type == "run_workflow" else "cron_message",
        "trigger_type": _normalize_text(job.get("trigger_type"), "cron"),
        "trigger_label": _trigger_label(job),
        "action_type": action_type,
        "action_label": _action_label(job),
        "trigger": {
            "type": _normalize_text(job.get("trigger_type"), "cron"),
            "cron": trigger_spec.get("cron"),
            "timezone": trigger_spec.get("timezone"),
        },
        "action": {
            "type": action_type,
            "workflow_name": action_spec.get("workflow_name"),
            "intervention_type": action_spec.get("intervention_type"),
            "urgency": action_spec.get("urgency"),
        },
        "session_id": job.get("session_id"),
        "created_by_session_id": job.get("created_by_session_id"),
        "last_run_at": job.get("last_run_at"),
        "last_outcome": job.get("last_outcome"),
        "last_error": job.get("last_error"),
        "last_approval_id": job.get("last_approval_id"),
        "run_history_count": len(runs),
        "run_count": len(runs),
        "run_history": runs[:5],
        "latest_run": latest_run,
        "next_run_at": job.get("next_run_at"),
        "lifecycle_controls": ["pause" if enabled else "resume", "update", "delete"],
        "audit_receipts": [
            f"scheduled_job:{job_id}",
            *[f"scheduled_job_run:{run.get('id')}" for run in runs[:3] if run.get("id")],
        ],
        "claim_boundary": "cron_style_job_or_routine",
    }


def _workflow_receipt(run: dict[str, Any]) -> dict[str, Any]:
    checkpoint_candidates = _as_list(run.get("checkpoint_candidates"))
    step_records = _as_list(run.get("step_records"))
    delegated_specialists: list[str] = []
    delegated_tool_names: list[str] = []
    approval_context = _as_dict(run.get("approval_context"))
    delegated_specialists.extend(str(item) for item in _as_list(approval_context.get("delegated_specialists")) if str(item).strip())
    delegated_tool_names.extend(str(item) for item in _as_list(approval_context.get("delegated_tool_names")) if str(item).strip())
    for step in step_records:
        step_context = _as_dict(_as_dict(step).get("approval_context"))
        delegated_specialists.extend(
            str(item) for item in _as_list(step_context.get("delegated_specialists")) if str(item).strip()
        )
        delegated_tool_names.extend(
            str(item) for item in _as_list(step_context.get("delegated_tool_names")) if str(item).strip()
        )
    return {
        "run_identity": run.get("run_identity"),
        "root_run_identity": run.get("root_run_identity"),
        "parent_run_identity": run.get("parent_run_identity"),
        "workflow_name": run.get("workflow_name"),
        "status": run.get("status"),
        "availability": run.get("availability"),
        "session_id": run.get("thread_id") or run.get("session_id"),
        "branch_kind": run.get("branch_kind"),
        "branch_depth": int(run.get("branch_depth") or 0),
        "checkpoint_candidate_count": len(checkpoint_candidates),
        "repair_ready": bool(run.get("retry_from_step_draft")) or any(
            bool(_as_dict(step).get("is_recoverable")) for step in step_records
        ),
        "replay_allowed": bool(run.get("replay_allowed", False)),
        "replay_block_reason": run.get("replay_block_reason"),
        "pending_approval_count": int(run.get("pending_approval_count") or 0),
        "delegation_receipt": {
            "delegated_specialists": sorted(dict.fromkeys(delegated_specialists)),
            "delegated_tool_names": sorted(dict.fromkeys(delegated_tool_names)),
            "delegation_present": bool(delegated_specialists or delegated_tool_names),
            "trust_partition": approval_context.get("trust_partition"),
        },
        "claim_boundary": "audit_projected_workflow_receipt_not_durable_state_machine",
    }


def _background_receipt(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session.get("session_id") or session.get("id"),
        "title": session.get("title"),
        "workflow_count": int(session.get("workflow_count") or 0),
        "active_workflows": int(session.get("active_workflows") or 0),
        "blocked_workflows": int(session.get("blocked_workflows") or 0),
        "background_process_count": int(session.get("background_process_count") or 0),
        "running_background_process_count": int(session.get("running_background_process_count") or 0),
        "branch_handoff_available": bool(session.get("branch_handoff_available")),
        "trust_partition": session.get("trust_partition"),
        "claim_boundary": "session_and_process_projection",
    }


def build_delegation_trust_receipts() -> list[dict[str, Any]]:
    probes = (
        ("memory", "Remember this project preference for later."),
        ("vault", "Store this API key safely."),
        ("workflow", "Run the release workflow."),
        ("missing-specialist", "Handle this privileged task."),
    )
    receipts: list[dict[str, Any]] = []
    for specialist, task in probes:
        context = infer_delegation_approval_context(task, specialist=specialist, specialists=[])
        receipts.append({
            "id": f"delegation:{specialist}",
            "specialist": specialist,
            "owner": "operator",
            "status": "blocked" if bool(context.get("delegation_target_unresolved")) else "available",
            "task_summary": task,
            "artifact_count": 0,
            "review_state": "not_started",
            "delegated_specialist": context.get("delegated_specialist"),
            "risk_level": context.get("risk_level"),
            "execution_boundaries": _as_list(context.get("execution_boundaries")),
            "accepts_secret_refs": bool(context.get("accepts_secret_refs")),
            "authenticated_source": bool(context.get("authenticated_source")),
            "delegation_target_unresolved": bool(context.get("delegation_target_unresolved")),
            "delegated_tool_names": _as_list(context.get("delegated_tool_names")),
            "trust_partition": _as_dict(context.get("trust_partition")),
        })
    return receipts


def build_m5_operating_layer_payload(
    *,
    scheduled_jobs: list[dict[str, Any]],
    scheduled_job_runs: list[dict[str, Any]],
    workflow_runs: list[dict[str, Any]],
    background_sessions: list[dict[str, Any]],
    delegation_receipts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    routines = [_routine_entry(job, scheduled_job_runs) for job in scheduled_jobs]
    workflow_receipts = [_workflow_receipt(run) for run in workflow_runs]
    background_receipts = [_background_receipt(session) for session in background_sessions]
    delegation_receipts = delegation_receipts or build_delegation_trust_receipts()
    run_outcomes = Counter(_normalize_text(run.get("outcome"), "unknown") for run in scheduled_job_runs)
    active_workflows = [
        receipt for receipt in workflow_receipts
        if str(receipt.get("status") or "") not in {"succeeded", "failed", "cancelled"}
    ]
    paused_routines = [routine for routine in routines if not routine["enabled"]]
    awaiting_approval_count = (
        sum(1 for run in scheduled_job_runs if run.get("outcome") == "approval_required")
        + sum(int(workflow.get("pending_approval_count") or 0) for workflow in workflow_receipts)
    )
    failed_work_count = (
        sum(1 for run in scheduled_job_runs if run.get("outcome") == "failed")
        + sum(1 for workflow in workflow_receipts if workflow.get("status") == "failed")
    )
    checkpoint_ready_count = sum(
        1 for item in workflow_receipts if int(item["checkpoint_candidate_count"]) > 0
    )
    repair_ready_count = sum(1 for item in workflow_receipts if bool(item["repair_ready"]))
    branch_ready_count = sum(
        1 for item in workflow_receipts
        if _normalize_text(item.get("branch_kind")) in {"branch_from_checkpoint", "retry_failed_step"}
        or int(item["checkpoint_candidate_count"]) > 0
    )
    work_queue: list[dict[str, Any]] = []
    for routine in routines:
        if routine.get("last_outcome") in {"failed", "approval_required"} or not routine.get("enabled"):
            work_queue.append({
                "id": f"job:{routine['id']}",
                "kind": "scheduled_job",
                "label": routine["name"],
                "status": routine["status"] if routine.get("enabled") else "paused",
                "detail": f"{routine['trigger_label']} -> {routine['action_label']}",
                "priority": "high" if routine.get("last_outcome") in {"failed", "approval_required"} else "normal",
                "thread_id": routine.get("session_id"),
                "continue_message": (
                    f"Review scheduled job \"{routine['name']}\" after {routine.get('last_outcome')}."
                    if routine.get("last_outcome")
                    else None
                ),
                "checkpoint_ready": False,
                "repair_ready": routine.get("last_outcome") == "failed",
                "branch_ready": False,
                "delegation_ready": False,
                "approval_required": routine.get("last_outcome") == "approval_required",
                "actions": list(routine.get("lifecycle_controls") or []),
            })
    for workflow in workflow_receipts:
        if workflow.get("status") in {"failed", "awaiting_approval", "running"} or workflow.get("repair_ready"):
            work_queue.append({
                "id": f"workflow:{workflow.get('run_identity') or workflow.get('workflow_name')}",
                "kind": "workflow_run",
                "label": _normalize_text(workflow.get("workflow_name"), "workflow"),
                "status": _normalize_text(workflow.get("status"), "unknown"),
                "detail": _normalize_text(workflow.get("replay_block_reason"), "workflow supervision receipt"),
                "priority": "high" if workflow.get("pending_approval_count") else "normal",
                "thread_id": workflow.get("session_id"),
                "continue_message": None,
                "checkpoint_ready": int(workflow.get("checkpoint_candidate_count") or 0) > 0,
                "repair_ready": bool(workflow.get("repair_ready")),
                "branch_ready": int(workflow.get("checkpoint_candidate_count") or 0) > 0,
                "delegation_ready": bool(_as_dict(workflow.get("delegation_receipt")).get("delegation_present")),
                "approval_required": int(workflow.get("pending_approval_count") or 0) > 0,
                "actions": ["inspect", "continue", "branch", "repair"],
            })
    for delegation in delegation_receipts:
        if delegation.get("delegation_target_unresolved") or _normalize_text(delegation.get("risk_level")) == "high":
            work_queue.append({
                "id": f"delegation:{delegation.get('specialist')}",
                "kind": "delegation",
                "label": _normalize_text(delegation.get("specialist"), "delegation"),
                "status": "blocked" if delegation.get("delegation_target_unresolved") else "review_required",
                "detail": _normalize_text(delegation.get("task_summary"), "delegation trust partition"),
                "priority": "high",
                "thread_id": None,
                "continue_message": None,
                "checkpoint_ready": False,
                "repair_ready": bool(delegation.get("delegation_target_unresolved")),
                "branch_ready": False,
                "delegation_ready": True,
                "approval_required": _normalize_text(delegation.get("risk_level")) == "high",
                "actions": ["inspect", "review"],
            })
    work_item_count = len(routines) + len(workflow_receipts) + len(delegation_receipts)
    return {
        "summary": {
            "work_item_count": work_item_count,
            "scheduled_job_count": len(scheduled_jobs),
            "routine_count": len(routines),
            "workflow_run_count": len(workflow_receipts),
            "delegation_partition_count": len(delegation_receipts),
            "active_work_count": len(active_workflows) + sum(1 for item in routines if item["enabled"]),
            "paused_work_count": len(paused_routines),
            "awaiting_approval_count": awaiting_approval_count,
            "failed_work_count": failed_work_count,
            "repair_ready_count": repair_ready_count,
            "checkpoint_ready_count": checkpoint_ready_count,
            "branch_ready_count": branch_ready_count,
            "session_churn_risk_count": sum(1 for item in background_receipts if item["active_workflows"] > 0),
            "durable_run_receipt_count": len(scheduled_job_runs),
            "operator_status": "m5_operating_layer_visible",
            "enabled_routine_count": sum(1 for item in routines if item["enabled"]),
            "scheduled_job_run_count": len(scheduled_job_runs),
            "workflow_receipt_count": len(workflow_receipts),
            "active_workflow_count": len(active_workflows),
            "checkpoint_ready_workflow_count": sum(
                1 for item in workflow_receipts if int(item["checkpoint_candidate_count"]) > 0
            ),
            "repair_ready_workflow_count": sum(1 for item in workflow_receipts if bool(item["repair_ready"])),
            "background_session_count": len(background_receipts),
            "delegation_receipt_count": len(delegation_receipts),
            "unresolved_delegation_count": sum(
                1 for item in delegation_receipts if bool(item.get("delegation_target_unresolved"))
            ),
            "claim_boundary": "cron_jobs_routines_projected_workflows_and_delegation_receipts",
        },
        "claim_boundaries": {
            "implemented_triggers": ["cron"],
            "implemented_routine_surfaces": ["scheduled_jobs", "scheduled_job_runs"],
            "workflow_state_source": "audit_projected_workflow_receipts",
            "delegation_state_source": "approval_context_and_trust_partition_receipts",
            "future_triggers": ["heartbeat", "reactive_event_trigger", "durable_state_machine_executor"],
            "not_claimed": [
                "full_durable_workflow_state_machine",
                "heartbeat_or_reactive_trigger_execution",
                "unbounded_autonomous_multi_agent_orchestration",
            ],
        },
        "jobs": routines,
        "routines": routines,
        "scheduled_job_runs": scheduled_job_runs,
        "run_outcomes": dict(sorted(run_outcomes.items())),
        "workflows": workflow_receipts,
        "background_sessions": background_receipts,
        "delegations": delegation_receipts,
        "delegation_trust_receipts": delegation_receipts,
        "work_queue": work_queue[:12],
        "missing_trigger_classes": ["heartbeat", "reactive_event_trigger"],
        "proof_receipts": [
            "/api/operator/m5-operating-layer",
            "/api/operator/m5-operating-layer-benchmark",
            "scheduled_job_runs",
            "workflow_audit_projection",
            "delegation_trust_partition",
        ],
    }


def m5_operating_layer_dimensions() -> list[dict[str, str]]:
    return [
        {
            "name": "cron_routine_receipts",
            "label": "Cron routine receipts",
            "summary": "Scheduled jobs and routines expose lifecycle, trigger, action, and per-run outcome history.",
        },
        {
            "name": "workflow_projection",
            "label": "Workflow projection",
            "summary": "Workflow branch, checkpoint, repair, and replay posture is shown as audit-projected state, not as a new durable executor.",
        },
        {
            "name": "delegation_partitions",
            "label": "Delegation trust partitions",
            "summary": "Delegated specialist routes expose risk, tool boundary, secret, authentication, and unresolved-target receipts.",
        },
        {
            "name": "background_churn",
            "label": "Background/session churn",
            "summary": "Active sessions, processes, branch handoffs, and blocked workflows stay visible in one operating-layer payload.",
        },
        {
            "name": "claim_boundary",
            "label": "Claim boundary",
            "summary": "Heartbeat and reactive triggers remain future work unless they are implemented as real trigger surfaces.",
        },
    ]


def m5_operating_layer_failure_taxonomy() -> list[dict[str, str]]:
    return [
        {
            "name": "missing_run_history",
            "severity": "high",
            "summary": "A scheduled routine fires without a durable per-run receipt.",
        },
        {
            "name": "paused_job_fires",
            "severity": "high",
            "summary": "A disabled routine executes its action instead of recording a skipped no-fire receipt.",
        },
        {
            "name": "workflow_projection_overclaim",
            "severity": "medium",
            "summary": "The operator surface implies a durable workflow state machine when only audit-projected receipts exist.",
        },
        {
            "name": "delegation_partition_hidden",
            "severity": "high",
            "summary": "Delegated specialist risk and trust partitions are not visible to the operator.",
        },
        {
            "name": "future_trigger_overclaim",
            "severity": "medium",
            "summary": "Heartbeat or reactive triggers are described as implemented before a real trigger path exists.",
        },
    ]


def m5_operating_layer_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suite": M5_OPERATING_LAYER_BENCHMARK_SUITE_NAME,
        "operator_visibility": "m5_operating_layer_and_benchmark_visible",
        "scheduled_job_run_history_policy": "every_trigger_attempt_records_a_durable_run_receipt",
        "pause_resume_policy": "disabled_jobs_record_skipped_receipts_without_firing_actions",
        "workflow_projection_policy": "workflow_runs_are_audit_projected_until_a_durable_executor_exists",
        "delegation_partition_policy": "delegated_specialist_trust_boundaries_must_be_operator_visible",
        "ci_gate_mode": "required_benchmark_suite",
        "receipt_surfaces": [
            "/api/operator/m5-operating-layer",
            "/api/operator/m5-operating-layer-benchmark",
            "/api/operator/benchmark-proof",
        ],
    }
