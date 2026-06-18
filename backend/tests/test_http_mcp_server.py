"""Tests for the HTTP Request MCP server."""

import sys
import os
import types
from unittest.mock import patch, MagicMock

import pytest

# Stub out fastmcp before importing server module — make @mcp.tool() a pass-through
class _FakeMCP:
    def __init__(self, *a, **kw):
        pass
    def tool(self):
        def decorator(fn):
            return fn
        return decorator
    def run(self, *a, **kw):
        pass

_fastmcp_stub = types.ModuleType("fastmcp")
_fastmcp_stub.FastMCP = _FakeMCP
sys.modules["fastmcp"] = _fastmcp_stub

# Add MCP server to path so we can import it
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../mcp-servers/http-request"))

from server import _is_internal_url, http_request


class TestIsInternalUrl:
    def test_blocks_localhost(self):
        assert _is_internal_url("http://localhost:8080/api") is True

    def test_blocks_127_0_0_1(self):
        assert _is_internal_url("http://127.0.0.1:3000/") is True

    def test_blocks_0_0_0_0(self):
        assert _is_internal_url("http://0.0.0.0/test") is True

    def test_blocks_private_ip_10(self):
        assert _is_internal_url("http://10.0.0.1/api") is True

    def test_blocks_private_ip_192(self):
        assert _is_internal_url("http://192.168.1.1/") is True

    def test_blocks_private_ip_172(self):
        assert _is_internal_url("http://172.16.0.1/") is True

    def test_blocks_dot_local(self):
        assert _is_internal_url("http://myhost.local/api") is True

    def test_blocks_dot_internal(self):
        assert _is_internal_url("http://service.internal/api") is True

    def test_allows_external_url(self):
        assert _is_internal_url("https://api.example.com/v1") is False

    def test_allows_external_ip(self):
        assert _is_internal_url("http://8.8.8.8/dns") is False

    def test_blocks_ipv6_loopback(self):
        assert _is_internal_url("http://[::1]:8080/") is True

    @patch("server.socket.getaddrinfo", return_value=[(None, None, None, None, ("10.0.0.9", 0))])
    def test_blocks_private_dns_resolution_preflight(self, _mock_getaddrinfo):
        assert _is_internal_url("https://public-looking.example", resolve_dns=True) is True
        assert _is_internal_url("https://public-looking.example") is False


class TestHttpRequest:
    def test_rejects_invalid_method(self):
        result = http_request("INVALID", "https://example.com")
        assert "error" in result
        assert "Invalid method" in result["error"]

    def test_blocks_internal_url(self):
        result = http_request("GET", "http://localhost:8080/secret")
        assert "error" in result
        assert "internal" in result["error"].lower()

    @patch("server.httpx.Client")
    def test_successful_get(self, mock_client_cls):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.text = '{"ok": true}'
        mock_response.url = "https://api.example.com/test"

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_response
        mock_client_cls.return_value = mock_client

        result = http_request("GET", "https://api.example.com/test")
        assert result["status"] == 200
        assert result["body"] == '{"ok": true}'
        assert "headers" in result

    @patch("server.httpx.Client")
    def test_blocks_redirect_to_internal_url(self, mock_client_cls):
        mock_response = MagicMock()
        mock_response.status_code = 302
        mock_response.headers = {"location": "http://127.0.0.1/secret"}
        mock_response.url = "https://api.example.com/start"

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_response
        mock_client_cls.return_value = mock_client

        result = http_request("GET", "https://api.example.com/start")

        assert "error" in result
        assert "redirect" in result["error"].lower()
        assert mock_client.request.call_count == 1

    @patch("server.httpx.Client")
    def test_timeout_handling(self, mock_client_cls):
        import httpx

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.side_effect = httpx.TimeoutException("timed out")
        mock_client_cls.return_value = mock_client

        result = http_request("GET", "https://slow.example.com", timeout=5)
        assert "error" in result
        assert "timed out" in result["error"].lower()

    def test_methods_case_insensitive(self):
        # lowercase method should be uppercased, then blocked by internal URL check
        result = http_request("get", "http://localhost/test")
        assert "internal" in result["error"].lower()
