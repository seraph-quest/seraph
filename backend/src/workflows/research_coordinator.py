"""Finite phases for one research parent and its fixed native children."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json

from sqlalchemy import select, text

from src.db.models import InferenceCostReservation, WorkBoardAttempt, WorkBoardStatus, WorkBoardTask, WorkflowRunState
from src.work_board.research_artifacts import dossier_bytes, json_bytes, read, verified_child, write_verified
from src.work_board.research_contracts import WAIT_CHILDREN, WAIT_SOURCES
from src.workflows.research_accounting import fund_fixed_group
from src.workflows.research_native import checkpoint, create_fixed_children
from src.workflows.research_provider import execute_funded_child, prepare_prompt
from src.workflows.research_sources import acquire_source, current_inputs
from src.workflows.research_waits import pause_parent, resume_parent


async def start_parent(jobs, *, parent_id, owner, board_task, board_attempt, inputs):
    parent = await jobs.get_job(parent_id)
    creation = await create_fixed_children(jobs, parent_id=parent_id, runtime_owner=owner,
        runtime_fence=parent["lease"]["fencing_token"], task_id=board_task.task_id,
        attempt_id=board_attempt.attempt_id, board_revision=board_task.task_revision,
        board_fence=board_attempt.fencing_token, board_owner=owner, inputs=inputs)
    await pause_parent(jobs, parent_id=parent_id, owner=owner, job_fence=parent["lease"]["fencing_token"],
        board_fence=board_attempt.fencing_token, board_revision=board_task.task_revision, reason=WAIT_SOURCES)
    return creation


async def _prepare_child(jobs, child_id, owner):
    child = await jobs.get_job(child_id)
    if child["status"] == "paused" and child["failure_reason"] == "research_prompt_ready":
        ready = checkpoint(child, "research:prompt-ready")
        read(ready["file_path"], ready["content_sha256"])
        read(ready["source_manifest_path"], ready["source_manifest_sha256"])
        return
    if child["status"] == "accepted":
        await jobs.queue_job(child_id)
    elif child["status"] != "queued":
        raise ValueError("research source preparation requires exact recovery of its original phase")
    child = await jobs.claim_job(child_id, owner=owner, lease_seconds=60,
        continue_existing_attempt=bool(child["attempt_count"]))
    fence = child["lease"]["fencing_token"]
    inputs = await current_inputs(jobs, child["parent_job_id"])
    slot = child["declared_authority"]["research_slot"]
    sources = []
    for source_slot in inputs.perspectives[slot].source_slots:
        source = await acquire_source(jobs, child_id=child_id,
            owner=owner, fence=fence, source_slot=source_slot)
        if source is None:
            raise ValueError("research shared source producer has not completed its verified artifact")
        sources.append(source[0])
    await prepare_prompt(jobs, child_id=child_id, owner=owner, fence=fence, sources=sources)


async def _execute_child(jobs, child_id, owner):
    child = await jobs.get_job(child_id)
    if child["status"] in {"succeeded", "failed", "blocked", "unknown_external_effect", "cost_liability", "cancelled"}:
        return
    if child["status"] == "paused" and child["failure_reason"] == "research_prompt_ready":
        await jobs.resume_job(child_id, expected_revision=child["revision"], reason="research_group_funded")
    elif child["status"] != "queued":
        raise ValueError("research execution requires exact original pre-contact recovery")
    claimed = await jobs.claim_job(child_id, owner=owner, lease_seconds=60, continue_existing_attempt=True)
    await execute_funded_child(jobs, child_id=child_id, owner=owner, fence=claimed["lease"]["fencing_token"])


async def continue_parent(jobs, *, parent_id, owner):
    """At most two source workers, two synthesis callbacks and one assembly."""
    parent = await jobs.get_job(parent_id)
    creation = checkpoint(parent, "research:creation")
    if creation is None or len(creation["child_ids"]) not in {1, 2}:
        raise ValueError("research immutable fixed group is unavailable")
    await current_inputs(jobs, parent_id)
    if parent["status"] == "paused" and parent["failure_reason"] == WAIT_SOURCES:
        # Producer slots run first, so a shared source never causes polling or
        # a second network GET. The parent owns neither execution lease here.
        for child_id in creation["child_ids"]:
            await _prepare_child(jobs, child_id, owner)
        phase = await resume_parent(jobs, parent_id=parent_id, owner=owner, phase="research_funding")
        await fund_fixed_group(jobs, parent_id=parent_id, owner=owner, fencing_token=phase["job_fence"])
        await pause_parent(jobs, parent_id=parent_id, owner=owner, job_fence=phase["job_fence"],
            board_fence=phase["board_fence"], board_revision=phase["task_revision"], reason=WAIT_CHILDREN)
        parent = await jobs.get_job(parent_id)
    if parent["status"] != "paused" or parent["failure_reason"] != WAIT_CHILDREN:
        raise ValueError("research parent is outside its exact original child wait")
    # Both slots enter the existing broker; that broker selects priority and
    # enforces the sole remote lane. No parent inference is queued.
    results = await asyncio.gather(*(_execute_child(jobs, child_id, owner)
        for child_id in creation["child_ids"]), return_exceptions=True)
    failures = [result for result in results if isinstance(result, BaseException)]
    if failures:
        raise failures[0]
    phase = await resume_parent(jobs, parent_id=parent_id, owner=owner, phase="research_assembly")
    inputs = await current_inputs(jobs, parent_id)
    verified = []
    child_refs = []
    for slot, child_id in enumerate(creation["child_ids"]):
        child = await jobs.get_job(child_id)
        if child["status"] != "succeeded":
            raise ValueError("research incomplete child remains visible and cannot be adopted")
        ready = checkpoint(child, "research:prompt-ready")
        output = checkpoint(child, f"research:artifact:child:{slot}")
        sources = json.loads(read(ready["source_manifest_path"], ready["source_manifest_sha256"]))
        content = read(output["file_path"], output["content_sha256"], max_bytes=16384)
        verified.append(verified_child(content, sources))
        child_refs.append({"child_id": child_id, "output_sha256": output["content_sha256"],
            "source_manifest_sha256": ready["source_manifest_sha256"], "payload_digest": ready["payload_digest"]})
    manifest = {"schema_version": 1, "parent_id": parent_id, "creation_digest": creation["creation_digest"],
        "children": child_refs, "adopted_claims": [{"slot": slot, "claim_index": index}
            for slot, child in enumerate(verified) for index, claim in enumerate(child["claims"])
            if claim["evidence_status"] == "mechanically_verified"],
        "unverified_claims_adopted": False, "semantic_truth_verified": False, "no_learning": True}
    await write_verified(jobs, job_id=parent_id, owner=owner, fence=phase["job_fence"],
        creation_digest=creation["creation_digest"], slot=0, kind="manifest", content=json_bytes(manifest))
    dossier = await write_verified(jobs, job_id=parent_id, owner=owner, fence=phase["job_fence"],
        creation_digest=creation["creation_digest"], slot=0, kind="dossier", content=dossier_bytes(inputs.question, verified))
    await current_inputs(jobs, parent_id)
    await jobs.transition_job(parent_id, "succeeded", owner=owner, fencing_token=phase["job_fence"],
        result={"output_sha256": dossier["content_sha256"], "child_count": len(verified), "no_learning": True},
        result_summary="Literal attributed research dossier; semantic truth unverified; no_learning")
    return {**phase, "dossier": dossier, "manifest": manifest}


async def freeze_quiescent(jobs, *, parent_id, reason):
    """Commit a blocked unfinished group after its awaited workers returned.

    This is not cancellation success or cost forgiveness. Contacted unknown
    rows and their debt remain visible, and no row is adopted or retried.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with jobs._session() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        parent = await jobs._fetch(db, parent_id)
        creation = json.loads(parent.checkpoint_receipts_json)
        created = next((item["payload"] for item in creation if item.get("checkpoint_id") == "research:creation"), None)
        if created is None:
            raise ValueError("research freeze requires its immutable creation record")
        attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == created["board_attempt_id"]))
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == created["board_task_id"]))
        if attempt is None or task is None or attempt.workflow_run_id != parent_id or task.owner_principal_id != parent.owner_principal_id:
            raise ValueError("research freeze Board binding changed")
        rows = list((await db.scalars(select(WorkflowRunState).where(
            WorkflowRunState.run_identity.in_([parent_id, *created["child_ids"]])))).all())
        for row in rows:
            if row.status in {"succeeded", "cancelled", "unknown_external_effect", "cost_liability"}:
                continue
            cost = await db.scalar(select(InferenceCostReservation).where(InferenceCostReservation.job_id == row.run_identity))
            row.status = "unknown_external_effect" if cost and cost.state in {"contact_started", "unknown"} else "blocked"
            row.failure_reason = reason
            row.lease_owner = row.lease_expires_at = None
            row.fencing_token += 1
            row.revision += 1
            row.updated_at = now
            db.add(row)
        task.status = WorkBoardStatus.blocked
        task.block_kind = "unknown_effect" if any(row.status == "unknown_external_effect" for row in rows) else "needs_input"
        task.block_reason = reason
        task.task_revision += 1
        task.updated_at = now
        attempt.lease_owner = attempt.lease_expires_at = None
        attempt.outcome = reason
        attempt.updated_at = now
        db.add_all([task, attempt])
