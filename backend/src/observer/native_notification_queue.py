"""Durable outbox for the built-in native desktop notification path.

The queue is intentionally a small delivery state machine shared by the
observer API and the macOS daemon. It is not an external transport and an
acknowledgement only proves that Seraph accepted a daemon receipt; it cannot
prove exactly-once display outside the process.

State transitions::

    queued -> claimed -> display_attempted -> delivered
                     \\-> unknown (lease expiry, daemon failure, or cancellation)
    queued -> cancelled (operator dismiss before a daemon claim)
    failed -> queued (explicit bounded retry before deadline)

``claimed`` and ``display_attempted`` are fenced by the daemon identity and
monotonic fencing token. An ambiguous external display is never replayed
automatically; an operator must reconcile it before requesting a retry.

The outbox and attempt rows are stored in the canonical SQLite database. The
public queue methods retain the old notification response shape while adding
fenced status metadata for daemon clients.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError

from src.db import engine as db_engine
from src.db.models import (
    NativeNotificationDeliveryAttempt,
    NativeNotificationOutbox,
    OperatorSession,
    QueuedInsight,
    Session,
)
from src.conversation.identity import (
    ConversationIdentityError,
    build_conversation_identity,
    validate_attachment_refs,
)


ACTIVE_STATUSES = frozenset({"queued", "claimed", "display_attempted"})
CLAIMED_STATUSES = frozenset({"claimed", "display_attempted"})
TERMINAL_STATUSES = frozenset({"delivered", "failed", "cancelled", "unknown"})
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_LEASE_SECONDS = 30
DEFAULT_TTL_SECONDS = 24 * 60 * 60
MAX_IDEMPOTENCY_KEY_CHARS = 256
MAX_IDENTIFIER_CHARS = 256
MAX_TITLE_CHARS = 240
MAX_BODY_CHARS = 8_000
MAX_RESUME_MESSAGE_CHARS = 4_000
MAX_REASON_CHARS = 200
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SECRET_VALUE = re.compile(
    r"(?i)(api[_-]?key|authorization|bearer|password|passphrase|secret|token)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)


class NativeNotificationConflictError(ValueError):
    """Raised when one idempotency key is reused with another payload."""


class NativeNotificationBudgetDenied(ValueError):
    """Raised when a durable standing-goal notification reservation is full."""

    def __init__(self, *, goal_id: str, budget_period_key: str, budget_limit: int) -> None:
        self.goal_id = goal_id
        self.budget_period_key = budget_period_key
        self.budget_limit = budget_limit
        super().__init__("goal_budget_notification_limit")


class NativeNotificationLeaseError(ValueError):
    """Raised for malformed or stale daemon lease state."""


@dataclass
class NativeNotification:
    id: str
    intervention_id: str | None
    title: str
    body: str
    intervention_type: str | None
    urgency: int | None
    surface: str
    session_id: str | None
    thread_id: str | None
    thread_source: str
    continuation_mode: str
    resume_message: str | None
    created_at: str
    # Additive operator/daemon receipt fields. Existing API consumers may
    # ignore them; daemon clients use the fencing token for stale-ack safety.
    delivery_status: str = "queued"
    attempt_count: int = 0
    fencing_token: int = 0
    conversation_id: str | None = None
    owner_principal_id: str | None = None
    operator_session_id: str | None = None
    device_id: str | None = None
    channel: str = "native_notification"
    transport: str = "native_notification"
    correlation_id: str | None = None
    causation_id: str | None = None
    attachment_refs: list[dict[str, Any]] | None = None
    degraded_state: str | None = None
    goal_id: str | None = None
    budget_period_key: str | None = None
    budget_limit: int | None = None

    def to_dict(self) -> dict[str, str | int | None]:
        return asdict(self)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _validate_text(value: object, *, field: str, max_chars: int, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"native notification {field} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"native notification {field} must be a string")
    if _CONTROL_CHARS.search(value):
        raise ValueError(f"native notification {field} contains control characters")
    if required and not value.strip():
        raise ValueError(f"native notification {field} is required")
    if len(value) > max_chars:
        raise ValueError(f"native notification {field} exceeds {max_chars} characters")
    return value


def _validate_identifier(value: object, *, field: str) -> str | None:
    return _validate_text(value, field=field, max_chars=MAX_IDENTIFIER_CHARS)


def _safe_reason(value: object) -> str:
    """Bound and redact error text before it becomes durable receipt state."""
    raw = str(value or "display_failed").strip()
    raw = _CONTROL_CHARS.sub(" ", raw)
    raw = _SECRET_VALUE.sub(r"\1\2[redacted]", raw)
    return raw[:MAX_REASON_CHARS] or "display_failed"


def _payload_digest(values: dict[str, Any]) -> str:
    canonical = json.dumps(values, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _row_to_notification(row: NativeNotificationOutbox) -> NativeNotification:
    created_at = _aware(row.created_at) or _utc_now()
    return NativeNotification(
        id=row.id,
        intervention_id=row.intervention_id,
        title=row.title,
        body=row.body,
        intervention_type=row.intervention_type,
        urgency=row.urgency,
        surface=row.surface,
        session_id=row.session_id,
        thread_id=row.thread_id,
        thread_source=row.thread_source,
        continuation_mode=row.continuation_mode,
        resume_message=row.resume_message,
        created_at=created_at.isoformat(),
        delivery_status=row.status,
        attempt_count=row.attempt_count,
        fencing_token=row.fencing_token,
        conversation_id=row.conversation_id or row.session_id,
        owner_principal_id=row.owner_principal_id,
        operator_session_id=row.operator_session_id,
        device_id=row.device_id,
        channel=row.channel,
        transport=row.transport,
        correlation_id=row.correlation_id,
        causation_id=row.causation_id,
        attachment_refs=_attachment_refs_from_json(
            row.attachment_refs_json,
            owner_principal_id=row.owner_principal_id,
        ),
        degraded_state=row.degraded_state,
        goal_id=row.goal_id,
        budget_period_key=row.budget_period_key,
        budget_limit=row.budget_limit,
    )


def _attachment_refs_from_json(
    value: str | None,
    *,
    owner_principal_id: str | None = None,
    raise_on_error: bool = False,
) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError) as exc:
        if raise_on_error:
            raise ConversationIdentityError(
                "attachment_reference_invalid",
                "Stored attachment references are malformed.",
            ) from exc
        return []
    if not isinstance(parsed, list):
        if raise_on_error:
            raise ConversationIdentityError(
                "attachment_reference_invalid",
                "Stored attachment references are malformed.",
            )
        return []
    try:
        # Stored rows contain the signed digest/timestamp proof rather than a
        # bearer receipt. Revalidate it on every read so an expired quarantine
        # reference is never returned to a daemon or operator surface.
        return validate_attachment_refs(parsed, owner_principal_id=owner_principal_id)
    except ConversationIdentityError:
        if raise_on_error:
            raise
        return []


def _valid_worker(value: object) -> str:
    worker = _validate_text(value, field="lease owner", max_chars=MAX_IDENTIFIER_CHARS, required=True)
    assert worker is not None
    return worker


def _bind_runtime_identity(
    *,
    session_id: str | None,
    owner_principal_id: str | None,
    operator_session_id: str | None,
) -> tuple[str | None, str | None]:
    """Resolve delivery identity against the authenticated runtime context.

    A queue call may be made by a durable internal adapter without a request
    context, so the already-canonical owner can be supplied by that adapter.
    Whenever a runtime principal is present, caller fields are assertions and
    cannot override its owner, operator session, or conversation scope.
    """
    from src.approval.runtime import get_current_trust_principal

    trusted = get_current_trust_principal()
    if trusted is None:
        if session_id is not None and owner_principal_id is None:
            raise ConversationIdentityError(
                "conversation_owner_missing",
                "A session-bound notification requires an owner principal.",
            )
        return owner_principal_id, operator_session_id
    if not trusted.authenticated or trusted.revoked:
        raise ConversationIdentityError(
            "conversation_authority_revoked",
            "The runtime principal is not authorized for notification delivery.",
        )

    trusted_owner = str(trusted.principal_id or "").strip() or None
    trusted_operator_session = str(trusted.operator_session_id or "").strip() or None
    trusted_session = str(trusted.session_id or "").strip() or None
    if owner_principal_id is not None and owner_principal_id != trusted_owner:
        raise ConversationIdentityError(
            "conversation_owner_mismatch",
            "Notification owner does not match the authenticated runtime principal.",
        )
    if (
        operator_session_id is not None
        and trusted_operator_session is not None
        and operator_session_id != trusted_operator_session
    ):
        raise ConversationIdentityError(
            "operator_session_mismatch",
            "Notification operator session does not match the authenticated runtime session.",
        )
    if session_id is not None and trusted_session is not None and session_id != trusted_session:
        raise ConversationIdentityError(
            "conversation_session_mismatch",
            "Notification session does not match the authenticated runtime conversation.",
        )
    if session_id is not None and owner_principal_id is None:
        owner_principal_id = trusted_owner
    if operator_session_id is None and trusted_operator_session is not None:
        operator_session_id = trusted_operator_session
    if session_id is not None and owner_principal_id is None:
        raise ConversationIdentityError(
            "conversation_owner_missing",
            "A session-bound notification requires an authenticated owner principal.",
        )
    return owner_principal_id, operator_session_id


def _owner_scope_predicate(
    *,
    owner_principal_id: str,
    operator_session_id: str | None,
):
    """Match one operator's rows plus genuinely ambient broadcasts."""
    ambient = and_(
        NativeNotificationOutbox.owner_principal_id.is_(None),
        NativeNotificationOutbox.operator_session_id.is_(None),
        NativeNotificationOutbox.session_id.is_(None),
    )
    operator_match = NativeNotificationOutbox.operator_session_id.is_(None)
    if operator_session_id is not None:
        operator_match = or_(
            operator_match,
            NativeNotificationOutbox.operator_session_id == operator_session_id,
        )
    owned = and_(
        NativeNotificationOutbox.owner_principal_id == owner_principal_id,
        operator_match,
    )
    return or_(ambient, owned)


async def _ensure_outbox_tables(db) -> None:
    """Create only the additive outbox tables when an older runtime starts.

    Normal application startup creates every SQLModel table in ``init_db``.
    This narrow check also keeps legacy operator/test callers that invoke the
    queue before startup from silently falling back to process-local memory.
    It runs on the active session, so test database patches remain isolated.
    """

    def create_tables(sync_session) -> None:
        connection = sync_session.connection()
        NativeNotificationOutbox.__table__.create(connection, checkfirst=True)
        NativeNotificationDeliveryAttempt.__table__.create(connection, checkfirst=True)

        # Queue callers can run before the application lifespan migration (the
        # observer tests and a restarted daemon do this deliberately).  Add the
        # small set of #750 columns in place so an existing SQLite outbox is
        # readable without falling back to process-local state.
        result = connection.exec_driver_sql(
            "PRAGMA table_info(native_notification_outbox)"
        )
        columns = {row[1] for row in result.fetchall()}
        definitions = {
            "goal_id": "VARCHAR",
            "budget_period_key": "VARCHAR",
            "budget_limit": "INTEGER",
            "operator_session_id": "VARCHAR",
            "device_id": "VARCHAR",
            "channel": "VARCHAR DEFAULT 'native_notification'",
            "transport": "VARCHAR DEFAULT 'native_notification'",
            "conversation_id": "VARCHAR",
            "correlation_id": "VARCHAR",
            "causation_id": "VARCHAR",
            "attachment_refs_json": "VARCHAR DEFAULT '[]'",
            "degraded_state": "VARCHAR",
        }
        for column, sql_type in definitions.items():
            if column not in columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE native_notification_outbox ADD COLUMN {column} {sql_type}"
                )

    await db.run_sync(create_tables)


class NativeNotificationQueue:
    """Persistent native-notification outbox with bounded leases and retries."""

    def __init__(
        self,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        if max_attempts < 1 or max_attempts > 10:
            raise ValueError("max_attempts must be between 1 and 10")
        if lease_seconds < 1 or lease_seconds > 300:
            raise ValueError("lease_seconds must be between 1 and 300")
        if ttl_seconds < lease_seconds or ttl_seconds > 7 * 24 * 60 * 60:
            raise ValueError("ttl_seconds must be between lease_seconds and seven days")
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds
        self.ttl_seconds = ttl_seconds
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def _session(self):
        async with db_engine.get_session() as db:
            await _ensure_outbox_tables(db)
            yield db

    async def enqueue(
        self,
        *,
        intervention_id: str | None,
        title: str,
        body: str,
        intervention_type: str | None,
        urgency: int | None,
        surface: str = "notification",
        session_id: str | None = None,
        thread_id: str | None = None,
        thread_source: str = "ambient",
        continuation_mode: str = "open_thread",
        resume_message: str | None = None,
        idempotency_key: str | None = None,
        owner_principal_id: str | None = None,
        operator_session_id: str | None = None,
        device_id: str | None = None,
        channel: str = "native_notification",
        transport: str = "native_notification",
        conversation_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        attachment_refs: object = None,
        source_insight_ids: list[str] | None = None,
        goal_id: str | None = None,
        budget_period_key: str | None = None,
        budget_limit: int | None = None,
    ) -> NativeNotification:
        """Persist one notification or return the matching idempotent row.

        Intervention-backed notifications derive their idempotency key from
        the intervention identity. Test/operator notifications without an
        intervention receive a fresh key unless the caller supplies one.
        Reusing a key with changed content raises a conflict rather than
        silently replacing an already-authorized delivery intent.
        """
        intervention_id = _validate_identifier(intervention_id, field="intervention_id")
        goal_id = _validate_identifier(goal_id, field="goal_id")
        budget_period_key = _validate_identifier(budget_period_key, field="budget_period_key")
        if budget_limit is not None:
            if isinstance(budget_limit, bool) or not isinstance(budget_limit, int) or budget_limit < 0:
                raise ValueError("native notification budget_limit must be a nonnegative integer")
        if (goal_id is None) != (budget_period_key is None) or (goal_id is None) != (budget_limit is None):
            raise ValueError("native notification budget binding requires goal_id, budget_period_key, and budget_limit")
        owner_principal_id = _validate_identifier(owner_principal_id, field="owner_principal_id")
        operator_session_id = _validate_identifier(operator_session_id, field="operator_session_id")
        device_id = _validate_identifier(device_id, field="device_id")
        session_id = _validate_identifier(session_id, field="session_id")
        conversation_id = _validate_identifier(conversation_id, field="conversation_id")
        thread_id = _validate_identifier(thread_id, field="thread_id")
        channel = _validate_text(channel, field="channel", max_chars=MAX_IDENTIFIER_CHARS, required=True)
        transport = _validate_text(transport, field="transport", max_chars=MAX_IDENTIFIER_CHARS, required=True)
        correlation_id = _validate_identifier(correlation_id, field="correlation_id")
        causation_id = _validate_identifier(causation_id, field="causation_id")
        intervention_type = _validate_text(
            intervention_type,
            field="intervention_type",
            max_chars=MAX_IDENTIFIER_CHARS,
        )
        surface_value = _validate_text(surface, field="surface", max_chars=MAX_IDENTIFIER_CHARS, required=True)
        thread_source_value = _validate_text(
            thread_source,
            field="thread_source",
            max_chars=MAX_IDENTIFIER_CHARS,
            required=True,
        )
        continuation_mode_value = _validate_text(
            continuation_mode,
            field="continuation_mode",
            max_chars=MAX_IDENTIFIER_CHARS,
            required=True,
        )
        title_value = _validate_text(title, field="title", max_chars=MAX_TITLE_CHARS, required=True)
        body_value = _validate_text(body, field="body", max_chars=MAX_BODY_CHARS, required=True)
        resume_value = _validate_text(
            resume_message,
            field="resume_message",
            max_chars=MAX_RESUME_MESSAGE_CHARS,
        )
        assert title_value is not None and body_value is not None

        if channel != "native_notification":
            raise ConversationIdentityError(
                "conversation_channel_mismatch",
                "The native notification outbox only accepts native_notification channel receipts.",
            )

        if conversation_id is not None:
            if session_id is not None and conversation_id != session_id:
                raise ConversationIdentityError(
                    "conversation_session_mismatch",
                    "Conversation identity must equal the session id.",
                )
            session_id = conversation_id
        canonical_thread_id = thread_id or session_id
        if session_id is not None and canonical_thread_id != session_id:
            raise ConversationIdentityError(
                "conversation_thread_mismatch",
                "Thread identity must equal the canonical session id.",
            )
        owner_principal_id, operator_session_id = _bind_runtime_identity(
            session_id=session_id,
            owner_principal_id=owner_principal_id,
            operator_session_id=operator_session_id,
        )
        if session_id is not None and owner_principal_id is None:
            raise ConversationIdentityError(
                "conversation_owner_missing",
                "A session-bound notification requires an owner principal.",
            )
        if operator_session_id is not None and owner_principal_id is None:
            raise ConversationIdentityError(
                "conversation_owner_missing",
                "An operator session cannot be persisted without its owner principal.",
            )
        try:
            safe_attachment_refs = validate_attachment_refs(
                attachment_refs,
                owner_principal_id=owner_principal_id,
            )
        except ConversationIdentityError:
            raise
        except Exception as exc:
            raise ConversationIdentityError(
                "attachment_reference_invalid",
                "Attachment references could not be persisted safely.",
            ) from exc
        # The identity helper validates the channel/transport registry and
        # supplies deterministic ambient defaults. Ambient delivery remains
        # ownerless only when it has no conversation/session binding.
        identity = build_conversation_identity(
            conversation_id=session_id,
            thread_id=canonical_thread_id,
            owner_principal_id=owner_principal_id or "ambient",
            operator_session_id=operator_session_id,
            device_id=device_id,
            channel=channel,
            transport=transport,
            correlation_id=correlation_id,
            causation_id=causation_id,
            require_owner=session_id is not None,
        )

        if urgency is not None and (not isinstance(urgency, int) or urgency < 0 or urgency > 5):
            raise ValueError("native notification urgency must be an integer between 0 and 5")

        supplied_key = _validate_text(
            idempotency_key,
            field="idempotency_key",
            max_chars=MAX_IDEMPOTENCY_KEY_CHARS,
        )
        key = supplied_key or (
            f"native_notification:{intervention_id}" if intervention_id else f"native_notification:{uuid4().hex}"
        )
        payload = {
            "intervention_id": intervention_id,
            "owner_principal_id": owner_principal_id,
            "operator_session_id": operator_session_id,
            "device_id": identity.device_id,
            "channel": identity.channel,
            "transport": identity.transport,
            "title": title_value,
            "body": body_value,
            "intervention_type": intervention_type,
            "urgency": urgency,
            "surface": surface_value,
            "session_id": session_id,
            "conversation_id": identity.conversation_id or None,
            "thread_id": identity.thread_id or None,
            "thread_source": thread_source_value,
            "continuation_mode": continuation_mode_value,
            "resume_message": resume_value,
            "correlation_id": identity.correlation_id,
            "causation_id": identity.causation_id,
            "attachment_refs": safe_attachment_refs,
            "goal_id": goal_id,
            "budget_period_key": budget_period_key,
            "budget_limit": budget_limit,
        }
        digest = _payload_digest(payload)
        now = _utc_now()

        source_ids: list[str] = []
        for source_id in source_insight_ids or []:
            validated_id = _validate_identifier(source_id, field="source_insight_id")
            if validated_id is None:
                raise ValueError("native notification source_insight_id is required")
            source_ids.append(validated_id)
        if len(source_ids) > 100:
            raise ValueError("native notification source_insight_ids exceeds 100 items")

        async def delete_source_rows(db) -> None:
            if source_ids:
                try:
                    await db.execute(delete(QueuedInsight).where(QueuedInsight.id.in_(source_ids)))
                except OperationalError as exc:
                    # Test/operator callers can use the outbox before the
                    # full insight schema exists. Production startup creates
                    # this table; never hide any other database failure.
                    if "no such table" not in str(exc).lower():
                        raise

        async with self._lock:
            async with self._session() as db:
                # A goal notification budget is a durable reservation, not an
                # advisory count. SQLite's immediate transaction serializes
                # distinct idempotency keys across queue instances/processes;
                # the normal outer session commit persists the reservation.
                if budget_limit is not None:
                    if db.in_transaction():
                        await db.commit()
                    bind = db.get_bind()
                    dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
                    if dialect_name == "sqlite":
                        await db.execute(text("BEGIN IMMEDIATE"))
                if session_id is not None:
                    session_result = await db.execute(
                        select(Session).where(Session.id == session_id)
                    )
                    session_row = session_result.scalar_one_or_none()
                    if session_row is None:
                        raise ConversationIdentityError(
                            "conversation_session_not_found",
                            "The canonical conversation session was not found.",
                        )
                    if session_row.owner_principal_id not in (None, owner_principal_id):
                        raise ConversationIdentityError(
                            "conversation_owner_mismatch",
                            "The canonical conversation belongs to another operator.",
                        )
                    if session_row.owner_principal_id is None:
                        session_row.owner_principal_id = owner_principal_id
                        db.add(session_row)
                        await db.flush()
                existing_result = await db.execute(
                    select(NativeNotificationOutbox).where(
                        NativeNotificationOutbox.idempotency_key == key
                    )
                )
                existing = existing_result.scalar_one_or_none()
                if existing is not None:
                    if existing.payload_digest != digest:
                        raise NativeNotificationConflictError(
                            "native notification idempotency key is bound to another payload"
                        )
                    # Bundle retries reuse the same key. Deleting source rows
                    # in this transaction makes a crash between enqueue and
                    # cleanup safe and idempotent.
                    await delete_source_rows(db)
                    return _row_to_notification(existing)

                if budget_limit is not None:
                    used_result = await db.execute(
                        select(func.count(NativeNotificationOutbox.id)).where(
                            NativeNotificationOutbox.goal_id == goal_id,
                            NativeNotificationOutbox.budget_period_key == budget_period_key,
                        )
                    )
                    if int(used_result.scalar_one() or 0) >= budget_limit:
                        raise NativeNotificationBudgetDenied(
                            goal_id=str(goal_id),
                            budget_period_key=str(budget_period_key),
                            budget_limit=budget_limit,
                        )

                # SQLite's conflict-aware insert is the cross-process CAS.
                # It avoids a SELECT-then-INSERT uniqueness exception and
                # lets a losing writer read the committed canonical row.
                await db.execute(
                    sqlite_insert(NativeNotificationOutbox)
                    .values(
                        id=uuid4().hex,
                        idempotency_key=key,
                        payload_digest=digest,
                        intervention_id=intervention_id,
                        goal_id=goal_id,
                        budget_period_key=budget_period_key,
                        budget_limit=budget_limit,
                        owner_principal_id=owner_principal_id,
                        operator_session_id=operator_session_id,
                        device_id=identity.device_id,
                        channel=identity.channel,
                        transport=identity.transport,
                        title=title_value,
                        body=body_value,
                        intervention_type=intervention_type,
                        urgency=urgency,
                        surface=surface_value,
                        session_id=session_id,
                        conversation_id=identity.conversation_id or None,
                        thread_id=identity.thread_id or None,
                        thread_source=thread_source_value,
                        continuation_mode=continuation_mode_value,
                        resume_message=resume_value,
                        correlation_id=identity.correlation_id,
                        causation_id=identity.causation_id,
                        attachment_refs_json=json.dumps(safe_attachment_refs, sort_keys=True),
                        status="queued",
                        max_attempts=self.max_attempts,
                        deadline_at=now + timedelta(seconds=self.ttl_seconds),
                        created_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(index_elements=["idempotency_key"])
                )
                existing_result = await db.execute(
                    select(NativeNotificationOutbox).where(
                        NativeNotificationOutbox.idempotency_key == key
                    )
                )
                row = existing_result.scalar_one_or_none()
                if row is None:
                    raise RuntimeError("native notification idempotency insert produced no row")
                if row.payload_digest != digest:
                    raise NativeNotificationConflictError(
                        "native notification idempotency key is bound to another payload"
                    )
                await delete_source_rows(db)
                return _row_to_notification(row)

    async def _finish_attempt(
        self,
        db,
        row: NativeNotificationOutbox,
        *,
        status: str,
        now: datetime,
        error_code: str | None = None,
    ) -> None:
        result = await db.execute(
            select(NativeNotificationDeliveryAttempt)
            .where(
                NativeNotificationDeliveryAttempt.notification_id == row.id,
                NativeNotificationDeliveryAttempt.attempt_index == row.attempt_count,
            )
            .limit(1)
        )
        attempt = result.scalar_one_or_none()
        if attempt is None:
            return
        attempt.status = status
        attempt.error_code = _safe_reason(error_code) if error_code else None
        attempt.finished_at = now

    async def _reconcile_expired(self, db, now: datetime) -> None:
        """Recover leases and expire queued work before every read/transition."""
        result = await db.execute(
            select(NativeNotificationOutbox).where(NativeNotificationOutbox.status.in_(CLAIMED_STATUSES))
        )
        for row in result.scalars().all():
            lease_expires_at = _aware(row.lease_expires_at)
            if lease_expires_at is not None and lease_expires_at > now:
                continue
            reason = "claimed_lease_missing" if lease_expires_at is None else "lease_expired_reconciliation_required"
            predicates = [
                NativeNotificationOutbox.id == row.id,
                NativeNotificationOutbox.status.in_(CLAIMED_STATUSES),
                NativeNotificationOutbox.fencing_token == row.fencing_token,
            ]
            if lease_expires_at is None:
                predicates.append(NativeNotificationOutbox.lease_expires_at.is_(None))
            else:
                predicates.append(NativeNotificationOutbox.lease_expires_at <= now)
            # CAS on both status and fence prevents a stale reconciler from
            # overwriting a concurrent ACK, dismiss, or claim transition.
            transition = await db.execute(
                update(NativeNotificationOutbox)
                .execution_options(synchronize_session=False)
                .where(*predicates)
                .values(
                    status="unknown",
                    last_error=reason,
                    degraded_state="delivery_unknown",
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
            if transition.rowcount != 1:
                continue
            await db.refresh(row)
            await self._finish_attempt(db, row, status="unknown", now=now, error_code=reason)

        # Quarantine receipts are short lived. A worker may have persisted a
        # valid reference and then remained offline until its proof expired;
        # make that intent terminal before claim/read can expose it.
        attachment_result = await db.execute(
            select(NativeNotificationOutbox).where(
                NativeNotificationOutbox.status.in_(ACTIVE_STATUSES),
            )
        )
        for row in attachment_result.scalars().all():
            try:
                _attachment_refs_from_json(
                    row.attachment_refs_json,
                    owner_principal_id=row.owner_principal_id,
                    raise_on_error=True,
                )
            except ConversationIdentityError as exc:
                reason = exc.code if exc.code == "attachment_receipt_expired" else "attachment_reference_invalid"
                transition = await db.execute(
                    update(NativeNotificationOutbox)
                    .execution_options(synchronize_session=False)
                    .where(
                        NativeNotificationOutbox.id == row.id,
                        NativeNotificationOutbox.status.in_(ACTIVE_STATUSES),
                        NativeNotificationOutbox.fencing_token == row.fencing_token,
                    )
                    .values(
                        status="cancelled",
                        cancelled_at=now,
                        last_error=reason,
                        degraded_state=reason,
                        lease_owner=None,
                        lease_expires_at=None,
                        attachment_refs_json="[]",
                        updated_at=now,
                    )
                )
                if transition.rowcount != 1:
                    continue
                await db.refresh(row)
                await self._finish_attempt(db, row, status="cancelled", now=now, error_code=reason)

        # A notification bound to an operator/session is an authorized intent,
        # not an ambient broadcast. Re-check both bindings on every queue
        # interaction so a restart or a worker that was already polling cannot
        # dispatch after the owner was revoked or the conversation changed.
        bound_result = await db.execute(
            select(NativeNotificationOutbox).where(
                NativeNotificationOutbox.status.in_(ACTIVE_STATUSES),
                or_(
                    NativeNotificationOutbox.session_id.is_not(None),
                    NativeNotificationOutbox.owner_principal_id.is_not(None),
                    NativeNotificationOutbox.operator_session_id.is_not(None),
                ),
            )
        )
        for row in bound_result.scalars().all():
            reason: str | None = None
            if row.session_id:
                if not row.owner_principal_id:
                    reason = "conversation_owner_missing"
                else:
                    session_result = await db.execute(
                        select(Session).where(Session.id == row.session_id)
                    )
                    session = session_result.scalar_one_or_none()
                    if session is None:
                        reason = "conversation_session_missing"
                    elif session.owner_principal_id not in (None, row.owner_principal_id):
                        reason = "conversation_owner_revoked"
            if reason is None and row.operator_session_id:
                operator_result = await db.execute(
                    select(OperatorSession).where(OperatorSession.id == row.operator_session_id)
                )
                operator_session = operator_result.scalar_one_or_none()
                if operator_session is None:
                    reason = "operator_session_missing"
                elif operator_session.revoked_at is not None:
                    reason = "operator_session_revoked"
                elif (
                    (_aware(operator_session.idle_expires_at) or now) <= now
                    or (_aware(operator_session.absolute_expires_at) or now) <= now
                ):
                    reason = "operator_session_expired"
            if reason is None:
                continue
            transition = await db.execute(
                update(NativeNotificationOutbox)
                .execution_options(synchronize_session=False)
                .where(
                    NativeNotificationOutbox.id == row.id,
                    NativeNotificationOutbox.status.in_(ACTIVE_STATUSES),
                    NativeNotificationOutbox.fencing_token == row.fencing_token,
                )
                .values(
                    status="cancelled",
                    cancelled_at=now,
                    last_error=reason,
                    degraded_state=reason,
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
            if transition.rowcount != 1:
                continue
            await db.refresh(row)
            await self._finish_attempt(db, row, status="cancelled", now=now, error_code=reason)

        await db.execute(
            update(NativeNotificationOutbox)
            .execution_options(synchronize_session=False)
            .where(
                NativeNotificationOutbox.status == "queued",
                NativeNotificationOutbox.deadline_at <= now,
            )
            .values(
                status="failed",
                last_error="deadline_expired",
                degraded_state="delivery_deadline_expired",
                updated_at=now,
            )
        )

        await db.execute(
            update(NativeNotificationOutbox)
            .execution_options(synchronize_session=False)
            .where(
                NativeNotificationOutbox.status == "queued",
                (
                    (NativeNotificationOutbox.deadline_at.is_(None))
                    | (NativeNotificationOutbox.max_attempts < 1)
                    | (NativeNotificationOutbox.attempt_count < 0)
                    | (NativeNotificationOutbox.fencing_token < 0)
                ),
            )
            .values(
                status="failed",
                last_error="malformed_outbox_state",
                degraded_state="delivery_state_invalid",
                updated_at=now,
            )
        )

    async def claim_next(
        self,
        *,
        worker_id: str = "native-daemon",
        lease_seconds: int | None = None,
    ) -> NativeNotification | None:
        """Atomically claim the oldest pending notification for one worker."""
        worker = _valid_worker(worker_id)
        lease_duration = self.lease_seconds if lease_seconds is None else lease_seconds
        if lease_duration < 1 or lease_duration > 300:
            raise ValueError("lease_seconds must be between 1 and 300")

        async with self._lock:
            async with self._session() as db:
                now = _utc_now()
                await self._reconcile_expired(db, now)
                # Returning an unexpired claim to the same worker preserves
                # the old poll/ack behaviour and avoids duplicate display.
                result = await db.execute(
                    select(NativeNotificationOutbox)
                    .where(
                        NativeNotificationOutbox.status.in_(ACTIVE_STATUSES),
                        NativeNotificationOutbox.deadline_at > now,
                        (
                            (NativeNotificationOutbox.status == "queued")
                            | (NativeNotificationOutbox.lease_owner == worker)
                        ),
                    )
                    .order_by(
                        NativeNotificationOutbox.urgency.desc().nullslast(),
                        NativeNotificationOutbox.created_at.asc(),
                        NativeNotificationOutbox.id.asc(),
                    )
                    .limit(1)
                )
                row = result.scalar_one_or_none()
                if row is None:
                    return None
                if row.status in CLAIMED_STATUSES:
                    return _row_to_notification(row)

                lease_expires = min(
                    now + timedelta(seconds=lease_duration),
                    _aware(row.deadline_at) or now,
                )
                # The conditional update is the cross-process fence. The
                # in-process lock only reduces local contention; rowcount is
                # authoritative when another worker races the same claim.
                claim_result = await db.execute(
                    update(NativeNotificationOutbox)
                    .execution_options(synchronize_session=False)
                    .where(
                        NativeNotificationOutbox.id == row.id,
                        NativeNotificationOutbox.status == "queued",
                        NativeNotificationOutbox.deadline_at > now,
                    )
                    .values(
                        status="claimed",
                        attempt_count=NativeNotificationOutbox.attempt_count + 1,
                        lease_owner=worker,
                        lease_expires_at=lease_expires,
                        fencing_token=NativeNotificationOutbox.fencing_token + 1,
                        updated_at=now,
                    )
                )
                if claim_result.rowcount != 1:
                    return None
                await db.refresh(row)
                db.add(
                    NativeNotificationDeliveryAttempt(
                        notification_id=row.id,
                        attempt_index=row.attempt_count,
                        lease_owner=worker,
                        fencing_token=row.fencing_token,
                        status="claimed",
                        started_at=now,
                    )
                )
                await db.flush()
                return _row_to_notification(row)

    async def peek(self) -> NativeNotification | None:
        """Return the next item while claiming it for an internal caller.

        The HTTP daemon path must provide its own unique worker identity.
        """
        return await self.claim_next(worker_id="native-daemon-internal")

    async def get(
        self,
        notification_id: str,
        *,
        owner_principal_id: str | None = None,
        operator_session_id: str | None = None,
    ) -> NativeNotification | None:
        notification_id = _validate_identifier(notification_id, field="notification_id")
        if not notification_id:
            return None
        async with self._lock:
            async with self._session() as db:
                await self._reconcile_expired(db, _utc_now())
                stmt = select(NativeNotificationOutbox).where(
                    NativeNotificationOutbox.id == notification_id
                )
                if owner_principal_id is not None:
                    stmt = stmt.where(
                        _owner_scope_predicate(
                            owner_principal_id=owner_principal_id,
                            operator_session_id=operator_session_id,
                        )
                    )
                result = await db.execute(stmt)
                row = result.scalar_one_or_none()
                return _row_to_notification(row) if row is not None else None

    async def list(
        self,
        *,
        owner_principal_id: str | None = None,
        operator_session_id: str | None = None,
    ) -> list[NativeNotification]:
        async with self._lock:
            async with self._session() as db:
                now = _utc_now()
                await self._reconcile_expired(db, now)
                stmt = (
                    select(NativeNotificationOutbox)
                    .where(NativeNotificationOutbox.status.in_(ACTIVE_STATUSES))
                )
                if owner_principal_id is not None:
                    stmt = stmt.where(
                        _owner_scope_predicate(
                            owner_principal_id=owner_principal_id,
                            operator_session_id=operator_session_id,
                        )
                    )
                result = await db.execute(
                    stmt.order_by(
                        NativeNotificationOutbox.created_at.asc(),
                        NativeNotificationOutbox.id.asc(),
                    )
                )
                return [_row_to_notification(row) for row in result.scalars().all()]

    async def ack(
        self,
        notification_id: str,
        *,
        worker_id: str | None = None,
        fencing_token: int | None = None,
    ) -> bool:
        """Record a successful daemon acknowledgement under the current fence."""
        notification_id = _validate_identifier(notification_id, field="notification_id")
        if not notification_id:
            return False
        worker = _valid_worker(worker_id) if worker_id is not None else None
        if fencing_token is not None and (not isinstance(fencing_token, int) or fencing_token < 1):
            raise NativeNotificationLeaseError("fencing_token must be a positive integer")
        # There is no browser/operator bypass. A receipt is accepted only
        # from the daemon that owns the current lease and fence.
        if worker is None or fencing_token is None:
            return False

        async with self._lock:
            async with self._session() as db:
                now = _utc_now()
                await self._reconcile_expired(db, now)
                result = await db.execute(
                    select(NativeNotificationOutbox).where(
                        NativeNotificationOutbox.id == notification_id
                    )
                )
                row = result.scalar_one_or_none()
                if row is None or row.status in TERMINAL_STATUSES:
                    return False
                if row.status in CLAIMED_STATUSES:
                    if row.status != "display_attempted":
                        # The daemon must first record the fenced OS handoff;
                        # a claim alone is not evidence that a display call
                        # was attempted.
                        return False
                    if row.lease_expires_at is None or (_aware(row.lease_expires_at) or now) <= now:
                        return False
                    if fencing_token is None or row.fencing_token != fencing_token:
                        return False
                    if row.lease_owner != worker:
                        return False
                    predicates = [
                        NativeNotificationOutbox.id == notification_id,
                        NativeNotificationOutbox.status.in_(CLAIMED_STATUSES),
                        NativeNotificationOutbox.fencing_token == fencing_token,
                        NativeNotificationOutbox.lease_expires_at > now,
                        NativeNotificationOutbox.lease_owner == worker,
                    ]
                    ack_result = await db.execute(
                        update(NativeNotificationOutbox)
                        .execution_options(synchronize_session=False)
                        .where(*predicates)
                        .values(
                            status="delivered",
                            lease_owner=None,
                            lease_expires_at=None,
                            delivered_at=now,
                            updated_at=now,
                        )
                    )
                    if ack_result.rowcount != 1:
                        return False
                    await db.refresh(row)
                else:
                    return False
                await self._finish_attempt(db, row, status="delivered", now=now)
                await db.flush()
                return True

    async def fail(
        self,
        notification_id: str,
        *,
        reason: str = "display_failed",
        worker_id: str | None = None,
        fencing_token: int | None = None,
    ) -> bool:
        """Record an ambiguous daemon failure for explicit reconciliation.

        The OS notification API does not provide a durable receipt. A failed
        call may therefore have displayed, so automatic retry is unsafe.
        """
        notification_id = _validate_identifier(notification_id, field="notification_id")
        if not notification_id:
            return False
        worker = _valid_worker(worker_id) if worker_id is not None else None
        if fencing_token is not None and (not isinstance(fencing_token, int) or fencing_token < 1):
            raise NativeNotificationLeaseError("fencing_token must be a positive integer")
        if worker is None or fencing_token is None:
            return False

        async with self._lock:
            async with self._session() as db:
                now = _utc_now()
                await self._reconcile_expired(db, now)
                result = await db.execute(
                    select(NativeNotificationOutbox).where(
                        NativeNotificationOutbox.id == notification_id
                    )
                )
                row = result.scalar_one_or_none()
                if row is None or row.status in {"delivered", "cancelled", "unknown"}:
                    return False
                if row.status not in CLAIMED_STATUSES:
                    return False
                if row.lease_expires_at is None or (_aware(row.lease_expires_at) or now) <= now:
                    return False
                if fencing_token is None or row.fencing_token != fencing_token:
                    return False
                if row.lease_owner != worker:
                    return False

                safe_reason = _safe_reason(reason)
                predicates = [
                    NativeNotificationOutbox.id == notification_id,
                    NativeNotificationOutbox.status.in_(CLAIMED_STATUSES),
                    NativeNotificationOutbox.fencing_token == fencing_token,
                    NativeNotificationOutbox.lease_expires_at > now,
                    NativeNotificationOutbox.lease_owner == worker,
                ]
                fail_result = await db.execute(
                    update(NativeNotificationOutbox)
                    .execution_options(synchronize_session=False)
                    .where(*predicates)
                    .values(
                        status="unknown",
                        last_error=safe_reason,
                        degraded_state="delivery_unknown",
                        lease_owner=None,
                        lease_expires_at=None,
                        updated_at=now,
                    )
                )
                if fail_result.rowcount != 1:
                    return False
                await db.refresh(row)
                await self._finish_attempt(
                    db,
                    row,
                    status="unknown",
                    now=now,
                    error_code=safe_reason,
                )
                await db.flush()
                return True

    async def mark_display_attempted(
        self,
        notification_id: str,
        *,
        worker_id: str,
        fencing_token: int,
    ) -> bool:
        """Fence the handoff immediately before invoking the OS display call."""
        notification_id = _validate_identifier(notification_id, field="notification_id")
        worker = _valid_worker(worker_id)
        if not notification_id or not isinstance(fencing_token, int) or fencing_token < 1:
            return False
        async with self._lock:
            async with self._session() as db:
                now = _utc_now()
                await self._reconcile_expired(db, now)
                row_result = await db.execute(
                    select(NativeNotificationOutbox).where(
                        NativeNotificationOutbox.id == notification_id
                    )
                )
                row = row_result.scalar_one_or_none()
                if row is None or row.attempt_count < 1:
                    return False
                attempt_result = await db.execute(
                    select(NativeNotificationDeliveryAttempt).where(
                        NativeNotificationDeliveryAttempt.notification_id == notification_id,
                        NativeNotificationDeliveryAttempt.attempt_index == row.attempt_count,
                    )
                )
                attempt = attempt_result.scalar_one_or_none()
                if attempt is None:
                    # A claim persisted without its attempt receipt is an
                    # incomplete handoff and must not reach the OS.
                    return False
                result = await db.execute(
                    update(NativeNotificationOutbox)
                    .execution_options(synchronize_session=False)
                    .where(
                        NativeNotificationOutbox.id == notification_id,
                        NativeNotificationOutbox.status == "claimed",
                        NativeNotificationOutbox.lease_owner == worker,
                        NativeNotificationOutbox.fencing_token == fencing_token,
                        NativeNotificationOutbox.lease_expires_at > now,
                    )
                    .values(status="display_attempted", updated_at=now)
                )
                if result.rowcount != 1:
                    return False
                attempt.status = "display_attempted"
                await db.flush()
                return True

    async def retry(self, notification_id: str) -> bool:
        """Explicitly requeue one failed notification inside its retry budget."""
        notification_id = _validate_identifier(notification_id, field="notification_id")
        if not notification_id:
            return False
        async with self._lock:
            async with self._session() as db:
                now = _utc_now()
                await self._reconcile_expired(db, now)
                result = await db.execute(
                    select(NativeNotificationOutbox).where(
                        NativeNotificationOutbox.id == notification_id
                    )
                )
                row = result.scalar_one_or_none()
                if (
                    row is None
                    or row.status != "failed"
                    or row.attempt_count >= row.max_attempts
                    or (_aware(row.deadline_at) or now) <= now
                ):
                    return False
                row.status = "queued"
                row.last_error = None
                row.degraded_state = None
                row.updated_at = now
                await db.flush()
                return True

    async def reconcile_unknown(
        self,
        notification_id: str,
        *,
        retry: bool = False,
        owner_principal_id: str | None = None,
        operator_session_id: str | None = None,
    ) -> bool:
        """Explicitly requeue an ambiguous receipt after operator review.

        This transition is opt-in and remains bounded by the original
        deadline/attempt budget. It is intentionally separate from daemon
        polling so restart or lease expiry can never replay silently.
        """
        notification_id = _validate_identifier(notification_id, field="notification_id")
        if not notification_id or not retry:
            return False
        async with self._lock:
            async with self._session() as db:
                now = _utc_now()
                await self._reconcile_expired(db, now)
                predicates = [
                    NativeNotificationOutbox.id == notification_id,
                    NativeNotificationOutbox.status == "unknown",
                    NativeNotificationOutbox.attempt_count < NativeNotificationOutbox.max_attempts,
                    NativeNotificationOutbox.deadline_at > now,
                ]
                if owner_principal_id is not None:
                    predicates.append(
                        _owner_scope_predicate(
                            owner_principal_id=owner_principal_id,
                            operator_session_id=operator_session_id,
                        )
                    )
                result = await db.execute(
                    update(NativeNotificationOutbox)
                    .execution_options(synchronize_session=False)
                    .where(*predicates)
                    .values(
                        status="queued",
                        last_error="operator_reconciled_retry",
                        degraded_state=None,
                        lease_owner=None,
                        lease_expires_at=None,
                        updated_at=now,
                    )
                )
                await db.flush()
                return result.rowcount == 1

    async def dismiss(
        self,
        notification_id: str,
        *,
        owner_principal_id: str | None = None,
        operator_session_id: str | None = None,
    ) -> NativeNotification | None:
        """Cancel a pending notification and retain its audit receipt."""
        notification_id = _validate_identifier(notification_id, field="notification_id")
        if not notification_id:
            return None
        async with self._lock:
            async with self._session() as db:
                now = _utc_now()
                await self._reconcile_expired(db, now)
                stmt = select(NativeNotificationOutbox).where(
                    NativeNotificationOutbox.id == notification_id
                )
                if owner_principal_id is not None:
                    stmt = stmt.where(
                        _owner_scope_predicate(
                            owner_principal_id=owner_principal_id,
                            operator_session_id=operator_session_id,
                        )
                    )
                result = await db.execute(stmt)
                row = result.scalar_one_or_none()
                if row is None or row.status not in ACTIVE_STATUSES:
                    return None
                if row.status in CLAIMED_STATUSES:
                    # Browser dismissal has no daemon fence. It can stop a
                    # future ACK, but must preserve ambiguity for recovery.
                    dismiss_predicates = [
                        NativeNotificationOutbox.id == notification_id,
                        NativeNotificationOutbox.status.in_(CLAIMED_STATUSES),
                        NativeNotificationOutbox.fencing_token == row.fencing_token,
                    ]
                    if owner_principal_id is not None:
                        dismiss_predicates.append(
                            _owner_scope_predicate(
                                owner_principal_id=owner_principal_id,
                                operator_session_id=operator_session_id,
                            )
                        )
                    dismiss_result = await db.execute(
                        update(NativeNotificationOutbox)
                        .execution_options(synchronize_session=False)
                        .where(*dismiss_predicates)
                        .values(
                            status="unknown",
                            last_error="operator_dismissed_reconciliation_required",
                            lease_owner=None,
                            lease_expires_at=None,
                            updated_at=now,
                        )
                    )
                    if dismiss_result.rowcount != 1:
                        return None
                    await db.refresh(row)
                    await self._finish_attempt(
                        db,
                        row,
                        status="unknown",
                        now=now,
                        error_code="operator_dismissed_reconciliation_required",
                    )
                else:
                    dismiss_predicates = [
                        NativeNotificationOutbox.id == notification_id,
                        NativeNotificationOutbox.status == "queued",
                        NativeNotificationOutbox.attempt_count == row.attempt_count,
                        NativeNotificationOutbox.fencing_token == row.fencing_token,
                    ]
                    if owner_principal_id is not None:
                        dismiss_predicates.append(
                            _owner_scope_predicate(
                                owner_principal_id=owner_principal_id,
                                operator_session_id=operator_session_id,
                            )
                        )
                    dismiss_result = await db.execute(
                        update(NativeNotificationOutbox)
                        .execution_options(synchronize_session=False)
                        .where(*dismiss_predicates)
                        .values(status="cancelled", cancelled_at=now, updated_at=now)
                    )
                    if dismiss_result.rowcount != 1:
                        return None
                    await db.refresh(row)
                    await self._finish_attempt(
                        db,
                        row,
                        status="cancelled",
                        now=now,
                        error_code="operator_dismissed",
                    )
                await db.flush()
                return _row_to_notification(row)

    async def dismiss_all(
        self,
        *,
        owner_principal_id: str | None = None,
        operator_session_id: str | None = None,
    ) -> list[NativeNotification]:
        """Cancel all pending notifications while preserving their receipts."""
        async with self._lock:
            async with self._session() as db:
                now = _utc_now()
                await self._reconcile_expired(db, now)
                stmt = select(NativeNotificationOutbox).where(
                    NativeNotificationOutbox.status.in_(ACTIVE_STATUSES)
                )
                if owner_principal_id is not None:
                    stmt = stmt.where(
                        _owner_scope_predicate(
                            owner_principal_id=owner_principal_id,
                            operator_session_id=operator_session_id,
                        )
                    )
                result = await db.execute(
                    stmt.order_by(
                        NativeNotificationOutbox.created_at.asc(),
                        NativeNotificationOutbox.id.asc(),
                    )
                )
                rows = list(result.scalars().all())
                changed: list[NativeNotification] = []
                for row in rows:
                    if row.status in CLAIMED_STATUSES:
                        dismiss_predicates = [
                            NativeNotificationOutbox.id == row.id,
                            NativeNotificationOutbox.status.in_(CLAIMED_STATUSES),
                            NativeNotificationOutbox.fencing_token == row.fencing_token,
                        ]
                        if owner_principal_id is not None:
                            dismiss_predicates.append(
                                _owner_scope_predicate(
                                    owner_principal_id=owner_principal_id,
                                    operator_session_id=operator_session_id,
                                )
                            )
                        dismiss_result = await db.execute(
                            update(NativeNotificationOutbox)
                            .execution_options(synchronize_session=False)
                            .where(*dismiss_predicates)
                            .values(
                                status="unknown",
                                last_error="operator_dismissed_reconciliation_required",
                                lease_owner=None,
                                lease_expires_at=None,
                                updated_at=now,
                            )
                        )
                        if dismiss_result.rowcount != 1:
                            continue
                        await db.refresh(row)
                        await self._finish_attempt(
                            db,
                            row,
                            status="unknown",
                            now=now,
                            error_code="operator_dismissed_reconciliation_required",
                        )
                    else:
                        dismiss_predicates = [
                            NativeNotificationOutbox.id == row.id,
                            NativeNotificationOutbox.status == "queued",
                            NativeNotificationOutbox.attempt_count == row.attempt_count,
                            NativeNotificationOutbox.fencing_token == row.fencing_token,
                        ]
                        if owner_principal_id is not None:
                            dismiss_predicates.append(
                                _owner_scope_predicate(
                                    owner_principal_id=owner_principal_id,
                                    operator_session_id=operator_session_id,
                                )
                            )
                        dismiss_result = await db.execute(
                            update(NativeNotificationOutbox)
                            .execution_options(synchronize_session=False)
                            .where(*dismiss_predicates)
                            .values(status="cancelled", cancelled_at=now, updated_at=now)
                        )
                        if dismiss_result.rowcount != 1:
                            continue
                        await db.refresh(row)
                        await self._finish_attempt(
                            db,
                            row,
                            status="cancelled",
                            now=now,
                            error_code="operator_dismissed",
                        )
                    changed.append(_row_to_notification(row))
                await db.flush()
                return changed

    async def get_attempts(self, notification_id: str) -> list[dict[str, Any]]:
        """Return bounded delivery-attempt metadata for operator/tests."""
        notification_id = _validate_identifier(notification_id, field="notification_id")
        if not notification_id:
            return []
        async with self._lock:
            async with self._session() as db:
                result = await db.execute(
                    select(NativeNotificationDeliveryAttempt)
                    .where(NativeNotificationDeliveryAttempt.notification_id == notification_id)
                    .order_by(NativeNotificationDeliveryAttempt.attempt_index.asc())
                )
                return [
                    {
                        "id": row.id,
                        "notification_id": row.notification_id,
                        "attempt_index": row.attempt_index,
                        "lease_owner": row.lease_owner,
                        "fencing_token": row.fencing_token,
                        "status": row.status,
                        "error_code": row.error_code,
                        "started_at": (_aware(row.started_at) or _utc_now()).isoformat(),
                        "finished_at": (
                            (_aware(row.finished_at) or _utc_now()).isoformat()
                            if row.finished_at is not None
                            else None
                        ),
                    }
                    for row in result.scalars().all()
                ]

    async def recovery(
        self,
        *,
        limit: int = 100,
        owner_principal_id: str | None = None,
        operator_session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return bounded failed/unknown rows and operator recovery state."""
        if limit < 1 or limit > 100:
            raise ValueError("recovery limit must be between 1 and 100")
        async with self._lock:
            async with self._session() as db:
                await self._reconcile_expired(db, _utc_now())
                stmt = select(NativeNotificationOutbox).where(
                    NativeNotificationOutbox.status.in_({"failed", "unknown", "cancelled"}),
                    (
                        NativeNotificationOutbox.status.in_({"failed", "unknown"})
                        | NativeNotificationOutbox.degraded_state.is_not(None)
                    ),
                )
                if owner_principal_id is not None:
                    stmt = stmt.where(
                        _owner_scope_predicate(
                            owner_principal_id=owner_principal_id,
                            operator_session_id=operator_session_id,
                        )
                    )
                result = await db.execute(
                    stmt.order_by(
                        NativeNotificationOutbox.updated_at.desc(),
                        NativeNotificationOutbox.id.asc(),
                    ).limit(limit)
                )
                rows = list(result.scalars().all())
                output: list[dict[str, Any]] = []
                for row in rows:
                    attempt_result = await db.execute(
                        select(NativeNotificationDeliveryAttempt)
                        .where(NativeNotificationDeliveryAttempt.notification_id == row.id)
                        .order_by(NativeNotificationDeliveryAttempt.attempt_index.asc())
                    )
                    output.append(
                        {
                            "notification": _row_to_notification(row).to_dict(),
                            "last_error": row.last_error,
                            "recovery_required": row.status in {"unknown", "cancelled"},
                            "attempts": [
                                {
                                    "attempt_index": attempt.attempt_index,
                                    "status": attempt.status,
                                    "error_code": attempt.error_code,
                                    "fencing_token": attempt.fencing_token,
                                    "lease_owner": attempt.lease_owner,
                                }
                                for attempt in attempt_result.scalars().all()
                            ],
                        }
                    )
                return output

    async def count(
        self,
        *,
        owner_principal_id: str | None = None,
        operator_session_id: str | None = None,
    ) -> int:
        async with self._lock:
            async with self._session() as db:
                now = _utc_now()
                await self._reconcile_expired(db, now)
                stmt = select(NativeNotificationOutbox).where(
                    NativeNotificationOutbox.status.in_(ACTIVE_STATUSES)
                )
                if owner_principal_id is not None:
                    stmt = stmt.where(
                        _owner_scope_predicate(
                            owner_principal_id=owner_principal_id,
                            operator_session_id=operator_session_id,
                        )
                    )
                result = await db.execute(stmt)
                return len(result.scalars().all())

    async def clear(self) -> None:
        """Delete outbox rows for test isolation; production dismiss preserves receipts."""
        async with self._lock:
            async with self._session() as db:
                await db.execute(delete(NativeNotificationDeliveryAttempt))
                await db.execute(delete(NativeNotificationOutbox))


native_notification_queue = NativeNotificationQueue()
