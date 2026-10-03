"""Actual reopened SQLite contact CAS; proposal setup explicitly seeded."""
from datetime import datetime,timedelta,timezone
import json
import pytest
from src.db.models import WorkBoardProposal
from src.work_board.triage import _claim_proposal_contact
from src.work_board.repository import _begin_sqlite_immediate


@pytest.mark.asyncio
@pytest.mark.parametrize('async_db',['file'],indirect=True)
@pytest.mark.parametrize('state',['current','expired','already-started','blocked'])
async def test_proposal_contact_cas_uses_sql_utc_expiry_and_remains_once_only(async_db,tmp_path,state):
    now=datetime.now(timezone.utc)
    async with async_db() as db:
        row=WorkBoardProposal(owner_principal_id='fixture-owner',owner_session_id='fixture-root',
            parent_task_id='fixture-task',kind='specify',idempotency_key='exact-contact-cas',
            expires_at=now+timedelta(seconds=-1 if state=='expired' else 60),
            status='blocked' if state=='blocked' else 'pending_inference',
            provider_contact_started=state=='already-started',
            provider_contact_state='started' if state=='already-started' else 'not_started')
        db.add(row);await db.commit();proposal_id=row.proposal_id
    async with async_db() as db:
        await _begin_sqlite_immediate(db)
        loaded=await db.get(WorkBoardProposal,proposal_id)
        assert loaded.expires_at.tzinfo is None  # Actual SQLite readback premise.
        claimed=await _claim_proposal_contact(db,proposal_id,now)
        assert claimed.rowcount==(1 if state=='current' else 0)
        repeated=await _claim_proposal_contact(db,proposal_id,now)
        assert repeated.rowcount==0
        await db.commit()
    async with async_db() as reopened:
        actual=await reopened.get(WorkBoardProposal,proposal_id)
        assert actual.provider_contact_started==(state in {'current','already-started'})
        assert actual.provider_contact_state==('started' if state in {'current','already-started'} else 'not_started')
        with (tmp_path/'actual-sqlite-contact-cas.json').open('x') as f:json.dump({'state':state,
            'original_expiry':actual.expires_at.isoformat(),'once_only':True,
            'boundary':__doc__,'no_provider_contact':True},f,indent=2)
