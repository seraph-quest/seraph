"""#1025: the scheduler and dispatcher lifecycle follows its owner rows.

Real SQLite and the real APScheduler on the test loop; nothing is contacted.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select

from config.settings import settings
from tests.cordis_research import seed_composition_inventory
from tests.test_inference_accounting import accounting_db  # noqa: F401
from src.runtime_plugins.lifecycle import (ACTIVATED_EVENT, DISPATCH_DOMAIN, QUIESCED_EVENT, RETENTION_JOBS,
                                           SCHEDULER_DOMAIN, RuntimeLifecycle, job_domain)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def lifecycle(monkeypatch):
    """A fresh attached lifecycle as the process singleton, and a clean scheduler."""
    from src.runtime_plugins import lifecycle as module
    from src.scheduler import engine
    fresh = RuntimeLifecycle()
    fresh.attach()
    monkeypatch.setattr(module, "runtime_lifecycle", fresh)
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    yield fresh
    engine.shutdown_scheduler()
    fresh.detach()


async def _events(event_type):
    from src.db.engine import get_session
    from src.db.models import AuditEvent
    async with get_session() as db:
        return [json.loads(row) for row in (await db.execute(select(AuditEvent.details_json).where(
            AuditEvent.event_type == event_type))).scalars()]


async def _set_row(domain, **values):
    from sqlalchemy import update
    from src.db.engine import get_session
    from src.db.models import RuntimeCompositionState
    async with get_session() as db:
        await db.execute(update(RuntimeCompositionState).where(
            RuntimeCompositionState.runtime_domain == domain).values(**values))


def _gated(scheduler, domain):
    return {job.id: job.next_run_time for job in scheduler.get_jobs() if job_domain(job.id) == domain}


def test_retention_and_cleanup_jobs_are_exempt_and_dispatch_is_its_own_domain():
    assert RETENTION_JOBS == {"screen_cleanup", "audio_ingress_cleanup"}
    assert all(job_domain(job) is None for job in RETENTION_JOBS)
    assert job_domain("work_board_dispatch") == DISPATCH_DOMAIN
    assert job_domain("strategist_tick") == job_domain("user_cron:abc") == SCHEDULER_DOMAIN


def test_any_deletion_or_retention_job_is_classified_explicitly():
    """A new job whose id names deletion or retention must be added to the exemption list."""
    from src.scheduler.engine import _system_job_specs
    loop = asyncio.new_event_loop()
    try:
        ids = [job["id"] for job in _system_job_specs(loop)]
    finally:
        loop.close()
    implied = {job for job in ids if any(token in job for token in ("cleanup", "retention", "purge", "delete"))}
    assert implied <= RETENTION_JOBS, implied - RETENTION_JOBS


async def test_attached_scheduler_starts_only_retention_until_the_domain_activates(accounting_db, lifecycle):  # noqa: F811
    from src.scheduler import engine
    await seed_composition_inventory()
    scheduler = engine.init_scheduler()
    assert {job.id for job in scheduler.get_jobs()} == RETENTION_JOBS
    activation = await lifecycle.activate(SCHEDULER_DOMAIN)
    assert (activation.owner_kind, activation.epoch) == ("legacy", 1)
    gated = _gated(scheduler, SCHEDULER_DOMAIN)
    assert "strategist_tick" in gated and "work_board_dispatch" not in gated
    assert all(next_run is not None for next_run in gated.values())
    # A second activation is a no-op returning the same activation.
    assert await lifecycle.activate(SCHEDULER_DOMAIN) is activation
    assert len(await _events(ACTIVATED_EVENT)) == 1
    # Deactivation pauses every gated job of the domain; retention keeps running.
    await lifecycle.deactivate(SCHEDULER_DOMAIN, "scheduler_owner_draining")
    assert all(next_run is None for next_run in _gated(scheduler, SCHEDULER_DOMAIN).values())
    assert all(scheduler.get_job(job).next_run_time is not None for job in RETENTION_JOBS)
    [quiesced] = await _events(QUIESCED_EVENT)
    assert quiesced == {"runtime_domain": SCHEDULER_DOMAIN, "epoch": 1, "reason": "scheduler_owner_draining"}
    status = await lifecycle.status(SCHEDULER_DOMAIN)
    assert (status["state"], status["reason"]) == ("blocked", "scheduler_owner_draining") and status["recovery"]
    # Reactivation resumes the same instance's jobs (no rebuild).
    again = await lifecycle.activate(SCHEDULER_DOMAIN)
    assert again.epoch == 1 and all(value is not None for value in _gated(scheduler, SCHEDULER_DOMAIN).values())


async def test_a_cordis_owner_without_its_runtime_blocks_gated_jobs_but_never_retention(accounting_db, lifecycle):  # noqa: F811
    from src.runtime_plugins.bridge import CordisHost
    from src.scheduler import engine
    await seed_composition_inventory()
    await _set_row(SCHEDULER_DOMAIN, owner_kind="cordis", epoch=2, composition_digest="a" * 64)
    scheduler = engine.init_scheduler()
    assert await lifecycle.activate(SCHEDULER_DOMAIN, CordisHost()) is None
    status = await lifecycle.status(SCHEDULER_DOMAIN, CordisHost())
    assert status["state"] == "blocked" and status["reason"].startswith("cordis_") and status["recovery"]
    assert _gated(scheduler, SCHEDULER_DOMAIN) == {}
    assert {job.id for job in scheduler.get_jobs()} == RETENTION_JOBS
    # No legacy fallback: nothing activated at all.
    assert await _events(ACTIVATED_EVENT) == []


async def test_a_draining_or_blocked_owner_never_activates(accounting_db, lifecycle):  # noqa: F811
    await seed_composition_inventory()
    for state in ("draining", "blocked"):
        await _set_row(SCHEDULER_DOMAIN, state=state)
        assert await lifecycle.activate(SCHEDULER_DOMAIN) is None
        assert lifecycle.blocked[SCHEDULER_DOMAIN] == f"scheduler_owner_{state}"


async def test_scheduler_disabled_is_disabled_not_blocked(accounting_db, lifecycle, monkeypatch):  # noqa: F811
    from src.scheduler import engine
    await seed_composition_inventory()
    monkeypatch.setattr(settings, "scheduler_enabled", False)
    assert engine.init_scheduler() is None
    assert await lifecycle.activate(SCHEDULER_DOMAIN) is None
    status = await lifecycle.status(SCHEDULER_DOMAIN)
    assert status["state"] == "disabled" and status["reason"] == "scheduler_disabled"


async def test_a_gated_tick_while_inactive_runs_nothing_and_is_visible(accounting_db, lifecycle):  # noqa: F811
    from src.scheduler.engine import _async_job_wrapper
    await seed_composition_inventory()
    calls = []

    async def job():
        calls.append("ran")
    lifecycle.blocked[SCHEDULER_DOMAIN] = "scheduler_owner_blocked"
    await _async_job_wrapper(job, asyncio.get_running_loop(), job_id="strategist_tick")()
    assert calls == []
    # The exempt retention job runs whatever the owner rows say.
    await _async_job_wrapper(job, asyncio.get_running_loop(), job_id="screen_cleanup")()
    assert calls == ["ran"]
    from src.db.engine import get_session
    from src.db.models import AuditEvent
    async with get_session() as db:
        details = [json.loads(row.details_json) for row in (await db.execute(select(AuditEvent).where(
            AuditEvent.tool_name == "strategist_tick"))).scalars()]
    assert any(item.get("reason") == "scheduler_owner_blocked" for item in details), details


async def test_the_active_tick_carries_its_activation(accounting_db, lifecycle):  # noqa: F811
    from src.runtime_plugins.lifecycle import current_activation
    from src.scheduler.engine import _async_job_wrapper
    await seed_composition_inventory()
    activation = await lifecycle.activate(SCHEDULER_DOMAIN)
    seen = []

    async def job():
        seen.append(current_activation.get())
    await _async_job_wrapper(job, asyncio.get_running_loop(), job_id="goal_check")()
    assert seen == [activation] and current_activation.get() is None


async def test_deactivation_drains_an_inflight_tick_within_its_bound(accounting_db, lifecycle, monkeypatch):  # noqa: F811
    from src.runtime_plugins import lifecycle as module
    from src.scheduler.engine import _async_job_wrapper
    await seed_composition_inventory()
    await lifecycle.activate(SCHEDULER_DOMAIN)
    started, release = asyncio.Event(), asyncio.Event()
    finished = []

    async def slow():
        started.set()
        await release.wait()
        finished.append(True)
    tick = asyncio.create_task(_async_job_wrapper(slow, asyncio.get_running_loop(), job_id="goal_check")())
    await started.wait()
    asyncio.get_running_loop().call_later(0.05, release.set)
    await lifecycle.deactivate(SCHEDULER_DOMAIN, "scheduler_owner_draining")
    assert finished == [True] and tick.done()
    # Past the bound a wrapper is cancelled, never waited on forever.
    await lifecycle.activate(SCHEDULER_DOMAIN)
    monkeypatch.setattr(module, "SCHEDULER_DRAIN_SECONDS", 0.05)
    started.clear()
    release.clear()
    stuck = asyncio.create_task(_async_job_wrapper(slow, asyncio.get_running_loop(), job_id="goal_check")())
    await started.wait()
    await lifecycle.deactivate(SCHEDULER_DOMAIN, "scheduler_owner_draining")
    assert stuck.cancelled() or stuck.done()


async def test_api_launched_workers_need_an_active_dispatch_domain(lifecycle):
    from src.runtime_plugins.lifecycle_dispatch import assert_dispatch_admission
    from src.work_board.repository import BoardError
    with pytest.raises(BoardError) as refused:
        assert_dispatch_admission()
    assert refused.value.code == "dispatch_owner_not_active"
    lifecycle.detach()
    assert_dispatch_admission()  # a process without the lifecycle keeps today's behaviour


async def test_dispatch_drain_waits_then_cancels_past_the_bound(monkeypatch):
    from src.runtime_plugins.lifecycle_dispatch import drain_dispatch
    from src.work_board import dispatcher
    quick_done = asyncio.Event()

    async def quick():
        await asyncio.sleep(0.01)
        quick_done.set()

    async def stuck():
        await asyncio.sleep(60)
    registry = {("t1", "a1"): asyncio.create_task(quick()), ("t2", "a2"): asyncio.create_task(stuck())}
    monkeypatch.setattr(dispatcher, "_ACTIVE_WORKER_TASKS", registry)
    cancelled = await drain_dispatch(bound_seconds=0.2)
    assert quick_done.is_set() and cancelled == [("t2", "a2")] and registry[("t2", "a2")].cancelled()
