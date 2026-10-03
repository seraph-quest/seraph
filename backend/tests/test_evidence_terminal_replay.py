"""Real file/SQLite durable consumer lifecycle and historical replay.

Canonical source/Board linkage is seeded by the existing token fixtures. This
is focused repository proof, not authentication or producer-journey evidence.
The consumer is admitted, claimed, writes actual fixed CPU output and records
its actual byte digest through the canonical job methods before completion.
"""
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from config.settings import settings
from src.db.models import Memory, WorkBoardAttempt, WorkBoardEvidenceDependency, WorkBoardEvent
from src.memory.evidence_dependencies import canonical_source_token, digest
from src.work_board.repository import BoardError
from src.work_board.pipeline_contracts import DOSSIER
from src.work_board.pipeline_cpu import output_bytes
from src.workflows.job_runtime import DurableJobIdentity, DurableJobRepository, DurableJobSpec
from tests.test_artifact_pipelines import browser_payload, consumer
from tests.test_evidence_dependency_artifact_tokens import canonical_chain
from tests.test_evidence_dependency_tokens import canonical_fact


async def bound_consumer(async_db, tmp_path, source_kind):
    async with async_db() as db:
        owner, task, memory, _ = await canonical_fact(db)
        task.capability_id = DOSSIER
        await db.commit()
        lineage = None
        canonical_id = memory.id
        if source_kind == 'evidence_dossier':
            canonical_id, lineage = (await canonical_chain(db, owner, task, tmp_path))[source_kind]
        token = await canonical_source_token(db, owner, task, source_kind, canonical_id, lineage)
        db.add(WorkBoardEvidenceDependency(task_id=task.task_id,
            owner_principal_id=owner.principal_id, owner_session_id=owner.session_id,
            goal_id=task.goal_id, source_kind=source_kind, canonical_source_id=canonical_id,
            source_id='c'*64, source_digest='d'*64, span_digest='e'*64,
            resolved_token_json=json.dumps(token), packet_revision=1, packet_digest='b'*64,
            binding_task_revision=task.task_revision, executor_input_digest=task.typed_input_digest))
        db.add(WorkBoardAttempt(attempt_id='consumer-attempt', task_id=task.task_id,
            workflow_run_id='bound-consumer-native', fencing_token=1))
        await db.commit()
        spec = DurableJobSpec(identity=DurableJobIdentity(job_id='bound-consumer-native',
            owner_kind='user', owner_principal_id=owner.principal_id, job_kind=DOSSIER,
            capability_version='1', idempotency_scope='work-board-attempt',
            idempotency_key=task.task_id+':consumer-attempt'),
            session_id=owner.session_id, operator_session_id=owner.session_id,
            goal_id=task.goal_id, goal_revision=task.goal_revision,
            declared_authority={'principal':owner.principal_id, 'owner_kind':'user'})
    jobs = DurableJobRepository()
    admitted = await jobs.admit_job(spec)
    await jobs.queue_job(admitted['job_id'])
    claimed = await jobs.claim_job(admitted['job_id'], owner='terminal-consumer')
    raw = output_bytes(DOSSIER, consumer(browser_payload()))
    path = tmp_path/'artifacts'/'actual-consumer-output.json'
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(raw);path.chmod(0o600)
    assert path.read_bytes() == raw
    fence = claimed['lease']['fencing_token']
    artifact = await jobs.record_artifact(admitted['job_id'],
        file_path=str(path.relative_to(tmp_path)), artifact_type='evidence_dossier', content=raw,
        owner='terminal-consumer', fencing_token=fence, expected_revision=claimed['revision'])
    verified = await jobs.record_readback(admitted['job_id'],
        effect_id='actual-consumer-output', effect_type='evidence_dossier',
        target_path=str(path.relative_to(tmp_path)), target_digest=digest(raw),
        content_sha256=digest(path.read_bytes()), status='succeeded',
        details={'verified':True, 'no_learning':True},
        owner='terminal-consumer', fencing_token=fence, expected_revision=artifact['revision'])
    return jobs, owner, task, memory, token, verified


@pytest.mark.asyncio
@pytest.mark.parametrize('async_db', ['file'], indirect=True)
@pytest.mark.parametrize('source_kind', ['canonical_memory', 'evidence_dossier'])
async def test_completed_bound_consumer_replays_after_correction_or_artifact_deletion(
    async_db, tmp_path, monkeypatch, source_kind,
):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(settings, 'workspace_dir', str(tmp_path))
    monkeypatch.setattr(settings, 'browser_site_allowlist', 'example.com')
    monkeypatch.setattr(settings, 'browser_site_blocklist', '')
    jobs, owner, task, memory, token, verified = await bound_consumer(async_db, tmp_path, source_kind)
    completed = await jobs.transition_job(verified['job_id'], 'succeeded',
        owner='terminal-consumer', fencing_token=verified['lease']['fencing_token'],
        expected_revision=verified['revision'], result={'no_learning':True})
    async with async_db() as db:
        if source_kind == 'canonical_memory':
            (await db.get(Memory, memory.id)).content = 'Operator correction after completion'
            await db.commit()
        else:
            (tmp_path/token['file_path']).unlink()
    # Historical replay must not inspect source bytes or mint a new use event.
    def forbidden(*args, **kwargs):
        raise AssertionError('Historical terminal replay inspected source files')
    monkeypatch.setattr('src.memory.evidence_working_set._read_file', forbidden)
    replay = await jobs.transition_job(verified['job_id'], 'succeeded',
        owner='stale-owner', fencing_token=999, expected_revision=1)
    claim = await jobs.claim_job(verified['job_id'], owner='stale-owner', expected_revision=1)
    assert replay['receipt']['terminal_noop'] is True
    assert claim['receipt']['status'] == 'terminal_noop'
    for current in (replay, claim, await jobs.get_job(verified['job_id'])):
        assert current['status'] == 'succeeded'
        assert current['revision'] == completed['revision']
        assert current['attempt_count'] == completed['attempt_count']
        assert current['artifacts'] == completed['artifacts']
        assert current['effects'] == completed['effects']
    async with async_db() as db:
        assert not list((await db.execute(select(WorkBoardEvent).where(
            WorkBoardEvent.kind == 'task.evidence.execution_stale'))).scalars())


@pytest.mark.asyncio
@pytest.mark.parametrize('async_db', ['file'], indirect=True)
async def test_fresh_terminal_transition_still_rejects_corrected_bound_source(
    async_db, tmp_path, monkeypatch,
):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(settings, 'workspace_dir', str(tmp_path))
    jobs, owner, task, memory, token, verified = await bound_consumer(async_db, tmp_path, 'canonical_memory')
    async with async_db() as db:
        (await db.get(Memory, memory.id)).content = 'Correction before fresh terminal admission'
        await db.commit()
    with pytest.raises(BoardError) as error:
        await jobs.transition_job(verified['job_id'], 'succeeded',
            owner='terminal-consumer', fencing_token=verified['lease']['fencing_token'],
            expected_revision=verified['revision'])
    assert error.value.code == 'evidence_dependency_stale'
    current = await jobs.get_job(verified['job_id'])
    assert current['status'] == 'running' and current['revision'] == verified['revision']
    assert current['effects'] == verified['effects']
    async with async_db() as db:
        assert (await db.scalar(select(WorkBoardEvent).where(
            WorkBoardEvent.kind == 'task.evidence.execution_stale'))) is not None
