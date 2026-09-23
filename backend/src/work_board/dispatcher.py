"""Bounded dispatcher for the canonical operator work board.

The board is only the coordination projection.  Every executable attempt is
admitted and leased through ``WorkflowRunState`` before a registered adapter
is called.  This module deliberately contains no queue implementation and no
second execution state machine.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Mapping
import uuid

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.auth.service import AuthFailure, authenticate_session
from src.db.engine import get_session
from src.db.models import Goal, WorkBoardAttempt, WorkBoardStatus, WorkBoardTask
from src.guardian.goal_snapshot_to_file import (
    CAPABILITY_ID as GOAL_SNAPSHOT_CAPABILITY,
    CAPABILITY_VERSION as GOAL_SNAPSHOT_VERSION,
    GoalSnapshotToFileRequest,
    GoalSnapshotToFileResult,
    GoalSnapshotToFileService,
    normalize_workspace_relative_path,
)
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal
from src.workspace import canonical_workspace_root
from config.settings import settings
from src.goals.repository import deserialize_admission_budget
from src.work_board.repository import (
    BoardError,
    BoardAttemptProjection,
    BoardDispatchClaim,
    WorkBoardOwner,
    WorkBoardRepository,
)
from src.work_board.tools import WorkBoardWorkerRequest
from src.tools.work_board_tools import WorkBoardWorkerHost
from src.workflows.job_runtime import (
    DurableJobAdmissionDenied,
    DurableJobError,
    DurableJobIdempotencyConflict,
    DurableJobIdentity,
    DurableJobSpec,
    UNCERTAIN_EXTERNAL_EFFECT_STATUSES,
    durable_job_repository,
)

logger = logging.getLogger(__name__)

DISPATCH_PASS_LIMIT = 20
DISPATCH_ADMISSION_LIMIT = 2
MAX_RUNNING_TASKS = 2
MAX_ATTEMPTS_PER_TASK = 2
DEFAULT_RUNTIME_SECONDS = 300
MAX_RUNTIME_SECONDS = 900
DISPATCHER_PRINCIPAL = "service:work-board"
DISPATCHER_SERVICE = "service:work-board"
DISPATCHER_SESSION = "service-session:work-board"

# All managed scheduler and API dispatcher entry points share this registry.
# It contains only live server asyncio tasks; it is not persisted or exposed
# to operators.
_ACTIVE_WORKER_TASKS: dict[tuple[str, str], asyncio.Task[Any]] = {}


@dataclass(frozen=True)
class CapabilitySpec:
    capability_id: str
    version: str
    blocked_reason: str | None = None


class TypedInputError(ValueError):
    """A task's immutable workspace JSON input failed pre-admission checks."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class _GoalSnapshotInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    file_path: str = Field(min_length=1, max_length=512)


class _SourceWatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    watch_id: str = Field(min_length=1, max_length=256)
    expected_plan_revision: int = Field(ge=1)


class _RepoChangeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    candidate_id: str = Field(min_length=1, max_length=160)
    repository_path: str = Field(min_length=1, max_length=512)
    patch_artifact_id: str = Field(min_length=1, max_length=160)
    patch_sha256: str = Field(min_length=64, max_length=64)
    allowed_paths: list[str] = Field(min_length=1, max_length=64)
    test_args: list[str] = Field(min_length=1, max_length=16)
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)


class _GitHubInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    dossier_artifact_id: str = Field(min_length=1, max_length=200)
    dossier_sha256: str = Field(min_length=64, max_length=64)
    connection_revision: int = Field(gt=0)
    action: str = Field(min_length=1, max_length=80)
    title: str | None = Field(default=None, max_length=200)
    body: str = Field(min_length=1)
    issue_number: int | None = Field(default=None, gt=0)


class _RoutineInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    routine_id: str = Field(min_length=1, max_length=256)
    version: int = Field(ge=1)
    expected_routine_revision: int = Field(ge=1)
    source_watch_id: str = Field(min_length=1, max_length=256)
    expected_watch_revision: int = Field(ge=1)


_TYPED_INPUT_MODELS: dict[str, type[BaseModel]] = {
    GOAL_SNAPSHOT_CAPABILITY: _GoalSnapshotInput,
    "guardian.research-watch.v1": _SourceWatchInput,
    "engineering.repo-change.v1": _RepoChangeInput,
    "work.github-followthrough.v1": _GitHubInput,
    "guardian-routine.v1": _RoutineInput,
}


REGISTERED_CAPABILITIES: dict[str, CapabilitySpec] = {
    GOAL_SNAPSHOT_CAPABILITY: CapabilitySpec(
        GOAL_SNAPSHOT_CAPABILITY,
        GOAL_SNAPSHOT_VERSION,
    ),
    "guardian.research-watch.v1": CapabilitySpec(
        "guardian.research-watch.v1",
        "1",
    ),
    "engineering.repo-change.v1": CapabilitySpec(
        "engineering.repo-change.v1",
        "1",
    ),
    "work.github-followthrough.v1": CapabilitySpec(
        "work.github-followthrough.v1",
        "1",
    ),
    "guardian-routine.v1": CapabilitySpec(
        "guardian-routine.v1",
        "1",
    ),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _text(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip()


def _board_attempt_uuid(attempt_id: str, task_id: str) -> uuid.UUID:
    """Normalize the durable attempt identity for adapter-specific IDs."""

    try:
        return uuid.UUID(str(attempt_id))
    except (ValueError, AttributeError, TypeError):
        return uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"seraph:work-board-attempt:{task_id}:{attempt_id}",
        )


def _status(value: Any) -> str:
    if isinstance(value, Mapping):
        receipt = value.get("receipt")
        receipt_status = receipt.get("status") if isinstance(receipt, Mapping) else None
        return _text(value.get("status") or receipt_status)
    return _text(value)


def _safe_error_code(exc: BaseException) -> str:
    code = _text(getattr(exc, "code", None))
    if code and len(code) <= 128 and all(char.isalnum() or char in {"_", "-", ":", "."} for char in code):
        return code
    return type(exc).__name__[:128] or "adapter_blocked"


_STABLE_REASON_CODES = frozenset(
    {
        "adapter_blocked",
        "admission_or_execution_blocked",
        "admission_binding_missing",
        "cancelled",
        "capability",
        "cleanup_unproven",
        "connection_revision_stale",
        "cost_liability",
        "dependency_unfinished",
        "dispatcher_failure",
        "execution_blocked",
        "executor_missing",
        "goal_binding_stale",
        "goal_not_admitted",
        "goal_not_active",
        "goal_not_found",
        "goal_owner_unbound",
        "goal_revision_stale",
        "needs_input",
        "no_external_effect",
        "not_dispatched",
        "operator_cancelled",
        "operator_retry",
        "pending_admission",
        "reconcile_admission_binding",
        "reconcile_external_effect",
        "repo_isolation_unavailable",
        "restore_prerequisite",
        "routine_not_active",
        "routine_revision_stale",
        "scheduled_not_due",
        "transient",
        "typed_input_invalid",
        "typed_input_missing",
        "unknown_effect",
        "verified_readback_missing",
        "watch_not_active",
        "watch_plan_revision_stale",
    }
)


def _stable_reason_code(value: Any, *, fallback: str = "execution_blocked") -> str:
    """Map adapter/runtime failures to a closed, non-sensitive code set."""

    candidate = _text(value).lower()
    if candidate in _STABLE_REASON_CODES:
        return candidate
    if any(token in candidate for token in ("unknown", "effect", "cost", "reconcile")):
        return "unknown_effect"
    if any(token in candidate for token in ("approval", "input", "consent", "review")):
        return "needs_input"
    if any(
        token in candidate
        for token in (
            "credential",
            "connection",
            "grant",
            "authority",
            "capability",
            "isolation",
            "profile",
            "route",
            "budget",
            "provider",
            "config",
            "permission",
        )
    ):
        return "capability"
    if any(token in candidate for token in ("timeout", "timed", "rate", "retry", "failed", "failure", "deadline")):
        return "transient"
    return fallback if fallback in _STABLE_REASON_CODES else "execution_blocked"


def _parse_typed_input(task: WorkBoardTask) -> dict[str, Any]:
    """Load and strictly validate one immutable workspace JSON envelope.

    The board stores only the reference and digest.  All execution authority,
    owner/session/goal binding, budget, deadline, and approval state come from
    the live task and canonical runtime, so none of those fields are accepted
    from the input file.
    """

    reference = _text(task.typed_input_ref)
    if not reference.startswith("workspace-json:"):
        raise TypedInputError(
            "typed_input_ref_invalid",
            "typed_input_ref must use workspace-json:<relative .json path>",
        )
    relative = reference[len("workspace-json:") :].strip()
    try:
        relative = normalize_workspace_relative_path(relative)
    except (TypeError, ValueError) as exc:
        raise TypedInputError("typed_input_ref_invalid", str(exc)) from exc
    if not relative.lower().endswith(".json"):
        raise TypedInputError("typed_input_file_invalid", "typed input must reference a .json file")
    root = Path(canonical_workspace_root(settings.workspace_dir)).resolve(strict=True)
    candidate = root / relative
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise TypedInputError("typed_input_missing", "typed input file is unavailable") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise TypedInputError("typed_input_path_escape", "typed input escapes the canonical workspace") from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise TypedInputError("typed_input_file_invalid", "typed input must be a regular file")
    try:
        payload = resolved.read_bytes()
    except OSError as exc:
        raise TypedInputError("typed_input_unreadable", "typed input cannot be read") from exc
    if len(payload) > 64 * 1024:
        raise TypedInputError("typed_input_too_large", "typed input exceeds 64 KiB")
    expected_digest = _text(task.typed_input_digest).lower()
    actual_digest = hashlib.sha256(payload).hexdigest()
    if not expected_digest or actual_digest != expected_digest:
        raise TypedInputError("typed_input_digest_mismatch", "typed input digest does not match the task")
    try:
        envelope = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TypedInputError("typed_input_json_invalid", "typed input is not valid UTF-8 JSON") from exc
    if not isinstance(envelope, Mapping):
        raise TypedInputError("typed_input_envelope_invalid", "typed input envelope must be an object")
    if set(envelope) != {"schema_version", "capability_id", "input"}:
        raise TypedInputError("typed_input_envelope_invalid", "typed input envelope has unexpected fields")
    if envelope.get("schema_version") != 1:
        raise TypedInputError("typed_input_schema_invalid", "typed input schema_version must be 1")
    capability_id = _text(task.capability_id)
    if envelope.get("capability_id") != capability_id:
        raise TypedInputError("typed_input_capability_mismatch", "typed input capability does not match the task")
    raw_input = envelope.get("input")
    if not isinstance(raw_input, Mapping):
        raise TypedInputError("typed_input_invalid", "typed input must contain an object input")
    model_type = _TYPED_INPUT_MODELS.get(capability_id)
    if model_type is None:
        raise TypedInputError("capability_unregistered", "the task names no registered capability")
    try:
        validated = model_type.model_validate(dict(raw_input))
    except ValidationError as exc:
        raise TypedInputError("typed_input_invalid", "typed input does not match the capability schema") from exc
    result = validated.model_dump(mode="json", exclude_none=True)
    if capability_id == GOAL_SNAPSHOT_CAPABILITY:
        try:
            result["file_path"] = normalize_workspace_relative_path(result["file_path"])
        except (TypeError, ValueError) as exc:
            raise TypedInputError("typed_input_invalid", "file_path must be workspace-relative") from exc
    return result


def _lease(projection: Mapping[str, Any] | None) -> tuple[str | None, int | None]:
    lease = projection.get("lease") if isinstance(projection, Mapping) else None
    if not isinstance(lease, Mapping):
        return None, None
    owner = _text(lease.get("owner")) or None
    try:
        fence = int(lease.get("fencing_token"))
    except (TypeError, ValueError):
        fence = None
    return owner, fence


class WorkBoardDispatcher:
    """One bounded managed-scheduler pass over canonical board tasks."""

    def __init__(
        self,
        *,
        repository: WorkBoardRepository | None = None,
        jobs: Any | None = None,
        session_provider: Any | None = None,
        now: Any = _now,
        runner_id: str = DISPATCHER_PRINCIPAL,
    ) -> None:
        self.repository = repository or WorkBoardRepository()
        self.jobs = jobs or durable_job_repository
        self.session_provider = session_provider or get_session
        self.now = now
        self.runner_id = runner_id
        self.runner_session = f"{runner_id}:session"
        # GoalSnapshot executes inline in the dispatcher.  Keep a server-side
        # handle so cancellation can stop that worker before the durable root
        # is reconciled; no client supplied identifier can reach this map.
        self._active_worker_tasks = _ACTIVE_WORKER_TASKS

    async def run_pass(self) -> dict[str, Any]:
        observed_at = self.now()
        reconciled = await self.reconcile_pending_attempts(now=observed_at)
        linked_reconciled = await self.reconcile_linked_attempts(now=observed_at)
        async with self.session_provider() as db:
            candidates = await self.repository.list_dispatch_candidates(
                db,
                now=observed_at,
                limit=DISPATCH_PASS_LIMIT,
            )

        receipt: dict[str, Any] = {
            "status": "completed",
            "considered": len(candidates),
            "promoted": 0,
            "claimed": 0,
            "admitted": 0,
            "completed": 0,
            "blocked": len(reconciled) + len(linked_reconciled),
            "reconciled": len(reconciled) + len(linked_reconciled),
            "task_ids": [],
        }
        admissions = 0
        for candidate in candidates:
            if admissions >= DISPATCH_ADMISSION_LIMIT:
                break
            task = candidate
            readiness_error, readiness_reason = await self._readiness(task)
            if task.status is WorkBoardStatus.todo:
                async with self.session_provider() as db:
                    mutation = await self.repository.promote_task_ready(
                        db,
                        task.task_id,
                        expected_revision=task.task_revision,
                        actor_principal_id=self.runner_id,
                        actor_session_id=self.runner_session,
                        readiness_error=readiness_error,
                        readiness_reason=readiness_reason,
                        now=observed_at,
                    )
                if mutation is None:
                    continue
                if mutation.task.status is WorkBoardStatus.blocked:
                    receipt["blocked"] += 1
                    continue
                receipt["promoted"] += 1
                task = mutation.task
            if readiness_error:
                # A Ready row can be left behind by a configuration/input
                # change between passes.  Close that gate before claim so no
                # execution attempt is counted for a pre-admission denial.
                if task.status is WorkBoardStatus.ready:
                    async with self.session_provider() as db:
                        await self.repository.block_ready_task(
                            db,
                            task.task_id,
                            expected_revision=task.task_revision,
                            block_kind=_stable_reason_code(readiness_error, fallback="capability"),
                            block_reason=_stable_reason_code(readiness_error, fallback="capability"),
                            actor_principal_id=self.runner_id,
                            actor_session_id=self.runner_session,
                            now=observed_at,
                        )
                    receipt["blocked"] += 1
                continue
            if task.status is not WorkBoardStatus.ready:
                continue
            async with self.session_provider() as db:
                claim = await self.repository.claim_ready_task(
                    db,
                    task.task_id,
                    expected_revision=task.task_revision,
                    lease_owner=self.runner_id,
                    lease_seconds=await self._effective_runtime(task),
                    now=observed_at,
                    actor_principal_id=self.runner_id,
                    actor_session_id=self.runner_session,
                )
            if claim is None:
                continue
            receipt["claimed"] += 1
            admissions += 1
            receipt["task_ids"].append(claim.task.task_id)
            outcome = await self._admit_execute_project(claim)
            receipt["admitted"] += int(outcome.get("admitted", False))
            receipt["completed"] += int(outcome.get("completed", False))
            receipt["blocked"] += int(outcome.get("blocked", False))
        return receipt

    async def validate_retry(
        self,
        owner: WorkBoardOwner,
        task_id: str,
        *,
        expected_revision: int,
    ) -> None:
        """Run live retry gates before the board projection is reopened.

        Repository retry is intentionally a small CAS kernel.  This method is
        the execution-plane preflight: it revalidates the owner session, goal,
        typed capability input, schedule, dependencies, and the capability's
        current grant/configuration without admitting a new run.  A failed
        gate leaves the task Blocked and exposes only a bounded recovery code.
        """

        async with self.session_provider() as db:
            detail = await self.repository.get_detail(db, owner, task_id)
            task = detail["task"]
            parent_ids = list(detail.get("parents") or [])
            if task.task_revision != int(expected_revision):
                raise BoardError("stale_revision", "The task changed before retry preflight", status_code=409)
            if task.status is not WorkBoardStatus.blocked:
                raise BoardError("illegal_transition", "Only blocked tasks can be retried", status_code=409)
            parent_statuses = []
            if parent_ids:
                parent_statuses = list(
                    (
                        await db.execute(
                            select(WorkBoardTask.status).where(
                                WorkBoardTask.task_id.in_(parent_ids),
                                WorkBoardTask.owner_principal_id == owner.principal_id,
                                WorkBoardTask.owner_session_id == owner.session_id,
                            )
                        )
                    ).scalars().all()
                )

        def gate_error(code: str, message: str) -> None:
            raise BoardError(
                "retry_prerequisite",
                message,
                status_code=409,
                reason_code=_stable_reason_code(code, fallback="capability"),
                recovery_action="restore_prerequisite",
            )

        readiness_error, readiness_reason = await self._readiness(task)
        if readiness_error:
            gate_error(readiness_error, readiness_reason or "A current retry prerequisite is unavailable")
        if task.scheduled_at is not None and task.scheduled_at > self.now():
            gate_error("scheduled_not_due", "The task schedule has not reached its retry eligibility time")
        if any(status is not WorkBoardStatus.done for status in parent_statuses):
            gate_error("dependency_unfinished", "Every blocking parent must be Done before retry")
        try:
            inputs = _parse_typed_input(task)
        except TypedInputError as exc:
            gate_error(exc.code, "The task typed input is not currently executable")

        capability = _text(task.capability_id)
        try:
            if capability == "guardian.research-watch.v1":
                from src.guardian.source_watch import _goal_admission, source_watch_service

                watch = await source_watch_service.get_watch(
                    _text(inputs["watch_id"]),
                    owner_principal_id=task.owner_principal_id,
                    owner_session_id=task.owner_session_id,
                )
                if not isinstance(watch, Mapping) or _text(watch.get("state")) != "active":
                    gate_error("watch_not_active", "The source watch is not currently active")
                if int(watch.get("plan_revision") or 0) != int(inputs["expected_plan_revision"]):
                    gate_error("watch_plan_revision_stale", "The source watch plan revision changed")
                async with self.session_provider() as db:
                    source_goal = (
                        await db.execute(select(Goal).where(Goal.id == task.goal_id))
                    ).scalar_one_or_none()
                if source_goal is None:
                    gate_error("goal_not_found", "The source watch goal is unavailable")
                admitted, admission_reason, _budget = _goal_admission(source_goal)
                if not admitted:
                    gate_error(admission_reason, "The source watch consent or budget is not currently admitted")
            elif capability == "engineering.repo-change.v1":
                from src.api.workflows import (
                    RepoSandboxError,
                    RootlessDockerRepoSandbox,
                    _resolve_repo_change_candidate,
                    authenticate_repo_change_operator,
                )

                await authenticate_repo_change_operator(
                    task.owner_session_id,
                    owner_principal_id=task.owner_principal_id,
                )
                preflight = RootlessDockerRepoSandbox().preflight()
                if not preflight.ok:
                    gate_error(_text(preflight.reason) or "repo_isolation_unavailable", "The repository isolation prerequisite is unavailable")
                await _resolve_repo_change_candidate(
                    candidate_id=_text(inputs["candidate_id"]),
                    goal_id=task.goal_id,
                    goal_revision=task.goal_revision,
                    owner_principal_id=task.owner_principal_id,
                    owner_session_id=task.owner_session_id,
                    evidence_refs=list(inputs.get("evidence_refs") or []),
                )
            elif capability == "work.github-followthrough.v1":
                from src.extensions.github_followthrough import GitHubFollowthroughService

                connection = await GitHubFollowthroughService().get_connection(task.owner_principal_id)
                if _text(connection.get("mode")) != "active":
                    gate_error("github_connection_not_active", "The GitHub connection is not currently active")
                if not bool(connection.get("credential_configured")):
                    gate_error("credential_not_configured", "The GitHub credential is not currently configured")
                if int(connection.get("revision") or 0) != int(inputs["connection_revision"]):
                    gate_error("connection_revision_stale", "The GitHub connection revision changed")
                try:
                    operator = await authenticate_session(task.owner_session_id, touch=False)
                except AuthFailure as exc:
                    gate_error(exc.code, "The GitHub owner session is no longer valid")
                grants = {
                    _text(getattr(grant, "value", grant))
                    for grant in (getattr(getattr(operator, "principal", None), "grants", ()) or ())
                }
                if AuthorityGrant.EXTERNAL_MUTATION.value not in grants:
                    gate_error("external_mutation_grant_required", "The GitHub external mutation grant is not current")
            elif capability == "guardian-routine.v1":
                from src.workflows.routines import routine_service

                routine = await routine_service.read(
                    _text(inputs["routine_id"]),
                    owner_principal_id=task.owner_principal_id,
                    owner_session_id=task.owner_session_id,
                )
                if not isinstance(routine, Mapping) or _text(routine.get("state")) != "active":
                    gate_error("routine_not_active", "The reusable procedure is not currently active")
                if int(routine.get("revision") or 0) != int(inputs["expected_routine_revision"]):
                    gate_error("routine_revision_stale", "The reusable procedure revision changed")
                versions = routine.get("versions") if isinstance(routine.get("versions"), list) else []
                selected = next(
                    (
                        version
                        for version in versions
                        if isinstance(version, Mapping)
                        and int(version.get("version") or 0) == int(inputs["version"])
                    ),
                    None,
                )
                package = routine.get("package") if isinstance(routine.get("package"), Mapping) else {}
                if selected is None or not _text(selected.get("installed_package_digest")):
                    gate_error("routine_version_not_installed", "The selected routine version is not installed")
                if _text(package.get("status")) != "active" or _text(package.get("digest")) != _text(selected.get("installed_package_digest")):
                    gate_error("package_review_required", "The routine package review is not current")
                from src.guardian.source_watch import source_watch_service

                watch = await source_watch_service.get_watch(
                    _text(inputs["source_watch_id"]),
                    owner_principal_id=task.owner_principal_id,
                    owner_session_id=task.owner_session_id,
                )
                if (
                    not isinstance(watch, Mapping)
                    or int(watch.get("plan_revision") or 0) != int(inputs["expected_watch_revision"])
                ):
                    gate_error("watch_plan_revision_stale", "The routine source watch revision changed")
        except BoardError:
            raise
        except Exception as exc:
            gate_error(_safe_error_code(exc), "A current capability prerequisite is unavailable")

    async def validate_unblock(
        self,
        owner: WorkBoardOwner,
        task_id: str,
        *,
        expected_revision: int,
    ) -> None:
        """Validate the live gates before exposing or applying manual unblock.

        The repository owns the transition CAS.  This preflight keeps a stale
        owner session, goal revision, Ready specification, or Review evidence
        from making an ``unblock`` control look usable after the projection was
        cached.  Triage/Todo intentionally retain their safe prior phase even
        when they have not yet acquired an executable capability.
        """

        async with self.session_provider() as db:
            detail = await self.repository.get_detail(db, owner, task_id)
            task = detail["task"]
            attempts = list(detail.get("attempts") or [])
            if task.task_revision != int(expected_revision):
                raise BoardError(
                    "stale_revision",
                    "The task changed before unblock preflight",
                    status_code=409,
                )
            if task.status is not WorkBoardStatus.blocked or task.block_kind != "operator":
                raise BoardError(
                    "typed_reconcile_required",
                    "Only an operator block can use generic unblock",
                    status_code=409,
                    reason_code="typed_reconcile_required",
                    recovery_action="restore_prerequisite",
                )
            if any(attempt.ended_at is None for attempt in attempts):
                raise BoardError(
                    "attempt_reconcile_required",
                    "An active or pending attempt requires typed recovery before unblock",
                    status_code=409,
                    reason_code="reconcile_admission_binding",
                    recovery_action="reconcile_admission_binding",
                )
            source = _text(task.block_source_status)
            if source not in {
                WorkBoardStatus.triage.value,
                WorkBoardStatus.todo.value,
                WorkBoardStatus.ready.value,
                WorkBoardStatus.review.value,
            }:
                raise BoardError(
                    "invalid_recovery_target",
                    "The operator block has no safe prior board phase",
                    status_code=409,
                    reason_code="restore_prerequisite",
                    recovery_action="restore_prerequisite",
                )

        def gate_error(code: str, message: str) -> None:
            raise BoardError(
                "unblock_prerequisite",
                message,
                status_code=409,
                reason_code=_stable_reason_code(code, fallback="restore_prerequisite"),
                recovery_action="restore_prerequisite",
            )

        try:
            operator = await authenticate_session(task.owner_session_id, touch=False)
        except AuthFailure as exc:
            gate_error(exc.code, "The task owner session is no longer valid")
        if operator is not None and str(operator.principal.principal_id) != str(task.owner_principal_id):
            gate_error("goal_owner_unbound", "The task owner session belongs to another principal")

        async with self.session_provider() as db:
            goal = (
                await db.execute(
                    select(Goal).where(
                        Goal.id == task.goal_id,
                        Goal.owner_principal_id == task.owner_principal_id,
                        Goal.owner_session_id == task.owner_session_id,
                        Goal.revision == task.goal_revision,
                    )
                )
            ).scalar_one_or_none()
        if goal is None:
            gate_error("goal_revision_stale", "The task goal is missing, stale, or no longer owner-bound")
        goal_status = _text(getattr(goal, "status", None))
        if goal_status and goal_status not in {"active", "draft"}:
            gate_error("goal_not_admitted", "The task goal is not currently executable")

        if source == WorkBoardStatus.ready.value:
            readiness_error, readiness_reason = await self._readiness(task)
            if readiness_error:
                gate_error(readiness_error, readiness_reason or "The Ready execution gates are not satisfied")
        elif source == WorkBoardStatus.review.value:
            if not _text(task.reviewer_id):
                gate_error("reviewer_required", "The Review phase has no named reviewer")
            latest = max(attempts, key=lambda item: item.created_at or datetime.min) if attempts else None
            refs: list[Any] = []
            if latest is not None:
                try:
                    parsed = json.loads(latest.receipt_refs_json or "[]")
                except (TypeError, ValueError):
                    parsed = []
                refs = parsed if isinstance(parsed, list) else []
            if latest is None or latest.ended_at is None or not any(
                isinstance(item, Mapping)
                and bool(item.get("verified"))
                and _text(item.get("status")) in {"succeeded", "read_back", "reconciled"}
                for item in refs
            ):
                gate_error("verified_readback_missing", "Review requires verified readback evidence")

    async def cancel_task(
        self,
        owner: WorkBoardOwner,
        task_id: str,
        *,
        expected_revision: int,
        reason: str = "operator_cancelled",
    ) -> BoardAttemptProjection:
        """Persist cancellation, clean up the adapter, then reconcile safely."""

        async with self.session_provider() as db:
            detail = await self.repository.get_detail(db, owner, task_id)
        task = detail["task"]
        if task.task_revision != int(expected_revision):
            raise BoardError("stale_revision", "The task changed before cancellation", status_code=409)
        if task.status is not WorkBoardStatus.running:
            raise BoardError("illegal_transition", "Only a running task can be cancelled", status_code=409)
        active = next((attempt for attempt in detail["attempts"] if attempt.ended_at is None), None)
        if active is None:
            raise BoardError("attempt_not_found", "The running task has no active attempt", status_code=409)
        job_id = _text(active.workflow_run_id)
        if not job_id:
            raise BoardError(
                "admission_reconcile_required",
                "Cancellation waits for the pending admission binding to be reconciled",
                status_code=409,
            )

        # Validate the complete immutable binding before recording intent.  A
        # caller must never cancel a run merely because it guessed its ID.
        inputs = _parse_typed_input(task)
        projection = await self.jobs.get_job(job_id)
        if not isinstance(projection, Mapping):
            raise BoardError("workflow_run_not_found", "The linked durable run is unavailable", status_code=409)
        binding_job_id = await self._lookup_linked_binding(task, active, inputs)
        if binding_job_id != job_id:
            raise BoardError("workflow_identity_conflict", "The durable run binding does not match this attempt")
        expected_identity = self._expected_identity_for_task(
            task,
            active,
            inputs,
            projection,
            runtime_seconds=await self._effective_runtime(task),
        )
        async with self.session_provider() as db:
            await self.repository.validate_attempt_binding(
                db,
                owner,
                task.task_id,
                active.attempt_id,
                workflow_run_id=job_id,
                board_fence=active.fencing_token,
                lease_owner=active.lease_owner or self.runner_id,
                workflow_projection=projection,
                expected_identity=expected_identity,
            )
            intent = await self.repository.request_cancel(
                db,
                owner,
                task.task_id,
                expected_revision=task.task_revision,
                attempt_id=active.attempt_id,
                board_fence=active.fencing_token,
                lease_owner=active.lease_owner or self.runner_id,
                actor_principal_id=self.runner_id,
                actor_session_id=self.runner_session,
            )
        task = intent.task

        if intent.idempotent_replay:
            # The durable cancel intent/event is already present.  A repeated
            # click with the fresh task revision must be a read-only replay;
            # running cleanup/projection again could append a second board
            # event or advance the task revision while the first recovery is
            # still in flight.  Startup reconciliation owns any unfinished
            # adapter cleanup.
            async with self.session_provider() as db:
                detail = await self.repository.get_detail(db, owner, task_id)
            replay_attempt = next(
                (
                    item
                    for item in detail.get("attempts", [])
                    if item.attempt_id == active.attempt_id
                ),
                active,
            )
            replay_event = next(
                (
                    item
                    for item in reversed(detail.get("events", []))
                    if item.kind == "attempt.cancel_requested"
                ),
                intent.event,
            )
            return BoardAttemptProjection(
                task=detail["task"],
                attempt=replay_attempt,
                event=replay_event,
            )

        # Mirror the board intent into the authoritative durable run before
        # adapter cleanup.  A crash after this checkpoint is replayed from the
        # persisted attempt binding and cannot silently relaunch work.
        checkpoint_proven = True
        lease = projection.get("lease") if isinstance(projection.get("lease"), Mapping) else {}
        if _status(projection) == "running" and _text(lease.get("owner")) and lease.get("fencing_token") is not None:
            try:
                await self.jobs.record_checkpoint(
                    job_id,
                    checkpoint_id="cancel_requested",
                    state={"phase": "cancel_requested", "reason_code": "operator_cancelled"},
                    owner=_text(lease.get("owner")),
                    fencing_token=int(lease.get("fencing_token")),
                    expected_revision=projection.get("revision"),
                )
            except Exception:
                # The adapter/root readback below remains authoritative.  A
                # failed checkpoint is treated as an unproven cleanup path.
                logger.info("durable cancel intent checkpoint requires reconciliation for %s", job_id)
                checkpoint_proven = False

        cleanup_receipts, cleanup_proven = await self._cleanup_adapter(
            task,
            active,
            inputs,
            projection,
            reason=reason,
        )
        # The common tree cleanup is still required after adapter-specific
        # hooks: descendants are authoritative durable jobs and are cancelled
        # before their root.  It is idempotent for a hook that already ended
        # the root.
        try:
            receipts = await self.jobs.cancel_job_tree(job_id, reason=reason[:128])
        except Exception:
            receipts = []
            cleanup_proven = False
        root = await self.jobs.get_job(job_id)
        return await self._project_cancel_result(
            task,
            active,
            root,
            [*cleanup_receipts, *receipts],
            cleanup_proven=cleanup_proven and checkpoint_proven,
        )

    def _expected_identity_for_task(
        self,
        task: WorkBoardTask,
        attempt: WorkBoardAttempt,
        inputs: Mapping[str, Any],
        projection: Mapping[str, Any],
        *,
        runtime_seconds: int = DEFAULT_RUNTIME_SECONDS,
    ) -> dict[str, Any]:
        if _text(task.capability_id) == GOAL_SNAPSHOT_CAPABILITY:
            spec, _inputs, expected_job_id, _owner, _runtime = self._build_spec(
                task,
                attempt,
                runtime_seconds=runtime_seconds,
            )
            return {
                "job_id": expected_job_id,
                "owner_principal_id": spec.identity.owner_principal_id,
                "owner_kind": spec.identity.owner_kind,
                "service_id": spec.service_id,
                "goal_id": spec.goal_id,
                "goal_revision": spec.goal_revision,
                "operator_session_id": spec.operator_session_id,
                "session_id": spec.session_id,
                "capability_id": spec.identity.job_kind,
                "capability_version": spec.identity.capability_version,
                "input_digest": _safe_digest(spec.inputs),
                "authority_digest": _safe_digest(spec.declared_authority),
                "run_fingerprint": spec.run_fingerprint,
                "idempotency_scope": spec.identity.idempotency_scope,
                "idempotency_key": spec.identity.idempotency_key,
            }
        return WorkBoardDispatcher._direct_expected_identity(task, attempt, inputs, projection)

    async def _cleanup_adapter(
        self,
        task: WorkBoardTask,
        attempt: WorkBoardAttempt,
        inputs: Mapping[str, Any],
        projection: Mapping[str, Any],
        *,
        reason: str,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Invoke the existing capability cleanup hook with its own fences."""

        capability = _text(task.capability_id)
        receipts: list[dict[str, Any]] = []
        try:
            if capability == GOAL_SNAPSHOT_CAPABILITY:
                worker = self._active_worker_tasks.get((task.task_id, attempt.attempt_id))
                if worker is not None and worker is not asyncio.current_task() and not worker.done():
                    worker.cancel()
                    try:
                        await worker
                    except asyncio.CancelledError:
                        pass
                return receipts, True
            if capability == "guardian.research-watch.v1":
                from src.guardian.source_watch import source_watch_service

                watch = await source_watch_service.get_watch(
                    _text(inputs["watch_id"]),
                    owner_principal_id=task.owner_principal_id,
                    owner_session_id=task.owner_session_id,
                )
                if not isinstance(watch, Mapping):
                    return receipts, False
                if int(watch.get("plan_revision") or 0) != int(inputs["expected_plan_revision"]):
                    receipts.append({"job_id": attempt.workflow_run_id, "status": "unknown", "reason_code": "goal_revision_stale"})
                    return receipts, False
                result = await source_watch_service.cancel_watch_job(
                    watch_id=_text(inputs["watch_id"]),
                    job_id=_text(attempt.workflow_run_id),
                    expected_plan_revision=int(watch.get("plan_revision") or 0),
                    expected_fencing_token=int(watch.get("active_job_fence") or 0),
                    owner_principal_id=task.owner_principal_id,
                    owner_session_id=task.owner_session_id,
                )
                receipts.append({"job_id": attempt.workflow_run_id, "status": _status(result), "reason_code": "cancelled"})
                return receipts, _status(result) == "cancelled"
            if capability == "engineering.repo-change.v1":
                from src.api.workflows import (
                    authenticate_repo_change_operator,
                    cancel_repo_change_for_authenticated_operator,
                )

                operator = await authenticate_repo_change_operator(
                    task.owner_session_id,
                    owner_principal_id=task.owner_principal_id,
                )
                result = await cancel_repo_change_for_authenticated_operator(
                    _text(attempt.workflow_run_id),
                    operator=operator,
                    reason=reason,
                )
                result_status = _status(result)
                proven = result_status == "cancelled"
                receipts.append(
                    {
                        "job_id": attempt.workflow_run_id,
                        "status": "cancelled" if proven else "unknown",
                        "reason_code": "operator_cancelled" if proven else "cleanup_unproven",
                    }
                )
                return receipts, proven
            if capability == "work.github-followthrough.v1":
                from src.extensions.github_followthrough import GitHubFollowthroughService

                result = await GitHubFollowthroughService().cancel(
                    owner_principal_id=task.owner_principal_id,
                    owner_session_id=task.owner_session_id,
                    job_id=_text(attempt.workflow_run_id),
                )
                receipts.append({"job_id": attempt.workflow_run_id, "status": _status(result), "reason_code": "cancelled"})
                return receipts, _status(result) in {"cancelled", "succeeded"}
            if capability == "guardian-routine.v1":
                from src.workflows.routines import routine_service

                result = await routine_service.cancel_invocation_job_tree(
                    _text(attempt.workflow_run_id),
                    routine_id=_text(inputs["routine_id"]),
                    owner_principal_id=task.owner_principal_id,
                    owner_session_id=task.owner_session_id,
                    reason=reason[:128],
                )
                # The routine helper returns every root/descendant projection.
                # A non-empty list alone is not proof of cleanup: a child can
                # remain running or carry an unresolved effect.  Preserve only
                # safe projections and let the common tree readback perform its
                # independent second check.
                if not isinstance(result, list) or not result:
                    return receipts, False
                proven = True
                for item in result:
                    if not isinstance(item, Mapping):
                        proven = False
                        continue
                    item_status = _status(item)
                    item_effects = item.get("effects") if isinstance(item.get("effects"), list) else []
                    unresolved = item_status in UNCERTAIN_EXTERNAL_EFFECT_STATUSES or any(
                        isinstance(effect, Mapping)
                        and _status(effect.get("status")) in {
                            "unknown",
                            "intent",
                            "dispatched",
                            "unknown_external_effect",
                            "cost_liability",
                        }
                        for effect in item_effects
                    )
                    if item_status not in {"cancelled", "failed", "succeeded", "degraded"} or unresolved:
                        proven = False
                    receipts.append(
                        {
                            "job_id": _text(item.get("job_id") or item.get("run_identity")),
                            "workflow_run_id": _text(item.get("run_identity") or item.get("job_id")),
                            "status": item_status if item_status in {"cancelled", "failed", "succeeded", "degraded"} else "unknown",
                            "reason_code": "operator_cancelled" if item_status == "cancelled" else "cleanup_unproven",
                        }
                    )
                return receipts, proven
        except Exception as exc:
            logger.info("work-board adapter cleanup requires reconciliation: %s", type(exc).__name__)
            receipts.append({"job_id": attempt.workflow_run_id, "status": "unknown", "reason_code": "cleanup_unproven"})
            return receipts, False
        # Every registered capability must have an explicit cleanup branch.
        # Falling through is a fail-closed unknown outcome.
        return receipts, False

    async def _project_cancel_result(
        self,
        task: WorkBoardTask,
        attempt: WorkBoardAttempt,
        root: Mapping[str, Any] | None,
        receipts: list[Mapping[str, Any]],
        *,
        cleanup_proven: bool,
    ) -> BoardAttemptProjection:
        job_id = _text(attempt.workflow_run_id)
        root_status = _status(root)
        tree_proven = await self._cancel_tree_readback(
            job_id,
            root,
            receipts,
        )
        unknown = not cleanup_proven or not tree_proven or root_status in UNCERTAIN_EXTERNAL_EFFECT_STATUSES
        safe_receipts = [
            {
                "job_id": _text(item.get("job_id") or item.get("run_identity")) or job_id,
                "workflow_run_id": _text(item.get("run_identity") or item.get("job_id")) or job_id,
                "status": _status(item) if _status(item) in {"cancelled", "succeeded", "failed", "blocked", "unknown", "unknown_external_effect", "cost_liability", "intent", "dispatched"} else "unknown",
                "reason_code": "reconcile_external_effect" if unknown else "operator_cancelled",
                "readback_status": "unknown" if unknown else "not_applicable",
                "verification_status": "reconciliation_required" if unknown else "cancelled",
            }
            for item in receipts
            if isinstance(item, Mapping)
        ]
        if root_status == "succeeded":
            proof = self._workflow_readback(root or {}, job_id)
            if proof is not None and not unknown:
                status = WorkBoardStatus.review if task.requires_review else WorkBoardStatus.done
                outcome = "verified"
                block_kind = None
                block_reason = None
            else:
                status = WorkBoardStatus.blocked
                outcome = "verified_readback_missing"
                block_kind = "unknown_effect" if unknown else "transient"
                block_reason = "reconcile_external_effect" if unknown else "verified_readback_missing"
                proof = None
        else:
            status = WorkBoardStatus.blocked
            outcome = "unknown_external_effect" if unknown else "cancelled"
            block_kind = "unknown_effect" if unknown else "cancelled"
            block_reason = "reconcile_external_effect" if unknown else "cancelled"
            proof = None
        async with self.session_provider() as db:
            return await self.repository.project_attempt(
                db,
                task.task_id,
                attempt.attempt_id,
                expected_revision=task.task_revision,
                board_fence=attempt.fencing_token,
                lease_owner=attempt.lease_owner or self.runner_id,
                status=status,
                outcome=outcome,
                verified_readback=proof,
                block_kind=block_kind,
                block_reason=block_reason,
                result_refs=safe_receipts or [{"job_id": job_id, "status": "unknown", "reason_code": block_kind or "operator_cancelled"}],
                receipt_refs=safe_receipts,
                actor_principal_id=self.runner_id,
                actor_session_id=self.runner_session,
            )

    async def _cancel_tree_readback(
        self,
        root_job_id: str,
        root: Mapping[str, Any] | None,
        receipts: list[Mapping[str, Any]],
    ) -> bool:
        """Read every returned root/descendant projection before board close."""

        root_status = _status(root)
        root_verified = root_status == "succeeded" and self._workflow_readback(root or {}, root_job_id) is not None
        identifiers: list[str] = [root_job_id]
        for receipt in receipts:
            if not isinstance(receipt, Mapping):
                return False
            identifier = _text(receipt.get("job_id") or receipt.get("run_identity"))
            if identifier and identifier not in identifiers:
                identifiers.append(identifier)
        projections: list[Mapping[str, Any]] = []
        for identifier in identifiers:
            projection = root if identifier == root_job_id and isinstance(root, Mapping) else await self.jobs.get_job(identifier)
            if not isinstance(projection, Mapping):
                return False
            projections.append(projection)
        for index, projection in enumerate(projections):
            status = _status(projection)
            effects = projection.get("effects") if isinstance(projection.get("effects"), list) else []
            if status in UNCERTAIN_EXTERNAL_EFFECT_STATUSES or any(
                isinstance(effect, Mapping)
                and _status(effect.get("status")) in {"unknown", "intent", "dispatched", "unknown_external_effect", "cost_liability"}
                for effect in effects
            ):
                return False
            if status in {"accepted", "queued", "running", "awaiting_approval", "paused"}:
                return False
            if index > 0 and status in {"succeeded", "degraded"} and not root_verified:
                return False
        return bool(root_verified or root_status in {"cancelled", "failed", "blocked"})

    async def _effective_runtime(self, task: WorkBoardTask) -> int:
        """Resolve the current goal admission deadline, never from card input."""

        async with self.session_provider() as db:
            goal = (
                await db.execute(
                    select(Goal).where(
                        Goal.id == task.goal_id,
                        Goal.owner_principal_id == task.owner_principal_id,
                        Goal.owner_session_id == task.owner_session_id,
                        Goal.revision == task.goal_revision,
                    )
                )
            ).scalar_one_or_none()
        if goal is None:
            return DEFAULT_RUNTIME_SECONDS
        budget = deserialize_admission_budget(goal)
        configured = int(getattr(budget, "max_runtime_seconds", DEFAULT_RUNTIME_SECONDS)) if budget else DEFAULT_RUNTIME_SECONDS
        return max(1, min(configured, MAX_RUNTIME_SECONDS))

    async def _readiness(self, task: WorkBoardTask) -> tuple[str | None, str | None]:
        """Check live owner/goal authority before a claim is made."""

        try:
            operator = await authenticate_session(task.owner_session_id, touch=False)
        except AuthFailure as exc:
            # The explicit test bypass is only a test configuration facility;
            # production always requires a persisted, live operator session.
            if not (
                settings.deployment_environment == "test"
                and settings.operator_auth_allow_unauthenticated_tests
                and task.owner_session_id == "test-auth-bypass"
                and task.owner_principal_id == "operator:test-bypass"
            ):
                return exc.code, "The task owner session is not valid"
            operator = None
        if operator is not None and str(operator.principal.principal_id) != str(task.owner_principal_id):
            return "owner_mismatch", "The task owner session belongs to another principal"
        async with self.session_provider() as db:
            goal = (
                await db.execute(
                    select(Goal).where(
                        Goal.id == task.goal_id,
                        Goal.owner_principal_id == task.owner_principal_id,
                        Goal.owner_session_id == task.owner_session_id,
                    )
                )
            ).scalar_one_or_none()
        if goal is None:
            return "goal_not_found_or_not_owned", "The task goal is missing or owned by another operator"
        if int(goal.revision or 0) != int(task.goal_revision):
            return "goal_revision_stale", "The task goal revision is stale"
        goal_status = _text(getattr(goal.status, "value", goal.status))
        if goal_status and goal_status not in {"active", "draft"}:
            return "goal_not_admitted", "The task goal is not currently executable"
        capability_id = _text(task.capability_id)
        spec = REGISTERED_CAPABILITIES.get(capability_id)
        if spec is None:
            return "capability_unregistered", "The task names no registered Seraph capability"
        if not _text(task.executor_id):
            return "executor_missing", "The task has no registered executor"
        if not _text(task.typed_input_ref) or not _text(task.typed_input_digest):
            return "typed_input_missing", "The task has no complete typed input reference"
        try:
            _parse_typed_input(task)
        except TypedInputError as exc:
            return exc.code, str(exc)
        return None, None

    def _build_spec(
        self,
        task: WorkBoardTask,
        attempt: WorkBoardAttempt,
        *,
        runtime_seconds: int = DEFAULT_RUNTIME_SECONDS,
    ) -> tuple[DurableJobSpec, dict[str, Any], str, str, int]:
        if _text(task.capability_id) != GOAL_SNAPSHOT_CAPABILITY:
            raise TypedInputError(
                "adapter_root_owned_by_capability",
                "Only GoalSnapshot uses the work-board wrapper root",
            )
        inputs = _parse_typed_input(task)
        runtime_seconds = max(1, min(runtime_seconds, MAX_RUNTIME_SECONDS))
        job_id = f"work-board:{task.task_id}:{attempt.attempt_id}"
        owner_principal = DISPATCHER_PRINCIPAL
        service_id = DISPATCHER_SERVICE
        declared_authority = {
            "principal": owner_principal,
            "owner_kind": "service",
            "service_id": service_id,
            "session_id": task.owner_session_id,
            "goal_owner_principal_id": task.owner_principal_id,
            "goal_owner_session_id": task.owner_session_id,
            "capability_id": task.capability_id,
            "capability_version": REGISTERED_CAPABILITIES[_text(task.capability_id)].version,
            "finite_authority": True,
            "budget_microusd": 0,
            "limits": {
                "runtime_seconds": runtime_seconds,
                "max_attempts": MAX_ATTEMPTS_PER_TASK,
            },
        }
        safe_inputs = {
            "task_id": task.task_id,
            "attempt_id": attempt.attempt_id,
            "capability_id": task.capability_id,
            "typed_input_ref": task.typed_input_ref,
            "typed_input_digest": task.typed_input_digest,
            **inputs,
        }
        deadline = self.now() + timedelta(seconds=runtime_seconds)
        spec = DurableJobSpec(
            identity=DurableJobIdentity(
                job_id=job_id,
                owner_kind="service",
                owner_principal_id=owner_principal,
                job_kind=_text(task.capability_id),
                capability_version=REGISTERED_CAPABILITIES[_text(task.capability_id)].version,
                idempotency_scope="work-board-attempt",
                idempotency_key=f"{task.task_id}:{attempt.attempt_id}",
            ),
            inputs=safe_inputs,
            session_id=task.owner_session_id,
            conversation_id=task.owner_session_id,
            operator_session_id=task.owner_session_id,
            goal_id=task.goal_id,
            goal_revision=task.goal_revision,
            priority=task.priority,
            resource_claims=(f"executor:{task.executor_id}",),
            declared_authority=declared_authority,
            deadline_at=deadline,
            max_attempts=1,
            service_id=service_id,
            run_fingerprint=_safe_digest(safe_inputs),
            budget_microusd=0,
        )
        return spec, inputs, job_id, owner_principal, runtime_seconds

    async def _admit_execute_project(self, claim: BoardDispatchClaim) -> dict[str, Any]:
        task, attempt = claim.task, claim.attempt
        result: dict[str, Any] = {"admitted": False, "completed": False, "blocked": False}
        runtime_seconds = await self._effective_runtime(task)
        if _text(task.capability_id) != GOAL_SNAPSHOT_CAPABILITY:
            try:
                inputs = _parse_typed_input(task)
                return await self._admit_execute_direct(claim, inputs, runtime_seconds=runtime_seconds)
            except Exception as exc:
                logger.info("work board adapter %s blocked before admission: %s", task.capability_id, type(exc).__name__)
                await self._close_unadmitted_or_block(claim, _safe_error_code(exc))
                result["blocked"] = True
                return result
        try:
            spec, inputs, expected_job_id, parent_owner, runtime_seconds = self._build_spec(
                task,
                attempt,
                runtime_seconds=runtime_seconds,
            )
        except Exception as exc:
            await self._project_blocked(claim, "admission_contract_invalid", str(type(exc).__name__))
            result["blocked"] = True
            return result

        try:
            admission = await self.jobs.admit_job(spec)
            job_id = _text(admission.get("job_id"))
            if job_id != expected_job_id:
                raise DurableJobIdempotencyConflict("board admission returned a mismatched job identity")
            result["admitted"] = True
            async with self.session_provider() as db:
                linked = await self.repository.link_attempt_workflow_run(
                    db,
                    task.task_id,
                    attempt.attempt_id,
                    workflow_run_id=job_id,
                    expected_revision=task.task_revision,
                    board_fence=attempt.fencing_token,
                    lease_owner=self.runner_id,
                    workflow_projection=admission,
                    expected_identity={
                        "owner_principal_id": spec.identity.owner_principal_id,
                        "owner_kind": spec.identity.owner_kind,
                        "service_id": spec.service_id,
                        "goal_id": spec.goal_id,
                        "goal_revision": spec.goal_revision,
                        "operator_session_id": spec.operator_session_id,
                        "session_id": spec.session_id,
                        "capability_id": spec.identity.job_kind,
                        "capability_version": spec.identity.capability_version,
                        "input_digest": _safe_digest(spec.inputs),
                        "authority_digest": _safe_digest(spec.declared_authority),
                        "run_fingerprint": spec.run_fingerprint,
                        "idempotency_scope": spec.identity.idempotency_scope,
                        "idempotency_key": spec.identity.idempotency_key,
                    },
                    actor_principal_id=self.runner_id,
                    actor_session_id=self.runner_session,
                )
            board_revision = linked.task.task_revision
            queued = await self.jobs.queue_job(
                job_id,
                expected_revision=admission.get("revision"),
            )
            claimed = await self.jobs.claim_job(
                job_id,
                owner=f"{self.runner_id}:{attempt.attempt_id}",
                lease_seconds=runtime_seconds,
                expected_state="queued",
                expected_revision=queued.get("revision"),
                expected_fencing_token=(queued.get("lease") or {}).get("fencing_token"),
            )
            parent_runtime_owner, parent_fence = _lease(claimed)
            if parent_runtime_owner is None or parent_fence is None:
                raise DurableJobError("parent durable job did not return a current lease fence")
            outcome = await self._execute_registered(
                task,
                attempt,
                inputs,
                job_id=job_id,
                parent_runtime_owner=parent_runtime_owner,
                parent_fence=parent_fence,
                runtime_seconds=runtime_seconds,
            )
            await self._settle_parent(
                job_id,
                parent_runtime_owner,
                parent_fence,
                outcome,
            )
            projection = await self.jobs.get_job(job_id)
            final_status = _status(projection)
            if outcome.get("verified") and final_status == "succeeded":
                proof = {
                    "source": "workflow_run",
                    "status": "succeeded",
                    "verified": True,
                    "workflow_run_id": job_id,
                    "content_sha256": outcome.get("content_sha256"),
                }
                target_status = WorkBoardStatus.review if task.requires_review else WorkBoardStatus.done
                await self._project(
                    task,
                    attempt,
                    board_revision=board_revision,
                    status=target_status,
                    outcome="verified",
                    proof=proof,
                    result_refs=outcome.get("result_refs"),
                    artifact_refs=outcome.get("artifact_refs"),
                )
                result["completed"] = True
            else:
                raw_reason = _text(outcome.get("reason")) or _text(projection.get("failure_reason") if isinstance(projection, Mapping) else "")
                reason = "unknown_effect" if outcome.get("unknown_effect") else _stable_reason_code(raw_reason)
                block_kind = reason
                await self._project(
                    task,
                    attempt,
                    board_revision=board_revision,
                    status=WorkBoardStatus.blocked,
                    outcome=reason,
                    block_kind=block_kind,
                    block_reason=reason,
                    result_refs=outcome.get("result_refs"),
                    artifact_refs=outcome.get("artifact_refs"),
                )
                result["blocked"] = True
        except (DurableJobAdmissionDenied, DurableJobIdempotencyConflict, DurableJobError, BoardError) as exc:
            logger.info("work board task %s blocked: %s", task.task_id, type(exc).__name__)
            await self._project_blocked(claim, "admission_or_execution_blocked", type(exc).__name__)
            result["blocked"] = True
        except Exception as exc:
            logger.exception("work board task %s failed", task.task_id)
            await self._project_blocked(claim, "dispatcher_failure", type(exc).__name__)
            result["blocked"] = True
        return result

    async def _admit_execute_direct(
        self,
        claim: BoardDispatchClaim,
        inputs: Mapping[str, Any],
        *,
        runtime_seconds: int,
    ) -> dict[str, Any]:
        """Run one existing capability service with its canonical root identity.

        The board claim is persisted before this method is entered.  Each
        adapter owns its historical root and performs its own durable admission;
        the common ``work-board-attempt`` binding makes a restart lookup safe.
        """

        task, attempt = claim.task, claim.attempt
        result: dict[str, Any] = {"admitted": False, "completed": False, "blocked": False}
        adapter_result: Mapping[str, Any] = {}
        try:
            adapter_result = await self._execute_direct_adapter(
                task,
                attempt,
                inputs,
                runtime_seconds=runtime_seconds,
                admission_only=True,
            )
        except Exception as exc:
            # A service may fail after durable admission but before returning
            # its receipt.  Resolve the exact common binding before deciding
            # whether this claim can be discarded.
            adapter_result = {"status": "blocked", "reason_code": _stable_reason_code(_safe_error_code(exc))}

        job_id = self._adapter_job_id(adapter_result)
        if not job_id:
            job_id = await self._lookup_direct_job_id(task, attempt, inputs)
        if not job_id:
            await self._close_unadmitted_or_block(
                claim,
                _stable_reason_code(adapter_result.get("reason_code"), fallback="admission_binding_missing"),
            )
            result["blocked"] = True
            return result
        projection = await self.jobs.get_job(job_id)
        if not isinstance(projection, Mapping):
            await self._project_blocked(claim, "unknown_effect", "durable_run_projection_missing")
            result["blocked"] = True
            return result
        result["admitted"] = True
        try:
            expected = self._direct_expected_identity(task, attempt, inputs, projection)
            async with self.session_provider() as db:
                linked = await self.repository.link_attempt_workflow_run(
                    db,
                    task.task_id,
                    attempt.attempt_id,
                    workflow_run_id=job_id,
                    expected_revision=task.task_revision,
                    board_fence=attempt.fencing_token,
                    lease_owner=self.runner_id,
                    workflow_projection=projection,
                    expected_identity=expected,
                    actor_principal_id=self.runner_id,
                    actor_session_id=self.runner_session,
                )
            board_revision = linked.task.task_revision
            if adapter_result.get("admission_only") is True:
                # The adapter has only admitted/prepared its canonical root.
                # The immutable board link is now durable, so the second
                # phase may enter the capability's existing execution path.
                executed = await self._execute_direct_adapter(
                    task,
                    attempt,
                    inputs,
                    runtime_seconds=runtime_seconds,
                    admission_only=False,
                )
                returned_job_id = self._adapter_job_id(executed)
                if returned_job_id and returned_job_id != job_id:
                    raise DurableJobIdempotencyConflict(
                        "direct adapter execution returned a different durable root"
                    )
                adapter_result = executed
                projection = await self.jobs.get_job(job_id)
                if not isinstance(projection, Mapping):
                    raise DurableJobError("durable_run_projection_missing_after_execution")
            safe_status = _status(adapter_result.get("status")) or _status(projection)
            reason = _stable_reason_code(
                _text(adapter_result.get("reason_code")) or _text(projection.get("failure_reason")),
            )
            unresolved = safe_status in {"unknown_external_effect", "cost_liability"} or _status(projection) in {
                "unknown_external_effect",
                "cost_liability",
            }
            if unresolved:
                await self._project(
                    task,
                    attempt,
                    board_revision=board_revision,
                    status=WorkBoardStatus.blocked,
                    outcome="unknown_effect",
                    block_kind="unknown_effect",
                    block_reason="reconcile_admission_binding",
                    result_refs=[{"job_id": job_id, "workflow_run_id": job_id, "status": "unknown", "recovery_action": "reconcile_admission_binding"}],
                )
                result["blocked"] = True
                return result
            verified = self._direct_verified(adapter_result, projection, job_id)
            if verified:
                proof = {
                    "source": "workflow_run",
                    "status": "succeeded",
                    "verified": True,
                    "workflow_run_id": job_id,
                    "content_sha256": _text(adapter_result.get("content_sha256")) or _text(projection.get("result_digest")),
                }
                target_status = WorkBoardStatus.review if task.requires_review else WorkBoardStatus.done
                await self._project(
                    task,
                    attempt,
                    board_revision=board_revision,
                    status=target_status,
                    outcome="verified",
                    proof=proof,
                    result_refs=[{"job_id": job_id, "workflow_run_id": job_id, "status": "succeeded", "verified": True}],
                    artifact_refs=adapter_result.get("artifact_refs"),
                )
                result["completed"] = True
                return result
            block_kind = "needs_input" if safe_status in {"awaiting_approval", "needs_input"} else (
                "capability" if safe_status in {"blocked", "deferred"} else "transient"
            )
            recovery = "approve_existing_run" if block_kind == "needs_input" else (
                "retry_after_prerequisite" if block_kind == "capability" else "operator_retry"
            )
            await self._project(
                task,
                attempt,
                board_revision=board_revision,
                status=WorkBoardStatus.blocked,
                outcome=reason or _stable_reason_code(safe_status),
                block_kind=block_kind,
                block_reason=reason or _stable_reason_code(safe_status),
                result_refs=[{"job_id": job_id, "workflow_run_id": job_id, "status": "blocked", "reason_code": reason or _stable_reason_code(safe_status), "recovery_action": recovery}],
            )
            result["blocked"] = True
        except Exception as exc:
            logger.info("work board direct adapter %s reconciliation blocked: %s", task.task_id, type(exc).__name__)
            await self._project_blocked(claim, "unknown_effect", "reconcile_admission_binding")
            result["blocked"] = True
        return result

    async def _execute_direct_adapter(
        self,
        task: WorkBoardTask,
        attempt: WorkBoardAttempt,
        inputs: Mapping[str, Any],
        *,
        runtime_seconds: int,
        admission_only: bool = False,
    ) -> Mapping[str, Any]:
        capability_id = _text(task.capability_id)
        board_binding = f"{task.task_id}:{attempt.attempt_id}"
        if capability_id == "guardian.research-watch.v1":
            from src.guardian.source_watch import source_watch_service

            occurrence = _board_attempt_uuid(attempt.attempt_id, task.task_id).hex
            return await source_watch_service.run_watch(
                _text(inputs["watch_id"]),
                occurrence_id=occurrence,
                expected_plan_revision=int(inputs["expected_plan_revision"]),
                expected_owner_session_id=task.owner_session_id,
                work_board_task_id=task.task_id,
                work_board_attempt_id=attempt.attempt_id,
                admit_only=admission_only,
            )
        if capability_id == "engineering.repo-change.v1":
            from src.api.workflows import (
                RepoChangePreviewRequest,
                _preview_repo_change_for_operator,
                authenticate_repo_change_operator,
            )

            operator = await authenticate_repo_change_operator(
                task.owner_session_id,
                owner_principal_id=task.owner_principal_id,
            )
            request = RepoChangePreviewRequest(
                goal_id=task.goal_id,
                goal_revision=task.goal_revision,
                candidate_id=_text(inputs["candidate_id"]),
                idempotency_key=board_binding,
                evidence_refs=list(inputs.get("evidence_refs") or []),
                repository_path=_text(inputs["repository_path"]),
                patch_artifact_id=_text(inputs["patch_artifact_id"]),
                patch_sha256=_text(inputs["patch_sha256"]).lower(),
                allowed_paths=list(inputs["allowed_paths"]),
                test_args=list(inputs["test_args"]),
                priority=int(task.priority),
                deadline_seconds=max(30, min(int(runtime_seconds), 180)),
            )
            prepared = await _preview_repo_change_for_operator(
                request,
                operator,
                work_board_task_id=task.task_id,
                work_board_attempt_id=attempt.attempt_id,
            )
            # RepoChange preview admits/holds the durable approval and does
            # not start the sandbox.  Mark that effect-free phase so the
            # board links the immutable root before any later resume path.
            return {**prepared, "admission_only": admission_only}
        if capability_id == "work.github-followthrough.v1":
            from src.extensions.github_followthrough import GitHubFollowthroughService, PrepareRequest

            attempt_uuid = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"seraph:work-board-attempt:{task.task_id}:{attempt.attempt_id}",
            )
            request = PrepareRequest(
                conversation_id=task.owner_session_id,
                goal_id=task.goal_id,
                goal_revision=task.goal_revision,
                dossier_artifact_id=_text(inputs["dossier_artifact_id"]),
                dossier_sha256=_text(inputs["dossier_sha256"]).lower(),
                connection_revision=int(inputs["connection_revision"]),
                action=_text(inputs["action"]),
                title=inputs.get("title"),
                body=_text(inputs["body"]),
                issue_number=inputs.get("issue_number"),
                idempotency_key=str(attempt_uuid),
            )
            prepared = await GitHubFollowthroughService().prepare(
                owner_principal_id=task.owner_principal_id,
                owner_session_id=task.owner_session_id,
                external_mutation_granted=False,
                work_board_idempotency_key=board_binding,
                request=request,
            )
            # GitHub prepare writes only the governed payload/approval
            # admission. Publication remains a separate approved route.
            return {**prepared, "admission_only": admission_only}
        if capability_id == "guardian-routine.v1":
            from src.workflows.routines import RoutineInvokeRequest, routine_service

            attempt_uuid = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"seraph:work-board-attempt:{task.task_id}:{attempt.attempt_id}",
            )
            request = RoutineInvokeRequest(
                version=int(inputs["version"]),
                expected_routine_revision=int(inputs["expected_routine_revision"]),
                goal_id=task.goal_id,
                expected_goal_revision=task.goal_revision,
                source_watch_id=_text(inputs["source_watch_id"]),
                expected_watch_revision=int(inputs["expected_watch_revision"]),
                invocation_uuid=str(attempt_uuid),
            )
            prepared = await routine_service.invoke(
                _text(inputs["routine_id"]),
                request,
                owner_principal_id=task.owner_principal_id,
                owner_session_id=task.owner_session_id,
                work_board_idempotency_key=board_binding,
            )
            # Routine invoke admits/holds its invocation approval. Child
            # capability steps execute only after that existing approval path.
            return {**prepared, "admission_only": admission_only}
        raise TypedInputError("capability_unregistered", "the task names no registered capability")

    @staticmethod
    def _adapter_job_id(result: Mapping[str, Any]) -> str | None:
        for candidate in (
            result.get("job_id"),
            (result.get("job") or {}).get("job_id") if isinstance(result.get("job"), Mapping) else None,
            (result.get("job") or {}).get("run_identity") if isinstance(result.get("job"), Mapping) else None,
        ):
            value = _text(candidate)
            if value:
                return value
        return None

    @staticmethod
    def _direct_job_identity(
        task: WorkBoardTask,
        attempt: WorkBoardAttempt,
        inputs: Mapping[str, Any],
    ) -> tuple[str, str, str, str | None, str]:
        """Return the reviewed root/binding identity for one direct adapter.

        The durable binding is the recovery authority.  These values are
        derived from the live task and typed input, never from a worker
        response, so a lookup cannot adopt a same-key run belonging to a
        different capability or owner.
        """

        capability_id = _text(task.capability_id)
        binding_key = f"{task.task_id}:{attempt.attempt_id}"
        if capability_id == "guardian.research-watch.v1":
            occurrence = _board_attempt_uuid(attempt.attempt_id, task.task_id).hex
            watch_id = _text(inputs.get("watch_id"))
            return (
                f"source-watch:{watch_id}:{occurrence}",
                "service:guardian-source-watch",
                "guardian_source_watch",
                "guardian-source-watch",
                binding_key,
            )
        if capability_id == "engineering.repo-change.v1":
            from src.api.workflows import _repo_change_job_id

            return (
                _repo_change_job_id(task.owner_principal_id, binding_key),
                task.owner_principal_id,
                "engineering.repo-change.v1",
                None,
                binding_key,
            )
        if capability_id == "work.github-followthrough.v1":
            from src.extensions.github_followthrough import _operation_id

            attempt_uuid = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"seraph:work-board-attempt:{task.task_id}:{attempt.attempt_id}",
            )
            return (
                f"ghfollow_{_operation_id(task.owner_principal_id, attempt_uuid).hex}",
                task.owner_principal_id,
                "github_followthrough_v1",
                None,
                binding_key,
            )
        if capability_id == "guardian-routine.v1":
            routine_id = _text(inputs.get("routine_id"))
            attempt_uuid = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"seraph:work-board-attempt:{task.task_id}:{attempt.attempt_id}",
            )
            return (
                f"routine-invocation:{routine_id}:{str(attempt_uuid)}",
                task.owner_principal_id,
                "routine_invocation",
                None,
                binding_key,
            )
        raise TypedInputError("capability_unregistered", "the task names no registered capability")

    @staticmethod
    def _direct_input_digest(
        task: WorkBoardTask,
        attempt: WorkBoardAttempt,
        inputs: Mapping[str, Any],
    ) -> str:
        """Compute the service input digest where the adapter contract is closed."""

        capability_id = _text(task.capability_id)
        if capability_id == "guardian.research-watch.v1":
            occurrence = _board_attempt_uuid(attempt.attempt_id, task.task_id).hex
            return _safe_digest({"watch_id": _text(inputs.get("watch_id")), "occurrence_id": occurrence})
        if capability_id == "engineering.repo-change.v1":
            return _safe_digest(
                {
                    "candidate_id": _text(inputs.get("candidate_id")),
                    "repository_path": _text(inputs.get("repository_path")),
                    "patch_artifact_id": _text(inputs.get("patch_artifact_id")),
                    "patch_sha256": _text(inputs.get("patch_sha256")).lower(),
                    "allowed_paths": list(inputs.get("allowed_paths") or []),
                    "test_args": list(inputs.get("test_args") or []),
                    "evidence_refs": list(inputs.get("evidence_refs") or []),
                }
            )
        if capability_id == "work.github-followthrough.v1":
            attempt_uuid = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"seraph:work-board-attempt:{task.task_id}:{attempt.attempt_id}",
            )
            return _safe_digest(
                {
                    "dossier_artifact_id": _text(inputs.get("dossier_artifact_id")),
                    "dossier_sha256": _text(inputs.get("dossier_sha256")).lower(),
                    "connection_revision": int(inputs.get("connection_revision")),
                    "action": _text(inputs.get("action")),
                    "title": inputs.get("title"),
                    "body": _text(inputs.get("body")),
                    "issue_number": inputs.get("issue_number"),
                    "attempt_uuid": str(attempt_uuid),
                }
            )
        if capability_id == "guardian-routine.v1":
            attempt_uuid = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"seraph:work-board-attempt:{task.task_id}:{attempt.attempt_id}",
            )
            return _safe_digest(
                {
                    "routine_id": _text(inputs.get("routine_id")),
                    "routine_version": int(inputs.get("version")),
                    "source_watch_id": _text(inputs.get("source_watch_id")),
                    "source_watch_revision": int(inputs.get("expected_watch_revision")),
                    "invocation_uuid": str(attempt_uuid),
                }
            )
        raise TypedInputError("capability_unregistered", "the task names no registered capability")

    async def _lookup_direct_job_id(
        self,
        task: WorkBoardTask,
        attempt: WorkBoardAttempt,
        inputs: Mapping[str, Any],
    ) -> str | None:
        lookup = getattr(self.jobs, "get_by_idempotency_binding", None)
        if lookup is None:
            return None
        expected_job_id, owner, job_kind, service_id, binding_key = self._direct_job_identity(
            task,
            attempt,
            inputs,
        )
        capability_version = REGISTERED_CAPABILITIES[_text(task.capability_id)].version
        try:
            existing = await lookup(
                owner_principal_id=owner,
                goal_id=task.goal_id,
                goal_revision=task.goal_revision,
                idempotency_scope="work-board-attempt",
                idempotency_key=binding_key,
                expected_job_id=expected_job_id,
                owner_kind="service" if service_id else "user",
                service_id=service_id,
                session_id=task.owner_session_id,
                operator_session_id=task.owner_session_id,
                job_kind=job_kind,
                capability_version=capability_version,
                input_digest=self._direct_input_digest(task, attempt, inputs),
                authority_digest=self._direct_authority_digest(task, attempt, inputs),
                run_fingerprint=self._direct_run_fingerprint(task, attempt, inputs),
            )
        except DurableJobIdempotencyConflict:
            raise
        return _text(existing.get("job_id")) if isinstance(existing, Mapping) else None

    @staticmethod
    def _direct_expected_identity(
        task: WorkBoardTask,
        attempt: WorkBoardAttempt,
        inputs: Mapping[str, Any],
        projection: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        expected_job_id, owner_principal_id, job_kind, service_id, binding_key = WorkBoardDispatcher._direct_job_identity(
            task,
            attempt,
            inputs,
        )
        owner_kind = "service" if service_id else "user"
        capability_version = REGISTERED_CAPABILITIES[_text(task.capability_id)].version
        input_digest = _text((projection or {}).get("input_digest")) or WorkBoardDispatcher._direct_input_digest(task, attempt, inputs)
        authority_digest = _text((projection or {}).get("authority_digest")) or WorkBoardDispatcher._direct_authority_digest(task, attempt, inputs)
        run_fingerprint = _text((projection or {}).get("run_fingerprint")) or WorkBoardDispatcher._direct_run_fingerprint(task, attempt, inputs)
        return {
            "owner_principal_id": owner_principal_id,
            "owner_kind": owner_kind,
            "service_id": service_id,
            "job_id": expected_job_id,
            "job_kind": job_kind,
            "goal_id": task.goal_id,
            "goal_revision": task.goal_revision,
            "operator_session_id": task.owner_session_id,
            "session_id": task.owner_session_id,
            "capability_id": task.capability_id,
            "capability_version": capability_version,
            "idempotency_scope": "work-board-attempt",
            "idempotency_key": binding_key,
            "input_digest": input_digest,
            "authority_digest": authority_digest,
            "run_fingerprint": run_fingerprint,
        }

    @staticmethod
    def _direct_authority_digest(
        task: WorkBoardTask,
        attempt: WorkBoardAttempt,
        inputs: Mapping[str, Any],
    ) -> str:
        return _safe_digest(
            {
                "owner_principal_id": task.owner_principal_id,
                "owner_session_id": task.owner_session_id,
                "goal_id": task.goal_id,
                "goal_revision": task.goal_revision,
                "capability_id": task.capability_id,
                "executor_id": task.executor_id,
                "priority": task.priority,
                "attempt_id": attempt.attempt_id,
                "finite_authority": True,
                "runtime_cap": MAX_RUNTIME_SECONDS,
            }
        )

    @staticmethod
    def _direct_run_fingerprint(
        task: WorkBoardTask,
        attempt: WorkBoardAttempt,
        inputs: Mapping[str, Any],
    ) -> str:
        # Existing governed services use the canonical durable input digest as
        # their run fingerprint when they do not provide a separate one.
        return WorkBoardDispatcher._direct_input_digest(task, attempt, inputs)

    async def _close_unadmitted_or_block(self, claim: BoardDispatchClaim, reason: str) -> None:
        try:
            async with self.session_provider() as db:
                await self.repository.close_proved_absent_attempt(
                    db,
                    claim.task.task_id,
                    claim.attempt.attempt_id,
                    expected_revision=claim.task.task_revision,
                    board_fence=claim.attempt.fencing_token,
                    lease_owner=claim.attempt.lease_owner or self.runner_id,
                    absence_proven=True,
                    actor_principal_id=self.runner_id,
                    actor_session_id=self.runner_session,
                    now=self.now(),
                )
        except Exception:
            await self._project_blocked(claim, reason, reason)

    @staticmethod
    def _direct_verified(result: Mapping[str, Any], projection: Mapping[str, Any], job_id: str) -> bool:
        if _status(projection) != "succeeded" or _status(result) not in {"succeeded", "completed"}:
            return False
        if not bool(result.get("verified")):
            return False
        digest = _text(result.get("content_sha256")) or _text(projection.get("result_digest"))
        return bool(digest) and len(digest) == 64

    async def _execute_registered(
        self,
        task: WorkBoardTask,
        attempt: WorkBoardAttempt,
        inputs: Mapping[str, Any],
        *,
        job_id: str,
        parent_runtime_owner: str,
        parent_fence: int,
        runtime_seconds: int = DEFAULT_RUNTIME_SECONDS,
    ) -> dict[str, Any]:
        capability_id = _text(task.capability_id)
        if capability_id != GOAL_SNAPSHOT_CAPABILITY:
            return {
                "verified": False,
                "reason": REGISTERED_CAPABILITIES[capability_id].blocked_reason or "adapter_blocked",
                "result_refs": [{"reason_code": REGISTERED_CAPABILITIES[capability_id].blocked_reason or "adapter_blocked"}],
            }
        file_path = _text(inputs.get("file_path")) or f"work-board/{task.task_id}.md"
        principal = TrustPrincipal(
            principal_id="service:goal-snapshot",
            principal_type=PrincipalType.SERVICE,
            authenticated=True,
            grants=(AuthorityGrant.CAPABILITY_EXECUTE,),
            session_id=task.owner_session_id,
            # The board wrapper owns the parent lease.  The governed
            # GoalSnapshot adapter binds this service principal to its exact
            # deterministic child job when it creates the nested run.  A
            # parent job id here would make the adapter reject that child as
            # an authenticated identity mismatch.
            job_id=job_id,
        )
        request = GoalSnapshotToFileRequest(
            goal_id=task.goal_id,
            goal_revision=task.goal_revision,
            file_path=file_path,
            owner_principal_id="service:goal-snapshot",
            service_id="service:goal-snapshot",
            session_id=task.owner_session_id,
            goal_owner_principal_id=task.owner_principal_id,
            goal_owner_session_id=task.owner_session_id,
            parent_job_id=job_id,
            parent_fencing_token=parent_fence,
            max_attempts=1,
            deadline_at=self.now() + timedelta(seconds=runtime_seconds),
            expected_outcome=task.title,
            reason="operator_work_board_task",
        )
        tokens = set_runtime_context(
            task.owner_session_id,
            "high_risk",
            trust_principal=principal,
        )
        worker_request = WorkBoardWorkerRequest(
            task_id=task.task_id,
            attempt_id=attempt.attempt_id,
            expected_task_revision=task.task_revision,
            board_fencing_token=attempt.fencing_token,
            workflow_run_id=job_id,
            workflow_fencing_token=parent_fence,
        )
        current_worker = asyncio.current_task()
        if current_worker is not None:
            self._active_worker_tasks[(task.task_id, attempt.attempt_id)] = current_worker
        try:
            with WorkBoardWorkerHost(worker_request):
                child = await GoalSnapshotToFileService(
                    jobs=self.jobs,
                    authority_principal=principal,
                ).run(
                    request,
                    work_board_task_id=task.task_id,
                    work_board_attempt_id=attempt.attempt_id,
                )
        finally:
            if current_worker is not None:
                self._active_worker_tasks.pop((task.task_id, attempt.attempt_id), None)
            reset_runtime_context(tokens)
        if not isinstance(child, GoalSnapshotToFileResult):
            return {"verified": False, "reason": "adapter_result_invalid"}
        verified = (
            child.execution_status == "succeeded"
            and child.verification == "passed"
            and bool(child.content_sha256)
            and child.output_exists
            and child.workspace_contained
            and child.goal_id_read_back
        )
        unknown_effect = bool(child.reconciliation_required)
        refs = [
            {
                "job_id": child.job_id,
                "workflow_run_id": job_id,
                "status": child.durable_status,
                "content_sha256": child.content_sha256,
                "artifact_id": child.artifact_ref,
                "file_path": child.file_path,
                "verified": verified,
                "reason_code": _stable_reason_code(child.reason, fallback="execution_blocked"),
            }
        ]
        return {
            "verified": verified,
            "unknown_effect": unknown_effect,
            "reason": _stable_reason_code(child.reason or child.execution_status),
            "content_sha256": child.content_sha256,
            "result_refs": refs,
            "artifact_refs": [
                {
                    "artifact_id": child.artifact_ref,
                    "file_path": child.file_path,
                    "content_sha256": child.content_sha256,
                    "exists": child.output_exists,
                }
            ],
            "child_job_id": child.job_id,
        }

    async def _settle_parent(
        self,
        job_id: str,
        owner: str,
        fence: int,
        outcome: Mapping[str, Any],
    ) -> None:
        current = await self.jobs.get_job(job_id)
        if not isinstance(current, Mapping) or _status(current) != "running":
            return
        if outcome.get("verified"):
            target_path = _text((outcome.get("result_refs") or [{}])[0].get("file_path"))
            digest = _text(outcome.get("content_sha256"))
            readback = await self.jobs.record_effect(
                job_id,
                effect_type="board_child_readback",
                receipt_kind="readback",
                status="succeeded",
                target_path=target_path,
                target_digest=digest,
                content_sha256=digest,
                details={
                    "verified": True,
                    "output_exists": True,
                    "workspace_contained": True,
                    "goal_id_read_back": True,
                    "child_job_id": outcome.get("child_job_id"),
                },
                owner=owner,
                fencing_token=fence,
                expected_revision=current.get("revision"),
            )
            await self.jobs.transition_job(
                job_id,
                "succeeded",
                owner=owner,
                fencing_token=fence,
                expected_revision=readback.get("revision"),
                result={"child_job_id": outcome.get("child_job_id"), "content_sha256": digest},
                result_summary="board capability completed with independent readback",
            )
            return
        effect_status = "unknown" if outcome.get("unknown_effect") else "blocked"
        recorded = await self.jobs.record_effect(
            job_id,
            effect_type="board_child_execution",
            status=effect_status,
            details={
                "reason_code": _stable_reason_code(outcome.get("reason")),
                "child_job_id": outcome.get("child_job_id"),
            },
            owner=owner,
            fencing_token=fence,
            expected_revision=current.get("revision"),
        )
        await self.jobs.transition_job(
            job_id,
            "blocked",
            owner=owner,
            fencing_token=fence,
            expected_revision=recorded.get("revision"),
            reason=_text(outcome.get("reason"))[:256] or "board_child_blocked",
            result_summary="board capability requires operator recovery",
        )

    async def _project(
        self,
        task: WorkBoardTask,
        attempt: WorkBoardAttempt,
        *,
        board_revision: int,
        status: WorkBoardStatus,
        outcome: str,
        proof: Mapping[str, Any] | None = None,
        block_kind: str | None = None,
        block_reason: str | None = None,
        result_refs: Any = None,
        artifact_refs: Any = None,
        lease_owner: str | None = None,
    ) -> BoardAttemptProjection:
        async with self.session_provider() as db:
            return await self.repository.project_attempt(
                db,
                task.task_id,
                attempt.attempt_id,
                expected_revision=board_revision,
                board_fence=attempt.fencing_token,
                lease_owner=lease_owner or self.runner_id,
                status=status,
                outcome=outcome,
                verified_readback=dict(proof) if proof is not None else None,
                block_kind=block_kind,
                block_reason=block_reason,
                result_refs=result_refs,
                artifact_refs=artifact_refs,
                receipt_refs=result_refs,
                actor_principal_id=self.runner_id,
                actor_session_id=self.runner_session,
            )

    async def _project_blocked(
        self,
        claim: BoardDispatchClaim,
        block_kind: str,
        reason: str,
    ) -> None:
        try:
            await self._project(
                claim.task,
                claim.attempt,
                board_revision=claim.task.task_revision,
                status=WorkBoardStatus.blocked,
                outcome=_stable_reason_code(block_kind),
                block_kind=_stable_reason_code(block_kind),
                block_reason=_stable_reason_code(
                    reason,
                    fallback=_stable_reason_code(block_kind),
                ),
                result_refs=[{"reason_code": _stable_reason_code(block_kind)}],
            )
        except Exception:
            logger.exception("failed to project blocked board task %s", claim.task.task_id)

    async def _lookup_linked_binding(
        self,
        task: WorkBoardTask,
        attempt: WorkBoardAttempt,
        inputs: Mapping[str, Any],
    ) -> str | None:
        """Resolve the persisted root through its immutable admission binding."""

        lookup = getattr(self.jobs, "get_by_idempotency_binding", None)
        if lookup is None:
            raise DurableJobError("durable_binding_lookup_unavailable")
        capability_id = _text(task.capability_id)
        if capability_id == GOAL_SNAPSHOT_CAPABILITY:
            expected_job_id = f"work-board:{task.task_id}:{attempt.attempt_id}"
            owner_principal_id = DISPATCHER_PRINCIPAL
            owner_kind = "service"
            service_id = DISPATCHER_SERVICE
            job_kind = capability_id
            capability_version = REGISTERED_CAPABILITIES[capability_id].version
            spec, _, _, _, _ = self._build_spec(
                task,
                attempt,
                runtime_seconds=await self._effective_runtime(task),
            )
            expected_input_digest = _safe_digest(spec.inputs)
            expected_authority_digest = _safe_digest(spec.declared_authority)
            expected_run_fingerprint = spec.run_fingerprint
        else:
            expected_job_id, owner_principal_id, job_kind, service_id, _ = self._direct_job_identity(
                task,
                attempt,
                inputs,
            )
            owner_kind = "service" if service_id else "user"
            capability_version = REGISTERED_CAPABILITIES[capability_id].version
            expected_input_digest = self._direct_input_digest(task, attempt, inputs)
            expected_authority_digest = None
            expected_run_fingerprint = expected_input_digest
        try:
            projection = await lookup(
                owner_principal_id=owner_principal_id,
                goal_id=task.goal_id,
                goal_revision=task.goal_revision,
                idempotency_scope="work-board-attempt",
                idempotency_key=f"{task.task_id}:{attempt.attempt_id}",
                expected_job_id=expected_job_id,
                owner_kind=owner_kind,
                service_id=service_id,
                session_id=task.owner_session_id,
                operator_session_id=task.owner_session_id,
                job_kind=job_kind,
                capability_version=capability_version,
                input_digest=expected_input_digest,
                authority_digest=expected_authority_digest,
                run_fingerprint=expected_run_fingerprint,
            )
        except DurableJobIdempotencyConflict:
            raise
        if not isinstance(projection, Mapping):
            return None
        return _text(projection.get("job_id") or projection.get("run_identity")) or None

    async def reconcile_linked_attempts(self, *, now: datetime | None = None) -> list[str]:
        """Reconcile already-linked roots after a dispatcher restart.

        This path never reconstructs a fresh root or blindly calls an adapter.
        It adopts the persisted binding, lets the durable runtime recover an
        expired lease, and projects only a terminal state with an independent
        readback. Accepted/queued roots with an empty effect ledger may resume
        through their existing exact binding; any uncertain effect stops in a
        visible recovery block.
        """
        observed_at = now or self.now()
        recovered: list[str] = []
        async with self.session_provider() as db:
            linked = await self.repository.list_linked_active_attempts(
                db,
                limit=DISPATCH_PASS_LIMIT,
            )
        for task, attempt in linked:
            job_id = _text(attempt.workflow_run_id)
            if not job_id:
                continue
            try:
                projection = await self.jobs.get_job(job_id)
                if not isinstance(projection, Mapping):
                    await self._project(
                        task,
                        attempt,
                        board_revision=task.task_revision,
                        status=WorkBoardStatus.blocked,
                        outcome="unknown_effect",
                        block_kind="unknown_effect",
                        block_reason="durable_run_projection_missing",
                        result_refs=[{"job_id": job_id, "status": "unknown", "recovery_action": "reconcile_admission_binding"}],
                        lease_owner=attempt.lease_owner or self.runner_id,
                    )
                    recovered.append(job_id)
                    continue

                # A linked row is recoverable only when the persisted durable
                # admission still matches the exact per-capability root and
                # common board binding.  Looking up by a reconstructed job id
                # alone could adopt an unrelated run after a crash.
                inputs = _parse_typed_input(task)
                binding_job_id = await self._lookup_linked_binding(
                    task,
                    attempt,
                    inputs,
                )
                if binding_job_id != job_id:
                    raise DurableJobIdempotencyConflict(
                        "linked attempt durable binding does not match its root"
                    )

                if attempt.cancel_requested_at is not None:
                    cleanup_receipts, cleanup_proven = await self._cleanup_adapter(
                        task,
                        attempt,
                        inputs,
                        projection,
                        reason="operator_cancelled",
                    )
                    try:
                        tree_receipts = await self.jobs.cancel_job_tree(
                            job_id,
                            reason="operator_cancelled",
                        )
                    except Exception:
                        tree_receipts = []
                        cleanup_proven = False
                    latest_projection = await self.jobs.get_job(job_id) or projection
                    await self._project_cancel_result(
                        task,
                        attempt,
                        latest_projection,
                        [*cleanup_receipts, *tree_receipts],
                        cleanup_proven=cleanup_proven,
                    )
                    recovered.append(job_id)
                    continue

                status = _status(projection)
                effects = projection.get("effects") if isinstance(projection.get("effects"), list) else []
                unsafe_effect = status in {"unknown_external_effect", "cost_liability"} or any(
                    isinstance(effect, Mapping)
                    and _status(effect.get("status")) in {"unknown", "intent", "dispatched"}
                    for effect in effects
                )
                if unsafe_effect:
                    await self._project(
                        task,
                        attempt,
                        board_revision=task.task_revision,
                        status=WorkBoardStatus.blocked,
                        outcome="unknown_effect",
                        block_kind="unknown_effect",
                        block_reason="reconcile_external_effect",
                        result_refs=[{"job_id": job_id, "status": "unknown", "recovery_action": "reconcile_external_effect"}],
                        lease_owner=attempt.lease_owner or self.runner_id,
                    )
                    recovered.append(job_id)
                    continue

                lease = projection.get("lease") if isinstance(projection.get("lease"), Mapping) else {}
                lease_expired = False
                expires_at = lease.get("expires_at")
                if expires_at:
                    try:
                        expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                        if expiry.tzinfo is None:
                            expiry = expiry.replace(tzinfo=timezone.utc)
                        lease_expired = expiry <= observed_at
                    except (TypeError, ValueError):
                        lease_expired = True
                if status == "running" and lease_expired:
                    recover = getattr(self.jobs, "recover_stale_job", None)
                    if recover is None:
                        raise DurableJobError("stale_workflow_recovery_unavailable")
                    projection = await recover(job_id, now=observed_at)
                    status = _status(projection)
                    effects = projection.get("effects") if isinstance(projection.get("effects"), list) else []
                    if status in {"unknown_external_effect", "cost_liability"} or any(
                        isinstance(effect, Mapping)
                        and _status(effect.get("status")) in {"unknown", "intent", "dispatched"}
                        for effect in effects
                    ):
                        await self._project(
                            task,
                            attempt,
                            board_revision=task.task_revision,
                            status=WorkBoardStatus.blocked,
                            outcome="unknown_effect",
                            block_kind="unknown_effect",
                            block_reason="stale_workflow_requires_reconciliation",
                            result_refs=[{"job_id": job_id, "status": "unknown", "recovery_action": "reconcile_external_effect"}],
                            lease_owner=attempt.lease_owner or self.runner_id,
                        )
                        recovered.append(job_id)
                        continue

                if status in {"accepted", "queued"} and not effects:
                    # The root was admitted before the process stopped. Resume
                    # its durable state under the same binding. Only the local
                    # deterministic GoalSnapshot worker is resumed here; the
                    # other services own their existing approval/recovery
                    # routes and are invoked only through the exact binding.
                    if _text(task.capability_id) == GOAL_SNAPSHOT_CAPABILITY:
                        if status == "accepted":
                            projection = await self.jobs.queue_job(
                                job_id,
                                expected_revision=projection.get("revision"),
                            )
                        if _status(projection) == "queued":
                            projection = await self.jobs.claim_job(
                                job_id,
                                owner=f"{self.runner_id}:{attempt.attempt_id}",
                                lease_seconds=await self._effective_runtime(task),
                                expected_state="queued",
                                expected_revision=projection.get("revision"),
                                expected_fencing_token=(projection.get("lease") or {}).get("fencing_token"),
                            )
                        parent_owner, parent_fence = _lease(projection)
                        if parent_owner is None or parent_fence is None:
                            raise DurableJobError("stale_workflow_fence")
                        outcome = await self._execute_registered(
                            task,
                            attempt,
                            inputs,
                            job_id=job_id,
                            parent_runtime_owner=parent_owner,
                            parent_fence=parent_fence,
                            runtime_seconds=await self._effective_runtime(task),
                        )
                        await self._settle_parent(job_id, parent_owner, parent_fence, outcome)
                        projection = await self.jobs.get_job(job_id) or projection
                    else:
                        # Direct adapters own their historical root, but each
                        # has a durable idempotent resume path.  Re-enter the
                        # service only while the exact root is accepted or
                        # queued and its effect ledger is empty; this is the
                        # admission crash window and cannot replay an effect.
                        adapter_result = await self._execute_direct_adapter(
                            task,
                            attempt,
                            inputs,
                            runtime_seconds=await self._effective_runtime(task),
                        )
                        returned_job_id = self._adapter_job_id(adapter_result)
                        if returned_job_id and returned_job_id != job_id:
                            raise DurableJobIdempotencyConflict(
                                "direct adapter returned a different durable root"
                            )
                        projection = await self.jobs.get_job(job_id) or projection
                    status = _status(projection)

                if status in {"awaiting_approval", "blocked", "failed", "cancelled", "degraded"}:
                    reason = _stable_reason_code(_text(projection.get("failure_reason")) or status)
                    kind = "needs_input" if status == "awaiting_approval" else (
                        "unknown_effect" if status in {"unknown_external_effect", "cost_liability"} else "transient"
                    )
                    await self._project(
                        task,
                        attempt,
                        board_revision=task.task_revision,
                        status=WorkBoardStatus.blocked,
                        outcome=reason,
                        block_kind=kind,
                        block_reason=reason,
                        result_refs=[{"job_id": job_id, "status": "blocked", "reason_code": reason}],
                        lease_owner=attempt.lease_owner or self.runner_id,
                    )
                    recovered.append(job_id)
                    continue

                if status == "succeeded":
                    proof = self._workflow_readback(projection, job_id)
                    if proof is not None:
                        target = WorkBoardStatus.review if task.requires_review else WorkBoardStatus.done
                        await self._project(
                            task,
                            attempt,
                            board_revision=task.task_revision,
                            status=target,
                            outcome="verified",
                            proof=proof,
                            result_refs=[{"job_id": job_id, "workflow_run_id": job_id, "status": "succeeded", "verified": True}],
                            lease_owner=attempt.lease_owner or self.runner_id,
                        )
                    else:
                        await self._project(
                            task,
                            attempt,
                            board_revision=task.task_revision,
                            status=WorkBoardStatus.blocked,
                            outcome="verified_readback_missing",
                            block_kind="transient",
                            block_reason="Durable run succeeded but independent readback is missing",
                            result_refs=[{"job_id": job_id, "status": "blocked", "reason_code": "verified_readback_missing", "recovery_action": "operator_retry"}],
                            lease_owner=attempt.lease_owner or self.runner_id,
                        )
                    recovered.append(job_id)
            except Exception as exc:
                logger.info("linked work-board run %s requires recovery: %s", job_id, type(exc).__name__)
                try:
                    await self._project(
                        task,
                        attempt,
                        board_revision=task.task_revision,
                        status=WorkBoardStatus.blocked,
                        outcome="unknown_effect",
                        block_kind="unknown_effect",
                        block_reason="reconcile_admission_binding",
                        result_refs=[{"job_id": job_id, "status": "unknown", "recovery_action": "reconcile_admission_binding"}],
                        lease_owner=attempt.lease_owner or self.runner_id,
                    )
                    recovered.append(job_id)
                except Exception:
                    logger.exception("failed to project linked work-board run %s", job_id)
        return recovered

    @staticmethod
    def _workflow_readback(projection: Mapping[str, Any], job_id: str) -> dict[str, Any] | None:
        effects = projection.get("effects") if isinstance(projection.get("effects"), list) else []
        for effect in effects:
            if not isinstance(effect, Mapping):
                continue
            details = effect.get("details") if isinstance(effect.get("details"), Mapping) else {}
            if (
                _text(effect.get("receipt_kind")) == "readback"
                or bool(details.get("verified"))
            ) and _status(effect.get("status")) in {"succeeded", "read_back", "reconciled"}:
                digest = _text(effect.get("content_sha256")) or _text(effect.get("target_digest")) or _text(details.get("content_sha256"))
                if len(digest) == 64:
                    return {
                        "source": "workflow_run",
                        "status": "succeeded",
                        "verified": True,
                        "workflow_run_id": job_id,
                        "content_sha256": digest,
                    }
        result = projection.get("result") if isinstance(projection.get("result"), Mapping) else {}
        digest = _text(result.get("digest"))
        if bool(result.get("verified")) and len(digest) == 64:
            return {
                "source": "workflow_run",
                "status": "succeeded",
                "verified": True,
                "workflow_run_id": job_id,
                "content_sha256": digest,
            }
        return None

    async def reconcile_pending_attempts(self, *, now: datetime | None = None) -> list[str]:
        """Adopt exact pending admissions after a process restart."""

        recovered: list[str] = []
        async with self.session_provider() as db:
            pending = await self.repository.list_pending_attempts(db, limit=DISPATCH_PASS_LIMIT)
        for attempt in pending:
            async with self.session_provider() as db:
                task = (
                    await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == attempt.task_id))
                ).scalar_one_or_none()
            if task is None:
                continue
            try:
                if _text(task.capability_id) != GOAL_SNAPSHOT_CAPABILITY:
                    admission = await self._lookup_direct_admission(task, attempt)
                    if admission is None:
                        async with self.session_provider() as db:
                            await self.repository.close_proved_absent_attempt(
                                db,
                                task.task_id,
                                attempt.attempt_id,
                                expected_revision=task.task_revision,
                                board_fence=attempt.fencing_token,
                                lease_owner=attempt.lease_owner or self.runner_id,
                                absence_proven=True,
                                actor_principal_id=self.runner_id,
                                actor_session_id=self.runner_session,
                                now=now or self.now(),
                            )
                        recovered.append(f"absent:{attempt.attempt_id}")
                        continue
                    job_id = _text(admission.get("job_id"))
                    if not job_id:
                        raise DurableJobIdempotencyConflict("pending adapter binding has no durable run identity")
                    unresolved = _status(admission) in {"unknown_external_effect", "cost_liability"} or any(
                        isinstance(row, Mapping)
                        and _status(row.get("status")) in {"unknown", "intent", "dispatched"}
                        for row in (admission.get("effects") if isinstance(admission.get("effects"), list) else [])
                    )
                    if unresolved:
                        raise DurableJobError("unknown_effect_requires_reconciliation")
                    async with self.session_provider() as db:
                        await self.repository.link_attempt_workflow_run(
                            db,
                            task.task_id,
                            attempt.attempt_id,
                            workflow_run_id=job_id,
                            expected_revision=task.task_revision,
                            board_fence=attempt.fencing_token,
                            lease_owner=attempt.lease_owner or self.runner_id,
                            workflow_projection=admission,
                            expected_identity=self._direct_expected_identity(task, attempt, admission),
                            actor_principal_id=self.runner_id,
                            actor_session_id=self.runner_session,
                        )
                    recovered.append(job_id)
                    continue
                spec, _inputs, expected_job_id, _owner, _runtime = self._build_spec(
                    task,
                    attempt,
                    runtime_seconds=await self._effective_runtime(task),
                )
                lookup = getattr(self.jobs, "get_by_idempotency_binding", None)
                admission = None
                if lookup is not None:
                    admission = await lookup(
                        owner_principal_id=spec.identity.owner_principal_id,
                        goal_id=spec.goal_id,
                        goal_revision=spec.goal_revision,
                        idempotency_scope=spec.identity.idempotency_scope,
                        idempotency_key=spec.identity.idempotency_key,
                        expected_job_id=expected_job_id,
                        owner_kind=spec.identity.owner_kind,
                        service_id=spec.service_id,
                        session_id=spec.session_id,
                        operator_session_id=spec.operator_session_id,
                        job_kind=spec.identity.job_kind,
                        capability_version=spec.identity.capability_version,
                        input_digest=_safe_digest(spec.inputs),
                        authority_digest=_safe_digest(spec.declared_authority),
                        run_fingerprint=spec.run_fingerprint,
                    )
                else:
                    raise DurableJobError("admission_binding_lookup_unavailable")
                if admission is None:
                    # A successful exact lookup that proves no durable row or
                    # effect exists closes this pre-admission claim.  It must
                    # never silently admit the same task after a restart.
                    async with self.session_provider() as db:
                        await self.repository.close_proved_absent_attempt(
                            db,
                            task.task_id,
                            attempt.attempt_id,
                            expected_revision=task.task_revision,
                            board_fence=attempt.fencing_token,
                            lease_owner=attempt.lease_owner or self.runner_id,
                            absence_proven=True,
                            actor_principal_id=self.runner_id,
                            actor_session_id=self.runner_session,
                            now=now or self.now(),
                        )
                    recovered.append(f"absent:{attempt.attempt_id}")
                    continue
                if _text(admission.get("job_id")) != expected_job_id:
                    raise DurableJobIdempotencyConflict("pending admission identity mismatch")
                # A persisted uncertain/cost-liable run is a recovery stop. It
                # must not be relinked and replayed as a fresh capability.
                effect_rows = admission.get("effects") if isinstance(admission, Mapping) else None
                unresolved = _status(admission) in {
                    "unknown_external_effect",
                    "cost_liability",
                } or any(
                    isinstance(row, Mapping)
                    and _status(row.get("status")) in {"unknown", "intent", "dispatched"}
                    for row in (effect_rows if isinstance(effect_rows, list) else [])
                )
                if unresolved:
                    raise DurableJobError("unknown_effect_requires_reconciliation")
                async with self.session_provider() as db:
                    linked = await self.repository.link_attempt_workflow_run(
                        db,
                        task.task_id,
                        attempt.attempt_id,
                        workflow_run_id=expected_job_id,
                        expected_revision=task.task_revision,
                        board_fence=attempt.fencing_token,
                        lease_owner=attempt.lease_owner or self.runner_id,
                        workflow_projection=admission,
                        expected_identity={
                            "owner_principal_id": spec.identity.owner_principal_id,
                            "owner_kind": spec.identity.owner_kind,
                            "service_id": spec.service_id,
                            "goal_id": spec.goal_id,
                            "goal_revision": spec.goal_revision,
                            "operator_session_id": spec.operator_session_id,
                            "session_id": spec.session_id,
                            "capability_id": spec.identity.job_kind,
                            "capability_version": spec.identity.capability_version,
                            "input_digest": _safe_digest(spec.inputs),
                            "authority_digest": _safe_digest(spec.declared_authority),
                            "run_fingerprint": spec.run_fingerprint,
                            "idempotency_scope": spec.identity.idempotency_scope,
                            "idempotency_key": spec.identity.idempotency_key,
                        },
                        actor_principal_id=self.runner_id,
                        actor_session_id=self.runner_session,
                    )
                recovered.append(expected_job_id)
                # Execution is deliberately left to the normal pass after the
                # immutable link is restored; this avoids replaying an effect
                # while the admission status is still being inspected.
                _ = linked
            except Exception as exc:
                try:
                    claim = BoardDispatchClaim(task, attempt, None)  # type: ignore[arg-type]
                    await self._project_blocked(claim, "unknown_effect", type(exc).__name__)
                except Exception:
                    logger.exception("pending board attempt %s needs manual recovery", attempt.attempt_id)
        return recovered

    async def _lookup_direct_admission(
        self,
        task: WorkBoardTask,
        attempt: WorkBoardAttempt,
    ) -> Mapping[str, Any] | None:
        lookup = getattr(self.jobs, "get_by_idempotency_binding", None)
        if lookup is None:
            raise DurableJobError("admission_binding_lookup_unavailable")
        owner = "service:guardian-source-watch" if _text(task.capability_id) == "guardian.research-watch.v1" else task.owner_principal_id
        return await lookup(
            owner_principal_id=owner,
            goal_id=task.goal_id,
            goal_revision=task.goal_revision,
            idempotency_scope="work-board-attempt",
            idempotency_key=f"{task.task_id}:{attempt.attempt_id}",
        )


_dispatcher = WorkBoardDispatcher()


async def run_work_board_dispatch() -> dict[str, Any]:
    """Managed scheduler entry point; never called by user cron execution."""

    return await _dispatcher.run_pass()


__all__ = [
    "CapabilitySpec",
    "DEFAULT_RUNTIME_SECONDS",
    "DISPATCH_PASS_LIMIT",
    "REGISTERED_CAPABILITIES",
    "WorkBoardDispatcher",
    "run_work_board_dispatch",
]
