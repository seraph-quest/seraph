"""Focused M2 dispatcher identity and recovery contracts."""

from types import SimpleNamespace
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel

from config.settings import settings
from src.db.models import (
    Goal,
    Secret,
    Session,
    WorkBoardAttempt,
    WorkBoardEvent,
    WorkBoardLink,
    WorkBoardProposal,
    WorkBoardReviewIntent,
    WorkBoardStatus,
    WorkBoardTask,
    WorkflowRunState,
)
from src.work_board.contracts import WorkBoardOwner
from src.goals.contracts import CriterionVerifierKind, GoalSuccessCriterion
from src.work_board.dispatcher import (
    BoardDispatchClaim,
    GOAL_SNAPSHOT_CAPABILITY,
    WorkBoardDispatcher,
    _preflight_recovery_action,
    _stable_reason_code,
    registered_executor_id,
)
from src.work_board.repository import BoardMutation, BoardError, WorkBoardRepository
from src.work_board import review as review_service
from src.workflows.job_runtime import DurableJobError, DurableJobRepository


OWNER = WorkBoardOwner(principal_id="operator:dispatcher", session_id="dispatcher-session")


@pytest.mark.parametrize(
    "reason_code",
    [
        "goal_snapshot_criterion_missing",
        "goal_snapshot_verifier_missing",
        "goal_snapshot_evidence_missing",
    ],
)
def test_goal_snapshot_readiness_failure_names_goal_verification_recovery(reason_code: str):
    assert _preflight_recovery_action(reason_code) == "configure_goal_success_criterion"


def test_other_readiness_failure_keeps_generic_prerequisite_recovery():
    assert _preflight_recovery_action("github_credential_missing") == "restore_prerequisite"


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

    async def execute(self, _statement):
        return _EmptyResult()


class _EmptyResult:
    def scalars(self):
        return self

    def all(self):
        return []


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
async def test_direct_adapter_admission_order_unit(monkeypatch):
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
        executor_id="executor.one",
        priority=50,
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
                "owner": {
                    "principal_id": "service:guardian-source-watch",
                    "kind": "service",
                    "service_id": "guardian-source-watch",
                },
                "job_kind": "guardian_source_watch",
                "operator_session_id": task.owner_session_id,
                "session_id": task.owner_session_id,
                "goal_id": task.goal_id,
                "goal_revision": task.goal_revision,
                "capability_version": "1",
                "declared_authority": {"capability_id": task.capability_id},
                "input_digest": WorkBoardDispatcher._direct_input_digest(
                    task, attempt, {"watch_id": "watch-1", "expected_plan_revision": 1}
                ),
                "authority_digest": "a" * 64,
                "run_fingerprint": "b" * 64,
                "idempotency": {
                    "scope": "work-board-attempt",
                    "key": f"{task.task_id}:{attempt.attempt_id}",
                    "binding": "binding-source-watch",
                },
            }

        async def get_by_idempotency_binding(self, **_kwargs):
            return await self.get_job(job_id)

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

    claim = BoardDispatchClaim(task, attempt, SimpleNamespace(event_id=1))
    result = await dispatcher._admit_execute_direct(
        claim,
        {"watch_id": "watch-1", "expected_plan_revision": 1},
        runtime_seconds=300,
    )
    assert phases == [(True, False), (False, True)]
    assert result == {"admitted": True, "completed": False, "blocked": True}


def test_direct_success_requires_typed_independent_readback():
    """Generic adapter verification cannot promote a direct board task."""

    job_id = "source-watch:watch-1:attempt-1"
    result = {
        "status": "succeeded",
        "verified": True,
        "content_sha256": "a" * 64,
    }
    projection = {
        "run_identity": job_id,
        "root_run_identity": job_id,
        "status": "succeeded",
        "effects": [
            {
                "effect_type": "source_watch_readback",
                "receipt_kind": "readback",
                "status": "succeeded",
                "workflow_run_id": job_id,
                "content_sha256": "b" * 64,
                "readback_id": "source-readback:watch-1",
                "verified_at": "2026-09-25T12:00:00+00:00",
            }
        ],
    }

    proof = WorkBoardDispatcher._direct_readback(result, projection, job_id)
    assert proof is not None
    assert proof["content_sha256"] == "b" * 64
    assert proof["readback_id"] == "source-readback:watch-1"
    assert proof["verified_at"] == "2026-09-25T12:00:00+00:00"

    generic_only = {
        **projection,
        "effects": [
            {
                "effect_type": "source_watch_summary",
                "receipt_kind": "effect",
                "status": "succeeded",
                "content_sha256": "b" * 64,
            }
        ],
    }
    assert WorkBoardDispatcher._direct_readback(result, generic_only, job_id) is None
    assert WorkBoardDispatcher._direct_verified(result, generic_only, job_id) is False


@pytest.mark.asyncio
async def test_direct_dispatcher_projection_preserves_dynamic_review(async_db, monkeypatch):
    """The direct adapter path preserves a review requested after linking."""

    task_id = "direct-dynamic-review"
    attempt_id = "direct-dynamic-review-attempt"
    inputs = {"watch_id": "watch-dynamic-review", "expected_plan_revision": 1}
    dispatcher = WorkBoardDispatcher()
    task = WorkBoardTask(
        task_id=task_id,
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        origin_session_id=OWNER.session_id,
        goal_id="goal-direct-dynamic-review",
        goal_revision=1,
        title="Direct adapter review race",
        idempotency_key=f"{task_id}-key",
        capability_id="guardian.research-watch.v1",
        status=WorkBoardStatus.running,
        task_revision=1,
        requires_review=False,
        executor_id=dispatcher.runner_id,
        priority=50,
    )
    attempt = WorkBoardAttempt(
        attempt_id=attempt_id,
        task_id=task_id,
        task_revision_at_claim=1,
        lease_owner=dispatcher.runner_id,
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        heartbeat_at=datetime.now(timezone.utc),
        fencing_token=14,
        executor_id=dispatcher.runner_id,
        started_at=datetime.now(timezone.utc),
    )
    expected = dispatcher._direct_expected_identity(task, attempt, inputs)
    job_id = expected["job_id"]
    projection = {
        "job_id": job_id,
        "run_identity": job_id,
        "root_run_identity": job_id,
        "owner": {
            "principal_id": expected["owner_principal_id"],
            "kind": expected["owner_kind"],
            "service_id": expected["service_id"],
        },
        "job_kind": expected["job_kind"],
        "capability_version": expected["capability_version"],
        "session_id": expected["session_id"],
        "operator_session_id": expected["operator_session_id"],
        "goal_id": expected["goal_id"],
        "goal_revision": expected["goal_revision"],
        "idempotency": {
            "scope": expected["idempotency_scope"],
            "key": expected["idempotency_key"],
        },
        "declared_authority": {"capability_id": task.capability_id},
        "input_digest": expected["input_digest"],
        "authority_digest": expected["authority_digest"],
        "run_fingerprint": expected["run_fingerprint"],
        "status": "accepted",
        "effects": [],
    }

    class Jobs:
        async def get_job(self, requested_job_id):
            assert requested_job_id == job_id
            return dict(projection)

        async def get_by_idempotency_binding(self, **_kwargs):
            return dict(projection)

    class Repository:
        def __init__(self):
            self.real = WorkBoardRepository()

        async def link_attempt_workflow_run(self, db, *args, **kwargs):
            linked = await self.real.link_attempt_workflow_run(db, *args, **kwargs)
            # Close the link transaction before a separate worker session
            # records review.  The returned dispatcher snapshot remains
            # requires_review=False, reproducing the race under test.
            await db.commit()
            async with async_db() as review_db:
                requested = await review_service.request_review(
                    review_db,
                    OWNER,
                    task_id,
                    expected_revision=linked.task.task_revision,
                    attempt_id=attempt_id,
                    evidence_refs=[],
                )
                assert requested.task.requires_review is True
            return linked

        async def project_attempt(self, *args, **kwargs):
            return await self.real.project_attempt(*args, **kwargs)

    jobs = Jobs()
    dispatcher = WorkBoardDispatcher(
        repository=Repository(),
        jobs=jobs,
        session_provider=async_db,
    )

    async def execute_direct(_task, _attempt, _inputs, *, runtime_seconds, admission_only=False):
        assert runtime_seconds == 300
        if admission_only:
            return {"job_id": job_id, "admission_only": True}
        projection.update(
            {
                "status": "succeeded",
                "effects": [
                    {
                        "receipt_kind": "readback",
                        "status": "succeeded",
                        "verified": True,
                        "workflow_run_id": job_id,
                        "content_sha256": "c" * 64,
                        "readback_id": "direct-dynamic-readback",
                        "verified_at": "2026-09-25T00:00:00+00:00",
                    }
                ],
            }
        )
        return {"job_id": job_id, "status": "succeeded", "verified": True}

    monkeypatch.setattr(dispatcher, "_execute_direct_adapter", execute_direct)
    async with async_db() as db:
        db.add(
            Goal(
                id=task.goal_id,
                title="Direct dynamic review goal",
                owner_principal_id=OWNER.principal_id,
                owner_session_id=OWNER.session_id,
                revision=1,
                status="active",
            )
        )
        db.add(task)
        await db.flush()
        db.add(attempt)
        await db.commit()

    result = await dispatcher._admit_execute_direct(
        BoardDispatchClaim(task, attempt, SimpleNamespace(event_id=1)),
        inputs,
        runtime_seconds=300,
    )
    assert result["completed"] is True, result
    async with async_db() as db:
        projected = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
        ).scalar_one()
        intent = (
            await db.execute(
                select(WorkBoardReviewIntent).where(
                    WorkBoardReviewIntent.task_id == task_id,
                    WorkBoardReviewIntent.attempt_id == attempt_id,
                )
            )
        ).scalar_one()
        assert projected.status is WorkBoardStatus.review
        assert projected.requires_review is True
        assert intent.workflow_run_id == job_id
        assert intent.status == "projected"


def _valid_goal_snapshot_child_projection(task, attempt, *, root_job_id="work-board:task:attempt", parent_fence=4):
    child_job_id = f"goal-snapshot-work-board:{task.task_id}:{attempt.attempt_id}"
    return {
        "job_id": child_job_id,
        "run_identity": child_job_id,
        "parent_run_identity": root_job_id,
        "parent_job_id": root_job_id,
        "root_run_identity": root_job_id,
        "parent_fencing_token": parent_fence,
        "job_kind": GOAL_SNAPSHOT_CAPABILITY,
        "capability_version": "1",
        "status": "succeeded",
        "owner": {
            "kind": "service",
            "principal_id": "service:goal-snapshot",
            "service_id": "service:goal-snapshot",
        },
        "session_id": task.owner_session_id,
        "operator_session_id": task.owner_session_id,
        "goal_id": task.goal_id,
        "goal_revision": task.goal_revision,
        "declared_authority": {
            "capability_id": GOAL_SNAPSHOT_CAPABILITY,
            "capability_version": "1",
            "principal": "service:goal-snapshot",
            "owner_kind": "service",
            "owner_principal_id": "service:goal-snapshot",
            "service_id": "service:goal-snapshot",
            "session_id": task.owner_session_id,
            "goal_id": task.goal_id,
            "goal_revision": task.goal_revision,
            "goal_owner_principal_id": task.owner_principal_id,
            "goal_owner_session_id": task.owner_session_id,
        },
    }


@pytest.mark.parametrize(
    ("mutation",),
    [
        (lambda projection: projection.update({"root_run_identity": "foreign-root"}),),
        (lambda projection: projection.update({"parent_fencing_token": 99}),),
        (lambda projection: projection.update({"goal_revision": "invalid"}),),
        (lambda projection: projection.update({"parent_fencing_token": "invalid"}),),
        (lambda projection: projection["declared_authority"].update({"session_id": "foreign-session"}),),
        (lambda projection: projection["declared_authority"].update({"goal_revision": "invalid"}),),
    ],
)
def test_goal_snapshot_child_foreign_lineage_is_rejected(mutation):
    task = SimpleNamespace(
        task_id="task",
        attempt_id="unused",
        owner_principal_id="operator:one",
        owner_session_id="session-one",
        goal_id="goal-one",
        goal_revision=3,
    )
    attempt = SimpleNamespace(
        attempt_id="attempt",
        workflow_run_id="work-board:task:attempt",
    )
    projection = _valid_goal_snapshot_child_projection(task, attempt)
    child_job_id = projection["job_id"]
    assert WorkBoardDispatcher._goal_snapshot_child_lineage_matches(
        task,
        attempt,
        projection,
        child_job_id=child_job_id,
        parent_job_id="work-board:task:attempt",
        parent_fencing_token=4,
    )

    mutation(projection)
    assert not WorkBoardDispatcher._goal_snapshot_child_lineage_matches(
        task,
        attempt,
        projection,
        child_job_id=child_job_id,
        parent_job_id="work-board:task:attempt",
        parent_fencing_token=4,
    )


@pytest.mark.asyncio
async def test_direct_binding_lookup_unavailable_is_not_proved_absent():
    """A missing runtime lookup cannot close a pending board claim."""

    dispatcher = WorkBoardDispatcher(jobs=SimpleNamespace())
    with pytest.raises(DurableJobError, match="lookup_unavailable"):
        await dispatcher._lookup_direct_job_id(
            _task("guardian.research-watch.v1"),
            _attempt(),
            {"watch_id": "watch-1", "expected_plan_revision": 1},
        )


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
        executor_id="executor-local",
        priority=50,
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
    inputs = {"watch_id": "watch-1", "expected_plan_revision": 1}
    expected_identity = WorkBoardDispatcher._direct_expected_identity(
        task,
        attempt,
        inputs,
    )
    attempt.workflow_run_id = expected_identity["job_id"]

    class Jobs:
        async def get_job(self, _job_id):
            return {
                "job_id": expected_identity["job_id"],
                "run_identity": expected_identity["job_id"],
                "status": "running",
                "owner": {
                    "principal_id": expected_identity["owner_principal_id"],
                    "kind": expected_identity["owner_kind"],
                    "service_id": expected_identity["service_id"],
                },
                "job_kind": expected_identity["job_kind"],
                "capability_version": expected_identity["capability_version"],
                "session_id": expected_identity["session_id"],
                "operator_session_id": expected_identity["operator_session_id"],
                "goal_id": expected_identity["goal_id"],
                "goal_revision": expected_identity["goal_revision"],
                "declared_authority": {"capability_id": expected_identity["capability_id"]},
                "idempotency": {
                    "scope": expected_identity["idempotency_scope"],
                    "key": expected_identity["idempotency_key"],
                },
                "input_digest": expected_identity["input_digest"],
                "authority_digest": expected_identity["authority_digest"],
                "run_fingerprint": expected_identity["run_fingerprint"],
                "lease": {},
            }

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
    monkeypatch.setattr("src.work_board.dispatcher._parse_typed_input", lambda _task: inputs)
    dispatcher._lookup_linked_binding = lambda *_args, **_kwargs: _async_value(attempt.workflow_run_id)

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
        ("goal_snapshot_executed_and_verified", "goal_snapshot_executed_and_verified"),
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
async def test_linked_running_missing_lease_fails_closed(monkeypatch):
    """A linked running root without an expiry cannot remain Running forever."""

    task = SimpleNamespace(
        task_id="task-missing-lease",
        owner_principal_id="operator:one",
        owner_session_id="session-one",
        goal_id="goal-one",
        goal_revision=3,
        capability_id="guardian.research-watch.v1",
        status=WorkBoardStatus.running,
        task_revision=4,
        requires_review=False,
    )
    attempt = SimpleNamespace(
        task_id=task.task_id,
        attempt_id="attempt-missing-lease",
        workflow_run_id="source-watch:watch-1:attempt-missing-lease",
        ended_at=None,
        cancel_requested_at=None,
        lease_owner="service:work-board",
        fencing_token=2,
    )

    class Jobs:
        async def get_job(self, job_id):
            return {
                "job_id": job_id,
                "run_identity": job_id,
                "status": "running",
                "effects": [],
                "lease": {},
            }

    class Repository:
        async def list_linked_active_attempts(self, _db, *, limit):
            assert limit > 0
            return [(task, attempt)]

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    dispatcher = WorkBoardDispatcher(
        repository=Repository(),
        jobs=Jobs(),
        session_provider=lambda: Session(),
    )
    monkeypatch.setattr(
        "src.work_board.dispatcher._parse_typed_input",
        lambda _task: {"watch_id": "watch-1", "expected_plan_revision": 3},
    )
    dispatcher._lookup_linked_binding = lambda *_args, **_kwargs: _async_value(
        attempt.workflow_run_id
    )
    projected: list[dict[str, Any]] = []

    async def project(*_args, **kwargs):
        projected.append(kwargs)

    dispatcher._project = project

    recovered = await dispatcher.reconcile_linked_attempts()

    assert recovered == [attempt.workflow_run_id]
    assert projected
    assert projected[0]["status"] is WorkBoardStatus.blocked
    assert projected[0]["block_kind"] == "unknown_effect"
    assert projected[0]["block_reason"] == "reconcile_external_effect"


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("post_claim_error", "post_claim_reason"),
    [
        ("session_not_found", "The current owner session is no longer valid"),
        ("workflow_not_loaded_or_disabled", "The registered capability lane is disabled"),
    ],
)
async def test_post_claim_authority_race_closes_pending_attempt_before_admission(
    post_claim_error,
    post_claim_reason,
):
    task = _dispatch_task("post-claim-authority-race", status=WorkBoardStatus.ready, priority=50, sequence=1)
    attempt = SimpleNamespace(
        attempt_id="attempt-post-claim-authority-race",
        task_id=task.task_id,
        fencing_token=1,
        lease_owner="service:work-board",
        parent_handoff_context_json="[]",
        parent_handoff_digest=None,
    )

    class Repository:
        async def list_dispatch_candidates(self, _db, **_kwargs):
            return [task]

        async def claim_ready_task(self, _db, task_id, **_kwargs):
            assert task_id == task.task_id
            return BoardDispatchClaim(task, attempt, SimpleNamespace(event_id=1))

    dispatcher = WorkBoardDispatcher(repository=Repository(), session_provider=lambda: _Session())

    async def no_expired_reviews(*_args, **_kwargs):
        return 0

    dispatcher._expire_review_windows = no_expired_reviews
    dispatcher.reconcile_pending_attempts = _empty_reconcile
    dispatcher.reconcile_linked_attempts = _empty_reconcile
    dispatcher._effective_runtime = _runtime
    readiness_calls = 0

    async def readiness(_task):
        nonlocal readiness_calls
        readiness_calls += 1
        if readiness_calls == 1:
            return None, None
        return post_claim_error, post_claim_reason

    dispatcher._readiness = readiness
    closed: list[dict[str, Any]] = []

    async def close_unadmitted(claim, reason, **kwargs):
        closed.append({"claim": claim, "reason": reason, **kwargs})

    dispatcher._close_unadmitted_or_block = close_unadmitted
    admitted = False

    async def admit(_claim):
        nonlocal admitted
        admitted = True
        return {"admitted": True, "completed": False, "blocked": False}

    dispatcher._admit_execute_project = admit
    receipt = await dispatcher.run_pass()

    assert readiness_calls == 2
    assert not admitted
    assert receipt["claimed"] == 0
    assert receipt["admitted"] == 0
    assert receipt["blocked"] == 1
    assert len(closed) == 1
    assert closed[0]["claim"] is not None
    assert closed[0]["reason"] == post_claim_error
    assert closed[0]["retryable_input"] is True


@pytest.mark.asyncio
async def test_post_link_exception_reconciles_active_durable_root_before_block():
    """A caller failure after link reads the durable root before board projection."""

    task = SimpleNamespace(
        task_id="task-post-link-recovery",
        owner_principal_id="operator:one",
        owner_session_id="session-one",
        goal_id="goal-one",
        goal_revision=3,
        capability_id=GOAL_SNAPSHOT_CAPABILITY,
        task_revision=1,
        requires_review=False,
        priority=50,
    )
    attempt = SimpleNamespace(
        task_id=task.task_id,
        attempt_id="attempt-post-link-recovery",
        fencing_token=7,
        lease_owner="service:work-board",
        workflow_run_id=None,
        ended_at=None,
    )
    job_id = f"work-board:{task.task_id}:{attempt.attempt_id}"
    linked = False
    reads_after_link: list[str] = []
    projected: list[dict[str, Any]] = []

    class Jobs:
        def __init__(self):
            self.status = "accepted"

        async def admit_job(self, _spec):
            return {"job_id": job_id, "run_identity": job_id, "status": self.status, "revision": 1}

        async def queue_job(self, requested_job_id, **_kwargs):
            assert requested_job_id == job_id
            assert linked is True
            self.status = "queued"
            raise RuntimeError("injected post-link queue failure")

        async def get_job(self, requested_job_id):
            assert requested_job_id == job_id
            assert linked is True
            reads_after_link.append(requested_job_id)
            return {
                "job_id": job_id,
                "run_identity": job_id,
                "status": self.status,
                "revision": 2,
                "effects": [],
            }

    class Repository:
        async def link_attempt_workflow_run(self, _db, *_args, **_kwargs):
            nonlocal linked
            linked = True
            attempt.workflow_run_id = job_id
            return SimpleNamespace(task=SimpleNamespace(task_revision=2))

        async def get_detail(self, _db, _owner, task_id):
            assert task_id == task.task_id
            current_values = vars(task).copy()
            current_values["task_revision"] = 2
            current_task = SimpleNamespace(**current_values)
            return {"task": current_task, "attempts": [attempt]}

        async def project_attempt(self, *_args, **kwargs):
            projected.append(dict(kwargs))
            return None

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Spec:
        identity = SimpleNamespace(
            job_id=job_id,
            owner_principal_id="service:work-board",
            owner_kind="service",
            job_kind=GOAL_SNAPSHOT_CAPABILITY,
            capability_version="1",
            idempotency_scope="work-board-attempt",
            idempotency_key=f"{task.task_id}:{attempt.attempt_id}",
        )
        service_id = "service:work-board"
        goal_id = task.goal_id
        goal_revision = task.goal_revision
        operator_session_id = task.owner_session_id
        session_id = task.owner_session_id
        inputs = {}
        declared_authority = {"finite_authority": True}
        run_fingerprint = "f" * 64

    jobs = Jobs()
    dispatcher = WorkBoardDispatcher(
        repository=Repository(),
        jobs=jobs,
        session_provider=lambda: Session(),
    )

    async def runtime(_task):
        return 300

    dispatcher._effective_runtime = runtime
    dispatcher._build_spec = lambda *_args, **_kwargs: (
        Spec(),
        {},
        job_id,
        "service:work-board",
        300,
    )

    async def record_projection(*_args, **kwargs):
        projected.append(dict(kwargs))

    dispatcher._project = record_projection
    dispatcher._project_blocked = record_projection
    claim = BoardDispatchClaim(task, attempt, SimpleNamespace(event_id=1))

    result = await dispatcher._admit_execute_project(claim)

    assert result == {"admitted": True, "completed": False, "blocked": True}
    assert linked is True
    assert reads_after_link == [job_id]
    assert projected == []


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


def _goal_snapshot_readiness_task() -> SimpleNamespace:
    return SimpleNamespace(
        task_id="task-goal-snapshot-readiness",
        task_revision=1,
        status=WorkBoardStatus.todo,
        owner_principal_id="operator:goal-snapshot",
        owner_session_id="goal-snapshot-session",
        goal_id="goal-goal-snapshot-readiness",
        goal_revision=7,
        capability_id=GOAL_SNAPSHOT_CAPABILITY,
        executor_id="executor.goal-snapshot",
        typed_input_ref="workspace-json:inputs/goal-snapshot.json",
        typed_input_digest="a" * 64,
        scheduled_at=None,
        priority=50,
    )


def _goal_snapshot_goal(*, criterion: GoalSuccessCriterion | None) -> Goal:
    return Goal(
        id="goal-goal-snapshot-readiness",
        title="GoalSnapshot readiness goal",
        status="active",
        revision=7,
        owner_principal_id="operator:goal-snapshot",
        owner_session_id="goal-snapshot-session",
        success_criterion_json=criterion.model_dump_json() if criterion is not None else None,
    )


class _ReadinessResult:
    def __init__(self, goal: Goal):
        self.goal = goal

    def scalar_one_or_none(self):
        return self.goal


class _ReadinessEmptyResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _ReadinessSession:
    def __init__(self, goal: Goal):
        self.goal = goal

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _statement):
        statement = _statement
        if any(
            description.get("entity") is WorkBoardTask
            for description in getattr(statement, "column_descriptions", ())
        ):
            return _ReadinessEmptyResult()
        return _ReadinessResult(self.goal)


class _ReadinessRepository:
    def __init__(self, task: SimpleNamespace):
        self.task = task
        self.promotions: list[dict[str, Any]] = []

    async def list_dispatch_candidates(self, _db, **_kwargs):
        return [self.task]

    async def promote_task_ready(self, _db, task_id, **kwargs):
        self.promotions.append({"task_id": task_id, **kwargs})
        blocked_values = vars(self.task).copy()
        blocked_values["status"] = WorkBoardStatus.blocked
        blocked = SimpleNamespace(**blocked_values)
        return SimpleNamespace(task=blocked)


class _ReadinessJobs:
    def __init__(self):
        self.admit_calls = 0

    async def admit_job(self, _spec):
        self.admit_calls += 1
        raise AssertionError("GoalSnapshot readiness must block before durable admission")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("criterion", "expected_error"),
    [
        (None, "goal_snapshot_criterion_missing"),
        (
            GoalSuccessCriterion(
                description="Write the goal snapshot",
                verifier_kind=None,
                evidence_refs=["operator:goal-snapshot-proof"],
            ),
            "goal_snapshot_verifier_missing",
        ),
        (
            GoalSuccessCriterion(
                description="Write the goal snapshot",
                verifier_kind=CriterionVerifierKind.artifact_readback,
                evidence_refs=[],
            ),
            "goal_snapshot_evidence_missing",
        ),
    ],
)
async def test_goal_snapshot_missing_success_contract_blocks_before_claim_or_admission(
    monkeypatch,
    criterion: GoalSuccessCriterion | None,
    expected_error: str,
):
    task = _goal_snapshot_readiness_task()
    goal = _goal_snapshot_goal(criterion=criterion)
    repository = _ReadinessRepository(task)
    jobs = _ReadinessJobs()
    dispatcher = WorkBoardDispatcher(
        repository=repository,
        jobs=jobs,
        session_provider=lambda: _ReadinessSession(goal),
    )
    dispatcher.reconcile_pending_attempts = _empty_reconcile
    dispatcher.reconcile_linked_attempts = _empty_reconcile
    async def authenticated(*_args, **_kwargs):
        return SimpleNamespace(
            principal=SimpleNamespace(principal_id=task.owner_principal_id),
        )

    monkeypatch.setattr(
        "src.work_board.dispatcher.authenticate_session",
        authenticated,
    )
    monkeypatch.setattr(
        "src.work_board.dispatcher._parse_typed_input",
        lambda _task: {"file_path": "artifacts/goal-snapshot.md"},
    )

    receipt = await dispatcher.run_pass()

    assert receipt["claimed"] == 0
    assert receipt["admitted"] == 0
    assert receipt["blocked"] == 1
    assert jobs.admit_calls == 0
    assert repository.promotions[0]["readiness_error"] == expected_error
    assert repository.promotions[0]["readiness_reason"]
    assert "private" not in repository.promotions[0]["readiness_reason"].lower()


@pytest.mark.asyncio
async def test_goal_snapshot_artifact_readback_success_contract_passes_readiness(monkeypatch):
    task = _goal_snapshot_readiness_task()
    goal = _goal_snapshot_goal(
        criterion=GoalSuccessCriterion(
            description="Write the goal snapshot and verify its artifact",
            verifier_kind=CriterionVerifierKind.artifact_readback,
            evidence_refs=["operator:goal-snapshot-proof"],
        )
    )
    dispatcher = WorkBoardDispatcher(session_provider=lambda: _ReadinessSession(goal))
    async def authenticated(*_args, **_kwargs):
        return SimpleNamespace(
            principal=SimpleNamespace(principal_id=task.owner_principal_id),
        )

    monkeypatch.setattr(
        "src.work_board.dispatcher.authenticate_session",
        authenticated,
    )
    monkeypatch.setattr(
        "src.work_board.dispatcher._parse_typed_input",
        lambda _task: {"file_path": "artifacts/goal-snapshot.md"},
    )

    async def capability_preflight(_task, _goal, _inputs):
        return None, None

    dispatcher._capability_preflight = capability_preflight

    assert await dispatcher._readiness(task) == (None, None)


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


@pytest.mark.asyncio
async def test_priority_then_fifo(async_db):
    """Candidate ordering is read from the real board rows, not a caller list."""

    repository = WorkBoardRepository()
    async with async_db() as db:
        db.add_all(
            [
                WorkBoardTask(
                    task_id="priority-low",
                    owner_principal_id=OWNER.principal_id,
                    owner_session_id=OWNER.session_id,
                    goal_id="goal-order",
                    goal_revision=1,
                    title="Low",
                    idempotency_key="priority-low",
                    status=WorkBoardStatus.ready,
                    priority=20,
                ),
                WorkBoardTask(
                    task_id="priority-high-old",
                    owner_principal_id=OWNER.principal_id,
                    owner_session_id=OWNER.session_id,
                    goal_id="goal-order",
                    goal_revision=1,
                    title="High old",
                    idempotency_key="priority-high-old",
                    status=WorkBoardStatus.ready,
                    priority=90,
                ),
                WorkBoardTask(
                    task_id="priority-high-new",
                    owner_principal_id=OWNER.principal_id,
                    owner_session_id=OWNER.session_id,
                    goal_id="goal-order",
                    goal_revision=1,
                    title="High new",
                    idempotency_key="priority-high-new",
                    status=WorkBoardStatus.ready,
                    priority=90,
                ),
            ]
        )
        db.add_all(
            [
                WorkBoardTask(
                    task_id=f"priority-todo-{index}",
                    owner_principal_id=OWNER.principal_id,
                    owner_session_id=OWNER.session_id,
                    goal_id="goal-order",
                    goal_revision=1,
                    title=f"Todo backlog {index}",
                    idempotency_key=f"priority-todo-{index}",
                    status=WorkBoardStatus.todo,
                    priority=100,
                )
                for index in range(30)
            ]
        )
        await db.flush()
        candidates = await repository.list_dispatch_candidates(db, limit=2)

    assert [item.task_id for item in candidates] == [
        "priority-high-old",
        "priority-high-new",
    ]


@pytest.mark.asyncio
async def test_racing_passes_create_one_attempt(tmp_path: Path):
    """Two real SQLite writers can produce only one fenced active attempt."""

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'racing-dispatch.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    runtime_tables = [
        Session.__table__,
        Goal.__table__,
        WorkBoardTask.__table__,
        WorkBoardAttempt.__table__,
        WorkBoardEvent.__table__,
        WorkBoardLink.__table__,
        WorkBoardProposal.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: SQLModel.metadata.create_all(sync, tables=runtime_tables)
        )
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def durable_session():
        async with factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async with factory() as db:
        db.add(
            Goal(
                id="goal-racing",
                title="Racing goal",
                status="active",
                revision=1,
                owner_principal_id=OWNER.principal_id,
                owner_session_id=OWNER.session_id,
            )
        )
        db.add(
            WorkBoardTask(
                task_id="task-racing-real",
                owner_principal_id=OWNER.principal_id,
                owner_session_id=OWNER.session_id,
                goal_id="goal-racing",
                goal_revision=1,
                title="Racing",
                idempotency_key="task-racing-real",
                status=WorkBoardStatus.ready,
                priority=50,
            )
        )
        await db.commit()

    async def empty_reconcile(*_args, **_kwargs):
        return []

    async def ready(_task):
        return None, None

    async def runtime(_task):
        return 300

    async def admitted(_claim):
        return {"admitted": True, "completed": False, "blocked": False}

    dispatchers = [
        WorkBoardDispatcher(
            repository=WorkBoardRepository(),
            session_provider=durable_session,
        )
        for _ in range(2)
    ]
    for dispatcher in dispatchers:
        dispatcher.reconcile_pending_attempts = empty_reconcile
        dispatcher.reconcile_linked_attempts = empty_reconcile
        dispatcher._readiness = ready
        dispatcher._effective_runtime = runtime
        dispatcher._admit_execute_project = admitted

    first, second = await asyncio.gather(*(dispatcher.run_pass() for dispatcher in dispatchers))
    assert first["claimed"] + second["claimed"] == 1
    async with factory() as db:
        attempts = list((await db.execute(select(WorkBoardAttempt))).scalars().all())
        task = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == "task-racing-real"))
        ).scalar_one()
    assert len(attempts) == 1
    assert task.status is WorkBoardStatus.running
    await engine.dispose()


@pytest.mark.asyncio
async def test_pending_admission_reconciles_after_restart(tmp_path: Path, monkeypatch):
    """A claimed pending row links an existing exact durable admission after restart."""

    workspace = tmp_path / "workspace"
    (workspace / "inputs").mkdir(parents=True)
    raw = json.dumps(
        {
            "schema_version": 1,
            "capability_id": GOAL_SNAPSHOT_CAPABILITY,
            "input": {"file_path": "artifacts/restart.md"},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    (workspace / "inputs" / "restart.json").write_bytes(raw)
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'pending-admission.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    runtime_tables = [
        Session.__table__,
        Goal.__table__,
        WorkBoardTask.__table__,
        WorkBoardAttempt.__table__,
        WorkBoardEvent.__table__,
        WorkBoardLink.__table__,
        WorkflowRunState.__table__,
        Secret.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: SQLModel.metadata.create_all(sync, tables=runtime_tables)
        )
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def durable_session():
        async with factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    monkeypatch.setattr("src.workflows.job_runtime.get_session", durable_session)
    monkeypatch.setattr("src.vault.repository.get_session", durable_session)
    repository = WorkBoardRepository()
    jobs = DurableJobRepository()
    task = WorkBoardTask(
        task_id="task-pending-restart",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        goal_id="goal-pending-restart",
        goal_revision=1,
        title="Pending restart",
        idempotency_key="task-pending-restart",
        status=WorkBoardStatus.ready,
        capability_id=GOAL_SNAPSHOT_CAPABILITY,
        typed_input_ref="workspace-json:inputs/restart.json",
        typed_input_digest=hashlib.sha256(raw).hexdigest(),
        executor_id=registered_executor_id(GOAL_SNAPSHOT_CAPABILITY),
    )
    async with factory() as db:
        db.add(
            Goal(
                id="goal-pending-restart",
                title="Pending goal",
                status="active",
                revision=1,
                owner_principal_id=OWNER.principal_id,
                owner_session_id=OWNER.session_id,
            )
        )
        db.add(task)
        await db.commit()

    async with factory() as db:
        claim = await repository.claim_ready_task(
            db,
            task.task_id,
            expected_revision=1,
            lease_owner="service:work-board",
            lease_seconds=300,
        )
        assert claim is not None
        await db.commit()

    dispatcher = WorkBoardDispatcher(
        repository=repository,
        jobs=jobs,
        session_provider=durable_session,
    )
    spec, _inputs, expected_job_id, _owner, _runtime = dispatcher._build_spec(
        claim.task,
        claim.attempt,
        runtime_seconds=300,
    )
    admitted = await jobs.admit_job(spec)
    assert admitted["job_id"] == expected_job_id
    assert admitted["status"] == "accepted"
    restarted = WorkBoardDispatcher(
        repository=repository,
        jobs=jobs,
        session_provider=durable_session,
    )
    recovered = await restarted.reconcile_pending_attempts()
    assert recovered == [expected_job_id]
    async with factory() as db:
        linked = (
            await db.execute(
                select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == claim.attempt.attempt_id)
            )
        ).scalar_one()
    assert linked.workflow_run_id == expected_job_id
    await engine.dispose()


@pytest.mark.asyncio
async def test_stale_fence_cannot_attach_output(async_db, tmp_path: Path, monkeypatch):
    repository = WorkBoardRepository()
    workspace = tmp_path / "workspace"
    (workspace / "inputs").mkdir(parents=True)
    raw = json.dumps(
        {
            "schema_version": 1,
            "capability_id": GOAL_SNAPSHOT_CAPABILITY,
            "input": {"file_path": "artifacts/stale.md"},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    (workspace / "inputs" / "stale.json").write_bytes(raw)
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    async with async_db() as db:
        db.add(
            Goal(
                id="goal-stale-output",
                title="Stale output goal",
                status="active",
                revision=1,
                owner_principal_id=OWNER.principal_id,
                owner_session_id=OWNER.session_id,
            )
        )
        db.add(
            WorkBoardTask(
                task_id="task-stale-output",
                owner_principal_id=OWNER.principal_id,
                owner_session_id=OWNER.session_id,
                goal_id="goal-stale-output",
                goal_revision=1,
                title="Stale output",
                idempotency_key="task-stale-output",
                status=WorkBoardStatus.ready,
                capability_id=GOAL_SNAPSHOT_CAPABILITY,
                typed_input_ref="workspace-json:inputs/stale.json",
                typed_input_digest=hashlib.sha256(raw).hexdigest(),
                executor_id=registered_executor_id(GOAL_SNAPSHOT_CAPABILITY),
            )
        )
        await db.flush()
        claim = await repository.claim_ready_task(
            db,
            "task-stale-output",
            expected_revision=1,
            lease_owner="service:work-board",
        )
        assert claim is not None
        await db.commit()
        dispatcher = WorkBoardDispatcher(repository=repository)
        jobs = DurableJobRepository()
        spec, _inputs, job_id, _owner, _runtime = dispatcher._build_spec(
            claim.task,
            claim.attempt,
            runtime_seconds=300,
        )
        admitted = await jobs.admit_job(spec)
        queued = await jobs.queue_job(job_id, expected_revision=admitted["revision"])
        leased = await jobs.claim_job(
            job_id,
            owner="worker:stale-proof",
            lease_seconds=300,
            expected_revision=queued["revision"],
            expected_fencing_token=queued["lease"]["fencing_token"],
        )
        linked = await repository.link_attempt_workflow_run(
            db,
            claim.task.task_id,
            claim.attempt.attempt_id,
            workflow_run_id=job_id,
            expected_revision=claim.task.task_revision,
            board_fence=claim.attempt.fencing_token,
            lease_owner=claim.attempt.lease_owner or "service:work-board",
            workflow_projection=leased,
            expected_identity={
                "owner_principal_id": spec.identity.owner_principal_id,
                "owner_kind": spec.identity.owner_kind,
                "service_id": spec.service_id,
                "goal_id": spec.goal_id,
                "goal_revision": spec.goal_revision,
                "operator_session_id": spec.operator_session_id,
                "session_id": spec.session_id,
                "capability_id": spec.identity.job_kind,
                "capability_version": spec.identity.capability_version,
                "input_digest": hashlib.sha256(json.dumps(spec.inputs, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest(),
                "authority_digest": hashlib.sha256(json.dumps(spec.declared_authority, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest(),
                "run_fingerprint": spec.run_fingerprint,
                "idempotency_scope": spec.identity.idempotency_scope,
                "idempotency_key": spec.identity.idempotency_key,
            },
        )
        readback = await jobs.record_effect(
            job_id,
            effect_type="board_child_readback",
            receipt_kind="readback",
            status="succeeded",
            target_path="artifacts/stale.md",
            target_digest="a" * 64,
            content_sha256="a" * 64,
            details={"verified": True},
            owner="worker:stale-proof",
            fencing_token=leased["lease"]["fencing_token"],
            expected_revision=leased["revision"],
        )
        await jobs.transition_job(
            job_id,
            "succeeded",
            owner="worker:stale-proof",
            fencing_token=leased["lease"]["fencing_token"],
            expected_revision=readback["revision"],
            result={"content_sha256": "a" * 64, "verified": True},
            result_summary="verified readback",
        )
        with pytest.raises(BoardError, match="stale"):
            await repository.project_attempt(
                db,
                claim.task.task_id,
                claim.attempt.attempt_id,
                expected_revision=linked.task.task_revision,
                board_fence=claim.attempt.fencing_token + 1,
                lease_owner="service:work-board",
                status=WorkBoardStatus.done,
                outcome="verified",
                verified_readback={
                    "source": "workflow_run",
                    "receipt_kind": "readback",
                    "status": "succeeded",
                    "verified": True,
                    "workflow_run_id": job_id,
                    "readback_id": "readback-stale-fence",
                    "content_sha256": "a" * 64,
                    "verified_at": "2026-09-24T12:34:56+00:00",
                },
            )


@pytest.mark.asyncio
async def test_unknown_effect_never_auto_retries(async_db):
    repository = WorkBoardRepository()
    async with async_db() as db:
        db.add(
            Goal(
                id="goal-unknown-effect",
                title="Unknown effect goal",
                status="active",
                revision=1,
                owner_principal_id=OWNER.principal_id,
                owner_session_id=OWNER.session_id,
            )
        )
        task = WorkBoardTask(
            task_id="task-unknown-effect",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            goal_id="goal-unknown-effect",
            goal_revision=1,
            title="Unknown effect",
            idempotency_key="task-unknown-effect",
            status=WorkBoardStatus.blocked,
            block_kind="unknown_effect",
            block_reason="Reconciliation required",
            block_source_status=WorkBoardStatus.ready.value,
            task_revision=4,
        )
        db.add(task)
        await db.flush()
        db.add(
            WorkBoardAttempt(
                task_id=task.task_id,
                executor_id="executor.unknown",
                fencing_token=1,
                ended_at=datetime.now(timezone.utc),
                outcome="unknown_effect",
                receipt_refs_json=json.dumps(
                    [{"job_id": "run-unknown", "status": "unknown", "reason_code": "unknown_effect"}]
                ),
            )
        )
        await db.flush()
        with pytest.raises(BoardError, match="typed recovery"):
            await repository.retry_task(
                db,
                OWNER,
                task.task_id,
                expected_revision=task.task_revision,
            )
        refreshed = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task.task_id))
        ).scalar_one()
        assert refreshed.status is WorkBoardStatus.blocked
