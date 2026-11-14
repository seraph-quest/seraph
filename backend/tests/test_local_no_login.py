"""Opt-in local single-operator mode without login (#1161).

Refusal cases, the per-request loopback/Host/Origin boundary, the persisted
local operator root, and a cockpit/chat/WebSocket journey.  No provider is
contacted: chat uses the intercepted direct-local stubs.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from contextlib import ExitStack, asynccontextmanager, suppress
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from starlette.testclient import TestClient

import src.db.models  # noqa: F401
from config.settings import settings
from src.auth.middleware import authenticate_websocket, validate_request_boundary
from src.auth.service import (
    LOCAL_ROOT_TOKEN_MARKER,
    AuthFailure,
    auth_mode_status,
    authenticate_local_operator,
    authenticate_principal,
    authenticate_session,
    local_bind_error,
    local_no_login_active,
    reconcile_local_operator_root,
    validate_auth_configuration,
)
from src.db.models import AuditEvent, OperatorIdentity, OperatorSession
from src.security.trust_contract import AuthorityGrant, PrincipalType
from src.utils.background import drain_tracked_tasks


ORIGIN = "http://127.0.0.1:3001"
HOST = "127.0.0.1:8004"
BASE_URL = f"http://{HOST}"


def _enable_local_mode(monkeypatch) -> None:
    monkeypatch.setattr(settings, "deployment_environment", "development")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_local_no_login", True)
    monkeypatch.setattr(settings, "backend_bind_host", "127.0.0.1")
    # "test" is a configured but non-loopback name: local mode must still
    # refuse it at request time.
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "localhost,127.0.0.1,test")
    monkeypatch.setattr(
        settings, "operator_auth_allowed_origins", "http://127.0.0.1:3001,http://localhost:3001",
    )
    monkeypatch.setattr(settings, "vault_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr("sys.argv", ["uvicorn", "src.app:create_app", "--factory", "--host", "127.0.0.1"])
    for name in (
        "UVICORN_HOST", "UVICORN_WORKERS", "WEB_CONCURRENCY", "FORWARDED_ALLOW_IPS",
        "GUNICORN_CMD_ARGS", "SERVER_SOFTWARE",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def local_mode(monkeypatch):
    _enable_local_mode(monkeypatch)
    yield


@asynccontextmanager
async def _client(app, peer: str = "127.0.0.1"):
    transport = ASGITransport(app=app, client=(peer, 51000))
    async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
        yield client


# ── Startup refusal ─────────────────────────────────────────────────────


def test_valid_local_configuration_is_accepted(local_mode):
    validate_auth_configuration()
    assert local_no_login_active() is True
    assert auth_mode_status() == {"mode": "local_no_login", "local_no_login": True, "login_required": False}


@pytest.mark.parametrize("environment", ["production", "prod", "test", "staging", ""])
def test_refuses_outside_development(local_mode, monkeypatch, environment):
    monkeypatch.setattr(settings, "deployment_environment", environment)
    if environment in {"production", "prod"}:
        # Production already refuses a missing credential first.
        with pytest.raises(RuntimeError):
            validate_auth_configuration()
    else:
        with pytest.raises(RuntimeError, match="DEPLOYMENT_ENVIRONMENT=development"):
            validate_auth_configuration()
    assert local_no_login_active() is False


def test_refuses_test_bypass_combination(local_mode, monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", True)
    with pytest.raises(RuntimeError):
        validate_auth_configuration()


@pytest.mark.parametrize("field", ["operator_auth_secret", "operator_auth_secret_hash"])
def test_refuses_with_configured_credential(local_mode, monkeypatch, field):
    monkeypatch.setattr(settings, field, "pbkdf2_sha256$600000$x$y" if field.endswith("hash") else "pw")
    with pytest.raises(RuntimeError, match="cannot be combined"):
        validate_auth_configuration()
    # A configured credential always wins: the mode is never active.
    assert local_no_login_active() is False


@pytest.mark.parametrize("bind_host", ["", "0.0.0.0", "::", "192.168.1.20", "seraph.lan"])
def test_refuses_non_loopback_configured_bind(local_mode, monkeypatch, bind_host):
    monkeypatch.setattr(settings, "backend_bind_host", bind_host)
    with pytest.raises(RuntimeError, match="BACKEND_BIND_HOST|loopback bind"):
        validate_auth_configuration()


@pytest.mark.parametrize(
    "argv",
    [
        ["uvicorn", "src.app:create_app", "--host", "0.0.0.0"],
        ["uvicorn", "src.app:create_app", "--host=10.0.0.4"],
        ["uvicorn", "src.app:create_app", "--fd", "3"],
        ["uvicorn", "src.app:create_app", "--uds=/run/seraph.sock"],
    ],
)
def test_refuses_non_loopback_process_bind(local_mode, monkeypatch, argv):
    monkeypatch.setattr("sys.argv", argv)
    with pytest.raises(RuntimeError, match="loopback bind|--fd/--uds"):
        validate_auth_configuration()


def test_loopback_peer_forms():
    from src.auth.middleware import local_peer_error

    for peer in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        assert local_peer_error(peer) is None
    for peer in (None, "", "testclient", "10.0.0.2", "::ffff:10.0.0.2", "0.0.0.0"):
        assert local_peer_error(peer) == "local_peer_forbidden"


UVICORN_MAIN = "/venv/lib/python3.12/site-packages/uvicorn/__main__.py"


@pytest.mark.parametrize(
    ("argv", "environ", "message"),
    [
        # backend/main.py: uvicorn.run(host="0.0.0.0") with argv ["main.py"].
        (["main.py"], {}, "uvicorn command line"),
        (["python", "-c", "import uvicorn; uvicorn.run(app, host='0.0.0.0')"], {}, "uvicorn command line"),
        # A host only from UVICORN_HOST, or none at all, is not verifiable.
        (["uvicorn", "src.app:create_app", "--factory"], {"UVICORN_HOST": "0.0.0.0"}, "explicit loopback --host"),
        (["uvicorn", "src.app:create_app", "--factory"], {}, "explicit loopback --host"),
        (["uvicorn", "src.app:create_app", "--host", "127.0.0.1"], {"UVICORN_HOST": "0.0.0.0"}, "UVICORN_HOST"),
        # gunicorn -b binds are invisible to this process.
        (["/venv/bin/gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:8003"], {}, "gunicorn"),
        (["uvicorn", "src.app:create_app", "--host", "127.0.0.1"], {"GUNICORN_CMD_ARGS": "-b 0.0.0.0:8003"}, "gunicorn"),
        (["uvicorn", "src.app:create_app", "--host", "127.0.0.1"], {"SERVER_SOFTWARE": "gunicorn/22.0.0"}, "gunicorn"),
        # Multiple workers.
        (["uvicorn", "src.app:create_app", "--host", "127.0.0.1", "--workers", "2"], {}, "single backend worker"),
        (["uvicorn", "src.app:create_app", "--host", "127.0.0.1"], {"WEB_CONCURRENCY": "4"}, "single backend worker"),
        # Proxy trust beyond loopback lets remote clients claim a loopback peer.
        (["uvicorn", "src.app:create_app", "--host", "127.0.0.1", "--forwarded-allow-ips", "*"], {}, "FORWARDED_ALLOW_IPS"),
        (["uvicorn", "src.app:create_app", "--host", "127.0.0.1"], {"FORWARDED_ALLOW_IPS": "*"}, "FORWARDED_ALLOW_IPS"),
        (["uvicorn", "src.app:create_app", "--host", "127.0.0.1"], {"FORWARDED_ALLOW_IPS": "127.0.0.1,10.0.0.0/8"}, "FORWARDED_ALLOW_IPS"),
    ],
)
def test_refuses_unverifiable_or_widened_launch(local_mode, argv, environ, message):
    error = local_bind_error(argv, environ)
    assert error is not None and message in error


@pytest.mark.parametrize(
    ("argv", "environ"),
    [
        ([UVICORN_MAIN, "src.app:create_app", "--factory", "--host", "127.0.0.1", "--port", "8004"], {}),
        (["/venv/bin/uvicorn", "src.app:create_app", "--host=::1", "--workers=1"], {"UVICORN_HOST": "127.0.0.1"}),
        (["uvicorn", "src.app:create_app", "--host", "localhost"], {"FORWARDED_ALLOW_IPS": "127.0.0.1,::1"}),
    ],
)
def test_managed_style_uvicorn_launch_is_accepted(local_mode, argv, environ):
    assert local_bind_error(argv, environ) is None


def test_main_py_entrypoint_is_refused(local_mode, monkeypatch):
    monkeypatch.setattr("sys.argv", ["main.py"])
    with pytest.raises(RuntimeError, match="uvicorn command line"):
        validate_auth_configuration()


@pytest.mark.parametrize("bind_host", ["127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.2"])
def test_loopback_bind_forms_are_accepted(local_mode, monkeypatch, bind_host):
    monkeypatch.setattr(settings, "backend_bind_host", bind_host)
    assert local_bind_error(["uvicorn", "--host", "127.0.0.1"], {}) is None


def test_refuses_without_loopback_origin(local_mode, monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "https://seraph.example")
    with pytest.raises(RuntimeError, match="loopback origin"):
        validate_auth_configuration()


def test_refuses_without_loopback_host(local_mode, monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "seraph.lan")
    with pytest.raises(RuntimeError, match="loopback host"):
        validate_auth_configuration()


def test_default_off_keeps_unconfigured_fail_closed(monkeypatch):
    monkeypatch.setattr(settings, "deployment_environment", "development")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_local_no_login", False)
    validate_auth_configuration()
    assert local_no_login_active() is False
    assert auth_mode_status()["mode"] == "unconfigured"


def test_create_app_refuses_invalid_local_configuration(local_mode, monkeypatch):
    from src.app import create_app

    monkeypatch.setattr(settings, "backend_bind_host", "0.0.0.0")
    with pytest.raises(RuntimeError, match="loopback bind"):
        create_app()


# ── Per-request boundary ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_loopback_peer_is_refused_before_endpoint(local_mode, app, async_db):
    async with _client(app, peer="192.168.1.30") as client:
        response = await client.get("/api/auth/session")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "local_peer_forbidden"
    async with async_db() as db:
        assert (await db.execute(select(OperatorSession))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["evil.example", "test", "seraph.lan:8004", "127.0.0.1.nip.io"])
async def test_disallowed_or_non_loopback_host_is_refused(local_mode, app, async_db, host):
    async with _client(app) as client:
        response = await client.get("/api/auth/session", headers={"host": host})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "origin_forbidden"


@pytest.mark.asyncio
async def test_mutation_requires_allowed_loopback_origin(local_mode, app, async_db):
    async with _client(app) as client:
        missing = await client.post("/api/sessions", json={})
        foreign = await client.post("/api/sessions", json={}, headers={"origin": "https://evil.example"})
        rebinding = await client.post("/api/sessions", json={}, headers={"origin": "http://evil.example:3001"})
        foreign_get = await client.get("/api/auth/session", headers={"origin": "https://evil.example"})
    assert missing.status_code == 403
    assert missing.json()["detail"]["code"] == "mutation_origin_required"
    for response in (foreign, rebinding, foreign_get):
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "origin_forbidden"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"x-forwarded-for": "192.168.1.9"},
        # A LAN client spoofing a loopback hop before the proxy appends its own.
        {"x-forwarded-for": "127.0.0.1, 192.168.1.9"},
        {"x-real-ip": "10.0.0.7"},
        {"forwarded": 'for="[2001:db8::1]:4711";proto=http'},
        {"forwarded": "for=unknown"},
    ],
)
async def test_non_loopback_forwarded_hop_is_refused(local_mode, app, async_db, headers):
    async with _client(app) as client:
        response = await client.get("/api/auth/session", headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "local_peer_forbidden"


@pytest.mark.asyncio
async def test_vite_xfwd_chain_through_uvicorn_proxy_headers(local_mode, app, async_db):
    """The Vite proxy (xfwd) -> uvicorn ProxyHeadersMiddleware -> local check chain."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    proxied = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1")
    transport = ASGITransport(app=proxied, client=("127.0.0.1", 51000))
    async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
        lan = await client.get("/api/auth/session", headers={"x-forwarded-for": "192.168.1.9"})
        spoofed = await client.get("/api/auth/session", headers={"x-forwarded-for": "127.0.0.1, 192.168.1.9"})
        local = await client.get("/api/auth/session", headers={"x-forwarded-for": "127.0.0.1"})
    assert lan.status_code == 403
    assert spoofed.status_code == 403
    assert local.status_code == 200
    assert local.json()["local_no_login"] is True


@pytest.mark.asyncio
async def test_cross_site_fetch_metadata_is_refused(local_mode, app, async_db):
    async with _client(app) as client:
        cross = await client.get("/api/auth/session", headers={"sec-fetch-site": "cross-site"})
        same = await client.get("/api/auth/session", headers={"sec-fetch-site": "same-origin"})
    assert cross.status_code == 403
    assert cross.json()["detail"]["code"] == "cross_site_forbidden"
    assert same.status_code == 200


def test_configured_non_loopback_origin_is_ignored_in_local_mode(local_mode, monkeypatch):
    monkeypatch.setattr(
        settings, "operator_auth_allowed_origins", "http://127.0.0.1:3001,https://seraph.lan",
    )
    assert validate_request_boundary(host=HOST, origin="https://seraph.lan", method="POST") == "origin_forbidden"
    assert validate_request_boundary(host=HOST, origin=ORIGIN, method="POST") is None


class _FakeWebSocket:
    def __init__(self, *, peer="127.0.0.1", host=HOST, origin: str | None = ORIGIN, extra=None):
        self.client = type("Client", (), {"host": peer, "port": 5})()
        self.headers = {"host": host, **(extra or {})}
        if origin is not None:
            self.headers["origin"] = origin
        self.cookies = {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("socket", "code"),
    [
        (_FakeWebSocket(peer="10.1.2.3"), "local_peer_forbidden"),
        (_FakeWebSocket(host="evil.example"), "origin_forbidden"),
        (_FakeWebSocket(origin=None), "mutation_origin_required"),
        (_FakeWebSocket(origin="https://evil.example"), "origin_forbidden"),
        (_FakeWebSocket(extra={"x-forwarded-for": "192.168.1.9"}), "local_peer_forbidden"),
        (_FakeWebSocket(extra={"sec-fetch-site": "cross-site"}), "cross_site_forbidden"),
    ],
)
async def test_websocket_handshake_refusals(local_mode, async_db, socket, code):
    with pytest.raises(AuthFailure) as excinfo:
        await authenticate_websocket(socket)
    assert excinfo.value.code == code


@pytest.mark.asyncio
async def test_websocket_handshake_resolves_local_root(local_mode, async_db):
    operator = await authenticate_websocket(_FakeWebSocket())
    assert operator.principal.principal_id.startswith("operator:root:")
    assert operator.principal.principal_id != "operator:test-bypass"


# ── Local operator principal ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_local_root_is_a_real_operator_principal(local_mode, async_db):
    operator = await authenticate_local_operator()
    principal = operator.principal
    assert principal.principal_type is PrincipalType.OPERATOR
    assert principal.authenticated is True
    assert principal.principal_id.startswith("operator:root:")
    assert principal.principal_id != "operator:test-bypass"
    assert set(principal.grants) == {
        AuthorityGrant.INGRESS,
        AuthorityGrant.MODEL_INFERENCE,
        AuthorityGrant.CAPABILITY_EXECUTE,
        AuthorityGrant.ARTIFACT_TRANSFER,
    }
    assert operator.ownership_continuity == "stable"
    assert operator.operator_identity_id
    assert operator._token_hash is None
    # Durable owner checks and device-principal rechecks see the same root.
    assert (await authenticate_session(operator.session_id)).principal == principal
    assert (await authenticate_principal(principal.principal_id)).session_id == operator.session_id
    async with async_db() as db:
        rows = (await db.execute(select(OperatorSession))).scalars().all()
        assert len(rows) == 1
        assert rows[0].token_hash == LOCAL_ROOT_TOKEN_MARKER
        assert await db.get(OperatorIdentity, operator.operator_identity_id) is not None


@pytest.mark.asyncio
async def test_identity_is_stable_across_restarts(local_mode, async_db):
    first_start = await reconcile_local_operator_root()
    assert first_start["root_created"] is True
    first = await authenticate_local_operator()
    # A restart is a fresh startup reconciliation against the same database.
    second_start = await reconcile_local_operator_root()
    assert second_start["root_created"] is False
    second = await authenticate_local_operator()
    assert second.session_id == first.session_id
    assert second.principal.principal_id == first.principal.principal_id
    assert second.operator_identity_id == first.operator_identity_id
    assert second_start["principal_id"] == first.principal.principal_id


@pytest.mark.asyncio
async def test_root_creation_adopts_a_concurrent_process_winner(local_mode, async_db, monkeypatch):
    from sqlalchemy.exc import IntegrityError
    import src.auth.service as service

    winner = await authenticate_local_operator()

    async def lost_race():
        raise IntegrityError("INSERT operator_sessions", {}, Exception("UNIQUE constraint failed"))

    monkeypatch.setattr(service, "_create_local_root", lost_race)
    adopted, created = await service._ensure_local_root()
    assert created is False
    assert adopted.session_id == winner.session_id


@pytest.mark.asyncio
async def test_concurrent_first_requests_create_one_root(local_mode, async_db):
    operators = await asyncio.gather(*(authenticate_local_operator() for _ in range(5)))
    assert len({operator.session_id for operator in operators}) == 1
    async with async_db() as db:
        assert len((await db.execute(select(OperatorSession))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_root_has_no_authority_once_mode_is_off(local_mode, monkeypatch, async_db):
    operator = await authenticate_local_operator()
    # Switching to password authentication: the local root is inert even
    # before startup reconciliation retires it.
    monkeypatch.setattr(settings, "operator_auth_secret", "correct horse battery staple")
    assert local_no_login_active() is False
    with pytest.raises(AuthFailure, match="session_revoked"):
        await authenticate_session(operator.session_id)
    with pytest.raises(AuthFailure, match="session_revoked"):
        await authenticate_principal(operator.principal.principal_id)
    with pytest.raises(AuthFailure, match="auth_not_configured"):
        await authenticate_local_operator()

    retired = await reconcile_local_operator_root()
    assert retired["local_root_retired"] == operator.session_id
    async with async_db() as db:
        row = await db.get(OperatorSession, operator.session_id)
        assert row.revoked_at is not None
        assert row.token_hash != LOCAL_ROOT_TOKEN_MARKER

    # Re-enabling binds a fresh root to the same live identity, never the
    # revoked root, so earlier work stays discoverable via ownership recovery.
    monkeypatch.setattr(settings, "operator_auth_secret", "")
    replacement = await authenticate_local_operator()
    assert replacement.session_id != operator.session_id
    assert replacement.operator_identity_id == operator.operator_identity_id


@pytest.mark.asyncio
async def test_journal_and_receipt_keys_are_vault_derived(local_mode, monkeypatch):
    from src.conversation.identity import _attachment_receipt_key
    from src.extensions.capability_execution import CapabilityJournalError, _journal_mac_secret

    monkeypatch.setattr(settings, "capability_journal_secret", "")
    monkeypatch.setattr(settings, "capability_journal_secret_hash", "")
    first = _journal_mac_secret()
    assert first == _journal_mac_secret()
    assert len(first) == 64
    receipt_key = _attachment_receipt_key()
    assert len(receipt_key) == 32
    # Rotating the vault key changes the derived MAC root: it is keyed off
    # the vault, not off any password hash.
    monkeypatch.setattr(settings, "vault_encryption_key", Fernet.generate_key().decode())
    assert _journal_mac_secret() != first

    # Outside the mode the existing fail-closed behaviour is unchanged.
    monkeypatch.setattr(settings, "operator_auth_local_no_login", False)
    with pytest.raises(CapabilityJournalError, match="MAC key unavailable"):
        _journal_mac_secret()


# ── HTTP journey ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cockpit_session_and_credential_routes(local_mode, app, async_db):
    async with _client(app) as client:
        session = await client.get("/api/auth/session")
        again = await client.get("/api/auth/session")
        login = await client.post("/api/auth/login", json={"password": "x"}, headers={"origin": ORIGIN})
        refresh = await client.post("/api/auth/refresh", headers={"origin": ORIGIN})
        logout = await client.post("/api/auth/logout", headers={"origin": ORIGIN})
        enroll = await client.post("/api/auth/ownership/enroll", headers={"origin": ORIGIN})
        revoke = await client.post("/api/auth/ownership/revoke", headers={"origin": ORIGIN})
    assert session.status_code == 200
    payload = session.json()
    assert payload["authenticated"] is True
    assert payload["auth_mode"] == "local_no_login"
    assert payload["local_no_login"] is True
    assert payload["principal_id"].startswith("operator:root:")
    assert payload["ownership_continuity"] == "stable"
    assert payload["operator_identity_id"]
    assert again.json()["session_id"] == payload["session_id"]
    assert "set-cookie" not in session.headers
    for response in (login, refresh, logout, enroll, revoke):
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "local_no_login_active"
    # The root survives the refused revoke.
    assert (await authenticate_local_operator()).session_id == payload["session_id"]


@pytest.mark.asyncio
async def test_chat_turn_is_owned_by_local_root(local_mode, app, async_db, monkeypatch):
    monkeypatch.setattr("src.api.chat.should_use_direct_local_chat", lambda *args, **kwargs: True)
    monkeypatch.setattr("src.api.chat.direct_local_chat_route_error", AsyncMock(return_value=None))
    monkeypatch.setattr("src.api.chat.run_direct_local_chat", AsyncMock(return_value="Local reply."))
    async with _client(app) as client:
        response = await client.post("/api/chat", json={"message": "hello"}, headers={"origin": ORIGIN})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["response"] == "Local reply."
    operator = await authenticate_local_operator()
    from src.db.models import Session as ConversationSession

    async with async_db() as db:
        conversation = await db.get(ConversationSession, body["session_id"])
        assert conversation.owner_principal_id == operator.principal.principal_id


@pytest.mark.asyncio
async def test_password_mode_session_contract_is_unchanged(monkeypatch, client, async_db):
    monkeypatch.setattr(settings, "deployment_environment", "development")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "correct horse battery staple")
    # The flag is ignored while a credential is configured.
    monkeypatch.setattr(settings, "operator_auth_local_no_login", True)
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    anonymous = await client.get("/api/auth/session")
    assert anonymous.status_code == 401
    login = await client.post(
        "/api/auth/login",
        json={"password": "correct horse battery staple"},
        headers={"origin": "http://localhost:3001"},
    )
    assert login.status_code == 200
    client.cookies.set(settings.operator_auth_cookie_name, login.cookies.get(settings.operator_auth_cookie_name))
    session = await client.get("/api/auth/session")
    assert session.status_code == 200
    assert "auth_mode" not in session.json()
    assert "local_no_login" not in session.json()


# ── Lifespan, audit, runtime status and WebSocket journey ───────────────


def _make_local_sync_client(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="seraph-local-no-login-")
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{os.path.join(tmpdir, 'seraph-test.db')}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _get_session():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _init_db():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

    async def _close_db():
        try:
            await drain_tracked_tasks(timeout_seconds=5.0)
        finally:
            await engine.dispose()

    targets = [
        "src.db.engine.get_session",
        "src.auth.service.get_session",
        "src.agent.session.get_session",
        "src.workflows.job_runtime.get_session",
        "src.approval.repository.get_session",
        "src.audit.repository.get_session",
        "src.goals.repository.get_session",
        "src.profile.service.get_db",
        "src.vault.repository.get_session",
        "src.api.profile.get_db",
        "src.api.ws.get_session",
    ]
    patches = [patch(target, _get_session) for target in targets]
    patches += [
        # #1024: the provider worker owns init_db/close_db in the lifespan.
        patch("src.runtime_plugins.worker.init_db", _init_db),
        patch("src.runtime_plugins.worker.close_db", _close_db),
        patch("src.app.init_scheduler", return_value=None),
        patch("src.app.shutdown_scheduler"),
        patch("src.memory.flush.flush_session_memory", AsyncMock(return_value=None)),
    ]
    for item in patches:
        item.start()
    from src.app import create_app

    stack = ExitStack()
    stack.callback(lambda: shutil.rmtree(tmpdir, ignore_errors=True))
    for item in reversed(patches):
        stack.callback(item.stop)
    client = TestClient(
        create_app(),
        base_url=BASE_URL,
        client=("127.0.0.1", 52000),
        headers={"origin": ORIGIN},
    )
    stack.enter_context(client)
    return client, stack, factory


def test_lifespan_audits_mode_and_websocket_chat_journey(monkeypatch):
    _enable_local_mode(monkeypatch)

    async def _fake_stream(*args, **kwargs):
        yield "Local socket reply."

    client, stack, factory = _make_local_sync_client(monkeypatch)
    try:
        session = client.get("/api/auth/session")
        assert session.status_code == 200
        principal_id = session.json()["principal_id"]

        async def _audit_rows():
            async with factory() as db:
                return (
                    await db.execute(select(AuditEvent).where(AuditEvent.event_type == "operator_auth_mode"))
                ).scalars().all()

        rows = asyncio.run(_audit_rows())
        assert len(rows) == 1
        details = json.loads(rows[0].details_json)
        assert details["mode"] == "local_no_login"
        assert details["local_no_login"] is True
        assert details["principal_id"] == principal_id
        assert details["bind_host"] == "127.0.0.1"

        with (
            patch("src.api.ws.should_use_direct_local_chat", return_value=True),
            patch("src.api.ws.direct_local_chat_route_error", new=AsyncMock(return_value=None)),
            patch("src.api.ws.stream_direct_local_chat", _fake_stream),
            client.websocket_connect("/ws/chat", headers={"origin": ORIGIN, "host": HOST}) as ws,
        ):
            _ = ws.receive_text()
            ws.send_text(json.dumps({"type": "message", "message": "Hello"}))
            received = []
            while not received or received[-1]["type"] != "final":
                received.append(json.loads(ws.receive_text()))
        assert received[-1]["content"] == "Local socket reply."
        assert received[-1]["session_id"]

        async def _owner(session_id):
            from src.db.models import Session as ConversationSession

            async with factory() as db:
                return (await db.get(ConversationSession, session_id)).owner_principal_id

        assert asyncio.run(_owner(received[-1]["session_id"])) == principal_id

        with pytest.raises(Exception):
            with client.websocket_connect("/ws/chat", headers={"origin": "https://evil.example", "host": HOST}) as ws:
                ws.receive_text()
    finally:
        stack.close()
        with suppress(RuntimeError):
            asyncio.run(drain_tracked_tasks(timeout_seconds=5.0))


def test_runtime_status_reports_local_no_login(monkeypatch):
    _enable_local_mode(monkeypatch)
    client, stack, _factory = _make_local_sync_client(monkeypatch)
    try:
        monkeypatch.setattr(
            "src.api.model_fabric_settings.httpx.AsyncClient",
            lambda *args, **kwargs: pytest.fail("runtime status must not call provider transport"),
        )
        response = client.get("/api/runtime/status")
        assert response.status_code == 200, response.text
        assert response.json()["operator_auth"] == {
            "mode": "local_no_login",
            "local_no_login": True,
            "login_required": False,
        }
    finally:
        stack.close()
