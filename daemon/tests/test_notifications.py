"""Tests for native notification polling in the macOS daemon."""

import asyncio
import os
import platform
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin",
    reason="Daemon tests require macOS (PyObjC, Quartz)",
)

httpx = pytest.importorskip("httpx")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from seraph_daemon import ack_notification, fetch_next_notification, poll_loop


class TestNotificationPolling:
    @pytest.mark.asyncio
    async def test_fetch_next_notification_returns_none_on_error(self):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))

        result = await fetch_next_notification(mock_client, "http://localhost:8004")

        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_next_notification_parses_payload(self):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "notification": {
                "id": "notif-1",
                "title": "Seraph alert",
                "body": "Hello",
            }
        }
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=response)

        result = await fetch_next_notification(mock_client, "http://localhost:8004")

        assert result == {
            "id": "notif-1",
            "title": "Seraph alert",
            "body": "Hello",
        }

    @pytest.mark.asyncio
    async def test_ack_notification_returns_true_on_success(self):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"acked": True}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=response)

        acked = await ack_notification(mock_client, "http://localhost:8004", "notif-1")

        assert acked is True

    @pytest.mark.asyncio
    async def test_poll_loop_displays_and_acks_notification(self):
        notification_payload = {
            "id": "notif-1",
            "title": "Seraph alert",
            "body": "Native path online",
        }
        context_posts: list[dict] = []
        ack_posts: list[str] = []
        notifications = [notification_payload, None, None]

        async def mock_get(url, **kwargs):
            response = MagicMock()
            response.status_code = 200
            response.json.return_value = {
                "notification": notifications.pop(0) if notifications else None,
            }
            return response

        async def mock_post(url, **kwargs):
            if "/notifications/" in url:
                ack_posts.append(url)
                response = MagicMock()
                response.status_code = 200
                response.json.return_value = {"acked": True}
                return response
            context_posts.append(kwargs.get("json", {}))
            return MagicMock(status_code=200)

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("seraph_daemon.get_frontmost_app_name", return_value="VS Code"), \
                patch("seraph_daemon.get_window_title", return_value="main.py"), \
                patch("seraph_daemon.get_idle_seconds", return_value=0.0), \
                patch("seraph_daemon.show_notification", return_value=True) as mock_show, \
                patch("httpx.AsyncClient", return_value=mock_client):
            task = asyncio.create_task(
                poll_loop("http://localhost:8004", interval=0.05, idle_timeout=300, verbose=False)
            )
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        mock_show.assert_called_once_with("Seraph alert", "Native path online")
        assert len(ack_posts) == 1
        assert len(context_posts) >= 1

    @pytest.mark.asyncio
    async def test_poll_loop_does_not_redisplay_notification_when_ack_fails(self):
        notification_payload = {
            "id": "notif-1",
            "title": "Seraph alert",
            "body": "Native path online",
        }
        notifications = [notification_payload, notification_payload, notification_payload, None]
        ack_posts: list[str] = []

        async def mock_get(url, **kwargs):
            response = MagicMock()
            response.status_code = 200
            response.json.return_value = {
                "notification": notifications.pop(0) if notifications else None,
            }
            return response

        async def mock_post(url, **kwargs):
            if "/notifications/" in url:
                ack_posts.append(url)
                response = MagicMock()
                response.status_code = 503
                response.json.return_value = {"acked": False}
                return response
            return MagicMock(status_code=200)

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("seraph_daemon.get_frontmost_app_name", return_value="VS Code"), \
                patch("seraph_daemon.get_window_title", return_value="main.py"), \
                patch("seraph_daemon.get_idle_seconds", return_value=0.0), \
                patch("seraph_daemon.show_notification", return_value=True) as mock_show, \
                patch("httpx.AsyncClient", return_value=mock_client):
            task = asyncio.create_task(
                poll_loop("http://localhost:8004", interval=0.05, idle_timeout=300, verbose=False)
            )
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        mock_show.assert_called_once_with("Seraph alert", "Native path online")
        assert len(ack_posts) >= 2
