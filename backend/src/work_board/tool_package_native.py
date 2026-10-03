"""One fixed reviewed local package through the existing native Work lane."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from sqlalchemy import select, text, update

from config.settings import settings
from src.db.models import WorkBoardInputArtifact, OperatorSession, WorkBoardAttempt, WorkBoardStatus, WorkBoardTask, WorkflowRunState
from src.execution.tool_package_profile import (
    CAPABILITY, JOB_KIND, PACKAGE_ID, PROFILE, MAX_OUTPUT, ToolPackageBlocked,
    canonical, digest, expected_output, inspect_runtime, read_private, source_package,
)
from src.extensions.capability_pack import CapabilityPackLifecycle, capability_pack_digest
from src.work_board.contracts import WorkBoardOwner
from src.work_board.repository import WorkBoardRepository
from src.work_board.tool_package_contracts import JsonFormatInput
from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec
from src.workspace import canonical_workspace_root

PREFIX = "artifacts/work-board/tool-package/"


def now():
    return datetime.now(timezone.utc)


def runtime_root():
    return canonical_workspace_root(settings.workspace_dir) / "artifacts/tool-package-runtime" / PROFILE


def runtime_binding():
    metadata=inspect_runtime(runtime_root())
    # Canonical job receipts deliberately redact deeply nested structures and
    # secret-named fields. Bind the complete verified profile with a digest;
    # retain only shallow non-secret runtime identities beside it.
    return {**{key:value for key,value in metadata.items() if key not in {"limits","secrets"}},
        "limits_digest":digest(canonical(metadata["limits"]))}


def job_id(task, attempt):
    return "json-format:"+digest(canonical([task.task_id, attempt.attempt_id]))[:40]


def pack_binding(task, *, lifecycle=None, state=None):
    lifecycle = lifecycle or CapabilityPackLifecycle()
    if state is None:
        with lifecycle._state_lock(shared=True):
            return pack_binding(task,lifecycle=lifecycle,state=bounded_pack_state(lifecycle))
    if state is not None:
        pointer = state["active"].get(PACKAGE_ID)
        if (not isinstance(pointer, dict) or pointer.get("status") != "active"
            or pointer.get("version") != "1.0.0" or pointer.get("goal_id") != task.goal_id
            or pointer.get("owner_principal_id") != task.owner_principal_id
            or pointer.get("session_id") != task.owner_session_id
            or pointer.get("digest") != capability_pack_digest(source_package().parent)
            or not lifecycle._pointer_binding_valid(state, PACKAGE_ID, pointer)):
            raise ToolPackageBlocked("tool_package_exact_review_required")
        return {key: pointer[key] for key in ("pack_id", "version", "digest", "goal_id",
            "review_id", "authority_digest", "dependencies_digest", "owner_principal_id", "session_id")}


def immutable_inputs(task, inputs):
    model = JsonFormatInput.model_validate(dict(inputs))
    if not task.input_artifact_id or not task.typed_input_digest:
        raise ToolPackageBlocked("tool_package_input_artifact_required")
    return {"input_artifact_id":task.input_artifact_id, "typed_input_digest":task.typed_input_digest,
        "json_sha256":digest(model.json_text.encode()), "no_learning":True}


def authority_for(task, attempt, inputs):
    from src.work_board.pipelines import root_binding
    metadata = runtime_binding()
    return {"principal":task.owner_principal_id, "owner_kind":"user", "session_id":task.owner_session_id,
        "goal_id":task.goal_id, "goal_revision":task.goal_revision, "capability_id":CAPABILITY,
        "capability_version":"1", "board_task_id":task.task_id,"board_attempt_id":attempt.attempt_id,
        "input_artifact_id":task.input_artifact_id, "typed_input_digest":task.typed_input_digest,
        "live_root_digest":digest(canonical(root_binding())), "pack":pack_binding(task),
        "runtime":metadata, "permissions":["workspace_read","workspace_write"],"no_learning":True}


def spec_for(task, attempt, inputs, *, deadline):
    first=attempt.started_at.replace(tzinfo=timezone.utc) if attempt.started_at.tzinfo is None else attempt.started_at
    deadline=min(deadline,first+timedelta(seconds=10))
    safe_inputs = immutable_inputs(task, inputs)
    authority = authority_for(task, attempt, inputs)
    return DurableJobSpec(identity=DurableJobIdentity(job_id=job_id(task,attempt), owner_kind="user",
        owner_principal_id=task.owner_principal_id, job_kind=JOB_KIND, capability_version="1",
        idempotency_scope="work-board-attempt", idempotency_key=f"{task.task_id}:{attempt.attempt_id}"),
        inputs=safe_inputs, session_id=task.owner_session_id, operator_session_id=task.owner_session_id,
        goal_id=task.goal_id, goal_revision=task.goal_revision, priority=task.priority,
        declared_authority=authority, deadline_at=deadline, max_attempts=1, max_outstanding_jobs=1,
        budget_microusd=0, run_fingerprint=digest(canonical({"task":task.task_id,"attempt":attempt.attempt_id,
            "inputs":safe_inputs,"authority":authority})))


_STAGE_SEAL = object()

@dataclass(frozen=True)
class AuthorityStage:
    seal: object
    job: str
    authority_json: str
    input_binding: tuple
    pack_state_digest: str


def bounded_pack_state(lifecycle):
    # The existing lifecycle lock pins this file. No file operation is made
    # from the SQLite writer, and the watcher never hashes the runtime closure.
    if lifecycle.state_path.exists() and lifecycle.state_path.stat().st_size>16_777_216:
        raise ToolPackageBlocked("tool_package_lifecycle_state_exceeds_bound")
    return lifecycle._load()


def pack_state_digest(state, authority):
    pointer=state["active"].get(PACKAGE_ID)
    package=authority["pack"]
    return digest(canonical([pointer,state.get("versions",{}).get(PACKAGE_ID,{}).get(package["digest"]),
        state.get("reviews",{}).get(package["review_id"]),state.get("revoked",{}).get(PACKAGE_ID)]))


def input_row_binding(row):
    return tuple(getattr(row,key) for key in ("artifact_id","owner_principal_id","owner_session_id",
        "goal_id","goal_revision","capability_id","capability_version","payload_sha256","metadata_digest",
        "size_bytes","state","bound_task_id","bound_task_revision","revision","expires_at"))


async def stage_authority(db,task,attempt,run,*,lifecycle,state,full=True):
    from src.work_board.input_artifacts import resolve_input_artifact_for_task
    from src.work_board.pipelines import root_binding
    owner=WorkBoardOwner(principal_id=task.owner_principal_id,session_id=task.owner_session_id)
    authority=json.loads(run.declared_authority_json)
    if authority["live_root_digest"]!=digest(canonical(root_binding())):
        raise ToolPackageBlocked("tool_package_original_root_changed")
    if full:
        resolved=await resolve_input_artifact_for_task(db,owner,artifact_id=task.input_artifact_id,
            capability_id=CAPABILITY,goal_id=task.goal_id,goal_revision=task.goal_revision,expected_task_id=task.task_id)
        row=resolved.row
        if row.payload_sha256!=task.typed_input_digest:
            raise ToolPackageBlocked("tool_package_input_changed")
        if authority["pack"]!=pack_binding(task,lifecycle=lifecycle,state=state) or authority["runtime"]!=runtime_binding():
            raise ToolPackageBlocked("tool_package_review_or_profile_changed")
    else:
        row=await db.get(WorkBoardInputArtifact,task.input_artifact_id,populate_existing=True)
        if row is None:raise ToolPackageBlocked("tool_package_input_changed")
    return AuthorityStage(_STAGE_SEAL,run.run_identity,run.declared_authority_json,input_row_binding(row),pack_state_digest(state,authority))


@asynccontextmanager
async def authority_guard(jobs,task,attempt,*,full=True):
    # One lock order: lifecycle -> staging/read session -> short SQL writer.
    # Every loop-thread lifecycle lock is nonblocking, so revoke returns busy
    # instead of blocking the event loop while this guard awaits SQLite.
    lifecycle=CapabilityPackLifecycle()
    with lifecycle._state_lock(shared=True):
        state=bounded_pack_state(lifecycle)
        async with jobs._session() as db:
            run=await jobs._fetch(db,job_id(task,attempt))
            remaining=(run.deadline_at.replace(tzinfo=timezone.utc) if run.deadline_at.tzinfo is None else run.deadline_at)-now()
            if remaining.total_seconds()<=0:raise ToolPackageBlocked("tool_package_original_deadline_expired")
            async with asyncio.timeout(remaining.total_seconds()):
                staged=await stage_authority(db,task,attempt,run,lifecycle=lifecycle,state=state,full=full)
        absolute=run.deadline_at.replace(tzinfo=timezone.utc) if run.deadline_at.tzinfo is None else run.deadline_at
        async with asyncio.timeout(max(0,(absolute-now()).total_seconds())):
            yield staged


async def current(db, task, attempt, run, *, require_lease=True, staged=None):
    """Pure canonical SQL check; physical authority must be staged beforehand."""
    stamp=now()
    owner=WorkBoardOwner(principal_id=task.owner_principal_id,session_id=task.owner_session_id)
    goal=await WorkBoardRepository._validate_goal(db,owner,goal_id=task.goal_id,goal_revision=task.goal_revision)
    from src.goals.repository import deserialize_admission_budget
    budget=deserialize_admission_budget(goal)
    for expiry in (goal.due_date, budget.period_expires_at if budget else None):
        if expiry is not None and (expiry.replace(tzinfo=timezone.utc) if expiry.tzinfo is None else expiry)<=stamp:
            raise ToolPackageBlocked("tool_package_goal_window_expired")
    session=await db.scalar(select(OperatorSession.id).where(
        OperatorSession.id==task.owner_session_id,OperatorSession.principal_id==task.owner_principal_id,
        OperatorSession.revoked_at.is_(None),OperatorSession.replaced_by_id.is_(None),
        OperatorSession.is_bearer_tombstone.is_(False),OperatorSession.idle_expires_at>stamp,
        OperatorSession.absolute_expires_at>stamp))
    authority=json.loads(run.declared_authority_json)
    if session is None:
        raise ToolPackageBlocked("tool_package_original_root_inactive")
    current_task=await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==task.task_id)
        .execution_options(populate_existing=True))
    current_attempt=await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id==attempt.attempt_id)
        .execution_options(populate_existing=True))
    deadline=run.deadline_at.replace(tzinfo=timezone.utc) if run.deadline_at.tzinfo is None else run.deadline_at
    if (current_task is None or current_attempt is None or deadline<=stamp
        or current_task.owner_principal_id!=task.owner_principal_id or current_task.owner_session_id!=task.owner_session_id
        or current_task.capability_id!=CAPABILITY or current_task.goal_id!=task.goal_id
        or current_task.goal_revision!=task.goal_revision or current_task.task_revision!=task.task_revision
        or current_task.status!=WorkBoardStatus.running or current_attempt.task_id!=task.task_id
        or current_attempt.workflow_run_id!=run.run_identity or current_attempt.ended_at is not None
        or current_attempt.cancel_requested_at is not None or current_attempt.fencing_token!=attempt.fencing_token
        or (require_lease and (current_attempt.lease_owner!=attempt.lease_owner or not current_attempt.lease_expires_at
            or (current_attempt.lease_expires_at.replace(tzinfo=timezone.utc) if current_attempt.lease_expires_at.tzinfo is None
                else current_attempt.lease_expires_at)<=stamp))):
        raise ToolPackageBlocked("tool_package_current_attempt_changed")
    if staged is None or staged.seal is not _STAGE_SEAL or staged.job!=run.run_identity or staged.authority_json!=run.declared_authority_json:
        raise ToolPackageBlocked("tool_package_staged_authority_required")
    row=await db.get(WorkBoardInputArtifact,task.input_artifact_id,populate_existing=True)
    if row is None or input_row_binding(row)!=staged.input_binding or row.payload_sha256!=task.typed_input_digest:
        raise ToolPackageBlocked("tool_package_input_changed")
    expiry=row.expires_at.replace(tzinfo=timezone.utc) if row.expires_at.tzinfo is None else row.expires_at
    if row.state not in {"bound","consumed"} or expiry<=stamp:
        raise ToolPackageBlocked("tool_package_input_unavailable")


def read_output(reference, sha):
    if not reference.startswith(PREFIX) or "/" in reference[len(PREFIX):] or ".." in reference:
        raise ToolPackageBlocked("tool_package_output_reference_invalid")
    path=canonical_workspace_root(settings.workspace_dir)/reference
    raw=read_private(path.parent,path.name,MAX_OUTPUT)
    if digest(raw)!=sha:
        raise ToolPackageBlocked("tool_package_output_changed")
    return raw


async def record_cleanup(jobs, *, task, attempt, runner, request, stage, reservation):
    """Only the exact actual supervisor's private ECHILD readback resolves its hold.

    This closes process liability even after Goal/Root revocation. It grants no
    artifact adoption or renewed work and never resolves any other effect.
    """
    from src.execution.repo_supervisor import start_identity
    raw=read_private(stage,"out/supervisor-result.json",32768)
    proof=json.loads(raw)
    if (proof.get("cleanup_proven") is not True or proof.get("process_cleanup",{}).get("oracle")!="linux_subreaper_waitpid_echild"
        or any(proof.get(key)!=request[key] for key in ("job_id","fence","token","runtime_digest","package_sha256","input_sha256"))
        or start_identity(proof.get("supervisor_pid"))==proof.get("supervisor_start")):
        raise ToolPackageBlocked("tool_package_cleanup_unproven")
    async with jobs._session() as db:
        if db.get_bind().dialect.name=="sqlite":await db.execute(text("BEGIN IMMEDIATE"))
        run=await jobs._fetch(db,request["job_id"])
        records=json.loads(run.checkpoint_receipts_json)
        original=next((item["payload"] for item in records if item.get("checkpoint_id")=="tool-package:reservation"),None)
        started=next((item["payload"] for item in records if item.get("checkpoint_id")=="tool-package:process"),None)
        authority=json.loads(run.declared_authority_json)
        if (original!=reservation or run.job_kind!=JOB_KIND or run.owner_principal_id!=task.owner_principal_id
            or run.session_id!=task.owner_session_id or authority["board_task_id"]!=task.task_id
            or authority["board_attempt_id"]!=attempt.attempt_id or run.fencing_token!=request["fence"]
            or run.lease_owner!=runner or not isinstance(started,dict)
            or started.get("supervisor_pid")!=proof.get("supervisor_pid") or started.get("supervisor_start")!=proof.get("supervisor_start")):
            raise ToolPackageBlocked("tool_package_cleanup_binding_changed")
        effect_id="tool-package-process-"+digest(canonical([request["job_id"],request["fence"]]))[:32]
        from src.workflows.job_runtime import _effect_ledger_or_raise
        effects=_effect_ledger_or_raise(run.effect_receipts_json)
        matched=[effect for effect in effects if effect.get("effect_id")==effect_id]
        if len(matched)!=1 or matched[0].get("target_digest")!=digest(canonical(reservation)):
            raise ToolPackageBlocked("tool_package_cleanup_effect_changed")
        matched[0].update(status="succeeded",verified_at=now().isoformat(),details={"cleanup_proven":True,"no_learning":True})
        records=[item for item in records if item.get("checkpoint_id")!="tool-package:cleanup"]
        records.append({"checkpoint_id":"tool-package:cleanup","payload":{**reservation,"cleanup_proven":True,
            "supervisor_pid":proof["supervisor_pid"],"supervisor_start":proof["supervisor_start"],
            "result_sha256":digest(raw),"process_cleanup":proof["process_cleanup"]}})
        changed=await db.execute(update(WorkflowRunState).where(WorkflowRunState.run_identity==run.run_identity,
            WorkflowRunState.revision==run.revision,WorkflowRunState.fencing_token==request["fence"],WorkflowRunState.lease_owner==runner)
            .values(checkpoint_receipts_json=canonical(records).decode(),effect_receipts_json=canonical(effects).decode(),revision=run.revision+1))
        if changed.rowcount!=1:raise ToolPackageBlocked("tool_package_cleanup_cas_changed")


def binds(task, attempt, run):
    """Completed history binds to admitted immutable identity, not an active pointer."""
    try:
        from src.work_board.dispatcher import _parse_typed_input
        inputs=immutable_inputs(task,_parse_typed_input(task))
        authority=json.loads(run.declared_authority_json)
        expected_fingerprint=digest(canonical({"task":task.task_id,"attempt":attempt.attempt_id,
            "inputs":inputs,"authority":authority}))
        return (run.run_identity==job_id(task,attempt) and run.job_kind==JOB_KIND and run.capability_version=="1"
            and run.owner_kind=="user" and run.owner_principal_id==task.owner_principal_id
            and run.session_id==task.owner_session_id and run.operator_session_id==task.owner_session_id
            and run.input_digest==digest(canonical(inputs)) and run.run_fingerprint==expected_fingerprint
            and run.authority_digest==digest(canonical(authority)) and authority["board_task_id"]==task.task_id
            and authority["board_attempt_id"]==attempt.attempt_id and authority["capability_id"]==CAPABILITY
            and authority["pack"]["pack_id"]==PACKAGE_ID and authority["pack"]["version"]=="1.0.0"
            and len(authority["pack"]["digest"])==64
            and authority["runtime"]["profile"]==PROFILE and authority["no_learning"] is True)
    except (OSError,KeyError,TypeError,ValueError):
        return False


@dataclass(frozen=True)
class ReadbackStage:
    seal: object
    binding: tuple
    artifact: dict
    raw: bytes


def readback_binding(task,attempt,run):
    return tuple(tuple(str(getattr(row,column.name)) for column in row.__table__.columns) for row in (task,attempt,run))


def stage_readback(task,attempt,run):
    artifact,raw=verified_output(task,attempt,run)
    return ReadbackStage(_STAGE_SEAL,readback_binding(task,attempt,run),dict(artifact),raw)


@asynccontextmanager
async def session_authority_guard(db,task,attempt,run):
    lifecycle=CapabilityPackLifecycle()
    deadline=run.deadline_at.replace(tzinfo=timezone.utc) if run.deadline_at.tzinfo is None else run.deadline_at
    with lifecycle._state_lock(shared=True):
        async with asyncio.timeout(max(0,(deadline-now()).total_seconds())):
            staged=await stage_authority(db,task,attempt,run,lifecycle=lifecycle,state=bounded_pack_state(lifecycle))
            yield staged


def verified_output(task, attempt, run, *, staged=None):
    if staged is not None:
        if staged.seal is not _STAGE_SEAL or staged.binding!=readback_binding(task,attempt,run):
            raise ToolPackageBlocked("tool_package_staged_readback_changed")
        return dict(staged.artifact),staged.raw
    if json.loads(run.declared_authority_json)["pack"]["digest"]!=capability_pack_digest(source_package().parent):
        raise ToolPackageBlocked("tool_package_package_changed")
    if run.status!="succeeded" or not binds(task,attempt,run):
        raise ToolPackageBlocked("tool_package_native_readback_required")
    from src.work_board.dispatcher import _parse_typed_input
    expected=expected_output(JsonFormatInput.model_validate(_parse_typed_input(task)).json_text.encode())
    sha=digest(expected)
    records=json.loads(run.checkpoint_receipts_json)
    reservation=[item["payload"] for item in records if item.get("checkpoint_id")=="tool-package:reservation"]
    cleanup=[item["payload"] for item in records if item.get("checkpoint_id")=="tool-package:cleanup"]
    artifacts=[item for item in json.loads(run.artifact_receipts_json) if item.get("artifact_type")=="tool_package_json"
        and item.get("content_sha256")==sha and item.get("exists") is True]
    effects=json.loads(run.effect_receipts_json)
    if (len(reservation)!=1 or len(cleanup)!=1 or len(artifacts)!=1
        or cleanup[0].get("cleanup_proven") is not True or cleanup[0].get("job_id")!=run.run_identity
        or cleanup[0].get("fence")!=reservation[0].get("fence")
        or reservation[0].get("content_sha256")!=sha or reservation[0].get("file_path")!=artifacts[0].get("file_path")
        or not any(effect.get("receipt_kind")=="readback" and effect.get("status")=="succeeded"
            and effect.get("target_path")==artifacts[0]["file_path"] and effect.get("content_sha256")==sha
            and effect.get("readback_id") and effect.get("verified_at") for effect in effects)
        or read_output(artifacts[0]["file_path"],sha)!=expected):
        raise ToolPackageBlocked("tool_package_exact_artifact_cleanup_required")
    return artifacts[0],expected


def cleanup_proven(task, attempt, projection):
    """Canonical cleanup requires the original reservation plus actual proof.

    No local registry absence, expired lease, or terminal label proves reap.
    Accepted/queued admission with no reserved process is proven prelaunch.
    """
    if (projection.get("job_id")!=job_id(task,attempt) or projection.get("job_kind")!=JOB_KIND
        or projection.get("owner",{}).get("principal_id")!=task.owner_principal_id
        or projection.get("session_id")!=task.owner_session_id):return False
    records=projection.get("checkpoints",[])
    reservation=[item.get("payload") for item in records if item.get("checkpoint_id")=="tool-package:reservation"]
    cleanup=[item.get("payload") for item in records if item.get("checkpoint_id")=="tool-package:cleanup"]
    effects=projection.get("effects",[])
    if not reservation and not effects and projection.get("status") in {"accepted","queued"}:return True
    if len(reservation)!=1 or len(cleanup)!=1 or not isinstance(reservation[0],dict) or not isinstance(cleanup[0],dict):return False
    proof=cleanup[0];reserved=reservation[0]
    return (all(proof.get(key)==value for key,value in reserved.items())
        and proof.get("cleanup_proven") is True and proof.get("process_cleanup",{}).get("oracle")=="linux_subreaper_waitpid_echild"
        and bool(proof.get("result_sha256")) and proof.get("supervisor_pid") and proof.get("supervisor_start")
        and any(effect.get("effect_type")=="tool_package_process" and effect.get("target_digest")==digest(canonical(reserved))
            and effect.get("status")=="succeeded" and effect.get("verified_at") for effect in effects))


async def execute(task, attempt, inputs, *, jobs, runner, deadline, admission_only):
    from src.execution.tool_package_runner import prepare,execute as isolated_execute
    from src.work_board.input_artifacts import _write_payload
    model=JsonFormatInput.model_validate(dict(inputs))
    spec=spec_for(task,attempt,inputs,deadline=deadline)
    projection=await jobs.get_job(spec.identity.job_id)
    if projection is None:
        projection=await jobs.admit_job(spec)
        CapabilityPackLifecycle().register_job(PACKAGE_ID,goal_id=task.goal_id,job_id=spec.identity.job_id,
            owner_principal_id=task.owner_principal_id,session_id=task.owner_session_id,
            request_contract={"native_job_id":spec.identity.job_id,"input_digest":digest(canonical(spec.inputs)),
                "authority_digest":digest(canonical(spec.declared_authority)),"deadline_at":projection["deadline_at"]},
            required_tools=["json_format"],required_filesystem=["workspace_read","workspace_write"])
    elif (projection["input_digest"]!=digest(canonical(spec.inputs)) or projection["run_fingerprint"]!=spec.run_fingerprint
        or projection["declared_authority"]!=spec.declared_authority):
        raise ToolPackageBlocked("tool_package_admission_drift")
    if admission_only or projection["status"]=="succeeded":
        return {**projection,"admission_only":admission_only,"artifact_refs":projection.get("artifacts",[])}
    if projection["status"]=="accepted":
        projection=await jobs.queue_job(spec.identity.job_id,expected_revision=projection["revision"],reason="tool_package_board_linked")
    if projection["status"]!="queued":
        return {**projection,"admission_only":False,"reason_code":"tool_package_explicit_recovery_required"}
    projection=await jobs.claim_job(spec.identity.job_id,owner=runner,lease_seconds=10,
        expected_state="queued",expected_revision=projection["revision"],expected_fencing_token=projection["lease"]["fencing_token"])
    fence=projection["lease"]["fencing_token"]
    original_deadline=datetime.fromisoformat(projection["deadline_at"])
    if original_deadline.tzinfo is None:original_deadline=original_deadline.replace(tzinfo=timezone.utc)
    expected=expected_output(model.json_text.encode());sha=digest(expected)
    reference=PREFIX+digest(canonical([task.task_id,attempt.attempt_id]))+"-"+sha+".json"
    stage=canonical_workspace_root(settings.workspace_dir)/"artifacts/tool-package-runs"/(digest(canonical(spec.identity.job_id))+f"-{fence}")
    stage.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    request,metadata=prepare(stage,runtime_root(),raw=model.json_text.encode(),job_id=spec.identity.job_id,
        fence=fence,deadline=original_deadline)
    reservation={"schema":1,"job_id":spec.identity.job_id,"fence":fence,"stage_ref":str(stage.relative_to(canonical_workspace_root(settings.workspace_dir))),
        "file_path":reference,"content_sha256":sha,"byte_count":len(expected),"dispatch_binding_sha256":digest(request["token"].encode()),
        "runtime_digest":metadata["runtime_digest"],"package_sha256":metadata["package_sha256"],"no_learning":True}
    await jobs.record_checkpoint(spec.identity.job_id,checkpoint_id="tool-package:reservation",state=reservation,
        checkpoint_payload=reservation,owner=runner,fencing_token=fence)
    await jobs.record_effect(spec.identity.job_id,effect_type="tool_package_process",
        effect_id="tool-package-process-"+digest(canonical([request["job_id"],request["fence"]]))[:32],
        target_path=reservation["stage_ref"],target_digest=digest(canonical(reservation)),status="intent",
        details={"no_learning":True,"cleanup_required":True},owner=runner,fencing_token=fence)
    dispatch_committed=asyncio.Event()
    async def commit_process(identity,staged):
        from src.work_board.repository import BoardError
        denial=None
        async with jobs._session() as db:
            if db.get_bind().dialect.name=="sqlite":await db.execute(text("BEGIN IMMEDIATE"))
            run=await jobs._fetch(db,spec.identity.job_id)
            if run.status!="running" or run.lease_owner!=runner or run.fencing_token!=fence:
                raise ToolPackageBlocked("tool_package_native_lease_changed")
            records=json.loads(run.checkpoint_receipts_json)
            if any(item.get("checkpoint_id")=="tool-package:process" for item in records):
                raise ToolPackageBlocked("tool_package_process_already_started")
            try:
                if staged is None:raise ToolPackageBlocked("tool_package_external_staging_denied")
                await current(db,task,attempt,run,staged=staged)
            except (ToolPackageBlocked,BoardError,OSError) as exc:
                # Preserve this already launched trusted helper's identity
                # even when current authority denies package admission.
                # Commit that ownership before raising outside the session.
                denial=exc
            records.append({"checkpoint_id":"tool-package:process","payload":{**reservation,
                "supervisor_pid":identity["supervisor_pid"],"supervisor_start":identity["supervisor_start"],
                "admission_status":"denied" if denial else "admitted"}})
            changed=await db.execute(update(WorkflowRunState).where(WorkflowRunState.run_identity==run.run_identity,
                WorkflowRunState.revision==run.revision,WorkflowRunState.status=="running",
                WorkflowRunState.lease_owner==runner,WorkflowRunState.fencing_token==fence)
                .values(checkpoint_receipts_json=canonical(records).decode(),revision=run.revision+1))
            if changed.rowcount!=1:raise ToolPackageBlocked("tool_package_native_process_cas_changed")
        return denial

    async def before_dispatch(identity):
        denial=None
        try:
            async with authority_guard(jobs,task,attempt) as staged:
                denial=await commit_process(identity,staged)
        except (ToolPackageBlocked,OSError,ValueError) as exc:
            # This helper already exists. Even a staging denial must durably
            # bind its actual identity, without granting package dispatch.
            denial=await commit_process(identity,None) or exc
        if denial is not None:
            raise ToolPackageBlocked("tool_package_current_authority_denied") from denial
        CapabilityPackLifecycle()._set_local_job_status(spec.identity.job_id,status="running",expected_statuses={"accepted","queued"},
            pack_id=PACKAGE_ID,owner_principal_id=task.owner_principal_id,session_id=task.owner_session_id,
            expected_digest=spec.declared_authority["pack"]["digest"])
        dispatch_committed.set()
    with CapabilityPackLifecycle()._state_lock(shared=True):
        admitted_pack_state=pack_state_digest(bounded_pack_state(CapabilityPackLifecycle()),spec.declared_authority)
    worker=asyncio.create_task(isolated_execute(stage,request,before_dispatch=before_dispatch))
    try:
        while not worker.done():
            done,_=await asyncio.wait({worker},timeout=.2)
            if done:break
            if not dispatch_committed.is_set():continue
            try:
                async with authority_guard(jobs,task,attempt,full=False) as watched:
                    if watched.pack_state_digest!=admitted_pack_state:
                        raise ToolPackageBlocked("tool_package_review_changed")
                    async with jobs._session() as db:
                        await current(db,task,attempt,await jobs._fetch(db,spec.identity.job_id),staged=watched)
            except Exception:
                # Current Root/Goal/package revocation stops the actual owning
                # controller. It must finish its supervisor readback before
                # any native cancellation can claim quiescence.
                worker.cancel()
                break
        execution=await asyncio.shield(worker)
    except asyncio.CancelledError:
        worker.cancel()
        execution=await asyncio.shield(worker)
    cleanup=execution.get("cleanup_proven") is True
    if cleanup:
        await record_cleanup(jobs,task=task,attempt=attempt,runner=runner,request=request,stage=stage,reservation=reservation)
    if execution["status"]!="succeeded":
        status="cancelled" if cleanup and execution["status"]=="cancelled" else "failed" if cleanup else "unknown_external_effect"
        finished=await jobs.transition_job(spec.identity.job_id,status,owner=runner,fencing_token=fence,
            reason="tool_package_execution_failed" if cleanup else "tool_package_cleanup_unproven")
        return {**finished,"admission_only":False,"reason_code":"tool_package_execution_failed" if cleanup else "tool_package_cleanup_required"}
    actual=read_private(stage,"out/result.json",MAX_OUTPUT)
    if actual!=expected:raise ToolPackageBlocked("tool_package_output_readback_failed")
    return await adopt_output(jobs,task,attempt,runner,reference,expected,fence)


async def adopt_output(jobs,task,attempt,runner,reference,expected,fence):
    """Stage produced bytes, then atomically authorize their positive adoption.

    Produced files are private audit evidence until the same pure writer has
    authorized the original attempt and committed artifact/readback/success.
    """
    from src.work_board.input_artifacts import _write_payload
    from src.artifacts.registry import build_artifact_record
    from src.workflows.job_runtime import (
        _append_parent_fence_condition, _assert_canonical_goal_fence, _digest,
        _effect_ledger_or_raise, _job_has_unsafe_effects, _serialize,
        _verified_readback_exists,
    )
    identity=job_id(task,attempt);sha=digest(expected)
    async with authority_guard(jobs,task,attempt) as staged:
        # All filesystem/runtime/lifecycle proof is outside BEGIN IMMEDIATE.
        _write_payload(canonical_workspace_root(settings.workspace_dir)/reference,expected)
        if read_output(reference,sha)!=expected:
            raise ToolPackageBlocked("tool_package_output_changed")
        record=build_artifact_record(file_path=reference,artifact_type="tool_package_json",
            producer=JOB_KIND,run_id=identity,session_id=task.owner_session_id,content=expected)
        artifact={key:record[key] for key in ("artifact_id","artifact_type","file_path","producer",
            "content_sha256","size_bytes","exists")}
        async with jobs._session() as db:
            if db.get_bind().dialect.name=="sqlite":await db.execute(text("BEGIN IMMEDIATE"))
            run=await jobs._fetch(db,identity)
            await current(db,task,attempt,run,staged=staged)
            await _assert_canonical_goal_fence(db,goal_id=run.goal_id,goal_revision=run.goal_revision,
                owner_kind=run.owner_kind,owner_principal_id=run.owner_principal_id,
                session_id=run.session_id,authority=run.declared_authority_json)
            jobs._assert_lease(run,owner=runner,fencing_token=fence)
            if run.status!="running" or not binds(task,attempt,run):
                raise ToolPackageBlocked("tool_package_adoption_binding_changed")
            projection=_serialize(run)
            reservations=[item.get("payload") for item in projection["checkpoints"]
                if item.get("checkpoint_id")=="tool-package:reservation"]
            if (not cleanup_proven(task,attempt,projection) or len(reservations)!=1
                or reservations[0].get("file_path")!=reference
                or reservations[0].get("content_sha256")!=sha or reservations[0].get("fence")!=fence):
                raise ToolPackageBlocked("tool_package_exact_artifact_cleanup_required")
            effects=_effect_ledger_or_raise(run.effect_receipts_json)
            if _job_has_unsafe_effects(effects):
                raise ToolPackageBlocked("tool_package_unresolved_effect")
            stamp=now();artifact["recorded_at"]=stamp.isoformat()
            readback={"effect_id":"eff_"+_digest({"job_id":identity,"receipt_kind":"readback",
                "effect_type":"tool_package_output","target_path":reference,"target_digest":sha,
                "adapter_idempotency_key":""})[:24],"receipt_kind":"readback",
                "effect_type":"tool_package_output","target_path":reference,"target_digest":sha,
                "approval_id":None,"adapter_idempotency_key":None,"status":"succeeded",
                "content_sha256":sha,"readback_id":"tool-package-readback-"+sha[:32],
                "verified_at":stamp.isoformat(),"recorded_at":stamp.isoformat(),"fencing_token":fence,
                "details":{"verified":True,"cleanup_proven":True,"no_learning":True}}
            if any(item.get("effect_id")==readback["effect_id"] for item in effects):
                raise ToolPackageBlocked("tool_package_output_receipt_already_present")
            effects.append(readback)
            if not _verified_readback_exists(effects):
                raise ToolPackageBlocked("tool_package_output_readback_required")
            artifacts=json.loads(run.artifact_receipts_json)
            if not isinstance(artifacts,list) or any(not isinstance(item,dict) for item in artifacts):
                raise ToolPackageBlocked("tool_package_artifact_history_invalid")
            artifacts=[item for item in artifacts if item.get("artifact_id")!=artifact["artifact_id"]]+[artifact]
            conditions=[WorkflowRunState.run_identity==identity,WorkflowRunState.status=="running",
                WorkflowRunState.revision==run.revision,WorkflowRunState.lease_owner==runner,
                WorkflowRunState.lease_expires_at>stamp,WorkflowRunState.fencing_token==fence,
                WorkflowRunState.deadline_at>stamp]
            _append_parent_fence_condition(conditions,run,now=stamp)
            changed=await db.execute(update(WorkflowRunState).execution_options(synchronize_session=False)
                .where(*conditions).values(status="succeeded",artifact_receipts_json=canonical(artifacts).decode(),
                    effect_receipts_json=canonical(effects).decode(),updated_at=stamp,heartbeat_at=stamp,
                    revision=run.revision+1,failure_reason=None,finished_at=stamp,lease_owner=None,
                    lease_expires_at=None,result_digest=_digest({"status":"succeeded","no_learning":True,"output_sha256":sha}),
                    result_summary="Isolated fixed JSON formatter; exact physical output and process cleanup verified; no_learning"))
            if changed.rowcount!=1:raise ToolPackageBlocked("tool_package_adoption_cas_changed")
            refreshed=await jobs._fetch(db,identity)
            db.expunge(refreshed)
            finished=_serialize(refreshed,receipt={"kind":"transition","status":"recorded",
                "from":"running","to":"succeeded","fencing_token":fence,
                "revision":refreshed.revision,"operator_visible":True})
    CapabilityPackLifecycle()._set_local_job_status(identity,status="succeeded",expected_statuses={"running"},
        pack_id=PACKAGE_ID,owner_principal_id=task.owner_principal_id,session_id=task.owner_session_id,
        expected_digest=pack_binding(task)["digest"],details={"no_learning":True,"cleanup_proven":True,"output_sha256":sha})
    return {**finished,"admission_only":False,"artifact_refs":finished.get("artifacts",[]),"memory_status":"no_learning"}
