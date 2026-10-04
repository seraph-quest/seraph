"""Explicit original-job cancellation and local recovery; never replay HTTP."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

from src.artifacts.registry import build_artifact_record
from src.db import engine
from src.db.models import MoltbookConnection
from src.integrations.moltbook import JOB_KIND, MoltbookError, canonical, digest, identifier
from src.browser.moltbook_private_read import JOB_KIND as PRIVATE_JOB_KIND
from src.integrations.moltbook_controls import PREFIX, now, root_current, save_state, state, utc, writer
from src.tools.filesystem_tool import _read_workspace_text_bounded, _safe_resolve


async def owned(service, db, owner, job_id):
    await root_current(db, owner.principal_id, owner.session_id)
    run = await service.jobs._fetch(db, job_id)
    if (run.owner_principal_id != owner.principal_id or run.operator_session_id != owner.session_id
        or run.job_kind not in {JOB_KIND, PRIVATE_JOB_KIND} or run.capability_version != "1"):
        raise MoltbookError("moltbook_job_owner_mismatch", status_code=404)
    return run


def checkpoint(run):
    return state(run) if run.checkpoint_receipts_json not in (None, "", "[]") else {
        "phase": "unattempted", "calls": []}


async def cancel(service, owner, job_id, *, request_key, expected_revision, fencing_token):
    identifier(request_key)
    binding = digest([job_id, owner.principal_id, owner.session_id, expected_revision, fencing_token])
    async with engine.get_session() as db:
        await writer(db)
        run = await owned(service, db, owner, job_id)
        value = checkpoint(run)
        prior = value.get("cancel_request")
        if prior:
            if prior["request_key"] != request_key or prior["binding"] != binding:
                raise MoltbookError("moltbook_original_cancel_request_conflict")
            if prior.get("quiescent_at"):
                from src.workflows.job_runtime import _serialize
                result = _serialize(run)
                result.update(no_learning=True, remote_data="untrusted_literal_owner_personal_noncommercial_no_redistribution")
                return result
        else:
            if run.status in {"succeeded", "cancelled"}:
                raise MoltbookError("moltbook_terminal_job_cannot_be_cancelled")
            if run.revision != expected_revision or run.fencing_token != fencing_token:
                raise MoltbookError("moltbook_cancel_original_fence_changed")
            value["cancel_request"] = {"request_key": request_key, "binding": binding,
                "original_revision": expected_revision, "fencing_token": fencing_token,
                "applied_at": now().isoformat(), "cleanup": "pending"}
            save_state(run, value)
            db.add(run)
        original_status = run.status
    worker = service._active.get(job_id)
    completed_worker = False
    if worker is not None and worker is not asyncio.current_task():
        worker.cancel()
        try:
            # Waiting does not renew the job deadline. Shield preserves a
            # continuing cleanup task if the finite observer times out.
            async with asyncio.timeout(5):
                await asyncio.shield(worker)
        except asyncio.CancelledError:
            completed_worker = worker.done()
        except (TimeoutError, MoltbookError):
            completed_worker = worker.done()
        else:
            completed_worker = worker.done()
    async with engine.get_session() as db:
        await writer(db)
        run = await owned(service, db, owner, job_id)
        value = checkpoint(run)
        receipt = value.get("cancel_request")
        if not receipt or receipt["binding"] != binding or receipt["request_key"] != request_key:
            raise MoltbookError("moltbook_cancel_receipt_changed")
        # Empty registry / expired lease is never evidence that a worker has
        # stopped. Unattempted states and typed, committed native waits have
        # no executing HTTP continuation. Running requires this exact awaited
        # worker or an already committed finalizer proof.
        quiescent = ((completed_worker and value.get("cleanup", {}).get("status") == "verified")
            or original_status in {"accepted", "queued", "paused"}
            or value.get("worker_completed") == {"fencing_token": fencing_token, "transport_closed": True})
        unsettled = any(call.get("status") != "received" for call in value.get("calls", []))
        authority = json.loads(run.declared_authority_json)
        row = await db.get(MoltbookConnection, authority["connection_id"])
        known_rate_limit = any(c.get("status") == "received" and c.get("http_status") == 429
            and c.get("method") == "GET" for c in value.get("calls", []))
        cooldown_proven = (not known_rate_limit or (row is not None
            and row.owner_principal_id == owner.principal_id and row.revision == authority["connection_revision"]
            and row.credential_binding == authority["vault_binding_digest"]
            and value.get("provider_cooldown_until") is not None and row.cooldown_until is not None
            and utc(row.cooldown_until) >= datetime.fromisoformat(value["provider_cooldown_until"])))
        exact_release = (not known_rate_limit or row is None or row.active_job_id != job_id
            or row.active_payload_digest == authority["payload_digest"])
        if quiescent and (not cooldown_proven or not exact_release):
            receipt["cleanup"] = "settled_rate_limit_cooldown_unproven_capacity_held"
        elif quiescent:
            receipt["cleanup"] = "completed_original_worker_or_native_wait"
            receipt["quiescent_at"] = now().isoformat()
            if unsettled:
                run.status = "unknown_external_effect"
                value["phase"] = "cancelled_unknown_contact"
            else:
                run.status, run.finished_at = "cancelled", now()
                value["phase"] = "cancelled_known_content_retained" if value.get("content_id") else "cancelled"
                if row and row.active_job_id == job_id and (not known_rate_limit
                    or row.active_payload_digest == authority["payload_digest"]):
                    row.active_job_id, row.active_deadline_at, row.active_payload_digest = None, None, ""
                    db.add(row)
            run.lease_owner = run.lease_expires_at = None
        else:
            receipt["cleanup"] = "unproven_worker_quiescence_capacity_held"
        # Produced bytes and known remote content are audit only. A cancel
        # winner can never acquire a usable successful artifact/readback.
        run.failure_reason = "moltbook_operator_cancelled"
        save_state(run, value)
        db.add(run)
    return await service.snapshot(owner, job_id)


async def recover(service, owner, job_id):
    """Inspect or adopt an exact already verified output; no provider contact."""
    async with engine.get_session() as db:
        run = await owned(service, db, owner, job_id)
        value = checkpoint(run)
        if run.status in {"succeeded", "cancelled"}:
            return await service.snapshot(owner, job_id)
        if value.get("cancel_request"):
            raise MoltbookError("moltbook_cancel_cleanup_or_unknown_requires_inspection")
        # Queued/unattempted and durable approval/manual-answer waits are safe
        # to inspect after restart; execution remains a separate explicit POST.
        if run.status in {"accepted", "queued", "paused"}:
            await service.current(db, owner, run)
            return await service.snapshot(owner, job_id)
        proof = value.get("verified_output")
        if not isinstance(proof, dict) or value.get("phase") != "verified_output_ready":
            raise MoltbookError("moltbook_no_verified_output_unknown_contact_never_replayed")
        await service.current(db, owner, run)
        original = (run.revision, run.fencing_token, run.authority_digest, digest(value))
        authority = json.loads(run.declared_authority_json)
        if (proof.get("payload_digest") != authority["payload_digest"]
            or any(call.get("status") != "received" for call in value.get("calls", []))):
            raise MoltbookError("moltbook_original_verified_output_binding_changed")
    worker = service._active.get(job_id)
    if worker is not None and not worker.done():
        raise MoltbookError("moltbook_original_worker_still_active")
    reference = PREFIX + digest(job_id.encode()) + ".output.json"
    raw, truncated = _read_workspace_text_bounded(_safe_resolve(reference), max_bytes=65536)
    output = raw.encode()
    if (truncated or proof.get("output_ref") != reference or digest(output) != proof.get("output_digest")
        or json.loads(raw).get("job_id") != job_id or json.loads(raw).get("no_learning") is not True):
        raise MoltbookError("moltbook_original_verified_output_changed")
    artifact = build_artifact_record(file_path=reference, artifact_type=proof["artifact_type"],
        producer=JOB_KIND, run_id=job_id, session_id=owner.session_id, content=output)
    async with engine.get_session() as db:
        await writer(db)
        run = await owned(service, db, owner, job_id)
        row = await service.current(db, owner, run)
        value = checkpoint(run)
        if (run.revision, run.fencing_token, run.authority_digest, digest(value)) != original:
            raise MoltbookError("moltbook_recovery_original_phase_changed")
        # The trusted ready phase is written only after ALL fixed HTTP slots
        # have settled, exact readback passed, and the response transport closed.
        # Its code has no future contact; fence the old physical-only continuation.
        run.fencing_token += 1
        value.update(phase=proof["terminal_phase"], output_ref=reference, output_digest=digest(output),
            recovery={"original_fencing_token": original[1], "no_http_replay": True, "at": now().isoformat()})
        effects = json.loads(run.effect_receipts_json)
        effects.append(proof["readback_receipt"])
        run.effect_receipts_json = canonical(effects).decode()
        run.artifact_receipts_json = canonical([artifact]).decode()
        run.status, run.result_digest, run.result_summary = "succeeded", digest(output), "Original verified output recovered; no HTTP replay or learning"
        run.finished_at, run.lease_owner, run.lease_expires_at = now(), None, None
        row.active_job_id, row.active_deadline_at, row.active_payload_digest = None, None, ""
        if proof.get("account_update"):
            account = proof["account_update"]
            row.account_id, row.account_name = account["account_id"], account["account_name"]
            row.mode = "active" if account["claimed"] else "pending_claim"
        save_state(run, value)
        db.add(run); db.add(row)
    return await service.snapshot(owner, job_id)
