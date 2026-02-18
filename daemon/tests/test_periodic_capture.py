"""Tests for periodic capture loop and fetch_capture_mode helper.

These tests depend on macOS-specific packages (PyObjC, Quartz) via seraph_daemon.
They are automatically skipped on non-macOS platforms.
"""

import asyncio
import platform
import sys
import os
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin",
    reason="Daemon tests require macOS (PyObjC, Quartz)",
)

httpx = pytest.importorskip("httpx")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from seraph_daemon import fetch_capture_mode, periodic_capture_loop


class TestFetchCaptureMode:
    @pytest.mark.asyncio
    async def test_returns_default_on_error(self):
        """Returns 'on_switch' when backend is unreachable."""
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        result = await fetch_capture_mode(mock_client, "http://localhost:9999")
        assert result == "on_switch"

    @pytest.mark.asyncio
    async def test_returns_default_on_non_200(self):
        """Returns 'on_switch' on non-200 response."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        result = await fetch_capture_mode(mock_client, "http://localhost:9999")
        assert result == "on_switch"

    @pytest.mark.asyncio
    async def test_parses_valid_response(self):
        """Parses mode from valid JSON response."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"mode": "balanced"}
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        result = await fetch_capture_mode(mock_client, "http://localhost:8004")
        assert result == "balanced"

    @pytest.mark.asyncio
    async def test_parses_detailed_mode(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"mode": "detailed"}
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        result = await fetch_capture_mode(mock_client, "http://localhost:8004")
        assert result == "detailed"


class TestPeriodicCaptureLoop:
    @pytest.mark.asyncio
    async def test_skips_when_on_switch(self):
        """No captures should happen when mode is on_switch."""
        posts = []

        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"mode": "on_switch"}
            return resp

        async def mock_post(url, **kwargs):
            posts.append(kwargs.get("json", {}))
            return MagicMock(status_code=200)

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("seraph_daemon.get_frontmost_app_name", return_value="VS Code"), \
             patch("seraph_daemon.get_window_title", return_value="main.py"), \
             patch("seraph_daemon.get_idle_seconds", return_value=0.0), \
             patch("httpx.AsyncClient", return_value=mock_client):

            task = asyncio.create_task(
                periodic_capture_loop(
                    "http://localhost:8004",
                    idle_timeout=300,
                    verbose=False,
                    ocr_provider=None,
                )
            )
            await asyncio.sleep(0.3)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # No posts should have been made
        assert len(posts) == 0

    @pytest.mark.asyncio
    async def test_skips_when_idle(self):
        """No captures when user is idle even in detailed mode."""
        posts = []

        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"mode": "detailed"}
            return resp

        async def mock_post(url, **kwargs):
            posts.append(kwargs.get("json", {}))
            return MagicMock(status_code=200)

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("seraph_daemon.get_frontmost_app_name", return_value="VS Code"), \
             patch("seraph_daemon.get_window_title", return_value="main.py"), \
             patch("seraph_daemon.get_idle_seconds", return_value=600.0), \
             patch("httpx.AsyncClient", return_value=mock_client):

            task = asyncio.create_task(
                periodic_capture_loop(
                    "http://localhost:8004",
                    idle_timeout=300,
                    verbose=False,
                    ocr_provider=None,
                )
            )
            await asyncio.sleep(0.3)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert len(posts) == 0
