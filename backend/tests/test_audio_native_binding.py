"""Isolated SQLite authority receipts; scripted proof is never readiness."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select
from src.db.models import AudioIngressJob, AudioConsentGrant, Message
from src.guardian.audio_worker import AudioIngressWorker, _run_process, AudioWorkerError
from src.workflows.audio_native import AudioNativeAdmissionBindingV1, AUTHORITY_KEY
from src.workflows.job_runtime import DurableJobSpec, DurableJobIdentity, DurableJobRepository
from src.agent.session import session_manager
from tests.test_audio_worker import audio_worker_authority, _request, OPERATOR_OWNER, OPERATOR_SESSION


@pytest.fixture
async def native(async_db, tmp_path, monkeypatch, audio_worker_authority):
    from src.llm_runtime import _provider_profile
    import src.llm_runtime as runtime
    import src.model_fabric.effective_policy as policy
    import src.model_fabric.audio_contracts as proof
    from config.settings import settings
    monkeypatch.setattr(settings, "operator_auth_secret", "isolated-audio-proof-secret")
    monkeypatch.setattr(runtime, "_provider_profile", lambda _: SimpleNamespace(contract_hash="1"*64))
    monkeypatch.setattr(policy, "current_inference_policy", lambda: (None, "2"*64))
    monkeypatch.setattr(proof, "audio_route_witness", AsyncMock(return_value=(SimpleNamespace(pricing=SimpleNamespace(reserve_microusd=5)), "3"*64)))
    session = await session_manager.get_or_create("audio-native-conversation", owner_principal_id=OPERATOR_OWNER)
    worker = AudioIngressWorker(quarantine_root=tmp_path)
    request = _request(session.id)
    capture = await worker.submit(request, process=False)
    row = await worker._job(capture.request_id)
    job_id = "audio-transcription:" + hashlib.sha256(row.id.encode()).hexdigest()[:40]
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    binding = AudioNativeAdmissionBindingV1("audio-native-admission.v1", row.id, job_id, "remote:"+job_id,
        OPERATOR_OWNER, OPERATOR_SESSION, session.id, row.request_digest,
        row.capture_consent_reference, row.model_consent_reference, "1"*64, "3"*64, "2"*64,
        deadline.isoformat(), 10, 1)
    spec = DurableJobSpec(identity=DurableJobIdentity(job_id=job_id, job_kind="audio_transcription_v1",
        owner_kind="user", owner_principal_id=OPERATOR_OWNER, capability_version="audio-transcription-v1",
        idempotency_scope="audio-ingress", idempotency_key=row.request_id),
        session_id=session.id, operator_session_id=OPERATOR_SESSION, deadline_at=deadline,
        resource_claims=("remote_inference",), budget_microusd=10, declared_authority={
            "principal": OPERATOR_OWNER, "owner_kind": "user", "session_id": session.id,
            "budget_microusd": 10, AUTHORITY_KEY: binding.payload()})
    return worker, row, binding, spec, DurableJobRepository()


@pytest.mark.asyncio
async def test_grant_pair_admission_exact_replay_changed_budget_conflicts(native, async_db):
    worker, row, binding, spec, jobs = native
    first = await jobs.admit_job(spec, audio_admission=binding)
    repeated = await jobs.admit_job(spec, audio_admission=binding)
    assert first["job_id"] == repeated["job_id"] == binding.workflow_job_id
    assert first["goal_id"] is None
    assert first["declared_authority"][AUTHORITY_KEY] == binding.payload()
    async with async_db() as db:
        audio = await db.get(AudioIngressJob, row.id)
        grant = (await db.execute(select(AudioConsentGrant).where(AudioConsentGrant.reference == binding.model_grant_ref))).scalars().one()
        assert audio.workflow_job_id == binding.workflow_job_id and audio.revision == 1
        assert json.loads(grant.audio_execution_binding_json) == binding.payload()
    changed = replace(binding, audio_budget_microusd=11)
    changed_spec = replace(spec, budget_microusd=11, declared_authority={**spec.declared_authority,
        "budget_microusd": 11, AUTHORITY_KEY: changed.payload()})
    with pytest.raises(RuntimeError, match="audio_execution_binding_changed"):
        await jobs.admit_job(changed_spec, audio_admission=changed)


@pytest.mark.asyncio
async def test_tampered_source_and_mapping_cannot_mint_authority(native, async_db):
    worker, row, binding, spec, jobs = native
    with pytest.raises(RuntimeError, match="audio_native_binding_required"):
        await jobs.admit_job(spec, audio_admission=binding.payload())
    Path(row.raw_path).write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="audio_source_digest_stale"):
        await jobs.admit_job(spec, audio_admission=binding)
    async with async_db() as db:
        grant = (await db.execute(select(AudioConsentGrant).where(AudioConsentGrant.reference == binding.model_grant_ref))).scalars().one()
        assert grant.audio_execution_binding_json is None
        assert (await db.get(AudioIngressJob, row.id)).workflow_job_id is None


@pytest.mark.asyncio
async def test_pair_claim_cancel_suppresses_late_publication(native, async_db):
    worker, row, binding, spec, jobs = native
    admitted = await jobs.admit_job(spec, audio_admission=binding)
    audio = await worker._job(row.request_id)
    claimed = await jobs.audio_transition(binding.workflow_job_id, expected_revision=admitted["revision"],
        expected_audio_revision=audio.revision, status="running", audio_changes={"status": "processing"}, owner="test-native", fencing_token=0)
    async with async_db() as db:
        await jobs.cancel_audio_in_session(db, binding.workflow_job_id, reason="operator_cancelled")
    with pytest.raises(RuntimeError, match="audio_pair_revision_stale"):
        await jobs.audio_transition(binding.workflow_job_id, expected_revision=claimed["revision"],
            expected_audio_revision=audio.revision+1, status="succeeded", owner="test-native", fencing_token=1,
            audio_changes={"status": "transcript_ready", "transcript": "late"})
    assert (await worker._job(row.request_id)).transcript is None


@pytest.mark.asyncio
async def test_restart_before_any_reservation_preserves_binding_budget_and_fences_old_owner(native):
    worker, row, binding, spec, jobs = native
    admitted = await jobs.admit_job(spec, audio_admission=binding)
    audio = await worker._job(row.request_id)
    claimed = await jobs.audio_transition(binding.workflow_job_id, expected_revision=admitted["revision"],
        expected_audio_revision=audio.revision, status="running", audio_changes={"status": "processing"},
        owner="dead-native", fencing_token=0)
    await jobs.recover_audio_owner()
    recovered = await jobs.get_job(binding.workflow_job_id)
    assert recovered["status"] == "queued" and recovered["lease"]["fencing_token"] == 2
    assert recovered["declared_authority"][AUTHORITY_KEY] == binding.payload()
    assert recovered["declared_authority"]["budget_microusd"] == 10
    with pytest.raises(RuntimeError, match="audio_pair_revision_stale"):
        await jobs.audio_transition(binding.workflow_job_id, expected_revision=claimed["revision"],
            expected_audio_revision=audio.revision+1, status="failed", audio_changes={"status": "blocked"},
            owner="dead-native", fencing_token=1, require_current=False)


@pytest.mark.asyncio
async def test_real_process_overflow_timeout_and_reap():
    import sys
    with pytest.raises(AudioWorkerError, match="audio processing failed") as overflow:
        await _run_process([sys.executable, "-c", "import os; os.write(1,b'x'*100000)"], timeout=1)
    assert overflow.value.code == "decoder_output_exceeds_limit"
    with pytest.raises(AudioWorkerError) as timeout:
        await _run_process([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.05)
    assert timeout.value.code == "decoder_timeout"


@pytest.mark.asyncio
async def test_installed_decoder_actual_file_only_wav_process(tmp_path, monkeypatch):
    import src.guardian.audio_worker as module
    from src.guardian.audio_worker import _decode_with_ffmpeg
    from tests.test_audio_worker import _wav
    raw, normalized = tmp_path / "quarantine.bin", tmp_path / "normalized.wav"
    raw.write_bytes(_wav(rate=16000, seconds=.1, channels=1))
    original, receipts = module._run_process, []
    async def recorded(command, **kwargs):
        result = await original(command, **kwargs)
        receipts.append((command[0], result[0], result[2].decode(errors="replace")))
        return result
    monkeypatch.setattr(module, "_run_process", recorded)
    try:
        result = await _decode_with_ffmpeg(raw, normalized, ("audio/wav", "wav", "pcm_s16le"),
            original_deadline_at=datetime.now(timezone.utc) + timedelta(seconds=20))
    except AudioWorkerError:
        pytest.fail(repr(receipts))
    assert normalized.is_file() and result.normalized_duration_seconds > 0


@pytest.mark.parametrize("field,value", [("audio_budget_microusd", True), ("max_calls", True), ("max_calls", 2),
    ("original_deadline_at", "2026-01-01T00:00:00"), ("profile_hash", "a"*65)])
@pytest.mark.asyncio
async def test_native_contract_strict(native, field, value):
    with pytest.raises(ValueError):
        replace(native[2], **{field: value})
