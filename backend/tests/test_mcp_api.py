"""Tests for MCP API endpoints (src/api/mcp.py)."""

from unittest.mock import MagicMock, patch

import pytest

from src.audit.repository import audit_repository


@pytest.mark.asyncio
async def test_validate_server_returns_missing_env_var_warnings(client):
    with patch("src.api.mcp.mcp_manager") as mock_mgr:
        mock_mgr._check_unresolved_vars.return_value = ["API_TOKEN"]
        mock_mgr._config = {}

        resp = await client.post(
            "/api/mcp/servers/validate",
            json={
                "name": "http-request",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer ${API_TOKEN}"},
                "enabled": True,
                "description": "HTTP tools",
                "auth_hint": "Set API_TOKEN",
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is True
    assert data["status"] == "auth_required"
    assert data["missing_env_vars"] == ["API_TOKEN"]
    assert data["warnings"]
    assert data["existing"] is False


@pytest.mark.asyncio
async def test_validate_server_rejects_invalid_url(client):
    with patch("src.api.mcp.mcp_manager") as mock_mgr:
        mock_mgr._check_unresolved_vars.return_value = []
        mock_mgr._config = {"gh": {"url": "https://api.github.com/mcp"}}

        resp = await client.post(
            "/api/mcp/servers/validate",
            json={"name": "gh", "url": "ftp://bad", "enabled": True},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert data["status"] == "invalid"
    assert "Server URL must use http or https" in data["issues"]
    assert data["existing"] is True


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


@pytest.mark.asyncio
async def test_test_server_auth_required_logs_runtime_audit(async_db, client):
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

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "integration_auth_required"
            and event["tool_name"] == "mcp_test:gh"
            and event["details"]["missing_env_vars"] == ["GITHUB_TOKEN"]
            for event in events
        )


@pytest.mark.asyncio
async def test_test_server_success_logs_runtime_audit(async_db, client):
    mock_tool = MagicMock(name="list_prs")
    mock_tool.name = "list_prs"
    mock_client = MagicMock()
    mock_client.get_tools.return_value = [mock_tool]

    with (
        patch("src.api.mcp.mcp_manager") as mock_mgr,
        patch("smolagents.MCPClient", return_value=mock_client),
    ):
        mock_mgr._config = {
            "gh": {
                "url": "http://gh/mcp",
                "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"},
            }
        }
        mock_mgr._check_unresolved_vars.return_value = []
        mock_mgr._resolve_env_vars.side_effect = lambda value: value.replace("${GITHUB_TOKEN}", "ghp_test")

        resp = await client.post("/api/mcp/servers/gh/test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["tool_count"] == 1
        assert data["tools"] == ["list_prs"]
        mock_client.disconnect.assert_called_once()

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "integration_succeeded"
            and event["tool_name"] == "mcp_test:gh"
            and event["details"]["tool_count"] == 1
            and event["details"]["tool_names"] == ["list_prs"]
            for event in events
        )


@pytest.mark.asyncio
async def test_test_server_auth_failed_logs_runtime_audit(async_db, client):
    with (
        patch("src.api.mcp.mcp_manager") as mock_mgr,
        patch("smolagents.MCPClient", side_effect=Exception("HTTP 401 Unauthorized")),
    ):
        mock_mgr._config = {
            "gh": {
                "url": "http://gh/mcp",
                "headers": None,
            }
        }
        mock_mgr._check_unresolved_vars.return_value = []

        resp = await client.post("/api/mcp/servers/gh/test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "auth_failed"

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "integration_failed"
            and event["tool_name"] == "mcp_test:gh"
            and event["details"]["status"] == "auth_failed"
            for event in events
        )


@pytest.mark.asyncio
async def test_test_server_connection_failure_logs_runtime_audit(async_db, client):
    with (
        patch("src.api.mcp.mcp_manager") as mock_mgr,
        patch("smolagents.MCPClient", side_effect=ConnectionError("refused")),
    ):
        mock_mgr._config = {
            "gh": {
                "url": "http://gh/mcp",
                "headers": None,
            }
        }
        mock_mgr._check_unresolved_vars.return_value = []

        resp = await client.post("/api/mcp/servers/gh/test")
        assert resp.status_code == 502

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "integration_failed"
            and event["tool_name"] == "mcp_test:gh"
            and event["details"]["status"] == "connection_failed"
            and event["details"]["error"] == "refused"
            for event in events
        )
