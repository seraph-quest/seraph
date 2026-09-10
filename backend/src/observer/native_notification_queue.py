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

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError

from src.db import engine as db_engine
from src.db.models import (
    NativeNotificationDeliveryAttempt,
    NativeNotificationOutbox,
    QueuedInsight,
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
        source_insight_ids: list[str] | None = None,
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
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
            if transition.rowcount != 1:
                continue
            await db.refresh(row)
            await self._finish_attempt(db, row, status="unknown", now=now, error_code=reason)

        await db.execute(
            update(NativeNotificationOutbox)
            .execution_options(synchronize_session=False)
            .where(
                NativeNotificationOutbox.status == "queued",
                NativeNotificationOutbox.deadline_at <= now,
            )
            .values(status="failed", last_error="deadline_expired", updated_at=now)
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
            .values(status="failed", last_error="malformed_outbox_state", updated_at=now)
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
                row.updated_at = now
                await db.flush()
                return True

    async def reconcile_unknown(self, notification_id: str, *, retry: bool = False) -> bool:
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
                result = await db.execute(
                    update(NativeNotificationOutbox)
                    .execution_options(synchronize_session=False)
                    .where(
                        NativeNotificationOutbox.id == notification_id,
                        NativeNotificationOutbox.status == "unknown",
                        NativeNotificationOutbox.attempt_count < NativeNotificationOutbox.max_attempts,
                        NativeNotificationOutbox.deadline_at > now,
                    )
                    .values(
                        status="queued",
                        last_error="operator_reconciled_retry",
                        lease_owner=None,
                        lease_expires_at=None,
                        updated_at=now,
                    )
                )
                await db.flush()
                return result.rowcount == 1

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
                if row.status in CLAIMED_STATUSES:
                    # Browser dismissal has no daemon fence. It can stop a
                    # future ACK, but must preserve ambiguity for recovery.
                    dismiss_result = await db.execute(
                        update(NativeNotificationOutbox)
                        .execution_options(synchronize_session=False)
                        .where(
                            NativeNotificationOutbox.id == notification_id,
                            NativeNotificationOutbox.status.in_(CLAIMED_STATUSES),
                            NativeNotificationOutbox.fencing_token == row.fencing_token,
                        )
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
                    dismiss_result = await db.execute(
                        update(NativeNotificationOutbox)
                        .execution_options(synchronize_session=False)
                        .where(
                            NativeNotificationOutbox.id == notification_id,
                            NativeNotificationOutbox.status == "queued",
                            NativeNotificationOutbox.attempt_count == row.attempt_count,
                            NativeNotificationOutbox.fencing_token == row.fencing_token,
                        )
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
                changed: list[NativeNotification] = []
                for row in rows:
                    if row.status in CLAIMED_STATUSES:
                        dismiss_result = await db.execute(
                            update(NativeNotificationOutbox)
                            .execution_options(synchronize_session=False)
                            .where(
                                NativeNotificationOutbox.id == row.id,
                                NativeNotificationOutbox.status.in_(CLAIMED_STATUSES),
                                NativeNotificationOutbox.fencing_token == row.fencing_token,
                            )
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
                        dismiss_result = await db.execute(
                            update(NativeNotificationOutbox)
                            .execution_options(synchronize_session=False)
                            .where(
                                NativeNotificationOutbox.id == row.id,
                                NativeNotificationOutbox.status == "queued",
                                NativeNotificationOutbox.attempt_count == row.attempt_count,
                                NativeNotificationOutbox.fencing_token == row.fencing_token,
                            )
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

    async def recovery(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return bounded failed/unknown rows and operator recovery state."""
        if limit < 1 or limit > 100:
            raise ValueError("recovery limit must be between 1 and 100")
        async with self._lock:
            async with self._session() as db:
                await self._reconcile_expired(db, _utc_now())
                result = await db.execute(
                    select(NativeNotificationOutbox)
                    .where(NativeNotificationOutbox.status.in_({"failed", "unknown"}))
                    .order_by(
                        NativeNotificationOutbox.updated_at.desc(),
                        NativeNotificationOutbox.id.asc(),
                    )
                    .limit(limit)
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
                            "recovery_required": row.status == "unknown",
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
