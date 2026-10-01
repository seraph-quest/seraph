"""Persisted user-scheduled jobs for dynamic cron routines."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import and_, or_, select as sa_select
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
    OperatorSession,
    ScheduledJob,
    ScheduledJobRun,
    WorkBoardInputArtifact,
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
    revoke_input_artifact,
)
from src.work_board.repository import WorkBoardRepository

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

def build_cron_trigger(job: dict[str, Any]) -> CronTrigger:
    trigger_spec = job.get("trigger_spec") or {}
    if str(job.get("action_type") or "") == "calendar.observe_due_events.v1":
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
    if action_type == "calendar.observe_due_events.v1" and scheduled_slot_utc is None:
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
