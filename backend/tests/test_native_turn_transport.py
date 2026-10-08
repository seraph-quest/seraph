"""Actual stock IPC/native transcript ownership; scripted model boundary only."""
import asyncio
import json
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from smolagents import ToolCallingAgent
from smolagents.models import Model, ChatMessage, ChatMessageToolCall, ChatMessageToolCallFunction
from sqlalchemy import select

from config.settings import settings
from src.auth.service import create_session
from src.db.engine import get_session
from src.db.models import Message, WorkflowRunState
from src.runtime_plugins.bridge import CordisHost
from src.runtime_plugins.dispatch import NativeServiceDispatcher
from tests.test_runtime_composition_ownership import composition_db


NODE = Path('/home/pawel/repos/seraph/.agent-worktrees/986-a1-host/.agent-evidence/986/a1-upstream/node-v24.13.1-linux-x64/bin/node')


class ScriptedModel(Model):
    """Owned deterministic model transport, no inference/provider request."""
    def __init__(self):
        super().__init__(model_id="owned-scripted-transport")
        self.calls = 0

    def generate(self, messages, **kwargs):
        self.calls += 1
        return ChatMessage(role="assistant", content="", tool_calls=[ChatMessageToolCall(
            id="owned-final", type="function", function=ChatMessageToolCallFunction(
                name="final_answer", arguments={"answer": "Scripted native reply"}))])


@pytest_asyncio.fixture
async def native_transport(composition_db, monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "isolated-native-transport")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "agent_chat_timeout", 120)
    token, operator = await create_session()
    host = CordisHost(node_path=NODE, service_dispatch=NativeServiceDispatcher())
    monkeypatch.setattr("src.runtime_plugins.bridge.cordis_host", host)
    monkeypatch.setattr("src.api.chat.direct_local_chat_route_error", lambda **kwargs: _no_route_error())
    monkeypatch.setattr("src.api.ws.direct_local_chat_route_error", lambda **kwargs: _no_route_error())
    model = ScriptedModel()
    agent = ToolCallingAgent(tools=[], model=model, max_steps=1, verbosity_level=0)
    monkeypatch.setattr("src.api.chat.create_onboarding_agent", lambda message: agent)
    monkeypatch.setattr("src.api.ws.create_onboarding_agent", lambda message: agent)
    direct_calls = []
    def scripted_completion(**kwargs):
        direct_calls.append(kwargs)
        return {"choices": [{"message": {"content": "Scripted native reply"}}]}
    monkeypatch.setattr("src.agent.direct_chat.completion_with_fallback_sync", scripted_completion)
    async def scripted_stream(message, **kwargs):
        yield "Scripted "
        yield "native reply"
    monkeypatch.setattr("src.api.ws.stream_direct_local_chat", scripted_stream)
    await host.start()
    assert host.admitting and len((await host.refresh_status())["plugins"]) == 15
    try:
        yield host, token, operator, model, direct_calls
    finally:
        await host.stop()
        cleanup = host.snapshot()["cleanup"]
        assert cleanup["state"] == "clean"
        assert cleanup["process_reaped"] is True and cleanup["resources_remaining"] == 0
        assert cleanup["cordis_disposal"] == "confirmed"


async def _no_route_error():
    return None


async def assert_native_result(operator, data, branch):
    async with get_session() as db:
        messages = list((await db.execute(select(Message).where(
            Message.session_id == data["session_id"]).order_by(Message.created_at))).scalars())
        assert [message.role for message in messages] == ["user", "assistant"]
        job = await db.scalar(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == "conversation-turn:" + messages[0].id))
        assert job.status == "succeeded" and job.attempt_count == 1 and job.max_attempts == 1
        assert json.loads(job.composition_binding_json)["native_branch"] == branch + "_turn"
        assert job.operator_session_id == operator.session_id == job.session_id
        assert job.conversation_id == messages[0].session_id != operator.session_id
        assert job.lease_owner is None and job.lease_expires_at is None
        assert messages[1].id == data["message_id"]
        receipts = json.loads(job.checkpoint_receipts_json)
        output = next(item for item in receipts if item["checkpoint_id"] == "conversation:assistant-message")
        assert output["safe"] is True and output["payload"] == {
            "schema_version": 1, "message_ref": messages[1].id,
            "input_message_ref": messages[0].id, "no_learning": True}
        assert sum(item["checkpoint_id"].startswith("runtime-service-invocation") for item in receipts) == 1
        return messages[0].id, job.run_fingerprint


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["direct", "generic"])
async def test_real_stock_host_rest_claim_loop_output_and_duplicate(native_transport, branch):
    from src.app import create_app
    host, token, operator, model, direct_calls = native_transport
    body = {"message": "Hello" if branch == "direct" else "Inspect this short answer",
        "message_id": "owned-rest-" + branch}
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: token}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        response = await client.post("/api/chat", json=body)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["response"] == "Scripted native reply"
        _, fingerprint = await assert_native_result(operator, data, branch)
        body["session_id"] = data["session_id"]
        replay = await client.post("/api/chat", json=body)
        assert replay.status_code == 409 and replay.json()["detail"]["code"] == "chat_message_duplicate"
        _, repeated = await assert_native_result(operator, data, branch)
        assert fingerprint == repeated
    assert len(direct_calls) == (1 if branch == "direct" else 0)
    assert model.calls == (1 if branch == "generic" else 0)
    assert host.admitting


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["direct", "generic"])
async def test_real_stock_host_asgi_ws_claim_frames_and_output(native_transport, branch):
    from src.app import create_app
    host, token, operator, model, direct_calls = native_transport
    incoming = asyncio.Queue()
    frames = []
    final = asyncio.get_running_loop().create_future()
    async def send(message):
        if message["type"] == "websocket.send":
            value = json.loads(message["text"])
            frames.append(value)
            if value["type"] in {"final", "error"} and not final.done():
                final.set_result(value)
    scope = {"type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws",
        "path": "/ws/chat", "raw_path": b"/ws/chat", "query_string": b"",
        "root_path": "", "server": ("127.0.0.1", 8004), "client": ("127.0.0.1", 12345),
        "headers": [(b"host", b"127.0.0.1:8004"), (b"origin", b"http://127.0.0.1:3001"),
            (b"cookie", (settings.operator_auth_cookie_name + "=" + token).encode())], "subprotocols": []}
    await incoming.put({"type": "websocket.connect"})
    await incoming.put({"type": "websocket.receive", "text": json.dumps({"type": "message",
        "message": "Hello" if branch == "direct" else "Inspect this short answer",
        "message_id": "owned-ws-" + branch})})
    task = asyncio.create_task(create_app()(scope, incoming.get, send))
    try:
        result = await asyncio.wait_for(asyncio.shield(final), timeout=30)
        assert result["type"] == "final", frames
        assert result["content"] == "Scripted native reply"
        assert any(frame["type"] == "status" for frame in frames)
        if branch == "direct":
            assert "".join(frame["content"] for frame in frames if frame["type"] == "delta") == result["content"]
        assert [frame["seq"] for frame in frames] == sorted({frame["seq"] for frame in frames})
        await assert_native_result(operator, result, branch)
    finally:
        await incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, timeout=5)
    assert model.calls == (1 if branch == "generic" else 0)
    assert host.admitting


@pytest.mark.asyncio
async def test_real_stock_native_rest_cancel_retains_pending_claim_and_fences_output(native_transport, monkeypatch):
    from threading import Event
    from src.app import create_app
    from src.workflows.job_runtime import durable_job_repository as jobs
    host, token, operator, _, _ = native_transport
    started, release, completed = Event(), Event(), Event()
    def held_completion(**kwargs):
        started.set()
        try:
            assert release.wait(10)
            return {"choices": [{"message": {"content": "Late callback reply"}}]}
        finally:
            completed.set()
    monkeypatch.setattr("src.agent.direct_chat.completion_with_fallback_sync", held_completion)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: token}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        task = asyncio.create_task(client.post("/api/chat", json={"message": "Hello",
            "message_id": "owned-cancel-native-turn"}))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with get_session() as db:
                run = await db.scalar(select(WorkflowRunState).where(
                    WorkflowRunState.job_kind == "conversation_turn_v1"))
                job_id = run.run_identity
                original = (run.attempt_count, run.fencing_token, run.deadline_at, run.lease_owner)
                assert run.status == "running" and run.attempt_count == 1
                assert not completed.is_set()
            pending = await jobs.get_job(job_id)
            assert pending["native_turn_execution"] == {
                "phase": "possibly_started", "physical_completion": "unproven", "replay": "denied"}
        finally:
            release.set()
            assert await asyncio.to_thread(completed.wait, 5)
            if not task.done():
                await task
        async with get_session() as db:
            run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))
            assert (run.attempt_count, run.fencing_token, run.deadline_at, run.lease_owner) == original
            assert run.status == "running"
            messages = list((await db.execute(select(Message).where(
                Message.session_id == run.conversation_id))).scalars())
            assert [message.role for message in messages] == ["user"]
            assert not any(item["checkpoint_id"] == "conversation:assistant-message"
                for item in json.loads(run.checkpoint_receipts_json))
    assert host.admitting
