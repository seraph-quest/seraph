"""Persisted user-scheduled jobs for dynamic cron routines."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import and_, func, or_, select as sa_select, update
from sqlmodel import select, col

from config.settings import settings
from src.approval.exceptions import ApprovalRequired
from src.approval.runtime import (
    reset_runtime_context,
    scheduled_workflow_service_principal,
    set_runtime_context,
)
from src.audit.runtime import log_scheduler_job_event
from src.db.engine import get_session
from src.db.models import (
    CalendarReadConsent,
    Goal,
    GoogleServiceConnection,
    GovernedScheduleBinding,
    GovernedScheduleOccurrence,
    GuardianInboxDisposition,
    GuardianRoutine,
    GuardianRoutineVersion,
    MailLabelBinding,
    MailMessageBinding,
    MailReadConsent,
    MailWatchState,
    OperatorSession,
    ScheduledJob,
    ScheduledJobRun,
    WorkBoardInputArtifact,
    WorkBoardStatus,
    WorkBoardTask,
)
from src.db.session_refs import ensure_sessions_exist
from src.models.schemas import WSResponse
from src.observer.delivery import deliver_or_queue
from src.observer.manager import context_manager
from src.workflows.manager import workflow_manager
from src.work_board.contracts import WorkBoardInputArtifactCreate, WorkBoardOwner, WorkBoardTaskCreate
from src.work_board.input_artifacts import (
    _decode_and_validate_payload,
    _metadata_digest,
    _payload_path,
    _safe_file_bytes,
    prepare_input_artifact,
    revoke_unpublished_input_artifact,
    revoke_input_artifact,
)
from src.work_board.repository import BoardError, WorkBoardRepository
from src.goals.repository import deserialize_admission_budget

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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


def _mail_watch_quiet_reason(goal: Goal, *, now: datetime | None = None) -> str | None:
    """Return the existing Goal quiet-hours admission reason for Mail watches."""

    budget = deserialize_admission_budget(goal)
    if budget is None or budget.quiet_hours_start is None or budget.quiet_hours_end is None:
        return None
    current = _utc(now or _utc_now())
    try:
        local_hour = current.astimezone(ZoneInfo(str(budget.timezone))).hour
    except Exception:
        return "goal_budget_timezone_invalid"
    start = int(budget.quiet_hours_start)
    end = int(budget.quiet_hours_end)
    quiet = local_hour >= start or local_hour < end if start > end else start <= local_hour < end
    return "goal_quiet_hours" if quiet else None


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

    if normalized_target in {"source_watch", "run_source_watch"}:
        args = _parse_workflow_args_json(workflow_args_json)
        watch_id = str(args.get("watch_id") or "").strip()
        if not watch_id:
            raise ValueError("run_source_watch jobs require a watch_id.")
        return "run_source_watch", {"watch_id": watch_id}

    raise ValueError(
        "target_type must be one of: message, deliver_message, workflow, run_workflow, run_source_watch."
    )


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def _loads(payload: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _safe_error_label(exc: Exception) -> str:
    safe_code = getattr(exc, "safe_code", None)
    return str(safe_code)[:128] if isinstance(safe_code, str) and safe_code else type(exc).__name__


class _GovernedScheduleArtifactCleanupRequired(RuntimeError):
    """The committed task input could not be proven tombstoned."""

    safe_code = "governed_schedule_artifact_cleanup_required"

    def __init__(self, cause: BaseException | None = None) -> None:
        super().__init__(self.safe_code)
        if cause is not None:
            self.__cause__ = cause


class _ProcedureScheduleDeferred(RuntimeError):
    """A reviewed procedure schedule is currently ineligible without contact."""

    safe_code = "procedure_schedule_deferred"

    def __init__(self, reason_code: str, recovery_action: str = "wait_for_next_slot") -> None:
        self.reason_code = str(reason_code or self.safe_code)[:128]
        self.recovery_action = str(recovery_action or "wait_for_next_slot")[:128]
        super().__init__(self.reason_code)


class _ProcedureScheduleAuthorityFailure(RuntimeError):
    """A typed failure from the pre-publication authority callback."""

    safe_code = "procedure_schedule_publication_authority_stale"


class _ProcedureSchedulePublishedTaskInvalid(RuntimeError):
    """A canonical task exists but cannot be safely adopted for this slot."""

    safe_code = "procedure_schedule_published_task_invalid"


def _procedure_quiet_now(budget: Any, now: datetime) -> bool:
    start = getattr(budget, "quiet_hours_start", None)
    end = getattr(budget, "quiet_hours_end", None)
    if start is None or end is None:
        return False
    try:
        local_hour = _utc(now).astimezone(ZoneInfo(str(getattr(budget, "timezone", "UTC")))).hour
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise _ProcedureScheduleDeferred("procedure_goal_budget_timezone_invalid", "repair_goal_budget") from exc
    return local_hour >= int(start) or local_hour < int(end) if int(start) > int(end) else int(start) <= local_hour < int(end)


def _procedure_budget_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, str) and value.strip():
        try:
            return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except (TypeError, ValueError):
            return None
    return None


async def _load_governed_procedure_authority(
    db: Any,
    job: dict[str, Any],
    binding_id: str,
    *,
    now: datetime | None = None,
    slot_utc: datetime | None = None,
) -> tuple[GovernedScheduleBinding, Goal, WorkBoardInputArtifact, dict[str, Any], dict[str, Any]]:
    """Re-read the pinned routine/goal/budget before a scheduled task claim.

    This is the procedure counterpart to the existing Calendar authority
    loader.  It intentionally performs no model/provider work and does not
    reuse the strategist notification budget: a zero notification allowance
    still permits a governed task, while delivery remains separately gated.
    """

    from src.scheduler.governed_schedules import PROCEDURE_ACTION, normalize_cadence
    from src.goals.repository import deserialize_admission_budget
    from src.workflows.procedure_service import goal_admission_budget_snapshot

    observed = _utc(now or _utc_now())
    canonical_job = await db.get(ScheduledJob, str(job.get("id") or ""), populate_existing=True)
    if (
        canonical_job is None
        or not canonical_job.enabled
        or canonical_job.trigger_type != "governed"
        or canonical_job.action_type != PROCEDURE_ACTION
    ):
        raise RuntimeError("procedure_schedule_prerequisite_stale")
    try:
        action_spec = json.loads(canonical_job.action_spec_json or "{}")
        trigger_spec = json.loads(canonical_job.trigger_spec_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("procedure_schedule_binding_link_invalid") from exc
    if not isinstance(action_spec, dict) or action_spec.get("binding_id") != binding_id:
        raise RuntimeError("procedure_schedule_binding_link_invalid")
    binding = (
        await db.execute(
            sa_select(GovernedScheduleBinding)
            .where(
                GovernedScheduleBinding.binding_id == binding_id,
                GovernedScheduleBinding.scheduled_job_id == canonical_job.id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (
        binding is None
        or binding.action_type != PROCEDURE_ACTION
        or binding.capability_id != PROCEDURE_ACTION
        or binding.read_consent_id is not None
        or binding.consent_kind != "goal_budget"
    ):
        raise RuntimeError("procedure_schedule_binding_missing")
    try:
        cadence = normalize_cadence(trigger_spec)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise RuntimeError("procedure_schedule_trigger_invalid") from exc
    if cadence != {
        "kind": binding.cadence_kind,
        "timezone": binding.timezone,
        "daily_hour": binding.daily_hour,
        "daily_minute": binding.daily_minute,
    }:
        raise RuntimeError("procedure_schedule_trigger_binding_mismatch")
    if binding.state != "active" or _utc(binding.expires_at) <= observed:
        raise _ProcedureScheduleDeferred("procedure_schedule_prerequisite_stale", "review_schedule")

    goal = await db.get(Goal, binding.goal_id, populate_existing=True)
    session = await db.get(OperatorSession, binding.owner_session_id, populate_existing=True)
    goal_status = getattr(getattr(goal, "status", None), "value", getattr(goal, "status", None))
    if (
        goal is None
        or session is None
        or session.revoked_at is not None
        or _utc(session.idle_expires_at) <= observed
        or _utc(session.absolute_expires_at) <= observed
        or goal.owner_principal_id != binding.owner_principal_id
        or goal.owner_session_id != binding.owner_session_id
        or goal_status != "active"
        or int(goal.revision) != int(binding.goal_revision)
        or not bool(getattr(goal, "proactive_enabled", False))
    ):
        raise _ProcedureScheduleDeferred("procedure_goal_authority_stale", "review_goal_authority")
    budget = deserialize_admission_budget(goal)
    if budget is None or not bool(budget.reviewed_grant) or not str(budget.grant_id or "").strip():
        raise _ProcedureScheduleDeferred("procedure_goal_budget_missing_reviewed_grant", "review_goal_budget")
    try:
        snapshot = goal_admission_budget_snapshot(
            goal_id=str(goal.id), goal_revision=int(goal.revision), budget=budget
        )
    except (TypeError, ValueError) as exc:
        raise _ProcedureScheduleDeferred("procedure_goal_budget_invalid", "repair_goal_budget") from exc
    period_expires = _utc(budget.period_expires_at) if budget.period_expires_at is not None else None
    period_started = _utc(budget.period_started_at) if budget.period_started_at is not None else None
    if period_expires is None:
        raise _ProcedureScheduleDeferred("procedure_goal_budget_finite_expiry_required", "set_goal_budget_expiry")
    if period_started is not None and period_started > observed:
        raise _ProcedureScheduleDeferred("procedure_goal_budget_period_not_started", "wait_for_goal_budget_period")
    if period_expires <= observed:
        raise _ProcedureScheduleDeferred("procedure_goal_budget_period_expired", "review_goal_budget")
    if _procedure_quiet_now(budget, observed):
        raise _ProcedureScheduleDeferred("goal_quiet_hours", "wait_for_next_eligible_slot")

    # These are the exact fields emitted by the API schedule-v2 contract.  Do
    # not accept aliases or coerce JSON scalars: a string/bool here would make
    # a malformed action spec look like a reviewed grant.
    expected_budget = {
        "goal_budget_digest": snapshot.digest,
        "consent_digest": snapshot.digest,
        "goal_budget_grant_id": str(budget.grant_id),
        "goal_budget_max_outstanding_jobs": int(budget.max_outstanding_jobs),
        "goal_budget_max_attempts": int(budget.max_attempts),
        "goal_budget_max_runtime_seconds": int(budget.max_runtime_seconds),
    }
    for key, expected in expected_budget.items():
        observed_value = action_spec.get(key)
        if key in {
            "goal_budget_max_outstanding_jobs",
            "goal_budget_max_attempts",
            "goal_budget_max_runtime_seconds",
        }:
            valid = type(observed_value) is int and observed_value == expected
        else:
            valid = type(observed_value) is str and observed_value == expected
        if not valid:
            raise _ProcedureScheduleDeferred("procedure_goal_budget_changed", "refresh_goal_budget")
    action_period = _procedure_budget_timestamp(action_spec.get("goal_budget_period_expires_at"))
    if action_period is None or action_period != period_expires or type(action_spec.get("goal_budget_period_expires_at")) is not str:
        raise _ProcedureScheduleDeferred("procedure_goal_budget_changed", "refresh_goal_budget")
    if (
        str(action_spec.get("goal_id") or "") != str(binding.goal_id)
        or type(action_spec.get("goal_revision")) is not int
        or action_spec.get("goal_revision") < 1
        or action_spec.get("goal_revision") != int(binding.goal_revision)
        or str(action_spec.get("routine_id") or "") == ""
        or type(action_spec.get("version")) is not int
        or action_spec.get("version") < 1
    ):
        raise RuntimeError("procedure_schedule_action_binding_invalid")

    # The schedule action is an immutable reviewed selector.  Re-read the
    # owner-bound routine and version before reserving an occurrence so a
    # paused routine, rollback, revision advance, or package replacement
    # cannot silently execute under the old schedule key.  These checks use
    # exact persisted scalars; the scheduler never accepts a caller alias or
    # coerces a malformed JSON value into authority.
    action_routine_revision = action_spec.get("routine_revision")
    action_version_id = action_spec.get("version_id")
    action_template_id = action_spec.get("template_id")
    action_plan_digest = action_spec.get("plan_digest")
    action_source_proof_digest = action_spec.get("source_proof_digest")
    action_package_digest = action_spec.get("package_digest")
    if (
        type(action_routine_revision) is not int
        or action_routine_revision < 1
        or type(action_version_id) is not str
        or not action_version_id.strip()
        or type(action_template_id) is not str
        or not action_template_id.strip()
        or type(action_plan_digest) is not str
        or not action_plan_digest.strip()
        or type(action_source_proof_digest) is not str
        or not action_source_proof_digest.strip()
        or type(action_package_digest) is not str
        or not action_package_digest.strip()
    ):
        raise _ProcedureScheduleDeferred("procedure_schedule_proof_missing", "review_procedure")

    routine = (
        await db.execute(
            sa_select(GuardianRoutine)
            .where(
                GuardianRoutine.id == action_spec["routine_id"],
                GuardianRoutine.owner_principal_id == binding.owner_principal_id,
                GuardianRoutine.owner_session_id == binding.owner_session_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    version_row = (
        await db.execute(
            sa_select(GuardianRoutineVersion)
            .where(
                GuardianRoutineVersion.routine_id == action_spec["routine_id"],
                GuardianRoutineVersion.version == action_spec["version"],
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    provenance = _loads(getattr(version_row, "source_provenance_json", "{}")) if version_row is not None else {}
    if (
        routine is None
        or version_row is None
        or routine.state != "active"
        or type(routine.revision) is not int
        or routine.revision != action_routine_revision
        or type(routine.current_version) is not int
        or routine.current_version != action_spec["version"]
        or str(version_row.id) != action_version_id
        or str(version_row.installed_package_digest or "") != action_package_digest
        or not isinstance(provenance, dict)
        or provenance.get("schema_version") != 2
        or provenance.get("template_id") != action_template_id
        or provenance.get("plan_digest") != action_plan_digest
        or provenance.get("source_proof_digest") != action_source_proof_digest
    ):
        raise _ProcedureScheduleDeferred("procedure_schedule_proof_stale", "review_procedure")
    # The persisted version digest is necessary but not sufficient: package
    # lifecycle state can be revoked or paused independently of the routine
    # selector.  Reuse the existing owner-bound package readback rather than
    # treating a matching database digest as executable authority.
    try:
        from src.workflows.routines import routine_service

        package_readback = routine_service._package_readback(
            binding.owner_principal_id,
            binding.owner_session_id,
            str(action_spec["routine_id"]),
            int(action_spec["version"]),
            action_package_digest,
        )
    except Exception as exc:
        raise _ProcedureScheduleDeferred("procedure_package_readback_unavailable", "review_procedure") from exc
    if (
        not isinstance(package_readback, dict)
        or package_readback.get("status") != "active"
        or package_readback.get("digest") != action_package_digest
    ):
        raise _ProcedureScheduleDeferred("procedure_package_not_current", "review_procedure")

    artifact = await db.get(WorkBoardInputArtifact, binding.input_artifact_id, populate_existing=True)
    if (
        artifact is None
        or artifact.owner_principal_id != binding.owner_principal_id
        or artifact.owner_session_id != binding.owner_session_id
        or artifact.capability_id != "guardian-routine.v2"
        or artifact.goal_id != binding.goal_id
        or int(artifact.goal_revision) != int(binding.goal_revision)
        or artifact.payload_sha256 != binding.input_digest.removeprefix("sha256:")
        or artifact.bound_task_id is not None
        or artifact.state not in {"pending", "bound"}
        or artifact.expires_at is None
        or _utc(artifact.expires_at) <= observed
        or _utc(artifact.expires_at) < _utc(binding.expires_at)
        or not artifact.metadata_digest
    ):
        raise RuntimeError("procedure_schedule_input_artifact_invalid")
    try:
        if _metadata_digest(artifact) != artifact.metadata_digest:
            raise RuntimeError("artifact_metadata_digest_mismatch")
        payload_bytes = _safe_file_bytes(
            _payload_path(artifact), expected_digest=artifact.payload_sha256, expected_size=artifact.size_bytes
        )
        payload = _decode_and_validate_payload(artifact, payload_bytes)
    except Exception as exc:
        raise RuntimeError("procedure_schedule_input_artifact_invalid") from exc
    payload_version = payload.get("version")
    payload_goal_revision = payload.get("expected_goal_revision")
    if (
        payload.get("routine_id") != action_spec.get("routine_id")
        or type(payload_version) is not int
        or payload_version < 1
        or payload_version != action_spec.get("version")
        or payload.get("goal_id") != binding.goal_id
        or type(payload_goal_revision) is not int
        or payload_goal_revision < 1
        or payload_goal_revision != binding.goal_revision
        or payload.get("parameters") != (action_spec.get("parameters") or {})
    ):
        raise RuntimeError("procedure_schedule_input_binding_mismatch")

    outstanding_states = (WorkBoardStatus.todo, WorkBoardStatus.ready, WorkBoardStatus.running, WorkBoardStatus.review)
    outstanding_query = select(func.count(WorkBoardTask.task_id)).where(
        WorkBoardTask.owner_principal_id == binding.owner_principal_id,
        WorkBoardTask.owner_session_id == binding.owner_session_id,
        WorkBoardTask.goal_id == binding.goal_id,
        WorkBoardTask.goal_revision == binding.goal_revision,
        WorkBoardTask.capability_id == "guardian-routine.v2",
        WorkBoardTask.status.in_(outstanding_states),
    )
    # An exact same-slot replay is allowed to adopt its already-published
    # task.  Other slots, including a new slot for this binding, still count
    # every outstanding procedure task against the reviewed goal budget.
    if slot_utc is not None:
        normalized_slot = _utc(slot_utc)
        try:
            if getattr(db.get_bind().dialect, "name", "") == "sqlite":
                normalized_slot = normalized_slot.replace(tzinfo=None)
        except (AttributeError, RuntimeError):
            pass
        same_slot_task = (
            await db.execute(
                sa_select(GovernedScheduleOccurrence.work_board_task_id).where(
                    GovernedScheduleOccurrence.binding_id == binding.binding_id,
                    GovernedScheduleOccurrence.slot_utc == normalized_slot,
                    GovernedScheduleOccurrence.state.in_(("reserved", "running", "unknown")),
                    GovernedScheduleOccurrence.work_board_task_id.is_not(None),
                )
            )
        ).scalar_one_or_none()
        if same_slot_task is None:
            # A writer may have committed the canonical task and then lost the
            # result before linking it to the occurrence.  The occurrence is
            # quarantined as unknown, but an exact same-slot task is still
            # eligible for adoption; do not let the budget count it as a new
            # outstanding job and mint a second artifact.
            slot_key = normalized_slot.strftime("%Y%m%dT%H%M%SZ")
            same_slot_task = (
                await db.execute(
                    sa_select(WorkBoardTask.task_id)
                    .where(
                        WorkBoardTask.owner_principal_id == binding.owner_principal_id,
                        WorkBoardTask.owner_session_id == binding.owner_session_id,
                        WorkBoardTask.goal_id == binding.goal_id,
                        WorkBoardTask.goal_revision == binding.goal_revision,
                        WorkBoardTask.capability_id == "guardian-routine.v2",
                        WorkBoardTask.idempotency_scope == "guardian-routine-v2-schedule",
                        WorkBoardTask.idempotency_key == f"{binding.binding_id}:{slot_key}",
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
        if same_slot_task:
            outstanding_query = outstanding_query.where(WorkBoardTask.task_id != str(same_slot_task))
    outstanding = int((await db.execute(outstanding_query)).scalar_one() or 0)
    if outstanding >= int(budget.max_outstanding_jobs):
        raise _ProcedureScheduleDeferred("procedure_goal_outstanding_limit", "wait_for_outstanding_work")
    return binding, goal, artifact, payload, action_spec


def _reject_legacy_governed_mutation(action_type: str | None) -> None:
    """Keep the generic ScheduledJob CRUD surface from becoming authority."""
    if str(action_type or "").strip() in {
        "calendar.observe_due_events.v1",
        "guardian.run_procedure.v2",
        "gmail.scan_metadata.v1",
    }:
        raise RuntimeError("governed_schedule_controls_required")


async def _load_governed_authority(db, job: dict[str, Any], binding_id: str):
    """Read every mutable fence immediately before a side effect."""
    canonical_job = await db.get(
        ScheduledJob,
        str(job.get("id") or ""),
        populate_existing=True,
    )
    if (
        canonical_job is None
        or not canonical_job.enabled
        or canonical_job.trigger_type != "governed"
        or canonical_job.action_type != "calendar.observe_due_events.v1"
    ):
        raise RuntimeError("governed_schedule_prerequisite_stale")
    try:
        canonical_action_spec = json.loads(canonical_job.action_spec_json or "{}")
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        canonical_action_spec = {}
    if not isinstance(canonical_action_spec, dict) or canonical_action_spec.get("binding_id") != binding_id:
        raise RuntimeError("governed_schedule_binding_link_invalid")
    binding = (
        await db.execute(
            sa_select(GovernedScheduleBinding)
            .where(
                GovernedScheduleBinding.binding_id == binding_id,
                GovernedScheduleBinding.scheduled_job_id == canonical_job.id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if binding is None or binding.action_type != "calendar.observe_due_events.v1" or binding.capability_id != binding.action_type:
        raise RuntimeError("governed_schedule_binding_missing")
    try:
        from src.scheduler.governed_schedules import normalize_cadence

        trigger_cadence = normalize_cadence(json.loads(canonical_job.trigger_spec_json or "{}"))
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        raise RuntimeError("governed_schedule_trigger_invalid")
    if trigger_cadence != {
        "kind": binding.cadence_kind,
        "timezone": binding.timezone,
        "daily_hour": binding.daily_hour,
        "daily_minute": binding.daily_minute,
    }:
        raise RuntimeError("governed_schedule_trigger_binding_mismatch")
    now = _utc_now()
    if binding.state != "active" or _utc(binding.expires_at) <= now:
        raise RuntimeError("governed_schedule_prerequisite_stale")
    consent = (
        await db.execute(
            sa_select(CalendarReadConsent)
            .where(
                CalendarReadConsent.consent_id == binding.read_consent_id,
                CalendarReadConsent.owner_principal_id == binding.owner_principal_id,
                CalendarReadConsent.owner_session_id == binding.owner_session_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    connection = (
        await db.execute(
            sa_select(GoogleServiceConnection)
            .where(
                GoogleServiceConnection.connection_id == (consent.connection_id if consent else ""),
                GoogleServiceConnection.owner_principal_id == binding.owner_principal_id,
                GoogleServiceConnection.owner_session_id == binding.owner_session_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none() if consent is not None else None
    goal = await db.get(Goal, binding.goal_id, populate_existing=True)
    artifact = await db.get(
        WorkBoardInputArtifact,
        binding.input_artifact_id,
        populate_existing=True,
    )
    session = await db.get(OperatorSession, binding.owner_session_id, populate_existing=True)
    goal_status = getattr(getattr(goal, "status", None), "value", getattr(goal, "status", None))
    if (
        consent is None
        or connection is None
        or goal is None
        or artifact is None
        or session is None
        or session.revoked_at is not None
        or _utc(session.idle_expires_at) <= now
        or _utc(session.absolute_expires_at) <= now
        or consent.state != "active"
        or not bool(consent.allow_remote_model)
        or _utc(consent.expires_at) <= now
        or consent.revision != binding.consent_revision
        or consent.connection_revision != connection.revision
        or connection.state != "active"
        or goal.owner_principal_id != binding.owner_principal_id
        or goal.owner_session_id != binding.owner_session_id
        or goal_status != "active"
        or int(goal.revision) != int(binding.goal_revision)
        or artifact.owner_principal_id != binding.owner_principal_id
        or artifact.owner_session_id != binding.owner_session_id
        or artifact.capability_id != "calendar.observe_due_events.v1"
        or artifact.goal_id != binding.goal_id
        or int(artifact.goal_revision) != int(binding.goal_revision)
        or artifact.payload_sha256 != binding.input_digest.removeprefix("sha256:")
        or artifact.state != "pending"
        or artifact.bound_task_id is not None
        or artifact.expires_at is None
        or _utc(artifact.expires_at) <= now
        or not artifact.metadata_digest
    ):
        raise RuntimeError("governed_schedule_prerequisite_stale")
    # Metadata is not enough to admit a provider read.  Re-read the immutable
    # workspace JSON through the existing owner-bound artifact primitives and
    # validate its scheduler-only payload against the current consent and
    # connection revisions.  A changed file, descriptor, or private calendar
    # identity therefore fails closed before any external contact.
    try:
        if _metadata_digest(artifact) != artifact.metadata_digest:
            raise RuntimeError("artifact_metadata_digest_mismatch")
        payload = _safe_file_bytes(
            _payload_path(artifact),
            expected_digest=artifact.payload_sha256,
            expected_size=artifact.size_bytes,
        )
        typed_input = _decode_and_validate_payload(artifact, payload, allow_scheduler=True)
        if (
            typed_input.get("consent_id") != consent.consent_id
            or typed_input.get("connection_id") != connection.connection_id
            or typed_input.get("goal_id") != binding.goal_id
            or int(typed_input.get("goal_revision") or 0) != int(binding.goal_revision)
            or int(typed_input.get("max_events_per_scan") or 0) != 10
        ):
            raise RuntimeError("artifact_authority_binding_mismatch")
    except Exception as exc:
        raise RuntimeError("governed_schedule_input_artifact_invalid") from exc
    return binding, consent, connection, goal, artifact


async def _load_governed_mail_authority(db, job: dict[str, Any], binding_id: str):
    """Load one Mail metadata-watch authority tuple before any provider read."""

    from src.scheduler.governed_schedules import normalize_cadence

    canonical_job = await db.get(ScheduledJob, str(job.get("id") or ""), populate_existing=True)
    if (
        canonical_job is None
        or not canonical_job.enabled
        or canonical_job.trigger_type != "governed"
        or canonical_job.action_type != "gmail.scan_metadata.v1"
    ):
        raise RuntimeError("mail_watch_prerequisite_stale")
    try:
        action_spec = json.loads(canonical_job.action_spec_json or "{}")
        trigger = normalize_cadence(json.loads(canonical_job.trigger_spec_json or "{}"))
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError("mail_watch_binding_link_invalid") from exc
    if not isinstance(action_spec, dict) or action_spec.get("binding_id") != binding_id:
        raise RuntimeError("mail_watch_binding_link_invalid")
    binding = (
        await db.execute(
            sa_select(GovernedScheduleBinding)
            .where(
                GovernedScheduleBinding.binding_id == binding_id,
                GovernedScheduleBinding.scheduled_job_id == canonical_job.id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (
        binding is None
        or binding.action_type != "gmail.scan_metadata.v1"
        or binding.capability_id != "gmail.scan_metadata.v1"
        or binding.cadence_kind not in {"hourly", "6h"}
        or trigger != {
            "kind": binding.cadence_kind,
            "timezone": binding.timezone,
            "daily_hour": binding.daily_hour,
            "daily_minute": binding.daily_minute,
        }
    ):
        raise RuntimeError("mail_watch_binding_invalid")
    now = _utc_now()
    if binding.state != "active" or _utc(binding.expires_at) <= now:
        raise RuntimeError("mail_watch_prerequisite_stale")
    state = await db.get(MailWatchState, binding.binding_id, populate_existing=True)
    consent = (
        await db.execute(
            sa_select(MailReadConsent)
            .where(
                MailReadConsent.consent_id == binding.read_consent_id,
                MailReadConsent.owner_principal_id == binding.owner_principal_id,
                MailReadConsent.owner_session_id == binding.owner_session_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    connection = (
        await db.execute(
            sa_select(GoogleServiceConnection)
            .where(
                GoogleServiceConnection.connection_id == (consent.connection_id if consent else ""),
                GoogleServiceConnection.owner_principal_id == binding.owner_principal_id,
                GoogleServiceConnection.owner_session_id == binding.owner_session_id,
                GoogleServiceConnection.service == "gmail_readonly",
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none() if consent is not None else None
    goal = await db.get(Goal, binding.goal_id, populate_existing=True)
    artifact = await db.get(WorkBoardInputArtifact, binding.input_artifact_id, populate_existing=True)
    session = await db.get(OperatorSession, binding.owner_session_id, populate_existing=True)
    goal_status = getattr(getattr(goal, "status", None), "value", getattr(goal, "status", None))
    budget = deserialize_admission_budget(goal) if goal is not None else None
    if (
        state is None
        or state.binding_id != binding.binding_id
        or state.owner_principal_id != binding.owner_principal_id
        or state.owner_session_id != binding.owner_session_id
        or consent is None
        or connection is None
        or goal is None
        or artifact is None
        or session is None
        or session.revoked_at is not None
        or _utc(session.idle_expires_at) <= now
        or _utc(session.absolute_expires_at) <= now
        or connection.state != "active"
        or consent.state != "active"
        or not bool(consent.source_read_allowed)
        or _utc(consent.expires_at) <= now
        or int(connection.revision) != int(consent.connection_revision)
        or int(connection.revision) != int(state.connection_revision)
        or int(consent.source_revision) != int(binding.consent_revision)
        or int(consent.source_revision) != int(state.source_consent_revision)
        or consent.connection_id != connection.connection_id
        or consent.goal_id != binding.goal_id
        or int(consent.goal_revision) != int(binding.goal_revision)
        or goal.owner_principal_id != binding.owner_principal_id
        or goal.owner_session_id != binding.owner_session_id
        or goal_status != "active"
        or int(goal.revision) != int(binding.goal_revision)
        or not bool(goal.proactive_enabled)
        or (
            state.state == "coverage_blocked"
            and state.skipped_coverage_reason == "mail_seen_cursor_capacity_exceeded"
        )
        or budget is None
        or not bool(budget.reviewed_grant)
        or not budget.grant_id
        or budget.period_expires_at is not None and _utc(budget.period_expires_at) <= now
        or artifact.owner_principal_id != binding.owner_principal_id
        or artifact.owner_session_id != binding.owner_session_id
        or artifact.capability_id != "gmail.scan_metadata.v1"
        or artifact.goal_id != binding.goal_id
        or int(artifact.goal_revision) != int(binding.goal_revision)
        or artifact.payload_sha256 != binding.input_digest.removeprefix("sha256:")
        or artifact.state != "pending"
        or artifact.bound_task_id is not None
        or artifact.expires_at is None
        or _utc(artifact.expires_at) <= now
        or not artifact.metadata_digest
    ):
        raise RuntimeError("mail_watch_prerequisite_stale")
    try:
        if _metadata_digest(artifact) != artifact.metadata_digest:
            raise RuntimeError("mail_watch_artifact_metadata_mismatch")
        payload = _safe_file_bytes(_payload_path(artifact), expected_digest=artifact.payload_sha256, expected_size=artifact.size_bytes)
        typed = _decode_and_validate_payload(artifact, payload, allow_scheduler=True)
        if (
            typed.get("consent_id") != consent.consent_id
            or typed.get("connection_id") != connection.connection_id
            or typed.get("goal_id") != binding.goal_id
            or int(typed.get("goal_revision") or 0) != int(binding.goal_revision)
            or int(typed.get("source_consent_revision") or 0) != int(consent.source_revision)
            or int(typed.get("window_days") or 0) != 7
            or not isinstance(typed.get("label_ids"), list)
            or not 1 <= len(typed.get("label_ids")) <= 3
            or int(typed.get("max_messages") or 0) != min(int(typed.get("max_messages") or 0), int(consent.max_messages))
        ):
            raise RuntimeError("mail_watch_artifact_authority_mismatch")
    except Exception as exc:
        raise RuntimeError("mail_watch_input_artifact_invalid") from exc
    return binding, state, consent, connection, goal, artifact, typed


async def _run_governed_calendar_observation(
    job: dict[str, Any],
    *,
    scheduled_slot_utc: datetime | None,
    scheduled_run_id: str | None = None,
) -> dict[str, Any]:
    """Run one bounded metadata observation through the durable occurrence fence."""
    from src.integrations.google_calendar import GoogleCalendarReadonlyAdapter, digest, persist_calendar_event_binding
    from src.scheduler.governed_schedules import (
        claim_occurrence,
        reserve_occurrence,
        settle_occurrence,
        write_server_cleanup_proof,
    )
    from src.vault import decrypt

    if scheduled_slot_utc is None:
        raise RuntimeError("governed_schedule_slot_unavailable")
    action_spec = job.get("action_spec") if isinstance(job.get("action_spec"), dict) else {}
    binding_id = str(action_spec.get("binding_id") or "").strip()
    if not binding_id:
        raise RuntimeError("governed_schedule_binding_missing")
    occurrence_id: str | None = None
    claim_token: str | None = None
    claim_fence: int | None = None
    occurrence_run_id: str | None = None
    owner_principal_id = ""
    owner_session_id = ""
    adapter: Any | None = None

    async def _transport_quiescence() -> dict[str, Any] | None:
        """Read only the adapter's explicit, server-owned lifecycle receipt."""

        if adapter is None:
            # The fixed observer has not constructed an adapter, and no
            # provider boundary exists before that point.  This is a distinct
            # server-known no-contact termination; it does not claim that an
            # HTTPX client was opened and closed.
            return {
                "status": "verified",
                "active_operations": 0,
                "unsettled_operations": 0,
                "requests_started": 0,
                "requests_settled": 0,
            }
        probe = getattr(adapter, "transport_quiescence", None)
        if not callable(probe):
            return None
        try:
            receipt = probe()
            if inspect.isawaitable(receipt):
                receipt = await receipt
        except BaseException:
            # A failed/abandoned lifecycle is precisely the condition for
            # which the occurrence must remain quarantined.
            return None
        if not isinstance(receipt, dict):
            return None
        return dict(receipt)

    async def _persist_failure(exc: BaseException) -> None:
        """Settle an owned failure, releasing the lane only with proof."""

        if not occurrence_id or not claim_token or claim_fence is None:
            return
        quiescence = await _transport_quiescence()
        cleanup_required = isinstance(exc, _GovernedScheduleArtifactCleanupRequired)
        if quiescence is not None and scheduled_run_id and not cleanup_required:
            try:
                async with get_session() as cleanup_db:
                    await write_server_cleanup_proof(
                        cleanup_db,
                        occurrence_id=occurrence_id,
                        scheduled_run_id=scheduled_run_id,
                        claim_token=claim_token,
                        fencing_token=claim_fence,
                        transport_quiescence=quiescence,
                        transport_origin=(
                            "server_no_contact_before_adapter"
                            if adapter is None
                            else "adapter_quiescence"
                        ),
                        failure_code=_safe_error_label(exc) if isinstance(exc, Exception) else type(exc).__name__,
                        owner_principal_id=owner_principal_id or None,
                        owner_session_id=owner_session_id or None,
                    )
                return
            except Exception:
                # A proof writer failure must not make the failure look
                # settled.  The fallback below retains unknown quarantine.
                logger.exception("Could not persist governed cleanup proof")
        try:
            async with get_session() as recovery_db:
                current = (
                    await recovery_db.execute(
                        sa_select(GovernedScheduleOccurrence).where(
                            GovernedScheduleOccurrence.occurrence_id == occurrence_id,
                            GovernedScheduleOccurrence.state == "running",
                            GovernedScheduleOccurrence.claim_token == claim_token,
                            GovernedScheduleOccurrence.fencing_token == claim_fence,
                        )
                    )
                ).scalar_one_or_none()
                if current is not None:
                    await settle_occurrence(
                        recovery_db,
                        current,
                        state="unknown",
                        job_id=scheduled_run_id,
                        failure_code=_safe_error_label(exc) if isinstance(exc, Exception) else type(exc).__name__,
                        recovery_action="reconcile_external_effect",
                        claim_token=claim_token,
                        fencing_token=claim_fence,
                    )
        except Exception:
            logger.exception("Could not persist governed occurrence recovery receipt")

    try:
        async with get_session() as db:
            binding, consent, connection, _goal, _artifact = await _load_governed_authority(db, job, binding_id)
            occurrence, replay = await reserve_occurrence(db, binding, slot_utc=scheduled_slot_utc)
            occurrence_id = occurrence.occurrence_id
            if replay:
                if occurrence.state in {"succeeded", "blocked", "cancelled", "coalesced"}:
                    return {"status": occurrence.state, "occurrence_id": occurrence.occurrence_id, "replayed": True, "event_count": 0}
                if occurrence.state == "unknown":
                    raise RuntimeError("governed_occurrence_requires_reconciliation")
            await claim_occurrence(db, occurrence)
            claim_token = occurrence.claim_token
            claim_fence = occurrence.fencing_token
            if scheduled_run_id:
                # Bind recovery to the canonical scheduler run before any
                # provider contact.  Reconciliation later requires a proof
                # written on this exact server-owned row.
                occurrence.durable_job_id = scheduled_run_id
                run_row = await db.get(ScheduledJobRun, scheduled_run_id)
                if run_row is None or run_row.scheduled_job_id != str(job.get("id") or ""):
                    raise RuntimeError("governed_schedule_run_binding_invalid")
                run_metadata = _loads(run_row.metadata_json or "{}")
                run_metadata.update(
                    {
                        "governed_occurrence_id": occurrence.occurrence_id,
                        "governed_binding_id": binding.binding_id,
                        "governed_claim_fence": claim_fence,
                    }
                )
                run_row.metadata_json = _dumps(run_metadata)
                await db.flush()
            owner_principal_id = binding.owner_principal_id
            owner_session_id = binding.owner_session_id
            # Capture the immutable read fence before any later populate-existing
            # refresh.  Comparisons in the publication callback must never use
            # ORM instances that a fresh query could mutate in place.
            expected_binding_id = str(binding.binding_id)
            expected_binding_revision = int(binding.binding_revision)
            expected_consent_revision = int(consent.revision)
            expected_connection_revision = int(connection.revision)
            # Leave the claim transaction cleanly before any local parsing or
            # decryption.  A malformed encrypted identity is a known
            # no-contact failure, but the durable occurrence/run must survive
            # long enough for the server-owned cleanup receipt to bind it.
            calendar_id_ciphertext = consent.calendar_id
            allowed_fields_json = consent.allowed_fields_json
            window_minutes_raw = consent.window_minutes
            max_events_raw = consent.max_events

        calendar_id = decrypt(calendar_id_ciphertext)
        allowed_fields = set(json.loads(allowed_fields_json or "[]"))
        window_minutes = max(1, min(int(window_minutes_raw), 24 * 60))
        max_events = max(1, min(int(max_events_raw), 10))

        async def authority_check() -> None:
            async with get_session() as check_db:
                current_binding, current_consent, current_connection, _goal, _artifact = await _load_governed_authority(
                    check_db,
                    job,
                    binding_id,
                )
                if (
                    current_binding.binding_id != expected_binding_id
                    or int(current_binding.binding_revision) != expected_binding_revision
                    or current_consent.revision != expected_consent_revision
                    or current_connection.revision != expected_connection_revision
                ):
                    raise RuntimeError("governed_schedule_authority_fence_stale")
                current = (
                    await check_db.execute(
                        sa_select(GovernedScheduleOccurrence)
                        .where(
                            GovernedScheduleOccurrence.occurrence_id == occurrence_id,
                            GovernedScheduleOccurrence.binding_id == current_binding.binding_id,
                            GovernedScheduleOccurrence.binding_revision == expected_binding_revision,
                            GovernedScheduleOccurrence.durable_job_id == scheduled_run_id,
                        )
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if (
                    current is None
                    or current.state != "running"
                    or current.claim_token != claim_token
                    or current.fencing_token != claim_fence
                ):
                    raise RuntimeError("governed_occurrence_fence_stale")

        async def publication_authority_check(publication_db) -> None:
            """Recheck the old read fence inside the task writer transaction."""

            current_binding, current_consent, current_connection, _goal, _scheduler_artifact = (
                await _load_governed_authority(publication_db, job, binding_id)
            )
            if (
                current_binding.binding_id != expected_binding_id
                or int(current_binding.binding_revision) != expected_binding_revision
                or current_consent.revision != expected_consent_revision
                or current_connection.revision != expected_connection_revision
            ):
                raise RuntimeError("governed_schedule_publication_fence_stale")
            current_occurrence = (
                await publication_db.execute(
                    sa_select(GovernedScheduleOccurrence)
                    .where(
                        GovernedScheduleOccurrence.occurrence_id == occurrence_id,
                        GovernedScheduleOccurrence.binding_id == expected_binding_id,
                        GovernedScheduleOccurrence.binding_revision == expected_binding_revision,
                        GovernedScheduleOccurrence.state == "running",
                        GovernedScheduleOccurrence.claim_token == claim_token,
                        GovernedScheduleOccurrence.fencing_token == claim_fence,
                    )
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if (
                current_occurrence is None
                or (
                    scheduled_run_id is not None
                    and current_occurrence.durable_job_id != scheduled_run_id
                )
            ):
                raise RuntimeError("governed_schedule_publication_fence_stale")
            # The artifact was committed before this callback. It must still be
            # the unbound pending row that this observation reserved; a task
            # or terminal transition means another writer won the fence.
            publication_artifact = await publication_db.get(
                WorkBoardInputArtifact,
                publication_artifact_id,
                populate_existing=True,
            )
            if (
                publication_artifact is None
                or publication_artifact.owner_principal_id != owner_principal_id
                or publication_artifact.owner_session_id != owner_session_id
                or publication_artifact.capability_id != "calendar.meeting-prep.v1"
                or publication_artifact.state != "pending"
                or publication_artifact.bound_task_id is not None
            ):
                raise RuntimeError("governed_schedule_publication_artifact_stale")

        async def revoke_orphan_artifact(db, artifact_id: str) -> None:
            """Tombstone a committed preparation input if task publication fenced out."""

            owner = WorkBoardOwner(
                principal_id=owner_principal_id,
                session_id=owner_session_id,
            )
            try:
                # The repository callback may have left a clean writer
                # transaction open.  Roll it back before using a fresh session
                # so the tombstone has its own durable commit boundary and
                # does not get erased by the caller's exception rollback.
                await db.rollback()
                async with get_session() as cleanup_db:
                    current_artifact = await cleanup_db.get(
                        WorkBoardInputArtifact,
                        artifact_id,
                        populate_existing=True,
                    )
                    if current_artifact is None:
                        raise RuntimeError("orphan_artifact_missing")
                    if (
                        current_artifact.owner_principal_id != owner.principal_id
                        or current_artifact.owner_session_id != owner.session_id
                        or current_artifact.capability_id != "calendar.meeting-prep.v1"
                    ):
                        raise RuntimeError("orphan_artifact_owner_or_capability_mismatch")
                    # An idempotent replay may have won the publication race
                    # before the callback failed.  Preserve that bound task;
                    # only an exact pending/unbound row is an orphan.
                    if current_artifact.bound_task_id is not None:
                        return
                    if current_artifact.state != "pending":
                        if current_artifact.state != "revoked":
                            raise RuntimeError("orphan_artifact_state_mismatch")
                    else:
                        await revoke_input_artifact(
                            cleanup_db,
                            owner,
                            artifact_id=artifact_id,
                        )
                    verified = await cleanup_db.get(
                        WorkBoardInputArtifact,
                        artifact_id,
                        populate_existing=True,
                    )
                    if (
                        verified is None
                        or verified.owner_principal_id != owner.principal_id
                        or verified.owner_session_id != owner.session_id
                        or verified.state != "revoked"
                        or verified.bound_task_id is not None
                    ):
                        raise RuntimeError("orphan_artifact_tombstone_unverified")
                    payload_path = _payload_path(verified)
                    if payload_path.exists() or payload_path.is_symlink():
                        raise RuntimeError("orphan_artifact_payload_present")
            except _GovernedScheduleArtifactCleanupRequired:
                raise
            except BaseException as cleanup_exc:
                logger.exception("Could not tombstone orphan Calendar preparation artifact %s", artifact_id)
                raise _GovernedScheduleArtifactCleanupRequired(cleanup_exc) from cleanup_exc

        adapter = GoogleCalendarReadonlyAdapter(
            connection,
            owner_principal_id=owner_principal_id,
            authority_check=authority_check,
        )
        now = _utc_now()
        snapshots, revision = await adapter.list_events(
            calendar_id,
            time_min=now,
            time_max=now + timedelta(minutes=window_minutes),
            allowed_fields=allowed_fields,
            max_events=max_events,
        )
        task_ids: list[str] = []
        owner = WorkBoardOwner(principal_id=owner_principal_id, session_id=owner_session_id)
        async with get_session() as db:
            current_binding, current_consent, current_connection, _goal, _artifact = await _load_governed_authority(db, job, binding_id)
            if (
                current_binding.binding_id != expected_binding_id
                or int(current_binding.binding_revision) != expected_binding_revision
                or current_consent.revision != expected_consent_revision
                or current_connection.revision != expected_connection_revision
            ):
                raise RuntimeError("governed_schedule_publication_fence_stale")
            current_occurrence = (
                await db.execute(
                    sa_select(GovernedScheduleOccurrence)
                    .where(
                        GovernedScheduleOccurrence.occurrence_id == occurrence_id,
                        GovernedScheduleOccurrence.binding_id == expected_binding_id,
                        GovernedScheduleOccurrence.binding_revision == expected_binding_revision,
                        GovernedScheduleOccurrence.state == "running",
                        GovernedScheduleOccurrence.claim_token == claim_token,
                        GovernedScheduleOccurrence.fencing_token == claim_fence,
                        GovernedScheduleOccurrence.durable_job_id == scheduled_run_id,
                    )
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if current_occurrence is None:
                raise RuntimeError("governed_occurrence_fence_stale")
            for snapshot in snapshots[:10]:
                event_binding = await persist_calendar_event_binding(
                    db,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                    connection=current_connection,
                    consent=current_consent,
                    snapshot=snapshot,
                )
                observation_key = "calendar-observation:" + digest(
                    {
                        "owner_principal_id": owner_principal_id,
                        "binding_id": current_binding.binding_id,
                        "binding_revision": int(current_binding.binding_revision),
                        "event_key": event_binding.event_key,
                        "event_revision": event_binding.event_revision,
                        "goal_revision": int(current_binding.goal_revision),
                        "action_digest": current_binding.action_digest,
                    }
                )
                typed_input = {
                    "schema_version": 1,
                    "consent_id": current_consent.consent_id,
                    "event_binding_id": event_binding.event_binding_id,
                    "expected_event_binding_revision": int(event_binding.revision),
                    "expected_consent_revision": int(current_consent.revision),
                    "expected_connection_revision": int(current_connection.revision),
                    "event_revision": event_binding.event_revision,
                    "calendar_list_revision": event_binding.calendar_list_revision,
                    "goal_id": current_binding.goal_id,
                    "goal_revision": int(current_binding.goal_revision),
                    "purpose": "bounded preparation request",
                }
                metadata = await prepare_input_artifact(
                    db,
                    owner,
                    WorkBoardInputArtifactCreate(
                        schema_version=1,
                        capability_id="calendar.meeting-prep.v1",
                        goal_id=current_binding.goal_id,
                        goal_revision=int(current_binding.goal_revision),
                        input=typed_input,
                        idempotency_key=observation_key,
                    ),
                )
                summary = adapter._scrub(snapshot.fields.get("summary") or "calendar event")
                if not isinstance(summary, str):
                    summary = "calendar event"
                publication_artifact_id = metadata.artifact_id
                try:
                    mutation = await WorkBoardRepository().create_task(
                        db,
                        owner,
                        WorkBoardTaskCreate(
                            title=f"Prepare for {summary}"[:200],
                            body="Calendar observation selected a bounded event for operator review.",
                            goal_id=current_binding.goal_id,
                            goal_revision=int(current_binding.goal_revision),
                            status="todo",
                            capability_id="calendar.meeting-prep.v1",
                            input_artifact_id=metadata.artifact_id,
                            priority=40,
                            idempotency_scope="calendar-observation",
                            idempotency_key=observation_key,
                        ),
                        origin_session_id=owner_session_id,
                        publication_authority_check=publication_authority_check,
                    )
                except (Exception, asyncio.CancelledError):
                    await revoke_orphan_artifact(db, metadata.artifact_id)
                    raise
                task_ids.append(mutation.task.task_id)
            await settle_occurrence(
                db,
                current_occurrence,
                state="succeeded",
                job_id=scheduled_run_id,
                claim_token=claim_token,
                fencing_token=claim_fence,
            )
        return {
            "status": "succeeded",
            "occurrence_id": occurrence_id,
            "replayed": False,
            "event_count": len(snapshots[:10]),
            "task_ids": task_ids[:10],
            "calendar_list_revision": revision.digest,
        }
    except (Exception, asyncio.CancelledError) as exc:
        await _persist_failure(exc)
        raise


async def _run_governed_procedure_schedule(
    job: dict[str, Any],
    *,
    scheduled_slot_utc: datetime | None,
    scheduled_run_id: str | None = None,
) -> dict[str, Any]:
    """Admit one pinned v2 procedure occurrence into the existing Work Board.

    The scheduler owns the slot/lease and creates exactly one fresh parent task
    artifact.  The normal Work Board dispatcher remains the execution owner;
    no second queue or scheduler is introduced.  The occurrence stays
    running until a later governed slot observes the task's terminal state,
    which preserves the shared at-most-one-outstanding rule across restarts.
    """

    from src.scheduler.governed_schedules import (
        PROCEDURE_ACTION,
        claim_occurrence,
        reserve_occurrence,
        settle_occurrence,
    )

    if scheduled_slot_utc is None:
        raise RuntimeError("governed_schedule_slot_unavailable")
    action_spec = job.get("action_spec") if isinstance(job.get("action_spec"), dict) else {}
    binding_id = str(action_spec.get("binding_id") or "").strip()
    if not binding_id:
        raise RuntimeError("procedure_schedule_binding_missing")
    occurrence_id: str | None = None
    claim_token: str | None = None
    claim_fence: int | None = None
    occurrence_run_id: str | None = None
    task_id: str | None = None
    owner: WorkBoardOwner | None = None
    fresh_artifact_id: str | None = None
    fresh_artifact_revision: int | None = None
    artifact_preparation_started = False
    publication_started = False

    async def _settle_failure(
        exc: BaseException,
        *,
        state: str,
        recovery_action: str,
    ) -> None:
        if not occurrence_id or not claim_token or claim_fence is None:
            return
        try:
            async with get_session() as recovery_db:
                current = await recovery_db.get(GovernedScheduleOccurrence, occurrence_id, populate_existing=True)
                if (
                    current is not None
                    and current.state == "running"
                    and current.claim_token == claim_token
                    and int(current.fencing_token) == int(claim_fence)
                ):
                    await settle_occurrence(
                        recovery_db,
                        current,
                        state=state,
                        task_id=task_id,
                        job_id=scheduled_run_id,
                        failure_code=_safe_error_label(exc) if isinstance(exc, Exception) else type(exc).__name__,
                        recovery_action=recovery_action,
                        claim_token=claim_token,
                        fencing_token=claim_fence,
                    )
                    if fresh_artifact_id:
                        metadata = _loads(current.metadata_json or "{}")
                        metadata["input_artifact_id"] = fresh_artifact_id
                        current.metadata_json = _dumps(metadata)
                        await recovery_db.flush()
        except Exception:
            logger.exception("Could not settle governed procedure occurrence %s", occurrence_id)

    async def _cleanup_unpublished_artifact() -> str:
        """Revoke only when the exact artifact is still unbound and unpublished."""

        if owner is None or not fresh_artifact_id or fresh_artifact_revision is None:
            return "unknown"

        async def publication_guard(cleanup_db: Any, _row: WorkBoardInputArtifact) -> bool:
            task_ref = (
                await cleanup_db.execute(
                    sa_select(WorkBoardTask.task_id)
                    .where(WorkBoardTask.input_artifact_id == fresh_artifact_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            binding_ref = (
                await cleanup_db.execute(
                    sa_select(GovernedScheduleBinding.binding_id)
                    .where(GovernedScheduleBinding.input_artifact_id == fresh_artifact_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            occurrence_rows = (
                await cleanup_db.execute(
                    sa_select(GovernedScheduleOccurrence.metadata_json)
                    .where(GovernedScheduleOccurrence.metadata_json.like(f"%{fresh_artifact_id}%"))
                    .limit(8)
                )
            ).scalars().all()
            occurrence_ref = False
            for raw_metadata in occurrence_rows:
                metadata = _loads(raw_metadata or "")
                if metadata.get("input_artifact_id") == fresh_artifact_id:
                    occurrence_ref = True
                    break
            return task_ref is None and binding_ref is None and not occurrence_ref

        try:
            async with get_session() as cleanup_db:
                await revoke_unpublished_input_artifact(
                    cleanup_db,
                    owner,
                    artifact_id=fresh_artifact_id,
                    expected_revision=fresh_artifact_revision,
                    publication_guard=publication_guard,
                )
            return "revoked"
        except BoardError as exc:
            if exc.code == "input_artifact_publication_protected":
                return "protected"
            return "unknown"
        except Exception:
            logger.exception("Could not reconcile unpublished procedure artifact %s", fresh_artifact_id)
            return "unknown"

    async def _adopt_persisted_task(
        db: Any,
        occurrence: GovernedScheduleOccurrence,
        *,
        occurrence_key: str,
        occurrence_run_id_value: str | None,
    ) -> bool:
        """Adopt a task committed before an occurrence writer lost its result."""

        nonlocal task_id, claim_token, claim_fence
        candidate = (
            await db.execute(
                sa_select(WorkBoardTask)
                .where(
                    WorkBoardTask.owner_principal_id == owner.principal_id,
                    WorkBoardTask.owner_session_id == owner.session_id,
                    WorkBoardTask.goal_id == binding.goal_id,
                    WorkBoardTask.goal_revision == int(binding.goal_revision),
                    WorkBoardTask.capability_id == "guardian-routine.v2",
                    WorkBoardTask.idempotency_scope == "guardian-routine-v2-schedule",
                    WorkBoardTask.idempotency_key == occurrence_key,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if candidate is None:
            return False
        candidate_artifact = await db.get(
            WorkBoardInputArtifact,
            candidate.input_artifact_id,
            populate_existing=True,
        ) if candidate.input_artifact_id else None
        if (
            candidate_artifact is None
            or candidate_artifact.owner_principal_id != owner.principal_id
            or candidate_artifact.owner_session_id != owner.session_id
            or candidate_artifact.capability_id != "guardian-routine.v2"
            or candidate_artifact.goal_id != binding.goal_id
            or int(candidate_artifact.goal_revision) != int(binding.goal_revision)
            or candidate_artifact.bound_task_id != candidate.task_id
            or candidate_artifact.state not in {"bound", "consumed"}
            or not candidate_artifact.metadata_digest
        ):
            raise _ProcedureSchedulePublishedTaskInvalid()
        task_id = candidate.task_id
        metadata = {
            "status": "queued",
            "task_id": candidate.task_id,
            "capability_id": "guardian-routine.v2",
            "routine_id": canonical_action.get("routine_id"),
            "version": canonical_action.get("version"),
            "input_artifact_id": candidate.input_artifact_id,
            "memory_status": "no_learning",
            "reconciled_publication": True,
        }
        if occurrence.state == "unknown":
            # Unknown is a quarantine state.  A canonical task plus its bound
            # artifact is the only proof that permits this exact slot to be
            # reopened; advance the fence so the writer that lost its commit
            # result cannot later mutate the recovered occurrence.
            if not occurrence.claim_token:
                raise _ProcedureSchedulePublishedTaskInvalid()
            next_fence = int(occurrence.fencing_token) + 1
            result = await db.execute(
                update(GovernedScheduleOccurrence)
                .where(
                    GovernedScheduleOccurrence.occurrence_id == occurrence.occurrence_id,
                    GovernedScheduleOccurrence.state == "unknown",
                    GovernedScheduleOccurrence.claim_token == occurrence.claim_token,
                    GovernedScheduleOccurrence.fencing_token == int(occurrence.fencing_token),
                )
                .values(
                    state="running",
                    work_board_task_id=candidate.task_id,
                    durable_job_id=occurrence_run_id_value or occurrence.durable_job_id,
                    fencing_token=next_fence,
                    lease_expires_at=_utc_now() + timedelta(minutes=5),
                    metadata_json=_dumps(metadata),
                    updated_at=_utc_now(),
                )
            )
            if int(result.rowcount or 0) != 1:
                raise _ProcedureSchedulePublishedTaskInvalid()
            await db.refresh(occurrence)
            claim_token = occurrence.claim_token
            claim_fence = occurrence.fencing_token
        else:
            occurrence.work_board_task_id = candidate.task_id
            occurrence.metadata_json = _dumps(metadata)
            await db.flush()
        return True

    try:
        async with get_session() as db:
            binding, _goal, source_artifact, source_payload, canonical_action = await _load_governed_procedure_authority(
                db, job, binding_id, slot_utc=scheduled_slot_utc
            )
            occurrence, replay = await reserve_occurrence(db, binding, slot_utc=scheduled_slot_utc)
            occurrence_id = occurrence.occurrence_id
            occurrence_run_id = occurrence.durable_job_id
            if replay:
                if occurrence.state in {"succeeded", "blocked", "cancelled", "coalesced"}:
                    return {
                        "status": occurrence.state,
                        "occurrence_id": occurrence.occurrence_id,
                        "task_id": occurrence.work_board_task_id,
                        "replayed": True,
                    }
                if occurrence.state == "running":
                    # A replay must adopt the exact fenced occurrence.  A
                    # worker can crash after claiming and before publishing
                    # its board task; claiming it again would either fail or
                    # create a second authority path.
                    claim_token = occurrence.claim_token
                    claim_fence = occurrence.fencing_token
                    occurrence_run_id = occurrence.durable_job_id
                    if occurrence.work_board_task_id:
                        return {
                            "status": "queued",
                            "occurrence_id": occurrence.occurrence_id,
                            "task_id": occurrence.work_board_task_id,
                            "replayed": True,
                            "memory_status": "no_learning",
                        }
                elif occurrence.state == "unknown":
                    # Keep the quarantine until an exact persisted task can
                    # prove that publication committed.  The adoption helper
                    # below advances the fence only after that proof.
                    claim_token = occurrence.claim_token
                    claim_fence = occurrence.fencing_token
            if occurrence.state == "reserved":
                await claim_occurrence(db, occurrence)
                claim_token = occurrence.claim_token
                claim_fence = occurrence.fencing_token
            elif occurrence.state not in {"running", "unknown"}:
                raise RuntimeError("governed_occurrence_not_claimable")
            if not claim_token:
                raise RuntimeError("procedure_schedule_claim_missing")
            if scheduled_run_id:
                if occurrence_run_id is None:
                    occurrence.durable_job_id = scheduled_run_id
                    occurrence_run_id = scheduled_run_id
                if not replay:
                    run_row = await db.get(ScheduledJobRun, scheduled_run_id)
                    if run_row is None or run_row.scheduled_job_id != str(job.get("id") or ""):
                        raise RuntimeError("governed_schedule_run_binding_invalid")
                    run_metadata = _loads(run_row.metadata_json or "{}")
                    run_metadata.update(
                        {
                            "governed_occurrence_id": occurrence.occurrence_id,
                            "governed_binding_id": binding.binding_id,
                            "governed_claim_fence": claim_fence,
                        }
                    )
                    run_row.metadata_json = _dumps(run_metadata)
            owner = WorkBoardOwner(
                principal_id=binding.owner_principal_id,
                session_id=binding.owner_session_id,
            )
            # The scheduled source artifact is immutable reviewed invocation
            # data.  Each occurrence gets a fresh executable artifact and a
            # deterministic occurrence UUID, so a consumed/expired prior task
            # can never become authority for a later run.
            slot_key = _utc(scheduled_slot_utc).strftime("%Y%m%dT%H%M%SZ")
            occurrence_key = f"{binding.binding_id}:{slot_key}"
            if replay and occurrence.state in {"running", "unknown"} and not occurrence.work_board_task_id:
                adopted = await _adopt_persisted_task(
                    db,
                    occurrence,
                    occurrence_key=occurrence_key,
                    occurrence_run_id_value=occurrence_run_id,
                )
                if adopted:
                    return {
                        "status": "queued",
                        "occurrence_id": occurrence.occurrence_id,
                        "task_id": task_id,
                        "replayed": True,
                        "memory_status": "no_learning",
                    }
                if occurrence.state == "unknown":
                    raise RuntimeError("governed_occurrence_requires_reconciliation")
            occurrence_payload = dict(source_payload)
            occurrence_payload["invocation_uuid"] = f"schedule:{occurrence_key}"

            artifact_preparation_started = True
        async with get_session() as artifact_db:
            fresh = await prepare_input_artifact(
                artifact_db,
                owner,
                WorkBoardInputArtifactCreate(
                    schema_version=1,
                    capability_id="guardian-routine.v2",
                    goal_id=binding.goal_id,
                    goal_revision=int(binding.goal_revision),
                    input=occurrence_payload,
                    idempotency_key=f"procedure-occurrence:{occurrence_key}",
                ),
            )
            fresh_artifact_id = fresh.artifact_id
            fresh_artifact_revision = int(fresh.revision)

        repository = WorkBoardRepository()

        async def publication_authority_check(check_db: Any) -> None:
            try:
                current_binding, _current_goal, _current_artifact, _current_payload, _current_action = (
                    await _load_governed_procedure_authority(
                        check_db, job, binding_id, slot_utc=scheduled_slot_utc
                    )
                )
                if current_binding.binding_id != binding.binding_id or int(current_binding.binding_revision) != int(binding.binding_revision):
                    raise RuntimeError("procedure_schedule_publication_fence_stale")
                current_occurrence = (
                    await check_db.execute(
                        sa_select(GovernedScheduleOccurrence).where(
                            GovernedScheduleOccurrence.occurrence_id == occurrence_id,
                            GovernedScheduleOccurrence.binding_id == binding.binding_id,
                            GovernedScheduleOccurrence.binding_revision == int(binding.binding_revision),
                            GovernedScheduleOccurrence.state == "running",
                            GovernedScheduleOccurrence.claim_token == claim_token,
                            GovernedScheduleOccurrence.fencing_token == claim_fence,
                            GovernedScheduleOccurrence.durable_job_id == occurrence_run_id,
                        )
                    )
                ).scalar_one_or_none()
                if current_occurrence is None:
                    raise RuntimeError("procedure_schedule_occurrence_fence_stale")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise _ProcedureScheduleAuthorityFailure() from exc

        publication_started = True
        async with get_session() as db:
            mutation = await repository.create_task(
                db,
                owner,
                WorkBoardTaskCreate(
                    title=f"Scheduled procedure: {canonical_action.get('routine_id') or 'reviewed routine'}",
                    body="A reviewed procedure occurrence is queued for the operator Work Board.",
                    goal_id=binding.goal_id,
                    goal_revision=int(binding.goal_revision),
                    status=WorkBoardStatus.todo,
                    capability_id="guardian-routine.v2",
                    input_artifact_id=fresh_artifact_id,
                    priority=60,
                    idempotency_scope="guardian-routine-v2-schedule",
                    idempotency_key=occurrence_key,
                    origin_thread_id=binding.owner_session_id,
                ),
                origin_session_id=binding.owner_session_id,
                publication_authority_check=publication_authority_check,
            )
            task_id = mutation.task.task_id
            occurrence = await db.get(GovernedScheduleOccurrence, occurrence_id)
            if occurrence is None:
                raise RuntimeError("procedure_schedule_occurrence_missing")
            occurrence.work_board_task_id = task_id
            occurrence.metadata_json = _dumps(
                {
                    "status": "queued",
                    "task_id": task_id,
                    "capability_id": "guardian-routine.v2",
                    "routine_id": canonical_action.get("routine_id"),
                    "version": canonical_action.get("version"),
                    "input_artifact_id": fresh_artifact_id,
                    "memory_status": "no_learning",
                }
            )
            await db.flush()
        return {
            "status": "queued",
            "occurrence_id": occurrence_id,
            "task_id": task_id,
            "replayed": False,
            "memory_status": "no_learning",
        }
    except (Exception, asyncio.CancelledError) as exc:
        # Only a typed authority/Board rejection proves that publication did
        # not cross the task writer boundary.  Its cleanup is still guarded by
        # exact owner/revision and a fresh reference check.  A generic writer,
        # flush, commit, or cancellation result is ambiguous: keep the exact
        # artifact/idempotency key and quarantine the occurrence for explicit
        # reconciliation instead of revoking or auto-replaying it.
        cleanup_outcome: str | None = None
        known_prepublication = isinstance(exc, (BoardError, _ProcedureScheduleAuthorityFailure))
        if known_prepublication and fresh_artifact_id:
            cleanup_outcome = await _cleanup_unpublished_artifact()
        if cleanup_outcome == "revoked":
            failure_state = "blocked"
            recovery_action = "retry_after_prerequisite"
        elif fresh_artifact_id or artifact_preparation_started or publication_started:
            failure_state = "unknown"
            recovery_action = "reconcile_existing_occurrence"
        else:
            # No artifact reservation or publication boundary was reached, so
            # an authority/setup failure can safely block this occurrence.
            failure_state = "blocked"
            recovery_action = "retry_after_prerequisite"
        await _settle_failure(
            exc,
            state=failure_state,
            recovery_action=recovery_action,
        )
        raise

def build_cron_trigger(job: dict[str, Any]) -> CronTrigger:
    trigger_spec = job.get("trigger_spec") or {}
    if str(job.get("action_type") or "") in {
        "calendar.observe_due_events.v1",
        "guardian.run_procedure.v2",
        "gmail.scan_metadata.v1",
    }:
        from src.scheduler.governed_schedules import cron_for_cadence

        return cron_for_cadence(trigger_spec)
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
    def _serialize_run(self, run: ScheduledJobRun) -> dict[str, Any]:
        return {
            "id": run.id,
            "scheduled_job_id": run.scheduled_job_id,
            "job_name": run.job_name,
            "trigger_type": run.trigger_type,
            "action_type": run.action_type,
            "session_id": run.session_id,
            "created_by_session_id": run.created_by_session_id,
            "status": run.status,
            "outcome": run.outcome,
            "error": run.error,
            "approval_id": run.approval_id,
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "metadata": _loads(run.metadata_json or "{}"),
        }

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
        _reject_legacy_governed_mutation(target_type)
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
            serialized = self._serialize(job)
        await log_scheduler_job_event(
            job_name=f"user_cron:{serialized['id']}",
            outcome="created",
            details={
                "scheduled_job_id": serialized["id"],
                "trigger_type": serialized["trigger_type"],
                "action_type": serialized["action_type"],
                "session_id": serialized.get("session_id"),
                "created_by_session_id": serialized.get("created_by_session_id"),
            },
        )
        return serialized

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

            _reject_legacy_governed_mutation(job.action_type)
            _reject_legacy_governed_mutation(target_type or job.action_type)

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
            serialized = self._serialize(job)
        await log_scheduler_job_event(
            job_name=f"user_cron:{serialized['id']}",
            outcome="updated",
            details={
                "scheduled_job_id": serialized["id"],
                "trigger_type": serialized["trigger_type"],
                "action_type": serialized["action_type"],
                "session_id": serialized.get("session_id"),
                "created_by_session_id": serialized.get("created_by_session_id"),
            },
        )
        return serialized

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
            _reject_legacy_governed_mutation(job.action_type)
            job.enabled = enabled
            job.updated_at = _utc_now()
            await db.flush()
            await db.refresh(job)
            serialized = self._serialize(job)
        await log_scheduler_job_event(
            job_name=f"user_cron:{serialized['id']}",
            outcome="resumed" if enabled else "paused",
            details={
                "scheduled_job_id": serialized["id"],
                "enabled": enabled,
                "trigger_type": serialized["trigger_type"],
                "action_type": serialized["action_type"],
            },
        )
        return serialized

    async def delete_job(self, job_id: str, *, owner_session_id: str | None = None) -> bool:
        async with get_session() as db:
            stmt = select(ScheduledJob).where(ScheduledJob.id == job_id)
            if owner_session_id:
                stmt = stmt.where(_owner_visibility_clause(owner_session_id))
            result = await db.execute(stmt)
            job = result.scalars().first()
            if job is None:
                return False
            _reject_legacy_governed_mutation(job.action_type)
            serialized = self._serialize(job)
            await db.delete(job)
        await log_scheduler_job_event(
            job_name=f"user_cron:{job_id}",
            outcome="deleted",
            details={
                "scheduled_job_id": job_id,
                "trigger_type": serialized["trigger_type"],
                "action_type": serialized["action_type"],
                "session_id": serialized.get("session_id"),
            },
        )
        return True

    async def start_run(
        self,
        job: dict[str, Any],
        *,
        status: str = "started",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with get_session() as db:
            run = ScheduledJobRun(
                scheduled_job_id=str(job.get("id") or ""),
                job_name=str(job.get("name") or ""),
                trigger_type=str(job.get("trigger_type") or "cron"),
                action_type=str(job.get("action_type") or ""),
                session_id=job.get("session_id") if isinstance(job.get("session_id"), str) else None,
                created_by_session_id=(
                    job.get("created_by_session_id")
                    if isinstance(job.get("created_by_session_id"), str)
                    else None
                ),
                status=status,
                metadata_json=_dumps(metadata or {}),
            )
            db.add(run)
            await db.flush()
            await db.refresh(run)
            return self._serialize_run(run)

    async def finish_run(
        self,
        run_id: str,
        *,
        outcome: str,
        status: str = "finished",
        error: str | None = None,
        approval_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        async with get_session() as db:
            result = await db.execute(select(ScheduledJobRun).where(ScheduledJobRun.id == run_id))
            run = result.scalars().first()
            if run is None:
                return None
            run.status = status
            run.outcome = outcome
            run.error = error[:400] if isinstance(error, str) and error else None
            run.approval_id = approval_id
            run.finished_at = _utc_now()
            if metadata is not None:
                # A governed occurrence/control may have written an
                # authoritative identity or cleanup proof before this
                # scheduler summary is available.  Preserve those server
                # receipts; a late summary must never erase the evidence
                # required to reconcile an unknown effect.
                existing_metadata = _loads(run.metadata_json or "{}")
                preserved_governed = {
                    key: value
                    for key, value in existing_metadata.items()
                    if str(key).startswith("governed_")
                }
                merged_metadata = dict(metadata)
                merged_metadata.update(preserved_governed)
                run.metadata_json = _dumps(merged_metadata)
            await db.flush()
            await db.refresh(run)
            return self._serialize_run(run)

    async def list_run_history(
        self,
        *,
        job_id: str | None = None,
        limit: int = 20,
        owner_session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = min(max(int(limit), 1), 100)
        async with get_session() as db:
            stmt = select(ScheduledJobRun).order_by(col(ScheduledJobRun.started_at).desc()).limit(limit)
            if job_id:
                stmt = stmt.where(ScheduledJobRun.scheduled_job_id == job_id)
            if owner_session_id:
                stmt = stmt.where(
                    or_(
                        ScheduledJobRun.created_by_session_id == owner_session_id,
                        ScheduledJobRun.session_id == owner_session_id,
                    )
                )
            result = await db.execute(stmt)
            return [self._serialize_run(run) for run in result.scalars().all()]

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


async def execute_scheduled_job(job_id: str, *, scheduled_slot_utc: datetime | None = None) -> None:
    job = await scheduled_job_repository.get_job(job_id)
    if job is None:
        return
    action_type = str(job.get("action_type") or "")
    if action_type in {
        "calendar.observe_due_events.v1",
        "guardian.run_procedure.v2",
        "gmail.scan_metadata.v1",
    } and scheduled_slot_utc is None:
        # The public legacy/manual execution seam cannot mint a governed
        # occurrence.  Only the APScheduler wrapper may forward a canonical
        # slot to this handler.
        raise RuntimeError("governed_schedule_controls_required")

    run = await scheduled_job_repository.start_run(
        job,
        metadata={
            "claim_boundary": "cron_style_scheduled_job_receipt",
            "trigger_type": job.get("trigger_type"),
        },
    )
    await log_scheduler_job_event(
        job_name=f"user_cron:{job_id}",
        outcome="triggered",
        details={
            "scheduled_job_id": job_id,
            "scheduled_job_run_id": run["id"],
            "action_type": job.get("action_type"),
            "trigger_type": job.get("trigger_type"),
        },
    )
    if not job.get("enabled", False):
        await scheduled_job_repository.finish_run(
            run["id"],
            outcome="skipped_disabled",
            status="skipped",
            metadata={"skip_reason": "job_disabled"},
        )
        await log_scheduler_job_event(
            job_name=f"user_cron:{job_id}",
            outcome="skipped",
            details={
                "scheduled_job_id": job_id,
                "scheduled_job_run_id": run["id"],
                "reason": "job_disabled",
            },
        )
        return

    try:
        if action_type == "calendar.observe_due_events.v1":
            observation = await _run_governed_calendar_observation(
                job,
                scheduled_slot_utc=scheduled_slot_utc,
                scheduled_run_id=run["id"],
            )
            outcome = str(observation.get("status") or "blocked")
            await scheduled_job_repository.record_run(job_id, outcome=outcome)
            await scheduled_job_repository.finish_run(
                run["id"],
                outcome=outcome,
                status="finished" if outcome == "succeeded" else outcome,
                metadata={
                    "occurrence_id": observation.get("occurrence_id"),
                    "event_count": observation.get("event_count", 0),
                    "replayed": bool(observation.get("replayed", False)),
                },
            )
            await log_scheduler_job_event(
                job_name=f"user_cron:{job_id}",
                outcome="succeeded" if outcome == "succeeded" else outcome,
                details={
                    "scheduled_job_id": job_id,
                    "scheduled_job_run_id": run["id"],
                    "action_type": action_type,
                    "occurrence_id": observation.get("occurrence_id"),
                },
            )
            return
        if action_type == "guardian.run_procedure.v2":
            scheduled = await _run_governed_procedure_schedule(
                job,
                scheduled_slot_utc=scheduled_slot_utc,
                scheduled_run_id=run["id"],
            )
            outcome = str(scheduled.get("status") or "blocked")
            await scheduled_job_repository.record_run(
                job_id,
                outcome=outcome,
            )
            await scheduled_job_repository.finish_run(
                run["id"],
                outcome=outcome,
                status="finished" if outcome in {"queued", "succeeded"} else outcome,
                metadata={
                    "occurrence_id": scheduled.get("occurrence_id"),
                    "task_id": scheduled.get("task_id"),
                    "replayed": bool(scheduled.get("replayed", False)),
                    "memory_status": scheduled.get("memory_status", "no_learning"),
                },
            )
            await log_scheduler_job_event(
                job_name=f"user_cron:{job_id}",
                outcome="succeeded" if outcome in {"queued", "succeeded"} else outcome,
                details={
                    "scheduled_job_id": job_id,
                    "scheduled_job_run_id": run["id"],
                    "action_type": action_type,
                    "occurrence_id": scheduled.get("occurrence_id"),
                    "task_id": scheduled.get("task_id"),
                },
            )
            return
        if action_type == "gmail.scan_metadata.v1":
            observation = await _run_governed_mail_metadata_scan(
                job,
                scheduled_slot_utc=scheduled_slot_utc,
                scheduled_run_id=run["id"],
            )
            outcome = str(observation.get("status") or "blocked")
            await scheduled_job_repository.record_run(job_id, outcome=outcome)
            await scheduled_job_repository.finish_run(
                run["id"],
                outcome=outcome,
                status="finished" if outcome == "succeeded" else outcome,
                metadata={
                    "occurrence_id": observation.get("occurrence_id"),
                    "observed_count": observation.get("observed_count", 0),
                    "new_count": observation.get("new_count", 0),
                    "notice_count": observation.get("notice_count", 0),
                    "baseline_complete": bool(observation.get("baseline_complete", False)),
                    "coverage": observation.get("coverage") or {},
                    "memory_status": "no_learning",
                },
            )
            await log_scheduler_job_event(
                job_name=f"user_cron:{job_id}",
                outcome="succeeded" if outcome == "succeeded" else outcome,
                details={
                    "scheduled_job_id": job_id,
                    "scheduled_job_run_id": run["id"],
                    "action_type": action_type,
                    "occurrence_id": observation.get("occurrence_id"),
                    "new_count": observation.get("new_count", 0),
                    "notice_count": observation.get("notice_count", 0),
                },
            )
            return
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
            await scheduled_job_repository.finish_run(
                run["id"],
                outcome=delivery_outcome,
                metadata={
                    "policy_action": result.action.value,
                    "delivery_outcome": delivery_outcome,
                },
            )
            await log_scheduler_job_event(
                job_name=f"user_cron:{job_id}",
                outcome="failed" if delivery_outcome == "failed" else "succeeded",
                details={
                    "scheduled_job_id": job_id,
                    "scheduled_job_run_id": run["id"],
                    "action_type": action_type,
                    "delivery_outcome": delivery_outcome,
                    "policy_action": result.action.value,
                },
            )
            return

        if action_type == "run_workflow":
            await _run_scheduled_workflow(job)
            await scheduled_job_repository.record_run(job_id, outcome="succeeded")
            await scheduled_job_repository.finish_run(
                run["id"],
                outcome="succeeded",
                metadata={"workflow_name": (job.get("action_spec") or {}).get("workflow_name")},
            )
            await log_scheduler_job_event(
                job_name=f"user_cron:{job_id}",
                outcome="succeeded",
                details={
                    "scheduled_job_id": job_id,
                    "scheduled_job_run_id": run["id"],
                    "action_type": action_type,
                },
            )
            return

        if action_type == "run_source_watch":
            from src.guardian.source_watch import source_watch_service

            action_spec = job.get("action_spec") or {}
            result = await source_watch_service.run_watch(
                str(action_spec.get("watch_id") or ""),
                occurrence_id=str(run["id"]),
                expected_scheduled_job_id=job_id,
                expected_owner_session_id=str(job.get("session_id") or "") or None,
            )
            outcome = str(result.get("status") or "blocked")
            approval_id = result.get("approval_id") if isinstance(result, dict) else None
            await scheduled_job_repository.record_run(
                job_id,
                outcome=outcome,
                approval_id=approval_id,
            )
            await scheduled_job_repository.finish_run(
                run["id"],
                outcome=outcome,
                status="approval_required" if outcome == "awaiting_approval" else "finished",
                approval_id=approval_id,
                metadata={"watch_id": action_spec.get("watch_id"), "job_id": result.get("job_id")},
            )
            await log_scheduler_job_event(
                job_name=f"user_cron:{job_id}",
                outcome="succeeded" if outcome in {"succeeded", "no_change", "degraded"} else outcome,
                details={
                    "scheduled_job_id": job_id,
                    "scheduled_job_run_id": run["id"],
                    "action_type": action_type,
                    "watch_id": action_spec.get("watch_id"),
                    "watch_outcome": outcome,
                },
            )
            return

        raise RuntimeError(f"Unsupported scheduled action '{action_type}'.")
    except _ProcedureScheduleDeferred as exc:
        await scheduled_job_repository.record_run(
            job_id,
            outcome="deferred",
            error=exc.reason_code,
        )
        await scheduled_job_repository.finish_run(
            run["id"],
            outcome="deferred",
            status="deferred",
            error=exc.reason_code,
            metadata={"recovery_action": exc.recovery_action},
        )
        await log_scheduler_job_event(
            job_name=f"user_cron:{job_id}",
            outcome="deferred",
            details={
                "scheduled_job_id": job_id,
                "scheduled_job_run_id": run["id"],
                "action_type": action_type,
                "reason_code": exc.reason_code,
                "recovery_action": exc.recovery_action,
            },
        )
    except _ProcedureScheduleAuthorityFailure as exc:
        await scheduled_job_repository.record_run(
            job_id,
            outcome="blocked",
            error=_safe_error_label(exc),
        )
        await scheduled_job_repository.finish_run(
            run["id"],
            outcome="blocked",
            status="blocked",
            error=_safe_error_label(exc),
            metadata={"recovery_action": "retry_after_prerequisite"},
        )
        await log_scheduler_job_event(
            job_name=f"user_cron:{job_id}",
            outcome="blocked",
            details={
                "scheduled_job_id": job_id,
                "scheduled_job_run_id": run["id"],
                "action_type": action_type,
                "reason_code": _safe_error_label(exc),
                "recovery_action": "retry_after_prerequisite",
            },
        )
    except ApprovalRequired as exc:
        await scheduled_job_repository.record_run(
            job_id,
            outcome="approval_required",
            approval_id=exc.approval_id,
        )
        await scheduled_job_repository.finish_run(
            run["id"],
            outcome="approval_required",
            status="approval_required",
            approval_id=exc.approval_id,
            metadata={"tool_name": exc.tool_name},
        )
        await log_scheduler_job_event(
            job_name=f"user_cron:{job_id}",
            outcome="approval_required",
            details={
                "scheduled_job_id": job_id,
                "scheduled_job_run_id": run["id"],
                "action_type": action_type,
                "approval_id": exc.approval_id,
                "tool_name": exc.tool_name,
            },
        )
    except asyncio.CancelledError:
        # ``CancelledError`` inherits directly from ``BaseException``.  Keep
        # the cancellation signal, but finish the already durable scheduler
        # run so the operator never sees a receipt stuck in ``started`` while
        # the governed occurrence has been quarantined by its recovery path.
        try:
            await scheduled_job_repository.record_run(
                job_id,
                outcome="cancelled",
                error="CancelledError",
            )
            await scheduled_job_repository.finish_run(
                run["id"],
                outcome="cancelled",
                status="cancelled",
                error="CancelledError",
            )
        except BaseException:
            # Preserve the original cancellation even if a second cancellation
            # or a database failure interrupts the best-effort finalization.
            logger.exception("Could not finalize cancelled scheduled job %s", job_id)
        raise
    except Exception as exc:
        logger.exception("Scheduled job %s failed", job_id)
        safe_error = _safe_error_label(exc)
        await scheduled_job_repository.record_run(
            job_id,
            outcome="failed",
            error=safe_error,
        )
        await scheduled_job_repository.finish_run(
            run["id"],
            outcome="failed",
            status="failed",
            error=safe_error,
        )
        await log_scheduler_job_event(
            job_name=f"user_cron:{job_id}",
            outcome="failed",
            details={
                "scheduled_job_id": job_id,
                "scheduled_job_run_id": run["id"],
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
    principal = scheduled_workflow_service_principal(
        scheduled_job_id=str(job.get("id") or ""),
        session_id=session_id,
    )
    tokens = set_runtime_context(
        session_id,
        approval_mode,
        trust_principal=principal,
    )
    try:
        workflow_tool(**dict(action_spec.get("workflow_args") or {}))
    finally:
        reset_runtime_context(tokens)


scheduled_job_repository = ScheduledJobRepository()
