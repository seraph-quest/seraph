"""Authenticated metadata-only partial review, over the immutable stop owner."""
from __future__ import annotations

import json
from types import SimpleNamespace
from sqlalchemy import select

from src.db.models import OperatorSession, WorkBoardTask, WorkBoardAttempt, WorkflowRunState, InferenceCostReservation
from src.work_board.contracts import (GeneralTaskCheckpointReservationV1, SpecialistPartialDecisionV1,
    SpecialistPartialResultV1, SpecialistPartialOutputV1, SpecialistPartialEffectV1,
    SpecialistPartialCostV1, SpecialistPartialJobV1, GeneralTaskArtifactRef, SpecialistPartialArtifactProofV1)
from src.work_board.general_task import digest,canonical
from src.work_board.repository import BoardError


def partial_checkpoint_ids(parent_id,attempt_id):
    key=digest([parent_id,attempt_id])
    return tuple('general:partial:'+kind+':'+key for kind in ('decision','artifact','readback'))


def deny(code='specialist_partial_binding_changed'):
    raise BoardError(code,'Original stopped partial-result evidence changed; refresh the task',status_code=409)


def bounded(model):
    if len(canonical(model.model_dump(mode='json')))>65536:
        deny('specialist_partial_capacity')
    return model


async def authenticate_partial_owner(db,task,*,operator=None,owner=None):
    from src.auth.service import AuthenticatedOperator
    from src.workflows.job_runtime import _as_utc,_utc_now
    root=await db.get(OperatorSession,task.owner_session_id,populate_existing=True)
    now=_utc_now()
    if (root is None or root.principal_id!=task.owner_principal_id or root.revoked_at or root.replaced_by_id
        or root.is_bearer_tombstone or _as_utc(root.idle_expires_at)<=now or _as_utc(root.absolute_expires_at)<=now):
        deny('specialist_partial_owner_unavailable')
    if operator is not None:
        if (type(operator) is not AuthenticatedOperator or operator.ownership_continuity!='stable'
            or operator.session_id!=root.id or operator.principal.principal_id!=root.principal_id
            or operator.principal.session_id!=root.id or operator.principal.operator_session_id!=root.id
            or not operator.principal.authenticated or operator.principal.revoked
            or operator.operator_identity_id!=root.operator_identity_id
            or (operator._token_hash is not None and operator._token_hash!=root.token_hash)):
            deny('specialist_partial_owner_unavailable')
    elif owner is None or (owner.principal_id,owner.session_id)!=(root.principal_id,root.id):
        deny('specialist_partial_owner_unavailable')
    return root


async def stopped_context(jobs,db,parent_id,*,operator=None,owner=None):
    from src.workflows.general_task_guard import _cancel_original,_cancel_witness,child_binding
    from src.workflows.specialist_stop import verify_specialist_stop
    from src.work_board.pipelines import root_binding
    parent,task,attempt,manifest,artifact,goal,callbacks=await _cancel_original(jobs,db,parent_id,observation=True)
    await authenticate_partial_owner(db,task,operator=operator,owner=owner)
    if goal.revision!=task.goal_revision:
        deny()
    if any(child_binding(callback).live_root_digest!=digest(root_binding()) for callback in callbacks):
        deny()
    stop=_cancel_witness(parent,task,attempt)
    facts=[]
    for entry in stop.children:
        if entry.delegation_stop_checkpoint is not None:
            facts.append(await verify_specialist_stop(db,parent,entry))
    cost_ids={item.operation_id for item in await current_costs(db,manifest)}
    if any(set(fact.cost_operation_ids)!=cost_ids for fact in facts):
        deny()
    return parent,task,attempt,manifest,stop,callbacks,facts


async def current_costs(db,manifest):
    from src.workflows.general_task_accounting import entry_for
    costs=[]
    ordinals=[]
    for row in (await db.execute(select(InferenceCostReservation).where(
        InferenceCostReservation.owner_id==manifest.owner_principal_id))).scalars():
        entry=entry_for(row)
        if entry is not None and entry['group']['group_id']==manifest.group_id:
            if (entry['group_digest']!=manifest.group_digest or entry['group']['owner_session_id']!=manifest.original_root_id
                or entry['group']['original_deadline_at']!=manifest.original_deadline_at.isoformat().replace('+00:00','Z')):
                deny()
            ordinals.append(entry['call_ordinal'])
            costs.append(SpecialistPartialCostV1(operation_id=row.operation_id,state=row.state,evidence_digest=digest(entry)))
    if len(costs)>12 or len(set(ordinals))!=len(ordinals) or sorted(ordinals)!=list(range(1,len(ordinals)+1)):
        deny()
    return sorted(costs,key=lambda item:item.operation_id)


async def selected_outputs(db,parent,manifest,callbacks,step_ids):
    from src.workflows.general_task_guard import child_binding,_step_receipt
    from src.workflows.specialist_result import verify_full_delegation_result
    from src.workflows.specialist_delegation import read_reservation
    outputs=[]
    receipt_digests=[]
    closure_digests=[]
    for step_id in step_ids:
        matches=[callback for callback in callbacks if child_binding(callback).step_id==step_id]
        if len(matches)!=1:
            deny()
        callback=matches[0]
        receipt=_step_receipt(manifest,step_id)
        if receipt.status!='verified' or receipt.contact_state!='settled':
            deny()
        closure=await verify_full_delegation_result(db,parent,callback,receipt)
        reservation=read_reservation(callback)
        child=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==reservation.child_job_id))
        records=json.loads(child.artifact_receipts_json or '[]')
        for reference in closure.artifact_refs:
            if not reference.startswith('artifact:'):
                deny()
            selected=[item for item in records if item.get('artifact_id')==reference[9:]]
            if len(selected)!=1:
                deny()
            record=selected[0]
            # Selected native filesystem output remains a real physical file,
            # in addition to its immutable private tool-result artifact.
            from src.work_board.input_artifacts import _safe_file_bytes
            from src.workspace import canonical_workspace_root
            from config.settings import settings
            body=json.loads(_safe_file_bytes(canonical_workspace_root(settings.workspace_dir)/record['file_path'],
                expected_digest=record['content_sha256'],expected_size=record['size_bytes']))
            natives=list((await db.execute(select(WorkflowRunState).where(
                WorkflowRunState.parent_job_id==child.run_identity))).scalars())
            producer=[native for native in natives if child_binding(native).step_id==body.get('step_id')]
            if len(producer)!=1:
                deny()
            if json.loads(producer[0].arguments_json or '{}').get('tool_id')=='write_file':
                from src.tools.filesystem_tool import _safe_resolve,_assert_not_secret_like_path
                output=body['output']
                _assert_not_secret_like_path(output['file_path'],'partial_result_readback')
                _safe_file_bytes(_safe_resolve(output['file_path']),expected_digest=output['content_sha256'],
                    expected_size=output['bytes_written'])
            outputs.append(SpecialistPartialOutputV1(step_id=step_id,child_task_id=reservation.child_task_id,
                child_job_id=child.run_identity,artifact_id=record['artifact_id'],file_path=record['file_path'],
                content_sha256=record['content_sha256'],size_bytes=record['size_bytes']))
        receipt_digests.append(digest(receipt.model_dump(mode='json')))
        closure_digests.append(digest(closure.model_dump(mode='json')))
    if len(outputs)>64:
        deny('specialist_partial_capacity')
    return outputs,receipt_digests,closure_digests


async def complete_inventory(db,parent,callbacks,facts,manifest):
    from src.workflows.job_runtime import _effect_is_unresolved
    rows=[parent,*callbacks]
    for fact in facts:
        for proof in fact.jobs:
            row=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==proof.job_id))
            if row is None:
                deny()
            rows.append(row)
    if len(rows)>85 or len({row.run_identity for row in rows})!=len(rows):
        deny()
    inventory=[]
    effects=[]
    unresolved=[]
    for row in rows:
        ledger=json.loads(row.effect_receipts_json or '[]')
        inventory.append(SpecialistPartialJobV1(job_id=row.run_identity,parent_job_id=row.parent_job_id,
            status=row.status,attempts=row.attempt_count,input_digest=row.input_digest,authority_digest=row.authority_digest,
            effect_digest=digest(ledger),artifact_digest=digest(json.loads(row.artifact_receipts_json or '[]')),
            checkpoint_digest=digest(json.loads(row.checkpoint_receipts_json or '[]'))))
        if row is not parent and row.status not in {'succeeded','degraded','cancelled'}:
            unresolved.append(row.run_identity)
        for item in ledger:
            if _effect_is_unresolved(item):
                identifier=item.get('effect_id') or item.get('readback_id')
                if not identifier:
                    deny()
                effects.append(SpecialistPartialEffectV1(job_id=row.run_identity,effect_id=identifier,status=item['status']))
    if len(effects)>256:
        deny('specialist_partial_capacity')
    return inventory,effects,unresolved,await current_costs(db,manifest)


def existing_decision(parent,attempt_id):
    from src.workflows.general_task_guard import _history,_protected_payload
    identity=partial_checkpoint_ids(parent.run_identity,attempt_id)[0]
    matches=[item for item in _history(parent) if item.get('checkpoint_id')==identity]
    if len(matches)==1 and matches[0].get('payload',{}).get('schema_version')=='SpecialistPartialDecision.v1':
        return _protected_payload(parent,identity,SpecialistPartialDecisionV1)
    return None


async def read_partial_overlay(jobs,db,parent_id,*,owner=None,operator=None):
    from src.work_board.general_task_runtime_artifacts import read_native_artifact_reference
    from src.workflows.general_task_guard import _protected_payload,child_binding
    parent,task,attempt,manifest,stop,callbacks,facts=await stopped_context(jobs,db,parent_id,owner=owner,operator=operator)
    decision=existing_decision(parent,attempt.attempt_id)
    if decision is None:
        return None
    if (decision.creation_digest!=manifest.creation_digest or decision.task_id!=task.task_id
        or decision.attempt_id!=attempt.attempt_id or decision.owner_principal_id!=task.owner_principal_id
        or decision.original_root_id!=task.owner_session_id or decision.group_digest!=manifest.group_digest):
        deny()
    # Current stop may genuinely reconcile; historical accepted overlay never
    # replaces it. Selected physical outputs are independently re-read now.
    outputs,receipts,closures=await selected_outputs(db,parent,manifest,callbacks,decision.selected_step_ids)
    if receipts!=decision.selected_receipt_digests or closures!=decision.selected_closure_digests:
        deny()
    result=read_native_artifact_reference(decision.result_ref,parent_job_id=parent_id,creation_digest=manifest.creation_digest)
    bounded(result)
    if (result.decision_binding_digest!=decision.decision_binding_digest
        or result.selected_outputs!=outputs):
        deny()
    keys=partial_checkpoint_ids(parent_id,attempt.attempt_id)
    records=json.loads(parent.artifact_receipts_json or '[]')
    for kind,key in zip(('artifact','readback'),keys[1:]):
        proof=_protected_payload(parent,key,SpecialistPartialArtifactProofV1)
        if (proof.kind!=kind or proof.parent_job_id!=parent_id or proof.creation_digest!=manifest.creation_digest
            or proof.decision_digest!=digest(decision.model_dump(mode='json')) or proof.result_ref!=decision.result_ref):
            deny()
        matched=[item for item in records if item.get('artifact_id')==proof.result_ref.artifact_id]
        if (len(matched)!=1 or matched[0].get('content_sha256')!=proof.result_ref.digest
            or matched[0].get('size_bytes')!=proof.size_bytes or matched[0].get('file_path')!=proof.file_path
            or matched[0].get('artifact_type')!='specialist_partial_result'):
            deny()
    current=await current_costs(db,manifest)
    if {item.operation_id for item in current}!={item.operation_id for item in result.costs}:
        deny()
    _,current_effects,current_unresolved,current=await complete_inventory(db,parent,callbacks,facts,manifest)
    return {'state':decision.state,'decision_digest':digest(decision.model_dump(mode='json')),
        'idempotency_key':decision.idempotency_key,'result_ref':decision.result_ref.model_dump(mode='json'),
        'selected_step_ids':decision.selected_step_ids,
        'selected_outputs':[{'step_id':item.step_id,'child_task_id':item.child_task_id,'child_job_id':item.child_job_id,
            'delegation_invocation_id':next(callback.run_identity for callback in callbacks
                if child_binding(callback).step_id==item.step_id),
            'artifact_id':item.artifact_id,'content_sha256':item.content_sha256,'size_bytes':item.size_bytes} for item in outputs],
        'unresolved_job_ids':result.unresolved_job_ids,'unresolved_effect_count':len(result.unresolved_effects),
        'current_unresolved_job_ids':current_unresolved,'current_unresolved_effect_count':len(current_effects),
        'current_unresolved_cost_count':sum(item.state not in {'settled','released'} for item in current),
        'current_cancellation_state':stop.state,'no_learning':True}


async def partial_review_options(jobs,db,parent_id,*,owner):
    from src.workflows.general_task_guard import child_binding
    parent,task,attempt,manifest,stop,callbacks,facts=await stopped_context(jobs,db,parent_id,owner=owner)
    accepted=existing_decision(parent,attempt.attempt_id)
    eligible=accepted is None and stop.state in {'pending','callback_closed_outcome_debt'} and attempt.ended_at is None
    selections=[]
    for entry in stop.children:
        if entry.delegation_closure_digest is None:
            continue
        callback=next(row for row in callbacks if row.run_identity==entry.original_binding.invocation_id)
        step_id=child_binding(callback).step_id
        outputs,_,_=await selected_outputs(db,parent,manifest,callbacks,[step_id])
        selections.append({'step_id':step_id,'outputs':[{'child_task_id':item.child_task_id,'child_job_id':item.child_job_id,
            'delegation_invocation_id':callback.run_identity,'artifact_id':item.artifact_id,
            'content_sha256':item.content_sha256,'size_bytes':item.size_bytes} for item in outputs]})
    return {'eligible':bool(eligible and selections),'attempt_id':attempt.attempt_id,'workflow_run_id':parent.run_identity,
        'expected_manifest_revision':manifest.manifest_revision,'expected_plan_revision':manifest.plan_revision,
        'selected_steps':selections,'no_learning':True}


async def accept_partial_results(jobs,*,task_id,operator,request):
    from src.work_board.repository import _begin_sqlite_immediate,WorkBoardRepository
    from src.workflows.general_task_guard import (_protected_payload,_published_proofs,_cas_parent,_history)
    from src.work_board.general_task_runtime_artifacts import stage_task_artifact,verify_staged_task_artifact
    selected=request.partial_decision
    if selected is None or request.action.value!='accept_partial_results' or request.model_fields_set-{'action','expected_revision','partial_decision'}:
        deny()
    # Both staging and final writer authenticate current actual Root. Exact
    # replay precedes stale request-CAS checks, but never current auth checks.
    async with jobs._session() as db:
        context=await stopped_context(jobs,db,selected.workflow_run_id,operator=operator)
        parent,task,attempt,manifest,stop,callbacks,facts=context
        if task.task_id!=task_id or selected.attempt_id!=attempt.attempt_id:
            deny()
        previous=existing_decision(parent,attempt.attempt_id)
        request_digest=digest(request.model_dump(mode='json'))
        if previous is not None:
            if previous.request_digest!=request_digest or previous.idempotency_key!=selected.idempotency_key:
                deny('specialist_partial_decision_conflict')
            return {'partial_review':await read_partial_overlay(jobs,db,parent.run_identity,operator=operator),'idempotent_replay':True}
        if (stop.state not in {'pending','callback_closed_outcome_debt'} or attempt.ended_at
            or task.task_revision!=request.expected_revision or manifest.manifest_revision!=selected.expected_manifest_revision
            or manifest.plan_revision!=selected.expected_plan_revision):
            deny()
        keys=partial_checkpoint_ids(parent.run_identity,attempt.attempt_id)
        for key in keys:
            capacity=_protected_payload(parent,key,GeneralTaskCheckpointReservationV1)
            if capacity.parent_job_id!=parent.run_identity or capacity.creation_digest!=manifest.creation_digest or capacity.attempt_id!=attempt.attempt_id:
                deny()
        if len(json.loads(parent.artifact_receipts_json or '[]'))>=50:
            deny('specialist_partial_capacity')
        outputs,receipts,closures=await selected_outputs(db,parent,manifest,callbacks,selected.selected_step_ids)
        inventory,effects,unresolved,costs=await complete_inventory(db,parent,callbacks,facts,manifest)
        binding=digest({'task_id':task_id,'attempt_id':attempt.attempt_id,'creation_digest':manifest.creation_digest,
            'stop_digest':digest(stop.model_dump(mode='json')),'request_digest':request_digest,
            'outputs':[item.model_dump(mode='json') for item in outputs],'receipts':receipts,'closures':closures,
            'jobs':[item.model_dump(mode='json') for item in inventory],'costs':[item.model_dump(mode='json') for item in costs]})
        result=bounded(SpecialistPartialResultV1(parent_job_id=parent.run_identity,creation_digest=manifest.creation_digest,
            decision_binding_digest=binding,selected_outputs=outputs,unresolved_effects=effects,unresolved_job_ids=unresolved,costs=costs))
        stage=stage_task_artifact(parent_job_id=parent.run_identity,creation_digest=manifest.creation_digest,payload=result)
        decision=bounded(SpecialistPartialDecisionV1(parent_job_id=parent.run_identity,task_id=task_id,attempt_id=attempt.attempt_id,
            creation_digest=manifest.creation_digest,original_stop_digest=digest(stop.model_dump(mode='json')),
            original_stop_manifest_digest=digest(manifest.model_dump(mode='json')),task_revision=task.task_revision,
            owner_principal_id=task.owner_principal_id,original_root_id=task.owner_session_id,goal_id=task.goal_id,
            goal_revision=task.goal_revision,group_id=manifest.group_id,group_digest=manifest.group_digest,
            original_deadline_at=manifest.original_deadline_at,native_deadline_at=manifest.native_deadline_at,
            idempotency_key=selected.idempotency_key,request_digest=request_digest,decision_binding_digest=binding,
            selected_step_ids=selected.selected_step_ids,selected_receipt_digests=receipts,selected_closure_digests=closures,
            result_ref=stage.reference,jobs=inventory,cost_membership_digest=digest([item.model_dump(mode='json') for item in costs])))
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        parent,task,attempt,manifest,stop,callbacks,facts=await stopped_context(jobs,db,selected.workflow_run_id,operator=operator)
        previous=existing_decision(parent,attempt.attempt_id)
        if previous is not None:
            if previous.request_digest!=request_digest:
                deny('specialist_partial_decision_conflict')
            return {'partial_review':await read_partial_overlay(jobs,db,parent.run_identity,operator=operator),'idempotent_replay':True}
        if (task.task_revision!=decision.task_revision or digest(stop.model_dump(mode='json'))!=decision.original_stop_digest
            or digest(manifest.model_dump(mode='json'))!=decision.original_stop_manifest_digest or attempt.ended_at):
            deny()
        current_outputs,current_receipts,current_closures=await selected_outputs(db,parent,manifest,callbacks,selected.selected_step_ids)
        current_inventory,current_effects,current_unresolved,current_costs_=await complete_inventory(db,parent,callbacks,facts,manifest)
        if (current_outputs!=outputs or current_receipts!=receipts or current_closures!=closures
            or current_inventory!=inventory or current_effects!=effects or current_unresolved!=unresolved or current_costs_!=costs):
            deny()
        if len(json.loads(parent.artifact_receipts_json or '[]'))>=50:
            deny('specialist_partial_capacity')
        parsed,record=verify_staged_task_artifact(stage,parent_job_id=parent.run_identity,creation_digest=manifest.creation_digest)
        if parsed!=result:
            deny()
        # Three existing reserved records are replaced, never appended beyond
        # the precontact whole-journal allocation. Stop phase/counters stay.
        artifact_proof=SpecialistPartialArtifactProofV1(kind='artifact',parent_job_id=parent.run_identity,
            creation_digest=manifest.creation_digest,decision_digest=digest(decision.model_dump(mode='json')),
            result_ref=stage.reference,file_path=record['file_path'],size_bytes=record['size_bytes'])
        readback_proof=artifact_proof.model_copy(update={'kind':'readback'})
        published,values=_published_proofs(parent,manifest,(stage,),((keys[0],decision),
            (keys[1],artifact_proof),(keys[2],readback_proof)))
        if published!=manifest:
            deny()
        await _cas_parent(db,parent,values)
        from src.work_board.contracts import WorkBoardOwner
        owner=WorkBoardOwner(principal_id=task.owner_principal_id,session_id=task.owner_session_id)
        from uuid import uuid5,NAMESPACE_URL
        from src.db.models import WorkBoardEvent
        event_key=str(uuid5(NAMESPACE_URL,'seraph:specialist-partial:'+parent.run_identity+':'+attempt.attempt_id))
        if await db.scalar(select(WorkBoardEvent.event_id).where(WorkBoardEvent.owner_principal_id==owner.principal_id,
            WorkBoardEvent.owner_session_id==owner.session_id,WorkBoardEvent.mutation_idempotency_key==event_key)):
            deny('specialist_partial_event_conflict')
        event=await WorkBoardRepository._event(db,task,owner,kind='task.partial_results_accepted',
            metadata={'attempt_id':attempt.attempt_id,'decision_digest':digest(decision.model_dump(mode='json')),'no_learning':True})
        event.mutation_idempotency_key=event_key
        event.mutation_request_digest=request_digest
        await db.flush()
        return {'partial_review':await read_partial_overlay(jobs,db,parent.run_identity,operator=operator),'idempotent_replay':False}
