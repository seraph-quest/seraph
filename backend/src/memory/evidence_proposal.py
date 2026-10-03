"""Server-staged actually-used proposal evidence; no private I/O in its CAS."""
import json
from sqlalchemy import select

from src.db.models import WorkBoardEvent
from src.memory.evidence_dependencies import digest, stage_packet, recheck_staged
from src.memory.evidence_working_set import _latest, evidence_for_task_context
from src.work_board.repository import BoardError


async def stage_context(db, owner, task, job_id, *, operator=None):
    context = await evidence_for_task_context(db, owner, task.task_id, job_id, operator=operator)
    if context is None:
        return None, None
    packet = await _latest(db, task)
    staged = await stage_packet(db, owner, task, packet, operator=operator, context_only=True)
    value = {'snapshot': staged.snapshot(), 'used_context_digest': digest(context)}
    return staged, value


async def recheck_context(db, owner, task, staged, value, *, prepared_context=None):
    if value is None:
        # A new packet/adoption between staging and the writer is not an
        # evidence-free default. This query does not open its private file.
        event = await db.scalar(select(WorkBoardEvent.event_id).where(
            WorkBoardEvent.task_id == task.task_id,
            WorkBoardEvent.owner_principal_id == owner.principal_id,
            WorkBoardEvent.owner_session_id == owner.session_id,
            WorkBoardEvent.kind == 'task.evidence.updated').limit(1))
        if event is not None or prepared_context is not None:
            raise BoardError('evidence_revision_stale', 'Review the current task evidence before model contact', status_code=409)
        return
    await recheck_staged(db, owner, task, staged)
    if staged.snapshot() != value['snapshot'] or (prepared_context is not None
        and digest(prepared_context) != value['used_context_digest']):
        raise BoardError('evidence_revision_stale', 'Actually used evidence changed before proposal contact or acceptance', status_code=409)


def stored_snapshot(proposal):
    raw = proposal.evidence_use_snapshot_json
    if raw is None:
        return None
    if len(raw.encode()) > 64 * 1024:
        raise BoardError('proposal_evidence_invalid', 'The canonical proposal evidence exceeds its bound', status_code=409)
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise BoardError('proposal_evidence_invalid', 'Canonical proposal evidence is unavailable', status_code=409) from exc


async def stage_proposal_context(db, owner, task, proposal):
    expected = stored_snapshot(proposal)
    if proposal.evidence_use_snapshot_json is None:
        used = list((await db.execute(select(WorkBoardEvent).where(
            WorkBoardEvent.task_id == task.task_id,
            WorkBoardEvent.owner_principal_id == owner.principal_id,
            WorkBoardEvent.owner_session_id == owner.session_id,
            WorkBoardEvent.kind == 'task.evidence.used').limit(4097))).scalars())
        if len(used) > 4096 or any(json.loads(event.metadata_json).get('job_id') == proposal.admission_job_id for event in used):
            raise BoardError('proposal_evidence_regeneration_required', 'Historical source-derived proposal lacks its protected snapshot; regenerate it', status_code=409)
        return None, None
    if expected is None:
        return None, None
    staged, current = await stage_context(db, owner, task, proposal.admission_job_id)
    if current != expected:
        raise BoardError('proposal_evidence_stale', 'Proposal sources changed; inspect and regenerate the typed proposal', status_code=409)
    return staged, current
