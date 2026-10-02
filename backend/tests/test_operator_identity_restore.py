"""Actual managed backup/restore/rollback must not resurrect private proof."""
import json, os, subprocess, sys
from pathlib import Path
from contextlib import asynccontextmanager
from unittest.mock import patch
import pytest
from httpx import AsyncClient,ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine,AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel
from src.db.models import Goal,OperatorSession,OperatorIdentity,OperatorContinuityCredential
from src.api.auth import _continuity_cookie_name
from tests.test_operator_identity import auth, login, enroll, HEADERS,PASSWORD
from tests.test_workspace_production import _workspace,_env,ROOT,CLI

@pytest.mark.asyncio
async def test_snapshot_before_proof_consumption_and_revoke_never_replays_cookie_or_code(client,app,tmp_path):
    root=_workspace(tmp_path)
    engine=create_async_engine('sqlite+aiosqlite:///'+str(root/'seraph.db'))
    async with engine.begin() as db: await db.run_sync(SQLModel.metadata.create_all)
    factory=sessionmaker(engine,class_=AsyncSession,expire_on_commit=False)
    @asynccontextmanager
    async def persisted():
        async with factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise
    env={**os.environ,**_env(root),'PYTHONPATH':str(ROOT/'backend')}
    def managed(*args):
        result=subprocess.run([sys.executable,str(CLI),'--base-dir',str(tmp_path),*args],env=env,cwd=ROOT,capture_output=True,text=True)
        assert result.returncode==0,result.stdout+result.stderr
        return json.loads(result.stdout)
    with patch('src.db.engine.get_session',persisted),patch('src.auth.service.get_session',persisted):
        first=await login(client)
        proof=await enroll(client)
        cookie=client.cookies.get(_continuity_cookie_name())
        async with persisted() as db:
            db.add(Goal(id='restore-original',title='Immutable original',owner_principal_id=first['principal_id'],owner_session_id=first['session_id']))
        archive=managed('backup','--archive','private-proof.zip')['archive_path']
        await login(client)  # consumes/rotates the pre-backup cookie proof
        assert (await client.post('/api/auth/ownership/revoke',headers=HEADERS)).status_code==204
        await engine.dispose()
        restored=managed('restore','--archive',archive,'--confirm')
        assert restored['stage_receipt']['restore_reconciliation']['authority_invalidation']['continuity_credentials_invalidated']==2
        engine=create_async_engine('sqlite+aiosqlite:///'+str(root/'seraph.db'))
        factory=sessionmaker(engine,class_=AsyncSession,expire_on_commit=False)
        async def denied_proofs():
            from src.api.auth import _reset_login_throttle_for_tests
            _reset_login_throttle_for_tests()
            async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as device:
                device.cookies.set(_continuity_cookie_name(),cookie,domain='test.local',path='/')
                response=await device.post('/api/auth/login',headers=HEADERS,json={'password':PASSWORD})
                assert response.status_code==401 and response.json()['detail']['code']=='ownership_proof_invalid'
                response=await device.post('/api/auth/login',headers=HEADERS,json={'password':PASSWORD,'recovery_code':proof['recovery_code']})
                assert response.status_code==401
            async with persisted() as db:
                original=await db.get(Goal,'restore-original')
                assert original.owner_principal_id==first['principal_id'] and original.owner_session_id==first['session_id']
                assert await db.get(OperatorIdentity,proof['operator_identity_id']) is not None
        await denied_proofs()
        await engine.dispose()
        rolled=managed('rollback','--restore-id',restored['restore_id'],'--confirm')
        assert rolled['status']=='rolled_back'
        engine=create_async_engine('sqlite+aiosqlite:///'+str(root/'seraph.db'))
        factory=sessionmaker(engine,class_=AsyncSession,expire_on_commit=False)
        await denied_proofs()
    await engine.dispose()
