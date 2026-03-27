"""Tests for MCP manager (src/tools/mcp_manager.py)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.audit.repository import audit_repository
from src.tools.mcp_manager import MCPManager


class TestMCPManager:
    def test_empty_init(self):
        mgr = MCPManager()
        assert mgr.get_tools() == []
        assert mgr._clients == {}
        assert mgr._tools == {}

    @patch("src.tools.mcp_manager.MCPClient")
    def test_connect_success(self, MockMCPClient):
        mock_client = MagicMock()
        mock_client.get_tools.return_value = [MagicMock(name="tool1")]
        MockMCPClient.return_value = mock_client

        mgr = MCPManager()
        mgr.connect("things", "http://localhost:9000/mcp")
        assert len(mgr.get_tools()) == 1
        assert "things" in mgr._clients

    @patch("src.tools.mcp_manager.MCPClient")
    def test_connect_failure(self, MockMCPClient):
        MockMCPClient.side_effect = ConnectionError("refused")

        mgr = MCPManager()
        mgr.connect("things", "http://bad-url/mcp")
        assert mgr.get_tools() == []
        assert "things" not in mgr._clients

    @patch("src.tools.mcp_manager.MCPClient")
    def test_connect_multiple_servers(self, MockMCPClient):
        mock_client_a = MagicMock()
        mock_client_a.get_tools.return_value = [MagicMock(name="tool_a")]
        mock_client_b = MagicMock()
        mock_client_b.get_tools.return_value = [MagicMock(name="tool_b1"), MagicMock(name="tool_b2")]
        MockMCPClient.side_effect = [mock_client_a, mock_client_b]

        mgr = MCPManager()
        mgr.connect("things", "http://things:9100/mcp")
        mgr.connect("github", "http://github:8090/mcp")
        assert len(mgr.get_tools()) == 3
        assert len(mgr.get_server_tools("things")) == 1
        assert len(mgr.get_server_tools("github")) == 2

    @patch("src.tools.mcp_manager.MCPClient")
    def test_disconnect_one_server(self, MockMCPClient):
        mock_client_a = MagicMock()
        mock_client_a.get_tools.return_value = [MagicMock(name="tool_a")]
        mock_client_b = MagicMock()
        mock_client_b.get_tools.return_value = [MagicMock(name="tool_b")]
        MockMCPClient.side_effect = [mock_client_a, mock_client_b]

        mgr = MCPManager()
        mgr.connect("things", "http://things:9100/mcp")
        mgr.connect("github", "http://github:8090/mcp")
        mgr.disconnect("things")

        assert len(mgr.get_tools()) == 1
        assert mgr.get_server_tools("things") == []
        assert len(mgr.get_server_tools("github")) == 1
        mock_client_a.disconnect.assert_called_once()
        mock_client_b.disconnect.assert_not_called()

    @patch("src.tools.mcp_manager.MCPClient")
    def test_disconnect_all(self, MockMCPClient):
        mock_client_a = MagicMock()
        mock_client_a.get_tools.return_value = [MagicMock()]
        mock_client_b = MagicMock()
        mock_client_b.get_tools.return_value = [MagicMock()]
        MockMCPClient.side_effect = [mock_client_a, mock_client_b]

        mgr = MCPManager()
        mgr.connect("things", "http://things:9100/mcp")
        mgr.connect("github", "http://github:8090/mcp")
        mgr.disconnect_all()

        assert mgr.get_tools() == []
        assert mgr._clients == {}
        mock_client_a.disconnect.assert_called_once()
        mock_client_b.disconnect.assert_called_once()

    @patch("src.tools.mcp_manager.MCPClient")
    def test_get_tools_merges_all(self, MockMCPClient):
        tool1 = MagicMock(name="t1")
        tool2 = MagicMock(name="t2")
        tool3 = MagicMock(name="t3")

        mock_client_a = MagicMock()
        mock_client_a.get_tools.return_value = [tool1]
        mock_client_b = MagicMock()
        mock_client_b.get_tools.return_value = [tool2, tool3]
        MockMCPClient.side_effect = [mock_client_a, mock_client_b]

        mgr = MCPManager()
        mgr.connect("a", "http://a/mcp")
        mgr.connect("b", "http://b/mcp")

        merged = mgr.get_tools()
        assert merged == [tool1, tool2, tool3]

    def test_get_tools_without_connect(self):
        mgr = MCPManager()
        assert mgr.get_tools() == []

    def test_get_server_tools_unknown(self):
        mgr = MCPManager()
        assert mgr.get_server_tools("nonexistent") == []

    def test_disconnect_unknown_server(self):
        mgr = MCPManager()
        # Should not raise
        mgr.disconnect("nonexistent")

    def test_resolve_env_vars(self):
        mgr = MCPManager()
        import os
        os.environ["TEST_TOKEN_XYZ"] = "my-secret"
        try:
            assert mgr._resolve_env_vars("Bearer ${TEST_TOKEN_XYZ}") == "Bearer my-secret"
        finally:
            del os.environ["TEST_TOKEN_XYZ"]

    def test_resolve_env_vars_missing_kept(self):
        mgr = MCPManager()
        result = mgr._resolve_env_vars("Bearer ${NONEXISTENT_VAR_12345}")
        assert result == "Bearer ${NONEXISTENT_VAR_12345}"

    @patch("src.tools.mcp_manager.MCPClient")
    def test_connect_with_headers(self, MockMCPClient):
        mock_client = MagicMock()
        mock_client.get_tools.return_value = []
        MockMCPClient.return_value = mock_client

        mgr = MCPManager()
        import os
        os.environ["TEST_GH_TOKEN"] = "ghp_abc123"
        try:
            mgr.connect("github", "http://example.com/mcp",
                        headers={"Authorization": "Bearer ${TEST_GH_TOKEN}"})
        finally:
            del os.environ["TEST_GH_TOKEN"]

        call_args = MockMCPClient.call_args
        params = call_args[0][0]
        assert params["headers"]["Authorization"] == "Bearer ghp_abc123"

    @patch("src.tools.mcp_manager.MCPClient")
    def test_connect_without_headers(self, MockMCPClient):
        mock_client = MagicMock()
        mock_client.get_tools.return_value = []
        MockMCPClient.return_value = mock_client

        mgr = MCPManager()
        mgr.connect("http-request", "http://localhost:9200/mcp")

        call_args = MockMCPClient.call_args
        params = call_args[0][0]
        assert "headers" not in params

    @patch("src.tools.mcp_manager.MCPClient")
    def test_load_config_passes_headers(self, MockMCPClient, tmp_path):
        mock_client = MagicMock()
        mock_client.get_tools.return_value = []
        MockMCPClient.return_value = mock_client

        config_file = tmp_path / "mcp-servers.json"
        config_file.write_text('{"mcpServers": {"gh": {"url": "http://gh/mcp", "enabled": true, "headers": {"Authorization": "Bearer ${TEST_LH_TOKEN}"}}}}')

        import os
        os.environ["TEST_LH_TOKEN"] = "tok_xyz"
        try:
            mgr = MCPManager()
            mgr.load_config(str(config_file))
        finally:
            del os.environ["TEST_LH_TOKEN"]

        call_args = MockMCPClient.call_args
        params = call_args[0][0]
        assert params["headers"]["Authorization"] == "Bearer tok_xyz"

    @patch("src.tools.mcp_manager.MCPClient")
    def test_add_server_with_headers(self, MockMCPClient, tmp_path):
        mock_client = MagicMock()
        mock_client.get_tools.return_value = []
        MockMCPClient.return_value = mock_client

        mgr = MCPManager()
        mgr._config_path = str(tmp_path / "mcp-servers.json")
        mgr.add_server("gh", "http://gh/mcp", headers={"X-Key": "val"}, enabled=True)

        assert mgr._config["gh"]["headers"] == {"X-Key": "val"}
        call_args = MockMCPClient.call_args
        params = call_args[0][0]
        assert params["headers"]["X-Key"] == "val"

    @patch("src.tools.mcp_manager.MCPClient")
    def test_get_config_includes_has_headers(self, MockMCPClient):
        mock_client = MagicMock()
        mock_client.get_tools.return_value = []
        MockMCPClient.return_value = mock_client

        mgr = MCPManager()
        mgr._config["gh"] = {"url": "http://gh/mcp", "enabled": True, "headers": {"Authorization": "Bearer tok"}}
        mgr._config["plain"] = {"url": "http://plain/mcp", "enabled": True}

        configs = mgr.get_config()
        gh_entry = next(c for c in configs if c["name"] == "gh")
        plain_entry = next(c for c in configs if c["name"] == "plain")
        assert gh_entry.get("has_headers") is True
        assert plain_entry.get("has_headers") is False

    # --- Status tracking tests ---

    def test_flatten_exception_text_simple(self):
        mgr = MCPManager()
        exc = Exception("something broke")
        assert "something broke" in mgr._flatten_exception_text(exc)

    def test_flatten_exception_text_group(self):
        mgr = MCPManager()
        inner = Exception("401 Unauthorized")
        group = ExceptionGroup("task group error", [inner])
        text = mgr._flatten_exception_text(group)
        assert "401" in text
        assert "unauthorized" in text

    def test_check_unresolved_vars_returns_missing(self):
        mgr = MCPManager()
        missing = mgr._check_unresolved_vars({"Authorization": "Bearer ${MY_TOKEN}"})
        assert missing == ["MY_TOKEN"]

    def test_check_unresolved_vars_returns_empty_when_resolved(self):
        import os
        os.environ["TEST_RESOLVED_VAR"] = "value"
        try:
            mgr = MCPManager()
            missing = mgr._check_unresolved_vars({"Authorization": "Bearer ${TEST_RESOLVED_VAR}"})
            assert missing == []
        finally:
            del os.environ["TEST_RESOLVED_VAR"]

    def test_check_unresolved_vars_no_headers(self):
        mgr = MCPManager()
        assert mgr._check_unresolved_vars(None) == []

    @patch("src.tools.mcp_manager.vault_repository.exists", new_callable=AsyncMock)
    def test_check_missing_vault_secrets_returns_missing(self, mock_exists):
        mock_exists.return_value = False

        mgr = MCPManager()
        missing = mgr._check_missing_vault_secrets({"Authorization": "Bearer ${vault:mcp.server.gh.bearer_token}"})

        assert missing == ["mcp.server.gh.bearer_token"]

    @patch("src.tools.mcp_manager.vault_repository.exists", new_callable=AsyncMock)
    @patch("src.tools.mcp_manager.vault_repository.get", new_callable=AsyncMock)
    def test_resolve_headers_resolves_vault_backed_credentials(self, mock_get, mock_exists):
        mock_exists.return_value = True
        mock_get.return_value = "ghp_secret"

        resolved, missing_env, missing_vault, credential_sources = MCPManager.resolve_headers(
            {"Authorization": "Bearer ${vault:mcp.server.gh.1041179cbd.bearer_token}"}
        )

        assert resolved == {"Authorization": "Bearer ghp_secret"}
        assert missing_env == []
        assert missing_vault == []
        assert credential_sources == ["vault"]

    def test_connect_unresolved_headers_sets_auth_required(self):
        mgr = MCPManager()
        mgr.connect("gh", "http://gh/mcp", headers={"Authorization": "Bearer ${MISSING_TOKEN_XYZ}"})
        assert "gh" not in mgr._clients
        assert mgr._status["gh"]["status"] == "auth_required"
        assert "MISSING_TOKEN_XYZ" in mgr._status["gh"]["error"]

    @patch("src.tools.mcp_manager.vault_repository.exists", new_callable=AsyncMock)
    def test_connect_missing_vault_secret_sets_auth_required(self, mock_exists):
        mock_exists.return_value = False

        mgr = MCPManager()
        mgr.connect(
            "gh",
            "http://gh/mcp",
            headers={"Authorization": "Bearer ${vault:mcp.server.gh.1041179cbd.bearer_token}"},
        )

        assert "gh" not in mgr._clients
        assert mgr._status["gh"]["status"] == "auth_required"
        assert "mcp.server.gh.1041179cbd.bearer_token" in mgr._status["gh"]["error"]

    @patch("src.tools.mcp_manager.MCPClient")
    def test_connect_401_in_exception_group_sets_auth_required(self, MockMCPClient):
        inner = Exception("Client error '401 Unauthorized' for url 'https://api.githubcopilot.com/mcp/'")
        MockMCPClient.side_effect = ExceptionGroup("unhandled errors in a TaskGroup", [inner])

        mgr = MCPManager()
        mgr.connect("gh", "http://gh/mcp")
        assert "gh" not in mgr._clients
        assert mgr._status["gh"]["status"] == "auth_required"

    @patch("src.tools.mcp_manager.MCPClient")
    def test_connect_401_sets_auth_required(self, MockMCPClient):
        MockMCPClient.side_effect = Exception("HTTP 401 Unauthorized")

        mgr = MCPManager()
        mgr.connect("gh", "http://gh/mcp")
        assert "gh" not in mgr._clients
        assert mgr._status["gh"]["status"] == "auth_required"

    @patch("src.tools.mcp_manager.MCPClient")
    def test_connect_other_error_sets_error(self, MockMCPClient):
        MockMCPClient.side_effect = ConnectionError("Connection refused")

        mgr = MCPManager()
        mgr.connect("gh", "http://gh/mcp")
        assert "gh" not in mgr._clients
        assert mgr._status["gh"]["status"] == "error"
        assert "Connection refused" in mgr._status["gh"]["error"]

    @patch("src.tools.mcp_manager.MCPClient")
    def test_connect_success_sets_connected(self, MockMCPClient):
        mock_client = MagicMock()
        mock_client.get_tools.return_value = [MagicMock(name="tool1")]
        MockMCPClient.return_value = mock_client

        mgr = MCPManager()
        mgr.connect("svc", "http://svc/mcp")
        assert mgr._status["svc"]["status"] == "connected"
        assert mgr._status["svc"]["error"] is None

    @patch("src.tools.mcp_manager.MCPClient")
    def test_connect_success_logs_runtime_audit_event(self, MockMCPClient, async_db):
        mock_client = MagicMock()
        mock_client.get_tools.return_value = [MagicMock(name="tool1")]
        MockMCPClient.return_value = mock_client

        mgr = MCPManager()
        mgr.connect("svc", "http://svc/mcp")

        events = asyncio.run(audit_repository.list_events(limit=10))
        assert any(
            event["event_type"] == "integration_connected"
            and event["tool_name"] == "mcp_server:svc"
            and event["details"]["tool_count"] == 1
            for event in events
        )

    @pytest.mark.asyncio
    @patch("src.tools.mcp_manager.MCPClient")
    async def test_connect_success_logs_runtime_audit_event_inside_async_context(self, MockMCPClient, async_db):
        mock_client = MagicMock()
        mock_client.get_tools.return_value = [MagicMock(name="tool1")]
        MockMCPClient.return_value = mock_client

        mgr = MCPManager()
        mgr.connect("svc", "http://svc/mcp")
        await asyncio.sleep(0)

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "integration_connected"
            and event["tool_name"] == "mcp_server:svc"
            and event["details"]["tool_count"] == 1
            for event in events
        )

    def test_disconnect_sets_disconnected(self):
        mgr = MCPManager()
        mgr._status["svc"] = {"status": "connected", "error": None}
        mgr.disconnect("svc")
        assert mgr._status["svc"]["status"] == "disconnected"

    @patch("src.tools.mcp_manager.MCPClient")
    def test_disconnect_logs_runtime_audit_event(self, MockMCPClient, async_db):
        mock_client = MagicMock()
        mock_client.get_tools.return_value = [MagicMock(name="tool1")]
        MockMCPClient.return_value = mock_client

        mgr = MCPManager()
        mgr.connect("svc", "http://svc/mcp")
        mgr.disconnect("svc")

        events = asyncio.run(audit_repository.list_events(limit=10))
        assert any(
            event["event_type"] == "integration_disconnected"
            and event["tool_name"] == "mcp_server:svc"
            and event["details"]["had_client"] is True
            for event in events
        )

    @patch("src.tools.mcp_manager.vault_repository.exists", new_callable=AsyncMock)
    @patch("src.tools.mcp_manager.vault_repository.get", new_callable=AsyncMock)
    @patch("src.tools.mcp_manager.vault_repository.store", new_callable=AsyncMock)
    @patch("src.tools.mcp_manager.MCPClient")
    def test_set_token_stores_in_vault_and_reconnects(
        self,
        MockMCPClient,
        mock_store,
        mock_get,
        mock_exists,
        tmp_path,
    ):
        mock_client = MagicMock()
        mock_client.get_tools.return_value = [MagicMock(name="t1")]
        MockMCPClient.return_value = mock_client
        mock_get.return_value = "ghp_mytoken123"
        mock_exists.return_value = True

        mgr = MCPManager()
        mgr._config_path = str(tmp_path / "mcp.json")
        mgr._config["gh"] = {
            "url": "http://gh/mcp",
            "enabled": True,
            "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"},
        }

        result = mgr.set_token("gh", "ghp_mytoken123")
        assert result is True
        assert (
            mgr._config["gh"]["headers"]["Authorization"]
            == "Bearer ${vault:mcp.server.gh.1041179cbd.bearer_token}"
        )
        assert mgr._status["gh"]["status"] == "connected"
        mock_store.assert_awaited_once_with(
            "mcp.server.gh.1041179cbd.bearer_token",
            "ghp_mytoken123",
            description="MCP bearer token for server 'gh'",
        )

    def test_set_token_unknown_server_returns_false(self):
        mgr = MCPManager()
        assert mgr.set_token("nonexistent", "tok") is False

    def test_get_config_includes_status_fields(self):
        mgr = MCPManager()
        mgr._config["gh"] = {
            "url": "http://gh/mcp",
            "enabled": True,
            "headers": {"Authorization": "Bearer ${TOK}"},
            "auth_hint": "Create a PAT at github.com",
        }
        mgr._status["gh"] = {"status": "auth_required", "error": "Missing TOK"}

        configs = mgr.get_config()
        entry = configs[0]
        assert entry["status"] == "auth_required"
        assert entry["status_message"] == "Missing TOK"
        assert entry["auth_hint"] == "Create a PAT at github.com"
        assert entry["has_headers"] is True

    def test_connect_unresolved_headers_logs_runtime_audit_event(self, async_db):
        mgr = MCPManager()
        mgr.connect("gh", "http://gh/mcp", headers={"Authorization": "Bearer ${MISSING_TOKEN_XYZ}"})

        events = asyncio.run(audit_repository.list_events(limit=10))
        assert any(
            event["event_type"] == "integration_auth_required"
            and event["tool_name"] == "mcp_server:gh"
            and "MISSING_TOKEN_XYZ" in event["details"]["error"]
            for event in events
        )

    @patch("src.tools.mcp_manager.MCPClient")
    def test_connect_error_logs_runtime_audit_event(self, MockMCPClient, async_db):
        MockMCPClient.side_effect = ConnectionError("Connection refused")

        mgr = MCPManager()
        mgr.connect("gh", "http://gh/mcp")

        events = asyncio.run(audit_repository.list_events(limit=10))
        assert any(
            event["event_type"] == "integration_failed"
            and event["tool_name"] == "mcp_server:gh"
            and event["details"]["error"] == "Connection refused"
            for event in events
        )
