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
    install_session_factory,
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


async def test_a_connection_after_teardown_is_reported_against_the_finished_test(monkeypatch):
    from tests.support import db as support_db

    monkeypatch.setattr(support_db, "_finished_test", ["tests/test_probe.py::test_finished"])
    with pytest.raises(CanonicalEngineReached):
        async with CANONICAL_ENGINE.connect():
            pass
    report = support_db.take_late_canonical_engine_violations()
    assert report is not None
    assert report.startswith("1 connection(s) to the canonical database engine after "
                             "tests/test_probe.py::test_finished finished its teardown")
    assert "test_db_seam.py" in report
    # Taken once: the next check has nothing to report.
    assert support_db.take_late_canonical_engine_violations() is None
    assert take_canonical_engine_violations() == []


async def test_an_injected_session_factory_still_runs_the_production_scope(db, monkeypatch):
    opened = []

    def failing_commit_factory():
        session = db.factory()
        opened.append(session)

        async def refuse_commit():
            raise RuntimeError("injected commit failure")

        session.commit = refuse_commit
        return session

    install_session_factory(monkeypatch, failing_commit_factory)
    with pytest.raises(RuntimeError, match="injected commit failure"):
        async with db_engine.get_session() as session:
            session.add(AuditEvent(event_type="probe", summary="never committed"))
    assert len(opened) == 1
    async with db() as session:
        assert (await session.execute(select(AuditEvent))).scalars().all() == []
    # The state gate still applies to the injected factory.
    db_engine.set_state_gate("failed", "seam_gate_probe")
    try:
        with pytest.raises(db_engine.StateProviderUnavailable):
            async with db_engine.get_session():
                pass
    finally:
        db_engine.set_state_gate(None)
    assert len(opened) == 1


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


def _rollback_recording_factory(db, rollbacks, *, fail_rollback=False):
    def factory():
        session = db.factory()
        real_rollback = session.rollback

        async def recording_rollback():
            rollbacks.append(session)
            if fail_rollback:
                await real_rollback()
                raise RuntimeError("injected rollback failure")
            await real_rollback()

        session.rollback = recording_rollback
        return session
    return factory


async def _cancel_after_a_write(event_type):
    """Run a write inside ``get_session``, cancel it mid-unit, return the task."""
    written = asyncio.Event()
    never = asyncio.Event()

    async def unit_of_work():
        async with db_engine.get_session() as session:
            session.add(AuditEvent(event_type=event_type, summary="cancelled"))
            await session.flush()
            session.info["work_board_events_after_commit"] = ["cancelled-event"]
            written.set()
            await never.wait()

    task = asyncio.create_task(unit_of_work())
    await written.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return task


@pytest.mark.parametrize("fail_rollback", [False, True], ids=["rollback", "rollback_fails"])
async def test_session_scope_rolls_back_explicitly_on_cancellation(db, monkeypatch, fail_rollback):
    import src.work_board.events as events

    published: list[list[str]] = []

    async def record(pending):
        published.append(list(pending))
    monkeypatch.setattr(events, "publish_work_board_events", record)
    rollbacks: list = []
    install_session_factory(
        monkeypatch, _rollback_recording_factory(db, rollbacks, fail_rollback=fail_rollback))

    task = await _cancel_after_a_write("cancelled_write")

    # CancelledError reached the caller unchanged, even when rollback failed.
    assert task.cancelled()
    assert len(rollbacks) == 1
    assert "work_board_events_after_commit" not in rollbacks[0].info
    assert published == []
    async with db() as session:
        rows = (await session.execute(
            select(AuditEvent).where(AuditEvent.event_type == "cancelled_write"))).scalars().all()
    assert rows == []


async def _rollback_reached(rollback_entered, task):
    """Wait until the rollback stub is entered, or fail at once if the task ends first."""
    waiter = asyncio.create_task(rollback_entered.wait())
    await asyncio.wait({waiter, task}, return_when=asyncio.FIRST_COMPLETED)
    if not waiter.done():
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
    assert rollback_entered.is_set(), "the unit of work ended without rolling back"


async def test_a_second_cancel_during_the_cancellation_rollback_keeps_the_cancellation(db, monkeypatch):
    rollback_entered = asyncio.Event()
    never = asyncio.Event()
    rollbacks: list = []

    def parking_factory():
        session = db.factory()
        real_rollback = session.rollback

        async def parking_rollback():
            rollbacks.append(session)
            rollback_entered.set()
            await never.wait()  # interrupted by the second cancel
            await real_rollback()

        session.rollback = parking_rollback
        return session

    install_session_factory(monkeypatch, parking_factory)
    written = asyncio.Event()

    async def unit_of_work():
        async with db_engine.get_session() as session:
            session.add(AuditEvent(event_type="cancelled_twice", summary="cancelled"))
            await session.flush()
            written.set()
            await never.wait()

    task = asyncio.create_task(unit_of_work())
    await written.wait()
    task.cancel()
    await _rollback_reached(rollback_entered, task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled() and len(rollbacks) == 1
    # The session close still discarded the transaction.
    async with db() as session:
        rows = (await session.execute(
            select(AuditEvent).where(AuditEvent.event_type == "cancelled_twice"))).scalars().all()
    assert rows == []


async def test_a_cancel_during_a_non_cancellation_rollback_propagates(db, monkeypatch):
    """Only a cancellation absorbs a second cancel; for KeyboardInterrupt it is new."""
    rollback_entered = asyncio.Event()
    never = asyncio.Event()

    def parking_factory():
        session = db.factory()

        async def parking_rollback():
            rollback_entered.set()
            await never.wait()

        session.rollback = parking_rollback
        return session

    install_session_factory(monkeypatch, parking_factory)

    async def unit_of_work():
        try:
            async with db_engine.get_session():
                raise KeyboardInterrupt
        except KeyboardInterrupt:
            return "interrupt was swallowed"
        except asyncio.CancelledError:
            return "cancel propagated"

    task = asyncio.create_task(unit_of_work())
    await _rollback_reached(rollback_entered, task)
    task.cancel()
    assert await task == "cancel propagated"


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
