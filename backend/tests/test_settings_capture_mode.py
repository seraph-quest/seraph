"""Tests for settings API — GET/PUT capture mode."""

import pytest
from unittest.mock import patch

from src.db.models import UserProfile
from src.observer.context import CurrentContext


@pytest.mark.asyncio
async def test_get_capture_mode_default(client):
    """GET returns default on_switch."""
    fresh = CurrentContext()
    with patch("src.api.settings.context_manager.get_context", return_value=fresh):
        resp = await client.get("/api/settings/capture-mode")
    assert resp.status_code == 200
    assert resp.json()["mode"] == "on_switch"


@pytest.mark.asyncio
async def test_put_capture_mode_balanced(client, async_db):
    """PUT balanced persists to DB and updates context."""
    async with async_db() as db:
        db.add(UserProfile(id="singleton"))

    resp = await client.put(
        "/api/settings/capture-mode",
        json={"mode": "balanced"},
    )
    assert resp.status_code == 200
    assert resp.json()["mode"] == "balanced"


@pytest.mark.asyncio
async def test_put_capture_mode_detailed(client, async_db):
    async with async_db() as db:
        db.add(UserProfile(id="singleton"))

    resp = await client.put(
        "/api/settings/capture-mode",
        json={"mode": "detailed"},
    )
    assert resp.status_code == 200
    assert resp.json()["mode"] == "detailed"


@pytest.mark.asyncio
async def test_put_invalid_capture_mode_422(client):
    """PUT with invalid mode returns 422."""
    resp = await client.put(
        "/api/settings/capture-mode",
        json={"mode": "invalid"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_capture_mode_round_trip(client, async_db):
    """PUT then GET returns the new value."""
    async with async_db() as db:
        db.add(UserProfile(id="singleton"))

    await client.put(
        "/api/settings/capture-mode",
        json={"mode": "detailed"},
    )

    resp = await client.get("/api/settings/capture-mode")
    assert resp.json()["mode"] == "detailed"


@pytest.mark.asyncio
async def test_put_on_switch_resets(client, async_db):
    """Can switch back to on_switch."""
    async with async_db() as db:
        db.add(UserProfile(id="singleton"))

    await client.put("/api/settings/capture-mode", json={"mode": "balanced"})
    resp = await client.put("/api/settings/capture-mode", json={"mode": "on_switch"})
    assert resp.json()["mode"] == "on_switch"
