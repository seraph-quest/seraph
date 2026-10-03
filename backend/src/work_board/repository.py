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
from typing import Any, Awaitable, Callable, Mapping

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.db.models import (
    GitHubFollowthroughConnection,
    Goal,
    OperatorSession,
    WorkBoardAttempt,
    WorkBoardComment,
    WorkBoardEvent,
    WorkBoardLink,
    WorkBoardProposal,
    WorkBoardReviewIntent,
    WorkBoardStatus,
    WorkBoardTask,
    WorkflowRunState,
)
from src.vault import redaction as vault_redaction
from src.goals.repository import deserialize_admission_budget
from src.work_board.contracts import (
    WORK_BOARD_AUTHENTICATED_BLOCK_KINDS,
    WorkBoardActionRequest,
    WorkBoardCommentCreate,
    WorkBoardLinkCreate,
    WorkBoardLinkDelete,
    WorkBoardOwner,
    WorkBoardTaskCreate,
    WorkBoardTaskPatch,
)


_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:/-]{1,512}$")
_SAFE_RECEIPT_IDENTIFIER = re.compile(r"^(?!\.{1,2}$)[A-Za-z0-9_.:-]{1,512}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_EFFECT_ID_DIGEST = re.compile(r"^[0-9a-f]{16}$", re.IGNORECASE)
_EVENT_LIMIT = 100
_TASK_LIMIT = 100
_MAX_PARENT_HANDOFF_CONTEXT_BYTES = 32_768
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
_SAFE_RESTORABLE_PHASES = frozenset(
    {
        WorkBoardStatus.triage.value,
        WorkBoardStatus.todo.value,
        WorkBoardStatus.ready.value,
        WorkBoardStatus.review.value,
    }
)
_UNRESOLVED_RECEIPT_STATUSES = {
    "unknown",
    "unknown_external_effect",
    "cost_liability",
    "intent",
    "dispatched",
}
_BOARD_BLOCK_KINDS = frozenset(
    {
        "operator",
        "dependency",
        "needs_input",
        "capability",
        "transient",
        "cancelled",
        "review_expired",
        "unknown_effect",
        "cost_liability",
        "reconcile_admission_binding",
        "attempt_limit",
    }
)
_BROWSER_CAPABILITY_ID = "browser.public-task.v1"
_BROWSER_GLOBAL_READY_CAPACITY = 8
_BROWSER_HARD_MAX_ATTEMPTS = 2
_BROWSER_LEGACY_MAX_OUTSTANDING = _BROWSER_GLOBAL_READY_CAPACITY
_PRIVATE_RECEIPT_TOKENS = (
    "private",
    "secret",
    "credential",
    "password",
    "token",
    "prompt",
    "payload",
)
_UNSAFE_RECEIPT_PATH_PARTS = frozenset(
    {
        ".aws",
        ".azure",
        ".config",
        ".docker",
        ".gnupg",
        ".ssh",
        "credential",
        "credentials",
        "private",
        "secret",
        "secrets",
        "token",
        "tokens",
        "vault",
    }
)


def effective_browser_limits(goal: Goal | None) -> tuple[int, int]:
    """Return server-derived browser attempt and outstanding-work limits.

    Browser tasks retain the historical two-attempt/eight-ready defaults for
    legacy goals without an admission-budget row.  Once a goal carries an
    admission budget, its finite limits become the authority for that goal;
    the browser lane can never raise ``max_attempts`` above its two-attempt
    hard cap.  The values are intentionally derived from the owner-checked
    ``Goal`` row and are never accepted from a typed browser input payload.
    """

    budget = deserialize_admission_budget(goal) if goal is not None else None
    if budget is None:
        return _BROWSER_HARD_MAX_ATTEMPTS, _BROWSER_LEGACY_MAX_OUTSTANDING
    return (
        max(1, min(_BROWSER_HARD_MAX_ATTEMPTS, int(budget.max_attempts))),
        max(1, int(budget.max_outstanding_jobs)),
    )


_UNSAFE_RECEIPT_FILE_TOKENS = (
    "api-key",
    "api_key",
    "apikey",
    "credential",
    "password",
    "private",
    "secret",
    "token",
)
_UNSAFE_RECEIPT_FILE_NAMES = frozenset(
    {
        ".env",
        ".env.dev",
        ".env.local",
        ".env.production",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "private_key",
    }
)
_UNSAFE_RECEIPT_FILE_SUFFIXES = (".key", ".p12", ".pem", ".pfx")


def _closed_block_kind(value: Any) -> str:
    code = str(value or "").strip().lower()
    if code in _BOARD_BLOCK_KINDS:
        return code
    if code in {
        "m5_memory_confirmation_required",
        "m5_memory_binding_blocked",
        "accepted_memory_requires_operator_confirmation",
    }:
        return "needs_input"
    if any(token in code for token in ("unknown", "effect", "cost", "reconcile")):
        return "unknown_effect"
    # A missing or malformed board input is a capability specification
    # prerequisite, not an approval request. Keep it out of the generic
    # "input" -> needs_input mapping below so the cockpit offers the
    # prerequisite-recheck path instead of linking to an unrelated approval.
    if code.startswith("typed_input_"):
        return "capability"
    if any(token in code for token in ("approval", "input", "consent", "review")):
        return "needs_input"
    if "handoff" in code or "depend" in code:
        return "dependency"
    if any(token in code for token in ("goal", "capability", "credential", "grant", "authority", "route", "config", "profile", "executor", "typed")):
        return "capability"
    if "cancel" in code:
        return "cancelled"
    return "transient"


def _registered_executor_lane(capability_id: Any) -> str | None:
    """Resolve the server-owned lane from the dispatcher registry.

    Import lazily because the dispatcher uses this repository for its board
    projection.  The helper is still the single registry-derived lane source;
    repository mutations never trust a caller-provided executor value.
    """

    from src.work_board.dispatcher import registered_executor_id

    return registered_executor_id(str(capability_id or "").strip())


def _executor_lane_error(
    capability_id: Any,
    executor_id: Any,
) -> tuple[str, str] | None:
    capability = str(capability_id or "").strip()
    if not capability:
        return None
    expected = _registered_executor_lane(capability)
    if expected is None:
        return (
            "capability_unregistered",
            "The task names no registered Seraph capability",
        )
    supplied = str(executor_id or "").strip()
    if not supplied:
        return "executor_missing", "The task has no registered executor"
    if supplied != expected:
        return (
            "executor_lane_mismatch",
            "The task executor does not match the registered capability lane",
        )
    return None


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


def _utc_datetime(value: datetime) -> datetime:
    """Normalize a persisted SQLite datetime before comparing eligibility."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _payload_digest(request: WorkBoardTaskCreate) -> str:
    payload = request.model_dump(mode="json")
    # The server fills the typed reference/digest from an already verified
    # input artifact.  Keep the legacy idempotency digest independent of that
    # derived metadata, while including the opaque artifact identity itself so
    # two reservations cannot replay the same task key interchangeably.
    if payload.get("input_artifact_id") is None:
        payload.pop("input_artifact_id", None)
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


def _validate_opaque_identifier(
    value: str | None,
    *,
    field: str,
    max_length: int = 512,
) -> None:
    """Reject path-shaped values for fields that name a capability or actor."""
    _validate_safe_identifier(value, field=field, max_length=max_length)
    if value is not None and "/" in str(value).strip():
        raise BoardError("invalid_reference", f"{field} must be an opaque identifier")


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


def _safe_receipt_type(value: Any) -> str | None:
    """Keep receipt type labels opaque and free of private-content markers."""

    if not isinstance(value, str):
        return None
    bounded = value.strip()
    if not bounded or len(bounded) > 512 or not _SAFE_RECEIPT_IDENTIFIER.fullmatch(bounded):
        return None
    lowered = bounded.casefold()
    if any(token in lowered for token in _PRIVATE_RECEIPT_TOKENS):
        return None
    return bounded


def _safe_receipt_path(value: Any) -> str | None:
    """Keep only relative, non-sensitive workspace path references."""

    if not isinstance(value, str):
        return None
    bounded = value.strip().replace("\\", "/")
    if (
        not bounded
        or len(bounded) > 512
        or bounded.startswith(("/", "~"))
        or "\x00" in bounded
        or any(part in {"", ".", ".."} for part in bounded.split("/"))
        or any(not (char.isalnum() or char in " ./_-") for char in bounded)
    ):
        return None
    parts = [part.casefold() for part in bounded.split("/")]
    file_name = parts[-1]
    if (
        set(parts) & _UNSAFE_RECEIPT_PATH_PARTS
        or file_name in _UNSAFE_RECEIPT_FILE_NAMES
        or file_name.endswith(_UNSAFE_RECEIPT_FILE_SUFFIXES)
        or any(token in file_name for token in _UNSAFE_RECEIPT_FILE_TOKENS)
    ):
        return None
    return bounded


def _safe_receipt_refs(
    value: Any,
    *,
    limit: int = 32,
    preserve_effect_ids: bool = False,
) -> list[dict[str, Any]]:
    """Keep only bounded, structured receipt references in board projections."""
    values = value if isinstance(value, (list, tuple)) else [value]
    allowed = {
        "artifact_id",
        "artifact_type",
        "receipt_kind",
        "readback_id",
        "verifier_id",
        "verification_id",
        "file_path",
        "content_sha256",
        "size_bytes",
        "exists",
        "effect_id",
        "effect_id_digest",
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
        "verified_at",
        "outcome",
        "learning",
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
                elif key == "effect_id_digest":
                    if not _EFFECT_ID_DIGEST.fullmatch(bounded):
                        continue
                    raw_effect_id = item.get("effect_id")
                    if isinstance(raw_effect_id, str) and raw_effect_id.strip():
                        expected = _text_digest(raw_effect_id.strip())[:16]
                        if bounded.lower() != expected:
                            continue
                    safe[key] = bounded.lower()
                elif key in {
                    "artifact_id",
                    "artifact_type",
                    "receipt_kind",
                    "readback_id",
                    "verifier_id",
                    "verification_id",
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
                            "readback_id",
                            "verifier_id",
                            "verification_id",
                            "effect_id",
                            "effect_type",
                            "job_id",
                            "workflow_run_id",
                            "child_job_id",
                        }
                        else _SAFE_ID
                    )
                    if len(bounded) > 512 or not identifier_pattern.fullmatch(bounded):
                        continue
                    if key in {"artifact_type", "effect_type"}:
                        safe_type = _safe_receipt_type(bounded)
                        if safe_type is None:
                            continue
                        safe[key] = safe_type
                        continue
                    if key == "receipt_kind":
                        if bounded not in {"effect", "readback"}:
                            continue
                        safe[key] = bounded
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
                    if key == "effect_id" and not preserve_effect_ids:
                        safe["effect_id_digest"] = _text_digest(bounded)[:16]
                    else:
                        safe[key] = bounded
                elif key == "verified_at":
                    if len(bounded) > 64 or "\n" in bounded or "\r" in bounded:
                        continue
                    safe[key] = bounded
                elif key == "learning":
                    # This is a closed typed outcome from the capability
                    # contract.  Never project arbitrary memory labels, and
                    # never turn absence into a learning claim.
                    if bounded == "no_learning":
                        safe[key] = bounded
                elif key in {"file_path", "target_path"}:
                    safe_path = _safe_receipt_path(bounded)
                    if safe_path is None:
                        continue
                    safe[key] = safe_path
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


def _projection_value(projection: Mapping[str, Any], field_name: str) -> Any:
    """Read an immutable durable-job field from its safe projection."""

    if field_name in projection:
        return projection.get(field_name)
    if field_name in {"owner_principal_id", "owner_kind", "service_id"}:
        owner = projection.get("owner")
        if isinstance(owner, Mapping):
            if field_name == "owner_principal_id":
                return owner.get("principal_id") or owner.get(field_name)
            if field_name == "owner_kind":
                return owner.get("kind") or owner.get(field_name)
            return owner.get(field_name)
    if field_name in {"idempotency_scope", "idempotency_key", "idempotency_binding"}:
        idempotency = projection.get("idempotency")
        if isinstance(idempotency, Mapping):
            short_name = {
                "idempotency_scope": "scope",
                "idempotency_key": "key",
                "idempotency_binding": "binding",
            }[field_name]
            return idempotency.get(short_name) or idempotency.get(field_name)
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
    # Server-derived, verified dependency context attached by the dispatcher
    # immediately after the fenced claim. It is never accepted from an API
    # request or persisted as task authority.
    parent_handoffs: tuple[dict[str, Any], ...] = ()


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
    async def _safe_text(value: str, *, db: AsyncSession | None = None) -> str:
        """Persist only vault-redacted bounded operator text.

        The redaction helper returns a fixed placeholder when the vault cannot
        be read with ``fail_closed=True``.  That keeps a database or API error
        from becoming a secret disclosure through a task card.
        """
        if db is not None:
            return await vault_redaction.redact_secrets_in_text_readonly(
                db,
                value,
                fail_closed=True,
            )
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
        goal_status = getattr(goal.status, "value", goal.status)
        if str(goal_status or "") != "active":
            raise BoardError(
                "goal_not_active",
                "The task goal is no longer active",
                status_code=409,
                goal_id=goal_id,
            )
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

    async def _validate_review_recovery(
        self,
        db: AsyncSession,
        task: WorkBoardTask,
    ) -> None:
        """Require durable, attempt-bound evidence before restoring Review.

        The dispatcher performs the live owner/session and goal preflight.  The
        repository must still enforce the immutable Review contract at the
        transition boundary because callers can reach this CAS kernel directly.
        A worker summary or a receipt for another durable run is not proof of
        the latest attempt's verified readback.
        """
        if not task.requires_review:
            raise BoardError(
                "reviewer_required",
                "Review recovery requires a task marked for review",
                status_code=409,
            )
        reviewer_id = str(task.reviewer_id or "").strip()
        if not reviewer_id:
            raise BoardError(
                "reviewer_required",
                "Review recovery requires a named reviewer",
                status_code=409,
            )
        _validate_opaque_identifier(reviewer_id, field="reviewer_id", max_length=128)

        latest_attempt = (
            await db.execute(
                select(WorkBoardAttempt)
                .where(WorkBoardAttempt.task_id == task.task_id)
                .order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest_attempt is None or latest_attempt.ended_at is None:
            raise BoardError(
                "attempt_reconcile_required",
                "Review recovery requires the latest execution attempt to be ended",
                status_code=409,
            )

        workflow_run_id = safe_workflow_run_id(latest_attempt.workflow_run_id)
        if not workflow_run_id:
            raise BoardError(
                "verified_readback_required",
                "Review recovery requires a linked durable workflow run",
                status_code=409,
            )
        try:
            receipt_refs = json.loads(latest_attempt.receipt_refs_json or "[]")
        except (TypeError, ValueError):
            receipt_refs = []
        if not isinstance(receipt_refs, list):
            receipt_refs = []
        for receipt in receipt_refs:
            if not isinstance(receipt, Mapping):
                continue
            # Review recovery is allowed to consume only the same typed,
            # inspectable readback proof that the live dispatcher stores.
            # Legacy summary receipts with a matching run ID and digest are
            # not enough to put a task back into Review.
            if str(receipt.get("receipt_kind") or "").strip() != "readback":
                continue
            if str(receipt.get("status") or "").strip() not in {
                "succeeded",
                "read_back",
                "reconciled",
            }:
                continue
            if not bool(receipt.get("verified")):
                continue
            if safe_workflow_run_id(receipt.get("workflow_run_id")) != workflow_run_id:
                continue
            if safe_sha256_digest(receipt.get("content_sha256")) is None:
                continue
            if not _SAFE_RECEIPT_IDENTIFIER.fullmatch(
                str(receipt.get("readback_id") or "").strip()
            ):
                continue
            verified_at = str(receipt.get("verified_at") or "").strip()
            if not verified_at or len(verified_at) > 64 or "\n" in verified_at or "\r" in verified_at:
                continue
            return
        raise BoardError(
            "verified_readback_required",
            "Review recovery requires verified readback for the linked workflow run and digest",
            status_code=409,
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
        db.info.setdefault("work_board_events_after_commit", []).append(event)
        return event

    @staticmethod
    def _validate_task_fields(request: WorkBoardTaskCreate) -> None:
        _validate_safe_identifier(request.goal_id, field="goal_id", max_length=128)
        _validate_opaque_identifier(request.capability_id, field="capability_id", max_length=128)
        _validate_opaque_identifier(request.input_artifact_id, field="input_artifact_id", max_length=512)
        _validate_safe_identifier(request.typed_input_ref, field="typed_input_ref")
        _validate_opaque_identifier(request.executor_id, field="executor_id", max_length=128)
        _validate_opaque_identifier(request.assignee_id, field="assignee_id", max_length=128)
        _validate_safe_identifier(request.idempotency_scope, field="idempotency_scope", max_length=128)
        _validate_safe_identifier(request.idempotency_key, field="idempotency_key", max_length=256)
        _validate_opaque_identifier(request.reviewer_id, field="reviewer_id", max_length=128)
        _validate_opaque_identifier(request.origin_thread_id, field="origin_thread_id", max_length=256)
        _validate_digest(request.typed_input_digest, field="typed_input_digest")

    async def create_task(
        self,
        db: AsyncSession,
        owner: WorkBoardOwner,
        request: WorkBoardTaskCreate,
        *,
        origin_session_id: str | None = None,
        publication_authority_check: Callable[[AsyncSession], Awaitable[None]] | None = None,
    ) -> BoardMutation:
        self._validate_task_fields(request)
        # Triage rows may remain unbound until Specify/Decompose acceptance,
        # but an executable Todo row must use the lane derived from the live
        # capability registry.  Normalize a missing lane and reject a forged
        # one before the idempotency digest is calculated.
        expected_executor = _registered_executor_lane(request.capability_id)
        supplied_executor = str(request.executor_id or "").strip() or None
        if expected_executor is None and supplied_executor is not None:
            raise BoardError(
                "executor_requires_capability",
                "An executor lane requires a registered capability",
                status_code=422,
            )
        if expected_executor is not None:
            if supplied_executor is not None and supplied_executor != expected_executor:
                raise BoardError(
                    "executor_lane_mismatch",
                    "The task executor does not match the registered capability lane",
                    status_code=422,
                )
            if request.status is WorkBoardStatus.todo:
                request = request.model_copy(update={"executor_id": expected_executor})
        if (
            request.status is WorkBoardStatus.todo
            and request.capability_id in {"browser.public-task.v1", "work.research-dossier.v1"}
            and not request.input_artifact_id
        ):
            raise BoardError(
                "browser_input_artifact_required",
                "Public browser tasks require a server-bound input artifact",
                status_code=422,
            )
        artifact = None
        if request.input_artifact_id or publication_authority_check is not None:
            # Reserve the writer before reading the owner/goal/artifact graph.
            # Those reads establish the authority that is bound by the task
            # insert; moving the fence after redaction would allow a stale
            # graph snapshot to be bound by a later mutation.
            await _begin_sqlite_immediate(db)
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
            if request.input_artifact_id:
                # Replays must remain idempotent after a successful browser
                # execution consumes its input artifact. The task already
                # stores the immutable artifact reference/digest, so a
                # replay only needs an owner-fenced metadata lookup; the
                # executable resolver is reserved for a new task or a live
                # dispatch and correctly rejects terminal artifact states.
                from src.work_board.input_artifacts import read_input_artifact_metadata

                artifact_metadata = await read_input_artifact_metadata(
                    db,
                    owner,
                    artifact_id=request.input_artifact_id,
                )
                if (
                    existing.input_artifact_id != request.input_artifact_id
                    or artifact_metadata.goal_id != request.goal_id
                    or int(artifact_metadata.goal_revision) != int(request.goal_revision)
                    or artifact_metadata.capability_id != (request.capability_id or "")
                    or existing.typed_input_ref != artifact_metadata.typed_input_ref
                    or existing.typed_input_digest != artifact_metadata.typed_input_digest
                ):
                    raise BoardIdempotencyConflict(request.idempotency_scope, request.idempotency_key)
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

        # Capability-specific checks belong after the repository acquires its
        # writer transaction. A check performed by the caller before this
        # method can be separated from publication by the transaction boundary
        # above. This server-only callback neither grants authority nor commits
        # the transaction; failure prevents a new task and its artifact binding.
        if publication_authority_check is not None:
            await publication_authority_check(db)

        await self._validate_goal(
            db,
            owner,
            goal_id=request.goal_id,
            goal_revision=request.goal_revision,
        )
        if request.input_artifact_id:
            if artifact is None:
                from src.work_board.input_artifacts import resolve_input_artifact_for_task

                artifact = await resolve_input_artifact_for_task(
                    db,
                    owner,
                    artifact_id=request.input_artifact_id,
                    goal_id=request.goal_id,
                    goal_revision=request.goal_revision,
                    capability_id=request.capability_id or "",
                )
            typed_input_ref = artifact.row.typed_input_ref
            typed_input_digest = artifact.row.payload_sha256
        else:
            typed_input_ref = request.typed_input_ref
            typed_input_digest = request.typed_input_digest
        reviewer_id = request.reviewer_id
        if request.requires_review:
            if reviewer_id is not None and reviewer_id != owner.principal_id:
                raise BoardError(
                    "reviewer_binding_mismatch",
                    "The named reviewer is bound to the authenticated task owner",
                    status_code=403,
                )
            reviewer_id = owner.principal_id
        safe_title = await self._safe_text(request.title, db=db)
        safe_body = await self._safe_text(request.body, db=db)

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
            input_artifact_id=request.input_artifact_id,
            typed_input_ref=typed_input_ref,
            typed_input_digest=typed_input_digest,
            executor_id=request.executor_id,
            assignee_id=request.assignee_id,
            priority=request.priority,
            idempotency_scope=request.idempotency_scope,
            idempotency_key=request.idempotency_key,
            idempotency_payload_digest=digest,
            scheduled_at=request.scheduled_at,
            status=request.status,
            requires_review=request.requires_review,
            reviewer_id=reviewer_id,
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
        if artifact is not None:
            from src.work_board.input_artifacts import bind_input_artifact

            await bind_input_artifact(
                db,
                owner,
                artifact=artifact,
                task_id=task.task_id,
                task_revision=task.task_revision,
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
        recovered_read_scopes: dict[str, str] | None = None,
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
        from src.auth.ownership import read_scope_clause
        statement = select(WorkBoardTask).where(
            read_scope_clause(WorkBoardTask.task_id, WorkBoardTask.owner_session_id, owner.session_id, recovered_read_scopes or {}, principal_column=WorkBoardTask.owner_principal_id, current_principal=owner.principal_id),
        )
        if status is not None:
            statement = statement.where(WorkBoardTask.status == status)
        if executor_id:
            _validate_opaque_identifier(executor_id, field="executor_id", max_length=128)
            statement = statement.where(WorkBoardTask.executor_id == executor_id)
        if assignee_id:
            _validate_opaque_identifier(assignee_id, field="assignee_id", max_length=128)
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
        closed_kind = _closed_block_kind(block_kind)
        await self._cas_task_update(
            db,
            owner,
            task,
            expected_revision=int(expected_revision),
            values={
                "status": WorkBoardStatus.blocked,
                "block_source_status": WorkBoardStatus.ready.value,
                "block_kind": closed_kind,
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
                "block_kind": closed_kind,
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
        if task.input_artifact_id and {
            "capability_id",
            "typed_input_ref",
            "typed_input_digest",
            "executor_id",
            "goal_id",
        }.intersection(changes):
            raise BoardError(
                "input_artifact_immutable",
                "An artifact-bound task cannot change its executable input authority",
                status_code=409,
            )
        safe_changes: dict[str, Any] = {}
        for field, value in changes.items():
            if field in {"title", "body"}:
                safe_changes[field] = await self._safe_text(str(value))
            else:
                if field in {"capability_id", "executor_id", "assignee_id"}:
                    _validate_opaque_identifier(value, field=field, max_length=128)
                elif field == "typed_input_ref":
                    _validate_safe_identifier(value, field=field, max_length=512)
                elif field == "typed_input_digest":
                    _validate_digest(value, field=field)
                safe_changes[field] = value
        post_capability = safe_changes.get("capability_id", task.capability_id)
        expected_executor = _registered_executor_lane(post_capability)
        explicit_executor = "executor_id" in safe_changes
        supplied_executor = safe_changes.get("executor_id", task.executor_id)
        if expected_executor is None:
            if explicit_executor and supplied_executor is not None:
                raise BoardError(
                    "executor_requires_capability",
                    "An executor lane requires a registered capability",
                    status_code=422,
                )
            if "capability_id" in safe_changes and safe_changes.get("capability_id") is None:
                safe_changes["executor_id"] = None
        if expected_executor is not None:
            if supplied_executor is not None and supplied_executor != expected_executor:
                raise BoardError(
                    "executor_lane_mismatch",
                    "The task executor does not match the registered capability lane",
                    status_code=422,
                )
            executable_phase = task.status in {
                WorkBoardStatus.todo,
                WorkBoardStatus.ready,
            } or task.block_source_status in {
                WorkBoardStatus.todo.value,
                WorkBoardStatus.ready.value,
            }
            # A typed executable task always carries the server-derived lane.
            # Triage may intentionally remain unbound until acceptance; an
            # explicit non-empty lane there is still checked above.
            if executable_phase or (explicit_executor and supplied_executor is not None):
                safe_changes["executor_id"] = expected_executor
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
        if task.pipeline_operation_id and (authority_fields | {"priority"}).intersection(safe_changes):
            raise BoardError("pipeline_review_required", "Change unfinished pipeline scope through exact plan review", status_code=409)
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
                if not task.capability_id:
                    raise BoardError(
                        "capability_required",
                        "Triage must name a registered capability before promotion",
                    )
                expected_executor = _registered_executor_lane(task.capability_id)
                if expected_executor is not None and task.executor_id and task.executor_id != expected_executor:
                    raise BoardError(
                        "executor_lane_mismatch",
                        "The task executor does not match the registered capability lane",
                        status_code=409,
                    )
                values["status"] = WorkBoardStatus.todo
                if expected_executor is not None:
                    values["executor_id"] = expected_executor
            elif task.status is WorkBoardStatus.todo:
                raise BoardError(
                    "dispatcher_readiness_required",
                    "Todo becomes Ready only after the governed dispatcher verifies capability authority",
                )
            else:
                raise BoardError("illegal_transition", "This task cannot be promoted from its current status")
        elif request.action.value == "block":
            if request.block_kind not in WORK_BOARD_AUTHENTICATED_BLOCK_KINDS:
                raise BoardError(
                    "invalid_block_kind",
                    "Manual board blocking accepts only a bounded recovery block kind",
                    status_code=422,
                )
            if task.status not in {
                WorkBoardStatus.triage,
                WorkBoardStatus.todo,
                WorkBoardStatus.ready,
                WorkBoardStatus.review,
            }:
                raise BoardError("illegal_transition", "This task cannot be manually blocked from its current status")
            if task.status is WorkBoardStatus.review:
                raise BoardError(
                    "review_typed_recovery_required",
                    "Review tasks use reviewer completion or typed expiry recovery",
                    status_code=409,
                )
            if not str(request.reason or "").strip():
                raise BoardError("reason_required", "Manual blocking requires a bounded reason", status_code=422)
            # The source phase is part of the authenticated action contract.
            # Compare it to the row loaded under the owner and expected
            # revision before persisting; never infer or trust a caller's
            # claimed prior phase.
            if request.source_status is None:
                raise BoardError(
                    "source_status_required",
                    "Manual blocking requires the current source status",
                    status_code=422,
                )
            if request.source_status is not task.status:
                raise BoardError(
                    "stale_source_status",
                    "The block source status no longer matches the current task",
                    status_code=409,
                )
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
                    "block_source_status": request.source_status.value,
                    "block_kind": request.block_kind,
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
            source = task.block_source_status
            if source not in _SAFE_RESTORABLE_PHASES:
                raise BoardError(
                    "invalid_recovery_phase",
                    "The blocked task has no safe restorable prior phase",
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
            if source == WorkBoardStatus.review.value:
                await self._validate_review_recovery(db, task)
                if task.review_expires_at is None:
                    raise BoardError(
                        "review_renewal_required",
                        "A Review task without an active expiry must be renewed by the named reviewer",
                        status_code=409,
                    )
            values.update(
                {
                    # M1 has no capability/authority readiness resolver.  A
                    # task previously blocked from Ready must re-enter Todo
                    # so M2 can re-admit it against current authority.
                    "status": (
                        WorkBoardStatus.todo
                        if source == WorkBoardStatus.ready.value
                        else WorkBoardStatus(source)
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
                "source_status": task.block_source_status,
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
        live_goal = await self.validate_task_goal(db, owner, task)
        if task.scheduled_at is not None and _utc_datetime(task.scheduled_at) > _now():
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
        max_attempts = 2
        if task.capability_id == _BROWSER_CAPABILITY_ID:
            max_attempts, _max_outstanding_jobs = effective_browser_limits(live_goal)
        if attempt_count >= max_attempts:
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
                    else [],
                    preserve_effect_ids=True,
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
                "review_request_attempt_id": None,
                "review_request_fence": None,
                "review_request_revision": None,
                "review_request_digest": None,
                "review_request_evidence_json": "[]",
                "review_requested_at": None,
                "review_expires_at": None,
                "task_revision": expected + 1,
                "updated_at": _now(),
            },
        )
        # A retry is a new attempt boundary.  Preserve old intent rows as
        # history, but make every still-pending intent terminal so it cannot
        # later project Review for the replacement attempt.
        await db.execute(
            update(WorkBoardReviewIntent)
            .where(
                WorkBoardReviewIntent.task_id == task.task_id,
                WorkBoardReviewIntent.owner_principal_id == owner.principal_id,
                WorkBoardReviewIntent.owner_session_id == owner.session_id,
                WorkBoardReviewIntent.status == "pending",
            )
            .values(status="superseded")
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
        *,
        acquire_lock: bool = True,
    ) -> tuple[WorkBoardLink, WorkBoardEvent]:
        if request.parent_task_id == request.child_task_id:
            raise BoardError("dependency_cycle", "A task cannot depend on itself")
        # The cycle check and link insert are one cross-process critical
        # section.  This must happen before the first graph read.  Proposal
        # acceptance already owns one enclosing transaction and lock across
        # child creation plus every edge; it passes acquire_lock=False so a
        # duplicate edge cannot commit the earlier child rows implicitly.
        if acquire_lock:
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
        if parent.status is WorkBoardStatus.done:
            # The edge and its immutable proof must commit together.  A
            # completed parent with no verified readback aborts this whole
            # transaction, leaving no dependency edge to bypass provenance.
            from src.work_board.review import materialize_handoff_for_link

            handoff = await materialize_handoff_for_link(
                db,
                owner,
                parent,
                child,
                link,
            )
        else:
            handoff = None
        event = await self._event(
            db,
            child,
            owner,
            kind="dependency.added",
            metadata={
                "parent_task_id": parent.task_id,
                "task_revision": child.task_revision,
                "ready_demoted": demote_ready,
                "handoff_id": handoff.handoff_id if handoff is not None else None,
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
        ready_result = await db.execute(
            select(WorkBoardTask)
            .where(
                WorkBoardTask.status == WorkBoardStatus.ready,
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
        ready_tasks = list(ready_result.scalars().all())
        remaining = bounded_limit - len(ready_tasks)
        if remaining <= 0:
            return ready_tasks
        # Promotion candidates are read in their own bounded phase.  A large
        # Todo backlog can therefore never consume the pass window reserved
        # for already-admitted Ready work.
        todo_result = await db.execute(
            select(WorkBoardTask)
            .where(
                WorkBoardTask.status == WorkBoardStatus.todo,
                ~select(WorkBoardProposal.proposal_id)
                .where(
                    WorkBoardProposal.owner_principal_id == WorkBoardTask.owner_principal_id,
                    WorkBoardProposal.owner_session_id == WorkBoardTask.owner_session_id,
                    WorkBoardProposal.parent_task_id == WorkBoardTask.task_id,
                    WorkBoardProposal.parent_revision == WorkBoardTask.task_revision,
                    WorkBoardProposal.status.in_(("pending_inference", "proposed")),
                )
                .exists(),
                (
                    WorkBoardTask.scheduled_at.is_(None)
                    | (WorkBoardTask.scheduled_at <= observed_at)
                ),
            )
            .order_by(
                WorkBoardTask.priority.desc(),
                WorkBoardTask.creation_sequence.asc(),
            )
            .limit(remaining)
        )
        return ready_tasks + list(todo_result.scalars().all())

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
        if task.scheduled_at is not None and _utc_datetime(task.scheduled_at) > _utc_datetime(observed_at):
            return None

        pending_proposal = await db.scalar(
            select(WorkBoardProposal.proposal_id)
            .where(
                WorkBoardProposal.owner_principal_id == task.owner_principal_id,
                WorkBoardProposal.owner_session_id == task.owner_session_id,
                WorkBoardProposal.parent_task_id == task.task_id,
                WorkBoardProposal.parent_revision == task.task_revision,
                WorkBoardProposal.status.in_(("pending_inference", "proposed")),
            )
            .limit(1)
        )
        if pending_proposal is not None:
            return None

        owner = WorkBoardOwner(
            principal_id=task.owner_principal_id,
            session_id=task.owner_session_id,
        )
        if task.capability_id == _BROWSER_CAPABILITY_ID:
            live_goal_for_limits = await db.scalar(
                select(Goal).where(
                    Goal.id == task.goal_id,
                    Goal.owner_principal_id == task.owner_principal_id,
                    Goal.owner_session_id == task.owner_session_id,
                )
            )
            _browser_max_attempts, browser_max_outstanding = effective_browser_limits(
                live_goal_for_limits
            )
            browser_outstanding = int(
                await db.scalar(
                    select(func.count(WorkBoardTask.task_id)).where(
                        WorkBoardTask.owner_principal_id == task.owner_principal_id,
                        WorkBoardTask.owner_session_id == task.owner_session_id,
                        WorkBoardTask.goal_id == task.goal_id,
                        WorkBoardTask.capability_id == _BROWSER_CAPABILITY_ID,
                        WorkBoardTask.status.in_(
                            (WorkBoardStatus.ready, WorkBoardStatus.running)
                        ),
                    )
                )
                or 0
            )
            if browser_outstanding >= browser_max_outstanding:
                reason = "browser_goal_outstanding_capacity"
                if task.block_reason != reason:
                    task.block_reason = reason
                    await db.flush()
                    await self._event(
                        db,
                        task,
                        owner,
                        kind="task.browser_goal_capacity",
                        metadata={
                            "status": WorkBoardStatus.todo.value,
                            "task_revision": task.task_revision,
                            "block_reason": reason,
                        },
                        actor_principal_id=actor_principal_id,
                        actor_session_id=actor_session_id or "work-board-dispatch",
                    )
                return None
            # Eight Ready browser rows is the global bounded admission cap.
            # This check runs under the same SQLite writer fence as the CAS
            # promotion, so concurrent owners cannot over-admit the lane.
            browser_ready = int(
                await db.scalar(
                    select(func.count(WorkBoardTask.task_id)).where(
                        WorkBoardTask.status == WorkBoardStatus.ready,
                        WorkBoardTask.capability_id == _BROWSER_CAPABILITY_ID,
                    )
                )
                or 0
            )
            if browser_ready >= _BROWSER_GLOBAL_READY_CAPACITY:
                if task.block_reason != "browser_ready_capacity":
                    task.block_reason = "browser_ready_capacity"
                    await db.flush()
                    await self._event(
                        db,
                        task,
                        owner,
                        kind="task.browser_ready_capacity",
                        metadata={
                            "status": WorkBoardStatus.todo.value,
                            "task_revision": task.task_revision,
                            "block_reason": "browser_ready_capacity",
                        },
                        actor_principal_id=actor_principal_id,
                        actor_session_id=actor_session_id or "work-board-dispatch",
                    )
                return None
        parent_rows = list(
            (
                await db.execute(
                    select(
                        WorkBoardTask.task_id,
                        WorkBoardTask.task_revision,
                        WorkBoardTask.status,
                        WorkBoardLink.link_id,
                        WorkBoardLink.current_handoff_id,
                    )
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
            ).all()
        )
        if any(
            status is not WorkBoardStatus.done
            for _parent_id, _parent_revision, status, _link_id, _handoff_id in parent_rows
        ):
            return None
        if any(
            not handoff_id
            for _parent_id, _parent_revision, _status, _link_id, handoff_id in parent_rows
        ):
            readiness_error = readiness_error or "handoff_materialization_required"
            from src.work_board.review import _HANDOFF_RECONCILIATION_REASON

            readiness_reason = readiness_reason or _HANDOFF_RECONCILIATION_REASON
        if not readiness_error:
            from src.work_board.review import (
                _HANDOFF_RECONCILIATION_REASON,
                current_handoff_is_verified,
            )

            for parent_id, _parent_revision, _status, link_id, _handoff_id in parent_rows:
                # ``creation_sequence`` is WorkBoardTask's SQLAlchemy primary
                # key; task_id is the public relationship key. Resolve the
                # parent by the latter rather than passing task_id to
                # AsyncSession.get(), which would silently return None.
                parent = await self._find_task(db, parent_id)
                link = (
                    await db.execute(
                        select(WorkBoardLink).where(
                            WorkBoardLink.link_id == link_id,
                            WorkBoardLink.child_task_id == task.task_id,
                            WorkBoardLink.owner_principal_id == task.owner_principal_id,
                            WorkBoardLink.owner_session_id == task.owner_session_id,
                        )
                    )
                ).scalar_one_or_none()
                if (
                    parent is None
                    or link is None
                    or not await current_handoff_is_verified(db, owner, parent, task, link)
                ):
                    readiness_error = "handoff_materialization_required"
                    readiness_reason = _HANDOFF_RECONCILIATION_REASON
                    break

        lane_error = _executor_lane_error(task.capability_id, task.executor_id)
        if lane_error is not None:
            readiness_error = readiness_error or lane_error[0]
            readiness_reason = readiness_reason or lane_error[1]

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
                "block_kind": _closed_block_kind(readiness_error),
                "block_reason": safe_reason[:1000],
            }
            event_kind = "task.dispatch_blocked"
            metadata = {
                "status": WorkBoardStatus.blocked.value,
                "block_kind": _closed_block_kind(readiness_error),
            }
        else:
            values = {"status": WorkBoardStatus.ready}
            if task.block_reason in {
                "browser_ready_capacity",
                "browser_goal_outstanding_capacity",
            }:
                values["block_reason"] = None
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
        if task.scheduled_at is not None and _utc_datetime(task.scheduled_at) > _utc_datetime(observed_at):
            return None

        owner = WorkBoardOwner(
            principal_id=task.owner_principal_id,
            session_id=task.owner_session_id,
        )
        browser_max_attempts = _BROWSER_HARD_MAX_ATTEMPTS
        browser_max_outstanding = _BROWSER_LEGACY_MAX_OUTSTANDING
        # Re-read the owner-bound goal inside the same immediate transaction
        # that creates the board claim.  The preflight pass is advisory; a
        # concurrent revision/owner/status change must not launch stale work.
        try:
            lane_error = _executor_lane_error(task.capability_id, task.executor_id)
            if lane_error is not None:
                raise BoardError(lane_error[0], lane_error[1])
            live_goal = await self.validate_task_goal(db, owner, task)
            live_goal_status = str(getattr(live_goal.status, "value", live_goal.status) or "")
            if live_goal_status and live_goal_status != "active":
                raise BoardError("goal_not_admitted", "The task goal is not currently executable")
            if task.capability_id == _BROWSER_CAPABILITY_ID:
                browser_max_attempts, browser_max_outstanding = effective_browser_limits(live_goal)
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
                    "block_kind": _closed_block_kind(exc.code),
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
                metadata={"status": WorkBoardStatus.blocked.value, "block_kind": _closed_block_kind(exc.code)},
                actor_principal_id=actor_principal_id or lease_owner,
                actor_session_id=actor_session_id or "work-board-dispatch",
            )
            return None

        if task.capability_id == _BROWSER_CAPABILITY_ID:
            # A task can remain Ready after a goal budget is tightened or
            # after an older process promoted siblings under a prior budget.
            # Recheck the owner/goal queue capacity under the same writer
            # fence as the claim so a stale Ready row cannot launch beyond
            # the current server-owned max_outstanding_jobs.
            browser_outstanding = int(
                await db.scalar(
                    select(func.count(WorkBoardTask.task_id)).where(
                        WorkBoardTask.owner_principal_id == task.owner_principal_id,
                        WorkBoardTask.owner_session_id == task.owner_session_id,
                        WorkBoardTask.goal_id == task.goal_id,
                        WorkBoardTask.capability_id == _BROWSER_CAPABILITY_ID,
                        WorkBoardTask.task_id != task.task_id,
                        WorkBoardTask.status.in_(
                            (WorkBoardStatus.ready, WorkBoardStatus.running)
                        ),
                    )
                )
                or 0
            )
            if browser_outstanding >= browser_max_outstanding:
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

        parent_handoff_context: list[dict[str, Any]] = []
        parent_handoff_digest: str | None = None
        if parent_statuses:
            # Capture the exact source-verified parent payload while the task
            # claim transaction is still open.  The attempt and every
            # downstream durable input then share one immutable binding.
            from src.work_board.review import parent_handoffs

            parent_handoff_context = await parent_handoffs(db, owner, task)
            if (
                len(parent_handoff_context) != len(parent_statuses)
                or any(
                    not isinstance(item, Mapping) or item.get("status") != "verified"
                    for item in parent_handoff_context
                )
            ):
                return None
            parent_handoff_context.sort(
                key=lambda item: (str(item.get("parent_task_id") or ""), str(item.get("handoff_id") or ""))
            )
            encoded_handoffs = _canonical_json(parent_handoff_context)
            if len(encoded_handoffs.encode("utf-8")) > _MAX_PARENT_HANDOFF_CONTEXT_BYTES:
                return None
            parent_handoff_digest = hashlib.sha256(encoded_handoffs.encode("utf-8")).hexdigest()

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
        attempt_limit = browser_max_attempts if task.capability_id == _BROWSER_CAPABILITY_ID else 2
        if attempt_count >= attempt_limit:
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
                metadata={
                    "status": task.status.value,
                    "task_revision": task.task_revision,
                    "block_kind": "attempt_limit",
                },
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
            parent_handoff_context_json=_canonical_json(parent_handoff_context),
            parent_handoff_digest=parent_handoff_digest,
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
        return BoardDispatchClaim(task, attempt, event, tuple(parent_handoff_context))

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
            # Idempotent replay is still a fenced mutation boundary.  A
            # stale worker cannot use the same run identity to bypass the
            # current task revision or board lease checks.
            if task.task_revision != int(expected_revision):
                raise BoardRevisionConflict(task.task_id, int(expected_revision), task.task_revision)
            if attempt.fencing_token != int(board_fence) or attempt.lease_owner != lease_owner:
                raise BoardError("stale_fence", "The board attempt fence is stale", status_code=409)
            latest = (
                await db.execute(
                    select(WorkBoardEvent)
                    .where(WorkBoardEvent.task_id == task_id)
                    .where(WorkBoardEvent.kind == "attempt.linked")
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

    async def pause_routine_attempt_for_operator(
        self,
        db: AsyncSession,
        task_id: str,
        attempt_id: str,
        *,
        expected_revision: int,
        board_fence: int,
        lease_owner: str | None,
        workflow_run_id: str,
        durable_fence: int,
        reason: str,
        actor_principal_id: str,
        actor_session_id: str,
        capability_id: str = "guardian-routine.v1",
        now: datetime | None = None,
    ) -> BoardAttemptProjection:
        """Project a routine's explicit human wait as Blocked and release its lease.

        The attempt stays open and linked to the same durable routine run. This
        records a suspended attempt, not a completed retry. A later explicit
        publication recovery reacquires a fresh board lease and updates the
        same attempt fence to match the durable run's new lease.
        """

        safe_reasons = {
            "awaiting_approval",
            "awaiting_publication_preview",
            "awaiting_publication_approval",
            "external_mutation_grant_required",
        }
        if capability_id == "engineering.repo-repair.v1":
            safe_reasons = {"repo_repair_code_egress_review", "review_repo_repair_proposal"}
        if reason not in safe_reasons:
            raise BoardError("routine_wait_reason_invalid", "The governed wait reason is not an operator recovery state")
        observed_at = now or _now()
        await _begin_sqlite_immediate(db)
        task = await self._find_task(db, task_id)
        if task is None:
            raise BoardNotFound(task_id)
        if task.capability_id != capability_id:
            raise BoardError("routine_wait_not_supported", "The capability does not support this operator wait")
        if task.task_revision != int(expected_revision):
            raise BoardRevisionConflict(task.task_id, int(expected_revision), task.task_revision)
        if task.status not in {WorkBoardStatus.running, WorkBoardStatus.blocked}:
            raise BoardError("task_not_recoverable", "Only the current routine attempt can enter an operator wait")
        attempt = (
            await db.execute(
                select(WorkBoardAttempt).where(
                    WorkBoardAttempt.task_id == task_id,
                    WorkBoardAttempt.attempt_id == attempt_id,
                )
            )
        ).scalar_one_or_none()
        if attempt is None or attempt.workflow_run_id != workflow_run_id:
            raise BoardError("attempt_workflow_conflict", "The routine attempt does not link to this durable run")
        if attempt.ended_at is not None or attempt.fencing_token != int(board_fence):
            raise BoardError("stale_fence", "The routine attempt fence is stale")
        if int(durable_fence) < int(attempt.fencing_token):
            raise BoardError("stale_fence", "The durable routine fence moved backwards")
        if task.status is WorkBoardStatus.running and attempt.lease_owner != lease_owner:
            raise BoardError("stale_fence", "The running routine attempt lease owner is stale")
        if task.status is WorkBoardStatus.blocked and (
            attempt.lease_owner is not None or attempt.lease_expires_at is not None
        ):
            raise BoardError("stale_fence", "A blocked routine attempt cannot retain an execution lease")

        changed = (
            task.status is not WorkBoardStatus.blocked
            or task.block_reason != reason
            or int(attempt.fencing_token) != int(durable_fence)
            or attempt.lease_owner is not None
            or attempt.lease_expires_at is not None
        )
        if not changed:
            return BoardAttemptProjection(task, attempt, None)

        attempt.fencing_token = int(durable_fence)
        attempt.lease_owner = None
        attempt.lease_expires_at = None
        attempt.outcome = reason
        attempt.updated_at = observed_at
        await self._cas_task_update(
            db,
            WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id),
            task,
            expected_revision=int(expected_revision),
            values={
                "status": WorkBoardStatus.blocked,
                "block_kind": "needs_input",
                "block_reason": reason,
                "block_source_status": WorkBoardStatus.running.value,
                "task_revision": int(expected_revision) + 1,
                "updated_at": observed_at,
            },
        )
        await db.flush()
        event = await self._event(
            db,
            task,
            WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id),
            kind="task.blocked_for_operator",
            metadata={
                "attempt_id": attempt.attempt_id,
                "workflow_run_id": workflow_run_id,
                "status": WorkBoardStatus.blocked.value,
                "block_kind": "needs_input",
                "block_reason": reason,
                "fencing_token": int(durable_fence),
                "task_revision": task.task_revision,
            },
            actor_principal_id=actor_principal_id,
            actor_session_id=actor_session_id,
        )
        return BoardAttemptProjection(task, attempt, event)

    async def resume_routine_attempt_for_operator_recovery(
        self,
        db: AsyncSession,
        task_id: str,
        attempt_id: str,
        *,
        expected_revision: int,
        previous_fence: int,
        next_fence: int,
        lease_owner: str,
        lease_seconds: int = 300,
        workflow_run_id: str,
        actor_principal_id: str,
        actor_session_id: str,
        capability_id: str = "guardian-routine.v1",
        now: datetime | None = None,
    ) -> BoardAttemptProjection:
        """Reacquire the same suspended routine attempt after approval."""

        observed_at = now or _now()
        if int(next_fence) != int(previous_fence) + 1:
            raise BoardError("stale_fence", "Routine recovery must advance exactly one fence")
        await _begin_sqlite_immediate(db)
        task = await self._find_task(db, task_id)
        if task is None:
            raise BoardNotFound(task_id)
        if task.capability_id != capability_id:
            raise BoardError("routine_wait_not_supported", "The capability does not support this operator recovery")
        if task.status is not WorkBoardStatus.blocked or task.task_revision != int(expected_revision):
            raise BoardError("stale_revision", "The blocked routine card changed before recovery")
        allowed_reasons = {
            "awaiting_approval",
            "awaiting_publication_preview",
            "awaiting_publication_approval",
            "external_mutation_grant_required",
        }
        if capability_id == "engineering.repo-repair.v1":
            allowed_reasons = {"repo_repair_code_egress_review", "review_repo_repair_proposal"}
        if task.block_reason not in allowed_reasons:
            raise BoardError("task_not_recoverable", "The routine card is not waiting for publication review")
        attempt = (
            await db.execute(
                select(WorkBoardAttempt).where(
                    WorkBoardAttempt.task_id == task_id,
                    WorkBoardAttempt.attempt_id == attempt_id,
                )
            )
        ).scalar_one_or_none()
        if (
            attempt is None
            or attempt.workflow_run_id != workflow_run_id
            or attempt.ended_at is not None
            or int(attempt.fencing_token) != int(previous_fence)
            or attempt.lease_owner is not None
            or attempt.lease_expires_at is not None
        ):
            raise BoardError("stale_fence", "The suspended routine attempt no longer owns this recovery")

        attempt.fencing_token = int(next_fence)
        attempt.lease_owner = str(lease_owner)[:256]
        attempt.lease_expires_at = observed_at + timedelta(seconds=max(1, min(int(lease_seconds), 900)))
        attempt.heartbeat_at = observed_at
        attempt.outcome = "operator_recovery_running"
        attempt.updated_at = observed_at
        owner = WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id)
        await self._cas_task_update(
            db,
            owner,
            task,
            expected_revision=int(expected_revision),
            values={
                "status": WorkBoardStatus.running,
                "block_kind": None,
                "block_reason": None,
                "block_source_status": None,
                "task_revision": int(expected_revision) + 1,
                "updated_at": observed_at,
            },
        )
        await db.flush()
        event = await self._event(
            db,
            task,
            owner,
            kind="task.operator_recovery_started",
            metadata={
                "attempt_id": attempt.attempt_id,
                "workflow_run_id": workflow_run_id,
                "status": WorkBoardStatus.running.value,
                "fencing_token": int(next_fence),
                "task_revision": task.task_revision,
            },
            actor_principal_id=actor_principal_id,
            actor_session_id=actor_session_id,
        )
        return BoardAttemptProjection(task, attempt, event)

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
        reconciled_github_root: Mapping[str, Any] | None = None,
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
                or not _SAFE_RECEIPT_IDENTIFIER.fullmatch(str(verified_readback.get("readback_id") or "").strip())
                or not str(verified_readback.get("verified_at") or "").strip()
                or len(str(verified_readback.get("verified_at") or "").strip()) > 64
                or "\n" in str(verified_readback.get("verified_at") or "")
                or "\r" in str(verified_readback.get("verified_at") or "")
            ):
                raise BoardError(
                    "verified_readback_required",
                    "The supplied readback is not an independent durable workflow proof",
                )
        observed_at = now or _now()
        reconciliation_owner_live = False
        if reconciled_github_root is not None:
            # Authentication may persist expiry/revocation. Run it before the
            # board write lock, then recheck its row under that lock below.
            from src.auth.service import AuthFailure, authenticate_session
            from src.security.trust_contract import AuthorityGrant
            try:
                operator = await authenticate_session(str(reconciled_github_root.get("session_id") or ""), touch=False)
                reconciliation_owner_live = operator.principal.principal_id == reconciled_github_root.get("owner", {}).get("principal_id") and AuthorityGrant.EXTERNAL_MUTATION in operator.principal.grants
            except AuthFailure:
                pass
        await _begin_sqlite_immediate(db)
        task = await self._find_task(db, task_id)
        if task is None:
            raise BoardNotFound(task_id)
        owner = WorkBoardOwner(
            principal_id=task.owner_principal_id,
            session_id=task.owner_session_id,
        )
        reconciling = reconciled_github_root is not None
        reconciliation_proof = verified_readback
        if task.status is not WorkBoardStatus.running and not reconciling:
            raise BoardError("task_not_running", "Only a running task can be projected")
        if task.task_revision != int(expected_revision):
            raise BoardRevisionConflict(task.task_id, int(expected_revision), task.task_revision)
        if status in {WorkBoardStatus.review, WorkBoardStatus.done}:
            try:
                await self.validate_task_goal(db, owner, task)
            except BoardError as exc:
                # A long-running attempt cannot turn stale or revoked goal
                # authority into a terminal board result.  End the attempt in
                # an explicit recoverable block while preserving the durable
                # run and its evidence for operator reconciliation.
                status = WorkBoardStatus.blocked
                outcome = "goal_authority_stale"
                block_kind = "capability"
                block_reason = f"goal_authority_stale:{exc.code}"
                verified_readback = None
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
        if reconciling:
            latest = (await db.execute(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task_id).order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc()).limit(1))).scalar_one_or_none()
            root = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id))).scalar_one_or_none()
            if (
                task.capability_id != "work.github-followthrough.v1" or task.status is not WorkBoardStatus.blocked
                or task.block_kind != "unknown_effect" or attempt.ended_at is None
                or latest is None or latest.attempt_id != attempt_id or attempt.fencing_token != int(board_fence)
                or root is None or root.status != "succeeded" or root.run_identity != reconciled_github_root.get("job_id")
                or root.owner_kind != "user" or root.owner_principal_id != owner.principal_id
                or root.session_id != owner.session_id or root.operator_session_id != owner.session_id
                or root.goal_id != task.goal_id or root.goal_revision != task.goal_revision
                or root.idempotency_scope != "work-board-attempt" or root.idempotency_key != f"{task_id}:{attempt_id}"
                or root.revision != reconciled_github_root.get("revision")
                or any(getattr(root, field) != reconciled_github_root.get(field) for field in ("input_digest", "authority_digest", "run_fingerprint"))
            ):
                raise BoardError("reconciliation_binding_mismatch", "Only the exact latest unknown GitHub attempt can adopt its verified root")
            from src.workflows.job_runtime import _job_has_unsafe_effects
            effects = json.loads(root.effect_receipts_json or "[]")
            proof = reconciliation_proof or {}
            if _job_has_unsafe_effects(effects) or not any(
                item.get("receipt_kind") == "readback" and item.get("effect_type") == "github_publication"
                and item.get("status") == "succeeded" and item.get("details", {}).get("verified") is True
                and item.get("readback_id") == proof.get("readback_id")
                and item.get("content_sha256") == proof.get("content_sha256")
                and item.get("verified_at") == proof.get("verified_at") for item in effects
            ):
                raise BoardError("verified_readback_required", "Reconciliation needs this root's exact verified GitHub effect")
            authority = json.loads(root.declared_authority_json or "{}")
            if root.job_kind != "github_followthrough_v1" or authority.get("capability_id") != "work.github-followthrough.v1":
                raise BoardError("reconciliation_binding_mismatch", "The durable root is not this GitHub capability")
            connection = await db.get(GitHubFollowthroughConnection, authority.get("connection_id"))
            current_session = await db.get(OperatorSession, owner.session_id)
            live = reconciliation_owner_live and current_session is not None and current_session.principal_id == owner.principal_id and current_session.revoked_at is None and current_session.replaced_by_id is None and not current_session.is_bearer_tombstone and _utc_datetime(current_session.idle_expires_at) > _now() and _utc_datetime(current_session.absolute_expires_at) > _now()
            if not live or connection is None or connection.owner_principal_id != owner.principal_id or connection.mode not in {"active", "reconcile_only"} or connection.revision != authority.get("connection_revision"):
                status = WorkBoardStatus.blocked
                outcome = block_reason = "reconciliation_authority_changed"
                block_kind = "capability"
        elif attempt.lease_owner != lease_owner or attempt.fencing_token != int(board_fence) or attempt.ended_at is not None:
            raise BoardError("stale_fence", "The board attempt fence is stale")
        # A worker can request review after the dispatcher has taken its
        # in-memory claim snapshot.  Treat the exact pending intent as a
        # durable projection override when the stale snapshot asks for Done.
        # The query binds every identity that can fence a late worker: owner,
        # session, task, attempt, durable run, fence, and board revision.
        intent = None
        promoted_review_intent = False
        if status in {WorkBoardStatus.review, WorkBoardStatus.done}:
            intent = (
                await db.execute(
                    select(WorkBoardReviewIntent).where(
                        WorkBoardReviewIntent.owner_principal_id == owner.principal_id,
                        WorkBoardReviewIntent.owner_session_id == owner.session_id,
                        WorkBoardReviewIntent.task_id == task.task_id,
                        WorkBoardReviewIntent.attempt_id == attempt.attempt_id,
                        WorkBoardReviewIntent.workflow_run_id == str(attempt.workflow_run_id or ""),
                        WorkBoardReviewIntent.fencing_token == int(board_fence),
                        WorkBoardReviewIntent.task_revision == int(expected_revision),
                        WorkBoardReviewIntent.status == "pending",
                    )
                )
            ).scalar_one_or_none()
        if status is WorkBoardStatus.done and intent is not None:
            status = WorkBoardStatus.review
            promoted_review_intent = True
            # The dedicated intent is canonical for this race.  Refresh the
            # compatibility projection from it before the review checks below
            # so a stale dispatcher copy cannot clear a valid request.
            task.requires_review = True
            task.reviewer_id = owner.principal_id
            task.review_request_attempt_id = intent.attempt_id
            task.review_request_fence = int(intent.fencing_token)
            task.review_request_revision = int(intent.task_revision)
            task.review_request_digest = intent.request_digest
            task.review_request_evidence_json = intent.evidence_refs_json
            task.review_requested_at = intent.created_at
            task.review_expires_at = None
        if status is WorkBoardStatus.review and task.review_request_attempt_id:
            if intent is None:
                raise BoardError(
                    "review_intent_stale",
                    "The durable review intent is missing or no longer current",
                )
            if (
                task.review_request_attempt_id != attempt.attempt_id
                or task.review_request_fence != int(board_fence)
                or task.review_request_revision not in {None, int(expected_revision)}
            ):
                raise BoardError(
                    "review_intent_stale",
                    "The review intent is bound to a different attempt or fence",
                )
        if status in {WorkBoardStatus.review, WorkBoardStatus.done}:
            proof_run_id = str(verified_readback.get("workflow_run_id") or "")
            proof_digest = str(verified_readback.get("content_sha256") or "")
            if proof_run_id != str(attempt.workflow_run_id or "") or not _DIGEST.fullmatch(proof_digest.lower()):
                raise BoardError(
                    "verified_readback_required",
                    "The readback proof must identify this attempt's durable run and digest",
                )
        safe_receipts = _safe_receipt_refs(receipt_refs, preserve_effect_ids=True)
        if status in {WorkBoardStatus.review, WorkBoardStatus.done}:
            # Persist the validated proof on the attempt itself.  Adapter
            # result summaries may omit the digest, so Review recovery must
            # never depend on a task-level summary or reconstruct proof from
            # an unrelated run.  The run ID comes from the immutable attempt
            # link, while the digest comes from the proof checked above.
            proof_fields = _safe_receipt_refs(
                [verified_readback],
                limit=1,
                preserve_effect_ids=False,
            )
            proof_receipt = {
                **(proof_fields[0] if proof_fields else {}),
                "workflow_run_id": str(attempt.workflow_run_id),
                "content_sha256": proof_digest.lower(),
                "status": "succeeded",
                "verified": True,
                "readback_status": "verified",
                "verification_status": "passed",
            }
            if proof_receipt not in safe_receipts:
                safe_receipts = [proof_receipt, *safe_receipts[:31]]
        safe_results = _safe_receipt_refs(result_refs, preserve_effect_ids=True)
        safe_artifacts = _safe_receipt_refs(artifact_refs, preserve_effect_ids=True)
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
        if task.capability_id == "work.research-dossier.v1" and status in {WorkBoardStatus.review, WorkBoardStatus.done}:
            from src.work_board.research_readback import verified_dossier
            from src.work_board.input_artifacts import consume_input_artifact, resolve_input_artifact_for_task
            from src.workflows.research_guard import assert_research_operator_session
            run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id))
            if run is None:
                raise BoardError("research_readback_required", "The original research root is unavailable", status_code=409)
            await assert_research_operator_session(db, run, now=observed_at)
            artifact, _raw = await verified_dossier(db, task, attempt, run)
            if artifact["content_sha256"] != proof_digest:
                raise BoardError("research_readback_required", "The physical dossier differs from this attempt's proof", status_code=409)
            resolved = await resolve_input_artifact_for_task(db, owner, artifact_id=task.input_artifact_id,
                goal_id=task.goal_id, goal_revision=task.goal_revision, capability_id=task.capability_id,
                expected_task_id=task.task_id)
            if resolved.row.payload_sha256 != task.typed_input_digest or resolved.row.bound_task_revision is None:
                raise BoardError("research_input_changed", "The original admitted input binding changed", status_code=409)
            if resolved.row.state != "consumed":
                await consume_input_artifact(db, owner, task_id=task.task_id,
                    task_revision=resolved.row.bound_task_revision, artifact_id=task.input_artifact_id)
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
        if promoted_review_intent:
            values.update(
                {
                    "requires_review": True,
                    "reviewer_id": owner.principal_id,
                    "review_request_attempt_id": intent.attempt_id,
                    "review_request_fence": int(intent.fencing_token),
                    "review_request_revision": int(intent.task_revision),
                    "review_request_digest": intent.request_digest,
                    "review_request_evidence_json": intent.evidence_refs_json,
                    "review_requested_at": intent.created_at,
                    "review_expires_at": None,
                }
            )
        if status is WorkBoardStatus.review:
            values["review_expires_at"] = observed_at + timedelta(days=7)
        elif status in {WorkBoardStatus.blocked, WorkBoardStatus.done}:
            # A failed/blocked or directly completed attempt cannot leave a
            # review intent attached to a later retry or terminal card.
            values.update(
                {
                    "review_request_attempt_id": None,
                    "review_request_fence": None,
                    "review_request_revision": None,
                    "review_request_digest": None,
                    "review_request_evidence_json": "[]",
                    "review_requested_at": None,
                    "review_expires_at": None,
                }
            )
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
                    "block_kind": _closed_block_kind(block_kind or "transient"),
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
        if status is WorkBoardStatus.done:
            # A successful dispatcher projection is the second handoff
            # materialization boundary (the first is linking an already-Done
            # parent).  Keep this local import to avoid a repository/review
            # module cycle while preserving one transaction for the parent
            # projection and all child handoff pointers.
            from src.work_board.review import _persist_child_handoffs

            await _persist_child_handoffs(
                db,
                owner,
                task,
                attempt,
                proof=verified_readback,
            )
        if status in {WorkBoardStatus.blocked, WorkBoardStatus.done}:
            await db.execute(
                update(WorkBoardReviewIntent)
                .where(
                    WorkBoardReviewIntent.task_id == task.task_id,
                    WorkBoardReviewIntent.owner_principal_id == owner.principal_id,
                    WorkBoardReviewIntent.owner_session_id == owner.session_id,
                    WorkBoardReviewIntent.status == "pending",
                )
                .values(status="superseded")
            )
        if status is WorkBoardStatus.review and task.review_request_attempt_id:
            await db.execute(
                update(WorkBoardReviewIntent)
                .where(
                    WorkBoardReviewIntent.owner_principal_id == owner.principal_id,
                    WorkBoardReviewIntent.owner_session_id == owner.session_id,
                    WorkBoardReviewIntent.task_id == task.task_id,
                    WorkBoardReviewIntent.attempt_id == attempt.attempt_id,
                    WorkBoardReviewIntent.fencing_token == int(board_fence),
                    WorkBoardReviewIntent.task_revision == int(expected_revision),
                    WorkBoardReviewIntent.status == "pending",
                )
                .values(status="projected")
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
        newer_attempt = aliased(WorkBoardAttempt)
        result = await db.execute(
            select(WorkBoardTask, WorkBoardAttempt)
            .join(WorkBoardAttempt, WorkBoardAttempt.task_id == WorkBoardTask.task_id)
            .outerjoin(WorkflowRunState, WorkflowRunState.run_identity == WorkBoardAttempt.workflow_run_id)
            .where(
                or_(
                    (WorkBoardTask.status == WorkBoardStatus.running) & WorkBoardAttempt.ended_at.is_(None),
                    (
                        WorkBoardTask.status == WorkBoardStatus.blocked
                    )
                    & (WorkBoardTask.capability_id == "guardian-routine.v1")
                    & WorkBoardTask.block_reason.in_(
                        (
                            "awaiting_approval",
                            "awaiting_publication_preview",
                            "awaiting_publication_approval",
                        )
                    ) & WorkBoardAttempt.ended_at.is_(None),
                    (WorkBoardTask.status == WorkBoardStatus.blocked)
                    & (WorkBoardTask.block_kind == "unknown_effect")
                    & (WorkBoardTask.capability_id == "work.github-followthrough.v1")
                    & WorkBoardAttempt.ended_at.is_not(None)
                    & (WorkflowRunState.status == "succeeded")
                    & ~select(newer_attempt.attempt_id).where(
                        newer_attempt.task_id == WorkBoardTask.task_id,
                        or_(newer_attempt.created_at > WorkBoardAttempt.created_at,
                            (newer_attempt.created_at == WorkBoardAttempt.created_at) & (newer_attempt.attempt_id > WorkBoardAttempt.attempt_id)),
                    ).exists(),
                ),
                WorkBoardAttempt.workflow_run_id.is_not(None),
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
        block_kind: str = "reconcile_admission_binding",
        block_reason: str | None = None,
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
        closed_kind = _closed_block_kind(block_kind)
        safe_reason = await self._safe_text(
            block_reason
            or (
                "No durable run exists for the pending admission binding; operator reconciliation is required."
                if closed_kind == "reconcile_admission_binding"
                else "The typed capability input disappeared before durable admission; repair it and retry."
            )
        )
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
                "block_kind": closed_kind,
                "block_reason": safe_reason,
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
                "block_kind": closed_kind,
                "recovery_action": (
                    "retry" if closed_kind == "capability" else "reconcile_admission_binding"
                ),
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
        attempt_limit = 2
        if task.capability_id == _BROWSER_CAPABILITY_ID:
            live_goal = await db.scalar(
                select(Goal).where(
                    Goal.id == task.goal_id,
                    Goal.owner_principal_id == task.owner_principal_id,
                    Goal.owner_session_id == task.owner_session_id,
                )
            )
            attempt_limit, _max_outstanding_jobs = effective_browser_limits(live_goal)
        if attempt_count >= attempt_limit:
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
                    "review_request_attempt_id": None,
                    "review_request_fence": None,
                    "review_request_revision": None,
                    "review_request_digest": None,
                    "review_request_evidence_json": "[]",
                    "review_requested_at": None,
                    "review_expires_at": None,
                    "task_revision": int(expected_revision) + 1,
                    "updated_at": observed_at,
                },
            )
            await db.execute(
                update(WorkBoardReviewIntent)
                .where(
                    WorkBoardReviewIntent.task_id == task.task_id,
                    WorkBoardReviewIntent.status == "pending",
                )
                .values(status="superseded")
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
        await db.execute(
            update(WorkBoardReviewIntent)
            .where(
                WorkBoardReviewIntent.task_id == task.task_id,
                WorkBoardReviewIntent.attempt_id == attempt.attempt_id,
                WorkBoardReviewIntent.status == "pending",
            )
            .values(status="superseded")
        )
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
            values={
                "task_revision": int(expected_revision) + 1,
                "review_request_attempt_id": None,
                "review_request_fence": None,
                "review_request_revision": None,
                "review_request_digest": None,
                "review_request_evidence_json": "[]",
                "review_requested_at": None,
                "review_expires_at": None,
                "updated_at": observed_at,
            },
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
        parent_rows = (
            await db.execute(
                select(WorkBoardLink.parent_task_id, parent_task.status)
                .join(parent_task, parent_task.task_id == WorkBoardLink.parent_task_id)
                .where(
                    WorkBoardLink.child_task_id == task.task_id,
                    WorkBoardLink.owner_principal_id == owner.principal_id,
                    WorkBoardLink.owner_session_id == owner.session_id,
                    parent_task.owner_principal_id == owner.principal_id,
                    parent_task.owner_session_id == owner.session_id,
                )
            )
        ).all()
        parents = [row[0] for row in parent_rows]
        dependency_counts = (
            len(parent_rows),
            sum(1 for _, status in parent_rows if status == WorkBoardStatus.done),
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
            "dependency_counts": dependency_counts,
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
