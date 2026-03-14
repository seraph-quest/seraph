"""Tests for high-risk tool approval wrappers."""

from smolagents import Tool
import pytest

from src.approval.exceptions import ApprovalRequired
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.tools.approval import wrap_tools_for_approval


class DummyShellTool(Tool):
    name = "shell_execute"
    description = "Dummy high-risk shell tool"
    inputs = {"code": {"type": "string", "description": "Code to run"}}
    output_type = "string"

    def forward(self, code: str) -> str:
        return f"ran:{code}"


def test_high_risk_tool_requires_approval_before_execution(async_db):
    tool = wrap_tools_for_approval([DummyShellTool()])[0]
    tokens = set_runtime_context("s1", "high_risk")
    try:
        with pytest.raises(ApprovalRequired):
            tool(code="print('hi')")
    finally:
        reset_runtime_context(tokens)


def test_high_risk_tool_runs_without_approval_mode(async_db):
    tool = wrap_tools_for_approval([DummyShellTool()])[0]
    tokens = set_runtime_context("s1", "off")
    try:
        assert tool(code="print('hi')") == "ran:print('hi')"
    finally:
        reset_runtime_context(tokens)
