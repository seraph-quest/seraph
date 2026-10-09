"""MCP (Model Context Protocol) server integration.

Manages connections to external MCP servers (e.g. Things3, GitHub) and exposes
their tools for use by the smolagents ToolCallingAgent.

Server configuration loaded from mcp-servers.json at startup. Servers can be
added/removed/toggled at runtime via the MCP API endpoints.
"""

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import math
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from types import MethodType
from pathlib import Path
from urllib.parse import urlparse

from smolagents import MCPClient
import httpx

from src.audit.formatting import redact_for_audit
from src.audit.runtime import log_integration_event_sync
from src.security.site_policy import evaluate_site_access
from src.vault.repository import vault_repository

logger = logging.getLogger(__name__)


def _mcp_procedure_contract(declaration, input_schema, *, extension_id, reference, server_id, tool_name):
    """Only the current trusted local producer declares reusable ordinary leaves."""
    from src.workflows.procedure_contracts import ProcedureInputContractV1, ProcedureInputLeafV1, procedure_v3_digest, _pointer_segments
    declared = declaration.get("procedure_inputs")
    if declared is None:
        return None
    if not isinstance(declared, list) or not 1 <= len(declared) <= 64:
        raise ValueError("bounded local producer classifications required")
    from src.work_board.dispatcher import _AUTHORITY_INPUT_KEYS
    denied = set(_AUTHORITY_INPUT_KEYS) | {"body", "content", "headers", "code", "script",
        "snippet", "command", "expression", "credentials", "credential", "credential_ref",
        "password", "secret", "secret_ref", "api_key", "token", "authorization", "cookie",
        "provider", "model", "verifier", "permissions", "effects", "policy", "install",
        "deadline", "budget", "limits", "allowed_tool_ids"}
    leaves = []
    for raw in declared:
        leaf = ProcedureInputLeafV1.model_validate(raw)
        schema = input_schema
        segments = _pointer_segments(leaf.input_pointer)
        for segment in segments:
            if segment.casefold().replace("-", "_") in denied and leaf.kind != "forbidden":
                raise ValueError("authority, credentials and free body cannot be reusable")
            if schema.get("type") != "object" or segment not in schema.get("properties", {}):
                raise ValueError("producer classification must name an exact advertised leaf")
            schema = schema["properties"][segment]
        if schema != leaf.schema:
            raise ValueError("producer classification schema must match exact advertised leaf")
        leaves.append(leaf)
    identity = procedure_v3_digest([extension_id, reference, server_id, tool_name])
    return ProcedureInputContractV1(producer_id="seraph.mcp:" + identity,
        producer_version=declaration["version"], input_schema_digest=procedure_v3_digest(input_schema),
        classifications=leaves)

_ENV_VAR_RE = re.compile(r"\$\{(\w+)\}")
_VAULT_SECRET_RE = re.compile(r"\$\{vault:([A-Za-z0-9_.:-]+)\}")
TASK_OUTPUT_BYTES = 64 * 1024
TASK_OUTPUT_POLICY = {"max_bytes": TASK_OUTPUT_BYTES, "max_depth": 32,
                      "max_nodes": 4096, "max_container_items": 256,
                      "transport": "stateless_inline_post_only", "content_encoding": "identity"}


class MCPTaskOutputLimit(ValueError):
    """Content-free post-contact failure, never a retry or verification receipt."""


def _bounded_utf8_size(value):
    if len(value) > TASK_OUTPUT_BYTES:
        raise MCPTaskOutputLimit("mcp_task_output_byte_limit")
    size = 0
    for offset in range(0, len(value), 1024):
        size += len(value[offset:offset + 1024].encode("utf-8"))
        if size > TASK_OUTPUT_BYTES:
            raise MCPTaskOutputLimit("mcp_task_output_byte_limit")
    return size


def _check_json_frame(raw):
    """Lexical allocation limits before either SDK or task JSON parsing."""
    if len(raw) > TASK_OUTPUT_BYTES:
        raise MCPTaskOutputLimit("mcp_task_output_byte_limit")
    depth = nodes = 0
    quoted = escaped = token = False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
            token = False
            nodes += 1
        elif byte in (123, 91):
            depth += 1
            nodes += 1
            token = False
        elif byte in (125, 93):
            depth -= 1
            token = False
        elif byte in (32, 9, 10, 13, 44, 58):
            token = False
        elif not token:
            nodes += 1
            token = True
        if depth > 32 or depth < 0 or nodes > 4096:
            raise MCPTaskOutputLimit("mcp_task_output_shape_limit")


def check_task_output(value):
    """Strict finite JSON tree and serialized-byte budget before serialization."""
    nodes = 0
    size = 0
    ancestors = set()
    def string_size(text):
        _bounded_utf8_size(text)
        total = 2
        for char in text:
            code = ord(char)
            total += (2 if char in '\\"\b\f\n\r\t' else 6 if code < 32
                      else 1 if code < 128 else 2 if code < 2048 else 3 if code < 65536 else 4)
            if total > TASK_OUTPUT_BYTES:
                raise MCPTaskOutputLimit("mcp_task_output_byte_limit")
        return total
    def visit(item, depth):
        nonlocal nodes, size
        nodes += 1
        if nodes > 4096 or depth > 32:
            raise MCPTaskOutputLimit("mcp_task_output_shape_limit")
        kind = type(item)
        if kind is str:
            size += string_size(item)
        elif item is None:
            size += 4
        elif kind is bool:
            size += 5
        elif kind is int:
            if item.bit_length() > 256:
                raise MCPTaskOutputLimit("mcp_task_output_number_limit")
            size += len(str(item))
        elif kind is float:
            if not math.isfinite(item):
                raise MCPTaskOutputLimit("mcp_task_output_number_limit")
            size += len(str(item))
        elif kind in (dict, list):
            if len(item) > 256 or id(item) in ancestors:
                raise MCPTaskOutputLimit("mcp_task_output_shape_limit")
            ancestors.add(id(item))
            size += 2 + max(0, len(item) - 1)
            if kind is dict:
                for key, child in item.items():
                    if type(key) is not str:
                        raise MCPTaskOutputLimit("mcp_task_output_non_json")
                    size += string_size(key) + 1
                    visit(child, depth + 1)
            else:
                for child in item:
                    visit(child, depth + 1)
            ancestors.remove(id(item))
        else:
            raise MCPTaskOutputLimit("mcp_task_output_non_json")
        if size > TASK_OUTPUT_BYTES:
            raise MCPTaskOutputLimit("mcp_task_output_byte_limit")
    visit(value, 0)


def check_task_raw_output(raw):
    if type(raw) is str:
        _bounded_utf8_size(raw)
        _check_json_frame(raw.encode("utf-8"))
    else:
        check_task_output(raw)


class _BoundedMCPStream(httpx.AsyncByteStream):
    def __init__(self, stream, *, sse):
        self.stream = stream
        self.sse = sse

    async def __aiter__(self):
        total = 0
        pending = bytearray()
        try:
            async for chunk in self.stream:
                total += len(chunk)
                if total > TASK_OUTPUT_BYTES:
                    raise MCPTaskOutputLimit("mcp_task_output_byte_limit")
                pending.extend(chunk)
                if self.sse:
                    while match := re.search(rb"\r\n\r\n|\n\n|\r\r", pending):
                        event = bytes(pending[:match.end()])
                        del pending[:match.end()]
                        self._check_event(event)
                        yield event
            if pending:
                if self.sse:
                    self._check_event(pending)
                else:
                    _check_json_frame(pending)
                yield bytes(pending)
        except BaseException:
            await self.stream.aclose()
            raise

    @staticmethod
    def _check_event(event):
        if event.count(b"\n") > 256:
            raise MCPTaskOutputLimit("mcp_task_output_shape_limit")
        data = b"\n".join(line[5:].lstrip(b" ") for line in event.splitlines() if line.startswith(b"data:"))
        if data:
            _check_json_frame(data)

    async def aclose(self):
        await self.stream.aclose()


class _TaskOutputGuard:
    """Per-connection collection ownership; SDK request IDs bind exact calls."""
    def __init__(self):
        self.scope_binding = ContextVar("mcp_task_output_binding", default=None)
        self.request_bindings = {}
        self.lock = threading.Lock()
        self.bound = False
        self.inline_supported = True

    @contextmanager
    def scope(self, binding):
        token = self.scope_binding.set(binding)
        try:
            yield
        finally:
            self.scope_binding.reset(token)

    def bind_session(self, session):
        from mcp import ClientSession
        if type(session) is not ClientSession:
            return False
        original = session.send_request
        if (not self.inline_supported
            or getattr(original, "__func__", None) is not ClientSession.send_request
            or type(session._request_id) is not int):
            return False
        async def send_request(owned_session, request, result_type, *args, **kwargs):
            binding = self.scope_binding.get()
            guarded = binding is not None and request.root.method == "tools/call"
            request_id = None
            if guarded:
                if not self.inline_supported:
                    raise MCPTaskOutputLimit("mcp_task_stateless_inline_transport_required")
                from mcpadapt.smolagents_adapter import _sanitize_function_name
                from src.work_board.general_task import canonical
                params = request.root.params
                if (_sanitize_function_name(params.name) != binding["tool_name"]
                    or hashlib.sha256(canonical(params.arguments)).hexdigest() != binding["input_digest"]):
                    raise MCPTaskOutputLimit("mcp_task_output_request_binding_changed")
                if type(owned_session._request_id) is not int:
                    raise MCPTaskOutputLimit("mcp_task_output_sdk_unavailable")
                # Original SDK increments this integer before its first await.
                # Registration and delegation therefore have no async gap.
                request_id = owned_session._request_id
                with self.lock:
                    if len(self.request_bindings) >= 16 or request_id in self.request_bindings:
                        raise MCPTaskOutputLimit("mcp_task_output_binding_limit")
                    self.request_bindings[request_id] = binding
            try:
                return await original(request, result_type, *args, **kwargs)
            finally:
                if request_id is not None:
                    with self.lock:
                        self.request_bindings.pop(request_id, None)
        session.send_request = MethodType(send_request, session)
        self.bound = True
        return True

    def http_client_factory(self, headers=None, timeout=None, auth=None):
        from mcp.shared._httpx_utils import create_mcp_http_client
        client = create_mcp_http_client(headers=headers, timeout=timeout, auth=auth)
        client.event_hooks["request"].append(self._request_hook)
        client.event_hooks["response"].append(self._response_hook)
        return client

    async def _request_hook(self, request):
        if request.method == "GET":
            self.inline_supported = False
            with self.lock:
                active = bool(self.request_bindings)
            if active:
                # A guarded inline SSE request must not silently move its
                # contacted result onto the unbounded shared GET transport.
                raise MCPTaskOutputLimit("mcp_task_stateless_inline_transport_required")
        if request.method != "POST":
            return
        with self.lock:
            active = bool(self.request_bindings)
        if not active:
            return
        if len(request.content) > TASK_OUTPUT_BYTES + 4096:
            # Never let an active typed invocation leave this guard merely
            # because its outgoing JSON representation expanded. Parsing or
            # sending it would lose exact response collection ownership.
            raise MCPTaskOutputLimit("mcp_task_output_request_byte_limit")
        message = json.loads(request.content)  # bounded, SDK-owned outgoing envelope
        with self.lock:
            bound = self.request_bindings.get(message.get("id"))
        if bound is not None and message.get("method") == "tools/call":
            request.extensions["seraph_task_output_bounded"] = True
            request.headers["Accept-Encoding"] = "identity"

    async def _response_hook(self, response):
        if "mcp-session-id" in response.headers:
            self.inline_supported = False
        if not response.request.extensions.get("seraph_task_output_bounded"):
            return
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if response.status_code == 202 or encoding != "identity":
            await response.aclose()
            raise MCPTaskOutputLimit("mcp_task_output_inline_identity_required")
        length = response.headers.get("content-length")
        if length is not None and (len(length) > 20 or not length.isdigit() or int(length) > TASK_OUTPUT_BYTES):
            await response.aclose()
            raise MCPTaskOutputLimit("mcp_task_output_byte_limit")
        if response.is_stream_consumed:
            # A custom prebuffering client is not the owned SDK transport.
            await response.aclose()
            raise MCPTaskOutputLimit("mcp_task_output_transport_unavailable")
        response.stream = _BoundedMCPStream(response.stream,
            sse="text/event-stream" in response.headers.get("content-type", "").lower())


def _check_closed_task_schema(schema, depth=0):
    """Require local, closed typed objects at every task schema boundary."""
    from src.work_board.general_task_schema import validate_safe_patterns
    validate_safe_patterns(schema)
    supported = {"type", "properties", "required", "additionalProperties", "items",
        "minItems", "maxItems", "uniqueItems", "minLength", "maxLength", "pattern",
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
        "minProperties", "maxProperties", "enum", "const", "title", "description",
        "default", "examples", "$comment"}
    if (depth > 32 or not isinstance(schema, dict) or set(schema) - supported
        or schema.get("type") not in {
        "object", "array", "string", "integer", "number", "boolean", "null"}):
        raise ValueError("task schema must declare a bounded local type")
    if schema["type"] == "object":
        if (schema.get("additionalProperties") is not False or not isinstance(schema.get("properties"), dict)
            or len(schema["properties"]) > 256):
            raise ValueError("task object schemas must be closed")
        for child in schema["properties"].values():
            _check_closed_task_schema(child, depth + 1)
    if schema["type"] == "array":
        if type(schema.get("maxItems")) is not int or not 0 <= schema["maxItems"] <= 256:
            raise ValueError("task arrays must be bounded")
        _check_closed_task_schema(schema.get("items"), depth + 1)
    if schema["type"] == "string":
        if type(schema.get("maxLength")) is not int or not 0 <= schema["maxLength"] <= TASK_OUTPUT_BYTES:
            raise ValueError("task strings must be bounded")
    if ("items" in schema and schema["type"] != "array") or (
        "properties" in schema and schema["type"] != "object"):
        raise ValueError("unsupported task schema branch")


class _InstrumentedMCPTool:
    """Delegate wrapper for MCP tools that cannot accept dynamic attributes."""

    def __init__(
        self,
        wrapped_tool: object,
        *,
        source_context: dict[str, object],
        approval_context_fn,
        audit_call_payload_fn,
        audit_result_payload_fn,
        audit_failure_payload_fn,
    ) -> None:
        self.wrapped_tool = wrapped_tool
        self.name = str(getattr(wrapped_tool, "name", "mcp_tool"))
        description = getattr(wrapped_tool, "description", "")
        self.description = description if isinstance(description, str) else ""
        inputs = getattr(wrapped_tool, "inputs", {})
        self.inputs = inputs if isinstance(inputs, dict) else {}
        output_type = getattr(wrapped_tool, "output_type", "string")
        self.output_type = output_type if isinstance(output_type, str) else "string"
        output_schema = getattr(wrapped_tool, "output_schema", None)
        self.output_schema = output_schema if isinstance(output_schema, dict) else None
        self.is_initialized = True
        self.seraph_source_context = dict(source_context)
        self.seraph_secret_ref_fields = MCPManager._secret_ref_fields_for_tool(wrapped_tool)
        self.get_approval_context = approval_context_fn
        self.get_audit_call_payload = audit_call_payload_fn
        self.get_audit_result_payload = audit_result_payload_fn
        self.get_audit_failure_payload = audit_failure_payload_fn

    def forward(self, *args, **kwargs):
        return self.wrapped_tool(*args, **kwargs)

    def __call__(self, *args, sanitize_inputs_outputs: bool = False, **kwargs):
        return self.wrapped_tool(*args, sanitize_inputs_outputs=sanitize_inputs_outputs, **kwargs)


class MCPManager:
    """Connects to multiple named MCP servers and provides their tools."""

    def __init__(self) -> None:
        self._clients: dict[str, MCPClient] = {}
        self._tools: dict[str, list] = {}
        self._config_path: str | None = None
        self._config: dict[str, dict] = {}
        self._status: dict[str, dict] = {}
        self._connection_revisions: dict[str, int] = {}
        self._task_output_guards: dict[str, _TaskOutputGuard] = {}
        self._task_output_block_reasons: dict[str, str] = {}
        # Each: {"status": "connected"|"disconnected"|"auth_required"|"error", "error": str|None}

    # --- Config loading ---

    def load_config(self, config_path: str) -> None:
        """Load MCP server config from JSON file and connect enabled servers."""
        path = Path(config_path)
        self._config_path = config_path
        if not path.exists():
            logger.info("No MCP config at %s — skipping", config_path)
            return
        with open(path) as f:
            data = json.load(f)
        for name, server in data.get("mcpServers", {}).items():
            self._config[name] = server
            if not server.get("enabled", True):
                logger.info("MCP server '%s' disabled — skipping", name)
                continue
            self.connect(name, server["url"], headers=server.get("headers"))

    def _save_config(self) -> None:
        """Write current config back to the JSON file."""
        if not self._config_path:
            return
        path = Path(self._config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"mcpServers": self._config}
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")

    # --- Connection management ---

    @staticmethod
    def endpoint_policy_issues(url: str) -> list[str]:
        """Validate MCP endpoints against the shared site/network guard."""
        normalized = (url or "").strip()
        if not normalized:
            return []
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return []
        decision = evaluate_site_access(normalized, resolve_dns=True)
        if decision.allowed:
            return []
        reason = decision.reason or "blocked"
        return [f"Server URL host '{decision.hostname}' is blocked by site policy: {reason}."]

    @staticmethod
    def _raise_for_endpoint_policy(url: str) -> None:
        issues = MCPManager.endpoint_policy_issues(url)
        if issues:
            raise ValueError(" ".join(issues))

    @staticmethod
    def _build_source_context(
        *,
        name: str,
        url: str,
        auth_hint: str = "",
        source: str = "manual",
        extension_id: str | None = None,
        extension_reference: str | None = None,
        extension_display_name: str | None = None,
        credential_sources: list[str] | None = None,
        used_headers: bool = False,
    ) -> dict[str, object]:
        parsed = urlparse(url)
        normalized_credential_sources = [
            item for item in (credential_sources or []) if isinstance(item, str) and item.strip()
        ]
        authenticated_source = bool(used_headers or auth_hint.strip() or normalized_credential_sources)
        credential_egress_policy = {
            "mode": (
                "explicit_host_allowlist"
                if authenticated_source and bool(parsed.hostname)
                else "no_credentials" if not authenticated_source else "blocked"
            ),
            "transport": parsed.scheme or "unknown",
            "allowed_hosts": [parsed.hostname] if parsed.hostname else [],
        }
        return {
            "server_name": name,
            "url": url,
            "hostname": parsed.hostname or "",
            "authenticated_source": authenticated_source,
            "auth_hint": auth_hint.strip(),
            "credential_sources": normalized_credential_sources,
            "source": source,
            "extension_id": extension_id,
            "extension_reference": extension_reference,
            "extension_display_name": extension_display_name,
            "credential_egress_policy": credential_egress_policy,
        }

    @staticmethod
    def _instrument_mcp_tool(tool: object, source_context: dict[str, object]) -> object:
        def _get_approval_context(_arguments: dict[str, object], *, _context: dict[str, object] = dict(source_context)) -> dict[str, object]:
            boundaries = ["external_mcp"]
            if bool(_context.get("authenticated_source")):
                boundaries.append("authenticated_external_source")
            return {
                "server_name": str(_context.get("server_name") or ""),
                "hostname": str(_context.get("hostname") or ""),
                "authenticated_source": bool(_context.get("authenticated_source")),
                "credential_sources": list(_context.get("credential_sources") or []),
                "credential_egress_policy": dict(_context.get("credential_egress_policy") or {}),
                "source": str(_context.get("source") or "manual"),
                "extension_id": _context.get("extension_id"),
                "extension_reference": _context.get("extension_reference"),
                "extension_display_name": _context.get("extension_display_name"),
                "execution_boundaries": boundaries,
            }

        def _get_audit_call_payload(
            arguments: dict[str, object],
            *,
            _context: dict[str, object] = dict(source_context),
            _tool_name: str = str(getattr(tool, "name", "mcp_tool")),
        ) -> tuple[str, dict[str, object]]:
            return (
                f"{_tool_name} called via {_context.get('server_name')}",
                {
                    "arguments": redact_for_audit(arguments),
                    "source_context": dict(_context),
                },
            )

        def _get_audit_result_payload(
            _arguments: dict[str, object],
            result: object,
            *,
            _context: dict[str, object] = dict(source_context),
            _tool_name: str = str(getattr(tool, "name", "mcp_tool")),
        ) -> tuple[str, dict[str, object]]:
            summary = f"{_tool_name} completed via {_context.get('server_name')}"
            return (
                summary,
                {
                    "result_preview": redact_for_audit(str(result))[:280],
                    "source_context": dict(_context),
                },
            )

        def _get_audit_failure_payload(
            arguments: dict[str, object],
            error: Exception,
            *,
            _context: dict[str, object] = dict(source_context),
            _tool_name: str = str(getattr(tool, "name", "mcp_tool")),
        ) -> tuple[str, dict[str, object]]:
            return (
                f"{_tool_name} failed via {_context.get('server_name')}",
                {
                    "arguments": redact_for_audit(arguments),
                    "error": redact_for_audit(str(error)),
                    "source_context": dict(_context),
                },
            )

        try:
            setattr(tool, "seraph_source_context", dict(source_context))
            setattr(tool, "seraph_secret_ref_fields", MCPManager._secret_ref_fields_for_tool(tool))
            setattr(tool, "get_approval_context", _get_approval_context)
            setattr(tool, "get_audit_call_payload", _get_audit_call_payload)
            setattr(tool, "get_audit_result_payload", _get_audit_result_payload)
            setattr(tool, "get_audit_failure_payload", _get_audit_failure_payload)
            return tool
        except Exception:
            return _InstrumentedMCPTool(
                tool,
                source_context=source_context,
                approval_context_fn=_get_approval_context,
                audit_call_payload_fn=_get_audit_call_payload,
                audit_result_payload_fn=_get_audit_result_payload,
                audit_failure_payload_fn=_get_audit_failure_payload,
            )

    @staticmethod
    def _retain_advertised_task_schemas(client, tools, *, guarded_session):
        """Retain this stock client's exact discovery metadata, without rediscovery."""
        from mcp.types import Tool as AdvertisedTool
        from src.work_board.general_task import validate_schema
        from jsonschema.exceptions import SchemaError
        try:
            adapter = vars(client).get("_adapter")
            sessions = vars(adapter).get("sessions")
            cached = vars(adapter).get("mcp_tools")
        except TypeError:
            sessions, cached = None, None
        valid_cache = (guarded_session and isinstance(sessions, list) and len(sessions) == 1
            and isinstance(cached, list) and len(cached) == 1 and isinstance(cached[0], list)
            and all(type(item) is AdvertisedTool for item in cached[0])
            and len(cached[0]) == len(tools))
        advertised = cached[0] if valid_cache else []
        names = [item.name for item in advertised]
        adapted_names = [getattr(tool, "name", None) for tool in tools]
        for tool in tools:
            reason, schema, input_schema = "mcp_task_advertised_schema_unavailable", None, None
            name = getattr(tool, "name", None)
            if valid_cache and isinstance(name, str) and names.count(name) == adapted_names.count(name) == 1:
                original = next(item for item in advertised if item.name == name)
                try:
                    if not isinstance(original.outputSchema, dict):
                        raise ValueError("advertised typed output required")
                    if not isinstance(original.inputSchema, dict) or original.inputSchema.get("type") != "object":
                        raise ValueError("advertised typed input required")
                    check_task_output(original.inputSchema)
                    _check_closed_task_schema(original.inputSchema)
                    validate_schema(original.inputSchema, check_value=False)
                    check_task_output(original.outputSchema)
                    _check_closed_task_schema(original.outputSchema)
                    validate_schema(original.outputSchema, check_value=False)
                    schema = json.loads(json.dumps(original.outputSchema, allow_nan=False))
                    input_schema = json.loads(json.dumps(original.inputSchema, allow_nan=False))
                    reason = None
                except (ValueError, TypeError, SchemaError):
                    reason = "mcp_task_advertised_schema_invalid"
            elif valid_cache:
                reason = "mcp_task_advertised_identity_ambiguous"
            # Keep structured_output=False and the original string-returning
            # callback. This metadata grants no effect or permission.
            tool.output_schema = schema
            tool.seraph_advertised_input_schema = input_schema
            tool.seraph_task_schema_block_reason = reason

    @staticmethod
    def _secret_ref_fields_for_tool(tool: object) -> list[str]:
        inputs = getattr(tool, "inputs", None)
        if not isinstance(inputs, dict):
            return []
        allowed: list[str] = []
        seen: set[str] = set()
        for field_name in ("headers", "authorization", "auth_header", "api_key", "token", "bearer_token", "password", "secret_ref"):
            if field_name in inputs and field_name not in seen:
                allowed.append(field_name)
                seen.add(field_name)
        return allowed

    @staticmethod
    def _resolve_env_vars(value: str) -> str:
        """Replace ${VAR} patterns with environment variable values."""
        return re.sub(
            _ENV_VAR_RE,
            lambda m: os.environ.get(m.group(1), m.group(0)),
            value,
        )

    @staticmethod
    def _run_async(coro):
        """Run an async coroutine from sync context, even under an active event loop."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()

    @staticmethod
    def _flatten_exception_text(exc: BaseException) -> str:
        """Collect str() of exc and all chained/grouped sub-exceptions into one lowercase string."""
        parts = [str(exc)]
        if isinstance(exc, BaseExceptionGroup):
            for sub in exc.exceptions:
                parts.append(MCPManager._flatten_exception_text(sub))
        if exc.__cause__:
            parts.append(MCPManager._flatten_exception_text(exc.__cause__))
        elif exc.__context__:
            parts.append(MCPManager._flatten_exception_text(exc.__context__))
        return " ".join(parts).lower()

    @staticmethod
    def _check_unresolved_vars(headers: dict[str, str] | None) -> list[str]:
        """Return list of env var names that are still unresolved after _resolve_env_vars."""
        if not headers:
            return []
        missing: list[str] = []
        for v in headers.values():
            resolved = MCPManager._resolve_env_vars(v)
            for m in re.finditer(_ENV_VAR_RE, resolved):
                missing.append(m.group(1))
        return missing

    @staticmethod
    def _check_missing_vault_secrets(headers: dict[str, str] | None) -> list[str]:
        """Return vault-backed secret keys referenced by headers that are missing."""
        if not headers:
            return []
        missing: list[str] = []
        checked: set[str] = set()
        for value in headers.values():
            if not isinstance(value, str):
                continue
            resolved = MCPManager._resolve_env_vars(value)
            for match in re.finditer(_VAULT_SECRET_RE, resolved):
                key = match.group(1)
                if key in checked:
                    continue
                checked.add(key)
                exists = MCPManager._run_async(vault_repository.exists(key))
                if not exists:
                    missing.append(key)
        return missing

    @staticmethod
    def inspect_headers(headers: dict[str, str] | None) -> tuple[list[str], list[str], list[str]]:
        """Inspect headers for missing env/vault credentials without resolving them."""
        if not headers:
            return [], [], []

        missing_env_vars = MCPManager._check_unresolved_vars(headers)
        missing_vault_keys = MCPManager._check_missing_vault_secrets(headers)
        credential_sources: set[str] = set()
        for key, value in headers.items():
            if not isinstance(value, str):
                continue
            resolved = MCPManager._resolve_env_vars(value)
            if re.search(_ENV_VAR_RE, value):
                credential_sources.add("env")
            if re.search(_VAULT_SECRET_RE, value) or re.search(_VAULT_SECRET_RE, resolved):
                credential_sources.add("vault")
            if (
                isinstance(key, str)
                and key.strip().lower() == "authorization"
                and not re.search(_ENV_VAR_RE, value)
                and not re.search(_VAULT_SECRET_RE, value)
                and not re.search(_VAULT_SECRET_RE, resolved)
            ):
                credential_sources.add("inline")

        return missing_env_vars, missing_vault_keys, sorted(credential_sources)

    @staticmethod
    def resolve_headers(
        headers: dict[str, str] | None,
    ) -> tuple[dict[str, str] | None, list[str], list[str], list[str]]:
        """Resolve env vars and vault placeholders inside headers.

        Returns: resolved headers, missing env vars, missing vault keys, credential sources.
        """
        if not headers:
            return None, [], [], []

        missing_env_vars, missing_vault_keys, credential_sources = MCPManager.inspect_headers(headers)
        if missing_env_vars or missing_vault_keys:
            return dict(headers), missing_env_vars, missing_vault_keys, credential_sources
        resolved_headers: dict[str, str] = {}

        for key, value in headers.items():
            if not isinstance(value, str):
                continue
            resolved = MCPManager._resolve_env_vars(value)

            def _replace_vault(match: re.Match[str]) -> str:
                secret_key = match.group(1)
                secret_value = MCPManager._run_async(vault_repository.get(secret_key))
                if secret_value is None:
                    return match.group(0)
                return secret_value

            resolved = re.sub(_VAULT_SECRET_RE, _replace_vault, resolved)
            resolved_headers[key] = resolved

        return resolved_headers, missing_env_vars, missing_vault_keys, credential_sources

    @staticmethod
    def _token_secret_key(name: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", name.strip()).strip("._-")
        digest = hashlib.sha1(name.strip().encode("utf-8")).hexdigest()[:10]
        return f"mcp.server.{normalized or 'default'}.{digest}.bearer_token"

    def connect(self, name: str, url: str, headers: dict[str, str] | None = None) -> None:
        """Connect to a named MCP server via HTTP/SSE. Fails gracefully."""
        # Invalidate every accepted snapshot even if reconnection fails.
        self._connection_revisions[name] = self._connection_revisions.get(name, 0) + 1
        try:
            endpoint_issues = self.endpoint_policy_issues(url)
            if endpoint_issues:
                msg = " ".join(endpoint_issues)
                self._status[name] = {"status": "blocked", "error": msg}
                logger.warning("MCP server '%s' blocked by endpoint policy: %s", name, msg)
                log_integration_event_sync(
                    integration_type="mcp_server",
                    name=name,
                    outcome="blocked",
                    details={
                        "url": url,
                        "status": "site_policy_blocked",
                        "issues": endpoint_issues,
                    },
                )
                return
            resolved_headers, missing_vars, missing_vault_keys, credential_sources = self.resolve_headers(headers)
            missing_details: list[str] = []
            if missing_vars:
                missing_details.append(f"Missing environment variables: {', '.join(missing_vars)}")
            if missing_vault_keys:
                missing_details.append(f"Missing vault secrets: {', '.join(missing_vault_keys)}")
            if missing_details:
                msg = "; ".join(missing_details)
                self._status[name] = {"status": "auth_required", "error": msg}
                logger.warning("MCP server '%s' requires auth: %s", name, msg)
                log_integration_event_sync(
                    integration_type="mcp_server",
                    name=name,
                    outcome="auth_required",
                    details={
                        "url": url,
                        "error": msg,
                        "missing_env_vars": missing_vars,
                        "missing_vault_keys": missing_vault_keys,
                        "credential_sources": credential_sources,
                    },
                )
                return

            params: dict = {"url": url, "transport": "streamable-http"}
            output_guard = _TaskOutputGuard()
            # MCPAdapt deep-copies params. A local function keeps the one
            # connection's guard identity without copying locks or state.
            def bounded_factory(headers=None, timeout=None, auth=None):
                return output_guard.http_client_factory(headers, timeout, auth)
            params["httpx_client_factory"] = bounded_factory
            if resolved_headers:
                params["headers"] = resolved_headers
            client = MCPClient(params, structured_output=False)
            try:
                adapter = vars(client).get("_adapter")
                sessions = vars(adapter).get("sessions") if adapter is not None else None
            except TypeError:
                sessions = None
            if isinstance(sessions, list) and len(sessions) == 1 and output_guard.bind_session(sessions[0]):
                self._task_output_guards[name] = output_guard
                self._task_output_block_reasons.pop(name, None)
            else:
                self._task_output_guards.pop(name, None)
                self._task_output_block_reasons[name] = (
                    "mcp_task_stateless_inline_transport_required" if not output_guard.inline_supported
                    else "mcp_task_transport_sdk_unavailable")
            source_context = self._build_source_context(
                name=name,
                url=url,
                auth_hint=str(self._config.get(name, {}).get("auth_hint") or ""),
                source=str(self._config.get(name, {}).get("source") or "manual"),
                extension_id=(
                    str(self._config.get(name, {}).get("extension_id"))
                    if self._config.get(name, {}).get("extension_id") is not None
                    else None
                ),
                extension_reference=(
                    str(self._config.get(name, {}).get("extension_reference"))
                    if self._config.get(name, {}).get("extension_reference") is not None
                    else None
                ),
                extension_display_name=(
                    str(self._config.get(name, {}).get("extension_display_name"))
                    if self._config.get(name, {}).get("extension_display_name") is not None
                    else None
                ),
                credential_sources=credential_sources,
                used_headers=bool(resolved_headers),
            )
            source_context["connection_revision"] = self._connection_revisions[name]
            tools = [
                self._instrument_mcp_tool(tool, source_context)
                for tool in client.get_tools()
            ]
            self._retain_advertised_task_schemas(client, tools,
                guarded_session=name in self._task_output_guards)
            self._clients[name] = client
            self._tools[name] = tools
            self._status[name] = {"status": "connected", "error": None}
            logger.info("Connected to MCP server '%s': %d tools loaded", name, len(tools))
            log_integration_event_sync(
                integration_type="mcp_server",
                name=name,
                outcome="connected",
                details={
                    "url": url,
                    "tool_count": len(tools),
                    "used_headers": bool(resolved_headers),
                    "credential_sources": credential_sources,
                },
            )
        except BaseException as exc:
            exc_str = self._flatten_exception_text(exc)
            if any(kw in exc_str for kw in ("401", "403", "unauthorized", "forbidden")):
                self._status[name] = {"status": "auth_required", "error": str(exc)}
                logger.warning("MCP server '%s' auth failed: %s", name, exc)
                log_integration_event_sync(
                    integration_type="mcp_server",
                    name=name,
                    outcome="auth_required",
                    details={
                        "url": url,
                        "error": str(exc),
                    },
                )
            else:
                self._status[name] = {"status": "error", "error": str(exc)}
                logger.warning("Failed to connect to MCP server '%s' at %s", name, url, exc_info=True)
                log_integration_event_sync(
                    integration_type="mcp_server",
                    name=name,
                    outcome="failed",
                    details={
                        "url": url,
                        "error": str(exc),
                    },
                )

    def disconnect(self, name: str) -> None:
        """Disconnect a specific named MCP server."""
        self._connection_revisions[name] = self._connection_revisions.get(name, 0) + 1
        self._task_output_guards.pop(name, None)
        self._task_output_block_reasons.pop(name, None)
        client = self._clients.pop(name, None)
        self._tools.pop(name, None)
        self._status[name] = {"status": "disconnected", "error": None}
        if client:
            try:
                client.disconnect()
            except Exception:
                logger.warning("Error disconnecting MCP client '%s'", name, exc_info=True)
        log_integration_event_sync(
            integration_type="mcp_server",
            name=name,
            outcome="disconnected",
            details={"had_client": client is not None},
        )

    def disconnect_all(self) -> None:
        """Disconnect all MCP servers."""
        for name in list(self._clients):
            self.disconnect(name)

    # --- Tool access ---

    def get_tools(self) -> list:
        """Return a flat list of tools from all connected servers."""
        tools: list = []
        for server_tools in self._tools.values():
            tools.extend(server_tools)
        return tools

    def task_tool_entries(self, extension_registry, mcp_mode: str) -> list:
        """Return typed snapshots declared by trusted existing MCP extensions.

        Server advertisements alone cannot grant effects or permissions. A
        local extension's existing MCP contribution may declare ``task_tools``
        alongside its server definition. Missing or incomplete declarations
        exclude a tool. This is metadata access only, never an MCP call API.
        """
        from src.extensions.connectors import load_connector_payload
        from src.work_board.contracts import ToolDescriptor
        from src.work_board.general_task import digest, validate_schema
        from src.tools.policy import get_tool_source_context
        from jsonschema.exceptions import SchemaError

        if mcp_mode not in {"approval", "full"}:
            return []
        entries = []
        for contribution in extension_registry.list_contributions("mcp_servers"):
            metadata = contribution.metadata
            if metadata.get("trust") != "local" or metadata.get("conflict"):
                continue
            server_id = metadata.get("name")
            config = self._config.get(server_id, {})
            if (not server_id or not self.is_connected(server_id)
                or server_id not in self._task_output_guards
                or not self._task_output_guards[server_id].inline_supported
                or self._status.get(server_id, {}).get("status") != "connected"
                or not config.get("enabled", True)
                or config.get("extension_id") != contribution.extension_id
                or config.get("extension_reference") != contribution.reference
                or config.get("url") != metadata.get("url")):
                continue
            path = Path(str(metadata.get("resolved_path") or ""))
            try:
                if not path.is_file() or path.is_symlink() or path.stat().st_size > TASK_OUTPUT_BYTES:
                    continue
                payload = load_connector_payload(path)
                declarations = payload.get("task_tools", {})
                if not isinstance(declarations, dict):
                    continue
                revision = self._connection_revisions.get(server_id, 0)
                if revision < 1:
                    continue
                for tool in self.get_server_tools(server_id):
                    declaration = declarations.get(tool.name)
                    if not isinstance(declaration, dict):
                        continue
                    try:
                        input_schema = declaration["input_schema"]
                        output_schema = declaration["output_schema"]
                        check_task_output(input_schema)
                        check_task_output(output_schema)
                        if input_schema.get("type") != "object":
                            continue
                        _check_closed_task_schema(input_schema)
                        _check_closed_task_schema(output_schema)
                        validate_schema(input_schema, check_value=False)
                        validate_schema(output_schema, check_value=False)
                        if declaration.get("verifier") != "json_schema.v1":
                            continue
                        # Exact advertised inputs/output contract is required;
                        # output_type="string" alone is never a typed result.
                        if getattr(tool, "output_schema", None) != output_schema:
                            continue
                        advertised = getattr(tool, "seraph_advertised_input_schema", None)
                        if advertised != input_schema:
                            continue
                        source = get_tool_source_context(tool)
                        if (not source or source.get("server_name") != server_id
                            or source.get("connection_revision") != revision):
                            continue
                        egress = source.get("credential_egress_policy")
                        if not isinstance(egress, dict) or egress.get("mode") not in {
                            "no_credentials", "explicit_host_allowlist"}:
                            continue
                        if egress.get("transport") not in {"http", "https"}:
                            continue
                        effects = declaration["effects"]
                        if not isinstance(effects, list) or any(effect not in {
                            "external_read", "connector_mutation"} for effect in effects):
                            continue
                        if declaration["permissions"] != ["capability_execute"]:
                            continue
                        # Connection-owned credentials stay in the current
                        # manager/vault boundary; planner-supplied refs denied.
                        credentials = source.get("credential_sources", [])
                        if not isinstance(credentials, list) or any(not isinstance(ref, str) for ref in credentials):
                            continue
                        policy = {"declaration": declaration, "source": source,
                            "mode": mcp_mode, "extension_id": contribution.extension_id,
                            "reference": contribution.reference, "advertised_inputs": advertised,
                            "advertised_output": getattr(tool, "output_schema", None),
                            "output_allowance": TASK_OUTPUT_POLICY,
                            "config_digest": digest(config)}
                        descriptor = ToolDescriptor(tool_id=f"mcp:{server_id}:{tool.name}",
                            version=declaration["version"], input_schema=input_schema,
                            output_schema=output_schema, effects=effects,
                            permissions=declaration["permissions"], credential_refs=credentials,
                            deadline=declaration["deadline"], verifier="json_schema.v1",
                            server_id=server_id, connection_revision=revision, policy_digest=digest(policy),
                            procedure_inputs=_mcp_procedure_contract(declaration, input_schema,
                                extension_id=contribution.extension_id, reference=contribution.reference,
                                server_id=server_id, tool_name=tool.name))
                        entries.append((descriptor, tool))
                    except (KeyError, TypeError, ValueError, SchemaError):
                        continue
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return entries

    def task_output_scope(self, descriptor, inputs, *, job_id, fencing_token, tool_name):
        guard = self._task_output_guards.get(descriptor.server_id)
        if (guard is None or not guard.inline_supported
            or self._connection_revisions.get(descriptor.server_id) != descriptor.connection_revision):
            raise MCPTaskOutputLimit("mcp_task_output_transport_unavailable")
        from src.work_board.general_task import canonical
        return guard.scope({"job_id": job_id, "fencing_token": fencing_token,
            "tool_name": tool_name, "input_digest": hashlib.sha256(canonical(inputs)).hexdigest()})

    def task_tool_block_reason(self, server_id):
        guard = self._task_output_guards.get(server_id)
        if guard is not None and not guard.inline_supported:
            return "mcp_task_stateless_inline_transport_required"
        return self._task_output_block_reasons.get(server_id, "trusted_typed_contract_or_policy_unavailable")

    def get_server_tools(self, name: str) -> list:
        """Return tools for a specific named server."""
        return self._tools.get(name, [])

    def get_server_names(self) -> list[str]:
        """Return names of all configured servers (connected or not)."""
        return list(self._config.keys())

    def is_connected(self, name: str) -> bool:
        """Check if a server is currently connected."""
        return name in self._clients

    def get_config(self) -> list[dict]:
        """Return current server states for the API."""
        result = []
        for name, server in self._config.items():
            connected = name in self._clients
            tool_count = len(self._tools.get(name, []))
            status_info = self._status.get(name, {"status": "disconnected", "error": None})
            entry: dict = {
                "name": name,
                "url": server.get("url", ""),
                "enabled": server.get("enabled", True),
                "connected": connected,
                "tool_count": tool_count,
                "description": server.get("description", ""),
                "status": status_info["status"],
                "status_message": status_info.get("error"),
                "has_headers": "headers" in server,
                "auth_hint": server.get("auth_hint", ""),
                "extension_id": server.get("extension_id"),
                "extension_reference": server.get("extension_reference"),
                "extension_display_name": server.get("extension_display_name"),
                "source": server.get("source", "manual"),
            }
            result.append(entry)
        return result

    # --- Token management ---

    def set_token(self, name: str, token: str) -> bool:
        """Set auth token for a server. Reconnects if enabled. Returns False if not found."""
        if name not in self._config:
            return False
        server = self._config[name]
        secret_key = self._token_secret_key(name)
        self._run_async(vault_repository.store(
            secret_key,
            token,
            description=f"MCP bearer token for server '{name}'",
        ))
        if "headers" not in server:
            server["headers"] = {}
        server["headers"]["Authorization"] = f"Bearer ${{vault:{secret_key}}}"
        self._save_config()
        if server.get("enabled", True):
            self.disconnect(name)
            self.connect(name, server["url"], headers=server.get("headers"))
        log_integration_event_sync(
            integration_type="mcp_server",
            name=name,
            outcome="credential_updated",
            details={
                "credential_source": "vault",
                "header_name": "Authorization",
                "secret_key": secret_key,
                "enabled": bool(server.get("enabled", True)),
            },
        )
        return True

    # --- Runtime config mutations ---

    def add_server(self, name: str, url: str,
                   description: str = "", enabled: bool = True,
                   headers: dict[str, str] | None = None,
                   auth_hint: str = "",
                   extension_id: str | None = None,
                   extension_reference: str | None = None,
                   extension_display_name: str | None = None,
                   source: str | None = None) -> None:
        """Add a new server to config and optionally connect it."""
        self._raise_for_endpoint_policy(url)
        self._config[name] = {
            "url": url,
            "enabled": enabled,
            "description": description,
        }
        if headers:
            self._config[name]["headers"] = headers
        if auth_hint:
            self._config[name]["auth_hint"] = auth_hint
        if extension_id:
            self._config[name]["extension_id"] = extension_id
        if extension_reference:
            self._config[name]["extension_reference"] = extension_reference
        if extension_display_name:
            self._config[name]["extension_display_name"] = extension_display_name
        if source:
            self._config[name]["source"] = source
        if enabled:
            self.connect(name, url, headers=headers)
        self._save_config()

    def update_server(self, name: str, **kwargs) -> bool:
        """Update server config. Returns False if server not found."""
        if name not in self._config:
            return False
        if "url" in kwargs:
            self._raise_for_endpoint_policy(str(kwargs["url"]))
        server = self._config[name]
        was_enabled = bool(server.get("enabled", True))
        previous_url = server.get("url")
        previous_headers = dict(server.get("headers", {})) if isinstance(server.get("headers"), dict) else None
        reconnected_from_toggle = False

        if "headers" in kwargs:
            if kwargs["headers"]:
                server["headers"] = kwargs["headers"]
            else:
                server.pop("headers", None)

        if "enabled" in kwargs:
            server["enabled"] = kwargs["enabled"]
            if kwargs["enabled"] and not was_enabled:
                self.connect(name, server["url"], headers=server.get("headers"))
                reconnected_from_toggle = True
            elif not kwargs["enabled"] and was_enabled:
                self.disconnect(name)

        if "url" in kwargs:
            server["url"] = kwargs["url"]
        if "description" in kwargs:
            server["description"] = kwargs["description"]
        if "auth_hint" in kwargs:
            if kwargs["auth_hint"]:
                server["auth_hint"] = kwargs["auth_hint"]
            else:
                server.pop("auth_hint", None)
        if "extension_id" in kwargs:
            if kwargs["extension_id"]:
                server["extension_id"] = kwargs["extension_id"]
            else:
                server.pop("extension_id", None)
        if "extension_reference" in kwargs:
            if kwargs["extension_reference"]:
                server["extension_reference"] = kwargs["extension_reference"]
            else:
                server.pop("extension_reference", None)
        if "extension_display_name" in kwargs:
            if kwargs["extension_display_name"]:
                server["extension_display_name"] = kwargs["extension_display_name"]
            else:
                server.pop("extension_display_name", None)
        if "source" in kwargs:
            if kwargs["source"]:
                server["source"] = kwargs["source"]
            else:
                server.pop("source", None)

        current_headers = dict(server.get("headers", {})) if isinstance(server.get("headers"), dict) else None
        reconnect_required = (
            bool(server.get("enabled", True))
            and not reconnected_from_toggle
            and (
                ("url" in kwargs and server.get("url") != previous_url)
                or ("headers" in kwargs and current_headers != previous_headers)
            )
        )
        if reconnect_required:
            self.disconnect(name)
            self.connect(name, server["url"], headers=server.get("headers"))

        self._save_config()
        return True

    def remove_server(self, name: str) -> bool:
        """Remove a server from config and disconnect it. Returns False if not found."""
        if name not in self._config:
            return False
        self.disconnect(name)
        del self._config[name]
        self._save_config()
        return True


mcp_manager = MCPManager()
