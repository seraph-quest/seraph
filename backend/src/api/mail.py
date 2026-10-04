"""Authenticated, owner-bound Gmail source routes for M7.

This router intentionally exposes a narrow local-read surface.  Gmail
connection metadata, label inventory, source consent, and message bindings are
all scoped to the middleware-authenticated operator and exact session.  No
caller supplied owner, provider URL, Gmail user, or arbitrary query becomes
authority.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select, text, update

from config.settings import settings

from src.auth.service import AuthenticatedOperator
from src.db.engine import get_session
from src.db.models import (
    Goal,
    GoogleServiceConnection,
    MailLabelBinding,
    MailMessageBinding,
    MailReadConsent,
    MailWatchState,
    OperatorSession,
    GovernedScheduleBinding,
    GovernedScheduleOccurrence,
    WorkBoardAttempt,
    WorkBoardInputArtifact,
    WorkBoardTask,
    WorkBoardStatus,
    WorkflowRunState,
)
from src.integrations.gmail_read import (
    GMAIL_READONLY_SCOPE,
    GMAIL_SERVICE,
    GmailLabel,
    GmailMessageBody,
    GmailMessageMetadata,
    GmailReadError,
    GoogleGmailReadonlyAdapter,
    digest,
    message_key,
    thread_key,
)
from src.integrations.gmail_controls import (
    MailSourceRequest,
    _artifact_path,
    _delete_verified_artifact,
    assert_mail_source_lease,
    run_mail_source_control,
)
from src.scheduler.governed_schedules import _begin_serialized
from src.scheduler.governed_schedules import (
    create_binding,
    create_mail_watch_input_artifact,
    serialize_binding,
)
from src.goals.repository import deserialize_admission_budget
from src.vault import decrypt, encrypt, vault_repository
from src.work_board.contracts import WorkBoardOwner
from src.work_board.contracts import WorkBoardInputArtifactCreate, WorkBoardTaskCreate
from src.work_board.input_artifacts import (
    _decode_and_validate_payload,
    _metadata_digest,
    _payload_path,
    _safe_file_bytes,
    prepare_input_artifact,
    revoke_input_artifact,
)
from src.work_board.repository import BoardError, WorkBoardRepository


router = APIRouter()
repository = WorkBoardRepository()
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SAFE_REQUEST = re.compile(r"^[A-Za-z0-9_.:@+,-]{1,256}$")
_MAIL_CONNECTION_CREATE_LOCK = asyncio.Lock()
_MAIL_CONSENT_CREATE_LOCK = asyncio.Lock()
_MAX_SETUP_BYTES = 16 * 1024
_DEFAULT_BODY_FIELDS = ("subject", "plainbody", "replyintent")
_ALLOWED_BODY_FIELDS = frozenset(_DEFAULT_BODY_FIELDS)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class ConnectionCreate(_Strict):
    schema_version: Literal[1] = 1
    service: Literal["gmail_readonly"] = GMAIL_SERVICE
    label: str = Field(min_length=1, max_length=200)
    client_id: str = Field(min_length=1, max_length=4096)
    client_secret: str | None = Field(default=None, max_length=4096)
    refresh_token: str = Field(min_length=1, max_length=8192)
    declared_scopes: list[str] = Field(min_length=1, max_length=1)
    idempotency_key: str = Field(min_length=1, max_length=256)

    @field_validator("declared_scopes")
    @classmethod
    def validate_declared_scopes(cls, values: list[str]) -> list[str]:
        if values != [GMAIL_READONLY_SCOPE]:
            raise ValueError("declared_scopes must contain exactly gmail.readonly")
        return values


class ConnectionControl(_Strict):
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)


class ConnectionVerify(_Strict):
    expected_revision: int = Field(ge=1)
    request_uuid: str = Field(min_length=1, max_length=256)


class ReplyConnectionCreate(_Strict):
    service: Literal["gmail_reply_read", "gmail_reply_send"]
    label: str = Field(min_length=1, max_length=200)
    client_id: str = Field(min_length=1, max_length=4096)
    client_secret: str | None = Field(default=None, max_length=4096)
    refresh_token: str = Field(min_length=1, max_length=8192)
    declared_scopes: list[str] = Field(min_length=3, max_length=3)
    acknowledge_separate_identity_profile: Literal[True]
    idempotency_key: str = Field(min_length=1, max_length=256)


class ReplyPairVerify(_Strict):
    read_connection_id: str = Field(min_length=1, max_length=256)
    expected_read_revision: int = Field(ge=1)
    send_connection_id: str = Field(min_length=1, max_length=256)
    expected_send_revision: int = Field(ge=1)
    goal_id: str = Field(min_length=1, max_length=256)
    goal_revision: int = Field(ge=1)
    acknowledge_identity_read: Literal[True]
    request_uuid: str = Field(min_length=1, max_length=256)
    priority: int = Field(default=60, ge=0, le=100)


class ReplySendPreview(_Strict):
    task_id: str = Field(min_length=1, max_length=256)
    expected_message_revision: str = Field(min_length=1, max_length=128)
    read_connection_id: str = Field(min_length=1, max_length=256)
    expected_read_revision: int = Field(ge=1)
    send_connection_id: str = Field(min_length=1, max_length=256)
    expected_send_revision: int = Field(ge=1)
    acknowledge_identity_source_read: Literal[True]
    acknowledge_exact_reply_send: Literal[True]
    request_uuid: str = Field(min_length=1, max_length=256)
    priority: int = Field(default=60, ge=0, le=100)


class ReplyDecision(_Strict):
    decision: Literal["approved", "denied"]
    expected_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReplyCancel(_Strict):
    expected_revision: int = Field(ge=1)
    request_uuid: str = Field(min_length=1, max_length=256)


class ReplyObservation(_Strict):
    expected_original_revision: int = Field(ge=1)
    read_connection_id: str = Field(min_length=1, max_length=256)
    expected_read_revision: int = Field(ge=1)
    goal_id: str = Field(min_length=1, max_length=256)
    goal_revision: int = Field(ge=1)
    acknowledge_readonly_recovery: Literal[True]
    request_uuid: str = Field(min_length=1, max_length=256)
    priority: int = Field(default=60, ge=0, le=100)


class LabelsRefresh(_Strict):
    connection_id: str = Field(min_length=1, max_length=256)
    expected_connection_revision: int = Field(ge=1)
    acknowledge_account_label_read: Literal[True]
    request_uuid: str = Field(min_length=1, max_length=256)


class ConsentCreate(_Strict):
    schema_version: Literal[1] = 1
    connection_id: str = Field(min_length=1, max_length=256)
    expected_connection_revision: int = Field(ge=1)
    goal_id: str = Field(min_length=1, max_length=256)
    expected_goal_revision: int = Field(ge=1)
    label_ids: list[str] = Field(min_length=1, max_length=3)
    expires_at: datetime
    max_messages: int = Field(default=10, ge=1, le=10)
    allowed_body_fields: list[str] = Field(default_factory=lambda: list(_DEFAULT_BODY_FIELDS), max_length=3)
    # Source access is a deliberate operator acknowledgement.  Omitting the
    # field must fail validation instead of silently granting a read scope.
    acknowledge_source_read: Literal[True]
    idempotency_key: str = Field(min_length=1, max_length=256)

    @field_validator("expires_at", mode="before")
    @classmethod
    def require_aware_expiry(cls, value: datetime | str) -> datetime:
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("expires_at must be an ISO datetime") from exc
        if not isinstance(value, datetime):
            raise ValueError("expires_at must be a datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must include an explicit timezone")
        return value

    @field_validator("label_ids")
    @classmethod
    def validate_label_ids(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(not _SAFE_REQUEST.fullmatch(value) for value in values):
            raise ValueError("label_ids must be bounded opaque identifiers")
        return values

    @field_validator("allowed_body_fields")
    @classmethod
    def validate_body_fields(cls, values: list[str]) -> list[str]:
        normalized = list(values)
        if len(set(normalized)) != len(normalized) or not normalized or any(value not in _ALLOWED_BODY_FIELDS for value in normalized):
            raise ValueError("allowed_body_fields contains an unsupported field")
        return normalized


class ModelConsent(_Strict):
    expected_revision: int = Field(ge=1)
    acknowledged_payload_fields: list[str] = Field(min_length=1, max_length=3)
    allow: bool

    @field_validator("acknowledged_payload_fields")
    @classmethod
    def validate_payload_fields(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(value not in _ALLOWED_BODY_FIELDS for value in values):
            raise ValueError("model consent must acknowledge a unique allowed payload-field subset")
        return values


class ConsentControl(_Strict):
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    reason: str = Field(default="operator_revoked", min_length=1, max_length=300)


class MessageScan(_Strict):
    connection_id: str = Field(min_length=1, max_length=256)
    expected_connection_revision: int = Field(ge=1)
    mail_consent_id: str = Field(min_length=1, max_length=256)
    expected_source_consent_revision: int = Field(ge=1)
    label_ids: list[str] = Field(min_length=1, max_length=3)
    received_after: datetime
    max_messages: int = Field(ge=1, le=10)
    request_uuid: str = Field(min_length=1, max_length=256)

    @field_validator("received_after", mode="before")
    @classmethod
    def require_aware_received_after(cls, value: datetime | str) -> datetime:
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("received_after must be an ISO datetime") from exc
        if not isinstance(value, datetime):
            raise ValueError("received_after must be a datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("received_after must include an explicit timezone")
        return value

    @field_validator("label_ids")
    @classmethod
    def validate_label_ids(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(not _SAFE_REQUEST.fullmatch(value) for value in values):
            raise ValueError("label_ids must be bounded opaque identifiers")
        return values


class MessageRead(_Strict):
    connection_id: str = Field(min_length=1, max_length=256)
    expected_connection_revision: int = Field(ge=1)
    mail_consent_id: str = Field(min_length=1, max_length=256)
    expected_source_consent_revision: int = Field(ge=1)
    message_binding_id: str = Field(min_length=1, max_length=256)
    expected_message_revision: str = Field(min_length=8, max_length=128)
    acknowledge_selected_body_read: Literal[True]
    request_uuid: str = Field(min_length=1, max_length=256)


class ReplyTaskCreate(_Strict):
    """Strict operator intent for a private, local Mail reply draft."""

    schema_version: Literal[1] = 1
    connection_id: str = Field(min_length=1, max_length=256)
    expected_connection_revision: int = Field(ge=1)
    message_binding_id: str = Field(min_length=1, max_length=256)
    expected_message_revision: str = Field(min_length=8, max_length=128)
    mail_consent_id: str = Field(min_length=1, max_length=256)
    expected_source_consent_revision: int = Field(ge=1)
    expected_model_consent_revision: int = Field(ge=1)
    goal_id: str = Field(min_length=1, max_length=256)
    expected_goal_revision: int = Field(ge=1)
    reply_intent: str = Field(min_length=1, max_length=2000)
    style: Literal["brief", "formal"]
    idempotency_key: str = Field(min_length=1, max_length=256)

    @field_validator("connection_id", "message_binding_id", "mail_consent_id", "goal_id", "idempotency_key")
    @classmethod
    def validate_opaque_request_ids(cls, value: str) -> str:
        if not _SAFE_REQUEST.fullmatch(value):
            raise ValueError("identifier is not a bounded opaque value")
        return value

    @field_validator("expected_message_revision")
    @classmethod
    def validate_revision_digest(cls, value: str) -> str:
        if not re.fullmatch(r"(?:sha256:)?[0-9a-fA-F]{8,128}", value):
            raise ValueError("message revision is not a bounded digest")
        return value


class MailWatchCadence(_Strict):
    """The canonical public cadence object shared by governed schedules.

    Mail metadata watches intentionally expose only the two supported periodic
    forms.  The explicit null daily fields keep the wire shape identical to
    the scheduler's canonical cadence DTO and make future daily support an
    additive contract rather than a scalar/string compatibility alias.
    """

    kind: Literal["hourly", "6h"]
    timezone: str = Field(min_length=1, max_length=64)
    daily_hour: Literal[None]
    daily_minute: Literal[None]

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("cadence timezone is not supported") from exc
        return value


class MailWatchCreate(_Strict):
    """Finite metadata-only Gmail watch admission."""

    schema_version: Literal[1] = 1
    connection_id: str = Field(min_length=1, max_length=256)
    expected_connection_revision: int = Field(ge=1)
    mail_consent_id: str = Field(min_length=1, max_length=256)
    expected_source_consent_revision: int = Field(ge=1)
    goal_id: str = Field(min_length=1, max_length=256)
    expected_goal_revision: int = Field(ge=1)
    label_ids: list[str] = Field(min_length=1, max_length=3)
    cadence: MailWatchCadence
    expires_at: datetime
    max_messages: int = Field(default=10, ge=1, le=10)
    idempotency_key: str = Field(min_length=1, max_length=256)

    @field_validator(
        "connection_id",
        "mail_consent_id",
        "goal_id",
        "idempotency_key",
    )
    @classmethod
    def validate_opaque_ids(cls, value: str) -> str:
        if not _SAFE_REQUEST.fullmatch(value):
            raise ValueError("identifier is not a bounded opaque value")
        return value

    @field_validator("label_ids")
    @classmethod
    def validate_label_ids(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(not _SAFE_REQUEST.fullmatch(value) for value in values):
            raise ValueError("label_ids must be bounded opaque identifiers")
        return values

    @field_validator("expires_at", mode="before")
    @classmethod
    def require_aware_expiry(cls, value: datetime | str) -> datetime:
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("expires_at must be an ISO datetime") from exc
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must include an explicit timezone")
        return value


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _owner(operator: AuthenticatedOperator) -> WorkBoardOwner:
    return WorkBoardOwner(
        principal_id=operator.principal.principal_id,
        session_id=operator.session_id,
    )


def _operator(request: Request) -> AuthenticatedOperator:
    operator = getattr(request.state, "operator", None)
    principal = getattr(operator, "principal", None)
    principal_id = str(getattr(principal, "principal_id", "") or "").strip()
    session_id = str(getattr(operator, "session_id", "") or "").strip()
    if (
        not isinstance(operator, AuthenticatedOperator)
        or not principal_id
        or not session_id
        or not bool(getattr(principal, "authenticated", False))
        or bool(getattr(principal, "revoked", False))
        or str(getattr(principal, "session_id", "") or "") != session_id
    ):
        raise HTTPException(status_code=401, detail={"code": "authentication_required", "recovery_action": "login"})
    return operator


async def _json_body(request: Request, model: type[_Strict]) -> _Strict:
    chunks: list[bytes] = []
    size = 0
    try:
        async for chunk in request.stream():
            if not isinstance(chunk, bytes):
                raise ValueError("request body is not bytes")
            size += len(chunk)
            if size > _MAX_SETUP_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail={"code": "mail_request_too_large", "message": "The Mail request is too large", "recovery_action": "reduce_request"},
                )
            chunks.append(chunk)
        raw = json.loads(b"".join(chunks))
        return model.model_validate(raw)
    except HTTPException:
        raise
    except (ValueError, ValidationError, TypeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "mail_request_invalid", "message": "The Mail request is invalid", "recovery_action": "correct_request"},
        ) from exc


def _error(exc: Exception, *, default_code: str = "mail_internal_error") -> HTTPException:
    code = str(getattr(exc, "code", default_code))[:128]
    message = str(exc)[:500] if isinstance(exc, (GmailReadError, BoardError)) else "Mail operation failed"
    status = int(getattr(exc, "status_code", 500) or 500)
    recovery = getattr(exc, "recovery_action", None)
    return HTTPException(status_code=status, detail={"code": code, "message": message, "recovery_action": recovery})


def _credential_value_allowed(value: str) -> bool:
    normalized = value.strip()
    return bool(normalized) and not _CONTROL.search(normalized) and not normalized.casefold().startswith(("http://", "https://", "file://", "/", "\\", "~/"))


def _connection_digest(body: ConnectionCreate) -> str:
    return "sha256:" + digest(
        {
            "schema_version": body.schema_version,
            "service": body.service,
            "label": body.label,
            "client_id": body.client_id,
            "client_secret": body.client_secret,
            "refresh_token": body.refresh_token,
            "declared_scopes": body.declared_scopes,
            "idempotency_key": body.idempotency_key,
        }
    )


def _credential_fingerprint(body: ConnectionCreate) -> str:
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


def _metadata(connection: GoogleServiceConnection) -> dict[str, Any]:
    return {
        "connection_id": connection.connection_id,
        "service": GMAIL_SERVICE,
        "label": connection.label,
        "revision": connection.revision,
        "state": connection.state,
        "scope_status": connection.scope_status or "scope_unverified",
        "declared_scopes": [GMAIL_READONLY_SCOPE],
        "provider_scopes_verified": bool(connection.provider_scopes_json not in ("", "[]", None)),
        "verified_setup_job_id": connection.verified_setup_job_id,
    }


def _connection_recovery_payload(
    connection: GoogleServiceConnection | None,
    *,
    idempotency_key: str,
) -> dict[str, Any]:
    """Return a redacted, owner-bound setup recovery projection.

    The setup key is the only caller supplied lookup value.  Credential
    material, the vault key, and the credential fingerprint remain private;
    the existing metadata projection is the sole connection object exposed.
    """

    base = {
        "idempotency_scope": "mail-connection-setup",
        "idempotency_key": idempotency_key,
        "connection_id": connection.connection_id if connection is not None else None,
        "request_digest": connection.setup_request_digest if connection is not None else None,
        "connection": _metadata(connection) if connection is not None else None,
        "memory_status": "no_learning",
    }
    if connection is None:
        return {
            "status": "not_found",
            **base,
            "recovery_action": "retry_same_key",
        }

    state = str(connection.state or "")
    if state == "active":
        status = "replayed"
        recovery_action = None
    elif state == "preparing":
        status = "pending"
        recovery_action = "reconcile_existing_setup"
    elif state in {"blocked", "blocked_cleanup"}:
        status = "blocked"
        recovery_action = "reconcile_existing_setup"
    elif state == "revoked":
        status = "blocked"
        recovery_action = "use_new_idempotency_key"
    else:
        # Unknown persisted state is never treated as active or replayable.
        status = "unknown"
        recovery_action = "reconcile_existing_setup"
    return {
        "status": status,
        **base,
        "recovery_action": recovery_action,
    }


def _json_list(value: str | None) -> list[dict[str, Any]]:
    try:
        loaded = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return [item for item in loaded if isinstance(item, dict)] if isinstance(loaded, list) else []


async def _cleanup_connection_mail_artifacts(owner: WorkBoardOwner, connection_id: str) -> bool:
    """Redact/delete only deterministic, receipt-backed Mail artifacts.

    The database snapshot is collected before filesystem work.  A terminal
    job with an exact artifact receipt may be removed after hash verification;
    an active or unknown job, an absent receipt, or any filesystem ambiguity
    remains explicitly pending so recovery never destroys uncertain evidence.
    """
    snapshots: list[tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]] = []
    async with get_session() as db:
        await _assert_live_session(db, owner)
        rows = (
            await db.execute(
                select(WorkflowRunState).where(
                    WorkflowRunState.owner_principal_id == owner.principal_id,
                    WorkflowRunState.operator_session_id == owner.session_id,
                    WorkflowRunState.job_kind.like("mail_%"),
                )
            )
        ).scalars().all()
        for run in rows:
            try:
                inputs = json.loads(run.arguments_json or "{}")
            except (TypeError, ValueError):
                inputs = {}
            if not isinstance(inputs, dict) or inputs.get("connection_id") != connection_id:
                continue
            snapshots.append(
                (
                    str(run.run_identity),
                    str(run.status),
                    _json_list(run.artifact_receipts_json),
                    _json_list(run.effect_receipts_json),
                )
            )

    terminal = {"succeeded", "failed", "cancelled", "degraded"}
    pending = False
    redactions: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    for job_id, status, artifacts, effects in snapshots:
        if status not in terminal:
            pending = True
            continue
        next_receipts: list[dict[str, Any]] = []
        found_private_receipt = False
        for receipt in artifacts:
            path = receipt.get("file_path")
            if receipt.get("artifact_type") != "mail_source_result" or path != _artifact_path(job_id):
                next_receipts.append(receipt)
                continue
            found_private_receipt = True
            sha = receipt.get("content_sha256")
            state = _delete_verified_artifact(job_id, sha if isinstance(sha, str) else None)
            if state not in {"deleted", "absent"}:
                pending = True
                next_receipts.append(receipt)
                continue
            next_receipts.append(
                {
                    "artifact_id": receipt.get("artifact_id"),
                    "artifact_type": "mail_source_result",
                    "state": "redacted",
                    "exists": False,
                    "redacted_at": _now().isoformat(),
                    "owner_principal_id": owner.principal_id,
                    "run_id": job_id,
                }
            )
        if not found_private_receipt:
            # An effect may prove that a file was published before the receipt
            # write failed.  Keep that evidence pending rather than guessing
            # that the deterministic file is safe to erase.
            if any(
                isinstance(effect.get("details"), dict)
                and effect.get("details", {}).get("artifact_path") == _artifact_path(job_id)
                for effect in effects
            ):
                pending = True
        redactions[job_id] = (artifacts, next_receipts)

    if redactions:
        async with get_session() as db:
            await _begin_serialized(db)
            await _assert_live_session(db, owner)
            for job_id, (expected_receipts, receipts) in redactions.items():
                row = (
                    await db.execute(
                        select(WorkflowRunState)
                        .where(
                            WorkflowRunState.run_identity == job_id,
                            WorkflowRunState.owner_principal_id == owner.principal_id,
                            WorkflowRunState.operator_session_id == owner.session_id,
                        )
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if row is None:
                    pending = True
                    continue
                # Do not overwrite a newer artifact receipt appended by a
                # concurrent recovery worker.  A changed JSON projection is a
                # reconciliation condition, not permission to erase it.
                current = _json_list(row.artifact_receipts_json)
                if current != expected_receipts:
                    pending = True
                    continue
                row.artifact_receipts_json = json.dumps(receipts[-100:], separators=(",", ":"))
                row.updated_at = _now()
            await db.flush()
    return not pending


def _label_metadata(row: MailLabelBinding) -> dict[str, Any]:
    return {
        "label_id": row.label_id,
        "name": row.label_name,
        "type": row.label_type,
        "connection_id": row.connection_id,
        "connection_revision": row.connection_revision,
        "revision": row.revision,
        "state": row.state,
    }


def _consent_metadata(row: MailReadConsent) -> dict[str, Any]:
    try:
        labels = json.loads(row.label_ids_json or "[]")
    except (TypeError, ValueError):
        labels = []
    return {
        "consent_id": row.consent_id,
        "connection_id": row.connection_id,
        "connection_revision": row.connection_revision,
        "goal_id": row.goal_id,
        "goal_revision": row.goal_revision,
        "label_ids": labels if isinstance(labels, list) else [],
        "window_days": row.window_days,
        "max_messages": row.max_messages,
        "source_read_allowed": bool(row.source_read_allowed),
        "source_revision": row.source_revision,
        "model_egress_allowed": bool(row.model_egress_allowed),
        "model_revision": row.model_revision,
        "allowed_body_fields": json.loads(row.allowed_body_fields_json or "[]"),
        "expires_at": _aware(row.expires_at).isoformat(),
        "state": row.state,
        "revision": row.revision,
    }


def _model_consent_digest(row: MailReadConsent, *, allow: bool, fields: list[str]) -> str:
    return "sha256:" + digest(
        {
            "consent_id": row.consent_id,
            "owner_principal_id": row.owner_principal_id,
            "owner_session_id": row.owner_session_id,
            "goal_id": row.goal_id,
            "goal_revision": row.goal_revision,
            "connection_id": row.connection_id,
            "connection_revision": row.connection_revision,
            "label_ids": sorted(_consent_label_ids(row)),
            "expires_at": _aware(row.expires_at).isoformat(),
            "source_revision": row.source_revision,
            "source_digest": row.source_digest,
            "model_revision": row.model_revision,
            "allow": bool(allow),
            "acknowledged_payload_fields": list(fields),
        }
    )


def _revoke_request_digest(
    *,
    kind: str,
    resource_id: str,
    expected_revision: int,
    reason: str,
    idempotency_key: str,
) -> str:
    """Canonical input digest for replayable destructive mutations."""
    return "sha256:" + digest(
        {
            "schema_version": 1,
            "operation": kind,
            "resource_id": resource_id,
            "expected_revision": expected_revision,
            "reason": reason,
            "idempotency_key": idempotency_key,
        }
    )


async def _assert_live_session(db: Any, owner: WorkBoardOwner) -> None:
    if owner.principal_id == "operator:test-bypass":
        if settings.deployment_environment == "test" and settings.operator_auth_allow_unauthenticated_tests:
            return
        raise GmailReadError("session_unavailable", "The operator session is unavailable", status_code=401, recovery_action="login")
    session = (
        await db.execute(
            select(OperatorSession)
            .where(OperatorSession.id == owner.session_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if session is None or session.principal_id != owner.principal_id:
        raise GmailReadError("session_unavailable", "The operator session is unavailable", status_code=401, recovery_action="login")
    if (
        session.is_bearer_tombstone is not False
        or session.replaced_by_id is not None
        or session.revoked_at is not None
        or _aware(session.idle_expires_at) <= _now()
        or _aware(session.absolute_expires_at) <= _now()
    ):
        raise GmailReadError("session_unavailable", "The operator session is unavailable", status_code=401, recovery_action="login")


async def _connection_for(db: Any, owner: WorkBoardOwner, connection_id: str) -> GoogleServiceConnection:
    row = (
        await db.execute(
            select(GoogleServiceConnection)
            .where(
                GoogleServiceConnection.connection_id == connection_id,
                GoogleServiceConnection.owner_principal_id == owner.principal_id,
                GoogleServiceConnection.owner_session_id == owner.session_id,
                GoogleServiceConnection.service == GMAIL_SERVICE,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        raise GmailReadError("mail_connection_not_found", "The Gmail connection is unavailable", status_code=404)
    return row


async def _consent_for(db: Any, owner: WorkBoardOwner, consent_id: str) -> MailReadConsent:
    row = await _consent_row_for(db, owner, consent_id)
    if row.state != "active" or not row.source_read_allowed or _aware(row.expires_at) <= _now():
        raise GmailReadError("mail_consent_unavailable", "The Gmail read consent is no longer active", status_code=409, recovery_action="create_new_consent")
    return row


async def _consent_row_for(db: Any, owner: WorkBoardOwner, consent_id: str) -> MailReadConsent:
    """Load an owner-bound consent, including a revoked row for replay."""
    row = (
        await db.execute(
            select(MailReadConsent)
            .where(
                MailReadConsent.consent_id == consent_id,
                MailReadConsent.owner_principal_id == owner.principal_id,
                MailReadConsent.owner_session_id == owner.session_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        raise GmailReadError("mail_consent_not_found", "The Gmail read consent is unavailable", status_code=404)
    return row


def _consent_label_ids(consent: MailReadConsent) -> list[str]:
    try:
        values = json.loads(consent.label_ids_json or "[]")
    except (TypeError, ValueError):
        return []
    return [value for value in values if isinstance(value, str)] if isinstance(values, list) else []


def _source_label_scope_digest(
    connection: GoogleServiceConnection,
    consent: MailReadConsent,
    label_ids: list[str] | None = None,
) -> str:
    """Bind a message identity to the exact source-consent label scope."""
    selected = sorted(label_ids if label_ids is not None else _consent_label_ids(consent))
    return "sha256:" + digest(
        {
            "namespace": "seraph.gmail.source-scope.v1",
            "connection_id": connection.connection_id,
            "connection_revision": connection.revision,
            "consent_id": consent.consent_id,
            "source_revision": consent.source_revision,
            "label_ids": list(selected),
        }
    )


async def _validate_source_scope(
    db: Any,
    owner: WorkBoardOwner,
    *,
    connection: GoogleServiceConnection,
    consent: MailReadConsent,
    expected_source_revision: int,
    label_ids: list[str] | None = None,
    max_messages: int | None = None,
    received_after: datetime | None = None,
) -> None:
    """Re-read every canonical source fence immediately before a read.

    The source and model permissions deliberately have independent revisions.
    A model-consent update must therefore not invalidate a metadata/body read,
    while a source-consent update must block it before provider contact.
    """
    await _assert_live_session(db, owner)
    current = await _connection_for(db, owner, connection.connection_id)
    if current.state != "active" or current.revision != connection.revision:
        raise GmailReadError("mail_connection_revision_stale", "The Gmail connection changed", status_code=409, recovery_action="reload_connection")
    if (
        consent.connection_id != current.connection_id
        or consent.connection_revision != current.revision
        or consent.source_revision != expected_source_revision
        or consent.state != "active"
        or not consent.source_read_allowed
        or _aware(consent.expires_at) <= _now()
    ):
        raise GmailReadError("mail_consent_revision_stale", "The Gmail read consent changed", status_code=409, recovery_action="reload_consent")
    if label_ids is not None and sorted(label_ids) != sorted(_consent_label_ids(consent)):
        raise GmailReadError("mail_consent_scope_mismatch", "The selected labels do not match the reviewed Mail scope", status_code=409, recovery_action="reload_consent")
    if max_messages is not None and max_messages > consent.max_messages:
        raise GmailReadError("mail_consent_scope_mismatch", "The requested Mail limit exceeds the reviewed scope", status_code=409, recovery_action="reload_consent")
    if received_after is not None:
        lower_bound = _now() - timedelta(days=consent.window_days)
        if received_after < lower_bound or received_after > _now():
            raise GmailReadError("mail_window_invalid", "The Mail scan window exceeds the reviewed scope", status_code=422, recovery_action="choose_bounded_window")
    if consent.goal_id:
        await repository._validate_goal(db, owner, goal_id=consent.goal_id, goal_revision=consent.goal_revision)


async def _authority_check(
    owner: WorkBoardOwner,
    *,
    connection_id: str,
    connection_revision: int,
    consent_id: str | None = None,
    consent_source_revision: int | None = None,
    label_ids: list[str] | None = None,
    max_messages: int | None = None,
    received_after: datetime | None = None,
) -> None:
    async with get_session() as db:
        await _assert_live_session(db, owner)
        connection = await _connection_for(db, owner, connection_id)
        if connection.state != "active" or connection.revision != connection_revision:
            raise GmailReadError("mail_connection_revision_stale", "The Gmail connection changed", status_code=409, recovery_action="reload_connection")
        if consent_id is not None:
            consent = await _consent_for(db, owner, consent_id)
            await _validate_source_scope(
                db,
                owner,
                connection=connection,
                consent=consent,
                expected_source_revision=int(consent_source_revision or 0),
                label_ids=label_ids,
                max_messages=max_messages,
                received_after=received_after,
            )
        else:
            consent = None


async def _assert_durable_lease_in_transaction(db: Any, lease: Any | None) -> None:
    """Fence a source write against cancellation while its DB transaction runs."""
    if lease is None:
        return
    run = (
        await db.execute(
            select(WorkflowRunState)
            .where(WorkflowRunState.run_identity == lease.job_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    now = _now()
    if (
        run is None
        or run.status != "running"
        or run.lease_owner != lease.owner
        or int(run.fencing_token or 0) != int(lease.fencing_token)
        or run.lease_expires_at is None
        or _aware(run.lease_expires_at) <= now
        or run.deadline_at is None
        or _aware(run.deadline_at) <= now
    ):
        raise GmailReadError(
            "mail_read_reconciliation_required",
            "Mail read lease authority changed",
            status_code=409,
            recovery_action="reconcile_existing_read",
        )


async def _persist_observed_provider_scope(
    owner: WorkBoardOwner,
    *,
    connection_id: str,
    expected_revision: int,
    observed_scopes: tuple[str, ...] | None,
) -> GoogleServiceConnection:
    """Persist exact token scope evidence under a connection revision CAS."""
    async with get_session() as db:
        await _begin_serialized(db)
        await _assert_live_session(db, owner)
        current = await _connection_for(db, owner, connection_id)
        if current.state != "active" or current.revision != expected_revision:
            raise GmailReadError(
                "mail_connection_revision_stale",
                "The Gmail connection changed while recording scope evidence",
                status_code=409,
                recovery_action="reload_connection",
            )
        if observed_scopes is not None:
            if tuple(observed_scopes) != (GMAIL_READONLY_SCOPE,):
                raise GmailReadError(
                    "mail_scope_unverified",
                    "Gmail authorization scope evidence is invalid",
                    status_code=502,
                    recovery_action="recreate_connection",
                )
            current.provider_scopes_json = json.dumps([GMAIL_READONLY_SCOPE], separators=(",", ":"))
            current.scope_status = "scope_verified"
            current.revision += 1
            current.updated_at = _now()
            await db.flush()
        else:
            # An OAuth response without a scope is an explicit absence of
            # evidence.  Clear any previous positive projection under the
            # same connection revision CAS so a later verify cannot retain
            # stale authorization scope.
            changed = (
                current.provider_scopes_json not in (None, "", "[]")
                or current.scope_status != "scope_unverified"
            )
            current.provider_scopes_json = "[]"
            current.scope_status = "scope_unverified"
            if changed:
                current.revision += 1
                current.updated_at = _now()
                await db.flush()
        return current


def _validate_expiry(expires_at: datetime) -> datetime:
    value = _aware(expires_at)
    now = _now()
    if value <= now or value > now + timedelta(days=7):
        raise HTTPException(status_code=422, detail={"code": "mail_consent_expiry_invalid", "message": "Mail consent expiry is outside the bounded window", "recovery_action": "choose_bounded_expiry"})
    return value


async def _store_labels(owner: WorkBoardOwner, connection: GoogleServiceConnection, labels: tuple[GmailLabel, ...], *, lease: Any | None = None) -> list[dict[str, Any]]:
    if lease is not None:
        await assert_mail_source_lease(lease)
    async with get_session() as db:
        await _begin_serialized(db)
        await _assert_durable_lease_in_transaction(db, lease)
        await _assert_live_session(db, owner)
        current = await _connection_for(db, owner, connection.connection_id)
        if current.revision != connection.revision or current.state != "active":
            raise GmailReadError("mail_connection_revision_stale", "The Gmail connection changed", status_code=409, recovery_action="reload_connection")
        existing = (
            await db.execute(
                select(MailLabelBinding).where(
                    MailLabelBinding.owner_principal_id == owner.principal_id,
                    MailLabelBinding.owner_session_id == owner.session_id,
                    MailLabelBinding.connection_id == connection.connection_id,
                )
            )
        ).scalars().all()
        by_digest = {row.provider_label_digest: row for row in existing}
        observed: set[str] = set()
        for label in labels:
            provider_digest = "sha256:" + digest({"namespace": "seraph.gmail.label.v1", "provider_label_id": label.provider_id})
            observed.add(provider_digest)
            row = by_digest.get(provider_digest)
            if row is None:
                row = MailLabelBinding(
                    owner_principal_id=owner.principal_id,
                    owner_session_id=owner.session_id,
                    connection_id=connection.connection_id,
                    connection_revision=connection.revision,
                    provider_label_id_ciphertext=encrypt(label.provider_id),
                    provider_label_digest=provider_digest,
                    label_name=label.name,
                    label_type=label.label_type,
                    state="active",
                )
                db.add(row)
            else:
                row.provider_label_id_ciphertext = encrypt(label.provider_id)
                row.connection_revision = connection.revision
                row.label_name = label.name
                row.label_type = label.label_type
                row.state = "active"
                row.revision += 1
                row.updated_at = _now()
        for row in existing:
            if row.provider_label_digest not in observed and row.state != "revoked":
                row.state = "revoked"
                row.revision += 1
                row.updated_at = _now()
        await db.flush()
        current_rows = (
            await db.execute(
                select(MailLabelBinding).where(
                    MailLabelBinding.owner_principal_id == owner.principal_id,
                    MailLabelBinding.owner_session_id == owner.session_id,
                    MailLabelBinding.connection_id == connection.connection_id,
                    MailLabelBinding.state == "active",
                ).order_by(MailLabelBinding.label_name, MailLabelBinding.label_id)
            )
        ).scalars().all()
        return [_label_metadata(row) for row in current_rows]


async def _selected_provider_labels(db: Any, owner: WorkBoardOwner, connection: GoogleServiceConnection, label_ids: list[str]) -> list[str]:
    rows = (
        await db.execute(
            select(MailLabelBinding).where(
                MailLabelBinding.label_id.in_(label_ids),
                MailLabelBinding.owner_principal_id == owner.principal_id,
                MailLabelBinding.owner_session_id == owner.session_id,
                MailLabelBinding.connection_id == connection.connection_id,
                MailLabelBinding.connection_revision == connection.revision,
                MailLabelBinding.state == "active",
            )
        )
    ).scalars().all()
    if len(rows) != len(set(label_ids)):
        raise GmailReadError("mail_labels_refresh_required", "Refresh the Gmail labels before scanning", status_code=409, recovery_action="refresh_labels")
    try:
        return [decrypt(row.provider_label_id_ciphertext) for row in rows]
    except Exception as exc:
        raise GmailReadError("mail_credential_unavailable", "The Gmail label inventory is unavailable", status_code=409, recovery_action="refresh_labels") from exc


async def _upsert_message_binding(
    owner: WorkBoardOwner,
    connection: GoogleServiceConnection,
    metadata: GmailMessageMetadata,
    *,
    lease: Any | None = None,
    consent_id: str | None = None,
    expected_source_revision: int | None = None,
    label_ids: list[str] | None = None,
    max_messages: int | None = None,
    received_after: datetime | None = None,
) -> MailMessageBinding:
    if lease is not None:
        await assert_mail_source_lease(lease)
    async with get_session() as db:
        await _begin_serialized(db)
        await _assert_durable_lease_in_transaction(db, lease)
        await _assert_live_session(db, owner)
        current = await _connection_for(db, owner, connection.connection_id)
        if current.revision != connection.revision or current.state != "active":
            raise GmailReadError("mail_connection_revision_stale", "The Gmail connection changed", status_code=409, recovery_action="reload_connection")
        if consent_id is not None:
            consent = await _consent_for(db, owner, consent_id)
            await _validate_source_scope(
                db,
                owner,
                connection=current,
                consent=consent,
                expected_source_revision=int(expected_source_revision or 0),
                label_ids=label_ids,
                max_messages=max_messages,
                received_after=received_after,
            )
        key = message_key(owner.principal_id, connection.connection_id, metadata.provider_message_id)
        row = (
            await db.execute(
                select(MailMessageBinding).where(
                    MailMessageBinding.owner_principal_id == owner.principal_id,
                    MailMessageBinding.owner_session_id == owner.session_id,
                    MailMessageBinding.connection_id == connection.connection_id,
                    MailMessageBinding.message_key == key,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = MailMessageBinding(
                owner_principal_id=owner.principal_id,
                owner_session_id=owner.session_id,
                connection_id=connection.connection_id,
                connection_revision=connection.revision,
                source_consent_id=consent.consent_id if consent is not None else None,
                source_consent_revision=consent.source_revision if consent is not None else None,
                source_label_scope_digest=(
                    _source_label_scope_digest(current, consent, label_ids)
                    if consent is not None
                    else None
                ),
                provider_message_id_ciphertext=encrypt(metadata.provider_message_id),
                provider_thread_id_ciphertext=encrypt(metadata.provider_thread_id),
                message_key=key,
                thread_key=thread_key(owner.principal_id, connection.connection_id, metadata.provider_thread_id),
                message_revision=metadata.message_revision,
                received_at=metadata.received_at,
                status="present",
            )
            db.add(row)
        else:
            row.connection_revision = connection.revision
            row.source_consent_id = consent.consent_id if consent is not None else row.source_consent_id
            row.source_consent_revision = consent.source_revision if consent is not None else row.source_consent_revision
            if consent is not None:
                row.source_label_scope_digest = _source_label_scope_digest(current, consent, label_ids)
            row.provider_thread_id_ciphertext = encrypt(metadata.provider_thread_id)
            row.message_revision = metadata.message_revision
            row.received_at = metadata.received_at
            row.fetched_at = _now()
            row.status = "present"
            row.revision += 1
            row.updated_at = _now()
        await db.flush()
        return row


async def _binding_for(db: Any, owner: WorkBoardOwner, binding_id: str, connection_id: str) -> MailMessageBinding:
    row = (
        await db.execute(
            select(MailMessageBinding).where(
                MailMessageBinding.message_binding_id == binding_id,
                MailMessageBinding.owner_principal_id == owner.principal_id,
                MailMessageBinding.owner_session_id == owner.session_id,
                MailMessageBinding.connection_id == connection_id,
            ).execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None or row.status != "present":
        raise GmailReadError("mail_message_not_found", "The selected Mail message is unavailable", status_code=404)
    return row


@router.get("/capabilities/mail/connections")
async def list_connections(request: Request) -> dict[str, Any]:
    operator = _operator(request)
    owner = _owner(operator)
    async with get_session() as db:
        await _assert_live_session(db, owner)
        rows = (
            await db.execute(
                select(GoogleServiceConnection)
                .where(
                    GoogleServiceConnection.owner_principal_id == owner.principal_id,
                    GoogleServiceConnection.owner_session_id == owner.session_id,
                    GoogleServiceConnection.service == GMAIL_SERVICE,
                )
                .order_by(GoogleServiceConnection.created_at.desc())
            )
        ).scalars().all()
        return {"connections": [_metadata(row) for row in rows]}


@router.get("/capabilities/mail/labels")
async def list_labels(request: Request, connection_id: str) -> dict[str, Any]:
    """Return the cached label inventory without contacting Gmail."""
    operator = _operator(request)
    owner = _owner(operator)
    if not _SAFE_REQUEST.fullmatch(connection_id):
        raise HTTPException(status_code=422, detail={"code": "mail_request_invalid", "message": "The Mail request is invalid", "recovery_action": "correct_request"})
    try:
        async with get_session() as db:
            await _assert_live_session(db, owner)
            connection = await _connection_for(db, owner, connection_id)
            rows = (
                await db.execute(
                    select(MailLabelBinding)
                    .where(
                        MailLabelBinding.owner_principal_id == owner.principal_id,
                        MailLabelBinding.owner_session_id == owner.session_id,
                        MailLabelBinding.connection_id == connection.connection_id,
                        MailLabelBinding.connection_revision == connection.revision,
                        MailLabelBinding.state == "active",
                    )
                    .order_by(MailLabelBinding.label_name, MailLabelBinding.label_id)
                )
            ).scalars().all()
            return {
                "connection_id": connection.connection_id,
                "connection_revision": connection.revision,
                "labels": [_label_metadata(row) for row in rows],
                "provider_contact": False,
            }
    except GmailReadError as exc:
        raise _error(exc) from exc


@router.get("/capabilities/mail/read-consents")
async def list_read_consents(request: Request, connection_id: str | None = None) -> dict[str, Any]:
    """Return owner/session-bound consent metadata from canonical storage."""
    operator = _operator(request)
    owner = _owner(operator)
    if connection_id is not None and not _SAFE_REQUEST.fullmatch(connection_id):
        raise HTTPException(status_code=422, detail={"code": "mail_request_invalid", "message": "The Mail request is invalid", "recovery_action": "correct_request"})
    async with get_session() as db:
        await _assert_live_session(db, owner)
        statement = select(MailReadConsent).where(
            MailReadConsent.owner_principal_id == owner.principal_id,
            MailReadConsent.owner_session_id == owner.session_id,
        )
        if connection_id is not None:
            statement = statement.where(MailReadConsent.connection_id == connection_id)
        rows = (await db.execute(statement.order_by(MailReadConsent.created_at.desc()))).scalars().all()
        return {"consents": [_consent_metadata(row) for row in rows], "provider_contact": False}


@router.post("/capabilities/mail/connections", response_model=None)
async def create_connection(request: Request) -> Any:
    async with _MAIL_CONNECTION_CREATE_LOCK:
        operator = _operator(request)
        body = await _json_body(request, ConnectionCreate)
        owner = _owner(operator)
        if any(not _credential_value_allowed(value) for value in (body.client_id, body.refresh_token, body.client_secret or "x")):
            raise HTTPException(status_code=422, detail={"code": "mail_request_invalid", "message": "The Gmail credential material is invalid", "recovery_action": "correct_request"})
        request_digest = _connection_digest(body)
        fingerprint = _credential_fingerprint(body)
        async with get_session() as db:
            await _begin_serialized(db)
            await _assert_live_session(db, owner)
            existing = (
                await db.execute(
                    select(GoogleServiceConnection).where(
                        GoogleServiceConnection.owner_principal_id == owner.principal_id,
                        GoogleServiceConnection.owner_session_id == owner.session_id,
                        GoogleServiceConnection.service == GMAIL_SERVICE,
                        GoogleServiceConnection.setup_idempotency_key == body.idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing.setup_request_digest != request_digest:
                    raise HTTPException(status_code=409, detail={"code": "mail_idempotency_conflict", "message": "The setup key is bound to another request", "recovery_action": "use_new_idempotency_key"})
                if existing.state == "active":
                    return {"connection": _metadata(existing)}
                if existing.state in {"blocked_cleanup", "blocked"}:
                    raise HTTPException(status_code=503, detail={"code": "mail_connection_reconciliation_required", "message": "The Gmail setup requires reconciliation", "recovery_action": "reconcile_existing_setup"})
                connection = existing
            else:
                connection = GoogleServiceConnection(
                    owner_principal_id=owner.principal_id,
                    owner_session_id=owner.session_id,
                    service=GMAIL_SERVICE,
                    label=body.label,
                    vault_secret_key=f"gmail:{owner.principal_id}:{secrets.token_urlsafe(18)}",
                    credential_fingerprint=fingerprint,
                    declared_scopes_json=json.dumps([GMAIL_READONLY_SCOPE], separators=(",", ":")),
                    provider_scopes_json="[]",
                    scope_status="scope_unverified",
                    setup_idempotency_key=body.idempotency_key,
                    setup_request_digest=request_digest,
                    state="preparing",
                    revision=1,
                )
                db.add(connection)
                await db.flush()
            connection_id = connection.connection_id
            vault_key = connection.vault_secret_key
        try:
            existing_secret = await vault_repository.get(vault_key)
            expected_secret = _vault_value(body)
            if existing_secret is None:
                await vault_repository.store(vault_key, expected_secret, description="Google Gmail read-only connection")
            elif existing_secret != expected_secret:
                raise GmailReadError("mail_idempotency_conflict", "The setup key is bound to another request", status_code=409, recovery_action="use_new_idempotency_key")
        except GmailReadError:
            raise
        except Exception as exc:
            async with get_session() as db:
                row = await db.get(GoogleServiceConnection, connection_id)
                if row is not None:
                    row.state = "blocked"
                    row.revision += 1
                    row.updated_at = _now()
            raise HTTPException(status_code=503, detail={"code": "mail_connection_cleanup_required", "message": "The Gmail credential could not be stored", "recovery_action": "reconcile_existing_setup"}) from exc
        async with get_session() as db:
            await _assert_live_session(db, owner)
            row = await _connection_for(db, owner, connection_id)
            if row.setup_request_digest != request_digest:
                raise HTTPException(status_code=409, detail={"code": "mail_idempotency_conflict", "message": "The setup key is bound to another request", "recovery_action": "use_new_idempotency_key"})
            if row.state == "preparing":
                row.state = "active"
                row.revision += 1
                row.updated_at = _now()
            await db.flush()
            return JSONResponse(content={"connection": _metadata(row)}, status_code=201)


@router.get("/capabilities/mail/connections/recovery/{idempotency_key}")
async def recover_connection_setup(request: Request, idempotency_key: str) -> dict[str, Any]:
    """Resolve a lost connection-setup response by its owner-bound key."""

    operator = _operator(request)
    owner = _owner(operator)
    if not _SAFE_REQUEST.fullmatch(idempotency_key):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "mail_request_invalid",
                "message": "The Mail request is invalid",
                "recovery_action": "correct_request",
            },
        )
    async with get_session() as db:
        await _assert_live_session(db, owner)
        connection = (
            await db.execute(
                select(GoogleServiceConnection)
                .where(
                    GoogleServiceConnection.owner_principal_id == owner.principal_id,
                    GoogleServiceConnection.owner_session_id == owner.session_id,
                    GoogleServiceConnection.service == GMAIL_SERVICE,
                    GoogleServiceConnection.setup_idempotency_key == idempotency_key,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        return _connection_recovery_payload(connection, idempotency_key=idempotency_key)


@router.post("/capabilities/mail/connections/{connection_id}/verify")
async def verify_connection(request: Request, connection_id: str) -> dict[str, Any]:
    """Perform one explicit fixed Gmail read to verify the imported setup."""
    operator = _operator(request)
    body = await _json_body(request, ConnectionVerify)
    owner = _owner(operator)
    if not _SAFE_REQUEST.fullmatch(connection_id):
        raise HTTPException(status_code=422, detail={"code": "mail_request_invalid", "message": "The Mail request is invalid", "recovery_action": "correct_request"})
    async with get_session() as db:
        await _assert_live_session(db, owner)
        connection = await _connection_for(db, owner, connection_id)
        if connection.state != "active" or connection.revision != body.expected_revision:
            raise HTTPException(status_code=409, detail={"code": "mail_connection_revision_stale", "message": "The Gmail connection changed", "recovery_action": "reload_connection"})
    request_digest = "sha256:" + digest(
        {
            "operation": "mail_connection_verify",
            "connection_id": connection.connection_id,
            "connection_revision": connection.revision,
            "request_uuid": body.request_uuid,
        }
    )
    control_request = MailSourceRequest(
        operation="mail_connection_verify",
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        connection_id=connection.connection_id,
        connection_revision=connection.revision,
        request_uuid=body.request_uuid,
        request_digest=request_digest,
    )

    async def execute(_lease: Any) -> dict[str, Any]:
        await assert_mail_source_lease(_lease)

        async def authority_check() -> None:
            await assert_mail_source_lease(_lease)
            await _authority_check(
                owner,
                connection_id=connection.connection_id,
                connection_revision=connection.revision,
            )

        adapter = GoogleGmailReadonlyAdapter(
            connection,
            owner_principal_id=owner.principal_id,
            authority_check=authority_check,
        )
        labels = await adapter.list_labels()
        return {
            "connection_id": connection.connection_id,
            "connection_revision": connection.revision,
            "verification": "provider_read_succeeded",
            "label_count": len(labels),
            "scope_status": connection.scope_status or "scope_unverified",
            # An OAuth token response without ``scope`` is a real, explicit
            # absence of evidence.  Keep that distinction from an observed
            # empty/broader scope so the connection remains visibly
            # unverified instead of being rejected after a successful read.
            "_observed_provider_scopes": (
                list(adapter.observed_provider_scopes)
                if adapter.observed_provider_scopes is not None
                else None
            ),
            "provider_contact": True,
        }

    try:
        payload = await run_mail_source_control(control_request, execute)
    except GmailReadError as exc:
        raise _error(exc) from exc
    observed_scopes = payload.pop("_observed_provider_scopes", None)
    persisted_connection = await _persist_observed_provider_scope(
        owner,
        connection_id=connection_id,
        expected_revision=body.expected_revision,
        observed_scopes=tuple(observed_scopes) if isinstance(observed_scopes, list) else None,
    )
    async with get_session() as db:
        await _begin_serialized(db)
        await _assert_live_session(db, owner)
        current = await _connection_for(db, owner, connection_id)
        if current.state != "active" or current.revision != persisted_connection.revision:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "mail_connection_revision_stale",
                    "message": "The Gmail connection changed after verification",
                    "recovery_action": "reload_connection",
                },
            )
        current.verified_setup_job_id = control_request.job_id
        current.updated_at = _now()
        await db.flush()
    return {
        **payload,
        "connection_revision": persisted_connection.revision,
        "scope_status": persisted_connection.scope_status or "scope_unverified",
        "provider_scopes_verified": bool(persisted_connection.provider_scopes_json not in ("", "[]", None)),
        "control_job_id": control_request.job_id,
    }


async def _reserve_connection_revoke(
    owner: WorkBoardOwner,
    connection_id: str,
    body: ConnectionControl,
    request_digest: str,
) -> tuple[str, str | None, dict[str, Any] | None, int | None]:
    """Reserve destructive cleanup before touching vault or private files.

    ``blocked_cleanup`` is the durable reservation state.  It carries the
    exact revoke key and digest, so a retry can resume the same cleanup while
    a different request cannot adopt or overwrite it.
    """
    async with get_session() as db:
        await _begin_serialized(db)
        await _assert_live_session(db, owner)
        connection = await _connection_for(db, owner, connection_id)
        if connection.revoke_idempotency_key is not None:
            if connection.revoke_idempotency_key != body.idempotency_key:
                if connection.state in {"revoked", "blocked_cleanup"}:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "mail_connection_revision_stale",
                            "message": "The Gmail connection changed",
                            "recovery_action": "reload_connection",
                        },
                    )
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "mail_revoke_idempotency_conflict",
                        "message": "The revoke key is bound to another request",
                        "recovery_action": "use_new_idempotency_key",
                    },
                )
            if connection.revoke_request_digest != request_digest:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "mail_revoke_idempotency_conflict",
                        "message": "The revoke key is bound to another request",
                        "recovery_action": "use_new_idempotency_key",
                    },
                )
            if connection.state == "revoked":
                return "complete", connection.vault_secret_key, _metadata(connection), None
            if connection.state != "blocked_cleanup":
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "mail_connection_reconciliation_required",
                        "message": "The Gmail revoke requires reconciliation",
                        "recovery_action": "retry_cleanup",
                    },
                )
            return "resume", connection.vault_secret_key, None, connection.revision
        if connection.state in {"revoked", "blocked_cleanup"}:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "mail_connection_revision_stale",
                    "message": "The Gmail connection changed",
                    "recovery_action": "reload_connection",
                },
            )
        if connection.revision != body.expected_revision:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "mail_connection_revision_stale",
                    "message": "The Gmail connection changed",
                    "recovery_action": "reload_connection",
                },
            )
        connection.state = "blocked_cleanup"
        connection.revision += 1
        connection.updated_at = _now()
        connection.revoke_idempotency_key = body.idempotency_key
        connection.revoke_request_digest = request_digest
        await db.execute(
            update(MailReadConsent)
            .where(
                MailReadConsent.connection_id == connection_id,
                MailReadConsent.owner_principal_id == owner.principal_id,
                MailReadConsent.owner_session_id == owner.session_id,
                MailReadConsent.state.in_(("active", "expired")),
            )
            .values(
                state="revoked",
                revision=MailReadConsent.revision + 1,
                updated_at=_now(),
                source_read_allowed=False,
                model_egress_allowed=False,
            )
        )
        await db.execute(
            update(MailLabelBinding)
            .where(
                MailLabelBinding.connection_id == connection_id,
                MailLabelBinding.owner_principal_id == owner.principal_id,
                MailLabelBinding.owner_session_id == owner.session_id,
                MailLabelBinding.state != "redacted",
            )
            .values(
                state="redacted",
                provider_label_id_ciphertext="",
                revision=MailLabelBinding.revision + 1,
                updated_at=_now(),
            )
        )
        await db.execute(
            update(MailMessageBinding)
            .where(
                MailMessageBinding.connection_id == connection_id,
                MailMessageBinding.owner_principal_id == owner.principal_id,
                MailMessageBinding.owner_session_id == owner.session_id,
                MailMessageBinding.status != "redacted",
            )
            .values(
                status="redacted",
                provider_message_id_ciphertext="",
                provider_thread_id_ciphertext="",
                revision=MailMessageBinding.revision + 1,
                updated_at=_now(),
            )
        )
        await db.flush()
        return "reserved", connection.vault_secret_key, None, connection.revision


async def _complete_connection_revoke(
    owner: WorkBoardOwner,
    connection_id: str,
    *,
    idempotency_key: str,
    request_digest: str,
    expected_revision: int,
) -> dict[str, Any]:
    """Commit the terminal revoke state only after all cleanup succeeds."""
    async with get_session() as db:
        await _begin_serialized(db)
        await _assert_live_session(db, owner)
        connection = await _connection_for(db, owner, connection_id)
        if (
            connection.state != "blocked_cleanup"
            or connection.revoke_idempotency_key != idempotency_key
            or connection.revoke_request_digest != request_digest
            or connection.revision != expected_revision
        ):
            raise GmailReadError(
                "mail_connection_reconciliation_required",
                "The Gmail revoke changed while cleanup was running",
                status_code=409,
                recovery_action="retry_cleanup",
            )
        connection.state = "revoked"
        connection.revision += 1
        connection.updated_at = _now()
        await db.flush()
        return {"connection": _metadata(connection)}


@router.delete("/capabilities/mail/connections/{connection_id}")
async def revoke_connection(request: Request, connection_id: str) -> dict[str, Any]:
    operator = _operator(request)
    body = await _json_body(request, ConnectionControl)
    owner = _owner(operator)
    request_digest = _revoke_request_digest(
        kind="mail_connection_revoke",
        resource_id=connection_id,
        expected_revision=body.expected_revision,
        reason="operator_revoked",
        idempotency_key=body.idempotency_key,
    )
    try:
        phase, vault_key, completed, reservation_revision = await _reserve_connection_revoke(owner, connection_id, body, request_digest)
    except (HTTPException,):
        raise
    except GmailReadError as exc:
        raise _error(exc) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "mail_connection_reconciliation_required",
                "message": "Gmail revoke reservation requires reconciliation",
                "recovery_action": "retry_cleanup",
            },
        ) from exc
    if phase == "complete":
        return {"connection": completed}
    try:
        if vault_key:
            await vault_repository.delete(vault_key)
        cleanup_complete = await _cleanup_connection_mail_artifacts(owner, connection_id)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "mail_connection_cleanup_required",
                "message": "Gmail credential or private artifact cleanup requires reconciliation",
                "recovery_action": "retry_cleanup",
            },
        ) from exc
    if not cleanup_complete:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "mail_connection_cleanup_required",
                "message": "Private Mail artifacts require exact owner/job/hash reconciliation",
                "recovery_action": "retry_cleanup",
            },
        )
    try:
        return await _complete_connection_revoke(
            owner,
            connection_id,
            idempotency_key=body.idempotency_key,
            request_digest=request_digest,
            expected_revision=int(reservation_revision or 0),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "mail_connection_cleanup_required",
                "message": "Gmail revoke completion requires reconciliation",
                "recovery_action": "retry_cleanup",
            },
        ) from exc


@router.post("/capabilities/mail/labels/refresh")
async def refresh_labels(request: Request) -> dict[str, Any]:
    operator = _operator(request)
    body = await _json_body(request, LabelsRefresh)
    owner = _owner(operator)
    async with get_session() as db:
        await _assert_live_session(db, owner)
        connection = await _connection_for(db, owner, body.connection_id)
        if connection.state != "active":
            raise HTTPException(status_code=409, detail={"code": "mail_connection_unavailable", "message": "The Gmail connection is not active", "recovery_action": "restore_prerequisite"})
        if connection.revision != body.expected_connection_revision:
            raise HTTPException(status_code=409, detail={"code": "mail_connection_revision_stale", "message": "The Gmail connection changed", "recovery_action": "reload_connection"})
    request_digest = "sha256:" + digest(
        {
            "operation": "mail_labels_refresh",
            "connection_id": connection.connection_id,
            "connection_revision": connection.revision,
            "acknowledge_account_label_read": True,
            "request_uuid": body.request_uuid,
        }
    )

    async def execute(_lease: Any) -> dict[str, Any]:
        await assert_mail_source_lease(_lease)

        async def authority_check() -> None:
            await assert_mail_source_lease(_lease)
            await _authority_check(
                owner,
                connection_id=connection.connection_id,
                connection_revision=connection.revision,
            )

        adapter = GoogleGmailReadonlyAdapter(
            connection,
            owner_principal_id=owner.principal_id,
            authority_check=authority_check,
        )
        labels = await adapter.list_labels()
        current_connection = await _persist_observed_provider_scope(
            owner,
            connection_id=connection.connection_id,
            expected_revision=connection.revision,
            observed_scopes=adapter.observed_provider_scopes,
        )
        stored = await _store_labels(owner, current_connection, labels, lease=_lease)
        return {
            "connection_id": current_connection.connection_id,
            "connection_revision": current_connection.revision,
            "labels": stored,
            "coverage": {"complete": True, "count": len(stored)},
            "scope_status": current_connection.scope_status or "scope_unverified",
            "provider_scopes_verified": bool(current_connection.provider_scopes_json not in ("", "[]", None)),
            "provider_contact": True,
        }

    try:
        payload = await run_mail_source_control(
            MailSourceRequest(
                operation="mail_labels_refresh",
                owner_principal_id=owner.principal_id,
                owner_session_id=owner.session_id,
                connection_id=connection.connection_id,
                connection_revision=connection.revision,
                request_uuid=body.request_uuid,
                request_digest=request_digest,
            ),
            execute,
        )
        return {**payload, "control_job_id": MailSourceRequest(
            operation="mail_labels_refresh",
            owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,
            connection_id=connection.connection_id,
            connection_revision=connection.revision,
            request_uuid=body.request_uuid,
            request_digest=request_digest,
        ).job_id}
    except GmailReadError as exc:
        raise _error(exc) from exc


@router.post("/capabilities/mail/read-consents", response_model=None)
async def create_consent(request: Request) -> Any:
    async with _MAIL_CONSENT_CREATE_LOCK:
        operator = _operator(request)
        body = await _json_body(request, ConsentCreate)
        owner = _owner(operator)
        expires_at = _validate_expiry(body.expires_at)
        async with get_session() as db:
            await _begin_serialized(db)
            await _assert_live_session(db, owner)
            connection = await _connection_for(db, owner, body.connection_id)
            if connection.state != "active" or connection.revision != body.expected_connection_revision:
                raise HTTPException(status_code=409, detail={"code": "mail_connection_revision_stale", "message": "The Gmail connection changed", "recovery_action": "reload_connection"})
            await repository._validate_goal(db, owner, goal_id=body.goal_id, goal_revision=body.expected_goal_revision)
            labels = (
                await db.execute(
                    select(MailLabelBinding).where(
                        MailLabelBinding.label_id.in_(body.label_ids),
                        MailLabelBinding.owner_principal_id == owner.principal_id,
                        MailLabelBinding.owner_session_id == owner.session_id,
                        MailLabelBinding.connection_id == connection.connection_id,
                        MailLabelBinding.connection_revision == connection.revision,
                        MailLabelBinding.state == "active",
                    )
                )
            ).scalars().all()
            if len(labels) != len(set(body.label_ids)):
                raise HTTPException(status_code=409, detail={"code": "mail_labels_refresh_required", "message": "Refresh Gmail labels before creating consent", "recovery_action": "refresh_labels"})
            allowed = list(dict.fromkeys(body.allowed_body_fields))
            request_digest = "sha256:" + digest({"connection_id": connection.connection_id, "connection_revision": connection.revision, "goal_id": body.goal_id, "goal_revision": body.expected_goal_revision, "label_ids": sorted(body.label_ids), "window_days": 7, "max_messages": body.max_messages, "allowed_body_fields": allowed, "expires_at": expires_at.isoformat(), "idempotency_key": body.idempotency_key})
            existing = (
                await db.execute(
                    select(MailReadConsent).where(
                        MailReadConsent.owner_principal_id == owner.principal_id,
                        MailReadConsent.owner_session_id == owner.session_id,
                        MailReadConsent.creation_idempotency_key == body.idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing.creation_request_digest != request_digest:
                    raise HTTPException(status_code=409, detail={"code": "mail_consent_idempotency_conflict", "message": "The consent key is bound to another request", "recovery_action": "use_new_idempotency_key"})
                return {"consent": _consent_metadata(existing)}
            source_digest = "sha256:" + digest({"owner": owner.principal_id, "session": owner.session_id, "connection_id": connection.connection_id, "connection_revision": connection.revision, "goal_id": body.goal_id, "goal_revision": body.expected_goal_revision, "label_ids": sorted(body.label_ids), "window_days": 7, "max_messages": body.max_messages, "allowed_body_fields": allowed, "expires_at": expires_at.isoformat()})
            row = MailReadConsent(
                owner_principal_id=owner.principal_id,
                owner_session_id=owner.session_id,
                connection_id=connection.connection_id,
                connection_revision=connection.revision,
                creation_idempotency_key=body.idempotency_key,
                creation_request_digest=request_digest,
                goal_id=body.goal_id,
                goal_revision=body.expected_goal_revision,
                label_ids_json=json.dumps(sorted(body.label_ids), separators=(",", ":")),
                window_days=7,
                max_messages=body.max_messages,
                source_read_allowed=True,
                source_revision=1,
                source_digest=source_digest,
                model_egress_allowed=False,
                model_revision=1,
                model_digest="",
                allowed_body_fields_json=json.dumps(allowed, separators=(",", ":")),
                expires_at=expires_at,
                state="active",
                revision=1,
            )
            db.add(row)
            await db.flush()
            return JSONResponse(content={"consent": _consent_metadata(row)}, status_code=201)


@router.post("/capabilities/mail/read-consents/{consent_id}/model-consent")
async def set_model_consent(request: Request, consent_id: str) -> dict[str, Any]:
    operator = _operator(request)
    body = await _json_body(request, ModelConsent)
    owner = _owner(operator)
    async with get_session() as db:
        await _begin_serialized(db)
        await _assert_live_session(db, owner)
        row = await _consent_for(db, owner, consent_id)
        if row.revision != body.expected_revision:
            raise HTTPException(status_code=409, detail={"code": "mail_consent_revision_stale", "message": "The Gmail consent changed", "recovery_action": "reload_consent"})
        try:
            configured_fields = json.loads(row.allowed_body_fields_json or "[]")
        except (TypeError, ValueError):
            configured_fields = None
        if not isinstance(configured_fields, list) or configured_fields != body.acknowledged_payload_fields:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "mail_model_consent_fields_mismatch",
                    "message": "Acknowledge exactly the payload fields configured for this consent",
                    "recovery_action": "reload_consent",
                },
            )
        row.model_egress_allowed = bool(body.allow)
        row.model_revision += 1
        row.model_reviewed_at = _now()
        row.model_digest = _model_consent_digest(
            row,
            allow=bool(body.allow),
            fields=body.acknowledged_payload_fields,
        )
        row.revision += 1
        row.updated_at = _now()
        await db.flush()
        return {"consent": _consent_metadata(row)}


@router.post("/capabilities/mail/read-consents/{consent_id}/revoke")
async def revoke_consent(request: Request, consent_id: str) -> dict[str, Any]:
    operator = _operator(request)
    body = await _json_body(request, ConsentControl)
    owner = _owner(operator)
    request_digest = _revoke_request_digest(
        kind="mail_consent_revoke",
        resource_id=consent_id,
        expected_revision=body.expected_revision,
        reason=body.reason,
        idempotency_key=body.idempotency_key,
    )
    async with get_session() as db:
        await _begin_serialized(db)
        await _assert_live_session(db, owner)
        row = await _consent_row_for(db, owner, consent_id)
        if row.revoke_idempotency_key == body.idempotency_key:
            if row.revoke_request_digest != request_digest:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "mail_revoke_idempotency_conflict",
                        "message": "The revoke key is bound to another request",
                        "recovery_action": "use_new_idempotency_key",
                    },
                )
            if row.state == "revoked":
                return {"consent": _consent_metadata(row)}
        elif row.state == "revoked":
            raise HTTPException(status_code=409, detail={"code": "mail_consent_revision_stale", "message": "The Gmail consent changed", "recovery_action": "reload_consent"})
        if row.revision != body.expected_revision:
            raise HTTPException(status_code=409, detail={"code": "mail_consent_revision_stale", "message": "The Gmail consent changed", "recovery_action": "reload_consent"})
        row.state = "revoked"
        row.source_read_allowed = False
        row.model_egress_allowed = False
        row.revoke_idempotency_key = body.idempotency_key
        row.revoke_request_digest = request_digest
        row.source_revision += 1
        row.model_revision += 1
        row.revision += 1
        row.updated_at = _now()
        await db.flush()
        return {"consent": _consent_metadata(row)}


@router.post("/capabilities/mail/messages/scan")
async def scan_messages(request: Request) -> dict[str, Any]:
    operator = _operator(request)
    body = await _json_body(request, MessageScan)
    owner = _owner(operator)
    received_after = _aware(body.received_after)
    now = _now()
    if received_after > now or received_after < now - timedelta(days=7):
        raise HTTPException(status_code=422, detail={"code": "mail_window_invalid", "message": "The Mail scan window must be within the last seven days", "recovery_action": "choose_bounded_window"})
    async with get_session() as db:
        await _assert_live_session(db, owner)
        connection = await _connection_for(db, owner, body.connection_id)
        if connection.state != "active" or connection.revision != body.expected_connection_revision:
            raise HTTPException(status_code=409, detail={"code": "mail_connection_revision_stale", "message": "The Gmail connection changed", "recovery_action": "reload_connection"})
        consent = await _consent_for(db, owner, body.mail_consent_id)
        await _validate_source_scope(
            db,
            owner,
            connection=connection,
            consent=consent,
            expected_source_revision=body.expected_source_consent_revision,
            label_ids=body.label_ids,
            max_messages=body.max_messages,
            received_after=received_after,
        )

    request_digest = "sha256:" + digest(
        {
            "operation": "mail_messages_scan",
            "connection_id": connection.connection_id,
            "connection_revision": connection.revision,
            "consent_id": consent.consent_id,
            "source_revision": consent.source_revision,
            "source_digest": consent.source_digest,
            "goal_id": consent.goal_id,
            "goal_revision": consent.goal_revision,
            "label_ids": sorted(body.label_ids),
            "received_after": received_after.isoformat(),
            "max_messages": body.max_messages,
            "request_uuid": body.request_uuid,
        }
    )
    control_request = MailSourceRequest(
        operation="mail_messages_scan",
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        connection_id=connection.connection_id,
        connection_revision=connection.revision,
        request_uuid=body.request_uuid,
        request_digest=request_digest,
        consent_id=consent.consent_id,
        consent_revision=consent.source_revision,
        goal_id=consent.goal_id,
        goal_revision=consent.goal_revision,
    )

    async def execute(_lease: Any) -> dict[str, Any]:
        await assert_mail_source_lease(_lease)
        async with get_session() as db:
            await _assert_live_session(db, owner)
            current_connection = await _connection_for(db, owner, body.connection_id)
            current_consent = await _consent_for(db, owner, body.mail_consent_id)
            await _validate_source_scope(
                db,
                owner,
                connection=current_connection,
                consent=current_consent,
                expected_source_revision=body.expected_source_consent_revision,
                label_ids=body.label_ids,
                max_messages=body.max_messages,
                received_after=received_after,
            )
            current_provider_labels = await _selected_provider_labels(db, owner, current_connection, body.label_ids)
        async def authority_check() -> None:
            await assert_mail_source_lease(_lease)
            await _authority_check(
                owner,
                connection_id=current_connection.connection_id,
                connection_revision=current_connection.revision,
                consent_id=current_consent.consent_id,
                consent_source_revision=current_consent.source_revision,
                label_ids=body.label_ids,
                max_messages=body.max_messages,
                received_after=received_after,
            )

        adapter = GoogleGmailReadonlyAdapter(
            current_connection,
            owner_principal_id=owner.principal_id,
            authority_check=authority_check,
        )
        page = await adapter.list_message_ids(current_provider_labels, received_after=received_after, max_messages=body.max_messages)
        semaphore = asyncio.Semaphore(2)

        async def read_metadata(provider_id: str) -> GmailMessageMetadata:
            async with semaphore:
                return await adapter.get_message_metadata(provider_id)

        metadata_items = await asyncio.gather(*(read_metadata(provider_id) for provider_id in page.provider_ids))
        bindings: list[dict[str, Any]] = []
        for metadata in metadata_items:
            if {"SPAM", "TRASH"}.intersection(metadata.label_ids):
                continue
            await assert_mail_source_lease(_lease)
            binding = await _upsert_message_binding(
                owner,
                current_connection,
                metadata,
                lease=_lease,
                consent_id=current_consent.consent_id,
                expected_source_revision=body.expected_source_consent_revision,
                label_ids=body.label_ids,
                max_messages=body.max_messages,
                received_after=received_after,
            )
            bindings.append(
                {
                    "source_binding_id": binding.message_binding_id,
                    "message_key": binding.message_key,
                    "thread_key": binding.thread_key,
                    "message_revision": binding.message_revision,
                    "received_at": metadata.received_at.isoformat() if metadata.received_at else None,
                    "subject": metadata.subject,
                    "preview": metadata.preview,
                    "read_status": metadata.read_status,
                    "fetched_at": _aware(binding.fetched_at).isoformat(),
                }
            )
        return {
            "connection_id": current_connection.connection_id,
            "connection_revision": current_connection.revision,
            "consent_id": current_consent.consent_id,
            "source_consent_revision": current_consent.source_revision,
            "messages": bindings,
            "coverage": {
                "list_page_complete": page.next_page_token is None,
                "more_available": page.next_page_token is not None,
                "returned": len(bindings),
                "max_messages": body.max_messages,
                "window_days": current_consent.window_days,
            },
            "provider_contact": True,
        }

    try:
        payload = await run_mail_source_control(control_request, execute)
        return {**payload, "control_job_id": control_request.job_id}
    except GmailReadError as exc:
        raise _error(exc) from exc


@router.post("/capabilities/mail/messages/{message_binding_id}/read")
async def read_message(request: Request, message_binding_id: str) -> dict[str, Any]:
    operator = _operator(request)
    body = await _json_body(request, MessageRead)
    owner = _owner(operator)
    async with get_session() as db:
        await _assert_live_session(db, owner)
        connection = await _connection_for(db, owner, body.connection_id)
        if connection.state != "active" or connection.revision != body.expected_connection_revision:
            raise HTTPException(status_code=409, detail={"code": "mail_connection_revision_stale", "message": "The Gmail connection changed", "recovery_action": "reload_connection"})
        try:
            consent = await _consent_for(db, owner, body.mail_consent_id)
            await _validate_source_scope(
                db,
                owner,
                connection=connection,
                consent=consent,
                expected_source_revision=body.expected_source_consent_revision,
                max_messages=1,
            )
        except GmailReadError as exc:
            raise _error(exc) from exc
        binding = await _binding_for(db, owner, message_binding_id, connection.connection_id)
        if binding.connection_revision != connection.revision or binding.message_revision != body.expected_message_revision:
            raise HTTPException(status_code=409, detail={"code": "mail_message_revision_stale", "message": "The Mail message changed", "recovery_action": "rescan_messages"})
        if (
            binding.source_consent_id != consent.consent_id
            or binding.source_consent_revision != body.expected_source_consent_revision
            or binding.source_label_scope_digest != _source_label_scope_digest(connection, consent)
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "mail_message_scope_stale",
                    "message": "The selected Mail message is bound to a different reviewed source scope",
                    "recovery_action": "rescan_messages",
                },
            )
    request_digest = "sha256:" + digest(
        {
            "operation": "mail_message_read",
            "connection_id": connection.connection_id,
            "connection_revision": connection.revision,
            "consent_id": consent.consent_id,
            "source_revision": consent.source_revision,
            "source_digest": consent.source_digest,
            "message_binding_id": binding.message_binding_id,
            "message_revision": binding.message_revision,
            "request_uuid": body.request_uuid,
        }
    )
    control_request = MailSourceRequest(
        operation="mail_message_read",
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        connection_id=connection.connection_id,
        connection_revision=connection.revision,
        request_uuid=body.request_uuid,
        request_digest=request_digest,
        consent_id=consent.consent_id,
        consent_revision=consent.source_revision,
        goal_id=consent.goal_id,
        goal_revision=consent.goal_revision,
    )

    async def execute(_lease: Any) -> dict[str, Any]:
        await assert_mail_source_lease(_lease)
        async with get_session() as db:
            await _assert_live_session(db, owner)
            current_connection = await _connection_for(db, owner, body.connection_id)
            current_consent = await _consent_for(db, owner, body.mail_consent_id)
            await _validate_source_scope(
                db,
                owner,
                connection=current_connection,
                consent=current_consent,
                expected_source_revision=body.expected_source_consent_revision,
                max_messages=1,
            )
            current_binding = await _binding_for(db, owner, message_binding_id, current_connection.connection_id)
            if current_binding.connection_revision != current_connection.revision or current_binding.message_revision != body.expected_message_revision:
                raise GmailReadError("mail_message_revision_stale", "The Mail message changed", status_code=409, recovery_action="rescan_messages")
            if (
                current_binding.source_consent_id != current_consent.consent_id
                or current_binding.source_consent_revision != body.expected_source_consent_revision
                or current_binding.source_label_scope_digest != _source_label_scope_digest(current_connection, current_consent)
            ):
                raise GmailReadError("mail_message_scope_stale", "The selected Mail message is bound to a different reviewed source scope", status_code=409, recovery_action="rescan_messages")
            try:
                provider_message_id = decrypt(current_binding.provider_message_id_ciphertext)
            except Exception as exc:
                raise GmailReadError("mail_credential_unavailable", "The selected Mail message is unavailable", status_code=409, recovery_action="restore_prerequisite") from exc
        async def authority_check() -> None:
            await assert_mail_source_lease(_lease)
            await _authority_check(
                owner,
                connection_id=current_connection.connection_id,
                connection_revision=current_connection.revision,
                consent_id=current_consent.consent_id,
                consent_source_revision=current_consent.source_revision,
            )

        adapter = GoogleGmailReadonlyAdapter(
            current_connection,
            owner_principal_id=owner.principal_id,
            authority_check=authority_check,
        )
        await assert_mail_source_lease(_lease)
        result: GmailMessageBody = await adapter.get_message_full(provider_message_id)
        if result.metadata.message_revision != body.expected_message_revision:
            raise GmailReadError("mail_message_revision_stale", "The Mail message changed", status_code=409, recovery_action="rescan_messages")
        return {
            "source_binding_id": current_binding.message_binding_id,
            "message_key": current_binding.message_key,
            "thread_key": current_binding.thread_key,
            "message_revision": result.metadata.message_revision,
            "subject": result.metadata.subject,
            "plain_text": result.body,
            "truncated": bool(result.truncated),
            "read_status": result.metadata.read_status,
            "received_at": result.metadata.received_at.isoformat() if result.metadata.received_at else None,
            "fetched_at": _now().isoformat(),
            "provenance": {
                "connection_id": current_connection.connection_id,
                "connection_revision": current_connection.revision,
                "consent_id": current_consent.consent_id,
                "source_consent_revision": current_consent.source_revision,
                "memory_status": "no_learning",
                "egress": "local_only",
            },
            "provider_contact": True,
        }

    try:
        payload = await run_mail_source_control(control_request, execute)
        return {**payload, "control_job_id": control_request.job_id}
    except GmailReadError as exc:
        raise _error(exc) from exc


def _reply_input(body: ReplyTaskCreate) -> dict[str, Any]:
    """Build the exact server-owned input; no provider identity/body crosses it."""

    return {
        "schema_version": 1,
        "connection_id": body.connection_id,
        "expected_connection_revision": body.expected_connection_revision,
        "message_binding_id": body.message_binding_id,
        "expected_message_revision": body.expected_message_revision,
        "mail_consent_id": body.mail_consent_id,
        "expected_source_consent_revision": body.expected_source_consent_revision,
        "expected_model_consent_revision": body.expected_model_consent_revision,
        "goal_id": body.goal_id,
        "expected_goal_revision": body.expected_goal_revision,
        "reply_intent": body.reply_intent,
        "style": body.style,
    }


def _reply_task_response(task: Any, metadata: Any, *, replay: bool) -> dict[str, Any]:
    return {
        "status": "replayed" if replay else "accepted",
        "task_id": task.task_id,
        "attempt_id": None,
        "job_id": None,
        "input_artifact_id": metadata.artifact_id,
        "input_digest": metadata.typed_input_digest,
        "message_key": None,
        "message_revision": None,
        "goal_id": task.goal_id,
        "goal_revision": int(task.goal_revision),
        "source_status": "ready",
        "effective_route": None,
        "recovery_action": None,
        "memory_status": "no_learning",
    }


def _mail_reply_readback_verified(run: WorkflowRunState | None) -> bool:
    """Return whether the durable Mail run contains its private readback pair."""

    if run is None:
        return False
    artifacts = _json_list(run.artifact_receipts_json)
    effects = _json_list(run.effect_receipts_json)
    for artifact in reversed(artifacts):
        if (
            artifact.get("artifact_type") != "mail_reply_draft"
            or artifact.get("exists") is not True
            or not isinstance(artifact.get("file_path"), str)
            or not artifact.get("content_sha256")
        ):
            continue
        for effect in reversed(effects):
            details = effect.get("details")
            if (
                effect.get("receipt_kind") == "readback"
                and effect.get("effect_type") == "mail_reply_draft"
                and effect.get("status") == "succeeded"
                and isinstance(details, dict)
                and details.get("verified") is True
                and effect.get("target_path") == artifact.get("file_path")
                and effect.get("target_digest") == artifact.get("content_sha256")
                and effect.get("content_sha256") == artifact.get("content_sha256")
            ):
                return True
    return False


def _mail_reply_recovery_payload(
    *,
    idempotency_key: str,
    task: WorkBoardTask | None,
    attempt: WorkBoardAttempt | None,
    run: WorkflowRunState | None,
) -> dict[str, Any]:
    """Build an owner-safe exact-key recovery projection without private bytes."""

    base: dict[str, Any] = {
        "idempotency_scope": "mail-reply-draft",
        "idempotency_key": idempotency_key,
        "task_id": task.task_id if task is not None else None,
        "attempt_id": attempt.attempt_id if attempt is not None else None,
        "job_id": attempt.workflow_run_id if attempt is not None else None,
        "input_artifact_id": task.input_artifact_id if task is not None else None,
        "input_digest": task.typed_input_digest if task is not None else None,
        "request_digest": task.idempotency_payload_digest if task is not None else None,
        "goal_id": task.goal_id if task is not None else None,
        "goal_revision": int(task.goal_revision) if task is not None else None,
        "memory_status": "no_learning",
    }
    if task is None:
        return {"status": "not_found", **base, "recovery_action": "retry_same_key"}

    task_status = str(getattr(task.status, "value", task.status) or "")
    run_status = str(getattr(run, "status", "") or "")
    if _mail_reply_readback_verified(run) and task_status in {"done", "review"}:
        status = "verified"
        recovery_action = "open_private_draft"
    elif run_status in {"unknown", "unknown_external_effect", "cost_liability"}:
        status = "unknown"
        recovery_action = "reconcile_existing_reply"
    elif task_status == "blocked" or run_status in {"failed", "cancelled"}:
        status = "blocked"
        recovery_action = "reconcile_existing_reply"
    elif task_status == "running" or run_status in {"running", "accepted", "queued"}:
        status = "running"
        recovery_action = "wait_for_completion"
    else:
        status = "pending"
        recovery_action = "wait_for_dispatch"
    return {"status": status, **base, "recovery_action": recovery_action}


def _watch_metadata(
    binding: GovernedScheduleBinding,
    state: MailWatchState | None,
    occurrence: GovernedScheduleOccurrence | None = None,
    *,
    label_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Return a redacted watch projection joined to its canonical binding."""

    return {
        "watch_id": binding.binding_id,
        "scheduled_job_id": binding.scheduled_job_id,
        "capability_id": binding.capability_id,
        "connection_id": state.connection_id if state is not None else None,
        "connection_revision": state.connection_revision if state is not None else None,
        "mail_consent_id": binding.read_consent_id,
        "source_consent_revision": state.source_consent_revision if state is not None else binding.consent_revision,
        "goal_id": binding.goal_id,
        "goal_revision": binding.goal_revision,
        "label_ids": list(label_ids or []),
        "cadence": {
            "kind": binding.cadence_kind,
            "timezone": binding.timezone,
            "daily_hour": binding.daily_hour,
            "daily_minute": binding.daily_minute,
        },
        "binding_revision": binding.binding_revision,
        "expires_at": _aware(binding.expires_at).isoformat(),
        "state": binding.state,
        "watch_state": state.state if state is not None else "unknown",
        "baseline_complete": bool(state.baseline_complete) if state is not None else False,
        "last_observed_at": _aware(state.last_observed_at).isoformat() if state is not None and state.last_observed_at else None,
        "last_completed_occurrence_id": state.last_completed_occurrence_id if state is not None else None,
        "skipped_coverage_reason": state.skipped_coverage_reason if state is not None else "watch_state_missing",
        "list_page_complete": bool(state.list_page_complete) if state is not None else False,
        "latest_occurrence": (
            {
                "occurrence_id": occurrence.occurrence_id,
                "state": occurrence.state,
                "slot_utc": _aware(occurrence.slot_utc).isoformat(),
                "failure_code": _watch_occurrence_metadata(occurrence).get("failure_code"),
                "recovery_action": _watch_occurrence_metadata(occurrence).get("recovery_action"),
            }
            if occurrence is not None
            else None
        ),
    }


def _watch_occurrence_metadata(occurrence: GovernedScheduleOccurrence) -> dict[str, Any]:
    try:
        value = json.loads(occurrence.metadata_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


async def _watch_label_ids(db: Any, binding: GovernedScheduleBinding) -> list[str]:
    artifact = await db.get(WorkBoardInputArtifact, binding.input_artifact_id, populate_existing=True)
    if artifact is None or not artifact.metadata_digest or _metadata_digest(artifact) != artifact.metadata_digest:
        return []
    try:
        payload = _safe_file_bytes(
            _payload_path(artifact),
            expected_digest=artifact.payload_sha256,
            expected_size=artifact.size_bytes,
        )
        typed = _decode_and_validate_payload(artifact, payload, allow_scheduler=True)
    except Exception:
        return []
    values = typed.get("label_ids") if isinstance(typed, dict) else None
    return [value for value in values if isinstance(value, str)] if isinstance(values, list) else []


def _validate_watch_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "mail_watch_timezone_invalid",
                "message": "The Mail watch timezone is invalid",
                "recovery_action": "choose_supported_timezone",
            },
        ) from exc
    return value


async def _validate_watch_goal(db: Any, owner: WorkBoardOwner, *, goal_id: str, goal_revision: int, expires_at: datetime) -> Goal:
    await repository._validate_goal(db, owner, goal_id=goal_id, goal_revision=goal_revision)
    goal = (
        await db.execute(
            select(Goal)
            .where(
                Goal.id == goal_id,
                Goal.owner_principal_id == owner.principal_id,
                Goal.owner_session_id == owner.session_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    status = getattr(getattr(goal, "status", None), "value", getattr(goal, "status", None))
    budget = deserialize_admission_budget(goal) if goal is not None else None
    now = _now()
    if goal is None or status != "active" or int(goal.revision or 0) != int(goal_revision):
        raise HTTPException(status_code=409, detail={"code": "goal_not_admitted", "message": "The Mail watch goal is not currently active", "recovery_action": "refresh_goal"})
    if not bool(goal.proactive_enabled) or budget is None or not bool(budget.reviewed_grant) or not budget.grant_id:
        raise HTTPException(status_code=409, detail={"code": "goal_budget_missing_reviewed_grant", "message": "The Mail watch requires a reviewed proactive goal budget", "recovery_action": "review_goal_budget"})
    if budget.period_started_at is not None and _aware(budget.period_started_at) > now:
        raise HTTPException(status_code=409, detail={"code": "goal_budget_period_not_started", "message": "The Mail watch goal budget has not started", "recovery_action": "refresh_goal"})
    if budget.period_expires_at is None or _aware(budget.period_expires_at) <= now or expires_at > _aware(budget.period_expires_at):
        raise HTTPException(status_code=422, detail={"code": "mail_watch_expiry_invalid", "message": "The Mail watch must remain inside the reviewed goal budget", "recovery_action": "choose_bounded_expiry"})
    return goal


@router.post("/capabilities/mail/reply-tasks", response_model=None)
async def create_reply_task(request: Request) -> Any:
    """Queue one owner-bound local reply draft under the WorkBoard lease."""

    operator = _operator(request)
    body = await _json_body(request, ReplyTaskCreate)
    owner = _owner(operator)
    inputs = _reply_input(body)
    async with get_session() as db:
        await _begin_serialized(db)
        await _assert_live_session(db, owner)
        connection = await _connection_for(db, owner, body.connection_id)
        if connection.state != "active" or int(connection.revision) != int(body.expected_connection_revision):
            raise HTTPException(
                status_code=409,
                detail={"code": "mail_connection_revision_stale", "message": "The Gmail connection changed", "recovery_action": "reload_connection"},
            )
        consent = await _consent_for(db, owner, body.mail_consent_id)
        if (
            consent.connection_id != connection.connection_id
            or int(consent.connection_revision) != int(connection.revision)
            or int(consent.source_revision) != int(body.expected_source_consent_revision)
            or int(consent.model_revision) != int(body.expected_model_consent_revision)
            or consent.goal_id != body.goal_id
            or int(consent.goal_revision) != int(body.expected_goal_revision)
            or not consent.model_egress_allowed
            or not consent.model_digest
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "mail_consent_revision_stale", "message": "The reviewed Mail consent changed", "recovery_action": "reload_consent"},
            )
        try:
            allowed_fields = json.loads(consent.allowed_body_fields_json or "[]")
        except (TypeError, ValueError):
            allowed_fields = []
        required_fields = {"subject", "plainbody", "replyintent"}
        if not isinstance(allowed_fields, list) or set(allowed_fields) != required_fields:
            raise HTTPException(
                status_code=409,
                detail={"code": "mail_model_consent_fields_mismatch", "message": "The reviewed Mail consent does not cover the reply payload", "recovery_action": "review_model_consent"},
            )
        binding = await _binding_for(db, owner, body.message_binding_id, connection.connection_id)
        if (
            binding.connection_revision != connection.revision
            or binding.message_revision != body.expected_message_revision
            or binding.source_consent_id != consent.consent_id
            or int(binding.source_consent_revision or 0) != int(consent.source_revision)
            or binding.source_label_scope_digest != _source_label_scope_digest(connection, consent)
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "mail_message_scope_stale", "message": "The selected Mail message is outside the reviewed scope", "recovery_action": "rescan_messages"},
            )
        await repository._validate_goal(db, owner, goal_id=body.goal_id, goal_revision=body.expected_goal_revision)
        goal = (
            await db.execute(
                select(Goal)
                .where(
                    Goal.id == body.goal_id,
                    Goal.owner_principal_id == owner.principal_id,
                    Goal.owner_session_id == owner.session_id,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        goal_status = getattr(getattr(goal, "status", None), "value", getattr(goal, "status", None))
        if goal is None or int(goal.revision or 0) != int(body.expected_goal_revision) or goal_status != "active":
            raise HTTPException(
                status_code=409,
                detail={"code": "goal_not_admitted", "message": "The reply draft goal is not currently active", "recovery_action": "refresh_goal"},
            )

        artifact_request = WorkBoardInputArtifactCreate(
            schema_version=1,
            capability_id="work.mail-reply-draft.v1",
            goal_id=body.goal_id,
            goal_revision=body.expected_goal_revision,
            input=inputs,
            idempotency_key=f"mail-reply:{body.idempotency_key}",
        )
        try:
            metadata = await prepare_input_artifact(db, owner, artifact_request)
        except BoardError as exc:
            raise _error(exc) from exc

        expected_connection_revision = int(connection.revision)
        expected_source_revision = int(consent.source_revision)
        expected_model_revision = int(consent.model_revision)
        expected_binding_revision = int(binding.revision)
        expected_goal_revision = int(body.expected_goal_revision)

        async def assert_publication_authority(publication_db: Any) -> None:
            """Repeat all reply authority checks inside the task writer fence."""

            await _assert_live_session(publication_db, owner)
            current_connection = await _connection_for(publication_db, owner, body.connection_id)
            current_consent = await _consent_row_for(publication_db, owner, body.mail_consent_id)
            current_binding = await _binding_for(publication_db, owner, body.message_binding_id, body.connection_id)
            current_goal = (
                await publication_db.execute(
                    select(Goal)
                    .where(
                        Goal.id == body.goal_id,
                        Goal.owner_principal_id == owner.principal_id,
                        Goal.owner_session_id == owner.session_id,
                    )
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            current_status = getattr(getattr(current_goal, "status", None), "value", getattr(current_goal, "status", None))
            if (
                current_goal is None
                or current_status != "active"
                or int(current_goal.revision or 0) != expected_goal_revision
                or current_connection.state != "active"
                or int(current_connection.revision) != expected_connection_revision
                or current_consent.state != "active"
                or not current_consent.source_read_allowed
                or not current_consent.model_egress_allowed
                or int(current_consent.connection_revision) != expected_connection_revision
                or int(current_consent.source_revision) != expected_source_revision
                or int(current_consent.model_revision) != expected_model_revision
                or current_consent.goal_id != body.goal_id
                or int(current_consent.goal_revision) != expected_goal_revision
                or _aware(current_consent.expires_at) <= _now()
                or current_binding.status != "present"
                or int(current_binding.connection_revision) != expected_connection_revision
                or int(current_binding.revision) != expected_binding_revision
                or current_binding.message_revision != body.expected_message_revision
                or current_binding.source_consent_id != current_consent.consent_id
                or int(current_binding.source_consent_revision or 0) != expected_source_revision
                or current_binding.source_label_scope_digest != _source_label_scope_digest(current_connection, current_consent)
            ):
                raise GmailReadError("mail_reply_authority_stale", "Mail reply authority changed during publication", status_code=409, recovery_action="reload_reply_context")

        task_request = WorkBoardTaskCreate(
            title="Prepare a private Mail reply draft",
            body="Operator requested a reviewed local reply draft.",
            goal_id=body.goal_id,
            goal_revision=body.expected_goal_revision,
            status="todo",
            capability_id="work.mail-reply-draft.v1",
            input_artifact_id=metadata.artifact_id,
            priority=70,
            idempotency_scope="mail-reply-draft",
            idempotency_key=body.idempotency_key,
        )
        try:
            mutation = await repository.create_task(
                db,
                owner,
                task_request,
                origin_session_id=owner.session_id,
                publication_authority_check=assert_publication_authority,
            )
        except (BoardError, GmailReadError) as exc:
            raise _error(exc) from exc
        return JSONResponse(
            content=_reply_task_response(mutation.task, metadata, replay=mutation.idempotent_replay),
            status_code=200 if mutation.idempotent_replay else 201,
        )


@router.get("/capabilities/mail/reply-tasks/recovery/{idempotency_key}")
async def recover_reply_task(request: Request, idempotency_key: str) -> dict[str, Any]:
    """Resolve one lost reply response by its original owner-bound key."""

    operator = _operator(request)
    owner = _owner(operator)
    if not _SAFE_REQUEST.fullmatch(idempotency_key):
        raise HTTPException(status_code=422, detail={"code": "mail_request_invalid", "message": "The Mail request is invalid", "recovery_action": "correct_request"})
    async with get_session() as db:
        await _assert_live_session(db, owner)
        task = (
            await db.execute(
                select(WorkBoardTask)
                .where(
                    WorkBoardTask.owner_principal_id == owner.principal_id,
                    WorkBoardTask.owner_session_id == owner.session_id,
                    WorkBoardTask.capability_id == "work.mail-reply-draft.v1",
                    WorkBoardTask.idempotency_scope == "mail-reply-draft",
                    WorkBoardTask.idempotency_key == idempotency_key,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if task is None:
            return _mail_reply_recovery_payload(
                idempotency_key=idempotency_key,
                task=None,
                attempt=None,
                run=None,
            )
        attempt = (
            await db.execute(
                select(WorkBoardAttempt)
                .where(WorkBoardAttempt.task_id == task.task_id)
                .order_by(WorkBoardAttempt.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
        run = None
        if attempt is not None and attempt.workflow_run_id:
            run = (
                await db.execute(
                    select(WorkflowRunState)
                    .where(
                        WorkflowRunState.run_identity == attempt.workflow_run_id,
                        WorkflowRunState.job_kind == "mail_reply_draft",
                        WorkflowRunState.owner_kind == "user",
                        WorkflowRunState.owner_principal_id == owner.principal_id,
                        WorkflowRunState.operator_session_id == owner.session_id,
                        WorkflowRunState.goal_id == task.goal_id,
                        WorkflowRunState.goal_revision == int(task.goal_revision),
                    )
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
        return _mail_reply_recovery_payload(
            idempotency_key=idempotency_key,
            task=task,
            attempt=attempt,
            run=run,
        )


@router.get("/capabilities/mail/reply-tasks/{task_id}/draft")
async def get_reply_draft(request: Request, task_id: str) -> dict[str, Any]:
    """Return a private draft only through its owner-scoped Mail view."""

    operator = _operator(request)
    owner = _owner(operator)
    if not _SAFE_REQUEST.fullmatch(task_id):
        raise HTTPException(status_code=422, detail={"code": "mail_request_invalid", "message": "The Mail request is invalid", "recovery_action": "correct_request"})
    async with get_session() as db:
        await _assert_live_session(db, owner)
        task = await repository.get_task(db, owner, task_id)
        if task.capability_id != "work.mail-reply-draft.v1":
            raise HTTPException(status_code=404, detail={"code": "mail_reply_not_found", "message": "The Mail reply draft is unavailable", "recovery_action": "reload_task"})
        # Generic Work Board projections intentionally strip private path
        # references.  Resolve the draft only through the exact owner-bound
        # attempt and its durable run, where the private receipt remains
        # available to this capability-specific route.
        attempt = (
            await db.execute(
                select(WorkBoardAttempt)
                .where(WorkBoardAttempt.task_id == task.task_id)
                .order_by(WorkBoardAttempt.created_at.desc())
            )
        ).scalars().first()
        run = None
        if attempt is not None and attempt.workflow_run_id:
            run = (
                await db.execute(
                    select(WorkflowRunState).where(
                        WorkflowRunState.run_identity == attempt.workflow_run_id,
                        WorkflowRunState.job_kind == "mail_reply_draft",
                        WorkflowRunState.owner_kind == "user",
                        WorkflowRunState.owner_principal_id == owner.principal_id,
                        WorkflowRunState.operator_session_id == owner.session_id,
                        WorkflowRunState.goal_id == task.goal_id,
                        WorkflowRunState.goal_revision == int(task.goal_revision),
                    )
                )
            ).scalar_one_or_none()
        try:
            artifacts = json.loads(run.artifact_receipts_json or "[]") if run is not None else []
            effects = json.loads(run.effect_receipts_json or "[]") if run is not None else []
        except (TypeError, ValueError, json.JSONDecodeError):
            artifacts, effects = [], []
        receipt = next(
            (
                item
                for item in reversed(artifacts if isinstance(artifacts, list) else [])
                if (
                    isinstance(item, dict)
                    and item.get("artifact_type") == "mail_reply_draft"
                    and item.get("exists") is True
                    and isinstance(item.get("file_path"), str)
                    and bool(item.get("content_sha256"))
                )
            ),
            None,
        )
        readback = next(
            (
                item
                for item in reversed(effects if isinstance(effects, list) else [])
                if (
                    isinstance(item, dict)
                    and item.get("receipt_kind") == "readback"
                    and item.get("effect_type") == "mail_reply_draft"
                    and item.get("status") == "succeeded"
                    and isinstance(item.get("details"), dict)
                    and item["details"].get("verified") is True
                    and item.get("target_path") == (receipt or {}).get("file_path")
                    and item.get("target_digest") == (receipt or {}).get("content_sha256")
                    and item.get("content_sha256") == (receipt or {}).get("content_sha256")
                )
            ),
            None,
        )
        if receipt is None or readback is None:
            return {
                "status": "pending" if task.status not in {WorkBoardStatus.done, WorkBoardStatus.blocked} else "blocked",
                "task_id": task.task_id,
                "recovery_action": "reconcile_existing_reply" if task.status is WorkBoardStatus.blocked else "wait_for_dispatch",
                "memory_status": "no_learning",
            }
        try:
            from src.workflows.mail_reply_draft import read_private_draft

            payload = read_private_draft(str(receipt.get("file_path") or ""), str(receipt.get("content_sha256") or ""))
        except Exception as exc:
            raise HTTPException(status_code=409, detail={"code": "mail_reply_artifact_unavailable", "message": "The private Mail draft requires reconciliation", "recovery_action": "reconcile_existing_reply"}) from exc
        return {
            "status": "verified",
            "task_id": task.task_id,
            "draft": {
                "subject": str(payload.get("subject") or ""),
                "plainbody": str(payload.get("plainbody") or ""),
                "caveats": list(payload.get("caveats") or []),
            },
            "message_revision": str(payload.get("message_revision") or ""),
            "memory_status": "no_learning",
            "sent": False,
            "saved_to_provider": False,
        }


def _reply_profile_metadata(row):
    return {"connection_id": row.connection_id, "service": row.service, "label": row.label,
        "revision": row.revision, "state": row.state, "scope_status": row.scope_status,
        "declared_scopes": json.loads(row.declared_scopes_json),
        "verified_setup_job_id": row.verified_setup_job_id, "provider_contact": False,
        "setup_is_send_permission": False}


@router.get("/capabilities/mail/reply-profiles")
async def list_reply_profiles(request: Request):
    from src.integrations.mail_reply_send import current_root
    operator = _operator(request)
    async with get_session() as db:
        await current_root(db, operator)
        rows = (await db.execute(select(GoogleServiceConnection).where(
            GoogleServiceConnection.owner_principal_id == operator.principal.principal_id,
            GoogleServiceConnection.owner_session_id == operator.session_id,
            GoogleServiceConnection.service.in_(["gmail_reply_read", "gmail_reply_send"])
        ).order_by(GoogleServiceConnection.created_at.desc()).limit(32))).scalars().all()
        return {"profiles": [_reply_profile_metadata(row) for row in rows], "provider_contact": False}


@router.post("/capabilities/mail/reply-profiles")
async def create_reply_profile(request: Request):
    from src.integrations.gmail_send import SCOPES, digest as exact_digest
    from src.integrations.mail_reply_send import current_root
    from src.integrations.mail_reply_runtime import writer
    operator = _operator(request)
    body = await _json_body(request, ReplyConnectionCreate)
    if (len(set(body.declared_scopes)) != 3 or frozenset(body.declared_scopes) != SCOPES[body.service]
        or any(not _credential_value_allowed(value) for value in (body.client_id, body.refresh_token, body.client_secret or "x"))):
        raise HTTPException(status_code=422, detail={"code": "mail_reply_profile_invalid"})
    request_digest = exact_digest(body.model_dump())
    credentials = {"client_id": body.client_id, "client_secret": body.client_secret, "refresh_token": body.refresh_token}
    async with _MAIL_CONNECTION_CREATE_LOCK:
        async with writer() as db:
            await current_root(db, operator)
            row = (await db.execute(select(GoogleServiceConnection).where(
                GoogleServiceConnection.owner_principal_id == operator.principal.principal_id,
                GoogleServiceConnection.owner_session_id == operator.session_id,
                GoogleServiceConnection.setup_idempotency_key == body.idempotency_key))).scalar_one_or_none()
            if row is not None:
                if row.setup_request_digest != request_digest or row.service != body.service:
                    raise HTTPException(status_code=409, detail={"code": "mail_reply_setup_conflict"})
                if row.state == "active":
                    return {"profile": _reply_profile_metadata(row)}
                if row.state != "preparing":
                    raise HTTPException(status_code=409, detail={"code": "mail_reply_setup_blocked"})
            else:
                row = GoogleServiceConnection(owner_principal_id=operator.principal.principal_id,
                    owner_session_id=operator.session_id, service=body.service, label=body.label,
                    vault_secret_key="gmail-reply:"+secrets.token_urlsafe(24),
                    credential_fingerprint=exact_digest(credentials),
                    declared_scopes_json=json.dumps(sorted(SCOPES[body.service])),
                    setup_idempotency_key=body.idempotency_key, setup_request_digest=request_digest,
                    state="preparing", revision=1)
                db.add(row)
                await db.flush()
            ident, key = row.connection_id, row.vault_secret_key
        # Encryption/Vault audit and its own session are OUTSIDE this writer.
        raw = json.dumps(credentials, sort_keys=True)
        prior = await vault_repository.get(key, owner_principal_id=operator.principal.principal_id)
        if prior is None:
            await vault_repository.store(key, raw, description="Separate exact Gmail reply identity profile",
                owner_principal_id=operator.principal.principal_id)
        elif prior != raw:
            raise HTTPException(status_code=409, detail={"code": "mail_reply_setup_conflict"})
        async with writer() as db:
            await current_root(db, operator)
            row = await db.get(GoogleServiceConnection, ident, populate_existing=True)
            if row is None or row.setup_request_digest != request_digest or row.state != "preparing":
                raise HTTPException(status_code=409, detail={"code": "mail_reply_setup_changed"})
            row.state = "active"
            row.revision += 1
            row.updated_at = _now()
            return {"profile": _reply_profile_metadata(row)}


@router.get("/capabilities/mail/reply-profiles/recovery/{idempotency_key}")
async def recover_reply_profile(request: Request, idempotency_key: str):
    from src.integrations.mail_reply_send import current_root
    operator = _operator(request)
    if not _SAFE_REQUEST.fullmatch(idempotency_key):
        raise HTTPException(status_code=422, detail={"code": "mail_request_invalid"})
    async with get_session() as db:
        await current_root(db, operator)
        row = (await db.execute(select(GoogleServiceConnection).where(
            GoogleServiceConnection.owner_principal_id == operator.principal.principal_id,
            GoogleServiceConnection.owner_session_id == operator.session_id,
            GoogleServiceConnection.service.in_(["gmail_reply_read", "gmail_reply_send"]),
            GoogleServiceConnection.setup_idempotency_key == idempotency_key))).scalar_one_or_none()
        return {"profile": _reply_profile_metadata(row) if row else None, "provider_contact": False}


@router.post("/capabilities/mail/reply-profiles/{connection_id}/revoke")
async def revoke_reply_profile(request: Request, connection_id: str):
    from src.integrations.mail_reply_send import current_root
    from src.integrations.mail_reply_runtime import writer
    operator = _operator(request)
    body = await _json_body(request, ConnectionControl)
    request_digest = digest([connection_id, body.model_dump()])
    async with writer() as db:
        await current_root(db, operator)
        row = await db.get(GoogleServiceConnection, connection_id, populate_existing=True)
        if (row is None or row.owner_principal_id != operator.principal.principal_id
            or row.owner_session_id != operator.session_id or row.service not in {"gmail_reply_read", "gmail_reply_send"}):
            raise HTTPException(status_code=404, detail={"code": "mail_reply_profile_missing"})
        if row.revoke_idempotency_key:
            if row.revoke_request_digest != request_digest:
                raise HTTPException(status_code=409, detail={"code": "mail_reply_revoke_conflict"})
            return {"profile": _reply_profile_metadata(row)}
        if row.revision != body.expected_revision:
            raise HTTPException(status_code=409, detail={"code": "mail_reply_revision_changed"})
        row.state = "revoked"
        row.revision += 1
        row.revoke_idempotency_key = body.idempotency_key
        row.revoke_request_digest = request_digest
        row.updated_at = _now()
        # Keeping quarantined encrypted bytes is deliberate: revocation is
        # immediately canonical and never authorizes another Vault egress.
        return {"profile": _reply_profile_metadata(row), "credential_cleanup": "encrypted_quarantine"}


async def _reply_expected_pair(operator, body, *, send=True):
    from src.integrations.mail_reply_runtime import pair_snapshots
    rows = await pair_snapshots(operator, body.read_connection_id, body.send_connection_id if send else None)
    if rows[0]["revision"] != body.expected_read_revision or (send and rows[1]["revision"] != body.expected_send_revision):
        raise HTTPException(status_code=409, detail={"code": "mail_reply_revision_changed"})


@router.get("/capabilities/mail/reply-operations/recovery/{kind}/{request_uuid}")
async def recover_reply_operation(request: Request, kind: str, request_uuid: str):
    from src.integrations import mail_reply_runtime as runtime
    if kind not in {runtime.IDENTITY_KIND, runtime.SEND_KIND, runtime.OBSERVATION_KIND} or not _SAFE_REQUEST.fullmatch(request_uuid):
        raise HTTPException(status_code=422, detail={"code": "mail_request_invalid"})
    operator = _operator(request)
    try:
        return {"job": await runtime.snapshot(operator, runtime.job_id(operator, kind, request_uuid)), "provider_contact": False}
    except runtime.DurableJobNotFound:
        return {"job": None, "provider_contact": False}
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/capabilities/mail/reply-profiles/verify-pair")
async def verify_reply_pair(request: Request):
    from src.integrations import mail_reply_runtime as runtime
    operator = _operator(request)
    body = await _json_body(request, ReplyPairVerify)
    try:
        request_binding = runtime.digest(body.model_dump())
        replay = await runtime.request_replay(operator, kind=runtime.IDENTITY_KIND, request_uuid=body.request_uuid, request_binding=request_binding)
        if replay is not None:
            return replay
        await _reply_expected_pair(operator, body)
        return await runtime.run_owned(operator, runtime.job_id(operator, runtime.IDENTITY_KIND, body.request_uuid),
            lambda: runtime.verify_pair(operator, request_uuid=body.request_uuid,
                goal_id=body.goal_id, goal_revision=body.goal_revision, priority=body.priority, request_binding=request_binding,
                read_connection_id=body.read_connection_id, send_connection_id=body.send_connection_id))
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/capabilities/mail/reply-sends/preview")
async def preview_reply_send(request: Request):
    from src.integrations import mail_reply_runtime as runtime
    operator = _operator(request)
    body = await _json_body(request, ReplySendPreview)
    try:
        request_binding = runtime.digest(body.model_dump())
        replay = await runtime.request_replay(operator, kind=runtime.SEND_KIND, request_uuid=body.request_uuid, request_binding=request_binding)
        if replay is not None:
            return replay
        await _reply_expected_pair(operator, body)
        staged = await runtime.stage_source(operator, body.task_id)
        if staged["message_revision"] != body.expected_message_revision:
            raise HTTPException(status_code=409, detail={"code": "mail_reply_source_changed"})
        return await runtime.run_owned(operator, runtime.job_id(operator, runtime.SEND_KIND, body.request_uuid),
            lambda: runtime.preview(operator, task_id=body.task_id, read_connection_id=body.read_connection_id,
                send_connection_id=body.send_connection_id, request_uuid=body.request_uuid, priority=body.priority, request_binding=request_binding))
    except HTTPException:
        raise
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/capabilities/mail/reply-sends/{job_id}")
async def get_reply_send(request: Request, job_id: str):
    from src.integrations.mail_reply_runtime import snapshot
    try:
        return await snapshot(_operator(request), job_id)
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/capabilities/mail/reply-sends/{job_id}/decision")
async def decide_reply_send(request: Request, job_id: str):
    from src.integrations.mail_reply_runtime import decide
    body = await _json_body(request, ReplyDecision)
    try:
        return await decide(_operator(request), job_id, decision=body.decision, expected_digest=body.expected_digest)
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/capabilities/mail/reply-sends/{job_id}/execute")
async def execute_reply_send(request: Request, job_id: str):
    from src.integrations import mail_reply_runtime as runtime
    operator = _operator(request)
    # No caller supplies a MIME resource, recipient, approval or provider URL.
    await _json_body(request, _Strict)
    try:
        return await runtime.run_owned(operator, job_id, lambda: runtime.execute(operator, job_id))
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/capabilities/mail/reply-sends/{job_id}/cancel")
async def cancel_reply_send(request: Request, job_id: str):
    from src.integrations.mail_reply_runtime import cancel
    body = await _json_body(request, ReplyCancel)
    try:
        return await cancel(_operator(request), job_id, request_uuid=body.request_uuid, expected_revision=body.expected_revision)
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/capabilities/mail/reply-sends/{job_id}/observe")
async def observe_reply_send(request: Request, job_id: str):
    from src.integrations import mail_reply_runtime as runtime
    operator = _operator(request)
    body = await _json_body(request, ReplyObservation)
    try:
        request_binding = runtime.digest(body.model_dump())
        replay = await runtime.request_replay(operator, kind=runtime.OBSERVATION_KIND, request_uuid=body.request_uuid, request_binding=request_binding)
        if replay is not None:
            return replay
        await _reply_expected_pair(operator, body, send=False)
        return await runtime.run_owned(operator, runtime.job_id(operator, runtime.OBSERVATION_KIND, body.request_uuid),
            lambda: runtime.observe(operator, original_job_id=job_id,
                expected_original_revision=body.expected_original_revision, read_connection_id=body.read_connection_id,
                goal_id=body.goal_id, goal_revision=body.goal_revision, request_uuid=body.request_uuid, priority=body.priority, request_binding=request_binding))
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/capabilities/mail/watches", response_model=None)
async def create_mail_watch(request: Request) -> Any:
    """Create one finite metadata-only Gmail watch.

    The watch is a governed schedule binding.  It never creates a second
    polling loop or a model task; the scheduler owns each fenced occurrence.
    """

    operator = _operator(request)
    body = await _json_body(request, MailWatchCreate)
    owner = _owner(operator)
    expires_at = _aware(body.expires_at)
    now = _now()
    if expires_at <= now or expires_at > now + timedelta(days=7):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "mail_watch_expiry_invalid",
                "message": "The Mail watch expiry is outside the seven-day bound",
                "recovery_action": "choose_bounded_expiry",
            },
        )
    timezone_name = _validate_watch_timezone(body.cadence.timezone)
    cadence = body.cadence.model_dump(mode="json")
    request_digest = "sha256:" + digest(
        {
            "schema_version": 1,
            "connection_id": body.connection_id,
            "expected_connection_revision": body.expected_connection_revision,
            "mail_consent_id": body.mail_consent_id,
            "expected_source_consent_revision": body.expected_source_consent_revision,
            "goal_id": body.goal_id,
            "expected_goal_revision": body.expected_goal_revision,
            "label_ids": sorted(body.label_ids),
            "cadence": cadence,
            "expires_at": expires_at.isoformat(),
            "max_messages": body.max_messages,
            "idempotency_key": body.idempotency_key,
        }
    )
    async with get_session() as db:
        try:
            await _begin_serialized(db)
            await _assert_live_session(db, owner)
            connection = await _connection_for(db, owner, body.connection_id)
            if connection.state != "active" or int(connection.revision) != int(body.expected_connection_revision):
                raise HTTPException(status_code=409, detail={"code": "mail_connection_revision_stale", "message": "The Gmail connection changed", "recovery_action": "reload_connection"})
            consent = await _consent_for(db, owner, body.mail_consent_id)
            if (
                consent.connection_id != connection.connection_id
                or int(consent.connection_revision) != int(connection.revision)
                or int(consent.source_revision) != int(body.expected_source_consent_revision)
                or consent.goal_id != body.goal_id
                or int(consent.goal_revision) != int(body.expected_goal_revision)
                or expires_at > _aware(consent.expires_at)
            ):
                raise HTTPException(status_code=409, detail={"code": "mail_consent_revision_stale", "message": "The reviewed Mail consent changed", "recovery_action": "reload_consent"})
            if sorted(_consent_label_ids(consent)) != sorted(body.label_ids):
                raise HTTPException(status_code=409, detail={"code": "mail_consent_scope_mismatch", "message": "The watch labels differ from the reviewed Mail scope", "recovery_action": "reload_consent"})
            await _selected_provider_labels(db, owner, connection, body.label_ids)
            goal = await _validate_watch_goal(
                db,
                owner,
                goal_id=body.goal_id,
                goal_revision=body.expected_goal_revision,
                expires_at=expires_at,
            )
            existing = (
                await db.execute(
                    select(GovernedScheduleBinding).where(
                        GovernedScheduleBinding.owner_principal_id == owner.principal_id,
                        GovernedScheduleBinding.owner_session_id == owner.session_id,
                        GovernedScheduleBinding.schedule_idempotency_key == body.idempotency_key,
                    ).execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing.schedule_request_digest != request_digest:
                    raise HTTPException(status_code=409, detail={"code": "mail_watch_idempotency_conflict", "message": "The watch key is bound to another request", "recovery_action": "use_new_idempotency_key"})
                state = await db.get(MailWatchState, existing.binding_id, populate_existing=True)
                occurrence = (
                    await db.execute(
                        select(GovernedScheduleOccurrence)
                        .where(GovernedScheduleOccurrence.binding_id == existing.binding_id)
                        .order_by(GovernedScheduleOccurrence.updated_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                return {"watch": _watch_metadata(existing, state, occurrence, label_ids=body.label_ids), "status": "replayed"}

            metadata = await create_mail_watch_input_artifact(
                db,
                owner,
                consent=consent,
                connection_id=connection.connection_id,
                label_ids=body.label_ids,
                max_messages=body.max_messages,
                idempotency_key=f"mail-watch:{body.idempotency_key}",
                retention_deadline=expires_at,
            )
            # The artifact helper performs filesystem publication between
            # transactions. Re-read all authority after that boundary before
            # creating the governed binding.
            await _begin_serialized(db)
            await _assert_live_session(db, owner)
            current_connection = await _connection_for(db, owner, body.connection_id)
            current_consent = await _consent_for(db, owner, body.mail_consent_id)
            if (
                current_connection.revision != connection.revision
                or current_consent.revision != consent.revision
                or current_consent.source_revision != consent.source_revision
            ):
                raise HTTPException(status_code=409, detail={"code": "mail_watch_revision_stale", "message": "Mail authority changed while preparing the watch", "recovery_action": "reload_mail_setup"})
            action_digest = "sha256:" + digest(
                {
                    "action_type": "gmail.scan_metadata.v1",
                    "consent_id": current_consent.consent_id,
                    "consent_revision": current_consent.source_revision,
                    "consent_digest": current_consent.source_digest,
                    "input_digest": metadata.typed_input_digest,
                    "cadence": cadence,
                }
            )
            binding = await create_binding(
                db,
                owner,
                {
                    "name": "Gmail metadata watch",
                    "action_type": "gmail.scan_metadata.v1",
                    "capability_id": "gmail.scan_metadata.v1",
                    "cadence": cadence,
                    "expires_at": expires_at,
                    "idempotency_key": body.idempotency_key,
                    "goal_id": body.goal_id,
                    "goal_revision": body.expected_goal_revision,
                    "consent_id": current_consent.consent_id,
                    "consent_revision": current_consent.source_revision,
                    "consent_digest": current_consent.source_digest,
                    "input_artifact_id": metadata.artifact_id,
                    "input_digest": metadata.typed_input_digest,
                    "action_digest": action_digest,
                    "schedule_request_digest": request_digest,
                },
            )
            state = MailWatchState(
                binding_id=binding.binding_id,
                owner_principal_id=owner.principal_id,
                owner_session_id=owner.session_id,
                connection_id=current_connection.connection_id,
                connection_revision=current_connection.revision,
                consent_id=current_consent.consent_id,
                source_consent_revision=current_consent.source_revision,
                goal_id=body.goal_id,
                goal_revision=body.expected_goal_revision,
                revision=1,
                state="not_started",
                baseline_complete=False,
                seen_message_keys_json="[]",
                seen_message_keys_digest="sha256:" + digest([]),
                list_page_complete=False,
                skipped_coverage_reason="baseline_pending",
            )
            db.add(state)
            await db.flush()
            return JSONResponse(
                content={"watch": _watch_metadata(binding, state, label_ids=body.label_ids), "status": "accepted"},
                status_code=201,
            )
        except HTTPException:
            raise
        except (GmailReadError, BoardError) as exc:
            raise _error(exc) from exc
        except RuntimeError as exc:
            code = str(exc)
            mapped = {
                "governed_schedule_capacity_exceeded": (429, "mail_watch_capacity_exceeded", "pause_or_revoke_existing_watch"),
                "governed_schedule_idempotency_conflict": (409, "mail_watch_idempotency_conflict", "use_new_idempotency_key"),
            }.get(code, (409, "mail_watch_reconciliation_required", "reconcile_watch"))
            raise HTTPException(status_code=mapped[0], detail={"code": mapped[1], "message": "The Mail watch requires reconciliation", "recovery_action": mapped[2]}) from exc
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=422, detail={"code": "mail_watch_invalid", "message": "The Mail watch is invalid", "recovery_action": "correct_watch"}) from exc


@router.get("/capabilities/mail/watches/recovery/{idempotency_key}")
async def recover_mail_watch(request: Request, idempotency_key: str) -> dict[str, Any]:
    """Resolve one lost watch response by its original owner-bound key."""

    operator = _operator(request)
    owner = _owner(operator)
    if not _SAFE_REQUEST.fullmatch(idempotency_key):
        raise HTTPException(status_code=422, detail={"code": "mail_request_invalid", "message": "The Mail request is invalid", "recovery_action": "correct_request"})
    async with get_session() as db:
        await _assert_live_session(db, owner)
        binding = (
            await db.execute(
                select(GovernedScheduleBinding)
                .where(
                    GovernedScheduleBinding.owner_principal_id == owner.principal_id,
                    GovernedScheduleBinding.owner_session_id == owner.session_id,
                    GovernedScheduleBinding.action_type == "gmail.scan_metadata.v1",
                    GovernedScheduleBinding.schedule_idempotency_key == idempotency_key,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if binding is None:
            return {
                "status": "not_found",
                "idempotency_scope": "mail-watch",
                "idempotency_key": idempotency_key,
                "watch": None,
                "watch_id": None,
                "input_artifact_id": None,
                "input_digest": None,
                "request_digest": None,
                "goal_id": None,
                "goal_revision": None,
                "recovery_action": "retry_same_key",
                "memory_status": "no_learning",
            }
        state = await db.get(MailWatchState, binding.binding_id, populate_existing=True)
        occurrence = (
            await db.execute(
                select(GovernedScheduleOccurrence)
                .where(GovernedScheduleOccurrence.binding_id == binding.binding_id)
                .order_by(GovernedScheduleOccurrence.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        watch = _watch_metadata(binding, state, occurrence, label_ids=await _watch_label_ids(db, binding))
        return {
            "status": "replayed",
            "idempotency_scope": "mail-watch",
            "idempotency_key": idempotency_key,
            "watch": watch,
            "watch_id": binding.binding_id,
            "input_artifact_id": binding.input_artifact_id,
            "input_digest": binding.input_digest,
            "request_digest": binding.schedule_request_digest,
            "goal_id": binding.goal_id,
            "goal_revision": int(binding.goal_revision),
            "recovery_action": None,
            "memory_status": "no_learning",
        }


@router.get("/capabilities/mail/watches")
async def list_mail_watches(request: Request) -> dict[str, Any]:
    """List only owner/session-bound Mail metadata watches."""

    operator = _operator(request)
    owner = _owner(operator)
    async with get_session() as db:
        await _assert_live_session(db, owner)
        rows = (
            await db.execute(
                select(GovernedScheduleBinding)
                .where(
                    GovernedScheduleBinding.owner_principal_id == owner.principal_id,
                    GovernedScheduleBinding.owner_session_id == owner.session_id,
                    GovernedScheduleBinding.action_type == "gmail.scan_metadata.v1",
                )
                .order_by(GovernedScheduleBinding.created_at.desc())
                .limit(100)
            )
        ).scalars().all()
        result: list[dict[str, Any]] = []
        for binding in rows:
            state = await db.get(MailWatchState, binding.binding_id, populate_existing=True)
            occurrence = (
                await db.execute(
                    select(GovernedScheduleOccurrence)
                    .where(GovernedScheduleOccurrence.binding_id == binding.binding_id)
                    .order_by(GovernedScheduleOccurrence.updated_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            labels = await _watch_label_ids(db, binding)
            result.append(_watch_metadata(binding, state, occurrence, label_ids=labels))
        return {"watches": result}


@router.get("/capabilities/mail/watches/{watch_id}")
async def get_mail_watch(request: Request, watch_id: str) -> dict[str, Any]:
    operator = _operator(request)
    owner = _owner(operator)
    if not _SAFE_REQUEST.fullmatch(watch_id):
        raise HTTPException(status_code=422, detail={"code": "mail_request_invalid", "message": "The Mail request is invalid", "recovery_action": "correct_request"})
    async with get_session() as db:
        await _assert_live_session(db, owner)
        binding = (
            await db.execute(
                select(GovernedScheduleBinding)
                .where(
                    GovernedScheduleBinding.binding_id == watch_id,
                    GovernedScheduleBinding.owner_principal_id == owner.principal_id,
                    GovernedScheduleBinding.owner_session_id == owner.session_id,
                    GovernedScheduleBinding.action_type == "gmail.scan_metadata.v1",
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if binding is None:
            raise HTTPException(status_code=404, detail={"code": "mail_watch_not_found", "message": "The Mail watch is unavailable", "recovery_action": "reload_watches"})
        state = await db.get(MailWatchState, binding.binding_id, populate_existing=True)
        occurrence = (
            await db.execute(
                select(GovernedScheduleOccurrence)
                .where(GovernedScheduleOccurrence.binding_id == binding.binding_id)
                .order_by(GovernedScheduleOccurrence.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return {"watch": _watch_metadata(binding, state, occurrence, label_ids=await _watch_label_ids(db, binding))}


__all__ = ["router"]
