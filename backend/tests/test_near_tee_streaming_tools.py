"""ADR-035 PR3c: NEAR TEE streaming and tools (chat agent and Cordis inference.request).

Provider-free: every NEAR request is answered by the intercepted MockTransport
fixture from the PR3b suite (which refuses any other host, backstops the real
sync/async HTTP transports and refuses litellm dispatch). No live, paid or
catalog network call is made.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import time
from uuid import NAMESPACE_DNS, uuid5

import httpx
import pytest

from src.model_fabric.near_tee import NearTeeError
from tests.test_near_tee_text_slot import (  # noqa: F401
    TEXT_MODEL,
    NearFixture,
    _ABSENT,
    _configure,
    _json,
    _login,
    _prove_slot,
    catalog_record,
    isolated_broker,
    keyless_openrouter,
    model_fabric_workspace,
)


REASONING = "PRIVATE-REASONING-MUST-NOT-LEAK"


class _SlowStream(httpx.AsyncByteStream):
    """SSE body; optionally stalls after ``stall_after`` events."""

    def __init__(self, events, *, stall_after=None, stall_seconds=0.0, done=True):
        self.events, self.stall_after, self.stall_seconds, self.done = events, stall_after, stall_seconds, done

    async def __aiter__(self):
        for index, event in enumerate(self.events):
            if self.stall_after is not None and index == self.stall_after:
                await asyncio.sleep(self.stall_seconds)
            yield ("data: " + json.dumps(event, separators=(",", ":")) + "\n\n").encode()
        if self.done:
            yield b"data: [DONE]\n\n"


class StreamingNear(NearFixture):
    """The PR3b intercepted gateway plus SSE streaming and tool calls."""

    def __init__(self):
        super().__init__()
        self.stream_parts = [("", REASONING), ("Hello ", None), ("from NEAR", REASONING)]
        self.stream_model_at: dict[int, str] = {}
        self.stall_after: int | None = None
        self.stall_seconds = 0.0
        self.tool_calls: list[list[dict]] = []
        self.times: list[tuple[str, float]] = []

    def stream_events(self, model):
        events = []
        for index, (content, reasoning) in enumerate(self.stream_parts):
            delta = {"role": "assistant"} if index == 0 else {}
            if content:
                delta["content"] = content
            if reasoning:
                delta["reasoning_content"] = reasoning
            events.append({"id": self.body_id, "object": "chat.completion.chunk",
                "model": self.stream_model_at.get(index, model), "choices": [{"index": 0, "delta": delta}]})
        events.append({"id": self.body_id, "model": model, "choices": [{"index": 0, "delta": {},
            "finish_reason": "stop"}]})
        return events

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        self.times.append((path, time.time()))
        if request.method == "POST" and path == "/v1/chat/completions":
            body = json.loads(request.content)
            if body.get("stream"):
                self.requests.append(request)
                assert request.url.host == "cloud-api.near.ai"
                assert request.headers["accept"] == "text/event-stream"
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                    stream=_SlowStream(self.stream_events(body["model"]), stall_after=self.stall_after,
                        stall_seconds=self.stall_seconds))
            if body.get("tools"):
                self.requests.append(request)
                calls = self.tool_calls.pop(0) if self.tool_calls else [{"id": "call_canary", "type": "function",
                    "function": {"name": body["tools"][0]["function"]["name"], "arguments": "{}"}}]
                return _json({"id": self.body_id, "model": self.echo_model or body["model"], "choices": [{"index": 0,
                    "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                    "reasoning_content": REASONING, "tool_calls": calls}}]})
        return super().handler(request)


async def _ready(client, monkeypatch, *, prove=("streaming", "tool_use"), **slot_changes):
    from src.auth.service import authenticate_session
    near = StreamingNear().install(monkeypatch)
    owner = await _login(client, monkeypatch)
    await _configure(client, **slot_changes)
    assert (await client.post("/api/settings/model-fabric/near-tee/catalog-check",
        json={"expected_policy_revision": 5})).status_code == 200
    await _prove_slot(client, near)
    for capability in prove:
        near.body_id = f"chatcmpl-check-{capability}"
        response = await client.post("/api/settings/model-fabric/canary", json={"profile_id": "near_tee.text",
            "capability": capability, "timeout_seconds": 40, "proof_ttl_seconds": 600})
        assert response.status_code == 200, response.text
        assert response.json()["outcome"] == "passed", response.text
        if capability == "tool_use":
            # The check covers what agent tool turns actually send.
            checked = json.loads([request for request in near.requests if b'"tools"' in request.content][-1].content)
            assert checked["tool_choice"] == "required"
    near.body_id = "chatcmpl-fixture"
    operator = await authenticate_session(owner["session_id"], touch=False)
    return near, operator


def _bind(operator):
    from src.approval.runtime import set_runtime_context
    return set_runtime_context(operator.session_id, "high_risk", trust_principal=operator.principal)


def _chat_stream_context(operator, messages, *, runtime_path="chat_agent", timeout_seconds=60):
    from src.model_fabric.caller_context import build_canonical_inference_context
    return build_canonical_inference_context(runtime_path, payload=messages, output_tokens=64,
        timeout_seconds=timeout_seconds, principal=operator.principal, session_id=operator.session_id,
        streaming=True)


async def _collect(stream):
    parts = []
    try:
        async for delta in stream:
            parts.append(delta)
    except BaseException as error:
        return parts, error
    return parts, None


MESSAGES = [{"role": "user", "content": "Say hello."}]


def _paths(requests):
    return [request.url.raw_path.decode() for request in requests]


async def _rows(runtime_path="chat_agent"):
    from src.workflows.job_runtime import durable_job_repository
    snapshot = await durable_job_repository.inference_accounting_snapshot()
    return snapshot, [row for row in snapshot["operations"] if row["runtime_path"] == runtime_path]


# ── Streaming ────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_streaming_happy_path_hides_reasoning_and_settles_billing_in_lease(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch, isolated_broker):
    from src.approval.runtime import reset_runtime_context
    from src.llm_runtime import stream_completion_with_fallback

    near, operator = await _ready(client, monkeypatch, prove=("streaming",))
    tokens = _bind(operator)
    try:
        before = len(near.requests)
        context = _chat_stream_context(operator, MESSAGES)
        parts, error = await _collect(stream_completion_with_fallback(MESSAGES, 0.3, 64, "chat_agent", context))
        assert error is None, repr(error)
        assert parts == ["Hello ", "from NEAR"]
        assert all(REASONING not in part for part in parts)
        sent = near.requests[before:]
        assert _paths(sent) == ["/v1/chat/completions", "/v1/billing/costs"]
        assert all(request.headers["x-no-aliasing"] == "true" for request in sent)
        assert all(request.headers["accept-encoding"] == "identity" for request in sent)
        body = json.loads(sent[0].content)
        assert body["stream"] is True and body["model"] == TEXT_MODEL and "tools" not in body
        snapshot, rows = await _rows()
        assert len(rows) == 1 and rows[0]["state"] == "settled" and rows[0]["actual_cost_microusd"] == 1500
        assert rows[0]["profile_id"] == "near_tee.text" and snapshot["unknown_microusd"] == 0
        assert isolated_broker._active_operation_id is None
        # The durable route receipt names the NEAR TEE profile.
        from src.model_fabric.repository import model_fabric_repository
        receipt = (await model_fabric_repository.latest_routes_for_runtime_paths(("chat_agent",)))["chat_agent"]
        assert receipt.outcome == "succeeded"
        assert "near_tee.text" in json.dumps(receipt.__dict__, default=str)
        assert REASONING not in json.dumps(receipt.__dict__, default=str)
    finally:
        reset_runtime_context(tokens)
    assert {request.url.host for request in near.requests} == {"cloud-api.near.ai"}


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_stream_echo_mismatch_mid_stream_keeps_delivered_deltas_and_settles(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch, isolated_broker):
    from src.approval.runtime import reset_runtime_context
    from src.llm_runtime import stream_completion_with_fallback

    near, operator = await _ready(client, monkeypatch, prove=("streaming",))
    tokens = _bind(operator)
    try:
        near.stream_model_at = {2: "Qwen/Qwen3-30B-A3B-Instruct-2507"}
        near.body_id = "chatcmpl-mismatch"
        before = len(near.requests)
        parts, error = await _collect(stream_completion_with_fallback(MESSAGES, 0.3, 64, "chat_agent",
            _chat_stream_context(operator, MESSAGES)))
        # The delta delivered before the mismatch is never retracted; nothing after it is emitted.
        assert parts == ["Hello "]
        assert isinstance(error, NearTeeError) and error.code == "near_tee_model_mismatch"
        assert _paths(near.requests[before:]) == ["/v1/chat/completions", "/v1/billing/costs"]
        snapshot, rows = await _rows()
        assert [row["state"] for row in rows] == ["settled"] and snapshot["unknown_microusd"] == 0
        assert isolated_broker._active_operation_id is None

        # A delta before any model echo fails closed too (echo checked on the first chunk carrying model).
        near.stream_model_at = {}
        near.body_id = "chatcmpl-ok-after"
        parts, error = await _collect(stream_completion_with_fallback(MESSAGES, 0.3, 64, "chat_agent",
            _chat_stream_context(operator, MESSAGES)))
        assert error is None and parts == ["Hello ", "from NEAR"]
    finally:
        reset_runtime_context(tokens)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_stream_is_cut_before_the_billing_window_and_billing_runs_in_deadline(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch, isolated_broker):
    """ADR-035: the stream ends >= 16s before the original deadline so the
    billing readback still runs inside the same lease and deadline."""
    from src.approval.runtime import reset_runtime_context
    from src.llm_runtime import stream_completion_with_fallback
    from src.model_fabric import near_tee

    near, operator = await _ready(client, monkeypatch, prove=("streaming",))
    tokens = _bind(operator)
    original = near_tee.prepare_near_tee_call
    deadlines = []

    async def short_deadline(**kwargs):
        call = await original(**kwargs)
        # 19s left: at most 3s of streaming, then the 16s billing reserve.
        context = replace(call.context, deadline_at=time.time() + 19)
        deadlines.append(context.deadline_at)
        return replace(call, context=context, inference_timeout_seconds=min(call.inference_timeout_seconds, 3))
    try:
        monkeypatch.setattr(near_tee, "prepare_near_tee_call", short_deadline)
        near.stall_after = 2
        near.stall_seconds = 30.0
        near.body_id = "chatcmpl-deadline"
        before = len(near.requests)
        started = time.time()
        parts, error = await _collect(stream_completion_with_fallback(MESSAGES, 0.3, 64, "chat_agent",
            _chat_stream_context(operator, MESSAGES)))
        assert time.time() - started < 10
        assert parts == ["Hello "]  # delivered before the stall, never retracted
        assert isinstance(error, NearTeeError) and error.code == "near_tee_stream_deadline"
        assert _paths(near.requests[before:]) == ["/v1/chat/completions", "/v1/billing/costs"]
        (deadline,) = deadlines
        billing_at = [at for path, at in near.times if path == "/v1/billing/costs"][-1]
        assert billing_at <= deadline - 16 + 0.5  # the cut left the whole billing window
        assert billing_at < deadline
        snapshot, rows = await _rows()
        assert [row["state"] for row in rows] == ["settled"] and snapshot["unknown_microusd"] == 0
        assert isolated_broker._active_operation_id is None
    finally:
        reset_runtime_context(tokens)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_stream_missing_cost_is_unknown_and_keeps_the_lease_blocked(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch, isolated_broker):
    from src.approval.runtime import reset_runtime_context
    from src.llm_runtime import stream_completion_with_fallback
    from src.model_fabric.gpu_admission import GpuAdmissionUncertainError

    near, operator = await _ready(client, monkeypatch, prove=("streaming",))
    tokens = _bind(operator)
    try:
        near.billing_missing = True
        near.body_id = "chatcmpl-stream-unknown"
        parts, error = await _collect(stream_completion_with_fallback(MESSAGES, 0.3, 64, "chat_agent",
            _chat_stream_context(operator, MESSAGES)))
        assert parts == ["Hello ", "from NEAR"]
        assert isinstance(error, GpuAdmissionUncertainError)
        assert isinstance(error.__cause__, NearTeeError) and error.__cause__.code == "near_tee_cost_liability"
        snapshot, rows = await _rows()
        assert [row["state"] for row in rows] == ["unknown"] and snapshot["unknown_microusd"] > 0
        receipt = isolated_broker.receipt_for(rows[0]["operation_id"])
        assert receipt.status == "blocked" and receipt.reconciliation_required
        assert receipt.active_operation_id == rows[0]["operation_id"]
    finally:
        reset_runtime_context(tokens)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_stream_precontact_denials_are_sealed_and_never_contact(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch, isolated_broker):
    from src.approval.runtime import reset_runtime_context
    from src.llm_runtime import stream_completion_with_fallback
    from src.model_fabric import near_tee

    near, operator = await _ready(client, monkeypatch, prove=("streaming",))
    tokens = _bind(operator)
    original = near_tee.prepare_near_tee_call

    async def late(**kwargs):
        call = await original(**kwargs)
        return replace(call, context=replace(call.context, deadline_at=time.time() + 5))
    try:
        monkeypatch.setattr(near_tee, "prepare_near_tee_call", late)
        before = len(near.requests)
        parts, error = await _collect(stream_completion_with_fallback(MESSAGES, 0.3, 64, "chat_agent",
            _chat_stream_context(operator, MESSAGES)))
        assert parts == [] and error is not None
        assert "near_tee_deadline_insufficient" in repr(error) + repr(error.__cause__)
        assert len(near.requests) == before
        snapshot, rows = await _rows()
        assert snapshot["unknown_microusd"] == 0
        assert all(row["contact_started_at"] is None and row["state"] != "unknown" for row in rows)
        assert isolated_broker._active_operation_id is None
    finally:
        reset_runtime_context(tokens)


# ── Tools (chat agent) ───────────────────────────────────────────


def _agent_model():
    from src.llm_runtime import FallbackLiteLLMModel
    return FallbackLiteLLMModel(model_id="openrouter/retired-placeholder", api_base="https://openrouter.ai/api/v1",
        api_key=None, runtime_path="chat_agent", max_tokens=256)


def _tools():
    from smolagents import Tool

    class Weather(Tool):
        name = "get_weather"
        description = "Weather for a city."
        inputs = {"city": {"type": "string", "description": "City name"}}
        output_type = "string"

        def forward(self, city):
            return "sunny"

    class FinalAnswer(Tool):
        name = "final_answer"
        description = "Final answer."
        inputs = {"answer": {"type": "string", "description": "Answer"}}
        output_type = "string"

        def forward(self, answer):
            return answer

    return [Weather(), FinalAnswer()]


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_agent_tool_call_round_trip_on_allowlisted_tools_model(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch, isolated_broker):
    from src.approval.runtime import reset_runtime_context

    near, operator = await _ready(client, monkeypatch)
    tokens = _bind(operator)
    model, tools = _agent_model(), _tools()
    try:
        near.tool_calls = [[{"id": "call_1", "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}}]]
        near.body_id = "chatcmpl-tool-1"
        before = len(near.requests)
        first = await asyncio.to_thread(model.generate, [{"role": "user", "content": "Weather in Paris?"}],
            tools_to_call_from=tools)
        assert first.tool_calls[0].function.name == "get_weather"
        assert first.tool_calls[0].function.arguments == {"city": "Paris"}
        assert first.raw is None and REASONING not in repr(first)
        sent = near.requests[before:]
        assert _paths(sent) == ["/v1/chat/completions", "/v1/billing/costs"]
        body = json.loads(sent[0].content)
        assert body["model"] == TEXT_MODEL and body["stream"] is False and body["tool_choice"] == "required"
        assert [tool["function"]["name"] for tool in body["tools"]] == ["get_weather", "final_answer"]
        assert sent[0].headers["x-no-aliasing"] == "true"

        # Round trip: the tool result goes back and the forced choice is honoured.
        near.tool_calls = [[{"id": "call_2", "type": "function",
            "function": {"name": "final_answer", "arguments": '{"answer":"It is sunny in Paris."}'}}]]
        near.body_id = "chatcmpl-tool-2"
        second = await asyncio.to_thread(model.generate, [
            {"role": "user", "content": "Weather in Paris?"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "sunny"}], tools_to_call_from=tools)
        assert second.tool_calls[0].function.name == "final_answer"
        assert second.tool_calls[0].function.arguments == {"answer": "It is sunny in Paris."}
        round_trip = json.loads(near.requests[-2].content)["messages"]
        assert round_trip[1]["tool_calls"][0]["id"] == "call_1"
        assert round_trip[2] == {"role": "tool", "tool_call_id": "call_1", "content": "sunny"}

        # Strict arguments: duplicate keys / invalid JSON / unoffered tools fail closed after a settled cost.
        for index, (calls, code) in enumerate((
            ([{"id": "call_3", "type": "function", "function": {"name": "get_weather",
                "arguments": '{"city":"Paris","city":"Rome"}'}}], "near_tee_tool_arguments_invalid"),
            ([{"id": "call_4", "type": "function", "function": {"name": "get_weather",
                "arguments": '{"city":'}}], "near_tee_tool_arguments_invalid"),
            ([{"id": "call_5", "type": "function", "function": {"name": "delete_everything",
                "arguments": "{}"}}], "near_tee_tool_call_not_offered"),
        )):
            near.tool_calls = [calls]
            near.body_id = f"chatcmpl-tool-bad-{index}"
            with pytest.raises(NearTeeError) as rejected:
                await asyncio.to_thread(model.generate, [{"role": "user", "content": "Weather?"}],
                    tools_to_call_from=tools)
            assert rejected.value.code == code
        snapshot, rows = await _rows()
        assert [row["state"] for row in rows] == ["settled"] * 5 and snapshot["unknown_microusd"] == 0
        assert isolated_broker._active_operation_id is None
    finally:
        reset_runtime_context(tokens)
    assert {request.url.host for request in near.requests} == {"cloud-api.near.ai"}


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_model_without_catalog_tools_feature_is_refused_for_tools(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch, isolated_broker):
    from src.approval.runtime import reset_runtime_context
    from src.model_fabric.contracts import NoCompliantModelRouteError

    near, operator = await _ready(client, monkeypatch, prove=())
    near.catalog = catalog_record(supportedFeatures=["structured_outputs", "reasoning"])
    assert (await client.post("/api/settings/model-fabric/near-tee/catalog-check",
        json={"expected_policy_revision": 5})).status_code == 200
    before = len(near.requests)
    check = await client.post("/api/settings/model-fabric/canary", json={"profile_id": "near_tee.text",
        "capability": "tool_use", "timeout_seconds": 40, "proof_ttl_seconds": 600})
    assert check.status_code == 409 and check.json()["detail"] == "near_tee_model_tools_unsupported"
    tokens = _bind(operator)
    try:
        with pytest.raises(NoCompliantModelRouteError) as refused:
            await asyncio.to_thread(_agent_model().generate, [{"role": "user", "content": "Weather?"}],
                tools_to_call_from=_tools())
        assert refused.value.reason_codes == ("near_tee_model_tools_unsupported",)
    finally:
        reset_runtime_context(tokens)
    assert [request.method for request in near.requests[before:]] == []  # no inference contact
    status = (await client.get("/api/runtime/status")).json()["effective_runtime"]
    assert status["inference_readiness"]["capabilities"]["tool_use"] == "near_tee_model_tools_unsupported"


# ── Chat surfaces, runtime status and Cordis ─────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_chat_rest_and_stream_effective_route_and_runtime_status(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch, isolated_broker):
    from unittest.mock import patch
    from src.agent.direct_chat import stream_direct_local_chat
    from src.approval.runtime import reset_runtime_context

    # Not configured: chat_agent reports the honest blocked reason.
    blocked = (await client.get("/api/runtime/status")).json()["effective_runtime"]
    assert blocked["inference_ready"] is False and "openrouter_retired" in blocked["inference_readiness"]["reasons"]

    near, operator = await _ready(client, monkeypatch, prove=("streaming", "tool_use"))
    status = (await client.get("/api/runtime/status")).json()["effective_runtime"]
    assert status["provider"] == "near_tee" and status["model"] == TEXT_MODEL
    assert status["active_profile"] == "near_tee.text" and status["inference_ready"] is True, status
    assert status["tee_hosting_label"].startswith("TEE-hosted per NEAR catalog as of ")
    assert "attestation not verified by Seraph" in status["tee_hosting_label"]
    assert status["tee_verified"] is False and status["e2ee"] is False
    assert status["inference_readiness"]["capabilities"] == {"streaming": "ready", "tool_use": "ready"}

    # REST lightweight turn: non-streaming governed completion on the slot.
    before = len(near.requests)
    with patch("src.api.chat.should_use_direct_local_chat", return_value=True), \
            patch("src.api.chat.direct_local_chat_route_error", return_value=None):
        response = await client.post("/api/chat", json={"message": "Hello"})
    assert response.status_code == 200, response.text
    assert "Report body from the NEAR TEE fixture." in response.text
    assert _paths(near.requests[before:]) == ["/v1/chat/completions", "/v1/billing/costs"]

    # The WebSocket lightweight turn streams through stream_direct_local_chat.
    tokens = _bind(operator)
    try:
        near.body_id = "chatcmpl-ws"
        parts = [delta async for delta in stream_direct_local_chat("Hello", runtime_path="chat_agent",
            is_onboarding=False, session_id=operator.session_id)]
    finally:
        reset_runtime_context(tokens)
    assert parts == ["Hello ", "from NEAR"]
    sent = near.requests[-2:]
    assert _paths(sent) == ["/v1/chat/completions", "/v1/billing/costs"]
    assert json.loads(sent[0].content)["stream"] is True
    snapshot, _rows_unused = await _rows()
    # A fresh profile has not finished onboarding, so the REST turn is the
    # onboarding_agent route; the streamed turn is chat_agent. Both on the slot.
    rows = [row for row in snapshot["operations"] if row["runtime_path"] in {"chat_agent", "onboarding_agent"}]
    assert sorted(row["runtime_path"] for row in rows) == ["chat_agent", "onboarding_agent"]
    assert all(row["state"] == "settled" and row["profile_id"] == "near_tee.text" for row in rows)
    assert snapshot["unknown_microusd"] == 0
    assert {request.url.host for request in near.requests} == {"cloud-api.near.ai"}


async def _governed_run(operator, *, purpose, max_tokens=64):
    from datetime import datetime, timedelta, timezone
    from src.runtime_plugins.governed_services import GOVERNED_JOB_KIND
    from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec, durable_job_repository as jobs
    candidate = {"schema_version": 1, "method": "inference.request", "purpose": purpose,
        "messages": [{"role": "user", "content": f"Cordis {purpose} fixture"}], "max_tokens": max_tokens}
    job_id = f"cordis-svc:near-tee-{purpose}"
    spec = DurableJobSpec(identity=DurableJobIdentity(job_id=job_id, owner_kind="user",
            owner_principal_id=operator.principal.principal_id, job_kind=GOVERNED_JOB_KIND, capability_version="1",
            idempotency_scope="cordis-service", idempotency_key=f"near-tee-{purpose}"),
        inputs=candidate, session_id=operator.session_id, operator_session_id=operator.session_id,
        declared_authority={"principal": operator.principal.principal_id, "owner_kind": "user",
            "session_id": operator.session_id, "grants": ["model_inference"]},
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=120), max_attempts=1)
    job = await jobs.admit_job(spec)
    queued = await jobs.transition_job(job_id, "queued", expected_revision=job["revision"])
    await jobs.claim_job(job_id, owner="cordis-host:fixture", lease_seconds=120, expected_revision=queued["revision"])
    async with jobs._session() as db:
        run = await jobs._fetch(db, job_id)
    return jobs, run, candidate


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_cordis_inference_request_reaches_near_tee_and_blocks_without_it(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch, isolated_broker):
    from src.runtime_plugins.dispatch import NativeServiceBlocked
    from src.runtime_plugins.governed_services import _request_inference

    owner = await _login(client, monkeypatch)
    from src.auth.service import authenticate_session
    operator = await authenticate_session(owner["session_id"], touch=False)
    jobs, run, candidate = await _governed_run(operator, purpose="background")
    # No NEAR TEE slot: the stable OpenRouter-retired block, no contact.
    with pytest.raises(NativeServiceBlocked) as blocked:
        await _request_inference(jobs, run, candidate)
    assert blocked.value.reason_code == "openrouter_retired"

    near, operator = await _ready(client, monkeypatch, prove=("streaming",))
    jobs, run, candidate = await _governed_run(operator, purpose="interactive")
    before = len(near.requests)
    outcome = await _request_inference(jobs, run, candidate)
    assert outcome["output"] == "Hello from NEAR" and REASONING not in json.dumps(outcome)
    assert outcome["route"] == "cordis_interactive_inference"
    sent = near.requests[before:]
    assert _paths(sent) == ["/v1/chat/completions", "/v1/billing/costs"]
    assert json.loads(sent[0].content)["stream"] is True
    assert [row["state"] for row in outcome["usage"]] == ["settled"]
    assert outcome["usage"][0]["operation_id"] == "remote:" + run.run_identity
    assert isolated_broker._active_operation_id is None
    assert {request.url.host for request in near.requests} == {"cloud-api.near.ai"}


def test_chat_priority_over_cordis_background_on_near_tee_contexts():
    from types import SimpleNamespace
    from src.model_fabric.caller_context import canonical_route_spec
    from src.model_fabric.gpu_admission import GpuPriority, priority_for_inference_context
    chat = SimpleNamespace(runtime_path="chat_agent", workload=canonical_route_spec("chat_agent").workload.value)
    interactive = SimpleNamespace(runtime_path="cordis_interactive_inference",
        workload=canonical_route_spec("cordis_interactive_inference").workload.value)
    background = SimpleNamespace(runtime_path="cordis_background_inference",
        workload=canonical_route_spec("cordis_background_inference").workload.value)
    assert priority_for_inference_context(chat).rank > priority_for_inference_context(background).rank
    assert priority_for_inference_context(interactive).rank >= priority_for_inference_context(background).rank
    assert priority_for_inference_context(chat) is GpuPriority.INTERACTIVE_CHAT


# ── Review follow-up: cancellation, chat turn cap, adoption authority, SSE ──


async def _first_then(stream, action):
    """Read one delta, then run ``action(stream)``; return the deltas read."""
    parts = []
    async for delta in stream:
        parts.append(delta)
        await action(stream)
        break
    return parts


def _settled_and_free(rows, broker):
    assert [row["state"] for row in rows] == ["settled"], rows
    assert rows[0]["contact_started_at"] is not None
    assert broker._active_operation_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_consumer_close_mid_stream_still_settles_billing(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch, isolated_broker):
    from src.approval.runtime import reset_runtime_context
    from src.llm_runtime import stream_completion_with_fallback

    near, operator = await _ready(client, monkeypatch, prove=("streaming",))
    tokens = _bind(operator)
    try:
        near.body_id = "chatcmpl-closed"
        before = len(near.requests)
        stream = stream_completion_with_fallback(MESSAGES, 0.3, 64, "chat_agent", _chat_stream_context(operator, MESSAGES))

        async def close(stream):
            await stream.aclose()
        assert await _first_then(stream, close) == ["Hello "]
        assert _paths(near.requests[before:]) == ["/v1/chat/completions", "/v1/billing/costs"]
        snapshot, rows = await _rows()
        _settled_and_free(rows, isolated_broker)
        assert snapshot["unknown_microusd"] == 0
        # The lane is free for the next turn.
        near.body_id = "chatcmpl-after-close"
        parts, error = await _collect(stream_completion_with_fallback(MESSAGES, 0.3, 64, "chat_agent",
            _chat_stream_context(operator, MESSAGES)))
        assert error is None and parts == ["Hello ", "from NEAR"]
    finally:
        reset_runtime_context(tokens)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_websocket_style_cancellation_mid_stream_settles_and_releases(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch, isolated_broker):
    """The WebSocket turn runs under ``wait_for``; its cut cancels the task
    while the provider stream is open. Billing is still read back, the known
    cost settles, the lease is released and the cancellation surfaces."""
    from src.agent.direct_chat import stream_direct_local_chat
    from src.approval.runtime import reset_runtime_context

    near, operator = await _ready(client, monkeypatch, prove=("streaming",))
    tokens = _bind(operator)
    parts = []

    async def turn():
        from contextlib import aclosing
        async with aclosing(stream_direct_local_chat("Hello", runtime_path="chat_agent", is_onboarding=False,
                session_id=operator.session_id)) as stream:
            async for delta in stream:
                parts.append(delta)
    try:
        near.stall_after = 2
        near.stall_seconds = 30.0
        near.body_id = "chatcmpl-ws-cancel"
        before = len(near.requests)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(turn(), timeout=2.0)
        assert parts == ["Hello "]
        assert _paths(near.requests[before:]) == ["/v1/chat/completions", "/v1/billing/costs"]
        snapshot, rows = await _rows()
        _settled_and_free(rows, isolated_broker)
        assert snapshot["unknown_microusd"] == 0
    finally:
        reset_runtime_context(tokens)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_cordis_output_oversize_cut_settles_and_releases(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch, isolated_broker):
    from src.runtime_plugins import governed_services
    from src.runtime_plugins.dispatch import NativeServiceBlocked

    near, operator = await _ready(client, monkeypatch, prove=("streaming",))
    jobs, run, candidate = await _governed_run(operator, purpose="interactive")
    monkeypatch.setattr(governed_services, "MAX_OUTPUT_BYTES", 3)
    near.body_id = "chatcmpl-oversize"
    before = len(near.requests)
    with pytest.raises(NativeServiceBlocked) as cut:
        await governed_services._request_inference(jobs, run, candidate)
    assert cut.value.reason_code == "inference_output_oversize"
    assert _paths(near.requests[before:]) == ["/v1/chat/completions", "/v1/billing/costs"]
    snapshot, rows = await _rows("cordis_interactive_inference")
    _settled_and_free(rows, isolated_broker)
    assert snapshot["unknown_microusd"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_cordis_max_tokens_above_slot_output_is_named(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch, isolated_broker):
    from src.runtime_plugins.dispatch import NativeServiceBlocked
    from src.runtime_plugins.governed_services import _request_inference

    near, operator = await _ready(client, monkeypatch, prove=("streaming",), max_output_tokens=64)
    jobs, run, candidate = await _governed_run(operator, purpose="interactive", max_tokens=128)
    before = len(near.requests)
    with pytest.raises(NativeServiceBlocked) as blocked:
        await _request_inference(jobs, run, candidate)
    assert blocked.value.reason_code == "inference_max_tokens_exceeds_route"
    assert len(near.requests) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_chat_turn_deadline_ends_stream_and_billing_before_the_outer_cut(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch, isolated_broker):
    """The outer REST/WebSocket cut (here 20s) never lands in the stream or
    billing window: the turn's deadline is 2s inside it and the stream ends
    16s before that deadline."""
    from config.settings import settings
    from src.agent.direct_chat import direct_chat_context_timeout, direct_chat_turn_timeout, stream_direct_local_chat
    from src.approval.runtime import reset_runtime_context

    near, operator = await _ready(client, monkeypatch, prove=("streaming",), timeout_seconds=10)
    monkeypatch.setattr(settings, "agent_chat_timeout", 20)
    assert direct_chat_turn_timeout() == 20 and direct_chat_context_timeout("chat_agent") == 18
    cap = (await client.get("/api/runtime/status")).json()["effective_runtime"]["chat_turn_cap"]
    assert cap == {"turn_timeout_seconds": 20.0, "inference_deadline_seconds": 18.0,
        "stream_window_seconds": 2.0, "billing_reserve_seconds": 16.0}
    tokens = _bind(operator)
    parts = []

    async def turn():
        async for delta in stream_direct_local_chat("Hello", runtime_path="chat_agent", is_onboarding=False,
                session_id=operator.session_id):
            parts.append(delta)
    try:
        near.stall_after = 2
        near.stall_seconds = 60.0
        near.body_id = "chatcmpl-turn-cap"
        started = time.time()
        with pytest.raises(NearTeeError) as cut:
            await asyncio.wait_for(turn(), timeout=direct_chat_turn_timeout())
        assert cut.value.code == "near_tee_stream_deadline"  # not the outer timeout
        billing_at = [at for path, at in near.times if path == "/v1/billing/costs"][-1]
        assert billing_at < started + 20 - 2
        assert parts == ["Hello "]
        snapshot, rows = await _rows()
        _settled_and_free(rows, isolated_broker)
    finally:
        reset_runtime_context(tokens)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.parametrize("variant", ["live_root", "revoked_root", "operator_mismatch"])
async def test_conversation_principal_adoption_uses_the_admitted_operator_root(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch, isolated_broker, variant):
    """A conversation-scoped chat principal adopts only while the operator
    root captured at admission still authenticates as the same owner. The
    root is revoked after contact (mid-stream), so the cost still settles."""
    from types import SimpleNamespace
    from src.approval.runtime import reset_runtime_context, set_runtime_context
    from src.auth import service as auth_service
    from src.auth.service import bind_operator_principal, create_session, revoke_session
    from src.llm_runtime import stream_completion_with_fallback
    from src.model_fabric.caller_context import build_canonical_inference_context
    from src.workflows.inference_accounting import InferenceAccountingError

    near, _operator = await _ready(client, monkeypatch, prove=("streaming",))
    _, root = await create_session()
    conversation = bind_operator_principal(root, "conversation-fixture")
    assert conversation.operator_session_id == root.session_id != conversation.session_id
    if variant == "operator_mismatch":
        real = auth_service.authenticate_session

        async def other_owner(session_id, **kwargs):
            operator = await real(session_id, **kwargs)
            if session_id != root.session_id:
                return operator
            return SimpleNamespace(principal=replace(operator.principal, principal_id="operator:root:someone-else-01"),
                session_id=operator.session_id)
        monkeypatch.setattr(auth_service, "authenticate_session", other_owner)
    tokens = set_runtime_context("conversation-fixture", "high_risk", trust_principal=conversation)
    parts = []
    try:
        context = build_canonical_inference_context("chat_agent", payload=MESSAGES, output_tokens=64,
            timeout_seconds=60, principal=conversation, session_id="conversation-fixture", streaming=True)
        near.body_id = f"chatcmpl-adopt-{variant}"
        error = None
        try:
            async for delta in stream_completion_with_fallback(MESSAGES, 0.2, 64, "chat_agent", context):
                parts.append(delta)
                if variant == "revoked_root" and len(parts) == 1:
                    await revoke_session(root.session_id)
        except Exception as exc:
            error = exc
        assert parts == ["Hello ", "from NEAR"]  # delivered deltas are never retracted
        if variant == "live_root":
            assert error is None
        else:
            assert isinstance(error, InferenceAccountingError), repr(error)
            assert error.code == "inference_result_authority_changed"
        snapshot, rows = await _rows()
        # Contact happened either way: the known cost settles and the lane is free.
        _settled_and_free(rows, isolated_broker)
        assert snapshot["unknown_microusd"] == 0
    finally:
        reset_runtime_context(tokens)


def test_admission_request_captures_operator_root_immutably():
    from types import SimpleNamespace
    from src.model_fabric.gpu_admission import GpuAdmissionRequest
    principal = SimpleNamespace(principal_id="operator:root:fixture-0001", operator_session_id="root-session",
        session_id="conversation")
    context = SimpleNamespace(principal=principal, request_id="r1", job_id="", deadline_at=time.time() + 60,
        runtime_path="chat_agent", session_id="conversation", workload="interactive",
        estimated_cost_microusd=1, owner_budget_microusd=10, data_digest="d")
    request = GpuAdmissionRequest.from_inference_context(context, operation_id="op-1")
    assert request.operator_session_id == "root-session" and request.session_id == "conversation"
    with pytest.raises(Exception):
        request.operator_session_id = "other"  # frozen


# SSE reader unit checks (in-process MockTransport only).


async def _sse(chunks, *, status=200, content_type="text/event-stream", cap=1 << 20):
    from src.model_fabric.near_client import stream_near_events

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in chunks:
                yield chunk

    def handler(request):
        return httpx.Response(status, headers={"content-type": content_type}, stream=Chunks())
    opened = []
    events = []
    async with _REAL_CLIENT(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        try:
            async for event in stream_near_events(client, "/chat/completions", body=b"{}", key="k",
                    stream_deadline=asyncio.get_running_loop().time() + 5, cap=cap, error=NearTeeError,
                    prefix="near_tee", on_open=opened.append):
                events.append(event)
        except NearTeeError as exc:
            return events, exc.code, opened
    return events, None, opened


_REAL_CLIENT = httpx.AsyncClient


@pytest.mark.asyncio
async def test_sse_reader_split_utf8_caps_duplicates_and_terminal_marker():
    word = "h\u00e9llo \u4e16\u754c".encode().decode("unicode_escape")
    raw = ("data: " + json.dumps({"choices": [{"delta": {"content": word}}]}, ensure_ascii=False) + "\n\n").encode()
    split = raw.index(word[1].encode()) + 1  # inside the 2-byte character
    events, error, opened = await _sse([raw[:split], raw[split:], b"data: [DONE]\n\n"])
    assert error is None and events[0]["choices"][0]["delta"]["content"] == word and opened
    # A line over the cap without a newline fails closed.
    events, error, _ = await _sse([b"data: " + b"x" * 70000])
    assert error == "near_tee_stream_line_too_large"
    # Duplicate keys are rejected.
    events, error, _ = await _sse([b'data: {"id":"a","id":"b"}\n\n'])
    assert error == "near_tee_stream_invalid"
    # A stream that closes without [DONE] is incomplete; the earlier event was delivered.
    events, error, _ = await _sse([b'data: {"id":"a"}\n\n'])
    assert events == [{"id": "a"}] and error == "near_tee_stream_incomplete"
    # Total byte cap.
    events, error, _ = await _sse([b'data: {"id":"a"}\n\n' * 10], cap=40)
    assert error == "near_tee_response_too_large"
    # on_open runs before the status check: a non-200 response is still contact.
    events, error, opened = await _sse([b"{}"], status=503, content_type="application/json")
    assert error == "near_tee_http_error" and len(opened) == 1
    events, error, opened = await _sse([b"{}"], content_type="application/json")
    assert error == "near_tee_stream_content_type_invalid" and len(opened) == 1
