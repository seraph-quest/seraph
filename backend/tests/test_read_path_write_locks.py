"""#1074: chat-turn and cockpit read paths must not queue behind a SQLite writer."""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import asynccontextmanager, contextmanager

import pytest
from sqlalchemy import event

from config.settings import settings
from src.db.models import MemoryKind
from src.memory.repository import memory_repository

# Before #1074 each of these waited out the 5s busy timeout at least once.
_READ_BOUND_SECONDS = 2.0


@asynccontextmanager
async def _database(async_db):
    async with async_db() as db:
        engine = db.get_bind()
    statements: list[str] = []

    def record(_conn, _cursor, statement, *_args):
        words = statement.split()
        if words and words[0].upper() in {"BEGIN", "INSERT", "UPDATE", "DELETE"}:
            import greenlet, sys
            stack = []
            g = greenlet.getcurrent()
            while g is not None:
                f = g.gr_frame if g is not greenlet.getcurrent() else sys._getframe()
                while f is not None:
                    stack.append(f); f = f.f_back
                g = g.parent
            chain = [f"{f.f_code.co_filename.split('/backend/')[-1]}:{f.f_lineno}:{f.f_code.co_name}" for f in stack if "/backend/src/" in f.f_code.co_filename and "db/engine" not in f.f_code.co_filename][:5]
            statements.append(" ".join(words[:3]).upper() + " @ " + " <- ".join(chain))

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield engine.url.database, statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


@contextmanager
def _foreign_writer(path):
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        connection = sqlite3.connect(path, isolation_level=None, timeout=30)
        try:
            connection.execute("BEGIN IMMEDIATE")
            held.set()
            release.wait(60)
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


async def _seed_memories():
    await memory_repository.create_memory(
        content="Review the Atlas brief tomorrow morning.",
        kind=MemoryKind.commitment,
        summary="Review the Atlas brief tomorrow morning",
        importance=0.95,
    )
    await memory_repository.create_memory(
        content="User prefers concise morning briefings.",
        kind=MemoryKind.communication_preference,
        summary="Prefers concise morning briefings",
        importance=0.8,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_chat_turn_builds_full_guardian_state_under_a_foreign_writer(async_db):
    from src.agent.session import SessionManager
    from src.guardian.state import build_guardian_state

    await SessionManager().get_or_create("contended")
    await _seed_memories()
    async with _database(async_db) as (path, statements):
        await build_guardian_state(session_id="contended", user_message="What next?")
        # A new memory changes the bounded snapshot, so this turn also wants
        # to persist the snapshot cache while the writer holds the lock.
        await memory_repository.create_memory(
            content="Alice owns the investor update thread.",
            kind=MemoryKind.collaborator,
            summary="Alice owns investor updates",
            importance=0.9,
        )
        statements.clear()
        with _foreign_writer(path):
            started = time.monotonic()
            state = await build_guardian_state(session_id="contended", user_message="What next?")
            elapsed = time.monotonic() - started
        # Full state (not the minimal-agent fallback) inside the chat budget.
        assert elapsed < min(_READ_BOUND_SECONDS, settings.guardian_state_timeout_seconds), statements
        assert "Prefers concise morning briefings" in state.memory_context
        assert "Alice owns investor updates" in state.bounded_memory_context
        # One deferred tombstone check per turn instead of five serialized ones;
        # the only writer-lock attempt is the bounded snapshot-cache save.
        assert len([item for item in statements if "_reconcile_memory_tombstones_pass" in item]) == 1
        assert all(":save_snapshot" in item for item in statements if item.startswith("BEGIN IMMEDIATE"))


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_tombstone_reapply_after_stale_restore_still_happens(async_db):
    from src.db.models import Memory, MemoryStatus
    from src.memory.snapshots import get_or_create_bounded_guardian_snapshot

    created = await memory_repository.create_memory(
        content="Private restore poison must never return.",
        summary="Private restore poison",
        kind=MemoryKind.fact,
    )
    await memory_repository.mark_memory_tombstoned(created.memory_id, actor="operator", reason="user deletion")
    await memory_repository.reconcile_memory_tombstones()
    async with _database(async_db) as (_path, statements):
        settled = await memory_repository.reconcile_memory_tombstones()
        assert settled["reapplied_count"] == 0 and "reapply_due" not in settled
        # Nothing due: the read never takes the writer lock.
        assert not [item for item in statements if item.startswith(("BEGIN IMMEDIATE", "UPDATE"))]

        # A stale backup restores the deleted row while the ledger survives.
        async with async_db() as db:
            await db.execute(
                Memory.__table__.update()
                .where(Memory.id == created.memory_id)
                .values(content="Private restore poison must never return.",
                        summary="Private restore poison", status=MemoryStatus.active,
                        importance=0.9, metadata_json="{}")
            )
        statements.clear()
        snapshot = await get_or_create_bounded_guardian_snapshot()
        assert "Private restore poison" not in snapshot
        assert any(item.startswith("BEGIN IMMEDIATE") for item in statements)
    restored = await memory_repository.get_memory(created.memory_id, include_deleted=True)
    assert restored.status is MemoryStatus.archived
    assert "poison" not in (restored.content or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_expired_insights_and_approvals_stay_hidden_without_waiting_for_a_writer(async_db):
    from datetime import datetime, timedelta, timezone
    from src.approval.repository import approval_repository
    from src.db.models import ApprovalRequest, QueuedInsight, Session
    from src.observer.insight_queue import insight_queue

    async with async_db() as db:
        db.add(Session(id="approval-session"))
    fresh = await insight_queue.enqueue("Fresh insight", urgency=4)
    stale = await insight_queue.enqueue("Expired insight", urgency=5)
    live = await approval_repository.get_or_create_pending(
        session_id="approval-session", tool_name="shell", risk_level="high",
        summary="Live approval", fingerprint="live", details={},
    )
    lapsed = await approval_repository.get_or_create_pending(
        session_id="approval-session", tool_name="shell", risk_level="high",
        summary="Lapsed approval", fingerprint="lapsed", details={},
    )
    async with async_db() as db:
        (await db.get(QueuedInsight, stale.id)).created_at = datetime.now(timezone.utc) - timedelta(days=30)
        (await db.get(ApprovalRequest, lapsed.id)).expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)

    async with _database(async_db) as (path, _statements):
        with _foreign_writer(path):
            started = time.monotonic()
            insights = await insight_queue.peek_all()
            pending = await approval_repository.list_pending(session_id="approval-session")
            assert time.monotonic() - started < _READ_BOUND_SECONDS
            # Expired items are never presented as active, even unpersisted.
            assert [item.id for item in insights] == [fresh.id]
            assert [item["id"] for item in pending] == [live.id]
        async with async_db() as db:
            assert await db.get(QueuedInsight, stale.id) is not None
            assert (await db.get(ApprovalRequest, lapsed.id)).status == "pending"

        # With the lock free the next read persists the housekeeping.
        assert [item.id for item in await insight_queue.peek_all()] == [fresh.id]
        assert [item["id"] for item in await approval_repository.list_pending(session_id="approval-session")] == [live.id]
    async with async_db() as db:
        assert await db.get(QueuedInsight, stale.id) is None
        assert (await db.get(ApprovalRequest, lapsed.id)).status == "expired"


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_idle_daemon_poll_takes_no_writer_lock_and_audits_once_per_window(async_db, monkeypatch):
    from src.api import observer as observer_api
    from src.observer.native_notification_queue import NativeNotificationQueue

    queue = NativeNotificationQueue()
    async with _database(async_db) as (path, statements):
        await queue.claim_next(worker_id="idle-daemon")
        assert not [item for item in statements if not item.startswith("BEGIN")], statements
        with _foreign_writer(path):
            started = time.monotonic()
            assert await queue.claim_next(worker_id="idle-daemon") is None
            assert time.monotonic() - started < _READ_BOUND_SECONDS

    monkeypatch.setattr(observer_api, "_empty_poll_audited_at", {})
    assert observer_api._empty_poll_audit_due("worker-a") is True
    assert observer_api._empty_poll_audit_due("worker-a") is False
    assert observer_api._empty_poll_audit_due("worker-b") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_long_writer_lock_hold_is_logged_with_its_call_site(async_db, monkeypatch, caplog):
    import logging
    from sqlalchemy import text
    from src.db import engine as db_engine

    async with async_db() as db:
        engine = db.get_bind()
    for name, listener in (("before_cursor_execute", db_engine._note_write_start),
                           ("commit", db_engine._report_write_hold), ("rollback", db_engine._report_write_hold)):
        event.listen(engine, name, listener)
    monkeypatch.setattr(db_engine, "_WRITE_HOLD_WARN_SECONDS", 0.2)

    async def slow_writer():
        async with async_db() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            await db.execute(text("SELECT 1"))
            time.sleep(0.3)

    async def quick_writer():
        async with async_db() as db:
            await db.execute(text("BEGIN IMMEDIATE"))

    try:
        with caplog.at_level(logging.WARNING, logger="src.db.engine"):
            await quick_writer()
            await slow_writer()
    finally:
        for name, listener in (("before_cursor_execute", db_engine._note_write_start),
                               ("commit", db_engine._report_write_hold), ("rollback", db_engine._report_write_hold)):
            event.remove(engine, name, listener)
    held = [record.getMessage() for record in caplog.records if "writer lock held" in record.getMessage()]
    assert len(held) == 1 and "slow_writer" in held[0], held
