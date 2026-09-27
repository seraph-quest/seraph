"""Provider-free proofs for bound worker-only board controls."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from src.native_tools.loader import reload_tools
from src.tools.work_board_tools import (
    WorkBoardWorkerHost,
    bind_work_board_worker_context,
    get_bound_work_board_tools,
    work_board_show,
)
from src.db.models import WorkBoardAttempt, WorkBoardStatus, WorkBoardTask
from src.work_board.repository import WorkBoardRepository
from src.work_board.tools import WorkBoardWorkerBlock, WorkBoardWorkerEvidence, WorkBoardWorkerRequest, WorkBoardWorkerTools
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal


def _future_lease() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=5)


def _future_lease_iso() -> str:
    return _future_lease().isoformat()


def _request() -> WorkBoardWorkerRequest:
    return WorkBoardWorkerRequest(
        task_id="task-1",
        attempt_id="attempt-1",
        expected_task_revision=3,
        board_fencing_token=2,
        workflow_run_id="work-board:task-1:attempt-1",
        workflow_fencing_token=4,
    )


def test_general_native_discovery_does_not_expose_board_worker_controls():
    assert not any(tool.name.startswith("work_board_") for tool in reload_tools())


def test_worker_controls_fail_closed_without_server_trust_binding():
    with pytest.raises(PermissionError):
        work_board_show("task-1")


def test_worker_tool_requests_use_closed_block_and_evidence_schemas():
    request = _request()
    assert WorkBoardWorkerBlock(**request.model_dump(), block_kind="unknown_effect").block_kind == "unknown_effect"
    with pytest.raises(ValidationError):
        WorkBoardWorkerBlock(**request.model_dump(), block_kind="arbitrary_private_reason")
    with pytest.raises(ValidationError):
        WorkBoardWorkerEvidence(**request.model_dump(), evidence_refs=["/private/source"])
    with pytest.raises(ValidationError):
        WorkBoardWorkerEvidence(**request.model_dump(), evidence_refs=[f"artifact:{i}" for i in range(21)])


def test_bound_worker_host_exposes_only_the_six_scoped_controls():
    host = WorkBoardWorkerHost(_request())
    assert [tool.name for tool in host.tools] == [
        "work_board_show",
        "work_board_heartbeat",
        "work_board_comment",
        "work_board_block",
        "work_board_request_review",
        "work_board_request_completion",
    ]
    assert all("create" not in tool.name and "link" not in tool.name and "unblock" not in tool.name for tool in host.tools)


def test_bound_worker_context_is_the_only_runtime_injection_path():
    assert get_bound_work_board_tools() == []
    with bind_work_board_worker_context(_request()):
        assert [tool.name for tool in get_bound_work_board_tools()] == [
            "work_board_show",
            "work_board_heartbeat",
            "work_board_comment",
            "work_board_block",
            "work_board_request_review",
            "work_board_request_completion",
        ]
    assert get_bound_work_board_tools() == []


@pytest.mark.asyncio
async def test_native_tools_accept_only_the_canonical_goal_snapshot_child(monkeypatch):
    task = WorkBoardTask(
        task_id="task-native-lineage",
        owner_principal_id="operator:native",
        owner_session_id="native-session",
        goal_id="goal-native",
        goal_revision=3,
        capability_id="workflow.goal-snapshot-to-file",
        executor_id="executor-native",
        status=WorkBoardStatus.running,
        task_revision=8,
    )
    attempt = WorkBoardAttempt(
        task_id=task.task_id,
        attempt_id="attempt-native-lineage",
        workflow_run_id="work-board:task-native-lineage:attempt-native-lineage",
        lease_owner="service:work-board",
        lease_expires_at=_future_lease(),
        fencing_token=2,
        executor_id=task.executor_id,
    )
    root_id = attempt.workflow_run_id
    child_id = f"goal-snapshot-work-board:{task.task_id}:{attempt.attempt_id}"
    nested_id = "goal-snapshot-to-file:child-run"
    root = {
        "job_id": root_id,
        "run_identity": root_id,
        "root_run_identity": root_id,
        "job_kind": task.capability_id,
        "owner": {
            "kind": "service",
            "principal_id": "service:work-board",
            "service_id": "service:work-board",
        },
        "session_id": task.owner_session_id,
        "goal_id": task.goal_id,
        "goal_revision": task.goal_revision,
        "status": "running",
        "lease": {
            "owner": "service:work-board:attempt-native-lineage",
            "fencing_token": 4,
            "expires_at": _future_lease_iso(),
        },
    }
    child = {
        "job_id": child_id,
        "run_identity": child_id,
        "root_run_identity": root_id,
        "parent_run_identity": root_id,
        "parent_job_id": root_id,
        "parent_fencing_token": 4,
        "job_kind": "workflow.goal-snapshot-to-file",
        "capability_version": "1",
        "owner": {
            "kind": "service",
            "principal_id": "service:goal-snapshot",
            "service_id": "service:goal-snapshot",
        },
        "session_id": task.owner_session_id,
        "goal_id": task.goal_id,
        "goal_revision": task.goal_revision,
        "status": "running",
        "declared_authority": {
            "principal": "service:goal-snapshot",
            "owner_principal_id": "service:goal-snapshot",
            "service_id": "service:goal-snapshot",
            "session_id": task.owner_session_id,
            "goal_revision": task.goal_revision,
        },
        "lease": {
            "owner": "service:goal-snapshot:child",
            "fencing_token": 7,
            "expires_at": _future_lease_iso(),
        },
    }
    nested = {
        "job_id": nested_id,
        "run_identity": nested_id,
        "root_run_identity": root_id,
        "parent_run_identity": child_id,
        "parent_job_id": child_id,
        "parent_fencing_token": 7,
        "job_kind": "goal-snapshot-to-file",
        "capability_version": "workflow-v2",
        "owner": {
            "kind": "service",
            "principal_id": "service:goal-snapshot",
            "service_id": "service:goal-snapshot",
        },
        "session_id": task.owner_session_id,
        "goal_id": task.goal_id,
        "goal_revision": task.goal_revision,
        "status": "running",
        "declared_authority": {
            "principal": "service:goal-snapshot",
            "owner_kind": "service",
            "service_id": "service:goal-snapshot",
            "session_id": task.owner_session_id,
            "capability": "workflow_goal_snapshot_to_file",
        },
        "lease": {
            "owner": "goal-snapshot-step",
            "fencing_token": 8,
            "expires_at": _future_lease_iso(),
        },
    }

    class _Jobs:
        def __init__(self):
            self.rows = {root_id: root, child_id: child, nested_id: nested}

        async def get_job(self, job_id):
            return self.rows.get(job_id)

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    worker = WorkBoardWorkerTools(jobs=_Jobs(), session_provider=lambda: _Session())

    async def fake_bound(_db, _request):
        return SimpleNamespace(principal_id=task.owner_principal_id, session_id=task.owner_session_id), task, attempt, root

    worker._bound = fake_bound
    request = WorkBoardWorkerRequest(
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        expected_task_revision=task.task_revision,
        board_fencing_token=attempt.fencing_token,
        workflow_run_id=root_id,
        workflow_fencing_token=4,
    )
    principal = TrustPrincipal(
        principal_id="service:goal-snapshot",
        principal_type=PrincipalType.SERVICE,
        authenticated=True,
        grants=(AuthorityGrant.CAPABILITY_EXECUTE,),
        session_id=task.owner_session_id,
        job_id=nested_id,
    )

    await worker.validate_native_principal(request, principal)

    foreign_session_principal = TrustPrincipal(
        principal_id=principal.principal_id,
        principal_type=principal.principal_type,
        authenticated=True,
        grants=principal.grants,
        session_id="foreign-session",
        job_id=nested_id,
    )
    with pytest.raises(PermissionError, match="session"):
        await worker.validate_native_principal(request, foreign_session_principal)

    worker.jobs.rows[child_id]["goal_revision"] = "invalid"
    with pytest.raises(PermissionError):
        await worker.validate_native_principal(request, principal)
    worker.jobs.rows[child_id]["goal_revision"] = task.goal_revision
    worker.jobs.rows[nested_id]["parent_fencing_token"] = "invalid"
    with pytest.raises(PermissionError):
        await worker.validate_native_principal(request, principal)
    worker.jobs.rows[nested_id]["parent_fencing_token"] = 7

    worker.jobs.rows[nested_id]["parent_run_identity"] = "foreign-descendant"
    with pytest.raises(PermissionError, match="delegated"):
        await worker.validate_native_principal(request, principal)


@pytest.mark.asyncio
async def test_worker_block_reconciles_authoritative_workflow_before_projection(async_db):
    task_id = "task-worker-block-reconcile"
    attempt_id = "attempt-worker-block-reconcile"
    run_id = "work-board:task-worker-block-reconcile:attempt-worker-block-reconcile"
    async with async_db() as db:
        db.add(
            WorkBoardTask(
                task_id=task_id,
                owner_principal_id="operator:worker",
                owner_session_id="worker-session",
                goal_id="goal-worker",
                goal_revision=1,
                title="Worker block",
                idempotency_key=task_id,
                capability_id="workflow.goal-snapshot-to-file",
                executor_id="executor-worker",
                status=WorkBoardStatus.running,
                task_revision=4,
            )
        )
        await db.flush()
        db.add(
            WorkBoardAttempt(
                attempt_id=attempt_id,
                task_id=task_id,
                workflow_run_id=run_id,
                task_revision_at_claim=3,
                lease_owner="executor-worker",
                lease_expires_at=_future_lease(),
                fencing_token=7,
                executor_id="executor-worker",
            )
        )
        await db.commit()

    class _Jobs:
        def __init__(self):
            self.transitions = []

        async def get_job(self, _job_id):
            return {
                "job_id": run_id,
                "status": "running",
                "revision": 9,
                "lease": {
                    "owner": "workflow-worker",
                    "fencing_token": 11,
                    "expires_at": _future_lease_iso(),
                },
                "effects": [],
            }

        async def transition_job(self, job_id, status, **kwargs):
            self.transitions.append((job_id, status, kwargs))
            return {
                "job_id": job_id,
                "status": "blocked",
                "revision": 10,
                "lease": {
                    "owner": "workflow-worker",
                    "fencing_token": 11,
                    "expires_at": _future_lease_iso(),
                },
                "effects": [],
            }

    jobs = _Jobs()
    worker = WorkBoardWorkerTools(
        repository=WorkBoardRepository(),
        jobs=jobs,
        session_provider=async_db,
    )
    projection = await worker.block(
        WorkBoardWorkerBlock(
            **WorkBoardWorkerRequest(
                task_id=task_id,
                attempt_id=attempt_id,
                expected_task_revision=4,
                board_fencing_token=7,
                workflow_run_id=run_id,
                workflow_fencing_token=11,
            ).model_dump(),
            block_kind="transient",
        )
    )

    assert jobs.transitions and jobs.transitions[0][1] == "blocked"
    assert projection.task.status is WorkBoardStatus.blocked
    async with async_db() as db:
        stored_attempt = (
            await db.execute(
                select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == attempt_id)
            )
        ).scalar_one()
    assert stored_attempt.ended_at is not None


@pytest.mark.asyncio
async def test_worker_block_does_not_project_when_workflow_reconciliation_fails(async_db):
    task_id = "task-worker-block-fails"
    attempt_id = "attempt-worker-block-fails"
    run_id = "work-board:task-worker-block-fails:attempt-worker-block-fails"
    async with async_db() as db:
        db.add(
            WorkBoardTask(
                task_id=task_id,
                owner_principal_id="operator:worker",
                owner_session_id="worker-session",
                goal_id="goal-worker-fail",
                goal_revision=1,
                title="Worker block failure",
                idempotency_key=task_id,
                capability_id="workflow.goal-snapshot-to-file",
                executor_id="executor-worker",
                status=WorkBoardStatus.running,
                task_revision=4,
            )
        )
        await db.flush()
        db.add(
            WorkBoardAttempt(
                attempt_id=attempt_id,
                task_id=task_id,
                workflow_run_id=run_id,
                task_revision_at_claim=3,
                lease_owner="executor-worker",
                lease_expires_at=_future_lease(),
                fencing_token=7,
                executor_id="executor-worker",
            )
        )
        await db.commit()

    class _FailingJobs:
        async def get_job(self, _job_id):
            return {
                "job_id": run_id,
                "status": "running",
                "revision": 9,
                "lease": {
                    "owner": "workflow-worker",
                    "fencing_token": 11,
                    "expires_at": _future_lease_iso(),
                },
                "effects": [],
            }

        async def transition_job(self, *_args, **_kwargs):
            raise RuntimeError("durable runtime unavailable")

    worker = WorkBoardWorkerTools(
        repository=WorkBoardRepository(),
        jobs=_FailingJobs(),
        session_provider=async_db,
    )
    with pytest.raises(Exception) as exc_info:
        await worker.block(
            WorkBoardWorkerBlock(
                **WorkBoardWorkerRequest(
                    task_id=task_id,
                    attempt_id=attempt_id,
                    expected_task_revision=4,
                    board_fencing_token=7,
                    workflow_run_id=run_id,
                    workflow_fencing_token=11,
                ).model_dump(),
                block_kind="transient",
            )
        )
    assert getattr(exc_info.value, "code", None) == "workflow_reconcile_required"
    async with async_db() as db:
        task = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
        ).scalar_one()
    assert task.status is WorkBoardStatus.running
