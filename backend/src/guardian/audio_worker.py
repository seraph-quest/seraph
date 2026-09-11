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
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import resource
import shutil
import subprocess
import tempfile
from typing import Any, Awaitable, Callable, Protocol
import uuid
import wave

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.agent.session import MessageIngressConflictError, session_manager
from src.conversation.identity import (
    ConversationIdentityError,
    issue_attachment_quarantine_receipt,
    validate_attachment_refs,
)
from src.db.engine import get_session
from src.db.models import AudioIngressJob
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
_AUDIO_STATUS = frozenset(
    {"queued", "processing", "transcript_ready", "confirmed", "failed", "blocked", "cancelled"}
)


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

    def __post_init__(self) -> None:
        for field_name in ("session_id", "owner_principal_id"):
            value = str(getattr(self, field_name) or "").strip()
            if not value or len(value) > _AUDIO_ID_MAX:
                raise ValueError(f"{field_name} is required and bounded")
            object.__setattr__(self, field_name, value)
        if self.operator_session_id is not None:
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
        if self.channel != "web" or self.transport not in {"rest", "websocket"}:
            raise ValueError("audio upload must use the web ingress transport")


@dataclass(frozen=True, slots=True)
class AudioJobSnapshot:
    id: str
    request_id: str
    request_digest: str
    owner_principal_id: str
    session_id: str
    message_id: str
    attachment_id: str
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
    duplicate: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": AUDIO_WORKER_SCHEMA_VERSION,
            "id": self.id,
            "request_id": self.request_id,
            "request_digest": self.request_digest,
            "owner_principal_id": self.owner_principal_id,
            "session_id": self.session_id,
            "message_id": self.message_id,
            "attachment_id": self.attachment_id,
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
                "available": self.transcript is not None,
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
                "transport": "intercepted_test_boundary",
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


def _bounded_id(value: str | None, prefix: str) -> str:
    normalized = str(value or "").strip()
    if normalized:
        return normalized
    return f"{prefix}-{uuid.uuid4().hex}"


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
        self.quarantine_root = Path(quarantine_root) if quarantine_root else Path(tempfile.gettempdir()) / "seraph-audio"
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.quarantine_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._tasks: dict[str, asyncio.Task[AudioJobSnapshot]] = {}
        self._task_lock = asyncio.Lock()

    def _now(self) -> datetime:
        return _utc(self.clock())

    def _work_dir(self, request_id: str) -> Path:
        path = self.quarantine_root / request_id
        path.mkdir(mode=0o700, parents=False, exist_ok=False)
        return path

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

    @classmethod
    def _cleanup_job_paths(cls, raw_path: str | Path | None, normalized_path: str | Path | None) -> None:
        paths = [Path(path) for path in (raw_path, normalized_path) if path]
        for path in paths:
            cls._unlink_tree(path)
        parents = {path.parent for path in paths}
        for parent in parents:
            cls._unlink_tree(parent)

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
    def _snapshot(row: AudioIngressJob, *, duplicate: bool = False) -> AudioJobSnapshot:
        return AudioJobSnapshot(
            id=row.id,
            request_id=row.request_id,
            request_digest=row.request_digest,
            owner_principal_id=row.owner_principal_id,
            session_id=row.session_id,
            message_id=row.message_id,
            attachment_id=row.attachment_id,
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
            transcript=row.transcript,
            transcript_digest=row.transcript_digest,
            confirmed_transcript_digest=row.confirmed_transcript_digest,
            result_digest=row.result_digest,
            error_code=row.error_code,
            admission_operation_id=row.admission_operation_id,
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
                media_type=ingress.media_type,
                container=ingress.container,
                codec=ingress.codec,
                sample_rate_hz=ingress.sample_rate_hz,
                channels=ingress.channels,
                capture_consent_reference=attachment_ref.get("capture_consent_reference", ""),
                model_consent_reference=attachment_ref.get("model_consent_reference") or "",
                raw_audio_retention_deadline=retention_deadline,
                metadata_json=_bounded_json({
                    "schema_version": AUDIO_WORKER_SCHEMA_VERSION,
                    "capture": "quarantined",
                    "transport": "intercepted_test_boundary_only",
                    "canonical_memory": "no_learning_until_confirmed",
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
                if existing is None or existing.request_digest != request_digest:
                    raise AudioWorkerError("request_identity_conflict", "audio request identity raced")
                db.expunge(existing)
                return existing, True
            db.expunge(row)
            return row, False

    async def submit(self, request: AudioUploadRequest, *, process: bool = True) -> AudioJobSnapshot:
        """Quarantine and admit one upload; repeated request IDs are idempotent."""
        now = self._now()
        if len(request.audio_bytes) > RAW_AUDIO_MAX_BYTES:
            raise AudioWorkerError("audio_size_exceeds_limit", "audio upload exceeds the bound")
        if request.request_id:
            existing = await self._job(request.request_id)
            if existing is not None:
                payload_digest = hashlib.sha256(request.audio_bytes).hexdigest()
                if (
                    existing.owner_principal_id != request.owner_principal_id
                    or existing.audio_payload_digest != payload_digest
                ):
                    raise AudioWorkerError("request_identity_conflict", "audio request identity is already bound")
                return replace(self._snapshot(existing), duplicate=True)
        await session_manager.get_for_ingress(
            request.session_id,
            owner_principal_id=request.owner_principal_id,
        )
        request_id = _bounded_id(request.request_id, "audio-request")
        message_id = _bounded_id(request.message_id, "audio-message")
        attachment_id = _bounded_id(request.attachment_id, "audio-attachment")
        work_dir: Path | None = None
        try:
            if request.capture_consent is None:
                raise AudioWorkerError("capture_consent_missing", "capture consent is required")
            work_dir = self._work_dir(request_id)
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
                session_id=request.session_id,
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
                "session_id": request.session_id,
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
                session_id=request.session_id,
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
            request_digest = canonical_audio_request_digest(ingress)
            row, duplicate = await self._insert_job(
                request,
                ingress=ingress,
                attachment_ref=attachment_ref,
                raw_path=raw_path,
                normalized_path=normalized_path,
                request_digest=request_digest,
                retention_deadline=retention_deadline,
            )
            if duplicate:
                self._unlink_tree(work_dir)
                return replace(self._snapshot(row), duplicate=True)
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
                return await self.process(request_id)
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
        row = await self._job(request_id)
        if row is None:
            raise AudioWorkerError("audio_job_not_found", "audio job is not available")
        return self._snapshot(row, duplicate=duplicate)

    async def process(self, request_id: str) -> AudioJobSnapshot:
        """Decode/admit/parse a queued job, using only an intercepted transport."""
        row = await self._job(request_id)
        if row is None:
            raise AudioWorkerError("audio_job_not_found", "audio job is not available")
        if row.status in {"confirmed", "failed", "blocked", "cancelled"}:
            return self._snapshot(row)
        if row.transcript is not None and row.status == "transcript_ready":
            return self._snapshot(row)
        try:
            capture = AudioConsent(
                reference=row.capture_consent_reference,
                state=AudioConsentState.ACTIVE,
                granted_at=row.captured_at - timedelta(seconds=1),
                expires_at=row.raw_audio_retention_deadline,
            )
            model = (
                AudioConsent(
                    reference=row.model_consent_reference,
                    state=AudioConsentState.ACTIVE,
                    granted_at=row.captured_at - timedelta(seconds=1),
                    expires_at=row.raw_audio_retention_deadline,
                )
                if row.model_consent_reference
                else None
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
                requested_capability="chat",
                sample_rate_hz=row.sample_rate_hz,
                channels=row.channels,
            )
            policy = AudioIngressPolicy(
                provider_status=AudioProviderStatus.READY,
                trusted_adapter_id="intercepted-audio-transport",
                provider_proof_reference="intercepted:transport",
                consent_proof_reference="intercepted:consent",
            )
            # Validate policy/consent and preserve the contract receipt, while
            # keeping this path provider-free and denying local fallback.
            result = validate_audio_ingress(request, policy=policy, now=self._now())
            if result.status is not AudioIngressStatus.ACCEPTED:
                self._cleanup_job_paths(row.raw_path, row.normalized_path)
                return await self._update(row.id, status="blocked", error_code=result.reason_code, raw_path=None, normalized_path=None)
            if self.transport is None:
                self._cleanup_job_paths(row.raw_path, row.normalized_path)
                return await self._update(row.id, status="blocked", error_code="audio_transport_unavailable", raw_path=None, normalized_path=None)
            normalized_path = Path(row.normalized_path or "")
            if not normalized_path.is_file():
                return await self._update(row.id, status="failed", error_code="normalized_audio_missing", raw_path=None, normalized_path=None)
            normalized_bytes = await asyncio.to_thread(normalized_path.read_bytes)
            if len(normalized_bytes) > NORMALIZED_WAV_MAX_BYTES:
                raise AudioWorkerError("normalized_wav_size_exceeds_limit", "normalized WAV exceeds the bound")
            operation_id = f"audio:{row.request_id}"
            await self._update(row.id, status="processing", admission_operation_id=operation_id)
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

            async def invoke() -> object:
                return await self.transport.transcribe(
                    normalized_bytes,
                    request_id=row.request_id,
                    session_id=row.session_id,
                    audio_digest=row.audio_payload_digest,
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
            return await self._update(
                row.id,
                status="transcript_ready",
                transcript=transcript,
                transcript_digest=transcript_digest,
                result_digest=result_digest,
                error_code=None,
                raw_path=None,
            )
        except asyncio.CancelledError:
            await self._update(row.id, status="cancelled", error_code="caller_cancelled", raw_path=None, normalized_path=None)
            raise
        except AudioWorkerError as exc:
            self._cleanup_job_paths(row.raw_path, row.normalized_path)
            return await self._update(row.id, status="failed", error_code=exc.code, raw_path=None, normalized_path=None)
        except Exception:
            self._cleanup_job_paths(row.raw_path, row.normalized_path)
            return await self._update(row.id, status="failed", error_code="audio_processing_failed", raw_path=None, normalized_path=None)

    async def confirm_transcript(
        self,
        request_id: str,
        transcript: str,
        *,
        expected_transcript_digest: str | None = None,
        transcript_digest: str | None = None,
    ) -> AudioJobSnapshot:
        """Persist only the operator-confirmed transcript through SessionManager."""
        row = await self._job(request_id)
        if row is None:
            raise AudioConfirmationConflict("audio_job_not_found")
        if row.status != "transcript_ready" or row.transcript_digest is None:
            raise AudioConfirmationConflict("transcript_confirmation_unavailable")
        if expected_transcript_digest and expected_transcript_digest != row.transcript_digest:
            raise AudioConfirmationConflict()
        if not isinstance(transcript, str):
            raise AudioConfirmationConflict("transcript_invalid")
        transcript = transcript.strip()
        if not transcript or len(transcript) > TRANSCRIPT_MAX_CHARS or "\x00" in transcript:
            raise AudioConfirmationConflict("transcript_invalid")
        digest = _digest_text(transcript)
        if transcript_digest and transcript_digest != digest:
            raise AudioConfirmationConflict("transcript_digest_mismatch")
        attachment_ref = json.loads(row.attachment_ref_json or "{}")
        metadata = {
            "audio": {
                "schema_version": AUDIO_WORKER_SCHEMA_VERSION,
                "request_id": row.request_id,
                "request_digest": row.request_digest,
                "audio_payload_digest": row.audio_payload_digest,
                "decoded_duration_seconds": row.decoded_duration_seconds,
                "normalized_wav_size_bytes": row.normalized_wav_size_bytes,
                "transcript_digest": digest,
                "capture_consent_reference": row.capture_consent_reference,
                "model_consent_reference": row.model_consent_reference,
                "confirmed": True,
                "provider": "intercepted_test_boundary_only",
                "live_provider_call_claimed": False,
                "canonical_memory": "confirmed_transcript_persisted",
            },
            "lineage": {
                "conversation_id": row.session_id,
                "thread_id": row.session_id,
                "owner_principal_id": row.owner_principal_id,
                "operator_session_id": row.operator_session_id,
                "device_id": "web-operator-session:" + str(row.operator_session_id or "unknown"),
                "channel": "web",
                "transport": "rest",
                "correlation_id": "audio:" + row.request_id,
                "causation_id": row.request_id,
                "attachment_refs": [attachment_ref],
            },
        }
        metadata_json = _bounded_json(metadata)
        try:
            await session_manager.add_message(
                row.session_id,
                "user",
                transcript,
                metadata_json=metadata_json,
                message_id=row.message_id,
                attachment_refs=[attachment_ref],
            )
        except MessageIngressConflictError as exc:
            raise AudioConfirmationConflict("canonical_message_identity_conflict") from exc
        except ConversationIdentityError as exc:
            raise AudioConfirmationConflict(exc.code) from exc
        self._cleanup_job_paths(row.raw_path, row.normalized_path)
        return await self._update(
            row.id,
            status="confirmed",
            confirmed_transcript_digest=digest,
            normalized_path=None,
            raw_path=None,
        )

    async def cancel(self, request_id: str) -> AudioJobSnapshot:
        row = await self._job(request_id)
        if row is None:
            raise AudioWorkerError("audio_job_not_found", "audio job is not available")
        if row.admission_operation_id:
            try:
                await self.admission_broker.cancel(row.admission_operation_id, owner_id=row.owner_principal_id)
            except (KeyError, GpuAdmissionError):
                pass
        self._cleanup_job_paths(row.raw_path, row.normalized_path)
        return await self._update(row.id, status="cancelled", error_code="cancelled", raw_path=None, normalized_path=None)

    async def cleanup_after_restart(self, *, now: datetime | None = None) -> int:
        """Expire/revoke orphaned files and never re-upload them automatically."""
        current = _utc(now or self._now())
        removed = 0
        async with get_session() as db:
            result = await db.execute(
                select(AudioIngressJob).where(AudioIngressJob.status.in_(("queued", "processing", "transcript_ready")))
            )
            rows = result.scalars().all()
            for row in rows:
                deadline = _utc(row.raw_audio_retention_deadline)
                should_expire = deadline <= current
                if row.status in {"queued", "processing"}:
                    should_expire = True
                if should_expire:
                    self._cleanup_job_paths(row.raw_path, row.normalized_path)
                    row.status = "failed"
                    row.error_code = "restart_cleanup_expired" if deadline <= current else "restart_recovery_required"
                    row.raw_path = None
                    row.normalized_path = None
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
    "default_audio_worker",
    "parse_intercepted_transcript",
]
