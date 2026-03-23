"""Audit wrappers for tool executions across all agent transports."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from smolagents import Tool

from src.approval.runtime import get_current_session_id
from src.audit.formatting import format_tool_call_summary, redact_for_audit, summarize_tool_result
from src.audit.repository import audit_repository
from src.llm_runtime import get_current_llm_request_id
from src.tools.policy import get_current_tool_policy_mode, get_tool_risk_level

logger = logging.getLogger(__name__)


def _run_async(coro):
    return asyncio.run(coro)


def _custom_result_payload(tool: Any, arguments: dict[str, Any], result: Any) -> tuple[str, dict[str, Any]] | None:
    hook = getattr(tool, "get_audit_result_payload", None)
    if not callable(hook):
        return None
    payload = hook(arguments, result)
    if (
        isinstance(payload, tuple)
        and len(payload) == 2
        and isinstance(payload[0], str)
        and isinstance(payload[1], dict)
    ):
        return payload
    return None


def _custom_call_payload(tool: Any, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    hook = getattr(tool, "get_audit_call_payload", None)
    if not callable(hook):
        return None
    payload = hook(arguments)
    if (
        isinstance(payload, tuple)
        and len(payload) == 2
        and isinstance(payload[0], str)
        and isinstance(payload[1], dict)
    ):
        return payload
    return None


def _custom_audit_arguments(tool: Any, arguments: dict[str, Any]) -> dict[str, Any] | None:
    hook = getattr(tool, "get_audit_arguments", None)
    if not callable(hook):
        return None
    payload = hook(arguments)
    if isinstance(payload, dict):
        return payload
    return None


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
        audit_arguments = _custom_audit_arguments(self.wrapped_tool, arguments)
        if audit_arguments is None:
            audit_arguments = redact_for_audit(arguments)

        if session_id is None:
            return self.wrapped_tool(*args, sanitize_inputs_outputs=sanitize_inputs_outputs, **kwargs)

        custom_call_payload = _custom_call_payload(self.wrapped_tool, arguments)
        if custom_call_payload is not None:
            call_summary, call_details = custom_call_payload
        else:
            call_summary = format_tool_call_summary(self.name, arguments, set())
            call_details = {"arguments": audit_arguments}
        self._log_event(
            session_id=session_id,
            event_type="tool_call",
            summary=call_summary,
            details=call_details,
        )

        try:
            result = self.wrapped_tool(*args, sanitize_inputs_outputs=sanitize_inputs_outputs, **kwargs)
        except Exception as exc:
            self._log_event(
                session_id=session_id,
                event_type="tool_failed",
                summary=f"{self.name} raised an error",
                details={
                    "arguments": audit_arguments,
                    "error": redact_for_audit(str(exc)),
                },
            )
            raise

        custom_payload = _custom_result_payload(self.wrapped_tool, arguments, result)
        if custom_payload is not None:
            result_summary, result_details = custom_payload
        else:
            result_summary, result_details = summarize_tool_result(self.name, str(result))
        self._log_event(
            session_id=session_id,
            event_type="tool_result",
            summary=result_summary,
            details={
                "arguments": audit_arguments,
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
        request_id = get_current_llm_request_id()
        try:
            _run_async(
                audit_repository.log_event(
                    session_id=session_id,
                    actor="agent",
                    event_type=event_type,
                    tool_name=self.name,
                    risk_level=get_tool_risk_level(self.name, is_mcp=self.is_mcp),
                    policy_mode=get_current_tool_policy_mode(),
                    summary=summary,
                    details={
                        **details,
                        **({"request_id": request_id} if request_id else {}),
                    },
                )
            )
        except Exception:
            logger.debug("Failed to record tool execution audit event", exc_info=True)


def wrap_tools_for_audit(tools: list[Tool], *, treat_all_as_mcp: bool = False) -> list[Tool]:
    """Wrap tools so real executions always emit audit events."""
    return [
        AuditedTool(tool, is_mcp=treat_all_as_mcp or tool.name.startswith("mcp_"))
        for tool in tools
    ]
