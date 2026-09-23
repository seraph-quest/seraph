"""Canonical SQLite repository for operator work-board state.

Every method receives the caller's SQLAlchemy session so task changes, their
append-only event, and any dependency/comment record share one transaction.
The repository never admits or mutates a durable workflow; M2 owns that
integration through the existing job runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Any, Mapping

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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
_UNRESOLVED_RECEIPT_STATUSES = {
    "unknown",
    "unknown_external_effect",
    "cost_liability",
    "intent",
    "dispatched",
}


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
    if not normalized or len(normalized) > max_length or not _SAFE_ID.fullmatch(normalized):
        raise BoardError("invalid_reference", f"{field} must be a bounded safe reference")


def _validate_digest(value: str | None, *, field: str) -> None:
    if value is not None and not _DIGEST.fullmatch(str(value).lower()):
        raise BoardError("invalid_digest", f"{field} must be a SHA-256 hexadecimal digest")


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


def _safe_receipt_refs(value: Any, *, limit: int = 32) -> list[dict[str, Any]]:
    """Keep only bounded operator-safe receipt references.

    Durable workflow serializers already redact their ledgers, but board
    projections are a second trust boundary.  Store identifiers, digests,
    statuses, and recovery codes only; never copy a workflow result or raw
    capability input into a task row.
    """

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
                    if not _SAFE_ID.fullmatch(bounded[:512]):
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
                    safe[key] = bounded[:512]
                elif key in {"file_path", "target_path"}:
                    # A path is retained only as a normalized workspace
                    # reference.  Absolute paths, traversal, and prose are
                    # deliberately omitted from board projections.
                    if (
                        not bounded
                        or bounded.startswith(("/", "~"))
                        or "\\" in bounded
                        or any(part in {"", ".", ".."} for part in bounded.split("/"))
                        or len(bounded) > 512
                    ):
                        continue
                    safe[key] = bounded
                else:
                    continue
        if safe:
            safe_items.append(safe)
    return safe_items


def _projection_value(projection: Mapping[str, Any], field_name: str) -> Any:
    """Read an immutable durable-job field from its safe projection."""

    if field_name in projection:
        return projection.get(field_name)
    if field_name in {"owner_principal_id", "owner_kind", "service_id"}:
        owner = projection.get("owner")
        if isinstance(owner, Mapping):
            return owner.get(field_name)
    if field_name in {"idempotency_scope", "idempotency_key", "idempotency_binding"}:
        idempotency = projection.get("idempotency")
        if isinstance(idempotency, Mapping):
            return idempotency.get(field_name)
    if field_name in {"capability_id", "capability_version"}:
        authority = projection.get("declared_authority")
        if isinstance(authority, Mapping) and field_name in authority:
            return authority.get(field_name)
        if field_name == "capability_id":
            return projection.get("job_kind")
    return None


def _validate_linked_workflow_projection(
    task: WorkBoardTask,
    attempt: WorkBoardAttempt,
    workflow_run_id: str,
    projection: Mapping[str, Any],
    expected_identity: Mapping[str, Any] | None = None,
) -> None:
    """Fail closed when a durable run does not match the claimed attempt.

    The board stores only the immutable run identity.  Callers that recovered
    a run by its durable idempotency binding pass the safe runtime projection
    here; raw inputs and runtime prose are deliberately not accepted.
    """

    projected_job_id = str(_projection_value(projection, "job_id") or "").strip()
    if projected_job_id and projected_job_id != workflow_run_id:
        raise BoardError(
            "workflow_identity_conflict",
            "The durable run projection does not match the requested workflow link",
        )
    expected: dict[str, Any] = {
        "goal_id": task.goal_id,
        "goal_revision": task.goal_revision,
        "operator_session_id": task.owner_session_id,
        "capability_id": task.capability_id,
    }
    if expected_identity:
        expected.update(
            {
                str(key): value
                for key, value in expected_identity.items()
                if value is not None
                and str(key)
                in {
                    "job_id",
                    "job_kind",
                    "owner_principal_id",
                    "owner_kind",
                    "service_id",
                    "goal_id",
                    "goal_revision",
                    "operator_session_id",
                    "session_id",
                    "capability_id",
                    "capability_version",
                    "input_digest",
                    "authority_digest",
                    "run_fingerprint",
                    "idempotency_scope",
                    "idempotency_key",
                    "idempotency_binding",
                }
            }
        )
    for field_name, expected_value in expected.items():
        if expected_value is None:
            continue
        actual_field = field_name
        actual = _projection_value(projection, actual_field)
        if actual is None and field_name == "operator_session_id":
            actual_field = "session_id"
            actual = _projection_value(projection, actual_field)
        if actual is None or str(actual) != str(expected_value):
            raise BoardError(
                "workflow_identity_conflict",
                "The durable run does not match the task's immutable execution contract",
            )
    # If the task already recorded a binding, it is immutable. A recovered
    # run may fill it once, but never replace it with a different binding.
    projected_binding = _projection_value(projection, "idempotency_binding")
    if task.idempotency_binding and projected_binding != task.idempotency_binding:
        raise BoardError(
            "workflow_idempotency_conflict",
            "The durable run has a different idempotency binding",
        )
    if attempt.workflow_run_id and attempt.workflow_run_id != workflow_run_id:
        raise BoardError(
            "attempt_workflow_conflict",
            "An attempt cannot link to a second workflow run",
        )


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
    latest_attempts: dict[str, WorkBoardAttempt] = field(default_factory=dict)
    attempt_counts: dict[str, int] = field(default_factory=dict)
    dispatch_ranks: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class BoardEventPage:
    events: list[WorkBoardEvent]
    last_event_id: int
    gap: bool


@dataclass(frozen=True)
class BoardDispatchClaim:
    """One atomically fenced board claim and its pending attempt."""

    task: WorkBoardTask
    attempt: WorkBoardAttempt
    event: WorkBoardEvent


@dataclass(frozen=True)
class BoardAttemptProjection:
    """Safe projection of one reconciled durable attempt."""

    task: WorkBoardTask
    attempt: WorkBoardAttempt
    event: WorkBoardEvent


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
                .where(WorkBoardEvent.task_id == existing.task_id)
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
        db.add(task)
        try:
            await db.flush()
        except IntegrityError as exc:
            # A concurrent request can win the unique idempotency index after
            # the preflight query.  Let the caller retry and read the winner;
            # never return an uncommitted duplicate projection.
            raise BoardError(
                "idempotency_race",
                "The idempotency key was claimed concurrently; retry the request",
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
        # Compute rank over the complete owner-visible Ready set before any
        # status/assignee/search/pagination filter.  This is an operator
        # projection only; claim ordering is still rechecked atomically by the
        # dispatcher kernel.
        ready_result = await db.execute(
            select(WorkBoardTask.task_id)
            .where(
                WorkBoardTask.owner_principal_id == owner.principal_id,
                WorkBoardTask.owner_session_id == owner.session_id,
                WorkBoardTask.status == WorkBoardStatus.ready,
            )
            .order_by(
                WorkBoardTask.priority.desc(),
                WorkBoardTask.creation_sequence.asc(),
            )
        )
        dispatch_ranks = {
            str(task_id): index
            for index, task_id in enumerate(ready_result.scalars().all(), start=1)
        }
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
        latest_attempts: dict[str, WorkBoardAttempt] = {}
        attempt_counts: dict[str, int] = {task.task_id: 0 for task in rows}
        if rows:
            attempt_count_result = await db.execute(
                select(WorkBoardAttempt.task_id, func.count(WorkBoardAttempt.attempt_id))
                .where(WorkBoardAttempt.task_id.in_([task.task_id for task in rows]))
                .group_by(WorkBoardAttempt.task_id)
            )
            attempt_counts.update(
                {str(task_id): int(count or 0) for task_id, count in attempt_count_result.all()}
            )
            attempt_result = await db.execute(
                select(WorkBoardAttempt)
                .where(WorkBoardAttempt.task_id.in_([task.task_id for task in rows]))
                .order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc())
            )
            for attempt in attempt_result.scalars().all():
                latest_attempts.setdefault(attempt.task_id, attempt)
        last_event_id = int(
            await db.scalar(
                select(func.max(WorkBoardEvent.event_id)).where(
                    WorkBoardEvent.owner_principal_id == owner.principal_id,
                    WorkBoardEvent.owner_session_id == owner.session_id,
                )
            )
            or 0
        )
        return BoardPage(
            rows,
            next_after,
            last_event_id,
            dependency_counts,
            latest_attempts,
            attempt_counts,
            dispatch_ranks,
        )

    async def dispatch_rank(
        self,
        db: AsyncSession,
        owner: WorkBoardOwner,
        task: WorkBoardTask,
    ) -> int | None:
        """Return a Ready task's rank over the complete owner-visible queue."""

        await _begin_read_snapshot(db)
        if task.status is not WorkBoardStatus.ready:
            return None
        result = await db.execute(
            select(WorkBoardTask.task_id)
            .where(
                WorkBoardTask.owner_principal_id == owner.principal_id,
                WorkBoardTask.owner_session_id == owner.session_id,
                WorkBoardTask.status == WorkBoardStatus.ready,
            )
            .order_by(
                WorkBoardTask.priority.desc(),
                WorkBoardTask.creation_sequence.asc(),
            )
        )
        for index, task_id in enumerate(result.scalars().all(), start=1):
            if str(task_id) == task.task_id:
                return index
        return None

    async def block_ready_task(
        self,
        db: AsyncSession,
        task_id: str,
        *,
        expected_revision: int,
        block_kind: str,
        block_reason: str,
        actor_principal_id: str,
        actor_session_id: str,
        now: datetime | None = None,
    ) -> BoardMutation:
        """Block a pre-dispatch Ready row after a fresh capability preflight."""

        observed_at = now or _now()
        await _begin_sqlite_immediate(db)
        task = await self._find_task(db, task_id)
        if task is None:
            raise BoardNotFound(task_id)
        owner = WorkBoardOwner(
            principal_id=task.owner_principal_id,
            session_id=task.owner_session_id,
        )
        if task.status is not WorkBoardStatus.ready:
            raise BoardError("stale_revision", "The task is no longer Ready")
        safe_reason = await self._safe_text(block_reason)
        await self._cas_task_update(
            db,
            owner,
            task,
            expected_revision=int(expected_revision),
            values={
                "status": WorkBoardStatus.blocked,
                "block_source_status": WorkBoardStatus.ready.value,
                "block_kind": str(block_kind)[:128],
                "block_reason": safe_reason,
                "task_revision": int(expected_revision) + 1,
                "updated_at": observed_at,
            },
        )
        event = await self._event(
            db,
            task,
            owner,
            kind="task.blocked",
            metadata={
                "status": task.status.value,
                "task_revision": task.task_revision,
                "block_kind": str(block_kind)[:128],
            },
            actor_principal_id=actor_principal_id,
            actor_session_id=actor_session_id,
        )
        return BoardMutation(task, event)

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
                safe_changes[field] = value
        authority_fields = {
            "capability_id",
            "typed_input_ref",
            "typed_input_digest",
            "executor_id",
            "scheduled_at",
        }
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
            if task.status not in {
                WorkBoardStatus.triage,
                WorkBoardStatus.todo,
                WorkBoardStatus.ready,
                WorkBoardStatus.review,
            }:
                raise BoardError("illegal_transition", "This task cannot be manually blocked from its current status")
            active_attempt = await db.scalar(
                select(WorkBoardAttempt.attempt_id).where(
                    WorkBoardAttempt.task_id == task.task_id,
                    WorkBoardAttempt.ended_at.is_(None),
                )
            )
            if active_attempt is not None:
                raise BoardError(
                    "active_attempt_requires_typed_cancel",
                    "A task with an active or pending attempt requires its typed recovery path",
                    status_code=409,
                )
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
                .where(
                    WorkBoardAttempt.task_id == task.task_id,
                    WorkBoardAttempt.ended_at.is_(None),
                )
                .limit(1)
            )
            if attempt_result.scalar_one_or_none() is not None:
                raise BoardError(
                    "attempt_reconcile_required",
                    "A task with an execution attempt requires typed recovery before unblock",
                    status_code=409,
                )
            source = task.block_source_status
            allowed_sources = {
                WorkBoardStatus.triage.value,
                WorkBoardStatus.todo.value,
                WorkBoardStatus.ready.value,
                WorkBoardStatus.review.value,
            }
            if source not in allowed_sources:
                raise BoardError(
                    "invalid_recovery_target",
                    "The operator block does not retain a safe prior board phase",
                    status_code=409,
                )
            resolution = await self._safe_text(request.resolution or "")
            if not resolution.strip():
                raise BoardError("resolution_required", "An explicit recovery resolution is required")
            if source == WorkBoardStatus.ready.value:
                if (
                    not task.capability_id
                    or not task.typed_input_ref
                    or not task.typed_input_digest
                    or not task.executor_id
                    or (task.scheduled_at is not None and task.scheduled_at > _now())
                ):
                    raise BoardError(
                        "ready_gates_unmet",
                        "A task may return to Ready only with a complete bounded executor specification",
                        status_code=409,
                    )
                parents = list(
                    (
                        await db.execute(
                            select(WorkBoardTask.status)
                            .join(
                                WorkBoardLink,
                                WorkBoardTask.task_id == WorkBoardLink.parent_task_id,
                            )
                            .where(
                                WorkBoardLink.child_task_id == task.task_id,
                                WorkBoardLink.owner_principal_id == owner.principal_id,
                                WorkBoardLink.owner_session_id == owner.session_id,
                            )
                        )
                    ).scalars().all()
                )
                if any(status is not WorkBoardStatus.done for status in parents):
                    raise BoardError(
                        "dependencies_unfinished",
                        "Every blocking parent must be Done before returning to Ready",
                        status_code=409,
                    )
            elif source == WorkBoardStatus.review.value:
                if not task.reviewer_id:
                    raise BoardError(
                        "reviewer_required",
                        "A Review task must retain its named reviewer",
                        status_code=409,
                    )
                latest_attempt = (
                    await db.execute(
                        select(WorkBoardAttempt)
                        .where(WorkBoardAttempt.task_id == task.task_id)
                        .order_by(WorkBoardAttempt.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                evidence = _safe_receipt_refs(
                    json.loads(latest_attempt.receipt_refs_json or "[]")
                    if latest_attempt is not None
                    else []
                )
                if latest_attempt is None or latest_attempt.ended_at is None or not any(
                    bool(item.get("verified"))
                    and str(item.get("status") or "") in {"succeeded", "read_back", "reconciled"}
                    for item in evidence
                ):
                    raise BoardError(
                        "verified_readback_required",
                        "A Review task may return to Review only with verified readback evidence",
                        status_code=409,
                    )
            values.update(
                {
                    "status": WorkBoardStatus(source),
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

    async def request_cancel(
        self,
        db: AsyncSession,
        owner: WorkBoardOwner,
        task_id: str,
        *,
        expected_revision: int,
        attempt_id: str,
        board_fence: int,
        lease_owner: str,
        actor_principal_id: str | None = None,
        actor_session_id: str | None = None,
        now: datetime | None = None,
    ) -> BoardMutation:
        """Persist one operator cancellation intent with a task revision CAS.

        Adapter cleanup happens after this transaction.  The timestamp and
        event make a crash between intent and cleanup recoverable without
        relaunching the attempt.  Replaying the same request is a no-op.
        """

        observed_at = now or _now()
        await _begin_sqlite_immediate(db)
        task = await self._owned_task(db, owner, task_id)
        expected = int(expected_revision)
        if task.task_revision != expected:
            raise BoardRevisionConflict(task.task_id, expected, task.task_revision)
        if task.status is not WorkBoardStatus.running:
            raise BoardError("illegal_transition", "Only a running task can be cancelled", status_code=409)
        attempt = (
            await db.execute(
                select(WorkBoardAttempt).where(
                    WorkBoardAttempt.task_id == task_id,
                    WorkBoardAttempt.attempt_id == attempt_id,
                    WorkBoardAttempt.ended_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if attempt is None:
            raise BoardError("attempt_not_found", "The running task has no active attempt", status_code=409)
        if not attempt.workflow_run_id:
            raise BoardError(
                "admission_reconcile_required",
                "Cancellation waits for the pending admission binding to be reconciled",
                status_code=409,
            )
        if attempt.fencing_token != int(board_fence) or attempt.lease_owner != lease_owner:
            raise BoardError("stale_fence", "The board attempt fence is stale")
        if attempt.cancel_requested_at is not None:
            cancel_key = f"work-board-cancel:{task_id}:{attempt_id}"
            latest = (
                await db.execute(
                    select(WorkBoardEvent)
                    .where(WorkBoardEvent.task_id == task_id)
                    .where(WorkBoardEvent.kind == "attempt.cancel_requested")
                    .where(WorkBoardEvent.metadata_json.contains(cancel_key))
                    .order_by(WorkBoardEvent.event_id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if latest is None:
                latest = await self._event(
                    db,
                    task,
                    owner,
                    kind="attempt.cancel_requested",
                    metadata={
                        "cancel_key": cancel_key,
                        "attempt_id": attempt_id,
                        "task_revision": task.task_revision,
                    },
                    actor_principal_id=actor_principal_id or owner.principal_id,
                    actor_session_id=actor_session_id or owner.session_id,
                )
            return BoardMutation(task, latest, idempotent_replay=True)
        await self._cas_task_update(
            db,
            owner,
            task,
            expected_revision=expected,
            values={
                "task_revision": expected + 1,
                "updated_at": observed_at,
            },
        )
        attempt.cancel_requested_at = observed_at
        attempt.updated_at = observed_at
        cancel_key = f"work-board-cancel:{task_id}:{attempt_id}"
        event = await self._event(
            db,
            task,
            owner,
            kind="attempt.cancel_requested",
            metadata={
                "cancel_key": cancel_key,
                "attempt_id": attempt_id,
                "workflow_run_id": attempt.workflow_run_id,
                "task_revision": task.task_revision,
                "recovery_action": "reconcile_external_effect",
            },
            actor_principal_id=actor_principal_id or owner.principal_id,
            actor_session_id=actor_session_id or owner.session_id,
        )
        return BoardMutation(task, event)

    async def retry_task(
        self,
        db: AsyncSession,
        owner: WorkBoardOwner,
        task_id: str,
        *,
        expected_revision: int,
    ) -> BoardMutation:
        """Return a safely retryable blocked task to Todo.

        This is an operator action over the board projection.  It never
        creates an attempt or replays a durable run; the dispatcher must
        re-admit the next fenced attempt after all live goal/capability gates
        pass. Unknown effects, approval-held work, and a spent attempt budget
        require their typed recovery path instead.
        """

        await _begin_sqlite_immediate(db)
        task = await self._owned_task(db, owner, task_id)
        expected = int(expected_revision)
        if task.task_revision != expected:
            raise BoardRevisionConflict(task.task_id, expected, task.task_revision)
        if task.status is not WorkBoardStatus.blocked:
            raise BoardError(
                "illegal_transition",
                "Only blocked tasks can be retried",
                status_code=409,
            )
        if task.block_kind in {
            "unknown_effect",
            "reconcile_admission_binding",
            "needs_input",
            "attempt_limit",
        }:
            raise BoardError(
                "typed_reconcile_required",
                "This block requires its typed recovery path before retry",
                status_code=409,
            )
        await self.validate_task_goal(db, owner, task)
        if task.scheduled_at is not None and task.scheduled_at > _now():
            raise BoardError(
                "retry_prerequisite",
                "The task schedule has not reached its retry eligibility time",
                status_code=409,
                recovery_action="restore_prerequisite",
            )
        parent_statuses = list(
            (
                await db.execute(
                    select(WorkBoardTask.status)
                    .join(
                        WorkBoardLink,
                        WorkBoardLink.parent_task_id == WorkBoardTask.task_id,
                    )
                    .where(
                        WorkBoardLink.child_task_id == task.task_id,
                        WorkBoardLink.owner_principal_id == owner.principal_id,
                        WorkBoardLink.owner_session_id == owner.session_id,
                        WorkBoardTask.owner_principal_id == owner.principal_id,
                        WorkBoardTask.owner_session_id == owner.session_id,
                    )
                )
            ).scalars().all()
        )
        if any(status is not WorkBoardStatus.done for status in parent_statuses):
            raise BoardError(
                "retry_prerequisite",
                "Every blocking parent must be Done before retry",
                status_code=409,
                recovery_action="restore_prerequisite",
            )
        active_attempt = await db.scalar(
            select(WorkBoardAttempt.attempt_id).where(
                WorkBoardAttempt.task_id == task.task_id,
                WorkBoardAttempt.ended_at.is_(None),
            )
        )
        if active_attempt is not None:
            raise BoardError(
                "attempt_reconcile_required",
                "A task with an active execution attempt requires reconciliation before retry",
                status_code=409,
            )
        attempt_count = int(
            await db.scalar(
                select(func.count(WorkBoardAttempt.attempt_id)).where(
                    WorkBoardAttempt.task_id == task.task_id,
                )
            )
            or 0
        )
        if attempt_count >= 2:
            raise BoardError(
                "attempt_limit",
                "The board attempt limit has been exhausted",
                status_code=409,
            )
        if attempt_count:
            latest_attempt = (
                await db.execute(
                    select(WorkBoardAttempt)
                    .where(WorkBoardAttempt.task_id == task.task_id)
                    .order_by(WorkBoardAttempt.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            try:
                latest_receipts = _safe_receipt_refs(
                    json.loads(latest_attempt.receipt_refs_json or "[]")
                    if latest_attempt is not None
                    else []
                )
            except (TypeError, ValueError):
                latest_receipts = []
            safe_no_effect = bool(latest_receipts) and all(
                str(item.get("status") or "") in {"failed", "blocked", "cancelled", "no_external_effect", "not_dispatched"}
                and not str(item.get("status") or "") in _UNRESOLVED_RECEIPT_STATUSES
                and str(item.get("reason_code") or item.get("outcome") or "")
                in {"no_external_effect", "not_dispatched", "cancelled", "operator_cancelled", "transient", "capability"}
                for item in latest_receipts
            )
            if not safe_no_effect:
                raise BoardError(
                    "retry_requires_no_effect_proof",
                    "Retry requires a durable receipt proving no external effect or confirmed cancellation",
                    status_code=409,
                )
        previous = task.status
        await self._cas_task_update(
            db,
            owner,
            task,
            expected_revision=expected,
            values={
                "status": WorkBoardStatus.todo,
                "block_source_status": None,
                "block_kind": None,
                "block_reason": None,
                "task_revision": expected + 1,
                "updated_at": _now(),
            },
        )
        event = await self._event(
            db,
            task,
            owner,
            kind="task.retry",
            metadata={
                "from_status": previous.value,
                "status": task.status.value,
                "task_revision": task.task_revision,
                "recovery_action": "retry",
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
        parent_task_id: str,
        child_task_id: str,
        owner: WorkBoardOwner | None = None,
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
            conditions = [WorkBoardLink.parent_task_id == current]
            if owner is not None:
                conditions.extend(
                    (
                        WorkBoardLink.owner_principal_id == owner.principal_id,
                        WorkBoardLink.owner_session_id == owner.session_id,
                    )
                )
            result = await db.execute(select(WorkBoardLink.child_task_id).where(*conditions))
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
            parent_task_id=parent.task_id,
            child_task_id=child.task_id,
            owner=owner,
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
        # Deletion is a graph mutation too. Serialize the lookup, revision
        # CAS, delete, and event so two sessions cannot both validate the
        # same edge against one child revision.
        await _begin_sqlite_immediate(db)
        child = await self._owned_task(db, owner, request.child_task_id)
        if child.task_revision != request.expected_child_revision:
            raise BoardRevisionConflict(child.task_id, request.expected_child_revision, child.task_revision)
        result = await db.execute(
            select(WorkBoardLink).where(
                WorkBoardLink.owner_principal_id == owner.principal_id,
                WorkBoardLink.owner_session_id == owner.session_id,
                WorkBoardLink.parent_task_id == request.parent_task_id,
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

    async def list_dispatch_candidates(
        self,
        db: AsyncSession,
        *,
        now: datetime | None = None,
        limit: int = 20,
    ) -> list[WorkBoardTask]:
        """Read a bounded, stable set of Todo candidates for one pass.

        Readiness is rechecked by ``promote_task_ready`` under the writer
        fence.  This listing is therefore advisory and cannot itself claim a
        task or authorize execution.
        """

        observed_at = now or _now()
        bounded_limit = max(1, min(int(limit), 20))
        result = await db.execute(
            select(WorkBoardTask)
            .where(
                WorkBoardTask.status.in_((WorkBoardStatus.todo, WorkBoardStatus.ready)),
                (
                    WorkBoardTask.scheduled_at.is_(None)
                    | (WorkBoardTask.scheduled_at <= observed_at)
                ),
            )
            .order_by(
                WorkBoardTask.priority.desc(),
                WorkBoardTask.creation_sequence.asc(),
            )
            .limit(bounded_limit)
        )
        return list(result.scalars().all())

    async def promote_task_ready(
        self,
        db: AsyncSession,
        task_id: str,
        *,
        expected_revision: int,
        actor_principal_id: str,
        actor_session_id: str | None = None,
        readiness_error: str | None = None,
        readiness_reason: str | None = None,
        now: datetime | None = None,
    ) -> BoardMutation | None:
        """Atomically admit one Todo task to Ready or a typed Blocked state.

        The dispatcher supplies capability-registry and authority results;
        dependency, schedule, and canonical-goal checks are repeated here so
        a stale candidate cannot be admitted after a concurrent mutation.
        """

        observed_at = now or _now()
        await _begin_sqlite_immediate(db)
        task = await self._find_task(db, task_id)
        if task is None:
            raise BoardNotFound(task_id)
        if task.status is not WorkBoardStatus.todo:
            return None
        if task.task_revision != int(expected_revision):
            raise BoardRevisionConflict(task.task_id, int(expected_revision), task.task_revision)
        if task.scheduled_at is not None and task.scheduled_at > observed_at:
            return None

        owner = WorkBoardOwner(
            principal_id=task.owner_principal_id,
            session_id=task.owner_session_id,
        )
        parents = list(
            (
                await db.execute(
                    select(WorkBoardTask.status)
                    .join(
                        WorkBoardLink,
                        WorkBoardTask.task_id == WorkBoardLink.parent_task_id,
                    )
                    .where(
                        WorkBoardLink.child_task_id == task.task_id,
                        WorkBoardLink.owner_principal_id == task.owner_principal_id,
                        WorkBoardLink.owner_session_id == task.owner_session_id,
                        WorkBoardTask.owner_principal_id == task.owner_principal_id,
                        WorkBoardTask.owner_session_id == task.owner_session_id,
                    )
                )
            ).scalars().all()
        )
        if any(status is not WorkBoardStatus.done for status in parents):
            return None

        if readiness_error is None:
            try:
                await self.validate_task_goal(db, owner, task)
            except BoardError as exc:
                readiness_error = exc.code
                readiness_reason = exc.message

        previous = task.status
        if readiness_error:
            safe_reason = await self._safe_text(
                readiness_reason or readiness_error
            )
            values: dict[str, Any] = {
                "status": WorkBoardStatus.blocked,
                "block_source_status": previous.value,
                "block_kind": readiness_error[:128],
                "block_reason": safe_reason[:1000],
            }
            event_kind = "task.dispatch_blocked"
            metadata = {
                "status": WorkBoardStatus.blocked.value,
                "block_kind": readiness_error[:128],
            }
        else:
            values = {"status": WorkBoardStatus.ready}
            event_kind = "task.ready"
            metadata = {"status": WorkBoardStatus.ready.value}
        values.update(
            {
                "task_revision": int(expected_revision) + 1,
                "updated_at": observed_at,
            }
        )
        await self._cas_task_update(
            db,
            owner,
            task,
            expected_revision=int(expected_revision),
            values=values,
        )
        metadata["task_revision"] = task.task_revision
        event = await self._event(
            db,
            task,
            owner,
            kind=event_kind,
            metadata=metadata,
            actor_principal_id=actor_principal_id,
            actor_session_id=actor_session_id or "work-board-dispatch",
        )
        return BoardMutation(task, event)

    async def claim_ready_task(
        self,
        db: AsyncSession,
        task_id: str,
        *,
        expected_revision: int,
        lease_owner: str,
        lease_seconds: int = 300,
        now: datetime | None = None,
        actor_principal_id: str | None = None,
        actor_session_id: str | None = None,
    ) -> BoardDispatchClaim | None:
        """Atomically claim a Ready task and persist its pending attempt."""

        observed_at = now or _now()
        lease_seconds = max(1, min(int(lease_seconds), 900))
        await _begin_sqlite_immediate(db)
        task = await self._find_task(db, task_id)
        if task is None:
            raise BoardNotFound(task_id)
        if task.status is not WorkBoardStatus.ready:
            return None
        if task.task_revision != int(expected_revision):
            raise BoardRevisionConflict(task.task_id, int(expected_revision), task.task_revision)
        if task.scheduled_at is not None and task.scheduled_at > observed_at:
            return None

        owner = WorkBoardOwner(
            principal_id=task.owner_principal_id,
            session_id=task.owner_session_id,
        )
        # Re-read the owner-bound goal inside the same immediate transaction
        # that creates the board claim.  The preflight pass is advisory; a
        # concurrent revision/owner/status change must not launch stale work.
        try:
            live_goal = await self.validate_task_goal(db, owner, task)
            live_goal_status = str(getattr(live_goal.status, "value", live_goal.status) or "")
            if live_goal_status and live_goal_status != "active":
                raise BoardError("goal_not_admitted", "The task goal is not currently executable")
        except BoardError as exc:
            safe_reason = await self._safe_text(exc.message)
            await self._cas_task_update(
                db,
                owner,
                task,
                expected_revision=int(expected_revision),
                values={
                    "status": WorkBoardStatus.blocked,
                    "block_source_status": WorkBoardStatus.ready.value,
                    "block_kind": exc.code,
                    "block_reason": safe_reason[:1000],
                    "task_revision": int(expected_revision) + 1,
                    "updated_at": observed_at,
                },
            )
            await self._event(
                db,
                task,
                owner,
                kind="task.dispatch_blocked",
                metadata={"status": WorkBoardStatus.blocked.value, "block_kind": exc.code},
                actor_principal_id=actor_principal_id or lease_owner,
                actor_session_id=actor_session_id or "work-board-dispatch",
            )
            return None
        parent_statuses = list(
            (
                await db.execute(
                    select(WorkBoardTask.status)
                    .join(
                        WorkBoardLink,
                        WorkBoardTask.task_id == WorkBoardLink.parent_task_id,
                    )
                    .where(
                        WorkBoardLink.child_task_id == task.task_id,
                        WorkBoardLink.owner_principal_id == task.owner_principal_id,
                        WorkBoardLink.owner_session_id == task.owner_session_id,
                        WorkBoardTask.owner_principal_id == task.owner_principal_id,
                        WorkBoardTask.owner_session_id == task.owner_session_id,
                    )
                )
            ).scalars().all()
        )
        if any(status is not WorkBoardStatus.done for status in parent_statuses):
            # A dependency can be added after promotion.  Return the task to
            # Todo so the next pass waits for the parent instead of executing
            # an invalid child.
            await self._cas_task_update(
                db,
                owner,
                task,
                expected_revision=int(expected_revision),
                values={
                    "status": WorkBoardStatus.todo,
                    "task_revision": int(expected_revision) + 1,
                    "updated_at": observed_at,
                },
            )
            event = await self._event(
                db,
                task,
                owner,
                kind="task.waiting_dependency",
                metadata={"status": WorkBoardStatus.todo.value, "task_revision": task.task_revision},
                actor_principal_id=actor_principal_id or lease_owner,
                actor_session_id=actor_session_id or "work-board-dispatch",
            )
            return None

        active_count = int(
            await db.scalar(
                select(func.count(WorkBoardTask.task_id)).where(
                    WorkBoardTask.status == WorkBoardStatus.running
                )
            )
            or 0
        )
        if active_count >= 2:
            return None
        if task.executor_id:
            executor_active = int(
                await db.scalar(
                    select(func.count(WorkBoardTask.task_id)).where(
                        WorkBoardTask.status == WorkBoardStatus.running,
                        WorkBoardTask.executor_id == task.executor_id,
                    )
                )
                or 0
            )
            if executor_active:
                return None

        attempt_count = int(
            await db.scalar(
                select(func.count(WorkBoardAttempt.attempt_id)).where(
                    WorkBoardAttempt.task_id == task.task_id
                )
            )
            or 0
        )
        if attempt_count >= 2:
            safe_reason = await self._safe_text("The board attempt limit has been exhausted")
            await self._cas_task_update(
                db,
                owner,
                task,
                expected_revision=int(expected_revision),
                values={
                    "status": WorkBoardStatus.blocked,
                    "block_source_status": WorkBoardStatus.ready.value,
                    "block_kind": "attempt_limit",
                    "block_reason": safe_reason,
                    "task_revision": int(expected_revision) + 1,
                    "updated_at": observed_at,
                },
            )
            event = await self._event(
                db,
                task,
                owner,
                kind="task.attempt_limit",
                metadata={"status": task.status.value, "task_revision": task.task_revision},
                actor_principal_id=actor_principal_id or lease_owner,
                actor_session_id=actor_session_id or "work-board-dispatch",
            )
            return None

        active_attempt = await db.scalar(
            select(WorkBoardAttempt.attempt_id).where(
                WorkBoardAttempt.task_id == task.task_id,
                WorkBoardAttempt.ended_at.is_(None),
            )
        )
        if active_attempt is not None:
            return None
        previous_fence = int(
            await db.scalar(
                select(func.max(WorkBoardAttempt.fencing_token)).where(
                    WorkBoardAttempt.task_id == task.task_id
                )
            )
            or 0
        )
        attempt = WorkBoardAttempt(
            task_id=task.task_id,
            task_revision_at_claim=int(expected_revision),
            lease_owner=str(lease_owner)[:256],
            lease_expires_at=observed_at + timedelta(seconds=lease_seconds),
            heartbeat_at=observed_at,
            fencing_token=previous_fence + 1,
            executor_id=(task.executor_id or "")[:128],
            started_at=observed_at,
            outcome="pending_admission",
        )
        db.add(attempt)
        await self._cas_task_update(
            db,
            owner,
            task,
            expected_revision=int(expected_revision),
            values={
                "status": WorkBoardStatus.running,
                "task_revision": int(expected_revision) + 1,
                "updated_at": observed_at,
            },
        )
        await db.flush()
        event = await self._event(
            db,
            task,
            owner,
            kind="task.claimed",
            metadata={
                "status": task.status.value,
                "task_revision": task.task_revision,
                "attempt_id": attempt.attempt_id,
                "executor_id": attempt.executor_id,
            },
            actor_principal_id=actor_principal_id or lease_owner,
            actor_session_id=actor_session_id or "work-board-dispatch",
        )
        return BoardDispatchClaim(task, attempt, event)

    async def link_attempt_workflow_run(
        self,
        db: AsyncSession,
        task_id: str,
        attempt_id: str,
        *,
        workflow_run_id: str,
        expected_revision: int,
        board_fence: int,
        lease_owner: str,
        workflow_projection: Mapping[str, Any] | None = None,
        expected_identity: Mapping[str, Any] | None = None,
        actor_principal_id: str | None = None,
        actor_session_id: str | None = None,
    ) -> BoardAttemptProjection:
        """Link a pending attempt to exactly one immutable durable run."""

        _validate_safe_identifier(workflow_run_id, field="workflow_run_id", max_length=256)
        await _begin_sqlite_immediate(db)
        task = await self._find_task(db, task_id)
        if task is None:
            raise BoardNotFound(task_id)
        owner = WorkBoardOwner(
            principal_id=task.owner_principal_id,
            session_id=task.owner_session_id,
        )
        attempt = (
            await db.execute(
                select(WorkBoardAttempt).where(
                    WorkBoardAttempt.attempt_id == attempt_id,
                    WorkBoardAttempt.task_id == task_id,
                )
            )
        ).scalar_one_or_none()
        if attempt is None:
            raise BoardError("attempt_not_found", "The board attempt does not exist", status_code=404)
        if workflow_projection is None and attempt.workflow_run_id is None:
            raise BoardError(
                "workflow_projection_required",
                "A pending board attempt must be linked to a verified durable-run projection",
            )
        if workflow_projection is not None:
            _validate_linked_workflow_projection(
                task,
                attempt,
                workflow_run_id,
                workflow_projection,
                expected_identity,
            )
        if attempt.workflow_run_id and attempt.workflow_run_id != workflow_run_id:
            raise BoardError("attempt_workflow_conflict", "An attempt cannot link to a second workflow run")
        if attempt.workflow_run_id == workflow_run_id:
            latest = (
                await db.execute(
                    select(WorkBoardEvent)
                    .where(WorkBoardEvent.task_id == task_id)
                    .order_by(WorkBoardEvent.event_id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if latest is None:
                latest = await self._event(
                    db,
                    task,
                    owner,
                    kind="attempt.linked",
                    metadata={"attempt_id": attempt_id, "workflow_run_id": workflow_run_id},
                    actor_principal_id=actor_principal_id or lease_owner,
                    actor_session_id=actor_session_id or "work-board-dispatch",
                )
            return BoardAttemptProjection(task, attempt, latest)
        if task.status is not WorkBoardStatus.running:
            raise BoardError("task_not_running", "Only a running task can link a workflow run")
        if task.task_revision != int(expected_revision):
            raise BoardRevisionConflict(task.task_id, int(expected_revision), task.task_revision)
        if attempt.fencing_token != int(board_fence) or attempt.lease_owner != lease_owner:
            raise BoardError("stale_fence", "The board attempt fence is stale")
        attempt.workflow_run_id = workflow_run_id
        attempt.updated_at = _now()
        projected_binding = (
            _projection_value(workflow_projection or {}, "idempotency_binding")
            if workflow_projection is not None
            else None
        )
        if projected_binding:
            task.idempotency_binding = str(projected_binding)[:512]
        await self._cas_task_update(
            db,
            owner,
            task,
            expected_revision=int(expected_revision),
            values={
                "task_revision": int(expected_revision) + 1,
                "updated_at": _now(),
                **({"idempotency_binding": str(projected_binding)[:512]} if projected_binding else {}),
            },
        )
        await db.flush()
        event = await self._event(
            db,
            task,
            owner,
            kind="attempt.linked",
            metadata={
                "attempt_id": attempt_id,
                "workflow_run_id": workflow_run_id,
                "task_revision": task.task_revision,
            },
            actor_principal_id=actor_principal_id or lease_owner,
            actor_session_id=actor_session_id or "work-board-dispatch",
        )
        return BoardAttemptProjection(task, attempt, event)

    async def validate_attempt_binding(
        self,
        db: AsyncSession,
        owner: WorkBoardOwner,
        task_id: str,
        attempt_id: str,
        *,
        workflow_run_id: str,
        board_fence: int,
        lease_owner: str,
        workflow_projection: Mapping[str, Any],
        expected_identity: Mapping[str, Any] | None = None,
    ) -> WorkBoardAttempt:
        """Validate a linked run without changing task state.

        Cancellation and restart recovery use this seam before invoking an
        adapter cleanup hook.  The complete owner/session/goal/capability and
        immutable runtime identity must match the persisted attempt.
        """

        task = await self._owned_task(db, owner, task_id)
        attempt = (
            await db.execute(
                select(WorkBoardAttempt).where(
                    WorkBoardAttempt.task_id == task_id,
                    WorkBoardAttempt.attempt_id == attempt_id,
                )
            )
        ).scalar_one_or_none()
        if attempt is None:
            raise BoardError("attempt_not_found", "The board attempt does not exist", status_code=404)
        if attempt.workflow_run_id != workflow_run_id:
            raise BoardError("attempt_workflow_conflict", "The durable run is not linked to this attempt")
        if attempt.ended_at is not None:
            raise BoardError("attempt_terminal", "The board attempt is already terminal")
        if attempt.fencing_token != int(board_fence) or attempt.lease_owner != lease_owner:
            raise BoardError("stale_fence", "The board attempt fence is stale")
        _validate_linked_workflow_projection(
            task,
            attempt,
            workflow_run_id,
            workflow_projection,
            expected_identity,
        )
        return attempt

    async def heartbeat_attempt(
        self,
        db: AsyncSession,
        task_id: str,
        attempt_id: str,
        *,
        expected_revision: int,
        board_fence: int,
        lease_owner: str,
        lease_seconds: int = 300,
        now: datetime | None = None,
    ) -> WorkBoardAttempt:
        """Refresh a board lease with task revision and fence CAS."""

        observed_at = now or _now()
        expiry = observed_at + timedelta(seconds=max(1, min(int(lease_seconds), 900)))
        task = await self._find_task(db, task_id)
        if task is None:
            raise BoardNotFound(task_id)
        if task.status is not WorkBoardStatus.running or task.task_revision != int(expected_revision):
            raise BoardError("stale_revision", "The running task revision is stale")
        updated = await db.execute(
            update(WorkBoardAttempt)
            .where(
                WorkBoardAttempt.attempt_id == attempt_id,
                WorkBoardAttempt.task_id == task_id,
                WorkBoardAttempt.lease_owner == lease_owner,
                WorkBoardAttempt.fencing_token == int(board_fence),
                WorkBoardAttempt.ended_at.is_(None),
                # An expired lease belongs to recovery. A late heartbeat must
                # never resurrect it after another dispatcher has had a
                # chance to reconcile or fence the attempt.
                WorkBoardAttempt.lease_expires_at > observed_at,
            )
            .values(lease_expires_at=expiry, heartbeat_at=observed_at, updated_at=observed_at)
            .execution_options(synchronize_session=False)
        )
        if int(updated.rowcount or 0) != 1:
            raise BoardError("stale_fence", "The board attempt lease is stale")
        refreshed = (
            await db.execute(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == attempt_id))
        ).scalar_one()
        return refreshed

    async def project_attempt(
        self,
        db: AsyncSession,
        task_id: str,
        attempt_id: str,
        *,
        expected_revision: int,
        board_fence: int,
        lease_owner: str,
        status: WorkBoardStatus,
        outcome: str,
        receipt_refs: Any = None,
        result_refs: Any = None,
        artifact_refs: Any = None,
        block_kind: str | None = None,
        block_reason: str | None = None,
        verified_readback: Mapping[str, Any] | None = None,
        actor_principal_id: str | None = None,
        actor_session_id: str | None = None,
        now: datetime | None = None,
    ) -> BoardAttemptProjection:
        """Project a reconciled attempt without overriding runtime authority."""

        if status not in {WorkBoardStatus.blocked, WorkBoardStatus.review, WorkBoardStatus.done}:
            raise BoardError("invalid_attempt_projection", "Only blocked, review, or done may follow Running")
        if status in {WorkBoardStatus.review, WorkBoardStatus.done}:
            if not isinstance(verified_readback, Mapping):
                raise BoardError(
                    "verified_readback_required",
                    "Review and Done require an independent durable readback proof",
                )
            if (
                str(verified_readback.get("source") or "") != "workflow_run"
                or str(verified_readback.get("status") or "") != "succeeded"
                or not bool(verified_readback.get("verified"))
            ):
                raise BoardError(
                    "verified_readback_required",
                    "The supplied readback is not an independent durable workflow proof",
                )
        observed_at = now or _now()
        await _begin_sqlite_immediate(db)
        task = await self._find_task(db, task_id)
        if task is None:
            raise BoardNotFound(task_id)
        owner = WorkBoardOwner(
            principal_id=task.owner_principal_id,
            session_id=task.owner_session_id,
        )
        if task.status is not WorkBoardStatus.running:
            raise BoardError("task_not_running", "Only a running task can be projected")
        if task.task_revision != int(expected_revision):
            raise BoardRevisionConflict(task.task_id, int(expected_revision), task.task_revision)
        attempt = (
            await db.execute(
                select(WorkBoardAttempt).where(
                    WorkBoardAttempt.attempt_id == attempt_id,
                    WorkBoardAttempt.task_id == task_id,
                )
            )
        ).scalar_one_or_none()
        if attempt is None:
            raise BoardError("attempt_not_found", "The board attempt does not exist", status_code=404)
        if attempt.lease_owner != lease_owner or attempt.fencing_token != int(board_fence) or attempt.ended_at is not None:
            raise BoardError("stale_fence", "The board attempt fence is stale")
        if status in {WorkBoardStatus.review, WorkBoardStatus.done}:
            proof_run_id = str(verified_readback.get("workflow_run_id") or "")
            proof_digest = str(verified_readback.get("content_sha256") or "")
            if proof_run_id != str(attempt.workflow_run_id or "") or not _DIGEST.fullmatch(proof_digest.lower()):
                raise BoardError(
                    "verified_readback_required",
                    "The readback proof must identify this attempt's durable run and digest",
                )
        safe_receipts = _safe_receipt_refs(receipt_refs)
        safe_results = _safe_receipt_refs(result_refs)
        safe_artifacts = _safe_receipt_refs(artifact_refs)
        unresolved_receipt = any(
            str(item.get("status") or "") in _UNRESOLVED_RECEIPT_STATUSES
            for item in safe_receipts
        )
        if unresolved_receipt and status is not WorkBoardStatus.blocked:
            raise BoardError(
                "unknown_effect_requires_reconciliation",
                "An unresolved effect cannot be projected as Review or Done",
            )
        if str(outcome) in _UNRESOLVED_RECEIPT_STATUSES and status is not WorkBoardStatus.blocked:
            raise BoardError(
                "unknown_effect_requires_reconciliation",
                "An unresolved outcome cannot be projected as Review or Done",
            )
        attempt.outcome = str(outcome)[:128]
        attempt.ended_at = observed_at
        attempt.lease_owner = None
        attempt.lease_expires_at = None
        attempt.updated_at = observed_at
        attempt.receipt_refs_json = _canonical_json(safe_receipts)
        values: dict[str, Any] = {
            "status": status,
            "task_revision": int(expected_revision) + 1,
            "result_refs_json": _canonical_json(safe_results),
            "artifact_refs_json": _canonical_json(safe_artifacts),
            "updated_at": observed_at,
        }
        if status is WorkBoardStatus.done:
            values.update(
                {
                    "completed_at": observed_at,
                    "block_kind": None,
                    "block_reason": None,
                    "block_source_status": None,
                }
            )
        elif status is WorkBoardStatus.blocked:
            values.update(
                {
                    "block_kind": (block_kind or "execution")[:128],
                    "block_reason": await self._safe_text(block_reason or outcome),
                    "block_source_status": WorkBoardStatus.running.value,
                    "completed_at": None,
                }
            )
        await self._cas_task_update(
            db,
            owner,
            task,
            expected_revision=int(expected_revision),
            values=values,
        )
        await db.flush()
        event = await self._event(
            db,
            task,
            owner,
            kind=f"attempt.{status.value}",
            metadata={
                "attempt_id": attempt_id,
                "status": status.value,
                "outcome": str(outcome)[:128],
                "verified_readback": bool(verified_readback),
                "task_revision": task.task_revision,
            },
            actor_principal_id=actor_principal_id or lease_owner,
            actor_session_id=actor_session_id or "work-board-dispatch",
        )
        return BoardAttemptProjection(task, attempt, event)

    async def list_pending_attempts(
        self,
        db: AsyncSession,
        *,
        limit: int = 100,
    ) -> list[WorkBoardAttempt]:
        result = await db.execute(
            select(WorkBoardAttempt)
            .where(
                WorkBoardAttempt.workflow_run_id.is_(None),
                WorkBoardAttempt.outcome == "pending_admission",
                WorkBoardAttempt.ended_at.is_(None),
            )
            .order_by(WorkBoardAttempt.created_at.asc())
            .limit(max(1, min(int(limit), 100)))
        )
        return list(result.scalars().all())

    async def list_linked_active_attempts(
        self,
        db: AsyncSession,
        *,
        limit: int = 20,
    ) -> list[tuple[WorkBoardTask, WorkBoardAttempt]]:
        """Return running board attempts whose durable root is already linked."""
        result = await db.execute(
            select(WorkBoardTask, WorkBoardAttempt)
            .join(WorkBoardAttempt, WorkBoardAttempt.task_id == WorkBoardTask.task_id)
            .where(
                WorkBoardTask.status == WorkBoardStatus.running,
                WorkBoardAttempt.workflow_run_id.is_not(None),
                WorkBoardAttempt.ended_at.is_(None),
            )
            .order_by(WorkBoardAttempt.created_at.asc(), WorkBoardAttempt.attempt_id.asc())
            .limit(max(1, min(int(limit), 100)))
        )
        return [(task, attempt) for task, attempt in result.all()]

    async def close_proved_absent_attempt(
        self,
        db: AsyncSession,
        task_id: str,
        attempt_id: str,
        *,
        expected_revision: int,
        board_fence: int,
        lease_owner: str,
        absence_proven: bool,
        actor_principal_id: str | None = None,
        actor_session_id: str | None = None,
        now: datetime | None = None,
    ) -> BoardMutation:
        """Discard a claim proven to have never reached durable admission.

        This is the only path that removes an attempt row. It is explicitly
        gated by the runtime binding lookup result so an ambiguous lookup or a
        transport error cannot spend a new claim or silently replay work.
        """

        if absence_proven is not True:
            raise BoardError(
                "admission_binding_unproven",
                "A pending claim may be discarded only after the durable binding is proven absent",
            )
        observed_at = now or _now()
        await _begin_sqlite_immediate(db)
        task = await self._find_task(db, task_id)
        if task is None:
            raise BoardNotFound(task_id)
        owner = WorkBoardOwner(
            principal_id=task.owner_principal_id,
            session_id=task.owner_session_id,
        )
        if task.status is not WorkBoardStatus.running:
            raise BoardError("task_not_running", "Only a running pending claim can be recovered")
        if task.task_revision != int(expected_revision):
            raise BoardRevisionConflict(task.task_id, int(expected_revision), task.task_revision)
        attempt = (
            await db.execute(
                select(WorkBoardAttempt).where(
                    WorkBoardAttempt.task_id == task_id,
                    WorkBoardAttempt.attempt_id == attempt_id,
                    WorkBoardAttempt.ended_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if attempt is None:
            raise BoardError("attempt_not_found", "The board attempt does not exist", status_code=404)
        if attempt.workflow_run_id:
            raise BoardError(
                "attempt_already_admitted",
                "An admitted attempt cannot be removed from the board history",
            )
        if attempt.outcome != "pending_admission":
            raise BoardError(
                "attempt_not_pending",
                "Only a pending admission claim can be discarded",
            )
        if attempt.fencing_token != int(board_fence) or attempt.lease_owner != lease_owner:
            raise BoardError("stale_fence", "The board attempt fence is stale")
        await db.delete(attempt)
        await db.flush()
        await self._cas_task_update(
            db,
            owner,
            task,
            expected_revision=int(expected_revision),
            values={
                "status": WorkBoardStatus.blocked,
                "block_source_status": WorkBoardStatus.running.value,
                "block_kind": "reconcile_admission_binding",
                "block_reason": await self._safe_text(
                    "No durable run exists for the pending admission binding; operator reconciliation is required."
                ),
                "task_revision": int(expected_revision) + 1,
                "updated_at": observed_at,
            },
        )
        event = await self._event(
            db,
            task,
            owner,
            kind="attempt.admission_absent",
            metadata={
                "attempt_id": attempt_id,
                "status": WorkBoardStatus.blocked.value,
                "block_kind": "reconcile_admission_binding",
                "recovery_action": "reconcile_admission_binding",
                "task_revision": task.task_revision,
            },
            actor_principal_id=actor_principal_id or lease_owner,
            actor_session_id=actor_session_id or "work-board-dispatch",
        )
        return BoardMutation(task, event)

    async def reclaim_expired_attempt(
        self,
        db: AsyncSession,
        task_id: str,
        attempt_id: str,
        *,
        expected_revision: int,
        lease_owner: str,
        lease_seconds: int = 300,
        now: datetime | None = None,
        actor_principal_id: str | None = None,
        actor_session_id: str | None = None,
    ) -> BoardDispatchClaim | None:
        """Fence an expired, unlinked board attempt and create its retry.

        This seam is intentionally limited to an attempt whose durable job
        link was never written.  A linked run, even if its worker lease has
        expired, first needs runtime reconciliation; creating a second board
        attempt there could replay an external effect.
        """

        observed_at = now or _now()
        lease_seconds = max(1, min(int(lease_seconds), 900))
        await _begin_sqlite_immediate(db)
        task = await self._find_task(db, task_id)
        if task is None:
            raise BoardNotFound(task_id)
        owner = WorkBoardOwner(
            principal_id=task.owner_principal_id,
            session_id=task.owner_session_id,
        )
        if task.status is not WorkBoardStatus.running:
            return None
        if task.task_revision != int(expected_revision):
            raise BoardRevisionConflict(task.task_id, int(expected_revision), task.task_revision)
        attempt = (
            await db.execute(
                select(WorkBoardAttempt).where(
                    WorkBoardAttempt.task_id == task_id,
                    WorkBoardAttempt.attempt_id == attempt_id,
                    WorkBoardAttempt.ended_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if attempt is None:
            raise BoardError("attempt_not_found", "The board attempt does not exist", status_code=404)
        if attempt.workflow_run_id:
            raise BoardError(
                "workflow_reconcile_required",
                "A linked durable run must be reconciled before a new board attempt can be claimed",
            )
        # Recovery is performed by the next dispatcher identity.  The old
        # owner is intentionally not required to match; the expired lease and
        # current task revision are the recovery fence.
        if not attempt.lease_owner or attempt.fencing_token <= 0:
            raise BoardError("stale_fence", "The board attempt fence is stale")
        if attempt.lease_expires_at is None or attempt.lease_expires_at > observed_at:
            raise BoardError("lease_active", "The board attempt lease is still active")

        attempt_count = int(
            await db.scalar(
                select(func.count(WorkBoardAttempt.attempt_id)).where(
                    WorkBoardAttempt.task_id == task.task_id
                )
            )
            or 0
        )
        if attempt_count >= 2:
            await self._cas_task_update(
                db,
                owner,
                task,
                expected_revision=int(expected_revision),
                values={
                    "status": WorkBoardStatus.blocked,
                    "block_source_status": WorkBoardStatus.running.value,
                    "block_kind": "attempt_limit",
                    "block_reason": await self._safe_text(
                        "The board attempt limit has been exhausted"
                    ),
                    "task_revision": int(expected_revision) + 1,
                    "updated_at": observed_at,
                },
            )
            event = await self._event(
                db,
                task,
                owner,
                kind="task.attempt_limit",
                metadata={
                    "status": task.status.value,
                    "task_revision": task.task_revision,
                    "attempt_id": attempt_id,
                },
                actor_principal_id=actor_principal_id or lease_owner,
                actor_session_id=actor_session_id or "work-board-dispatch",
            )
            return None

        # End the old attempt before inserting its replacement so the partial
        # unique active-attempt index remains true throughout the transaction.
        attempt.ended_at = observed_at
        attempt.lease_owner = None
        attempt.lease_expires_at = None
        attempt.updated_at = observed_at
        attempt.outcome = "lease_expired"
        await db.flush()
        replacement = WorkBoardAttempt(
            task_id=task.task_id,
            task_revision_at_claim=int(expected_revision),
            lease_owner=str(lease_owner)[:256],
            lease_expires_at=observed_at + timedelta(seconds=lease_seconds),
            heartbeat_at=observed_at,
            fencing_token=int(attempt.fencing_token) + 1,
            executor_id=(task.executor_id or "")[:128],
            started_at=observed_at,
            outcome="pending_admission",
        )
        db.add(replacement)
        await self._cas_task_update(
            db,
            owner,
            task,
            expected_revision=int(expected_revision),
            values={"task_revision": int(expected_revision) + 1, "updated_at": observed_at},
        )
        await db.flush()
        event = await self._event(
            db,
            task,
            owner,
            kind="attempt.reclaimed",
            metadata={
                "status": task.status.value,
                "task_revision": task.task_revision,
                "previous_attempt_id": attempt_id,
                "attempt_id": replacement.attempt_id,
                "fencing_token": replacement.fencing_token,
            },
            actor_principal_id=actor_principal_id or lease_owner,
            actor_session_id=actor_session_id or "work-board-dispatch",
        )
        return BoardDispatchClaim(task, replacement, event)

    async def block_attempt_unknown_effect(
        self,
        db: AsyncSession,
        task_id: str,
        attempt_id: str,
        *,
        expected_revision: int,
        board_fence: int,
        lease_owner: str,
        actor_principal_id: str | None = None,
        actor_session_id: str | None = None,
        now: datetime | None = None,
    ) -> BoardAttemptProjection:
        """Close a claim when admission/effect status cannot be proven.

        The fixed recovery code is deliberately used instead of carrying an
        exception or provider response into the board.  Unknown outcomes are
        never converted into an automatic retry.
        """

        return await self.project_attempt(
            db,
            task_id,
            attempt_id,
            expected_revision=expected_revision,
            board_fence=board_fence,
            lease_owner=lease_owner,
            status=WorkBoardStatus.blocked,
            outcome="unknown_external_effect",
            receipt_refs=[
                {
                    "reason_code": "unknown_effect",
                    "recovery_action": "reconcile_external_effect",
                }
            ],
            block_kind="unknown_effect",
            block_reason="The effect outcome is unknown; reconcile it before retrying.",
            actor_principal_id=actor_principal_id,
            actor_session_id=actor_session_id,
            now=now,
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
                    .where(WorkBoardComment.task_id == task.task_id)
                    .order_by(WorkBoardComment.created_at.asc())
                )
            ).scalars().all()
        )
        parents = list(
            (
                await db.execute(
                    select(WorkBoardLink.parent_task_id).where(
                        WorkBoardLink.child_task_id == task.task_id,
                        WorkBoardLink.owner_principal_id == owner.principal_id,
                        WorkBoardLink.owner_session_id == owner.session_id,
                    )
                )
            ).scalars().all()
        )
        children = list(
            (
                await db.execute(
                    select(WorkBoardLink.child_task_id).where(
                        WorkBoardLink.parent_task_id == task.task_id,
                        WorkBoardLink.owner_principal_id == owner.principal_id,
                        WorkBoardLink.owner_session_id == owner.session_id,
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
    "BoardDispatchClaim",
    "BoardAttemptProjection",
    "BoardMutation",
    "BoardNotFound",
    "BoardOwnerMismatch",
    "BoardPage",
    "BoardRevisionConflict",
    "WorkBoardRepository",
]
