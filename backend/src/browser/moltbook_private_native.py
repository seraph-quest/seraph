"""Canonical fixed private browser invocation on the existing Moltbook row."""
from __future__ import annotations

import asyncio
import json
import uuid

from config.settings import settings
from src.artifacts.registry import build_artifact_record
from src.browser.moltbook_private_read import ARTIFACT_TYPE, JOB_KIND, OPERATION, PROFILE, validate_read_result
from src.browser.task_lane import try_acquire_browser_task_lane
from src.db import engine
from src.integrations.moltbook import MoltbookError, canonical, digest
from src.integrations.moltbook_controls import PREFIX, now, utc, writer, state, save_state
from src.tools.filesystem_tool import _read_workspace_text_bounded, _safe_resolve
from src.vault.crypto import decrypt, encrypt
from src.vault.repository import vault_repository
from src.work_board.input_artifacts import _write_payload
from src.work_board.repository import BoardError
from src.workspace import canonical_workspace_root

MAX_CIPHERTEXT = 98304


def output_ref(job_id):
    return PREFIX+digest(job_id.encode())+".private-browser.enc"


def read_output(reference, expected_digest, *, expected_job):
    if reference != output_ref(expected_job):
        raise MoltbookError("moltbook_private_output_reference_changed")
    raw, truncated = _read_workspace_text_bounded(_safe_resolve(reference),max_bytes=MAX_CIPHERTEXT)
    if truncated or digest(raw.encode()) != expected_digest:
        raise MoltbookError("moltbook_private_output_changed")
    payload = json.loads(decrypt(raw))
    if (len(canonical(payload)) > 65536 or payload.get("job_id") != expected_job
        or payload.get("profile") != PROFILE or payload.get("no_learning") is not True):
        raise MoltbookError("moltbook_private_output_binding_changed")
    validate_read_result(payload,expected_job=expected_job)
    return payload


async def execute_private_read(service, owner, job_id, *, execution):
    service.private_browser.require_available()
    projected = await service.snapshot(owner,job_id)
    if projected["lease"]["fencing_token"] != execution["fencing_token"]:
        raise MoltbookError("moltbook_original_execution_fence_changed")
    if projected["status"] == "accepted":
        projected = await service.jobs.queue_job(job_id,expected_revision=projected["revision"])
    if projected["status"] != "queued":
        raise MoltbookError("moltbook_private_original_contact_not_replayable")
    # Shared physical browser lane is acquired outside SQL before attempt claim.
    lane = try_acquire_browser_task_lane(settings.workspace_dir)
    if lane is None: raise MoltbookError("browser_slot_busy")
    lease = None
    task = asyncio.current_task()
    cleanup = {"status":"verified","browser_closed":True,"launch_attempted":False,
        "no_persistent_storage":True,"transport":{"status":"verified","requests_started":0,"requests_settled":0}}
    authority = None
    try:
        async with engine.get_session() as db:
            run = await service.jobs._fetch(db,job_id)
            row = await service.current(db,owner,run)
            if run.job_kind != JOB_KIND: raise MoltbookError("moltbook_private_native_kind_changed")
            authority = json.loads(run.declared_authority_json)
            vault_key, deadline = row.vault_key, utc(run.deadline_at)
        reference = PREFIX+digest(job_id.encode())+".input.enc"
        ciphertext,truncated = _read_workspace_text_bounded(_safe_resolve(reference),max_bytes=32768)
        payload = json.loads(decrypt(ciphertext))
        if (truncated or digest(payload) != authority["payload_digest"] or payload != {
            "operation":OPERATION,"fields":{},"no_learning":True}
            or digest(ciphertext.encode()) != authority["input_cipher_digest"]):
            raise MoltbookError("moltbook_private_input_changed")
        credential = await vault_repository.snapshot(vault_key,owner_principal_id=owner.principal_id)
        if credential is None or credential.binding_digest != authority["vault_binding_digest"]:
            raise MoltbookError("moltbook_credential_changed")
        projected = await service.jobs.claim_job(job_id,owner="moltbook-private:"+uuid.uuid4().hex,
            expected_revision=projected["revision"],expected_fencing_token=projected["lease"]["fencing_token"],
            lease_seconds=120)
        lease = (projected["lease"]["owner"],projected["lease"]["fencing_token"])
        async with engine.get_session() as db:
            await writer(db)
            run = await service.jobs._fetch(db,job_id)
            await service.current(db,owner,run,lease=lease)
            if run.checkpoint_receipts_json not in (None,"","[]"):
                raise MoltbookError("moltbook_private_original_contact_not_replayable")
            save_state(run,{"phase":"prepared","calls":[],"executions":[execution],"max_contacts":3,
                "profile":PROFILE,"input_cipher_digest":authority["input_cipher_digest"]})
            db.add(run)
        service._active[job_id] = task

        async def check_current():
            # Physical proof is staged before the canonical session/writer;
            # no private file or decrypt is ever performed inside a writer.
            raw,over = _read_workspace_text_bounded(_safe_resolve(reference),max_bytes=32768)
            if over or digest(raw.encode()) != authority["input_cipher_digest"]:
                raise MoltbookError("moltbook_private_input_changed")
            async with engine.get_session() as db:
                run = await service.jobs._fetch(db,job_id)
                await service.current(db,owner,run,lease=lease)

        async def contact(operation):
            async with engine.get_session() as db:
                await writer(db)
                run = await service.jobs._fetch(db,job_id)
                await service.current(db,owner,run,lease=lease)
                value = state(run)
                expected = ["me_before","home","me_after"]
                index = len(value["calls"])
                if (index >= 3 or operation != expected[index]
                    or any(c["status"] != "received" for c in value["calls"])):
                    raise MoltbookError("moltbook_private_contact_inventory_or_uncertainty")
                value["calls"].append({"operation":operation,"method":"GET","status":"intent",
                    "request_digest":digest([PROFILE,operation,authority["account_id"],authority["vault_binding_digest"]]),
                    "may_deliver_briefing":operation == "home","fencing_token":lease[1],"at":now().isoformat()})
                value["phase"] = "contact_started"
                save_state(run,value); db.add(run)

        async def observe(operation,status,response_digest,transport):
            async with engine.get_session() as db:
                await writer(db)
                run = await service.jobs._fetch(db,job_id)
                service.jobs._assert_lease(run,owner=lease[0],fencing_token=lease[1])
                value = state(run)
                last = value["calls"][-1]
                if last["operation"] != operation or last["status"] != "intent" or transport["status"] != "verified":
                    raise MoltbookError("moltbook_private_response_inventory_changed")
                last.update(status="received",http_status=status,response_digest=response_digest,
                    transport_closed=True,observed_at=now().isoformat())
                save_state(run,value); db.add(run)

        async def retain_cleanup(value):
            nonlocal cleanup
            cleanup = value
            async with engine.get_session() as db:
                await writer(db)
                run = await service.jobs._fetch(db,job_id)
                service.jobs._assert_lease(run,owner=lease[0],fencing_token=lease[1])
                current = state(run)
                current["cleanup"] = value
                if value["status"] == "verified":
                    current["worker_completed"] = {"fencing_token":lease[1],"transport_closed":True}
                save_state(run,current); db.add(run)

        result = await service.private_browser.read(credential=credential.value,expected_id=authority["account_id"],
            expected_name=authority["account_name"],deadline=deadline,check_current=check_current,
            contact=contact,observe=observe,cleanup_observer=retain_cleanup)
        result["job_id"] = job_id
        validate_read_result(result,expected_job=job_id)
        plain = canonical(result)
        if len(plain) > 65536: raise MoltbookError("moltbook_private_output_bound")
        encrypted = encrypt(plain.decode()).encode()
        if len(encrypted) > MAX_CIPHERTEXT: raise MoltbookError("moltbook_private_ciphertext_bound")
        target = output_ref(job_id)
        _write_payload(canonical_workspace_root(settings.workspace_dir)/target,encrypted)
        actual = read_output(target,digest(encrypted),expected_job=job_id)
        if canonical(actual) != plain: raise MoltbookError("moltbook_private_output_readback_changed")
        artifact = build_artifact_record(file_path=target,artifact_type=ARTIFACT_TYPE,producer=JOB_KIND,
            run_id=job_id,session_id=owner.session_id,content=encrypted,
            trust_boundary="owner_private_encrypted_no_model_context",recovery_hint="Inspect original job; never replay Home contact")
        receipt = {"effect_id":"moltbook-private-read:"+digest(encrypted),"receipt_kind":"readback",
            "effect_type":ARTIFACT_TYPE,"target_path":target,"target_digest":digest(encrypted),
            "content_sha256":digest(encrypted),"status":"succeeded","readback_id":"physical:"+digest(encrypted),
            "verified_at":now().isoformat(),"details":{"verified":True,"no_learning":True,"plain_digest":digest(plain)}}
        await check_current()
        async with engine.get_session() as db:
            await writer(db)
            run = await service.jobs._fetch(db,job_id)
            row = await service.current(db,owner,run,lease=lease)
            value = state(run)
            if (len(value["calls"]) != 3 or any(c["status"] != "received" or c["http_status"] != 200 for c in value["calls"])
                or value.get("cleanup") != cleanup or cleanup["status"] != "verified" or value.get("cancel_request")):
                raise MoltbookError("moltbook_private_positive_completion_unproven")
            value.update(phase="adopted",output_ref=target,output_digest=digest(encrypted),
                plaintext_digest=digest(plain),no_learning=True)
            save_state(run,value)
            run.artifact_receipts_json,run.effect_receipts_json = canonical([artifact]).decode(),canonical([receipt]).decode()
            run.status,run.result_digest,run.result_summary = "succeeded",digest(encrypted),"Private Chromium Home read verified; no learning; production unverified"
            run.finished_at,run.lease_owner,run.lease_expires_at = now(),None,None
            row.active_job_id,row.active_deadline_at,row.active_payload_digest = None,None,""
            db.add(run); db.add(row)
        return await service.snapshot(owner,job_id)
    except BaseException as exc:
        if lease is not None:
            async with engine.get_session() as db:
                await writer(db)
                run = await service.jobs._fetch(db,job_id)
                if run.lease_owner == lease[0] and run.fencing_token == lease[1] and run.status == "running":
                    value = state(run)
                    uncertain = cleanup["status"] != "verified" or any(c["status"] != "received" for c in value["calls"])
                    value.update(phase="unknown" if uncertain else "blocked",cleanup=cleanup)
                    if cleanup["status"] == "verified":
                        value["worker_completed"] = {"fencing_token":lease[1],"transport_closed":True}
                    try: row = await service.current(db,owner,run,lease=lease,settlement_only=True)
                    except (MoltbookError,BoardError): row = None
                    if not uncertain and row is not None and not value.get("cancel_request"):
                        row.active_job_id,row.active_deadline_at,row.active_payload_digest = None,None,""
                        db.add(row)
                    save_state(run,value)
                    run.status = "unknown_external_effect" if uncertain else "blocked"
                    run.failure_reason = getattr(exc,"code","moltbook_private_browser_or_transfer_failed")
                    run.lease_owner = run.lease_expires_at = None
                    db.add(run)
        if isinstance(exc,(MoltbookError,asyncio.CancelledError)): raise
        raise MoltbookError("moltbook_private_browser_or_transfer_failed") from None
    finally:
        if service._active.get(job_id) is task: service._active.pop(job_id,None)
        if cleanup["status"] == "verified": lane.release()
        else: lane.quarantine(job_id)
