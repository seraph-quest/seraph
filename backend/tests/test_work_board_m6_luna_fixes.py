"""Focused regression tests for the M6 Luna Max recovery findings."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.auth.service import AuthFailure
from src.db.models import WorkBoardStatus
from src.work_board.dispatcher import WorkBoardDispatcher
import src.work_board.dispatcher as dispatcher_module
from src.workflows.routines import RoutineError, RoutineExecuteRequest, RoutineService
import src.workflows.routines as routines_module


OWNER = "operator:luna-fix"
SESSION = "session:luna-fix"


def _routine_job(*, status: str = "awaiting_approval") -> dict[str, object]:
    return {
        "job_id": "routine-invocation:routine-1:invocation-1",
        "run_identity": "routine-invocation:routine-1:invocation-1",
        "job_kind": "routine_invocation",
        "status": status,
        "owner": {"kind": "user", "principal_id": OWNER},
        "session_id": SESSION,
        "operator_session_id": SESSION,
        "goal_id": "goal-1",
        "goal_revision": 3,
        "revision": 8,
        "lease": {"owner": "routine:parent", "fencing_token": 4},
        "declared_authority": {
            "goal_owner_principal_id": OWNER,
            "goal_owner_session_id": SESSION,
            "session_id": SESSION,
            "routine_id": "routine-1",
            "routine_revision": 7,
            "routine_version": 2,
            "package_digest": "d" * 64,
            "approval_id": "approval-routine-1",
            "invocation_uuid": "11111111-1111-4111-8111-111111111111",
        },
        "effects": [],
    }


@pytest.mark.asyncio
async def test_approved_routine_cannot_consume_approval_after_package_revoke(monkeypatch):
    service = RoutineService()
    job = _routine_job()
    monkeypatch.setattr(
        service,
        "_require_active_routine",
        AsyncMock(return_value=SimpleNamespace(state="active", revision=7, current_version=2, owner_session_id=SESSION)),
    )
    monkeypatch.setattr(
        service,
        "_version",
        AsyncMock(return_value=SimpleNamespace(installed_package_digest="d" * 64)),
    )
    monkeypatch.setattr(
        service,
        "_package_readback",
        lambda *_args: {"status": "quarantined", "digest": "d" * 64},
    )
    get_job = AsyncMock(return_value=job)
    monkeypatch.setattr(routines_module.durable_job_repository, "get_job", get_job)
    monkeypatch.setattr(
        routines_module.durable_job_repository,
        "transition_job",
        AsyncMock(return_value={**job, "status": "blocked"}),
    )
    resume = AsyncMock(side_effect=AssertionError("revoked package must not consume approval"))
    monkeypatch.setattr(service, "_resume_approval", resume)

    result = await service.execute_invocation(
        "routine-1",
        "routine-invocation:routine-1:invocation-1",
        RoutineExecuteRequest(approval_id="approval-routine-1", expected_routine_revision=7),
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )

    assert result["status"] == "blocked"
    assert result["reason_code"] == "package_review_required"
    resume.assert_not_awaited()


@pytest.mark.asyncio
async def test_m3_recovery_blocks_after_package_mutation_without_external_write(monkeypatch):
    service = RoutineService()
    job = _routine_job(status="blocked")
    monkeypatch.setattr(service, "_routine", AsyncMock(return_value=SimpleNamespace(owner_session_id=SESSION, revision=7, current_version=2)))
    monkeypatch.setattr(routines_module.durable_job_repository, "get_job", AsyncMock(return_value=job))
    monkeypatch.setattr(
        service,
        "_require_active_routine",
        AsyncMock(return_value=SimpleNamespace(state="active", revision=7, current_version=2, owner_session_id=SESSION)),
    )
    monkeypatch.setattr(
        service,
        "_require_active_package_binding",
        AsyncMock(side_effect=RoutineError("package_review_required")),
    )
    monkeypatch.setattr(
        routines_module.GitHubFollowthroughService,
        "execute",
        AsyncMock(side_effect=AssertionError("mutated package must not publish M3")),
    )

    result = await service.recover(
        "routine-1",
        str(job["job_id"]),
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
        external_mutation_granted=True,
    )

    assert result == {
        "status": "blocked",
        "job_id": str(job["job_id"]),
        "reason_code": "package_review_required",
        "recovery_action": "restore_prerequisite",
        "operator_visible": True,
        "learning": "no_learning",
    }


class _LinkedRepository:
    async def list_linked_active_attempts(self, _db, *, limit):
        assert limit > 0
        return [(self.task, self.attempt)]


class _LinkedSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_code", ["session_revoked", "session_expired"])
async def test_routine_recovery_revalidates_current_owner_session(monkeypatch, auth_code):
    task = SimpleNamespace(
        task_id="task-session-recovery",
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
        goal_id="goal-1",
        goal_revision=3,
        capability_id="guardian-routine.v1",
        # A prior dispatch pass has already projected the exact approval wait
        # as Blocked and released its board lease. Restart recovery must still
        # revalidate the owner session before it reacquires that same attempt.
        status=WorkBoardStatus.blocked,
        task_revision=4,
        requires_review=False,
        block_reason="awaiting_approval",
    )
    attempt = SimpleNamespace(
        task_id=task.task_id,
        attempt_id="attempt-session-recovery",
        workflow_run_id="routine-invocation:routine-1:session-recovery",
        ended_at=None,
        cancel_requested_at=None,
        lease_owner=None,
        fencing_token=4,
    )
    repository = _LinkedRepository()
    repository.task = task
    repository.attempt = attempt
    projection = {
        "job_id": attempt.workflow_run_id,
        "run_identity": attempt.workflow_run_id,
        "status": "awaiting_approval",
        "effects": [],
    }

    class Jobs:
        async def get_job(self, job_id):
            assert job_id == attempt.workflow_run_id
            return dict(projection)

    async def authenticate(_session_id, *, touch):
        assert touch is False
        raise AuthFailure(auth_code)

    dispatcher = WorkBoardDispatcher(
        repository=repository,
        jobs=Jobs(),
        session_provider=lambda: _LinkedSession(),
    )
    monkeypatch.setattr(dispatcher_module, "authenticate_session", authenticate)
    monkeypatch.setattr(dispatcher_module, "_parse_typed_input", lambda _task: {})
    dispatcher._lookup_linked_binding = AsyncMock(return_value=attempt.workflow_run_id)
    dispatcher._execute_direct_adapter = AsyncMock(side_effect=AssertionError("revoked session must not run the watch"))
    dispatcher._project = AsyncMock()
    dispatcher.resume_routine_attempt_for_operator_recovery = AsyncMock(
        side_effect=AssertionError("revoked session must not reacquire the board attempt")
    )

    recovered = await dispatcher.reconcile_linked_attempts()

    assert recovered == [attempt.workflow_run_id]
    dispatcher._execute_direct_adapter.assert_not_awaited()
    dispatcher.resume_routine_attempt_for_operator_recovery.assert_not_awaited()
    # Keep the already-visible approval block stable until an authenticated
    # operator starts an explicit recovery action.
    assert task.status is WorkBoardStatus.blocked
    assert task.block_reason == "awaiting_approval"
    dispatcher._project.assert_not_awaited()


@pytest.mark.asyncio
async def test_adopted_m3_persists_parent_and_child_prepared_checkpoints(monkeypatch):
    service = RoutineService()
    parent = {
        "job_id": "routine-parent",
        "status": "running",
        "revision": 11,
        "lease": {"owner": "routine:parent", "fencing_token": 9},
    }
    child = {
        "job_id": "routine-child:publication",
        "status": "running",
        "revision": 5,
        "parent_fencing_token": 9,
        "lease": {"owner": "routine-child:routine-child:publication", "fencing_token": 3},
        "declared_authority": {"publication_operation_uuid": "operation-m3"},
    }
    m3_job = {
        "job_id": "m3-job",
        "status": "awaiting_approval",
        "declared_authority": {"approval_id": "approval-m3"},
    }

    async def get_job(job_id):
        return {
            "routine-parent": parent,
            "routine-child:publication": child,
        }.get(job_id)

    record_parent = AsyncMock(
        return_value={
            **parent,
            "revision": 12,
            "checkpoints": [
                {
                    "checkpoint_id": "routine:publication_child_recorded",
                    "payload": {"m3_job_id": "m3-job"},
                }
            ],
        }
    )
    monkeypatch.setattr(routines_module.durable_job_repository, "get_job", get_job)
    monkeypatch.setattr(routines_module.durable_job_repository, "record_checkpoint", record_parent)
    record_child = AsyncMock(return_value={**child, "revision": 6})
    monkeypatch.setattr(service, "_record_child_checkpoint", record_child)

    refreshed, prepared_child = await service._persist_adopted_publication_checkpoints(
        parent,
        child,
        publication_child_id="routine-child:publication",
        m3_job_id="m3-job",
        m3_job=m3_job,
    )

    assert refreshed["job_id"] == "routine-parent"
    assert prepared_child["revision"] == 6
    record_child.assert_awaited_once()
    assert record_child.await_args.kwargs["checkpoint_id"] == "routine-child:prepared"
    assert record_child.await_args.kwargs["payload"] == {
        "m3_job_id": "m3-job",
        "approval_id": "approval-m3",
        "status": "awaiting_approval",
        "publication_operation_uuid": "operation-m3",
        "recovery": "adopted_child_checkpoint",
    }
    assert record_parent.await_count == 1
    assert record_parent.await_args.kwargs["checkpoint_id"] == "routine:publication_child_recorded"
    assert record_parent.await_args.kwargs["checkpoint_payload"]["m3_job_id"] == "m3-job"
    assert record_parent.await_args.kwargs["checkpoint_payload"]["approval_id"] == "approval-m3"
    assert record_parent.await_args.kwargs["checkpoint_payload"]["publication_operation_uuid"] == "operation-m3"


@pytest.mark.asyncio
async def test_recover_adopts_same_approved_m3_after_parent_fence_rollover_and_readback(
    monkeypatch,
    client,
    async_db,
):
    from tests.test_github_connection_consent import active_connection
    owner, connection, consent, _request = await active_connection(client, monkeypatch)
    OWNER, SESSION = owner["principal_id"], owner["session_id"]
    service = RoutineService()
    parent_id = "routine-invocation:routine-recovery:invocation-1"
    child_id = "routine-child:publication-recovery"
    m3_id = "ghfollow_publication-recovery"
    invocation_uuid = "11111111-1111-4111-8111-111111111111"
    operation_uuid = "22222222-2222-4222-8222-222222222222"
    authority = {
        "principal": OWNER,
        "owner_kind": "user",
        "session_id": SESSION,
        "goal_owner_principal_id": OWNER,
        "goal_owner_session_id": SESSION,
        "routine_id": "routine-recovery",
        "routine_revision": 7,
        "routine_version": 2,
        "package_digest": "d" * 64,
        "invocation_uuid": invocation_uuid,
        "source_watch_id": "watch-recovery",
        "github_connection_id": connection.id,
        "github_connection_revision": connection.revision,
        "github_repository": connection.repository,
        "github_action": "create_issue",
    }
    checkpoint = {
        "checkpoint_id": "routine:publication_child_recorded",
        "payload": {
            "step_id": "github_followthrough",
            "child_job_id": child_id,
            "m3_job_id": m3_id,
            "approval_id": "approval-recovery",
            "status": "awaiting_approval",
            "publication_operation_uuid": operation_uuid,
        },
    }
    parent = {
        "job_id": parent_id,
        "job_kind": "routine_invocation",
        "status": "running",
        "owner": {"kind": "user", "principal_id": OWNER},
        "session_id": SESSION,
        "operator_session_id": SESSION,
        "goal_id": "goal-recovery",
        "goal_revision": 3,
        "revision": 12,
        "lease": {"owner": "routine:recovery", "fencing_token": 8},
        "declared_authority": authority,
        "checkpoints": [checkpoint],
    }
    initial_parent = {**parent, "status": "blocked", "lease": None}
    child = {
        "job_id": child_id,
        "job_kind": "routine_github_followthrough_child",
        "status": "blocked",
        "owner": {"kind": "user", "principal_id": OWNER},
        "session_id": SESSION,
        "operator_session_id": SESSION,
        "goal_id": "goal-recovery",
        "goal_revision": 3,
        "revision": 5,
        "parent_job_id": parent_id,
        "parent_fencing_token": 7,
        "lease": {"owner": None, "expires_at": None, "fencing_token": 3},
        "declared_authority": {
            **authority,
            "parent_job_id": parent_id,
            "parent_fencing_token": 7,
            "routine_invocation_job_id": parent_id,
            "step_id": "github_followthrough",
            "m3_job_id": m3_id,
            "publication_operation_uuid": operation_uuid,
        },
        "checkpoints": [
            {
                "checkpoint_id": "routine-child:prepared",
                "payload": {
                    "m3_job_id": m3_id,
                    "approval_id": "approval-recovery",
                    "publication_operation_uuid": operation_uuid,
                },
            }
        ],
    }
    m3_waiting = {
        "job_id": m3_id,
        "job_kind": "github_followthrough_v1",
        "status": "awaiting_approval",
        "declared_authority": {
            "approval_id": "approval-recovery",
            "action": "create_issue",
            "repository": connection.repository,
            "connection_revision": connection.revision,
            "github_consent": consent,
        },
        "effects": [],
    }
    verified_readback = {
        "job_id": m3_id,
        "job_kind": "github_followthrough_v1",
        "status": "succeeded",
        "declared_authority": {"approval_id": "approval-recovery"},
        "effects": [
            {
                "receipt_kind": "readback",
                "status": "succeeded",
                "reconciled": True,
            }
        ],
    }
    executed = False

    parent_reads = 0

    async def get_job(job_id):
        nonlocal parent_reads
        if job_id == parent_id:
            parent_reads += 1
            return initial_parent if parent_reads == 1 else parent
        if job_id == child_id:
            return child
        if job_id == m3_id:
            return verified_readback if executed else m3_waiting
        return None

    async def execute_m3(**kwargs):
        nonlocal executed
        assert kwargs == {
            "owner_principal_id": OWNER,
            "job_id": m3_id,
            "owner_session_id": SESSION,
            "external_mutation_granted": True,
        }
        executed = True
        return {"status": "succeeded", "remote_id": "123", "remote_url": "https://example.invalid/123"}

    monkeypatch.setattr(
        service,
        "_routine",
        AsyncMock(return_value=SimpleNamespace(owner_session_id=SESSION, revision=7, current_version=2)),
    )
    monkeypatch.setattr(service, "_require_active_routine", AsyncMock())
    monkeypatch.setattr(service, "_require_active_package_binding", AsyncMock())
    monkeypatch.setattr(service, "_claim_parent_for_recovery", AsyncMock(return_value=parent))
    monkeypatch.setattr(service, "_persist_adopted_publication_checkpoints", AsyncMock(return_value=(parent, {**child, "parent_fencing_token": 8})))
    monkeypatch.setattr(service, "_reacquire_parent_after_child", AsyncMock(return_value=parent))
    finalize = AsyncMock(return_value={**parent, "status": "succeeded"})
    monkeypatch.setattr(service, "_finalize_parent", finalize)
    monkeypatch.setattr(routines_module.durable_job_repository, "get_job", get_job)
    monkeypatch.setattr(routines_module.GitHubFollowthroughService, "execute", AsyncMock(side_effect=execute_m3))

    result = await service.recover(
        "routine-recovery",
        parent_id,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
        external_mutation_granted=True,
    )

    assert result["status"] == "succeeded"
    assert result["job_id"] == parent_id
    assert result["child"]["status"] == "succeeded"
    assert executed is True
    service._persist_adopted_publication_checkpoints.assert_awaited_once_with(
        parent,
        child,
        publication_child_id=child_id,
        m3_job_id=m3_id,
        m3_job=m3_waiting,
    )
    routines_module.GitHubFollowthroughService.execute.assert_awaited_once()
    finalize.assert_awaited_once()


@pytest.mark.asyncio
async def test_recover_watch_child_recorded_crash_leaves_operator_recovery_action(monkeypatch):
    service = RoutineService()
    parent_id = "routine-invocation:routine-watch-recovery:invocation-1"
    child_id = "routine-child:watch-recovery"
    m1_id = "source-watch:watch-recovery:routine-child:watch-recovery"
    authority = {
        "principal": OWNER,
        "owner_kind": "user",
        "session_id": SESSION,
        "routine_id": "routine-watch-recovery",
        "routine_revision": 7,
        "routine_version": 2,
        "package_digest": "d" * 64,
        "invocation_uuid": "11111111-1111-4111-8111-111111111111",
        "source_watch_id": "watch-recovery",
    }
    current = {
        "job_id": parent_id,
        "job_kind": "routine_invocation",
        "status": "running",
        "owner": {"kind": "user", "principal_id": OWNER},
        "session_id": SESSION,
        "operator_session_id": SESSION,
        "revision": 6,
        "lease": {"owner": "routine:watch-recovery", "fencing_token": 8},
        "declared_authority": authority,
        "checkpoints": [
            {
                "checkpoint_id": "routine:watch_child_recorded",
                "payload": {
                    "step_id": "guardian_watch_run",
                    "watch_id": "watch-recovery",
                    "child_job_id": child_id,
                },
            }
        ],
    }
    watch_child = {"job_id": child_id, "status": "running", "effects": []}

    async def get_job(job_id):
        return current if job_id == parent_id else watch_child if job_id == child_id else None

    monkeypatch.setattr(
        service,
        "_routine",
        AsyncMock(return_value=SimpleNamespace(owner_session_id=SESSION, revision=7, current_version=2)),
    )
    monkeypatch.setattr(service, "_require_active_routine", AsyncMock())
    monkeypatch.setattr(service, "_require_active_package_binding", AsyncMock())
    monkeypatch.setattr(service, "_claim_parent_for_recovery", AsyncMock(return_value=current))
    cancel = AsyncMock(
        return_value={
            "status": "blocked",
            "reason_code": "routine_watch_outcome_checkpoint_missing",
            "operator_action": "recover_or_cancel",
        }
    )
    monkeypatch.setattr(service, "_cancel_stale_child", cancel)
    monkeypatch.setattr(routines_module.durable_job_repository, "get_job", get_job)

    result = await service.recover(
        "routine-watch-recovery",
        parent_id,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )

    assert result["status"] == "blocked"
    assert result["child_job_id"] == child_id
    assert result["m1_job_id"] == m1_id
    assert result["operator_action"] == "recover_or_cancel"
    assert result["learning"] == "no_learning"
    cancel.assert_awaited_once_with(
        watch_child,
        parent=current,
        step_id="guardian_watch_run",
        reason_code="routine_watch_outcome_checkpoint_missing",
        operator_action="recover_or_cancel",
    )
