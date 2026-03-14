"""Tests for tool list filtering by tool policy mode."""

import pytest
from unittest.mock import patch

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
