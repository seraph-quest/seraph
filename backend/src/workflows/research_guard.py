"""Research-only parent gate; every other child keeps its live-parent fence."""
from __future__ import annotations

import json
from sqlalchemy import and_, false, func, or_, select
from sqlalchemy.orm import aliased

from src.db.models import OperatorSession, WorkBoardAttempt, WorkBoardStatus, WorkBoardTask, WorkflowRunState
from src.work_board.research_contracts import CHILD_KIND, PARENT_CAPABILITY, PARENT_KIND, WAIT_CHILDREN, WAIT_SOURCES


async def assert_research_operator_session(db, run, *, now):
    """Pure exact owner-Root predicate inside the current native writer."""
    from src.workflows.job_runtime import DurableJobLeaseError
    if run.operator_session_id != run.session_id or await db.scalar(select(OperatorSession.id).where(
        OperatorSession.id == run.operator_session_id, OperatorSession.principal_id == run.owner_principal_id,
        OperatorSession.revoked_at.is_(None), OperatorSession.replaced_by_id.is_(None),
        OperatorSession.is_bearer_tombstone.is_(False), OperatorSession.idle_expires_at > now,
        OperatorSession.absolute_expires_at > now)) is None:
        raise DurableJobLeaseError("research original operator Root session is inactive")


def append_research_parent_gate(conditions, run, *, now):
    """Return True only for the fixed child kind, including malformed denials."""
    if run.job_kind != CHILD_KIND:
        return False
    try:
        authority = json.loads(run.declared_authority_json)
        from src.work_board.pipelines import root_binding
        from src.work_board.pipeline_contracts import digest
        if (run.capability_version != "1" or run.branch_depth != 1
            or authority["research_slot"] not in {0, 1}
            or authority["live_root_digest"] != digest(root_binding())
            or authority["capability_id"] != "work.readonly-research-child.v1"
            or not run.parent_job_id or not run.parent_fencing_token):
            raise ValueError("fixed research lineage unavailable")
        creation_digest = authority["parent_creation_digest"]
        task_id, attempt_id = authority["parent_board_task_id"], authority["parent_board_attempt_id"]
    except (KeyError, TypeError, ValueError):
        conditions.append(false())
        return True
    parent, task, attempt = aliased(WorkflowRunState), aliased(WorkBoardTask), aliased(WorkBoardAttempt)
    original_session = select(OperatorSession.id).where(
        OperatorSession.id == parent.operator_session_id,
        OperatorSession.principal_id == parent.owner_principal_id,
        OperatorSession.revoked_at.is_(None), OperatorSession.replaced_by_id.is_(None),
        OperatorSession.is_bearer_tombstone.is_(False),
        OperatorSession.idle_expires_at > now, OperatorSession.absolute_expires_at > now).exists()
    checkpoints = func.json_each(parent.checkpoint_receipts_json).table_valued("key", "value").alias()
    creation = select(checkpoints.c.key).where(
        func.json_extract(checkpoints.c.value, "$.checkpoint_id") == "research:creation",
        func.json_extract(checkpoints.c.value, "$.payload.creation_digest") == creation_digest,
        func.json_extract(checkpoints.c.value, "$.payload.creation_job_fence") == run.parent_fencing_token,
        func.json_extract(checkpoints.c.value, "$.payload.board_attempt_id") == attempt_id,
        func.json_extract(checkpoints.c.value, "$.payload.board_task_id") == task_id,
    ).exists()
    phases = func.json_each(parent.checkpoint_receipts_json).table_valued("key", "value").alias()
    phase = func.json_extract(phases.c.value, "$.payload.phase")
    current_phase = select(phases.c.key).where(
        func.json_extract(phases.c.value, "$.checkpoint_id") == "research:phase",
        func.json_extract(phases.c.value, "$.payload.creation_digest") == creation_digest,
        or_(
            and_(parent.status == "paused", parent.failure_reason.in_([WAIT_SOURCES, WAIT_CHILDREN]),
                phase == parent.failure_reason, parent.lease_owner.is_(None), parent.lease_expires_at.is_(None),
                task.status == WorkBoardStatus.blocked, task.block_reason == parent.failure_reason,
                attempt.lease_owner.is_(None), attempt.lease_expires_at.is_(None)),
            and_(parent.status == "running", phase.in_(["research_funding", "research_assembly"]),
                parent.lease_owner.is_not(None), parent.lease_expires_at > now,
                task.status == WorkBoardStatus.running,
                attempt.lease_owner.is_not(None), attempt.lease_expires_at > now),
        ),
    ).exists()
    conditions.append(select(parent.id).join(task, task.task_id == task_id)
        .join(attempt, and_(attempt.attempt_id == attempt_id, attempt.task_id == task.task_id))
        .where(parent.run_identity == run.parent_job_id, parent.job_kind == PARENT_KIND,
            parent.capability_version == "1", parent.parent_job_id.is_(None), parent.branch_depth == 0,
            parent.owner_kind == "user", parent.owner_principal_id == run.owner_principal_id,
            parent.session_id == run.session_id, parent.operator_session_id == run.operator_session_id,
            parent.operator_session_id == parent.session_id, original_session,
            parent.root_run_identity == run.root_run_identity,
            parent.goal_id == run.goal_id, parent.goal_revision == run.goal_revision,
            parent.deadline_at > now,
            task.capability_id == PARENT_CAPABILITY, task.goal_id == run.goal_id, task.goal_revision == run.goal_revision,
            task.owner_principal_id == run.owner_principal_id, task.owner_session_id == run.session_id,
            task.typed_input_digest == func.json_extract(parent.declared_authority_json, "$.typed_input_digest"),
            task.input_artifact_id == func.json_extract(parent.declared_authority_json, "$.input_artifact_id"),
            attempt.workflow_run_id == parent.run_identity,
            attempt.ended_at.is_(None), attempt.cancel_requested_at.is_(None), creation, current_phase).exists())
    return True


async def assert_research_parent_current(db, run):
    if run.job_kind != CHILD_KIND:
        return
    from datetime import datetime, timezone
    from src.workflows.job_runtime import DurableJobLeaseError
    conditions = [WorkflowRunState.run_identity == run.run_identity]
    append_research_parent_gate(conditions, run, now=datetime.now(timezone.utc))
    if await db.scalar(select(WorkflowRunState.id).where(*conditions)) is None:
        raise DurableJobLeaseError("canonical research parent creation/current phase is unavailable")
