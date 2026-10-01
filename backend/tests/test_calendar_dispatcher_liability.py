"""Dispatcher-level Calendar recovery and terminal-proof boundaries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from src.integrations.google_calendar import CalendarIntegrationError
from src.work_board.dispatcher import BoardDispatchClaim, WorkBoardDispatcher
from src.workflows.job_runtime import (
    DurableJobIdentity,
    DurableJobRepository,
    DurableJobSpec,
)


class _SessionContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


def _task() -> SimpleNamespace:
    return SimpleNamespace(
        task_id="task-calendar-liability",
        task_revision=4,
        owner_principal_id="operator:calendar",
        owner_session_id="session:calendar",
        goal_id="goal-calendar-liability",
        goal_revision=1,
        capability_id="calendar.meeting-prep.v1",
        requires_review=False,
        input_artifact_id="artifact-calendar-input",
        typed_input_digest="sha256:" + "a" * 64,
        executor_id="executor.calendar.meeting-prep.v1",
        priority=50,
    )


def _attempt() -> SimpleNamespace:
    return SimpleNamespace(
        attempt_id="attempt-calendar-liability",
        task_id="task-calendar-liability",
        fencing_token=7,
        lease_owner="service:work-board",
        workflow_run_id=None,
        cancel_requested_at=None,
    )


class _Repository:
    def __init__(self, task: SimpleNamespace, attempt: SimpleNamespace) -> None:
        self.task = task
        self.attempt = attempt

    async def link_attempt_workflow_run(self, _db, *_args, **_kwargs):
        linked_task = SimpleNamespace(**vars(self.task))
        linked_task.task_revision += 1
        linked_attempt = SimpleNamespace(**vars(self.attempt))
        linked_attempt.workflow_run_id = "calendar-liability-root"
        return SimpleNamespace(task=linked_task, attempt=linked_attempt)


class _Jobs:
    def __init__(self, *, successful_projection: bool = False) -> None:
        self.projection: dict[str, Any] = {
            "job_id": "calendar-liability-root",
            "run_identity": "calendar-liability-root",
            "root_run_identity": "calendar-liability-root",
            "status": "accepted",
            "revision": 1,
            "effects": [],
        }
        self.successful_projection = successful_projection
        self.retry_calls = 0

    async def get_job(self, _job_id: str):
        return dict(self.projection)

    async def queue_job(self, _job_id: str, **_kwargs):
        self.projection.update(status="queued", revision=self.projection["revision"] + 1)
        return dict(self.projection)

    async def claim_job(self, _job_id: str, *, owner: str, **_kwargs):
        self.projection.update(
            status="running",
            revision=self.projection["revision"] + 1,
            lease={"owner": owner, "fencing_token": 11},
        )
        return dict(self.projection)


async def _run_direct(dispatcher: WorkBoardDispatcher, task, attempt, jobs, *, execute):
    claim = BoardDispatchClaim(task, attempt, SimpleNamespace(event_id=1))
    dispatcher._canonical_direct_admission = execute["admission"]
    dispatcher._execute_direct_adapter = execute["execution"]
    return await dispatcher._admit_execute_direct(claim, {}, runtime_seconds=120)


@pytest.mark.asyncio
async def test_calendar_timeout_or_unknown_effect_never_retries_or_frees_board_claim(monkeypatch):
    task = _task()
    attempt = _attempt()
    jobs = _Jobs()
    repository = _Repository(task, attempt)
    dispatcher = WorkBoardDispatcher(
        repository=repository,
        jobs=jobs,
        session_provider=lambda: _SessionContext(),
    )
    projected: list[dict[str, Any]] = []

    async def admission(*_args, **_kwargs):
        return (
            {"status": "accepted", "job_id": "calendar-liability-root", "admission_only": True},
            jobs.projection,
            {"job_id": "calendar-liability-root"},
        )

    async def execution(*_args, **_kwargs):
        raise CalendarIntegrationError(
            "calendar_reconciliation_required",
            "The model call timed out and may still be in flight",
            status_code=504,
            recovery_action="reconcile_external_effect",
        )

    async def refresh(claim):
        return claim

    async def project(*_args, **kwargs):
        projected.append(kwargs)

    dispatcher._canonical_direct_admission = admission
    dispatcher._execute_direct_adapter = execution
    dispatcher._refresh_claim = refresh
    dispatcher._project = project

    result = await dispatcher._admit_execute_direct(
        BoardDispatchClaim(task, attempt, SimpleNamespace(event_id=1)),
        {},
        runtime_seconds=120,
    )

    assert result == {"admitted": True, "completed": False, "blocked": True}
    assert projected and projected[-1]["block_kind"] == "unknown_effect"
    assert projected[-1]["block_reason"] == "calendar_reconciliation_required"
    assert jobs.projection["status"] == "running"
    assert jobs.retry_calls == 0


@pytest.mark.asyncio
async def test_calendar_partial_success_receipt_cannot_project_board_done():
    task = _task()
    attempt = _attempt()
    jobs = _Jobs(successful_projection=True)
    repository = _Repository(task, attempt)
    dispatcher = WorkBoardDispatcher(
        repository=repository,
        jobs=jobs,
        session_provider=lambda: _SessionContext(),
    )
    projected: list[dict[str, Any]] = []

    async def admission(*_args, **_kwargs):
        return (
            {"status": "accepted", "job_id": "calendar-liability-root", "admission_only": True},
            jobs.projection,
            {"job_id": "calendar-liability-root"},
        )

    async def execution(*_args, **_kwargs):
        jobs.projection.update(status="succeeded", revision=jobs.projection["revision"] + 1)
        return {"status": "succeeded", "job_id": "calendar-liability-root"}

    async def project(*_args, **kwargs):
        projected.append(kwargs)

    dispatcher._canonical_direct_admission = admission
    dispatcher._execute_direct_adapter = execution
    dispatcher._project = project

    result = await dispatcher._admit_execute_direct(
        BoardDispatchClaim(task, attempt, SimpleNamespace(event_id=1)),
        {},
        runtime_seconds=120,
    )

    assert result["admitted"] is True
    assert result["completed"] is False
    assert projected[-1]["status"].value == "blocked"
    assert projected[-1]["block_reason"] == "execution_blocked"


@pytest.mark.asyncio
async def test_calendar_terminal_authority_revoke_blocks_root_success_cas(async_db):
    jobs = DurableJobRepository()
    job_id = "calendar-terminal-authority-revoke"
    owner = "operator:calendar"
    session = "session:calendar"
    spec = DurableJobSpec(
        identity=DurableJobIdentity(
            job_id=job_id,
            owner_kind="user",
            owner_principal_id=owner,
            job_kind="calendar.meeting-prep.v1",
            capability_version="1",
            idempotency_scope="work-board-attempt",
            idempotency_key="task-calendar-liability:attempt-calendar-liability",
        ),
        inputs={"input": {"event_binding_id": "binding"}, "parent_handoff": {}},
        session_id=session,
        operator_session_id=session,
        declared_authority={
            "principal": owner,
            "owner_kind": "user",
            "service_id": None,
            "session_id": session,
        },
        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        max_attempts=1,
        resource_claims=("remote-inference",),
    )
    admitted = await jobs.admit_job(spec)
    queued = await jobs.queue_job(job_id, expected_revision=admitted["revision"])
    claimed = await jobs.claim_job(
        job_id,
        owner="service:work-board",
        lease_seconds=120,
        expected_state="queued",
        expected_revision=queued["revision"],
        expected_fencing_token=queued["lease"]["fencing_token"],
    )
    readback = await jobs.record_readback(
        job_id,
        target_path="artifacts/work-board/calendar/result.json",
        status="succeeded",
        content_sha256="a" * 64,
        details={"verified": True, "memory_status": "no_learning"},
        owner="service:work-board",
        fencing_token=claimed["lease"]["fencing_token"],
        expected_revision=claimed["revision"],
    )

    async def revoked(_db, run):
        assert run.status == "running"
        raise CalendarIntegrationError(
            "calendar_reconciliation_required",
            "Calendar consent was revoked before terminal settlement",
            recovery_action="reconcile_external_effect",
        )

    with pytest.raises(CalendarIntegrationError) as error:
        await jobs.transition_job(
            job_id,
            "succeeded",
            owner="service:work-board",
            fencing_token=claimed["lease"]["fencing_token"],
            expected_revision=readback["revision"],
            terminal_authority_check=revoked,
        )
    assert error.value.code == "calendar_reconciliation_required"
    projection = await jobs.get_job(job_id)
    assert projection["status"] == "running"
