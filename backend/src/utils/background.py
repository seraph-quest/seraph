"""Fire-and-forget background task and thread tracking with error logging."""

import asyncio
import logging
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Coroutine

logger = logging.getLogger(__name__)

_tasks: set[asyncio.Task] = set()
_threads: set[threading.Thread] = set()
_threads_lock = threading.Lock()
_task_scope: ContextVar[set[asyncio.Task] | None] = ContextVar(
    "seraph_background_task_scope", default=None
)


@contextmanager
def background_task_scope():
    """Track and drain only the background tasks created in this context."""
    scoped_tasks: set[asyncio.Task] = set()
    token: Token[set[asyncio.Task] | None] = _task_scope.set(scoped_tasks)
    try:
        yield
    finally:
        _task_scope.reset(token)


def track_task(coro: Coroutine, name: str = "background") -> asyncio.Task:
    """Create a tracked background task with automatic cleanup and error logging."""
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)
    scoped_tasks = _task_scope.get()
    if scoped_tasks is not None:
        scoped_tasks.add(task)

    def _done(t: asyncio.Task):
        _tasks.discard(t)
        if scoped_tasks is not None:
            scoped_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            logger.error("Background task %r failed: %s", t.get_name(), exc, exc_info=exc)

    task.add_done_callback(_done)
    return task


async def drain_tracked_tasks(*, timeout_seconds: float | None = None) -> None:
    """Wait for tracked tasks on the current loop before tearing shared state down."""
    loop = asyncio.get_running_loop()
    scoped_tasks = _task_scope.get()
    tasks = _tasks if scoped_tasks is None else scoped_tasks

    while True:
        pending = [
            task for task in tuple(tasks)
            if task.get_loop() is loop and not task.done()
        ]
        if not pending:
            return

        done, still_pending = await asyncio.wait(pending, timeout=timeout_seconds)
        if not still_pending:
            continue

        logger.warning(
            "Cancelling %d tracked background task(s) after timeout during shutdown",
            len(still_pending),
        )
        for task in still_pending:
            task.cancel()
        _, stubborn = await asyncio.wait(still_pending, timeout=timeout_seconds)
        if stubborn:
            task_names = ", ".join(sorted(task.get_name() for task in stubborn))
            logger.error(
                "Tracked background task shutdown failed for: %s",
                task_names,
            )
            raise RuntimeError(
                "Tracked background tasks refused to stop during shutdown: "
                f"{task_names}"
            )
        return


def start_tracked_thread(target, *, name: str) -> threading.Thread:
    """Start a daemon thread that shutdown (and each test) can join.

    For background work that has to run on its own thread, such as a sync
    caller's audit write on a dedicated event loop. ``join_tracked_threads``
    waits for it, so it does not outlive the engine it writes to.
    """
    thread = threading.Thread(target=target, name=name, daemon=True)
    with _threads_lock:
        # Started under the lock, so a concurrent join or prune never sees
        # it registered but not yet alive.
        _threads.difference_update([item for item in _threads if not item.is_alive()])
        _threads.add(thread)
        thread.start()
    return thread


def join_tracked_threads(*, timeout_seconds: float | None = None) -> list[str]:
    """Join the threads started by ``start_tracked_thread``.

    Returns the names of any still running after ``timeout_seconds`` (a
    shutdown liveness bound; ``None`` waits for all). A thread cannot be
    cancelled, so those are only reported.
    """
    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    while True:
        with _threads_lock:
            alive = [thread for thread in _threads if thread.is_alive()]
            _threads.intersection_update(alive)
        if not alive:
            return []
        for thread in alive:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            thread.join(remaining)
        if deadline is not None and time.monotonic() >= deadline:
            with _threads_lock:
                stuck = sorted(thread.name for thread in _threads if thread.is_alive())
            if stuck:
                logger.warning("Tracked background thread(s) still running at shutdown: %s", ", ".join(stuck))
            return stuck
