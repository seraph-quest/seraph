import asyncio

import pytest

from src.utils.background import (
    background_task_scope,
    drain_tracked_tasks,
    track_task,
)


@pytest.mark.asyncio
async def test_drain_tracked_tasks_waits_for_pending_work():
    completed = asyncio.Event()

    async def _worker():
        await asyncio.sleep(0)
        completed.set()

    track_task(_worker(), name="drain-pending")

    await drain_tracked_tasks(timeout_seconds=1.0)

    assert completed.is_set()


@pytest.mark.asyncio
async def test_drain_tracked_tasks_cancels_stalled_work():
    cancelled = asyncio.Event()

    async def _worker():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    track_task(_worker(), name="drain-stalled")

    await drain_tracked_tasks(timeout_seconds=0.0)

    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_drain_tracked_tasks_raises_when_task_ignores_cancellation():
    release = asyncio.Event()
    finished = asyncio.Event()

    async def _worker():
        try:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
        finally:
            finished.set()

    track_task(_worker(), name="drain-stubborn")

    with pytest.raises(RuntimeError, match="drain-stubborn"):
        await asyncio.wait_for(drain_tracked_tasks(timeout_seconds=0.0), timeout=0.5)

    release.set()
    await asyncio.wait_for(drain_tracked_tasks(timeout_seconds=1.0), timeout=0.5)

    assert finished.is_set()


@pytest.mark.asyncio
async def test_scoped_drain_ignores_unrelated_tasks_and_supports_nested_scopes():
    release_unrelated = asyncio.Event()
    release_outer = asyncio.Event()
    outer_finished = asyncio.Event()
    inner_finished = asyncio.Event()

    async def _unrelated_worker():
        await release_unrelated.wait()

    async def _scoped_worker(finished: asyncio.Event):
        await asyncio.sleep(0)
        finished.set()

    async def _outer_worker():
        await release_outer.wait()
        outer_finished.set()

    unrelated = track_task(_unrelated_worker(), name="unrelated-background")
    await asyncio.sleep(0)

    with background_task_scope():
        outer = track_task(_outer_worker(), name="outer-background")
        with background_task_scope():
            inner = track_task(
                _scoped_worker(inner_finished), name="inner-background"
            )
            await drain_tracked_tasks(timeout_seconds=1.0)

            assert inner_finished.is_set()
            assert not outer.done()
            assert not unrelated.done()

        release_outer.set()
        await drain_tracked_tasks(timeout_seconds=1.0)

    release_unrelated.set()
    await drain_tracked_tasks(timeout_seconds=1.0)

    assert outer_finished.is_set()
    assert unrelated.done()
