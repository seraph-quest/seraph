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
from src.db.models import ApprovalRequest, GoogleServiceConnection, MailMessageBinding, WorkBoardTask
from src.integrations.gmail_send import (GmailReplyAdapter, READ_SERVICE, SEND_SERVICE,
    SCOPES, source, freeze_mime, validate_resource, sent_readback, provider_id, digest, fail)
from src.integrations.mail_reply_send import (SEND_KIND, IDENTITY_KIND, OBSERVATION_KIND,
    VERSION, ReplyLease, arguments, authority, cas, canonical, claim_intent, connection,
    connection_snapshot, current_root, mark_dispatch, source_snapshot, state, utc,
    append_observation)
from src.vault import vault_repository, decrypt
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
    return "mail-reply:" + uuid.uuid5(uuid.NAMESPACE_URL, seed).hex


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
            await connection(db, operator, expected)
            result.append(expected)
        return result


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
    declared = {"schema_version": 1, "capability_id": "mail.reply.send" if kind == SEND_KIND else "mail.reply.observe",
        "capability_version": VERSION, "owner_kind": "user",
        "principal": operator.principal.principal_id, "session_id": operator.session_id,
        "operator_session_id": operator.session_id, "goal_id": goal_id, "goal_revision": goal_revision,
        "finite_authority": True, "credential_egress": True,
        "external_mutation": kind == SEND_KIND, "no_learning": True,
        "runtime_cap_seconds": 120, "max_contacts": inputs["max_contacts"],
        "max_send_posts": 1 if kind == SEND_KIND else 0, "budget_microusd": 0}
    spec = DurableJobSpec(identity=DurableJobIdentity(job_id=ident, owner_kind="user",
        owner_principal_id=operator.principal.principal_id, job_kind=kind,
        capability_version=VERSION, idempotency_scope=kind, idempotency_key=request_uuid),
        inputs=inputs, session_id=operator.session_id, conversation_id=operator.session_id,
        operator_session_id=operator.session_id, goal_id=goal_id, goal_revision=goal_revision,
        priority=priority, resource_claims=("mail-read",), declared_authority=declared,
        deadline_at=now()+timedelta(seconds=120), max_attempts=1, max_outstanding_jobs=1,
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
    owner = "mail-reply-worker:" + uuid.uuid4().hex
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
        raw = await vault_repository.get(expected["vault_secret_key"],
            owner_principal_id=operator.principal.principal_id)
        if not raw:
            fail("credential_unavailable")
        try:
            value = json.loads(raw)
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
            if operation == "send":
                await mark_dispatch(db, self.operator, run, lease=self.lease,
                    intent_digest=digest(checkpoint["intent"]))
                # Re-read after dispatch CAS before the same writer's contact reservation.
                run = await get_run(self.operator, self.lease.job_id, db=db)
                checkpoint = state(run)
                records = checkpoint.setdefault("contacts", [])
            records.append({"slot": len(records)+1, "operation": operation, "service": service,
                "status": "reserved", "fence": self.lease.fencing_token, "reserved_at": now().isoformat()})
            await cas(db, run, {"checkpoint_context_json": canonical(checkpoint)})

    async def request(self, adapter, operation, **kwargs):
        result = await adapter.request(operation, **kwargs)
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
        return result

    async def authenticate(self, adapter):
        await self.request(adapter, "refresh")
        await self.request(adapter, "identity")
        if adapter.service == READ_SERVICE:
            profile = await self.request(adapter, "profile")
            if profile.get("emailAddress") != adapter.identity["email"]:
                fail("mailbox_changed")
            adapter.mailbox_verified = True
        return adapter.identity

    def adapter(self, service, credentials, deadline, **boundary):
        value = GmailReplyAdapter(service=service, credentials=credentials, deadline=deadline,
            contact=self.before, **boundary)
        self.adapters.append(value)
        return value

    def quiescent(self):
        return bool(self.adapters) and all(item.marker.snapshot()["status"] == "verified" for item in self.adapters)


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


async def terminal(operator, lease, contacts, *, outcome, private_ref=None):
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
                fail("send_receipt_unavailable")
            effects[0].update(status="succeeded", details={"verified": True, "no_learning": True})
        values = {"checkpoint_context_json": canonical(checkpoint), "effect_receipts_json": canonical(effects),
            "status": "succeeded", "lease_owner": None, "lease_expires_at": None,
            "finished_at": now(), "result_digest": digest({"outcome": outcome, "private_ref": private_ref}),
            "result_summary": outcome}
        if private_ref:
            values["artifact_receipts_json"] = canonical([{"artifact_type": "mail_exact_reply",
                "file_path": private_ref["path"], "content_sha256": private_ref["digest"],
                "exists": True, "no_learning": True}])
        await cas(db, run, values)


async def failed(operator, lease, contacts):
    """Awaited worker failure may attest closure, never successful send/readback."""
    async with writer() as db:
        run = await get_run(operator, lease.job_id, db=db)
        # Current Root is still required by get_run. Expired Goal can block
        # adoption but cannot erase an already recorded contact liability.
        # An awaited worker can close its OWN expired lease after timeout.
        # Expiry alone is not quiescence and cannot authorize adoption.
        if (run.status != "running" or run.lease_owner != lease.owner
            or run.fencing_token != lease.fencing_token):
            fail("execution_changed")
        checkpoint = state(run)
        contacted = checkpoint.get("contact_may_have_occurred") is True
        checkpoint["transport_quiescent"] = contacts.quiescent()
        await cas(db, run, {"checkpoint_context_json": canonical(checkpoint),
            "status": "unknown_external_effect" if contacted else "blocked",
            "failure_reason": "reply_readback_unconfirmed" if contacted else "reply_preflight_blocked",
            "lease_owner": None, "lease_expires_at": None}, current_goal=False)


def same_account(read, send):
    if any(read[key] != send[key] for key in ("sub", "email", "issuer")):
        fail("account_pair_mismatch")
    return {key: read[key] for key in ("sub", "email", "issuer")}


async def snapshot(operator, ident):
    run = await get_run(operator, ident)
    checkpoint = state(run)
    result = {"job_id": ident, "kind": run.job_kind, "status": run.status,
        "revision": run.revision, "deadline_at": utc(run.deadline_at).isoformat(),
        "goal_id": run.goal_id, "goal_revision": run.goal_revision,
        "outcome": checkpoint.get("outcome"), "contact_may_have_occurred": checkpoint.get("contact_may_have_occurred", False),
        "contacts_spent": len(checkpoint.get("contacts", [])), "no_learning": True,
        "failure_reason": run.failure_reason}
    if checkpoint.get("preview"):
        result["preview"] = {key: item for key, item in checkpoint["preview"].items()
            if key in {"approval_id", "approval_fingerprint", "expires_at", "mime_digest", "request_digest"}}
        ref = checkpoint["private_artifacts"]["preview"]
        payload = await asyncio.to_thread(read_private_draft, ref["path"], ref["digest"])
        # This is the private, authenticated Mail UI only. None of this
        # plaintext is returned by generic job/checkpoint projections.
        result["preview"].update(sender=payload["account"]["email"], recipient=payload["frozen"]["expected"]["to"],
            subject=payload["frozen"]["expected"]["subject"], body=payload["frozen"]["expected"]["body"],
            reply_to_untrusted=True, recipient_delivery_proven=False)
    return result


async def verify_pair(operator, *, request_uuid, goal_id, goal_revision,
    read_connection_id, send_connection_id, priority=60, **boundary):
    connections = await pair_snapshots(operator, read_connection_id, send_connection_id)
    admitted = await admit(operator, kind=IDENTITY_KIND, request_uuid=request_uuid,
        goal_id=goal_id, goal_revision=goal_revision, priority=priority,
        inputs={"schema_version": 1, "connections": connections, "max_contacts": 5,
            "acknowledge_identity_read": True, "no_learning": True})
    if admitted.get("receipt", {}).get("status") == "deduped":
        return await snapshot(operator, admitted["job_id"])
    lease = await claim(operator, admitted)
    contacts = Contacts(operator, lease, source_required=False)
    try:
        run = await get_run(operator, lease.job_id)
        credentials = await stage_credentials(operator, run)
        async with asyncio.timeout(max(0, (utc(run.deadline_at)-now()).total_seconds())):
            read = contacts.adapter(READ_SERVICE, credentials[READ_SERVICE], utc(run.deadline_at), **boundary)
            send = contacts.adapter(SEND_SERVICE, credentials[SEND_SERVICE], utc(run.deadline_at), **boundary)
            account = same_account(await contacts.authenticate(read), await contacts.authenticate(send))
            ref = await reserve_private(operator, lease, "identity", {"account": account,
                "read_scope": read.scope_evidence, "send_scope": send.scope_evidence})
            await terminal(operator, lease, contacts, outcome="verified_reply_identity", private_ref=ref)
        async with writer() as db:
            current = await get_run(operator, lease.job_id, db=db)
            await authority(db, operator, current, source_required=False)
            if current.status != "succeeded":
                fail("identity_readback_unavailable")
            for expected in connections:
                row = await connection(db, operator, expected)
                row.provider_scopes_json = canonical(sorted(SCOPES[row.service]))
                row.scope_status = "verified"
                row.verified_setup_job_id = lease.job_id
                row.updated_at = now()
    except BaseException:
        await failed(operator, lease, contacts)
        raise
    return await snapshot(operator, lease.job_id)


async def paired_account(operator, connections):
    """Read back the genuine private paired-identity artifact before staging."""
    async with session() as db:
        await current_root(db, operator)
        ids = []
        for expected in connections:
            row = await connection(db, operator, expected)
            if row.scope_status != "verified" or not row.verified_setup_job_id:
                fail("pair_verification_required")
            ids.append(row.verified_setup_job_id)
        if len(set(ids)) != 1:
            fail("pair_verification_changed")
        verification = await get_run(operator, ids[0], db=db)
        if verification.job_kind != IDENTITY_KIND or verification.status != "succeeded":
            fail("pair_verification_required")
        if arguments(verification)["connections"] != connections:
            fail("pair_verification_changed")
        ref = state(verification)["private_artifacts"]["identity"]
        if json.loads(verification.artifact_receipts_json) != [{"artifact_type": "mail_exact_reply",
            "file_path": ref["path"], "content_sha256": ref["digest"], "exists": True, "no_learning": True}]:
            fail("identity_readback_unavailable")
    actual = await asyncio.to_thread(read_private_draft, ref["path"], ref["digest"])
    return actual["account"]


async def actual_source(operator, run, read, contacts):
    expected = arguments(run)["source"]
    draft = await asyncio.to_thread(read_private_draft, expected["artifact_path"], expected["artifact_digest"])
    if draft.get("message_revision") != expected["message_revision"] or draft.get("memory_status") != "no_learning":
        fail("draft_artifact_changed")
    async with session() as db:
        await authority(db, operator, await get_run(operator, run.run_identity, db=db), source_required=True)
        binding = await db.get(MailMessageBinding, expected["message_binding_id"])
        ciphertexts = binding.provider_message_id_ciphertext, binding.provider_thread_id_ciphertext
    # Provider identifiers and body remain private encrypted staging data.
    message_id, thread_id = [provider_id(decrypt(item)) for item in ciphertexts]
    message = await contacts.request(read, "raw", provider_id_value=message_id)
    thread = await contacts.request(read, "thread", provider_id_value=thread_id)
    facts = source(message, thread)
    if facts["thread_id"] != thread_id or facts["provider_message_id"] != message_id:
        fail("provider_source_changed")
    return facts, draft


async def stage_source(operator, task_id):
    from src.work_board.contracts import WorkBoardOwner
    from src.work_board.input_artifacts import resolve_input_artifact_for_copy
    async with session() as db:
        await current_root(db, operator)
        task = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))).scalar_one_or_none()
        if task is None or task.owner_principal_id != operator.principal.principal_id or task.owner_session_id != operator.session_id:
            fail("draft_unavailable")
        resolved = await resolve_input_artifact_for_copy(db,
            WorkBoardOwner(operator.principal.principal_id, operator.session_id),
            typed_input_ref=task.typed_input_ref, typed_input_digest=task.typed_input_digest,
            capability_id=task.capability_id, goal_id=task.goal_id, goal_revision=task.goal_revision)
        inputs = resolved.input
        hint = {"message_binding_id": inputs["message_binding_id"],
            "message_revision": inputs["expected_message_revision"],
            "source_consent_id": inputs["mail_consent_id"],
            "source_consent_revision": inputs["expected_source_consent_revision"],
            "source_input_digest": digest(inputs)}
        return await source_snapshot(db, operator, task_id, binding_hint=hint)


async def preview(operator, *, task_id, read_connection_id, send_connection_id,
    request_uuid, priority=60, **boundary):
    connections = await pair_snapshots(operator, read_connection_id, send_connection_id)
    account = await paired_account(operator, connections)
    original = await stage_source(operator, task_id)
    inputs = {"schema_version": 1, "connections": connections, "source": original,
        "account_digest": digest(account), "max_contacts": 14,
        "acknowledge_identity_source_read": True, "acknowledge_exact_reply_send": True,
        "preview_contact_limit": 5, "execution_contact_limit": 9, "no_learning": True}
    admitted = await admit(operator, kind=SEND_KIND, request_uuid=request_uuid,
        goal_id=original["goal_id"], goal_revision=original["goal_revision"], inputs=inputs, priority=priority)
    if admitted.get("receipt", {}).get("status") == "deduped":
        return await snapshot(operator, admitted["job_id"])
    lease = await claim(operator, admitted)
    contacts = Contacts(operator, lease)
    try:
        run = await get_run(operator, lease.job_id)
        credentials = await stage_credentials(operator, run)
        async with asyncio.timeout(max(0, (utc(run.deadline_at)-now()).total_seconds())):
            read = contacts.adapter(READ_SERVICE, credentials[READ_SERVICE], utc(run.deadline_at), **boundary)
            identity = await contacts.authenticate(read)
            if {key: identity[key] for key in account} != account:
                fail("account_changed")
            facts, draft = await actual_source(operator, run, read, contacts)
            message_id = "<seraph."+digest(lease.job_id)[:40]+"@seraph.invalid>"
            frozen = freeze_mime(facts, sender=account["email"], body=draft["plainbody"], message_id=message_id)
            ref = await reserve_private(operator, lease, "preview", {"frozen": frozen, "account": account,
                "source": facts, "draft_artifact_digest": original["artifact_digest"]})
            run = await get_run(operator, lease.job_id)
            expires = min(utc(run.deadline_at).timestamp(), (now()+timedelta(minutes=5)).timestamp())
            approval_id = str(uuid.uuid5(uuid.NAMESPACE_URL, lease.job_id+":exact-approval"))
            scope = {"schema_version": 1, "job_id": lease.job_id, "source": original,
                "account_digest": digest(account), "connections": connections,
                "mime_digest": frozen["mime_digest"], "request_digest": frozen["request_digest"],
                "source_digest": facts["source_digest"], "goal_id": run.goal_id,
                "goal_revision": run.goal_revision, "authority_digest": run.authority_digest,
                "budget_digest": run.budget_digest, "expires_at": expires}
            fingerprint = fingerprint_tool_call("mail.reply.send", {"scope_digest": digest(scope)})
            details = {"approval_scope": scope, "scope_digest": digest(scope),
                "session_id": operator.session_id, "conversation_id": operator.session_id,
                "operator_session_id": operator.session_id, "owner_principal_id": run.owner_principal_id,
                "approval_operator_principal_id": run.owner_principal_id,
                "durable_job_id": lease.job_id, "durable_owner_kind": "user",
                "durable_owner_principal_id": run.owner_principal_id, "durable_service_id": None,
                "durable_authority_digest": run.authority_digest, "durable_goal_id": run.goal_id,
                "durable_goal_revision": run.goal_revision, "durable_plan_revision": None,
                "durable_capability_version": VERSION, "durable_budget_digest": run.budget_digest,
                "durable_approval_id": approval_id, "approval_expires_at": expires,
                "expires_at": expires, "attachment_refs": []}
            row = await approval_repository.get_or_create_pending(session_id=operator.session_id,
                tool_name="mail.reply.send", risk_level="high", summary="Send this one exact Gmail reply",
                fingerprint=fingerprint, details=details, request_id=approval_id)
            if row.id != approval_id:
                fail("approval_changed")
            async with writer() as db:
                run = await get_run(operator, lease.job_id, db=db)
                await authority(db, operator, run, lease=lease)
                checkpoint = state(run)
                if checkpoint.get("preview") or len(checkpoint.get("contacts", [])) != 5 or not contacts.quiescent():
                    fail("preview_phase_changed")
                checkpoint["preview"] = {"approval_id": row.id, "approval_fingerprint": fingerprint,
                    "expires_at": expires, "mime_digest": frozen["mime_digest"],
                    "request_digest": frozen["request_digest"], "source_digest": facts["source_digest"],
                    "scope_digest": digest(scope)}
                checkpoint["transport_quiescent"] = True
                await cas(db, run, {"checkpoint_context_json": canonical(checkpoint),
                    "status": "paused", "lease_owner": None, "lease_expires_at": None})
    except BaseException:
        await failed(operator, lease, contacts)
        raise
    return await snapshot(operator, lease.job_id)


async def decide(operator, ident, *, decision, expected_digest):
    async with writer() as db:
        run = await get_run(operator, ident, db=db)
        await authority(db, operator, run)
        preview = state(run).get("preview")
        if run.status != "paused" or not isinstance(preview, dict):
            fail("approval_phase_changed")
        row = await db.get(ApprovalRequest, preview["approval_id"], populate_existing=True)
        if row is None or row.owner_principal_id != run.owner_principal_id or row.operator_session_id != run.operator_session_id:
            fail("approval_changed")
        await approval_repository.resolve_exact_in_session(db, row.id, decision, expected_digest=expected_digest)
    return await snapshot(operator, ident)


async def execute(operator, ident, **boundary):
    original = await get_run(operator, ident)
    if original.status != "paused" or state(original).get("intent"):
        return await snapshot(operator, ident)  # Receipt-first replay, never another POST.
    async with session() as db:
        await authority(db, operator, await get_run(operator, ident, db=db))
        approval = await db.get(ApprovalRequest, state(original)["preview"]["approval_id"])
        if approval is None or approval.status != "approved":
            fail("approval_required")
    # Stage the actual immutable encrypted preview before claiming any writer.
    ref = state(original)["private_artifacts"]["preview"]
    private = await asyncio.to_thread(read_private_draft, ref["path"], ref["digest"])
    validate_resource(private["frozen"])
    lease = await claim(operator, _serialize(original), continuation=True)
    contacts = Contacts(operator, lease)
    try:
        run = await get_run(operator, ident)
        credentials = await stage_credentials(operator, run)
        async with asyncio.timeout(max(0, (utc(run.deadline_at)-now()).total_seconds())):
            read = contacts.adapter(READ_SERVICE, credentials[READ_SERVICE], utc(run.deadline_at), **boundary)
            send = contacts.adapter(SEND_SERVICE, credentials[SEND_SERVICE], utc(run.deadline_at), **boundary)
            account = same_account(await contacts.authenticate(read), await contacts.authenticate(send))
            if account != private["account"] or digest(account) != arguments(run)["account_digest"]:
                fail("account_changed")
            facts, draft = await actual_source(operator, run, read, contacts)
            if (facts != private["source"] or draft["plainbody"] != private["frozen"]["expected"]["body"]):
                fail("source_changed_since_preview")
            observed_at = now().timestamp()
            refreshed = await get_run(operator, ident)
            intent = {"schema_version": 1, "job_id": ident, "effect_id": ident+":send-once",
                "input_digest": refreshed.input_digest, "authority_digest": refreshed.authority_digest,
                "budget_digest": refreshed.budget_digest, "original_root": operator.session_id,
                "owner_principal_id": refreshed.owner_principal_id, "goal_id": refreshed.goal_id,
                "goal_revision": refreshed.goal_revision, "attempt": refreshed.attempt_count,
                "fencing_token": lease.fencing_token, "approval_id": state(refreshed)["preview"]["approval_id"],
                "mime_digest": private["frozen"]["mime_digest"], "request_digest": private["frozen"]["request_digest"],
                "source_digest": facts["source_digest"], "source_observed_at": observed_at,
                "account_digest": digest(account), "preview_artifact": ref}
            async with writer() as db:
                run = await get_run(operator, ident, db=db)
                await claim_intent(db, operator, run, lease=lease, intent=intent, approval_id=intent["approval_id"])
            # Fresh pure canonical authority is checked in Contacts.before,
            # after strict resource recomputation and immediately before POST.
            response = await contacts.request(send, "send", resource=validate_resource(private["frozen"]))
            sent_id = provider_id(response.get("id"))
            if provider_id(response.get("threadId")) != private["frozen"]["resource"]["threadId"]:
                fail("sent_thread_changed")
            sent = await contacts.request(read, "raw", provider_id_value=sent_id)
            observed = sent_readback(sent, private["frozen"], provider_message_id=sent_id)
            output_ref = await reserve_private(operator, lease, "sent", {"observation": observed,
                "sent_response": sent, "intent_digest": digest(intent)})
            await terminal(operator, lease, contacts, outcome="verified_in_sender_sent_mailbox", private_ref=output_ref)
    except BaseException:
        await failed(operator, lease, contacts)
        raise
    return await snapshot(operator, ident)
