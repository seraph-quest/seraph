"""Wrappers and tools for session-scoped secret references."""

from __future__ import annotations

from smolagents import Tool, tool

from src.approval.runtime import get_current_session_id
from src.tools.vault_tools import _log_secret_event, _run
from src.vault.refs import issue_secret_ref, resolve_secret_refs
from src.vault.repository import vault_repository


class SecretRefResolvingTool(Tool):
    """Tool wrapper that resolves opaque secret references before execution."""

    skip_forward_signature_validation = True

    def __init__(self, wrapped_tool: Tool):
        super().__init__()
        self.wrapped_tool = wrapped_tool
        self.name = str(getattr(wrapped_tool, "name", "wrapped_tool"))
        description = getattr(wrapped_tool, "description", "")
        self.description = description if isinstance(description, str) else ""
        inputs = getattr(wrapped_tool, "inputs", {})
        self.inputs = inputs if isinstance(inputs, dict) else {}
        output_type = getattr(wrapped_tool, "output_type", "string")
        self.output_type = output_type if isinstance(output_type, str) else "string"
        output_schema = getattr(wrapped_tool, "output_schema", None)
        self.output_schema = output_schema if isinstance(output_schema, dict) else None
        self.is_initialized = True

    def forward(self, *args, **kwargs):
        return self.wrapped_tool(*args, **kwargs)

    def __call__(self, *args, sanitize_inputs_outputs: bool = False, **kwargs):
        session_id = get_current_session_id()
        resolved_args = tuple(resolve_secret_refs(arg, session_id) for arg in args)
        resolved_kwargs = {
            key: resolve_secret_refs(value, session_id)
            for key, value in kwargs.items()
        }
        return self.wrapped_tool(
            *resolved_args,
            sanitize_inputs_outputs=sanitize_inputs_outputs,
            **resolved_kwargs,
        )


def wrap_tools_for_secret_refs(tools: list[Tool]) -> list[Tool]:
    """Wrap tools so secret refs are resolved just before invocation."""
    return [SecretRefResolvingTool(tool) for tool in tools]


@tool
def get_secret_ref(key: str) -> str:
    """Create a session-scoped opaque reference for a stored secret.

    Use the returned `secret://...` reference directly inside later tool
    arguments such as HTTP headers. Seraph will resolve it at execution time
    without placing the raw secret value back into the model context.

    Args:
        key: The key of the stored secret to reference.

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

    secret_ref = issue_secret_ref(session_id, result)
    _log_secret_event(
        event_type="secret_ref_issue",
        tool_name="get_secret_ref",
        summary=f"Issued secret reference for key '{key}'",
        details={"key": key, "found": True},
    )
    return secret_ref
