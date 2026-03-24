"""Tests for tool list filtering by tool policy mode."""

import pytest
from unittest.mock import patch, MagicMock

from src.observer.context import CurrentContext


@pytest.mark.asyncio
async def test_tools_api_full_mode_includes_execute_code(client):
    ctx = CurrentContext(tool_policy_mode="full")
    with patch("src.tools.policy.context_manager.get_context", return_value=ctx):
        resp = await client.get("/api/tools")
    assert resp.status_code == 200
    names = {tool["name"] for tool in resp.json()}
    assert "execute_code" in names
    assert "run_command" in names
    assert "start_process" in names
    assert "list_processes" in names
    assert "read_process_output" in names
    assert "stop_process" in names


@pytest.mark.asyncio
async def test_tools_api_safe_mode_keeps_clarify_available(client):
    ctx = CurrentContext(tool_policy_mode="safe")
    with patch("src.tools.policy.context_manager.get_context", return_value=ctx):
        resp = await client.get("/api/tools")
    assert resp.status_code == 200
    names = {tool["name"] for tool in resp.json()}
    assert "clarify" in names


@pytest.mark.asyncio
async def test_tools_api_safe_mode_keeps_todo_available(client):
    ctx = CurrentContext(tool_policy_mode="safe")
    with patch("src.tools.policy.context_manager.get_context", return_value=ctx):
        resp = await client.get("/api/tools")
    assert resp.status_code == 200
    names = {tool["name"] for tool in resp.json()}
    assert "todo" in names


@pytest.mark.asyncio
async def test_tools_api_safe_mode_keeps_session_search_available(client):
    ctx = CurrentContext(tool_policy_mode="safe")
    with patch("src.tools.policy.context_manager.get_context", return_value=ctx):
        resp = await client.get("/api/tools")
    assert resp.status_code == 200
    names = {tool["name"] for tool in resp.json()}
    assert "session_search" in names


@pytest.mark.asyncio
async def test_tools_api_safe_mode_keeps_browser_session_available(client):
    ctx = CurrentContext(tool_policy_mode="safe")
    with patch("src.tools.policy.context_manager.get_context", return_value=ctx):
        resp = await client.get("/api/tools")
    assert resp.status_code == 200
    names = {tool["name"] for tool in resp.json()}
    assert "browser_session" in names


@pytest.mark.asyncio
async def test_tools_api_safe_mode_keeps_get_scheduled_jobs_available(client):
    ctx = CurrentContext(tool_policy_mode="safe")
    with patch("src.tools.policy.context_manager.get_context", return_value=ctx):
        resp = await client.get("/api/tools")
    assert resp.status_code == 200
    names = {tool["name"] for tool in resp.json()}
    assert "get_scheduled_jobs" in names


@pytest.mark.asyncio
async def test_tools_api_balanced_mode_keeps_delegate_task_available(client):
    ctx = CurrentContext(tool_policy_mode="balanced")
    with (
        patch("src.tools.policy.context_manager.get_context", return_value=ctx),
        patch("src.agent.factory.settings.use_delegation", True),
    ):
        resp = await client.get("/api/tools")
    assert resp.status_code == 200
    names = {tool["name"] for tool in resp.json()}
    assert "delegate_task" in names


@pytest.mark.asyncio
async def test_tools_api_balanced_mode_keeps_manage_scheduled_job_available(client):
    ctx = CurrentContext(tool_policy_mode="balanced")
    with patch("src.tools.policy.context_manager.get_context", return_value=ctx):
        resp = await client.get("/api/tools")
    assert resp.status_code == 200
    names = {tool["name"] for tool in resp.json()}
    assert "manage_scheduled_job" in names


@pytest.mark.asyncio
async def test_tools_api_hides_delegate_task_when_delegation_is_disabled(client):
    ctx = CurrentContext(tool_policy_mode="full")
    with (
        patch("src.tools.policy.context_manager.get_context", return_value=ctx),
        patch("src.agent.factory.settings.use_delegation", False),
    ):
        resp = await client.get("/api/tools")
    assert resp.status_code == 200
    names = {tool["name"] for tool in resp.json()}
    assert "delegate_task" not in names


@pytest.mark.asyncio
async def test_tools_api_balanced_mode_hides_full_only_tools(client):
    ctx = CurrentContext(tool_policy_mode="balanced")
    with patch("src.tools.policy.context_manager.get_context", return_value=ctx):
        resp = await client.get("/api/tools")
    assert resp.status_code == 200
    names = {tool["name"] for tool in resp.json()}
    assert "write_file" in names
    assert "execute_code" not in names
    assert "run_command" not in names
    assert "get_secret" not in names


@pytest.mark.asyncio
async def test_tools_api_hides_mcp_tools_when_disabled(client):
    ctx = CurrentContext(tool_policy_mode="full", mcp_policy_mode="disabled")
    mcp_tool = MagicMock()
    mcp_tool.name = "mcp_tasks"
    mcp_tool.description = "Task MCP"
    with patch("src.tools.policy.context_manager.get_context", return_value=ctx), \
         patch("src.agent.factory.mcp_manager.get_tools", return_value=[mcp_tool]):
        resp = await client.get("/api/tools")
    assert resp.status_code == 200
    names = {tool["name"] for tool in resp.json()}
    assert "mcp_tasks" not in names


@pytest.mark.asyncio
async def test_tools_api_marks_mcp_tools_as_approval_required_in_approval_mode(client):
    ctx = CurrentContext(tool_policy_mode="full", mcp_policy_mode="approval")
    mcp_tool = MagicMock()
    mcp_tool.name = "mcp_tasks"
    mcp_tool.description = "Task MCP"
    with patch("src.tools.policy.context_manager.get_context", return_value=ctx), \
         patch("src.agent.factory.mcp_manager.get_tools", return_value=[mcp_tool]):
        resp = await client.get("/api/tools")
    assert resp.status_code == 200
    mcp_entry = next(tool for tool in resp.json() if tool["name"] == "mcp_tasks")
    assert mcp_entry["requires_approval"] is True
    assert mcp_entry["policy_modes"] == ["approval"]
    assert mcp_entry["approval_behavior"] == "always"
    assert mcp_entry["risk_level"] == "high"
    assert mcp_entry["execution_boundaries"] == ["external_mcp"]
    assert mcp_entry["accepts_secret_refs"] is True


@pytest.mark.asyncio
async def test_tools_api_allows_mcp_tools_with_balanced_native_policy_when_mcp_approval_enabled(client):
    ctx = CurrentContext(tool_policy_mode="balanced", mcp_policy_mode="approval")
    mcp_tool = MagicMock()
    mcp_tool.name = "mcp_tasks"
    mcp_tool.description = "Task MCP"
    with patch("src.tools.policy.context_manager.get_context", return_value=ctx), \
         patch("src.agent.factory.mcp_manager.get_tools", return_value=[mcp_tool]):
        resp = await client.get("/api/tools")
    assert resp.status_code == 200
    mcp_entry = next(tool for tool in resp.json() if tool["name"] == "mcp_tasks")
    assert mcp_entry["requires_approval"] is True
    assert mcp_entry["policy_modes"] == ["approval"]
    assert mcp_entry["approval_behavior"] == "always"


@pytest.mark.asyncio
async def test_tools_api_surfaces_workflow_execution_boundaries(client):
    ctx = CurrentContext(tool_policy_mode="balanced", mcp_policy_mode="disabled")
    workflow_tool = MagicMock()
    workflow_tool.name = "workflow_web_brief_to_file"
    workflow_tool.description = "fallback description"

    with (
        patch("src.tools.policy.context_manager.get_context", return_value=ctx),
        patch("src.api.tools.get_tools", return_value=[workflow_tool]),
        patch("src.api.tools.workflow_manager.get_tool_metadata", return_value={
            "description": "Search the web and save a note",
            "policy_modes": ["balanced", "full"],
            "risk_level": "medium",
            "execution_boundaries": ["external_read", "workspace_write"],
        }),
    ):
        resp = await client.get("/api/tools")
    assert resp.status_code == 200
    assert resp.json() == [{
            "name": "workflow_web_brief_to_file",
            "description": "Search the web and save a note",
            "policy_modes": ["balanced", "full"],
            "requires_approval": False,
            "approval_behavior": "never",
            "risk_level": "medium",
            "execution_boundaries": ["external_read", "workspace_write"],
            "accepts_secret_refs": False,
        }]
