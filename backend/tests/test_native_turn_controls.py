"""Authenticated real stock-native turn controls; original producers are retained."""
import asyncio
import json
from threading import Event
from time import monotonic

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from smolagents import ToolCallingAgent

from config.settings import settings
from src.db.engine import get_session
from src.db.models import Message, WorkflowRunState
from src.agent.native_turn_controls import NativeTurnResourceOwner, CANCEL_ID, CLOSURE_ID
from tests.test_native_turn_transport import composition_db, native_transport, ScriptedModel, ControlledScriptedModel


def actual_app():
    from src.app import create_app
    app = create_app()
    app.state.native_turn_resources = NativeTurnResourceOwner()
    return app


async def held_until(started):
    # The real worker signals once; finite test wait is not runtime polling.
    assert await asyncio.to_thread(started.wait, 10)


async def current_turn():
    async with get_session() as db:
        rows = list((await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.job_kind == "conversation_turn_v1"))).scalars())
        assert len(rows) == 1
        row = rows[0]
        return {"job_id": row.run_identity, "revision": row.revision, "fence": row.fencing_token,
            "deadline": row.deadline_at, "status": row.status, "root": row.operator_session_id,
            "conversation": row.conversation_id, "checkpoints": row.checkpoint_receipts_json,
            "effects": row.effect_receipts_json, "artifacts": row.artifact_receipts_json,
            "fingerprint": row.run_fingerprint}


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,full_capacity", [("conversation.cancel", "conversation/cancel", False),
    ("agent-loop.cancelTurn", "agent-loop/cancel-turn", False), ("conversation.cancel", "conversation/cancel", True)])
async def test_actual_rest_original_held_callback_cancel_and_exact_replay(native_transport, monkeypatch, method, path, full_capacity):
    entered, release = Event(), Event()
    from smolagents.models import ChatMessage, ChatMessageToolCall, ChatMessageToolCallFunction
    from tests.test_native_turn_transport import _scripted_accounted_result
    class HeldModel(ScriptedModel):
        def generate(self, messages, **kwargs):
            self.calls += 1
            entered.set()
            assert release.wait(15)
            return _scripted_accounted_result(ChatMessage(role="assistant", content="", tool_calls=[ChatMessageToolCall(
                id="owned-held-final", type="function", function=ChatMessageToolCallFunction(
                    name="final_answer", arguments={"answer": "Scripted native reply"}))]))
    model = HeldModel()
    agent = ToolCallingAgent(tools=[], model=model, max_steps=1, verbosity_level=0)
    monkeypatch.setattr("src.api.chat.create_onboarding_agent", lambda message: agent)
    app = actual_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: native_transport[1]},
        headers={"origin": "http://127.0.0.1:3001"}) as client:
        task = asyncio.create_task(client.post("/api/chat", json={"message": "Inspect this short answer", "message_id": "owned-held-" + path.replace('/', '-')}))
        try:
            await held_until(entered)
            before = await current_turn()
            resource = app.state.native_turn_resources._entries[before["job_id"]]
            assert resource.worker is resource.execution.worker and not resource.worker.done()
            if not full_capacity:
                from copy import copy
                original_claim = resource.execution.claim
                resource.execution.claim = copy(original_claim)
                try:
                    copied = await client.post("/api/chat/native-turns/" + before["job_id"] + "/conversation/read",
                        json={"conversation_ref": before["conversation"], "limit": 1, "before_message_ref": None})
                    assert copied.status_code == 409 and await current_turn() == before
                finally:
                    resource.execution.claim = original_claim
            if full_capacity:
                from src.workflows.job_runtime import durable_job_repository as jobs, DurableJobError
                from src.workspace.production import ProductionWorkspaceReconciliationError
                witness = resource.execution.scope.witness
                for index in range(45):
                    await jobs.record_checkpoint(before["job_id"], checkpoint_id=f"owned-existing:{index}",
                        state={"owned": index}, owner=witness["lease_owner"], fencing_token=before["fence"])
                before = await current_turn()
                assert len(json.loads(before["checkpoints"])) == 49  # Exactly50 including reserved original output slot.
                with pytest.raises((ProductionWorkspaceReconciliationError, ValueError)):
                    await jobs.record_checkpoint(before["job_id"], checkpoint_id="owned-over-capacity",
                        state={"owned": 46}, owner=witness["lease_owner"], fencing_token=before["fence"])
                with pytest.raises(DurableJobError):
                    await jobs.record_checkpoint(before["job_id"], checkpoint_id=CANCEL_ID,
                        state={"physically_closed": True}, owner=witness["lease_owner"], fencing_token=before["fence"])
                assert await current_turn() == before
            base = "/api/chat/native-turns/" + before["job_id"] + "/"
            read = await client.post(base + "conversation/read", json={"conversation_ref": before["conversation"], "limit": 1, "before_message_ref": None})
            assert read.status_code == 200, read.text
            assert read.json()["status"] == "succeeded" and read.json()["memory_status"] == "no_learning"
            assert [row["role"] for row in read.json()["value"]["messages"]] == ["user"]
            body = {"turn_ref": before["job_id"], "expected_revision": before["revision"]}
            started = monotonic()
            cancel = await client.post(base + path, json=body)
            assert cancel.status_code == 200 and cancel.json()["status"] == "succeeded", cancel.text
            assert monotonic() - started < 5 and not resource.worker.done()
            cancelled = await current_turn()
            assert cancelled["status"] == "unknown_external_effect"
            assert cancelled["fence"] == before["fence"] + 1
            assert cancelled["deadline"] == before["deadline"] and cancelled["root"] == before["root"]
            assert cancelled["fingerprint"] == before["fingerprint"]
            history = json.loads(cancelled["checkpoints"])
            receipt = next(row for row in history if row["checkpoint_id"] == CANCEL_ID)
            assert receipt["payload"]["control_method"] == method and receipt["payload"]["no_learning"] is True
            first_purpose = resource.purpose
            repeat = await client.post(base + path, json=body)
            assert repeat.json() == cancel.json()
            assert await current_turn() == cancelled and resource.purpose is first_purpose
            other = "agent-loop/cancel-turn" if path == "conversation/cancel" else "conversation/cancel"
            changed = await client.post(base + other, json=body)
            assert changed.status_code == 409 and await current_turn() == cancelled
            read_after = await client.post(base + "conversation/read", json={"conversation_ref": before["conversation"], "limit": 1, "before_message_ref": None})
            assert read_after.status_code == 409 or read_after.json()["status"] == "blocked"
            if method == "conversation.cancel" and not full_capacity:
                await asyncio.sleep(5.1)  # Actual immutable first-action expiry, no reconstructed clock.
                expired = await client.post(base + path, json=body)
                assert expired.status_code == 409 and resource.purpose is first_purpose
                assert await current_turn() == cancelled
        finally:
            release.set()
            response = await asyncio.wait_for(task, 15)
        assert response.status_code == 504, response.text
        assert resource.worker.done() and not resource.worker.cancelled()
        assert (await current_turn())["status"] == "unknown_external_effect"
        async with get_session() as db:
            messages = list((await db.execute(select(Message))).scalars())
            assert [row.role for row in messages] == ["user"]
    await app.state.native_turn_resources.shutdown()


@pytest.mark.asyncio
async def test_actual_websocket_executor_future_retained_before_callback_and_rest_cancel(native_transport, monkeypatch):
    from smolagents.models import ChatMessage, ChatMessageToolCall, ChatMessageToolCallFunction
    from tests.test_native_turn_transport import _scripted_accounted_result
    entered, release = Event(), Event()
    executions = []
    from src.agent.turn_execution import NativeTurnExecution
    original_register = NativeTurnExecution.register_worker
    def register_actual_future(execution, worker):
        assert not entered.is_set()
        executions.append((execution, worker))
        return original_register(execution, worker)
    monkeypatch.setattr(NativeTurnExecution, "register_worker", register_actual_future)
    class HeldWSModel(ScriptedModel):
        def generate(self, messages, **kwargs):
            self.calls += 1
            entered.set()
            assert release.wait(15)
            return _scripted_accounted_result(ChatMessage(role="assistant", content="", tool_calls=[ChatMessageToolCall(
                id="owned-ws-held-final", type="function", function=ChatMessageToolCallFunction(
                    name="final_answer", arguments={"answer": "Scripted native reply"}))]))
    model = HeldWSModel()
    agent = ToolCallingAgent(tools=[], model=model, max_steps=1, verbosity_level=0)
    monkeypatch.setattr("src.api.ws.create_onboarding_agent", lambda message: agent)
    app = actual_app()
    incoming = asyncio.Queue()
    frames = []
    async def send(message):
        if message["type"] == "websocket.send":
            frames.append(json.loads(message["text"]))
    scope = {"type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws", "path": "/ws/chat",
        "raw_path": b"/ws/chat", "query_string": b"", "root_path": "", "server": ("127.0.0.1", 8004),
        "client": ("127.0.0.1", 12345), "headers": [(b"host", b"127.0.0.1:8004"),
            (b"origin", b"http://127.0.0.1:3001"), (b"cookie", (settings.operator_auth_cookie_name + "=" + native_transport[1]).encode())], "subprotocols": []}
    await incoming.put({"type": "websocket.connect"})
    await incoming.put({"type": "websocket.receive", "text": json.dumps({"type": "message",
        "message": "Inspect this short answer", "message_id": "owned-controls-ws"})})
    task = asyncio.create_task(app(scope, incoming.get, send))
    try:
        await held_until(entered)
        before = await current_turn()
        resource = app.state.native_turn_resources._entries[before["job_id"]]
        assert len(executions) == 1 and executions[0] == (resource.execution, resource.worker)
        assert not resource.worker.done()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8004",
            cookies={settings.operator_auth_cookie_name: native_transport[1]}, headers={"origin": "http://127.0.0.1:3001"}) as client:
            result = await client.post("/api/chat/native-turns/" + before["job_id"] + "/agent-loop/cancel-turn",
                json={"turn_ref": before["job_id"], "expected_revision": before["revision"]})
            assert result.status_code == 200 and result.json()["status"] == "succeeded", result.text
            assert not resource.worker.done() and (await current_turn())["fence"] == before["fence"] + 1
    finally:
        release.set()
        await incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, 15)
    assert resource.worker.done() and not resource.worker.cancelled()
    assert (await current_turn())["status"] == "unknown_external_effect"
    assert not any(frame.get("type") == "final" for frame in frames)
    async with get_session() as db:
        assert [row.role for row in (await db.execute(select(Message))).scalars()] == ["user"]
    await app.state.native_turn_resources.shutdown()


@pytest.mark.asyncio
async def test_actual_same_session_pages_and_closed_private_content_bounds(native_transport, monkeypatch):
    from smolagents.models import ChatMessage, ChatMessageToolCall, ChatMessageToolCallFunction
    from tests.test_native_turn_transport import _scripted_accounted_result
    entered, release = Event(), Event()
    app = actual_app()
    monkeypatch.setattr("src.api.chat.create_onboarding_agent", lambda message:
        ToolCallingAgent(tools=[], model=ScriptedModel(), max_steps=1, verbosity_level=0))
    oversized = "Inspect " + "é" * 4096
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: native_transport[1]}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        first = await client.post("/api/chat", json={"message": oversized, "message_id": "owned-page-first"})
        assert first.status_code == 200, first.text
        conversation = first.json()["session_id"]
        second = await client.post("/api/chat", json={"message": "Inspect second answer", "session_id": conversation, "message_id": "owned-page-second"})
        assert second.status_code == 200, second.text
        class HeldPageModel(ScriptedModel):
            def generate(self, messages, **kwargs):
                self.calls += 1
                entered.set()
                assert release.wait(15)
                return _scripted_accounted_result(ChatMessage(role="assistant", content="", tool_calls=[ChatMessageToolCall(
                    id="owned-page-final", type="function", function=ChatMessageToolCallFunction(
                        name="final_answer", arguments={"answer": "Scripted native reply"}))]))
        model = HeldPageModel()
        agent = ToolCallingAgent(tools=[], model=model, max_steps=1, verbosity_level=0)
        monkeypatch.setattr("src.api.chat.create_onboarding_agent", lambda message: agent)
        task = asyncio.create_task(client.post("/api/chat", json={"message": "Inspect third answer", "session_id": conversation, "message_id": "owned-page-third"}))
        try:
            await held_until(entered)
            resources = app.state.native_turn_resources._entries
            assert len(resources) == 1
            turn_ref = next(iter(resources))
            endpoint = "/api/chat/native-turns/" + turn_ref + "/conversation/read"
            body = {"conversation_ref": conversation, "limit": 2, "before_message_ref": None}
            page = await client.post(endpoint, json=body)
            assert page.json()["status"] == "succeeded", page.text
            value = page.json()["value"]
            assert [row["role"] for row in value["messages"]] == ["assistant", "user"]
            next_page = await client.post(endpoint, json={**body, "before_message_ref": value["next_cursor"]})
            assert next_page.json()["status"] == "succeeded", next_page.text
            previous = next_page.json()["value"]
            assert [row["role"] for row in previous["messages"]] == ["assistant", "user"]
            assert not {row["message_ref"] for row in value["messages"]}.intersection(row["message_ref"] for row in previous["messages"])
            denied = await client.post(endpoint, json={**body, "before_message_ref": previous["next_cursor"]})
            assert denied.status_code == 409 and oversized not in denied.text, denied.text
            for invalid in ({**body, "limit": 101}, {**body, "raw_query": "select *"},
                {**body, "conversation_ref": native_transport[2].session_id},
                {**body, "before_message_ref": native_transport[2].session_id}):
                blocked = await client.post(endpoint, json=invalid)
                assert blocked.status_code == 409 or blocked.json()["status"] == "blocked"
                assert oversized not in blocked.text
            assert model.calls == 1
        finally:
            release.set()
            response = await asyncio.wait_for(task, 15)
        assert response.status_code == 200, response.text
    await app.state.native_turn_resources.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["clarification", "approval"])
async def test_actual_controlled_original_producer_cancel_and_restart_missing_handle(native_transport, monkeypatch, kind):
    from src.tools.clarify_tool import clarify
    from src.tools.approval import ApprovalTool
    producer = ApprovalTool(clarify, force_approval=True) if kind == "approval" else clarify
    model = ControlledScriptedModel("clarify")
    agent = ToolCallingAgent(tools=[producer], model=model, max_steps=2, verbosity_level=0)
    monkeypatch.setattr("src.api.chat.create_onboarding_agent", lambda message: agent)
    app = actual_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: native_transport[1]}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        response = await client.post("/api/chat", json={"message": "Inspect this short answer", "message_id": "owned-controlled-controls"})
        assert response.status_code == 409, response.text
        before = await current_turn()
        assert before["status"] == ("awaiting_approval" if kind == "approval" else "paused")
        resource = app.state.native_turn_resources._entries[before["job_id"]]
        assert resource.worker.done() and resource.execution.worker is resource.worker
        path = "/api/chat/native-turns/" + before["job_id"] + "/conversation/cancel"
        body = {"turn_ref": before["job_id"], "expected_revision": before["revision"]}
        result = await client.post(path, json=body)
        assert result.status_code == 200 and result.json()["status"] == "succeeded", result.text
        final = await current_turn()
        assert final["status"] == "cancelled" and final["deadline"] == before["deadline"]
        assert model.calls == 1
        if kind == "approval":
            from src.approval.repository import approval_repository
            original_approval = await approval_repository.get(response.json()["detail"]["approval_id"])
            assert original_approval.status == "pending"
        fresh_app = actual_app()
        async with AsyncClient(transport=ASGITransport(app=fresh_app), base_url="http://127.0.0.1:8004",
            cookies={settings.operator_auth_cookie_name: native_transport[1]}, headers={"origin": "http://127.0.0.1:3001"}) as restarted:
            blocked = await restarted.post(path, json=body)
            assert blocked.status_code == 409 and await current_turn() == final
        await fresh_app.state.native_turn_resources.shutdown()
    await app.state.native_turn_resources.shutdown()


@pytest.mark.asyncio
async def test_actual_returned_accounted_future_cancel_before_output_adoption(native_transport, monkeypatch):
    from src.api import chat
    entered, release = asyncio.Event(), asyncio.Event()
    original = chat.persist_turn_output
    async def actual_output_barrier(native_turn, *args, **kwargs):
        assert native_turn.worker.done() and not native_turn.worker.cancelled()
        entered.set()
        await release.wait()
        return await original(native_turn, *args, **kwargs)
    monkeypatch.setattr(chat, "persist_turn_output", actual_output_barrier)
    app = actual_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: native_transport[1]}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        task = asyncio.create_task(client.post("/api/chat", json={"message": "Inspect this short answer", "message_id": "owned-output-barrier"}))
        try:
            await asyncio.wait_for(entered.wait(), 15)
            before = await current_turn()
            resource = app.state.native_turn_resources._entries[before["job_id"]]
            endpoint = "/api/chat/native-turns/" + before["job_id"] + "/conversation/cancel"
            body = {"turn_ref": before["job_id"], "expected_revision": before["revision"]}
            result = await client.post(endpoint, json=body)
            assert result.status_code == 200 and result.json()["status"] == "succeeded", result.text
            final = await current_turn()
            assert final["status"] == "cancelled"
            assert final["fence"] == before["fence"] + 1 and final["deadline"] == before["deadline"]
            closure = next(row["payload"] for row in json.loads(final["checkpoints"]) if row["checkpoint_id"] == CLOSURE_ID)
            assert closure["physically_closed"] is True and closure["no_learning"] is True
            repeat = await client.post(endpoint, json=body)
            assert repeat.json()["value"]["state"] == "cancelled" and await current_turn() == final
            assert resource.worker is resource.execution.worker
        finally:
            release.set()
            response = await asyncio.wait_for(task, 15)
        assert response.status_code == 503, response.text
        async with get_session() as db:
            assert [row.role for row in (await db.execute(select(Message))).scalars()] == ["user"]
    await app.state.native_turn_resources.shutdown()


@pytest.mark.asyncio
async def test_actual_32_pending_ingresses_reject_before_message_job_claim_or_callback(native_transport, monkeypatch):
    from src.agent.session import session_manager
    from src.agent.turn_execution import NativeTurnBlocked
    session = await session_manager.get_or_create(owner_principal_id=native_transport[2].principal.principal_id)
    entered, release = asyncio.Queue(), asyncio.Event()
    async def before_original_message_writer(*args, **kwargs):
        entered.put_nowait(kwargs["message_id"])
        await release.wait()
        raise NativeTurnBlocked("owned_preadmission_test_stop")
    monkeypatch.setattr(session_manager, "reserve_native_turn_message", before_original_message_writer)
    app = actual_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: native_transport[1]}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        tasks = []
        try:
            for index in range(32):
                tasks.append(asyncio.create_task(client.post("/api/chat", json={"message": "Inspect this short answer",
                    "session_id": session.id, "message_id": f"owned-reserved-ingress-{index}"})))
                await asyncio.wait_for(entered.get(), 10)
            assert len(app.state.native_turn_resources._entries) == 32
            denied = await client.post("/api/chat", json={"message": "Inspect this short answer",
                "session_id": session.id, "message_id": "owned-over-capacity-ingress"})
            assert denied.status_code == 503 and denied.json()["detail"]["code"] == "native_turn_resource_capacity"
            assert native_transport[3].calls == 0
            async with get_session() as db:
                assert list((await db.execute(select(Message))).scalars()) == []
                assert list((await db.execute(select(WorkflowRunState))).scalars()) == []
        finally:
            release.set()
            responses = await asyncio.wait_for(asyncio.gather(*tasks), 15)
        assert all(response.status_code == 503 for response in responses)
        assert app.state.native_turn_resources._entries == {}
    await app.state.native_turn_resources.shutdown()
