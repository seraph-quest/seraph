"""Focused M2 dispatcher identity and recovery contracts."""

from types import SimpleNamespace
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel

from src.db.models import Goal, WorkBoardAttempt, WorkBoardStatus, WorkBoardTask
from src.work_board.contracts import WorkBoardOwner
from src.work_board.dispatcher import (
    BoardDispatchClaim,
    WorkBoardDispatcher,
    _stable_reason_code,
)
from src.work_board.repository import BoardMutation, BoardError, WorkBoardRepository


OWNER = WorkBoardOwner(principal_id="operator:dispatcher", session_id="dispatcher-session")


def _task(capability_id: str, *, owner: str = "operator:one"):
    return SimpleNamespace(
        task_id="task-dispatch",
        owner_principal_id=owner,
        owner_session_id="session-one",
        goal_id="goal-one",
        goal_revision=3,
        capability_id=capability_id,
    )


def _attempt():
    return SimpleNamespace(attempt_id="4e6f6d65-2d61-4d32-a9f4-0b4e2b8e6d70")


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


def _dispatch_task(task_id: str, *, status: WorkBoardStatus, priority: int, sequence: int):
    return SimpleNamespace(
        task_id=task_id,
        task_revision=1,
        status=status,
        priority=priority,
        creation_sequence=sequence,
        scheduled_at=None,
        executor_id=f"executor-{task_id}",
        attempt_id=None,
    )


def test_direct_adapters_use_the_reviewed_root_and_binding_identities():
    dispatcher = WorkBoardDispatcher()
    attempt = _attempt()

    source_root, source_owner, source_kind, source_service, source_key = dispatcher._direct_job_identity(
        _task("guardian.research-watch.v1"),
        attempt,
        {"watch_id": "watch-1"},
    )
    assert source_root == "source-watch:watch-1:4e6f6d652d614d32a9f40b4e2b8e6d70"
    assert source_owner == "service:guardian-source-watch"
    assert source_kind == "guardian_source_watch"
    assert source_service == "guardian-source-watch"
    assert source_key == "task-dispatch:4e6f6d65-2d61-4d32-a9f4-0b4e2b8e6d70"

    repo_root, repo_owner, repo_kind, repo_service, _ = dispatcher._direct_job_identity(
        _task("engineering.repo-change.v1"),
        attempt,
        {},
    )
    assert repo_root.startswith("repo-change-")
    assert repo_owner == "operator:one"
    assert repo_kind == "engineering.repo-change.v1"
    assert repo_service is None

    github_root, github_owner, github_kind, github_service, _ = dispatcher._direct_job_identity(
        _task("work.github-followthrough.v1"),
        attempt,
        {},
    )
    assert github_root.startswith("ghfollow_")
    assert github_owner == "operator:one"
    assert github_kind == "github_followthrough_v1"
    assert github_service is None

    routine_root, routine_owner, routine_kind, routine_service, _ = dispatcher._direct_job_identity(
        _task("guardian-routine.v1"),
        attempt,
        {"routine_id": "routine-1"},
    )
    assert routine_root.startswith("routine-invocation:routine-1:")
    assert routine_owner == "operator:one"
    assert routine_kind == "routine_invocation"
    assert routine_service is None


@pytest.mark.asyncio
async def test_attempt_run_link_persisted_before_adapter_execution(monkeypatch):
    """A direct adapter cannot enter its effect phase before board linking."""

    task = SimpleNamespace(
        task_id="task-admission-order",
        owner_principal_id="operator:one",
        owner_session_id="session-one",
        goal_id="goal-one",
        goal_revision=3,
        capability_id="guardian.research-watch.v1",
        task_revision=8,
        requires_review=False,
    )
    attempt = SimpleNamespace(
        attempt_id="4e6f6d65-2d61-4d32-a9f4-0b4e2b8e6d70",
        fencing_token=4,
        lease_owner="service:work-board",
    )
    job_id, *_ = WorkBoardDispatcher._direct_job_identity(
        task,
        attempt,
        {"watch_id": "watch-1", "expected_plan_revision": 1},
    )
    linked = False
    phases: list[tuple[bool, bool]] = []

    class Jobs:
        async def get_job(self, requested_job_id):
            assert requested_job_id == job_id
            return {
                "job_id": job_id,
                "run_identity": job_id,
                "status": "running",
                "owner_principal_id": "service:guardian-source-watch",
                "owner_kind": "guardian_source_watch",
                "service_id": "guardian-source-watch",
                "operator_session_id": task.owner_session_id,
                "session_id": task.owner_session_id,
                "goal_id": task.goal_id,
                "goal_revision": task.goal_revision,
                "capability_id": task.capability_id,
                "capability_version": "1",
                "input_digest": WorkBoardDispatcher._direct_input_digest(
                    task, attempt, {"watch_id": "watch-1", "expected_plan_revision": 1}
                ),
                "run_fingerprint": WorkBoardDispatcher._direct_input_digest(
                    task, attempt, {"watch_id": "watch-1", "expected_plan_revision": 1}
                ),
                "idempotency_scope": "work-board-attempt",
                "idempotency_key": f"{task.task_id}:{attempt.attempt_id}",
            }

    class Repository:
        async def link_attempt_workflow_run(self, _db, *_args, **kwargs):
            nonlocal linked
            linked = True
            assert kwargs["workflow_run_id"] == job_id
            return SimpleNamespace(task=SimpleNamespace(task_revision=9))

    dispatcher = WorkBoardDispatcher(
        repository=Repository(),
        jobs=Jobs(),
        session_provider=lambda: _Session(),
    )

    async def fake_run_watch(
        _service,
        _watch_id,
        *,
        occurrence_id,
        expected_plan_revision,
        expected_owner_session_id,
        work_board_task_id,
        work_board_attempt_id,
        admit_only,
    ):
        assert occurrence_id == "4e6f6d652d614d32a9f40b4e2b8e6d70"
        assert expected_plan_revision == 1
        assert expected_owner_session_id == task.owner_session_id
        assert work_board_task_id == task.task_id
        assert work_board_attempt_id == attempt.attempt_id
        phases.append((admit_only, linked))
        return {"job_id": job_id, "status": "running", "admission_only": admit_only}

    async def fake_project(*_args, **_kwargs):
        return None

    monkeypatch.setattr("src.guardian.source_watch.SourceWatchService.run_watch", fake_run_watch)
    dispatcher._project = fake_project
    dispatcher._direct_verified = lambda *_args, **_kwargs: False

    claim = BoardDispatchClaim(task, attempt, SimpleNamespace(event_id=1))
    result = await dispatcher._admit_execute_direct(
        claim,
        {"watch_id": "watch-1", "expected_plan_revision": 1},
        runtime_seconds=300,
    )
    assert phases == [(True, False), (False, True)]
    assert result == {"admitted": True, "completed": False, "blocked": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("child_status", "expected_proven"),
    [("cancelled", True), ("running", False)],
)
async def test_routine_cleanup_proof_requires_terminal_tree_readback(
    monkeypatch,
    child_status: str,
    expected_proven: bool,
):
    task = SimpleNamespace(
        task_id="task-routine-cancel",
        owner_principal_id="operator:one",
        owner_session_id="session-one",
        capability_id="guardian-routine.v1",
    )
    attempt = SimpleNamespace(
        attempt_id="attempt-routine-cancel",
        workflow_run_id="routine-invocation:routine-1:attempt-routine-cancel",
    )

    class RoutineService:
        async def cancel_invocation_job_tree(self, *_args, **_kwargs):
            return [
                {
                    "job_id": attempt.workflow_run_id,
                    "run_identity": attempt.workflow_run_id,
                    "status": "cancelled",
                    "effects": [],
                },
                {
                    "job_id": "routine-child:1",
                    "run_identity": "routine-child:1",
                    "status": child_status,
                    "effects": [],
                },
            ]

    monkeypatch.setattr("src.workflows.routines.routine_service", RoutineService())
    dispatcher = WorkBoardDispatcher()
    receipts, proven = await dispatcher._cleanup_adapter(
        task,
        attempt,
        {"routine_id": "routine-1"},
        {"status": "running"},
        reason="operator_cancelled",
    )

    assert proven is expected_proven
    assert [receipt["job_id"] for receipt in receipts] == [attempt.workflow_run_id, "routine-child:1"]


@pytest.mark.asyncio
async def test_repo_change_cleanup_uses_authenticated_shared_route_helper(monkeypatch):
    task = SimpleNamespace(
        task_id="task-repo-cancel",
        owner_principal_id="operator:one",
        owner_session_id="session-one",
        capability_id="engineering.repo-change.v1",
    )
    attempt = SimpleNamespace(
        attempt_id="attempt-repo-cancel",
        workflow_run_id="repo-change-operator-one-task-repo-cancel-attempt-repo-cancel",
    )
    calls: list[tuple[str, Any, str]] = []
    operator = object()

    async def authenticate(session_id, *, owner_principal_id):
        assert session_id == task.owner_session_id
        assert owner_principal_id == task.owner_principal_id
        return operator

    async def cancel(job_id, *, operator, reason):
        calls.append((job_id, operator, reason))
        return {"status": "cancelled", "job": {"job_id": job_id, "status": "cancelled"}}

    monkeypatch.setattr("src.api.workflows.authenticate_repo_change_operator", authenticate)
    monkeypatch.setattr("src.api.workflows.cancel_repo_change_for_authenticated_operator", cancel)
    dispatcher = WorkBoardDispatcher()
    receipts, proven = await dispatcher._cleanup_adapter(
        task,
        attempt,
        {},
        {"status": "running"},
        reason="operator_cancelled",
    )

    assert proven is True
    assert calls == [(attempt.workflow_run_id, operator, "operator_cancelled")]
    assert receipts == [
        {
            "job_id": attempt.workflow_run_id,
            "status": "cancelled",
            "reason_code": "operator_cancelled",
        }
    ]


@pytest.mark.asyncio
async def test_repeated_cancel_returns_persisted_intent_without_projection_churn(monkeypatch):
    task = SimpleNamespace(
        task_id="task-cancel-replay",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        goal_id="goal-one",
        goal_revision=1,
        capability_id="guardian.research-watch.v1",
        task_revision=9,
        status=WorkBoardStatus.running,
    )
    attempt = SimpleNamespace(
        task_id=task.task_id,
        attempt_id="attempt-cancel-replay",
        workflow_run_id="source-watch:watch-1:attempt-cancel-replay",
        fencing_token=7,
        lease_owner="service:work-board",
        ended_at=None,
    )
    event = SimpleNamespace(kind="attempt.cancel_requested", event_id=42)
    detail = {
        "task": task,
        "attempts": [attempt],
        "events": [event],
    }
    cleanup_called = False

    class Jobs:
        async def get_job(self, _job_id):
            return {"job_id": attempt.workflow_run_id, "status": "running", "lease": {}}

    class Repository:
        async def get_detail(self, _db, _owner, _task_id):
            return detail

        async def validate_attempt_binding(self, *_args, **_kwargs):
            return None

        async def request_cancel(self, *_args, **_kwargs):
            return BoardMutation(task=task, event=event, idempotent_replay=True)

    dispatcher = WorkBoardDispatcher(
        repository=Repository(),
        jobs=Jobs(),
        session_provider=lambda: _Session(),
    )
    async def _runtime(_task):
        return 300

    dispatcher._effective_runtime = _runtime
    monkeypatch.setattr("src.work_board.dispatcher._parse_typed_input", lambda _task: {"watch_id": "watch-1", "expected_plan_revision": 1})
    dispatcher._lookup_linked_binding = lambda *_args, **_kwargs: _async_value(attempt.workflow_run_id)
    dispatcher._expected_identity_for_task = lambda *_args, **_kwargs: {}

    async def fail_cleanup(*_args, **_kwargs):
        nonlocal cleanup_called
        cleanup_called = True
        raise AssertionError("replayed cancellation must not run cleanup")

    dispatcher._cleanup_adapter = fail_cleanup
    projection = await dispatcher.cancel_task(
        OWNER,
        task.task_id,
        expected_revision=task.task_revision,
    )

    assert cleanup_called is False
    assert projection.task.task_revision == 9
    assert projection.event.event_id == 42
    assert projection.attempt.attempt_id == attempt.attempt_id


async def _async_value(value):
    return value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("unknown external effect at /private/source", "unknown_effect"),
        ("provider rate limit", "capability"),
        ("approval expired", "needs_input"),
        ("worker timed out", "transient"),
        ("verified_readback_missing", "verified_readback_missing"),
    ],
)
def test_adapter_reasons_are_closed_stable_codes(value: str, expected: str):
    assert _stable_reason_code(value) == expected
    assert "/" not in _stable_reason_code(value)


@pytest.mark.asyncio
async def test_linked_recovery_requires_exact_binding_before_projection():
    task = _task("guardian.research-watch.v1")
    task.status = WorkBoardStatus.running
    task.task_revision = 4
    task.executor_id = "executor-one"
    task.typed_input_ref = "workspace-json:inputs/source.json"
    task.typed_input_digest = "0" * 64
    task.requires_review = False
    attempt = _attempt()
    attempt.workflow_run_id = "source-watch:watch-1:4e6f6d652d614d32a9f40b4e2b8e6d70"
    attempt.ended_at = None
    attempt.lease_owner = "service:work-board"
    attempt.fencing_token = 2

    class Jobs:
        async def get_job(self, _job_id):
            return {
                "job_id": attempt.workflow_run_id,
                "run_identity": attempt.workflow_run_id,
                "status": "running",
                "owner_principal_id": "service:guardian-source-watch",
                "owner_kind": "guardian_source_watch",
                "service_id": "guardian-source-watch",
                "operator_session_id": task.owner_session_id,
                "session_id": task.owner_session_id,
                "goal_id": task.goal_id,
                "goal_revision": task.goal_revision,
                "capability_id": task.capability_id,
                "capability_version": "1",
                "input_digest": "1" * 64,
                "authority_digest": "2" * 64,
                "run_fingerprint": "3" * 64,
                "idempotency_scope": "work-board-attempt",
                "idempotency_key": f"{task.task_id}:{attempt.attempt_id}",
            }

        async def get_by_idempotency_binding(self, **_kwargs):
            return None

    class Repository:
        async def list_linked_active_attempts(self, _db, *, limit):
            return [(task, attempt)]

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    dispatcher = WorkBoardDispatcher(repository=Repository(), jobs=Jobs(), session_provider=lambda: Session())
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "src.work_board.dispatcher._parse_typed_input",
        lambda _task: {"watch_id": "watch-1", "expected_plan_revision": 1},
    )
    blocked: list[str] = []

    async def project(*_args, **kwargs):
        blocked.append(kwargs["block_reason"])
        return None

    dispatcher._project = project
    try:
        recovered = await dispatcher.reconcile_linked_attempts()
    finally:
        monkeypatch.undo()
    assert recovered == [attempt.workflow_run_id]
    assert blocked == ["reconcile_admission_binding"]


@pytest.mark.asyncio
async def test_dispatch_pass_admits_at_most_two_in_priority_fifo_order():
    first = _dispatch_task("high-old", status=WorkBoardStatus.ready, priority=90, sequence=1)
    second = _dispatch_task("high-new", status=WorkBoardStatus.ready, priority=90, sequence=2)
    third = _dispatch_task("lower", status=WorkBoardStatus.ready, priority=40, sequence=3)
    candidates = [first, second, third]
    admitted: list[str] = []

    class Repository:
        async def list_dispatch_candidates(self, _db, **_kwargs):
            return candidates

        async def claim_ready_task(self, _db, task_id, **_kwargs):
            task = next(item for item in candidates if item.task_id == task_id)
            if task_id in admitted:
                return None
            attempt = SimpleNamespace(
                attempt_id=f"attempt-{task_id}",
                task_id=task_id,
                fencing_token=1,
                lease_owner="service:work-board",
            )
            return BoardDispatchClaim(task, attempt, SimpleNamespace(event_id=1))

    dispatcher = WorkBoardDispatcher(repository=Repository(), session_provider=lambda: _Session())
    dispatcher.reconcile_pending_attempts = _empty_reconcile
    dispatcher.reconcile_linked_attempts = _empty_reconcile
    dispatcher._readiness = lambda _task: _ready()
    dispatcher._effective_runtime = lambda _task: _runtime()

    async def execute(claim):
        admitted.append(claim.task.task_id)
        return {"admitted": True, "completed": False, "blocked": False}

    dispatcher._admit_execute_project = execute
    receipt = await dispatcher.run_pass()

    assert admitted == ["high-old", "high-new"]
    assert receipt["admitted"] == 2
    assert receipt["considered"] == 3


async def _empty_reconcile(*_args, **_kwargs):
    return []


async def _ready(*_args, **_kwargs):
    return None, None


async def _runtime(*_args, **_kwargs):
    return 300


@pytest.mark.asyncio
async def test_two_racing_dispatch_passes_can_create_only_one_claim():
    task = _dispatch_task("racing", status=WorkBoardStatus.ready, priority=50, sequence=1)
    lock = asyncio.Lock()
    claim_count = 0

    class Repository:
        async def list_dispatch_candidates(self, _db, **_kwargs):
            return [task]

        async def claim_ready_task(self, _db, task_id, **_kwargs):
            nonlocal claim_count
            async with lock:
                if claim_count:
                    return None
                claim_count += 1
                return BoardDispatchClaim(
                    task,
                    SimpleNamespace(
                        attempt_id="attempt-racing",
                        task_id=task_id,
                        fencing_token=1,
                        lease_owner="service:work-board",
                    ),
                    SimpleNamespace(event_id=1),
                )

    def make_dispatcher():
        dispatcher = WorkBoardDispatcher(repository=Repository(), session_provider=lambda: _Session())
        dispatcher.reconcile_pending_attempts = _empty_reconcile
        dispatcher.reconcile_linked_attempts = _empty_reconcile
        dispatcher._readiness = lambda _task: _ready()
        dispatcher._effective_runtime = lambda _task: _runtime()
        dispatcher._admit_execute_project = lambda _claim: _admitted()
        return dispatcher

    results = await asyncio.gather(make_dispatcher().run_pass(), make_dispatcher().run_pass())
    assert claim_count == 1
    assert sum(item["claimed"] for item in results) == 1


async def _admitted(*_args, **_kwargs):
    return {"admitted": True, "completed": False, "blocked": False}


@pytest.mark.asyncio
async def test_claim_enforces_global_and_per_executor_limits(tmp_path):
    repository = WorkBoardRepository()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'dispatcher-limits.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        db.add(
            Goal(
                id="goal-dispatch-limits",
                title="Dispatcher limit goal",
                status="active",
                revision=1,
                owner_principal_id=OWNER.principal_id,
                owner_session_id=OWNER.session_id,
            )
        )
        db.add_all(
            [
                WorkBoardTask(
                    task_id="running-a",
                    owner_principal_id=OWNER.principal_id,
                    owner_session_id=OWNER.session_id,
                    goal_id="goal-dispatch-limits",
                    goal_revision=1,
                    title="A",
                    idempotency_key="running-a",
                    status=WorkBoardStatus.running,
                    executor_id="executor-a",
                ),
                WorkBoardTask(
                    task_id="running-b",
                    owner_principal_id=OWNER.principal_id,
                    owner_session_id=OWNER.session_id,
                    goal_id="goal-dispatch-limits",
                    goal_revision=1,
                    title="B",
                    idempotency_key="running-b",
                    status=WorkBoardStatus.running,
                    executor_id="executor-b",
                ),
                WorkBoardTask(
                    task_id="ready-c",
                    owner_principal_id=OWNER.principal_id,
                    owner_session_id=OWNER.session_id,
                    goal_id="goal-dispatch-limits",
                    goal_revision=1,
                    title="C",
                    idempotency_key="ready-c",
                    status=WorkBoardStatus.ready,
                    executor_id="executor-c",
                ),
            ]
        )
        await db.flush()
        db.add_all(
            [
                WorkBoardAttempt(task_id="running-a", executor_id="executor-a", lease_owner="worker-a", fencing_token=1),
                WorkBoardAttempt(task_id="running-b", executor_id="executor-b", lease_owner="worker-b", fencing_token=1),
            ]
        )
        await db.commit()
        assert await repository.claim_ready_task(
            db,
            "ready-c",
            expected_revision=1,
            lease_owner="service:work-board",
        ) is None
        await db.commit()

        running_b = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == "running-b"))
        ).scalar_one()
        running_b.status = WorkBoardStatus.done
        running_attempt = (
            await db.execute(
                __import__("sqlalchemy").select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == "running-b")
            )
        ).scalar_one()
        running_attempt.ended_at = datetime.now(timezone.utc)
        ready = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == "ready-c"))
        ).scalar_one()
        ready.executor_id = "executor-a"
        await db.flush()
        await db.commit()
        assert await repository.claim_ready_task(
            db,
            "ready-c",
            expected_revision=1,
            lease_owner="service:work-board",
        ) is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_stale_board_fence_cannot_heartbeat_or_project(async_db):
    repository = WorkBoardRepository()
    async with async_db() as db:
        db.add(
            Goal(
                id="goal-dispatch-fence",
                title="Dispatcher fence goal",
                status="active",
                revision=1,
                owner_principal_id=OWNER.principal_id,
                owner_session_id=OWNER.session_id,
            )
        )
        task = WorkBoardTask(
            task_id="ready-fence",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            goal_id="goal-dispatch-fence",
            goal_revision=1,
            title="Fence",
            idempotency_key="ready-fence",
            status=WorkBoardStatus.ready,
            executor_id="executor-fence",
        )
        db.add(task)
        await db.flush()
        claim = await repository.claim_ready_task(
            db,
            task.task_id,
            expected_revision=1,
            lease_owner="worker-fence",
        )
        with pytest.raises(BoardError, match="stale"):
            await repository.heartbeat_attempt(
                db,
                task.task_id,
                claim.attempt.attempt_id,
                expected_revision=claim.task.task_revision,
                board_fence=claim.attempt.fencing_token + 1,
                lease_owner="worker-fence",
            )
