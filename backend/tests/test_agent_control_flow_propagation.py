"""#1107: approval and clarification interrupts leave a real agent loop.

smolagents turns every tool exception into an observation. These tests drive
Seraph's real ``ToolCallingAgent`` through the production chat transports and
agent factory, with the real ``FallbackLiteLLMModel`` canonical preflight and
route selection. Only the governed OpenRouter transport (the owned boundary
below the model), the proof store and admission bookkeeping are scripted, so
no provider is contacted and nothing is spent.
"""
from __future__ import annotations

import json
import threading
import time
from contextlib import ExitStack
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from smolagents import Tool
from smolagents.models import ChatMessage, Model

from config.settings import settings
from src.agent.exceptions import AgentControlFlowInterrupt, ClarificationRequired
from src.agent.tool_calling_agent import ToolCallingAgent
from src.approval.exceptions import ApprovalRequired
from src.security.trust_contract import AuthorityGrant, EgressClass, PrincipalType, TrustPrincipal

PRINCIPAL = TrustPrincipal(principal_id="operator:test", principal_type=PrincipalType.OPERATOR,
                           grants=(AuthorityGrant.MODEL_INFERENCE,))


# ---------------------------------------------------------------- scripted owned transport

def _is_observation(message: dict) -> bool:
    """A tool result in the body, whatever role spelling the transport used."""
    role = str(message.get("role", ""))
    content = message.get("content")
    text = " ".join(part.get("text", "") for part in content if isinstance(part, dict)) \
        if isinstance(content, list) else str(content or "")
    return role in {"tool", "tool-response"} or "TOOL_RESPONSE" in role or text.startswith("Observation:")


class ScriptedProvider:
    """The provider behind the governed transport's own HTTP client.

    Requests leave ``_governed_openai_chat_completion`` exactly as they would
    for OpenRouter; an ``httpx.MockTransport`` answers them, so the real
    request serialization and response parsing both run. One tool call per
    turn: ``tool`` until the run has an observation for it, then
    ``final_answer``.
    """

    def __init__(self, tool: str, arguments: dict[str, Any]):
        self.tool, self.arguments = tool, arguments
        self.bodies: list[dict] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "openrouter.ai", request.url
        body = json.loads(request.content)
        self.bodies.append(body)
        observed = any(_is_observation(message) for message in body["messages"])
        name, arguments = ("final_answer", {"answer": "done"}) if observed else (self.tool, self.arguments)
        call = {"id": f"call-{len(self.bodies)}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)}}
        return httpx.Response(200, json={
            "id": f"gen-{len(self.bodies)}", "object": "chat.completion", "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "tool_calls",
                         "message": {"role": "assistant", "content": None, "tool_calls": [call]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.000001}})

    def offered_tools(self) -> set[str]:
        return {tool["function"]["name"] for tool in self.bodies[0].get("tools", [])}


def _openrouter_route():
    from src.model_fabric import candidate_from_profile
    from src.model_fabric.configuration import (
        OPENROUTER_SETUP_V2_SCHEMA_VERSION, OpenRouterRoute, OpenRouterSetup, openrouter_profiles_for_setup,
    )
    from src.model_fabric.proofs import build_model_route_proof
    route = OpenRouterRoute(model_id="openrouter/anthropic/claude-sonnet-4", enabled=True,
                            capabilities=("text", "tool_use", "structured_output", "streaming"),
                            allowed_upstreams=("anthropic",), temperature=0.7, max_output_tokens=4096,
                            timeout_seconds=120.0, zero_data_retention=False, request_cost_bound_microusd=50_000)
    setup = OpenRouterSetup(schema_version=OPENROUTER_SETUP_V2_SCHEMA_VERSION,
                            routes={"text": route, "vision": None, "embedding": None}, purpose_consents={},
                            egress_class=EgressClass.CLOUD_ALLOWED_FULL, cloud_egress_acknowledged=True,
                            spend_ceiling_microusd=5_000_000)
    (profile,) = openrouter_profiles_for_setup(setup)
    candidate, now = candidate_from_profile(profile), time.time()
    values = {capability: "supported" for capability in profile.capabilities}
    values.update({"health": "healthy", "latency_ms": 900})
    proofs = {capability: build_model_route_proof(
        profile=profile, endpoint_class=candidate.endpoint_class, adapter=candidate.adapter,
        capability=capability, canary_version="seraph.manual-canary.v1", outcome="passed",
        checked_at=now - 60, expires_at=now + 86_000, probe_receipt_id=f"probe-{capability}",
        probe_receipt_hash="c" * 64, proven_value=value) for capability, value in values.items()}
    return setup, profile, proofs


@pytest.fixture
def scripted_chat_model(monkeypatch):
    """The production chat agent with a real model whose provider is scripted."""
    from src import llm_runtime
    from src.model_fabric.configuration import openrouter_policy_for_setup
    from src.model_fabric.repository import model_fabric_repository

    setup, profile, proofs = _openrouter_route()
    monkeypatch.setattr(settings, "openrouter_api_key", "fixture-openrouter-key")
    monkeypatch.setattr("src.api.chat.should_use_direct_local_chat", lambda *a, **k: False)
    monkeypatch.setattr("src.api.ws.should_use_direct_local_chat", lambda *a, **k: False, raising=False)
    monkeypatch.setattr("src.api.chat.get_current_trust_principal", lambda: PRINCIPAL)

    def model(**_kwargs):
        return llm_runtime.FallbackLiteLLMModel(model_id=profile.routing_model, api_base=profile.api_base,
                                                api_key="fixture-openrouter-key", runtime_profile=profile.id,
                                                runtime_path="chat_agent", max_tokens=512)

    async def latest_capability_proof(*, capability, **_kwargs):
        return proofs.get(capability)

    providers: list[ScriptedProvider] = []

    real_client = httpx.Client

    def script(tool: str, arguments: dict[str, Any]) -> ScriptedProvider:
        provider = ScriptedProvider(tool, arguments)
        providers.append(provider)

        def client(*args, **kwargs):
            return real_client(*args, **{**kwargs, "transport": httpx.MockTransport(provider.handle)})
        # Only the governed transport builds a synchronous httpx.Client here.
        stack.enter_context(patch.object(httpx, "Client", client))
        return provider

    with ExitStack() as stack:
        for target in (
            patch("src.agent.factory.get_model", side_effect=model),
            patch.object(llm_runtime, "provider_profiles", return_value={profile.id: profile}),
            patch.object(llm_runtime, "_fallback_targets", return_value=[]),
            patch("src.model_fabric.caller_context.effective_workload_policy",
                  return_value=openrouter_policy_for_setup(setup, "chat_agent")),
            patch.object(model_fabric_repository, "latest_capability_proof", side_effect=latest_capability_proof),
            patch.object(llm_runtime, "_execute_sync_with_gpu_admission", side_effect=lambda **kw: kw["operation"]()),
            patch.object(llm_runtime, "_new_route_receipt_session", return_value=None),
            patch.object(llm_runtime, "_can_log_request", return_value=False),
        ):
            stack.enter_context(target)
        yield script


@pytest.fixture
def counted_run_command(monkeypatch):
    from src.tools.process_tools import ProcessRuntimeManager
    calls: list[dict] = []
    real = ProcessRuntimeManager.run_command

    def counting(self, **kwargs):
        calls.append(kwargs)
        return real(self, **kwargs)
    monkeypatch.setattr(ProcessRuntimeManager, "run_command", counting)
    return calls


async def _onboarded():
    from src.profile.service import mark_onboarding_complete
    await mark_onboarding_complete()


@pytest.fixture
def recorded_generate(monkeypatch):
    """The (messages, tools) each real model turn received, for body comparison."""
    from src import llm_runtime
    turns: list[tuple[list, list]] = []
    real = llm_runtime.FallbackLiteLLMModel.generate

    def recording(self, messages, *args, **kwargs):
        turns.append((list(messages), list(kwargs.get("tools_to_call_from") or [])))
        return real(self, messages, *args, **kwargs)
    monkeypatch.setattr(llm_runtime.FallbackLiteLLMModel, "generate", recording)
    return turns


_OPENAI_ROLES = {"system", "user", "assistant", "tool"}


def _assert_bodies_match_litellm(provider: ScriptedProvider, turns: list[tuple[list, list]]) -> None:
    """Each governed body carries what smolagents' LiteLLMModel would send."""
    from smolagents import LiteLLMModel
    reference = LiteLLMModel(model_id="openrouter/anthropic/claude-sonnet-4")
    assert len(provider.bodies) == len(turns)
    for body, (messages, tools) in zip(provider.bodies, turns):
        assert {message["role"] for message in body["messages"]} <= _OPENAI_ROLES, body["messages"]
        expected = reference._prepare_completion_kwargs(messages=messages, tools_to_call_from=tools,
                                                        convert_images_to_image_urls=True)
        assert body["messages"] == json.loads(json.dumps(expected["messages"]))
        assert body["tools"] == json.loads(json.dumps(expected["tools"]))
        assert body["tool_choice"] == expected["tool_choice"] == "required"


def _write_soul(marker: str) -> None:
    from pathlib import Path
    Path(settings.workspace_dir, "soul.md").write_text(f"# Soul\n\n{marker}\n", encoding="utf-8")


# ---------------------------------------------------------------- REST

@pytest.mark.asyncio
async def test_rest_real_tool_turn_executes_and_returns_the_final_answer(
        client, scripted_chat_model, recorded_generate):
    await _onboarded()
    _write_soul("MARKER-1107-REST")
    provider = scripted_chat_model("view_soul", {})
    response = await client.post("/api/chat", json={"message": "who am I?"})
    assert response.status_code == 200, response.text
    assert response.json()["response"] == "done"
    assert len(provider.bodies) == 2 and "view_soul" in provider.offered_tools()
    # The provider's tool_call was parsed, the tool ran, and its observation was sent back.
    observation = [message for message in provider.bodies[1]["messages"] if _is_observation(message)]
    assert observation and "MARKER-1107-REST" in json.dumps(observation)
    _assert_bodies_match_litellm(provider, recorded_generate)


@pytest.mark.asyncio
async def test_rest_high_risk_tool_pauses_for_approval_then_runs_exactly_once(
        client, scripted_chat_model, counted_run_command, recorded_generate):
    from src.approval.repository import approval_repository
    await _onboarded()
    provider = scripted_chat_model("run_command", {"command": "pwd"})

    paused = await client.post("/api/chat", json={"message": "where am I?"})
    assert paused.status_code == 409, paused.text
    detail = paused.json()["detail"]
    assert detail["type"] == "approval_required" and detail["tool_name"] == "run_command"
    assert counted_run_command == []
    assert "run_command" in provider.offered_tools()
    # The pause left the loop: no further model turn saw a tool-error observation.
    assert len(provider.bodies) == 1
    pending = await approval_repository.get(detail["approval_id"])
    assert pending.status == "pending"
    assert json.loads(pending.details_json)["resume_message"] == "where am I?"

    approved = await client.post(f"/api/approvals/{detail['approval_id']}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["resume_message"] == "where am I?"

    resumed = await client.post("/api/chat", json={"message": approved.json()["resume_message"],
                                                    "session_id": approved.json()["session_id"]})
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["response"] == "done"
    assert len(counted_run_command) == 1 and counted_run_command[0]["command"] == "pwd"

    # The approval was consumed: the same request pauses again instead of running twice.
    again = await client.post("/api/chat", json={"message": "where am I?",
                                                 "session_id": approved.json()["session_id"]})
    assert again.status_code == 409 and again.json()["detail"]["approval_id"] != detail["approval_id"]
    assert len(counted_run_command) == 1
    _assert_bodies_match_litellm(provider, recorded_generate)


@pytest.mark.asyncio
async def test_rest_clarify_returns_the_clarification_and_persists_the_row(client, scripted_chat_model):
    await _onboarded()
    provider = scripted_chat_model("clarify", {"question": "Which city should I check?",
                                               "reason": "Weather depends on location.",
                                               "options": "Wroclaw, Warsaw"})
    response = await client.post("/api/chat", json={"message": "What is the weather?"})
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["type"] == "clarification_required"
    assert detail["question"] == "Which city should I check?"
    assert detail["options"] == ["Wroclaw", "Warsaw"]
    assert len(provider.bodies) == 1
    history = (await client.get(f"/api/sessions/{detail['session_id']}/messages")).json()
    assert history[-1]["metadata"]["display_role"] == "clarification"
    assert history[-1]["metadata"]["options"] == ["Wroclaw", "Warsaw"]


# ---------------------------------------------------------------- agent loop semantics

class _ScriptedModel(Model):
    """A smolagents model that replays tool calls, one list per turn."""

    def __init__(self, turns: list[list[tuple[str, dict]]]):
        super().__init__(model_id="scripted")
        self.turns, self.seen = list(turns), []

    def generate(self, messages, stop_sequences=None, response_format=None, tools_to_call_from=None, **kwargs):
        self.seen.append(messages)
        calls = self.turns.pop(0) if self.turns else [("final_answer", {"answer": "done"})]
        return ChatMessage.from_dict({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"t{len(self.seen)}-{index}", "type": "function",
             "function": {"name": name, "arguments": arguments}} for index, (name, arguments) in enumerate(calls)]})


class _RaisingTool(Tool):
    name = "probe"
    description = "Raises the configured exception."
    inputs = {"value": {"type": "string", "description": "anything"}}
    output_type = "string"

    def __init__(self, error: BaseException | None = None):
        super().__init__()
        self.error, self.calls = error, 0

    def forward(self, value: str) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return f"ok:{value}"


def _approval() -> ApprovalRequired:
    return ApprovalRequired(approval_id="a-1", session_id="s-1", tool_name="probe", risk_level="high",
                            summary="Calling tool: probe")


def _agent(tools, turns, **kwargs) -> ToolCallingAgent:
    return ToolCallingAgent(tools=tools, model=_ScriptedModel(turns), max_steps=4, verbosity_level=0, **kwargs)


@pytest.mark.parametrize("error", [_approval(), ClarificationRequired(question="Which city?")])
def test_interrupts_leave_the_agent_loop_unchanged(error):
    tool = _RaisingTool(error)
    agent = _agent([tool], [[("probe", {"value": "x"})]])
    with pytest.raises(type(error)) as raised:
        agent.run("go")
    assert raised.value is error and isinstance(raised.value, AgentControlFlowInterrupt)
    assert tool.calls == 1 and len(agent.model.seen) == 1  # no further model turn


def test_ordinary_tool_errors_still_become_observations():
    tool = _RaisingTool(RuntimeError("disk on fire"))
    agent = _agent([tool], [[("probe", {"value": "x"})]])
    assert agent.run("go") == "done"
    assert len(agent.model.seen) == 2
    assert "disk on fire" in json.dumps([str(m) for m in agent.model.seen[1]])


def test_interrupt_from_a_managed_sub_agent_reaches_the_parent_caller():
    inner_tool = _RaisingTool(_approval())
    inner = ToolCallingAgent(tools=[inner_tool], model=_ScriptedModel([[("probe", {"value": "x"})]]),
                             name="worker", description="does work", max_steps=3, verbosity_level=0)
    parent = _agent([], [[("worker", {"task": "do it"})]], managed_agents=[inner])
    with pytest.raises(ApprovalRequired):
        parent.run("delegate")
    assert inner_tool.calls == 1 and len(parent.model.seen) == 1


def test_interrupt_among_parallel_tool_calls_propagates():
    approval_tool, ok_tool = _RaisingTool(_approval()), _RaisingTool()
    ok_tool.name = "fine"
    agent = _agent([approval_tool, ok_tool], [[("probe", {"value": "x"}), ("fine", {"value": "y"})]])
    with pytest.raises(ApprovalRequired):
        agent.run("go")
    assert len(agent.model.seen) == 1


def test_every_agent_factory_builds_the_interrupt_aware_agent():
    import ast
    from pathlib import Path
    import src.agent.factory as factory
    import src.agent.onboarding as onboarding
    import src.agent.specialists as specialists
    import src.agent.strategist as strategist
    for module in (factory, onboarding, specialists, strategist):
        assert module.ToolCallingAgent is ToolCallingAgent, module.__name__
    # No other module may reach smolagents' agent classes, by any import form.
    offenders = {path.as_posix(): _smolagents_agent_references(path.read_text(encoding="utf-8"))
                 for path in Path("src").rglob("*.py") if path.as_posix() != "src/agent/tool_calling_agent.py"}
    assert {path: refs for path, refs in offenders.items() if refs} == {}


_AGENT_CLASSES = frozenset({"ToolCallingAgent", "CodeAgent", "MultiStepAgent"})


def _smolagents_agent_references(source: str) -> list[str]:
    """Every way a module can name smolagents' agent classes or its agents module."""
    import ast
    tree = ast.parse(source)
    module_aliases: set[str] = set()   # names bound to the smolagents package
    agents_aliases: set[str] = set()   # names bound to smolagents.agents
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "smolagents":
                    module_aliases.add(alias.asname or "smolagents")
                elif alias.name == "smolagents.agents":
                    found.append("import smolagents.agents")
                    agents_aliases.add(alias.asname or "smolagents")
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "smolagents":
            if node.module == "smolagents.agents":
                found.append(f"from {node.module} import ...")
            for alias in node.names:
                if alias.name in _AGENT_CLASSES or (node.module == "smolagents" and alias.name == "agents"):
                    found.append(f"from {node.module} import {alias.name}")
                    if alias.name == "agents":
                        agents_aliases.add(alias.asname or "agents")
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id in module_aliases and (node.attr in _AGENT_CLASSES or node.attr == "agents"):
                found.append(f"{node.value.id}.{node.attr}")
            elif node.value.id in agents_aliases and node.attr in _AGENT_CLASSES:
                found.append(f"{node.value.id}.{node.attr}")
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if name in {"__import__", "import_module"} and str(node.args[0].value).startswith("smolagents.agents"):
                found.append(f"{name}({node.args[0].value!r})")
    return found


@pytest.mark.parametrize("source", [
    "from smolagents import ToolCallingAgent",
    "from smolagents import CodeAgent as Agent",
    "from smolagents.agents import ToolCallingAgent",
    "from smolagents.agents import MultiStepAgent",
    "from smolagents import agents\nagents.ToolCallingAgent",
    "import smolagents\nsmolagents.ToolCallingAgent(tools=[], model=None)",
    "import smolagents as sa\nsa.CodeAgent",
    "import smolagents.agents",
    "import importlib\nimportlib.import_module('smolagents.agents')",
])
def test_agent_import_guard_catches_every_import_form(source):
    assert _smolagents_agent_references(source)


def test_agent_import_guard_allows_unrelated_smolagents_imports():
    assert _smolagents_agent_references(
        "from smolagents import Tool, tool\nimport smolagents\nsmolagents.Tool\nfrom smolagents.models import ChatMessage") == []


def test_tools_are_serialized_by_their_calling_schema_not_their_source_export():
    from src import llm_runtime
    from src.tools.approval import ApprovalTool
    from smolagents.models import get_tool_json_schema
    wrapped = ApprovalTool(_RaisingTool())
    assert llm_runtime._transport_json_value([wrapped]) == [get_tool_json_schema(wrapped)]


# ---------------------------------------------------------------- WebSocket

def _ws_frames(ws, *, until: set[str], limit: int = 40) -> list[dict]:
    from tests.test_e2e_conversation import _receive_text
    frames = []
    for _ in range(limit):
        frame = json.loads(_receive_text(ws, timeout_seconds=20.0))
        frames.append(frame)
        if frame["type"] in until:
            return frames
    raise AssertionError(f"no {until} frame in {[frame['type'] for frame in frames]}")


def _ws_send(ws, message: str, session_id: str | None):
    ws.send_text(json.dumps({"type": "message", "message": message, "session_id": session_id}))


def test_ws_high_risk_tool_sends_approval_required_then_runs_exactly_once(
        scripted_chat_model, counted_run_command):
    from tests.test_websocket import _close_sync_client_with_db, _make_sync_client_with_db
    client, patches, stack = _make_sync_client_with_db()
    try:
        provider = scripted_chat_model("run_command", {"command": "pwd"})
        with patch("src.memory.consolidator.consolidate_session"), client.websocket_connect("/ws/chat") as ws:
            _ws_frames(ws, until={"proactive"})
            ws.send_text(json.dumps({"type": "skip_onboarding"}))
            _ws_frames(ws, until={"status", "proactive", "onboarding_skipped", "final"}, limit=3)
            _ws_send(ws, "where am I?", None)
            frames = _ws_frames(ws, until={"approval_required", "final", "error"})
            pause = frames[-1]
            assert pause["type"] == "approval_required", frames
            assert pause["tool_name"] == "run_command" and "continue automatically" in pause["content"]
            assert counted_run_command == [] and len(provider.bodies) == 1

            approved = client.post(f"/api/approvals/{pause['approval_id']}/approve")
            assert approved.status_code == 200, approved.text
            assert approved.json()["resume_message"] == "where am I?"  # persisted by the WS handler

            _ws_send(ws, approved.json()["resume_message"], pause["session_id"])
            frames = _ws_frames(ws, until={"approval_required", "final", "error"})
            assert frames[-1]["type"] == "final" and frames[-1]["content"] == "done", frames
            assert len(counted_run_command) == 1

            _ws_send(ws, "where am I?", pause["session_id"])
            frames = _ws_frames(ws, until={"approval_required", "final", "error"})
            assert frames[-1]["type"] == "approval_required"
            assert frames[-1]["approval_id"] != pause["approval_id"] and len(counted_run_command) == 1
    finally:
        _close_sync_client_with_db(patches, stack)


def test_ws_clarify_sends_the_clarification_and_persists_the_row(scripted_chat_model):
    from tests.test_websocket import _close_sync_client_with_db, _make_sync_client_with_db
    client, patches, stack = _make_sync_client_with_db()
    try:
        provider = scripted_chat_model("clarify", {"question": "Which city should I check?",
                                                   "reason": "Weather depends on location.",
                                                   "options": "Wroclaw, Warsaw"})
        with patch("src.memory.consolidator.consolidate_session"), client.websocket_connect("/ws/chat") as ws:
            _ws_frames(ws, until={"proactive"})
            ws.send_text(json.dumps({"type": "skip_onboarding"}))
            _ws_frames(ws, until={"status", "proactive", "onboarding_skipped", "final"}, limit=3)
            _ws_send(ws, "What is the weather?", None)
            frames = _ws_frames(ws, until={"clarification_required", "final", "error"})
            frame = frames[-1]
            assert frame["type"] == "clarification_required", frames
            assert frame["question"] == "Which city should I check?" and frame["options"] == ["Wroclaw", "Warsaw"]
            assert len(provider.bodies) == 1
        history = client.get(f"/api/sessions/{frame['session_id']}/messages").json()
        assert history[-1]["metadata"]["display_role"] == "clarification"
    finally:
        _close_sync_client_with_db(patches, stack)


def test_ws_real_tool_turn_executes_and_returns_the_final_answer(scripted_chat_model, recorded_generate):
    from tests.test_websocket import _close_sync_client_with_db, _make_sync_client_with_db
    client, patches, stack = _make_sync_client_with_db()
    try:
        _write_soul("MARKER-1107-WS")
        provider = scripted_chat_model("view_soul", {})
        with patch("src.memory.consolidator.consolidate_session"), client.websocket_connect("/ws/chat") as ws:
            _ws_frames(ws, until={"proactive"})
            ws.send_text(json.dumps({"type": "skip_onboarding"}))
            _ws_frames(ws, until={"status", "proactive", "onboarding_skipped", "final"}, limit=3)
            _ws_send(ws, "who am I?", None)
            frames = _ws_frames(ws, until={"final", "error", "approval_required"})
        assert frames[-1]["type"] == "final" and frames[-1]["content"] == "done", frames
        assert any(frame["type"] == "step" and "view_soul" in frame["content"] for frame in frames), frames
        observation = [message for message in provider.bodies[1]["messages"] if _is_observation(message)]
        assert observation and "MARKER-1107-WS" in json.dumps(observation)
        _assert_bodies_match_litellm(provider, recorded_generate)
    finally:
        _close_sync_client_with_db(patches, stack)


# ---------------------------------------------------------------- plain-message callers keep their exact bodies

def _model(*, flatten: bool):
    from src import llm_runtime
    return llm_runtime.FallbackLiteLLMModel(model_id="openrouter/anthropic/claude-sonnet-4",
                                            api_base="https://openrouter.ai/api/v1", api_key="fixture",
                                            runtime_path="chat_agent", flatten_messages_as_text=flatten)


@pytest.mark.parametrize("flatten", [True, False])
@pytest.mark.parametrize("messages", [
    [{"role": "system", "content": "You draft replies."}, {"role": "user", "content": "Reply to Ann."}],
    [{"role": "user", "content": "one"}, {"role": "user", "content": "two"}],
    [{"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
                                                            "function": {"name": "f", "arguments": "{}"}}]},
     {"role": "tool", "tool_call_id": "c1", "content": "result"}],
])
def test_plain_openai_messages_are_sent_byte_identical(flatten, messages):
    from src import llm_runtime
    legacy = [llm_runtime._openai_message_payload(message) for message in messages]
    prepared = _model(flatten=flatten)._provider_messages(messages)
    assert json.dumps(prepared, sort_keys=True) == json.dumps(legacy, sort_keys=True)


@pytest.mark.parametrize("flatten", [True, False])
def test_agent_memory_matches_litellm_including_flatten_models(flatten):
    from smolagents import LiteLLMModel
    from smolagents.models import MessageRole
    memory = [
        ChatMessage(role=MessageRole.SYSTEM, content=[{"type": "text", "text": "system prompt"}]),
        ChatMessage(role=MessageRole.USER, content=[{"type": "text", "text": "New task:\nhi"}]),
        ChatMessage(role=MessageRole.TOOL_CALL, content=[{"type": "text", "text": "Calling tools: view_soul"}]),
        ChatMessage(role=MessageRole.TOOL_RESPONSE, content=[{"type": "text", "text": "Observation:\nsoul"}]),
        ChatMessage(role=MessageRole.ASSISTANT, content="a plain string turn"),
    ]
    prepared = _model(flatten=flatten)._provider_messages(memory)
    assert {message["role"] for message in prepared} <= _OPENAI_ROLES
    reference = LiteLLMModel(model_id="openrouter/anthropic/claude-sonnet-4", flatten_messages_as_text=flatten)
    wrapped = [ChatMessage(role=m.role, content=[{"type": "text", "text": m.content}]) if isinstance(m.content, str)
               else m for m in memory]
    expected = reference._prepare_completion_kwargs(messages=wrapped, convert_images_to_image_urls=True)["messages"]
    assert prepared == json.loads(json.dumps(expected))


def test_plain_string_generate_on_a_flatten_model_sends_the_legacy_body(scripted_chat_model, monkeypatch):
    """A direct caller (mail reply, calendar) on a flatten route: unchanged body, no exception."""
    from src import llm_runtime
    _setup, profile, _proofs = _openrouter_route()
    principal = TrustPrincipal(principal_id="operator:test", principal_type=PrincipalType.OPERATOR,
                               grants=(AuthorityGrant.MODEL_INFERENCE,), session_id="session-1107")
    monkeypatch.setattr(llm_runtime, "get_current_trust_principal", lambda: principal)
    provider = scripted_chat_model("final_answer", {"answer": "ignored"})
    model = llm_runtime.FallbackLiteLLMModel(model_id=profile.routing_model, api_base=profile.api_base,
                                             api_key="fixture-openrouter-key", runtime_profile=profile.id,
                                             runtime_path="chat_agent", max_tokens=512,
                                             flatten_messages_as_text=True)
    messages = [{"role": "system", "content": "You draft replies."}, {"role": "user", "content": "Reply to Ann."}]
    model.generate(messages)
    assert provider.bodies[0]["messages"] == messages


# ---------------------------------------------------------------- revocation inside a tool (#1116)

def test_revocation_raised_by_a_tool_leaves_the_agent_loop():
    from src.auth.cancellation import RuntimeRevokedError
    error = RuntimeRevokedError("authenticated operator session was revoked")
    tool = _RaisingTool(error)
    agent = _agent([tool], [[("probe", {"value": "x"})]])
    with pytest.raises(RuntimeRevokedError) as raised:
        agent.run("go")
    assert raised.value is error and isinstance(raised.value, PermissionError)
    assert len(agent.model.seen) == 1


@pytest.fixture
def revoke_after_model_reply(monkeypatch):
    """Revoke the operator session after the provider replied, before the tool runs.

    The real tool wrapper then observes the revocation itself.
    """
    from src import llm_runtime
    from src.auth import cancellation
    real = llm_runtime._governed_agent_chat_message

    def revoking(*args, **kwargs):
        message = real(*args, **kwargs)
        guard = cancellation._guard.get()
        if guard is None:
            # The auth-bypass REST test client has no session cookie, so no
            # watcher installed a guard; install the one a cookie would.
            guard = threading.Event()
            cancellation._guard.set(guard)
        guard.set()
        return message
    monkeypatch.setattr(llm_runtime, "_governed_agent_chat_message", revoking)


@pytest.mark.asyncio
async def test_rest_revocation_inside_a_tool_returns_session_revoked_without_another_model_turn(
        client, scripted_chat_model, recorded_generate, revoke_after_model_reply):
    await _onboarded()
    _write_soul("MARKER-1116")
    provider = scripted_chat_model("view_soul", {})
    response = await client.post("/api/chat", json={"message": "who am I?"})
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "session_revoked"
    # One model turn, one provider request: the revoking tool call ended the run.
    assert len(recorded_generate) == 1 and len(provider.bodies) == 1


def test_ws_revocation_inside_a_tool_closes_the_socket_without_another_model_turn(
        scripted_chat_model, recorded_generate, revoke_after_model_reply, monkeypatch):
    from starlette.websockets import WebSocketDisconnect
    from src.api import ws as ws_module
    from tests.test_websocket import _close_sync_client_with_db, _make_sync_client_with_db
    outcomes: list[str] = []
    real_log = ws_module.log_agent_run_event

    async def record_outcome(**kwargs):
        outcomes.append(kwargs["outcome"])
        await real_log(**kwargs)
    monkeypatch.setattr(ws_module, "log_agent_run_event", record_outcome)
    client, patches, stack = _make_sync_client_with_db()
    try:
        _write_soul("MARKER-1116-WS")
        provider = scripted_chat_model("view_soul", {})
        frames: list[dict] = []
        with patch("src.memory.consolidator.consolidate_session"), client.websocket_connect("/ws/chat") as ws:
            _ws_frames(ws, until={"proactive"})
            ws.send_text(json.dumps({"type": "skip_onboarding"}))
            _ws_frames(ws, until={"status", "proactive", "onboarding_skipped", "final"}, limit=3)
            _ws_send(ws, "who am I?", None)
            from tests.test_e2e_conversation import _receive_text
            with pytest.raises(WebSocketDisconnect) as closed:
                for _ in range(20):
                    frames.append(json.loads(_receive_text(ws, timeout_seconds=15.0)))
        assert closed.value.code == 4401 and closed.value.reason == "session_revoked"
        assert not any(frame["type"] in {"final", "error"} for frame in frames), frames
        assert len(recorded_generate) == 1 and len(provider.bodies) == 1
        # The explicit revoked outcome, not a success or the generic error path.
        assert outcomes == ["revoked"], outcomes
    finally:
        _close_sync_client_with_db(patches, stack)
