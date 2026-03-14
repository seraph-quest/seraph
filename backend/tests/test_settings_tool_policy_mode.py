"""Tests for settings API — GET/PUT tool policy mode."""

import pytest
from unittest.mock import patch

from src.db.models import UserProfile
from src.observer.context import CurrentContext


@pytest.mark.asyncio
async def test_get_tool_policy_mode_default(client):
    fresh = CurrentContext()
    with patch("src.api.settings.context_manager.get_context", return_value=fresh):
        resp = await client.get("/api/settings/tool-policy-mode")
    assert resp.status_code == 200
    assert resp.json()["mode"] == "full"


@pytest.mark.asyncio
async def test_put_tool_policy_mode_balanced(client, async_db):
    async with async_db() as db:
        db.add(UserProfile(id="singleton"))

    resp = await client.put(
        "/api/settings/tool-policy-mode",
        json={"mode": "balanced"},
    )
    assert resp.status_code == 200
    assert resp.json()["mode"] == "balanced"


@pytest.mark.asyncio
async def test_put_invalid_tool_policy_mode_422(client):
    resp = await client.put(
        "/api/settings/tool-policy-mode",
        json={"mode": "risky"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_tool_policy_mode_round_trip(client, async_db):
    async with async_db() as db:
        db.add(UserProfile(id="singleton"))

    await client.put(
        "/api/settings/tool-policy-mode",
        json={"mode": "safe"},
    )

    resp = await client.get("/api/settings/tool-policy-mode")
    assert resp.json()["mode"] == "safe"
