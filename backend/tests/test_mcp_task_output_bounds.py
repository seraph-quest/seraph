"""Local HTTP byte streams exercise the installed SDK before its JSON parser."""
import asyncio
from contextlib import nullcontext
from datetime import timedelta
import json

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import JSONRPCMessage
import pytest

from src.tools.mcp_manager import (MCPTaskOutputLimit, TASK_OUTPUT_BYTES,
    _TaskOutputGuard, _check_closed_task_schema, check_task_output, check_task_raw_output)
from src.work_board.general_task import digest


class Chunks(httpx.AsyncByteStream):
    def __init__(self, content):
        self.content = content
        self.reads = 0
        self.closed = False
    async def __aiter__(self):
        for start in range(0, len(self.content), 1024):
            self.reads += 1
            yield self.content[start:start + 1024]
    async def aclose(self):
        self.closed = True


def setup_sdk(monkeypatch, *, mode="json", oversized=False, sessionful=False, escape_requests=False):
    guard = _TaskOutputGuard()
    calls = []
    guard.fixture_get_contacts = []
    guard.fixture_request_failures = []
    owned_request_hook = guard._request_hook
    async def observe_request_guard(request):
        try:
            await owned_request_hook(request)
        except MCPTaskOutputLimit as exc:
            guard.fixture_request_failures.append(str(exc))
            raise
    guard._request_hook = observe_request_guard
    streams = []
    parsed_fixture = []
    original_parse = JSONRPCMessage.model_validate_json
    def parse(cls, value, *args, **kwargs):
        sentinel = "PRIVATE_FIXTURE_SENTINEL" if isinstance(value, str) else b"PRIVATE_FIXTURE_SENTINEL"
        if sentinel in value:
            parsed_fixture.append(True)
        return original_parse(value, *args, **kwargs)
    monkeypatch.setattr(JSONRPCMessage, "model_validate_json", classmethod(parse))

    def local_http(request):
        if request.method == "GET":
            guard.fixture_get_contacts.append(True)
            return httpx.Response(405, stream=Chunks(b""))
        if request.method == "DELETE":
            return httpx.Response(200, stream=Chunks(b""))
        message = json.loads(request.content)
        method = message.get("method")
        request_id = message.get("id")
        status = 200
        headers = {"content-type": "application/json"}
        if method == "initialize":
            if sessionful:
                headers["Mcp-Session-Id"] = "local-session"
            result = {"protocolVersion": "2025-06-18", "capabilities": {},
                      "serverInfo": {"name": "local", "version": "1"}}
        elif method == "notifications/initialized":
            return httpx.Response(202, stream=Chunks(b""))
        elif method == "tools/list":
            result = {"tools": [{"name": "same_tool", "description": "Local fixture",
                "inputSchema": {"type": "object", "properties": {
                    "query": {"type": "string", "maxLength": 30000 if escape_requests else 100}},
                    "required": ["query"], "additionalProperties": False}}]}
        elif method == "tools/call":
            with guard.lock:
                binding = guard.request_bindings.get(request_id)
            calls.append({"id": request_id, "bounded": request.extensions.get("seraph_task_output_bounded", False),
                          "binding": dict(binding) if binding else None,
                          "encoding": request.headers.get("accept-encoding")})
            text = "PRIVATE_FIXTURE_SENTINEL" + "x" * TASK_OUTPUT_BYTES if oversized else "local result"
            result = {"content": [{"type": "text", "text": text}], "isError": False}
            if mode == "deferred":
                status = 202
            elif mode == "compressed":
                headers["content-encoding"] = "gzip"
        else:
            raise AssertionError("Unexpected local SDK request")
        content = json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}).encode()
        if method == "tools/call" and mode in {"sse", "resumption"}:
            headers["content-type"] = "text/event-stream"
            content = b"event: message\r\ndata: " + content + b"\r\n\r\n"
            if mode == "resumption":
                notification = json.dumps({"jsonrpc": "2.0", "method": "notifications/progress",
                    "params": {"progressToken": request_id, "progress": 0}}).encode()
                content = b"id: local-event\nretry: 0\nevent: message\ndata: " + notification + b"\n\n"
        stream = Chunks(content)
        if method == "tools/call":
            streams.append(stream)
        return httpx.Response(status, headers=headers, stream=stream)

    def client_factory(headers=None, timeout=None, auth=None):
        client = httpx.AsyncClient(transport=httpx.MockTransport(local_http), headers=headers,
            timeout=timeout, auth=auth)
        if escape_requests:
            async def legal_ascii_json(request):
                if request.method == "POST":
                    # Legal equivalent wire representation at the owned
                    # serializer boundary, before the production guard hook.
                    message = json.loads(request.content)
                    content = json.dumps(message, ensure_ascii=True).encode()
                    request.stream = httpx.ByteStream(content)
                    request._content = content
                    request.headers["Content-Length"] = str(len(content))
                    if message.get("method") == "tools/call":
                        guard.fixture_wire_size = len(content)
            client.event_hooks["request"].append(legal_ascii_json)
        return client
    monkeypatch.setattr("mcp.shared._httpx_utils.create_mcp_http_client", client_factory)
    return guard, calls, streams, parsed_fixture


async def invoke_sdk(guard, *, concurrent=False, query="literal", bound=True):
    requests = []
    try:
        async with streamablehttp_client("https://fixture.invalid/mcp",
            httpx_client_factory=guard.http_client_factory) as (read, write, _):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=0.25)) as session:
                await session.initialize()
                assert guard.bind_session(session)
                loop = asyncio.get_running_loop()
                async def call(bound):
                    scope = guard.scope({"tool_name": "same_tool", "input_digest": digest({"query": query}),
                        "job_id": "native-job", "fencing_token": 7}) if bound else nullcontext()
                    with scope:
                        # The current MCPAdapt bridge uses this exact API;
                        # exercise ContextVar transfer over that real boundary.
                        async def sdk_call():
                            requests.append(asyncio.current_task())
                            return await session.call_tool("same_tool", {"query": query})
                        def sync_call():
                            return asyncio.run_coroutine_threadsafe(sdk_call(), loop).result(timeout=2)
                        return await asyncio.to_thread(sync_call)
                if concurrent:
                    return await asyncio.gather(call(True), call(False))
                return await call(bound)
    finally:
        # A rejected HTTP header can close the SDK task group before the sync
        # bridge's future observes it. Explicitly drain our two fixture calls.
        for request in requests:
            if not request.done():
                request.cancel()
        await asyncio.gather(*requests, return_exceptions=True)


@pytest.mark.parametrize("mode", ["json", "sse"])
async def test_sdk_exact_request_ids_separate_identical_interactive_call(monkeypatch, mode):
    guard, calls, _, parsed = setup_sdk(monkeypatch, mode=mode)
    results = await invoke_sdk(guard, concurrent=True)
    assert [result.content[0].text for result in results] == ["local result", "local result"]
    assert len(calls) == 2 and len({call["id"] for call in calls}) == 2
    bound = next(call for call in calls if call["bounded"])
    interactive = next(call for call in calls if not call["bounded"])
    assert bound["binding"]["job_id"] == "native-job" and bound["binding"]["fencing_token"] == 7
    assert bound["encoding"] == "identity"
    assert interactive["binding"] is None
    assert guard.request_bindings == {} and parsed == []


@pytest.mark.parametrize("mode", ["json", "sse"])
async def test_sdk_stops_oversized_inline_stream_before_message_parse(monkeypatch, mode):
    guard, calls, streams, parsed = setup_sdk(monkeypatch, mode=mode, oversized=True)
    with pytest.raises(Exception) as failure:
        await invoke_sdk(guard)
    assert len(calls) == 1  # no replay after contact
    assert parsed == []
    assert streams[0].closed and streams[0].reads <= 65
    assert "PRIVATE_FIXTURE_SENTINEL" not in str(failure.value)
    assert guard.request_bindings == {}


@pytest.mark.parametrize("query", ["é" * 30000, "漢" * 20000, "😀" * 15000],
    ids=["latin", "cjk", "astral"])
async def test_sdk_rejects_escaped_oversized_outgoing_task_before_transport(monkeypatch, query):
    from src.work_board.general_task import canonical, validate_schema
    assert len(canonical({"query": query})) < TASK_OUTPUT_BYTES
    validate_schema({"type": "object", "properties": {"query": {"type": "string", "maxLength": 30000}},
        "required": ["query"], "additionalProperties": False}, {"query": query})
    guard, calls, streams, parsed = setup_sdk(monkeypatch, escape_requests=True, oversized=True)
    with pytest.raises(Exception):
        await invoke_sdk(guard, query=query)
    assert guard.fixture_wire_size > TASK_OUTPUT_BYTES + 4096
    assert calls == [] and streams == [] and parsed == []
    assert guard.fixture_request_failures == ["mcp_task_output_request_byte_limit"]
    assert guard.request_bindings == {}


async def test_large_escaped_interactive_request_has_no_global_task_cap(monkeypatch):
    guard, calls, streams, _ = setup_sdk(monkeypatch, escape_requests=True)
    result = await invoke_sdk(guard, query="é" * 30000, bound=False)
    assert result.content[0].text == "local result"
    assert guard.fixture_wire_size > TASK_OUTPUT_BYTES + 4096
    assert len(calls) == 1 and calls[0]["bounded"] is False
    assert streams and guard.request_bindings == {}


async def test_active_task_on_another_client_does_not_cap_interactive_request(monkeypatch):
    guarded_connection, calls, streams, _ = setup_sdk(monkeypatch, escape_requests=True)
    guarded_connection.request_bindings[999] = {"job_id": "another-task"}
    independent_connection = _TaskOutputGuard()
    result = await invoke_sdk(independent_connection, query="é" * 30000, bound=False)
    assert result.content[0].text == "local result"
    assert guarded_connection.fixture_wire_size > TASK_OUTPUT_BYTES + 4096
    assert len(calls) == 1 and calls[0]["bounded"] is False
    assert streams and independent_connection.request_bindings == {}
    assert 999 in guarded_connection.request_bindings


@pytest.mark.parametrize("mode", ["compressed", "deferred"])
async def test_sdk_rejects_compression_and_deferred_reply_before_body_read(monkeypatch, mode):
    guard, calls, streams, parsed = setup_sdk(monkeypatch, mode=mode, oversized=True)
    with pytest.raises(Exception):
        await invoke_sdk(guard)
    assert len(calls) == 1 and streams[0].reads == 0 and streams[0].closed
    assert parsed == [] and guard.request_bindings == {}


async def test_sessionful_sdk_cannot_bind_task_even_with_inline_post_200(monkeypatch):
    guard, calls, _, _ = setup_sdk(monkeypatch, sessionful=True)
    async with streamablehttp_client("https://fixture.invalid/mcp",
        httpx_client_factory=guard.http_client_factory) as (read, write, _):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=1)) as session:
            await session.initialize()
            assert guard.inline_supported is False
            assert guard.bind_session(session) is False
            assert calls == []  # no typed task contact can be admitted
            # Existing interactive inline calls remain usable and unmodified.
            result = await session.call_tool("same_tool", {"query": "literal"})
            assert result.content[0].text == "local result"
    assert len(calls) == 1 and calls[0]["bounded"] is False and calls[0]["binding"] is None


async def test_sdk_sse_resumption_cannot_escape_guard_via_shared_get(monkeypatch):
    guard, calls, _, _ = setup_sdk(monkeypatch, mode="resumption")
    with pytest.raises(Exception):
        await invoke_sdk(guard)
    assert guard.inline_supported is False
    assert len(calls) == 1 and guard.fixture_get_contacts == []
    assert guard.request_bindings == {}


@pytest.mark.parametrize("value", ["x" * (TASK_OUTPUT_BYTES + 1), "😀" * 20000,
    '[' * 33 + '0' + ']' * 33])
def test_raw_output_is_bounded_before_json_parse(value):
    with pytest.raises(MCPTaskOutputLimit):
        check_task_raw_output(value)


def test_structured_result_is_bounded_before_serialization():
    class CannotSerialize:
        def __str__(self):
            raise AssertionError("custom object serialization must not run")
    cyclic = []
    cyclic.append(cyclic)
    for value in (CannotSerialize(), list(range(257)), cyclic,
                  [[0] * 256 for _ in range(20)], {"value": float("inf")}, {"value": 1 << 1000}):
        with pytest.raises(MCPTaskOutputLimit):
            check_task_output(value)
    check_task_output({"value": "bounded literal", "array": [True, None, 1.25]})


@pytest.mark.parametrize("schema", [
    {"type": "string"}, {"type": "string", "maxLength": TASK_OUTPUT_BYTES + 1},
    {"type": "array", "maxItems": 256, "items": {"type": "string"}},
    {"type": "object", "additionalProperties": False, "properties": {"nested": {
        "type": "array", "maxItems": 256, "items": {"type": "string"}}}},
    {"type": "string", "maxLength": 100, "anyOf": [{"type": "string"}]},
])
def test_incomplete_or_unbounded_output_schema_is_excluded(schema):
    with pytest.raises(ValueError):
        _check_closed_task_schema(schema)
