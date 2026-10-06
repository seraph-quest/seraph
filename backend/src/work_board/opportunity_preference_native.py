"""Fixed provider-free opportunity recommendation behind existing Work execution."""
from __future__ import annotations

from dataclasses import dataclass, field
import json

from src.work_board.repository import BoardError, SafeTaskText, _payload_digest
from src.work_board.input_artifacts import InputArtifactWitness

CAPABILITY = "memory.opportunity-preference.v1"
REQUEST_SCOPE = "opportunity-recommendation"
MAX_SECONDS = 30
MAX_ATTEMPTS = 1
MAX_BYTES = 65536


@dataclass(frozen=True)
class PublicationWitness:
    owner_principal_id: str
    original_root_id: str
    request_digest: str
    population: object
    staged_text: SafeTaskText
    staged_input: InputArtifactWitness


async def recheck_publication(db, owner, request, *, witness):
    """Only a canonical current server request may publish the fixed Todo."""
    from src.guardian.opportunity_preferences import PopulationWitness, recheck_population
    if (not isinstance(witness, PublicationWitness)
        or not isinstance(witness.population, PopulationWitness)
        or not isinstance(witness.staged_text, SafeTaskText)
        or not isinstance(witness.staged_input, InputArtifactWitness)
        or witness.owner_principal_id != owner.principal_id
        or witness.original_root_id != owner.session_id
        or witness.request_digest != _payload_digest(request)
        or request.capability_id != CAPABILITY
        or request.status.value != "todo" or request.requires_review
        or request.idempotency_scope != REQUEST_SCOPE
        or request.idempotency_key != f"{witness.population.opportunity_id}:{witness.population.request_uuid}"
        or request.goal_id != witness.population.goal_id
        or request.goal_revision != witness.population.goal_revision
        or request.input_artifact_id != witness.staged_input.artifact_id
        or json.loads(witness.staged_input.input_bytes) != witness.population.cpu_input().model_dump(mode="json")):
        raise BoardError("opportunity_recommendation_system_only", "Use the authenticated opportunity recommendation", status_code=409)
    if min(utc(witness.population.operator.idle_expires_at),utc(witness.population.operator.absolute_expires_at)) <= now():
        raise BoardError("opportunity_recommendation_root_stale", "The original HTTP Root bounds have expired")
    await recheck_population(db, witness=witness.population)

from datetime import datetime, timedelta, timezone
from dataclasses import replace
import hashlib
import hmac
from typing import Any, Mapping

from sqlalchemy import select
from src.db import engine as db_engine
from src.db.models import (WorkBoardTask, WorkBoardAttempt, WorkflowRunState,
    WorkBoardInputArtifact, WorkBoardEvent, OperatorSession, Goal, GuardianOpportunity, WorkBoardStatus)
from src.work_board.contracts import WorkBoardOwner
from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec

REQUESTED_EVENT = "opportunity.recommendation.requested.v1"
FINALIZED_EVENT = "opportunity.recommendation.finalized.v1"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def now():
    return datetime.now(timezone.utc)


def row_token(row):
    return canonical(row.model_dump(mode="json"))


def job_id(task, attempt):
    return f"opportunity-preference:{task.task_id}:{attempt.attempt_id}"


def spec_for(task, attempt, inputs, *, deadline):
    from src.guardian.opportunity_preferences import OpportunityPreferenceInput
    model = OpportunityPreferenceInput.model_validate(dict(inputs))
    if (task.capability_id != CAPABILITY or task.requires_review
        or task.idempotency_scope != REQUEST_SCOPE
        or task.idempotency_key != f"{model.opportunity_id}:{model.request_uuid}"
        or attempt.task_id != task.task_id or attempt.parent_handoff_digest
        or attempt.parent_handoff_context_json not in {None, "[]"}):
        raise BoardError("opportunity_recommendation_binding_invalid", "The original recommendation task binding changed")
    safe_inputs = {"input": model.model_dump(mode="json"), "task_id": task.task_id,
        "attempt_id": attempt.attempt_id, "input_artifact_id": task.input_artifact_id,
        "input_artifact_digest": task.typed_input_digest}
    authority = {"principal": task.owner_principal_id, "owner_kind":"user",
        "session_id":task.owner_session_id,"goal_id":task.goal_id,"goal_revision":task.goal_revision,
        "capability_id":CAPABILITY,"capability_version":"1","finite_authority":True,
        "permissions":["workspace_read","workspace_write"],
        "limits":{"max_seconds":30,"max_output_bytes":MAX_BYTES,"max_attempts":1},
        "input_binding":model.model_dump(mode="json"),"task_id":task.task_id,
        "attempt_id":attempt.attempt_id,"input_artifact_id":task.input_artifact_id,
        "input_artifact_digest":task.typed_input_digest,
        "publication_digest":task.idempotency_payload_digest,"no_learning":True}
    fingerprint = digest({"inputs":safe_inputs,"authority":authority})
    return DurableJobSpec(identity=DurableJobIdentity(job_id=job_id(task,attempt),owner_kind="user",
        owner_principal_id=task.owner_principal_id,job_kind=CAPABILITY,capability_version="1",
        idempotency_scope="work-board-attempt",idempotency_key=f"{task.task_id}:{attempt.attempt_id}"),
        inputs=safe_inputs,session_id=task.owner_session_id,operator_session_id=task.owner_session_id,
        goal_id=task.goal_id,goal_revision=task.goal_revision,priority=task.priority,
        declared_authority=authority,deadline_at=deadline,max_attempts=1,max_outstanding_jobs=1,
        run_fingerprint=fingerprint)


def binds(task, attempt, run):
    try:
        authority = json.loads(run.declared_authority_json)
        spec = spec_for(task,attempt,authority["input_binding"],deadline=utc(run.deadline_at))
        return (run.run_identity == spec.identity.job_id and run.owner_kind == "user"
            and run.owner_principal_id == task.owner_principal_id and not run.service_id
            and run.session_id == run.operator_session_id == task.owner_session_id
            and run.goal_id == task.goal_id and run.goal_revision == task.goal_revision
            and run.job_kind == CAPABILITY and run.capability_version == "1"
            and run.idempotency_scope == "work-board-attempt"
            and run.idempotency_key == spec.identity.idempotency_key
            and run.max_attempts == 1 and run.attempt_count <= 1
            and run.input_digest == digest(spec.inputs) and run.run_fingerprint == spec.run_fingerprint
            and authority == spec.declared_authority)
    except (ValueError,TypeError,KeyError,BoardError):
        return False


async def _task(db, task_id):
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id).execution_options(populate_existing=True))
    if task is None:
        raise BoardError("opportunity_recommendation_unavailable", "The original recommendation is unavailable", status_code=404)
    return task


async def _request_receipt(db, task):
    rows = (await db.execute(select(WorkBoardEvent).where(WorkBoardEvent.task_id == task.task_id,
        WorkBoardEvent.kind == REQUESTED_EVENT, WorkBoardEvent.owner_principal_id == task.owner_principal_id,
        WorkBoardEvent.owner_session_id == task.owner_session_id).order_by(WorkBoardEvent.event_id).limit(2))).scalars().all()
    if len(rows) != 1:
        raise BoardError("opportunity_recommendation_binding_invalid", "The original sealed request is required")
    return rows[0], _parse_request_metadata(rows[0].metadata_json)


def _parse_request_metadata(raw):
    """Closed original publication contract, including malformed signed values."""
    from uuid import UUID
    import re

    fields = {"schema_version", "request_uuid", "request_body_digest", "owner_principal_id",
        "original_root_id", "goal_id", "goal_revision", "task_id", "task_create_digest",
        "input_artifact_id", "input_sha256", "cpu_input_digest", "population_digest",
        "generation_cutoff_at", "original_idle_expires_at", "original_absolute_expires_at",
        "execution_deadline", "preview_expires_at", "key_id", "authorization_mac"}

    def pairs(items):
        result = {}
        for name, value in items:
            if name in result:
                raise ValueError("duplicate field")
            result[name] = value
        return result

    try:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 8192:
            raise ValueError("metadata size")
        value = json.loads(raw, object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("nonfinite value")))
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("metadata shape")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("version")
        if type(value["goal_revision"]) is not int or value["goal_revision"] < 1:
            raise ValueError("revision")
        if not isinstance(value["request_uuid"], str) or str(UUID(value["request_uuid"])) != value["request_uuid"]:
            raise ValueError("request UUID")
        for name in ("owner_principal_id", "original_root_id", "goal_id", "task_id", "input_artifact_id"):
            item = value[name]
            if not isinstance(item, str) or not item or len(item) > 256 or any(ord(c) < 32 for c in item):
                raise ValueError("identity")
        for name in ("request_body_digest", "task_create_digest", "input_sha256", "cpu_input_digest",
            "population_digest", "authorization_mac", "key_id"):
            length = 24 if name == "key_id" else 64
            if not isinstance(value[name], str) or re.fullmatch(f"[0-9a-f]{{{length}}}", value[name]) is None:
                raise ValueError("digest")
        timestamps = {}
        for name in ("generation_cutoff_at", "original_idle_expires_at", "original_absolute_expires_at",
            "execution_deadline", "preview_expires_at"):
            item = value[name]
            if not isinstance(item, str) or len(item) > 64:
                raise ValueError("timestamp")
            stamp = datetime.fromisoformat(item)
            if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
                raise ValueError("UTC timestamp")
            timestamps[name] = stamp
        cutoff = timestamps["generation_cutoff_at"]
        root_end = min(timestamps["original_idle_expires_at"], timestamps["original_absolute_expires_at"])
        if (not cutoff < timestamps["execution_deadline"] <= min(cutoff + timedelta(seconds=30), root_end)
            or not cutoff < timestamps["preview_expires_at"] <= min(cutoff + timedelta(minutes=5), root_end)):
            raise ValueError("original finite bounds")
        return value
    except (ValueError, TypeError, UnicodeError, OverflowError) as exc:
        raise BoardError("opportunity_recommendation_binding_invalid", "The original sealed request changed") from exc


def _authorization_mac(metadata, token_hash, key):
    return hmac.new(key,canonical({"request":{k:v for k,v in metadata.items() if k != "authorization_mac"},
        "original_root_token_hash":token_hash}),hashlib.sha256).hexdigest()


async def stage_request_operator(*, task_id, session_provider=None):
    """Restore only an originally sealed fixed-capability server request context."""
    from src.auth.service import authenticate_session
    from src.memory.repository import _effect_mac_key, _m5_selection_binding_key_id
    provider = session_provider or db_engine.get_session
    key = _effect_mac_key()
    async with provider() as db:
        task = await _task(db,task_id)
        _event,metadata = await _request_receipt(db,task)
        root = await db.get(OperatorSession,task.owner_session_id,populate_existing=True)
        artifact = await db.get(WorkBoardInputArtifact,task.input_artifact_id,populate_existing=True)
        goal = await db.get(Goal,task.goal_id,populate_existing=True)
        if (task.capability_id != CAPABILITY or task.idempotency_scope != REQUEST_SCOPE
            or root is None or artifact is None or goal is None
            or root.principal_id != task.owner_principal_id or not root.token_hash
            or root.revoked_at is not None or root.replaced_by_id is not None or root.is_bearer_tombstone
            or metadata.get("key_id") != _m5_selection_binding_key_id(_signing_key=key)
            or not hmac.compare_digest(str(metadata.get("authorization_mac") or ""), _authorization_mac(metadata,root.token_hash,key))
            or metadata.get("task_id") != task.task_id or metadata.get("task_create_digest") != task.idempotency_payload_digest
            or metadata.get("input_artifact_id") != task.input_artifact_id
            or metadata.get("input_sha256") != task.typed_input_digest
            or metadata.get("owner_principal_id") != task.owner_principal_id
            or metadata.get("original_root_id") != task.owner_session_id
            or metadata.get("goal_id") != task.goal_id or metadata.get("goal_revision") != task.goal_revision
            or (artifact.bound_task_id,artifact.owner_principal_id,artifact.owner_session_id,artifact.goal_id,artifact.goal_revision,artifact.capability_id,artifact.payload_sha256)
                != (task.task_id,task.owner_principal_id,task.owner_session_id,task.goal_id,task.goal_revision,CAPABILITY,task.typed_input_digest)
            or goal.owner_principal_id != task.owner_principal_id or goal.owner_session_id != task.owner_session_id
            or goal.revision != task.goal_revision or str(goal.status) != "active"):
            raise BoardError("opportunity_recommendation_binding_invalid", "The original sealed request authority changed")
        idle = min(utc(root.idle_expires_at),utc(datetime.fromisoformat(metadata["original_idle_expires_at"])))
        absolute = min(utc(root.absolute_expires_at),utc(datetime.fromisoformat(metadata["original_absolute_expires_at"])))
        if min(idle,absolute) <= now():
            raise BoardError("opportunity_recommendation_root_stale", "The original finite Root has expired")
        token_hash = root.token_hash
        root_id = root.id
    operator = await authenticate_session(root_id,touch=False)
    return replace(operator,_token_hash=token_hash,idle_expires_at=idle,absolute_expires_at=absolute)


@dataclass(frozen=True)
class TaskAuthorityWitness:
    population: object
    operator: object
    task_id: str
    task_token: bytes
    attempt_id: str | None
    attempt_token: bytes | None
    input_artifact_id: str
    input_token: bytes
    input_bytes: bytes
    cpu_input: object
    request_event_id: int
    request_event_token: bytes
    execution_deadline: datetime
    signing_key: bytes = field(repr=False)


async def stage_task_authority(db, task, *, attempt=None, session_provider=None):
    from src.guardian.opportunity_preferences import OpportunityPreferenceInput, OpportunityRecommendationRequest, stage_population
    from src.work_board.dispatcher import _parse_typed_input
    from src.memory.repository import _effect_mac_key
    signing_key = _effect_mac_key()
    operator = await stage_request_operator(task_id=task.task_id,session_provider=session_provider)
    cpu_input = OpportunityPreferenceInput.model_validate(_parse_typed_input(task))
    request = OpportunityRecommendationRequest(expected_opportunity_revision=cpu_input.expected_opportunity_revision,
        expected_feedback_revision=cpu_input.expected_feedback_revision,idempotency_key=cpu_input.request_uuid)
    current = await _task(db,task.task_id)
    event,metadata = await _request_receipt(db,current)
    artifact = await db.get(WorkBoardInputArtifact,current.input_artifact_id,populate_existing=True)
    anchor = await db.get(GuardianOpportunity,cpu_input.opportunity_id,populate_existing=True)
    if anchor is None or metadata.get("cpu_input_digest") != digest(cpu_input.model_dump(mode="json")):
        raise BoardError("opportunity_recommendation_binding_invalid", "The original request input changed")
    owner = WorkBoardOwner(principal_id=current.owner_principal_id,session_id=current.owner_session_id)
    population = await stage_population(db,owner,anchor=anchor,request=request,
        cutoff_at=datetime.fromisoformat(cpu_input.generation_cutoff_at),operator=operator)
    if cpu_input != population.cpu_input():
        raise BoardError("learning_population_incomplete", "The current eligible population changed")
    if attempt is not None:
        attempt = await db.get(WorkBoardAttempt,attempt.attempt_id,populate_existing=True)
    return TaskAuthorityWitness(population,operator,current.task_id,row_token(current),
        attempt.attempt_id if attempt else None,row_token(attempt) if attempt else None,
        artifact.artifact_id,row_token(artifact),canonical({"schema_version":1,"capability_id":CAPABILITY,
            "input":cpu_input.model_dump(mode="json")}),cpu_input,event.event_id,row_token(event),
        utc(datetime.fromisoformat(metadata["execution_deadline"])),signing_key)


async def recheck_task_authority(db, *, witness, execution=False):
    from src.guardian.opportunity_preferences import recheck_population
    if not isinstance(witness,TaskAuthorityWitness):
        raise BoardError("opportunity_recommendation_binding_invalid", "A staged original request is required")
    if execution and witness.execution_deadline <= now():
        raise BoardError("opportunity_recommendation_expired", "The original CPU execution deadline has expired")
    task = await _task(db,witness.task_id)
    artifact = await db.get(WorkBoardInputArtifact,witness.input_artifact_id,populate_existing=True)
    event = await db.get(WorkBoardEvent,witness.request_event_id,populate_existing=True)
    attempt = await db.get(WorkBoardAttempt,witness.attempt_id,populate_existing=True) if witness.attempt_id else None
    if (row_token(task) != witness.task_token or artifact is None or row_token(artifact) != witness.input_token
        or event is None or row_token(event) != witness.request_event_token
        or (witness.attempt_id is not None and (attempt is None or row_token(attempt) != witness.attempt_token))):
        raise BoardError("opportunity_recommendation_binding_invalid", "The exact staged request tokens changed")
    await _recheck_original_commitment(db,task,event,witness)
    await recheck_population(db,witness=witness.population)
    return task,attempt

def _recommendation_request(request):
    from src.guardian.opportunity_preferences import OpportunityRecommendationRequest
    return OpportunityRecommendationRequest.model_validate(
        request if isinstance(request,dict) else request.model_dump(mode="json"))


async def request_opportunity_recommendation(*, operator, opportunity_id, request):
    from src.guardian.opportunity_preferences import OpportunityRecommendationRequest, stage_population
    from src.memory.procedure_recommendations import assert_current_root
    from src.memory.repository import _effect_mac_key, _m5_selection_binding_key_id
    from src.work_board.repository import WorkBoardRepository, stage_safe_task_text, _begin_sqlite_immediate
    from src.work_board.contracts import WorkBoardTaskCreate, WorkBoardInputArtifactCreate
    from src.work_board.input_artifacts import prepare_input_artifact, stage_input_artifact
    from src.work_board.dispatcher import registered_executor_id
    request = _recommendation_request(request)
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id,session_id=operator.session_id)
    async with db_engine.get_session() as db:
        await assert_current_root(db,operator)
        existing = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.owner_principal_id == owner.principal_id,
            WorkBoardTask.owner_session_id == owner.session_id,WorkBoardTask.idempotency_scope == REQUEST_SCOPE,
            WorkBoardTask.idempotency_key == f"{opportunity_id}:{request.idempotency_key}"))
        if existing is not None:
            _event,metadata = await _request_receipt(db,existing)
            if metadata.get("request_body_digest") != digest(request.model_dump(mode="json")):
                raise BoardError("idempotency_conflict", "The original recommendation request changed", status_code=409)
            replay = True
        else:
            replay = False
    if replay:
        return await inspect_opportunity_recommendation(operator=operator,opportunity_id=opportunity_id,
            request_uuid=request.idempotency_key,idempotent_replay=True)
    key = _effect_mac_key()
    cutoff = now()
    async with db_engine.get_session() as db:
        anchor = await db.get(GuardianOpportunity,opportunity_id,populate_existing=True)
        if anchor is None:
            raise BoardError("opportunity_not_proposed", "The opportunity is unavailable", status_code=404)
        population = await stage_population(db,owner,anchor=anchor,request=request,cutoff_at=cutoff,operator=operator)
        cpu_input = population.cpu_input()
        artifact = await prepare_input_artifact(db,owner,WorkBoardInputArtifactCreate(capability_id=CAPABILITY,
            goal_id=population.goal_id,goal_revision=population.goal_revision,input=cpu_input.model_dump(mode="json"),
            idempotency_key=f"opportunity-recommendation:{opportunity_id}:{request.idempotency_key}"),publication_population=population)
    async with db_engine.get_session() as db:
        artifact_witness = await stage_input_artifact(db,owner,artifact_id=artifact.artifact_id,
            capability_id=CAPABILITY,goal_id=population.goal_id,goal_revision=population.goal_revision)
        create = WorkBoardTaskCreate(title="Review opportunity usefulness",body="Provider-free opportunity feedback calculation; separate memory review required.",
            capability_id=CAPABILITY,goal_id=population.goal_id,goal_revision=population.goal_revision,
            input_artifact_id=artifact.artifact_id,executor_id=registered_executor_id(CAPABILITY),status="todo",
            requires_review=False,idempotency_scope=REQUEST_SCOPE,idempotency_key=f"{opportunity_id}:{request.idempotency_key}")
        staged_text = await stage_safe_task_text(db,owner,create)
        witness = PublicationWitness(owner.principal_id,owner.session_id,_payload_digest(create),population,staged_text,artifact_witness)
        root = await assert_current_root(db,operator)
        original_idle = min(utc(root.idle_expires_at),utc(operator.idle_expires_at))
        original_absolute = min(utc(root.absolute_expires_at),utc(operator.absolute_expires_at))
        deadline = min(cutoff+timedelta(seconds=30),original_idle,original_absolute)
        preview_expiry = min(cutoff+timedelta(minutes=5),original_idle,original_absolute,population.expires_at)
        await db.rollback()
        await _begin_sqlite_immediate(db)
        mutation = await WorkBoardRepository()._create_task_locked(db,owner,create,staged_text=staged_text,
            staged_input=artifact_witness,publication_witness=witness)
        task = mutation.task
        if mutation.idempotent_replay:
            _event,metadata = await _request_receipt(db,task)
            if metadata.get("request_body_digest") != digest(request.model_dump(mode="json")):
                raise BoardError("idempotency_conflict", "The original recommendation request changed", status_code=409)
        else:
            metadata = {"schema_version":1,"request_uuid":request.idempotency_key,
                "request_body_digest":digest(request.model_dump(mode="json")),"owner_principal_id":owner.principal_id,
                "original_root_id":owner.session_id,"goal_id":population.goal_id,"goal_revision":population.goal_revision,
                "task_id":task.task_id,"task_create_digest":task.idempotency_payload_digest,
                "input_artifact_id":artifact.artifact_id,"input_sha256":task.typed_input_digest,
                "cpu_input_digest":digest(cpu_input.model_dump(mode="json")),"population_digest":population.population_digest,
                "generation_cutoff_at":cutoff.isoformat(),"original_idle_expires_at":original_idle.isoformat(),
                "original_absolute_expires_at":original_absolute.isoformat(),"execution_deadline":deadline.isoformat(),
                "preview_expires_at":preview_expiry.isoformat(),"key_id":_m5_selection_binding_key_id(_signing_key=key)}
            metadata["authorization_mac"] = _authorization_mac(metadata,operator._token_hash,key)
            await WorkBoardRepository()._event(db,task,owner,kind=REQUESTED_EVENT,metadata=metadata)
        await db.commit()
    return await inspect_opportunity_recommendation(operator=operator,opportunity_id=opportunity_id,
        request_uuid=request.idempotency_key,idempotent_replay=mutation.idempotent_replay)

@dataclass(frozen=True)
class NativeWitness:
    authority: TaskAuthorityWitness
    run_token: bytes | None
    output_reference: str | None = None
    output_sha256: str | None = None
    output_bytes: bytes | None = None


async def recheck_native(db, run, *, witness, terminal=False):
    if not isinstance(witness,NativeWitness):
        raise BoardError("opportunity_recommendation_binding_invalid", "The fixed native staged witness is required")
    task,attempt = await recheck_task_authority(db,witness=witness.authority,execution=True)
    if attempt is None or not binds(task,attempt,run):
        raise BoardError("opportunity_recommendation_binding_invalid", "The exact original native envelope changed")
    if utc(run.deadline_at) != witness.authority.execution_deadline or run.max_attempts != 1:
        raise BoardError("opportunity_recommendation_binding_invalid", "The original native bounds changed")
    if witness.run_token is not None and row_token(run) != witness.run_token:
        raise BoardError("opportunity_recommendation_binding_invalid", "The staged native revision changed")
    if terminal and (run.status != "running" or witness.output_bytes is None
        or hashlib.sha256(witness.output_bytes).hexdigest() != witness.output_sha256):
        raise BoardError("opportunity_recommendation_output_unverified", "Actual output and readback are required")
    if terminal:
        artifact,readback = _output_proof(task,attempt,run)
        if artifact["file_path"] != witness.output_reference or artifact["content_sha256"] != witness.output_sha256:
            raise BoardError("opportunity_recommendation_output_unverified", "The staged physical output does not match exact SQL receipts")
        from src.guardian.opportunity_preferences import RecommendationOutput,calculate_recommendation
        output = RecommendationOutput.model_validate_json(witness.output_bytes)
        if output != calculate_recommendation(witness.authority.population,witness.authority.cpu_input):
            raise BoardError("opportunity_recommendation_output_unverified", "The staged output does not match the complete authorized population")
    if (task.status is not WorkBoardStatus.running or attempt.ended_at is not None
        or attempt.cancel_requested_at is not None or attempt.lease_expires_at is None
        or utc(attempt.lease_expires_at) <= now() or attempt.workflow_run_id not in {None,run.run_identity}):
        raise BoardError("opportunity_recommendation_binding_invalid", "The original board attempt is no longer active")


def read_output(reference, sha):
    from src.work_board.pipeline_cpu import read_output as bounded_read
    if not reference.startswith("artifacts/work-board/evidence/opportunity-preference/"):
        raise BoardError("opportunity_recommendation_output_unverified", "The private output reference is invalid")
    return bounded_read(reference,sha,max_bytes=MAX_BYTES)


def _output_proof(task,attempt,run):
    if not binds(task,attempt,run):
        raise BoardError("opportunity_recommendation_output_unverified", "The actual native envelope changed")
    artifacts = json.loads(run.artifact_receipts_json or "[]")
    effects = json.loads(run.effect_receipts_json or "[]")
    artifacts = [a for a in artifacts if isinstance(a,dict) and a.get("artifact_type") == "opportunity_preference_output"]
    if len(artifacts) != 1:
        raise BoardError("opportunity_recommendation_output_unverified", "One actual output is required")
    artifact = artifacts[0]
    sha = artifact.get("content_sha256")
    reference = artifact.get("file_path")
    readbacks = [e for e in effects if isinstance(e,dict) and e.get("receipt_kind") == "readback"
        and e.get("effect_type") == "opportunity_preference_output" and e.get("status") == "succeeded"
        and e.get("target_path") == reference and e.get("content_sha256") == sha
        and e.get("target_digest") == sha and e.get("verified_at") and e.get("readback_id")]
    if len(readbacks) != 1:
        raise BoardError("opportunity_recommendation_output_unverified", "The exact native readback is required")
    return artifact,readbacks[0]


async def _calculate(population,cpu_input):
    from src.guardian.opportunity_preferences import calculate_recommendation
    return calculate_recommendation(population,cpu_input)


async def execute(task,attempt,inputs,*,jobs,runner,admission_only,session_provider):
    import asyncio
    from src.work_board.input_artifacts import _write_payload
    from src.workspace import canonical_workspace_root
    from config.settings import settings
    async with session_provider() as db:
        staged = await stage_task_authority(db,task,attempt=attempt,session_provider=session_provider)
    if staged.execution_deadline <= now():
        raise BoardError("opportunity_recommendation_expired", "The original CPU deadline has expired")
    spec = spec_for(task,attempt,inputs,deadline=staged.execution_deadline)
    projection = await jobs.get_job(spec.identity.job_id)
    if projection is None:
        projection = await jobs.admit_job(spec,opportunity_preference_witness=NativeWitness(staged,None))
    elif (projection.get("input_digest") != digest(spec.inputs)
        or projection.get("run_fingerprint") != spec.run_fingerprint
        or projection.get("declared_authority") != spec.declared_authority
        or utc(datetime.fromisoformat(projection["deadline_at"])) != staged.execution_deadline):
        raise BoardError("opportunity_recommendation_binding_invalid", "The original native admission changed")
    if admission_only or projection.get("status") == "succeeded":
        return {**projection,"admission_only":admission_only}
    if projection.get("status") not in {"accepted","queued"}:
        raise BoardError("opportunity_recommendation_original_attempt_required", "The original native attempt must be inspected")
    if projection.get("status") == "accepted":
        projection = await jobs.queue_job(spec.identity.job_id,expected_revision=projection["revision"],
            opportunity_preference_witness=NativeWitness(staged,None))
    projection = await jobs.claim_job(spec.identity.job_id,owner=runner,lease_seconds=30,
        expected_revision=projection["revision"],expected_fencing_token=projection.get("fencing_token"),
        opportunity_preference_witness=NativeWitness(staged,None))
    fence = projection["lease"]["fencing_token"]
    try:
        async with session_provider() as db:
            staged = await stage_task_authority(db,task,attempt=attempt,session_provider=session_provider)
            run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == spec.identity.job_id))
            current = NativeWitness(staged,row_token(run))
        await jobs.record_checkpoint(spec.identity.job_id,checkpoint_id="opportunity-preference-source-use",
            state={"phase":"source_use_admitted"},owner=runner,fencing_token=fence,
            expected_revision=projection["revision"],opportunity_preference_witness=current)
        async with session_provider() as db:
            staged = await stage_task_authority(db,task,attempt=attempt,session_provider=session_provider)
        remaining = (staged.execution_deadline-now()).total_seconds()
        if remaining <= 0:
            raise BoardError("opportunity_recommendation_expired", "The original CPU deadline has expired")
        output = await asyncio.wait_for(_calculate(staged.population,staged.cpu_input),timeout=remaining)
        content = canonical(output.model_dump(mode="json"))
        if len(content) > MAX_BYTES:
            raise BoardError("opportunity_recommendation_output_overflow", "The private output exceeds its finite bound")
        sha = digest(content)
        reference = f"artifacts/work-board/evidence/opportunity-preference/{task.task_id}-{attempt.attempt_id}-{sha}.json"
        _write_payload(canonical_workspace_root(settings.workspace_dir)/reference,content)
        if read_output(reference,sha) != content:
            raise BoardError("opportunity_recommendation_output_unverified", "The private output readback differs")
        await jobs.record_artifact(spec.identity.job_id,file_path=reference,artifact_type="opportunity_preference_output",
            content=content,owner=runner,fencing_token=fence)
        await jobs.record_readback(spec.identity.job_id,effect_type="opportunity_preference_output",target_path=reference,
            target_digest=sha,content_sha256=sha,readback_id=f"opportunity-preference-readback-{sha[:24]}",
            verified_at=now().isoformat(),status="succeeded",details={"verified":True,"no_learning":True},owner=runner,fencing_token=fence)
        async with session_provider() as db:
            staged = await stage_task_authority(db,task,attempt=attempt,session_provider=session_provider)
            run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == spec.identity.job_id))
            _output_proof(task,attempt,run)
            actual = read_output(reference,sha)
            terminal = NativeWitness(staged,row_token(run),reference,sha,actual)
        finished = await jobs.transition_job(spec.identity.job_id,"succeeded",owner=runner,fencing_token=fence,
            result={"status":output.status,"no_learning":True,"output_sha256":sha},
            result_summary="Provider-free opportunity recommendation independently read back; no_learning",
            opportunity_preference_witness=terminal)
        return {**finished,"memory_status":"no_learning","admission_only":False}
    except asyncio.CancelledError:
        # Cancellation settlement belongs to the existing registered worker owner.
        raise


@dataclass(frozen=True)
class DoneSourceWitness:
    authority: TaskAuthorityWitness
    run_token: bytes
    task: Any
    attempt: Any
    run: Any
    cpu_input: Any
    input_bytes: bytes
    output_bytes: bytes
    output_sha256: str
    artifact_id: str
    output_reference: str
    readback_id: str
    readback_digest: str


async def stage_output_source(db,task,attempt,*,session_provider=None,require_done=True):
    from src.guardian.opportunity_preferences import RecommendationOutput,calculate_recommendation
    authority = await stage_task_authority(db,task,attempt=attempt,session_provider=session_provider)
    task = await _task(db,task.task_id)
    attempt = await db.get(WorkBoardAttempt,attempt.attempt_id,populate_existing=True)
    run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id).execution_options(populate_existing=True))
    if (run is None or run.status != "succeeded" or run.finished_at is None
        or utc(run.finished_at) > authority.execution_deadline
        or (require_done and (task.status is not WorkBoardStatus.done or attempt.ended_at is None))):
        raise BoardError("opportunity_recommendation_output_unverified", "The actual completed CPU source is required")
    artifact,readback = _output_proof(task,attempt,run)
    raw = read_output(artifact["file_path"],artifact["content_sha256"])
    output = RecommendationOutput.model_validate_json(raw)
    if output != calculate_recommendation(authority.population,authority.cpu_input):
        raise BoardError("learning_population_incomplete", "The actual output no longer matches the current complete population")
    latest = await db.scalar(select(WorkBoardAttempt.attempt_id).where(WorkBoardAttempt.task_id == task.task_id)
        .order_by(WorkBoardAttempt.created_at.desc(),WorkBoardAttempt.attempt_id.desc()).limit(1))
    if latest != attempt.attempt_id:
        raise BoardError("opportunity_recommendation_output_unverified", "The actual latest CPU attempt changed")
    return DoneSourceWitness(authority,row_token(run),task.model_copy(deep=True),attempt.model_copy(deep=True),run.model_copy(deep=True),
        authority.cpu_input,authority.input_bytes,raw,artifact["content_sha256"],artifact["artifact_id"],artifact["file_path"],
        readback["readback_id"],digest(readback))


async def stage_done_source(*,operator,task_id,attempt_id,job_id):
    async with db_engine.get_session() as db:
        task = await _task(db,task_id)
        if (task.owner_principal_id,task.owner_session_id) != (operator.principal.principal_id,operator.session_id):
            raise BoardError("opportunity_recommendation_not_owned", "The actual CPU source belongs to another Root", status_code=403)
        attempt = await db.get(WorkBoardAttempt,attempt_id,populate_existing=True)
        if attempt is None or attempt.task_id != task_id or attempt.workflow_run_id != job_id:
            raise BoardError("opportunity_recommendation_output_unverified", "The original CPU attempt/job binding changed")
        return await stage_output_source(db,task,attempt)


async def recheck_done_source(db,*,witness):
    if not isinstance(witness,DoneSourceWitness):
        raise BoardError("opportunity_recommendation_output_unverified", "The staged actual CPU source is required")
    task,attempt = await recheck_task_authority(db,witness=witness.authority)
    run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == witness.run.run_identity).execution_options(populate_existing=True))
    if task.status is not WorkBoardStatus.done or attempt is None or attempt.ended_at is None or run is None or row_token(run) != witness.run_token:
        raise BoardError("opportunity_recommendation_output_unverified", "The actual Done source tokens changed")
    return task,attempt,run


async def _recheck_original_commitment(db,task,event,witness):
    """SQL-only original/current Root fence; historical Done keeps its own deadline."""
    from src.memory.repository import _m5_selection_binding_key_id
    metadata = _parse_request_metadata(event.metadata_json)
    root = await db.get(OperatorSession,task.owner_session_id,populate_existing=True)
    if (root is None or not root.token_hash or root.principal_id != task.owner_principal_id
        or root.token_hash != witness.operator._token_hash
        or root.revoked_at is not None or root.replaced_by_id is not None or root.is_bearer_tombstone
        or metadata["key_id"] != _m5_selection_binding_key_id(_signing_key=witness.signing_key)
        or not hmac.compare_digest(metadata["authorization_mac"],_authorization_mac(metadata,root.token_hash,witness.signing_key))
        or min(utc(root.idle_expires_at),utc(root.absolute_expires_at),
            utc(datetime.fromisoformat(metadata["original_idle_expires_at"])),
            utc(datetime.fromisoformat(metadata["original_absolute_expires_at"])),
            utc(witness.operator.idle_expires_at),utc(witness.operator.absolute_expires_at)) <= now()):
        raise BoardError("opportunity_recommendation_root_stale", "The original sealed Root authority changed or expired")


async def recheck_projection_source(db,*,witness):
    if not isinstance(witness,DoneSourceWitness):
        raise BoardError("opportunity_recommendation_output_unverified", "The staged actual output is required")
    task,attempt = await recheck_task_authority(db,witness=witness.authority)
    run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == witness.run.run_identity).execution_options(populate_existing=True))
    if (task.status is not WorkBoardStatus.running or attempt is None or attempt.ended_at is not None
        or attempt.cancel_requested_at is not None or run is None or run.status != "succeeded"
        or row_token(run) != witness.run_token):
        raise BoardError("opportunity_recommendation_output_unverified", "The actual settled output binding changed")
    return task,attempt,run


async def finalize_done(*,task_id,attempt_id,job_id):
    from src.guardian.opportunity_preferences import stage_finalization,finalize_in_session
    from src.work_board.repository import WorkBoardRepository,_begin_sqlite_immediate
    operator = await stage_request_operator(task_id=task_id)
    async with db_engine.get_session() as db:
        task = await _task(db,task_id)
        prior = await db.scalar(select(WorkBoardEvent).where(WorkBoardEvent.task_id == task_id,WorkBoardEvent.kind == FINALIZED_EVENT).limit(1))
        if prior is not None:
            return json.loads(prior.metadata_json)
    staged = await stage_finalization(operator=operator,task_id=task_id,attempt_id=attempt_id,job_id=job_id)
    async with db_engine.get_session() as db:
        await _begin_sqlite_immediate(db)
        task = await _task(db,task_id)
        prior = await db.scalar(select(WorkBoardEvent).where(WorkBoardEvent.task_id == task_id,WorkBoardEvent.kind == FINALIZED_EVENT).limit(1))
        if prior is not None:
            return json.loads(prior.metadata_json)
        result = await finalize_in_session(db,staged)
        metadata = {**result,"request_uuid":staged.population.request_uuid,"task_id":task_id,
            "attempt_id":attempt_id,"job_id":job_id}
        await WorkBoardRepository()._event(db,task,WorkBoardOwner(principal_id=task.owner_principal_id,
            session_id=task.owner_session_id),kind=FINALIZED_EVENT,metadata=metadata)
        await db.commit()
        return metadata


async def inspect_opportunity_recommendation(*,operator,opportunity_id,request_uuid,idempotent_replay=False):
    from src.memory.procedure_recommendations import assert_current_root
    from src.guardian.opportunity_preferences import exact_uuid
    exact_uuid(request_uuid)
    async with db_engine.get_session() as db:
        await assert_current_root(db,operator)
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.owner_principal_id == operator.principal.principal_id,
            WorkBoardTask.owner_session_id == operator.session_id,WorkBoardTask.idempotency_scope == REQUEST_SCOPE,
            WorkBoardTask.idempotency_key == f"{opportunity_id}:{request_uuid}"))
        if task is None:
            raise BoardError("opportunity_recommendation_unavailable", "The original recommendation is unavailable", status_code=404)
        _event,metadata = await _request_receipt(db,task)
        attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task.task_id)
            .order_by(WorkBoardAttempt.created_at.desc(),WorkBoardAttempt.attempt_id.desc()).limit(1))
        finalized = await db.scalar(select(WorkBoardEvent).where(WorkBoardEvent.task_id == task.task_id,WorkBoardEvent.kind == FINALIZED_EVENT).limit(1))
        values = json.loads(finalized.metadata_json) if finalized else None
        task_snapshot = task.model_copy(deep=True)
        attempt_snapshot = attempt.model_copy(deep=True) if attempt else None
    # GET can finish only this previously authorized, actually committed Done source.
    if values is None and task_snapshot.status is WorkBoardStatus.done and attempt_snapshot is not None:
        values = await finalize_done(task_id=task_snapshot.task_id,attempt_id=attempt_snapshot.attempt_id,job_id=attempt_snapshot.workflow_run_id)
    await stage_request_operator(task_id=task_snapshot.task_id)
    if values is not None:
        status = values["status"]
        reason = values["reason_code"]
    elif attempt_snapshot is not None and attempt_snapshot.cancel_requested_at is not None and attempt_snapshot.ended_at is None:
        status,reason = "cancel_requested","operator_cancelled"
    elif task_snapshot.status is WorkBoardStatus.blocked:
        status = "unknown" if task_snapshot.block_kind == "unknown_effect" else "cancelled" if task_snapshot.block_kind == "cancelled" else "blocked"
        reason = "outcome_unknown" if status == "unknown" else "operator_cancelled" if status == "cancelled" else "learning_population_incomplete"
    else:
        status,reason = ("running","opportunity_recommendation_running") if task_snapshot.status is WorkBoardStatus.running else ("queued","opportunity_recommendation_queued")
    async with db_engine.get_session() as db:
        from src.work_board.dispatcher import _parse_typed_input
        cpu_input = _parse_typed_input(task_snapshot)
    return {"opportunity_id":opportunity_id,"opportunity_revision":cpu_input["expected_opportunity_revision"],
        "feedback_revision":cpu_input["expected_feedback_revision"],"task_id":task_snapshot.task_id,
        "task_revision":task_snapshot.task_revision,"attempt_id":attempt_snapshot.attempt_id if attempt_snapshot else None,
        "job_id":attempt_snapshot.workflow_run_id if attempt_snapshot else None,"reason_code":reason,
        "population_digest":metadata["population_digest"],"idempotent_replay":idempotent_replay,
        "memory_status":"no_learning","status":status,"proposal_id":values.get("proposal_id") if values else None,
        "bundle_digest":values.get("bundle_digest") if values else None}
