"""Tests for MCP API endpoints (src/api/mcp.py)."""

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_set_token_happy_path(client):
    """POST /api/mcp/servers/{name}/token should set token and return updated entry."""
    with patch("src.api.mcp.mcp_manager") as mock_mgr:
        mock_mgr.set_token.return_value = True
        mock_mgr.get_config.return_value = [
            {
                "name": "gh",
                "url": "http://gh/mcp",
                "enabled": True,
                "connected": True,
                "tool_count": 5,
                "description": "GitHub",
                "status": "connected",
                "status_message": None,
                "has_headers": True,
                "auth_hint": "",
            }
        ]

        resp = await client.post(
            "/api/mcp/servers/gh/token",
            json={"token": "ghp_abc123"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "updated"
        assert data["server"]["name"] == "gh"
        assert data["server"]["status"] == "connected"
        mock_mgr.set_token.assert_called_once_with("gh", "ghp_abc123")


@pytest.mark.asyncio
async def test_set_token_unknown_server(client):
    """POST /api/mcp/servers/{name}/token should return 404 for unknown server."""
    with patch("src.api.mcp.mcp_manager") as mock_mgr:
        mock_mgr.set_token.return_value = False

        resp = await client.post(
            "/api/mcp/servers/nonexistent/token",
            json={"token": "tok"},
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_test_server_auth_required_for_missing_vars(client):
    """POST /api/mcp/servers/{name}/test should return auth_required for unresolved env vars."""
    with patch("src.api.mcp.mcp_manager") as mock_mgr:
        mock_mgr._config = {
            "gh": {
                "url": "http://gh/mcp",
                "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"},
            }
        }
        mock_mgr._check_unresolved_vars.return_value = ["GITHUB_TOKEN"]

        resp = await client.post("/api/mcp/servers/gh/test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "auth_required"
        assert "GITHUB_TOKEN" in data["missing_env_vars"]
