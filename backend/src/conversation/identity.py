"""Server-owned identity and redaction primitives for every conversation surface.

The ``Session.id`` row is Seraph's canonical conversation identity.  Browser,
WebSocket, native notification, and adapter payloads carry the same identity
and thread id; a surface may add transport metadata but cannot mint another
conversation.  Attachment references are reduced to safe metadata before they
reach transcript, approval, or outbox persistence.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping


CONVERSATION_SCHEMA_VERSION = "seraph.conversation.v1"
MAX_IDENTITY_CHARS = 256
MAX_ATTACHMENT_REFS = 32
MAX_ATTACHMENT_ID_CHARS = 256
MAX_HASH_CHARS = 256
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

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
        redacted.append(safe)
    return redacted


def validate_attachment_refs(
    value: object,
    *,
    owner_principal_id: str | None = None,
) -> list[dict[str, Any]]:
    """Validate an external attachment handoff before it enters Seraph.

    A generic attachment registry is not present on this branch. The existing
    Telegram ingress quarantine contract is therefore the authority seam:
    unknown references, missing content hashes, and non-quarantined files are
    rejected before transcript, approval, or delivery metadata is created.
    """
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ConversationIdentityError(
            "attachment_reference_invalid",
            "Attachment references must be a list.",
        )
    for raw in value:
        item = _object_mapping(raw)
        if item is None:
            raise ConversationIdentityError(
                "attachment_reference_unknown",
                "Attachment references must come from the quarantined ingress adapter.",
            )
        attachment_id = _safe_attachment_text(
            item.get("attachment_id", item.get("id")),
            field="id",
        )
        if attachment_id is None:
            raise ConversationIdentityError(
                "attachment_reference_unknown",
                "An attachment registry id is required before delivery.",
            )
        quarantine_status = _safe_attachment_text(
            item.get("quarantine_status", item.get("status")),
            field="quarantine_status",
            max_chars=64,
        )
        if quarantine_status != "quarantined":
            raise ConversationIdentityError(
                "attachment_quarantine_required",
                "Attachment delivery requires a server-owned quarantine receipt.",
            )
        content_hash = _safe_attachment_text(
            item.get("content_hash"),
            field="content_hash",
            max_chars=MAX_HASH_CHARS,
        )
        if content_hash is None:
            raise ConversationIdentityError(
                "attachment_reference_unknown",
                "A quarantined attachment requires a content hash.",
            )
    return redact_attachment_refs(value, owner_principal_id=owner_principal_id)


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
        "attachment_refs": redact_attachment_refs(
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
