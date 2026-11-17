"""The single database seam for tests (#1222, #1217 slice 5).

``db`` gives each test its own SQLite file database, built by the production
``create_database_engine`` (same pool, foreign keys, WAL and busy timeout) and
installed as the process default in ``src.db.engine``. Every ``get_session``
caller, whatever module it lives in and whichever task or thread it runs on,
then reaches the test database through the real ``get_session``: the state
provider gate and post-commit work-board publication included. There is no
list of import sites to keep in step with ``src``.

A test that needs a failing or hand-written session installs it with
``install_session_factory`` or ``install_fake_session`` (#1241) rather than
patching ``get_session``, so the real gate and ``session_scope`` still run.

``override_session_factory`` keeps its meaning: a task-scoped, named bypass of
the default database. A test that runs code against a second database enters
it around that code; it is not the routing for the default database because
it bypasses the state gate and does not reach other threads.

A sentinel on the canonical engine (the one ``src.db.engine`` builds at import
for the session workspace) refuses every connection. A test that reaches it
fails, even when the code under test swallows the refusal; a connection made
after a test's teardown fails the next test's setup (or the run, after the
last test) and names the test that finished before it.

Schemas are created once per process in a template file and copied per test,
so a test pays for a file copy instead of ``create_all``. The copy is deleted
at teardown.
"""
from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import greenlet
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

import src.db.engine as db_engine

# The engine ``src.db.engine`` built at import, for the session workspace.
# Captured before any test can install another default.
CANONICAL_ENGINE: AsyncEngine = db_engine.engine


class CanonicalEngineReached(RuntimeError):
    """A test reached the canonical engine instead of its own database."""


_violations: list[str] = []


_BACKEND_ROOT = str(Path(__file__).resolve().parents[2]) + "/"


def _caller_frames(limit: int = 8) -> str:
    """The src and tests frames that led to the connection, innermost first.

    Async callers reach the pool inside a greenlet; walk the whole greenlet
    chain back to the caller.
    """
    frames = []
    try:
        current = greenlet.getcurrent()
        while current is not None:
            frame = sys._getframe(1) if current is greenlet.getcurrent() else current.gr_frame
            while frame is not None:
                name = frame.f_code.co_filename
                if (name.startswith(_BACKEND_ROOT) and "site-packages" not in name
                        and not name.endswith(("tests/support/db.py", "src/db/engine.py"))):
                    frames.append(f"{name[len(_BACKEND_ROOT):]}:{frame.f_lineno}:{frame.f_code.co_name}")
                frame = frame.f_back
            current = current.parent
    except Exception as exc:  # diagnostics must never break the sentinel
        frames.append(f"<call chain unavailable: {type(exc).__name__}>")
    return " <- ".join(frames[:limit]) or "unknown"


def _refuse_canonical_connect(_dialect, _connection_record, _cargs, _cparams):
    _violations.append(_caller_frames())
    raise CanonicalEngineReached(
        "test reached the canonical database engine; request the `db` fixture "
        "(tests/support/db.py) so get_session uses the test's own database")


def arm_canonical_engine_sentinel() -> None:
    if not event.contains(CANONICAL_ENGINE.sync_engine, "do_connect", _refuse_canonical_connect):
        event.listen(CANONICAL_ENGINE.sync_engine, "do_connect", _refuse_canonical_connect)


def take_canonical_engine_violations() -> list[str]:
    """Return and clear the connection attempts the sentinel refused."""
    taken = list(_violations)
    _violations.clear()
    return taken


# The last test whose teardown finished, so a connection made after it (by
# work it left running) is reported against it rather than dropped (#1241).
_finished_test: list[str | None] = [None]


def note_test_finished(nodeid: str) -> None:
    _finished_test[0] = nodeid


def take_late_canonical_engine_violations() -> str | None:
    """Describe the refused connections made since the last test finished.

    The conftest calls this before each test's setup and at session end, so a
    canonical-engine connection made after a test's teardown fails the run
    instead of being cleared silently. Returns ``None`` when there were none.
    """
    violations = take_canonical_engine_violations()
    if not violations:
        return None
    after = _finished_test[0]
    when = f"after {after} finished its teardown" if after else "before the first test"
    return (f"{len(violations)} connection(s) to the canonical database engine {when}; work a test "
            "left running reached it. Request the `db` fixture and stop or drain that work. "
            "First attempt:\n" + violations[0])


@dataclass
class TestDatabase:
    """A test's own database. Calling it opens a session, like ``get_session``.

    The handle bypasses the state gate, so a test can read and seed its
    database whatever the gate state; code under test goes through the real
    ``get_session``.
    """

    __test__ = False  # not a pytest test class

    engine: AsyncEngine
    path: Path
    factory: Callable[[], AsyncSession] = field(init=False)

    def __post_init__(self) -> None:
        self.factory = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    def __call__(self):
        return db_engine.session_scope(self.factory)


@dataclass
class _Template:
    path: Path
    tables: frozenset[str]


_template: _Template | None = None


async def _build_template(directory: Path) -> _Template:
    path = directory / "schema-template.sqlite3"
    template_engine = db_engine.create_database_engine(f"sqlite+aiosqlite:///{path}")
    try:
        async with template_engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
            await db_engine._ensure_search_indexes(connection)
        async with template_engine.connect() as connection:
            await connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        await template_engine.dispose()
    return _Template(path=path, tables=frozenset(SQLModel.metadata.tables))


async def create_test_database(path: Path, *, template_directory: Path) -> TestDatabase:
    """Create a production-configured file database with the full schema at ``path``."""
    global _template
    if (_template is None or not _template.path.exists()
            or set(SQLModel.metadata.tables) - _template.tables):
        # First use, or a table registered after the template was built:
        # (re)build it so late tables get the same schema and search indexes
        # production builds. ``create_all`` and the index DDL only add what
        # is missing.
        _template = await _build_template(template_directory)
    shutil.copyfile(_template.path, path)
    return TestDatabase(engine=db_engine.create_database_engine(f"sqlite+aiosqlite:///{path}"), path=path)


def remove_test_database(path: Path) -> None:
    """Delete a test database file and its WAL and shared-memory siblings."""
    for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        candidate.unlink(missing_ok=True)


def install_database(monkeypatch, database_engine: AsyncEngine) -> Callable[[], AsyncSession]:
    """Make ``database_engine`` the default database for the rest of the test.

    Every ``get_session`` caller then uses it through the production path,
    starting with an open state gate. Returns the session factory.
    ``monkeypatch`` restores the canonical default and the gate at teardown.
    """
    factory = sessionmaker(database_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_engine, "engine", database_engine)
    monkeypatch.setattr(db_engine, "async_session_factory", factory)
    # Every session now passes the process-global state gate. Start the test
    # with no provider worker attached and restore the previous gate after
    # it, so a gate a test leaves closed cannot fail later tests.
    monkeypatch.setattr(db_engine, "_STATE_GATE", [None, None])
    return factory


def install_session_factory(monkeypatch, factory: Callable[[], AsyncSession]) -> None:
    """Make every default ``get_session`` open its session from ``factory``.

    For a test that injects a fake or failing session (#1241). The code under
    test still calls the real ``get_session``, so the state gate and
    ``session_scope`` (commit on success, rollback on error, post-commit
    publication) run as in production; only the session it opens is the
    test's. ``factory()`` must return an async context manager whose session
    offers ``commit``, ``rollback`` and an ``info`` dict. ``monkeypatch``
    restores the previous factory at teardown. Do not patch ``get_session``.

    The factory serves every default ``get_session`` caller, request
    authentication included, not one module. A failure meant for one unit of
    work must check which session it is in (for example by the rows it is
    about to commit) and assert that it fired, or the test can fail early,
    for the wrong reason, and still pass.
    """
    monkeypatch.setattr(db_engine, "async_session_factory", factory)


class _FakeSessionContext:
    """Open a test's fake session the way a session factory opens a real one."""

    def __init__(self, session) -> None:
        self.session = session

    async def __aenter__(self):
        session = self.session
        # session_scope needs these; a fake that does not model them gets inert ones.
        if not hasattr(session, "info"):
            session.info = {}
        for name in ("commit", "rollback"):
            if not hasattr(session, name):
                setattr(session, name, _inert)
        return session

    async def __aexit__(self, *_exc) -> bool:
        return False


async def _inert() -> None:
    return None


def install_fake_session(monkeypatch, make_session: Callable[[], object]) -> None:
    """Make every default ``get_session`` yield ``make_session()`` (#1241).

    For a test that drives code against a hand-written fake session (scripted
    results, injected lock errors). The real ``get_session`` and
    ``session_scope`` still run around it: the fake's ``commit`` runs when the
    unit of work succeeds and its ``rollback`` when it raises. A fake without
    ``commit``, ``rollback`` or ``info`` gets inert ones. A ``MagicMock`` has
    every attribute, so give it an awaitable ``commit``/``rollback`` and a real
    ``info`` dict; an ``object()`` cannot take attributes, so use a class or
    ``SimpleNamespace``.
    """
    install_session_factory(monkeypatch, lambda: _FakeSessionContext(make_session()))
