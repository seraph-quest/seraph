"""Guard: an ``AuthFailure`` that escapes a route is never a 500 (#1175).

The middleware maps refusals at the edge; a route or service that rechecks
the current root later (``ownership._current_root`` and friends) used to let
the exception reach Starlette as an unhandled 500 with a traceback.  The app
now maps it to its typed status with the reason code.
"""
from __future__ import annotations

import pytest
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient

from config.settings import settings
from src.app import create_app
from src.auth.middleware import auth_failure_status
from src.auth.service import AuthFailure


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("session_revoked", 401),
        ("session_expired", 401),
        ("authentication_required", 401),
        ("ownership_proof_required", 403),
        ("legacy_ownership_unproved", 403),
        ("local_peer_forbidden", 403),
        ("origin_forbidden", 403),
        ("recovery_record_unavailable", 404),
        ("recovery_preview_stale", 409),
        ("auth_not_configured", 503),
    ],
)
def test_auth_failure_status_is_typed(code, status):
    assert auth_failure_status(code) == status


@pytest.fixture
def raising_app(monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test,localhost,127.0.0.1")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    app = create_app()
    router = APIRouter()

    @router.get("/api/__test__/auth-failure/{code}")
    async def _raise(code: str):
        raise AuthFailure(code)

    @router.get("/api/calendar/__test__/auth-failure/{code}")
    async def _raise_calendar(code: str):
        raise AuthFailure(code)

    app.include_router(router)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "status"),
    [("session_revoked", 401), ("ownership_proof_required", 403), ("recovery_preview_stale", 409)],
)
async def test_escaped_auth_failure_is_never_a_500(raising_app, async_db, code, status):
    transport = ASGITransport(app=raising_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/__test__/auth-failure/{code}")
    assert response.status_code == status
    assert response.json() == {"detail": {"code": code}}
    assert "Traceback" not in response.text


@pytest.mark.asyncio
async def test_escaped_auth_failure_keeps_calendar_envelope(raising_app, async_db):
    transport = ASGITransport(app=raising_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/calendar/__test__/auth-failure/session_revoked")
    assert response.status_code == 401
    detail = response.json()["detail"]
    assert detail["code"] == "session_revoked"
    assert detail["recovery_action"] == "login"
