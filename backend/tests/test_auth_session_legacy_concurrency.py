"""Actual legacy reads and original coalescing writers."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from src.auth import service as auth
from src.db.models import OperatorSession
from tests.test_operator_auth import configured_auth
from tests.test_auth_session_composition_privacy import original_auth_composition


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_legacy_reads_overlap_held_touch_lock_and_stale_writers_coalesce(async_db, monkeypatch):
    token, operator = await auth.create_session()
    lock = auth._auth_touch_lock()
    entered = [asyncio.Event(), asyncio.Event()]
    release_reads = asyncio.Event()
    gate_reads = True
    original_token = auth._find_token_record
    original_session = auth._find_session_record

    async def observe_token(*args, **kwargs):
        result = await original_token(*args, **kwargs)
        if gate_reads:
            entered[0].set()
            await release_reads.wait()
        return result

    async def observe_session(*args, **kwargs):
        result = await original_session(*args, **kwargs)
        if gate_reads:
            entered[1].set()
            await release_reads.wait()
        return result

    monkeypatch.setattr(auth, "_find_token_record", observe_token)
    monkeypatch.setattr(auth, "_find_session_record", observe_session)
    tasks = []
    held = False
    engine = None
    updates = []

    def observe_update(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("UPDATE OPERATOR_SESSIONS"):
            updates.append(statement)

    try:
        await lock.acquire()
        held = True
        tasks = [asyncio.create_task(auth.authenticate_token(token)),
                 asyncio.create_task(auth.authenticate_websocket_session(operator.session_id, touch=False))]
        await asyncio.wait_for(asyncio.gather(*(item.wait() for item in entered)), 3)
        # Both original database readers reached their post-read gate while
        # the actual coalescing lock was held; neither awaits the other read.
        assert not any(task.done() for task in tasks)
        release_reads.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 3)
        assert all(result.session_id == operator.session_id for result in results)
        assert lock.locked()
        gate_reads = False
        stale = datetime.now(timezone.utc) - timedelta(minutes=1)
        async with async_db() as db:
            engine = db.sync_session.get_bind()
            row = await db.get(OperatorSession, operator.session_id)
            row.last_seen_at = stale
            row.idle_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        event.listen(engine, "before_cursor_execute", observe_update)
        attempts = []
        both_waiting = asyncio.Event()
        original_acquire = lock.acquire

        async def observe_acquire():
            attempts.append(asyncio.current_task())
            if len(attempts) == 2:
                both_waiting.set()
            return await original_acquire()

        monkeypatch.setattr(lock, "acquire", observe_acquire)
        tasks = [asyncio.create_task(auth.authenticate_token(token)),
                 asyncio.create_task(auth.authenticate_session(operator.session_id))]
        await asyncio.wait_for(both_waiting.wait(), 3)
        assert len(set(attempts)) == 2
        assert not any(task.done() for task in tasks)
        assert updates == []
        lock.release()
        held = False
        results = await asyncio.wait_for(asyncio.gather(*tasks), 3)
        assert all(result.session_id == operator.session_id for result in results)
        assert len(updates) == 1
        async with async_db() as db:
            row = await db.get(OperatorSession, operator.session_id)
            seen = row.last_seen_at
            seen = seen.replace(tzinfo=timezone.utc) if seen.tzinfo is None else seen
            assert seen > stale and row.revoked_at is None
    finally:
        release_reads.set()
        if held:
            lock.release()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if engine is not None:
            event.remove(engine, "before_cursor_execute", observe_update)


@pytest.mark.asyncio
async def test_composed_auth_waits_before_writer_and_body(original_auth_composition, monkeypatch):
    from src.runtime_plugins import ownership
    from src.workspace import accounting_witness

    case = original_auth_composition
    lock = auth._auth_touch_lock()
    waiting = asyncio.Event()
    body_started = asyncio.Event()
    writer_started = asyncio.Event()
    probes = []
    original_prepare = accounting_witness.prepare_composition_read_session
    original_acquire = lock.acquire
    original_find = auth._find_token_record
    original_writer = ownership.begin_native_writer

    async def observe_prepare(db, *, header_budget=None):
        result = await original_prepare(db, header_budget=header_budget)
        assert result is db.info["composition_read_guard"]
        assert header_budget is not None
        probes.append(header_budget)
        return result

    async def observe_acquire():
        waiting.set()
        return await original_acquire()

    async def observe_find(*args, **kwargs):
        body_started.set()
        return await original_find(*args, **kwargs)

    async def observe_writer(*args, **kwargs):
        writer_started.set()
        assert kwargs["owner"] == "finite_service"
        assert kwargs["header_budget"] is probes[0]
        return await original_writer(*args, **kwargs)

    monkeypatch.setattr(accounting_witness, "prepare_composition_read_session", observe_prepare)
    monkeypatch.setattr(auth, "_find_token_record", observe_find)
    monkeypatch.setattr(ownership, "begin_native_writer", observe_writer)
    await lock.acquire()
    held = True
    monkeypatch.setattr(lock, "acquire", observe_acquire)
    task = asyncio.create_task(auth.authenticate_token(case.token, touch=False))
    try:
        await asyncio.wait_for(waiting.wait(), 3)
        assert len(probes) == 1
        assert not body_started.is_set() and not writer_started.is_set()
        assert not task.done()
        lock.release()
        held = False
        result = await asyncio.wait_for(task, 3)
        assert result.session_id == case.operator.session_id
        assert body_started.is_set() and writer_started.is_set()
    finally:
        if held:
            lock.release()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
