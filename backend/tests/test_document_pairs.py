"""Real authenticated SQLite/private binary reservation and sealing."""
import hashlib
import os
from pathlib import Path
import shutil
import json
import asyncio
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import select
from tests.test_inference_accounting import accounting_db
from config.settings import settings
from src.auth.middleware import OperatorAuthMiddleware
from src.db.models import WorkBoardInputArtifact


@pytest.mark.asyncio
@pytest.mark.parametrize("mode",["ingest","native","recovery","interruption","cancel","unsupported"])
async def test_authenticated_private_pair_reserve_stream_seal_and_exact_bind(accounting_db, monkeypatch,mode):
    from src.api import auth, goals, work_board
    from src.vault import crypto
    root, engine, factory = accounting_db
    monkeypatch.setattr(crypto, "_fernet", None)
    monkeypatch.setattr(settings, "vault_encryption_key", "")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "document-pair-isolated-test")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test,localhost,127.0.0.1")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)
    auth._reset_login_throttle_for_tests()
    app=FastAPI(); app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router,prefix="/api/auth")
    for router in (goals.router,work_board.router): app.include_router(router,prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test",headers={"origin":"http://localhost:3001"}) as client:
        assert (await client.post('/api/work-board/document-pairs',json={})).status_code==401
        assert (await client.post('/api/auth/login',json={"password":"document-pair-isolated-test"})).status_code==200
        goal=await client.post('/api/goals',json={"title":"Compare selected private invoice",
            "admission_budget":{"reviewed_grant":True,"grant_id":"document-test-reviewed",
                "max_outstanding_jobs":1,"max_attempts":2,"max_runtime_seconds":70}})
        assert goal.status_code==200,goal.text
        goal_id=goal.json()['id']
        # Deliberately larger than ordinary typed JSON: raw stream remains a
        # separate bounded path without changing generic 64 KiB input policy.
        sources={"pdf":b"%PDF-1.4\n"+b"x"*70000,"csv":b"SKU,QTY,UNIT_PRICE\nPEN-01,3,0.10\n"}
        if mode!="ingest":
            from tests.document_compare_fixtures import invoice_pdf
            sources={"pdf":invoice_pdf(),"csv":b"SKU,QTY,UNIT_PRICE\nPEN-01,2,3.75\nBOOK-02,1,12.00\nMUG-03,1,8.00\n"}
            if mode=="unsupported":sources['csv']=sources['csv'].replace(b'SKU,QTY,UNIT_PRICE',b'sku,qty,unit_price')
        body={"schema_version":1,"operation":"compare-line-totals-by-sku","goal_id":goal_id,
            "goal_revision":1,"idempotency_key":"document-pair-test","no_learning":True,
            **{slot:{"size_bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest()} for slot,raw in sources.items()}}
        reserved=await client.post('/api/work-board/document-pairs',json=body)
        assert reserved.status_code==200,reserved.text
        pair=reserved.json(); identifier=pair['artifact_id']
        async with factory() as db:
            row=await db.get(WorkBoardInputArtifact,identifier)
            assert row.metadata_digest is None and row.document_reserved_bytes==16*1024*1024
        if mode=="ingest":
            original_deadline=pair['ingest_deadline']
            bad=await client.put(f'/api/work-board/document-pairs/{identifier}/sources/pdf',
                params={"expected_revision":pair['revision']},content=sources['pdf']+b'extra',
                headers={"content-type":"application/octet-stream"})
            assert bad.status_code==409 and bad.json()['detail']['code']=='document_upload_failed_cleanup_required'
            state=await client.get(f'/api/work-board/document-pairs/{identifier}')
            assert state.json()['pair_state']=='cleanup_required'
            retry=await client.post(f'/api/work-board/document-pairs/{identifier}/retry',
                json={"expected_revision":state.json()['revision']})
            assert retry.status_code==200,retry.text
            pair=retry.json()
            assert pair['generation']==2 and pair['ingest_deadline']==original_deadline
            assert pair['quota_reserved_bytes']==16*1024*1024
            exhausted=await client.post(f'/api/work-board/document-pairs/{identifier}/retry',json={"expected_revision":pair['revision']})
            assert exhausted.status_code==409 and exhausted.json()['detail']['code']=='document_upload_retry_limit'
            # Two services reserve concurrently against the same SQLite quota;
            # only one of these additional pending rows can fit this owner.
            contenders=[{**body,"idempotency_key":f"quota-contender-{n}"} for n in range(2)]
            results=await asyncio.gather(*(client.post('/api/work-board/document-pairs',json=value) for value in contenders))
            assert sorted(result.status_code for result in results)==[200,409]
            second=next(result.json() for result in results if result.status_code==200)
            discarded=await client.post(f"/api/work-board/document-pairs/{second['artifact_id']}/discard",json={"expected_revision":second['revision']})
            assert discarded.status_code==200,discarded.text
            assert discarded.json()['quota_reserved_bytes']==0 and discarded.json()['pair_state']=='deleted'
        incomplete=await client.post(f'/api/work-board/document-pairs/{identifier}/complete',json={"expected_revision":pair['revision']})
        assert incomplete.status_code==409,incomplete.text
        for slot,raw in sources.items():
            uploaded=await client.put(f'/api/work-board/document-pairs/{identifier}/sources/{slot}',params={"expected_revision":pair['revision']},content=raw,headers={"content-type":"application/octet-stream"})
            assert uploaded.status_code==200,uploaded.text
            pair=uploaded.json()
        complete=await client.post(f'/api/work-board/document-pairs/{identifier}/complete',json={"expected_revision":pair['revision']})
        assert complete.status_code==200,complete.text
        pair=complete.json(); assert pair['pair_state']=='sealed'
        overwrite=await client.put(f'/api/work-board/document-pairs/{identifier}/sources/pdf',params={"expected_revision":pair['revision']},content=sources['pdf'],headers={"content-type":"application/octet-stream"})
        assert overwrite.status_code==409 and overwrite.json()['detail']['code']=='document_pair_slot_unavailable'
        files=list((root/'artifacts/work-board/document-pairs'/identifier).glob('*'))
        assert len(files)==2 and all(p.stat().st_mode&0o777==0o600 for p in files)
        assert all(b'%PDF-' not in p.read_bytes() and b'PEN-01' not in p.read_bytes() for p in files)
        task=await client.post('/api/work-board/tasks',json={"title":"Private invoice comparison",
            "body":"Compare only selected immutable PDF and CSV","goal_id":goal_id,"goal_revision":1,
            "capability_id":"work.document-compare.v1","input_artifact_id":identifier,
            "status":"todo","requires_review":False,"idempotency_scope":"document-test","idempotency_key":"compare-one"})
        assert task.status_code==200,task.text
        # Admission is not parser success. The deliberately unsupported PDF
        # fixture above proves ingestion/private binding independently.
        assert 'PEN-01' not in task.text
        async with factory() as db:
            row=await db.get(WorkBoardInputArtifact,identifier)
            assert row.state=='bound' and row.bound_task_id==task.json()['task']['task_id']
        if mode!="ingest":
            from src.work_board.dispatcher import WorkBoardDispatcher
            from src.workflows.job_runtime import DurableJobRepository
            from src.work_board import document_compare_native
            original_execute=document_compare_native.execute
            async def traced_execute(*args,**kwargs):
                try:return await original_execute(*args,**kwargs)
                except Exception:
                    import traceback
                    traceback.print_exc()
                    raise
            monkeypatch.setattr(document_compare_native,"execute",traced_execute)
            ready=asyncio.Event();release=asyncio.Event();stage_calls=0
            original_stage=document_compare_native.stage_current
            if mode in {"interruption","cancel"}:
                async def barrier_stage(*args,**kwargs):
                    nonlocal stage_calls
                    stage_calls+=1
                    if stage_calls==2:
                        ready.set()
                        if mode=="cancel":await release.wait()
                        else:
                            from src.work_board.repository import BoardError
                            raise BoardError("document_test_interruption","Actual parent closes before source delivery")
                    return await original_stage(*args,**kwargs)
                monkeypatch.setattr(document_compare_native,"stage_current",barrier_stage)
            if mode=="recovery":
                original_adopt=document_compare_native.adopt_output
                async def interrupted_adoption(*args,**kwargs):
                    raise RuntimeError("isolated crash seam after actual output/witness before canonical adoption")
                monkeypatch.setattr(document_compare_native,"adopt_output",interrupted_adoption)
            jobs=DurableJobRepository();dispatcher=WorkBoardDispatcher(jobs=jobs,session_provider=factory.accounting_sessions)
            monkeypatch.setattr(work_board,"dispatcher",dispatcher)
            if mode=="cancel":
                running=asyncio.create_task(dispatcher.run_pass())
                await asyncio.wait_for(ready.wait(),timeout=10)
                task_id=task.json()['task']['task_id']
                second=WorkBoardDispatcher(jobs=DurableJobRepository(),session_provider=factory.accounting_sessions)
                # Same-process service fixture with independent process-local
                # lookup. The owning instance retains its real worker task.
                second._active_worker_tasks={}
                assert not second._active_worker_tasks
                monkeypatch.setattr(work_board,"dispatcher",second)
                state=await client.get(f'/api/work-board/tasks/{task_id}/document-comparison')
                assert state.status_code==200 and not state.json()['cleanup_proven']
                from src.db.models import WorkflowRunState
                async with factory() as db:
                    run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind=='document_invoice_compare_v1'))
                    binding=document_compare_native.checkpoints(run)['document-child']
                    fake_path=document_compare_native.directory_path(identifier)/(binding['nonce']+'.witness.json')
                # Wrong generation under the correct private name cannot prove
                # quiescence while the actual parser is still awaiting source.
                forged={k:binding[k] for k in ('job_id','input_digest','nonce','supervisor_pid','parser_pid')}
                forged.update(generation=2,parser_exit=0,wait_reaped=True,reason=None)
                with fake_path.open('x') as f:json.dump(forged,f)
                fake_path.chmod(0o600)
                state=await client.get(f'/api/work-board/tasks/{task_id}/document-comparison')
                assert not state.json()['cleanup_proven'] and not state.json()['quiescence_recorded']
                fake_path.unlink()
                cancelled=await client.post(f'/api/work-board/tasks/{task_id}/actions',json={"action":"cancel","expected_revision":state.json()['task_revision']})
                assert cancelled.status_code==200,cancelled.text
                state=await client.get(f'/api/work-board/tasks/{task_id}/document-comparison')
                assert not state.json()['cleanup_proven'] and not state.json()['quiescence_recorded']
                rejected=await client.post(f'/api/work-board/tasks/{task_id}/document-comparison/reconcile',
                    json={"expected_revision":state.json()['task_revision'],"idempotency_key":"missing-witness-holds"})
                assert rejected.status_code==409
                next_pair=(await client.post('/api/work-board/document-pairs',json={**body,"idempotency_key":"capacity-next-pair"})).json()
                for slot,raw in sources.items():
                    uploaded=await client.put(f"/api/work-board/document-pairs/{next_pair['artifact_id']}/sources/{slot}",
                        params={"expected_revision":next_pair['revision']},content=raw,headers={"content-type":"application/octet-stream"})
                    assert uploaded.status_code==200,uploaded.text
                    next_pair=uploaded.json()
                sealed=await client.post(f"/api/work-board/document-pairs/{next_pair['artifact_id']}/complete",json={"expected_revision":next_pair['revision']})
                assert sealed.status_code==200,sealed.text
                next_task=await client.post('/api/work-board/tasks',json={"title":"Capacity waits for actual prior reap",
                    "goal_id":goal_id,"goal_revision":1,"capability_id":"work.document-compare.v1",
                    "input_artifact_id":next_pair['artifact_id'],"status":"todo","requires_review":False,
                    "idempotency_scope":"document-test","idempotency_key":"capacity-next-task"})
                assert next_task.status_code==200,next_task.text
                held=await second.run_pass()
                assert held['completed']==0
                async with factory() as db:
                    rows=list((await db.scalars(select(WorkflowRunState).where(WorkflowRunState.job_kind=='document_invoice_compare_v1'))).all())
                    assert len(rows)==2
                    assert sum('document-child' in document_compare_native.checkpoints(row) for row in rows)==1
                release.set();receipt=await asyncio.wait_for(running,timeout=10)
                resumed=await second.run_pass()
                assert resumed['reconciled']>=1,resumed
                completed=await client.get('/api/work-board/tasks/'+next_task.json()['task']['task_id'])
                assert completed.json()['task']['status']=='done',completed.text
                async with factory() as db:
                    rows=list((await db.scalars(select(WorkflowRunState).where(WorkflowRunState.job_kind=='document_invoice_compare_v1'))).all())
                    succeeded=next(row for row in rows if row.status=='succeeded')
                    assert succeeded.failure_reason is None
            else:receipt=await dispatcher.run_pass()
            detail=await client.get('/api/work-board/tasks/'+task.json()['task']['task_id'])
            (root/'document-native-readback.json').write_text(json.dumps({"dispatch":receipt,"detail":detail.json()},indent=2))
            if mode=="interruption":
                from src.db.models import WorkflowRunState
                task_id=task.json()['task']['task_id']
                state=await client.get(f'/api/work-board/tasks/{task_id}/document-comparison')
                assert state.status_code==200 and state.json()['retryable'],state.text
                async with factory() as db:
                    run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind=='document_invoice_compare_v1'))
                    original=(run.run_identity,run.deadline_at,run.max_attempts)
                    first_fence=run.fencing_token
                monkeypatch.setattr(document_compare_native,"stage_current",original_stage)
                second=WorkBoardDispatcher(jobs=DurableJobRepository(),session_provider=factory.accounting_sessions)
                monkeypatch.setattr(work_board,"dispatcher",second)
                request={"expected_revision":state.json()['task_revision'],"idempotency_key":"retry-known-interruption"}
                retried=await client.post(f'/api/work-board/tasks/{task_id}/document-comparison/retry',json=request)
                assert retried.status_code==200 and retried.json()['retry']['status']=='succeeded',retried.text
                replay=await client.post(f'/api/work-board/tasks/{task_id}/document-comparison/retry',json=request)
                assert replay.status_code==200 and replay.json()['retry']['replayed'],replay.text
                async with factory() as db:
                    run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind=='document_invoice_compare_v1'))
                    assert (run.run_identity,run.deadline_at,run.max_attempts)==original
                    assert run.attempt_count==2 and run.fencing_token==first_fence+1
                    assert document_compare_native.checkpoints(run)['document-child']['generation']==2
                receipt={'completed':1}
            if mode in {"cancel","unsupported"}:
                task_id=task.json()['task']['task_id']
                state=await client.get(f'/api/work-board/tasks/{task_id}/document-comparison')
                assert state.status_code==200,state.text
                assert state.json()['cleanup_proven'] and state.json()['quiescence_recorded'],state.text
                assert not state.json()['retryable'] and not state.json()['report_available']
                denied=await client.get(f'/api/work-board/tasks/{task_id}/document-output/report')
                assert denied.status_code==409
                denied_retry=await client.post(f'/api/work-board/tasks/{task_id}/document-comparison/retry',json={"expected_revision":state.json()['task_revision'],"idempotency_key":"deny-unavailable-retry"})
                assert denied_retry.status_code==409
            if mode=="recovery":
                from src.db.models import WorkflowRunState
                from src.work_board.document_compare_native import checkpoints
                task_id=task.json()['task']['task_id']
                state=await client.get(f'/api/work-board/tasks/{task_id}/document-comparison')
                assert state.status_code==200,state.text
                assert state.json()['cleanup_proven'] and not state.json()['recoverable']
                request={"expected_revision":detail.json()['task']['task_revision'],"idempotency_key":"recover-original-output"}
                assert (await client.post(f'/api/work-board/tasks/{task_id}/document-comparison/recover',json=request)).status_code==409
                async with factory() as db:
                    run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind=='document_invoice_compare_v1'))
                    original=(run.run_identity,run.fencing_token,run.attempt_count,run.deadline_at,checkpoints(run)['document-child'])
                    expiry=document_compare_native.utc(run.lease_expires_at)
                # Observe the real original 35s lease expiry, without editing
                # timestamps or extending the immutable 70s allowance.
                await asyncio.sleep(max(0,(expiry-document_compare_native.now()).total_seconds())+.05)
                monkeypatch.setattr(document_compare_native,"adopt_output",original_adopt)
                second=WorkBoardDispatcher(jobs=DurableJobRepository(),session_provider=factory.accounting_sessions)
                assert not second._active_worker_tasks
                monkeypatch.setattr(work_board,"dispatcher",second)
                recovered=await client.post(f'/api/work-board/tasks/{task_id}/document-comparison/recover',json=request)
                assert recovered.status_code==200,recovered.text
                assert recovered.json()['recovery']['status']=='succeeded'
                replay=await client.post(f'/api/work-board/tasks/{task_id}/document-comparison/recover',json=request)
                assert replay.status_code==200 and replay.json()['recovery']['replayed'],replay.text
                async with factory() as db:
                    run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind=='document_invoice_compare_v1'))
                    assert (run.run_identity,run.fencing_token,run.attempt_count,run.deadline_at,checkpoints(run)['document-child'])==original
                receipt={'completed':1}
            if mode not in {"cancel","unsupported"}:
                assert receipt['completed']==1,detail.json()
            report=await client.get('/api/work-board/tasks/'+task.json()['task']['task_id']+'/document-output/report')
            if mode in {"cancel","unsupported"}:
                assert report.status_code==409
            else:
                assert report.status_code==200,report.text
            if mode in {"cancel","unsupported"}:
                evidence=os.environ.get('SERAPH_DOCUMENT_TEST_EVIDENCE')
                if evidence:
                    destination=Path(evidence)/mode;destination.mkdir(parents=True,exist_ok=False,mode=0o700)
                    shutil.copytree(root,destination/'workspace')
                    for file in destination.rglob('*'):
                        if file.is_file():file.chmod(0o600)
                return
            assert 'difference 0.50' in report.json()['text']
            csv_output=await client.get('/api/work-board/tasks/'+task.json()['task']['task_id']+'/document-output/csv')
            assert csv_output.status_code==200,csv_output.text
            assert 'MUG-03,csv_only,,,,' in csv_output.json()['text']
            state=await client.get('/api/work-board/tasks/'+task.json()['task']['task_id']+'/document-comparison')
            assert state.status_code==200,state.text
            assert state.json()['cleanup_proven'] and state.json()['report_available']
            assert not state.json()['recoverable'] and state.json()['no_learning']
        evidence=os.environ.get('SERAPH_DOCUMENT_TEST_EVIDENCE')
        if evidence:
            destination=Path(evidence)/mode; destination.mkdir(parents=True,exist_ok=False,mode=0o700)
            shutil.copytree(root,destination/'workspace')
            for file in destination.rglob('*'):
                if file.is_file(): file.chmod(0o600)
