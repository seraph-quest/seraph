"""Provider-free vertical checks for the governed v2 procedure schedule."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from src.db.models import (
    Goal,
    GovernedScheduleBinding,
    GovernedScheduleOccurrence,
    GuardianRoutine,
    GuardianRoutineVersion,
    OperatorSession,
    Session,
    ScheduledJob,
    ScheduledJobRun,
    WorkBoardAttempt,
    WorkBoardInputArtifact,
    WorkBoardStatus,
    WorkBoardTask,
    WorkflowRunState,
)
from src.goals.contracts import GoalAdmissionBudget
from src.scheduler.governed_schedules import _procedure_terminal_state, normalize_cadence, reserve_occurrence
from src.scheduler import scheduled_jobs as scheduled_jobs_module
from src.scheduler.scheduled_jobs import (
    _load_governed_procedure_authority,
    build_cron_trigger,
    execute_scheduled_job,
)
from src.work_board.contracts import WorkBoardInputArtifactCreate, WorkBoardOwner
from src.work_board.input_artifacts import prepare_input_artifact
from src.work_board.repository import WorkBoardRepository
from src.workflows.procedure_service import goal_admission_budget_snapshot


OWNER = "operator:procedure-schedule-tests"
SESSION = "procedure-schedule-session"
GOAL = "procedure-schedule-goal"
JOB = "procedure-schedule-job"
BINDING = "procedure-schedule-binding"
SLOT = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


@pytest.fixture(autouse=True)
def provider_free_package_readback(monkeypatch: pytest.MonkeyPatch):
    """Keep SQL authority tests local while native tests use real packages."""

    monkeypatch.setattr(
        "src.workflows.routines.routine_service._package_readback",
        lambda *_args, **_kwargs: {"status": "active", "digest": "a" * 64},
    )


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
        "invocation_uuid": "schedule:procedure-schedule-source",
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
                    idle_expires_at=now + timedelta(days=5),
                    absolute_expires_at=now + timedelta(days=7),
                ),
                GuardianRoutine(
                    id=payload["routine_id"],
                    owner_principal_id=OWNER,
                    owner_session_id=SESSION,
                    name="Scheduled procedure fixture",
                    state="active",
                    revision=1,
                    current_version=1,
                ),
                GuardianRoutineVersion(
                    id="routine-version-1",
                    routine_id=payload["routine_id"],
                    version=1,
                    source_provenance_json=json.dumps(
                        {
                            "schema_version": 2,
                            "template_id": "public-browser-check",
                            "plan_digest": "b" * 64,
                            "source_proof_digest": "c" * 64,
                        },
                        sort_keys=True,
                    ),
                    installed_package_digest="a" * 64,
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
                idempotency_key="schedule:procedure-schedule-source",
            ),
            now=now,
            retention_deadline=SLOT + timedelta(days=3),
        )
        action = {
            "binding_id": BINDING,
            "routine_id": payload["routine_id"],
            "version": 1,
            "version_id": "routine-version-1",
            "template_id": "public-browser-check",
            "routine_revision": 1,
            "plan_digest": "b" * 64,
            "source_proof_digest": "c" * 64,
            "package_digest": "a" * 64,
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
                expires_at=SLOT + timedelta(days=3),
                state="active",
            )
        )


@pytest.mark.parametrize(
    "seed_expiry",
    (SLOT + timedelta(days=1), SLOT + timedelta(days=2)),
    ids=("expired-seed", "shorter-than-binding"),
)
async def test_procedure_schedule_rejects_expired_or_short_seed_before_contact(async_db, seed_expiry):
    await _seed_procedure_schedule(async_db)
    observed = SLOT + timedelta(hours=25)
    async with async_db() as db:
        binding = await db.get(GovernedScheduleBinding, BINDING)
        assert binding is not None
        artifact = await db.get(WorkBoardInputArtifact, binding.input_artifact_id)
        assert artifact is not None
        artifact.expires_at = seed_expiry
        await db.commit()

        with pytest.raises(RuntimeError, match="procedure_schedule_input_artifact_invalid"):
            await _load_governed_procedure_authority(
                db,
                {"id": JOB},
                BINDING,
                now=observed,
                slot_utc=observed,
            )


async def _seed_terminal_native_parent(
    async_db,
    *,
    authority_overrides: dict[str, object] | None = None,
    effect_mode: str = "verified",
) -> dict[str, str]:
    """Build a real scheduled wrapper plus its distinct native procedure root."""

    await _seed_procedure_schedule(async_db)
    await execute_scheduled_job(JOB, scheduled_slot_utc=SLOT)
    parent_id = "procedure-native-parent-root"
    attempt_id = "procedure-native-parent-attempt"
    async with async_db() as db:
        task = (await db.execute(select(WorkBoardTask))).scalar_one()
        occurrence = (await db.execute(select(GovernedScheduleOccurrence))).scalar_one()
        assert occurrence.durable_job_id
        assert occurrence.durable_job_id != parent_id
        task.status = WorkBoardStatus.done
        task.task_revision = 2
        artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id)
        assert artifact is not None
        artifact.state = "consumed"
        now = SLOT - timedelta(minutes=30)
        authority: dict[str, object] = {
            "principal": OWNER,
            "owner_kind": "user",
            "session_id": SESSION,
            "operator_session_id": SESSION,
            "goal_id": GOAL,
            "goal_revision": 1,
            "capability_id": "guardian-routine.v2",
            "routine_id": "routine-0123456789abcdef0123456789abcdef",
            "routine_version": 1,
            "routine_revision": 1,
            "package_digest": "a" * 64,
            "template_id": "public-browser-check",
            "plan_digest": "b" * 64,
            "invocation_uuid": "schedule:procedure-native-parent",
            "board_task_id": task.task_id,
            "board_attempt_id": attempt_id,
            # claim_ready_task stores 1 as task_revision_at_claim and moves
            # the running task to revision 2 before ProcedureV2 admission.
            "board_task_revision": 2,
            "board_fencing_token": 7,
            "input_artifact_id": task.input_artifact_id,
            "input_artifact_digest": task.typed_input_digest,
        }
        authority.update(authority_overrides or {})
        readback = {
            "effect_id": "procedure-native-parent-effect",
            "effect_type": "guardian_routine_v2_parent",
            "receipt_kind": "readback",
            "status": "succeeded",
            "workflow_run_id": parent_id,
            "content_sha256": "c" * 64,
            "readback_id": "procedure-native-parent-readback",
            "verified_at": now.isoformat(),
        }
        if effect_mode == "missing":
            effects: list[dict[str, object]] = []
            run_status = "succeeded"
        elif effect_mode == "unknown":
            readback["status"] = "unknown"
            effects = [readback]
            run_status = "unknown_external_effect"
        else:
            effects = [readback]
            run_status = "succeeded"
        db.add(
            WorkflowRunState(
                run_identity=parent_id,
                root_run_identity=parent_id,
                workflow_name="guardian_routine_v2",
                tool_name="guardian-routine.v2",
                session_id=SESSION,
                operator_session_id=SESSION,
                status=run_status,
                run_fingerprint="d" * 64,
                input_digest="e" * 64,
                authority_digest="f" * 64,
                job_kind="guardian_routine_v2",
                owner_kind="user",
                owner_principal_id=OWNER,
                goal_id=GOAL,
                goal_revision=1,
                capability_version="guardian-routine.v2",
                idempotency_scope="work-board-attempt",
                idempotency_key=f"{task.task_id}:{attempt_id}",
                declared_authority_json=json.dumps(authority, sort_keys=True),
                deadline_at=now + timedelta(hours=1),
                effect_receipts_json=json.dumps(effects, sort_keys=True),
                started_at=now,
                updated_at=now,
                finished_at=now,
            )
        )
        db.add(
            WorkBoardAttempt(
                attempt_id=attempt_id,
                task_id=task.task_id,
                workflow_run_id=parent_id,
                task_revision_at_claim=1,
                lease_owner="procedure-native-dispatcher",
                lease_expires_at=now + timedelta(hours=1),
                fencing_token=7,
                executor_id="seraph-work-board:guardian-routine.v2",
                started_at=now,
                ended_at=now,
                outcome="verified",
                receipt_refs_json=json.dumps(
                    [{"workflow_run_id": parent_id, "status": "succeeded", "verified": True}]
                ),
            )
        )
        await db.flush()
        return {
            "task_id": task.task_id,
            "attempt_id": attempt_id,
            "parent_id": parent_id,
            "wrapper_id": str(occurrence.durable_job_id),
            "occurrence_id": occurrence.occurrence_id,
        }


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
@pytest.mark.parametrize("field", ["goal_revision", "version"])
async def test_procedure_schedule_rejects_coerced_action_identity_scalars_before_occurrence(async_db, field):
    await _seed_procedure_schedule(async_db, action_overrides={field: "1"})

    await execute_scheduled_job(JOB, scheduled_slot_utc=SLOT)

    async with async_db() as db:
        assert (await db.execute(select(GovernedScheduleOccurrence))).scalars().all() == []
        run = (await db.execute(select(ScheduledJobRun).where(ScheduledJobRun.scheduled_job_id == JOB))).scalar_one()
        assert run.outcome == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("routine_state", "paused"),
        ("routine_revision", 2),
        ("package_digest", "d" * 64),
    ],
)
async def test_procedure_schedule_rejects_current_routine_or_package_drift_before_occurrence(
    async_db,
    field: str,
    value: object,
):
    """A schedule never rebinds itself to a changed routine/package."""

    await _seed_procedure_schedule(async_db)
    async with async_db() as db:
        routine = await db.get(GuardianRoutine, "routine-0123456789abcdef0123456789abcdef")
        version = await db.get(GuardianRoutineVersion, "routine-version-1")
        assert routine is not None and version is not None
        if field == "routine_state":
            routine.state = str(value)
        elif field == "routine_revision":
            routine.revision = int(value)
        else:
            version.installed_package_digest = str(value)
        await db.commit()

    await execute_scheduled_job(JOB, scheduled_slot_utc=SLOT)

    async with async_db() as db:
        assert (await db.execute(select(GovernedScheduleOccurrence))).scalars().all() == []
        run = (await db.execute(select(ScheduledJobRun).where(ScheduledJobRun.scheduled_job_id == JOB))).scalar_one()
        assert run.outcome == "deferred"
        metadata = json.loads(run.metadata_json or "{}")
        assert metadata["recovery_action"] == "review_procedure"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", "1"),
        ("version", True),
        ("version", 1.0),
        ("version", 0),
        ("expected_goal_revision", "1"),
        ("expected_goal_revision", True),
        ("expected_goal_revision", 1.0),
        ("expected_goal_revision", 0),
    ],
)
async def test_procedure_schedule_rejects_noncanonical_payload_identity_scalars(
    async_db,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
):
    """The persisted payload binding never relies on JSON scalar coercion.

    The artifact, owner, Goal, binding, and schedule are real SQLite rows. We
    alter only the already-decoded payload at this exact comparison seam so
    the scheduler's defense-in-depth check is exercised even though the
    earlier typed-input validator also rejects malformed JSON scalars.
    """

    await _seed_procedure_schedule(async_db)
    original_decode = scheduled_jobs_module._decode_and_validate_payload

    def malformed_decode(artifact, payload, **kwargs):
        parsed = original_decode(artifact, payload, **kwargs)
        parsed[field] = value
        return parsed

    monkeypatch.setattr(scheduled_jobs_module, "_decode_and_validate_payload", malformed_decode)
    await execute_scheduled_job(JOB, scheduled_slot_utc=SLOT)

    async with async_db() as db:
        assert (await db.execute(select(GovernedScheduleOccurrence))).scalars().all() == []
        run = (await db.execute(select(ScheduledJobRun).where(ScheduledJobRun.scheduled_job_id == JOB))).scalar_one()
        assert run.outcome == "failed"


@pytest.mark.asyncio
async def test_unknown_post_publication_keeps_artifact_and_exact_slot_reconciles_task(async_db, monkeypatch):
    await _seed_procedure_schedule(async_db)
    original_create_task = WorkBoardRepository.create_task

    async def commit_then_lose_result(self, db, owner, request, **kwargs):
        mutation = await original_create_task(self, db, owner, request, **kwargs)
        # This is an actual SQLite commit of the task/artifact binding followed
        # by a lost writer result, matching the ambiguous publication boundary.
        await db.commit()
        raise RuntimeError("simulated post-publication commit result")

    monkeypatch.setattr(scheduled_jobs_module.WorkBoardRepository, "create_task", commit_then_lose_result)
    await execute_scheduled_job(JOB, scheduled_slot_utc=SLOT)

    async with async_db() as db:
        tasks = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.idempotency_scope == "guardian-routine-v2-schedule",
                )
            )
        ).scalars().all()
        occurrence = (await db.execute(select(GovernedScheduleOccurrence))).scalar_one()
        artifacts = (
            await db.execute(
                select(WorkBoardInputArtifact).where(
                    WorkBoardInputArtifact.capability_id == "guardian-routine.v2",
                )
            )
        ).scalars().all()
        assert len(tasks) == 1
        assert len(artifacts) == 2
        occurrence_metadata = json.loads(occurrence.metadata_json)
        assert occurrence.state == "unknown"
        assert occurrence.work_board_task_id is None
        assert occurrence_metadata["recovery_action"] == "reconcile_existing_occurrence"
        binding = await db.get(GovernedScheduleBinding, BINDING)
        assert binding is not None
        fresh = next(row for row in artifacts if row.artifact_id != binding.input_artifact_id)
        assert fresh.state == "bound"
        assert occurrence_metadata["input_artifact_id"] == fresh.artifact_id

    # The exact same slot adopts the committed task and does not prepare a
    # second artifact or task UUID after the unknown receipt.
    await execute_scheduled_job(JOB, scheduled_slot_utc=SLOT)
    async with async_db() as db:
        tasks = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.idempotency_scope == "guardian-routine-v2-schedule",
                )
            )
        ).scalars().all()
        artifacts = (
            await db.execute(
                select(WorkBoardInputArtifact).where(
                    WorkBoardInputArtifact.capability_id == "guardian-routine.v2",
                )
            )
        ).scalars().all()
        occurrence = (await db.execute(select(GovernedScheduleOccurrence))).scalar_one()
        assert len(tasks) == 1
        assert len(artifacts) == 2
        assert occurrence.state == "running"
        assert occurrence.work_board_task_id == tasks[0].task_id


@pytest.mark.asyncio
async def test_cancellation_after_publication_keeps_unknown_receipt_and_artifact(async_db, monkeypatch):
    await _seed_procedure_schedule(async_db)
    original_create_task = WorkBoardRepository.create_task

    async def commit_then_cancel(self, db, owner, request, **kwargs):
        await original_create_task(self, db, owner, request, **kwargs)
        await db.commit()
        raise asyncio.CancelledError()

    monkeypatch.setattr(scheduled_jobs_module.WorkBoardRepository, "create_task", commit_then_cancel)
    with pytest.raises(asyncio.CancelledError):
        await execute_scheduled_job(JOB, scheduled_slot_utc=SLOT)

    async with async_db() as db:
        occurrence = (await db.execute(select(GovernedScheduleOccurrence))).scalar_one()
        artifacts = (
            await db.execute(
                select(WorkBoardInputArtifact).where(
                    WorkBoardInputArtifact.capability_id == "guardian-routine.v2",
                )
            )
        ).scalars().all()
        binding = await db.get(GovernedScheduleBinding, BINDING)
        assert binding is not None
        fresh = next(row for row in artifacts if row.artifact_id != binding.input_artifact_id)
        assert occurrence.state == "unknown"
        assert json.loads(occurrence.metadata_json)["recovery_action"] == "reconcile_existing_occurrence"
        assert fresh.state == "bound"


@pytest.mark.asyncio
async def test_known_authority_failure_revokes_only_unpublished_artifact(async_db, monkeypatch):
    await _seed_procedure_schedule(async_db)
    original_create_task = WorkBoardRepository.create_task

    async def stale_authority(self, db, owner, request, **kwargs):
        await db.execute(
            update(GovernedScheduleBinding)
            .where(GovernedScheduleBinding.binding_id == BINDING)
            .values(binding_revision=2)
        )
        await db.commit()
        return await original_create_task(self, db, owner, request, **kwargs)

    monkeypatch.setattr(scheduled_jobs_module.WorkBoardRepository, "create_task", stale_authority)
    await execute_scheduled_job(JOB, scheduled_slot_utc=SLOT)

    async with async_db() as db:
        tasks = (await db.execute(select(WorkBoardTask))).scalars().all()
        occurrence = (await db.execute(select(GovernedScheduleOccurrence))).scalar_one()
        artifacts = (
            await db.execute(
                select(WorkBoardInputArtifact).where(
                    WorkBoardInputArtifact.capability_id == "guardian-routine.v2",
                )
            )
        ).scalars().all()
        run = (await db.execute(select(ScheduledJobRun).where(ScheduledJobRun.scheduled_job_id == JOB))).scalar_one()
        assert tasks == []
        assert occurrence.state == "blocked"
        assert json.loads(occurrence.metadata_json)["recovery_action"] == "retry_after_prerequisite"
        binding = await db.get(GovernedScheduleBinding, BINDING)
        assert binding is not None
        fresh = next(row for row in artifacts if row.artifact_id != binding.input_artifact_id)
        assert fresh.state == "revoked"
        assert run.outcome == "blocked"


@pytest.mark.asyncio
async def test_concurrent_bound_artifact_blocks_cleanup_and_quarantines_occurrence(async_db, monkeypatch):
    await _seed_procedure_schedule(async_db)
    original_create_task = WorkBoardRepository.create_task

    async def concurrent_bound_then_stale(self, db, owner, request, **kwargs):
        async with scheduled_jobs_module.get_session() as competitor_db:
            competitor = request.model_copy(
                update={
                    "idempotency_scope": "concurrent-guard",
                    "idempotency_key": "concurrent-guard-key",
                    "title": "Concurrent bound task",
                }
            )
            await original_create_task(WorkBoardRepository(), competitor_db, owner, competitor)
        raise scheduled_jobs_module._ProcedureScheduleAuthorityFailure()

    monkeypatch.setattr(scheduled_jobs_module.WorkBoardRepository, "create_task", concurrent_bound_then_stale)
    await execute_scheduled_job(JOB, scheduled_slot_utc=SLOT)

    async with async_db() as db:
        tasks = (await db.execute(select(WorkBoardTask))).scalars().all()
        occurrence = (await db.execute(select(GovernedScheduleOccurrence))).scalar_one()
        artifacts = (
            await db.execute(
                select(WorkBoardInputArtifact).where(
                    WorkBoardInputArtifact.capability_id == "guardian-routine.v2",
                )
            )
        ).scalars().all()
        assert len(tasks) == 1
        assert occurrence.state == "unknown"
        binding = await db.get(GovernedScheduleBinding, BINDING)
        assert binding is not None
        fresh = next(row for row in artifacts if row.artifact_id != binding.input_artifact_id)
        assert fresh.state == "bound"
        assert json.loads(occurrence.metadata_json)["recovery_action"] == "reconcile_existing_occurrence"


@pytest.mark.asyncio
async def test_procedure_schedule_settles_from_native_parent_not_scheduler_wrapper(async_db):
    refs = await _seed_terminal_native_parent(async_db)

    async with async_db() as db:
        occurrence = await db.get(GovernedScheduleOccurrence, refs["occurrence_id"])
        task = (
            await db.execute(
                select(WorkBoardTask).where(WorkBoardTask.task_id == refs["task_id"])
            )
        ).scalar_one_or_none()
        binding = await db.get(GovernedScheduleBinding, BINDING)
        assert occurrence is not None and task is not None and binding is not None
        assert await _procedure_terminal_state(db, occurrence, task) == ("succeeded", None, None)

        # The next slot exercises the real terminal transition. The row keeps
        # the scheduler wrapper identity while proof resolution follows the
        # exact terminal attempt's native workflow root.
        _next, replay = await reserve_occurrence(db, binding, slot_utc=SLOT + timedelta(hours=1))
        assert replay is False
        await db.refresh(occurrence)
        assert occurrence.state == "succeeded"
        assert occurrence.durable_job_id == refs["wrapper_id"]
        assert occurrence.durable_job_id != refs["parent_id"]


@pytest.mark.asyncio
async def test_procedure_schedule_rejects_forged_native_attempt_authority(async_db):
    refs = await _seed_terminal_native_parent(
        async_db,
        authority_overrides={"board_attempt_id": "forged-attempt"},
    )

    async with async_db() as db:
        occurrence = await db.get(GovernedScheduleOccurrence, refs["occurrence_id"])
        task = (
            await db.execute(
                select(WorkBoardTask).where(WorkBoardTask.task_id == refs["task_id"])
            )
        ).scalar_one_or_none()
        assert occurrence is not None and task is not None
        assert await _procedure_terminal_state(db, occurrence, task) == (
            "blocked",
            "procedure_parent_authority_mismatch",
            "reconcile_admission_binding",
        )


@pytest.mark.asyncio
async def test_procedure_schedule_rejects_preclaim_parent_task_revision(async_db):
    # The producer records the post-claim running revision (claim revision +
    # one).  Accepting the pre-claim value would let stale authority look
    # current at terminal settlement.
    refs = await _seed_terminal_native_parent(
        async_db,
        authority_overrides={"board_task_revision": 1},
    )

    async with async_db() as db:
        occurrence = await db.get(GovernedScheduleOccurrence, refs["occurrence_id"])
        task = (
            await db.execute(
                select(WorkBoardTask).where(WorkBoardTask.task_id == refs["task_id"])
            )
        ).scalar_one_or_none()
        assert occurrence is not None and task is not None
        assert await _procedure_terminal_state(db, occurrence, task) == (
            "blocked",
            "procedure_parent_authority_mismatch",
            "reconcile_admission_binding",
        )


@pytest.mark.asyncio
async def test_procedure_schedule_requires_native_verified_readback(async_db):
    refs = await _seed_terminal_native_parent(async_db, effect_mode="missing")

    async with async_db() as db:
        occurrence = await db.get(GovernedScheduleOccurrence, refs["occurrence_id"])
        task = (
            await db.execute(
                select(WorkBoardTask).where(WorkBoardTask.task_id == refs["task_id"])
            )
        ).scalar_one_or_none()
        assert occurrence is not None and task is not None
        assert await _procedure_terminal_state(db, occurrence, task) == (
            "blocked",
            "procedure_verified_readback_missing",
            "reconcile_admission_binding",
        )


@pytest.mark.asyncio
async def test_procedure_schedule_quarantines_unknown_native_effect(async_db):
    refs = await _seed_terminal_native_parent(async_db, effect_mode="unknown")

    async with async_db() as db:
        occurrence = await db.get(GovernedScheduleOccurrence, refs["occurrence_id"])
        task = (
            await db.execute(
                select(WorkBoardTask).where(WorkBoardTask.task_id == refs["task_id"])
            )
        ).scalar_one_or_none()
        assert occurrence is not None and task is not None
        # ``None`` lets the normal governed occurrence path quarantine the
        # unresolved native effect instead of settling it as blocked/success.
        assert await _procedure_terminal_state(db, occurrence, task) is None


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
