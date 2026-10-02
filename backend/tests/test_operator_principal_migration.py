"""Forward-only file SQLite migration, rollback and older issuer denial."""
import ast
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import subprocess
from pathlib import Path
import uuid
import pytest
from sqlalchemy import Column, String, DateTime, Boolean, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from src.db.engine import _ensure_operator_session_columns, _ensure_operator_principals
from src.db.models import OperatorSession
from src.auth import service

OLD_SCHEMA = '''CREATE TABLE operator_sessions (id VARCHAR PRIMARY KEY, token_hash VARCHAR UNIQUE NOT NULL,
created_at DATETIME NOT NULL, last_seen_at DATETIME NOT NULL, idle_expires_at DATETIME NOT NULL,
absolute_expires_at DATETIME NOT NULL, revoked_at DATETIME, replaced_by_id VARCHAR, is_bearer_tombstone BOOLEAN NOT NULL DEFAULT 0)'''

@pytest.mark.asyncio
async def test_existing_principals_migration_reopen_and_actual_older_auth_fail_closed(tmp_path, monkeypatch):
    path = tmp_path/'seraph.db'
    engine = create_async_engine('sqlite+aiosqlite:///'+str(path))
    now = datetime.now(timezone.utc)
    async with engine.begin() as db:
        await db.exec_driver_sql(OLD_SCHEMA)
        for identifier, replaced, tombstone in [('root-original', None, False), ('root-legacy', 'root-original', False), ('bearer-tombstone', 'root-original', True)]:
            await db.execute(text('INSERT INTO operator_sessions VALUES (:id,:hash,:now,:now,:end,:end,NULL,:replaced,:tombstone)'),
                {'id':identifier,'hash':service._token_hash(identifier),'now':now,'end':now+timedelta(hours=1),'replaced':replaced,'tombstone':tombstone})
        await _ensure_operator_session_columns(db)
        await _ensure_operator_principals(db)
        await _ensure_operator_principals(db)
    await engine.dispose()
    engine = create_async_engine('sqlite+aiosqlite:///'+str(path))
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    @asynccontextmanager
    async def sessions():
        async with factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise
    monkeypatch.setattr(service, 'get_session', sessions)
    monkeypatch.setattr(service.settings, 'operator_auth_secret', 'migration-test')
    Base = declarative_base()
    class LegacySession(Base):
        __tablename__ = 'operator_sessions'
        id = Column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
        token_hash = Column(String)
        created_at = Column(DateTime, default=lambda: now)
        last_seen_at = Column(DateTime, default=lambda: now)
        idle_expires_at = Column(DateTime)
        absolute_expires_at = Column(DateTime)
        revoked_at = Column(DateTime)
        replaced_by_id = Column(String)
        is_bearer_tombstone = Column(Boolean, default=False)
    # Execute the actual pinned pre-#900 auth functions against a mapper with
    # their original columns. No facade writes a new principal on their behalf.
    old_source = subprocess.check_output(['git','show','2e4f71bb:backend/src/auth/service.py'], cwd=Path(__file__).resolve().parents[2], text=True)
    namespace = dict(vars(service), OperatorSession=LegacySession, get_session=sessions)
    names = {'create_session','authenticate_token','_find_token_record','_operator_for_record','_principal'}
    for node in ast.parse(old_source).body:
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name in names:
            exec(compile(ast.Module(body=[node], type_ignores=[]), 'pre900_auth.py', 'exec'), namespace)
    with pytest.raises(service.AuthFailure, match='session_revoked'):
        await namespace['authenticate_token']('root-original', touch=False)
    with pytest.raises(IntegrityError, match='operator_principal_migration_required'):
        await namespace['create_session']()
    token, current = await service.create_session()
    assert current.principal.principal_id.startswith('operator:root:')
    async with sessions() as db:
        rows = (await db.execute(select(OperatorSession))).scalars().all()
        assert len(rows) == 4 and len({row.principal_id for row in rows}) == 4
        old = await db.get(OperatorSession, 'root-original')
        assert old.revoked_at is not None and old.operator_identity_id is None
        assert old.legacy_owner_principal_id == 'operator:single'
        assert (await db.get(OperatorSession,'bearer-tombstone')).legacy_owner_principal_id is None
    guard = Path(__file__).resolve().parents[2]/'scripts/operator_schema_guard.py'
    refusal = subprocess.run(['python3',str(guard),str(path),'--runtime-schema','0'],capture_output=True,text=True)
    assert refusal.returncode == 1 and 'restore' in refusal.stderr
    assert subprocess.run(['python3',str(guard),str(path)],capture_output=True).returncode == 0
    assert 'operator_schema_guard.py' in (guard.parent.parent/'manage.sh').read_text()
    await engine.dispose()

@pytest.mark.asyncio
async def test_migration_failure_rolls_back_schema_and_bearer_revocation(tmp_path):
    engine = create_async_engine('sqlite+aiosqlite:///'+str(tmp_path/'seraph.db'))
    now = datetime.now(timezone.utc)
    async with engine.begin() as db:
        await db.exec_driver_sql(OLD_SCHEMA)
        await db.execute(text('INSERT INTO operator_sessions VALUES (:id,:hash,:now,:now,:end,:end,NULL,NULL,0)'),
            {'id':'migration-owner','hash':service._token_hash('migration-owner'),'now':now,'end':now+timedelta(hours=1)})
    with pytest.raises(RuntimeError, match='injected'):
        async with engine.begin() as db:
            await db.exec_driver_sql('BEGIN IMMEDIATE')
            await _ensure_operator_session_columns(db)
            await _ensure_operator_principals(db)
            raise RuntimeError('injected migration interruption')
    async with engine.begin() as db:
        cols = {row[1] for row in (await db.exec_driver_sql('PRAGMA table_info(operator_sessions)')).fetchall()}
        assert 'principal_id' not in cols
        assert (await db.exec_driver_sql('SELECT revoked_at FROM operator_sessions')).scalar_one() is None
        await db.exec_driver_sql('BEGIN IMMEDIATE')
        await _ensure_operator_session_columns(db)
        await _ensure_operator_principals(db)
    async with engine.begin() as db:
        assert (await db.exec_driver_sql('SELECT revoked_at FROM operator_sessions')).scalar_one() is not None
    await engine.dispose()

@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['operator:single', 'operator:root:bad???owner', None])
async def test_malformed_persisted_principal_blocks_startup(tmp_path, bad):
    engine = create_async_engine('sqlite+aiosqlite:///'+str(tmp_path/'seraph.db'))
    async with engine.begin() as db:
        await db.exec_driver_sql(OLD_SCHEMA)
        await db.exec_driver_sql('ALTER TABLE operator_sessions ADD COLUMN principal_id VARCHAR')
        await db.exec_driver_sql('ALTER TABLE operator_sessions ADD COLUMN legacy_owner_principal_id VARCHAR')
        await db.execute(text('INSERT INTO operator_sessions (id,token_hash,created_at,last_seen_at,idle_expires_at,absolute_expires_at,principal_id) VALUES (:id,:hash,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,:bad)'), {'id':'malformed-owner','hash':'hash','bad':bad})
    with pytest.raises(RuntimeError, match='malformed'):
        async with engine.begin() as db: await _ensure_operator_principals(db)
    await engine.dispose()
