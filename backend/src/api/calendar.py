"""Authenticated, owner-bound Google Calendar setup and preparation routes."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Mapping

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select, update

from src.auth.service import AuthenticatedOperator
from src.db.engine import get_session
from src.db.models import CalendarEventBinding, CalendarReadConsent, Goal, GoogleServiceConnection, GovernedScheduleBinding, GovernedScheduleOccurrence, OperatorSession, ScheduledJob
from src.integrations.google_calendar import (
    CalendarIntegrationError,
    GoogleCalendarReadonlyAdapter,
    digest,
    persist_calendar_event_binding,
)
from src.integrations.calendar_controls import (
    CONTROL_REVOKE_CONNECTION,
    CONTROL_REVOKE_CONSENT,
    CONTROL_VERIFY,
    CONTROL_MAX_RUNTIME_SECONDS,
    CalendarControlError,
    CalendarControlExecution,
    CalendarControlLease,
    CalendarControlRequest,
    control_job_id,
    find_verified_setup,
    run_control,
)
from src.vault import decrypt, encrypt, vault_repository
from src.work_board.contracts import WorkBoardInputArtifactCreate, WorkBoardOwner, WorkBoardTaskCreate
from src.work_board.dispatcher import CalendarMeetingPrepInput
from src.work_board.input_artifacts import prepare_input_artifact, revoke_input_artifact
from src.work_board.repository import BoardError, WorkBoardRepository
from src.scheduler.governed_schedules import (
    _begin_serialized,
    apply_control,
    create_binding,
    create_observation_input_artifact,
    normalize_cadence,
    serialize_binding,
)


router = APIRouter()
from src.api.calendar_reschedule import router as reschedule_router
router.include_router(reschedule_router)
repository = WorkBoardRepository()
_MAX_SETUP_BYTES = 16 * 1024
_ALLOWED_FIELDS = frozenset({"summary", "start", "end", "location", "description", "attendees"})
_DEFAULT_FIELDS = frozenset({"summary", "start", "end", "location"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CALENDAR_CONSENT_CREATE_LOCK = asyncio.Lock()
_CALENDAR_CONNECTION_CREATE_LOCK = asyncio.Lock()


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class ConnectionCreate(_Strict):
    schema_version: Literal[1]
    service: Literal["calendar_readonly"]
    label: str = Field(min_length=1, max_length=200)
    client_id: str = Field(min_length=1, max_length=4096)
    client_secret: str | None = Field(default=None, max_length=4096)
    refresh_token: str = Field(min_length=1, max_length=8192)
    idempotency_key: str = Field(min_length=1, max_length=256)


class ConsentCreate(_Strict):
    schema_version: Literal[1]
    connection_id: str = Field(min_length=1, max_length=256)
    calendar_id: str = Field(min_length=1, max_length=1024)
    goal_id: str = Field(min_length=1, max_length=256)
    goal_revision: int = Field(ge=1)
    allowed_fields: list[str] = Field(default_factory=lambda: list(_DEFAULT_FIELDS), max_length=6)
    window_minutes: int = Field(ge=5, le=1440)
    max_events: int = Field(ge=1, le=50)
    allow_remote_model: bool
    expires_at: datetime
    idempotency_key: str = Field(min_length=1, max_length=256)

    @field_validator("expires_at")
    @classmethod
    def require_aware_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must include an explicit timezone")
        return value

    @field_validator("allowed_fields")
    @classmethod
    def validate_fields(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(values))
        if not normalized or any(value not in _ALLOWED_FIELDS for value in normalized):
            raise ValueError("allowed_fields contains an unsupported field")
        if "summary" not in normalized or "start" not in normalized or "end" not in normalized:
            raise ValueError("allowed_fields must include summary, start, and end")
        return normalized


class VerifyCreate(_Strict):
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)


class ConnectionControl(_Strict):
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)


class ConsentControl(_Strict):
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)


class PrepCreate(_Strict):
    schema_version: Literal[1]
    input: CalendarMeetingPrepInput
    title: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=256)


class ScheduleCreate(_Strict):
    consent_id: str = Field(min_length=1, max_length=256)
    goal_id: str = Field(min_length=1, max_length=256)
    goal_revision: int = Field(ge=1)
    calendar_id: str = Field(min_length=1, max_length=1024)
    cadence: dict[str, Any]
    expires_at: datetime
    idempotency_key: str = Field(min_length=1, max_length=256)

    @field_validator("expires_at")
    @classmethod
    def require_aware_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must include an explicit timezone")
        return value


class SchedulePatch(_Strict):
    expected_binding_revision: int = Field(ge=1)
    action: Literal["pause", "resume"]
    idempotency_key: str = Field(min_length=1, max_length=256)


class ScheduleRevoke(_Strict):
    expected_binding_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=500)


def _safe_error(exc: CalendarIntegrationError | BoardError | Exception, *, status_code: int | None = None) -> HTTPException:
    code = getattr(exc, "code", "calendar_internal_error")
    message = str(exc)[:500] if isinstance(exc, (CalendarIntegrationError, BoardError)) else "Calendar operation failed"
    recovery = getattr(exc, "recovery_action", None)
    if recovery is None and isinstance(getattr(exc, "extra", None), dict):
        recovery = exc.extra.get("recovery_action")
    return HTTPException(status_code=status_code or int(getattr(exc, "status_code", 500) or 500), detail={"code": str(code)[:128], "message": message, "recovery_action": recovery})


def _operator(request: Request) -> AuthenticatedOperator:
    operator = getattr(request.state, "operator", None)
    principal = getattr(operator, "principal", None)
    session_id = str(getattr(operator, "session_id", "") or "").strip()
    principal_id = str(getattr(principal, "principal_id", "") or "").strip()
    if not isinstance(operator, AuthenticatedOperator) or not principal_id or not session_id:
        raise HTTPException(status_code=401, detail={"code": "authentication_required", "message": "Authentication is required", "recovery_action": "login"})
    if not bool(getattr(principal, "authenticated", False)) or bool(getattr(principal, "revoked", False)) or str(getattr(principal, "session_id", "") or "") != session_id:
        raise HTTPException(status_code=401, detail={"code": "session_unavailable", "message": "The operator session is unavailable", "recovery_action": "login"})
    return operator


def _owner(operator: AuthenticatedOperator) -> WorkBoardOwner:
    return WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)


def _credential_value_allowed(value: str) -> bool:
    """Reject explicit transport/file references while preserving opaque tokens."""
    normalized = value.strip()
    lowered = normalized.casefold()
    if not normalized or _CONTROL.search(normalized):
        return False
    if lowered.startswith(("http://", "https://", "file://")):
        return False
    if normalized.startswith(("/", "\\", "~/")):
        return False
    if re.match(r"^[A-Za-z]:[\\/]", normalized):
        return False
    return True


def _connection_request_digest(body: ConnectionCreate) -> str:
    """Hash the complete normalized request without retaining credential text."""

    return "sha256:" + digest(
        {
            "schema_version": 1,
            "service": body.service,
            "label": body.label,
            "client_id": body.client_id,
            "client_secret": body.client_secret,
            "refresh_token": body.refresh_token,
            "idempotency_key": body.idempotency_key,
        }
    )


def _connection_credential_fingerprint(body: ConnectionCreate) -> str:
    return "sha256:" + digest(
        {
            "client_id": body.client_id,
            "client_secret": bool(body.client_secret),
            "refresh_token": digest(body.refresh_token),
        }
    )


def _vault_value(body: ConnectionCreate) -> str:
    return json.dumps(
        {
            key: value
            for key, value in {
                "client_id": body.client_id,
                "client_secret": body.client_secret,
                "refresh_token": body.refresh_token,
            }.items()
            if value is not None
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _vault_value_matches(raw: str | None, body: ConnectionCreate) -> bool:
    """Validate a recovered secret without exposing it or accepting a guess."""

    if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_SETUP_BYTES:
        return False
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError):
        return False
    if not isinstance(value, dict):
        return False
    expected = {
        key: item
        for key, item in {
            "client_id": body.client_id,
            "client_secret": body.client_secret,
            "refresh_token": body.refresh_token,
        }.items()
        if item is not None
    }
    if value != expected:
        return False
    return _connection_credential_fingerprint(body) == "sha256:" + digest(
        {
            "client_id": value.get("client_id"),
            "client_secret": bool(value.get("client_secret")),
            "refresh_token": digest(value.get("refresh_token")),
        }
    )


async def _compensate_vault_secret(key: str) -> bool:
    """Delete and read back a possibly committed secret after setup failure."""

    try:
        current = await vault_repository.get(key)
        if current is not None:
            await vault_repository.delete(key)
        return await vault_repository.get(key) is None
    except Exception:
        return False


async def _compensate_connection_secret(
    *,
    connection_id: str,
    owner: WorkBoardOwner,
    key: str,
) -> bool:
    """Compensate only while our durable row is still unclaimed.

    A second process may have completed the same owner/key reservation while
    the original request was unwinding.  An active row is the winner's
    durable claim; deleting its vault key would turn an uncertain failure into
    credential loss.
    """

    # Reserve cleanup under the same SQLite writer fence used by setup and
    # recovery.  A plain read followed by a vault delete has a cross-process
    # race: a recovery worker could activate the row after the read and lose
    # its winning credential when the failed request deletes the key.  The
    # blocked_cleanup state is deliberately a durable tombstone: recovery
    # refuses to activate it, so the external delete is performed while the
    # owner/key reservation is fenced.
    async with get_session() as db:
        await _begin_serialized(db)
        row = (
            await db.execute(
                select(GoogleServiceConnection).where(
                    GoogleServiceConnection.connection_id == connection_id,
                    GoogleServiceConnection.owner_principal_id == owner.principal_id,
                    GoogleServiceConnection.owner_session_id == owner.session_id,
                )
            )
        ).scalar_one_or_none()
        if row is not None and row.state == "active":
            return True
        if row is not None and row.state not in {"preparing", "blocked", "blocked_cleanup"}:
            return False
        if row is not None and row.state != "blocked_cleanup":
            row.state = "blocked_cleanup"
            row.revision += 1
            row.updated_at = _now()
            await db.flush()

    cleanup_ok = await _compensate_vault_secret(key)
    if not cleanup_ok:
        return False

    # Keep the durable row explicitly recoverable after a verified delete.
    # A crash before this transaction commits leaves blocked_cleanup, which is
    # the safe retry state; it never silently returns to preparing.
    async with get_session() as db:
        await _begin_serialized(db)
        row = (
            await db.execute(
                select(GoogleServiceConnection).where(
                    GoogleServiceConnection.connection_id == connection_id,
                    GoogleServiceConnection.owner_principal_id == owner.principal_id,
                    GoogleServiceConnection.owner_session_id == owner.session_id,
                )
            )
        ).scalar_one_or_none()
        if row is not None and row.state == "active":
            # A manually completed winner is authoritative.  The normal
            # recovery path cannot make this transition from blocked_cleanup,
            # but preserve it if an external reconciler already did so.
            return True
        if row is not None and row.state == "blocked_cleanup":
            row.state = "blocked"
            row.revision += 1
            row.updated_at = _now()
            await db.flush()
    return True


def _preparing_setup_is_stale(created_at: datetime) -> bool:
    """Give an in-flight setup one bounded control window before cleanup.

    A fresh ``preparing`` row may belong to another process that is between
    its durable reservation and vault store/CAS.  A retry during that window
    must not delete a live store.  Once the existing 30-second control window
    has elapsed, a retry owns only reconciliation: missing material becomes
    ``blocked`` and invalid material is tombstoned before deletion.
    """

    return _now() - _aware(created_at) > timedelta(seconds=CONTROL_MAX_RUNTIME_SECONDS)


async def _reconcile_stale_preparing_connection(
    *,
    connection_id: str,
    owner: WorkBoardOwner,
    key: str,
) -> dict[str, Any] | None:
    """Reconcile stale setup material without deleting an active winner."""

    cleanup_ok = await _compensate_connection_secret(
        connection_id=connection_id,
        owner=owner,
        key=key,
    )
    async with get_session() as db:
        await _begin_serialized(db)
        row = (
            await db.execute(
                select(GoogleServiceConnection).where(
                    GoogleServiceConnection.connection_id == connection_id,
                    GoogleServiceConnection.owner_principal_id == owner.principal_id,
                    GoogleServiceConnection.owner_session_id == owner.session_id,
                )
            )
        ).scalar_one_or_none()
        if row is not None and row.state == "active":
            # A concurrent worker completed the exact reservation.  Its
            # active row is authoritative and its vault material is retained.
            return {"connection": _metadata(row)}
        if row is not None and row.state == "blocked_cleanup":
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "calendar_connection_cleanup_blocked",
                    "message": "Calendar credentials require cleanup reconciliation",
                    "recovery_action": "retry_cleanup",
                },
            )
        if not cleanup_ok:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "calendar_connection_cleanup_blocked",
                    "message": "Calendar credentials require cleanup reconciliation",
                    "recovery_action": "retry_cleanup",
                },
            )
    raise _verify_reconciliation_error(message="The existing Calendar setup requires reconciliation")


async def _mark_connection_state(
    *,
    connection_id: str,
    owner: WorkBoardOwner,
    state: str,
) -> None:
    async with get_session() as db:
        row = (
            await db.execute(
                select(GoogleServiceConnection).where(
                    GoogleServiceConnection.connection_id == connection_id,
                    GoogleServiceConnection.owner_principal_id == owner.principal_id,
                    GoogleServiceConnection.owner_session_id == owner.session_id,
                )
            )
        ).scalar_one_or_none()
        if row is not None and row.state != "active":
            row.state = state
            row.updated_at = _now()
            await db.flush()


async def _json_body(request: Request, model: type[BaseModel], *, max_bytes: int = _MAX_SETUP_BYTES) -> BaseModel:
    try:
        length = request.headers.get("content-length")
        if length is not None and (int(length) < 0 or int(length) > max_bytes):
            raise HTTPException(status_code=413, detail={"code": "calendar_request_too_large", "message": "Calendar setup request exceeds the bounded limit", "recovery_action": "reduce_request"})
    except ValueError:
        raise HTTPException(status_code=422, detail={"code": "calendar_request_invalid", "message": "Calendar request is invalid", "recovery_action": "correct_request"})
    body_buffer = bytearray()
    try:
        async for chunk in request.stream():
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise HTTPException(status_code=422, detail={"code": "calendar_request_invalid", "message": "Calendar request is invalid", "recovery_action": "correct_request"})
            if len(body_buffer) + len(chunk) > max_bytes:
                # Stop consuming the ASGI receive stream as soon as the bound
                # is crossed.  Credentials in later chunks are never parsed,
                # stored, or passed to a provider/vault path.
                raise HTTPException(status_code=413, detail={"code": "calendar_request_too_large", "message": "Calendar setup request exceeds the bounded limit", "recovery_action": "reduce_request"})
            body_buffer.extend(chunk)
    except HTTPException:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail={"code": "calendar_request_invalid", "message": "Calendar request is invalid", "recovery_action": "correct_request"}) from exc
    body = bytes(body_buffer)
    try:
        # Pydantic's JSON parser keeps strict scalar validation while still
        # applying the JSON-to-datetime coercion required by the public
        # RFC3339 request fields.  ``model_validate`` on a Python dict would
        # reject those otherwise valid timestamp strings in strict mode.
        return model.model_validate_json(body)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
        # Never let Pydantic echo a write-only credential in a validation
        # trace.  The route owns the safe error envelope.
        raise HTTPException(status_code=422, detail={"code": "calendar_request_invalid", "message": "Calendar request is invalid", "recovery_action": "correct_request"}) from exc


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _metadata(connection: GoogleServiceConnection) -> dict[str, Any]:
    return {
        "connection_id": connection.connection_id,
        "service": "calendar_readonly",
        "label": connection.label,
        "credential_fingerprint": connection.credential_fingerprint,
        "state": connection.state,
        "revision": connection.revision,
        "created_at": _aware(connection.created_at).isoformat().replace("+00:00", "Z"),
        "updated_at": _aware(connection.updated_at).isoformat().replace("+00:00", "Z"),
    }


def _consent_metadata(consent: CalendarReadConsent) -> dict[str, Any]:
    return {
        "consent_id": consent.consent_id,
        "connection_id": consent.connection_id,
        "connection_revision": consent.connection_revision,
        "goal_id": consent.goal_id,
        "goal_revision": consent.goal_revision,
        "allowed_fields": json.loads(consent.allowed_fields_json or "[]"),
        "window_minutes": consent.window_minutes,
        "max_events": consent.max_events,
        "allow_remote_model": consent.allow_remote_model,
        "expires_at": _aware(consent.expires_at).isoformat().replace("+00:00", "Z"),
        "state": consent.state,
        "revision": consent.revision,
        "consent_digest": consent.consent_digest,
        "created_at": _aware(consent.created_at).isoformat().replace("+00:00", "Z"),
        "updated_at": _aware(consent.updated_at).isoformat().replace("+00:00", "Z"),
    }


async def _connection_for(db, owner: WorkBoardOwner, connection_id: str) -> GoogleServiceConnection:
    connection = (
        await db.execute(
            select(GoogleServiceConnection)
            .where(
                GoogleServiceConnection.connection_id == connection_id,
                GoogleServiceConnection.owner_principal_id == owner.principal_id,
                GoogleServiceConnection.owner_session_id == owner.session_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if connection is None:
        raise CalendarIntegrationError("calendar_connection_not_found", "The Calendar connection is unavailable", status_code=404)
    return connection


async def _assert_live_operator_session(db, owner: WorkBoardOwner) -> None:
    """Recheck the persisted session at Calendar control boundaries."""

    session = (
        await db.execute(
            select(OperatorSession)
            .where(OperatorSession.id == owner.session_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if session is None:
        raise CalendarControlError(
            "authentication_required",
            "Authentication is required",
            status_code=401,
            recovery_action="login",
        )
    if session.revoked_at is not None:
        raise CalendarControlError(
            "session_revoked",
            "The operator session is unavailable",
            status_code=401,
            recovery_action="login",
        )
    if _aware(session.idle_expires_at) <= _now() or _aware(session.absolute_expires_at) <= _now():
        raise CalendarControlError(
            "session_expired",
            "The operator session is unavailable",
            status_code=401,
            recovery_action="login",
        )


def _verify_reconciliation_error(*, message: str = "Calendar verification requires reconciliation") -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "calendar_connection_reconciliation_required",
            "message": message,
            "recovery_action": "reconcile_existing_verification",
        },
    )


def _control_http_error(exc: CalendarControlError) -> HTTPException:
    """Expose one bounded error envelope for the durable control routes."""

    return HTTPException(
        status_code=int(exc.status_code or 409),
        detail={
            "code": str(exc.code)[:128],
            "message": str(exc)[:500],
            "recovery_action": exc.recovery_action,
        },
    )


def _post_contact_control_error(exc: Exception) -> CalendarControlError:
    """Collapse provider-path failures into the durable reconciliation code.

    Once a control has entered the adapter, the operator cannot safely infer
    that no provider request or credential operation occurred.  Reusing the
    pre-contact revision/availability codes would invite the UI to mint a new
    key and contact the provider again.
    """

    return CalendarControlError(
        "calendar_control_reconciliation_required",
        "Calendar control contact requires reconciliation",
        status_code=409,
        recovery_action="reconcile_existing_control",
        uncertain=True,
    )


async def _tombstone_input_artifact(
    owner: WorkBoardOwner,
    artifact_id: str,
    *,
    db: Any | None = None,
    error_code: str,
    message: str,
    recovery_action: str,
) -> None:
    """Revoke an unbound input artifact after a publication aborts.

    Artifact reservation and schedule binding are separate durable writes.  A
    failed binding therefore must leave the input row tombstoned before the
    route exposes the original validation/capacity error.  If the cleanup
    itself cannot be proven, fail with an explicit reconciliation state rather
    than leaving executable owner-private bytes behind.
    """

    # A caller may still hold a SQLite writer transaction when it discovers
    # the failure. Roll it back before opening the cleanup session; otherwise
    # the cleanup writer would wait behind the caller until the exception
    # unwinds, leaving the artifact executable during the recovery window.
    if db is not None and db.in_transaction():
        await db.rollback()
    try:
        async with get_session() as cleanup_db:
            await revoke_input_artifact(cleanup_db, owner, artifact_id=artifact_id)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": error_code,
                "message": message,
                "recovery_action": recovery_action,
            },
        ) from exc


async def _tombstone_schedule_artifact(
    owner: WorkBoardOwner,
    artifact_id: str,
    *,
    db: Any | None = None,
) -> None:
    await _tombstone_input_artifact(
        owner,
        artifact_id,
        db=db,
        error_code="calendar_schedule_artifact_cleanup_required",
        message="The failed Calendar schedule input requires cleanup reconciliation",
        recovery_action="reconcile_schedule_artifact",
    )


def _control_request_digest(
    *,
    operation: str,
    target_id: str,
    expected_revision: int,
    idempotency_key: str,
) -> str:
    return "sha256:" + digest(
        {
            "schema_version": 1,
            "operation": operation,
            "target_id": target_id,
            "expected_revision": int(expected_revision),
            "idempotency_key": idempotency_key,
        }
    )


async def _control_connection_preflight(
    db,
    owner: WorkBoardOwner,
    connection_id: str,
    expected_revision: int,
) -> GoogleServiceConnection:
    await _assert_live_operator_session(db, owner)
    connection = await _connection_for(db, owner, connection_id)
    if connection.state != "active":
        raise CalendarControlError(
            "calendar_connection_unavailable",
            "The Calendar connection is not active",
            status_code=409,
            recovery_action="restore_prerequisite",
        )
    if connection.revision != expected_revision:
        raise CalendarControlError(
            "calendar_connection_revision_stale",
            "The Calendar connection changed",
            status_code=409,
            recovery_action="reload_connection",
        )
    return connection


async def _control_connection_exists(
    db,
    owner: WorkBoardOwner,
    connection_id: str,
) -> GoogleServiceConnection:
    """Resolve owner scope before durable replay without requiring active state."""

    await _assert_live_operator_session(db, owner)
    return await _connection_for(db, owner, connection_id)


async def _control_consent_preflight(
    db,
    owner: WorkBoardOwner,
    consent_id: str,
    expected_revision: int,
) -> CalendarReadConsent:
    await _assert_live_operator_session(db, owner)
    consent = (
        await db.execute(
            select(CalendarReadConsent).where(
                CalendarReadConsent.consent_id == consent_id,
                CalendarReadConsent.owner_principal_id == owner.principal_id,
                CalendarReadConsent.owner_session_id == owner.session_id,
            )
        )
    ).scalar_one_or_none()
    if consent is None:
        raise CalendarControlError(
            "calendar_consent_not_found",
            "The Calendar consent is unavailable",
            status_code=404,
        )
    if consent.state != "active":
        raise CalendarControlError(
            "calendar_consent_unavailable",
            "The Calendar consent is no longer active",
            status_code=409,
            recovery_action="create_new_consent",
        )
    if consent.revision != expected_revision:
        raise CalendarControlError(
            "calendar_consent_revision_stale",
            "The Calendar consent changed",
            status_code=409,
            recovery_action="reload_consent",
        )
    return consent


async def _control_consent_exists(
    db,
    owner: WorkBoardOwner,
    consent_id: str,
) -> CalendarReadConsent:
    await _assert_live_operator_session(db, owner)
    consent = (
        await db.execute(
            select(CalendarReadConsent).where(
                CalendarReadConsent.consent_id == consent_id,
                CalendarReadConsent.owner_principal_id == owner.principal_id,
                CalendarReadConsent.owner_session_id == owner.session_id,
            )
        )
    ).scalar_one_or_none()
    if consent is None:
        raise CalendarControlError(
            "calendar_consent_not_found",
            "The Calendar consent is unavailable",
            status_code=404,
        )
    return consent


def _verified_calendar_ids(result: Mapping[str, Any]) -> set[str]:
    calendars = result.get("calendars")
    if not isinstance(calendars, list) or result.get("provider_status") != "verified":
        return set()
    return {
        str(item.get("calendar_id"))
        for item in calendars
        if isinstance(item, Mapping)
        and isinstance(item.get("calendar_id"), str)
        and item.get("calendar_id")
    }


@router.post("/calendar/connections")
async def create_connection(request: Request):
    # Hold the process-local lock through the vault side effect.  The durable
    # BEGIN IMMEDIATE reservation below remains the cross-process fence, and
    # a cross-process replay only reads the existing vault key.
    async with _CALENDAR_CONNECTION_CREATE_LOCK:
        return await _create_connection_locked(request)


async def _create_connection_locked(request: Request):
    operator = _operator(request)
    body = await _json_body(request, ConnectionCreate)
    credential_values = [body.client_id, body.refresh_token]
    if body.client_secret is not None:
        credential_values.append(body.client_secret)
    if any(not _credential_value_allowed(value) for value in credential_values):
        raise HTTPException(status_code=422, detail={"code": "calendar_request_invalid", "message": "Calendar credential material is invalid", "recovery_action": "correct_request"})
    request_digest = _connection_request_digest(body)
    credential_fingerprint = _connection_credential_fingerprint(body)
    owner = _owner(operator)

    existing_connection_id: str | None = None
    existing_secret_key: str | None = None
    existing_preparing_stale = False
    async with get_session() as db:
        await _begin_serialized(db)
        try:
            await _assert_live_operator_session(db, owner)
        except CalendarControlError as exc:
            raise _control_http_error(exc) from exc
        existing = (
            await db.execute(
                select(GoogleServiceConnection).where(
                    GoogleServiceConnection.owner_principal_id == owner.principal_id,
                    GoogleServiceConnection.owner_session_id == owner.session_id,
                    GoogleServiceConnection.setup_idempotency_key == body.idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.setup_request_digest != request_digest:
                raise HTTPException(status_code=409, detail={"code": "calendar_idempotency_conflict", "message": "The setup key is bound to another request", "recovery_action": "use_new_idempotency_key"})
            if existing.state == "active":
                return {"connection": _metadata(existing)}
            if existing.state == "blocked_cleanup":
                raise HTTPException(status_code=503, detail={"code": "calendar_connection_cleanup_blocked", "message": "Calendar credentials require cleanup reconciliation", "recovery_action": "retry_cleanup"})
            if existing.state == "blocked":
                raise _verify_reconciliation_error(message="The existing Calendar setup is blocked and requires reconciliation")
            if existing.state != "preparing":
                raise _verify_reconciliation_error(message="The existing Calendar setup requires reconciliation")
            # Leave the transaction before reading the vault.  This avoids
            # nesting the vault repository's independent DB session inside a
            # SQLite write transaction and never performs a second store.
            existing_connection_id = existing.connection_id
            existing_secret_key = existing.vault_secret_key
            existing_preparing_stale = _preparing_setup_is_stale(existing.created_at)
        else:
            connection = GoogleServiceConnection(
                owner_principal_id=owner.principal_id,
                owner_session_id=owner.session_id,
                service="calendar_readonly",
                label=body.label,
                vault_secret_key=f"calendar:{owner.principal_id}:{secrets.token_urlsafe(18)}",
                credential_fingerprint=credential_fingerprint,
                setup_idempotency_key=body.idempotency_key,
                setup_request_digest=request_digest,
                state="preparing",
                revision=1,
            )
            db.add(connection)
            try:
                await db.flush()
            except Exception as exc:
                # A server-side unique-key race can occur before either
                # process sees the other reservation. Re-read the canonical
                # owner/key row after rollback and continue through the same
                # exact-replay/recovery path; never expose the raw constraint
                # error or store a second secret.
                from sqlalchemy.exc import IntegrityError

                if not isinstance(exc, IntegrityError):
                    raise
                await db.rollback()
                raced = (
                    await db.execute(
                        select(GoogleServiceConnection).where(
                            GoogleServiceConnection.owner_principal_id == owner.principal_id,
                            GoogleServiceConnection.owner_session_id == owner.session_id,
                            GoogleServiceConnection.setup_idempotency_key == body.idempotency_key,
                        )
                    )
                ).scalar_one_or_none()
                if raced is None:
                    raise HTTPException(
                        status_code=503,
                        detail={
                            "code": "calendar_connection_reconciliation_required",
                            "message": "Calendar setup requires reconciliation",
                            "recovery_action": "reconcile_existing_setup",
                        },
                    ) from exc
                if raced.setup_request_digest != request_digest:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "calendar_idempotency_conflict",
                            "message": "The setup key is bound to another request",
                            "recovery_action": "use_new_idempotency_key",
                        },
                    ) from exc
                if raced.state == "active":
                    return {"connection": _metadata(raced)}
                if raced.state == "blocked_cleanup":
                    raise HTTPException(
                        status_code=503,
                        detail={
                            "code": "calendar_connection_cleanup_blocked",
                            "message": "Calendar credentials require cleanup reconciliation",
                            "recovery_action": "retry_cleanup",
                        },
                    ) from exc
                if raced.state == "blocked":
                    raise _verify_reconciliation_error(
                        message="The existing Calendar setup is blocked and requires reconciliation"
                    ) from exc
                if raced.state != "preparing":
                    raise _verify_reconciliation_error(
                        message="The existing Calendar setup requires reconciliation"
                    ) from exc
                existing_connection_id = raced.connection_id
                existing_secret_key = raced.vault_secret_key
                existing_preparing_stale = _preparing_setup_is_stale(raced.created_at)
            connection_id = connection.connection_id
            secret_key = connection.vault_secret_key

    if existing_connection_id is not None and existing_secret_key is not None:
        if not existing_preparing_stale:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "calendar_connection_reconciliation_required",
                    "message": "Calendar setup is still settling",
                    "recovery_action": "retry_setup",
                },
            )
        try:
            recovered = await vault_repository.get(existing_secret_key)
        except Exception as exc:
            # A stale unreadable key is reconciled through the fenced cleanup
            # path. It either returns a concurrent active winner or raises a
            # bounded blocked/reconciliation error.
            return await _reconcile_stale_preparing_connection(
                connection_id=existing_connection_id,
                owner=owner,
                key=existing_secret_key,
            )
        if not _vault_value_matches(recovered, body):
            return await _reconcile_stale_preparing_connection(
                connection_id=existing_connection_id,
                owner=owner,
                key=existing_secret_key,
            )
        async with get_session() as db:
            await _begin_serialized(db)
            try:
                await _assert_live_operator_session(db, owner)
            except CalendarControlError as exc:
                raise _control_http_error(exc) from exc
            row = (
                await db.execute(
                    select(GoogleServiceConnection).where(
                        GoogleServiceConnection.connection_id == existing_connection_id,
                        GoogleServiceConnection.owner_principal_id == owner.principal_id,
                        GoogleServiceConnection.owner_session_id == owner.session_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None or row.setup_request_digest != request_digest:
                raise _verify_reconciliation_error(message="The existing Calendar setup requires reconciliation")
            if row.state == "active":
                return {"connection": _metadata(row)}
            if row.state != "preparing":
                raise _verify_reconciliation_error(message="The existing Calendar setup requires reconciliation")
            row.state = "active"
            row.revision += 1
            row.updated_at = _now()
            await db.flush()
            return {"connection": _metadata(row)}

    vault_value = _vault_value(body)
    try:
        await vault_repository.store(secret_key, vault_value, description="Google Calendar read-only connection")
    except asyncio.CancelledError:
        cleanup_ok = await _compensate_connection_secret(connection_id=connection_id, owner=owner, key=secret_key)
        await _mark_connection_state(
            connection_id=connection_id,
            owner=owner,
            state="blocked" if cleanup_ok else "blocked_cleanup",
        )
        if not cleanup_ok:
            raise HTTPException(status_code=503, detail={"code": "calendar_connection_cleanup_blocked", "message": "Calendar credentials require cleanup reconciliation", "recovery_action": "retry_cleanup"})
        raise
    except Exception as exc:
        cleanup_ok = await _compensate_connection_secret(connection_id=connection_id, owner=owner, key=secret_key)
        await _mark_connection_state(
            connection_id=connection_id,
            owner=owner,
            state="blocked" if cleanup_ok else "blocked_cleanup",
        )
        if not cleanup_ok:
            raise HTTPException(status_code=503, detail={"code": "calendar_connection_cleanup_blocked", "message": "Calendar credentials require cleanup reconciliation", "recovery_action": "retry_cleanup"}) from exc
        raise _safe_error(exc, status_code=503)

    async def _activate_after_store() -> JSONResponse:
        session_error: CalendarControlError | None = None
        completed_payload: dict[str, Any] | None = None
        activated_by_this_request = False
        needs_compensation = False
        async with get_session() as db:
            await _begin_serialized(db)
            row = (
                await db.execute(
                    select(GoogleServiceConnection).where(
                        GoogleServiceConnection.connection_id == connection_id,
                        GoogleServiceConnection.owner_principal_id == owner.principal_id,
                        GoogleServiceConnection.owner_session_id == owner.session_id,
                    )
                )
            ).scalar_one_or_none()
            try:
                await _assert_live_operator_session(db, owner)
            except CalendarControlError as exc:
                session_error = exc
            if session_error is None and row is not None and row.state == "active" and row.setup_request_digest == request_digest:
                # Another process completed the same reservation. Preserve its
                # winner and return a normal replay rather than deleting its key.
                completed_payload = {"connection": _metadata(row)}
            elif session_error is None and row is not None and row.state == "preparing" and row.setup_request_digest == request_digest:
                row.state = "active"
                row.revision += 1
                row.updated_at = _now()
                await db.flush()
                completed_payload = {"connection": _metadata(row)}
                activated_by_this_request = True
            else:
                needs_compensation = True

        if session_error is not None:
            # The row is still ours unless a concurrent worker already
            # completed it. The compensation helper checks that fence before
            # deleting.
            cleanup_ok = await _compensate_connection_secret(connection_id=connection_id, owner=owner, key=secret_key)
            await _mark_connection_state(connection_id=connection_id, owner=owner, state="blocked" if cleanup_ok else "blocked_cleanup")
            if not cleanup_ok:
                raise HTTPException(status_code=503, detail={"code": "calendar_connection_cleanup_blocked", "message": "Calendar credentials require cleanup reconciliation", "recovery_action": "retry_cleanup"}) from session_error
            raise _control_http_error(session_error)
        if needs_compensation:
            cleanup_ok = await _compensate_connection_secret(connection_id=connection_id, owner=owner, key=secret_key)
            await _mark_connection_state(connection_id=connection_id, owner=owner, state="blocked" if cleanup_ok else "blocked_cleanup")
            if not cleanup_ok:
                raise HTTPException(status_code=503, detail={"code": "calendar_connection_cleanup_blocked", "message": "Calendar credentials require cleanup reconciliation", "recovery_action": "retry_cleanup"})
            raise _verify_reconciliation_error(message="Calendar setup requires reconciliation")
        assert completed_payload is not None
        # Only the request that wins the preparing -> active CAS created the
        # connection.  An observed active winner is an exact replay and must
        # use 200 even when this request performed the vault store first.
        return JSONResponse(content=completed_payload, status_code=201 if activated_by_this_request else 200)

    try:
        return await _activate_after_store()
    except asyncio.CancelledError:
        # Cancellation after the vault commit must not leave a durable
        # preparing row and an untracked credential. The helper preserves a
        # concurrent active winner and marks cleanup explicitly when deletion
        # cannot be proven.
        cleanup_ok = await _compensate_connection_secret(connection_id=connection_id, owner=owner, key=secret_key)
        await _mark_connection_state(connection_id=connection_id, owner=owner, state="blocked" if cleanup_ok else "blocked_cleanup")
        if not cleanup_ok:
            raise HTTPException(status_code=503, detail={"code": "calendar_connection_cleanup_blocked", "message": "Calendar credentials require cleanup reconciliation", "recovery_action": "retry_cleanup"})
        raise


@router.get("/calendar/connections")
async def list_connections(request: Request):
    operator = _operator(request)
    owner = _owner(operator)
    async with get_session() as db:
        try:
            await _assert_live_operator_session(db, owner)
        except CalendarControlError as exc:
            raise _control_http_error(exc) from exc
        rows = (await db.execute(select(GoogleServiceConnection).where(GoogleServiceConnection.owner_principal_id == owner.principal_id, GoogleServiceConnection.owner_session_id == owner.session_id).order_by(GoogleServiceConnection.created_at.desc()).limit(50))).scalars().all()
        return {"connections": [_metadata(row) for row in rows]}


@router.post("/calendar/connections/{connection_id}/verify")
async def verify_connection(request: Request, connection_id: str):
    """Verify one connection through a durable, replay-safe control root."""

    operator = _operator(request)
    body = await _json_body(request, VerifyCreate)
    owner = _owner(operator)
    request_digest = _control_request_digest(
        operation=CONTROL_VERIFY,
        target_id=connection_id,
        expected_revision=body.expected_revision,
        idempotency_key=body.idempotency_key,
    )
    try:
        async with get_session() as db:
            await _control_connection_exists(db, owner, connection_id)
    except CalendarIntegrationError as exc:
        raise _safe_error(exc) from exc
    except CalendarControlError as exc:
        raise _control_http_error(exc) from exc

    control = CalendarControlRequest(
        operation=CONTROL_VERIFY,
        target_id=connection_id,
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        expected_revision=body.expected_revision,
        idempotency_key=body.idempotency_key,
        request_digest=request_digest,
    )

    async def execute(lease: CalendarControlLease) -> CalendarControlExecution:
        try:
            async with get_session() as db:
                try:
                    current = await _control_connection_preflight(
                        db,
                        owner,
                        connection_id,
                        body.expected_revision,
                    )
                except CalendarIntegrationError as exc:
                    raise CalendarControlError(
                        exc.code,
                        str(exc),
                        status_code=exc.status_code,
                        recovery_action=exc.recovery_action,
                        uncertain=False,
                    ) from exc
                connection_snapshot = _metadata(current)
        except CalendarControlError:
            raise
        except Exception as exc:
            raise CalendarControlError(
                "calendar_connection_reconciliation_required",
                "Calendar verification requires reconciliation",
                status_code=409,
                recovery_action="reconcile_existing_control",
                uncertain=False,
            ) from exc

        async def assert_authority() -> None:
            try:
                async with get_session() as db:
                    await _control_connection_preflight(
                        db,
                        owner,
                        connection_id,
                        body.expected_revision,
                    )
            except CalendarIntegrationError as exc:
                raise _post_contact_control_error(exc) from exc
            except CalendarControlError as exc:
                # The adapter invokes this guard at every provider boundary;
                # after entering that path, a stale authority result is
                # reconciled rather than exposed as a fresh-retry code.
                raise _post_contact_control_error(exc) from exc

        adapter = GoogleCalendarReadonlyAdapter(
            current,
            owner_principal_id=owner.principal_id,
            authority_check=assert_authority,
        )
        try:
            calendars, revision = await adapter.list_calendars()
        except CalendarIntegrationError as exc:
            raise _post_contact_control_error(exc) from exc
        payload = {
            "connection": connection_snapshot,
            "calendars": calendars[:50],
            "calendar_list_revision": revision.digest,
            "pages_read": revision.pages_read,
            "truncated": revision.truncated,
            "provider_status": "verified",
        }
        return CalendarControlExecution(
            payload=payload,
            details={
                "provider": "google_calendar",
                "calendar_list_revision": revision.digest,
                "pages_read": revision.pages_read,
                "truncated": revision.truncated,
            },
        )

    try:
        payload = await run_control(control, execute)
    except CalendarControlError as exc:
        raise _control_http_error(exc) from exc

    # The durable job is already terminal and has the encrypted proof.  This
    # opaque link is only a fast owner-private index; GET still revalidates the
    # artifact/readback pair and the live connection revision.
    async with get_session() as db:
        await _begin_serialized(db)
        try:
            current = await _control_connection_preflight(
                db,
                owner,
                connection_id,
                body.expected_revision,
            )
        except CalendarIntegrationError as exc:
            raise _safe_error(exc) from exc
        except CalendarControlError as exc:
            raise _control_http_error(exc) from exc
        current.verified_setup_job_id = control_job_id(control)
        current.updated_at = _now()
        await db.flush()
    return payload


@router.delete("/calendar/connections/{connection_id}")
async def revoke_connection(request: Request, connection_id: str):
    """Revoke local Calendar authority through one durable control root."""

    operator = _operator(request)
    body = await _json_body(request, ConnectionControl)
    owner = _owner(operator)
    request_digest = _control_request_digest(
        operation=CONTROL_REVOKE_CONNECTION,
        target_id=connection_id,
        expected_revision=body.expected_revision,
        idempotency_key=body.idempotency_key,
    )
    try:
        async with get_session() as db:
            await _control_connection_exists(db, owner, connection_id)
    except CalendarIntegrationError as exc:
        raise _safe_error(exc) from exc
    except CalendarControlError as exc:
        raise _control_http_error(exc) from exc

    control = CalendarControlRequest(
        operation=CONTROL_REVOKE_CONNECTION,
        target_id=connection_id,
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        expected_revision=body.expected_revision,
        idempotency_key=body.idempotency_key,
        request_digest=request_digest,
    )

    async def execute(lease: CalendarControlLease) -> CalendarControlExecution:
        async with get_session() as db:
            await _begin_serialized(db)
            try:
                connection = await _control_connection_preflight(
                    db,
                    owner,
                    connection_id,
                    body.expected_revision,
                )
            except CalendarIntegrationError as exc:
                raise CalendarControlError(
                    exc.code,
                    str(exc),
                    status_code=exc.status_code,
                    recovery_action=exc.recovery_action,
                    uncertain=False,
                ) from exc
            vault_key = connection.vault_secret_key
            connection.state = "revoked"
            connection.revision += 1
            connection.verified_setup_job_id = None
            connection.updated_at = _now()
            await db.execute(
                update(CalendarReadConsent)
                .where(
                    CalendarReadConsent.connection_id == connection_id,
                    CalendarReadConsent.owner_principal_id == owner.principal_id,
                    CalendarReadConsent.owner_session_id == owner.session_id,
                    CalendarReadConsent.state.in_(("active", "expired")),
                )
                .values(
                    state="revoked",
                    revision=CalendarReadConsent.revision + 1,
                    updated_at=_now(),
                )
            )
            await db.execute(
                update(CalendarEventBinding)
                .where(
                    CalendarEventBinding.connection_id == connection_id,
                    CalendarEventBinding.owner_principal_id == owner.principal_id,
                    CalendarEventBinding.owner_session_id == owner.session_id,
                    CalendarEventBinding.state != "revoked",
                )
                .values(
                    state="revoked",
                    revision=CalendarEventBinding.revision + 1,
                    updated_at=_now(),
                )
            )
            binding_ids = (
                await db.execute(
                    select(GovernedScheduleBinding.binding_id).where(
                        GovernedScheduleBinding.owner_principal_id == owner.principal_id,
                        GovernedScheduleBinding.owner_session_id == owner.session_id,
                        GovernedScheduleBinding.read_consent_id.in_(
                            select(CalendarReadConsent.consent_id).where(
                                CalendarReadConsent.connection_id == connection_id,
                                CalendarReadConsent.owner_principal_id == owner.principal_id,
                                CalendarReadConsent.owner_session_id == owner.session_id,
                            )
                        ),
                        GovernedScheduleBinding.state != "revoked",
                    )
                )
            ).scalars().all()
            if binding_ids:
                await db.execute(
                    update(GovernedScheduleBinding)
                    .where(GovernedScheduleBinding.binding_id.in_(binding_ids))
                    .values(
                        state="revoked",
                        binding_revision=GovernedScheduleBinding.binding_revision + 1,
                        updated_at=_now(),
                    )
                )
                await db.execute(
                    update(ScheduledJob)
                    .where(
                        ScheduledJob.id.in_(
                            select(GovernedScheduleBinding.scheduled_job_id).where(
                                GovernedScheduleBinding.binding_id.in_(binding_ids)
                            )
                        )
                    )
                    .values(enabled=False, updated_at=_now())
                )
                # Revoke the binding and disable future scheduler claims,
                # but retain every occurrence row.  Reserved, running, and
                # unknown rows are still the global lane's durable occupancy
                # until the worker records verified cleanup; bulk-cancelling
                # them here would permit overlapping provider work after a
                # local revoke.
            await db.flush()
            metadata = _metadata(connection)
        try:
            await vault_repository.delete(vault_key)
        except Exception as exc:
            async with get_session() as db:
                await _begin_serialized(db)
                failed = await _connection_for(db, owner, connection_id)
                failed.state = "blocked_cleanup"
                failed.updated_at = _now()
                await db.flush()
            raise CalendarControlError(
                "calendar_connection_cleanup_blocked",
                "Calendar credentials require cleanup reconciliation",
                status_code=503,
                recovery_action="retry_cleanup",
                uncertain=True,
            ) from exc
        return CalendarControlExecution(
            payload={"connection": metadata},
            details={"provider_contact": False, "vault_cleanup": "verified"},
        )

    try:
        return await run_control(control, execute)
    except CalendarControlError as exc:
        raise _control_http_error(exc) from exc


@router.get("/calendar/connections/{connection_id}/calendars")
async def list_calendars(request: Request, connection_id: str):
    operator = _operator(request)
    owner = _owner(operator)
    async with get_session() as db:
        try:
            await _assert_live_operator_session(db, owner)
            connection = await _connection_for(db, owner, connection_id)
        except CalendarIntegrationError as exc:
            raise _safe_error(exc) from exc
        except CalendarControlError as exc:
            raise _control_http_error(exc) from exc
        if connection.state != "active":
            raise _verify_reconciliation_error(message="The Calendar connection is not active")
    try:
        replay = await find_verified_setup(
            owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,
            connection_id=connection_id,
            connection_revision=connection.revision,
        )
    except CalendarControlError as exc:
        raise _control_http_error(exc) from exc
    if replay is not None:
        async with get_session() as db:
            try:
                await _assert_live_operator_session(db, owner)
            except CalendarControlError as exc:
                raise _control_http_error(exc) from exc
        return replay
    raise _verify_reconciliation_error(message="Verify the Calendar connection before reading calendars")


@router.post("/calendar/read-consents")
async def create_consent(request: Request):
    # SQLite's test and single-process deployments use a shared connection
    # pool; keep the serialized transaction from colliding with another
    # coroutine on that connection.  The database BEGIN IMMEDIATE fence below
    # remains the cross-process boundary.
    async with _CALENDAR_CONSENT_CREATE_LOCK:
        return await _create_consent_locked(request)


async def _create_consent_locked(request: Request):
    operator = _operator(request)
    body = await _json_body(request, ConsentCreate)
    owner = _owner(operator)
    expires_at = _aware(body.expires_at)
    now = _now()
    if expires_at <= now or expires_at > now + timedelta(days=7):
        raise HTTPException(status_code=422, detail={"code": "calendar_consent_expiry_invalid", "message": "Calendar consent expiry is outside the bounded window", "recovery_action": "choose_bounded_expiry"})
    async with get_session() as db:
        # Calendar consent creation has no model-level unique constraint on
        # legacy databases.  Use the existing SQLite serialized-write fence
        # around the owner/session/key lookup and insert so concurrent retries
        # cannot create two rows.  The canonical model migration remains the
        # cross-dialect uniqueness backstop.
        await _begin_serialized(db)
        try:
            await _assert_live_operator_session(db, owner)
        except CalendarControlError as exc:
            raise _control_http_error(exc) from exc
        connection = await _connection_for(db, owner, body.connection_id)
        if connection.state != "active":
            raise HTTPException(status_code=409, detail={"code": "calendar_connection_unavailable", "message": "The Calendar connection is not active", "recovery_action": "restore_prerequisite"})
        await repository._validate_goal(db, owner, goal_id=body.goal_id, goal_revision=body.goal_revision)
        allowed = list(dict.fromkeys(body.allowed_fields))
        request_digest = "sha256:" + digest({"schema_version": 1, "connection_id": connection.connection_id, "connection_revision": connection.revision, "calendar_id": body.calendar_id, "goal_id": body.goal_id, "goal_revision": body.goal_revision, "allowed_fields": allowed, "window_minutes": body.window_minutes, "max_events": body.max_events, "allow_remote_model": body.allow_remote_model, "expires_at": expires_at.isoformat(), "idempotency_key": body.idempotency_key})
        existing = (await db.execute(select(CalendarReadConsent).where(CalendarReadConsent.owner_principal_id == owner.principal_id, CalendarReadConsent.owner_session_id == owner.session_id, CalendarReadConsent.creation_idempotency_key == body.idempotency_key))).scalar_one_or_none()
        if existing is not None:
            if existing.creation_request_digest != request_digest:
                raise HTTPException(status_code=409, detail={"code": "calendar_consent_idempotency_conflict", "message": "The consent key is bound to another request", "recovery_action": "use_new_idempotency_key"})
            return {"consent": _consent_metadata(existing)}
        verified_setup = await find_verified_setup(
            owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,
            connection_id=connection.connection_id,
            connection_revision=connection.revision,
        )
        if verified_setup is None or body.calendar_id not in _verified_calendar_ids(verified_setup):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "calendar_setup_membership_required",
                    "message": "Verify the Calendar connection before selecting this calendar",
                    "recovery_action": "verify_connection",
                },
            )
        try:
            # The verification read and this durable grant are separate
            # transactions; do not publish a consent after session revocation.
            await _assert_live_operator_session(db, owner)
        except CalendarControlError as exc:
            raise _control_http_error(exc) from exc
        consent_id = secrets.token_urlsafe(18)
        consent_digest = "sha256:" + digest({"owner": owner.principal_id, "session": owner.session_id, "connection_id": connection.connection_id, "connection_revision": connection.revision, "calendar_id": body.calendar_id, "goal_id": body.goal_id, "goal_revision": body.goal_revision, "allowed_fields": allowed, "window_minutes": body.window_minutes, "max_events": body.max_events, "allow_remote_model": body.allow_remote_model, "expires_at": expires_at.isoformat()})
        row = CalendarReadConsent(consent_id=consent_id, owner_principal_id=owner.principal_id, owner_session_id=owner.session_id, connection_id=connection.connection_id, connection_revision=connection.revision, creation_idempotency_key=body.idempotency_key, creation_request_digest=request_digest, calendar_id=encrypt(body.calendar_id), goal_id=body.goal_id, goal_revision=body.goal_revision, allowed_fields_json=json.dumps(allowed, separators=(",", ":")), window_minutes=body.window_minutes, max_events=body.max_events, allow_remote_model=body.allow_remote_model, expires_at=expires_at, state="active", revision=1, consent_digest=consent_digest)
        db.add(row)
        await db.flush()
        return JSONResponse(content={"consent": _consent_metadata(row)}, status_code=201)


@router.delete("/calendar/read-consents/{consent_id}")
async def revoke_consent(request: Request, consent_id: str):
    """Revoke one read grant and its scheduled descendants durably."""

    operator = _operator(request)
    body = await _json_body(request, ConsentControl)
    owner = _owner(operator)
    request_digest = _control_request_digest(
        operation=CONTROL_REVOKE_CONSENT,
        target_id=consent_id,
        expected_revision=body.expected_revision,
        idempotency_key=body.idempotency_key,
    )
    try:
        async with get_session() as db:
            await _control_consent_exists(db, owner, consent_id)
    except CalendarControlError as exc:
        raise _control_http_error(exc) from exc

    control = CalendarControlRequest(
        operation=CONTROL_REVOKE_CONSENT,
        target_id=consent_id,
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        expected_revision=body.expected_revision,
        idempotency_key=body.idempotency_key,
        request_digest=request_digest,
    )

    async def execute(lease: CalendarControlLease) -> CalendarControlExecution:
        async with get_session() as db:
            await _begin_serialized(db)
            consent = await _control_consent_preflight(
                db,
                owner,
                consent_id,
                body.expected_revision,
            )
            consent.state = "revoked"
            consent.revision += 1
            consent.updated_at = _now()
            await db.execute(
                update(CalendarEventBinding)
                .where(
                    CalendarEventBinding.consent_id == consent_id,
                    CalendarEventBinding.owner_principal_id == owner.principal_id,
                    CalendarEventBinding.owner_session_id == owner.session_id,
                    CalendarEventBinding.state != "revoked",
                )
                .values(
                    state="revoked",
                    revision=CalendarEventBinding.revision + 1,
                    updated_at=_now(),
                )
            )
            binding_ids = (
                await db.execute(
                    select(GovernedScheduleBinding.binding_id).where(
                        GovernedScheduleBinding.owner_principal_id == owner.principal_id,
                        GovernedScheduleBinding.owner_session_id == owner.session_id,
                        GovernedScheduleBinding.read_consent_id == consent_id,
                        GovernedScheduleBinding.state != "revoked",
                    )
                )
            ).scalars().all()
            if binding_ids:
                await db.execute(
                    update(GovernedScheduleBinding)
                    .where(GovernedScheduleBinding.binding_id.in_(binding_ids))
                    .values(
                        state="revoked",
                        binding_revision=GovernedScheduleBinding.binding_revision + 1,
                        updated_at=_now(),
                    )
                )
                await db.execute(
                    update(ScheduledJob)
                    .where(
                        ScheduledJob.id.in_(
                            select(GovernedScheduleBinding.scheduled_job_id).where(
                                GovernedScheduleBinding.binding_id.in_(binding_ids)
                            )
                        )
                    )
                    .values(enabled=False, updated_at=_now())
                )
                # Keep active and unknown occurrences fenced until the
                # scheduler proves cleanup.  Revocation prevents future
                # claims through the binding/job; it cannot prove that an
                # already-running provider call stopped.
            await db.flush()
            return CalendarControlExecution(
                payload={"consent": _consent_metadata(consent)},
                details={"provider_contact": False, "cascade": "revoked"},
            )

    try:
        return await run_control(control, execute)
    except CalendarControlError as exc:
        raise _control_http_error(exc) from exc



async def _active_consent(db, owner: WorkBoardOwner, consent_id: str) -> tuple[CalendarReadConsent, GoogleServiceConnection]:
    try:
        await _assert_live_operator_session(db, owner)
    except CalendarControlError as exc:
        raise CalendarIntegrationError(
            exc.code,
            str(exc),
            status_code=exc.status_code,
            recovery_action=exc.recovery_action,
        ) from exc
    consent = (
        await db.execute(
            select(CalendarReadConsent)
            .where(
                CalendarReadConsent.consent_id == consent_id,
                CalendarReadConsent.owner_principal_id == owner.principal_id,
                CalendarReadConsent.owner_session_id == owner.session_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if consent is None:
        raise CalendarIntegrationError("calendar_consent_not_found", "The Calendar consent is unavailable", status_code=404)
    if consent.state != "active" or _aware(consent.expires_at) <= _now():
        if consent.state == "active":
            consent.state = "expired"
        raise CalendarIntegrationError("calendar_consent_unavailable", "The Calendar consent is no longer active", status_code=409, recovery_action="create_new_consent")
    connection = await _connection_for(db, owner, consent.connection_id)
    if connection.state != "active" or connection.revision != consent.connection_revision:
        raise CalendarIntegrationError("calendar_connection_revision_stale", "The Calendar connection changed", status_code=409, recovery_action="create_new_consent")
    return consent, connection


def _consented_calendar_id(consent: CalendarReadConsent) -> str:
    """Decrypt a private calendar identity only after the owner fence passed."""
    try:
        value = decrypt(consent.calendar_id)
    except Exception as exc:
        raise CalendarIntegrationError(
            "calendar_consent_unavailable",
            "The Calendar consent material is unavailable",
            status_code=409,
            recovery_action="create_new_consent",
        ) from exc
    if not isinstance(value, str) or not value or len(value) > 1024 or _CONTROL.search(value):
        raise CalendarIntegrationError("calendar_consent_unavailable", "The Calendar consent material is unavailable", status_code=409, recovery_action="create_new_consent")
    return value


@router.get("/calendar/connections/{connection_id}/events")
async def list_events(request: Request, connection_id: str, consent_id: str = Query(...), max_events: int = Query(50, ge=1, le=50)):
    operator = _operator(request)
    owner = _owner(operator)
    try:
        async with get_session() as db:
            consent, connection = await _active_consent(db, owner, consent_id)
            if consent.connection_id != connection_id:
                raise HTTPException(status_code=404, detail={"code": "calendar_consent_not_found", "message": "The Calendar consent is unavailable", "recovery_action": None})
            allowed = set(json.loads(consent.allowed_fields_json or "[]"))
            expected_consent_revision = consent.revision
            expected_connection_revision = connection.revision
    except CalendarIntegrationError as exc:
        raise _safe_error(exc) from exc

    async def assert_authority() -> None:
        try:
            async with get_session() as db:
                current_consent, current_connection = await _active_consent(db, owner, consent_id)
                if (
                    current_consent.connection_id != connection_id
                    or current_consent.revision != expected_consent_revision
                    or current_connection.revision != expected_connection_revision
                ):
                    raise CalendarIntegrationError(
                        "calendar_revision_stale",
                        "Calendar authority changed during the provider read",
                        status_code=409,
                        recovery_action="refresh_event",
                    )
        except (CalendarIntegrationError, CalendarControlError) as exc:
            # Once a provider read has started, a revoked session or changed
            # revision is an uncertain read boundary.  Do not expose a
            # pre-contact retry code that could cause a second provider read.
            raise _post_contact_control_error(exc) from exc

        # The adapter invokes this callback before vault access, token refresh,
        # every provider GET, and every pagination page.  It is the live
        # owner/session fence for the read rather than a one-time preflight.

    adapter = GoogleCalendarReadonlyAdapter(
        connection,
        owner_principal_id=owner.principal_id,
        authority_check=assert_authority,
    )
    try:
        now = _now()
        calendar_id = _consented_calendar_id(consent)
        snapshots, revision = await adapter.list_events(calendar_id, time_min=now, time_max=now + timedelta(minutes=consent.window_minutes), allowed_fields=allowed, max_events=min(max_events, consent.max_events))
        # Do not persist a provider snapshot after the owner/consent fence has
        # changed since the final provider page.
        await assert_authority()
    except CalendarIntegrationError as exc:
        raise _safe_error(exc)
    except CalendarControlError as exc:
        raise _control_http_error(exc)
    events = []
    try:
        async with get_session() as db:
            await _begin_serialized(db)
            # Keep the final authority check and selected-binding publication in
            # one owner transaction.  A session/consent revoke committed before
            # this transaction starts therefore cannot leave a stale selection.
            current_consent, current_connection = await _active_consent(db, owner, consent_id)
            if (
                current_consent.connection_id != connection_id
                or current_consent.revision != expected_consent_revision
                or current_connection.revision != expected_connection_revision
            ):
                raise CalendarIntegrationError(
                    "calendar_revision_stale",
                    "Calendar authority changed during the provider read",
                    status_code=409,
                    recovery_action="refresh_event",
                )
            for snapshot in snapshots:
                existing = await persist_calendar_event_binding(
                    db,
                    owner_principal_id=owner.principal_id,
                    owner_session_id=owner.session_id,
                    connection=current_connection,
                    consent=current_consent,
                    snapshot=snapshot,
                )
                fields = adapter._scrub(snapshot.fields)
                # The envelope has the newly fetched list digest, while each
                # event retains the binding's canonical selection provenance.
                events.append({"event_binding_id": existing.event_binding_id, "event_binding_revision": existing.revision, "event_key": snapshot.event_key, "event_revision": snapshot.event_revision, "calendar_list_revision": existing.calendar_list_revision, "summary": fields.get("summary"), "start": fields.get("start"), "end": fields.get("end"), "location": fields.get("location"), "description": fields.get("description"), "attendees": fields.get("attendees")})
            await _assert_live_operator_session(db, owner)
            return {"events": events[:50], "consent_id": current_consent.consent_id, "consent_revision": current_consent.revision, "connection_revision": current_connection.revision, "calendar_list_revision": revision.digest, "fetched_at": _now().isoformat().replace("+00:00", "Z"), "pages_read": revision.pages_read, "truncated": revision.truncated}
    except CalendarIntegrationError as exc:
        raise _safe_error(exc) from exc
    except CalendarControlError as exc:
        raise _control_http_error(exc) from exc


@router.post("/calendar/prep")
async def create_prep(request: Request):
    operator = _operator(request)
    body = await _json_body(request, PrepCreate)
    inner = body.input
    if not _DIGEST.fullmatch(inner.event_revision.removeprefix("sha256:")) or not _DIGEST.fullmatch(inner.calendar_list_revision.removeprefix("sha256:")):
        raise HTTPException(status_code=422, detail={"code": "calendar_request_invalid", "message": "Calendar revisions are invalid", "recovery_action": "refresh_event"})
    owner = _owner(operator)
    async with get_session() as db:
        try:
            consent, connection = await _active_consent(db, owner, inner.consent_id)
        except CalendarIntegrationError as exc:
            raise _safe_error(exc) from exc
        if not consent.allow_remote_model:
            raise HTTPException(status_code=403, detail={"code": "calendar_remote_model_consent_required", "message": "Remote preparation requires explicit consent", "recovery_action": "create_new_consent"})
        if consent.goal_id != inner.goal_id or consent.goal_revision != inner.goal_revision or consent.revision != inner.expected_consent_revision or connection.revision != inner.expected_connection_revision:
            raise HTTPException(status_code=409, detail={"code": "calendar_revision_stale", "message": "Calendar consent or connection revision is stale", "recovery_action": "refresh_event"})
        binding = (await db.execute(select(CalendarEventBinding).where(CalendarEventBinding.event_binding_id == inner.event_binding_id, CalendarEventBinding.owner_principal_id == owner.principal_id, CalendarEventBinding.owner_session_id == owner.session_id))).scalar_one_or_none()
        if binding is None:
            raise HTTPException(status_code=404, detail={"code": "calendar_event_not_found", "message": "The Calendar event is unavailable", "recovery_action": "refresh_event"})
        try:
            consent_calendar_id = _consented_calendar_id(consent)
            binding_calendar_id = decrypt(str(binding.calendar_id_private or ""))
        except Exception as exc:
            raise HTTPException(status_code=409, detail={"code": "calendar_event_revision_stale", "message": "The Calendar event changed", "recovery_action": "refresh_event"}) from exc
        if (
            binding.state != "selected"
            or binding.connection_id != connection.connection_id
            or binding.connection_revision != connection.revision
            or binding.consent_id != consent.consent_id
            or binding.consent_revision != consent.revision
            or binding_calendar_id != consent_calendar_id
            or binding.revision != inner.expected_event_binding_revision
            or binding.event_revision != inner.event_revision
            or binding.calendar_list_revision != inner.calendar_list_revision
        ):
            raise HTTPException(status_code=409, detail={"code": "calendar_event_revision_stale", "message": "The Calendar event changed", "recovery_action": "refresh_event"})
        await repository._validate_goal(db, owner, goal_id=inner.goal_id, goal_revision=inner.goal_revision)
        try:
            await _assert_live_operator_session(db, owner)
        except CalendarControlError as exc:
            raise _control_http_error(exc) from exc
        typed_input = inner.model_dump(mode="json", exclude_none=True)
        artifact_request = WorkBoardInputArtifactCreate(schema_version=1, capability_id="calendar.meeting-prep.v1", goal_id=inner.goal_id, goal_revision=inner.goal_revision, input=typed_input, idempotency_key=body.idempotency_key)
        try:
            metadata = await prepare_input_artifact(db, owner, artifact_request)
        except BoardError as exc:
            if exc.code == "input_artifact_write_failed":
                # The artifact helper intentionally retains an exact pending
                # owner/digest reservation so a retry can complete its
                # existing row. Never delete a possible winner here.
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": exc.code,
                        "message": "The input artifact could not be written",
                        "recovery_action": "retry_existing_request",
                    },
                ) from exc
            raise _safe_error(exc) from exc
        try:
            # Artifact creation and task publication must not outlive a session
            # revoke or a goal/consent authority change.
            await _begin_serialized(db)
            await _assert_live_operator_session(db, owner)
            await repository._validate_goal(db, owner, goal_id=inner.goal_id, goal_revision=inner.goal_revision)
            current_consent, current_connection = await _active_consent(db, owner, inner.consent_id)
            if current_consent.revision != consent.revision or current_connection.revision != connection.revision:
                raise HTTPException(status_code=409, detail={"code": "calendar_revision_stale", "message": "Calendar consent or connection revision is stale", "recovery_action": "refresh_event"})
        except CalendarIntegrationError as exc:
            await _tombstone_input_artifact(
                owner,
                metadata.artifact_id,
                db=db,
                error_code="calendar_prep_artifact_cleanup_required",
                message="The failed Calendar preparation input requires cleanup reconciliation",
                recovery_action="reconcile_prep_artifact",
            )
            raise _safe_error(exc) from exc
        except CalendarControlError as exc:
            await _tombstone_input_artifact(
                owner,
                metadata.artifact_id,
                db=db,
                error_code="calendar_prep_artifact_cleanup_required",
                message="The failed Calendar preparation input requires cleanup reconciliation",
                recovery_action="reconcile_prep_artifact",
            )
            raise _control_http_error(exc) from exc
        except HTTPException:
            await _tombstone_input_artifact(
                owner,
                metadata.artifact_id,
                db=db,
                error_code="calendar_prep_artifact_cleanup_required",
                message="The failed Calendar preparation input requires cleanup reconciliation",
                recovery_action="reconcile_prep_artifact",
            )
            raise
        task_request = WorkBoardTaskCreate(title=body.title, body=inner.purpose, goal_id=inner.goal_id, goal_revision=inner.goal_revision, status="todo", capability_id="calendar.meeting-prep.v1", input_artifact_id=metadata.artifact_id, priority=50, idempotency_scope="calendar-prep", idempotency_key=body.idempotency_key)

        expected_consent_revision = int(consent.revision)
        expected_connection_revision = int(connection.revision)
        expected_event_binding_revision = int(binding.revision)

        async def assert_publication_authority(publication_db) -> None:
            """Re-read every Calendar authority inside the task writer fence."""

            session = (
                await publication_db.execute(
                    select(OperatorSession)
                    .where(OperatorSession.id == owner.session_id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if (
                session is None
                or session.revoked_at is not None
                or _aware(session.idle_expires_at) <= _now()
                or _aware(session.absolute_expires_at) <= _now()
            ):
                raise CalendarIntegrationError(
                    "session_unavailable",
                    "The operator session is unavailable",
                    status_code=401,
                    recovery_action="login",
                )

            goal = (
                await publication_db.execute(
                    select(Goal)
                    .where(Goal.id == inner.goal_id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            goal_status = getattr(getattr(goal, "status", None), "value", getattr(goal, "status", None))
            if (
                goal is None
                or goal.owner_principal_id != owner.principal_id
                or goal.owner_session_id != owner.session_id
                or int(goal.revision or 1) != int(inner.goal_revision)
                or goal_status != "active"
            ):
                raise CalendarIntegrationError(
                    "calendar_goal_revision_stale",
                    "The Calendar preparation goal changed",
                    status_code=409,
                    recovery_action="refresh_goal",
                )

            current_consent = (
                await publication_db.execute(
                    select(CalendarReadConsent)
                    .where(
                        CalendarReadConsent.consent_id == inner.consent_id,
                        CalendarReadConsent.owner_principal_id == owner.principal_id,
                        CalendarReadConsent.owner_session_id == owner.session_id,
                    )
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if (
                current_consent is None
                or current_consent.state != "active"
                or _aware(current_consent.expires_at) <= _now()
                or not current_consent.allow_remote_model
                or current_consent.connection_id != connection.connection_id
                or int(current_consent.connection_revision) != expected_connection_revision
                or int(current_consent.revision) != expected_consent_revision
                or current_consent.goal_id != inner.goal_id
                or int(current_consent.goal_revision) != int(inner.goal_revision)
            ):
                raise CalendarIntegrationError(
                    "calendar_revision_stale",
                    "Calendar consent changed during preparation publication",
                    status_code=409,
                    recovery_action="refresh_event",
                )

            current_connection = (
                await publication_db.execute(
                    select(GoogleServiceConnection)
                    .where(
                        GoogleServiceConnection.connection_id == connection.connection_id,
                        GoogleServiceConnection.owner_principal_id == owner.principal_id,
                        GoogleServiceConnection.owner_session_id == owner.session_id,
                    )
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if (
                current_connection is None
                or current_connection.state != "active"
                or int(current_connection.revision) != expected_connection_revision
            ):
                raise CalendarIntegrationError(
                    "calendar_revision_stale",
                    "Calendar connection changed during preparation publication",
                    status_code=409,
                    recovery_action="refresh_event",
                )

            current_binding = (
                await publication_db.execute(
                    select(CalendarEventBinding)
                    .where(
                        CalendarEventBinding.event_binding_id == inner.event_binding_id,
                        CalendarEventBinding.owner_principal_id == owner.principal_id,
                        CalendarEventBinding.owner_session_id == owner.session_id,
                    )
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            try:
                current_consent_calendar_id = _consented_calendar_id(current_consent)
                current_binding_calendar_id = decrypt(str(current_binding.calendar_id_private or "")) if current_binding is not None else None
            except Exception as exc:
                raise CalendarIntegrationError(
                    "calendar_event_revision_stale",
                    "The Calendar event changed during preparation publication",
                    status_code=409,
                    recovery_action="refresh_event",
                ) from exc
            if (
                current_binding is None
                or current_binding.state != "selected"
                or current_binding.connection_id != current_connection.connection_id
                or int(current_binding.connection_revision) != expected_connection_revision
                or current_binding.consent_id != current_consent.consent_id
                or int(current_binding.consent_revision) != expected_consent_revision
                or int(current_binding.revision) != expected_event_binding_revision
                or current_binding.event_revision != inner.event_revision
                or current_binding.calendar_list_revision != inner.calendar_list_revision
                or current_binding_calendar_id != current_consent_calendar_id
            ):
                raise CalendarIntegrationError(
                    "calendar_event_revision_stale",
                    "The Calendar event changed during preparation publication",
                    status_code=409,
                    recovery_action="refresh_event",
                )

        try:
            mutation = await repository.create_task(
                db,
                owner,
                task_request,
                origin_session_id=operator.session_id,
                publication_authority_check=assert_publication_authority,
            )
        except CalendarIntegrationError as exc:
            await _tombstone_input_artifact(
                owner,
                metadata.artifact_id,
                db=db,
                error_code="calendar_prep_artifact_cleanup_required",
                message="The failed Calendar preparation input requires cleanup reconciliation",
                recovery_action="reconcile_prep_artifact",
            )
            raise _safe_error(exc) from exc
        except CalendarControlError as exc:
            await _tombstone_input_artifact(
                owner,
                metadata.artifact_id,
                db=db,
                error_code="calendar_prep_artifact_cleanup_required",
                message="The failed Calendar preparation input requires cleanup reconciliation",
                recovery_action="reconcile_prep_artifact",
            )
            raise _control_http_error(exc) from exc
        except HTTPException:
            await _tombstone_input_artifact(
                owner,
                metadata.artifact_id,
                db=db,
                error_code="calendar_prep_artifact_cleanup_required",
                message="The failed Calendar preparation input requires cleanup reconciliation",
                recovery_action="reconcile_prep_artifact",
            )
            raise
        except Exception:
            await _tombstone_input_artifact(
                owner,
                metadata.artifact_id,
                db=db,
                error_code="calendar_prep_artifact_cleanup_required",
                message="The failed Calendar preparation input requires cleanup reconciliation",
                recovery_action="reconcile_prep_artifact",
            )
            raise
        from src.api.work_board import _input_artifact_payload, _safe_task_payload
        response_payload = {"input_artifact": _input_artifact_payload(metadata), "task": await _safe_task_payload(mutation.task, db=db), "idempotent_replay": mutation.idempotent_replay}
        return JSONResponse(content=response_payload, status_code=200 if mutation.idempotent_replay else 201)


@router.post("/calendar/schedules")
async def create_schedule(request: Request):
    operator = _operator(request)
    body = await _json_body(request, ScheduleCreate)
    owner = _owner(operator)
    try:
        cadence = normalize_cadence(body.cadence)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "calendar_schedule_cadence_invalid", "message": "Calendar schedule cadence is invalid", "recovery_action": "correct_cadence"}) from exc
    expires_at = _aware(body.expires_at)
    now = _now()
    # The scheduler input artifact is the executable authority and expires
    # after 24 hours.  Reject a longer requested schedule before reserving
    # that artifact; accepting a seven-day row here would advertise work that
    # cannot be executed after the artifact has expired.
    if expires_at <= now or expires_at > now + timedelta(hours=24):
        raise HTTPException(status_code=422, detail={"code": "calendar_schedule_expiry_invalid", "message": "Calendar schedule expiry is outside the bounded window", "recovery_action": "choose_bounded_expiry"})
    async with get_session() as db:
        try:
            consent, connection = await _active_consent(db, owner, body.consent_id)
        except CalendarIntegrationError as exc:
            raise _safe_error(exc) from exc
        if consent.goal_id != body.goal_id or consent.goal_revision != body.goal_revision or _consented_calendar_id(consent) != body.calendar_id:
            raise HTTPException(status_code=409, detail={"code": "calendar_schedule_binding_stale", "message": "Calendar schedule binding is stale", "recovery_action": "refresh_event"})
        if not consent.allow_remote_model:
            raise HTTPException(status_code=403, detail={"code": "calendar_remote_model_consent_required", "message": "Remote preparation requires explicit consent", "recovery_action": "create_new_consent"})
        consent_expires_at = _aware(consent.expires_at)
        if expires_at > consent_expires_at:
            # Request timestamps are independently serialized by the client;
            # tolerate only the small clock/serialization skew between the
            # consent response and the immediately-following schedule create,
            # then clamp the stored expiry to the finite consent boundary.
            if expires_at - consent_expires_at <= timedelta(seconds=2):
                expires_at = consent_expires_at
            else:
                raise HTTPException(status_code=422, detail={"code": "calendar_schedule_expiry_invalid", "message": "Calendar schedule cannot outlive consent", "recovery_action": "choose_bounded_expiry"})
        await repository._validate_goal(db, owner, goal_id=body.goal_id, goal_revision=body.goal_revision)
        try:
            await _assert_live_operator_session(db, owner)
        except CalendarControlError as exc:
            raise _control_http_error(exc) from exc
        existing = (await db.execute(select(GovernedScheduleBinding).where(GovernedScheduleBinding.owner_principal_id == owner.principal_id, GovernedScheduleBinding.owner_session_id == owner.session_id, GovernedScheduleBinding.schedule_idempotency_key == body.idempotency_key))).scalar_one_or_none()
        if existing is not None:
            request_digest = "sha256:" + digest({"consent_id": body.consent_id, "goal_id": body.goal_id, "goal_revision": body.goal_revision, "calendar_id": body.calendar_id, "cadence": cadence, "expires_at": expires_at.isoformat(), "idempotency_key": body.idempotency_key})
            if existing.schedule_request_digest != request_digest:
                raise HTTPException(status_code=409, detail={"code": "calendar_schedule_idempotency_conflict", "message": "The schedule key is bound to another request", "recovery_action": "use_new_idempotency_key"})
            return {"binding": serialize_binding(existing)}
        metadata = await create_observation_input_artifact(db, owner, consent=consent, connection_id=connection.connection_id, idempotency_key=f"schedule:{body.idempotency_key}")
        try:
            await _begin_serialized(db)
            await _assert_live_operator_session(db, owner)
            await repository._validate_goal(db, owner, goal_id=body.goal_id, goal_revision=body.goal_revision)
            current_consent, current_connection = await _active_consent(db, owner, body.consent_id)
            if current_consent.revision != consent.revision or current_connection.revision != connection.revision:
                raise HTTPException(status_code=409, detail={"code": "calendar_revision_stale", "message": "Calendar consent or connection revision is stale", "recovery_action": "refresh_event"})
        except CalendarIntegrationError as exc:
            await _tombstone_schedule_artifact(owner, metadata.artifact_id, db=db)
            raise _safe_error(exc) from exc
        except CalendarControlError as exc:
            await _tombstone_schedule_artifact(owner, metadata.artifact_id, db=db)
            raise _control_http_error(exc) from exc
        except Exception:
            await _tombstone_schedule_artifact(owner, metadata.artifact_id, db=db)
            raise
        request_digest = "sha256:" + digest({"consent_id": body.consent_id, "goal_id": body.goal_id, "goal_revision": body.goal_revision, "calendar_id": body.calendar_id, "cadence": cadence, "expires_at": expires_at.isoformat(), "idempotency_key": body.idempotency_key})
        action_digest = "sha256:" + digest({"action_type": "calendar.observe_due_events.v1", "consent_id": consent.consent_id, "consent_revision": consent.revision, "consent_digest": consent.consent_digest, "input_digest": metadata.typed_input_digest, "cadence": cadence})
        try:
            binding = await create_binding(
                db,
                owner,
                {
                    "action_type": "calendar.observe_due_events.v1",
                    "capability_id": "calendar.observe_due_events.v1",
                    "cadence": cadence,
                    "expires_at": expires_at,
                    "idempotency_key": body.idempotency_key,
                    "goal_id": body.goal_id,
                    "goal_revision": body.goal_revision,
                    "consent_id": consent.consent_id,
                    "consent_revision": consent.revision,
                    "consent_digest": consent.consent_digest,
                    "input_artifact_id": metadata.artifact_id,
                    "input_digest": metadata.typed_input_digest,
                    "action_digest": action_digest,
                    "schedule_request_digest": request_digest,
                },
            )
        except RuntimeError as exc:
            await _tombstone_schedule_artifact(owner, metadata.artifact_id, db=db)
            code = str(exc)
            mapped = {
                "governed_schedule_capacity_exceeded": (429, "calendar_schedule_capacity_exceeded", "pause_or_revoke_existing_schedule"),
                "governed_schedule_idempotency_conflict": (409, "calendar_schedule_idempotency_conflict", "use_new_idempotency_key"),
            }.get(code, (409, "calendar_schedule_reconciliation_required", "reconcile_schedule_control"))
            raise HTTPException(status_code=mapped[0], detail={"code": mapped[1], "message": "The governed Calendar schedule requires reconciliation", "recovery_action": mapped[2]}) from exc
        except (LookupError, ValueError) as exc:
            await _tombstone_schedule_artifact(owner, metadata.artifact_id, db=db)
            raise HTTPException(status_code=422, detail={"code": "calendar_schedule_invalid", "message": "The governed Calendar schedule is invalid", "recovery_action": "correct_schedule"}) from exc
        except Exception:
            await _tombstone_schedule_artifact(owner, metadata.artifact_id, db=db)
            raise
        return JSONResponse(content={"binding": serialize_binding(binding)}, status_code=201)


@router.get("/governed-schedules")
async def list_schedules(request: Request):
    operator = _operator(request)
    owner = _owner(operator)
    async with get_session() as db:
        try:
            await _assert_live_operator_session(db, owner)
        except CalendarControlError as exc:
            raise _control_http_error(exc) from exc
        rows = (await db.execute(select(GovernedScheduleBinding).where(GovernedScheduleBinding.owner_principal_id == owner.principal_id, GovernedScheduleBinding.owner_session_id == owner.session_id).order_by(GovernedScheduleBinding.created_at.desc()).limit(100))).scalars().all()
        result = []
        for row in rows:
            occurrence = (await db.execute(select(GovernedScheduleOccurrence).where(GovernedScheduleOccurrence.binding_id == row.binding_id).order_by(GovernedScheduleOccurrence.updated_at.desc()).limit(1))).scalar_one_or_none()
            result.append(serialize_binding(row, occurrence))
        return {"bindings": result}


@router.patch("/governed-schedules/{binding_id}")
async def patch_schedule(request: Request, binding_id: str):
    operator = _operator(request)
    body = await _json_body(request, SchedulePatch)
    owner = _owner(operator)
    async with get_session() as db:
        try:
            await _assert_live_operator_session(db, owner)
            row = await apply_control(
                db,
                owner,
                binding_id,
                body.action,
                body.expected_binding_revision,
                body.idempotency_key,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail={"code": "calendar_schedule_not_found", "message": "The governed schedule is unavailable", "recovery_action": None}) from exc
        except CalendarControlError as exc:
            raise _control_http_error(exc) from exc
        except RuntimeError as exc:
            code = str(exc)
            mapped = {
                "governed_schedule_idempotency_conflict": (409, "calendar_schedule_idempotency_conflict", "use_new_idempotency_key"),
                "governed_schedule_revision_stale": (409, "calendar_schedule_revision_stale", "reload_schedule"),
                "governed_schedule_not_active": (409, "calendar_schedule_not_active", "create_new_schedule"),
            }.get(code, (409, "calendar_schedule_reconciliation_required", "reconcile_schedule_control"))
            raise HTTPException(status_code=mapped[0], detail={"code": mapped[1], "message": "The governed Calendar schedule requires reconciliation", "recovery_action": mapped[2]}) from exc
        # ``apply_control`` returns the immutable durable receipt projection;
        # reserializing it as an ORM row would discard A→B→A replay lineage.
        return {"binding": row}


@router.post("/governed-schedules/{binding_id}/revoke")
async def revoke_schedule(request: Request, binding_id: str):
    operator = _operator(request)
    body = await _json_body(request, ScheduleRevoke)
    owner = _owner(operator)
    async with get_session() as db:
        try:
            await _assert_live_operator_session(db, owner)
            row = await apply_control(
                db,
                owner,
                binding_id,
                "revoke",
                body.expected_binding_revision,
                body.idempotency_key,
                body.reason,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail={"code": "calendar_schedule_not_found", "message": "The governed schedule is unavailable", "recovery_action": None}) from exc
        except CalendarControlError as exc:
            raise _control_http_error(exc) from exc
        except RuntimeError as exc:
            code = str(exc)
            mapped = {
                "governed_schedule_idempotency_conflict": (409, "calendar_schedule_idempotency_conflict", "use_new_idempotency_key"),
                "governed_schedule_revision_stale": (409, "calendar_schedule_revision_stale", "reload_schedule"),
                "governed_schedule_not_active": (409, "calendar_schedule_not_active", "create_new_schedule"),
            }.get(code, (409, "calendar_schedule_reconciliation_required", "reconcile_schedule_control"))
            raise HTTPException(status_code=mapped[0], detail={"code": mapped[1], "message": "The governed Calendar schedule requires reconciliation", "recovery_action": mapped[2]}) from exc
        return {"binding": row}


__all__ = ["router"]
