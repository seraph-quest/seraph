"""The causal synchronisation helpers fail loudly and never fall through (#1221)."""
import asyncio
import threading

import pytest

from tests.support.sync import (
    Gate,
    LivenessTimeout,
    Signal,
    assert_blocked,
    await_chain,
    settle,
    started_or_raise,
    suspended_in,
    wait_until,
    wait_until_blocking,
    within,
)


@pytest.mark.asyncio
async def test_signal_set_from_a_thread_wakes_an_async_waiter():
    signal = Signal("from thread")
    worker = threading.Thread(target=signal.set)
    worker.start()
    await signal.wait()
    worker.join()
    assert signal.is_set()


@pytest.mark.asyncio
async def test_signal_wait_fails_fast_with_the_task_outcome():
    signal = Signal("never")

    async def boom():
        raise ValueError("early failure")

    task = asyncio.create_task(boom())
    with pytest.raises(ValueError, match="early failure"):
        await signal.wait(task=task)

    async def quiet():
        return "done"

    task = asyncio.create_task(quiet())
    with pytest.raises(AssertionError, match="task ended before never was set"):
        await signal.wait(task=task)


@pytest.mark.asyncio
async def test_signal_liveness_bound_raises_named_diagnostic():
    with pytest.raises(LivenessTimeout, match="quiet signal was not set within"):
        await Signal("quiet signal").wait(bound=0.01)
    with pytest.raises(LivenessTimeout, match="blocking signal"):
        Signal("blocking signal").wait_blocking(bound=0.01)


@pytest.mark.asyncio
async def test_gate_parks_async_and_thread_arrivals_until_release():
    gate = Gate("work")
    order = []

    async def on_loop():
        await gate.arrive()
        order.append("loop")

    def on_thread():
        gate.arrive_blocking()
        order.append("thread")

    task = asyncio.create_task(on_loop())
    thread = threading.Thread(target=on_thread)
    thread.start()
    await assert_blocked(task, at=gate, what="loop arrival parks")
    await wait_until(lambda: gate.arrivals == 2, what="both arrivals", state=lambda: gate.arrivals)
    assert order == []
    gate.release()
    await task
    await asyncio.to_thread(thread.join)
    assert sorted(order) == ["loop", "thread"]


@pytest.mark.asyncio
async def test_gate_arrival_fails_loudly_when_never_released():
    gate = Gate("abandoned", bound=0.01)
    with pytest.raises(LivenessTimeout, match="abandoned released"):
        await gate.arrive()


@pytest.mark.asyncio
async def test_assert_blocked_rejects_a_task_that_is_not_parked():
    gate = Gate("passed through")

    async def does_not_park():
        gate.reached.set()  # reaches the point but never parks there

    task = asyncio.create_task(does_not_park())
    with pytest.raises(AssertionError, match="should be blocked"):
        await assert_blocked(task, at=gate.reached, what="vacuous negative proof")


@pytest.mark.asyncio
async def test_assert_blocked_on_suspension_point_and_await_chain():
    lock = asyncio.Lock()
    await lock.acquire()

    async def contender():
        async with lock:
            return "acquired"

    task = asyncio.create_task(contender())
    await assert_blocked(task, at=lambda: suspended_in(task, "Lock.acquire"), what="lock contender")
    chain = await_chain(task)
    assert chain[0].endswith("<locals>.contender") and "Lock.acquire" in chain and chain[-1] == "Future"
    lock.release()
    assert await task == "acquired"

    unguarded = asyncio.create_task(asyncio.sleep(0, "free"))
    with pytest.raises(AssertionError, match="task ended before"):
        await assert_blocked(unguarded, at=lambda: suspended_in(unguarded, "Lock.acquire"), what="no lock")


@pytest.mark.asyncio
async def test_wait_until_never_falls_through():
    state = {"n": 0}
    with pytest.raises(LivenessTimeout, match=r"n reaches 3 was not reached .* last state: \{'n': 0\}"):
        await wait_until(lambda: state["n"] >= 3, what="n reaches 3", state=lambda: dict(state), bound=0.02)
    with pytest.raises(LivenessTimeout, match="flag set"):
        wait_until_blocking(lambda: False, what="flag set", bound=0.02)
    assert wait_until_blocking(lambda: "value", what="immediately true") == "value"


@pytest.mark.asyncio
async def test_wait_until_wakes_on_notifier_and_fails_fast_on_task():
    condition = asyncio.Condition()
    box = []

    async def producer():
        await asyncio.sleep(0)
        async with condition:
            box.append(1)
            condition.notify_all()

    task = asyncio.create_task(producer())
    assert await wait_until(lambda: box, what="box filled", notifier=condition) == [1]
    await task

    async def fails():
        raise RuntimeError("producer died")

    task = asyncio.create_task(fails())
    with pytest.raises(RuntimeError, match="producer died"):
        await wait_until(lambda: False, what="never", task=task)


@pytest.mark.asyncio
async def test_settle_runs_ready_callbacks_and_within_names_the_wait():
    seen = []

    async def chained(depth):
        seen.append(depth)
        if depth:
            await asyncio.sleep(0)
            await chained(depth - 1)

    asyncio.create_task(chained(5))
    await settle()
    assert seen == [5, 4, 3, 2, 1, 0]
    with pytest.raises(LivenessTimeout, match="never finishes did not happen"):
        await within(asyncio.Event().wait(), what="never finishes", bound=0.01)


@pytest.mark.asyncio
async def test_started_or_raise_surfaces_early_task_outcome():
    started = asyncio.Event()

    async def denied():
        raise PermissionError("denied before callback")

    with pytest.raises(PermissionError):
        await started_or_raise(started, asyncio.create_task(denied()))


@pytest.mark.asyncio
async def test_task_timeout_error_is_not_reported_as_liveness():
    async def times_out():
        raise TimeoutError("product timeout")

    task = asyncio.create_task(times_out())
    with pytest.raises(TimeoutError, match="product timeout") as raised:
        await wait_until(lambda: False, what="never", task=task)
    assert not isinstance(raised.value, LivenessTimeout)
    with pytest.raises(TimeoutError, match="product timeout") as raised:
        await within(times_out(), what="own timeout")
    assert not isinstance(raised.value, LivenessTimeout)


@pytest.mark.asyncio
async def test_wait_until_accepts_an_async_predicate():
    reads = []

    async def read():
        reads.append(1)
        return len(reads) >= 3 and "ready"

    assert await wait_until(read, what="third read") == "ready"


@pytest.mark.asyncio
async def test_unbounded_gate_stays_parked_until_released():
    gate = Gate("stuck", bound=None)
    task = asyncio.create_task(gate.arrive())
    await assert_blocked(task, at=gate, what="unbounded gate")
    with pytest.raises(LivenessTimeout):
        await within(asyncio.shield(task), what="outer bound", bound=0.01)
    assert not task.done()
    gate.release()
    await task
