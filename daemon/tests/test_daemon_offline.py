"""Daemon offline backend resilience — verify daemon handles unreachable backend gracefully.

These tests depend on macOS-specific packages (PyObjC, Quartz) via seraph_daemon.
They are automatically skipped on non-macOS platforms (e.g., GitHub CI on Linux).
"""

import asyncio
import json
import platform
import sys
import os
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

# Skip entire module on non-macOS (daemon uses PyObjC / Quartz)
pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin",
    reason="Daemon tests require macOS (PyObjC, Quartz)",
)

httpx = pytest.importorskip("httpx")

# We need to import the daemon module; adjust sys.path for the daemon directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from seraph_daemon import format_active_window, get_frontmost_app_name, poll_loop


class _NoopScreenRuntime:
    provider = type("Provider", (), {"name": "test-provider"})()
    blocklist: set[str] = set()

    async def refresh(self, *args, **kwargs):
        return None


class TestFormatActiveWindow:
    def test_with_both(self):
        assert format_active_window("VS Code", "main.py") == "VS Code \u2014 main.py"

    def test_app_only(self):
        assert format_active_window("Finder", None) == "Finder"

    def test_no_app(self):
        assert format_active_window(None, "title") is None

    def test_both_none(self):
        assert format_active_window(None, None) is None


class TestFrontmostAppLookup:
    def test_prefers_system_events_over_workspace_fallback(self):
        with patch("seraph_daemon._get_frontmost_app_name_from_system_events", return_value="Brave Browser"), \
             patch("seraph_daemon._get_frontmost_app_name_from_workspace", return_value="iTerm2"):
            assert get_frontmost_app_name() == "Brave Browser"

    def test_falls_back_to_workspace_when_system_events_unavailable(self):
        with patch("seraph_daemon._get_frontmost_app_name_from_system_events", return_value=None), \
             patch("seraph_daemon._get_frontmost_app_name_from_workspace", return_value="iTerm2"):
            assert get_frontmost_app_name() == "iTerm2"


class TestPollLoopOfflineBackend:
    @pytest.mark.asyncio
    async def test_frontmost_app_unavailable_updates_status_without_posting_context(self, tmp_path, monkeypatch):
        """On-switch mode should expose frontmost-app failures instead of silently appearing ready."""
        status_file = tmp_path / "daemon-status.json"
        monkeypatch.setenv("SERAPH_DAEMON_STATUS_FILE", str(status_file))
        status_file.write_text(
            json.dumps(
                {
                    "state": "running",
                    "screen_analysis": "active",
                    "capture_ready": True,
                    "active_window": "OldApp",
                    "last_capture_at": "2026-06-21T08:00:00+00:00",
                    "last_error": None,
                    "last_error_kind": None,
                }
            ),
            encoding="utf-8",
        )

        async def mock_get(*args, **kwargs):
            response = MagicMock()
            response.status_code = 200
            response.json.return_value = {"notification": None}
            return response

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.post = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("seraph_daemon.get_frontmost_app_name", return_value=None), \
             patch("seraph_daemon.get_window_title", return_value=None), \
             patch("seraph_daemon.get_idle_seconds", return_value=0.0), \
             patch("httpx.AsyncClient", return_value=mock_client):

            task = asyncio.create_task(
                poll_loop(
                    "http://localhost:9999",
                    interval=0.05,
                    idle_timeout=300,
                    verbose=False,
                    screen_runtime=_NoopScreenRuntime(),
                )
            )
            await asyncio.sleep(0.12)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        mock_client.post.assert_not_called()
        status = json.loads(Path(status_file).read_text(encoding="utf-8"))
        assert status["capture_ready"] is False
        assert status["active_window"] is None
        assert status["screen_analysis"] == "frontmost_unavailable"
        assert status["last_error_kind"] == "frontmost_app_unavailable"

    @pytest.mark.asyncio
    async def test_capture_exception_clears_stale_ready_status(self, tmp_path, monkeypatch):
        """Screenshot exceptions should not leave Settings showing an old ready capture state."""
        status_file = tmp_path / "daemon-status.json"
        monkeypatch.setenv("SERAPH_DAEMON_STATUS_FILE", str(status_file))
        status_file.write_text(
            json.dumps(
                {
                    "state": "running",
                    "screen_analysis": "active",
                    "capture_ready": True,
                    "active_window": "OldApp",
                    "last_capture_at": "2026-06-21T08:00:00+00:00",
                    "last_error": None,
                    "last_error_kind": None,
                }
            ),
            encoding="utf-8",
        )

        async def mock_get(*args, **kwargs):
            response = MagicMock()
            response.status_code = 200
            response.json.return_value = {"notification": None}
            return response

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("seraph_daemon.get_frontmost_app_name", return_value="TestApp"), \
             patch("seraph_daemon.get_window_title", return_value="TestWindow"), \
             patch("seraph_daemon.get_idle_seconds", return_value=0.0), \
             patch("blocklist.is_blocked", return_value=False), \
             patch("ocr.screenshot.capture_screen_png", side_effect=RuntimeError("boom")), \
             patch("httpx.AsyncClient", return_value=mock_client):

            task = asyncio.create_task(
                poll_loop(
                    "http://localhost:9999",
                    interval=0.05,
                    idle_timeout=300,
                    verbose=False,
                    screen_runtime=_NoopScreenRuntime(),
                )
            )
            await asyncio.sleep(0.12)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        status = json.loads(Path(status_file).read_text(encoding="utf-8"))
        assert status["capture_ready"] is False
        assert status["active_window"] == "TestApp — TestWindow"
        assert status["screen_analysis"] == "analysis_error"
        assert status["last_error_kind"] == "analysis_exception"

    @pytest.mark.asyncio
    async def test_empty_screenshot_clears_stale_ready_status_after_context_post(self, tmp_path, monkeypatch):
        """Screen Recording denial should stay visible after the context post succeeds."""
        status_file = tmp_path / "daemon-status.json"
        monkeypatch.setenv("SERAPH_DAEMON_STATUS_FILE", str(status_file))
        status_file.write_text(
            json.dumps(
                {
                    "state": "running",
                    "screen_analysis": "active",
                    "capture_ready": True,
                    "active_window": "OldApp",
                    "last_capture_at": "2026-06-21T08:00:00+00:00",
                    "last_error": None,
                    "last_error_kind": None,
                }
            ),
            encoding="utf-8",
        )

        async def mock_get(*args, **kwargs):
            response = MagicMock()
            response.status_code = 200
            response.json.return_value = {"notification": None}
            return response

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("seraph_daemon.get_frontmost_app_name", return_value="TestApp"), \
             patch("seraph_daemon.get_window_title", return_value="TestWindow"), \
             patch("seraph_daemon.get_idle_seconds", return_value=0.0), \
             patch("blocklist.is_blocked", return_value=False), \
             patch("ocr.screenshot.capture_screen_png", return_value=None), \
             patch("httpx.AsyncClient", return_value=mock_client):

            task = asyncio.create_task(
                poll_loop(
                    "http://localhost:9999",
                    interval=0.05,
                    idle_timeout=300,
                    verbose=False,
                    screen_runtime=_NoopScreenRuntime(),
                )
            )
            await asyncio.sleep(0.12)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        status = json.loads(Path(status_file).read_text(encoding="utf-8"))
        assert status["capture_ready"] is False
        assert status["active_window"] == "TestApp — TestWindow"
        assert status["screen_analysis"] == "capture_error"
        assert status["last_error_kind"] == "screen_capture_permission"

    @pytest.mark.asyncio
    async def test_continues_after_connect_error(self):
        """Poll loop should log a warning and continue when backend is unreachable."""
        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise httpx.ConnectError("Connection refused")

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("seraph_daemon.get_frontmost_app_name", return_value="TestApp"), \
             patch("seraph_daemon.get_window_title", return_value="TestWindow"), \
             patch("seraph_daemon.get_idle_seconds", return_value=0.0), \
             patch("httpx.AsyncClient", return_value=mock_client):

            # Run the poll loop for a short time
            task = asyncio.create_task(
                poll_loop("http://localhost:9999", interval=0.05, idle_timeout=300, verbose=False)
            )
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Should have attempted at least one post
        assert call_count >= 1

    @pytest.mark.asyncio
    async def test_continues_after_http_error(self):
        """Poll loop should handle HTTP errors (e.g., 500) without crashing."""
        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise httpx.HTTPStatusError(
                "Server error",
                request=httpx.Request("POST", "http://test"),
                response=httpx.Response(500),
            )

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("seraph_daemon.get_frontmost_app_name", return_value="App"), \
             patch("seraph_daemon.get_window_title", return_value="Win"), \
             patch("seraph_daemon.get_idle_seconds", return_value=0.0), \
             patch("httpx.AsyncClient", return_value=mock_client):

            task = asyncio.create_task(
                poll_loop("http://localhost:9999", interval=0.05, idle_timeout=300, verbose=False)
            )
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert call_count >= 1

    @pytest.mark.asyncio
    async def test_skips_when_idle(self):
        """Poll loop should skip posting when user is idle."""
        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("seraph_daemon.get_frontmost_app_name", return_value="App"), \
             patch("seraph_daemon.get_window_title", return_value="Win"), \
             patch("seraph_daemon.get_idle_seconds", return_value=600.0), \
             patch("httpx.AsyncClient", return_value=mock_client):

            task = asyncio.create_task(
                poll_loop("http://localhost:9999", interval=0.05, idle_timeout=300, verbose=False)
            )
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Should NOT have posted since user is idle
        assert call_count == 0

    @pytest.mark.asyncio
    async def test_skips_unchanged_window(self):
        """Poll loop should not re-post if the window hasn't changed."""
        posts = []

        async def mock_post(url, **kwargs):
            posts.append(kwargs.get("json", {}))
            return MagicMock(status_code=200)

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("seraph_daemon.get_frontmost_app_name", return_value="SameApp"), \
             patch("seraph_daemon.get_window_title", return_value="SameTitle"), \
             patch("seraph_daemon.get_idle_seconds", return_value=0.0), \
             patch("httpx.AsyncClient", return_value=mock_client):

            task = asyncio.create_task(
                poll_loop("http://localhost:9999", interval=0.05, idle_timeout=300, verbose=False)
            )
            await asyncio.sleep(0.3)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Only one POST since window never changes
        assert len(posts) == 1
