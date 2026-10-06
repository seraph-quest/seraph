"""Provider-free opportunity recommendations and separately reviewed preferences.

Physical/native/key proof is staged before the supplied-session SQL writers.
This namespace never changes generic M5 or manual procedure populations.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Annotated, Any, Literal
from uuid import UUID, NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator
from sqlalchemy import select

from src.db import engine as db_engine
from src.db.models import (GuardianIntervention, GuardianOpportunity, Memory, MemoryKind,
    MemoryProposal, MemoryProposalStatus, MemoryProposalDecisionEffect,
    MemoryProposalProviderContactState, MemoryProposalPrivacyState, MemoryStatus,
    MemoryTombstone, WorkBoardEvent, WorkBoardTask)
from src.memory.procedure_recommendations import assert_current_root
from src.memory.repository import (_effect_mac_key, _m5_verified_source_binding,
    _m5_selection_binding_key_id, _m5_selection_binding_mac, _m5_selection_binding_matches,
    _canonical_memory_deletion_marker, memory_repository)
from src.work_board.contracts import WorkBoardOwner
from src.work_board.repository import BoardError, _begin_sqlite_immediate
from src.extensions.capability_execution import CapabilityJournalError

CAPABILITY_ID = "memory.opportunity-preference.v1"
PROPOSAL_SCHEMA = "opportunity_recommendation.v1"
SCOPE_SCHEMA = "guardian_opportunity_preference.v1"
MAX_BYTES = 65536
ACTION_KIND = "opportunity.preference_review.v1"
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Identifier = Annotated[str, Field(min_length=1, max_length=256)]
Blueprint = Literal["public-browser-check", "public-evidence-report"]
Action = Literal["prefer_blueprint", "suppress_watch"]


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def now() -> datetime:
    return datetime.now(timezone.utc)


async def _assert_original_root(db, operator):
    try:
        root = await assert_current_root(db, operator)
    except BoardError as exc:
        raise BoardError("original_root_unavailable", "The exact original Root is no longer current", status_code=403) from exc
    if now() >= min(utc(operator.idle_expires_at), utc(operator.absolute_expires_at)):
        raise BoardError("original_root_unavailable", "The original captured Root authority expired", status_code=403)
    return root


def parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() != timedelta(0):
        raise ValueError("explicit UTC time required")
    return result


def exact_uuid(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("canonical UUID required")
    return value


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class OpportunityRecommendationRequest(Closed):
    expected_opportunity_revision: int = Field(ge=1)
    expected_feedback_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=36, max_length=36)
    _uuid = field_validator("idempotency_key")(exact_uuid)


class OpportunityPreferenceInput(Closed):
    schema_version: Literal["seraph.opportunity.preference-input.v1"] = "seraph.opportunity.preference-input.v1"
    opportunity_id: Identifier
    expected_opportunity_revision: int = Field(ge=1)
    expected_feedback_revision: int = Field(ge=0)
    request_uuid: str = Field(min_length=36, max_length=36)
    generation_cutoff_at: str
    population_digest: Digest
    _uuid = field_validator("request_uuid")(exact_uuid)

    @field_validator("generation_cutoff_at")
    @classmethod
    def cutoff(cls, value):
        parse_time(value)
        return value


class RecommendationOutput(Closed):
    schema_version: Literal["seraph.opportunity.preference-output.v1"] = "seraph.opportunity.preference-output.v1"
    input_digest: Digest
    population_digest: Digest
    generation_cutoff_at: str
    status: Literal["candidate", "no_learning"]
    reason_code: str = Field(min_length=1, max_length=128)
    action: Action | None
    blueprint_id: Blueprint | None
    watch_id: Identifier | None
    watch_revision: int | None = Field(ge=1)

    @model_validator(mode="after")
    def action_scope(self):
        parse_time(self.generation_cutoff_at)
        if self.status == "no_learning":
            if any(x is not None for x in (self.action, self.blueprint_id, self.watch_id, self.watch_revision)):
                raise ValueError("no_learning cannot carry a preference")
        elif self.action == "prefer_blueprint":
            if self.blueprint_id is None or self.watch_id is not None or self.watch_revision is not None:
                raise ValueError("invalid blueprint scope")
        elif self.action == "suppress_watch":
            if self.blueprint_id is not None or self.watch_id is None or self.watch_revision is None:
                raise ValueError("invalid watch scope")
        else:
            raise ValueError("candidate requires an action")
        return self


class OpportunityPreferenceMember(Closed):
    opportunity_id: Identifier
    intervention_id: Identifier
    feedback_revision: int = Field(ge=1)
    feedback_event_id: str = Field(min_length=36, max_length=36)
    feedback_at: str
    feedback_binding_digest: Digest
    _uuid = field_validator("feedback_event_id")(exact_uuid)

    @field_validator("feedback_at")
    @classmethod
    def feedback_time(cls, value):
        parse_time(value)
        return value


class OpportunityPreferenceScope(Closed):
    schema_version: Literal["guardian_opportunity_preference.v1"] = SCOPE_SCHEMA
    owner_principal_id: Identifier
    owner_session_id: Identifier
    goal_id: Identifier
    goal_revision: int = Field(ge=1)
    action: Action
    blueprint_id: Blueprint | None
    watch_id: Identifier | None
    watch_revision: int | None = Field(ge=1)
    source_context_digest: Digest
    generation_cutoff_at: str
    window_days: Literal[30]
    population_count: int = Field(ge=2, le=100)
    feedback_event_count: int = Field(ge=2, le=100)
    population_members: tuple[OpportunityPreferenceMember, ...]
    population_digest: Digest
    bundle_digest: Digest

    @model_validator(mode="before")
    @classmethod
    def members_tuple(cls, value):
        if isinstance(value, dict) and isinstance(value.get("population_members"), list):
            value = {**value, "population_members": tuple(value["population_members"])}
        return value

    @model_validator(mode="after")
    def closed_scope(self):
        parse_time(self.generation_cutoff_at)
        ids = [m.opportunity_id for m in self.population_members]
        if ids != sorted(set(ids)) or len(ids) != self.population_count or self.feedback_event_count < self.population_count:
            raise ValueError("complete unique population required")
        if len({m.intervention_id for m in self.population_members}) != len(ids):
            raise ValueError("duplicate interventions")
        if self.action == "prefer_blueprint":
            if self.blueprint_id is None or self.watch_id is not None or self.watch_revision is not None:
                raise ValueError("invalid blueprint scope")
        elif self.blueprint_id is not None or self.watch_id is None or self.watch_revision is None:
            raise ValueError("invalid watch scope")
        if len(canonical(self.model_dump(mode="json"))) > MAX_BYTES:
            raise ValueError("scope exceeds finite byte bound")
        return self


class OpportunityPreferenceActionRequest(Closed):
    action: Literal["accept", "reject", "rollback"]
    expected_revision: int = Field(ge=1)
    expected_preview_text_digest: Digest
    expected_bundle_digest: Digest
    acknowledged_opportunity_preference_only: StrictBool
    mutation_uuid: str = Field(min_length=36, max_length=36)
    reason: str = Field(default="", max_length=500)
    _uuid = field_validator("mutation_uuid")(exact_uuid)

    @field_validator("acknowledged_opportunity_preference_only")
    @classmethod
    def acknowledgement(cls, value):
        if value is not True:
            raise ValueError("explicit opportunity-only acknowledgment required")
        return value


@dataclass(frozen=True)
class PopulationWitness:
    owner_principal_id: str
    original_root_id: str
    goal_id: str
    goal_revision: int
    opportunity_id: str
    opportunity_revision: int
    feedback_revision: int
    request_uuid: str
    generation_cutoff_at: datetime
    expires_at: datetime
    members: tuple[OpportunityPreferenceMember, ...]
    feedback_event_count: int
    population_digest: str
    inventory_bytes: bytes
    source_witnesses: tuple[Any, ...] = field(repr=False)
    anchor_witness: Any = field(repr=False)
    vote_bytes: bytes
    operator: Any = field(repr=False)

    def cpu_input(self) -> OpportunityPreferenceInput:
        return OpportunityPreferenceInput(opportunity_id=self.opportunity_id,
            expected_opportunity_revision=self.opportunity_revision,
            expected_feedback_revision=self.feedback_revision, request_uuid=self.request_uuid,
            generation_cutoff_at=self.generation_cutoff_at.isoformat(), population_digest=self.population_digest)


def calculate_recommendation(population: PopulationWitness, cpu_input: OpportunityPreferenceInput) -> RecommendationOutput:
    if cpu_input != population.cpu_input():
        raise BoardError("learning_population_incomplete", "The complete authorized population changed")
    votes = json.loads(population.vote_bytes)
    candidates = []
    for blueprint in ("public-browser-check", "public-evidence-report"):
        matching = [v for v in votes if v["blueprint_id"] == blueprint]
        if sum(v["feedback_type"] == "helpful" for v in matching) >= 2 and not any(v["feedback_type"] == "not_helpful" for v in matching):
            candidates.append(("prefer_blueprint", blueprint, None, None))
    for watch, revision in sorted({(v["watch_id"], v["watch_revision"]) for v in votes}):
        matching = [v for v in votes if (v["watch_id"], v["watch_revision"]) == (watch, revision)]
        if sum(v["feedback_type"] == "not_helpful" for v in matching) >= 2 and not any(v["feedback_type"] == "helpful" for v in matching):
            candidates.append(("suppress_watch", None, watch, revision))
    actionable = len(candidates) == 1 and len(population.members) >= 2
    action, blueprint, watch, revision = candidates[0] if actionable else (None, None, None, None)
    return RecommendationOutput(input_digest=digest(cpu_input.model_dump(mode="json")),
        population_digest=population.population_digest, generation_cutoff_at=cpu_input.generation_cutoff_at,
        status="candidate" if actionable else "no_learning",
        reason_code="opportunity_preference_candidate" if actionable else "opportunity_feedback_insufficient_or_conflicting",
        action=action, blueprint_id=blueprint, watch_id=watch, watch_revision=revision)


async def _inventory(db, *, owner, root, goal_id, goal_revision, cutoff):
    rows = list((await db.execute(select(GuardianIntervention).where(
        GuardianIntervention.intervention_type == "opportunity",
        GuardianIntervention.owner_principal_id == owner,
        GuardianIntervention.original_root_id == root,
        GuardianIntervention.goal_id == goal_id,
        GuardianIntervention.goal_revision == goal_revision,
        GuardianIntervention.feedback_revision > 0,
        GuardianIntervention.feedback_at >= cutoff - timedelta(days=30),
        GuardianIntervention.feedback_at <= cutoff,
    ).order_by(GuardianIntervention.feedback_at, GuardianIntervention.id).limit(101))).scalars().all())
    if len(rows) > 100:
        raise BoardError("learning_population_incomplete", "The complete opportunity population exceeds its cap")
    from src.guardian.feedback import parse_opportunity_feedback_history
    from src.work_board.pipelines import row_token
    tokens = []
    for row in rows:
        history = parse_opportunity_feedback_history(row)
        tokens.append({"id": row.id, "opportunity_id": row.opportunity_id,
            "row_token": row_token(row), "history_digest": history.history_digest})
    return rows, canonical(tokens)


async def stage_population(db, owner, *, anchor, request, cutoff_at, operator) -> PopulationWitness:
    """Stage the complete owner/Goal population and physical proof before writers."""
    from src.guardian.feedback import stage_opportunity_feedback_source
    root = await _assert_original_root(db, operator)
    cutoff_at = utc(cutoff_at)
    if cutoff_at > now() + timedelta(seconds=1):
        raise BoardError("learning_population_incomplete", "The generation cutoff cannot be in the future")
    anchor_witness = await stage_opportunity_feedback_source(db, owner, anchor.id, operator=operator)
    if (anchor.owner_principal_id != owner.principal_id or anchor.original_root_id != owner.session_id
        or anchor.revision != request.expected_opportunity_revision
        or anchor_witness.history.revision != request.expected_feedback_revision):
        raise BoardError("feedback_outcome_stale", "The exact opportunity and feedback revisions changed")
    rows, inventory = await _inventory(db, owner=owner.principal_id, root=owner.session_id,
        goal_id=anchor.goal_id, goal_revision=anchor.goal_revision, cutoff=cutoff_at)
    members, witnesses, votes = [], [], []
    event_count = 0
    for row in rows:
        witness = anchor_witness if row.id == anchor_witness.intervention_id else await stage_opportunity_feedback_source(
            db, owner, row.opportunity_id, operator=operator)
        history = witness.history
        if history.tip_bytes is None:
            raise BoardError("learning_population_incomplete", "An included tip is missing")
        tip = json.loads(history.tip_bytes)
        if (tip["outcome_binding_digest"] != witness.outcome_binding_digest
            or (tip["feedback_type"] == "helpful" and (not witness.outcomes or witness.blueprint_id is None))):
            raise BoardError("feedback_outcome_stale", "The explicit feedback binds a different or unverified outcome")
        event_count += history.event_count
        if event_count > 100:
            raise BoardError("learning_population_incomplete", "All included feedback history exceeds its cap")
        opportunity = await db.get(GuardianOpportunity, row.opportunity_id)
        binding = digest({"opportunity": witness.opportunity_token.decode(),
            "intervention": witness.intervention_token.decode(), "history": history.history_digest,
            "tip": tip, "outcome": witness.outcome_binding_digest})
        members.append(OpportunityPreferenceMember(opportunity_id=opportunity.id,
            intervention_id=row.id, feedback_revision=history.revision,
            feedback_event_id=tip["event_id"], feedback_at=tip["feedback_at"], feedback_binding_digest=binding))
        witnesses.append(witness)
        votes.append({"opportunity_id": opportunity.id, "feedback_type": tip["feedback_type"],
            "blueprint_id": witness.blueprint_id, "watch_id": opportunity.watch_id,
            "watch_revision": opportunity.watch_revision})
    members.sort(key=lambda m: m.opportunity_id)
    if len({m.opportunity_id for m in members}) != len(members):
        raise BoardError("learning_population_incomplete", "The population contains duplicate opportunities")
    population_digest = digest({"owner": owner.principal_id, "root": owner.session_id,
        "goal_id": anchor.goal_id, "goal_revision": anchor.goal_revision,
        "members": [m.model_dump(mode="json") for m in members], "feedback_event_count": event_count})
    return PopulationWitness(owner.principal_id, owner.session_id, anchor.goal_id, anchor.goal_revision,
        anchor.id, anchor.revision, anchor_witness.history.revision, request.idempotency_key, cutoff_at,
        min(cutoff_at + timedelta(minutes=5), utc(root.idle_expires_at), utc(root.absolute_expires_at),
            utc(operator.idle_expires_at), utc(operator.absolute_expires_at), utc(anchor.expires_at)),
        tuple(members), event_count, population_digest, inventory, tuple(witnesses), anchor_witness,
        canonical(sorted(votes, key=lambda v: v["opportunity_id"])), operator)


async def recheck_population(db, *, witness: PopulationWitness):
    """Re-enumerate canonical membership with no I/O or nested transaction."""
    from src.guardian.feedback import recheck_opportunity_feedback_source
    if not isinstance(witness, PopulationWitness):
        raise BoardError("learning_population_incomplete", "A staged complete population is required")
    await _assert_original_root(db, witness.operator)
    owner = WorkBoardOwner(principal_id=witness.owner_principal_id, session_id=witness.original_root_id)
    anchor, intervention = await recheck_opportunity_feedback_source(db, owner, witness=witness.anchor_witness)
    if anchor.revision != witness.opportunity_revision or intervention.feedback_revision != witness.feedback_revision:
        raise BoardError("feedback_outcome_stale", "The authorized anchor changed")
    _, inventory = await _inventory(db, owner=witness.owner_principal_id, root=witness.original_root_id,
        goal_id=witness.goal_id, goal_revision=witness.goal_revision, cutoff=max(witness.generation_cutoff_at, now()))
    if inventory != witness.inventory_bytes:
        raise BoardError("learning_population_incomplete", "The complete current feedback population changed")
    for member_witness in witness.source_witnesses:
        await recheck_opportunity_feedback_source(db, owner, witness=member_witness)
    return anchor, intervention


@dataclass(frozen=True)
class FinalizationWitness:
    operator: Any = field(repr=False)
    population: PopulationWitness
    native_source: Any = field(repr=False)
    generic_source: Any = field(repr=False)
    source_fragment_bytes: bytes
    output: RecommendationOutput
    bundle_digest: str
    specialized_context_digest: str | None
    scope: OpportunityPreferenceScope | None
    preview_text: str | None
    preview_text_digest: str | None
    provenance_bytes: bytes
    signing_key: bytes | None = field(repr=False)


async def stage_finalization(*, operator, task_id, attempt_id, job_id) -> FinalizationWitness:
    from src.work_board.opportunity_preference_native import stage_done_source
    from src.memory.m5 import _verified_source, _proof_digest, _source_refs, sanitize_m5_memory_text_async
    native = await stage_done_source(operator=operator, task_id=task_id, attempt_id=attempt_id, job_id=job_id)
    cpu_input = native.cpu_input
    output = RecommendationOutput.model_validate_json(native.output_bytes)
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    async with db_engine.get_session() as db:
        anchor = await db.get(GuardianOpportunity, cpu_input.opportunity_id)
        if anchor is None:
            raise BoardError("opportunity_not_proposed", "The original opportunity is unavailable")
        request = OpportunityRecommendationRequest(expected_opportunity_revision=cpu_input.expected_opportunity_revision,
            expected_feedback_revision=cpu_input.expected_feedback_revision, idempotency_key=cpu_input.request_uuid)
        population = await stage_population(db, owner, anchor=anchor, request=request,
            cutoff_at=parse_time(cpu_input.generation_cutoff_at), operator=operator)
        if cpu_input != population.cpu_input() or output != calculate_recommendation(population, cpu_input):
            raise BoardError("learning_population_incomplete", "The complete CPU input/result population changed")
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id,
            WorkBoardTask.owner_principal_id == owner.principal_id, WorkBoardTask.owner_session_id == owner.session_id))
        if task is None or task.capability_id != CAPABILITY_ID:
            raise BoardError("source_not_verified", "The real CPU recommendation source is unavailable")
        generic_source = await _verified_source(db, task, requested_attempt_id=attempt_id)
    fragment = {"generic_source_context_digest": generic_source.source_context_digest,
        "generic_evidence_digest": generic_source.evidence_digest, "task_intent_digest": generic_source.task_intent_digest,
        "task_id": task_id, "task_revision": generic_source.task.task_revision,
        "attempt_id": attempt_id, "attempt_fence": generic_source.attempt.fencing_token,
        "job_id": job_id, "job_revision": generic_source.run.revision,
        "input_digest": generic_source.task.typed_input_digest,
        "readback_digest": _proof_digest(generic_source.readback),
        "artifact_digest": native.output_sha256, "readback_refs": _source_refs(generic_source.readback)}
    bundle = digest({"input": cpu_input.model_dump(mode="json"), "output_digest": native.output_sha256,
        "output": output.model_dump(mode="json"), "source": fragment})
    context, scope, text, text_digest = None, None, None, None
    if output.status == "candidate":
        context = digest({"owner": owner.principal_id, "root": owner.session_id,
            "goal_id": population.goal_id, "goal_revision": population.goal_revision,
            "action": output.action, "blueprint_id": output.blueprint_id, "watch_id": output.watch_id,
            "watch_revision": output.watch_revision, "population_digest": population.population_digest,
            "bundle_digest": bundle, "source": fragment})
        scope = OpportunityPreferenceScope(owner_principal_id=owner.principal_id, owner_session_id=owner.session_id,
            goal_id=population.goal_id, goal_revision=population.goal_revision, action=output.action,
            blueprint_id=output.blueprint_id, watch_id=output.watch_id, watch_revision=output.watch_revision,
            source_context_digest=context, generation_cutoff_at=cpu_input.generation_cutoff_at, window_days=30,
            population_count=len(population.members), feedback_event_count=population.feedback_event_count,
            population_members=population.members, population_digest=population.population_digest, bundle_digest=bundle)
        text = (f"Prefer {output.blueprint_id} in eligible opportunity offers only." if output.action == "prefer_blueprint"
            else f"Suppress optional opportunities for watch {output.watch_id} revision {output.watch_revision} only.")
        safe = await sanitize_m5_memory_text_async(text)
        if safe != text:
            raise BoardError("opportunity_preview_changed", "Review text cannot be safely represented")
        text_digest = digest(text.encode())
    provenance = canonical({"schema_version": "opportunity_recommendation_source.v1",
        "source": fragment, "bundle_digest": bundle, "cpu_input": cpu_input.model_dump(mode="json")})
    if len(native.output_bytes) + len(provenance) + len(canonical(scope.model_dump(mode="json")) if scope else b"") > MAX_BYTES:
        raise BoardError("learning_population_incomplete", "The complete recommendation exceeds 64KiB")
    return FinalizationWitness(operator, population, native, generic_source, canonical(fragment), output,
        bundle, context, scope, text, text_digest, provenance, _effect_mac_key() if scope else None)


async def finalize_in_session(db, witness: FinalizationWitness) -> dict:
    from src.work_board.opportunity_preference_native import recheck_done_source
    from src.memory.m5 import _write_source_baseline, _proof_digest, _source_refs
    if not isinstance(witness, FinalizationWitness):
        raise BoardError("learning_population_incomplete", "A staged real CPU finalization is required")
    await recheck_population(db, witness=witness.population)
    source = await _fresh_generic_source(db, witness)
    proposal_id = str(uuid5(NAMESPACE_URL, f"seraph:opportunity-preference:{source.task.task_id}:{source.attempt.attempt_id}"))
    if witness.output.status == "no_learning":
        return {"status": "no_learning", "proposal_id": None, "bundle_digest": witness.bundle_digest,
            "population_digest": witness.population.population_digest, "memory_status": "no_learning",
            "reason_code": witness.output.reason_code}
    existing = await db.get(MemoryProposal, proposal_id)
    if existing:
        if (existing.schema_version != PROPOSAL_SCHEMA or existing.source_attempt_id != source.attempt.attempt_id
            or existing.source_context_digest != witness.specialized_context_digest
            or existing.evidence_digest != source.evidence_digest):
            raise BoardError("opportunity_request_conflict", "The existing recommendation binds different evidence")
        return {"status": "proposed", "proposal_id": proposal_id, "bundle_digest": witness.bundle_digest,
            "population_digest": witness.population.population_digest, "memory_status": "no_learning",
            "reason_code": existing.reason_code}
    if witness.population.expires_at <= now():
        raise BoardError("opportunity_preview_expired", "The original finite review window expired")
    row = MemoryProposal(proposal_id=proposal_id, schema_version=PROPOSAL_SCHEMA,
        owner_principal_id=witness.population.owner_principal_id, owner_session_id=witness.population.original_root_id,
        source_task_id=source.task.task_id, source_task_revision=source.task.task_revision,
        source_attempt_id=source.attempt.attempt_id, source_attempt_fence=source.attempt.fencing_token,
        workflow_run_id=source.attempt.workflow_run_id, workflow_run_revision=source.run.revision,
        goal_id=source.task.goal_id, goal_revision=source.task.goal_revision,
        capability_id=CAPABILITY_ID, capability_version=source.capability_version,
        typed_input_digest=source.task.typed_input_digest, source_context_digest=witness.specialized_context_digest,
        evidence_digest=source.evidence_digest, readback_kind=source.readback.get("kind", "verified_workflow_readback"),
        readback_ref=next(iter(_source_refs(source.readback)), None), readback_digest=_proof_digest(source.readback),
        artifact_ref=witness.native_source.artifact_id, artifact_digest=witness.native_source.output_sha256,
        proposal_job_id=source.run.run_identity, request_idempotency_key=witness.population.request_uuid,
        request_binding_digest=digest(witness.population.cpu_input().model_dump(mode="json")),
        memory_kind=MemoryKind.pattern, memory_scope_json=canonical(witness.scope.model_dump(mode="json")).decode(),
        preview_text=witness.preview_text, preview_text_digest=witness.preview_text_digest,
        decision_effect=MemoryProposalDecisionEffect.require_operator_confirmation, confidence=0.5,
        provenance_json=witness.provenance_bytes.decode(), source_refs_json=canonical(_source_refs(source.readback)).decode(),
        reason_code="opportunity_preference_proposed", recovery_action="none", status=MemoryProposalStatus.proposed,
        expires_at=witness.population.expires_at,
        provider_contact_state=MemoryProposalProviderContactState.not_started,
        privacy_state=MemoryProposalPrivacyState.visible)
    db.add(row)
    await db.flush()
    specialized_source = replace(source, source_context_digest=witness.specialized_context_digest)
    baseline = await _write_source_baseline(db, specialized_source, row, _signing_key=witness.signing_key)
    if baseline.receipt_integrity_mac is None:
        raise BoardError("source_baseline_integrity_unverifiable", "The exact source baseline could not be authenticated")
    return {"status": "proposed", "proposal_id": row.proposal_id, "bundle_digest": witness.bundle_digest,
        "population_digest": witness.population.population_digest, "memory_status": "no_learning",
        "reason_code": row.reason_code}


def proposal_projection(row, *, include_preview=True) -> dict:
    try:
        scope = OpportunityPreferenceScope.model_validate_json(row.memory_scope_json or "null").model_dump(mode="json")
    except (TypeError, ValueError):
        scope = None
    payload = {k: getattr(row, k) for k in ("proposal_id", "schema_version", "owner_principal_id", "owner_session_id",
        "source_task_id", "source_task_revision", "source_attempt_id", "source_attempt_fence", "workflow_run_id",
        "goal_id", "goal_revision", "preview_text_digest", "accepted_memory_id", "revision", "reason_code",
        "rollback_reason", "evidence_digest", "source_context_digest")}
    payload.update(status=getattr(row.status, "value", row.status), scope=scope,
        canonical_status=getattr(row.status, "value", row.status),
        rollback_available=False,
        preview_text=row.preview_text if include_preview else None,
        expires_at=utc(row.expires_at).isoformat() if row.expires_at else None,
        bundle_digest=scope["bundle_digest"] if scope else None,
        included_count=scope["population_count"] if scope else 0,
        feedback_event_count=scope["feedback_event_count"] if scope else 0,
        evidence_population="current_explicit_opportunity_feedback_only", quality_evidence="unmeasured",
        quality_disclosure="Opportunity feedback; usefulness improvement is unmeasured.",
        memory_status="no_learning",
        registered_capabilities=[], allowed_decision_effects=[])
    return payload


async def _owned_proposal(db, operator, proposal_id):
    await _assert_original_root(db, operator)
    row = await db.get(MemoryProposal, proposal_id, populate_existing=True)
    if (row is None or row.schema_version != PROPOSAL_SCHEMA
        or (row.owner_principal_id, row.owner_session_id) != (operator.principal.principal_id, operator.session_id)):
        raise BoardError("opportunity_proposal_owner_mismatch", "The review belongs to a different original Root", status_code=403)
    return row


async def _recheck_finalization(db, witness, row):
    await recheck_population(db, witness=witness.population)
    source = await _fresh_generic_source(db, witness)
    if (witness.scope is None or row.memory_scope_json != canonical(witness.scope.model_dump(mode="json")).decode()
        or row.source_context_digest != witness.specialized_context_digest
        or row.evidence_digest != source.evidence_digest
        or row.preview_text_digest != witness.preview_text_digest
        or row.source_task_id != witness.generic_source.task.task_id
        or row.source_attempt_id != witness.generic_source.attempt.attempt_id):
        raise BoardError("feedback_outcome_stale", "The exact reviewed CPU source or complete population changed")
    await _verify_baseline(db, row, witness.signing_key)


async def _fresh_generic_source(db, witness):
    """Pure SQL source verification consumes only the exact staged native proof."""
    from src.work_board.opportunity_preference_native import recheck_done_source
    from src.memory.m5 import _verified_source, _proof_digest
    task, attempt, _run = await recheck_done_source(db, witness=witness.native_source)
    db.info["opportunity_preference_done_source"] = witness.native_source
    source = await _verified_source(db, task, requested_attempt_id=attempt.attempt_id)
    if (source.evidence_digest != witness.generic_source.evidence_digest
        or source.source_context_digest != witness.generic_source.source_context_digest
        or source.task_intent_digest != witness.generic_source.task_intent_digest
        or _proof_digest(source.readback) != _proof_digest(witness.generic_source.readback)):
        raise BoardError("feedback_outcome_stale", "The literal verified CPU source evidence changed")
    return source


async def _verify_baseline(db, row, signing_key):
    from src.memory.repository import _m5_receipt_integrity_matches, _m5_receipt_binding_matches
    from src.db.models import WorkBoardDecisionReceipt, WorkBoardDecisionReceiptStage
    baselines = list((await db.execute(select(WorkBoardDecisionReceipt).where(
        WorkBoardDecisionReceipt.receipt_stage == WorkBoardDecisionReceiptStage.source_baseline,
        WorkBoardDecisionReceipt.source_proposal_id == row.proposal_id))).scalars().all())
    if len(baselines) != 1 or not _m5_receipt_binding_matches(baselines[0], row) or not _m5_receipt_integrity_matches(
        baselines[0], _signing_key=signing_key):
        raise BoardError("source_baseline_integrity_unverifiable", "The exact authenticated source baseline is required")


async def inspect_preference(operator, proposal_id) -> dict:
    async with db_engine.get_session() as db:
        row = await _owned_proposal(db, operator, proposal_id)
        result = proposal_projection(row)
        task_id, attempt_id, job_id = row.source_task_id, row.source_attempt_id, row.workflow_run_id
    if result["canonical_status"] == "accepted":
        try:
            signing_key = _effect_mac_key()
            async with db_engine.get_session() as db:
                row = await _owned_proposal(db, operator, proposal_id)
                await _verify_historical_adoption(db, row, signing_key)
                result["rollback_available"] = True
        except (BoardError, ValueError, OSError, CapabilityJournalError) as exc:
            result.update(status="blocked", reason_code=getattr(exc, "code", "accepted_memory_binding_unverifiable"),
                memory_status="no_learning", rollback_available=False)
    if result["status"] in {"proposed", "accepted"}:
        try:
            witness = await stage_finalization(operator=operator, task_id=task_id, attempt_id=attempt_id, job_id=job_id)
            async with db_engine.get_session() as db:
                row = await _owned_proposal(db, operator, proposal_id)
                await _recheck_finalization(db, witness, row)
                if row.status == MemoryProposalStatus.proposed and (row.expires_at is None or utc(row.expires_at) <= now()):
                    raise BoardError("opportunity_preview_expired", "The original review window expired")
                if row.status == MemoryProposalStatus.accepted:
                    await _verify_active_memory(db, row, witness.signing_key)
                rollback_available = result["rollback_available"]
                result = proposal_projection(row)
                result["rollback_available"] = rollback_available
                if row.status == MemoryProposalStatus.accepted:
                    result["memory_status"] = "accepted"
        except (BoardError, ValueError, OSError, CapabilityJournalError) as exc:
            result.update(status="blocked", reason_code=getattr(exc, "code", "feedback_outcome_stale"), memory_status="no_learning")
    return result


async def _verify_active_memory(db, row, signing_key):
    memory = await db.get(Memory, row.accepted_memory_id, populate_existing=True) if row.accepted_memory_id else None
    tombstone = await db.scalar(select(MemoryTombstone).where(MemoryTombstone.memory_id == row.accepted_memory_id))
    if (memory is None or memory.status != MemoryStatus.active or tombstone is not None
        or _canonical_memory_deletion_marker(memory) is not None
        or memory.source_session_id != row.owner_session_id
        or digest(memory.content.encode()) != row.accepted_memory_content_digest):
        raise BoardError("accepted_memory_binding_unverifiable", "The active canonical memory is unavailable")
    try:
        provenance = json.loads(memory.metadata_json or "{}").get("work_board_provenance", {})
        scope = OpportunityPreferenceScope.model_validate_json(row.memory_scope_json).model_dump(mode="json")
    except (TypeError, ValueError):
        raise BoardError("accepted_memory_binding_unverifiable", "The exact signed memory is unavailable")
    if not _m5_selection_binding_matches(provenance, proposal_id=row.proposal_id,
        accepted_content_digest=row.accepted_memory_content_digest, decision_effect=row.decision_effect,
        memory_scope=scope, source_binding=row, _signing_key=signing_key):
        raise BoardError("accepted_memory_binding_mismatch", "The signed memory source/scope changed")
    return memory


async def _verify_historical_adoption(db, row, signing_key):
    """Removal authority uses authenticated adoption, not newly eligible feedback."""
    if row.status != MemoryProposalStatus.accepted:
        raise BoardError("opportunity_preference_not_accepted", "Only an adopted preference can be rolled back")
    memory = await _verify_active_memory(db, row, signing_key)
    await _verify_baseline(db, row, signing_key)
    return memory


def _proposal_token(row):
    return digest({k: getattr(row, k) for k in ("memory_scope_json", "provenance_json", "preview_text",
        "preview_text_digest", "evidence_digest", "source_context_digest", "artifact_ref", "artifact_digest",
        "source_task_id", "source_task_revision", "source_attempt_id", "source_attempt_fence", "workflow_run_id",
        "workflow_run_revision", "typed_input_digest", "request_binding_digest", "accepted_memory_id",
        "accepted_memory_content_digest", "acceptance_binding_digest")})


async def apply_preference_action(operator, proposal_id, request: OpportunityPreferenceActionRequest):
    owner, root = operator.principal.principal_id, operator.session_id
    binding = digest({"owner": owner, "root": root, "proposal_id": proposal_id, **request.model_dump()})
    async with db_engine.get_session() as db:
        row = await _owned_proposal(db, operator, proposal_id)
        replay = await db.scalar(select(WorkBoardEvent).where(WorkBoardEvent.owner_principal_id == owner,
            WorkBoardEvent.owner_session_id == root, WorkBoardEvent.mutation_idempotency_key == request.mutation_uuid))
        if replay is not None:
            if replay.kind != ACTION_KIND or replay.mutation_request_digest != binding:
                raise BoardError("opportunity_request_conflict", "The request UUID binds a different action")
            return {**proposal_projection(row), "idempotent_replay": True, "audit_event_id": replay.event_id}
        token = _proposal_token(row)
        task_id, attempt_id, job_id = row.source_task_id, row.source_attempt_id, row.workflow_run_id
        text = row.preview_text
        scope = OpportunityPreferenceScope.model_validate_json(row.memory_scope_json).model_dump(mode="json")
    signing_key = _effect_mac_key()
    if request.action == "rollback" and not request.reason.strip():
        raise BoardError("opportunity_rollback_reason_required", "A rollback reason is required")
    witness = None
    if request.action == "accept":
        witness = await stage_finalization(operator=operator, task_id=task_id, attempt_id=attempt_id, job_id=job_id)
        signing_key = witness.signing_key
        if text != witness.preview_text:
            raise BoardError("opportunity_preview_changed", "Review the exact current preview")
    async with db_engine.get_session() as db:
        await _begin_sqlite_immediate(db)
        row = await _owned_proposal(db, operator, proposal_id)
        replay = await db.scalar(select(WorkBoardEvent).where(WorkBoardEvent.owner_principal_id == owner,
            WorkBoardEvent.owner_session_id == root, WorkBoardEvent.mutation_idempotency_key == request.mutation_uuid))
        if replay is not None:
            if replay.kind != ACTION_KIND or replay.mutation_request_digest != binding:
                raise BoardError("opportunity_request_conflict", "The request UUID binds a different action")
            return {**proposal_projection(row), "idempotent_replay": True, "audit_event_id": replay.event_id}
        if (row.revision != request.expected_revision or _proposal_token(row) != token
            or row.preview_text_digest != request.expected_preview_text_digest
            or scope["bundle_digest"] != request.expected_bundle_digest):
            raise BoardError("opportunity_review_stale", "The exact review changed")
        if request.action == "accept":
            if row.status != MemoryProposalStatus.proposed or row.expires_at is None or utc(row.expires_at) <= now():
                raise BoardError("opportunity_preview_expired", "The original preview expired")
            await _recheck_finalization(db, witness, row)
            conflicts = list((await db.execute(select(MemoryProposal).where(
                MemoryProposal.schema_version == PROPOSAL_SCHEMA, MemoryProposal.owner_principal_id == owner,
                MemoryProposal.owner_session_id == root, MemoryProposal.goal_id == row.goal_id,
                MemoryProposal.goal_revision == row.goal_revision, MemoryProposal.status == MemoryProposalStatus.accepted,
            ).limit(101))).scalars().all())
            if len(conflicts) > 100:
                raise BoardError("learning_population_incomplete", "Preference history exceeds its bound")
            for other in conflicts:
                other_scope = OpportunityPreferenceScope.model_validate_json(other.memory_scope_json)
                if (other_scope.action == scope["action"] and
                    (scope["action"] == "prefer_blueprint" or (other_scope.watch_id, other_scope.watch_revision) ==
                        (scope["watch_id"], scope["watch_revision"]))):
                    raise BoardError("opportunity_preference_conflict", "Rollback the existing preference before adopting another")
            source = _m5_verified_source_binding(row)
            if source is None:
                raise BoardError("source_baseline_binding_mismatch", "The real CPU source binding is unavailable")
            provenance = {"schema_version": "work_board_memory_provenance.v1", "proposal_id": proposal_id,
                "owner_principal_id": owner, "owner_session_id": root, "source_context_digest": row.source_context_digest,
                "accepted_content_digest": row.preview_text_digest, "memory_kind": "pattern", "memory_scope": scope,
                "decision_effect": row.decision_effect.value, "lifecycle_state": "active", "verified_source_binding": source,
                "selection_binding_key_id": _m5_selection_binding_key_id(_signing_key=signing_key)}
            provenance["selection_binding_mac"] = _m5_selection_binding_mac(proposal_id=proposal_id,
                accepted_content_digest=row.preview_text_digest, owner_principal_id=owner, owner_session_id=root,
                source_context_digest=row.source_context_digest, source_binding=source, decision_effect=row.decision_effect,
                memory_scope=scope, _signing_key=signing_key)
            metadata = canonical({"work_board_provenance": provenance})
            if len(metadata) + len(row.provenance_json.encode()) + len(row.memory_scope_json.encode()) + len(witness.native_source.output_bytes) > MAX_BYTES:
                raise BoardError("learning_population_incomplete", "Complete signed metadata exceeds 64KiB")
            memory = await memory_repository.create_m5_memory_in_session(db, content=witness.preview_text,
                kind=MemoryKind.pattern, source_session_id=root, scope_key=digest({"scope": scope, "proposal_id": proposal_id}),
                metadata_json=metadata.decode(), confidence=0.5, proposal_id=proposal_id)
            row.accepted_memory_id, row.accepted_memory_content_digest = memory.id, row.preview_text_digest
            row.accepted_by_principal_id, row.accepted_by_session_id, row.accepted_at = owner, root, now()
            row.acceptance_binding_digest, row.status = binding, MemoryProposalStatus.accepted
            row.reason_code = "opportunity_preference_accepted"
        elif request.action == "rollback":
            if row.status != MemoryProposalStatus.accepted or not row.accepted_memory_id:
                raise BoardError("opportunity_preference_not_accepted", "Only an adopted preference can be rolled back")
            await _verify_historical_adoption(db, row, signing_key)
            await memory_repository.rollback_m5_memory_in_session(db, memory_id=row.accepted_memory_id,
                expected_content_digest=row.accepted_memory_content_digest, expected_proposal_id=proposal_id,
                rollback_reason=request.reason.strip(), _signing_key=signing_key)
            row.status, row.reason_code = MemoryProposalStatus.rolled_back, "opportunity_preference_rolled_back"
            row.rollback_by_principal_id, row.rollback_by_session_id, row.rollback_at = owner, root, now()
            row.rollback_reason = request.reason.strip()
        else:
            if row.status != MemoryProposalStatus.proposed:
                raise BoardError("opportunity_preference_not_proposed", "Only a proposed preference can be rejected")
            row.status, row.reason_code = MemoryProposalStatus.rejected, "opportunity_preference_rejected"
            row.rejected_by_principal_id, row.rejected_by_session_id, row.rejected_at = owner, root, now()
        row.revision += 1
        row.updated_at = now()
        db.add(row)
        event = WorkBoardEvent(owner_principal_id=owner, owner_session_id=root, actor_principal_id=owner,
            actor_session_id=root, task_id=row.source_task_id, kind=ACTION_KIND,
            mutation_idempotency_key=request.mutation_uuid, mutation_request_digest=binding,
            metadata_json=canonical({"proposal_id": proposal_id, "action": request.action,
                "bundle_digest": scope["bundle_digest"], "proposal_revision": row.revision}).decode())
        db.add(event)
        from src.memory.m5 import _write_memory_action_audit
        await _write_memory_action_audit(db, owner_principal_id=owner, owner_session_id=root,
            proposal=row, action=request.action)
        await db.flush()
        result = proposal_projection(row)
        if row.status == MemoryProposalStatus.accepted:
            await _verify_historical_adoption(db, row, signing_key)
            result["rollback_available"] = True
            result["memory_status"] = "accepted"
        return {**result, "idempotent_replay": False, "audit_event_id": event.event_id}


@dataclass(frozen=True)
class PreferenceUseWitness:
    proposal_id: str
    proposal_revision: int
    proposal_token: str
    memory_id: str
    memory_token: str
    finalization: FinalizationWitness = field(repr=False)


async def stage_preference_use(db, *, owner, goal_id, goal_revision, action="suppress_watch",
    watch_id=None, watch_revision=None, operator=None) -> PreferenceUseWitness | None:
    """Stage only an accepted exact preference; background uses sealed CPU source custody."""
    from src.work_board.pipelines import row_token
    rows = list((await db.execute(select(MemoryProposal).where(MemoryProposal.schema_version == PROPOSAL_SCHEMA,
            MemoryProposal.owner_principal_id == owner.principal_id, MemoryProposal.owner_session_id == owner.session_id,
            MemoryProposal.goal_id == goal_id, MemoryProposal.goal_revision == goal_revision,
            MemoryProposal.status == MemoryProposalStatus.accepted).order_by(MemoryProposal.proposal_id).limit(101))).scalars().all())
    if len(rows) > 100:
        raise BoardError("learning_population_incomplete", "Preference history exceeds its bound")
    candidates = []
    for row in rows:
        scope = OpportunityPreferenceScope.model_validate_json(row.memory_scope_json)
        if scope.action == action and (action != "suppress_watch" or
            (scope.watch_id, scope.watch_revision) == (watch_id, watch_revision)):
            candidates.append(row)
    if not candidates:
        return None
    if len(candidates) != 1:
        raise BoardError("opportunity_preference_conflict", "Multiple accepted preferences cannot have an active effect")
    row = candidates[0]
    if operator is None:
        from src.work_board.opportunity_preference_native import stage_request_operator
        operator = await stage_request_operator(task_id=row.source_task_id)
    if (operator.principal.principal_id, operator.session_id) != (owner.principal_id, owner.session_id):
        raise BoardError("opportunity_proposal_owner_mismatch", "The exact original Root custody is required", status_code=403)
    finalization = await stage_finalization(operator=operator, task_id=row.source_task_id,
        attempt_id=row.source_attempt_id, job_id=row.workflow_run_id)
    await _recheck_finalization(db, finalization, row)
    memory = await _verify_active_memory(db, row, finalization.signing_key)
    return PreferenceUseWitness(row.proposal_id, row.revision, _proposal_token(row), memory.id,
        row_token(memory), finalization)


async def recheck_preference_use(db, *, witness: PreferenceUseWitness):
    from src.work_board.pipelines import row_token
    if not isinstance(witness, PreferenceUseWitness):
        raise BoardError("accepted_memory_binding_unverifiable", "A staged signed current preference is required")
    row = await _owned_proposal(db, witness.finalization.operator, witness.proposal_id)
    if row.status != MemoryProposalStatus.accepted or row.revision != witness.proposal_revision or _proposal_token(row) != witness.proposal_token:
        raise BoardError("opportunity_review_stale", "The accepted preference changed after staging")
    await _recheck_finalization(db, witness.finalization, row)
    memory = await _verify_active_memory(db, row, witness.finalization.signing_key)
    if memory.id != witness.memory_id or row_token(memory) != witness.memory_token:
        raise BoardError("accepted_memory_binding_mismatch", "The signed canonical memory changed after staging")
    return {"status": "active", "reason_code": "opportunity_preference_current", "memory_status": "accepted",
        "scope": witness.finalization.scope.model_dump(mode="json"), "proposal_id": row.proposal_id}


async def current_preference(operator, *, goal_id, goal_revision, action, watch_id=None, watch_revision=None):
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    try:
        async with db_engine.get_session() as db:
            await _assert_original_root(db, operator)
            witness = await stage_preference_use(db, owner=owner, goal_id=goal_id, goal_revision=goal_revision,
                action=action, watch_id=watch_id, watch_revision=watch_revision, operator=operator)
        if witness is None:
            return {"status": "none", "reason_code": "opportunity_preference_none", "memory_status": "no_learning"}
        async with db_engine.get_session() as db:
            return await recheck_preference_use(db, witness=witness)
    except (BoardError, ValueError, OSError, CapabilityJournalError) as exc:
        return {"status": "blocked", "reason_code": getattr(exc, "code", "feedback_outcome_stale"), "memory_status": "no_learning"}


def order_eligible_offers(offers, preference):
    """Display only; never choose or authorize a blueprint."""
    preferred = (preference.get("scope") or {}).get("blueprint_id") if preference.get("status") == "active" else None
    return sorted(offers, key=lambda offer: (offer["blueprint_id"] != preferred, offer["blueprint_id"]))


def suppress_optional_opportunity(preference, *, optional, security_or_recovery=False):
    return bool(optional and not security_or_recovery and preference.get("status") == "active"
        and (preference.get("scope") or {}).get("action") == "suppress_watch")
