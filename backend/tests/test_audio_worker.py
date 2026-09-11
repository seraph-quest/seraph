"""Provider-free receipts for the #751 local audio vertical slice."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch
import wave

import pytest
from dataclasses import replace
from sqlmodel import select

from config.settings import settings
from src.auth.service import revoke_session
from src.agent.session import MessageIngressConflictError, session_manager
from src.guardian.audio_ingress import AudioConsent, AudioConsentState, _build_server_owned_audio_consent
from src.guardian.audio_worker import (
    AudioConfirmationConflict,
    AudioIngressWorker,
    AudioUploadRequest,
    AudioWorkerError,
    InterceptedAudioTransport,
)
from src.db.models import AudioConsentGrant, AudioIngressJob, OperatorSession
from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal


OPERATOR_OWNER = "operator:single"
OPERATOR_SESSION = "operator-session-test"
CAPTURE_REF = "audio-consent:capture:11111111111111111111111111111111"
MODEL_REF = "audio-consent:cloud_upload:22222222222222222222222222222222"


@pytest.fixture(autouse=True)
async def audio_worker_authority(async_db):
    """Seed only the durable operator/session and consent evidence used by tests."""
    now = datetime.now(timezone.utc)
    async with async_db() as db:
        db.add(
            OperatorSession(
                id=OPERATOR_SESSION,
                token_hash="audio-worker-test-token-hash",
                idle_expires_at=now + timedelta(hours=1),
                absolute_expires_at=now + timedelta(hours=1),
            )
        )
        db.add_all(
            [
                AudioConsentGrant(
                    reference=CAPTURE_REF,
                    owner_principal_id=OPERATOR_OWNER,
                    operator_session_id=OPERATOR_SESSION,
                    boundary="capture",
                    granted_at=now - timedelta(seconds=1),
                    expires_at=now + timedelta(minutes=15),
                    updated_at=now,
                ),
                AudioConsentGrant(
                    reference=MODEL_REF,
                    owner_principal_id=OPERATOR_OWNER,
                    operator_session_id=OPERATOR_SESSION,
                    boundary="cloud_upload",
                    granted_at=now - timedelta(seconds=1),
                    expires_at=now + timedelta(minutes=15),
                    updated_at=now,
                ),
            ]
        )


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
        owner_principal_id=OPERATOR_OWNER,
        operator_session_id=OPERATOR_SESSION,
        audio_bytes=_wav(),
        captured_at=now,
        capture_consent=_build_server_owned_audio_consent(
            CAPTURE_REF,
            AudioConsentState.ACTIVE,
            now - timedelta(seconds=1),
            now + timedelta(minutes=15),
        ),
        model_consent=(
            _build_server_owned_audio_consent(
                MODEL_REF,
                AudioConsentState.ACTIVE,
                now - timedelta(seconds=1),
                now + timedelta(minutes=15),
            )
            if model
            else None
        ),
        request_id=request_id,
        model_inference_granted=True,
    )


@pytest.mark.asyncio
async def test_audio_worker_normalizes_admits_confirms_and_is_idempotent(async_db, tmp_path: Path):
    session = await session_manager.get_or_create("audio-session-1", owner_principal_id=OPERATOR_OWNER)
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
        first_request = _request(session.id)
        snapshot = await worker.submit(first_request, process=True)
        assert snapshot.status == "transcript_ready"
        assert snapshot.decoded_duration_seconds is not None
        assert snapshot.normalized_wav_size_bytes is not None
        assert snapshot.normalized_wav_size_bytes <= 2 * 1024 * 1024
        assert len(seen) == 1
        confirmed = await worker.confirm_transcript(
            snapshot.request_id,
            "operator edited transcript",
            expected_transcript_digest=snapshot.transcript_digest,
            owner_principal_id=OPERATOR_OWNER,
            operator_session_id=OPERATOR_SESSION,
        )
        assert confirmed.status == "confirmed"
        duplicate = await worker.submit(first_request, process=True)
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
    session = await session_manager.get_or_create("audio-session-2", owner_principal_id=OPERATOR_OWNER)
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
        cancelled = await worker.cancel(
            queued.request_id,
            owner_principal_id=OPERATOR_OWNER,
            operator_session_id=OPERATOR_SESSION,
        )
        assert cancelled.status == "cancelled"
        assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_audio_worker_rejects_path_retry_keys_and_redacts_legacy_transcript(async_db, tmp_path: Path):
    session = await session_manager.get_or_create("audio-session-boundary", owner_principal_id=OPERATOR_OWNER)
    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport({"transcript": "never persist this"}),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        for request_id in ("../escape", "/tmp/escape", r"C:\\escape"):
            with pytest.raises(AudioWorkerError) as exc_info:
                await worker.submit(_request(session.id, request_id=request_id), process=False)
            assert exc_info.value.code == "invalid_request_id"
        assert not list(tmp_path.iterdir())

        queued = await worker.submit(_request(session.id, request_id="audio-legacy-row"), process=False)
        async with async_db() as db:
            row = await db.get(AudioIngressJob, queued.id)
            assert row is not None
            row.status = "transcript_ready"
            row.transcript = "legacy unconfirmed text"
            row.transcript_digest = "a" * 64
            db.add(row)

        snapshot = await worker._snapshot_by_request(queued.request_id)
        assert snapshot.transcript is None
        assert "text" not in snapshot.as_dict()["transcript"]
        assert await worker.cleanup_after_restart() == 1
        async with async_db() as db:
            row = await db.get(AudioIngressJob, queued.id)
            assert row is not None
            assert row.transcript is None
            assert row.transcript_digest is None


@pytest.mark.asyncio
async def test_audio_worker_rechecks_persisted_authority_and_consent(async_db, tmp_path: Path):
    session = await session_manager.get_or_create("audio-session-consent", owner_principal_id=OPERATOR_OWNER)
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
        missing = await worker.submit(
            replace(_request(session.id, request_id="audio-missing-grant"), model_inference_granted=None),
            process=False,
        )
        blocked = await worker.process(
            missing.request_id,
            owner_principal_id=OPERATOR_OWNER,
            operator_session_id=OPERATOR_SESSION,
        )
        assert blocked.status == "blocked"
        assert blocked.error_code == "model_inference_grant_missing"

        no_grant = await worker.submit(_request(session.id, request_id="audio-no-grant"), process=False)
        async with async_db() as db:
            row = await db.get(AudioIngressJob, no_grant.id)
            assert row is not None
            metadata = json.loads(row.metadata_json)
            metadata["authority"]["model_inference_granted"] = False
            row.metadata_json = json.dumps(metadata, sort_keys=True)
            db.add(row)
        blocked = await worker.process(
            no_grant.request_id,
            owner_principal_id=OPERATOR_OWNER,
            operator_session_id=OPERATOR_SESSION,
        )
        assert blocked.status == "blocked"
        assert blocked.error_code == "model_inference_grant_missing"

        revoked = await worker.submit(_request(session.id, request_id="audio-revoked"), process=False)
        async with async_db() as db:
            row = await db.get(AudioIngressJob, revoked.id)
            assert row is not None
            metadata = json.loads(row.metadata_json)
            metadata["consent"]["model"]["state"] = "revoked"
            row.metadata_json = json.dumps(metadata, sort_keys=True)
            db.add(row)
        blocked = await worker.process(
            revoked.request_id,
            owner_principal_id=OPERATOR_OWNER,
            operator_session_id=OPERATOR_SESSION,
        )
        assert blocked.status == "blocked"
        assert blocked.error_code == "cloud_upload_consent_revoked"
        assert calls == 0


@pytest.mark.asyncio
async def test_audio_worker_full_digest_and_server_identity_conflicts(async_db, tmp_path: Path):
    session = await session_manager.get_or_create("audio-session-identity", owner_principal_id=OPERATOR_OWNER)
    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport({"transcript": "ok"}),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        first = _request(session.id, request_id="audio-immutable")
        accepted = await worker.submit(first, process=False)
        changed_consent = replace(
            first,
            model_consent=replace(
                first.model_consent,
                expires_at=first.model_consent.expires_at + timedelta(minutes=1),
            ),
        )
        with pytest.raises(AudioWorkerError) as exc_info:
            await worker.submit(changed_consent, process=False)
        assert exc_info.value.code == "request_identity_conflict"

        caller_asserted_id = replace(_request(session.id, request_id="audio-asserted"), message_id="caller-message")
        with pytest.raises(AudioWorkerError) as exc_info:
            await worker.submit(caller_asserted_id, process=False)
        assert exc_info.value.code == "canonical_message_identity_conflict"
        assert accepted.message_id != "caller-message"


@pytest.mark.asyncio
async def test_audio_worker_cannot_mint_consent_from_identity_strings(async_db, tmp_path: Path):
    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport({"transcript": "unused"}),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    async with async_db() as db:
        before = await db.execute(
            select(AudioConsentGrant).where(
                AudioConsentGrant.owner_principal_id == OPERATOR_OWNER,
                AudioConsentGrant.operator_session_id == OPERATOR_SESSION,
            )
        )
        before_count = len(before.scalars().all())

    with pytest.raises(AudioWorkerError) as exc_info:
        await worker.issue_consent_grant(
            owner_principal_id=OPERATOR_OWNER,
            operator_session_id=OPERATOR_SESSION,
            boundary="capture",
        )

    assert exc_info.value.code == "server_owned_identity_required"
    async with async_db() as db:
        after = await db.execute(
            select(AudioConsentGrant).where(
                AudioConsentGrant.owner_principal_id == OPERATOR_OWNER,
                AudioConsentGrant.operator_session_id == OPERATOR_SESSION,
            )
        )
        assert len(after.scalars().all()) == before_count


@pytest.mark.asyncio
async def test_audio_worker_mutations_require_owner_and_operator_session(async_db, tmp_path: Path):
    session = await session_manager.get_or_create("audio-session-mutation-identity", owner_principal_id=OPERATOR_OWNER)
    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport({"transcript": "unused"}),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        queued = await worker.submit(_request(session.id, request_id="audio-mutation-identity"), process=False)

        with pytest.raises(AudioWorkerError) as process_error:
            await worker.process(queued.request_id, operator_session_id=OPERATOR_SESSION)
        assert process_error.value.code == "audio_operator_session_mismatch"

        with pytest.raises(AudioConfirmationConflict) as confirm_error:
            await worker.confirm_transcript(
                queued.request_id,
                "should fail before processing",
                expected_transcript_digest="a" * 64,
                operator_session_id=OPERATOR_SESSION,
            )
        assert confirm_error.value.code == "audio_operator_session_mismatch"

        with pytest.raises(AudioWorkerError) as cancel_error:
            await worker.cancel(queued.request_id, operator_session_id=OPERATOR_SESSION)
        assert cancel_error.value.code == "audio_operator_session_mismatch"

        assert (
            await worker.cancel(
                queued.request_id,
                owner_principal_id=OPERATOR_OWNER,
                operator_session_id=OPERATOR_SESSION,
            )
        ).status == "cancelled"


@pytest.mark.asyncio
async def test_audio_worker_submit_rejects_revoked_operator_session(async_db, tmp_path: Path):
    session = await session_manager.get_or_create("audio-session-submit-revoked", owner_principal_id=OPERATOR_OWNER)
    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport({"transcript": "must not run"}),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    await revoke_session(OPERATOR_SESSION)

    with pytest.raises(AudioWorkerError) as exc_info:
        await worker.submit(_request(session.id, request_id="audio-submit-revoked"), process=False)

    assert exc_info.value.code == "audio_operator_session_invalid"
    async with async_db() as db:
        result = await db.execute(select(AudioIngressJob).where(AudioIngressJob.request_id == "audio-submit-revoked"))
        assert result.scalars().first() is None
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_audio_worker_cancel_rejects_revoked_operator_session(async_db, tmp_path: Path):
    session = await session_manager.get_or_create("audio-session-cancel-revoked", owner_principal_id=OPERATOR_OWNER)
    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport({"transcript": "unused"}),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        queued = await worker.submit(_request(session.id, request_id="audio-cancel-revoked"), process=False)
    await revoke_session(OPERATOR_SESSION)

    with pytest.raises(AudioWorkerError) as exc_info:
        await worker.cancel(
            queued.request_id,
            owner_principal_id=OPERATOR_OWNER,
            operator_session_id=OPERATOR_SESSION,
        )

    assert exc_info.value.code == "audio_operator_session_invalid"
    current = await worker._snapshot_by_request(queued.request_id)
    assert current.status == "queued"
    assert list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_audio_worker_revoke_consent_rejects_revoked_operator_session(async_db, tmp_path: Path):
    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport({"transcript": "unused"}),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    await revoke_session(OPERATOR_SESSION)

    with pytest.raises(AudioWorkerError) as exc_info:
        await worker.revoke_consent_grant(
            CAPTURE_REF,
            owner_principal_id=OPERATOR_OWNER,
            operator_session_id=OPERATOR_SESSION,
        )

    assert exc_info.value.code == "audio_operator_session_invalid"
    async with async_db() as db:
        result = await db.execute(select(AudioConsentGrant).where(AudioConsentGrant.reference == CAPTURE_REF))
        row = result.scalars().first()
        assert row is not None
        assert row.state == AudioConsentState.ACTIVE.value


@pytest.mark.asyncio
async def test_audio_worker_revoke_consent_fences_queued_job(async_db, tmp_path: Path):
    session = await session_manager.get_or_create(
        "audio-session-queued-revocation",
        owner_principal_id=OPERATOR_OWNER,
    )
    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport({"transcript": "unused"}),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        queued = await worker.submit(
            _request(session.id, request_id="audio-queued-revocation"),
            process=False,
        )
        assert await worker.revoke_consent_grant(
            MODEL_REF,
            owner_principal_id=OPERATOR_OWNER,
            operator_session_id=OPERATOR_SESSION,
        ) is True

    blocked = await worker._snapshot_by_request(
        queued.request_id,
        owner_principal_id=OPERATOR_OWNER,
        operator_session_id=OPERATOR_SESSION,
    )
    assert blocked.status == "blocked"
    assert blocked.error_code == "cloud_upload_consent_revoked"
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_audio_worker_confirm_rejects_revoked_operator_session(async_db, tmp_path: Path):
    session = await session_manager.get_or_create("audio-session-confirm-revoked", owner_principal_id=OPERATOR_OWNER)
    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport({"transcript": "generated"}),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        ready = await worker.submit(_request(session.id, request_id="audio-confirm-revoked"), process=True)
    await revoke_session(OPERATOR_SESSION)

    with pytest.raises(AudioConfirmationConflict) as exc_info:
        await worker.confirm_transcript(
            ready.request_id,
            "operator confirmed",
            expected_transcript_digest=ready.transcript_digest,
            owner_principal_id=OPERATOR_OWNER,
            operator_session_id=OPERATOR_SESSION,
        )

    assert exc_info.value.code == "audio_operator_session_invalid"
    current = await worker._snapshot_by_request(ready.request_id)
    assert current.status == "transcript_ready"
    assert await session_manager.get_message(ready.message_id) is None


@pytest.mark.asyncio
async def test_audio_worker_direct_process_rejects_wrong_owner_before_expiry_cleanup(async_db, tmp_path: Path):
    session = await session_manager.get_or_create("audio-session-direct-auth", owner_principal_id=OPERATOR_OWNER)
    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport({"transcript": "must not run"}),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        queued = await worker.submit(_request(session.id, request_id="audio-direct-auth"), process=False)

    with (
        patch.object(worker, "_expire_if_needed", new_callable=AsyncMock) as expire,
        patch.object(worker, "_cleanup_job_paths") as cleanup,
    ):
        with pytest.raises(AudioWorkerError) as read_error:
            await worker._snapshot_by_request(
                queued.request_id,
                owner_principal_id=OPERATOR_OWNER,
                operator_session_id="operator-session-attacker",
            )
        with pytest.raises(AudioWorkerError) as exc_info:
            await worker.process(
                queued.request_id,
                owner_principal_id=OPERATOR_OWNER,
                operator_session_id="operator-session-attacker",
            )

    assert read_error.value.code == "audio_operator_session_mismatch"
    assert exc_info.value.code == "audio_operator_session_mismatch"
    expire.assert_not_awaited()
    cleanup.assert_not_called()


@pytest.mark.asyncio
async def test_audio_worker_direct_process_reauthenticates_before_expiry_cleanup(async_db, tmp_path: Path):
    session = await session_manager.get_or_create("audio-session-direct-revoked", owner_principal_id=OPERATOR_OWNER)
    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport({"transcript": "must not run"}),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        queued = await worker.submit(_request(session.id, request_id="audio-direct-revoked"), process=False)
    await revoke_session(OPERATOR_SESSION)

    with (
        patch.object(worker, "_expire_if_needed", new_callable=AsyncMock) as expire,
        patch.object(worker, "_cleanup_job_paths") as cleanup,
    ):
        with pytest.raises(AudioWorkerError) as exc_info:
            await worker.process(
                queued.request_id,
                owner_principal_id=OPERATOR_OWNER,
                operator_session_id=OPERATOR_SESSION,
            )
        with pytest.raises(AudioWorkerError) as read_info:
            await worker._snapshot_by_request(
                queued.request_id,
                owner_principal_id=OPERATOR_OWNER,
                operator_session_id=OPERATOR_SESSION,
            )

    assert exc_info.value.code == "audio_operator_session_invalid"
    assert read_info.value.code == "audio_operator_session_invalid"
    expire.assert_not_awaited()
    cleanup.assert_not_called()
    assert (await worker._snapshot_by_request(queued.request_id)).status == "queued"
    assert list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_audio_worker_transport_claim_rechecks_revocation_in_same_transaction(async_db, tmp_path: Path, monkeypatch):
    session = await session_manager.get_or_create("audio-session-transport-claim", owner_principal_id=OPERATOR_OWNER)
    calls = 0
    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport({"transcript": "must not run"}),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    original_boundary_check = worker._assert_current_transport_authority
    boundary_checks = 0

    async def revoke_before_claim(row):
        nonlocal boundary_checks
        if boundary_checks == 0:
            boundary_checks += 1
            await worker.revoke_consent_grant(
                MODEL_REF,
                owner_principal_id=OPERATOR_OWNER,
                operator_session_id=OPERATOR_SESSION,
            )
            return
        await original_boundary_check(row)

    def intercepted(**_kwargs):
        nonlocal calls
        calls += 1
        return {"transcript": "must not run"}

    worker.transport = InterceptedAudioTransport(intercepted)
    monkeypatch.setattr(worker, "_assert_current_transport_authority", revoke_before_claim)
    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        queued = await worker.submit(_request(session.id, request_id="audio-claim-revoked"), process=False)
        blocked = await worker.process(
            queued.request_id,
            owner_principal_id=OPERATOR_OWNER,
            operator_session_id=OPERATOR_SESSION,
        )

    assert blocked.status == "blocked"
    assert blocked.error_code == "cloud_upload_consent_revoked"
    assert calls == 0


@pytest.mark.asyncio
async def test_audio_worker_confirmation_keeps_message_job_pair_after_reauth_loss(async_db, tmp_path: Path, monkeypatch):
    session = await session_manager.get_or_create("audio-session-confirm-revoked-after-reserve", owner_principal_id=OPERATOR_OWNER)
    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport({"transcript": "generated"}),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    original_reserve = session_manager.reserve_ingress_message

    async def reserve_then_revoke(*args, **kwargs):
        await original_reserve(*args, **kwargs)
        await revoke_session(OPERATOR_SESSION)
        raise MessageIngressConflictError(str(kwargs["message_id"]))

    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        snapshot = await worker.submit(_request(session.id, request_id="audio-confirm-revoked-after-reserve"), process=True)
        monkeypatch.setattr(session_manager, "reserve_ingress_message", reserve_then_revoke)
        confirmed = await worker.confirm_transcript(
            snapshot.request_id,
            "operator confirmed",
            expected_transcript_digest=snapshot.transcript_digest,
            owner_principal_id=OPERATOR_OWNER,
            operator_session_id=OPERATOR_SESSION,
        )

    assert confirmed.status == "confirmed"
    assert await session_manager.get_message(snapshot.message_id) is not None


@pytest.mark.asyncio
async def test_audio_worker_confirmation_session_revoke_is_atomic_with_message_reservation(
    async_db,
    tmp_path: Path,
    monkeypatch,
):
    """A revoke racing the final auth check cannot create a canonical message."""
    session = await session_manager.get_or_create(
        "audio-session-confirm-revoke-fence",
        owner_principal_id=OPERATOR_OWNER,
    )
    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport({"transcript": "generated"}),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    original_authority = worker._require_current_operator_authority
    revoke_on_next_check = False

    async def revoke_after_last_check(**kwargs):
        nonlocal revoke_on_next_check
        principal = await original_authority(**kwargs)
        if revoke_on_next_check:
            revoke_on_next_check = False
            # The reserve transaction must re-read this durable row rather
            # than trusting the worker's earlier successful check.
            await revoke_session(OPERATOR_SESSION)
        return principal

    monkeypatch.setattr(worker, "_require_current_operator_authority", revoke_after_last_check)
    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        ready = await worker.submit(
            _request(session.id, request_id="audio-confirm-revoke-fence"),
            process=True,
        )
        revoke_on_next_check = True
        with pytest.raises(AudioConfirmationConflict) as exc_info:
            await worker.confirm_transcript(
                ready.request_id,
                "operator confirmed",
                expected_transcript_digest=ready.transcript_digest,
                owner_principal_id=OPERATOR_OWNER,
                operator_session_id=OPERATOR_SESSION,
            )

    assert exc_info.value.code == "canonical_message_identity_conflict"
    final = await worker._snapshot_by_request(ready.request_id)
    assert final.status == "transcript_ready"
    assert await session_manager.get_message(ready.message_id) is None


@pytest.mark.asyncio
async def test_audio_worker_confirmation_cleanup_failure_keeps_recoverable_pair(
    async_db,
    tmp_path: Path,
    monkeypatch,
):
    """A committed canonical message remains paired until quarantine cleanup succeeds."""
    session = await session_manager.get_or_create(
        "audio-session-confirm-cleanup-fence",
        owner_principal_id=OPERATOR_OWNER,
    )
    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport({"transcript": "generated"}),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        ready = await worker.submit(
            _request(session.id, request_id="audio-confirm-cleanup-fence"),
            process=True,
        )

    original_cleanup = worker._cleanup_job_paths
    monkeypatch.setattr(worker, "_cleanup_job_paths", lambda _raw, _normalized: False)
    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        with pytest.raises(AudioConfirmationConflict) as exc_info:
            await worker.confirm_transcript(
                ready.request_id,
                "operator confirmed",
                expected_transcript_digest=ready.transcript_digest,
                owner_principal_id=OPERATOR_OWNER,
                operator_session_id=OPERATOR_SESSION,
            )
    assert exc_info.value.code == "confirmation_cancelled"
    degraded = await worker._snapshot_by_request(ready.request_id)
    assert degraded.status == "degraded"
    assert degraded.error_code == "audio_cleanup_failed"
    assert degraded.confirmed_transcript_digest == hashlib.sha256(b"operator confirmed").hexdigest()
    assert await session_manager.get_message(ready.message_id) is not None

    monkeypatch.setattr(worker, "_cleanup_job_paths", original_cleanup)
    recovered = await worker.cleanup_after_restart()
    assert recovered == 1
    confirmed = await worker._snapshot_by_request(ready.request_id)
    assert confirmed.status == "confirmed"
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_audio_worker_cleanup_failure_is_degraded_and_retryable(async_db, tmp_path: Path, monkeypatch):
    session = await session_manager.get_or_create("audio-session-cleanup-retry", owner_principal_id=OPERATOR_OWNER)
    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport({"transcript": "unused"}),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        queued = await worker.submit(_request(session.id, request_id="audio-cleanup-retry"), process=False)

    original_unlink = worker._unlink_tree
    monkeypatch.setattr(worker, "_unlink_tree", lambda _path: False)
    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        degraded = await worker.cancel(
            queued.request_id,
            owner_principal_id=OPERATOR_OWNER,
            operator_session_id=OPERATOR_SESSION,
        )
    assert degraded.status == "degraded"
    assert degraded.error_code == "audio_cleanup_failed"
    assert degraded.cleanup_status == "failed"
    assert degraded.as_dict()["cleanup"] == {"status": "failed", "retryable": True}
    assert degraded.as_dict()["privacy"]["raw_audio_persisted_after_cleanup"] is True
    assert list(tmp_path.iterdir())

    monkeypatch.setattr(worker, "_unlink_tree", original_unlink)
    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        recovered = await worker._snapshot_by_request(
            queued.request_id,
            owner_principal_id=OPERATOR_OWNER,
            operator_session_id=OPERATOR_SESSION,
        )
    assert recovered.status == "degraded"
    assert recovered.cleanup_status == "complete"
    assert recovered.as_dict()["privacy"]["raw_audio_persisted_after_cleanup"] is False
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_audio_worker_cancel_fences_late_transport_result(async_db, tmp_path: Path):
    session = await session_manager.get_or_create("audio-session-cancel-race", owner_principal_id=OPERATOR_OWNER)
    started = asyncio.Event()
    calls = 0

    async def intercepted(**_kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.Future()

    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport(intercepted),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        queued = await worker.submit(_request(session.id, request_id="audio-cancel-race"), process=False)
        processing = asyncio.create_task(
            worker.process(
                queued.request_id,
                owner_principal_id=OPERATOR_OWNER,
                operator_session_id=OPERATOR_SESSION,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        cancelled = await worker.cancel(
            queued.request_id,
            owner_principal_id=OPERATOR_OWNER,
            operator_session_id=OPERATOR_SESSION,
        )
        assert cancelled.status == "degraded"
        assert cancelled.error_code == "audio_transport_outcome_unknown"
        with pytest.raises(asyncio.CancelledError):
            await processing
        final = await worker._snapshot_by_request(queued.request_id)
        assert final.status == "degraded"
        assert final.error_code == "audio_transport_outcome_unknown"
        assert final.transcript is None
        assert final.transcript_digest is None
        assert calls == 1


@pytest.mark.asyncio
async def test_audio_worker_confirmation_reservation_is_cancel_fenced(async_db, tmp_path: Path, monkeypatch):
    session = await session_manager.get_or_create("audio-session-confirm-cancel-race", owner_principal_id=OPERATOR_OWNER)
    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport({"transcript": "generated"}),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    original_reserve = session_manager.reserve_ingress_message

    async def reserve_after_cancel(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original_reserve(*args, **kwargs)

    monkeypatch.setattr(session_manager, "reserve_ingress_message", reserve_after_cancel)
    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        snapshot = await worker.submit(_request(session.id, request_id="audio-confirm-cancel-race"), process=True)
        confirming = asyncio.create_task(
            worker.confirm_transcript(
                snapshot.request_id,
                "operator confirmed",
                expected_transcript_digest=snapshot.transcript_digest,
                owner_principal_id=OPERATOR_OWNER,
                operator_session_id=OPERATOR_SESSION,
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2)
        cancelled = await worker.cancel(
            snapshot.request_id,
            owner_principal_id=OPERATOR_OWNER,
            operator_session_id=OPERATOR_SESSION,
        )
        assert cancelled.status == "cancelled"
        release.set()
        with pytest.raises(AudioConfirmationConflict) as exc_info:
            await confirming
        assert exc_info.value.code == "confirmation_cancelled"

    final = await worker._snapshot_by_request(snapshot.request_id)
    assert final.status == "cancelled"
    assert await session_manager.get_message(snapshot.message_id) is None
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_audio_worker_rechecks_current_model_authority_before_transport(async_db, tmp_path: Path):
    session = await session_manager.get_or_create("audio-session-current-authority", owner_principal_id=OPERATOR_OWNER)
    calls = 0

    def intercepted(**_kwargs):
        nonlocal calls
        calls += 1
        return {"transcript": "must not run"}

    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport(intercepted),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    revoked_principal = TrustPrincipal(
        principal_id=OPERATOR_OWNER,
        principal_type=PrincipalType.OPERATOR,
        authenticated=True,
        revoked=True,
        grants=(AuthorityGrant.MODEL_INFERENCE,),
        operator_session_id=OPERATOR_SESSION,
    )
    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        queued = await worker.submit(_request(session.id, request_id="audio-current-authority"), process=False)
        async with async_db() as db:
            operator_session = await db.get(OperatorSession, OPERATOR_SESSION)
            assert operator_session is not None
            operator_session.revoked_at = datetime.now(timezone.utc)
            db.add(operator_session)
        blocked = await worker.process(
            queued.request_id,
            owner_principal_id=OPERATOR_OWNER,
            operator_session_id=OPERATOR_SESSION,
            authority_principal=revoked_principal,
        )
    assert blocked.status == "blocked"
    assert blocked.error_code == "model_inference_grant_revoked"
    assert calls == 0
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_audio_worker_revocation_after_transport_returns_typed_unknown(async_db, tmp_path: Path):
    session = await session_manager.get_or_create("audio-session-transport-revocation", owner_principal_id=OPERATOR_OWNER)
    calls = 0
    worker: AudioIngressWorker

    async def intercepted(**_kwargs):
        nonlocal calls
        calls += 1
        await worker.revoke_consent_grant(
            MODEL_REF,
            owner_principal_id=OPERATOR_OWNER,
            operator_session_id=OPERATOR_SESSION,
        )
        return {"transcript": "must not become canonical"}

    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport(intercepted),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        queued = await worker.submit(_request(session.id, request_id="audio-transport-revocation"), process=False)
        result = await worker.process(
            queued.request_id,
            owner_principal_id=OPERATOR_OWNER,
            operator_session_id=OPERATOR_SESSION,
        )

    assert calls == 1
    assert result.status == "degraded"
    assert result.error_code == "audio_transport_outcome_unknown"
    final = await worker._snapshot_by_request(queued.request_id)
    assert final.status == "degraded"
    assert final.error_code == "audio_transport_outcome_unknown"
    assert await session_manager.get_message(queued.message_id) is None


@pytest.mark.asyncio
async def test_audio_worker_runtime_cleanup_expires_deadline_and_files(async_db, tmp_path: Path):
    session = await session_manager.get_or_create("audio-session-runtime-cleanup", owner_principal_id=OPERATOR_OWNER)
    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport({"transcript": "unused"}),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        queued = await worker.submit(_request(session.id, request_id="audio-runtime-cleanup"), process=False)
    assert list(tmp_path.iterdir())
    async with async_db() as db:
        row = await db.get(AudioIngressJob, queued.id)
        assert row is not None
        row.raw_audio_retention_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.add(row)
    removed = await worker.cleanup_after_restart(
        now=datetime.now(timezone.utc),
        recover_process_local=False,
    )
    assert removed == 1
    assert not list(tmp_path.iterdir())
    final = await worker._snapshot_by_request(queued.request_id)
    assert final.status == "failed"
    assert final.error_code == "restart_cleanup_expired"


@pytest.mark.asyncio
async def test_audio_worker_recovery_expires_confirming_rows(async_db, tmp_path: Path):
    session = await session_manager.get_or_create("audio-session-confirming-recovery", owner_principal_id=OPERATOR_OWNER)
    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport({"transcript": "unused"}),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        queued = await worker.submit(_request(session.id, request_id="audio-confirming-recovery"), process=False)
    async with async_db() as db:
        row = await db.get(AudioIngressJob, queued.id)
        assert row is not None
        row.status = "confirming"
        row.transcript = None
        row.transcript_digest = "a" * 64
        row.confirmed_transcript_digest = "b" * 64
        db.add(row)
    worker._review_transcripts[queued.request_id] = ("process-local review", "a" * 64)

    assert await worker.cleanup_after_restart() == 1
    final = await worker._snapshot_by_request(queued.request_id)
    assert final.status == "failed"
    assert final.error_code == "restart_recovery_required"
    assert final.transcript_digest is None
    assert final.confirmed_transcript_digest is None
    assert queued.request_id not in worker._review_transcripts


@pytest.mark.asyncio
async def test_audio_worker_confirmation_recovers_after_message_commit(async_db, tmp_path: Path, monkeypatch):
    session = await session_manager.get_or_create("audio-session-confirm-retry", owner_principal_id=OPERATOR_OWNER)
    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport({"transcript": "generated"}),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    original_reserve = session_manager.reserve_ingress_message

    async def reserve_then_crash(*args, **kwargs):
        await original_reserve(*args, **kwargs)
        raise MessageIngressConflictError(str(kwargs["message_id"]))

    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        snapshot = await worker.submit(_request(session.id, request_id="audio-confirm-retry"), process=True)
        monkeypatch.setattr(session_manager, "reserve_ingress_message", reserve_then_crash)
        confirmed = await worker.confirm_transcript(
            snapshot.request_id,
            "operator confirmed",
            expected_transcript_digest=snapshot.transcript_digest,
            owner_principal_id=OPERATOR_OWNER,
            operator_session_id=OPERATOR_SESSION,
        )
        assert confirmed.status == "confirmed"
        retry = await worker.confirm_transcript(
            snapshot.request_id,
            "operator confirmed",
            expected_transcript_digest=snapshot.transcript_digest,
            owner_principal_id=OPERATOR_OWNER,
            operator_session_id=OPERATOR_SESSION,
        )
        assert retry.status == "confirmed"


@pytest.mark.asyncio
async def test_audio_worker_restart_reconciles_reserved_canonical_message(async_db, tmp_path: Path, monkeypatch):
    session = await session_manager.get_or_create("audio-session-confirm-restart", owner_principal_id=OPERATOR_OWNER)
    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport({"transcript": "generated"}),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    original_cleanup = worker._cleanup_job_paths

    def crash_after_message_commit(_raw_path, _normalized_path):
        raise RuntimeError("simulated worker crash after canonical commit")

    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        snapshot = await worker.submit(_request(session.id, request_id="audio-confirm-restart"), process=True)
        monkeypatch.setattr(worker, "_cleanup_job_paths", crash_after_message_commit)
        with pytest.raises(RuntimeError, match="simulated worker crash"):
            await worker.confirm_transcript(
                snapshot.request_id,
                "operator confirmed",
                expected_transcript_digest=snapshot.transcript_digest,
                owner_principal_id=OPERATOR_OWNER,
                operator_session_id=OPERATOR_SESSION,
            )

    monkeypatch.setattr(worker, "_cleanup_job_paths", original_cleanup)
    reserved = await worker._snapshot_by_request(snapshot.request_id)
    assert reserved.status == "confirming_reserved"
    assert await session_manager.get_message(snapshot.message_id) is not None

    assert await worker.cleanup_after_restart() == 1
    recovered = await worker._snapshot_by_request(snapshot.request_id)
    assert recovered.status == "confirmed"
    assert recovered.confirmed_transcript_digest == hashlib.sha256(b"operator confirmed").hexdigest()
    assert await session_manager.get_message(snapshot.message_id) is not None


@pytest.mark.asyncio
async def test_audio_worker_confirmation_rejects_tampered_existing_message(async_db, tmp_path: Path, monkeypatch):
    session = await session_manager.get_or_create("audio-session-confirm-tamper", owner_principal_id=OPERATOR_OWNER)
    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport({"transcript": "generated"}),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    original_reserve = session_manager.reserve_ingress_message

    async def reserve_tampered(*args, **kwargs):
        metadata = json.loads(kwargs["metadata_json"])
        metadata["audio"]["provider"] = "tampered"
        await session_manager.add_message(
            args[0],
            "user",
            args[1],
            metadata_json=json.dumps(metadata, sort_keys=True),
            message_id=kwargs["message_id"],
            attachment_refs=kwargs["attachment_refs"],
        )
        return await original_reserve(*args, **kwargs)

    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        snapshot = await worker.submit(_request(session.id, request_id="audio-confirm-tamper"), process=True)
        monkeypatch.setattr(session_manager, "reserve_ingress_message", reserve_tampered)
        with pytest.raises(AudioConfirmationConflict) as exc_info:
            await worker.confirm_transcript(
                snapshot.request_id,
                "operator confirmed",
                expected_transcript_digest=snapshot.transcript_digest,
                owner_principal_id=OPERATOR_OWNER,
                operator_session_id=OPERATOR_SESSION,
            )
        assert exc_info.value.code == "canonical_message_identity_conflict"
        assert (
            await worker.cancel(
                snapshot.request_id,
                owner_principal_id=OPERATOR_OWNER,
                operator_session_id=OPERATOR_SESSION,
            )
        ).status == "cancelled"


@pytest.mark.asyncio
async def test_session_delete_removes_audio_rows_before_foreign_key(async_db, tmp_path: Path):
    session = await session_manager.get_or_create("audio-session-delete", owner_principal_id=OPERATOR_OWNER)
    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport({"transcript": "unused"}),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        queued = await worker.submit(_request(session.id, request_id="audio-delete"), process=False)
        worker._review_transcripts[queued.request_id] = ("process-local review", "a" * 64)
        assert await session_manager.delete(session.id, owner_principal_id=OPERATOR_OWNER) is True
        assert queued.request_id not in worker._review_transcripts
        async with async_db() as db:
            assert await db.get(AudioIngressJob, queued.id) is None


@pytest.mark.asyncio
async def test_session_delete_preserves_audio_job_when_cleanup_fails(
    async_db,
    tmp_path: Path,
    monkeypatch,
):
    session = await session_manager.get_or_create(
        "audio-session-delete-cleanup-failure",
        owner_principal_id=OPERATOR_OWNER,
    )
    worker = AudioIngressWorker(
        transport=InterceptedAudioTransport({"transcript": "unused"}),
        admission_broker=RemoteInferenceAdmissionBroker(),
        quarantine_root=tmp_path,
    )
    with patch.object(settings, "operator_auth_secret", "test-audio-secret"):
        queued = await worker.submit(
            _request(session.id, request_id="audio-delete-cleanup-failure"),
            process=False,
        )

    monkeypatch.setattr(
        "src.guardian.audio_worker.cleanup_audio_job_paths",
        lambda _raw_path, _normalized_path: False,
    )
    assert await session_manager.delete(session.id, owner_principal_id=OPERATOR_OWNER) is False
    assert await session_manager.get(session.id, owner_principal_id=OPERATOR_OWNER) is not None
    async with async_db() as db:
        row = await db.get(AudioIngressJob, queued.id)
        assert row is not None
        assert row.normalized_path is not None
    assert list(tmp_path.iterdir())
