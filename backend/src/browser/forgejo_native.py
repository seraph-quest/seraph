"""One fixed Forgejo transaction on canonical jobs, approvals and Vault.

All file/crypto/network work precedes writers. Writer callbacks compare only
canonical rows and immutable staged digests; no browser input grants authority.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import uuid

from sqlmodel import select

from config.settings import settings
from src.approval.repository import approval_repository, approval_decision_digest, fingerprint_tool_call
from src.artifacts.registry import build_artifact_record
from src.browser.forgejo_issue_title import (CAPABILITY, JOB_KIND, PROFILE, ForgejoError,
    TitleTarget, canonical, digest, checked_segment, checked_title, positive_id)
from src.browser.forgejo_profile import BackendSession
from src.browser.task_lane import try_acquire_browser_task_lane
from src.db import engine
from src.db.models import ApprovalRequest, ForgejoConnection, Goal, Secret, WorkflowRunState
from src.integrations.forgejo_controls import original_root, now, utc, writer, credential_payload
from src.tools.filesystem_tool import _read_workspace_text_bounded, _safe_resolve
from src.vault.crypto import decrypt, encrypt
from src.vault.repository import secret_binding_digest, vault_repository
from src.work_board.input_artifacts import _write_payload
from src.work_board.repository import WorkBoardRepository
from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec, durable_job_repository, _serialize, _digest as authority_digest
from src.workspace import canonical_workspace_root

PREFIX = "artifacts/forgejo/"
STATE = "forgejo:state"


def reference(job_id, kind):
    return PREFIX+digest(job_id.encode())+"."+kind+".enc"


def journal(run):
    entries = json.loads(run.checkpoint_receipts_json or "[]")
    found = [v for v in entries if v.get("checkpoint_id") == STATE]
    if len(found) != 1 or digest(found[0]["payload"]) != found[0]["state_digest"]:
        raise ForgejoError("forgejo_canonical_journal_changed")
    return found[0]["payload"]


def save(run, value):
    if len(canonical(value)) > 65536 or len(value.get("calls",[])) > 64:
        raise ForgejoError("forgejo_canonical_journal_bound")
    run.checkpoint_receipts_json = canonical([{"checkpoint_id":STATE,"payload":value,
        "state_digest":digest(value),"fencing_token":run.fencing_token,
        "recorded_at":now().isoformat(),"safe":True}]).decode()
    run.revision += 1; run.updated_at = now()


def stage(job_id, kind, payload=None):
    ref = reference(job_id,kind)
    path = canonical_workspace_root(settings.workspace_dir)/ref
    if payload is not None and not path.exists():
        raw = encrypt(canonical(payload).decode()).encode()
        if len(raw) > 98304: raise ForgejoError("forgejo_private_output_bound")
        _write_payload(path,raw)
    text,over = _read_workspace_text_bounded(_safe_resolve(ref),max_bytes=98304)
    if over: raise ForgejoError("forgejo_private_artifact_bound")
    value = json.loads(decrypt(text))
    if len(canonical(value)) > 65536: raise ForgejoError("forgejo_private_artifact_bound")
    if payload is not None and value != payload: raise ForgejoError("forgejo_private_artifact_readback_changed")
    return ref,digest(text.encode()),value


class ForgejoNative:
    def __init__(self, service):
        self.service = service
        self.jobs = durable_job_repository
        self.active = {}

    async def consent(self, owner, *, expected_revision, duration_seconds, read_ack):
        if read_ack is not True or type(duration_seconds) is not int or not 30 <= duration_seconds <= 900:
            raise ForgejoError("forgejo_explicit_finite_read_consent_required",status_code=422)
        async with engine.get_session() as db:
            await writer(db); root = await original_root(db,owner)
            row = await db.scalar(select(ForgejoConnection).where(
                ForgejoConnection.owner_principal_id==owner.principal_id))
            if row is None or row.owner_session_id != owner.session_id or row.revision != expected_revision:
                raise ForgejoError("forgejo_configuration_revision_changed")
            # A new explicit READ window grants no replay of an old write.
            # Old jobs retain their original read revision/deadline; a later
            # recovery is a separate fixed GET-only canonical job.
            row.read_consent_revision += 1
            row.read_consent_expires_at = min(now()+timedelta(seconds=duration_seconds),
                utc(root.absolute_expires_at),utc(root.idle_expires_at))
            row.updated_at = now(); db.add(row)
        return await self.service.connection(owner)

    async def inventory(self, db, owner, connection_id, *, excluding=None, observed_original=None):
        # Filtering by owner and fixed kind is performed before this cap;
        # every bounded canonical row is inspected, never a truncated success.
        runs = (await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.owner_principal_id==owner.principal_id,
            WorkflowRunState.job_kind==JOB_KIND,
            WorkflowRunState.status.not_in(("succeeded","degraded","cancelled")))
            .order_by(WorkflowRunState.updated_at.desc()).limit(21))).scalars().all()
        if len(runs)>20: raise ForgejoError("forgejo_unresolved_inventory_overflow")
        for run in runs:
            try: authority = json.loads(run.declared_authority_json)
            except (TypeError,ValueError): raise ForgejoError("forgejo_unresolved_inventory_invalid") from None
            if not isinstance(authority,dict) or not authority.get("connection_id"):
                raise ForgejoError("forgejo_unresolved_inventory_invalid")
            if (run.run_identity not in {excluding,observed_original}
                and authority["connection_id"]==connection_id):
                value = journal(run) if run.checkpoint_receipts_json not in (None,"","[]") else {}
                # An Unknown POST can never become free from expiry, equality
                # observation, absence of a process or a missing lock.
                if value.get("capacity_closed") is not True:
                    raise ForgejoError("forgejo_connection_operation_unresolved")

    async def current(self, db, owner, run, *, lease=None, admission=False, read_only=False):
        await original_root(db,owner)
        if (run.owner_principal_id != owner.principal_id or run.operator_session_id != owner.session_id
            or run.job_kind != JOB_KIND or run.capability_version != "1"):
            raise ForgejoError("forgejo_original_job_owner_changed",status_code=404)
        await WorkBoardRepository._validate_goal(db,owner,goal_id=run.goal_id,goal_revision=run.goal_revision)
        authority = json.loads(run.declared_authority_json)
        if (run.authority_digest != authority_digest(authority) or authority.get("capability_id") != CAPABILITY
            or authority.get("principal") != owner.principal_id or authority.get("session_id") != owner.session_id
            or authority.get("no_learning") is not True or authority.get("profile") != PROFILE):
            raise ForgejoError("forgejo_original_authority_changed")
        if not read_only and (run.deadline_at is None or utc(run.deadline_at)<=now()):
            raise ForgejoError("forgejo_original_deadline")
        row = await db.get(ForgejoConnection,authority["connection_id"],populate_existing=True)
        if (row is None or row.owner_principal_id != owner.principal_id or row.owner_session_id != owner.session_id
            or row.state=="revoked" or row.revision != authority["connection_revision"]
            or row.credential_binding != authority["vault_binding_digest"]
            or row.read_consent_revision != authority["read_consent_revision"]
            or row.read_consent_expires_at is None or utc(row.read_consent_expires_at)<=now()):
            raise ForgejoError("forgejo_current_connection_read_consent_required")
        expected_inputs={"payload_ref":reference(run.run_identity,"input"),
            "payload_digest":authority["payload_digest"],"no_learning":True}
        if run.input_digest != digest(expected_inputs): raise ForgejoError("forgejo_original_input_changed")
        for key,binding in [(row.credential_vault_key,authority["vault_binding_digest"])]+(
            [] if authority["operation"]=="provision" else [(row.session_vault_key,authority["session_binding"])]):
            secret = await db.scalar(select(Secret).where(Secret.key==key,
                Secret.owner_principal_id==owner.principal_id,Secret.revoked_at.is_(None)))
            if secret is None or secret_binding_digest(secret)!=binding:
                raise ForgejoError("forgejo_original_vault_binding_changed")
        if authority["operation"]!="provision" and (
            row.state!="active" or row.provider_user_id!=authority["provider_user_id"]
            or row.provider_login!=authority["provider_login"] or row.session_binding!=authority["session_binding"]):
            raise ForgejoError("forgejo_original_numeric_session_identity_changed")
        if authority["operation"]=="title" and run.status=="running":
            value=journal(run);approval=await db.get(ApprovalRequest,value.get("approval_id"))
            if (approval is None or approval.status!="consumed" or approval.owner_principal_id!=owner.principal_id
                or approval.operator_session_id!=owner.session_id or approval.tool_name!=CAPABILITY
                or approval.fingerprint!=value.get("approval_fingerprint") or utc(approval.expires_at)<=now()
                or json.loads(approval.details_json).get("approval_scope")!=value.get("approval_scope")
                or value["approval_scope"]["authority_digest"]!=run.authority_digest):
                raise ForgejoError("forgejo_original_consumed_approval_changed")
        observed_original=None
        if authority["operation"]=="observe":
            observed_original=authority["original_job_id"]
            original=await self.jobs._fetch(db,observed_original)
            original_authority=json.loads(original.declared_authority_json)
            if (original.status!="unknown_external_effect" or original.job_kind!=JOB_KIND
                or original.owner_principal_id!=owner.principal_id or original.operator_session_id!=owner.session_id
                or original.goal_id!=run.goal_id or original.goal_revision!=run.goal_revision
                or original.revision!=authority["original_job_revision"]
                or original.fencing_token!=authority["original_job_fence"]
                or original_authority.get("operation")!="title"
                or original.authority_digest!=authority["original_authority_digest"]
                or any(original_authority.get(k)!=authority.get(k) for k in (
                    "connection_id","connection_revision","vault_binding_digest","session_binding",
                    "provider_user_id","provider_login"))
                or not any(c["operation"]=="title_submission" for c in journal(original)["calls"])):
                raise ForgejoError("forgejo_original_unknown_observation_binding_changed")
        if lease is not None: self.jobs._assert_lease(run,owner=lease[0],fencing_token=lease[1])
        if admission: await self.inventory(db,owner,row.id,excluding=run.run_identity,observed_original=observed_original)
        return row

    async def snapshot(self, owner, job_id):
        async with engine.get_session() as db:
            await original_root(db,owner)
            run = await self.jobs._fetch(db,job_id)
            if (run.owner_principal_id!=owner.principal_id or run.operator_session_id!=owner.session_id
                or run.job_kind!=JOB_KIND): raise ForgejoError("forgejo_job_not_owned",status_code=404)
            result = _serialize(run)
            value = journal(run) if run.checkpoint_receipts_json not in (None,"","[]") else {}
            result["forgejo"] = value
            approval = await db.get(ApprovalRequest,value.get("approval_id")) if value.get("approval_id") else None
            result["approval"] = {"id":approval.id,"status":approval.status,
                "expires_at":utc(approval.expires_at).isoformat()} if approval else None
            result["no_learning"] = True
            return result

    async def prepare(self, owner, *, operation, fields, request_key, goal_id, goal_revision,
                      expected_revision, preview_job_id=None, preview_digest=None, original_binding=None):
        self.service.browser.require_available()
        try: uuid.UUID(request_key)
        except (ValueError,TypeError,AttributeError): raise ForgejoError("forgejo_request_uuid_required",status_code=422) from None
        if operation not in {"provision","preview","title","observe"}:
            raise ForgejoError("forgejo_fixed_operation_required",status_code=422)
        if operation in {"preview","observe"}:
            if set(fields)!={"owner","repository","issue_index","new_title"}:
                raise ForgejoError("forgejo_closed_title_input_required",status_code=422)
            checked_segment(fields["owner"]);checked_segment(fields["repository"])
            positive_id(fields["issue_index"]);checked_title(fields["new_title"])
        elif fields: raise ForgejoError("forgejo_closed_input_required",status_code=422)
        request={"operation":operation,"fields":fields,"goal_id":goal_id,"goal_revision":goal_revision,
            "expected_revision":expected_revision,"preview_job_id":preview_job_id,"preview_digest":preview_digest,
            "original_binding":original_binding}
        job_id="forgejo:"+digest([owner.principal_id,owner.session_id,request_key])[:40]
        prior=await self.jobs.get_job(job_id)
        if prior is not None:
            prior_authority=prior["declared_authority"]
            if prior_authority.get("request_digest")!=digest(request): raise ForgejoError("forgejo_idempotency_conflict")
            return await self.snapshot(owner,job_id)
        payload={"operation":operation,"fields":fields,"no_learning":True}
        if operation=="observe":
            if not isinstance(original_binding,dict):raise ForgejoError("forgejo_original_unknown_binding_required")
            payload["original_binding"]=original_binding
        if operation=="title":
            preview=await self.output(owner,preview_job_id)
            snap=await self.snapshot(owner,preview_job_id)
            if (snap["status"]!="succeeded" or snap["declared_authority"]["operation"]!="preview"
                or preview_digest!=digest(preview) or snap["goal_id"]!=goal_id or snap["goal_revision"]!=goal_revision
                or snap["declared_authority"]["connection_revision"]!=expected_revision
                or now()-utc(datetime.fromisoformat(snap["finished_at"]))>timedelta(seconds=300)):
                raise ForgejoError("forgejo_current_exact_preview_required")
            payload["target"]=preview["target"]
            if payload["target"]["old_title"]==payload["target"]["new_title"]:
                raise ForgejoError("forgejo_no_change_read_only_preview")
            payload["preview_job_id"]=preview_job_id;payload["preview_digest"]=preview_digest
        ref,cipher_digest,_=stage(job_id,"input",payload)
        async with engine.get_session() as db:
            root=await original_root(db,owner)
            goal=await WorkBoardRepository._validate_goal(db,owner,goal_id=goal_id,goal_revision=goal_revision)
            row=await db.scalar(select(ForgejoConnection).where(ForgejoConnection.owner_principal_id==owner.principal_id))
            if row is None or row.revision!=expected_revision: raise ForgejoError("forgejo_configuration_revision_changed")
            deadline=min(now()+timedelta(seconds=120),utc(root.absolute_expires_at),utc(root.idle_expires_at),
                utc(row.read_consent_expires_at) if row.read_consent_expires_at else now())
            if goal.due_date is not None: deadline=min(deadline,utc(goal.due_date))
            authority={"principal":owner.principal_id,"owner_kind":"user","session_id":owner.session_id,
                "capability_id":CAPABILITY,"profile":PROFILE,"operation":operation,"connection_id":row.id,
                "connection_revision":row.revision,"read_consent_revision":row.read_consent_revision,
                "vault_binding_digest":row.credential_binding,"session_binding":row.session_binding,
                "provider_user_id":row.provider_user_id,"provider_login":row.provider_login,
                "payload_digest":digest(payload),"input_cipher_digest":cipher_digest,"request_digest":digest(request),
                "permissions":["forgejo_private_read","credential_egress","browser_process","workspace_write"]+
                    (["external_mutation"] if operation=="title" else ["provider_session_login"] if operation=="provision" else []),
                "no_learning":True}
            if operation=="observe":authority.update(original_binding)
        async def guard(db,candidate): await self.current(db,owner,candidate,admission=True)
        admitted=await self.jobs.admit_job(DurableJobSpec(identity=DurableJobIdentity(job_id=job_id,
            owner_kind="user",owner_principal_id=owner.principal_id,job_kind=JOB_KIND,capability_version="1",
            idempotency_scope="forgejo-title",idempotency_key=request_key),
            inputs={"payload_ref":ref,"payload_digest":digest(payload),"no_learning":True},
            session_id=owner.session_id,operator_session_id=owner.session_id,goal_id=goal_id,goal_revision=goal_revision,
            priority=50,declared_authority=authority,deadline_at=deadline,max_attempts=1,budget_microusd=0,
            resource_claims=("browser-task-lane",),run_fingerprint=digest([digest(payload),authority,50])),
            admission_authority_check=guard)
        async with engine.get_session() as db:
            await writer(db);run=await self.jobs._fetch(db,job_id);await self.current(db,owner,run)
            save(run,{"phase":"prepared","calls":[],"operation":operation,"no_learning":True})
            db.add(run)
        if operation=="title": await self.make_approval(owner,job_id,payload)
        return await self.snapshot(owner,job_id)

    async def recover(self, owner, original_job_id, *, expected_revision, original_job_revision,
                      original_fencing_token, request_key, read_ack):
        if read_ack is not True:raise ForgejoError("forgejo_separate_read_only_ack_required",status_code=422)
        async with engine.get_session() as db:
            await original_root(db,owner);original=await self.jobs._fetch(db,original_job_id)
            if (original.job_kind!=JOB_KIND or original.owner_principal_id!=owner.principal_id
                or original.operator_session_id!=owner.session_id or original.status!="unknown_external_effect"
                or original.revision!=original_job_revision or original.fencing_token!=original_fencing_token):
                raise ForgejoError("forgejo_exact_original_unknown_required")
            a=json.loads(original.declared_authority_json)
            if a.get("operation")!="title":raise ForgejoError("forgejo_exact_original_title_required")
            goal_id,goal_revision=original.goal_id,original.goal_revision
        _,sha,payload=stage(original_job_id,"input")
        if sha!=a["input_cipher_digest"] or digest(payload)!=a["payload_digest"]:
            raise ForgejoError("forgejo_original_private_input_changed")
        target=TitleTarget(**payload["target"])
        fields={"owner":target.owner,"repository":target.repository,"issue_index":target.issue_index,
            "new_title":target.new_title}
        binding={"original_job_id":original_job_id,"original_job_revision":original_job_revision,
            "original_job_fence":original_fencing_token,"original_authority_digest":original.authority_digest,
            "original_target":vars(target)}
        return await self.prepare(owner,operation="observe",fields=fields,request_key=request_key,
            goal_id=goal_id,goal_revision=goal_revision,expected_revision=expected_revision,original_binding=binding)

    async def make_approval(self, owner, job_id, payload):
        async with engine.get_session() as db:
            run=await self.jobs._fetch(db,job_id);await self.current(db,owner,run)
            scope={"job_id":job_id,"authority_digest":run.authority_digest,"target":payload["target"],
                "preview_digest":payload["preview_digest"],"owner":owner.principal_id,"root":owner.session_id,
                "goal_id":run.goal_id,"goal_revision":run.goal_revision,"expires_at":utc(run.deadline_at).isoformat(),
                "no_provider_cas":True,"single_original_post":True,"unknown_no_reclick":True}
        fingerprint=fingerprint_tool_call(CAPABILITY,{"scope_digest":digest(scope)})
        approval=await approval_repository.get_or_create_pending(session_id=owner.session_id,tool_name=CAPABILITY,
            risk_level="high",summary="Approve one exact issue title edit; no provider CAS or automatic repeat",
            fingerprint=fingerprint,details={"approval_scope":scope,"approval_owner_principal_id":owner.principal_id,
                "approval_owner_operator_session_id":owner.session_id,"operator_session_id":owner.session_id,
                "approval_conversation_id":owner.session_id,"approval_expires_at":utc(run.deadline_at).timestamp()})
        async with engine.get_session() as db:
            await writer(db);run=await self.jobs._fetch(db,job_id);await self.current(db,owner,run)
            value=journal(run);value.update(approval_id=approval.id,approval_scope=scope,
                approval_fingerprint=fingerprint,phase="awaiting_exact_approval")
            save(run,value);db.add(run)

    async def approve(self, owner, job_id, *, approval_id, decision, exact_ack):
        if exact_ack is not True or decision not in {"approved","denied"}:
            raise ForgejoError("forgejo_explicit_exact_approval_required",status_code=422)
        async with engine.get_session() as db:
            await writer(db);run=await self.jobs._fetch(db,job_id);await self.current(db,owner,run)
            value=journal(run);row=await db.get(ApprovalRequest,approval_id)
            if (value.get("approval_id")!=approval_id or row is None or run.status!="accepted"
                or row.owner_principal_id!=owner.principal_id or row.operator_session_id!=owner.session_id
                or row.fingerprint!=value.get("approval_fingerprint") or utc(row.expires_at)<=now()
                or json.loads(row.details_json).get("approval_scope")!=value.get("approval_scope")):
                raise ForgejoError("forgejo_exact_approval_changed")
            await approval_repository.resolve_exact_in_session(db,approval_id,decision,
                expected_digest=approval_decision_digest(row))
        return await self.snapshot(owner,job_id)

    async def output(self, owner, job_id):
        async with engine.get_session() as db:
            run=await self.jobs._fetch(db,job_id);await self.current(db,owner,run,read_only=True)
            value=journal(run)
            if not value.get("output_digest"): raise ForgejoError("forgejo_verified_private_output_unavailable")
        ref,sha,payload=stage(job_id,"output")
        if (sha!=value["output_digest"] or ref!=value["output_ref"] or payload.get("job_id")!=job_id
            or payload.get("no_learning") is not True): raise ForgejoError("forgejo_verified_private_output_changed")
        async with engine.get_session() as db:
            run=await self.jobs._fetch(db,job_id);await self.current(db,owner,run,read_only=True)
            if journal(run)!=value: raise ForgejoError("forgejo_output_snapshot_changed")
        return payload

    async def execute(self, owner, job_id, *, expected_revision, fencing_token):
        self.service.browser.require_available()
        projected=await self.snapshot(owner,job_id)
        execution_request={"expected_revision":expected_revision,"fencing_token":fencing_token}
        previous=projected["forgejo"].get("execution_request")
        if previous is not None:
            if previous!=execution_request:raise ForgejoError("forgejo_original_execution_request_changed")
            # Exact historical request retry is an inspection, even after
            # response loss/restart. It never claims, contacts or resumes.
            return projected
        if (projected["status"]!="accepted" or projected["revision"]!=expected_revision
            or projected["lease"]["fencing_token"]!=fencing_token):
            raise ForgejoError("forgejo_original_execution_not_replayable")
        async with engine.get_session() as db:
            run=await self.jobs._fetch(db,job_id);row=await self.current(db,owner,run)
            authority=json.loads(run.declared_authority_json);deadline=utc(run.deadline_at)
            credential_key=row.credential_vault_key;session_key=row.session_vault_key
        _,input_sha,payload=stage(job_id,"input")
        if input_sha!=authority["input_cipher_digest"] or digest(payload)!=authority["payload_digest"]:
            raise ForgejoError("forgejo_original_private_input_changed")
        credential=await vault_repository.snapshot(credential_key,owner_principal_id=owner.principal_id)
        if credential is None or credential.binding_digest!=authority["vault_binding_digest"]:
            raise ForgejoError("forgejo_original_vault_binding_changed")
        credentials=credential_payload(credential.value)
        session=None
        if authority["operation"]!="provision":
            snapshot=await vault_repository.snapshot(session_key,owner_principal_id=owner.principal_id)
            if snapshot is None or snapshot.binding_digest!=authority["session_binding"]:
                raise ForgejoError("forgejo_original_session_changed")
            session_value=json.loads(snapshot.value)
            session=BackendSession(session_value["value"],session_value["provider_user_id"],session_value["provider_login"])
        assets=json.loads(Path(__file__).with_name("forgejo_assets_v15.json").read_bytes())["assets"]
        lane=try_acquire_browser_task_lane(settings.workspace_dir)
        if lane is None: raise ForgejoError("browser_slot_busy")
        lease=None;cleanup={"status":"verified","browser_closed":True,"launch_attempted":False,
            "transport":{"status":"verified","requests_started":0,"requests_settled":0}};task=asyncio.current_task()
        try:
            async with engine.get_session() as db:
                await writer(db);run=await self.jobs._fetch(db,job_id);await self.current(db,owner,run)
                value=journal(run)
                if run.revision!=expected_revision or run.fencing_token!=fencing_token or value.get("execution_request"):
                    raise ForgejoError("forgejo_original_execution_request_changed")
                value["execution_request"]=execution_request;save(run,value);db.add(run)
                queue_revision=run.revision
            queued=await self.jobs.queue_job(job_id,expected_revision=queue_revision)
            runner="forgejo:"+uuid.uuid4().hex
            async def claim_guard(db,run):
                await self.current(db,owner,run)
                value=journal(run)
                if value.get("calls") or value.get("cancel_requested"):
                    raise ForgejoError("forgejo_original_execution_not_replayable")
                if authority["operation"]=="title":
                    approval=await db.get(ApprovalRequest,value.get("approval_id"))
                    if (approval is None or approval.status!="approved" or approval.tool_name!=CAPABILITY
                        or approval.owner_principal_id!=owner.principal_id or approval.operator_session_id!=owner.session_id
                        or utc(approval.expires_at)<=now() or approval.fingerprint!=value.get("approval_fingerprint")
                        or json.loads(approval.details_json).get("approval_scope")!=value.get("approval_scope")
                        or value["approval_scope"]["authority_digest"]!=run.authority_digest):
                        raise ForgejoError("forgejo_exact_current_approval_required")
                    approval.status="consumed";approval.resolved_at=now();db.add(approval)
            claimed=await self.jobs.claim_job(job_id,owner=runner,expected_revision=queued["revision"],
                expected_fencing_token=fencing_token,lease_seconds=120,claim_authority_check=claim_guard)
            lease=(runner,claimed["lease"]["fencing_token"]);self.active[job_id]=task

            def fence(run):
                if run.lease_owner!=lease[0] or run.fencing_token!=lease[1]:
                    raise ForgejoError("forgejo_original_worker_fence_changed")

            async def current():
                _,sha,value=stage(job_id,"input")
                if sha!=authority["input_cipher_digest"] or digest(value)!=authority["payload_digest"]:
                    raise ForgejoError("forgejo_original_private_input_changed")
                async with engine.get_session() as db:
                    run=await self.jobs._fetch(db,job_id);await self.current(db,owner,run,lease=lease)
                    if journal(run).get("cancel_requested"): raise ForgejoError("forgejo_cancel_requested")

            async def contact(operation,descriptor):
                if (set(descriptor) not in ({"request_id","method","path_digest","operation"},
                    {"request_id","method","path_digest","operation","body_digest"})
                    or descriptor["operation"]!=operation or descriptor["method"] not in {"GET","POST"}):
                    raise ForgejoError("forgejo_internal_request_descriptor_changed")
                async with engine.get_session() as db:
                    await writer(db);run=await self.jobs._fetch(db,job_id);await self.current(db,owner,run,lease=lease)
                    value=journal(run);calls=value["calls"]
                    if value.get("cancel_requested") or len(calls)>=64 or any(c["request_id"]==descriptor["request_id"] for c in calls):
                        raise ForgejoError("forgejo_contact_bound_or_cancelled")
                    if descriptor["method"]=="POST":
                        expected="provision_login" if authority["operation"]=="provision" else "title_submission"
                        if (operation!=expected or any(c["method"]=="POST" for c in calls)
                            or (operation=="title_submission" and (authority["operation"]!="title"
                                or descriptor.get("body_digest")!=digest(TitleTarget(**payload["target"]).title_body)))):
                            raise ForgejoError("forgejo_original_single_post_required")
                    calls.append({**descriptor,"status":"intent","fencing_token":lease[1],"at":now().isoformat()})
                    value["phase"]="contact_started";save(run,value);db.add(run)

            async def observe(operation,status,sha,transport,request_id):
                # Past-contact audit survives authority drift, but cannot
                # adopt output or grant another contact. Every concurrent
                # asset has its exact server-generated request identity.
                async with engine.get_session() as db:
                    await writer(db);run=await self.jobs._fetch(db,job_id);fence(run)
                    value=journal(run);matches=[c for c in value["calls"] if c["request_id"]==request_id]
                    if len(matches)!=1 or matches[0]["operation"]!=operation or matches[0]["status"]!="intent":
                        raise ForgejoError("forgejo_response_request_identity_changed")
                    matches[0].update(status="received",http_status=status,response_digest=sha,
                        response_closed=True,observed_at=now().isoformat())
                    save(run,value);db.add(run)

            async def retain_cleanup(value):
                nonlocal cleanup
                cleanup=value
                async with engine.get_session() as db:
                    await writer(db);run=await self.jobs._fetch(db,job_id);fence(run)
                    value=journal(run);value["cleanup"]=cleanup;save(run,value);db.add(run)

            operation=authority["operation"]
            session_cipher=None
            cleanup={"status":"unknown","browser_closed":False,"launch_attempted":True}
            if operation=="provision":
                minted,transport=await self.service.browser.provision(username=credentials["user_name"],
                    password=credentials["password"],deadline=deadline,check_current=current,contact=contact,observe=observe)
                await retain_cleanup({"status":transport["status"],"browser_closed":True,"launch_attempted":False,
                    "transport":transport,"possible_submission":False})
                session_cipher=encrypt(canonical({"value":minted.value,"provider_user_id":minted.provider_user_id,
                    "provider_login":minted.provider_login}).decode())
                result={"provider_user_id":minted.provider_user_id,"provider_login":minted.provider_login,
                    "session_provisioned":True,"no_learning":True}
            elif operation in {"preview","observe"}:
                result=await self.service.browser.inspect(**payload["fields"],username=credentials["user_name"],
                    password=credentials["password"],expected_user_id=authority["provider_user_id"],deadline=deadline,
                    check_current=current,contact=contact,observe=observe,cleanup_observer=retain_cleanup)
                if operation=="observe":
                    expected=authority["original_target"];observed=result["target"]
                    if any(observed[k]!=expected[k] for k in ("owner","repository","issue_index",
                        "repository_id","issue_id","provider_user_id","provider_login")):
                        raise ForgejoError("forgejo_original_observed_destination_changed")
                    result.update(observation_only=True,attribution_uncertain=True,original_unknown=True,
                        original_job_id=authority["original_job_id"],observed_current_title=observed["old_title"],
                        approved_title=expected["new_title"],original_capacity_released=False)
            else:
                result=await self.service.browser.submit(target=TitleTarget(**payload["target"]),session=session,
                    username=credentials["user_name"],password=credentials["password"],asset_manifest=assets,deadline=deadline,
                    check_current=current,contact=contact,observe=observe,cleanup_observer=retain_cleanup)
            result.update(job_id=job_id,operation=operation,profile=PROFILE,no_learning=True)
            ref,sha,actual=stage(job_id,"output",result)
            artifact=build_artifact_record(file_path=ref,artifact_type="forgejo_private_transaction",
                producer=JOB_KIND,run_id=job_id,session_id=owner.session_id,
                content=_safe_resolve(ref).read_bytes(),trust_boundary="owner_private_encrypted_no_model_context",
                recovery_hint="Unknown title edit: explicit GET-only inspection; never Save again")
            await current()
            async with engine.get_session() as db:
                await writer(db);run=await self.jobs._fetch(db,job_id);row=await self.current(db,owner,run,lease=lease)
                value=journal(run)
                if (cleanup["status"]!="verified" or value.get("cleanup")!=cleanup or value.get("cancel_requested")
                    or not value["calls"] or any(c["status"]!="received" for c in value["calls"])):
                    raise ForgejoError("forgejo_positive_completion_unproven")
                value.update(phase="verified",output_ref=ref,output_digest=sha,plaintext_digest=digest(actual),capacity_closed=True)
                save(run,value)
                run.artifact_receipts_json=canonical([artifact]).decode()
                run.effect_receipts_json=canonical([{"effect_id":"forgejo-output:"+sha,"effect_type":"forgejo_verified_output",
                    "receipt_kind":"readback","status":"succeeded","target_path":ref,"target_digest":sha,
                    "content_sha256":sha,"readback_id":"physical:"+sha,"verified_at":now().isoformat(),
                    "details":{"verified":True,"no_learning":True,"operation":operation}}]).decode()
                run.status="succeeded";run.finished_at=now();run.result_digest=sha
                run.result_summary="Fixed Forgejo "+operation+" verified; no learning; production blocked"
                run.lease_owner=run.lease_expires_at=None
                if operation=="provision":
                    key="forgejo.session."+row.id+"."+job_id.split(":")[1]
                    secret=Secret(key=key,owner_principal_id=owner.principal_id,encrypted_value=session_cipher,
                        description="Fixed Forgejo backend-only session")
                    db.add(secret);await db.flush();await db.refresh(secret)
                    row.session_vault_key=key;row.session_binding=secret_binding_digest(secret)
                    row.provider_user_id=result["provider_user_id"];row.provider_login=result["provider_login"]
                    row.state="active";row.provisioning_job_id=job_id;row.updated_at=now();db.add(row)
                db.add(run)
            return await self.snapshot(owner,job_id)
        except BaseException as exc:
            if lease is not None:
                async with engine.get_session() as db:
                    await writer(db);run=await self.jobs._fetch(db,job_id)
                    if run.lease_owner==lease[0] and run.fencing_token==lease[1] and run.status=="running":
                        value=journal(run)
                        uncertain=cleanup["status"]!="verified" or any(c["status"]!="received" for c in value["calls"])
                        possible_title=any(c["operation"]=="title_submission" for c in value["calls"])
                        value.update(phase="unknown" if uncertain or possible_title else "blocked",
                            cleanup=cleanup,capacity_closed=not uncertain and not possible_title)
                        save(run,value);run.status="unknown_external_effect" if uncertain or possible_title else "blocked"
                        run.failure_reason=getattr(exc,"reason",type(exc).__name__)
                        run.lease_owner=run.lease_expires_at=None;db.add(run)
            raise
        finally:
            if self.active.get(job_id) is task:self.active.pop(job_id,None)
            if cleanup["status"]=="verified":lane.release()
            else:lane.quarantine(job_id)

    async def local_withdrawal_current(self, db, owner, run):
        """Prove a never-started withdrawal using canonical rows only.

        Provider consent, credentials and Goal revision/status grant execution,
        not this zero-contact reduction of the original owner's authority.
        """
        await original_root(db,owner)
        if (run.owner_kind!="user" or run.owner_principal_id!=owner.principal_id
            or run.operator_session_id!=owner.session_id or run.job_kind!=JOB_KIND
            or run.capability_version!="1"):
            raise ForgejoError("forgejo_original_job_owner_changed",status_code=404)
        goal=await db.get(Goal,run.goal_id,populate_existing=True)
        if (goal is None or goal.owner_principal_id!=owner.principal_id
            or goal.owner_session_id!=owner.session_id):
            raise ForgejoError("forgejo_original_goal_owner_changed")
        try:
            authority=json.loads(run.declared_authority_json)
            entries=json.loads(run.checkpoint_receipts_json)
            value=journal(run)
            empty_receipts=all(json.loads(raw)==[] for raw in (
                run.effect_receipts_json,run.artifact_receipts_json,run.artifact_paths_json))
            valid=(isinstance(authority,dict) and run.authority_digest==authority_digest(authority)
                and authority.get("principal")==owner.principal_id
                and authority.get("session_id")==owner.session_id
                and authority.get("owner_kind")=="user" and authority.get("capability_id")==CAPABILITY
                and authority.get("profile")==PROFILE and authority.get("no_learning") is True
                and authority.get("operation") in {"provision","preview","title","observe"}
                and run.input_digest==digest({"payload_ref":reference(run.run_identity,"input"),
                    "payload_digest":authority["payload_digest"],"no_learning":True})
                and len(entries)==1 and entries[0].get("safe") is True
                and entries[0].get("fencing_token")==0 and run.fencing_token==0
                and run.status=="accepted" and run.attempt_count==0
                and run.lease_owner is None and run.lease_expires_at is None
                and run.finished_at is None and run.result_digest is None and run.result_summary is None
                and empty_receipts and value.get("calls")==[] and "execution_request" not in value
                and "output_ref" not in value and "output_digest" not in value
                and value.get("phase") in {"prepared","awaiting_exact_approval"}
                and value.get("operation")==authority["operation"] and value.get("no_learning") is True)
        except (TypeError,ValueError,KeyError,AttributeError):
            valid=False
        if not valid:raise ForgejoError("forgejo_never_started_withdrawal_unproven")

    async def cancel(self, owner, job_id, *, expected_revision, fencing_token):
        request={"expected_revision":expected_revision,"fencing_token":fencing_token}
        projected=await self.snapshot(owner,job_id)
        previous=projected["forgejo"].get("cancel_request")
        if previous is not None:
            if previous!=request:raise ForgejoError("forgejo_cancel_request_changed")
            return projected  # Historical exact retry; never a new grant.
        async def guard(db,run):
            value=journal(run)
            if run.revision!=expected_revision or run.fencing_token!=fencing_token:
                raise ForgejoError("forgejo_cancel_revision_changed")
            if run.status=="accepted":
                await self.local_withdrawal_current(db,owner,run)
            else:
                await self.current(db,owner,run)
            value.update(cancel_requested=True,cancel_request=request);save(run,value);db.add(run)
        # Running cancellation first persists its exact request, then the
        # directly owned task is cancelled and proves resource cleanup.
        async with engine.get_session() as db:
            await writer(db);run=await self.jobs._fetch(db,job_id)
            await guard(db,run);running=run.status=="running"
            if not running:
                if journal(run)["calls"]:raise ForgejoError("forgejo_contacted_job_readonly_recovery_required")
                run.status="cancelled";run.finished_at=now()
                value=journal(run);value.update(phase="cancelled",capacity_closed=True);save(run,value);db.add(run)
        task=self.active.get(job_id)
        if running and task is not None:task.cancel()
        return await self.snapshot(owner,job_id)
