"""Reviewed typed task methods on canonical M5 memory and a signed pointer.

The pointer selects future admissions. Existing admissions validate their exact
immutable pin without consulting that pointer. This module owns no execution,
inference, generic recall, permissions, or budget.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from sqlalchemy import select, func, case, or_
from sqlalchemy.exc import SQLAlchemyError
from src.extensions.capability_execution import CapabilityJournalError

from src.auth.service import AuthFailure, authenticate_principal
from src.db import engine as database
from src.db.models import (Goal, Memory, MemoryProposal, MemoryProposalStatus,
    MemoryTombstone, OperatorIdentity, OperatorSession, WorkBoardAttempt,
    WorkBoardTask, WorkflowRunState, WorkBoardEvent, MemoryKind, MemoryProposalDecisionEffect)
from src.db.task_method_models import TaskMethodActive
from src.memory.procedure_recommendations import canonical, digest, assert_current_root, read_private_proof
from src.memory.task_lessons import Candidate, LessonScope, PROPOSAL_SCHEMA, ResearchStrategy, TaskMethod
from src.work_board.contracts import TaskStrategyBinding
from src.work_board.repository import BoardError, _begin_sqlite_immediate

MAX_VERSIONS = 16
MAX_BYTES = 64 * 1024
ACTION_KIND = "task_method.review.v1"
Id = Annotated[str, Field(min_length=1, max_length=128)]
Sha = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
CANDIDATE = TypeAdapter(Candidate)


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class TaskMethodReview(Closed):
    proposal_id: Id
    expected_revision: int = Field(ge=1)
    artifact_digest: Sha
    scope_digest: Sha
    action: Literal["accept", "reject", "rollback", "disable", "activate", "delete"]
    reason: str = Field(max_length=500)
    idempotency_key: Id


class MethodOwner(Closed):
    identity_id: Id
    issuer_principal_id: Id
    issuer_root_id: Id


class ActiveMethodBinding(Closed):
    owner: MethodOwner
    scope: LessonScope
    version: Id
    digest: Sha
    proposal_id: Id


class TaskMethodScope(Closed):
    schema_version: Literal["task_method_scope.v1"] = "task_method_scope.v1"
    owner: MethodOwner
    goal_id: Id
    goal_revision: int = Field(ge=1)
    family: Literal["research", "software", "knowledge", "general"]
    candidate_schema: Literal["TaskMethod.v1", "ResearchStrategy.v1", "ProcedurePlan.v3"]
    candidate_digest: Sha
    candidate_version: Id
    proposal_id: Id
    source_context_digest: Sha
    source_token_digest: Sha
    source_scope_digest: Sha
    source_task_id: Id
    source_task_revision: int = Field(ge=1)
    source_attempt_id: Id
    source_attempt_fence: int = Field(ge=1)
    workflow_run_id: Id
    workflow_run_revision: int = Field(ge=0)
    source_refs: list[Id] = Field(min_length=1, max_length=16)


def _fail(code, message="Review the original method, source and current authority"):
    raise BoardError(code, message)


def _family(task_family):
    return {"work.general-task.v1": "general", "guardian.goal-discovery.v1": "research"}.get(task_family)


def _pointer_payload(row):
    return {"schema_version": "task_method_pointer.v1", "owner_identity_id": row.owner_identity_id,
        "goal_id": row.goal_id, "goal_revision": row.goal_revision, "family": row.family,
        "revision": row.revision, "binding": json.loads(row.binding_json),
        "previous_binding": json.loads(row.previous_binding_json), "baseline": row.baseline}


def _pointer_mac(row, key):
    return hmac.new(key, b"seraph-task-method-pointer-v1:" + canonical(_pointer_payload(row)).encode(),
        hashlib.sha256).hexdigest()


def _pointer_valid(row, key):
    from src.memory.repository import _m5_selection_binding_key_id
    try:
        valid = hmac.compare_digest(row.signature_key_id, _m5_selection_binding_key_id(_signing_key=key))
        valid = valid and hmac.compare_digest(row.signature_mac, _pointer_mac(row, key))
        if row.baseline:
            return valid and json.loads(row.binding_json) is None
        binding = ActiveMethodBinding.model_validate_json(row.binding_json)
        return valid and (binding.owner.identity_id, binding.scope.goal_id, binding.scope.goal_revision,
            binding.scope.family) == (row.owner_identity_id, row.goal_id, row.goal_revision, row.family)
    except (TypeError, ValueError):
        return False


async def _pointer(db, identity_id, scope):
    rows = list((await db.execute(select(TaskMethodActive).where(
        TaskMethodActive.owner_identity_id == identity_id, TaskMethodActive.goal_id == scope.goal_id,
        TaskMethodActive.goal_revision == scope.goal_revision, TaskMethodActive.family == scope.family).limit(2))).scalars())
    if len(rows) > 1:
        _fail("method_selection_ambiguous")
    return rows[0] if rows else None


async def _context(db, owner, goal_ref, family, programme_grant=None):
    goal = await db.get(Goal, goal_ref, populate_existing=True)
    if goal is None:
        _fail("method_goal_missing")
    if programme_grant is not None:
        from src.guardian.goal_programmes import goal_programme_service
        from src.workflows.research_guard import discovery_writer_scope
        from src.work_board.research_parent import DISCOVERY_SERVICE, DISCOVERY_CAPABILITY
        if (owner.principal_id != DISCOVERY_SERVICE or owner.session_id != ""
            or programme_grant.capability_id != DISCOVERY_CAPABILITY):
            _fail("method_programme_service_mismatch")
        if programme_grant.goal_id != goal_ref:
            _fail("method_programme_scope_mismatch")
        async with discovery_writer_scope() as policy:
            programme = await goal_programme_service.validate_current_binding(db=db, binding=programme_grant, policy=policy)
        identity_id = programme.owner_identity_id
    else:
        operator = await authenticate_principal(owner.principal_id, db=db)
        if (operator.session_id != owner.session_id or goal.owner_principal_id != owner.principal_id
            or goal.owner_session_id != owner.session_id):
            _fail("method_current_owner_required")
        root = await db.get(OperatorSession, operator.session_id)
        identity_id = root.operator_identity_id
    identity = await db.get(OperatorIdentity, identity_id) if identity_id else None
    if identity_id is not None and (identity is None or identity.revoked_at):
        _fail("method_identity_revoked")
    return identity_id, LessonScope(goal_id=goal.id, goal_revision=goal.revision, family=family)


async def _source_metadata(db, proposal, scope):
    """Current canonical source metadata only; no browser revival or SourceIO."""
    token = json.loads(proposal.provenance_json)["source_token"]
    if digest(token) != scope.source_context_digest or digest(token) != scope.source_token_digest:
        _fail("method_source_token_changed")
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == proposal.source_task_id))
    attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == proposal.source_attempt_id))
    run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == proposal.workflow_run_id))
    goal = await db.get(Goal, proposal.goal_id, populate_existing=True)
    if not all((task, attempt, run, goal)):
        _fail("method_source_missing")
    if (task.task_revision != proposal.source_task_revision or task.goal_id != scope.goal_id
        or task.goal_revision != scope.goal_revision or goal.revision != scope.goal_revision
        or (task.owner_principal_id, task.owner_session_id) !=
            (scope.owner.issuer_principal_id, scope.owner.issuer_root_id)
        or (goal.owner_principal_id, goal.owner_session_id) !=
            (scope.owner.issuer_principal_id, scope.owner.issuer_root_id)
        or attempt.task_id != task.task_id or attempt.fencing_token != proposal.source_attempt_fence
        or attempt.workflow_run_id != run.run_identity or attempt.ended_at is None
        or task.status.value != token["task_status"] or attempt.outcome != token["attempt_outcome"]
        or run.run_fingerprint != token["run_fingerprint"]
        or digest({"capability": task.capability_id, "input": task.typed_input_digest,
            "goal": task.goal_id, "goal_revision": task.goal_revision}) != token["task_intent_digest"]
        or run.revision != proposal.workflow_run_revision or run.status != token["status"]
        or run.input_digest != token["input_digest"]
        or digest(run.declared_authority_json) != token["authority_digest"]
        or digest(run.artifact_receipts_json) != token["artifacts_digest"]
        or digest(run.effect_receipts_json) != token["effects_digest"]
        or digest(attempt.receipt_refs_json) != token["receipt_digest"]):
        _fail("method_source_changed")
    if json.loads(run.declared_authority_json or "{}").get("source_learning_excluded") is True:
        _fail("method_source_excluded")
    if task.capability_id == "agent.task.v1":
        from src.memory.task_lesson_native import native_source_metadata
        _, _, current_audit = await native_source_metadata(db, task, run)
        if current_audit != token.get("method_receipt"):
            _fail("method_native_source_changed")
    elif task.capability_id != "work.json-format.v1":
        from src.memory.task_lessons import _observed_method
        _, current_audit = await _observed_method(db, task, run, scope.family,
            structured_research=task.capability_id == "work.research-dossier.v1")
        if current_audit != token.get("method_receipt"):
            _fail("method_original_source_changed")


async def _history(db, scope):
    # Malformed/unknown family metadata cannot establish never-selected absence.
    family = case((func.json_valid(MemoryProposal.memory_scope_json) == 1,
        func.json_extract(MemoryProposal.memory_scope_json, "$.family")), else_=None)
    rows = list((await db.execute(select(MemoryProposal).where(
        MemoryProposal.schema_version == PROPOSAL_SCHEMA, MemoryProposal.goal_id == scope.goal_id,
        MemoryProposal.goal_revision == scope.goal_revision, MemoryProposal.accepted_memory_id.is_not(None),
        or_(family.is_(None), family == scope.family)).limit(MAX_VERSIONS + 1))).scalars())
    if len(rows) > MAX_VERSIONS:
        _fail("method_version_capacity_requires_review")
    return rows


class CurrentMethod:
    def __init__(self):
        self._key = None
        self._started = False

    async def start(self):
        from src.memory.repository import _effect_mac_key
        from src.extensions.capability_execution import CapabilityJournalError
        self._started = True
        try:
            self._key = await asyncio.to_thread(_effect_mac_key)
        except (CapabilityJournalError, OSError):
            self._key = None

    async def stop(self):
        self._key = None
        self._started = False

    async def resolve(self, owner, goal_ref, task_family, programme_grant=None):
        family = _family(task_family)
        try:
            if not self._started:
                _fail("method_lifecycle_not_started")
            async with database.get_session() as db:
                identity, scope = await _context(db, owner, goal_ref, family or "general", programme_grant)
                if family is None:
                    return TaskStrategyBinding(status="none", reason="baseline")
                history = await _history(db, scope)
                if identity is None:
                    any_pointer = await db.scalar(select(TaskMethodActive.id).where(
                        TaskMethodActive.goal_id == scope.goal_id, TaskMethodActive.goal_revision == scope.goal_revision,
                        TaskMethodActive.family == scope.family).limit(1))
                    if history or any_pointer:
                        _fail("method_stable_identity_required")
                    return TaskStrategyBinding(status="none", reason="baseline")
                pointer = await _pointer(db, identity, scope)
                if pointer is None:
                    other_pointer = await db.scalar(select(TaskMethodActive.id).where(
                        TaskMethodActive.goal_id == scope.goal_id,
                        TaskMethodActive.goal_revision == scope.goal_revision,
                        TaskMethodActive.family == scope.family).limit(1))
                    if other_pointer is not None:
                        _fail("method_pointer_owner_changed")
                    if history:
                        _fail("method_pointer_missing")
                    return TaskStrategyBinding(status="none", reason="baseline")
                if self._key is None:
                    _fail("method_signing_key_unavailable")
                if not _pointer_valid(pointer, self._key):
                    _fail("method_pointer_invalid")
                if pointer.baseline:
                    return TaskStrategyBinding(status="none", reason="baseline")
                selected = ActiveMethodBinding.model_validate_json(pointer.binding_json)
                return await self._version(db, selected, identity, scope, allow_rollback=False)
        except (BoardError, AuthFailure, ValueError, KeyError, TypeError, SQLAlchemyError) as error:
            return TaskStrategyBinding(status="blocked", reason=getattr(error, "code", "method_selection_invalid"))

    async def validate_pinned(self, owner, goal_ref, binding, programme_grant=None, db=None):
        try:
            return await self._validate_pinned(owner, goal_ref, binding, programme_grant, db)
        except AuthFailure as error:
            raise BoardError("method_current_owner_required", "Authenticate the current task owner before execution", status_code=403) from error
        except (ValueError, KeyError, TypeError) as error:
            raise BoardError("method_pin_metadata_invalid", "The original method pin requires current review") from error
        except SQLAlchemyError as error:
            raise BoardError("method_pin_store_unavailable", "Restore canonical method storage before execution", status_code=503) from error

    async def _validate_pinned(self, owner, goal_ref, binding, programme_grant=None, db=None):
        if not self._started:
            _fail("method_lifecycle_not_started")
        binding = TaskStrategyBinding.model_validate(binding)
        if binding.status == "blocked":
            _fail("method_pin_blocked")
        if db is None:
            async with database.get_session() as session:
                return await self.validate_pinned(owner, goal_ref, binding, programme_grant, session)
        family = "research" if programme_grant is not None else "general"
        identity, scope = await _context(db, owner, goal_ref, family, programme_grant)
        if binding.status == "none":
            return binding  # An admitted baseline does not consult a future pointer.
        if identity is None or self._key is None:
            _fail("method_pin_identity_or_key_unavailable")
        proposal = await db.get(MemoryProposal, binding.method_id, populate_existing=True)
        if proposal is None or proposal.schema_version != PROPOSAL_SCHEMA:
            _fail("method_pin_proposal_missing")
        memory = await db.get(Memory, binding.version, populate_existing=True)
        if memory is None:
            _fail("method_pin_version_missing")
        signed = TaskMethodScope.model_validate(json.loads(memory.metadata_json)["work_board_provenance"]["memory_scope"])
        selected = ActiveMethodBinding(owner=signed.owner, scope=scope, version=binding.version,
            digest=binding.digest, proposal_id=binding.method_id)
        verified = await self._version(db, selected, identity, scope, allow_rollback=True)
        if verified != binding:
            _fail("method_pin_candidate_changed")
        return binding

    async def _version(self, db, selected, identity, scope, *, allow_rollback):
        from src.memory.repository import (_canonical_memory_deletion_marker, _m5_verified_source_binding,
            _m5_selection_binding_mac, _m5_selection_binding_key_id)
        proposal = await db.get(MemoryProposal, selected.proposal_id, populate_existing=True)
        memory = await db.get(Memory, selected.version, populate_existing=True)
        if (proposal is None or memory is None or proposal.schema_version != PROPOSAL_SCHEMA
            or proposal.accepted_memory_id != memory.id or selected.owner.identity_id != identity
            or memory.kind != MemoryKind.pattern or proposal.memory_kind != MemoryKind.pattern
            or proposal.decision_effect != MemoryProposalDecisionEffect.require_operator_confirmation
            or selected.scope != scope or _canonical_memory_deletion_marker(memory) is not None
            or await db.scalar(select(MemoryTombstone.memory_id).where(MemoryTombstone.memory_id == memory.id))):
            _fail("method_version_unavailable")
        provenance = json.loads(memory.metadata_json)["work_board_provenance"]
        signed = TaskMethodScope.model_validate(provenance["memory_scope"])
        if TaskMethodScope.model_validate_json(proposal.memory_scope_json) != signed:
            _fail("method_original_scope_changed")
        candidate = CANDIDATE.validate_json(memory.content)
        typed = candidate.model_dump(mode="json")
        candidate_sha = digest(typed)
        if (signed.owner != selected.owner or signed.proposal_id != proposal.proposal_id
            or signed.candidate_version != memory.id or signed.candidate_digest != candidate_sha
            or signed.candidate_schema != typed["schema_version"]
            or signed.source_task_id != proposal.source_task_id
            or signed.source_attempt_id != proposal.source_attempt_id
            or signed.source_attempt_fence != proposal.source_attempt_fence
            or signed.source_task_revision != proposal.source_task_revision
            or signed.workflow_run_id != proposal.workflow_run_id
            or signed.workflow_run_revision != proposal.workflow_run_revision
            or signed.source_refs != json.loads(proposal.source_refs_json)
            or signed.source_scope_digest != digest(scope.model_dump(mode="json"))
            or signed.source_context_digest != proposal.source_context_digest
            or candidate_sha != selected.digest or proposal.accepted_memory_content_digest != digest(typed)
            or (signed.goal_id, signed.goal_revision, signed.family) != (scope.goal_id, scope.goal_revision, scope.family)
            or provenance["accepted_content_digest"] != hashlib.sha256(memory.content.encode()).hexdigest()):
            _fail("method_version_binding_invalid")
        lifecycle = provenance.get("lifecycle_state")
        if lifecycle == "active":
            if str(getattr(memory.status, "value", memory.status)) != "active" or proposal.status != MemoryProposalStatus.accepted:
                _fail("method_version_revoked")
        elif lifecycle == "rolled_back" and allow_rollback:
            if str(getattr(memory.status, "value", memory.status)) != "archived" or proposal.status != MemoryProposalStatus.rolled_back:
                _fail("method_rollback_binding_invalid")
        else:
            _fail("method_version_revoked")
        source = _m5_verified_source_binding(proposal)
        if source is None or provenance["verified_source_binding"] != source:
            _fail("method_signed_source_changed")
        mac = _m5_selection_binding_mac(proposal_id=proposal.proposal_id,
            accepted_content_digest=provenance["accepted_content_digest"],
            owner_principal_id=selected.owner.issuer_principal_id, owner_session_id=selected.owner.issuer_root_id,
            source_context_digest=signed.source_context_digest, source_binding=source,
            decision_effect=proposal.decision_effect, memory_scope=signed.model_dump(mode="json"),
            lifecycle_state=lifecycle, lifecycle_at=provenance.get("lifecycle_at"),
            lifecycle_reason=provenance.get("lifecycle_reason"), _signing_key=self._key)
        if (not hmac.compare_digest(provenance["selection_binding_key_id"], _m5_selection_binding_key_id(_signing_key=self._key))
            or not hmac.compare_digest(provenance["selection_binding_mac"], mac)):
            _fail("method_signature_invalid")
        await _source_metadata(db, proposal, signed)
        if (scope.family == "research" and not isinstance(candidate, ResearchStrategy)
            or scope.family == "general" and not (typed.get("schema_version") == "ProcedurePlan.v3"
                or isinstance(candidate, TaskMethod) and candidate.family == "general")):
            _fail("method_consumer_schema_unsupported")
        return TaskStrategyBinding(status="active", method_id=proposal.proposal_id,
            version=memory.id, digest=candidate_sha, typed_data=typed)


class TaskMethodStrategyResolver:
    def __init__(self, current):
        self.current = current

    async def resolve(self, owner, goal_ref, task_family, programme_grant=None):
        return await self.current.resolve(owner, goal_ref, task_family, programme_grant)

    async def validate_pinned(self, owner, goal_ref, binding, programme_grant=None, db=None):
        return await self.current.validate_pinned(owner, goal_ref, binding, programme_grant, db)


current_method = CurrentMethod()


async def _proposal(db, operator, proposal_id, *, mutate=False):
    await assert_current_root(db, operator)
    row = await db.get(MemoryProposal, proposal_id, populate_existing=True)
    if row is None or row.schema_version != PROPOSAL_SCHEMA:
        _fail("method_proposal_unavailable")
    goal = await db.get(Goal, row.goal_id, populate_existing=True)
    if goal is None or (goal.owner_principal_id, goal.owner_session_id) != (row.owner_principal_id, row.owner_session_id):
        _fail("method_original_scope_unavailable")
    own = (row.owner_principal_id, row.owner_session_id) == (operator.principal.principal_id, operator.session_id)
    if not own:
        from src.auth.ownership import selected_read_scopes, selected_read_principal
        scopes = await selected_read_scopes(operator, "goal", db=db)
        if (mutate or scopes.get(goal.id) != row.owner_session_id
            or await selected_read_principal(operator, "goal", goal.id, db=db) != row.owner_principal_id):
            _fail("method_current_owner_required")
    root = await db.get(OperatorSession, row.owner_session_id)
    identity = await db.get(OperatorIdentity, root.operator_identity_id) if root else None
    if identity is None or identity.revoked_at:
        _fail("method_identity_revoked")
    return row, MethodOwner(identity_id=identity.id, issuer_principal_id=row.owner_principal_id,
        issuer_root_id=row.owner_session_id), own


def _proposal_token(row):
    return digest(row.model_dump(mode="json"))


def _review_scope(owner, scope, row, candidate_sha, pointer):
    return digest({"owner": owner.model_dump(mode="json"), "scope": scope.model_dump(mode="json"),
        "proposal_id": row.proposal_id, "artifact_digest": row.artifact_digest,
        "candidate_digest": candidate_sha, "pointer": _pointer_payload(pointer) if pointer else "absent"})


@dataclass(frozen=True)
class MethodWitness:
    owner: MethodOwner
    scope: LessonScope
    proposal_token: str
    envelope: dict
    candidate: object
    source_stage: object
    source_request: object
    key: bytes
    consumer_supported: bool


def _consumer_supported(candidate, scope, proposal_id):
    """Current descriptor compatibility is data, never execution authority."""
    if isinstance(candidate, ResearchStrategy):
        return scope.family == "research"
    if scope.family != "general" or not (isinstance(candidate, TaskMethod)
        or candidate.model_dump(mode="json").get("schema_version") == "ProcedurePlan.v3"):
        return False
    from src.native_tools.registry import ToolRegistry
    from src.work_board.general_task import method_constraints
    from src.work_board.dispatcher import _dispatcher
    service = _dispatcher.general_tasks
    registry = None
    try:
        if service is not None and service.started and getattr(service.registry, "started", False):
            descriptors, _ = service.snapshot()
        else:
            registry = ToolRegistry()
            registry.start()
            descriptors = registry.descriptors()
        method_constraints(TaskStrategyBinding(status="active", method_id=proposal_id,
            version="candidate-preview", digest=digest(candidate.model_dump(mode="json")),
            typed_data=candidate.model_dump(mode="json")), descriptors)
        return True
    except BoardError:
        return False
    finally:
        if registry is not None:
            registry.stop()


async def _stage(operator, proposal_id, *, acceptance):
    from src.memory import task_lessons as lessons
    from src.memory.repository import _effect_mac_key
    async with database.get_session() as db:
        row, owner, _ = await _proposal(db, operator, proposal_id, mutate=acceptance)
        token, artifact_ref, artifact_sha = _proposal_token(row), row.artifact_ref, row.artifact_digest
        source_token = json.loads(row.provenance_json)["source_token"]
        task_revision, source_task, source_attempt = row.source_task_revision, row.source_task_id, row.source_attempt_id
        source_refs = json.loads(row.source_refs_json)
        source_scope = (row.goal_id, row.goal_revision)
    raw = await asyncio.to_thread(read_private_proof, artifact_ref, artifact_sha)
    envelope = json.loads(raw)
    if (len(raw) > MAX_BYTES or envelope["schema_version"] != PROPOSAL_SCHEMA
        or envelope["source_token"] != source_token or envelope.get("new_method") is None):
        _fail("method_original_candidate_invalid")
    scope = LessonScope.model_validate(envelope["scope"])
    if ((scope.goal_id, scope.goal_revision) != source_scope or envelope["source_refs"] != source_refs):
        _fail("method_original_scope_changed")
    candidate = CANDIDATE.validate_python(envelope["new_method"])
    consumer_supported = _consumer_supported(candidate, scope, proposal_id)
    text = canonical(candidate.model_dump(mode="json"))
    from src.memory.m5 import sanitize_m5_memory_text_async
    if await sanitize_m5_memory_text_async(text) != text:
        _fail("method_candidate_requires_redaction")
    key = await asyncio.to_thread(_effect_mac_key)
    request = lessons.LessonRequest(task_id=source_task, attempt_id=source_attempt,
        correction=envelope["correction"], source_refs=source_refs, scope=scope, expected_revision=task_revision)
    staged = None
    if acceptance:
        async with database.get_session() as db:
            task, attempt, run, current = await lessons._source(db, operator, request)
            procedure_source = None
            if candidate.model_dump(mode="json").get("schema_version") == "ProcedurePlan.v3":
                from src.memory.task_lesson_native import stage_completed_procedure_source, build_procedure_candidate
                from src.workflows.procedure_contracts import ProcedureParameterSelection
                procedure_source = await stage_completed_procedure_source(db, task, run)
                selections = [ProcedureParameterSelection.model_validate(item)
                    for item in envelope["procedure_parameter_selections"]]
                observed = build_procedure_candidate(procedure_source, selections)
                if observed != candidate:
                    _fail("method_original_candidate_changed")
                audit = procedure_source.audit
            else:
                observed, audit = await lessons._observed_method(db, task, run, scope.family,
                    structured_research=isinstance(candidate, ResearchStrategy))
            current["method_receipt"] = audit
            if current != source_token:
                _fail("method_original_source_changed")
            staged = lessons._SourceStage(lessons._STAGE_SEAL, current, observed, audit, procedure_source)
    return MethodWitness(owner, scope, token, envelope, candidate, staged, request, key, consumer_supported)


async def inspect_method(operator, proposal_id):
    try:
        return await _inspect_method(operator, proposal_id)
    except (ValueError, KeyError, TypeError) as error:
        raise BoardError("method_review_metadata_invalid", "Restore the original immutable method candidate before review") from error
    except AuthFailure as error:
        raise BoardError("method_current_owner_required", "Authenticate the current method reader", status_code=403) from error
    except (OSError, SQLAlchemyError, CapabilityJournalError) as error:
        raise BoardError("method_review_store_unavailable", "Restore the private method artifact and canonical store", status_code=503) from error


async def _inspect_method(operator, proposal_id):
    witness = await _stage(operator, proposal_id, acceptance=False)
    async with database.get_session() as db:
        row, owner, own = await _proposal(db, operator, proposal_id)
        if _proposal_token(row) != witness.proposal_token:
            _fail("method_preview_changed")
        pointer = await _pointer(db, owner.identity_id, witness.scope)
        if pointer and not _pointer_valid(pointer, witness.key):
            _fail("method_pointer_invalid")
        history = await _history(db, witness.scope)
        return {"proposal_id": row.proposal_id, "status": row.status.value, "expected_revision": row.revision,
            "task_id": row.source_task_id, "attempt_id": row.source_attempt_id,
            "artifact_digest": row.artifact_digest, "scope": witness.scope.model_dump(mode="json"),
            "scope_digest": _review_scope(owner, witness.scope, row, digest(witness.candidate.model_dump(mode="json")), pointer),
            "old_method": witness.envelope["old_method"], "new_method": witness.candidate.model_dump(mode="json"),
            "source_refs": witness.envelope["source_refs"], "observed": witness.envelope["observed"],
            "active_binding": json.loads(pointer.binding_json) if pointer and not pointer.baseline else None,
            "pointer_revision": pointer.revision if pointer else None,
            "version": row.accepted_memory_id, "digest": digest(witness.candidate.model_dump(mode="json")),
            "family_history": [{"proposal_id": item.proposal_id, "version": item.accepted_memory_id,
                "digest": item.accepted_memory_content_digest, "status": item.status.value} for item in history],
            "parameter_selections": witness.envelope.get("procedure_parameter_selections", []),
            "parameters": witness.candidate.model_dump(mode="json").get("plan", {}).get("parameters", []),
            "source_receipt": witness.envelope["source_token"].get("method_receipt"),
            "disable_scope": "general-task family" if witness.scope.family == "general" else witness.scope.family,
            "configured_baseline": bool(pointer and pointer.baseline), "quality_evidence": "unmeasured",
            "adoption_requires_current_owner": not own, "behavior_changed": row.status == MemoryProposalStatus.accepted}


async def review_method(operator, request: TaskMethodReview):
    try:
        return await _review_method(operator, request)
    except (ValueError, KeyError, TypeError) as error:
        raise BoardError("method_review_metadata_invalid", "Restore the original immutable method candidate before review") from error
    except AuthFailure as error:
        raise BoardError("method_current_owner_required", "Authenticate the current method owner", status_code=403) from error
    except (OSError, SQLAlchemyError, CapabilityJournalError) as error:
        raise BoardError("method_review_store_unavailable", "Restore the private method artifact and canonical store", status_code=503) from error


async def _review_method(operator, request: TaskMethodReview):
    from src.memory import task_lessons as lessons
    from src.memory.repository import (memory_repository, _m5_verified_source_binding,
        _m5_selection_binding_key_id, _m5_selection_binding_mac)
    owner_id, root_id = operator.principal.principal_id, operator.session_id
    request_sha = digest({"owner": owner_id, "root": root_id, "request": request.model_dump(mode="json")})
    async with database.get_session() as db:
        row, owner, _ = await _proposal(db, operator, request.proposal_id, mutate=True)
        replay = await db.scalar(select(WorkBoardEvent).where(WorkBoardEvent.owner_principal_id == owner_id,
            WorkBoardEvent.owner_session_id == root_id, WorkBoardEvent.mutation_idempotency_key == request.idempotency_key))
        if replay:
            if replay.kind != ACTION_KIND or replay.mutation_request_digest != request_sha:
                _fail("method_review_idempotency_conflict")
            return {**json.loads(replay.metadata_json)["result"], "idempotent_replay": True}
    if request.action in {"rollback", "disable", "activate", "delete"} and not request.reason.strip():
        _fail("method_rollback_reason_required")
    witness = await _stage(operator, request.proposal_id, acceptance=request.action == "accept")
    typed = witness.candidate.model_dump(mode="json")
    text, candidate_sha = canonical(typed), digest(typed)
    if request.action == "accept" and not witness.consumer_supported:
        _fail("method_consumer_schema_unsupported")
    async with database.get_session() as db:
        await _begin_sqlite_immediate(db)
        row, owner, _ = await _proposal(db, operator, request.proposal_id, mutate=True)
        replay = await db.scalar(select(WorkBoardEvent).where(WorkBoardEvent.owner_principal_id == owner_id,
            WorkBoardEvent.owner_session_id == root_id, WorkBoardEvent.mutation_idempotency_key == request.idempotency_key))
        if replay:
            if replay.kind != ACTION_KIND or replay.mutation_request_digest != request_sha:
                _fail("method_review_idempotency_conflict")
            return {**json.loads(replay.metadata_json)["result"], "idempotent_replay": True}
        pointer = await _pointer(db, owner.identity_id, witness.scope)
        if pointer and not _pointer_valid(pointer, witness.key):
            _fail("method_pointer_invalid")
        if (row.revision != request.expected_revision or _proposal_token(row) != witness.proposal_token
            or row.artifact_digest != request.artifact_digest
            or _review_scope(owner, witness.scope, row, candidate_sha, pointer) != request.scope_digest):
            _fail("method_review_stale")
        now = datetime.now(timezone.utc)
        if request.action == "accept":
            if not _consumer_supported(witness.candidate, witness.scope, request.proposal_id):
                _fail("method_consumer_schema_unsupported")
            if row.status != MemoryProposalStatus.proposed:
                _fail("method_not_proposed")
            duplicate = await db.scalar(select(MemoryProposal.proposal_id).where(
                MemoryProposal.owner_principal_id == owner_id, MemoryProposal.owner_session_id == root_id,
                MemoryProposal.source_task_id == row.source_task_id,
                MemoryProposal.source_attempt_id == row.source_attempt_id,
                MemoryProposal.preview_text_digest == candidate_sha,
                MemoryProposal.proposal_id != row.proposal_id).limit(1))
            if duplicate is not None:
                _fail("method_candidate_duplicate", "Review the existing immutable candidate for this exact source")
            task, attempt, run, token = await lessons._source(db, operator, witness.source_request, staged=witness.source_stage)
            _, audit = await lessons._observed_method_after_stage(db, task, run, witness.scope.family,
                witness.source_stage, structured_research=isinstance(witness.candidate, ResearchStrategy))
            token["method_receipt"] = audit
            if token != witness.envelope["source_token"]:
                _fail("method_original_source_changed")
            versions = await _history(db, witness.scope)
            if len(versions) >= MAX_VERSIONS:
                _fail("method_version_capacity_requires_review")
            version = str(uuid4())
            signed = TaskMethodScope(owner=owner, **witness.scope.model_dump(mode="json"), candidate_schema=typed["schema_version"],
                candidate_digest=candidate_sha, candidate_version=version, proposal_id=row.proposal_id,
                source_context_digest=row.source_context_digest, source_token_digest=digest(token),
                source_scope_digest=digest(witness.scope.model_dump(mode="json")), source_task_id=row.source_task_id,
                source_task_revision=row.source_task_revision, source_attempt_id=row.source_attempt_id,
                source_attempt_fence=row.source_attempt_fence, workflow_run_id=row.workflow_run_id,
                workflow_run_revision=row.workflow_run_revision, source_refs=json.loads(row.source_refs_json))
            row.memory_scope_json = canonical(signed.model_dump(mode="json"))
            row.memory_kind = MemoryKind.pattern
            row.decision_effect = MemoryProposalDecisionEffect.require_operator_confirmation
            source = _m5_verified_source_binding(row)
            if source is None:
                _fail("method_source_binding_invalid")
            provenance = {"schema_version": "work_board_memory_provenance.v1", "proposal_id": row.proposal_id,
                "owner_principal_id": owner_id, "owner_session_id": root_id, "source_context_digest": row.source_context_digest,
                "accepted_content_digest": candidate_sha, "memory_kind": "pattern", "memory_scope": signed.model_dump(mode="json"),
                "decision_effect": row.decision_effect.value, "lifecycle_state": "active", "verified_source_binding": source,
                "selection_binding_key_id": _m5_selection_binding_key_id(_signing_key=witness.key)}
            provenance["selection_binding_mac"] = _m5_selection_binding_mac(proposal_id=row.proposal_id,
                accepted_content_digest=candidate_sha, owner_principal_id=owner_id, owner_session_id=root_id,
                source_context_digest=row.source_context_digest, source_binding=source, decision_effect=row.decision_effect,
                memory_scope=signed.model_dump(mode="json"), _signing_key=witness.key)
            metadata = canonical({"work_board_provenance": provenance})
            if len(text.encode()) + len(metadata.encode()) + len(row.provenance_json.encode()) > MAX_BYTES:
                _fail("method_metadata_capacity_requires_review")
            memory = await memory_repository.create_m5_memory_in_session(db, content=text, kind=MemoryKind.pattern,
                source_session_id=root_id, scope_key=digest({"proposal_id": row.proposal_id, "scope": signed.model_dump(mode="json")}),
                metadata_json=metadata, confidence=0.5, proposal_id=row.proposal_id, _memory_id=version)
            selected = ActiveMethodBinding(owner=owner, scope=witness.scope, version=memory.id,
                digest=candidate_sha, proposal_id=row.proposal_id)
            if pointer is None:
                pointer = TaskMethodActive(owner_identity_id=owner.identity_id, goal_id=witness.scope.goal_id,
                    goal_revision=witness.scope.goal_revision, family=witness.scope.family, binding_json="null",
                    signature_key_id="", signature_mac="")
            else:
                pointer.revision += 1
            pointer.previous_binding_json = pointer.binding_json
            pointer.binding_json, pointer.baseline = selected.model_dump_json(), False
            row.preview_text, row.preview_text_digest = text, candidate_sha
            row.accepted_memory_id, row.accepted_memory_content_digest = memory.id, candidate_sha
            row.accepted_by_principal_id, row.accepted_by_session_id, row.accepted_at = owner_id, root_id, now
            row.acceptance_binding_digest, row.status = request_sha, MemoryProposalStatus.accepted
            row.reason_code = "task_method_accepted"
        elif request.action == "rollback":
            if row.status != MemoryProposalStatus.accepted or pointer is None or pointer.baseline:
                _fail("method_not_active")
            selected = ActiveMethodBinding.model_validate_json(pointer.binding_json)
            if selected.proposal_id != row.proposal_id or selected.version != row.accepted_memory_id:
                _fail("method_rollback_selection_changed")
            validator = CurrentMethod()
            validator._key = witness.key
            await validator._version(db, selected, owner.identity_id, witness.scope, allow_rollback=False)
            await memory_repository.rollback_m5_memory_in_session(db, memory_id=selected.version,
                expected_content_digest=selected.digest, expected_proposal_id=row.proposal_id,
                rollback_reason=request.reason.strip(), _signing_key=witness.key)
            row.status, row.reason_code = MemoryProposalStatus.rolled_back, "task_method_rolled_back"
            row.rollback_by_principal_id, row.rollback_by_session_id, row.rollback_at = owner_id, root_id, now
            row.rollback_reason = request.reason.strip()
            # Only future selection changes; the signed historical version is
            # still available to its already-admitted native pin validator.
            previous = json.loads(pointer.previous_binding_json)
            restored = None
            if previous is not None and typed["schema_version"] == "ProcedurePlan.v3":
                prior = ActiveMethodBinding.model_validate(previous)
                if prior != selected:
                    try:
                        await validator._version(db, prior, owner.identity_id, witness.scope, allow_rollback=False)
                        restored = prior
                    except BoardError:
                        # An unavailable exact prior pin falls back only to the
                        # signed family baseline, never a history search.
                        restored = None
            pointer.previous_binding_json = pointer.binding_json
            pointer.binding_json = restored.model_dump_json() if restored else "null"
            pointer.baseline = restored is None
            pointer.revision += 1
        elif request.action in {"disable", "activate", "delete"}:
            if row.status != MemoryProposalStatus.accepted or pointer is None:
                _fail("method_not_active")
            validator = CurrentMethod()
            validator._key = witness.key
            if request.action == "activate":
                if not pointer.baseline:
                    _fail("method_not_disabled")
                previous = ActiveMethodBinding.model_validate_json(pointer.previous_binding_json)
                if previous.proposal_id != row.proposal_id or previous.version != row.accepted_memory_id:
                    _fail("method_activation_selection_changed")
                await validator._version(db, previous, owner.identity_id, witness.scope, allow_rollback=False)
                pointer.binding_json, pointer.baseline = previous.model_dump_json(), False
                pointer.previous_binding_json = "null"
                pointer.revision += 1
            else:
                signed = TaskMethodScope.model_validate_json(row.memory_scope_json)
                selected = ActiveMethodBinding(owner=owner, scope=witness.scope,
                    version=row.accepted_memory_id, digest=signed.candidate_digest, proposal_id=row.proposal_id)
                await validator._version(db, selected, owner.identity_id, witness.scope, allow_rollback=False)
                if request.action == "delete":
                    await memory_repository.mark_memory_tombstoned_in_session(db, selected.version,
                        actor=owner_id, reason=request.reason.strip()[:255])
                current = None if pointer.baseline else ActiveMethodBinding.model_validate_json(pointer.binding_json)
                if request.action == "disable" and current != selected:
                    _fail("method_disable_selection_changed")
                if current == selected:
                    pointer.previous_binding_json = pointer.binding_json
                    pointer.binding_json, pointer.baseline = "null", True
                    pointer.revision += 1
        else:
            if row.status != MemoryProposalStatus.proposed:
                _fail("method_not_proposed")
            row.status, row.reason_code = MemoryProposalStatus.rejected, "task_method_rejected"
            row.rejected_by_principal_id, row.rejected_by_session_id, row.rejected_at = owner_id, root_id, now
        if pointer is not None:
            from src.memory.repository import _m5_selection_binding_key_id
            pointer.signature_key_id = _m5_selection_binding_key_id(_signing_key=witness.key)
            pointer.signature_mac, pointer.updated_at = _pointer_mac(pointer, witness.key), now
            db.add(pointer)
        row.revision += 1
        row.updated_at = now
        db.add(row)
        result = {"proposal_id": row.proposal_id, "status": row.status.value, "expected_revision": row.revision,
            "active_binding": json.loads(pointer.binding_json) if pointer and not pointer.baseline else None,
            "configured_baseline": bool(pointer and pointer.baseline), "behavior_changed": request.action != "reject",
            "quality_evidence": "unmeasured", "idempotent_replay": False}
        result["pointer_revision"] = pointer.revision if pointer else None
        db.add(WorkBoardEvent(task_id=row.source_task_id, owner_principal_id=owner_id, owner_session_id=root_id,
            actor_principal_id=owner_id, actor_session_id=root_id, kind=ACTION_KIND,
            mutation_idempotency_key=request.idempotency_key, mutation_request_digest=request_sha,
            metadata_json=canonical({"proposal_id": row.proposal_id, "action": request.action, "result": result})))
        from src.memory.m5 import _write_memory_action_audit
        await _write_memory_action_audit(db, owner_principal_id=owner_id, owner_session_id=root_id,
            proposal=row, action=request.action)
        await db.flush()
        return result
