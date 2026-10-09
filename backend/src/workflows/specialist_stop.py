"""Fixed specialist stop lineage. This proof never closes a callback."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from pydantic import Field
from sqlalchemy import select

from src.db.models import WorkBoardTask, WorkBoardAttempt, WorkBoardInputArtifact, WorkflowRunState, InferenceCostReservation
from src.work_board.contracts import ClosedTaskModel, TaskDigest, TaskIdentity, GeneralTaskToolClosureV1
from src.work_board.general_task import digest
from src.work_board.repository import BoardError


def stop_checkpoint_id(invocation_id):
    return "general:specialist-stop:" + digest(invocation_id)


class SpecialistStoppedJobV1(ClosedTaskModel):
    job_id: TaskIdentity
    parent_job_id: TaskIdentity
    input_digest: TaskDigest
    authority_digest: TaskDigest
    original_revision: int = Field(ge=0)
    original_fence: int = Field(ge=0)
    current_revision: int = Field(ge=0)
    current_fence: int = Field(ge=0)
    attempts: int = Field(ge=0, le=1)
    original_status: str = Field(max_length=64)
    current_status: Literal["cancelled", "blocked", "succeeded", "degraded"]
    effect_digest: TaskDigest
    artifact_digest: TaskDigest
    checkpoint_digest: TaskDigest
    immutable_completed: bool


class SpecialistDelegationCancelV1(ClosedTaskModel):
    schema_version: Literal["SpecialistDelegationCancel.v1"] = "SpecialistDelegationCancel.v1"
    parent_job_id: TaskIdentity
    parent_creation_digest: TaskDigest
    invocation_id: TaskIdentity
    original_binding_digest: TaskDigest
    reservation_digest: TaskDigest
    creation_digest: TaskDigest | None = None
    admission_digest: TaskDigest | None = None
    wait_digest: TaskDigest | None = None
    child_task_id: TaskIdentity
    child_attempt_id: TaskIdentity
    child_job_id: TaskIdentity
    child_input_digest: TaskDigest | None = None
    child_input_metadata_digest: TaskDigest | None = None
    child_task_binding_digest: TaskDigest | None = None
    original_task_revision: int | None = Field(default=None, ge=1)
    current_task_revision: int | None = Field(default=None, ge=1)
    original_board_fence: int | None = Field(default=None, ge=1)
    current_board_fence: int | None = Field(default=None, ge=1)
    original_attempt_outcome: str | None = Field(default=None, max_length=128)
    completed_child: bool = False
    jobs: list[SpecialistStoppedJobV1] = Field(default_factory=list, max_length=17)
    observed_closures: list[GeneralTaskToolClosureV1] = Field(default_factory=list, max_length=16)
    cost_membership_digest: TaskDigest
    cost_operation_ids: list[TaskIdentity] = Field(default_factory=list, max_length=12)
    cost_unresolved: bool = False
    stop_action: Literal["cancel", "pause"]
    no_learning: Literal[True] = True


@dataclass
class CompiledStop:
    fact: SpecialistDelegationCancelV1
    rows: list
    task: object | None
    attempt: object | None
    artifact: object | None
    goal: object | None


def deny():
    raise BoardError("specialist_stop_binding_changed", "Original sealed specialist stop set changed", status_code=409)


async def compile_specialist_stops(jobs, db, parent, manifest, callbacks, *, action):
    """Compile the complete exact bounded set before any fence mutation."""
    from src.workflows.specialist_delegation import read_reservation
    from src.workflows.specialist_lifecycle import (read_fact, CREATION_KEY, ADMISSION_KEY, WAIT_KEY,
        SpecialistChildCreationV1, SpecialistChildAdmissionV1, SpecialistWaitV1,
        _task_binding, _input_immutable_binding, verify_terminal_specialist_origin)
    from src.workflows.general_task_guard import child_binding, read_manifest, _protected_payload, cleanup_checkpoint_id
    from src.work_board.input_artifacts import _metadata_digest
    from src.workflows.general_task_accounting import entry_for
    from src.db.models import Goal
    from src.workflows.job_runtime import _canonical
    costs = []
    for row in (await db.execute(select(InferenceCostReservation).where(
        InferenceCostReservation.owner_id == manifest.owner_principal_id))).scalars():
        entry = entry_for(row)
        if entry is not None and entry["group"]["group_id"] == manifest.group_id:
            if entry["group_digest"] != manifest.group_digest:
                deny()
            costs.append({"operation_id":row.operation_id,"state":row.state,"evidence_digest":digest(entry)})
    if len(costs) > 12 or len({item["operation_id"] for item in costs}) != len(costs):
        deny()
    costs.sort(key=lambda item:item["operation_id"])
    compiled = []
    for callback in callbacks:
        if json.loads(callback.arguments_json or "{}").get("tool_id") != "delegate_task":
            continue
        reservation = read_reservation(callback)
        if reservation is None:
            # No actual child publication may exist without its original reservation.
            if (await db.scalar(select(WorkflowRunState.id).where(WorkflowRunState.parent_job_id == callback.run_identity))) is not None:
                deny()
            continue
        binding = child_binding(callback)
        if (reservation.parent_job_id != parent.run_identity or reservation.parent_creation_digest != manifest.creation_digest
            or reservation.delegation_invocation_id != callback.run_identity
            or reservation.original_root_id != manifest.original_root_id
            or reservation.owner_principal_id != manifest.owner_principal_id):
            deny()
        creation = read_fact(callback, CREATION_KEY, SpecialistChildCreationV1)
        admission = read_fact(callback, ADMISSION_KEY, SpecialistChildAdmissionV1)
        wait = read_fact(callback, WAIT_KEY, SpecialistWaitV1)
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == reservation.child_task_id))
        attempt = await db.get(WorkBoardAttempt, reservation.child_attempt_id)
        child = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == reservation.child_job_id))
        actual_jobs = list((await db.execute(select(WorkflowRunState).where(WorkflowRunState.parent_job_id == callback.run_identity))).scalars())
        if {row.run_identity for row in actual_jobs} != ({reservation.child_job_id} if child is not None else set()):
            deny()
        fact = SpecialistDelegationCancelV1(parent_job_id=parent.run_identity,parent_creation_digest=manifest.creation_digest,
            invocation_id=callback.run_identity,original_binding_digest=digest(binding.model_dump(mode="json")),
            reservation_digest=digest(reservation.model_dump(mode="json")),child_task_id=reservation.child_task_id,
            child_attempt_id=reservation.child_attempt_id,child_job_id=reservation.child_job_id,
            cost_membership_digest=digest(costs),cost_operation_ids=[item["operation_id"] for item in costs],
            cost_unresolved=any(item["state"] not in {"settled","released"} for item in costs),stop_action=action)
        artifact = goal = None
        rows = []
        if creation is None:
            if task is not None or attempt is not None or child is not None or admission is not None or wait is not None:
                deny()
        else:
            if task is None or creation.reservation_digest != fact.reservation_digest:
                deny()
            artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id)
            goal = await db.get(Goal, task.goal_id)
            if (artifact is None or goal is None or _task_binding(task) != creation.child_task_binding_digest
                or goal.revision!=task.goal_revision or goal.owner_principal_id!=task.owner_principal_id
                or goal.owner_session_id!=task.owner_session_id
                or artifact.metadata_digest != _metadata_digest(artifact)
                or _input_immutable_binding(artifact) != creation.child_input_immutable_digest
                or task.typed_input_digest != creation.child_input_digest or artifact.bound_task_id != task.task_id
                or (task.owner_principal_id,task.owner_session_id)!=(manifest.owner_principal_id,manifest.original_root_id)
                or (creation.child_task_id,creation.reserved_attempt_id,creation.reserved_job_id)!=
                    (reservation.child_task_id,reservation.child_attempt_id,reservation.child_job_id)):
                deny()
            fact=fact.model_copy(update={"creation_digest":digest(creation.model_dump(mode="json")),
                "child_input_digest":task.typed_input_digest,"child_input_metadata_digest":artifact.metadata_digest,
                "child_task_binding_digest":creation.child_task_binding_digest,
                "original_task_revision":task.task_revision,"current_task_revision":task.task_revision+1})
            if admission is None:
                # Created Task may not yet have its reserved execution Attempt.
                if child is not None or wait is not None or attempt is not None:
                    deny()
            else:
                if (child is None or attempt is None or admission.creation_digest != fact.creation_digest
                    or admission.reservation_digest != fact.reservation_digest
                    or attempt.task_id != task.task_id or attempt.workflow_run_id != child.run_identity
                    or child.parent_job_id != callback.run_identity or child.input_digest != admission.child_input_digest
                    or child.authority_digest != admission.child_authority_digest
                    or child.parent_fencing_token != reservation.callback_fence
                    or child.goal_id != task.goal_id or child.goal_revision != task.goal_revision
                    or child.owner_principal_id != task.owner_principal_id or child.operator_session_id != task.owner_session_id):
                    deny()
                fact=fact.model_copy(update={"admission_digest":digest(admission.model_dump(mode="json"))})
                if wait is not None:
                    if (wait.child_job_id != child.run_identity or wait.reservation_digest != fact.reservation_digest
                        or wait.child_creation_digest != fact.creation_digest or wait.child_admission_digest != fact.admission_digest):
                        deny()
                    fact=fact.model_copy(update={"wait_digest":digest(wait.model_dump(mode="json"))})
                fact=fact.model_copy(update={"original_board_fence":attempt.fencing_token,
                    "current_board_fence":attempt.fencing_token,"original_attempt_outcome":attempt.outcome})
                completed = child.status == "succeeded" and task.status.value == "done" and attempt.ended_at is not None
                if completed:
                    await verify_terminal_specialist_origin(db,task,attempt,child)
                fact=fact.model_copy(update={"completed_child":completed,
                    "current_task_revision":task.task_revision if completed else task.task_revision+1})
                native_rows = list((await db.execute(select(WorkflowRunState).where(WorkflowRunState.parent_job_id == child.run_identity))).scalars())
                child_manifest = read_manifest(child)
                if {row.run_identity for row in native_rows} != (set(child_manifest.admitted_invocation_ids) if child_manifest else set()):
                    deny()
                if len(native_rows)>16:
                    deny()
                for native in native_rows:
                    native_binding=child_binding(native)
                    if (native_binding.parent_job_id!=child.run_identity or native_binding.task_id!=task.task_id
                        or native_binding.attempt_id!=attempt.attempt_id or native_binding.original_root_id!=manifest.original_root_id
                        or native_binding.owner_principal_id!=manifest.owner_principal_id
                        or child_manifest is None or native_binding.creation_digest!=child_manifest.creation_digest):
                        deny()
                    if native.status in {"succeeded","degraded"}:
                        from src.work_board.contracts import GeneralTaskToolClosureV1
                        closure=_protected_payload(child,cleanup_checkpoint_id(native_binding,native.fencing_token),GeneralTaskToolClosureV1)
                        if closure.outcome not in {"returned","approval_precontact"}:
                            deny()
                rows=[child,*native_rows]
                if not completed:
                    fact=fact.model_copy(update={"current_task_revision":task.task_revision+1,
                        "current_board_fence":attempt.fencing_token+1})
                for row in rows:
                    effects=json.loads(row.effect_receipts_json or "[]")
                    if digest(json.loads(row.declared_authority_json))!=row.authority_digest:
                        deny()
                    immutable=row.status in {"succeeded","degraded"} and (row is not child or completed)
                    if row.attempt_count not in {0,1} or (row.attempt_count==0 and (row.fencing_token or effects or row.lease_owner)):
                        deny()
                    fact.jobs.append(SpecialistStoppedJobV1(job_id=row.run_identity,parent_job_id=row.parent_job_id,
                        input_digest=row.input_digest,authority_digest=row.authority_digest,original_revision=row.revision,
                        original_fence=row.fencing_token,current_revision=row.revision+int(not immutable),
                        current_fence=row.fencing_token+int(not immutable),attempts=row.attempt_count,
                        original_status=row.status,current_status=row.status if immutable else ("cancelled" if row.attempt_count==0 else "blocked"),
                        effect_digest=digest(effects),artifact_digest=digest(json.loads(row.artifact_receipts_json or "[]")),
                        checkpoint_digest=digest(json.loads(row.checkpoint_receipts_json or "[]")),immutable_completed=immutable))
        if len(_canonical(fact.model_dump(mode="json")).encode())>65536:
            deny()
        fact=SpecialistDelegationCancelV1.model_validate(fact.model_dump(mode="json"))
        compiled.append(CompiledStop(fact,rows,task,attempt,artifact,goal))
    if len(compiled)>4 or sum(len(item.fact.jobs)-bool(item.fact.jobs) for item in compiled)>16:
        deny()
    return compiled


async def apply_specialist_stops(db, compiled):
    from src.workflows.general_task_guard import _cancel_cas_job, _cancel_cas_board
    from sqlalchemy import update
    from src.workflows.job_runtime import _utc_now
    for item in compiled:
        for row, proof in zip(item.rows,item.fact.jobs):
            if proof.immutable_completed:
                continue
            await _cancel_cas_job(db,row,{"status":proof.current_status,"failure_reason":"specialist_stop_"+item.fact.stop_action,
                "fencing_token":proof.current_fence,"lease_owner":None,"lease_expires_at":None})
        if item.task is not None and not item.fact.completed_child:
            if item.attempt is not None:
                await _cancel_cas_board(db,item.task,item.attempt,item.artifact,item.goal,state="pending",first=True)
            else:
                changed=await db.execute(update(WorkBoardTask).where(WorkBoardTask.task_id==item.task.task_id,
                    WorkBoardTask.task_revision==item.task.task_revision,WorkBoardTask.typed_input_digest==item.task.typed_input_digest).values(
                    status="blocked",block_kind="unknown_effect",block_reason="specialist_stop_"+item.fact.stop_action,
                    task_revision=item.task.task_revision+1,updated_at=_utc_now()))
                if changed.rowcount!=1:
                    deny()


async def verify_specialist_stop(db,parent,entry):
    """Original stopped metadata only; never calls the live delegation gate."""
    from src.workflows.general_task_guard import _protected_payload, child_binding, read_manifest
    from src.workflows.specialist_delegation import read_reservation
    from src.workflows.specialist_lifecycle import _task_binding, _input_immutable_binding, read_fact, CREATION_KEY, SpecialistChildCreationV1
    from src.work_board.input_artifacts import _metadata_digest
    fact=_protected_payload(parent,entry.delegation_stop_checkpoint,SpecialistDelegationCancelV1)
    callback=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==fact.invocation_id).execution_options(populate_existing=True))
    if callback is None or fact.parent_job_id!=parent.run_identity or read_reservation(callback) is None:
        deny()
    if digest(read_reservation(callback).model_dump(mode="json"))!=fact.reservation_digest:
        deny()
    manifest=read_manifest(parent)
    if (manifest is None or fact.parent_creation_digest!=manifest.creation_digest
        or digest(child_binding(callback).model_dump(mode='json'))!=fact.original_binding_digest):
        deny()
    direct=list((await db.execute(select(WorkflowRunState.run_identity).where(
        WorkflowRunState.parent_job_id==callback.run_identity))).scalars())
    if set(direct)!=({fact.child_job_id} if fact.jobs else set()):
        deny()
    native=list((await db.execute(select(WorkflowRunState.run_identity).where(
        WorkflowRunState.parent_job_id==fact.child_job_id))).scalars())
    if set(native)!={proof.job_id for proof in fact.jobs if proof.parent_job_id==fact.child_job_id}:
        deny()
    for proof in fact.jobs:
        row=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==proof.job_id).execution_options(populate_existing=True))
        if (row is None or row.parent_job_id!=proof.parent_job_id or row.revision!=proof.current_revision
            or row.fencing_token!=proof.current_fence or row.status!=proof.current_status
            or row.attempt_count!=proof.attempts or row.lease_owner or row.lease_expires_at
            or row.input_digest!=proof.input_digest or row.authority_digest!=proof.authority_digest
            or digest(json.loads(row.declared_authority_json))!=row.authority_digest
            or digest(json.loads(row.effect_receipts_json or "[]"))!=proof.effect_digest
            or digest(json.loads(row.artifact_receipts_json or "[]"))!=proof.artifact_digest
            or digest(json.loads(row.checkpoint_receipts_json or "[]"))!=proof.checkpoint_digest):
            deny()
    for closure in fact.observed_closures:
        proof=next((item for item in fact.jobs if item.job_id==closure.invocation_id),None)
        if proof is None or proof.attempts!=1 or closure.child_fence!=proof.original_fence:
            deny()
        from src.workflows.general_task_guard import child_binding
        native=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==proof.job_id))
        binding=child_binding(native)
        if (closure.input_digest!=binding.input_digest or closure.descriptor_digest!=binding.descriptor_digest
            or closure.original_binding_digest!=digest(binding.model_dump(mode='json'))):
            deny()
    if fact.creation_digest is not None:
        from src.db.models import Goal
        task=await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==fact.child_task_id).execution_options(populate_existing=True))
        attempt=await db.get(WorkBoardAttempt,fact.child_attempt_id,populate_existing=True)
        if task is None or _task_binding(task)!=fact.child_task_binding_digest or task.task_revision!=fact.current_task_revision:
            deny()
        artifact=await db.get(WorkBoardInputArtifact,task.input_artifact_id,populate_existing=True)
        creation=read_fact(callback,CREATION_KEY,SpecialistChildCreationV1)
        goal=await db.get(Goal,task.goal_id,populate_existing=True)
        if (artifact is None or creation is None or digest(creation.model_dump(mode='json'))!=fact.creation_digest
            or goal is None or goal.revision!=task.goal_revision or goal.owner_principal_id!=task.owner_principal_id
            or goal.owner_session_id!=task.owner_session_id
            or artifact.bound_task_id!=task.task_id or artifact.metadata_digest!=_metadata_digest(artifact)
            or artifact.metadata_digest!=fact.child_input_metadata_digest
            or task.typed_input_digest!=fact.child_input_digest
            or _input_immutable_binding(artifact)!=creation.child_input_immutable_digest):
            deny()
        if fact.admission_digest is not None and (attempt is None or attempt.fencing_token!=fact.current_board_fence
            or attempt.workflow_run_id!=fact.child_job_id or (not fact.completed_child and attempt.cancel_requested_at is None)):
            deny()
    else:
        task=await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==fact.child_task_id))
        attempt=await db.get(WorkBoardAttempt,fact.child_attempt_id)
        if task is not None or attempt is not None:
            deny()
    return fact


async def observe_specialist_stop_closure(jobs,child_id,*,producer_witness):
    """Persist only an actual retained callback closure; no outcome adoption."""
    from src.work_board.repository import _begin_sqlite_immediate
    from src.workflows.general_task_guard import (_cancel_original,_cancel_witness,_cancel_result,
        child_binding,_published_proofs,_cas_parent)
    from src.native_tools.task_adapters import verify_task_tool_closure
    from src.workflows.job_runtime import _canonical
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        child=await jobs._fetch(db,child_id)
        binding=child_binding(child)
        authority=json.loads(child.declared_authority_json or '{}')
        parent_id=authority.get('specialist_original_parent_id')
        parent,task,attempt,manifest,artifact,goal,callbacks=await _cancel_original(jobs,db,parent_id,observation=True)
        cancel=_cancel_witness(parent,task,attempt)
        found=[]
        for entry in cancel.children:
            if entry.delegation_stop_checkpoint is not None:
                fact=await verify_specialist_stop(db,parent,entry)
                proof=next((item for item in fact.jobs if item.job_id==child_id and item.parent_job_id==binding.parent_job_id),None)
                if proof is not None:
                    found.append((entry,fact,proof))
        if len(found)!=1:
            deny()
        entry,fact,proof=found[0]
        if proof.attempts!=1 or proof.job_id==fact.child_job_id:
            deny()
        closure=verify_task_tool_closure(producer_witness,binding=binding,fencing_token=proof.original_fence)
        previous=[item for item in fact.observed_closures if item.invocation_id==child_id]
        if previous:
            if previous!=[closure]:
                deny()
            return await _cancel_result(jobs,db,parent_id,task.task_id,attempt.attempt_id)
        updated=SpecialistDelegationCancelV1.model_validate(fact.model_dump(mode='json') | {
            'observed_closures':[item.model_dump(mode='json') for item in fact.observed_closures]+[closure.model_dump(mode='json')]})
        if len(_canonical(updated.model_dump(mode='json')).encode())>65536:
            deny()
        _published,values=_published_proofs(parent,manifest,(),((entry.delegation_stop_checkpoint,updated),))
        await _cas_parent(db,parent,values)
        return await _cancel_result(jobs,db,parent_id,task.task_id,attempt.attempt_id)
