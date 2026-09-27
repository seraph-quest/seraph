"""Fire-and-forget background task tracking with error logging."""

import asyncio
import logging
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Coroutine

logger = logging.getLogger(__name__)

_tasks: set[asyncio.Task] = set()
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
