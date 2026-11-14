"""Shared fixtures for the #1019 Cordis agent-loop suites.

Scripted inference only: either a fixed in-memory smolagents ``Model`` returns
the next scripted message, or (``near_scripted_gateway``) the production model
runs its canonical preflight and ADR-035 NEAR TEE route selection and only the
NEAR gateway is answered by an ``httpx.MockTransport`` (#1107's pattern). Sockets and DNS are denied, so no
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


def near_scripted_gateway(script):
    """ADR-035: the scripted conversation answered by the intercepted NEAR TEE gateway.

    The chat and onboarding agents keep their production model; their route is
    the NEAR TEE text slot (prepared by ``tests.near_tee_support``). Each NEAR
    completion pops the next scripted message (content and/or tool calls);
    every NEAR billing readback settles the request. Any other host, the real
    transports and litellm fail the test. ``calls`` records, per completion,
    the offered tool names and the message count, as ``GovernedProvider`` did.
    """
    import httpx
    from tests.test_near_tee_text_slot import NearFixture, _json

    class ScriptedNear(NearFixture):
        def __init__(self):
            super().__init__()
            self.script, self.calls, self.bodies = list(script), [], []

        def handler(self, request):
            if request.method == "POST" and request.url.raw_path.decode() == "/v1/chat/completions":
                self.requests.append(request)
                assert request.url.host == "cloud-api.near.ai", request.url
                body = json.loads(request.content)
                self.bodies.append(body)
                if body.get("stream"):
                    return self._stream(body)
                self.calls.append({"tools": [tool["function"]["name"] for tool in body.get("tools") or []],
                                   "messages": len(body["messages"])})
                if not self.script:
                    raise AssertionError("scripted provider exhausted")
                message = self.script.pop(0)
                calls = [{"id": item.id, "type": "function",
                          "function": {"name": item.function.name, "arguments": json.dumps(item.function.arguments)}}
                         for item in message.tool_calls or []]
                self.body_id = f"chatcmpl-scripted-{len(self.bodies)}"
                answer = {"role": "assistant", "content": message.content or None}
                if calls:
                    answer["tool_calls"] = calls
                return _json({"id": self.body_id, "model": body["model"], "choices": [{"index": 0,
                    "finish_reason": "tool_calls" if calls else "stop", "message": answer}]})
            return super().handler(request)

        def _stream(self, body):
            # Direct chat streams one scripted reply: its content, or the
            # answer of a scripted final_answer call, as SSE deltas.
            self.calls.append({"tools": [], "messages": len(body["messages"]), "stream": True})
            if not self.script:
                raise AssertionError("scripted provider exhausted")
            message = self.script.pop(0)
            content = message.content or next((item.function.arguments.get("answer", "")
                                               for item in message.tool_calls or []), "")
            self.body_id = f"chatcmpl-scripted-{len(self.bodies)}"
            events = [{"id": self.body_id, "model": body["model"], "choices": [{"index": 0,
                "delta": {"role": "assistant", "content": content}}]},
                {"id": self.body_id, "model": body["model"], "choices": [{"index": 0, "delta": {},
                    "finish_reason": "stop"}]}]
            payload = b"".join(("data: " + json.dumps(event, separators=(",", ":")) + "\n\n").encode()
                               for event in events) + b"data: [DONE]\n\n"
            class _SSE(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield payload
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_SSE())

        def install(self, monkeypatch):
            # The starlette TestClient drives the app through httpx.Client.post
            # on its own in-process transport; keep that live. The real sync and
            # async network transports and litellm stay refused.
            real_post, real_stream = httpx.Client.post, httpx.Client.stream
            super().install(monkeypatch)
            monkeypatch.setattr(httpx.Client, "post", real_post)
            monkeypatch.setattr(httpx.Client, "stream", real_stream)
            return self
    return ScriptedNear()
