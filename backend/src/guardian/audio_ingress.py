"""Provider-free preflight contracts for the #751 push-to-talk canary.

This module owns no session, message, attachment, or outbox storage.  A caller
that already resolved those server-owned identities (for example, the #750 web
ingress path) can pass an immutable envelope here for deterministic policy
checking.  The module never decodes audio, opens a socket, invokes a model, or
uses a local speech/VLM runtime.

OpenRouter's documented audio input uses a chat content item with
``type=input_audio`` and ``input_audio.data`` containing standard base64 of raw
bytes plus a short format such as ``wav`` or ``mp3``.  The payload builder below
only shapes that data for a later governed caller; it is not a provider call.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Final


AUDIO_INGRESS_SCHEMA_VERSION: Final = "seraph.guardian.audio-ingress.v1"
AUDIO_INGRESS_RECEIPT_SCHEMA_VERSION: Final = "seraph.guardian.audio-ingress-receipt.v1"
OPENROUTER_PROVIDER: Final = "openrouter"
OPENROUTER_CHAT_COMPLETIONS_ENDPOINT: Final = "https://openrouter.ai/api/v1/chat/completions"
MAX_AUDIO_BYTES: Final = 10 * 1024 * 1024
MAX_AUDIO_DURATION_SECONDS: Final = 60.0
MAX_NORMALIZED_WAV_BYTES: Final = 2 * 1024 * 1024
MAX_RAW_AUDIO_RETENTION_SECONDS: Final = 15 * 60
DEFAULT_CLOCK_SKEW_SECONDS: Final = 30

_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CONSENT_REF_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_BASE64_RE: Final = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")
_MEDIA_TYPE_RE: Final = re.compile(r"^audio/[a-z0-9][a-z0-9.+-]{0,62}$")
_CONTAINER_RE: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
_CODEC_RE: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_DIGEST_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_TRUSTED_RESULT_TOKEN: Final = object()
_CONSENT_REASON_CODES: Final = frozenset(
    f"{boundary}_consent_{suffix}"
    for boundary in ("capture", "cloud_upload")
    for suffix in ("missing", "invalid", "reference_invalid", "revoked", "stale", "not_current")
)
_ALLOWED_REASON_CODES: Final = frozenset(
    {
        "invalid_request_type",
        "invalid_policy",
        "invalid_server_owned_id",
        "server_owned_identity_required",
        "invalid_capture_timestamp",
        "invalid_base64_payload",
        "invalid_audio_size",
        "audio_size_exceeds_limit",
        "audio_size_metadata_mismatch",
        "invalid_duration",
        "duration_exceeds_limit",
        "invalid_normalized_wav_size",
        "normalized_wav_size_exceeds_limit",
        "invalid_audio_format",
        "audio_format_not_allowed",
        "multiple_streams_not_allowed",
        "invalid_channel_count",
        "invalid_sample_rate",
        "invalid_requested_capability",
        "invalid_transcript_confirmation",
        "invalid_transcript_confirmation_ref",
        "invalid_authority_flags",
        "provider_not_openrouter",
        "local_fallback_forbidden",
        "invalid_validation_clock",
        "capture_consent_after_capture",
        "capture_consent_expired_before_capture",
        "consent_references_not_separate",
        "raw_audio_retention_deadline_missing",
        "raw_audio_retention_expired",
        "raw_audio_retention_before_capture",
        "raw_audio_retention_exceeds_limit",
        "requested_capability_not_allowed",
        "transcript_confirmation_required",
        "openrouter_route_unavailable",
        "openrouter_capability_unverified",
        "trusted_adapter_proof_required",
        "identity_lookup_invalid",
        "request_already_recorded",
        "request_identity_conflict",
        "attachment_identity_conflict",
        "audio_ingress_preflight_accepted",
        "invalid_receipt_reason_code",
        "invalid_result_provenance",
        "capture_timestamp_in_future",
    }
) | _CONSENT_REASON_CODES


class AudioIngressStatus(str, Enum):
    """Stable preflight outcome states exposed to an operator or caller."""

    ACCEPTED = "accepted"
    BLOCKED = "blocked"
    DEGRADED = "degraded"
    DUPLICATE = "duplicate"


class AudioConsentState(str, Enum):
    """State of one independently recorded consent grant."""

    ACTIVE = "active"
    STALE = "stale"
    REVOKED = "revoked"


class AudioProviderStatus(str, Enum):
    """Provider proof state; this module does not perform the proof."""

    READY = "ready"
    UNAVAILABLE = "unavailable"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class AudioFormatRule:
    """One allowed MIME/container/codec combination."""

    media_type: str
    container: str
    codec: str


DEFAULT_AUDIO_FORMATS: Final[tuple[AudioFormatRule, ...]] = (
    AudioFormatRule("audio/aac", "aac", "aac"),
    AudioFormatRule("audio/flac", "flac", "flac"),
    AudioFormatRule("audio/mp4", "m4a", "aac"),
    AudioFormatRule("audio/mpeg", "mp3", "mp3"),
    AudioFormatRule("audio/ogg", "ogg", "opus"),
    AudioFormatRule("audio/wav", "wav", "pcm_s16le"),
    AudioFormatRule("audio/webm", "webm", "opus"),
)
DEFAULT_AUDIO_CAPABILITIES: Final[tuple[str, ...]] = (
    "analysis",
    "chat",
    "transcription",
)


@dataclass(frozen=True, slots=True)
class AudioIngressPolicy:
    """Finite, deliberately capped policy for the PTT preflight."""

    max_audio_bytes: int = MAX_AUDIO_BYTES
    max_duration_seconds: float = MAX_AUDIO_DURATION_SECONDS
    max_normalized_wav_bytes: int = MAX_NORMALIZED_WAV_BYTES
    max_raw_retention_seconds: int = MAX_RAW_AUDIO_RETENTION_SECONDS
    clock_skew_seconds: int = DEFAULT_CLOCK_SKEW_SECONDS
    allowed_formats: tuple[AudioFormatRule, ...] = DEFAULT_AUDIO_FORMATS
    allowed_capabilities: tuple[str, ...] = DEFAULT_AUDIO_CAPABILITIES
    provider: str = OPENROUTER_PROVIDER
    provider_status: AudioProviderStatus = AudioProviderStatus.UNVERIFIED
    openrouter_endpoint: str = OPENROUTER_CHAT_COMPLETIONS_ENDPOINT
    # These are attestation handles supplied by a trusted adapter outside this
    # pure module.  The module validates their shape but cannot establish who
    # supplied them or prove a live provider/consent check.
    trusted_adapter_id: str | None = None
    provider_proof_reference: str | None = None
    consent_proof_reference: str | None = None


@dataclass(frozen=True, slots=True)
class AudioConsent:
    """Metadata-only consent evidence for one audio boundary."""

    reference: str
    state: AudioConsentState
    granted_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AudioIngressRequest:
    """Server-bound PTT metadata and a base64 payload shape.

    The three conversation identifiers must already be server-owned.  The
    ``identity_server_owned`` bit records that upstream fact for this pure
    boundary; it does not create or claim a canonical store.
    """

    session_id: str
    message_id: str
    attachment_id: str
    request_id: str
    captured_at: datetime
    audio_base64: str
    audio_size_bytes: int
    duration_seconds: float
    media_type: str
    container: str
    codec: str
    stream_count: int
    normalized_wav_size_bytes: int | None
    capture_consent: AudioConsent | None
    cloud_upload_consent: AudioConsent | None
    raw_audio_retention_deadline: datetime | None
    requested_capability: str
    transcript_confirmed: bool = False
    transcript_confirmation_ref: str | None = None
    identity_server_owned: bool = True
    inference_provider: str = OPENROUTER_PROVIDER
    local_fallback_requested: bool = False
    sample_rate_hz: int = 16_000
    channels: int = 1

    @property
    def size_bytes(self) -> int:
        return self.audio_size_bytes

    @property
    def capability(self) -> str:
        return self.requested_capability

    @property
    def payload_digest(self) -> str:
        """Digest the encoded payload without decoding or retaining a receipt copy."""

        return hashlib.sha256(self.audio_base64.encode("ascii", errors="strict")).hexdigest()


@dataclass(frozen=True, slots=True)
class AudioRequestIdentity:
    """Metadata-only lookup row supplied by a canonical caller for retries."""

    request_id: str
    session_id: str
    message_id: str
    attachment_id: str
    request_digest: str


@dataclass(frozen=True, slots=True)
class OpenRouterInputAudio:
    """Typed content item matching OpenRouter's documented chat shape."""

    data: str
    format: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "type": "input_audio",
            "input_audio": {
                "data": self.data,
                "format": self.format,
            },
        }


@dataclass(frozen=True, slots=True)
class AudioIngressResult:
    """Typed, stable outcome of a pure audio preflight."""

    status: AudioIngressStatus
    reason_code: str
    accepted: bool
    retryable: bool
    request_digest: str | None = None
    # Public construction remains compatible for adapters and tests, but a
    # hand-built result cannot assert status in an operator receipt.  Only
    # ``_result`` below can attach the module-private provenance token.
    _provenance: object | None = field(default=None, init=False, repr=False, compare=False)

    @property
    def terminal(self) -> bool:
        return not self.retryable


@dataclass(frozen=True, slots=True)
class AudioIngressReceipt:
    """Operator-safe receipt that contains no raw audio or transcript."""

    schema_version: str
    status: AudioIngressStatus
    reason_code: str
    request_digest: str | None
    session_id: str | None
    message_id: str | None
    attachment_id: str | None
    request_id: str | None
    captured_at: str | None
    audio_payload_digest: str | None
    audio_size_bytes: int | None
    duration_seconds: float | None
    media_type: str | None
    container: str | None
    codec: str | None
    normalized_wav_size_bytes: int | None
    stream_count: int | None
    capture_consent_reference: str | None
    cloud_upload_consent_reference: str | None
    raw_audio_retention_deadline: str | None
    requested_capability: str | None
    transcript_confirmed: bool
    provider: str | None
    provider_status: AudioProviderStatus | None
    trusted_adapter_id: str | None = None
    provider_proof_reference: str | None = None
    consent_proof_reference: str | None = None
    raw_audio_in_receipt: bool = False
    transcript_in_receipt: bool = False
    live_provider_call_claimed: bool = False
    local_fallback_claimed: bool = False

    def as_payload(self) -> dict[str, Any]:
        """Serialize only bounded metadata suitable for operator surfaces."""

        return {
            "schema_version": self.schema_version,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "request_digest": self.request_digest,
            "identity": {
                "session_id": self.session_id,
                "message_id": self.message_id,
                "attachment_id": self.attachment_id,
                "request_id": self.request_id,
            },
            "capture": {
                "captured_at": self.captured_at,
                "audio_payload_digest": self.audio_payload_digest,
                "audio_size_bytes": self.audio_size_bytes,
                "duration_seconds": self.duration_seconds,
                "media_type": self.media_type,
                "container": self.container,
                "codec": self.codec,
                "stream_count": self.stream_count,
                "normalized_wav_size_bytes": self.normalized_wav_size_bytes,
                "raw_audio_retention_deadline": self.raw_audio_retention_deadline,
            },
            "consent": {
                "capture_reference": self.capture_consent_reference,
                "cloud_upload_reference": self.cloud_upload_consent_reference,
                "transcript_confirmed": self.transcript_confirmed,
            },
            "capability": self.requested_capability,
            "provider": {
                "name": self.provider,
                "status": self.provider_status.value if self.provider_status is not None else None,
                "trusted_adapter_id": self.trusted_adapter_id,
                "provider_proof_reference": self.provider_proof_reference,
                "consent_proof_reference": self.consent_proof_reference,
                "live_call_claimed": self.live_provider_call_claimed,
                "local_fallback_claimed": self.local_fallback_claimed,
            },
            "privacy": {
                "raw_audio_in_receipt": self.raw_audio_in_receipt,
                "transcript_in_receipt": self.transcript_in_receipt,
            },
        }


# Short aliases make the contract easy to discover for adapters without
# creating duplicate data models or storage surfaces.
AudioRequest = AudioIngressRequest
AudioPolicy = AudioIngressPolicy
AudioReceipt = AudioIngressReceipt


def _utc(value: datetime | None) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _now(value: datetime | None) -> datetime | None:
    return _utc(value) if value is not None else datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    normalized = _utc(value)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z") if normalized else None


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.fullmatch(value))


def _valid_consent_ref(value: Any) -> bool:
    return isinstance(value, str) and bool(_CONSENT_REF_RE.fullmatch(value))


def _valid_base64_shape(value: Any) -> bool:
    """Validate standard base64 syntax arithmetically; never decode it."""

    if not isinstance(value, str) or not value or len(value) % 4 != 0 or not _BASE64_RE.fullmatch(value):
        return False
    padding = len(value) - len(value.rstrip("="))
    if padding > 2:
        return False
    unpadded = value[:-padding] if padding else value
    if "=" in unpadded:
        return False
    return True


def _estimated_base64_bytes(value: str) -> int:
    padding = len(value) - len(value.rstrip("="))
    return (len(value) // 4) * 3 - padding


def _valid_policy(policy: AudioIngressPolicy) -> bool:
    if not isinstance(policy, AudioIngressPolicy):
        return False
    if (
        isinstance(policy.max_audio_bytes, bool)
        or not isinstance(policy.max_audio_bytes, int)
        or not 1 <= policy.max_audio_bytes <= MAX_AUDIO_BYTES
        or not isinstance(policy.max_duration_seconds, (int, float))
        or isinstance(policy.max_duration_seconds, bool)
        or not 0 < policy.max_duration_seconds <= MAX_AUDIO_DURATION_SECONDS
        or not math.isfinite(float(policy.max_duration_seconds))
        or isinstance(policy.max_normalized_wav_bytes, bool)
        or not isinstance(policy.max_normalized_wav_bytes, int)
        or not 1 <= policy.max_normalized_wav_bytes <= MAX_NORMALIZED_WAV_BYTES
        or isinstance(policy.max_raw_retention_seconds, bool)
        or not isinstance(policy.max_raw_retention_seconds, int)
        or not 1 <= policy.max_raw_retention_seconds <= MAX_RAW_AUDIO_RETENTION_SECONDS
        or isinstance(policy.clock_skew_seconds, bool)
        or not isinstance(policy.clock_skew_seconds, int)
        or not 0 <= policy.clock_skew_seconds <= 24 * 60 * 60
        or policy.provider != OPENROUTER_PROVIDER
        or policy.openrouter_endpoint != OPENROUTER_CHAT_COMPLETIONS_ENDPOINT
        or not isinstance(policy.provider_status, AudioProviderStatus)
        or not isinstance(policy.allowed_formats, tuple)
        or not isinstance(policy.allowed_capabilities, tuple)
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
        and _valid_consent_ref(policy.provider_proof_reference)
        and _valid_consent_ref(policy.consent_proof_reference)
    ):
        return False
    if not policy.allowed_formats or not policy.allowed_capabilities:
        return False
    for rule in policy.allowed_formats:
        if not isinstance(rule, AudioFormatRule):
            return False
        if (
            not isinstance(rule.media_type, str)
            or not isinstance(rule.container, str)
            or not isinstance(rule.codec, str)
            or not _MEDIA_TYPE_RE.fullmatch(rule.media_type)
            or not _CONTAINER_RE.fullmatch(rule.container)
            or not _CODEC_RE.fullmatch(rule.codec)
        ):
            return False
    if len(set(policy.allowed_formats)) != len(policy.allowed_formats):
        return False
    return all(
        isinstance(capability, str)
        and bool(capability)
        and len(capability) <= 64
        and not any(char.isspace() for char in capability)
        for capability in policy.allowed_capabilities
    ) and len(set(policy.allowed_capabilities)) == len(policy.allowed_capabilities)


def _request_structure_error(request: AudioIngressRequest, policy: AudioIngressPolicy) -> str | None:
    if not isinstance(request, AudioIngressRequest):
        return "invalid_request_type"
    if not policy or not _valid_policy(policy):
        return "invalid_policy"
    if not all(_valid_id(value) for value in (request.session_id, request.message_id, request.attachment_id, request.request_id)):
        return "invalid_server_owned_id"
    if request.identity_server_owned is not True:
        return "server_owned_identity_required"
    if _utc(request.captured_at) is None:
        return "invalid_capture_timestamp"
    if not _valid_base64_shape(request.audio_base64):
        return "invalid_base64_payload"
    if isinstance(request.audio_size_bytes, bool) or not isinstance(request.audio_size_bytes, int) or request.audio_size_bytes <= 0:
        return "invalid_audio_size"
    if request.audio_size_bytes > policy.max_audio_bytes:
        return "audio_size_exceeds_limit"
    if _estimated_base64_bytes(request.audio_base64) != request.audio_size_bytes:
        return "audio_size_metadata_mismatch"
    if (
        isinstance(request.duration_seconds, bool)
        or not isinstance(request.duration_seconds, (int, float))
        or not math.isfinite(float(request.duration_seconds))
        or request.duration_seconds <= 0
    ):
        return "invalid_duration"
    if request.duration_seconds > policy.max_duration_seconds:
        return "duration_exceeds_limit"
    if (
        isinstance(request.normalized_wav_size_bytes, bool)
        or request.normalized_wav_size_bytes is not None
        and (
            not isinstance(request.normalized_wav_size_bytes, int)
            or request.normalized_wav_size_bytes < 0
        )
    ):
        return "invalid_normalized_wav_size"
    if (
        request.normalized_wav_size_bytes is not None
        and request.normalized_wav_size_bytes > policy.max_normalized_wav_bytes
    ):
        return "normalized_wav_size_exceeds_limit"
    if not isinstance(request.media_type, str) or not isinstance(request.container, str) or not isinstance(request.codec, str):
        return "invalid_audio_format"
    if not _CONTAINER_RE.fullmatch(request.container) or not _CODEC_RE.fullmatch(request.codec):
        return "invalid_audio_format"
    if AudioFormatRule(request.media_type, request.container, request.codec) not in policy.allowed_formats:
        return "audio_format_not_allowed"
    if isinstance(request.stream_count, bool) or not isinstance(request.stream_count, int) or request.stream_count != 1:
        return "multiple_streams_not_allowed"
    if isinstance(request.channels, bool) or not isinstance(request.channels, int) or not 1 <= request.channels <= 2:
        return "invalid_channel_count"
    if isinstance(request.sample_rate_hz, bool) or not isinstance(request.sample_rate_hz, int) or not 8_000 <= request.sample_rate_hz <= 192_000:
        return "invalid_sample_rate"
    if not isinstance(request.requested_capability, str) or not request.requested_capability.strip() or any(
        char.isspace() for char in request.requested_capability
    ):
        return "invalid_requested_capability"
    if not isinstance(request.transcript_confirmed, bool):
        return "invalid_transcript_confirmation"
    if request.transcript_confirmation_ref is not None and not _valid_consent_ref(request.transcript_confirmation_ref):
        return "invalid_transcript_confirmation_ref"
    if not isinstance(request.identity_server_owned, bool) or not isinstance(request.local_fallback_requested, bool):
        return "invalid_authority_flags"
    if request.inference_provider != OPENROUTER_PROVIDER:
        return "provider_not_openrouter"
    if request.local_fallback_requested:
        return "local_fallback_forbidden"
    return None


def _canonical_request_digest(request: AudioIngressRequest) -> str:
    payload = {
        "schema_version": AUDIO_INGRESS_SCHEMA_VERSION,
        "session_id": request.session_id,
        "message_id": request.message_id,
        "attachment_id": request.attachment_id,
        "request_id": request.request_id,
        "captured_at": _iso(request.captured_at),
        "audio_payload_digest": request.payload_digest,
        "audio_size_bytes": request.audio_size_bytes,
        "duration_seconds": request.duration_seconds,
        "media_type": request.media_type,
        "container": request.container,
        "codec": request.codec,
        "stream_count": request.stream_count,
        "normalized_wav_size_bytes": request.normalized_wav_size_bytes,
        "capture_consent_reference": _consent_reference(request.capture_consent),
        "cloud_upload_consent_reference": _consent_reference(request.cloud_upload_consent),
        "raw_audio_retention_deadline": _iso(request.raw_audio_retention_deadline),
        "requested_capability": request.requested_capability,
        "transcript_confirmed": request.transcript_confirmed,
        "transcript_confirmation_ref": request.transcript_confirmation_ref,
        "identity_server_owned": request.identity_server_owned,
        "inference_provider": request.inference_provider,
        "local_fallback_requested": request.local_fallback_requested,
        "sample_rate_hz": request.sample_rate_hz,
        "channels": request.channels,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def canonical_audio_request_digest(request: AudioIngressRequest) -> str:
    """Return a stable metadata digest without placing raw audio in it."""

    if not isinstance(request, AudioIngressRequest):
        raise TypeError("request must be an AudioIngressRequest")
    if _utc(request.captured_at) is None:
        raise ValueError("captured_at must be timezone-aware")
    return _canonical_request_digest(request)


def _valid_identity(identity: AudioRequestIdentity) -> bool:
    return (
        isinstance(identity, AudioRequestIdentity)
        and all(_valid_id(value) for value in (identity.request_id, identity.session_id, identity.message_id, identity.attachment_id))
        and isinstance(identity.request_digest, str)
        and bool(_DIGEST_RE.fullmatch(identity.request_digest))
    )


def _consent_reference(consent: AudioConsent | None) -> str | None:
    """Return only a validated consent handle for digest/receipt metadata."""

    if isinstance(consent, AudioConsent) and _valid_consent_ref(consent.reference):
        return consent.reference
    return None


def _safe_payload_digest(request: AudioIngressRequest) -> str | None:
    """Digest only a syntactically valid payload; malformed input stays redacted."""

    if not isinstance(request, AudioIngressRequest) or not _valid_base64_shape(request.audio_base64):
        return None
    return request.payload_digest


def _safe_request_digest(value: Any) -> str | None:
    """Allow only a canonical lowercase SHA-256 handle into receipts."""

    return value if isinstance(value, str) and _DIGEST_RE.fullmatch(value) else None


def _safe_reason_code(value: Any) -> str:
    """Keep result receipts on the finite, documented reason-code contract."""

    return value if isinstance(value, str) and value in _ALLOWED_REASON_CODES else "invalid_receipt_reason_code"


def _safe_canonical_request_digest(request: AudioIngressRequest) -> str | None:
    """Derive a digest for a receipt only when the request shape is usable."""

    if not isinstance(request, AudioIngressRequest):
        return None
    try:
        return canonical_audio_request_digest(request)
    except (AttributeError, TypeError, UnicodeError, ValueError, OverflowError):
        return None


def _receipt_request_digest(
    request: AudioIngressRequest,
    result: AudioIngressResult,
) -> str | None:
    """Include a result digest only when it matches this request exactly."""

    canonical_digest = _safe_canonical_request_digest(request)
    candidate = _safe_request_digest(result.request_digest)
    return candidate if canonical_digest is not None and candidate == canonical_digest else None


def _consent_reason(
    consent: AudioConsent | None,
    *,
    boundary: str,
    current: datetime,
    clock_skew_seconds: int,
    capture_at: datetime | None = None,
) -> str | None:
    if consent is None:
        return f"{boundary}_consent_missing"
    if not isinstance(consent, AudioConsent):
        return f"{boundary}_consent_invalid"
    if not _valid_consent_ref(consent.reference):
        return f"{boundary}_consent_reference_invalid"
    if not isinstance(consent.state, AudioConsentState):
        return f"{boundary}_consent_invalid"
    granted_at = _utc(consent.granted_at)
    expires_at = _utc(consent.expires_at)
    if granted_at is None or expires_at is None or expires_at <= granted_at:
        return f"{boundary}_consent_invalid"
    if consent.state is AudioConsentState.REVOKED:
        return f"{boundary}_consent_revoked"
    if consent.state is AudioConsentState.STALE or expires_at <= current:
        return f"{boundary}_consent_stale"
    if granted_at > current + timedelta(seconds=clock_skew_seconds):
        return f"{boundary}_consent_not_current"
    if boundary == "capture" and capture_at is not None:
        if granted_at > capture_at + timedelta(seconds=clock_skew_seconds):
            return "capture_consent_after_capture"
        if expires_at <= capture_at:
            return "capture_consent_expired_before_capture"
    return None


def _result(
    status: AudioIngressStatus,
    reason_code: str,
    *,
    request_digest: str | None = None,
) -> AudioIngressResult:
    result = AudioIngressResult(
        status=status,
        reason_code=reason_code,
        accepted=status is AudioIngressStatus.ACCEPTED,
        retryable=status is AudioIngressStatus.DEGRADED,
        request_digest=request_digest,
    )
    object.__setattr__(result, "_provenance", _TRUSTED_RESULT_TOKEN)
    return result


def validate_audio_ingress(
    request: AudioIngressRequest,
    *,
    policy: AudioIngressPolicy | None = None,
    known_identities: tuple[AudioRequestIdentity, ...] = (),
    now: datetime | None = None,
) -> AudioIngressResult:
    """Purely validate one PTT request against policy and retry metadata.

    ``known_identities`` is a read-only lookup snapshot supplied by the
    canonical session/message/outbox owner.  This function never writes it.
    ``now`` is injectable for deterministic tests; an omitted value uses UTC
    solely as a convenience for adapters.
    """

    effective_policy = policy if policy is not None else AudioIngressPolicy()
    structure_error = _request_structure_error(request, effective_policy)
    if structure_error:
        return _result(
            AudioIngressStatus.BLOCKED,
            structure_error,
            request_digest=None,
        )
    current = _now(now)
    if current is None:
        return _result(AudioIngressStatus.BLOCKED, "invalid_validation_clock")
    captured_at = _utc(request.captured_at)
    assert captured_at is not None
    if captured_at > current + timedelta(seconds=effective_policy.clock_skew_seconds):
        return _result(AudioIngressStatus.BLOCKED, "capture_timestamp_in_future")
    request_digest = _canonical_request_digest(request)
    if not isinstance(known_identities, tuple) or any(not _valid_identity(identity) for identity in known_identities):
        return _result(AudioIngressStatus.BLOCKED, "identity_lookup_invalid", request_digest=request_digest)
    for identity in known_identities:
        if identity.request_id == request.request_id:
            if identity.request_digest == request_digest:
                return _result(AudioIngressStatus.DUPLICATE, "request_already_recorded", request_digest=request_digest)
            return _result(AudioIngressStatus.BLOCKED, "request_identity_conflict", request_digest=request_digest)
        if identity.attachment_id == request.attachment_id:
            return _result(AudioIngressStatus.BLOCKED, "attachment_identity_conflict", request_digest=request_digest)

    capture_consent_error = _consent_reason(
        request.capture_consent,
        boundary="capture",
        current=current,
        clock_skew_seconds=effective_policy.clock_skew_seconds,
        capture_at=captured_at,
    )
    if capture_consent_error:
        return _result(AudioIngressStatus.BLOCKED, capture_consent_error, request_digest=request_digest)
    cloud_consent_error = _consent_reason(
        request.cloud_upload_consent,
        boundary="cloud_upload",
        current=current,
        clock_skew_seconds=effective_policy.clock_skew_seconds,
    )
    if cloud_consent_error:
        return _result(AudioIngressStatus.BLOCKED, cloud_consent_error, request_digest=request_digest)
    assert request.capture_consent is not None
    assert request.cloud_upload_consent is not None
    if request.capture_consent.reference == request.cloud_upload_consent.reference:
        return _result(AudioIngressStatus.BLOCKED, "consent_references_not_separate", request_digest=request_digest)

    retention_deadline = _utc(request.raw_audio_retention_deadline)
    if retention_deadline is None:
        return _result(AudioIngressStatus.BLOCKED, "raw_audio_retention_deadline_missing", request_digest=request_digest)
    if retention_deadline <= current:
        return _result(AudioIngressStatus.BLOCKED, "raw_audio_retention_expired", request_digest=request_digest)
    if retention_deadline <= captured_at:
        return _result(AudioIngressStatus.BLOCKED, "raw_audio_retention_before_capture", request_digest=request_digest)
    max_deadline = captured_at + timedelta(seconds=effective_policy.max_raw_retention_seconds)
    if retention_deadline > max_deadline:
        return _result(AudioIngressStatus.BLOCKED, "raw_audio_retention_exceeds_limit", request_digest=request_digest)

    if request.requested_capability not in effective_policy.allowed_capabilities:
        return _result(AudioIngressStatus.BLOCKED, "requested_capability_not_allowed", request_digest=request_digest)
    if request.requested_capability not in {"chat", "transcription"}:
        if not request.transcript_confirmed or not _valid_consent_ref(request.transcript_confirmation_ref):
            return _result(AudioIngressStatus.BLOCKED, "transcript_confirmation_required", request_digest=request_digest)

    if effective_policy.provider_status is AudioProviderStatus.UNAVAILABLE:
        return _result(AudioIngressStatus.DEGRADED, "openrouter_route_unavailable", request_digest=request_digest)
    if effective_policy.provider_status is AudioProviderStatus.UNVERIFIED:
        return _result(AudioIngressStatus.DEGRADED, "openrouter_capability_unverified", request_digest=request_digest)
    if not all(
        (
            effective_policy.trusted_adapter_id,
            effective_policy.provider_proof_reference,
            effective_policy.consent_proof_reference,
        )
    ):
        return _result(AudioIngressStatus.DEGRADED, "trusted_adapter_proof_required", request_digest=request_digest)
    return _result(AudioIngressStatus.ACCEPTED, "audio_ingress_preflight_accepted", request_digest=request_digest)


def build_openrouter_input_audio(request: AudioIngressRequest) -> OpenRouterInputAudio:
    """Shape, but do not send, the documented OpenRouter audio content item."""

    if not isinstance(request, AudioIngressRequest):
        raise TypeError("request must be an AudioIngressRequest")
    return OpenRouterInputAudio(data=request.audio_base64, format=request.container)


def _empty_receipt_request_metadata() -> dict[str, Any]:
    """Return the redacted shape used when request metadata is not trusted."""

    return {
        "session_id": None,
        "message_id": None,
        "attachment_id": None,
        "request_id": None,
        "captured_at": None,
        "audio_payload_digest": None,
        "audio_size_bytes": None,
        "duration_seconds": None,
        "media_type": None,
        "container": None,
        "codec": None,
        "normalized_wav_size_bytes": None,
        "stream_count": None,
        "capture_consent_reference": None,
        "cloud_upload_consent_reference": None,
        "raw_audio_retention_deadline": None,
        "requested_capability": None,
        "transcript_confirmed": False,
        "provider": None,
        "local_fallback_claimed": False,
    }


def _safe_receipt_request_metadata(
    request: AudioIngressRequest,
    *,
    policy: AudioIngressPolicy,
    reason_code: str,
    include_request_fields: bool,
) -> dict[str, Any]:
    """Copy only bounded request fields after structural validation.

    Receipts are also built for blocked requests, so they must not treat a
    typed dataclass as proof that caller-supplied values are safe.  Blocked or
    malformed requests are fully redacted.  For an accepted/degraded or
    duplicate result with a structurally valid request, IDs and bounded media
    fields are copied only after their allowlists/ranges pass; capability and
    retention fields receive their own semantic checks.
    """

    redacted = _empty_receipt_request_metadata()
    if not include_request_fields:
        return redacted
    if not isinstance(request, AudioIngressRequest) or not _valid_policy(policy):
        return redacted
    if _request_structure_error(request, policy) is not None:
        return redacted

    captured_at = _utc(request.captured_at)
    if captured_at is None:
        return redacted
    retention_deadline = _utc(request.raw_audio_retention_deadline)
    if retention_deadline is not None:
        max_deadline = captured_at + timedelta(seconds=policy.max_raw_retention_seconds)
        if retention_deadline <= captured_at or retention_deadline > max_deadline:
            retention_deadline = None

    capability = (
        request.requested_capability
        if request.requested_capability in policy.allowed_capabilities
        else None
    )
    # A future capture is a semantic validation failure.  Do not repeat the
    # untrusted timestamp in its receipt even though it has a valid datetime
    # shape.
    safe_captured_at = None if reason_code == "capture_timestamp_in_future" else _iso(captured_at)
    redacted.update(
        {
            "session_id": request.session_id,
            "message_id": request.message_id,
            "attachment_id": request.attachment_id,
            "request_id": request.request_id,
            "captured_at": safe_captured_at,
            "audio_payload_digest": _safe_payload_digest(request),
            "audio_size_bytes": request.audio_size_bytes,
            "duration_seconds": request.duration_seconds,
            "media_type": request.media_type,
            "container": request.container,
            "codec": request.codec,
            "normalized_wav_size_bytes": request.normalized_wav_size_bytes,
            "stream_count": request.stream_count,
            "capture_consent_reference": _consent_reference(request.capture_consent),
            "cloud_upload_consent_reference": _consent_reference(request.cloud_upload_consent),
            "raw_audio_retention_deadline": _iso(retention_deadline),
            "requested_capability": capability,
            "transcript_confirmed": request.transcript_confirmed,
            "provider": OPENROUTER_PROVIDER,
            "local_fallback_claimed": False,
        }
    )
    return redacted


def _trusted_result_for_receipt(
    result: AudioIngressResult,
    request: AudioIngressRequest | None,
) -> AudioIngressResult:
    """Fail closed when a result is forged or bound to another request."""

    if (
        isinstance(result, AudioIngressResult)
        and result._provenance is _TRUSTED_RESULT_TOKEN
        and isinstance(result.status, AudioIngressStatus)
        and isinstance(result.accepted, bool)
        and isinstance(result.retryable, bool)
        and result.accepted is (result.status is AudioIngressStatus.ACCEPTED)
        and result.retryable is (result.status is AudioIngressStatus.DEGRADED)
    ):
        canonical_digest = _safe_canonical_request_digest(request)
        supplied_digest = _safe_request_digest(result.request_digest)
        if result.status is not AudioIngressStatus.BLOCKED:
            if canonical_digest is None or supplied_digest != canonical_digest:
                return _result(AudioIngressStatus.BLOCKED, "invalid_result_provenance")
        elif result.request_digest is not None and (
            canonical_digest is None or supplied_digest != canonical_digest
        ):
            return _result(AudioIngressStatus.BLOCKED, "invalid_result_provenance")
        return result
    return _result(AudioIngressStatus.BLOCKED, "invalid_result_provenance")


def serialize_audio_ingress_receipt(
    request: AudioIngressRequest | None,
    result: AudioIngressResult,
    *,
    policy: AudioIngressPolicy | None = None,
) -> AudioIngressReceipt:
    """Return an immutable receipt with no base64 payload or transcript."""

    if not isinstance(result, AudioIngressResult):
        raise TypeError("result must be an AudioIngressResult")
    safe_result = _trusted_result_for_receipt(result, request)
    effective_policy = policy if policy is not None else AudioIngressPolicy()
    safe_reason_code = _safe_reason_code(safe_result.reason_code)
    if request is None:
        return AudioIngressReceipt(
            schema_version=AUDIO_INGRESS_RECEIPT_SCHEMA_VERSION,
            status=safe_result.status,
            reason_code=safe_reason_code,
            request_digest=None,
            session_id=None,
            message_id=None,
            attachment_id=None,
            request_id=None,
            captured_at=None,
            audio_payload_digest=None,
            audio_size_bytes=None,
            duration_seconds=None,
            media_type=None,
            container=None,
            codec=None,
            normalized_wav_size_bytes=None,
            stream_count=None,
            capture_consent_reference=None,
            cloud_upload_consent_reference=None,
            raw_audio_retention_deadline=None,
            requested_capability=None,
            transcript_confirmed=False,
            provider=None,
            provider_status=effective_policy.provider_status if _valid_policy(effective_policy) else None,
            trusted_adapter_id=effective_policy.trusted_adapter_id if _valid_policy(effective_policy) else None,
            provider_proof_reference=effective_policy.provider_proof_reference if _valid_policy(effective_policy) else None,
            consent_proof_reference=effective_policy.consent_proof_reference if _valid_policy(effective_policy) else None,
        )
    metadata = _safe_receipt_request_metadata(
        request,
        policy=effective_policy,
        reason_code=safe_reason_code,
        include_request_fields=safe_result.status is not AudioIngressStatus.BLOCKED,
    )
    policy_valid = _valid_policy(effective_policy)
    return AudioIngressReceipt(
        schema_version=AUDIO_INGRESS_RECEIPT_SCHEMA_VERSION,
        status=safe_result.status,
        reason_code=safe_reason_code,
        request_digest=_receipt_request_digest(request, safe_result),
        session_id=metadata["session_id"],
        message_id=metadata["message_id"],
        attachment_id=metadata["attachment_id"],
        request_id=metadata["request_id"],
        captured_at=metadata["captured_at"],
        audio_payload_digest=metadata["audio_payload_digest"],
        audio_size_bytes=metadata["audio_size_bytes"],
        duration_seconds=metadata["duration_seconds"],
        media_type=metadata["media_type"],
        container=metadata["container"],
        codec=metadata["codec"],
        normalized_wav_size_bytes=metadata["normalized_wav_size_bytes"],
        stream_count=metadata["stream_count"],
        capture_consent_reference=metadata["capture_consent_reference"],
        cloud_upload_consent_reference=metadata["cloud_upload_consent_reference"],
        raw_audio_retention_deadline=metadata["raw_audio_retention_deadline"],
        requested_capability=metadata["requested_capability"],
        transcript_confirmed=metadata["transcript_confirmed"],
        provider=metadata["provider"],
        provider_status=effective_policy.provider_status if policy_valid else None,
        trusted_adapter_id=effective_policy.trusted_adapter_id if policy_valid else None,
        provider_proof_reference=effective_policy.provider_proof_reference if policy_valid else None,
        consent_proof_reference=effective_policy.consent_proof_reference if policy_valid else None,
        local_fallback_claimed=metadata["local_fallback_claimed"],
    )


def build_audio_ingress_receipt(
    request: AudioIngressRequest | None,
    result: AudioIngressResult,
    *,
    policy: AudioIngressPolicy | None = None,
) -> AudioIngressReceipt:
    """Compatibility alias for receipt-oriented callers."""

    return serialize_audio_ingress_receipt(request, result, policy=policy)


__all__ = [
    "AUDIO_INGRESS_RECEIPT_SCHEMA_VERSION",
    "AUDIO_INGRESS_SCHEMA_VERSION",
    "AudioConsent",
    "AudioConsentState",
    "AudioFormatRule",
    "AudioIngressPolicy",
    "AudioIngressReceipt",
    "AudioIngressRequest",
    "AudioIngressResult",
    "AudioIngressStatus",
    "AudioPolicy",
    "AudioProviderStatus",
    "AudioReceipt",
    "AudioRequest",
    "AudioRequestIdentity",
    "DEFAULT_AUDIO_CAPABILITIES",
    "DEFAULT_AUDIO_FORMATS",
    "MAX_AUDIO_BYTES",
    "MAX_AUDIO_DURATION_SECONDS",
    "MAX_NORMALIZED_WAV_BYTES",
    "MAX_RAW_AUDIO_RETENTION_SECONDS",
    "OPENROUTER_CHAT_COMPLETIONS_ENDPOINT",
    "OPENROUTER_PROVIDER",
    "OpenRouterInputAudio",
    "build_audio_ingress_receipt",
    "build_openrouter_input_audio",
    "canonical_audio_request_digest",
    "serialize_audio_ingress_receipt",
    "validate_audio_ingress",
]
