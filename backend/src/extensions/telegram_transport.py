"""Provider-free, durable Telegram transport seam for #752.

The adapter deliberately has no Telegram or model client.  A recording
transport is injected at the effect boundary, while pairing, replay, consent,
canonical conversation handoff, quarantine metadata, and delivery receipts are
stored in the normal SQLite workspace.  This keeps local tests useful without
turning a fixture into a claim that Telegram is live.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from inspect import isawaitable
from typing import Any, Protocol

from sqlalchemy import select, update

from src.agent.session import MessageIngressConflictError, SessionNotFoundError, SessionOwnerMismatchError, session_manager
from src.conversation.identity import (
    ConversationIdentityError,
    build_conversation_identity,
    build_lineage,
    issue_attachment_quarantine_receipt,
    validate_attachment_refs,
)
from src.db import engine as db_engine
from src.db.models import (
    TelegramDeliveryAttempt,
    TelegramInboundUpdate,
    Message,
    TelegramTransportOutbox,
    TelegramTransportState,
)
from src.extensions.telegram_ingress import (
    OPENROUTER_INFERENCE_CONSENT_SCOPE,
    TELEGRAM_TRANSIT_CONSENT_SCOPE,
    TelegramAttachmentMetadata,
    TelegramConsent,
    TelegramConsentState,
    TelegramIngressPolicy,
    TelegramIngressState,
    TelegramPairingLifecycleState,
    TelegramPairingSnapshot,
    TelegramRateEvent,
    TelegramReplayEntry,
    TelegramUpdate,
    build_telegram_ingress_receipt,
    ingest_telegram_update,
)
from src.guardian.audio_ingress import AudioProviderStatus
from src.vault.repository import vault_repository


TELEGRAM_TRANSPORT_SCHEMA_VERSION = "seraph.telegram.transport.v1"
TELEGRAM_TOKEN_SECRET_PREFIX = "telegram.transport.token:"
TELEGRAM_CONSENT_TTL = timedelta(minutes=15)
TELEGRAM_PAIRING_MAX_TTL = timedelta(days=30)
TELEGRAM_DEFAULT_MAX_ATTEMPTS = 3
TELEGRAM_MAX_TEXT_CHARS = 50_000
TELEGRAM_MAX_UPDATE_BYTES = 1_000_000


class TelegramTransportError(RuntimeError):
    """Bounded operator-safe adapter error."""

    def __init__(self, code: str, message: str = "Telegram transport operation failed") -> None:
        self.code = code
        super().__init__(message)


class InjectedTelegramTransport(Protocol):
    intercepted: bool

    async def send_message(self, *, token: str, chat_id: int, text: str, idempotency_key: str) -> object: ...

    async def send_voice(
        self,
        *,
        token: str,
        chat_id: int,
        attachment: dict[str, Any],
        caption: str,
        idempotency_key: str,
    ) -> object: ...


@dataclass
class RecordingTelegramTransport:
    """Deterministic local HTTP-like transport used by the adapter and tests.

    ``responses`` may contain status dictionaries, exceptions, or callables.
    Raw tokens are intentionally omitted from ``calls`` so a test receipt can
    be safely shown to an operator.
    """

    responses: list[object] = field(default_factory=list)
    intercepted: bool = True
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def _response(self, *, kind: str, chat_id: int, idempotency_key: str, **payload: Any) -> object:
        self.calls.append({"kind": kind, "chat_id": chat_id, "idempotency_key": idempotency_key, **payload})
        if self.responses:
            response = self.responses.pop(0)
            if callable(response):
                response = response()
            if isawaitable(response):
                response = await response
            if isinstance(response, BaseException):
                raise response
            return response
        return {"status_code": 200, "message_id": f"recorded:{uuid.uuid4().hex[:12]}"}

    async def send_message(self, *, token: str, chat_id: int, text: str, idempotency_key: str) -> object:
        return await self._response(
            kind="text",
            chat_id=chat_id,
            idempotency_key=idempotency_key,
            text=text,
            token_present=bool(token),
        )

    async def send_voice(
        self,
        *,
        token: str,
        chat_id: int,
        attachment: dict[str, Any],
        caption: str,
        idempotency_key: str,
    ) -> object:
        return await self._response(
            kind="voice",
            chat_id=chat_id,
            idempotency_key=idempotency_key,
            attachment_id=attachment.get("attachment_id"),
            caption=caption,
            token_present=bool(token),
        )


@dataclass(frozen=True)
class TelegramTransportReceipt:
    status: str
    reason_code: str
    idempotency_key: str | None = None
    request_digest: str | None = None
    session_id: str | None = None
    canonical_message_id: str | None = None
    outbox_id: str | None = None
    delivery_attempt: int | None = None
    response_code: int | None = None
    retryable: bool = False
    payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": TELEGRAM_TRANSPORT_SCHEMA_VERSION,
            "status": self.status,
            "reason_code": self.reason_code,
            "idempotency_key": self.idempotency_key,
            "request_digest": self.request_digest,
            "session_id": self.session_id,
            "canonical_message_id": self.canonical_message_id,
            "outbox_id": self.outbox_id,
            "delivery_attempt": self.delivery_attempt,
            "response_code": self.response_code,
            "retryable": self.retryable,
            **self.payload,
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    try:
        return value.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _parse_datetime(value: object, *, default: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        parsed = _aware(value)
    elif isinstance(value, str):
        try:
            parsed = _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            parsed = None
    else:
        parsed = None
    if parsed is None:
        if default is not None:
            return default
        raise TelegramTransportError("invalid_timestamp", "Telegram timestamp is invalid")
    return parsed


def _owner(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 256:
        raise TelegramTransportError("authentication_required", "Telegram transport requires an owner principal")
    return normalized


def _session(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 256:
        raise TelegramTransportError("operator_session_required", "Telegram transport requires an operator session")
    return normalized


def _positive_int(value: object, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TelegramTransportError(code, f"{code} must be a positive integer")
    return value


def _sha(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _session_id(owner: str, chat_id: int) -> str:
    return "telegram-conversation:" + hashlib.sha256(f"{owner}:{chat_id}".encode()).hexdigest()[:32]


def _consent_from_state(
    reference: str | None,
    expires_at: datetime | None,
    scope: str,
    now: datetime,
) -> TelegramConsent | None:
    expires = _aware(expires_at)
    if not reference or expires is None:
        return None
    return TelegramConsent(
        reference=reference,
        state=TelegramConsentState.ACTIVE,
        granted_at=now - timedelta(seconds=1),
        expires_at=expires,
        scope=scope,
    )


class TelegramTransportAdapter:
    """Durable local Telegram ingress/outbox with an injected effect seam."""

    def __init__(
        self,
        *,
        transport: InjectedTelegramTransport | None = None,
        max_attempts: int = TELEGRAM_DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        if max_attempts < 1 or max_attempts > 5:
            raise ValueError("max_attempts must be between 1 and 5")
        self.transport = transport or RecordingTelegramTransport()
        self.max_attempts = max_attempts
        self._lock = asyncio.Lock()

    async def _state(self, db) -> TelegramTransportState | None:
        result = await db.execute(select(TelegramTransportState).where(TelegramTransportState.id == "telegram"))
        return result.scalar_one_or_none()

    async def _required_state(self, *, owner_principal_id: str, operator_session_id: str, now: datetime | None = None) -> TelegramTransportState:
        owner = _owner(owner_principal_id)
        operator_session = _session(operator_session_id)
        current = _aware(now) or _now()
        async with db_engine.get_session() as db:
            row = await self._state(db)
            if row is None:
                raise TelegramTransportError("telegram_unconfigured", "Telegram pairing is not configured")
            if row.owner_principal_id != owner or row.operator_session_id != operator_session:
                raise TelegramTransportError("telegram_authority_mismatch", "Telegram pairing belongs to another operator session")
            if row.pairing_state != "active":
                raise TelegramTransportError(f"telegram_pairing_{row.pairing_state}", "Telegram pairing is not active")
            expiry = _aware(row.pairing_expires_at)
            if expiry is not None and expiry <= current:
                row.pairing_state = "expired"
                db.add(row)
                raise TelegramTransportError("telegram_pairing_expired", "Telegram pairing has expired")
            return row

    async def pair(
        self,
        *,
        owner_principal_id: str,
        operator_session_id: str,
        operator_id: int,
        chat_id: int,
        token: str | None = None,
        expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        owner = _owner(owner_principal_id)
        operator_session = _session(operator_session_id)
        operator_id = _positive_int(operator_id, "invalid_operator_id")
        chat_id = _positive_int(chat_id, "invalid_chat_id")
        current = _now()
        expiry = _aware(expires_at) if expires_at is not None else None
        if expiry is not None and (expiry <= current or expiry - current > TELEGRAM_PAIRING_MAX_TTL):
            raise TelegramTransportError("pairing_expiry_invalid", "Telegram pairing expiry is outside the bound")
        raw_token = str(token or "").strip() or f"telegram-test-token-{secrets.token_urlsafe(24)}"
        if len(raw_token.encode()) > 4096:
            raise TelegramTransportError("telegram_token_invalid", "Telegram token is too large")
        secret_ref = f"{TELEGRAM_TOKEN_SECRET_PREFIX}{owner}:{chat_id}"
        async with self._lock:
            async with db_engine.get_session() as db:
                existing = await self._state(db)
                if existing is not None and existing.owner_principal_id not in (None, owner):
                    raise TelegramTransportError("telegram_authority_mismatch", "Telegram pairing belongs to another operator")
            # Keep the vault transaction independent from the state
            # transaction.  This matters for SQLite's single-writer behavior
            # and keeps a failed state write from leaving a credential with no
            # pairing projection.
            await vault_repository.store(secret_ref, raw_token, description="Scoped provider-free Telegram transport token")
            async with db_engine.get_session() as db:
                existing = await self._state(db)
                if existing is not None and existing.owner_principal_id not in (None, owner):
                    raise TelegramTransportError("telegram_authority_mismatch", "Telegram pairing belongs to another operator")
                values = {
                    "owner_principal_id": owner,
                    "operator_session_id": operator_session,
                    "operator_id": operator_id,
                    "chat_id": chat_id,
                    "pairing_id": f"telegram-pairing:{uuid.uuid4().hex}",
                    "pairing_state": "active",
                    "pairing_expires_at": expiry,
                    "token_secret_ref": secret_ref,
                    "token_fingerprint": _token_fingerprint(raw_token),
                    "transit_consent_reference": None,
                    "transit_consent_expires_at": None,
                    "model_consent_reference": None,
                    "model_consent_expires_at": None,
                    "cursor": 0,
                    "sequence": 0,
                    "rate_events_json": "[]",
                    "revoked_at": None,
                    "updated_at": current,
                }
                if existing is None:
                    db.add(TelegramTransportState(id="telegram", **values))
                else:
                    for key, value in values.items():
                        setattr(existing, key, value)
                    db.add(existing)
                await db.flush()
                return self._state_payload_from_row(await self._state(db), now=current)

    async def grant_consent(
        self,
        *,
        owner_principal_id: str,
        operator_session_id: str,
        boundary: str,
        ttl: timedelta = TELEGRAM_CONSENT_TTL,
    ) -> dict[str, Any]:
        owner = _owner(owner_principal_id)
        operator_session = _session(operator_session_id)
        if boundary not in {TELEGRAM_TRANSIT_CONSENT_SCOPE, OPENROUTER_INFERENCE_CONSENT_SCOPE}:
            raise TelegramTransportError("consent_scope_invalid", "Telegram consent boundary is invalid")
        if ttl.total_seconds() <= 0 or ttl > TELEGRAM_CONSENT_TTL:
            raise TelegramTransportError("consent_expiry_invalid", "Telegram consent lifetime is outside the bound")
        async with self._lock:
            async with db_engine.get_session() as db:
                row = await self._state(db)
                if row is None or row.owner_principal_id != owner or row.operator_session_id != operator_session:
                    raise TelegramTransportError("telegram_authority_mismatch", "Telegram pairing belongs to another operator session")
                if row.pairing_state != "active":
                    raise TelegramTransportError("telegram_pairing_not_active", "Telegram pairing is not active")
                current = _now()
                reference = f"telegram-consent:{boundary}:{uuid.uuid4().hex}"
                expiry = current + ttl
                if boundary == TELEGRAM_TRANSIT_CONSENT_SCOPE:
                    row.transit_consent_reference = reference
                    row.transit_consent_expires_at = expiry
                else:
                    row.model_consent_reference = reference
                    row.model_consent_expires_at = expiry
                row.updated_at = current
                db.add(row)
                await db.flush()
                return {
                    "reference": reference,
                    "boundary": boundary,
                    "state": "active",
                    "granted_at": current.isoformat(),
                    "expires_at": expiry.isoformat(),
                }

    async def revoke_consent(self, *, owner_principal_id: str, operator_session_id: str, boundary: str) -> dict[str, Any]:
        owner = _owner(owner_principal_id)
        operator_session = _session(operator_session_id)
        if boundary not in {TELEGRAM_TRANSIT_CONSENT_SCOPE, OPENROUTER_INFERENCE_CONSENT_SCOPE}:
            raise TelegramTransportError("consent_scope_invalid", "Telegram consent boundary is invalid")
        async with self._lock:
            async with db_engine.get_session() as db:
                row = await self._state(db)
                if row is None or row.owner_principal_id != owner or row.operator_session_id != operator_session:
                    raise TelegramTransportError("telegram_authority_mismatch", "Telegram pairing belongs to another operator session")
                current = _now()
                if boundary == TELEGRAM_TRANSIT_CONSENT_SCOPE:
                    row.transit_consent_expires_at = current
                    row.transit_consent_reference = None
                    await db.execute(
                        update(TelegramTransportOutbox)
                        .where(
                            TelegramTransportOutbox.owner_principal_id == owner,
                            TelegramTransportOutbox.operator_session_id == operator_session,
                            TelegramTransportOutbox.status.in_({"queued", "sending", "unknown"}),
                        )
                        .values(status="cancelled", last_error="telegram_transit_consent_revoked", updated_at=current)
                    )
                else:
                    row.model_consent_expires_at = current
                    row.model_consent_reference = None
                row.updated_at = current
                db.add(row)
                await db.flush()
                return self._state_payload_from_row(row, now=current)

    async def revoke(self, *, owner_principal_id: str, operator_session_id: str, reason: str = "operator_revoked") -> dict[str, Any]:
        owner = _owner(owner_principal_id)
        operator_session = _session(operator_session_id)
        async with self._lock:
            async with db_engine.get_session() as db:
                row = await self._state(db)
                if row is None or row.owner_principal_id != owner or row.operator_session_id != operator_session:
                    raise TelegramTransportError("telegram_authority_mismatch", "Telegram pairing belongs to another operator session")
                current = _now()
                row.pairing_state = "revoked"
                row.revoked_at = current
                row.updated_at = current
                db.add(row)
                await db.execute(
                    update(TelegramTransportOutbox)
                    .where(
                        TelegramTransportOutbox.owner_principal_id == owner,
                        TelegramTransportOutbox.operator_session_id == operator_session,
                        TelegramTransportOutbox.status.in_({"queued", "sending", "unknown"}),
                    )
                    .values(status="cancelled", last_error=reason[:128], updated_at=current)
                )
                await db.flush()
                return self._state_payload_from_row(row, now=current)

    def _state_payload_from_row(self, row: TelegramTransportState | None, *, now: datetime | None = None) -> dict[str, Any]:
        current = now or _now()
        if row is None:
            return {
                "configured": False,
                "pairing_state": "unpaired",
                "transport_mode": "injected_recording",
                "live_transport": False,
            }
        def consent_payload(reference: str | None, expiry: datetime | None) -> dict[str, Any]:
            expires = _aware(expiry)
            return {
                "configured": bool(reference and expires and expires > current),
                "reference": reference,
                "expires_at": expires.isoformat() if expires else None,
                "state": "active" if reference and expires and expires > current else "expired" if reference else "missing",
            }
        return {
            "configured": row.pairing_state == "active" and bool(row.token_secret_ref),
            "pairing_state": row.pairing_state,
            "pairing_id": row.pairing_id,
            "operator_id": row.operator_id,
            "chat_id": row.chat_id,
            "owner_principal_id": row.owner_principal_id,
            "operator_session_id": row.operator_session_id,
            "pairing_expires_at": _aware(row.pairing_expires_at).isoformat() if _aware(row.pairing_expires_at) else None,
            "token_configured": bool(row.token_secret_ref),
            "token_fingerprint": row.token_fingerprint,
            "cursor": row.cursor,
            "sequence": row.sequence,
            "consent": {
                "telegram_transit": consent_payload(row.transit_consent_reference, row.transit_consent_expires_at),
                "openrouter_inference": consent_payload(row.model_consent_reference, row.model_consent_expires_at),
            },
            "transport_mode": "injected_recording" if getattr(self.transport, "intercepted", False) else "unavailable",
            "live_transport": False,
        }

    async def status(self, *, owner_principal_id: str | None = None, operator_session_id: str | None = None) -> dict[str, Any]:
        async with db_engine.get_session() as db:
            row = await self._state(db)
            if row is not None and owner_principal_id is not None and (
                row.owner_principal_id != _owner(owner_principal_id) or row.operator_session_id != _session(operator_session_id)
            ):
                raise TelegramTransportError("telegram_authority_mismatch", "Telegram pairing belongs to another operator session")
            return self._state_payload_from_row(row)

    async def _build_update(self, payload: dict[str, Any], row: TelegramTransportState, *, now: datetime) -> TelegramUpdate:
        if not isinstance(payload, dict):
            raise TelegramTransportError("invalid_update", "Telegram update must be an object")
        try:
            if len(repr(payload).encode("utf-8", "replace")) > TELEGRAM_MAX_UPDATE_BYTES:
                raise TelegramTransportError("update_too_large", "Telegram update exceeds the local bound")
        except (MemoryError, UnicodeError) as exc:
            raise TelegramTransportError("update_too_large", "Telegram update exceeds the local bound") from exc
        message = payload.get("message") if isinstance(payload.get("message"), dict) else payload
        user = message.get("from") if isinstance(message.get("from"), dict) else message
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else message
        operator_id = user.get("id", payload.get("operator_id"))
        chat_id = chat.get("id", payload.get("chat_id"))
        update_id = payload.get("update_id", message.get("update_id"))
        message_id = message.get("message_id", payload.get("message_id"))
        sequence = payload.get("sequence", row.sequence + 1)
        operator_id = _positive_int(operator_id, "invalid_operator_id")
        chat_id = _positive_int(chat_id, "invalid_chat_id")
        update_id = _positive_int(update_id, "invalid_update_id")
        message_id = _positive_int(message_id, "invalid_message_id")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise TelegramTransportError("invalid_sequence", "Telegram sequence is invalid")
        text = message.get("text", payload.get("text"))
        if text is not None and not isinstance(text, str):
            raise TelegramTransportError("invalid_text", "Telegram text is invalid")
        attachment_payload = message.get("voice") or payload.get("attachment")
        attachment = None
        if attachment_payload is not None:
            if not isinstance(attachment_payload, dict):
                raise TelegramTransportError("invalid_attachment", "Telegram attachment is invalid")
            attachment_id = attachment_payload.get("attachment_id") or attachment_payload.get("file_unique_id") or attachment_payload.get("file_id")
            content = attachment_payload.get("content")
            content_hash = attachment_payload.get("content_hash")
            size_bytes = attachment_payload.get("size_bytes")
            if isinstance(content, bytes):
                content_hash = "sha256:" + hashlib.sha256(content).hexdigest()
                size_bytes = len(content)
            attachment = TelegramAttachmentMetadata(
                attachment_id=str(attachment_id or ""),
                media_type=str(attachment_payload.get("media_type") or "audio/ogg"),
                size_bytes=int(size_bytes or 0),
                content_hash=str(content_hash or ""),
                voice_note=True,
                duration_seconds=float(attachment_payload.get("duration_seconds") or attachment_payload.get("duration") or 0),
                file_reference=None,
            )
            text = None
        return TelegramUpdate(
            operator_id=operator_id,
            chat_id=chat_id,
            update_id=update_id,
            message_id=message_id,
            received_at=_parse_datetime(payload.get("received_at", message.get("date")), default=now),
            text=text,
            attachment=attachment,
            external_transit_consent=_consent_from_state(row.transit_consent_reference, row.transit_consent_expires_at, TELEGRAM_TRANSIT_CONSENT_SCOPE, now),
            openrouter_consent=_consent_from_state(row.model_consent_reference, row.model_consent_expires_at, OPENROUTER_INFERENCE_CONSENT_SCOPE, now),
            sequence=sequence,
        )

    async def _ingress_state(self, db, *, row: TelegramTransportState) -> TelegramIngressState:
        result = await db.execute(
            select(TelegramInboundUpdate)
            .where(TelegramInboundUpdate.owner_principal_id == row.owner_principal_id)
            .where(TelegramInboundUpdate.operator_session_id == row.operator_session_id)
            .order_by(TelegramInboundUpdate.sequence.desc())
            .limit(128)
        )
        records = list(result.scalars().all())
        entries = tuple(
            TelegramReplayEntry(
                update_id=item.update_id,
                message_id=item.message_id,
                sequence=item.sequence,
                idempotency_key=item.idempotency_key,
                request_digest=item.request_digest,
                accepted_at=_aware(item.created_at) or _now(),
            )
            for item in reversed(records)
            if item.status in {"accepted", "degraded"}
        )
        try:
            events = json.loads(row.rate_events_json or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            events = []
        rate_events = tuple(
            TelegramRateEvent(str(item["idempotency_key"]), _parse_datetime(item["accepted_at"]))
            for item in events
            if isinstance(item, dict) and item.get("idempotency_key")
        )
        return TelegramIngressState(last_sequence=row.sequence, replay_entries=entries[-64:], rate_events=rate_events[-20:])

    def _policy(self, row: TelegramTransportState, *, now: datetime) -> TelegramIngressPolicy:
        assert row.operator_id is not None and row.chat_id is not None and row.pairing_id
        snapshot = TelegramPairingSnapshot(
            pairing_id=row.pairing_id,
            operator_id=row.operator_id,
            chat_id=row.chat_id,
            lifecycle=TelegramPairingLifecycleState.ACTIVE,
            expires_at=_aware(row.pairing_expires_at),
            server_owned_identity=True,
        )
        return TelegramIngressPolicy(
            operator_id=row.operator_id,
            chat_id=row.chat_id,
            pairing_id=row.pairing_id,
            pairing_snapshot=snapshot,
            provider_status=AudioProviderStatus.UNAVAILABLE,
        )

    async def ingest_update(
        self,
        payload: dict[str, Any],
        *,
        owner_principal_id: str,
        operator_session_id: str,
    ) -> dict[str, Any]:
        owner = _owner(owner_principal_id)
        operator_session = _session(operator_session_id)
        async with self._lock:
            async with db_engine.get_session() as db:
                row = await self._state(db)
                if row is None or row.owner_principal_id != owner or row.operator_session_id != operator_session:
                    raise TelegramTransportError("telegram_authority_mismatch", "Telegram pairing belongs to another operator session")
                current = _now()
                if row.pairing_state != "active":
                    raise TelegramTransportError("telegram_pairing_not_active", "Telegram pairing is not active")
                update_payload = await self._build_update(payload, row, now=current)
                # A retry that omits the adapter sequence must retain the
                # original canonical digest.  Resolve that sequence from the
                # durable update ledger before running the pure validator.
                if "sequence" not in payload:
                    prior_result = await db.execute(
                        select(TelegramInboundUpdate)
                        .where(TelegramInboundUpdate.operator_id == update_payload.operator_id)
                        .where(TelegramInboundUpdate.chat_id == update_payload.chat_id)
                        .where(TelegramInboundUpdate.update_id == update_payload.update_id)
                    )
                    prior = prior_result.scalar_one_or_none()
                    if prior is not None:
                        update_payload = replace(update_payload, sequence=prior.sequence)
                state = await self._ingress_state(db, row=row)
                policy = self._policy(row, now=current)
                result, next_state = ingest_telegram_update(state, update_payload, policy, now=current)
                receipt = build_telegram_ingress_receipt(update_payload, result, policy=policy).as_payload()
                if result.status.value == "duplicate" and result.idempotency_key:
                    existing_result = await db.execute(
                        select(TelegramInboundUpdate).where(TelegramInboundUpdate.idempotency_key == result.idempotency_key)
                    )
                    existing = existing_result.scalar_one_or_none()
                    if existing is not None:
                        return json.loads(existing.receipt_json)
                if result.status.value not in {"accepted", "degraded"}:
                    return TelegramTransportReceipt(
                        status=result.status.value,
                        reason_code=result.reason_code,
                        idempotency_key=result.idempotency_key,
                        request_digest=result.request_digest,
                        retryable=result.retryable,
                        payload={"ingress_receipt": receipt},
                    ).as_dict()
                assert result.idempotency_key and result.request_digest
                canonical_session_id = _session_id(owner, update_payload.chat_id)
                try:
                    session = await session_manager.get_or_create(canonical_session_id, owner_principal_id=owner)
                except (SessionOwnerMismatchError, SessionNotFoundError) as exc:
                    raise TelegramTransportError("conversation_owner_mismatch", str(exc)) from exc
                canonical_message_id = uuid.uuid5(uuid.NAMESPACE_URL, f"seraph-telegram:{owner}:{update_payload.chat_id}:{update_payload.update_id}").hex
                attachment_refs: list[dict[str, Any]] = []
                attachment_receipt_digest = None
                if update_payload.attachment is not None:
                    attachment = update_payload.attachment
                    try:
                        quarantine_receipt = issue_attachment_quarantine_receipt(
                            attachment_id=attachment.attachment_id,
                            owner_principal_id=owner,
                            content_hash=attachment.content_hash,
                            media_type=attachment.media_type,
                            size_bytes=attachment.size_bytes,
                            duration_seconds=attachment.duration_seconds,
                            voice_note=True,
                        )
                        attachment_refs = [{
                            "attachment_id": attachment.attachment_id,
                            "owner_principal_id": owner,
                            "content_hash": attachment.content_hash,
                            "media_type": attachment.media_type,
                            "size_bytes": attachment.size_bytes,
                            "duration_seconds": attachment.duration_seconds,
                            "voice_note": True,
                            "quarantine_status": "quarantined",
                            "quarantine_receipt": quarantine_receipt,
                        }]
                        # The canonical attachment projection stores the
                        # receipt's signed digest, never the bearer token (or
                        # a hash of the bearer token).
                        attachment_receipt_digest = quarantine_receipt.rsplit(".", 1)[-1]
                    except ConversationIdentityError as exc:
                        raise TelegramTransportError("attachment_quarantine_unavailable", str(exc)) from exc
                identity = build_conversation_identity(
                    conversation_id=session.id,
                    thread_id=session.id,
                    owner_principal_id=owner,
                    operator_session_id=operator_session,
                    device_id=f"telegram-chat:{update_payload.chat_id}",
                    channel="telegram",
                    transport="telegram",
                    correlation_id=f"telegram:{update_payload.update_id}",
                )
                lineage = build_lineage(identity, attachment_refs=attachment_refs, message_id=canonical_message_id)
                metadata = {
                    "lineage": lineage,
                    "telegram": {
                        "update_id": update_payload.update_id,
                        "message_id": update_payload.message_id,
                        "sequence": update_payload.sequence,
                        "request_digest": result.request_digest,
                        "voice_handoff": receipt.get("voice_handoff", {"status": "none"}),
                    },
                }
                content = update_payload.normalized_text or "[voice message quarantined for #751 handoff]"
                try:
                    canonical_message, duplicate = await session_manager.reserve_ingress_message(
                        session.id,
                        content,
                        message_id=canonical_message_id,
                        metadata_json=json.dumps(metadata, ensure_ascii=True, sort_keys=True),
                        attachment_refs=attachment_refs,
                    )
                except (MessageIngressConflictError, ConversationIdentityError) as exc:
                    raise TelegramTransportError("canonical_message_identity_conflict", str(exc)) from exc
                if duplicate:
                    existing_result = await db.execute(select(TelegramInboundUpdate).where(TelegramInboundUpdate.idempotency_key == result.idempotency_key))
                    existing = existing_result.scalar_one_or_none()
                    if existing is not None:
                        return json.loads(existing.receipt_json)
                receipt_payload = TelegramTransportReceipt(
                    status=result.status.value,
                    reason_code=result.reason_code,
                    idempotency_key=result.idempotency_key,
                    request_digest=result.request_digest,
                    session_id=session.id,
                    canonical_message_id=canonical_message.id,
                    retryable=result.retryable,
                    payload={
                        "owner_principal_id": owner,
                        "operator_session_id": operator_session,
                        "chat_id": update_payload.chat_id,
                        "canonical_channel": "telegram",
                        "canonical_transport": "telegram",
                        "attachment_quarantine": "quarantined" if update_payload.attachment else "none",
                        "voice_handoff": receipt.get("voice_handoff", {"status": "none"}),
                        "ingress_receipt": receipt,
                    },
                ).as_dict()
                db.add(TelegramInboundUpdate(
                    idempotency_key=result.idempotency_key,
                    request_digest=result.request_digest,
                    owner_principal_id=owner,
                    operator_session_id=operator_session,
                    operator_id=update_payload.operator_id,
                    chat_id=update_payload.chat_id,
                    update_id=update_payload.update_id,
                    message_id=update_payload.message_id,
                    sequence=update_payload.sequence,
                    session_id=session.id,
                    canonical_message_id=canonical_message.id,
                    content_digest=hashlib.sha256((update_payload.normalized_text or "").encode()).hexdigest(),
                    attachment_id=update_payload.attachment.attachment_id if update_payload.attachment else None,
                    attachment_hash=update_payload.attachment.content_hash if update_payload.attachment else None,
                    attachment_media_type=update_payload.attachment.media_type if update_payload.attachment else None,
                    attachment_size_bytes=update_payload.attachment.size_bytes if update_payload.attachment else None,
                    attachment_duration_seconds=update_payload.attachment.duration_seconds if update_payload.attachment else None,
                    attachment_quarantine_receipt_digest=attachment_receipt_digest,
                    status=result.status.value,
                    reason_code=result.reason_code,
                    receipt_json=json.dumps(receipt_payload, ensure_ascii=True, sort_keys=True),
                ))
                row.cursor = max(row.cursor, update_payload.update_id)
                row.sequence = max(row.sequence, next_state.last_sequence)
                row.rate_events_json = json.dumps([
                    {"idempotency_key": event.idempotency_key, "accepted_at": (_aware(event.accepted_at) or current).isoformat()}
                    for event in next_state.rate_events[-20:]
                ], sort_keys=True)
                row.updated_at = current
                db.add(row)
                await db.flush()
                return receipt_payload

    async def enqueue_outbound(
        self,
        content: str,
        *,
        owner_principal_id: str,
        operator_session_id: str,
        idempotency_key: str | None = None,
        chat_id: int | None = None,
        session_id: str | None = None,
        kind: str = "text",
        attachment_refs: object = None,
    ) -> dict[str, Any]:
        owner = _owner(owner_principal_id)
        operator_session = _session(operator_session_id)
        if not isinstance(content, str) or not content.strip() or len(content) > TELEGRAM_MAX_TEXT_CHARS:
            raise TelegramTransportError("invalid_text", "Telegram outbound text is invalid")
        if kind not in {"text", "voice"}:
            raise TelegramTransportError("invalid_kind", "Telegram outbound kind is invalid")
        async with self._lock:
            async with db_engine.get_session() as db:
                row = await self._state(db)
                if row is None or row.owner_principal_id != owner or row.operator_session_id != operator_session:
                    raise TelegramTransportError("telegram_authority_mismatch", "Telegram pairing belongs to another operator session")
                current = _now()
                self._assert_active_row(row, current=current)
                target_chat_id = chat_id if chat_id is not None else row.chat_id
                if target_chat_id != row.chat_id:
                    raise TelegramTransportError("telegram_identity_not_allowlisted", "Telegram chat is not paired")
                safe_refs = validate_attachment_refs(attachment_refs, owner_principal_id=owner) if kind == "voice" else []
                canonical_session_id = session_id or _session_id(owner, target_chat_id)
                if session_id is not None:
                    session = await session_manager.get(session_id, owner_principal_id=owner)
                    if session is None:
                        raise TelegramTransportError("conversation_session_not_found", "Canonical conversation is not available")
                digest_material = {
                    "owner": owner,
                    "operator_session": operator_session,
                    "chat_id": target_chat_id,
                    "session_id": canonical_session_id,
                    "kind": kind,
                    "content": content,
                    "attachment_refs": safe_refs,
                }
                digest = _sha(digest_material)
                key = str(idempotency_key or f"telegram:{digest}").strip()
                if len(key) > 256 or not key:
                    raise TelegramTransportError("invalid_idempotency_key", "Telegram idempotency key is invalid")
                existing_result = await db.execute(select(TelegramTransportOutbox).where(TelegramTransportOutbox.idempotency_key == key))
                existing = existing_result.scalar_one_or_none()
                if existing is not None:
                    if existing.payload_digest != digest:
                        raise TelegramTransportError("idempotency_conflict", "Telegram idempotency key is bound to another payload")
                    return self._outbox_payload(existing)
                if session_id is None:
                    canonical_session = await session_manager.get_or_create(
                        canonical_session_id,
                        owner_principal_id=owner,
                    )
                else:
                    canonical_session = session
                canonical_message_id = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"seraph-telegram-outbox:{owner}:{key}",
                ).hex
                identity = build_conversation_identity(
                    conversation_id=canonical_session.id,
                    thread_id=canonical_session.id,
                    owner_principal_id=owner,
                    operator_session_id=operator_session,
                    device_id=f"telegram-chat:{target_chat_id}",
                    channel="telegram",
                    transport="telegram",
                    correlation_id=f"telegram-outbox:{digest[:20]}",
                )
                metadata = {
                    "lineage": build_lineage(
                        identity,
                        attachment_refs=safe_refs,
                        message_id=canonical_message_id,
                    ),
                    "telegram": {
                        "outbox_idempotency_key": key,
                        "kind": kind,
                        "payload_digest": digest,
                    },
                }
                try:
                    await session_manager.add_message(
                        canonical_session.id,
                        "assistant",
                        content,
                        metadata_json=json.dumps(metadata, ensure_ascii=True, sort_keys=True),
                        message_id=canonical_message_id,
                        attachment_refs=safe_refs,
                    )
                except MessageIngressConflictError as exc:
                    raise TelegramTransportError("canonical_message_identity_conflict", str(exc)) from exc
                db.add(TelegramTransportOutbox(
                    idempotency_key=key,
                    payload_digest=digest,
                    owner_principal_id=owner,
                    operator_session_id=operator_session,
                    chat_id=target_chat_id,
                    session_id=canonical_session.id,
                    conversation_id=canonical_session.id,
                    thread_id=canonical_session.id,
                    message_id=canonical_message_id,
                    correlation_id=f"telegram-outbox:{digest[:20]}",
                    content=content,
                    content_digest=hashlib.sha256(content.encode()).hexdigest(),
                    kind=kind,
                    attachment_refs_json=json.dumps(safe_refs, sort_keys=True),
                    status="queued",
                    attempt_count=0,
                    max_attempts=self.max_attempts,
                    next_attempt_at=current,
                    created_at=current,
                    updated_at=current,
                ))
                await db.flush()
                result = await db.execute(select(TelegramTransportOutbox).where(TelegramTransportOutbox.idempotency_key == key))
                return self._outbox_payload(result.scalar_one())

    @staticmethod
    def _assert_active_row(row: TelegramTransportState, *, current: datetime) -> None:
        if row.pairing_state != "active":
            raise TelegramTransportError("telegram_pairing_not_active", "Telegram pairing is not active")
        expiry = _aware(row.pairing_expires_at)
        if expiry is not None and expiry <= current:
            row.pairing_state = "expired"
            raise TelegramTransportError("telegram_pairing_expired", "Telegram pairing has expired")
        if not row.transit_consent_reference or not _aware(row.transit_consent_expires_at) or _aware(row.transit_consent_expires_at) <= current:
            raise TelegramTransportError("consent_missing", "Telegram transit consent is required")

    @staticmethod
    def _outbox_payload(row: TelegramTransportOutbox) -> dict[str, Any]:
        return {
            "id": row.id,
            "idempotency_key": row.idempotency_key,
            "payload_digest": row.payload_digest,
            "owner_principal_id": row.owner_principal_id,
            "operator_session_id": row.operator_session_id,
            "chat_id": row.chat_id,
            "session_id": row.session_id,
            "conversation_id": row.conversation_id,
            "thread_id": row.thread_id,
            "kind": row.kind,
            "content_digest": row.content_digest,
            "status": row.status,
            "attempt_count": row.attempt_count,
            "max_attempts": row.max_attempts,
            "last_error": row.last_error,
            "response_code": row.response_code,
            "external_message_id": row.external_message_id,
            "created_at": (_aware(row.created_at) or _now()).isoformat(),
            "updated_at": (_aware(row.updated_at) or _now()).isoformat(),
            "delivered_at": (_aware(row.delivered_at).isoformat() if row.delivered_at else None),
        }

    @staticmethod
    def _response(response: object) -> tuple[int, str | None]:
        if isinstance(response, dict):
            code = response.get("status_code", response.get("status", 200))
            try:
                code_int = int(code)
            except (TypeError, ValueError):
                code_int = 502
            message_id = response.get("message_id")
            return code_int, str(message_id) if message_id is not None else None
        return 502, None

    async def deliver(self, outbox_id: str, *, owner_principal_id: str, operator_session_id: str) -> dict[str, Any]:
        owner = _owner(owner_principal_id)
        operator_session = _session(operator_session_id)
        async with self._lock:
            async with db_engine.get_session() as db:
                result = await db.execute(
                    select(TelegramTransportOutbox).where(
                        TelegramTransportOutbox.id == str(outbox_id),
                        TelegramTransportOutbox.owner_principal_id == owner,
                        TelegramTransportOutbox.operator_session_id == operator_session,
                    )
                )
                row = result.scalar_one_or_none()
                if row is None:
                    raise TelegramTransportError("telegram_outbox_not_found", "Telegram outbox item is not available")
                if row.status == "delivered":
                    return self._outbox_payload(row)
                if row.status in {"cancelled", "failed"}:
                    return self._outbox_payload(row)
                current = _now()
                if row.attempt_count >= row.max_attempts:
                    row.status = "failed"
                    row.last_error = "telegram_delivery_attempts_exhausted"
                    row.updated_at = current
                    db.add(row)
                    return self._outbox_payload(row)
                state = await self._state(db)
                if state is None or state.owner_principal_id != owner or state.operator_session_id != operator_session:
                    raise TelegramTransportError("telegram_authority_mismatch", "Telegram pairing belongs to another operator session")
                self._assert_active_row(state, current=current)
                # The final pairing/consent check is immediately before the
                # injected callback.  The local lock makes revoke and send
                # mutually exclusive in one backend process.
                token = await vault_repository.get(state.token_secret_ref or "")
                if not token:
                    raise TelegramTransportError("telegram_token_unavailable", "Scoped Telegram token is unavailable")
                row.attempt_count += 1
                attempt_index = row.attempt_count
                row.status = "sending"
                row.updated_at = current
                attempt = TelegramDeliveryAttempt(
                    outbox_id=row.id,
                    attempt_index=attempt_index,
                    status="started",
                    started_at=current,
                )
                db.add(row)
                db.add(attempt)
                await db.flush()
                content = row.content
                refs = json.loads(row.attachment_refs_json or "[]")
                key = row.idempotency_key
                kind = row.kind
            # Never hold a database transaction across injected user code.
            try:
                if kind == "voice":
                    response = await self.transport.send_voice(
                        token=token,
                        chat_id=row.chat_id,
                        attachment=refs[0] if refs else {},
                        caption=content,
                        idempotency_key=key,
                    )
                else:
                    response = await self.transport.send_message(
                        token=token,
                        chat_id=row.chat_id,
                        text=content,
                        idempotency_key=key,
                    )
                response_code, external_message_id = self._response(response)
                error = None
            except (TimeoutError, asyncio.TimeoutError):
                response_code, external_message_id, error = None, None, "telegram_delivery_timeout"
            except Exception as exc:
                response_code, external_message_id, error = None, None, f"telegram_delivery_exception:{type(exc).__name__}"
            async with db_engine.get_session() as db:
                current = _now()
                state = await self._state(db)
                result = await db.execute(select(TelegramTransportOutbox).where(TelegramTransportOutbox.id == row.id))
                fresh = result.scalar_one_or_none()
                attempt_result = await db.execute(
                    select(TelegramDeliveryAttempt).where(
                        TelegramDeliveryAttempt.outbox_id == row.id,
                        TelegramDeliveryAttempt.attempt_index == attempt_index,
                    )
                )
                attempt_row = attempt_result.scalar_one_or_none()
                if fresh is None or attempt_row is None:
                    raise TelegramTransportError("telegram_delivery_state_lost", "Telegram delivery receipt could not be persisted")
                revoked = state is None or state.pairing_state != "active" or state.owner_principal_id != owner or state.operator_session_id != operator_session
                if revoked:
                    fresh.status = "unknown"
                    fresh.last_error = "telegram_pairing_revoked_during_delivery"
                    receipt_status = "unknown"
                    reason = "telegram_pairing_revoked_during_delivery"
                    retryable = False
                elif error:
                    fresh.status = "unknown"
                    fresh.last_error = error
                    receipt_status = "unknown"
                    reason = error
                    retryable = attempt_index < fresh.max_attempts
                elif response_code is not None and 200 <= response_code < 300:
                    fresh.status = "delivered"
                    fresh.external_message_id = external_message_id
                    fresh.delivered_at = current
                    fresh.last_error = None
                    receipt_status = "delivered"
                    reason = "telegram_delivery_accepted"
                    retryable = False
                elif response_code in {401, 403}:
                    fresh.status = "failed"
                    fresh.last_error = "telegram_transport_unauthorized"
                    receipt_status = "failed"
                    reason = "telegram_transport_unauthorized"
                    retryable = False
                elif response_code == 429 or (response_code is not None and response_code >= 500):
                    if attempt_index < fresh.max_attempts:
                        fresh.status = "queued"
                        fresh.next_attempt_at = current + timedelta(seconds=min(60, 2 ** (attempt_index - 1)))
                        fresh.last_error = f"telegram_transport_http_{response_code}"
                        receipt_status = "queued"
                        reason = "telegram_transport_retryable"
                        retryable = True
                    else:
                        fresh.status = "failed"
                        fresh.last_error = f"telegram_transport_http_{response_code}"
                        receipt_status = "failed"
                        reason = "telegram_transport_attempts_exhausted"
                        retryable = False
                else:
                    fresh.status = "failed"
                    fresh.last_error = f"telegram_transport_http_{response_code or 502}"
                    receipt_status = "failed"
                    reason = "telegram_transport_terminal"
                    retryable = False
                fresh.response_code = response_code
                fresh.updated_at = current
                attempt_row.status = receipt_status
                attempt_row.response_code = response_code
                attempt_row.error_code = reason if receipt_status != "delivered" else None
                attempt_row.finished_at = current
                db.add(fresh)
                db.add(attempt_row)
                await db.flush()
                payload = self._outbox_payload(fresh)
                payload.update({"reason_code": reason, "retryable": retryable, "delivery_attempt": attempt_index})
                return payload

    async def list_outbox(self, *, owner_principal_id: str, operator_session_id: str, limit: int = 100) -> list[dict[str, Any]]:
        owner = _owner(owner_principal_id)
        operator_session = _session(operator_session_id)
        if limit < 1 or limit > 100:
            raise TelegramTransportError("invalid_limit", "Telegram outbox limit is outside the bound")
        async with db_engine.get_session() as db:
            result = await db.execute(
                select(TelegramTransportOutbox)
                .where(TelegramTransportOutbox.owner_principal_id == owner)
                .where(TelegramTransportOutbox.operator_session_id == operator_session)
                .order_by(TelegramTransportOutbox.created_at.desc())
                .limit(limit)
            )
            return [self._outbox_payload(row) for row in result.scalars().all()]


default_telegram_transport = TelegramTransportAdapter()


__all__ = [
    "InjectedTelegramTransport",
    "RecordingTelegramTransport",
    "TelegramDeliveryAttempt",
    "TelegramTransportAdapter",
    "TelegramTransportError",
    "TelegramTransportReceipt",
    "default_telegram_transport",
]
