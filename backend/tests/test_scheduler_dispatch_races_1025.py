"""#1025 (A3.2) closing slice: the D2 status table, in-transaction ``run_pass`` step
fences and races on a real governed occurrence and a real WorkBoard claim.

Every test uses a temporary SQLite file database built by the production
engine factory (``db``), seeded with the legacy owner inventory. External
transport and inference are never reached: nothing here calls a provider.
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta, timezone

import pytest
from sqlalchemy import func, select, update

from config.settings import settings
from src.db import engine as db_engine
from src.db.models import (AuditEvent, BackgroundJobStatus, GovernedScheduleBinding, GovernedScheduleOccurrence,
                           RuntimeCompositionState, WorkBoardAttempt, WorkBoardStatus, WorkBoardTask)
from src.runtime_plugins.lifecycle import LifecycleFenced, RuntimeLifecycle, current_tick, fenced_step
from src.runtime_plugins.ownership import DISPATCH_DOMAIN, SCHEDULER_DOMAIN
from src.scheduler import job_status
from tests.cordis_research import seed_composition_inventory
from tests.support.clock import fake_clock  # noqa: F401

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def lifecycle_db(db, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    await seed_composition_inventory()
    yield db


def _lifecycle() -> RuntimeLifecycle:
    lifecycle = RuntimeLifecycle()
    lifecycle.managed = True
    lifecycle.worker_registry = lambda: {}
    return lifecycle


async def _bump(domain: str, **values) -> None:
    """Another owner moved the row (as a fenced transition would): an outside writer, never under a step guard."""
    with db_engine.commit_guard(None):
        async with db_engine.get_session() as db:
            await db.execute(update(RuntimeCompositionState).where(RuntimeCompositionState.runtime_domain == domain)
                             .values(**values))


async def _count(model, *where) -> int:
    async with db_engine.get_session() as db:
        return int(await db.scalar(select(func.count()).select_from(model).where(*where)))


# ------------------------------------------------------------------ D2: the status table


async def test_the_legacy_json_status_is_folded_into_the_table_once_and_removed(lifecycle_db):
    path = job_status.legacy_status_path()
    path.write_text(json.dumps({"schema_version": job_status.LEGACY_SCHEMA_VERSION, "jobs": {
        "daily_briefing": {"status": "blocked", "reason": "cordis_host_unavailable", "recovery": "Start Cordis.",
                           "at": "2026-11-01T08:00:00+00:00", "since": "2026-11-01T07:00:00+00:00"},
        "goal_check": {"status": "skipped", "reason": "missed_fire", "at": "2026-11-01T08:00:00+00:00",
                       "source": "lifecycle"},
        "not_a_job": {"status": "blocked", "reason": "x", "at": "2026-11-01T08:00:00+00:00"},
    }}), encoding="utf-8")
    path.with_suffix(".tmp").write_text("{", encoding="utf-8")
    rows = await job_status.statuses()
    assert set(rows) == {"daily_briefing", "goal_check"}
    assert (rows["daily_briefing"]["state"], rows["daily_briefing"]["reason"]) == ("blocked", "cordis_host_unavailable")
    assert rows["daily_briefing"]["since"] == "2026-11-01T07:00:00+00:00"
    assert rows["goal_check"]["source"] == "lifecycle"
    # One status record: the file is gone and a block carried over is not announced again.
    assert not path.exists() and not path.with_suffix(".tmp").exists()
    assert (await job_status.status_row("daily_briefing"))["notified_for"] == "2026-11-01T07:00:00+00:00"
    state = await job_status.record_blocked("daily_briefing", "cordis_host_unavailable", "Start Cordis.",
                                            source=job_status.SOURCE_GUARDIAN)
    assert state["notify"] is False and state["transition"] is False


async def test_an_unreadable_legacy_status_file_is_left_in_place_never_deleted(lifecycle_db):
    path = job_status.legacy_status_path()
    path.write_text("{not json", encoding="utf-8")
    assert await job_status.statuses() == {}
    assert path.read_text(encoding="utf-8") == "{not json"
    # Writes still work; the unreadable file is not retried on every write.
    await job_status.record_missed("goal_check")
    assert path.exists() and (await job_status.status_row("goal_check"))["reason"] == "missed_fire"


async def test_repeated_identical_skips_take_no_writer_lock(lifecycle_db, monkeypatch):
    """A job polled every second reads its status; only a transition or a due audit writes."""
    interval = timedelta(hours=1)
    assert await job_status.record_skip("screenshot_folder_analysis", "disabled_by_operator",
                                        audit_interval=interval) is True
    writes: list[str] = []
    original = job_status._Write.__aenter__

    async def counted(self):
        writes.append(self.job_id)
        return await original(self)

    monkeypatch.setattr(job_status._Write, "__aenter__", counted)
    for _ in range(5):
        assert await job_status.record_skip("screenshot_folder_analysis", "disabled_by_operator",
                                            audit_interval=interval) is False
    await job_status.record_run("never-skipped")
    assert writes == []
    await job_status.record_run("screenshot_folder_analysis")  # clears the audit marker: one write
    assert writes == ["screenshot_folder_analysis"]
    assert (await job_status.status_row("screenshot_folder_analysis"))["audited_at"] is None


async def test_the_table_is_newer_than_a_late_legacy_file(lifecycle_db):
    await job_status.record_skip("daily_briefing", "disabled_by_operator", audit_interval=timedelta(hours=1))
    job_status._legacy_folded.clear()  # a fresh process
    job_status.legacy_status_path().write_text(json.dumps({"schema_version": job_status.LEGACY_SCHEMA_VERSION,
        "jobs": {"daily_briefing": {"status": "blocked", "reason": "stale", "at": "2020-01-01T00:00:00+00:00"}}}))
    row = await job_status.status_row("daily_briefing")
    assert (row["state"], row["reason"]) == ("skipped", "disabled_by_operator")
    assert not job_status.legacy_status_path().exists()


async def test_status_writes_are_monotonic_in_the_epoch(lifecycle_db):
    await job_status.record_blocked("goal_check", "scheduler_owner_blocked", "Finish it.",
                                    source=job_status.SOURCE_LIFECYCLE, epoch=3)
    # A stale tick at E=2 finishing late neither clears nor rewrites the E=3 block.
    assert await job_status.record_ok("goal_check", only_source=job_status.SOURCE_LIFECYCLE, epoch=2) is False
    stale = await job_status.record_blocked("goal_check", "composition_dependency_changed", "x",
                                            source=job_status.SOURCE_LIFECYCLE, epoch=2)
    assert stale == {"transition": False, "activity_due": False, "notify": False}
    row = await job_status.status_row("goal_check")
    assert (row["state"], row["reason"], row["epoch"]) == ("blocked", "scheduler_owner_blocked", 3)
    # A write that names no epoch keeps the stored one; the live epoch clears the block.
    await job_status.record_missed("goal_check")
    assert (await job_status.status_row("goal_check"))["epoch"] == 3
    assert await job_status.record_ok("goal_check", only_source=job_status.SOURCE_LIFECYCLE, epoch=4) is True
    row = await job_status.status_row("goal_check")
    assert (row["state"], row["epoch"]) == ("ok", 4) and row["last_success_at"]


async def test_one_notification_per_blocked_transition_even_for_concurrent_writers(lifecycle_db):
    results = await asyncio.gather(*(job_status.record_blocked(
        "lifecycle:dispatch", "cordis_host_unavailable", "Start Cordis.", source=job_status.SOURCE_LIFECYCLE)
        for _ in range(8)))
    assert sum(item["notify"] for item in results) == 1
    assert await _count(BackgroundJobStatus) == 1
    assert await job_status.record_ok("lifecycle:dispatch") is True
    again = await job_status.record_blocked("lifecycle:dispatch", "cordis_host_unavailable", "Start Cordis.",
                                            source=job_status.SOURCE_LIFECYCLE)
    assert again["notify"] is True  # a new transition into blocked


async def test_a_disabled_scheduler_is_recorded_disabled_never_blocked(lifecycle_db):
    lifecycle = _lifecycle()
    lifecycle.scheduler_enabled = False
    notices: list[str] = []

    async def notify(domain, reason):
        notices.append(reason)

    lifecycle._notify = notify
    status = await lifecycle.activate(SCHEDULER_DOMAIN)
    assert status["state"] == "disabled"
    row = await job_status.status_row("lifecycle:scheduler")
    assert (row["state"], row["reason"]) == ("disabled", "scheduler_disabled") and notices == []


# ------------------------------------------------------------------ fences inside each run_pass step


async def test_a_step_transaction_that_writes_after_the_owner_moved_commits_nothing(lifecycle_db):
    lifecycle = _lifecycle()
    await lifecycle.activate(DISPATCH_DOMAIN)
    async with lifecycle.dispatch_launch():
        with fenced_step():
            async with db_engine.get_session() as db:
                await db.scalar(select(func.count()).select_from(AuditEvent))  # reads are never fenced
            # The row moves after the step's boundary check, before the step's own write transaction.
            await _bump(DISPATCH_DOMAIN, epoch=2)
            async with db_engine.get_session() as db:
                await db.scalar(select(func.count()).select_from(AuditEvent))  # a read-only step still runs
            with pytest.raises(LifecycleFenced) as fenced:
                async with db_engine.get_session() as db:
                    db.add(AuditEvent(session_id=None, actor="runtime", event_type="step_write_1025",
                                      summary="a maintenance write"))
            assert fenced.value.reason_code == "composition_dependency_changed"
            # An explicit mid-scope commit is fenced too.
            with pytest.raises(LifecycleFenced):
                async with db_engine.get_session() as db:
                    db.add(AuditEvent(session_id=None, actor="runtime", event_type="step_write_1025", summary="x"))
                    await db.commit()
            # Status writes are deliberately unfenced: a block must land while the owner is not ready.
            await job_status.record_blocked("work_board_dispatch", "composition_dependency_changed", "x",
                                            source=job_status.SOURCE_LIFECYCLE)
    assert await _count(AuditEvent, AuditEvent.event_type == "step_write_1025") == 0
    assert (await job_status.status_row("work_board_dispatch"))["state"] == "blocked"
    # Outside a tick nothing is fenced (API and manual writes).
    with fenced_step():
        async with db_engine.get_session() as db:
            db.add(AuditEvent(session_id=None, actor="runtime", event_type="step_write_1025", summary="manual"))
    assert await _count(AuditEvent, AuditEvent.event_type == "step_write_1025") == 1


async def test_run_pass_refuses_a_step_whose_own_transaction_starts_after_the_owner_moved(lifecycle_db, monkeypatch):
    """The owner flips inside a step, after that step's boundary check: its write is rolled back."""
    from src.work_board import dispatcher as module
    from src.work_board.dispatcher import WorkBoardDispatcher
    lifecycle = _lifecycle()
    await lifecycle.activate(DISPATCH_DOMAIN)
    dispatcher = WorkBoardDispatcher()
    reached: list[str] = []

    async def no_inbox(*args, **kwargs):
        return 0

    async def expire_reviews(*, now, limit=100):
        reached.append("reviews")
        await _bump(DISPATCH_DOMAIN, state="draining")
        async with db_engine.get_session() as db:
            db.add(AuditEvent(session_id=None, actor="runtime", event_type="review_expiry_1025", summary="expired"))
        return 1

    async def later_step(*args, **kwargs):
        reached.append("later")
        return []

    monkeypatch.setattr(module, "expire_inbox_items", no_inbox)
    monkeypatch.setattr(module, "repair_inbox_dispositions", no_inbox)
    monkeypatch.setattr(dispatcher, "_expire_review_windows", expire_reviews)
    monkeypatch.setattr(dispatcher, "recover_expired_near_finance", later_step)
    monkeypatch.setattr(dispatcher, "reconcile_pending_attempts", later_step)
    assert await lifecycle.run_gated("work_board_dispatch", dispatcher.run_pass) is None
    assert reached == ["reviews"]
    assert await _count(AuditEvent, AuditEvent.event_type == "review_expiry_1025") == 0
    row = await job_status.status_row("work_board_dispatch")
    assert (row["state"], row["reason"]) == ("blocked", "composition_dependency_changed")


# ------------------------------------------------------------------ races on a real governed occurrence


async def _seed_binding() -> GovernedScheduleBinding:
    from tests.test_calendar_governed_schedules import NOW, _seed_binding

    class _Factory:
        def __call__(self):
            return db_engine.get_session()

    await _seed_binding(_Factory())
    async with db_engine.get_session() as db:
        binding = (await db.execute(select(GovernedScheduleBinding))).scalar_one()
    return binding, NOW


async def test_racing_scheduler_ticks_reserve_one_occurrence_and_a_stale_epoch_reserves_none(lifecycle_db):
    from src.scheduler.governed_schedules import claim_occurrence, reserve_occurrence
    binding, now = await _seed_binding()
    lifecycle = _lifecycle()
    await lifecycle.activate(SCHEDULER_DOMAIN)
    # Both racers are admitted before either touches the store.
    start = asyncio.Barrier(2)

    async def tick():
        await start.wait()
        async with db_engine.get_session() as db:
            occurrence, replay = await reserve_occurrence(db, binding, slot_utc=now, now_utc=now)
            return occurrence.occurrence_id, replay

    # Two activations' worth of ticks (a duplicate fire, a restart during the tick) race on one slot.
    first = asyncio.create_task(lifecycle.run_gated("user_cron:race-a", tick))
    second = asyncio.create_task(lifecycle.run_gated("user_cron:race-b", tick))
    results = await asyncio.gather(first, second)
    assert len({occurrence_id for occurrence_id, _ in results}) == 1
    assert sorted(replay for _, replay in results) == [False, True]
    assert await _count(GovernedScheduleOccurrence) == 1

    claims: list[str] = []

    async def claim():
        async with db_engine.get_session() as db:
            occurrence = (await db.execute(select(GovernedScheduleOccurrence))).scalar_one()
            try:
                await claim_occurrence(db, occurrence, now_utc=now)
                claims.append("claimed")
            except (RuntimeError, ValueError) as exc:
                claims.append(str(exc))

    await asyncio.gather(lifecycle.run_gated("user_cron:claim-a", claim),
                         lifecycle.run_gated("user_cron:claim-b", claim))
    assert claims.count("claimed") == 1, claims
    # The loser lost on the token fence or saw the running receipt; nothing else counts as losing.
    assert set(claims) - {"claimed"} <= {"governed_occurrence_claim_stale", "governed occurrence is not claimable"}

    # The owner moves between a tick's admission and its reservation: nothing is reserved.
    async def stale_tick():
        assert current_tick().epoch == 1
        await _bump(SCHEDULER_DOMAIN, epoch=2)
        async with db_engine.get_session() as db:
            await reserve_occurrence(db, binding, slot_utc=now + timedelta(hours=1), now_utc=now + timedelta(hours=1))

    assert await lifecycle.run_gated("user_cron:race-a", stale_tick) is None
    assert await _count(GovernedScheduleOccurrence) == 1
    row = await job_status.status_row("user_cron:race-a")
    assert (row["state"], row["reason"]) == ("blocked", "composition_dependency_changed")


# ------------------------------------------------------------------ races on a real WorkBoard claim


async def _ready_tasks(count: int) -> list[str]:
    from tests.test_evidence_dependency_tokens import canonical_fact
    async with db_engine.get_session() as db:
        owner, task, _memory, _source = await canonical_fact(db)
        ids = [task.task_id]
        task.status = WorkBoardStatus.ready
        task.capability_id = "work.evidence-dossier.v1"
        task.executor_id = "seraph-work-board:work.evidence-dossier.v1"
        for index in range(1, count):
            extra = WorkBoardTask(task_id=f"bound-task-{index}", owner_principal_id=owner.principal_id,
                                  owner_session_id=owner.session_id, goal_id=task.goal_id,
                                  capability_id=task.capability_id, executor_id=task.executor_id,
                                  idempotency_key=f"bound-task-{index}",
                                  typed_input_digest="a" * 64, status=WorkBoardStatus.ready)
            db.add(extra)
            ids.append(extra.task_id)
    return ids


async def test_racing_dispatch_claims_create_one_attempt_and_a_stale_epoch_claims_none(lifecycle_db, monkeypatch):
    from unittest.mock import AsyncMock
    from src.work_board.repository import BoardError, WorkBoardRepository
    monkeypatch.setattr(WorkBoardRepository, "_safe_text", AsyncMock(side_effect=lambda value, **kw: value))
    raced, stale = await _ready_tasks(2)
    lifecycle = _lifecycle()
    await lifecycle.activate(DISPATCH_DOMAIN)
    repository = WorkBoardRepository()
    # Both racers are admitted before either touches the store.
    start = asyncio.Barrier(2)
    outcomes: list[str] = []

    async def claim(runner: str, task_id: str, *, race: bool = True):
        if race:
            await start.wait()
        async with db_engine.get_session() as db:
            task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
            try:
                result = await repository.claim_ready_task(db, task_id, expected_revision=task.task_revision,
                                                           lease_owner=runner)
            except BoardError as exc:
                outcomes.append(exc.code)
                return
            outcomes.append("claimed" if result is not None else "none")

    async def tick(runner: str):
        await claim(runner, raced)

    first = asyncio.create_task(lifecycle.run_gated("work_board_dispatch", lambda: tick("runner-a")))
    second = asyncio.create_task(lifecycle.run_gated("work_board_dispatch", lambda: tick("runner-b")))
    await asyncio.gather(first, second)
    async with db_engine.get_session() as db:
        dbg = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == raced))
    assert outcomes.count("claimed") == 1, (outcomes, dbg.status, dbg.block_kind, dbg.block_reason)
    assert await _count(WorkBoardAttempt, WorkBoardAttempt.task_id == raced) == 1

    async def stale_tick():
        await _bump(DISPATCH_DOMAIN, epoch=2)
        await claim("runner-a", stale, race=False)

    assert await lifecycle.run_gated("work_board_dispatch", stale_tick) is None
    assert await _count(WorkBoardAttempt, WorkBoardAttempt.task_id == stale) == 0
    async with db_engine.get_session() as db:
        assert (await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == stale))).status == WorkBoardStatus.ready  # untouched, still claimable


# ------------------------------------------------------------------ a pending approval at cutover (real child)

from tests.cordis_research import needs_cordis, research_host  # noqa: E402
from tests.test_inference_accounting import accounting_db  # noqa: E402,F401


@needs_cordis
async def test_a_pending_approval_never_holds_a_dispatch_cutover_open_and_resumes_after_it(accounting_db,  # noqa: F811
                                                                                          monkeypatch):
    from unittest.mock import AsyncMock
    from src.work_board.repository import WorkBoardRepository
    from src.workflows.job_runtime import DurableJobRepository
    monkeypatch.setattr(WorkBoardRepository, "_safe_text", AsyncMock(side_effect=lambda value, **kw: value))
    (task_id,) = await _ready_tasks(1)
    # The task waits on an operator approval: durable and visible, owned by no live worker.
    with db_engine.commit_guard(None):
        async with db_engine.get_session() as db:
            task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
            task.status, task.block_kind, task.block_reason = WorkBoardStatus.blocked, "needs_input", "awaiting_approval"
    async with research_host(DurableJobRepository()) as host:
        lifecycle = _lifecycle()
        lifecycle.host = host
        assert (await lifecycle.activate(DISPATCH_DOMAIN))["state"] == "active"
        # Manifest order: the scheduler is cut over before dispatch.
        await lifecycle.transition(SCHEDULER_DOMAIN, "cordis", actor="operator:test", expected_epoch=1, reason="cutover")
        receipt = await lifecycle.transition(DISPATCH_DOMAIN, "cordis", actor="operator:test", expected_epoch=1,
                                             reason="cutover with a pending approval")
        assert (receipt["owner_kind"], receipt["epoch"], receipt["state"]) == ("cordis", 2, "ready")
        status = lifecycle.status(DISPATCH_DOMAIN)
        assert (status["state"], status["owner_kind"], status["epoch"]) == ("active", "cordis", 2)
        async with db_engine.get_session() as db:
            task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
            assert (task.status, task.block_reason) == (WorkBoardStatus.blocked, "awaiting_approval")
            # The operator answers the approval after the cutover: the task is Ready again.
            task.status, task.block_kind, task.block_reason = WorkBoardStatus.ready, None, None
        claimed: list[object] = []

        async def tick():
            assert current_tick().epoch == 2
            async with db_engine.get_session() as db:
                task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
                claimed.append(await WorkBoardRepository().claim_ready_task(
                    db, task_id, expected_revision=task.task_revision, lease_owner="runner-after-cutover"))

        await lifecycle.run_gated("work_board_dispatch", tick)
        assert claimed and claimed[0] is not None
        assert await _count(WorkBoardAttempt, WorkBoardAttempt.task_id == task_id) == 1


# ------------------------------------------------------------------ controlled clock: priority without starvation


async def test_dispatch_ticks_keep_priority_order_and_never_starve_lower_or_scheduled_work(lifecycle_db, fake_clock,
                                                                                         monkeypatch):
    """Under the lifecycle, a controlled clock drives the passes: priority first, lower work still runs,
    a scheduled task waits for its time, and board capacity (unchanged by #1025) is released on settle."""
    from unittest.mock import AsyncMock
    from src.work_board.repository import WorkBoardRepository
    monkeypatch.setattr(WorkBoardRepository, "_safe_text", AsyncMock(side_effect=lambda value, **kw: value))
    _first, low, high, later = await _ready_tasks(4)
    now = fake_clock.now(timezone.utc)
    report = ("work.local-evidence-report.v1", "seraph-work-board:work.local-evidence-report.v1")
    with db_engine.commit_guard(None):
        async with db_engine.get_session() as db:
            first = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == _first))
            first.status = WorkBoardStatus.todo  # not part of this ordering
            for task_id, priority, scheduled in ((low, 20, None), (high, 90, None),
                                                 (later, 99, (now + timedelta(minutes=5)).replace(tzinfo=None))):
                task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
                task.priority, task.scheduled_at = priority, scheduled
                if task_id == low:
                    # A different executor: the one-running-per-executor capacity does not hold it back.
                    task.capability_id, task.executor_id = report
    lifecycle = _lifecycle()
    await lifecycle.activate(DISPATCH_DOMAIN)
    repository = WorkBoardRepository()
    order: list[str] = []

    async def tick():
        observed = fake_clock.now(timezone.utc)
        assert current_tick() is not None
        async with db_engine.get_session() as db:
            candidates = await repository.list_dispatch_candidates(db, now=observed)
        for head in candidates:
            async with db_engine.get_session() as db:
                claim = await repository.claim_ready_task(db, head.task_id, expected_revision=head.task_revision,
                                                          lease_owner="runner", now=observed)
            if claim is not None:
                order.append(head.task_id)
                return

    await lifecycle.run_gated("work_board_dispatch", tick)
    await lifecycle.run_gated("work_board_dispatch", tick)
    await lifecycle.run_gated("work_board_dispatch", tick)  # the scheduled task is not due yet
    assert order == [high, low]
    fake_clock.advance(301)
    await lifecycle.run_gated("work_board_dispatch", tick)
    # Due, but board capacity (two running, one per executor) is full: it waits, it is never skipped.
    assert order == [high, low]
    with db_engine.commit_guard(None):
        async with db_engine.get_session() as db:
            (await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == high))).status = WorkBoardStatus.done
    await lifecycle.run_gated("work_board_dispatch", tick)
    assert order == [high, low, later]
