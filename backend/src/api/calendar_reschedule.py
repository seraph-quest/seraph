"""Production-authenticated exact Calendar operator controls."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
import json
import secrets
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy import select, func

from src.db.models import GoogleServiceConnection, CalendarRescheduleConsent
from src.integrations import calendar_reschedule_runtime as runtime
from src.integrations.calendar_reschedule_contract import SCOPES, READ_SERVICE, SEND_SERVICE, text, proposed_times
from src.integrations.calendar_reschedule import current_root, consent, consent_snapshot
from src.vault import vault_repository

router=APIRouter(prefix="/capabilities/calendar/reschedule")
_IMPORT_LOCK=asyncio.Lock()
UUID_PATTERN=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


class Strict(BaseModel):
    model_config=ConfigDict(extra="forbid",strict=True)


class ProfileCreate(Strict):
    service: Literal["calendar_reschedule_read","calendar_reschedule_write"]
    label: str=Field(min_length=1,max_length=200)
    client_id: str=Field(min_length=1,max_length=4096)
    client_secret: str|None=Field(default=None,max_length=4096)
    refresh_token: str=Field(min_length=1,max_length=8192)
    declared_scopes: list[str]=Field(min_length=4,max_length=4)
    acknowledge_separate_identity_profile: StrictBool
    idempotency_key: str=Field(pattern=UUID_PATTERN)


class Control(Strict):
    expected_revision: int=Field(ge=1)
    idempotency_key: str=Field(pattern=UUID_PATTERN)


class ProfileRevokeControl(Control):
    revoke_request_digest: str|None=Field(default=None,pattern=r"^[a-f0-9]{64}$")


class Pair(Strict):
    read_connection_id: str=Field(min_length=1,max_length=256)
    expected_read_revision: int=Field(ge=1)
    write_connection_id: str=Field(min_length=1,max_length=256)
    expected_write_revision: int=Field(ge=1)
    event_binding_id: str=Field(min_length=1,max_length=256)
    expected_event_binding_revision: int=Field(ge=1)
    goal_id: str=Field(min_length=1,max_length=256)
    goal_revision: int=Field(ge=1)
    acknowledge_identity_and_selected_calendar_read: StrictBool
    request_uuid: str=Field(pattern=UUID_PATTERN)


class ConsentCreate(Pair):
    expires_at: datetime
    acknowledge_owned_event_read: StrictBool
    acknowledge_calendar_list_metadata_read: StrictBool
    acknowledge_one_conditional_reschedule: StrictBool

    @field_validator("expires_at")
    @classmethod
    def aware(cls,value):
        if value.tzinfo is None or value.utcoffset() is None: raise ValueError("explicit expiry offset required")
        return value.astimezone(timezone.utc)


class TaskCreate(Strict):
    title: str=Field(default="Reschedule one owned calendar event",min_length=1,max_length=200)
    request_uuid: str=Field(pattern=UUID_PATTERN)
    input: dict


class Preview(Strict):
    task_id: str=Field(min_length=1,max_length=256)
    expected_task_revision: int=Field(ge=1)
    read_connection_id: str=Field(min_length=1,max_length=256)
    expected_read_revision: int=Field(ge=1)
    write_connection_id: str=Field(min_length=1,max_length=256)
    expected_write_revision: int=Field(ge=1)
    acknowledge_fresh_preview_read: StrictBool
    request_uuid: str=Field(pattern=UUID_PATTERN)


class Decision(Strict):
    decision: Literal["approved","denied"]
    expected_digest: str=Field(pattern=r"^[a-f0-9]{64}$")


class Cancel(Strict):
    expected_revision: int=Field(ge=1)
    request_uuid: str=Field(pattern=UUID_PATTERN)


class Observation(Strict):
    expected_original_revision: int=Field(ge=1)
    read_connection_id: str=Field(min_length=1,max_length=256)
    expected_read_revision: int=Field(ge=1)
    goal_id: str=Field(min_length=1,max_length=256)
    goal_revision: int=Field(ge=1)
    acknowledge_readonly_recovery: StrictBool
    request_uuid: str=Field(pattern=UUID_PATTERN)


def operator(request):
    from src.api.calendar import _operator
    return _operator(request)


async def body(request,model):
    from src.api.calendar import _json_body
    parsed=await _json_body(request,model)
    for name,value in parsed.model_dump().items():
        if name.startswith("acknowledge_") and value is not True:
            raise HTTPException(422,detail={"code":"calendar_reschedule_explicit_acknowledgement_required"})
    return parsed


def error(exc):
    from src.auth.service import AuthFailure
    if isinstance(exc,HTTPException): return exc
    return HTTPException(status_code=401 if isinstance(exc,AuthFailure) else getattr(exc,"status_code",409),
        detail={"code":getattr(exc,"code","calendar_reschedule_binding_unavailable"),"message":"The exact Calendar operation is unavailable; inspect its canonical original receipt",
            "recovery_action":getattr(exc,"recovery_action","inspect_original_reschedule")})


def profile(row):
    return {"connection_id":row.connection_id,"service":row.service,"label":row.label,"revision":row.revision,"state":row.state,
        "scope_status":row.scope_status,"declared_scopes":json.loads(row.declared_scopes_json),"verified_setup_job_id":row.verified_setup_job_id,
        "provider_contact":False,"setup_is_write_permission":False,
        "cleanup_retry": {"expected_revision":row.revision,"idempotency_key":row.revoke_idempotency_key,
            "revoke_request_digest":row.revoke_request_digest} if row.state=="blocked_cleanup" and row.revoke_idempotency_key and row.revoke_request_digest else None}


@router.get("/profiles")
async def list_profiles(request:Request):
    owner=operator(request)
    async with runtime.session() as db:
        await current_root(db,owner)
        rows=(await db.execute(select(GoogleServiceConnection).where(GoogleServiceConnection.owner_principal_id==owner.principal.principal_id,
            GoogleServiceConnection.owner_session_id==owner.session_id,GoogleServiceConnection.service.in_(SCOPES)).order_by(GoogleServiceConnection.created_at.desc()).limit(32))).scalars().all()
        return {"profiles":[profile(row) for row in rows],"provider_contact":False}


@router.post("/profiles")
async def import_profile(request:Request):
    owner=operator(request); inner=await body(request,ProfileCreate)
    if len(set(inner.declared_scopes))!=4 or frozenset(inner.declared_scopes)!=SCOPES[inner.service]: raise HTTPException(422,detail={"code":"calendar_reschedule_scope_not_exact"})
    for value in (inner.label,inner.client_id,inner.refresh_token,inner.client_secret or "x"): text(value,8192)
    credentials={"client_id":inner.client_id,"client_secret":inner.client_secret,"refresh_token":inner.refresh_token}
    request_digest=runtime.digest(inner.model_dump())
    async with _IMPORT_LOCK:
        async with runtime.writer() as db:
            await current_root(db,owner)
            row=(await db.execute(select(GoogleServiceConnection).where(GoogleServiceConnection.owner_principal_id==owner.principal.principal_id,
                GoogleServiceConnection.owner_session_id==owner.session_id,GoogleServiceConnection.setup_idempotency_key==inner.idempotency_key))).scalar_one_or_none()
            if row is not None:
                if row.setup_request_digest!=request_digest or row.service!=inner.service: raise HTTPException(409,detail={"code":"calendar_reschedule_setup_conflict"})
                if row.state=="active": return {"profile":profile(row)}
                if row.state!="preparing": raise HTTPException(409,detail={"code":"calendar_reschedule_setup_blocked"})
            else:
                count=await db.scalar(select(func.count()).select_from(GoogleServiceConnection).where(GoogleServiceConnection.owner_principal_id==owner.principal.principal_id,
                    GoogleServiceConnection.owner_session_id==owner.session_id,GoogleServiceConnection.service.in_(SCOPES)))
                if count>=32: raise HTTPException(409,detail={"code":"calendar_reschedule_profile_bound"})
                row=GoogleServiceConnection(owner_principal_id=owner.principal.principal_id,owner_session_id=owner.session_id,service=inner.service,label=inner.label,
                    vault_secret_key="calendar-reschedule:"+secrets.token_urlsafe(24),credential_fingerprint=runtime.digest(credentials),declared_scopes_json=json.dumps(sorted(SCOPES[inner.service])),
                    setup_idempotency_key=inner.idempotency_key,setup_request_digest=request_digest,state="preparing",revision=1)
                db.add(row); await db.flush()
            ident,key=row.connection_id,row.vault_secret_key
        raw=json.dumps(credentials,sort_keys=True)
        prior=await vault_repository.get(key,owner_principal_id=owner.principal.principal_id)
        if prior is None: await vault_repository.store(key,raw,description="Separate exact Calendar owned-event identity profile",owner_principal_id=owner.principal.principal_id)
        elif prior!=raw: raise HTTPException(409,detail={"code":"calendar_reschedule_setup_conflict"})
        async with runtime.writer() as db:
            await current_root(db,owner); row=await db.get(GoogleServiceConnection,ident,populate_existing=True)
            if row is None or row.setup_request_digest!=request_digest or row.state not in {"preparing","active"}: raise HTTPException(409,detail={"code":"calendar_reschedule_setup_changed"})
            if row.state=="preparing": row.state="active"; row.revision+=1; row.updated_at=runtime.now()
            return {"profile":profile(row)}


@router.get("/profiles/recovery/{request_uuid}")
async def recover_profile(request:Request,request_uuid:str):
    owner=operator(request)
    async with runtime.session() as db:
        await current_root(db,owner)
        row=(await db.execute(select(GoogleServiceConnection).where(GoogleServiceConnection.owner_principal_id==owner.principal.principal_id,
            GoogleServiceConnection.owner_session_id==owner.session_id,GoogleServiceConnection.service.in_(SCOPES),GoogleServiceConnection.setup_idempotency_key==request_uuid))).scalar_one_or_none()
        return {"profile":profile(row) if row else None,"provider_contact":False}


@router.post("/profiles/{connection_id}/revoke")
async def revoke_profile(request:Request,connection_id:str):
    owner=operator(request); inner=await body(request,ProfileRevokeControl)
    try: return await _revoke_profile(owner,connection_id,inner)
    except Exception as exc: raise error(exc) from exc


async def _revoke_profile(owner,connection_id,inner):
    request_digest=runtime.digest([connection_id,inner.model_dump(exclude={"revoke_request_digest"})])
    async with runtime.writer() as db:
        await current_root(db,owner); row=await db.get(GoogleServiceConnection,connection_id,populate_existing=True)
        if row is None or row.owner_principal_id!=owner.principal.principal_id or row.owner_session_id!=owner.session_id or row.service not in SCOPES: raise HTTPException(404,detail={"code":"calendar_reschedule_profile_unavailable"})
        if row.revoke_idempotency_key:
            retry=inner.revoke_request_digest is not None
            if row.revoke_idempotency_key!=inner.idempotency_key or row.state not in {"revoked","blocked_cleanup"} or (retry and (inner.revoke_request_digest!=row.revoke_request_digest or inner.expected_revision!=row.revision)) or (not retry and row.revoke_request_digest!=request_digest):
                raise HTTPException(409,detail={"code":"calendar_reschedule_revoke_conflict"})
        else:
            if inner.revoke_request_digest is not None: raise HTTPException(409,detail={"code":"calendar_reschedule_revoke_conflict"})
            if row.revision!=inner.expected_revision: raise HTTPException(409,detail={"code":"calendar_reschedule_revision_changed"})
            # A crash or denied final CAS keeps authority revoked and cleanup
            # visibly unfinished. Never do Vault/crypto/filesystem I/O here.
            row.state="blocked_cleanup"; row.revision+=1; row.revoke_idempotency_key=inner.idempotency_key; row.revoke_request_digest=request_digest; row.verified_setup_job_id=None; row.updated_at=runtime.now()
        key,revision,frozen_digest=row.vault_secret_key,row.revision,row.revoke_request_digest
    cleanup_ok=False
    try:
        async with asyncio.timeout(5):
            await vault_repository.delete(key,owner_principal_id=owner.principal.principal_id)
            cleanup_ok=await vault_repository.snapshot(key,owner_principal_id=owner.principal.principal_id) is None
    except Exception:
        # Credential deletion/verification failure is local, bounded and
        # retryable only with this original owner/Root/revoke identity.
        pass
    async with runtime.writer() as db:
        await current_root(db,owner); row=await db.get(GoogleServiceConnection,connection_id,populate_existing=True)
        if row is None or row.owner_principal_id!=owner.principal.principal_id or row.owner_session_id!=owner.session_id or row.service not in SCOPES or row.vault_secret_key!=key or row.revoke_idempotency_key!=inner.idempotency_key or row.revoke_request_digest!=frozen_digest:
            raise HTTPException(409,detail={"code":"calendar_reschedule_revoke_conflict"})
        if row.revision!=revision:
            # Another exact cleanup may have converged while I/O awaited.
            # Its verified terminal state wins; never downgrade it or renew.
            if row.state!="revoked" or not cleanup_ok: raise HTTPException(409,detail={"code":"calendar_reschedule_revision_changed"})
        elif row.state!=("revoked" if cleanup_ok else "blocked_cleanup"):
            row.state="revoked" if cleanup_ok else "blocked_cleanup"; row.revision+=1; row.updated_at=runtime.now()
        result=profile(row)
    if not cleanup_ok:
        raise HTTPException(503,detail={"code":"calendar_reschedule_credential_cleanup_blocked","message":"Local authority is revoked; Vault credential cleanup remains unverified. Refresh metadata and explicitly retry the original cleanup.",
            "recovery_action":"retry_cleanup","provider_contact":False,"credential_cleanup":"blocked_cleanup","encrypted_audit_bytes_may_remain":True,"physical_erasure_verified":False})
    return {"profile":result,"provider_contact":False,"credential_cleanup":"verified_unavailable","encrypted_audit_bytes_may_remain":True,"physical_erasure_verified":False}


async def expected_pair(owner,inner, *, write=True):
    rows=await runtime.pair_snapshots(owner,inner.read_connection_id,inner.write_connection_id if write else None)
    if rows[0]["revision"]!=inner.expected_read_revision or (write and rows[1]["revision"]!=inner.expected_write_revision): raise HTTPException(409,detail={"code":"calendar_reschedule_revision_changed"})


@router.post("/profiles/verify-pair")
async def verify_pair(request:Request):
    owner=operator(request); inner=await body(request,Pair); binding=runtime.digest(inner.model_dump())
    try:
        replay=await runtime.request_replay(owner,kind=runtime.IDENTITY_KIND,request_uuid=inner.request_uuid,request_binding=binding)
        if replay is not None: return replay
        await expected_pair(owner,inner)
        return await runtime.run_owned(owner,runtime.job_id(owner,runtime.IDENTITY_KIND,inner.request_uuid),lambda:runtime.verify_pair(owner,
            request_uuid=inner.request_uuid,goal_id=inner.goal_id,goal_revision=inner.goal_revision,read_connection_id=inner.read_connection_id,send_connection_id=inner.write_connection_id,
            event_binding_id=inner.event_binding_id,expected_event_binding_revision=inner.expected_event_binding_revision,request_binding=binding))
    except Exception as exc: raise error(exc) from exc


@router.get("/consents")
async def list_consents(request:Request):
    owner=operator(request)
    async with runtime.session() as db:
        await current_root(db,owner)
        rows=(await db.execute(select(CalendarRescheduleConsent).where(CalendarRescheduleConsent.owner_principal_id==owner.principal.principal_id,
            CalendarRescheduleConsent.original_root_session_id==owner.session_id).order_by(CalendarRescheduleConsent.created_at.desc()).limit(32))).scalars().all()
        return {"consents":[runtime.consent_metadata(row) for row in rows],"provider_contact":False}


@router.post("/consents")
async def create_consent(request:Request):
    try: return {"consent":await runtime.create_consent(operator(request),body=await body(request,ConsentCreate))}
    except Exception as exc: raise error(exc) from exc


@router.post("/consents/{consent_id}/revoke")
async def revoke_consent(request:Request,consent_id:str):
    inner=await body(request,Control)
    try: return {"consent":await runtime.revoke_consent(operator(request),consent_id,request_uuid=inner.idempotency_key,expected_revision=inner.expected_revision)}
    except Exception as exc: raise error(exc) from exc


@router.post("/tasks")
async def create_task(request:Request):
    from src.work_board.contracts import WorkBoardOwner,WorkBoardInputArtifactCreate,WorkBoardTaskCreate,WorkBoardStatus
    from src.work_board.dispatcher import validate_capability_input
    from src.work_board.input_artifacts import prepare_input_artifact
    from src.work_board.repository import WorkBoardRepository
    from src.api.work_board import _safe_task_payload
    owner=operator(request); inner=await body(request,TaskCreate)
    try:
        typed=validate_capability_input("calendar.event.reschedule.v1",inner.input)
        proposed_times(typed["new_start"],typed["new_end"])
        board_owner=WorkBoardOwner(principal_id=owner.principal.principal_id,session_id=owner.session_id)
        async with runtime.session() as db:
            await current_root(db,owner); row=await db.get(CalendarRescheduleConsent,typed["consent_id"],populate_existing=True)
            if row is None or row.revision!=typed["expected_consent_revision"] or row.event_binding_id!=typed["event_binding_id"] or row.event_binding_revision!=typed["expected_event_binding_revision"] or (row.goal_id,row.goal_revision)!=(typed["goal_id"],typed["goal_revision"]): runtime.fail("write_consent_changed")
            expected=consent_snapshot(row); await consent(db,owner,expected)
            async def publication_authority(current_db):
                await current_root(current_db,owner); await consent(current_db,owner,expected)
                await runtime.selection_snapshot(current_db,owner,typed["event_binding_id"],expected_revision=typed["expected_event_binding_revision"])
            metadata=await prepare_input_artifact(db,board_owner,WorkBoardInputArtifactCreate(schema_version=1,capability_id="calendar.event.reschedule.v1",
                goal_id=typed["goal_id"],goal_revision=typed["goal_revision"],input=typed,idempotency_key=inner.request_uuid))
            task=await WorkBoardRepository().create_task(db,board_owner,WorkBoardTaskCreate(title=inner.title,body="One literal owned-event reschedule; exact preview and approval required; no learning.",
                status=WorkBoardStatus.todo,goal_id=typed["goal_id"],goal_revision=typed["goal_revision"],capability_id="calendar.event.reschedule.v1",input_artifact_id=metadata.artifact_id,
                priority=60,idempotency_scope="calendar-exact-reschedule",idempotency_key=inner.request_uuid),
                origin_session_id=owner.session_id,publication_authority_check=publication_authority)
            return {"task":await _safe_task_payload(task.task,db=db),"idempotent_replay":task.idempotent_replay,"provider_contact":False}
    except Exception as exc: raise error(exc) from exc


@router.post("/operations/preview")
async def preview(request:Request):
    owner=operator(request); inner=await body(request,Preview); binding=runtime.digest(inner.model_dump())
    try:
        replay=await runtime.request_replay(owner,kind=runtime.SEND_KIND,request_uuid=inner.request_uuid,request_binding=binding)
        if replay is not None: return replay
        await expected_pair(owner,inner); source,_=await runtime.stage_source(owner,inner.task_id)
        if source["task_revision"]!=inner.expected_task_revision: runtime.fail("task_revision_changed")
        return await runtime.run_owned(owner,runtime.job_id(owner,runtime.SEND_KIND,inner.request_uuid),lambda:runtime.preview(owner,task_id=inner.task_id,
            read_connection_id=inner.read_connection_id,send_connection_id=inner.write_connection_id,request_uuid=inner.request_uuid,request_binding=binding))
    except Exception as exc: raise error(exc) from exc


@router.get("/operations/recovery/{kind}/{request_uuid}")
async def recover_operation(request:Request,kind:str,request_uuid:str):
    if kind not in {runtime.IDENTITY_KIND,runtime.SEND_KIND,runtime.OBSERVATION_KIND}: raise HTTPException(422,detail={"code":"calendar_reschedule_kind_invalid"})
    try: return {"job":await runtime.snapshot(operator(request),runtime.job_id(operator(request),kind,request_uuid)),"provider_contact":False}
    except runtime.DurableJobNotFound: return {"job":None,"provider_contact":False}
    except Exception as exc: raise error(exc) from exc


@router.get("/operations/{job_id}")
async def inspect(request:Request,job_id:str):
    try: return await runtime.snapshot(operator(request),job_id)
    except Exception as exc: raise error(exc) from exc


@router.get("/operations/{job_id}/private")
async def inspect_private(request:Request,job_id:str):
    try: return await runtime.snapshot(operator(request),job_id,include_private=True)
    except Exception as exc: raise error(exc) from exc


@router.post("/operations/{job_id}/decision")
async def decision(request:Request,job_id:str):
    inner=await body(request,Decision)
    try: return await runtime.decide(operator(request),job_id,decision=inner.decision,expected_digest=inner.expected_digest)
    except Exception as exc: raise error(exc) from exc


@router.post("/operations/{job_id}/execute")
async def execute(request:Request,job_id:str):
    await body(request,Strict); owner=operator(request)
    try: return await runtime.run_owned(owner,job_id,lambda:runtime.execute(owner,job_id))
    except Exception as exc: raise error(exc) from exc


@router.post("/operations/{job_id}/cancel")
async def cancel(request:Request,job_id:str):
    inner=await body(request,Cancel)
    try: return await runtime.cancel(operator(request),job_id,request_uuid=inner.request_uuid,expected_revision=inner.expected_revision)
    except Exception as exc: raise error(exc) from exc


@router.post("/operations/{job_id}/observe")
async def observe(request:Request,job_id:str):
    owner=operator(request); inner=await body(request,Observation); binding=runtime.digest([job_id,inner.model_dump()])
    try:
        replay=await runtime.request_replay(owner,kind=runtime.OBSERVATION_KIND,request_uuid=inner.request_uuid,request_binding=binding)
        if replay is not None: return replay
        await expected_pair(owner,inner,write=False)
        return await runtime.run_owned(owner,runtime.job_id(owner,runtime.OBSERVATION_KIND,inner.request_uuid),lambda:runtime.observe(owner,
            original_job_id=job_id,expected_original_revision=inner.expected_original_revision,read_connection_id=inner.read_connection_id,
            goal_id=inner.goal_id,goal_revision=inner.goal_revision,request_uuid=inner.request_uuid,request_binding=binding))
    except Exception as exc: raise error(exc) from exc
