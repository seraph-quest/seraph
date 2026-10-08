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


from contextlib import asynccontextmanager
from contextvars import ContextVar

_programme_policy = ContextVar("native_goal_discovery_policy", default=None)


def current_discovery_witness():
    import asyncio
    held = _programme_policy.get()
    if held is None or held[0] is not asyncio.current_task() or held[2] is None:
        from src.workflows.job_runtime import DurableJobTransitionError
        raise DurableJobTransitionError("programme physical witness unavailable")
    return held[2]


@asynccontextmanager
async def discovery_writer_scope(*, witness=None):
    """Current configuration owner fences only the short native writer.

    Release before any physical HTTP or artifact work. Possession of a staged
    policy never authenticates an issuer or grants a new generation.
    """
    from src.model_fabric.effective_policy import configuration_mutation_lock
    from src.guardian.goal_programmes import stage_programme_policy
    import asyncio
    held = _programme_policy.get()
    if held is not None and held[0] is asyncio.current_task():
        if witness is not None and held[2] is not witness:
            token = _programme_policy.set((held[0], held[1], witness))
            try:
                yield held[1]
            finally:
                _programme_policy.reset(token)
            return
        yield held[1]
        return
    async with configuration_mutation_lock:
        policy = stage_programme_policy()
        token = _programme_policy.set((asyncio.current_task(), policy, witness))
        try:
            yield policy
        finally:
            _programme_policy.reset(token)


async def assert_discovery_authority(db, value, *, run=None):
    """DB-only programme validation in the existing original job writer."""
    from src.work_board.research_parent import discovery_authority, DISCOVERY_KIND, DISCOVERY_SERVICE
    from src.guardian.goal_programmes import goal_programme_service
    from src.workflows.job_runtime import DurableJobTransitionError
    authority = discovery_authority(value)
    import asyncio
    held = _programme_policy.get()
    if held is None or held[0] is not asyncio.current_task():
        raise DurableJobTransitionError("programme transition requires its native configuration writer scope")
    policy = held[1]
    binding = authority.programme_binding
    if run is None:
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == authority.original_job_id))
    if run is not None and (run.job_kind != DISCOVERY_KIND or run.owner_kind != "service"
            or run.service_id != DISCOVERY_SERVICE or run.owner_principal_id != DISCOVERY_SERVICE
            or run.session_id is not None or run.operator_session_id is not None
            or run.run_identity != authority.original_job_id or run.parent_job_id
            or run.branch_depth != 0 or run.goal_id != binding.goal_id
            or run.goal_revision != binding.goal_revision):
        raise DurableJobTransitionError("programme original native lineage changed")
    if run is not None:
        from src.workflows.research_sources import DiscoveryInputWitness
        from src.workflows.job_runtime import _digest
        witness = held[2]
        if (not isinstance(witness, DiscoveryInputWitness) or witness.job_id != run.run_identity
                or witness.input_digest != run.input_digest or witness.authority_digest != run.authority_digest
                or witness.checkpoint_digest != _digest(json.loads(run.checkpoint_receipts_json))
                or witness.artifact_digest != _digest(json.loads(run.artifact_receipts_json))):
            raise DurableJobTransitionError("programme physical readback is not current at this writer")
    programme = await goal_programme_service.validate_current_binding(db=db, binding=binding, policy=policy)
    from src.guardian.research_plan_contracts import STAGES
    if not {capability for _, capability, _ in STAGES} <= set(programme.capability_ids):
        raise DurableJobTransitionError("programme fixed stage capability grant changed")
    return programme
