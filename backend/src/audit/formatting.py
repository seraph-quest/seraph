"""Helpers for safe audit formatting and redaction."""

import json
from collections.abc import Mapping, Sequence
from typing import Any

_SENSITIVE_TOKENS = (
    "secret",
    "token",
    "password",
    "passphrase",
    "authorization",
    "auth",
    "cookie",
    "credential",
    "api_key",
    "apikey",
    "header",
    "key",
    "value",
)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower()
    return any(token in normalized for token in _SENSITIVE_TOKENS)


def redact_for_audit(value: Any, key_hint: str | None = None) -> Any:
    """Recursively redact sensitive fields before audit persistence."""
    if key_hint and _is_sensitive_key(key_hint):
        return "[redacted]"

    if isinstance(value, Mapping):
        return {str(key): redact_for_audit(inner, str(key)) for key, inner in value.items()}

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_for_audit(item, key_hint) for item in value]

    if isinstance(value, (bytes, bytearray)):
        return f"[binary:{len(value)} bytes]"

    if isinstance(value, str) and len(value) > 200:
        return f"{value[:197]}..."

    return value


def format_tool_call_summary(tool_name: str, arguments: Any, specialist_names: set[str]) -> str:
    """Create a safe summary string for tool calls."""
    safe_args = redact_for_audit(arguments)
    if tool_name in specialist_names:
        task = ""
        if isinstance(safe_args, Mapping):
            task = str(safe_args.get("task", ""))
        return f"Delegating to {tool_name}: {task}"
    return f"Calling tool: {tool_name}({json.dumps(safe_args)})"


def summarize_tool_result(tool_name: str | None, observations: str) -> tuple[str, dict[str, Any]]:
    """Return a safe audit summary/details pair for tool results."""
    label = tool_name or "tool"
    return (
        f"{label} returned output ({len(observations)} chars)",
        {
            "output_length": len(observations),
            "content_redacted": True,
        },
    )
