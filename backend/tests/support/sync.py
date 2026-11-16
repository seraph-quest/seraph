"""Causal synchronisation helpers for tests (#1217 slice 4, #1221).

Tests synchronise on causes, never on durations. These helpers replace the
patterns that made the suite depend on machine speed:

``Signal``
    A one-shot flag that can be set from the event loop or from any thread and
    awaited from either side.
``Gate``
    A point a fake calls that records it was reached and parks until the test
    releases it. Replaces "sleep 10 ms so the task gets there".
``assert_blocked``
    Waits until a task is *observed* parked at a known point (a gate, a signal
    or a predicate over its suspension chain), lets the loop settle, then
    asserts the task is still not done. Replaces "sleep, then assert not
    done", which passed vacuously whenever the task was merely slow.
``settle``
    Runs the event loop until no callback is ready, optionally after draining
    tracked background tasks. Use it between requests.
``wait_until`` / ``wait_until_blocking``
    Await a condition. With a notifier the wait is woken by the state change;
    without one it is a bounded poll. Either way it never falls through: on
    its liveness bound it raises ``LivenessTimeout`` with the last observed
    state.
``started_or_raise``
    Wait for a callback inside a task to set an event, failing fast if the
    task ends first.

Every bound here is a generous *liveness* watchdog that fails loudly with a
diagnostic. None of them is the synchronisation itself, and none is an oracle:
a test must never assert on elapsed time.
"""
from __future__ import annotations

import asyncio
import inspect
import threading
import time
from typing import Any, Awaitable, Callable, Iterable

# A healthy test reaches each of these points in milliseconds. The bound only
# turns a hang into a readable failure; the per-test watchdog stays behind it.
LIVENESS_SECONDS = 60.0
# Poll step for conditions that have no notifier (a file, a process, a thread's
# progress). It decides how often the condition is re-read, never the outcome.
POLL_INTERVAL_SECONDS = 0.005
# Upper bound on loop iterations ``settle`` runs before it gives up on the
# ready queue emptying (a task that reschedules itself forever).
_SETTLE_ROUNDS = 1000


class LivenessTimeout(AssertionError):
    """A condition a test waits for was not reached within its liveness bound."""


def _describe(state: Any) -> str:
    try:
        text = repr(state)
    except Exception as error:  # pragma: no cover - diagnostics only
        text = f"<unrepresentable state: {error!r}>"
    return text if len(text) <= 2000 else text[:2000] + "..."


def _resolve(future: asyncio.Future) -> None:
    if not future.done():
        future.set_result(None)


class Signal:
    """A thread-safe one-shot flag that can be awaited or waited on."""

    def __init__(self, name: str = "signal") -> None:
        self.name = name
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._futures: list[tuple[asyncio.AbstractEventLoop, asyncio.Future]] = []

    def __repr__(self) -> str:
        return f"<Signal {self.name!r} {'set' if self.is_set() else 'unset'}>"

    def is_set(self) -> bool:
        return self._event.is_set()

    def set(self) -> None:
        """Set the flag. Safe from the event loop or from any thread."""

        with self._lock:
            self._event.set()
            futures, self._futures = self._futures, []
        for loop, future in futures:
            try:
                loop.call_soon_threadsafe(_resolve, future)
            except RuntimeError:  # the waiting loop already closed
                pass

    async def wait(self, *, bound: float = LIVENESS_SECONDS, task: asyncio.Future | None = None) -> None:
        """Await the flag. With ``task``, fail fast if that task ends first."""

        loop = asyncio.get_running_loop()
        with self._lock:
            if self._event.is_set():
                return
            future = loop.create_future()
            self._futures.append((loop, future))
        try:
            async with asyncio.timeout(bound):
                if task is None:
                    await future
                else:
                    await asyncio.wait({future, task}, return_when=asyncio.FIRST_COMPLETED)
        except TimeoutError:
            raise LivenessTimeout(
                f"{self.name} was not set within the {bound:g} s liveness bound"
                + (f"; task state: {_task_state(task)}" if task is not None else "")
            ) from None
        finally:
            with self._lock:
                self._futures = [entry for entry in self._futures if entry[1] is not future]
            future.cancel()
        if not self._event.is_set():
            assert task is not None and task.done()
            if not task.cancelled():
                task.result()
            raise AssertionError(f"task ended before {self.name} was set: {_task_state(task)}")

    def wait_blocking(self, *, bound: float = LIVENESS_SECONDS) -> None:
        """Block the calling thread until the flag is set."""

        if not self._event.wait(bound):
            raise LivenessTimeout(f"{self.name} was not set within the {bound:g} s liveness bound")


class Gate:
    """A point a fake parks at until the test releases it.

    The code under test calls ``await gate.arrive()`` (on the event loop) or
    ``gate.arrive_blocking()`` (on a worker thread). Arrival sets ``reached``
    and parks until ``release()``. Use ``async with gate:`` or call
    ``release()`` in ``finally`` so a failing test never leaves a fake parked.
    """

    def __init__(self, name: str = "gate", *, bound: float = LIVENESS_SECONDS) -> None:
        self.name = name
        self.bound = bound
        self.reached = Signal(f"{name} reached")
        self.released = Signal(f"{name} released")
        self.arrivals = 0
        self._count_lock = threading.Lock()

    def __repr__(self) -> str:
        return (f"<Gate {self.name!r} arrivals={self.arrivals} "
                f"released={self.released.is_set()}>")

    def _arrived(self) -> None:
        with self._count_lock:
            self.arrivals += 1
        self.reached.set()

    async def arrive(self) -> None:
        self._arrived()
        await self.released.wait(bound=self.bound)

    def arrive_blocking(self) -> None:
        self._arrived()
        self.released.wait_blocking(bound=self.bound)

    async def wait_reached(self, *, task: asyncio.Future | None = None) -> None:
        await self.reached.wait(bound=self.bound, task=task)

    def wait_reached_blocking(self) -> None:
        self.reached.wait_blocking(bound=self.bound)

    def release(self) -> None:
        self.released.set()

    async def __aenter__(self) -> "Gate":
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.release()

    def __enter__(self) -> "Gate":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def await_chain(task: asyncio.Future) -> list[str]:
    """Return the qualified names of the coroutines ``task`` is suspended in.

    The list runs from the task's own coroutine down to the innermost awaited
    coroutine; the last element is the ``repr`` of the awaited future when the
    chain ends in one. Use it to state *where* a task is parked.
    """

    chain: list[str] = []
    coro = task.get_coro() if isinstance(task, asyncio.Task) else None
    seen = 0
    while coro is not None and seen < 200:
        seen += 1
        code = getattr(coro, "cr_code", None) or getattr(coro, "gi_code", None) or getattr(coro, "ag_code", None)
        if code is not None:
            chain.append(code.co_qualname)
        nxt = getattr(coro, "cr_await", None)
        if nxt is None:
            nxt = getattr(coro, "gi_yieldfrom", None)
        if nxt is None:
            break
        if inspect.iscoroutine(nxt) or inspect.isgenerator(nxt) or hasattr(nxt, "cr_await"):
            coro = nxt
            continue
        # A bare future shows up as its C iterator; name the future itself.
        waiter = getattr(task, "_fut_waiter", None)
        chain.append(type(waiter).__name__ if waiter is not None else type(nxt).__name__)
        break
    return chain


def suspended_in(task: asyncio.Future, *qualnames: str, without: Iterable[str] = ()) -> bool:
    """True when ``task`` is pending, suspended inside every ``qualnames`` frame
    and inside none of the ``without`` frames."""

    if task.done():
        return False
    chain = await_chain(task)
    return all(name in chain for name in qualnames) and not any(name in chain for name in without)


def _task_state(task: asyncio.Future | None) -> str:
    if task is None:
        return "no task"
    if not task.done():
        return f"pending in {await_chain(task)}"
    if task.cancelled():
        return "cancelled"
    error = task.exception()
    return f"finished with {error!r}" if error is not None else f"returned {_describe(task.result())}"


async def settle(*, tracked: bool = False, bound: float = LIVENESS_SECONDS) -> None:
    """Run the event loop until no callback is ready.

    With ``tracked`` first drain the tasks registered through
    ``src.utils.background.track_task``. This settles work that lives on the
    loop; it does not wait for a worker thread, which needs a gate or signal.
    """

    if tracked:
        from src.utils.background import drain_tracked_tasks

        await within(drain_tracked_tasks(), what="tracked background tasks to drain", bound=bound)
    loop = asyncio.get_running_loop()
    ready = getattr(loop, "_ready", None)
    for _ in range(_SETTLE_ROUNDS):
        await asyncio.sleep(0)
        if ready is not None and not ready:
            return
    if ready is None:
        return
    raise LivenessTimeout(f"event loop still had ready callbacks after {_SETTLE_ROUNDS} rounds: {list(ready)[:5]}")


async def within(awaitable: Awaitable[Any], *, what: str, bound: float = LIVENESS_SECONDS) -> Any:
    """Await ``awaitable`` under a liveness bound that fails with ``what``."""

    try:
        async with asyncio.timeout(bound):
            return await awaitable
    except TimeoutError:
        raise LivenessTimeout(f"{what} did not happen within the {bound:g} s liveness bound") from None


async def wait_until(
    predicate: Callable[[], Any],
    *,
    what: str,
    notifier: asyncio.Condition | None = None,
    state: Callable[[], Any] | None = None,
    task: asyncio.Future | None = None,
    bound: float = LIVENESS_SECONDS,
    interval: float = POLL_INTERVAL_SECONDS,
) -> Any:
    """Wait until ``predicate()`` is truthy and return its value.

    ``notifier`` is an ``asyncio.Condition`` the code under test notifies on a
    state change; without one the predicate is re-read every ``interval``.
    ``task``, when given, fails the wait fast if that task finishes first
    (re-raising its exception). On the liveness bound this raises
    ``LivenessTimeout`` naming ``what`` and the last ``state()`` (or predicate
    value) it saw. It never returns without the condition holding.
    """

    last: list[Any] = [None]

    def check() -> Any:
        value = predicate()
        last[0] = state() if state is not None else value
        return value

    try:
        async with asyncio.timeout(bound):
            while True:
                value = check()
                if value:
                    return value
                if task is not None and task.done():
                    value = check()
                    if value:
                        return value
                    if not task.cancelled():
                        task.result()
                    raise AssertionError(
                        f"task ended before {what}: {_task_state(task)}; last state: {_describe(last[0])}"
                    )
                if notifier is not None:
                    async with notifier:
                        if not check():
                            await notifier.wait()
                    continue
                # The one place a duration appears: how often an un-notified
                # condition is re-read. It never decides the outcome.
                await asyncio.sleep(interval)
    except TimeoutError:
        raise LivenessTimeout(
            f"{what} was not reached within the {bound:g} s liveness bound; last state: {_describe(last[0])}"
        ) from None


def wait_until_blocking(
    predicate: Callable[[], Any],
    *,
    what: str,
    state: Callable[[], Any] | None = None,
    bound: float = LIVENESS_SECONDS,
    interval: float = POLL_INTERVAL_SECONDS,
) -> Any:
    """Synchronous ``wait_until`` for tests that are not on an event loop.

    The bound is an attempt budget (``bound / interval`` re-reads), so a loaded
    host only makes the watchdog more generous.
    """

    last: Any = None
    attempts = max(1, int(bound / interval))
    for _ in range(attempts):
        value = predicate()
        last = state() if state is not None else value
        if value:
            return value
        # As in ``wait_until``: the re-read cadence of an un-notified condition.
        time.sleep(interval)
    value = predicate()
    if value:
        return value
    raise LivenessTimeout(
        f"{what} was not reached within the {bound:g} s liveness bound; last state: "
        f"{_describe(state() if state is not None else last)}"
    )


async def assert_blocked(
    task: asyncio.Future,
    *,
    at: Gate | Signal | Callable[[], Any],
    what: str,
    bound: float = LIVENESS_SECONDS,
) -> None:
    """Assert ``task`` is parked at a known point, not merely slow.

    ``at`` is the point the task must be observed at first: a ``Gate`` it
    reached, a ``Signal`` that was set, or a predicate (for example
    ``lambda: suspended_in(task, "Lock.acquire")``). After the observation the
    loop is settled, so a task that was not truly blocked gets every chance to
    finish on the loop, and the task is asserted still pending (and, for a
    predicate, still at that point).
    """

    if isinstance(at, Gate):
        await at.reached.wait(bound=bound, task=task)
    elif isinstance(at, Signal):
        await at.wait(bound=bound, task=task)
    else:
        await wait_until(at, what=f"{what}: task observed at its blocking point", task=task, bound=bound,
                         state=lambda: _task_state(task))
    await settle()
    assert not task.done(), f"{what}: task finished although it should be blocked ({_task_state(task)})"
    if callable(at) and not isinstance(at, (Gate, Signal)):
        assert at(), f"{what}: task left its blocking point ({_task_state(task)})"


async def started_or_raise(started: asyncio.Event, task: asyncio.Future) -> None:
    """Wait for a callback inside ``task`` to set ``started``, failing fast if
    ``task`` ends first.

    A finished task re-raises its own exception; a task that returned normally
    without setting ``started`` fails with an explicit assertion. The wait has
    the shared liveness bound.
    """

    waiter = asyncio.ensure_future(started.wait())
    try:
        async with asyncio.timeout(LIVENESS_SECONDS):
            await asyncio.wait({waiter, task}, return_when=asyncio.FIRST_COMPLETED)
    except TimeoutError:
        raise LivenessTimeout(
            f"background callback did not start within the {LIVENESS_SECONDS:g} s liveness bound; "
            f"task state: {_task_state(task)}"
        ) from None
    finally:
        waiter.cancel()
    if not started.is_set():
        task.result()
        raise AssertionError("background task finished before its callback started")


__all__ = [
    "Gate",
    "LIVENESS_SECONDS",
    "LivenessTimeout",
    "POLL_INTERVAL_SECONDS",
    "Signal",
    "assert_blocked",
    "await_chain",
    "settle",
    "started_or_raise",
    "suspended_in",
    "wait_until",
    "wait_until_blocking",
    "within",
]
