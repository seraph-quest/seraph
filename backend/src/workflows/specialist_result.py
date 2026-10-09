"""Actual completed specialist results; fixed paused callback publication only."""
from datetime import datetime,timezone
import json
from sqlalchemy import select
from src.db.models import WorkBoardTask,WorkBoardAttempt,WorkBoardStatus,WorkflowRunState
from src.work_board.repository import BoardError


async def _completed_result(db,context):
    from src.work_board.review import _verified_workflow_readback
    from src.workflows.specialist_lifecycle import verify_terminal_specialist_origin
    from src.workflows.delegation_contracts import ChildResult,verify_child_result
    from src.work_board.input_artifacts import _safe_file_bytes
    from src.workspace import canonical_workspace_root
    from config.settings import settings
    reservation = context.reservation
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==reservation.child_task_id))
    attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id==reservation.child_attempt_id))
    run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==reservation.child_job_id))
    if task is None or attempt is None or run is None or task.status != WorkBoardStatus.done or run.status != 'succeeded':
        return None
    await verify_terminal_specialist_origin(db,task,attempt,run)
    if await _verified_workflow_readback(db,task,attempt) is None:
        raise BoardError('specialist_result_unverified','Actual completed specialist readback required',status_code=409)
    effects = json.loads(run.effect_receipts_json or '[]')
    artifacts = [item for item in json.loads(run.artifact_receipts_json or '[]')
        if item.get('exists') and item.get('artifact_type')=='general_task_step'
        and any(effect.get('receipt_kind')=='readback' and effect.get('status')=='succeeded'
            and effect.get('target_path')==item.get('file_path')
            and effect.get('content_sha256')==item.get('content_sha256') for effect in effects)]
    refs = []
    for item in artifacts:
        _safe_file_bytes(canonical_workspace_root(settings.workspace_dir)/item['file_path'],
            expected_digest=item['content_sha256'],expected_size=item['size_bytes'])
        refs.append('artifact:'+item['artifact_id'])
    if not refs:
        raise BoardError('specialist_result_missing','Actual specialist artifacts required',status_code=409)
    result = ChildResult(child_id=task.task_id,artifact_refs=refs,unresolved=[],summary_ref=refs[-1])
    verify_child_result(result,child_id=task.task_id,verified_artifact_refs=refs)
    return result,run


async def settle_specialist_callback(jobs,invocation_id):
    """Same writer closes a real wait; no lease, attempt or callback execution."""
    from src.workflows.specialist_delegation import current_delegation
    from src.workflows.specialist_lifecycle import (read_fact,WAIT_KEY,CREATION_KEY,CLOSURE_KEY,
        SpecialistWaitV1,SpecialistChildCreationV1,SpecialistDelegationClosureV1)
    from src.work_board.general_task import canonical,digest,validate_schema
    from src.work_board.general_task_native import current_plan
    from src.work_board.input_artifacts import _write_payload,_safe_file_bytes
    from src.work_board.repository import _begin_sqlite_immediate
    from src.work_board.general_task_runtime_artifacts import stage_task_artifact,verify_staged_task_artifact
    from src.work_board.contracts import GeneralTaskArtifactRef,GeneralTaskStepReceiptV1
    from src.workflows.general_task_guard import (_published_proofs,_cas_parent,_next_manifest,
        cleanup_checkpoint_id,effective_child_phase,_assert_joint_manifest)
    from src.workflows.job_runtime import _effect_ledger_or_raise,_job_has_unsafe_effects,_github_recovery_history
    from src.artifacts.registry import build_artifact_record
    from src.workspace import canonical_workspace_root
    from config.settings import settings
    async with jobs._session() as db:
        context = await current_delegation(db,invocation_id)
        completed = await _completed_result(db,context)
        if completed is None:
            return False
        result,_run = completed
        binding = context.native_binding
        body = canonical({'step_id':binding.step_id,'output':result.model_dump(mode='json')})
        sha = digest(json.loads(body))
        path = 'artifacts/work-board/general-tasks/'+digest([invocation_id,binding.plan_digest,binding.step_id])+'-'+sha+'.json'
        absolute = canonical_workspace_root(settings.workspace_dir)/path
        _write_payload(absolute,body)
        _safe_file_bytes(absolute,expected_digest=sha,expected_size=len(body))
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        context = await current_delegation(db,invocation_id)
        callback,parent,previous = context.callback,context.parent,context.manifest
        _assert_joint_manifest(parent,context.task,context.attempt,previous)
        wait = read_fact(callback,WAIT_KEY,SpecialistWaitV1)
        creation = read_fact(callback,CREATION_KEY,SpecialistChildCreationV1)
        if wait is None or creation is None or callback.status!='paused' or callback.failure_reason!='specialist_wait':
            raise BoardError('specialist_result_wait_changed','Original paused specialist wait required',status_code=409)
        completed = await _completed_result(db,context)
        if completed is None:
            raise BoardError('specialist_result_changed','Original completed child changed',status_code=409)
        actual,run = completed
        if canonical({'step_id':binding.step_id,'output':actual.model_dump(mode='json')}) != body:
            raise BoardError('specialist_result_changed','Staged actual child result changed',status_code=409)
        if _safe_file_bytes(absolute,expected_digest=sha,expected_size=len(body)) != body:
            raise BoardError('specialist_result_changed','Staged physical callback output changed',status_code=409)
        step = next(item for item in current_plan(previous,context.envelope).steps if item.step_id==binding.step_id)
        descriptor = next(item for item in context.envelope.descriptors if item.tool_id==step.tool_id)
        if step.tool_id!='delegate_task' or digest(descriptor.model_dump(mode='json'))!=binding.descriptor_digest:
            raise BoardError('specialist_result_changed','Original delegation descriptor required',status_code=409)
        validate_schema(descriptor.output_schema,actual.model_dump(mode='json'))
        validate_schema(step.output_contract,actual.model_dump(mode='json'))
        effect_id = 'general:'+binding.step_id+':'+str(wait.original_claim_fence)
        effects = _effect_ledger_or_raise(callback.effect_receipts_json)
        intent = next(item for item in effects if item.get('effect_id')==effect_id)
        now = datetime.now(timezone.utc)
        output_record = build_artifact_record(file_path=path,artifact_type='general_task_step',
            producer=callback.job_kind,run_id=invocation_id,content=body)
        output_record['recorded_at'] = now.isoformat()
        settled = {**intent,'receipt_kind':'readback','status':'succeeded','content_sha256':sha,
            'readback_id':'general-step-readback:'+digest([invocation_id,binding.step_id])[:32],
            'verified_at':now.isoformat(),'recorded_at':now.isoformat(),
            'details':{'step_id':binding.step_id,'tool_id':'delegate_task','verified':True,'output_exists':True,
                'file_path':path,'no_learning':True,'input_digest':binding.input_digest,
                'original_intent_digest':wait.original_intent_digest,'delegation_wait_digest':digest(wait.model_dump(mode='json'))}}
        artifact_readback = {**settled,'effect_id':'general-artifact:'+digest([invocation_id,binding.plan_digest,binding.step_id])[:32],
            'effect_type':'general_task_artifact_readback','target_path':path,'target_digest':sha}
        effects = [item for item in effects if item.get('effect_id')!=effect_id]+[settled,artifact_readback]
        if _job_has_unsafe_effects(effects):
            raise BoardError('specialist_result_unknown','Unresolved original debt cannot close',status_code=409)
        closure = SpecialistDelegationClosureV1(invocation_id=invocation_id,
            original_binding_digest=digest(binding.model_dump(mode='json')),
            reservation_digest=digest(context.reservation.model_dump(mode='json')),
            wait_digest=digest(wait.model_dump(mode='json')),child_creation_digest=digest(creation.model_dump(mode='json')),
            original_claim_fence=wait.original_claim_fence,child_task_id=wait.child_task_id,
            child_attempt_id=wait.child_attempt_id,child_job_id=wait.child_job_id,
            child_input_digest=run.input_digest,child_authority_digest=run.authority_digest,
            child_effect_digest=digest(json.loads(run.effect_receipts_json)),
            child_artifact_digest=digest(json.loads(run.artifact_receipts_json)),
            child_checkpoint_digest=digest(json.loads(run.checkpoint_receipts_json)),
            outcome='durable_result_verified',output_digest=digest(actual.model_dump(mode='json')),
            artifact_refs=actual.artifact_refs,unresolved=[])
        effective = await effective_child_phase(db,callback,parent)
        reference = GeneralTaskArtifactRef(artifact_id=output_record['artifact_id'],digest=sha,schema_version='GeneralTaskOutput.v1')
        staged = stage_task_artifact(parent_job_id=parent.run_identity,creation_digest=previous.creation_digest,
            payload=GeneralTaskStepReceiptV1(step_id=binding.step_id,plan_revision=binding.plan_revision,
                invocation_id=invocation_id,input_digest=binding.input_digest,contact_state='settled',status='verified',
                descriptor_digest=binding.descriptor_digest,selected_grant_digest=binding.selected_grant_digest,
                task_id=binding.task_id,attempt_id=binding.attempt_id,child_job_id=invocation_id,
                child_attempt_count=1,child_fence=wait.original_claim_fence,parent_creation_digest=binding.creation_digest,
                phase_digest=effective.phase_digest,approval_binding_digest=effective.approval_binding_digest,
                artifact_refs=[reference],effect_receipt_digest=digest(effects),cleanup_receipt_digest=digest(closure.model_dump(mode='json'))))
        verify_staged_task_artifact(staged,parent_job_id=parent.run_identity,creation_digest=previous.creation_digest)
        refs = dict(zip(previous.step_ids,zip(previous.step_receipt_artifact_ids,previous.step_receipt_digests,previous.step_receipt_schemas)))
        refs[binding.step_id] = (staged.reference.artifact_id,staged.reference.digest,'StepReceipt.v1')
        steps = sorted(refs)
        proposed = previous.model_copy(update={'manifest_revision':previous.manifest_revision+1,'step_ids':steps,
            'step_receipt_artifact_ids':[refs[key][0] for key in steps],
            'step_receipt_digests':[refs[key][1] for key in steps],'step_receipt_schemas':[refs[key][2] for key in steps]})
        _next_manifest(parent,previous,proposed,task=context.task,attempt=context.attempt)
        _published,values = _published_proofs(parent,proposed,(staged,),((cleanup_checkpoint_id(binding,wait.original_claim_fence),closure),))
        history = json.loads(callback.checkpoint_receipts_json or '[]')
        artifact_binding = {'schema_version':1,'producer_ref':invocation_id,'step_id':binding.step_id,
            'plan_digest':binding.plan_digest,'producer_fence':wait.original_claim_fence,'file_path':path,
            'content_sha256':sha,'size_bytes':len(body),'no_learning':True}
        for key,payload in [('general:artifact:'+binding.step_id,artifact_binding),(CLOSURE_KEY,closure.model_dump(mode='json'))]:
            if any(item.get('checkpoint_id')==key for item in history):
                raise BoardError('specialist_result_exists','Original result is immutable',status_code=409)
            history.append({'checkpoint_id':key,'safe':True,'payload':payload,'state_digest':digest(payload),
                'state_keys':sorted(payload),'fencing_token':wait.original_claim_fence,'recorded_at':now.isoformat()})
        artifacts = json.loads(callback.artifact_receipts_json or '[]')+[output_record]
        await _cas_parent(db,parent,values)
        await _cas_parent(db,callback,{'status':'succeeded','failure_reason':None,'lease_owner':None,'lease_expires_at':None,
            'finished_at':now,'checkpoint_receipts_json':json.dumps(_github_recovery_history(callback,history,kind='checkpoint'),sort_keys=True,separators=(',',':')),
            'artifact_receipts_json':json.dumps(_github_recovery_history(callback,artifacts,kind='artifact'),sort_keys=True,separators=(',',':')),
            'effect_receipts_json':json.dumps(_github_recovery_history(callback,effects,kind='effect'),sort_keys=True,separators=(',',':')),
            'result_digest':digest({'verified':True,'artifact_refs':[reference.model_dump(mode='json')],'no_learning':True}),
            'result_summary':'Original specialist child output physically verified'})
        return True


async def settle_specialist_waits(jobs,parent_job_id):
    """Finite existing manifest trigger; remaining/Unknown waits stay paused."""
    from src.workflows.general_task_guard import _current,child_binding
    from src.work_board.general_task_runtime_artifacts import read_bound_native_tool_input
    async with jobs._session() as db:
        parent,_task,_attempt,manifest,_envelope = await _current(jobs,db,parent_job_id)
        if parent.status!='paused' or manifest.phase!='native_wait':
            return []
        ids = []
        for child_id in manifest.admitted_invocation_ids:
            callback = await jobs._fetch(db,child_id)
            if callback.status=='paused' and callback.failure_reason=='specialist_wait':
                binding = child_binding(callback)
                if read_bound_native_tool_input(callback,binding).tool_id!='delegate_task':
                    raise BoardError('specialist_result_binding_changed','Original delegation callback required',status_code=409)
                ids.append(child_id)
    settled = []
    for child_id in ids:
        if await settle_specialist_callback(jobs,child_id):
            settled.append(child_id)
    return settled


def read_full_delegation_closure(parent,callback,binding):
    """Closed metadata reader only for an actual completed delegate invocation."""
    from src.workflows.specialist_lifecycle import (read_fact,WAIT_KEY,CREATION_KEY,CLOSURE_KEY,
        SpecialistWaitV1,SpecialistChildCreationV1,SpecialistDelegationClosureV1)
    from src.workflows.specialist_delegation import read_reservation
    from src.workflows.general_task_guard import _protected_payload,cleanup_checkpoint_id
    from src.work_board.general_task import digest
    closure = _protected_payload(parent,cleanup_checkpoint_id(binding,callback.fencing_token),SpecialistDelegationClosureV1)
    own = read_fact(callback,CLOSURE_KEY,SpecialistDelegationClosureV1)
    wait = read_fact(callback,WAIT_KEY,SpecialistWaitV1)
    creation = read_fact(callback,CREATION_KEY,SpecialistChildCreationV1)
    reservation = read_reservation(callback)
    if (json.loads(callback.arguments_json or '{}').get('tool_id')!='delegate_task'
        or closure != own or wait is None or creation is None or reservation is None
        or callback.status!='succeeded' or callback.lease_owner is not None or callback.lease_expires_at is not None
        or callback.attempt_count!=1 or closure.outcome!='durable_result_verified' or closure.unresolved
        or closure.invocation_id!=callback.run_identity or closure.original_claim_fence!=callback.fencing_token
        or closure.original_binding_digest!=digest(binding.model_dump(mode='json'))
        or closure.reservation_digest!=digest(reservation.model_dump(mode='json'))
        or closure.wait_digest!=digest(wait.model_dump(mode='json'))
        or closure.child_creation_digest!=digest(creation.model_dump(mode='json'))
        or closure.original_claim_fence!=wait.original_claim_fence
        or (closure.child_task_id,closure.child_attempt_id,closure.child_job_id)!=
            (reservation.child_task_id,reservation.child_attempt_id,reservation.child_job_id)
        or (closure.child_input_digest,closure.child_authority_digest)!=(wait.child_input_digest,wait.child_authority_digest)):
        raise BoardError('specialist_closure_changed','Exact full original delegation closure required',status_code=409)
    return closure


async def verify_full_delegation_result(db,parent,callback,receipt):
    """Completed child proof cannot renew contact or make a partial result full."""
    from types import SimpleNamespace
    from src.workflows.general_task_guard import child_binding
    from src.workflows.specialist_delegation import read_reservation
    from src.work_board.general_task import digest
    binding = child_binding(callback)
    closure = read_full_delegation_closure(parent,callback,binding)
    completed = await _completed_result(db,SimpleNamespace(reservation=read_reservation(callback)))
    if completed is None:
        raise BoardError('specialist_closure_changed','Original completed specialist required',status_code=409)
    result,run = completed
    if (closure.child_effect_digest!=digest(json.loads(run.effect_receipts_json))
        or closure.child_artifact_digest!=digest(json.loads(run.artifact_receipts_json))
        or closure.child_checkpoint_digest!=digest(json.loads(run.checkpoint_receipts_json))
        or closure.output_digest!=digest(result.model_dump(mode='json'))
        or closure.artifact_refs!=result.artifact_refs
        or receipt.cleanup_receipt_digest!=digest(closure.model_dump(mode='json'))
        or receipt.effect_receipt_digest!=digest(json.loads(callback.effect_receipts_json))):
        raise BoardError('specialist_closure_changed','Actual original child result proof changed',status_code=409)
    return closure
