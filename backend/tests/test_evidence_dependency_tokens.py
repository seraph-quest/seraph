"""Canonical source token mechanics; no executable-authority fixture claims."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from src.db.models import Goal, Memory, MemorySource, MemoryTombstone, WorkBoardEvent, WorkBoardTask
from src.memory.evidence_dependencies import (
    ResolvedSource, StagedEvidence, canonical_source_token, digest, recheck_staged,
)
from src.work_board.contracts import WorkBoardOwner
from src.work_board.repository import BoardError, _begin_sqlite_immediate


async def canonical_fact(db):
    owner = WorkBoardOwner(principal_id='operator:source', session_id='source-session')
    goal = Goal(id='source-goal', title='Canonical source scope',
                owner_principal_id=owner.principal_id, owner_session_id=owner.session_id)
    task = WorkBoardTask(task_id='bound-task', owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id, goal_id=goal.id, capability_id='browser.public-task.v1',
        idempotency_key='bound-task', typed_input_digest='a'*64)
    memory = Memory(id='selected-fact', source_session_id=owner.session_id,
        content='An exact private operator fact', metadata_json=json.dumps({'goal_id':goal.id}))
    source = MemorySource(memory_id=memory.id, source_type='operator', source_session_id=owner.session_id)
    db.add_all([goal, task, memory])
    await db.flush()
    db.add(source)
    db.add(WorkBoardEvent(task_id=task.task_id, owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id, actor_principal_id=owner.principal_id,
        kind='task.evidence.updated', metadata_json=json.dumps({'packet_revision':1,'packet_digest':'b'*64})))
    await db.commit()
    return owner, task, memory, source


@pytest.mark.asyncio
@pytest.mark.parametrize('async_db', ['file'], indirect=True)
@pytest.mark.parametrize('change', ['content','metadata','time','tombstone','deleted','foreign-source','foreign-memory','goal-owner'])
async def test_canonical_correction_invalidates_exact_staged_fact_inside_writer(async_db, change, monkeypatch):
    async with async_db() as db:
        owner,task,memory,source = await canonical_fact(db)
        token = await canonical_source_token(db,owner,task,'canonical_memory',memory.id)
        assert memory.content not in json.dumps(token)
        staged = StagedEvidence(task.task_id,task.task_revision,1,'b'*64,
            (ResolvedSource('c'*64,'canonical_memory',memory.id,digest(memory.content.encode()),
                'd'*64,1,1,memory.updated_at.isoformat(),json.dumps(token)),),task.capability_id,task.goal_id,task.goal_revision,'active')
        await db.commit()
        if change == 'content': memory.content = 'The operator corrected this fact'
        elif change == 'metadata': memory.metadata_json = json.dumps({'goal_id':task.goal_id,'privacy_boundary':'private'})
        elif change == 'time': memory.updated_at = datetime.now(timezone.utc)+timedelta(seconds=2)
        elif change == 'tombstone': db.add(MemoryTombstone(memory_id=memory.id))
        elif change == 'deleted':
            await db.delete(source)
            await db.flush()
            await db.delete(memory)
        elif change == 'foreign-source': source.source_session_id = 'another-owner'
        elif change == 'foreign-memory': memory.source_session_id = 'another-owner'
        elif change == 'goal-owner': (await db.get(Goal,task.goal_id)).owner_principal_id = 'another-owner'
        await db.commit()
        await _begin_sqlite_immediate(db)
        # Any source/file/decryption reader call inside this recheck is a bug.
        def forbidden(*args,**kwargs): raise AssertionError('private I/O inside canonical writer')
        monkeypatch.setattr('src.memory.evidence_working_set._read_file',forbidden)
        monkeypatch.setattr('src.vault.crypto.decrypt',forbidden)
        with pytest.raises(BoardError) as error:
            await recheck_staged(db,owner,task,staged)
        assert error.value.code == 'evidence_dependency_stale'


@pytest.mark.asyncio
@pytest.mark.parametrize('async_db', ['file'], indirect=True)
async def test_unaffected_canonical_sibling_and_same_bytes_survive_fresh_session(async_db):
    async with async_db() as db:
        owner,task,memory,source = await canonical_fact(db)
        sibling = Memory(id='unselected-sibling',source_session_id=owner.session_id,
            content='Independent fact',metadata_json=json.dumps({'goal_id':task.goal_id}))
        db.add(sibling);await db.commit()
        token = await canonical_source_token(db,owner,task,'canonical_memory',memory.id)
        sibling.content='Sibling correction cannot change the selected source token'
        await db.commit()
        assert await canonical_source_token(db,owner,task,'canonical_memory',memory.id) == token
    async with async_db() as reopened:
        loaded = (await reopened.get(Memory,memory.id))
        assert await canonical_source_token(reopened,owner,task,'canonical_memory',loaded.id) == token


@pytest.mark.asyncio
@pytest.mark.parametrize('async_db', ['file'], indirect=True)
async def test_unregistered_output_is_never_an_execution_dependency(async_db):
    async with async_db() as db:
        owner,task,memory,source = await canonical_fact(db)
        with pytest.raises(BoardError) as error:
            await canonical_source_token(db,owner,task,'document_summary','arbitrary-artifact',
                {'task_id':'producer','attempt_id':'attempt','run_id':'run'})
        assert error.value.code == 'evidence_dependency_unsupported'


@pytest.mark.asyncio
@pytest.mark.parametrize('async_db', ['file'], indirect=True)
@pytest.mark.parametrize('consumer', ['browser.public-task.v1','work.evidence-dossier.v1','work.local-evidence-report.v1'])
async def test_canonical_fact_remains_eligible_for_each_exact_consumer(async_db, consumer):
    async with async_db() as db:
        owner,task,memory,_source = await canonical_fact(db)
        task.capability_id=consumer
        await db.commit()
        assert (await canonical_source_token(db,owner,task,'canonical_memory',memory.id))['canonical_source_id']==memory.id


@pytest.mark.asyncio
@pytest.mark.parametrize('async_db', ['file'], indirect=True)
@pytest.mark.parametrize('change', ['task-revision','capability','packet-revision','packet-digest','missing-packet'])
async def test_task_and_packet_snapshot_rechecked_in_same_actual_writer(async_db, change, monkeypatch):
    from sqlalchemy import delete
    async with async_db() as db:
        owner,task,memory,_source=await canonical_fact(db)
        token=await canonical_source_token(db,owner,task,'canonical_memory',memory.id)
        staged=StagedEvidence(task.task_id,task.task_revision,1,'b'*64,
            (ResolvedSource('c'*64,'canonical_memory',memory.id,digest(memory.content.encode()),
                'd'*64,1,1,memory.updated_at.isoformat(),json.dumps(token)),),task.capability_id,task.goal_id,task.goal_revision,'active')
        await db.commit()
        if change=='task-revision':task.task_revision+=1
        elif change=='capability':task.capability_id='work.evidence-dossier.v1'
        elif change=='missing-packet':await db.execute(delete(WorkBoardEvent).where(WorkBoardEvent.task_id==task.task_id))
        else:db.add(WorkBoardEvent(task_id=task.task_id,owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,actor_principal_id=owner.principal_id,kind='task.evidence.updated',
            metadata_json=json.dumps({'packet_revision':2 if change=='packet-revision' else 1,
                                     'packet_digest':'e'*64 if change=='packet-digest' else 'b'*64})))
        await db.commit()
        await _begin_sqlite_immediate(db)
        def forbidden(*args,**kwargs):raise AssertionError('physical packet read inside writer')
        monkeypatch.setattr('src.memory.evidence_working_set._latest',forbidden)
        monkeypatch.setattr('src.memory.evidence_working_set._read_file',forbidden)
        with pytest.raises(BoardError) as error:await recheck_staged(db,owner,task,staged)
        assert error.value.code=='evidence_dependency_stale'


@pytest.mark.parametrize('kind,consumer', [
    (kind,consumer) for kind,producer in {
        'browser_public_task_result':'browser.public-task.v1',
        'evidence_dossier':'work.evidence-dossier.v1',
        'evidence_local_report':'work.local-evidence-report.v1'}.items()
    for consumer in ['browser.public-task.v1','work.evidence-dossier.v1','work.local-evidence-report.v1']
    if producer!=consumer])
@pytest.mark.asyncio
async def test_six_artifact_cross_pairs_fail_before_any_source_resolution(kind,consumer):
    task=WorkBoardTask(task_id='matrix',goal_id='goal',owner_principal_id='owner',owner_session_id='session',
                       capability_id=consumer,idempotency_key='matrix')
    with pytest.raises(BoardError) as error:
        await canonical_source_token(None,WorkBoardOwner(principal_id='owner',session_id='session'),task,kind,'artifact')
    assert error.value.code=='evidence_dependency_unsupported'
