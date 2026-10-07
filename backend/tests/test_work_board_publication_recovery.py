"""Same-card, operator-controlled recovery for routine publication previews."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.api import work_board as api
from src.db.models import WorkBoardAttempt, WorkBoardStatus, WorkBoardTask
from src.work_board.contracts import (
    WorkBoardRoutinePublicationPrepareRequest,
    WorkBoardRoutinePublicationRecoverRequest,
)
from src.workflows.routines import RoutinePublicationRequest
from tests.test_github_connection_consent import active_connection


OWNER = "operator:m6-publication"
SESSION = "session:m6-publication"
TASK_ID = "m6-routine-task"
ATTEMPT_ID = "m6-routine-attempt"
PARENT_RUN_ID = "routine-invocation:m6"


def _operator(*, grants: tuple[str, ...] = ("external_mutation",)):
    return SimpleNamespace(
        principal=SimpleNamespace(principal_id=OWNER, grants=grants),
        session_id=SESSION,
    )


def _context(
    *,
    m3_job_id: str | None = None,
    approval_id: str | None = None,
    approval_status: str | None = None,
    preview: dict[str, str] | None = None,
):
    task = WorkBoardTask(
        task_id=TASK_ID,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
        goal_id="goal-m6-publication",
        goal_revision=3,
        title="Run reviewed procedure",
        idempotency_key="m6-routine-task-key",
        capability_id="guardian-routine.v1",
        typed_input_ref="workspace-json:artifacts/work-board/routine.json",
        typed_input_digest="a" * 64,
        status=WorkBoardStatus.blocked,
        task_revision=7,
        block_reason="awaiting_publication_preview",
    )
    attempt = WorkBoardAttempt(
        attempt_id=ATTEMPT_ID,
        task_id=TASK_ID,
        workflow_run_id=PARENT_RUN_ID,
        task_revision_at_claim=6,
        lease_owner=None,
        lease_expires_at=None,
        heartbeat_at=datetime.now(timezone.utc),
        fencing_token=4,
        executor_id="seraph-work-board:guardian-routine.v1",
        started_at=datetime.now(timezone.utc),
    )
    detail = {"task": task, "attempts": [attempt], "dependency_counts": (0, 0)}
    return {
        "task": task,
        "attempt": attempt,
        "detail": detail,
        "parent": {
            "job_id": PARENT_RUN_ID,
            "status": "blocked",
            "failure_reason": "awaiting_publication_preview",
            "lease": {"fencing_token": 4},
        },
        "routine_id": "routine-m6",
        "routine_version": 2,
        "routine_revision": 5,
        "source_watch_id": "watch-m6",
        "source_watch_revision": 9,
        "parent_workflow_run_id": PARENT_RUN_ID,
        "m3_job_id": m3_job_id,
        "m3_status": "running" if m3_job_id else None,
        "approval_id": approval_id,
        "approval_status": approval_status,
        "preview": preview,
        "publication_response": None,
        "parent_effects": [],
        "publication_effects": [],
    }


@asynccontextmanager
async def _empty_db_session():
    yield object()


async def _bind_publication_consent(client, monkeypatch):
    owner, connection, _binding, _request = await active_connection(client, monkeypatch)
    monkeypatch.setitem(globals(), "OWNER", owner["principal_id"])
    monkeypatch.setitem(globals(), "SESSION", owner["session_id"])
    return {
        "github_action": "create_issue",
        "github_repository": connection.repository,
        "github_connection_revision": connection.revision,
    }


def _patch_route_context(monkeypatch, *contexts, authority):
    for context in contexts:
        context["parent"]["declared_authority"] = authority
    monkeypatch.setattr(api, "_operator", lambda _request: _operator())
    monkeypatch.setattr(api, "get_session", _empty_db_session)
    # These route-contract tests must not consult the live workspace vault;
    # secret redaction is covered separately against its managed store.
    monkeypatch.setattr(
        api.vault_redaction,
        "redact_secrets_in_text",
        AsyncMock(side_effect=lambda value, **_kwargs: value),
    )
    monkeypatch.setattr(
        api.vault_redaction,
        "redact_secrets_in_text_readonly",
        AsyncMock(side_effect=lambda _db, value, **_kwargs: value),
    )
    monkeypatch.setattr(
        api,
        "_routine_publication_context",
        AsyncMock(side_effect=list(contexts)),
    )


@pytest.mark.asyncio
async def test_prepare_publication_uses_same_card_binding_and_never_approves(client, async_db, monkeypatch):
    authority = await _bind_publication_consent(client, monkeypatch)
    initial = _context()
    prepared = _context(
        m3_job_id="github-followthrough:m6",
        approval_id="approval-m6",
        approval_status="pending",
        preview={"title": "Updated source", "body": "Verified operator supplied summary"},
    )
    _patch_route_context(monkeypatch, initial, prepared, authority=authority)
    prepare = AsyncMock(return_value={"status": "awaiting_approval"})
    monkeypatch.setattr(api.routine_service, "prepare_publication", prepare)
    resume = AsyncMock()
    monkeypatch.setattr(api.dispatcher, "resume_routine_attempt_for_operator_recovery", resume)
    monkeypatch.setattr(api.dispatcher, "reconcile_linked_attempts", AsyncMock())
    body = WorkBoardRoutinePublicationPrepareRequest(
        expected_revision=7,
        title="Updated source",
        body="Verified operator supplied summary",
    )

    response = await api.prepare_work_board_routine_publication(
        request=None,
        task_id=TASK_ID,
        body=body,
    )

    assert response["approval_required"] is True
    assert response["operator_action"] == "approve_exact_publication_in_pending_approvals"
    assert response["publication"]["parent_workflow_run_id"] == PARENT_RUN_ID
    assert response["publication"]["m3_job_id"] == "github-followthrough:m6"
    resume.assert_awaited_once_with(
        api._owner(_operator()),
        initial["task"],
        initial["attempt"],
        initial["parent"],
        expected_revision=7,
    )
    assert prepare.await_args.args == (
        "routine-m6",
        PARENT_RUN_ID,
        RoutinePublicationRequest(
            title="Updated source",
            body="Verified operator supplied summary",
        ),
    )
    assert prepare.await_args.kwargs == {
        "owner_principal_id": OWNER,
        "owner_session_id": SESSION,
        "external_mutation_granted": True,
    }


@pytest.mark.asyncio
async def test_prepare_publication_requires_current_external_mutation_grant(monkeypatch):
    context = _context()
    context_lookup = AsyncMock(return_value=context)
    monkeypatch.setattr(api, "_operator", lambda _request: _operator(grants=()))
    monkeypatch.setattr(api, "_routine_publication_context", context_lookup)
    block = AsyncMock(return_value=True)
    monkeypatch.setattr(api, "_block_routine_for_missing_external_authority", block)
    prepare = AsyncMock()
    monkeypatch.setattr(api.routine_service, "prepare_publication", prepare)

    with pytest.raises(HTTPException) as raised:
        await api.prepare_work_board_routine_publication(
            request=None,
            task_id=TASK_ID,
            body=WorkBoardRoutinePublicationPrepareRequest(
                expected_revision=7,
                body="summary",
            ),
        )

    assert raised.value.status_code == 403
    assert raised.value.detail["recovery_action"] == "restore_prerequisite"
    context_lookup.assert_awaited_once()
    block.assert_awaited_once_with(context)
    prepare.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_grant_blocks_only_a_no_effect_publication_wait(monkeypatch):
    context = _context()
    context["task"].status = WorkBoardStatus.running
    context["task"].block_reason = None
    context["attempt"].lease_owner = "seraph-work-board:guardian-routine.v1"
    context["attempt"].lease_expires_at = datetime.now(timezone.utc)
    cancel = AsyncMock(return_value=[])
    project = AsyncMock()
    reconcile = AsyncMock()
    monkeypatch.setattr(api.routine_service, "cancel_invocation_job_tree", cancel)
    monkeypatch.setattr(api.dispatcher, "_project", project)
    monkeypatch.setattr(api.dispatcher, "reconcile_linked_attempts", reconcile)

    assert await api._block_routine_for_missing_external_authority(context) is True
    cancel.assert_awaited_once_with(
        PARENT_RUN_ID,
        routine_id="routine-m6",
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
        reason="external_mutation_authority_missing",
    )
    assert project.await_args.kwargs["status"] is WorkBoardStatus.blocked
    assert project.await_args.kwargs["block_kind"] == "capability"
    assert project.await_args.kwargs["result_refs"][0]["recovery_action"] == "retry_after_prerequisite"

    project.reset_mock()
    cancel.reset_mock()
    context["parent_effects"] = [{"status": "unknown"}]
    assert await api._block_routine_for_missing_external_authority(context) is False
    cancel.assert_not_awaited()
    project.assert_not_awaited()
    reconcile.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_missing_grant_preserves_blocked_open_attempt_for_same_run_recovery(monkeypatch):
    context = _context()
    context["task"].status = WorkBoardStatus.blocked
    context["task"].block_reason = "awaiting_publication_approval"
    context["attempt"].lease_owner = None
    context["attempt"].lease_expires_at = None
    pause = AsyncMock()
    cancel = AsyncMock()
    reconcile = AsyncMock()
    monkeypatch.setattr(api.dispatcher, "_pause_routine_for_operator", pause)
    monkeypatch.setattr(api.dispatcher, "reconcile_linked_attempts", reconcile)
    monkeypatch.setattr(api.routine_service, "cancel_invocation_job_tree", cancel)

    assert await api._block_routine_for_missing_external_authority(context) is True

    pause.assert_awaited_once_with(
        context["task"],
        context["attempt"],
        context["parent"],
        reason="external_mutation_grant_required",
    )
    cancel.assert_not_awaited()
    reconcile.assert_not_awaited()


@pytest.mark.asyncio
async def test_recover_requires_approved_preview_before_resuming_same_parent(client, async_db, monkeypatch):
    authority = await _bind_publication_consent(client, monkeypatch)
    context = _context(
        m3_job_id="github-followthrough:m6",
        approval_id="approval-m6",
        approval_status="approved",
        preview={"title": "Updated source", "body": "Verified operator supplied summary"},
    )
    _patch_route_context(monkeypatch, context, authority=authority)
    running_task = context["task"].model_copy(update={"status": WorkBoardStatus.running})
    running_detail = {**context["detail"], "task": running_task}
    monkeypatch.setattr(api.repository, "get_detail", AsyncMock(return_value=running_detail))
    recover = AsyncMock(
        return_value={
            "status": "running",
            "child": {"status": "awaiting_readback", "job_id": "github-followthrough:m6"},
        }
    )
    monkeypatch.setattr(api.routine_service, "recover", recover)
    monkeypatch.setattr(api.dispatcher, "resume_routine_attempt_for_operator_recovery", AsyncMock())
    reconcile = AsyncMock()
    monkeypatch.setattr(api.dispatcher, "reconcile_linked_attempts", reconcile)

    response = await api.recover_work_board_routine_publication(
        request=None,
        task_id=TASK_ID,
        body=WorkBoardRoutinePublicationRecoverRequest(expected_revision=7),
    )

    assert response["readback_required"] is True
    assert response["task"]["task_id"] == TASK_ID
    assert response["task"]["status"] == "running"
    assert response["publication"]["parent_workflow_run_id"] == PARENT_RUN_ID
    recover.assert_awaited_once_with(
        "routine-m6",
        PARENT_RUN_ID,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
        external_mutation_granted=True,
    )
    reconcile.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_recover_does_not_resume_pending_approval(client, async_db, monkeypatch):
    authority = await _bind_publication_consent(client, monkeypatch)
    context = _context(
        m3_job_id="github-followthrough:m6",
        approval_id="approval-m6",
        approval_status="pending",
        preview={"title": "Updated source", "body": "Verified operator supplied summary"},
    )
    _patch_route_context(monkeypatch, context, authority=authority)
    recover = AsyncMock()
    monkeypatch.setattr(api.routine_service, "recover", recover)
    reconcile = AsyncMock()
    monkeypatch.setattr(api.dispatcher, "reconcile_linked_attempts", reconcile)

    with pytest.raises(HTTPException) as raised:
        await api.recover_work_board_routine_publication(
            request=None,
            task_id=TASK_ID,
            body=WorkBoardRoutinePublicationRecoverRequest(expected_revision=7),
        )

    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "approval_not_current"
    recover.assert_not_awaited()
    reconcile.assert_not_awaited()
