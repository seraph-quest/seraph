"""Pinned old device readers cannot revive exact legacy transport secrets."""
import ast,json,os,subprocess,sys
from datetime import datetime,timedelta,timezone
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from cryptography.fernet import Fernet,InvalidToken
from sqlalchemy.ext.asyncio import create_async_engine,AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel
from src.db.models import Secret,TelegramTransportState
from src.db.engine import _ensure_operator_session_columns,_ensure_operator_principals,_ensure_vault_owner
from src.extensions import paired_edge,telegram_transport
from src.vault import repository,crypto
from tests.test_operator_principal_migration import OLD_SCHEMA
from tests.test_workspace_production import _workspace,_env,ROOT,CLI

@pytest.mark.asyncio
@pytest.mark.parametrize('transition',['migration','managed_restore'])
async def test_old_device_auth_rejects_revoked_ciphertext_and_unrelated_keys_survive(tmp_path,monkeypatch,transition):
    root=_workspace(tmp_path)
    monkeypatch.setattr(crypto,'_fernet',Fernet(Fernet.generate_key()))
    now=datetime.now(timezone.utc)
    engine=create_async_engine('sqlite+aiosqlite:///'+str(root/'seraph.db'))
    async with engine.begin() as db:
        await db.exec_driver_sql(OLD_SCHEMA)
        await db.exec_driver_sql("INSERT INTO operator_sessions VALUES ('legacy-root','hash',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'2099-01-01','2099-01-01',NULL,NULL,0)")
        await db.run_sync(SQLModel.metadata.create_all)
    factory=sessionmaker(engine,class_=AsyncSession,expire_on_commit=False)
    @asynccontextmanager
    async def sessions():
        async with factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise
    ns=dict(vars(repository),get_session=sessions)
    async def quiet(*args,**kwargs): pass
    ns['_log_vault_event']=quiet
    source=subprocess.check_output(['git','show','2e4f71bb:backend/src/vault/repository.py'],cwd=ROOT,text=True)
    cls=next(node for node in ast.parse(source).body if isinstance(node,ast.ClassDef) and node.name=='VaultRepository')
    old_get=next(node for node in cls.body if isinstance(node,ast.AsyncFunctionDef) and node.name=='get')
    exec(compile(ast.Module(body=[old_get],type_ignores=[]),'pre900_vault.py','exec'),ns)
    async def get_old(key): return await ns['get'](None,key)
    old_vault=SimpleNamespace(get=get_old)
    edge_ns=dict(vars(paired_edge),vault_repository=old_vault)
    source=subprocess.check_output(['git','show','2e4f71bb:backend/src/extensions/paired_edge.py'],cwd=ROOT,text=True)
    fn=next(node for node in ast.parse(source).body if isinstance(node,ast.AsyncFunctionDef) and node.name=='authenticate_edge_request')
    exec(compile(ast.Module(body=[fn],type_ignores=[]),'pre900_edge.py','exec'),edge_ns)
    telegram_ns=dict(vars(telegram_transport),vault_repository=old_vault)
    source=subprocess.check_output(['git','show','2e4f71bb:backend/src/extensions/telegram_transport.py'],cwd=ROOT,text=True)
    cls=next(node for node in ast.parse(source).body if isinstance(node,ast.ClassDef) and node.name=='TelegramTransportAdapter')
    exec(compile(ast.Module(body=[cls],type_ignores=[]),'pre900_telegram.py','exec'),telegram_ns)
    edge_token='private-old-edge-token'
    extension,reference,pairing,device='test-edge','nodes/device.yaml','pairing-1','device-1'
    scope=paired_edge.canonical_edge_scope(device_id=device,pairing_id=pairing,capability_scope='media.ingest',data_purpose='screen_capture')
    key=paired_edge._credential_key(extension,reference,pairing,edge_token)
    from src.extensions.node_pairing import NodePairingState,PairingLifecycleState,scoped_credential_fingerprint
    import hashlib
    state=NodePairingState(device_id=device,pairing_id=pairing,lifecycle=PairingLifecycleState.PAIRED,
        credential_fingerprint=scoped_credential_fingerprint(edge_token,scope),credential_scope_digest=hashlib.sha256(scope.encode()).hexdigest())
    entry=paired_edge.pairing_entry_from_state(state,base_entry={'name':'edge','reference':reference},
        credential_ref=paired_edge.PAIRING_CREDENTIAL_PREFIX+hashlib.sha256(key.encode()).hexdigest()[:24],credential_scope=scope,owner_principal_id='operator:single')
    payload={'revision':1,'extensions':{extension:{'node_pairings':{reference:entry}}}}
    telegram_key='telegram.transport.token:'+'a'*64
    async with sessions() as db:
        for identifier,secret_key in [('edge-original',key),('telegram-original',telegram_key),('provider-original','openrouter_api_key'),('near-match','seraph-node-pairing-not-a-credential')]:
            db.add(Secret(id=identifier,key=secret_key,encrypted_value=crypto.encrypt(edge_token),description='metadata retained'))
        db.add(TelegramTransportState(owner_principal_id='operator:single',operator_session_id='legacy-root',pairing_state='active',
            pairing_id='telegram-1',operator_id=42,chat_id=77,token_secret_ref=telegram_key,pairing_expires_at=now+timedelta(hours=1),
            transit_consent_reference='consent',transit_consent_expires_at=now+timedelta(minutes=10)))
    transport=telegram_transport.RecordingTelegramTransport()
    adapter=telegram_ns['TelegramTransportAdapter'](transport=transport)
    async def edge_read():
        return await edge_ns['authenticate_edge_request'](payload,extension_id=extension,reference=reference,name='edge',device_id=device,pairing_id=pairing,
            request_id='request-1',sequence=1,captured_at=now,content_hash='a'*64,media_type='application/json',content_size=0,
            capability_scope='media.ingest',data_purpose='screen_capture',policy_version=paired_edge.DEFAULT_POLICY_VERSION,presented_credential=edge_token)
    with patch('src.db.engine.get_session',sessions):
        assert (await edge_read()).owner_principal_id=='operator:single'
        await adapter.poll_updates(owner_principal_id='operator:single',operator_session_id='legacy-root')
        if transition=='migration':
            async with engine.begin() as db:
                await db.exec_driver_sql('BEGIN IMMEDIATE')
                await _ensure_operator_session_columns(db)
                await _ensure_vault_owner(db)
                await _ensure_operator_principals(db)
        else:
            env={**os.environ,**_env(root),'PYTHONPATH':str(ROOT/'backend')}
            def managed(*args):
                result=subprocess.run([sys.executable,str(CLI),'--base-dir',str(tmp_path),*args],env=env,cwd=ROOT,capture_output=True,text=True)
                assert result.returncode==0,result.stdout+result.stderr
                return json.loads(result.stdout)
            archive=managed('backup','--archive','old-devices.zip')['archive_path']
            await engine.dispose()
            managed('restore','--archive',archive,'--confirm')
            engine=create_async_engine('sqlite+aiosqlite:///'+str(root/'seraph.db'))
            factory=sessionmaker(engine,class_=AsyncSession,expire_on_commit=False)
        with pytest.raises(InvalidToken): await edge_read()
        with pytest.raises(InvalidToken): await adapter.poll_updates(owner_principal_id='operator:single',operator_session_id='legacy-root')
        assert await get_old('openrouter_api_key')==edge_token
        assert await get_old('seraph-node-pairing-not-a-credential')==edge_token
        async with sessions() as db:
            for identifier in ('edge-original','telegram-original'):
                original=await db.get(Secret,identifier)
                assert original.description=='metadata retained' and original.revoked_at is not None
    await engine.dispose()
