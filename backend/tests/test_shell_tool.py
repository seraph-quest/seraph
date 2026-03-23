"""Tests for sandbox-backed execute_code and shell_execute tools."""

import asyncio
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.audit.repository import audit_repository
from src.tools.execute_code_tool import execute_code
from src.tools.shell_tool import shell_execute


class TestShellExecute:
    @patch("src.tools.shell_tool.httpx.Client")
    def test_success(self, MockClient):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"stdout": "hello\n", "returncode": 0}
        mock_resp.raise_for_status = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=MagicMock(post=MagicMock(return_value=mock_resp)))
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        result = shell_execute('print("hello")')
        assert "hello" in result

    @patch("src.tools.shell_tool.httpx.Client")
    def test_execute_code_success(self, MockClient):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"stdout": "hello\n", "returncode": 0}
        mock_resp.raise_for_status = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=MagicMock(post=MagicMock(return_value=mock_resp)))
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        result = execute_code('print("hello")')
        assert "hello" in result

    @patch("src.tools.shell_tool.httpx.Client")
    def test_error_returncode(self, MockClient):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"stdout": "", "returncode": 1, "stderr": "NameError"}
        mock_resp.raise_for_status = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=MagicMock(post=MagicMock(return_value=mock_resp)))
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        result = shell_execute("bad_code")
        assert "Exit code 1" in result
        assert "NameError" in result

    @patch("src.tools.shell_tool.httpx.Client")
    def test_no_output(self, MockClient):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"stdout": "", "returncode": 0}
        mock_resp.raise_for_status = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=MagicMock(post=MagicMock(return_value=mock_resp)))
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        result = shell_execute("x = 1")
        assert result == "(no output)"

    def test_wrong_language(self):
        result = shell_execute("echo hello", language="bash")
        assert "Error" in result
        assert "python" in result.lower()

    def test_execute_code_wrong_language(self):
        result = execute_code("echo hello", language="bash")
        assert "Error" in result
        assert "python" in result.lower()

    def test_code_too_large(self):
        code = "x" * 200_000
        result = shell_execute(code)
        assert "Error" in result
        assert "too large" in result.lower()

    @patch("src.tools.shell_tool.httpx.Client")
    def test_timeout(self, MockClient):
        MockClient.return_value.__enter__ = MagicMock(
            return_value=MagicMock(post=MagicMock(side_effect=httpx.TimeoutException("timeout")))
        )
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        result = shell_execute("import time; time.sleep(999)")
        assert "timed out" in result.lower()

    @patch("src.tools.shell_tool.httpx.Client")
    def test_connection_error(self, MockClient):
        MockClient.return_value.__enter__ = MagicMock(
            return_value=MagicMock(post=MagicMock(side_effect=httpx.ConnectError("refused")))
        )
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        result = shell_execute("print(1)")
        assert "not available" in result.lower()

    @patch("src.tools.shell_tool.httpx.Client")
    def test_success_logs_runtime_audit(self, MockClient, async_db):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"stdout": "hello\n", "returncode": 0}
        mock_resp.raise_for_status = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=MagicMock(post=MagicMock(return_value=mock_resp)))
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        result = shell_execute('print("hello")')
        assert "hello" in result

        async def _fetch():
            events = await audit_repository.list_events(limit=5)
            return [e for e in events if e["event_type"] == "integration_succeeded"]

        events = asyncio.run(_fetch())
        assert events
        assert events[0]["tool_name"] == "sandbox:snekbox"
        assert events[0]["details"]["returncode"] == 0

    @patch("src.tools.shell_tool.httpx.Client")
    def test_execute_code_logs_runtime_audit(self, MockClient, async_db):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"stdout": "ok\n", "returncode": 0}
        mock_resp.raise_for_status = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=MagicMock(post=MagicMock(return_value=mock_resp)))
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        result = execute_code('print("ok")')
        assert "ok" in result

        async def _fetch():
            events = await audit_repository.list_events(limit=5)
            return [e for e in events if e["event_type"] == "integration_succeeded"]

        events = asyncio.run(_fetch())
        assert events
        assert events[0]["tool_name"] == "sandbox:snekbox"
        assert events[0]["details"]["returncode"] == 0

    @patch("src.tools.shell_tool.httpx.Client")
    def test_timeout_logs_runtime_audit(self, MockClient, async_db):
        MockClient.return_value.__enter__ = MagicMock(
            return_value=MagicMock(post=MagicMock(side_effect=httpx.TimeoutException("timeout")))
        )
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        result = shell_execute("import time; time.sleep(999)")
        assert "timed out" in result.lower()

        async def _fetch():
            events = await audit_repository.list_events(limit=5)
            return [e for e in events if e["event_type"] == "integration_timed_out"]

        events = asyncio.run(_fetch())
        assert events
        assert events[0]["tool_name"] == "sandbox:snekbox"
        assert events[0]["details"]["timeout_seconds"] == 35
