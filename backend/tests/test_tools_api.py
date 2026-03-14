"""Tests for tool list filtering by tool policy mode."""

import pytest
from unittest.mock import patch, MagicMock

from src.observer.context import CurrentContext


@pytest.mark.asyncio
async def test_tools_api_full_mode_includes_shell_execute(client):
    ctx = CurrentContext(tool_policy_mode="full")
    with patch("src.tools.policy.context_manager.get_context", return_value=ctx):
        resp = await client.get("/api/tools")
    assert resp.status_code == 200
    names = {tool["name"] for tool in resp.json()}
    assert "shell_execute" in names


@pytest.mark.asyncio
async def test_tools_api_balanced_mode_hides_full_only_tools(client):
    ctx = CurrentContext(tool_policy_mode="balanced")
    with patch("src.tools.policy.context_manager.get_context", return_value=ctx):
        resp = await client.get("/api/tools")
    assert resp.status_code == 200
    names = {tool["name"] for tool in resp.json()}
    assert "write_file" in names
    assert "shell_execute" not in names
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
