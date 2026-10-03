"""Explicit execution binding on the existing private task evidence surface."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, select

from src.db.models import OperatorSession, WorkBoardAttempt, WorkBoardEvent, WorkBoardEvidenceDependency, WorkBoardStatus, WorkBoardTask
from src.memory.evidence_dependencies import (
    _row_binding, dependency_rows, digest, recheck_dependencies, recheck_staged,
    stage_dependencies, stage_packet,
)
from src.memory.evidence_working_set import _latest, _task
from src.work_board.repository import BoardError, WorkBoardRepository, _begin_sqlite_immediate


class ExecutionPreviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_task_revision: int = Field(ge=1)
    expected_packet_revision: int = Field(ge=1)
    expected_packet_digest: str = Field(pattern='^[a-f0-9]{64}$')
    operation: Literal['bind', 'revoke'] = 'bind'


class ExecutionAcceptRequest(ExecutionPreviewRequest):
    preview_digest: str = Field(pattern='^[a-f0-9]{64}$')
    idempotency_key: str = Field(min_length=36, max_length=36)
    acknowledge_execution_use: bool

    @field_validator('acknowledge_execution_use')
    @classmethod
    def literal_acknowledgment(cls, value):
        if value is not True:
            raise ValueError('Explicit execution-use acknowledgment is required')
        return value

    @field_validator('idempotency_key')
    @classmethod
    def canonical_uuid(cls, value):
        if str(UUID(value)) != value:
            raise ValueError('A canonical UUID is required')
        return value


async def _unlocked(db, task):
    if task.status in {WorkBoardStatus.running, WorkBoardStatus.archived}:
        raise BoardError('evidence_task_locked', 'Execution evidence is locked for this task', status_code=409)
    active = await db.scalar(select(WorkBoardAttempt.attempt_id).where(
        WorkBoardAttempt.task_id == task.task_id, WorkBoardAttempt.ended_at.is_(None)).limit(1))
    if active is not None:
        raise BoardError('evidence_task_locked', 'Quiesce the current native attempt before rebinding', status_code=409)
    if task.block_kind in {'unknown_effect', 'cost_liability', 'reconcile_admission_binding'}:
        raise BoardError('evidence_task_locked', 'Resolve the original native liability before rebinding', status_code=409)
    if task.pipeline_operation_id:
        siblings = list((await db.execute(select(WorkBoardTask).where(
            WorkBoardTask.pipeline_operation_id == task.pipeline_operation_id).limit(5))).scalars())
        if len(siblings) > 4 or any(sibling.status == WorkBoardStatus.running or sibling.block_kind in
            {'unknown_effect', 'cost_liability', 'reconcile_admission_binding'} for sibling in siblings):
            raise BoardError('pipeline_quiescence_required', 'Quiesce the exact fixed operation before rebinding', status_code=409)
        active = await db.scalar(select(WorkBoardAttempt.attempt_id).join(WorkBoardTask,
            WorkBoardTask.task_id == WorkBoardAttempt.task_id).where(
                WorkBoardTask.pipeline_operation_id == task.pipeline_operation_id,
                WorkBoardAttempt.ended_at.is_(None)).limit(1))
        if active is not None:
            raise BoardError('pipeline_quiescence_required', 'The fixed operation still owns an active attempt', status_code=409)


async def _current_operator(db, owner, operator):
    if operator is None:  # Internal callers already carry the server owner.
        return
    now = datetime.now(timezone.utc)
    row = await db.get(OperatorSession, owner.session_id, populate_existing=True)
    def utc(value):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if (operator.session_id != owner.session_id or operator.principal.principal_id != owner.principal_id
        or operator.ownership_continuity != 'stable' or row is None
        or row.principal_id != owner.principal_id or row.revoked_at is not None
        or row.replaced_by_id is not None or row.is_bearer_tombstone
        or utc(row.idle_expires_at) <= now or utc(row.absolute_expires_at) <= now
        or row.token_hash != operator._token_hash):
        raise BoardError('evidence_owner_not_current', 'The original authenticated owner is no longer current', status_code=409)


async def _stage(db, owner, task_id, request, operator):
    await _current_operator(db, owner, operator)
    task = await _task(db, owner, task_id)
    if task.task_revision != request.expected_task_revision:
        raise BoardError('stale_revision', 'Reload the task before binding execution evidence', status_code=409)
    await _unlocked(db, task)
    packet = await _latest(db, task)
    if (packet is None or packet['revision'] != request.expected_packet_revision
        or packet['digest'] != request.expected_packet_digest):
        raise BoardError('evidence_revision_stale', 'Review the current evidence packet', status_code=409)
    rows = await dependency_rows(db, task)
    staged = await stage_packet(db, owner, task, packet, operator=operator)
    old = [_row_binding(row) for row in rows]
    if request.operation == 'revoke':
        # Removing a stale binding cannot turn stale work into executable work.
        physical = await stage_dependencies(db, task)
        await recheck_dependencies(db, task, physical)
    value = {'schema': 'work.evidence-rebind-preview.v1', 'operation': request.operation,
        'owner_principal_id': owner.principal_id, 'owner_session_id': owner.session_id,
        'task_id': task.task_id, 'task_revision': task.task_revision,
        'goal_id': task.goal_id, 'goal_revision': task.goal_revision,
        'old_binding_digest': digest(old), 'replacement_snapshot': staged.snapshot(),
        'executor_input_digest': task.typed_input_digest,
        'pipeline_operation_id': task.pipeline_operation_id, 'pipeline_slot': task.pipeline_slot}
    return task, staged, value


def _projection(value):
    return {'task_id': value['task_id'], 'task_revision': value['task_revision'],
        'packet_revision': value['replacement_snapshot']['packet_revision'],
        'packet_digest': value['replacement_snapshot']['packet_digest'],
        'operation': value['operation'], 'preview_digest': digest(value),
        'old_binding_digest': value['old_binding_digest'],
        'executor_input_digest': value['executor_input_digest'],
        'affected_slots': [value['pipeline_slot']] if value['pipeline_slot'] else [],
        'selected_sources': [{'source_id': source['source_id'], 'source_digest': source['source_digest'],
            'span_digest': source['span_digest']} for source in value['replacement_snapshot']['sources']],
        'memory_status': 'no_learning'}


async def preview_execution(db, owner, task_id, request, *, operator=None):
    _task_row, _staged, value = await _stage(db, owner, task_id, request, operator)
    return _projection(value)


async def _prior(db, owner, request, request_digest):
    prior = await db.scalar(select(WorkBoardEvent).where(
        WorkBoardEvent.owner_principal_id == owner.principal_id,
        WorkBoardEvent.owner_session_id == owner.session_id,
        WorkBoardEvent.mutation_idempotency_key == request.idempotency_key))
    if prior is None:
        return None
    if (prior.kind != 'task.evidence.execution_bound' or prior.mutation_request_digest != request_digest):
        raise BoardError('evidence_idempotency_conflict', 'The request key belongs to another exact request', status_code=409)
    return json.loads(prior.metadata_json)['applied_result']


async def accept_execution(db, owner, task_id, request, *, operator=None):
    request_digest = digest({'task_id': task_id, 'mutation': 'execution-evidence',
        'request': request.model_dump()})
    # An exact applied retry needs no current source/file read or new authority.
    await _current_operator(db, owner, operator)
    await WorkBoardRepository().get_task(db, owner, task_id)
    prior = await _prior(db, owner, request, request_digest)
    if prior is not None:
        return prior
    task, staged, value = await _stage(db, owner, task_id, request, operator)
    if digest(value) != request.preview_digest:
        raise BoardError('evidence_preview_stale', 'Inspect a fresh execution-evidence preview', status_code=409)
    # Finish all private reads before the short canonical writer.
    await db.rollback()
    await _begin_sqlite_immediate(db)
    await _current_operator(db, owner, operator)
    prior = await _prior(db, owner, request, request_digest)
    if prior is not None:
        return prior
    task = await _task(db, owner, task_id)
    await _unlocked(db, task)
    await recheck_staged(db, owner, task, staged)
    old = [_row_binding(row) for row in await dependency_rows(db, task)]
    if (digest(old) != value['old_binding_digest'] or task.typed_input_digest != value['executor_input_digest']
        or task.pipeline_operation_id != value['pipeline_operation_id'] or task.pipeline_slot != value['pipeline_slot']):
        raise BoardError('evidence_preview_stale', 'The task binding changed before acceptance', status_code=409)
    if request.operation == 'revoke':
        from src.memory.evidence_dependencies import StagedDependencies
        await recheck_dependencies(db, task, StagedDependencies(task.task_id, task.task_revision, digest(old)))
    await db.execute(delete(WorkBoardEvidenceDependency).where(WorkBoardEvidenceDependency.task_id == task_id))
    revision = task.task_revision + 1
    if request.operation == 'bind':
        for source in staged.sources:
            db.add(WorkBoardEvidenceDependency(task_id=task_id, owner_principal_id=owner.principal_id,
                owner_session_id=owner.session_id, goal_id=task.goal_id, source_kind=source.source_kind,
                canonical_source_id=source.canonical_source_id, source_id=source.source_id,
                source_digest=source.source_digest, span_digest=source.span_digest,
                resolved_token_json=source.token_json, packet_revision=staged.packet_revision,
                packet_digest=staged.packet_digest, binding_task_revision=revision,
                executor_input_digest=task.typed_input_digest, pipeline_operation_id=task.pipeline_operation_id,
                pipeline_slot=task.pipeline_slot))
    await WorkBoardRepository()._cas_task_update(db, owner, task,
        expected_revision=request.expected_task_revision, values={'task_revision': revision})
    result = {**_projection(value), 'task_revision': revision, 'binding_state':
        'bound' if request.operation == 'bind' else 'unbound'}
    event = WorkBoardEvent(task_id=task_id, owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id, actor_principal_id=owner.principal_id,
        actor_session_id=owner.session_id, kind='task.evidence.execution_bound',
        mutation_idempotency_key=request.idempotency_key, mutation_request_digest=request_digest,
        metadata_json=json.dumps({'applied_result': result, 'previous_binding_digest': digest(old),
            'binding_snapshot_digest': digest(staged.snapshot()), 'executor_input_digest': task.typed_input_digest}))
    db.add(event)
    await db.flush()
    return result


async def inspect_execution(db, owner, task_id, *, pending=None, operator=None):
    """Pure current projection and exact applied-event inspection after reload."""
    task = await WorkBoardRepository().get_task(db, owner, task_id)
    rows = await dependency_rows(db, task)
    state = 'unbound' if not rows else 'bound'
    reason = None
    if rows:
        try:
            physical = await stage_dependencies(db, task)
            await recheck_dependencies(db, task, physical)
        except (BoardError, OSError, KeyError, TypeError):
            state, reason = 'stale', 'evidence_dependency_stale'
    applied = None
    if pending is not None:
        request_digest = digest({'task_id': task_id, 'mutation': 'execution-evidence',
            'request': pending.model_dump()})
        applied = await _prior(db, owner, pending, request_digest)
    return {'task_id': task.task_id, 'task_revision': task.task_revision,
        'binding_state': state, 'binding_count': len(rows), 'reason_code': reason,
        'applied_result': applied, 'executor_input_digest': task.typed_input_digest,
        'sources': [{'source_id': row.source_id, 'source_digest': row.source_digest,
                     'span_digest': row.span_digest} for row in rows],
        'memory_status': 'no_learning'}
