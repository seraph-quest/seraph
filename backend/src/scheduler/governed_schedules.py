"""The single durable seam for governed scheduler bindings and occurrences.

The legacy scheduler owns the trigger row.  This module owns the capability
binding and the occurrence fence around it.  In particular, provider work is
never admitted from an in-memory boolean: a fresh serialized database
transaction reserves the slot, and every worker transition uses the claim
token and fencing value as a compare-and-swap.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from src.db.models import (
    CalendarReadConsent,
    GovernedScheduleBinding,
    GovernedScheduleOccurrence,
    MailReadConsent,
    OperatorSession,
    ScheduledJob,
    ScheduledJobRun,
    WorkBoardAttempt,
    WorkBoardInputArtifact,
    WorkBoardStatus,
    WorkBoardTask,
)
from src.work_board.contracts import WorkBoardInputArtifactCreate, WorkBoardOwner
from src.work_board.dispatcher import validate_capability_input
from src.work_board.input_artifacts import prepare_input_artifact


ALLOWED_CADENCES = frozenset({"5min", "hourly", "6h", "daily"})
GOVERNED_ACTION = "calendar.observe_due_events.v1"
PROCEDURE_ACTION = "guardian.run_procedure.v2"
OCCURRENCE_STATES = frozenset(
    {"reserved", "running", "coalesced", "succeeded", "blocked", "cancelled", "unknown"}
)
TERMINAL_OCCURRENCE_STATES = frozenset({"coalesced", "succeeded", "blocked", "cancelled"})
_ACTIVE_OCCURRENCE_STATES = ("reserved", "running", "unknown")
_LEASE = timedelta(minutes=5)
_MAX_BINDINGS = 32
_MAX_OBSERVATIONS = 10
_CONTROL_ACTION_TYPE = "calendar.governed_schedule_control.v1"
_CONTROL_KEY_MAX = 256
_CONTROL_RECEIPT_MAX_BYTES = 16 * 1024
_QUIESCENCE_KEYS = frozenset(
    {
        "status",
        "active_operations",
        "unsettled_operations",
        "requests_started",
        "requests_settled",
    }
)
_CLEANUP_ORIGINS = frozenset({"adapter_quiescence", "server_no_contact_before_adapter"})


def _control_receipt_id(
    *,
    owner_principal_id: str,
    owner_session_id: str,
    scheduled_job_id: str,
    idempotency_key: str,
) -> str:
    """Return the stable primary key for one owner/session/job/control tuple."""

    identity = json.dumps(
        {
            "prefix": "seraph:governed-control",
            "owner_principal_id": owner_principal_id,
            "owner_session_id": owner_session_id,
            "scheduled_job_id": scheduled_job_id,
            "idempotency_key": idempotency_key,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))

# This is deliberately server-owned.  Reserved entries are visible to the
# registry for diagnostics but cannot be enabled or executed by a caller.
ACTION_REGISTRY: dict[str, dict[str, Any]] = {
    GOVERNED_ACTION: {
        "capability_id": GOVERNED_ACTION,
        "consent_kind": "calendar_read",
        "model": False,
        "enabled": True,
    },
    PROCEDURE_ACTION: {
        "capability_id": PROCEDURE_ACTION,
        "consent_kind": "goal_budget",
        "model": False,
        "enabled": True,
    },
    "gmail.scan_metadata.v1": {
        "capability_id": "gmail.scan_metadata.v1",
        "consent_kind": "mail_read",
        "model": False,
        "enabled": True,
    },
}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _json(value: Mapping[str, Any]) -> str:
    # Receipt metadata is operator-visible and must remain bounded even when a
    # provider or an exception supplies a large value.
    encoded = json.dumps(dict(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 2048:
        raise ValueError("governed occurrence metadata is too large")
    return encoded


def is_governed_action(action_type: str | None) -> bool:
    return str(action_type or "").strip() in ACTION_REGISTRY


def _action_pair_is_supported(action_type: Any, capability_id: Any) -> bool:
    """Accept only a registered governed action with its exact capability."""

    action = str(action_type or "").strip()
    capability = str(capability_id or "").strip()
    return bool(action) and action == capability and is_governed_action(action)


async def _procedure_terminal_state(
    db: Any,
    occurrence: GovernedScheduleOccurrence,
    task: WorkBoardTask,
) -> tuple[str, str | None, str | None] | None:
    """Return a safe terminal transition for one completed procedure task.

    A Work Board ``review``/``done`` projection is not itself a native
    procedure proof.  The parent durable root and its independently verified
    readback must still match the occurrence before the scheduler can mark the
    slot succeeded.  Uncertain durable effects deliberately return ``None``;
    the normal occurrence lease path then quarantines them as ``unknown``.
    """

    task_state = str(getattr(getattr(task, "status", None), "value", getattr(task, "status", "")) or "")
    if task_state == WorkBoardStatus.blocked.value:
        return "blocked", "procedure_task_blocked", "retry_after_prerequisite"
    if task_state == WorkBoardStatus.review.value:
        return "blocked", "procedure_task_review_required", "review_procedure_task"
    if task_state != WorkBoardStatus.done.value:
        return None

    binding = await db.get(GovernedScheduleBinding, occurrence.binding_id)
    if (
        binding is None
        or binding.action_type != PROCEDURE_ACTION
        or binding.capability_id != PROCEDURE_ACTION
        or task.capability_id != "guardian-routine.v2"
        or task.owner_principal_id != binding.owner_principal_id
        or task.owner_session_id != binding.owner_session_id
        or task.goal_id != binding.goal_id
        or int(task.goal_revision or 0) != int(binding.goal_revision or 0)
        or task.idempotency_scope != "guardian-routine-v2-schedule"
        or task.idempotency_key
        != f"{binding.binding_id}:{_utc(occurrence.slot_utc).strftime('%Y%m%dT%H%M%SZ')}"
    ):
        return "blocked", "procedure_parent_binding_mismatch", "reconcile_admission_binding"

    # The occurrence durable_job_id is the scheduler wrapper/run identity and
    # must remain unchanged for occurrence idempotency and cleanup.  The native
    # procedure root is server-derived from the exact terminal Board attempt.
    artifact_id = str(task.input_artifact_id or "").strip()
    artifact = await db.get(WorkBoardInputArtifact, artifact_id) if artifact_id else None
    if (
        artifact is None
        or artifact.owner_principal_id != task.owner_principal_id
        or artifact.owner_session_id != task.owner_session_id
        or artifact.goal_id != task.goal_id
        or int(artifact.goal_revision or 0) != int(task.goal_revision or 0)
        or artifact.capability_id != "guardian-routine.v2"
        or artifact.bound_task_id != task.task_id
        or artifact.state not in {"bound", "consumed"}
        or not artifact.typed_input_ref
        or not task.typed_input_ref
        or artifact.typed_input_ref != task.typed_input_ref
        or not task.typed_input_digest
        or artifact.payload_sha256 != task.typed_input_digest
    ):
        return "blocked", "procedure_input_binding_missing", "reconcile_admission_binding"

    attempt = (
        await db.execute(
            select(WorkBoardAttempt)
            .where(
                WorkBoardAttempt.task_id == task.task_id,
            )
            .order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    job_id = str(attempt.workflow_run_id or "").strip() if attempt is not None else ""
    if attempt is None or not job_id or attempt.ended_at is None or str(attempt.outcome or "") != "verified":
        return "blocked", "procedure_verified_readback_missing", "reconcile_admission_binding"

    try:
        from src.work_board.dispatcher import WorkBoardDispatcher
        from src.workflows.job_runtime import UNCERTAIN_EXTERNAL_EFFECT_STATUSES, durable_job_repository

        projection = await durable_job_repository.get_job(job_id)
        if not isinstance(projection, Mapping):
            return "blocked", "procedure_parent_missing", "reconcile_admission_binding"
        owner = projection.get("owner") if isinstance(projection.get("owner"), Mapping) else {}
        authority = (
            projection.get("declared_authority")
            if isinstance(projection.get("declared_authority"), Mapping)
            else {}
        )
        idempotency = (
            projection.get("idempotency")
            if isinstance(projection.get("idempotency"), Mapping)
            else {}
        )
        positive_int = lambda value: type(value) is int and value > 0
        if (
            str(projection.get("job_id") or projection.get("run_identity") or "") != job_id
            or str(projection.get("root_run_identity") or "") != job_id
            or projection.get("parent_run_identity") is not None
            or projection.get("parent_job_id") is not None
            or str(projection.get("job_kind") or "") != "guardian_routine_v2"
            or str(projection.get("capability_version") or "") != "guardian-routine.v2"
            or str(owner.get("kind") or "") != "user"
            or str(owner.get("principal_id") or "") != task.owner_principal_id
            or str(projection.get("session_id") or "") != task.owner_session_id
            or str(projection.get("operator_session_id") or "") != task.owner_session_id
            or str(projection.get("goal_id") or "") != task.goal_id
            or not positive_int(projection.get("goal_revision"))
            or int(projection.get("goal_revision")) != int(task.goal_revision)
            or str(idempotency.get("scope") or "") != "work-board-attempt"
            or str(idempotency.get("key") or "") != f"{task.task_id}:{attempt.attempt_id}"
            or str(authority.get("principal") or "") != task.owner_principal_id
            or str(authority.get("owner_kind") or "") != "user"
            or str(authority.get("session_id") or "") != task.owner_session_id
            or str(authority.get("operator_session_id") or "") != task.owner_session_id
            or str(authority.get("goal_id") or "") != task.goal_id
            or not positive_int(authority.get("goal_revision"))
            or int(authority.get("goal_revision")) != int(task.goal_revision)
            or str(authority.get("capability_id") or "") != "guardian-routine.v2"
            or str(authority.get("board_task_id") or "") != task.task_id
            or str(authority.get("board_attempt_id") or "") != attempt.attempt_id
            or str(authority.get("input_artifact_id") or "") != artifact.artifact_id
            or str(authority.get("input_artifact_digest") or "") != task.typed_input_digest
            or not positive_int(authority.get("board_task_revision"))
            or int(authority.get("board_task_revision")) > int(task.task_revision)
            # claim_ready_task records the pre-transition revision on the
            # attempt, then atomically advances the Board task to the
            # running revision.  ProcedureV2 admits its native root from that
            # running task, so its immutable authority must carry exactly the
            # post-claim revision.  Do not widen this to a >= check: a later
            # unrelated Board mutation must remain a binding failure.
            or int(authority.get("board_task_revision")) != int(attempt.task_revision_at_claim) + 1
            or not positive_int(authority.get("board_fencing_token"))
            or int(authority.get("board_fencing_token")) != int(attempt.fencing_token)
        ):
            return "blocked", "procedure_parent_authority_mismatch", "reconcile_admission_binding"
        projection_status = str(projection.get("status") or "")
        effects = projection.get("effects") if isinstance(projection.get("effects"), list) else []
        if projection_status in UNCERTAIN_EXTERNAL_EFFECT_STATUSES or any(
            isinstance(effect, Mapping)
            and str(effect.get("status") or "")
            in {"unknown", "intent", "dispatched", "unknown_external_effect", "cost_liability"}
            for effect in effects
        ):
            return None
        if projection_status != "succeeded":
            return "blocked", "procedure_parent_not_verified", "reconcile_admission_binding"
        proof = WorkBoardDispatcher._workflow_readback(projection, job_id)
        if proof is None:
            return "blocked", "procedure_verified_readback_missing", "reconcile_admission_binding"
    except Exception:
        # A missing/corrupt proof must never become a successful occurrence.
        return "blocked", "procedure_verified_readback_unavailable", "reconcile_admission_binding"
    return "succeeded", None, None


def action_spec(action_type: str) -> dict[str, Any]:
    entry = ACTION_REGISTRY.get(str(action_type or "").strip())
    if entry is None or not bool(entry.get("enabled")):
        raise ValueError("governed schedule action is unavailable")
    return dict(entry)


def normalize_cadence(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("cadence must be an object")
    allowed = {"kind", "timezone", "daily_hour", "daily_minute"}
    if set(value) - allowed:
        raise ValueError("cadence contains unsupported fields")
    kind = str(value.get("kind") or "")
    timezone_name = str(value.get("timezone") or "")
    if kind not in ALLOWED_CADENCES:
        raise ValueError("cadence kind is not supported")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("cadence timezone is not supported") from exc
    hour = value.get("daily_hour")
    minute = value.get("daily_minute")
    if kind == "daily":
        if (
            type(hour) is not int
            or not 0 <= hour <= 23
            or type(minute) is not int
            or not 0 <= minute <= 59
        ):
            raise ValueError("daily cadence requires a valid hour and minute")
    elif hour is not None or minute is not None:
        raise ValueError("daily hour and minute are only valid for daily cadence")
    return {
        "kind": kind,
        "timezone": timezone_name,
        "daily_hour": hour if kind == "daily" else None,
        "daily_minute": minute if kind == "daily" else None,
    }


def _cadence_from_trigger(value: Mapping[str, Any]) -> dict[str, Any]:
    """Accept only the canonical trigger representation used by old rows."""
    if "kind" in value:
        return normalize_cadence(value)
    cron = str(value.get("cron") or "").strip()
    timezone_name = str(value.get("timezone") or "UTC")
    canonical = {
        "*/5 * * * *": "5min",
        "0 * * * *": "hourly",
        "0 */6 * * *": "6h",
    }
    if cron in canonical:
        return normalize_cadence(
            {"kind": canonical[cron], "timezone": timezone_name, "daily_hour": None, "daily_minute": None}
        )
    fields = cron.split()
    if len(fields) == 5 and fields[2:] == ["*", "*", "*"]:
        try:
            minute, hour = int(fields[0]), int(fields[1])
        except ValueError:
            minute = hour = -1
        if 0 <= minute <= 59 and 0 <= hour <= 23:
            return normalize_cadence(
                {"kind": "daily", "timezone": timezone_name, "daily_hour": hour, "daily_minute": minute}
            )
    raise ValueError("governed schedule trigger is not canonical")


def cron_for_cadence(cadence: Mapping[str, Any]) -> CronTrigger:
    value = normalize_cadence(cadence)
    expressions = {"5min": "*/5 * * * *", "hourly": "0 * * * *", "6h": "0 */6 * * *"}
    expression = expressions.get(value["kind"])
    if expression is None:
        expression = f"{value['daily_minute']} {value['daily_hour']} * * *"
    return CronTrigger.from_crontab(expression, timezone=ZoneInfo(value["timezone"]))


def latest_due_slot(cadence: Mapping[str, Any], now_utc: datetime | None = None) -> datetime | None:
    """Return one latest canonical UTC slot from a bounded 24-hour window."""
    try:
        normalized = _cadence_from_trigger(cadence)
        trigger = cron_for_cadence(normalized)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return None
    now = _utc(now_utc or _now())
    cursor = now - timedelta(hours=24)
    latest: datetime | None = None
    for _ in range(320):
        candidate = trigger.get_next_fire_time(
            None if latest is None else latest,
            cursor if latest is None else latest,
        )
        if candidate is None:
            break
        candidate = _utc(candidate)
        if candidate > now:
            break
        latest = candidate
    return latest.replace(second=0, microsecond=0) if latest is not None else None


def _is_sqlite(db: Any) -> bool:
    try:
        return getattr(db.get_bind().dialect, "name", "") == "sqlite"
    except (AttributeError, RuntimeError):
        return False


async def _begin_serialized(db: Any) -> None:
    """Start the scheduler write fence after discarding only a clean read tx."""
    if not _is_sqlite(db):
        return
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("scheduler_transaction_boundary")
    if db.in_transaction():
        # Preserve any already-flushed clean work (for example a prior
        # occurrence receipt) while closing the deferred SELECT transaction.
        # A rollback here would silently erase a terminal receipt before the
        # next slot reservation.
        await db.commit()
    await db.execute(text("BEGIN IMMEDIATE"))


async def _persist_unknown_and_raise(db: Any, row: GovernedScheduleOccurrence, now: datetime) -> None:
    row.state = "unknown"
    row.metadata_json = _json(
        {"failure_code": "occurrence_lease_expired", "recovery_action": "reconcile_external_effect"}
    )
    row.updated_at = now
    await db.flush()
    # Preserve the evidence which caused the slot to be fenced even though the
    # caller will receive an exception and normally roll back its context.
    await db.commit()
    raise RuntimeError("governed_occurrence_requires_reconciliation")


def _iso(value: datetime | None) -> str | None:
    return _utc(value).isoformat().replace("+00:00", "Z") if value else None


def serialize_occurrence(row: GovernedScheduleOccurrence | None) -> dict[str, Any] | None:
    if row is None:
        return None
    try:
        metadata = json.loads(row.metadata_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "occurrence_id": row.occurrence_id,
        "binding_revision": row.binding_revision,
        "slot_utc": _iso(row.slot_utc),
        "state": row.state,
        "task_id": row.work_board_task_id,
        "job_id": row.durable_job_id,
        "failure_code": metadata.get("failure_code"),
        "recovery_action": metadata.get("recovery_action"),
        "updated_at": _iso(row.updated_at),
    }


def serialize_binding(
    row: GovernedScheduleBinding,
    occurrence: GovernedScheduleOccurrence | None = None,
) -> dict[str, Any]:
    return {
        "binding_id": row.binding_id,
        "scheduled_job_id": row.scheduled_job_id,
        "capability_id": row.capability_id,
        "action_type": row.action_type,
        "goal_id": row.goal_id,
        "goal_revision": row.goal_revision,
        "input_artifact_id": row.input_artifact_id,
        "input_digest": row.input_digest,
        "consent_kind": row.consent_kind,
        "consent_id": row.read_consent_id,
        "consent_revision": row.consent_revision,
        "consent_digest": row.consent_digest,
        "cadence": {
            "kind": row.cadence_kind,
            "timezone": row.timezone,
            "daily_hour": row.daily_hour,
            "daily_minute": row.daily_minute,
        },
        "binding_revision": row.binding_revision,
        "expires_at": _iso(row.expires_at),
        "state": row.state,
        "last_slot_utc": _iso(row.last_slot_utc),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "latest_occurrence": serialize_occurrence(occurrence),
    }


_CONTROL_RESPONSE_KEYS = frozenset(
    {
        "binding_id",
        "scheduled_job_id",
        "capability_id",
        "action_type",
        "goal_id",
        "goal_revision",
        "input_artifact_id",
        "input_digest",
        "consent_kind",
        "consent_id",
        "consent_revision",
        "consent_digest",
        "cadence",
        "binding_revision",
        "expires_at",
        "state",
        "last_slot_utc",
        "created_at",
        "updated_at",
        "latest_occurrence",
    }
)
_CONTROL_RESPONSE_STATES = frozenset({"active", "paused", "revoked", "expired"})


def _validate_control_response(
    response: Any,
    *,
    binding_id: str,
    scheduled_job_id: str,
    expected_revision: int,
    expected_state: str,
) -> dict[str, Any]:
    """Validate the stored public DTO before returning an idempotent replay.

    Control receipts are durable database data, so a malformed or hand-edited
    row must fail closed.  In particular, never project arbitrary fields from
    receipt JSON into an operator response.
    """
    if not isinstance(response, dict) or set(response) != _CONTROL_RESPONSE_KEYS:
        raise RuntimeError("governed_schedule_control_receipt_invalid")
    if (
        response.get("binding_id") != binding_id
        or response.get("scheduled_job_id") != scheduled_job_id
        or not _action_pair_is_supported(response.get("action_type"), response.get("capability_id"))
        or response.get("state") != expected_state
        or type(response.get("goal_revision")) is not int
        or response.get("goal_revision") < 1
        or type(response.get("consent_revision")) is not int
        or response.get("consent_revision") < 1
        or type(response.get("binding_revision")) is not int
        or response.get("binding_revision") != expected_revision + 1
        or not isinstance(response.get("binding_id"), str)
        or not isinstance(response.get("scheduled_job_id"), str)
        or not isinstance(response.get("goal_id"), str)
        or not isinstance(response.get("input_artifact_id"), str)
        or not isinstance(response.get("input_digest"), str)
        or not isinstance(response.get("consent_kind"), str)
        or not isinstance(response.get("consent_digest"), str)
        or not isinstance(response.get("created_at"), str)
        or not isinstance(response.get("updated_at"), str)
        or response.get("state") not in _CONTROL_RESPONSE_STATES
    ):
        raise RuntimeError("governed_schedule_control_receipt_invalid")
    for field in ("consent_id", "expires_at", "last_slot_utc"):
        if response[field] is not None and not isinstance(response[field], str):
            raise RuntimeError("governed_schedule_control_receipt_invalid")
    cadence = response.get("cadence")
    if not isinstance(cadence, dict):
        raise RuntimeError("governed_schedule_control_receipt_invalid")
    try:
        if normalize_cadence(cadence) != cadence:
            raise ValueError("noncanonical cadence")
    except (TypeError, ValueError):
        raise RuntimeError("governed_schedule_control_receipt_invalid")
    occurrence = response.get("latest_occurrence")
    if occurrence is not None:
        expected_occurrence_keys = {
            "occurrence_id",
            "binding_revision",
            "slot_utc",
            "state",
            "task_id",
            "job_id",
            "failure_code",
            "recovery_action",
            "updated_at",
        }
        if not isinstance(occurrence, dict) or set(occurrence) != expected_occurrence_keys:
            raise RuntimeError("governed_schedule_control_receipt_invalid")
        if (
            not isinstance(occurrence.get("occurrence_id"), str)
            or type(occurrence.get("binding_revision")) is not int
            or not isinstance(occurrence.get("slot_utc"), str)
            or occurrence.get("state") not in OCCURRENCE_STATES
            or (occurrence.get("task_id") is not None and not isinstance(occurrence.get("task_id"), str))
            or (occurrence.get("job_id") is not None and not isinstance(occurrence.get("job_id"), str))
            or (occurrence.get("failure_code") is not None and not isinstance(occurrence.get("failure_code"), str))
            or (occurrence.get("recovery_action") is not None and not isinstance(occurrence.get("recovery_action"), str))
            or not isinstance(occurrence.get("updated_at"), str)
        ):
            raise RuntimeError("governed_schedule_control_receipt_invalid")
    return json.loads(json.dumps(response, ensure_ascii=True, separators=(",", ":")))


async def reserve_occurrence(
    db: Any,
    binding: GovernedScheduleBinding,
    *,
    slot_utc: datetime,
    now_utc: datetime | None = None,
) -> tuple[GovernedScheduleOccurrence, bool]:
    """Reserve one slot before provider contact."""
    now = _utc(now_utc or _now())
    slot = _utc(slot_utc).replace(second=0, microsecond=0)
    binding_id = str(binding.binding_id or "")
    if not binding_id:
        raise ValueError("governed schedule binding is unavailable")
    await _begin_serialized(db)
    # SQLite DateTime binds are timezone-naive. Store one canonical UTC wall
    # value there; _utc restores its semantic timezone at comparison/receipt
    # boundaries.
    db_slot = slot.replace(tzinfo=None) if _is_sqlite(db) else slot
    current = (
        await db.execute(select(GovernedScheduleBinding).where(GovernedScheduleBinding.binding_id == binding_id))
    ).scalar_one_or_none()
    if current is None:
        raise ValueError("governed schedule binding is unavailable")
    if not _action_pair_is_supported(current.action_type, current.capability_id):
        raise RuntimeError("governed_schedule_action_unavailable")
    if current.state != "active" or _utc(current.expires_at) <= now:
        raise ValueError("governed schedule binding is not active")
    job = (
        await db.execute(select(ScheduledJob).where(ScheduledJob.id == current.scheduled_job_id))
    ).scalar_one_or_none()
    if job is None or job.trigger_type != "governed" or not _action_pair_is_supported(job.action_type, current.capability_id):
        raise RuntimeError("governed_schedule_binding_link_invalid")
    try:
        job_spec = json.loads(job.action_spec_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        job_spec = {}
    if not isinstance(job_spec, dict) or job_spec.get("binding_id") != current.binding_id:
        raise RuntimeError("governed_schedule_binding_link_invalid")

    existing = (
        await db.execute(
            select(GovernedScheduleOccurrence).where(
                GovernedScheduleOccurrence.binding_id == current.binding_id,
                GovernedScheduleOccurrence.slot_utc == db_slot,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (
            existing.state in {"reserved", "running"}
            and existing.lease_expires_at is not None
            and _utc(existing.lease_expires_at) <= now
        ):
            await _persist_unknown_and_raise(db, existing, now)
        return existing, True

    active_count = int(
        (
            await db.execute(
                select(func.count(GovernedScheduleBinding.binding_id)).where(
                    GovernedScheduleBinding.owner_principal_id == current.owner_principal_id,
                    GovernedScheduleBinding.state.in_(("active", "paused")),
                )
            )
        ).scalar_one()
        or 0
    )
    if active_count > _MAX_BINDINGS:
        raise RuntimeError("governed_schedule_capacity_exceeded")

    active = (
        await db.execute(
            select(GovernedScheduleOccurrence)
            .where(GovernedScheduleOccurrence.state.in_(_ACTIVE_OCCURRENCE_STATES))
            .order_by(GovernedScheduleOccurrence.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if active is not None:
        active_binding = await db.get(GovernedScheduleBinding, active.binding_id)
        if (
            active_binding is not None
            and active_binding.action_type == PROCEDURE_ACTION
            and active.work_board_task_id
        ):
            task = (
                await db.execute(
                    select(WorkBoardTask).where(
                        WorkBoardTask.task_id == active.work_board_task_id
                    )
                )
            ).scalar_one_or_none()
            terminal = (
                await _procedure_terminal_state(db, active, task)
                if task is not None
                else None
            )
            if terminal is not None:
                terminal_state, failure_code, recovery_action = terminal
                await settle_occurrence(
                    db,
                    active,
                    state=terminal_state,
                    task_id=active.work_board_task_id,
                    job_id=active.durable_job_id,
                    failure_code=failure_code,
                    recovery_action=recovery_action,
                    claim_token=active.claim_token,
                    fencing_token=active.fencing_token,
                )
                active = None
    if active is not None:
        if active.state == "unknown":
            raise RuntimeError("governed_occurrence_requires_reconciliation")
        if active.lease_expires_at is None or _utc(active.lease_expires_at) <= now:
            await _persist_unknown_and_raise(db, active, now)
        raise RuntimeError("observation_capacity_deferred")

    row = GovernedScheduleOccurrence(
        binding_id=current.binding_id,
        binding_revision=current.binding_revision,
        slot_utc=db_slot,
        idempotency_key=f"{current.owner_principal_id}:{current.binding_id}:{slot.isoformat()}",
        request_digest=current.action_digest,
        claim_token=uuid.uuid4().hex,
        fencing_token=1,
        lease_expires_at=now + _LEASE,
        state="reserved",
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        await _begin_serialized(db)
        existing = (
            await db.execute(
                select(GovernedScheduleOccurrence).where(
                    GovernedScheduleOccurrence.binding_id == current.binding_id,
                    GovernedScheduleOccurrence.slot_utc == db_slot,
                )
            )
        ).scalar_one()
        return existing, True
    current.last_slot_utc = db_slot
    current.updated_at = now
    await db.flush()
    return row, False


async def claim_occurrence(
    db: Any,
    occurrence: GovernedScheduleOccurrence,
    *,
    claim_token: str | None = None,
    fencing_token: int | None = None,
    now_utc: datetime | None = None,
) -> GovernedScheduleOccurrence:
    """CAS a reserved receipt into running and advance its fence."""
    now = _utc(now_utc or _now())
    token = str(claim_token or occurrence.claim_token or "")
    fence = int(occurrence.fencing_token if fencing_token is None else fencing_token)
    if not token or occurrence.state != "reserved":
        raise ValueError("governed occurrence is not claimable")
    result = await db.execute(
        update(GovernedScheduleOccurrence)
        .where(
            GovernedScheduleOccurrence.occurrence_id == occurrence.occurrence_id,
            GovernedScheduleOccurrence.state == "reserved",
            GovernedScheduleOccurrence.claim_token == token,
            GovernedScheduleOccurrence.fencing_token == fence,
        )
        .values(
            state="running",
            fencing_token=fence + 1,
            lease_expires_at=now + _LEASE,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        raise RuntimeError("governed_occurrence_claim_stale")
    await db.refresh(occurrence)
    return occurrence


async def settle_occurrence(
    db: Any,
    occurrence: GovernedScheduleOccurrence,
    *,
    state: str,
    task_id: str | None = None,
    job_id: str | None = None,
    failure_code: str | None = None,
    recovery_action: str | None = None,
    claim_token: str | None = None,
    fencing_token: int | None = None,
) -> GovernedScheduleOccurrence:
    """CAS a running occurrence to a terminal/unknown receipt."""
    if state not in {"succeeded", "blocked", "cancelled", "coalesced", "unknown"}:
        raise ValueError("invalid governed occurrence state")
    token = str(claim_token or occurrence.claim_token or "")
    fence = int(occurrence.fencing_token if fencing_token is None else fencing_token)
    if occurrence.state != "running" or not token:
        raise ValueError("governed occurrence is not settleable")
    result = await db.execute(
        update(GovernedScheduleOccurrence)
        .where(
            GovernedScheduleOccurrence.occurrence_id == occurrence.occurrence_id,
            GovernedScheduleOccurrence.state == "running",
            GovernedScheduleOccurrence.claim_token == token,
            GovernedScheduleOccurrence.fencing_token == fence,
        )
        .values(
            state=state,
            work_board_task_id=task_id or occurrence.work_board_task_id,
            durable_job_id=job_id or occurrence.durable_job_id,
            metadata_json=_json({"failure_code": failure_code, "recovery_action": recovery_action}),
            lease_expires_at=None,
            updated_at=_now(),
        )
    )
    if result.rowcount != 1:
        raise RuntimeError("governed_occurrence_settlement_stale")
    await db.refresh(occurrence)
    return occurrence


async def reconcile_occurrence(
    db: Any,
    occurrence: GovernedScheduleOccurrence,
    *,
    known_state: str | None = None,
    failure_code: str | None = None,
    expected_binding_revision: int | None = None,
    server_cleanup_run_id: str | None = None,
    owner_principal_id: str | None = None,
    owner_session_id: str | None = None,
) -> GovernedScheduleOccurrence:
    """Resolve an unknown effect only after a server-owned cleanup receipt.

    ``unknown`` is a quarantine state.  A caller supplied boolean (or an
    unbound dictionary that merely claims cleanup) cannot release the global
    observation lane.  The recovery writer must first persist a proof on the
    canonical :class:`ScheduledJobRun` row bound to the occurrence's durable
    run, claim token, fencing token, binding revision, and owner.  This
    helper only consumes that fresh database receipt and performs a matching
    CAS; when no such writer exists the occurrence remains recoverable but
    blocked.
    """
    if occurrence.state != "unknown":
        raise ValueError("only unknown occurrences require reconciliation")
    if known_state not in TERMINAL_OCCURRENCE_STATES:
        raise ValueError("reconciliation outcome is invalid")
    if expected_binding_revision is not None and occurrence.binding_revision != expected_binding_revision:
        raise RuntimeError("governed_occurrence_revision_stale")
    if not server_cleanup_run_id:
        raise RuntimeError("occurrence_cleanup_proof_required")
    if not owner_principal_id or not owner_session_id:
        raise RuntimeError("occurrence_cleanup_owner_required")

    binding = (
        await db.execute(
            select(GovernedScheduleBinding).where(
                GovernedScheduleBinding.binding_id == occurrence.binding_id,
                GovernedScheduleBinding.owner_principal_id == owner_principal_id,
                GovernedScheduleBinding.owner_session_id == owner_session_id,
            )
        )
    ).scalar_one_or_none()
    if binding is None or binding.binding_revision != occurrence.binding_revision:
        raise RuntimeError("governed_occurrence_revision_stale")
    if occurrence.durable_job_id != server_cleanup_run_id:
        raise RuntimeError("occurrence_cleanup_run_mismatch")
    run = (
        await db.execute(
            select(ScheduledJobRun).where(
                ScheduledJobRun.id == server_cleanup_run_id,
                ScheduledJobRun.scheduled_job_id == binding.scheduled_job_id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise RuntimeError("occurrence_cleanup_proof_unavailable")
    try:
        metadata = json.loads(run.metadata_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        metadata = {}
    proof = metadata.get("governed_cleanup_proof") if isinstance(metadata, dict) else None
    if not isinstance(proof, dict):
        raise RuntimeError("occurrence_cleanup_proof_unavailable")
    expected_proof = {
        "status": "verified",
        "run_id": run.id,
        "occurrence_id": occurrence.occurrence_id,
        "binding_id": occurrence.binding_id,
        "binding_revision": occurrence.binding_revision,
        "claim_token": occurrence.claim_token,
        "fencing_token": occurrence.fencing_token,
        "owner_principal_id": binding.owner_principal_id,
        "owner_session_id": binding.owner_session_id,
    }
    if any(proof.get(key) != value for key, value in expected_proof.items()):
        raise RuntimeError("occurrence_cleanup_proof_mismatch")
    verified_at = proof.get("verified_at")
    try:
        verified_time = _utc(datetime.fromisoformat(str(verified_at).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        raise RuntimeError("occurrence_cleanup_proof_invalid")
    if verified_time > _now() or verified_time < _utc(occurrence.updated_at):
        raise RuntimeError("occurrence_cleanup_proof_invalid")
    result = await db.execute(
        update(GovernedScheduleOccurrence)
        .where(
            GovernedScheduleOccurrence.occurrence_id == occurrence.occurrence_id,
            GovernedScheduleOccurrence.state == "unknown",
            GovernedScheduleOccurrence.binding_revision == occurrence.binding_revision,
            GovernedScheduleOccurrence.claim_token == occurrence.claim_token,
            GovernedScheduleOccurrence.fencing_token == occurrence.fencing_token,
        )
        .values(
            state=known_state,
            lease_expires_at=None,
            claim_token=None,
            metadata_json=_json(
                {
                    "failure_code": failure_code,
                    "recovery_action": "reconciled_cleanup_verified",
                    "cleanup_proof_run_id": run.id,
                    "cleanup_verified": True,
                    "cleanup_verified_at": verified_time.isoformat(),
                }
            ),
            updated_at=_now(),
        )
    )
    if result.rowcount != 1:
        raise RuntimeError("governed_occurrence_reconciliation_stale")
    await db.refresh(occurrence)
    return occurrence


def _validated_transport_quiescence(value: Any) -> dict[str, Any]:
    """Validate the adapter's server-owned transport settlement receipt.

    The scheduler must never infer that a request ended from a timeout,
    cancellation, an ``is_closed`` flag, or the absence of an in-memory task.
    The adapter owns the HTTPX lifecycle and exposes this small, bounded
    receipt only after it has awaited client closure.  Keep the validator
    strict so a test double or future adapter cannot accidentally turn a
    partial lifecycle observation into authority to release the lane.
    """

    if not isinstance(value, Mapping) or set(value) != _QUIESCENCE_KEYS:
        raise RuntimeError("governed_transport_quiescence_invalid")
    if value.get("status") != "verified":
        raise RuntimeError("governed_transport_quiescence_unverified")
    counts: dict[str, int] = {}
    for key in ("active_operations", "unsettled_operations", "requests_started", "requests_settled"):
        count = value.get(key)
        if type(count) is not int or count < 0:
            raise RuntimeError("governed_transport_quiescence_invalid")
        counts[key] = count
    if counts["active_operations"] != 0 or counts["unsettled_operations"] != 0:
        raise RuntimeError("governed_transport_quiescence_unverified")
    if counts["requests_started"] != counts["requests_settled"]:
        raise RuntimeError("governed_transport_quiescence_unverified")
    return {"status": "verified", **counts}


async def write_server_cleanup_proof(
    db: Any,
    *,
    occurrence_id: str,
    scheduled_run_id: str,
    claim_token: str,
    fencing_token: int,
    transport_quiescence: Mapping[str, Any],
    failure_code: str | None = None,
    owner_principal_id: str | None = None,
    owner_session_id: str | None = None,
    transport_origin: str = "adapter_quiescence",
    now_utc: datetime | None = None,
) -> GovernedScheduleOccurrence:
    """Write one server-owned cleanup proof and release an old claim.

    This is an internal scheduler seam.  It accepts no operator supplied
    cleanup flag and never checks the current session as authority: the
    scheduler already owns the historical run that made the provider call.
    A later pause/revoke or binding revision therefore cannot prevent cleanup
    of that old invocation, while all future slots still use their current
    authority fences.  The proof and the occurrence CAS commit in one
    serialized transaction; a worker crash before commit leaves the lane
    quarantined as ``unknown``.

    A verified adapter quiescence receipt proves only local read transport
    settlement.  The fixed observer may also pass the separate
    ``server_no_contact_before_adapter`` origin when it failed before an
    adapter could be constructed.  The old occurrence is consequently
    settled to ``blocked`` (never ``succeeded``), and no task/model/cost
    result is fabricated.
    """

    occurrence_key = str(occurrence_id or "").strip()
    run_key = str(scheduled_run_id or "").strip()
    token = str(claim_token or "").strip()
    if not occurrence_key or not run_key or not token or type(fencing_token) is not int or fencing_token <= 0:
        raise ValueError("governed cleanup identity is incomplete")
    if transport_origin not in _CLEANUP_ORIGINS:
        raise ValueError("governed cleanup origin is invalid")
    if not str(owner_principal_id or "").strip() or not str(owner_session_id or "").strip():
        raise RuntimeError("governed_occurrence_cleanup_owner_required")
    quiescence = _validated_transport_quiescence(transport_quiescence)
    now = _utc(now_utc or _now())
    await _begin_serialized(db)
    occurrence = await db.get(GovernedScheduleOccurrence, occurrence_key)
    if occurrence is None:
        raise RuntimeError("governed_occurrence_cleanup_missing")
    binding = await db.get(GovernedScheduleBinding, occurrence.binding_id)
    if binding is None:
        raise RuntimeError("governed_occurrence_cleanup_binding_missing")
    if not _action_pair_is_supported(binding.action_type, binding.capability_id):
        raise RuntimeError("governed_occurrence_cleanup_action_unavailable")
    if str(owner_principal_id) != str(binding.owner_principal_id):
        raise RuntimeError("governed_occurrence_cleanup_owner_mismatch")
    if str(owner_session_id) != str(binding.owner_session_id):
        raise RuntimeError("governed_occurrence_cleanup_owner_mismatch")
    if occurrence.durable_job_id != run_key:
        raise RuntimeError("occurrence_cleanup_run_mismatch")
    job = await db.get(ScheduledJob, binding.scheduled_job_id)
    if job is None or job.trigger_type != "governed" or not _action_pair_is_supported(job.action_type, binding.capability_id):
        raise RuntimeError("governed_occurrence_cleanup_action_unavailable")
    run = await db.get(ScheduledJobRun, run_key)
    if (
        run is None
        or run.scheduled_job_id != binding.scheduled_job_id
        or run.action_type != binding.action_type
        or run.trigger_type != job.trigger_type
    ):
        raise RuntimeError("occurrence_cleanup_proof_unavailable")
    if occurrence.claim_token != token or occurrence.fencing_token != fencing_token:
        # A committed proof/CAS clears the claim token.  A retry may consume
        # that exact terminal receipt, but cannot mint another timestamp or
        # release a different occurrence.
        try:
            prior_metadata = json.loads(run.metadata_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            prior_metadata = None
        prior_proof = prior_metadata.get("governed_cleanup_proof") if isinstance(prior_metadata, dict) else None
        if occurrence.state == "blocked" and occurrence.claim_token is None and isinstance(prior_proof, dict):
            if (
                set(prior_proof)
                == {
                    "status",
                    "run_id",
                    "occurrence_id",
                    "binding_id",
                    "binding_revision",
                    "claim_token",
                    "fencing_token",
                    "owner_principal_id",
                    "owner_session_id",
                    "verified_at",
                    "transport_quiescence",
                }
                and prior_proof.get("status") == "verified"
                and prior_proof.get("run_id") == run.id
                and prior_proof.get("occurrence_id") == occurrence.occurrence_id
                and prior_proof.get("binding_id") == occurrence.binding_id
                and prior_proof.get("binding_revision") == occurrence.binding_revision
                and prior_proof.get("claim_token") == token
                and prior_proof.get("fencing_token") == fencing_token
                and prior_proof.get("owner_principal_id") == binding.owner_principal_id
                and prior_proof.get("owner_session_id") == binding.owner_session_id
                and prior_proof.get("transport_quiescence") == quiescence
                and prior_metadata.get("governed_cleanup_origin") == transport_origin
                and isinstance(prior_proof.get("verified_at"), str)
            ):
                try:
                    prior_verified_at = _utc(
                        datetime.fromisoformat(str(prior_proof["verified_at"]).replace("Z", "+00:00"))
                    )
                except (TypeError, ValueError):
                    prior_verified_at = None
                if prior_verified_at is not None and prior_verified_at <= _now():
                    return occurrence
        raise RuntimeError("occurrence_cleanup_proof_mismatch")

    try:
        metadata = json.loads(run.metadata_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        raise RuntimeError("occurrence_cleanup_proof_invalid")
    if not isinstance(metadata, dict):
        raise RuntimeError("occurrence_cleanup_proof_invalid")

    verified_at = _iso(now)
    proof = {
        "status": "verified",
        "run_id": run.id,
        "occurrence_id": occurrence.occurrence_id,
        "binding_id": occurrence.binding_id,
        "binding_revision": occurrence.binding_revision,
        "claim_token": token,
        "fencing_token": fencing_token,
        "owner_principal_id": binding.owner_principal_id,
        "owner_session_id": binding.owner_session_id,
        "verified_at": verified_at,
        "transport_quiescence": quiescence,
    }
    previous_proof = metadata.get("governed_cleanup_proof")
    if previous_proof is not None:
        if not isinstance(previous_proof, dict):
            raise RuntimeError("governed_occurrence_cleanup_receipt_conflict")
        for key, value in proof.items():
            if key == "verified_at":
                continue
            if previous_proof.get(key) != value:
                raise RuntimeError("governed_occurrence_cleanup_receipt_conflict")
        if set(previous_proof) != set(proof):
            raise RuntimeError("governed_occurrence_cleanup_receipt_conflict")
        raise RuntimeError("governed_occurrence_cleanup_receipt_conflict")
    metadata["governed_cleanup_proof"] = proof
    metadata["governed_cleanup_status"] = "verified"
    metadata["governed_cleanup_recovery"] = "blocked"
    metadata["governed_cleanup_origin"] = transport_origin
    run.metadata_json = _json(metadata)

    # A request failure normally arrives while the claim is still running.  A
    # timeout/exception first becomes unknown in this same transaction and is
    # then released only after the verified proof is present.  If a prior
    # transaction already persisted unknown, the second CAS is the only write.
    if occurrence.state == "running":
        unknown_metadata = {
            "failure_code": failure_code,
            "recovery_action": "reconcile_external_effect",
        }
        running_result = await db.execute(
            update(GovernedScheduleOccurrence)
            .where(
                GovernedScheduleOccurrence.occurrence_id == occurrence.occurrence_id,
                GovernedScheduleOccurrence.state == "running",
                GovernedScheduleOccurrence.claim_token == token,
                GovernedScheduleOccurrence.fencing_token == fencing_token,
            )
            .values(
                state="unknown",
                metadata_json=_json(unknown_metadata),
                updated_at=now,
            )
        )
        if running_result.rowcount != 1:
            raise RuntimeError("governed_occurrence_cleanup_stale")
    elif occurrence.state != "unknown":
        if occurrence.state in TERMINAL_OCCURRENCE_STATES:
            raise RuntimeError("governed_occurrence_cleanup_terminal")
        raise RuntimeError("governed_occurrence_cleanup_state_invalid")

    result = await db.execute(
        update(GovernedScheduleOccurrence)
        .where(
            GovernedScheduleOccurrence.occurrence_id == occurrence.occurrence_id,
            GovernedScheduleOccurrence.state == "unknown",
            GovernedScheduleOccurrence.binding_revision == occurrence.binding_revision,
            GovernedScheduleOccurrence.claim_token == token,
            GovernedScheduleOccurrence.fencing_token == fencing_token,
        )
        .values(
            state="blocked",
            lease_expires_at=None,
            claim_token=None,
            metadata_json=_json(
                {
                    "failure_code": failure_code,
                    "recovery_action": "reconciled_cleanup_verified",
                    "cleanup_proof_run_id": run.id,
                    "cleanup_verified": True,
                    "cleanup_verified_at": verified_at,
                    "cleanup_transport": quiescence,
                }
            ),
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        raise RuntimeError("governed_occurrence_cleanup_stale")
    await db.refresh(occurrence)
    return occurrence


async def create_binding(db: Any, owner: WorkBoardOwner, request: Mapping[str, Any]) -> GovernedScheduleBinding:
    """Create a shared binding row with registry and idempotency validation."""
    principal = str(owner.principal_id or "").strip()
    session = str(owner.session_id or "").strip()
    if not principal or not session:
        raise ValueError("governed schedule owner is unavailable")
    await _begin_serialized(db)
    action = str(request.get("action_type") or GOVERNED_ACTION)
    spec = action_spec(action)
    if str(request.get("capability_id") or spec["capability_id"]) != spec["capability_id"]:
        raise ValueError("governed action/capability pair is invalid")
    cadence = normalize_cadence(request.get("cadence") or {})
    expires_at = request.get("expires_at")
    if not isinstance(expires_at, datetime):
        raise ValueError("governed schedule expiry is required")
    expires_at = _utc(expires_at)
    key = str(request.get("idempotency_key") or request.get("schedule_idempotency_key") or "").strip()
    if not key:
        raise ValueError("governed schedule idempotency key is required")
    goal_id = str(request.get("goal_id") or "").strip()
    goal_revision = int(request.get("goal_revision") or 0)
    consent_id = str(request.get("consent_id") or request.get("read_consent_id") or "").strip()
    artifact_id = str(request.get("input_artifact_id") or "").strip()
    input_digest = str(request.get("input_digest") or "").strip()
    if not goal_id or goal_revision < 1 or not artifact_id or not input_digest:
        raise ValueError("governed schedule binding fields are incomplete")
    if action == GOVERNED_ACTION and not consent_id:
        raise ValueError("calendar governed schedule consent is required")
    if action == PROCEDURE_ACTION:
        # Goal-budget schedules deliberately carry no Calendar read consent.
        consent_id = ""
    request_digest = str(request.get("schedule_request_digest") or "sha256:" + _digest(dict(request)))
    existing = (
        await db.execute(
            select(GovernedScheduleBinding).where(
                GovernedScheduleBinding.owner_principal_id == principal,
                GovernedScheduleBinding.owner_session_id == session,
                GovernedScheduleBinding.schedule_idempotency_key == key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.schedule_request_digest != request_digest:
            raise RuntimeError("governed_schedule_idempotency_conflict")
        return existing
    count = int(
        (
            await db.execute(
                select(func.count(GovernedScheduleBinding.binding_id)).where(
                    GovernedScheduleBinding.owner_principal_id == principal,
                    GovernedScheduleBinding.state.in_(("active", "paused")),
                )
            )
        ).scalar_one()
        or 0
    )
    if count >= _MAX_BINDINGS:
        raise RuntimeError("governed_schedule_capacity_exceeded")
    scheduled_job_id = str(request.get("scheduled_job_id") or "").strip()
    job = None
    if scheduled_job_id:
        job = await db.get(ScheduledJob, scheduled_job_id)
        if job is None:
            raise ValueError("governed schedule job is unavailable")
    else:
        job = ScheduledJob(
            name=str(
                request.get("name")
                or (
                    "Gmail metadata watch"
                    if action == "gmail.scan_metadata.v1"
                    else "Calendar observation"
                    if action == GOVERNED_ACTION
                    else "Reviewed procedure"
                )
            )[:200],
            enabled=True,
            trigger_type="governed",
            trigger_spec_json=json.dumps(cadence, separators=(",", ":")),
            action_type=action,
            action_spec_json=json.dumps({"binding_id": "pending"}, separators=(",", ":")),
        )
        db.add(job)
        await db.flush()
    binding = GovernedScheduleBinding(
        scheduled_job_id=job.id,
        owner_principal_id=principal,
        owner_session_id=session,
        goal_id=goal_id,
        goal_revision=goal_revision,
        capability_id=spec["capability_id"],
        action_type=action,
        input_artifact_id=artifact_id,
        input_digest=input_digest,
        action_digest=str(request.get("action_digest") or "sha256:" + _digest({"action": action, "input": input_digest})),
        consent_kind=spec["consent_kind"],
        read_consent_id=consent_id or None,
        consent_revision=int(request.get("consent_revision") or 1),
        consent_digest=str(request.get("consent_digest") or ""),
        schedule_idempotency_key=key,
        schedule_request_digest=request_digest,
        cadence_kind=cadence["kind"],
        timezone=cadence["timezone"],
        daily_hour=cadence["daily_hour"],
        daily_minute=cadence["daily_minute"],
        binding_revision=1,
        expires_at=expires_at,
        state="active",
    )
    db.add(binding)
    await db.flush()
    job.action_spec_json = json.dumps({"binding_id": binding.binding_id}, separators=(",", ":"))
    await db.flush()
    return binding


async def apply_control(
    db: Any,
    owner: WorkBoardOwner,
    binding_id: str,
    action: str,
    expected_revision: int,
    idempotency_key: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Apply one schedule control and return its immutable public receipt.

    ``GovernedScheduleBinding`` predates durable control receipts and still
    contains a few one-entry cache columns.  They are intentionally ignored
    here.  Every accepted control is recorded as an immutable
    ``ScheduledJobRun`` metadata receipt, so a later A -> B -> A retry can
    return A's original response without changing the current binding.
    """
    normalized_action = str(action or "").strip()
    if normalized_action not in {"pause", "resume", "revoke"}:
        raise ValueError("governed schedule control is invalid")
    normalized_key = str(idempotency_key or "").strip()
    if (
        type(expected_revision) is not int
        or expected_revision < 1
        or not normalized_key
        or len(normalized_key) > _CONTROL_KEY_MAX
    ):
        raise ValueError("governed schedule control is incomplete")
    normalized_reason = None if reason is None else str(reason)
    if normalized_reason is not None and len(normalized_reason) > 500:
        raise ValueError("governed schedule control reason is too long")
    await _begin_serialized(db)
    if not owner.principal_id or not owner.session_id:
        raise RuntimeError("governed_schedule_session_unavailable")
    session = await db.get(OperatorSession, owner.session_id)
    now = _now()
    if (
        session is None
        or session.revoked_at is not None
        or _utc(session.idle_expires_at) <= now
        or _utc(session.absolute_expires_at) <= now
    ):
        raise RuntimeError("governed_schedule_session_unavailable")
    row = (
        await db.execute(
            select(GovernedScheduleBinding).where(
                GovernedScheduleBinding.binding_id == binding_id,
                GovernedScheduleBinding.owner_principal_id == owner.principal_id,
                GovernedScheduleBinding.owner_session_id == owner.session_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise LookupError("governed schedule is unavailable")
    job = await db.get(ScheduledJob, row.scheduled_job_id)
    if job is None:
        raise RuntimeError("governed_schedule_job_unavailable")
    request_digest = "sha256:" + _digest(
        {
            "action": normalized_action,
            "expected_revision": expected_revision,
            "idempotency_key": normalized_key,
            "reason": normalized_reason,
        }
    )

    # Resolve the append-only receipt by its deterministic primary key before
    # checking the current binding revision.  This is what makes an exact old
    # intent replayable after newer controls have advanced the binding without
    # scanning unbounded run history.
    receipt_id = _control_receipt_id(
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        scheduled_job_id=row.scheduled_job_id,
        idempotency_key=normalized_key,
    )
    expected_identity = {
        "kind": "governed_control",
        "receipt_id": receipt_id,
        "binding_id": row.binding_id,
        "scheduled_job_id": row.scheduled_job_id,
        "owner_principal_id": owner.principal_id,
        "owner_session_id": owner.session_id,
        "action": normalized_action,
        "expected_binding_revision": expected_revision,
        "idempotency_key": normalized_key,
        "request_digest": request_digest,
    }
    receipt = await db.get(ScheduledJobRun, receipt_id)
    if receipt is not None:
        if (
            receipt.scheduled_job_id != row.scheduled_job_id
            or receipt.action_type != _CONTROL_ACTION_TYPE
            or receipt.session_id != owner.session_id
            or receipt.created_by_session_id != owner.session_id
        ):
            raise RuntimeError("governed_schedule_control_receipt_invalid")
        try:
            metadata = json.loads(receipt.metadata_json or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = None
        if not isinstance(metadata, dict) or metadata.get("schema_version") != 1:
            raise RuntimeError("governed_schedule_control_receipt_invalid")
        identity = metadata.get("control")
        if not isinstance(identity, dict):
            raise RuntimeError("governed_schedule_control_receipt_invalid")
        if identity != expected_identity:
            raise RuntimeError("governed_schedule_idempotency_conflict")
        response = metadata.get("response")
        next_state = "paused" if normalized_action == "pause" else "active" if normalized_action == "resume" else "revoked"
        return _validate_control_response(
            response,
            binding_id=row.binding_id,
            scheduled_job_id=row.scheduled_job_id,
            expected_revision=expected_revision,
            expected_state=next_state,
        )

    if row.binding_revision != expected_revision:
        raise RuntimeError("governed_schedule_revision_stale")
    if row.state in {"revoked", "expired"}:
        raise RuntimeError("governed_schedule_not_active")
    next_state = "paused" if normalized_action == "pause" else "active" if normalized_action == "resume" else "revoked"
    result = await db.execute(
        update(GovernedScheduleBinding)
        .where(
            GovernedScheduleBinding.binding_id == binding_id,
            GovernedScheduleBinding.owner_principal_id == owner.principal_id,
            GovernedScheduleBinding.owner_session_id == owner.session_id,
            GovernedScheduleBinding.binding_revision == expected_revision,
        )
        .values(
            state=next_state,
            binding_revision=expected_revision + 1,
            updated_at=_now(),
        )
    )
    if result.rowcount != 1:
        raise RuntimeError("governed_schedule_revision_stale")
    await db.execute(
        update(ScheduledJob)
        .where(ScheduledJob.id == row.scheduled_job_id)
        .values(enabled=next_state == "active", updated_at=_now())
    )
    await db.refresh(row)
    latest_occurrence = (
        await db.execute(
            select(GovernedScheduleOccurrence)
            .where(GovernedScheduleOccurrence.binding_id == row.binding_id)
            .order_by(GovernedScheduleOccurrence.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    response = serialize_binding(row, latest_occurrence)
    next_state = "paused" if normalized_action == "pause" else "active" if normalized_action == "resume" else "revoked"
    response = _validate_control_response(
        response,
        binding_id=row.binding_id,
        scheduled_job_id=row.scheduled_job_id,
        expected_revision=expected_revision,
        expected_state=next_state,
    )
    receipt_metadata = json.dumps(
        {
            "schema_version": 1,
            "control": expected_identity,
            "response": response,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(receipt_metadata.encode("utf-8")) > _CONTROL_RECEIPT_MAX_BYTES:
        raise RuntimeError("governed_schedule_control_receipt_too_large")
    receipt = ScheduledJobRun(
        id=receipt_id,
        scheduled_job_id=row.scheduled_job_id,
        job_name=job.name,
        trigger_type=job.trigger_type,
        action_type=_CONTROL_ACTION_TYPE,
        session_id=owner.session_id,
        created_by_session_id=owner.session_id,
        status="succeeded",
        outcome="succeeded",
        started_at=now,
        finished_at=_now(),
        metadata_json=receipt_metadata,
    )
    db.add(receipt)
    await db.flush()
    return response


async def create_observation_input_artifact(
    db: Any,
    owner: WorkBoardOwner,
    *,
    consent: CalendarReadConsent,
    connection_id: str,
    idempotency_key: str,
):
    payload = {
        "schema_version": 1,
        "consent_id": consent.consent_id,
        "connection_id": connection_id,
        "goal_id": consent.goal_id,
        "goal_revision": consent.goal_revision,
        "max_events_per_scan": _MAX_OBSERVATIONS,
    }
    validate_capability_input(GOVERNED_ACTION, payload, allow_scheduler=True)
    request = WorkBoardInputArtifactCreate(
        schema_version=1,
        capability_id=GOVERNED_ACTION,
        goal_id=consent.goal_id,
        goal_revision=consent.goal_revision,
        input=payload,
        idempotency_key=idempotency_key,
    )
    return await prepare_input_artifact(db, owner, request, allow_scheduler=True)


async def create_mail_watch_input_artifact(
    db: Any,
    owner: WorkBoardOwner,
    *,
    consent: MailReadConsent,
    connection_id: str,
    label_ids: list[str],
    max_messages: int,
    idempotency_key: str,
):
    """Create the immutable metadata-only input for one Mail watch.

    Provider identities remain encrypted in the Mail connection/label rows;
    this scheduler input carries only the reviewed opaque label references and
    the source/Goal revisions that must match again before every scan.
    """

    payload = {
        "schema_version": 1,
        "consent_id": consent.consent_id,
        "connection_id": connection_id,
        "goal_id": consent.goal_id,
        "goal_revision": int(consent.goal_revision),
        "source_consent_revision": int(consent.source_revision),
        "label_ids": list(label_ids),
        "window_days": 7,
        "max_messages": int(max_messages),
    }
    validate_capability_input("gmail.scan_metadata.v1", payload, allow_scheduler=True)
    request = WorkBoardInputArtifactCreate(
        schema_version=1,
        capability_id="gmail.scan_metadata.v1",
        goal_id=consent.goal_id,
        goal_revision=int(consent.goal_revision),
        input=payload,
        idempotency_key=idempotency_key,
    )
    return await prepare_input_artifact(db, owner, request, allow_scheduler=True)


__all__ = [
    "ACTION_REGISTRY",
    "ALLOWED_CADENCES",
    "GOVERNED_ACTION",
    "PROCEDURE_ACTION",
    "action_spec",
    "apply_control",
    "claim_occurrence",
    "create_binding",
    "create_observation_input_artifact",
    "create_mail_watch_input_artifact",
    "cron_for_cadence",
    "is_governed_action",
    "latest_due_slot",
    "normalize_cadence",
    "reconcile_occurrence",
    "reserve_occurrence",
    "serialize_binding",
    "serialize_occurrence",
    "settle_occurrence",
    "write_server_cleanup_proof",
]
