"""Bounded reviewed procedure preferences; these records never grant execution.

Canonical inventory/feedback helpers are deliberately DB-only. Physical native
proof, redaction and signing material are staged separately before any writer.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
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
            "supersedes_event_id": expected, "label": metadata["label"], "metadata_digest": digest(metadata)})
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
        def job_token(row):
            fields = _fields(row, ("run_identity", "revision", "fencing_token", "status", "job_kind", "input_digest", "authority_digest", "parent_job_id", "parent_fencing_token", "result_digest"))
            if fields is not None:
                for key in ("effect_receipts_json", "artifact_receipts_json", "checkpoint_receipts_json", "declared_authority_json", "arguments_json"):
                    value = bounded_json(getattr(row, key), [] if "receipts" in key else {})
                    fields[key.removesuffix("_json") + "_digest"] = digest(value)
            return fields
        chain = feedback[task.task_id]
        members.append({"task": _fields(task, ("task_id", "task_revision", "status", "capability_id", "typed_input_digest", "input_artifact_id", "idempotency_payload_digest")),
            "input": _fields(artifact, ("artifact_id", "revision", "metadata_digest", "payload_sha256", "state", "bound_task_id", "bound_task_revision")),
            "attempt": _fields(attempt, ("attempt_id", "fencing_token", "outcome", "workflow_run_id", "ended_at")),
            "parent": job_token(parent), "leaf": job_token(child), "expected_leaf_id": child_id,
            "feedback_count": len(chain), "feedback_tip": chain[-1] if chain else None, "feedback_digest": digest(chain)})
    token = {"schema": MEMBERSHIP_SCHEMA, "scope": asdict(scope), "task_count": len(ids), "task_ids": ids,
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
