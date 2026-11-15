"""Shared fail-fast waits for tests that coordinate a background task (#1156).

A test that waits on an ``asyncio.Event`` set inside a background task hangs
forever if the task finishes first (for example a precontact route denial or
an early exception). ``started_or_raise`` waits for whichever comes first and
surfaces the task's own outcome instead of hanging.
"""
from __future__ import annotations

import asyncio


async def started_or_raise(started: asyncio.Event, task: asyncio.Future) -> None:
    """Wait for a callback inside ``task`` to set ``started``, failing fast if
    ``task`` ends first.

    A finished task re-raises its own exception; a task that returned normally
    without setting ``started`` fails with an explicit assertion.
    """
    waiter = asyncio.ensure_future(started.wait())
    try:
        await asyncio.wait({waiter, task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
    if not started.is_set():
        task.result()
        raise AssertionError("background task finished before its callback started")
