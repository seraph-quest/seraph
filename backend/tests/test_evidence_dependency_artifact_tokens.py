"""Canonical token fixtures and real deterministic CPU/file bytes; no execution proof."""
from datetime import datetime,timezone
import json,os

import pytest

from config.settings import settings
from src.artifacts.registry import artifact_id_for
from src.db.models import WorkBoardAttempt,WorkBoardTask,WorkflowRunState
from src.memory.evidence_dependencies import canonical_source_token,digest,recheck_staged,stage_packet
from src.memory.evidence_working_set import _sources
from src.work_board.repository import BoardError,_begin_sqlite_immediate
from src.workflows.job_runtime import _digest
from tests.test_evidence_dependency_tokens import canonical_fact
from tests.test_artifact_pipelines import consumer
from src.work_board.pipeline_contracts import canonical_bytes,DOSSIER,REPORT
from src.work_board.pipeline_cpu import output_bytes


async def canonical_chain(db,owner,task,tmp_path):
    identities={}
    raw=canonical_bytes({'schema_version':1,'capability_id':'browser.public-task.v1',
        'task_id':'browser','attempt_id':'browser-attempt','final_url':'https://example.com/',
        'extracts':[{'kind':'extract','value':'Public exact evidence'}],'checks':[],'request_count':1})
    previous=None
    for name,kind,capability in [('browser','browser_public_task_result','browser.public-task.v1'),
                                ('dossier','evidence_dossier',DOSSIER),('report','evidence_local_report',REPORT)]:
        arguments={}
        if previous is not None:
            inputs=consumer(raw,schema='browser_public_task_result' if name=='dossier' else 'evidence_dossier.v1',
                            task=previous,attempt=previous+'-attempt')
            arguments={'input':inputs,'parent_handoff_context':[],'parent_handoff_digest':_digest([])}
            raw=output_bytes(capability,inputs)
        source=WorkBoardTask(task_id=name,goal_id=task.goal_id,goal_revision=task.goal_revision,
            owner_principal_id=owner.principal_id,owner_session_id=owner.session_id,
            capability_id=capability,idempotency_key=name,typed_input_digest='a'*64,
            input_artifact_id='browser-input' if name=='browser' else None)
        attempt=WorkBoardAttempt(attempt_id=name+'-attempt',task_id=name,workflow_run_id=name+'-run',
                                 fencing_token=1,ended_at=datetime.now(timezone.utc),outcome='verified')
        authority={}
        if name=='browser':
            authority={'principal':'service:browser-task','owner_kind':'service','service_id':'service:browser-task',
                'capability_id':capability,'goal_owner_principal_id':owner.principal_id,'goal_owner_session_id':owner.session_id,
                'operator_owner_principal_id':owner.principal_id,'operator_owner_session_id':owner.session_id,
                'goal_id':task.goal_id,'goal_revision':task.goal_revision,'board_fencing_token':1,
                'board_task_revision':2,'input_artifact_id':'browser-input','input_artifact_digest':'a'*64}
        path='artifacts/work-board/browser/'+name+'.json' if name=='browser' else 'artifacts/work-board/evidence/'+name+'.json'
        full=tmp_path/path;full.parent.mkdir(parents=True,exist_ok=True);full.write_bytes(raw);full.chmod(0o600)
        for parent in full.parents:
            if parent==tmp_path.parent:break
            parent.chmod(0o700)
        content=digest(raw)
        job_kind='browser_public_task' if name=='browser' else capability
        artifact=artifact_id_for(file_path=path,artifact_type=kind,producer=job_kind,run_id=name+'-run',content_sha256=content)
        receipt={'artifact_id':artifact,'artifact_type':kind,'producer':job_kind,'file_path':path,
                 'content_sha256':content,'exists':True}
        effect={'receipt_kind':'readback','status':'succeeded','target_path':path,'target_digest':content,
                'content_sha256':content,'details':{'verified':True}}
        run=WorkflowRunState(run_identity=name+'-run',root_run_identity=name+'-run',workflow_name=job_kind,
            job_kind=job_kind,owner_kind='service' if name=='browser' else 'user',
            owner_principal_id='service:browser-task' if name=='browser' else owner.principal_id,
            service_id='service:browser-task' if name=='browser' else None,
            operator_session_id=owner.session_id,goal_id=task.goal_id,goal_revision=task.goal_revision,status='succeeded',
            idempotency_scope='work-board-attempt',idempotency_key=name+':'+name+'-attempt',
            declared_authority_json=json.dumps(authority),authority_digest=_digest(authority),
            arguments_json=json.dumps(arguments),input_digest=_digest(arguments),
            artifact_receipts_json=json.dumps([receipt]),effect_receipts_json=json.dumps([effect]))
        db.add(source);await db.flush();db.add_all([attempt,run]);await db.flush()
        identities[kind]=(artifact,{'task_id':name,'attempt_id':name+'-attempt','run_id':name+'-run'})
        previous=name
    await db.commit()
    return identities


@pytest.mark.asyncio
@pytest.mark.parametrize('async_db',['file'],indirect=True)
@pytest.mark.parametrize('kind,capability',[('browser_public_task_result','browser.public-task.v1'),
    ('evidence_dossier',DOSSIER),('evidence_local_report',REPORT)])
async def test_three_exact_artifact_pairs_resolve_canonical_chain(async_db,monkeypatch,tmp_path,kind,capability):
    monkeypatch.setattr(settings,'workspace_dir',str(tmp_path))
    monkeypatch.setattr(settings,'browser_site_allowlist','example.com')
    monkeypatch.setattr(settings,'browser_site_blocklist','')
    async with async_db() as db:
        owner,task,_memory,_source=await canonical_fact(db)
        task.capability_id=capability;await db.commit()
        identities=await canonical_chain(db,owner,task,tmp_path)
        artifact,lineage=identities[kind]
        token=await canonical_source_token(db,owner,task,kind,artifact,lineage)
        assert token['producer']==capability
        assert token['read_policy_digest']
        if kind!='browser_public_task_result':assert token['upstream_permission']['url_digest']==digest('https://example.com/')


@pytest.mark.asyncio
@pytest.mark.parametrize('async_db',['file'],indirect=True)
@pytest.mark.parametrize('kind,capability',[('evidence_dossier',DOSSIER),('evidence_local_report',REPORT)])
async def test_derived_cpu_staged_permission_change_blocks_same_writer(async_db,monkeypatch,tmp_path,kind,capability):
    monkeypatch.setattr(settings,'workspace_dir',str(tmp_path))
    monkeypatch.setattr(settings,'browser_site_allowlist','example.com')
    monkeypatch.setattr(settings,'browser_site_blocklist','')
    async with async_db() as db:
        owner,task,_memory,_source=await canonical_fact(db)
        task.capability_id=capability;await db.commit()
        await canonical_chain(db,owner,task,tmp_path)
        sources,_blocked=await _sources(db,owner,task)
        source=next(item for item in sources if item['source_kind']==kind)
        line=source['text'].splitlines()[0]
        packet={'revision':1,'digest':'b'*64,'citations':[{'source_id':source['source_id'],
            'source_digest':source['source_digest'],'version':source['version'],
            'line_start':1,'line_end':1,'span_digest':digest(line.encode())}]}
        staged=await stage_packet(db,owner,task,packet)
        await db.commit()
        monkeypatch.setattr(settings,'browser_site_blocklist','example.com')
        await _begin_sqlite_immediate(db)
        def forbidden(*args,**kwargs):raise AssertionError('physical read or network inside writer')
        monkeypatch.setattr('src.memory.evidence_working_set._read_file',forbidden)
        monkeypatch.setattr('socket.getaddrinfo',forbidden)
        with pytest.raises(BoardError) as error:await recheck_staged(db,owner,task,staged)
        assert error.value.code=='evidence_dependency_stale'
