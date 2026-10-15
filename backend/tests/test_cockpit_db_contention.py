"""#702: a foreign SQLite writer must not wedge the cockpit's read fan-out.

A file-backed database configured like production (WAL, 5s busy timeout,
pooled connections) serves the real app while another connection holds
``BEGIN IMMEDIATE``. Every cockpit baseline read must still answer with a 200
(or an explicit degraded payload) well inside the cockpit's 5s fetch timeout,
with or without a session touch due.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from config.settings import settings
from src.app import create_app
from src.auth import service as auth_service
from src.db.engine import _configure_sqlite_connection, _ensure_search_indexes, override_session_factory

COCKPIT_BASELINE = (
    "/api/auth/session",
    "/api/runtime/status",
    "/api/observer/state",
    "/api/audit/events?limit=12",
    "/api/approvals/pending?limit=8",
    "/api/capabilities/overview",
    "/api/extensions",
    "/api/settings/tool-policy-mode",
    "/api/settings/mcp-policy-mode",
    "/api/settings/approval-mode",
    "/api/observer/continuity",
    "/api/work-board/tasks",
)
# Before #702 these reads queued for the 5s busy timeout (the cockpit's own
# fetch timeout is also 5s). Some endpoints are CPU-bound (catalog scans) and
# host-load sensitive, so the writer's cost is measured against the same
# fan-out without a writer: a deferred bookkeeping write may add its short lock
# wait plus scheduling jitter (about 1s on an idle host, more on a saturated
# one), never the busy timeout. The writer also outlives every fan-out, so no
# answer can come from the lock being released.
_SQLITE_BUSY_TIMEOUT_SECONDS = 5.0
_WRITER_ADDED_LATENCY_BOUND_SECONDS = _SQLITE_BUSY_TIMEOUT_SECONDS - 1.0
_WRITER_HOLD_SECONDS = 60.0


@asynccontextmanager
async def _cockpit(tmp_path, monkeypatch):
    from src.auth.service import create_session

    path = tmp_path / "cockpit.sqlite3"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{path}",
        connect_args={"check_same_thread": False},
        pool_size=20,
        max_overflow=0,
        pool_timeout=3,
        pool_pre_ping=True,
    )
    event.listen(engine.sync_engine, "connect", _configure_sqlite_connection)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        await _ensure_search_indexes(conn)
    monkeypatch.setattr(settings, "operator_auth_secret", "cockpit-contention-secret")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test,localhost,127.0.0.1")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        # The context override reaches every get_session caller, including
        # the request tasks the ASGI transport and middleware spawn from here.
        with override_session_factory(factory):
            token, _operator = await create_session()
            async with AsyncClient(
                transport=ASGITransport(app=create_app()),
                base_url="http://localhost",
                cookies={settings.operator_auth_cookie_name: token},
                timeout=30,
            ) as client:
                yield client, path
    finally:
        await engine.dispose()


async def _fan_out(client: AsyncClient) -> dict[str, tuple[int, float, object]]:
    async def one(path: str):
        started = time.monotonic()
        response = await client.get(path)
        return path, (response.status_code, time.monotonic() - started, response.json())

    return dict(await asyncio.gather(*(one(path) for path in COCKPIT_BASELINE)))


@contextmanager
def _foreign_writer(path):
    release, held = threading.Event(), threading.Event()
    released_early = []

    def hold() -> None:
        connection = sqlite3.connect(path, isolation_level=None, timeout=30)
        try:
            connection.execute("BEGIN IMMEDIATE")
            held.set()
            released_early.append(not release.wait(_WRITER_HOLD_SECONDS))
            connection.execute("COMMIT")
        finally:
            connection.close()

    writer = threading.Thread(target=hold, daemon=True)
    writer.start()
    assert held.wait(5)
    try:
        yield
    finally:
        release.set()
        writer.join(10)
    assert not writer.is_alive()
    assert released_early == [False], "the writer timed out before the reads finished"


def _sql(path, statement: str, *params):
    connection = sqlite3.connect(path, isolation_level=None, timeout=30)
    try:
        return connection.execute(statement, params).fetchall()
    finally:
        connection.close()


def _sqlite_now(offset: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + offset).strftime("%Y-%m-%d %H:%M:%S.%f")


def _last_seen(path) -> str:
    return _sql(path, "SELECT last_seen_at FROM operator_sessions")[0][0]


def _assert_cockpit_answered(results, baseline, label: str) -> None:
    for path, (status, elapsed, body) in results.items():
        assert status == 200, f"{label}: {path} answered {status}: {body}"
        added = elapsed - baseline[path][1]
        assert added < _WRITER_ADDED_LATENCY_BOUND_SECONDS, (
            f"{label}: {path} took {elapsed:.2f}s ({added:.2f}s over the writer-free fan-out)"
        )
    runtime = results["/api/runtime/status"][2]
    # Accounting may be blocked in a test workspace, but the effective
    # provider path must still be reported rather than collapsing to unknown.
    assert runtime["provider"] and runtime["provider"] != "unknown", runtime
    assert runtime["model"], runtime


async def _writer_free_baseline(client):
    await _fan_out(client)  # imports, caches and table checks
    baseline = await _fan_out(client)
    assert all(status == 200 for status, _elapsed, _body in baseline.values()), baseline
    return baseline


@pytest.mark.asyncio
async def test_cockpit_reads_do_not_queue_behind_a_foreign_writer(tmp_path, monkeypatch):
    async with _cockpit(tmp_path, monkeypatch) as (client, path):
        baseline = await _writer_free_baseline(client)
        # A loaded host can make the baseline outlast the 30s touch interval;
        # pin the session as just seen so this case really has no touch due.
        _sql(path, "UPDATE operator_sessions SET last_seen_at = ?", _sqlite_now())
        with _foreign_writer(path):
            # Two polls: nothing deferred by the first may stall the second.
            _assert_cockpit_answered(await _fan_out(client), baseline, "touch not due")
            _assert_cockpit_answered(await _fan_out(client), baseline, "touch not due, second poll")


@pytest.mark.asyncio
async def test_due_session_touch_defers_under_a_foreign_writer(tmp_path, monkeypatch):
    async with _cockpit(tmp_path, monkeypatch) as (client, path):
        baseline = await _writer_free_baseline(client)
        stale = _sqlite_now(timedelta(minutes=-10))
        _sql(path, "UPDATE operator_sessions SET last_seen_at = ?", stale)
        with _foreign_writer(path):
            _assert_cockpit_answered(await _fan_out(client), baseline, "touch due")
            # The touch could not persist: the validated operator was served
            # and the session was not extended.
            assert _last_seen(path) == stale

        # Without the writer the next due touch persists normally (once the
        # short post-deferral backoff has passed).
        monkeypatch.setattr(auth_service, "_auth_touch_retry_after", 0.0)
        response = await client.get("/api/auth/session")
        assert response.status_code == 200
        assert _last_seen(path) != stale


@pytest.mark.asyncio
async def test_expired_session_fails_closed_under_a_foreign_writer(tmp_path, monkeypatch):
    async with _cockpit(tmp_path, monkeypatch) as (client, path):
        assert (await client.get("/api/auth/session")).status_code == 200
        expired = _sqlite_now(timedelta(seconds=-1))
        _sql(path, "UPDATE operator_sessions SET idle_expires_at = ?", expired)
        with _foreign_writer(path):
            started = time.monotonic()
            response = await client.get("/api/auth/session")
            assert time.monotonic() - started < _WRITER_ADDED_LATENCY_BOUND_SECONDS
            # The revocation write is deferred, never the expiry verdict.
            assert response.status_code == 401
            assert response.json()["detail"]["code"] == "session_expired"
            assert _sql(path, "SELECT revoked_at FROM operator_sessions")[0][0] is None
        response = await client.get("/api/auth/session")
        assert response.status_code == 401
        assert _sql(path, "SELECT revoked_at FROM operator_sessions")[0][0] is not None
