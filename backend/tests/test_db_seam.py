"""The single database seam for tests (#1222) and the engine factory it shares
with production."""
import asyncio
import threading

import pytest
from sqlalchemy import event, select, text

import src.db.engine as db_engine
from src.db.models import AuditEvent
from sqlmodel import SQLModel

from tests.support.db import (
    CANONICAL_ENGINE,
    CanonicalEngineReached,
    create_test_database,
    install_database,
    remove_test_database,
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
        # Every listener in registration order, SQLAlchemy's own included.
        "pool_events": {name: [_name(fn) for fn in getattr(pool.dispatch, name)]
                        for name in ("connect", "checkout", "checkin")},
        "engine_events": {name: [_name(fn) for fn in getattr(database_engine.sync_engine.dispatch, name)]
                          for name in ("before_cursor_execute", "commit", "rollback")},
    }


def _name(listener) -> str:
    return getattr(listener, "__qualname__", repr(listener))


async def test_canonical_engine_keeps_its_production_configuration(tmp_path):
    canonical = _configuration(CANONICAL_ENGINE)
    expected = {"pool": "AsyncAdaptedQueuePool", "size": 20, "max_overflow": 0, "timeout": 3,
                "pre_ping": True, "echo": db_engine.settings.database_echo,
                "driver": "sqlite+aiosqlite", "listeners": [True] * len(_LISTENERS),
                "pool_events": canonical["pool_events"], "engine_events": canonical["engine_events"]}
    assert canonical == expected
    # Seraph's own listeners, in the order production registered them.
    assert [fn for fn in canonical["pool_events"]["connect"] if fn.startswith("_")] == [
        "_configure_sqlite_connection"]
    assert canonical["pool_events"]["checkout"] == ["_restore_sqlite_busy_timeout"]
    assert canonical["pool_events"]["checkin"] == ["_forget_write_start"]
    assert canonical["engine_events"]["commit"] == canonical["engine_events"]["rollback"] == [
        "_report_write_hold"]
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


async def test_session_scope_publishes_work_board_events_only_after_commit(db, monkeypatch):
    import src.work_board.events as events

    published: list[list[str]] = []

    async def record(pending):
        published.append(list(pending))
    monkeypatch.setattr(events, "publish_work_board_events", record)

    async with db_engine.get_session() as session:
        session.add(AuditEvent(event_type="committed", summary="committed"))
        session.info["work_board_events_after_commit"] = ["after-commit"]
    assert published == [["after-commit"]]

    with pytest.raises(RuntimeError):
        async with db_engine.get_session() as session:
            session.info["work_board_events_after_commit"] = ["rolled-back"]
            raise RuntimeError("roll back")
    assert published == [["after-commit"]]


async def test_each_test_database_starts_with_an_open_state_gate_and_restores_it(db, tmp_path):
    assert db_engine.state_gate() == (None, None)
    # Install a second database by hand, leave its gate closed, and undo it
    # as the fixture's teardown does: the previous gate comes back.
    outer_gate = db_engine._STATE_GATE
    other = db_engine.create_database_engine(f"sqlite+aiosqlite:///{tmp_path / 'other.sqlite3'}")
    patcher = pytest.MonkeyPatch()
    try:
        install_database(patcher, other)
        assert db_engine.state_gate() == (None, None)
        db_engine.set_state_gate("failed", "leaked_by_a_test")
        assert db_engine.state_gate() == ("failed", "leaked_by_a_test")
    finally:
        patcher.undo()
        await other.dispose()
    assert db_engine._STATE_GATE is outer_gate
    assert db_engine.state_gate() == (None, None)
    assert db_engine.engine is db.engine


async def test_late_tables_get_the_template_schema_and_indexes(db, tmp_path, _db_template_directory):
    from sqlalchemy import Column, Integer, Table

    late = Table("zz_late_registered_probe", SQLModel.metadata, Column("id", Integer, primary_key=True))
    try:
        database = await create_test_database(tmp_path / "late.sqlite3", template_directory=_db_template_directory)
        try:
            async with database.engine.connect() as connection:
                names = {row[0] for row in (await connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master")).all()}
        finally:
            await database.engine.dispose()
            remove_test_database(database.path)
    finally:
        SQLModel.metadata.remove(late)
    assert {"zz_late_registered_probe", "session_recall_fts"} <= names
    # The template itself was rebuilt, so the search-index DDL ran with the
    # late table registered, as production's init_db does.
    from tests.support import db as support_db
    assert "zz_late_registered_probe" in support_db._template.tables
    assert not database.path.exists()
    assert not database.path.with_name(database.path.name + "-wal").exists()
