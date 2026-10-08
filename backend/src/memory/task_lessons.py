"""Private, evidence-bound task lessons. Candidates are data, never authority."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import hashlib
import json
from typing import Annotated, Literal
import re
from uuid import UUID
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from config.settings import settings
from src.db import engine as db_engine
from src.db.models import Goal, MemoryProposal, MemoryProposalStatus, WorkBoardAttempt, WorkBoardTask, WorkflowRunState, WorkflowStepState, WorkBoardEvent, OperatorSession
from src.memory.procedure_recommendations import assert_current_root, canonical, digest, read_private_proof
from src.work_board.repository import BoardError, _begin_sqlite_immediate

PROPOSAL_SCHEMA = "task_method_proposal.v1"
_STAGE_SEAL = object()


@dataclass(frozen=True)
class _SourceStage:
    seal: object
    token: dict
    method: TaskMethod | None
    method_token: dict | None
BoundedText = Annotated[str, Field(min_length=1, max_length=1000)]
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]


async def _task(db, task_id):
    # Public task_id is unique; the SQLite ordering sequence is the primary key.
    return (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id)
        .execution_options(populate_existing=True))).scalar_one_or_none()


async def _assert_owner(db, operator, *, automatic=False):
    if not automatic:
        return await assert_current_root(db, operator)
    # A canonical opt-in event authorizes the terminal owner callback, not a
    # new bearer token, renewed Root, service principal or trace-egress grant.
    row = await db.get(OperatorSession, operator.session_id, populate_existing=True)
    utc = lambda value: value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    if (row is None or row.principal_id != operator.principal.principal_id
        or not operator.principal.authenticated or operator.ownership_continuity != "stable"
        or getattr(operator.principal.principal_type, "value", operator.principal.principal_type) != "operator"
        or row.revoked_at is not None or row.replaced_by_id is not None or row.is_bearer_tombstone
        or utc(row.idle_expires_at) <= now or utc(row.absolute_expires_at) <= now):
        raise BoardError("lesson_root_stale", "The original automatic-proposal owner is no longer current")
    return row


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ResearchStrategy(ClosedModel):
    schema_version: Literal["ResearchStrategy.v1"] = "ResearchStrategy.v1"
    query_templates: list[BoundedText] = Field(max_length=3)
    source_preferences: list[Literal["primary", "official", "peer_reviewed", "dated", "independent"]] = Field(max_length=5)
    required_evidence_fields: list[Literal["url", "title", "date", "excerpt", "claim", "limitation"]] = Field(max_length=6)
    draft_sections: list[BoundedText] = Field(max_length=16)
    stop_conditions: list[BoundedText] = Field(min_length=1, max_length=8)


class MethodOutput(ClosedModel):
    artifact_type: Identifier
    required_fields: list[Identifier] = Field(min_length=1, max_length=16)


class ToolStep(ClosedModel):
    kind: Literal["registered_tool"] = "registered_tool"
    tool_id: Identifier


class GuardStep(ClosedModel):
    kind: Literal["guard"] = "guard"
    check: Literal["source_exists", "verified_readback", "preserve_source_attribution"]


class CapabilityStep(ClosedModel):
    """Snapshot of one current native interface; never a generic invoke string."""
    kind: Literal["registered_capability"] = "registered_capability"
    capability_id: Literal["work.json-format.v1"]
    capability_version: Literal["1"]
    typed_input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


MethodStep = Annotated[ToolStep | GuardStep | CapabilityStep, Field(discriminator="kind")]


class TaskMethod(ClosedModel):
    schema_version: Literal["TaskMethod.v1"] = "TaskMethod.v1"
    family: Literal["research", "software", "knowledge", "general"]
    steps: list[MethodStep] = Field(min_length=1, max_length=16)
    registered_tool_ids: list[Identifier] = Field(max_length=16)
    input_parameters: dict[Identifier, str | int | bool | None] = Field(max_length=16)
    output_contract: MethodOutput

    @field_validator("registered_tool_ids")
    @classmethod
    def registered(cls, values):
        from src.native_tools.registry import TOOL_METADATA
        if len(values) != len(set(values)) or any(value not in TOOL_METADATA for value in values):
            raise ValueError("Only existing registered tools may be referenced")
        return values

    @field_validator("input_parameters")
    @classmethod
    def parameters(cls, values):
        forbidden = {"code", "command", "script", "permissions", "provider", "model", "runtime_limits", "credentials", "secret_ref", "api_key", "install"}
        if any(key.lower() in forbidden or (isinstance(value, str) and len(value) > 1000) for key, value in values.items()):
            raise ValueError("Method input cannot change execution, permissions, providers or credentials")
        return values

    @field_validator("steps")
    @classmethod
    def valid_steps(cls, values):
        from src.native_tools.registry import TOOL_METADATA
        if any(isinstance(step, ToolStep) and step.tool_id not in TOOL_METADATA for step in values):
            raise ValueError("Each tool step must reference an existing registered tool")
        return values


Candidate = Annotated[ResearchStrategy | TaskMethod, Field(discriminator="schema_version")]


class LessonScope(ClosedModel):
    goal_id: Identifier
    goal_revision: int = Field(ge=1)
    family: Literal["research", "software", "knowledge", "general"]


class LessonRequest(ClosedModel):
    task_id: Identifier
    attempt_id: Identifier
    correction: str = Field(max_length=4000)
    source_refs: list[Annotated[str, Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")]] = Field(min_length=1, max_length=16)
    scope: LessonScope
    expected_revision: int = Field(ge=1)

    @field_validator("source_refs")
    @classmethod
    def unique_refs(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("Source references must be unique")
        return values


class LessonAutoPolicyRequest(ClosedModel):
    enabled: bool
    expected_revision: int = Field(ge=1)
    mutation_uuid: str

    @field_validator("mutation_uuid")
    @classmethod
    def canonical_uuid(cls, value):
        if str(UUID(value)) != value:
            raise ValueError("mutation_uuid must be a canonical UUID")
        return value


def _policy_binding(task):
    return digest({"task_id": task.task_id, "goal_id": task.goal_id, "goal_revision": task.goal_revision,
        "capability_id": task.capability_id, "typed_input_digest": task.typed_input_digest})


async def _automatic_policy(db, operator, task):
    event = (await db.execute(select(WorkBoardEvent).where(WorkBoardEvent.task_id == task.task_id,
        WorkBoardEvent.owner_principal_id == operator.principal.principal_id,
        WorkBoardEvent.owner_session_id == operator.session_id,
        WorkBoardEvent.kind == "task_lesson.automatic_policy.v1")
        .order_by(WorkBoardEvent.event_id.desc()).limit(1))).scalar_one_or_none()
    state = json.loads(event.metadata_json) if event else {}
    return {"enabled": state.get("enabled") is True and state.get("task_binding") == _policy_binding(task),
        "policy_revision": event.event_id if event else None, "daily_cap": 2,
        "inference_egress": "not_permitted", "adoption": "requires_separate_review"}


async def set_automatic_lesson_policy(operator, task_id, request: LessonAutoPolicyRequest):
    async with db_engine.get_session() as db:
        await _begin_sqlite_immediate(db)
        await assert_current_root(db, operator)
        task = await _task(db, task_id)
        owner = (operator.principal.principal_id, operator.session_id)
        if task is None or (task.owner_principal_id, task.owner_session_id) != owner:
            raise BoardError("lesson_owner_mismatch", "The task belongs to another operator", status_code=403)
        if task.task_revision != request.expected_revision:
            raise BoardError("lesson_task_changed", "Refresh the current task before changing automatic proposal consent")
        request_sha = digest({"task_id": task_id, **request.model_dump()})
        existing = (await db.execute(select(WorkBoardEvent).where(
            WorkBoardEvent.owner_principal_id == owner[0], WorkBoardEvent.owner_session_id == owner[1],
            WorkBoardEvent.mutation_idempotency_key == request.mutation_uuid))).scalar_one_or_none()
        if existing:
            if existing.kind != "task_lesson.automatic_policy.v1" or existing.mutation_request_digest != request_sha:
                raise BoardError("lesson_policy_request_conflict", "This request key already binds another change")
            return await _automatic_policy(db, operator, task)
        db.add(WorkBoardEvent(task_id=task_id, owner_principal_id=owner[0], owner_session_id=owner[1],
            actor_principal_id=owner[0], actor_session_id=owner[1], kind="task_lesson.automatic_policy.v1",
            mutation_idempotency_key=request.mutation_uuid, mutation_request_digest=request_sha,
            metadata_json=canonical({"enabled": request.enabled, "task_revision": task.task_revision,
                "task_binding": _policy_binding(task), "egress_permitted": False, "daily_cap": 2})))
        await db.flush()
        return await _automatic_policy(db, operator, task)


async def _source(db, operator, request: LessonRequest, *, automatic=False, staged=None):
    """Read current authority and the actual durable attempt, never a caller vote."""
    await _assert_owner(db, operator, automatic=automatic)
    task = await _task(db, request.task_id)
    owner = (operator.principal.principal_id, operator.session_id)
    if task is None or (task.owner_principal_id, task.owner_session_id) != owner:
        raise BoardError("lesson_owner_mismatch", "The task belongs to another operator", status_code=403)
    if task.task_revision != request.expected_revision:
        raise BoardError("lesson_task_changed", "Refresh the exact task revision")
    goal = await db.get(Goal, task.goal_id, populate_existing=True)
    if (goal is None or (goal.owner_principal_id, goal.owner_session_id) != owner
        or (task.goal_id, task.goal_revision) != (request.scope.goal_id, request.scope.goal_revision)
        or goal.revision != request.scope.goal_revision):
        raise BoardError("lesson_scope_changed", "The current goal and scope must match")
    attempt = (await db.execute(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task.task_id)
        .order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc()).limit(1))).scalar_one_or_none()
    if attempt is None or attempt.attempt_id != request.attempt_id or attempt.ended_at is None:
        raise BoardError("lesson_attempt_unverified", "The current attempt must have a terminal receipt")
    run = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id))).scalar_one_or_none()
    from src.work_board.review import _workflow_run_binds_board_attempt
    if run is None or (staged is None and not _workflow_run_binds_board_attempt(task, attempt, run)):
        raise BoardError("lesson_run_unverified", "The durable run must bind the exact attempt")
    db_binding = digest([[{column.name: str(getattr(row, column.name)) for column in row.__table__.columns}]
        for row in (task, attempt, run, goal)])
    if staged is not None:
        if not isinstance(staged, _SourceStage) or staged.seal is not _STAGE_SEAL or staged.token.get("db_binding_digest") != db_binding:
            raise BoardError("lesson_source_changed", "The staged canonical source changed")
    authority = json.loads(run.declared_authority_json or "{}")
    # Existing native no_learning means that invocation performs no learning.
    # It does not exclude a later explicit metadata-only lesson. Distinct
    # source-specific exclusions remain absolute; source bodies are not copied
    # into lessons or projected to any provider by verification.
    if authority.get("source_learning_excluded") is True:
        raise BoardError("lesson_source_excluded", "This source explicitly excludes learning")
    effects = json.loads(run.effect_receipts_json)
    if any(isinstance(effect, dict) and effect.get("status") in {"unknown", "contact_started", "pending"} for effect in effects):
        raise BoardError("lesson_outcome_unresolved", "Unresolved contacted work cannot authorize a lesson")
    if staged is not None:
        observed = staged.token["observed"]
        verified_refs = staged.token["verified_refs"]
    elif run.status == "succeeded":
        from src.memory.m5 import _verified_source, _source_refs
        try:
            proof = await _verified_source(db, task, requested_attempt_id=attempt.attempt_id)
        except ValueError as exc:
            raise BoardError("lesson_source_unverified", "The ordinary task readback is not verified") from exc
        verified_refs = _source_refs(proof.readback)
        observed = {"status": "completed", "readback_digest": proof.evidence_digest}
    elif run.status == "failed" and run.finished_at is not None:
        # Failure is an observation, not a successful readback or positive vote.
        verified_refs = [run.run_identity, attempt.attempt_id]
        observed = {"status": "failed", "failure_reason_digest": digest(run.failure_reason or run.error or "unspecified")}
    else:
        raise BoardError("lesson_outcome_unresolved", "Unresolved, blocked or contacted-unknown work cannot authorize reflection")
    if not set(request.source_refs).issubset(verified_refs):
        raise BoardError("lesson_source_unbound", "Only references verified against this exact attempt are allowed")
    token = {"task_revision": task.task_revision, "goal_revision": goal.revision,
        "attempt_id": attempt.attempt_id, "fence": attempt.fencing_token,
        "run_id": run.run_identity, "run_revision": run.revision,
        "receipt_digest": digest(attempt.receipt_refs_json), "artifacts_digest": digest(run.artifact_receipts_json),
        "effects_digest": digest(run.effect_receipts_json), "observed": observed,
        "run_fingerprint": run.run_fingerprint, "input_digest": run.input_digest,
        "authority_digest": digest(run.declared_authority_json), "status": run.status,
        "task_status": task.status.value, "attempt_outcome": attempt.outcome,
        "db_binding_digest": db_binding, "verified_refs": verified_refs,
        "task_intent_digest": digest({"capability": task.capability_id, "input": task.typed_input_digest,
            "goal": task.goal_id, "goal_revision": task.goal_revision})}
    return task, attempt, run, token


async def _observed_method(db, task, run, family):
    """Project only recorded tool identities, never source bodies or arguments."""
    if task.capability_id == "work.json-format.v1":
        from src.work_board.dispatcher import REGISTERED_CAPABILITIES
        from src.work_board.tool_package_contracts import JsonFormatInput
        from src.execution.tool_package_profile import source_package, MAX_INPUT, MAX_OUTPUT, PROFILE
        capability = REGISTERED_CAPABILITIES.get(task.capability_id)
        if capability is None or capability.version != "1" or run.capability_version != "1":
            return None, None
        authority = json.loads(run.declared_authority_json)
        input_schema = JsonFormatInput.model_json_schema()
        output_contract = {"artifact_type": "tool_package_json", "content": "bounded_sorted_json",
            "max_input_bytes": MAX_INPUT, "max_output_bytes": MAX_OUTPUT, "profile": PROFILE,
            "duplicate_keys": "rejected", "nonfinite_numbers": "rejected"}
        snapshot = {"capability_id": capability.capability_id, "capability_version": capability.version,
            "input_schema": input_schema, "output_contract": output_contract,
            "package_code_digest": hashlib.sha256(source_package().read_bytes()).hexdigest(),
            "admitted_pack": authority.get("pack"), "admitted_runtime": authority.get("runtime")}
        step = CapabilityStep(capability_id=task.capability_id, capability_version=capability.version,
            typed_input_digest=task.typed_input_digest, input_schema_digest=digest(input_schema),
            output_contract_digest=digest(output_contract), contract_snapshot_digest=digest(snapshot))
        method = TaskMethod(family=family, steps=[step], registered_tool_ids=[], input_parameters={},
            output_contract=MethodOutput(artifact_type="tool_package_json", required_fields=["artifact_ref", "content_sha256", "readback_id"]))
        return method, {"native_contract_snapshot": snapshot, "contract_snapshot_digest": digest(snapshot)}
    rows = list((await db.execute(select(WorkflowStepState).where(WorkflowStepState.run_identity == run.run_identity)
        .order_by(WorkflowStepState.step_index.asc()).limit(17))).scalars().all())
    if not 1 <= len(rows) <= 15:
        return None, None
    from src.native_tools.registry import TOOL_METADATA
    if any(row.tool_name not in TOOL_METADATA or row.completed_at is None
        or row.status not in {"succeeded", "completed", "failed", "continued_error"} for row in rows):
        return None, None
    steps = [{"id": row.id, "step_id": row.step_id, "index": row.step_index,
        "tool": row.tool_name, "status": row.status, "updated_at": row.updated_at.isoformat()} for row in rows]
    method = TaskMethod(family=family,
        steps=[ToolStep(tool_id=step["tool"]) for step in steps],
        registered_tool_ids=list(dict.fromkeys(step["tool"] for step in steps)),
        input_parameters={}, output_contract=MethodOutput(artifact_type="task_result", required_fields=["status", "source_refs"]))
    return method, {"step_refs": [row.id for row in rows], "steps_digest": digest(steps)}


def _correct_method(old: TaskMethod | None, correction: str):
    """Finite deterministic lesson grammar; arbitrary prose remains evidence only."""
    if old is None or not correction.strip():
        return None
    normalized = correction.lower()
    if re.search(r"\b(check|verify|ensure)\b.*\b(source|file)\b.*\b(exists?|existence|present)\b", normalized):
        guard, before = "source_exists", True
    elif re.search(r"\b(verify|verified|require|check)\b.*\b(readback|read.back)\b", normalized):
        guard, before = "verified_readback", False
    elif re.search(r"\b(preserve|include|keep|require)\b.*\b(attribution|citations?|source references)\b", normalized):
        guard, before = "preserve_source_attribution", False
    else:
        return None
    added = GuardStep(check=guard)
    return TaskMethod.model_validate({**old.model_dump(), "steps":
        [added.model_dump(), *[step.model_dump() for step in old.steps]] if before
        else [*[step.model_dump() for step in old.steps], added.model_dump()]})


async def create_task_lesson(operator, request: LessonRequest, *, _automatic: bool = False):
    """Draft locally from an explicit correction; never contact any provider."""
    from src.memory.m5 import sanitize_m5_memory_text_async
    # Vault-aware sanitization is staged before the SQLite writer lock.
    try:
        correction = await sanitize_m5_memory_text_async(request.correction) if request.correction.strip() else ""
    except ValueError as exc:
        unavailable = "unavailable" in str(exc)
        raise BoardError("lesson_redaction_unavailable" if unavailable else "lesson_correction_unsafe",
            "Restore redaction before requesting the lesson" if unavailable else "Remove secret or authority-changing text from the correction",
            status_code=503 if unavailable else 422) from exc
    if len(correction) > 1000:
        raise BoardError("lesson_correction_limit", "Use a correction of at most 1000 characters", status_code=422)
    async with db_engine.get_session() as db:
        task, attempt, run, token = await _source(db, operator, request, automatic=_automatic)
        policy = await _automatic_policy(db, operator, task) if _automatic else None
        if _automatic and not policy["enabled"]:
            return {"status": "blocked", "reason_code": "automatic_lessons_not_opted_in", "result": "no_change", "behavior_changed": False}
        old, audit_token = await _observed_method(db, task, run, request.scope.family)
        token["method_receipt"] = audit_token
        staged = _SourceStage(_STAGE_SEAL, token, old, audit_token)
        binding = digest({"owner": task.owner_principal_id, "root": task.owner_session_id,
            "request": request.model_dump(), "correction_digest": digest(correction), "source": token,
            "automatic_policy": policy})
        previous = (await db.execute(select(MemoryProposal).where(
            MemoryProposal.schema_version == PROPOSAL_SCHEMA,
            MemoryProposal.owner_principal_id == task.owner_principal_id,
            MemoryProposal.owner_session_id == task.owner_session_id,
            MemoryProposal.request_binding_digest == binding))).scalar_one_or_none()
        if previous:
            return {**proposal_projection(previous), "idempotent_replay": True}
        candidate = _correct_method(old, correction)
        reason = ("observed_failure_candidate" if _automatic else "explicit_correction") if candidate else "insufficient_method_evidence" if old is None else "no_explicit_correction" if not correction else "unsupported_correction_no_change"
        envelope = {"schema_version": PROPOSAL_SCHEMA, "old_method": old.model_dump() if old else None,
            "new_method": candidate.model_dump() if candidate else None, "correction": correction,
            "correction_provenance": "observed_failure_rule" if _automatic else "explicit_operator",
            "lesson_provenance": "deterministic_draft_from_observed_failure" if _automatic else "deterministic_draft_from_correction",
            "observed": token["observed"], "source_token": token, "source_refs": request.source_refs,
            "scope": request.scope.model_dump(), "behavior_changed": False, "positive_preference_vote": False,
            "reflection": {"mode": "local_projection", "provider_contacts": 0, "spend_microusd": 0}}
        raw = canonical(envelope).encode()
        sha = hashlib.sha256(raw).hexdigest()
        relative = f"artifacts/memory/task-lessons/{binding}.json"
        staged_task_revision = task.task_revision
    from src.workspace import canonical_workspace_root
    from src.work_board.input_artifacts import _write_payload
    _write_payload(canonical_workspace_root(settings.workspace_dir) / relative, raw)
    read_private_proof(relative, sha)
    async with db_engine.get_session() as db:
        await _begin_sqlite_immediate(db)
        task, attempt, run, current = await _source(db, operator, request, automatic=_automatic, staged=staged)
        if task.capability_id == "work.json-format.v1":
            current_method, current_audit = staged.method, staged.method_token
        else:
            current_method, current_audit = await _observed_method(db, task, run, request.scope.family)
        current["method_receipt"] = current_audit
        if current != token or task.task_revision != staged_task_revision:
            raise BoardError("lesson_source_changed", "The exact ordinary task evidence changed; request a new lesson")
        previous = (await db.execute(select(MemoryProposal).where(
            MemoryProposal.schema_version == PROPOSAL_SCHEMA,
            MemoryProposal.owner_principal_id == task.owner_principal_id,
            MemoryProposal.owner_session_id == task.owner_session_id,
            MemoryProposal.request_binding_digest == binding))).scalar_one_or_none()
        if previous:
            return {**proposal_projection(previous), "idempotent_replay": True}
        if _automatic:
            current_policy = await _automatic_policy(db, operator, task)
            if current_policy != policy or not current_policy["enabled"]:
                raise BoardError("lesson_policy_changed", "Automatic proposal consent changed during staging")
            midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            today = list((await db.execute(select(MemoryProposal).where(
                MemoryProposal.owner_principal_id == task.owner_principal_id,
                MemoryProposal.schema_version == PROPOSAL_SCHEMA,
                MemoryProposal.created_at >= midnight))).scalars().all())
            if sum(json.loads(item.provenance_json).get("automatic") is True for item in today) >= 2:
                return {"status": "blocked", "reason_code": "automatic_lesson_daily_cap", "result": "no_change", "behavior_changed": False}
        row = MemoryProposal(schema_version=PROPOSAL_SCHEMA, owner_principal_id=task.owner_principal_id,
            owner_session_id=task.owner_session_id, source_task_id=task.task_id,
            source_task_revision=task.task_revision, source_attempt_id=attempt.attempt_id,
            source_attempt_fence=attempt.fencing_token, workflow_run_id=run.run_identity,
            workflow_run_revision=run.revision, goal_id=task.goal_id, goal_revision=task.goal_revision,
            capability_id=task.capability_id or "legacy-workflow", capability_version=run.capability_version,
            typed_input_digest=task.typed_input_digest or "", source_context_digest=digest(token),
            evidence_digest=digest(token), artifact_ref=relative, artifact_digest=sha,
            proposal_job_id=f"task-lesson:{binding}", request_idempotency_key=binding,
            request_binding_digest=binding, memory_scope_json=canonical(request.scope.model_dump()),
            source_refs_json=canonical(request.source_refs), provenance_json=canonical({"source_token": token,
                "correction_digest": digest(correction), "positive_preference_vote": False,
                "automatic": _automatic, "automatic_policy": policy}),
            status=MemoryProposalStatus.proposed if candidate else MemoryProposalStatus.blocked,
            reason_code=reason, recovery_action="review_candidate" if candidate else "supply_explicit_correction_and_verified_method_receipt")
        db.add(row)
        await db.commit()
        await db.refresh(row)
        payload = proposal_projection(row)
    from src.evolution.runtime import EvolutionRuntime
    runtime = EvolutionRuntime(EvolutionRuntime.default_path(settings.workspace_dir))
    runtime.record_task_lesson(proposal_id=payload["proposal_id"], owner_id=operator.principal.principal_id,
        source_digest=digest(token), candidate_digest=sha, result="candidate_inert" if candidate else "no_change")
    return payload


async def propose_automatic_task_lesson(operator, task_id):
    """Current authenticated owner callback; finite failure-derived proposals only."""
    source = await eligible_lesson_source(operator, task_id, _automatic=True)
    if not source["eligible"]:
        return {"status": "blocked", "reason_code": source["reason_code"], "result": "no_change", "behavior_changed": False}
    async with db_engine.get_session() as db:
        await _assert_owner(db, operator, automatic=True)
        task = await _task(db, task_id)
        policy = await _automatic_policy(db, operator, task)
        if not policy["enabled"]:
            return {"status": "blocked", "reason_code": "automatic_lessons_not_opted_in", "result": "no_change", "behavior_changed": False}
        attempt = await db.get(WorkBoardAttempt, source["attempt_id"])
        steps = list((await db.execute(select(WorkflowStepState).where(WorkflowStepState.run_identity == attempt.workflow_run_id))).scalars().all())
        missing_source = source.get("observed", {}).get("status") == "failed" and any(step.error_kind == "FileNotFoundError" for step in steps)
    if not missing_source:
        return {"status": "no_change", "reason_code": "no_supported_failure_lesson", "result": "no_change", "behavior_changed": False}
    return await create_task_lesson(operator, LessonRequest(task_id=task_id, attempt_id=source["attempt_id"],
        correction="Check source existence before using the selected source.", source_refs=source["source_refs"],
        scope=LessonScope.model_validate(source["scope"]), expected_revision=source["expected_revision"]), _automatic=True)


async def maybe_propose_automatic_lesson(task):
    """Called only after committed terminal projection; exact Root, no renewal."""
    from src.auth.service import authenticate_session
    operator = await authenticate_session(task.owner_session_id, touch=False)
    if operator.principal.principal_id != task.owner_principal_id:
        raise BoardError("lesson_owner_mismatch", "The original task owner changed", status_code=403)
    return await propose_automatic_task_lesson(operator, task.task_id)


async def inspect_task_lesson(operator, proposal_id):
    async with db_engine.get_session() as db:
        await assert_current_root(db, operator)
        row = await db.get(MemoryProposal, proposal_id, populate_existing=True)
        if (row is None or row.schema_version != PROPOSAL_SCHEMA or
            (row.owner_principal_id, row.owner_session_id) != (operator.principal.principal_id, operator.session_id)):
            raise BoardError("lesson_owner_mismatch", "The lesson belongs to another operator", status_code=403)
        payload = proposal_projection(row)
        ref, sha = row.artifact_ref, row.artifact_digest
    raw = read_private_proof(ref, sha)
    envelope = json.loads(raw)
    request = LessonRequest(task_id=payload["task_id"], attempt_id=payload["attempt_id"],
        correction=envelope["correction"], source_refs=envelope["source_refs"], scope=LessonScope.model_validate(envelope["scope"]),
        expected_revision=envelope["source_token"]["task_revision"])
    async with db_engine.get_session() as db:
        try:
            task, attempt, run, token = await _source(db, operator, request)
            _, audit_token = await _observed_method(db, task, run, request.scope.family)
            token["method_receipt"] = audit_token
            current = token == envelope["source_token"]
        except BoardError:
            current = False
    return {**envelope, **payload, "source_current": current, "status": payload["status"] if current else "blocked",
        "reason_code": payload["reason_code"] if current else "lesson_source_changed"}


def proposal_projection(row):
    return {"proposal_id": row.proposal_id, "schema_version": row.schema_version,
        "task_id": row.source_task_id, "attempt_id": row.source_attempt_id,
        "revision": row.revision, "status": row.status.value, "reason_code": row.reason_code,
        "source_refs": json.loads(row.source_refs_json), "scope": json.loads(row.memory_scope_json or "{}"),
        "candidate_digest": row.artifact_digest, "behavior_changed": False,
        "result": "candidate_inert" if row.reason_code in {"explicit_correction", "observed_failure_candidate"} else "no_change",
        "provider_contact_count": row.provider_contact_count, "quality_evidence": "unmeasured"}


async def eligible_lesson_source(operator, task_id, *, _automatic=False):
    """Authoritative input discovery. Never expose caller-selected evidence."""
    async with db_engine.get_session() as db:
        await _assert_owner(db, operator, automatic=_automatic)
        task = await _task(db, task_id)
        if task is None or (task.owner_principal_id, task.owner_session_id) != (operator.principal.principal_id, operator.session_id):
            raise BoardError("lesson_owner_mismatch", "The task belongs to another operator", status_code=403)
        attempt = (await db.execute(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task_id)
            .order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc()).limit(1))).scalar_one_or_none()
        payload = {"task_id": task_id, "expected_revision": task.task_revision,
            "attempt_id": attempt.attempt_id if attempt else None, "source_refs": [],
            "scope": {"goal_id": task.goal_id, "goal_revision": task.goal_revision,
                "family": "research" if "research" in (task.capability_id or "") else "general"},
            "eligible": False, "reason_code": "lesson_attempt_unverified", "behavior_changed": False}
        payload["automatic_policy"] = await _automatic_policy(db, operator, task)
        if attempt is None:
            return payload
        run = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id))).scalar_one_or_none()
        refs = [attempt.attempt_id, attempt.workflow_run_id] if attempt.workflow_run_id else []
        if run is not None and run.status == "succeeded":
            from src.memory.m5 import _verified_source, _source_refs
            try:
                proof = await _verified_source(db, task, requested_attempt_id=attempt.attempt_id)
                refs = _source_refs(proof.readback)
            except ValueError:
                return {**payload, "reason_code": "lesson_run_unverified"}
        if not refs:
            return payload
        request = LessonRequest(task_id=task_id, attempt_id=attempt.attempt_id, correction="",
            source_refs=refs, scope=LessonScope.model_validate(payload["scope"]), expected_revision=task.task_revision)
        try:
            _, _, run, token = await _source(db, operator, request, automatic=_automatic)
            method, _ = await _observed_method(db, task, run, request.scope.family)
        except BoardError as exc:
            return {**payload, "reason_code": exc.code}
        return {**payload, "source_refs": refs, "eligible": method is not None,
            "reason_code": "verified_ordinary_task" if method else "insufficient_method_evidence",
            "observed": token["observed"], "supported_guards": ["source_exists", "verified_readback", "preserve_source_attribution"]}
