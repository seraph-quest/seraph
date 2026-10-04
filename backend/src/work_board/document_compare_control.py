"""Original comparison inspection and adoption, without starting a parser."""
from __future__ import annotations
import json
import asyncio
from sqlalchemy import select, text, update
from src.db.models import WorkBoardTask, WorkBoardAttempt, WorkBoardStatus, WorkflowRunState
from src.work_board.repository import BoardError
from src.work_board.document_compare_native import (
    CAPABILITY, JOB_KIND, job_id, stage, current, read_output, witness,
    checkpoints, cleanup_proven, adopt_output, canonical, now, utc,
)
from src.workflows.job_runtime import _serialize


async def bound(db, owner, task_id):
    task = await db.scalar(select(WorkBoardTask).where(
        WorkBoardTask.task_id == task_id,
        WorkBoardTask.owner_principal_id == owner.principal_id,
        WorkBoardTask.owner_session_id == owner.session_id,
        WorkBoardTask.capability_id == CAPABILITY))
    if task is None:
        raise BoardError("document_task_unavailable", "The comparison is unavailable", status_code=404)
    attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task_id)
        .order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc()).limit(1))
    run = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == attempt.workflow_run_id)) if attempt else None
    if (run is None or run.job_kind != JOB_KIND or run.run_identity != job_id(task, attempt)
        or run.owner_principal_id != owner.principal_id or run.operator_session_id != owner.session_id):
        raise BoardError("document_original_binding_unavailable", "The original comparison admission is unavailable")
    return task, attempt, run


def inputs(task):
    from src.work_board.dispatcher import _parse_typed_input
    return _parse_typed_input(task)


async def snapshot(db, owner, task_id):
    task, attempt, run = await bound(db, owner, task_id)
    projection = _serialize(run)
    proven = cleanup_proven(task, attempt, projection)
    recoverable = False; retryable = False; authorized = False
    reason = run.failure_reason
    try:
        staged = await stage(db, task, attempt, run, inputs(task))
        await current(db, task, attempt, run, staged, require_lease=False)
        authorized = True
        read_output(task, attempt, run)
        recoverable = (run.status in {"running", "blocked", "unknown_external_effect"}
            and (run.lease_expires_at is None or utc(run.lease_expires_at) <= now()))
    except (BoardError, OSError, ValueError, TypeError, KeyError):
        state = checkpoints(run)
        if proven and "document-output" not in state:
            try:
                actual, _ = witness(state.get("document-child") or state["document-capacity"])
                if actual["parser_exit"] == 0:
                    reason = "document_output_lost_nonretryable"
                elif (authorized and actual["reason"] == "document_supervisor_interrupted" and actual["parser_exit"] == -9
                    and run.attempt_count < 2 and attempt.cancel_requested_at is None
                    and utc(run.deadline_at) > now() + __import__('datetime').timedelta(seconds=35)):
                    retryable = True; reason = "document_supervisor_interrupted"
            except (OSError, ValueError, TypeError, KeyError):
                pass
    return {"task_id": task_id, "task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "job_id": run.run_identity,
        "status": run.status, "reason_code": reason, "deadline_at": projection["deadline_at"],
        "attempt_count": run.attempt_count, "max_attempts": 2,
        "cleanup_proven": proven, "recoverable": recoverable, "retryable": retryable,
        "quiescence_recorded": "document-reaped" in checkpoints(run),
        "cancel_requested": attempt.cancel_requested_at is not None,
        "report_available": run.status == "succeeded" and task.status in {WorkBoardStatus.done, WorkBoardStatus.review},
        "no_learning": True,
        "recovery_limit": "Only original verified output with an exact parser reap witness can be adopted. Lost output is blocked; expiry alone never releases parser capacity."}


async def recover(dispatcher, owner, task_id, request):
    jobs = dispatcher.jobs
    async with jobs._session() as initial:
        original_task, original_attempt, original_run = await bound(initial, owner, task_id)
        records = checkpoints(original_run)
        prior = records.get("document-operator-recovery")
        if prior and prior.get("idempotency_key") == request.idempotency_key:
            if prior.get("requested_revision") != request.expected_revision:
                raise BoardError("document_control_conflict", "The original recovery request differs")
            if original_run.status == "succeeded":
                read_output(original_task, original_attempt, original_run)
                return {"status": "succeeded", "replayed": True, "no_learning": True}
        selected = inputs(original_task)
        staged = await stage(initial, original_task, original_attempt, original_run, selected)
        read_output(original_task, original_attempt, original_run)
        binding = records["document-child"]
        actual, witness_sha = witness(binding)
        if actual["parser_exit"] != 0 or actual["reason"] is not None:
            raise BoardError("document_output_unverified", "The original parser output cannot be adopted")
        original_binding = (original_run.revision, original_run.checkpoint_receipts_json, original_run.effect_receipts_json)
    async with jobs._session() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        task, attempt, run = await bound(db, owner, task_id)
        if task.task_revision != request.expected_revision:
            raise BoardError("document_revision_stale", "Read the current comparison before recovery")
        if (run.revision, run.checkpoint_receipts_json, run.effect_receipts_json) != original_binding:
            raise BoardError("document_recovery_changed", "The original output or witness binding changed")
        await current(db, task, attempt, run, staged, require_lease=False)
        stamp = now()
        if (run.status not in {"running", "blocked", "unknown_external_effect"}
            or run.lease_expires_at and utc(run.lease_expires_at) > stamp):
            raise BoardError("document_live_lease", "The original parser controller lease remains active")
        records = json.loads(run.checkpoint_receipts_json)
        records.append({"checkpoint_id": "document-operator-recovery", "payload": {
            "idempotency_key": request.idempotency_key, "requested_revision": request.expected_revision,
            "nonce": binding["nonce"], "generation": binding["generation"],
            "witness_sha256": witness_sha, "no_new_execution": True, "no_learning": True}})
        # Board revision invalidates the old worker. This lease owns adoption
        # only; the original job UUID, fence, count and deadline stay fixed.
        changed = await db.execute(update(WorkflowRunState).where(
            WorkflowRunState.run_identity == run.run_identity, WorkflowRunState.revision == run.revision,
            WorkflowRunState.fencing_token == run.fencing_token).values(status="running",
                lease_owner=dispatcher.runner_id, lease_expires_at=run.deadline_at,
                checkpoint_receipts_json=canonical(records).decode(), revision=run.revision + 1))
        board = await db.execute(update(WorkBoardTask).where(WorkBoardTask.task_id == task_id,
            WorkBoardTask.task_revision == request.expected_revision, WorkBoardTask.status == WorkBoardStatus.running)
            .values(task_revision=task.task_revision + 1))
        owned = await db.execute(update(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == attempt.attempt_id,
            WorkBoardAttempt.fencing_token == attempt.fencing_token, WorkBoardAttempt.ended_at.is_(None),
            WorkBoardAttempt.cancel_requested_at.is_(None)).values(lease_expires_at=run.deadline_at))
        if changed.rowcount != 1 or board.rowcount != 1 or owned.rowcount != 1:
            raise BoardError("document_recovery_cas_changed", "The original attempt changed")
        await db.refresh(task); await db.refresh(attempt)
        fence = run.fencing_token
    finished = await adopt_output(jobs, task, attempt, selected, dispatcher.runner_id, fence)
    proof = dispatcher._workflow_readback(finished, finished["job_id"])
    await dispatcher._project(task, attempt, board_revision=task.task_revision,
        status=WorkBoardStatus.review if task.requires_review else WorkBoardStatus.done,
        outcome="verified", proof=proof, lease_owner=attempt.lease_owner,
        artifact_refs=finished["artifacts"], result_refs=[{"job_id": finished["job_id"], "status": "succeeded", "verified": True}])
    return {"status": "succeeded", "replayed": False, "no_learning": True}


async def retry(dispatcher,owner,task_id,request):
    """One explicit second parser generation after exact terminated-child proof."""
    from datetime import timedelta
    from src.work_board.document_compare_native import execute
    jobs=dispatcher.jobs
    async with jobs._session() as initial:
        task,attempt,run=await bound(initial,owner,task_id)
        previous=checkpoints(run).get("document-parser-retry")
        if previous and previous.get("idempotency_key")==request.idempotency_key:
            if previous.get("requested_revision")!=request.expected_revision:
                raise BoardError("document_control_conflict","The original retry request differs")
            return {"status":run.status,"job_id":run.run_identity,"replayed":True,"no_learning":True}
        selected=inputs(task);staged=await stage(initial,task,attempt,run,selected)
        state=checkpoints(run);binding=state.get("document-child") or state.get("document-capacity")
        actual,witness_sha=witness(binding)
        original=(run.revision,run.checkpoint_receipts_json)
    if (actual["reason"]!="document_supervisor_interrupted" or actual["parser_exit"]!=-9
        or "document-output" in state or binding["generation"]!=1):
        raise BoardError("document_retry_unavailable","Only a known terminated first-generation interruption may retry")
    async with jobs._session() as db:
        await db.execute(text("BEGIN IMMEDIATE"));task,attempt,run=await bound(db,owner,task_id)
        if task.task_revision!=request.expected_revision or (run.revision,run.checkpoint_receipts_json)!=original:
            raise BoardError("document_revision_stale","Read the exact original comparison before retry")
        await current(db,task,attempt,run,staged)
        stamp=now()
        if (run.status not in {"running","blocked","unknown_external_effect"} or run.attempt_count!=1
            or run.max_attempts!=2 or utc(run.deadline_at)<=stamp+timedelta(seconds=35)):
            raise BoardError("document_retry_allowance_expired","The original allowance cannot cover another bounded launch and cleanup")
        records=json.loads(run.checkpoint_receipts_json)
        prior={item["checkpoint_id"]:item.get("payload",{}) for item in records
            if item.get("checkpoint_id") in {"document-capacity","document-child","document-reaped"}}
        records=[item for item in records if item.get("checkpoint_id") not in prior]
        records.extend([{"checkpoint_id":"document-history:1","payload":prior},
            {"checkpoint_id":"document-parser-retry","payload":{"generation":2,
                "idempotency_key":request.idempotency_key,"requested_revision":request.expected_revision,
                "original_nonce":binding["nonce"],"witness_sha256":witness_sha,"no_learning":True}}])
        changed=await db.execute(update(WorkflowRunState).execution_options(synchronize_session=False).where(
            WorkflowRunState.run_identity==run.run_identity,WorkflowRunState.revision==run.revision,
            WorkflowRunState.fencing_token==run.fencing_token).values(status="queued",lease_owner=None,
                lease_expires_at=None,checkpoint_receipts_json=canonical(records).decode(),revision=run.revision+1))
        board=await db.execute(update(WorkBoardTask).where(WorkBoardTask.task_id==task_id,
            WorkBoardTask.task_revision==request.expected_revision,WorkBoardTask.status==WorkBoardStatus.running)
            .values(task_revision=task.task_revision+1))
        if changed.rowcount!=1 or board.rowcount!=1:raise BoardError("document_retry_fence_changed","The original attempt changed")
        await db.refresh(task);await db.refresh(attempt);deadline=utc(run.deadline_at)
    worker=asyncio.create_task(execute(task,attempt,selected,jobs=jobs,runner=dispatcher.runner_id,
        deadline=deadline,admission_only=False))
    key=(task.task_id,attempt.attempt_id);dispatcher._active_worker_tasks[key]=worker
    try:finished=await worker
    finally:
        if dispatcher._active_worker_tasks.get(key) is worker:dispatcher._active_worker_tasks.pop(key,None)
    if finished["status"]=="succeeded":
        proof=dispatcher._workflow_readback(finished,finished["job_id"])
        await dispatcher._project(task,attempt,board_revision=task.task_revision,
            status=WorkBoardStatus.review if task.requires_review else WorkBoardStatus.done,outcome="verified",
            proof=proof,lease_owner=attempt.lease_owner,artifact_refs=finished["artifacts"],
            result_refs=[{"job_id":finished["job_id"],"status":"succeeded","verified":True}])
    return {"status":finished["status"],"job_id":finished["job_id"],"no_learning":True}


async def reconcile(dispatcher,owner,task_id,request):
    from src.work_board.document_compare_native import reconcile_reap
    async with dispatcher.jobs._session() as db:
        task,attempt,run=await bound(db,owner,task_id)
        if task.task_revision!=request.expected_revision:
            raise BoardError("document_revision_stale","Read the exact original comparison before cleanup")
    settled=await reconcile_reap(dispatcher.jobs,task,attempt)
    return {"status":settled["status"],"quiescence_recorded":True,"no_learning":True}
