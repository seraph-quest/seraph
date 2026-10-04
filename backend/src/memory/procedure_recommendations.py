"""Bounded reviewed procedure preferences; these records never grant execution.

Canonical inventory/feedback helpers are deliberately DB-only. Physical native
proof, redaction and signing material are staged separately before any writer.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from src.auth.service import AuthenticatedOperator
from src.db.models import (
    Goal, GuardianRoutine, GuardianRoutineVersion, OperatorSession,
    WorkBoardAttempt, WorkBoardEvent, WorkBoardInputArtifact, WorkBoardTask,
    WorkflowRunState,
)
from src.work_board.repository import BoardError, _begin_sqlite_immediate
from src.workflows.procedure_contracts import plan_digest, validate_procedure_plan
from src.workflows.procedure_v2_runtime import deterministic_child_job_id

PROPOSAL_SCHEMA = "procedure_recommendation.v1"
SCOPE_SCHEMA = "procedure_preference.v1"
MEMBERSHIP_SCHEMA = "procedure_membership.v1"
FEEDBACK_KIND = "procedure.outcome_feedback.v1"
MAX_TASKS = 20
MAX_FEEDBACK = 100
MAX_METADATA_BYTES = 128 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_PROOF_BYTES = 16 * 1024 * 1024
MANUAL_DISCLOSURE = "Only matching manual invocations are counted. Governed scheduled invocations are excluded."
QUALITY_DISCLOSURE = "This deterministic preference is not a measured quality improvement."


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def bounded_json(raw: str | None, fallback: Any = None) -> Any:
    if raw is None:
        return fallback
    if len(raw.encode()) > MAX_METADATA_BYTES:
        raise BoardError("procedure_metadata_limit", "Procedure metadata exceeds its finite bound")
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise BoardError("procedure_metadata_invalid", "Canonical procedure metadata is malformed") from exc


def _value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    return getattr(value, "value", value)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _fields(row: Any, names: tuple[str, ...]) -> dict[str, Any] | None:
    return {key: _value(getattr(row, key)) for key in names} if row is not None else None


class ProcedureFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: int = Field(ge=1)
    expected_routine_revision: int = Field(ge=1)
    goal_id: str = Field(min_length=1, max_length=128)
    expected_goal_revision: int = Field(ge=1)
    expected_task_revision: int = Field(ge=1)
    expected_attempt_id: str | None = Field(default=None, min_length=1, max_length=256)
    expected_attempt_fence: int | None = Field(default=None, ge=0)
    label: Literal["helpful", "harmful"]
    supersedes_event_id: int | None = Field(default=None, ge=1)
    reason: str = Field(default="", max_length=500)
    mutation_uuid: str = Field(min_length=36, max_length=36)

    @field_validator("mutation_uuid")
    @classmethod
    def uuid_exact(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("mutation_uuid must be canonical")
        return value


@dataclass(frozen=True)
class ProcedureScope:
    owner_principal_id: str
    owner_session_id: str
    goal_id: str
    goal_revision: int
    routine_id: str
    routine_revision: int
    version_id: str
    version: int
    plan_digest: str
    package_digest: str
    copied_input_digest: str
    template_id: str = "public-browser-check"

    @property
    def invocation_scope(self) -> str:
        return f"procedure-v2:{self.routine_id}:{self.version_id}"


async def assert_current_root(db, operator: AuthenticatedOperator, *, now: datetime | None = None) -> OperatorSession:
    """Pure canonical recheck of a previously authenticated HTTP context."""
    now = _utc(now or datetime.now(timezone.utc))
    row = await db.get(OperatorSession, operator.session_id, populate_existing=True)
    if (row is None or not operator._token_hash
        or row.principal_id != operator.principal.principal_id
        or row.token_hash != operator._token_hash or row.revoked_at is not None
        or row.replaced_by_id is not None or row.is_bearer_tombstone
        or _utc(row.idle_expires_at) <= now or _utc(row.absolute_expires_at) <= now):
        raise BoardError("procedure_root_stale", "The original authenticated Root is no longer current")
    return row


async def resolve_scope(db, operator: AuthenticatedOperator, *, routine_id: str, version: int,
                        routine_revision: int, goal_id: str, goal_revision: int) -> ProcedureScope:
    """Canonical DB identity only; active package file proof is staged later."""
    await assert_current_root(db, operator)
    routine = await db.get(GuardianRoutine, routine_id, populate_existing=True)
    goal = await db.get(Goal, goal_id, populate_existing=True)
    owner = (operator.principal.principal_id, operator.session_id)
    if (routine is None or (routine.owner_principal_id, routine.owner_session_id) != owner
        or routine.state != "active" or routine.current_version != version
        or routine.revision != routine_revision):
        raise BoardError("procedure_version_stale", "The reviewed active procedure version changed")
    if (goal is None or (goal.owner_principal_id, goal.owner_session_id) != owner
        or _value(goal.status) != "active" or goal.revision != goal_revision):
        raise BoardError("procedure_goal_stale", "The current Goal changed")
    row = (await db.execute(select(GuardianRoutineVersion).where(
        GuardianRoutineVersion.routine_id == routine_id, GuardianRoutineVersion.version == version
    ))).scalar_one_or_none()
    if row is None or not row.installed_package_digest:
        raise BoardError("procedure_version_unreviewed", "A reviewed installed procedure version is required")
    provenance = bounded_json(row.source_provenance_json, {})
    try:
        plan = validate_procedure_plan(provenance.get("plan"))
    except ValueError as exc:
        raise BoardError("procedure_version_invalid", "The reviewed procedure plan is invalid") from exc
    if (provenance.get("schema_version") != 2 or plan.template_id != "public-browser-check"
        or plan_digest(plan) != provenance.get("plan_digest")):
        raise BoardError("procedure_template_unsupported", "Only the reviewed public-browser-check version supports preferences")
    from src.workflows.procedure_service import _validated_immutable_step_inputs, _plan_step_input_digests
    from src.workflows.procedure_contracts import get_procedure_template
    inputs = _validated_immutable_step_inputs(provenance.get("immutable_step_inputs"),
        get_procedure_template("public-browser-check"), expected_step_input_digests=_plan_step_input_digests(plan))
    return ProcedureScope(*owner, goal_id, goal_revision, routine_id, routine_revision,
        row.id, version, plan_digest(plan), row.installed_package_digest, digest(inputs))


async def canonical_procedure_membership(db, scope: ProcedureScope) -> dict[str, Any]:
    """Complete bounded inventory: never status-filter before detecting overflow."""
    tasks = list((await db.execute(select(WorkBoardTask).where(
        WorkBoardTask.owner_principal_id == scope.owner_principal_id,
        WorkBoardTask.owner_session_id == scope.owner_session_id,
        WorkBoardTask.goal_id == scope.goal_id,
        WorkBoardTask.goal_revision == scope.goal_revision,
        WorkBoardTask.capability_id == "guardian-routine.v2",
        WorkBoardTask.idempotency_scope == scope.invocation_scope,
    ).order_by(WorkBoardTask.task_id).limit(MAX_TASKS + 1))).scalars().all())
    if len(tasks) > MAX_TASKS:
        raise BoardError("procedure_outcome_limit", "More than twenty matching manual invocations; no preference can be learned")
    ids = [row.task_id for row in tasks]
    events = list((await db.execute(select(WorkBoardEvent).where(
        WorkBoardEvent.owner_principal_id == scope.owner_principal_id,
        WorkBoardEvent.owner_session_id == scope.owner_session_id,
        WorkBoardEvent.task_id.in_(ids), WorkBoardEvent.kind == FEEDBACK_KIND,
    ).order_by(WorkBoardEvent.event_id).limit(MAX_FEEDBACK + 1))).scalars().all()) if ids else []
    if len(events) > MAX_FEEDBACK:
        raise BoardError("procedure_feedback_limit", "Feedback history exceeds its finite bound; no preference can be learned")
    feedback: dict[str, list[dict[str, Any]]] = {task_id: [] for task_id in ids}
    for event in events:
        metadata = bounded_json(event.metadata_json, {})
        chain = feedback[event.task_id]
        expected = chain[-1]["event_id"] if chain else None
        if (metadata.get("schema") != FEEDBACK_KIND or metadata.get("scope_digest") != digest(asdict(scope))
            or metadata.get("supersedes_event_id") != expected or metadata.get("label") not in {"helpful", "harmful"}
            or metadata.get("task_id") != event.task_id or not event.mutation_request_digest):
            raise BoardError("procedure_feedback_invalid", "The canonical feedback chain no longer matches this exact procedure")
        chain.append({"event_id": event.event_id, "request_digest": event.mutation_request_digest,
            "supersedes_event_id": expected, "label": metadata["label"], "metadata_digest": digest(metadata),
            "task_revision": metadata.get("task_revision"), "attempt_id": metadata.get("attempt_id"),
            "attempt_fence": metadata.get("attempt_fence")})
    def job_token(row):
        fields = _fields(row, ("run_identity", "revision", "fencing_token", "status", "job_kind", "input_digest", "authority_digest", "parent_job_id", "parent_fencing_token", "result_digest"))
        if fields is not None:
            for key in ("effect_receipts_json", "artifact_receipts_json", "checkpoint_receipts_json", "declared_authority_json", "arguments_json"):
                value = bounded_json(getattr(row, key), [] if "receipts" in key else {})
                fields[key.removesuffix("_json") + "_digest"] = digest(value)
        return fields
    members = []
    for task in tasks:
        artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id, populate_existing=True) if task.input_artifact_id else None
        if (artifact is None or artifact.bound_task_id != task.task_id
            or (artifact.owner_principal_id, artifact.owner_session_id) != (scope.owner_principal_id, scope.owner_session_id)
            or (artifact.goal_id, artifact.goal_revision, artifact.capability_id) != (scope.goal_id, scope.goal_revision, "guardian-routine.v2")
            or artifact.payload_sha256 != task.typed_input_digest):
            raise BoardError("procedure_input_binding_invalid", "A matching invocation has missing or changed canonical input")
        attempt = (await db.execute(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task.task_id)
            .order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc()).limit(1))).scalar_one_or_none()
        parent = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id))).scalar_one_or_none() if attempt and attempt.workflow_run_id else None
        child_id = deterministic_child_job_id(parent.run_identity, scope.template_id, scope.version, "public_browser_check") if parent else None
        child = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == child_id))).scalar_one_or_none() if child_id else None
        leaf_authority = bounded_json(child.declared_authority_json, {}) if child else {}
        leaf_input = await db.get(WorkBoardInputArtifact, leaf_authority.get("input_artifact_id"), populate_existing=True) if child and leaf_authority.get("input_artifact_id") else None
        # Browser authority binds its canonical input, whose row binds the
        # Board task. Unlike the procedure parent, it has no board_task_id or
        # board_attempt_id authority fields; resolve its actual latest attempt.
        leaf_task = (await db.execute(select(WorkBoardTask).where(
            WorkBoardTask.task_id == leaf_input.bound_task_id))).scalar_one_or_none() if leaf_input and leaf_input.bound_task_id else None
        leaf_attempt = (await db.execute(select(WorkBoardAttempt).where(
            WorkBoardAttempt.task_id == leaf_task.task_id).order_by(
            WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc()).limit(1))).scalar_one_or_none() if leaf_task else None
        chain = feedback[task.task_id]
        tip = chain[-1] if chain else None
        feedback_allowed = bool(attempt and attempt.ended_at and attempt.outcome
            and _value(task.status) in {"done", "blocked", "archived"})
        tip_current = bool(tip and feedback_allowed and tip["task_revision"] == task.task_revision
            and tip["attempt_id"] == attempt.attempt_id and tip["attempt_fence"] == attempt.fencing_token)
        members.append({"task": _fields(task, ("task_id", "task_revision", "status", "capability_id", "typed_input_digest", "input_artifact_id", "idempotency_payload_digest")),
            "input": _fields(artifact, ("artifact_id", "revision", "metadata_digest", "payload_sha256", "state", "bound_task_id", "bound_task_revision")),
            "attempt": _fields(attempt, ("attempt_id", "fencing_token", "outcome", "workflow_run_id", "ended_at")),
            "parent": job_token(parent), "leaf": job_token(child), "expected_leaf_id": child_id,
            "leaf_task": _fields(leaf_task, ("task_id", "task_revision", "status", "owner_principal_id", "owner_session_id", "goal_id", "goal_revision", "capability_id", "input_artifact_id", "typed_input_digest")),
            "leaf_attempt": _fields(leaf_attempt, ("attempt_id", "task_id", "fencing_token", "outcome", "workflow_run_id", "ended_at", "task_revision_at_claim")),
            "leaf_input": _fields(leaf_input, ("artifact_id", "revision", "metadata_digest", "payload_sha256", "state", "bound_task_id", "bound_task_revision")),
            "feedback_count": len(chain), "feedback_tip": tip, "effective_feedback_tip": tip if tip_current else None,
            "feedback_allowed": feedback_allowed, "feedback_digest": digest(chain)})
    version_row = await db.get(GuardianRoutineVersion, scope.version_id, populate_existing=True)
    provenance = bounded_json(version_row.source_provenance_json, {}) if version_row else {}
    source_refs = provenance.get("source_refs")
    if not isinstance(source_refs, list) or len(source_refs) != 1:
        raise BoardError("procedure_source_binding_invalid", "The fixed version requires its exact original browser source")
    original_sources = []
    for ref in source_refs:
        source_task = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == ref.get("task_id")))).scalar_one_or_none()
        source_attempt = (await db.execute(select(WorkBoardAttempt).where(
            WorkBoardAttempt.task_id == ref.get("task_id")).order_by(
            WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc()).limit(1))).scalar_one_or_none()
        source_run = (await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == source_attempt.workflow_run_id))).scalar_one_or_none() if source_attempt else None
        original_sources.append({"task": _fields(source_task, ("task_id", "task_revision", "status", "owner_principal_id", "owner_session_id", "goal_id", "goal_revision", "capability_id", "input_artifact_id", "typed_input_digest")),
            "attempt": _fields(source_attempt, ("attempt_id", "fencing_token", "outcome", "workflow_run_id", "ended_at")), "run": job_token(source_run)})
    token = {"schema": MEMBERSHIP_SCHEMA, "scope": asdict(scope), "task_count": len(ids), "task_ids": ids,
        "version_provenance_digest": digest(provenance), "original_sources": original_sources,
        "members": members, "feedback_count": len(events), "feedback_digest": digest(feedback)}
    if len(canonical(token).encode()) > MAX_METADATA_BYTES:
        raise BoardError("procedure_metadata_limit", "The complete inventory exceeds its finite metadata bound")
    token["membership_digest"] = digest(token)
    return token


async def assert_membership_unchanged(db, scope: ProcedureScope, expected: dict[str, Any]) -> dict[str, Any]:
    current = await canonical_procedure_membership(db, scope)
    if current != expected:
        raise BoardError("procedure_membership_changed", "Matching invocation outcomes or feedback changed; review a fresh complete set")
    return current


async def record_procedure_feedback(db, operator: AuthenticatedOperator, *, routine_id: str,
                                    task_id: str, request: ProcedureFeedbackRequest) -> dict[str, Any]:
    """Append one explicit feedback decision/correction in the caller's writer."""
    await _begin_sqlite_immediate(db)
    scope = await resolve_scope(db, operator, routine_id=routine_id, version=request.version,
        routine_revision=request.expected_routine_revision, goal_id=request.goal_id,
        goal_revision=request.expected_goal_revision)
    request_digest = digest({"routine_id": routine_id, "task_id": task_id, "request": request.model_dump(mode="json"), "scope": asdict(scope)})
    replay = (await db.execute(select(WorkBoardEvent).where(
        WorkBoardEvent.owner_principal_id == scope.owner_principal_id,
        WorkBoardEvent.owner_session_id == scope.owner_session_id,
        WorkBoardEvent.mutation_idempotency_key == request.mutation_uuid))).scalar_one_or_none()
    if replay is not None:
        if replay.kind != FEEDBACK_KIND or replay.task_id != task_id or replay.mutation_request_digest != request_digest:
            raise BoardError("procedure_feedback_conflict", "The feedback key belongs to a different exact request")
        return {"event_id": replay.event_id, "label": request.label, "idempotent_replay": True}
    token = await canonical_procedure_membership(db, scope)
    member = next((row for row in token["members"] if row["task"]["task_id"] == task_id), None)
    if member is None or member["task"]["task_revision"] != request.expected_task_revision:
        raise BoardError("procedure_task_stale", "The exact matching invocation changed")
    attempt = member["attempt"]
    if not member["feedback_allowed"]:
        raise BoardError("procedure_feedback_outcome_pending", "Feedback requires the current ended invocation attempt and outcome")
    if ((attempt["attempt_id"] if attempt else None) != request.expected_attempt_id
        or (attempt["fencing_token"] if attempt else None) != request.expected_attempt_fence):
        raise BoardError("procedure_attempt_stale", "The latest invocation attempt changed")
    tip = member["feedback_tip"]
    if (tip["event_id"] if tip else None) != request.supersedes_event_id:
        raise BoardError("procedure_feedback_stale", "Correction must name the current feedback decision")
    if token["feedback_count"] >= MAX_FEEDBACK:
        raise BoardError("procedure_feedback_limit", "The feedback history is full; prior decisions remain intact")
    if request.supersedes_event_id is not None and not request.reason.strip():
        raise BoardError("procedure_feedback_reason_required", "An explicit reason is required to correct feedback")
    event = WorkBoardEvent(task_id=task_id, owner_principal_id=scope.owner_principal_id,
        owner_session_id=scope.owner_session_id, actor_principal_id=scope.owner_principal_id,
        actor_session_id=scope.owner_session_id, kind=FEEDBACK_KIND,
        mutation_idempotency_key=request.mutation_uuid, mutation_request_digest=request_digest,
        metadata_json=canonical({"schema": FEEDBACK_KIND, "scope_digest": digest(asdict(scope)),
            "task_id": task_id, "task_revision": request.expected_task_revision,
            "attempt_id": request.expected_attempt_id, "attempt_fence": request.expected_attempt_fence,
            "label": request.label, "supersedes_event_id": request.supersedes_event_id,
            "reason_digest": digest(request.reason)}))
    db.add(event)
    await db.flush()
    return {"event_id": event.event_id, "label": request.label, "idempotent_replay": False}


def read_private_proof(relative: str, expected_digest: str) -> bytes:
    """Bounded held nofollow path traversal beneath the canonical workspace."""
    from config.settings import settings
    from src.workspace import canonical_workspace_root
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise BoardError("procedure_proof_path_invalid", "The native proof path is outside the workspace")
    root = canonical_workspace_root(settings.workspace_dir)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = None
    try:
        root_info = os.fstat(directory)
        if root_info.st_uid != os.getuid() or root_info.st_mode & 0o077:
            raise BoardError("procedure_proof_file_invalid", "The canonical proof workspace must be private")
        for part in parts[:-1]:
            next_directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_directory
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or not 0 < info.st_size <= MAX_FILE_BYTES):
            raise BoardError("procedure_proof_file_invalid", "Native proof requires a private finite regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(info.st_size + 1)
        after = os.fstat(descriptor)
        if (len(raw) != info.st_size or (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            != (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            or hashlib.sha256(raw).hexdigest() != expected_digest):
            raise BoardError("procedure_proof_changed", "Native proof bytes changed during staging")
        return raw
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


_BUNDLE_SEAL = object()


@dataclass(frozen=True)
class PreparedProcedureBundle:
    """Internal proof stage; no public JSON or caller boolean can mint it."""
    seal: object
    scope: ProcedureScope
    membership_json: str
    outcomes_json: str
    files_json: str
    bundle_digest: str

    def projection(self) -> dict[str, Any]:
        outcomes = bounded_json(self.outcomes_json)
        helpful = sum(item["verified"] and item["feedback"] == "helpful" for item in outcomes)
        harmful = sum(item["feedback"] == "harmful" for item in outcomes)
        pending = any(item["unresolved"] for item in outcomes)
        stale_feedback = any(item.get("feedback_history_label") and not item.get("feedback_current") for item in outcomes)
        reason = ("procedure_outcome_unresolved" if pending else "feedback_outcome_stale" if stale_feedback else "harmful_feedback" if harmful
            else "insufficient_helpful_outcomes" if helpful < 2 else "reviewed_outcomes_support_preference")
        return {"schema": PROPOSAL_SCHEMA, "status": "blocked" if pending else "proposed" if helpful >= 2 and not harmful and not stale_feedback else "no_learning",
            "reason_code": reason, "scope": asdict(self.scope), "outcomes": outcomes,
            "included_count": len(outcomes), "helpful_count": helpful, "harmful_count": harmful,
            "membership_digest": bounded_json(self.membership_json)["membership_digest"],
            "bundle_digest": self.bundle_digest, "evidence_population": "matching_manual_invocations_only",
            "quality_evidence": "unmeasured", "manual_disclosure": MANUAL_DISCLOSURE,
            "quality_disclosure": QUALITY_DISCLOSURE, "memory_status": "no_learning"}


async def stage_procedure_bundle(operator: AuthenticatedOperator, *, routine_id: str, version: int,
                                 routine_revision: int, goal_id: str, goal_revision: int) -> PreparedProcedureBundle:
    """Read real native parent/leaf proof OUTSIDE any SQLite writer."""
    from src.db import engine as db_engine
    from src.workflows.routines import routine_service
    from src.workflows.job_runtime import _effect_is_unresolved, _serialize
    from src.browser.task_runner import BrowserTaskRunner
    from src.work_board.input_artifacts import _payload_path, _decode_and_validate_payload, _metadata_digest
    async with db_engine.get_session() as db:
        scope = await resolve_scope(db, operator, routine_id=routine_id, version=version,
            routine_revision=routine_revision, goal_id=goal_id, goal_revision=goal_revision)
        initial = await canonical_procedure_membership(db, scope)
    # This service stages current package and original version source proof.
    # Its native/file/session work must never run in the final canonical writer.
    descriptor = await routine_service._procedure_v2().resolve_v2_version(routine_id, version,
        owner_principal_id=scope.owner_principal_id, owner_session_id=scope.owner_session_id)
    if (descriptor.version_id != scope.version_id or descriptor.routine_revision != scope.routine_revision
        or descriptor.plan_digest != scope.plan_digest or descriptor.installed_package_digest != scope.package_digest):
        raise BoardError("procedure_version_stale", "The reviewed procedure changed during proof staging")
    files: dict[str, dict[str, Any]] = {}
    outcomes = []
    def read(relative, expected):
        raw = read_private_proof(relative, expected)
        prior = files.get(relative)
        item = {"path": relative, "sha256": expected, "bytes": len(raw)}
        if prior is not None and prior != item:
            raise BoardError("procedure_proof_changed", "One native proof path names conflicting content")
        files[relative] = item
        if sum(item["bytes"] for item in files.values()) > MAX_PROOF_BYTES:
            raise BoardError("procedure_proof_limit", "Native proof exceeds the aggregate finite limit")
        return raw
    # Count the original version source output in the same finite file budget.
    async with db_engine.get_session() as db:
        for source in descriptor.source_refs:
            source_run = (await db.execute(select(WorkflowRunState).where(
                WorkflowRunState.run_identity == source["job_id"]))).scalar_one()
            for artifact in bounded_json(source_run.artifact_receipts_json, []):
                if artifact.get("artifact_type") == "browser_public_task_result":
                    read(artifact["file_path"], artifact["content_sha256"])
    async with db_engine.get_session() as db:
        for member in initial["members"]:
            task_id = member["task"]["task_id"]
            task = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))).scalar_one()
            artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id)
            from config.settings import settings
            from src.workspace import canonical_workspace_root
            relative = str(_payload_path(artifact).relative_to(canonical_workspace_root(settings.workspace_dir)))
            raw_input = read(relative, artifact.payload_sha256)
            if _metadata_digest(artifact) != artifact.metadata_digest:
                raise BoardError("procedure_input_binding_invalid", "The invocation metadata changed")
            payload = _decode_and_validate_payload(artifact, raw_input)
            if (payload.get("routine_id") != scope.routine_id or payload.get("version") != scope.version
                or payload.get("expected_routine_revision") != scope.routine_revision
                or payload.get("goal_id") != scope.goal_id or payload.get("expected_goal_revision") != scope.goal_revision
                or payload.get("parameters") != {"goal_id": scope.goal_id, "expected_goal_revision": scope.goal_revision}):
                raise BoardError("procedure_input_binding_invalid", "A matching invocation has different reviewed version parameters")
            attempt = await db.get(WorkBoardAttempt, member["attempt"]["attempt_id"]) if member["attempt"] else None
            parent = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id))).scalar_one_or_none() if attempt and attempt.workflow_run_id else None
            leaf = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == member["expected_leaf_id"]))).scalar_one_or_none() if member["expected_leaf_id"] else None
            unresolved = _value(task.status) in {"running", "ready", "todo", "triage", "review"}
            for run in (parent, leaf):
                if run and (run.status in {"running", "accepted", "queued", "unknown_external_effect", "cost_liability"}
                    or any(_effect_is_unresolved(e) for e in bounded_json(run.effect_receipts_json, []))):
                    unresolved = True
            verified = False
            proof = None
            if _value(task.status) == "done" and attempt and attempt.ended_at and attempt.outcome == "verified":
                if parent is None or leaf is None:
                    raise BoardError("procedure_native_proof_missing", "A completed invocation is missing its exact native parent or leaf")
                authority = bounded_json(parent.declared_authority_json, {})
                expected_authority = {"principal": scope.owner_principal_id, "session_id": scope.owner_session_id,
                    "operator_session_id": scope.owner_session_id, "owner_kind": "user", "routine_id": scope.routine_id,
                    "routine_version": scope.version, "routine_revision": scope.routine_revision, "package_digest": scope.package_digest,
                    "template_id": scope.template_id, "plan_digest": scope.plan_digest, "goal_id": scope.goal_id,
                    "goal_revision": scope.goal_revision, "board_task_id": task.task_id, "board_attempt_id": attempt.attempt_id,
                    "board_fencing_token": attempt.fencing_token, "input_artifact_id": artifact.artifact_id,
                    "input_artifact_digest": task.typed_input_digest, "capability_id": "guardian-routine.v2"}
                if (parent.status != "succeeded" or parent.job_kind != "guardian_routine_v2"
                    or parent.owner_principal_id != scope.owner_principal_id or parent.operator_session_id != scope.owner_session_id
                    or any(authority.get(k) != v for k, v in expected_authority.items())):
                    raise BoardError("procedure_parent_binding_invalid", "The native parent does not match this exact reviewed invocation")
                parent_effects = bounded_json(parent.effect_receipts_json, [])
                matching = [e for e in parent_effects if isinstance(e, dict)
                    and e.get("effect_type") == "guardian_routine_v2_parent" and e.get("receipt_kind") == "readback"
                    and e.get("effect_id") == f"procedure-v2-parent:{parent.run_identity}"
                    and e.get("readback_id") == f"procedure-v2-parent-readback:{parent.run_identity}"
                    and e.get("target_path") == f"procedure-v2:{parent.run_identity}" and e.get("status") == "succeeded"
                    and isinstance(e.get("details"), dict) and e["details"].get("verified") is True
                    and e.get("fencing_token") == parent.fencing_token and e.get("target_digest")]
                if len(matching) != 1:
                    raise BoardError("procedure_parent_readback_missing", "The exact parent has no unique positive native readback")
                leaf_authority = bounded_json(leaf.declared_authority_json, {})
                child_task = member["leaf_task"]
                child_attempt = member["leaf_attempt"]
                child_input = member["leaf_input"]
                if (leaf.status != "succeeded" or leaf.parent_job_id != parent.run_identity
                    or leaf.parent_fencing_token != parent.fencing_token
                    or leaf_authority.get("routine_parent_job_id") != parent.run_identity
                    or leaf_authority.get("routine_parent_fencing_token") != parent.fencing_token
                    or leaf_authority.get("routine_step_id") != "public_browser_check"
                    or child_task is None or child_attempt is None or child_input is None
                    or child_task["status"] != "done" or child_attempt["outcome"] != "verified"
                    or child_attempt["ended_at"] is None or child_attempt["workflow_run_id"] != leaf.run_identity
                    or child_attempt["task_id"] != child_task["task_id"]
                    or child_task["capability_id"] != "browser.public-task.v1"
                    or (child_task["owner_principal_id"], child_task["owner_session_id"], child_task["goal_id"], child_task["goal_revision"])
                    != (scope.owner_principal_id, scope.owner_session_id, scope.goal_id, scope.goal_revision)
                    or child_input["bound_task_id"] != child_task["task_id"]
                    or child_input["payload_sha256"] != child_task["typed_input_digest"]
                    or leaf_authority.get("board_fencing_token") != child_attempt["fencing_token"]
                    or leaf_authority.get("input_artifact_digest") != child_input["payload_sha256"]):
                    raise BoardError("procedure_leaf_binding_invalid", "The fixed native browser leaf has changed canonical Board lineage")
                projection = _serialize(leaf)
                for artifact_receipt in projection["artifacts"]:
                    if artifact_receipt.get("artifact_type") == "browser_public_task_result" and artifact_receipt.get("exists") is True:
                        read(artifact_receipt["file_path"], artifact_receipt["content_sha256"])
                proof = BrowserTaskRunner(workspace_root=settings.workspace_dir)._terminal_replay_proof(
                    projection, expected_job_id=member["expected_leaf_id"], workspace_root=settings.workspace_dir)
                if proof is None:
                    raise BoardError("procedure_leaf_readback_missing", "Native browser output/readback/cleanup proof is unavailable")
                verified = True
            tip = member["effective_feedback_tip"]
            history_tip = member["feedback_tip"]
            outcomes.append({"task_id": task_id, "task_revision": task.task_revision, "status": _value(task.status),
                "attempt_id": attempt.attempt_id if attempt else None, "verified": verified, "unresolved": unresolved,
                "feedback": tip["label"] if tip else None, "feedback_event_id": history_tip["event_id"] if history_tip else None,
                "feedback_current": tip is not None, "feedback_history_label": history_tip["label"] if history_tip else None,
                "feedback_history_count": member["feedback_count"], "feedback_allowed": member["feedback_allowed"],
                "readback_id": proof.get("readback_id") if proof else None,
                "artifact_digest": proof.get("artifact_sha256") if proof else None,
                "reason_code": "feedback_outcome_stale" if history_tip and not tip else "native_verified" if verified else "outcome_not_verified"})
    async with db_engine.get_session() as db:
        current_scope = await resolve_scope(db, operator, routine_id=routine_id, version=version,
            routine_revision=routine_revision, goal_id=goal_id, goal_revision=goal_revision)
        if current_scope != scope:
            raise BoardError("procedure_version_stale", "The reviewed version changed during physical staging")
        await assert_membership_unchanged(db, scope, initial)
    body = {"membership": initial, "outcomes": outcomes, "files": sorted(files.values(), key=lambda item: item["path"])}
    if len(canonical(body).encode()) > MAX_METADATA_BYTES:
        raise BoardError("procedure_metadata_limit", "The complete recommendation bundle exceeds its finite metadata limit")
    return PreparedProcedureBundle(_BUNDLE_SEAL, scope, canonical(initial), canonical(outcomes),
        canonical(body["files"]), digest(body))
