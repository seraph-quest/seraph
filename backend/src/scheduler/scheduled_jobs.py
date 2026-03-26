"""Persisted user-scheduled jobs for dynamic cron routines."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import and_, or_
from sqlmodel import select, col

from config.settings import settings
from src.approval.exceptions import ApprovalRequired
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.audit.runtime import log_scheduler_job_event
from src.db.engine import get_session
from src.db.models import ScheduledJob
from src.db.session_refs import ensure_sessions_exist
from src.models.schemas import WSResponse
from src.observer.delivery import deliver_or_queue
from src.observer.manager import context_manager
from src.workflows.manager import workflow_manager

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_timezone(tz_name: str | None) -> str:
    candidate = (tz_name or "").strip() or settings.user_timezone.strip() or "UTC"
    try:
        import zoneinfo

        zoneinfo.ZoneInfo(candidate)
        return candidate
    except Exception:
        fallback = settings.user_timezone.strip() or "UTC"
        try:
            import zoneinfo

            zoneinfo.ZoneInfo(fallback)
            return fallback
        except Exception:
            return "UTC"


def _validate_cron_spec(cron: str, timezone_name: str) -> dict[str, Any]:
    normalized_cron = cron.strip()
    if not normalized_cron:
        raise ValueError("Scheduled jobs require a non-empty cron expression.")
    effective_timezone = _normalize_timezone(timezone_name)
    try:
        CronTrigger.from_crontab(normalized_cron, timezone=effective_timezone)
    except Exception as exc:
        raise ValueError(f"Invalid cron expression '{normalized_cron}'.") from exc
    return {
        "cron": normalized_cron,
        "timezone": effective_timezone,
    }


def _parse_workflow_args_json(raw: str) -> dict[str, Any]:
    normalized = raw.strip()
    if not normalized:
        return {}
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise ValueError("workflow_args_json must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ValueError("workflow_args_json must decode to an object.")
    return payload


def _normalize_action_spec(
    *,
    target_type: str,
    content: str,
    intervention_type: str,
    urgency: int,
    workflow_name: str,
    workflow_args_json: str,
) -> tuple[str, dict[str, Any]]:
    normalized_target = target_type.strip().lower()
    if normalized_target in {"message", "deliver_message"}:
        normalized_content = content.strip()
        if not normalized_content:
            raise ValueError("deliver_message jobs require non-empty content.")
        return (
            "deliver_message",
            {
                "content": normalized_content,
                "intervention_type": (intervention_type or "advisory").strip() or "advisory",
                "urgency": max(1, min(int(urgency), 5)),
            },
        )

    if normalized_target in {"workflow", "run_workflow"}:
        normalized_workflow = workflow_name.strip()
        if not normalized_workflow:
            raise ValueError("run_workflow jobs require a workflow_name.")
        workflow = workflow_manager.get_workflow(normalized_workflow)
        if workflow is None or not workflow.enabled:
            raise ValueError(f"Workflow '{normalized_workflow}' is not available.")
        return (
            "run_workflow",
            {
                "workflow_name": normalized_workflow,
                "workflow_args": _parse_workflow_args_json(workflow_args_json),
            },
        )

    raise ValueError("target_type must be one of: message, deliver_message, workflow, run_workflow.")


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def _loads(payload: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _safe_error_label(exc: Exception) -> str:
    return type(exc).__name__


def build_cron_trigger(job: dict[str, Any]) -> CronTrigger:
    trigger_spec = job.get("trigger_spec") or {}
    return CronTrigger.from_crontab(
        str(trigger_spec.get("cron") or "").strip(),
        timezone=str(trigger_spec.get("timezone") or "UTC"),
    )


def _owner_visibility_clause(owner_session_id: str):
    return or_(
        ScheduledJob.created_by_session_id == owner_session_id,
        and_(
            ScheduledJob.created_by_session_id.is_(None),
            ScheduledJob.session_id == owner_session_id,
        ),
    )


class ScheduledJobRepository:
    async def list_jobs(
        self,
        *,
        include_disabled: bool = True,
        limit: int | None = 20,
        owner_session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        async with get_session() as db:
            stmt = select(ScheduledJob).order_by(col(ScheduledJob.updated_at).desc())
            if owner_session_id:
                stmt = stmt.where(_owner_visibility_clause(owner_session_id))
            if not include_disabled:
                stmt = stmt.where(ScheduledJob.enabled.is_(True))
            if isinstance(limit, int):
                stmt = stmt.limit(limit)
            result = await db.execute(stmt)
            return [self._serialize(job) for job in result.scalars().all()]

    async def get_job(self, job_id: str, *, owner_session_id: str | None = None) -> dict[str, Any] | None:
        async with get_session() as db:
            stmt = select(ScheduledJob).where(ScheduledJob.id == job_id)
            if owner_session_id:
                stmt = stmt.where(_owner_visibility_clause(owner_session_id))
            result = await db.execute(stmt)
            job = result.scalars().first()
            if job is None:
                return None
            return self._serialize(job)

    async def create_job(
        self,
        *,
        name: str,
        cron: str,
        timezone_name: str,
        target_type: str,
        content: str,
        intervention_type: str,
        urgency: int,
        workflow_name: str,
        workflow_args_json: str,
        session_id: str | None,
        created_by_session_id: str | None,
    ) -> dict[str, Any]:
        trigger_spec = _validate_cron_spec(cron, timezone_name)
        action_type, action_spec = _normalize_action_spec(
            target_type=target_type,
            content=content,
            intervention_type=intervention_type,
            urgency=urgency,
            workflow_name=workflow_name,
            workflow_args_json=workflow_args_json,
        )
        async with get_session() as db:
            await ensure_sessions_exist(db, [session_id, created_by_session_id])
            job = ScheduledJob(
                name=name.strip() or "Scheduled job",
                enabled=True,
                trigger_type="cron",
                trigger_spec_json=_dumps(trigger_spec),
                action_type=action_type,
                action_spec_json=_dumps(action_spec),
                session_id=session_id,
                created_by_session_id=created_by_session_id,
            )
            db.add(job)
            await db.flush()
            await db.refresh(job)
            return self._serialize(job)

    async def update_job(
        self,
        job_id: str,
        *,
        name: str = "",
        cron: str = "",
        timezone_name: str = "",
        target_type: str = "",
        content: str = "",
        intervention_type: str = "",
        urgency: int | None = None,
        workflow_name: str = "",
        workflow_args_json: str = "",
        session_id: str | None = None,
        owner_session_id: str | None = None,
    ) -> dict[str, Any] | None:
        async with get_session() as db:
            stmt = select(ScheduledJob).where(ScheduledJob.id == job_id)
            if owner_session_id:
                stmt = stmt.where(_owner_visibility_clause(owner_session_id))
            result = await db.execute(stmt)
            job = result.scalars().first()
            if job is None:
                return None

            trigger_spec = _loads(job.trigger_spec_json)
            action_spec = _loads(job.action_spec_json)
            effective_target = target_type or job.action_type
            effective_urgency = urgency if urgency is not None else int(action_spec.get("urgency", 3) or 3)
            next_content = content if content else str(action_spec.get("content", ""))
            next_intervention_type = (
                intervention_type
                if intervention_type
                else str(action_spec.get("intervention_type", "advisory") or "advisory")
            )
            next_workflow_name = workflow_name or str(action_spec.get("workflow_name", ""))
            existing_workflow_args = action_spec.get("workflow_args", {})
            next_workflow_args_json = (
                workflow_args_json
                if workflow_args_json
                else json.dumps(existing_workflow_args, ensure_ascii=True, sort_keys=True)
            )

            next_trigger_spec = _validate_cron_spec(
                cron or str(trigger_spec.get("cron") or ""),
                timezone_name or str(trigger_spec.get("timezone") or ""),
            )
            next_action_type, next_action_spec = _normalize_action_spec(
                target_type=effective_target,
                content=next_content,
                intervention_type=next_intervention_type,
                urgency=effective_urgency,
                workflow_name=next_workflow_name,
                workflow_args_json=next_workflow_args_json,
            )

            if name.strip():
                job.name = name.strip()
            job.trigger_type = "cron"
            job.trigger_spec_json = _dumps(next_trigger_spec)
            job.action_type = next_action_type
            job.action_spec_json = _dumps(next_action_spec)
            if session_id is not None:
                await ensure_sessions_exist(db, [session_id])
                job.session_id = session_id
            job.updated_at = _utc_now()
            await db.flush()
            await db.refresh(job)
            return self._serialize(job)

    async def set_enabled(
        self,
        job_id: str,
        enabled: bool,
        *,
        owner_session_id: str | None = None,
    ) -> dict[str, Any] | None:
        async with get_session() as db:
            stmt = select(ScheduledJob).where(ScheduledJob.id == job_id)
            if owner_session_id:
                stmt = stmt.where(_owner_visibility_clause(owner_session_id))
            result = await db.execute(stmt)
            job = result.scalars().first()
            if job is None:
                return None
            job.enabled = enabled
            job.updated_at = _utc_now()
            await db.flush()
            await db.refresh(job)
            return self._serialize(job)

    async def delete_job(self, job_id: str, *, owner_session_id: str | None = None) -> bool:
        async with get_session() as db:
            stmt = select(ScheduledJob).where(ScheduledJob.id == job_id)
            if owner_session_id:
                stmt = stmt.where(_owner_visibility_clause(owner_session_id))
            result = await db.execute(stmt)
            job = result.scalars().first()
            if job is None:
                return False
            await db.delete(job)
            return True

    async def record_run(
        self,
        job_id: str,
        *,
        outcome: str,
        error: str | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any] | None:
        async with get_session() as db:
            result = await db.execute(select(ScheduledJob).where(ScheduledJob.id == job_id))
            job = result.scalars().first()
            if job is None:
                return None
            job.last_run_at = _utc_now()
            job.last_outcome = outcome
            job.last_error = error[:400] if isinstance(error, str) and error else None
            job.last_approval_id = approval_id
            job.updated_at = _utc_now()
            await db.flush()
            await db.refresh(job)
            return self._serialize(job)

    def _serialize(self, job: ScheduledJob) -> dict[str, Any]:
        return {
            "id": job.id,
            "name": job.name,
            "enabled": bool(job.enabled),
            "trigger_type": job.trigger_type,
            "trigger_spec": _loads(job.trigger_spec_json),
            "action_type": job.action_type,
            "action_spec": _loads(job.action_spec_json),
            "session_id": job.session_id,
            "created_by_session_id": job.created_by_session_id,
            "last_run_at": job.last_run_at.isoformat() if job.last_run_at else None,
            "last_outcome": job.last_outcome,
            "last_error": job.last_error,
            "last_approval_id": job.last_approval_id,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
        }


async def execute_scheduled_job(job_id: str) -> None:
    job = await scheduled_job_repository.get_job(job_id)
    if job is None or not job.get("enabled", False):
        return

    action_type = str(job.get("action_type") or "")
    try:
        if action_type == "deliver_message":
            action_spec = job.get("action_spec") or {}
            message = WSResponse(
                type="proactive",
                content=str(action_spec.get("content") or ""),
                intervention_type=str(action_spec.get("intervention_type") or "advisory"),
                urgency=int(action_spec.get("urgency") or 3),
                reasoning=f"scheduled job {job.get('name')}",
            )
            result = await deliver_or_queue(
                message,
                is_scheduled=True,
                session_id=job.get("session_id"),
            )
            delivery_outcome = result.audit_decision
            if message.intervention_id:
                from src.guardian.feedback import guardian_feedback_repository

                intervention = await guardian_feedback_repository.get(message.intervention_id)
                if intervention is not None and intervention.latest_outcome:
                    delivery_outcome = intervention.latest_outcome
            await scheduled_job_repository.record_run(
                job_id,
                outcome=delivery_outcome,
            )
            await log_scheduler_job_event(
                job_name=f"user_cron:{job_id}",
                outcome="failed" if delivery_outcome == "failed" else "succeeded",
                details={
                    "scheduled_job_id": job_id,
                    "action_type": action_type,
                    "delivery_outcome": delivery_outcome,
                    "policy_action": result.action.value,
                },
            )
            return

        if action_type == "run_workflow":
            await _run_scheduled_workflow(job)
            await scheduled_job_repository.record_run(job_id, outcome="succeeded")
            await log_scheduler_job_event(
                job_name=f"user_cron:{job_id}",
                outcome="succeeded",
                details={
                    "scheduled_job_id": job_id,
                    "action_type": action_type,
                },
            )
            return

        raise RuntimeError(f"Unsupported scheduled action '{action_type}'.")
    except ApprovalRequired as exc:
        await scheduled_job_repository.record_run(
            job_id,
            outcome="approval_required",
            approval_id=exc.approval_id,
        )
        await log_scheduler_job_event(
            job_name=f"user_cron:{job_id}",
            outcome="approval_required",
            details={
                "scheduled_job_id": job_id,
                "action_type": action_type,
                "approval_id": exc.approval_id,
                "tool_name": exc.tool_name,
            },
        )
    except Exception as exc:
        logger.exception("Scheduled job %s failed", job_id)
        safe_error = _safe_error_label(exc)
        await scheduled_job_repository.record_run(
            job_id,
            outcome="failed",
            error=safe_error,
        )
        await log_scheduler_job_event(
            job_name=f"user_cron:{job_id}",
            outcome="failed",
            details={
                "scheduled_job_id": job_id,
                "action_type": action_type,
                "error": safe_error,
            },
        )


async def _run_scheduled_workflow(job: dict[str, Any]) -> None:
    session_id = job.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise RuntimeError("Scheduled workflow jobs require a session_id.")

    action_spec = job.get("action_spec") or {}
    workflow_name = str(action_spec.get("workflow_name") or "")
    workflow = workflow_manager.get_workflow(workflow_name)
    if workflow is None or not workflow.enabled:
        raise RuntimeError(f"Workflow '{workflow_name}' is not available.")

    from src.agent.factory import get_tools

    tools = {tool.name: tool for tool in get_tools()}
    workflow_tool = tools.get(workflow.tool_name)
    if workflow_tool is None:
        raise RuntimeError(f"Workflow tool '{workflow.tool_name}' is not executable.")

    approval_mode = context_manager.get_context().approval_mode
    tokens = set_runtime_context(session_id, approval_mode)
    try:
        workflow_tool(**dict(action_spec.get("workflow_args") or {}))
    finally:
        reset_runtime_context(tokens)


scheduled_job_repository = ScheduledJobRepository()
