"""Actual file-SQLite bounded impact; canonical binding setup is seeded."""
import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from src.db.models import Memory, WorkBoardAttempt, WorkBoardEvent, WorkBoardEvidenceDependency, WorkBoardStatus, WorkBoardTask
from src.memory.evidence_dependencies import canonical_source_token, digest
from src.memory.evidence_impact import ImpactRequest, evaluate_impact, inspect_impact
from src.work_board.repository import BoardError, WorkBoardRepository
from tests.test_evidence_dependency_tokens import canonical_fact


async def consumers(db,count=1):
    owner,anchor,memory,_=await canonical_fact(db)
    anchor.status=WorkBoardStatus.todo
    token=await canonical_source_token(db,owner,anchor,'canonical_memory',memory.id)
    tasks=[anchor]
    for index in range(count-1):
        task=WorkBoardTask(task_id=f'dependent-{index:03}',owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,goal_id=anchor.goal_id,capability_id=anchor.capability_id,
            idempotency_key=f'dependent-{index:03}',typed_input_digest=anchor.typed_input_digest,
            status=WorkBoardStatus.todo)
        db.add(task);tasks.append(task)
    await db.flush()
    for task in tasks:
        db.add(WorkBoardEvidenceDependency(task_id=task.task_id,owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,goal_id=anchor.goal_id,source_kind='canonical_memory',
            canonical_source_id=memory.id,source_id='c'*64,source_digest=digest(memory.content.encode()),
            span_digest='d'*64,resolved_token_json=json.dumps(token),packet_revision=1,
            packet_digest='b'*64,binding_task_revision=task.task_revision,executor_input_digest=task.typed_input_digest))
    await db.commit()
    return owner,anchor,memory,tasks


def request(page):
    return ImpactRequest(source_id=page['source_id'],cursor=page['cursor'],
        expected_snapshot_digest=page['snapshot_digest'],idempotency_key=str(uuid4()),acknowledge_safety_pause=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('async_db',['file'],indirect=True)
async def test_bounded_indexed_pages_pure_get_exact_retry_and_reopen(async_db):
    async with async_db() as db:
        owner,anchor,memory,tasks=await consumers(db,53)
        # Same canonical ID under a foreign owner cannot enter this page.
        foreign=WorkBoardTask(task_id='foreign',owner_principal_id='foreign-owner',
            owner_session_id='foreign-root',goal_id=anchor.goal_id,idempotency_key='foreign')
        db.add(foreign);await db.commit()
        memory.content='Correction requiring explicit safety evaluation';await db.commit()
        before=await db.scalar(select(func.count(WorkBoardEvent.event_id)))
        page=await inspect_impact(db,owner,anchor.task_id,'c'*64)
        assert len(page['tasks'])==50 and page['next_cursor'] and all(t['stale'] for t in page['tasks'])
        assert await db.scalar(select(func.count(WorkBoardEvent.event_id)))==before
        assert all(task.status==WorkBoardStatus.todo for task in tasks)
        body=request(page);first=await evaluate_impact(db,owner,anchor.task_id,body);await db.commit()
        assert len(first['paused_task_ids'])==50
        assert await evaluate_impact(db,owner,anchor.task_id,body)==first
        current=await inspect_impact(db,owner,anchor.task_id,'c'*64,page['next_cursor'])
        assert len(current['tasks'])==3 and current['next_cursor'] is None
        second=await evaluate_impact(db,owner,anchor.task_id,request(current));await db.commit()
        assert len(second['paused_task_ids'])==3
        with pytest.raises(BoardError) as conflict:
            await evaluate_impact(db,owner,anchor.task_id,body.model_copy(update={'expected_snapshot_digest':'f'*64}))
        assert conflict.value.code=='evidence_idempotency_conflict'
    async with async_db() as db:
        assert await evaluate_impact(db,owner,anchor.task_id,body)==first
        inspection=await inspect_impact(db,owner,anchor.task_id,'c'*64,pending=body)
        assert inspection['applied_result']==first
        assert await db.scalar(select(func.count(WorkBoardTask.creation_sequence)).where(
            WorkBoardTask.owner_principal_id==owner.principal_id,WorkBoardTask.status==WorkBoardStatus.blocked))==53
        assert not list((await db.execute(select(WorkBoardAttempt))).scalars())
        assert (await WorkBoardRepository().get_task(db,
            type(owner)(principal_id='foreign-owner',session_id='foreign-root'),'foreign')).status==WorkBoardStatus.triage


@pytest.mark.asyncio
@pytest.mark.parametrize('async_db',['file'],indirect=True)
async def test_impact_preserves_running_completed_review_and_other_blockers(async_db):
    async with async_db() as db:
        owner,anchor,memory,tasks=await consumers(db,4)
        for task,status in zip(tasks,[WorkBoardStatus.running,WorkBoardStatus.done,WorkBoardStatus.review,WorkBoardStatus.blocked]):
            task.status=status
        tasks[-1].block_kind='dependency';tasks[-1].block_reason='Different required handoff'
        db.add(WorkBoardAttempt(attempt_id='live-original',task_id=anchor.task_id))
        await db.commit();memory.content='Corrected while original attempts/history remain';await db.commit()
        revisions={task.task_id:task.task_revision for task in tasks}
        page=await inspect_impact(db,owner,anchor.task_id,'c'*64)
        applied=await evaluate_impact(db,owner,anchor.task_id,request(page));await db.commit()
        assert not applied['paused_task_ids'] and len(applied['retained_task_ids'])==4
        assert revisions=={task.task_id:task.task_revision for task in tasks}
        assert tasks[-1].block_reason=='Different required handoff'
        assert (await db.get(WorkBoardAttempt,'live-original')).ended_at is None
        assert not list((await db.execute(select(WorkBoardEvent).where(
            WorkBoardEvent.kind=='task.evidence.impact_paused'))).scalars())


@pytest.mark.asyncio
@pytest.mark.parametrize('async_db',['file'],indirect=True)
async def test_impact_snapshot_and_cursor_reject_correction_without_mutating(async_db):
    async with async_db() as db:
        owner,anchor,memory,tasks=await consumers(db,51)
        page=await inspect_impact(db,owner,anchor.task_id,'c'*64)
        body=request(page)
        memory.content='A correction after the inspected source snapshot';await db.commit()
        with pytest.raises(BoardError) as stale:
            await evaluate_impact(db,owner,anchor.task_id,body)
        assert stale.value.code=='evidence_impact_stale'
        with pytest.raises(BoardError) as cursor:
            await inspect_impact(db,owner,anchor.task_id,'c'*64,page['next_cursor'])
        assert cursor.value.code=='evidence_impact_cursor_stale'
        assert all(task.status==WorkBoardStatus.todo for task in tasks)
        assert not list((await db.execute(select(WorkBoardEvent).where(
            WorkBoardEvent.kind=='task.evidence.impact_evaluated'))).scalars())


@pytest.mark.parametrize('ack',[1,'true',False,None])
def test_impact_ack_is_literal_true(ack):
    with pytest.raises(ValueError):
        ImpactRequest(source_id='a'*64,expected_snapshot_digest='b'*64,
            idempotency_key=str(uuid4()),acknowledge_safety_pause=ack)


@pytest.mark.asyncio
@pytest.mark.parametrize('async_db',['file'],indirect=True)
@pytest.mark.parametrize('change',['task','source'])
async def test_writer_rechecks_current_page_after_staging_without_file_io(async_db,monkeypatch,change):
    import src.memory.evidence_impact as impact
    async with async_db() as db:
        owner,anchor,memory,tasks=await consumers(db)
        memory.content='First correction requiring inspection';await db.commit()
        anchor_id,memory_id=anchor.task_id,memory.id
        page=await inspect_impact(db,owner,anchor.task_id,'c'*64)
        original_begin=impact._begin_sqlite_immediate
        original_physical=impact._physical
        writer_started=False
        def physical(*args,**kwargs):
            assert not writer_started,'Physical inspection occurred inside writer'
            return original_physical(*args,**kwargs)
        async def barrier(session):
            nonlocal writer_started
            async with async_db() as other:
                if change=='task':
                    current=await WorkBoardRepository().get_task(other,owner,anchor_id)
                    current.task_revision+=1
                else:
                    (await other.get(Memory,memory_id)).content='Another correction wins before writer'
                await other.commit()
            await original_begin(session);writer_started=True
        monkeypatch.setattr(impact,'_physical',physical)
        monkeypatch.setattr(impact,'_begin_sqlite_immediate',barrier)
        with pytest.raises(BoardError) as error:
            await evaluate_impact(db,owner,anchor_id,request(page))
        assert error.value.code=='evidence_impact_stale'
        await db.rollback()
    async with async_db() as reopened:
        assert (await WorkBoardRepository().get_task(reopened,owner,anchor_id)).status==WorkBoardStatus.todo
        assert not list((await reopened.execute(select(WorkBoardEvent).where(
            WorkBoardEvent.kind=='task.evidence.impact_evaluated'))).scalars())
