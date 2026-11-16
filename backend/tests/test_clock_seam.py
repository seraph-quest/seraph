"""The clock seam (#1225): SystemClock is the real clock; FakeClock moves only on ``advance``."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

from src.utils import clock as clock_module
from src.utils.clock import Clock, FakeClock, SystemClock, get_clock
from tests.support.clock import advance_until_done, fake_clock, timers_pending  # noqa: F401
from tests.support.sync import assert_blocked, settle, suspended_in, within


def test_system_clock_is_the_default_and_delegates_to_the_calls_it_replaces(monkeypatch):
    assert isinstance(get_clock(), SystemClock) and isinstance(get_clock(), Clock)
    clock = SystemClock()
    # Each method is the exact call it replaced: patch the underlying call and observe it.
    monkeypatch.setattr(time, "time", lambda: 111.0)
    monkeypatch.setattr(time, "monotonic", lambda: 222.0)
    monkeypatch.setattr(time, "perf_counter", lambda: 333.0)
    assert (clock.time(), clock.monotonic(), clock.perf_counter()) == (111.0, 222.0, 333.0)
    assert clock.now(timezone.utc).tzinfo is timezone.utc and clock.now().tzinfo is None


async def test_system_clock_timers_are_the_asyncio_primitives(monkeypatch):
    seen = []

    async def wait_for(aw, timeout):
        seen.append(("wait_for", timeout))
        return await aw

    async def sleep(delay, result=None):
        seen.append(("sleep", delay))
        return result

    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    clock = SystemClock()

    async def answer():
        return 42

    assert await clock.wait_for(answer(), 5) == 42
    assert await clock.sleep(7, "r") == "r"
    assert seen == [("wait_for", 5), ("sleep", 7)]
    assert isinstance(clock.timeout(1.0), asyncio.Timeout)


def test_fake_clock_reads_move_together_and_only_on_advance():
    start = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    clock = FakeClock(start=start, monotonic_start=50.0)
    assert clock.now(timezone.utc) == start and clock.time() == start.timestamp()
    assert clock.monotonic() == clock.perf_counter() == 50.0
    clock.advance(1.5)
    assert clock.monotonic() == 51.5 and clock.time() == start.timestamp() + 1.5 and clock.elapsed == 1.5
    with pytest.raises(ValueError):
        clock.advance(-1)
    with pytest.raises(ValueError):
        FakeClock(start=datetime(2026, 1, 1))


async def test_fake_sleep_wakes_only_when_advanced_to_its_deadline(fake_clock):
    sleeper = asyncio.ensure_future(get_clock().sleep(2.0, "woke"))
    assert await timers_pending(fake_clock) == [2.0]
    fake_clock.advance(1.5)
    await assert_blocked(sleeper, at=lambda: suspended_in(sleeper, "FakeClock.sleep"), what="sleep before deadline")
    fake_clock.advance(0.5)
    assert await within(sleeper, what="sleep at its deadline") == "woke"
    assert fake_clock.pending_deadlines() == []


async def test_fake_timeout_and_wait_for_raise_timeout_error_at_the_fake_deadline(fake_clock):
    async def forever():
        await asyncio.Event().wait()

    waiting = asyncio.ensure_future(get_clock().wait_for(forever(), 3.0))
    await timers_pending(fake_clock)
    fake_clock.advance(3.0)
    with pytest.raises(TimeoutError):
        await within(waiting, what="wait_for timing out")

    async def scoped():
        async with get_clock().timeout(1.0) as scope:
            await asyncio.Event().wait()
        return scope

    timed = asyncio.ensure_future(scoped())
    await timers_pending(fake_clock)
    fake_clock.advance(1.0)
    with pytest.raises(TimeoutError):
        await within(timed, what="timeout scope expiring")

    async def answer():
        return "done"

    # A result before the deadline wins and leaves no timer behind.
    assert await get_clock().wait_for(answer(), 1.0) == "done"
    assert await get_clock().wait_for(answer(), None) == "done"
    assert fake_clock.pending_deadlines() == []


async def test_fake_wait_returns_on_completion_or_fake_timeout(fake_clock):
    gate = asyncio.Event()
    quick = asyncio.ensure_future(gate.wait())
    stuck = asyncio.ensure_future(asyncio.Event().wait())
    waiting = asyncio.ensure_future(get_clock().wait([quick, stuck], timeout=0.5))
    await timers_pending(fake_clock)
    gate.set()
    await settle()
    assert not waiting.done()  # ALL_COMPLETED: the stuck future still holds it
    assert await advance_until_done(fake_clock, waiting, what="wait timing out") == 0.5
    done, pending = waiting.result()
    assert done == {quick} and pending == {stuck}
    stuck.cancel()
    first = await get_clock().wait([asyncio.ensure_future(asyncio.sleep(0))], timeout=5,
                                   return_when=asyncio.FIRST_COMPLETED)
    assert len(first[0]) == 1 and fake_clock.pending_deadlines() == []
    with pytest.raises(ValueError):
        await get_clock().wait([], timeout=1)


def test_fake_clock_fixture_restores_the_previous_clock():
    assert isinstance(clock_module.get_clock(), SystemClock)
