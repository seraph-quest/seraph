"""Tests for settings API — GET/PUT interruption mode."""

import pytest
import pytest_asyncio

from src.db.models import UserProfile


@pytest.mark.asyncio
async def test_get_interruption_mode(client):
    resp = await client.get("/api/settings/interruption-mode")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "balanced"
    assert data["attention_budget_remaining"] == 5
    assert data["user_state"] == "available"


@pytest.mark.asyncio
async def test_put_interruption_mode_focus(client, async_db):
    # Create a profile first
    async with async_db() as db:
        db.add(UserProfile(id="singleton"))

    resp = await client.put(
        "/api/settings/interruption-mode",
        json={"mode": "focus"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "focus"
    assert data["attention_budget_remaining"] == 0


@pytest.mark.asyncio
async def test_put_interruption_mode_active(client, async_db):
    async with async_db() as db:
        db.add(UserProfile(id="singleton"))

    resp = await client.put(
        "/api/settings/interruption-mode",
        json={"mode": "active"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "active"
    assert data["attention_budget_remaining"] == 15


@pytest.mark.asyncio
async def test_put_invalid_mode_422(client):
    resp = await client.put(
        "/api/settings/interruption-mode",
        json={"mode": "invalid_mode"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_budget_resets_on_mode_change(client, async_db):
    """Changing mode should reset budget to the new mode's default."""
    async with async_db() as db:
        db.add(UserProfile(id="singleton"))

    # Set to active (budget=15)
    resp = await client.put(
        "/api/settings/interruption-mode",
        json={"mode": "active"},
    )
    assert resp.json()["attention_budget_remaining"] == 15

    # Switch to balanced (budget=5)
    resp = await client.put(
        "/api/settings/interruption-mode",
        json={"mode": "balanced"},
    )
    assert resp.json()["attention_budget_remaining"] == 5


@pytest.mark.asyncio
async def test_get_reflects_put(client, async_db):
    """GET should reflect the mode set by PUT."""
    async with async_db() as db:
        db.add(UserProfile(id="singleton"))

    await client.put(
        "/api/settings/interruption-mode",
        json={"mode": "focus"},
    )

    resp = await client.get("/api/settings/interruption-mode")
    assert resp.json()["mode"] == "focus"
