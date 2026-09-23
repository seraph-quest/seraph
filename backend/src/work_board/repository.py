"""Canonical SQLite repository for operator work-board state.

Every method receives the caller's SQLAlchemy session so task changes, their
append-only event, and any dependency/comment record share one transaction.
The repository never admits or mutates a durable workflow; M2 owns that
integration through the existing job runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.db.models import (
    Goal,
    WorkBoardAttempt,
    WorkBoardComment,
    WorkBoardEvent,
    WorkBoardLink,
    WorkBoardStatus,
    WorkBoardTask,
)
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


_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:/-]{1,512}$")
_SAFE_RECEIPT_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,512}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_EVENT_LIMIT = 100
_TASK_LIMIT = 100
_ALLOWED_RECEIPT_STATUSES = {
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
_AUTHORITY_RECONCILIATION_BLOCK_KINDS = frozenset(
    {"unknown_effect", "cost_liability", "reconcile_admission_binding"}
)


async def _begin_sqlite_immediate(db: AsyncSession) -> None:
    """Acquire SQLite's writer lock before a graph read/insert sequence.

    HTTP callers open a fresh session for a dependency mutation.  Focused
    repository callers may have flushed a task in the same session first; if
    that transaction has no pending ORM changes, commit that completed setup
    transaction before opening the serialized dependency transaction.  Never
    commit a caller's pending task mutation implicitly.
    """
    bind = db.get_bind()
    dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
    if dialect_name != "sqlite":
        return
    if db.in_transaction():
        if db.new or db.dirty or db.deleted:
            raise BoardError(
                "dependency_write_requires_transaction_boundary",
                "Dependency changes require a fresh transaction after pending task writes",
                status_code=409,
            )
        await db.commit()
    await db.execute(text("BEGIN IMMEDIATE"))


async def _begin_read_snapshot(db: AsyncSession) -> None:
    """Start one explicit read snapshot for task/count/cursor projection."""
    if db.in_transaction():
        return
    bind = db.get_bind()
    dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
    if dialect_name == "sqlite":
        await db.execute(text("BEGIN"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _payload_digest(request: WorkBoardTaskCreate) -> str:
    payload = request.model_dump(mode="json")
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_safe_identifier(value: str | None, *, field: str, max_length: int = 512) -> None:
    if value is None:
        return
    normalized = str(value).strip()
    if (
        not normalized
        or len(normalized) > max_length
        or not _SAFE_ID.fullmatch(normalized)
        or normalized.startswith(("/", "~"))
        or "\\" in normalized
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
    ):
        raise BoardError("invalid_reference", f"{field} must be a bounded safe reference")


def _validate_digest(value: str | None, *, field: str) -> None:
    if value is not None and not _DIGEST.fullmatch(str(value).lower()):
        raise BoardError("invalid_digest", f"{field} must be a SHA-256 hexadecimal digest")


def _safe_reference(value: Any, *, max_length: int = 512) -> str | None:
    """Return one bounded opaque identifier, or ``None`` when unsafe."""
    if not isinstance(value, str):
        return None
    bounded = value.strip()
    if (
        not bounded
        or len(bounded) > max_length
        or not _SAFE_ID.fullmatch(bounded)
        or bounded.startswith(("/", "~"))
        or "\\" in bounded
        or any(part in {"", ".", ".."} for part in bounded.split("/"))
    ):
        return None
    return bounded


def _safe_code(value: Any, *, max_length: int = 128) -> str | None:
    bounded = _safe_reference(value, max_length=max_length)
    if bounded is None or "/" in bounded or "\\" in bounded:
        return None
    return bounded


def safe_workflow_run_id(value: Any) -> str | None:
    """Return a safe durable-run reference for an operator projection."""
    reference = _safe_reference(value)
    if reference is None or "/" in reference or "\\" in reference:
        return None
    return reference


def safe_board_reference(value: Any, *, max_length: int = 512) -> str | None:
    """Return a bounded board reference without exposing path escapes."""
    return _safe_reference(value, max_length=max_length)


def safe_board_identifier(value: Any, *, max_length: int = 512) -> str | None:
    """Return a bounded opaque identifier; relative paths are not identifiers."""
    reference = safe_board_reference(value, max_length=max_length)
    if reference is None or "/" in reference or "\\" in reference:
        return None
    return reference


def safe_sha256_digest(value: Any) -> str | None:
    """Return only a canonical SHA-256 digest for operator projections."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if _DIGEST.fullmatch(normalized) else None


def _safe_receipt_refs(value: Any, *, limit: int = 32) -> list[dict[str, Any]]:
    """Keep only bounded, structured receipt references in board projections."""
    values = value if isinstance(value, (list, tuple)) else [value]
    allowed = {
        "artifact_id",
        "artifact_type",
        "file_path",
        "content_sha256",
        "size_bytes",
        "exists",
        "effect_id",
        "effect_type",
        "status",
        "verified",
        "target_digest",
        "target_path",
        "job_id",
        "workflow_run_id",
        "recovery_action",
        "reason_code",
        "error_code",
        "child_job_id",
        "readback_status",
        "verification_status",
        "outcome",
    }
    safe_items: list[dict[str, Any]] = []
    for item in values[:limit]:
        if not isinstance(item, Mapping):
            continue
        safe: dict[str, Any] = {}
        for key in allowed:
            if key not in item:
                continue
            candidate = item[key]
            if isinstance(candidate, bool) or candidate is None:
                safe[key] = candidate
            elif isinstance(candidate, int):
                safe[key] = candidate
            elif isinstance(candidate, str):
                bounded = candidate.strip()
                if key in {"content_sha256", "target_digest"}:
                    if not _DIGEST.fullmatch(bounded.lower()):
                        continue
                    safe[key] = bounded.lower()
                elif key in {
                    "artifact_id",
                    "artifact_type",
                    "effect_id",
                    "effect_type",
                    "status",
                    "job_id",
                    "workflow_run_id",
                    "child_job_id",
                    "recovery_action",
                    "reason_code",
                    "error_code",
                    "readback_status",
                    "verification_status",
                    "outcome",
                }:
                    identifier_pattern = (
                        _SAFE_RECEIPT_IDENTIFIER
                        if key
                        in {
                            "artifact_id",
                            "artifact_type",
                            "effect_id",
                            "effect_type",
                            "job_id",
                            "child_job_id",
                        }
                        else _SAFE_ID
                    )
                    if len(bounded) > 512 or not identifier_pattern.fullmatch(bounded):
                        continue
                    if key == "status" and bounded not in _ALLOWED_RECEIPT_STATUSES:
                        continue
                    if key == "readback_status" and bounded not in {
                        "not_started", "pending", "verified", "failed", "unknown", "not_applicable"
                    }:
                        continue
                    if key == "verification_status" and bounded not in {
                        "not_started", "pending", "passed", "failed", "reconciliation_required", "cancelled"
                    }:
                        continue
                    if key == "workflow_run_id":
                        bounded = safe_workflow_run_id(bounded) or ""
                        if not bounded:
                            continue
                    if key in {"recovery_action", "reason_code", "error_code", "outcome"}:
                        bounded = _safe_code(bounded) or ""
                        if not bounded:
                            continue
                    safe[key] = bounded
                elif key in {"file_path", "target_path"}:
                    if (
                        not bounded
                        or bounded.startswith(("/", "~"))
                        or "\\" in bounded
                        or any(part in {"", ".", ".."} for part in bounded.split("/"))
                        or len(bounded) > 512
                    ):
                        continue
                    safe[key] = bounded
        if safe:
            safe_items.append(safe)
    return safe_items


def _safe_metadata(metadata: dict[str, Any]) -> str:
    """Keep event metadata to redacted scalar identifiers and bounded values."""

    safe: dict[str, Any] = {}
    for key, value in metadata.items():
        normalized_key = str(key).strip()[:64]
        if not normalized_key or normalized_key.lower() in {
            "body",
            "content",
            "prompt",
            "secret",
            "token",
            "credential",
            "raw",
        }:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, str):
                value = value[:256]
            safe[normalized_key] = value
        elif isinstance(value, (list, tuple)):
            safe[normalized_key] = [str(item)[:128] for item in value[:16]]
    return _canonical_json(safe)


class BoardError(Exception):
    """Typed repository failure mapped to a stable API error code."""

    def __init__(self, code: str, message: str, *, status_code: int = 409, **extra: Any):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.extra = extra
        super().__init__(message)


class BoardNotFound(BoardError):
    def __init__(self, task_id: str):
        super().__init__("task_not_found", "The requested board task does not exist", status_code=404, task_id=task_id)


class BoardOwnerMismatch(BoardError):
    def __init__(self, task_id: str):
        super().__init__("task_owner_mismatch", "The task is owned by another operator session", status_code=403, task_id=task_id)


class BoardRevisionConflict(BoardError):
    def __init__(self, task_id: str, expected: int, actual: int):
        super().__init__(
            "stale_revision",
            "The task changed before this mutation was applied",
            status_code=409,
            task_id=task_id,
            expected_revision=expected,
            current_revision=actual,
        )


class BoardIdempotencyConflict(BoardError):
    def __init__(self, scope: str, key: str):
        super().__init__(
            "idempotency_conflict",
            "The idempotency key was already used with a different task payload",
            status_code=409,
            idempotency_scope=scope,
            idempotency_key=key,
        )


class BoardGoalNotFound(BoardError):
    def __init__(self, goal_id: str):
        super().__init__(
            "goal_not_found",
            "The task goal does not exist",
            status_code=404,
            goal_id=goal_id,
        )


class BoardGoalOwnerMismatch(BoardError):
    def __init__(self, goal_id: str, *, unbound: bool = False):
        super().__init__(
            "goal_owner_unbound" if unbound else "goal_owner_mismatch",
            "The task goal is not owned by this operator session",
            status_code=403,
            goal_id=goal_id,
        )


class BoardGoalRevisionConflict(BoardError):
    def __init__(self, goal_id: str, expected: int, actual: int):
        super().__init__(
            "stale_goal_revision",
            "The task goal changed before this board task was admitted",
            status_code=409,
            goal_id=goal_id,
            expected_goal_revision=expected,
            current_goal_revision=actual,
        )


@dataclass(frozen=True)
class BoardMutation:
    task: WorkBoardTask
    event: WorkBoardEvent
    idempotent_replay: bool = False


@dataclass(frozen=True)
class BoardPage:
    tasks: list[WorkBoardTask]
    next_after: int | None
    last_event_id: int
    dependency_counts: dict[str, tuple[int, int]]


@dataclass(frozen=True)
class BoardEventPage:
    events: list[WorkBoardEvent]
    last_event_id: int
    gap: bool


class WorkBoardRepository:
    """Single kernel for all M1 task, dependency, comment, and event writes."""

    async def _find_task(self, db: AsyncSession, task_id: str) -> WorkBoardTask | None:
        result = await db.execute(
            select(WorkBoardTask).where(WorkBoardTask.task_id == str(task_id).strip())
        )
        return result.scalar_one_or_none()

    async def _owned_task(
        self,
        db: AsyncSession,
        owner: WorkBoardOwner,
        task_id: str,
    ) -> WorkBoardTask:
        task = await self._find_task(db, task_id)
        if task is None:
            raise BoardNotFound(task_id)
        if (
            task.owner_principal_id != owner.principal_id
            or task.owner_session_id != owner.session_id
        ):
            raise BoardOwnerMismatch(task_id)
        return task

    @staticmethod
    async def _safe_text(value: str) -> str:
        """Persist only vault-redacted bounded operator text.

        The redaction helper returns a fixed placeholder when the vault cannot
        be read with ``fail_closed=True``.  That keeps a database or API error
        from becoming a secret disclosure through a task card.
        """
        return await vault_redaction.redact_secrets_in_text(value, fail_closed=True)

    @staticmethod
    async def _validate_goal(
        db: AsyncSession,
        owner: WorkBoardOwner,
        *,
        goal_id: str,
        goal_revision: int,
    ) -> Goal:
        result = await db.execute(select(Goal).where(Goal.id == goal_id))
        goal = result.scalar_one_or_none()
        if goal is None:
            raise BoardGoalNotFound(goal_id)
        goal_owner = str(getattr(goal, "owner_principal_id", "") or "").strip()
        goal_session = str(getattr(goal, "owner_session_id", "") or "").strip()
        if not goal_owner or not goal_session:
            raise BoardGoalOwnerMismatch(goal_id, unbound=True)
        if goal_owner != owner.principal_id or goal_session != owner.session_id:
            raise BoardGoalOwnerMismatch(goal_id)
        current_revision = max(int(getattr(goal, "revision", 1) or 1), 1)
        if current_revision != int(goal_revision):
            raise BoardGoalRevisionConflict(goal_id, int(goal_revision), current_revision)
        return goal

    async def validate_task_goal(
        self,
        db: AsyncSession,
        owner: WorkBoardOwner,
        task: WorkBoardTask,
    ) -> Goal:
        """Revalidate a task's owner-bound goal before a Ready admission."""
        return await self._validate_goal(
            db,
            owner,
            goal_id=task.goal_id,
            goal_revision=task.goal_revision,
        )

    async def _cas_task_update(
        self,
        db: AsyncSession,
        owner: WorkBoardOwner,
        task: WorkBoardTask,
        *,
        expected_revision: int,
        values: dict[str, Any],
    ) -> None:
        """Apply one task mutation only if the loaded revision still matches."""
        result = await db.execute(
            update(WorkBoardTask)
            .where(
                WorkBoardTask.task_id == task.task_id,
                WorkBoardTask.owner_principal_id == owner.principal_id,
                WorkBoardTask.owner_session_id == owner.session_id,
                WorkBoardTask.task_revision == expected_revision,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if int(result.rowcount or 0) == 1:
            await db.refresh(task)
            return
        current = await self._owned_task(db, owner, task.task_id)
        raise BoardRevisionConflict(task.task_id, expected_revision, current.task_revision)

    @staticmethod
    async def _event(
        db: AsyncSession,
        task: WorkBoardTask,
        owner: WorkBoardOwner,
        *,
        kind: str,
        metadata: dict[str, Any],
        actor_principal_id: str | None = None,
        actor_session_id: str | None = None,
    ) -> WorkBoardEvent:
        event = WorkBoardEvent(
            task_id=task.task_id,
            owner_principal_id=task.owner_principal_id,
            owner_session_id=task.owner_session_id,
            actor_principal_id=actor_principal_id or owner.principal_id,
            actor_session_id=actor_session_id or owner.session_id,
            kind=kind,
            metadata_json=_safe_metadata(metadata),
        )
        db.add(event)
        await db.flush()
        return event

    @staticmethod
    def _validate_task_fields(request: WorkBoardTaskCreate) -> None:
        _validate_safe_identifier(request.goal_id, field="goal_id", max_length=128)
        _validate_safe_identifier(request.capability_id, field="capability_id", max_length=128)
        _validate_safe_identifier(request.typed_input_ref, field="typed_input_ref")
        _validate_safe_identifier(request.executor_id, field="executor_id", max_length=128)
        _validate_safe_identifier(request.assignee_id, field="assignee_id", max_length=128)
        _validate_safe_identifier(request.idempotency_scope, field="idempotency_scope", max_length=128)
        _validate_safe_identifier(request.idempotency_key, field="idempotency_key", max_length=256)
        _validate_safe_identifier(request.reviewer_id, field="reviewer_id", max_length=128)
        _validate_safe_identifier(request.origin_thread_id, field="origin_thread_id", max_length=256)
        _validate_digest(request.typed_input_digest, field="typed_input_digest")

    async def create_task(
        self,
        db: AsyncSession,
        owner: WorkBoardOwner,
        request: WorkBoardTaskCreate,
        *,
        origin_session_id: str | None = None,
    ) -> BoardMutation:
        self._validate_task_fields(request)
        digest = _payload_digest(request)
        existing_result = await db.execute(
            select(WorkBoardTask).where(
                WorkBoardTask.owner_principal_id == owner.principal_id,
                WorkBoardTask.owner_session_id == owner.session_id,
                WorkBoardTask.idempotency_scope == request.idempotency_scope,
                WorkBoardTask.idempotency_key == request.idempotency_key,
            )
        )
        existing = existing_result.scalar_one_or_none()
        if existing is not None:
            if existing.idempotency_payload_digest != digest:
                raise BoardIdempotencyConflict(request.idempotency_scope, request.idempotency_key)
            latest_event = await db.execute(
                select(WorkBoardEvent)
                .where(
                    WorkBoardEvent.task_id == existing.task_id,
                    WorkBoardEvent.owner_principal_id == owner.principal_id,
                    WorkBoardEvent.owner_session_id == owner.session_id,
                )
                .order_by(WorkBoardEvent.event_id.desc())
                .limit(1)
            )
            event = latest_event.scalar_one_or_none()
            if event is None:
                event = await self._event(
                    db,
                    existing,
                    owner,
                    kind="task.replayed",
                    metadata={"status": existing.status.value, "task_revision": existing.task_revision},
                )
            return BoardMutation(existing, event, idempotent_replay=True)

        await self._validate_goal(
            db,
            owner,
            goal_id=request.goal_id,
            goal_revision=request.goal_revision,
        )
        safe_title = await self._safe_text(request.title)
        safe_body = await self._safe_text(request.body)

        task = WorkBoardTask(
            owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,
            origin_session_id=origin_session_id or owner.session_id,
            origin_thread_id=request.origin_thread_id,
            goal_id=request.goal_id,
            goal_revision=request.goal_revision,
            title=safe_title,
            body=safe_body,
            capability_id=request.capability_id,
            typed_input_ref=request.typed_input_ref,
            typed_input_digest=request.typed_input_digest,
            executor_id=request.executor_id,
            assignee_id=request.assignee_id,
            priority=request.priority,
            idempotency_scope=request.idempotency_scope,
            idempotency_key=request.idempotency_key,
            idempotency_payload_digest=digest,
            scheduled_at=request.scheduled_at,
            status=request.status,
            requires_review=request.requires_review,
            reviewer_id=request.reviewer_id,
        )
        try:
            # Keep a concurrent unique-key loser usable for the winner
            # refetch.  The savepoint contains only this insert.
            async with db.begin_nested():
                db.add(task)
                await db.flush()
        except IntegrityError as exc:
            # A concurrent request can win the unique idempotency index after
            # the preflight query.  Return that canonical task when the owner,
            # session, and key match exactly; unrelated integrity failures stay
            # typed conflicts.
            winner_result = await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.owner_principal_id == owner.principal_id,
                    WorkBoardTask.owner_session_id == owner.session_id,
                    WorkBoardTask.idempotency_scope == request.idempotency_scope,
                    WorkBoardTask.idempotency_key == request.idempotency_key,
                )
            )
            winner = winner_result.scalar_one_or_none()
            if winner is not None:
                if winner.idempotency_payload_digest != digest:
                    raise BoardIdempotencyConflict(
                        request.idempotency_scope,
                        request.idempotency_key,
                    ) from exc
                latest_event = await db.execute(
                    select(WorkBoardEvent)
                    .where(
                        WorkBoardEvent.task_id == winner.task_id,
                        WorkBoardEvent.owner_principal_id == owner.principal_id,
                        WorkBoardEvent.owner_session_id == owner.session_id,
                    )
                    .order_by(WorkBoardEvent.event_id.desc())
                    .limit(1)
                )
                event = latest_event.scalar_one_or_none()
                if event is None:
                    event = await self._event(
                        db,
                        winner,
                        owner,
                        kind="task.replayed",
                        metadata={
                            "status": winner.status.value,
                            "task_revision": winner.task_revision,
                        },
                    )
                return BoardMutation(winner, event, idempotent_replay=True)
            raise BoardError(
                "integrity_conflict",
                "The task could not be persisted because another record conflicts with it",
                status_code=409,
            ) from exc
        event = await self._event(
            db,
            task,
            owner,
            kind="task.created",
            metadata={"status": task.status.value, "task_revision": task.task_revision},
        )
        return BoardMutation(task, event)

    async def get_task(self, db: AsyncSession, owner: WorkBoardOwner, task_id: str) -> WorkBoardTask:
        return await self._owned_task(db, owner, task_id)

    async def list_tasks(
        self,
        db: AsyncSession,
        owner: WorkBoardOwner,
        *,
        status: WorkBoardStatus | None = None,
        executor_id: str | None = None,
        assignee_id: str | None = None,
        query: str | None = None,
        after: int | None = None,
        limit: int = _TASK_LIMIT,
    ) -> BoardPage:
        # The task rows, dependency counts, and event cursor must all come
        # from one SQLite snapshot.  API callers use a fresh session; an
        # already-active caller transaction is already the authoritative
        # snapshot and is deliberately left intact.
        await _begin_read_snapshot(db)
        limit = max(1, min(int(limit), _TASK_LIMIT))
        statement = select(WorkBoardTask).where(
            WorkBoardTask.owner_principal_id == owner.principal_id,
            WorkBoardTask.owner_session_id == owner.session_id,
        )
        if status is not None:
            statement = statement.where(WorkBoardTask.status == status)
        if executor_id:
            _validate_safe_identifier(executor_id, field="executor_id", max_length=128)
            statement = statement.where(WorkBoardTask.executor_id == executor_id)
        if assignee_id:
            _validate_safe_identifier(assignee_id, field="assignee_id", max_length=128)
            statement = statement.where(WorkBoardTask.assignee_id == assignee_id)
        if query:
            bounded_query = str(query).strip()[:200]
            statement = statement.where(
                WorkBoardTask.title.contains(bounded_query)
                | WorkBoardTask.body.contains(bounded_query)
            )
        if after is not None:
            if after < 0:
                raise BoardError("invalid_cursor", "after must be non-negative", status_code=400)
            statement = statement.where(WorkBoardTask.creation_sequence > after)
        statement = statement.order_by(WorkBoardTask.creation_sequence.asc()).limit(limit + 1)
        rows = list((await db.execute(statement)).scalars().all())
        next_after = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_after = rows[-1].creation_sequence
        dependency_counts: dict[str, tuple[int, int]] = {
            task.task_id: (0, 0) for task in rows
        }
        if rows:
            task_ids = [task.task_id for task in rows]
            dependency_result = await db.execute(
                select(WorkBoardLink.child_task_id, WorkBoardTask.status)
                .join(
                    WorkBoardTask,
                    WorkBoardTask.task_id == WorkBoardLink.parent_task_id,
                )
                .where(
                    WorkBoardLink.child_task_id.in_(task_ids),
                    WorkBoardLink.owner_principal_id == owner.principal_id,
                    WorkBoardLink.owner_session_id == owner.session_id,
                    WorkBoardTask.owner_principal_id == owner.principal_id,
                    WorkBoardTask.owner_session_id == owner.session_id,
                )
            )
            totals: dict[str, int] = {}
            completed: dict[str, int] = {}
            for child_task_id, parent_status in dependency_result.all():
                child_key = str(child_task_id)
                totals[child_key] = totals.get(child_key, 0) + 1
                if parent_status == WorkBoardStatus.done:
                    completed[child_key] = completed.get(child_key, 0) + 1
            dependency_counts = {
                task.task_id: (totals.get(task.task_id, 0), completed.get(task.task_id, 0))
                for task in rows
            }
        last_event_id = int(
            await db.scalar(
                select(func.max(WorkBoardEvent.event_id)).where(
                    WorkBoardEvent.owner_principal_id == owner.principal_id,
                    WorkBoardEvent.owner_session_id == owner.session_id,
                )
            )
            or 0
        )
        return BoardPage(rows, next_after, last_event_id, dependency_counts)

    async def patch_task(
        self,
        db: AsyncSession,
        owner: WorkBoardOwner,
        task_id: str,
        request: WorkBoardTaskPatch,
    ) -> BoardMutation:
        task = await self._owned_task(db, owner, task_id)
        expected = request.expected_revision
        if task.task_revision != expected:
            raise BoardRevisionConflict(task.task_id, expected, task.task_revision)
        if task.status in {
            WorkBoardStatus.running,
            WorkBoardStatus.review,
            WorkBoardStatus.done,
            WorkBoardStatus.archived,
        }:
            raise BoardError("edit_not_allowed_in_status", "This task status cannot be edited", status_code=409)
        changes = request.model_dump(exclude_unset=True, exclude={"expected_revision"})
        if not changes:
            raise BoardError("empty_mutation", "At least one task field must change", status_code=400)
        safe_changes: dict[str, Any] = {}
        for field, value in changes.items():
            if field in {"title", "body"}:
                safe_changes[field] = await self._safe_text(str(value))
            else:
                if field in {"capability_id", "typed_input_ref", "executor_id", "assignee_id"}:
                    _validate_safe_identifier(
                        value,
                        field=field,
                        max_length=128 if field in {"capability_id", "executor_id", "assignee_id"} else 512,
                    )
                elif field == "typed_input_digest":
                    _validate_digest(value, field=field)
                safe_changes[field] = value
        typed_fields = {"typed_input_ref", "typed_input_digest"}
        changed_typed_fields = typed_fields.intersection(safe_changes)
        if changed_typed_fields and changed_typed_fields != typed_fields:
            raise BoardError(
                "typed_spec_required",
                "Typed input reference and digest must be changed together",
                status_code=422,
            )
        if (
            changed_typed_fields == typed_fields
            and safe_changes["typed_input_ref"] is None
            and safe_changes["typed_input_digest"] is None
            and (
                task.status in {WorkBoardStatus.todo, WorkBoardStatus.ready}
                or task.block_source_status in {
                    WorkBoardStatus.todo.value,
                    WorkBoardStatus.ready.value,
                }
            )
        ):
            raise BoardError(
                "typed_spec_required",
                "Todo and Ready tasks cannot lose their complete typed specification",
                status_code=422,
            )
        authority_fields = {
            "capability_id",
            "typed_input_ref",
            "typed_input_digest",
            "executor_id",
            "assignee_id",
            "scheduled_at",
        }
        if (
            authority_fields.intersection(safe_changes)
            and task.status is WorkBoardStatus.blocked
            and task.block_kind in _AUTHORITY_RECONCILIATION_BLOCK_KINDS
        ):
            raise BoardError(
                "typed_reconcile_required",
                "Execution authority cannot change until the blocked attempt is reconciled",
                status_code=409,
            )
        if authority_fields.intersection(safe_changes) and task.status is WorkBoardStatus.ready:
            safe_changes["status"] = WorkBoardStatus.todo
        safe_changes["task_revision"] = expected + 1
        safe_changes["updated_at"] = _now()
        await self._cas_task_update(
            db,
            owner,
            task,
            expected_revision=expected,
            values=safe_changes,
        )
        event = await self._event(
            db,
            task,
            owner,
            kind="task.updated",
            metadata={
                "status": task.status.value,
                "task_revision": task.task_revision,
                "changed_fields": sorted(safe_changes),
            },
        )
        return BoardMutation(task, event)

    async def action_task(
        self,
        db: AsyncSession,
        owner: WorkBoardOwner,
        task_id: str,
        request: WorkBoardActionRequest,
    ) -> BoardMutation:
        task = await self._owned_task(db, owner, task_id)
        expected = request.expected_revision
        if task.task_revision != expected:
            raise BoardRevisionConflict(task.task_id, expected, task.task_revision)
        previous = task.status
        values: dict[str, Any] = {}
        if request.action.value == "promote":
            await self.validate_task_goal(db, owner, task)
            if task.status is WorkBoardStatus.triage:
                if not task.typed_input_ref or not task.typed_input_digest:
                    raise BoardError(
                        "typed_spec_required",
                        "Triage must have a complete typed specification before promotion",
                    )
                values["status"] = WorkBoardStatus.todo
            elif task.status is WorkBoardStatus.todo:
                raise BoardError(
                    "dispatcher_readiness_required",
                    "Todo becomes Ready only after the governed dispatcher verifies capability authority",
                )
            else:
                raise BoardError("illegal_transition", "This task cannot be promoted from its current status")
        elif request.action.value == "block":
            if request.block_kind not in {None, "operator"}:
                raise BoardError(
                    "invalid_block_kind",
                    "Manual board blocking accepts only the operator block kind",
                    status_code=422,
                )
            if task.status not in {
                WorkBoardStatus.triage,
                WorkBoardStatus.todo,
                WorkBoardStatus.ready,
                WorkBoardStatus.review,
            }:
                raise BoardError("illegal_transition", "This task cannot be manually blocked from its current status")
            values.update(
                {
                    "block_source_status": task.status.value,
                    "block_kind": request.block_kind or "operator",
                    "block_reason": await self._safe_text(request.reason or ""),
                    "status": WorkBoardStatus.blocked,
                }
            )
        elif request.action.value == "unblock":
            if task.status is not WorkBoardStatus.blocked:
                raise BoardError("illegal_transition", "Only blocked tasks can be unblocked")
            await self.validate_task_goal(db, owner, task)
            if task.block_kind != "operator":
                raise BoardError(
                    "typed_reconcile_required",
                    "This block requires its typed recovery path before it can be unblocked",
                    status_code=409,
                )
            attempt_result = await db.execute(
                select(WorkBoardAttempt.attempt_id)
                .where(WorkBoardAttempt.task_id == task.task_id)
                .limit(1)
            )
            if attempt_result.scalar_one_or_none() is not None:
                raise BoardError(
                    "attempt_reconcile_required",
                    "A task with an execution attempt requires typed recovery before unblock",
                    status_code=409,
                )
            source = task.block_source_status
            values.update(
                {
                    "status": (
                        WorkBoardStatus.triage
                        if source == WorkBoardStatus.triage.value
                        else WorkBoardStatus.todo
                    ),
                    "block_source_status": None,
                    "block_kind": None,
                    "block_reason": None,
                }
            )
        elif request.action.value == "archive":
            if task.status is not WorkBoardStatus.done:
                raise BoardError("illegal_transition", "Only Done tasks can be archived")
            values.update({"status": WorkBoardStatus.archived, "archived_at": _now()})
        else:  # pragma: no cover - Pydantic constrains the enum
            raise BoardError("invalid_action", "Unsupported board action", status_code=400)

        values["task_revision"] = expected + 1
        values["updated_at"] = _now()
        await self._cas_task_update(
            db,
            owner,
            task,
            expected_revision=expected,
            values=values,
        )
        event = await self._event(
            db,
            task,
            owner,
            kind=f"task.{request.action.value}",
            metadata={
                "from_status": previous.value,
                "status": task.status.value,
                "task_revision": task.task_revision,
                "block_kind": task.block_kind,
            },
        )
        return BoardMutation(task, event)

    async def add_comment(
        self,
        db: AsyncSession,
        owner: WorkBoardOwner,
        task_id: str,
        request: WorkBoardCommentCreate,
    ) -> tuple[WorkBoardComment, WorkBoardEvent]:
        task = await self._owned_task(db, owner, task_id)
        expected = request.expected_revision
        if task.task_revision != expected:
            raise BoardRevisionConflict(task.task_id, expected, task.task_revision)
        safe_body = await self._safe_text(request.body)
        await self._cas_task_update(
            db,
            owner,
            task,
            expected_revision=expected,
            values={"task_revision": expected + 1, "updated_at": _now()},
        )
        comment = WorkBoardComment(
            task_id=task.task_id,
            owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,
            author_principal_id=owner.principal_id,
            author_session_id=owner.session_id,
            body=safe_body,
        )
        db.add(comment)
        await db.flush()
        event = await self._event(
            db,
            task,
            owner,
            kind="comment.created",
            metadata={"comment_id": comment.comment_id, "body_digest": _text_digest(safe_body)},
        )
        return comment, event

    async def _would_cycle(
        self,
        db: AsyncSession,
        *,
        owner: WorkBoardOwner,
        parent_task_id: str,
        child_task_id: str,
    ) -> bool:
        frontier = [child_task_id]
        visited: set[str] = set()
        while frontier:
            current = frontier.pop()
            if current in visited:
                continue
            visited.add(current)
            if current == parent_task_id:
                return True
            child_task = WorkBoardTask
            parent_task = aliased(WorkBoardTask)
            result = await db.execute(
                select(WorkBoardLink.child_task_id)
                .join(child_task, child_task.task_id == WorkBoardLink.child_task_id)
                .join(parent_task, parent_task.task_id == WorkBoardLink.parent_task_id)
                .where(
                    WorkBoardLink.parent_task_id == current,
                    WorkBoardLink.owner_principal_id == owner.principal_id,
                    WorkBoardLink.owner_session_id == owner.session_id,
                    child_task.owner_principal_id == owner.principal_id,
                    child_task.owner_session_id == owner.session_id,
                    parent_task.owner_principal_id == owner.principal_id,
                    parent_task.owner_session_id == owner.session_id,
                )
            )
            frontier.extend(str(value) for value in result.scalars().all())
        return False

    async def add_link(
        self,
        db: AsyncSession,
        owner: WorkBoardOwner,
        request: WorkBoardLinkCreate,
    ) -> tuple[WorkBoardLink, WorkBoardEvent]:
        if request.parent_task_id == request.child_task_id:
            raise BoardError("dependency_cycle", "A task cannot depend on itself")
        # The cycle check and link insert are one cross-process critical
        # section.  This must happen before the first graph read.
        await _begin_sqlite_immediate(db)
        parent = await self._owned_task(db, owner, request.parent_task_id)
        child = await self._owned_task(db, owner, request.child_task_id)
        if child.task_revision != request.expected_child_revision:
            raise BoardRevisionConflict(child.task_id, request.expected_child_revision, child.task_revision)
        if child.status is WorkBoardStatus.running:
            raise BoardError("running_task_dependency", "A blocking parent cannot be added to a running task")
        duplicate = await db.execute(
            select(WorkBoardLink).where(
                WorkBoardLink.parent_task_id == parent.task_id,
                WorkBoardLink.child_task_id == child.task_id,
            )
        )
        if duplicate.scalar_one_or_none() is not None:
            raise BoardError("link_exists", "The dependency link already exists")
        if await self._would_cycle(
            db,
            owner=owner,
            parent_task_id=parent.task_id,
            child_task_id=child.task_id,
        ):
            raise BoardError("dependency_cycle", "The dependency would create a cycle")
        demote_ready = (
            child.status is WorkBoardStatus.ready
            and parent.status is not WorkBoardStatus.done
        )
        link = WorkBoardLink(
            owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,
            parent_task_id=parent.task_id,
            child_task_id=child.task_id,
        )
        values: dict[str, Any] = {
            "task_revision": request.expected_child_revision + 1,
            "updated_at": _now(),
        }
        if demote_ready:
            values["status"] = WorkBoardStatus.todo
        await self._cas_task_update(
            db,
            owner,
            child,
            expected_revision=request.expected_child_revision,
            values=values,
        )
        db.add(link)
        try:
            await db.flush()
        except IntegrityError as exc:
            raise BoardError("link_exists", "The dependency link already exists") from exc
        event = await self._event(
            db,
            child,
            owner,
            kind="dependency.added",
            metadata={
                "parent_task_id": parent.task_id,
                "task_revision": child.task_revision,
                "ready_demoted": demote_ready,
            },
        )
        return link, event

    async def delete_link(
        self,
        db: AsyncSession,
        owner: WorkBoardOwner,
        request: WorkBoardLinkDelete,
    ) -> WorkBoardEvent:
        parent = await self._owned_task(db, owner, request.parent_task_id)
        child = await self._owned_task(db, owner, request.child_task_id)
        if child.task_revision != request.expected_child_revision:
            raise BoardRevisionConflict(child.task_id, request.expected_child_revision, child.task_revision)
        result = await db.execute(
            select(WorkBoardLink).where(
                WorkBoardLink.owner_principal_id == owner.principal_id,
                WorkBoardLink.owner_session_id == owner.session_id,
                WorkBoardLink.parent_task_id == parent.task_id,
                WorkBoardLink.child_task_id == request.child_task_id,
            )
        )
        link = result.scalar_one_or_none()
        if link is None:
            raise BoardError("link_not_found", "The dependency link does not exist", status_code=404)
        await db.delete(link)
        await self._cas_task_update(
            db,
            owner,
            child,
            expected_revision=request.expected_child_revision,
            values={
                "task_revision": request.expected_child_revision + 1,
                "updated_at": _now(),
            },
        )
        await db.flush()
        return await self._event(
            db,
            child,
            owner,
            kind="dependency.removed",
            metadata={"parent_task_id": request.parent_task_id, "task_revision": child.task_revision},
        )

    async def get_detail(
        self,
        db: AsyncSession,
        owner: WorkBoardOwner,
        task_id: str,
    ) -> dict[str, Any]:
        task = await self._owned_task(db, owner, task_id)
        attempts = list(
            (
                await db.execute(
                    select(WorkBoardAttempt)
                    .where(WorkBoardAttempt.task_id == task.task_id)
                    .order_by(WorkBoardAttempt.created_at.desc())
                )
            ).scalars().all()
        )
        comments = list(
            (
                await db.execute(
                    select(WorkBoardComment)
                    .where(
                        WorkBoardComment.task_id == task.task_id,
                        WorkBoardComment.owner_principal_id == owner.principal_id,
                        WorkBoardComment.owner_session_id == owner.session_id,
                    )
                    .order_by(WorkBoardComment.created_at.asc())
                )
            ).scalars().all()
        )
        parent_task = aliased(WorkBoardTask)
        parents = list(
            (
                await db.execute(
                    select(WorkBoardLink.parent_task_id)
                    .join(parent_task, parent_task.task_id == WorkBoardLink.parent_task_id)
                    .where(
                        WorkBoardLink.child_task_id == task.task_id,
                        WorkBoardLink.owner_principal_id == owner.principal_id,
                        WorkBoardLink.owner_session_id == owner.session_id,
                        parent_task.owner_principal_id == owner.principal_id,
                        parent_task.owner_session_id == owner.session_id,
                    )
                )
            ).scalars().all()
        )
        child_task = aliased(WorkBoardTask)
        children = list(
            (
                await db.execute(
                    select(WorkBoardLink.child_task_id)
                    .join(child_task, child_task.task_id == WorkBoardLink.child_task_id)
                    .where(
                        WorkBoardLink.parent_task_id == task.task_id,
                        WorkBoardLink.owner_principal_id == owner.principal_id,
                        WorkBoardLink.owner_session_id == owner.session_id,
                        child_task.owner_principal_id == owner.principal_id,
                        child_task.owner_session_id == owner.session_id,
                    )
                )
            ).scalars().all()
        )
        events = list(
            (
                await db.execute(
                    select(WorkBoardEvent)
                    .where(
                        WorkBoardEvent.task_id == task.task_id,
                        WorkBoardEvent.owner_principal_id == owner.principal_id,
                        WorkBoardEvent.owner_session_id == owner.session_id,
                    )
                    .order_by(WorkBoardEvent.event_id.desc())
                    .limit(20)
                )
            ).scalars().all()
        )
        return {
            "task": task,
            "attempts": attempts,
            "parents": [str(item) for item in parents],
            "children": [str(item) for item in children],
            "comments": comments,
            "events": list(reversed(events)),
        }

    async def list_events(
        self,
        db: AsyncSession,
        owner: WorkBoardOwner,
        *,
        after: int = 0,
        limit: int = _EVENT_LIMIT,
    ) -> BoardEventPage:
        if after < 0:
            raise BoardError("invalid_cursor", "after must be non-negative", status_code=400)
        limit = max(1, min(int(limit), _EVENT_LIMIT))
        owner_filter = (
            WorkBoardEvent.owner_principal_id == owner.principal_id,
            WorkBoardEvent.owner_session_id == owner.session_id,
        )
        minimum = await db.scalar(select(func.min(WorkBoardEvent.event_id)).where(*owner_filter))
        maximum = await db.scalar(select(func.max(WorkBoardEvent.event_id)).where(*owner_filter))
        gap = bool(after > 0 and minimum is not None and after < int(minimum) - 1)
        result = await db.execute(
            select(WorkBoardEvent)
            .where(*owner_filter, WorkBoardEvent.event_id > after)
            .order_by(WorkBoardEvent.event_id.asc())
            .limit(limit)
        )
        events = list(result.scalars().all())
        last_event_id = int(maximum or after or 0)
        if events and events[-1].event_id is not None:
            last_event_id = int(events[-1].event_id)
        return BoardEventPage(events, last_event_id, gap)

    async def list_task_events(
        self,
        db: AsyncSession,
        owner: WorkBoardOwner,
        task_id: str,
        *,
        after: int = 0,
        limit: int = _EVENT_LIMIT,
    ) -> BoardEventPage:
        await self._owned_task(db, owner, task_id)
        # Keep the global event cursor semantics while constraining the task.
        if after < 0:
            raise BoardError("invalid_cursor", "after must be non-negative", status_code=400)
        limit = max(1, min(int(limit), _EVENT_LIMIT))
        filters = (
            WorkBoardEvent.task_id == task_id,
            WorkBoardEvent.owner_principal_id == owner.principal_id,
            WorkBoardEvent.owner_session_id == owner.session_id,
        )
        result = await db.execute(
            select(WorkBoardEvent)
            .where(*filters, WorkBoardEvent.event_id > after)
            .order_by(WorkBoardEvent.event_id.asc())
            .limit(limit)
        )
        events = list(result.scalars().all())
        maximum = await db.scalar(select(func.max(WorkBoardEvent.event_id)).where(*filters))
        return BoardEventPage(events, int(maximum or after or 0), False)


__all__ = [
    "BoardError",
    "BoardEventPage",
    "BoardIdempotencyConflict",
    "BoardMutation",
    "BoardNotFound",
    "BoardOwnerMismatch",
    "BoardPage",
    "BoardRevisionConflict",
    "WorkBoardRepository",
]
