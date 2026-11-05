"""#1020 PR2 parity: ``use_delegation`` chat through the real WS route on both loops.

The orchestrator and its real specialists (the agent factories' wrapped tools)
run one scripted conversation on the legacy loop and on the Cordis loop, where
each specialist is a durable child turn. The model is the real governed
``FallbackLiteLLMModel`` for the orchestrator and every specialist; only its
OpenRouter HTTP client is scripted, so nothing leaves the process.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from config.settings import settings
from tests.cordis_agent_loop import call, final, tools
from tests.cordis_research import needs_cordis
from tests.test_cordis_agent_loop_chat import ORIGIN, chat_env, shape, ws_turn
from tests.test_research_native_vertical import real_auth  # noqa: F401

pytestmark = [needs_cordis]


def _delegating(env, monkeypatch, loop):
    """use_delegation on; specialists use the same governed model; Cordis owns presets and delegation."""
    from src.runtime_plugins.cutover import transition_domain

    def specialist_model(temperature, runtime_path):
        from src.agent import factory
        return factory.get_model(runtime_path=runtime_path, temperature=temperature)
    monkeypatch.setattr(settings, "use_delegation", True)
    monkeypatch.setattr("src.agent.specialists._create_model", specialist_model)
    if loop == "cordis":
        for domain in ("seraph.agent-presets.v1", "seraph.delegation.v1"):
            env.run(transition_domain(domain, "cordis", host=env.host, actor="operator:test"))


def _children(env):
    from sqlalchemy import select
    from src.db.engine import get_session
    from src.db.models import WorkflowRunState
    from src.runtime_plugins.agent_turns import CHILD_TURN_JOB_KIND

    async def read():
        async with get_session() as db:
            rows = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.job_kind == CHILD_TURN_JOB_KIND)
                                     .order_by(WorkflowRunState.started_at))).scalars()
            return [(row.status, [((item.get("details") or {}).get("tool_name"), item.get("status"))
                                  for item in json.loads(row.effect_receipts_json or "[]")
                                  if item.get("effect_type") == "cordis_turn_tool"]) for row in rows]
    return env.client.portal.call(read)


def _run_both(tmp_path, monkeypatch, script, drive):
    outcomes = {}
    for loop in ("legacy", "cordis"):
        with monkeypatch.context() as scoped, chat_env(tmp_path, scoped, script(), loop=loop) as env:
            _delegating(env, scoped, loop)
            outcomes[loop] = drive(env)
    return outcomes["legacy"], outcomes["cordis"]


def test_ws_delegation_to_a_real_specialist_matches_legacy(tmp_path, monkeypatch, real_auth):  # noqa: F811
    def script():
        return [tools(call("file_worker", {"task": "Read notes.txt and report what it says."})),
                tools(call("read_file", {"file_path": "notes.txt"})), final("notes.txt says: hello from notes"),
                final("Your notes say hello.")]

    def drive(env):
        (env.workspace / "notes.txt").write_text("hello from notes")
        frames = ws_turn(env, "what do my notes say? ask the file worker")
        session_id = frames[-1]["session_id"]
        offered = [item["tools"] for item in env.model.calls]
        return (shape(frames), env.messages(session_id), len(env.model.calls), offered, env.turns(),
                env.tool_effects(), _children(env))
    legacy, cordis = _run_both(tmp_path, monkeypatch, script, drive)
    # The same frames (the orchestrator's delegation step included), transcript and model calls.
    assert legacy[:4] == cordis[:4] and cordis[2] == 4
    assert cordis[0][-1][:2] == ("final", "Your notes say hello.")
    assert "file_worker" in cordis[3][0] and "read_file" in cordis[3][1]
    assert cordis[4] == [("succeeded", None)] and cordis[5] == [("file_worker", "succeeded")]
    assert cordis[6] == [("succeeded", [("read_file", "succeeded")])]
    assert legacy[4] == [] and legacy[6] == []


def test_ws_child_approval_pauses_the_parent_and_the_resume_runs_the_write_once(tmp_path, monkeypatch, real_auth):  # noqa: F811
    def script():
        task = {"task": "Write ship it into plan.txt."}
        write = {"file_path": "plan.txt", "content": "ship it"}
        return [tools(call("file_worker", task)), tools(call("write_file", write)),
                tools(call("file_worker", task, "call-2")), tools(call("write_file", write, "call-2")),
                final("Wrote plan.txt."), final("Wrote the plan.")]

    def drive(env):
        with patch("src.tools.approval.get_tool_approval_behavior", return_value="always"):
            paused = ws_turn(env, "write ship it into plan.txt")
            pause = paused[-1]
            written_before = (env.workspace / "plan.txt").exists()
            approved = env.client.post(f"/api/approvals/{pause['approval_id']}/approve", headers=ORIGIN)
            assert approved.status_code == 200, approved.text
            resumed = ws_turn(env, approved.json()["resume_message"], session_id=pause["session_id"],
                              kind="resume_message")
        return (shape(paused), shape(resumed), written_before, (env.workspace / "plan.txt").read_text(),
                env.messages(pause["session_id"]), len(env.model.calls), env.turns(), env.tool_effects(),
                _children(env))
    legacy, cordis = _run_both(tmp_path, monkeypatch, script, drive)
    assert legacy[:6] == cordis[:6] and cordis[5] == 6
    paused, resumed, written_before, content = cordis[:4]
    assert paused[-1][0] == "approval_required" and written_before is False
    assert resumed[-1][:2] == ("final", "Wrote the plan.") and content == "ship it"
    # The parent paused with the gated call as no effect; the resumed turn's child ran it once.
    assert cordis[6] == [("blocked", "approval_required"), ("succeeded", None)]
    assert sorted(cordis[7]) == [("file_worker", "no_effect"), ("file_worker", "succeeded")]
    assert cordis[8] == [("blocked", [("write_file", "no_effect")]), ("succeeded", [("write_file", "succeeded")])]


def test_delegation_without_a_cordis_delegation_owner_fails_closed_visibly(tmp_path, monkeypatch, real_auth):  # noqa: F811
    with chat_env(tmp_path, monkeypatch, [final("never")], loop="cordis") as env:
        monkeypatch.setattr(settings, "use_delegation", True)
        frames = ws_turn(env, "delegate this research please")
        assert frames[-1]["type"] == "error" and frames[-1]["reason"] == "delegation_owner_not_ready"
        assert env.model.calls == [] and env.turns() == []
