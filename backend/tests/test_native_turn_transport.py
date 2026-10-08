"""Actual stock IPC/native transcript ownership; scripted model boundary only."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from smolagents import ToolCallingAgent
from smolagents.models import Model, ChatMessage, ChatMessageToolCall, ChatMessageToolCallFunction
from sqlalchemy import event, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from config.settings import settings
from src.auth.service import create_session
from src.db.engine import get_session, override_session_factory
from src.db.models import Message, RuntimeCompositionState, WorkflowRunState
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


@asynccontextmanager
async def _stock_transport(monkeypatch):
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


@pytest_asyncio.fixture
async def native_transport(composition_db, monkeypatch):
    async with _stock_transport(monkeypatch) as transport:
        yield transport


@pytest_asyncio.fixture
async def fresh_native_transport(tmp_path, monkeypatch):
    root = tmp_path / "fresh-cpu"
    root.mkdir()
    monkeypatch.setattr(settings, "workspace_dir", str(root))
    monkeypatch.setenv("SERAPH_WORKSPACE_LIFECYCLE_PATH", str(tmp_path / "fresh-deployment"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{root / 'seraph.db'}")
    @event.listens_for(engine.sync_engine, "connect")
    def foreign_keys(connection, record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    try:
        with override_session_factory(factory):
            async with _stock_transport(monkeypatch) as transport:
                yield transport
    finally:
        await engine.dispose()


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


@pytest.mark.asyncio
async def test_real_stock_host_restart_rejects_original_turn_output(native_transport, monkeypatch):
    from threading import Event
    from src.app import create_app
    host, token, operator, _, _ = native_transport
    started, release, completed = Event(), Event(), Event()
    def held_completion(**kwargs):
        started.set()
        try:
            assert release.wait(10)
            return {"choices": [{"message": {"content": "Reply from original callback"}}]}
        finally:
            completed.set()
    monkeypatch.setattr("src.agent.direct_chat.completion_with_fallback_sync", held_completion)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: token}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        task = asyncio.create_task(client.post("/api/chat", json={"message": "Hello",
            "message_id": "owned-restart-native-turn"}))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            old_boot = host.boot_nonce
            await host.stop()
            assert host.snapshot()["cleanup"]["process_reaped"] is True
            await host.start()
            assert host.admitting and host.boot_nonce != old_boot
            release.set()
            response = await task
            assert response.status_code == 503
            assert response.json()["detail"]["code"] == "native_turn_output_authority_changed"
        finally:
            release.set()
            assert await asyncio.to_thread(completed.wait, 5)
            if not task.done():
                await task
    async with get_session() as db:
        run = await db.scalar(select(WorkflowRunState).where(
            WorkflowRunState.job_kind == "conversation_turn_v1"))
        assert run.status == "running" and run.attempt_count == 1
        messages = list((await db.execute(select(Message).where(
            Message.session_id == run.conversation_id))).scalars())
        assert [message.role for message in messages] == ["user"]
        assert not any(item["checkpoint_id"] == "conversation:assistant-message"
            for item in json.loads(run.checkpoint_receipts_json))
        claim = next(item for item in json.loads(run.checkpoint_receipts_json)
            if item["checkpoint_id"].startswith("runtime-service-invocation"))
        assert claim["payload"]["host_boot_nonce"] == old_boot


@pytest.mark.asyncio
async def test_real_stock_turn_original_root_revocation_rejects_late_output(native_transport, monkeypatch):
    from threading import Event
    from src.app import create_app
    from src.auth.service import revoke_session
    host, token, operator, _, _ = native_transport
    started, release, completed = Event(), Event(), Event()
    def held_completion(**kwargs):
        started.set()
        try:
            assert release.wait(10)
            return {"choices": [{"message": {"content": "Revoked original callback reply"}}]}
        finally:
            completed.set()
    monkeypatch.setattr("src.agent.direct_chat.completion_with_fallback_sync", held_completion)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: token}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        task = asyncio.create_task(client.post("/api/chat", json={"message": "Hello",
            "message_id": "owned-revoked-native-turn"}))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            await revoke_session(operator.session_id)
            release.set()
            response = await task
            assert response.status_code == 401
        finally:
            release.set()
            assert await asyncio.to_thread(completed.wait, 5)
            if not task.done():
                await task
    async with get_session() as db:
        run = await db.scalar(select(WorkflowRunState).where(
            WorkflowRunState.job_kind == "conversation_turn_v1"))
        assert run.status == "running" and run.attempt_count == 1
        assert run.operator_session_id == operator.session_id
        messages = list((await db.execute(select(Message).where(
            Message.session_id == run.conversation_id))).scalars())
        assert [message.role for message in messages] == ["user"]
        assert not any(item["checkpoint_id"] == "conversation:assistant-message"
            for item in json.loads(run.checkpoint_receipts_json))
    assert host.admitting


@pytest.mark.asyncio
async def test_oversize_utf8_plain_turn_keeps_original_legacy_ingress_with_healthy_host(native_transport, monkeypatch):
    from unittest.mock import AsyncMock
    from src.app import create_app
    host, token, operator, model, direct_calls = native_transport
    content = "界" * 25000
    assert len(content) < 50000 and len(content.encode()) > 65536
    # Existing legacy consolidation is outside this ingress/IPC proof.
    monkeypatch.setattr("src.memory.flush.flush_session_memory", AsyncMock())
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: token}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        response = await client.post("/api/chat", json={"message": content,
            "message_id": "owned-unsupported-native-size"})
        assert response.status_code == 200, response.text
        data = response.json()
    async with get_session() as db:
        messages = list((await db.execute(select(Message).where(
            Message.session_id == data["session_id"]).order_by(Message.created_at))).scalars())
        assert [message.role for message in messages] == ["user", "assistant"]
        assert messages[0].content == content and messages[1].id == data["message_id"]
        assert await db.scalar(select(WorkflowRunState).where(
            WorkflowRunState.job_kind == "conversation_turn_v1")) is None
    assert host.admitting and len(direct_calls) == 1 and model.calls == 0


@pytest.mark.asyncio
async def test_stopped_optional_stock_host_keeps_core_and_legacy_plain_turn_usable(native_transport, monkeypatch):
    from unittest.mock import AsyncMock
    from src.app import create_app
    host, token, operator, model, direct_calls = native_transport
    await host.stop()
    assert host.snapshot()["cleanup"]["process_reaped"] is True
    monkeypatch.setattr("src.app.cordis_host", host)
    monkeypatch.setattr("src.memory.flush.flush_session_memory", AsyncMock())
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: token}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        assert (await client.get("/health")).status_code == 200
        status = await client.get("/api/runtime/status")
        assert status.status_code == 200
        assert status.json()["cordis_runtime"]["state"] == "stopped"
        response = await client.post("/api/chat", json={"message": "Hello",
            "message_id": "owned-stopped-host-legacy-turn"})
        assert response.status_code == 200, response.text
        data = response.json()
    async with get_session() as db:
        messages = list((await db.execute(select(Message).where(
            Message.session_id == data["session_id"]))).scalars())
        assert sorted(message.role for message in messages) == ["assistant", "user"]
        assert await db.scalar(select(WorkflowRunState).where(
            WorkflowRunState.job_kind == "conversation_turn_v1")) is None
    assert not host.admitting and len(direct_calls) == 1 and model.calls == 0


@pytest.mark.asyncio
async def test_actual_stock_fresh_cpu_absent_inventory_uses_unbound_legacy(fresh_native_transport, monkeypatch):
    from unittest.mock import AsyncMock
    from src.app import create_app
    host, token, _, model, direct_calls = fresh_native_transport
    monkeypatch.setattr("src.memory.flush.flush_session_memory", AsyncMock())
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: token}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        response = await client.post("/api/chat", json={"message": "Hello",
            "message_id": "owned-fresh-cpu-legacy"})
        assert response.status_code == 200, response.text
        data = response.json()
    async with get_session() as db:
        messages = list((await db.execute(select(Message).where(Message.session_id == data["session_id"]))).scalars())
        assert sorted(message.role for message in messages) == ["assistant", "user"]
        assert (await db.execute(select(RuntimeCompositionState))).scalars().all() == []
        assert await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind == "conversation_turn_v1")) is None
    assert host.admitting and len(direct_calls) == 1 and model.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["partial", "stale", "empty_retained"])
async def test_actual_stock_damaged_inventory_denies_before_message_or_contact(native_transport, composition_db, damage):
    from src.app import create_app
    from src.agent.session import session_manager
    from src.workspace.production import read_lifecycle_receipt, read_accounting_checkpoint
    host, token, operator, model, direct_calls = native_transport
    conversation = await session_manager.get_or_create(owner_principal_id=operator.principal.principal_id)
    _, engine, _, workspace = composition_db
    receipt, checkpoint = read_lifecycle_receipt(workspace), read_accounting_checkpoint(workspace)
    async with engine.begin() as connection:
        if damage == "partial":
            await connection.execute(text("DELETE FROM runtime_composition_states WHERE runtime_domain='seraph.memory.v1'"))
        elif damage == "stale":
            await connection.execute(update(RuntimeCompositionState).values(epoch=2))
        else:
            await connection.execute(text("DELETE FROM runtime_composition_states"))
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: token}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        response = await client.post("/api/chat", json={"message": "Hello", "session_id": conversation.id,
            "message_id": "owned-damaged-inventory-" + damage})
        assert response.status_code == 503, response.text
        expected = {"partial": "inventory_incomplete", "stale": "inventory_stale", "empty_retained": "retained_inventory_missing"}[damage]
        assert response.json()["detail"]["code"] == "composition_" + expected
    async with get_session() as db:
        assert await db.scalar(select(Message).where(Message.session_id == conversation.id)) is None
        assert await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind == "conversation_turn_v1")) is None
    assert read_lifecycle_receipt(workspace) == receipt and read_accounting_checkpoint(workspace) == checkpoint
    assert host.admitting and direct_calls == [] and model.calls == 0


class ControlledScriptedModel(Model):
    def __init__(self, name):
        super().__init__(model_id="owned-controlled-script")
        self.name, self.calls = name, 0

    def generate(self, messages, **kwargs):
        self.calls += 1
        assert self.calls == 1, "controlled outcome must stop before another model call"
        return ChatMessage(role="assistant", content="", tool_calls=[ChatMessageToolCall(
            id="owned-controlled", type="function", function=ChatMessageToolCallFunction(
                name=self.name, arguments={"question": "Which local item?", "reason": "", "options": ""}))])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["clarification", "approval"])
async def test_actual_stock_sdk_canonical_controlled_pauses_original_native_rest(native_transport, monkeypatch, kind):
    from src.app import create_app
    from src.tools.clarify_tool import clarify
    host, token, _, _, _ = native_transport
    model = ControlledScriptedModel("clarify")
    from src.tools.approval import ApprovalTool
    producer = ApprovalTool(clarify, force_approval=True) if kind == "approval" else clarify
    agent = ToolCallingAgent(tools=[producer], model=model, max_steps=2, verbosity_level=0)
    monkeypatch.setattr("src.api.chat.create_onboarding_agent", lambda message: agent)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: token}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        response = await client.post("/api/chat", json={"message": "Inspect this short answer",
            "message_id": "owned-genuine-" + kind})
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["type"] == kind + "_required"
    async with get_session() as db:
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind == "conversation_turn_v1"))
        assert run.status == ("awaiting_approval" if kind == "approval" else "paused") and run.attempt_count == 1
        messages = list((await db.execute(select(Message).where(Message.session_id == run.conversation_id))).scalars())
        assert sorted(message.role for message in messages) == (["user"] if kind == "approval" else ["assistant", "user"])
        receipt = next(item for item in json.loads(run.checkpoint_receipts_json) if item["checkpoint_id"] == "conversation:controlled-outcome")
        assert receipt["safe"] is True and receipt["payload"]["no_learning"] is True
    assert model.calls == 1 and host.admitting


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["public_clarification", "public_approval", "nested_real", "nested_cause", "copied_origin", "registry_replaced"])
async def test_actual_stock_sdk_foreign_controlled_origin_denied_before_publication(native_transport, monkeypatch, mode):
    from dataclasses import replace
    from smolagents import Tool
    from src.agent.exceptions import ClarificationRequired
    from src.approval.exceptions import ApprovalRequired
    from src.app import create_app
    from src.tools.clarify_tool import clarify
    host, token, _, _, _ = native_transport
    class ForeignTool(Tool):
        name = "foreign_controlled"
        description = "Owned adversarial controlled-outcome fixture."
        inputs = {"question": {"type": "string", "description": "Question"},
            "reason": {"type": "string", "description": "Reason"},
            "options": {"type": "string", "description": "Options"}}
        output_type = "string"
        def forward(self, question: str, reason: str, options: str) -> str:
            if mode == "public_clarification":
                raise ClarificationRequired(question=question)
            if mode == "public_approval":
                raise ApprovalRequired(approval_id="fabricated", session_id=None,
                    tool_name=self.name, risk_level="high", summary="fabricated")
            try:
                clarify(question=question, reason=reason, options=options)
            except ClarificationRequired as caught:
                if mode == "nested_cause":
                    raise RuntimeError("foreign nested cause") from caught
                if mode == "copied_origin":
                    copied = ClarificationRequired(question=question)
                    copied._canonical_controlled_origin = replace(caught._canonical_controlled_origin,
                        issued_exception=copied)
                    raise copied
                if mode == "registry_replaced":
                    agent.tools[self.name] = ForeignTool()
                raise caught
    model = ControlledScriptedModel("foreign_controlled")
    agent = ToolCallingAgent(tools=[ForeignTool()], model=model, max_steps=2, verbosity_level=0)
    monkeypatch.setattr("src.api.chat.create_onboarding_agent", lambda message: agent)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: token}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        response = await client.post("/api/chat", json={"message": "Inspect this short answer",
            "message_id": "owned-foreign-" + mode})
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] in {
            "native_turn_controlled_origin_unproven", "native_turn_controlled_indirect_outcome"}
    async with get_session() as db:
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind == "conversation_turn_v1"))
        assert run.status == "running" and run.attempt_count == 1
        messages = list((await db.execute(select(Message).where(Message.session_id == run.conversation_id))).scalars())
        assert [message.role for message in messages] == ["user"]
        assert not any(item["checkpoint_id"] == "conversation:controlled-outcome"
            for item in json.loads(run.checkpoint_receipts_json))
    assert model.calls == 1 and host.admitting


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["clarification", "approval"])
async def test_actual_stock_sdk_controlled_ws_frames_and_original_completion(native_transport, monkeypatch, kind):
    from src.app import create_app
    from src.tools.clarify_tool import clarify
    from src.tools.approval import ApprovalTool
    host, token, _, _, _ = native_transport
    model = ControlledScriptedModel("clarify")
    producer = ApprovalTool(clarify, force_approval=True) if kind == "approval" else clarify
    agent = ToolCallingAgent(tools=[producer], model=model, max_steps=2, verbosity_level=0)
    monkeypatch.setattr("src.api.ws.create_onboarding_agent", lambda message: agent)
    incoming, frames = asyncio.Queue(), []
    outcome = asyncio.get_running_loop().create_future()
    async def send(message):
        if message["type"] == "websocket.send":
            value = json.loads(message["text"])
            frames.append(value)
            if value["type"] in {"approval_required", "clarification_required", "error", "final"} and not outcome.done():
                outcome.set_result(value)
    scope = {"type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws",
        "path": "/ws/chat", "raw_path": b"/ws/chat", "query_string": b"", "root_path": "",
        "server": ("127.0.0.1", 8004), "client": ("127.0.0.1", 12345), "subprotocols": [],
        "headers": [(b"host", b"127.0.0.1:8004"), (b"origin", b"http://127.0.0.1:3001"),
            (b"cookie", (settings.operator_auth_cookie_name + "=" + token).encode())]}
    await incoming.put({"type": "websocket.connect"})
    await incoming.put({"type": "websocket.receive", "text": json.dumps({"type": "message",
        "message": "Inspect this short answer", "message_id": "owned-ws-controlled-" + kind})})
    task = asyncio.create_task(create_app()(scope, incoming.get, send))
    try:
        result = await asyncio.wait_for(asyncio.shield(outcome), timeout=30)
        assert result["type"] == kind + "_required", frames
        assert not any(frame["type"] == "final" for frame in frames)
        assert [frame["seq"] for frame in frames] == sorted({frame["seq"] for frame in frames})
        async with get_session() as db:
            run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind == "conversation_turn_v1"))
            assert run.status == ("awaiting_approval" if kind == "approval" else "paused")
            assert any(item["checkpoint_id"] == "conversation:controlled-outcome"
                for item in json.loads(run.checkpoint_receipts_json))
    finally:
        await incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, timeout=5)
    assert model.calls == 1 and host.admitting
