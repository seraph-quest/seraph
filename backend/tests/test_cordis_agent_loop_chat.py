"""#1019 parity: the real REST/WS chat routes on the legacy loop and the Cordis loop.

Each case runs one scripted conversation through both loops in fresh
temporary workspaces, with real Auth, SQLite, the real wrapped ``Tool``
objects from the agent factories and the real reviewed Cordis child, then
compares tool effects, approval rows, transcript rows and WS frames. Only the
loop-naming status text may differ. By default the model is the real governed
``FallbackLiteLLMModel`` and only its OpenRouter HTTP client is scripted (an
``httpx.MockTransport``; no network, inference or spend), so tool export,
provider message cleaning, ``tool_choice`` and approval/clarification
propagation are the production ones (#1107).
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
from tests.cordis_agent_loop import (AGENT_LOOP, ScriptedModel, call, cutover_agent_loop, deny_sockets, final,
                                     governed_inference, text, tools)
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

    def tool_effects(self):
        from sqlalchemy import select
        from src.db.engine import get_session
        from src.db.models import WorkflowRunState
        from src.runtime_plugins.agent_turns import TURN_JOB_KIND

        async def read():
            async with get_session() as db:
                rows = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.job_kind == TURN_JOB_KIND))).scalars()
                return [((item.get("details") or {}).get("tool_name"), item.get("status")) for row in rows
                        for item in json.loads(row.effect_receipts_json or "[]") if item.get("effect_type") == "cordis_turn_tool"]
        return self.client.portal.call(read)

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
def chat_env(tmp_path, monkeypatch, script, *, loop: str, onboarded: bool = True, governed: bool = True):
    """A minimal real app (auth + approvals + REST chat + WS chat) whose async work all runs on the portal loop."""
    from src.api import approvals, auth, chat, ws
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
    if governed:
        model = governed_inference(monkeypatch, script)
    else:
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
    app.include_router(approvals.router, prefix="/api")
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


def ws_turn(env, message, *, message_id="m-1", session_id=None, kind="message",
            until=("final", "error", "approval_required", "clarification_required")):
    frames = []
    with env.client.websocket_connect("/ws/chat", headers=ORIGIN) as socket:
        payload = {"type": kind, "message": message, "session_id": session_id}
        if kind == "message":
            payload["message_id"] = message_id
        socket.send_text(json.dumps(payload))
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
        # The refusal came before the user row was reserved: a retry after the fix is not a duplicate.
        assert env.client.portal.call(env.host.start)
        retried = env.client.post("/api/chat", json={"message": "what do my notes say?", "message_id": "r-2"},
                                  headers=ORIGIN)
        assert retried.status_code == 200 and retried.json()["response"] == "never"
        assert env.turns() == [("succeeded", None)]


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


def test_ws_broken_direct_stream_keeps_honest_uncertainty_like_legacy(tmp_path, monkeypatch, real_auth):  # noqa: F811
    async def broken(*_args, **_kwargs):
        yield "Partial"
        raise RuntimeError("provider stream reset")

    def drive(env):
        with patch("src.api.ws.should_use_direct_local_chat", return_value=True), \
             patch("src.api.ws.direct_local_chat_route_error", new=AsyncMock(return_value=None)), \
             patch("src.api.ws.stream_direct_local_chat", broken):
            frames = ws_turn(env, "hi")
        return shape(frames), env.messages(frames[0]["session_id"]), env.turns()
    legacy, cordis = both(tmp_path, monkeypatch, lambda: [], drive)
    assert legacy[0] == cordis[0] and legacy[1] == cordis[1]
    assert cordis[0][-1][0] == "error" and "remote outcome is uncertain" in cordis[0][-1][1]
    # The committed user row stays; no assistant row is invented; the turn is honestly blocked.
    assert [role for role, _ in cordis[1]] == ["user"]
    assert cordis[2] == [("blocked", "provider_outcome_uncertain")]


def test_ws_approval_gate_approve_and_resume_match_legacy(tmp_path, monkeypatch, real_auth):  # noqa: F811
    """The real wrapped write_file pauses for approval, the operator approves, the resume runs it once."""
    def script():
        return [tools(call("write_file", {"file_path": "plan.txt", "content": "ship it"})),
                tools(call("write_file", {"file_path": "plan.txt", "content": "ship it"}, "call-2")),
                final("Wrote the plan.")]

    def drive(env):
        from src.approval.repository import approval_repository
        # The operator requires approval for write_file (tool approval behaviour "always").
        with patch("src.tools.approval.get_tool_approval_behavior", return_value="always"):
            paused = ws_turn(env, "write ship it into plan.txt")
            pause = paused[-1]
            session_id = pause["session_id"]
            pending = env.client.portal.call(lambda: approval_repository.list_pending(session_id=session_id))
            rows = [(item["tool_name"], item["risk_level"], item["status"]) for item in pending]
            written_before = (env.workspace / "plan.txt").exists()
            effects_before = env.tool_effects()
            approved = env.client.post(f"/api/approvals/{pause['approval_id']}/approve", headers=ORIGIN)
            assert approved.status_code == 200, approved.text
            resume = approved.json()["resume_message"]
            resumed = ws_turn(env, resume, session_id=session_id, kind="resume_message")
        consumed = env.client.portal.call(approval_repository.get, pause["approval_id"])
        return (shape(paused), shape(resumed), rows, written_before, (env.workspace / "plan.txt").read_text(),
                resume, consumed.status, env.messages(session_id), env.turns(), effects_before, env.tool_effects(),
                len(env.model.calls))
    legacy, cordis = both(tmp_path, monkeypatch, script, drive)
    assert legacy[:8] == cordis[:8] and legacy[11] == cordis[11] == 3
    paused, resumed, rows, written_before, content, resume, status = cordis[:7]
    assert paused[-1][0] == "approval_required" and "Approve it in chat" in paused[-1][1]
    assert rows == [("write_file", "medium", "pending")] and written_before is False
    assert resume == "write ship it into plan.txt" and resumed[-1][:2] == ("final", "Wrote the plan.")
    assert content == "ship it" and status != "pending"
    # The paused turn settled blocked with the gated call as no effect; the resume ran it once.
    assert cordis[8] == [("blocked", "approval_required"), ("succeeded", None)]
    assert cordis[9] == [("write_file", "no_effect")]
    assert sorted(cordis[10]) == [("write_file", "no_effect"), ("write_file", "succeeded")]


def test_rest_approval_gate_approve_and_resume_match_legacy(tmp_path, monkeypatch, real_auth):  # noqa: F811
    def script():
        return [tools(call("write_file", {"file_path": "plan.txt", "content": "ship it"})),
                tools(call("write_file", {"file_path": "plan.txt", "content": "ship it"}, "call-2")),
                final("Wrote the plan.")]

    def drive(env):
        with patch("src.tools.approval.get_tool_approval_behavior", return_value="always"):
            paused = env.client.post("/api/chat", json={"message": "write ship it into plan.txt", "message_id": "a-1"},
                                     headers=ORIGIN)
            detail = paused.json()["detail"]
            written_before = (env.workspace / "plan.txt").exists()
            approved = env.client.post(f"/api/approvals/{detail['approval_id']}/approve", headers=ORIGIN)
            session_id = approved.json()["session_id"]
            resumed = env.client.post("/api/chat", json={"message": approved.json()["resume_message"],
                                                         "session_id": session_id, "message_id": "a-2"},
                                      headers=ORIGIN)
        return (paused.status_code, detail["type"], detail["tool_name"], written_before, approved.status_code,
                resumed.status_code, resumed.json()["response"], (env.workspace / "plan.txt").read_text(),
                env.messages(session_id), env.turns(), env.tool_effects())
    legacy, cordis = both(tmp_path, monkeypatch, script, drive)
    assert legacy[:9] == cordis[:9]
    assert cordis[:8] == (409, "approval_required", "write_file", False, 200, 200, "Wrote the plan.", "ship it")
    assert cordis[9] == [("blocked", "approval_required"), ("succeeded", None)]
    assert sorted(cordis[10]) == [("write_file", "no_effect"), ("write_file", "succeeded")]


def test_onboarding_runs_its_per_turn_surface_through_cordis(tmp_path, monkeypatch, real_auth):  # noqa: F811
    def drive(env):
        # On the OpenRouter profile onboarding defaults to direct chat; this case drives its agent.
        with patch("src.api.ws.should_use_direct_local_chat", return_value=False):
            frames = ws_turn(env, "I am Ada and I build compilers for a living")
        return shape(frames), env.messages(frames[-1]["session_id"]), env.turns(), env.model.calls[0]["tools"]
    legacy, cordis = both(tmp_path, monkeypatch, lambda: [final("Nice to meet you, Ada.")], drive, onboarded=False)
    assert legacy[:2] == cordis[:2] and legacy[3] == cordis[3]
    assert "update_soul" in cordis[3] and "write_file" not in cordis[3]
    assert cordis[2] == [("succeeded", None)]


def test_store_secret_is_denied_and_its_value_never_enters_turn_state(tmp_path, monkeypatch, real_auth):  # noqa: F811
    from sqlalchemy import select
    from src.db import engine as db_engine
    from src.db.models import WorkflowRunState
    with chat_env(tmp_path, monkeypatch, [tools(call("store_secret", {"key": "api", "value": SECRET})),
                                          final("Use Settings for secrets.")], loop="cordis") as env:
        frames = ws_turn(env, "remember my api token please")
        assert frames[-1]["type"] == "final"
        assert any("Settings -> Vault" in str(frame.get("content")) for frame in frames if frame["type"] == "step")

        async def persisted():
            async with db_engine.get_session() as db:
                return json.dumps([row.model_dump() for row in (await db.execute(select(WorkflowRunState))).scalars()],
                                  default=str)
        assert SECRET not in env.client.portal.call(persisted)
        from src.vault.repository import vault_repository
        assert env.client.portal.call(vault_repository.get, "api") is None
        # Nor in any WS frame, audit event, approval or transcript row.
        from src.approval.repository import approval_repository
        from src.audit.repository import audit_repository
        session_id = frames[-1]["session_id"]
        assert SECRET not in json.dumps(frames)
        assert SECRET not in json.dumps(env.client.portal.call(lambda: audit_repository.list_events(limit=500)), default=str)
        assert SECRET not in json.dumps(env.client.portal.call(lambda: approval_repository.list_pending(limit=100)), default=str)
        assert SECRET not in json.dumps(env.messages(session_id))


def test_measured_direct_chat_first_delta_legacy_vs_cordis(tmp_path, monkeypatch, real_auth):  # noqa: F811
    """Real first delta through the WS route (send -> first delta frame), both loops; printed,
    and asserted against the 250 ms added-latency budget only with SERAPH_CORDIS_LATENCY_BUDGET=1."""
    import os
    import statistics
    import time

    async def stream(*_args, **_kwargs):
        yield "Hi"

    def drive(env):
        samples = []
        with patch("src.api.ws.should_use_direct_local_chat", return_value=True), \
             patch("src.api.ws.direct_local_chat_route_error", new=AsyncMock(return_value=None)), \
             patch("src.api.ws.stream_direct_local_chat", stream):
            with env.client.websocket_connect("/ws/chat", headers=ORIGIN) as socket:
                for index in range(20):
                    began = time.perf_counter()
                    socket.send_text(json.dumps({"type": "message", "message": "hi", "message_id": f"fd-{index}"}))
                    first = None
                    while True:
                        frame = receive(socket)
                        if frame["type"] == "delta" and first is None:
                            first = time.perf_counter() - began
                        if frame["type"] in {"final", "error"}:
                            assert frame["type"] == "final", frame
                            break
                    samples.append(first)
        return statistics.median(samples)
    legacy, cordis = both(tmp_path, monkeypatch, lambda: [], drive)
    print(f"MEASURED direct first delta median: legacy {legacy * 1000:.1f} ms, cordis {cordis * 1000:.1f} ms, "
          f"added {(cordis - legacy) * 1000:.1f} ms")
    if os.environ.get("SERAPH_CORDIS_LATENCY_BUDGET") == "1":
        assert cordis - legacy <= 0.250


def test_malformed_inventory_blocks_chat_visibly_instead_of_a_500(tmp_path, monkeypatch, real_auth):  # noqa: F811
    from sqlalchemy import update
    from src.db.models import RuntimeCompositionState
    with chat_env(tmp_path, monkeypatch, [final("never")], loop="cordis") as env:
        async def corrupt():
            from src.db.engine import get_session
            async with get_session() as db:
                await db.execute(update(RuntimeCompositionState).where(
                    RuntimeCompositionState.runtime_domain == "seraph.agent-loop.v1").values(epoch=0))
        env.client.portal.call(corrupt)
        response = env.client.post("/api/chat", json={"message": "summarize my notes", "message_id": "bad-1"},
                                   headers=ORIGIN)
        assert response.status_code == 503 and response.json()["detail"]["code"] == "agent_loop_inventory_invalid"
        frames = ws_turn(env, "summarize my notes", message_id="bad-2")
        assert frames[-1]["type"] == "error" and frames[-1]["reason"] == "agent_loop_inventory_invalid"
        assert env.model.calls == [] and env.messages(frames[-1]["session_id"]) == []


def test_ws_clarify_matches_legacy(tmp_path, monkeypatch, real_auth):  # noqa: F811
    def script():
        return [tools(call("clarify", {"question": "Which project?", "reason": "ambiguous", "options": "a,b"})),
                final("Tell me which project you mean.")]

    def drive(env):
        frames = ws_turn(env, "update the project plan please")
        return shape(frames), env.messages(frames[-1]["session_id"]), env.turns()
    legacy, cordis = both(tmp_path, monkeypatch, script, drive)
    assert legacy[:2] == cordis[:2] and cordis[2] in ([("succeeded", None)], [("blocked", "clarification_required")])


def test_ws_max_steps_large_observation_and_multi_call_match_legacy(tmp_path, monkeypatch, real_auth):  # noqa: F811
    monkeypatch.setattr(settings, "agent_max_steps", 2)
    big = "line of notes\n" * 3000

    def script():
        return [tools(call("read_file", {"file_path": "big.txt"}, "call-a"), call("read_file", {"file_path": "small.txt"}, "call-b")),
                tools(call("read_file", {"file_path": "other.txt"}, "call-c")),
                text("Summary after the step budget.")]

    def drive(env):
        (env.workspace / "big.txt").write_text(big)
        (env.workspace / "small.txt").write_text("small notes")
        (env.workspace / "other.txt").write_text("other notes")
        frames = ws_turn(env, "read both of my note files and summarize")
        return shape(frames), env.messages(frames[-1]["session_id"]), env.turns(), env.model.calls, env.tool_effects()
    legacy, cordis = both(tmp_path, monkeypatch, script, drive)
    assert legacy[0] == cordis[0] and legacy[1] == cordis[1] and legacy[3] == cordis[3]
    assert cordis[0][-1][:2] == ("final", "Summary after the step budget.")
    # The large observation reached the model and the frames untruncated, in both loops.
    assert any(item[0] == "step" and item[1].count("line of notes") == 3000 for item in cordis[0])
    assert [entry["tools"] != [] for entry in cordis[3]] == [True, True, False]
    assert cordis[4] == [("read_file", "succeeded")] * 3 and cordis[2] == [("succeeded", None)]


def test_rest_client_disconnect_cancels_the_turn(tmp_path, monkeypatch, real_auth):  # noqa: F811
    import threading
    import time
    import httpx
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
        cookies = dict(env.client.cookies)

        async def disconnect():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.client.app), base_url="http://testserver",
                                         cookies=cookies, headers=ORIGIN) as client:
                request = asyncio.create_task(client.post("/api/chat", json={"message": "read my notes and summarize",
                                                                             "message_id": "rest-cancel"}))
                while not entered.is_set():
                    await asyncio.sleep(0.02)
                request.cancel()
                try:
                    await request
                except asyncio.CancelledError:
                    pass
        env.client.portal.call(disconnect)
        gate.set()
        for _ in range(200):
            if env.turns() and env.turns()[0][0] != "running":
                break
            time.sleep(0.05)
        assert env.turns() == [("blocked", "agent_turn_cancelled")]


def test_rest_failure_or_disconnect_between_admission_and_attach_settles_the_turn(tmp_path, monkeypatch, real_auth):  # noqa: F811
    import time
    import httpx
    with chat_env(tmp_path, monkeypatch, [final("never")], loop="cordis") as env:
        # The agent build fails after admission: REST errors and the turn is closed, not left running.
        def broken(*_args, **_kwargs):
            raise RuntimeError("agent build failed")
        monkeypatch.setattr("src.api.chat.build_agent", broken)
        with pytest.raises(RuntimeError):
            env.client.post("/api/chat", json={"message": "summarize my notes", "message_id": "build-1"}, headers=ORIGIN)
        assert env.turns() == [("blocked", "agent_turn_failed")]
        # A client disconnect while guardian state is being built cancels the turn.
        entered = asyncio.Event()

        async def hanging(**_kwargs):
            entered.set()
            await asyncio.sleep(60)
        monkeypatch.setattr("src.api.chat.build_guardian_state", hanging)
        cookies = dict(env.client.cookies)

        async def disconnect():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.client.app), base_url="http://testserver",
                                         cookies=cookies, headers=ORIGIN) as client:
                request = asyncio.create_task(client.post("/api/chat", json={"message": "summarize my notes",
                                                                             "message_id": "build-2"}))
                await asyncio.wait_for(entered.wait(), 10)
                request.cancel()
                try:
                    await request
                except asyncio.CancelledError:
                    pass
        env.client.portal.call(disconnect)
        for _ in range(100):
            if len(env.turns()) == 2 and env.turns()[1][0] != "running":
                break
            time.sleep(0.05)
        assert env.turns() == [("blocked", "agent_turn_failed"), ("blocked", "agent_turn_cancelled")]
        assert env.model.calls == []


def test_a_cutover_between_the_route_read_and_the_legacy_gate_uses_the_new_route(tmp_path, monkeypatch, real_auth):  # noqa: F811
    """REST and WS re-read the route after taking the legacy gate: no legacy turn starts after the owner moved."""
    from src.runtime_plugins import agent_turns
    from src.runtime_plugins.cutover import legacy_turns, transition_domain
    with chat_env(tmp_path, monkeypatch, [final("REST on Cordis."), final("WS on Cordis.")], loop="legacy") as env:
        # Chat's prerequisite (#1020): the reviewed presets are Cordis-owned before chat can move.
        env.run(transition_domain("seraph.agent-presets.v1", "cordis", host=env.host, actor="operator:test"))
        real = agent_turns.chat_runtime_status
        raced = []

        async def racing(host=None):
            status = await real(host)
            if not raced:
                # The owner moves after the first read, before the legacy gate is taken.
                raced.append(await transition_domain(AGENT_LOOP, "cordis", host=env.host, actor="operator:test"))
            return status
        monkeypatch.setattr(agent_turns, "chat_runtime_status", racing)
        response = env.client.post("/api/chat", json={"message": "what do my notes say?", "message_id": "race-1"},
                                   headers=ORIGIN)
        assert response.status_code == 200 and response.json()["response"] == "REST on Cordis."
        assert raced[0]["owner_kind"] == "cordis" and env.turns() == [("succeeded", None)]
        assert legacy_turns.active.get(AGENT_LOOP, 0) == 0
        env.run(transition_domain(AGENT_LOOP, "legacy", host=env.host, actor="operator:test"))
        raced.clear()
        frames = ws_turn(env, "what do my notes say?", message_id="race-2")
        assert frames[-1]["type"] == "final" and frames[-1]["content"] == "WS on Cordis."
        assert any("Cordis agent loop" in str(frame.get("content")) for frame in frames if frame["type"] == "status")
        assert env.turns() == [("succeeded", None)] * 2 and legacy_turns.active.get(AGENT_LOOP, 0) == 0
