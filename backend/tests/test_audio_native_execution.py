"""Isolated documentary-owner mechanics; no production readiness evidence.

The absent documentary issuer is scripted explicitly. Auth, physical WAV,
paired writer, broker, accounting, route receipts and response parsing execute.
Only the final provider HTTP response is scripted after those local boundaries.
"""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
import time
import base64

import httpx
import pytest
from sqlmodel import select

from config.settings import settings
from src.agent.session import session_manager
from src.db.models import AudioIngressJob, InferenceCostReservation, ModelRouteAttemptReceiptRecord
from src.guardian.audio_worker import AudioIngressWorker
from src.model_fabric.configuration import ModelFabricConfiguration, write_model_fabric_configuration, openrouter_profiles_for_setup
from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
from src.workflows.job_runtime import durable_job_repository as jobs
from src.workspace.production import ProductionWorkspace, prepare_lifecycle_directory
from tests.test_audio_worker import audio_worker_authority, _request, _wav, OPERATOR_OWNER
from tests.test_audio_model_fabric_v3 import configured_audio, evidence, valid_response


def test_authoritative_money_preserves_sub_micro_dollar_remainder():
    from src.workflows.inference_accounting import account_charge_microusd
    value = Decimal("0.000001000000000000000000000000000001")
    assert account_charge_microusd({"usage": {"cost": value}})[0] == 2


@pytest.fixture
async def execution(async_db, audio_worker_authority, tmp_path, monkeypatch):
    import src.model_fabric.audio_contracts as contracts
    import src.model_fabric.selector as selector
    from src.model_fabric.repository import model_fabric_repository
    from src.model_fabric.proofs import build_model_route_proof
    from src.model_fabric.contracts import EndpointClass
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(settings, "workspace_dir", str(root))
    monkeypatch.setenv("SERAPH_WORKSPACE_LIFECYCLE_PATH", str(tmp_path / "lifecycle"))
    prepare_lifecycle_directory(ProductionWorkspace(host_root=root))
    monkeypatch.setattr(settings, "operator_auth_secret", "isolated-native-secret")
    monkeypatch.setattr(settings, "openrouter_provider_only", True)
    monkeypatch.setattr(settings, "openrouter_api_key", "scripted-fixture-key")
    setup = configured_audio()
    write_model_fabric_configuration(ModelFabricConfiguration(status="ready", openrouter_setup=setup,
        profiles=openrouter_profiles_for_setup(setup), egress_revision=1))
    await jobs.configure_inference_accounting(10000)
    from src.llm_runtime import _provider_profile
    profile = _provider_profile("openrouter.audio")
    assert profile is not None
    witness = evidence(profile)
    proofs = {}
    for capability in ("text", "audio_input", "health", "latency_ms"):
        value = witness.model_dump_json() if capability == "audio_input" else {"health": "healthy", "latency_ms": 1}.get(capability)
        proofs[capability] = build_model_route_proof(profile=profile, endpoint_class=EndpointClass.REMOTE,
            adapter="openai_compatible_chat", capability=capability, canary_version="scripted.v1",
            outcome="passed", checked_at=time.time()-1, expires_at=time.time()+3600,
            probe_receipt_id="scripted-documentary-owner", probe_receipt_hash="a"*64, proven_value=value)
    async def latest(**kwargs):
        return proofs.get(kwargs["capability"])
    async def scripted_documentary_owner(selected, proof_ref=None):
        assert selected.contract_hash == profile.contract_hash
        assert proof_ref is None or proof_ref == proofs["audio_input"].proof_hash
        return witness, proofs["audio_input"].proof_hash
    original_reason = selector._proof_reason
    def isolated_issuer_reason(*args, **kwargs):
        reason = original_reason(*args, **kwargs)
        return None if reason == "audio_documentary_acquisition_unavailable" else reason
    monkeypatch.setattr(model_fabric_repository, "latest_capability_proof", latest)
    monkeypatch.setattr(contracts, "audio_route_witness", scripted_documentary_owner)
    monkeypatch.setattr(selector, "_proof_reason", isolated_issuer_reason)
    class Calls(list):
        payload = valid_response()
    calls = Calls()
    original_client = httpx.AsyncClient
    def provider_client(**kwargs):
        def respond(request):
            assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
            calls.append(json.loads(request.content))
            class Stream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield json.dumps(calls.payload).encode()
            return httpx.Response(200, stream=Stream())
        return original_client(**kwargs, transport=httpx.MockTransport(respond))
    monkeypatch.setattr(httpx, "AsyncClient", provider_client)
    session = await session_manager.get_or_create("native-execution-conversation", owner_principal_id=OPERATOR_OWNER)
    worker = AudioIngressWorker(quarantine_root=tmp_path / "private-audio",
        admission_broker=RemoteInferenceAdmissionBroker(durable_accounting=True))
    capture = await worker.submit(replace(_request(session.id), audio_bytes=_wav(rate=16000, seconds=.03, channels=1)), process=False)
    return worker, capture, calls


@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.asyncio
async def test_native_actual_broker_settlement_route_and_corrected_message(execution, async_db):
    worker, capture, calls = execution
    result = await worker.process(capture.request_id, audio_budget_microusd=100,
        owner_principal_id=capture.owner_principal_id, operator_session_id=capture.operator_session_id)
    assert result.status == "transcript_ready"
    assert len(calls) == 1

    assert calls[0]["provider"]["only"] == ["deepinfra/turbo"]
    run = await jobs.get_job(result.workflow_job_id)
    assert run["goal_id"] is None and run["session_id"] != run["operator_session_id"]
    async with async_db() as db:
        cost = (await db.execute(select(InferenceCostReservation))).scalars().one()
        assert cost.state == "settled" and cost.actual_cost_microusd == 66
        assert cost.provider_operation_id == "gen-scripted" and cost.contact_started_at is not None
        assert (await db.execute(select(ModelRouteAttemptReceiptRecord))).scalars().all()
    confirmed = await worker.confirm_transcript(capture.request_id, "Corrected canonical intent",
        expected_transcript_digest=result.transcript_digest,
        owner_principal_id=capture.owner_principal_id, operator_session_id=capture.operator_session_id)
    assert confirmed.status == "confirmed"
    from src.db.models import Message
    async with async_db() as db:
        message = await db.get(Message, confirmed.message_id)
        assert message.content == "Corrected canonical intent"
    assert len(calls) == 1


@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.asyncio
async def test_contacted_unknown_retains_original_allowance_and_never_replays(execution, async_db):
    from src.guardian.audio_worker import AudioWorkerError
    worker, capture, calls = execution
    calls.payload = {**valid_response(), "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}
    with pytest.raises(AudioWorkerError):
        await worker.process(capture.request_id, audio_budget_microusd=100,
            owner_principal_id=capture.owner_principal_id, operator_session_id=capture.operator_session_id)
    audio = await worker._job(capture.request_id)
    assert audio.transcript is None and audio.admission_operation_id
    async with async_db() as db:
        cost = (await db.execute(select(InferenceCostReservation))).scalars().one()
        assert cost.state == "unknown" and cost.actual_cost_microusd is None
        assert cost.bound_microusd == 100 and cost.contact_started_at is not None
    await jobs.recover_audio_owner()
    replay = await worker.process(capture.request_id, audio_budget_microusd=100,
        owner_principal_id=capture.owner_principal_id, operator_session_id=capture.operator_session_id)
    assert replay.status == "blocked" and replay.transcript_digest is None
    assert len(calls) == 1


@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.asyncio
async def test_current_grant_revocation_prevents_native_contact(execution, async_db):
    from src.guardian.audio_worker import AudioWorkerError
    from src.db.models import WorkflowRunState
    worker, capture, calls = execution
    audio = await worker._job(capture.request_id)
    assert await worker.revoke_consent_grant(audio.model_consent_reference,
        owner_principal_id=audio.owner_principal_id, operator_session_id=audio.operator_session_id)
    with pytest.raises(AudioWorkerError):
        await worker.process(capture.request_id, audio_budget_microusd=100,
            owner_principal_id=capture.owner_principal_id, operator_session_id=capture.operator_session_id)
    assert calls == []
    async with async_db() as db:
        assert not (await db.execute(select(WorkflowRunState))).scalars().all()
        assert not (await db.execute(select(InferenceCostReservation))).scalars().all()


@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.asyncio
async def test_authenticated_api_capture_execution_confirmation_and_source_read(client, execution, monkeypatch):
    import src.api.audio as api
    import src.guardian.audio_worker as owner_module
    from src.db.models import OperatorSession
    worker, _other_capture, calls = execution
    monkeypatch.setattr(api, "default_audio_worker", worker)
    monkeypatch.setattr(owner_module, "default_audio_worker", worker)
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)
    origin = {"Origin": "http://localhost:3001"}
    login = await client.post("/api/auth/login", json={"password": "isolated-native-secret"}, headers=origin)
    assert login.status_code == 200, login.text
    root_id = login.json()["session_id"]
    async with jobs._session() as db:
        root = await db.get(OperatorSession, root_id)
    conversation = await session_manager.get_or_create("api-native-conversation", owner_principal_id=root.principal_id)
    grants = []
    for boundary in ("capture", "cloud_upload"):
        response = await client.post("/api/audio/ptt/consent", json={"boundary": boundary}, headers=origin)
        assert response.status_code == 200, response.text
        grants.append(response.json()["reference"])
    capture = await client.post("/api/audio/ptt", json={"session_id": conversation.id,
        "audio_base64": base64.b64encode(_wav(rate=16000, seconds=.03, channels=1)).decode(),
        "capture_consent_reference": grants[0], "model_consent_reference": grants[1]}, headers=origin)
    assert capture.status_code == 200, capture.text
    request_id = capture.json()["request_id"]
    result = await client.post(f"/api/audio/ptt/{request_id}/process", json={"audio_budget_microusd": 100, "max_calls": 1}, headers=origin)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "transcript_ready" and len(calls) == 1
    corrected = "Explicit corrected API intent"
    confirmed = await client.post(f"/api/audio/ptt/{request_id}/confirm", json={"transcript": corrected,
        "expected_transcript_digest": result.json()["transcript"]["digest"]}, headers=origin)
    assert confirmed.status_code == 200, confirmed.text
    readback = await client.get(f"/api/audio/ptt/{request_id}")
    assert readback.status_code == 200, readback.text
    assert readback.json()["transcript"]["text"] == corrected
    assert readback.json()["operator_session_id"] != readback.json()["session_id"]
