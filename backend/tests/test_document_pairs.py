"""Real authenticated SQLite/private binary reservation and sealing."""
import hashlib
import os
from pathlib import Path
import shutil
import json
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
@pytest.mark.parametrize("mode",["ingest","native"])
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
        if mode=="native":
            from tests.document_compare_fixtures import invoice_pdf
            sources={"pdf":invoice_pdf(),"csv":b"SKU,QTY,UNIT_PRICE\nPEN-01,2,3.75\nBOOK-02,1,12.00\nMUG-03,1,8.00\n"}
        body={"schema_version":1,"operation":"compare-line-totals-by-sku","goal_id":goal_id,
            "goal_revision":1,"idempotency_key":"document-pair-test","no_learning":True,
            **{slot:{"size_bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest()} for slot,raw in sources.items()}}
        reserved=await client.post('/api/work-board/document-pairs',json=body)
        assert reserved.status_code==200,reserved.text
        pair=reserved.json(); identifier=pair['artifact_id']
        async with factory() as db:
            row=await db.get(WorkBoardInputArtifact,identifier)
            assert row.metadata_digest is None and row.document_reserved_bytes==16*1024*1024
        incomplete=await client.post(f'/api/work-board/document-pairs/{identifier}/complete',json={"expected_revision":pair['revision']})
        assert incomplete.status_code==409,incomplete.text
        for slot,raw in sources.items():
            uploaded=await client.put(f'/api/work-board/document-pairs/{identifier}/sources/{slot}',params={"expected_revision":pair['revision']},content=raw,headers={"content-type":"application/octet-stream"})
            assert uploaded.status_code==200,uploaded.text
            pair=uploaded.json()
        complete=await client.post(f'/api/work-board/document-pairs/{identifier}/complete',json={"expected_revision":pair['revision']})
        assert complete.status_code==200,complete.text
        pair=complete.json(); assert pair['pair_state']=='sealed'
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
        if mode=="native":
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
            jobs=DurableJobRepository();dispatcher=WorkBoardDispatcher(jobs=jobs,session_provider=factory.accounting_sessions)
            monkeypatch.setattr(work_board,"dispatcher",dispatcher)
            receipt=await dispatcher.run_pass()
            detail=await client.get('/api/work-board/tasks/'+task.json()['task']['task_id'])
            (root/'document-native-readback.json').write_text(json.dumps({"dispatch":receipt,"detail":detail.json()},indent=2))
            assert receipt['completed']==1,detail.json()
            report=await client.get('/api/work-board/tasks/'+task.json()['task']['task_id']+'/document-output/report')
            assert report.status_code==200,report.text
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
