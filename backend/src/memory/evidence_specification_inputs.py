"""Exact existing input-artifact handoff for the three reviewed consumers."""
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from src.db.models import WorkBoardInputArtifact
from src.memory.evidence_dependencies import CONSUMERS
from src.memory.evidence_execution import _unlocked
from src.work_board.input_artifacts import (
    ResolvedInputArtifact, _metadata_digest, _utc, bind_input_artifact,
    resolve_input_artifact_for_copy, resolve_input_artifact_for_task,
)
from src.work_board.repository import BoardError


def _stale():
    return BoardError('specification_input_stale', 'The exact prepared input changed before acceptance', status_code=409)


@dataclass(frozen=True)
class SpecificationInput:
    resolved: ResolvedInputArtifact
    target_artifact_id: str
    target_metadata_digest: str
    original_artifact_id: str | None
    original_metadata_digest: str | None
    changed: bool


async def _owned_row(db, owner, artifact_id):
    row = await db.scalar(select(WorkBoardInputArtifact).where(
        WorkBoardInputArtifact.artifact_id == artifact_id,
        WorkBoardInputArtifact.owner_principal_id == owner.principal_id,
        WorkBoardInputArtifact.owner_session_id == owner.session_id,
    ).execution_options(populate_existing=True))
    if row is None or not row.metadata_digest or _metadata_digest(row) != row.metadata_digest:
        raise _stale()
    return row


async def stage_specification_input(db, owner, task, kind, items):
    if kind != 'specify' or len(items) != 1:
        return None
    item = items[0]
    capability = str(item.get('capability_id') or '')
    if capability not in CONSUMERS:
        return None
    await _unlocked(db, task)
    original = await _owned_row(db, owner, task.input_artifact_id) if task.input_artifact_id else None
    if original is not None and (original.bound_task_id != task.task_id
        or original.goal_id != task.goal_id or original.goal_revision != task.goal_revision
        or original.typed_input_ref != task.typed_input_ref or original.payload_sha256 != task.typed_input_digest):
        raise _stale()
    unchanged = (capability, item.get('typed_input_ref'), item.get('typed_input_digest')) == (
        task.capability_id, task.typed_input_ref, task.typed_input_digest) and original is not None
    if not unchanged and task.pipeline_operation_id:
        raise BoardError('pipeline_specification_revision_required', 'Changed fixed pipeline inputs require the existing operation revision review', status_code=409)
    if unchanged:
        resolved = await resolve_input_artifact_for_task(db, owner,
            artifact_id=task.input_artifact_id, goal_id=task.goal_id, goal_revision=task.goal_revision,
            capability_id=capability, expected_task_id=task.task_id)
    else:
        resolved = await resolve_input_artifact_for_copy(db, owner,
            typed_input_ref=str(item.get('typed_input_ref') or ''),
            typed_input_digest=str(item.get('typed_input_digest') or ''),
            capability_id=capability, goal_id=task.goal_id, goal_revision=task.goal_revision)
        if resolved.row.state != 'pending' or resolved.row.bound_task_id is not None:
            raise _stale()
    return SpecificationInput(resolved, resolved.row.artifact_id, _metadata_digest(resolved.row),
        original.artifact_id if original else None, _metadata_digest(original) if original else None, not unchanged)


async def recheck_specification_input(db, owner, task, staged):
    """All physical inspection is already staged; this writer uses pure rows."""
    if staged is None:
        return None
    await _unlocked(db, task)
    if task.input_artifact_id != staged.original_artifact_id:
        raise _stale()
    if staged.original_artifact_id is not None:
        original = await _owned_row(db, owner, staged.original_artifact_id)
        if _metadata_digest(original) != staged.original_metadata_digest:
            raise _stale()
    row = await _owned_row(db, owner, staged.target_artifact_id)
    if (_metadata_digest(row) != staged.target_metadata_digest
        or _utc(row.expires_at) <= datetime.now(timezone.utc)
        or (staged.changed and (row.state != 'pending' or row.bound_task_id is not None))
        or (not staged.changed and (row.state != 'bound' or row.bound_task_id != task.task_id))):
        raise _stale()
    return ResolvedInputArtifact(row, staged.resolved.input, staged.resolved.payload)


async def bind_specification_input(db, owner, task, staged, resolved):
    if staged is not None and staged.changed:
        await bind_input_artifact(db, owner, artifact=resolved,
            task_id=task.task_id, task_revision=task.task_revision)
