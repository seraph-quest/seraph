"""Actual file-SQLite first admission; canonical source fixture is declared."""
import json

import pytest
from sqlalchemy import select

from src.db.models import Memory, WorkBoardAttempt, WorkBoardEvent, WorkBoardEvidenceDependency, WorkflowRunState
from src.memory.evidence_dependencies import canonical_source_token, digest
from src.work_board.repository import BoardError
from src.workflows.job_runtime import DurableJobIdentity, DurableJobRepository, DurableJobSpec
from tests.test_evidence_dependency_tokens import canonical_fact


@pytest.mark.asyncio
@pytest.mark.parametrize('async_db', ['file'], indirect=True)
@pytest.mark.parametrize('consumer,kind', [('browser.public-task.v1','browser_public_task'),
    ('work.evidence-dossier.v1','work.evidence-dossier.v1'),
    ('work.local-evidence-report.v1','work.local-evidence-report.v1')])
@pytest.mark.parametrize('corrected', [False, True, 'malformed'])
async def test_native_first_admission_guards_all_fixed_consumers(async_db, consumer, kind, corrected):
    async with async_db() as db:
        owner, task, memory, _ = await canonical_fact(db)
        task.capability_id = consumer
        await db.commit()
        token = await canonical_source_token(db, owner, task, 'canonical_memory', memory.id)
        db.add(WorkBoardEvidenceDependency(task_id=task.task_id,
            owner_principal_id=owner.principal_id, owner_session_id=owner.session_id,
            goal_id=task.goal_id, source_kind='canonical_memory', canonical_source_id=memory.id,
            source_id='c'*64, source_digest=digest(memory.content.encode()), span_digest='d'*64,
            resolved_token_json=json.dumps(token), packet_revision=1, packet_digest='b'*64,
            binding_task_revision=task.task_revision, executor_input_digest=task.typed_input_digest))
        db.add(WorkBoardAttempt(attempt_id='admitted-attempt', task_id=task.task_id,
            workflow_run_id='first-bound-native'))
        await db.commit()
        if corrected == 'malformed':
            row=await db.scalar(select(WorkBoardEvidenceDependency).where(
                WorkBoardEvidenceDependency.task_id==task.task_id))
            row.resolved_token_json='invalid protected metadata'
            await db.commit()
        elif corrected:
            memory.content = 'Canonical correction before native admission'
            await db.commit()
        spec = DurableJobSpec(identity=DurableJobIdentity(job_id='first-bound-native',
            owner_kind='user', owner_principal_id=owner.principal_id, job_kind=kind,
            capability_version='1', idempotency_scope='work-board-attempt',
            idempotency_key=task.task_id+':admitted-attempt'),
            session_id=owner.session_id, operator_session_id=owner.session_id,
            goal_id=task.goal_id, goal_revision=task.goal_revision,
            declared_authority={'principal':owner.principal_id, 'owner_kind':'user',
                'operator_owner_principal_id':owner.principal_id})
    jobs = DurableJobRepository()
    if corrected:
        with pytest.raises(BoardError) as exc:
            await jobs.admit_job(spec)
        assert exc.value.code in {'evidence_dependency_stale','evidence_dependency_invalid'}
        async with async_db() as reopened:
            assert await reopened.scalar(select(WorkflowRunState).where(
                WorkflowRunState.run_identity=='first-bound-native')) is None
            assert await reopened.scalar(select(WorkBoardEvent).where(
                WorkBoardEvent.kind=='task.evidence.execution_stale')) is not None
            assert await reopened.get(WorkBoardAttempt,'admitted-attempt') is not None
    else:
        admitted = await jobs.admit_job(spec)
        assert admitted['status']=='accepted'
        async with async_db() as db:
            (await db.get(Memory,memory.id)).content='Correction after immutable admission'
            await db.commit()
        replay = await jobs.admit_job(spec)
        assert replay['job_id']==admitted['job_id'] and replay['revision']==admitted['revision']
        assert replay['attempt_count']==0
