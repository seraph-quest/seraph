"""Authenticated same-card recovery for governed routine publication."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.api import work_board as work_board_api
from src.auth.service import AuthenticatedOperator
from src.db.models import WorkBoardStatus
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal
from src.work_board.contracts import (
    WorkBoardRoutinePublicationPrepareRequest,
    WorkBoardRoutinePublicationRecoverRequest,
)
from src.work_board.repository import BoardRevisionConflict


def _operator() -> AuthenticatedOperator:
    now = datetime.now(timezone.utc)
    session_id = "session:publication"
    return AuthenticatedOperator(
        session_id=session_id,
        principal=TrustPrincipal(
            principal_id="operator:publication",
            principal_type=PrincipalType.OPERATOR,
            authenticated=True,
            grants=(AuthorityGrant.CAPABILITY_EXECUTE, AuthorityGrant.EXTERNAL_MUTATION),
            session_id=session_id,
            operator_session_id=session_id,
        ),
        idle_expires_at=now,
        absolute_expires_at=now,
    )


def _request(operator: AuthenticatedOperator):
    return SimpleNamespace(state=SimpleNamespace(operator=operator))


def _context(*, approval_status=None, m3_job_id=None, failure_reason="awaiting_publication_preview"):
    task = SimpleNamespace(
        task_id="task-publication",
        task_revision=7,
        status=WorkBoardStatus.blocked,
        capability_id="guardian-routine.v1",
        block_reason=failure_reason,
    )
    attempt = SimpleNamespace(
        attempt_id="attempt-publication",
        workflow_run_id="routine-parent",
        ended_at=None,
        lease_owner=None,
        lease_expires_at=None,
    )
    return {
        "task": task,
        "attempt": attempt,
        "detail": {"task": task, "attempts": [attempt], "dependency_counts": {}},
        "parent": {
            "job_id": "routine-parent",
            "status": "blocked",
            "failure_reason": failure_reason,
            "lease": {"fencing_token": 1},
        },
        "routine_id": "routine-1",
        "routine_version": 2,
        "routine_revision": 5,
        "source_watch_id": "watch-1",
        "source_watch_revision": 3,
        "parent_workflow_run_id": "routine-parent",
        "m3_job_id": m3_job_id,
        "m3_status": "awaiting_approval" if m3_job_id else None,
        "approval_id": "approval-1" if m3_job_id else None,
        "approval_status": approval_status,
        "preview": {"title": "Exact title", "body": "Exact body"} if m3_job_id else None,
        "publication_response": None,
    }


@pytest.mark.asyncio
async def test_publication_prepare_approval_and_resume_reconcile_same_card(monkeypatch):
    operator = _operator()
    request = _request(operator)
    contexts = iter(
        (
            _context(),  # read preview boundary
            _context(),  # prepare input
            _context(m3_job_id="m3-job", approval_status="pending", failure_reason="awaiting_publication_approval"),
            _context(m3_job_id="m3-job", approval_status="pending", failure_reason="awaiting_publication_approval"),
            _context(m3_job_id="m3-job", approval_status="approved", failure_reason="awaiting_publication_approval"),
        )
    )

    async def context(*_args, **_kwargs):
        return next(contexts)

    @asynccontextmanager
    async def fake_session():
        yield SimpleNamespace()

    monkeypatch.setattr(work_board_api, "get_session", fake_session)
    monkeypatch.setattr(work_board_api, "_routine_publication_context", context)
    monkeypatch.setattr(
        work_board_api,
        "_safe_task_payload",
        AsyncMock(return_value={"task_id": "task-publication", "task_revision": 7, "status": "blocked"}),
    )
    prepare = AsyncMock(return_value={"status": "awaiting_approval", "job_id": "m3-job"})
    recover = AsyncMock(return_value={"status": "succeeded", "child": {"status": "succeeded"}})
    reconcile = AsyncMock(return_value=["routine-parent"])
    resume_attempt = AsyncMock()
    monkeypatch.setattr(work_board_api.routine_service, "prepare_publication", prepare)
    monkeypatch.setattr(work_board_api.routine_service, "recover", recover)
    monkeypatch.setattr(work_board_api._dispatcher, "reconcile_linked_attempts", reconcile)
    monkeypatch.setattr(work_board_api._dispatcher, "resume_routine_attempt_for_operator_recovery", resume_attempt)
    monkeypatch.setattr(
        work_board_api.repository,
        "get_detail",
        AsyncMock(return_value={"task": _context(m3_job_id="m3-job", approval_status="approved")["task"], "attempts": [_context(m3_job_id="m3-job", approval_status="approved")["attempt"]], "dependency_counts": {}}),
    )

    preview = await work_board_api.get_work_board_routine_publication(request, "task-publication")
    assert preview["publication"]["recovery_action"] == "prepare_routine_publication"

    prepared = await work_board_api.prepare_work_board_routine_publication(
        request,
        "task-publication",
        WorkBoardRoutinePublicationPrepareRequest(
            expected_revision=7,
            title="Exact title",
            body="Exact body",
        ),
    )
    prepare.assert_awaited_once_with(
        "routine-1",
        "routine-parent",
        work_board_api.RoutinePublicationRequest(title="Exact title", body="Exact body"),
        owner_principal_id="operator:publication",
        owner_session_id="session:publication",
        external_mutation_granted=True,
    )
    assert prepared["approval_required"] is True
    assert prepared["operator_action"] == "approve_exact_publication_in_pending_approvals"
    resume_attempt.assert_awaited_once()

    with pytest.raises(HTTPException) as pending:
        await work_board_api.recover_work_board_routine_publication(
            request,
            "task-publication",
            WorkBoardRoutinePublicationRecoverRequest(expected_revision=7),
        )
    assert pending.value.status_code == 409
    assert pending.value.detail["code"] == "approval_not_current"
    recover.assert_not_awaited()

    resumed = await work_board_api.recover_work_board_routine_publication(
        request,
        "task-publication",
        WorkBoardRoutinePublicationRecoverRequest(expected_revision=7),
    )
    recover.assert_awaited_once_with(
        "routine-1",
        "routine-parent",
        owner_principal_id="operator:publication",
        owner_session_id="session:publication",
        external_mutation_granted=True,
    )
    assert reconcile.await_count == 2
    assert resumed["readback_required"] is True
    assert resumed["task"]["task_id"] == "task-publication"


@pytest.mark.asyncio
async def test_publication_rejects_stale_revision_and_caller_selected_bindings(monkeypatch):
    operator = _operator()
    request = _request(operator)

    async def stale_context(*_args, **_kwargs):
        raise BoardRevisionConflict("task-publication", 6, 7)

    @asynccontextmanager
    async def fake_session():
        yield SimpleNamespace()

    monkeypatch.setattr(work_board_api, "get_session", fake_session)
    monkeypatch.setattr(work_board_api, "_routine_publication_context", stale_context)

    with pytest.raises(HTTPException) as stale:
        await work_board_api.prepare_work_board_routine_publication(
            request,
            "task-publication",
            WorkBoardRoutinePublicationPrepareRequest(
                expected_revision=6,
                title="Exact title",
                body="Exact body",
            ),
        )
    assert stale.value.status_code == 409
    assert stale.value.detail["code"] == "stale_revision"

    with pytest.raises(ValidationError):
        WorkBoardRoutinePublicationPrepareRequest(
            expected_revision=7,
            title="Exact title",
            body="Exact body",
            attempt_id="caller-selected-attempt",
            workflow_run_id="caller-selected-run",
            approval_id="caller-selected-approval",
        )


@pytest.mark.asyncio
async def test_publication_context_rejects_cross_owner_parent(monkeypatch):
    operator = _operator()
    task = SimpleNamespace(
        task_id="task-publication",
        task_revision=7,
        status=WorkBoardStatus.running,
        capability_id="guardian-routine.v1",
        goal_id="goal-1",
        goal_revision=2,
    )
    attempt = SimpleNamespace(
        attempt_id="attempt-publication",
        workflow_run_id="routine-parent",
        fencing_token=9,
        ended_at=None,
    )
    detail = {"task": task, "attempts": [attempt]}
    monkeypatch.setattr(work_board_api.repository, "get_detail", AsyncMock(return_value=detail))
    monkeypatch.setattr(
        work_board_api,
        "_parse_typed_input",
        lambda _task: {
            "routine_id": "routine-1",
            "version": 2,
            "expected_routine_revision": 5,
            "source_watch_id": "watch-1",
            "expected_watch_revision": 3,
            "goal_id": "goal-1",
            "expected_goal_revision": 2,
        },
    )
    monkeypatch.setattr(
        work_board_api.durable_job_repository,
        "get_job",
        AsyncMock(
            return_value={
                "job_id": "routine-parent",
                "job_kind": "routine_invocation",
                "owner": {"kind": "user", "principal_id": "operator:other"},
                "session_id": "other-session",
                "goal_id": "goal-1",
                "goal_revision": 2,
                "declared_authority": {},
                "lease": {"fencing_token": 9},
            }
        ),
    )

    with pytest.raises(work_board_api.BoardError) as denied:
        await work_board_api._routine_publication_context(
            SimpleNamespace(),
            operator,
            "task-publication",
        )
    assert denied.value.code == "routine_publication_binding_mismatch"
