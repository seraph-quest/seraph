from datetime import datetime, timedelta, timezone
import asyncio
from contextlib import asynccontextmanager
import re
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel

from config.settings import settings
from src.auth.middleware import validate_request_boundary
from src.auth.middleware import OperatorAuthMiddleware
from src.api.ws import _OperatorSessionRevoked, _await_authorized, watch_operator_session, websocket_chat
from src.api.chat import _ensure_rest_authorized, _watch_rest_operator_session
from src.auth.service import (
    AuthFailure,
    _token_hash,
    authenticate_session,
    authenticate_websocket_session,
    authenticate_token,
    bind_operator_principal,
    create_session,
    _find_token_record,
    _ownership_metadata,
    revoke_session,
)
from src.auth.cancellation import RuntimeRevokedError, reset_revocation_guard, set_revocation_guard
from src.llm_runtime import _governed_openai_chat_completion
from src.api.auth import _reset_login_throttle_for_tests, _login_source
from src.api.auth import LoginRequest, login, refresh
from src.api.model_fabric_settings import _is_local_request


def _request(peer: str = "127.0.0.1", headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request({"type": "http", "method": "POST", "path": "/api/auth/login", "headers": headers or [], "client": (peer, 1234), "server": ("test", 80), "scheme": "http"})


def _path_request(path: str, method: str = "GET") -> Request:
    return Request({"type": "http", "method": method, "path": path, "query_string": b"", "headers": [(b"host", b"test")], "client": ("127.0.0.1", 1234), "server": ("test", 80), "scheme": "http"})
from src.db.models import OperatorSession
from src.db.engine import _ensure_operator_session_columns
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
async def test_login_cookie_session_refresh_rotation_and_logout(client, async_db):
    login, old_token = await _login(client)
    original_absolute_expiry = login.json()["absolute_expires_at"]
    cookie = login.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "Secure" not in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/" in cookie

    session = await client.get("/api/auth/session")
    assert session.status_code == 200
    assert session.json()["principal_id"] == login.json()["principal_id"]
    assert session.json()["principal_id"].startswith("operator:root:")
    assert session.json()["session_id"]

    refreshed = await client.post("/api/auth/refresh", headers={"origin": ORIGIN})
    assert refreshed.status_code == 200
    new_token = refreshed.cookies.get(settings.operator_auth_cookie_name)
    assert new_token and new_token != old_token
    assert refreshed.json()["principal_id"] == login.json()["principal_id"]
    assert refreshed.json()["session_id"] == session.json()["session_id"]
    assert refreshed.json()["absolute_expires_at"] == original_absolute_expiry
    assert refreshed.json()["ownership_continuity"] == "stable"
    assert refreshed.json()["ownership_recovery_action"] is None
    assert set(refreshed.json()) == {
        "authenticated",
        "principal_id",
        "session_id",
        "idle_expires_at",
        "absolute_expires_at",
        "ownership_continuity",
        "ownership_recovery_action",
        "operator_identity_id",
    }

    with pytest.raises(AuthFailure, match="session_revoked"):
        await authenticate_token(old_token)

    client.cookies.set(settings.operator_auth_cookie_name, new_token)
    second_refresh = await client.post("/api/auth/refresh", headers={"origin": ORIGIN})
    assert second_refresh.status_code == 200
    newest_token = second_refresh.cookies.get(settings.operator_auth_cookie_name)
    assert newest_token and newest_token not in {old_token, new_token}
    assert second_refresh.json()["session_id"] == session.json()["session_id"]
    assert second_refresh.json()["absolute_expires_at"] == original_absolute_expiry
    with pytest.raises(AuthFailure, match="session_revoked"):
        await authenticate_token(new_token)

    client.cookies.set(settings.operator_auth_cookie_name, newest_token)
    logout = await client.post("/api/auth/logout", headers={"origin": ORIGIN})
    assert logout.status_code == 204
    with pytest.raises(AuthFailure, match="session_revoked"):
        await authenticate_token(new_token)

    async with async_db() as db:
        rows = (await db.execute(select(OperatorSession))).scalars().all()
        assert sum(not row.is_bearer_tombstone and row.revoked_at is None for row in rows) == 0
        assert sum(row.is_bearer_tombstone and row.revoked_at is not None for row in rows) == 2


@pytest.mark.asyncio
async def test_refresh_cookie_max_age_stays_within_preserved_absolute_expiry(client, async_db):
    _, old_token = await _login(client)
    absolute_expiry = datetime.now(timezone.utc) + timedelta(seconds=8)
    async with async_db() as db:
        record = (await db.execute(select(OperatorSession))).scalar_one()
        record.idle_expires_at = absolute_expiry
        record.absolute_expires_at = absolute_expiry
        db.add(record)

    client.cookies.set(settings.operator_auth_cookie_name, old_token)
    refreshed = await client.post("/api/auth/refresh", headers={"origin": ORIGIN})
    assert refreshed.status_code == 200, refreshed.text
    max_age_match = re.search(r"(?:^|; )Max-Age=(\d+)(?:;|$)", refreshed.headers["set-cookie"])
    assert max_age_match, refreshed.headers["set-cookie"]
    max_age = int(max_age_match.group(1))
    absolute = datetime.fromisoformat(refreshed.json()["absolute_expires_at"].replace("Z", "+00:00"))
    remaining = int((absolute - datetime.now(timezone.utc)).total_seconds())
    assert 0 <= max_age <= max(remaining, 0)
    assert max_age < settings.operator_auth_absolute_seconds


@pytest.mark.asyncio
async def test_refresh_tombstone_insert_failure_rolls_back_active_credential(
    client,
    async_db,
    monkeypatch,
):
    _, old_token = await _login(client)
    operator = await authenticate_token(old_token, touch=False)
    flush_calls = 0

    @asynccontextmanager
    async def failing_get_session():
        nonlocal flush_calls
        async with async_db() as db:
            original_flush = db.flush

            async def flush(*args, **kwargs):
                nonlocal flush_calls
                flush_calls += 1
                if flush_calls == 2:
                    raise IntegrityError(
                        "forced tombstone insert failure",
                        {},
                        RuntimeError("duplicate tombstone"),
                    )
                return await original_flush(*args, **kwargs)

            monkeypatch.setattr(db, "flush", flush)
            yield db

    monkeypatch.setattr("src.auth.service.get_session", failing_get_session)
    with pytest.raises(IntegrityError):
        await create_session(
            replace_session_id=operator.session_id,
            expected_token_hash=operator._token_hash,
        )
    assert flush_calls == 2

    restored = await authenticate_token(old_token, touch=False)
    assert restored.session_id == operator.session_id
    async with async_db() as db:
        active = await db.get(OperatorSession, operator.session_id)
        tombstones = (
            await db.execute(select(OperatorSession).where(OperatorSession.is_bearer_tombstone.is_(True)))
        ).scalars().all()
        assert active is not None
        assert active.token_hash == _token_hash(old_token)
        assert active.revoked_at is None
        assert tombstones == []


@pytest.mark.asyncio
async def test_unrelated_malformed_inventory_does_not_contaminate_healthy_root(client, async_db):
    _, token = await _login(client)
    before = await authenticate_token(token, touch=False)
    now = datetime.now(timezone.utc)
    async with async_db() as db:
        for index in range(64):
            db.add(
                OperatorSession(
                    id=f"unrelated-session-{index}",
                    token_hash=f"unrelated-token-hash-{index}",
                    created_at=now,
                    last_seen_at=now,
                    idle_expires_at=now + timedelta(hours=1),
                    absolute_expires_at=now + timedelta(hours=1),
                    is_bearer_tombstone=False,
                )
            )
        db.add(
            OperatorSession(
                id="unrelated-malformed-cycle",
                token_hash="unrelated-malformed-token-hash",
                created_at=now,
                last_seen_at=now,
                idle_expires_at=now + timedelta(hours=1),
                absolute_expires_at=now + timedelta(hours=1),
                replaced_by_id="unrelated-malformed-cycle",
                is_bearer_tombstone=False,
            )
        )
    after = await authenticate_token(token, touch=False)
    assert after.session_id == before.session_id
    assert after.ownership_continuity == "stable"
    assert after.ownership_recovery_action is None


@pytest.mark.asyncio
async def test_relevant_predecessor_inventory_is_bounded_and_legacy(client, async_db):
    _, token = await _login(client)
    root = await authenticate_token(token, touch=False)
    now = datetime.now(timezone.utc)
    async with async_db() as db:
        for index in range(3):
            db.add(
                OperatorSession(
                    id=f"relevant-legacy-parent-{index}",
                    token_hash=f"relevant-legacy-token-hash-{index}",
                    created_at=now,
                    last_seen_at=now,
                    idle_expires_at=now + timedelta(hours=1),
                    absolute_expires_at=now + timedelta(hours=1),
                    revoked_at=now,
                    replaced_by_id=root.session_id,
                    is_bearer_tombstone=False,
                )
            )
    current = await authenticate_token(token, touch=False)
    assert current.session_id == root.session_id
    assert current.ownership_continuity == "legacy_rebind_required"
    assert current.ownership_recovery_action == "review_and_recreate_work_in_current_scope"


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_id", ["", " "])
async def test_malformed_active_replacement_link_cannot_authenticate(
    client,
    async_db,
    replacement_id,
):
    _, token = await _login(client)
    async with async_db() as db:
        record = (await db.execute(select(OperatorSession))).scalar_one()
        record.replaced_by_id = replacement_id
        db.add(record)
    with pytest.raises(AuthFailure, match="session_revoked"):
        await authenticate_token(token, touch=False)


@pytest.mark.asyncio
async def test_null_marker_and_impossible_tombstone_fail_closed(client, async_db):
    _, token = await _login(client)
    token_hash = _token_hash(token)
    now = datetime.now(timezone.utc)
    async with async_db() as db:
        record = (await db.execute(select(OperatorSession))).scalar_one()
        record.is_bearer_tombstone = None
        with db.no_autoflush:
            found, error = await _find_token_record(db, token_hash, now)
            assert found is None
            assert error == "session_revoked"
            continuity, recovery_action = await _ownership_metadata(db, record)
            assert continuity == "legacy_rebind_required"
            assert recovery_action == "review_and_recreate_work_in_current_scope"
        await db.rollback()

    _, impossible_token = await _login(client)
    impossible_root = await authenticate_token(impossible_token, touch=False)
    async with async_db() as db:
        db.add(
            OperatorSession(
                id="impossible-tombstone-predecessor",
                token_hash=_token_hash("impossible-tombstone-token"),
                created_at=now,
                last_seen_at=now,
                idle_expires_at=now + timedelta(hours=1),
                absolute_expires_at=now + timedelta(hours=1),
                replaced_by_id=impossible_root.session_id,
                is_bearer_tombstone=True,
                revoked_at=None,
            )
        )
    classified = await authenticate_token(impossible_token, touch=False)
    assert classified.ownership_continuity == "legacy_rebind_required"
    with pytest.raises(AuthFailure, match="session_revoked"):
        await authenticate_token("impossible-tombstone-token", touch=False)


@pytest.mark.asyncio
async def test_strict_session_identity_stays_stable_and_websocket_continuity_is_separate(client):
    _, old_token = await _login(client)
    old_operator = await authenticate_token(old_token, touch=False)

    refreshed = await client.post("/api/auth/refresh", headers={"origin": ORIGIN})
    assert refreshed.status_code == 200
    new_token = refreshed.cookies.get(settings.operator_auth_cookie_name)
    assert new_token
    new_operator = await authenticate_token(new_token, touch=False)

    assert old_operator.session_id == new_operator.session_id
    strict = await authenticate_session(old_operator.session_id, touch=False)
    assert strict.session_id == old_operator.session_id
    assert strict.ownership_continuity == "stable"
    followed = await authenticate_websocket_session(old_operator.session_id, touch=False)
    assert followed.session_id == old_operator.session_id
    with pytest.raises(AuthFailure, match="session_revoked"):
        await authenticate_token(old_token, touch=False)


@pytest.mark.asyncio
async def test_legacy_non_tombstone_ancestor_is_visible_but_never_strict_owner(
    client,
    async_db,
):
    _, token = await _login(client)
    operator = await authenticate_token(token, touch=False)
    now = datetime.now(timezone.utc)
    async with async_db() as db:
        db.add(
            OperatorSession(
                id="legacy-owner-row",
                token_hash=_token_hash("legacy-bearer"),
                created_at=now,
                last_seen_at=now,
                idle_expires_at=now + timedelta(minutes=5),
                absolute_expires_at=now + timedelta(hours=1),
                revoked_at=now,
                replaced_by_id=operator.session_id,
                is_bearer_tombstone=False,
            )
        )

    current = await authenticate_token(token, touch=False)
    assert current.session_id == operator.session_id
    assert current.ownership_continuity == "legacy_rebind_required"
    assert current.ownership_recovery_action == "review_and_recreate_work_in_current_scope"
    with pytest.raises(AuthFailure, match="session_revoked"):
        await authenticate_session("legacy-owner-row", touch=False)
    websocket_operator = await authenticate_websocket_session("legacy-owner-row", touch=False)
    assert websocket_operator.session_id == operator.session_id
    assert websocket_operator.ownership_continuity == "legacy_rebind_required"


@pytest.mark.asyncio
async def test_refresh_requires_private_expected_credential_and_tombstone_is_not_owner(
    client,
    async_db,
):
    _, token = await _login(client)
    operator = await authenticate_token(token, touch=False)
    assert operator._token_hash == _token_hash(token)
    assert operator._token_hash not in repr(operator)
    with pytest.raises(AuthFailure, match="authentication_required"):
        await create_session(replace_session_id=operator.session_id)

    new_token, replacement = await create_session(
        replace_session_id=operator.session_id,
        expected_token_hash=operator._token_hash,
    )
    assert replacement.session_id == operator.session_id
    async with async_db() as db:
        tombstone = (
            await db.execute(
                select(OperatorSession).where(OperatorSession.is_bearer_tombstone.is_(True))
            )
        ).scalar_one()
        assert tombstone.replaced_by_id == operator.session_id
        tombstone_id = tombstone.id
    with pytest.raises(AuthFailure, match="session_revoked"):
        await authenticate_session(tombstone_id, touch=False)
    assert (await authenticate_token(new_token, touch=False)).session_id == operator.session_id


@pytest.mark.asyncio
async def test_operator_session_tombstone_marker_migration_is_additive_and_idempotent(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'legacy-auth.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                """
                CREATE TABLE operator_sessions (
                    id VARCHAR PRIMARY KEY,
                    token_hash VARCHAR NOT NULL UNIQUE,
                    created_at DATETIME NOT NULL,
                    last_seen_at DATETIME NOT NULL,
                    idle_expires_at DATETIME NOT NULL,
                    absolute_expires_at DATETIME NOT NULL,
                    revoked_at DATETIME,
                    replaced_by_id VARCHAR
                )
                """
            )
            await connection.exec_driver_sql(
                """
                INSERT INTO operator_sessions (
                    id, token_hash, created_at, last_seen_at,
                    idle_expires_at, absolute_expires_at
                ) VALUES ('legacy-root', 'hash', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
            await _ensure_operator_session_columns(connection)
            await _ensure_operator_session_columns(connection)
            columns = {
                row[1]: row for row in (
                    await connection.exec_driver_sql("PRAGMA table_info(operator_sessions)")
                ).fetchall()
            }
            assert columns["is_bearer_tombstone"][3] == 1
            assert str(columns["is_bearer_tombstone"][4]).strip("'") in {"0", "false", "FALSE"}
            value = (
                await connection.exec_driver_sql(
                    "SELECT is_bearer_tombstone FROM operator_sessions WHERE id = 'legacy-root'"
                )
            ).scalar_one()
            assert value in (0, False)
            indexes = (
                await connection.exec_driver_sql("PRAGMA index_list(operator_sessions)")
            ).fetchall()
            assert sum("is_bearer_tombstone" in str(row) for row in indexes) == 1
            assert any("ix_operator_sessions_replacement_state" in str(row) for row in indexes)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_refresh_maps_cas_loser_to_authenticated_denial(monkeypatch):
    async def _loser(**_kwargs):
        raise AuthFailure("session_revoked")

    monkeypatch.setattr("src.api.auth.create_session", _loser)
    request = SimpleNamespace(
        state=SimpleNamespace(
            operator=SimpleNamespace(session_id="stable-root", _token_hash="private-hash")
        )
    )
    with pytest.raises(HTTPException) as captured:
        await refresh(request, Response())
    assert captured.value.status_code == 401
    assert captured.value.detail == {"code": "session_revoked"}


@pytest.mark.asyncio
async def test_refresh_maps_auth_store_failure_to_redacted_unavailable(monkeypatch):
    async def _unavailable(**_kwargs):
        raise RuntimeError("database path must not escape")

    monkeypatch.setattr("src.api.auth.create_session", _unavailable)
    request = SimpleNamespace(
        state=SimpleNamespace(
            operator=SimpleNamespace(session_id="stable-root", _token_hash="private-hash")
        )
    )
    with pytest.raises(HTTPException) as captured:
        await refresh(request, Response())
    assert captured.value.status_code == 503
    assert captured.value.detail == {"code": "session_unavailable"}
    assert "database path" not in str(captured.value.detail)


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
    assert principal.principal_id == operator.principal.principal_id
    assert principal.session_id == "attacker-chosen-conversation"
    assert AuthorityGrant.MODEL_INFERENCE in principal.grants
    assert AuthorityGrant.EXTERNAL_MUTATION not in principal.grants
    assert AuthorityGrant.CREDENTIAL_EGRESS not in principal.grants


@pytest.mark.asyncio
async def test_concurrent_refresh_has_exactly_one_winner(tmp_path, monkeypatch):
    # The normal auth fixture intentionally uses a StaticPool for speed.  CAS
    # correctness needs two real SQLite connections so the database, rather
    # than an in-memory connection-sharing artifact, arbitrates the race.
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'operator-auth.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def file_get_session():
        async with factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    monkeypatch.setattr("src.auth.service.get_session", file_get_session)
    try:
        token, operator = await create_session()
        operator = await authenticate_token(token, touch=False)
        results = await asyncio.gather(
            create_session(
                replace_session_id=operator.session_id,
                expected_token_hash=operator._token_hash,
            ),
            create_session(
                replace_session_id=operator.session_id,
                expected_token_hash=operator._token_hash,
            ),
            return_exceptions=True,
        )
        async with factory() as db:
            rows = (await db.execute(select(OperatorSession))).scalars().all()
    finally:
        await engine.dispose()
    assert sum(isinstance(result, tuple) for result in results) == 1, repr(results)
    loser = next(result for result in results if isinstance(result, Exception))
    assert isinstance(loser, AuthFailure)
    assert loser.code == "session_revoked"
    assert sum(not row.is_bearer_tombstone and row.revoked_at is None for row in rows) == 1
    assert sum(row.is_bearer_tombstone and row.revoked_at is not None for row in rows) == 1


@pytest.mark.asyncio
async def test_concurrent_http_refresh_returns_one_success_and_one_bounded_denial(
    tmp_path,
    monkeypatch,
):
    from src.app import create_app

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'operator-http-auth.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def file_get_session():
        async with factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    monkeypatch.setattr("src.auth.service.get_session", file_get_session)
    app = create_app()
    clients = [
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test"),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test"),
    ]
    try:
        login_response = await clients[0].post(
            "/api/auth/login",
            json={"password": "correct horse battery staple"},
            headers={"origin": ORIGIN},
        )
        assert login_response.status_code == 200
        old_token = login_response.cookies.get(settings.operator_auth_cookie_name)
        assert old_token
        clients[1].cookies.set(settings.operator_auth_cookie_name, old_token)
        results = await asyncio.gather(
            *(
                client.post("/api/auth/refresh", headers={"origin": ORIGIN})
                for client in clients
            )
        )
    finally:
        await clients[0].aclose()
        await clients[1].aclose()
        await engine.dispose()

    assert sorted(response.status_code for response in results) == [200, 401]
    denied = next(response for response in results if response.status_code == 401)
    assert denied.json()["detail"]["code"] == "session_revoked"
    successful = next(response for response in results if response.status_code == 200)
    assert successful.json()["session_id"] == login_response.json()["session_id"]


@pytest.mark.asyncio
async def test_concurrent_authentication_coalesces_stale_session_touch(client, async_db):
    _, token = await _login(client)
    operator = await authenticate_token(token, touch=False)
    stale_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    async with async_db() as db:
        sync_engine = db.sync_session.get_bind()
        record = await db.get(OperatorSession, operator.session_id)
        assert record is not None
        record.last_seen_at = stale_at
        record.idle_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        db.add(record)

    update_count = 0

    def _observe_auth_touch(
        _conn, _cursor, statement, _parameters, _context, _executemany
    ):
        nonlocal update_count
        if statement.lstrip().upper().startswith("UPDATE OPERATOR_SESSIONS"):
            update_count += 1

    event.listen(sync_engine, "before_cursor_execute", _observe_auth_touch)
    try:
        token_results = await asyncio.gather(
            *(authenticate_token(token) for _ in range(40))
        )
        session_results = await asyncio.gather(
            *(authenticate_session(operator.session_id) for _ in range(40))
        )
    finally:
        event.remove(sync_engine, "before_cursor_execute", _observe_auth_touch)

    assert all(result.session_id == operator.session_id for result in token_results)
    assert all(result.session_id == operator.session_id for result in session_results)
    assert update_count == 1

    async with async_db() as db:
        record = await db.get(OperatorSession, operator.session_id)
        assert record is not None
        last_seen_at = record.last_seen_at
        if last_seen_at.tzinfo is None:
            last_seen_at = last_seen_at.replace(tzinfo=timezone.utc)
        assert last_seen_at > stale_at
        assert record.revoked_at is None


@pytest.mark.asyncio
async def test_fresh_authentication_does_not_extend_idle_session_on_every_request(
    client,
    async_db,
):
    _, token = await _login(client)
    operator = await authenticate_token(token, touch=False)
    async with async_db() as db:
        record = await db.get(OperatorSession, operator.session_id)
        assert record is not None
        baseline_last_seen = record.last_seen_at
        baseline_idle_expiry = record.idle_expires_at

    results = await asyncio.gather(
        *(authenticate_token(token) for _ in range(40))
    )
    assert all(result.session_id == operator.session_id for result in results)

    async with async_db() as db:
        record = await db.get(OperatorSession, operator.session_id)
        assert record is not None
        assert record.last_seen_at == baseline_last_seen
        assert record.idle_expires_at == baseline_idle_expiry


@pytest.mark.asyncio
async def test_short_idle_configuration_touches_before_half_idle_window(
    client,
    async_db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "operator_auth_idle_seconds", 10)
    _, token = await _login(client)
    operator = await authenticate_token(token, touch=False)
    stale_at = datetime.now(timezone.utc) - timedelta(seconds=6)
    async with async_db() as db:
        record = await db.get(OperatorSession, operator.session_id)
        assert record is not None
        record.last_seen_at = stale_at
        record.idle_expires_at = datetime.now(timezone.utc) + timedelta(seconds=3)
        db.add(record)

    await authenticate_token(token)

    async with async_db() as db:
        record = await db.get(OperatorSession, operator.session_id)
        assert record is not None
        last_seen_at = record.last_seen_at
        if last_seen_at.tzinfo is None:
            last_seen_at = last_seen_at.replace(tzinfo=timezone.utc)
        idle_expires_at = record.idle_expires_at
        if idle_expires_at.tzinfo is None:
            idle_expires_at = idle_expires_at.replace(tzinfo=timezone.utc)
        assert last_seen_at > stale_at
        assert idle_expires_at > datetime.now(timezone.utc)


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


def test_configured_lan_host_and_origin_are_enforced(monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "seraph.lan,192.168.1.50")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "https://seraph.lan")
    assert validate_request_boundary(
        host="192.168.1.50:8004", origin="https://seraph.lan", method="PUT"
    ) is None
    assert validate_request_boundary(
        host="192.168.1.50:8004", origin=None, method="PUT"
    ) == "mutation_origin_required"
    assert validate_request_boundary(
        host="192.168.1.50:8004", origin="https://evil.example", method="PUT"
    ) == "origin_forbidden"
    assert validate_request_boundary(
        host="evil.example", origin="https://seraph.lan", method="PUT"
    ) == "origin_forbidden"


@pytest.mark.asyncio
async def test_revocation_watch_closes_socket_and_cancels_active_turn(monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_revocation_poll_seconds", 0.25)
    revoked = asyncio.Event()
    guard = Event()
    closed = {}

    class FakeWebSocket:
        async def close(self, *, code, reason):
            closed.update(code=code, reason=reason)

    async def _revoked(_session_id, *, touch=False):
        raise AuthFailure("session_revoked")

    monkeypatch.setattr("src.api.ws.authenticate_websocket_session", _revoked)
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

    async def _unavailable(_session_id, *, touch=False):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("src.api.ws.authenticate_websocket_session", _unavailable)
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
        # An omitted session exercises the authenticated ingress creation path;
        # explicit unknown IDs are intentionally rejected by SessionManager.
        json={"message": "hello"},
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
async def test_authenticated_operator_can_read_runtime_and_settings_without_provider_transport(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    _, token = await _login(client)
    monkeypatch.setattr(
        "src.api.model_fabric_settings.httpx.AsyncClient",
        lambda *args, **kwargs: pytest.fail("metadata reads must not call provider transport"),
    )

    runtime = await client.get("/api/runtime/status")
    settings_response = await client.get("/api/settings/model-fabric")

    assert runtime.status_code == 200
    assert settings_response.status_code == 200
    fabric = runtime.json()["model_fabric"]
    assert fabric["status"] == "blocked"
    assert fabric["inference_readiness"]["status"] == "blocked"
    assert "chat_cloud_consent_missing" in fabric["inference_readiness"]["reasons"]
    assert fabric["inference_accounting"]["status"] == "blocked"
    assert fabric["inference_accounting"]["reason_code"] == "accounting_continuity_unavailable"
    assert fabric["inference_accounting"]["reason_code"] in fabric["inference_readiness"]["reasons"]
    assert fabric["inference_accounting"]["remaining_microusd"] is None
    # Metadata may expose the boolean ``api_key_configured`` flag; raw key
    # material must never be returned.
    assert "test-key" not in runtime.text
    assert "test-key" not in settings_response.text
