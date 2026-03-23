"""Tests for high-risk tool approval wrappers."""

from smolagents import Tool
import pytest

from src.approval.exceptions import ApprovalRequired
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.tools.approval import wrap_tools_for_approval, wrap_tools_with_forced_approval


class DummyExecuteCodeTool(Tool):
    name = "execute_code"
    description = "Dummy high-risk execute-code tool"
    inputs = {"code": {"type": "string", "description": "Code to run"}}
    output_type = "string"

    def forward(self, code: str) -> str:
        return f"ran:{code}"


def test_high_risk_tool_requires_approval_before_execution(async_db):
    tool = wrap_tools_for_approval([DummyExecuteCodeTool()])[0]
    tokens = set_runtime_context("s1", "high_risk")
    try:
        with pytest.raises(ApprovalRequired):
            tool(code="print('hi')")
    finally:
        reset_runtime_context(tokens)


def test_high_risk_tool_runs_without_approval_mode(async_db):
    tool = wrap_tools_for_approval([DummyExecuteCodeTool()])[0]
    tokens = set_runtime_context("s1", "off")
    try:
        assert tool(code="print('hi')") == "ran:print('hi')"
    finally:
        reset_runtime_context(tokens)


def test_forced_approval_tool_requires_confirmation_even_when_global_mode_off(async_db):
    tool = wrap_tools_with_forced_approval([DummyExecuteCodeTool()])[0]
    tokens = set_runtime_context("s1", "off")
    try:
        with pytest.raises(ApprovalRequired):
            tool(code="print('hi')")
    finally:
        reset_runtime_context(tokens)
