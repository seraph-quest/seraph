"""One fixed private comparison through canonical WorkBoard/native jobs."""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import struct
import sys
import uuid
from sqlalchemy import select, text, update
from config.settings import settings
from src.db.models import OperatorSession, WorkBoardAttempt, WorkBoardInputArtifact, WorkBoardStatus, WorkBoardTask, WorkflowRunState
from src.work_board.contracts import WorkBoardOwner
from src.work_board.document_compare_contracts import DocumentCompareInput
from src.work_board.document_compare_parser import CAPABILITY, JOB_KIND, canonical, sha256
from src.work_board.document_pairs import source_pair, read_private, publish_private, metadata, authority
from src.work_board.input_artifacts import _metadata_digest, _open_input_artifact_parent, _private_input_file_metadata
from src.work_board.pipelines import now, utc, root_binding
from src.work_board.repository import BoardError
from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec
from src.workspace import canonical_workspace_root

PREFIX="artifacts/work-board/document-output"
SEAL=object()
LAUNCH_SECONDS=35


async def execution_expiries(db,task):
    """Stage finite SQL facts before admission; no writer or filesystem I/O."""
    row=await db.get(WorkBoardInputArtifact,task.input_artifact_id,populate_existing=True)
    if row is None:raise BoardError("document_pair_missing","The private pair is unavailable")
    owner=WorkBoardOwner(principal_id=task.owner_principal_id,session_id=task.owner_session_id)
    value=metadata(row)
    goal,budget=await authority(db,owner,row,value,value["root"])
    session=await db.get(OperatorSession,task.owner_session_id,populate_existing=True)
    if (session is None or session.principal_id!=task.owner_principal_id or session.revoked_at is not None
        or session.replaced_by_id is not None or session.is_bearer_tombstone
        or utc(session.idle_expires_at)<=now() or utc(session.absolute_expires_at)<=now()):
        raise BoardError("document_operator_session_inactive","The original operator session is inactive")
    return {key:utc(value).isoformat() if value is not None else None for key,value in {
        "artifact":row.expires_at,"goal":goal.due_date,"budget":budget.period_expires_at,
        "root_absolute":session.absolute_expires_at,"root_idle":session.idle_expires_at}.items()}


def expiry_cap(expiries):
    if (not isinstance(expiries,dict) or set(expiries)!={"artifact","goal","budget","root_absolute","root_idle"}
        or any(expiries[key] is None for key in ("artifact","root_absolute","root_idle"))):
        raise BoardError("document_execution_window_required","The original finite execution window is required")
    try:
        stamps=[datetime.fromisoformat(value) for value in expiries.values() if value is not None]
        if any(stamp.tzinfo is None or stamp.utcoffset()!=timedelta(0) for stamp in stamps):raise ValueError()
        if any(not isinstance(value,str) for value in expiries.values() if value is not None):raise ValueError()
    except (ValueError,TypeError):
        raise BoardError("document_execution_window_required","The original finite execution window is required") from None
    return min(stamps)


async def validate_expiries(db,task,attempt,run):
    original=json.loads(run.declared_authority_json).get("execution_expiries")
    bound=expiry_cap(original)
    live=await execution_expiries(db,task)
    if (any(live[key]!=original[key] for key in original if key!="root_idle")
        or utc(datetime.fromisoformat(live["root_idle"]))<utc(datetime.fromisoformat(original["root_idle"]))
        or utc(run.deadline_at)>min(bound,utc(attempt.started_at)+timedelta(seconds=70))):
        raise BoardError("document_execution_expiry_changed","The original execution expiry facts changed")


def remaining(deadline,maximum,*,cleanup=0):
    value=min(maximum,(utc(deadline)-now()).total_seconds()-cleanup)
    if value<=0:raise BoardError("document_job_window_expired","The original job window expired")
    return value


def require_launch_window(deadline):
    if remaining(deadline,LAUNCH_SECONDS)<LAUNCH_SECONDS:
        raise BoardError("document_insufficient_execution_window","The original window cannot cover parser execution and cleanup")


def job_id(task, attempt):
    return "document-compare:"+sha256(canonical([task.task_id,attempt.attempt_id]))[:40]


def immutable_inputs(task,inputs):
    model=DocumentCompareInput.model_validate(dict(inputs))
    if not task.input_artifact_id or not task.typed_input_digest:
        raise BoardError("document_pair_required","A sealed private pair is required")
    return {"input":model.model_dump(),"input_artifact_id":task.input_artifact_id,
        "typed_input_digest":task.typed_input_digest,"no_learning":True}


def authority_for(task,attempt,inputs):
    return {"principal":task.owner_principal_id,"owner_kind":"user","session_id":task.owner_session_id,
        "goal_id":task.goal_id,"goal_revision":task.goal_revision,"capability_id":CAPABILITY,"capability_version":"1",
        "board_task_id":task.task_id,"board_attempt_id":attempt.attempt_id,
        "input_artifact_id":task.input_artifact_id,"typed_input_digest":task.typed_input_digest,
        "live_root_digest":sha256(canonical(root_binding())),"finite_authority":True,
        "permissions":["workspace_read","workspace_write"],
        "limits":{"memory_bytes":256*1024*1024,"wall_seconds":30,"cpu_seconds":10,
            "max_attempts":2,"job_seconds":70,"encrypted_output_bytes":512*1024},"no_learning":True}


def spec_for(task,attempt,inputs,*,deadline,expiries):
    deadline=min(utc(deadline),utc(attempt.started_at)+timedelta(seconds=70),expiry_cap(expiries))
    safe=immutable_inputs(task,inputs); declared=authority_for(task,attempt,inputs)
    declared["execution_expiries"]=expiries
    return DurableJobSpec(identity=DurableJobIdentity(job_id=job_id(task,attempt),owner_kind="user",
        owner_principal_id=task.owner_principal_id,job_kind=JOB_KIND,capability_version="1",
        idempotency_scope="work-board-attempt",idempotency_key=f"{task.task_id}:{attempt.attempt_id}"),
        inputs=safe,session_id=task.owner_session_id,operator_session_id=task.owner_session_id,
        goal_id=task.goal_id,goal_revision=task.goal_revision,priority=task.priority,declared_authority=declared,
        deadline_at=deadline,max_attempts=2,max_outstanding_jobs=1,budget_microusd=0,
        run_fingerprint=sha256(canonical([task.task_id,attempt.attempt_id,safe,declared])))


def checkpoints(run):
    raw=run.get("checkpoints",[]) if isinstance(run,dict) else json.loads(run.checkpoint_receipts_json)
    if not isinstance(raw,list) or len(raw)>100: raise BoardError("document_checkpoint_invalid","The native job needs reconciliation")
    return {r["checkpoint_id"]:r.get("payload",{}) for r in raw if isinstance(r,dict) and "checkpoint_id" in r}


def directory_path(identifier):
    return canonical_workspace_root(settings.workspace_dir)/PREFIX/identifier


def witness(binding):
    path=directory_path(binding["input_artifact_id"])/(binding["nonce"]+".witness.json")
    parent,leaf=_open_input_artifact_parent(path,create=False); fd=-1
    try:
        fd=os.open(leaf,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0),dir_fd=parent); stat=os.fstat(fd)
        if not _private_input_file_metadata(stat) or not 1<=stat.st_size<=4096:
            raise ValueError("document witness metadata invalid")
        raw=os.read(fd,4097)
    finally:
        if fd>=0:os.close(fd)
        os.close(parent)
    value=json.loads(raw)
    if (set(value)!={"job_id","input_digest","generation","nonce","supervisor_pid","parser_pid","parser_exit","wait_reaped","reason"}
        or any(value.get(k)!=binding[k] for k in ("job_id","input_digest","generation","nonce"))
        or value.get("wait_reaped") is not True or type(value.get("parser_exit")) is not int
        or any(type(value.get(k)) is not int or value[k]<=0 for k in ("supervisor_pid","parser_pid"))
        or any(k in binding and value[k]!=binding[k] for k in ("supervisor_pid","parser_pid"))):
        raise ValueError("document witness original identity mismatch")
    return value,sha256(raw)


@dataclass(frozen=True)
class Stage:
    seal:object
    job:str
    authority_json:str
    input_digest:str
    root_digest:str


async def stage(db,task,attempt,run,inputs,*,physical=True):
    if physical:await source_pair(db,task,inputs)
    await validate_expiries(db,task,attempt,run)
    row=await db.get(WorkBoardInputArtifact,task.input_artifact_id,populate_existing=True)
    if row is None:raise BoardError("document_pair_missing","The private pair is unavailable")
    return Stage(SEAL,run.run_identity,run.declared_authority_json,_metadata_digest(row),sha256(canonical(root_binding())))


async def current(db,task,attempt,run,staged,*,require_lease=True):
    """Pure SQL validation; no filesystem, Vault or nested writer."""
    if (staged is None or staged.seal is not SEAL or staged.job!=run.run_identity
        or staged.authority_json!=run.declared_authority_json
        or json.loads(run.declared_authority_json)["live_root_digest"]!=staged.root_digest):
        raise BoardError("document_stage_required","The original physical authority needs readback")
    owner=WorkBoardOwner(principal_id=task.owner_principal_id,session_id=task.owner_session_id)
    row=await db.get(WorkBoardInputArtifact,task.input_artifact_id,populate_existing=True)
    # The root is already physically staged; compare its sealed representation.
    await authority(db,owner,row,metadata(row),json.loads(row.document_metadata_json)["root"])
    await validate_expiries(db,task,attempt,run)
    if (_metadata_digest(row)!=staged.input_digest or row.metadata_digest!=staged.input_digest
        or row.state not in {"bound","consumed"} or row.bound_task_id!=task.task_id
        or row.payload_sha256!=task.typed_input_digest):
        raise BoardError("document_pair_binding_changed","The sealed pair changed")
    stamp=now()
    session=await db.scalar(select(OperatorSession.id).where(OperatorSession.id==task.owner_session_id,
        OperatorSession.principal_id==task.owner_principal_id,OperatorSession.revoked_at.is_(None),
        OperatorSession.replaced_by_id.is_(None),OperatorSession.is_bearer_tombstone.is_(False),
        OperatorSession.idle_expires_at>stamp,OperatorSession.absolute_expires_at>stamp))
    active=await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==task.task_id).execution_options(populate_existing=True))
    live=await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id==attempt.attempt_id).execution_options(populate_existing=True))
    if session is None:raise BoardError("document_operator_session_inactive","The original operator session is inactive")
    if active is None or live is None:raise BoardError("document_attempt_missing","The original task or attempt is unavailable")
    if active.task_revision!=task.task_revision:raise BoardError("document_task_revision_changed","The original task revision changed")
    if live.workflow_run_id!=run.run_identity:raise BoardError("document_attempt_job_changed","The original attempt job binding changed")
    if live.lease_owner!=attempt.lease_owner:raise BoardError("document_attempt_lease_owner_changed","The original board lease owner changed")
    if utc(run.deadline_at)<=stamp:raise BoardError("document_job_window_expired","The original job window expired")
    if active.status!=WorkBoardStatus.running:raise BoardError("document_task_not_running","The original task is no longer running")
    if live.fencing_token!=attempt.fencing_token:raise BoardError("document_attempt_fence_changed","The original board fence changed")
    if require_lease and (not live.lease_expires_at or utc(live.lease_expires_at)<=stamp):
        raise BoardError("document_attempt_lease_expired","The original board lease expired")
    if active.owner_principal_id!=task.owner_principal_id or active.owner_session_id!=task.owner_session_id:
        raise BoardError("document_task_owner_changed","The original task owner changed")
    if active.capability_id!=CAPABILITY or active.goal_id!=task.goal_id or active.goal_revision!=task.goal_revision:
        raise BoardError("document_task_goal_or_capability_changed","The original task Goal or capability changed")
    if live.task_id!=task.task_id or live.ended_at is not None or live.cancel_requested_at is not None:
        raise BoardError("document_attempt_closed_or_cancelled","The original attempt is closed or cancelled")
    if (session is None or active is None or live is None or utc(run.deadline_at)<=stamp
        or active.owner_principal_id!=task.owner_principal_id or active.owner_session_id!=task.owner_session_id
        or active.capability_id!=CAPABILITY or active.goal_id!=task.goal_id or active.goal_revision!=task.goal_revision
        or active.task_revision!=task.task_revision or active.status!=WorkBoardStatus.running
        or live.task_id!=task.task_id or live.workflow_run_id!=run.run_identity or live.ended_at is not None
        or live.cancel_requested_at is not None or live.fencing_token!=attempt.fencing_token
        or (require_lease and (live.lease_owner!=attempt.lease_owner or not live.lease_expires_at or utc(live.lease_expires_at)<=stamp))):
        raise BoardError("document_current_authority_changed","The original session, Goal or attempt changed")


async def stage_current(jobs,task,attempt,inputs,*,physical=True):
    async with jobs._session() as db:
        run=await jobs._fetch(db,job_id(task,attempt)); staged=await stage(db,task,attempt,run,inputs,physical=physical)
    async with jobs._session() as db:
        run=await jobs._fetch(db,job_id(task,attempt)); await current(db,task,attempt,run,staged)
    return staged


def cleanup_proven(task,attempt,projection):
    state=checkpoints(projection); binding=state.get("document-child") or state.get("document-capacity")
    if binding is None:return projection.get("status") in {"accepted","queued","cancelled"}
    try:
        actual,_sha=witness(binding)
        return bool(actual["wait_reaped"] and binding["job_id"]==job_id(task,attempt))
    except (OSError,ValueError,KeyError,TypeError):return False


async def reconcile_reap(jobs,task,attempt):
    """Record physical quiescence only; this grants no execution/adoption."""
    async with jobs._session() as db:
        original=await jobs._fetch(db,job_id(task,attempt))
        state=checkpoints(original);binding=state.get("document-child") or state.get("document-capacity")
        if binding is None:return _serialize_job(original)
        actual,witness_sha=witness(binding)
        original_checkpoints=original.checkpoint_receipts_json
        root_digest=sha256(canonical(root_binding()))
        original_authority=original.declared_authority_json
    # Filesystem proof and Root readback finished before acquiring the writer.
    async with jobs._session() as db:
        await db.execute(text("BEGIN IMMEDIATE"));run=await jobs._fetch(db,job_id(task,attempt))
        active=await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==task.task_id))
        live=await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id==attempt.attempt_id))
        declared=json.loads(run.declared_authority_json)
        if (run.checkpoint_receipts_json!=original_checkpoints or run.declared_authority_json!=original_authority
            or run.job_kind!=JOB_KIND or run.owner_principal_id!=task.owner_principal_id
            or run.operator_session_id!=task.owner_session_id or declared["live_root_digest"]!=root_digest
            or declared["typed_input_digest"]!=binding["input_digest"]
            or declared["input_artifact_id"]!=binding["input_artifact_id"]
            or active is None or live is None or live.task_id!=task.task_id
            or live.workflow_run_id!=run.run_identity or live.fencing_token!=attempt.fencing_token
            or active.owner_principal_id!=task.owner_principal_id or active.owner_session_id!=task.owner_session_id):
            raise BoardError("document_reap_binding_changed","The exact original parser witness changed")
        existing=checkpoints(run).get("document-reaped")
        if existing:
            if existing.get("witness_sha256")!=witness_sha:
                raise BoardError("document_reap_binding_changed","The committed parser witness changed")
            return _serialize_job(run)
        records=json.loads(run.checkpoint_receipts_json)
        records.append({"checkpoint_id":"document-reaped","payload":{"binding":binding,
            "witness_sha256":witness_sha,"wait_reaped":True,"parser_exit":actual["parser_exit"]},"safe":True})
        changed=await db.execute(update(WorkflowRunState).execution_options(synchronize_session=False)
            .where(WorkflowRunState.run_identity==run.run_identity,WorkflowRunState.revision==run.revision,
                WorkflowRunState.checkpoint_receipts_json==original_checkpoints)
            .values(checkpoint_receipts_json=canonical(records).decode(),revision=run.revision+1,updated_at=now()))
        if changed.rowcount!=1:raise BoardError("document_reap_binding_changed","The exact parser quiescence write raced")
        await db.refresh(run);return _serialize_job(run)


def _serialize_job(run):
    from src.workflows.job_runtime import _serialize
    return _serialize(run)


async def execute(task,attempt,inputs,*,jobs,runner,deadline,admission_only):
    projection=await jobs.get_job(job_id(task,attempt))
    if projection is None:
        async with jobs._session() as db:expiries=await execution_expiries(db,task)
    else:
        expiries=projection["declared_authority"].get("execution_expiries")
        deadline=utc(datetime.fromisoformat(projection["deadline_at"]))
    spec=spec_for(task,attempt,inputs,deadline=deadline,expiries=expiries)
    if projection is None:projection=await jobs.admit_job(spec)
    else:
        from src.workflows.job_runtime import _digest
        if (projection["input_digest"]!=_digest(spec.inputs) or projection["run_fingerprint"]!=spec.run_fingerprint
            or projection["declared_authority"]!=spec.declared_authority):
            raise BoardError("document_admission_drift","The original native admission changed")
    if admission_only or projection["status"]=="succeeded":return {**projection,"admission_only":admission_only,"job_id":spec.identity.job_id}
    # Existing child identity always requires exact actual witness before a
    # recovery can adopt output. A fresh execution never launches a second.
    prior=checkpoints(projection)
    if "document-capacity" in prior:
        raise BoardError("document_original_attempt_recovery_required","Recover the original parser witness and output")
    staged=await stage_current(jobs,task,attempt,inputs)
    require_launch_window(spec.deadline_at)
    if projection["status"]=="accepted":projection=await jobs.queue_job(spec.identity.job_id,expected_revision=projection["revision"])
    binding={"job_id":spec.identity.job_id,"input_digest":spec.inputs["typed_input_digest"],
        "input_artifact_id":task.input_artifact_id,
        "generation":prior.get("document-parser-retry",{}).get("generation",1),"nonce":uuid.uuid4().hex}
    from src.work_board.document_capacity import stage_capacity, assert_capacity
    async with jobs._session() as db:
        capacity_snapshot = await stage_capacity(db)
    async def claim(db,run):
        await current(db,task,attempt,run,staged)
        await assert_capacity(db, snapshot=capacity_snapshot, run=run)
        history=json.loads(run.checkpoint_receipts_json)
        history.append({"checkpoint_id":"document-capacity","payload":binding,"safe":True})
        run.checkpoint_receipts_json=canonical(history).decode();await db.flush()
    try:
        projection=await jobs.claim_job(spec.identity.job_id,owner=runner,lease_seconds=35,
            expected_revision=projection["revision"],expected_fencing_token=(projection.get("lease") or {}).get("fencing_token",0),claim_authority_check=claim)
    except BoardError as exc:
        if exc.code not in {"document_parser_capacity_held","document_higher_priority_ready"}:raise
        async with jobs._session() as db:
            await db.execute(text("BEGIN IMMEDIATE"));waiting=await jobs._fetch(db,spec.identity.job_id)
            await current(db,task,attempt,waiting,staged)
            if waiting.status!="queued" or waiting.lease_owner is not None:
                raise BoardError("document_queue_wait_changed","The original queued comparison changed")
            changed=await db.execute(update(WorkflowRunState).execution_options(synchronize_session=False).where(
                WorkflowRunState.run_identity==waiting.run_identity,WorkflowRunState.revision==waiting.revision,
                WorkflowRunState.status=="queued",WorkflowRunState.lease_owner.is_(None))
                .values(failure_reason=exc.code,updated_at=now(),revision=waiting.revision+1))
            if changed.rowcount!=1:raise BoardError("document_queue_wait_changed","The original queue receipt changed")
            await db.refresh(waiting);held=_serialize_job(waiting)
        return {**held,"job_id":spec.identity.job_id,"status":"queued","admission_only":False,"reason_code":exc.code}
    fence=int(projection["lease"]["fencing_token"]); process=None; parent=-1
    try:
        path=directory_path(task.input_artifact_id)/"unused"
        parent,_leaf=_open_input_artifact_parent(path,create=True)
        child_binding={key:binding[key] for key in ("job_id","input_digest","generation","nonce")}
        await stage_current(jobs,task,attempt,inputs)
        require_launch_window(spec.deadline_at)
        process=await asyncio.create_subprocess_exec(sys.executable,"-I",str(Path(__file__).with_name("document_compare_supervisor.py")),str(parent),canonical(child_binding).decode(),
            str(spec.deadline_at.timestamp()),
            pass_fds=(parent,),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,limit=512*1024+4096)
        ready=await asyncio.wait_for(process.stdout.readline(),timeout=remaining(spec.deadline_at,5,cleanup=5))
        packet=json.loads(ready)
        if (len(ready)>4096 or packet.get("state")!="ready" or packet.get("binding")!=child_binding
            or packet.get("supervisor_pid")!=process.pid or type(packet.get("parser_pid")) is not int or packet["parser_pid"]<=0):
            raise BoardError("document_resource_self_check_failed","The supported resource host self-check failed before source")
        binding.update({"supervisor_pid":process.pid,"parser_pid":packet["parser_pid"]})
        await jobs.record_checkpoint(spec.identity.job_id,checkpoint_id="document-child",state=binding,
            checkpoint_payload=binding,owner=runner,fencing_token=fence)
        await stage_current(jobs,task,attempt,inputs)
        async with jobs._session() as db: pdf,csv_source=await source_pair(db,task,inputs)
        await stage_current(jobs,task,attempt,inputs,physical=False)
        require_launch_window(spec.deadline_at)
        # Source delivery follows persisted positive identity and fresh gates.
        process.stdin.write(struct.pack("!II",len(pdf),len(csv_source))+pdf+csv_source)
        await asyncio.wait_for(process.stdin.drain(),timeout=remaining(spec.deadline_at,5,cleanup=5))
        process.stdin.close();await asyncio.wait_for(process.stdin.wait_closed(),timeout=remaining(spec.deadline_at,5,cleanup=5))
        raw=await asyncio.wait_for(process.stdout.read(512*1024+1),timeout=remaining(spec.deadline_at,30,cleanup=5))
        await asyncio.wait_for(process.wait(),timeout=remaining(spec.deadline_at,5))
        actual,witness_sha=witness(binding)
        if process.returncode!=0 or actual["parser_exit"]!=0 or len(raw)>512*1024:
            raise BoardError("document_parser_resource_exit","The bounded parser did not complete")
        await reconcile_reap(jobs, task, attempt)
        result=json.loads(raw)
        if result.get("status")!="succeeded":
            raise BoardError(str(result.get("reason") or "document_parser_blocked"),"The selected document grammar is unsupported")
        await stage_current(jobs,task,attempt,inputs)
        output=result["result"]; plaintext=canonical(output)
        if len(plaintext)>352*1024:raise BoardError("document_output_bound_exceeded","Derived output exceeds the fixed generation bound")
        reference=f"{PREFIX}/{task.input_artifact_id}/{binding['nonce']}.output.fernet"
        path=canonical_workspace_root(settings.workspace_dir)/reference
        receipt=publish_private(path,plaintext)
        if receipt["cipher_size"]>512*1024:raise BoardError("document_output_bound_exceeded","Encrypted output exceeds the generation bound")
        if read_private(path,receipt,maximum=352*1024)!=plaintext:raise BoardError("document_output_readback_failed","The derived output failed physical readback")
        await jobs.record_checkpoint(spec.identity.job_id,checkpoint_id="document-output",state=receipt,
            checkpoint_payload={**receipt,"reference":reference,"plain_sha256":sha256(plaintext),"no_learning":True},owner=runner,fencing_token=fence)
        return await adopt_output(jobs,task,attempt,inputs,runner,fence)
    except BaseException as exc:
        # Closing the actual parent pipe lets the independent supervisor reap
        # and publish its witness even after this coroutine is cancelled.
        if process is not None:
            if process.stdin is not None:process.stdin.close()
            try:await asyncio.wait_for(asyncio.shield(process.wait()),timeout=remaining(spec.deadline_at,5))
            except (asyncio.TimeoutError,asyncio.CancelledError,BoardError):pass
        latest=await jobs.get_job(spec.identity.job_id)
        if latest and cleanup_proven(task,attempt,latest):
            actual,witness_sha=witness(binding)
            try:latest=await reconcile_reap(jobs,task,attempt)
            except Exception:pass
            if (isinstance(exc,BoardError) and "document-output" not in checkpoints(latest)
                and actual["reason"]!="document_supervisor_interrupted"):
                try:
                    failed=await jobs.transition_job(spec.identity.job_id,"failed",owner=runner,
                        fencing_token=fence,reason=exc.code,result={"memory_status":"no_learning"},
                        result_summary="Selected documents visibly blocked; exact parser quiescence verified")
                    return {**failed,"job_id":spec.identity.job_id,"admission_only":False,
                        "reason_code":exc.code,"memory_status":"no_learning"}
                except Exception:pass
        # Never free host capacity or quota on an absent/unverified witness.
        raise
    finally:
        if parent>=0:os.close(parent)


def read_output(task,attempt,run):
    state=checkpoints(run); binding=state.get("document-child"); receipt=state.get("document-output")
    if not binding or not receipt or not cleanup_proven(task,attempt,run if isinstance(run,dict) else {"checkpoints":json.loads(run.checkpoint_receipts_json),"status":run.status}):
        raise BoardError("document_output_readback_required","The original output requires actual parser cleanup")
    reference=f"{PREFIX}/{task.input_artifact_id}/{binding['nonce']}.output.fernet"
    if receipt.get("reference")!=reference:raise BoardError("document_output_binding_changed","The original output binding changed")
    raw=read_private(canonical_workspace_root(settings.workspace_dir)/reference,receipt,maximum=352*1024)
    if sha256(raw)!=receipt["plain_sha256"]:raise BoardError("document_output_changed","The private derived output changed")
    value=json.loads(raw)
    if value.get("no_learning") is not True or set(value)!={"report","csv","manifest","no_learning"}:
        raise BoardError("document_output_schema_invalid","The private output schema changed")
    manifest=value["manifest"]
    from src.work_board.dispatcher import _parse_typed_input
    selected=_parse_typed_input(task)
    if (not isinstance(value["report"],str) or not isinstance(value["csv"],str)
        or len(value["report"].encode())>65536 or len(value["csv"].encode())>262144
        or not isinstance(manifest,dict) or len(canonical(manifest))>32768
        or manifest.get("schema")!="document_invoice_compare.v1"
        or manifest.get("operation")!="compare-line-totals-by-sku" or manifest.get("no_learning") is not True
        or manifest.get("pdf_sha256")!=selected["pdf"]["sha256"]
        or manifest.get("csv_sha256")!=selected["csv"]["sha256"]
        or not isinstance(manifest.get("rows"),list) or len(manifest["rows"])>1000):
        raise BoardError("document_output_schema_invalid","The bounded private output schema changed")
    return receipt,value


def read_ciphertext(path,receipt):
    parent,leaf=_open_input_artifact_parent(path,create=False);fd=-1
    try:
        fd=os.open(leaf,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0),dir_fd=parent)
        stat=os.fstat(fd)
        if not _private_input_file_metadata(stat) or stat.st_size!=receipt["cipher_size"] or stat.st_size>512*1024:
            raise ValueError("document output ciphertext changed")
        chunks=[]; remaining=stat.st_size
        while remaining:
            chunk=os.read(fd,min(65536,remaining))
            if not chunk:raise ValueError("document output ciphertext truncated")
            chunks.append(chunk); remaining-=len(chunk)
        raw=b"".join(chunks)
        if sha256(raw)!=receipt["cipher_sha256"]:raise ValueError("document output ciphertext changed")
        return raw
    finally:
        if fd>=0:os.close(fd)
        os.close(parent)


async def adopt_output(jobs,task,attempt,inputs,runner,fence):
    """Stage exact physical proof, then atomically adopt under pure SQL fences."""
    from src.artifacts.registry import build_artifact_record
    from src.workflows.job_runtime import (_digest, _serialize, _effect_ledger_or_raise,
        _job_has_unsafe_effects, _verified_readback_exists, _assert_canonical_goal_fence)
    async with jobs._session() as db:
        original=await jobs._fetch(db,job_id(task,attempt))
        staged=await stage(db,task,attempt,original,inputs)
        receipt,_output=read_output(task,attempt,original)
        checkpoint_binding=original.checkpoint_receipts_json
        binding=checkpoints(original)["document-child"]
        actual,witness_sha=witness(binding)
    if actual["parser_exit"]!=0 or actual["reason"] is not None:
        raise BoardError("document_parser_output_unverified","The original parser did not finish normally")
    ciphertext=read_ciphertext(canonical_workspace_root(settings.workspace_dir)/receipt["reference"],receipt)
    record=build_artifact_record(file_path=receipt["reference"],artifact_type="document_invoice_comparison_private",
        producer=JOB_KIND,run_id=job_id(task,attempt),session_id=task.owner_session_id,content=ciphertext)
    artifact={key:record[key] for key in ("artifact_id","artifact_type","file_path","producer","content_sha256","size_bytes","exists")}
    async with jobs._session() as db:
        await db.execute(text("BEGIN IMMEDIATE"));run=await jobs._fetch(db,job_id(task,attempt))
        await current(db,task,attempt,run,staged)
        await _assert_canonical_goal_fence(db,goal_id=run.goal_id,goal_revision=run.goal_revision,
            owner_kind=run.owner_kind,owner_principal_id=run.owner_principal_id,session_id=run.session_id,authority=run.declared_authority_json)
        stamp=now()
        if (run.status!="running" or run.lease_owner!=runner or run.fencing_token!=fence
            or not run.lease_expires_at or utc(run.lease_expires_at)<=stamp
            or run.checkpoint_receipts_json!=checkpoint_binding):
            raise BoardError("document_adoption_fence_changed","The original output adoption lease changed")
        effects=_effect_ledger_or_raise(run.effect_receipts_json)
        if _job_has_unsafe_effects(effects):raise BoardError("document_effect_unknown","The original job requires reconciliation")
        effect_id="eff_"+_digest({"job_id":run.run_identity,"receipt_kind":"readback","effect_type":"document_invoice_comparison_private",
            "target_path":receipt["reference"],"target_digest":receipt["cipher_sha256"],"adapter_idempotency_key":""})[:24]
        readback={"effect_id":effect_id,"receipt_kind":"readback","effect_type":"document_invoice_comparison_private",
            "target_path":receipt["reference"],"target_digest":receipt["cipher_sha256"],"approval_id":None,
            "adapter_idempotency_key":None,"status":"succeeded","content_sha256":receipt["cipher_sha256"],
            "readback_id":"document-readback:"+binding["nonce"],"verified_at":stamp.isoformat(),"recorded_at":stamp.isoformat(),
            "fencing_token":fence,"details":{"verified":True,"cleanup_proven":True,"no_learning":True,
                "plain_sha256":receipt["plain_sha256"],"witness_sha256":witness_sha}}
        effects=[item for item in effects if item.get("effect_id")!=effect_id]+[readback]
        if not _verified_readback_exists(effects):raise BoardError("document_output_readback_required","The exact output readback is required")
        artifacts=json.loads(run.artifact_receipts_json)
        if not isinstance(artifacts,list) or any(not isinstance(item,dict) for item in artifacts):
            raise BoardError("document_artifact_history_invalid","The original artifact history is invalid")
        artifact["recorded_at"]=stamp.isoformat();artifacts=[item for item in artifacts if item.get("artifact_id")!=artifact["artifact_id"]]+[artifact]
        changed=await db.execute(update(WorkflowRunState).execution_options(synchronize_session=False).where(WorkflowRunState.run_identity==run.run_identity,
            WorkflowRunState.revision==run.revision,WorkflowRunState.status=="running",WorkflowRunState.lease_owner==runner,
            WorkflowRunState.fencing_token==fence,WorkflowRunState.lease_expires_at>stamp,WorkflowRunState.deadline_at>stamp)
            .values(status="succeeded",artifact_receipts_json=canonical(artifacts).decode(),effect_receipts_json=canonical(effects).decode(),
                result_digest=_digest({"status":"succeeded","no_learning":True,"output_sha256":receipt["cipher_sha256"]}),
                result_summary="Selected private PDF/CSV compared with cited exact Decimal formulas; no_learning",
                finished_at=stamp,updated_at=stamp,heartbeat_at=stamp,
                failure_reason=None if run.failure_reason in {"document_parser_capacity_held","document_higher_priority_ready"} else run.failure_reason,
                lease_owner=None,lease_expires_at=None,revision=run.revision+1))
        if changed.rowcount!=1:raise BoardError("document_adoption_fence_changed","The original output changed before adoption")
        await db.flush();await db.refresh(run);finished=run
        projection=_serialize(finished)
    return {**projection,"job_id":job_id(task,attempt),"status":"succeeded","admission_only":False,"memory_status":"no_learning"}
