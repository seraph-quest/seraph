"""Hermes-style persisted scheduled job runtime tools."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Any

from smolagents import Tool, tool

from src.approval.runtime import get_current_session_id
from src.scheduler.engine import get_scheduler, sync_scheduled_jobs_blocking
from src.scheduler.scheduled_jobs import scheduled_job_repository

logger = logging.getLogger(__name__)


def _run(coro):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _parse_urgency(raw_value: Any) -> int:
    if raw_value is None:
        return 3
    try:
        return int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("urgency must be an integer.") from exc


def _sync_scheduler_state() -> str | None:
    try:
        sync_scheduled_jobs_blocking()
    except Exception:
        logger.exception("Failed to synchronize scheduled jobs with the runtime scheduler")
        return " Scheduler sync is degraded; restart the runtime or inspect logs."
    return None


def _render_job(job: dict[str, Any], *, index: int) -> list[str]:
    trigger_spec = job.get("trigger_spec") or {}
    action_spec = job.get("action_spec") or {}
    scheduler = get_scheduler()
    next_run_at = None
    if scheduler is not None:
        apscheduler_job = scheduler.get_job(f"user_cron:{job['id']}")
        if apscheduler_job is not None and apscheduler_job.next_run_time is not None:
            next_run_at = apscheduler_job.next_run_time.isoformat()

    lines = [
        (
            f"{index}. {job['name']} "
            f"(job={job['id']}, enabled={'yes' if job['enabled'] else 'no'}, "
            f"target={job['action_type']}, cron={trigger_spec.get('cron', '')}, "
            f"timezone={trigger_spec.get('timezone', '')})"
        )
    ]
    if job.get("session_id"):
        lines.append(f"   session={job['session_id']}")
    if next_run_at:
        lines.append(f"   next_run_at={next_run_at}")
    if job.get("last_outcome"):
        lines.append(f"   last_outcome={job['last_outcome']}")
    if job.get("last_error"):
        lines.append(f"   last_error={job['last_error']}")
    if job.get("last_approval_id"):
        lines.append(f"   last_approval_id={job['last_approval_id']}")
    if job["action_type"] == "deliver_message":
        lines.append(
            "   message="
            f"{action_spec.get('intervention_type', 'advisory')} "
            f"urgency={action_spec.get('urgency', 3)}"
        )
    elif job["action_type"] == "run_workflow":
        lines.append(f"   workflow={action_spec.get('workflow_name', '')}")
    return lines


@tool
def get_scheduled_jobs(limit: int = 20, include_disabled: bool = True) -> str:
    """List persisted scheduled jobs and their runtime status.

    Args:
        limit: Maximum number of jobs to include.
        include_disabled: Whether disabled jobs should be included.

    Returns:
        A formatted list of scheduled jobs.
    """
    current_session_id = get_current_session_id()
    if not current_session_id:
        return "Error: scheduled jobs require an active session."
    jobs = _run(
        scheduled_job_repository.list_jobs(
            include_disabled=include_disabled,
            limit=max(1, min(limit, 100)),
            owner_session_id=current_session_id,
        )
    )
    if not jobs:
        return "No scheduled jobs configured."
    lines: list[str] = []
    for index, job in enumerate(jobs, start=1):
        lines.extend(_render_job(job, index=index))
    return "\n".join(lines)


class ManageScheduledJobTool(Tool):
    skip_forward_signature_validation = True

    def __init__(self) -> None:
        super().__init__()
        self.name = "manage_scheduled_job"
        self.description = (
            "Create, update, pause, resume, or delete persisted scheduled jobs "
            "that deliver scheduled messages or run workflows."
        )
        self.inputs = {
            "action": {
                "type": "string",
                "description": "One of: create, update, delete, pause, resume.",
            },
            "job_id": {
                "type": "string",
                "description": "Existing job id for update/delete/pause/resume.",
                "nullable": True,
            },
            "name": {
                "type": "string",
                "description": "Human-readable job name.",
                "nullable": True,
            },
            "cron": {
                "type": "string",
                "description": "Five-field cron expression.",
                "nullable": True,
            },
            "timezone": {
                "type": "string",
                "description": "IANA timezone name for the cron schedule.",
                "nullable": True,
            },
            "target_type": {
                "type": "string",
                "description": "Target kind: message|deliver_message or workflow|run_workflow.",
                "nullable": True,
            },
            "content": {
                "type": "string",
                "description": "Scheduled message content for deliver_message jobs.",
                "nullable": True,
            },
            "intervention_type": {
                "type": "string",
                "description": "Scheduled message type (default advisory).",
                "nullable": True,
            },
            "urgency": {
                "type": "integer",
                "description": "Urgency 1-5 for scheduled messages.",
                "nullable": True,
            },
            "workflow_name": {
                "type": "string",
                "description": "Workflow name for run_workflow jobs.",
                "nullable": True,
            },
            "workflow_args_json": {
                "type": "string",
                "description": "JSON object of workflow inputs.",
                "nullable": True,
            },
            "session_id": {
                "type": "string",
                "description": "Conversation session id to bind the job to.",
                "nullable": True,
            },
        }
        self.output_type = "string"
        self.is_initialized = True

    def forward(self, *args, **kwargs):
        return self.__call__(*args, **kwargs)

    def __call__(self, *args, sanitize_inputs_outputs: bool = False, **kwargs):
        try:
            payload = self._normalize_inputs(args, kwargs)
        except ValueError as exc:
            return f"Error: {exc}"
        action = payload["action"]
        current_session_id = get_current_session_id()
        requested_session_id = payload["session_id"]
        if not current_session_id:
            return "Error: scheduled jobs require an active session."
        effective_session_id = current_session_id
        if action == "create":
            if requested_session_id and requested_session_id != current_session_id:
                return "Error: scheduled jobs cannot target a different session."
            payload["session_id"] = effective_session_id
        elif action == "update":
            if requested_session_id and requested_session_id != current_session_id:
                return "Error: scheduled jobs cannot target a different session."

        if action == "create":
            try:
                job = _run(
                    scheduled_job_repository.create_job(
                        name=payload["name"],
                        cron=payload["cron"],
                        timezone_name=payload["timezone"],
                        target_type=payload["target_type"],
                        content=payload["content"],
                        intervention_type=payload["intervention_type"],
                        urgency=payload["urgency"],
                        workflow_name=payload["workflow_name"],
                        workflow_args_json=payload["workflow_args_json"],
                        session_id=payload["session_id"],
                        created_by_session_id=effective_session_id,
                    )
                )
            except ValueError as exc:
                return f"Error: {exc}"
            sync_warning = _sync_scheduler_state()
            return (
                f"Scheduled job '{job['name']}' created "
                f"(job={job['id']}, target={job['action_type']})."
                f"{sync_warning or ''}"
            )

        job_id = payload["job_id"]
        if not job_id:
            return "Error: job_id is required for update, delete, pause, and resume."

        if action == "update":
            try:
                job = _run(
                    scheduled_job_repository.update_job(
                        job_id,
                        name=payload["name"],
                        cron=payload["cron"],
                        timezone_name=payload["timezone"],
                        target_type=payload["target_type"],
                        content=payload["content"],
                        intervention_type=payload["intervention_type"],
                        urgency=payload["urgency"],
                        workflow_name=payload["workflow_name"],
                        workflow_args_json=payload["workflow_args_json"],
                        session_id=payload["session_id"],
                        owner_session_id=effective_session_id,
                    )
                )
            except ValueError as exc:
                return f"Error: {exc}"
            if job is None:
                return f"Error: Scheduled job '{job_id}' was not found."
            sync_warning = _sync_scheduler_state()
            return (
                f"Scheduled job '{job['name']}' updated "
                f"(job={job['id']}, target={job['action_type']})."
                f"{sync_warning or ''}"
            )

        if action == "delete":
            deleted = _run(
                scheduled_job_repository.delete_job(job_id, owner_session_id=effective_session_id)
            )
            sync_warning = _sync_scheduler_state()
            return (
                f"Scheduled job '{job_id}' deleted.{sync_warning or ''}"
                if deleted
                else f"Error: Scheduled job '{job_id}' was not found."
            )

        if action == "pause":
            job = _run(
                scheduled_job_repository.set_enabled(
                    job_id,
                    False,
                    owner_session_id=effective_session_id,
                )
            )
            if job is None:
                return f"Error: Scheduled job '{job_id}' was not found."
            sync_warning = _sync_scheduler_state()
            return f"Scheduled job '{job['name']}' paused.{sync_warning or ''}"

        if action == "resume":
            job = _run(
                scheduled_job_repository.set_enabled(
                    job_id,
                    True,
                    owner_session_id=effective_session_id,
                )
            )
            if job is None:
                return f"Error: Scheduled job '{job_id}' was not found."
            sync_warning = _sync_scheduler_state()
            return f"Scheduled job '{job['name']}' resumed.{sync_warning or ''}"

        return "Error: Unsupported scheduled job action. Use create, update, delete, pause, or resume."

    def get_audit_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = str(arguments.get("action", "") or "").strip().lower() or "unknown"
        target_type = str(arguments.get("target_type", "") or "").strip().lower()
        payload = {
            "action": action,
            "job_id": bool(str(arguments.get("job_id", "") or "").strip()),
            "name_provided": bool(str(arguments.get("name", "") or "").strip()),
            "cron_provided": bool(str(arguments.get("cron", "") or "").strip()),
            "timezone_provided": bool(str(arguments.get("timezone", "") or "").strip()),
            "target_type": target_type or None,
            "content_redacted": bool(str(arguments.get("content", "") or "").strip()),
            "workflow_name": str(arguments.get("workflow_name", "") or "").strip() or None,
            "workflow_args_redacted": bool(str(arguments.get("workflow_args_json", "") or "").strip()),
            "session_id_provided": bool(str(arguments.get("session_id", "") or "").strip()),
        }
        return {key: value for key, value in payload.items() if value is not None}

    def get_audit_call_payload(self, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        sanitized = self.get_audit_arguments(arguments)
        action = sanitized.get("action", "unknown")
        target_type = sanitized.get("target_type", "")
        summary = f"Calling tool: manage_scheduled_job(action={action}"
        if target_type:
            summary += f", target_type={target_type}"
        summary += ")"
        return summary, {"arguments": sanitized}

    def _normalize_inputs(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        if len(args) == 1 and not kwargs and isinstance(args[0], dict):
            payload = dict(args[0])
        else:
            payload = dict(kwargs)
        return {
            "action": str(payload.get("action", "") or "").strip().lower(),
            "job_id": str(payload.get("job_id", "") or "").strip(),
            "name": str(payload.get("name", "") or "").strip(),
            "cron": str(payload.get("cron", "") or "").strip(),
            "timezone": str(payload.get("timezone", "") or "").strip(),
            "target_type": str(payload.get("target_type", "") or "").strip(),
            "content": str(payload.get("content", "") or ""),
            "intervention_type": str(payload.get("intervention_type", "") or "advisory").strip() or "advisory",
            "workflow_name": str(payload.get("workflow_name", "") or "").strip(),
            "workflow_args_json": str(payload.get("workflow_args_json", "") or "").strip(),
            "session_id": str(payload.get("session_id", "") or "").strip() or None,
            "urgency": _parse_urgency(payload.get("urgency", 3)),
        }


manage_scheduled_job = ManageScheduledJobTool()
