"""Real SQLite/browser runner race with declared browser and HTTP fixtures.

Canonical source/Board setup is seeded, not an authenticated operator proof.
Native admission/checkpoints/cleanup/Unknown and source corrections use real
repositories. The browser facade and public transport are intercepted.
"""
import json

import pytest
from sqlalchemy import select

from config.settings import settings
from src.browser.pinned_transport import PinnedBrowserResponse, PinnedBrowserTransport
from src.browser.task_runner import BrowserTaskRunner
from src.api.work_board import _browser_execution_progress
from src.db.models import Memory, WorkBoardAttempt, WorkBoardEvent, WorkBoardEvidenceDependency, WorkBoardStatus
from src.memory.evidence_dependencies import canonical_source_token, digest
from src.work_board.repository import WorkBoardRepository
from src.workflows.job_runtime import DurableJobRepository
from tests.test_browser_task_runtime import FakeBrowser, FakePage, FakeRequest, FakeRoute, _ARTIFACT_DIGEST, _input, _policy
from tests.test_evidence_dependency_tokens import canonical_fact


@pytest.mark.asyncio
@pytest.mark.parametrize('async_db',['file'],indirect=True)
@pytest.mark.parametrize('correction_at',['post_response','next_subrequest'])
async def test_past_contact_stale_receipt_cleanup_unknown_and_no_next_contact(async_db,tmp_path,monkeypatch,correction_at):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(settings,'workspace_dir',str(tmp_path))
    async with async_db() as db:
        owner,task,memory,_=await canonical_fact(db)
        task.status=WorkBoardStatus.running
        task.input_artifact_id='art-browser-race'
        task_id,memory_id=task.task_id,memory.id
        attempt_id='actual-browser-race-attempt'
        db.add(WorkBoardAttempt(attempt_id=attempt_id,task_id=task_id,fencing_token=1))
        token=await canonical_source_token(db,owner,task,'canonical_memory',memory.id)
        db.add(WorkBoardEvidenceDependency(task_id=task_id,owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,goal_id=task.goal_id,source_kind='canonical_memory',
            canonical_source_id=memory_id,source_id='c'*64,source_digest=digest(memory.content.encode()),
            span_digest='d'*64,resolved_token_json=json.dumps(token),packet_revision=1,
            packet_digest='b'*64,binding_task_revision=task.task_revision,executor_input_digest=task.typed_input_digest))
        await db.commit()
        goal_id,goal_revision=task.goal_id,task.goal_revision
    async def correct():
        async with async_db() as db:
            (await db.get(Memory,memory_id)).content='Canonical correction after the first actual admitted transport'
            await db.commit()
    async def current(**binding):
        # Real canonical Board/attempt/Goal fence for this narrow seeded test.
        # Full auth/input-artifact preflight is owned by the managed journey.
        async with async_db() as db:
            row=await WorkBoardRepository().get_task(db,owner,task_id)
            attempt=await db.get(WorkBoardAttempt,attempt_id)
            await WorkBoardRepository().validate_task_goal(db,owner,row)
            return (row.status==WorkBoardStatus.running and row.task_revision==binding['board_task_revision']
                and attempt is not None and attempt.fencing_token==binding['board_fencing_token']
                and attempt.ended_at is None and not attempt.cancel_requested_at)
    calls=[]
    responses={url:PinnedBrowserResponse(200,{'content-type':'text/html'},b'public fixture',url,'93.184.216.34')
        for url in ['https://fixture.example/docs','https://fixture.example/reference','https://fixture.example/docs/asset']}
    async def fixture(request):
        calls.append(request.url)
        if correction_at=='post_response':await correct()
        return responses[request.url]
    class RacePage(FakePage):
        async def goto(self,url,**kwargs):
            result=await super().goto(url,**kwargs)
            if correction_at=='next_subrequest':
                await correct()
                route=FakeRoute()
                await self.route_handler(route,FakeRequest('https://fixture.example/docs/asset',resource_type='stylesheet'))
                assert route.aborted
            return result
    browser=FakeBrowser(responses);browser.context.page=RacePage(responses)
    jobs=DurableJobRepository()
    runner=BrowserTaskRunner(jobs=jobs,browser_launcher=lambda:browser,runtime_controls=current,
        transport_factory=lambda:PinnedBrowserTransport(resolver=lambda *_:['93.184.216.34'],
            injected_fetch=fixture,site_policy=_policy),workspace_root=tmp_path)
    admitted=await runner.run(task_id=task_id,attempt_id=attempt_id,
        owner_principal_id=owner.principal_id,owner_session_id=owner.session_id,
        goal_id=goal_id,goal_revision=goal_revision,board_task_revision=1,board_fencing_token=1,
        input_artifact_id='art-browser-race',input_artifact_digest=_ARTIFACT_DIGEST,
        inputs=_input(),runtime_seconds=180,admission_only=True,task_priority=50,
        effective_max_attempts=1,effective_max_outstanding_jobs=1)
    assert admitted['status']=='admitted',admitted
    result=await runner.run(task_id=task_id,attempt_id=attempt_id,
        owner_principal_id=owner.principal_id,owner_session_id=owner.session_id,
        goal_id=goal_id,goal_revision=goal_revision,board_task_revision=1,board_fencing_token=1,
        input_artifact_id='art-browser-race',input_artifact_digest=_ARTIFACT_DIGEST,
        inputs=_input(),runtime_seconds=180,admission_only=False,task_priority=50,
        admission_board_task_revision=1,effective_max_attempts=1,effective_max_outstanding_jobs=1)
    assert calls==['https://fixture.example/docs']
    assert result['status']=='unknown_external_effect' and browser.closed
    assert result['request_count']==1 and len(result['request_receipts'])==1
    assert result['request_receipts'][0]['status']==200
    native=await jobs.get_job(admitted['job_id'])
    assert native['status']=='unknown_external_effect' and native['attempt_count']==1
    markers=[c for c in native['checkpoints'] if c['checkpoint_id']=='network-dispatch']
    assert len(markers)==1 and markers[0]['payload']['request_dispatch_count']==1
    assert _browser_execution_progress(native)[1]==1
    assert any(e.get('effect_type')=='browser_context_cleanup' and e['status']=='succeeded' for e in native['effects'])
    observations=[e for e in native['effects'] if e.get('effect_type')=='browser_network_observation']
    if correction_at=='post_response':
        assert observations and observations[0]['details']['observation_only'] is True
        assert observations[0]['details']['request_receipt']['status']==200
        assert observations[0]['details']['request_dispatch_count']==1
    assert len(observations)<=32
    assert not native['artifacts']
    async with async_db() as db:
        assert (await db.scalar(select(WorkBoardEvent).where(
            WorkBoardEvent.kind=='task.evidence.execution_stale'))) is not None
        assert (await WorkBoardRepository().get_task(db,owner,task_id)).status==WorkBoardStatus.running
        assert (await db.get(WorkBoardAttempt,attempt_id)).ended_at is None
    with (tmp_path/'actual-browser-race-result.json').open('x') as f:
        json.dump({'boundary':'Real native SQLite runner; seeded canonical source/Board; browser facade and HTTP intercepted',
            'correction_at':correction_at,'actual_transport_urls':calls,'browser_closed':browser.closed,
            'result':result,'native':native,'no_learning':True},f,indent=2)


def test_primary_progress_observation_excludes_blocked_callbacks_and_possible_dispatch():
    job='browser-task:current'
    projection={'job_id':job,'checkpoints':[{'checkpoint_id':'network-dispatch',
        'payload':{'request_count':0,'request_dispatch_count':2,'action_index':0}}],
        'effects':[{'effect_id':f'browser-network-observation:{job}:1',
            'effect_type':'browser_network_observation','status':'succeeded',
            'details':{'observation_only':True,'request_count':1,'request_receipt':{'status':200}}},
            {'effect_id':f'browser-network-observation:{job}:2',
            'effect_type':'browser_network_observation','status':'succeeded',
            'details':{'observation_only':True,'request_count':2,'request_receipt':{'status':'blocked'}}}]}
    assert _browser_execution_progress(projection)==(0,1)
    projection['effects']=projection['effects'][1:]
    assert _browser_execution_progress(projection)==(0,0)
