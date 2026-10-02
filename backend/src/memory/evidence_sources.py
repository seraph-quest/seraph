"""Exact output-artifact ownership proof shared with identity recovery."""
from __future__ import annotations

import re
import json
from src.workflows.job_runtime import _digest
from sqlalchemy import func, or_, select

from src.db.models import Goal, WorkBoardAttempt, WorkBoardTask, WorkflowRunState


async def verified_task_outputs(db, task, attempt, parent):
    """Resolve only a direct run or the dispatcher's named snapshot child."""
    from src.memory.evidence_working_set import _read_file, _verified_receipts
    from src.work_board.dispatcher import WorkBoardDispatcher, GOAL_SNAPSHOT_CAPABILITY
    from src.workflows.job_runtime import _serialize
    if task.capability_id != GOAL_SNAPSHOT_CAPABILITY:
        return [(parent, receipt) for receipt in _verified_receipts(parent)] if run_has_task_owner(task, attempt, parent) else []
    child_id = f"goal-snapshot-work-board:{task.task_id}:{attempt.attempt_id}"
    child = (await db.execute(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == child_id))).scalar_one_or_none()
    if child is None or parent.status != "succeeded" or attempt.outcome != "verified":
        return []
    try:
        dispatcher = WorkBoardDispatcher()
        authority = json.loads(parent.declared_authority_json or "{}")
        spec, inputs, expected_parent_id, _, _ = dispatcher._build_spec(task, attempt,
            runtime_seconds=authority["limits"]["runtime_seconds"])
        if (parent.run_identity != expected_parent_id or parent.authority_digest != _digest(authority)
            or authority != spec.declared_authority or parent.input_digest != _digest(spec.inputs)
            or not re.fullmatch(r"[a-f0-9]{64}", child.authority_digest or "")):
            return []
        projection = _serialize(parent)
        # Replay the dispatcher's completed-root check at the same persisted
        # fence; status is the only field that changes on successful settlement.
        projection["status"] = "running"
        if not dispatcher._board_root_lineage_matches(task, attempt, projection,
            job_id=parent.run_identity, lease_owner=parent.lease_owner,
            fencing_token=parent.fencing_token):
            return []
        if not dispatcher._goal_snapshot_child_lineage_matches(task, attempt, _serialize(child),
            child_job_id=child_id, parent_job_id=parent.run_identity,
            parent_fencing_token=parent.fencing_token):
            return []
        # The child authority ledger is intentionally sanitized, so its full
        # original digest cannot be reconstructed from that projection. The
        # dispatcher's typed named-owner/goal authority check remains mandatory.
        proof = dispatcher._workflow_readback(_serialize(child), child_id)
        if proof is None:
            return []
        receipts = json.loads(child.artifact_receipts_json or "[]")
        effects = json.loads(parent.effect_receipts_json or "[]")
        result = []
        for receipt in receipts:
            from src.artifacts.registry import artifact_id_for
            if (not isinstance(receipt, dict) or receipt.get("exists") is not True
                or receipt.get("producer") != child.job_kind
                or receipt.get("content_sha256") != proof["content_sha256"]
                or receipt.get("artifact_id") != proof["readback_id"]
                or receipt.get("artifact_id") != artifact_id_for(file_path=receipt.get("file_path"),
                    artifact_type=receipt.get("artifact_type"), producer=receipt.get("producer"),
                    run_id=child_id, content_sha256=receipt.get("content_sha256"))):
                continue
            if receipt["artifact_type"] != "goal_snapshot" or receipt["file_path"] != inputs.get("file_path"):
                continue
            if not any(isinstance(e, dict) and e.get("effect_type") == "board_child_readback"
                and e.get("receipt_kind") == "readback" and e.get("status") == "succeeded"
                and e.get("target_path") == receipt["file_path"]
                and e.get("target_digest") == receipt["content_sha256"]
                and e.get("content_sha256") == receipt["content_sha256"]
                and e.get("fencing_token") == parent.fencing_token
                and isinstance(e.get("details"), dict) and e["details"].get("verified") is True
                and e["details"].get("child_job_id") == child_id
                and e["details"].get("artifact_id") == receipt["artifact_id"]
                and e.get("readback_id") == proof["readback_id"]
                and e.get("verified_at") == proof["verified_at"] for e in effects):
                continue
            _read_file(receipt["file_path"], receipt["content_sha256"])
            result.append((child, receipt))
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return []


def run_has_task_owner(task: WorkBoardTask, attempt: WorkBoardAttempt, run: WorkflowRunState) -> bool:
    if (run.operator_session_id != task.owner_session_id or run.goal_id != task.goal_id
        or run.goal_revision != task.goal_revision or attempt.task_id != task.task_id
        or attempt.workflow_run_id != run.run_identity):
        return False
    if run.owner_kind == "user":
        return (task.capability_id not in {"browser.public-task.v1", "guardian.research-watch.v1"}
                and run.owner_principal_id == task.owner_principal_id)
    delegated = {
        "browser.public-task.v1": ("browser_public_task", "service:browser-task", "service:browser-task"),
        "guardian.research-watch.v1": ("guardian_source_watch", "service:guardian-source-watch", "guardian-source-watch"),
    }.get(task.capability_id)
    if delegated is None or run.owner_kind != "service":
        return False
    try:
        authority = json.loads(run.declared_authority_json or "{}")
    except (ValueError, TypeError):
        return False
    if not isinstance(authority, dict) or _digest(authority) != run.authority_digest:
        return False
    kind, principal, service_id = delegated
    if (run.job_kind != kind or run.owner_principal_id != principal or run.service_id != service_id
        or authority.get("principal") != principal or authority.get("owner_kind") != "service"
        or authority.get("service_id") != service_id or authority.get("capability_id") != task.capability_id
        or authority.get("goal_owner_principal_id") != task.owner_principal_id
        or authority.get("goal_owner_session_id") != task.owner_session_id
        or run.idempotency_scope != "work-board-attempt"
        or run.idempotency_key != f"{task.task_id}:{attempt.attempt_id}"):
        return False
    if task.capability_id == "browser.public-task.v1":
        return (authority.get("operator_owner_principal_id") == task.owner_principal_id
            and authority.get("operator_owner_session_id") == task.owner_session_id
            and authority.get("goal_id") == task.goal_id and authority.get("goal_revision") == task.goal_revision
            and authority.get("board_fencing_token") == attempt.fencing_token
            and authority.get("board_task_revision") == attempt.task_revision_at_claim + 1
            and authority.get("input_artifact_id") == task.input_artifact_id
            and authority.get("input_artifact_digest") == task.typed_input_digest
            and bool(task.input_artifact_id) and bool(task.typed_input_digest))
    return (authority.get("session_id") == task.owner_session_id
        and authority.get("goal_id") == task.goal_id and authority.get("goal_revision") == task.goal_revision
        and authority.get("plan_revision") == run.plan_revision)


async def output_artifact_scope(db, record_id: str) -> dict[str, str | int] | None:
    """Return neutral immutable ownership only after the entire output proof.

    This is discovery metadata, never read, execution or model authority. The
    caller must independently prove identity/selection and source permission.
    """
    if not re.fullmatch(r"art_[a-f0-9]{24}", record_id):
        return None
    from src.memory.evidence_working_set import _read_file, _verified_receipts

    rows = (await db.execute(select(WorkBoardTask, WorkBoardAttempt, WorkflowRunState, Goal).join(
        WorkBoardAttempt, WorkBoardAttempt.task_id == WorkBoardTask.task_id).join(
        WorkflowRunState, WorkflowRunState.run_identity == WorkBoardAttempt.workflow_run_id).join(
        Goal, Goal.id == WorkBoardTask.goal_id).where(
        or_(func.instr(WorkflowRunState.artifact_receipts_json, '"' + record_id + '"') > 0,
            func.instr(WorkflowRunState.effect_receipts_json, '"' + record_id + '"') > 0),
        WorkBoardTask.archived_at.is_(None),
        WorkflowRunState.status == "succeeded",
        WorkflowRunState.owner_kind.in_(["user", "service"]),
    ).limit(2))).all()
    proof = None
    for task, attempt, run, goal in rows:
        if (goal.owner_principal_id != task.owner_principal_id or goal.owner_session_id != task.owner_session_id
            or goal.revision != task.goal_revision or goal.status != "active"
            or run.goal_id != task.goal_id or run.goal_revision != task.goal_revision):
            continue
        resolved = next(((source_run, item) for source_run, item in await verified_task_outputs(db, task, attempt, run)
                         if item["artifact_id"] == record_id), None)
        if resolved is None:
            continue
        run, receipt = resolved
        try:
            _read_file(receipt["file_path"], receipt["content_sha256"])
        except OSError:
            continue
        candidate = {"record_id": record_id, "owner_principal_id": task.owner_principal_id,
            "owner_session_id": task.owner_session_id, "task_id": task.task_id,
            "goal_id": task.goal_id, "goal_revision": task.goal_revision,
            "version": run.revision, "digest": receipt["content_sha256"],
            "source_kind": receipt["artifact_type"]}
        if proof is not None and proof != candidate:
            return None
        proof = candidate
    return proof
