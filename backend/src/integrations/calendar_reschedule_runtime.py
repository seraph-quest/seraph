"""Bounded Mail operations through the existing native job repository.

Encrypted staging uses the established private draft artifact helpers. There
is no outbox, retry worker or separate send ledger. Only Google HTTP is an
external boundary; each contact has a canonical pre-contact reservation.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import uuid

from sqlalchemy import select, text

from src.approval.repository import (approval_repository, approval_decision_digest,
    fingerprint_tool_call)
from src.db.models import ApprovalRequest, GoogleServiceConnection, CalendarEventBinding, CalendarReadConsent, CalendarRescheduleConsent, WorkBoardTask, Goal, Secret
from src.integrations.calendar_reschedule_contract import (CalendarRescheduleAdapter, READ_SERVICE, SEND_SERVICE,
    SCOPES, event, freeze, validate_resource, reschedule_readback, owned_calendar, instant, proposed_times, protected, provider_id, digest, fail, ConditionalConflict, ProviderRefusal)
from src.integrations.calendar_reschedule import (SEND_KIND, IDENTITY_KIND, OBSERVATION_KIND,
    VERSION, ReplyLease, consent, consent_snapshot, selection_snapshot, finite_goal_grant, arguments, authority, cas, canonical, claim_intent, connection,
    connection_snapshot, current_root, mark_dispatch, source_snapshot, state, utc,
    append_observation, assert_original_recovery)
from src.vault import vault_repository, decrypt, encrypt
from src.vault.repository import secret_binding_digest
from src.workflows.job_runtime import (DurableJobIdentity, DurableJobSpec,
    durable_job_repository, _serialize, DurableJobNotFound)
from src.workflows.mail_reply_draft import (prepare_private_draft,
    publish_private_draft, read_private_draft)


def session():
    from src.db.engine import get_session
    return get_session()


@asynccontextmanager
async def writer():
    async with session() as db:
        if db.get_bind().dialect.name == "sqlite":
            await db.execute(text("BEGIN IMMEDIATE"))
        yield db


def now():
    return datetime.now(timezone.utc)


def job_id(operator, kind, request_uuid):
    seed = "|".join([operator.principal.principal_id, operator.session_id, kind, request_uuid])
    return "calendar-reschedule:" + uuid.uuid5(uuid.NAMESPACE_URL, seed).hex


async def pair_snapshots(operator, read_id, send_id=None):
    async with session() as db:
        await current_root(db, operator)
        result = []
        for ident, service in [(read_id, READ_SERVICE), (send_id, SEND_SERVICE)]:
            if ident is None:
                continue
            row = await db.get(GoogleServiceConnection, ident, populate_existing=True)
            if row is None or row.service != service:
                fail("reply_profile_required")
            expected = connection_snapshot(row)
            secret = (await db.execute(select(Secret).where(Secret.key == row.vault_secret_key,
                Secret.owner_principal_id == operator.principal.principal_id,
                Secret.revoked_at.is_(None)))).scalar_one_or_none()
            if secret is None:
                fail("credential_unavailable")
            expected["vault_record_digest"] = secret_binding_digest(secret)
            await connection(db, operator, expected)
            result.append(expected)
        return result


async def request_replay(operator, *, kind, request_uuid, request_binding):
    """Exact authenticated receipt read; never refresh admission authority."""
    async with session() as db:
        await current_root(db, operator)
        try:
            prior = await durable_job_repository._fetch(db, job_id(operator, kind, request_uuid))
        except DurableJobNotFound:
            return None
        if (prior.owner_principal_id != operator.principal.principal_id
            or prior.operator_session_id != operator.session_id or prior.job_kind != kind
            or arguments(prior).get("operator_request_digest") != request_binding):
            fail("admission_replay_conflict")
        ident = prior.run_identity
    return await snapshot(operator, ident)


async def admit(operator, *, kind, request_uuid, goal_id, goal_revision, inputs, priority=60):
    ident = job_id(operator, kind, request_uuid)
    # Exact owner/request replay returns the original deadline and receipts.
    # Never pass a freshly computed deadline to a historical admission.
    async with session() as db:
        await current_root(db, operator)
        try:
            prior = await durable_job_repository._fetch(db, ident)
        except DurableJobNotFound:
            prior = None
        if prior is not None:
            if (prior.owner_principal_id != operator.principal.principal_id
                or prior.operator_session_id != operator.session_id or prior.job_kind != kind
                or prior.goal_id != goal_id or prior.goal_revision != goal_revision
                or prior.priority != priority or prior.input_digest != digest(inputs)):
                fail("admission_replay_conflict")
            return _serialize(prior, receipt={"kind": "admission", "status": "deduped"})
    async with session() as db:
        root = await current_root(db,operator)
        goal = await db.get(Goal,goal_id,populate_existing=True)
        grant=finite_goal_grant(goal) if goal is not None else None
        if grant is None: fail("finite_goal_grant_required")
        deadline = min(now()+timedelta(seconds=120),utc(root.idle_expires_at),utc(root.absolute_expires_at),utc(grant.period_expires_at))
        if kind==SEND_KIND: deadline=min(deadline,datetime.fromisoformat(inputs["source"]["consent"]["expires_at"]))
        if deadline<=now(): fail("deadline_expired")
    declared = {"schema_version": 1, "capability_id": "calendar.event.reschedule.v1" if kind == SEND_KIND else "calendar.event.observe-reschedule.v1",
        "capability_version": VERSION, "owner_kind": "user",
        "principal": operator.principal.principal_id, "session_id": operator.session_id,
        "operator_session_id": operator.session_id, "goal_id": goal_id, "goal_revision": goal_revision,
        "finite_authority": True, "credential_egress": True,
        "external_mutation": kind == SEND_KIND, "no_learning": True,
        "runtime_cap_seconds": 120, "max_contacts": inputs["max_contacts"],
        "max_conditional_patches": 1 if kind == SEND_KIND else 0, "budget_microusd": 0,
        "goal_grant_digest":digest(goal.admission_budget_json)}
    spec = DurableJobSpec(identity=DurableJobIdentity(job_id=ident, owner_kind="user",
        owner_principal_id=operator.principal.principal_id, job_kind=kind,
        capability_version=VERSION, idempotency_scope=kind, idempotency_key=request_uuid),
        inputs=inputs, session_id=operator.session_id, conversation_id=operator.session_id,
        operator_session_id=operator.session_id, goal_id=goal_id, goal_revision=goal_revision,
        priority=priority, resource_claims=("calendar-write",), declared_authority=declared,
        deadline_at=deadline, max_attempts=1, max_outstanding_jobs=1,
        run_fingerprint=digest(inputs), budget_microusd=0, budget_digest=digest({"budget_microusd": 0}))
    admitted = await durable_job_repository.admit_job(spec)
    # Preserve the generic redacted argument projection. Bind this fixed,
    # body-free input on the SAME canonical run before it can be queued.
    async with writer() as db:
        await current_root(db, operator)
        run = await durable_job_repository._fetch(db, ident)
        if run.input_digest != digest(inputs) or run.run_fingerprint != digest(inputs):
            fail("admission_input_changed")
        checkpoint = state(run)
        if checkpoint.get("admitted_inputs") is None:
            if run.status != "accepted" or checkpoint:
                fail("admission_phase_changed")
            await cas(db, run, {"checkpoint_context_json": canonical({
                "schema_version": 1, "admitted_inputs": inputs})})
            run = await durable_job_repository._fetch(db, ident)
            admitted = _serialize(run, receipt=admitted.get("receipt"))
        elif checkpoint["admitted_inputs"] != inputs:
            fail("admission_input_changed")
    return admitted


async def get_run(operator, ident, *, db=None):
    if db is None:
        async with session() as owned:
            run = await get_run(operator, ident, db=owned)
            owned.expunge(run)
            return run
    await current_root(db, operator)
    run = await durable_job_repository._fetch(db, ident)
    if (run.job_kind not in {SEND_KIND, OBSERVATION_KIND, IDENTITY_KIND}
        or run.owner_principal_id != operator.principal.principal_id
        or run.operator_session_id != operator.session_id):
        fail("owner_changed")
    arguments(run)
    return run


async def claim(operator, admitted, *, continuation=False):
    ident = admitted["job_id"]
    queued = await durable_job_repository.queue_job(ident,
        expected_state=admitted["status"], expected_revision=admitted["revision"])
    owner = "calendar-reschedule-worker:" + uuid.uuid4().hex
    claimed = await durable_job_repository.claim_job(ident, owner=owner, lease_seconds=120,
        expected_revision=queued["revision"], continue_existing_attempt=continuation)
    if claimed["status"] != "running":
        fail("native_admission_blocked")
    return ReplyLease(ident, owner, claimed["lease"]["fencing_token"])


async def stage_credentials(operator, run):
    inputs = arguments(run)
    async with session() as db:
        await authority(db, operator, await get_run(operator, run.run_identity, db=db),
            source_required=run.job_kind == SEND_KIND)
    result = {}
    for expected in inputs["connections"]:
        staged = await vault_repository.snapshot(expected["vault_secret_key"],
            owner_principal_id=operator.principal.principal_id)
        if staged is None or staged.binding_digest != expected["vault_record_digest"]:
            fail("credential_unavailable")
        try:
            value = json.loads(staged.value)
        except (ValueError, TypeError):
            fail("credential_invalid")
        if digest({"client_id": value.get("client_id"), "client_secret": value.get("client_secret"),
            "refresh_token": value.get("refresh_token")}) != expected["credential_fingerprint"]:
            fail("credential_changed")
        result[expected["service"]] = {key: item for key, item in value.items() if item is not None}
    return result


class Contacts:
    """Native reservations, settled response receipts and actual client closure."""
    def __init__(self, operator, lease, *, source_required=True):
        self.operator, self.lease, self.source_required = operator, lease, source_required
        self.adapters = []

    async def validate(self, operation, service):
        async with session() as db:
            run = await get_run(self.operator,self.lease.job_id,db=db)
            await authority(db,self.operator,run,lease=self.lease,source_required=self.source_required)
            if state(run).get("cancel_requested"): fail("cancel_requested")

    async def before(self, operation, service):
        async with writer() as db:
            run = await get_run(self.operator, self.lease.job_id, db=db)
            inputs = await authority(db, self.operator, run, lease=self.lease,
                source_required=self.source_required)
            checkpoint = state(run)
            if checkpoint.get("cancel_requested"):
                fail("cancel_requested")
            records = checkpoint.setdefault("contacts", [])
            if len(records) >= inputs["max_contacts"] or any(item["status"] == "reserved" for item in records):
                fail("contact_budget_or_uncertainty")
            allowed={READ_SERVICE:{"refresh","identity","calendar","event"},SEND_SERVICE:{"refresh","identity","calendar","patch"}}
            if service not in allowed or operation not in allowed[service]: fail("route_invalid")
            phase=[record for record in records if record["fence"]==self.lease.fencing_token]
            cap=(4 if run.job_kind==OBSERVATION_KIND else (6 if run.job_kind==IDENTITY_KIND else (9 if checkpoint.get("preview") else 4)))
            if len(phase)>=cap: fail("phase_contact_budget")
            if operation == "patch":
                await mark_dispatch(db, self.operator, run, lease=self.lease,
                    intent_digest=digest(checkpoint["intent"]))
                # Re-read after dispatch CAS before the same writer's contact reservation.
                run = await get_run(self.operator, self.lease.job_id, db=db)
                checkpoint = state(run)
                records = checkpoint.setdefault("contacts", [])
            records.append({"slot": len(records)+1, "operation": operation, "service": service,
                "status": "reserved", "fence": self.lease.fencing_token, "reserved_at": now().isoformat()})
            # A preview/previous phase's closure never attests this new contact.
            # Reservation, dispatch and invalidation commit in the same writer.
            checkpoint["transport_quiescent"] = False
            checkpoint.pop("transport_closure", None)
            await cas(db, run, {"checkpoint_context_json": canonical(checkpoint)})

    async def request(self, adapter, operation, **kwargs):
        refusal=None
        try: result = await adapter.request(operation, **kwargs)
        except (ConditionalConflict,ProviderRefusal) as exc:
            refusal=exc
            result={"provider_status":412 if isinstance(exc,ConditionalConflict) else exc.provider_status}
        proof = adapter.marker.snapshot()
        if proof["status"] != "verified":
            fail("transport_unsettled")
        async with writer() as db:
            run = await get_run(self.operator, self.lease.job_id, db=db)
            await authority(db, self.operator, run, lease=self.lease, source_required=self.source_required)
            checkpoint = state(run)
            records = checkpoint.get("contacts", [])
            if (not records or records[-1]["status"] != "reserved"
                or records[-1]["operation"] != operation or records[-1]["service"] != adapter.service):
                fail("contact_receipt_changed")
            records[-1].update(status="received", response_digest=digest(result), transport=proof)
            await cas(db, run, {"checkpoint_context_json": canonical(checkpoint)})
        if refusal is not None: raise refusal
        return result

    async def authenticate(self, adapter):
        await self.request(adapter, "refresh")
        await self.request(adapter, "identity")
        return adapter.identity

    def adapter(self, service, credentials, deadline, **boundary):
        value = CalendarRescheduleAdapter(service=service, credentials=credentials, deadline=deadline,
            contact=self.before, preflight=self.validate, **boundary)
        self.adapters.append(value)
        return value

    def quiescent(self):
        return bool(self.adapters) and all(item.marker.snapshot()["status"] == "verified" for item in self.adapters)

    def closure_receipt(self, run, checkpoint):
        """Only the actual awaited worker can attest its original contact phase."""
        intent = checkpoint.get("intent")
        if (not isinstance(intent, dict) or not self.quiescent()
            or run.lease_owner != self.lease.owner or run.fencing_token != self.lease.fencing_token
            or intent.get("fencing_token") != self.lease.fencing_token):
            return None
        records = checkpoint.get("contacts", [])
        return {"schema_version": 1, "kind": "calendar_reschedule_owned_transport_closure_v1",
            "job_id": run.run_identity, "original_root": run.operator_session_id,
            "owner_principal_id": run.owner_principal_id, "attempt": run.attempt_count,
            "fencing_token": self.lease.fencing_token, "lease_owner": self.lease.owner,
            "intent_digest": digest(intent), "contact_history_digest": digest(records),
            "last_slot": len(records), "closed_revision": run.revision + 1,
            "closed_at": now().isoformat(), "adapters": [
                {"service": item.service, "transport": item.marker.snapshot()} for item in self.adapters]}


async def reserve_private(operator, lease, name, payload):
    # Encrypt before the writer; checkpoint the exact ciphertext before publish.
    path, sha, encrypted = await asyncio.to_thread(prepare_private_draft, lease.job_id+":"+name, payload)
    async with writer() as db:
        run = await get_run(operator, lease.job_id, db=db)
        await authority(db, operator, run, lease=lease, source_required=run.job_kind == SEND_KIND)
        checkpoint = state(run)
        reservations = checkpoint.setdefault("private_artifacts", {})
        if name in reservations:
            fail("artifact_slot_consumed")
        reservations[name] = {"path": path, "digest": sha}
        await cas(db, run, {"checkpoint_context_json": canonical(checkpoint)})
    await asyncio.to_thread(publish_private_draft, path, encrypted)
    actual = await asyncio.to_thread(read_private_draft, path, sha)
    if actual != payload:
        fail("artifact_readback_changed")
    return {"path": path, "digest": sha}


async def terminal(operator, lease, contacts, *, outcome, private_ref=None, verified_connections=()):
    async with writer() as db:
        run = await get_run(operator, lease.job_id, db=db)
        await authority(db, operator, run, lease=lease, source_required=run.job_kind == SEND_KIND)
        checkpoint = state(run)
        if checkpoint.get("cancel_requested") or not contacts.quiescent():
            fail("terminal_quiescence_unavailable")
        checkpoint.update(outcome=outcome, transport_quiescent=True)
        effects = json.loads(run.effect_receipts_json)
        if run.job_kind == SEND_KIND:
            if len(effects) != 1 or not checkpoint.get("contact_may_have_occurred"):
                fail("patch_receipt_unavailable")
            effects[0].update(status="succeeded", details={"verified": True, "no_learning": True})
        values = {"checkpoint_context_json": canonical(checkpoint), "effect_receipts_json": canonical(effects),
            "status": "succeeded", "lease_owner": None, "lease_expires_at": None,
            "finished_at": now(), "result_digest": digest({"outcome": outcome, "private_ref": private_ref}),
            "result_summary": outcome}
        if private_ref:
            values["artifact_receipts_json"] = canonical([{"artifact_type": "calendar_exact_reschedule",
                "file_path": private_ref["path"], "content_sha256": private_ref["digest"],
                "exists": True, "no_learning": True}])
        for expected in verified_connections:
            row = await connection(db, operator, expected)
            row.provider_scopes_json = canonical(sorted(SCOPES[row.service]))
            row.scope_status = "verified"
            row.verified_setup_job_id = lease.job_id
            row.updated_at = now()
        await cas(db, run, values)


async def failed(operator, lease, contacts, *, cause=None):
    """Cleanup adopts no authority, including after the original Root revokes."""
    async with writer() as db:
        run = await durable_job_repository._fetch(db,lease.job_id)
        if (run.job_kind not in {SEND_KIND,IDENTITY_KIND,OBSERVATION_KIND}
            or run.owner_principal_id!=operator.principal.principal_id
            or run.operator_session_id!=operator.session_id or run.status!="running"
            or run.lease_owner!=lease.owner or run.fencing_token!=lease.fencing_token):
            fail("execution_changed")
        checkpoint=state(run)
        contacted=bool(checkpoint.get("intent"))
        quiescent=contacts.quiescent()
        checkpoint["transport_quiescent"]=quiescent
        checkpoint.pop("transport_closure",None)
        closure=contacts.closure_receipt(run,checkpoint)
        if closure is not None: checkpoint["transport_closure"]=closure
        effects=json.loads(run.effect_receipts_json)
        refusal=isinstance(cause,(ConditionalConflict,ProviderRefusal)) and quiescent
        if contacted:
            if len(effects)!=1: fail("effect_changed")
            effects[0]["status"]="failed" if refusal else "unknown"
            if refusal:
                effects[0]["details"]={"definite_provider_refusal":True,"provider_status":412 if isinstance(cause,ConditionalConflict) else cause.provider_status,"no_learning":True}
        checkpoint["outcome"]="precondition_conflict" if isinstance(cause,ConditionalConflict) and refusal else ("provider_refused" if refusal else None)
        cancelled=checkpoint.get("cancel_requested") and quiescent and not contacted
        await cas(db,run,{"checkpoint_context_json":canonical(checkpoint),"effect_receipts_json":canonical(effects),
            "status":"unknown_external_effect" if contacted and not refusal else ("cancelled" if cancelled else "blocked"),
            "failure_reason":"reschedule_readback_unconfirmed" if contacted and not refusal else ("reschedule_precondition_conflict" if isinstance(cause,ConditionalConflict) else "reschedule_preflight_blocked"),
            "lease_owner":None,"lease_expires_at":None},current_goal=False)


def same_account(read, send):
    if any(read[key] != send[key] for key in ("sub", "email", "issuer")):
        fail("account_pair_mismatch")
    return {key: read[key] for key in ("sub", "email", "issuer")}


async def snapshot(operator, ident, *, include_private=False):
    run=await get_run(operator,ident)
    checkpoint=state(run)
    result={"job_id":ident,"kind":run.job_kind,"status":run.status,"revision":run.revision,
        "deadline_at":utc(run.deadline_at).isoformat(),"goal_id":run.goal_id,"goal_revision":run.goal_revision,
        "request_uuid":run.idempotency_key,"source_task_id":arguments(run).get("source",{}).get("task_id"),
        "original_job_id":arguments(run).get("original_job_id"),"outcome":checkpoint.get("outcome"),
        "contact_may_have_occurred":checkpoint.get("contact_may_have_occurred",False),
        "contacts_spent":len(checkpoint.get("contacts",[])),"no_learning":True,"model_used":False,
        "effective_route":"google_calendar_https","failure_reason":run.failure_reason,
        "private_read_available":False,"private_read_reason":"no_private_preview",
        "cancel_requested":bool(checkpoint.get("cancel_requested")),
        "cancel_request_uuid":(checkpoint.get("cancel_request") or {}).get("request_uuid"),
        "transport_quiescent":checkpoint.get("transport_quiescent") is True}
    if checkpoint.get("preview"):
        try:
            async with session() as db:
                await authority(db,operator,await get_run(operator,ident,db=db),source_required=True)
            result["private_read_available"]=True
            result["private_read_reason"]=None
            if include_private:
                ref=checkpoint["private_artifacts"]["preview"]
                payload=await asyncio.to_thread(read_private_draft,ref["path"],ref["digest"])
                frozen=payload["frozen"]; validate_resource(frozen)
                async with session() as db:
                    latest=await get_run(operator,ident,db=db)
                    await authority(db,operator,latest,source_required=True)
                    if state(latest).get("private_artifacts",{}).get("preview")!=ref: fail("private_artifact_changed")
                    row=await db.get(ApprovalRequest,checkpoint["preview"]["approval_id"],populate_existing=True)
                    if row is None or row.owner_principal_id!=run.owner_principal_id or row.operator_session_id!=run.operator_session_id: fail("approval_changed")
                    approval_status,decision_digest=row.status,approval_decision_digest(row)
                source=frozen["source"]; resource=frozen["resource"]
                result["preview"]={"approval_id":row.id,"approval_status":approval_status,"decision_digest":decision_digest,
                    "expires_at":checkpoint["preview"]["expires_at"],"request_digest":frozen["request_digest"],
                    "source_digest":frozen["source_digest"],"account_email":payload["account"]["email"],
                    "calendar_id":frozen["calendar"]["id"],"event_id":source["id"],"title":source.get("summary",""),
                    "old_start":source["start"],"old_end":source["end"],"new_start":resource["start"],"new_end":resource["end"],
                    "old_start_utc":instant(source["start"]).isoformat(),"old_end_utc":instant(source["end"]).isoformat(),
                    "new_start_utc":instant(resource["start"]).isoformat(),"new_end_utc":instant(resource["end"]).isoformat(),
                    "source_etag":frozen["source_etag"],"marker_key":frozen["marker_key"],"marker_value":frozen["marker_value"],
                    "protected_digest":digest(protected(source)),
                    "notification_policy":"sendUpdates=none; provider reminders may still produce messages"}
        except Exception as exc:
            result["private_read_available"]=False
            result["private_read_reason"]=getattr(exc,"code","current_permission_unavailable")
            result.pop("preview",None)
    effects=json.loads(run.effect_receipts_json)
    if len(effects)==1 and effects[0].get("observation_history"):
        result["observations"]=effects[0]["observation_history"]
    return result


_active_workers = {}


async def run_owned(operator, ident, execute_operation):
    """Track only positively created callbacks, never infer restart quiescence."""
    if ident in _active_workers:
        return await snapshot(operator, ident)
    task = asyncio.create_task(execute_operation(), name="calendar-reschedule:"+ident)
    _active_workers[ident] = task
    def completed(done):
        if _active_workers.get(ident) is done:
            _active_workers.pop(ident, None)
    task.add_done_callback(completed)
    try:
        return await asyncio.shield(task)
    finally:
        if task.done() and _active_workers.get(ident) is task:
            _active_workers.pop(ident, None)


async def cancel(operator, ident, *, request_uuid, expected_revision):
    async with writer() as db:
        run = await get_run(operator, ident, db=db)
        checkpoint = state(run)
        receipt = checkpoint.get("cancel_request")
        if receipt is not None:
            if receipt["request_uuid"] != request_uuid:
                fail("cancel_request_conflict")
        else:
            if run.revision != expected_revision or run.status == "succeeded":
                fail("cancel_revision_changed")
            checkpoint["cancel_request"] = {"request_uuid": request_uuid,
                "original_fence": run.fencing_token, "requested_at": now().isoformat()}
            checkpoint["cancel_requested"] = True
            values = {"checkpoint_context_json": canonical(checkpoint)}
            # Paused preview or positively awaited preflight failure cannot
            # resume through this API. Historical closure is a durable proof,
            # not the absence of a local worker after restart.
            if (run.lease_owner is None and run.lease_expires_at is None
                and checkpoint.get("transport_quiescent") is True
                and not checkpoint.get("contact_may_have_occurred")):
                values.update(status="cancelled", finished_at=now())
            await cas(db, run, values, current_goal=False)
    task = _active_workers.get(ident)
    if task is not None and not task.done():
        task.cancel()
        # Await actual provider/client close and writer completion. Deadline
        # does not turn an unclosed callback into a successful cancellation.
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10)
        except asyncio.CancelledError:
            pass
        except (TimeoutError, Exception):
            pass
        finally:
            if task.done() and _active_workers.get(ident) is task:
                _active_workers.pop(ident, None)
    return await snapshot(operator, ident)


async def staged_selection(operator,binding_id,expected_revision=None):
    async with session() as db:
        await current_root(db,operator)
        selected=await selection_snapshot(db,operator,binding_id,expected_revision=expected_revision)
        binding=await db.get(CalendarEventBinding,binding_id)
        encrypted=(binding.calendar_id_private,binding.provider_event_id_private)
    calendar_id,event_id=[provider_id(decrypt(value),maximum) for value,maximum in zip(encrypted,(1024,512))]
    return selected,calendar_id,event_id


async def verify_pair(operator, *, request_uuid,goal_id,goal_revision,read_connection_id,
    send_connection_id,event_binding_id,expected_event_binding_revision,priority=60,request_binding=None,**boundary):
    connections=await pair_snapshots(operator,read_connection_id,send_connection_id)
    selected,calendar_id,_=await staged_selection(operator,event_binding_id,expected_event_binding_revision)
    if (selected["goal_id"],selected["goal_revision"])!=(goal_id,goal_revision): fail("selection_goal_changed")
    admitted=await admit(operator,kind=IDENTITY_KIND,request_uuid=request_uuid,goal_id=goal_id,goal_revision=goal_revision,
        priority=priority,inputs={"schema_version":1,"connections":connections,"selection":selected,"max_contacts":6,
            "acknowledge_identity_and_selected_calendar_read":True,"operator_request_digest":request_binding,"no_learning":True})
    if admitted.get("receipt",{}).get("status")=="deduped": return await snapshot(operator,admitted["job_id"])
    lease=await claim(operator,admitted); contacts=Contacts(operator,lease,source_required=False)
    try:
        run=await get_run(operator,lease.job_id); credentials=await stage_credentials(operator,run)
        async with asyncio.timeout(max(0,(utc(run.deadline_at)-now()).total_seconds())):
            read=contacts.adapter(READ_SERVICE,credentials[READ_SERVICE],utc(run.deadline_at),**boundary)
            write=contacts.adapter(SEND_SERVICE,credentials[SEND_SERVICE],utc(run.deadline_at),**boundary)
            account=same_account(await contacts.authenticate(read),await contacts.authenticate(write))
            await contacts.request(read,"calendar",calendar_id=calendar_id)
            await contacts.request(write,"calendar",calendar_id=calendar_id)
            if read.calendar_verified!=write.calendar_verified: fail("calendar_proof_changed")
            ref=await reserve_private(operator,lease,"identity",{"account":account,"calendar":read.calendar_verified,
                "selection":selected,"read_scope":read.scope_evidence,"write_scope":write.scope_evidence})
            await terminal(operator,lease,contacts,outcome="verified_calendar_identity",private_ref=ref,verified_connections=connections)
    except BaseException as exc:
        await failed(operator,lease,contacts,cause=exc); raise
    return await snapshot(operator,lease.job_id)


async def paired_proof(operator,connections,selection):
    async with session() as db:
        await current_root(db,operator); ids=[]
        for expected in connections:
            row=await connection(db,operator,expected)
            if row.scope_status!="verified" or not row.verified_setup_job_id: fail("pair_verification_required")
            ids.append(row.verified_setup_job_id)
        if len(set(ids))!=1: fail("pair_verification_changed")
        run=await get_run(operator,ids[0],db=db)
        if run.job_kind!=IDENTITY_KIND or run.status!="succeeded" or arguments(run)["connections"]!=connections or arguments(run)["selection"]!=selection: fail("pair_verification_changed")
        ref=state(run)["private_artifacts"]["identity"]
        if json.loads(run.artifact_receipts_json)!=[{"artifact_type":"calendar_exact_reschedule","file_path":ref["path"],"content_sha256":ref["digest"],"exists":True,"no_learning":True}]: fail("identity_readback_unavailable")
        metadata={"job_id":run.run_identity,"revision":run.revision,"artifact":ref}
    actual=await asyncio.to_thread(read_private_draft,ref["path"],ref["digest"])
    if actual["selection"]!=selection: fail("identity_readback_changed")
    return actual,metadata


def consent_metadata(row):
    return {"consent_id":row.consent_id,"revision":row.revision,"state":row.state,"expires_at":utc(row.expires_at).isoformat(),
        "goal_id":row.goal_id,"goal_revision":row.goal_revision,"event_binding_id":row.event_binding_id,
        "event_binding_revision":row.event_binding_revision,"read_connection_id":row.read_connection_id,
        "read_connection_revision":row.read_connection_revision,"write_connection_id":row.write_connection_id,
        "write_connection_revision":row.write_connection_revision,"provider_contact":False}


async def create_consent(operator, *, body):
    request_digest=digest(body.model_dump(mode="json"))
    async with session() as db:
        await current_root(db,operator)
        existing=(await db.execute(select(CalendarRescheduleConsent).where(CalendarRescheduleConsent.owner_principal_id==operator.principal.principal_id,
            CalendarRescheduleConsent.original_root_session_id==operator.session_id,CalendarRescheduleConsent.creation_request_uuid==str(body.request_uuid)))).scalar_one_or_none()
        if existing is not None:
            if existing.creation_request_digest!=request_digest: fail("consent_request_conflict")
            return consent_metadata(existing)
    selected,calendar_id,_=await staged_selection(operator,body.event_binding_id,body.expected_event_binding_revision)
    if (selected["goal_id"],selected["goal_revision"])!=(body.goal_id,body.goal_revision): fail("selection_goal_changed")
    connections=await pair_snapshots(operator,body.read_connection_id,body.write_connection_id)
    if (connections[0]["revision"],connections[1]["revision"])!=(body.expected_read_revision,body.expected_write_revision): fail("connection_changed")
    proof,verification=await paired_proof(operator,connections,selected)
    if proof["calendar"]["id"]!=calendar_id: fail("calendar_proof_changed")
    encrypted=await asyncio.to_thread(encrypt,calendar_id)
    async with writer() as db:
        root=await current_root(db,operator)
        from src.workflows.job_runtime import _assert_canonical_goal_fence
        goal=await _assert_canonical_goal_fence(db,goal_id=body.goal_id,goal_revision=body.goal_revision,owner_kind="user",owner_principal_id=operator.principal.principal_id,session_id=operator.session_id)
        grant=finite_goal_grant(goal)
        expires=min(utc(body.expires_at),now()+timedelta(minutes=5),utc(root.idle_expires_at),utc(root.absolute_expires_at),utc(grant.period_expires_at))
        if expires<=now(): fail("consent_expired")
        for expected in connections: await connection(db,operator,expected)
        if await selection_snapshot(db,operator,body.event_binding_id)!=selected: fail("selected_event_changed")
        verified=await get_run(operator,verification["job_id"],db=db)
        if verified.revision!=verification["revision"] or state(verified)["private_artifacts"]["identity"]!=verification["artifact"] or verified.status!="succeeded": fail("pair_verification_changed")
        prior=(await db.execute(select(CalendarRescheduleConsent).where(CalendarRescheduleConsent.owner_principal_id==operator.principal.principal_id,
            CalendarRescheduleConsent.original_root_session_id==operator.session_id,CalendarRescheduleConsent.creation_request_uuid==str(body.request_uuid)))).scalar_one_or_none()
        if prior is not None:
            if prior.creation_request_digest!=request_digest: fail("consent_request_conflict")
            return consent_metadata(prior)
        active=await db.scalar(select(CalendarRescheduleConsent.consent_id).where(CalendarRescheduleConsent.owner_principal_id==operator.principal.principal_id,
            CalendarRescheduleConsent.original_root_session_id==operator.session_id,CalendarRescheduleConsent.event_binding_id==body.event_binding_id,CalendarRescheduleConsent.state=="active"))
        if active is not None: fail("revoke_existing_consent_before_regrant")
        row=CalendarRescheduleConsent(owner_principal_id=operator.principal.principal_id,original_root_session_id=operator.session_id,
            goal_id=body.goal_id,goal_revision=body.goal_revision,read_connection_id=connections[0]["connection_id"],read_connection_revision=connections[0]["revision"],
            write_connection_id=connections[1]["connection_id"],write_connection_revision=connections[1]["revision"],profile_binding_digest=digest(connections),
            account_identity_digest=digest(proof["account"]),selected_calendar_id_private=encrypted,selected_calendar_digest=digest(proof["calendar"]),
            event_binding_id=selected["event_binding_id"],event_binding_revision=selected["event_binding_revision"],event_identity_digest=selected["provider_identity_digest"],
            owned_event_read_allowed=True,calendar_list_metadata_read_allowed=True,one_conditional_reschedule_allowed=True,expires_at=expires,
            creation_request_uuid=str(body.request_uuid),creation_request_digest=request_digest,consent_digest="")
        row.consent_digest=digest({"schema_version":1,"owner":row.owner_principal_id,"root":row.original_root_session_id,"goal":[row.goal_id,row.goal_revision],
            "profiles":row.profile_binding_digest,"account":row.account_identity_digest,"calendar":row.selected_calendar_digest,"encrypted_calendar":digest(encrypted.encode()),
            "event":[row.event_binding_id,row.event_binding_revision,row.event_identity_digest],"expires":expires.isoformat(),"request":request_digest})
        db.add(row); await db.flush(); return consent_metadata(row)


async def revoke_consent(operator, ident, *, request_uuid,expected_revision):
    from sqlalchemy import update
    request_digest=digest({"consent_id":ident,"request_uuid":request_uuid,"expected_revision":expected_revision})
    async with writer() as db:
        await current_root(db,operator)
        row=await db.get(CalendarRescheduleConsent,ident,populate_existing=True)
        if row is None or row.owner_principal_id!=operator.principal.principal_id or row.original_root_session_id!=operator.session_id: fail("consent_unavailable")
        if row.revocation_request_uuid is not None:
            if row.revocation_request_uuid!=request_uuid or row.revocation_request_digest!=request_digest: fail("consent_revoke_conflict")
            return consent_metadata(row)
        if row.revision!=expected_revision: fail("consent_revision_changed")
        result=await db.execute(update(CalendarRescheduleConsent).where(CalendarRescheduleConsent.consent_id==ident,CalendarRescheduleConsent.revision==expected_revision,
            CalendarRescheduleConsent.state=="active").values(state="revoked",revision=expected_revision+1,revocation_request_uuid=request_uuid,revocation_request_digest=request_digest,updated_at=now()).execution_options(synchronize_session=False))
        if result.rowcount!=1: fail("consent_revision_changed")
        return consent_metadata(await db.get(CalendarRescheduleConsent,ident,populate_existing=True))


async def stage_source(operator,task_id):
    from src.work_board.contracts import WorkBoardOwner
    from src.work_board.input_artifacts import resolve_input_artifact_for_copy
    async with session() as db:
        await current_root(db,operator)
        task=(await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id==task_id))).scalar_one_or_none()
        if task is None or task.owner_principal_id!=operator.principal.principal_id or task.owner_session_id!=operator.session_id: fail("task_unavailable")
        resolved=await resolve_input_artifact_for_copy(db,WorkBoardOwner(principal_id=operator.principal.principal_id,session_id=operator.session_id),
            typed_input_ref=task.typed_input_ref,typed_input_digest=task.typed_input_digest,capability_id=task.capability_id,goal_id=task.goal_id,goal_revision=task.goal_revision)
        typed=resolved.input
        row=await db.get(CalendarRescheduleConsent,typed["consent_id"],populate_existing=True)
        if row is None or row.revision!=typed["expected_consent_revision"] or row.event_binding_id!=typed["event_binding_id"] or row.event_binding_revision!=typed["expected_event_binding_revision"]: fail("write_consent_changed")
        source=await source_snapshot(db,operator,task_id,binding_hint={"consent":consent_snapshot(row)})
    proposed_times(typed["new_start"],typed["new_end"])
    return source,typed


async def actual_source(operator,run,read,contacts):
    expected=arguments(run)["source"]
    async with session() as db:
        await authority(db,operator,await get_run(operator,run.run_identity,db=db),source_required=True)
        binding=await db.get(CalendarEventBinding,expected["event_binding_id"])
        ciphertexts=(binding.calendar_id_private,binding.provider_event_id_private)
        selection_consent=await db.get(CalendarReadConsent,binding.consent_id)
        allowed_fields=set(json.loads(selection_consent.allowed_fields_json))
    calendar_id,event_id=[provider_id(decrypt(value),maximum) for value,maximum in zip(ciphertexts,(1024,512))]
    await contacts.request(read,"calendar",calendar_id=calendar_id)
    calendar_observed_at=now().timestamp()
    raw=await contacts.request(read,"event",calendar_id=calendar_id,event_id=event_id)
    event(raw,calendar_id=calendar_id,event_id=event_id,account=read.identity)
    if digest(read.calendar_verified)!=expected["consent"]["selected_calendar_digest"] or digest({key:read.identity[key] for key in ("sub","email","issuer")})!=expected["consent"]["account_identity_digest"]: fail("account_or_calendar_changed")
    from src.integrations.google_calendar import _selected_event,event_revision
    if event_revision(_selected_event(raw,allowed_fields=allowed_fields))!=expected["event_revision"]: fail("selected_event_revision_changed")
    return raw,read.calendar_verified,calendar_observed_at


async def preview(operator, *, task_id,read_connection_id,send_connection_id,request_uuid,
    priority=60,request_binding=None,**boundary):
    connections=await pair_snapshots(operator,read_connection_id,send_connection_id)
    original,typed=await stage_source(operator,task_id)
    async with session() as db:
        await consent(db,operator,original["consent"],connections=connections)
        selected=await selection_snapshot(db,operator,original["event_binding_id"])
    proof,_=await paired_proof(operator,connections,selected)
    account=proof["account"]
    inputs={"schema_version":1,"connections":connections,"source":original,"account_digest":digest(account),
        "max_contacts":13,"preview_contact_limit":4,"execution_contact_limit":9,
        "operator_request_digest":request_binding,"no_learning":True}
    admitted=await admit(operator,kind=SEND_KIND,request_uuid=request_uuid,goal_id=original["goal_id"],goal_revision=original["goal_revision"],inputs=inputs,priority=priority)
    if admitted.get("receipt",{}).get("status")=="deduped": return await snapshot(operator,admitted["job_id"],include_private=True)
    lease=await claim(operator,admitted); contacts=Contacts(operator,lease)
    try:
        run=await get_run(operator,lease.job_id); credentials=await stage_credentials(operator,run)
        async with asyncio.timeout(max(0,(utc(run.deadline_at)-now()).total_seconds())):
            read=contacts.adapter(READ_SERVICE,credentials[READ_SERVICE],utc(run.deadline_at),**boundary)
            identity=await contacts.authenticate(read)
            if {key:identity[key] for key in account}!=account: fail("account_changed")
            raw,calendar,_=await actual_source(operator,run,read,contacts)
            frozen=freeze(raw,calendar=calendar,account=account,new_start=typed["new_start"],new_end=typed["new_end"])
            new_start_epoch=instant(frozen["resource"]["start"]).timestamp()
            ref=await reserve_private(operator,lease,"preview",{"frozen":frozen,"account":account})
            run=await get_run(operator,lease.job_id)
            expires=min(utc(run.deadline_at).timestamp(),now().timestamp()+300)
            approval_id=str(uuid.uuid5(uuid.NAMESPACE_URL,lease.job_id+":exact-approval"))
            scope={"schema_version":1,"job_id":lease.job_id,"source":original,"account_digest":digest(account),"connections":connections,
                "source_etag_digest":digest(frozen["source_etag"]),"request_digest":frozen["request_digest"],"source_digest":frozen["source_digest"],
                "goal_id":run.goal_id,"goal_revision":run.goal_revision,"authority_digest":run.authority_digest,"budget_digest":run.budget_digest,"expires_at":expires}
            fingerprint=fingerprint_tool_call("calendar.event.reschedule.v1",{"scope_digest":digest(scope)})
            details={"approval_scope":scope,"scope_digest":digest(scope),"session_id":operator.session_id,"conversation_id":operator.session_id,
                "operator_session_id":operator.session_id,"owner_principal_id":run.owner_principal_id,"approval_operator_principal_id":run.owner_principal_id,
                "durable_job_id":lease.job_id,"durable_owner_kind":"user","durable_owner_principal_id":run.owner_principal_id,"durable_service_id":None,
                "durable_authority_digest":run.authority_digest,"durable_goal_id":run.goal_id,"durable_goal_revision":run.goal_revision,"durable_plan_revision":None,
                "durable_capability_version":VERSION,"durable_budget_digest":run.budget_digest,"durable_approval_id":approval_id,
                "approval_expires_at":expires,"expires_at":expires,"attachment_refs":[]}
            row=await approval_repository.get_or_create_pending(session_id=operator.session_id,tool_name="calendar.event.reschedule.v1",risk_level="high",
                summary="Reschedule this one owned event with exact times and source version",fingerprint=fingerprint,details=details,request_id=approval_id)
            if row.id!=approval_id: fail("approval_changed")
            async with writer() as db:
                run=await get_run(operator,lease.job_id,db=db); await authority(db,operator,run,lease=lease)
                checkpoint=state(run)
                if checkpoint.get("preview") or len(checkpoint.get("contacts",[]))!=4 or not contacts.quiescent(): fail("preview_phase_changed")
                checkpoint["preview"]={"approval_id":row.id,"approval_fingerprint":fingerprint,"expires_at":expires,
                    "source_etag_digest":digest(frozen["source_etag"]),"request_digest":frozen["request_digest"],"source_digest":frozen["source_digest"],
                    "new_start_epoch":new_start_epoch,"scope_digest":digest(scope)}
                checkpoint["transport_quiescent"]=True
                await cas(db,run,{"checkpoint_context_json":canonical(checkpoint),"status":"paused","lease_owner":None,"lease_expires_at":None})
    except BaseException as exc:
        await failed(operator,lease,contacts,cause=exc); raise
    return await snapshot(operator,lease.job_id,include_private=True)


async def decide(operator,ident, *, decision,expected_digest):
    async with writer() as db:
        run=await get_run(operator,ident,db=db); await authority(db,operator,run)
        pending=state(run).get("preview")
        if run.status!="paused" or not isinstance(pending,dict): fail("approval_phase_changed")
        row=await db.get(ApprovalRequest,pending["approval_id"],populate_existing=True)
        if row is None or row.owner_principal_id!=run.owner_principal_id or row.operator_session_id!=run.operator_session_id: fail("approval_changed")
        await approval_repository.resolve_exact_in_session(db,row.id,decision,expected_digest=expected_digest)
    return await snapshot(operator,ident,include_private=True)


async def execute(operator,ident,**boundary):
    original=await get_run(operator,ident)
    if original.status!="paused" or state(original).get("intent"): return await snapshot(operator,ident)
    async with session() as db:
        await authority(db,operator,await get_run(operator,ident,db=db))
        approved=await db.get(ApprovalRequest,state(original)["preview"]["approval_id"])
        if approved is None or approved.status!="approved": fail("approval_required")
    ref=state(original)["private_artifacts"]["preview"]
    private=await asyncio.to_thread(read_private_draft,ref["path"],ref["digest"])
    frozen=private["frozen"]; validate_resource(frozen)
    staged,typed=await stage_source(operator,arguments(original)["source"]["task_id"])
    if staged!=arguments(original)["source"] or typed["new_start"]!=frozen["resource"]["start"] or typed["new_end"]!=frozen["resource"]["end"]: fail("task_input_changed")
    proposed_times(frozen["resource"]["start"],frozen["resource"]["end"])
    lease=await claim(operator,_serialize(original),continuation=True); contacts=Contacts(operator,lease)
    try:
        run=await get_run(operator,ident); credentials=await stage_credentials(operator,run)
        async with asyncio.timeout(max(0,(utc(run.deadline_at)-now()).total_seconds())):
            read=contacts.adapter(READ_SERVICE,credentials[READ_SERVICE],utc(run.deadline_at),**boundary)
            write=contacts.adapter(SEND_SERVICE,credentials[SEND_SERVICE],utc(run.deadline_at),**boundary)
            account=same_account(await contacts.authenticate(read),await contacts.authenticate(write))
            if account!=private["account"] or digest(account)!=arguments(run)["account_digest"]: fail("account_changed")
            raw,calendar,observed_at=await actual_source(operator,run,read,contacts)
            await contacts.request(write,"calendar",calendar_id=calendar["id"])
            if write.calendar_verified!=calendar or calendar!=frozen["calendar"] or raw!=frozen["source"]: fail("source_changed_since_preview")
            refreshed=await get_run(operator,ident)
            intent={"schema_version":1,"job_id":ident,"effect_id":ident+":patch-once","input_digest":refreshed.input_digest,
                "authority_digest":refreshed.authority_digest,"budget_digest":refreshed.budget_digest,"original_root":operator.session_id,
                "owner_principal_id":refreshed.owner_principal_id,"goal_id":refreshed.goal_id,"goal_revision":refreshed.goal_revision,
                "attempt":refreshed.attempt_count,"fencing_token":lease.fencing_token,"approval_id":state(refreshed)["preview"]["approval_id"],
                "source_etag_digest":digest(frozen["source_etag"]),"request_digest":frozen["request_digest"],"source_digest":frozen["source_digest"],
                "source_observed_at":observed_at,"new_start_epoch":instant(frozen["resource"]["start"]).timestamp(),"account_digest":digest(account),"preview_artifact":ref}
            async with writer() as db:
                run=await get_run(operator,ident,db=db)
                await claim_intent(db,operator,run,lease=lease,intent=intent,approval_id=intent["approval_id"])
            await contacts.request(write,"patch",calendar_id=calendar["id"],event_id=raw["id"],resource=validate_resource(frozen),etag=frozen["source_etag"])
            # PATCH response is intentionally not outcome proof. A separate
            # readonly token and exact-ID GET verify the whole protected state.
            current=await contacts.request(read,"event",calendar_id=calendar["id"],event_id=raw["id"])
            observed=reschedule_readback(current,frozen)
            output=await reserve_private(operator,lease,"rescheduled",{"observation":observed,"event":current,"intent_digest":digest(intent)})
            await terminal(operator,lease,contacts,outcome="verified_reschedule",private_ref=output)
    except BaseException as exc:
        await failed(operator,lease,contacts,cause=exc)
        if isinstance(exc,(ConditionalConflict,ProviderRefusal)): return await snapshot(operator,ident)
        raise
    return await snapshot(operator,ident,include_private=True)


async def recovery_original(db,operator,ident,expected_revision=None):
    original=await get_run(operator,ident,db=db)
    await assert_original_recovery(db,operator,original,expected_revision=expected_revision)
    return original


async def observe(operator, *, original_job_id,expected_original_revision,read_connection_id,
    goal_id,goal_revision,request_uuid,priority=60,request_binding=None,**boundary):
    async with session() as db:
        original=await recovery_original(db,operator,original_job_id,expected_original_revision)
        intent=state(original)["intent"]; ref=state(original)["private_artifacts"]["preview"]
        original_revision=original.revision
    connections=await pair_snapshots(operator,read_connection_id)
    # A recovery grant cannot silently adopt a different credential/profile.
    if connections!=[arguments(original)["connections"][0]]: fail("readonly_recovery_profile_changed")
    private=await asyncio.to_thread(read_private_draft,ref["path"],ref["digest"])
    frozen=private["frozen"]; validate_resource(frozen)
    if digest(frozen["source_etag"])!=intent["source_etag_digest"] or frozen["request_digest"]!=intent["request_digest"] or frozen["source_digest"]!=intent["source_digest"]: fail("original_artifact_changed")
    inputs={"schema_version":1,"connections":connections,"max_contacts":4,"original_job_id":original_job_id,"original_revision":original_revision,
        "original_intent_digest":digest(intent),"original_effect_id":intent["effect_id"],"account_digest":intent["account_digest"],
        "acknowledge_readonly_recovery":True,"operator_request_digest":request_binding,"no_learning":True}
    admitted=await admit(operator,kind=OBSERVATION_KIND,request_uuid=request_uuid,goal_id=goal_id,goal_revision=goal_revision,inputs=inputs,priority=priority)
    if admitted.get("receipt",{}).get("status")=="deduped": return await snapshot(operator,admitted["job_id"])
    lease=await claim(operator,admitted); contacts=Contacts(operator,lease,source_required=False)
    try:
        run=await get_run(operator,lease.job_id); credentials=await stage_credentials(operator,run)
        async with asyncio.timeout(max(0,(utc(run.deadline_at)-now()).total_seconds())):
            read=contacts.adapter(READ_SERVICE,credentials[READ_SERVICE],utc(run.deadline_at),**boundary)
            identity=await contacts.authenticate(read)
            if {key:identity[key] for key in private["account"]}!=private["account"]: fail("recovery_account_changed")
            await contacts.request(read,"calendar",calendar_id=frozen["calendar"]["id"])
            if read.calendar_verified!=frozen["calendar"]: fail("recovery_calendar_changed")
            try:
                raw=await contacts.request(read,"event",calendar_id=frozen["calendar"]["id"],event_id=frozen["source"]["id"])
                try: observation=reschedule_readback(raw,frozen)
                except Exception: observation={"outcome":"unknown_observation","response_digest":digest(raw),"no_learning":True}
            except ProviderRefusal as exc:
                if exc.provider_status not in {404,410}: raise
                observation={"outcome":"unknown_observation","response_digest":digest({"provider_status":exc.provider_status}),"no_learning":True}
            output=await reserve_private(operator,lease,"observation",{"observation":observation,"original_intent_digest":digest(intent)})
            observation["private_artifact"]=output
            if not contacts.quiescent(): fail("transport_unsettled")
            async with writer() as db:
                auxiliary=await get_run(operator,lease.job_id,db=db)
                current=await recovery_original(db,operator,original_job_id,original_revision)
                await append_observation(db,operator,current,auxiliary,lease=lease,original_revision=original_revision,
                    original_intent_digest=digest(intent),observation=observation)
    except BaseException as exc:
        await failed(operator,lease,contacts,cause=exc); raise
    return await snapshot(operator,lease.job_id)
