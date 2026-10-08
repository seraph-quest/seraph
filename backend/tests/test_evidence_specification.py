"""Real SQLite Specify evidence staging/CAS, with declared proposal setup.

The typed target is supplied as a server proposal fixture here; this does not
claim governed generation, auth, input producer, or whole accept API coverage.
"""
import json
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from config.settings import settings
from src.db.models import Memory, WorkBoardAttempt, WorkBoardEvent, WorkBoardEvidenceDependency
from src.memory.evidence_dependencies import _row_binding, digest
from src.memory.evidence_execution import ExecutionAcceptRequest, accept_execution, preview_execution
from src.memory.evidence_specification import stage_specification, recheck_specification, replace_specification_evidence
from src.work_board.contracts import WorkBoardProposalAccept
from src.work_board.repository import BoardError, WorkBoardRepository, _begin_sqlite_immediate
from tests.test_evidence_dependency_tokens import canonical_fact
from tests.test_evidence_execution_binding import packet_for


@pytest.mark.parametrize('ack',[False,1,'true',None])
def test_specification_execution_ack_requires_literal_true(ack):
    with pytest.raises(ValueError):
        WorkBoardProposalAccept(expected_proposal_revision=1,expected_parent_revision=2,
            execution_replacement={'expected_packet_revision':2,'expected_packet_digest':'a'*64,
                'acknowledge_execution_use':ack})


@pytest.mark.asyncio
@pytest.mark.parametrize('async_db',['file'],indirect=True)
@pytest.mark.parametrize('consumer',['browser.public-task.v1','work.evidence-dossier.v1','work.local-evidence-report.v1'])
async def test_specify_authority_preview_preserves_actual_consumer_prerequisites(async_db,consumer):
    from src.work_board.triage import _proposal_task_authority_summary
    from src.work_board.dispatcher import REGISTERED_CAPABILITIES, registered_executor_id
    async with async_db() as db:
        _owner,task,_memory,_source=await canonical_fact(db)
        summary=await _proposal_task_authority_summary(task,{'task_id':'reviewed-target',
            'capability_id':consumer,'capability_version':REGISTERED_CAPABILITIES[consumer].version,
            'executor_id':registered_executor_id(consumer),'typed_input_ref':'workspace-json:inputs/declared.json',
            'typed_input_digest':'e'*64})
        assert consumer in summary and 'Current provider-free preflight:' in summary
        assert 'grants no authority or external-effect approval' in summary


@pytest.mark.asyncio
@pytest.mark.parametrize('async_db',['file'],indirect=True)
@pytest.mark.parametrize('change',['unchanged','correction','typed-without-ack','unsupported',
    'replacement','task-race','packet-race','source-race','active-attempt'])
async def test_bound_specification_current_source_and_atomic_replacement(async_db,tmp_path,monkeypatch,change):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(settings,'workspace_dir',str(tmp_path))
    monkeypatch.setattr(WorkBoardRepository,'_safe_text',AsyncMock(side_effect=lambda value,**kw:value))
    async with async_db() as db:
        owner,task,memory,_=await canonical_fact(db)
        task.typed_input_ref='workspace-json:inputs/declared-original.json'
        await db.commit()
        request=await packet_for(db,task,memory)
        preview=await preview_execution(db,owner,task.task_id,request)
        accepted=ExecutionAcceptRequest(**request.model_dump(),preview_digest=preview['preview_digest'],
            idempotency_key=str(uuid4()),acknowledge_execution_use=True)
        await accept_execution(db,owner,task.task_id,accepted)
        await db.commit()
        task=await WorkBoardRepository().get_task(db,owner,task.task_id)
        task_id,memory_id=task.task_id,memory.id
        old=[_row_binding(row) for row in (await db.execute(select(WorkBoardEvidenceDependency))).scalars()]
        item={'capability_id':task.capability_id,'typed_input_ref':task.typed_input_ref,
            'typed_input_digest':task.typed_input_digest}
        replacement=change in {'replacement','task-race','packet-race','source-race'}
        if replacement or change=='typed-without-ack':item['typed_input_digest']='e'*64
        if change=='unsupported':item['capability_id']='guardian.local-task.v1'
        if change=='correction':
            (await db.get(Memory,memory_id)).content='Corrected source cannot accept an unchanged binding'
            await db.commit()
        if change=='active-attempt':
            db.add(WorkBoardAttempt(attempt_id='active',task_id=task_id));await db.commit()
        body=WorkBoardProposalAccept(expected_proposal_revision=1,expected_parent_revision=2,
            execution_replacement={'expected_packet_revision':2,
                'expected_packet_digest':request.expected_packet_digest,
                'acknowledge_execution_use':True} if replacement else None)
        if change in {'typed-without-ack','unsupported','active-attempt'}:
            with pytest.raises(BoardError):
                await stage_specification(db,owner,task,'specify',[item],body)
            return
        staged=await stage_specification(db,owner,task,'specify',[item],body)
        if change=='task-race':task.typed_input_ref='workspace-json:changed.json'
        elif change=='source-race':(await db.get(Memory,memory_id)).content='Correction wins before Specify CAS'
        elif change=='packet-race':
            db.add(WorkBoardEvent(task_id=task_id,owner_principal_id=owner.principal_id,
                owner_session_id=owner.session_id,actor_principal_id=owner.principal_id,
                kind='task.evidence.updated',metadata_json=json.dumps({'packet_revision':3,'packet_digest':'f'*64})))
        await db.commit()
        await _begin_sqlite_immediate(db)
        task=await WorkBoardRepository().get_task(db,owner,task_id)
        def forbidden(*a,**kw):raise AssertionError('Private I/O inside Specify writer')
        monkeypatch.setattr('src.memory.evidence_working_set._latest',forbidden)
        monkeypatch.setattr('src.memory.evidence_working_set._read_file',forbidden)
        monkeypatch.setattr('src.vault.crypto.decrypt',forbidden)
        if change in {'correction','task-race','packet-race','source-race'}:
            with pytest.raises(BoardError):await recheck_specification(db,owner,task,'specify',[item],staged)
            await db.rollback()
        else:
            await recheck_specification(db,owner,task,'specify',[item],staged)
            await WorkBoardRepository()._cas_task_update(db,owner,task,expected_revision=2,
                values={'typed_input_digest':item['typed_input_digest'],'task_revision':3})
            proposal=__import__('types').SimpleNamespace(proposal_id='declared-specify',proposal_digest='9'*64)
            await replace_specification_evidence(db,owner,task,proposal,staged)
            await db.commit()
    async with async_db() as reopened:
        task=await WorkBoardRepository().get_task(reopened,owner,task_id)
        rows=[_row_binding(row) for row in (await reopened.execute(select(WorkBoardEvidenceDependency))).scalars()]
        events=list((await reopened.execute(select(WorkBoardEvent).where(
            WorkBoardEvent.kind=='task.evidence.specification_replaced'))).scalars())
        if change=='replacement':
            assert task.task_revision==3 and task.typed_input_digest=='e'*64
            assert rows[0]['binding_task_revision']==3 and rows[0]['executor_input_digest']=='e'*64
            assert rows[0]['dependency_id']!=old[0]['dependency_id'] and len(events)==1
        else:
            assert rows==old and not events
            assert task.task_revision==(3 if change=='unchanged' else 2)
        with (tmp_path/'actual-specification-state.json').open('x') as f:
            json.dump({'boundary':__doc__,'change':change,'task_revision':task.task_revision,
                'old_binding_digest':digest(old),'current_binding_digest':digest(rows),
                'replacement_events':len(events),'no_learning':True},f,indent=2)
