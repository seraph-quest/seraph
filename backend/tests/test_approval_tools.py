"""Tests for high-risk tool approval wrappers."""

import asyncio

from smolagents import Tool
import pytest

from src.approval.exceptions import ApprovalRequired
from src.approval.repository import approval_repository
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.tools.approval import wrap_tools_for_approval, wrap_tools_with_forced_approval
from src.tools.process_tools import start_process
from src.tools.secret_ref_tools import wrap_tools_for_secret_refs


class DummyExecuteCodeTool(Tool):
    name = "execute_code"
    description = "Dummy high-risk execute-code tool"
    inputs = {"code": {"type": "string", "description": "Code to run"}}
    output_type = "string"

    def forward(self, code: str) -> str:
        return f"ran:{code}"


class DummyPrivilegedWorkflowTool(Tool):
    name = "workflow_release_repair"
    description = "Dummy workflow-shaped privileged tool"
    inputs = {"file_path": {"type": "string", "description": "Path"}}
    output_type = "string"

    def __init__(self, *, boundary: str = "workspace_write"):
        super().__init__()
        self.boundary = boundary
        self.calls: list[str] = []
        self.is_initialized = True

    def forward(self, file_path: str) -> str:
        self.calls.append(file_path)
        return f"saved:{file_path}"

    def get_approval_context(self, _arguments):
        return {
            "workflow_name": "release-repair",
            "risk_level": "high",
            "execution_boundaries": [self.boundary],
            "accepts_secret_refs": False,
            "step_tools": ["write_file"],
        }


class DummyAuthenticatedMCPTool(Tool):
    name = "mcp_fetch_repo"
    description = "Dummy authenticated MCP tool"
    inputs = {"query": {"type": "string", "description": "Query"}}
    output_type = "string"

    def __init__(self):
        super().__init__()
        self.seraph_source_context = {
            "server_name": "github",
            "authenticated_source": True,
        }
        self.is_initialized = True

    def forward(self, query: str) -> str:
        return f"ok:{query}"

    def get_approval_context(self, _arguments):
        return {
            "execution_boundaries": ["external_mcp", "authenticated_external_source"],
            "authenticated_source": True,
        }


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


def test_forced_approval_does_not_consume_approval_after_boundary_context_changes(async_db):
    tool_impl = DummyPrivilegedWorkflowTool(boundary="workspace_write")
    tool = wrap_tools_with_forced_approval([tool_impl])[0]
    tokens = set_runtime_context("s1", "off")
    try:
        with pytest.raises(ApprovalRequired) as excinfo:
            tool(file_path="notes/release.md")
        approval_id = excinfo.value.approval_id
        assert asyncio.run(approval_repository.resolve(approval_id, "approved")) is not None

        tool_impl.boundary = "secret_injection"
        with pytest.raises(ApprovalRequired):
            tool(file_path="notes/release.md")
    finally:
        reset_runtime_context(tokens)

    assert tool_impl.calls == []


def test_secret_ref_wrapper_preserves_authenticated_mcp_approval_context(async_db):
    tool = wrap_tools_for_approval(
        wrap_tools_for_secret_refs([DummyAuthenticatedMCPTool()]),
        treat_all_as_mcp=True,
    )[0]
    tokens = set_runtime_context("s1", "high_risk")
    try:
        with pytest.raises(ApprovalRequired) as excinfo:
            tool(query="repo")
        pending = asyncio.run(approval_repository.list_pending(session_id="s1"))
    finally:
        reset_runtime_context(tokens)

    request = next(item for item in pending if item["id"] == excinfo.value.approval_id)
    assert request["approval_context"]["authenticated_source"] is True
    assert request["approval_context"]["execution_boundaries"] == [
        "external_mcp",
        "authenticated_external_source",
    ]


def test_start_process_requires_approval_even_when_global_mode_is_off(async_db):
    tool = wrap_tools_for_approval([start_process])[0]
    tokens = set_runtime_context("s1", "off")
    try:
        with pytest.raises(ApprovalRequired) as excinfo:
            tool(command="pwd")
        pending = asyncio.run(approval_repository.list_pending(session_id="s1"))
    finally:
        reset_runtime_context(tokens)

    request = next(item for item in pending if item["id"] == excinfo.value.approval_id)
    assert request["tool_name"] == "start_process"
    assert request["approval_context"]["confirmation_scope"] == "background_process_lifecycle"
    assert request["approval_context"]["persistent_background_execution"] is True
    assert request["approval_context"]["session_process_partition"] is True
    assert request["approval_context"]["runtime_log_storage"] == "temp_runtime_outside_workspace"
