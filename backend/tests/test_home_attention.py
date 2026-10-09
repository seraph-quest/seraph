"""Current Home owner production and bounded negative metadata mechanics."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import json
import pytest
from sqlalchemy import event,select
from tests.test_home_continuation import accounting_db,home_setup
from tests.test_research_native_vertical import real_auth
from tests.test_general_task_planner import forbid_external_inference
from src.operator import home_projection as hm
from src.goals.repository import GoalRepository
from src.db.models import GuardianInboxDisposition,GuardianOpportunity,GuardianDecisionPacket,Goal


async def test_genuine_source_and_opportunity_auth_home_wire(accounting_db,real_auth,monkeypatch):
    from fastapi import FastAPI
    from src.api.operator import router
    from src.api.guardian_inbox import router as inbox_router
    from tests.test_guardian_opportunity_vertical import test_actual_http_goal_watch_native_cited_inbox
    original=FastAPI.include_router
    def include(app,*args,**kwargs):
        result=original(app,*args,**kwargs)
        if not getattr(app,'_home_observer_route',False):
            app._home_observer_route=True
            original(app,router,prefix='/api')
            original(app,inbox_router,prefix='/api')
        return result
    monkeypatch.setattr(FastAPI,'include_router',include)
    monkeypatch.setattr(hm,'get_session',accounting_db[2].accounting_sessions)
    statements=[]
    def observe(_conn,_cursor,statement,*_): statements.append(statement)
    async def capture(client,owner,stage):
        if not hm.home_projection.started: hm.home_projection.start()
        statements.clear()
        event.listen(accounting_db[1].sync_engine,'before_cursor_execute',observe)
        try:
            result=await client.get('/api/operator/continuation')
        finally: event.remove(accounting_db[1].sync_engine,'before_cursor_execute',observe)
        assert result.status_code==200,result.text
        rows=result.json()['task_next_actions']['items']
        decisions=[r for r in rows if r['kind']=='inbox_decision']
        assert any(r['source_kind']=='source_packet' for r in decisions)
        opportunities=[r for r in decisions if r['source_kind']=='guardian_opportunity']
        if stage=='queued': assert not opportunities
        else:
            assert len(opportunities)==1 and opportunities[0]['state']==('snoozed' if stage=='snoozed' else 'pending')
            assert opportunities[0]['title']=='Public evidence opportunity'
            assert opportunities[0]['target']['inbox_id']==opportunities[0]['inbox_id']
        forbidden=('proposal_text','task_text','source_observation_json','assessment_json','source_token_json','provider_message_id_ciphertext')
        assert not any(any(word in statement for word in forbidden) for statement in statements)
        assert not any(statement.lstrip().upper().startswith(('UPDATE','INSERT','DELETE')) for statement in statements)
        assert len([s for s in statements if s.lstrip().upper().startswith(('SELECT','WITH'))])<=19
        (accounting_db[0]/f'home-attention-{stage}-wire.json').write_text(result.text)
        native=await client.get('/api/guardian/inbox',params={'limit':50})
        assert native.status_code==200,native.text
        (accounting_db[0]/f'home-inbox-{stage}-wire.json').write_text(native.text)
        (accounting_db[0]/f'home-attention-{stage}-receipt.json').write_text(json.dumps({'route':'/api/operator/continuation',
            'status':result.status_code,'principal':owner['principal_id'],'root':owner['session_id'],'cursor':result.headers.get('x-continuation-cursor')}))
    try:
        await test_actual_http_goal_watch_native_cited_inbox(accounting_db,real_auth,monkeypatch,'completed',home_observer=capture)
    finally: hm.home_projection.stop()


async def test_actual_mail_notice_producer_current_auth_home_wire(accounting_db,monkeypatch,forbid_external_inference):
    from tests import test_mail_reply_watch as mail_fixture
    from src.scheduler import scheduled_jobs
    from src.integrations.gmail_read import GoogleGmailReadonlyAdapter,GMAIL_READONLY_SCOPE
    from src.vault.repository import vault_repository
    import httpx
    client,operator=await home_setup(accounting_db,monkeypatch)
    monkeypatch.setattr(scheduled_jobs,'get_session',accounting_db[2].accounting_sessions)
    monkeypatch.setattr(mail_fixture,'OWNER',operator.principal.principal_id)
    monkeypatch.setattr(mail_fixture,'SESSION',operator.session_id)
    # Existing adapter resolves this legacy vault key without an owner argument.
    await vault_repository.store('mail-watch-secret',json.dumps({'client_id':'isolated_mail_client','refresh_token':'isolated_mail_refresh'}))
    calls=[]
    def leaf_adapter(rounds,*args,**kwargs):
        async def provider(request):
            calls.append(str(request.url))
            if request.url.host=='oauth2.googleapis.com':
                payload={'access_token':'isolated_mail_access','scope':GMAIL_READONLY_SCOPE}
            elif request.url.path.endswith('/messages'):
                payload={'messages':[{'id':value} for value in (('m1',) if rounds.round==0 else ('m1','m2','m3','m4'))]}
            else:
                assert request.url.params.get('format')=='metadata'
                identifier=request.url.path.split('/')[-1]
                payload={'id':identifier,'threadId':'thread-'+identifier,'internalDate':str(int(datetime.now(timezone.utc).timestamp()*1000)),
                    'labelIds':['INBOX','UNREAD'],'historyId':'h-'+identifier,'snippet':'private preview',
                    'payload':{'headers':[{'name':'Subject','value':'private subject'}]}}
            return httpx.Response(200,headers={'content-type':'application/json'},stream=httpx.ByteStream(json.dumps(payload).encode()))
        return GoogleGmailReadonlyAdapter(*args,**kwargs,transport=httpx.MockTransport(provider),resolver=lambda host,port:['93.184.216.34'])
    async def capture(notices):
        async with client:
            response=await client.get('/api/operator/continuation');assert response.status_code==200,response.text
            rows=[r for r in response.json()['task_next_actions']['items'] if r['kind']=='inbox_decision']
            assert len(rows)==3 and {r['inbox_id'] for r in rows}=={r.id for r in notices}
            assert all(r['source_kind']=='mail_notice' and r['source_availability']=='present' for r in rows)
            assert all(r['title']=='New message in watched mailbox' for r in rows)
            assert 'private subject' not in response.text and 'private preview' not in response.text
            (accounting_db[0]/'home-attention-mail-wire.json').write_text(response.text)
            (accounting_db[0]/'home-attention-mail-receipt.json').write_text(json.dumps({'route':'/api/operator/continuation','status':200,
                'root':operator.session_id,'principal':operator.principal.principal_id,'producer':'actual governed Mail metadata scan and real Gmail adapter; existing connection/consent input fixture; only final HTTP/DNS scripted','provider_requests':len(calls)}))
            first=await client.get('/api/operator/continuation',params={'limit':1})
            cursor=first.headers['x-continuation-cursor']
            from src.db.models import GoogleServiceConnection
            async with accounting_db[2].accounting_sessions() as db:
                connection=await db.get(GoogleServiceConnection,mail_fixture.CONNECTION);connection.revision+=1;db.add(connection)
            stale=await client.get('/api/operator/continuation',params={'limit':1,'cursor':cursor})
            assert stale.status_code==409 and stale.json()['detail']['code']=='continuation_stale'
    try:
        await mail_fixture.test_watch_baseline_restart_deduplicates_metadata_notice(accounting_db[2].accounting_sessions,
            monkeypatch,None,existing_operator=operator,home_observer=capture,leaf_adapter=leaf_adapter)
    finally:hm.home_projection.stop()


async def _mechanics(accounting_db,monkeypatch,count=22):
    client,op=await home_setup(accounting_db,monkeypatch)
    goal=await GoalRepository().create('Readable bounded Goal',owner_principal_id=op.principal.principal_id,owner_session_id=op.session_id)
    now=datetime.now(timezone.utc)
    async with accounting_db[2].accounting_sessions() as db:
        for i in range(count):
            db.add(GuardianInboxDisposition(id=f'decision-{i:03}',owner_principal_id=op.principal.principal_id,
                owner_session_id=op.session_id,source_id=f'missing-{i}',goal_id=goal.id,watch_id=f'missing-watch-{i}',
                state='snoozed' if i%2 else 'pending',snoozed_until=now+timedelta(hours=1) if i%2 else None,
                expires_at=now+timedelta(hours=2),created_at=now-timedelta(minutes=1),updated_at=now-timedelta(minutes=1)))
    return client,op,goal


async def test_missing_source_mechanics_paging_anchor_graph_and_privacy(accounting_db,monkeypatch,forbid_external_inference):
    client,op,goal=await _mechanics(accounting_db,monkeypatch)
    try:
        async with client:
            first=await client.get('/api/operator/continuation');assert first.status_code==200,first.text
            b=first.json();rows=b['task_next_actions']['items'];assert len(rows)==20
            assert all(r['source_availability']=='unavailable' for r in rows)
            assert len({r['target']['inbox_id'] for r in rows})==20
            cursor=first.headers['x-continuation-cursor']
            second=await client.get('/api/operator/continuation',params={'cursor':cursor});assert second.status_code==200,second.text
            assert second.json()['as_of']==b['as_of']
            anchor=rows[-1]
            async with accounting_db[2].accounting_sessions() as db:
                d=await db.get(GuardianInboxDisposition,anchor['inbox_id'])
                db.add(GuardianDecisionPacket(id=d.source_id,source_watch_id=d.watch_id,watch_id=d.watch_id,
                    goal_id=goal.id,run_identity='mechanics-only-unavailable',proposal_text='PRIVATE_PACKET_SENTINEL'*10000,
                    created_at=datetime.now(timezone.utc)-timedelta(hours=1)))
            stale=await client.get('/api/operator/continuation',params={'cursor':cursor})
            assert stale.status_code==409 and stale.json()['detail']['code']=='continuation_stale'
            refreshed=await client.get('/api/operator/continuation');assert 'PRIVATE_PACKET_SENTINEL' not in refreshed.text
            async with accounting_db[2].accounting_sessions() as db:
                g=await db.get(Goal,goal.id);g.title='SENSITIVE_OVERSIZED_TITLE'*50000;db.add(g)
            result=await client.get('/api/operator/continuation',params={'limit':1})
            # Small page stays one aggregate item; title is admitted independently only when selected.
            assert sum(len(result.json()[k]['items']) for k in hm.SECTIONS)==1
    finally:hm.home_projection.stop()


@pytest.mark.parametrize('mutation',['foreign_root','foreign_goal','expired','malformed_id','unknown_kind'])
async def test_inbox_metadata_negatives(accounting_db,monkeypatch,forbid_external_inference,mutation):
    client,op,goal=await _mechanics(accounting_db,monkeypatch,count=1)
    async with accounting_db[2].accounting_sessions() as db:
        d=await db.get(GuardianInboxDisposition,'decision-000')
        if mutation=='foreign_root':d.owner_session_id='foreign-root'
        elif mutation=='expired':d.expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)
        elif mutation=='malformed_id':d.id='x'*513
        elif mutation=='unknown_kind':d.source_kind='invented_source_kind'
        elif mutation=='foreign_goal':
            db.add(GuardianDecisionPacket(id=d.source_id,source_watch_id=d.watch_id,watch_id=d.watch_id,
                goal_id='different-goal',run_identity='negative-only'))
        db.add(d)
    try:
        async with client:
            r=await client.get('/api/operator/continuation');assert r.status_code==200,r.text
            assert not r.json()['task_next_actions']['items']
            assert r.json()['task_next_actions']['state']==('blocked' if mutation=='malformed_id' else 'empty')
    finally:hm.home_projection.stop()


async def test_queued_nonanchor_current_membership_and_exact_anchor_revision(accounting_db,monkeypatch,forbid_external_inference):
    client,op,goal=await _mechanics(accounting_db,monkeypatch,count=1)
    now=datetime.now(timezone.utc)
    async with accounting_db[2].accounting_sessions() as db:
        db.add(GuardianOpportunity(id='metadata-mechanics-opportunity',owner_principal_id=op.principal.principal_id,
            original_root_id=op.session_id,goal_id=goal.id,goal_revision=1,policy_revision=1,
            watch_id='mechanics-watch',watch_revision=1,source_packet_id='mechanics-packet',source_digest='a'*64,
            source_token_json='PRIVATE_TOKEN_JSON_SENTINEL',dedupe_key='mechanics-only',status='queued',
            expires_at=now+timedelta(hours=3),assessment_deadline_at=now+timedelta(minutes=5),created_at=now-timedelta(minutes=1)))
        db.add(GuardianInboxDisposition(id='opportunity-decision',owner_principal_id=op.principal.principal_id,
            owner_session_id=op.session_id,source_kind='guardian_opportunity',source_id='metadata-mechanics-opportunity',
            goal_id=goal.id,watch_id='mechanics-watch',expires_at=now+timedelta(hours=3),created_at=now-timedelta(minutes=1),updated_at=now-timedelta(minutes=1)))
    try:
        async with client:
            first=await client.get('/api/operator/continuation',params={'limit':1});assert first.status_code==200,first.text
            cursor=first.headers['x-continuation-cursor'];asof=first.json()['as_of']
            assert first.json()['task_next_actions']['items'][0]['source_kind']=='source_packet'
            async with accounting_db[2].accounting_sessions() as db:
                o=await db.get(GuardianOpportunity,'metadata-mechanics-opportunity');o.status='proposed';o.revision+=1;db.add(o)
            page=await client.get('/api/operator/continuation',params={'limit':1,'cursor':cursor})
            assert page.status_code==200,page.text
            item=page.json()['task_next_actions']['items'][0]
            assert item['source_kind']=='guardian_opportunity' and item['state']=='pending' and page.json()['as_of']==asof
            assert 'PRIVATE_TOKEN_JSON_SENTINEL' not in page.text
            original=page.headers['x-continuation-cursor']
            async with accounting_db[2].accounting_sessions() as db:
                o=await db.get(GuardianOpportunity,'metadata-mechanics-opportunity');o.revision+=1;db.add(o)
            stale=await client.get('/api/operator/continuation',params={'limit':1,'cursor':original})
            assert stale.status_code==409 and stale.json()['detail']['code']=='continuation_stale'
    finally:hm.home_projection.stop()
