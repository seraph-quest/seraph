"""Observe SQLite writer-lock attempts and the busy wait they ran under (#1074, #1221).

A read path that meets a foreign writer must not queue behind it. The proof is
causal, not a stopwatch: the foreign writer holds its lock until the read has
returned, and every write the read attempts in that window must run under a
bounded lock wait (``bound_lock_wait`` in ``src/db/engine.py``), never the
standard busy timeout.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager

import greenlet
from sqlalchemy import event

_BOUNDED_WAIT_CAP_MS = 250
_WRITE_LOCK_WORDS = {"INSERT", "UPDATE", "DELETE", "REPLACE"}


def _takes_writer_lock(statement: str) -> bool:
    words = statement.split()
    if not words:
        return False
    head = words[0].upper()
    return head in _WRITE_LOCK_WORDS or (
        head == "BEGIN" and len(words) > 1 and words[1].upper() in {"IMMEDIATE", "EXCLUSIVE"})


def greenlet_frames():
    stack = []
    g = greenlet.getcurrent()
    while g is not None:
        f = g.gr_frame if g is not greenlet.getcurrent() else sys._getframe()
        while f is not None:
            stack.append(f); f = f.f_back
        g = g.parent
    return stack


# A read-path audit that met the busy writer is retried once at the normal wait
# as its own detached task or worker (src/audit/runtime.py); that retry never
# holds the read. It is recognised by a stack that holds the retry and nothing
# but the audit owner: the same retry awaited inline by a read path would show
# that read path's frames above it and is counted.
_DEFERRED_RETRY = "_retry_deferred_integration_event"


def _is_detached_audit_retry() -> bool:
    frames = [frame for frame in greenlet_frames()
              if "/backend/src/" in frame.f_code.co_filename and "/db/engine" not in frame.f_code.co_filename]
    return (any(frame.f_code.co_name == _DEFERRED_RETRY for frame in frames)
            and all("/backend/src/audit/" in frame.f_code.co_filename for frame in frames))


def assert_only_bounded_writes(writes, statements=None):
    """Every writer-lock attempt during contention ran under a bounded busy wait."""
    unbounded = [(statement, wait) for statement, wait in writes
                 if wait is None or wait > _BOUNDED_WAIT_CAP_MS]
    assert not unbounded, (unbounded, statements)


@contextmanager
def bounded_write_watch(engine):
    """Record every writer-lock attempt on ``engine`` with the busy wait in force.

    Each entry is ``(statement head, bounded wait in ms)``; ``None`` means the
    connection ran at the standard busy timeout. Pool checkout restores the
    standard wait (``src/db/engine.py``), so the recorded wait is cleared there.
    A deferred audit retry on its own detached thread is not on the read path.
    """
    writes: list[tuple[str, int | None]] = []
    bounded: dict[int, int] = {}

    def track_wait(conn, _cursor, statement, *_args):
        dbapi = id(conn.connection.dbapi_connection)
        normalized = statement.strip().lower().replace(" ", "")
        if normalized.startswith("pragmabusy_timeout="):
            bounded[dbapi] = int(normalized.split("=", 1)[1])
        elif _takes_writer_lock(statement):
            if _is_detached_audit_retry():
                return
            writes.append((" ".join(statement.split()[:3]).upper(), bounded.get(dbapi)))

    def restore_on_checkout(dbapi_connection, _record, _proxy):
        bounded.pop(id(dbapi_connection), None)

    event.listen(engine, "before_cursor_execute", track_wait)
    event.listen(engine, "checkout", restore_on_checkout)
    try:
        yield writes
    finally:
        event.remove(engine, "before_cursor_execute", track_wait)
        event.remove(engine, "checkout", restore_on_checkout)


__all__ = ["assert_only_bounded_writes", "bounded_write_watch", "greenlet_frames"]
