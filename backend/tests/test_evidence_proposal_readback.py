"""Actual SQLite output/readback CAS; seeded proposal/job and policy-lock fixture.

This isolates canonical handoff races, not governed model generation. The
managed journey supplies the actual producer and lifecycle policy binding.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import fcntl
import json
import os

import pytest
from unittest.mock import AsyncMock

from config.settings import settings
from src.db.models import Memory, OperatorSession, Session, WorkBoardProposal, WorkflowRunState
from src.memory.evidence_proposal import stage_context
from src.work_board.repository import BoardError, WorkBoardRepository
from src.work_board import triage
from src.workflows.job_runtime import durable_job_repository
from tests.test_evidence_proposal_snapshot import setup_context


@pytest.mark.asyncio
@pytest.mark.parametrize('async_db',['file'],indirect=True)
@pytest.mark.parametrize('change',['none','source-race','revoked-root','output-race','busy','policy-race','native-admit'])
async def test_generated_output_readback_is_current_fenced_and_pure(async_db,tmp_path,monkeypatch,change):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(settings,'workspace_dir',str(tmp_path))
    monkeypatch.setattr(WorkBoardRepository,'_safe_text',AsyncMock(side_effect=lambda value,**kw:value))
    now=datetime.now(timezone.utc)
    async with async_db() as db:
        owner,task,memory=await setup_context(db,monkeypatch)
        db.add(Session(id=owner.session_id,owner_principal_id=owner.principal_id))
        await db.flush()
        _,snapshot=await stage_context(db,owner,task,'original-proposal-job')
        output={'proposed_tasks':[{'title':'Advisory fixture only'}],'proposed_links':[],'blocked_reason':None,
            'evidence_snapshot_digest':triage._proposal_digest(snapshot)}
        digest=triage._proposal_digest(output)
        proposal=WorkBoardProposal(proposal_id='verified-advisory-proposal',owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,parent_task_id=task.task_id,parent_revision=task.task_revision,
            goal_id=task.goal_id,goal_revision=task.goal_revision,kind='specify',idempotency_key='fixture-readback',
            admission_job_id='original-proposal-job',expires_at=now+timedelta(minutes=5),
            provider_contact_started=True,provider_contact_state='started',status='pending_inference',
            capability_id='strategist_agent',capability_version='fixture-version',grant_revision=task.goal_revision,
            route_id='strategist_agent',authority_digest='a'*64,request_digest='b'*64,
            evidence_use_snapshot_json=json.dumps(snapshot),proposal_json=json.dumps(output),proposal_digest=digest)
        authority=triage._proposal_job_authority(proposal)
        run=WorkflowRunState(id=proposal.admission_job_id,run_identity=proposal.admission_job_id,root_run_identity=proposal.admission_job_id,
            branch_kind='root',workflow_name='proposal-fixture',
            owner_kind='user',owner_principal_id=owner.principal_id,session_id=owner.session_id,
            operator_session_id=owner.session_id,goal_id=task.goal_id,goal_revision=task.goal_revision,
            job_kind='work_board_proposal',capability_version=proposal.capability_version,status='running',
            lease_owner='fixture-runner',lease_expires_at=now+timedelta(minutes=3),fencing_token=1,
            deadline_at=now+timedelta(minutes=4),idempotency_scope='work-board-proposal',
            idempotency_key=proposal.proposal_id,input_digest=triage._proposal_admission_input_digest(proposal),
            authority_digest=triage._proposal_digest(authority),run_fingerprint=proposal.request_digest,
            declared_authority_json=json.dumps(authority),effect_receipts_json='[]')
        db.add_all([proposal,OperatorSession(id=owner.session_id,principal_id=owner.principal_id,
            token_hash='fixture-token',idle_expires_at=now+timedelta(hours=1),absolute_expires_at=now+timedelta(hours=1))])
        if change!='native-admit':db.add(run)
        await db.commit()
        memory_id=memory.id
    lease_owner,fence='fixture-runner',1
    if change=='native-admit':
        binding=await triage._admit_proposal_job(owner=owner,task=task,proposal=proposal)
        assert binding is not None
        _,lease_owner,fence=binding
    operator=SimpleNamespace(session_id=owner.session_id,principal=SimpleNamespace(principal_id=owner.principal_id),
        ownership_continuity='stable',_token_hash='fixture-token')
    lock_path=tmp_path/'policy.lock'
    lock_observed=[]
    @contextmanager
    def policy_lock(root):
        assert str(root)==str(tmp_path)
        fd=os.open(lock_path,os.O_CREAT|os.O_RDWR,0o600)
        rival=os.open(lock_path,os.O_RDWR)
        try:
            if change=='busy':fcntl.flock(rival,fcntl.LOCK_EX|fcntl.LOCK_NB)
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            lock_observed.append(True)
            yield
        finally:os.close(rival);os.close(fd)
    monkeypatch.setattr('src.workspace.accounting_witness.maintenance_accounting_lock',policy_lock)
    monkeypatch.setattr(triage,'_route_binding',lambda:('strategist_agent','fixture-version'))
    monkeypatch.setattr(triage,'_authority_digest',lambda *args:'a'*64)
    original=durable_job_repository.record_readback
    async def barrier(*args,**kwargs):
        if change in {'source-race','revoked-root','output-race'}:
            async with async_db() as db:
                if change=='source-race':(await db.get(Memory,memory_id)).content='Correction before positive append'
                elif change=='revoked-root':(await db.get(OperatorSession,owner.session_id)).revoked_at=now
                else:(await db.get(WorkBoardProposal,proposal.proposal_id)).proposal_digest='f'*64
                await db.commit()
        if change=='policy-race':
            from src.workspace.accounting_witness import publish_policy_configuration
            # Actual existing publisher hits the held flock before any file
            # write. Its production lifecycle metadata is a declared fixture.
            with pytest.raises(BlockingIOError):publish_policy_configuration(tmp_path,{})
        def forbidden(*args,**kwargs):raise AssertionError('Physical read inside readback/terminal writer')
        monkeypatch.setattr('src.memory.evidence_working_set._latest',forbidden)
        monkeypatch.setattr('src.memory.evidence_working_set._read_file',forbidden)
        monkeypatch.setattr('src.vault.crypto.decrypt',forbidden)
        return await original(*args,**kwargs)
    monkeypatch.setattr(durable_job_repository,'record_readback',barrier)
    call=triage._complete_generated_proposal(owner,proposal.proposal_id,digest,operator=operator,
        job_id=proposal.admission_job_id,lease_owner=lease_owner,fence=fence)
    if change in {'source-race','revoked-root','output-race','busy'}:
        with pytest.raises((BoardError,BlockingIOError)):await call
    else:await call
    async with async_db() as reopened:
        from sqlalchemy import select
        run=await reopened.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==proposal.admission_job_id))
        effects=json.loads(run.effect_receipts_json)
        positive=change in {'none','policy-race','native-admit'}
        assert run.status==('succeeded' if positive else 'running')
        assert len(effects)==(1 if positive else 0)
        if positive:
            assert effects[0]['content_sha256']==digest and effects[0]['details']['verified'] is True
            assert effects[0]['details']['verification_scope']=='generated_advisory_output_only'
        with (tmp_path/'actual-proposal-readback.json').open('x') as f:
            json.dump({'boundary':__doc__,'change':change,'status':run.status,'effects':effects,
                'policy_lock_observed':lock_observed,'no_learning':True},f,indent=2)
