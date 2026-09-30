"""Authenticated API for the canonical operator work board."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import uuid
from typing import Any, Mapping

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from src.auth.service import AuthenticatedOperator
from src.approval.repository import approval_repository
from src.db.engine import get_session
from src.db.models import (
    WorkBoardAttempt,
    WorkBoardComment,
    WorkBoardLink,
    WorkBoardStatus,
    WorkBoardTask,
    Goal,
)
from src.vault import redaction as vault_redaction
from src.work_board.contracts import (
    WorkBoardActionRequest,
    WORK_BOARD_AUTHENTICATED_BLOCK_KINDS,
    WorkBoardCommentCreate,
    WorkBoardLinkCreate,
    WorkBoardLinkDelete,
    WorkBoardOwner,
    WorkBoardTaskCreate,
    WorkBoardTaskPatch,
    WorkBoardProposalAccept,
    WorkBoardProposalReject,
    WorkBoardProposalRequest,
    WorkBoardRoutinePublicationPrepareRequest,
    WorkBoardRoutinePublicationRecoverRequest,
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
from src.work_board.events import (
    _SAFE_EVENT_BLOCK_KINDS,
    _SAFE_EVENT_OUTCOMES,
    _SAFE_EVENT_STATUSES,
    _event_payload,
)
from src.goals.repository import deserialize_admission_budget
from src.work_board.dispatcher import TypedInputError, _dispatcher, _parse_typed_input
from src.work_board import review as review_service
from src.work_board import triage as triage_service
from src.work_board.time import serialize_utc_datetime
from src.security.trust_contract import AuthorityGrant
from src.workflows.job_runtime import durable_job_repository
from src.workflows.routines import (
    RoutinePublicationRequest,
    RoutineError,
    _job_checkpoint,
    routine_service,
)


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
        "configure_goal_success_criterion",
        "renew_review",
        "prepare_routine_publication",
        "resume_routine_publication",
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
        ):
            return "cancel"
        return "reconcile_admission_binding" if latest_attempt is not None else None
    if status != WorkBoardStatus.blocked.value:
        return None
    if (
        (
            block_kind == "operator"
            or review_service._is_handoff_reconciliation_block(
                block_kind,
                task.block_reason,
            )
        )
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
        if (
            task.block_reason == "external_mutation_grant_required"
            and latest_attempt is not None
            and latest_attempt.ended_at is not None
            and attempt_count < 2
        ):
            return "retry"
        # A pre-admission capability gate has no effect to reconcile. Once
        # its prerequisite is restored, Retry re-runs the live gate before
        # returning the card to Todo; no attempt or effect is replayed here.
        if latest_attempt is None and attempt_count == 0:
            return "retry"
        return "restore_prerequisite"
    if block_kind == "review_expired":
        return "renew_review"
    if block_kind == "attempt_limit":
        # Attempt exhaustion is a terminal recovery boundary for this card.
        # Further work starts as a new linked task, so never expose retry.
        return None
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


def _operator_has_external_mutation(operator: AuthenticatedOperator) -> bool:
    grants = {
        str(getattr(item, "value", item))
        for item in (getattr(getattr(operator, "principal", None), "grants", ()) or ())
    }
    return AuthorityGrant.EXTERNAL_MUTATION.value in grants


def _raise_board_error(exc: BoardError) -> None:
    detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
    detail.update(exc.extra)
    raise HTTPException(status_code=exc.status_code, detail=detail) from exc


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return serialize_utc_datetime(value)
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
        "review_expires_at": _json_value(task.review_expires_at),
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
        # Generic unblock is only exposed for an owner-bound manual block or
        # an exact verified-handoff recovery. Recheck the live session, goal,
        # and reviewer boundary before advertising the action.
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


def _routine_invocation_uuid(task_id: str, attempt_id: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"seraph:work-board-attempt:{task_id}:{attempt_id}",
        )
    )


def _routine_publication_error(
    code: str,
    message: str,
    *,
    status_code: int = 409,
    **extra: Any,
) -> BoardError:
    return BoardError(code, message, status_code=status_code, **extra)


def _safe_publication_preview(value: Any) -> dict[str, Any] | None:
    """Keep only bounded, operator-safe fields from the M3 preview receipt."""

    if not isinstance(value, Mapping):
        return None
    preview: dict[str, Any] = {}
    for key in (
        "repository",
        "action",
        "issue_number",
        "body_sha256",
        "marker",
        "dossier_artifact_id",
        "dossier_sha256",
        "source_watch_id",
        "connection_revision",
    ):
        item = value.get(key)
        if item is not None:
            preview[key] = item
    for key, maximum in (("title", 160), ("body", 32_000)):
        item = value.get(key)
        if item is not None:
            preview[key] = str(item)[:maximum]
    return preview or None


async def _routine_publication_context(
    db,
    operator: AuthenticatedOperator,
    task_id: str,
    *,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Resolve publication recovery only from the canonical board binding.

    The browser supplies a task ID and, for mutations, the task revision. The
    attempt, parent workflow run, routine binding, package/watch authority,
    publication child, and approval are all read from durable task/runtime
    records. This intentionally accepts no caller-selected attempt, run, or
    approval identity.
    """

    owner = _owner(operator)
    detail = await repository.get_detail(db, owner, task_id)
    task = detail["task"]
    if task.capability_id != "guardian-routine.v1":
        raise _routine_publication_error(
            "routine_publication_not_supported",
            "The selected task is not a governed routine invocation",
            status_code=422,
        )
    if expected_revision is not None and int(task.task_revision) != int(expected_revision):
        raise BoardRevisionConflict(task.task_id, int(expected_revision), int(task.task_revision))
    if task.status not in {WorkBoardStatus.running, WorkBoardStatus.blocked}:
        raise _routine_publication_error(
            "routine_publication_task_state_invalid",
            "Routine publication recovery requires the same active or publication-blocked task",
        )
    attempts = [
        item
        for item in detail["attempts"]
        if item.workflow_run_id and item.ended_at is None
    ]
    if not attempts:
        raise _routine_publication_error(
            "routine_publication_attempt_missing",
            "The canonical routine task has no resumable linked durable workflow attempt",
        )
    attempt = attempts[0]
    workflow_run_id = str(attempt.workflow_run_id or "")
    if not workflow_run_id:
        raise _routine_publication_error(
            "routine_publication_attempt_missing",
            "The canonical routine attempt has no durable parent workflow run",
        )
    try:
        inputs = _parse_typed_input(task)
    except TypedInputError as exc:
        raise _routine_publication_error(
            exc.code,
            "The routine task's immutable typed input cannot be trusted for publication recovery",
        ) from exc
    routine_id = str(inputs.get("routine_id") or "")
    try:
        routine_version = int(inputs.get("version") or 0)
        routine_revision = int(inputs.get("expected_routine_revision") or 0)
        watch_revision = int(inputs.get("expected_watch_revision") or 0)
        goal_revision = int(inputs.get("expected_goal_revision") or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _routine_publication_error(
            "routine_publication_binding_invalid",
            "The routine task's typed authority is malformed",
        ) from exc
    watch_id = str(inputs.get("source_watch_id") or "")
    if (
        not routine_id
        or routine_version < 1
        or routine_revision < 1
        or not watch_id
        or watch_revision < 1
        or task.goal_id != str(inputs.get("goal_id") or "")
        or int(task.goal_revision) != goal_revision
    ):
        raise _routine_publication_error(
            "routine_publication_binding_mismatch",
            "The task goal and typed routine authority do not match",
        )

    parent = await durable_job_repository.get_job(workflow_run_id)
    if not isinstance(parent, Mapping):
        raise _routine_publication_error(
            "routine_publication_parent_missing",
            "The linked routine workflow run is unavailable",
        )
    if str(parent.get("job_id") or parent.get("run_identity") or "") != workflow_run_id:
        raise _routine_publication_error(
            "routine_publication_parent_mismatch",
            "The linked attempt does not identify the expected routine workflow",
        )
    parent_owner = parent.get("owner") if isinstance(parent.get("owner"), Mapping) else {}
    authority = parent.get("declared_authority") if isinstance(parent.get("declared_authority"), Mapping) else {}
    persisted_sessions = {
        str(value)
        for value in (
            authority.get("session_id"),
            parent.get("operator_session_id"),
            parent.get("session_id"),
        )
        if str(value or "")
    }
    try:
        parent_goal_revision = int(parent.get("goal_revision") or 0)
        parent_connection_revision = int(authority.get("github_connection_revision") or 0)
        authority_routine_version = int(authority.get("routine_version") or 0)
        authority_routine_revision = int(authority.get("routine_revision") or 0)
        authority_watch_revision = int(authority.get("source_watch_revision") or 0)
        binding_matches = (
            str(parent.get("job_kind") or "") == "routine_invocation"
            and str(parent_owner.get("kind") or "") == "user"
            and str(parent_owner.get("principal_id") or "") == owner.principal_id
            and str(authority.get("goal_owner_principal_id") or "") == owner.principal_id
            and str(authority.get("goal_owner_session_id") or "") == owner.session_id
            and persisted_sessions == {owner.session_id}
            and str(authority.get("capability_id") or "") == "guardian-routine.v1"
            and bool(str(authority.get("package_digest") or ""))
            and bool(str(authority.get("github_connection_id") or ""))
            and parent_connection_revision >= 1
            and str(parent.get("goal_id") or "") == task.goal_id
            and parent_goal_revision == int(task.goal_revision)
            and str(parent.get("session_id") or parent.get("operator_session_id") or "") == owner.session_id
            and (
                parent.get("operator_session_id") is None
                or str(parent.get("operator_session_id") or "") == owner.session_id
            )
            and str(authority.get("routine_id") or "") == routine_id
            and authority_routine_version == routine_version
            and authority_routine_revision == routine_revision
            and str(authority.get("source_watch_id") or "") == watch_id
            and authority_watch_revision == watch_revision
            and str(authority.get("invocation_uuid") or "")
            == _routine_invocation_uuid(task.task_id, attempt.attempt_id)
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise _routine_publication_error(
            "routine_publication_binding_invalid",
            "The linked routine workflow authority is malformed",
        ) from exc
    if not binding_matches:
        raise _routine_publication_error(
            "routine_publication_binding_mismatch",
            "The linked routine workflow authority does not match the board task",
        )

    try:
        attempt_fence = int(attempt.fencing_token or 0)
        parent_fence = int((parent.get("lease") or {}).get("fencing_token") or 0)
    except (TypeError, ValueError, AttributeError) as exc:
        raise _routine_publication_error(
            "routine_publication_fence_invalid",
            "The routine attempt fence is malformed",
        ) from exc
    parent_status = str(parent.get("status") or "")
    parent_wait_reason = str(parent.get("failure_reason") or "")
    waiting_for_operator = (
        parent_status == "blocked"
        and parent_wait_reason
        in {"awaiting_publication_preview", "awaiting_publication_approval"}
    )
    if attempt_fence <= 0 or parent_fence != attempt_fence:
        raise _routine_publication_error(
            "stale_fence",
            "The routine attempt no longer owns the linked durable workflow",
        )
    if waiting_for_operator:
        if (
            task.status is not WorkBoardStatus.blocked
            or str(task.block_reason or "") not in {
                parent_wait_reason,
                "external_mutation_grant_required",
            }
            or attempt.lease_owner is not None
            or attempt.lease_expires_at is not None
        ):
            raise _routine_publication_error(
                "routine_publication_task_state_invalid",
                "The same card must be Blocked with its board lease released while an operator decision is pending",
            )
    elif parent_status == "running":
        if task.status is not WorkBoardStatus.running or not attempt.lease_owner:
            raise _routine_publication_error(
                "routine_publication_task_state_invalid",
                "A running routine parent requires its current fenced board attempt",
            )
        lease = parent.get("lease") if isinstance(parent.get("lease"), Mapping) else {}
        if not lease.get("owner") or not lease.get("expires_at"):
            raise _routine_publication_error(
                "stale_fence",
                "The running routine workflow has no current lease",
            )
        try:
            expiry = datetime.fromisoformat(str(lease["expires_at"]).replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= datetime.now(timezone.utc):
                raise ValueError("expired")
        except (TypeError, ValueError, OverflowError) as exc:
            raise _routine_publication_error(
                "stale_fence",
                "The routine workflow lease is expired or malformed",
            ) from exc
    else:
        raise _routine_publication_error(
            "routine_publication_not_ready",
            "The linked durable routine is not waiting for publication review",
        )

    publication_checkpoint = _job_checkpoint(parent, "routine:publication_child_recorded")
    m3_job_id = str((publication_checkpoint or {}).get("m3_job_id") or "")
    approval_id = str((publication_checkpoint or {}).get("approval_id") or "")
    m3_job = await durable_job_repository.get_job(m3_job_id) if m3_job_id else None
    publication = None
    if isinstance(m3_job, Mapping):
        m3_authority = (
            m3_job.get("declared_authority")
            if isinstance(m3_job.get("declared_authority"), Mapping)
            else {}
        )
        m3_owner = m3_job.get("owner") if isinstance(m3_job.get("owner"), Mapping) else {}
        m3_sessions = {
            str(value)
            for value in (
                m3_authority.get("session_id"),
                m3_job.get("operator_session_id"),
                m3_job.get("session_id"),
            )
            if str(value or "")
        }
        try:
            m3_goal_revision = int(m3_job.get("goal_revision") or 0)
            m3_connection_revision = int(m3_authority.get("connection_revision") or 0)
            m3_matches = (
                str(m3_job.get("job_kind") or "") == "github_followthrough_v1"
                and str(m3_job.get("capability_version") or "") == "1"
                and str(m3_owner.get("principal_id") or "") == owner.principal_id
                and m3_sessions == {owner.session_id}
                and str(m3_job.get("goal_id") or "") == task.goal_id
                and m3_goal_revision == int(task.goal_revision)
                and str(m3_authority.get("connection_id") or "")
                == str(authority.get("github_connection_id") or "")
                and m3_connection_revision == parent_connection_revision
                and str(m3_authority.get("source_watch_id") or "") == watch_id
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise _routine_publication_error(
                "routine_publication_child_binding_invalid",
                "The prepared publication child authority is malformed",
            ) from exc
        if not m3_matches:
            raise _routine_publication_error(
                "routine_publication_child_binding_mismatch",
                "The prepared publication child is not bound to the current routine authority",
            )
        from src.extensions.github_followthrough import GitHubFollowthroughService

        try:
            publication = await GitHubFollowthroughService()._prepare_job_response(m3_job)
        except Exception:
            publication = None
        approval_id = approval_id or str(
            (m3_job.get("declared_authority") or {}).get("approval_id")
            if isinstance(m3_job.get("declared_authority"), Mapping)
            else ""
        )
    approval_status = None
    if approval_id:
        approval = await approval_repository.get(approval_id)
        if approval is not None:
            if (
                str(getattr(approval, "owner_principal_id", "") or "") != owner.principal_id
                or str(getattr(approval, "operator_session_id", "") or "") != owner.session_id
            ):
                raise _routine_publication_error(
                    "approval_owner_mismatch",
                    "The publication approval belongs to another operator session",
                    status_code=403,
                )
            approval_details = {}
            try:
                parsed_details = json.loads(getattr(approval, "details_json", "") or "{}")
                if isinstance(parsed_details, Mapping):
                    approval_details = dict(parsed_details)
            except (TypeError, ValueError):
                approval_details = {}
            if str(approval_details.get("durable_job_id") or "") != m3_job_id:
                raise _routine_publication_error(
                    "approval_job_binding_mismatch",
                    "The publication approval is not bound to the canonical M3 job",
                )
            approval_status = str(getattr(approval, "status", "") or "")

    parent_failure = str(parent.get("failure_reason") or "")
    if not m3_job_id and parent_failure != "awaiting_publication_preview":
        raise _routine_publication_error(
            "routine_publication_not_ready",
            "The routine parent is not waiting for a publication preview",
        )
    if m3_job_id and not isinstance(m3_job, Mapping):
        raise _routine_publication_error(
            "routine_publication_child_missing",
            "The prepared publication child is unavailable for reconciliation",
        )
    return {
        "task": task,
        "attempt": attempt,
        "detail": detail,
        "parent": dict(parent),
        "parent_effects": list(parent.get("effects") or []),
        "inputs": inputs,
        "routine_id": routine_id,
        "routine_version": routine_version,
        "routine_revision": routine_revision,
        "source_watch_id": watch_id,
        "source_watch_revision": watch_revision,
        "parent_workflow_run_id": workflow_run_id,
        "m3_job_id": m3_job_id or None,
        "m3_status": str(m3_job.get("status") or "") if isinstance(m3_job, Mapping) else None,
        "publication_effects": list(m3_job.get("effects") or []) if isinstance(m3_job, Mapping) else [],
        "approval_id": approval_id or None,
        "approval_status": approval_status,
        "preview": _safe_publication_preview((publication or {}).get("preview")),
        "publication_response": publication,
    }


async def _routine_publication_payload(context: Mapping[str, Any]) -> dict[str, Any]:
    task = context["task"]
    attempt = context["attempt"]
    publication_response = context.get("publication_response")
    return {
        "task_id": task.task_id,
        "task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id,
        "parent_workflow_run_id": safe_workflow_run_id(context["parent_workflow_run_id"]),
        "routine_id": context["routine_id"],
        "routine_version": context["routine_version"],
        "routine_revision": context["routine_revision"],
        "source_watch_id": context["source_watch_id"],
        "source_watch_revision": context["source_watch_revision"],
        "parent_status": context["parent"].get("status"),
        "m3_job_id": safe_workflow_run_id(context.get("m3_job_id")),
        "m3_status": context.get("m3_status"),
        "approval_id": context.get("approval_id"),
        "approval_status": context.get("approval_status"),
        "preview": context.get("preview"),
        "status": (
            publication_response.get("status")
            if isinstance(publication_response, Mapping)
            else context["parent"].get("failure_reason") or context["parent"].get("status")
        ),
        "recovery_action": (
            "resume_routine_publication"
            if context.get("m3_job_id")
            else "prepare_routine_publication"
        ),
    }


async def _block_routine_for_missing_external_authority(context: Mapping[str, Any]) -> bool:
    """Safely block an unpublished routine attempt when its grant disappears.

    The routine parent is cancelled only while it is waiting for its explicit
    publication preview/approval and both durable effect ledgers are empty.
    Any uncertain or already dispatched outcome stays in normal reconciliation.
    """

    task = context["task"]
    attempt = context["attempt"]
    parent = context.get("parent") if isinstance(context.get("parent"), Mapping) else {}
    m3_status = str(context.get("m3_status") or "")
    parent_status = str(parent.get("status") or "")
    parent_reason = str(parent.get("failure_reason") or "")
    safe_parent_wait = (
        parent_status == "blocked"
        and parent_reason in {"awaiting_publication_preview", "awaiting_publication_approval"}
    )
    safe_child_wait = not context.get("m3_job_id") or m3_status in {
        "accepted",
        "queued",
        "awaiting_approval",
    }
    parent_effects = context.get("parent_effects")
    safe_parent_effects = isinstance(parent_effects, list) and all(
        isinstance(item, Mapping)
        and (
            str(item.get("status") or "") == "approved"
            or (
                str(item.get("status") or "") == "succeeded"
                and str(item.get("effect_type") or "")
                in {"guardian_routine_child", "guardian_routine_outcome"}
            )
        )
        for item in parent_effects
    )
    safe_effect_ledgers = safe_parent_effects and not context.get("publication_effects")
    if not safe_parent_wait or not safe_child_wait or not safe_effect_ledgers or attempt.ended_at is not None:
        await dispatcher.reconcile_linked_attempts()
        return False

    if task.status is WorkBoardStatus.blocked:
        # The durable routine parent is already waiting for an explicit
        # operator decision and its effect ledgers are empty. Preserve the
        # same open task attempt and workflow binding, release any board
        # lease, and expose the missing grant as the prerequisite to restore.
        # Recovery can then recheck authority and advance the fence before
        # resuming this exact durable run.
        if attempt.lease_owner is not None or attempt.lease_expires_at is not None:
            await dispatcher.reconcile_linked_attempts()
            return False
        await dispatcher._pause_routine_for_operator(
            task,
            attempt,
            context["parent"],
            reason="external_mutation_grant_required",
        )
        return True

    if task.status is not WorkBoardStatus.running:
        await dispatcher.reconcile_linked_attempts()
        return False

    cancellations = await routine_service.cancel_invocation_job_tree(
        context["parent_workflow_run_id"],
        routine_id=context["routine_id"],
        owner_principal_id=task.owner_principal_id,
        owner_session_id=task.owner_session_id,
        reason="external_mutation_authority_missing",
    )
    if cancellations:
        await dispatcher.reconcile_linked_attempts()
        return False
    receipt = {
        "workflow_run_id": context["parent_workflow_run_id"],
        "status": "cancelled",
        "reason_code": "capability",
        "recovery_action": "retry_after_prerequisite",
    }
    await dispatcher._project(
        task,
        attempt,
        board_revision=int(task.task_revision),
        status=WorkBoardStatus.blocked,
        outcome="capability",
        block_kind="capability",
        block_reason="external_mutation_grant_required",
        result_refs=[receipt],
        lease_owner=attempt.lease_owner or dispatcher.runner_id,
    )
    return True


def _decode_json_list(value: str | None) -> list[Any]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _action_receipt_payload(
    task_payload: dict[str, Any],
    event: Any,
    *,
    attempt_id: str | None,
) -> dict[str, Any]:
    """Return the authoritative receipt for one persisted board mutation.

    The nested task remains the compatibility projection used by existing
    callers.  These top-level fields let an operator reconcile one action
    against the exact append-only event created by the mutation transaction.
    Only already-safe task projection values are exposed as reason and
    recovery fields.
    """

    return {
        "task_id": task_payload.get("task_id"),
        "status": task_payload.get("status"),
        "revision": task_payload.get("task_revision"),
        "attempt_id": attempt_id,
        "reason_code": task_payload.get("block_kind"),
        "recovery_action": task_payload.get("recovery_action"),
        "event_id": getattr(event, "event_id", None),
    }


def _safe_attempt_outcome(value: Any) -> str | None:
    if isinstance(value, str) and value in _SAFE_EVENT_OUTCOMES:
        return value
    return None


def _attempt_payload(attempt: WorkBoardAttempt) -> dict[str, Any]:
    receipt_refs = _decode_json_list(attempt.receipt_refs_json)
    proof = review_service._verified_readback(attempt)
    verified = bool(
        proof
        and proof.get("workflow_run_id") == str(attempt.workflow_run_id or "")
        and proof.get("readback_id")
        and proof.get("verified_at")
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
                    dependency_counts=detail["dependency_counts"],
                    latest_attempt=(detail["attempts"][0] if detail["attempts"] else None),
                    attempt_count=len(detail["attempts"]),
                    dispatch_rank=dispatch_rank,
                ),
                "attempts": [_attempt_payload(item) for item in detail["attempts"]],
                "parents": detail["parents"],
                "children": detail["children"],
                "comments": [await _safe_comment_payload(item) for item in detail["comments"]],
                "events": [_event_payload(item) for item in detail["events"]],
                "parent_handoffs": await review_service.parent_handoffs(
                    db,
                    _owner(operator),
                    detail["task"],
                ),
                "revision": detail["task"].task_revision,
            }
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.get("/tasks/{task_id}/routine-publication")
async def get_work_board_routine_publication(request: Request, task_id: str):
    """Read the exact publication recovery state for one board card."""

    operator = _operator(request)
    try:
        async with get_session() as db:
            context = await _routine_publication_context(db, operator, task_id)
            task_payload = await _safe_task_payload(
                context["task"],
                latest_attempt=context["attempt"],
                attempt_count=len(context["detail"]["attempts"]),
            )
        return {
            "task": task_payload,
            "publication": await _routine_publication_payload(context),
        }
    except (BoardError, RoutineError) as exc:
        if isinstance(exc, BoardError):
            _raise_board_error(exc)
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.post("/tasks/{task_id}/routine-publication/prepare")
async def prepare_work_board_routine_publication(
    request: Request,
    task_id: str,
    body: WorkBoardRoutinePublicationPrepareRequest,
):
    """Prepare a bounded M3 preview from the task's canonical parent run.

    This route never accepts task/attempt/run/approval IDs from the browser and
    never approves or executes the publication.
    """

    operator = _operator(request)
    try:
        async with get_session() as db:
            context = await _routine_publication_context(
                db,
                operator,
                task_id,
                expected_revision=body.expected_revision,
            )
        if not _operator_has_external_mutation(operator):
            safely_blocked = await _block_routine_for_missing_external_authority(context)
            raise BoardError(
                "external_mutation_grant_required",
                "The current operator session has no external mutation grant; restore the grant before recovering this same run"
                if safely_blocked
                else "The current operator session has no external mutation grant; reconcile the linked run before retrying",
                status_code=403,
                recovery_action="restore_prerequisite",
            )
        existing_preview = context.get("preview")
        if context.get("m3_job_id"):
            # The exact M3 binding is immutable. A retry may redisplay it, but
            # cannot replace its approved body with a new caller-selected one.
            if isinstance(existing_preview, Mapping) and (
                str(existing_preview.get("body") or "") != body.body
                or str(existing_preview.get("title") or "") != str(body.title or "")
            ):
                raise BoardError(
                    "routine_publication_binding_conflict",
                    "The prepared publication preview is immutable; create a new governed invocation for changed text",
                )
        else:
            await dispatcher.resume_routine_attempt_for_operator_recovery(
                _owner(operator),
                context["task"],
                context["attempt"],
                context["parent"],
                expected_revision=body.expected_revision,
            )
            await routine_service.prepare_publication(
                context["routine_id"],
                context["parent_workflow_run_id"],
                RoutinePublicationRequest(title=body.title, body=body.body),
                owner_principal_id=operator.principal.principal_id,
                owner_session_id=operator.session_id,
                external_mutation_granted=True,
            )
            # Preparation resumes the durable parent only long enough to
            # create the exact M3 approval hold. Reconcile it back to
            # Blocked before returning the refreshed card and cursor.
            await dispatcher.reconcile_linked_attempts()
        async with get_session() as db:
            refreshed = await _routine_publication_context(db, operator, task_id)
            task_payload = await _safe_task_payload(
                refreshed["task"],
                latest_attempt=refreshed["attempt"],
                attempt_count=len(refreshed["detail"]["attempts"]),
            )
        return {
            "task": task_payload,
            "publication": await _routine_publication_payload(refreshed),
            "approval_required": True,
            "operator_action": "approve_exact_publication_in_pending_approvals",
        }
    except (BoardError, RoutineError) as exc:
        if isinstance(exc, BoardError):
            _raise_board_error(exc)
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.post("/tasks/{task_id}/routine-publication/recover")
async def recover_work_board_routine_publication(
    request: Request,
    task_id: str,
    body: WorkBoardRoutinePublicationRecoverRequest,
):
    """Consume the exact approved M3 publication and reconcile the same card."""

    operator = _operator(request)
    try:
        async with get_session() as db:
            context = await _routine_publication_context(
                db,
                operator,
                task_id,
                expected_revision=body.expected_revision,
            )
        if not _operator_has_external_mutation(operator):
            safely_blocked = await _block_routine_for_missing_external_authority(context)
            raise BoardError(
                "external_mutation_grant_required",
                "The current operator session has no external mutation grant; restore the grant before recovering this same run"
                if safely_blocked
                else "The current operator session has no external mutation grant; reconcile the linked run before retrying",
                status_code=403,
                recovery_action="restore_prerequisite",
            )
        if not context.get("m3_job_id") or not context.get("approval_id"):
            raise BoardError(
                "routine_publication_preview_required",
                "Prepare and inspect the exact publication preview before approval",
            )
        if context.get("approval_status") != "approved":
            raise BoardError(
                "approval_not_current",
                "Approve the exact publication preview in Pending approvals before resuming",
                recovery_action="approve_existing_run",
            )
        await dispatcher.resume_routine_attempt_for_operator_recovery(
            _owner(operator),
            context["task"],
            context["attempt"],
            context["parent"],
            expected_revision=body.expected_revision,
        )
        try:
            result = await routine_service.recover(
                context["routine_id"],
                context["parent_workflow_run_id"],
                owner_principal_id=operator.principal.principal_id,
                owner_session_id=operator.session_id,
                external_mutation_granted=True,
            )
        finally:
            # The routine service remains execution authority. The board
            # dispatcher projects the same fenced attempt only after the
            # current durable run exposes a verified independent readback;
            # otherwise it returns the card to a truthful recovery Blocked
            # state or leaves an actually running run under its live lease.
            await dispatcher.reconcile_linked_attempts()
        async with get_session() as db:
            detail = await repository.get_detail(db, _owner(operator), task_id)
            latest_attempt = detail["attempts"][0] if detail["attempts"] else None
            task_payload = await _safe_task_payload(
                detail["task"],
                dependency_counts=detail["dependency_counts"],
                latest_attempt=latest_attempt,
                attempt_count=len(detail["attempts"]),
            )
        publication = await _routine_publication_payload(
            {
                **context,
                "parent": {
                    **context["parent"],
                    "status": result.get("status") or context["parent"].get("status"),
                },
                "publication_response": result.get("child") if isinstance(result, Mapping) else None,
            }
        )
        return {
            "task": task_payload,
            "publication": publication,
            "recovery": result,
            "readback_required": True,
        }
    except (BoardError, RoutineError) as exc:
        if isinstance(exc, BoardError):
            _raise_board_error(exc)
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}) from exc
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
            task_payload = await _safe_task_payload(
                projection.task,
                latest_attempt=projection.attempt,
                attempt_count=1,
            )
            payload = {
                **_action_receipt_payload(
                    task_payload,
                    projection.event,
                    attempt_id=projection.attempt.attempt_id,
                ),
                "task": task_payload,
                "attempt": _attempt_payload(projection.attempt),
            }
            return payload
        if body.action.value == "retry":
            await dispatcher.validate_retry(
                owner,
                task_id,
                expected_revision=body.expected_revision,
            )
        async with get_session() as db:
            if body.action.value == "request_review":
                mutation = await review_service.request_review(
                    db,
                    owner,
                    task_id,
                    expected_revision=body.expected_revision,
                    attempt_id=body.attempt_id or "",
                    evidence_refs=body.evidence_refs,
                    repository=repository,
                )
            elif body.action.value == "request_changes":
                mutation = await review_service.request_changes(
                    db,
                    owner,
                    task_id,
                    expected_revision=body.expected_revision,
                    reason=body.reason or "",
                    repository=repository,
                )
            elif body.action.value == "complete_review":
                mutation = await review_service.complete_review(
                    db,
                    owner,
                    task_id,
                    expected_revision=body.expected_revision,
                    attempt_id=body.attempt_id or "",
                    repository=repository,
                )
            elif body.action.value == "renew_review":
                mutation = await review_service.renew_review(
                    db,
                    owner,
                    task_id,
                    expected_revision=body.expected_revision,
                    repository=repository,
                )
            elif body.action.value == "block" and body.block_kind not in WORK_BOARD_AUTHENTICATED_BLOCK_KINDS:
                # Workflow/effect categories are written only after the
                # authoritative runtime has reconciled them.  The
                # authenticated generic action may record only bounded board
                # recovery categories; repository.action_task repeats this
                # check for direct callers and performs the revision/source
                # status CAS.
                raise HTTPException(
                    status_code=422,
                    detail={"code": "invalid_block_kind"},
                )
            elif body.action.value == "unblock":
                mutation = await review_service.unblock_task(
                    db,
                    owner,
                    task_id,
                    expected_revision=body.expected_revision,
                    resolution=body.resolution or "",
                    repository=repository,
                )
            elif body.action.value == "retry":
                mutation = await repository.retry_task(
                    db,
                    owner,
                    task_id,
                    expected_revision=body.expected_revision,
                )
            else:
                mutation = await repository.action_task(db, owner, task_id, body)
            latest_attempt = (
                await db.execute(
                    select(WorkBoardAttempt)
                    .where(WorkBoardAttempt.task_id == mutation.task.task_id)
                    .order_by(
                        WorkBoardAttempt.created_at.desc(),
                        WorkBoardAttempt.attempt_id.desc(),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
        # Recovery guidance performs fresh provider-free authority checks.
        # Build this projection only after the mutation session commits so a
        # manual Block response cannot validate the pre-mutation Todo state
        # and incorrectly hide its new Unblock action.
        task_payload = await _safe_task_payload(
            mutation.task,
            latest_attempt=latest_attempt,
        )
        payload = {
            **_action_receipt_payload(
                task_payload,
                mutation.event,
                attempt_id=(latest_attempt.attempt_id if latest_attempt is not None else None),
            ),
            "task": task_payload,
        }
        return payload
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.post("/tasks/{task_id}/specify")
async def specify_work_board_task(
    request: Request,
    task_id: str,
    body: WorkBoardProposalRequest,
):
    operator = _operator(request)
    try:
        return await triage_service.create_proposal(
            _owner(operator),
            task_id,
            kind="specify",
            request=body,
            operator=operator,
        )
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.get("/tasks/{task_id}/proposals")
async def list_work_board_proposals(
    request: Request,
    task_id: str,
    kind: str | None = Query(default=None),
):
    operator = _operator(request)
    try:
        return {
            "proposals": await triage_service.list_proposals(
                _owner(operator),
                task_id,
                kind=kind,
            )
        }
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Retry after the workspace database is ready."},
        ) from exc


@router.get("/proposals/{proposal_id}")
async def get_work_board_proposal(request: Request, proposal_id: str):
    operator = _operator(request)
    try:
        return await triage_service.get_proposal(_owner(operator), proposal_id)
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Retry after the workspace database is ready."},
        ) from exc


@router.post("/tasks/{task_id}/decompose")
async def decompose_work_board_task(
    request: Request,
    task_id: str,
    body: WorkBoardProposalRequest,
):
    operator = _operator(request)
    try:
        return await triage_service.create_proposal(
            _owner(operator),
            task_id,
            kind="decompose",
            request=body,
            operator=operator,
        )
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.post("/proposals/{proposal_id}/accept")
async def accept_work_board_proposal(
    request: Request,
    proposal_id: str,
    body: WorkBoardProposalAccept,
):
    operator = _operator(request)
    try:
        return await triage_service.accept_proposal(_owner(operator), proposal_id, body)
    except BoardError as exc:
        _raise_board_error(exc)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "board_storage_unavailable", "recovery": "Check the local database readiness receipt and retry."},
        ) from exc


@router.post("/proposals/{proposal_id}/reject")
async def reject_work_board_proposal(
    request: Request,
    proposal_id: str,
    body: WorkBoardProposalReject,
):
    operator = _operator(request)
    try:
        return await triage_service.reject_proposal(_owner(operator), proposal_id, body)
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
