"""Provider-free receipts for the #751 local audio vertical slice."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
from unittest.mock import patch
import wave

import pytest

from config.settings import settings
from src.agent.session import session_manager
from src.guardian.audio_ingress import AudioConsent, AudioConsentState
from src.guardian.audio_worker import (
    AudioIngressWorker,
    AudioUploadRequest,
    InterceptedAudioTransport,
)
from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker


def _wav(*, rate: int = 8_000, seconds: float = 0.25, channels: int = 2) -> bytes:
    frames = int(rate * seconds)
    payload = b"\x00\x00" * frames * channels
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(payload)
    return output.getvalue()


def _request(session_id: str, *, request_id: str = "audio-test-1", model: bool = True) -> AudioUploadRequest:
    now = datetime.now(timezone.utc)
    return AudioUploadRequest(
        session_id=session_id,
        owner_principal_id="operator:test",
        operator_session_id="operator-session-test",
        audio_bytes=_wav(),
        captured_at=now,
        capture_consent=AudioConsent(
            reference="capture-test",
            state=AudioConsentState.ACTIVE,
            granted_at=now - timedelta(seconds=1),
            expires_at=now + timedelta(minutes=15),
        ),
        model_consent=(
            AudioConsent(
                reference="model-test",
                state=AudioConsentState.ACTIVE,
                granted_at=now - timedelta(seconds=1),
                expires_at=now + timedelta(minutes=15),
            )
            if model
            else None
        ),
        request_id=request_id,
    )


@pytest.mark.asyncio
async def test_audio_worker_normalizes_admits_confirms_and_is_idempotent(async_db, tmp_path: Path):
    session = await session_manager.get_or_create("audio-session-1", owner_principal_id="operator:test")
    seen: list[bytes] = []

    def intercepted(**kwargs):
        normalized = kwargs["normalized_wav"]
        seen.append(normalized)
        with wave.open(io.BytesIO(normalized), "rb") as stream:
            assert (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()) == (1, 2, 16_000)
            assert 0 < stream.getnframes() <= 60 * 16_000
        return {"transcript": "local intercepted transcript"}

    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport(intercepted),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        snapshot = await worker.submit(_request(session.id), process=True)
        assert snapshot.status == "transcript_ready"
        assert snapshot.decoded_duration_seconds is not None
        assert snapshot.normalized_wav_size_bytes is not None
        assert snapshot.normalized_wav_size_bytes <= 2 * 1024 * 1024
        assert len(seen) == 1
        confirmed = await worker.confirm_transcript(
            snapshot.request_id,
            "operator edited transcript",
            expected_transcript_digest=snapshot.transcript_digest,
        )
        assert confirmed.status == "confirmed"
        duplicate = await worker.submit(_request(session.id), process=True)
        assert duplicate.duplicate is True
        assert duplicate.status == "confirmed"
        assert len(seen) == 1

    messages = await session_manager.get_messages(session.id)
    assert len(messages) == 1
    assert messages[0]["content"] == "operator edited transcript"
    assert messages[0]["metadata"]
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_audio_worker_separate_model_consent_and_cancel_cleanup(async_db, tmp_path: Path):
    session = await session_manager.get_or_create("audio-session-2", owner_principal_id="operator:test")
    calls = 0

    def intercepted(**_kwargs):
        nonlocal calls
        calls += 1
        return "should not run"

    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport(intercepted),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        snapshot = await worker.submit(_request(session.id, request_id="audio-test-2", model=False), process=True)
        assert snapshot.status == "blocked"
        assert snapshot.error_code == "cloud_upload_consent_missing"
        assert calls == 0
        assert not list(tmp_path.iterdir())

        queued = await worker.submit(_request(session.id, request_id="audio-test-3"), process=False)
        assert queued.status == "queued"
        cancelled = await worker.cancel(queued.request_id)
        assert cancelled.status == "cancelled"
        assert not list(tmp_path.iterdir())
