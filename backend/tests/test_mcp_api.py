"""Tests for MCP API endpoints (src/api/mcp.py)."""

from unittest.mock import MagicMock, patch

import pytest

from src.audit.repository import audit_repository


@pytest.mark.asyncio
async def test_validate_server_returns_missing_env_var_warnings(client):
    with patch("src.api.mcp.mcp_manager") as mock_mgr:
        mock_mgr.inspect_headers.return_value = (["API_TOKEN"], [], ["env"])
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
        mock_mgr.inspect_headers.return_value = ([], [], [])
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
async def test_validate_server_rejects_raw_sensitive_header(client):
    with patch("src.api.mcp.mcp_manager") as mock_mgr:
        mock_mgr.inspect_headers.return_value = ([], [], ["inline"])
        mock_mgr._config = {}

        resp = await client.post(
            "/api/mcp/servers/validate",
            json={
                "name": "gh",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer raw-token"},
                "enabled": True,
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert data["status"] == "invalid"
    assert "Sensitive header 'Authorization'" in data["issues"][0]


@pytest.mark.asyncio
async def test_validate_server_rejects_mixed_raw_and_placeholder_sensitive_header(client):
    with patch("src.api.mcp.mcp_manager") as mock_mgr:
        mock_mgr.inspect_headers.return_value = ([], [], ["env"])
        mock_mgr._config = {}

        resp = await client.post(
            "/api/mcp/servers/validate",
            json={
                "name": "gh",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer raw-token${DUMMY}"},
                "enabled": True,
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert data["status"] == "invalid"
    assert "Sensitive header 'Authorization'" in data["issues"][0]


@pytest.mark.asyncio
async def test_validate_server_degrades_when_credential_inspection_fails(client):
    with patch("src.api.mcp.mcp_manager") as mock_mgr:
        mock_mgr.inspect_headers.side_effect = RuntimeError("vault unavailable")
        mock_mgr._config = {}

        resp = await client.post(
            "/api/mcp/servers/validate",
            json={
                "name": "gh",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer ${vault:mcp.server.gh.bearer_token}"},
                "enabled": True,
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert data["status"] == "invalid"
    assert "Credential inspection failed: vault unavailable" in data["issues"]


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
async def test_set_token_rejects_extension_managed_entry(client):
    with patch("src.api.mcp.mcp_manager") as mock_mgr:
        mock_mgr._config = {
            "github-packaged": {
                "url": "https://example.test/mcp",
                "source": "extension",
                "extension_id": "seraph.test-connector",
                "extension_display_name": "Test Connector",
            }
        }

        resp = await client.post(
            "/api/mcp/servers/github-packaged/token",
            json={"token": "secret"},
        )

    assert resp.status_code == 409
    assert "token updates" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_update_server_rejects_extension_managed_entry(client):
    with patch("src.api.mcp.mcp_manager") as mock_mgr:
        mock_mgr._config = {
            "github-packaged": {
                "url": "https://example.test/mcp",
                "source": "extension",
                "extension_id": "seraph.test-connector",
                "extension_display_name": "Test Connector",
            }
        }

        resp = await client.put("/api/mcp/servers/github-packaged", json={"enabled": False})

    assert resp.status_code == 409
    assert "managed by Test Connector" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_add_server_rejects_raw_sensitive_header(client):
    with patch("src.api.mcp.mcp_manager") as mock_mgr:
        mock_mgr._config = {}

        resp = await client.post(
            "/api/mcp/servers",
            json={
                "name": "gh",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer raw-token"},
                "enabled": True,
            },
        )

    assert resp.status_code == 400
    assert "Sensitive header 'Authorization'" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_add_server_rejects_mixed_raw_and_placeholder_sensitive_header(client):
    with patch("src.api.mcp.mcp_manager") as mock_mgr:
        mock_mgr._config = {}

        resp = await client.post(
            "/api/mcp/servers",
            json={
                "name": "gh",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer raw-token${DUMMY}"},
                "enabled": True,
            },
        )

    assert resp.status_code == 400
    assert "Sensitive header 'Authorization'" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_remove_server_rejects_extension_managed_entry(client):
    with patch("src.api.mcp.mcp_manager") as mock_mgr:
        mock_mgr._config = {
            "github-packaged": {
                "url": "https://example.test/mcp",
                "source": "extension",
                "extension_id": "seraph.test-connector",
                "extension_display_name": "Test Connector",
            }
        }

        resp = await client.delete("/api/mcp/servers/github-packaged")

    assert resp.status_code == 409
    assert "extension connector lifecycle" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_test_server_rejects_extension_managed_entry(client):
    with patch("src.api.mcp.mcp_manager") as mock_mgr:
        mock_mgr._config = {
            "github-packaged": {
                "url": "https://example.test/mcp",
                "source": "extension",
                "extension_id": "seraph.test-connector",
                "extension_display_name": "Test Connector",
            }
        }

        resp = await client.post("/api/mcp/servers/github-packaged/test")

    assert resp.status_code == 409
    assert "raw MCP tests" in resp.json()["detail"]


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
        mock_mgr.resolve_headers.return_value = (
            {"Authorization": "Bearer ${GITHUB_TOKEN}"},
            ["GITHUB_TOKEN"],
            [],
            ["env"],
        )

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
        mock_mgr.resolve_headers.return_value = (
            {"Authorization": "Bearer ${GITHUB_TOKEN}"},
            ["GITHUB_TOKEN"],
            [],
            ["env"],
        )

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
        mock_mgr.resolve_headers.return_value = (
            {"Authorization": "Bearer ghp_test"},
            [],
            [],
            ["env"],
        )

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
        mock_mgr.resolve_headers.return_value = (None, [], [], [])

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
        mock_mgr.resolve_headers.return_value = (None, [], [], [])

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


@pytest.mark.asyncio
async def test_test_server_credential_resolution_failure_degrades_to_auth_required(async_db, client):
    with patch("src.api.mcp.mcp_manager") as mock_mgr:
        mock_mgr._config = {
            "gh": {
                "url": "http://gh/mcp",
                "headers": {"Authorization": "Bearer ${vault:mcp.server.gh.bearer_token}"},
            }
        }
        mock_mgr.resolve_headers.side_effect = RuntimeError("vault unavailable")

        resp = await client.post("/api/mcp/servers/gh/test")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "auth_required"
    assert data["message"] == "Credential resolution failed: vault unavailable"

    events = await audit_repository.list_events(limit=10)
    assert any(
        event["event_type"] == "integration_auth_required"
        and event["tool_name"] == "mcp_test:gh"
        and event["details"]["status"] == "credential_resolution_failed"
        and event["details"]["error"] == "vault unavailable"
        for event in events
    )
