"""#1020 PR2: orchestrator, specialists and delegation on the Cordis loop.

Each delegated specialist runs as a durable child turn of the chat turn with
the real reviewed Cordis child (agent-loop, delegation and presets plugins),
real Auth, SQLite and durable jobs. Models are scripted local smolagents
models; sockets and DNS are denied, so no provider is contacted.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import threading

import pytest
from smolagents import Tool

from src.agent.tool_calling_agent import ToolCallingAgent
from src.runtime_plugins.agent_turns import (CHILD_TURN_JOB_KIND, DELEGATION_STEP_JOB_KIND, SETTLEMENT_GRACE,
                                             CordisTurn, TurnBlocked, turn_failure_reason)
from src.workflows.job_runtime import DurableJobRepository
from tests.cordis_agent_loop import AGENT_LOOP, ScriptedModel, call, deny_sockets, final, tools
from tests.cordis_research import needs_cordis, research_host
from tests.test_inference_accounting import accounting_db  # noqa: F401
from tests.test_research_native_vertical import real_auth  # noqa: F401

pytestmark = [needs_cordis, pytest.mark.asyncio]
PRESETS = "seraph.agent-presets.v1"
DELEGATION = "seraph.delegation.v1"


class ReadFile(Tool):
    """Stands in for the file worker's ``read_file``; ``hook`` runs inside the tool call."""
    name = "read_file"
    description = "Read a workspace file."
    inputs = {"file_path": {"type": "string", "description": "path"}}
    output_type = "string"

    def __init__(self, hook=None):
        super().__init__()
        self.seen, self.hook = [], hook

    def forward(self, file_path):
        self.seen.append(file_path)
        if self.hook is not None:
            self.hook(file_path)
        return f"contents of {file_path}"


class DelegateTask(Tool):
    name = "delegate_task"
    description = "Delegate again."
    inputs = {"task": {"type": "string", "description": "task"}}
    output_type = "string"

    def forward(self, task):
        return "never"


def specialist(name, script, tool_list, *, temperature=0.3, max_steps=6):
    """A specialist built like ``create_specialist``: its model carries its route and temperature."""
    model = ScriptedModel(script)
    model._runtime_path = name
    model.kwargs["temperature"] = temperature
    return ToolCallingAgent(tools=tool_list, model=model, name=name, description=f"The {name} specialist.",
                            max_steps=max_steps, verbosity_level=0)


def orchestrator(script, specialists, max_steps=8):
    return ToolCallingAgent(tools=[], managed_agents=specialists, model=ScriptedModel(script), max_steps=max_steps,
                            verbosity_level=0)


def delegate(name="file_worker", task="read notes.txt", call_id="call-1"):
    return tools(call(name, {"task": task}, call_id))


@asynccontextmanager
async def delegation_host(jobs):
    """Presets, the agent loop and delegation on Cordis through the production owner transitions."""
    from src.runtime_plugins.cutover import transition_domain
    async with research_host(jobs) as host:
        for domain in (PRESETS, AGENT_LOOP, DELEGATION):
            moved = await transition_domain(domain, "cordis", host=host, actor="operator:test")
            assert (moved["owner_kind"], moved["state"]) == ("cordis", "ready"), moved
        yield host


@pytest.fixture
def delegating(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "use_delegation", True)
    monkeypatch.setattr(settings, "orchestrator_max_steps", 10)
    deny_sockets(monkeypatch)


async def _turn(host, jobs, *, timeout=60, message_id="m-1"):
    from tests.test_cordis_agent_loop import _session
    operator = await _session()
    turn, replayed = await CordisTurn.admit(operator=operator, conversation_session_id="chat-1", message_id=message_id,
                                            profile="agent", timeout_seconds=timeout, host=host, jobs=jobs)
    assert not replayed and turn.preset_id == "chat.orchestrator"
    return operator, turn


def _reply(message_id, content):
    return {"session_id": "chat-1", "role": "assistant", "content": content, "message_id": message_id,
            "metadata_json": None}


async def _run(turn, agent, task="hello"):
    turn.attach(agent)
    return await asyncio.get_running_loop().run_in_executor(None, lambda: agent.run(task))


async def _runs(jobs, kind):
    from sqlalchemy import select
    from src.db.models import WorkflowRunState
    async with jobs._session() as db:
        return list((await db.execute(select(WorkflowRunState).where(WorkflowRunState.job_kind == kind)
                                      .order_by(WorkflowRunState.started_at))).scalars())


def _effects(run):
    return [((item.get("details") or {}).get("tool_name"), item.get("status"), item.get("details") or {})
            for item in json.loads(run.effect_receipts_json or "[]") if item.get("effect_type") == "cordis_turn_tool"]


async def test_a_delegated_specialist_is_a_durable_child_turn_with_derived_receipts(accounting_db, real_auth, delegating):  # noqa: F811
    jobs = DurableJobRepository()
    async with delegation_host(jobs) as host:
        _, turn = await _turn(host, jobs)
        reader = ReadFile()
        worker = specialist("file_worker", [tools(call("read_file", {"file_path": "notes.txt"})),
                                            final("notes.txt says hello")], [reader])
        answer = await _run(turn, orchestrator([delegate(), final("Your notes say hello.")], [worker]))
        assert answer == "Your notes say hello." and reader.seen == ["notes.txt"]
        assert await turn.settle("succeeded", message=_reply("a-1", answer))
        (child,) = await _runs(jobs, CHILD_TURN_JOB_KIND)
        (admission,) = await _runs(jobs, DELEGATION_STEP_JOB_KIND)
        # Same owner and Root, a durable child of the turn, its own reviewed preset.
        assert (child.status, child.parent_job_id, child.owner_principal_id, child.operator_session_id) == (
            "succeeded", turn.job_id, turn.operator.principal.principal_id, turn.operator.session_id)
        authority = json.loads(child.declared_authority_json)
        assert authority["parent_job_id"] == turn.job_id and authority["grants"]
        context = json.loads(child.checkpoint_context_json)
        assert context["preset_id"] == "specialist.file_worker" and context["settlement"]["outcome"] == "succeeded"
        assert [step["kind"] for step in context["steps"]] == ["tool_calls", "final_answer"]
        # The child's deadline leaves the parent its settlement window.
        expiry = child.deadline_at.replace(tzinfo=turn.deadline.tzinfo)
        assert abs((turn.deadline - SETTLEMENT_GRACE - expiry).total_seconds()) < 1
        assert admission.status == "succeeded"
        assert [item["ref"] for item in json.loads(admission.checkpoint_context_json)["operations"]] == ["child_admit:0"]
        # The child's receipts are on its own row; the parent's receipt for the call is derived from them.
        assert [(name, status) for name, status, _ in _effects(child)] == [("read_file", "succeeded")]
        async with jobs._session() as db:
            parent = await jobs._fetch(db, turn.job_id)
        ((name, status, details),) = _effects(parent)
        assert (name, status, details["child_job_id"]) == ("file_worker", "succeeded", child.run_identity)
        assert [(item["tool_name"], item["status"]) for item in details["child_effects"]] == [("read_file", "succeeded")]


async def test_at_most_four_children_run_per_turn_and_refusals_do_not_count(accounting_db, real_auth, delegating):  # noqa: F811
    jobs = DurableJobRepository()
    async with delegation_host(jobs) as host:
        _, turn = await _turn(host, jobs)
        worker = specialist("file_worker", [final(f"done {index}") for index in range(4)], [ReadFile()])
        empty = specialist("memory_keeper", [], [], temperature=0.5, max_steps=3)
        script = ([delegate(call_id=f"w{index}") for index in range(2)]
                  + [delegate("memory_keeper", call_id=f"e{index}") for index in range(2)]
                  + [delegate(call_id=f"w{index}") for index in range(2, 4)]
                  + [delegate(call_id="w4"), final("All done.")])
        agent = orchestrator(script, [worker, empty])
        assert await _run(turn, agent) == "All done."
        observations = [str(step.observations) for step in agent.memory.steps if getattr(step, "observations", None)]
        assert sum("No specialists are currently available" in item for item in observations) == 2
        assert sum("Delegation limit reached for this turn (4)" in item for item in observations) == 1
        assert [run.status for run in await _runs(jobs, CHILD_TURN_JOB_KIND)] == ["succeeded"] * 4
        assert len(await _runs(jobs, DELEGATION_STEP_JOB_KIND)) == 4
        assert await turn.settle("succeeded", message=_reply("a-1", "All done."))


async def test_no_child_starts_without_turn_time_left(accounting_db, real_auth, delegating):  # noqa: F811
    import time

    class Late(ScriptedModel):
        def generate(self, *args, **kwargs):
            if not self.calls:
                time.sleep(1.2)  # the turn's own time runs out while the model decides
            return super().generate(*args, **kwargs)
    jobs = DurableJobRepository()
    async with delegation_host(jobs) as host:
        _, turn = await _turn(host, jobs, timeout=1)
        agent = ToolCallingAgent(tools=[], managed_agents=[specialist("file_worker", [], [ReadFile()])],
                                 model=Late([delegate(), final("No time.")]), max_steps=10, verbosity_level=0)
        assert await _run(turn, agent) == "No time."
        assert any("Not enough turn time left to delegate" in str(step.observations)
                   for step in agent.memory.steps if getattr(step, "observations", None))
        assert await _runs(jobs, CHILD_TURN_JOB_KIND) == [] and await _runs(jobs, DELEGATION_STEP_JOB_KIND) == []


async def test_a_changed_specialist_or_a_nested_surface_blocks_the_parent_before_any_child(accounting_db, real_auth, delegating):  # noqa: F811
    jobs = DurableJobRepository()
    async with delegation_host(jobs) as host:
        for index, worker in enumerate((
                specialist("file_worker", [], [ReadFile()], temperature=0.9),          # widened temperature
                specialist("file_worker", [], [ReadFile(), DelegateTask()]))):         # a child that could delegate
            _, turn = await _turn(host, jobs, message_id=f"m-{index}")
            with pytest.raises(Exception) as stopped:
                await _run(turn, orchestrator([delegate()], [worker]))
            assert turn_failure_reason(stopped.value) == "agent_turn_child_blocked"
            assert turn.child_block == ("file_worker", ["agent_preset_changed", "agent_preset_tools_exceeded"][index])
            from src.runtime_plugins.agent_turns import turn_blocked_message
            assert "Delegated specialist file_worker stopped" in turn_blocked_message("agent_turn_child_blocked", turn)
            assert await turn.settle("blocked", reason="agent_turn_child_blocked")
        assert await _runs(jobs, CHILD_TURN_JOB_KIND) == [] and await _runs(jobs, DELEGATION_STEP_JOB_KIND) == []
        # A specialist that itself has specialists is refused as nested delegation (an observation).
        inner = specialist("web_researcher", [], [ReadFile()], max_steps=8)
        nested = ToolCallingAgent(tools=[ReadFile()], managed_agents=[inner], model=ScriptedModel([]),
                                  name="file_worker", description="nested", max_steps=6, verbosity_level=0)
        _, turn = await _turn(host, jobs, message_id="m-nested")
        agent = orchestrator([delegate(), final("Could not.")], [nested])
        assert await _run(turn, agent) == "Could not."
        assert any("Nested delegation is not allowed" in str(step.observations)
                   for step in agent.memory.steps if getattr(step, "observations", None))


async def test_parent_interrupt_mid_child_tool_leaves_both_receipts_unknown(accounting_db, real_auth, delegating):  # noqa: F811
    entered, release = threading.Event(), threading.Event()

    def hold(_path):
        entered.set()
        release.wait(10)
    jobs = DurableJobRepository()
    async with delegation_host(jobs) as host:
        _, turn = await _turn(host, jobs)
        worker = specialist("file_worker", [tools(call("read_file", {"file_path": "big.txt"})), final("never")],
                            [ReadFile(hold)])
        running = asyncio.ensure_future(_run(turn, orchestrator([delegate(), final("never")], [worker])))
        assert await asyncio.to_thread(entered.wait, 10)
        # The operator disconnects while the child's tool is mid-call.
        assert await turn.interrupt(None)
        release.set()
        with pytest.raises(Exception):
            await running
        (child,) = await _runs(jobs, CHILD_TURN_JOB_KIND)
        async with jobs._session() as db:
            parent = await jobs._fetch(db, turn.job_id)
        assert [(name, status) for name, status, _ in _effects(child)] == [("read_file", "unknown")]
        assert [(name, status) for name, status, _ in _effects(parent)] == [("file_worker", "unknown")]
        assert child.status != "succeeded" and child.status != "running"
        assert json.loads(parent.checkpoint_context_json)["settlement"]["outcome"] == "cancelled"


async def test_parent_cancel_between_child_steps_stops_the_child_first(accounting_db, real_auth, delegating):  # noqa: F811
    jobs = DurableJobRepository()
    async with delegation_host(jobs) as host:
        _, turn = await _turn(host, jobs)
        reader = ReadFile(lambda _path: turn.cancel())
        worker = specialist("file_worker", [tools(call("read_file", {"file_path": "a.txt"})),
                                            tools(call("read_file", {"file_path": "b.txt"}, "call-2")),
                                            final("never")], [reader])
        with pytest.raises(Exception) as stopped:
            await _run(turn, orchestrator([delegate(), final("never")], [worker]))
        assert turn_failure_reason(stopped.value) == "agent_turn_cancelled"
        assert reader.seen == ["a.txt"]  # no later child effect
        (child,) = await _runs(jobs, CHILD_TURN_JOB_KIND)
        assert child.status == "blocked" and [(n, s) for n, s, _ in _effects(child)] == [("read_file", "succeeded")]
        assert await turn.settle("cancelled", reason="agent_turn_cancelled")


async def test_a_revoked_root_or_a_changed_epoch_fences_the_child_and_its_adoption(accounting_db, real_auth, delegating):  # noqa: F811
    from datetime import datetime, timezone
    from sqlalchemy import update
    from src.db.models import OperatorSession, RuntimeCompositionState
    jobs = DurableJobRepository()
    async with delegation_host(jobs) as host:
        operator, turn = await _turn(host, jobs)

        def revoke(_path):
            async def write():
                async with jobs._session() as db:
                    await db.execute(update(OperatorSession).where(OperatorSession.id == operator.session_id)
                                     .values(revoked_at=datetime.now(timezone.utc)))
                    await db.commit()
            turn.run_sync(write())
        worker = specialist("file_worker", [tools(call("read_file", {"file_path": "a.txt"})), final("never")],
                            [ReadFile(revoke)])
        with pytest.raises(Exception) as stopped:
            await _run(turn, orchestrator([delegate(), final("never")], [worker]))
        assert turn_failure_reason(stopped.value) == "agent_turn_child_blocked"
        assert turn.child_block == ("file_worker", "native_original_root_inactive")
        (child,) = await _runs(jobs, CHILD_TURN_JOB_KIND)
        assert child.status != "succeeded"

        # A fresh Root; the delegation owner moves between the child's settlement and its adoption.
        operator, turn = await _turn(host, jobs, message_id="m-2")
        settle = CordisTurn.settle

        async def settle_then_move(self, outcome, **kwargs):
            done = await settle(self, outcome, **kwargs)
            if self.parent is not None and done:
                async with jobs._session() as db:
                    await db.execute(update(RuntimeCompositionState).where(
                        RuntimeCompositionState.runtime_domain == DELEGATION).values(epoch=RuntimeCompositionState.epoch + 3))
                    await db.commit()
            return done
        CordisTurn.settle = settle_then_move
        try:
            worker = specialist("file_worker", [final("child report")], [ReadFile()])
            with pytest.raises(Exception) as moved:
                await _run(turn, orchestrator([delegate(), final("never adopted")], [worker]))
        finally:
            CordisTurn.settle = settle
        assert turn_failure_reason(moved.value) == "agent_turn_child_blocked"
        assert turn.child_block == ("file_worker", "agent_turn_epoch_changed")


async def test_delegation_turns_need_the_delegation_owner_on_cordis(accounting_db, real_auth, delegating):  # noqa: F811
    from src.runtime_plugins.agent_turns import chat_runtime_status
    from src.runtime_plugins.cutover import transition_domain
    jobs = DurableJobRepository()
    async with delegation_host(jobs) as host:
        assert (await chat_runtime_status(host))["loop"] == "cordis.agent-loop.v1"
        await transition_domain(DELEGATION, "legacy", host=host, actor="operator:test")
        status = await chat_runtime_status(host)
        assert (status["loop"], status["reason"]) == ("blocked", "delegation_owner_not_ready")
        assert "use_delegation" in status["recovery"]
        with pytest.raises(TurnBlocked) as refused:
            await _turn(host, jobs)
        assert refused.value.reason_code == "delegation_owner_not_ready"
        assert timedelta(seconds=30) == SETTLEMENT_GRACE


class Lookup(Tool):
    name = "lookup"
    description = "Look up a record on the fixture MCP server."
    inputs = {"key": {"type": "string", "description": "key"}}
    output_type = "string"

    def forward(self, key):
        return f"record {key}"


class StoreSecret(Tool):
    name = "store_secret"
    description = "Store a secret."
    inputs = {"key": {"type": "string", "description": "key"}, "value": {"type": "string", "description": "value"}}
    output_type = "string"

    def __init__(self):
        super().__init__()
        self.calls = 0

    def forward(self, key, value):
        self.calls += 1
        return "stored"


async def test_rule_presets_bind_mcp_and_workflow_specialists_to_their_admission_surface(accounting_db, real_auth, delegating, monkeypatch):  # noqa: F811
    from src.tools import mcp_manager as mcp_module
    manager = mcp_module.mcp_manager
    monkeypatch.setattr(manager, "get_config", lambda: [{"name": "fixture", "description": "Fixture records"}])
    monkeypatch.setattr(manager, "is_connected", lambda name: name == "fixture")
    monkeypatch.setattr(manager, "get_server_tools", lambda name: [Lookup()] if name == "fixture" else [])
    jobs = DurableJobRepository()
    async with delegation_host(jobs) as host:
        _, turn = await _turn(host, jobs)
        mcp = specialist("mcp_fixture", [tools(call("lookup", {"key": "a"})), final("record a")], [Lookup()])
        agent = orchestrator([delegate("mcp_fixture"), final("Found record a.")], [mcp])
        assert await _run(turn, agent) == "Found record a."
        (child,) = await _runs(jobs, CHILD_TURN_JOB_KIND)
        assert json.loads(child.checkpoint_context_json)["preset_id"] == "specialist.mcp"
        assert child.status == "succeeded" and [(n, s) for n, s, _ in _effects(child)] == [("lookup", "succeeded")]
        # A tool the server's filtered surface does not offer is outside the rule preset.
        _, turn = await _turn(host, jobs, message_id="m-2")
        widened = specialist("mcp_fixture", [], [Lookup(), ReadFile()])
        with pytest.raises(Exception) as stopped:
            await _run(turn, orchestrator([delegate("mcp_fixture")], [widened]))
        assert turn_failure_reason(stopped.value) == "agent_turn_child_blocked"
        assert turn.child_block == ("mcp_fixture", "agent_preset_tools_exceeded")
        # workflow_runner's rule: only workflow tools.
        from src.agent.presets import PresetError, require_child
        require_child("specialist.workflow_runner", name="workflow_runner", runtime_path="workflow_runner",
                      temperature=0.2, max_steps=6, tool_names=["workflow_daily_review"])
        with pytest.raises(PresetError):
            require_child("specialist.workflow_runner", name="workflow_runner", runtime_path="workflow_runner",
                          temperature=0.2, max_steps=6, tool_names=["workflow_daily_review", "write_file"])


async def test_store_secret_stays_denied_inside_a_delegated_vault_keeper(accounting_db, real_auth, delegating):  # noqa: F811
    secret = "VAULT_SECRET_CANARY_1020_child"
    jobs = DurableJobRepository()
    async with delegation_host(jobs) as host:
        _, turn = await _turn(host, jobs)
        store = StoreSecret()
        keeper = specialist("vault_keeper", [tools(call("store_secret", {"key": "k", "value": secret})),
                                             final("could not store")], [store], temperature=0.2, max_steps=3)
        agent = orchestrator([delegate("vault_keeper", task="store my key"), final("Use Settings.")], [keeper])
        assert await _run(turn, agent) == "Use Settings."
        assert store.calls == 0
        (child,) = await _runs(jobs, CHILD_TURN_JOB_KIND)
        async with jobs._session() as db:
            parent = await jobs._fetch(db, turn.job_id)
        for run in (child, parent):
            assert secret not in (run.effect_receipts_json or "") + (run.checkpoint_context_json or "")
        assert _effects(child) == []


async def test_measured_child_admission_overhead_p95_over_100_samples(accounting_db, real_auth, delegating):  # noqa: F811
    """The added Cordis cost of one delegated call, against #1019's 250 ms budget: the
    ``delegation.startChild`` step plus the child turn's admission, and its settlement."""
    import os
    import statistics
    import time
    samples = 110

    def p95(values):
        return statistics.quantiles(values, n=100)[94]
    jobs = DurableJobRepository()
    async with delegation_host(jobs) as host:
        _, turn = await _turn(host, jobs, timeout=600)
        admissions, settlements = [], []
        for index in range(samples):
            began = time.perf_counter()
            child = await turn.admit_child(name="file_worker", preset_id="specialist.file_worker", step_index=index + 1,
                                           call_index=0, child_index=0)
            admissions.append(time.perf_counter() - began)
            began = time.perf_counter()
            assert await child.settle("blocked", reason="measurement")
            settlements.append(time.perf_counter() - began)
    print("MEASURED child n=%d p50/p95 ms: admission %.1f/%.1f, settlement %.1f/%.1f" % (
        samples, statistics.median(admissions) * 1000, p95(admissions) * 1000,
        statistics.median(settlements) * 1000, p95(settlements) * 1000))
    if os.environ.get("SERAPH_CORDIS_LATENCY_BUDGET") == "1":
        assert p95(admissions) <= 0.250 and p95(settlements) <= 0.250
