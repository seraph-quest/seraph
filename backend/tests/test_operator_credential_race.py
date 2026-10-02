"""One-time proof claim/rotation race on independent file SQLite connections."""
import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlmodel import SQLModel
from httpx import AsyncClient, ASGITransport
import pytest
from src.db.models import OperatorSession,OperatorContinuityCredential
from src.auth import ownership
from tests.test_operator_identity import auth, login, enroll, HEADERS, PASSWORD

@pytest.mark.asyncio
async def test_recovery_code_failure_rollback_and_concurrent_single_consumer(client, app, tmp_path):
    engine=create_async_engine('sqlite+aiosqlite:///'+str(tmp_path/'proof.db'))
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
    with patch('src.db.engine.get_session',persisted),patch('src.auth.service.get_session',persisted):
        first=await login(client)
        enrolled=await enroll(client)
        code=enrolled['recovery_code']
        credential=ownership._credential
        def fail_after_claim(identity,kind):
            raise OperationalError('injected',{},Exception('injected'))
        with patch('src.auth.ownership._credential',fail_after_claim):
            async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as recovery:
                response=await recovery.post('/api/auth/login',headers=HEADERS,json={'password':PASSWORD,'recovery_code':code})
                assert response.status_code==503
        async with persisted() as db:
            proofs=(await db.execute(select(OperatorContinuityCredential).where(OperatorContinuityCredential.kind=='recovery'))).scalars().all()
            assert len(proofs)==1 and proofs[0].revoked_at is None
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as left, AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as right:
            results=await asyncio.gather(*[device.post('/api/auth/login',headers=HEADERS,json={'password':PASSWORD,'recovery_code':code}) for device in (left,right)])
            assert sorted(response.status_code for response in results)==[200,401]
            async with persisted() as db:
                sessions=(await db.execute(select(OperatorSession))).scalars().all()
                assert len(sessions)==2 and len({row.principal_id for row in sessions})==2
                proof=(await db.execute(select(OperatorContinuityCredential).where(OperatorContinuityCredential.kind=='recovery'))).scalar_one()
                assert proof.revoked_at is not None
                assert all(row.operator_identity_id==enrolled['operator_identity_id'] for row in sessions)
    await engine.dispose()
