"""Actual threads prove bounded optional-metadata scheduling and ownership."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread, current_thread
import time

import pytest
from fastapi import HTTPException

from src.api import extension_metadata as metadata


@pytest.fixture(autouse=True)
def isolated_worker(monkeypatch):
    old = metadata._pending
    if old is not None:
        assert old.worker_done.wait(2), "prior actual worker did not quiesce"
    monkeypatch.setattr(metadata, "_pending", None)
    monkeypatch.setattr(metadata, "CALLER_WAIT_SECONDS", 0.1)


@pytest.mark.asyncio
async def test_worker_preserves_heartbeat_and_returns_fresh_build_each_time():
    names, calls = [], []
    def build():
        names.append(current_thread().name)
        time.sleep(0.03)
        calls.append(1)
        return {"sequence": len(calls)}
    start = time.monotonic()
    task = asyncio.create_task(metadata.bounded_extension_metadata(build))
    await asyncio.sleep(0.005)
    assert time.monotonic() - start < 0.025
    assert await task == {"sequence": 1}
    assert await metadata.bounded_extension_metadata(build) == {"sequence": 2}
    assert names and all(name != "MainThread" for name in names)


@pytest.mark.asyncio
async def test_prepare_runs_once_on_event_loop_before_detached_worker_and_redacts_failure():
    prepared, built = [], []
    loop = asyncio.get_running_loop()
    def prepare():
        assert asyncio.get_running_loop() is loop
        prepared.append(current_thread().name)
        return {"immutable": "snapshot"}
    def build(snapshot):
        built.append(current_thread().name)
        assert snapshot == {"immutable": "snapshot"}
        return {"extensions": []}
    assert await metadata.bounded_extension_metadata(build, prepare=prepare) == {"extensions": []}
    assert prepared == ["MainThread"] and built[0] != "MainThread"
    def broken():
        raise RuntimeError("/private/path secret-token")
    with pytest.raises(HTTPException) as failure:
        await metadata.bounded_extension_metadata(build, prepare=broken)
    assert "secret-token" not in repr(failure.value.detail)
    assert metadata._pending.worker_done.is_set() and not metadata._pending.submitted


@pytest.mark.asyncio
async def test_abort_and_concurrent_timeout_do_not_submit_another_actual_worker():
    started, release, finished = Event(), Event(), Event()
    calls = []
    def build():
        calls.append(1)
        started.set()
        try:
            assert release.wait(2)
            return {"extensions": []}
        finally:
            finished.set()
    first = asyncio.create_task(metadata.bounded_extension_metadata(build))
    try:
        while not started.is_set():
            await asyncio.sleep(0.001)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        for _ in range(2):
            with pytest.raises(HTTPException) as error:
                await metadata.bounded_extension_metadata(build)
            assert error.value.status_code == 503
        assert calls == [1]
        assert metadata._pending.submitted is True
        assert not metadata._pending.worker_done.is_set()
    finally:
        release.set()
        assert finished.wait(2)
    await asyncio.sleep(0.01)
    assert await metadata.bounded_extension_metadata(lambda: {"recovered": True}) == {"recovered": True}


@pytest.mark.asyncio
async def test_queued_before_start_abort_retains_actual_thread_completion(monkeypatch):
    executor = ThreadPoolExecutor(max_workers=1)
    blocker_started, release_blocker, release_worker, worker_started, worker_finished = (Event() for _ in range(5))
    def blocker():
        blocker_started.set()
        assert release_blocker.wait(2)
    executor.submit(blocker)
    assert blocker_started.wait(2)
    loop = asyncio.get_running_loop()
    original = loop.run_in_executor
    monkeypatch.setattr(loop, "run_in_executor", lambda _executor, callback: original(executor, callback))
    calls = []
    def build():
        calls.append(1)
        worker_started.set()
        try:
            assert release_worker.wait(2)
            return {"complete": True}
        finally:
            worker_finished.set()
    caller = asyncio.create_task(metadata.bounded_extension_metadata(build))
    try:
        await asyncio.sleep(0.005)
        assert metadata._pending.submitted is True
        assert not worker_started.is_set()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        with pytest.raises(HTTPException):
            await metadata.bounded_extension_metadata(build)
        assert not metadata._pending.worker_done.is_set()
        assert calls == []
        release_blocker.set()
        while not worker_started.is_set():
            await asyncio.sleep(0.001)
        assert calls == [1]
        release_worker.set()
        assert worker_finished.wait(2)
        await asyncio.sleep(0.01)
        assert await metadata.bounded_extension_metadata(lambda: {"fresh": True}) == {"fresh": True}
    finally:
        release_blocker.set()
        release_worker.set()
        executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_failed_submission_marks_done_and_can_recover(monkeypatch):
    loop = asyncio.get_running_loop()
    original = loop.run_in_executor
    def unavailable(*_):
        raise RuntimeError("token=private-secret /home/operator/private")
    monkeypatch.setattr(loop, "run_in_executor", unavailable)
    with pytest.raises(HTTPException) as error:
        await metadata.bounded_extension_metadata(lambda: {})
    assert metadata._pending.submitted is False
    assert metadata._pending.worker_done.is_set()
    assert "private-secret" not in str(error.value.detail)
    monkeypatch.setattr(loop, "run_in_executor", original)
    assert await metadata.bounded_extension_metadata(lambda: {"fresh": True}) == {"fresh": True}


@pytest.mark.asyncio
async def test_exception_and_invalid_shape_are_redacted_and_uncached():
    def failing():
        raise ValueError("token=private-secret https://private.example /home/operator/private")
    for builder in (failing, lambda: []):
        with pytest.raises(HTTPException) as error:
            await metadata.bounded_extension_metadata(builder)
        assert "private-secret" not in str(error.value.detail)
        assert "private.example" not in str(error.value.detail)
        assert "/home/operator" not in str(error.value.detail)
        assert metadata._pending.worker_done.is_set()
    assert await metadata.bounded_extension_metadata(lambda: {"fresh": True}) == {"fresh": True}


@pytest.mark.asyncio
async def test_other_loop_pending_worker_is_not_replaced():
    metadata._pending = metadata._PendingBuild(loop=object(), worker_done=Event(), submitted=True)
    calls = []
    try:
        with pytest.raises(HTTPException):
            await metadata.bounded_extension_metadata(lambda: calls.append(1) or {})
        assert not calls
        assert not metadata._pending.worker_done.is_set()
    finally:
        metadata._pending.worker_done.set()


@pytest.mark.asyncio
async def test_same_pending_snapshot_coalesces_finite_get_callers():
    release, started, finished = Event(), Event(), Event()
    calls = []
    def build():
        calls.append(1)
        started.set()
        try:
            assert release.wait(2)
            return {"extensions": [{"id": "one"}]}
        finally:
            finished.set()
    first = asyncio.create_task(metadata.bounded_extension_metadata(build))
    second = asyncio.create_task(metadata.bounded_extension_metadata(build))
    try:
        while not started.is_set():
            await asyncio.sleep(0.001)
        release.set()
        left, right = await asyncio.gather(first, second)
        assert left == right == {"extensions": [{"id": "one"}]}
        assert calls == [1]
    finally:
        release.set()
        assert finished.wait(2)


@pytest.mark.asyncio
async def test_actual_closed_loop_retains_worker_until_its_real_completion():
    started, release, finished, loop_closed = (Event() for _ in range(4))
    calls = []
    def build():
        calls.append(1)
        started.set()
        try:
            assert release.wait(2)
            return {"old": True}
        finally:
            finished.set()
    def old_loop_runner():
        old_loop = asyncio.new_event_loop()
        async def run_and_abort():
            task = asyncio.create_task(metadata.bounded_extension_metadata(build))
            while not started.is_set():
                await asyncio.sleep(0.001)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        try:
            old_loop.run_until_complete(run_and_abort())
        finally:
            old_loop.close()
            loop_closed.set()
    thread = Thread(target=old_loop_runner)
    thread.start()
    try:
        while not loop_closed.is_set():
            await asyncio.sleep(0.001)
        assert started.is_set() and not metadata._pending.worker_done.is_set()
        with pytest.raises(HTTPException):
            await metadata.bounded_extension_metadata(lambda: calls.append(1) or {})
        assert calls == [1]
        release.set()
        assert finished.wait(2)
        assert await metadata.bounded_extension_metadata(lambda: {"fresh": True}) == {"fresh": True}
    finally:
        release.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert finished.wait(2)
