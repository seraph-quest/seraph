"""Authenticated current owner inventory/control proof; no provider contact."""
from datetime import datetime,timedelta,timezone
import pytest
from tests.test_operator_identity import auth,login,goal,HEADERS
from src.db.models import AudioConsentGrant,CalendarReadConsent,GoogleServiceConnection,Goal

@pytest.mark.asyncio
@pytest.mark.parametrize('initial_status',['pending','approved'])
async def test_one_action_revoke_current_owner_revision_blocks_consumer(client,async_db,initial_status):
    from src.approval.repository import approval_repository
    from src.db.models import ApprovalRequest,Session
    owner=await login(client)
    async with async_db() as db:
        db.add(Session(id='approval-conversation',owner_principal_id=owner['principal_id']))
        await db.flush()
        db.add(ApprovalRequest(id='one-action',session_id='approval-conversation',tool_name='exact-effect',
            owner_principal_id=owner['principal_id'],operator_session_id=owner['session_id'],
            fingerprint='private-fingerprint',summary='private-body',status=initial_status,
            expires_at=datetime.now(timezone.utc)+timedelta(minutes=5)))
    view=(await client.get('/api/extensions/effective-grants')).json()
    grant=next(g for g in view['grants'] if g['grant_id']=='approval:one-action')
    assert 'private-fingerprint' not in str(view) and 'private-body' not in str(view)
    body={'grant_id':grant['grant_id'],'expected_revision':grant['revision'],'idempotency_key':'one-action-revoke'}
    stale=await client.post('/api/extensions/effective-grants/revoke',headers=HEADERS,json={**body,'expected_revision':grant['revision']+1})
    assert stale.status_code==409
    revoked=await client.post('/api/extensions/effective-grants/revoke',headers=HEADERS,json=body)
    assert revoked.status_code==200,revoked.text
    assert (await approval_repository.get('one-action')).status=='denied'
    consumed=await approval_repository.consume_approved(session_id='approval-conversation',tool_name='exact-effect',
        fingerprint='private-fingerprint',approval_id='one-action',owner_principal_id=owner['principal_id'],owner_operator_session_id=owner['session_id'])
    assert not consumed
    await client.post('/api/auth/logout',headers=HEADERS);await login(client)
    assert (await client.post('/api/extensions/effective-grants/revoke',headers=HEADERS,json=body)).status_code==404
    assert (await client.post('/api/approvals/one-action/revoke',headers=HEADERS,json={'expected_revision':grant['revision']})).status_code==403

@pytest.mark.asyncio
async def test_consumption_winner_is_spent_receipt_never_undo(client,async_db):
    from src.approval.repository import approval_repository
    from src.db.models import ApprovalRequest,Session
    owner=await login(client)
    async with async_db() as db:
        db.add(Session(id='spent-conversation',owner_principal_id=owner['principal_id']));await db.flush()
        db.add(ApprovalRequest(id='spent-action',session_id='spent-conversation',tool_name='exact-effect',
            owner_principal_id=owner['principal_id'],operator_session_id=owner['session_id'],fingerprint='exact',
            status='approved',expires_at=datetime.now(timezone.utc)+timedelta(minutes=5)))
    grant=next(g for g in (await client.get('/api/extensions/effective-grants')).json()['grants'] if g['grant_id']=='approval:spent-action')
    assert await approval_repository.consume_approved(session_id='spent-conversation',tool_name='exact-effect',
        fingerprint='exact',approval_id='spent-action',owner_principal_id=owner['principal_id'],owner_operator_session_id=owner['session_id'])
    result=await client.post('/api/extensions/effective-grants/revoke',headers=HEADERS,json={
        'grant_id':grant['grant_id'],'expected_revision':grant['revision'],'idempotency_key':'spent-revoke'})
    assert result.status_code==200,result.text
    assert result.json()['status']=='already_consumed'
    assert result.json()['external_revocation']=='not_confirmed'
    assert (await approval_repository.get('spent-action')).status=='consumed'
    spent=next(g for g in result.json()['readback']['grants'] if g['grant_id']==grant['grant_id'])
    assert spent['controls']==[]

@pytest.mark.asyncio
async def test_revoke_consume_race_file_database_has_one_winner(tmp_path,monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
    from sqlmodel import SQLModel
    from src.approval import repository
    from src.db.models import ApprovalRequest,Session
    engine=create_async_engine('sqlite+aiosqlite:///'+str(tmp_path/'approval-race.db'))
    async with engine.begin() as conn:await conn.run_sync(SQLModel.metadata.create_all)
    factory=async_sessionmaker(engine,expire_on_commit=False)
    @asynccontextmanager
    async def sessions():
        async with factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback();raise
    monkeypatch.setattr(repository,'get_session',sessions)
    try:
        async with sessions() as db:
            db.add(Session(id='race-conversation',owner_principal_id='operator:race'));await db.flush()
            row=ApprovalRequest(id='race-action',session_id='race-conversation',tool_name='exact-effect',
                owner_principal_id='operator:race',operator_session_id='root-race',fingerprint='race',status='approved',
                expires_at=datetime.now(timezone.utc)+timedelta(minutes=5))
            db.add(row);await db.flush()
        row=await repository.approval_repository.get('race-action')
        outcome,consumed=await asyncio.gather(
            repository.approval_repository.revoke_unconsumed('race-action',expected_revision=repository.approval_state_revision(row),
                owner_principal_id='operator:race',operator_session_id='root-race'),
            repository.approval_repository.consume_approved(session_id='race-conversation',tool_name='exact-effect',fingerprint='race',
                approval_id='race-action',owner_principal_id='operator:race',owner_operator_session_id='root-race'))
        final=await repository.approval_repository.get('race-action')
        assert (outcome=='revoked' and not consumed and final.status=='denied') or (outcome=='already_consumed' and consumed and final.status=='consumed')
    finally:await engine.dispose()

@pytest.mark.asyncio
async def test_current_owner_redacted_inventory_real_goal_revoke_and_stale_revision(client,async_db):
    first=await login(client)
    g=await goal(client)
    enabled=await client.patch('/api/goals/'+g['id'],headers=HEADERS,json={'proactive_enabled':True,'expected_revision':g['revision']})
    assert enabled.status_code==200,enabled.text
    async with async_db() as db:
        expiry=datetime.now(timezone.utc)+timedelta(hours=1)
        db.add(AudioConsentGrant(reference='audio-consent:capture:'+'a'*32,owner_principal_id=first['principal_id'],operator_session_id=first['session_id'],boundary='capture',expires_at=expiry))
        db.add(AudioConsentGrant(reference='foreign-private-token',owner_principal_id='operator:single',operator_session_id='foreign',boundary='capture',expires_at=expiry))
    view=await client.get('/api/extensions/effective-grants')
    assert view.status_code==200,view.text
    assert 'foreign-private-token' not in view.text
    assert view.json()['authority_cache'] is False
    grant=next(x for x in view.json()['grants'] if x['grant_id']=='goal:'+g['id'])
    body={'grant_id':grant['grant_id'],'expected_revision':grant['revision'],'idempotency_key':'goal-revoke-one'}
    result=await client.post('/api/extensions/effective-grants/revoke',headers=HEADERS,json=body)
    assert result.status_code==200,result.text
    assert result.json()['status']=='local_revocation_confirmed'
    async with async_db() as db:
        row=await db.get(Goal,g['id'])
        assert not row.proactive_enabled
    assert (await client.post('/api/extensions/effective-grants/revoke',headers=HEADERS,json=body)).status_code==404
    await client.post('/api/auth/logout',headers=HEADERS)
    await login(client)
    view=await client.get('/api/extensions/effective-grants')
    assert all(x['record_id']!=g['id'] for x in view.json()['grants'])
    assert (await client.post('/api/extensions/effective-grants/revoke',headers=HEADERS,json=body)).status_code==404

@pytest.mark.asyncio
async def test_connected_credential_does_not_grant_model_transfer_private_scopes_not_returned(client,async_db):
    owner=await login(client);g=await goal(client)
    async with async_db() as db:
        c=GoogleServiceConnection(owner_principal_id=owner['principal_id'],owner_session_id=owner['session_id'],service='calendar_readonly',vault_secret_key='must-never-expose-secret-key',label='private account label',state='active')
        db.add(c);await db.flush()
        consent=CalendarReadConsent(owner_principal_id=owner['principal_id'],owner_session_id=owner['session_id'],connection_id=c.connection_id,calendar_id='private-calendar-id',goal_id=g['id'],allow_remote_model=False,expires_at=datetime.now(timezone.utc)+timedelta(hours=1))
        db.add(consent);await db.flush();consent_id=consent.consent_id
    view=await client.get('/api/extensions/effective-grants')
    assert view.status_code==200,view.text
    assert all(private not in view.text for private in ('must-never-expose-secret-key','private account label','private-calendar-id'))
    entries=view.json()['grants']
    assert next(x for x in entries if x['grant_id']=='calendar_consent:'+consent_id)['state']=='active'
    assert next(x for x in entries if x['grant_id']=='calendar_consent_model:'+consent_id)['state']=='denied'
    assert next(x for x in entries if x['kind']=='calendar_connection')['limits']['credential_is_grant'] is False

@pytest.mark.asyncio
@pytest.mark.parametrize('during_transport',[False,True])
async def test_inventory_revoke_real_audio_queued_and_late_result_fenced(client,async_db,tmp_path,monkeypatch,during_transport):
    from dataclasses import replace
    from src.api import audio
    from src.auth.service import authenticate_token
    from config.settings import settings
    from src.agent.session import session_manager
    from src.guardian.audio_worker import AudioIngressWorker,InterceptedAudioTransport
    from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
    from tests.test_audio_worker import _request,CAPTURE_REF,MODEL_REF
    first=await login(client)
    operator=await authenticate_token(client.cookies.get(settings.operator_auth_cookie_name))
    session=await session_manager.get_or_create('audio-current-conversation',owner_principal_id=first['principal_id'])
    async with async_db() as db:
        for ref,boundary in ((CAPTURE_REF,'capture'),(MODEL_REF,'cloud_upload')):
            db.add(AudioConsentGrant(reference=ref,owner_principal_id=first['principal_id'],operator_session_id=first['session_id'],boundary=boundary,granted_at=datetime.now(timezone.utc)-timedelta(seconds=1),expires_at=datetime.now(timezone.utc)+timedelta(minutes=15)))
    calls=[]
    async def intercepted(**kwargs):
        calls.append(kwargs['request_id'])
        if during_transport:
            result=await client.post('/api/extensions/effective-grants/revoke',headers=HEADERS,json={'grant_id':'audio:'+MODEL_REF,'expected_revision':1,'idempotency_key':'audio-real-revoke'})
            assert result.status_code==200,result.text
        return {'transcript':'late private response must never be adopted'}
    worker=AudioIngressWorker(transport=InterceptedAudioTransport(intercepted),admission_broker=RemoteInferenceAdmissionBroker(),quarantine_root=tmp_path/'audio')
    monkeypatch.setattr(audio,'default_audio_worker',worker)
    upload=replace(_request(session.id,request_id='audio-grant-journey'),owner_principal_id=first['principal_id'],operator_session_id=first['session_id'])
    queued=await worker.submit(upload,process=False,authority_principal=operator.principal)
    view=(await client.get('/api/extensions/effective-grants')).json()
    grant=next(g for g in view['grants'] if g['grant_id']=='audio:'+MODEL_REF)
    assert grant['affected_jobs'][0]['job_id']==queued.request_id
    if not during_transport:
        result=await client.post('/api/extensions/effective-grants/revoke',headers=HEADERS,json={'grant_id':grant['grant_id'],'expected_revision':1,'idempotency_key':'audio-real-revoke'})
        assert result.status_code==200,result.text
    final=await worker.process(queued.request_id,owner_principal_id=first['principal_id'],operator_session_id=first['session_id'],authority_principal=operator.principal)
    assert final.status not in {'transcript_ready','confirmed'}
    assert worker.review_transcript(queued.request_id,owner_principal_id=first['principal_id'],operator_session_id=first['session_id']) is None
    assert len(calls)==int(during_transport)

@pytest.mark.asyncio
async def test_connection_cleanup_failure_locally_fences_and_exact_retry_recovers(client,async_db,monkeypatch):
    from src.api import mail
    monkeypatch.setattr(mail,'get_session',async_db)
    from src.api.mail import GMAIL_SERVICE
    owner=await login(client)
    async with async_db() as db:
        c=GoogleServiceConnection(owner_principal_id=owner['principal_id'],owner_session_id=owner['session_id'],service=GMAIL_SERVICE,vault_secret_key='isolated-mail-secret',state='active')
        db.add(c);await db.flush();cid=c.connection_id
    async def fail(*args,**kwargs):raise RuntimeError('private provider secret must not appear')
    async def success(*args,**kwargs):return True
    monkeypatch.setattr(mail.vault_repository,'delete',fail)
    body={'grant_id':'mail_connection:'+cid,'expected_revision':1,'idempotency_key':'cleanup-exact-one'}
    result=await client.post('/api/extensions/effective-grants/revoke',headers=HEADERS,json=body)
    assert result.status_code==200,result.text
    assert result.json()['status']=='partial_failure'
    assert result.json()['local_state'] not in {'active','unconfirmed'}
    assert 'private provider secret' not in result.text
    monkeypatch.setattr(mail.vault_repository,'delete',success)
    monkeypatch.setattr(mail,'_cleanup_connection_mail_artifacts',success)
    repeated=await client.post('/api/extensions/effective-grants/revoke',headers=HEADERS,json=body)
    assert repeated.status_code==200,repeated.text
    assert repeated.json()['status']=='local_revocation_confirmed'
    assert next(g for g in repeated.json()['readback']['grants'] if g['grant_id']==body['grant_id'])['state']=='revoked'

@pytest.mark.asyncio
async def test_host_local_exact_reset_blocks_stale_and_old_credential_then_fresh_pair(client,async_db,tmp_path,monkeypatch):
    from config.settings import settings
    from src.extensions.pairing_reset import reset_pairing
    from src.extensions.state import load_extension_state_payload,ExtensionStateRevisionConflict
    from src.extensions.paired_edge import verify_pairing_credential
    from tests.test_nodes_api import _write_node_pack
    workspace=tmp_path/'pairing';_write_node_pack(workspace)
    monkeypatch.setattr(settings,'workspace_dir',str(workspace))
    monkeypatch.setattr('src.vault.repository.encrypt',lambda v:v)
    monkeypatch.setattr('src.vault.repository.decrypt',lambda v:v)
    await login(client)
    pair={'extension_id':'seraph.openclaw-node','reference':'connectors/nodes/device.yaml'}
    created=await client.post('/api/nodes/pairings/pair',headers=HEADERS,json=pair)
    assert created.status_code==200,created.text
    old=created.json()['credential'];payload=load_extension_state_payload();revision=payload['revision']
    await client.post('/api/auth/logout',headers=HEADERS);await login(client)
    assert (await client.post('/api/nodes/pairings/pair',headers=HEADERS,json=pair)).status_code==404
    view=(await client.get('/api/extensions/effective-grants')).json()
    assert next(g for g in view['grants'] if g['kind']=='node')['controls']==['host_local_reset']
    with pytest.raises(ExtensionStateRevisionConflict):
        await reset_pairing(**pair,expected_revision=revision-1)
    assert load_extension_state_payload()['revision']==revision
    result=await reset_pairing(**pair,expected_revision=revision)
    assert result['state']=='fresh_pairing_required'
    # Cached pre-reset state cannot authenticate its old secret generation.
    with pytest.raises((ValueError,RuntimeError)):
        await verify_pairing_credential(payload,**pair,name='openclaw-device',presented_credential=old)
    fresh=await client.post('/api/nodes/pairings/pair',headers=HEADERS,json=pair)
    assert fresh.status_code==200 and fresh.json()['credential']!=old
    assert result['authority_imported'] is False and result['history_imported'] is False

@pytest.mark.asyncio
@pytest.mark.parametrize('revoke_at',['none','dns','response','identity'])
async def test_finite_service_grant_survives_browser_expiry_but_final_read_and_adoption_are_fenced(client,async_db,monkeypatch,revoke_at):
    import httpx,json
    from src.db.models import GuardianSourceWatch,OperatorSession,OperatorIdentity
    from src.guardian import source_watch
    from src.security.http_transport import fetch_pinned_https
    from tests.test_operator_identity import enroll
    owner=await login(client);await enroll(client);g=await goal(client)
    sources=source_watch.parse_sources([{'source_key':'public-status','kind':'public_https_text','target':'https://example.com/status'}])
    now=datetime.now(timezone.utc)
    async with async_db() as db:
        goal_row=await db.get(Goal,g['id']);goal_row.proactive_enabled=True
        goal_row.admission_budget_json=json.dumps({'reviewed_grant':True,'grant_id':'finite-service-grant','period_expires_at':(now+timedelta(hours=1)).isoformat()})
        root=await db.get(OperatorSession,owner['session_id']);root.idle_expires_at=now-timedelta(seconds=1)
        watch=GuardianSourceWatch(goal_id=g['id'],owner_principal_id=owner['principal_id'],owner_session_id=owner['session_id'],scheduled_job_id='standing-source-job',
            sources_json=json.dumps([{'source_key':s.source_key,'kind':s.kind,'target':s.target} for s in sources]),criteria_json='{}',read_authority_json='{"grant_id":"finite-service-grant"}')
        db.add(watch);await db.flush();wid=watch.id
        if revoke_at=='identity':
            identity=await db.get(OperatorIdentity,root.operator_identity_id);identity.revoked_at=now
    stream_calls=[]
    async def revoke_watch():
        async with async_db() as db:
            w=await db.get(GuardianSourceWatch,wid);w.state='revoked';w.plan_revision+=1
    async def resolver(*args):
        if revoke_at=='dns':await revoke_watch()
        return ['93.184.216.34']
    async def handler(request):
        stream_calls.append(request.url)
        if revoke_at=='response':await revoke_watch()
        return httpx.Response(200,headers={'content-type':'text/plain'},text='local deterministic source proof')
    async def fetch(url,**kwargs):
        return await fetch_pinned_https(url,resolver=resolver,transport=httpx.MockTransport(handler),**kwargs)
    monkeypatch.setattr(source_watch,'fetch_pinned_https',fetch)
    async with async_db() as db:
        watch=await db.get(GuardianSourceWatch,wid);db.expunge(watch)
    result=await source_watch.SourceWatchService()._scan(watch,occurrence_id='accepted-service-occurrence-proof')
    if revoke_at=='none':
        assert result.successful_sources==1 and len(stream_calls)==1
    else:
        assert result.successful_sources==0
        assert len(stream_calls)==int(revoke_at=='response')
        assert all(item.baseline_text is None for item in result.observations)

@pytest.mark.asyncio
async def test_github_owner_local_revoke_retains_reserved_liability_and_stale_dispatch_denies(client,async_db):
    from src.db.models import GitHubFollowthroughConnection
    from src.extensions.github_followthrough import github_followthrough_service,GitHubFollowthroughError
    owner=await login(client)
    async with async_db() as db:
        connection=GitHubFollowthroughConnection(owner_principal_id=owner['principal_id'],repository='private/repository',vault_key='private-key',mode='active',revision=7,active_job_id='contacted-job',active_fence=4)
        db.add(connection);await db.flush();cid=connection.id
    view=await client.get('/api/extensions/effective-grants')
    assert 'private/repository' not in view.text and 'private-key' not in view.text
    grant=next(g for g in view.json()['grants'] if g['kind']=='github')
    response=await client.post('/api/extensions/effective-grants/revoke',headers=HEADERS,json={'grant_id':grant['grant_id'],'expected_revision':7,'idempotency_key':'local-gh-revoke'})
    assert response.status_code==200,response.text
    async with async_db() as db:
        current=await db.get(GitHubFollowthroughConnection,cid)
        assert current.mode=='disabled' and current.active_job_id=='contacted-job' and current.active_fence==4
    with pytest.raises(GitHubFollowthroughError,match='connection_dispatch_binding_stale'):
        await github_followthrough_service._assert_dispatch_binding(owner_principal_id=owner['principal_id'],connection_id=cid,expected_revision=7,repository='private/repository',vault_key='private-key',mode='active',job_id='contacted-job',fence=4)

@pytest.mark.asyncio
async def test_exact_owner_can_revoke_stale_goal_watch_but_cannot_edit_through_stop_bypass(client,async_db):
    import json
    from src.db.models import GuardianSourceWatch
    owner=await login(client);g=await goal(client)
    async with async_db() as db:
        goal_row=await db.get(Goal,g['id']);goal_row.revision=3
        watch=GuardianSourceWatch(owner_principal_id=owner['principal_id'],owner_session_id=owner['session_id'],goal_id=g['id'],goal_revision=1,scheduled_job_id='isolated-stop-watch',write_mode='standing_reviewed')
        db.add(watch);await db.flush();wid=watch.id
    edit=await client.patch('/api/capabilities/source-watches/'+wid,headers=HEADERS,json={'expected_plan_revision':1,'state':'revoked','schedule':{'cron':'0 * * * *','timezone':'UTC'}})
    assert edit.status_code==409,edit.text
    result=await client.post('/api/extensions/effective-grants/revoke',headers=HEADERS,json={'grant_id':'source_watch:'+wid,'expected_revision':1,'idempotency_key':'stop-stale-watch'})
    assert result.status_code==200,result.text
    async with async_db() as db:
        current=await db.get(GuardianSourceWatch,wid)
        assert current.state=='revoked' and current.plan_revision==2
    repeated=await client.patch('/api/capabilities/source-watches/'+wid,headers=HEADERS,json={'expected_plan_revision':1,'state':'active'})
    assert repeated.status_code==409,repeated.text

@pytest.mark.asyncio
async def test_actual_host_local_command_exact_fixture_and_compatibility_guard(tmp_path,monkeypatch):
    import asyncio,hashlib,json,subprocess,sys
    from pathlib import Path
    from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
    from sqlalchemy import text
    from sqlmodel import SQLModel,select
    from src.db.models import Secret
    from src.extensions.paired_edge import PAIRING_CREDENTIAL_PREFIX
    from src.extensions.state import load_extension_state_payload,save_extension_state_payload,set_node_adapter_pairing_entry
    from config.settings import settings
    from tests.test_nodes_api import _write_node_pack
    workspace=tmp_path/'isolated-cli';_write_node_pack(workspace)
    monkeypatch.setattr(settings,'workspace_dir',str(workspace))
    key='seraph-node-pairing-'+'b'*40
    payload=load_extension_state_payload()
    set_node_adapter_pairing_entry(payload,extension_id='seraph.openclaw-node',reference='connectors/nodes/device.yaml',name='openclaw-device',pairing={
        'owner_principal_id':'operator:old-fixture','owner_session_id':'root-old-fixture','paired':True,
        'credential_ref':PAIRING_CREDENTIAL_PREFIX+hashlib.sha256(key.encode()).hexdigest()[:24]})
    revision=save_extension_state_payload(payload,expected_revision=int(payload.get('revision') or 0))
    engine=create_async_engine('sqlite+aiosqlite:///'+str(workspace/'seraph.db'))
    factory=async_sessionmaker(engine,expire_on_commit=False)
    try:
        async with engine.begin() as conn:await conn.run_sync(SQLModel.metadata.create_all)
        async with factory() as db:
            db.add(Secret(key=key,encrypted_value='fixture-only'))
            db.add(Secret(key='deployment-provider-unrelated',encrypted_value='unchanged-fixture'));await db.commit()
        args=[sys.executable,str(Path(__file__).resolve().parents[2]/'scripts/reset-node-pairing.py'),'--workspace',str(workspace),
            '--extension-id','seraph.openclaw-node','--reference','connectors/nodes/device.yaml','--expected-revision',str(revision),'--acknowledge-owner-reset']
        rejected=await asyncio.to_thread(subprocess.run,args,capture_output=True,text=True,timeout=15)
        assert rejected.returncode!=0 and 'Compatible migrated workspace required' in rejected.stderr
        assert load_extension_state_payload()['revision']==revision
        async with engine.begin() as conn:await conn.execute(text('PRAGMA user_version=900'))
        result=await asyncio.to_thread(subprocess.run,args,capture_output=True,text=True,timeout=15)
        assert result.returncode==0,result.stderr
        assert json.loads(result.stdout)['state']=='fresh_pairing_required'
        async with factory() as db:
            target=(await db.execute(select(Secret).where(Secret.key==key))).scalars().one()
            other=(await db.execute(select(Secret).where(Secret.key=='deployment-provider-unrelated'))).scalars().one()
            assert target.revoked_at is not None and target.encrypted_value=='revoked:host-local-pairing-reset'
            assert other.revoked_at is None and other.encrypted_value=='unchanged-fixture'
    finally:await engine.dispose()

@pytest.mark.asyncio
@pytest.mark.parametrize('blocked_by',['goal_revision','grant_expiry','grant_revision','paused'])
async def test_watch_projection_uses_live_owner_grant_readiness(client,async_db,blocked_by):
    import json
    from src.db.models import GuardianSourceWatch
    owner=await login(client);g=await goal(client)
    async with async_db() as db:
        row=await db.get(Goal,g['id']);row.proactive_enabled=True
        row.admission_budget_json=json.dumps({'reviewed_grant':True,'grant_id':'reviewed-read','period_expires_at':(datetime.now(timezone.utc)+timedelta(hours=-1 if blocked_by=='grant_expiry' else 1)).isoformat()})
        db.add(GuardianSourceWatch(id='projection-watch',owner_principal_id=owner['principal_id'],owner_session_id=owner['session_id'],goal_id=g['id'],goal_revision=2 if blocked_by=='goal_revision' else 1,
            sources_json='[{"source_key":"public","kind":"public_https_text","target":"https://example.com/status"}]',
            state='paused' if blocked_by=='paused' else 'active',scheduled_job_id='watch-job',read_authority_json=json.dumps({'grant_id':'other-grant' if blocked_by=='grant_revision' else 'reviewed-read'})))
    response=await client.get('/api/extensions/effective-grants')
    assert response.status_code==200,response.text
    item=next(x for x in response.json()['grants'] if x['grant_id']=='source_watch:projection-watch')
    assert item['state']=='blocked' and item['controls']==['revoke']
    assert item['stored_state']==('paused' if blocked_by=='paused' else 'active')
    assert item['blocked_reason']=={'goal_revision':'goal_binding_stale','grant_expiry':'goal_budget_period_expired','grant_revision':'standing_grant_stale','paused':'watch_read_authority_revoked'}[blocked_by]

@pytest.mark.asyncio
@pytest.mark.parametrize('blocked_by',['expiry','paused_job','goal_revision','consent_revision'])
async def test_schedule_projection_reuses_actual_scheduler_readiness(client,async_db,blocked_by):
    import json
    from src.db.models import GovernedScheduleBinding,ScheduledJob,WorkBoardInputArtifact
    owner=await login(client);g=await goal(client);now=datetime.now(timezone.utc)
    async with async_db() as db:
        db.add(ScheduledJob(id='exact-job',enabled=blocked_by!='paused_job',trigger_type='governed',action_type='calendar.observe_due_events.v1',
            trigger_spec_json=json.dumps({'kind':'5min','timezone':'UTC'}),action_spec_json=json.dumps({'binding_id':'exact-binding'})))
        db.add(GovernedScheduleBinding(binding_id='exact-binding',scheduled_job_id='exact-job',owner_principal_id=owner['principal_id'],owner_session_id=owner['session_id'],
            goal_id=g['id'],goal_revision=2 if blocked_by=='goal_revision' else 1,input_artifact_id='schedule-artifact',read_consent_id='exact-consent',
            consent_revision=2 if blocked_by=='consent_revision' else 1,expires_at=now+timedelta(hours=-1 if blocked_by=='expiry' else 1)))
        connection=GoogleServiceConnection(owner_principal_id=owner['principal_id'],owner_session_id=owner['session_id'],service='calendar_readonly',vault_secret_key='fixture-only',state='active')
        db.add(connection);await db.flush()
        db.add(CalendarReadConsent(consent_id='exact-consent',owner_principal_id=owner['principal_id'],owner_session_id=owner['session_id'],connection_id=connection.connection_id,
            calendar_id='private',goal_id=g['id'],allow_remote_model=True,expires_at=now+timedelta(hours=1)))
        db.add(WorkBoardInputArtifact(artifact_id='schedule-artifact',owner_principal_id=owner['principal_id'],owner_session_id=owner['session_id'],goal_id=g['id'],goal_revision=1,
            capability_id='calendar.observe_due_events.v1',capability_version='v1',idempotency_key='schedule-input',payload_sha256='fixture',typed_input_ref='fixture-only',expires_at=now+timedelta(hours=1),metadata_digest='fixture'))
    response=await client.get('/api/extensions/effective-grants')
    assert response.status_code==200,response.text
    item=next(x for x in response.json()['grants'] if x['grant_id']=='schedule:exact-binding')
    assert item['state']=='blocked' and item['stored_state']=='active' and item['controls']==['revoke']
    assert item['blocked_reason']=='governed_schedule_prerequisite_stale'
    consent=next(x for x in response.json()['grants'] if x['grant_id']=='calendar_consent:exact-consent')
    assert consent['affected_jobs']==[{'job_id':'exact-job','state':'active','kind':'scheduled_job'}]

@pytest.mark.asyncio
async def test_provider_metadata_failure_does_not_hide_owned_controls(client,monkeypatch):
    import sys,types
    await login(client);g=await goal(client)
    enabled=await client.patch('/api/goals/'+g['id'],headers=HEADERS,json={'proactive_enabled':True,'expected_revision':g['revision']})
    assert enabled.status_code==200
    async def failed(operator):raise RuntimeError('private provider account must not leak')
    module=types.ModuleType('src.model_fabric.effective_policy');module.effective_policy_grants=failed
    monkeypatch.setitem(sys.modules,'src.model_fabric.effective_policy',module)
    response=await client.get('/api/extensions/effective-grants')
    assert response.status_code==200,response.text
    assert response.json()['unavailable']==['provider_policy']
    assert 'private provider' not in response.text
    assert next(x for x in response.json()['grants'] if x['grant_id']=='goal:'+g['id'])['controls']==['revoke']

@pytest.mark.asyncio
@pytest.mark.parametrize('root_state',['expired','logout'])
@pytest.mark.parametrize('source_kind',['workspace','mixed'])
@pytest.mark.parametrize('write_mode',['standing_reviewed','approval_each_run'])
async def test_private_legacy_standing_plan_after_expiry_never_reads_or_adopts(client,async_db,tmp_path,monkeypatch,root_state,source_kind,write_mode):
    import json
    from sqlalchemy import select
    from src.db.models import GuardianSourceWatch,OperatorSession,GuardianSourceBaseline,GuardianDecisionPacket
    from src.guardian import source_watch
    from config.settings import settings
    owner=await login(client);g=await goal(client);now=datetime.now(timezone.utc)
    monkeypatch.setattr(settings,'workspace_dir',str(tmp_path))
    (tmp_path/'private-note.txt').write_text('private local evidence must remain unread')
    sources=[{'source_key':'private','kind':'workspace_text','target':'private-note.txt'}]
    if source_kind=='mixed':sources.insert(0,{'source_key':'public','kind':'public_https_text','target':'https://example.com/status'})
    async with async_db() as db:
        row=await db.get(Goal,g['id']);row.proactive_enabled=True
        row.admission_budget_json=json.dumps({'reviewed_grant':True,'grant_id':'private-old-grant','period_expires_at':(now+timedelta(hours=1)).isoformat()})
        root=await db.get(OperatorSession,owner['session_id'])
        if root_state=='expired':root.idle_expires_at=now-timedelta(seconds=1)
        db.add(GuardianSourceWatch(id='legacy-private-watch',owner_principal_id=owner['principal_id'],owner_session_id=owner['session_id'],goal_id=g['id'],
            write_mode=write_mode,sources_json=json.dumps(sources),scheduled_job_id='legacy-job',read_authority_json='{"grant_id":"private-old-grant"}'))
    if root_state=='logout':assert (await client.post('/api/auth/logout',headers=HEADERS)).status_code==204
    calls=[]
    original=source_watch._read_workspace_text_bounded
    def file_read(*args,**kwargs):calls.append('file');return original(*args,**kwargs)
    async def network(*args,**kwargs):calls.append('network');raise AssertionError('expired mixed watch must never contact network')
    monkeypatch.setattr(source_watch,'_read_workspace_text_bounded',file_read)
    monkeypatch.setattr(source_watch,'fetch_pinned_https',network)
    async with async_db() as db:
        watch=await db.get(GuardianSourceWatch,'legacy-private-watch');db.expunge(watch)
    result=await source_watch.SourceWatchService()._scan(watch,occurrence_id='accepted-legacy-occurrence')
    assert calls==[] and result.successful_sources==0 and not result.baseline_updates
    assert all(x.baseline_text is None and x.error_code=='private_source_requires_live_browser' for x in result.observations)
    async with async_db() as db:
        assert not (await db.execute(select(GuardianSourceBaseline).where(GuardianSourceBaseline.watch_id==watch.id))).scalars().all()
        assert not (await db.execute(select(GuardianDecisionPacket).where(GuardianDecisionPacket.watch_id==watch.id))).scalars().all()

@pytest.mark.asyncio
async def test_live_manual_workspace_remains_available_standing_review_is_public_only(client,async_db,tmp_path,monkeypatch):
    import json
    from config.settings import settings
    from src.guardian import source_watch
    from src.db.models import GuardianSourceWatch
    owner=await login(client);g=await goal(client)
    monkeypatch.setattr(settings,'workspace_dir',str(tmp_path));(tmp_path/'review-note.txt').write_text('bounded authenticated local read')
    async with async_db() as db:
        row=await db.get(Goal,g['id']);row.proactive_enabled=True
        row.admission_budget_json=json.dumps({'reviewed_grant':True,'grant_id':'manual-grant','period_expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()})
    body={'goal_id':g['id'],'expected_goal_revision':1,'sources':[{'source_key':'local','kind':'workspace_text','target':'review-note.txt'}],
        'schedule':{'cron':'0 * * * *','timezone':'UTC'},'write_mode':'standing_reviewed','reviewed_grant_id':'manual-grant'}
    denied=await client.post('/api/capabilities/source-watches',headers=HEADERS,json=body)
    assert denied.status_code==400 and denied.json()['detail']['code']=='standing_sources_must_be_public',denied.text
    created=await client.post('/api/capabilities/source-watches',headers=HEADERS,json={**body,'write_mode':'approval_each_run'})
    assert created.status_code==200,created.text
    data=created.json();assert data['read_authority']['browser_expiry_scope']=='public_https_text_only'
    review=await client.patch('/api/capabilities/source-watches/'+data['id'],headers=HEADERS,json={'expected_plan_revision':1,'write_mode':'standing_reviewed','reviewed_grant_id':'manual-grant'})
    assert review.status_code==400 and review.json()['detail']['code']=='standing_sources_must_be_public',review.text
    async with async_db() as db:
        watch=await db.get(GuardianSourceWatch,data['id']);assert watch.write_mode=='approval_each_run' and watch.plan_revision==1;db.expunge(watch)
    result=await source_watch.SourceWatchService()._scan(watch,occurrence_id='authenticated-manual-occurrence')
    assert result.successful_sources==1 and result.baseline_updates[0].baseline_text=='bounded authenticated local read'

@pytest.mark.asyncio
@pytest.mark.parametrize('sources',['[]','{"kind":"public_https_text"}','not-json','[{"source_key":"bad","kind":"unknown","target":"private.txt"}]'])
async def test_malformed_or_unknown_sources_never_receive_public_exception(client,async_db,sources):
    from src.guardian.source_watch import SourceWatchService,SourceWatchError
    from src.db.models import GuardianSourceWatch,OperatorSession
    owner=await login(client);g=await goal(client)
    async with async_db() as db:
        root=await db.get(OperatorSession,owner['session_id']);root.idle_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)
        watch=GuardianSourceWatch(owner_principal_id=owner['principal_id'],owner_session_id=owner['session_id'],goal_id=g['id'],sources_json=sources,scheduled_job_id='bad-source-job')
        db.add(watch);await db.flush();db.expunge(watch)
    with pytest.raises(SourceWatchError,match='watch_source_classification_invalid'):
        await SourceWatchService()._assert_read_authority(watch)

@pytest.mark.asyncio
async def test_disabled_goal_has_no_revoke_but_enabled_blocked_goal_retains_stop(client,async_db):
    owner=await login(client);g=await goal(client)
    disabled=next(x for x in (await client.get('/api/extensions/effective-grants')).json()['grants'] if x['grant_id']=='goal:'+g['id'])
    assert disabled['controls']==[]
    body={'grant_id':disabled['grant_id'],'expected_revision':disabled['revision'],'idempotency_key':'disabled-noop'}
    assert (await client.post('/api/extensions/effective-grants/revoke',headers=HEADERS,json=body)).status_code==404
    async with async_db() as db:assert (await db.get(Goal,g['id'])).revision==g['revision']
    enabled=await client.patch('/api/goals/'+g['id'],headers=HEADERS,json={'proactive_enabled':True,'expected_revision':g['revision']})
    assert enabled.status_code==200
    blocked=next(x for x in (await client.get('/api/extensions/effective-grants')).json()['grants'] if x['grant_id']==disabled['grant_id'])
    assert blocked['state'].startswith('blocked_') and blocked['controls']==['revoke']
    stop=await client.post('/api/extensions/effective-grants/revoke',headers=HEADERS,json={**body,'expected_revision':blocked['revision']})
    assert stop.status_code==200 and stop.json()['status']=='local_revocation_confirmed'
    after=next(x for x in stop.json()['readback']['grants'] if x['grant_id']==disabled['grant_id'])
    assert after['controls']==[] and after['revision']==blocked['revision']+1
