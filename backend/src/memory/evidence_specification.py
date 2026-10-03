"""Execution evidence on one reviewed, canonical Specify acceptance.

Private bytes are staged before the writer. The verified proposal supplies
the prospective capability/input; requests supply only packet references and
a separate literal acknowledgment. No source tokens come from the caller.
"""
from dataclasses import dataclass

from sqlalchemy import delete

from src.db.models import WorkBoardEvidenceDependency
from src.memory.evidence_dependencies import (
    CONSUMERS, StagedDependencies, StagedEvidence, _recheck_staged_sources,
    _row_binding, dependency_rows, digest, recheck_dependencies,
    stage_dependencies, stage_packet,
)
from src.memory.evidence_execution import _unlocked
from src.memory.evidence_working_set import _latest
from src.work_board.repository import BoardError, WorkBoardRepository


def _identity(task):
    return {key: getattr(task, key) for key in (
        'task_id', 'task_revision', 'owner_principal_id', 'owner_session_id',
        'goal_id', 'goal_revision', 'capability_id', 'typed_input_ref',
        'typed_input_digest', 'executor_id', 'pipeline_operation_id', 'pipeline_slot')}


def _target(task, item):
    # SQLModel model_copy carries ORM instrumentation. A new row-shaped value
    # stays detached and must never alter the canonical task while staging.
    return type(task)(**{**task.model_dump(),
        'capability_id': str(item.get('capability_id') or ''),
        'typed_input_ref': str(item.get('typed_input_ref') or ''),
        'typed_input_digest': str(item.get('typed_input_digest') or '')})


@dataclass(frozen=True)
class SpecificationEvidence:
    task_identity_digest: str
    target_identity_digest: str
    old_rows_digest: str
    old_count: int
    existing: StagedDependencies | None
    replacement: StagedEvidence | None


async def stage_specification(db, owner, task, proposal_kind, items, request):
    rows = await dependency_rows(db, task)
    if request.execution_replacement is not None and proposal_kind != 'specify':
        raise BoardError('evidence_replacement_unsupported', 'Execution evidence replacement requires one reviewed Specify', status_code=409)
    if proposal_kind != 'specify':
        # A bound parent remains bound even when a proposal only creates children.
        return SpecificationEvidence(digest(_identity(task)), digest(_identity(task)),
            digest([_row_binding(row) for row in rows]), len(rows),
            await stage_dependencies(db, task), None)
    if len(items) != 1 or not isinstance(items[0], dict):
        raise BoardError('invalid_proposal', 'Specify requires one exact typed target', status_code=409)
    target = _target(task, items[0])
    if rows and target.capability_id not in CONSUMERS:
        raise BoardError('evidence_dependency_unsupported', 'A bound Specify must retain a supported consumer', status_code=409)
    replacement = None
    if rows or request.execution_replacement is not None:
        await _unlocked(db, task)
    if request.execution_replacement is not None:
        packet = await _latest(db, task)
        expected = request.execution_replacement
        if (packet is None or packet['revision'] != expected.expected_packet_revision
            or packet['digest'] != expected.expected_packet_digest):
            raise BoardError('evidence_revision_stale', 'Inspect the exact replacement packet before accepting Specify', status_code=409)
        replacement = await stage_packet(db, owner, target, packet)
    elif rows and (task.capability_id, task.typed_input_ref, task.typed_input_digest) != (
        target.capability_id, target.typed_input_ref, target.typed_input_digest):
        raise BoardError('evidence_replacement_required', 'Changed typed inputs require a separate execution-evidence acknowledgment', status_code=409)
    return SpecificationEvidence(digest(_identity(task)), digest(_identity(target)),
        digest([_row_binding(row) for row in rows]), len(rows),
        None if replacement else await stage_dependencies(db, task), replacement)


async def recheck_specification(db, owner, task, proposal_kind, items, staged):
    target = _target(task, items[0]) if proposal_kind == 'specify' else task
    rows = await dependency_rows(db, task)
    if (digest(_identity(task)) != staged.task_identity_digest
        or digest(_identity(target)) != staged.target_identity_digest
        or digest([_row_binding(row) for row in rows]) != staged.old_rows_digest):
        raise BoardError('evidence_dependency_stale', 'Execution binding changed before Specify acceptance', status_code=409)
    if staged.old_count or staged.replacement is not None:
        await _unlocked(db, task)
    if staged.replacement is None:
        await recheck_dependencies(db, task, staged.existing)
    else:
        await _recheck_staged_sources(db, owner, target, staged.replacement)


async def replace_specification_evidence(db, owner, task, proposal, staged):
    if staged.replacement is None:
        return
    replacement = staged.replacement
    await db.execute(delete(WorkBoardEvidenceDependency).where(
        WorkBoardEvidenceDependency.task_id == task.task_id))
    for source in replacement.sources:
        db.add(WorkBoardEvidenceDependency(task_id=task.task_id,
            owner_principal_id=owner.principal_id, owner_session_id=owner.session_id,
            goal_id=task.goal_id, source_kind=source.source_kind,
            canonical_source_id=source.canonical_source_id, source_id=source.source_id,
            source_digest=source.source_digest, span_digest=source.span_digest,
            resolved_token_json=source.token_json, packet_revision=replacement.packet_revision,
            packet_digest=replacement.packet_digest, binding_task_revision=task.task_revision,
            executor_input_digest=task.typed_input_digest,
            pipeline_operation_id=task.pipeline_operation_id, pipeline_slot=task.pipeline_slot))
    await WorkBoardRepository()._event(db, task, owner,
        kind='task.evidence.specification_replaced', metadata={
            'proposal_id': proposal.proposal_id, 'proposal_digest': proposal.proposal_digest,
            'previous_binding_digest': staged.old_rows_digest,
            'binding_snapshot_digest': digest(replacement.snapshot()),
            'packet_revision': replacement.packet_revision, 'packet_digest': replacement.packet_digest,
            'task_revision': task.task_revision, 'executor_input_digest': task.typed_input_digest,
            'memory_status': 'no_learning'})
