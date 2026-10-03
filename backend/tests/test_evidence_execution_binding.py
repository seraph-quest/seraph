"""Actual file/SQLite execution binding; no provider or authority proof claim."""
import json
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from config.settings import settings
from src.db.models import Memory, WorkBoardAttempt, WorkBoardEvent, WorkBoardEvidenceDependency, WorkBoardStatus
from src.memory.evidence_dependencies import canonical_source_token, digest, stage_dependencies, recheck_dependencies
from src.memory.evidence_execution import ExecutionAcceptRequest, ExecutionPreviewRequest, accept_execution, preview_execution
from src.memory.evidence_working_set import _source_id, _write_packet
from src.work_board.repository import BoardError, WorkBoardRepository, _begin_sqlite_immediate
from tests.test_evidence_dependency_tokens import canonical_fact


async def packet_for(db, task, memory):
    packet = {'schema_version': 1, 'task_id': task.task_id, 'goal_id': task.goal_id,
        'goal_revision': task.goal_revision, 'revision': 2, 'query': '',
        'excluded_source_ids': [], 'allow_model_context': False,
        'citations': [{'source_id': _source_id('canonical_memory', memory.id),
            'source_digest': digest(memory.content.encode()), 'span_digest': digest(memory.content.encode()),
            'version': memory.updated_at.replace(tzinfo=__import__('datetime').timezone.utc).isoformat(),
            'line_start': 1, 'line_end': 1}]}
    packet_digest = _write_packet(task, packet)
    db.add(WorkBoardEvent(task_id=task.task_id, owner_principal_id=task.owner_principal_id,
        owner_session_id=task.owner_session_id, actor_principal_id=task.owner_principal_id,
        kind='task.evidence.updated', metadata_json=json.dumps({'packet_revision':2,'packet_digest':packet_digest})))
    await db.commit()
    return ExecutionPreviewRequest(expected_task_revision=task.task_revision,
        expected_packet_revision=2, expected_packet_digest=packet_digest)


@pytest.mark.asyncio
@pytest.mark.parametrize('async_db',['file'],indirect=True)
async def test_real_packet_binding_exact_retry_and_correction_require_fresh_review(async_db, tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(settings,'workspace_dir',str(tmp_path))
    # Only vault redaction is intercepted in this repository-level file proof.
    monkeypatch.setattr(WorkBoardRepository,'_safe_text',AsyncMock(side_effect=lambda value, **kw:value))
    async with async_db() as db:
        owner,task,memory,_ = await canonical_fact(db)
        request = await packet_for(db,task,memory)
        preview = await preview_execution(db,owner,task.task_id,request)
        accepted = ExecutionAcceptRequest(**request.model_dump(), preview_digest=preview['preview_digest'],
            idempotency_key=str(uuid4()),acknowledge_execution_use=True)
        result = await accept_execution(db,owner,task.task_id,accepted)
        assert result['binding_state']=='bound' and result['task_revision']==2
        await db.commit()
    async with async_db() as db:
        assert await accept_execution(db,owner,task.task_id,accepted)==result
        rows=list((await db.execute(select(WorkBoardEvidenceDependency))).scalars())
        assert len(rows)==1 and memory.content not in rows[0].resolved_token_json
        changed = accepted.model_copy(update={'operation':'revoke'})
        with pytest.raises(BoardError,match='another exact request'):
            await accept_execution(db,owner,task.task_id,changed)
        current=await WorkBoardRepository().get_task(db,owner,task.task_id)
        staged=await stage_dependencies(db,current)
        (await db.get(Memory,memory.id)).content='A corrected private operator fact'
        await db.commit()
        await _begin_sqlite_immediate(db)
        with pytest.raises(BoardError) as failure:
            await recheck_dependencies(db,current,staged)
        assert failure.value.code=='evidence_dependency_stale'
        # Removing the stale evidence is not an execution bypass.
        await db.rollback()
        with pytest.raises(BoardError):
            await preview_execution(db,owner,task.task_id,request.model_copy(update={
                'expected_task_revision':2,'operation':'revoke'}))
    async with async_db() as db:
        assert len(list((await db.execute(select(WorkBoardEvidenceDependency))).scalars()))==1
        assert len(list((await db.execute(select(WorkBoardAttempt))).scalars()))==0


@pytest.mark.asyncio
@pytest.mark.parametrize('async_db',['file'],indirect=True)
@pytest.mark.parametrize('consumer',['browser.public-task.v1','work.evidence-dossier.v1','work.local-evidence-report.v1'])
async def test_stale_claim_commits_block_before_creating_attempt(async_db,consumer,monkeypatch):
    async with async_db() as db:
        owner,task,memory,_ = await canonical_fact(db)
        task.capability_id=consumer
        task.status=WorkBoardStatus.ready
        await db.commit()
        token=await canonical_source_token(db,owner,task,'canonical_memory',memory.id)
        db.add(WorkBoardEvidenceDependency(task_id=task.task_id,owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,goal_id=task.goal_id,source_kind='canonical_memory',
            canonical_source_id=memory.id,source_id='c'*64,source_digest=digest(memory.content.encode()),
            span_digest='d'*64,resolved_token_json=json.dumps(token),packet_revision=1,packet_digest='b'*64,
            binding_task_revision=1,executor_input_digest=task.typed_input_digest))
        await db.commit()
        memory.content='Correction wins before Ready claim'
        await db.commit()
        # This branch must not decrypt or inspect files under the writer.
        def forbidden(*args,**kwargs):raise AssertionError('I/O inside stale claim writer')
        monkeypatch.setattr('src.memory.evidence_working_set._read_file',forbidden)
        monkeypatch.setattr(WorkBoardRepository,'_safe_text',forbidden)
        assert await WorkBoardRepository().claim_ready_task(db,task.task_id,
            expected_revision=task.task_revision,lease_owner='test-claim') is None
        assert task.status==WorkBoardStatus.blocked and task.block_kind=='dependency'
    async with async_db() as reopened:
        current=await WorkBoardRepository().get_task(reopened,owner,task.task_id)
        assert current.status==WorkBoardStatus.blocked
        assert len(list((await reopened.execute(select(WorkBoardAttempt))).scalars()))==0
        assert (await reopened.scalar(select(WorkBoardEvent).where(
            WorkBoardEvent.kind=='task.dispatch_blocked'))) is not None


@pytest.mark.parametrize('ack',[False,1,'true',None])
def test_execution_acknowledgment_is_literal_true(ack):
    with pytest.raises(ValueError):
        ExecutionAcceptRequest(expected_task_revision=1,expected_packet_revision=1,
            expected_packet_digest='a'*64,preview_digest='b'*64,idempotency_key=str(uuid4()),
            acknowledge_execution_use=ack)


@pytest.mark.asyncio
@pytest.mark.parametrize('async_db',['file'],indirect=True)
@pytest.mark.parametrize('other_block',[False, True])
async def test_rebind_clears_only_exact_canonical_evidence_pause(async_db,tmp_path,monkeypatch,other_block):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(settings,'workspace_dir',str(tmp_path))
    monkeypatch.setattr(WorkBoardRepository,'_safe_text',AsyncMock(side_effect=lambda value,**kw:value))
    async with async_db() as db:
        owner,task,memory,_=await canonical_fact(db)
        task.capability_id='work.evidence-dossier.v1'
        task.executor_id='seraph-work-board:work.evidence-dossier.v1'
        await db.commit()
        request=await packet_for(db,task,memory)
        preview=await preview_execution(db,owner,task.task_id,request)
        await accept_execution(db,owner,task.task_id,ExecutionAcceptRequest(**request.model_dump(),
            preview_digest=preview['preview_digest'],idempotency_key=str(uuid4()),acknowledge_execution_use=True))
        await db.commit()
        task=await WorkBoardRepository().get_task(db,owner,task.task_id)
        task.status=WorkBoardStatus.ready
        memory.content='Reviewed replacement private operator fact'
        await db.commit()
        assert await WorkBoardRepository().claim_ready_task(db,task.task_id,
            expected_revision=task.task_revision,lease_owner='guarded-dispatch') is None
        await db.commit()
        if other_block:
            db.add(WorkBoardEvent(task_id=task.task_id,owner_principal_id=owner.principal_id,
                owner_session_id=owner.session_id,actor_principal_id=owner.principal_id,
                kind='task.dispatch_blocked',metadata_json=json.dumps({'task_revision':task.task_revision,
                    'reason_code':'handoff_materialization_required'})))
            await db.commit()
        replacement=await packet_for(db,task,memory)
        refreshed=await preview_execution(db,owner,task.task_id,replacement)
        result=await accept_execution(db,owner,task.task_id,ExecutionAcceptRequest(**replacement.model_dump(),
            preview_digest=refreshed['preview_digest'],idempotency_key=str(uuid4()),acknowledge_execution_use=True))
        await db.commit()
        assert result['cleared_stale_evidence_pause'] is (not other_block)
        assert task.status==(WorkBoardStatus.blocked if other_block else WorkBoardStatus.todo)
        assert task.typed_input_digest=='a'*64
    async with async_db() as reopened:
        current=await WorkBoardRepository().get_task(reopened,owner,task.task_id)
        assert current.status==(WorkBoardStatus.blocked if other_block else WorkBoardStatus.todo)
        assert not list((await reopened.execute(select(WorkBoardAttempt))).scalars())
