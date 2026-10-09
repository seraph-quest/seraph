"""Ordinary provider-free regressions for optional governed audio."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import base64
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from src.model_fabric.configuration import (
    OpenRouterSetup, OpenRouterRoute, OPENROUTER_SETUP_V2_SCHEMA_VERSION,
    OPENROUTER_SETUP_V3_SCHEMA_VERSION, _openrouter_setup_from_payload,
    _openrouter_setup_payload, migrate_openrouter_setup_to_v3,
    openrouter_profiles_for_setup, validate_openrouter_setup,
)
from src.model_fabric.audio_contracts import (
    AudioEndpointWitnessV1, AudioPricingWitnessV1, AudioOfficialEvidenceV1,
    validate_exact_audio_endpoint_slug, validate_audio_response,
    witness_digest, input_audio_payload,
)
from src.security.trust_contract import EgressClass


def legacy():
    return OpenRouterSetup(model_ids=("openrouter/vendor/test",), capabilities=("text",),
        allowed_upstreams=("vendor",), cloud_egress_acknowledged=True,
        egress_class=EgressClass.CLOUD_ALLOWED_FULL, spend_ceiling_microusd=10000,
        max_output_tokens=512, request_cost_bound_microusd=100)


def configured_audio():
    setup = migrate_openrouter_setup_to_v3(legacy())
    route = OpenRouterRoute(model_id="openrouter/vendor/audio", enabled=True,
        capabilities=("text", "audio_input"), allowed_upstreams=("deepinfra/turbo",),
        temperature=0, max_output_tokens=512, timeout_seconds=30,
        zero_data_retention=True, request_cost_bound_microusd=100)
    return replace(setup, routes={**setup.routes, "audio": route}, purpose_consents={"audio": 1})


def evidence(profile):
    now = datetime.now(timezone.utc) - timedelta(seconds=2)
    pricing = AudioPricingWitnessV1(schema_version="audio-pricing-witness.v1", model_id=profile.routing_model,
        upstream_endpoint_tag="deepinfra/turbo", api_kind="chat_completions", pricing_digest="a"*64,
        billing_units=("request", "input_audio_second", "output_token"),
        unit_rates_microusd={"request": "0", "input_audio_second": "1", "output_token": "0.01"},
        maximum_billable_units={"request": 1, "input_audio_second": "60", "output_token": 512},
        reserve_microusd=66, observed_at=now, expires_at=now+timedelta(hours=1))
    endpoint = AudioEndpointWitnessV1(schema_version="audio-endpoint-witness.v1", profile_hash=profile.contract_hash,
        model_id=profile.routing_model, upstream_endpoint_tag="deepinfra/turbo", api_kind="chat_completions",
        input_format="wav", container="wav", codec="pcm_s16le", output_kind="text",
        max_duration_millis=60000, max_input_bytes=2097152, max_output_tokens=512,
        metadata_digest="b"*64, format_evidence_digest="c"*64,
        pricing_witness_digest=witness_digest(pricing), observed_at=now, expires_at=now+timedelta(hours=1))
    return AudioOfficialEvidenceV1(schema_version="audio-official-evidence.v1", origin="official_documentation",
        endpoint=endpoint, pricing=pricing, exact_endpoint_identity_attested=True,
        all_charge_units_bounded_attested=True, format_source_digest="c"*64, pricing_source_digest="a"*64)


def test_populated_legacy_roundtrip_and_pure_audio_projection():
    one = legacy()
    assert _openrouter_setup_from_payload(_openrouter_setup_payload(one)) == one
    three = migrate_openrouter_setup_to_v3(one)
    assert three.routes["audio"] is None
    two = replace(three, schema_version=OPENROUTER_SETUP_V2_SCHEMA_VERSION,
        routes={key: value for key, value in three.routes.items() if key != "audio"})
    assert _openrouter_setup_payload(_openrouter_setup_from_payload(_openrouter_setup_payload(two))) == _openrouter_setup_payload(two)
    projected = migrate_openrouter_setup_to_v3(two)
    assert projected.routes["text"] == two.routes["text"]
    assert projected.purpose_consents == two.purpose_consents
    assert projected.credential_ref == two.credential_ref


@pytest.mark.parametrize("schema", ["seraph.openrouter.setup.v1", OPENROUTER_SETUP_V2_SCHEMA_VERSION])
def test_audio_cannot_extend_legacy_capabilities(schema):
    if schema.endswith("v1"):
        setup = replace(legacy(), capabilities=("text", "audio_input"))
    else:
        three = configured_audio()
        setup = replace(three, schema_version=schema)
    with pytest.raises(ValueError):
        validate_openrouter_setup(setup)


def test_v3_audio_roundtrip_full_suffix_only_and_three_other_routes_unchanged():
    setup = configured_audio()
    assert _openrouter_setup_payload(_openrouter_setup_from_payload(_openrouter_setup_payload(setup))) == _openrouter_setup_payload(setup)
    profiles = openrouter_profiles_for_setup(setup)
    audio = next(p for p in profiles if p.id == "openrouter.audio")
    assert audio.options["provider"]["only"] == ["deepinfra/turbo"]
    assert audio.task_classes == ("audio_transcription",)
    assert audio.capabilities == ("text", "audio_input")
    assert audio.transport_adapter == "openai_compatible_chat"
    assert next(p for p in profiles if p.id == "openrouter.text").options["provider"]["only"] == ["vendor"]


@pytest.mark.parametrize("slug", ["DeepInfra/turbo", "deepinfra/turbo/extra", "https://deepinfra", "vendor/*", "vendor/", "vendor%2Fturbo", " vendor", "vendor?x", "vendor\\x"])
def test_endpoint_rejects_ambiguous_or_transformed_tag(slug):
    with pytest.raises(ValueError):
        validate_exact_audio_endpoint_slug(slug)


def test_pricing_is_closed_finite_and_complete():
    profile = next(p for p in openrouter_profiles_for_setup(configured_audio()) if p.id == "openrouter.audio")
    price = evidence(profile).pricing.model_dump()
    for mutation in ({"reserve_microusd": 65}, {"billing_units": ("input_audio_second", "reasoning_token")},
        {"unit_rates_microusd": {"request": "NaN", "input_audio_second": "1", "output_token": "0.01"}},
        {"maximum_billable_units": {"request": True, "input_audio_second": "60", "output_token": 512}}):
        with pytest.raises(ValidationError):
            AudioPricingWitnessV1.model_validate({**price, **mutation})
    with pytest.raises(ValidationError):
        AudioOfficialEvidenceV1.model_validate({**evidence(profile).model_dump(), "origin": "test_fixture"})


def valid_response():
    return {"id": "gen-scripted", "model": "vendor/audio", "provider": "deepinfra/turbo",
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Literal transcript"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8, "cost": "0.000066"}}


def test_result_requires_exact_identity_billing_and_literal_transcript():
    assert validate_audio_response(valid_response(), model_id="openrouter/vendor/audio", upstream="deepinfra/turbo") == ("Literal transcript", "gen-scripted", 66)
    for mutation in ({"provider": "deepinfra"}, {"model": "vendor/other"}, {"choices": []},
        {"choices": [{"finish_reason": "length", "message": {"role": "assistant", "content": "truncated"}}]},
        {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "x", "audio": {}}}]},
        {"usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}},
        {"usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8, "cost": "NaN"}}):
        with pytest.raises(ValueError):
            validate_audio_response({**valid_response(), **mutation}, model_id="openrouter/vendor/audio", upstream="deepinfra/turbo")


def test_input_audio_has_no_url_or_noncanonical_base64():
    profile = next(p for p in openrouter_profiles_for_setup(configured_audio()) if p.id == "openrouter.audio")
    witness = evidence(profile).endpoint
    data = base64.b64encode(b"private-fixture").decode()
    assert input_audio_payload(data, "wav", witness)["input_audio"] == {"data": data, "format": "wav"}
    for invalid in ("https://audio", "data:audio/wav;base64,"+data, data+"\n"):
        with pytest.raises(ValueError):
            input_audio_payload(invalid, "wav", witness)


@pytest.mark.asyncio
async def test_shared_transfer_retains_research_envelope_and_audio_envelope(monkeypatch):
    from src import llm_runtime
    monkeypatch.setattr(llm_runtime, "assert_runtime_not_revoked", lambda: None)
    monkeypatch.setattr("src.model_fabric.accounting.assert_current_inference_policy", lambda: None)
    monkeypatch.setattr("src.model_fabric.accounting.capture_response_usage", lambda _: None)
    calls = []
    original = httpx.AsyncClient
    def client(**kwargs):
        assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
        def respond(request):
            calls.append(request)
            raw = json.dumps({"choices": [{"message": {"role": "assistant", "content": "x"*17000}}]}).encode()
            class Stream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield raw
            return httpx.Response(200, stream=Stream())
        return original(**kwargs, transport=httpx.MockTransport(respond))
    monkeypatch.setattr(httpx, "AsyncClient", client)
    decision = SimpleNamespace(allowed=True, selected=SimpleNamespace(adapter="openai_compatible_chat", endpoint="https://openrouter.ai/api/v1/chat/completions"))
    context = SimpleNamespace(deadline_at=time.time()+10, fallback_allowed=False)
    with pytest.raises(RuntimeError, match="invalid_research"):
        await llm_runtime._governed_research_chat_completion(decision=decision, context=context, body={}, api_key=None)
    payload = await llm_runtime._governed_bounded_chat_transfer(decision=decision, context=context, body={}, api_key=None, envelope="audio")
    assert len(payload["choices"][0]["message"]["content"]) == 17000
    assert len(calls) == 2  # In-process transport, zero sockets/provider calls.


@pytest.mark.asyncio
async def test_audio_accounting_rejects_missing_native_receipt_before_ephemeral_job(monkeypatch):
    from src.model_fabric import accounting
    from src.model_fabric.configuration import ModelFabricConfiguration
    from src.model_fabric.gpu_admission import GpuAdmissionRequest, GpuPriority
    from src.model_fabric.remote_inference_admission import RemoteInferenceBindingError
    setup = configured_audio()
    configured = ModelFabricConfiguration(status="ready", openrouter_setup=setup, egress_revision=1)
    monkeypatch.setattr("src.model_fabric.configuration.read_model_fabric_configuration", lambda: configured)
    monkeypatch.setattr(accounting, "_policy_for_runtime", lambda _: (configured, "d"*64))
    monkeypatch.setattr("src.model_fabric.remote_inference_admission.current_remote_inference_receipt_binding", lambda: None)
    request = GpuAdmissionRequest(operation_id="audio-original", owner_id="operator:original", session_id="conversation",
        job_id="audio-transcription:original", runtime_path="audio_transcription", data_digest="e"*64,
        priority=GpuPriority.INTERACTIVE_CHAT, deadline_at=time.time()+30)
    token = accounting._profile_bindings.set({request.operation_id: "openrouter.audio"})
    try:
        with pytest.raises(RemoteInferenceBindingError) as rejected:
            await accounting.DurableInferenceBrokerMixin()._prepare_accounting(request)
        assert rejected.value.code == "audio_native_binding_required"
    finally:
        accounting._profile_bindings.reset(token)


@pytest.mark.asyncio
async def test_settings_audio_absence_and_fixture_cannot_mint_ready(monkeypatch):
    from src.api import model_fabric_settings as api
    from src.model_fabric.configuration import ModelFabricConfiguration
    setup = configured_audio()
    monkeypatch.setattr(api.settings, "openrouter_api_key", "scripted-fixture-key")
    profile = next(p for p in openrouter_profiles_for_setup(setup) if p.id == "openrouter.audio")
    monkeypatch.setattr(api, "provider_profiles", lambda: {"openrouter.audio": profile})
    async def missing(**_):
        return None
    monkeypatch.setattr(api.model_fabric_repository, "latest_capability_proof", missing)
    status = await api._openrouter_setup_status(setup, configuration=ModelFabricConfiguration(openrouter_setup=setup, egress_revision=1))
    assert status["slot_statuses"]["audio"]["status"] == "blocked"
    assert status["slot_statuses"]["audio"]["error_code"] == "capability_proof_missing"
    assert "purpose_consents" not in status


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_v3_settings_actual_sqlite_save_consent_restart_and_legacy_conflict(client, tmp_path, monkeypatch):
    from config.settings import settings
    from src.workspace.production import ProductionWorkspace, prepare_lifecycle_directory
    from src.model_fabric.configuration import read_model_fabric_configuration
    from src.workflows.job_runtime import durable_job_repository
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setenv("SERAPH_WORKSPACE_LIFECYCLE_PATH", str(tmp_path.parent / (tmp_path.name+"-lifecycle")))
    prepare_lifecycle_directory(ProductionWorkspace(host_root=tmp_path))
    monkeypatch.setattr(settings, "openrouter_provider_only", True)
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    payload = _openrouter_setup_payload(configured_audio())
    for field in ("purpose_consents", "credential_fingerprint"):
        payload.pop(field, None)
    denied = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1, "openrouter_setup": payload})
    assert denied.status_code == 403, denied.text
    assert not (tmp_path/"model-fabric-settings.json").exists()
    payload["audio_egress_acknowledged"] = True
    saved = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1, "openrouter_setup": payload})
    assert saved.status_code == 200, saved.text
    configured = read_model_fabric_configuration()
    assert configured.openrouter_setup.schema_version == OPENROUTER_SETUP_V3_SCHEMA_VERSION
    assert configured.openrouter_setup.purpose_consents == {"audio": 3}
    assert configured.openrouter_setup.routes["audio"].allowed_upstreams == ("deepinfra/turbo",)
    snapshot = await durable_job_repository.inference_accounting_snapshot()
    assert snapshot["status"] == "ready" and snapshot["ceiling_microusd"] == 10000
    before = (tmp_path/"model-fabric-settings.json").read_bytes()
    reopened = await client.get("/api/settings/model-fabric")
    assert reopened.status_code == 200, reopened.text
    status = reopened.json()["openrouter_setup"]
    assert status["schema_version"] == OPENROUTER_SETUP_V3_SCHEMA_VERSION
    assert status["slot_statuses"]["audio"]["status"] == "configuration_required"
    assert status["slot_statuses"]["audio"]["error_code"] == "credential_missing"
    old = dict(payload)
    old["schema_version"] = OPENROUTER_SETUP_V2_SCHEMA_VERSION
    old.pop("audio_egress_acknowledged")
    old["routes"] = {k: v for k, v in payload["routes"].items() if k != "audio"}
    conflict = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 3, "openrouter_setup": old})
    assert conflict.status_code == 409, conflict.text
    assert (tmp_path/"model-fabric-settings.json").read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_actual_proof_repository_caller_documentary_json_never_authorizes_audio(async_db, monkeypatch):
    from src.db.models import ModelRouteReceiptRecord
    from src.model_fabric.repository import ModelFabricRepository, model_fabric_repository
    from src.model_fabric.proofs import build_model_route_proof
    from src.model_fabric.selector import candidate_from_profile
    from src.model_fabric.audio_contracts import audio_route_witness
    profile = next(p for p in openrouter_profiles_for_setup(configured_audio()) if p.id == "openrouter.audio")
    candidate = candidate_from_profile(profile)
    documentary = evidence(profile)
    now = time.time()
    proof = build_model_route_proof(profile=profile, endpoint_class=candidate.endpoint_class,
        adapter=candidate.adapter, capability="audio_input", canary_version="caller-documentary-assertion",
        outcome="passed", checked_at=now-2, expires_at=now+3600,
        probe_receipt_id="caller-receipt", probe_receipt_hash="d"*64,
        proven_value=documentary.model_dump_json())
    repository = ModelFabricRepository(async_db)
    async with async_db() as db:
        db.add(ModelRouteReceiptRecord(receipt_id="caller-receipt", receipt_hash="d"*64,
            request_id="caller-request", route_decision_id="caller-decision", runtime_path="capability_probe",
            workload="capability_probe", outcome="succeeded", actual_profile_id=profile.id,
            actual_model=profile.model, actual_adapter=candidate.adapter, destination_class="remote",
            egress_class="cloud_allowed_full", started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc), latency_ms=0))
    assert (await repository.persist_capability_proof(proof)).persisted
    monkeypatch.setattr(model_fabric_repository, "_session_provider", async_db)
    # An exact database row/receipt and syntactically valid origin assertions
    # still cannot substitute for the unadopted documentary issuance owner.
    with pytest.raises(PermissionError, match="audio_documentary_acquisition_unavailable"):
        await audio_route_witness(profile, proof_ref=proof.proof_hash)
