"""Explicit original-slot inspection and adoption; never package re-execution."""
from __future__ import annotations
import json
from datetime import timezone
from sqlalchemy import select,text,update
from src.db.models import WorkBoardTask,WorkBoardAttempt,WorkBoardStatus,WorkflowRunState
from src.work_board.repository import BoardError
from src.work_board.tool_package_native import (CAPABILITY,cleanup_proven,binds,current,
    now,verified_output,read_private,canonical,digest,runtime_root,adopt_output)
from src.execution.tool_package_profile import expected_output,MAX_OUTPUT,ToolPackageBlocked
from src.workflows.job_runtime import _serialize
from src.workspace import canonical_workspace_root
from config.settings import settings


async def bound(db,owner,task_id):
    task=await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==task_id,
        WorkBoardTask.owner_principal_id==owner.principal_id,WorkBoardTask.owner_session_id==owner.session_id,
        WorkBoardTask.capability_id==CAPABILITY))
    if task is None:raise BoardError('tool_package_unavailable','Formatter task unavailable',status_code=404)
    attempt=await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id==task_id)
        .order_by(WorkBoardAttempt.created_at.desc(),WorkBoardAttempt.attempt_id.desc()).limit(1))
    run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==attempt.workflow_run_id)) if attempt else None
    if run is None or not binds(task,attempt,run):raise BoardError('tool_package_binding_unavailable','The original formatter admission is unavailable')
    return task,attempt,run


def reserved_output(task,attempt,run):
    from src.work_board.dispatcher import _parse_typed_input
    from src.execution.repo_supervisor import start_identity
    projection=_serialize(run)
    if not cleanup_proven(task,attempt,projection):raise ToolPackageBlocked('tool_package_cleanup_unproven')
    records=projection['checkpoints']
    reserved=next(item['payload'] for item in records if item.get('checkpoint_id')=='tool-package:reservation')
    clean=next(item['payload'] for item in records if item.get('checkpoint_id')=='tool-package:cleanup')
    expected=expected_output(_parse_typed_input(task)['json_text'].encode())
    from src.work_board.tool_package_native import PREFIX
    reference=PREFIX+digest(canonical([task.task_id,attempt.attempt_id]))+'-'+digest(expected)+'.json'
    stage_ref='artifacts/tool-package-runs/'+digest(canonical(run.run_identity))+f'-{reserved["fence"]}'
    if (reserved['job_id']!=run.run_identity or reserved['file_path']!=reference
        or reserved['stage_ref']!=stage_ref or reserved['content_sha256']!=digest(expected)
        or reserved['byte_count']!=len(expected) or reserved['no_learning'] is not True):
        raise ToolPackageBlocked('tool_package_output_reservation_changed')
    stage=canonical_workspace_root(settings.workspace_dir)/stage_ref
    proof=read_private(stage,'out/supervisor-result.json',32768)
    actual=json.loads(proof)
    if (digest(proof)!=clean['result_sha256'] or actual['exit_code']!=0 or actual.get('reason')
        or actual['supervisor_pid']!=clean['supervisor_pid'] or actual['supervisor_start']!=clean['supervisor_start']
        or start_identity(actual['supervisor_pid'])==actual['supervisor_start']
        or actual['job_id']!=run.run_identity or actual['fence']!=reserved['fence']
        or digest(actual['token'].encode())!=reserved['dispatch_binding_sha256']
        or actual['runtime_digest']!=reserved['runtime_digest'] or actual['package_sha256']!=reserved['package_sha256']
        or actual['input_sha256']!=digest(_parse_typed_input(task)['json_text'].encode())
        or actual['cleanup_proven'] is not True or read_private(stage,'out/result.json',MAX_OUTPUT)!=expected):
        raise ToolPackageBlocked('tool_package_reserved_output_unverified')
    return reference,expected


async def snapshot(jobs,db,owner,task_id):
    task,attempt,run=await bound(db,owner,task_id)
    projection=_serialize(run);recoverable=False
    try:
        await current(db,task,attempt,run,require_lease=False)
        reserved_output(task,attempt,run)
        expiry=run.lease_expires_at
        recoverable=(run.status in {'running','unknown_external_effect','blocked'} and
            (expiry is None or expiry.replace(tzinfo=timezone.utc)<=now()))
    except (ToolPackageBlocked,BoardError,OSError,ValueError,KeyError,TypeError,StopIteration):pass
    return {'task_id':task_id,'task_revision':task.task_revision,'attempt_id':attempt.attempt_id,
        'job_id':run.run_identity,'status':run.status,'deadline_at':projection['deadline_at'],
        'attempt_count':run.attempt_count,'max_attempts':1,'profile':projection['declared_authority']['runtime']['profile'],
        'cleanup_proven':bool(cleanup_proven(task,attempt,projection)),'recoverable':bool(recoverable),
        'report_available':run.status=='succeeded' and task.status in {WorkBoardStatus.done,WorkBoardStatus.review},
        'cancel_available':not attempt.ended_at and run.status!='succeeded','no_learning':True,
        'recovery_limit':'Only the exact reserved finished output with actual reap proof may be adopted within the original deadline. Uncertain execution remains Blocked; no package retry.'}


async def recover(dispatcher,owner,task_id,request):
    jobs=dispatcher.jobs
    async with jobs._session() as db:
        await db.execute(text('BEGIN IMMEDIATE'))
        task,attempt,run=await bound(db,owner,task_id)
        records=json.loads(run.checkpoint_receipts_json)
        prior=next((item.get('payload') for item in records if item.get('checkpoint_id')=='tool-package:operator-recovery'),None)
        replay=prior and prior.get('idempotency_key')==request.idempotency_key
        if replay and prior['requested_revision']!=request.expected_revision:
            raise BoardError('tool_package_control_conflict','The original recovery request differs')
        if run.status=='succeeded':
            verified_output(task,attempt,run)
            return {'status':'succeeded','replayed':bool(replay),'no_learning':True}
        if task.task_revision!=request.expected_revision:raise BoardError('tool_package_revision_stale','Reload the current formatter task')
        if dispatcher._active_worker_tasks.get((task.task_id,attempt.attempt_id)) is not None:
            raise BoardError('tool_package_execution_active','The actual formatter controller is still active')
        await current(db,task,attempt,run,require_lease=False)
        reference,expected=reserved_output(task,attempt,run)
        stamp=now();deadline=run.deadline_at.replace(tzinfo=timezone.utc)
        if (run.status not in {'running','unknown_external_effect','blocked'} or
            run.lease_expires_at and run.lease_expires_at.replace(tzinfo=timezone.utc)>stamp):
            raise BoardError('tool_package_live_lease','The original execution lease remains active')
        receipt={'idempotency_key':request.idempotency_key,'requested_revision':request.expected_revision,
            'task_id':task.task_id,'attempt_id':attempt.attempt_id,'job_id':run.run_identity,
            'fence':run.fencing_token,'output_sha256':digest(expected),'no_new_execution':True,'no_learning':True}
        records.append({'checkpoint_id':'tool-package:operator-recovery','payload':receipt})
        # Original attempt/fence/deadline/count remain unchanged. This lease
        # owns adoption of a positively reaped output, never physical work.
        changed=await db.execute(update(WorkflowRunState).where(WorkflowRunState.run_identity==run.run_identity,
            WorkflowRunState.revision==run.revision,WorkflowRunState.fencing_token==run.fencing_token)
            .values(status='running',lease_owner=dispatcher.runner_id,lease_expires_at=deadline,
                checkpoint_receipts_json=canonical(records).decode(),revision=run.revision+1))
        board=await db.execute(update(WorkBoardTask).where(WorkBoardTask.task_id==task_id,
            WorkBoardTask.task_revision==request.expected_revision,WorkBoardTask.status==WorkBoardStatus.running)
            .values(task_revision=task.task_revision+1))
        owned=await db.execute(update(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id==attempt.attempt_id,
            WorkBoardAttempt.fencing_token==attempt.fencing_token,WorkBoardAttempt.ended_at.is_(None),
            WorkBoardAttempt.cancel_requested_at.is_(None)).values(lease_expires_at=deadline))
        if changed.rowcount!=1 or board.rowcount!=1 or owned.rowcount!=1:raise BoardError('tool_package_recovery_cas_changed','The original formatter attempt changed')
        await db.refresh(task);await db.refresh(attempt)
        fence=run.fencing_token
    finished=await adopt_output(jobs,task,attempt,dispatcher.runner_id,reference,expected,fence)
    proof=dispatcher._workflow_readback(finished,finished['job_id'])
    await dispatcher._project(task,attempt,board_revision=task.task_revision,
        status=WorkBoardStatus.review if task.requires_review else WorkBoardStatus.done,outcome='verified',proof=proof,
        lease_owner=attempt.lease_owner,artifact_refs=finished['artifacts'],result_refs=[{'job_id':finished['job_id'],'status':'succeeded','verified':True}])
    return {'status':'succeeded','replayed':False,'no_learning':True}
