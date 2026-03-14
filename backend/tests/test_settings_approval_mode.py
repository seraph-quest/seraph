"""Tests for settings API — GET/PUT approval mode."""

import pytest
from unittest.mock import patch

from src.db.models import UserProfile
from src.observer.context import CurrentContext


@pytest.mark.asyncio
async def test_get_approval_mode_default(client):
    fresh = CurrentContext()
    with patch("src.api.settings.context_manager.get_context", return_value=fresh):
        resp = await client.get("/api/settings/approval-mode")
    assert resp.status_code == 200
    assert resp.json()["mode"] == "high_risk"


@pytest.mark.asyncio
async def test_put_approval_mode_off(client, async_db):
    async with async_db() as db:
        db.add(UserProfile(id="singleton"))

    resp = await client.put("/api/settings/approval-mode", json={"mode": "off"})
    assert resp.status_code == 200
    assert resp.json()["mode"] == "off"


@pytest.mark.asyncio
async def test_put_invalid_approval_mode_422(client):
    resp = await client.put("/api/settings/approval-mode", json={"mode": "always"})
    assert resp.status_code == 422
