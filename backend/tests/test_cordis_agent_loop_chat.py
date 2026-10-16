"""#1019 parity: the real REST/WS chat routes on the legacy loop and the Cordis loop.

Each case runs one scripted local model (fixed in-memory responses, no network
or inference) through both loops in fresh temporary workspaces, with real Auth,
SQLite, the wrapped tool surface and the real reviewed Cordis child, then
compares tool effects, transcript rows and WS frames. Only the loop-naming
status text may differ.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, asynccontextmanager, contextmanager
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel
from starlette.testclient import TestClient

from config.settings import settings
from tests.cordis_agent_loop import ScriptedModel, call, cutover_agent_loop, deny_sockets, final, text, tools
from tests.cordis_research import NODE, needs_cordis, seed_composition_inventory
from tests.test_research_native_vertical import real_auth  # noqa: F401

pytestmark = [needs_cordis]
SECRET = "VAULT_SECRET_CANARY_1019_value"
ORIGIN = {"origin": "http://localhost:3001"}
TARGETS = ("src.workflows.job_runtime.get_session", "src.workflows.durable_state.get_session",
           "src.db.engine.get_session", "src.auth.service.get_session", "src.model_fabric.repository.get_session",
           "src.vault.repository.get_session", "src.audit.repository.get_session", "src.goals.repository.get_session",
           "src.agent.session.get_session", "src.approval.repository.get_session", "src.api.ws.get_session",
           "src.memory.repository.get_session", "src.profile.service.get_db", "src.api.profile.get_db")


class Env:
    def __init__(self, client, host, model, workspace):
        self.client, self.host, self.model, self.workspace = client, host, model, workspace

    def run(self, coroutine):
        return self.client.portal.call(lambda: coroutine)

    def messages(self, session_id):
        from src.agent.session import session_manager
        rows = self.client.portal.call(session_manager.get_messages, session_id)
        return [(row["role"], row["content"]) for row in rows]

    def turns(self):
        from sqlalchemy import select
        from src.db.engine import get_session
        from src.db.models import WorkflowRunState
        from src.runtime_plugins.agent_turns import TURN_JOB_KIND

        async def read():
            async with get_session() as db:
                return [(row.status, row.failure_reason) for row in (await db.execute(select(WorkflowRunState).where(
                    WorkflowRunState.job_kind == TURN_JOB_KIND).order_by(WorkflowRunState.started_at))).scalars()]
        return self.client.portal.call(read)


@contextmanager
def chat_env(tmp_path, monkeypatch, script, *, loop: str, onboarded: bool = True):
    """A minimal real app (auth + REST chat + WS chat) whose async work all runs on the portal loop."""
    from src.api import auth, chat, ws
    from src.auth.middleware import OperatorAuthMiddleware
    from src.runtime_plugins import bridge
    from src.runtime_plugins.bridge import CordisHost
    from src.runtime_plugins.dispatch import NativeServiceDispatcher
    from src.workflows.job_runtime import DurableJobRepository
    # Starlette's WS test transport presents host ``testserver``.
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test,testserver,localhost,127.0.0.1")
    workspace = tmp_path / f"workspace-{loop}"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    monkeypatch.setenv("SERAPH_WORKSPACE_LIFECYCLE_PATH", str(tmp_path / f"lifecycle-{loop}"))
    from src.workspace.production import ProductionWorkspace, prepare_lifecycle_directory
    prepare_lifecycle_directory(ProductionWorkspace(host_root=workspace))
    engine = create_async_engine(f"sqlite+aiosqlite:///{workspace / 'seraph.db'}", connect_args={"timeout": 5})
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def sessions():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
    for target in TARGETS:
        monkeypatch.setattr(target, sessions)
    model = ScriptedModel(script)
    monkeypatch.setattr("src.agent.factory.get_model", lambda **_kwargs: model)
    monkeypatch.setattr("src.agent.onboarding.LiteLLMModel", lambda **_kwargs: model)
    monkeypatch.setattr("src.agent.onboarding.build_model_kwargs", lambda **_kwargs: {})

    async def no_guardian_state(**_kwargs):
        raise RuntimeError("guardian state is not part of the loop parity surface")
    monkeypatch.setattr("src.api.ws.build_guardian_state", no_guardian_state)
    monkeypatch.setattr("src.api.chat.build_guardian_state", no_guardian_state)
    monkeypatch.setattr("src.memory.flush.flush_session_memory", AsyncMock(return_value=None))
    deny_sockets(monkeypatch)
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(chat.router, prefix="/api")
    app.include_router(ws.router, prefix="/ws")
    jobs = DurableJobRepository()
    with ExitStack() as stack:
        client = stack.enter_context(TestClient(app))

        async def setup():
            async with engine.begin() as connection:
                await connection.run_sync(SQLModel.metadata.create_all)
            await seed_composition_inventory()
            host = CordisHost(node_path=Path(NODE), service_dispatch=NativeServiceDispatcher(jobs=jobs))
            assert await host.start(), host.snapshot()
            if onboarded:
                from src.api.profile import mark_onboarding_complete
                await mark_onboarding_complete()
            return host
        host = client.portal.call(setup)
        monkeypatch.setattr(bridge, "cordis_host", host)
        try:
            if loop == "cordis":
                client.portal.call(cutover_agent_loop, host)
            assert client.post("/api/auth/login", json={"password": "research-vertical-private-secret"},
                               headers=ORIGIN).status_code == 200
            yield Env(client, host, model, workspace)
        finally:
            client.portal.call(host.stop)
            client.portal.call(engine.dispose)


async def _receive_with_timeout(socket, timeout_seconds):
    import anyio
    with anyio.fail_after(timeout_seconds):
        message = await socket._send_rx.receive()
    socket._raise_on_close(message)
    return str(message["text"])


def receive(socket, timeout_seconds=20.0):
    return json.loads(socket.portal.call(_receive_with_timeout, socket, timeout_seconds))


def ws_turn(env, message, *, message_id="m-1", session_id=None, until=("final", "error", "approval_required",
                                                                       "clarification_required")):
    frames = []
    with env.client.websocket_connect("/ws/chat", headers=ORIGIN) as socket:
        socket.send_text(json.dumps({"type": "message", "message": message, "session_id": session_id,
                                     "message_id": message_id}))
        while True:
            frame = receive(socket)
            if frame["type"] == "proactive":
                continue
            frames.append(frame)
            if frame["type"] in until:
                break
    return frames


def shape(frames):
    """Frame identity for parity: type, content and step; loop-naming status text normalized."""
    result = []
    for frame in frames:
        content = frame.get("content")
        if frame["type"] == "status" and "agent loop" in str(content):
            content = "<loop status>"
        result.append((frame["type"], content, frame.get("step"), frame.get("reason")))
    return result


def both(tmp_path, monkeypatch, script_factory, drive, **kwargs):
    outcomes = {}
    for loop in ("legacy", "cordis"):
        with monkeypatch.context() as scoped, chat_env(tmp_path, scoped, script_factory(), loop=loop, **kwargs) as env:
            outcomes[loop] = drive(env)
    return outcomes["legacy"], outcomes["cordis"]


# ---------------------------------------------------------------- cases

def test_ws_tool_turn_parse_error_and_final_answer_match_legacy(tmp_path, monkeypatch, real_auth):  # noqa: F811
    def script():
        return [tools(call("read_file", {"file_path": "notes.txt"})), text("plain text without a tool call"),
                final("The notes say hello.")]

    def drive(env):
        (env.workspace / "notes.txt").write_text("hello from notes")
        frames = ws_turn(env, "what do my notes say?")
        session_id = frames[-1]["session_id"]
        return shape(frames), env.messages(session_id), env.turns(), len(env.model.calls)
    legacy, cordis = both(tmp_path, monkeypatch, script, drive)
    assert legacy[0] == cordis[0] and legacy[1] == cordis[1] and legacy[3] == cordis[3] == 3
    assert cordis[0][-1][:2] == ("final", "The notes say hello.")
    assert any(item[0] == "step" and "hello from notes" in str(item[1]) for item in cordis[0])
    assert legacy[2] == [] and cordis[2] == [("succeeded", None)]


def test_ws_status_names_the_actual_loop(tmp_path, monkeypatch, real_auth):  # noqa: F811
    def drive(env):
        return [frame["content"] for frame in ws_turn(env, "hi there, tell me a joke please now") if frame["type"] == "status"]
    legacy, cordis = both(tmp_path, monkeypatch, lambda: [final("ok")], drive)
    assert any("legacy agent loop (legacy.smolagents)" in item for item in legacy)
    assert any("Cordis agent loop (cordis.agent-loop.v1)" in item for item in cordis)


def test_ws_direct_chat_streams_deltas_then_settles_through_cordis(tmp_path, monkeypatch, real_auth):  # noqa: F811
    async def stream(*_args, **_kwargs):
        for part in ("Hello", " there", "!"):
            yield part

    def drive(env):
        with patch("src.api.ws.should_use_direct_local_chat", return_value=True), \
             patch("src.api.ws.direct_local_chat_route_error", new=AsyncMock(return_value=None)), \
             patch("src.api.ws.stream_direct_local_chat", stream):
            frames = ws_turn(env, "hi")
        return shape(frames), env.messages(frames[-1]["session_id"]), env.turns()
    legacy, cordis = both(tmp_path, monkeypatch, lambda: [], drive)
    assert legacy[0] == cordis[0] and legacy[1] == cordis[1]
    assert [item[0] for item in cordis[0] if item[0] in {"delta", "final"}] == ["delta", "delta", "delta", "final"]
    assert cordis[0][-1][1] == "Hello there!"
    assert cordis[2] == [("succeeded", None)]


def test_ws_duplicate_ingress_creates_one_turn(tmp_path, monkeypatch, real_auth):  # noqa: F811
    with chat_env(tmp_path, monkeypatch, [final("first"), final("never")], loop="cordis") as env:
        first = ws_turn(env, "summarize my week please")
        session_id = first[-1]["session_id"]
        again = ws_turn(env, "summarize my week please", session_id=session_id)
        assert again[-1]["type"] == "error" and again[-1]["reason"] == "chat_message_duplicate"
        assert env.turns() == [("succeeded", None)] and len(env.model.calls) == 1


def test_rest_tool_turn_matches_legacy_and_blocked_owner_never_falls_back(tmp_path, monkeypatch, real_auth):  # noqa: F811
    def script():
        return [tools(call("read_file", {"file_path": "notes.txt"})), final("REST says hello.")]

    def drive(env):
        (env.workspace / "notes.txt").write_text("hello from notes")
        response = env.client.post("/api/chat", json={"message": "what do my notes say?", "message_id": "r-1"},
                                   headers=ORIGIN)
        body = response.json()
        return response.status_code, body["response"], env.messages(body["session_id"]), env.turns()
    legacy, cordis = both(tmp_path, monkeypatch, script, drive)
    assert legacy[:3] == cordis[:3] and cordis[0] == 200 and cordis[1] == "REST says hello."
    assert cordis[3] == [("succeeded", None)]
    # A Cordis owner whose host is down blocks chat visibly with the fix; legacy is never used.
    with chat_env(tmp_path / "down", monkeypatch, [final("never")], loop="cordis") as env:
        env.client.portal.call(env.host.stop)
        response = env.client.post("/api/chat", json={"message": "what do my notes say?", "message_id": "r-2"},
                                   headers=ORIGIN)
        assert response.status_code == 503 and response.json()["detail"]["code"].startswith("cordis_")
        assert "Chat is unavailable" in response.json()["detail"]["message"] and env.model.calls == []


def test_ws_disconnect_cancels_the_turn_and_writes_one_fenced_interrupted_row(tmp_path, monkeypatch, real_auth):  # noqa: F811
    import threading
    gate, entered = threading.Event(), threading.Event()

    class Slow(ScriptedModel):
        def generate(self, *args, **kwargs):
            if len(self.calls) == 1:
                entered.set()
                gate.wait(10)
            return super().generate(*args, **kwargs)
    with chat_env(tmp_path, monkeypatch, [], loop="cordis") as env:
        model = Slow([tools(call("read_file", {"file_path": "notes.txt"})), final("never adopted")])
        monkeypatch.setattr("src.agent.factory.get_model", lambda **_kwargs: model)
        (env.workspace / "notes.txt").write_text("hello")
        with env.client.websocket_connect("/ws/chat", headers=ORIGIN) as socket:
            socket.send_text(json.dumps({"type": "message", "message": "read my notes and summarize", "message_id": "w-1"}))
            assert entered.wait(10)
            session_id = None
            while session_id is None:
                session_id = receive(socket).get("session_id")
        gate.set()

        import time
        for _ in range(200):
            if env.turns() and env.turns()[0][0] != "running":
                break
            time.sleep(0.05)
        assert env.turns() == [("blocked", "agent_turn_cancelled")]
        rows = env.messages(session_id)
        assert [role for role, _ in rows].count("assistant") == 1 and "never adopted" not in json.dumps(rows)
