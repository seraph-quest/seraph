"""The single database seam for tests (#1222) and the engine factory it shares
with production."""
import asyncio
import threading

import pytest
from sqlalchemy import event, select, text

import src.db.engine as db_engine
from src.db.models import AuditEvent
from tests.support.db import (
    CANONICAL_ENGINE,
    CanonicalEngineReached,
    take_canonical_engine_violations,
)

_LISTENERS = (
    ("connect", db_engine._configure_sqlite_connection),
    ("checkout", db_engine._restore_sqlite_busy_timeout),
    ("before_cursor_execute", db_engine._note_write_start),
    ("commit", db_engine._report_write_hold),
    ("rollback", db_engine._report_write_hold),
    ("checkin", db_engine._forget_write_start),
)


def _configuration(database_engine):
    pool = database_engine.sync_engine.pool
    return {
        "pool": type(pool).__name__,
        "size": pool.size(),
        "max_overflow": pool._max_overflow,
        "timeout": pool.timeout(),
        "pre_ping": pool._pre_ping,
        "echo": database_engine.echo,
        "driver": database_engine.url.drivername,
        "listeners": [event.contains(database_engine.sync_engine, name, fn) for name, fn in _LISTENERS],
    }


async def test_canonical_engine_keeps_its_production_configuration(tmp_path):
    expected = {"pool": "AsyncAdaptedQueuePool", "size": 20, "max_overflow": 0, "timeout": 3,
                "pre_ping": True, "echo": db_engine.settings.database_echo,
                "driver": "sqlite+aiosqlite", "listeners": [True] * len(_LISTENERS)}
    assert _configuration(CANONICAL_ENGINE) == expected
    assert CANONICAL_ENGINE.url.database == db_engine._db_path
    # A test database is built by the same factory, so it is configured the same.
    other = db_engine.create_database_engine(f"sqlite+aiosqlite:///{tmp_path / 'other.sqlite3'}")
    try:
        assert _configuration(other) == expected
    finally:
        await other.dispose()


async def test_db_fixture_has_production_pragmas_on_a_file(db):
    assert db.path.is_file()
    assert db_engine.engine is db.engine
    async with db() as session:
        pragmas = {name: (await session.execute(text(f"PRAGMA {name}"))).scalar_one()
                   for name in ("journal_mode", "busy_timeout", "foreign_keys")}
    assert pragmas == {"journal_mode": "wal", "busy_timeout": db_engine._SQLITE_BUSY_TIMEOUT_MS,
                       "foreign_keys": 1}


async def test_every_get_session_caller_reaches_the_test_database(db):
    from src.api import mail
    from src.audit.repository import audit_repository

    await audit_repository.log_event(event_type="seam_probe", summary="seam probe")
    async with mail.get_session() as session:  # a module that was never on a patch list
        seen = (await session.execute(select(AuditEvent.event_type).where(
            AuditEvent.event_type == "seam_probe"))).scalars().all()
    assert seen == ["seam_probe"]

    # A thread without this test's context reaches it too.
    async def count() -> int:
        async with db_engine.get_session() as session:
            return len((await session.execute(select(AuditEvent.id))).all())
    result: list[int] = []
    worker = threading.Thread(target=lambda: result.append(asyncio.run(count())))
    worker.start()
    worker.join()
    assert result == [1]


async def test_get_session_keeps_the_state_gate_on_the_test_database(db):
    db_engine.set_state_gate("failed", "seam_gate_probe")
    try:
        with pytest.raises(db_engine.StateProviderUnavailable):
            async with db_engine.get_session():
                pass
        # The test's own handle is a named bypass for seeding and readback.
        async with db() as session:
            assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        db_engine.set_state_gate(None)


async def test_concurrent_sessions_have_their_own_connections(db):
    """#1197 / #1227 item 4: one session's rollback must not discard another's
    flushed rows, as it did on the old single shared StaticPool connection."""
    async with db() as kept:
        kept.add(AuditEvent(event_type="kept", summary="kept"))
        await kept.flush()
        with pytest.raises(RuntimeError):
            async with db_engine.get_session() as other:
                # Its own connection: the uncommitted row is not visible here.
                assert (await other.execute(select(AuditEvent.id))).all() == []
                raise RuntimeError("roll this session back")
    async with db() as session:
        types = (await session.execute(select(AuditEvent.event_type))).scalars().all()
    assert types == ["kept"]


async def test_sentinel_refuses_and_records_a_canonical_connection():
    with pytest.raises(CanonicalEngineReached):
        async with CANONICAL_ENGINE.connect():
            pass
    violations = take_canonical_engine_violations()  # taken, so this test does not fail
    assert len(violations) == 1
    assert "test_db_seam.py" in violations[0]
