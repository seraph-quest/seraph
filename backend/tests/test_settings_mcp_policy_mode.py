"""Tests for settings API — GET/PUT MCP policy mode."""

import pytest
from unittest.mock import patch

from src.db.models import UserProfile
from src.observer.context import CurrentContext


@pytest.mark.asyncio
async def test_get_mcp_policy_mode_default(client):
    fresh = CurrentContext()
    with patch("src.api.settings.context_manager.get_context", return_value=fresh):
        resp = await client.get("/api/settings/mcp-policy-mode")
    assert resp.status_code == 200
    assert resp.json()["mode"] == "full"


@pytest.mark.asyncio
async def test_put_mcp_policy_mode_approval(client, async_db):
    async with async_db() as db:
        db.add(UserProfile(id="singleton"))

    resp = await client.put(
        "/api/settings/mcp-policy-mode",
        json={"mode": "approval"},
    )
    assert resp.status_code == 200
    assert resp.json()["mode"] == "approval"


@pytest.mark.asyncio
async def test_put_invalid_mcp_policy_mode_422(client):
    resp = await client.put(
        "/api/settings/mcp-policy-mode",
        json={"mode": "always"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_mcp_policy_mode_round_trip(client, async_db):
    async with async_db() as db:
        db.add(UserProfile(id="singleton"))

    await client.put(
        "/api/settings/mcp-policy-mode",
        json={"mode": "disabled"},
    )

    resp = await client.get("/api/settings/mcp-policy-mode")
    assert resp.json()["mode"] == "disabled"
