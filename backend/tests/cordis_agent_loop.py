"""Shared fixtures for the #1019 Cordis agent-loop suites.

Scripted inference only: either a fixed in-memory smolagents ``Model`` returns
the next scripted message, or (``governed_inference``) the real
``FallbackLiteLLMModel`` runs its canonical preflight and route selection and
only the governed OpenRouter transport's HTTP client is answered by an
``httpx.MockTransport`` (#1107's pattern). Sockets and DNS are denied, so no
provider is contacted and nothing is spent. The reviewed Cordis child is real,
and the agent-loop owner is cut over with the production fenced owner
transition and a boot proof from that child.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import json
import socket

from smolagents.models import ChatMessage, ChatMessageToolCall, ChatMessageToolCallFunction, Model

from tests.cordis_research import research_host

AGENT_LOOP = "seraph.agent-loop.v1"


def deny_sockets(monkeypatch):
    def denied(*_args, **_kwargs):
        raise RuntimeError("external transport denied in #1019 tests")
    for target, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"), (socket, "create_connection"),
                         (socket, "getaddrinfo")):
        monkeypatch.setattr(target, name, denied)


def call(name: str, arguments: dict, call_id: str = "call-1") -> ChatMessageToolCall:
    return ChatMessageToolCall(id=call_id, type="function",
                               function=ChatMessageToolCallFunction(name=name, arguments=arguments))


def tools(*calls: ChatMessageToolCall, content: str = "") -> ChatMessage:
    return ChatMessage(role="assistant", content=content, tool_calls=list(calls))


def final(answer: str) -> ChatMessage:
    return tools(call("final_answer", {"answer": answer}, "call-final"))


def text(content: str) -> ChatMessage:
    return ChatMessage(role="assistant", content=content, tool_calls=None)


class ScriptedModel(Model):
    """Returns the scripted messages in order; records whether each call offered tools."""

    def __init__(self, script):
        super().__init__(model_id="scripted-local")
        self.script, self.calls = list(script), []

    def generate(self, messages, stop_sequences=None, tools_to_call_from=None, **kwargs):
        self.calls.append({"tools": [tool.name for tool in tools_to_call_from or []], "messages": len(messages)})
        if not self.script:
            raise AssertionError("scripted model exhausted")
        return self.script.pop(0)


class GovernedProvider:
    """The provider behind the governed transport: replays the script as OpenRouter chat completions.

    Requests leave ``_governed_openai_chat_completion`` exactly as they would for
    OpenRouter; the real request serialization and response parsing both run.
    ``calls`` records, per request, the offered tool names and the message count.
    """

    def __init__(self, script):
        self.script, self.calls, self.bodies = list(script), [], []

    def handle(self, request):
        import httpx
        assert request.url.host == "openrouter.ai", request.url
        body = json.loads(request.content)
        self.bodies.append(body)
        self.calls.append({"tools": [tool["function"]["name"] for tool in body.get("tools") or []],
                           "messages": len(body["messages"])})
        if not self.script:
            raise AssertionError("scripted provider exhausted")
        message = self.script.pop(0)
        calls = [{"id": item.id, "type": "function",
                  "function": {"name": item.function.name, "arguments": json.dumps(item.function.arguments)}}
                 for item in message.tool_calls or []]
        return httpx.Response(200, json={
            "id": f"gen-{len(self.bodies)}", "object": "chat.completion", "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop",
                         "message": {"role": "assistant", "content": message.content or None,
                                     "tool_calls": calls or None}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.000001}})


def governed_inference(monkeypatch, script) -> GovernedProvider:
    """Chat and onboarding agents get the real governed model; only its provider HTTP is scripted."""
    import httpx
    from config.settings import settings
    from src import llm_runtime
    from src.model_fabric.configuration import openrouter_policy_for_setup
    from src.model_fabric.repository import model_fabric_repository
    from tests.test_agent_control_flow_propagation import _openrouter_route
    setup, profile, proofs = _openrouter_route()
    provider = GovernedProvider(script)
    monkeypatch.setattr(settings, "openrouter_api_key", "fixture-openrouter-key")

    def model(*, runtime_path: str = "chat_agent", temperature=None, **_kwargs):
        return llm_runtime.FallbackLiteLLMModel(model_id=profile.routing_model, api_base=profile.api_base,
                                                api_key="fixture-openrouter-key", runtime_profile=profile.id,
                                                runtime_path=runtime_path, max_tokens=512,
                                                **({} if temperature is None else {"temperature": temperature}))

    async def latest_capability_proof(*, capability, **_kwargs):
        return proofs.get(capability)
    real_client = httpx.Client

    def client(*args, **kwargs):
        return real_client(*args, **{**kwargs, "transport": httpx.MockTransport(provider.handle)})
    monkeypatch.setattr("src.agent.factory.get_model", model)
    monkeypatch.setattr("src.agent.onboarding.LiteLLMModel", model)
    monkeypatch.setattr("src.agent.onboarding.build_model_kwargs", lambda **kwargs: kwargs)
    monkeypatch.setattr(llm_runtime, "provider_profiles", lambda *_a, **_k: {profile.id: profile})
    monkeypatch.setattr(llm_runtime, "_fallback_targets", lambda *_a, **_k: [])
    monkeypatch.setattr("src.model_fabric.caller_context.effective_workload_policy",
                        lambda *_a, **_k: openrouter_policy_for_setup(setup, "chat_agent"))
    monkeypatch.setattr(model_fabric_repository, "latest_capability_proof", latest_capability_proof)
    monkeypatch.setattr(llm_runtime, "_execute_sync_with_gpu_admission", lambda **kwargs: kwargs["operation"]())
    monkeypatch.setattr(llm_runtime, "_new_route_receipt_session", lambda *_a, **_k: None)
    monkeypatch.setattr(llm_runtime, "_can_log_request", lambda *_a, **_k: False)
    # Only the governed transport builds a synchronous httpx.Client on these paths.
    monkeypatch.setattr(httpx, "Client", client)
    return provider


async def cutover_agent_loop(host, *, to: str = "cordis"):
    """Production fenced owner transition: ready -> draining -> blocked(epoch+1) -> ready(boot proof)."""
    from src.db.engine import get_session
    from src.runtime_plugins.ownership import (CompositionDependency, composition_writer, inventory,
                                               legacy_composition_digest, transition_owner)
    from src.workspace.lifecycle import lifecycle_fence_marker
    async with get_session() as db:
        row = (await inventory(db))[AGENT_LOOP]
        current = CompositionDependency(AGENT_LOOP, row.owner_kind, row.epoch, row.composition_digest)
        state = row.state
    digest = host.reviewed.plugin_digests[AGENT_LOOP] if to == "cordis" else legacy_composition_digest(AGENT_LOOP)
    epoch = current.epoch + 1
    target = CompositionDependency(AGENT_LOOP, to, epoch, digest)
    with lifecycle_fence_marker():
        for expected, expected_state, owner, at, next_state, proof in (
                (current, state, current.owner_kind, current.epoch, "draining", None),
                (current, "draining", to, epoch, "blocked", None),
                (target, "blocked", to, epoch, "ready", host)):
            if next_state == "draining" and expected_state != "ready":
                continue
            async with get_session() as db:
                async with composition_writer(db):
                    await transition_owner(db, runtime_domain=AGENT_LOOP, expected=expected,
                                           expected_state=expected_state, owner_kind=owner, epoch=at,
                                           composition_digest=expected.composition_digest if next_state == "draining"
                                           else digest, state=next_state, boot_host=proof)
    return epoch


@asynccontextmanager
async def agent_loop_host(jobs, *, package_root=None, cordis: bool = True):
    async with research_host(jobs, package_root=package_root) as host:
        if cordis:
            await cutover_agent_loop(host)
        yield host


def patched_package(directory, old: str, new: str):
    """A separately reviewed package copy whose built agent-loop sequencer differs by one edit.

    The build-manifest receipt is recomputed exactly as scripts/build-manifest.mjs
    does, so the copy passes the host's package/profile/plugin-digest checks.
    """
    import hashlib
    import json
    import shutil
    from src.runtime_plugins.composition import BUILD_FILES, NPM_VERSION
    from tests.cordis_research import PACKAGE
    root = directory.resolve() / "cordis"
    shutil.copytree(PACKAGE, root, ignore=shutil.ignore_patterns("typescript", "@types", "undici-types", ".build-home"))
    target = root / "dist/src/plugins/agent_loop/index.js"
    source = target.read_text()
    assert source.count(old) == 1, old
    target.write_text(source.replace(old, new))
    files = {path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in BUILD_FILES}
    (root / "dist/build-manifest.json").write_text(json.dumps({"format": 1, "npm_version": NPM_VERSION, "files": files}) + "\n")
    return root
