"""Audit wrappers for tool executions across all agent transports."""

from __future__ import annotations

import asyncio
from typing import Any

from smolagents import Tool

from src.approval.runtime import get_current_session_id
from src.audit.formatting import format_tool_call_summary, redact_for_audit, summarize_tool_result
from src.audit.repository import audit_repository
from src.tools.policy import get_current_tool_policy_mode, get_tool_risk_level


def _run_async(coro):
    return asyncio.run(coro)


class AuditedTool(Tool):
    """Tool wrapper that records execution lifecycle events for real invocations."""

    skip_forward_signature_validation = True

    def __init__(self, wrapped_tool: Tool, *, is_mcp: bool = False):
        super().__init__()
        self.wrapped_tool = wrapped_tool
        self.is_mcp = is_mcp
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
        arguments = self._normalize_invocation(args, kwargs)

        if session_id is None:
            return self.wrapped_tool(*args, sanitize_inputs_outputs=sanitize_inputs_outputs, **kwargs)

        self._log_event(
            session_id=session_id,
            event_type="tool_call",
            summary=format_tool_call_summary(self.name, arguments, set()),
            details={"arguments": redact_for_audit(arguments)},
        )

        try:
            result = self.wrapped_tool(*args, sanitize_inputs_outputs=sanitize_inputs_outputs, **kwargs)
        except Exception as exc:
            self._log_event(
                session_id=session_id,
                event_type="tool_failed",
                summary=f"{self.name} raised an error",
                details={
                    "arguments": redact_for_audit(arguments),
                    "error": redact_for_audit(str(exc)),
                },
            )
            raise

        result_summary, result_details = summarize_tool_result(self.name, str(result))
        self._log_event(
            session_id=session_id,
            event_type="tool_result",
            summary=result_summary,
            details={
                "arguments": redact_for_audit(arguments),
                **result_details,
            },
        )
        return result

    def _normalize_invocation(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        if len(args) == 1 and not kwargs and isinstance(args[0], dict):
            return args[0]
        if kwargs:
            return kwargs
        input_names = list(self.inputs.keys())
        return {
            name: args[idx]
            for idx, name in enumerate(input_names)
            if idx < len(args)
        }

    def _log_event(
        self,
        *,
        session_id: str,
        event_type: str,
        summary: str,
        details: dict[str, Any],
    ) -> None:
        _run_async(
            audit_repository.log_event(
                session_id=session_id,
                actor="agent",
                event_type=event_type,
                tool_name=self.name,
                risk_level=get_tool_risk_level(self.name, is_mcp=self.is_mcp),
                policy_mode=get_current_tool_policy_mode(),
                summary=summary,
                details=details,
            )
        )


def wrap_tools_for_audit(tools: list[Tool], *, treat_all_as_mcp: bool = False) -> list[Tool]:
    """Wrap tools so real executions always emit audit events."""
    return [
        AuditedTool(tool, is_mcp=treat_all_as_mcp or tool.name.startswith("mcp_"))
        for tool in tools
    ]
