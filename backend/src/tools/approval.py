"""Approval wrappers for high-risk tool invocations."""

import asyncio
from typing import Any

from smolagents import Tool

from src.approval.exceptions import ApprovalRequired
from src.approval.repository import approval_repository, fingerprint_tool_call
from src.approval.runtime import get_current_approval_mode, get_current_session_id
from src.audit.formatting import format_tool_call_summary, redact_for_audit
from src.tools.policy import get_tool_risk_level


def _run_async(coro):
    return asyncio.run(coro)


class ApprovalTool(Tool):
    """Tool wrapper that pauses high-risk actions pending approval."""

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
        approval_mode = get_current_approval_mode()
        session_id = get_current_session_id()
        if approval_mode != "high_risk" or session_id is None:
            return self.wrapped_tool(*args, sanitize_inputs_outputs=sanitize_inputs_outputs, **kwargs)

        arguments = self._normalize_invocation(args, kwargs)
        fingerprint = fingerprint_tool_call(self.name, arguments)
        if _run_async(
            approval_repository.consume_approved(
                session_id=session_id,
                tool_name=self.name,
                fingerprint=fingerprint,
            )
        ):
            return self.wrapped_tool(*args, sanitize_inputs_outputs=sanitize_inputs_outputs, **kwargs)

        summary = format_tool_call_summary(self.name, arguments, set())
        request = _run_async(
            approval_repository.get_or_create_pending(
                session_id=session_id,
                tool_name=self.name,
                risk_level=get_tool_risk_level(self.name, is_mcp=self.name.startswith("mcp_")),
                summary=summary,
                fingerprint=fingerprint,
                details={"arguments": redact_for_audit(arguments)},
            )
        )
        raise ApprovalRequired(
            approval_id=request.id,
            session_id=session_id,
            tool_name=self.name,
            risk_level=request.risk_level,
            summary=summary,
        )

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


def wrap_tools_for_approval(tools: list[Tool], *, treat_all_as_mcp: bool = False) -> list[Tool]:
    """Wrap high-risk tools with approval checkpoints."""
    wrapped: list[Tool] = []
    for tool in tools:
        risk_level = get_tool_risk_level(
            tool.name,
            is_mcp=treat_all_as_mcp or tool.name.startswith("mcp_"),
        )
        if risk_level == "high":
            wrapped.append(ApprovalTool(tool))
        else:
            wrapped.append(tool)
    return wrapped
