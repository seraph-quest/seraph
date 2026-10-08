"""Sealed native-task continuation; the existing tool wrapper consumes approval.

The canonical job owner rechecks this witness inside its serialized transition.
Neither wire data nor a generic paused-job resume can manufacture permission.
"""
from dataclasses import dataclass

_SEAL = object()


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
