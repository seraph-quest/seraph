"""The process clock seam (#1225, #1217 slice 8).

Production code that reads time or waits on a timer does it through
``get_clock()`` instead of calling ``time``, ``datetime.now`` or the asyncio
timer primitives directly. The default is ``SystemClock``, whose every method
*is* the call the site made before: ``time()`` is ``time.time``,
``monotonic()`` is ``time.monotonic``, ``now(tz)`` is ``datetime.now(tz)``,
``sleep`` / ``timeout`` / ``wait_for`` / ``wait`` are the asyncio functions of
the same name. Wall time and monotonic time stay distinct; a site keeps the
kind it read before. Nothing here adds a deadline, expiry or time-based gate.

Tests install ``FakeClock`` for the duration of one test (see
``tests/support/clock.py``). It moves only when the test calls ``advance``;
it never auto-advances, because much of the backend's work happens on worker
threads (aiosqlite, ``to_thread``) while the loop is idle, and an
auto-advancing clock would fire timeouts while real work is still in flight.

The clock is process-wide, like the database default in ``src.db.engine``,
so code on any task or thread reads the same clock. Importing this module
starts nothing.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import threading
import time as _time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Protocol, TypeVar, runtime_checkable

T = TypeVar("T")


@runtime_checkable
class Clock(Protocol):
    """Time as an input: reads and timers that a test can supply and advance."""

    def time(self) -> float:
        """Wall-clock seconds since the epoch (``time.time``)."""

    def monotonic(self) -> float:
        """Monotonic seconds for deadlines and intervals (``time.monotonic``)."""

    def perf_counter(self) -> float:
        """High-resolution seconds for measuring durations (``time.perf_counter``)."""

    def now(self, tz: tzinfo | None = None) -> datetime:
        """The current wall-clock datetime (``datetime.now(tz)``)."""

    async def sleep(self, delay: float, result: Any = None) -> Any:
        """Suspend for ``delay`` seconds (``asyncio.sleep``)."""

    def timeout(self, delay: float | None) -> Any:
        """An async context manager that times out its body (``asyncio.timeout``)."""

    async def wait_for(self, aw: Awaitable[T], timeout: float | None) -> T:
        """Await ``aw`` under ``timeout`` (``asyncio.wait_for``)."""

    async def wait(self, fs: Iterable[asyncio.Future], *, timeout: float | None = None,
                   return_when: str = asyncio.ALL_COMPLETED) -> tuple[set, set]:
        """Wait for futures (``asyncio.wait``)."""


class SystemClock:
    """The real clock. Each method is exactly the call it replaces."""

    __slots__ = ()

    def time(self) -> float:
        return _time.time()

    def monotonic(self) -> float:
        return _time.monotonic()

    def perf_counter(self) -> float:
        return _time.perf_counter()

    def now(self, tz: tzinfo | None = None) -> datetime:
        return datetime.now(tz)

    async def sleep(self, delay: float, result: Any = None) -> Any:
        return await asyncio.sleep(delay, result)

    def timeout(self, delay: float | None) -> asyncio.Timeout:
        return asyncio.timeout(delay)

    async def wait_for(self, aw: Awaitable[T], timeout: float | None) -> T:
        return await asyncio.wait_for(aw, timeout)

    async def wait(self, fs: Iterable[asyncio.Future], *, timeout: float | None = None,
                   return_when: str = asyncio.ALL_COMPLETED) -> tuple[set, set]:
        return await asyncio.wait(fs, timeout=timeout, return_when=return_when)

    def __repr__(self) -> str:
        return "SystemClock()"


@dataclass(order=True)
class _Timer:
    deadline: float  # in elapsed seconds, the space ``advance`` moves, so no rounding drift
    sequence: int
    loop: asyncio.AbstractEventLoop = field(compare=False)
    callback: Callable[[], None] = field(compare=False)
    cancelled: bool = field(default=False, compare=False)


class FakeClock:
    """A clock that moves only when the test calls ``advance``.

    Wall time starts at ``start`` (default: the real wall time at
    construction, so rows written by code outside the seam stay comparable)
    and monotonic time at ``monotonic_start``; both move together. Timers
    (``sleep``, ``timeout``, ``wait_for``, ``wait``) fire when ``advance``
    moves the clock to or past their deadline, in deadline order. Reads are
    thread-safe; timers belong to the loop that created them.
    """

    def __init__(self, *, start: datetime | float | None = None, monotonic_start: float = 10_000.0) -> None:
        if isinstance(start, datetime):
            if start.tzinfo is None:
                raise ValueError("FakeClock start must be timezone-aware")
            wall = start.timestamp()
        else:
            wall = _time.time() if start is None else float(start)
        self._wall_start = wall
        self._monotonic_start = float(monotonic_start)
        self._elapsed = 0.0
        self._lock = threading.Lock()
        self._timers: list[_Timer] = []
        self._sequence = itertools.count()
        self._watchers: list[tuple[asyncio.AbstractEventLoop, asyncio.Future]] = []

    def __repr__(self) -> str:
        return f"<FakeClock elapsed={self._elapsed:g}s timers={self.pending_deadlines()}>"

    # -- reads ---------------------------------------------------------------------
    def time(self) -> float:
        with self._lock:
            return self._wall_start + self._elapsed

    def monotonic(self) -> float:
        with self._lock:
            return self._monotonic_start + self._elapsed

    def perf_counter(self) -> float:
        return self.monotonic()

    def now(self, tz: tzinfo | None = None) -> datetime:
        return datetime.fromtimestamp(self.time(), tz)

    @property
    def elapsed(self) -> float:
        """Seconds advanced since construction."""
        with self._lock:
            return self._elapsed

    # -- control -------------------------------------------------------------------
    def advance(self, seconds: float) -> None:
        """Move the clock forward and fire every timer now due, in deadline order.

        Fired callbacks run on their own loop (scheduled thread-safely), so a
        test awaits the woken code afterwards, for example with ``settle()``
        or by awaiting the task. One call is one jump: code woken by a fired
        timer arms its next timer from the new time, so a chain of timers is
        stepped one advance at a time (``advance_until_done`` in
        ``tests/support/clock.py``).
        """
        if seconds < 0:
            raise ValueError("a clock never moves backwards")
        due: list[_Timer] = []
        with self._lock:
            self._elapsed += float(seconds)
            while self._timers and self._timers[0].deadline <= self._elapsed:
                timer = heapq.heappop(self._timers)
                if not timer.cancelled:
                    due.append(timer)
        for timer in due:
            self._fire(timer)

    def pending_deadlines(self) -> list[float]:
        """Seconds from now until each live timer is due, soonest first."""
        with self._lock:
            return sorted(timer.deadline - self._elapsed for timer in self._timers if not timer.cancelled)

    async def wait_for_timers(self, count: int = 1) -> None:
        """Return once at least ``count`` timers are pending (a test wraps this in a liveness bound)."""
        loop = asyncio.get_running_loop()
        while len(self.pending_deadlines()) < count:
            waiter = loop.create_future()
            with self._lock:
                self._watchers.append((loop, waiter))
            if len(self.pending_deadlines()) >= count:
                waiter.cancel()
                break
            await waiter

    # -- timers --------------------------------------------------------------------
    def _fire(self, timer: _Timer) -> None:
        def run() -> None:
            if not timer.cancelled:
                timer.cancelled = True
                timer.callback()

        try:
            timer.loop.call_soon_threadsafe(run)
        except RuntimeError:  # the loop is closed: nothing is waiting any more
            pass

    def _add_timer(self, delay: float, callback: Callable[[], None]) -> _Timer:
        loop = asyncio.get_running_loop()
        with self._lock:
            deadline = self._elapsed + max(float(delay), 0.0)
            timer = _Timer(deadline, next(self._sequence), loop, callback)
            due = delay <= 0
            if not due:
                heapq.heappush(self._timers, timer)
            watchers, self._watchers = self._watchers, []
        if due:
            self._fire(timer)
        for watcher_loop, waiter in watchers:
            try:
                watcher_loop.call_soon_threadsafe(lambda w=waiter: w.done() or w.set_result(None))
            except RuntimeError:
                pass
        return timer

    def _cancel_timer(self, timer: _Timer) -> None:
        with self._lock:
            if timer.cancelled:
                return
            timer.cancelled = True
            try:
                self._timers.remove(timer)
            except ValueError:
                return
            heapq.heapify(self._timers)

    async def sleep(self, delay: float, result: Any = None) -> Any:
        if delay <= 0:
            await asyncio.sleep(0)
            return result
        waiter = asyncio.get_running_loop().create_future()
        timer = self._add_timer(delay, lambda: waiter.done() or waiter.set_result(None))
        try:
            await waiter
        finally:
            self._cancel_timer(timer)
        return result

    @asynccontextmanager
    async def timeout(self, delay: float | None) -> AsyncIterator[asyncio.Timeout]:
        # asyncio's own Timeout owns the cancel/uncancel and TimeoutError
        # semantics; the fake timer only decides *when* it expires.
        async with asyncio.timeout(None) as scope:
            if delay is None:
                yield scope
                return
            loop = asyncio.get_running_loop()

            def expire() -> None:
                if not scope.expired():
                    try:
                        scope.reschedule(loop.time())
                    except RuntimeError:  # the body already left the scope
                        pass

            timer = self._add_timer(delay, expire)
            try:
                yield scope
            finally:
                self._cancel_timer(timer)

    async def wait_for(self, aw: Awaitable[T], timeout: float | None) -> T:
        if timeout is not None and timeout <= 0:
            # asyncio.wait_for: a non-positive timeout returns only an already-done result.
            future = asyncio.ensure_future(aw)
            if future.done():
                return future.result()
            future.cancel()
            # Like asyncio's _cancel_and_wait: an outer cancellation propagates.
            await asyncio.wait([future])
            try:
                return future.result()
            except asyncio.CancelledError as error:
                raise TimeoutError from error
        async with self.timeout(timeout):
            return await aw

    async def wait(self, fs: Iterable[asyncio.Future], *, timeout: float | None = None,
                   return_when: str = asyncio.ALL_COMPLETED) -> tuple[set, set]:
        futures = {asyncio.ensure_future(f) for f in fs}
        if not futures:
            raise ValueError("Set of Tasks/Futures is empty.")
        if timeout is None:
            return await asyncio.wait(futures, return_when=return_when)
        expired = asyncio.get_running_loop().create_future()
        timer = self._add_timer(timeout, lambda: expired.done() or expired.set_result(None))

        def satisfied() -> bool:
            done = [f for f in futures if f.done()]
            if return_when == asyncio.FIRST_COMPLETED:
                return bool(done)
            if return_when == asyncio.FIRST_EXCEPTION and any(
                    not f.cancelled() and f.exception() is not None for f in done):
                return True
            return len(done) == len(futures)

        try:
            while not satisfied() and not expired.done():
                await asyncio.wait({*(f for f in futures if not f.done()), expired},
                                   return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._cancel_timer(timer)
            if not expired.done():
                expired.cancel()
        done = {f for f in futures if f.done()}
        return done, futures - done


_clock: Clock = SystemClock()


def get_clock() -> Clock:
    """The process clock. Read it at the call site, never cache it at import."""
    return _clock


def set_clock(clock: Clock) -> Clock:
    """Install ``clock`` as the process clock and return the previous one (tests only)."""
    global _clock
    previous, _clock = _clock, clock
    return previous
