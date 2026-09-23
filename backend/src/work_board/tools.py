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
from src.work_board.contracts import WorkBoardCommentCreate, WorkBoardOwner
from src.work_board.repository import BoardError, BoardAttemptProjection, WorkBoardRepository
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
        if attempt.lease_expires_at is not None:
            expiry = attempt.lease_expires_at
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= datetime.now(timezone.utc):
                raise BoardError("stale_fence", "The board attempt lease has expired", status_code=409)
        workflow = await self.jobs.get_job(request.workflow_run_id)
        if not isinstance(workflow, Mapping):
            raise BoardError("workflow_run_not_found", "The authoritative workflow run does not exist", status_code=409)
        lease = workflow.get("lease") if isinstance(workflow.get("lease"), Mapping) else {}
        if (
            _text(workflow.get("status")) != "running"
            or int(lease.get("fencing_token") or 0) != request.workflow_fencing_token
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
                    "started_at": attempt.started_at.isoformat() if attempt.started_at else None,
                    "heartbeat_at": attempt.heartbeat_at.isoformat() if attempt.heartbeat_at else None,
                    "lease_expires_at": attempt.lease_expires_at.isoformat() if attempt.lease_expires_at else None,
                    "outcome": attempt.outcome,
                },
                "workflow": {
                    "job_id": workflow.get("job_id"),
                    "status": workflow.get("status"),
                    "revision": workflow.get("revision"),
                },
                "dependency_count": len(detail.get("parents", [])),
            }

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
                "lease_expires_at": refreshed.lease_expires_at.isoformat() if refreshed.lease_expires_at else None,
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
            owner, task, _attempt, _workflow = await self._bound(db, request)
            return await self.repository.project_attempt(
                db,
                task.task_id,
                request.attempt_id,
                expected_revision=request.expected_task_revision,
                board_fence=request.board_fencing_token,
                lease_owner=_text(getattr(_attempt, "lease_owner", None)),
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
        """Record a review request; dispatcher verification owns Review status."""

        return await self._evidence_comment(request, kind="review_requested")

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
