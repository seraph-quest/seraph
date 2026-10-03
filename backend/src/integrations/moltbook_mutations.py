"""One exact approved creation and manual verification on the SAME native job."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import uuid

from sqlmodel import select

from config.settings import settings
from src.approval.repository import approval_repository, fingerprint_tool_call
from src.db import engine
from src.db.models import ApprovalRequest, Secret
from src.integrations.moltbook import MoltbookAdapter, MoltbookError, canonical, digest, identifier, route, safe_comments, safe_content, text
from src.integrations.moltbook_controls import JOB_KIND, PREFIX, WRITES, now, utc, writer, state, save_state
from src.tools.filesystem_tool import _read_workspace_text_bounded, _safe_resolve
from src.vault.crypto import decrypt, encrypt
from src.vault.repository import secret_binding_digest, vault_repository
from src.work_board.input_artifacts import _write_payload
from src.artifacts.registry import build_artifact_record
from src.workspace import canonical_workspace_root


async def load(service, owner, job_id):
    async with engine.get_session() as db:
        run = await service.jobs._fetch(db, job_id)
        connection = await service.current(db, owner, run)
        authority = json.loads(run.declared_authority_json)
        key_name = connection.vault_key
        deadline = utc(run.deadline_at)
    raw, truncated = _read_workspace_text_bounded(_safe_resolve(PREFIX+digest(job_id.encode())+".input.enc"), max_bytes=32768)
    payload = json.loads(decrypt(raw))
    if truncated or digest(payload) != authority["payload_digest"] or payload.get("operation") != authority["operation"]:
        raise MoltbookError("moltbook_private_input_changed")
    credential = await vault_repository.snapshot(key_name, owner_principal_id=owner.principal_id)
    if credential is None or credential.binding_digest != authority["vault_binding_digest"]:
        raise MoltbookError("moltbook_credential_changed")
    if credential.value in canonical(payload).decode():
        raise MoltbookError("moltbook_credential_in_content_denied")
    return payload, credential, authority, deadline


async def reviewed_community(service, owner, *, job_id, expected_digest, community, goal_id, goal_revision, revision):
    snapshot = await service.snapshot(owner, job_id)
    binding = snapshot["declared_authority"]
    if (snapshot["status"] != "succeeded" or binding.get("operation") != "community"
        or snapshot["goal_id"] != goal_id or snapshot["goal_revision"] != goal_revision
        or binding.get("connection_revision") != revision or len(snapshot["artifacts"]) != 1
        or snapshot["artifacts"][0].get("content_sha256") != expected_digest
        or not snapshot["finished_at"] or not now()-timedelta(minutes=5) <= utc(datetime.fromisoformat(snapshot["finished_at"])) <= now()):
        raise MoltbookError("moltbook_current_reviewed_community_required")
    output = await service.output(owner, job_id)
    data = output.get("data")
    if (not isinstance(data, dict) or data.get("is_private") is not False or data.get("name") != community
        or community != "introductions"):
        raise MoltbookError("moltbook_reviewed_public_introductions_required")
    # Provider metadata is data, never a permission grant. The operator must
    # independently acknowledge the community purpose below.
    return {"job_id": job_id, "artifact_digest": expected_digest, "metadata_digest": digest(data),
        "community_id": data["id"], "community": community, "finished_at": snapshot["finished_at"],
        "source_authority_digest": snapshot["authority_digest"], "connection_id": binding["connection_id"],
        "account_id": binding["account_id"], "account_name": binding["account_name"],
        "vault_binding_digest": binding["vault_binding_digest"], "consent_digest": binding["consent_digest"]}


async def prepare_write(service, owner, *, operation, fields, request_key, goal_id, goal_revision,
                        expected_revision, community_job_id, community_digest, introductions_allowed,
                        public_only, priority=50):
    if operation not in WRITES or introductions_allowed is not True or public_only is not True:
        raise MoltbookError("moltbook_exact_public_write_review_required", status_code=422)
    route(operation, fields)
    community = fields.get("community", "introductions")
    review = await reviewed_community(service, owner, job_id=community_job_id, expected_digest=community_digest,
        community=community, goal_id=goal_id, goal_revision=goal_revision, revision=expected_revision)
    review.update(introductions_allowed=True, public_only=True)
    admitted = await service._prepare(owner, operation=operation, fields=fields, request_key=request_key,
        goal_id=goal_id, goal_revision=goal_revision, expected_revision=expected_revision, priority=priority, review=review)
    if admitted["status"] != "accepted": return admitted
    job_id = admitted["job_id"]
    queued = await service.jobs.queue_job(job_id, expected_revision=admitted["revision"])
    runner = "moltbook-prepare:" + uuid.uuid4().hex
    claimed = await service.jobs.claim_job(job_id, owner=runner, expected_revision=queued["revision"],
        expected_fencing_token=queued["lease"]["fencing_token"], lease_seconds=30)
    lease = (runner, claimed["lease"]["fencing_token"])
    async with engine.get_session() as db:
        await writer(db)
        run = await service.jobs._fetch(db, job_id)
        connection = await service.current(db, owner, run, lease=lease)
        if (review["connection_id"] != connection.id or review["vault_binding_digest"] != connection.credential_binding
            or review["consent_digest"] != digest(json.loads(connection.consent_json))
            or review["account_id"] != connection.account_id or review["account_name"] != connection.account_name):
            raise MoltbookError("moltbook_community_authority_changed")
        save_state(run, {"phase": "prepared", "calls": [], "max_contacts": 6,
            "creation_sent": False, "verification_sent": False})
        db.add(run)
    await make_approval(service, owner, job_id, lease, "create")
    projected = await service.jobs.get_job(job_id)
    await service.jobs.transition_job(job_id, "paused", owner=lease[0], fencing_token=lease[1],
        expected_revision=projected["revision"], reason="moltbook_exact_creation_approval_required")
    return await service.snapshot(owner, job_id)


def approval_scope(run, value, kind):
    authority = json.loads(run.declared_authority_json)
    return {"schema": "seraph.moltbook.approval.v1", "kind": kind, "job_id": run.run_identity,
        "owner": run.owner_principal_id, "root": run.operator_session_id, "goal_id": run.goal_id,
        "goal_revision": run.goal_revision, "authority_digest": run.authority_digest,
        "connection_id": authority["connection_id"], "connection_revision": authority["connection_revision"],
        "account_id": authority["account_id"], "vault_binding_digest": authority["vault_binding_digest"],
        "payload_digest": authority["payload_digest"], "review_digest": authority["review_digest"],
        "content_id": value.get("content_id"), "challenge_digest": value.get("challenge_digest") if kind == "verify" else None,
        "answer_binding": value.get("answer_binding") if kind == "verify" else None,
        "expires_at": min(utc(run.deadline_at), datetime.fromisoformat(value["challenge_expires_at"])) .isoformat()
            if kind == "verify" else utc(run.deadline_at).isoformat()}


async def make_approval(service, owner, job_id, lease, kind):
    async with engine.get_session() as db:
        run = await service.jobs._fetch(db, job_id)
        await service.current(db, owner, run, lease=lease)
        value = state(run)
        scope = approval_scope(run, value, kind)
        fingerprint = fingerprint_tool_call("moltbook:"+kind, {"scope_digest": digest(scope)})
        details = {"approval_scope": scope, "approval_operator_principal_id": owner.principal_id,
            "approval_owner_principal_id": owner.principal_id, "approval_owner_operator_session_id": owner.session_id,
            "operator_session_id": owner.session_id, "approval_conversation_id": owner.session_id,
            "durable_job_id": job_id, "durable_owner_kind": "user", "durable_owner_principal_id": owner.principal_id,
            "durable_service_id": None, "durable_authority_digest": run.authority_digest,
            "durable_goal_id": run.goal_id, "durable_goal_revision": run.goal_revision,
            "durable_plan_revision": run.plan_revision, "durable_capability_version": run.capability_version,
            "durable_budget_digest": run.budget_digest, "approval_expires_at": datetime.fromisoformat(scope["expires_at"]).timestamp()}
    approval = await approval_repository.get_or_create_pending(session_id=owner.session_id, tool_name="moltbook:"+kind,
        risk_level="high", summary="Approve exact Moltbook "+("public text creation" if kind == "create" else "manual answer for original content"),
        fingerprint=fingerprint, details=details)
    async with engine.get_session() as db:
        await writer(db)
        run = await service.jobs._fetch(db, job_id)
        await service.current(db, owner, run, lease=lease)
        value = state(run)
        if approval_scope(run, value, kind) != scope: raise MoltbookError("moltbook_approval_binding_changed")
        value.update(phase="awaiting_"+kind+"_approval", approval_id=approval.id, approval_kind=kind,
            approval_fingerprint=fingerprint, approval_scope_digest=digest(scope))
        receipt = {"id": approval.id, "kind": kind, "fingerprint": fingerprint, "scope_digest": digest(scope)}
        history = value.setdefault("approvals", [])
        if receipt not in history:
            if len(history) >= 2: raise MoltbookError("moltbook_original_approval_slot_bound")
            history.append(receipt)
        save_state(run, value)
        db.add(run)
    return approval.id


async def approve(service, owner, job_id, approval_id, decision):
    if decision not in {"approved", "denied"}: raise MoltbookError("moltbook_approval_decision_invalid", status_code=422)
    async with engine.get_session() as db:
        run = await service.jobs._fetch(db, job_id)
        await service.current(db, owner, run)
        value = state(run)
        row = await db.get(ApprovalRequest, approval_id)
        if (run.status != "paused" or value.get("approval_id") != approval_id or row is None
            or row.owner_principal_id != owner.principal_id or row.operator_session_id != owner.session_id
            or row.fingerprint != value.get("approval_fingerprint") or utc(row.expires_at) <= now()):
            raise MoltbookError("moltbook_exact_current_approval_required")
        if row.status == decision: return {"approval_id": approval_id, "status": decision}
        if row.status != "pending": raise MoltbookError("moltbook_approval_already_resolved")
    resolved = await approval_repository.resolve(approval_id, decision)
    if resolved is None or resolved.status != decision: raise MoltbookError("moltbook_approval_resolution_failed")
    return {"approval_id": approval_id, "status": decision}


async def consume(db, service, owner, run, value, kind):
    row = await db.get(ApprovalRequest, value.get("approval_id"))
    scope = approval_scope(run, value, kind)
    if (row is None or value.get("approval_kind") != kind or row.tool_name != "moltbook:"+kind
        or row.fingerprint != value.get("approval_fingerprint") or row.attachment_refs_json != "[]"
        or value.get("approval_scope_digest") != digest(scope)
        or json.loads(row.details_json).get("approval_scope") != scope):
        raise MoltbookError("moltbook_exact_current_approval_required")
    # Empty attachment receipt is required above, avoiding any physical or
    # quarantine callback. Reuse the existing canonical approval consumer in
    # this same writer; it cannot open a separate transaction on this path.
    receipt = await approval_repository._consume_approved_for_resume_in_session(db,
        quarantine_in_separate_session=False, approval_id=row.id, owner_operator_session_id=owner.session_id,
        operator_principal_id=owner.principal_id, job_id=run.run_identity, owner_kind="user",
        owner_principal_id=owner.principal_id, service_id=None, approval_owner_principal_id=owner.principal_id,
        authority_digest=run.authority_digest, goal_id=run.goal_id, goal_revision=run.goal_revision,
        plan_revision=run.plan_revision, capability_version=run.capability_version, budget_digest=run.budget_digest,
        expires_at=utc(row.expires_at).timestamp(), session_id=owner.session_id, conversation_id=owner.session_id,
        criterion_id=None, candidate_id=None)
    if receipt is None: raise MoltbookError("moltbook_approval_not_current_approved")
    return row.id


async def call(service, adapter, owner, job_id, lease, operation, fields, credential, deadline):
    async def contact():
        async with engine.get_session() as db:
            await writer(db)
            run = await service.jobs._fetch(db, job_id)
            await service.current(db, owner, run, lease=lease)
            value = state(run)
            calls = value["calls"]
            if len(calls) >= 6 or any(c["status"] == "intent" for c in calls):
                raise MoltbookError("moltbook_contact_budget_or_uncertainty")
            if operation == "status" and any(c["operation"] == "status" for c in calls):
                raise MoltbookError("moltbook_status_slot_consumed")
            method, path, body = route(operation, fields)
            if method == "GET" and operation != "status" and sum(c["method"] == "GET" and c["operation"] != "status" for c in calls) >= 3:
                raise MoltbookError("moltbook_exact_get_slots_consumed")
            approval_id = None
            if operation in WRITES:
                if value["creation_sent"]: raise MoltbookError("moltbook_creation_slot_consumed")
                approval_id = await consume(db, service, owner, run, value, "create")
                value["creation_sent"] = True
            elif operation == "verify":
                if value["verification_sent"] or not value.get("content_id"):
                    raise MoltbookError("moltbook_original_verification_slot_consumed")
                if datetime.fromisoformat(value["challenge_expires_at"]) <= now(): raise MoltbookError("moltbook_original_challenge_expired")
                challenge = await db.scalar(select(Secret).where(Secret.key == value["challenge_vault_key"],
                    Secret.owner_principal_id == owner.principal_id, Secret.revoked_at.is_(None)))
                answer = await db.scalar(select(Secret).where(Secret.key == value["answer_vault_key"],
                    Secret.owner_principal_id == owner.principal_id, Secret.revoked_at.is_(None)))
                if (challenge is None or answer is None or secret_binding_digest(challenge) != value["challenge_binding"]
                    or secret_binding_digest(answer) != value["answer_binding"]):
                    raise MoltbookError("moltbook_original_challenge_or_answer_changed")
                approval_id = await consume(db, service, owner, run, value, "verify")
                value["verification_sent"] = True
            effect_id = "moltbook-contact:" + str(len(calls)+1)
            request_digest = digest([operation, fields])
            calls.append({"operation": operation, "method": method, "request_digest": request_digest,
                "status": "intent", "effect_id": effect_id})
            effects = json.loads(run.effect_receipts_json)
            effects.append({"effect_id": effect_id, "receipt_kind": "effect", "effect_type": "moltbook_http_"+operation,
                "target_path": path.split("?",1)[0], "target_digest": request_digest,
                "status": "intent", "approval_id": approval_id, "fencing_token": lease[1],
                "recorded_at": now().isoformat(), "details": {"no_learning": True, "single_send": method == "POST"}})
            run.effect_receipts_json = canonical(effects).decode()
            save_state(run, value)
            db.add(run)
    result = await adapter.call(operation, fields, key=credential.value, deadline=deadline, before_contact=contact)
    async with engine.get_session() as db:
        await writer(db)
        run = await service.jobs._fetch(db, job_id)
        service.jobs._assert_lease(run, owner=lease[0], fencing_token=lease[1])
        value = state(run)
        last = value["calls"][-1]
        if last["operation"] != operation or last["status"] != "intent": raise MoltbookError("moltbook_contact_receipt_changed")
        last.update(status="received", response_digest=digest(result))
        effects = json.loads(run.effect_receipts_json)
        matching = [e for e in effects if e["effect_id"] == last["effect_id"]]
        if len(matching) != 1: raise MoltbookError("moltbook_contact_effect_invalid")
        matching[0].update(status="succeeded", content_sha256=digest(result), verified_at=now().isoformat())
        run.effect_receipts_json = canonical(effects).decode()
        save_state(run, value)
        db.add(run)
    return result


def comments_flat(value):
    result = []
    def visit(comment):
        result.append(comment)
        for reply in comment["replies"]: visit(reply)
    for comment in safe_comments(value): visit(comment)
    return result


async def execute_write(service, owner, job_id, *, execution):
    projected = await service.snapshot(owner, job_id)
    if projected["lease"]["fencing_token"] != execution["fencing_token"]:
        raise MoltbookError("moltbook_original_execution_fence_changed")
    payload, credential, authority, deadline = await load(service, owner, job_id)
    async with engine.get_session() as db:
        run = await service.jobs._fetch(db, job_id)
        value = state(run)
        if (run.status != "paused" or value.get("phase") != execution["phase"]
            or value.get("phase") not in {"awaiting_create_approval", "awaiting_verify_approval"}):
            raise MoltbookError("moltbook_explicit_original_recovery_required")
        approval = await db.get(ApprovalRequest, value.get("approval_id"))
        if approval is None or approval.status != "approved": raise MoltbookError("moltbook_exact_approval_pending")
    projected = await service.jobs.queue_job(job_id, expected_revision=projected["revision"])
    runner = "moltbook-execute:"+uuid.uuid4().hex
    claimed = await service.jobs.claim_job(job_id, owner=runner, expected_revision=projected["revision"],
        expected_fencing_token=projected["lease"]["fencing_token"], continue_existing_attempt=True, lease_seconds=max(1, (deadline-now()).total_seconds()))
    lease = (runner, claimed["lease"]["fencing_token"])
    async with engine.get_session() as db:
        await writer(db); run = await service.jobs._fetch(db, job_id)
        await service.current(db, owner, run, lease=lease)
        value = state(run)
        if value["phase"] != execution["phase"] or any(item["phase"] == execution["phase"] for item in value.get("executions", [])):
            raise MoltbookError("moltbook_original_execution_phase_changed")
        value.setdefault("executions", []).append(execution)
        if len(value["executions"]) > 2: raise MoltbookError("moltbook_original_execution_slot_bound")
        save_state(run, value); db.add(run)
    adapter = MoltbookAdapter(transport=service.adapter.transport, resolver=service.adapter.resolver)
    worker = asyncio.current_task()
    service._active[job_id] = worker
    try:
        fields = payload["fields"]
        if value["phase"] == "awaiting_create_approval":
            review = payload["review"]
            await reviewed_community(service, owner, job_id=review["job_id"], expected_digest=review["artifact_digest"],
                community=review["community"], goal_id=projected["goal_id"], goal_revision=projected["goal_revision"],
                revision=authority["connection_revision"])
            status = await call(service, adapter, owner, job_id, lease, "status", {}, credential, deadline)
            if status.get("status") != "claimed": raise MoltbookError("moltbook_current_human_claim_required")
            if payload["operation"] == "create_comment":
                destination = safe_content((await call(service, adapter, owner, job_id, lease, "post", {"post_id": fields["post_id"]}, credential, deadline)).get("post"))
                if (destination["id"] != fields["post_id"] or destination.get("community") != review["community"]
                    or destination["explicitly_hidden"] or destination["visibility"] in {"pending", "failed"}):
                    raise MoltbookError("moltbook_current_public_target_changed")
                if "parent_id" in fields:
                    parent = await call(service, adapter, owner, job_id, lease, "comments", {"post_id": fields["post_id"], "sort": "new", "limit": 10}, credential, deadline)
                    if not any(c["id"] == fields["parent_id"] for c in comments_flat(parent)):
                        raise MoltbookError("moltbook_parent_not_in_exact_selected_post")
            created = await call(service, adapter, owner, job_id, lease, payload["operation"], fields, credential, deadline)
            kind = "post" if payload["operation"] == "create_post" else "comment"
            content = created.get(kind)
            if not isinstance(content, dict): raise MoltbookError("moltbook_known_created_content_required")
            content_id = identifier(content.get("id"))
            async with engine.get_session() as db:
                await writer(db)
                run = await service.jobs._fetch(db, job_id)
                service.jobs._assert_lease(run, owner=lease[0], fencing_token=lease[1])
                value = state(run)
                value.update(content_id=content_id, content_kind=kind, phase="created_received")
                save_state(run, value); db.add(run)
            verification = content.get("verification")
            if created.get("verification_required") is True or content.get("verification_status") == "pending":
                if not isinstance(verification, dict): raise MoltbookError("moltbook_original_challenge_missing")
                code = text(verification.get("verification_code"), 512)
                challenge = text(verification.get("challenge_text"), 4096).replace(credential.value, "[redacted credential]")
                try: expiry = utc(datetime.fromisoformat(verification["expires_at"].replace("Z", "+00:00")))
                except (ValueError, TypeError, KeyError): raise MoltbookError("moltbook_challenge_expiry_invalid") from None
                if expiry <= now() or expiry > now()+timedelta(minutes=5): raise MoltbookError("moltbook_challenge_window_invalid")
                encrypted = encrypt(code)
                async with engine.get_session() as db:
                    await writer(db)
                    run = await service.jobs._fetch(db, job_id)
                    service.jobs._assert_lease(run, owner=lease[0], fencing_token=lease[1])
                    value = state(run)
                    secret = Secret(key="moltbook.challenge."+digest(job_id.encode()), owner_principal_id=owner.principal_id,
                        encrypted_value=encrypted, description="Private original Moltbook content verification code")
                    db.add(secret); await db.flush(); await db.refresh(secret)
                    value.update(phase="awaiting_manual_answer", content_id=content_id, content_kind=kind,
                        challenge_text=challenge, challenge_expires_at=min(expiry, deadline).isoformat(),
                        challenge_vault_key=secret.key, challenge_binding=secret_binding_digest(secret),
                        challenge_digest=digest([content_id, challenge, expiry.isoformat(), secret_binding_digest(secret)]))
                    save_state(run, value); db.add(run)
                # Current authority is required to publish this continuation;
                # a received secret remains private audit if authority drifted.
                async with engine.get_session() as db:
                    run = await service.jobs._fetch(db, job_id)
                    await service.current(db, owner, run, lease=lease)
                latest = await service.jobs.get_job(job_id)
                await service.jobs.transition_job(job_id, "paused", owner=lease[0], fencing_token=lease[1],
                    expected_revision=latest["revision"], reason="moltbook_manual_answer_required")
                return await service.snapshot(owner, job_id)
            async with engine.get_session() as db:
                await writer(db); run = await service.jobs._fetch(db, job_id)
                await service.current(db, owner, run, lease=lease)
                value = state(run); value.update(content_id=content_id, content_kind=kind, phase="created")
                save_state(run, value); db.add(run)
        else:
            challenge = await vault_repository.snapshot(value["challenge_vault_key"], owner_principal_id=owner.principal_id)
            answer = await vault_repository.snapshot(value["answer_vault_key"], owner_principal_id=owner.principal_id)
            if (challenge is None or answer is None or challenge.binding_digest != value["challenge_binding"]
                or answer.binding_digest != value["answer_binding"]): raise MoltbookError("moltbook_original_challenge_or_answer_changed")
            await call(service, adapter, owner, job_id, lease, "verify", {"verification_code": challenge.value, "answer": answer.value}, credential, deadline)
        async with engine.get_session() as db:
            run = await service.jobs._fetch(db, job_id); value = state(run)
        if payload["operation"] == "create_post":
            # The documented community feed is the public visibility proof.
            # Exact-ID GET alone may return hidden owner content. One bounded
            # page, no pagination/retry: absent membership remains unconfirmed.
            page = await call(service, adapter, owner, job_id, lease, "feed", {"sort": "new", "limit": 1,
                "community": fields["community"]}, credential, deadline)
            posts = page.get("posts")
            if not isinstance(posts, list) or len(posts) > 1: raise MoltbookError("moltbook_public_feed_readback_bound")
            matches = [safe_content(post) for post in posts if isinstance(post, dict) and post.get("id") == value["content_id"]]
            if len(matches) != 1: raise MoltbookError("moltbook_public_listing_membership_unconfirmed")
            observed = matches[0]
        else:
            response = await call(service, adapter, owner, job_id, lease, "comments", {"post_id": fields["post_id"], "sort": "new", "limit": 10}, credential, deadline)
            matches = [c for c in comments_flat(response) if c["id"] == value["content_id"]]
            if len(matches) != 1: raise MoltbookError("moltbook_exact_comment_readback_incomplete")
            observed = matches[0]
        if (observed["id"] != value["content_id"] or observed["author_id"] != authority["account_id"]
            or observed["content"] != fields["content"] or observed["visibility"] != "verified" or observed["explicitly_hidden"]
            or (payload["operation"] == "create_post" and (observed.get("title") != fields["title"] or observed.get("community") != fields["community"]))
            or observed.get("parent_id") != fields.get("parent_id")):
            raise MoltbookError("moltbook_exact_visible_verified_readback_required")
        output = canonical({"schema": "seraph.moltbook.publication.v1", "job_id": job_id,
            "content_id": value["content_id"], "outcome": "published_verified", "author_id": authority["account_id"],
            "payload_digest": authority["payload_digest"], "no_learning": True, "private_owner_receipt": True})
        output_ref = PREFIX+digest(job_id.encode())+".output.json"
        readback_receipt = {"effect_id": "moltbook-published:"+value["content_id"], "receipt_kind": "readback",
            "effect_type": "moltbook_publication", "target_path": value["content_id"], "status": "succeeded",
            "content_sha256": digest(output), "readback_id": "provider:"+digest(observed), "verified_at": now().isoformat()}
        async with engine.get_session() as db:
            await writer(db); run = await service.jobs._fetch(db, job_id)
            await service.current(db, owner, run, lease=lease)
            value = state(run)
            if any(c["status"] != "received" for c in value["calls"]): raise MoltbookError("moltbook_contact_unsettled")
            value.update(phase="verified_output_ready", verified_output={"output_ref": output_ref,
                "output_digest": digest(output), "payload_digest": authority["payload_digest"],
                "artifact_type": "moltbook_private_publication", "terminal_phase": "published_verified",
                "readback_receipt": readback_receipt}, cleanup=adapter.marker.snapshot())
            save_state(run, value); db.add(run)
        _write_payload(canonical_workspace_root(settings.workspace_dir)/output_ref, output)
        readback, truncated = _read_workspace_text_bounded(_safe_resolve(output_ref), max_bytes=65536)
        if truncated or readback.encode() != output: raise MoltbookError("moltbook_output_readback_failed")
        artifact = build_artifact_record(file_path=output_ref, artifact_type="moltbook_private_publication",
            producer=JOB_KIND, run_id=job_id, session_id=owner.session_id, content=output)
        async with engine.get_session() as db:
            await writer(db); run = await service.jobs._fetch(db, job_id)
            connection = await service.current(db, owner, run, lease=lease)
            value = state(run)
            if any(c["status"] != "received" for c in value["calls"]): raise MoltbookError("moltbook_contact_unsettled")
            value.update(phase="published_verified", output_ref=output_ref, output_digest=digest(output), cleanup=adapter.marker.snapshot())
            save_state(run, value)
            effects = json.loads(run.effect_receipts_json)
            effects.append(readback_receipt)
            run.effect_receipts_json, run.artifact_receipts_json = canonical(effects).decode(), canonical([artifact]).decode()
            run.status, run.result_digest, run.result_summary = "succeeded", digest(output), "Exact original content independently verified; no learning"
            run.finished_at, run.lease_owner, run.lease_expires_at = now(), None, None
            connection.active_job_id, connection.active_deadline_at, connection.active_payload_digest = None, None, ""
            db.add(run); db.add(connection)
        return await service.snapshot(owner, job_id)
    except BaseException as exc:
        async with engine.get_session() as db:
            await writer(db); run = await service.jobs._fetch(db, job_id)
            if run.status == "running" and run.lease_owner == lease[0] and run.fencing_token == lease[1]:
                value = state(run)
                await service.retain_cooldown(db, owner, run, value, exc, lease)
                uncertain = bool(value.get("creation_sent")) or any(
                    call.get("status") != "received" for call in value.get("calls", []))
                if value.get("phase") != "verified_output_ready":
                    value["phase"] = "unknown" if uncertain else "blocked"
                value["cleanup"] = adapter.marker.snapshot()
                if value["cleanup"]["status"] == "verified":
                    value["worker_completed"] = {"fencing_token": lease[1], "transport_closed": True}
                save_state(run, value)
                run.status = "unknown_external_effect" if uncertain else "blocked"
                run.failure_reason = getattr(exc, "code", "moltbook_transfer_or_authority_failed")
                run.lease_owner = run.lease_expires_at = None
                db.add(run)
        if isinstance(exc, asyncio.CancelledError): raise
        if isinstance(exc, MoltbookError): raise
        raise MoltbookError("moltbook_transfer_or_authority_failed") from None
    finally:
        if service._active.get(job_id) is worker: service._active.pop(job_id, None)


async def manual_answer(service, owner, job_id, *, answer, request_key):
    identifier(request_key)
    route("verify", {"verification_code": "shape-validation-only", "answer": answer})
    projected = await service.snapshot(owner, job_id)
    async with engine.get_session() as db:
        run = await service.jobs._fetch(db, job_id)
        await service.current(db, owner, run)
        value = state(run)
        if value.get("answer_request_key") == request_key:
            if value.get("answer_digest") != digest(answer.encode()): raise MoltbookError("moltbook_answer_idempotency_conflict")
            return projected
        if run.status != "paused" or value.get("phase") != "awaiting_manual_answer" or datetime.fromisoformat(value["challenge_expires_at"]) <= now():
            raise MoltbookError("moltbook_original_manual_challenge_not_current")
    encrypted = encrypt(answer)
    queued = await service.jobs.queue_job(job_id, expected_revision=projected["revision"])
    runner = "moltbook-answer:"+uuid.uuid4().hex
    claimed = await service.jobs.claim_job(job_id, owner=runner, expected_revision=queued["revision"],
        expected_fencing_token=queued["lease"]["fencing_token"], continue_existing_attempt=True, lease_seconds=30)
    lease = (runner, claimed["lease"]["fencing_token"])
    async with engine.get_session() as db:
        await writer(db); run = await service.jobs._fetch(db, job_id)
        await service.current(db, owner, run, lease=lease); value = state(run)
        if value["phase"] != "awaiting_manual_answer" or datetime.fromisoformat(value["challenge_expires_at"]) <= now():
            raise MoltbookError("moltbook_original_manual_challenge_not_current")
        secret = Secret(key="moltbook.answer."+digest(job_id.encode()), owner_principal_id=owner.principal_id,
            encrypted_value=encrypted, description="Private operator answer for original Moltbook challenge")
        db.add(secret); await db.flush(); await db.refresh(secret)
        value.update(answer_vault_key=secret.key, answer_binding=secret_binding_digest(secret),
            answer_request_key=request_key, answer_digest=digest(answer.encode()))
        save_state(run, value); db.add(run)
    await make_approval(service, owner, job_id, lease, "verify")
    latest = await service.jobs.get_job(job_id)
    await service.jobs.transition_job(job_id, "paused", owner=lease[0], fencing_token=lease[1],
        expected_revision=latest["revision"], reason="moltbook_exact_manual_verification_approval_required")
    return await service.snapshot(owner, job_id)
