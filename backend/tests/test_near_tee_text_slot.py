"""ADR-035 NEAR TEE text slot (PR3b): every NEAR request is intercepted here.

No live, paid or catalog network call: the httpx transport is replaced with a
MockTransport that only answers the pinned NEAR host and fails the test on any
other destination (OpenRouter included).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import time
from uuid import NAMESPACE_DNS, uuid5

import httpx
import pytest

from config.settings import settings
from src.model_fabric.configuration import (
    NEAR_TEE_TEXT_ALLOWLIST,
    NearTeeSetup,
    NearTeeSlot,
    effective_workload_policy,
    near_tee_profile_for_slot,
    read_model_fabric_configuration,
    validate_near_tee_setup,
    write_model_fabric_configuration,
)
from src.model_fabric.contracts import provider_uncertain_on_error
from src.model_fabric.near_tee import NearTeeError, assert_no_aliasing, near_tee_answer
from src.model_fabric.near_tee_catalog import (
    RECEIPT_STALE,
    catalog_receipt_block_reason,
    evaluate_catalog,
    parse_catalog_record,
    read_catalog_receipts,
    write_catalog_receipts,
)
from src.vault.repository import vault_repository
from tests.test_openrouter_setup_api import keyless_openrouter, model_fabric_workspace  # noqa: F401


TEXT_MODEL = "Qwen/Qwen3.8-27B"
PROVIDER_KEY = "private-intercepted-near-tee-key"
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def slot_payload(**changes):
    body = {"model_id": TEXT_MODEL, "enabled": True, "max_input_tokens": 8192, "max_output_tokens": 1024,
        "timeout_seconds": 20, "request_cost_bound_microusd": 20000, "catalog_ttl_hours": 24,
        "plaintext_provider_egress_acknowledged": True}
    body.update(changes)
    return body


def catalog_record(model=TEXT_MODEL, **metadata_changes):
    metadata = {"providerType": "vllm", "attestationSupported": True, "verifiable": True, "isReady": True,
        "contextLength": 262144, "maxOutputTokens": 65536, "inputModalities": ["text", "image"],
        "outputModalities": ["text"], "supportedFeatures": ["tools", "structured_outputs", "reasoning"]}
    metadata.update(metadata_changes)
    metadata = {key: value for key, value in metadata.items() if value is not None}
    return {"modelId": model, "inputCostPerToken": {"amount": 4, "scale": 8, "currency": "USD"},
        "outputCostPerToken": {"amount": 135, "scale": 8, "currency": "USD"}, "metadata": metadata}


class _Stream(httpx.AsyncByteStream):
    def __init__(self, value):
        self.data = json.dumps(value, separators=(",", ":")).encode()

    async def __aiter__(self):
        yield self.data


def _json(value, **kwargs):
    return httpx.Response(200, stream=_Stream(value), **kwargs)


class NearFixture:
    """Intercepted NEAR gateway; records every request and refuses other hosts."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.catalog = catalog_record()
        self.answer: dict = {"role": "assistant", "content": "Report body from the NEAR TEE fixture."}
        self.echo_model: str | None = None
        self.billing_missing = False
        self.cost_nano = 1_500_000
        self.body_id = "chatcmpl-fixture"

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "cloud-api.near.ai", f"unexpected provider host {request.url.host}"
        assert request.url.scheme == "https"
        path = request.url.raw_path.decode()
        if request.method == "GET" and path.startswith("/v1/model/"):
            assert "authorization" not in request.headers
            return _json(self.catalog)
        body = json.loads(request.content)
        if path == "/v1/chat/completions":
            model = self.echo_model or body["model"]
            return _json({"id": self.body_id, "model": model, "choices": [{"index": 0,
                "finish_reason": "stop", "message": dict(self.answer)}]})
        if path == "/v1/billing/costs":
            if self.billing_missing:
                return httpx.Response(503, stream=_Stream({"error": "unavailable"}))
            return _json({"requests": [{"requestId": str(uuid5(NAMESPACE_DNS, self.body_id)),
                "costNanoUsd": self.cost_nano}]})
        raise AssertionError(f"unexpected NEAR path {path}")

    def install(self, monkeypatch):
        def clients(**kwargs):
            if "transport" not in kwargs:
                kwargs["transport"] = httpx.MockTransport(self.handler)
            return _REAL_ASYNC_CLIENT(**kwargs)
        monkeypatch.setattr(httpx, "AsyncClient", clients)

        def forbidden(*_args, **_kwargs):
            raise AssertionError("synchronous provider transport contacted")
        monkeypatch.setattr(httpx.Client, "post", forbidden)
        monkeypatch.setattr(httpx.Client, "stream", forbidden)
        return self


@pytest.fixture(autouse=True)
def isolated_broker(monkeypatch):
    """A fresh process-local broker per test: a blocked Unknown lease left by a
    negative test must never leak into other tests sharing the process."""
    from src import llm_runtime
    from src.api import model_fabric_settings as api
    from src.model_fabric import execution, probe, remote_inference_admission
    broker = remote_inference_admission.RemoteInferenceAdmissionBroker(durable_accounting=True)
    monkeypatch.setattr(remote_inference_admission, "remote_inference_admission_broker", broker)
    monkeypatch.setattr(llm_runtime, "gpu_admission_broker", broker)
    monkeypatch.setattr(execution, "gpu_admission_broker", broker)
    monkeypatch.setattr(probe, "remote_inference_admission_broker", broker)
    monkeypatch.setattr(api, "remote_inference_admission_broker", broker)
    return broker


# ── Pure contract checks ─────────────────────────────────────────


def _slot(**changes):
    values = dict(model_id=TEXT_MODEL, max_input_tokens=8192, max_output_tokens=1024,
        request_cost_bound_microusd=20000, enabled=False)
    values.update(changes)
    return NearTeeSlot(**values)


def test_setup_is_closed_disabled_by_default_and_allowlisted():
    validate_near_tee_setup(NearTeeSetup())
    assert NearTeeSetup().slots is None
    for model in NEAR_TEE_TEXT_ALLOWLIST:
        validate_near_tee_setup(NearTeeSetup(slots={"text": _slot(model_id=model)}))
    for model in ("anthropic/claude-sonnet-4", "openai/gpt-4o", "z-ai/glm-5.3", "Qwen/Qwen3-VL-30B-A3B-Instruct",
            "openrouter/z-ai/glm-5.3-flash", "z-ai/GLM-5.3-flash"):
        with pytest.raises(ValueError, match="near_tee_model_not_allowlisted"):
            validate_near_tee_setup(NearTeeSetup(slots={"text": _slot(model_id=model)}))
    with pytest.raises(ValueError, match="near_tee_slot_not_implemented"):
        validate_near_tee_setup(NearTeeSetup(slots={"vision": _slot()}))
    with pytest.raises(ValueError):
        validate_near_tee_setup(NearTeeSetup(slots={"text": _slot(max_input_tokens=262_145)}))
    with pytest.raises(ValueError):
        validate_near_tee_setup(NearTeeSetup(slots={"text": _slot(purpose_consent_revision=3)}))
    with pytest.raises(ValueError):
        validate_near_tee_setup(replace(NearTeeSetup(), api_base="https://openrouter.ai/api/v1"))


def test_profile_is_pinned_and_has_no_env_credential(monkeypatch):
    monkeypatch.setenv("NEAR_API_KEY", "env-must-not-be-used")
    profile = near_tee_profile_for_slot("text", _slot(enabled=True))
    assert profile.provider_kind == "near_tee" and profile.id == "near_tee.text"
    assert profile.api_base == "https://cloud-api.near.ai/v1" and profile.model == TEXT_MODEL
    assert profile.api_key == ""
    assert not profile.fallback_models and not profile.follow_redirects
    from src.model_fabric.selector import active_provider_exclusion_reason
    assert active_provider_exclusion_reason(profile) is None
    assert active_provider_exclusion_reason(replace(profile, model="z-ai/glm-5.3")) == "near_tee_model_not_allowlisted"
    assert active_provider_exclusion_reason(replace(profile, api_base="https://evil.example/v1")) == "near_tee_endpoint_not_canonical"
    assert active_provider_exclusion_reason(replace(profile, id="near_tee.other")) == "near_tee_profile_not_slot"


def test_uncertain_on_error_covers_near_tee_and_not_local():
    assert provider_uncertain_on_error("near_tee") is True
    assert provider_uncertain_on_error("openrouter") is True
    assert provider_uncertain_on_error("local") is False


def test_missing_no_aliasing_header_is_rejected_before_contact():
    with pytest.raises(NearTeeError, match="near_tee_no_aliasing_required"):
        assert_no_aliasing({"Authorization": "Bearer x"})
    with pytest.raises(NearTeeError):
        assert_no_aliasing({"x-no-aliasing": "false"})
    assert_no_aliasing({"x-no-aliasing": "true"})


def test_answer_never_uses_reasoning_and_checks_echo():
    payload = {"model": TEXT_MODEL, "choices": [{"finish_reason": "stop", "message": {"role": "assistant",
        "content": "answer", "reasoning_content": "secret chain of thought"}}]}
    assert near_tee_answer(payload, TEXT_MODEL) == ("answer", None)
    reasoning_only = json.loads(json.dumps(payload))
    reasoning_only["choices"][0]["message"]["content"] = ""
    assert near_tee_answer(reasoning_only, TEXT_MODEL) == (None, "near_tee_empty_output")
    reasoning_only["choices"][0]["message"]["content"] = None
    assert near_tee_answer(reasoning_only, TEXT_MODEL) == (None, "near_tee_empty_output")
    assert near_tee_answer({**payload, "model": "Qwen/Qwen3-30B-A3B-Instruct-2507"}, TEXT_MODEL) == (None, "near_tee_model_mismatch")
    tools = json.loads(json.dumps(payload))
    tools["choices"][0]["message"]["tool_calls"] = [{"id": "x"}]
    assert near_tee_answer(tools, TEXT_MODEL) == (None, "near_tee_tools_unavailable")


@pytest.mark.parametrize("changes, reason", [
    ({"providerType": "chutes"}, "near_tee_catalog_provider_not_vllm"),
    ({"attestationSupported": False}, "near_tee_catalog_not_attested"),
    ({"verifiable": False}, "near_tee_catalog_not_verifiable"),
    ({"isReady": None}, "near_tee_catalog_not_ready"),
    ({"isReady": False}, "near_tee_catalog_not_ready"),
    ({"inputModalities": ["image"]}, "near_tee_catalog_modality_missing"),
    ({"contextLength": 4096}, "near_tee_catalog_context_too_small"),
    ({"maxOutputTokens": 512}, "near_tee_catalog_output_too_small"),
])
def test_catalog_failures(changes, reason):
    facts = parse_catalog_record(json.dumps(catalog_record(**changes)).encode(), model_id=TEXT_MODEL)
    assert evaluate_catalog(facts, slot_name="text", slot=_slot(enabled=True))[0] == reason


def test_catalog_cost_bound_and_shape():
    facts = parse_catalog_record(json.dumps(catalog_record()).encode(), model_id=TEXT_MODEL)
    # 8192 x 0.04/M + 1024 x 1.35/M USD = 327.68 + 1382.4 microUSD -> 1711 (ceil)
    assert evaluate_catalog(facts, slot_name="text", slot=_slot(enabled=True)) == (None, 1711)
    assert evaluate_catalog(facts, slot_name="text", slot=_slot(enabled=True, request_cost_bound_microusd=1710)) == (
        "near_tee_catalog_cost_bound_too_small", 1711)
    no_price = catalog_record()
    del no_price["outputCostPerToken"]
    facts = parse_catalog_record(json.dumps(no_price).encode(), model_id=TEXT_MODEL)
    assert evaluate_catalog(facts, slot_name="text", slot=_slot(enabled=True))[0] == "near_tee_catalog_price_missing"
    from src.model_fabric.near_tee_catalog import NearTeeCatalogError
    with pytest.raises(NearTeeCatalogError, match="near_tee_catalog_model_mismatch"):
        parse_catalog_record(json.dumps(catalog_record(model="z-ai/glm-5.3-flash")).encode(), model_id=TEXT_MODEL)
    with pytest.raises(NearTeeCatalogError, match="near_tee_catalog_response_invalid"):
        parse_catalog_record(b'{"modelId":"a","modelId":"b"}', model_id=TEXT_MODEL)


# ── Settings, catalog check, dispatch: real vault/accounting/witness ─────


async def _login(client, monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "fixture-auth-password")
    client.headers["origin"] = "http://localhost:3001"
    login = await client.post("/api/auth/login", json={"password": "fixture-auth-password"})
    assert login.status_code == 200, login.text
    return login.json()


async def _configure(client, **slot_changes):
    lane = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1,
        "inference_lane": {"spend_ceiling_microusd": 1_000_000}})
    assert lane.status_code == 200, lane.text
    saved = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 3,
        "near_tee": {"api_key": PROVIDER_KEY, "slots": {"text": slot_payload(**slot_changes)}}})
    assert saved.status_code == 200, saved.text
    return saved.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_settings_save_is_witnessed_cas_and_local(client, model_fabric_workspace, keyless_openrouter, monkeypatch):
    near = NearFixture().install(monkeypatch)
    early = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1,
        "near_tee": {"slots": {"text": slot_payload()}}})
    assert early.status_code == 409 and early.json()["detail"] == "inference_lane_required"
    assert (await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1,
        "inference_lane": {"spend_ceiling_microusd": 1_000_000}})).status_code == 200
    unlisted = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 3,
        "near_tee": {"slots": {"text": slot_payload(model_id="anthropic/claude-sonnet-4")}}})
    assert unlisted.status_code == 422 and unlisted.json()["detail"] == "near_tee_model_not_allowlisted"
    unacknowledged = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 3,
        "near_tee": {"api_key": PROVIDER_KEY, "slots": {"text": slot_payload(plaintext_provider_egress_acknowledged=False)}}})
    assert unacknowledged.status_code == 403
    assert await vault_repository.get("near_api_key") is None
    stale = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 2,
        "near_tee": {"slots": {"text": slot_payload()}}})
    assert stale.status_code == 409
    saved = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 3,
        "near_tee": {"api_key": PROVIDER_KEY, "slots": {"text": slot_payload()}}})
    assert saved.status_code == 200, saved.text
    assert PROVIDER_KEY not in saved.text
    body = saved.json()
    tee = body["near_tee"]
    assert body["egress_revision"] == 5
    assert tee["key_present"] is True and tee["tee_verified"] is False and tee["e2ee"] is False
    assert "not verified by Seraph" in tee["disclosure"] and "plaintext" in tee["disclosure"]
    assert tee["slots"]["text"]["consent_current"] is True
    assert tee["slots"]["text"]["status"] == "configuration_required"
    assert tee["slots"]["text"]["reason_code"] == "near_tee_catalog_receipt_missing"
    assert await vault_repository.get("near_api_key") == PROVIDER_KEY
    assert await vault_repository.get("near_text_api_key") is None
    # GET and PUT stay local: no NEAR contact at all.
    assert (await client.get("/api/settings/model-fabric")).status_code == 200
    assert near.requests == []
    # Unchanged re-save carries consent without a new acknowledgement and keeps the key.
    resaved = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 5,
        "near_tee": {"slots": {"text": slot_payload(plaintext_provider_egress_acknowledged=False)}}})
    assert resaved.status_code == 200, resaved.text
    assert resaved.json()["near_tee"]["slots"]["text"]["consent_current"] is True
    # A changed slot needs a fresh acknowledgement.
    changed = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 7,
        "near_tee": {"slots": {"text": slot_payload(max_output_tokens=900, plaintext_provider_egress_acknowledged=False)}}})
    assert changed.status_code == 403
    # Global revoke blocks NEAR TEE and no save resurrects the revoked consent.
    current = read_model_fabric_configuration()
    write_model_fabric_configuration(replace(current, egress_revision=8, egress_revoked=True), expected_revision=7)
    assert effective_workload_policy("end_of_day_goal_report").allowed_provider_kinds == ()
    resurrect = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 8,
        "near_tee": {"slots": {"text": slot_payload(plaintext_provider_egress_acknowledged=False)}}})
    assert resurrect.status_code == 403
    assert near.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_catalog_check_receipts_gate_dispatch(client, model_fabric_workspace, keyless_openrouter, monkeypatch):
    near = NearFixture().install(monkeypatch)
    await _login(client, monkeypatch)
    await _configure(client)
    policy = effective_workload_policy("end_of_day_goal_report")
    assert policy.allowed_provider_kinds == ("near_tee",) and policy.allowed_profile_ids == ("near_tee.text",)
    stale_revision = await client.post("/api/settings/model-fabric/near-tee/catalog-check",
        json={"expected_policy_revision": 4})
    assert stale_revision.status_code == 409 and near.requests == []
    near.catalog = catalog_record(providerType="chutes")
    failed = await client.post("/api/settings/model-fabric/near-tee/catalog-check", json={"expected_policy_revision": 5})
    assert failed.status_code == 200, failed.text
    assert failed.json()["catalog_check"]["text"]["reason_code"] == "near_tee_catalog_provider_not_vllm"
    assert failed.json()["near_tee"]["slots"]["text"]["reason_code"] == "near_tee_catalog_provider_not_vllm"
    near.catalog = catalog_record()
    passed = await client.post("/api/settings/model-fabric/near-tee/catalog-check", json={"expected_policy_revision": 5})
    assert passed.status_code == 200, passed.text
    receipt = passed.json()["catalog_check"]["text"]
    assert receipt["outcome"] == "passed" and receipt["required_cost_microusd"] == 1711
    assert receipt["tee_verified"] is False and receipt["e2ee"] is False and len(receipt["response_sha256"]) == 64
    slot_status = passed.json()["near_tee"]["slots"]["text"]
    assert slot_status["reason_code"] == "proof_missing:health"
    assert slot_status["tee_hosting_label"].startswith("TEE-hosted per NEAR catalog as of ")
    catalog_requests = [request for request in near.requests if request.method == "GET"]
    assert len(catalog_requests) == 2 and all(request.headers["x-no-aliasing"] == "true" for request in catalog_requests)
    assert catalog_requests[0].url.raw_path.decode() == "/v1/model/Qwen%2FQwen3.8-27B"
    assert all(request.method == "GET" for request in near.requests)  # no inference
    profile = near_tee_profile_for_slot("text", read_model_fabric_configuration().near_tee.slots["text"])
    assert catalog_receipt_block_reason("text", profile) is None
    stored = read_catalog_receipts()
    assert catalog_receipt_block_reason("text", profile, now=stored["text"]["expires_at"] + 1) == RECEIPT_STALE
    assert catalog_receipt_block_reason("text", replace(profile, model="z-ai/glm-5.3-flash")) == RECEIPT_STALE
    # A changed slot profile invalidates the receipt (bound to the profile hash).
    saved = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 5,
        "near_tee": {"slots": {"text": slot_payload(max_output_tokens=900)}}})
    assert saved.status_code == 200
    assert saved.json()["near_tee"]["slots"]["text"]["reason_code"] == RECEIPT_STALE


async def _prove_slot(client, near):
    for capability in ("text", "health", "latency_ms"):
        response = await client.post("/api/settings/model-fabric/canary", json={"profile_id": "near_tee.text",
            "capability": capability, "timeout_seconds": 40, "proof_ttl_seconds": 600})
        assert response.status_code == 200, response.text
        assert response.json()["outcome"] == "passed", response.text
    unsupported = await client.post("/api/settings/model-fabric/canary", json={"profile_id": "near_tee.text",
        "capability": "streaming", "timeout_seconds": 40, "proof_ttl_seconds": 600})
    assert unsupported.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_report_route_reaches_near_transport_with_billing_and_failures(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch, isolated_broker):
    from src.approval.runtime import reset_runtime_context, set_runtime_context
    from src.auth.service import authenticate_session
    from src.llm_runtime import completion_with_fallback, stream_completion_with_fallback
    from src.model_fabric.caller_context import build_canonical_inference_context
    from src.model_fabric.contracts import NoCompliantModelRouteError
    from src.workflows.job_runtime import durable_job_repository

    remote_inference_admission_broker = isolated_broker
    near = NearFixture().install(monkeypatch)
    owner = await _login(client, monkeypatch)
    await _configure(client)
    assert (await client.post("/api/settings/model-fabric/near-tee/catalog-check",
        json={"expected_policy_revision": 5})).status_code == 200
    await _prove_slot(client, near)
    ledger = await durable_job_repository.inference_accounting_snapshot()
    assert ledger["committed_microusd"] == 3 * 1500 and ledger["unknown_microusd"] == 0
    assert {row["profile_id"] for row in ledger["operations"]} == {"near_tee.text"}
    status = (await client.get("/api/settings/model-fabric")).json()
    assert status["near_tee"]["slots"]["text"]["status"] == "ready", status["near_tee"]
    assert status["active_inference_provider"] == "near_tee"
    operator = await authenticate_session(owner["session_id"], touch=False)
    tokens = set_runtime_context(operator.session_id, "high_risk", trust_principal=operator.principal)
    messages = [{"role": "user", "content": "Summarize today's fixture activity."}]

    def report_context():
        return build_canonical_inference_context("end_of_day_goal_report", payload=messages,
            output_tokens=900, timeout_seconds=60, principal=operator.principal, session_id=operator.session_id)

    async def report():
        return await completion_with_fallback(messages=messages, temperature=0.4, max_tokens=900,
            runtime_path="end_of_day_goal_report", request_context=report_context())

    try:
        # Screen-derived decision is provider-generic and resolves the NEAR TEE route.
        monkeypatch.setattr(settings, "end_of_day_report_llm_enabled", True)
        from src.scheduler import background_inference
        monkeypatch.setattr(background_inference, "background_inference_job_enabled", lambda _path: True)
        from src.scheduler.screen_llm_policy import screen_derived_llm_decision
        decision = await screen_derived_llm_decision("end_of_day_goal_report")
        assert decision.allowed, decision.reason
        assert decision.runtime_profile == "near_tee.text"
        from src.scheduler.jobs.end_of_day_goal_report import _report_llm_label
        label = _report_llm_label(decision.runtime_profile)
        assert label.startswith(f"near_tee.text:{TEXT_MODEL} (TEE-hosted per NEAR catalog as of ")
        assert "attestation not verified by Seraph" in label and "no end-to-end encryption" in label

        before = len(near.requests)
        near.answer = {"role": "assistant", "content": "Report body from the NEAR TEE fixture.",
            "reasoning_content": "PRIVATE-REASONING-MUST-NOT-LEAK"}
        response = await report()
        assert response.choices[0].message.content == "Report body from the NEAR TEE fixture."
        assert "PRIVATE-REASONING" not in json.dumps(str(response.choices[0].message))
        sent = near.requests[before:]
        assert [request.url.raw_path.decode() for request in sent] == ["/v1/chat/completions", "/v1/billing/costs"]
        assert all(request.headers["x-no-aliasing"] == "true" for request in sent)
        assert all(request.headers["authorization"] == f"Bearer {PROVIDER_KEY}" for request in sent)
        assert all(request.headers["accept-encoding"] == "identity" for request in sent)
        inference_body = json.loads(sent[0].content)
        assert inference_body["model"] == TEXT_MODEL and inference_body["stream"] is False
        assert inference_body["temperature"] == 0.4 and inference_body["max_tokens"] == 900
        snapshot = await durable_job_repository.inference_accounting_snapshot()
        assert snapshot["committed_microusd"] == 4 * 1500 and snapshot["unknown_microusd"] == 0
        row = [item for item in snapshot["operations"] if item["runtime_path"] == "end_of_day_goal_report"]
        assert len(row) == 1 and row[0]["state"] == "settled" and row[0]["profile_id"] == "near_tee.text"
        assert row[0]["actual_cost_microusd"] == 1500

        # Model echo mismatch: contact happened, cost settles, no output adopted, lane free.
        near.echo_model = "Qwen/Qwen3-30B-A3B-Instruct-2507"
        near.body_id = "chatcmpl-mismatch"
        with pytest.raises(NearTeeError, match="near_tee_model_mismatch"):
            await report()
        near.echo_model = None
        snapshot = await durable_job_repository.inference_accounting_snapshot()
        assert snapshot["committed_microusd"] == 5 * 1500 and snapshot["unknown_microusd"] == 0

        # Reasoning with empty answer content is an empty output, never answer text.
        near.answer = {"role": "assistant", "content": "", "reasoning_content": "PRIVATE-REASONING-MUST-NOT-LEAK"}
        near.body_id = "chatcmpl-empty"
        with pytest.raises(NearTeeError, match="near_tee_empty_output") as raised:
            await report()
        assert "PRIVATE-REASONING" not in str(raised.value)
        near.answer = {"role": "assistant", "content": "ok"}

        # Streaming is cleanly blocked in PR3b, before any contact.
        before = len(near.requests)
        stream_context = build_canonical_inference_context("chat_agent", payload=messages, output_tokens=64,
            timeout_seconds=60, principal=operator.principal, session_id=operator.session_id, streaming=True)
        with pytest.raises(NoCompliantModelRouteError) as streamed:
            async for _delta in stream_completion_with_fallback(messages, 0.2, 64, "chat_agent", stream_context):
                pass
        assert streamed.value.reason_codes == ("near_tee_streaming_unavailable",)
        assert len(near.requests) == before

        # Missing billing becomes an Unknown liability and the serial lease stays blocked.
        near.billing_missing = True
        near.body_id = "chatcmpl-unknown"
        from src.model_fabric.gpu_admission import GpuAdmissionUncertainError
        with pytest.raises(GpuAdmissionUncertainError) as uncertain:
            await report()
        assert isinstance(uncertain.value.__cause__, NearTeeError)
        assert uncertain.value.__cause__.code == "near_tee_cost_liability"
        snapshot = await durable_job_repository.inference_accounting_snapshot()
        unknown = [item for item in snapshot["operations"] if item["state"] == "unknown"]
        assert len(unknown) == 1 and unknown[0]["profile_id"] == "near_tee.text"
        assert snapshot["unknown_microusd"] > 0
        receipt = remote_inference_admission_broker.receipt_for(unknown[0]["operation_id"])
        assert receipt.status == "blocked" and receipt.reconciliation_required
        near.billing_missing = False
        # The serial lease stays held by the Unknown operation until fenced settlement.
        assert receipt.active_operation_id == unknown[0]["operation_id"]
        # Zero OpenRouter contact on every route: the fixture refuses any other host.
        assert {request.url.host for request in near.requests} == {"cloud-api.near.ai"}
    finally:
        reset_runtime_context(tokens)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_stale_receipt_blocks_before_admission(client, model_fabric_workspace, keyless_openrouter, monkeypatch):
    from src.approval.runtime import reset_runtime_context, set_runtime_context
    from src.auth.service import authenticate_session
    from src.llm_runtime import completion_with_fallback
    from src.model_fabric.caller_context import build_canonical_inference_context
    from src.model_fabric.contracts import NoCompliantModelRouteError
    from src.workflows.job_runtime import durable_job_repository

    near = NearFixture().install(monkeypatch)
    owner = await _login(client, monkeypatch)
    await _configure(client)
    assert (await client.post("/api/settings/model-fabric/near-tee/catalog-check",
        json={"expected_policy_revision": 5})).status_code == 200
    receipts = read_catalog_receipts()
    receipts["text"]["checked_at"] -= 25 * 3600
    receipts["text"]["expires_at"] = time.time() - 1
    write_catalog_receipts(receipts)
    before = len(near.requests)
    operator = await authenticate_session(owner["session_id"], touch=False)
    tokens = set_runtime_context(operator.session_id, "high_risk", trust_principal=operator.principal)
    try:
        messages = [{"role": "user", "content": "fixture"}]
        context = build_canonical_inference_context("daily_briefing", payload=messages, output_tokens=200,
            timeout_seconds=60, principal=operator.principal, session_id=operator.session_id)
        with pytest.raises(NoCompliantModelRouteError) as denied:
            await completion_with_fallback(messages=messages, temperature=0.2, max_tokens=200,
                runtime_path="daily_briefing", request_context=context)
        assert denied.value.reason_codes == (RECEIPT_STALE,)
    finally:
        reset_runtime_context(tokens)
    assert len(near.requests) == before
    assert (await durable_job_repository.inference_accounting_snapshot())["operations"] == []


def test_priority_and_serial_bound_preserved_for_near_tee_contexts():
    from src.model_fabric.gpu_admission import GpuPriority, priority_for_inference_context
    from src.model_fabric.configuration import InferenceLaneConfig, validate_inference_lane
    from types import SimpleNamespace
    chat = SimpleNamespace(runtime_path="chat_agent", workload="interactive")
    report = SimpleNamespace(runtime_path="end_of_day_goal_report", workload="report")
    assert priority_for_inference_context(chat) is GpuPriority.INTERACTIVE_CHAT
    assert priority_for_inference_context(report) is GpuPriority.REPORTS_RESEARCH_MEMORY
    assert GpuPriority.INTERACTIVE_CHAT.rank > GpuPriority.REPORTS_RESEARCH_MEMORY.rank  # chat first
    with pytest.raises(ValueError):
        validate_inference_lane(InferenceLaneConfig(spend_ceiling_microusd=1000, max_inflight=2))
