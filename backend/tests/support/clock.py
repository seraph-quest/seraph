"""The test side of the clock seam (#1225, #1217 slice 8).

``fake_clock`` installs a ``FakeClock`` as the process clock for one test and
restores the previous clock at teardown. Production code that reads time
through ``src.utils.clock.get_clock()`` then sees time move only when the
test calls ``clock.advance(seconds)``.

A test synchronises on the code under test having reached its timer (a
signal from a fake it calls, or ``await timers_pending(clock)`` when no other
timer can be pending) before it advances, so the advance cannot race the code
that registers the deadline. Pending timers are process-wide. Request the fixture by importing
it into the test module::

    from tests.support.clock import fake_clock  # noqa: F401
"""
from __future__ import annotations

import asyncio
from typing import Iterator

import pytest

from src.utils import clock as clock_module
from src.utils.clock import FakeClock
from tests.support.sync import LIVENESS_SECONDS, settle, wait_until, within


# Every clock installed during the current test; the leak checks
# (tests/support/leaks.py) fail a test whose fake clock still has a pending
# timer after its loop closed, then clear this list.
installed_clocks: list[clock_module.Clock] = []


def install_clock(clock: clock_module.Clock) -> clock_module.Clock:
    """Install ``clock`` as the process clock; returns the previous one to restore."""
    installed_clocks.append(clock)
    return clock_module.set_clock(clock)


@pytest.fixture
def fake_clock() -> Iterator[FakeClock]:
    clock = FakeClock()
    previous = install_clock(clock)
    try:
        yield clock
    finally:
        clock_module.set_clock(previous)


async def timers_pending(clock: FakeClock, count: int = 1, *, what: str = "code under test parks on its timer",
                         bound: float = LIVENESS_SECONDS) -> list[float]:
    """Wait until ``count`` fake timers are pending and return their remaining seconds."""
    await within(clock.wait_for_timers(count), what=what, bound=bound)
    return clock.pending_deadlines()


async def advance_until_done(clock: FakeClock, task: asyncio.Future, *, what: str,
                             bound: float = LIVENESS_SECONDS) -> float:
    """Advance fake time timer by timer until ``task`` finishes; return the fake seconds advanced.

    Each step waits until the task is done or *some* fake timer is pending,
    then moves the clock exactly to the soonest deadline. Pending timers are
    process-wide, and a timer may be armed around real I/O still in flight
    (``wait_for(db_io, 5)``), so this does not prove the task is parked only
    on time. Use it where the remaining work is timer-bound, and assert the
    returned fake seconds exactly, so a premature advance fails loudly.
    """
    started = clock.elapsed
    while not task.done():
        await wait_until(lambda: task.done() or clock.pending_deadlines(), what=f"{what}: parked on a timer",
                         state=lambda: (clock, task), bound=bound)
        if task.done():
            break
        clock.advance(max(clock.pending_deadlines()[0], 0.0))
        await settle()
    return clock.elapsed - started
