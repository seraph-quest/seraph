"""#1025 (A3.2): scheduler and WorkBoard dispatcher lifecycle under the domain epoch.

Real SQLite owner rows and real fenced transitions; a real reviewed Cordis
child where the Cordis boot proof matters. External transport and inference
stay denied (``service_db`` / ``accounting_db``).
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select, update

from src.db import engine as db_engine
from src.db.models import AuditEvent, RuntimeCompositionState, ScheduledJobRun
from src.runtime_plugins.lifecycle import (ACTIVATED_EVENT, DISPATCH_JOBS, QUIESCED_EVENT, RETENTION_JOBS,
                                           SCHEDULER_GATED_JOBS, LifecycleBlocked, LifecycleFenced,
                                           RuntimeLifecycle, assert_tick_fence, current_tick, job_domain)
from src.runtime_plugins.ownership import DISPATCH_DOMAIN, SCHEDULER_DOMAIN, CompositionBindingError
from tests.cordis_research import needs_cordis, research_host
from tests.test_cordis_services_core import service_db  # noqa: F401
from tests.test_inference_accounting import accounting_db  # noqa: F401
from tests.test_research_native_vertical import real_auth  # noqa: F401

pytestmark = pytest.mark.asyncio
BACKEND = Path(__file__).resolve().parents[1]


def _lifecycle(**kwargs) -> RuntimeLifecycle:
    lifecycle = RuntimeLifecycle()
    lifecycle.managed = True
    lifecycle.scheduler_enabled = kwargs.get("scheduler_enabled", True)
    lifecycle.host = kwargs.get("host")
    lifecycle.worker_registry = kwargs.get("registry", lambda: {})
    return lifecycle


async def _events(event_type: str) -> list[dict]:
    async with db_engine.get_session() as db:
        return [json.loads(raw) for raw in (await db.execute(select(AuditEvent.details_json).where(
            AuditEvent.event_type == event_type))).scalars()]


async def _bump(domain: str, **values) -> None:
    """Another owner moved the row (as a fenced transition would): never reuses an epoch."""
    async with db_engine.get_session() as db:
        await db.execute(update(RuntimeCompositionState).where(RuntimeCompositionState.runtime_domain == domain)
                         .values(**values))


def _status_entry(job_id: str) -> dict:
    from src.scheduler.background_inference import background_inference_status_path
    path = background_inference_status_path()
    return json.loads(path.read_text())["jobs"].get(job_id, {}) if path.exists() else {}


# ------------------------------------------------------------------ classification and naming

async def test_every_system_job_is_classified_and_deletion_jobs_are_exempt(service_db, monkeypatch):  # noqa: F811
    from config.settings import settings
    from src.scheduler.engine import init_scheduler, shutdown_scheduler
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    scheduler = init_scheduler()
    try:
        ids = {job.id for job in scheduler.get_jobs()}
    finally:
        shutdown_scheduler()
    assert ids == RETENTION_JOBS | DISPATCH_JOBS | SCHEDULER_GATED_JOBS
    assert not (RETENTION_JOBS & SCHEDULER_GATED_JOBS) and not (DISPATCH_JOBS & SCHEDULER_GATED_JOBS)
    for job_id in ids:
        if any(word in job_id for word in ("cleanup", "retention", "purge", "delete", "prune")):
            assert job_id in RETENTION_JOBS and job_domain(job_id) is None
    assert job_domain("work_board_dispatch") == DISPATCH_DOMAIN
    assert job_domain("user_cron:any") == SCHEDULER_DOMAIN
    with pytest.raises(LifecycleBlocked) as unknown:
        job_domain("new_unclassified_job")
    assert unknown.value.reason_code == "lifecycle_job_unclassified"
    # #1179: the retired voice-input cleanup job is not registered and is no
    # longer classified, so a stale id fails closed like any unknown job.
    assert "audio_ingress_cleanup" not in ids
    with pytest.raises(LifecycleBlocked):
        job_domain("audio_ingress_cleanup")


def test_tasks_is_the_data_domain_and_dispatch_the_lifecycle_domain():
    from src.runtime_plugins.contracts import METHOD_DOMAINS
    from src.runtime_plugins.cutover import cutover_manifest
    from src.runtime_plugins.ownership import DOMAINS, method_dependencies
    assert DISPATCH_DOMAIN in DOMAINS and "seraph.tasks.v1" in DOMAINS
    # Dispatch owns no service method; every method still depends on the tasks ledger, never on dispatch.
    assert DISPATCH_DOMAIN not in set(METHOD_DOMAINS.values())
    assert all(DISPATCH_DOMAIN not in method_dependencies(method) for method in METHOD_DOMAINS)
    assert all("seraph.tasks.v1" in method_dependencies(method) for method in METHOD_DOMAINS)
    order = [entry.domain for entry in cutover_manifest()]
    assert order[-2:] == [SCHEDULER_DOMAIN, DISPATCH_DOMAIN]
    assert dict((entry.domain, entry.prerequisites) for entry in cutover_manifest())[DISPATCH_DOMAIN] == (
        SCHEDULER_DOMAIN,)
    assert "seraph.tasks.v1" not in order


def test_importing_app_engine_and_dispatcher_starts_no_scheduler_timer_or_lifecycle():
    code = (
        "import asyncio, threading\n"
        "import src.app, src.scheduler.engine as engine, src.work_board.dispatcher as dispatcher\n"
        "from src.runtime_plugins.lifecycle import runtime_lifecycle\n"
        "assert engine.get_scheduler() is None\n"
        "assert not runtime_lifecycle.managed and runtime_lifecycle._active == {}\n"
        "assert dispatcher._ACTIVE_WORKER_TASKS == {}\n"
        "assert [t.name for t in threading.enumerate()] == ['MainThread'], threading.enumerate()\n"
        "print('ok')\n")
    result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().endswith("ok")


# ------------------------------------------------------------------ activation

async def test_activation_is_once_per_epoch_racing_starts_yield_one_activation(service_db):  # noqa: F811
    lifecycle = _lifecycle()
    statuses = await asyncio.gather(*(lifecycle.activate(SCHEDULER_DOMAIN) for _ in range(5)))
    assert {status["state"] for status in statuses} == {"active"}
    assert (await lifecycle.activate(SCHEDULER_DOMAIN))["epoch"] == 1  # a second activate is a no-op
    events = [event for event in await _events(ACTIVATED_EVENT) if event["domain"] == SCHEDULER_DOMAIN]
    assert len(events) == 1 and events[0]["owner_kind"] == "legacy" and events[0]["epoch"] == 1


async def test_unmanaged_process_and_disabled_scheduler_never_run_gated_work(service_db):  # noqa: F811
    ran: list[str] = []

    async def job():
        ran.append("ran")

    unmanaged = RuntimeLifecycle()
    assert await unmanaged.run_gated("goal_check", job) is None
    assert ran == [] and _status_entry("goal_check")["reason"] == "lifecycle_not_started"
    disabled = _lifecycle(scheduler_enabled=False)
    status = await disabled.activate(SCHEDULER_DOMAIN)
    assert status["state"] == "disabled" and status["reason"] == "scheduler_disabled"
    assert await disabled.run_gated("calendar_scan", job) is None
    # Disabled is never "blocked": no block is recorded and nothing announces it.
    assert ran == [] and _status_entry("calendar_scan") == {}
    assert disabled._notified == {}


async def test_retention_jobs_run_while_the_scheduler_domain_is_blocked(service_db):  # noqa: F811
    await _bump(SCHEDULER_DOMAIN, state="blocked", epoch=2)
    lifecycle = _lifecycle()
    status = await lifecycle.activate(SCHEDULER_DOMAIN)
    assert (status["state"], status["reason"]) == ("blocked", "scheduler_owner_blocked")
    ran: list[str] = []

    async def job(name):
        ran.append(name)

    await lifecycle.run_gated("screen_cleanup", lambda: job("screen_cleanup"))
    await lifecycle.run_gated("cordis_mail_retention", lambda: job("cordis_mail_retention"))
    await lifecycle.run_gated("goal_check", lambda: job("goal_check"))
    assert ran == ["screen_cleanup", "cordis_mail_retention"]
    assert _status_entry("goal_check")["status"] == "blocked"
    assert _status_entry("goal_check")["reason"] == "scheduler_owner_blocked"
    assert _status_entry("screen_cleanup") == {}


async def test_a_cordis_owner_without_an_admitting_host_blocks_with_the_fix(service_db):  # noqa: F811
    await _bump(SCHEDULER_DOMAIN, owner_kind="cordis", epoch=2, composition_digest="e" * 64)
    lifecycle = _lifecycle(host=None)
    status = await lifecycle.activate(SCHEDULER_DOMAIN)
    assert status["state"] == "blocked" and status["reason"].startswith("cordis_")
    assert "Cordis runtime" in status["recovery"]
    # No legacy fallback once cut over.
    assert lifecycle.current(SCHEDULER_DOMAIN) is None


# ------------------------------------------------------------------ blocked visibility

async def test_blocked_is_announced_once_repeated_ticks_are_silent_and_success_clears(service_db, monkeypatch):  # noqa: F811
    lifecycle = _lifecycle()
    notices: list[str] = []
    activity: list[str] = []

    async def notify(domain, reason):
        notices.append(reason)

    async def record_activity(domain, outcome, reason):
        activity.append(outcome)

    monkeypatch.setattr(lifecycle, "_notify", notify)
    monkeypatch.setattr(lifecycle, "_activity", record_activity)
    await _bump(SCHEDULER_DOMAIN, state="draining")
    for _ in range(3):
        lifecycle._last_attempt.clear()
        await lifecycle.activate(SCHEDULER_DOMAIN)
    assert notices == ["scheduler_owner_draining"] and activity == ["blocked"]

    async def job():
        return None

    for _ in range(3):
        await lifecycle.run_gated("goal_check", job)
    since = _status_entry("goal_check")["since"]
    assert notices == ["scheduler_owner_draining"]
    # The record survives a restart (a fresh process reads the same status record).
    from src.scheduler import background_inference
    background_inference._status_loaded_from = None
    assert _status_entry("goal_check")["since"] == since
    assert any(item["job_id"] == "goal_check" for item in background_inference.scheduler_job_blocks())
    await _bump(SCHEDULER_DOMAIN, state="ready")
    lifecycle._last_attempt.clear()
    await lifecycle.run_gated("goal_check", job)
    assert activity == ["blocked", "recovered"] and notices == ["scheduler_owner_draining"]
    assert _status_entry("goal_check")["status"] == "succeeded"


# ------------------------------------------------------------------ fences

async def test_a_stale_epoch_between_tick_and_admission_records_no_run_and_contacts_nothing(service_db):  # noqa: F811
    from src.scheduler.scheduled_jobs import scheduled_job_repository
    lifecycle = _lifecycle()
    await lifecycle.activate(SCHEDULER_DOMAIN)
    contacts: list[str] = []

    async def tick():
        assert current_tick().epoch == 1
        # The owner row moves after the tick was admitted at E and before its first effect.
        await _bump(SCHEDULER_DOMAIN, epoch=2)
        await scheduled_job_repository.start_run({"id": "job-1", "name": "Source watch", "trigger_type": "cron",
                                                  "action_type": "run_source_watch"})
        contacts.append("provider")

    assert await lifecycle.run_gated("user_cron:job-1", tick) is None
    assert contacts == []
    async with db_engine.get_session() as db:
        assert await db.scalar(select(func.count()).select_from(ScheduledJobRun)) == 0
    entry = _status_entry("user_cron:job-1")
    assert (entry["status"], entry["reason"]) == ("blocked", "composition_dependency_changed")


async def test_the_fence_is_a_no_op_outside_a_tick_and_exact_inside_one(service_db):  # noqa: F811
    from src.scheduler.scheduled_jobs import scheduled_job_repository
    # API/manual writes are not gated.
    await scheduled_job_repository.start_run({"id": "manual", "name": "manual", "action_type": "x"})
    lifecycle = _lifecycle()
    await lifecycle.activate(DISPATCH_DOMAIN)

    async def tick():
        async with db_engine.get_session() as db:
            await assert_tick_fence(db)
        await _bump(DISPATCH_DOMAIN, state="draining")
        async with db_engine.get_session() as db:
            with pytest.raises(LifecycleFenced):
                await assert_tick_fence(db)
        return "checked"

    assert await lifecycle.run_gated("work_board_dispatch", tick) == "checked"


async def test_run_pass_refuses_at_its_first_step_under_a_stale_dispatch_epoch(service_db):  # noqa: F811
    from src.work_board.dispatcher import WorkBoardDispatcher
    lifecycle = _lifecycle()
    await lifecycle.activate(DISPATCH_DOMAIN)
    await _bump(DISPATCH_DOMAIN, epoch=2)
    dispatcher = WorkBoardDispatcher()
    calls: list[str] = []

    async def never(*args, **kwargs):
        calls.append("inbox")
        return 0

    import src.work_board.dispatcher as module
    original = module.expire_inbox_items
    module.expire_inbox_items = never
    try:
        assert await lifecycle.run_gated("work_board_dispatch", dispatcher.run_pass) is None
    finally:
        module.expire_inbox_items = original
    assert calls == []
    assert _status_entry("work_board_dispatch")["reason"] == "composition_dependency_changed"


async def test_api_launched_workers_refuse_while_dispatch_is_not_active(service_db, monkeypatch):  # noqa: F811
    from src.runtime_plugins import lifecycle as module
    from src.work_board.dispatcher import require_dispatch_active
    from src.work_board.repository import BoardError
    lifecycle = _lifecycle()
    monkeypatch.setattr(module, "runtime_lifecycle", lifecycle)
    with pytest.raises(BoardError) as refused:
        require_dispatch_active()
    assert refused.value.code == "dispatch_owner_not_active"
    await lifecycle.activate(DISPATCH_DOMAIN)
    require_dispatch_active()


# ------------------------------------------------------------------ drain and restart

async def test_drain_waits_then_routes_leftover_attempts_through_their_cleanup_never_a_bare_cancel(service_db):  # noqa: F811
    registry: dict = {}
    calls: list[tuple] = []
    lifecycle = _lifecycle(registry=lambda: registry)

    async def cleanup(task_id, attempt_id, *, reason):
        # Stands in for WorkBoardDispatcher.drain_attempt_past_bound (hook + cancel_job_tree).
        calls.append((task_id, attempt_id, reason))
        worker = registry[(task_id, attempt_id)]
        worker.cancel()
        await asyncio.wait([worker], timeout=1)
        return {"task_id": task_id, "attempt_id": attempt_id, "job_id": "job-" + task_id, "status": "cancelled",
                "cleanup_proven": True, "worker_alive": False}

    lifecycle.attempt_cleanup = cleanup
    await lifecycle.activate(DISPATCH_DOMAIN)
    finished: list[str] = []

    async def quick():
        await asyncio.sleep(0.05)
        finished.append("quick")

    async def stuck():
        await asyncio.sleep(3600)

    registry[("t1", "a1")] = asyncio.create_task(quick())
    registry[("t2", "a2")] = asyncio.create_task(stuck())
    status = await lifecycle.deactivate(DISPATCH_DOMAIN, "dispatch_owner_draining", drain_seconds=0.3)
    assert finished == ["quick"]
    assert calls == [("t2", "a2", "dispatch_drain_timeout")]  # only the attempt past its bound
    assert status["state"] == "blocked" and lifecycle.current(DISPATCH_DOMAIN) is None
    quiesced = [event for event in await _events(QUIESCED_EVENT) if event["domain"] == DISPATCH_DOMAIN]
    assert len(quiesced) == 1 and quiesced[0]["cancelled"] == 1 and quiesced[0]["unknown"] == []

    # Admission is closed during and after the drain: a gated tick does not run.
    ran: list[str] = []

    async def tick():
        ran.append("tick")

    lifecycle._last_attempt[DISPATCH_DOMAIN] = 10**12  # no re-evaluation in this assertion
    await lifecycle.run_gated("work_board_dispatch", tick)
    assert ran == []


async def test_unproven_cleanup_records_unknown_never_audits_quiescence_and_refuses_the_flip(service_db):  # noqa: F811
    from src.runtime_plugins.lifecycle import DRAIN_UNKNOWN_EVENT
    registry: dict = {}
    lifecycle = _lifecycle(registry=lambda: registry)
    release = asyncio.Event()

    async def shielded_child():
        # Like _await_communication_model_call: outlives cancellation until its callback closes.
        while not release.is_set():
            try:
                await asyncio.shield(release.wait())
            except asyncio.CancelledError:
                continue

    async def cleanup(task_id, attempt_id, *, reason):
        registry[(task_id, attempt_id)].cancel()
        return {"task_id": task_id, "attempt_id": attempt_id, "job_id": "job-1", "status": "unknown",
                "cleanup_proven": False, "worker_alive": True}

    lifecycle.attempt_cleanup = cleanup
    await lifecycle.activate(DISPATCH_DOMAIN)
    registry[("t1", "a1")] = worker = asyncio.create_task(shielded_child())
    await asyncio.sleep(0)
    with pytest.raises(LifecycleBlocked) as refused:
        await lifecycle.deactivate(DISPATCH_DOMAIN, "dispatch_owner_draining", drain_seconds=0.05)
    assert refused.value.reason_code == "dispatch_drain_cleanup_unknown"
    assert [event for event in await _events(QUIESCED_EVENT) if event["domain"] == DISPATCH_DOMAIN] == []
    unknown = [event for event in await _events(DRAIN_UNKNOWN_EVENT) if event["domain"] == DISPATCH_DOMAIN]
    assert unknown[0]["unknown"][0]["status"] == "unknown" and unknown[0]["alive"] == 1
    # The domain never activates beside the still-live cancelled worker.
    lifecycle._last_attempt.clear()
    assert (await lifecycle.activate(DISPATCH_DOMAIN))["reason"] == "dispatch_drain_cleanup_unknown"
    release.set()
    await worker
    lifecycle._last_attempt.clear()
    assert (await lifecycle.activate(DISPATCH_DOMAIN))["state"] == "active"


async def test_a_live_transition_with_unknown_drain_never_flips_the_owner(service_db):  # noqa: F811
    from src.runtime_plugins.ownership import inventory
    lifecycle = _lifecycle()
    await lifecycle.activate(SCHEDULER_DOMAIN)

    async def unknown_drain(domain, bound, *, stop_leftovers):
        return {"cancelled": [], "unknown": [{"task_id": "t", "attempt_id": "a", "status": "unknown"}], "alive": []}

    lifecycle._drain = unknown_drain
    with pytest.raises(LifecycleBlocked):
        await lifecycle.transition(SCHEDULER_DOMAIN, "legacy", actor="operator:test", expected_epoch=1,
                                   reason="test")
    async with db_engine.get_session() as db:
        row = (await inventory(db))[SCHEDULER_DOMAIN]
    assert (row.epoch, row.state) == (1, "ready")


def _real_dispatcher(registry: dict, tree: list[dict]):
    """A real WorkBoardDispatcher (real stop sequence, hook and tree readback) over a stub job store."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from src.work_board.dispatcher import WorkBoardDispatcher
    task_row = SimpleNamespace(task_id="t1", capability_id="inference.near-text.v1", typed_input_json="{}")
    attempt_row = SimpleNamespace(task_id="t1", attempt_id="a1", workflow_run_id="job-1")

    class Db:
        async def get(self, model, key):
            return task_row if key == "t1" else attempt_row

    @asynccontextmanager
    async def sessions():
        yield Db()

    class Jobs:
        def __init__(self):
            self.rows = {"job-1": {"job_id": "job-1", "status": "running", "revision": 4,
                                   "lease": {"owner": "dispatcher", "fencing_token": 7}}}
            self.calls: list[str] = []

        async def get_job(self, job_id):
            return dict(self.rows[job_id]) if job_id in self.rows else None

        async def record_checkpoint(self, job_id, *, checkpoint_id, state, owner, fencing_token, expected_revision):
            self.calls.append(f"checkpoint:{checkpoint_id}:{state['reason_code']}")

        async def cancel_job_tree(self, job_id, *, reason):
            self.calls.append(f"tree:{job_id}")
            self.rows["job-1"] = {"job_id": "job-1", "status": "cancelled"}
            for item in tree:
                self.rows[item["job_id"]] = dict(item)
            return [{"job_id": "job-1", "status": "cancelled"}, *tree]

    dispatcher = WorkBoardDispatcher.__new__(WorkBoardDispatcher)
    dispatcher.session_provider = sessions
    dispatcher.jobs = Jobs()
    dispatcher._active_worker_tasks = registry
    return dispatcher


async def test_drain_cleanup_reads_the_tree_back_and_an_uncertain_descendant_is_unknown():
    """The real stop sequence: checkpoint, hook, tree cancel, then the tree readback decides."""
    finished = asyncio.get_running_loop().create_future()
    finished.set_result(None)
    registry = {("t1", "a1"): finished}
    unresolved = _real_dispatcher(registry, [{"job_id": "child-1", "status": "unknown_external_effect"}])
    outcome = await unresolved.drain_attempt_past_bound("t1", "a1")
    # The hook alone passes (worker done) but the tree cancel left an unresolved effect.
    assert unresolved.jobs.calls == ["checkpoint:cancel_requested:dispatch_drain_timeout", "tree:job-1"]
    assert (outcome["status"], outcome["cleanup_proven"]) == ("unknown", False)
    clean = _real_dispatcher(registry, [])
    assert (await clean.drain_attempt_past_bound("t1", "a1"))["status"] == "cancelled"


async def test_an_uncertain_tree_at_the_drain_bound_never_audits_quiescence_or_flips(service_db, monkeypatch):  # noqa: F811
    from src.runtime_plugins import lifecycle as module
    from src.runtime_plugins.lifecycle import DRAIN_UNKNOWN_EVENT
    monkeypatch.setattr(module, "_dispatch_drain_seconds", lambda: 0.2)  # the attempt is past its bound
    from src.runtime_plugins.ownership import inventory
    registry: dict = {}
    dispatcher = _real_dispatcher(registry, [{"job_id": "child-1", "status": "unknown_external_effect"}])
    lifecycle = _lifecycle(registry=lambda: registry)
    lifecycle.attempt_cleanup = dispatcher.drain_attempt_past_bound
    await lifecycle.activate(DISPATCH_DOMAIN)

    async def stuck():
        await asyncio.sleep(3600)

    registry[("t1", "a1")] = asyncio.create_task(stuck())
    with pytest.raises(LifecycleBlocked) as refused:
        await lifecycle.transition(DISPATCH_DOMAIN, "cordis", actor="operator:test", expected_epoch=1,
                                   reason="test")
    assert refused.value.reason_code == "dispatch_drain_cleanup_unknown"
    assert [event for event in await _events(QUIESCED_EVENT) if event["domain"] == DISPATCH_DOMAIN] == []
    unknown = [event for event in await _events(DRAIN_UNKNOWN_EVENT) if event["domain"] == DISPATCH_DOMAIN]
    assert unknown and unknown[0]["unknown"][0]["status"] == "unknown"
    async with db_engine.get_session() as db:
        row = (await inventory(db))[DISPATCH_DOMAIN]
    assert (row.owner_kind, row.epoch, row.state) == ("legacy", 1, "ready")  # no flip
    status = lifecycle.status(DISPATCH_DOMAIN)
    assert status["last_transition"]["status"] == "failed"
    assert status["last_transition"]["reason"] == "dispatch_drain_cleanup_unknown"


async def test_a_scheduler_tick_in_flight_at_deactivation_finishes_within_its_bound(service_db):  # noqa: F811
    lifecycle = _lifecycle()
    await lifecycle.activate(SCHEDULER_DOMAIN)
    release = asyncio.Event()
    done: list[str] = []

    async def tick():
        await release.wait()
        done.append("finished")

    running = asyncio.create_task(lifecycle.run_gated("goal_check", tick))
    await asyncio.sleep(0.01)
    deactivating = asyncio.create_task(lifecycle.deactivate(SCHEDULER_DOMAIN, "scheduler_owner_draining",
                                                            drain_seconds=5.0))
    await asyncio.sleep(0.05)
    assert not deactivating.done() and lifecycle.current(SCHEDULER_DOMAIN) is None
    release.set()
    await asyncio.gather(running, deactivating)
    assert done == ["finished"]
    quiesced = [event for event in await _events(QUIESCED_EVENT) if event["domain"] == SCHEDULER_DOMAIN]
    assert quiesced[-1]["cancelled"] == 0


async def test_restart_activates_at_the_current_epoch_and_replays_nothing(service_db):  # noqa: F811
    from src.scheduler.scheduled_jobs import scheduled_job_repository
    first = _lifecycle()
    await first.activate(SCHEDULER_DOMAIN)

    async def tick():
        await scheduled_job_repository.start_run({"id": "job", "name": "job", "action_type": "x"})

    await first.run_gated("user_cron:job", tick)
    await first.deactivate(SCHEDULER_DOMAIN, "lifecycle_shutdown", drain_seconds=0, mark_jobs=False)
    await _bump(SCHEDULER_DOMAIN, epoch=3)  # a transition happened while the process was down
    second = _lifecycle()
    status = await second.activate(SCHEDULER_DOMAIN)
    assert (status["state"], status["epoch"]) == ("active", 3)
    async with db_engine.get_session() as db:
        assert await db.scalar(select(func.count()).select_from(ScheduledJobRun)) == 1
    activations = [event["epoch"] for event in await _events(ACTIVATED_EVENT) if event["domain"] == SCHEDULER_DOMAIN]
    assert activations == [1, 3]


# ------------------------------------------------------------------ real child: Cordis owner, host loss, live transitions

@needs_cordis
async def test_cordis_owned_scheduler_activates_on_the_boot_proof_and_host_loss_deactivates(accounting_db):  # noqa: F811
    from src.runtime_plugins.cutover import transition_domain
    from src.workflows.job_runtime import DurableJobRepository
    async with research_host(DurableJobRepository()) as host:
        receipt = await transition_domain(SCHEDULER_DOMAIN, "cordis", host=host, actor="test", reason="test")
        assert (receipt["owner_kind"], receipt["epoch"], receipt["state"]) == ("cordis", 2, "ready")
        lifecycle = _lifecycle(host=host)
        status = await lifecycle.activate(SCHEDULER_DOMAIN)
        assert (status["state"], status["owner_kind"], status["epoch"]) == ("active", "cordis", 2)
        dispatch = await lifecycle.activate(DISPATCH_DOMAIN)
        assert (dispatch["state"], dispatch["owner_kind"]) == ("active", "legacy")
        cut = await transition_domain(DISPATCH_DOMAIN, "cordis", host=host, actor="test", reason="test")
        assert (cut["owner_kind"], cut["state"]) == ("cordis", "ready")
        await host.stop()
        # Host loss: the activation is gone at once; no legacy fallback.
        assert lifecycle.current(SCHEDULER_DOMAIN) is None
        assert lifecycle.status(SCHEDULER_DOMAIN)["reason"].startswith("cordis_")
        lifecycle._last_attempt.clear()
        assert (await lifecycle.activate(SCHEDULER_DOMAIN))["state"] == "blocked"


@needs_cordis
async def test_live_transition_drains_at_e_before_the_flip_and_rollback_takes_a_higher_epoch(accounting_db):  # noqa: F811
    from src.runtime_plugins.ownership import inventory
    from src.workflows.job_runtime import DurableJobRepository
    async with research_host(DurableJobRepository()) as host:
        lifecycle = _lifecycle(host=host)
        await lifecycle.activate(SCHEDULER_DOMAIN)
        release = asyncio.Event()
        observed: list[tuple[int, str]] = []

        async def tick():
            await release.wait()
            async with db_engine.get_session() as db:
                row = (await inventory(db))[SCHEDULER_DOMAIN]
                observed.append((row.epoch, row.state))
                await assert_tick_fence(db)  # still the exact ready owner at E: settles at E

        running = asyncio.create_task(lifecycle.run_gated("goal_check", tick))
        await asyncio.sleep(0.01)
        moving = asyncio.create_task(lifecycle.transition(SCHEDULER_DOMAIN, "cordis", actor="operator:test",
                                                          expected_epoch=1, reason="test"))
        await asyncio.sleep(0.05)
        with pytest.raises(CompositionBindingError) as busy:
            lifecycle.claim_transition(SCHEDULER_DOMAIN)  # one transition at a time
        assert busy.value.reason_code == "composition_transition_in_progress"
        assert not moving.done()
        release.set()
        await running
        receipt = await moving
        assert observed == [(1, "ready")]
        assert (receipt["owner_kind"], receipt["epoch"]) == ("cordis", 2)
        status = lifecycle.status(SCHEDULER_DOMAIN)
        assert (status["state"], status["owner_kind"], status["epoch"]) == ("active", "cordis", 2)
        back = await lifecycle.transition(SCHEDULER_DOMAIN, "legacy", actor="operator:test", expected_epoch=2,
                                          reason="rollback")
        assert (back["owner_kind"], back["epoch"]) == ("legacy", 3)
        assert lifecycle.status(SCHEDULER_DOMAIN)["epoch"] == 3
        epochs = [event["epoch"] for event in await _events(ACTIVATED_EVENT) if event["domain"] == SCHEDULER_DOMAIN]
        assert epochs == [1, 2, 3]  # never revived at an older epoch


@needs_cordis
async def test_operator_api_moves_a_lifecycle_domain_as_one_background_drain_step(accounting_db, real_auth,  # noqa: F811
                                                                                 monkeypatch):
    import httpx
    from fastapi import FastAPI
    from src.api import auth, runtime_composition
    from src.auth.middleware import OperatorAuthMiddleware
    from src.runtime_plugins import bridge
    from src.runtime_plugins import lifecycle as module
    from src.workflows.job_runtime import DurableJobRepository
    async with research_host(DurableJobRepository()) as host:
        monkeypatch.setattr(bridge, "cordis_host", host)
        lifecycle = _lifecycle(host=host)
        monkeypatch.setattr(module, "runtime_lifecycle", lifecycle)
        await lifecycle.activate(SCHEDULER_DOMAIN)
        app = FastAPI()
        app.add_middleware(OperatorAuthMiddleware)
        app.include_router(auth.router, prefix="/api/auth")
        app.include_router(runtime_composition.router, prefix="/api")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                     headers={"origin": "http://localhost:3001"}) as client:
            assert (await client.post("/api/auth/login", json={"password": "research-vertical-private-secret"})
                    ).status_code == 200
            path = "/api/runtime/composition/domains/" + SCHEDULER_DOMAIN + "/owner"
            body = {"target": "cordis", "expected_epoch": 1, "confirm_domain": SCHEDULER_DOMAIN}
            stale = await client.post(path, json={**body, "expected_epoch": 9})
            assert stale.status_code == 409 and stale.json()["detail"]["code"] == "composition_owner_changed"
            accepted = await client.post(path, json=body)
            assert accepted.status_code == 202, accepted.text
            assert accepted.json()["transition"]["state"] == "draining"
            busy = await client.post(path, json=body)
            assert busy.status_code == 409
            for _ in range(200):
                if SCHEDULER_DOMAIN not in lifecycle._transitioning:
                    break
                await asyncio.sleep(0.02)
            domains = {item["runtime_domain"]: item for item in (
                await client.get("/api/runtime/composition/domains")).json()["domains"]}
        assert (domains[SCHEDULER_DOMAIN]["owner_kind"], domains[SCHEDULER_DOMAIN]["epoch"]) == ("cordis", 2)
        status = lifecycle.status(SCHEDULER_DOMAIN)
        assert (status["state"], status["epoch"], status["last_transition"]["epoch"]) == ("active", 2, 2)


# ------------------------------------------------------------------ shutdown (critic SHOULD 1 and 2)

async def test_shutdown_closes_admission_first_so_no_tick_reactivates_after_the_drain(service_db):  # noqa: F811
    lifecycle = _lifecycle()
    await lifecycle.activate(SCHEDULER_DOMAIN)
    await lifecycle.activate(DISPATCH_DOMAIN)
    release = asyncio.Event()
    ran: list[str] = []

    async def slow():
        await release.wait()

    async def late():
        ran.append("late")

    in_flight = asyncio.create_task(lifecycle.run_gated("goal_check", slow))
    await asyncio.sleep(0.01)
    stopping = asyncio.create_task(lifecycle.stop())
    await asyncio.sleep(0.01)
    assert lifecycle.closing and lifecycle.current(SCHEDULER_DOMAIN) is None
    lifecycle._last_attempt.clear()  # a tick in the window would otherwise re-evaluate
    await lifecycle.run_gated("calendar_scan", late)
    await lifecycle.run_gated("work_board_dispatch", late)
    assert (await lifecycle.activate(DISPATCH_DOMAIN))["state"] != "active"
    release.set()
    await asyncio.gather(in_flight, stopping)
    assert ran == [] and lifecycle.current(DISPATCH_DOMAIN) is None and not lifecycle.managed
    # Post-stop ticks persist nothing and API launches are refused (not let through).
    assert _status_entry("calendar_scan") == {}
    with pytest.raises(LifecycleBlocked) as refused:
        lifecycle.require_dispatch_active()
    assert refused.value.reason_code == "dispatch_owner_not_active"
    activations = [event for event in await _events(ACTIVATED_EVENT)]
    assert len(activations) == 2  # nothing reactivated during or after the shutdown drain


async def test_shutdown_during_a_live_transition_is_bounded_and_restart_is_consistent(service_db, monkeypatch):  # noqa: F811
    from src.runtime_plugins import lifecycle as module
    from src.runtime_plugins.ownership import inventory
    monkeypatch.setattr(module, "SHUTDOWN_TRANSITION_SECONDS", 2.0)
    lifecycle = _lifecycle()
    await lifecycle.activate(SCHEDULER_DOMAIN)
    never = asyncio.Event()

    async def stuck_tick():
        await never.wait()

    tick = asyncio.create_task(lifecycle.run_gated("goal_check", stuck_tick))
    await asyncio.sleep(0.01)
    moving = asyncio.create_task(lifecycle.transition(SCHEDULER_DOMAIN, "legacy", actor="operator:test",
                                                      expected_epoch=1, reason="test"))
    await asyncio.sleep(0.05)
    assert lifecycle.status(SCHEDULER_DOMAIN)["transition"] == "draining"
    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(lifecycle.stop(), timeout=15)  # not the 120 s scheduler drain bound
    assert asyncio.get_running_loop().time() - started < 10
    assert moving.done() and moving.cancelled()
    tick.cancel()
    await asyncio.gather(tick, return_exceptions=True)
    # The flip never ran: the row is still the ready owner at E, and a restart activates there.
    async with db_engine.get_session() as db:
        row = (await inventory(db))[SCHEDULER_DOMAIN]
    assert (row.owner_kind, row.epoch, row.state) == ("legacy", 1, "ready")
    restarted = _lifecycle()
    assert (await restarted.activate(SCHEDULER_DOMAIN))["epoch"] == 1


async def test_a_crash_after_the_flip_restarts_at_the_new_epoch_only(service_db):  # noqa: F811
    from src.runtime_plugins.cutover import transition_domain
    lifecycle = _lifecycle()
    await lifecycle.activate(SCHEDULER_DOMAIN)
    await lifecycle.deactivate(SCHEDULER_DOMAIN, "scheduler_owner_draining", mark_jobs=False)
    # No host: the Cordis target is recorded blocked at E+1 (pending digest), then rolled back at E+2.
    pending = await transition_domain(SCHEDULER_DOMAIN, "cordis", host=None, actor="test", reason="test")
    assert (pending["epoch"], pending["state"]) == (2, "blocked")
    blocked = _lifecycle()
    assert (await blocked.activate(SCHEDULER_DOMAIN))["reason"] == "scheduler_owner_blocked"
    receipt = await transition_domain(SCHEDULER_DOMAIN, "legacy", host=None, actor="test", reason="rollback")
    assert (receipt["epoch"], receipt["state"]) == (3, "ready")
    # The process dies here, before reactivation; the next boot activates at the new epoch, never E.
    restarted = _lifecycle()
    assert (await restarted.activate(SCHEDULER_DOMAIN))["epoch"] == 3


def test_the_dispatch_introduction_refuses_a_corrupt_row_and_inserts_nothing(tmp_path):
    from sqlalchemy import create_engine, text
    from sqlmodel import SQLModel
    from src.runtime_plugins.ownership import (PRE_DISPATCH_DOMAINS, introduce_dispatch_domain_sync,
                                               legacy_composition_digest)
    engine = create_engine(f"sqlite:///{tmp_path / 'corrupt.db'}")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        for domain in PRE_DISPATCH_DOMAINS:
            connection.execute(text(
                "INSERT INTO runtime_composition_states (runtime_domain, owner_kind, epoch, composition_digest, "
                "state, recovery_receipt_ref) VALUES (:d, 'legacy', 1, :g, 'ready', NULL)"),
                {"d": domain, "g": legacy_composition_digest(domain)})
        connection.execute(text("UPDATE runtime_composition_states SET state = 'bogus' "
                                "WHERE runtime_domain = 'seraph.scheduler.v1'"))
    with pytest.raises(CompositionBindingError) as refused:
        with engine.begin() as connection:
            introduce_dispatch_domain_sync(connection)
    assert refused.value.reason_code == "composition_inventory_invalid"
    with engine.connect() as connection:
        domains = {row[0] for row in connection.execute(text("SELECT runtime_domain FROM runtime_composition_states"))}
        events = connection.execute(text("SELECT COUNT(*) FROM audit_events")).scalar_one()
    assert DISPATCH_DOMAIN not in domains and events == 0
    engine.dispose()


async def test_boot_without_the_cordis_proof_keeps_scheduler_and_dispatch_legacy_and_running(service_db):  # noqa: F811
    from src.runtime_plugins.bridge import CordisHost
    from src.runtime_plugins.cutover import CUTOVER_EVENT, run_boot_cutover
    from src.runtime_plugins.ownership import inventory
    receipts = {item["runtime_domain"]: item for item in await run_boot_cutover(CordisHost())}
    assert receipts[SCHEDULER_DOMAIN] == {"runtime_domain": SCHEDULER_DOMAIN, "action": "deferred",
                                          "reason": "cordis_boot_proof_missing"}
    assert DISPATCH_DOMAIN not in receipts  # waits for its prerequisite; nothing marked
    async with db_engine.get_session() as db:
        rows = await inventory(db)
    for domain in (SCHEDULER_DOMAIN, DISPATCH_DOMAIN):
        assert (rows[domain].owner_kind, rows[domain].epoch, rows[domain].state) == ("legacy", 1, "ready")
    marked = {event["runtime_domain"] for event in await _events(CUTOVER_EVENT)}
    assert SCHEDULER_DOMAIN not in marked and DISPATCH_DOMAIN not in marked  # a later proven boot still cuts over
    lifecycle = _lifecycle()
    assert (await lifecycle.activate(SCHEDULER_DOMAIN))["state"] == "active"
    assert (await lifecycle.activate(DISPATCH_DOMAIN))["state"] == "active"


async def test_cordis_block_recovery_names_the_legacy_rollback_route():
    from src.runtime_plugins.lifecycle import lifecycle_recovery
    text = lifecycle_recovery("cordis_host_unavailable")
    assert "legacy" in text and "/api/runtime/composition/domains/" in text
