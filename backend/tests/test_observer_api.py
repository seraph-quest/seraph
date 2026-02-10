from unittest.mock import patch, AsyncMock, MagicMock

import pytest
import pytest_asyncio

from src.observer.context import CurrentContext
from src.observer.manager import ContextManager


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
    async def test_post_screen_context_null(self, client):
        mgr = ContextManager()
        mgr.update_screen_context("VS Code", "Editing")
        with patch("src.api.observer.context_manager", mgr):
            resp = await client.post("/api/observer/context", json={
                "active_window": None,
                "screen_context": None,
            })

        assert resp.status_code == 200
        assert mgr.get_context().active_window is None

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
