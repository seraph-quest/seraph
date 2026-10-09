"""Fixed specialist ownership under one original native delegation callback.

The existing Board, native runtime and inference accounting own all execution.
This module binds their existing rows; it neither runs another queue nor renews
the original proposal group's authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from weakref import WeakKeyDictionary

from pydantic import Field
from sqlalchemy import select, text, update

from src.db.models import WorkBoardAttempt, WorkBoardTask, WorkflowRunState
from src.work_board.contracts import ClosedTaskModel, TaskDigest, TaskIdentity, GeneralTaskArtifactRef
from src.work_board.repository import BoardError
from src.workflows.delegation_contracts import DelegateRequest

DELEGATION_KEY = "general:delegation:reservation:v1"


def delegation_descriptor():
    """Public data schema; original Task/step identities stay server-owned."""
    from src.work_board.contracts import ToolDescriptor
    from src.work_board.general_task import digest
    from src.workflows.delegation_contracts import ChildResult, DelegateLimits
    from src.tools.policy import get_task_policy_snapshot
    schema = DelegateRequest.model_json_schema()
    schema.pop("$defs", None)
    schema["properties"]["limits"] = DelegateLimits.model_json_schema()
    for field in ("parent_task_id", "step_id"):
        schema["properties"].pop(field)
        schema["required"].remove(field)
    return ToolDescriptor(tool_id="delegate_task", version="1", input_schema=schema,
        output_schema=ChildResult.model_json_schema(), effects=["delegation"],
        permissions=["capability_execute"], deadline=900,
        verifier="specialist_child_physical_readback.v1",
        policy_digest=digest(get_task_policy_snapshot()))


class DelegationReservationV1(ClosedTaskModel):
    """Content-free immutable reservation before planning or task publication."""
    schema_version: str = Field(default="SpecialistDelegationReservation.v1",
        pattern=r"^SpecialistDelegationReservation\.v1$")
    delegation_invocation_id: str = Field(min_length=1, max_length=128)
    delegation_request_digest: TaskDigest
    parent_job_id: str = Field(min_length=1, max_length=128)
    parent_task_id: TaskIdentity
    parent_attempt_id: TaskIdentity
    parent_creation_digest: TaskDigest
    selected_grant_digest: TaskDigest
    original_group_digest: TaskDigest
    original_root_id: str = Field(min_length=1, max_length=128)
    owner_principal_id: str = Field(min_length=1, max_length=128)
    goal_id: str = Field(min_length=1, max_length=128)
    goal_revision: int = Field(ge=1)
    callback_fence: int = Field(ge=1)
    callback_owner: str = Field(min_length=1, max_length=128)
    child_publication_key: str = Field(pattern=r"^specialist:[a-f0-9]{64}$")
    child_task_id: TaskIdentity
    child_attempt_id: TaskIdentity
    child_job_id: TaskIdentity
    handoff_ref: GeneralTaskArtifactRef
    handoff_producer_tokens: list[TaskDigest] = Field(max_length=60)
    handoff_vault_digest: TaskDigest
    original_deadline_at: str = Field(min_length=1, max_length=64)
    child_deadline_at: str = Field(min_length=1, max_length=64)


_PUBLICATION_SEAL = object()
_PUBLICATIONS = WeakKeyDictionary()


@dataclass(frozen=True, eq=False)
class _SpecialistPublication:
    invocation_id: str
    reservation_digest: str
    envelope_digest: str
    task_id: str
    _seal: object


async def specialist_publication(db, context, envelope):
    from src.work_board.general_task import digest
    from src.work_board.input_artifacts import _canonical_json
    import hashlib
    current = await current_delegation(db, context.callback.run_identity)
    if current.request != context.request or current.reservation != context.reservation:
        _deny()
    witness = _SpecialistPublication(current.callback.run_identity,
        digest(current.reservation.model_dump(mode="json")),
        hashlib.sha256(_canonical_json({"schema_version":1,"capability_id":"agent.task.v1",
            "input":envelope.model_dump(mode="json", exclude_none=True)})).hexdigest(),
        current.reservation.child_task_id, _PUBLICATION_SEAL)
    _PUBLICATIONS[witness] = (witness.invocation_id,witness.reservation_digest,witness.envelope_digest,witness.task_id)
    return witness


def verify_publication_source(witness):
    if (type(witness) is not _SpecialistPublication or witness._seal is not _PUBLICATION_SEAL
        or _PUBLICATIONS.get(witness) != (witness.invocation_id,witness.reservation_digest,witness.envelope_digest,witness.task_id)):
        _deny("specialist_delegation_publication_denied")


async def verify_specialist_publication(db, owner, request, witness):
    """The existing Board writer consumes an original reserved publication."""
    from src.work_board.general_task import digest
    from src.db.models import WorkBoardInputArtifact
    verify_publication_source(witness)
    context = await current_delegation(db, witness.invocation_id)
    artifact = await db.get(WorkBoardInputArtifact, request.input_artifact_id, populate_existing=True)
    if (digest(context.reservation.model_dump(mode="json")) != witness.reservation_digest
        or witness.task_id != context.reservation.child_task_id
        or (owner.principal_id, owner.session_id) != (context.task.owner_principal_id, context.task.owner_session_id)
        or request.idempotency_scope != "general-task"
        or request.idempotency_key != context.reservation.child_publication_key
        or request.origin_thread_id != witness.invocation_id
        or request.capability_id != "agent.task.v1" or request.goal_id != context.task.goal_id
        or request.goal_revision != context.task.goal_revision
        or artifact is None or artifact.payload_sha256 != witness.envelope_digest
        or artifact.owner_principal_id != owner.principal_id or artifact.owner_session_id != owner.session_id):
        _deny("specialist_delegation_publication_changed")
    return witness.task_id


@dataclass(frozen=True)
class DelegationContext:
    parent: object
    task: object
    attempt: object
    manifest: object
    envelope: object
    callback: object
    native_binding: object
    request: DelegateRequest
    reservation: DelegationReservationV1 | None


def _deny(code="specialist_delegation_binding_changed"):
    raise BoardError(code, "Original fixed specialist delegation binding required", status_code=409)


def read_reservation(run):
    """A damaged reservation is never interpreted as permission to start over."""
    from src.work_board.general_task import digest
    try:
        history = json.loads(run.checkpoint_receipts_json or "[]")
        if not isinstance(history, list):
            raise ValueError()
        matches = [item for item in history if isinstance(item, dict)
            and item.get("checkpoint_id") == DELEGATION_KEY]
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError()
        row = matches[0]
        reservation = DelegationReservationV1.model_validate(row["payload"])
        if (row.get("safe") is not True
            or row.get("state_digest") != digest(reservation.model_dump(mode="json"))
            or row.get("fencing_token") != reservation.callback_fence
            or reservation.delegation_invocation_id != run.run_identity):
            raise ValueError()
        return reservation
    except (ValueError, TypeError, KeyError):
        _deny("specialist_delegation_reservation_corrupt")


async def current_delegation(db, invocation_id, *, callback_fence=None,
                             require_reservation=True):
    """Read the real original callback and private request, never caller claims."""
    from src.workflows.general_task_guard import (
        assert_general_task_child_current, assert_general_task_child_phase_current, child_binding, read_manifest,
    )
    from src.work_board.general_task_runtime_artifacts import (
        read_bound_native_tool_input, verify_general_task_manifest,
    )
    from src.work_board.general_task import digest
    callback = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == invocation_id))
    if callback is None:
        _deny()
    waiting = callback.status == "paused" and callback.failure_reason == "specialist_wait"
    if waiting:
        await assert_general_task_child_phase_current(db, callback)
    else:
        await assert_general_task_child_current(db, callback)
    native = child_binding(callback)
    # A specialist may not become another delegation owner. This exact native
    # invocation must belong to the original transport-depth-zero Board root.
    parent = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == native.parent_job_id))
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == native.task_id)
        .execution_options(populate_existing=True))
    attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == native.attempt_id)
        .execution_options(populate_existing=True))
    if (parent is None or task is None or attempt is None
        or parent.parent_job_id is not None or parent.branch_depth != 0
        or parent.status != "paused" or (callback.status != "running" and not waiting)
        or (callback_fence is not None and callback.fencing_token != callback_fence)):
        _deny()
    manifest = read_manifest(parent)
    if manifest is None or manifest.phase != "native_wait":
        _deny()
    envelope = await verify_general_task_manifest(db, parent, task, attempt, manifest)
    private = read_bound_native_tool_input(callback, native)
    if private.tool_id != "delegate_task":
        _deny()
    # Root and step identities belong to the canonical native invocation,
    # never to model-selected tool arguments.
    request = DelegateRequest.model_validate({**private.inputs,
        "parent_task_id": task.task_id, "step_id": native.step_id})
    if request.parent_task_id != task.task_id or request.step_id != native.step_id:
        _deny()
    reservation = read_reservation(callback)
    if require_reservation and reservation is None:
        _deny("specialist_delegation_reservation_missing")
    if reservation is not None:
        if reservation.child_job_id != "work-board:" + reservation.child_task_id + ":" + reservation.child_attempt_id:
            _deny()
        expected = dict(delegation_invocation_id=callback.run_identity,
            delegation_request_digest=digest(request.model_dump(mode="json")),
            parent_job_id=parent.run_identity, parent_task_id=task.task_id,
            parent_attempt_id=attempt.attempt_id,
            parent_creation_digest=manifest.creation_digest,
            selected_grant_digest=manifest.selected_grant_digest,
            original_group_digest=digest(envelope.proposal_group.model_dump(mode="json")),
            original_root_id=task.owner_session_id, owner_principal_id=task.owner_principal_id,
            goal_id=task.goal_id, goal_revision=task.goal_revision,
            callback_fence=callback.fencing_token, callback_owner=(reservation.callback_owner if waiting else callback.lease_owner),
            child_publication_key="specialist:" + digest([callback.run_identity, native.input_digest]),
            child_task_id=reservation.child_task_id, child_attempt_id=reservation.child_attempt_id,
            child_job_id=reservation.child_job_id,
            handoff_ref=reservation.handoff_ref,
            handoff_producer_tokens=reservation.handoff_producer_tokens,
            handoff_vault_digest=reservation.handoff_vault_digest,
            original_deadline_at=manifest.original_deadline_at.isoformat(),
            child_deadline_at=reservation.child_deadline_at)
        if reservation != DelegationReservationV1(**expected):
            _deny()
        from src.workflows.delegation_contracts import _producer_tokens, _vault_state
        from src.work_board.contracts import WorkBoardOwner
        selected_owner = WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id)
        if (len(reservation.handoff_producer_tokens) != 5 * len(request.evidence_refs)
            or tuple(reservation.handoff_producer_tokens) != await _producer_tokens(db, selected_owner, request.evidence_refs)
            or reservation.handoff_vault_digest != await _vault_state(db)):
            _deny("specialist_handoff_changed")
        cutoff = datetime.fromisoformat(reservation.child_deadline_at)
        if (cutoff.tzinfo is None or cutoff > manifest.native_deadline_at
            or cutoff <= datetime.now(timezone.utc)):
            _deny("specialist_delegation_deadline")
    context = DelegationContext(parent, task, attempt, manifest, envelope,
        callback, native, request, reservation)
    if waiting:
        from src.workflows.specialist_lifecycle import verify_current_wait
        await verify_current_wait(db,context)
    return context


async def validate_specialist_accounting(db, group, binding, initial):
    """Fixed role validator called by the existing cost-reservation writer."""
    from src.work_board.general_task import digest
    context = await current_delegation(db, binding.get("delegation_invocation_id"),
        callback_fence=binding.get("parent_fence"))
    expected = {"task_id": context.task.task_id, "task_attempt_id": context.attempt.attempt_id,
        "plan_revision": context.manifest.plan_revision,
        "selected_grant_digest": context.manifest.selected_grant_digest,
        "parent_owner": context.callback.lease_owner, "parent_fence": context.callback.fencing_token,
        "delegation_invocation_id": context.callback.run_identity,
        "delegation_request_digest": digest(context.request.model_dump(mode="json"))}
    if group != context.envelope.proposal_group or any(binding.get(key) != value for key, value in expected.items()):
        _deny("specialist_delegation_accounting_changed")
    provenance = context.envelope.proposal_provenance
    if provenance is not None:
        from src.work_board.general_task_proposal import proposal_provenance
        if initial is None or proposal_provenance(initial.model_dump(mode="json"), group) != provenance:
            _deny("specialist_delegation_provenance_changed")
    elif initial is not None:
        _deny("specialist_delegation_provenance_changed")
    return context


async def validate_specialist_planning(db, group, binding, task_input, descriptors):
    """Only the reserved explicit instruction and intersected tools may plan."""
    from src.db.models import InferenceCostReservation
    from src.workflows.general_task_accounting import entry_for
    rows = list((await db.execute(select(InferenceCostReservation).where(
        InferenceCostReservation.owner_id == group.owner_principal_id))).scalars())
    initial = next((row for row in rows if entry_for(row)
        and entry_for(row)["group"]["group_id"] == group.group_id
        and entry_for(row)["role"] == "initial_proposal"), None)
    context = await validate_specialist_accounting(db, group, binding, initial)
    selected = [item for item in context.envelope.descriptors
        if item.tool_id in context.request.allowed_tool_ids]
    if (task_input.intent != context.request.instruction
        or task_input.goal_ref != context.task.goal_id
        or task_input.evidence_refs != context.request.evidence_refs
        or task_input.limits != context.envelope.task_input.limits
        or task_input.inference_egress_acknowledged != context.envelope.task_input.inference_egress_acknowledged
        or task_input.requested_output != {"type": "object"}
        or descriptors != selected or len(selected) != len(context.request.allowed_tool_ids)):
        _deny("specialist_delegation_planning_changed")
    return context


def is_specialist_root(run):
    try:
        return json.loads(run.declared_authority_json or "{}").get("specialist_delegation_invocation_id") is not None
    except (ValueError, TypeError):
        _deny()


async def specialist_for_task(db, task):
    """Resolve a reserved child publication; UI linkage supplies no authority."""
    from src.work_board.dispatcher import _parse_typed_input
    from src.work_board.contracts import GeneralTaskEnvelope
    if not task.idempotency_key.startswith("specialist:"):
        return None
    if not task.origin_thread_id or task.idempotency_scope != "general-task":
        _deny()
    context = await current_delegation(db, task.origin_thread_id)
    envelope = GeneralTaskEnvelope.model_validate(_parse_typed_input(task))
    if (task.idempotency_key != context.reservation.child_publication_key
        or task.task_id != context.reservation.child_task_id
        or task.owner_principal_id != context.task.owner_principal_id
        or task.owner_session_id != context.task.owner_session_id
        or task.goal_id != context.task.goal_id or task.goal_revision != context.task.goal_revision
        or envelope.proposal_group != context.envelope.proposal_group
        or envelope.proposal_provenance != context.envelope.proposal_provenance
        or envelope.task_input.intent != context.request.instruction
        or envelope.task_input.limits != context.envelope.task_input.limits
        or envelope.task_input.evidence_refs != context.request.evidence_refs
        or envelope.specialist_handoff != context.reservation.handoff_ref
        or envelope.plan is None or len(envelope.plan.steps) > context.request.limits.max_steps
        or any(step.tool_id not in context.request.allowed_tool_ids for step in envelope.plan.steps)):
        _deny()
    return context


async def assert_specialist_root_current(db, run):
    from src.work_board.general_task import digest
    authority = json.loads(run.declared_authority_json or "{}")
    invocation = authority.get("specialist_delegation_invocation_id")
    if not invocation:
        _deny()
    attempt = await db.scalar(select(WorkBoardAttempt).where(
        WorkBoardAttempt.workflow_run_id == run.run_identity))
    task = await db.scalar(select(WorkBoardTask).where(
        WorkBoardTask.task_id == attempt.task_id)) if attempt else None
    if task is None:
        _deny()
    context = await specialist_for_task(db, task)
    if (context is None or context.callback.run_identity != invocation
        or attempt.attempt_id != context.reservation.child_attempt_id
        or run.run_identity != context.reservation.child_job_id
        or run.job_kind != "agent.task.v1" or run.capability_version != "1"
        or run.owner_kind != "user" or run.branch_depth != 2
        or run.parent_job_id != invocation or run.parent_run_identity != invocation
        or run.root_run_identity != context.parent.run_identity
        or run.parent_fencing_token != context.callback.fencing_token
        or run.owner_principal_id != context.task.owner_principal_id
        or run.session_id != context.task.owner_session_id
        or run.operator_session_id != context.task.owner_session_id
        or run.goal_id != context.task.goal_id or run.goal_revision != context.task.goal_revision
        or authority.get("specialist_delegation_request_digest") != context.reservation.delegation_request_digest
        or authority.get("specialist_original_parent_id") != context.parent.run_identity
        or run.authority_digest != digest(authority)
        or run.deadline_at.replace(tzinfo=timezone.utc) > datetime.fromisoformat(context.reservation.child_deadline_at)):
        _deny()
    object.__setattr__(run, "_specialist_parent_snapshot", _SpecialistParentSnapshot(
        run.run_identity,run.fencing_token,context.callback.run_identity,context.callback.fencing_token,
        context.callback.status,context.callback.failure_reason,context.callback.lease_owner,
        context.callback.lease_expires_at,context.callback.checkpoint_receipts_json,
        context.callback.effect_receipts_json,context.parent.run_identity,context.parent.checkpoint_receipts_json,
        _SPECIALIST_SQL_SEAL))
    return context


_SPECIALIST_SQL_SEAL = object()


@dataclass(frozen=True)
class _SpecialistParentSnapshot:
    child_id: str
    child_fence: int
    callback_id: str
    callback_fence: int
    callback_status: str
    callback_reason: str | None
    callback_owner: str | None
    callback_expiry: datetime | None
    callback_checkpoints: str
    callback_effects: str
    original_id: str
    original_checkpoints: str
    seal: object


def append_specialist_parent_gate(conditions,run,*,now):
    """CAS exact current source-issued parent snapshots; never generic pause."""
    if not is_specialist_root(run):
        return False
    from sqlalchemy import false
    from sqlalchemy.orm import aliased
    snapshot = getattr(run,"_specialist_parent_snapshot",None)
    if (type(snapshot) is not _SpecialistParentSnapshot or snapshot.seal is not _SPECIALIST_SQL_SEAL
        or snapshot.child_id != run.run_identity or snapshot.child_fence != run.fencing_token
        or snapshot.callback_id != run.parent_job_id or snapshot.callback_fence != run.parent_fencing_token):
        conditions.append(false())
        return True
    callback,original = aliased(WorkflowRunState),aliased(WorkflowRunState)
    contact = (callback.status == "running") & (callback.lease_owner.is_not(None)) & (callback.lease_expires_at > now)
    waiting = ((callback.status == "paused") & (callback.failure_reason == "specialist_wait")
        & callback.lease_owner.is_(None) & callback.lease_expires_at.is_(None))
    conditions.append(select(callback.id).join(original,original.run_identity == snapshot.original_id).where(
        callback.run_identity == snapshot.callback_id,callback.parent_job_id == original.run_identity,
        callback.status == snapshot.callback_status,callback.failure_reason == snapshot.callback_reason,
        callback.lease_owner == snapshot.callback_owner,callback.lease_expires_at == snapshot.callback_expiry,
        callback.fencing_token == snapshot.callback_fence,callback.checkpoint_receipts_json == snapshot.callback_checkpoints,
        callback.effect_receipts_json == snapshot.callback_effects,callback.deadline_at > now,
        contact if snapshot.callback_status == "running" else waiting,
        original.status == "paused",original.failure_reason == "general_task_native_wait",
        original.checkpoint_receipts_json == snapshot.original_checkpoints,original.deadline_at > now).exists())
    return True


def specialist_admission_check(task, attempt, spec):
    """Canonical admission callback checks the reserved Board publication."""
    async def check(db, child):
        from src.work_board.general_task import digest
        current_task = await db.scalar(select(WorkBoardTask).where(
            WorkBoardTask.task_id == task.task_id).execution_options(populate_existing=True))
        current_attempt = await db.scalar(select(WorkBoardAttempt).where(
            WorkBoardAttempt.attempt_id == attempt.attempt_id).execution_options(populate_existing=True))
        if (current_task is None or current_attempt is None
            or current_task.task_revision != task.task_revision
            or current_attempt.task_id != task.task_id or current_attempt.ended_at
            or current_attempt.cancel_requested_at
            or current_attempt.fencing_token != attempt.fencing_token
            or current_attempt.lease_owner != attempt.lease_owner
            or current_attempt.workflow_run_id is not None):
            _deny()
        context = await specialist_for_task(db, current_task)
        if (context is None or child.run_identity != spec.identity.job_id
            or child.parent_job_id != context.callback.run_identity
            or child.parent_fencing_token != context.callback.fencing_token
            or child.input_digest != digest(spec.inputs)
            or child.authority_digest != digest(spec.declared_authority)
            or child.deadline_at.replace(tzinfo=timezone.utc) > datetime.fromisoformat(context.reservation.child_deadline_at)):
            _deny()
        from src.workflows.specialist_lifecycle import seal_child_admission
        await seal_child_admission(db,context,current_task,current_attempt,child)
    return check


async def execute_specialist(jobs, *, service, invocation_id, fencing_token, principal, durable_wait=False):
    """Continue one reserved real Board child through the canonical dispatcher."""
    from src.work_board.contracts import GeneralTaskCreate, GeneralTaskInput, WorkBoardOwner
    from src.work_board.general_task import digest
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.workflows.delegation_contracts import ChildResult, verify_child_result
    async with jobs._session() as db:
        context = await current_delegation(db, invocation_id,
            callback_fence=fencing_token, require_reservation=False)
        if (principal.principal_id != context.task.owner_principal_id
            or principal.operator_session_id != context.task.owner_session_id
            or principal.job_id != invocation_id or not principal.authenticated or principal.revoked):
            _deny()
        owner = context.callback.lease_owner
    await reserve_delegation(jobs, invocation_id, service=service, owner=owner, fence=fencing_token)
    async with jobs._session() as db:
        context = await current_delegation(db, invocation_id, callback_fence=fencing_token)
        from src.workflows.specialist_evidence import read_specialist_handoff
        await read_specialist_handoff(db, context)
        operator = WorkBoardOwner(principal_id=context.task.owner_principal_id,
            session_id=context.task.owner_session_id)
        child_task = await db.scalar(select(WorkBoardTask).where(
            WorkBoardTask.owner_principal_id == operator.principal_id,
            WorkBoardTask.owner_session_id == operator.session_id,
            WorkBoardTask.idempotency_scope == "general-task",
            WorkBoardTask.idempotency_key == context.reservation.child_publication_key))
        if child_task is None:
            if service.planner is None:
                _deny("specialist_delegation_planner_unavailable")
            descriptors = [item for item in context.envelope.descriptors
                if item.tool_id in context.request.allowed_tool_ids]
            _, tool_digest = service.snapshot()
            task_input = GeneralTaskInput(goal_ref=context.task.goal_id,
                intent=context.request.instruction, evidence_refs=context.request.evidence_refs,
                requested_output={"type": "object"}, limits=context.envelope.task_input.limits,
                tool_set_digest=tool_digest,
                inference_egress_acknowledged=context.envelope.task_input.inference_egress_acknowledged)
            binding = {"role": "specialist", "task_id": context.task.task_id,
                "task_attempt_id": context.attempt.attempt_id,
                "plan_revision": context.manifest.plan_revision,
                "selected_grant_digest": context.manifest.selected_grant_digest,
                "parent_owner": owner, "parent_fence": fencing_token,
                "delegation_invocation_id": invocation_id,
                "delegation_request_digest": digest(context.request.model_dump(mode="json"))}
            proposed = await service.planner.propose_specialist(db, operator,
                group=context.envelope.proposal_group, binding=binding, task_input=task_input,
                descriptors=descriptors, original_provenance=context.envelope.proposal_provenance,
                request_key=context.reservation.child_publication_key)
            if proposed.plan is None or proposed.error:
                _deny("specialist_delegation_plan_invalid")
            created = await service.create(db, operator, GeneralTaskCreate(
                goal_revision=context.task.goal_revision,
                idempotency_key=context.reservation.child_publication_key,
                input=task_input, plan=proposed.plan,
                expected_plan_revision=proposed.plan.revision, accept=True),
                _specialist_context=context)
            child_task = created.task
        child_task_id = child_task.task_id
    dispatcher = WorkBoardDispatcher(jobs=jobs, general_tasks=service,
        repository=service.repository, session_provider=jobs._session)
    async with jobs._session() as db:
        child_task = await service.repository.get_task(db, operator, child_task_id)
        await specialist_for_task(db, child_task)
        from src.db.models import WorkBoardStatus
        if child_task.status == WorkBoardStatus.todo:
            mutation = await service.repository.promote_task_ready(db, child_task_id,
                expected_revision=child_task.task_revision,
                actor_principal_id=dispatcher.runner_id, actor_session_id=dispatcher.runner_session)
            child_task = mutation.task
    async with jobs._session() as db:
        child_task = await service.repository.get_task(db, operator, child_task_id)
        if child_task.status == WorkBoardStatus.ready:
            claim = await service.repository.claim_ready_task(db, child_task_id,
                expected_revision=child_task.task_revision, lease_owner=dispatcher.runner_id)
        else:
            claim = None
    if claim is not None:
        admitted = await dispatcher._admit_execute_project(claim,_defer_specialist=durable_wait)
        if durable_wait and not admitted.get("deferred_specialist"):
            _deny("specialist_delegation_child_admission_pending")
    if durable_wait:
        from src.workflows.specialist_lifecycle import pause_specialist_callback
        raise await pause_specialist_callback(jobs,invocation_id,owner=owner,fence=fencing_token)
    async with jobs._session() as db:
        child_task = await service.repository.get_task(db, operator, child_task_id)
        await specialist_for_task(db, child_task)
        attempt = await db.scalar(select(WorkBoardAttempt).where(
            WorkBoardAttempt.task_id == child_task_id).order_by(WorkBoardAttempt.created_at.desc()).limit(1))
        if attempt is None or not attempt.workflow_run_id:
            _deny("specialist_delegation_child_admission_pending")
        run = await jobs._fetch(db, attempt.workflow_run_id)
        await assert_specialist_root_current(db, run)
        from src.work_board.review import _verified_workflow_readback
        if child_task.status != WorkBoardStatus.done or await _verified_workflow_readback(db, child_task, attempt) is None:
            _deny("specialist_delegation_child_unresolved")
        from src.work_board.input_artifacts import _safe_file_bytes
        from src.workspace import canonical_workspace_root
        from config.settings import settings
        effects = json.loads(run.effect_receipts_json or "[]")
        artifacts = [item for item in json.loads(run.artifact_receipts_json or "[]")
            if item.get("exists") and item.get("artifact_type") == "general_task_step"
            and any(effect.get("receipt_kind") == "readback" and effect.get("status") == "succeeded"
                and effect.get("target_path") == item.get("file_path")
                and effect.get("content_sha256") == item.get("content_sha256") for effect in effects)]
        refs = []
        for item in artifacts:
            _safe_file_bytes(canonical_workspace_root(settings.workspace_dir) / item["file_path"],
                expected_digest=item["content_sha256"], expected_size=item["size_bytes"])
            refs.append("artifact:" + item["artifact_id"])
        if not refs:
            _deny("specialist_delegation_child_output_missing")
        result = ChildResult(child_id=child_task_id, artifact_refs=refs, unresolved=[], summary_ref=refs[-1])
        verify_child_result(result, child_id=child_task_id, verified_artifact_refs=refs)
        return result.model_dump(mode="json")


async def reserve_delegation(jobs, invocation_id, *, service, owner, fence):
    """Fixed protected writer reserves this original child before planning.

    Physical evidence staging and secret checks precede the short SQL writer;
    canonical rows and explicit bindings are checked again before publication.
    """
    from src.work_board.general_task import digest
    from src.work_board.contracts import WorkBoardOwner
    from src.workflows.delegation_contracts import (
        verify_delegate_request, validate_delegation_instruction, resolve_delegation_evidence,
        recheck_delegation_evidence,
    )
    async with jobs._session() as db:
        context = await current_delegation(db, invocation_id,
            callback_fence=fence, require_reservation=False)
        if context.reservation is not None:
            return context.reservation
        operator = WorkBoardOwner(principal_id=context.task.owner_principal_id,
            session_id=context.task.owner_session_id)
        evidence_handoffs = await resolve_delegation_evidence(service, db, operator, context.envelope,
            context.request.evidence_refs)
        await validate_delegation_instruction(db, context.request)
        await recheck_delegation_evidence(db, operator, context.envelope, evidence_handoffs)
        from uuid import uuid4
        from src.work_board.contracts import SpecialistEvidenceHandoffV1
        from src.work_board.general_task_runtime_artifacts import stage_task_artifact, verify_staged_task_artifact
        child_task_id, child_attempt_id = uuid4().hex, uuid4().hex
        copied = SpecialistEvidenceHandoffV1(parent_job_id=context.parent.run_identity,
            creation_digest=context.manifest.creation_digest, invocation_id=invocation_id,
            request_digest=digest(context.request.model_dump(mode="json")), child_task_id=child_task_id,
            owner_principal_id=evidence_handoffs.owner_principal_id,
            original_root_id=evidence_handoffs.original_root_id, group_digest=evidence_handoffs.group_digest,
            producer_tokens=list(evidence_handoffs.producer_tokens), vault_state_digest=evidence_handoffs.vault_state_digest,
            entries=[entry.model_dump(mode="json") for entry in evidence_handoffs])
        staged_copy = stage_task_artifact(parent_job_id=context.parent.run_identity,
            creation_digest=context.manifest.creation_digest, payload=copied)
        _copied, copied_record = verify_staged_task_artifact(staged_copy,
            parent_job_id=context.parent.run_identity, creation_digest=context.manifest.creation_digest)
        await recheck_delegation_evidence(db, operator, context.envelope, evidence_handoffs)
        await db.rollback()
        await db.execute(text("BEGIN IMMEDIATE"))
        context = await current_delegation(db, invocation_id,
            callback_fence=fence, require_reservation=False)
        await recheck_delegation_evidence(db, operator, context.envelope, evidence_handoffs)
        jobs._assert_lease(context.callback, owner=owner, fencing_token=fence)
        if context.reservation is not None:
            return context.reservation
        siblings = list((await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.parent_job_id == context.parent.run_identity))).scalars())
        retained = []
        for sibling in siblings:
            reserved = read_reservation(sibling)
            if reserved is not None:
                from src.work_board.general_task_runtime_artifacts import read_bound_native_tool_input
                from src.workflows.general_task_guard import child_binding
                original_binding = child_binding(sibling)
                original = DelegateRequest.model_validate({**read_bound_native_tool_input(
                    sibling, original_binding).inputs, "parent_task_id": original_binding.task_id,
                    "step_id": original_binding.step_id})
                retained.append({"status": sibling.status, "request": original.model_dump(mode="json")})
        verify_delegate_request(context.request, parent_task_id=context.task.task_id,
            parent_limits=context.envelope.task_input.limits,
            parent_allowed_tool_ids=[item.tool_id for item in context.envelope.descriptors],
            parent_evidence_refs=context.envelope.task_input.evidence_refs,
            existing_children=retained, parent_is_child=False)
        reservation = DelegationReservationV1(delegation_invocation_id=invocation_id,
            delegation_request_digest=digest(context.request.model_dump(mode="json")),
            parent_job_id=context.parent.run_identity, parent_task_id=context.task.task_id,
            parent_attempt_id=context.attempt.attempt_id,
            parent_creation_digest=context.manifest.creation_digest,
            selected_grant_digest=context.manifest.selected_grant_digest,
            original_group_digest=digest(context.envelope.proposal_group.model_dump(mode="json")),
            original_root_id=context.task.owner_session_id, owner_principal_id=context.task.owner_principal_id,
            goal_id=context.task.goal_id, goal_revision=context.task.goal_revision,
            callback_fence=fence, callback_owner=owner,
            child_publication_key="specialist:" + digest([invocation_id, context.native_binding.input_digest]),
            child_task_id=child_task_id, child_attempt_id=child_attempt_id,
            child_job_id="work-board:" + child_task_id + ":" + child_attempt_id,
            handoff_ref=staged_copy.reference,
            handoff_producer_tokens=list(evidence_handoffs.producer_tokens),
            handoff_vault_digest=evidence_handoffs.vault_state_digest,
            original_deadline_at=context.manifest.original_deadline_at.isoformat(),
            child_deadline_at=min(context.manifest.native_deadline_at,
                datetime.now(timezone.utc) + timedelta(seconds=context.request.limits.wall_seconds)).isoformat())
        payload = reservation.model_dump(mode="json")
        history = json.loads(context.callback.checkpoint_receipts_json or "[]")
        if len(history) >= 49:
            _deny("specialist_delegation_checkpoint_capacity")
        history.append({"checkpoint_id": DELEGATION_KEY, "state_digest": digest(payload),
            "payload": payload, "safe": True, "fencing_token": fence,
            "recorded_at": datetime.now(timezone.utc).isoformat()})
        artifacts = json.loads(context.callback.artifact_receipts_json or "[]")
        artifacts.append(copied_record)
        if (len(artifacts) > 50 or len(json.dumps(artifacts).encode()) > 4 * 1024 * 1024
            or len(json.dumps(history).encode()) + 3 * 65536 > 4 * 1024 * 1024
            or len(history) + 3 > 50):
            _deny("specialist_delegation_checkpoint_capacity")
        changed = await db.execute(update(WorkflowRunState).where(
            WorkflowRunState.run_identity == invocation_id,
            WorkflowRunState.revision == context.callback.revision,
            WorkflowRunState.status == "running", WorkflowRunState.lease_owner == owner,
            WorkflowRunState.fencing_token == fence).values(
                checkpoint_receipts_json=json.dumps(history, sort_keys=True, separators=(",", ":")),
                artifact_receipts_json=json.dumps(artifacts, sort_keys=True, separators=(",", ":")),
                revision=WorkflowRunState.revision + 1))
        if changed.rowcount != 1:
            _deny()
        return reservation
