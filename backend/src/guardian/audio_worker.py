"""Local, bounded push-to-talk audio processing.

The worker owns the byte lifecycle for the #751 vertical slice.  It accepts
bytes from an already authenticated caller, puts them in a private quarantine
directory, validates and converts them to a small mono 16 kHz PCM16 WAV, and
then calls an explicitly injected *intercepted* transport under the shared
remote-inference admission lane.  The default worker has no transport and
therefore cannot make a provider request.

Raw and normalized paths are private implementation state.  Only hashes,
format metadata, bounded status, and a confirmed transcript cross into
canonical storage.  A transcript is never a capability input until the
operator confirms its current digest.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import re
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import resource
import shutil
import stat
import subprocess
import tempfile
from typing import Any, Awaitable, Callable, Protocol
import uuid
import wave
import weakref

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from src.agent.session import MessageIngressConflictError, session_manager
from src.conversation.identity import (
    ConversationIdentityError,
    build_conversation_identity,
    build_lineage,
    issue_attachment_quarantine_receipt,
    validate_attachment_refs,
)
from src.db.engine import get_session
from src.db.models import AudioConsentGrant, AudioIngressJob
from src.security.trust_contract import AuthorityGrant, TrustPrincipal
from src.auth.service import AuthFailure, authenticate_session
from src.model_fabric.remote_inference_admission import (
    RemoteInferenceAdmissionBroker,
    remote_inference_admission_broker,
)
from src.model_fabric.gpu_admission import GpuAdmissionError, GpuAdmissionRequest, GpuPriority
from src.guardian.audio_ingress import (
    AudioConsent,
    AudioConsentState,
    AudioIngressPolicy,
    AudioIngressRequest,
    AudioProviderStatus,
    AudioIngressStatus,
    _build_server_owned_audio_consent,
    canonical_audio_request_digest,
    validate_audio_ingress,
)
from src.guardian.multimodal_voice import guardian_safe_multimodal_voice_policy_payload


RAW_AUDIO_MAX_BYTES = 10 * 1024 * 1024
MAX_AUDIO_SECONDS = 60.0
NORMALIZED_WAV_MAX_BYTES = 2 * 1024 * 1024
RAW_RETENTION = timedelta(minutes=15)
TRANSCRIPT_MAX_CHARS = 20_000
DECODER_TIMEOUT_SECONDS = 8.0
DECODER_CPU_SECONDS = 5
DECODER_MEMORY_BYTES = 256 * 1024 * 1024
DECODER_STDERR_MAX_BYTES = 64 * 1024
AUDIO_WORKER_SCHEMA_VERSION = "seraph.guardian.audio-worker.v1"
AUDIO_CAPABILITY_VERSION = "guardian.audio.transcription.v1"
_AUDIO_ID_MAX = 256
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_AUDIO_STATUS = frozenset(
    {
        "queued",
        "processing",
        "transcript_ready",
        "confirming",
        "confirming_reserved",
        "confirmed",
        "degraded",
        "failed",
        "blocked",
        "cancelled",
    }
)
_CONSENT_BOUNDARIES = frozenset({"capture", "cloud_upload"})
_CONSENT_REFERENCE_RE = re.compile(r"^audio-consent:(capture|cloud_upload):[0-9a-f]{32}$")
_AUDIO_WORKERS: weakref.WeakSet["AudioIngressWorker"] = weakref.WeakSet()


class AudioWorkerError(RuntimeError):
    """Bounded, operator-safe audio processing failure."""

    def __init__(self, code: str, message: str = "audio processing failed") -> None:
        self.code = code
        super().__init__(message)


class AudioTransportUnavailable(AudioWorkerError):
    def __init__(self) -> None:
        super().__init__("audio_transport_unavailable", "no intercepted audio transport is configured")


class AudioTransportMalformed(AudioWorkerError):
    def __init__(self) -> None:
        super().__init__("audio_transport_malformed", "intercepted audio transport returned no bounded transcript")


class _AudioTransportBoundaryBlocked(AudioWorkerError):
    """A final durable authority/consent check denied the transport callback."""


class AudioConfirmationConflict(AudioWorkerError):
    def __init__(self, code: str = "transcript_confirmation_stale") -> None:
        super().__init__(code, "transcript confirmation does not match the current transcript")


class AudioTransport(Protocol):
    """Test-only transport seam; implementations must not perform networking."""

    intercepted: bool

    async def transcribe(
        self,
        normalized_wav: bytes,
        *,
        request_id: str,
        session_id: str,
        audio_digest: str,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class InterceptedAudioTransport:
    """A deterministic response seam for tests and local integration checks."""

    response: object | Callable[..., object | Awaitable[object]]
    intercepted: bool = True

    async def transcribe(
        self,
        normalized_wav: bytes,
        *,
        request_id: str,
        session_id: str,
        audio_digest: str,
    ) -> object:
        if callable(self.response):
            try:
                value = self.response(
                    normalized_wav=normalized_wav,
                    request_id=request_id,
                    session_id=session_id,
                    audio_digest=audio_digest,
                )
            except TypeError as first_error:
                # Preserve the tiny zero-argument fixture seam used by older
                # local tests while preferring the richer bounded receipt.
                try:
                    value = self.response()
                except TypeError:
                    raise first_error
            if hasattr(value, "__await__"):
                return await value  # type: ignore[misc]
            return value
        return self.response


@dataclass(frozen=True, slots=True)
class AudioUploadRequest:
    """Server-bound upload metadata; owner identity is supplied by auth."""

    session_id: str
    owner_principal_id: str
    operator_session_id: str | None
    audio_bytes: bytes
    captured_at: datetime
    capture_consent: AudioConsent
    model_consent: AudioConsent | None
    message_id: str | None = None
    attachment_id: str | None = None
    request_id: str | None = None
    requested_capability: str = "chat"
    channel: str = "web"
    transport: str = "rest"
    # API callers must set this from the authenticated operator principal.  A
    # missing value is persisted as denied and can never authorize processing.
    model_inference_granted: bool | None = None

    def __post_init__(self) -> None:
        for field_name in ("session_id", "owner_principal_id"):
            value = str(getattr(self, field_name) or "").strip()
            if not value or len(value) > _AUDIO_ID_MAX:
                raise ValueError(f"{field_name} is required and bounded")
            object.__setattr__(self, field_name, value)
        if self.operator_session_id is None:
            raise ValueError("operator_session_id is required")
        value = str(self.operator_session_id).strip()
        if not value or len(value) > _AUDIO_ID_MAX:
            raise ValueError("operator_session_id is invalid")
        object.__setattr__(self, "operator_session_id", value)
        if not isinstance(self.audio_bytes, bytes) or not self.audio_bytes:
            raise ValueError("audio_bytes must be non-empty bytes")
        for field_name in ("message_id", "attachment_id", "request_id"):
            value = getattr(self, field_name)
            if value is not None:
                normalized = str(value).strip()
                if not normalized or len(normalized) > _AUDIO_ID_MAX:
                    raise ValueError(f"{field_name} is invalid")
                object.__setattr__(self, field_name, normalized)
        if not isinstance(self.captured_at, datetime) or self.captured_at.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware")
        if (
            not isinstance(self.requested_capability, str)
            or not 1 <= len(self.requested_capability) <= 32
            or any(char.isspace() for char in self.requested_capability)
        ):
            raise ValueError("requested_capability is invalid")
        if self.channel != "web" or self.transport not in {"rest", "websocket"}:
            raise ValueError("audio upload must use the web ingress transport")
        if self.model_inference_granted is not None and not isinstance(self.model_inference_granted, bool):
            raise ValueError("model_inference_granted must be a boolean")


@dataclass(frozen=True, slots=True)
class AudioJobSnapshot:
    id: str
    request_id: str
    request_digest: str
    owner_principal_id: str
    operator_session_id: str | None
    session_id: str
    message_id: str
    attachment_id: str
    requested_capability: str
    status: str
    audio_payload_digest: str
    audio_size_bytes: int
    duration_seconds: float
    decoded_duration_seconds: float | None
    normalized_wav_size_bytes: int | None
    media_type: str
    container: str
    codec: str
    sample_rate_hz: int
    channels: int
    capture_consent_reference: str
    model_consent_reference: str
    raw_audio_retention_deadline: datetime
    transcript: str | None
    transcript_digest: str | None
    confirmed_transcript_digest: str | None
    result_digest: str | None
    error_code: str | None
    admission_operation_id: str | None
    provider_status: str
    transport_status: str
    duplicate: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": AUDIO_WORKER_SCHEMA_VERSION,
            "id": self.id,
            "request_id": self.request_id,
            "request_digest": self.request_digest,
            "owner_principal_id": self.owner_principal_id,
            "operator_session_id": self.operator_session_id,
            "session_id": self.session_id,
            "message_id": self.message_id,
            "attachment_id": self.attachment_id,
            "requested_capability": self.requested_capability,
            "status": self.status,
            "capture": {
                "audio_payload_digest": self.audio_payload_digest,
                "audio_size_bytes": self.audio_size_bytes,
                "duration_seconds": self.duration_seconds,
                "decoded_duration_seconds": self.decoded_duration_seconds,
                "media_type": self.media_type,
                "container": self.container,
                "codec": self.codec,
                "sample_rate_hz": self.sample_rate_hz,
                "channels": self.channels,
                "normalized_wav_size_bytes": self.normalized_wav_size_bytes,
                "raw_audio_retention_deadline": self.raw_audio_retention_deadline.isoformat(),
            },
            "consent": {
                "capture_reference": self.capture_consent_reference,
                "model_reference": self.model_consent_reference,
            },
            "transcript": {
                # The digest is the confirmation handle; the unconfirmed text
                # itself is intentionally process-local and redacted.
                "available": self.transcript_digest is not None,
                "digest": self.transcript_digest,
                "confirmed_digest": self.confirmed_transcript_digest,
            },
            "result_digest": self.result_digest,
            "error_code": self.error_code,
            "admission_operation_id": self.admission_operation_id,
            "duplicate": self.duplicate,
            "privacy": {
                "raw_audio_in_receipt": False,
                "raw_audio_persisted_after_cleanup": False,
                "transcript_in_receipt": False,
            },
            "provider": {
                "provider_status": self.provider_status,
                "transport": self.transport_status,
                "live_provider_call_claimed": False,
                "local_model_fallback_claimed": False,
            },
        }


@dataclass(frozen=True, slots=True)
class _DecodedAudio:
    media_type: str
    container: str
    codec: str
    source_duration_seconds: float
    source_sample_rate_hz: int
    source_channels: int
    normalized_duration_seconds: float
    normalized_size_bytes: int


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_request_id(value: str | None) -> str | None:
    """Validate a caller retry key before any path operation is attempted."""
    if value is None:
        return None
    normalized = str(value).strip()
    # The retry key is opaque metadata, never a filesystem component.  Keep
    # the allow-list intentionally narrower than the conversation identity
    # contract so both POSIX and Windows path tricks fail closed.
    if (
        not _REQUEST_ID_RE.fullmatch(normalized)
        or "/" in normalized
        or "\\" in normalized
        or ".." in normalized
        or Path(normalized).is_absolute()
    ):
        raise AudioWorkerError("invalid_request_id", "audio request identity is invalid")
    return normalized


def _validate_supplied_identity(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if (
        not normalized
        or len(normalized) > _AUDIO_ID_MAX
        or "\x00" in normalized
        or "/" in normalized
        or "\\" in normalized
    ):
        raise AudioWorkerError("invalid_server_owned_id", f"{field} must be a server-owned identity")
    return normalized


def _derive_audio_identity(kind: str, *, owner: str, session_id: str, material: str) -> str:
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"seraph-audio:{kind}:{owner}:{session_id}:{material}",
    ).hex


def _serialize_consent(consent: AudioConsent | object | None) -> dict[str, str] | None:
    if consent is None:
        return None
    reference = getattr(consent, "reference", None)
    if not isinstance(reference, str) or not reference:
        return None
    # Request seams may carry only a reference.  The durable worker grant is
    # the sole source for state/timestamps; retaining those fields here is
    # limited to trusted AudioConsent values already read by this worker.
    if not isinstance(consent, AudioConsent):
        return {"reference": reference}
    try:
        state = consent.state.value
    except AttributeError:
        state = str(consent.state)
    return {
        "reference": reference,
        "state": state,
        "granted_at": _utc(consent.granted_at).isoformat(),
        "expires_at": _utc(consent.expires_at).isoformat(),
    }


def _deserialize_consent(value: object) -> AudioConsent | None:
    if not isinstance(value, dict):
        return None
    try:
        reference = str(value["reference"])
        state = AudioConsentState(str(value["state"]))
        granted_at = datetime.fromisoformat(str(value["granted_at"]))
        expires_at = datetime.fromisoformat(str(value["expires_at"]))
        return AudioConsent(
            reference=reference,
            state=state,
            granted_at=_utc(granted_at),
            expires_at=_utc(expires_at),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _bound_request_digest(
    request: AudioUploadRequest,
    ingress: AudioIngressRequest,
    *,
    capture_consent: AudioConsent | object | None = None,
    model_consent: AudioConsent | object | None = None,
) -> str:
    """Bind the immutable ingress digest to authenticated owner metadata."""
    capture = request.capture_consent if capture_consent is None else capture_consent
    model = request.model_consent if model_consent is None else model_consent
    payload = {
        "canonical_ingress_digest": canonical_audio_request_digest(ingress),
        "owner_principal_id": request.owner_principal_id,
        "operator_session_id": request.operator_session_id,
        "model_inference_granted": request.model_inference_granted,
        "capture_consent": _serialize_consent(capture),
        "model_consent": _serialize_consent(model),
    }
    return _digest_text(_bounded_json(payload))


def _bounded_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _decoder_preexec() -> None:
    """Apply #747-style resource bounds before a local decoder starts."""
    if os.name != "posix":
        return
    for limit_name, requested in (
        (resource.RLIMIT_CPU, DECODER_CPU_SECONDS),
        (resource.RLIMIT_AS, DECODER_MEMORY_BYTES),
        (resource.RLIMIT_FSIZE, NORMALIZED_WAV_MAX_BYTES),
    ):
        try:
            soft, hard = resource.getrlimit(limit_name)
            if hard == resource.RLIM_INFINITY:
                hard = requested
            resource.setrlimit(limit_name, (min(requested, hard), hard))
        except (AttributeError, OSError, ValueError):
            continue


def _safe_decoder_env() -> dict[str, str]:
    return {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"}


def _media_type_for_bytes(payload: bytes) -> tuple[str, str, str] | None:
    if payload[:4] == b"RIFF" and payload[8:12] == b"WAVE":
        return "audio/wav", "wav", "pcm_s16le"
    if payload[:4] == b"OggS":
        return "audio/ogg", "ogg", "opus"
    if payload[:4] == b"\x1a\x45\xdf\xa3":
        return "audio/webm", "webm", "opus"
    if payload[:3] == b"ID3" or (len(payload) > 1 and payload[0] == 0xFF and payload[1] & 0xE0 == 0xE0):
        return "audio/mpeg", "mp3", "mp3"
    if len(payload) >= 12 and payload[4:8] == b"ftyp":
        return "audio/mp4", "m4a", "aac"
    return None


def _read_wav_and_write_normalized(source: Path, normalized: Path) -> _DecodedAudio:
    try:
        with wave.open(str(source), "rb") as stream:
            channels = int(stream.getnchannels())
            sample_width = int(stream.getsampwidth())
            sample_rate = int(stream.getframerate())
            frames = int(stream.getnframes())
            if channels not in {1, 2}:
                raise AudioWorkerError("invalid_channel_count", "WAV channel count is not supported")
            if sample_width != 2:
                raise AudioWorkerError("audio_codec_rejected", "only PCM16 WAV input is supported")
            if sample_rate < 8_000 or sample_rate > 192_000:
                raise AudioWorkerError("invalid_sample_rate", "WAV sample rate is outside the bound")
            duration = frames / sample_rate if sample_rate else 0.0
            if not math.isfinite(duration) or duration <= 0 or duration > MAX_AUDIO_SECONDS:
                raise AudioWorkerError("duration_exceeds_limit", "WAV duration is outside the bound")
            # The source size bound and frame-count bound are both checked before
            # reading frames, preventing a corrupt header from causing an
            # unbounded allocation.
            raw = stream.readframes(min(frames, int(MAX_AUDIO_SECONDS * sample_rate)))
            if len(raw) > RAW_AUDIO_MAX_BYTES:
                raise AudioWorkerError("audio_size_exceeds_limit", "decoded source exceeds the bound")
    except AudioWorkerError:
        raise
    except (OSError, EOFError, wave.Error, ValueError) as exc:
        raise AudioWorkerError("audio_decode_failed", "WAV decoding failed") from exc

    try:
        import audioop

        if channels == 2:
            mono = audioop.tomono(raw, 2, 0.5, 0.5)
        else:
            mono = raw
        if sample_rate != 16_000:
            mono, _ = audioop.ratecv(mono, 2, 1, sample_rate, 16_000, None)
    except (audioop.error, ValueError, TypeError) as exc:
        raise AudioWorkerError("audio_decode_failed", "PCM conversion failed") from exc

    try:
        with wave.open(str(normalized), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16_000)
            output.writeframes(mono)
        size = normalized.stat().st_size
        if size > NORMALIZED_WAV_MAX_BYTES:
            raise AudioWorkerError("normalized_wav_size_exceeds_limit", "normalized WAV exceeds the bound")
        with wave.open(str(normalized), "rb") as check:
            check_channels = check.getnchannels()
            check_width = check.getsampwidth()
            check_rate = check.getframerate()
            check_frames = check.getnframes()
        normalized_duration = check_frames / check_rate if check_rate else 0.0
        if (check_channels, check_width, check_rate) != (1, 2, 16_000):
            raise AudioWorkerError("normalized_wav_invalid", "normalized WAV shape is invalid")
        if not 0 < normalized_duration <= MAX_AUDIO_SECONDS:
            raise AudioWorkerError("duration_exceeds_limit", "normalized WAV duration is outside the bound")
    except AudioWorkerError:
        raise
    except (OSError, wave.Error, ValueError) as exc:
        raise AudioWorkerError("normalized_wav_invalid", "normalized WAV validation failed") from exc
    return _DecodedAudio(
        media_type="audio/wav",
        container="wav",
        codec="pcm_s16le",
        source_duration_seconds=duration,
        source_sample_rate_hz=sample_rate,
        source_channels=channels,
        normalized_duration_seconds=normalized_duration,
        normalized_size_bytes=size,
    )


async def _run_process(argv: list[str], *, timeout: float) -> tuple[int, bytes, bytes]:
    """Run a fixed local decoder command with a process-group cancellation bound."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_safe_decoder_env(),
            start_new_session=True,
            preexec_fn=_decoder_preexec if os.name == "posix" else None,
        )
    except (OSError, ValueError) as exc:
        raise AudioWorkerError("decoder_unavailable", "bounded local decoder is unavailable") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        raise AudioWorkerError("decoder_timeout", "bounded decoder exceeded its time limit")
    except asyncio.CancelledError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        raise
    return process.returncode or 0, stdout[:DECODER_STDERR_MAX_BYTES], stderr[:DECODER_STDERR_MAX_BYTES]


async def _decode_with_ffmpeg(source: Path, normalized: Path, source_format: tuple[str, str, str]) -> _DecodedAudio:
    """Decode one compressed local file using a fixed, no-network command."""
    probe = [
        "/usr/bin/ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=index,codec_name,channels,sample_rate,duration",
        "-of",
        "json",
        str(source),
    ]
    try:
        code, stdout, _ = await _run_process(probe, timeout=DECODER_TIMEOUT_SECONDS)
        data = json.loads(stdout.decode("utf-8", "replace")) if code == 0 else {}
        streams = data.get("streams") if isinstance(data, dict) else None
        if not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], dict):
            raise AudioWorkerError("audio_stream_rejected", "audio must contain exactly one stream")
        stream = streams[0]
        channels = int(stream.get("channels") or 0)
        sample_rate = int(stream.get("sample_rate") or 0)
        duration = float(stream.get("duration") or 0.0)
        if channels not in {1, 2} or sample_rate < 8_000 or sample_rate > 192_000:
            raise AudioWorkerError("audio_stream_rejected", "audio stream shape is outside the bound")
        if not math.isfinite(duration) or duration <= 0 or duration > MAX_AUDIO_SECONDS:
            raise AudioWorkerError("duration_exceeds_limit", "audio duration is outside the bound")
    except AudioWorkerError:
        raise
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise AudioWorkerError("audio_decode_failed", "audio stream probing failed") from exc

    command = [
        "/usr/bin/ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-threads",
        "1",
        "-filter_threads",
        "1",
        "-filter_complex_threads",
        "1",
        "-t",
        str(MAX_AUDIO_SECONDS),
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-sample_fmt",
        "s16",
        "-f",
        "wav",
        "-fs",
        str(NORMALIZED_WAV_MAX_BYTES),
        "-y",
        str(normalized),
    ]
    code, _, _ = await _run_process(command, timeout=DECODER_TIMEOUT_SECONDS)
    if code != 0:
        raise AudioWorkerError("audio_decode_failed", "bounded decoder rejected the audio")
    try:
        with wave.open(str(normalized), "rb") as check:
            checked = (check.getnchannels(), check.getsampwidth(), check.getframerate(), check.getnframes())
        size = normalized.stat().st_size
    except (OSError, wave.Error, ValueError) as exc:
        raise AudioWorkerError("normalized_wav_invalid", "decoder output is not a valid WAV") from exc
    if checked[:3] != (1, 2, 16_000) or size > NORMALIZED_WAV_MAX_BYTES:
        raise AudioWorkerError("normalized_wav_invalid", "decoder output shape is outside the bound")
    normalized_duration = checked[3] / 16_000
    if not 0 < normalized_duration <= MAX_AUDIO_SECONDS:
        raise AudioWorkerError("duration_exceeds_limit", "decoder output duration is outside the bound")
    return _DecodedAudio(
        media_type=source_format[0],
        container=source_format[1],
        codec=source_format[2],
        source_duration_seconds=duration,
        source_sample_rate_hz=sample_rate,
        source_channels=channels,
        normalized_duration_seconds=normalized_duration,
        normalized_size_bytes=size,
    )


class AudioIngressWorker:
    """Durable audio worker with injected transport and bounded local storage."""

    def __init__(
        self,
        *,
        transport: AudioTransport | None = None,
        admission_broker: RemoteInferenceAdmissionBroker | None = None,
        quarantine_root: Path | str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if transport is not None and not bool(getattr(transport, "intercepted", False)):
            raise ValueError("audio transport must be explicitly marked intercepted")
        self.transport = transport
        self.admission_broker = admission_broker or remote_inference_admission_broker
        configured_root = Path(quarantine_root) if quarantine_root else Path(tempfile.gettempdir()) / "seraph-audio"
        if not configured_root.is_absolute():
            raise ValueError("audio quarantine root must be an absolute server path")
        current = Path(configured_root.anchor)
        for component in configured_root.parts[1:]:
            current /= component
            try:
                info = current.lstat()
            except FileNotFoundError:
                break
            except OSError as exc:
                raise ValueError("audio quarantine root cannot be inspected") from exc
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("audio quarantine root must not contain symlinks")
        configured_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.quarantine_root = self._resolve_quarantine_root(configured_root)
        try:
            os.chmod(self.quarantine_root, 0o700)
        except OSError:
            pass
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._tasks: dict[str, asyncio.Task[AudioJobSnapshot]] = {}
        self._task_lock = asyncio.Lock()
        self._confirmation_tasks: dict[str, asyncio.Task[AudioJobSnapshot]] = {}
        self._confirmation_lock = asyncio.Lock()
        self._review_transcripts: dict[str, tuple[str, str, str]] = {}
        _AUDIO_WORKERS.add(self)
        # Runtime proof handles are generated per worker.  They are never
        # caller-controlled constants and never evidence of a live provider.
        self._trusted_adapter_id = f"intercepted-audio-transport:{uuid.uuid4().hex}"
        self._provider_proof_reference = f"runtime-provider-proof:{uuid.uuid4().hex}"
        self._consent_proof_reference = f"runtime-consent-proof:{uuid.uuid4().hex}"

    def _now(self) -> datetime:
        return _utc(self.clock())

    @staticmethod
    def _resolve_quarantine_root(path: Path) -> Path:
        """Resolve and verify the server-owned quarantine root once."""
        try:
            if path.is_symlink() or not path.is_dir():
                raise ValueError("audio quarantine root must be a real directory")
            resolved = path.resolve(strict=True)
            if resolved.is_symlink() or not resolved.is_dir():
                raise ValueError("audio quarantine root is invalid")
            return resolved
        except OSError as exc:
            raise ValueError("audio quarantine root is unavailable") from exc

    def _safe_quarantine_path(
        self,
        path: str | Path | None,
        *,
        allow_directory: bool = False,
    ) -> Path | None:
        """Resolve one persisted path only if it remains inside quarantine."""
        if path is None or str(path).strip() == "":
            return None
        candidate = Path(path)
        if not candidate.is_absolute():
            raise AudioWorkerError("quarantine_path_invalid", "audio quarantine path is not absolute")
        try:
            # Inspect the persisted spelling before resolving it.  A symlink
            # that points back into quarantine is still untrusted state and
            # must not become a read/delete capability.
            lexical = candidate.relative_to(self.quarantine_root)
            if any(component in {".", ".."} for component in lexical.parts):
                raise AudioWorkerError("quarantine_path_invalid", "audio quarantine path is not canonical")
            resolved = candidate.resolve(strict=False)
            relative = resolved.relative_to(self.quarantine_root)
        except AudioWorkerError:
            raise
        except (OSError, ValueError) as exc:
            raise AudioWorkerError("quarantine_path_invalid", "audio quarantine path is outside the root") from exc
        if not lexical.parts or len(lexical.parts) > 2 or len(relative.parts) > 2:
            raise AudioWorkerError("quarantine_path_invalid", "audio quarantine path has an invalid shape")
        if len(lexical.parts) == 1 and not allow_directory:
            raise AudioWorkerError("quarantine_path_invalid", "audio quarantine file path is invalid")
        if len(lexical.parts) == 2 and lexical.name not in {"quarantine.bin", "normalized.wav"}:
            raise AudioWorkerError("quarantine_path_invalid", "audio quarantine filename is invalid")
        current = self.quarantine_root
        for component in lexical.parts:
            current = current / component
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise AudioWorkerError("quarantine_path_invalid", "audio quarantine path cannot be inspected") from exc
            if info.st_mode & 0o170000 == 0o120000:
                raise AudioWorkerError("quarantine_path_invalid", "audio quarantine path contains a symlink")
        return resolved

    def _validated_job_paths(
        self,
        raw_path: str | Path | None,
        normalized_path: str | Path | None,
    ) -> tuple[Path, ...]:
        # Validate every path before returning any deletion target.
        return tuple(
            path
            for path in (
                self._safe_quarantine_path(raw_path),
                self._safe_quarantine_path(normalized_path),
            )
            if path is not None
        )

    @staticmethod
    def _read_quarantine_bytes(path: Path) -> bytes:
        """Read a regular quarantine file without following a leaf symlink."""
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_size < 0:
                    raise AudioWorkerError("quarantine_path_invalid", "audio quarantine file is invalid")
                with os.fdopen(descriptor, "rb", closefd=True) as handle:
                    descriptor = -1
                    return handle.read(NORMALIZED_WAV_MAX_BYTES + 1)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        except AudioWorkerError:
            raise
        except OSError as exc:
            raise AudioWorkerError("normalized_audio_missing", "normalized audio is unavailable") from exc

    def _work_dir(self, quarantine_identity: str) -> Path:
        """Create a directory from a worker-owned bounded identity only."""
        if (
            not _REQUEST_ID_RE.fullmatch(quarantine_identity)
            or "/" in quarantine_identity
            or "\\" in quarantine_identity
            or ".." in quarantine_identity
            or Path(quarantine_identity).is_absolute()
        ):
            raise AudioWorkerError("invalid_quarantine_identity", "audio quarantine identity is invalid")
        path = self.quarantine_root / quarantine_identity
        path.mkdir(mode=0o700, parents=False, exist_ok=False)
        resolved = self._safe_quarantine_path(path, allow_directory=True)
        if resolved is None:
            raise AudioWorkerError("invalid_quarantine_identity", "audio quarantine directory is invalid")
        return resolved

    @staticmethod
    def _unlink_tree(path: str | Path | None) -> None:
        if not path:
            return
        candidate = Path(path)
        try:
            if candidate.is_dir() and not candidate.is_symlink():
                shutil.rmtree(candidate)
            elif candidate.exists() or candidate.is_symlink():
                candidate.unlink(missing_ok=True)
        except OSError:
            return

    def _cleanup_job_paths(self, raw_path: str | Path | None, normalized_path: str | Path | None) -> bool:
        try:
            paths = self._validated_job_paths(raw_path, normalized_path)
        except AudioWorkerError:
            # A stale/tampered DB path must never redirect deletion.
            return False
        for path in paths:
            self._unlink_tree(path)
        parents = {path.parent for path in paths if path.parent != self.quarantine_root}
        for parent in parents:
            try:
                safe_parent = self._safe_quarantine_path(parent, allow_directory=True)
            except AudioWorkerError:
                continue
            if safe_parent is not None:
                self._unlink_tree(safe_parent)
        return True

    def review_transcript(
        self,
        request_id: str,
        *,
        owner_principal_id: str,
        operator_session_id: str,
    ) -> str | None:
        """Return one bounded, process-local transcript for its owning review.

        The review text is never persisted in the job row or canonical memory.
        Ownership is checked against the durable job snapshot before this
        process-local value crosses the authenticated API boundary.
        """
        value = self._review_transcripts.get(request_id)
        if value is None or value[:2] != (owner_principal_id, operator_session_id):
            return None
        return value[2]

    def _set_review_transcript(self, row: AudioIngressJob, transcript: str) -> None:
        self._review_transcripts[row.request_id] = (
            row.owner_principal_id,
            row.operator_session_id or "",
            transcript[:TRANSCRIPT_MAX_CHARS],
        )

    def _drop_review_transcript(self, request_id: str) -> None:
        self._review_transcripts.pop(request_id, None)

    async def _job(self, request_id: str) -> AudioIngressJob | None:
        async with get_session() as db:
            result = await db.execute(select(AudioIngressJob).where(AudioIngressJob.request_id == request_id))
            row = result.scalars().first()
            if row is not None:
                for field_name in ("captured_at", "raw_audio_retention_deadline", "created_at", "updated_at"):
                    value = getattr(row, field_name, None)
                    if isinstance(value, datetime) and value.tzinfo is None:
                        setattr(row, field_name, value.replace(tzinfo=timezone.utc))
                db.expunge(row)
            return row

    @staticmethod
    def _normalize_consent_boundary(boundary: str) -> str:
        normalized = str(boundary or "").strip()
        if normalized == "model":
            normalized = "cloud_upload"
        if normalized not in _CONSENT_BOUNDARIES:
            raise AudioWorkerError("invalid_consent_boundary", "audio consent boundary is invalid")
        return normalized

    @staticmethod
    def _principal_has_grant(
        principal: object,
        *,
        owner_principal_id: str,
        operator_session_id: str,
        required_grant: AuthorityGrant,
    ) -> bool:
        """Check one server-authenticated operator identity binding."""
        if not isinstance(principal, TrustPrincipal):
            return False
        grants = {
            str(getattr(grant, "value", grant))
            for grant in principal.grants
        }
        principal_type = str(getattr(principal.principal_type, "value", principal.principal_type))
        return bool(
            principal_type == "operator"
            and principal.authenticated
            and not principal.revoked
            and principal.principal_id == owner_principal_id
            and principal.operator_session_id == operator_session_id
            and required_grant.value in grants
        )

    async def _require_issued_consent_authority(
        self,
        *,
        owner_principal_id: str,
        operator_session_id: str,
        authority_principal: object | None,
        required_grant: AuthorityGrant,
    ) -> TrustPrincipal:
        """Require both a server principal and its current durable session.

        The worker intentionally does not treat owner/session strings, an
        intercepted transport, or a caller-provided consent object as proof of
        authority.  The API passes the authenticated principal as evidence;
        this method then re-reads the durable operator session before issuing a
        grant.
        """
        owner = str(owner_principal_id or "").strip()
        operator_session = str(operator_session_id or "").strip()
        if (
            not owner
            or not operator_session
            or not isinstance(authority_principal, TrustPrincipal)
            or not self._principal_has_grant(
                authority_principal,
                owner_principal_id=owner,
                operator_session_id=operator_session,
                required_grant=required_grant,
            )
        ):
            raise AudioWorkerError(
                "server_owned_identity_required",
                "audio consent requires a server-authenticated operator session",
            )
        try:
            current = await authenticate_session(operator_session, touch=False)
        except AuthFailure as exc:
            raise AudioWorkerError(
                "audio_operator_session_invalid",
                "audio consent requires a current operator session",
            ) from exc
        if current.session_id != operator_session or not self._principal_has_grant(
            current.principal,
            owner_principal_id=owner,
            operator_session_id=operator_session,
            required_grant=required_grant,
        ):
            raise AudioWorkerError(
                "audio_operator_authority_required",
                "audio consent authority is no longer current",
            )
        return current.principal

    async def issue_consent_grant(
        self,
        *,
        owner_principal_id: str,
        operator_session_id: str,
        boundary: str,
        authority_principal: object | None = None,
        now: datetime | None = None,
        ttl: timedelta = RAW_RETENTION,
    ) -> AudioConsent:
        """Issue a grant only for a current server-authenticated operator."""
        boundary = self._normalize_consent_boundary(boundary)
        owner = str(owner_principal_id or "").strip()
        operator_session = str(operator_session_id or "").strip()
        await self._require_issued_consent_authority(
            owner_principal_id=owner,
            operator_session_id=operator_session,
            authority_principal=authority_principal,
            required_grant=(
                AuthorityGrant.INGRESS
                if boundary == "capture"
                else AuthorityGrant.MODEL_INFERENCE
            ),
        )
        current = _utc(now or self._now())
        if ttl.total_seconds() <= 0 or ttl > RAW_RETENTION:
            raise AudioWorkerError("invalid_consent_ttl", "audio consent lifetime is outside the bound")
        granted_at = current
        expires_at = current + ttl
        reference = f"audio-consent:{boundary}:{uuid.uuid4().hex}"
        async with get_session() as db:
            row = AudioConsentGrant(
                reference=reference,
                owner_principal_id=owner,
                operator_session_id=operator_session,
                boundary=boundary,
                state=AudioConsentState.ACTIVE.value,
                granted_at=granted_at,
                expires_at=expires_at,
                updated_at=current,
            )
            db.add(row)
            await db.flush()
        return _build_server_owned_audio_consent(
            reference,
            AudioConsentState.ACTIVE,
            granted_at,
            expires_at,
        )

    async def revoke_consent_grant(
        self,
        reference: str,
        *,
        owner_principal_id: str,
        operator_session_id: str,
        now: datetime | None = None,
    ) -> bool:
        current = _utc(now or self._now())
        if not isinstance(reference, str) or not _CONSENT_REFERENCE_RE.fullmatch(reference):
            raise AudioWorkerError("audio_consent_reference_invalid", "audio consent reference is invalid")
        async with get_session() as db:
            result = await db.execute(
                select(AudioConsentGrant).where(AudioConsentGrant.reference == reference)
            )
            row = result.scalars().first()
            if row is None or row.owner_principal_id != owner_principal_id or row.operator_session_id != operator_session_id:
                raise AudioWorkerError("audio_consent_not_found", "audio consent grant is not available")
            if row.state != AudioConsentState.REVOKED.value:
                row.state = AudioConsentState.REVOKED.value
                row.revoked_at = current
                row.updated_at = current
                db.add(row)
                await db.flush()
            return True

    async def read_consent_grant(
        self,
        reference: str,
        *,
        owner_principal_id: str,
        operator_session_id: str,
        now: datetime | None = None,
    ) -> AudioConsent:
        """Read one server-owned grant and apply revocation/expiry now."""
        current = _utc(now or self._now())
        if not isinstance(reference, str) or not _CONSENT_REFERENCE_RE.fullmatch(reference):
            raise AudioWorkerError("audio_consent_reference_invalid", "audio consent reference is invalid")
        reference_match = _CONSENT_REFERENCE_RE.fullmatch(reference)
        assert reference_match is not None
        expected_boundary = reference_match.group(1)
        async with get_session() as db:
            result = await db.execute(
                select(AudioConsentGrant).where(AudioConsentGrant.reference == reference)
            )
            row = result.scalars().first()
            if (
                row is None
                or row.owner_principal_id != owner_principal_id
                or row.operator_session_id != operator_session_id
                or row.boundary != expected_boundary
            ):
                raise AudioWorkerError("audio_consent_untrusted", "audio consent is not a server-owned grant")
            if row.state == AudioConsentState.REVOKED.value or row.revoked_at is not None:
                raise AudioWorkerError(f"{row.boundary}_consent_revoked", "audio consent has been revoked")
            if row.state != AudioConsentState.ACTIVE.value:
                raise AudioWorkerError(f"{expected_boundary}_consent_untrusted", "audio consent grant state is invalid")
            expires_at = row.expires_at
            granted_at = row.granted_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if granted_at.tzinfo is None:
                granted_at = granted_at.replace(tzinfo=timezone.utc)
            expires_at = _utc(expires_at)
            granted_at = _utc(granted_at)
            if expires_at <= current:
                raise AudioWorkerError(f"{row.boundary}_consent_stale", "audio consent has expired")
            if granted_at > current:
                raise AudioWorkerError(f"{row.boundary}_consent_not_current", "audio consent is not current")
            return _build_server_owned_audio_consent(
                row.reference,
                AudioConsentState.ACTIVE,
                granted_at,
                expires_at,
            )

    async def _resolve_upload_consents(
        self,
        request: AudioUploadRequest,
        *,
        now: datetime,
    ) -> tuple[AudioConsent, AudioConsent | None]:
        capture_reference = getattr(request.capture_consent, "reference", None)
        if not isinstance(capture_reference, str) or not capture_reference:
            raise AudioWorkerError("capture_consent_missing", "capture consent is required")
        try:
            capture = await self.read_consent_grant(
                capture_reference,
                owner_principal_id=request.owner_principal_id,
                operator_session_id=request.operator_session_id or "",
                now=now,
            )
        except AudioWorkerError as exc:
            if exc.code == "audio_consent_untrusted":
                raise AudioWorkerError("capture_consent_untrusted", "capture consent is not server-owned") from exc
            if exc.code == "audio_consent_reference_invalid":
                raise AudioWorkerError("capture_consent_reference_invalid", "capture consent reference is invalid") from exc
            raise
        model = None
        model_reference = getattr(request.model_consent, "reference", None)
        if model_reference:
            try:
                model = await self.read_consent_grant(
                    model_reference,
                    owner_principal_id=request.owner_principal_id,
                    operator_session_id=request.operator_session_id or "",
                    now=now,
                )
            except AudioWorkerError as exc:
                if exc.code == "audio_consent_untrusted":
                    raise AudioWorkerError("cloud_upload_consent_untrusted", "model consent is not server-owned") from exc
                if exc.code == "audio_consent_reference_invalid":
                    raise AudioWorkerError("cloud_upload_consent_reference_invalid", "model consent reference is invalid") from exc
                raise
        return capture, model

    @staticmethod
    def _assert_operator_session(
        row: AudioIngressJob,
        owner_principal_id: str | None,
        operator_session_id: str | None,
    ) -> None:
        owner = str(owner_principal_id or "").strip()
        operator_session = str(operator_session_id or "").strip()
        if (
            not owner
            or not operator_session
            or row.owner_principal_id != owner
            or row.operator_session_id != operator_session
        ):
            raise AudioWorkerError(
                "audio_operator_session_mismatch",
                "audio job requires its owning operator principal and session",
            )

    @staticmethod
    def _principal_has_model_authority(principal: object, row: AudioIngressJob) -> bool:
        return AudioIngressWorker._principal_has_grant(
            principal,
            owner_principal_id=row.owner_principal_id,
            operator_session_id=str(row.operator_session_id or ""),
            required_grant=AuthorityGrant.MODEL_INFERENCE,
        )

    async def _recheck_model_authority(
        self,
        row: AudioIngressJob,
        *,
        authority_principal: object | None = None,
    ) -> bool:
        """Read current durable operator authority before model transport.

        ``authority_principal`` remains an API compatibility parameter for
        callers that carry request context, but it is deliberately ignored:
        request-time principal objects can be stale after revocation.
        """
        del authority_principal
        operator_session_id = str(row.operator_session_id or "").strip()
        if not operator_session_id:
            return False
        try:
            current = await authenticate_session(operator_session_id, touch=False)
        except AuthFailure:
            return False
        if current.session_id != operator_session_id:
            return False
        return self._principal_has_model_authority(current.principal, row)

    async def _expire_if_needed(self, request_id: str, *, now: datetime | None = None) -> AudioJobSnapshot:
        row = await self._job(request_id)
        if row is None:
            raise AudioWorkerError("audio_job_not_found", "audio job is not available")
        current = _utc(now or self._now())
        deadline = row.raw_audio_retention_deadline
        if isinstance(deadline, datetime):
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            deadline = _utc(deadline)
        expired = not isinstance(deadline, datetime) or deadline <= current
        if expired and row.status in {"queued", "processing", "transcript_ready", "confirming", "confirming_reserved"}:
            self._cleanup_job_paths(row.raw_path, row.normalized_path)
            self._drop_review_transcript(row.request_id)
            return await self._update_if_status(
                row.id,
                {"queued", "processing", "transcript_ready", "confirming", "confirming_reserved"},
                status="failed",
                error_code="raw_audio_retention_expired",
                raw_path=None,
                normalized_path=None,
                transcript=None,
                transcript_digest=None,
            )
        if row.status in {"confirmed", "failed", "blocked", "cancelled", "degraded"} and (row.raw_path or row.normalized_path):
            self._cleanup_job_paths(row.raw_path, row.normalized_path)
        if row.status in {"confirmed", "failed", "blocked", "cancelled", "degraded"}:
            self._drop_review_transcript(row.request_id)
        return self._snapshot(row)

    @staticmethod
    def _snapshot(row: AudioIngressJob, *, duplicate: bool = False) -> AudioJobSnapshot:
        return AudioJobSnapshot(
            id=row.id,
            request_id=row.request_id,
            request_digest=row.request_digest,
            owner_principal_id=row.owner_principal_id,
            operator_session_id=row.operator_session_id,
            session_id=row.session_id,
            message_id=row.message_id,
            attachment_id=row.attachment_id,
            requested_capability=row.requested_capability,
            status=row.status,
            audio_payload_digest=row.audio_payload_digest,
            audio_size_bytes=row.audio_size_bytes,
            duration_seconds=row.duration_seconds,
            decoded_duration_seconds=row.decoded_duration_seconds,
            normalized_wav_size_bytes=row.normalized_wav_size_bytes,
            media_type=row.media_type,
            container=row.container,
            codec=row.codec,
            sample_rate_hz=row.sample_rate_hz,
            channels=row.channels,
            capture_consent_reference=row.capture_consent_reference,
            model_consent_reference=row.model_consent_reference,
            raw_audio_retention_deadline=row.raw_audio_retention_deadline,
            # Raw/unconfirmed text is never exposed, including for legacy rows
            # created before the process-local transcript rule was tightened.
            transcript=None,
            transcript_digest=row.transcript_digest,
            confirmed_transcript_digest=row.confirmed_transcript_digest,
            result_digest=row.result_digest,
            error_code=row.error_code,
            admission_operation_id=row.admission_operation_id,
            provider_status=str(getattr(row, "provider_status", "unverified") or "unverified"),
            transport_status=str(getattr(row, "transport_status", "unknown") or "unknown"),
            duplicate=duplicate,
        )

    async def _update(self, job_id: str, **changes: object) -> AudioJobSnapshot:
        async with get_session() as db:
            row = await db.get(AudioIngressJob, job_id)
            if row is None:
                raise AudioWorkerError("audio_job_not_found", "audio job is not available")
            for key, value in changes.items():
                if hasattr(row, key):
                    setattr(row, key, value)
            row.updated_at = self._now()
            db.add(row)
            await db.flush()
            db.expunge(row)
            return self._snapshot(row)

    async def _update_if_status(
        self,
        job_id: str,
        expected_statuses: set[str] | frozenset[str],
        **changes: object,
    ) -> AudioJobSnapshot:
        """Apply a fenced state transition and return the durable winner.

        Cancellation and restart cleanup can race an in-flight intercepted
        transport.  Every terminal/process result uses this conditional write
        so a stale task can never resurrect a cancelled job.
        """
        async with get_session() as db:
            values = dict(changes)
            values["updated_at"] = self._now()
            await db.execute(
                update(AudioIngressJob)
                .where(
                    AudioIngressJob.id == job_id,
                    AudioIngressJob.status.in_(tuple(expected_statuses)),
                )
                .values(**values)
            )
            row = await db.get(AudioIngressJob, job_id)
            if row is None:
                raise AudioWorkerError("audio_job_not_found", "audio job is not available")
            db.expunge(row)
            return self._snapshot(row)

    async def _fence_confirmation(
        self,
        job_id: str,
        digest: str,
    ) -> tuple[AudioJobSnapshot, bool]:
        """Claim the confirmation fence and report whether this caller won."""
        async with get_session() as db:
            result = await db.execute(
                update(AudioIngressJob)
                .where(
                    AudioIngressJob.id == job_id,
                    AudioIngressJob.status == "transcript_ready",
                )
                .values(
                    status="confirming",
                    confirmed_transcript_digest=digest,
                    updated_at=self._now(),
                )
            )
            row = await db.get(AudioIngressJob, job_id)
            if row is None:
                raise AudioWorkerError("audio_job_not_found", "audio job is not available")
            db.expunge(row)
            return self._snapshot(row), result.rowcount == 1

    async def _insert_job(
        self,
        request: AudioUploadRequest,
        *,
        ingress: AudioIngressRequest,
        attachment_ref: dict[str, Any],
        raw_path: Path,
        normalized_path: Path,
        request_digest: str,
        retention_deadline: datetime,
        identity_material_digest: str,
    ) -> tuple[AudioIngressJob, bool]:
        async with get_session() as db:
            existing_result = await db.execute(
                select(AudioIngressJob).where(AudioIngressJob.request_id == ingress.request_id)
            )
            existing = existing_result.scalars().first()
            if existing is not None:
                if existing.request_digest != request_digest or existing.owner_principal_id != request.owner_principal_id:
                    raise AudioWorkerError("request_identity_conflict", "audio request identity is already bound")
                db.expunge(existing)
                return existing, True
            # Missing authority is always persisted as denied.  The
            # intercepted transport is a provider-free execution seam, not an
            # authority bypass.
            model_inference_granted = request.model_inference_granted is True
            row = AudioIngressJob(
                request_id=ingress.request_id,
                request_digest=request_digest,
                owner_principal_id=request.owner_principal_id,
                operator_session_id=request.operator_session_id,
                session_id=ingress.session_id,
                message_id=ingress.message_id,
                attachment_id=ingress.attachment_id,
                attachment_ref_json=_bounded_json(attachment_ref),
                status="queued",
                raw_path=str(raw_path),
                normalized_path=str(normalized_path),
                captured_at=_utc(ingress.captured_at),
                audio_payload_digest=str(attachment_ref.get("content_hash") or ingress.payload_digest),
                audio_size_bytes=ingress.audio_size_bytes,
                duration_seconds=ingress.duration_seconds,
                requested_capability=ingress.requested_capability,
                media_type=ingress.media_type,
                container=ingress.container,
                codec=ingress.codec,
                sample_rate_hz=ingress.sample_rate_hz,
                channels=ingress.channels,
                capture_consent_reference=attachment_ref.get("capture_consent_reference", ""),
                model_consent_reference=attachment_ref.get("model_consent_reference") or "",
                raw_audio_retention_deadline=retention_deadline,
                # The intercepted seam proves only local plumbing.  It does
                # not prove a live provider is reachable or verified.
                provider_status="unverified" if self.transport is not None else "unavailable",
                transport_status="intercepted" if self.transport is not None else "unavailable",
                metadata_json=_bounded_json({
                    "schema_version": AUDIO_WORKER_SCHEMA_VERSION,
                    "capture": "quarantined",
                    "transport": "intercepted_test_boundary_only",
                    "canonical_memory": "no_learning_until_confirmed",
                    "consent": {
                        "capture": _serialize_consent(request.capture_consent),
                        "model": _serialize_consent(request.model_consent),
                    },
                    "authority": {
                        "model_inference_granted": model_inference_granted is True,
                    },
                    "identity": {
                        "idempotency_key_digest": identity_material_digest,
                    },
                    "multimodal_voice_claim_boundary": guardian_safe_multimodal_voice_policy_payload()["claim_boundary"],
                }),
            )
            db.add(row)
            try:
                await db.flush()
            except IntegrityError:
                await db.rollback()
                retry = await db.execute(
                    select(AudioIngressJob).where(AudioIngressJob.request_id == ingress.request_id)
                )
                existing = retry.scalars().first()
                if (
                    existing is None
                    or existing.request_digest != request_digest
                    or existing.owner_principal_id != request.owner_principal_id
                ):
                    raise AudioWorkerError("request_identity_conflict", "audio request identity raced")
                db.expunge(existing)
                return existing, True
            db.expunge(row)
            return row, False

    async def submit(
        self,
        request: AudioUploadRequest,
        *,
        process: bool = True,
        authority_principal: object | None = None,
    ) -> AudioJobSnapshot:
        """Quarantine and admit one upload; repeated request IDs are idempotent."""
        now = self._now()
        if len(request.audio_bytes) > RAW_AUDIO_MAX_BYTES:
            raise AudioWorkerError("audio_size_exceeds_limit", "audio upload exceeds the bound")
        if _utc(request.captured_at) + RAW_RETENTION <= now:
            raise AudioWorkerError("raw_audio_retention_expired", "audio retention window has expired")
        # Validate the caller retry key before it can influence a path.  The
        # key remains an idempotency input; the durable request/message/
        # attachment IDs are derived below from the authenticated owner and
        # canonical session, matching the #750 web ingress contract.
        supplied_request_id = _validate_request_id(request.request_id)
        session = await session_manager.get_for_ingress(
            request.session_id,
            owner_principal_id=request.owner_principal_id,
        )
        canonical_session_id = str(session.id)
        request_material = supplied_request_id or uuid.uuid4().hex
        request_id = _derive_audio_identity(
            "request",
            owner=request.owner_principal_id,
            session_id=canonical_session_id,
            material=request_material,
        )
        # Match the #750 web-ingress message identity namespace: the caller's
        # opaque retry key is only material for a server-derived UUID5 scoped
        # to the authenticated principal and canonical session.
        message_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"seraph-chat:{request.owner_principal_id}:{canonical_session_id}:{request_material}",
        ).hex
        attachment_id = _derive_audio_identity(
            "attachment",
            owner=request.owner_principal_id,
            session_id=canonical_session_id,
            material=request_id,
        )
        supplied_message_id = _validate_supplied_identity(request.message_id, field="message_id")
        supplied_attachment_id = _validate_supplied_identity(request.attachment_id, field="attachment_id")
        if supplied_message_id is not None and supplied_message_id != message_id:
            raise AudioWorkerError(
                "canonical_message_identity_conflict",
                "message identity must be derived from the authenticated session",
            )
        if supplied_attachment_id is not None and supplied_attachment_id != attachment_id:
            raise AudioWorkerError(
                "canonical_attachment_identity_conflict",
                "attachment identity must be derived from the authenticated session",
            )
        work_dir: Path | None = None
        try:
            # Keep the caller's immutable consent evidence for request
            # identity only.  Authority and timestamps are resolved from the
            # durable grant below and never copied into the grant registry.
            submitted_capture_consent = request.capture_consent
            submitted_model_consent = request.model_consent
            capture_consent, model_consent = await self._resolve_upload_consents(request, now=now)
            request = replace(
                request,
                capture_consent=capture_consent,
                model_consent=model_consent,
            )
            # Never use a request or caller identity as a directory name.  A
            # fresh bounded worker-owned token also prevents retries from
            # observing or colliding with a previous quarantine directory.
            work_dir = self._work_dir(f"job-{uuid.uuid4().hex}")
            raw_path = work_dir / "quarantine.bin"
            normalized_path = work_dir / "normalized.wav"
            with raw_path.open("xb") as handle:
                os.chmod(raw_path, 0o600)
                handle.write(request.audio_bytes)
            payload_digest = hashlib.sha256(request.audio_bytes).hexdigest()
            source_format = _media_type_for_bytes(request.audio_bytes)
            if source_format is None:
                raise AudioWorkerError("audio_format_rejected", "audio container is not supported")
            if source_format[1] == "wav":
                decoded = await asyncio.to_thread(_read_wav_and_write_normalized, raw_path, normalized_path)
            else:
                decoded = await _decode_with_ffmpeg(raw_path, normalized_path, source_format)
            retention_deadline = _utc(request.captured_at) + RAW_RETENTION
            capture_ref = request.capture_consent.reference
            model_ref = request.model_consent.reference if request.model_consent is not None else None
            receipt = issue_attachment_quarantine_receipt(
                attachment_id=attachment_id,
                owner_principal_id=request.owner_principal_id,
                content_hash=payload_digest,
                media_type=decoded.media_type,
                size_bytes=len(request.audio_bytes),
                duration_seconds=decoded.source_duration_seconds,
                voice_note=True,
                purpose="push_to_talk_audio",
                session_id=canonical_session_id,
                message_id=message_id,
                codec=decoded.codec,
                capture_consent_reference=capture_ref,
                model_consent_reference=model_ref,
                raw_audio_retention_deadline=retention_deadline.isoformat(),
                issued_at=now,
                expires_at=now + timedelta(hours=1),
            )
            raw_attachment = {
                "attachment_id": attachment_id,
                "owner_principal_id": request.owner_principal_id,
                "content_hash": payload_digest,
                "media_type": decoded.media_type,
                "size_bytes": len(request.audio_bytes),
                "duration_seconds": decoded.source_duration_seconds,
                "voice_note": True,
                "purpose": "push_to_talk_audio",
                "session_id": canonical_session_id,
                "message_id": message_id,
                "codec": decoded.codec,
                "capture_consent_reference": capture_ref,
                "model_consent_reference": model_ref,
                "raw_audio_retention_deadline": retention_deadline.isoformat(),
                "quarantine_receipt": receipt,
            }
            attachment_ref = validate_attachment_refs(
                [raw_attachment], owner_principal_id=request.owner_principal_id
            )[0]
            ingress = AudioIngressRequest(
                session_id=canonical_session_id,
                message_id=message_id,
                attachment_id=attachment_id,
                request_id=request_id,
                captured_at=_utc(request.captured_at),
                audio_base64=base64.b64encode(request.audio_bytes).decode("ascii"),
                audio_size_bytes=len(request.audio_bytes),
                duration_seconds=decoded.source_duration_seconds,
                media_type=decoded.media_type,
                container=decoded.container,
                codec=decoded.codec,
                stream_count=1,
                normalized_wav_size_bytes=decoded.normalized_size_bytes,
                capture_consent=request.capture_consent,
                cloud_upload_consent=request.model_consent,
                raw_audio_retention_deadline=retention_deadline,
                requested_capability=request.requested_capability,
                identity_server_owned=True,
                sample_rate_hz=decoded.source_sample_rate_hz,
                channels=decoded.source_channels,
            )
            request_digest = _bound_request_digest(
                request,
                ingress,
                capture_consent=submitted_capture_consent,
                model_consent=submitted_model_consent,
            )
            row, duplicate = await self._insert_job(
                request,
                ingress=ingress,
                attachment_ref=attachment_ref,
                raw_path=raw_path,
                normalized_path=normalized_path,
                request_digest=request_digest,
                retention_deadline=retention_deadline,
                identity_material_digest=_digest_text(request_material),
            )
            if duplicate:
                self._unlink_tree(work_dir)
                existing_snapshot = self._snapshot(row, duplicate=True)
                # A retry of an admitted but unfinished job must still pass
                # through the durable consent/authority checks.  Terminal and
                # transcript-ready jobs remain idempotent snapshots.
                if process and existing_snapshot.status in {"queued", "processing"}:
                    return replace(
                        await self.process(
                            request_id,
                            owner_principal_id=request.owner_principal_id,
                            operator_session_id=request.operator_session_id,
                            authority_principal=authority_principal,
                        ),
                        duplicate=True,
                    )
                return existing_snapshot
            # The quarantined source is no longer needed once conversion has
            # succeeded.  Keep only normalized bytes until confirmation/expiry.
            self._unlink_tree(raw_path)
            await self._update(
                row.id,
                raw_path=None,
                decoded_duration_seconds=decoded.normalized_duration_seconds,
                normalized_wav_size_bytes=decoded.normalized_size_bytes,
            )
            if process:
                return await self.process(
                    request_id,
                    owner_principal_id=request.owner_principal_id,
                    operator_session_id=request.operator_session_id,
                    authority_principal=authority_principal,
                )
            return await self._snapshot_by_request(request_id)
        except Exception as exc:
            if work_dir is not None:
                self._unlink_tree(work_dir)
            if isinstance(exc, AudioWorkerError):
                raise
            if isinstance(exc, ConversationIdentityError):
                raise AudioWorkerError(exc.code, "audio attachment proof was rejected") from exc
            raise AudioWorkerError("audio_ingress_failed", "audio upload could not be admitted") from exc

    async def _snapshot_by_request(self, request_id: str, *, duplicate: bool = False) -> AudioJobSnapshot:
        _validate_request_id(request_id)
        snapshot = await self._expire_if_needed(request_id)
        return replace(snapshot, duplicate=duplicate)

    async def process(
        self,
        request_id: str,
        *,
        owner_principal_id: str | None = None,
        operator_session_id: str | None = None,
        authority_principal: object | None = None,
    ) -> AudioJobSnapshot:
        """Run one process task and register it for cancellation fencing."""
        request_id = _validate_request_id(request_id) or ""
        current_snapshot = await self._expire_if_needed(request_id)
        row_for_identity = await self._job(request_id)
        if row_for_identity is None:
            raise AudioWorkerError("audio_job_not_found", "audio job is not available")
        self._assert_operator_session(row_for_identity, owner_principal_id, operator_session_id)
        current_task = asyncio.current_task()
        wait_for: asyncio.Task[AudioJobSnapshot] | None = None
        async with self._task_lock:
            existing = self._tasks.get(request_id)
            if existing is not None and existing is not current_task and not existing.done():
                wait_for = existing
            elif current_task is not None:
                self._tasks[request_id] = current_task  # type: ignore[assignment]
        if wait_for is not None:
            return await asyncio.shield(wait_for)
        try:
            return await self._process_impl(
                request_id,
                owner_principal_id=owner_principal_id,
                operator_session_id=operator_session_id,
                authority_principal=authority_principal,
            )
        finally:
            if current_task is not None:
                async with self._task_lock:
                    if self._tasks.get(request_id) is current_task:
                        self._tasks.pop(request_id, None)

    async def _process_impl(
        self,
        request_id: str,
        *,
        owner_principal_id: str | None = None,
        operator_session_id: str | None = None,
        authority_principal: object | None = None,
    ) -> AudioJobSnapshot:
        """Decode/admit/parse a queued job, using only an intercepted transport."""
        row = await self._job(request_id)
        if row is None:
            raise AudioWorkerError("audio_job_not_found", "audio job is not available")
        self._assert_operator_session(row, owner_principal_id, operator_session_id)
        if row.status in {"confirmed", "failed", "blocked", "cancelled", "degraded"}:
            return self._snapshot(row)
        if row.status == "transcript_ready":
            return self._snapshot(row)
        try:
            if row.status == "queued":
                transitioned = await self._update_if_status(row.id, {"queued"}, status="processing")
                if transitioned.status != "processing":
                    return transitioned
                refreshed = await self._job(request_id)
                if refreshed is None:
                    raise AudioWorkerError("audio_job_not_found", "audio job is not available")
                row = refreshed
            if row.status != "processing":
                return self._snapshot(row)
            try:
                persisted_metadata = json.loads(row.metadata_json or "{}")
            except (TypeError, json.JSONDecodeError):
                persisted_metadata = {}
            consent_metadata = (
                persisted_metadata.get("consent")
                if isinstance(persisted_metadata, dict)
                else None
            )
            authority_metadata = (
                persisted_metadata.get("authority")
                if isinstance(persisted_metadata, dict)
                else None
            )
            persisted_capture = _deserialize_consent(
                consent_metadata.get("capture") if isinstance(consent_metadata, dict) else None
            )
            persisted_model = _deserialize_consent(
                consent_metadata.get("model") if isinstance(consent_metadata, dict) else None
            )
            if isinstance(persisted_capture, AudioConsent) and persisted_capture.state is AudioConsentState.REVOKED:
                self._cleanup_job_paths(row.raw_path, row.normalized_path)
                return await self._update_if_status(
                    row.id,
                    {"processing"},
                    status="blocked",
                    error_code="capture_consent_revoked",
                    raw_path=None,
                    normalized_path=None,
                    transcript=None,
                    transcript_digest=None,
                )
            if isinstance(persisted_model, AudioConsent) and persisted_model.state is AudioConsentState.REVOKED:
                self._cleanup_job_paths(row.raw_path, row.normalized_path)
                return await self._update_if_status(
                    row.id,
                    {"processing"},
                    status="blocked",
                    error_code="cloud_upload_consent_revoked",
                    raw_path=None,
                    normalized_path=None,
                    transcript=None,
                    transcript_digest=None,
                )
            if not isinstance(authority_metadata, dict) or authority_metadata.get("model_inference_granted") is not True:
                self._cleanup_job_paths(row.raw_path, row.normalized_path)
                return await self._update_if_status(
                    row.id,
                    {"processing"},
                    status="blocked",
                    error_code="model_inference_grant_missing",
                    raw_path=None,
                    normalized_path=None,
                    transcript=None,
                    transcript_digest=None,
                )
            try:
                capture, model = await self._resolve_upload_consents(
                    AudioUploadRequest(
                        session_id=row.session_id,
                        owner_principal_id=row.owner_principal_id,
                        operator_session_id=row.operator_session_id,
                        audio_bytes=b"placeholder",
                        captured_at=row.captured_at,
                        capture_consent=persisted_capture,
                        model_consent=persisted_model,
                        requested_capability=row.requested_capability,
                        model_inference_granted=True,
                    ),
                    now=self._now(),
                )
            except AudioWorkerError as exc:
                self._cleanup_job_paths(row.raw_path, row.normalized_path)
                return await self._update_if_status(
                    row.id,
                    {"processing"},
                    status="blocked",
                    error_code=exc.code,
                    raw_path=None,
                    normalized_path=None,
                    transcript=None,
                    transcript_digest=None,
                )
            request = AudioIngressRequest(
                session_id=row.session_id,
                message_id=row.message_id,
                attachment_id=row.attachment_id,
                request_id=row.request_id,
                captured_at=row.captured_at,
                audio_base64="YQ==",
                audio_size_bytes=1,
                duration_seconds=row.duration_seconds,
                media_type=row.media_type,
                container=row.container,
                codec=row.codec,
                stream_count=1,
                normalized_wav_size_bytes=row.normalized_wav_size_bytes,
                capture_consent=capture,
                cloud_upload_consent=model,
                raw_audio_retention_deadline=row.raw_audio_retention_deadline,
                requested_capability=row.requested_capability,
                sample_rate_hz=row.sample_rate_hz,
                channels=row.channels,
            )
            policy = AudioIngressPolicy(
                provider_status=AudioProviderStatus.READY,
                trusted_adapter_id=self._trusted_adapter_id,
                provider_proof_reference=self._provider_proof_reference,
                consent_proof_reference=self._consent_proof_reference,
            )
            # Validate policy/consent and preserve the contract receipt, while
            # keeping this path provider-free and denying local fallback.
            result = validate_audio_ingress(request, policy=policy, now=self._now())
            if result.status is AudioIngressStatus.DEGRADED:
                self._cleanup_job_paths(row.raw_path, row.normalized_path)
                unavailable = result.reason_code == "openrouter_route_unavailable"
                return await self._update_if_status(
                    row.id,
                    {"processing"},
                    status="degraded",
                    error_code=result.reason_code,
                    provider_status="unavailable" if unavailable else "unverified",
                    transport_status="unavailable" if unavailable else "unknown",
                    raw_path=None,
                    normalized_path=None,
                    transcript=None,
                    transcript_digest=None,
                )
            if result.status is not AudioIngressStatus.ACCEPTED:
                self._cleanup_job_paths(row.raw_path, row.normalized_path)
                return await self._update_if_status(
                    row.id,
                    {"processing"},
                    status="blocked",
                    error_code=result.reason_code,
                    raw_path=None,
                    normalized_path=None,
                    transcript=None,
                    transcript_digest=None,
                )
            if self.transport is None:
                self._cleanup_job_paths(row.raw_path, row.normalized_path)
                return await self._update_if_status(
                    row.id,
                    {"processing"},
                    status="degraded",
                    error_code="audio_transport_unavailable",
                    provider_status="unavailable",
                    transport_status="unavailable",
                    raw_path=None,
                    normalized_path=None,
                    transcript=None,
                    transcript_digest=None,
                )
            try:
                normalized_path = self._safe_quarantine_path(row.normalized_path)
            except AudioWorkerError:
                self._cleanup_job_paths(row.raw_path, row.normalized_path)
                return await self._update_if_status(
                    row.id,
                    {"processing"},
                    status="blocked",
                    error_code="quarantine_path_invalid",
                    raw_path=None,
                    normalized_path=None,
                    transcript=None,
                    transcript_digest=None,
                )
            if normalized_path is None or not normalized_path.is_file() or normalized_path.is_symlink():
                return await self._update_if_status(
                    row.id,
                    {"processing"},
                    status="failed",
                    error_code="normalized_audio_missing",
                    raw_path=None,
                    normalized_path=None,
                    transcript=None,
                    transcript_digest=None,
                )
            normalized_bytes = await asyncio.to_thread(self._read_quarantine_bytes, normalized_path)
            if len(normalized_bytes) > NORMALIZED_WAV_MAX_BYTES:
                raise AudioWorkerError("normalized_wav_size_exceeds_limit", "normalized WAV exceeds the bound")
            # Consent and model authority are re-read at the effect boundary;
            # enqueue-time metadata is only an admission record and cannot
            # authorize a later transport after revocation.
            try:
                capture, model = await self._resolve_upload_consents(
                    AudioUploadRequest(
                        session_id=row.session_id,
                        owner_principal_id=row.owner_principal_id,
                        operator_session_id=row.operator_session_id,
                        audio_bytes=b"placeholder",
                        captured_at=row.captured_at,
                        capture_consent=persisted_capture,
                        model_consent=persisted_model,
                        requested_capability=row.requested_capability,
                        model_inference_granted=True,
                    ),
                    now=self._now(),
                )
            except AudioWorkerError as exc:
                self._cleanup_job_paths(row.raw_path, row.normalized_path)
                return await self._update_if_status(
                    row.id,
                    {"processing"},
                    status="blocked",
                    error_code=exc.code,
                    raw_path=None,
                    normalized_path=None,
                    transcript=None,
                    transcript_digest=None,
                )
            if not await self._recheck_model_authority(
                row,
                authority_principal=authority_principal,
            ):
                self._cleanup_job_paths(row.raw_path, row.normalized_path)
                return await self._update_if_status(
                    row.id,
                    {"processing"},
                    status="blocked",
                    error_code="model_inference_grant_revoked",
                    raw_path=None,
                    normalized_path=None,
                    transcript=None,
                    transcript_digest=None,
                )
            operation_id = f"audio:{row.request_id}"
            admission_request = GpuAdmissionRequest(
                operation_id=operation_id,
                job_id=row.id,
                owner_id=row.owner_principal_id,
                priority=GpuPriority.INTERACTIVE_CHAT,
                deadline_at=self._now().timestamp() + DECODER_TIMEOUT_SECONDS,
                runtime_path="audio_transcription",
                session_id=row.session_id,
                capability_version=AUDIO_CAPABILITY_VERSION,
                estimated_cost_microusd=0,
                owner_budget_microusd=0,
                uncertain_on_error=False,
            )

            transitioned = await self._update_if_status(
                row.id,
                {"processing"},
                admission_operation_id=operation_id,
            )
            if transitioned.status != "processing":
                return transitioned

            if not await self._recheck_model_authority(
                row,
                authority_principal=authority_principal,
            ):
                self._cleanup_job_paths(row.raw_path, row.normalized_path)
                return await self._update_if_status(
                    row.id,
                    {"processing"},
                    status="blocked",
                    error_code="model_inference_grant_revoked",
                    raw_path=None,
                    normalized_path=None,
                    transcript=None,
                    transcript_digest=None,
                )
            try:
                capture, model = await self._resolve_upload_consents(
                    AudioUploadRequest(
                        session_id=row.session_id,
                        owner_principal_id=row.owner_principal_id,
                        operator_session_id=row.operator_session_id,
                        audio_bytes=b"placeholder",
                        captured_at=row.captured_at,
                        capture_consent=persisted_capture,
                        model_consent=persisted_model,
                        requested_capability=row.requested_capability,
                        model_inference_granted=True,
                    ),
                    now=self._now(),
                )
            except AudioWorkerError as exc:
                self._cleanup_job_paths(row.raw_path, row.normalized_path)
                return await self._update_if_status(
                    row.id,
                    {"processing"},
                    status="blocked",
                    error_code=exc.code,
                    raw_path=None,
                    normalized_path=None,
                    transcript=None,
                    transcript_digest=None,
                )

            async def invoke() -> object:
                # ``execute`` may wait behind another remote operation.  This
                # callback runs only after the broker grants the lease, so the
                # durable job, operator session, current model grant, and both
                # consent rows are checked again at the actual transport edge.
                latest = await self._job(row.request_id)
                if latest is None:
                    raise _AudioTransportBoundaryBlocked(
                        "audio_job_not_found",
                        "audio job is no longer available",
                    )
                if latest.status != "processing":
                    raise _AudioTransportBoundaryBlocked(
                        "audio_job_cancelled" if latest.status == "cancelled" else "audio_job_not_ready",
                        "audio job is no longer admitted for transport",
                    )
                try:
                    self._assert_operator_session(
                        latest,
                        latest.owner_principal_id,
                        latest.operator_session_id,
                    )
                except AudioWorkerError as exc:
                    raise _AudioTransportBoundaryBlocked(exc.code, str(exc)) from exc
                if not await self._recheck_model_authority(latest):
                    raise _AudioTransportBoundaryBlocked(
                        "model_inference_grant_revoked",
                        "model inference authority is no longer current",
                    )
                try:
                    await self._resolve_upload_consents(
                        AudioUploadRequest(
                            session_id=latest.session_id,
                            owner_principal_id=latest.owner_principal_id,
                            operator_session_id=latest.operator_session_id,
                            audio_bytes=b"placeholder",
                            captured_at=latest.captured_at,
                            capture_consent=persisted_capture,
                            model_consent=persisted_model,
                            requested_capability=latest.requested_capability,
                            model_inference_granted=True,
                        ),
                        now=self._now(),
                    )
                except AudioWorkerError as exc:
                    raise _AudioTransportBoundaryBlocked(exc.code, str(exc)) from exc
                return await self.transport.transcribe(
                    normalized_bytes,
                    request_id=latest.request_id,
                    session_id=latest.session_id,
                    audio_digest=latest.audio_payload_digest,
                )

            try:
                response = await self.admission_broker.execute(
                    admission_request,
                    invoke,
                    uncertain_on_error=False,
                )
            except GpuAdmissionError as exc:
                raise AudioWorkerError(f"admission_{getattr(exc, 'code', 'rejected')}", "audio admission rejected") from exc
            transcript = parse_intercepted_transcript(response)
            transcript_digest = _digest_text(transcript)
            result_digest = _digest_text(_bounded_json({"request": row.request_digest, "transcript": transcript_digest}))
            # Keep the bounded text only in this worker process for the owning
            # operator's review.  It is never written to the durable job row or
            # canonical memory before confirmation.
            updated = await self._update_if_status(
                row.id,
                {"processing"},
                status="transcript_ready",
                transcript=None,
                transcript_digest=transcript_digest,
                result_digest=result_digest,
                error_code=None,
                raw_path=None,
            )
            if updated.status == "transcript_ready":
                self._set_review_transcript(row, transcript)
            else:
                self._drop_review_transcript(row.request_id)
            del transcript
            return updated
        except asyncio.CancelledError:
            self._cleanup_job_paths(row.raw_path, row.normalized_path)
            await self._update_if_status(
                row.id,
                {"processing", "queued"},
                status="cancelled",
                error_code="caller_cancelled",
                raw_path=None,
                normalized_path=None,
                transcript=None,
                transcript_digest=None,
            )
            raise
        except _AudioTransportBoundaryBlocked as exc:
            self._cleanup_job_paths(row.raw_path, row.normalized_path)
            return await self._update_if_status(
                row.id,
                {"processing"},
                status="blocked",
                error_code=exc.code,
                raw_path=None,
                normalized_path=None,
                transcript=None,
                transcript_digest=None,
            )
        except AudioTransportUnavailable as exc:
            self._cleanup_job_paths(row.raw_path, row.normalized_path)
            return await self._update_if_status(
                row.id,
                {"processing"},
                status="degraded",
                error_code=exc.code,
                provider_status="unavailable",
                transport_status="unavailable",
                raw_path=None,
                normalized_path=None,
                transcript=None,
                transcript_digest=None,
            )
        except AudioTransportMalformed as exc:
            self._cleanup_job_paths(row.raw_path, row.normalized_path)
            return await self._update_if_status(
                row.id,
                {"processing"},
                status="degraded",
                error_code=exc.code,
                provider_status="unverified",
                transport_status="unknown",
                raw_path=None,
                normalized_path=None,
                transcript=None,
                transcript_digest=None,
            )
        except AudioWorkerError as exc:
            self._cleanup_job_paths(row.raw_path, row.normalized_path)
            return await self._update_if_status(
                row.id,
                {"processing"},
                status="failed",
                error_code=exc.code,
                raw_path=None,
                normalized_path=None,
                transcript=None,
                transcript_digest=None,
            )
        except Exception:
            self._cleanup_job_paths(row.raw_path, row.normalized_path)
            return await self._update_if_status(
                row.id,
                {"processing"},
                status="failed",
                error_code="audio_processing_failed",
                raw_path=None,
                normalized_path=None,
                transcript=None,
                transcript_digest=None,
            )

    async def confirm_transcript(
        self,
        request_id: str,
        transcript: str,
        *,
        expected_transcript_digest: str | None = None,
        transcript_digest: str | None = None,
        owner_principal_id: str | None = None,
        operator_session_id: str | None = None,
    ) -> AudioJobSnapshot:
        """Persist only the operator-confirmed transcript through SessionManager."""
        request_id = _validate_request_id(request_id) or ""
        row = await self._job(request_id)
        if row is None:
            raise AudioConfirmationConflict("audio_job_not_found")
        try:
            self._assert_operator_session(row, owner_principal_id, operator_session_id)
        except AudioWorkerError as exc:
            raise AudioConfirmationConflict(exc.code) from exc
        if not isinstance(transcript, str):
            raise AudioConfirmationConflict("transcript_invalid")
        transcript = transcript.strip()
        if not transcript or len(transcript) > TRANSCRIPT_MAX_CHARS or "\x00" in transcript:
            raise AudioConfirmationConflict("transcript_invalid")
        digest = _digest_text(transcript)
        if transcript_digest and transcript_digest != digest:
            raise AudioConfirmationConflict("transcript_digest_mismatch")
        if not isinstance(expected_transcript_digest, str) or not expected_transcript_digest:
            raise AudioConfirmationConflict("transcript_confirmation_digest_required")
        if row.status == "confirmed":
            if row.confirmed_transcript_digest != digest:
                raise AudioConfirmationConflict("transcript_confirmation_stale")
            if expected_transcript_digest not in {
                row.transcript_digest,
                row.confirmed_transcript_digest,
            }:
                raise AudioConfirmationConflict("transcript_confirmation_stale")
            return self._snapshot(row)
        if row.status in {"confirming", "confirming_reserved"}:
            if row.confirmed_transcript_digest != digest:
                raise AudioConfirmationConflict("transcript_confirmation_stale")
        elif row.status != "transcript_ready" or row.transcript_digest is None:
            raise AudioConfirmationConflict("transcript_confirmation_unavailable")
        if expected_transcript_digest != (
            row.confirmed_transcript_digest
            if row.status in {"confirming", "confirming_reserved"}
            else row.transcript_digest
        ):
            raise AudioConfirmationConflict()
        fenced_here = False
        if row.status == "transcript_ready":
            deadline = row.raw_audio_retention_deadline
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            if _utc(deadline) <= self._now():
                await self._update_if_status(
                    row.id,
                    {"transcript_ready"},
                    status="failed",
                    error_code="raw_audio_retention_expired",
                    raw_path=None,
                    normalized_path=None,
                    transcript=None,
                    transcript_digest=None,
                )
                raise AudioConfirmationConflict("raw_audio_retention_expired")
            fenced, claimed_fence = await self._fence_confirmation(row.id, digest)
            if fenced.status != "confirming":
                if fenced.status == "confirmed" and fenced.confirmed_transcript_digest == digest:
                    return fenced
                raise AudioConfirmationConflict("transcript_confirmation_unavailable")
            if fenced.confirmed_transcript_digest != digest:
                raise AudioConfirmationConflict("transcript_confirmation_stale")
            fenced_here = claimed_fence
            refreshed = await self._job(request_id)
            if refreshed is None:
                raise AudioConfirmationConflict("audio_job_not_found")
            row = refreshed
        if row.status == "cancelled":
            raise AudioConfirmationConflict("confirmation_cancelled")
        attachment_ref = json.loads(row.attachment_ref_json or "{}")
        try:
            identity = build_conversation_identity(
                conversation_id=row.session_id,
                thread_id=row.session_id,
                owner_principal_id=row.owner_principal_id,
                operator_session_id=row.operator_session_id,
                device_id=f"web-operator-session:{row.operator_session_id or 'ambient'}",
                channel="web",
                transport="rest",
                correlation_id=f"chat:{row.message_id}",
                causation_id=row.message_id,
            )
            lineage = build_lineage(
                identity,
                attachment_refs=[attachment_ref],
                message_id=row.message_id,
            )
        except ConversationIdentityError as exc:
            raise AudioConfirmationConflict(exc.code) from exc
        metadata = {
            "audio": {
                "schema_version": AUDIO_WORKER_SCHEMA_VERSION,
                "request_id": row.request_id,
                "request_digest": row.request_digest,
                "audio_payload_digest": row.audio_payload_digest,
                "decoded_duration_seconds": row.decoded_duration_seconds,
                "normalized_wav_size_bytes": row.normalized_wav_size_bytes,
                "transcript_digest": digest,
                "confirmed_transcript_digest": digest,
                "capture_consent_reference": row.capture_consent_reference,
                "model_consent_reference": row.model_consent_reference,
                "confirmed": True,
                "provider": "intercepted_test_boundary_only",
                "provider_status": row.provider_status,
                "transport_status": row.transport_status,
                "live_provider_call_claimed": False,
                "canonical_memory": "confirmed_transcript_persisted",
            },
            "lineage": lineage,
            "ingress": {
                "schema_version": "seraph.conversation.v1",
                "message_id": row.message_id,
                "idempotency_key_digest": row.request_digest,
                "content_digest": digest,
                "principal_id": row.owner_principal_id,
                "device_id": identity.device_id,
                "session_id": row.session_id,
                "conversation_id": row.session_id,
                "thread_id": row.session_id,
                "channel": "web",
                "transport": "rest",
                "correlation_id": f"chat:{row.message_id}",
                "causation_id": row.message_id,
                "attachment_refs": [attachment_ref],
            },
        }
        try:
            persisted_metadata = json.loads(row.metadata_json or "{}")
        except (TypeError, json.JSONDecodeError):
            persisted_metadata = {}
        identity_metadata = (
            persisted_metadata.get("identity")
            if isinstance(persisted_metadata, dict)
            else None
        )
        idempotency_key_digest = (
            identity_metadata.get("idempotency_key_digest")
            if isinstance(identity_metadata, dict)
            else None
        )
        if not isinstance(idempotency_key_digest, str) or len(idempotency_key_digest) != 64:
            idempotency_key_digest = row.request_digest
        metadata["ingress"]["idempotency_key_digest"] = idempotency_key_digest
        metadata_json = _bounded_json(metadata)
        try:
            if row.status == "confirming_reserved":
                existing = await session_manager.get_message(row.message_id)
                duplicate = existing is not None
                if existing is None:
                    raise MessageIngressConflictError(row.message_id)
            else:
                existing, duplicate = await session_manager.reserve_ingress_message(
                    row.session_id,
                    transcript,
                    message_id=row.message_id,
                    metadata_json=metadata_json,
                    attachment_refs=[attachment_ref],
                    confirmation_job_id=row.id,
                    confirmation_digest=digest,
                    confirmation_operator_session_id=row.operator_session_id or "",
                )
            if duplicate and (
                existing.session_id != row.session_id
                or existing.role != "user"
                or existing.content != transcript
                or existing.metadata_json != metadata_json
                or _digest_text(existing.content) != digest
            ):
                if fenced_here or row.status == "confirming_reserved":
                    await self._update_if_status(
                        row.id,
                        {"confirming", "confirming_reserved"},
                        status="transcript_ready",
                        confirmed_transcript_digest=None,
                    )
                raise AudioConfirmationConflict("canonical_message_identity_conflict")
        except MessageIngressConflictError as exc:
            # A crash can leave the canonical message committed before the job
            # status update.  Re-read and accept only the exact immutable
            # confirmation; any other content remains a conflict.
            current_row = await self._job(request_id)
            if current_row is not None and current_row.status == "cancelled":
                self._drop_review_transcript(request_id)
                raise AudioConfirmationConflict("confirmation_cancelled") from exc
            existing = await session_manager.get_message(row.message_id)
            if (
                existing is None
                or existing.session_id != row.session_id
                or existing.role != "user"
                or existing.content != transcript
                or existing.metadata_json != metadata_json
                or _digest_text(existing.content) != digest
            ):
                if fenced_here or row.status == "confirming_reserved":
                    await self._update_if_status(
                        row.id,
                        {"confirming", "confirming_reserved"},
                        status="transcript_ready",
                        confirmed_transcript_digest=None,
                    )
                raise AudioConfirmationConflict("canonical_message_identity_conflict") from exc
        except ConversationIdentityError as exc:
            if fenced_here or row.status == "confirming_reserved":
                await self._update_if_status(
                    row.id,
                    {"confirming", "confirming_reserved"},
                    status="transcript_ready",
                    confirmed_transcript_digest=None,
                )
            raise AudioConfirmationConflict(exc.code) from exc
        self._cleanup_job_paths(row.raw_path, row.normalized_path)
        self._drop_review_transcript(row.request_id)
        confirmed = await self._update_if_status(
            row.id,
            {"confirming", "confirming_reserved"},
            status="confirmed",
            confirmed_transcript_digest=digest,
            normalized_path=None,
            raw_path=None,
            transcript=None,
        )
        if confirmed.status != "confirmed":
            raise AudioConfirmationConflict("confirmation_cancelled")
        return confirmed

    async def cancel(
        self,
        request_id: str,
        *,
        owner_principal_id: str | None = None,
        operator_session_id: str | None = None,
    ) -> AudioJobSnapshot:
        request_id = _validate_request_id(request_id) or ""
        row = await self._job(request_id)
        if row is None:
            raise AudioWorkerError("audio_job_not_found", "audio job is not available")
        self._assert_operator_session(row, owner_principal_id, operator_session_id)
        if row.status == "confirming_reserved":
            raise AudioWorkerError("confirmation_in_progress", "transcript confirmation has already fenced cancellation")
        if row.status in {"confirmed", "failed", "blocked", "cancelled", "degraded"}:
            self._cleanup_job_paths(row.raw_path, row.normalized_path)
            self._drop_review_transcript(row.request_id)
            return self._snapshot(row)
        # Fence the durable status before asking an in-flight task or broker to
        # stop.  A transport that returns after cancellation can then only see
        # the terminal cancelled row and cannot write transcript_ready.
        cancelled = await self._update_if_status(
            row.id,
            {"queued", "processing", "transcript_ready", "confirming"},
            status="cancelled",
            error_code="cancelled",
            raw_path=None,
            normalized_path=None,
            transcript=None,
            transcript_digest=None,
        )
        if cancelled.status in {"confirming", "confirming_reserved"}:
            raise AudioWorkerError("confirmation_in_progress", "transcript confirmation has already fenced cancellation")
        if cancelled.admission_operation_id:
            try:
                await self.admission_broker.cancel(
                    cancelled.admission_operation_id,
                    owner_id=cancelled.owner_principal_id,
                )
            except (KeyError, GpuAdmissionError):
                pass
        self._cleanup_job_paths(row.raw_path, row.normalized_path)
        self._drop_review_transcript(row.request_id)
        async with self._task_lock:
            task = self._tasks.get(request_id)
        current_task = asyncio.current_task()
        if task is not None and task is not current_task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(task)
        return cancelled

    async def cleanup_after_restart(
        self,
        *,
        now: datetime | None = None,
        recover_process_local: bool = True,
    ) -> int:
        """Expire/revoke quarantine state without ever re-uploading it.

        Startup recovery treats in-flight and unconfirmed process-local work as
        unrecoverable.  Runtime cleanup only expires rows whose durable
        retention deadline has passed, allowing active jobs to continue.
        """
        current = _utc(now or self._now())
        removed = 0
        async with get_session() as db:
            result = await db.execute(select(AudioIngressJob))
            rows = result.scalars().all()
            for row in rows:
                deadline_value = row.raw_audio_retention_deadline
                malformed_deadline = not isinstance(deadline_value, datetime)
                if malformed_deadline:
                    deadline = current
                else:
                    if deadline_value.tzinfo is None:
                        # SQLite may materialize legacy DATETIME values without
                        # their original offset; persisted Seraph timestamps are
                        # UTC by contract.
                        deadline_value = deadline_value.replace(tzinfo=timezone.utc)
                    deadline = _utc(deadline_value)
                if malformed_deadline:
                    # Missing retention metadata is treated as expired so a
                    # corrupt row cannot keep a quarantine path alive.
                    should_expire = True
                else:
                    should_expire = deadline <= current
                changed = False
                # Clear any raw text left by a legacy or interrupted writer,
                # regardless of its stale status.  Confirmed rows retain only
                # their immutable digest; no transcript text is recoverable.
                if row.transcript is not None:
                    row.transcript = None
                    changed = True
                    if row.status != "confirmed":
                        row.transcript_digest = None
                if recover_process_local and row.status in {
                    "queued",
                    "processing",
                    "confirming",
                    "confirming_reserved",
                }:
                    should_expire = True
                # An unconfirmed transcript is process-local by contract.  It
                # is never resumed after restart; clear its digest and text.
                if recover_process_local and row.status == "transcript_ready":
                    should_expire = True
                if should_expire:
                    self._cleanup_job_paths(row.raw_path, row.normalized_path)
                    row.status = "failed"
                    row.error_code = "restart_cleanup_expired" if deadline <= current else "restart_recovery_required"
                    row.raw_path = None
                    row.normalized_path = None
                    row.transcript = None
                    row.transcript_digest = None
                    row.confirmed_transcript_digest = None
                    self._drop_review_transcript(row.request_id)
                    row.updated_at = current
                    changed = True
                if changed:
                    row.updated_at = current
                    db.add(row)
                    removed += 1
            await db.flush()
        try:
            for child in self.quarantine_root.iterdir():
                if child.is_dir() and child.stat().st_mtime < (current - RAW_RETENTION).timestamp():
                    self._unlink_tree(child)
        except OSError:
            pass
        return removed


def parse_intercepted_transcript(response: object) -> str:
    """Parse a bounded test transport response without trusting provider text."""
    candidate: object = None
    if isinstance(response, str):
        candidate = response
    elif isinstance(response, dict):
        if isinstance(response.get("transcript"), str):
            candidate = response["transcript"]
        else:
            choices = response.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str):
                        candidate = content
                    elif isinstance(content, list):
                        chunks = [
                            item.get("text")
                            for item in content
                            if isinstance(item, dict) and isinstance(item.get("text"), str)
                        ]
                        candidate = "".join(chunks)
    if not isinstance(candidate, str):
        raise AudioTransportMalformed()
    candidate = candidate.strip()
    if not candidate or len(candidate) > TRANSCRIPT_MAX_CHARS or "\x00" in candidate:
        raise AudioTransportMalformed()
    return candidate


async def cleanup_audio_ingress_jobs(*, now: datetime | None = None) -> int:
    """Startup hook using the default worker; no provider or model is touched."""
    return await default_audio_worker.cleanup_after_restart(now=now)


async def cleanup_expired_audio_ingress_jobs(*, now: datetime | None = None) -> int:
    """Periodic runtime cleanup for deadline-expired audio jobs."""
    return await default_audio_worker.cleanup_after_restart(
        now=now,
        recover_process_local=False,
    )


def cleanup_audio_job_paths(raw_path: str | Path | None, normalized_path: str | Path | None) -> bool:
    """Clean one job's paths through a live worker-owned quarantine root.

    Session deletion can run outside the worker instance that admitted a job.
    Use the registered live workers so a custom test/runtime quarantine root is
    handled without making session deletion trust arbitrary filesystem paths.
    """
    cleaned = False
    for worker in tuple(_AUDIO_WORKERS):
        cleaned = worker._cleanup_job_paths(raw_path, normalized_path) or cleaned
    return cleaned


def forget_audio_job_review(request_id: str) -> None:
    """Drop process-local transcript text when a canonical session is deleted."""
    for worker in tuple(_AUDIO_WORKERS):
        worker._drop_review_transcript(request_id)


default_audio_worker = AudioIngressWorker()


__all__ = [
    "AUDIO_CAPABILITY_VERSION",
    "AUDIO_WORKER_SCHEMA_VERSION",
    "AudioConfirmationConflict",
    "AudioIngressWorker",
    "AudioJobSnapshot",
    "AudioTransportMalformed",
    "AudioTransportUnavailable",
    "AudioUploadRequest",
    "InterceptedAudioTransport",
    "MAX_AUDIO_SECONDS",
    "NORMALIZED_WAV_MAX_BYTES",
    "RAW_AUDIO_MAX_BYTES",
    "RAW_RETENTION",
    "cleanup_audio_ingress_jobs",
    "cleanup_expired_audio_ingress_jobs",
    "cleanup_audio_job_paths",
    "forget_audio_job_review",
    "default_audio_worker",
    "parse_intercepted_transcript",
]
