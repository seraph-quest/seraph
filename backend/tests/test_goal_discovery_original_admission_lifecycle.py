"""Original admission lifetime only; no authority, Source or job is issued."""
import asyncio
import os
from pathlib import Path

import pytest

from src.guardian.goal_discovery import (
    GoalDiscoveryService, current_goal_discovery, goal_discovery_service,
)
from src.guardian.goal_programmes import goal_programme_service
from src.runtime_plugins.bridge import CordisHost, cordis_host
from src.work_board.dispatcher import _dispatcher
from src.workflows.job_runtime import DurableJobRepository


def original_references():
    service = goal_discovery_service
    return (service.jobs, service.strategy_resolver,
        service._lifecycle_dispatcher, service._lifecycle_host)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["partial_start", "body", "late_stop"])
async def test_original_discovery_lifecycle_restores_references(monkeypatch, failure):
    service = goal_discovery_service
    before = original_references()
    assert not service.started and _dispatcher.goal_discovery is None
    actual_start, actual_stop = service.start, service.stop

    async def start():
        await actual_start()
        if failure == "partial_start":
            raise RuntimeError("original partial start")

    async def stop():
        await actual_stop()
        if failure == "late_stop":
            raise RuntimeError("original late stop")

    monkeypatch.setattr(service, "start", start)
    monkeypatch.setattr(service, "stop", stop)
    with pytest.raises(RuntimeError, match="original"):
        async with current_goal_discovery() as current:
            assert current is service and current.jobs is _dispatcher.jobs
            assert current._lifecycle_host is cordis_host
            assert current.strategy_resolver is _dispatcher.strategy_resolver
            if failure == "body":
                raise RuntimeError("original body failure")
    assert original_references() == before
    assert not service.started and _dispatcher.goal_discovery is None
    assert service._tasks == set()


@pytest.mark.asyncio
async def test_original_stop_cancels_and_gathers_admission_before_authority(monkeypatch):
    service = goal_discovery_service
    before = original_references()
    entered, never_release = asyncio.Event(), asyncio.Event()
    cancelled = asyncio.Event()

    async def before_authority(**_kwargs):
        # Observe the original boundary, then stop without returning authority.
        assert asyncio.current_task() in service._tasks
        entered.set()
        try:
            await never_release.wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(goal_programme_service, "assert_authority", before_authority)
    async with current_goal_discovery():
        task = asyncio.create_task(service.admit(
            goal_id="not-read", programme_id="not-read", grant_revision=1))
        await asyncio.wait_for(entered.wait(), 5)
        await service.stop()
        assert task.done() and task.cancelled() and cancelled.is_set()
        assert service._tasks == set()
    assert original_references() == before
    assert _dispatcher.goal_discovery is None and not service.started


@pytest.mark.asyncio
async def test_cancelled_lifecycle_owner_unwinds_and_gathers_admission(monkeypatch):
    service = goal_discovery_service
    before = original_references()
    entered, never_release = asyncio.Event(), asyncio.Event()
    task_holder = []

    async def before_authority(**_kwargs):
        entered.set()
        await never_release.wait()

    monkeypatch.setattr(goal_programme_service, "assert_authority", before_authority)

    async def lifetime():
        async with current_goal_discovery():
            task_holder.append(asyncio.create_task(service.admit(
                goal_id="not-read", programme_id="not-read", grant_revision=1)))
            await never_release.wait()

    owner = asyncio.create_task(lifetime())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert task_holder[0].done() and task_holder[0].cancelled()
        assert service._tasks == set()
        assert original_references() == before
        assert _dispatcher.goal_discovery is None and not service.started
    finally:
        if not owner.done():
            owner.cancel()
        await asyncio.gather(owner, *task_holder, return_exceptions=True)


@pytest.mark.asyncio
async def test_actual_host_checker_binds_only_original_admission_task(monkeypatch):
    service = goal_discovery_service
    before = original_references()
    assert not service.started and _dispatcher.goal_discovery is None
    assert not cordis_host.admitting
    assert cordis_host.process is None or cordis_host.process.returncode is not None
    # Existing test binary selector; start the real original singleton child.
    # No reviewed/admitting/boot/callback flag is synthesized.
    monkeypatch.setattr(cordis_host, "node_path", Path(os.environ["SERAPH_CORDIS_TEST_NODE"]))
    observations = []

    async def before_authority(**_kwargs):
        task = asyncio.current_task()
        checker = GoalDiscoveryService._validate_original_admission_owner
        assert checker(service, jobs=service.jobs, host=cordis_host, task=task) is task
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await checkpoint.wait()
        assert checker(service, jobs=service.jobs, host=cordis_host, task=task) is task
        with pytest.raises(RuntimeError, match="programme_original_admission_owner_unavailable"):
            checker(service, jobs=DurableJobRepository(), host=cordis_host, task=task)
        with pytest.raises(RuntimeError, match="programme_original_admission_owner_unavailable"):
            checker(GoalDiscoveryService(jobs=service.jobs), jobs=service.jobs,
                host=cordis_host, task=task)

        async def inherited_other_task():
            with pytest.raises(RuntimeError, match="programme_original_admission_owner_unavailable"):
                checker(service, jobs=service.jobs, host=cordis_host, task=asyncio.current_task())

        await asyncio.create_task(inherited_other_task())
        # Same original class but foreign instance: no readiness is forged.
        with pytest.raises(RuntimeError, match="programme_original_admission_owner_unavailable"):
            checker(service, jobs=service.jobs, host=CordisHost(), task=task)
        observations.append(task)
        raise RuntimeError("observed original pre-authority boundary")

    monkeypatch.setattr(goal_programme_service, "assert_authority", before_authority)
    try:
        async with current_goal_discovery():
            assert await cordis_host.start(), cordis_host.snapshot()
            with pytest.raises(RuntimeError, match="observed original pre-authority boundary"):
                await service.admit(goal_id="not-read", programme_id="not-read", grant_revision=1)
            assert observations == [asyncio.current_task()]
            assert service._tasks == set()
            with pytest.raises(RuntimeError, match="programme_original_admission_owner_unavailable"):
                service._validate_original_admission_owner(
                    jobs=service.jobs, host=cordis_host, task=asyncio.current_task())
    finally:
        await cordis_host.stop()
    assert original_references() == before
    assert _dispatcher.goal_discovery is None and not service.started


@pytest.mark.asyncio
async def test_already_started_original_owner_is_not_stopped_or_rebound(monkeypatch):
    service = goal_discovery_service
    before = original_references()
    assert not service.started and _dispatcher.goal_discovery is None
    # Establish real existing ownership; do not fabricate started/tasks flags.
    await service.start()
    entered, release = asyncio.Event(), asyncio.Event()
    stopped = []
    actual_stop = service.stop

    async def stop():
        stopped.append(True)
        await actual_stop()

    async def before_authority(**_kwargs):
        entered.set()
        await release.wait()
        raise RuntimeError("existing owner boundary released")

    monkeypatch.setattr(service, "stop", stop)
    monkeypatch.setattr(goal_programme_service, "assert_authority", before_authority)
    task = asyncio.create_task(service.admit(
        goal_id="not-read", programme_id="not-read", grant_revision=1))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert task in service._tasks
        with pytest.raises(RuntimeError, match="public discovery lifecycle already owned"):
            async with current_goal_discovery():
                raise AssertionError("second lifecycle must not enter")
        assert stopped == [] and service.started
        assert original_references() == before
        assert _dispatcher.goal_discovery is None
        assert not task.done() and not task.cancelling() and task in service._tasks
        release.set()
        with pytest.raises(RuntimeError, match="existing owner boundary released"):
            await task
        assert stopped == [] and not task.cancelled()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await actual_stop()
    assert original_references() == before
    assert not service.started and service._tasks == set()
