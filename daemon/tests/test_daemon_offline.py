"""Daemon offline backend resilience — verify daemon handles unreachable backend gracefully.

These tests depend on macOS-specific packages (PyObjC, Quartz) via seraph_daemon.
They are automatically skipped on non-macOS platforms (e.g., GitHub CI on Linux).
"""

import asyncio
import platform
import sys
import os
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

from seraph_daemon import poll_loop, format_active_window


class TestFormatActiveWindow:
    def test_with_both(self):
        assert format_active_window("VS Code", "main.py") == "VS Code \u2014 main.py"

    def test_app_only(self):
        assert format_active_window("Finder", None) == "Finder"

    def test_no_app(self):
        assert format_active_window(None, "title") is None

    def test_both_none(self):
        assert format_active_window(None, None) is None


class TestPollLoopOfflineBackend:
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
