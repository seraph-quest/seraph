"""Wrappers and tools for session-scoped secret references."""

from __future__ import annotations

from urllib.parse import urlparse

from smolagents import Tool, tool

from src.approval.runtime import get_current_session_id
from src.security.site_policy import evaluate_site_access
from src.tools.policy import (
    get_tool_credential_egress_policy,
    get_tool_secret_ref_fields,
    tool_accepts_secret_refs,
)
from src.tools.vault_tools import _log_secret_event, _run
from src.vault.refs import _REF_PREFIX
from src.security.secure_host import redact_secret_values_from_payload
from src.vault.refs import issue_secret_ref, resolve_secret_refs_with_values
from src.vault.repository import vault_repository


class SecretRefResolvingTool(Tool):
    """Tool wrapper that resolves opaque secret references before execution."""

    skip_forward_signature_validation = True

    def __init__(self, wrapped_tool: Tool):
        super().__init__()
        self.wrapped_tool = wrapped_tool
        self.name = str(getattr(wrapped_tool, "name", "wrapped_tool"))
        self._is_mcp = self.name.startswith("mcp_")
        description = getattr(wrapped_tool, "description", "")
        self.description = description if isinstance(description, str) else ""
        inputs = getattr(wrapped_tool, "inputs", {})
        self.inputs = inputs if isinstance(inputs, dict) else {}
        output_type = getattr(wrapped_tool, "output_type", "string")
        self.output_type = output_type if isinstance(output_type, str) else "string"
        output_schema = getattr(wrapped_tool, "output_schema", None)
        self.output_schema = output_schema if isinstance(output_schema, dict) else None
        self.seraph_secret_ref_fields = get_tool_secret_ref_fields(
            self.name,
            is_mcp=self._is_mcp,
            tool=wrapped_tool,
        )
        self.is_initialized = True

    def forward(self, *args, **kwargs):
        return self.wrapped_tool(*args, **kwargs)

    def __call__(self, *args, sanitize_inputs_outputs: bool = False, **kwargs):
        session_id = get_current_session_id()
        invocation = self._normalize_invocation(args, kwargs)
        has_secret_refs = any(_contains_secret_ref(value) for value in invocation.values())
        if not tool_accepts_secret_refs(self.name, is_mcp=self._is_mcp, tool=self.wrapped_tool):
            if has_secret_refs:
                raise ValueError(
                    f"Tool '{self.name}' cannot receive secret references; "
                    "use an explicit secret-injection path instead."
                )
        secret_ref_fields = list(self.seraph_secret_ref_fields)
        if has_secret_refs:
            disallowed_fields = sorted(
                field_name
                for field_name, value in invocation.items()
                if _contains_secret_ref(value) and field_name not in secret_ref_fields
            )
            if disallowed_fields:
                field_list = ", ".join(disallowed_fields)
                raise ValueError(
                    f"Tool '{self.name}' can only receive secret references in allowlisted fields: "
                    f"{', '.join(secret_ref_fields) or 'none'}. Refs were provided in: {field_list}."
                )
            if self._is_mcp:
                egress_policy = get_tool_credential_egress_policy(
                    self.name,
                    is_mcp=True,
                    tool=self.wrapped_tool,
                )
                allowed_hosts = list((egress_policy or {}).get("allowed_hosts") or [])
                if str((egress_policy or {}).get("mode") or "") != "explicit_host_allowlist" or not allowed_hosts:
                    raise ValueError(
                        f"Tool '{self.name}' cannot receive secret references without an explicit credential egress allowlist."
                    )
                blocked_hosts = _disallowed_credential_destination_hosts(invocation, allowed_hosts)
                if blocked_hosts:
                    raise ValueError(
                        f"Tool '{self.name}' cannot receive secret references for non-allowlisted destination host(s): "
                        f"{', '.join(blocked_hosts)}."
                    )
        resolved_invocation: dict[str, object] = {}
        resolved_secret_values: list[str] = []
        for key, value in invocation.items():
            if key in secret_ref_fields:
                destination = _primary_credential_destination(invocation)
                resolved_value, secret_values = resolve_secret_refs_with_values(
                    value,
                    session_id,
                    tool_name=self.name,
                    field_name=key,
                    destination_host=destination.get("host"),
                    destination_scheme=destination.get("scheme"),
                    destination_port=destination.get("port"),
                    purpose="tool_credential_injection",
                )
                resolved_invocation[key] = resolved_value
                resolved_secret_values.extend(secret_values)
            else:
                resolved_invocation[key] = value

        result = self.wrapped_tool(
            sanitize_inputs_outputs=sanitize_inputs_outputs,
            **resolved_invocation,
        )
        if resolved_secret_values:
            _redacted, leaked = redact_secret_values_from_payload(result, resolved_secret_values)
            if leaked:
                raise ValueError(
                    f"Tool '{self.name}' returned resolved secret material; "
                    "redaction failure blocked the result."
                )
        return result

    def get_audit_result_payload(self, arguments, result):
        hook = getattr(self.wrapped_tool, "get_audit_result_payload", None)
        if callable(hook):
            return hook(arguments, result)
        return None

    def get_audit_failure_payload(self, arguments, error):
        hook = getattr(self.wrapped_tool, "get_audit_failure_payload", None)
        if callable(hook):
            return hook(arguments, error)
        return None

    def get_audit_call_payload(self, arguments):
        hook = getattr(self.wrapped_tool, "get_audit_call_payload", None)
        if callable(hook):
            return hook(arguments)
        return None

    def get_approval_context(self, arguments):
        hook = getattr(self.wrapped_tool, "get_approval_context", None)
        if callable(hook):
            return hook(arguments)
        return None

    def get_audit_arguments(self, arguments):
        hook = getattr(self.wrapped_tool, "get_audit_arguments", None)
        if callable(hook):
            return hook(arguments)
        return None

    def _normalize_invocation(self, args, kwargs):
        if len(args) == 1 and not kwargs and isinstance(args[0], dict):
            return dict(args[0])
        if kwargs:
            return dict(kwargs)
        input_names = list(self.inputs.keys())
        return {
            name: args[idx]
            for idx, name in enumerate(input_names)
            if idx < len(args)
        }


def wrap_tools_for_secret_refs(tools: list[Tool]) -> list[Tool]:
    """Wrap tools so secret refs are resolved just before invocation."""
    return [SecretRefResolvingTool(tool) for tool in tools]


def _contains_secret_ref(value) -> bool:
    if isinstance(value, str):
        return _REF_PREFIX in value
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_ref(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_secret_ref(item) for item in value.values())
    return False


def _disallowed_credential_destination_hosts(invocation: dict, allowed_hosts: list[str]) -> list[str]:
    normalized_allowed = {host.lower().strip() for host in allowed_hosts if isinstance(host, str) and host.strip()}
    discovered_hosts: set[str] = set()
    for key, value in invocation.items():
        if not _looks_like_destination_field(str(key)):
            continue
        for host in _extract_url_hosts(value):
            decision = evaluate_site_access(host, resolve_dns=True)
            if not decision.allowed or host.lower() not in normalized_allowed:
                discovered_hosts.add(host)
    return sorted(discovered_hosts)


def _looks_like_destination_field(field_name: str) -> bool:
    lowered = field_name.lower()
    return lowered in {"url", "uri", "endpoint", "base_url", "request_url", "target_url", "webhook_url"} or lowered.endswith("_url")


def _extract_url_hosts(value) -> list[str]:
    if isinstance(value, str):
        parsed = urlparse(value)
        return [parsed.hostname] if parsed.hostname else []
    if isinstance(value, dict):
        hosts: list[str] = []
        for item in value.values():
            hosts.extend(_extract_url_hosts(item))
        return hosts
    if isinstance(value, (list, tuple)):
        hosts: list[str] = []
        for item in value:
            hosts.extend(_extract_url_hosts(item))
        return hosts
    return []


@tool
def get_secret_ref(
    key: str,
    tool_name: str = "",
    field_name: str = "",
    destination_url: str = "",
    purpose: str = "tool_credential_injection",
    one_time: bool = False,
) -> str:
    """Create an opaque scoped reference for a stored secret.

    Use the returned `secret://...` reference directly inside later tool
    arguments such as HTTP headers. Seraph will resolve it at execution time
    without placing the raw secret value back into the model context.

    Args:
        key: The key of the stored secret to reference.
        tool_name: Tool name this ref may be resolved by.
        field_name: Top-level tool field this ref may be injected into.
        destination_url: Destination URL this ref may be used against.
        purpose: Optional purpose label for audit and scope matching.
        one_time: Whether the ref should be consumed after its first resolution.

    Returns:
        An opaque secret reference string, or a not-found message.
    """
    session_id = get_current_session_id()
    if session_id is None:
        return "Secret references require an active session."

    result = _run(vault_repository.get(key))
    if result is None:
        _log_secret_event(
            event_type="secret_ref_issue",
            tool_name="get_secret_ref",
            summary=f"Attempted to issue secret reference for missing key '{key}'",
            details={"key": key, "found": False},
        )
        return f"Secret '{key}' not found in vault."

    destination = _destination_from_url(destination_url)
    if not tool_name.strip() or not field_name.strip() or not destination.get("host"):
        _log_secret_event(
            event_type="secret_ref_issue",
            tool_name="get_secret_ref",
            summary=f"Blocked unscoped secret reference for key '{key}'",
            details={
                "key": key,
                "found": True,
                "scoped_tool": bool(tool_name.strip()),
                "scoped_field": bool(field_name.strip()),
                "scoped_destination": bool(destination.get("host")),
            },
        )
        return "Secret references require tool_name, field_name, and destination_url scope."

    secret_ref = issue_secret_ref(
        session_id,
        result,
        tool_name=tool_name,
        field_name=field_name,
        destination_host=destination.get("host"),
        destination_scheme=destination.get("scheme"),
        destination_port=destination.get("port"),
        purpose=purpose or None,
        one_time=one_time,
    )
    _log_secret_event(
        event_type="secret_ref_issue",
        tool_name="get_secret_ref",
        summary=f"Issued secret reference for key '{key}'",
        details={
            "key": key,
            "found": True,
            "scoped_tool": bool(tool_name),
            "scoped_field": bool(field_name),
            "scoped_destination": bool(destination.get("host")),
            "one_time": bool(one_time),
        },
    )
    return secret_ref


def _primary_credential_destination(invocation: dict) -> dict[str, object | None]:
    for key, value in invocation.items():
        if _looks_like_destination_field(str(key)):
            destination = _destination_from_nested_value(value)
            if destination.get("host"):
                return destination
    return {"scheme": None, "host": None, "port": None}


def _destination_from_nested_value(value) -> dict[str, object | None]:
    if isinstance(value, str):
        return _destination_from_url(value)
    if isinstance(value, dict):
        for item in value.values():
            destination = _destination_from_nested_value(item)
            if destination.get("host"):
                return destination
    if isinstance(value, (list, tuple)):
        for item in value:
            destination = _destination_from_nested_value(item)
            if destination.get("host"):
                return destination
    return {"scheme": None, "host": None, "port": None}


def _destination_from_url(value: str) -> dict[str, object | None]:
    if not isinstance(value, str) or not value.strip():
        return {"scheme": None, "host": None, "port": None}
    parsed = urlparse(value)
    if not parsed.hostname:
        return {"scheme": None, "host": None, "port": None}
    return {
        "scheme": parsed.scheme.lower() if parsed.scheme else None,
        "host": parsed.hostname.lower(),
        "port": parsed.port or (443 if parsed.scheme == "https" else 80 if parsed.scheme == "http" else None),
    }
