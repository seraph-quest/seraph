"""Actual authenticated native route; only final provider HTTP is scripted."""
import json
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from config.settings import settings
from src.db.engine import get_session
from src.db.models import WorkflowRunState, InferenceCostReservation, Message
from tests.test_native_turn_transport import composition_db, native_transport
from tests.test_native_turn_cleanup import original_app
from src.agent.direct_chat import _uses_openrouter_profile as original_openrouter_profile


async def original_websocket_turn(app, token, *, early_close=False, consumer_errors=None):
    incoming, frames = asyncio.Queue(), []
    final = asyncio.get_running_loop().create_future()
    async def send(message):
        if message["type"] == "websocket.send":
            value = json.loads(message["text"])
            frames.append(value)
            if early_close and value["type"] == "delta" and not consumer_errors:
                error = RuntimeError("Original WS consumer closed after its first delta")
                consumer_errors.append(error)
                raise error
            if value["type"] in ("final", "error") and not final.done():
                final.set_result(value)
    scope = {"type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws",
        "path": "/ws/chat", "raw_path": b"/ws/chat", "query_string": b"", "root_path": "",
        "server": ("127.0.0.1", 8004), "client": ("127.0.0.1", 12345), "subprotocols": [],
        "headers": [(b"host", b"127.0.0.1:8004"), (b"origin", b"http://127.0.0.1:3001"),
            (b"cookie", (settings.operator_auth_cookie_name + "=" + token).encode())]}
    await incoming.put({"type": "websocket.connect"})
    await incoming.put({"type": "websocket.receive", "text": json.dumps({"type": "message", "message": "Hello", "message_id": "actual-inference-stream"})})
    task = asyncio.create_task(app(scope, incoming.get, send))
    try:
        value = await asyncio.wait_for(asyncio.shield(final), 30)
        assert value["type"] == ("error" if early_close else "final"), frames
        if early_close:
            value = dict(value)
            value["session_id"] = next(item["session_id"] for item in frames if item["type"] == "delta")
        else:
            assert "".join(item["content"] for item in frames if item["type"] == "delta") == value["content"]
        return value
    finally:
        await incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("route,outcome", [("direct", "success"), ("generic", "success"), ("stream", "success"),
    ("direct", "nonzero_cost"),
    ("stream", "early_close"),
    ("direct", "unknown_cost"), ("direct", "transport_failed"),
    ("direct", "foreign_conversation"), ("direct", "foreign_root"), ("direct", "copied_result")])
async def test_actual_original_direct_route_seals_same_owner_before_ack(native_transport, monkeypatch, route, outcome):
    from src.agent import direct_chat
    from src.llm_runtime import completion_with_fallback_sync
    from src.vlm_runtime import direct_local_chat_route_error
    from src.workflows.job_runtime import durable_job_repository
    assert callable(getattr(durable_job_repository, "seal_native_inference_output", None)), "actual output owner not implemented"
    original_seal = durable_job_repository.seal_native_inference_output
    async def observe_seal(**kwargs):
        try:
            if outcome == "copied_result":
                from copy import copy
                kwargs["result_witness"] = copy(kwargs["result_witness"])
            return await original_seal(**kwargs)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Actual original output owner denied")
            raise
    monkeypatch.setattr(durable_job_repository, "seal_native_inference_output", observe_seal)
    monkeypatch.setattr(settings, "openrouter_api_key", "intercepted-test-key")
    monkeypatch.setattr(settings, "openrouter_allowed_upstreams", "openai")
    monkeypatch.setattr(direct_chat, "completion_with_fallback_sync", completion_with_fallback_sync)
    monkeypatch.setattr(direct_chat, "_uses_openrouter_profile", original_openrouter_profile)
    monkeypatch.setattr("src.api.chat.direct_local_chat_route_error", direct_local_chat_route_error)
    monkeypatch.setattr("src.api.ws.direct_local_chat_route_error", direct_local_chat_route_error)
    if route == "stream":
        from src.llm_runtime import stream_completion_with_fallback
        monkeypatch.setattr(direct_chat, "stream_completion_with_fallback", stream_completion_with_fallback)
    if route == "generic":
        from src.agent.onboarding import create_onboarding_agent
        monkeypatch.setattr("src.api.chat.create_onboarding_agent", create_onboarding_agent)
    provider_calls = []
    original_candidates = []
    original_stream_closed = []
    consumer_errors = []
    if outcome in ("foreign_conversation", "foreign_root"):
        from src.model_fabric.native_inference import NativeInferenceContinuation
        from src.agent.session import session_manager
        from src.auth.service import create_session
        foreign_conversation = await session_manager.get_or_create(owner_principal_id=native_transport[2].principal.principal_id)
        _, foreign_operator = await create_session()
        original_start = NativeInferenceContinuation.start
        def change_original_binding(candidate):
            original_candidates.append(candidate)
            if outcome == "foreign_conversation":
                object.__setattr__(candidate.route_scope.context, "session_id", foreign_conversation.id)
            else:
                object.__setattr__(candidate.execution.admission, "principal", foreign_operator.principal)
            return original_start(candidate)
        monkeypatch.setattr(NativeInferenceContinuation, "start", change_original_binding)
    from src import llm_runtime
    from src.model_fabric import execution as model_execution
    from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
    original_broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    monkeypatch.setattr("src.model_fabric.remote_inference_admission.remote_inference_admission_broker", original_broker)
    monkeypatch.setattr(llm_runtime, "gpu_admission_broker", original_broker)
    monkeypatch.setattr(model_execution, "gpu_admission_broker", original_broker)
    original_preflight = llm_runtime._governed_preflight_target
    route_decisions = []
    def observe_preflight(*args, **kwargs):
        result = original_preflight(*args, **kwargs)
        if result[0] is not None:
            route_decisions.append([item.reason_code for item in result[0].rejections])
        return result
    monkeypatch.setattr(llm_runtime, "_governed_preflight_target", observe_preflight)
    real_async_post = httpx.AsyncClient.post
    async def scripted_canary(client, url, **kwargs):
        if str(url).startswith("https://openrouter.ai/"):
            provider_calls.append(("canary", kwargs["json"]))
            message = {"role": "assistant", "content": "CANARY_OK"}
            if kwargs["json"].get("tools"):
                message["tool_calls"] = [{"id": "canary-owned", "type": "function", "function": {"name": "canary_ok", "arguments": "{}"}}]
            return httpx.Response(200, request=httpx.Request("POST", url), json={"id": "gen-owned-canary-" + str(len(provider_calls)),
                "choices": [{"message": message}],
                "usage": {"cost": "0", "prompt_tokens": 1, "completion_tokens": 1}})
        return await real_async_post(client, url, **kwargs)
    def scripted_original(client, url, **kwargs):
        assert str(url) == "https://openrouter.ai/api/v1/chat/completions"
        provider_calls.append(("original", kwargs["json"]))
        from src.agent.native_turn_family import _current_call
        original_candidates.append(_current_call.get().operation.handle.native_inference_continuation)
        if outcome == "transport_failed":
            raise httpx.TransportError("Original scripted transport failed")
        message = {"role": "assistant", "content": "Actual original route reply"}
        if route == "generic":
            message["tool_calls"] = [{"id": "original-final-answer", "type": "function", "function": {
                "name": "final_answer", "arguments": json.dumps({"answer": "Actual original route reply"})}}]
        return httpx.Response(200, request=httpx.Request("POST", url), json={"id": "gen-owned-original",
            "choices": [{"message": message}],
            "usage": {**({"cost": "0.000001" if outcome == "nonzero_cost" else "0"} if outcome != "unknown_cost" else {}), "prompt_tokens": 1, "completion_tokens": 1}})
    monkeypatch.setattr(httpx.AsyncClient, "post", scripted_canary)
    monkeypatch.setattr(httpx.Client, "post", scripted_original)
    real_stream = httpx.AsyncClient.stream
    @asynccontextmanager
    async def scripted_stream(client, method, url, **kwargs):
        if str(url) != "https://openrouter.ai/api/v1/chat/completions":
            async with real_stream(client, method, url, **kwargs) as response:
                yield response
            return
        canary = kwargs["json"]["messages"][0].get("content") == "Reply with CANARY_OK only."
        provider_calls.append(("canary" if canary else "original", kwargs["json"]))
        if not canary:
            from src.agent.native_turn_family import _current_call
            original_candidates.append(_current_call.get().operation.handle.native_inference_continuation)
        text = "CANARY_OK" if canary else "Actual original route reply"
        events = [{"id": "actual-owned-stream", "choices": [{"delta": {"content": text}}]},
            {"id": "actual-owned-stream", "choices": [{"delta": {}}], "usage": {"cost": "0", "prompt_tokens": 1, "completion_tokens": 1}}]
        data = "".join("data: " + json.dumps(item) + "\n\n" for item in events) + "data: [DONE]\n\n"
        try:
            yield httpx.Response(200, request=httpx.Request(method, url), content=data.encode())
        finally:
            if not canary:
                original_stream_closed.append(True)
    monkeypatch.setattr(httpx.AsyncClient, "stream", scripted_stream)
    app = original_app()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8004",
            cookies={settings.operator_auth_cookie_name: native_transport[1]},
            headers={"origin": "http://127.0.0.1:3001"}) as client:
            configured = await client.put("/api/settings/model-fabric", json={"openrouter_setup": {"model_ids": ["openai/gpt-4o-mini"],
                    "capabilities": ["text", "tool_use", "streaming"], "allowed_upstreams": ["openai"],
                    "data_collection": "deny", "data_retention_policy": "deny", "egress_class": "cloud_allowed_full",
                    "cloud_egress_acknowledged": True, "spend_ceiling_microusd": 1000,
                    "request_cost_bound_microusd": 100, "credential_ref": "env:OPENROUTER_API_KEY"}})
            assert configured.status_code == 200, configured.text
            for capability in ("text", "health", "latency_ms", "tool_use", "streaming"):
                canary = await client.post("/api/settings/model-fabric/canary",
                    json={"profile_id": "openrouter", "capability": capability, "timeout_seconds": 120})
                assert canary.status_code == 200, canary.text
                assert canary.json()["outcome"] == "passed", canary.text
            async with get_session() as db:
                baseline_owners = set((await db.execute(select(WorkflowRunState.run_identity))).scalars())
            if route == "stream":
                result = await original_websocket_turn(app, native_transport[1], early_close=outcome == "early_close", consumer_errors=consumer_errors)
            else:
                response = await client.post("/api/chat", json={"message": "Hello" if route == "direct" else "Inspect this short answer",
                    "message_id": "owned-original-inference-direct"})
            if outcome not in ("success", "nonzero_cost"):
                if route != "stream":
                    assert response.status_code in (409, 500, 503), (response.text, route_decisions)
                originals = [kind for kind, _ in provider_calls].count("original")
                assert originals == (0 if outcome.startswith("foreign_") else 1)
                assert len(original_candidates) == 1
                candidate = original_candidates[0]
                if outcome.startswith("foreign_"):
                    assert candidate._rpc is None
                else:
                    assert candidate._rpc.done()
                    assert candidate._rpc.result()["status"] == "blocked"
                    if outcome != "copied_result":
                        assert candidate.result_witness is None and candidate.route_witness is None
                    if outcome == "early_close":
                        assert original_stream_closed == [True]
                        actual_broker_receipt = original_broker.receipt_for(candidate.handle.request.operation_id)
                        assert actual_broker_receipt.callback_completed is True
                        assert actual_broker_receipt.reconciliation_required is True
                        assert actual_broker_receipt.status == "blocked"
                        assert candidate.execution.worker.done() and not candidate.execution.worker.cancelled()
                        assert candidate.execution.worker.exception() is consumer_errors[0]
                async with get_session() as db:
                    owners = list((await db.execute(select(WorkflowRunState).where(WorkflowRunState.job_kind == "model_inference_ephemeral_v1", WorkflowRunState.run_identity.not_in(baseline_owners)))).scalars())
                    assert len(owners) == 1
                    assert owners[0].status != "succeeded"
                    assert not any(item["checkpoint_id"] == "inference:owned-output.v1" for item in json.loads(owners[0].checkpoint_receipts_json))
                    reservation = await db.scalar(select(InferenceCostReservation).where(InferenceCostReservation.job_id == owners[0].run_identity))
                    if outcome.startswith("foreign_"):
                        assert reservation.contact_started_at is None
                    elif outcome == "copied_result":
                        assert reservation.state == "settled" and reservation.actual_cost_microusd == 0
                        assert candidate.result_witness is not None and candidate.route_witness is not None
                    else:
                        assert reservation.contact_started_at is not None and reservation.state == "unknown"
                        assert owners[0].status == "cost_liability"
                    if outcome == "early_close":
                        turn = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == candidate.execution.admission.job_id))
                        assert turn.status == "cost_liability"
                        assert not any(item["checkpoint_id"] == "conversation:assistant-message" for item in json.loads(turn.checkpoint_receipts_json))
                return
            if route != "stream":
                assert response.status_code == 200, (response.text, route_decisions)
                result = response.json()
            assert "Actual original route reply" in json.dumps(result)
        assert [kind for kind, _ in provider_calls].count("original") == 1
        async with get_session() as db:
            input_message = await db.scalar(select(Message).where(Message.session_id == result["session_id"], Message.role == "user"))
            turn = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == "conversation-turn:" + input_message.id))
            assert turn.status == "succeeded"
            family = next(item["payload"] for item in json.loads(turn.checkpoint_receipts_json)
                if item["checkpoint_id"] == "conversation:operation-family")
            assert len(family["operations"]) == 1
            owner = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == family["operations"][0]["job_id"]))
            reservation = await db.scalar(select(InferenceCostReservation).where(InferenceCostReservation.job_id == owner.run_identity))
            assert owner.status == "succeeded" and reservation.state == "settled"
            assert reservation.actual_cost_microusd == (1 if outcome == "nonzero_cost" else 0)
            outputs = [item for item in json.loads(owner.checkpoint_receipts_json)
                if item["checkpoint_id"] == "inference:owned-output.v1"]
            assert len(outputs) == 1 and outputs[0]["safe"] is True
            payload = outputs[0]["payload"]
            assert payload["operation_id"] == reservation.operation_id
            assert payload["accounting_job_id"] == owner.run_identity
            assert payload["turn_job_id"] == turn.run_identity
            content = (Path(settings.workspace_dir) / payload["file_ref"]).read_bytes()
            import hashlib
            assert hashlib.sha256(content).hexdigest() == payload["content_sha256"]
            assert len(content) == payload["size_bytes"]
            assert b"Actual original route reply" in content
    finally:
        await app.state.native_turn_resources.shutdown()
