"""Explicit finite research recovery on the original canonical attempt."""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone

from sqlalchemy import select, text

from src.db.models import InferenceCostReservation, WorkBoardAttempt, WorkBoardStatus, WorkBoardTask, WorkflowRunState
from src.work_board.repository import BoardError
from src.work_board.research_artifacts import read, verified_child
from src.work_board.research_contracts import PARENT_CAPABILITY, PROMPT_READY, WAIT_CHILDREN, WAIT_SOURCES
from src.work_board.research_readback import binds
from src.workflows.research_native import checkpoint


async def precontact_intent_reusable(jobs, db, run, effects):
    """Recognize only this original funded, never-contacted native intent."""
    from config.settings import settings
    from src.workflows.inference_accounting import _continuity_lock
    from src.workflows.job_runtime import _job_has_unsafe_effects, _serialize
    from src.workflows.research_sources import verify_current_prompt_in_db
    if run.job_kind != "readonly_research_child":
        return False
    operation = "remote:"+run.run_identity
    intents = [item for item in effects if item.get("effect_id") == "remote_inference:"+operation
        and item.get("effect_type") == "remote_inference_admission" and item.get("status") == "intent"]
    if len(intents) != 1 or _job_has_unsafe_effects([item for item in effects if item is not intents[0]]):
        return False
    await verify_current_prompt_in_db(jobs, db, _serialize(run))
    ready = checkpoint(_serialize(run), "research:prompt-ready")
    account, rows = await jobs._accounting_rows(db)
    with _continuity_lock(Path(settings.workspace_dir).resolve()) as workspace:
        jobs._assert_accounting_continuity(workspace, account, rows)
    row = next((item for item in rows if item.operation_id == operation), None)
    details = intents[0].get("details", {})
    parent = await jobs._fetch(db, run.parent_job_id)
    creation = checkpoint(_serialize(parent), "research:creation")
    call_ids = ["remote:"+child_id for child_id in creation["child_ids"]]
    return bool(row and row.job_id == run.run_identity and row.owner_id == run.owner_principal_id
        and row.state == "reserved" and row.contact_started_at is None and not row.recovery_reason
        and row.payload_digest == ready["payload_digest"] and row.policy_digest == ready["policy_digest"]
        and row.deadline_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
        and details.get("job_id") == run.run_identity and details.get("owner_id") == run.owner_principal_id
        and details.get("parent_job_id") == run.parent_job_id
        and details.get("deadline_at") == ready["contact_deadline_at"]
        and all(any(cost.operation_id == call_id and cost.job_id == creation["child_ids"][slot]
            and any(item.get("kind") == "research_group_reservation" and item.get("creation_digest") == creation["creation_digest"]
                and item.get("slot") == slot and item.get("group_call_ids") == call_ids
                for item in json.loads(cost.evidence_json)) for cost in rows) for slot, call_id in enumerate(call_ids)))


async def bound(db, owner, task_id):
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id,
        WorkBoardTask.owner_principal_id == owner.principal_id, WorkBoardTask.owner_session_id == owner.session_id,
        WorkBoardTask.capability_id == PARENT_CAPABILITY))
    if task is None:
        raise BoardError("research_unavailable", "Research task unavailable", status_code=404)
    attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task_id)
        .order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc()).limit(1))
    parent = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id)) if attempt else None
    if parent is None or not binds(task, attempt, parent):
        raise BoardError("research_binding_unavailable", "The original research admission requires recovery", status_code=409)
    return task, attempt, parent


def binding(task, attempt, parent):
    from src.workflows.job_runtime import _serialize
    projection = _serialize(parent)
    creation = checkpoint(projection, "research:creation")
    phase = checkpoint(projection, "research:phase")
    if not creation or not phase:
        raise BoardError("research_phase_unavailable", "The original finite research phase is unavailable", status_code=409)
    return {"task_id": task.task_id, "attempt_id": attempt.attempt_id, "task_revision": task.task_revision,
        "board_fence": attempt.fencing_token, "job_fence": parent.fencing_token,
        "phase": phase["phase"], "creation_digest": creation["creation_digest"]}


async def snapshot(jobs, db, owner, task_id):
    task, attempt, parent = await bound(db, owner, task_id)
    from src.workflows.job_runtime import _serialize
    creation = checkpoint(_serialize(parent), "research:creation")
    children = list((await db.scalars(select(WorkflowRunState).where(WorkflowRunState.parent_job_id == parent.run_identity))).all())
    costs = list((await db.scalars(select(InferenceCostReservation).where(InferenceCostReservation.job_id.in_(
        [child.run_identity for child in children])))).all())
    safe = parent.status == "paused" and parent.failure_reason in {WAIT_SOURCES, WAIT_CHILDREN} and not attempt.ended_at
    safe = safe and all(child.status in {"accepted", "queued", "succeeded"} or (
        child.status == "paused" and child.failure_reason == PROMPT_READY) for child in children)
    safe = safe and not any(child.lease_owner or child.lease_expires_at for child in children)
    safe = safe and not any(cost.state in {"contact_started", "unknown"} or cost.recovery_reason == "provider_contact_denied" for cost in costs)
    return {"task_id": task.task_id, "task_revision": task.task_revision, "attempt_id": attempt.attempt_id,
        "parent_id": parent.run_identity, "status": parent.status, "phase": parent.failure_reason,
        "deadline_at": parent.deadline_at.replace(tzinfo=timezone.utc).isoformat(),
        "creation_digest": creation["creation_digest"] if creation else None,
        "children": [{"job_id": child.run_identity, "status": child.status,
            "reason": child.failure_reason, "attempt_count": child.attempt_count,
            "lease_present": bool(child.lease_owner or child.lease_expires_at)} for child in children],
        "costs": [{"operation_id": cost.operation_id, "job_id": cost.job_id, "state": cost.state,
            "bound_microusd": cost.bound_microusd, "actual_cost_microusd": cost.actual_cost_microusd,
            "contact_started": cost.contact_started_at is not None, "reason": cost.recovery_reason} for cost in costs],
        "recoverable": bool(safe), "cancel_available": not attempt.ended_at and parent.status != "succeeded",
        "report_available": parent.status == "succeeded" and task.status in {WorkBoardStatus.done, WorkBoardStatus.review},
        "no_learning": True, "semantic_truth_verified": False,
        "recovery_limit": "Only the original verified precontact phase or reserved completed output may resume; uncertain contacts remain held."}


async def reserve_recovery(jobs, owner, task_id, request):
    """Commit the phase generation before any resumed physical work."""
    from src.workflows.job_runtime import _digest, _serialize
    from src.workflows.research_sources import current_inputs_in_db
    now = datetime.now(timezone.utc)
    async with jobs._session() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        task, attempt, parent = await bound(db, owner, task_id)
        original = binding(task, attempt, parent)
        prior = checkpoint(_serialize(parent), "research:operator-control")
        if prior and prior.get("idempotency_key") == request.idempotency_key:
            if prior.get("requested_revision") != request.expected_revision or prior.get("action") != "recover":
                raise BoardError("research_control_conflict", "The request identity has different research input", status_code=409)
            if parent.status == "succeeded" or attempt.ended_at:
                return {"replayed": True, "completed": parent.status == "succeeded", "binding": None}
        elif task.task_revision != request.expected_revision:
            raise BoardError("research_revision_stale", "Research changed; reload its current state", status_code=409)
        await current_inputs_in_db(jobs, db, parent.run_identity)
        if (attempt.ended_at or attempt.cancel_requested_at or parent.status != "paused"
            or parent.failure_reason not in {WAIT_SOURCES, WAIT_CHILDREN} or task.status != WorkBoardStatus.blocked
            or task.block_reason != parent.failure_reason or parent.lease_owner or parent.lease_expires_at
            or attempt.lease_owner or attempt.lease_expires_at):
            raise BoardError("research_recovery_unproven", "Research has no verified lease-free wait to resume", status_code=409)
        creation = checkpoint(_serialize(parent), "research:creation")
        children = list((await db.scalars(select(WorkflowRunState).where(WorkflowRunState.parent_job_id == parent.run_identity))).all())
        if sorted(child.run_identity for child in children) != sorted(creation["child_ids"]):
            raise BoardError("research_group_changed", "The original fixed child group is incomplete", status_code=409)
        for child in children:
            authority = json.loads(child.declared_authority_json)
            if (child.job_kind != "readonly_research_child" or child.owner_principal_id != parent.owner_principal_id
                or child.session_id != parent.session_id or child.parent_fencing_token != creation["creation_job_fence"]
                or authority.get("parent_creation_digest") != creation["creation_digest"]):
                raise BoardError("research_group_changed", "Research child lineage changed", status_code=409)
            cost = await db.scalar(select(InferenceCostReservation).where(InferenceCostReservation.job_id == child.run_identity))
            if cost and (cost.state in {"contact_started", "unknown"} or cost.recovery_reason == "provider_contact_denied"):
                raise BoardError("research_contact_unresolved", "The original contact liability remains held; no provider retry is allowed", status_code=409)
            if child.status == "running":
                # Output is written only after the actual broker callback and
                # settlement returned. An expired lease alone is insufficient.
                output = checkpoint(_serialize(child), f"research:artifact:child:{authority['research_slot']}")
                ready = checkpoint(_serialize(child), "research:prompt-ready")
                if (child.lease_expires_at is None or child.lease_expires_at.replace(tzinfo=timezone.utc) > now
                    or not output or not ready or not cost or cost.state != "settled" or cost.actual_cost_microusd is None
                    or cost.contact_started_at is None or cost.operation_id != "remote:"+child.run_identity
                    or cost.payload_digest != ready["payload_digest"] or output["creation_digest"] != creation["creation_digest"]):
                    raise BoardError("research_worker_unproven", "The current worker has no proven completed reserved output", status_code=409)
                sources = json.loads(read(ready["source_manifest_path"], ready["source_manifest_sha256"]))
                verified_child(read(output["file_path"], output["content_sha256"], max_bytes=16384), sources)
                child.status = "queued"
                child.lease_owner = child.lease_expires_at = None
                child.fencing_token += 1
                child.revision += 1
                child.updated_at = now.replace(tzinfo=None)
                db.add(child)
            elif child.lease_owner or child.lease_expires_at or not (child.status in {"accepted", "queued", "succeeded"}
                or child.status == "paused" and child.failure_reason == PROMPT_READY):
                raise BoardError("research_worker_unproven", "The unfinished worker requires explicit reconciliation", status_code=409)
        # Even an exact uncertain retry receives a new current phase fence.
        # This closes a cross-process coordinator that had not claimed a child
        # yet; no deadline, attempt or provider operation identity is renewed.
        task.task_revision += 1
        task.updated_at = now.replace(tzinfo=None)
        parent.revision += 1
        parent.updated_at = now.replace(tzinfo=None)
        payload = {"action": "recover", "idempotency_key": request.idempotency_key,
            "requested_revision": request.expected_revision, "reserved_revision": task.task_revision,
            "creation_digest": creation["creation_digest"], "no_learning": True}
        records = [record for record in json.loads(parent.checkpoint_receipts_json)
            if record.get("checkpoint_id") != "research:operator-control"]
        records.append({"checkpoint_id": "research:operator-control", "payload": payload,
            "state_digest": _digest(payload), "state_keys": sorted(payload), "safe": True,
            "fencing_token": parent.fencing_token, "recorded_at": now.isoformat()})
        parent.checkpoint_receipts_json = json.dumps(records, sort_keys=True, separators=(",", ":"))
        db.add_all([task, parent])
        original["task_revision"] = task.task_revision
        return {"replayed": bool(prior and prior.get("idempotency_key") == request.idempotency_key), "binding": original}


async def request_cancel(jobs, owner, task_id, request):
    from src.workflows.job_runtime import _digest, _serialize
    from src.workflows.research_guard import assert_research_operator_session
    now = datetime.now(timezone.utc)
    async with jobs._session() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        task, attempt, parent = await bound(db, owner, task_id)
        await assert_research_operator_session(db, parent, now=now)
        prior = checkpoint(_serialize(parent), "research:operator-cancel")
        if prior and prior.get("idempotency_key") == request.idempotency_key:
            if prior["requested_revision"] != request.expected_revision:
                raise BoardError("research_control_conflict", "The cancellation request changed", status_code=409)
            return {"parent_id": parent.run_identity, "attempt_id": attempt.attempt_id, "replayed": True}
        if (task.task_revision != request.expected_revision or attempt.ended_at or parent.status == "succeeded"
            or task.status in {WorkBoardStatus.done, WorkBoardStatus.review} or attempt.cancel_requested_at):
            raise BoardError("research_cancel_stale", "Research changed or already has a cancellation request", status_code=409)
        creation = checkpoint(_serialize(parent), "research:creation")
        if not creation:
            raise BoardError("research_cancel_unproven", "Research creation must be reconciled first", status_code=409)
        payload = {"idempotency_key": request.idempotency_key, "requested_revision": request.expected_revision,
            "attempt_id": attempt.attempt_id, "creation_digest": creation["creation_digest"], "no_learning": True}
        records = [item for item in json.loads(parent.checkpoint_receipts_json)
            if item.get("checkpoint_id") != "research:operator-cancel"]
        records.append({"checkpoint_id": "research:operator-cancel", "payload": payload,
            "state_digest": _digest(payload), "state_keys": sorted(payload), "safe": True,
            "fencing_token": parent.fencing_token, "recorded_at": now.isoformat()})
        parent.checkpoint_receipts_json = json.dumps(records, sort_keys=True, separators=(",", ":"))
        parent.revision += 1
        parent.updated_at = now.replace(tzinfo=None)
        attempt.cancel_requested_at = now.replace(tzinfo=None)
        task.task_revision += 1
        task.updated_at = now.replace(tzinfo=None)
        db.add_all([parent, attempt, task])
        return {"parent_id": parent.run_identity, "attempt_id": attempt.attempt_id, "replayed": False}


async def finish_cancel(jobs, owner, task_id, request):
    """Close only persisted safe waits or positively completed owned workers."""
    from config.settings import settings
    from src.workflows.inference_accounting import _continuity_lock, _json
    from src.workflows.job_runtime import _serialize
    from src.workflows.research_guard import assert_research_operator_session
    now = datetime.now(timezone.utc)
    async with jobs._session() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        task, attempt, parent = await bound(db, owner, task_id)
        await assert_research_operator_session(db, parent, now=now)
        cancellation = checkpoint(_serialize(parent), "research:operator-cancel")
        creation = checkpoint(_serialize(parent), "research:creation")
        if (not cancellation or cancellation["idempotency_key"] != request.idempotency_key
            or cancellation["requested_revision"] != request.expected_revision or not attempt.cancel_requested_at):
            raise BoardError("research_cancel_unproven", "The exact cancellation reservation is missing", status_code=409)
        if attempt.ended_at:
            return {"completed": True, "cancelled": attempt.outcome == "research_cancelled", "replayed": True}
        children = list((await db.scalars(select(WorkflowRunState).where(WorkflowRunState.parent_job_id == parent.run_identity))).all())
        if sorted(child.run_identity for child in children) != sorted(creation["child_ids"]):
            raise BoardError("research_cancel_unproven", "The fixed child group changed", status_code=409)
        uncertain = False
        uncertain_ids = set()
        for row in [parent, *children]:
            if row.status == "succeeded":
                continue
            if row.lease_owner or row.lease_expires_at or row.status == "running":
                return {"completed": False, "cancellation_pending": True, "reason": "actual_worker_completion_required"}
            records = json.loads(row.checkpoint_receipts_json)
            proof = checkpoint(_serialize(row), "research:quiescence")
            safe_wait = row.status in {"accepted", "queued"} or (row.status == "paused"
                and row.failure_reason in {WAIT_SOURCES, WAIT_CHILDREN, PROMPT_READY})
            safe_closed = bool(row.status == "blocked" and proof and proof.get("job_id") == row.run_identity
                and proof.get("parent_id") == parent.run_identity and proof.get("board_attempt_id") == attempt.attempt_id
                and proof.get("creation_digest") == creation["creation_digest"]
                and proof.get("closed_fence") == row.fencing_token and proof.get("owned_worker_completed") is True)
            if row.status in {"unknown_external_effect", "cost_liability"}:
                uncertain = True
                uncertain_ids.add(row.run_identity)
            elif not (safe_wait or safe_closed):
                return {"completed": False, "cancellation_pending": True, "reason": "canonical_quiescence_required"}
            if any(item.get("checkpoint_id", "").startswith("research:source-intent:") and not any(
                other.get("checkpoint_id") == "research:artifact:source:"+str(item["payload"]["source_slot"])
                for other in records) for item in records):
                uncertain = True
                uncertain_ids.add(row.run_identity)
        account, costs = await jobs._accounting_rows(db)
        with _continuity_lock(Path(settings.workspace_dir).resolve()) as workspace:
            jobs._assert_accounting_continuity(workspace, account, costs)
            changed = False
            for cost in costs:
                if cost.job_id not in creation["child_ids"]:
                    continue
                if cost.state in {"contact_started", "unknown"}:
                    uncertain = True
                    uncertain_ids.add(cost.job_id)
                    continue
                if cost.state != "reserved":
                    continue
                child = next(row for row in children if row.run_identity == cost.job_id)
                proof = checkpoint(_serialize(child), "research:quiescence")
                # Held denial release also requires the existing real broker's
                # completion evidence bound to this original contact fence.
                denied = cost.recovery_reason == "provider_contact_denied"
                denial_closed = bool(proof and proof.get("completed_execution_fence") == cost.job_fencing_token
                    and any(item.get("kind") == "provider_contact_denial_quiesced"
                        and item.get("job_id") == cost.job_id and item.get("root_job_id") == child.root_run_identity
                        and item.get("job_fence") == cost.job_fencing_token and item.get("provider_never_contacted") is True
                        and item.get("callback_completed") is True for item in json.loads(cost.evidence_json)))
                if cost.contact_started_at is not None or (denied and not denial_closed):
                    continue
                cost.state = "released"
                cost.recovery_reason = "cancelled_before_contact"
                cost.revision += 1
                cost.updated_at = now.replace(tzinfo=None)
                history = json.loads(cost.evidence_json)
                history.append({"kind": "released", "reason": "cancelled_before_contact",
                    "job_id": cost.job_id, "creation_digest": creation["creation_digest"],
                    "closed_fence": child.fencing_token, "memory_status": "no_learning"})
                cost.evidence_json = _json(history)
                db.add(cost)
                changed = True
            for row in [*children, parent]:
                if row.status in {"succeeded", "unknown_external_effect", "cost_liability"}:
                    continue
                row.status = "unknown_external_effect" if row.run_identity in uncertain_ids or (
                    uncertain and row.run_identity == parent.run_identity) else "cancelled"
                row.failure_reason = "research_contact_or_source_unresolved" if row.status == "unknown_external_effect" else "operator_cancelled"
                row.fencing_token += 1
                row.revision += 1
                row.finished_at = row.updated_at = now.replace(tzinfo=None)
                db.add(row)
            task.status = WorkBoardStatus.blocked
            task.block_kind = "unknown_effect" if uncertain else "needs_input"
            task.block_reason = "research_contact_or_source_unresolved" if uncertain else "research_cancelled"
            task.task_revision += 1
            task.updated_at = now.replace(tzinfo=None)
            attempt.outcome = task.block_reason
            attempt.ended_at = attempt.updated_at = now.replace(tzinfo=None)
            attempt.lease_owner = attempt.lease_expires_at = None
            db.add_all([task, attempt])
            if changed:
                await jobs._persist_accounting_witness(db, workspace, account, costs)
        return {"completed": True, "cancelled": not uncertain, "unknown_liability_preserved": uncertain}
