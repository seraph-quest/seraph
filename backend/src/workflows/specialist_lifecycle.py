"""Closed protected specialist publication/wait facts on the original callback.

These facts are not a queue or independent execution authority. Every live
writer consumes the original native callback and canonical Board owner.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Literal
from dataclasses import dataclass
from weakref import WeakKeyDictionary

from pydantic import Field, model_validator
from sqlalchemy import select, update
from src.db.models import WorkBoardAttempt, WorkBoardTask, WorkflowRunState, WorkBoardInputArtifact, Goal
from src.work_board.contracts import ClosedTaskModel, TaskDigest, TaskIdentity
from src.work_board.repository import BoardError

CREATION_KEY = "general:delegation:child_creation:v1"
ADMISSION_KEY = "general:delegation:child_admission:v1"
WAIT_KEY = "general:delegation:wait:v1"
CLOSURE_KEY = "general:delegation:closure:v1"


class SpecialistChildCreationV1(ClosedTaskModel):
    schema_version: Literal["SpecialistChildCreation.v1"] = "SpecialistChildCreation.v1"
    invocation_id: str = Field(min_length=1, max_length=128)
    reservation_digest: TaskDigest
    parent_job_id: str = Field(min_length=1, max_length=128)
    parent_creation_digest: TaskDigest
    owner_principal_id: TaskIdentity
    original_root_id: TaskIdentity
    goal_id: TaskIdentity
    goal_revision: int = Field(ge=1)
    callback_fence: int = Field(ge=1)
    child_task_id: TaskIdentity
    reserved_attempt_id: TaskIdentity
    reserved_job_id: TaskIdentity
    child_input_artifact_id: str = Field(min_length=1, max_length=512)
    child_input_digest: TaskDigest
    child_input_metadata_digest: TaskDigest
    child_input_immutable_digest: TaskDigest
    child_input_ref: str = Field(min_length=1, max_length=512)
    child_task_binding_digest: TaskDigest
    child_plan_digest: TaskDigest
    original_deadline_at: str = Field(min_length=1, max_length=64)
    child_deadline_at: str = Field(min_length=1, max_length=64)


class SpecialistWaitV1(ClosedTaskModel):
    schema_version: Literal["SpecialistWait.v1"] = "SpecialistWait.v1"
    invocation_id: str = Field(min_length=1, max_length=128)
    reservation_digest: TaskDigest
    creation_digest: TaskDigest
    child_creation_digest: TaskDigest
    child_admission_digest: TaskDigest
    original_claim_fence: int = Field(ge=1)
    original_claim_owner: str = Field(min_length=1, max_length=128)
    original_intent_digest: TaskDigest
    child_task_id: TaskIdentity
    child_attempt_id: TaskIdentity
    child_job_id: TaskIdentity
    child_input_digest: TaskDigest
    child_authority_digest: TaskDigest
    original_deadline_at: str = Field(min_length=1, max_length=64)
    child_deadline_at: str = Field(min_length=1, max_length=64)
    state: Literal["specialist_wait"] = "specialist_wait"


class SpecialistChildAdmissionV1(ClosedTaskModel):
    schema_version: Literal["SpecialistChildAdmission.v1"] = "SpecialistChildAdmission.v1"
    invocation_id: str = Field(min_length=1, max_length=128)
    reservation_digest: TaskDigest
    creation_digest: TaskDigest
    child_task_id: TaskIdentity
    child_attempt_id: TaskIdentity
    child_job_id: TaskIdentity
    board_fence: int = Field(ge=1)
    board_owner: str = Field(min_length=1, max_length=128)
    child_input_digest: TaskDigest
    child_authority_digest: TaskDigest
    child_deadline_at: str = Field(min_length=1, max_length=64)


class SpecialistDelegationClosureV1(ClosedTaskModel):
    schema_version: Literal["SpecialistDelegationClosure.v1"] = "SpecialistDelegationClosure.v1"
    invocation_id: str = Field(min_length=1, max_length=128)
    original_binding_digest: TaskDigest
    reservation_digest: TaskDigest
    wait_digest: TaskDigest
    child_creation_digest: TaskDigest
    original_claim_fence: int = Field(ge=1)
    child_task_id: TaskIdentity
    child_attempt_id: TaskIdentity
    child_job_id: TaskIdentity
    child_input_digest: TaskDigest
    child_authority_digest: TaskDigest
    child_effect_digest: TaskDigest
    child_artifact_digest: TaskDigest
    child_checkpoint_digest: TaskDigest
    outcome: Literal["durable_result_verified", "partial_decision_verified", "held_unknown"]
    output_digest: TaskDigest | None = None
    artifact_refs: list[str] = Field(default_factory=list, max_length=16)
    unresolved: list[str] = Field(default_factory=list, max_length=16)
    no_learning: Literal[True] = True

    @model_validator(mode="after")
    def exact_outcome(self):
        if self.outcome == "durable_result_verified" and (self.output_digest is None or self.unresolved or not self.artifact_refs):
            raise ValueError("full closure requires actual artifacts without unresolved debt")
        if self.outcome == "partial_decision_verified" and (self.output_digest is None or not self.unresolved):
            raise ValueError("partial closure requires an explicit decision and retained unresolved debt")
        if self.outcome == "held_unknown" and (self.output_digest is not None or not self.unresolved):
            raise ValueError("Unknown cannot become a successful output closure")
        return self


def read_fact(callback, key, model):
    from src.work_board.general_task import digest
    rows = [row for row in json.loads(callback.checkpoint_receipts_json or "[]")
        if row.get("checkpoint_id") == key]
    if not rows:
        return None
    if len(rows) != 1:
        raise BoardError("specialist_lifecycle_corrupt", "Ambiguous original specialist facts", status_code=409)
    row = rows[0]
    fact = model.model_validate(row.get("payload"))
    if (row.get("safe") is not True or row.get("state_digest") != digest(fact.model_dump(mode="json"))
        or fact.invocation_id != callback.run_identity):
        raise BoardError("specialist_lifecycle_corrupt", "Original specialist fact changed", status_code=409)
    return fact


_WAIT_SEAL = object()
_WAIT_SOURCES = WeakKeyDictionary()


@dataclass(frozen=True,eq=False)
class _SpecialistWaitWitness:
    invocation_id: str
    original_binding_digest: str
    wait_digest: str
    fencing_token: int
    seal: object


class SpecialistWaitRequired(Exception):
    """Actual source-issued durable wait, never a native callback return."""
    def __init__(self,witness):
        self.witness = witness
        super().__init__("Original specialist children are durably waiting")


async def pause_specialist_callback(jobs,invocation_id,*,owner,fence):
    from src.work_board.repository import _begin_sqlite_immediate
    from src.workflows.specialist_delegation import current_delegation
    from src.work_board.general_task import digest
    from src.workflows.job_runtime import _effect_ledger_or_raise
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        context = await current_delegation(db,invocation_id,callback_fence=fence)
        jobs._assert_lease(context.callback,owner=owner,fencing_token=fence)
        reservation = context.reservation
        creation = read_fact(context.callback,CREATION_KEY,SpecialistChildCreationV1)
        admission = read_fact(context.callback,ADMISSION_KEY,SpecialistChildAdmissionV1)
        child = await jobs._fetch(db,reservation.child_job_id)
        child_attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == reservation.child_attempt_id))
        if (creation is None or admission is None or child_attempt is None
            or child_attempt.workflow_run_id != child.run_identity or child_attempt.ended_at
            or child_attempt.cancel_requested_at or child.status not in {"accepted","queued"}
            or child.attempt_count != 0 or child.fencing_token != 0
            or child.input_digest != admission.child_input_digest or child.authority_digest != admission.child_authority_digest
            or admission.creation_digest != digest(creation.model_dump(mode="json"))
            or child.parent_fencing_token != fence or child.parent_job_id != invocation_id
            or child.effect_receipts_json not in {"[]",""}):
            raise BoardError("specialist_wait_child_changed","Exact original uncontacted child required",status_code=409)
        effect_id = "general:"+context.native_binding.step_id+":"+str(fence)
        intents = [item for item in _effect_ledger_or_raise(context.callback.effect_receipts_json)
            if item.get("effect_id") == effect_id and item.get("effect_type") == "general_tool_call"
            and item.get("receipt_kind") == "effect" and item.get("status") == "intent"
            and item.get("fencing_token") == fence]
        if len(intents) != 1:
            raise BoardError("specialist_wait_intent_changed","Original actual native intent required",status_code=409)
        wait = SpecialistWaitV1(invocation_id=invocation_id,
            reservation_digest=digest(reservation.model_dump(mode="json")),creation_digest=context.manifest.creation_digest,
            child_creation_digest=digest(creation.model_dump(mode="json")),child_admission_digest=digest(admission.model_dump(mode="json")),
            original_claim_fence=fence,original_claim_owner=owner,original_intent_digest=digest(intents[0]),
            child_task_id=reservation.child_task_id,child_attempt_id=reservation.child_attempt_id,child_job_id=reservation.child_job_id,
            child_input_digest=child.input_digest,child_authority_digest=child.authority_digest,
            original_deadline_at=reservation.original_deadline_at,child_deadline_at=reservation.child_deadline_at)
        if read_fact(context.callback,WAIT_KEY,SpecialistWaitV1) is not None:
            raise BoardError("specialist_wait_exists","Recover the original sealed wait",status_code=409)
        history = json.loads(context.callback.checkpoint_receipts_json or "[]")
        history.append({"checkpoint_id":WAIT_KEY,"state_digest":digest(wait.model_dump(mode="json")),
            "payload":wait.model_dump(mode="json"),"safe":True,"fencing_token":fence,
            "recorded_at":datetime.now(timezone.utc).isoformat()})
        if len(history)+3 > 50 or len(json.dumps(history).encode())+3*65536 > 4*1024*1024:
            raise BoardError("specialist_proof_capacity","Original specialist wait capacity exhausted",status_code=409)
        changed = await db.execute(update(WorkflowRunState).where(
            WorkflowRunState.run_identity == invocation_id,WorkflowRunState.revision == context.callback.revision,
            WorkflowRunState.status == "running",WorkflowRunState.lease_owner == owner,
            WorkflowRunState.fencing_token == fence).values(
                checkpoint_receipts_json=json.dumps(history,sort_keys=True,separators=(",",":")),
                status="paused",failure_reason="specialist_wait",lease_owner=None,lease_expires_at=None,
                revision=WorkflowRunState.revision+1))
        if changed.rowcount != 1:
            raise BoardError("specialist_wait_changed","Original specialist wait fence changed",status_code=409)
        witness = _SpecialistWaitWitness(invocation_id,digest(context.native_binding.model_dump(mode="json")),
            digest(wait.model_dump(mode="json")),fence,_WAIT_SEAL)
        _WAIT_SOURCES[witness] = (witness.invocation_id,witness.original_binding_digest,witness.wait_digest,witness.fencing_token)
        return SpecialistWaitRequired(witness)


async def verify_wait_signal(db,signal,*,binding,fencing_token):
    from src.work_board.general_task import digest
    witness = signal.witness
    if (type(signal) is not SpecialistWaitRequired or type(witness) is not _SpecialistWaitWitness
        or witness.seal is not _WAIT_SEAL
        or _WAIT_SOURCES.get(witness) != (witness.invocation_id,witness.original_binding_digest,witness.wait_digest,witness.fencing_token)
        or witness.invocation_id != binding.invocation_id or witness.fencing_token != fencing_token
        or witness.original_binding_digest != digest(binding.model_dump(mode="json"))):
        raise BoardError("specialist_wait_signal_denied","Actual original delegation wait producer required",status_code=409)
    callback = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == binding.invocation_id))
    wait = read_fact(callback,WAIT_KEY,SpecialistWaitV1) if callback else None
    if callback is None or callback.status != "paused" or callback.failure_reason != "specialist_wait" or wait is None:
        raise BoardError("specialist_wait_changed","Original sealed specialist wait required",status_code=409)
    if digest(wait.model_dump(mode="json")) != witness.wait_digest or callback.fencing_token != fencing_token:
        raise BoardError("specialist_wait_changed","Original sealed specialist wait changed",status_code=409)
    return wait


async def verify_terminal_specialist_origin(db, task, attempt, run):
    """Metadata-only historical origin; cannot authorize execution or adoption.

    The caller already owns this Task read scope. Cutoffs and running phases
    intentionally do not grant anything here: only actual completed child
    rows and the original protected publication can produce a history proof.
    """
    from src.workflows.specialist_delegation import read_reservation
    from src.workflows.general_task_guard import child_binding, read_manifest, assert_original_parent_authority
    from src.work_board.general_task import digest
    from src.work_board.input_artifacts import _metadata_digest
    def deny():
        raise BoardError("specialist_history_binding_changed", "Original completed specialist metadata changed", status_code=409)
    if run.status != "succeeded" or attempt.ended_at is None or task.status.value != "done":
        deny()
    callback = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == task.origin_thread_id).execution_options(populate_existing=True))
    if callback is None:
        deny()
    reservation = read_reservation(callback)
    creation = read_fact(callback,CREATION_KEY,SpecialistChildCreationV1)
    admission = read_fact(callback,ADMISSION_KEY,SpecialistChildAdmissionV1)
    if reservation is None or creation is None or admission is None:
        deny()
    native = child_binding(callback)
    original = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == native.parent_job_id).execution_options(populate_existing=True))
    original_task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == native.task_id))
    original_attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == native.attempt_id))
    artifact = await db.get(WorkBoardInputArtifact,task.input_artifact_id,populate_existing=True)
    goal = await db.get(Goal,task.goal_id,populate_existing=True)
    if original is None or original_task is None or original_attempt is None or artifact is None or goal is None:
        deny()
    assert_original_parent_authority(original)
    assert_original_parent_authority(run)
    manifest = read_manifest(original)
    authority = json.loads(run.declared_authority_json or "{}")
    if (manifest is None or manifest.creation_digest != native.creation_digest
        or callback.run_identity not in manifest.admitted_invocation_ids
        or original.parent_job_id is not None or original.branch_depth != 0
        or reservation.parent_job_id != original.run_identity
        or reservation.parent_task_id != original_task.task_id or reservation.parent_attempt_id != original_attempt.attempt_id
        or original_attempt.workflow_run_id != original.run_identity
        or reservation.parent_creation_digest != manifest.creation_digest
        or native.parent_authority_digest != original.authority_digest
        or native.selected_grant_digest != reservation.selected_grant_digest
        or (creation.owner_principal_id,creation.original_root_id) != (task.owner_principal_id,task.owner_session_id)
        or (original.owner_principal_id,original.operator_session_id) != (task.owner_principal_id,task.owner_session_id)
        or (goal.owner_principal_id,goal.owner_session_id) != (task.owner_principal_id,task.owner_session_id)
        or creation.reservation_digest != digest(reservation.model_dump(mode="json"))
        or creation.parent_creation_digest != manifest.creation_digest
        or creation.child_task_id != task.task_id or reservation.child_task_id != task.task_id
        or creation.reserved_attempt_id != attempt.attempt_id or reservation.child_attempt_id != attempt.attempt_id
        or creation.reserved_job_id != run.run_identity or reservation.child_job_id != run.run_identity
        or creation.callback_fence != reservation.callback_fence
        or attempt.task_id != task.task_id or attempt.workflow_run_id != run.run_identity
        or task.idempotency_key != reservation.child_publication_key or task.idempotency_scope != "general-task"
        or _task_binding(task) != creation.child_task_binding_digest
        or creation.child_input_artifact_id != task.input_artifact_id
        or creation.child_input_ref != task.typed_input_ref or creation.child_input_digest != task.typed_input_digest
        or artifact.bound_task_id != task.task_id or artifact.payload_sha256 != task.typed_input_digest
        or artifact.metadata_digest != _metadata_digest(artifact)
        or _input_immutable_binding(artifact) != creation.child_input_immutable_digest
        or artifact.state not in {"bound","consumed"}
        or admission.reservation_digest != creation.reservation_digest
        or admission.creation_digest != digest(creation.model_dump(mode="json"))
        or admission.child_task_id != task.task_id or admission.child_attempt_id != attempt.attempt_id
        or admission.child_job_id != run.run_identity
        or attempt.fencing_token != admission.board_fence + 1
        or attempt.outcome != "verified" or attempt.lease_owner is not None or attempt.lease_expires_at is not None
        or admission.child_input_digest != run.input_digest or admission.child_authority_digest != run.authority_digest
        or admission.child_deadline_at != run.deadline_at.replace(tzinfo=timezone.utc).isoformat()
        or authority.get("specialist_delegation_invocation_id") != callback.run_identity
        or authority.get("specialist_delegation_request_digest") != reservation.delegation_request_digest
        or authority.get("specialist_original_parent_id") != original.run_identity
        or run.authority_digest != digest(authority) or run.parent_job_id != callback.run_identity
        or run.parent_fencing_token != reservation.callback_fence
        or run.root_run_identity != original.run_identity or run.branch_depth != 2
        or run.goal_id != task.goal_id or run.goal_revision != task.goal_revision
        or (run.owner_principal_id,run.session_id,run.operator_session_id) !=
            (task.owner_principal_id,task.owner_session_id,task.owner_session_id)):
        deny()
    return creation


def _task_binding(task):
    from src.work_board.general_task import digest
    return digest({key:getattr(task,key) for key in ("task_id","owner_principal_id","owner_session_id",
        "goal_id","goal_revision","capability_id","input_artifact_id","typed_input_ref","typed_input_digest",
        "idempotency_scope","idempotency_key","origin_thread_id")})


def _input_immutable_binding(artifact):
    from src.work_board.general_task import digest
    return digest({key:(getattr(artifact,key).replace(tzinfo=timezone.utc).isoformat()
        if isinstance(getattr(artifact,key),datetime) else getattr(artifact,key))
        for key in ("artifact_id","owner_principal_id","owner_session_id","goal_id","goal_revision",
            "capability_id","capability_version","idempotency_key","payload_sha256","typed_input_ref",
            "size_bytes","bound_task_id","bound_task_revision","created_at","expires_at")})


async def seal_child_creation(db, owner, task, publication):
    """Called inside the existing Board Task/input publication writer."""
    from src.workflows.specialist_delegation import current_delegation, verify_publication_source
    from src.work_board.general_task import digest
    from src.work_board.input_artifacts import _metadata_digest
    from src.work_board.dispatcher import _parse_typed_input
    from src.work_board.contracts import GeneralTaskEnvelope
    verify_publication_source(publication)
    context = await current_delegation(db,publication.invocation_id)
    artifact = await db.get(WorkBoardInputArtifact,task.input_artifact_id,populate_existing=True)
    envelope = GeneralTaskEnvelope.model_validate(_parse_typed_input(task))
    reservation = context.reservation
    if (task.task_id != publication.task_id or task.task_id != reservation.child_task_id
        or task.idempotency_scope != "general-task" or task.idempotency_key != reservation.child_publication_key
        or task.origin_thread_id != context.callback.run_identity
        or (task.owner_principal_id,task.owner_session_id) != (owner.principal_id,owner.session_id)
        or artifact is None or artifact.bound_task_id != task.task_id
        or artifact.payload_sha256 != publication.envelope_digest
        or artifact.metadata_digest != _metadata_digest(artifact)
        or envelope.specialist_handoff != reservation.handoff_ref):
        raise BoardError("specialist_child_publication_changed", "Exact original child publication required", status_code=409)
    fact = SpecialistChildCreationV1(invocation_id=context.callback.run_identity,
        reservation_digest=digest(reservation.model_dump(mode="json")),parent_job_id=context.parent.run_identity,
        parent_creation_digest=context.manifest.creation_digest,owner_principal_id=owner.principal_id,
        original_root_id=owner.session_id,goal_id=task.goal_id,goal_revision=task.goal_revision,
        callback_fence=reservation.callback_fence,child_task_id=task.task_id,
        reserved_attempt_id=reservation.child_attempt_id,reserved_job_id=reservation.child_job_id,
        child_input_artifact_id=task.input_artifact_id,child_input_digest=task.typed_input_digest,
        child_input_metadata_digest=artifact.metadata_digest,child_input_ref=task.typed_input_ref,
        child_input_immutable_digest=_input_immutable_binding(artifact),
        child_task_binding_digest=_task_binding(task),child_plan_digest=digest(envelope.plan.model_dump(mode="json")),
        original_deadline_at=reservation.original_deadline_at,child_deadline_at=reservation.child_deadline_at)
    prior = read_fact(context.callback,CREATION_KEY,SpecialistChildCreationV1)
    if prior is not None:
        if prior != fact:
            raise BoardError("specialist_child_publication_changed", "Original creation receipt changed", status_code=409)
        return fact
    history = json.loads(context.callback.checkpoint_receipts_json or "[]")
    history.append({"checkpoint_id":CREATION_KEY,"state_digest":digest(fact.model_dump(mode="json")),
        "payload":fact.model_dump(mode="json"),"safe":True,"fencing_token":reservation.callback_fence,
        "recorded_at":datetime.now(timezone.utc).isoformat()})
    if len(history) + 4 > 50 or len(json.dumps(history).encode()) + 4 * 65536 > 4 * 1024 * 1024:
        raise BoardError("specialist_proof_capacity", "Original specialist proof capacity exhausted", status_code=409)
    changed = await db.execute(update(WorkflowRunState).where(
        WorkflowRunState.run_identity == context.callback.run_identity,
        WorkflowRunState.revision == context.callback.revision,
        WorkflowRunState.status == "running",WorkflowRunState.lease_owner == reservation.callback_owner,
        WorkflowRunState.fencing_token == reservation.callback_fence).values(
            checkpoint_receipts_json=json.dumps(history,sort_keys=True,separators=(",",":")),
            revision=WorkflowRunState.revision + 1))
    if changed.rowcount != 1:
        raise BoardError("specialist_child_publication_changed", "Original callback publication fence changed", status_code=409)
    return fact


async def seal_child_admission(db, context, task, attempt, child):
    """Same-writer receipt of the actual admitted original child job."""
    from src.work_board.general_task import digest
    creation = read_fact(context.callback,CREATION_KEY,SpecialistChildCreationV1)
    reservation = context.reservation
    if (creation is None or creation.reservation_digest != digest(reservation.model_dump(mode="json"))
        or task.task_id != reservation.child_task_id or attempt.attempt_id != reservation.child_attempt_id
        or child.run_identity != reservation.child_job_id or child.parent_job_id != context.callback.run_identity
        or child.parent_fencing_token != reservation.callback_fence or _task_binding(task) != creation.child_task_binding_digest):
        raise BoardError("specialist_admission_changed", "Actual reserved child admission required", status_code=409)
    fact = SpecialistChildAdmissionV1(invocation_id=context.callback.run_identity,
        reservation_digest=creation.reservation_digest,creation_digest=digest(creation.model_dump(mode="json")),
        child_task_id=task.task_id,child_attempt_id=attempt.attempt_id,child_job_id=child.run_identity,
        board_fence=attempt.fencing_token,board_owner=attempt.lease_owner,
        child_input_digest=child.input_digest,child_authority_digest=child.authority_digest,
        child_deadline_at=child.deadline_at.replace(tzinfo=timezone.utc).isoformat())
    prior = read_fact(context.callback,ADMISSION_KEY,SpecialistChildAdmissionV1)
    if prior is not None:
        if prior != fact:
            raise BoardError("specialist_admission_changed", "Original admitted child receipt changed", status_code=409)
        return fact
    history = json.loads(context.callback.checkpoint_receipts_json or "[]")
    history.append({"checkpoint_id":ADMISSION_KEY,"state_digest":digest(fact.model_dump(mode="json")),
        "payload":fact.model_dump(mode="json"),"safe":True,"fencing_token":reservation.callback_fence,
        "recorded_at":datetime.now(timezone.utc).isoformat()})
    if len(history) + 4 > 50 or len(json.dumps(history).encode()) + 4 * 65536 > 4 * 1024 * 1024:
        raise BoardError("specialist_proof_capacity", "Original specialist proof capacity exhausted", status_code=409)
    changed = await db.execute(update(WorkflowRunState).where(
        WorkflowRunState.run_identity == context.callback.run_identity,
        WorkflowRunState.revision == context.callback.revision,
        WorkflowRunState.status == "running",WorkflowRunState.lease_owner == reservation.callback_owner,
        WorkflowRunState.fencing_token == reservation.callback_fence).values(
            checkpoint_receipts_json=json.dumps(history,sort_keys=True,separators=(",",":")),
            revision=WorkflowRunState.revision + 1))
    if changed.rowcount != 1:
        raise BoardError("specialist_admission_changed", "Original child admission fence changed", status_code=409)
    return fact


async def verify_current_wait(db, context):
    """A sealed original wait authorizes only its already-admitted specialist.

    The callback itself remains paused and cannot contact a tool. The original
    positive claim, finite child set and current root phase must all survive.
    """
    from src.work_board.general_task import digest
    from src.workflows.general_task_guard import _require_callback_reservation, _step_receipt, effective_child_phase
    from src.workflows.job_runtime import _effect_ledger_or_raise
    callback, reservation = context.callback, context.reservation
    wait = read_fact(callback,WAIT_KEY,SpecialistWaitV1)
    creation = read_fact(callback,CREATION_KEY,SpecialistChildCreationV1)
    admission = read_fact(callback,ADMISSION_KEY,SpecialistChildAdmissionV1)
    if wait is None or creation is None or admission is None or reservation is None:
        raise BoardError("specialist_wait_missing","Exact original admitted child wait required",status_code=409)
    _require_callback_reservation(context.parent,context.native_binding,wait.original_claim_fence)
    phase = await effective_child_phase(db,callback,context.parent)
    receipt = _step_receipt(context.manifest,context.native_binding.step_id)
    child = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == wait.child_job_id))
    attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == wait.child_attempt_id))
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == wait.child_task_id))
    artifact = await db.get(WorkBoardInputArtifact,task.input_artifact_id,populate_existing=True) if task else None
    from src.work_board.input_artifacts import _metadata_digest
    intents = [item for item in _effect_ledger_or_raise(callback.effect_receipts_json)
        if item.get("effect_id") == "general:"+context.native_binding.step_id+":"+str(wait.original_claim_fence)
        and item.get("receipt_kind") == "effect" and item.get("effect_type") == "general_tool_call"
        and item.get("status") == "intent" and item.get("fencing_token") == wait.original_claim_fence]
    if (callback.status != "paused" or callback.failure_reason != "specialist_wait"
        or callback.lease_owner is not None or callback.lease_expires_at is not None
        or callback.attempt_count != 1 or callback.fencing_token != wait.original_claim_fence
        or reservation.callback_owner != wait.original_claim_owner or len(intents) != 1
        or digest(intents[0]) != wait.original_intent_digest
        or wait.reservation_digest != digest(reservation.model_dump(mode="json"))
        or wait.creation_digest != context.manifest.creation_digest
        or wait.child_creation_digest != digest(creation.model_dump(mode="json"))
        or wait.child_admission_digest != digest(admission.model_dump(mode="json"))
        or (wait.child_task_id,wait.child_attempt_id,wait.child_job_id) !=
            (reservation.child_task_id,reservation.child_attempt_id,reservation.child_job_id)
        or (wait.child_input_digest,wait.child_authority_digest) !=
            (admission.child_input_digest,admission.child_authority_digest)
        or (wait.original_deadline_at,wait.child_deadline_at) !=
            (reservation.original_deadline_at,reservation.child_deadline_at)
        or receipt.child_job_id != callback.run_identity or receipt.child_attempt_count != 1
        or receipt.child_fence != wait.original_claim_fence or receipt.invocation_id != callback.run_identity
        or receipt.input_digest != context.native_binding.input_digest
        or receipt.descriptor_digest != context.native_binding.descriptor_digest
        or receipt.phase_digest != phase.phase_digest or receipt.approval_binding_digest != phase.approval_binding_digest
        or receipt.status != "running" or receipt.contact_state not in {"not_contacted","contact_started"}
        or child is None or task is None or attempt is None or artifact is None
        or _task_binding(task) != creation.child_task_binding_digest
        or _input_immutable_binding(artifact) != creation.child_input_immutable_digest
        or artifact.metadata_digest != _metadata_digest(artifact)
        or artifact.bound_task_id != task.task_id or artifact.payload_sha256 != task.typed_input_digest
        or child.parent_job_id != callback.run_identity or child.parent_fencing_token != wait.original_claim_fence
        or child.input_digest != wait.child_input_digest or child.authority_digest != wait.child_authority_digest
        or attempt.task_id != task.task_id or attempt.workflow_run_id != child.run_identity
        or attempt.cancel_requested_at is not None
        or task.owner_principal_id != context.task.owner_principal_id or task.owner_session_id != context.task.owner_session_id):
        raise BoardError("specialist_wait_changed","Original sealed specialist wait changed",status_code=409)
    return wait


async def verify_unclaimed_specialist(db,child,task,attempt):
    """Recover one actually admitted child before its first claim, never rebuild."""
    from src.workflows.specialist_delegation import assert_specialist_root_current
    from src.work_board.general_task import digest
    context = await assert_specialist_root_current(db,child)
    wait = await verify_current_wait(db,context)
    admission = read_fact(context.callback,ADMISSION_KEY,SpecialistChildAdmissionV1)
    actual_task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == wait.child_task_id))
    actual_attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == wait.child_attempt_id))
    if (actual_task is None or actual_attempt is None or actual_attempt.ended_at is not None
        or actual_attempt.cancel_requested_at is not None or actual_attempt.workflow_run_id != child.run_identity
        or actual_task.task_id != task.task_id or actual_task.task_revision != task.task_revision
        or actual_attempt.attempt_id != attempt.attempt_id or actual_attempt.fencing_token != attempt.fencing_token
        or actual_attempt.fencing_token != admission.board_fence
        or child.status not in {"accepted","queued"} or child.attempt_count != 0 or child.fencing_token != 0
        or child.effect_receipts_json not in {"[]",""} or child.input_digest != admission.child_input_digest
        or child.authority_digest != admission.child_authority_digest):
        raise BoardError("specialist_wait_child_changed","Original unclaimed specialist admission changed",status_code=409)
    return context
