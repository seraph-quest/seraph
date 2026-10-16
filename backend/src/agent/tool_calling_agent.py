"""Seraph's ToolCallingAgent: smolagents' agent with control-flow interrupts kept intact.

smolagents' ``ToolCallingAgent.execute_tool_call`` catches every exception a
tool raises and re-raises it as ``AgentToolExecutionError``, which the agent
loop records as an ordinary error observation before asking the model again.
That is right for tool failures, but it swallows deliberate pauses such as
``ApprovalRequired`` and ``ClarificationRequired``: the chat transports never
see them, so the operator gets no approval prompt or clarification.

Every Seraph agent factory builds this subclass. It re-raises any
``AgentControlFlowInterrupt`` found in the tool error's explicit cause chain
out of ``agent.run``; all other tool errors still become observations. A
managed or delegated sub-agent built from this class propagates the same way,
because the parent unwraps the interrupt again from its own tool call.
"""
from __future__ import annotations

from typing import Any

from smolagents import ToolCallingAgent as _SmolagentsToolCallingAgent
from smolagents.utils import AgentToolExecutionError

from src.agent.exceptions import AgentControlFlowInterrupt


def control_flow_interrupt(error: BaseException) -> AgentControlFlowInterrupt | None:
    """The interrupt in ``error``'s explicit ``raise ... from`` chain, if any."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(seen) < 8:
        if isinstance(current, AgentControlFlowInterrupt):
            return current
        seen.add(id(current))
        current = current.__cause__
    return None


class ToolCallingAgent(_SmolagentsToolCallingAgent):
    def execute_tool_call(self, tool_name: str, arguments: dict[str, str] | str) -> Any:
        try:
            return super().execute_tool_call(tool_name, arguments)
        except AgentToolExecutionError as error:
            interrupt = control_flow_interrupt(error)
            if interrupt is None:
                raise
            raise interrupt from None
