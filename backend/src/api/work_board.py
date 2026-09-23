"""Authenticated API for the canonical operator work board."""

from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
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


router = APIRouter(prefix="/work-board")
repository = WorkBoardRepository()


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
) -> dict[str, Any]:
    dependency_count, completed_dependency_count = dependency_counts or (0, 0)
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
        "requires_review": task.requires_review,
        "reviewer_id": safe_board_identifier(task.reviewer_id, max_length=128),
        "dependency_count": dependency_count,
        "completed_dependency_count": completed_dependency_count,
        "task_revision": task.task_revision,
        "result_refs": _safe_receipt_refs(_decode_json_list(task.result_refs_json)),
        "artifact_refs": _safe_receipt_refs(_decode_json_list(task.artifact_refs_json)),
        "created_at": _json_value(task.created_at),
        "updated_at": _json_value(task.updated_at),
        "completed_at": _json_value(task.completed_at),
        "archived_at": _json_value(task.archived_at),
    }


async def _safe_task_payload(
    task: WorkBoardTask,
    *,
    dependency_counts: tuple[int, int] | None = None,
) -> dict[str, Any]:
    payload = _task_payload(task, dependency_counts=dependency_counts)
    for key in ("title", "body", "block_reason"):
        value = payload.get(key)
        if isinstance(value, str):
            payload[key] = await vault_redaction.redact_secrets_in_text(
                value,
                fail_closed=True,
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
    return {
        "attempt_id": attempt.attempt_id,
        "task_id": attempt.task_id,
        "workflow_run_id": safe_workflow_run_id(attempt.workflow_run_id),
        "task_revision_at_claim": attempt.task_revision_at_claim,
        "lease_owner": safe_board_identifier(attempt.lease_owner, max_length=512),
        "lease_expires_at": _json_value(attempt.lease_expires_at),
        "heartbeat_at": _json_value(attempt.heartbeat_at),
        "fencing_token": attempt.fencing_token,
        "executor_id": safe_board_identifier(attempt.executor_id, max_length=128),
        "started_at": _json_value(attempt.started_at),
        "ended_at": _json_value(attempt.ended_at),
        "outcome": _safe_attempt_outcome(attempt.outcome),
        "receipt_refs": _safe_receipt_refs(_decode_json_list(attempt.receipt_refs_json)),
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
            return {
                "task": await _safe_task_payload(detail["task"]),
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
        async with get_session() as db:
            mutation = await repository.action_task(db, _owner(operator), task_id, body)
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
