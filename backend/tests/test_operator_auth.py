from datetime import datetime, timedelta, timezone
import asyncio
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request
from sqlalchemy import select

from config.settings import settings
from src.auth.middleware import validate_request_boundary
from src.auth.middleware import OperatorAuthMiddleware
from src.api.ws import _OperatorSessionRevoked, _await_authorized, watch_operator_session, websocket_chat
from src.api.chat import _ensure_rest_authorized, _watch_rest_operator_session
from src.auth.service import AuthFailure, authenticate_token, bind_operator_principal, create_session, revoke_session
from src.auth.cancellation import RuntimeRevokedError, reset_revocation_guard, set_revocation_guard
from src.llm_runtime import _governed_openai_chat_completion
from src.api.auth import _reset_login_throttle_for_tests, _login_source
from src.api.auth import LoginRequest, login
from src.api.model_fabric_settings import _is_local_request


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


def test_websocket_boundary_requires_exact_host_and_origin(monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test,localhost,127.0.0.1,[::1]")
    assert validate_request_boundary(host="test", origin=ORIGIN, method="POST") is None
    assert validate_request_boundary(host="test:8004", origin=ORIGIN, method="POST") is None
    assert validate_request_boundary(host="127.0.0.1:8004", origin=ORIGIN, method="POST") is None
    assert validate_request_boundary(host="[::1]:8004", origin=ORIGIN, method="POST") is None
    assert validate_request_boundary(host="evil.example", origin=ORIGIN, method="POST") == "origin_forbidden"
    assert validate_request_boundary(host="test", origin=None, method="POST") == "mutation_origin_required"


@pytest.mark.parametrize(
    "host",
    [
        "user:pass@test",
        "@test",
        "test@",
        "test:",
        "test:not-a-port",
        "test:65536",
        "test:80:90",
        "[::1]:",
        "[::1]:not-a-port",
        "[::1",
        "::1",
        "test/path",
        "test?query=1",
        "test#fragment",
    ],
)
def test_websocket_boundary_rejects_malformed_host_authorities(host):
    assert validate_request_boundary(host=host, origin=ORIGIN, method="POST") == "origin_forbidden"


def test_authenticated_lan_operator_can_use_model_setup_without_being_loopback(monkeypatch):
    operator = SimpleNamespace(
        principal=SimpleNamespace(authenticated=True),
    )
    request = SimpleNamespace(
        client=SimpleNamespace(host="192.168.1.50"),
        state=SimpleNamespace(operator=operator),
    )
    assert _is_local_request(request) is True

    anonymous = SimpleNamespace(
        client=SimpleNamespace(host="192.168.1.50"),
        state=SimpleNamespace(),
    )
    assert _is_local_request(anonymous) is False


@pytest.mark.asyncio
async def test_revocation_watch_closes_socket_and_cancels_active_turn(monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_revocation_poll_seconds", 0.25)
    revoked = asyncio.Event()
    guard = Event()
    closed = {}

    class FakeWebSocket:
        async def close(self, *, code, reason):
            closed.update(code=code, reason=reason)

    async def _revoked(_token, *, touch=False):
        raise AuthFailure("session_revoked")

    monkeypatch.setattr("src.api.ws.authenticate_token", _revoked)
    await asyncio.wait_for(
        watch_operator_session(FakeWebSocket(), "token", revoked, guard),
        timeout=1,
    )
    assert revoked.is_set()
    assert guard.is_set()
    assert closed == {"code": 4401, "reason": "session_revoked"}

    with pytest.raises(_OperatorSessionRevoked):
        await _await_authorized(asyncio.sleep(10), revoked)


@pytest.mark.asyncio
async def test_revocation_watch_fails_closed_when_auth_store_is_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_revocation_poll_seconds", 0.25)
    revoked = asyncio.Event()
    guard = Event()
    closed = {}

    class FakeWebSocket:
        async def close(self, *, code, reason):
            closed.update(code=code, reason=reason)

    async def _unavailable(_token, *, touch=False):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("src.api.ws.authenticate_token", _unavailable)
    await asyncio.wait_for(
        watch_operator_session(FakeWebSocket(), "token", revoked, guard),
        timeout=1,
    )
    assert revoked.is_set()
    assert guard.is_set()
    assert closed == {"code": 1011, "reason": "auth_state_unavailable"}


@pytest.mark.asyncio
async def test_rest_revocation_watch_sets_guard_when_session_is_revoked(monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_revocation_poll_seconds", 0.25)
    guard = Event()
    stop = asyncio.Event()

    async def _revoked(_token, *, touch=False):
        raise AuthFailure("session_revoked")

    monkeypatch.setattr("src.api.chat.authenticate_token", _revoked)
    await asyncio.wait_for(
        _watch_rest_operator_session("token", guard, stop),
        timeout=1,
    )
    assert guard.is_set()


@pytest.mark.asyncio
async def test_rest_chat_discards_result_when_session_is_revoked(client, monkeypatch):
    _, token = await _login(client)
    operator = await authenticate_token(token, touch=False)
    monkeypatch.setattr(settings, "operator_auth_revocation_poll_seconds", 0.25)
    monkeypatch.setattr("src.api.chat.should_use_direct_local_chat", lambda *args, **kwargs: True)

    async def fake_route_error(**kwargs):
        return None

    monkeypatch.setattr("src.api.chat.direct_local_chat_route_error", fake_route_error)

    async def slow_chat(*args, **kwargs):
        await asyncio.sleep(0.3)
        await revoke_session(operator.session_id)
        await asyncio.sleep(0.4)
        return "must be discarded"

    monkeypatch.setattr("src.api.chat.run_direct_local_chat", slow_chat)
    response = await client.post(
        "/api/chat",
        json={"session_id": "rest-revocation", "message": "hello"},
        headers={"origin": ORIGIN},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "session_revoked"


@pytest.mark.asyncio
async def test_rest_authority_recheck_blocks_revoked_exception_side_effects():
    guard = Event()
    guard.set()
    with pytest.raises(HTTPException) as captured:
        await _ensure_rest_authorized(None, (guard, None, None, None))
    assert captured.value.status_code == 401
    assert captured.value.detail["code"] == "session_revoked"


@pytest.mark.asyncio
async def test_rest_authority_recheck_closes_revocation_after_authentication(monkeypatch):
    from types import SimpleNamespace

    guard = Event()
    request = SimpleNamespace(
        cookies={settings.operator_auth_cookie_name: "token"},
    )

    async def _authenticated(_token, *, touch=False):
        guard.set()
        return SimpleNamespace()

    monkeypatch.setattr("src.api.chat.authenticate_token", _authenticated)
    token = set_revocation_guard(guard)
    try:
        with pytest.raises(HTTPException) as captured:
            await _ensure_rest_authorized(request, (guard, None, None, token))
    finally:
        reset_revocation_guard(token)

    assert captured.value.status_code == 401
    assert captured.value.detail["code"] == "session_revoked"


def test_revocation_guard_blocks_new_governed_model_transport():
    guard = Event()
    guard.set()
    token = set_revocation_guard(guard)
    try:
        with pytest.raises(RuntimeRevokedError):
            _governed_openai_chat_completion(
                decision=None,
                context=None,
                body={},
                api_key=None,
            )
    finally:
        reset_revocation_guard(token)


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


@pytest.mark.asyncio
async def test_authenticated_operator_can_read_runtime_and_settings_without_provider_transport(client, monkeypatch):
    _, token = await _login(client)
    monkeypatch.setattr(
        "src.api.model_fabric_settings.httpx.AsyncClient",
        lambda *args, **kwargs: pytest.fail("metadata reads must not call provider transport"),
    )

    runtime = await client.get("/api/runtime/status")
    settings_response = await client.get("/api/settings/model-fabric")

    assert runtime.status_code == 200
    assert settings_response.status_code == 200
    assert runtime.json()["model_fabric"]["status"] in {"configuration_required", "ready", "degraded"}
    assert "api_key" not in runtime.text
    assert "api_key" not in settings_response.text
    assert client.cookies.get(settings.operator_auth_cookie_name) == token
