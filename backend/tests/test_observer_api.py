import time
from unittest.mock import patch, AsyncMock, MagicMock

import pytest
import pytest_asyncio

from src.observer.context import CurrentContext
from src.observer.manager import ContextManager
from src.observer.screen_repository import ScreenObservationRepository


class TestObserverAPI:
    @pytest.mark.asyncio
    async def test_get_state(self, client):
        mgr = ContextManager()
        with patch("src.api.observer.context_manager", mgr):
            resp = await client.get("/api/observer/state")

        assert resp.status_code == 200
        data = resp.json()
        assert "time_of_day" in data
        assert "is_working_hours" in data
        assert "upcoming_events" in data

    @pytest.mark.asyncio
    async def test_post_screen_context(self, client):
        mgr = ContextManager()
        with patch("src.api.observer.context_manager", mgr):
            resp = await client.post("/api/observer/context", json={
                "active_window": "Terminal",
                "screen_context": "Running tests",
            })

        assert resp.status_code == 200
        assert mgr.get_context().active_window == "Terminal"
        assert mgr.get_context().screen_context == "Running tests"

    @pytest.mark.asyncio
    async def test_post_screen_context_null_preserves(self, client):
        """Posting None fields should not overwrite existing values (partial update)."""
        mgr = ContextManager()
        mgr.update_screen_context("VS Code", "Editing")
        with patch("src.api.observer.context_manager", mgr):
            resp = await client.post("/api/observer/context", json={
                "active_window": None,
                "screen_context": None,
            })

        assert resp.status_code == 200
        # None means "don't overwrite" — previous values preserved
        assert mgr.get_context().active_window == "VS Code"
        assert mgr.get_context().screen_context == "Editing"

    @pytest.mark.asyncio
    async def test_daemon_status_disconnected(self, client):
        """Daemon status returns disconnected when no POST received."""
        mgr = ContextManager()
        with patch("src.api.observer.context_manager", mgr):
            resp = await client.get("/api/observer/daemon-status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["connected"] is False
        assert data["last_post"] is None
        assert data["active_window"] is None
        assert data["has_screen_context"] is False

    @pytest.mark.asyncio
    async def test_daemon_status_connected(self, client):
        """Daemon status returns connected after a recent POST."""
        mgr = ContextManager()
        mgr.update_screen_context("VS Code — main.py", None)
        with patch("src.api.observer.context_manager", mgr):
            resp = await client.get("/api/observer/daemon-status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["connected"] is True
        assert data["last_post"] is not None
        assert data["active_window"] == "VS Code — main.py"

    @pytest.mark.asyncio
    async def test_daemon_status_stale(self, client):
        """Daemon status returns disconnected when last POST is too old."""
        mgr = ContextManager()
        mgr.update_screen_context("Terminal", None)
        # Simulate stale timestamp (60 seconds ago)
        mgr._context.last_daemon_post = time.time() - 60
        with patch("src.api.observer.context_manager", mgr):
            resp = await client.get("/api/observer/daemon-status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["connected"] is False

    @pytest.mark.asyncio
    async def test_post_refresh(self, client):
        mgr = ContextManager()
        mgr.refresh = AsyncMock(return_value=CurrentContext(time_of_day="evening"))
        with patch("src.api.observer.context_manager", mgr):
            resp = await client.post("/api/observer/refresh")

        assert resp.status_code == 200
        data = resp.json()
        assert data["time_of_day"] == "evening"
        mgr.refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_post_structured_observation(self, async_db, client):
        """Posting with observation field should persist to screen_observations table."""
        mgr = ContextManager()
        with patch("src.api.observer.context_manager", mgr):
            resp = await client.post("/api/observer/context", json={
                "active_window": "VS Code — main.py",
                "observation": {
                    "app": "VS Code",
                    "window_title": "main.py",
                    "activity": "coding",
                    "project": "seraph",
                    "summary": "Editing Python file",
                    "details": ["file: main.py"],
                    "blocked": False,
                },
                "switch_timestamp": 1700000000.0,
            })

        assert resp.status_code == 200
        assert mgr.get_context().active_window == "VS Code — main.py"

        # Verify observation was persisted
        from src.db.models import ScreenObservation
        from sqlmodel import select
        async with async_db() as db:
            result = await db.execute(select(ScreenObservation))
            obs = result.scalar_one()

        assert obs.app_name == "VS Code"
        assert obs.activity_type == "coding"
        assert obs.project == "seraph"

    @pytest.mark.asyncio
    async def test_post_blocked_observation(self, async_db, client):
        """Blocked observations should be persisted with blocked=True."""
        mgr = ContextManager()
        with patch("src.api.observer.context_manager", mgr):
            resp = await client.post("/api/observer/context", json={
                "active_window": "1Password — Vault",
                "observation": {
                    "app": "1Password",
                    "blocked": True,
                },
            })

        assert resp.status_code == 200

        from src.db.models import ScreenObservation
        from sqlmodel import select
        async with async_db() as db:
            result = await db.execute(select(ScreenObservation))
            obs = result.scalar_one()

        assert obs.app_name == "1Password"
        assert obs.blocked is True
        assert obs.activity_type == "other"

    @pytest.mark.asyncio
    async def test_legacy_compat_no_observation(self, client):
        """Legacy POST without observation field should still work."""
        mgr = ContextManager()
        with patch("src.api.observer.context_manager", mgr):
            resp = await client.post("/api/observer/context", json={
                "active_window": "Terminal",
                "screen_context": "Running tests",
            })

        assert resp.status_code == 200
        assert mgr.get_context().active_window == "Terminal"
        assert mgr.get_context().screen_context == "Running tests"

    @pytest.mark.asyncio
    async def test_get_activity_today(self, async_db, client):
        """Activity today endpoint should return daily summary."""
        mgr = ContextManager()
        with patch("src.api.observer.context_manager", mgr):
            resp = await client.get("/api/observer/activity/today")

        assert resp.status_code == 200
        data = resp.json()
        assert "total_observations" in data
