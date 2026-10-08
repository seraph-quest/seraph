"""Real private packet staging and pure proposal-source CAS comparison."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from config.settings import settings
from src.db.models import Memory, WorkBoardEvent, WorkBoardProposal
from src.memory.evidence_proposal import stage_context, recheck_context, stage_proposal_context
from src.memory.evidence_working_set import evidence_for_task_context
from src.work_board.repository import BoardError, WorkBoardRepository, _begin_sqlite_immediate
from tests.test_evidence_dependency_tokens import canonical_fact
from tests.test_evidence_execution_binding import packet_for


async def setup_context(db, monkeypatch):
    owner,task,memory,_ = await canonical_fact(db)
    # Model-context provenance is separate from finite execution eligibility.
    task.capability_id='guardian.local-task.v1'
    await db.commit()
    packet=await packet_for(db,task,memory)
    db.add(WorkBoardEvent(task_id=task.task_id,owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,actor_principal_id=owner.principal_id,
        kind='task.evidence.adopted',metadata_json=json.dumps({'packet_revision':2,
            'packet_digest':packet.expected_packet_digest,'task_revision':task.task_revision})))
    await db.commit()
    return owner,task,memory


@pytest.mark.asyncio
@pytest.mark.parametrize('async_db',['file'],indirect=True)
@pytest.mark.parametrize('change',['none','correction','packet','revocation'])
async def test_actual_used_snapshot_rechecks_without_private_io_in_writer(async_db,tmp_path,monkeypatch,change):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(settings,'workspace_dir',str(tmp_path))
    monkeypatch.setattr(WorkBoardRepository,'_safe_text',AsyncMock(side_effect=lambda value,**kw:value))
    async with async_db() as db:
        owner,task,memory=await setup_context(db,monkeypatch)
        context=await evidence_for_task_context(db,owner,task.task_id,'actual-prompt')
        staged,value=await stage_context(db,owner,task,'actual-prompt')
        assert memory.content not in json.dumps(value)
        if change=='correction':(await db.get(Memory,memory.id)).content='Corrected fact'
        elif change in {'packet','revocation'}:
            db.add(WorkBoardEvent(task_id=task.task_id,owner_principal_id=owner.principal_id,
                owner_session_id=owner.session_id,actor_principal_id=owner.principal_id,
                kind='task.evidence.updated' if change=='packet' else 'task.evidence.revoked',
                metadata_json=json.dumps({'packet_revision':3 if change=='packet' else 2,
                    'packet_digest':'a'*64,'task_revision':task.task_revision})))
        await db.commit()
        await _begin_sqlite_immediate(db)
        def forbidden(*args,**kwargs):raise AssertionError('private reader inside proposal writer')
        monkeypatch.setattr('src.memory.evidence_working_set._latest',forbidden)
        monkeypatch.setattr('src.memory.evidence_working_set._read_file',forbidden)
        monkeypatch.setattr('src.vault.crypto.decrypt',forbidden)
        if change=='none':
            await recheck_context(db,owner,task,staged,value,prepared_context=context)
        else:
            with pytest.raises(BoardError):
                await recheck_context(db,owner,task,staged,value,prepared_context=context)


@pytest.mark.asyncio
@pytest.mark.parametrize('async_db',['file'],indirect=True)
async def test_historical_source_derived_proposal_cannot_use_missing_snapshot(async_db,tmp_path,monkeypatch):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(settings,'workspace_dir',str(tmp_path))
    monkeypatch.setattr(WorkBoardRepository,'_safe_text',AsyncMock(side_effect=lambda value,**kw:value))
    async with async_db() as db:
        owner,task,_=await setup_context(db,monkeypatch)
        proposal=WorkBoardProposal(owner_principal_id=owner.principal_id,owner_session_id=owner.session_id,
            parent_task_id=task.task_id,kind='specify',idempotency_key='historical-source-derived',
            admission_job_id='original-proposal-job',expires_at=datetime.now(timezone.utc)+timedelta(minutes=5))
        db.add(proposal)
        db.add(WorkBoardEvent(task_id=task.task_id,owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,actor_principal_id=owner.principal_id,
            kind='task.evidence.used',metadata_json=json.dumps({'packet_revision':2,
                'packet_digest':'b'*64,'job_id':proposal.admission_job_id})))
        await db.commit()
        with pytest.raises(BoardError) as failure:
            await stage_proposal_context(db,owner,task,proposal)
        assert failure.value.code=='proposal_evidence_regeneration_required'
        assert proposal.evidence_use_snapshot_json is None
