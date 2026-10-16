"""configuration_mutation_lock must exclude across event loops and threads (#1075).

The sync inference path resolves accounting coroutines with ``asyncio.run`` on
another thread (``_run_awaitable_sync``); a loop-bound ``asyncio.Lock`` would
raise there and give no cross-thread exclusion.
"""

import asyncio
import threading

import pytest

from src.model_fabric.effective_policy import LoopAgnosticLock
from src.model_fabric.execution import _run_awaitable_sync


def _sync_path_acquires(lock: LoopAgnosticLock, events: list[str], acquired: threading.Event) -> None:
    """Stand-in for AccountingExecutor.execute_sync on a worker thread."""

    async def writer() -> str:
        async with lock:
            events.append("sync-path acquired")
            acquired.set()
        return "done"

    events.append(_run_awaitable_sync(writer()))


@pytest.mark.asyncio
async def test_sync_path_on_another_thread_waits_for_app_loop_holder_without_error():
    lock = LoopAgnosticLock()
    events: list[str] = []
    acquired = threading.Event()
    async with lock:
        worker = threading.Thread(target=_sync_path_acquires, args=(lock, events, acquired))
        worker.start()
        # The worker runs its own loop and must be excluded, not error.
        assert not await asyncio.to_thread(acquired.wait, 0.2)
        events.append("app loop released")
    await asyncio.to_thread(worker.join, 5)
    assert not worker.is_alive()
    assert events == ["app loop released", "sync-path acquired", "done"]
    assert not lock.locked()


@pytest.mark.asyncio
async def test_app_loop_waits_for_a_holder_on_another_loop():
    lock = LoopAgnosticLock()
    held, release = threading.Event(), threading.Event()

    def other_loop_holder() -> None:
        async def hold() -> None:
            async with lock:
                held.set()
                await asyncio.to_thread(release.wait, 5)
        asyncio.run(hold())

    worker = threading.Thread(target=other_loop_holder)
    worker.start()
    assert await asyncio.to_thread(held.wait, 5)
    waiter = asyncio.create_task(lock.acquire())
    await asyncio.sleep(0.1)
    assert not waiter.done()
    release.set()
    assert await asyncio.wait_for(waiter, 5) is True
    lock.release()
    await asyncio.to_thread(worker.join, 5)


@pytest.mark.asyncio
async def test_bridge_from_the_holding_loop_thread_fails_fast_instead_of_deadlocking():
    lock = LoopAgnosticLock()

    async def nested() -> None:
        async with lock:
            pass

    async with lock:
        # The loop thread blocks joining the bridge thread, so it can never
        # release; the bridge must raise rather than wait forever.
        with pytest.raises(RuntimeError, match="held by the caller blocked on this sync bridge"):
            _run_awaitable_sync(nested())
    assert not lock.locked()


def test_nested_bridges_from_the_holding_loop_thread_fail_fast():
    """Holder A -> bridge B -> bridge C -> acquire must raise, not hang."""
    lock = LoopAgnosticLock()
    outcome: list[BaseException | str] = []

    async def innermost() -> None:
        async with lock:
            pass

    async def middle() -> None:
        # Runs on bridge B's loop, so this starts a second bridge C.
        _run_awaitable_sync(innermost())

    def scenario() -> None:
        async def holder() -> None:
            async with lock:
                _run_awaitable_sync(middle())
        try:
            asyncio.run(holder())
            outcome.append("no error")
        except BaseException as exc:
            outcome.append(exc)

    # A daemon thread bounds a regression: the suite fails instead of hanging.
    worker = threading.Thread(target=scenario, daemon=True)
    worker.start()
    worker.join(10)
    assert not worker.is_alive(), "nested sync bridge deadlocked on the held lock"
    assert len(outcome) == 1 and isinstance(outcome[0], RuntimeError)
    assert "held by the caller blocked on this sync bridge" in str(outcome[0])
    assert not lock.locked()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_leave_the_lock_held():
    lock = LoopAgnosticLock()
    await lock.acquire()
    waiter = asyncio.create_task(lock.acquire())
    await asyncio.sleep(0.02)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    lock.release()
    assert not lock.locked()
    async with lock:
        assert lock.locked()


def test_process_lock_is_the_loop_agnostic_primitive():
    from src.model_fabric import effective_policy

    assert isinstance(effective_policy.configuration_mutation_lock, LoopAgnosticLock)


@pytest.mark.asyncio
async def test_discovery_writer_scope_on_the_sync_path_is_excluded_by_the_app_loop(monkeypatch):
    """Drive the real discovery writer scope from a worker-thread event loop."""
    from unittest.mock import patch

    from src.model_fabric import effective_policy
    from src.workflows.research_guard import discovery_writer_scope

    lock = LoopAgnosticLock()
    monkeypatch.setattr(effective_policy, "configuration_mutation_lock", lock)
    events: list[str] = []
    entered = threading.Event()

    async def discovery_writer() -> str:
        async with discovery_writer_scope() as policy:
            events.append(f"discovery writer {policy}")
            entered.set()
        return "done"

    with patch("src.guardian.goal_programmes.stage_programme_policy", return_value="staged-policy"):
        async with lock:
            worker = threading.Thread(target=lambda: events.append(_run_awaitable_sync(discovery_writer())))
            worker.start()
            assert not await asyncio.to_thread(entered.wait, 0.2)
            events.append("settings save released")
        await asyncio.to_thread(worker.join, 5)
    assert events == ["settings save released", "discovery writer staged-policy", "done"]
