"""Finite server-resolved execution preconditions on existing task evidence.

Physical/private inspection is staged outside the writer. Canonical token
rechecks use only loaded rows and bounded JSON, never source text as authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any

from sqlalchemy import select

from config.settings import settings
from src.db.models import (
    Goal, Memory, MemoryKind, MemoryProposal, MemorySource, MemoryStatus, MemoryTombstone,
    WorkBoardAttempt, WorkBoardEvidenceDependency, WorkBoardStatus, WorkBoardTask,
    WorkflowRunState,
)
from src.work_board.contracts import WorkBoardOwner
from src.work_board.repository import BoardError

CONSUMERS = frozenset({'browser.public-task.v1', 'work.evidence-dossier.v1', 'work.local-evidence-report.v1'})
PRODUCERS = {
    'browser_public_task_result': 'browser.public-task.v1',
    'evidence_dossier': 'work.evidence-dossier.v1',
    'evidence_local_report': 'work.local-evidence-report.v1',
}
MAX_DEPENDENCIES = 16
MAX_SNAPSHOT_BYTES = 64 * 1024
MAX_TOKEN_BYTES = 8192
_DIGEST = re.compile(r'^[a-f0-9]{64}$')


def digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(raw).hexdigest()


def _json(value: str | None, *, maximum: int = MAX_SNAPSHOT_BYTES) -> Any:
    if not isinstance(value, str) or len(value.encode()) > maximum:
        raise BoardError('evidence_dependency_invalid', 'Canonical evidence metadata is unavailable', status_code=409)
    try:
        return json.loads(value)
    except (TypeError, ValueError) as exc:
        raise BoardError('evidence_dependency_invalid', 'Canonical evidence metadata is malformed', status_code=409) from exc


def _time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)).isoformat()


def _changed() -> BoardError:
    return BoardError('evidence_dependency_stale', 'Selected execution evidence changed; inspect and review its replacement', status_code=409)


@dataclass(frozen=True)
class ResolvedSource:
    """Internal staged identity, minted only after the private source reader."""

    source_id: str
    source_kind: str
    canonical_source_id: str
    source_digest: str
    span_digest: str
    line_start: int
    line_end: int
    version: str
    token_json: str

    def token(self) -> dict[str, Any]:
        value = _json(self.token_json, maximum=MAX_TOKEN_BYTES)
        if not isinstance(value, dict):
            raise _changed()
        return value


@dataclass(frozen=True)
class StagedEvidence:
    task_id: str
    task_revision: int
    packet_revision: int
    packet_digest: str
    sources: tuple[ResolvedSource, ...]

    def snapshot(self) -> dict[str, Any]:
        value = {'schema': 'work.evidence-execution.v1', 'task_id': self.task_id,
                 'task_revision': self.task_revision, 'packet_revision': self.packet_revision,
                 'packet_digest': self.packet_digest,
                 'sources': [{'source_id': item.source_id, 'source_kind': item.source_kind,
                     'canonical_source_id': item.canonical_source_id, 'source_digest': item.source_digest,
                     'span_digest': item.span_digest, 'line_start': item.line_start,
                     'line_end': item.line_end, 'version': item.version, 'token': item.token()}
                    for item in self.sources]}
        if len(json.dumps(value, sort_keys=True).encode()) > MAX_SNAPSHOT_BYTES:
            raise BoardError('evidence_dependency_limit', 'Execution evidence exceeds its finite snapshot limit', status_code=409)
        return value


async def _goal(db, owner: WorkBoardOwner, task: WorkBoardTask) -> Goal:
    goal = await db.get(Goal, task.goal_id, populate_existing=True)
    if goal is None or (goal.owner_principal_id, goal.owner_session_id) != (owner.principal_id, owner.session_id):
        raise _changed()
    return goal


async def canonical_source_token(db, owner: WorkBoardOwner, task: WorkBoardTask,
                                 source_kind: str, canonical_id: str,
                                 lineage: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pure canonical DB checks, safe inside an existing immediate writer."""
    await _goal(db, owner, task)
    identity = {'owner_principal_id': owner.principal_id, 'owner_session_id': owner.session_id,
                'goal_id': task.goal_id, 'source_kind': source_kind, 'canonical_source_id': canonical_id}
    if source_kind == 'canonical_memory':
        memory = await db.get(Memory, canonical_id, populate_existing=True)
        tombstone = (await db.execute(select(MemoryTombstone.id).where(
            MemoryTombstone.memory_id == canonical_id).limit(1))).scalar_one_or_none()
        if (memory is None or tombstone is not None or memory.status != MemoryStatus.active
            or memory.kind != MemoryKind.fact or memory.source_session_id != owner.session_id
            or len(memory.content.encode()) > MAX_SNAPSHOT_BYTES):
            raise _changed()
        metadata = _json(memory.metadata_json or '{}')
        if not isinstance(metadata, dict):
            raise _changed()
        from src.memory.repository import _canonical_memory_deletion_marker
        if _canonical_memory_deletion_marker(memory) is not None:
            raise _changed()
        provenance = metadata.get('work_board_provenance', metadata.get('provenance', metadata))
        if not isinstance(provenance, dict):
            raise _changed()
        accepted = list((await db.execute(select(MemoryProposal).where(
            MemoryProposal.accepted_memory_id == canonical_id,
            MemoryProposal.owner_principal_id == owner.principal_id,
            MemoryProposal.owner_session_id == owner.session_id,
            MemoryProposal.goal_id == task.goal_id, MemoryProposal.status == 'accepted').limit(2))).scalars())
        if provenance.get('goal_id') != task.goal_id and metadata.get('goal_id') != task.goal_id and not accepted:
            raise _changed()
        sources = list((await db.execute(select(MemorySource).where(
            MemorySource.memory_id == canonical_id).order_by(MemorySource.id).limit(33))).scalars())
        if (not sources or len(sources) > 32 or any(item.source_session_id != owner.session_id for item in sources)
            or not any(item.source_type in {'operator', 'work_board_m5'} for item in sources)):
            raise _changed()
        return {**identity, 'state': 'active', 'content_digest': digest(memory.content.encode()),
                'metadata_digest': digest(memory.metadata_json or '{}'), 'updated_at': _time(memory.updated_at),
                'provenance_digest': digest([{'id': item.id, 'type': item.source_type,
                    'session': item.source_session_id, 'message': item.source_message_id,
                    'snippet_digest': digest((item.snippet or '').encode())} for item in sources]),
                'accepted_proposal_digest': digest([{'id': item.proposal_id, 'status': item.status,
                    'memory_id': item.accepted_memory_id} for item in accepted])}
    producer = PRODUCERS.get(source_kind)
    if producer is None or not isinstance(lineage, dict) or set(lineage) != {'task_id', 'attempt_id', 'run_id'}:
        raise BoardError('evidence_dependency_unsupported', 'This exact source type cannot bind execution', status_code=409)
    source_task = (await db.execute(select(WorkBoardTask).where(
        WorkBoardTask.task_id == lineage['task_id']).execution_options(populate_existing=True))).scalar_one_or_none()
    attempt = await db.get(WorkBoardAttempt, lineage['attempt_id'], populate_existing=True)
    run = (await db.execute(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == lineage['run_id']).execution_options(populate_existing=True))).scalar_one_or_none()
    if (source_task is None or attempt is None or run is None
        or source_task.capability_id != producer or source_task.archived_at is not None
        or (source_task.owner_principal_id, source_task.owner_session_id, source_task.goal_id)
            != (owner.principal_id, owner.session_id, task.goal_id)
        or attempt.task_id != source_task.task_id or attempt.workflow_run_id != run.run_identity
        or attempt.ended_at is None or attempt.outcome != 'verified' or run.status != 'succeeded'):
        raise _changed()
    from src.memory.evidence_sources import run_has_task_owner
    from src.memory.evidence_working_set import _verified_receipts
    if not run_has_task_owner(source_task, attempt, run):
        raise _changed()
    for value in (run.artifact_receipts_json, run.effect_receipts_json, run.declared_authority_json):
        _json(value, maximum=256 * 1024)
    matches = [item for item in _verified_receipts(run) if item.get('artifact_id') == canonical_id
               and item.get('artifact_type') == source_kind]
    if len(matches) != 1:
        raise _changed()
    receipt = matches[0]
    return {**identity, 'lineage': dict(lineage), 'producer': producer,
            'task_revision': source_task.task_revision, 'task_status': source_task.status.value,
            'input_digest': source_task.typed_input_digest, 'attempt_fence': attempt.fencing_token,
            'attempt_outcome': attempt.outcome, 'run_revision': run.revision,
            'run_authority_digest': run.authority_digest, 'run_input_digest': run.input_digest,
            'readback_digest': digest(run.effect_receipts_json), 'receipt_digest': digest(receipt),
            'file_path': receipt['file_path'], 'content_digest': receipt['content_sha256'],
            'read_policy_digest': digest({'allow': settings.browser_site_allowlist,
                'block': settings.browser_site_blocklist}) if producer == 'browser.public-task.v1' else None}


async def stage_packet(db, owner: WorkBoardOwner, task: WorkBoardTask,
                       packet: dict[str, Any], *, operator=None) -> StagedEvidence:
    """Stage existing eligibility, redaction and bounded actual file bytes."""
    from src.memory.evidence_working_set import _digest, _sources
    if (task.capability_id not in CONSUMERS or len(packet.get('citations', [])) > MAX_DEPENDENCIES
        or not packet.get('citations')):
        raise BoardError('evidence_dependency_unsupported', 'Select one to sixteen eligible execution sources for a supported consumer', status_code=409)
    sources, _blocked = await _sources(db, owner, task, operator=operator)
    indexed = {item['source_id']: item for item in sources}
    resolved = []
    seen = set()
    for citation in packet['citations']:
        source = indexed.get(citation['source_id'])
        if (source is None or source.get('ownership_access', 'current') != 'current'
            or source['source_kind'] not in {'canonical_memory', *PRODUCERS}
            or (source['source_digest'], source['version']) != (citation['source_digest'], citation['version'])):
            raise _changed()
        start, end = citation['line_start'], citation['line_end']
        lines = source['text'].splitlines()
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
            raise _changed()
        if _digest('\n'.join(lines[start-1:end]).encode()) != citation['span_digest']:
            raise _changed()
        token = await canonical_source_token(db, owner, task, source['source_kind'], source['identifier'],
                                             source.get('canonical_binding'))
        serialized = json.dumps(token, sort_keys=True, separators=(',', ':'))
        if len(serialized.encode()) > MAX_TOKEN_BYTES:
            raise BoardError('evidence_dependency_limit', 'Canonical token exceeds its finite limit', status_code=409)
        key = (source['source_kind'], source['identifier'], citation['span_digest'])
        if key in seen:
            raise BoardError('evidence_dependency_invalid', 'Duplicate source spans cannot bind execution', status_code=409)
        seen.add(key)
        resolved.append(ResolvedSource(citation['source_id'], source['source_kind'], source['identifier'],
            citation['source_digest'], citation['span_digest'], start, end, citation['version'], serialized))
    staged = StagedEvidence(task.task_id, task.task_revision, packet['revision'], packet['digest'], tuple(resolved))
    staged.snapshot()
    return staged


async def recheck_staged(db, owner: WorkBoardOwner, task: WorkBoardTask, staged: StagedEvidence) -> None:
    """Canonical-only source comparison after the caller acquires its writer."""
    if task.task_id != staged.task_id:
        raise _changed()
    for source in staged.sources:
        expected = source.token()
        current = await canonical_source_token(db, owner, task, source.source_kind,
                                               source.canonical_source_id, expected.get('lineage'))
        if current != expected:
            raise _changed()


async def dependency_rows(db, task: WorkBoardTask) -> list[WorkBoardEvidenceDependency]:
    rows = list((await db.execute(select(WorkBoardEvidenceDependency).where(
        WorkBoardEvidenceDependency.task_id == task.task_id).order_by(
            WorkBoardEvidenceDependency.dependency_id).limit(MAX_DEPENDENCIES+1))).scalars())
    if len(rows) > MAX_DEPENDENCIES or (rows and task.capability_id not in CONSUMERS):
        raise BoardError('evidence_dependency_unsupported', 'Persisted execution dependencies are unsupported or over limit', status_code=409)
    for row in rows:
        if ((row.owner_principal_id, row.owner_session_id, row.goal_id)
                != (task.owner_principal_id, task.owner_session_id, task.goal_id)
            or row.source_kind not in {'canonical_memory', *PRODUCERS}
            or row.executor_input_digest != task.typed_input_digest
            or row.pipeline_operation_id != task.pipeline_operation_id or row.pipeline_slot != task.pipeline_slot
            or not 1 <= len(row.canonical_source_id) <= 256
            or type(row.packet_revision) is not int or row.packet_revision < 1
            or type(row.binding_task_revision) is not int or row.binding_task_revision < 1
            or any(not isinstance(value, str) or not _DIGEST.fullmatch(value)
                   for value in (row.source_id, row.source_digest, row.span_digest, row.packet_digest))):
            raise _changed()
        token = _json(row.resolved_token_json, maximum=MAX_TOKEN_BYTES)
        if not isinstance(token, dict):
            raise _changed()
    if len(json.dumps([row.resolved_token_json for row in rows]).encode()) > MAX_SNAPSHOT_BYTES:
        raise BoardError('evidence_dependency_limit', 'Persisted evidence exceeds its finite snapshot limit', status_code=409)
    return rows
