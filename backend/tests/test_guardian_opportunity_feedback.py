"""M4 feedback SQL mechanics; source prerequisites isolated, no seeded native success."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine
from pydantic import ValidationError

from src.db.models import GuardianOpportunity, GuardianIntervention, OperatorSession, AuditEvent, WorkBoardProposal
from src.guardian import feedback
from src.guardian.opportunity_contracts import OpportunityFeedbackRequest, OpportunityRecommendationRequest, OpportunityError
from src.guardian.opportunity_plans import PlanSourceWitness
from src.auth.service import _operator_for_record


async def setup_feedback(async_db, monkeypatch):
    now = datetime.now(timezone.utc)
    principal = 'operator:root:feedback-owner'
    async with async_db() as db:
        root = OperatorSession(id='root', principal_id=principal, token_hash='captured-http-hash',
            idle_expires_at=now+timedelta(hours=1), absolute_expires_at=now+timedelta(hours=2))
        db.add(root)
        db.add(GuardianOpportunity(id='opportunity', owner_principal_id=principal, original_root_id='root',
            goal_id='goal', goal_revision=1, policy_revision=1, watch_id='watch', watch_revision=1,
            source_packet_id='packet', source_digest='a'*64, source_token_json='{}', dedupe_key='dedupe',
            status='proposed', revision=2, intervention_id='intervention', expires_at=now+timedelta(minutes=40),
            assessment_deadline_at=now))
        db.add(GuardianIntervention(id='intervention', intervention_type='opportunity', opportunity_id='opportunity',
            owner_principal_id=principal, original_root_id='root', goal_id='goal', goal_revision=1,delivery_status='not_requested'))
        await db.commit()
    monkeypatch.setattr(feedback, 'get_session', async_db)
    from src.guardian import opportunity_plans as plans
    async def staged(db, row, **kwargs):
        return PlanSourceWitness(row.id,row.revision,row.owner_principal_id,row.original_root_id,row.goal_id,row.goal_revision,
            row.policy_revision,row.watch_id,row.watch_revision,row.source_packet_id,row.source_digest,
            'private-snapshot',b'{}',b'{}','public','b'*64,'https://example.com/public',b'{}')
    async def pure_recheck(db, row, *, source_witness, **kwargs):
        assert row.source_digest == source_witness.source_digest
    monkeypatch.setattr(plans, 'stage_plan_source', staged)
    monkeypatch.setattr(plans, 'recheck_plan_source', pure_recheck)
    return _operator_for_record(root,token_hash=root.token_hash)


def request(revision=0, *, label='not_helpful', reason='', key=None):
    return OpportunityFeedbackRequest(expected_feedback_revision=revision,feedback_type=label,reason=reason,idempotency_key=key or uuid4())


async def test_append_correction_exact_original_replay_and_summary(async_db, monkeypatch):
    operator = await setup_feedback(async_db,monkeypatch)
    original = request(reason='Unwanted notification')
    first = await feedback.record_opportunity_feedback(operator=operator,opportunity_id='opportunity',request=original)
    correction = await feedback.record_opportunity_feedback(operator=operator,opportunity_id='opportunity',request=request(1))
    replay = await feedback.record_opportunity_feedback(operator=operator,opportunity_id='opportunity',request=original)
    assert replay.model_dump(exclude={'idempotent_replay'}) == first.model_dump(exclude={'idempotent_replay'})
    assert replay.idempotent_replay and correction.feedback_revision==2
    async with async_db() as db:
        row = await db.get(GuardianIntervention,'intervention')
        history = feedback.parse_opportunity_feedback_history(row)
        assert history.revision==2 and json.loads(history.events[0])['reason']=='Unwanted notification'
        assert row.feedback_note=='' and row.delivery_status=='not_requested' and row.latest_outcome=='created'
        summary = await feedback.opportunity_feedback_summary(db,await db.get(GuardianOpportunity,'opportunity'))
        assert summary['feedback_revision']==2 and summary['event_count']==2 and summary['feedback_type']=='not_helpful'
        assert await db.scalar(select(func.count()).select_from(AuditEvent))==2


async def test_changed_uuid_body_and_stale_revision_no_append(async_db,monkeypatch):
    operator=await setup_feedback(async_db,monkeypatch)
    body=request()
    await feedback.record_opportunity_feedback(operator=operator,opportunity_id='opportunity',request=body)
    for candidate,code in ((body.model_copy(update={'reason':'different'}),'feedback_idempotency_conflict'),(request(),'feedback_revision_stale')):
        with pytest.raises(OpportunityError,match=code):
            await feedback.record_opportunity_feedback(operator=operator,opportunity_id='opportunity',request=candidate)
    async with async_db() as db:
        assert (await db.get(GuardianIntervention,'intervention')).feedback_revision==1
        assert await db.scalar(select(func.count()).select_from(AuditEvent))==1


@pytest.mark.parametrize('same_uuid',[True,False])
@pytest.mark.parametrize('async_db', ['file'], indirect=True)
async def test_concurrent_uuid_replay_and_distinct_uuid_cas(async_db,monkeypatch,same_uuid):
    operator=await setup_feedback(async_db,monkeypatch)
    original_stage=feedback.stage_opportunity_feedback_source
    ready=asyncio.Event(); staged=0
    async def both_stage(*args,**kwargs):
        nonlocal staged
        witness=await original_stage(*args,**kwargs)
        staged+=1
        if staged==2: ready.set()
        await asyncio.wait_for(ready.wait(),5)
        return witness
    monkeypatch.setattr(feedback,'stage_opportunity_feedback_source',both_stage)
    first=request(); second=first if same_uuid else request()
    results=await asyncio.gather(*(feedback.record_opportunity_feedback(operator=operator,opportunity_id='opportunity',request=r)
        for r in (first,second)),return_exceptions=True)
    if same_uuid:
        assert all(not isinstance(r,Exception) for r in results), results
        assert sorted(r.idempotent_replay for r in results)==[False,True]
        assert results[0].feedback_event_digest==results[1].feedback_event_digest
    else:
        assert sum(isinstance(r,OpportunityError) for r in results)==1, results
        assert next(r.code for r in results if isinstance(r,OpportunityError))=='feedback_revision_stale'
    async with async_db() as db:
        assert (await db.get(GuardianIntervention,'intervention')).feedback_revision==1
        assert await db.scalar(select(func.count()).select_from(AuditEvent))==1


@pytest.mark.parametrize('fault',['helpful_no_execution','cross_owner','no_intervention','silent','legacy_bypass','root_rotation'])
async def test_authority_and_outcome_fail_closed_without_feedback(async_db,monkeypatch,fault):
    operator=await setup_feedback(async_db,monkeypatch)
    if fault=='root_rotation':
        stage=feedback.stage_opportunity_feedback_source
        async def race(*args,**kwargs):
            proof=await stage(*args,**kwargs)
            async with async_db() as db:
                (await db.get(OperatorSession,'root')).token_hash='rotated-http-hash'
                await db.commit()
            return proof
        monkeypatch.setattr(feedback,'stage_opportunity_feedback_source',race)
    if fault in {'cross_owner','no_intervention','silent'}:
        async with async_db() as db:
            row=await db.get(GuardianOpportunity,'opportunity')
            if fault=='cross_owner': row.owner_principal_id='foreign-owner'
            if fault=='no_intervention': row.intervention_id=None
            if fault=='silent': row.status='silent'
            await db.commit()
    from src.work_board.repository import BoardError
    with pytest.raises((OpportunityError,BoardError)):
        if fault=='legacy_bypass':
            await feedback.guardian_feedback_repository.record_feedback('intervention',feedback_type='helpful',
                owner_principal_id=operator.principal.principal_id,original_root_id=operator.session_id)
        else:
            await feedback.record_opportunity_feedback(operator=operator,opportunity_id='opportunity',
                request=request(label='helpful' if fault=='helpful_no_execution' else 'not_helpful'))
    async with async_db() as db:
        assert (await db.get(GuardianIntervention,'intervention')).feedback_revision==0
        assert await db.scalar(select(func.count()).select_from(AuditEvent))==0


@pytest.mark.parametrize('fault',['event_digest','tip_projection','revision','duplicate_uuid','overflow_events','overflow_utf8'])
async def test_malformed_or_bounded_history_never_silently_truncates(async_db,monkeypatch,fault):
    operator=await setup_feedback(async_db,monkeypatch)
    await feedback.record_opportunity_feedback(operator=operator,opportunity_id='opportunity',request=request())
    async with async_db() as db:
        row=await db.get(GuardianIntervention,'intervention')
        events=json.loads(row.feedback_history_json)
        if fault=='event_digest': events[0]['event_digest']='0'*64
        if fault=='tip_projection': row.feedback_type='helpful'
        if fault=='revision': row.feedback_revision=2
        if fault=='duplicate_uuid': events.append(events[0])
        if fault=='overflow_events': events=events*101
        if fault=='overflow_utf8': events[0]['reason']='é'*20000
        row.feedback_history_json=json.dumps(events,ensure_ascii=False)
        with pytest.raises(OpportunityError,match='learning_population_incomplete'):
            feedback.parse_opportunity_feedback_history(row)


@pytest.mark.parametrize('revision',[-1,True,'0',0.5])
def test_strict_revisions_and_exact_uuid(revision):
    with pytest.raises(ValidationError): request(revision)
    with pytest.raises(ValidationError): OpportunityRecommendationRequest(expected_opportunity_revision=1,expected_feedback_revision=revision,idempotency_key=uuid4())
    with pytest.raises(ValidationError): request(key=uuid4().hex)


def test_empty_feedback_reason_and_revision_zero_recommendation_are_valid():
    assert request().reason==''
    assert OpportunityRecommendationRequest(expected_opportunity_revision=1,expected_feedback_revision=0,idempotency_key=uuid4()).expected_feedback_revision==0


async def test_populated_previous_schema_migration_repeat_is_additive(tmp_path):
    from src.db.engine import _ensure_legacy_columns
    engine=create_async_engine(f'sqlite+aiosqlite:///{tmp_path}/previous.db')
    try:
        async with engine.begin() as db:
            await db.exec_driver_sql('CREATE TABLE guardian_interventions (id VARCHAR PRIMARY KEY, intervention_type VARCHAR, owner_principal_id VARCHAR, original_root_id VARCHAR, goal_id VARCHAR, goal_revision INTEGER, feedback_at DATETIME)')
            await db.exec_driver_sql("INSERT INTO guardian_interventions (id,intervention_type) VALUES ('legacy','advisory')")
            await _ensure_legacy_columns(db)
            await _ensure_legacy_columns(db)
            row=(await db.exec_driver_sql("SELECT id,intervention_type,feedback_revision,feedback_history_json,outcome_binding_json,opportunity_id FROM guardian_interventions")).one()
            assert tuple(row)==('legacy','advisory',0,None,None,None)
            assert [r[2] for r in (await db.exec_driver_sql('PRAGMA index_info(ix_guardian_interventions_opportunity_feedback)')).all()]==[
                'intervention_type','owner_principal_id','original_root_id','goal_id','goal_revision','feedback_at','id']
    finally:
        await engine.dispose()

# Effect wiring tests isolate the specialized memory proof boundary. Actual
# adopted native CPU/source custody is exercised by the owned vertical suite.
from tests.test_work_board_m6_provider_free_journey import isolated_runtime, SESSION

@pytest.mark.parametrize('preference_state', ['active', 'invalid', 'rolled_back', 'key_missing'])
async def test_optional_publication_effect_rechecks_before_any_candidate_write(isolated_runtime, monkeypatch, preference_state):
    from tests.test_guardian_opportunity_policy import publish_source
    from src.guardian import opportunity_preferences as preferences
    from src.guardian.opportunities import publish_verified_packet
    from src.guardian.opportunity_contracts import VerifiedSourcePacket
    from src.guardian.source_watch import SourceWatchService
    from src.db.models import GuardianDecisionPacket, WorkBoardTask
    from src.work_board.repository import BoardError
    sessions, goal, watch, _, original, packet = await publish_source(isolated_runtime)
    witnessed = object()
    phases = []
    async def stage(db, **scope):
        phases.append('stage')
        assert db._session.connection().connection.driver_connection.in_transaction is False
        assert scope['watch_id'] == watch['id'] and scope['watch_revision'] == 1
        assert scope['goal_id'] == goal.id and scope['goal_revision'] == 1
        if preference_state == 'key_missing':
            from src.extensions.capability_execution import CapabilityJournalError
            raise CapabilityJournalError('Server key unavailable')
        if preference_state == 'invalid':
            raise BoardError('feedback_outcome_stale', 'No current proof')
        return witnessed
    async def recheck(db, *, witness):
        phases.append('recheck')
        assert witness is witnessed and db._session.connection().connection.driver_connection.in_transaction is True
        if preference_state == 'rolled_back':
            raise BoardError('opportunity_review_stale', 'Preference rolled back')
        return {'status':'active', 'scope':{'action':'suppress_watch'}, 'proposal_id':'reviewed-preference'}
    monkeypatch.setattr(preferences, 'stage_preference_use', stage)
    monkeypatch.setattr(preferences, 'recheck_preference_use', recheck)
    # Existing historical semantic lineage is never hidden by suppression.
    assert await publish_verified_packet(VerifiedSourcePacket(packet_id=packet.id, watch_revision=1,
        goal_revision=1)) == original.id
    phases.clear()
    async def fetch(source):
        return 'Stable public line\nA third independent relevant release\n', {'content_type':'text/plain'}
    result = await SourceWatchService(fetcher=fetch).run_watch(watch['id'], occurrence_id='feedback-effect-third',
        expected_plan_revision=1, expected_owner_session_id=SESSION)
    assert result['status'] == 'succeeded'  # Watch read/publication still occurs.
    assert phases == (['stage'] if preference_state in {'invalid','key_missing'} else ['stage','recheck'])
    async with sessions() as db:
        assert await db.get(GuardianOpportunity, original.id) is not None
        candidates = await db.scalar(select(func.count()).select_from(GuardianOpportunity))
        receipts = list((await db.execute(select(AuditEvent).where(
            AuditEvent.event_type=='guardian_opportunity_preference_suppressed'))).scalars())
        assert candidates == (1 if preference_state=='active' else 2)
        assert len(receipts) == (1 if preference_state=='active' else 0)
        assert await db.scalar(select(func.count()).select_from(WorkBoardTask)) == 0
        if receipts:
            assert set(json.loads(receipts[0].details_json)) == {'reason_code','source_packet_id','watch_id',
                'watch_revision','goal_id','goal_revision','proposal_id'}


async def test_http_offer_preference_only_orders_current_available_blueprints(async_db, monkeypatch):
    from types import SimpleNamespace
    from src.guardian import opportunity_plans as plans, opportunity_preferences as preferences
    row = SimpleNamespace(status='proposed', proposal_id=None, goal_id='goal', goal_revision=1,
        owner_principal_id='owner')
    async def stage(*args, **kwargs): pass
    async def authority(*args): return None,None,None,SimpleNamespace(max_plan_proposals_per_utc_day=2),None
    async def count(*args): return 2
    calls=[]
    async def current(operator, **scope):
        calls.append((operator,scope))
        return {'status':'active','scope':{'action':'prefer_blueprint','blueprint_id':'public-evidence-report'}}
    monkeypatch.setattr(plans,'stage_plan_source',stage)
    monkeypatch.setattr(plans,'assert_opportunity_current',authority)
    monkeypatch.setattr(plans,'contacted_plan_count',count)
    monkeypatch.setattr(preferences,'current_preference',current)
    operator=object()
    async with async_db() as db:
        default=await plans.get_plan_offer(db,row)
        ordered=await plans.get_plan_offer(db,row,operator=operator)
    assert default['available_blueprint_ids']==['public-browser-check','public-evidence-report']
    assert ordered['available_blueprint_ids']==['public-evidence-report','public-browser-check']
    assert {k:v for k,v in default.items() if k!='available_blueprint_ids'} == {
        k:v for k,v in ordered.items() if k!='available_blueprint_ids'}
    assert ordered['can_generate'] is False and ordered['generation_block_reason']=='opportunity_plan_daily_limit'
    assert calls==[(operator,{'goal_id':'goal','goal_revision':1,'action':'prefer_blueprint'})]


@pytest.mark.parametrize('change',['native_id','content_digest','readback_id','verified_at','missing_parent'])
def test_report_feedback_handoff_matches_actual_staged_parent_readback(change):
    from types import SimpleNamespace
    proof={'receipt_kind':'readback','status':'verified','workflow_run_id':'native-parent',
        'content_sha256':'a'*64,'readback_id':'readback-parent','verified_at':'2026-10-06T10:00:00+00:00'}
    parent=SimpleNamespace(task_id='parent',proof_bytes=json.dumps(proof).encode())
    handoff={'parent_task_id':'parent','verification_json':json.dumps(proof)}
    inventory=(('handoff','edge',json.dumps(handoff).encode()),)
    feedback._validate_feedback_handoffs(inventory,(parent,))
    if change=='missing_parent':
        outcomes=()
    else:
        altered=dict(proof)
        field={'native_id':'workflow_run_id','content_digest':'content_sha256','readback_id':'readback_id',
            'verified_at':'verified_at'}[change]
        altered[field]='b'*64 if change=='content_digest' else 'different'
        handoff['verification_json']=json.dumps(altered)
        inventory=(('handoff','edge',json.dumps(handoff).encode()),)
        outcomes=(parent,)
    with pytest.raises(OpportunityError,match='feedback_outcome_stale'):
        feedback._validate_feedback_handoffs(inventory,outcomes)


from tests.test_research_native_vertical import real_auth

@pytest.mark.parametrize('method',['POST','GET'])
@pytest.mark.parametrize('status,code',[(403,'original_root_unavailable'),(409,'opportunity_source_stale'),
    (422,'invalid_recommendation_request'),(503,'native_dependency_unavailable'),
    (503,'source_baseline_integrity_unverifiable')])
async def test_recommendation_routes_map_typed_failures_without_side_effects(isolated_runtime, real_auth,
    monkeypatch, method, status, code):
    import httpx
    from fastapi import FastAPI
    from config.settings import settings
    from src.api import goals
    from src.auth.middleware import OperatorAuthMiddleware
    from src.work_board import opportunity_preference_native as native
    from src.work_board.repository import BoardError
    from src.extensions.capability_execution import CapabilityJournalError
    from src.db.models import WorkBoardTask,WorkBoardAttempt,WorkflowRunState
    sessions,_=isolated_runtime
    calls=[]
    async def fail(**kwargs):
        calls.append(kwargs)
        assert kwargs['operator'].session_id==SESSION
        if code=='source_baseline_integrity_unverifiable':
            raise CapabilityJournalError('PRIVATE signing-key cause must never appear in HTTP')
        raise BoardError(code,'PRIVATE native binding cause must never appear in HTTP',status_code=status)
    monkeypatch.setattr(native,'request_opportunity_recommendation',fail)
    monkeypatch.setattr(native,'inspect_opportunity_recommendation',fail)
    async def counts():
        async with sessions() as db:
            return tuple([await db.scalar(select(func.count()).select_from(model)) for model in (
                WorkBoardTask,WorkBoardAttempt,WorkflowRunState,GuardianIntervention,GuardianOpportunity)])
    before=await counts()
    app=FastAPI();app.add_middleware(OperatorAuthMiddleware);app.include_router(goals.router,prefix='/api')
    request_uuid=str(uuid4())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',
        headers={'origin':'http://localhost:3001'}) as client:
        client.cookies.set(settings.operator_auth_cookie_name,'m6-provider-free-root')
        path='/api/guardian/opportunities/opportunity/recommendation'
        if method=='POST':
            response=await client.post(path,json={'expected_opportunity_revision':1,
                'expected_feedback_revision':0,'idempotency_key':request_uuid})
        else:
            response=await client.get(path,params={'idempotency_key':request_uuid})
    assert response.status_code==status,response.text
    assert response.json()=={'detail':{'code':code}}
    assert len(calls)==1 and calls[0]['opportunity_id']=='opportunity'
    if method=='GET': assert calls[0]['request_uuid']==request_uuid
    else: assert calls[0]['request'].expected_feedback_revision==0
    assert await counts()==before


@pytest.mark.parametrize('change',['canonical','wrong_slots','wrong_actual_capability'])
async def test_feedback_report_uses_canonical_slot_refs_and_checks_actual_tasks(async_db, monkeypatch, change):
    from types import SimpleNamespace
    from src.work_board import pipelines
    from src.work_board.contracts import WorkBoardOwner
    from src.work_board.pipeline_contracts import SLOTS,CAPABILITIES
    from src.db.models import WorkBoardTask,WorkBoardLink
    operator=await setup_feedback(async_db,monkeypatch)
    owner=WorkBoardOwner(principal_id=operator.principal.principal_id,session_id=operator.session_id)
    ids=['browser','dossier','report']
    steps=[{'slot':slot,'task_ref':task_id} for slot,task_id in zip(SLOTS,ids)]
    assert all(set(step)=={'slot','task_ref'} for step in steps)
    if change=='wrong_slots': steps[1]['slot']='local_report'
    async def owned(db,passed_owner,operation_id,*,workspace_identity):
        assert passed_owner==owner and operation_id=='proposal' and workspace_identity==b'workspace'
        return None,{'steps':steps}
    monkeypatch.setattr(pipelines,'owned',owned)
    async with async_db() as db:
        opportunity=await db.get(GuardianOpportunity,'opportunity')
        opportunity.status='planned';opportunity.proposal_id='proposal'
        for index,(task_id,capability) in enumerate(zip(ids,CAPABILITIES)):
            db.add(WorkBoardTask(task_id=task_id,owner_principal_id=owner.principal_id,owner_session_id=owner.session_id,
                goal_id=opportunity.goal_id,goal_revision=opportunity.goal_revision,idempotency_key='key-'+task_id,
                capability_id=('arbitrary.other.v1' if change=='wrong_actual_capability' and index==1 else capability)))
        await db.flush()  # Persist real FK parents before dependent links.
        for parent,child in zip(ids,ids[1:]):
            db.add(WorkBoardLink(owner_principal_id=owner.principal_id,owner_session_id=owner.session_id,
                parent_task_id=parent,child_task_id=child))
        await db.commit()
        proposal=SimpleNamespace(status='accepted',proposal_id='proposal',parent_task_id='browser',
            proposal_json=json.dumps({'blueprint_id':'public-evidence-report'}))
        if change=='canonical':
            outcomes,inventory,blueprint=await feedback._feedback_lineage(db,owner,opportunity,proposal,
                source=SimpleNamespace(workspace_identity=b'workspace'))
            assert outcomes==()  # Triage/no attempt is no Helpful outcome.
            assert blueprint=='public-evidence-report'
            assert len([entry for entry in inventory if entry[0]=='task'])==3
            assert len([entry for entry in inventory if entry[0]=='link'])==2
        else:
            with pytest.raises(OpportunityError,match='feedback_outcome_stale'):
                await feedback._feedback_lineage(db,owner,opportunity,proposal,
                    source=SimpleNamespace(workspace_identity=b'workspace'))
