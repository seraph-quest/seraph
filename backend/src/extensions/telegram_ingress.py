"""Provider-free Telegram ingress contract for the #752 canary.

This module validates one already-authenticated Telegram update before a future
transport adapter can hand it to Seraph's canonical conversation owner.  It
does not open a Telegram connection, read a bot token, persist state, dispatch
models/tools, or decode an attachment.  Receipts contain bounded metadata and
never contain message text, an audio payload, or a Telegram file reference.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Final

from src.guardian.audio_ingress import AUDIO_INGRESS_SCHEMA_VERSION, AudioProviderStatus


TELEGRAM_INGRESS_SCHEMA_VERSION: Final = "seraph.telegram.ingress.v1"
TELEGRAM_RECEIPT_SCHEMA_VERSION: Final = "seraph.telegram.ingress-receipt.v1"
DEFAULT_MAX_AGE_SECONDS: Final = 300
DEFAULT_MAX_CLOCK_SKEW_SECONDS: Final = 30
DEFAULT_MAX_TEXT_BYTES: Final = 4096
DEFAULT_MAX_ATTACHMENT_BYTES: Final = 10 * 1024 * 1024
DEFAULT_MAX_VOICE_DURATION_SECONDS: Final = 60.0
DEFAULT_MAX_RAW_RETENTION_SECONDS: Final = 15 * 60
DEFAULT_REPLAY_WINDOW: Final = 64
DEFAULT_RATE_LIMIT_WINDOW_SECONDS: Final = 60
DEFAULT_RATE_LIMIT_MAX_UPDATES: Final = 20
TELEGRAM_TRANSIT_CONSENT_SCOPE: Final = "telegram_transit"
OPENROUTER_INFERENCE_CONSENT_SCOPE: Final = "openrouter_inference"
DEFAULT_ALLOWED_MEDIA_TYPES: Final[tuple[str, ...]] = (
    "audio/ogg",
    "audio/mpeg",
    "audio/wav",
    "audio/mp4",
)

_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_HASH_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_IDEMPOTENCY_RE: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_MEDIA_RE: Final = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_ALLOWED_REASON_CODES: Final = frozenset(
    {
        "invalid_request_type",
        "invalid_policy",
        "invalid_pairing_state",
        "server_owned_identity_required",
        "invalid_operator_id",
        "invalid_chat_id",
        "invalid_update_id",
        "invalid_message_id",
        "invalid_sequence",
        "invalid_timestamp",
        "update_too_old",
        "update_clock_ahead",
        "update_not_monotonic",
        "update_id_conflict",
        "message_id_conflict",
        "telegram_identity_not_allowlisted",
        "invalid_text",
        "text_required",
        "text_exceeds_limit",
        "invalid_attachment",
        "attachment_required_voice",
        "attachment_media_type_not_allowed",
        "attachment_size_invalid",
        "attachment_size_exceeds_limit",
        "attachment_hash_invalid",
        "attachment_duration_invalid",
        "attachment_duration_exceeds_limit",
        "attachment_retention_missing",
        "attachment_retention_expired",
        "attachment_retention_before_capture",
        "attachment_retention_exceeds_limit",
        "consent_missing",
        "consent_invalid",
        "consent_reference_invalid",
        "consent_scope_invalid",
        "consent_revoked",
        "consent_expired",
        "consent_not_current",
        "consent_references_not_separate",
        "local_fallback_forbidden",
        "rate_limit_exceeded",
        "replay_conflict",
        "request_already_recorded",
        "text_ingress_accepted_provider_unavailable",
        "text_ingress_accepted_provider_unverified",
        "text_ingress_accepted",
        "voice_ingress_degraded_provider_unavailable",
        "voice_ingress_degraded_provider_unverified",
        "voice_ingress_degraded_preflight_proof_required",
        "voice_handoff_ready",
        "invalid_result_provenance",
    }
)
_PROVENANCE_TOKEN: Final = object()


class TelegramIngressStatus(str, Enum):
    ACCEPTED = "accepted"
    BLOCKED = "blocked"
    DUPLICATE = "duplicate"
    DEGRADED = "degraded"


class TelegramConsentState(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class TelegramConsent:
    reference: str
    state: TelegramConsentState
    granted_at: datetime
    expires_at: datetime
    scope: str = ""


@dataclass(frozen=True, slots=True)
class TelegramAttachmentMetadata:
    """Quarantined metadata; ``file_reference`` is never emitted in receipts."""

    attachment_id: str
    media_type: str
    size_bytes: int
    content_hash: str
    voice_note: bool = True
    duration_seconds: float | None = None
    file_reference: str | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class TelegramIngressPolicy:
    """One server-owned operator/chat binding and bounded ingress limits."""

    operator_id: int
    chat_id: int
    pairing_id: str = "telegram-pairing-1"
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS
    max_clock_skew_seconds: int = DEFAULT_MAX_CLOCK_SKEW_SECONDS
    max_text_bytes: int = DEFAULT_MAX_TEXT_BYTES
    max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES
    max_voice_duration_seconds: float = DEFAULT_MAX_VOICE_DURATION_SECONDS
    max_raw_retention_seconds: int = DEFAULT_MAX_RAW_RETENTION_SECONDS
    replay_window: int = DEFAULT_REPLAY_WINDOW
    rate_limit_window_seconds: int = DEFAULT_RATE_LIMIT_WINDOW_SECONDS
    rate_limit_max_updates: int = DEFAULT_RATE_LIMIT_MAX_UPDATES
    allowed_media_types: tuple[str, ...] = DEFAULT_ALLOWED_MEDIA_TYPES
    provider_status: AudioProviderStatus = AudioProviderStatus.UNVERIFIED
    # These are proof handles issued by the governed #751 adapter.  This
    # pure module validates their shape but never creates or verifies them.
    trusted_adapter_id: str | None = None
    provider_proof_reference: str | None = None
    consent_proof_reference: str | None = None


@dataclass(frozen=True, slots=True)
class TelegramUpdate:
    """External update plus canonical-owner identity references."""

    operator_id: int
    chat_id: int
    update_id: int
    message_id: int
    received_at: datetime
    text: str | None
    attachment: TelegramAttachmentMetadata | None = None
    external_transit_consent: TelegramConsent | None = None
    openrouter_consent: TelegramConsent | None = None
    sequence: int = 1
    server_owned_identity: bool = True
    local_fallback_requested: bool = False

    @property
    def normalized_text(self) -> str | None:
        return self.text.strip() if isinstance(self.text, str) else None


@dataclass(frozen=True, slots=True)
class TelegramReplayEntry:
    update_id: int
    message_id: int
    sequence: int
    idempotency_key: str
    request_digest: str
    accepted_at: datetime


@dataclass(frozen=True, slots=True)
class TelegramRateEvent:
    idempotency_key: str
    accepted_at: datetime


@dataclass(frozen=True, slots=True)
class TelegramIngressState:
    """Caller-owned immutable replay/rate snapshot for pure ingestion."""

    last_sequence: int = 0
    replay_entries: tuple[TelegramReplayEntry, ...] = ()
    rate_events: tuple[TelegramRateEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class TelegramVoiceHandoff:
    """Metadata handoff to #751; it carries no bytes and performs no decode/send."""

    audio_schema_version: str
    attachment_id: str
    content_hash: str
    media_type: str
    size_bytes: int
    duration_seconds: float
    capture_consent_reference: str
    cloud_upload_consent_reference: str
    retention_deadline: str
    request_digest: str
    decode_claimed: bool = False
    send_claimed: bool = False


@dataclass(frozen=True, slots=True)
class TelegramIngressResult:
    status: TelegramIngressStatus
    reason_code: str
    accepted: bool
    retryable: bool
    request_digest: str | None = None
    idempotency_key: str | None = None
    attachment_quarantined: bool = False
    voice_handoff: TelegramVoiceHandoff | None = None
    provider_status: AudioProviderStatus | None = None
    _provenance: object | None = field(default=None, init=False, repr=False, compare=False)
    _policy_fingerprint: str | None = field(default=None, init=False, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class TelegramIngressReceipt:
    schema_version: str
    status: TelegramIngressStatus
    reason_code: str
    request_digest: str | None
    idempotency_key: str | None
    policy_fingerprint: str | None
    identity: dict[str, Any]
    content: dict[str, Any]
    attachment: dict[str, Any]
    consent: dict[str, Any]
    provider: dict[str, Any]
    voice_handoff: dict[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "request_digest": self.request_digest,
            "idempotency_key": self.idempotency_key,
            "policy_fingerprint": self.policy_fingerprint,
            "identity": dict(self.identity),
            "content": dict(self.content),
            "attachment": dict(self.attachment),
            "consent": dict(self.consent),
            "provider": dict(self.provider),
            "voice_handoff": dict(self.voice_handoff),
        }


TelegramRequest = TelegramUpdate
TelegramPolicy = TelegramIngressPolicy
TelegramResult = TelegramIngressResult
TelegramReceipt = TelegramIngressReceipt


def _utc(value: datetime | None) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        if value.utcoffset() is None:
            return None
        return value.astimezone(timezone.utc)
    except (OverflowError, TypeError, ValueError):
        return None


def _now(value: datetime | None) -> datetime | None:
    return _utc(value) if value is not None else datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    normalized = _utc(value)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z") if normalized else None


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.fullmatch(value))


def _normalized_hash(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    if candidate.startswith("sha256:"):
        candidate = candidate[7:]
    return f"sha256:{candidate}" if _HASH_RE.fullmatch(candidate) else None


def _valid_policy(policy: TelegramIngressPolicy) -> bool:
    if not isinstance(policy, TelegramIngressPolicy):
        return False
    if (
        isinstance(policy.operator_id, bool)
        or not isinstance(policy.operator_id, int)
        or policy.operator_id <= 0
        or isinstance(policy.chat_id, bool)
        or not isinstance(policy.chat_id, int)
        or policy.chat_id <= 0
        or not _valid_id(policy.pairing_id)
        or not isinstance(policy.provider_status, AudioProviderStatus)
        or not all(
            value is None or isinstance(value, str)
            for value in (
                policy.trusted_adapter_id,
                policy.provider_proof_reference,
                policy.consent_proof_reference,
            )
        )
    ):
        return False
    proof_fields = (
        policy.trusted_adapter_id,
        policy.provider_proof_reference,
        policy.consent_proof_reference,
    )
    if any(value is not None for value in proof_fields) and not (
        _valid_id(policy.trusted_adapter_id)
        and _valid_id(policy.provider_proof_reference)
        and _valid_id(policy.consent_proof_reference)
    ):
        return False
    positive_ints = (
        policy.max_age_seconds,
        policy.max_clock_skew_seconds,
        policy.max_text_bytes,
        policy.max_attachment_bytes,
        policy.max_raw_retention_seconds,
        policy.replay_window,
        policy.rate_limit_window_seconds,
        policy.rate_limit_max_updates,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in positive_ints):
        return False
    if policy.max_age_seconds > 7 * 24 * 60 * 60 or policy.max_clock_skew_seconds > 24 * 60 * 60:
        return False
    if policy.max_text_bytes > 1024 * 1024 or policy.max_attachment_bytes > 25 * 1024 * 1024:
        return False
    if (
        policy.max_raw_retention_seconds > 15 * 60
        or policy.replay_window > 4096
        or policy.rate_limit_max_updates > 4096
    ):
        return False
    if not isinstance(policy.max_voice_duration_seconds, (int, float)) or isinstance(
        policy.max_voice_duration_seconds, bool
    ) or not math.isfinite(float(policy.max_voice_duration_seconds)) or not 0 < policy.max_voice_duration_seconds <= 60:
        return False
    if not isinstance(policy.allowed_media_types, tuple) or not policy.allowed_media_types:
        return False
    return all(
        isinstance(value, str) and value == value.lower() and bool(_MEDIA_RE.fullmatch(value))
        for value in policy.allowed_media_types
    ) and len(set(policy.allowed_media_types)) == len(policy.allowed_media_types)


def _policy_fingerprint(policy: TelegramIngressPolicy | None) -> str | None:
    if not _valid_policy(policy):
        return None
    assert policy is not None
    payload = {
        "schema": TELEGRAM_INGRESS_SCHEMA_VERSION,
        "operator_id": policy.operator_id,
        "chat_id": policy.chat_id,
        "pairing_id": policy.pairing_id,
        "max_age_seconds": policy.max_age_seconds,
        "max_clock_skew_seconds": policy.max_clock_skew_seconds,
        "max_text_bytes": policy.max_text_bytes,
        "max_attachment_bytes": policy.max_attachment_bytes,
        "max_voice_duration_seconds": policy.max_voice_duration_seconds,
        "max_raw_retention_seconds": policy.max_raw_retention_seconds,
        "replay_window": policy.replay_window,
        "rate_limit_window_seconds": policy.rate_limit_window_seconds,
        "rate_limit_max_updates": policy.rate_limit_max_updates,
        "allowed_media_types": sorted(policy.allowed_media_types),
        "provider_status": policy.provider_status.value,
        "trusted_adapter_id": policy.trusted_adapter_id,
        "provider_proof_reference": policy.provider_proof_reference,
        "consent_proof_reference": policy.consent_proof_reference,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def canonical_telegram_idempotency_key(update: TelegramUpdate) -> str:
    if not isinstance(update, TelegramUpdate):
        raise TypeError("update must be a TelegramUpdate")
    payload = [TELEGRAM_INGRESS_SCHEMA_VERSION, update.operator_id, update.chat_id, update.update_id, update.message_id]
    return "sha256:" + hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def canonical_telegram_request_digest(update: TelegramUpdate) -> str:
    if not isinstance(update, TelegramUpdate):
        raise TypeError("update must be a TelegramUpdate")
    captured = _utc(update.received_at)
    if captured is None:
        raise ValueError("received_at must be timezone-aware")
    attachment = update.attachment
    attachment_payload = None
    if attachment is not None:
        attachment_payload = {
            "attachment_id": attachment.attachment_id,
            "media_type": attachment.media_type,
            "size_bytes": attachment.size_bytes,
            "content_hash": _normalized_hash(attachment.content_hash),
            "voice_note": attachment.voice_note,
            "duration_seconds": attachment.duration_seconds,
        }
    payload = {
        "schema": TELEGRAM_INGRESS_SCHEMA_VERSION,
        "operator_id": update.operator_id,
        "chat_id": update.chat_id,
        "update_id": update.update_id,
        "message_id": update.message_id,
        "text_digest": hashlib.sha256(update.normalized_text.encode("utf-8")).hexdigest()
        if update.normalized_text is not None
        else None,
        "attachment": attachment_payload,
        "external_transit_consent": update.external_transit_consent.reference
        if isinstance(update.external_transit_consent, TelegramConsent)
        else None,
        "external_transit_scope": update.external_transit_consent.scope
        if isinstance(update.external_transit_consent, TelegramConsent)
        else None,
        "openrouter_consent": update.openrouter_consent.reference
        if isinstance(update.openrouter_consent, TelegramConsent)
        else None,
        "openrouter_scope": update.openrouter_consent.scope
        if isinstance(update.openrouter_consent, TelegramConsent)
        else None,
        "sequence": update.sequence,
        "server_owned_identity": update.server_owned_identity,
        "local_fallback_requested": update.local_fallback_requested,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _result(
    status: TelegramIngressStatus,
    reason_code: str,
    *,
    policy: TelegramIngressPolicy | None = None,
    request_digest: str | None = None,
    idempotency_key: str | None = None,
    attachment_quarantined: bool = False,
    voice_handoff: TelegramVoiceHandoff | None = None,
    provider_status: AudioProviderStatus | None = None,
) -> TelegramIngressResult:
    result = TelegramIngressResult(
        status=status,
        reason_code=reason_code,
        accepted=status is TelegramIngressStatus.ACCEPTED,
        retryable=status is TelegramIngressStatus.DEGRADED,
        request_digest=request_digest,
        idempotency_key=idempotency_key,
        attachment_quarantined=attachment_quarantined,
        voice_handoff=voice_handoff,
        provider_status=provider_status,
    )
    object.__setattr__(result, "_provenance", _PROVENANCE_TOKEN)
    object.__setattr__(result, "_policy_fingerprint", _policy_fingerprint(policy))
    return result


def _structural_error(update: TelegramUpdate, policy: TelegramIngressPolicy) -> str | None:
    if not isinstance(policy, TelegramIngressPolicy) or not _valid_policy(policy):
        return "invalid_policy"
    if not isinstance(update, TelegramUpdate):
        return "invalid_request_type"
    if not isinstance(update.operator_id, int) or isinstance(update.operator_id, bool) or update.operator_id <= 0:
        return "invalid_operator_id"
    if not isinstance(update.chat_id, int) or isinstance(update.chat_id, bool) or update.chat_id <= 0:
        return "invalid_chat_id"
    for value, reason in ((update.update_id, "invalid_update_id"), (update.message_id, "invalid_message_id"), (update.sequence, "invalid_sequence")):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            return reason
    if _utc(update.received_at) is None:
        return "invalid_timestamp"
    if update.text is not None and not isinstance(update.text, str):
        return "invalid_text"
    if update.text is not None:
        try:
            text_bytes = update.normalized_text.encode("utf-8")
        except UnicodeError:
            return "invalid_text"
        if len(text_bytes) > policy.max_text_bytes:
            return "text_exceeds_limit"
    if not isinstance(update.server_owned_identity, bool):
        return "server_owned_identity_required"
    if not isinstance(update.local_fallback_requested, bool):
        return "local_fallback_forbidden"
    if update.local_fallback_requested:
        return "local_fallback_forbidden"
    if update.attachment is not None and not isinstance(update.attachment, TelegramAttachmentMetadata):
        return "invalid_attachment"
    return None


def _attachment_error(update: TelegramUpdate, policy: TelegramIngressPolicy) -> str | None:
    attachment = update.attachment
    if attachment is None:
        if not update.normalized_text:
            return "text_required"
        return None
    if not _valid_id(attachment.attachment_id) or not isinstance(attachment.media_type, str) or not _MEDIA_RE.fullmatch(attachment.media_type):
        return "invalid_attachment"
    if attachment.media_type not in policy.allowed_media_types:
        return "attachment_media_type_not_allowed"
    if not isinstance(attachment.voice_note, bool) or not attachment.voice_note:
        return "attachment_required_voice"
    if isinstance(attachment.size_bytes, bool) or not isinstance(attachment.size_bytes, int) or attachment.size_bytes <= 0:
        return "attachment_size_invalid"
    if attachment.size_bytes > policy.max_attachment_bytes:
        return "attachment_size_exceeds_limit"
    if _normalized_hash(attachment.content_hash) is None:
        return "attachment_hash_invalid"
    if (
        not isinstance(attachment.duration_seconds, (int, float))
        or isinstance(attachment.duration_seconds, bool)
        or not math.isfinite(float(attachment.duration_seconds))
        or attachment.duration_seconds <= 0
    ):
        return "attachment_duration_invalid"
    if attachment.duration_seconds > policy.max_voice_duration_seconds:
        return "attachment_duration_exceeds_limit"
    if update.normalized_text:
        return "attachment_required_voice"
    return None


def _consent_error(consent: TelegramConsent | None, *, boundary: str, current: datetime, skew: int) -> str | None:
    if consent is None:
        return "consent_missing"
    if not isinstance(consent, TelegramConsent):
        return "consent_invalid"
    if not isinstance(consent.reference, str) or not _valid_id(consent.reference):
        return "consent_reference_invalid"
    if not isinstance(consent.state, TelegramConsentState):
        return "consent_invalid"
    expected_scope = (
        TELEGRAM_TRANSIT_CONSENT_SCOPE
        if boundary == "external"
        else OPENROUTER_INFERENCE_CONSENT_SCOPE
        if boundary == "openrouter"
        else None
    )
    if expected_scope is None or consent.scope != expected_scope:
        return "consent_scope_invalid"
    granted = _utc(consent.granted_at)
    expires = _utc(consent.expires_at)
    if granted is None or expires is None or expires <= granted:
        return "consent_invalid"
    if consent.state is TelegramConsentState.REVOKED:
        return "consent_revoked"
    if consent.state is TelegramConsentState.EXPIRED or expires <= current:
        return "consent_expired"
    if granted > current + timedelta(seconds=skew):
        return "consent_not_current"
    return None


def _valid_state(state: TelegramIngressState, policy: TelegramIngressPolicy) -> bool:
    if not isinstance(state, TelegramIngressState):
        return False
    if not isinstance(state.last_sequence, int) or isinstance(state.last_sequence, bool) or state.last_sequence < 0:
        return False
    if not isinstance(state.replay_entries, tuple) or len(state.replay_entries) > policy.replay_window:
        return False
    if not isinstance(state.rate_events, tuple) or len(state.rate_events) > policy.rate_limit_max_updates:
        return False
    seen_keys: set[str] = set()
    seen_updates: set[int] = set()
    seen_messages: set[int] = set()
    for entry in state.replay_entries:
        if not isinstance(entry, TelegramReplayEntry):
            return False
        if (
            not isinstance(entry.idempotency_key, str)
            or not _IDEMPOTENCY_RE.fullmatch(entry.idempotency_key)
            or entry.idempotency_key in seen_keys
            or not isinstance(entry.request_digest, str)
            or not _HASH_RE.fullmatch(entry.request_digest)
        ):
            return False
        if (
            not isinstance(entry.update_id, int)
            or isinstance(entry.update_id, bool)
            or not isinstance(entry.message_id, int)
            or isinstance(entry.message_id, bool)
            or not isinstance(entry.sequence, int)
            or isinstance(entry.sequence, bool)
            or entry.update_id < 1
            or entry.message_id < 1
            or entry.sequence < 1
            or entry.sequence > state.last_sequence
            or entry.update_id in seen_updates
            or entry.message_id in seen_messages
        ):
            return False
        if not isinstance(entry.accepted_at, datetime) or _utc(entry.accepted_at) is None:
            return False
        seen_keys.add(entry.idempotency_key)
        seen_updates.add(entry.update_id)
        seen_messages.add(entry.message_id)
    return all(
        isinstance(event, TelegramRateEvent)
        and isinstance(event.accepted_at, datetime)
        and _utc(event.accepted_at) is not None
        and isinstance(event.idempotency_key, str)
        and bool(_IDEMPOTENCY_RE.fullmatch(event.idempotency_key))
        for event in state.rate_events
    )


def validate_telegram_update(
    update: TelegramUpdate,
    state: TelegramIngressState = TelegramIngressState(),
    policy: TelegramIngressPolicy | None = None,
    *,
    now: datetime | None = None,
) -> TelegramIngressResult:
    effective_policy = policy if policy is not None else TelegramIngressPolicy(operator_id=0, chat_id=0)
    if not _valid_policy(effective_policy):
        return _result(TelegramIngressStatus.BLOCKED, "invalid_policy")
    structural = _structural_error(update, effective_policy)
    if structural:
        return _result(TelegramIngressStatus.BLOCKED, structural)
    if not _valid_state(state, effective_policy):
        return _result(TelegramIngressStatus.BLOCKED, "invalid_pairing_state")
    if update.server_owned_identity is not True:
        return _result(TelegramIngressStatus.BLOCKED, "server_owned_identity_required")
    if update.operator_id != effective_policy.operator_id or update.chat_id != effective_policy.chat_id:
        return _result(TelegramIngressStatus.BLOCKED, "telegram_identity_not_allowlisted")
    if now is not None and _utc(now) is None:
        return _result(TelegramIngressStatus.BLOCKED, "invalid_timestamp")
    current = _now(now)
    if current is None:
        return _result(TelegramIngressStatus.BLOCKED, "invalid_timestamp")
    received_at = _utc(update.received_at)
    assert received_at is not None
    age = (current - received_at).total_seconds()
    if age > effective_policy.max_age_seconds:
        return _result(TelegramIngressStatus.BLOCKED, "update_too_old")
    if age < -effective_policy.max_clock_skew_seconds:
        return _result(TelegramIngressStatus.BLOCKED, "update_clock_ahead")
    digest = canonical_telegram_request_digest(update)
    idempotency_key = canonical_telegram_idempotency_key(update)
    for entry in state.replay_entries:
        if hmac.compare_digest(entry.idempotency_key, idempotency_key):
            if hmac.compare_digest(entry.request_digest, digest):
                return _result(
                    TelegramIngressStatus.DUPLICATE,
                    "request_already_recorded",
                    policy=effective_policy,
                    request_digest=digest,
                    idempotency_key=idempotency_key,
                    provider_status=effective_policy.provider_status,
                )
            return _result(TelegramIngressStatus.BLOCKED, "replay_conflict")
        if entry.update_id == update.update_id:
            return _result(TelegramIngressStatus.BLOCKED, "update_id_conflict")
        if entry.message_id == update.message_id:
            return _result(TelegramIngressStatus.BLOCKED, "message_id_conflict")
    if update.sequence <= state.last_sequence:
        return _result(TelegramIngressStatus.BLOCKED, "update_not_monotonic")
    external_error = _consent_error(update.external_transit_consent, boundary="external", current=current, skew=effective_policy.max_clock_skew_seconds)
    if external_error:
        return _result(TelegramIngressStatus.BLOCKED, external_error)
    openrouter_error = _consent_error(update.openrouter_consent, boundary="openrouter", current=current, skew=effective_policy.max_clock_skew_seconds)
    if openrouter_error:
        return _result(TelegramIngressStatus.BLOCKED, openrouter_error)
    assert update.external_transit_consent is not None and update.openrouter_consent is not None
    if update.external_transit_consent.reference == update.openrouter_consent.reference:
        return _result(TelegramIngressStatus.BLOCKED, "consent_references_not_separate")
    attachment_error = _attachment_error(update, effective_policy)
    if attachment_error:
        return _result(TelegramIngressStatus.BLOCKED, attachment_error, attachment_quarantined=update.attachment is not None)
    active_events = [
        event
        for event in state.rate_events
        if (current - _utc(event.accepted_at)).total_seconds() <= effective_policy.rate_limit_window_seconds
    ]
    if len(active_events) >= effective_policy.rate_limit_max_updates:
        return _result(TelegramIngressStatus.BLOCKED, "rate_limit_exceeded")
    attachment = update.attachment
    if attachment is None:
        reason = (
            "text_ingress_accepted_provider_unavailable"
            if effective_policy.provider_status is AudioProviderStatus.UNAVAILABLE
            else "text_ingress_accepted_provider_unverified"
            if effective_policy.provider_status is AudioProviderStatus.UNVERIFIED
            else "text_ingress_accepted"
        )
        return _result(
            TelegramIngressStatus.ACCEPTED,
            reason,
            policy=effective_policy,
            request_digest=digest,
            idempotency_key=idempotency_key,
            provider_status=effective_policy.provider_status,
        )
    deadline = _utc(received_at + timedelta(seconds=effective_policy.max_raw_retention_seconds))
    if deadline is None or deadline <= current:
        return _result(
            TelegramIngressStatus.BLOCKED,
            "attachment_retention_expired",
            attachment_quarantined=True,
        )
    handoff = TelegramVoiceHandoff(
        audio_schema_version=AUDIO_INGRESS_SCHEMA_VERSION,
        attachment_id=attachment.attachment_id,
        content_hash=_normalized_hash(attachment.content_hash),
        media_type=attachment.media_type,
        size_bytes=attachment.size_bytes,
        duration_seconds=float(attachment.duration_seconds),
        capture_consent_reference=update.external_transit_consent.reference,
        cloud_upload_consent_reference=update.openrouter_consent.reference,
        retention_deadline=_iso(deadline),
        request_digest=digest,
    )
    proof_available = all(
        (
            effective_policy.trusted_adapter_id,
            effective_policy.provider_proof_reference,
            effective_policy.consent_proof_reference,
        )
    )
    if effective_policy.provider_status is AudioProviderStatus.UNAVAILABLE:
        status = TelegramIngressStatus.DEGRADED
        reason = "voice_ingress_degraded_provider_unavailable"
    elif effective_policy.provider_status is AudioProviderStatus.UNVERIFIED:
        status = TelegramIngressStatus.DEGRADED
        reason = "voice_ingress_degraded_provider_unverified"
    elif not proof_available:
        status = TelegramIngressStatus.DEGRADED
        reason = "voice_ingress_degraded_preflight_proof_required"
    else:
        status = TelegramIngressStatus.ACCEPTED
        reason = "voice_handoff_ready"
    return _result(
        status,
        reason,
        policy=effective_policy,
        request_digest=digest,
        idempotency_key=idempotency_key,
        attachment_quarantined=True,
        voice_handoff=handoff,
        provider_status=effective_policy.provider_status,
    )


def ingest_telegram_update(
    state: TelegramIngressState,
    update: TelegramUpdate,
    policy: TelegramIngressPolicy | None = None,
    *,
    now: datetime | None = None,
) -> tuple[TelegramIngressResult, TelegramIngressState]:
    """Validate and return the next replay/rate snapshot without persistence."""

    effective_policy = policy if policy is not None else TelegramIngressPolicy(operator_id=0, chat_id=0)
    result = validate_telegram_update(update, state, effective_policy, now=now)
    current = _now(now)
    if current is None or not _valid_policy(effective_policy) or not _valid_state(state, effective_policy):
        return result, state
    live_events = [
        event
        for event in state.rate_events
        if (current - _utc(event.accepted_at)).total_seconds() <= effective_policy.rate_limit_window_seconds
    ]
    next_state = replace(state, rate_events=tuple(live_events))
    if result.status not in (TelegramIngressStatus.ACCEPTED, TelegramIngressStatus.DEGRADED):
        return result, next_state
    assert result.idempotency_key is not None and result.request_digest is not None
    live_events.append(TelegramRateEvent(result.idempotency_key, current))
    entries = list(state.replay_entries)
    entries.append(
        TelegramReplayEntry(
            update_id=update.update_id,
            message_id=update.message_id,
            sequence=update.sequence,
            idempotency_key=result.idempotency_key,
            request_digest=result.request_digest,
            accepted_at=current,
        )
    )
    return result, replace(
        next_state,
        last_sequence=update.sequence,
        replay_entries=tuple(entries[-effective_policy.replay_window :]),
        rate_events=tuple(live_events),
    )


def _trusted_result(result: TelegramIngressResult, update: TelegramUpdate | None, policy: TelegramIngressPolicy | None) -> TelegramIngressResult:
    if not (
        isinstance(result, TelegramIngressResult)
        and result._provenance is _PROVENANCE_TOKEN
        and isinstance(result.status, TelegramIngressStatus)
        and result.accepted is (result.status is TelegramIngressStatus.ACCEPTED)
        and result.retryable is (result.status is TelegramIngressStatus.DEGRADED)
    ):
        return _result(TelegramIngressStatus.BLOCKED, "invalid_result_provenance")
    if result.status is not TelegramIngressStatus.BLOCKED:
        expected_policy_fingerprint = _policy_fingerprint(policy)
        if update is None or expected_policy_fingerprint is None:
            return _result(TelegramIngressStatus.BLOCKED, "invalid_result_provenance")
        if result._policy_fingerprint != expected_policy_fingerprint:
            return _result(TelegramIngressStatus.BLOCKED, "invalid_result_provenance")
        try:
            if result.request_digest != canonical_telegram_request_digest(update) or result.idempotency_key != canonical_telegram_idempotency_key(update):
                return _result(TelegramIngressStatus.BLOCKED, "invalid_result_provenance")
        except (TypeError, ValueError, UnicodeError):
            return _result(TelegramIngressStatus.BLOCKED, "invalid_result_provenance")
    elif result.request_digest is not None and update is not None:
        try:
            if result.request_digest != canonical_telegram_request_digest(update):
                return _result(TelegramIngressStatus.BLOCKED, "invalid_result_provenance")
        except (TypeError, ValueError, UnicodeError):
            return _result(TelegramIngressStatus.BLOCKED, "invalid_result_provenance")
    return result


def serialize_telegram_receipt(
    update: TelegramUpdate | None,
    result: TelegramIngressResult,
    *,
    policy: TelegramIngressPolicy | None = None,
) -> TelegramIngressReceipt:
    safe_result = _trusted_result(result, update, policy)
    reason = safe_result.reason_code if safe_result.reason_code in _ALLOWED_REASON_CODES else "invalid_result_provenance"
    include = safe_result.status is not TelegramIngressStatus.BLOCKED and isinstance(update, TelegramUpdate) and _valid_policy(policy)
    policy_fp = _policy_fingerprint(policy) if include else None
    if include:
        assert update is not None
        normalized = update.normalized_text
        attachment = update.attachment
        identity = {
            "operator_id": update.operator_id,
            "chat_id": update.chat_id,
            "update_id": update.update_id,
            "message_id": update.message_id,
            "sequence": update.sequence,
        }
        content = {
            "present": bool(normalized),
            "size_bytes": len(normalized.encode("utf-8")) if normalized else 0,
            "digest": hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else None,
            "redacted": True,
        }
        attachment_payload = {
            "present": attachment is not None,
            "quarantine_status": "quarantined" if attachment is not None else "none",
            "attachment_id": attachment.attachment_id if attachment is not None else None,
            "media_type": attachment.media_type if attachment is not None else None,
            "size_bytes": attachment.size_bytes if attachment is not None else None,
            "content_hash": _normalized_hash(attachment.content_hash) if attachment is not None else None,
            "duration_seconds": attachment.duration_seconds if attachment is not None else None,
            "file_reference": None,
        }
        consent = {
            "external_transit_reference": update.external_transit_consent.reference if update.external_transit_consent else None,
            "external_transit_scope": update.external_transit_consent.scope if update.external_transit_consent else None,
            "openrouter_reference": update.openrouter_consent.reference if update.openrouter_consent else None,
            "openrouter_scope": update.openrouter_consent.scope if update.openrouter_consent else None,
        }
        provider = {
            "name": "openrouter",
            "status": safe_result.provider_status.value if safe_result.provider_status else None,
            "preflight_proof_present": bool(
                policy
                and policy.trusted_adapter_id
                and policy.provider_proof_reference
                and policy.consent_proof_reference
            ),
            "model_dispatch_claimed": False,
            "local_fallback_claimed": False,
        }
        handoff = (
            {
                "audio_schema_version": safe_result.voice_handoff.audio_schema_version,
                "attachment_id": safe_result.voice_handoff.attachment_id,
                "content_hash": safe_result.voice_handoff.content_hash,
                "media_type": safe_result.voice_handoff.media_type,
                "size_bytes": safe_result.voice_handoff.size_bytes,
                "duration_seconds": safe_result.voice_handoff.duration_seconds,
                "retention_deadline": safe_result.voice_handoff.retention_deadline,
                "request_digest": safe_result.voice_handoff.request_digest,
                "decode_claimed": False,
                "send_claimed": False,
            }
            if safe_result.voice_handoff is not None
            else {"status": "none"}
        )
        return TelegramIngressReceipt(
            TELEGRAM_RECEIPT_SCHEMA_VERSION,
            safe_result.status,
            reason,
            safe_result.request_digest,
            safe_result.idempotency_key,
            policy_fp,
            identity,
            content,
            attachment_payload,
            consent,
            provider,
            handoff,
        )
    return TelegramIngressReceipt(
        TELEGRAM_RECEIPT_SCHEMA_VERSION,
        TelegramIngressStatus.BLOCKED if safe_result.status is not TelegramIngressStatus.BLOCKED else safe_result.status,
        reason,
        None,
        None,
        None,
        {"operator_id": None, "chat_id": None, "update_id": None, "message_id": None, "sequence": None},
        {"present": False, "size_bytes": None, "digest": None, "redacted": True},
        {"present": False, "quarantine_status": "blocked", "attachment_id": None, "media_type": None, "size_bytes": None, "content_hash": None, "duration_seconds": None, "file_reference": None},
        {"external_transit_reference": None, "external_transit_scope": None, "openrouter_reference": None, "openrouter_scope": None},
        {"name": None, "status": None, "preflight_proof_present": False, "model_dispatch_claimed": False, "local_fallback_claimed": False},
        {"status": "none"},
    )


def build_telegram_ingress_receipt(update: TelegramUpdate | None, result: TelegramIngressResult, *, policy: TelegramIngressPolicy | None = None) -> TelegramIngressReceipt:
    return serialize_telegram_receipt(update, result, policy=policy)


validate_telegram_ingress = validate_telegram_update
ingest_telegram_ingress = ingest_telegram_update
serialize_telegram_ingress_receipt = serialize_telegram_receipt


__all__ = [
    "AUDIO_INGRESS_SCHEMA_VERSION",
    "DEFAULT_ALLOWED_MEDIA_TYPES",
    "OPENROUTER_INFERENCE_CONSENT_SCOPE",
    "TELEGRAM_TRANSIT_CONSENT_SCOPE",
    "TelegramAttachmentMetadata",
    "TelegramConsent",
    "TelegramConsentState",
    "TelegramIngressPolicy",
    "TelegramIngressReceipt",
    "TelegramIngressResult",
    "TelegramIngressState",
    "TelegramIngressStatus",
    "TelegramRateEvent",
    "TelegramReplayEntry",
    "TelegramUpdate",
    "TelegramVoiceHandoff",
    "TelegramPolicy",
    "TelegramReceipt",
    "TelegramRequest",
    "TelegramResult",
    "build_telegram_ingress_receipt",
    "canonical_telegram_idempotency_key",
    "canonical_telegram_request_digest",
    "ingest_telegram_ingress",
    "ingest_telegram_update",
    "serialize_telegram_ingress_receipt",
    "serialize_telegram_receipt",
    "validate_telegram_ingress",
    "validate_telegram_update",
]
