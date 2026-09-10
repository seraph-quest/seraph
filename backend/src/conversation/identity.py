"""Server-owned identity and redaction primitives for every conversation surface.

The ``Session.id`` row is Seraph's canonical conversation identity.  Browser,
WebSocket, native notification, and adapter payloads carry the same identity
and thread id; a surface may add transport metadata but cannot mint another
conversation.  Attachment references are reduced to safe metadata before they
reach transcript, approval, or outbox persistence.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


CONVERSATION_SCHEMA_VERSION = "seraph.conversation.v1"
MAX_IDENTITY_CHARS = 256
MAX_ATTACHMENT_REFS = 32
MAX_ATTACHMENT_ID_CHARS = 256
MAX_HASH_CHARS = 256
MAX_ATTACHMENT_RECEIPT_CHARS = 4096
ATTACHMENT_RECEIPT_SCHEMA_VERSION = "seraph.attachment-quarantine.v1"
ATTACHMENT_RECEIPT_PREFIX = "seraph-attachment-v1"
ATTACHMENT_RECEIPT_MAX_TTL = timedelta(days=1)
ATTACHMENT_RECEIPT_CLOCK_SKEW = timedelta(minutes=5)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_RECEIPT_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")

# These are the only transport labels currently owned by Seraph.  Adapter
# modules can use their own transport label only after it is added here, which
# makes an accidental caller-supplied channel fail closed.
KNOWN_CHANNELS = frozenset({"web", "native_notification", "telegram", "macos"})
KNOWN_TRANSPORTS = frozenset({"rest", "websocket", "native_notification", "telegram", "macos"})


class ConversationIdentityError(ValueError):
    """Raised when a surface tries to change server-owned lineage."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _identifier(value: object, *, field: str, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ConversationIdentityError(
                f"conversation_{field}_missing",
                f"Conversation {field} is required.",
            )
        return None
    if not isinstance(value, str):
        raise ConversationIdentityError(
            f"conversation_{field}_invalid",
            f"Conversation {field} must be a string.",
        )
    normalized = value.strip()
    if not normalized:
        if required:
            raise ConversationIdentityError(
                f"conversation_{field}_missing",
                f"Conversation {field} is required.",
            )
        return None
    if len(normalized) > MAX_IDENTITY_CHARS or _CONTROL_CHARS.search(normalized):
        raise ConversationIdentityError(
            f"conversation_{field}_invalid",
            f"Conversation {field} is invalid.",
        )
    return normalized


@dataclass(frozen=True, slots=True)
class ConversationIdentity:
    """Immutable canonical binding shared by all ingress and delivery edges."""

    conversation_id: str
    thread_id: str
    owner_principal_id: str
    operator_session_id: str | None
    device_id: str
    channel: str
    transport: str
    correlation_id: str
    causation_id: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


def build_conversation_identity(
    *,
    conversation_id: object,
    owner_principal_id: object,
    operator_session_id: object = None,
    device_id: object = None,
    channel: object = "web",
    transport: object = "rest",
    thread_id: object = None,
    correlation_id: object = None,
    causation_id: object = None,
    require_owner: bool = True,
) -> ConversationIdentity:
    """Validate one server-owned identity binding.

    ``thread_id`` defaults to the canonical session id.  If a caller supplies a
    different thread it is rejected instead of creating a second continuity
    namespace.  ``require_owner=False`` is reserved for ambient notifications
    without a conversation; bound conversations always carry an owner.
    """

    conversation = _identifier(conversation_id, field="id", required=require_owner)
    owner = _identifier(owner_principal_id, field="owner", required=require_owner)
    thread = _identifier(thread_id, field="thread", required=False)
    operator_session = _identifier(operator_session_id, field="operator_session", required=False)
    device = _identifier(device_id, field="device", required=False)
    channel_value = _identifier(channel, field="channel", required=True)
    transport_value = _identifier(transport, field="transport", required=True)
    correlation = _identifier(correlation_id, field="correlation", required=False)
    causation = _identifier(causation_id, field="causation", required=False)

    if channel_value not in KNOWN_CHANNELS:
        raise ConversationIdentityError(
            "conversation_channel_invalid",
            f"Conversation channel '{channel_value}' is not registered.",
        )
    if transport_value not in KNOWN_TRANSPORTS:
        raise ConversationIdentityError(
            "conversation_transport_invalid",
            f"Conversation transport '{transport_value}' is not registered.",
        )
    if conversation is not None:
        if thread is None:
            thread = conversation
        elif thread != conversation:
            raise ConversationIdentityError(
                "conversation_thread_mismatch",
                "Thread identity must equal the canonical conversation id.",
            )
    elif thread is not None:
        raise ConversationIdentityError(
            "conversation_thread_without_conversation",
            "A thread cannot be bound without a canonical conversation.",
        )
    if owner is None:
        owner = "ambient"
    if device is None:
        device = f"{channel_value}-operator-session:{operator_session or 'ambient'}"
    if correlation is None:
        correlation = f"conversation:{conversation or 'ambient'}"
    assert conversation is not None or not require_owner
    assert thread is not None or conversation is None
    return ConversationIdentity(
        conversation_id=conversation or "",
        thread_id=thread or "",
        owner_principal_id=owner,
        operator_session_id=operator_session,
        device_id=device,
        channel=channel_value,
        transport=transport_value,
        correlation_id=correlation,
        causation_id=causation,
    )


def _object_mapping(value: object) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    # Telegram/native adapter dataclasses intentionally expose metadata fields
    # while keeping their raw file reference private.  Read only the known
    # public fields and never call arbitrary serializers here.
    if value is not None and is_dataclass(value):
        return {
            field.name: getattr(value, field.name)
            for field in fields(value)
            if field.name in {
                "attachment_id",
                "owner_principal_id",
                "media_type",
                "size_bytes",
                "content_hash",
                "voice_note",
                "duration_seconds",
                "quarantine_status",
                "status",
                "quarantine_receipt",
                "attachment_receipt",
                "quarantine_receipt_digest",
                "quarantine_receipt_issued_at",
                "quarantine_receipt_expires_at",
            }
        }
    if value is not None and hasattr(value, "__dict__"):
        return {
            key: getattr(value, key)
            for key in (
                "attachment_id",
                "owner_principal_id",
                "media_type",
                "size_bytes",
                "content_hash",
                "voice_note",
                "duration_seconds",
                "quarantine_status",
                "status",
                "quarantine_receipt",
                "attachment_receipt",
                "quarantine_receipt_digest",
                "quarantine_receipt_issued_at",
                "quarantine_receipt_expires_at",
            )
            if hasattr(value, key)
        }
    return None


def _safe_attachment_text(value: object, *, field: str, max_chars: int = MAX_ATTACHMENT_ID_CHARS) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConversationIdentityError("attachment_reference_invalid", f"Attachment {field} is invalid.")
    normalized = value.strip()
    if not normalized or len(normalized) > max_chars or _CONTROL_CHARS.search(normalized):
        raise ConversationIdentityError("attachment_reference_invalid", f"Attachment {field} is invalid.")
    return normalized


def _attachment_receipt_key() -> bytes:
    """Return a process-local HMAC key derived from the configured server secret.

    The operator credential is intentionally read only at verification time and
    never included in a receipt or log.  A missing credential makes attachment
    handoff unavailable instead of allowing caller metadata to act as proof.
    """

    from config.settings import settings

    configured = str(settings.operator_auth_secret or settings.operator_auth_secret_hash or "").strip()
    if not configured:
        raise ConversationIdentityError(
            "attachment_receipt_unavailable",
            "A configured server secret is required for attachment quarantine receipts.",
        )
    return hashlib.sha256(configured.encode("utf-8")).digest()


def _receipt_datetime(value: object, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConversationIdentityError(
                "attachment_receipt_invalid",
                f"Attachment receipt {field} is invalid.",
            ) from exc
    else:
        raise ConversationIdentityError(
            "attachment_receipt_invalid",
            f"Attachment receipt {field} is invalid.",
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ConversationIdentityError(
            "attachment_receipt_invalid",
            f"Attachment receipt {field} must include a timezone.",
        )
    return parsed.astimezone(timezone.utc)


def _receipt_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _attachment_receipt_metadata(
    *,
    attachment_id: object,
    owner_principal_id: object,
    content_hash: object,
    media_type: object = None,
    size_bytes: object = None,
    duration_seconds: object = None,
    voice_note: object = None,
) -> dict[str, Any]:
    normalized_id = _safe_attachment_text(attachment_id, field="id")
    normalized_owner = _safe_attachment_text(owner_principal_id, field="owner_principal_id")
    normalized_hash = _safe_attachment_text(content_hash, field="content_hash", max_chars=MAX_HASH_CHARS)
    if normalized_id is None or normalized_owner is None or normalized_hash is None:
        raise ConversationIdentityError(
            "attachment_receipt_invalid",
            "Attachment receipts require an id, owner, and content hash.",
        )
    payload: dict[str, Any] = {
        "schema_version": ATTACHMENT_RECEIPT_SCHEMA_VERSION,
        "attachment_id": normalized_id,
        "owner_principal_id": normalized_owner,
        "content_hash": normalized_hash,
        "quarantine_status": "quarantined",
    }
    normalized_media_type = _safe_attachment_text(media_type, field="media_type", max_chars=128)
    if normalized_media_type is not None:
        payload["media_type"] = normalized_media_type
    if size_bytes is not None:
        try:
            normalized_size = int(size_bytes)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ConversationIdentityError(
                "attachment_receipt_invalid",
                "Attachment receipt size_bytes is invalid.",
            ) from exc
        if normalized_size < 0:
            raise ConversationIdentityError(
                "attachment_receipt_invalid",
                "Attachment receipt size_bytes is invalid.",
            )
        payload["size_bytes"] = normalized_size
    if duration_seconds is not None:
        try:
            normalized_duration = float(duration_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ConversationIdentityError(
                "attachment_receipt_invalid",
                "Attachment receipt duration_seconds is invalid.",
            ) from exc
        if normalized_duration < 0:
            raise ConversationIdentityError(
                "attachment_receipt_invalid",
                "Attachment receipt duration_seconds is invalid.",
            )
        payload["duration_seconds"] = normalized_duration
    if voice_note is not None:
        if not isinstance(voice_note, bool):
            raise ConversationIdentityError(
                "attachment_receipt_invalid",
                "Attachment receipt voice_note is invalid.",
            )
        payload["voice_note"] = voice_note
    return payload


def issue_attachment_quarantine_receipt(
    *,
    attachment_id: object,
    owner_principal_id: object,
    content_hash: object,
    media_type: object = None,
    size_bytes: object = None,
    duration_seconds: object = None,
    voice_note: object = None,
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> str:
    """Issue a short-lived server-signed quarantine handoff receipt.

    Ingress adapters call this only after they have quarantined the bytes and
    computed ``content_hash``.  Downstream callers receive the bearer receipt,
    while persistence keeps only its digest and signed metadata.
    """

    issued = _receipt_datetime(issued_at or datetime.now(timezone.utc), field="issued_at")
    expires = _receipt_datetime(
        expires_at or issued + timedelta(hours=1),
        field="expires_at",
    )
    if expires <= issued or expires - issued > ATTACHMENT_RECEIPT_MAX_TTL:
        raise ConversationIdentityError(
            "attachment_receipt_invalid",
            "Attachment receipt expiry is outside the allowed lifetime.",
        )
    now = datetime.now(timezone.utc)
    if issued > now + ATTACHMENT_RECEIPT_CLOCK_SKEW:
        raise ConversationIdentityError(
            "attachment_receipt_invalid",
            "Attachment receipt issue time is in the future.",
        )
    payload = _attachment_receipt_metadata(
        attachment_id=attachment_id,
        owner_principal_id=owner_principal_id,
        content_hash=content_hash,
        media_type=media_type,
        size_bytes=size_bytes,
        duration_seconds=duration_seconds,
        voice_note=voice_note,
    )
    payload.update({"issued_at": _receipt_iso(issued), "expires_at": _receipt_iso(expires)})
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii").rstrip("=")
    signature = hmac.new(_attachment_receipt_key(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    token = f"{ATTACHMENT_RECEIPT_PREFIX}.{encoded}.{signature}"
    if len(token) > MAX_ATTACHMENT_RECEIPT_CHARS:
        raise ConversationIdentityError(
            "attachment_receipt_invalid",
            "Attachment receipt exceeds the size limit.",
        )
    return token


def _decode_attachment_quarantine_receipt(
    token: object,
    *,
    expected_owner_principal_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(token, str) or not token.strip() or len(token) > MAX_ATTACHMENT_RECEIPT_CHARS:
        raise ConversationIdentityError(
            "attachment_receipt_invalid",
            "Attachment quarantine receipt is invalid.",
        )
    parts = token.strip().split(".")
    if len(parts) != 3 or parts[0] != ATTACHMENT_RECEIPT_PREFIX:
        raise ConversationIdentityError(
            "attachment_receipt_invalid",
            "Attachment quarantine receipt is invalid.",
        )
    _, encoded, supplied_signature = parts
    if not encoded or not _RECEIPT_SEGMENT.fullmatch(encoded) or not re.fullmatch(r"[0-9a-f]{64}", supplied_signature):
        raise ConversationIdentityError(
            "attachment_receipt_invalid",
            "Attachment quarantine receipt is invalid.",
        )
    expected_signature = hmac.new(
        _attachment_receipt_key(),
        encoded.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise ConversationIdentityError(
            "attachment_receipt_invalid",
            "Attachment quarantine receipt signature is invalid.",
        )
    try:
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConversationIdentityError(
            "attachment_receipt_invalid",
            "Attachment quarantine receipt payload is invalid.",
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != ATTACHMENT_RECEIPT_SCHEMA_VERSION:
        raise ConversationIdentityError(
            "attachment_receipt_invalid",
            "Attachment quarantine receipt schema is invalid.",
        )
    metadata = _attachment_receipt_metadata(
        attachment_id=payload.get("attachment_id"),
        owner_principal_id=payload.get("owner_principal_id"),
        content_hash=payload.get("content_hash"),
        media_type=payload.get("media_type"),
        size_bytes=payload.get("size_bytes"),
        duration_seconds=payload.get("duration_seconds"),
        voice_note=payload.get("voice_note"),
    )
    if payload.get("quarantine_status") != "quarantined":
        raise ConversationIdentityError(
            "attachment_quarantine_required",
            "Attachment delivery requires a quarantined receipt.",
        )
    issued = _receipt_datetime(payload.get("issued_at"), field="issued_at")
    expires = _receipt_datetime(payload.get("expires_at"), field="expires_at")
    if expires <= issued or expires - issued > ATTACHMENT_RECEIPT_MAX_TTL:
        raise ConversationIdentityError(
            "attachment_receipt_invalid",
            "Attachment receipt expiry is outside the allowed lifetime.",
        )
    current = _receipt_datetime(now or datetime.now(timezone.utc), field="now")
    if issued > current + ATTACHMENT_RECEIPT_CLOCK_SKEW:
        raise ConversationIdentityError(
            "attachment_receipt_invalid",
            "Attachment receipt issue time is in the future.",
        )
    if expires <= current:
        raise ConversationIdentityError(
            "attachment_receipt_expired",
            "Attachment quarantine receipt has expired.",
        )
    expected_owner = _safe_attachment_text(
        expected_owner_principal_id,
        field="owner_principal_id",
    )
    if expected_owner is not None and metadata["owner_principal_id"] != expected_owner:
        raise ConversationIdentityError(
            "attachment_owner_mismatch",
            "Attachment ownership does not match the authenticated principal.",
        )
    metadata.update(
        {
            "issued_at": _receipt_iso(issued),
            "expires_at": _receipt_iso(expires),
            "quarantine_receipt_digest": supplied_signature,
        }
    )
    return metadata


def _compare_attachment_field(item: Mapping[str, Any], field: str, expected: object) -> None:
    if field not in item or item.get(field) is None:
        return
    if field in {"attachment_id", "owner_principal_id", "content_hash", "media_type"}:
        actual = _safe_attachment_text(item.get(field), field=field, max_chars=MAX_HASH_CHARS)
    elif field == "size_bytes":
        try:
            actual = int(item.get(field))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ConversationIdentityError("attachment_reference_invalid", f"Attachment {field} is invalid.") from exc
    elif field == "duration_seconds":
        try:
            actual = float(item.get(field))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ConversationIdentityError("attachment_reference_invalid", f"Attachment {field} is invalid.") from exc
    else:
        actual = item.get(field)
    if actual != expected:
        raise ConversationIdentityError(
            "attachment_reference_conflict",
            f"Attachment {field} does not match its quarantine receipt.",
        )


def _canonical_attachment_ref(
    raw: object,
    *,
    owner_principal_id: str | None = None,
) -> dict[str, Any]:
    item = _object_mapping(raw)
    if item is None:
        raise ConversationIdentityError(
            "attachment_reference_unknown",
            "Attachment references must come from the quarantined ingress adapter.",
        )
    token = item.get("quarantine_receipt", item.get("attachment_receipt"))
    if token is not None:
        metadata = _decode_attachment_quarantine_receipt(
            token,
            expected_owner_principal_id=owner_principal_id,
        )
    else:
        # Persisted lineage deliberately removes the bearer token. It can be
        # revalidated only when the signed digest and its bounded timestamps
        # are still present; id/status/hash alone never qualify as proof.
        digest = _safe_attachment_text(
            item.get("quarantine_receipt_digest"),
            field="quarantine_receipt_digest",
            max_chars=64,
        )
        issued = item.get("quarantine_receipt_issued_at")
        expires = item.get("quarantine_receipt_expires_at")
        if digest is None or issued is None or expires is None:
            raise ConversationIdentityError(
                "attachment_receipt_required",
                "Attachment delivery requires a server-issued quarantine receipt.",
            )
        issued_at = _receipt_datetime(issued, field="issued_at")
        expires_at = _receipt_datetime(expires, field="expires_at")
        if expires_at <= issued_at or expires_at - issued_at > ATTACHMENT_RECEIPT_MAX_TTL:
            raise ConversationIdentityError(
                "attachment_receipt_invalid",
                "Attachment receipt expiry is outside the allowed lifetime.",
            )
        current = datetime.now(timezone.utc)
        if issued_at > current + ATTACHMENT_RECEIPT_CLOCK_SKEW:
            raise ConversationIdentityError(
                "attachment_receipt_invalid",
                "Attachment receipt issue time is in the future.",
            )
        if expires_at <= current:
            raise ConversationIdentityError(
                "attachment_receipt_expired",
                "Attachment quarantine receipt has expired.",
            )
        payload = _attachment_receipt_metadata(
            attachment_id=item.get("attachment_id", item.get("id")),
            owner_principal_id=item.get("owner_principal_id") or owner_principal_id,
            content_hash=item.get("content_hash"),
            media_type=item.get("media_type"),
            size_bytes=item.get("size_bytes"),
            duration_seconds=item.get("duration_seconds"),
            voice_note=item.get("voice_note"),
        )
        payload.update({"issued_at": _receipt_iso(issued_at), "expires_at": _receipt_iso(expires_at)})
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).decode("ascii").rstrip("=")
        expected_digest = hmac.new(_attachment_receipt_key(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(digest, expected_digest):
            raise ConversationIdentityError(
                "attachment_receipt_invalid",
                "Attachment quarantine receipt signature is invalid.",
            )
        metadata = {**payload, "quarantine_receipt_digest": digest}
        expected_owner = _safe_attachment_text(owner_principal_id, field="owner_principal_id")
        if expected_owner is not None and metadata["owner_principal_id"] != expected_owner:
            raise ConversationIdentityError(
                "attachment_owner_mismatch",
                "Attachment ownership does not match the authenticated principal.",
            )
    _compare_attachment_field(item, "attachment_id", metadata["attachment_id"])
    _compare_attachment_field(item, "owner_principal_id", metadata["owner_principal_id"])
    _compare_attachment_field(item, "content_hash", metadata["content_hash"])
    _compare_attachment_field(item, "media_type", metadata.get("media_type"))
    _compare_attachment_field(item, "size_bytes", metadata.get("size_bytes"))
    _compare_attachment_field(item, "duration_seconds", metadata.get("duration_seconds"))
    if "voice_note" in item and item.get("voice_note") is not None and item.get("voice_note") != metadata.get("voice_note"):
        raise ConversationIdentityError(
            "attachment_reference_conflict",
            "Attachment voice_note does not match its quarantine receipt.",
        )
    supplied_status = item.get("quarantine_status", item.get("status"))
    if supplied_status is not None and supplied_status != "quarantined":
        raise ConversationIdentityError(
            "attachment_quarantine_required",
            "Attachment delivery requires a quarantined receipt.",
        )
    safe = {
        "attachment_id": metadata["attachment_id"],
        "owner_principal_id": metadata["owner_principal_id"],
        "content_hash": metadata["content_hash"],
        "quarantine_status": "quarantined",
        "quarantine_receipt_digest": metadata["quarantine_receipt_digest"],
        "quarantine_receipt_issued_at": metadata["issued_at"],
        "quarantine_receipt_expires_at": metadata["expires_at"],
    }
    for field in ("media_type", "size_bytes", "duration_seconds", "voice_note"):
        if field in metadata:
            safe[field] = metadata[field]
    return safe


def redact_attachment_refs(
    value: object,
    *,
    owner_principal_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return durable attachment metadata without file paths, URLs, or tokens.

    Unknown keys are intentionally dropped.  This protects transcript,
    approval, and native outbox rows even when an adapter receives a private
    file handle or provider token alongside its public attachment metadata.
    """

    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ConversationIdentityError(
            "attachment_reference_invalid",
            "Attachment references must be a list.",
        )
    if len(value) > MAX_ATTACHMENT_REFS:
        raise ConversationIdentityError(
            "attachment_reference_limit",
            f"At most {MAX_ATTACHMENT_REFS} attachment references are allowed.",
        )

    redacted: list[dict[str, Any]] = []
    for raw in value:
        item = _object_mapping(raw)
        if item is None:
            raise ConversationIdentityError(
                "attachment_reference_invalid",
                "Each attachment reference must be an object.",
            )
        attachment_owner = _safe_attachment_text(
            item.get("owner_principal_id"),
            field="owner_principal_id",
        )
        expected_owner = _safe_attachment_text(
            owner_principal_id,
            field="owner_principal_id",
        )
        if (
            attachment_owner is not None
            and expected_owner is not None
            and attachment_owner != expected_owner
        ):
            raise ConversationIdentityError(
                "attachment_owner_mismatch",
                "Attachment ownership does not match the authenticated principal.",
            )
        attachment_id = _safe_attachment_text(
            item.get("attachment_id", item.get("id")),
            field="id",
        )
        media_type = _safe_attachment_text(item.get("media_type"), field="media_type", max_chars=128)
        content_hash = _safe_attachment_text(item.get("content_hash"), field="content_hash", max_chars=MAX_HASH_CHARS)
        safe: dict[str, Any] = {}
        if attachment_id is not None:
            safe["attachment_id"] = attachment_id
        if media_type is not None:
            safe["media_type"] = media_type
        if content_hash is not None:
            safe["content_hash"] = content_hash
        if "size_bytes" in item and item.get("size_bytes") is not None:
            try:
                size_bytes = int(item["size_bytes"])
            except (TypeError, ValueError, OverflowError) as exc:
                raise ConversationIdentityError(
                    "attachment_reference_invalid",
                    "Attachment size_bytes is invalid.",
                ) from exc
            if size_bytes < 0:
                raise ConversationIdentityError(
                    "attachment_reference_invalid",
                    "Attachment size_bytes is invalid.",
                )
            safe["size_bytes"] = size_bytes
        if "duration_seconds" in item and item.get("duration_seconds") is not None:
            try:
                duration = float(item["duration_seconds"])
            except (TypeError, ValueError, OverflowError) as exc:
                raise ConversationIdentityError(
                    "attachment_reference_invalid",
                    "Attachment duration_seconds is invalid.",
                ) from exc
            if duration < 0:
                raise ConversationIdentityError(
                    "attachment_reference_invalid",
                    "Attachment duration_seconds is invalid.",
                )
            safe["duration_seconds"] = duration
        if "voice_note" in item and item.get("voice_note") is not None:
            safe["voice_note"] = bool(item["voice_note"])
        quarantine_status = _safe_attachment_text(
            item.get("quarantine_status", item.get("status")),
            field="quarantine_status",
            max_chars=64,
        )
        if quarantine_status is not None:
            safe["quarantine_status"] = quarantine_status
        if attachment_owner is not None:
            safe["owner_principal_id"] = attachment_owner
        for proof_field in (
            "quarantine_receipt_digest",
            "quarantine_receipt_issued_at",
            "quarantine_receipt_expires_at",
        ):
            proof_value = _safe_attachment_text(
                item.get(proof_field),
                field=proof_field,
                max_chars=64 if proof_field.endswith("digest") else MAX_IDENTITY_CHARS,
            )
            if proof_value is not None:
                safe[proof_field] = proof_value
        redacted.append(safe)
    return redacted


def validate_attachment_refs(
    value: object,
    *,
    owner_principal_id: str | None = None,
) -> list[dict[str, Any]]:
    """Validate an external attachment handoff before it enters Seraph.

    Ingress adapters issue a short-lived HMAC receipt after quarantine. The
    receipt binds the attachment id, owner, hash, status, and safe metadata;
    downstream persistence accepts only that proof (or its revalidated digest).
    """
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ConversationIdentityError(
            "attachment_reference_invalid",
            "Attachment references must be a list.",
        )
    if len(value) > MAX_ATTACHMENT_REFS:
        raise ConversationIdentityError(
            "attachment_reference_limit",
            f"At most {MAX_ATTACHMENT_REFS} attachment references are allowed.",
        )
    safe_refs: list[dict[str, Any]] = []
    for raw in value:
        safe_refs.append(
            _canonical_attachment_ref(
                raw,
                owner_principal_id=owner_principal_id,
            )
        )
    return safe_refs


def build_lineage(
    identity: ConversationIdentity,
    *,
    attachment_refs: object = None,
    message_id: str | None = None,
    approval_id: str | None = None,
    delivery_attempt: int | None = None,
    degraded_state: str | None = None,
) -> dict[str, Any]:
    """Build the JSON-safe lineage payload shared by all surfaces."""

    payload: dict[str, Any] = {
        "schema_version": CONVERSATION_SCHEMA_VERSION,
        **identity.to_dict(),
        "attachment_refs": validate_attachment_refs(
            attachment_refs,
            owner_principal_id=(
                identity.owner_principal_id
                if identity.owner_principal_id != "ambient"
                else None
            ),
        ),
    }
    if message_id:
        payload["message_id"] = message_id
    if approval_id:
        payload["approval_id"] = approval_id
    if delivery_attempt is not None:
        payload["delivery_attempt"] = delivery_attempt
    if degraded_state:
        payload["degraded_state"] = degraded_state
    return payload


def lineage_json(identity: ConversationIdentity, **kwargs: Any) -> str:
    """Serialize lineage deterministically for existing metadata columns."""

    return json.dumps(build_lineage(identity, **kwargs), ensure_ascii=True, sort_keys=True)
