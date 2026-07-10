from datetime import datetime, timedelta, timezone
import asyncio

import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request
from sqlalchemy import select

from config.settings import settings
from src.auth.middleware import validate_request_boundary
from src.auth.middleware import OperatorAuthMiddleware
from src.api.ws import websocket_chat
from src.auth.service import AuthFailure, authenticate_token, bind_operator_principal, create_session
from src.api.auth import _reset_login_throttle_for_tests, _login_source
from src.api.auth import LoginRequest, login


def _request(peer: str = "127.0.0.1", headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request({"type": "http", "method": "POST", "path": "/api/auth/login", "headers": headers or [], "client": (peer, 1234), "server": ("test", 80), "scheme": "http"})


def _path_request(path: str, method: str = "GET") -> Request:
    return Request({"type": "http", "method": method, "path": path, "query_string": b"", "headers": [(b"host", b"test")], "client": ("127.0.0.1", 1234), "server": ("test", 80), "scheme": "http"})
from src.db.models import OperatorSession
from src.security.trust_contract import AuthorityGrant, PrincipalType


ORIGIN = "http://localhost:3001"


@pytest.fixture(autouse=True)
def configured_auth(monkeypatch):
    _reset_login_throttle_for_tests()
    monkeypatch.setattr(settings, "operator_auth_secret", "correct horse battery staple")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test,localhost,127.0.0.1")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", ORIGIN)
    monkeypatch.setattr(settings, "operator_auth_idle_seconds", 300)
    monkeypatch.setattr(settings, "operator_auth_absolute_seconds", 3600)
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)
    yield
    _reset_login_throttle_for_tests()


async def _login(client):
    response = await client.post(
        "/api/auth/login",
        json={"password": "correct horse battery staple"},
        headers={"origin": ORIGIN},
    )
    assert response.status_code == 200
    token = response.cookies.get(settings.operator_auth_cookie_name)
    assert token
    client.cookies.set(settings.operator_auth_cookie_name, token)
    return response, token


@pytest.mark.asyncio
async def test_login_cookie_session_refresh_rotation_and_logout(client):
    login, old_token = await _login(client)
    original_absolute_expiry = login.json()["absolute_expires_at"]
    cookie = login.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "Secure" not in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/" in cookie

    session = await client.get("/api/auth/session")
    assert session.status_code == 200
    assert session.json()["principal_id"] == "operator:single"

    refreshed = await client.post("/api/auth/refresh", headers={"origin": ORIGIN})
    assert refreshed.status_code == 200
    new_token = refreshed.cookies.get(settings.operator_auth_cookie_name)
    assert new_token and new_token != old_token
    assert refreshed.json()["principal_id"] == "operator:single"
    assert refreshed.json()["absolute_expires_at"] == original_absolute_expiry

    with pytest.raises(AuthFailure, match="session_revoked"):
        await authenticate_token(old_token)

    client.cookies.set(settings.operator_auth_cookie_name, new_token)
    logout = await client.post("/api/auth/logout", headers={"origin": ORIGIN})
    assert logout.status_code == 204
    with pytest.raises(AuthFailure, match="session_revoked"):
        await authenticate_token(new_token)


@pytest.mark.asyncio
async def test_api_denies_anonymous_foreign_host_origin_and_missing_mutation_origin(client):
    anonymous = await client.get("/api/auth/session")
    assert anonymous.status_code == 401
    assert anonymous.json()["detail"]["code"] == "authentication_required"

    foreign_host = await client.get("/api/auth/session", headers={"host": "evil.example"})
    assert foreign_host.status_code == 403
    assert foreign_host.json()["detail"]["code"] == "origin_forbidden"

    missing_origin = await client.post("/api/auth/login", json={"password": "x"})
    assert missing_origin.status_code == 403
    assert missing_origin.json()["detail"]["code"] == "mutation_origin_required"

    foreign_origin = await client.post(
        "/api/auth/login",
        json={"password": "x"},
        headers={"origin": "http://evil.example"},
    )
    assert foreign_origin.status_code == 403
    assert foreign_origin.json()["detail"]["code"] == "origin_forbidden"


@pytest.mark.asyncio
async def test_expired_session_is_rejected_and_raw_token_is_not_stored(client, async_db):
    _, token = await _login(client)
    async with async_db() as db:
        result = await db.execute(select(OperatorSession))
        record = result.scalar_one()
        assert record.token_hash != token
        assert len(record.token_hash) == 64
        record.idle_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.add(record)

    with pytest.raises(AuthFailure, match="session_expired"):
        await authenticate_token(token)


@pytest.mark.asyncio
async def test_server_mints_operator_principal_and_conversation_id_is_only_scope(client):
    _, token = await _login(client)
    operator = await authenticate_token(token)
    principal = bind_operator_principal(operator, "attacker-chosen-conversation")
    assert principal.principal_type is PrincipalType.OPERATOR
    assert principal.principal_id == "operator:single"
    assert principal.session_id == "attacker-chosen-conversation"
    assert AuthorityGrant.MODEL_INFERENCE in principal.grants
    assert AuthorityGrant.EXTERNAL_MUTATION not in principal.grants
    assert AuthorityGrant.CREDENTIAL_EGRESS not in principal.grants


@pytest.mark.asyncio
async def test_concurrent_refresh_has_exactly_one_winner(client):
    _, token = await _login(client)
    operator = await authenticate_token(token, touch=False)
    results = await asyncio.gather(
        create_session(replace_session_id=operator.session_id),
        create_session(replace_session_id=operator.session_id),
        return_exceptions=True,
    )
    assert sum(isinstance(result, tuple) for result in results) == 1
    loser = next(result for result in results if isinstance(result, Exception))
    assert isinstance(loser, AuthFailure)
    assert loser.code == "session_revoked"


def test_websocket_boundary_requires_exact_host_and_origin():
    assert validate_request_boundary(host="test", origin=ORIGIN, method="POST") is None
    assert validate_request_boundary(host="evil.example", origin=ORIGIN, method="POST") == "origin_forbidden"
    assert validate_request_boundary(host="test", origin=None, method="POST") == "mutation_origin_required"


def test_login_source_only_honors_forwarding_from_trusted_proxy(monkeypatch):
    forwarded = [(b"x-forwarded-for", b"203.0.113.7, 10.0.0.2")]
    monkeypatch.setattr(settings, "operator_auth_trusted_proxy_ips", "")
    assert _login_source(_request("10.0.0.2", forwarded)) == "10.0.0.2"
    monkeypatch.setattr(settings, "operator_auth_trusted_proxy_ips", "10.0.0.2")
    assert _login_source(_request("10.0.0.2", forwarded)) == "203.0.113.7"
    malformed = [(b"x-forwarded-for", b"not-an-ip")]
    assert _login_source(_request("10.0.0.2", malformed)) == "10.0.0.2"


@pytest.mark.asyncio
async def test_login_fails_stably_when_auth_is_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_secret", "")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    with pytest.raises(HTTPException) as captured:
        await login(LoginRequest(password="anything"), Response(), _request())
    assert captured.value.status_code == 503
    assert captured.value.detail["code"] == "auth_not_configured"


@pytest.mark.asyncio
async def test_login_has_dedicated_global_throttle(monkeypatch):
    async def _invalid(_password: str) -> bool:
        return False

    monkeypatch.setattr("src.api.auth.verify_secret", _invalid)
    for _ in range(5):
        with pytest.raises(HTTPException) as captured:
            await login(LoginRequest(password="wrong"), Response(), _request())
        assert captured.value.status_code == 401
    with pytest.raises(HTTPException) as captured:
        await login(LoginRequest(password="wrong"), Response(), _request())
    assert captured.value.status_code == 429
    assert captured.value.detail["code"] == "login_rate_limited"


@pytest.mark.asyncio
async def test_unconfigured_api_is_locked_before_endpoint_side_effect(monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_secret", "")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    called = False

    async def endpoint(_request):
        nonlocal called
        called = True
        raise AssertionError("endpoint must not run")

    middleware = OperatorAuthMiddleware(lambda *_args, **_kwargs: None)
    response = await middleware.dispatch(_path_request("/api/sessions"), endpoint)
    assert response.status_code == 503
    assert called is False


@pytest.mark.asyncio
async def test_unconfigured_websocket_closes_before_accept(monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_secret", "")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)

    class FakeWebSocket:
        accepted = False
        closed = None

        async def accept(self):
            self.accepted = True

        async def close(self, *, code, reason):
            self.closed = (code, reason)

    websocket = FakeWebSocket()
    await websocket_chat(websocket)
    assert websocket.accepted is False
    assert websocket.closed == (4401, "auth_not_configured")
