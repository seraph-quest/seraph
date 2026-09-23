"""Authenticated API for the canonical operator work board."""

from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from src.auth.service import AuthenticatedOperator
from src.db.engine import get_session
from src.db.models import (
    WorkBoardAttempt,
    WorkBoardComment,
    WorkBoardEvent,
    WorkBoardLink,
    WorkBoardStatus,
    WorkBoardTask,
    Goal,
)
from src.scheduler.connection_manager import ws_manager
from src.vault import redaction as vault_redaction
from src.work_board.contracts import (
    WorkBoardActionRequest,
    WorkBoardCommentCreate,
    WorkBoardLinkCreate,
    WorkBoardLinkDelete,
    WorkBoardOwner,
    WorkBoardTaskCreate,
    WorkBoardTaskPatch,
)
from src.work_board.repository import (
    BoardError,
    BoardMutation,
    WorkBoardRepository,
    _safe_receipt_refs,
    safe_board_reference,
    safe_board_identifier,
    safe_sha256_digest,
    safe_workflow_run_id,
)
from src.goals.repository import deserialize_admission_budget
from src.work_board.dispatcher import _dispatcher


router = APIRouter(prefix="/work-board")
repository = WorkBoardRepository()
# Use the same managed dispatcher instance as the scheduler so cancellation
# can reach an inline GoalSnapshot worker admitted by the scheduler pass.
dispatcher = _dispatcher

_RECOVERY_ACTIONS = frozenset(
    {
        "unblock",
        "retry",
        "cancel",
        "approve_existing_run",
        "reconcile_external_effect",
        "reconcile_admission_binding",
        "restore_prerequisite",
    }
)


def _recovery_action(
    task: WorkBoardTask,
    *,
    latest_attempt: WorkBoardAttempt | None = None,
    attempt_count: int = 0,
) -> str | None:
    """Derive the only operator recovery action allowed for this projection."""

    status = _json_value(task.status)
    block_kind = str(task.block_kind or "")
    if status == WorkBoardStatus.running.value:
        # A pending admission has no durable run to cancel.  The dispatcher
        # must reconcile that binding first so the card never advertises a
        # control that could guess a process or run id.
        if (
            latest_attempt is not None
            and latest_attempt.workflow_run_id
            and latest_attempt.ended_at is None
            and latest_attempt.lease_owner
            and latest_attempt.cancel_requested_at is None
        ):
            return "cancel"
        return "reconcile_admission_binding" if latest_attempt is not None else None
    if status != WorkBoardStatus.blocked.value:
        return None
    if (
        block_kind == "operator"
        and (latest_attempt is None or latest_attempt.ended_at is not None)
        and str(task.block_source_status or "")
        in {item.value for item in (WorkBoardStatus.triage, WorkBoardStatus.todo, WorkBoardStatus.ready, WorkBoardStatus.review)}
    ):
        return "unblock"
    if block_kind == "reconcile_admission_binding":
        return "reconcile_admission_binding"
    if block_kind in {"unknown_effect", "cost_liability"}:
        return "reconcile_external_effect"
    if block_kind == "needs_input":
        return "approve_existing_run"
    if block_kind == "capability":
        return "restore_prerequisite"
    if block_kind in {"transient", "cancelled"} and latest_attempt is not None:
        if latest_attempt.ended_at is None or attempt_count >= 2:
            return None
        refs = _decode_json_list(latest_attempt.receipt_refs_json)
        if not refs or any(
            not isinstance(item, dict)
            or str(item.get("status") or "") in {"unknown", "intent", "dispatched", "unknown_external_effect", "cost_liability"}
            or str(item.get("reason_code") or item.get("outcome") or "")
            not in {"no_external_effect", "not_dispatched", "cancelled", "operator_cancelled", "transient"}
            for item in refs
        ):
            return None
        return "retry"
    return None


def _operator(request: Request) -> AuthenticatedOperator:
    operator = getattr(request.state, "operator", None)
    principal = getattr(operator, "principal", None)
    session_id = str(getattr(operator, "session_id", "") or "").strip()
    principal_id = str(getattr(principal, "principal_id", "") or "").strip()
    if not isinstance(operator, AuthenticatedOperator) or not principal_id or not session_id:
        raise HTTPException(status_code=401, detail={"code": "authentication_required"})
    if (
        not bool(getattr(principal, "authenticated", False))
        or bool(getattr(principal, "revoked", False))
        or str(getattr(principal, "session_id", "") or "") != session_id
        or str(getattr(principal, "operator_session_id", "") or "") != session_id
    ):
        raise HTTPException(status_code=401, detail={"code": "session_unavailable"})
    return operator


def _owner(operator: AuthenticatedOperator) -> WorkBoardOwner:
    return WorkBoardOwner(
        principal_id=operator.principal.principal_id,
        session_id=operator.session_id,
    )


def _raise_board_error(exc: BoardError) -> None:
    detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
    detail.update(exc.extra)
    raise HTTPException(status_code=exc.status_code, detail=detail) from exc


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    return value


def _task_payload(
    task: WorkBoardTask,
    *,
    dependency_counts: tuple[int, int] | None = None,
    latest_attempt: WorkBoardAttempt | None = None,
    attempt_count: int = 0,
    dispatch_rank: int | None = None,
) -> dict[str, Any]:
    dependency_count, completed_dependency_count = dependency_counts or (0, 0)
    attempt_payload = _attempt_payload(latest_attempt) if latest_attempt is not None else None
    readback_status = attempt_payload.get("readback_status") if attempt_payload else "not_started"
    verification_status = attempt_payload.get("verification_status") if attempt_payload else "not_started"
    return {
        "task_id": task.task_id,
        "creation_sequence": task.creation_sequence,
        "owner_principal_id": task.owner_principal_id,
        "owner_session_id": task.owner_session_id,
        "origin_session_id": safe_board_identifier(task.origin_session_id, max_length=512),
        "origin_thread_id": safe_board_identifier(task.origin_thread_id, max_length=256),
        "goal_id": task.goal_id,
        "goal_revision": task.goal_revision,
        "title": task.title,
        "body": task.body,
        "capability_id": safe_board_identifier(task.capability_id, max_length=128),
        "typed_input_ref": safe_board_reference(task.typed_input_ref, max_length=512),
        "typed_input_digest": safe_sha256_digest(task.typed_input_digest),
        "executor_id": safe_board_identifier(task.executor_id, max_length=128),
        "assignee_id": safe_board_identifier(task.assignee_id, max_length=128),
        "priority": task.priority,
        "idempotency_scope": task.idempotency_scope,
        "idempotency_key": task.idempotency_key,
        "scheduled_at": _json_value(task.scheduled_at),
        "status": _json_value(task.status),
        "block_kind": (
            task.block_kind
            if isinstance(task.block_kind, str) and task.block_kind in _SAFE_EVENT_BLOCK_KINDS
            else None
        ),
        "block_reason": task.block_reason,
        "block_source_status": (
            task.block_source_status
            if isinstance(task.block_source_status, str)
            and task.block_source_status in _SAFE_EVENT_STATUSES
            else None
        ),
        "cancel_requested_at": _json_value(latest_attempt.cancel_requested_at) if latest_attempt is not None else None,
        "requires_review": task.requires_review,
        "reviewer_id": safe_board_identifier(task.reviewer_id, max_length=128),
        "dependency_count": dependency_count,
        "completed_dependency_count": completed_dependency_count,
        "dispatch_rank": dispatch_rank,
        "recovery_action": _recovery_action(
            task,
            latest_attempt=latest_attempt,
            attempt_count=attempt_count,
        ),
        "readback_status": readback_status,
        "verification_status": verification_status,
        "task_revision": task.task_revision,
        "result_refs": _safe_receipt_refs(_decode_json_list(task.result_refs_json)),
        "artifact_refs": _safe_receipt_refs(_decode_json_list(task.artifact_refs_json)),
        "latest_attempt": attempt_payload,
        "created_at": _json_value(task.created_at),
        "updated_at": _json_value(task.updated_at),
        "completed_at": _json_value(task.completed_at),
        "archived_at": _json_value(task.archived_at),
    }


async def _safe_task_payload(
    task: WorkBoardTask,
    *,
    dependency_counts: tuple[int, int] | None = None,
    latest_attempt: WorkBoardAttempt | None = None,
    attempt_count: int = 0,
    dispatch_rank: int | None = None,
) -> dict[str, Any]:
    payload = _task_payload(
        task,
        dependency_counts=dependency_counts,
        latest_attempt=latest_attempt,
        attempt_count=attempt_count,
        dispatch_rank=dispatch_rank,
    )
    for key in ("title", "body", "block_reason"):
        value = payload.get(key)
        if isinstance(value, str):
            payload[key] = await vault_redaction.redact_secrets_in_text(
                value,
                fail_closed=True,
            )
    if payload.get("recovery_action") == "retry":
        # Recovery controls are an operator projection of current authority,
        # not a cached promise from the last dispatcher pass.  Re-run the
        # provider-free retry gates before exposing a retry button; a failed
        # gate remains a bounded prerequisite recovery while the task stays
        # Blocked.
        try:
            await dispatcher.validate_retry(
                WorkBoardOwner(
                    principal_id=task.owner_principal_id,
                    session_id=task.owner_session_id,
                ),
                task.task_id,
                expected_revision=task.task_revision,
            )
        except BoardError as exc:
            payload["recovery_action"] = str(
                exc.extra.get("recovery_action") or "restore_prerequisite"
            )
    elif payload.get("recovery_action") == "unblock":
        # Generic unblock is only a live operator convenience for an
        # owner-bound manual block.  Recheck the session, goal revision, and
        # phase-specific Ready/Review evidence before advertising it.
        try:
            await dispatcher.validate_unblock(
                WorkBoardOwner(
                    principal_id=task.owner_principal_id,
                    session_id=task.owner_session_id,
                ),
                task.task_id,
                expected_revision=task.task_revision,
            )
        except BoardError as exc:
            payload["recovery_action"] = str(
                exc.extra.get("recovery_action") or "restore_prerequisite"
            )
    return payload


def _decode_json_list(value: str | None) -> list[Any]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


_SAFE_EVENT_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SAFE_EVENT_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SAFE_EVENT_STATUSES = frozenset(item.value for item in WorkBoardStatus)
_SAFE_EVENT_BLOCK_KINDS = frozenset(
    {
        "operator",
        "unknown_effect",
        "cost_liability",
        "reconcile_admission_binding",
        "capability",
        "needs_input",
        "transient",
        "cancelled",
    }
)
_SAFE_EVENT_OUTCOMES = frozenset(
    {
        "accepted",
        "queued",
        "running",
        "succeeded",
        "degraded",
        "settled",
        "failed",
        "blocked",
        "cancelled",
        "awaiting_approval",
        "needs_input",
        "capability",
        "transient",
        "read_back",
        "reconciled",
        "unknown",
        "unknown_external_effect",
        "cost_liability",
        "intent",
        "dispatched",
        "no_external_effect",
        "not_dispatched",
    }
)
_SAFE_EVENT_RECOVERY_ACTIONS = frozenset(
    {
        "unblock",
        "retry",
        "cancel",
        "approve_existing_run",
        "reconcile_external_effect",
        "reconcile_admission_binding",
        "restore_prerequisite",
    }
)
_SAFE_EVENT_CHANGED_FIELDS = frozenset(
    {
        "title",
        "body",
        "priority",
        "capability_id",
        "typed_input_ref",
        "typed_input_digest",
        "executor_id",
        "assignee_id",
        "scheduled_at",
        "status",
        "task_revision",
        "updated_at",
    }
)
_SAFE_EVENT_REFERENCE_FIELDS = frozenset(
    {"parent_task_id", "child_task_id", "comment_id", "attempt_id", "workflow_run_id"}
)


def _safe_event_metadata(value: Any) -> dict[str, Any]:
    """Redact legacy event rows again at the API and websocket boundary."""
    if not isinstance(value, dict):
        return {}
    safe: dict[str, Any] = {}
    for key, candidate in value.items():
        if key in {"task_revision", "expected_revision", "event_id"}:
            if isinstance(candidate, int) and not isinstance(candidate, bool) and 0 <= candidate <= 2**63 - 1:
                safe[key] = candidate
        elif key == "ready_demoted":
            if isinstance(candidate, bool):
                safe[key] = candidate
        elif key in {"status", "from_status"}:
            if isinstance(candidate, str) and candidate in _SAFE_EVENT_STATUSES:
                safe[key] = candidate
        elif key == "block_kind":
            if isinstance(candidate, str) and candidate in _SAFE_EVENT_BLOCK_KINDS:
                safe[key] = candidate
        elif key in {"outcome", "reason_code", "error_code"}:
            if isinstance(candidate, str) and candidate in _SAFE_EVENT_OUTCOMES:
                safe[key] = candidate
        elif key == "recovery_action":
            if isinstance(candidate, str) and candidate in _SAFE_EVENT_RECOVERY_ACTIONS:
                safe[key] = candidate
        elif key in _SAFE_EVENT_REFERENCE_FIELDS:
            reference = safe_workflow_run_id(candidate)
            if reference is not None:
                safe[key] = reference
        elif key == "body_digest":
            if isinstance(candidate, str) and _SAFE_EVENT_DIGEST.fullmatch(candidate.lower()):
                safe[key] = candidate.lower()
        elif key == "changed_fields":
            if isinstance(candidate, (list, tuple)):
                fields = [
                    item
                    for item in candidate[:32]
                    if isinstance(item, str) and item in _SAFE_EVENT_CHANGED_FIELDS
                ]
                if fields:
                    safe[key] = fields
    return safe


def _safe_event_kind(value: Any) -> str:
    if isinstance(value, str) and _SAFE_EVENT_TOKEN.fullmatch(value):
        return value
    return "event.unknown"


def _safe_attempt_outcome(value: Any) -> str | None:
    if isinstance(value, str) and value in _SAFE_EVENT_OUTCOMES:
        return value
    return None


def _attempt_payload(attempt: WorkBoardAttempt) -> dict[str, Any]:
    receipt_refs = _decode_json_list(attempt.receipt_refs_json)
    verified = any(
        isinstance(item, dict)
        and bool(item.get("verified"))
        and str(item.get("status") or "") in {"succeeded", "read_back", "reconciled"}
        for item in receipt_refs
    )
    unresolved = any(
        isinstance(item, dict)
        and str(item.get("status") or "") in {"unknown", "intent", "dispatched", "unknown_external_effect", "cost_liability"}
        for item in receipt_refs
    )
    decisive_failure = any(
        isinstance(item, dict)
        and (
            str(item.get("readback_status") or "") == "failed"
            or str(item.get("verification_status") or "") == "failed"
        )
        for item in receipt_refs
    )
    if verified:
        readback_status = "verified"
        verification_status = "passed"
    elif unresolved:
        readback_status = "unknown"
        verification_status = "reconciliation_required"
    elif attempt.ended_at is None:
        readback_status = "pending"
        verification_status = "pending"
    elif str(attempt.outcome or "") == "cancelled":
        readback_status = "not_applicable"
        verification_status = "cancelled"
    elif decisive_failure:
        readback_status = "failed"
        verification_status = "failed"
    else:
        readback_status = "unknown"
        verification_status = "reconciliation_required"
    return {
        "attempt_id": attempt.attempt_id,
        "task_id": attempt.task_id,
        "workflow_run_id": safe_workflow_run_id(attempt.workflow_run_id),
        "task_revision_at_claim": attempt.task_revision_at_claim,
        "lease_owner": safe_board_identifier(attempt.lease_owner, max_length=512),
        "cancel_requested_at": _json_value(attempt.cancel_requested_at),
        "lease_expires_at": _json_value(attempt.lease_expires_at),
        "heartbeat_at": _json_value(attempt.heartbeat_at),
        "fencing_token": attempt.fencing_token,
        "executor_id": safe_board_identifier(attempt.executor_id, max_length=128),
        "started_at": _json_value(attempt.started_at),
        "ended_at": _json_value(attempt.ended_at),
        "outcome": _safe_attempt_outcome(attempt.outcome),
        "receipt_refs": _safe_receipt_refs(_decode_json_list(attempt.receipt_refs_json)),
        "readback_status": readback_status,
        "verification_status": verification_status,
        "created_at": _json_value(attempt.created_at),
        "updated_at": _json_value(attempt.updated_at),
    }


async def _safe_comment_payload(comment: WorkBoardComment) -> dict[str, Any]:
    return {
        "comment_id": comment.comment_id,
        "task_id": comment.task_id,
        "author_principal_id": comment.author_principal_id,
        "author_session_id": comment.author_session_id,
        "body": await vault_redaction.redact_secrets_in_text(comment.body, fail_closed=True),
        "created_at": _json_value(comment.created_at),
    }


def _event_payload(event: WorkBoardEvent) -> dict[str, Any]:
    try:
        metadata = json.loads(event.metadata_json or "{}")
    except (TypeError, ValueError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "event_id": event.event_id,
        "task_id": event.task_id,
        "kind": _safe_event_kind(event.kind),
        "metadata": _safe_event_metadata(metadata),
        "created_at": _json_value(event.created_at),
    }


async def _broadcast(event: WorkBoardEvent) -> None:
    if event.event_id is None:
        return
    await ws_manager.broadcast_work_board_event(
        _event_payload(event),
        owner_principal_id=event.owner_principal_id,
        operator_session_id=event.owner_session_id,
    )


@router.get("/tasks")
async def list_work_board_tasks(
    request: Request,
    status: WorkBoardStatus | None = None,
    executor_id: str | None = Query(default=None, max_length=128),
    assignee_id: str | None = Query(default=None, max_length=128),
    q: str | None = Query(default=None, max_length=200),
    after: int | None = Query(default=None, ge=0),
    limit: int = Query(default=100, ge=1, le=100),
):
    operator = _operator(request)
    try:
        async with get_session() as db:
            page = await repository.list_tasks(
                db,
                _owner(operator),
                status=status,
                executor_id=executor_id,
                assignee_id=assignee_id,
                query=q,
                after=after,
                limit=limit,
            )
            return {
                "tasks": [
                    await _safe_task_payload(
                        task,
                        dependency_counts=page.dependency_counts.get(task.task_id),
                        latest_attempt=page.latest_attempts.get(task.task_id),
                        attempt_count=page.attempt_counts.get(task.task_id, 0),
                        dispatch_rank=page.dispatch_ranks.get(task.task_id),
                    )
                    for task in page.tasks
                ],
                "next_after": page.next_after,
                "last_event_id": page.last_event_id,
            }
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.get("/goals/{goal_id}/execution-limits")
async def get_work_board_execution_limits(
    request: Request,
    goal_id: str,
    goal_revision: int = Query(..., ge=1),
):
    """Return the server-derived finite board runtime limit for one goal."""

    operator = _operator(request)
    try:
        async with get_session() as db:
            goal = (
                await db.execute(
                    select(Goal).where(
                        Goal.id == goal_id,
                        Goal.owner_principal_id == operator.principal.principal_id,
                        Goal.owner_session_id == operator.session_id,
                    )
                )
            ).scalar_one_or_none()
            if goal is None:
                raise HTTPException(status_code=404, detail={"code": "goal_not_found"})
            current_revision = max(int(getattr(goal, "revision", 1) or 1), 1)
            if current_revision != int(goal_revision):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "goal_revision_stale",
                        "goal_id": goal_id,
                        "expected_revision": int(goal_revision),
                        "current_revision": current_revision,
                    },
                )
            budget = deserialize_admission_budget(goal)
            configured = int(budget.max_runtime_seconds) if budget is not None else 300
            effective = min(max(configured, 1), 900)
            return {
                "goal_id": goal_id,
                "goal_revision": current_revision,
                "effective_max_runtime_seconds": effective,
                "default_max_runtime_seconds": 300,
                "hard_max_runtime_seconds": 900,
                "attempt_limit": 2,
                "limit_source": "goal_admission_budget" if budget is not None else "default",
            }
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Retry after the workspace database is ready."},
        ) from exc


@router.post("/tasks")
async def create_work_board_task(request: Request, body: WorkBoardTaskCreate):
    operator = _operator(request)
    try:
        async with get_session() as db:
            mutation = await repository.create_task(
                db,
                _owner(operator),
                body,
                origin_session_id=operator.session_id,
            )
            payload = {
                "task": await _safe_task_payload(mutation.task),
                "idempotent_replay": mutation.idempotent_replay,
            }
        if not mutation.idempotent_replay:
            await _broadcast(mutation.event)
        return payload
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.get("/tasks/{task_id}")
async def get_work_board_task(request: Request, task_id: str):
    operator = _operator(request)
    try:
        async with get_session() as db:
            detail = await repository.get_detail(db, _owner(operator), task_id)
            dispatch_rank = await repository.dispatch_rank(db, _owner(operator), detail["task"])
            return {
                "task": await _safe_task_payload(
                    detail["task"],
                    latest_attempt=(detail["attempts"][0] if detail["attempts"] else None),
                    attempt_count=len(detail["attempts"]),
                    dispatch_rank=dispatch_rank,
                ),
                "attempts": [_attempt_payload(item) for item in detail["attempts"]],
                "parents": detail["parents"],
                "children": detail["children"],
                "comments": [await _safe_comment_payload(item) for item in detail["comments"]],
                "events": [_event_payload(item) for item in detail["events"]],
                "revision": detail["task"].task_revision,
            }
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.patch("/tasks/{task_id}")
async def patch_work_board_task(request: Request, task_id: str, body: WorkBoardTaskPatch):
    operator = _operator(request)
    try:
        async with get_session() as db:
            mutation = await repository.patch_task(db, _owner(operator), task_id, body)
            payload = {"task": await _safe_task_payload(mutation.task)}
        await _broadcast(mutation.event)
        return payload
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.post("/tasks/{task_id}/actions")
async def action_work_board_task(request: Request, task_id: str, body: WorkBoardActionRequest):
    operator = _operator(request)
    try:
        owner = _owner(operator)
        if body.action.value == "cancel":
            projection = await dispatcher.cancel_task(
                owner,
                task_id,
                expected_revision=body.expected_revision,
            )
            payload = {
                "task": await _safe_task_payload(
                    projection.task,
                    latest_attempt=projection.attempt,
                    attempt_count=1,
                ),
                "attempt": _attempt_payload(projection.attempt),
            }
            await _broadcast(projection.event)
            return payload
        if body.action.value == "retry":
            await dispatcher.validate_retry(
                owner,
                task_id,
                expected_revision=body.expected_revision,
            )
        elif body.action.value == "unblock":
            # Run the live owner/goal/phase preflight before opening the
            # repository transaction.  The repository remains the final CAS
            # authority, so a stale preflight can only fail closed.
            await dispatcher.validate_unblock(
                owner,
                task_id,
                expected_revision=body.expected_revision,
            )
        async with get_session() as db:
            if body.action.value == "retry":
                mutation = await repository.retry_task(
                    db,
                    owner,
                    task_id,
                    expected_revision=body.expected_revision,
                )
            else:
                mutation = await repository.action_task(db, owner, task_id, body)
            payload = {"task": await _safe_task_payload(mutation.task)}
        await _broadcast(mutation.event)
        return payload
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.post("/tasks/{task_id}/comments")
async def add_work_board_comment(request: Request, task_id: str, body: WorkBoardCommentCreate):
    operator = _operator(request)
    try:
        async with get_session() as db:
            comment, event = await repository.add_comment(db, _owner(operator), task_id, body)
            payload = {"comment": await _safe_comment_payload(comment)}
        await _broadcast(event)
        return payload
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.post("/links")
async def add_work_board_link(request: Request, body: WorkBoardLinkCreate):
    operator = _operator(request)
    try:
        async with get_session() as db:
            link, event = await repository.add_link(db, _owner(operator), body)
            payload = {
                "link": {
                    "link_id": link.link_id,
                    "parent_task_id": link.parent_task_id,
                    "child_task_id": link.child_task_id,
                    "created_at": _json_value(link.created_at),
                }
            }
        await _broadcast(event)
        return payload
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.delete("/links")
async def delete_work_board_link(request: Request, body: WorkBoardLinkDelete):
    operator = _operator(request)
    try:
        async with get_session() as db:
            event = await repository.delete_link(db, _owner(operator), body)
        await _broadcast(event)
        return {"deleted": True, "event_id": event.event_id}
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.get("/events")
async def list_work_board_events(
    request: Request,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=100),
):
    operator = _operator(request)
    try:
        async with get_session() as db:
            page = await repository.list_events(db, _owner(operator), after=after, limit=limit)
            return {
                "events": [_event_payload(event) for event in page.events],
                "last_event_id": page.last_event_id,
                "gap": page.gap,
            }
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


__all__ = ["repository", "router"]
