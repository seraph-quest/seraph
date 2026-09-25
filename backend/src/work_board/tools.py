"""Task-scoped worker controls for one fenced work-board attempt.

These controls are intentionally narrower than operator board actions.  A
worker may report progress or request a review, but it cannot create tasks,
add dependencies, unblock a task, attach a workflow run, or grant completion.
Every operation checks both the board lease and the authoritative workflow
lease before changing durable state.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from src.db.engine import get_session
from src.db.models import WorkBoardAttempt, WorkBoardStatus, WorkBoardTask
from src.work_board import review as review_service
from src.work_board.contracts import WorkBoardCommentCreate, WorkBoardOwner
from src.work_board.repository import (
    BoardError,
    BoardAttemptProjection,
    WorkBoardRepository,
    _begin_sqlite_immediate,
)
from src.work_board.time import serialize_utc_datetime
from src.workflows.job_runtime import durable_job_repository


def _text(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip()


class WorkBoardWorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    task_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(min_length=1, max_length=128)
    expected_task_revision: int = Field(ge=1)
    board_fencing_token: int = Field(ge=1)
    workflow_run_id: str = Field(min_length=1, max_length=256)
    workflow_fencing_token: int = Field(ge=1)


class WorkBoardWorkerComment(WorkBoardWorkerRequest):
    body: str = Field(min_length=1, max_length=2_000)


WorkerBlockKind = Literal[
    "capability",
    "needs_input",
    "transient",
    "unknown_effect",
    "cancelled",
]


class WorkBoardWorkerBlock(WorkBoardWorkerRequest):
    block_kind: WorkerBlockKind


class WorkBoardWorkerEvidence(WorkBoardWorkerRequest):
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("evidence_refs must be unique")
        for item in value:
            if (
                not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,256}", item)
                or item.startswith(("/", ".", "~"))
                or ".." in item
            ):
                raise ValueError("evidence_refs must contain safe evidence IDs")
        return value


class WorkBoardWorkerTools:
    """Authenticated, attempt-bound worker controls."""

    def __init__(
        self,
        *,
        repository: WorkBoardRepository | None = None,
        jobs: Any | None = None,
        session_provider: Any | None = None,
    ) -> None:
        self.repository = repository or WorkBoardRepository()
        self.jobs = jobs or durable_job_repository
        self.session_provider = session_provider or get_session

    @staticmethod
    def _lease_expiry_is_future(value: Any) -> bool:
        raw = value if isinstance(value, datetime) else _text(value)
        if not raw:
            return False
        try:
            expiry = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return False
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry > datetime.now(timezone.utc)

    @staticmethod
    def _principal_type(principal: Any) -> str:
        value = getattr(principal, "principal_type", None)
        return _text(getattr(value, "value", value)).lower()

    @staticmethod
    def _principal_grants(principal: Any) -> set[str]:
        return {
            _text(getattr(grant, "value", grant))
            for grant in (getattr(principal, "grants", ()) or ())
        }

    @staticmethod
    def _int_or_none(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return None

    async def validate_native_principal(
        self,
        request: WorkBoardWorkerRequest,
        principal: Any,
    ) -> None:
        """Validate the durable lineage before a native task tool runs.

        Native board controls are injected into the governed GoalSnapshot
        WorkflowTool.  The nested workflow run may carry a different durable
        job identity than the board root, so equality with
        ``request.workflow_run_id`` alone is insufficient.  The only allowed
        delegation is one running ``goal-snapshot-to-file`` child whose parent
        is the exact GoalSnapshot board child for this task/attempt.  A deeper
        or unrelated delegated run is rejected.
        """

        if principal is None:
            raise PermissionError("work-board worker authority requires an authenticated durable principal")
        if (
            self._principal_type(principal) != "service"
            or not bool(getattr(principal, "authenticated", False))
            or bool(getattr(principal, "revoked", False))
            or "capability_execute" not in self._principal_grants(principal)
        ):
            raise PermissionError("work-board worker authority requires a current service capability principal")
        if _text(getattr(principal, "session_id", None)) == "":
            raise PermissionError("work-board worker authority requires a session-bound principal")

        async with self.session_provider() as db:
            _owner, task, attempt, root = await self._bound(db, request)

        if _text(getattr(principal, "session_id", None)) != _text(task.owner_session_id):
            raise PermissionError("work-board worker principal session is mismatched")

        root_owner = root.get("owner") if isinstance(root.get("owner"), Mapping) else {}
        root_lease = root.get("lease") if isinstance(root.get("lease"), Mapping) else {}
        root_goal_revision = self._int_or_none(root.get("goal_revision"))
        expected_goal_revision = self._int_or_none(task.goal_revision)
        root_fence = self._int_or_none(root_lease.get("fencing_token"))
        expected_root_fence = self._int_or_none(request.workflow_fencing_token)
        if (
            _text(root.get("job_id") or root.get("run_identity")) != request.workflow_run_id
            or _text(root.get("root_run_identity")) != request.workflow_run_id
            or _text(root.get("job_kind")) != _text(task.capability_id)
            or _text(root_owner.get("kind")) != "service"
            or _text(root_owner.get("principal_id")) != "service:work-board"
            or _text(root_owner.get("service_id")) != "service:work-board"
            or _text(root.get("goal_id")) != _text(task.goal_id)
            or root_goal_revision is None
            or expected_goal_revision is None
            or root_goal_revision != expected_goal_revision
            or _text(root.get("session_id")) != _text(task.owner_session_id)
            or _text(root_lease.get("owner")) == ""
            or root_fence is None
            or expected_root_fence is None
            or root_fence != expected_root_fence
            or not self._lease_expiry_is_future(root_lease.get("expires_at"))
        ):
            raise PermissionError("work-board worker root lineage is stale or mismatched")

        principal_job_id = _text(getattr(principal, "job_id", None))
        if principal_job_id == request.workflow_run_id:
            if _text(getattr(principal, "principal_id", None)) != _text(root_owner.get("principal_id")):
                raise PermissionError("work-board worker root principal is mismatched")
            return

        if _text(task.capability_id) != "workflow.goal-snapshot-to-file":
            raise PermissionError("native work-board controls are unavailable to this capability")

        expected_child_id = f"goal-snapshot-work-board:{task.task_id}:{attempt.attempt_id}"
        child = await self.jobs.get_job(expected_child_id)
        nested = await self.jobs.get_job(principal_job_id) if principal_job_id else None
        if not isinstance(child, Mapping) or not isinstance(nested, Mapping):
            raise PermissionError("work-board worker child lineage is unavailable")
        child_owner = child.get("owner") if isinstance(child.get("owner"), Mapping) else {}
        child_lease = child.get("lease") if isinstance(child.get("lease"), Mapping) else {}
        child_authority = child.get("declared_authority") if isinstance(child.get("declared_authority"), Mapping) else {}
        child_fence = self._int_or_none(child_lease.get("fencing_token"))
        child_parent_fence = self._int_or_none(child.get("parent_fencing_token"))
        expected_child_goal_revision = self._int_or_none(child.get("goal_revision"))
        child_authority_goal_revision = self._int_or_none(child_authority.get("goal_revision"))
        if (
            _text(child.get("job_id") or child.get("run_identity")) != expected_child_id
            or _text(child.get("root_run_identity")) != request.workflow_run_id
            or _text(child.get("parent_run_identity")) != request.workflow_run_id
            or _text(child.get("parent_job_id")) != request.workflow_run_id
            or child_parent_fence is None
            or expected_root_fence is None
            or child_parent_fence != expected_root_fence
            or _text(child.get("status")) != "running"
            or _text(child_owner.get("kind")) != "service"
            or _text(child_owner.get("principal_id")) != "service:goal-snapshot"
            or _text(child_owner.get("service_id")) != "service:goal-snapshot"
            or _text(child.get("job_kind")) != "workflow.goal-snapshot-to-file"
            or _text(child.get("capability_version")) != "1"
            or _text(child.get("session_id")) != _text(task.owner_session_id)
            or _text(child.get("goal_id")) != _text(task.goal_id)
            or expected_child_goal_revision is None
            or expected_goal_revision is None
            or expected_child_goal_revision != expected_goal_revision
            or _text(child_authority.get("principal")) != "service:goal-snapshot"
            or _text(child_authority.get("owner_principal_id")) != "service:goal-snapshot"
            or _text(child_authority.get("service_id")) != "service:goal-snapshot"
            or _text(child_authority.get("session_id")) != _text(task.owner_session_id)
            or child_authority_goal_revision is None
            or expected_goal_revision is None
            or child_authority_goal_revision != expected_goal_revision
            or child_fence is None
            or child_fence < 1
            or _text(child_lease.get("owner")) == ""
            or not self._lease_expiry_is_future(child_lease.get("expires_at"))
        ):
            raise PermissionError("work-board worker GoalSnapshot child lineage is stale or mismatched")

        nested_owner = nested.get("owner") if isinstance(nested.get("owner"), Mapping) else {}
        nested_lease = nested.get("lease") if isinstance(nested.get("lease"), Mapping) else {}
        nested_authority = nested.get("declared_authority") if isinstance(nested.get("declared_authority"), Mapping) else {}
        nested_parent_fence = self._int_or_none(nested.get("parent_fencing_token"))
        nested_fence = self._int_or_none(nested_lease.get("fencing_token"))
        raw_nested_goal_revision = nested.get("goal_revision")
        nested_goal_revision = (
            None
            if raw_nested_goal_revision is None
            else self._int_or_none(raw_nested_goal_revision)
        )
        if (
            _text(nested.get("job_id") or nested.get("run_identity")) != principal_job_id
            or _text(nested.get("root_run_identity")) != request.workflow_run_id
            or _text(nested.get("parent_run_identity")) != expected_child_id
            or _text(nested.get("parent_job_id")) != expected_child_id
            or nested_parent_fence is None
            or child_fence is None
            or nested_parent_fence != child_fence
            or _text(nested.get("status")) != "running"
            or _text(nested_owner.get("kind")) != "service"
            or _text(nested_owner.get("principal_id")) != "service:goal-snapshot"
            or _text(nested_owner.get("service_id")) != "service:goal-snapshot"
            or _text(nested.get("job_kind")) != "goal-snapshot-to-file"
            or _text(nested.get("capability_version")) != "workflow-v2"
            or _text(nested.get("session_id")) != _text(task.owner_session_id)
            or (
                _text(nested.get("goal_id"))
                and _text(nested.get("goal_id")) != _text(task.goal_id)
            )
            or (
                raw_nested_goal_revision is not None
                and nested_goal_revision is None
            )
            or (
                nested_goal_revision is not None
                and expected_goal_revision is not None
                and nested_goal_revision != expected_goal_revision
            )
            or _text(nested_authority.get("principal")) != "service:goal-snapshot"
            or _text(nested_authority.get("owner_kind")) != "service"
            or _text(nested_authority.get("service_id")) != "service:goal-snapshot"
            or _text(nested_authority.get("session_id")) != _text(task.owner_session_id)
            or _text(nested_authority.get("capability")) != "workflow_goal_snapshot_to_file"
            or _text(nested_lease.get("owner")) == ""
            or nested_fence is None
            or nested_fence < 1
            or not self._lease_expiry_is_future(nested_lease.get("expires_at"))
        ):
            raise PermissionError("work-board worker nested workflow lineage is stale or delegated")

    async def _bound(
        self,
        db: Any,
        request: WorkBoardWorkerRequest,
    ) -> tuple[WorkBoardOwner, WorkBoardTask, WorkBoardAttempt, Mapping[str, Any]]:
        task = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == request.task_id))
        ).scalar_one_or_none()
        if task is None:
            raise BoardError("task_not_found", "The board task does not exist", status_code=404)
        owner = WorkBoardOwner(
            principal_id=task.owner_principal_id,
            session_id=task.owner_session_id,
        )
        if task.task_revision != request.expected_task_revision:
            raise BoardError("stale_revision", "The task revision is stale", status_code=409)
        if task.status is not WorkBoardStatus.running:
            raise BoardError("task_not_running", "Worker controls require a Running task", status_code=409)
        attempt = (
            await db.execute(
                select(WorkBoardAttempt).where(
                    WorkBoardAttempt.task_id == request.task_id,
                    WorkBoardAttempt.attempt_id == request.attempt_id,
                )
            )
        ).scalar_one_or_none()
        if attempt is None or attempt.ended_at is not None:
            raise BoardError("attempt_not_active", "The board attempt is no longer active", status_code=409)
        if (
            attempt.fencing_token != request.board_fencing_token
            or not attempt.lease_owner
            or attempt.executor_id != task.executor_id
            or not attempt.workflow_run_id
            or attempt.workflow_run_id != request.workflow_run_id
        ):
            raise BoardError("stale_fence", "The board attempt fence is stale", status_code=409)
        raw_board_expiry = attempt.lease_expires_at
        if raw_board_expiry is None:
            raise BoardError("stale_fence", "The board attempt lease is missing", status_code=409)
        if isinstance(raw_board_expiry, datetime):
            expiry = raw_board_expiry
        else:
            try:
                expiry = datetime.fromisoformat(str(raw_board_expiry).replace("Z", "+00:00"))
            except (TypeError, ValueError) as exc:
                raise BoardError("stale_fence", "The board attempt lease is malformed", status_code=409) from exc
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry <= datetime.now(timezone.utc):
            raise BoardError("stale_fence", "The board attempt lease has expired", status_code=409)
        workflow = await self.jobs.get_job(request.workflow_run_id)
        if not isinstance(workflow, Mapping):
            raise BoardError("workflow_run_not_found", "The authoritative workflow run does not exist", status_code=409)
        lease = workflow.get("lease") if isinstance(workflow.get("lease"), Mapping) else {}
        raw_expiry = lease.get("expires_at")
        if not _text(lease.get("owner")) or not isinstance(raw_expiry, str) or not raw_expiry.strip():
            raise BoardError("stale_workflow_fence", "The authoritative workflow lease is not active", status_code=409)
        try:
            workflow_expiry = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BoardError("stale_workflow_fence", "The authoritative workflow lease is malformed", status_code=409) from exc
        if workflow_expiry.tzinfo is None:
            workflow_expiry = workflow_expiry.replace(tzinfo=timezone.utc)
        if (
            _text(workflow.get("status")) != "running"
            or int(lease.get("fencing_token") or 0) != request.workflow_fencing_token
            or workflow_expiry <= datetime.now(timezone.utc)
        ):
            raise BoardError("stale_workflow_fence", "The workflow fence is stale", status_code=409)
        return owner, task, attempt, workflow

    async def show(self, request: WorkBoardWorkerRequest) -> dict[str, Any]:
        async with self.session_provider() as db:
            owner, task, attempt, workflow = await self._bound(db, request)
            detail = await self.repository.get_detail(db, owner, task.task_id)
            return {
                "task_id": task.task_id,
                "attempt_id": attempt.attempt_id,
                "task_revision": task.task_revision,
                "board_fencing_token": attempt.fencing_token,
                "workflow_run_id": attempt.workflow_run_id,
                "workflow_fencing_token": request.workflow_fencing_token,
                "status": task.status.value,
                "title": task.title,
                "goal_id": task.goal_id,
                "goal_revision": task.goal_revision,
                "attempt": {
                    "started_at": serialize_utc_datetime(attempt.started_at),
                    "heartbeat_at": serialize_utc_datetime(attempt.heartbeat_at),
                    "lease_expires_at": serialize_utc_datetime(attempt.lease_expires_at),
                    "outcome": attempt.outcome,
                },
                "workflow": {
                    "job_id": workflow.get("job_id"),
                    "status": workflow.get("status"),
                    "revision": workflow.get("revision"),
                },
                "dependency_count": len(detail.get("parents", [])),
                "parent_handoff_context": self._attempt_parent_handoffs(attempt),
                "parent_handoff_digest": attempt.parent_handoff_digest,
            }

    @staticmethod
    def _attempt_parent_handoffs(attempt: WorkBoardAttempt) -> list[dict[str, Any]]:
        """Expose only the immutable, digest-checked context captured at claim."""

        from src.work_board.dispatcher import WorkBoardDispatcher

        return WorkBoardDispatcher._attempt_parent_handoffs(attempt)

    async def heartbeat(self, request: WorkBoardWorkerRequest, *, lease_seconds: int = 300) -> dict[str, Any]:
        async with self.session_provider() as db:
            _owner, _task, attempt, _workflow = await self._bound(db, request)
            refreshed = await self.repository.heartbeat_attempt(
                db,
                request.task_id,
                request.attempt_id,
                expected_revision=request.expected_task_revision,
                board_fence=request.board_fencing_token,
                lease_owner=attempt.lease_owner or "",
                lease_seconds=lease_seconds,
            )
            return {
                "status": "ok",
                "task_id": request.task_id,
                "attempt_id": request.attempt_id,
                "lease_expires_at": serialize_utc_datetime(refreshed.lease_expires_at),
                "board_fencing_token": refreshed.fencing_token,
                "workflow_fencing_token": request.workflow_fencing_token,
            }

    async def comment(self, request: WorkBoardWorkerComment) -> dict[str, Any]:
        async with self.session_provider() as db:
            owner, _task, _attempt, _workflow = await self._bound(db, request)
            comment, event = await self.repository.add_comment(
                db,
                owner,
                request.task_id,
                WorkBoardCommentCreate(expected_revision=request.expected_task_revision, body=request.body),
            )
            return {
                "status": "commented",
                "comment_id": comment.comment_id,
                "event_id": event.event_id,
                "task_revision": request.expected_task_revision + 1,
            }

    async def block(self, request: WorkBoardWorkerBlock) -> BoardAttemptProjection:
        async with self.session_provider() as db:
            owner, task, attempt, workflow = await self._bound(db, request)
            workflow_status = _text(workflow.get("status"))
            if workflow_status == "succeeded":
                raise BoardError(
                    "workflow_already_completed",
                    "A worker cannot block a task after the authoritative run succeeded",
                    status_code=409,
                )
            if workflow_status != "blocked":
                lease = workflow.get("lease") if isinstance(workflow.get("lease"), Mapping) else {}
                workflow_owner = _text(lease.get("owner"))
                workflow_fence = int(lease.get("fencing_token") or 0)
                if not workflow_owner or workflow_fence != request.workflow_fencing_token:
                    raise BoardError(
                        "stale_workflow_fence",
                        "The authoritative workflow fence is stale",
                        status_code=409,
                    )
                try:
                    reconciled = await self.jobs.transition_job(
                        request.workflow_run_id,
                        "blocked",
                        owner=workflow_owner,
                        fencing_token=workflow_fence,
                        expected_revision=workflow.get("revision"),
                        reason=request.block_kind,
                        result_summary="worker requested bounded board recovery",
                    )
                except Exception as exc:
                    raise BoardError(
                        "workflow_reconcile_required",
                        "The authoritative workflow run could not be reconciled before blocking",
                        status_code=409,
                        reason_code="reconcile_external_effect",
                    ) from exc
                if not isinstance(reconciled, Mapping) or _text(reconciled.get("status")) != "blocked":
                    raise BoardError(
                        "workflow_reconcile_required",
                        "The authoritative workflow run did not enter a safe blocked state",
                        status_code=409,
                        reason_code="reconcile_external_effect",
                    )
            return await self.repository.project_attempt(
                db,
                task.task_id,
                request.attempt_id,
                expected_revision=request.expected_task_revision,
                board_fence=request.board_fencing_token,
                lease_owner=_text(getattr(attempt, "lease_owner", None)),
                status=WorkBoardStatus.blocked,
                outcome=request.block_kind,
                block_kind=request.block_kind,
                block_reason=request.block_kind,
                result_refs=[{"status": "blocked", "reason_code": request.block_kind}],
                receipt_refs=[{"status": "blocked", "reason_code": request.block_kind}],
                actor_principal_id=task.executor_id,
                actor_session_id=owner.session_id,
            )

    async def _evidence_comment(
        self,
        request: WorkBoardWorkerEvidence,
        *,
        kind: str,
    ) -> dict[str, Any]:
        async with self.session_provider() as db:
            owner, _task, attempt, _workflow = await self._bound(db, request)
            try:
                raw_receipts = json.loads(attempt.receipt_refs_json or "[]")
            except (TypeError, ValueError):
                raw_receipts = []
            known: set[str] = set()
            for item in raw_receipts if isinstance(raw_receipts, list) else []:
                if not isinstance(item, Mapping):
                    continue
                for key in (
                    "artifact_id",
                    "evidence_id",
                    "job_id",
                    "workflow_run_id",
                    "content_sha256",
                    "digest",
                ):
                    value = _text(item.get(key))
                    if value:
                        known.add(value)
                refs = item.get("evidence_refs")
                if isinstance(refs, list):
                    known.update(_text(ref) for ref in refs if _text(ref))
            if any(ref not in known for ref in request.evidence_refs):
                raise BoardError(
                    "evidence_ref_not_bound",
                    "Every evidence ID must be present in the current attempt receipt",
                    status_code=409,
                )
            body = json.dumps(
                {"kind": kind, "evidence_refs": list(request.evidence_refs)},
                separators=(",", ":"),
                sort_keys=True,
            )
            comment, event = await self.repository.add_comment(
                db,
                owner,
                request.task_id,
                WorkBoardCommentCreate(expected_revision=request.expected_task_revision, body=body),
            )
            return {
                "status": kind,
                "comment_id": comment.comment_id,
                "event_id": event.event_id,
                "task_revision": request.expected_task_revision + 1,
                "evidence_refs": list(request.evidence_refs),
                "projection": "dispatcher_verification_required",
            }

    async def request_review(self, request: WorkBoardWorkerEvidence) -> dict[str, Any]:
        """Persist an attempt/fence-bound review intent.

        The worker never changes the board phase and never supplies reviewer
        authority.  ``review_service`` records the durable intent while this
        server-bound attempt is still active; the dispatcher later verifies
        the authoritative run and readback before projecting Review.
        """

        async with self.session_provider() as db:
            # Serialize the worker's live binding read with dispatcher
            # projection.  review_service.request_review keeps this same
            # transaction open instead of committing and reopening between
            # the bound check and intent insert.
            await _begin_sqlite_immediate(db)
            owner, task, attempt, _workflow = await self._bound(db, request)
            mutation = await review_service.request_review(
                db,
                owner,
                task.task_id,
                expected_revision=request.expected_task_revision,
                attempt_id=attempt.attempt_id,
                evidence_refs=request.evidence_refs,
                repository=self.repository,
                transaction_locked=True,
            )
            return {
                "status": "review_requested",
                "task_id": task.task_id,
                "attempt_id": attempt.attempt_id,
                "workflow_run_id": attempt.workflow_run_id,
                "task_revision": mutation.task.task_revision,
                "event_id": mutation.event.event_id,
                "evidence_refs": list(request.evidence_refs),
                "projection": "dispatcher_verification_required",
                "idempotent_replay": mutation.idempotent_replay,
            }

    async def completion_request(self, request: WorkBoardWorkerEvidence) -> dict[str, Any]:
        """Record completion evidence request without granting Done."""

        return await self._evidence_comment(request, kind="completion_requested")


__all__ = [
    "WorkBoardWorkerBlock",
    "WorkBoardWorkerComment",
    "WorkBoardWorkerEvidence",
    "WorkBoardWorkerRequest",
    "WorkBoardWorkerTools",
]
