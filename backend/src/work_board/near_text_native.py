"""Fixed optional HTTPS inference using existing native job and accounting owners."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
from uuid import UUID
from sqlalchemy import select,func
from src.db.models import (OperatorSession, WorkBoardTask, WorkBoardAttempt,
    WorkBoardInputArtifact, WorkflowRunState, InferenceCostReservation, WorkBoardStatus)
from src.work_board.contracts import WorkBoardOwner
from src.work_board.repository import BoardError, WorkBoardRepository
from src.work_board.input_artifacts import _metadata_digest, resolve_input_artifact_for_task
from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec
from src.work_board.pipelines import now, utc, root_binding

CAPABILITY='inference.near-text.v1'
RUNTIME='near_text_native'
SEAL=object()
PREFIX='artifacts/work-board/near-text-output'

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
def digest(value):
    return hashlib.sha256(value if isinstance(value,bytes) else canonical(value)).hexdigest()
def job_id(task,attempt):
    return 'near-text:'+digest([task.task_id,attempt.attempt_id])[:40]
def _mac(metadata,token_hash,key):
    return hmac.new(key,canonical({'binding':metadata,'original_root_token_hash':token_hash}),hashlib.sha256).hexdigest()

def _input(value):
    from src.model_fabric.near_text_contracts import NearTextInput
    return NearTextInput.model_validate(value)

def finite_goal_budget(goal):
    from src.goals.repository import deserialize_admission_budget
    budget=deserialize_admission_budget(goal)
    observed=now()
    if (budget is None or budget.reviewed_grant is not True or not isinstance(budget.grant_id,str)
        or not budget.grant_id.strip() or (budget.period_started_at is not None and utc(budget.period_started_at)>observed)
        or any(observed>=utc(bound) for bound in (goal.due_date,budget.period_expires_at) if bound is not None)):
        raise BoardError('near_goal_grant_required','A current reviewed finite Goal purpose grant is required')
    return budget

async def seal_input_authority(db,owner,request):
    """Capture original authenticated authority before the input publication writer."""
    from src.memory.repository import _effect_mac_key
    from src.model_fabric.effective_policy import current_near_text_policy
    try:
        identifier=UUID(request.idempotency_key)
    except (ValueError,TypeError,AttributeError):
        raise BoardError('near_idempotency_invalid','A canonical UUID idempotency key is required',status_code=422) from None
    if str(identifier)!=request.idempotency_key:
        raise BoardError('near_idempotency_invalid','A canonical UUID idempotency key is required',status_code=422)
    model=_input(request.input)
    config,policy=current_near_text_policy()
    if model.max_output_tokens>config.near_text.max_output_tokens:
        raise BoardError('near_output_limit_exceeded','The configured output limit must be respected',status_code=422)
    root=await db.get(OperatorSession,owner.session_id,populate_existing=True)
    if (root is None or root.principal_id!=owner.principal_id or not root.token_hash
        or root.revoked_at is not None or root.replaced_by_id is not None or root.is_bearer_tombstone
        or min(utc(root.idle_expires_at),utc(root.absolute_expires_at))<=now()):
        raise BoardError('near_root_inactive','The original authenticated Root is required',status_code=403)
    goal=await WorkBoardRepository._validate_goal(db,owner,goal_id=request.goal_id,goal_revision=request.goal_revision)
    budget=finite_goal_budget(goal)
    if str(getattr(goal.status,'value',goal.status))!='active':
        raise BoardError('near_goal_inactive','An active finite Goal is required')
    deadline=min(now()+timedelta(seconds=min(120,budget.max_runtime_seconds)),utc(root.idle_expires_at),utc(root.absolute_expires_at),
        *[utc(t) for t in (goal.due_date,budget.period_expires_at) if t is not None])
    value={'schema':'seraph.near.text.authority.v1','owner':owner.principal_id,'root':owner.session_id,
        'goal':request.goal_id,'goal_revision':request.goal_revision,'request_uuid':request.idempotency_key,
        'input_digest':digest(model.model_dump(mode='json')),'root_idle':utc(root.idle_expires_at).isoformat(),
        'root_absolute':utc(root.absolute_expires_at).isoformat(),'deadline':deadline.isoformat(),
        'workspace_digest':digest(root_binding()),'policy_digest':policy,'policy_revision':config.egress_revision,
        'goal_budget_digest':digest(budget.model_dump(mode='json'))}
    key=_effect_mac_key()
    return canonical({**value,'mac':_mac(value,root.token_hash,key)}).decode()

@dataclass(frozen=True)
class NearContactWitness:
    seal: object=field(repr=False)
    task_id: str
    attempt_id: str
    job_id: str
    input_artifact_id: str
    input_digest: str
    input_metadata_digest: str
    metadata_bytes: bytes=field(repr=False)
    signing_key: bytes=field(repr=False)
    root_token_hash: str=field(repr=False)
    task_revision: int
    attempt_fencing_token: int
    owner_principal_id: str
    owner_session_id: str
    goal_id: str
    goal_revision: int
    deadline: datetime
    max_output_tokens: int
    policy_digest: str
    policy_revision: int
    workspace_digest: str
    private_input: object=field(repr=False)

async def stage_provider_contact(db,task,attempt,inputs,*,run=None):
    from src.memory.repository import _effect_mac_key
    from src.model_fabric.effective_policy import current_near_text_policy
    owner=WorkBoardOwner(principal_id=task.owner_principal_id,session_id=task.owner_session_id)
    resolved=await resolve_input_artifact_for_task(db,owner,artifact_id=task.input_artifact_id,
        capability_id=CAPABILITY,goal_id=task.goal_id,goal_revision=task.goal_revision,expected_task_id=task.task_id)
    model=_input(resolved.input)
    if model!=_input(dict(inputs)) or task.typed_input_digest!=resolved.row.payload_sha256:
        raise BoardError('near_input_changed','The original immutable question changed')
    root=await db.get(OperatorSession,task.owner_session_id,populate_existing=True)
    if root is None:raise BoardError('near_root_inactive','The original Root is unavailable')
    config,policy=current_near_text_policy()
    try:
        metadata=json.loads(resolved.row.document_metadata_json)
        deadline=utc(datetime.fromisoformat(metadata['deadline']))
    except (ValueError,TypeError,KeyError):
        raise BoardError('near_authority_missing','The original sealed authority is required') from None
    result=NearContactWitness(SEAL,task.task_id,attempt.attempt_id,job_id(task,attempt),resolved.row.artifact_id,
        task.typed_input_digest,_metadata_digest(resolved.row),canonical(metadata),_effect_mac_key(),root.token_hash,
        task.task_revision,attempt.fencing_token,task.owner_principal_id,task.owner_session_id,
        task.goal_id,task.goal_revision,deadline,model.max_output_tokens,policy,config.egress_revision,digest(root_binding()),model)
    await recheck_provider_contact(db,run,witness=result,require_run=run is not None)
    return result

async def recheck_provider_contact(db,run,*,witness,require_run=True,require_execution=True,require_lease=True,contact_operation_id=None):
    """Supplied-session SQL/token checks; no filesystem, key getter or provider await."""
    if not isinstance(witness,NearContactWitness) or witness.seal is not SEAL:
        raise BoardError('near_contact_authority_required','The staged native authority is required')
    task=await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==witness.task_id).execution_options(populate_existing=True))
    attempt=await db.get(WorkBoardAttempt,witness.attempt_id,populate_existing=True)
    attempt_count=await db.scalar(select(func.count(WorkBoardAttempt.attempt_id)).where(WorkBoardAttempt.task_id==witness.task_id))
    root=await db.get(OperatorSession,witness.owner_session_id,populate_existing=True)
    row=await db.get(WorkBoardInputArtifact,witness.input_artifact_id,populate_existing=True)
    owner=WorkBoardOwner(principal_id=witness.owner_principal_id,session_id=witness.owner_session_id)
    goal=await WorkBoardRepository._validate_goal(db,owner,goal_id=witness.goal_id,goal_revision=witness.goal_revision)
    budget=finite_goal_budget(goal)
    if (task is None or attempt is None or row is None or root is None or attempt_count!=1
        or task.capability_id!=CAPABILITY or task.requires_review is not True or task.owner_principal_id!=witness.owner_principal_id
        or task.owner_session_id!=witness.owner_session_id or task.goal_id!=witness.goal_id
        or task.goal_revision!=witness.goal_revision or task.input_artifact_id!=row.artifact_id
        or task.typed_input_digest!=witness.input_digest or attempt.task_id!=task.task_id
        or attempt.fencing_token!=witness.attempt_fencing_token or attempt.cancel_requested_at is not None
        or row.bound_task_id!=task.task_id or row.state not in {'bound','consumed'}
        or _metadata_digest(row)!=witness.input_metadata_digest or row.metadata_digest!=witness.input_metadata_digest
        or canonical(json.loads(row.document_metadata_json))!=witness.metadata_bytes
        or root.token_hash!=witness.root_token_hash or root.principal_id!=witness.owner_principal_id
        or root.revoked_at is not None or root.replaced_by_id is not None or root.is_bearer_tombstone
        or budget is None or str(getattr(goal.status,'value',goal.status))!='active'):
        raise BoardError('near_authority_changed','The original native authority changed')
    metadata=json.loads(witness.metadata_bytes);mac=metadata.pop('mac',None)
    if (metadata['owner']!=witness.owner_principal_id or metadata['root']!=witness.owner_session_id
        or metadata['goal']!=witness.goal_id or metadata['goal_revision']!=witness.goal_revision
        or metadata['input_digest']!=digest(witness.private_input.model_dump(mode='json'))
        or metadata['workspace_digest']!=witness.workspace_digest
        or metadata['goal_budget_digest']!=digest(budget.model_dump(mode='json'))
        or not isinstance(mac,str) or not hmac.compare_digest(mac,_mac(metadata,root.token_hash,witness.signing_key))):
        raise BoardError('near_authority_changed','The original signed publication changed')
    expiry=min(utc(root.idle_expires_at),utc(root.absolute_expires_at),utc(datetime.fromisoformat(metadata['root_idle'])),
        utc(datetime.fromisoformat(metadata['root_absolute'])),utc(row.expires_at),
        *[utc(t) for t in (goal.due_date,budget.period_expires_at) if t is not None])
    if now()>=expiry or (require_execution and now()>=witness.deadline):
        raise BoardError('near_authority_expired','The original finite authority expired')
    if (metadata['policy_revision']!=witness.policy_revision or metadata['policy_digest']!=witness.policy_digest):
        raise BoardError('near_policy_changed','The original purpose configuration changed')
    if require_execution and (task.task_revision!=witness.task_revision or task.status is not WorkBoardStatus.running or attempt.ended_at is not None):
        raise BoardError('near_attempt_changed','The current running attempt is required')
    if contact_operation_id is not None:
        operation=await db.get(InferenceCostReservation,contact_operation_id,populate_existing=True)
        historical_jobs=select(WorkBoardAttempt.workflow_run_id).where(WorkBoardAttempt.task_id==witness.task_id)
        contacted=await db.scalar(select(func.count(InferenceCostReservation.operation_id)).where(
            InferenceCostReservation.job_id.in_(historical_jobs),
            InferenceCostReservation.contact_started_at.is_not(None)))
        if (operation is None or operation.job_id!=witness.job_id or operation.runtime_path!=RUNTIME
            or operation.profile_id!='near.text' or operation.state!='reserved'
            or operation.contact_started_at is not None or contacted):
            raise BoardError('near_original_contact_required','An original question may contact the provider only once')
    if require_run:
        if (run is None or run.run_identity!=witness.job_id or run.job_kind!=CAPABILITY
            or run.owner_principal_id!=witness.owner_principal_id or run.operator_session_id!=witness.owner_session_id
            or run.goal_id!=witness.goal_id or run.goal_revision!=witness.goal_revision
            or (attempt.workflow_run_id!=run.run_identity if require_lease else attempt.workflow_run_id not in {None,run.run_identity}) or not binds(task,attempt,run)):
            raise BoardError('near_native_binding_changed','The exact original native job is required')
        expected=spec_for(task,attempt,witness.private_input.model_dump(mode='json'),deadline=witness.deadline,
            metadata=json.loads(witness.metadata_bytes))
        if (canonical(json.loads(run.declared_authority_json))!=canonical(expected.declared_authority)
            or utc(run.deadline_at)!=witness.deadline or run.input_digest!=digest(expected.inputs)
            or run.authority_digest!=digest(expected.declared_authority) or run.run_fingerprint!=expected.run_fingerprint):
            raise BoardError('near_native_binding_changed','The complete original publication and native spec are required')
        if require_lease and (run.status!='running' or run.lease_expires_at is None or utc(run.lease_expires_at)<=now()):
            raise BoardError('near_native_lease_inactive','The original native lease is inactive')


def immutable_inputs(task,inputs):
    model=_input(dict(inputs))
    return {'input_artifact_id':task.input_artifact_id,'typed_input_digest':task.typed_input_digest,
        'max_output_tokens':model.max_output_tokens,'no_learning':True}

def spec_for(task,attempt,inputs,*,deadline,metadata):
    safe=immutable_inputs(task,inputs)
    authority={'principal':task.owner_principal_id,'owner_kind':'user','session_id':task.owner_session_id,
        'goal_id':task.goal_id,'goal_revision':task.goal_revision,'capability_id':CAPABILITY,'capability_version':'1',
        'task_id':task.task_id,'attempt_id':attempt.attempt_id,'input_artifact_id':task.input_artifact_id,
        'input_digest':task.typed_input_digest,'publication_digest':digest(metadata),'runtime_path':RUNTIME,
        'original_deadline':utc(deadline).isoformat(),'output_quota':safe['max_output_tokens'],
        'finite_authority':True,'permissions':['workspace_read','workspace_write','model_inference'],
        'limits':{'max_seconds':120,'max_attempts':1,'answer_bytes':65536},'no_learning':True}
    return DurableJobSpec(identity=DurableJobIdentity(job_id=job_id(task,attempt),owner_kind='user',
        owner_principal_id=task.owner_principal_id,job_kind=CAPABILITY,capability_version='1',
        idempotency_scope='work-board-attempt',idempotency_key=f'{task.task_id}:{attempt.attempt_id}'),
        inputs=safe,session_id=task.owner_session_id,operator_session_id=task.owner_session_id,
        goal_id=task.goal_id,goal_revision=task.goal_revision,priority=task.priority,declared_authority=authority,
        deadline_at=deadline,max_attempts=1,max_outstanding_jobs=1,resource_claims=('serial_remote_inference',),
        run_fingerprint=digest([safe,authority]))

def binds(task,attempt,run):
    try:
        authority=json.loads(run.declared_authority_json)
        maximum=authority['output_quota']
        if type(maximum) is not int or not 1<=maximum<=1024:return False
        safe={'input_artifact_id':task.input_artifact_id,'typed_input_digest':task.typed_input_digest,
            'max_output_tokens':maximum,'no_learning':True}
        return (run.run_identity==job_id(task,attempt) and run.job_kind==CAPABILITY and run.owner_kind=='user'
            and run.owner_principal_id==task.owner_principal_id and run.session_id==run.operator_session_id==task.owner_session_id
            and run.goal_id==task.goal_id and run.goal_revision==task.goal_revision
            and run.idempotency_scope=='work-board-attempt' and run.idempotency_key==f'{task.task_id}:{attempt.attempt_id}'
            and run.capability_version=='1' and run.max_attempts==1 and 0<=run.attempt_count<=1
            and run.input_digest==digest(safe) and run.authority_digest==digest(authority)
            and run.run_fingerprint==digest([safe,authority])
            and run.deadline_at is not None and utc(run.deadline_at)==utc(datetime.fromisoformat(authority['original_deadline']))
            and json.loads(run.resource_claims_json)==['serial_remote_inference']
            and authority['input_digest']==task.typed_input_digest
            and authority['task_id']==task.task_id and authority['attempt_id']==attempt.attempt_id
            and authority['runtime_path']==RUNTIME and authority['input_artifact_id']==task.input_artifact_id)
    except (ValueError,KeyError,TypeError):return False

async def _ledger(db,receipt,*,job):
    row=await ledger_before_output(db,operation_id=receipt.operation_id,job=job)
    if row.actual_cost_microusd!=receipt.cost_microusd or row.provider_operation_id!=receipt.provider_request_id:
        raise BoardError('near_cost_readback_required','The charge receipt differs from its canonical ledger')
    return row

async def ledger_before_output(db,*,operation_id,job):
    row=await db.get(InferenceCostReservation,operation_id,populate_existing=True)
    if (row is None or row.job_id!=job or row.runtime_path!=RUNTIME or row.profile_id!='near.text'
        or row.state!='settled' or row.actual_cost_microusd is None or not row.provider_operation_id):
        raise BoardError('near_cost_readback_required','The authoritative charge must be settled')
    # The canonical continuity owner records overrun review separately from settlement.
    if row.actual_cost_microusd>row.bound_microusd:
        from src.workspace.accounting_witness import unreviewed_overruns
        from src.db.models import InferenceAccountingOwner
        account=await db.scalar(select(InferenceAccountingOwner).where(InferenceAccountingOwner.deployment_id==row.deployment_id).execution_options(populate_existing=True))
        rows=(await db.execute(select(InferenceCostReservation).where(InferenceCostReservation.deployment_id==row.deployment_id))).scalars().all()
        from src.workflows.inference_accounting import _operation_payload
        if account is None or unreviewed_overruns(account.model_dump(mode='json'),[_operation_payload(r) for r in rows]):
            raise BoardError('near_cost_overrun_review_required','The actual charge overrun requires review')
    return row

async def execute(task,attempt,inputs,*,jobs,runner,admission_only,session_provider):
    import asyncio,time
    from src.model_fabric.near_text import invoke_near_text
    from src.model_fabric.accounting import bind_near_contact_authority
    from src.model_fabric.remote_inference_admission import bind_remote_inference_receipt
    from src.model_fabric.contracts import InferenceRequestContext,InferenceWorkload,InferenceRequirements,ModelCapability
    from src.model_fabric.hooks import PersistedRouteReceiptHooks
    from src.model_fabric.repository import model_fabric_repository
    from src.security.trust_contract import TrustPrincipal,PrincipalType,AuthorityGrant,TrustProvenance,ContentOrigin,EgressClass
    from src.work_board.document_pairs import publish_private,read_private
    from src.workspace import canonical_workspace_root
    from config.settings import settings
    async with session_provider() as db:
        witness=await stage_provider_contact(db,task,attempt,inputs)
    metadata=json.loads(witness.metadata_bytes)
    spec=spec_for(task,attempt,inputs,deadline=witness.deadline,metadata=metadata)
    projection=await jobs.get_job(spec.identity.job_id)
    async def native_guard(db,run):
        outstanding=await db.scalar(select(func.count(WorkflowRunState.run_identity)).where(
            WorkflowRunState.owner_principal_id==witness.owner_principal_id,
            WorkflowRunState.job_kind==CAPABILITY,WorkflowRunState.run_identity!=witness.job_id,
            WorkflowRunState.status.in_(('accepted','queued','running','awaiting_approval','paused'))))
        if outstanding:
            raise BoardError('near_owner_outstanding_limit','Only one NEAR operation may be outstanding for this owner')
        await recheck_provider_contact(db,run,witness=witness,require_lease=False)
    if projection is None:
        with policy_lock(witness):
            projection=await jobs.admit_job(spec,admission_authority_check=native_guard)
    elif (projection.get('input_digest')!=digest(spec.inputs) or projection.get('run_fingerprint')!=spec.run_fingerprint):
        raise BoardError('near_native_binding_changed','The original native admission changed')
    if admission_only or projection.get('status')=='succeeded':return {**projection,'admission_only':admission_only}
    if projection.get('status') not in {'accepted','queued'}:
        raise BoardError('near_original_attempt_required','Inspect the original attempt; inference cannot be replayed')
    if projection['status']=='accepted':
        with policy_lock(witness):
            projection=await jobs.queue_job(spec.identity.job_id,expected_revision=projection['revision'],near_text_witness=witness)
    with policy_lock(witness):
        projection=await jobs.claim_job(spec.identity.job_id,owner=runner,lease_seconds=120,
            expected_revision=projection['revision'],expected_fencing_token=projection.get('fencing_token'),claim_authority_check=native_guard)
    fence=projection['lease']['fencing_token']
    # Native and board bindings are canonicalized by the existing dispatcher before execution.
    async with session_provider() as db:
        run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==spec.identity.job_id))
        witness=await stage_provider_contact(db,task,attempt,inputs,run=run)
    async def validate_current():
        assert_policy(witness)
        async with session_provider() as db:
            run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==witness.job_id))
            await recheck_provider_contact(db,run,witness=witness)
    principal=TrustPrincipal(principal_id=task.owner_principal_id,principal_type=PrincipalType.OPERATOR,
        authenticated=True,revoked=False,grants=(AuthorityGrant.MODEL_INFERENCE,),session_id=task.owner_session_id,
        operator_session_id=task.owner_session_id,job_id=witness.job_id)
    context=InferenceRequestContext(principal=principal,session_id=task.owner_session_id,job_id=witness.job_id,
        provenance=(TrustProvenance(origin=ContentOrigin.OPERATOR_INPUT,source_id=task.input_artifact_id,
            data_digest=task.typed_input_digest,egress_class=EgressClass.CLOUD_ALLOWED_FULL),),
        data_digest=task.typed_input_digest,egress_class=EgressClass.CLOUD_ALLOWED_FULL,
        transformation_digest=task.typed_input_digest,request_id=f'near-text:{witness.job_id}',runtime_path=RUNTIME,
        workload=InferenceWorkload.INTERACTIVE,requirements=InferenceRequirements(task_class=RUNTIME,capabilities=(ModelCapability.TEXT,),
            context_tokens=8192,output_tokens=witness.max_output_tokens,max_cost_microusd=None,max_local_resource_ms=None,max_latency_ms=45000),deadline_at=witness.deadline.timestamp(),
        fallback_allowed=False,allowed_profile_ids=('near.text',),allowed_provider_kinds=('near',),requested_profile_id='near.text')
    answer=None
    try:
        with bind_remote_inference_receipt(repository=jobs,job_id=witness.job_id,owner=runner,fencing_token=fence),bind_near_contact_authority(witness):
            answer=await asyncio.wait_for(invoke_near_text(context=context,question=witness.private_input.question,
                max_output_tokens=witness.max_output_tokens,validate_current=validate_current,
                hooks=PersistedRouteReceiptHooks(repository=model_fabric_repository)),timeout=max(0.001,witness.deadline.timestamp()-time.time()))
        await validate_current()
        receipt=answer.receipt.model_copy(update={'task_id':task.task_id,'attempt_id':attempt.attempt_id})
        async with session_provider() as db:await _ledger(db,receipt,job=witness.job_id)
        text=answer.text
        content=canonical({'schema_version':'seraph.near.text.output.v1','task_id':task.task_id,
            'attempt_id':attempt.attempt_id,'job_id':witness.job_id,'text':text,'receipt':receipt.model_dump(mode='json'),'no_learning':True})
        if len(text.encode())>65536:raise BoardError('near_output_overflow','The answer exceeds its bound')
        reference=f'{PREFIX}/{witness.job_id}-{digest(content)}.bin'
        path=canonical_workspace_root(settings.workspace_dir)/reference
        cipher=publish_private(path,content)
        if read_private(path,cipher,maximum=73728)!=content:raise BoardError('near_output_unverified','The private output differs')
        await validate_current()
        await jobs.record_checkpoint(witness.job_id,checkpoint_id='near-private-output',state={
            'reference':reference,**cipher,'plaintext_sha256':digest(content),'operation_id':receipt.operation_id},checkpoint_payload={
            'reference':reference,**cipher,'plaintext_sha256':digest(content),'operation_id':receipt.operation_id},owner=runner,fencing_token=fence)
        await jobs.record_artifact(witness.job_id,file_path=reference,artifact_type='near_text_output',owner=runner,fencing_token=fence)
        await jobs.record_readback(witness.job_id,effect_type='near_text_output',target_path=reference,
            target_digest=cipher['cipher_sha256'],content_sha256=cipher['cipher_sha256'],readback_id='near-text-'+digest(content)[:24],
            verified_at=now().isoformat(),status='succeeded',details={'verified':True,'no_learning':True},owner=runner,fencing_token=fence)
        await validate_current()
        async def terminal_guard(db,run):
            await recheck_provider_contact(db,run,witness=witness)
            await _ledger(db,receipt,job=witness.job_id)
            checkpoints=json.loads(run.checkpoint_receipts_json or '[]')
            if not any(c.get('checkpoint_id')=='near-private-output' and c.get('payload',{}).get('plaintext_sha256')==digest(content) for c in checkpoints):
                raise BoardError('near_output_unverified','The staged output checkpoint changed')
        with policy_lock(witness):
            result=await jobs.transition_job(witness.job_id,'succeeded',owner=runner,fencing_token=fence,
                terminal_authority_check=terminal_guard,result={'no_learning':True,'output_digest':digest(content)},
                result_summary='NEAR HTTPS answer retained privately; settled cost; no_learning')
        return {**result,'memory_status':'no_learning','admission_only':False}
    except asyncio.CancelledError:raise
    except Exception as exc:
        # No output is constructed until accounting really settled; uncertain contact is never replayed.
        from src.model_fabric.near_text_contracts import NearTextError
        from src.workflows.inference_accounting import InferenceAccountingError
        from src.model_fabric.gpu_admission import GpuAdmissionUncertainError
        if not isinstance(exc,(BoardError,NearTextError,InferenceAccountingError,GpuAdmissionUncertainError,TimeoutError)):
            raise
        answer=None
        import logging
        logging.getLogger(__name__).info('NEAR original attempt blocked: %s',getattr(exc,'code',type(exc).__name__))
        latest=await jobs.get_job(witness.job_id)
        if latest.get('status')=='running':
            return await jobs.transition_job(witness.job_id,'blocked',owner=runner,fencing_token=fence,reason='near_cost_readback_required',
                result={'reason_code':'near_cost_readback_required','cause_code':getattr(exc,'code',type(exc).__name__),'no_learning':True},result_summary='NEAR answer unavailable; inspect original accounting')
        if latest.get('status')=='cost_liability':
            return {**latest,'reason_code':'near_cost_readback_required','no_learning':True}
        raise
    finally:
        answer=None


def read_output(task,attempt,run):
    from src.work_board.document_pairs import read_private
    from src.workspace import canonical_workspace_root
    from config.settings import settings
    from src.model_fabric.near_text_contracts import NearTextReceipt
    if not binds(task,attempt,run) or run.status!='succeeded':raise BoardError('near_output_unverified','The actual succeeded source is required')
    checkpoints=json.loads(run.checkpoint_receipts_json or '[]')
    proof=[c.get('payload',{}) for c in checkpoints if c.get('checkpoint_id')=='near-private-output']
    if len(proof)!=1:raise BoardError('near_output_unverified','One private output receipt is required')
    value=proof[0];reference=value['reference']
    if not reference.startswith(PREFIX+'/'+run.run_identity+'-') or '..' in reference:
        raise BoardError('near_output_unverified','The private output path changed')
    effects=json.loads(run.effect_receipts_json or '[]');artifacts=json.loads(run.artifact_receipts_json or '[]')
    if not any(a.get('file_path')==reference and a.get('content_sha256')==value['cipher_sha256'] for a in artifacts):
        raise BoardError('near_output_unverified','The original ciphertext artifact is required')
    if not any(e.get('receipt_kind')=='readback' and e.get('effect_type')=='near_text_output' and e.get('target_path')==reference
        and e.get('status')=='succeeded' and e.get('content_sha256')==value['cipher_sha256'] for e in effects):
        raise BoardError('near_output_unverified','The exact independent readback is required')
    raw=read_private(canonical_workspace_root(settings.workspace_dir)/reference,value,maximum=73728)
    if digest(raw)!=value['plaintext_sha256']:raise BoardError('near_output_unverified','The retained output bytes changed')
    output=json.loads(raw);receipt=NearTextReceipt.model_validate(output['receipt'])
    if (receipt.task_id!=task.task_id or receipt.attempt_id!=attempt.attempt_id or receipt.job_id!=run.run_identity
        or receipt.input_digest!=task.typed_input_digest or receipt.output_digest!=digest(output['text'].encode())
        or len(output['text'].encode())>65536 or output['no_learning'] is not True):
        raise BoardError('near_output_unverified','The private receipt binding changed')
    return output


def assert_policy(witness):
    from src.model_fabric.effective_policy import current_near_text_policy
    config,current=current_near_text_policy()
    if (current!=witness.policy_digest or config.egress_revision!=witness.policy_revision
        or witness.max_output_tokens>config.near_text.max_output_tokens):
        raise BoardError('near_policy_changed','The original current witnessed policy changed')

from contextlib import contextmanager
@contextmanager
def policy_lock(witness):
    from config.settings import settings
    from src.workspace.accounting_witness import maintenance_accounting_lock
    with maintenance_accounting_lock(Path(settings.workspace_dir).resolve()):
        assert_policy(witness)
        yield

async def recheck_native_queue(db,run,*,witness):
    await recheck_provider_contact(db,run,witness=witness,require_lease=False)
