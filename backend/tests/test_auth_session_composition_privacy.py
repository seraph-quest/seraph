"""Original Auth privacy under a real stopped empty composition.

Actual create/refresh issuers run before activation. The original deployment,
accounting and stopped-v2 transition write their real private files. There is no
selected programme, execution Source, service claim, or inference contact here.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import Response
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel
from starlette.requests import Request

from config.settings import settings
from src.auth import service as auth
from src.api.auth import logout, _reset_login_throttle_for_tests
from src.app import create_app
from src.db.engine import get_session as canonical_session, override_session_factory, _configure_sqlite_connection
from src.db.models import OperatorSession
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError, MAX_BYTES
from src.runtime_plugins.ownership import DOMAINS, begin_native_writer, initialize_fresh_deployment
from src.workflows.job_runtime import DurableJobRepository
from src.workspace.accounting_continuity import transition_programme_envelope
from src.workspace.production import (
    ProductionWorkspace, maintenance_fence, prepare_lifecycle_directory,
    read_lifecycle_receipt, read_accounting_checkpoint,
)

ORIGIN = "http://localhost:3001"
PRIVATE_KEYS = {"token", "token_hash", "_token_hash", "access_token", "refresh_token"}


def private_files(case):
    return {name: (case.workspace.lifecycle_directory / name).read_bytes()
            for name in ("receipt.json", "accounting-checkpoint.json")}


def sql_state(case):
    # Test-only physical observer; literal rows stay in memory, never receipts.
    with sqlite3.connect(case.database) as db:
        state = {name: (tuple(r[1] for r in db.execute('PRAGMA table_info("' + name + '")')),
                        tuple(sorted(db.execute('SELECT * FROM "' + name + '"').fetchall(), key=repr)))
                 for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")}
        state["sqlite_schema"] = (("type", "name", "tbl_name", "rootpage", "sql"),
                                   tuple(sorted(db.execute("SELECT * FROM sqlite_master").fetchall(), key=repr)))
        return state


def state_digest(state):
    return hashlib.sha256(repr(state).encode()).hexdigest()


def root_row(case, state=None, session_id=None):
    columns, rows = (state or sql_state(case))["operator_sessions"]
    identity = session_id or case.operator.session_id
    return dict(zip(columns, next(row for row in rows if row[columns.index("id")] == identity)))


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def assert_only_original_root_changed(case, before, allowed):
    after = sql_state(case)
    assert set(after) == set(before)
    for table in before:
        if table != "operator_sessions":
            assert state_digest({table: after[table]}) == state_digest({table: before[table]})
    columns, old_rows = before["operator_sessions"]
    new_columns, new_rows = after["operator_sessions"]
    assert columns == new_columns and len(old_rows) == len(new_rows)
    old = {row[columns.index("id")]: dict(zip(columns, row)) for row in old_rows}
    new = {row[columns.index("id")]: dict(zip(columns, row)) for row in new_rows}
    assert set(old) == set(new)
    for identity in old:
        changed = {field for field in columns if old[identity][field] != new[identity][field]}
        assert changed <= (allowed if identity == case.operator.session_id else set())
    return root_row(case, after)


def assert_public_privacy(payload):
    if isinstance(payload, dict):
        assert PRIVATE_KEYS.isdisjoint(payload)
        for value in payload.values():
            assert_public_privacy(value)
    elif isinstance(payload, list):
        for value in payload:
            assert_public_privacy(value)


@pytest_asyncio.fixture
async def original_auth_composition(tmp_path, monkeypatch, request):
    _reset_login_throttle_for_tests()
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "isolated-original-auth-privacy")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test,localhost,127.0.0.1")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", ORIGIN)
    monkeypatch.setattr(settings, "operator_auth_idle_seconds", 300)
    monkeypatch.setattr(settings, "operator_auth_absolute_seconds", 3600)
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(settings, "workspace_dir", str(root))
    monkeypatch.setenv("SERAPH_WORKSPACE_LIFECYCLE_PATH", str(tmp_path / "deployment"))
    database = root / "seraph.db"
    engine = create_async_engine("sqlite+aiosqlite:///" + str(database))
    event.listen(engine.sync_engine, "connect", _configure_sqlite_connection)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    provider_attempts = []
    async def deny_async_provider(*args, **kwargs):
        provider_attempts.append("async")
        raise AssertionError("external transport denied in original Auth regression")
    def deny_sync_provider(*args, **kwargs):
        provider_attempts.append("sync")
        raise AssertionError("external transport denied in original Auth regression")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_async_provider)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_sync_provider)
    current_time = [datetime.now(timezone.utc)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current_time[0] if tz else current_time[0].replace(tzinfo=None)
    monkeypatch.setattr(auth, "datetime", Clock)
    try:
        with override_session_factory(factory):
            old_token, operator = await auth.create_session()
            token = old_token
            mode = getattr(request, "param", "active")
            if mode == "refreshed":
                token, operator = await auth.create_session(
                    replace_session_id=operator.session_id, expected_token_hash=operator._token_hash)
            workspace = ProductionWorkspace(host_root=root)
            prepare_lifecycle_directory(workspace)
            with maintenance_fence(workspace):
                async with canonical_session() as db:
                    await begin_native_writer(db, owner="composition_maintenance", fresh=True)
                    await initialize_fresh_deployment(db, composition_digests={domain: "a" * 64 for domain in DOMAINS})
            await DurableJobRepository().configure_inference_accounting(1000)
            with maintenance_fence(workspace):
                transition_programme_envelope(workspace=workspace, budget=HeaderReadBudget())
            case = SimpleNamespace(database=database, engine=engine, workspace=workspace, operator=operator,
                                   token=token, old_token=old_token, clock=current_time)
            receipt = read_lifecycle_receipt(workspace)
            assert receipt["runtime_composition"]["schema_version"] == 2
            assert receipt["runtime_composition"]["programme"] is None
            assert read_accounting_checkpoint(workspace)["composition_target"] == receipt["runtime_composition"]
            yield case
    finally:
        await engine.dispose()
        _reset_login_throttle_for_tests()
        assert provider_attempts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup", ["bearer", "session"])
@pytest.mark.parametrize("due", [False, True])
async def test_original_composed_due_and_non_due_touch_preserve_private_owner(original_auth_composition, lookup, due):
    case = original_auth_composition
    before, files = sql_state(case), private_files(case)
    original = root_row(case, before)
    case.clock[0] = utc(original["last_seen_at"]) + timedelta(seconds=31 if due else 1)
    operator = (await auth.authenticate_token(case.token) if lookup == "bearer"
                else await auth.authenticate_session(case.operator.session_id))
    assert operator.session_id == case.operator.session_id
    assert operator.principal.principal_id == case.operator.principal.principal_id
    final = assert_only_original_root_changed(case, before, {"last_seen_at", "idle_expires_at"})
    assert state_digest({"binding": final["token_hash"]}) == state_digest({"binding": original["token_hash"]})
    assert final["revoked_at"] is None
    assert final["absolute_expires_at"] == original["absolute_expires_at"]
    if due:
        assert utc(final["last_seen_at"]) == case.clock[0]
        assert utc(final["idle_expires_at"]) == min(case.clock[0] + timedelta(seconds=300), utc(original["absolute_expires_at"]))
    else:
        assert state_digest(sql_state(case)) == state_digest(before)
        assert private_files(case) == files
    receipt, checkpoint = read_lifecycle_receipt(case.workspace), read_accounting_checkpoint(case.workspace)
    assert checkpoint["composition_target"] == receipt["runtime_composition"]
    assert receipt["runtime_composition"]["programme"] is None
    assert json.loads(files["receipt.json"])["inference_accounting"] == receipt["inference_accounting"]


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup", ["bearer", "session"])
async def test_touch_false_expiry_commits_original_revocation_before_failure(original_auth_composition, lookup):
    case = original_auth_composition
    before = sql_state(case)
    case.clock[0] = utc(root_row(case, before)["idle_expires_at"]) + timedelta(seconds=1)
    with pytest.raises(auth.AuthFailure, match="^session_expired$"):
        if lookup == "bearer":
            await auth.authenticate_token(case.token, touch=False)
        else:
            await auth.authenticate_session(case.operator.session_id, touch=False)
    final = assert_only_original_root_changed(case, before, {"revoked_at"})
    assert utc(final["revoked_at"]) == case.clock[0]
    after = state_digest(sql_state(case))
    with pytest.raises(auth.AuthFailure, match="^session_revoked$"):
        await auth.authenticate_token(case.token, touch=False)
    assert state_digest(sql_state(case)) == after


@pytest.mark.asyncio
async def test_public_logout_commits_denial_before_cookie_clear_success(original_auth_composition):
    case = original_auth_composition
    before = sql_state(case)
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set(settings.operator_auth_cookie_name, case.token)
        status = await client.get("/api/auth/session")
        assert status.status_code == 200
        assert_public_privacy(status.json())
        response = await client.post("/api/auth/logout", headers={"origin": ORIGIN})
        assert response.status_code == 204
        cleared_cookies = SimpleCookie()
        for header in response.headers.get_list("set-cookie"):
            cleared_cookies.load(header)
        assert settings.operator_auth_cookie_name in cleared_cookies
        cleared_auth_cookie = cleared_cookies[settings.operator_auth_cookie_name]
        assert cleared_auth_cookie["max-age"] == "0"
        assert cleared_auth_cookie.value == ""
        final = assert_only_original_root_changed(case, before, {"revoked_at"})
        assert final["revoked_at"] is not None
        client.cookies.set(settings.operator_auth_cookie_name, case.token)
        denied = await client.get("/api/auth/session")
        assert denied.status_code == 401
        assert denied.json() == {"detail": {"code": "session_revoked"}}


def exhaust_original_budget(monkeypatch):
    # Negative-only consumption of the ACTUAL frame type; no caller certificate,
    # replacement budget, cap change, grant or positive witness is supplied.
    actual = HeaderReadBudget.__init__
    def depleted(self):
        actual(self)
        self.debit(MAX_BYTES, appearance=("isolated-negative-exhaustion",))
    monkeypatch.setattr(HeaderReadBudget, "__init__", depleted)


@contextmanager
def observe_prebody_sql(case):
    """Observe actual owner SQL; numeric/header/cost statements remain allowed.

    Compiled mapped-entity selects identify ORM materialization before execution.
    Full literal column delivery also catches an equivalent raw full-row SELECT.
    No query parameters, token values or row bodies are retained by this observer.
    """
    observed = {"queries": 0, "inventory_probes": 0, "full_bodies_started": 0,
                "full_bodies_delivered": 0}
    expected_columns = tuple(OperatorSession.__table__.columns.keys())

    def before_query(connection, cursor, statement, parameters, context, executemany):
        observed["queries"] += 1
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("select 1 from") and "runtime_composition_states" in normalized:
            observed["inventory_probes"] += 1
        compiled = getattr(context, "compiled", None)
        source = getattr(compiled, "statement", None)
        descriptions = getattr(source, "column_descriptions", ())
        if any(item.get("entity") is OperatorSession and item.get("expr") is OperatorSession
               for item in descriptions):
            observed["full_bodies_started"] += 1
            raise AssertionError("selected_root_body_before_capacity_denial")

    def after_query(connection, cursor, statement, parameters, context, executemany):
        delivered = tuple(column[0] for column in (cursor.description or ()))
        if len(delivered) == len(expected_columns) and set(delivered) == set(expected_columns):
            observed["full_bodies_delivered"] += 1
            raise AssertionError("selected_root_body_before_capacity_denial")

    event.listen(case.engine.sync_engine, "before_cursor_execute", before_query)
    event.listen(case.engine.sync_engine, "after_cursor_execute", after_query)
    try:
        yield observed
    finally:
        event.remove(case.engine.sync_engine, "after_cursor_execute", after_query)
        event.remove(case.engine.sync_engine, "before_cursor_execute", before_query)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["bearer", "session", "expiry", "logout"])
async def test_capacity_failure_preserves_sql_files_and_never_clears_cookie(original_auth_composition, monkeypatch, operation):
    case = original_auth_composition
    if operation == "expiry":
        case.clock[0] = utc(root_row(case)["idle_expires_at"]) + timedelta(seconds=1)
    before, files = state_digest(sql_state(case)), private_files(case)
    response = Response()
    request = Request({"type": "http", "method": "POST", "path": "/api/auth/logout", "headers": []})
    request.state.operator = case.operator  # Actual original issuer return, no caller identity.
    exhaust_original_budget(monkeypatch)
    with observe_prebody_sql(case) as observed:
        with pytest.raises(HeaderBoundsError, match="^canonical_bound_not_certified$"):
            if operation == "logout":
                await logout(request, response)
            elif operation == "session":
                await auth.authenticate_session(case.operator.session_id)
            else:
                await auth.authenticate_token(case.token, touch=operation != "expiry")
    assert observed["queries"] >= 2
    assert observed["inventory_probes"] >= 2  # Actual owner's pre-yield existence/populated probes.
    assert observed["full_bodies_started"] == observed["full_bodies_delivered"] == 0
    assert "set-cookie" not in response.headers
    assert state_digest(sql_state(case)) == before
    assert private_files(case) == files
    assert root_row(case)["revoked_at"] is None  # No false claim of expiry/logout commit.


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing_index", "nonunique_index", "extra_column"])
async def test_schema_or_locator_failure_preserves_original_sql_and_private_files(original_auth_composition, damage):
    case = original_auth_composition
    # Actual adversarial DDL, never an accepted witness or changed grant.
    with sqlite3.connect(case.database) as db:
        if damage == "extra_column":
            db.execute("ALTER TABLE operator_sessions ADD COLUMN unintended TEXT")
        else:
            db.execute("DROP INDEX ix_operator_sessions_token_hash")
            if damage == "nonunique_index":
                db.execute("CREATE INDEX ix_operator_sessions_token_hash ON operator_sessions(token_hash)")
    before, files = state_digest(sql_state(case)), private_files(case)
    with observe_prebody_sql(case) as observed:
        with pytest.raises(HeaderBoundsError, match="^header_schema_changed$" if damage == "extra_column" else "^header_locator_changed$"):
            await auth.authenticate_token(case.token)
    assert observed["full_bodies_started"] == observed["full_bodies_delivered"] == 0
    assert state_digest(sql_state(case)) == before
    assert private_files(case) == files
    assert root_row(case)["revoked_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("original_auth_composition", ["refreshed"], indirect=True)
async def test_real_retired_bearer_tombstone_never_becomes_owner_or_websocket_alias(original_auth_composition):
    case = original_auth_composition
    before, files = state_digest(sql_state(case)), private_files(case)
    columns, rows = sql_state(case)["operator_sessions"]
    tombstones = [dict(zip(columns, row)) for row in rows if row[columns.index("is_bearer_tombstone")]]
    assert len(tombstones) == 1 and tombstones[0]["replaced_by_id"] == case.operator.session_id
    assert tombstones[0]["revoked_at"] is not None
    with pytest.raises(auth.AuthFailure, match="^session_revoked$"):
        await auth.authenticate_token(case.old_token, touch=False)
    for validate in (auth.authenticate_session, auth.authenticate_websocket_session):
        with pytest.raises(auth.AuthFailure, match="^session_revoked$"):
            await validate(tombstones[0]["id"], touch=False)
    original = await auth.authenticate_token(case.token, touch=False)
    assert original.session_id == case.operator.session_id
    assert original.principal.principal_id == case.operator.principal.principal_id
    assert original.ownership_continuity == "stable"
    assert original.ownership_recovery_action is None
    assert state_digest(sql_state(case)) == before
    assert private_files(case) == files


@pytest.mark.asyncio
async def test_unbounded_session_locator_is_denied_without_private_mutation(original_auth_composition):
    case = original_auth_composition
    before, files = state_digest(sql_state(case)), private_files(case)
    with observe_prebody_sql(case) as observed:
        with pytest.raises(HeaderBoundsError, match="^header_request_bound$"):
            await auth.authenticate_session("x" * 513, touch=False)
    assert observed["full_bodies_started"] == observed["full_bodies_delivered"] == 0
    assert state_digest(sql_state(case)) == before
    assert private_files(case) == files
