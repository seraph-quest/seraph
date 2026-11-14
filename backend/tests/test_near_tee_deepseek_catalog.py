"""#1167: DeepSeek V4.1 Flash through the NEAR TEE catalog gate.

NEAR's catalog record for ``deepseek-ai/DeepSeek-V4.1-Flash-1017`` omits
``isReady`` and lists no ``structured_outputs``. Readiness is inferred only for
an attested, verifiable vllm record; structured output is established by a
current capability proof, never by the catalog alone and never without one.

Provider-free: every NEAR request (catalog, capability checks, inference,
billing) is answered by the intercepted PR3b fixture transport, which fails
the test on any other host; real network transports and litellm are refused.
No eval, no live or paid call.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json

import pytest

from src.model_fabric.configuration import NearTeeSlot
from src.model_fabric.near_tee_catalog import (
    READINESS_ABSENT_FLAG_ATTESTED,
    READINESS_CATALOG_READY,
    REASONING_CHECK_OUTPUT_TOKENS,
    build_receipt,
    check_output_tokens,
    evaluate_catalog,
    parse_catalog_record,
    readiness_basis,
)
from tests.test_near_tee_text_slot import (  # noqa: F401  (fixtures)
    PROVIDER_KEY,
    NearFixture,
    _ABSENT,
    _configure,
    _login,
    isolated_broker,
)
from tests.test_near_tee_vision_slot import (  # noqa: F401  (fixtures)
    VisionFixture,
    _analyze,
    _details,
    _lane,
    _upload,
    vision_payload,
)
from tests.test_openrouter_setup_api import keyless_openrouter, model_fabric_workspace  # noqa: F401


DEEPSEEK = "deepseek-ai/DeepSeek-V4.1-Flash-1017"


def deepseek_catalog_record(**metadata_changes):
    """The fields of NEAR's ``/v1/model/deepseek-ai%2FDeepSeek-V4.1-Flash-1017``
    record as reported in #1167 (read 2026-11-14): vllm, attested, verifiable,
    **no** ``isReady``, ``supportedFeatures`` tools+reasoning (no
    ``structured_outputs``), an empty sampling list, no ``maxOutputLength``,
    text+image input, $0.30/M input and $1.20/M output, context 1,048,576.
    ``_ABSENT`` removes a field; other keyword arguments replace it."""
    metadata = {"verifiable": True, "contextLength": 1_048_576, "modelDisplayName": "DeepSeek V4.1 Flash",
        "modelDescription": "Served privately from NEAR AI's attested TEE infrastructure",
        "ownedBy": "deepseek-ai", "aliases": ["deepseek-ai/DeepSeek-V4-Flash"], "providerType": "vllm",
        "providerConfig": {}, "attestationSupported": True,
        "architecture": {"inputModalities": ["text", "image"], "outputModalities": ["text"]},
        "supportedFeatures": ["tools", "reasoning"], "supportedSamplingParameters": []}
    metadata.update(metadata_changes)
    metadata = {key: value for key, value in metadata.items() if value is not _ABSENT}
    return {"modelId": DEEPSEEK, "inputCostPerToken": {"amount": 300, "scale": 9, "currency": "USD"},
        "outputCostPerToken": {"amount": 1200, "scale": 9, "currency": "USD"}, "metadata": metadata}


def _facts(**changes):
    return parse_catalog_record(json.dumps(deepseek_catalog_record(**changes)).encode(), model_id=DEEPSEEK)


def _slot(**changes):
    values = dict(model_id=DEEPSEEK, max_input_tokens=8192, max_output_tokens=4096,
        request_cost_bound_microusd=20000, enabled=True)
    values.update(changes)
    return NearTeeSlot(**values)


# ── Pure catalog rules ───────────────────────────────────────────


def test_deepseek_record_passes_with_readiness_inferred_from_attestation():
    facts = _facts()
    assert facts.is_ready is None and facts.max_output_tokens is None
    assert facts.features == ("reasoning", "tools") and "structured_outputs" not in facts.features
    assert readiness_basis(facts) == READINESS_ABSENT_FLAG_ATTESTED
    # 8192 x 0.30/M + 4096 x 1.20/M = 2457.6 + 4915.2 microUSD -> 7373 (ceil)
    assert evaluate_catalog(facts, slot_name="text", slot=_slot()) == (None, 7373)
    # 16384 x 0.30/M + 4096 x 1.20/M = 4915.2 + 4915.2 -> 9831 (no listed image price)
    assert evaluate_catalog(facts, slot_name="vision", slot=_slot(max_input_tokens=16384)) == (None, 9831)
    # Every other gate is unchanged: unknown output limit, reserve, context, modality.
    assert evaluate_catalog(facts, slot_name="text", slot=_slot(max_output_tokens=8193,
        request_cost_bound_microusd=10**8))[0] == "near_tee_catalog_output_limit_unknown"
    assert evaluate_catalog(facts, slot_name="text", slot=_slot(request_cost_bound_microusd=7372))[0] \
        == "near_tee_catalog_cost_bound_too_small"
    text_only = _facts(architecture={"inputModalities": ["text"], "outputModalities": ["text"]})
    assert evaluate_catalog(text_only, slot_name="vision", slot=_slot())[0] == "near_tee_catalog_modality_missing"
    assert evaluate_catalog(_facts(contextLength=4096), slot_name="text", slot=_slot())[0] \
        == "near_tee_catalog_context_too_small"


@pytest.mark.parametrize("changes, reason", [
    ({"isReady": False}, "near_tee_catalog_not_ready"),
    ({"attestationSupported": _ABSENT}, "near_tee_catalog_not_attested"),
    ({"attestationSupported": False}, "near_tee_catalog_not_attested"),
    ({"verifiable": _ABSENT}, "near_tee_catalog_not_verifiable"),
    ({"verifiable": False}, "near_tee_catalog_not_verifiable"),
    ({"providerType": "chutes"}, "near_tee_catalog_provider_not_vllm"),
])
def test_deepseek_record_fails_closed_without_full_eligibility(changes, reason):
    facts = _facts(**changes)
    assert readiness_basis(facts) is None
    for slot_name in ("text", "vision"):
        assert evaluate_catalog(facts, slot_name=slot_name, slot=_slot())[0] == reason


def test_readiness_basis_is_recorded_in_the_receipt():
    from src.model_fabric.configuration import near_tee_profile_for_slot
    slot = _slot()
    profile = near_tee_profile_for_slot("text", slot)
    raw = json.dumps(deepseek_catalog_record()).encode()
    facts = parse_catalog_record(raw, model_id=DEEPSEEK)
    reason, required = evaluate_catalog(facts, slot_name="text", slot=slot)
    receipt = build_receipt(slot_name="text", slot=slot, profile=profile, facts=facts, raw=raw, reason=reason,
        required=required, revision=5, now=1_000.0)
    assert receipt["outcome"] == "passed" and receipt["is_ready"] is None
    assert receipt["readiness_basis"] == READINESS_ABSENT_FLAG_ATTESTED
    assert receipt["structured_outputs"] is False and receipt["tee_verified"] is False
    explicit = parse_catalog_record(json.dumps(deepseek_catalog_record(isReady=True)).encode(), model_id=DEEPSEEK)
    assert readiness_basis(explicit) == READINESS_CATALOG_READY
    refused = parse_catalog_record(json.dumps(deepseek_catalog_record(isReady=False)).encode(), model_id=DEEPSEEK)
    reason, required = evaluate_catalog(refused, slot_name="text", slot=slot)
    receipt = build_receipt(slot_name="text", slot=slot, profile=profile, facts=refused, raw=raw, reason=reason,
        required=required, revision=5, now=1_000.0)
    assert receipt["outcome"] == "failed" and receipt["reason_code"] == "near_tee_catalog_not_ready"
    assert receipt["is_ready"] is False and receipt["readiness_basis"] is None
    assert receipt["tee_hosted_per_catalog"] is False


def test_capability_check_budget_covers_reasoning():
    reasoning = {"text": {"features": ["reasoning", "tools"]}}
    assert check_output_tokens("text", 4096, receipts=reasoning) == REASONING_CHECK_OUTPUT_TOKENS == 1024
    assert check_output_tokens("text", 300, receipts=reasoning) == 300  # never above the slot bound
    assert check_output_tokens("text", 4096, receipts={"text": {"features": ["tools"]}}) == 64
    assert check_output_tokens("text", 4096, receipts={}) == 64


# ── Settings, capability checks and dispatch (intercepted) ─────────


async def _structured_call(operator, near, *, body_id, response_format=None):
    from src.approval.runtime import reset_runtime_context, set_runtime_context
    from src.llm_runtime import _near_tee_completion_sync
    from src.model_fabric.caller_context import build_canonical_inference_context

    near.body_id = body_id
    messages = [{"role": "user", "content": "Consolidate the fixture session as JSON."}]
    tokens = set_runtime_context(operator.session_id, "high_risk", trust_principal=operator.principal)
    try:
        context = build_canonical_inference_context("session_consolidation", payload=messages,
            output_tokens=2048, timeout_seconds=60, principal=operator.principal, session_id=operator.session_id)
        return await asyncio.to_thread(_near_tee_completion_sync, messages=messages, temperature=0.2,
            max_tokens=2048, request_context=context, request_id=None, runtime_path="session_consolidation",
            response_format=response_format or {"type": "json_object"})
    finally:
        reset_runtime_context(tokens)


def _chat_bodies(near):
    return [json.loads(request.content) for request in near.requests
        if request.url.raw_path.decode() == "/v1/chat/completions"]


async def _check(client, profile_id, capability, **extra):
    return await client.post("/api/settings/model-fabric/canary", json={"profile_id": profile_id,
        "capability": capability, "timeout_seconds": 40, "proof_ttl_seconds": 600, **extra})


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_deepseek_text_slot_structured_output_needs_a_live_proof(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch):
    from src.auth.service import authenticate_session
    from src.model_fabric.near_tee import NearTeeDenied, NearTeeError
    from src.workflows.job_runtime import durable_job_repository

    near = NearFixture().install(monkeypatch)
    near.catalog = deepseek_catalog_record()
    owner = await _login(client, monkeypatch)
    await _configure(client, model_id=DEEPSEEK, max_output_tokens=4096)
    checked = await client.post("/api/settings/model-fabric/near-tee/catalog-check",
        json={"expected_policy_revision": 5})
    assert checked.status_code == 200, checked.text
    receipt = checked.json()["catalog_check"]["text"]
    assert receipt["outcome"] == "passed" and receipt["reason_code"] is None
    assert receipt["is_ready"] is None and receipt["readiness_basis"] == READINESS_ABSENT_FLAG_ATTESTED
    assert receipt["structured_outputs"] is False and receipt["required_cost_microusd"] == 7373
    slot = checked.json()["near_tee"]["slots"]["text"]
    assert slot["catalog_receipt"]["readiness_basis"] == READINESS_ABSENT_FLAG_ATTESTED
    assert slot["catalog_receipt"]["is_ready"] is None
    assert slot["tee_hosting_label"].startswith("TEE-hosted per NEAR catalog as of ")
    assert checked.json()["near_tee"]["reasoning_recommended_max_output_tokens"] == 4096

    for capability in ("text", "health", "latency_ms"):
        response = await _check(client, "near_tee.text", capability)
        assert response.status_code == 200 and response.json()["outcome"] == "passed", response.text
    # A reasoning model's checks get a reasoning-sized budget (bounded by the slot).
    assert [body["max_tokens"] for body in _chat_bodies(near)] == [1024, 1024, 1024]
    status = (await client.get("/api/settings/model-fabric")).json()["near_tee"]["slots"]["text"]
    assert status["status"] == "ready", status
    assert status["structured_output_capability"] == "proof_missing:structured_output"
    assert status["structured_output_basis"] is None

    operator = await authenticate_session(owner["session_id"], touch=False)
    # No catalog feature and no proof: a structured caller is blocked before contact.
    before = len(near.requests)
    with pytest.raises(NearTeeDenied) as denied:
        await _structured_call(operator, near, body_id="chatcmpl-structured-denied")
    assert "structured_output" in denied.value.code
    assert len(near.requests) == before

    # A live, governed structured_output check establishes the capability.
    near.answer = {"role": "assistant", "content": '{"ok": true}', "reasoning_content": "PRIVATE-REASONING"}
    near.body_id = "chatcmpl-check-structured"
    proven = await _check(client, "near_tee.text", "structured_output")
    assert proven.status_code == 200 and proven.json()["outcome"] == "passed", proven.text
    check_body = _chat_bodies(near)[-1]
    assert check_body["response_format"] == {"type": "json_object"} and check_body["max_tokens"] == 1024
    status = (await client.get("/api/settings/model-fabric")).json()["near_tee"]["slots"]["text"]
    assert status["structured_output_capability"] == "ready"
    assert status["structured_output_basis"] == "capability_proof"

    near.answer = {"role": "assistant", "content": '{"summary": "consolidated"}',
        "reasoning_content": "PRIVATE-REASONING-MUST-NOT-LEAK"}
    before = len(near.requests)
    response = await _structured_call(operator, near, body_id="chatcmpl-structured-ok")
    assert json.loads(response.choices[0].message.content) == {"summary": "consolidated"}
    sent = near.requests[before:]
    assert [request.url.raw_path.decode() for request in sent] == ["/v1/chat/completions", "/v1/billing/costs"]
    assert all(request.headers["x-no-aliasing"] == "true" for request in sent)
    body = json.loads(sent[0].content)
    assert body["model"] == DEEPSEEK and body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == 2048

    # A JSON answer cut at the output bound is named, its cost settles and
    # nothing is adopted; the lane stays free.
    near.finish_reason = "length"
    near.answer = {"role": "assistant", "content": '{"summary": "cut', "reasoning_content": "PRIVATE"}
    with pytest.raises(NearTeeError, match="near_tee_output_truncated"):
        await _structured_call(operator, near, body_id="chatcmpl-structured-truncated")
    # Reasoning that used the whole bound (no answer content) is a truncation too.
    near.answer = {"role": "assistant", "content": "", "reasoning_content": "PRIVATE"}
    with pytest.raises(NearTeeError, match="near_tee_output_truncated"):
        await _structured_call(operator, near, body_id="chatcmpl-structured-reasoning-only")
    snapshot = await durable_job_repository.inference_accounting_snapshot()
    rows = [row for row in snapshot["operations"] if row["runtime_path"] == "session_consolidation"]
    assert len(rows) == 3 and {row["state"] for row in rows} == {"settled"}
    assert snapshot["unknown_microusd"] == 0
    # Model echo and x-no-aliasing are still enforced for DeepSeek.
    near.finish_reason = "stop"
    near.answer = {"role": "assistant", "content": '{"summary": "alias"}'}
    near.echo_model = "deepseek-ai/DeepSeek-V4-Flash"
    with pytest.raises(NearTeeError, match="near_tee_model_mismatch"):
        await _structured_call(operator, near, body_id="chatcmpl-structured-alias")


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_deepseek_vision_json_schema_is_established_only_by_a_current_proof(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch, tmp_path):
    from src.auth.service import authenticate_session
    from src.model_fabric.configuration import near_tee_profile_for_slot, read_model_fabric_configuration
    from src.model_fabric.near_tee_vision import latest_structured_output_proof

    near = VisionFixture().install(monkeypatch)
    near.catalogs[DEEPSEEK] = deepseek_catalog_record()
    owner = await _login(client, monkeypatch)
    await _lane(client)
    saved = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 3,
        "near_tee": {"api_key": PROVIDER_KEY, "slots": {"vision": vision_payload(model_id=DEEPSEEK,
            max_output_tokens=4096)}}})
    assert saved.status_code == 200, saved.text
    checked = await client.post("/api/settings/model-fabric/near-tee/catalog-check",
        json={"expected_policy_revision": 5})
    assert checked.status_code == 200, checked.text
    receipt = checked.json()["catalog_check"]["vision"]
    assert receipt["outcome"] == "passed" and receipt["readiness_basis"] == READINESS_ABSENT_FLAG_ATTESTED
    assert receipt["structured_outputs"] is False

    # structured_mode belongs to the vision structured_output check only.
    assert (await _check(client, "near_tee.vision", "vision", structured_mode="json_schema")).status_code == 422
    # No catalog feature and no proof: the default check runs prompt-JSON.
    for capability in ("vision", "structured_output", "health", "latency_ms"):
        response = await _check(client, "near_tee.vision", capability)
        assert response.status_code == 200 and response.json()["outcome"] == "passed", response.text
    assert "response_format" not in _chat_bodies(near)[1]
    assert [body["max_tokens"] for body in _chat_bodies(near)] == [1024, 1024, 1024, 1024]
    status = (await client.get("/api/settings/model-fabric")).json()["near_tee"]["slots"]["vision"]
    assert status["status"] == "ready" and status["structured_output_basis"] is None, status
    assert (await client.put("/api/settings/screen-analysis", json={"provider": "near_tee"})).status_code == 200
    operator = await authenticate_session(owner["session_id"], touch=False)
    shots = tmp_path / "shots"
    await _upload(shots, monkeypatch, capture_id="cap-deepseek-0001", color=(12, 40, 220))
    near.body_id = "chatcmpl-deepseek-prompt-json"
    assert (await _analyze(operator)).analyzed == 1
    assert "response_format" not in _chat_bodies(near)[-1]

    # A live json_schema check establishes json_schema despite the catalog.
    near.body_id = "chatcmpl-deepseek-check-schema"
    proven = await _check(client, "near_tee.vision", "structured_output", structured_mode="json_schema")
    assert proven.status_code == 200 and proven.json()["outcome"] == "passed", proven.text
    assert _chat_bodies(near)[-1]["response_format"]["json_schema"]["strict"] is True
    profile = near_tee_profile_for_slot("vision", read_model_fabric_configuration().near_tee.slots["vision"])
    assert (await latest_structured_output_proof(profile)).proven_value == "vision_json_schema"
    status = (await client.get("/api/settings/model-fabric")).json()["near_tee"]["slots"]["vision"]
    assert status["status"] == "ready" and status["structured_output_basis"] == "capability_proof", status
    await _upload(shots, monkeypatch, capture_id="cap-deepseek-0002", color=(220, 40, 12))
    near.body_id = "chatcmpl-deepseek-schema"
    assert (await _analyze(operator)).analyzed == 1
    body = _chat_bodies(near)[-1]
    assert body["model"] == DEEPSEEK and body["response_format"]["type"] == "json_schema"

    # A json_schema answer cut at the bound is a named truncation (cost settled).
    near.finish_reason = "length"
    near.body_id = "chatcmpl-deepseek-truncated"
    third = await _upload(shots, monkeypatch, capture_id="cap-deepseek-0003", color=(40, 220, 12))
    result = await _analyze(operator)
    assert (result.analyzed, result.failed) == (0, 1)
    assert (await _details(third))[2]["reason"] == "near_tee_output_truncated"

    # Once that proof is no longer current the capability is gone again (fail
    # closed to prompt-JSON): with only an expired json_schema proof the slot
    # needs a new check and the default check returns to prompt-JSON.
    from src.model_fabric.near_tee_vision import vision_structured_basis
    proof = await latest_structured_output_proof(profile)
    assert vision_structured_basis(catalog_lists=False, proof=proof) == "capability_proof"
    assert vision_structured_basis(catalog_lists=False, proof=proof, now=proof.expires_at) is None
    assert vision_structured_basis(catalog_lists=False, proof=None) is None
    assert vision_structured_basis(catalog_lists=True, proof=None) == "catalog"
    assert vision_structured_basis(catalog_lists=False, proof=replace(proof, outcome="failed")) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_prompt_json_check_is_refused_when_the_catalog_lists_structured_outputs(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch):
    near = VisionFixture().install(monkeypatch)
    near.catalogs[DEEPSEEK] = deepseek_catalog_record(supportedFeatures=["tools", "reasoning", "structured_outputs"])
    await _login(client, monkeypatch)
    await _lane(client)
    saved = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 3,
        "near_tee": {"api_key": PROVIDER_KEY, "slots": {"vision": vision_payload(model_id=DEEPSEEK)}}})
    assert saved.status_code == 200, saved.text
    assert (await client.post("/api/settings/model-fabric/near-tee/catalog-check",
        json={"expected_policy_revision": 5})).status_code == 200
    before = len(near.requests)
    refused = await _check(client, "near_tee.vision", "structured_output", structured_mode="prompt_json")
    assert refused.status_code == 409 and refused.json()["detail"] == "near_tee_vision_structured_mode_conflict"
    assert len(near.requests) == before
    status = (await client.get("/api/settings/model-fabric")).json()["near_tee"]["slots"]["vision"]
    assert status["structured_output_basis"] == "catalog"
