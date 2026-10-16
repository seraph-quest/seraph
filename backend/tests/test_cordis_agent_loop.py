"""#1019 acceptance: the Cordis agent-loop turn owner with the real reviewed child.

Scripted local inference responses (no network or inference), real Auth,
SQLite, durable jobs and temporary transcript state.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from smolagents import Tool, ToolCallingAgent

from tests.cordis_agent_loop import (ScriptedModel, agent_loop_host, call, cutover_agent_loop, deny_sockets, final,
                                     text, tools)
from tests.cordis_research import needs_cordis
from tests.test_inference_accounting import accounting_db  # noqa: F401
from tests.test_research_native_vertical import real_auth  # noqa: F401
from src.runtime_plugins.agent_turns import CordisTurn, TurnBlocked, turn_failure_reason
from src.workflows.job_runtime import DurableJobRepository

pytestmark = [needs_cordis, pytest.mark.asyncio]


class Echo(Tool):
    name = "echo"
    description = "Echo the text back."
    inputs = {"text": {"type": "string", "description": "text"}}
    output_type = "string"

    def __init__(self):
        super().__init__()
        self.seen = []

    def forward(self, text):
        self.seen.append(text)
        if text == "explode":
            raise RuntimeError("echo exploded")
        return "echo:" + text


async def _session(session_id="chat-1"):
    from src.agent.session import session_manager
    from src.auth.service import create_session
    _, operator = await create_session()
    await session_manager.get_or_create(session_id)
    return operator


async def _turn(operator, host, jobs, message_id="m-1", profile="agent", timeout=60):
    turn, replayed = await CordisTurn.admit(operator=operator, conversation_session_id="chat-1", message_id=message_id,
                                            profile=profile, timeout_seconds=timeout, host=host, jobs=jobs)
    assert not replayed
    return turn


async def _run(turn, agent, task="hello"):
    turn.attach(agent)
    return await asyncio.get_running_loop().run_in_executor(None, lambda: agent.run(task))


def _reply(message_id, content):
    return {"session_id": "chat-1", "role": "assistant", "content": content, "message_id": message_id,
            "metadata_json": None}


async def _context(jobs, turn):
    async with jobs._session() as db:
        run = await jobs._fetch(db, turn.job_id)
        return json.loads(run.checkpoint_context_json), await jobs.get_job(turn.job_id)


async def test_tool_call_then_final_answer_settles_once_with_the_transcript(accounting_db, real_auth, monkeypatch):  # noqa: F811
    from src.agent.session import session_manager
    deny_sockets(monkeypatch)
    jobs = DurableJobRepository()
    async with agent_loop_host(jobs) as host:
        operator = await _session()
        echo = Echo()
        model = ScriptedModel([tools(call("echo", {"text": "hi"})), text("no tool call here"), final("done: echo:hi")])
        agent = ToolCallingAgent(tools=[echo], model=model, max_steps=5, verbosity_level=0)
        turn = await _turn(operator, host, jobs)
        answer = await _run(turn, agent)
        assert answer == "done: echo:hi" and echo.seen == ["hi"]
        # A reply without a tool call was an observation, never a settlement.
        assert "Error while parsing tool call" in str(agent.memory.steps[2].error)
        assert await turn.settle("succeeded", message=_reply("a-1", answer))
        context, job = await _context(jobs, turn)
        assert [(step["kind"], step["decisions"]) for step in context["steps"]] == [
            ("tool_calls", ["tool_intent:0"]), ("parse_error", ["continue"]), ("final_answer", ["settlement"])]
        assert context["surface_digest"] and context["settlement"]["message_id"] == "a-1"
        assert job["status"] == "succeeded"
        assert await turn.effects() == [{"tool_name": "echo", "status": "succeeded"}]
        rows = await session_manager.get_messages("chat-1")
        assert [row["content"] for row in rows if row["role"] == "assistant"] == ["done: echo:hi"]
        # Settlement happens exactly once; a second attempt adopts nothing.
        assert not await turn.settle("succeeded", message=_reply("a-2", "again"))
        for step in context["steps"]:
            assert (await jobs.get_job(step["step_job"]))["status"] == "succeeded"


async def test_tool_errors_and_unknown_tools_are_observations_and_max_steps_makes_one_final_call(accounting_db, real_auth, monkeypatch):  # noqa: F811
    from config.settings import settings
    deny_sockets(monkeypatch)
    jobs = DurableJobRepository()
    monkeypatch.setattr(settings, "agent_max_steps", 2)
    async with agent_loop_host(jobs) as host:
        operator = await _session()
        echo = Echo()
        model = ScriptedModel([tools(call("echo", {"text": "explode"})), tools(call("nope", {})),
                               text("summary after max steps")])
        agent = ToolCallingAgent(tools=[echo], model=model, max_steps=2, verbosity_level=0)
        turn = await _turn(operator, host, jobs)
        answer = await _run(turn, agent)
        assert answer == "summary after max steps"
        # The extra max-steps call offers no tools, exactly as smolagents' provide_final_answer.
        assert [entry["tools"] != [] for entry in model.calls] == [True, True, False]
        context, _ = await _context(jobs, turn)
        assert [(step["kind"], step["decisions"]) for step in context["steps"]] == [
            ("tool_calls", ["tool_intent:0"]), ("tool_calls", ["tool_intent:0"]), ("max_steps", ["final_call"])]
        # The raising tool's intent resolved as failed; the unknown tool never started an effect.
        assert await turn.effects() == [{"tool_name": "echo", "status": "failed"}]


async def test_duplicate_ingress_admits_one_turn(accounting_db, real_auth, monkeypatch):  # noqa: F811
    deny_sockets(monkeypatch)
    jobs = DurableJobRepository()
    async with agent_loop_host(jobs) as host:
        operator = await _session()
        first = await _turn(operator, host, jobs)
        again, replayed = await CordisTurn.admit(operator=operator, conversation_session_id="chat-1", message_id="m-1",
                                                 profile="agent", timeout_seconds=60, host=host, jobs=jobs)
        assert again is None and replayed is True
        other = await _turn(operator, host, jobs, message_id="m-2")
        assert other.job_id != first.job_id


async def test_cancellation_revoked_memory_and_stale_epoch_cannot_continue_or_adopt(accounting_db, real_auth, monkeypatch):  # noqa: F811
    from sqlalchemy import update
    from src.agent.session import session_manager
    from src.db.models import Memory, MemoryKind, RuntimeCompositionState
    from src.memory.control import forget_memory
    deny_sockets(monkeypatch)
    monkeypatch.setattr("src.memory.repository.get_session", accounting_db[2].accounting_sessions)
    jobs = DurableJobRepository()
    async with agent_loop_host(jobs) as host:
        operator = await _session()
        # Cancelled: the next boundary stops the loop; the one interrupted row is fenced.
        turn = await _turn(operator, host, jobs)
        echo = Echo()
        agent = ToolCallingAgent(tools=[echo], model=ScriptedModel([final("never")]), max_steps=3, verbosity_level=0)
        turn.cancel()
        with pytest.raises(Exception) as cancelled:
            await _run(turn, agent)
        assert turn_failure_reason(cancelled.value) == "agent_turn_cancelled"
        assert await turn.interrupt(_reply("a-int", "interrupted"))
        _, job = await _context(jobs, turn)
        assert job["status"] == "blocked" and job["failure_reason"] == "agent_turn_cancelled"

        # Revoked memory: a memory forgotten mid-turn can never reach the next prompt assembly.
        async with jobs._session() as db:
            memory = Memory(content="private fact", summary="private fact", kind=MemoryKind.fact)
            db.add(memory)
            await db.commit()
            memory_id = memory.id
        turn = await _turn(operator, host, jobs, message_id="m-mem")
        model = ScriptedModel([tools(call("echo", {"text": "a"})), final("never")])
        agent = ToolCallingAgent(tools=[Echo()], model=model, max_steps=3, verbosity_level=0)
        forgotten = {"done": False}
        original = agent.execute_tool_call

        def forget_during_tool(name, arguments):
            result = original(name, arguments)
            if not forgotten["done"]:
                forgotten["done"] = True
                turn.run_sync(forget_memory(memory_id=memory_id))
            return result
        agent.execute_tool_call = forget_during_tool
        with pytest.raises(Exception) as revoked:
            await _run(turn, agent)
        assert turn_failure_reason(revoked.value) == "agent_turn_memory_revoked"
        assert len(model.calls) == 1  # The second prompt was never assembled.

        # Stale epoch: an owner row that moved on cannot adopt final output.
        turn = await _turn(operator, host, jobs, message_id="m-epoch")
        async with jobs._session() as db:
            await db.execute(update(RuntimeCompositionState).where(
                RuntimeCompositionState.runtime_domain == "seraph.agent-loop.v1").values(epoch=RuntimeCompositionState.epoch + 5))
            await db.commit()
        assert not await turn.settle("succeeded", message=_reply("a-epoch", "stale"))
        assert await session_manager.get_message("a-epoch") is None
        with pytest.raises(TurnBlocked) as stale:
            await turn.check_live()
        assert stale.value.reason_code == "agent_turn_epoch_changed"


async def test_child_staging_anything_but_pythons_parse_is_rejected(accounting_db, real_auth, monkeypatch, tmp_path):  # noqa: F811
    from tests.cordis_research import PACKAGE
    from tests.cordis_agent_loop import patched_package
    deny_sockets(monkeypatch)
    jobs = DurableJobRepository()
    variant = patched_package(tmp_path, "return ['settlement'];", "return ['continue'];")
    async with agent_loop_host(jobs, package_root=variant) as host:
        operator = await _session()
        turn = await _turn(operator, host, jobs)
        agent = ToolCallingAgent(tools=[Echo()], model=ScriptedModel([final("adopt me")]), max_steps=3, verbosity_level=0)
        with pytest.raises(Exception) as mismatch:
            await _run(turn, agent)
        assert turn_failure_reason(mismatch.value) == "native_child_result_mismatch"
        context, _ = await _context(jobs, turn)
        assert context["steps"] == []
    assert PACKAGE.exists()


async def test_restart_never_resumes_and_keeps_committed_and_unknown_effects(accounting_db, real_auth, monkeypatch):  # noqa: F811
    from datetime import datetime, timedelta, timezone
    deny_sockets(monkeypatch)
    jobs = DurableJobRepository()
    async with agent_loop_host(jobs) as host:
        operator = await _session()
        turn = await _turn(operator, host, jobs, timeout=5)
        await turn.tool_intent(step_index=1, call_index=0, tool_name="echo")
        await turn.tool_outcome(step_index=1, call_index=0, tool_name="echo", outcome="succeeded")
        await turn.tool_intent(step_index=2, call_index=0, tool_name="write_file")  # in flight at the crash
        recovered = await jobs.recover_stale_job(turn.job_id, now=datetime.now(timezone.utc) + timedelta(hours=1))
        assert recovered["status"] in {"failed", "blocked", "unknown_external_effect"}
        assert await turn.effects() == [{"tool_name": "echo", "status": "succeeded"},
                                        {"tool_name": "write_file", "status": "intent"}]
        # The old owner can neither continue nor settle after recovery.
        with pytest.raises(TurnBlocked):
            await turn.check_live()
        assert not await turn.settle("succeeded", message=_reply("a-crash", "late"))


async def test_runtime_status_names_the_actual_loop_and_route(accounting_db, real_auth, monkeypatch):  # noqa: F811
    from src.runtime_plugins.agent_turns import chat_runtime_status
    deny_sockets(monkeypatch)
    jobs = DurableJobRepository()
    async with agent_loop_host(jobs, cordis=False) as host:
        status = await chat_runtime_status(host)
        assert (status["loop"], status["route"], status["reason"]) == ("legacy.smolagents", "chat_agent", None)
        epoch = await cutover_agent_loop(host)
        status = await chat_runtime_status(host)
        assert status == {"loop": "cordis.agent-loop.v1", "route": "chat_agent", "owner_epoch": epoch, "reason": None,
                          "recovery": None}
    # The host stopped: Cordis chat is blocked with the exact fix, never legacy.
    status = await chat_runtime_status(host)
    assert status["loop"] == "blocked" and status["reason"].startswith("cordis_") and status["recovery"]
    assert Path(__file__).exists()


async def test_measured_step_and_first_delta_overhead_p95_over_100_samples(accounting_db, real_auth, monkeypatch):  # noqa: F811
    """One budget, 250 ms p95, for each added Cordis cost on the real path: a startTurn step
    (step job, invocation, decisions, verification), a tool's intent+outcome receipts, and the
    turn admission that is the only Cordis work before a direct-chat first delta."""
    import statistics
    import time
    from config.settings import settings
    deny_sockets(monkeypatch)
    monkeypatch.setattr(settings, "agent_max_steps", 130)
    jobs = DurableJobRepository()
    samples = 110

    def p95(values):
        return statistics.quantiles(values, n=100)[94]
    async with agent_loop_host(jobs) as host:
        operator = await _session()
        admissions, steps, receipts = [], [], []
        for index in range(samples):
            began = time.perf_counter()
            admitted = await _turn(operator, host, jobs, message_id=f"measure-{index}", timeout=600)
            admissions.append(time.perf_counter() - began)
            if index:
                assert await admitted.settle("blocked", reason="measurement")
            else:
                turn = admitted
        for index in range(1, samples + 1):
            began = time.perf_counter()
            assert await turn.decide("tool_calls", step_index=index, call_count=1, output_digest="a" * 64) == ["tool_intent:0"]
            steps.append(time.perf_counter() - began)
            began = time.perf_counter()
            await turn.tool_intent(step_index=index, call_index=0, tool_name="echo")
            await turn.tool_outcome(step_index=index, call_index=0, tool_name="echo", outcome="succeeded")
            receipts.append(time.perf_counter() - began)
        print("MEASURED n=%d p50/p95 ms: step %.1f/%.1f, tool receipts %.1f/%.1f, admission %.1f/%.1f" % (
            samples, statistics.median(steps) * 1000, p95(steps) * 1000, statistics.median(receipts) * 1000,
            p95(receipts) * 1000, statistics.median(admissions) * 1000, p95(admissions) * 1000))
        assert p95(steps) <= 0.250 and p95(receipts) <= 0.250 and p95(admissions) <= 0.250
