"""#900 cross-root and exact historical citation authority proof."""
import asyncio
from datetime import datetime,timedelta,timezone
import pytest
from httpx import AsyncClient, ASGITransport
from config.settings import settings
from src.db.models import Session, Message, WorkBoardTask
from src.auth.service import authenticate_token, AuthFailure, test_bypass_operator as bypass_operator
from src.auth.ownership import fresh_work_source_scope, enroll as enroll_service
from tests.test_operator_identity import auth, login, enroll, recover, goal, HEADERS

@pytest.mark.asyncio
async def test_conversations_and_legacy_ownerless_ingress_are_not_caller_claimable(client, async_db, app):
    first = await login(client)
    from src.agent.session import session_manager, SessionOwnerMismatchError
    current = await session_manager.get_or_create(owner_principal_id=first['principal_id'])
    async with async_db() as db:
        db.add(Session(id='guess-unowned', owner_principal_id=None, title='PRIVATE legacy'))
        db.add(Session(id='guess-shared-role', owner_principal_id='operator:single', title='PRIVATE ambiguous'))
        await db.flush()
        db.add(Message(session_id='guess-unowned', role='user', content='PRIVATE linked'))
    for identifier in ('guess-unowned', 'guess-shared-role'):
        for path in (f'/api/sessions/{identifier}/messages', f'/api/sessions/{identifier}/todos'):
            result = await client.get(path)
            assert result.status_code == 403 and 'PRIVATE' not in result.text
        for method, path, body in [('patch', f'/api/sessions/{identifier}', {'title': 'stolen'}),
            ('patch', f'/api/sessions/{identifier}', {'title': 'stolen again'}), ('delete', f'/api/sessions/{identifier}', None)]:
            result = await getattr(client, method)(path, headers=HEADERS, **({'json': body} if body is not None else {}))
            assert result.status_code == 403
        with pytest.raises(SessionOwnerMismatchError):
            await session_manager.get_for_ingress(identifier, owner_principal_id=first['principal_id'])
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as independent:
        second = await login(independent)
        assert second['principal_id'] != first['principal_id']
        assert (await independent.get('/api/sessions')).json() == []
        assert (await independent.get(f'/api/sessions/{current.id}/messages')).status_code == 403
    async with async_db() as db:
        assert (await db.get(Session, 'guess-unowned')).owner_principal_id is None
        assert (await db.get(Session, 'guess-shared-role')).owner_principal_id == 'operator:single'

@pytest.mark.asyncio
async def test_exact_fresh_goal_lineage_does_not_adopt_other_selection(client, async_db):
    await login(client)
    await enroll(client)
    a, b = await goal(client), await goal(client)
    tasks = []
    for label, old_goal in [('A', a), ('B', b)]:
        result = await client.post('/api/work-board/tasks', headers=HEADERS, json={
            'title': label, 'goal_id': old_goal['id'], 'goal_revision': 1, 'idempotency_key': label})
        assert result.status_code == 200, result.text
        tasks.append(result.json()['task'])
    await client.post('/api/auth/logout', headers=HEADERS)
    await login(client)
    journal, _ = await recover(client, [{'kind': 'goal', 'record_id': row['id']} for row in (a, b)] +
        [{'kind': 'task', 'record_id': row['task_id']} for row in tasks])
    result = await client.post(f"/api/auth/ownership/recovery/{journal['journal_id']}/fresh-work", headers=HEADERS)
    assert result.status_code == 200, result.text
    fresh_a = next(item['task_id'] for item in result.json()['fresh_work']['tasks'] if item['source_task_id'] == tasks[0]['task_id'])
    operator = await authenticate_token(client.cookies.get(settings.operator_auth_cookie_name), touch=False)
    assert await fresh_work_source_scope(operator, fresh_a, 'goal', a['id']) is not None
    assert await fresh_work_source_scope(operator, fresh_a, 'task', tasks[0]['task_id']) is not None
    assert await fresh_work_source_scope(operator, fresh_a, 'goal', b['id']) is None
    assert await fresh_work_source_scope(operator, fresh_a, 'task', tasks[1]['task_id']) is None
    async with async_db() as db:
        from sqlalchemy import select
        task = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == fresh_a))).scalar_one()
        task.goal_id = 'unrelated-fresh-goal'
    assert await fresh_work_source_scope(operator, fresh_a, 'goal', a['id']) is None

@pytest.mark.asyncio
async def test_vault_current_owner_only_and_ambiguous_system_keys_remain_private(client, app, monkeypatch):
    first = await login(client)
    monkeypatch.setattr('src.vault.repository.encrypt', lambda value: value)
    monkeypatch.setattr('src.vault.repository.decrypt', lambda value: value)
    from src.vault.repository import vault_repository
    await vault_repository.store('root-private', 'PRIVATE', owner_principal_id=first['principal_id'])
    await vault_repository.store('legacy-system-private', 'PRIVATE')
    assert [item['key'] for item in (await client.get('/api/vault/keys')).json()] == ['root-private']
    assert (await client.delete('/api/vault/keys/legacy-system-private', headers=HEADERS)).status_code == 404
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as independent:
        outsider = await login(independent)
        assert (await independent.get('/api/vault/keys')).json() == []
        assert (await independent.delete('/api/vault/keys/root-private', headers=HEADERS)).status_code == 404
        assert await vault_repository.get('root-private', owner_principal_id=outsider['principal_id']) is None
        with pytest.raises(PermissionError):
            await vault_repository.store('root-private', 'stolen', owner_principal_id=outsider['principal_id'])
    assert await vault_repository.get('root-private', owner_principal_id=first['principal_id']) == 'PRIVATE'

@pytest.mark.asyncio
async def test_recovery_storage_failure_is_bounded(client, monkeypatch):
    await login(client)
    await enroll(client)
    from sqlalchemy.exc import OperationalError
    async def unavailable(*args, **kwargs):
        raise OperationalError('PRIVATE SQL', {'secret': 'PRIVATE'}, Exception('PRIVATE'))
    monkeypatch.setattr('src.auth.ownership.inventory', unavailable)
    response = await client.get('/api/auth/ownership/recovery')
    assert response.status_code == 503 and 'PRIVATE' not in response.text
    assert response.json()['detail']['code'] == 'ownership_storage_unavailable'

@pytest.mark.asyncio
async def test_identity_revoke_closes_existing_socket_and_cancels_authority(client, monkeypatch):
    current = await login(client)
    await enroll(client)
    from threading import Event
    from src.api.ws import watch_operator_session, _await_authorized, _OperatorSessionRevoked
    revoked, guard, closed = asyncio.Event(), Event(), {}
    class Socket:
        async def close(self, *, code, reason): closed.update(code=code, reason=reason)
    monkeypatch.setattr(settings, 'operator_auth_revocation_poll_seconds', 0.05)
    watcher = asyncio.create_task(watch_operator_session(Socket(), current['session_id'], revoked, guard))
    assert (await client.post('/api/auth/ownership/revoke', headers=HEADERS)).status_code == 204
    await asyncio.wait_for(watcher, 2)
    assert closed == {'code': 4401, 'reason': 'session_revoked'} and revoked.is_set() and guard.is_set()
    with pytest.raises(_OperatorSessionRevoked): await _await_authorized(asyncio.sleep(10), revoked)

@pytest.mark.asyncio
async def test_synthetic_bypass_never_enrolls_durable_identity(client, monkeypatch):
    monkeypatch.setattr(settings, 'deployment_environment', 'test')
    monkeypatch.setattr(settings, 'operator_auth_allow_unauthenticated_tests', True)
    with pytest.raises(AuthFailure): await enroll_service(bypass_operator())

@pytest.mark.asyncio
async def test_exact_selected_artifact_lineage_and_forged_original_pair_denied(client, async_db):
    from src.db.models import WorkBoardInputArtifact, OperatorSession
    from sqlalchemy import select
    from src.auth.ownership import selected_read_scopes
    first = await login(client)
    await enroll(client)
    a, b = await goal(client), await goal(client)
    tasks = []
    for label, old_goal in [('A', a), ('B', b)]:
        result = await client.post('/api/work-board/tasks', headers=HEADERS, json={'title':label,'goal_id':old_goal['id'],'goal_revision':1,'idempotency_key':label})
        tasks.append(result.json()['task'])
    async with async_db() as db:
        for identifier, old_goal in [('artifact-a',a), ('artifact-b',b), ('artifact-unselected',a)]:
            db.add(WorkBoardInputArtifact(artifact_id=identifier,owner_principal_id=first['principal_id'],owner_session_id=first['session_id'],
                capability_id='browser.public-task.v1',capability_version='1',goal_id=old_goal['id'],goal_revision=1,
                payload_sha256='a'*64,size_bytes=0,typed_input_ref='unused/'+identifier,idempotency_key=identifier,expires_at=datetime.now(timezone.utc)+timedelta(hours=1)))
    await client.post('/api/auth/logout',headers=HEADERS)
    await login(client)
    selections = [{'kind':'task','record_id':t['task_id']} for t in tasks]+[{'kind':'artifact','record_id':identifier} for identifier in ('artifact-a','artifact-b')]
    journal,_ = await recover(client,selections)
    result = await client.post(f"/api/auth/ownership/recovery/{journal['journal_id']}/fresh-work",headers=HEADERS)
    assert result.status_code == 200,result.text
    fresh_a = next(item['task_id'] for item in result.json()['fresh_work']['tasks'] if item['source_task_id']==tasks[0]['task_id'])
    operator = await authenticate_token(client.cookies.get(settings.operator_auth_cookie_name),touch=False)
    assert await fresh_work_source_scope(operator,fresh_a,'artifact','artifact-a') == first['session_id']
    assert await fresh_work_source_scope(operator,fresh_a,'artifact','artifact-b') is None
    assert await fresh_work_source_scope(operator,fresh_a,'artifact','artifact-unselected') is None
    async with async_db() as db:
        row = (await db.execute(select(WorkBoardInputArtifact).where(WorkBoardInputArtifact.artifact_id=='artifact-a'))).scalar_one()
        row.owner_principal_id = 'operator:root:forged-principal'
    assert 'artifact-a' not in await selected_read_scopes(operator,'artifact')

@pytest.mark.asyncio
async def test_paired_device_and_telegram_effects_recheck_current_root(client, async_db, monkeypatch):
    from src.auth.service import authenticate_principal
    from src.extensions.telegram_transport import TelegramTransportAdapter,TelegramTransportError
    from src.db.models import TelegramTransportState
    first = await login(client)
    await enroll(client)
    state = TelegramTransportState(owner_principal_id=first['principal_id'],operator_session_id=first['session_id'],pairing_state='active',
        pairing_expires_at=datetime.now(timezone.utc)+timedelta(hours=1))
    assert (await authenticate_principal(first['principal_id'])).session_id == first['session_id']
    adapter=TelegramTransportAdapter()
    # Consent-specific checks may deny the active root for missing consent;
    # root lookup itself has already proved this is a real current owner.
    await client.post('/api/auth/ownership/revoke',headers=HEADERS)
    with pytest.raises(AuthFailure): await authenticate_principal(first['principal_id'])
    with pytest.raises(AuthFailure): await authenticate_principal('operator:single')
    with pytest.raises(TelegramTransportError,match='Reconnect'):
        await adapter._assert_active_row(state,current=datetime.now(timezone.utc))

@pytest.mark.asyncio
async def test_real_authenticated_websocket_rejects_existing_unowned_and_foreign_conversations(client,async_db,monkeypatch):
    import json
    from types import SimpleNamespace
    from starlette.websockets import WebSocketDisconnect
    from src.api.ws import websocket_chat
    first=await login(client)
    async with async_db() as db:
        db.add(Session(id='ws-unowned',owner_principal_id=None,title='PRIVATE'))
        db.add(Session(id='ws-legacy',owner_principal_id='operator:single',title='PRIVATE'))
    class Socket:
        headers={'host':'test',**HEADERS}
        cookies={settings.operator_auth_cookie_name:client.cookies.get(settings.operator_auth_cookie_name)}
        def __init__(self): self.frames=[];self.remaining=['ws-unowned','ws-legacy'];self.accepted=False
        async def accept(self): self.accepted=True
        async def close(self,**kwargs): pass
        async def send_text(self,text): self.frames.append(json.loads(text))
        async def receive_text(self):
            if not self.remaining: raise WebSocketDisconnect()
            return json.dumps({'type':'message','message':'attempt','session_id':self.remaining.pop(0)})
    async def profile(): return SimpleNamespace(onboarding_completed=True)
    monkeypatch.setattr('src.api.ws.get_or_create_profile',profile)
    socket=Socket()
    await websocket_chat(socket)
    errors=[frame for frame in socket.frames if frame['type']=='error']
    assert socket.accepted and len(errors)==2
    assert all('another operator' in frame['content'] and 'PRIVATE' not in str(frame) for frame in errors)
    async with async_db() as db:
        assert (await db.get(Session,'ws-unowned')).owner_principal_id is None
        from sqlalchemy import select
        assert (await db.execute(select(Message).where(Message.session_id.in_(['ws-unowned','ws-legacy'])))).scalars().all()==[]

@pytest.mark.asyncio
async def test_node_pair_inventory_and_foreign_replace_are_owner_fenced(client,app,tmp_path,monkeypatch):
    from tests.test_nodes_api import _write_node_pack
    workspace=tmp_path/'workspace'
    _write_node_pack(workspace)
    monkeypatch.setattr(settings,'workspace_dir',str(workspace))
    monkeypatch.setattr('src.vault.repository.encrypt',lambda value:value)
    monkeypatch.setattr('src.vault.repository.decrypt',lambda value:value)
    first=await login(client)
    pair={'extension_id':'seraph.openclaw-node','reference':'connectors/nodes/device.yaml','label':'PRIVATE device'}
    created=await client.post('/api/nodes/pairings/pair',headers=HEADERS,json=pair)
    assert created.status_code==200,created.text
    original=created.json()['credential']
    owned=(await client.get('/api/nodes/pairings')).json()
    assert 'PRIVATE device' in str(owned)
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as foreign:
        await login(foreign)
        response=await foreign.get('/api/nodes/pairings')
        assert response.status_code==200 and 'PRIVATE device' not in response.text
        assert 're_pair_required' in response.text
        assert (await foreign.post('/api/nodes/pairings/pair',headers=HEADERS,json=pair)).status_code==404
    # Explicit own replacement is fresh credential creation under existing
    # revision fencing, not transfer of the foreign or retired authority.
    second=await client.post('/api/nodes/pairings/pair',headers=HEADERS,json=pair)
    assert second.status_code==200 and second.json()['credential']!=original

@pytest.mark.asyncio
async def test_connection_cannot_adopt_global_or_foreign_vault_key(client,app,monkeypatch):
    from src.extensions.github_consent import GitHubConsentRequest
    from src.extensions.github_followthrough import GitHubFollowthroughService,GitHubFollowthroughError,CONNECTION_MODE_ACTIVE
    from src.vault.repository import vault_repository
    first=await login(client)
    monkeypatch.setattr('src.vault.repository.encrypt',lambda value:value)
    monkeypatch.setattr('src.vault.repository.decrypt',lambda value:value)
    await vault_repository.store('global-provider-secret','PRIVATE')
    await vault_repository.store('current-connection-secret','PRIVATE',owner_principal_id=first['principal_id'])
    service=GitHubFollowthroughService()
    consent=GitHubConsentRequest(acknowledged=True,duration_seconds=60,actions=['github_issue_write'])
    with pytest.raises(GitHubFollowthroughError,match='credential_not_configured'):
        await service.put_connection(owner_principal_id=first['principal_id'],owner_session_id=first['session_id'],repository='example/repository',vault_key='global-provider-secret',mode=CONNECTION_MODE_ACTIVE,expected_revision=0,consent=consent)
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as outsider:
        other=await login(outsider)
        with pytest.raises(GitHubFollowthroughError,match='credential_not_configured'):
            await service.put_connection(owner_principal_id=other['principal_id'],owner_session_id=other['session_id'],repository='example/repository',vault_key='current-connection-secret',mode=CONNECTION_MODE_ACTIVE,expected_revision=0,consent=consent)
    own=await service.put_connection(owner_principal_id=first['principal_id'],owner_session_id=first['session_id'],repository='example/repository',vault_key='current-connection-secret',mode=CONNECTION_MODE_ACTIVE,expected_revision=0,consent=consent)
    assert own['credential_configured'] is True
    assert own['consent']['state']=='active' and own['consent']['root_bound'] is True

@pytest.mark.asyncio
async def test_recovered_memory_citation_requires_accepted_exact_project_task_provenance(client,async_db):
    from src.db.models import Memory,MemoryProposal,MemoryStatus,MemoryProposalStatus
    first=await login(client)
    await enroll(client)
    a,b=await goal(client),await goal(client)
    tasks=[]
    for label,g in [('A',a),('B',b)]:
        response=await client.post('/api/work-board/tasks',headers=HEADERS,json={'title':label,'goal_id':g['id'],'goal_revision':1,'idempotency_key':label})
        tasks.append(response.json()['task'])
    # These canonical accepted rows are lineage fixtures, not a claim of model
    # learning/external outcome. #913 independently revalidates source evidence.
    async with async_db() as db:
        for suffix,g,t in [('a',a,tasks[0]),('b',b,tasks[1])]:
            db.add(Memory(id='memory-'+suffix,content='Reviewed local source',status=MemoryStatus.active,source_session_id=first['session_id'],last_confirmed_at=datetime.now(timezone.utc)))
            await db.flush()
            db.add(MemoryProposal(owner_principal_id=first['principal_id'],owner_session_id=first['session_id'],source_task_id=t['task_id'],source_attempt_id='lineage-fixture-'+suffix,
                goal_id=g['id'],goal_revision=1,capability_id='local-fixture',status=MemoryProposalStatus.accepted,accepted_memory_id='memory-'+suffix))
    await client.post('/api/auth/logout',headers=HEADERS)
    await login(client)
    journal,_=await recover(client,[{'kind':'task','record_id':t['task_id']} for t in tasks]+[{'kind':'memory','record_id':'memory-'+suffix} for suffix in ('a','b')])
    result=await client.post(f"/api/auth/ownership/recovery/{journal['journal_id']}/fresh-work",headers=HEADERS)
    assert result.status_code==200,result.text
    fresh_a=next(item['task_id'] for item in result.json()['fresh_work']['tasks'] if item['source_task_id']==tasks[0]['task_id'])
    operator=await authenticate_token(client.cookies.get(settings.operator_auth_cookie_name),touch=False)
    assert await fresh_work_source_scope(operator,fresh_a,'memory','memory-a')==first['session_id']
    assert await fresh_work_source_scope(operator,fresh_a,'memory','memory-b') is None
