"""Provider-free vertical checks for the governed v2 procedure schedule."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from src.db.models import (
    Goal,
    GovernedScheduleBinding,
    GovernedScheduleOccurrence,
    OperatorSession,
    Session,
    ScheduledJob,
    ScheduledJobRun,
    WorkBoardInputArtifact,
    WorkBoardStatus,
    WorkBoardTask,
)
from src.goals.contracts import GoalAdmissionBudget
from src.scheduler.governed_schedules import _procedure_terminal_state, normalize_cadence, reserve_occurrence
from src.scheduler.scheduled_jobs import build_cron_trigger, execute_scheduled_job
from src.work_board.contracts import WorkBoardInputArtifactCreate, WorkBoardOwner
from src.work_board.input_artifacts import prepare_input_artifact
from src.workflows.procedure_service import goal_admission_budget_snapshot


OWNER = "operator:procedure-schedule-tests"
SESSION = "procedure-schedule-session"
GOAL = "procedure-schedule-goal"
JOB = "procedure-schedule-job"
BINDING = "procedure-schedule-binding"
SLOT = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


async def _seed_procedure_schedule(async_db, *, action_overrides: dict[str, object] | None = None) -> None:
    budget = GoalAdmissionBudget(
        reviewed_grant=True,
        grant_id="grant-procedure-schedule",
        max_outstanding_jobs=1,
        max_attempts=1,
        max_runtime_seconds=300,
        period_started_at=SLOT - timedelta(days=1),
        period_expires_at=SLOT + timedelta(days=7),
        timezone="UTC",
    )
    digest = goal_admission_budget_snapshot(goal_id=GOAL, goal_revision=1, budget=budget).digest
    payload = {
        "routine_id": "routine-0123456789abcdef0123456789abcdef",
        "version": 1,
        "expected_routine_revision": 1,
        "goal_id": GOAL,
        "expected_goal_revision": 1,
        "parameters": {"goal_id": GOAL, "expected_goal_revision": 1},
        "invocation_uuid": "schedule-source-invocation",
    }
    now = SLOT - timedelta(hours=1)
    owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)
    async with async_db() as db:
        db.add_all(
            [
                Session(id=SESSION, owner_principal_id=OWNER),
                Goal(
                    id=GOAL,
                    title="Scheduled procedure goal",
                    status="active",
                    revision=1,
                    proactive_enabled=True,
                    owner_principal_id=OWNER,
                    owner_session_id=SESSION,
                    admission_budget_json=budget.model_dump_json(),
                ),
                OperatorSession(
                    id=SESSION,
                    token_hash="procedure-schedule-test-token-hash",
                    idle_expires_at=now + timedelta(days=1),
                    absolute_expires_at=now + timedelta(days=2),
                ),
            ]
        )
        await db.flush()
        source = await prepare_input_artifact(
            db,
            owner,
            WorkBoardInputArtifactCreate(
                schema_version=1,
                capability_id="guardian-routine.v2",
                goal_id=GOAL,
                goal_revision=1,
                input=payload,
                idempotency_key="procedure-schedule-source",
            ),
        )
        action = {
            "binding_id": BINDING,
            "routine_id": payload["routine_id"],
            "version": 1,
            "version_id": "routine-version-1",
            "template_id": "public-browser-check",
            "goal_id": GOAL,
            "goal_revision": 1,
            "parameters": payload["parameters"],
            "consent_kind": "goal_budget",
            "consent_id": None,
            "consent_revision": 1,
            "consent_digest": digest,
            "goal_budget_digest": digest,
            "goal_budget_grant_id": budget.grant_id,
            "goal_budget_max_outstanding_jobs": budget.max_outstanding_jobs,
            "goal_budget_max_attempts": budget.max_attempts,
            "goal_budget_max_runtime_seconds": budget.max_runtime_seconds,
            "goal_budget_period_expires_at": budget.period_expires_at.isoformat(),
        }
        if action_overrides:
            action.update(action_overrides)
        db.add(
            ScheduledJob(
                id=JOB,
                name="Reviewed procedure schedule",
                enabled=True,
                trigger_type="governed",
                trigger_spec_json=json.dumps(
                    {"kind": "hourly", "timezone": "UTC", "daily_hour": None, "daily_minute": None}
                ),
                action_type="guardian.run_procedure.v2",
                action_spec_json=json.dumps(action, sort_keys=True),
                session_id=SESSION,
                created_by_session_id=SESSION,
            )
        )
        db.add(
            GovernedScheduleBinding(
                binding_id=BINDING,
                scheduled_job_id=JOB,
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                goal_id=GOAL,
                goal_revision=1,
                capability_id="guardian.run_procedure.v2",
                action_type="guardian.run_procedure.v2",
                input_artifact_id=source.artifact_id,
                input_digest=source.typed_input_digest,
                action_digest="a" * 64,
                consent_kind="goal_budget",
                read_consent_id=None,
                consent_revision=1,
                consent_digest=digest,
                schedule_idempotency_key="procedure-schedule-key",
                schedule_request_digest="b" * 64,
                cadence_kind="hourly",
                timezone="UTC",
                expires_at=SLOT + timedelta(days=1),
                state="active",
            )
        )


@pytest.mark.asyncio
async def test_procedure_schedule_queues_one_task_and_same_slot_replay_is_idempotent(async_db):
    await _seed_procedure_schedule(async_db)

    await execute_scheduled_job(JOB, scheduled_slot_utc=SLOT)
    await execute_scheduled_job(JOB, scheduled_slot_utc=SLOT)

    async with async_db() as db:
        tasks = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.capability_id == "guardian-routine.v2",
                    WorkBoardTask.goal_id == GOAL,
                )
            )
        ).scalars().all()
        occurrences = (await db.execute(select(GovernedScheduleOccurrence))).scalars().all()
        artifacts = (
            await db.execute(
                select(WorkBoardInputArtifact).where(
                    WorkBoardInputArtifact.capability_id == "guardian-routine.v2",
                )
            )
        ).scalars().all()
        runs = (
            await db.execute(
                select(ScheduledJobRun).where(ScheduledJobRun.scheduled_job_id == JOB)
            )
        ).scalars().all()

        assert len(tasks) == 1
        assert tasks[0].status is WorkBoardStatus.todo
        assert tasks[0].input_artifact_id is not None
        assert len(occurrences) == 1
        assert occurrences[0].state == "running"
        assert occurrences[0].work_board_task_id == tasks[0].task_id
        assert len(artifacts) == 2  # immutable schedule source + one fresh occurrence input
        assert len(runs) == 2
        assert sorted(run.outcome for run in runs) == ["queued", "queued"]


@pytest.mark.asyncio
async def test_procedure_schedule_rejects_coerced_budget_scalars_before_occurrence(async_db):
    await _seed_procedure_schedule(async_db, action_overrides={"goal_budget_max_attempts": "1"})

    await execute_scheduled_job(JOB, scheduled_slot_utc=SLOT)

    async with async_db() as db:
        assert (await db.execute(select(GovernedScheduleOccurrence))).scalars().all() == []
        run = (await db.execute(select(ScheduledJobRun).where(ScheduledJobRun.scheduled_job_id == JOB))).scalar_one()
        assert run.outcome == "deferred"
        assert json.loads(run.metadata_json or "{}")["recovery_action"] == "refresh_goal_budget"


@pytest.mark.asyncio
async def test_procedure_schedule_does_not_treat_board_review_as_verified_success(async_db):
    await _seed_procedure_schedule(async_db)
    await execute_scheduled_job(JOB, scheduled_slot_utc=SLOT)

    next_slot = SLOT + timedelta(hours=1)
    async with async_db() as db:
        task = (await db.execute(select(WorkBoardTask))).scalar_one()
        task.status = WorkBoardStatus.review
        await db.flush()
        binding = await db.get(GovernedScheduleBinding, BINDING)
        assert binding is not None
        active = (await db.execute(select(GovernedScheduleOccurrence))).scalar_one()
        assert active.state == "running"
        assert active.binding_id == BINDING
        assert active.work_board_task_id == task.task_id
        active_binding = await db.get(GovernedScheduleBinding, active.binding_id)
        assert active_binding is not None
        assert active_binding.action_type == "guardian.run_procedure.v2"
        assert binding.action_type == "guardian.run_procedure.v2"
        assert await _procedure_terminal_state(db, active, task) == (
            "blocked",
            "procedure_task_review_required",
            "review_procedure_task",
        )
        _new_occurrence, replay = await reserve_occurrence(db, binding, slot_utc=next_slot)
        assert replay is False
        prior = (
            await db.execute(
                select(GovernedScheduleOccurrence).where(
                    GovernedScheduleOccurrence.slot_utc == SLOT.replace(tzinfo=None),
                )
            )
        ).scalar_one()
        assert prior.state == "blocked"
        assert json.loads(prior.metadata_json or "{}")["failure_code"] == "procedure_task_review_required"


def test_procedure_schedule_uses_governed_cadence_trigger():
    cadence = {"kind": "6h", "timezone": "UTC", "daily_hour": None, "daily_minute": None}
    assert normalize_cadence(cadence)["kind"] == "6h"
    trigger = build_cron_trigger(
        {"action_type": "guardian.run_procedure.v2", "trigger_spec": cadence}
    )
    assert str(trigger.timezone) == "UTC"
