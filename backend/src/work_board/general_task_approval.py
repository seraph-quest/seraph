"""Sealed native-task continuation; the existing tool wrapper consumes approval.

The canonical job owner rechecks this witness inside its serialized transition.
Neither wire data nor a generic paused-job resume can manufacture permission.
"""
from dataclasses import dataclass

_SEAL = object()


async def publish_approval_wait(jobs, *, service, proof, checkpoint_id, binding,
        owner_principal_id, operator_session_id, runtime_owner, tool_name,
        approval_context, original_input_digest, verify_current):
    """Publish one proven precontact wait through the existing joint writer."""
    import json
    from datetime import datetime, timezone
    from sqlalchemy import update
    from src.db.models import WorkflowRunState, WorkBoardStatus
    from src.approval.repository import approval_repository
    from src.native_tools.task_adapters import TaskToolApprovalRequired
    from src.work_board.contracts import WorkBoardOwner
    from src.work_board.general_task import canonical, digest
    from src.work_board.repository import BoardError, _begin_sqlite_immediate
    from src.workflows.job_runtime import _safe_structure, _job_effect_ledger
    if (type(proof) is not TaskToolApprovalRequired
        or proof.approval_id != binding.get("approval_id")
        or proof.binding.job_id != binding.get("job_id")
        or proof.binding.fencing_token != binding.get("fence")
        or proof.binding.input_digest != binding.get("input_digest")
        or proof.binding.descriptor_digest != binding.get("descriptor_digest")):
        raise BoardError("general_task_unresolved_step", "Exact wrapper no-contact proof is required", status_code=409)
    canonical(binding)
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        run = await jobs._fetch(db, binding["job_id"])
        jobs._assert_lease(run, owner=runtime_owner, fencing_token=binding["fence"])
        task, attempt = await verify_current(db, run)
        now = datetime.now(timezone.utc)
        if (task.status is not WorkBoardStatus.running
            or not attempt.lease_owner
            or f"{attempt.lease_owner}:{attempt.attempt_id}" != runtime_owner
            or attempt.fencing_token != binding["fence"]
            or attempt.lease_expires_at is None
            or attempt.lease_expires_at.replace(tzinfo=timezone.utc) <= now):
            raise BoardError("general_task_unresolved_step", "Original board lease changed", status_code=409)
        request = await approval_repository.attach_general_task_wait_binding_in_session(db, proof.approval_id,
            owner_principal_id=owner_principal_id, operator_session_id=operator_session_id,
            runtime_owner=runtime_owner, binding=binding, tool_name=tool_name,
            approval_context=approval_context, original_input_digest=original_input_digest,
            verify_current=verify_current)
        if request is None:
            raise BoardError("general_task_unresolved_step", "Exact unconsumed approval is unavailable", status_code=409)
        checkpoints = json.loads(run.checkpoint_receipts_json or "[]")
        selected = [item for item in checkpoints if item.get("checkpoint_id") == checkpoint_id]
        if (len(selected) != 1 or selected[0].get("fencing_token") != binding["fence"]
            or selected[0].get("state_digest") != digest({"phase": "intent",
                "step_id": binding["step_id"], "descriptor_digest": binding["descriptor_digest"],
                "input_digest": binding["input_digest"]})):
            raise BoardError("general_task_unresolved_step", "Original intent checkpoint changed", status_code=409)
        safe = _safe_structure(binding)
        checkpoints = [item for item in checkpoints if item.get("checkpoint_id") != checkpoint_id]
        checkpoints.append({"checkpoint_id": checkpoint_id, "state_digest": digest(binding),
            "state_keys": sorted(binding), "safe": True, "payload": safe,
            "fencing_token": binding["fence"], "recorded_at": now.isoformat()})
        effects = json.loads(run.effect_receipts_json or "[]")
        selected_effect = [item for item in effects if item.get("effect_id") == binding["effect_id"]]
        expected_target = "general-step:" + digest([run.run_identity, binding["step_id"]])
        if (len(selected_effect) != 1 or selected_effect[0].get("status") != "intent"
            or selected_effect[0].get("fencing_token") != binding["fence"]
            or selected_effect[0].get("effect_type") != "general_tool_call"
            or selected_effect[0].get("target_path") != expected_target
            or selected_effect[0].get("details", {}).get("input_digest") != binding["input_digest"]):
            raise BoardError("general_task_unresolved_step", "Unknown effect cannot be settled as absent", status_code=409)
        receipt = {**selected_effect[0], "receipt_kind": "readback", "status": "succeeded",
            "content_sha256": digest(binding), "readback_id": "general-precontact:" + digest(binding)[:32],
            "verified_at": now.isoformat(), "recorded_at": now.isoformat(),
            "reconciled": True, "reconciliation_status": "resolved",
            "details": {"verified": True, "never_contacted": True, "approval_precontact": True,
                "step_id": binding["step_id"], "no_learning": True}}
        effects = [item for item in effects if item.get("effect_id") != binding["effect_id"]] + [receipt]
        changed = await db.execute(update(WorkflowRunState).where(
            WorkflowRunState.run_identity == run.run_identity,
            WorkflowRunState.status == "running", WorkflowRunState.revision == run.revision,
            WorkflowRunState.lease_owner == runtime_owner,
            WorkflowRunState.fencing_token == binding["fence"],
            WorkflowRunState.lease_expires_at > now,
        ).values(checkpoint_receipts_json=canonical(checkpoints).decode(),
            effect_receipts_json=canonical(_job_effect_ledger(run, effects)).decode(),
            status="paused", failure_reason="general_task_approval_required",
            lease_owner=None, lease_expires_at=None, updated_at=now,
            revision=WorkflowRunState.revision + 1).execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            raise BoardError("general_task_unresolved_step", "Original job changed before pause", status_code=409)
        await service.repository._cas_task_update(db,
            WorkBoardOwner(principal_id=owner_principal_id, session_id=operator_session_id), task,
            expected_revision=task.task_revision, values={"status": WorkBoardStatus.blocked,
                "block_kind": "needs_input", "block_reason": "awaiting_approval",
                "block_source_status": WorkBoardStatus.running.value,
                "task_revision": task.task_revision + 1, "updated_at": now})
        attempt.lease_owner = attempt.lease_expires_at = None
        attempt.outcome = "awaiting_approval"
        attempt.updated_at = now
        db.add(attempt)
        await service.repository._event(db, task,
            WorkBoardOwner(principal_id=owner_principal_id, session_id=operator_session_id),
            kind="task.approval_wait", metadata={"approval_id": proof.approval_id,
                "workflow_run_id": run.run_identity, "step_id": binding["step_id"]})
        await db.flush()


@dataclass(frozen=True)
class _ResumeWitness:
    service: object
    owner: object
    request: object
    task_id: str
    seal: object
    runner_id: str = ""


async def prepare_resume_witness(service, db, owner, task_id, request, projection, *, runner_id):
    await service.validate_resume(db, owner, task_id, request, projection)
    return _ResumeWitness(service, owner, request, task_id, _SEAL, runner_id)


async def recheck_resume_witness(db, run, witness):
    from src.work_board.repository import BoardError
    from src.workflows.job_runtime import _serialize
    if type(witness) is not _ResumeWitness or witness.seal is not _SEAL:
        raise BoardError("general_task_resume_witness_invalid", "Exact native continuation proof is required", status_code=409)
    if not witness.service.started:
        raise BoardError("general_task_inactive", "Restore the current task service", status_code=503)
    task, attempt, _envelope = await witness.service.validate_resume(db, witness.owner, witness.task_id,
        witness.request, _serialize(run))
    from datetime import datetime, timezone
    deadline = run.deadline_at.replace(tzinfo=timezone.utc)
    seconds = max(1, int((deadline - datetime.now(timezone.utc)).total_seconds()))
    if not witness.runner_id:
        raise BoardError("general_task_resume_witness_invalid", "Current native runner is required", status_code=409)
    # Same canonical writer owns both board reacquisition and root queue CAS.
    # A failed queue CAS rolls back this attempt mutation too.
    await witness.service.repository.resume_routine_attempt_for_operator_recovery(db,
        task.task_id, attempt.attempt_id, expected_revision=witness.request.expected_revision,
        previous_fence=witness.request.fencing_token, next_fence=witness.request.fencing_token + 1,
        lease_owner=witness.runner_id, lease_seconds=seconds,
        workflow_run_id=witness.request.workflow_run_id,
        actor_principal_id=witness.owner.principal_id, actor_session_id=witness.owner.session_id,
        capability_id="agent.task.v1", _writer_held=True)
