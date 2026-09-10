"""Durable outbox for the built-in native desktop notification path.

The queue is intentionally a small delivery state machine shared by the
observer API and the macOS daemon. It is not an external transport and an
acknowledgement only proves that Seraph accepted a daemon receipt; it cannot
prove exactly-once display outside the process.

State transitions::

    queued -> claimed -> delivered
                     \\-> queued (lease expiry, retry budget remains)
                     \\-> unknown (lease expiry after retry budget)
                     \\-> failed (bounded daemon failure or expiry)
    queued/claimed -> cancelled (operator dismiss)
    failed -> queued (explicit bounded retry before deadline)

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

from sqlalchemy import delete, select, update

from src.db import engine as db_engine
from src.db.models import NativeNotificationDeliveryAttempt, NativeNotificationOutbox


ACTIVE_STATUSES = frozenset({"queued", "claimed"})
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
    )


def _valid_worker(value: object) -> str:
    worker = _validate_text(value, field="lease owner", max_chars=MAX_IDENTIFIER_CHARS, required=True)
    assert worker is not None
    return worker


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
    ) -> NativeNotification:
        """Persist one notification or return the matching idempotent row.

        Intervention-backed notifications derive their idempotency key from
        the intervention identity. Test/operator notifications without an
        intervention receive a fresh key unless the caller supplies one.
        Reusing a key with changed content raises a conflict rather than
        silently replacing an already-authorized delivery intent.
        """
        intervention_id = _validate_identifier(intervention_id, field="intervention_id")
        owner_principal_id = _validate_identifier(owner_principal_id, field="owner_principal_id")
        session_id = _validate_identifier(session_id, field="session_id")
        thread_id = _validate_identifier(thread_id, field="thread_id")
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
            "title": title_value,
            "body": body_value,
            "intervention_type": intervention_type,
            "urgency": urgency,
            "surface": surface_value,
            "session_id": session_id,
            "thread_id": thread_id or session_id,
            "thread_source": thread_source_value,
            "continuation_mode": continuation_mode_value,
            "resume_message": resume_value,
        }
        digest = _payload_digest(payload)
        now = _utc_now()

        async with self._lock:
            async with self._session() as db:
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
                    return _row_to_notification(existing)

                row = NativeNotificationOutbox(
                    idempotency_key=key,
                    payload_digest=digest,
                    intervention_id=intervention_id,
                    owner_principal_id=owner_principal_id,
                    title=title_value,
                    body=body_value,
                    intervention_type=intervention_type,
                    urgency=urgency,
                    surface=surface_value,
                    session_id=session_id,
                    thread_id=thread_id or session_id,
                    thread_source=thread_source_value,
                    continuation_mode=continuation_mode_value,
                    resume_message=resume_value,
                    status="queued",
                    max_attempts=self.max_attempts,
                    deadline_at=now + timedelta(seconds=self.ttl_seconds),
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
                await db.flush()
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
            select(NativeNotificationOutbox).where(NativeNotificationOutbox.status == "claimed")
        )
        for row in result.scalars().all():
            lease_expires_at = _aware(row.lease_expires_at)
            if lease_expires_at is not None and lease_expires_at > now:
                continue
            if lease_expires_at is None:
                # A claimed row without a lease cannot be safely replayed or
                # acknowledged: its external display outcome is ambiguous.
                await self._finish_attempt(
                    db,
                    row,
                    status="unknown",
                    now=now,
                    error_code="claimed_lease_missing",
                )
                row.status = "unknown"
                row.last_error = "claimed_lease_missing"
                row.lease_owner = None
                row.lease_expires_at = None
                row.updated_at = now
                continue
            await self._finish_attempt(db, row, status="lease_expired", now=now, error_code="lease_expired")
            row.lease_owner = None
            row.lease_expires_at = None
            row.updated_at = now
            deadline_at = _aware(row.deadline_at) or now
            if deadline_at <= now:
                row.status = "unknown"
                row.last_error = "deadline_expired_after_claim"
            elif row.attempt_count < row.max_attempts:
                row.status = "queued"
                row.last_error = "lease_expired_retryable"
            else:
                row.status = "unknown"
                row.last_error = "lease_expired_retry_budget_exhausted"

        queued_result = await db.execute(
            select(NativeNotificationOutbox).where(
                NativeNotificationOutbox.status == "queued",
                NativeNotificationOutbox.deadline_at <= now,
            )
        )
        for row in queued_result.scalars().all():
            row.status = "failed"
            row.last_error = "deadline_expired"
            row.updated_at = now

        malformed_result = await db.execute(
            select(NativeNotificationOutbox).where(
                NativeNotificationOutbox.status == "queued",
                (
                    (NativeNotificationOutbox.deadline_at.is_(None))
                    | (NativeNotificationOutbox.max_attempts < 1)
                    | (NativeNotificationOutbox.attempt_count < 0)
                    | (NativeNotificationOutbox.fencing_token < 0)
                ),
            )
        )
        for row in malformed_result.scalars().all():
            row.status = "failed"
            row.last_error = "malformed_outbox_state"
            row.updated_at = now

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
                        NativeNotificationOutbox.created_at.asc(),
                        NativeNotificationOutbox.id.asc(),
                    )
                    .limit(1)
                )
                row = result.scalar_one_or_none()
                if row is None:
                    return None
                if row.status == "claimed":
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
        """Return the next item while claiming it for the built-in daemon."""
        return await self.claim_next()

    async def get(self, notification_id: str) -> NativeNotification | None:
        notification_id = _validate_identifier(notification_id, field="notification_id")
        if not notification_id:
            return None
        async with self._lock:
            async with self._session() as db:
                await self._reconcile_expired(db, _utc_now())
                result = await db.execute(
                    select(NativeNotificationOutbox).where(
                        NativeNotificationOutbox.id == notification_id
                    )
                )
                row = result.scalar_one_or_none()
                return _row_to_notification(row) if row is not None else None

    async def list(self) -> list[NativeNotification]:
        async with self._lock:
            async with self._session() as db:
                now = _utc_now()
                await self._reconcile_expired(db, now)
                result = await db.execute(
                    select(NativeNotificationOutbox)
                    .where(NativeNotificationOutbox.status.in_(ACTIVE_STATUSES))
                    .order_by(
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
                if row.status == "claimed":
                    if row.lease_expires_at is None or (_aware(row.lease_expires_at) or now) <= now:
                        return False
                    # A claimed row can only be completed by a receipt from
                    # the claim that owns it. The legacy empty-body API
                    # cannot acknowledge a daemon claim without its fence.
                    if fencing_token is None or row.fencing_token != fencing_token:
                        return False
                    if worker is not None and row.lease_owner != worker:
                        return False
                    predicates = [
                        NativeNotificationOutbox.id == notification_id,
                        NativeNotificationOutbox.status == "claimed",
                        NativeNotificationOutbox.fencing_token == fencing_token,
                        NativeNotificationOutbox.lease_expires_at > now,
                    ]
                    if worker is not None:
                        predicates.append(NativeNotificationOutbox.lease_owner == worker)
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
                elif (
                    row.status != "queued"
                    or fencing_token is not None
                    or worker is not None
                    or row.attempt_count != 0
                ):
                    return False
                else:
                    # The conditional update also prevents an operator/test
                    # ACK racing a daemon claim from completing that claim.
                    ack_result = await db.execute(
                        update(NativeNotificationOutbox)
                        .execution_options(synchronize_session=False)
                        .where(
                            NativeNotificationOutbox.id == notification_id,
                            NativeNotificationOutbox.status == "queued",
                            NativeNotificationOutbox.attempt_count == 0,
                            NativeNotificationOutbox.fencing_token == 0,
                            NativeNotificationOutbox.deadline_at > now,
                        )
                        .values(
                            status="delivered",
                            attempt_count=NativeNotificationOutbox.attempt_count + 1,
                            fencing_token=NativeNotificationOutbox.fencing_token + 1,
                            delivered_at=now,
                            updated_at=now,
                        )
                    )
                    if ack_result.rowcount != 1:
                        return False
                    await db.refresh(row)
                    # Keep direct browser/test acknowledgement compatible by
                    # creating a fenced attempt in the same transaction.
                    db.add(
                        NativeNotificationDeliveryAttempt(
                            notification_id=row.id,
                            attempt_index=row.attempt_count,
                            lease_owner="observer-api",
                            fencing_token=row.fencing_token,
                            status="claimed",
                            started_at=now,
                        )
                    )
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
        """Record a bounded known delivery failure and requeue if permitted."""
        notification_id = _validate_identifier(notification_id, field="notification_id")
        if not notification_id:
            return False
        worker = _valid_worker(worker_id) if worker_id is not None else None
        if fencing_token is not None and (not isinstance(fencing_token, int) or fencing_token < 1):
            raise NativeNotificationLeaseError("fencing_token must be a positive integer")

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
                if row.status != "claimed":
                    return False
                if row.lease_expires_at is None or (_aware(row.lease_expires_at) or now) <= now:
                    return False
                if fencing_token is None or row.fencing_token != fencing_token:
                    return False
                if worker is not None and row.lease_owner != worker:
                    return False

                safe_reason = _safe_reason(reason)
                next_status = (
                    "queued"
                    if row.attempt_count < row.max_attempts and (_aware(row.deadline_at) or now) > now
                    else "failed"
                )
                predicates = [
                    NativeNotificationOutbox.id == notification_id,
                    NativeNotificationOutbox.status == "claimed",
                    NativeNotificationOutbox.fencing_token == fencing_token,
                    NativeNotificationOutbox.lease_expires_at > now,
                ]
                if worker is not None:
                    predicates.append(NativeNotificationOutbox.lease_owner == worker)
                fail_result = await db.execute(
                    update(NativeNotificationOutbox)
                    .execution_options(synchronize_session=False)
                    .where(*predicates)
                    .values(
                        status=next_status,
                        last_error=safe_reason,
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
                    status="failed",
                    now=now,
                    error_code=safe_reason,
                )
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
                row.updated_at = now
                await db.flush()
                return True

    async def dismiss(self, notification_id: str) -> NativeNotification | None:
        """Cancel a pending notification and retain its audit receipt."""
        notification_id = _validate_identifier(notification_id, field="notification_id")
        if not notification_id:
            return None
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
                if row is None or row.status not in ACTIVE_STATUSES:
                    return None
                await self._finish_attempt(db, row, status="cancelled", now=now, error_code="operator_dismissed")
                row.status = "cancelled"
                row.cancelled_at = now
                row.lease_owner = None
                row.lease_expires_at = None
                row.updated_at = now
                await db.flush()
                return _row_to_notification(row)

    async def dismiss_all(self) -> list[NativeNotification]:
        """Cancel all pending notifications while preserving their receipts."""
        async with self._lock:
            async with self._session() as db:
                now = _utc_now()
                await self._reconcile_expired(db, now)
                result = await db.execute(
                    select(NativeNotificationOutbox)
                    .where(NativeNotificationOutbox.status.in_(ACTIVE_STATUSES))
                    .order_by(
                        NativeNotificationOutbox.created_at.asc(),
                        NativeNotificationOutbox.id.asc(),
                    )
                )
                rows = list(result.scalars().all())
                for row in rows:
                    await self._finish_attempt(
                        db,
                        row,
                        status="cancelled",
                        now=now,
                        error_code="operator_dismissed",
                    )
                    row.status = "cancelled"
                    row.cancelled_at = now
                    row.lease_owner = None
                    row.lease_expires_at = None
                    row.updated_at = now
                await db.flush()
                return [_row_to_notification(row) for row in rows]

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

    async def count(self) -> int:
        async with self._lock:
            async with self._session() as db:
                now = _utc_now()
                await self._reconcile_expired(db, now)
                result = await db.execute(
                    select(NativeNotificationOutbox).where(
                        NativeNotificationOutbox.status.in_(ACTIVE_STATUSES)
                    )
                )
                return len(result.scalars().all())

    async def clear(self) -> None:
        """Delete outbox rows for test isolation; production dismiss preserves receipts."""
        async with self._lock:
            async with self._session() as db:
                await db.execute(delete(NativeNotificationDeliveryAttempt))
                await db.execute(delete(NativeNotificationOutbox))


native_notification_queue = NativeNotificationQueue()
