"""Research-only paired Board/job waits; the original attempt remains open."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, text, update

from src.db.models import WorkBoardAttempt, WorkBoardStatus, WorkBoardTask, WorkflowRunState
from src.work_board.research_contracts import PARENT_CAPABILITY, PARENT_KIND, WAIT_CHILDREN, WAIT_SOURCES, PROMPT_READY
from src.workflows.research_accounting import payload_checkpoint


async def _current(jobs, db, parent_id):
    from src.workflows.job_runtime import _assert_canonical_goal_fence, DurableJobLeaseError
    parent = await jobs._fetch(db, parent_id)
    from src.workflows.research_guard import assert_research_operator_session
    await assert_research_operator_session(db, parent, now=datetime.now(timezone.utc))
    attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.workflow_run_id == parent_id))
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == attempt.task_id)) if attempt else None
    if (parent.job_kind != PARENT_KIND or parent.capability_version != "1" or task is None
        or task.capability_id != PARENT_CAPABILITY or attempt.ended_at or attempt.cancel_requested_at
        or parent.owner_principal_id != task.owner_principal_id or parent.session_id != task.owner_session_id):
        raise DurableJobLeaseError("fixed research wait binding changed")
    await _assert_canonical_goal_fence(db, goal_id=parent.goal_id, goal_revision=parent.goal_revision,
        owner_kind=parent.owner_kind, owner_principal_id=parent.owner_principal_id,
        session_id=parent.session_id, authority=parent.declared_authority_json)
    creation = payload_checkpoint(parent,"research:creation")
    if creation["board_attempt_id"] != attempt.attempt_id or creation["board_task_id"] != task.task_id:
        raise DurableJobLeaseError("immutable research creation attempt changed")
    return parent, task, attempt, creation


def _phase_receipts(parent, creation, phase, fence, now):
    from src.workflows.job_runtime import _digest
    payload = {"creation_digest":creation["creation_digest"], "phase":phase, "no_learning":True}
    records = [item for item in json.loads(parent.checkpoint_receipts_json) if item.get("checkpoint_id") != "research:phase"]
    records.append({"checkpoint_id":"research:phase", "state_digest":_digest(payload),
        "state_keys":sorted(payload), "safe":True, "payload":payload,
        "fencing_token":fence, "recorded_at":now.isoformat()})
    return json.dumps(records,sort_keys=True,separators=(",",":"))


async def pause_parent(jobs, *, parent_id, owner, job_fence, board_fence, board_revision, reason):
    """Release both execution leases in the same existing SQLite writer."""
    from src.workflows.job_runtime import DurableJobLeaseError
    if reason not in {WAIT_SOURCES, WAIT_CHILDREN}:
        raise ValueError("only the two fixed research waits exist")
    now=datetime.now(timezone.utc)
    async with jobs._session() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        parent, task, attempt, creation = await _current(jobs,db,parent_id)
        jobs._assert_lease(parent,owner=owner,fencing_token=job_fence)
        if (parent.status != "running" or task.status != WorkBoardStatus.running
            or task.task_revision != board_revision or attempt.fencing_token != board_fence
            or attempt.lease_owner != owner or attempt.lease_expires_at is None
            or attempt.lease_expires_at.replace(tzinfo=timezone.utc) <= now):
            raise DurableJobLeaseError("research pause lost the current joint lease")
        parent.status="paused"
        parent.failure_reason=reason
        parent.lease_owner=parent.lease_expires_at=None
        parent.checkpoint_receipts_json=_phase_receipts(parent,creation,reason,job_fence,now)
        parent.revision+=1
        parent.updated_at=now.replace(tzinfo=None)
        task.status=WorkBoardStatus.blocked
        task.block_kind="needs_input"
        task.block_reason=reason
        task.block_source_status=WorkBoardStatus.running.value
        task.task_revision+=1
        task.updated_at=now.replace(tzinfo=None)
        attempt.lease_owner=attempt.lease_expires_at=None
        attempt.outcome=reason
        attempt.updated_at=now.replace(tzinfo=None)
        db.add_all([parent,task,attempt])
        await db.flush()
        return {"task_id":task.task_id,"attempt_id":attempt.attempt_id,"task_revision":task.task_revision,
            "board_fence":attempt.fencing_token,"job_fence":parent.fencing_token,"phase":reason}


async def resume_parent(jobs, *, parent_id, owner, phase, expected_binding=None):
    """Fresh leases on the original attempt; neither deadline nor count renews."""
    from src.workflows.job_runtime import DurableJobLeaseError
    from src.model_fabric.effective_policy import current_inference_policy
    from src.work_board.pipelines import root_binding
    from src.work_board.pipeline_contracts import digest
    if phase not in {"research_funding","research_assembly"}:
        raise ValueError("only fixed funding and assembly may resume a parent")
    now=datetime.now(timezone.utc)
    async with jobs._session() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        parent,task,attempt,creation=await _current(jobs,db,parent_id)
        if expected_binding is not None:
            await assert_phase_binding(db, parent, expected_binding)
        reason=WAIT_SOURCES if phase == "research_funding" else WAIT_CHILDREN
        authority=json.loads(parent.declared_authority_json)
        if (parent.status != "paused" or parent.failure_reason != reason or task.status != WorkBoardStatus.blocked
            or task.block_reason != reason or parent.lease_owner or parent.lease_expires_at
            or attempt.lease_owner or attempt.lease_expires_at or parent.deadline_at.replace(tzinfo=timezone.utc) <= now
            or authority.get("live_root_digest") != digest(root_binding())
            or authority.get("model_policy_digest") != current_inference_policy()[1]
            or payload_checkpoint(parent,"research:phase").get("phase") != reason):
            raise DurableJobLeaseError("research original wait authority changed")
        children=list((await db.scalars(select(WorkflowRunState).where(WorkflowRunState.parent_job_id == parent_id))).all())
        if sorted(item.run_identity for item in children) != sorted(creation["child_ids"]):
            raise DurableJobLeaseError("fixed child group is incomplete")
        for child in children:
            child_authority=json.loads(child.declared_authority_json)
            if (child.job_kind != "readonly_research_child" or child.parent_fencing_token != creation["creation_job_fence"]
                or child_authority.get("parent_creation_digest") != creation["creation_digest"]
                or child.lease_owner or child.lease_expires_at
                or (phase == "research_funding" and (child.status != "paused" or child.failure_reason != PROMPT_READY))
                or (phase == "research_assembly" and child.status not in {"succeeded","failed","blocked","unknown_external_effect","cost_liability","cancelled"})):
                raise DurableJobLeaseError("research children have not reached the exact bounded wait result")
        expiry=min(parent.deadline_at.replace(tzinfo=timezone.utc),now+timedelta(seconds=30)).replace(tzinfo=None)
        parent.fencing_token+=1
        parent.status="running"
        parent.failure_reason=None
        parent.lease_owner=owner
        parent.lease_expires_at=expiry
        parent.revision+=1
        parent.heartbeat_at=parent.updated_at=now.replace(tzinfo=None)
        parent.checkpoint_receipts_json=_phase_receipts(parent,creation,phase,parent.fencing_token,now)
        task.status=WorkBoardStatus.running
        task.block_kind=task.block_reason=task.block_source_status=None
        task.task_revision+=1
        task.updated_at=now.replace(tzinfo=None)
        attempt.fencing_token+=1
        attempt.lease_owner=owner
        attempt.lease_expires_at=expiry
        attempt.heartbeat_at=attempt.updated_at=now.replace(tzinfo=None)
        db.add_all([parent,task,attempt])
        await db.flush()
        return {"task_id":task.task_id,"attempt_id":attempt.attempt_id,"task_revision":task.task_revision,
            "board_fence":attempt.fencing_token,"job_fence":parent.fencing_token,"phase":phase}


async def assert_phase_binding(db, parent, binding):
    """Bind a coordinator to the exact current canonical Board/job phase."""
    from src.workflows.job_runtime import DurableJobLeaseError
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.get("task_id")))
    attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == binding.get("attempt_id")))
    creation = payload_checkpoint(parent, "research:creation")
    phase = payload_checkpoint(parent, "research:phase")
    if (task is None or attempt is None or attempt.task_id != task.task_id
        or attempt.workflow_run_id != parent.run_identity or attempt.ended_at or attempt.cancel_requested_at
        or task.task_revision != binding.get("task_revision") or attempt.fencing_token != binding.get("board_fence")
        or parent.fencing_token != binding.get("job_fence") or phase.get("phase") != binding.get("phase")
        or creation.get("creation_digest") != binding.get("creation_digest")
        or creation.get("board_task_id") != task.task_id or creation.get("board_attempt_id") != attempt.attempt_id):
        raise DurableJobLeaseError("research coordinator phase reservation changed")
