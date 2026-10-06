"""Authenticated opportunity plans through actual Chromium and native CPU jobs.

Only named public DNS/HTTPS and OpenRouter HTTP boundaries are intercepted.
No browser, policy decision, Work status, or native result is manufactured.
"""
import json
import socket
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from config.settings import settings
from src.auth.middleware import OperatorAuthMiddleware
from src.db.models import GuardianOpportunity, WorkBoardAttempt, WorkBoardProposal, WorkBoardStatus, WorkBoardTask
from src.guardian.opportunity_runtime import admit_assessment, execute_assessment
from src.work_board.dispatcher import WorkBoardDispatcher
from src.workflows.job_runtime import durable_job_repository
from tests.test_guardian_opportunity_vertical import OpportunityHttpBoundary
from tests.test_inference_accounting import accounting_db, setup_configuration
from tests.test_research_native_vertical import real_auth, ResponseBytes


class PlanHttpBoundary(OpportunityHttpBoundary):
    async def handle_async_request(self, request):
        body = json.loads(request.content)
        if len(body['messages']) != 2 or 'offered_blueprints' not in json.loads(body['messages'][1]['content']):
            return await super().handle_async_request(request)
        assert request.url.host == 'openrouter.ai'
        assert request.method == 'POST' and request.url.path == '/api/v1/chat/completions'
        assert not body.get('tools') and not body.get('stream')
        assert body['max_tokens'] <= 1024
        self.calls.append(body)
        offered = json.loads(body['messages'][1]['content'])
        source = offered['public_evidence']['sources'][0]
        content = dict(schema_version='seraph.opportunity.plan.v1', blueprint_id=self.controls['blueprint'],
            title='Review the cited public release', reason='Read the bound public source and retain a local result.',
            citations=[dict(source_id=source['source_key'], start_line=1,
                end_line=len(source['excerpt'].split('\n')), span_sha256=source['excerpt_sha256'])])
        payload = dict(id='intercepted-plan', choices=[dict(message=dict(role='assistant', content=json.dumps(content)))],
            usage=dict(cost='0.000002', prompt_tokens=10, completion_tokens=10))
        return httpx.Response(200, request=request, headers={'content-type': 'application/json'},
            stream=ResponseBytes(json.dumps(payload).encode()))


@pytest.mark.parametrize('blueprint', ['public-browser-check', 'public-evidence-report'])
async def test_authenticated_opportunity_generate_triage_accept_real_browser_cpu(accounting_db, real_auth, monkeypatch, blueprint):
    await _actual_plan_journey(accounting_db, real_auth, monkeypatch, blueprint)


async def test_actual_succeeded_generation_readback_adopted_after_local_finalizer_cut(accounting_db, real_auth, monkeypatch):
    await _actual_plan_journey(accounting_db, real_auth, monkeypatch, 'public-browser-check', finalizer_cut=True)


async def _actual_plan_journey(accounting_db, real_auth, monkeypatch, blueprint, *, finalizer_cut=False):
    from src.api import auth, goals, model_fabric_settings, work_board, approvals
    from src.browser.pinned_transport import PinnedBrowserTransport, PinnedBrowserResponse
    from src.guardian import source_watch
    from src.model_fabric import remote_inference_admission
    from src.model_fabric.configuration import write_model_fabric_configuration
    from src.security.http_transport import fetch_pinned_https
    from src.security.site_policy import evaluate_site_access
    from src.work_board.pipeline_cpu import read_output
    from src.work_board import pipelines
    from src.work_board.contracts import WorkBoardOwner

    root, _, factory = accounting_db
    original_connect = socket.socket.connect
    def local_connect(sock, address):
        if isinstance(address, tuple) and address[0] not in ('127.0.0.1', '::1', 'localhost'):
            raise AssertionError('vertical fixture denies all nonlocal network')
        return original_connect(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', local_connect)
    monkeypatch.setattr(settings, 'operator_auth_idle_seconds', 3600)
    monkeypatch.setattr(settings, 'browser_site_allowlist', 'example.com')
    assert evaluate_site_access('https://example.com/public').allowed
    assert not evaluate_site_access('https://unapproved.example/public').allowed
    monkeypatch.setattr('src.model_fabric.execution.gpu_admission_broker',
        remote_inference_admission.remote_inference_admission_broker)
    configured = setup_configuration()
    write_model_fabric_configuration(replace(model_fabric_settings._setup_configuration(
        replace(configured.openrouter_setup, timeout_seconds=30, capabilities=('text', 'structured_output')),
        profiles=(), policies=()), egress_revision=configured.egress_revision+1))
    await durable_job_repository.configure_inference_accounting(1000)
    calls, source_calls, browser_calls = [], [], []
    original_client = httpx.AsyncClient
    def clients(**kwargs):
        if kwargs.get('transport') is None:
            kwargs['transport'] = PlanHttpBoundary(calls, {'blueprint': blueprint})
        return original_client(**kwargs)
    monkeypatch.setattr(httpx, 'AsyncClient', clients)
    versions = iter(('Stable public line\nPrevious public release\n',
        'Stable public line\nA relevant new public release\n'))
    async def source_http(request):
        assert request.method == 'GET' and str(request.url) == 'https://example.com/public'
        source_calls.append(str(request.url))
        return httpx.Response(200, request=request, headers={'content-type':'text/plain'},
            stream=ResponseBytes(next(versions).encode()))
    def named_dns(host, port):
        assert host == 'example.com' and port == 443
        return ['93.184.216.34']
    async def pinned_source(url, **kwargs):
        return await fetch_pinned_https(url, resolver=named_dns, transport=httpx.MockTransport(source_http), **kwargs)
    monkeypatch.setattr(source_watch, 'fetch_pinned_https', pinned_source)
    async def public_browser_http(request):
        assert request.url == 'https://example.com/public' and request.method == 'GET'
        browser_calls.append(request.url)
        return PinnedBrowserResponse(200, {'content-type':'text/html'},
            b'<!doctype html><html><body>Stable public line A relevant new public release</body></html>',
            request.url, '93.184.216.34')
    original_transport_init = PinnedBrowserTransport.__init__
    def browser_transport(self, **kwargs):
        # Constructor seams only; production SitePolicy and default Chromium
        # launcher, routing guards and dispatcher controls remain unchanged.
        kwargs.setdefault('resolver', named_dns)
        kwargs.setdefault('injected_fetch', public_browser_http)
        original_transport_init(self, **kwargs)
    monkeypatch.setattr(PinnedBrowserTransport, '__init__', browser_transport)
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    for router, prefix in ((auth.router, '/api/auth'), (goals.router, '/api'),
            (model_fabric_settings.router, '/api'), (source_watch.source_watch_router, '/api'),
            (work_board.router, '/api'), (approvals.router, '/api')):
        app.include_router(router, prefix=prefix)
    async with original_client(transport=httpx.ASGITransport(app=app), base_url='http://test',
            headers={'origin':'http://localhost:3001'}) as client:
        assert (await client.get('/api/guardian/opportunities')).status_code == 401
        login = await client.post('/api/auth/login', json={'password':'research-vertical-private-secret'})
        assert login.status_code == 200, login.text
        operator = login.json()
        owner = WorkBoardOwner(principal_id=operator['principal_id'], session_id=operator['session_id'])
        for capability in ('text', 'structured_output', 'latency_ms', 'health'):
            response = await client.post('/api/settings/model-fabric/canary', json=dict(
                profile_id='openrouter', capability=capability, timeout_seconds=30))
            assert response.status_code == 200 and response.json()['outcome'] == 'passed', response.text
        now = datetime.now(timezone.utc)
        response = await client.post('/api/goals', json=dict(title='Review cited public release changes',
            proactive_enabled=True, admission_budget=dict(reviewed_grant=True, grant_id='plan-review',
                max_outstanding_jobs=2, max_attempts=2, max_runtime_seconds=300,
                period_started_at=now.isoformat(), period_expires_at=(now+timedelta(hours=1)).isoformat())))
        assert response.status_code == 200, response.text
        goal = response.json()
        response = await client.post('/api/capabilities/source-watches', json=dict(goal_id=goal['id'],
            expected_goal_revision=goal['revision'], sources=[dict(source_key='public', kind='public_https_text',
                target='https://example.com/public', label='Public release', priority=1)], criteria={},
            schedule=dict(cron='0 * * * *',timezone='UTC'),write_mode='standing_reviewed',reviewed_grant_id='plan-review'))
        assert response.status_code == 200, response.text
        watch = response.json()
        response = await client.put(f"/api/goals/{goal['id']}/guardian-policy", json=dict(
            expected_goal_revision=goal['revision'],expected_policy_revision=0,idempotency_key=str(uuid4()),
            policy=dict(schema_version='seraph.guardian.policy.v1',assessment_enabled=True,auto_stage_plan=False,
                confirmed_at=now.isoformat(),review_due_at=(now+timedelta(hours=1)).isoformat(),
                grant_id='plan-review',original_root_id=operator['session_id'],goal_revision=goal['revision'],
                source_watch_ids=[watch['id']],max_assessments_per_utc_day=2,max_plan_proposals_per_utc_day=2)))
        assert response.status_code == 200, response.text
        service = source_watch.SourceWatchService()
        for occurrence, expected in (('plan-baseline','baseline_initialized'),('plan-material','succeeded')):
            receipt = await service.run_watch(watch['id'],occurrence_id=occurrence,
                expected_plan_revision=1,expected_owner_session_id=operator['session_id'])
            assert receipt['status'] == expected, receipt
        async with factory.accounting_sessions() as db:
            opportunity = (await db.scalars(select(GuardianOpportunity))).one()
        await admit_assessment(opportunity.id)
        await execute_assessment(opportunity.id)
        async with factory.accounting_sessions() as db:
            opportunity = await db.get(GuardianOpportunity,opportunity.id)
            assert opportunity.status == 'proposed'
            request = dict(expected_opportunity_revision=opportunity.revision,
                expected_goal_revision=opportunity.goal_revision,idempotency_key=str(uuid4()))
        before = len(calls)
        if finalizer_cut:
            from src.guardian import opportunity_plans
            original_finalize = opportunity_plans._finalize_plan
            cut_jobs = []
            async def cut_once(*args, **kwargs):
                if not cut_jobs:
                    async with factory.accounting_sessions() as db:
                        actual_proposal = (await db.scalars(select(WorkBoardProposal))).one()
                        job = actual_proposal.admission_job_id
                    native = await durable_job_repository.get_job(job)
                    assert native['status'] == 'succeeded', native
                    cut_jobs.append(job)
                    raise RuntimeError('test-only local failure before finalization after actual native success')
                return await original_finalize(*args, **kwargs)
            monkeypatch.setattr(opportunity_plans, '_finalize_plan', cut_once)
        response = await client.post(f'/api/guardian/opportunities/{opportunity.id}/plan',json=request)
        assert response.status_code == 200, response.text
        if finalizer_cut:
            cut_response = response.json()
            assert cut_jobs and len(calls) == before + 1, cut_response
            cut_cost = await durable_job_repository.inference_accounting_snapshot()
            response = await client.post(f'/api/guardian/opportunities/{opportunity.id}/plan', json=request)
            assert response.status_code == 200, response.text
            assert response.json()['proposal_ref']['proposal_id'] == cut_response['proposal_ref']['proposal_id']
            assert len(calls) == before + 1
            assert await durable_job_repository.inference_accounting_snapshot() == cut_cost
        reference = response.json()['proposal_ref']
        if not reference or reference['status'] != 'proposed':
            async with factory.accounting_sessions() as db:
                rows = list((await db.scalars(select(WorkBoardProposal))).all())
            diagnostic = dict(response=response.json(),proposals=[dict(id=row.proposal_id,status=row.status,
                output=row.proposal_json,job_id=row.admission_job_id,contact_state=row.provider_contact_state) for row in rows],
                native=[await durable_job_repository.get_job(row.admission_job_id) for row in rows if row.admission_job_id])
            (root/'plan-generation-diagnostic.json').write_text(json.dumps(diagnostic,indent=2,default=str))
            pytest.fail(json.dumps(diagnostic,default=str))
        assert reference['blueprint_id'] == blueprint, response.text
        assert len(calls) == before+1 and browser_calls == []
        staged_cost = await durable_job_repository.inference_accounting_snapshot()
        proposal_id, task_id = reference['proposal_id'], reference['parent_task_id']
        async with factory.accounting_sessions() as db:
            proposal = await db.get(WorkBoardProposal,proposal_id)
            task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==task_id))
            assert task.status == WorkBoardStatus.triage
            generation_id = proposal.admission_job_id
        generation = await durable_job_repository.get_job(generation_id)
        assert generation['status'] == 'succeeded'
        if finalizer_cut:
            assert cut_jobs == [generation_id]
        dispatcher = WorkBoardDispatcher(jobs=durable_job_repository,session_provider=factory.accounting_sessions)
        for _ in range(3):
            receipt = await dispatcher.run_pass()
            assert receipt['claimed'] == 0 and receipt['admitted'] == 0, receipt
        assert browser_calls == []
        replay = await client.post(f'/api/guardian/opportunities/{opportunity.id}/plan',json=request)
        assert replay.status_code == 200 and len(calls) == before+1
        for _ in range(2):
            assert (await client.get('/api/guardian/opportunities')).status_code == 200
            assert (await client.get(f'/api/work-board/tasks/{task_id}')).status_code == 200
        assert len(calls) == before+1 and browser_calls == []
        if blueprint == 'public-browser-check':
            response = await client.post(f'/api/work-board/proposals/{proposal_id}/accept',json=dict(
                expected_proposal_revision=reference['proposal_revision'],expected_parent_revision=reference['parent_revision']))
        else:
            response = await client.post(f'/api/work-board/pipelines/{proposal_id}/accept',json=dict(
                expected_revision=reference['proposal_revision'],expected_parent_revision=reference['parent_revision'],
                expected_digest=reference['proposal_digest']))
        assert response.status_code == 200, response.text
        async with factory.accounting_sessions() as db:
            task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==task_id))
            assert task.status == WorkBoardStatus.todo
        passes = []
        for _ in range(8):
            passes.append(await dispatcher.run_pass())
            pending = await client.get('/api/approvals/pending')
            assert pending.status_code == 200, pending.text
            assert pending.json() == [], {'native_approval_required':pending.json(),'passes':passes}
            async with factory.accounting_sessions() as db:
                tasks = list((await db.scalars(select(WorkBoardTask))).all())
            if all(task.status == WorkBoardStatus.done for task in tasks):
                break
        assert all(task.status == WorkBoardStatus.done for task in tasks), {
            'tasks':[(task.task_id,task.status,task.block_kind,task.block_reason) for task in tasks],'passes':passes}
        assert browser_calls and len(calls) == before+1
        assert len(tasks) == (1 if blueprint == 'public-browser-check' else 3)
        async with factory.accounting_sessions() as db:
            attempts = list((await db.scalars(select(WorkBoardAttempt))).all())
            assert len(attempts) == len(tasks)
            native_ids = [attempt.workflow_run_id for attempt in attempts]
            assert len(set(native_ids+[generation_id])) == len(tasks)+1
            if blueprint == 'public-evidence-report':
                operation = await pipelines.read(db,owner,proposal_id)
                assert operation['operation_id'] == proposal_id
                for task in tasks:
                    output = await pipelines.verified_output(db,owner,task)
                    assert read_output(output['file_path'],output['content_sha256'])
        for native_id in native_ids:
            native = await durable_job_repository.get_job(native_id)
            assert native['status'] == 'succeeded', native
            assert native['lease']['fencing_token'] > 0
            assert any(effect.get('receipt_kind') == 'readback' and effect.get('status') == 'succeeded'
                and effect.get('details',{}).get('verified') is True for effect in native['effects']), native
            assert any(artifact.get('exists') is True for artifact in native['artifacts']), native
        if blueprint == 'public-evidence-report':
            report = await client.get(f'/api/work-board/pipelines/{proposal_id}/report')
            assert report.status_code == 200 and 'no_learning' in report.text, report.text
        assert source_calls == ['https://example.com/public']*2
        final_cost = await durable_job_repository.inference_accounting_snapshot()
        assert final_cost['committed_microusd'] == staged_cost['committed_microusd']
        assert (root/'seraph.db').is_file()


async def test_actual_same_goal_three_watches_two_contacted_plan_cap(accounting_db, real_auth, monkeypatch):
    """Focused cadence control only; this is not a managed elapsed-hour proof.

    The original assessment contact-limit predicate receives a logical cadence
    clock. Every other authority/native/source clock and stored timestamp remains
    real. Actual governed HTTP contacts own the original Goal's UTC plan cap.
    """
    from contextvars import ContextVar
    from sqlalchemy import func
    from src.db.models import InferenceCostReservation
    from src.api import auth, goals, model_fabric_settings
    from src.guardian import opportunities, opportunity_runtime, source_watch
    from src.model_fabric import remote_inference_admission
    from src.model_fabric.configuration import write_model_fabric_configuration
    from src.security.http_transport import fetch_pinned_https

    root, _, factory = accounting_db
    original_connect = socket.socket.connect
    def local_connect(sock, address):
        if isinstance(address, tuple) and address[0] not in ('127.0.0.1', '::1', 'localhost'):
            raise AssertionError('focused cap fixture denies all nonlocal network')
        return original_connect(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', local_connect)
    monkeypatch.setattr(settings, 'operator_auth_idle_seconds', 7200)
    monkeypatch.setattr(settings, 'operator_auth_absolute_seconds', 10800)
    monkeypatch.setattr(settings, 'browser_site_allowlist', 'example.com')
    monkeypatch.setattr('src.model_fabric.execution.gpu_admission_broker',
        remote_inference_admission.remote_inference_admission_broker)
    configured = setup_configuration()
    write_model_fabric_configuration(replace(model_fabric_settings._setup_configuration(
        replace(configured.openrouter_setup, timeout_seconds=30, capabilities=('text', 'structured_output')),
        profiles=(), policies=()), egress_revision=configured.egress_revision+1))
    await durable_job_repository.configure_inference_accounting(1000)
    logical_cadence = ContextVar('focused_assessment_contact_clock', default=False)
    original_now, original_limits = opportunities.now, opportunity_runtime._contact_limits
    cadence_seconds = 0
    def focused_now():
        current = original_now()
        return current + timedelta(seconds=cadence_seconds) if logical_cadence.get() else current
    async def original_limits_with_focused_clock(*args, **kwargs):
        token = logical_cadence.set(True)
        try:
            return await original_limits(*args, **kwargs)
        finally:
            logical_cadence.reset(token)
    monkeypatch.setattr(opportunities, 'now', focused_now)
    monkeypatch.setattr(opportunity_runtime, '_contact_limits', original_limits_with_focused_clock)
    calls, source_calls = [], []
    original_client = httpx.AsyncClient
    def clients(**kwargs):
        if kwargs.get('transport') is None:
            kwargs['transport'] = PlanHttpBoundary(calls, {'blueprint': 'public-browser-check'})
        return original_client(**kwargs)
    monkeypatch.setattr(httpx, 'AsyncClient', clients)
    source_counts = {}
    async def source_http(request):
        target = str(request.url)
        assert request.method == 'GET' and target in [f'https://example.com/cap-{index}' for index in range(3)]
        source_calls.append(target)
        source_counts[target] = source_counts.get(target, 0) + 1
        text = 'Stable public line\n' + ('Previous release\n' if source_counts[target] == 1 else 'Relevant new release\n')
        return httpx.Response(200, request=request, headers={'content-type': 'text/plain'},
            stream=ResponseBytes(text.encode()))
    def named_dns(host, port):
        assert host == 'example.com' and port == 443
        return ['93.184.216.34']
    async def pinned_source(url, **kwargs):
        return await fetch_pinned_https(url, resolver=named_dns, transport=httpx.MockTransport(source_http), **kwargs)
    monkeypatch.setattr(source_watch, 'fetch_pinned_https', pinned_source)
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    for router, prefix in ((auth.router, '/api/auth'), (goals.router, '/api'),
            (model_fabric_settings.router, '/api'), (source_watch.source_watch_router, '/api')):
        app.include_router(router, prefix=prefix)
    async with original_client(transport=httpx.ASGITransport(app=app), base_url='http://test',
            headers={'origin': 'http://localhost:3001'}) as client:
        login = await client.post('/api/auth/login', json={'password': 'research-vertical-private-secret'})
        assert login.status_code == 200, login.text
        operator = login.json()
        for capability in ('text', 'structured_output', 'latency_ms', 'health'):
            response = await client.post('/api/settings/model-fabric/canary', json=dict(
                profile_id='openrouter', capability=capability, timeout_seconds=30))
            assert response.status_code == 200 and response.json()['outcome'] == 'passed', response.text
        started = original_now()
        expires = started + timedelta(hours=2)
        response = await client.post('/api/goals', json=dict(title='Review three actual public watches',
            proactive_enabled=True, admission_budget=dict(reviewed_grant=True, grant_id='cap-original',
                max_outstanding_jobs=2, max_attempts=2, max_runtime_seconds=300,
                period_started_at=started.isoformat(), period_expires_at=expires.isoformat())))
        assert response.status_code == 200, response.text
        goal = response.json()
        watches = []
        for index in range(3):
            response = await client.post('/api/capabilities/source-watches', json=dict(goal_id=goal['id'],
                expected_goal_revision=goal['revision'], sources=[dict(source_key=f'cap-{index}',
                    kind='public_https_text', target=f'https://example.com/cap-{index}', label='Explicit public cap source', priority=1)],
                criteria={}, schedule=dict(cron='0 * * * *', timezone='UTC'), write_mode='standing_reviewed',
                reviewed_grant_id='cap-original'))
            assert response.status_code == 200, response.text
            watches.append(response.json())
        response = await client.put(f"/api/goals/{goal['id']}/guardian-policy", json=dict(
            expected_goal_revision=goal['revision'], expected_policy_revision=0, idempotency_key=str(uuid4()),
            policy=dict(schema_version='seraph.guardian.policy.v1', assessment_enabled=True, auto_stage_plan=False,
                confirmed_at=started.isoformat(), review_due_at=expires.isoformat(), grant_id='cap-original',
                original_root_id=operator['session_id'], goal_revision=goal['revision'],
                source_watch_ids=[watch['id'] for watch in watches], max_assessments_per_utc_day=3,
                max_plan_proposals_per_utc_day=2, max_notification_per_utc_day=0)))
        assert response.status_code == 200, response.text
        service = source_watch.SourceWatchService()
        actual_plan_jobs = []
        for index, watch in enumerate(watches):
            cadence_seconds = index * 1801
            for stage, expected in (('baseline', 'baseline_initialized'), ('material', 'succeeded')):
                receipt = await service.run_watch(watch['id'], occurrence_id=f'cap-{index}-{stage}',
                    expected_plan_revision=watch['plan_revision'], expected_owner_session_id=operator['session_id'])
                assert receipt['status'] == expected, receipt
            async with factory.accounting_sessions() as db:
                opportunity = (await db.scalars(select(GuardianOpportunity).where(
                    GuardianOpportunity.watch_id == watch['id']))).one()
            await admit_assessment(opportunity.id)
            await execute_assessment(opportunity.id)
            async with factory.accounting_sessions() as db:
                opportunity = await db.get(GuardianOpportunity, opportunity.id)
                assert opportunity.status == 'proposed', opportunity.reason_code
                request = dict(expected_opportunity_revision=opportunity.revision,
                    expected_goal_revision=goal['revision'], idempotency_key=str(uuid4()))
            before = len(calls)
            before_sources = list(source_calls)
            if index == 2:
                before_cost = await durable_job_repository.inference_accounting_snapshot()
                async with factory.accounting_sessions() as db:
                    before_counts = [await db.scalar(select(func.count()).select_from(model))
                        for model in (WorkBoardTask, WorkBoardProposal, InferenceCostReservation)]
                    original_identity = (opportunity.id, opportunity.revision, opportunity.goal_id,
                        opportunity.original_root_id, opportunity.expires_at, opportunity.assessment_deadline_at)
            response = await client.post(f'/api/guardian/opportunities/{opportunity.id}/plan', json=request)
            if index < 2:
                assert response.status_code == 200 and response.json()['proposal_ref']['status'] == 'proposed', response.text
                assert len(calls) == before + 1
                reference = response.json()['proposal_ref']
                async with factory.accounting_sessions() as db:
                    proposal = await db.get(WorkBoardProposal, reference['proposal_id'])
                    actual_plan_jobs.append(proposal.admission_job_id)
                    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == reference['parent_task_id']))
                    assert task.status == WorkBoardStatus.triage
                native = await durable_job_repository.get_job(actual_plan_jobs[-1])
                assert native['status'] == 'succeeded' and native['lease']['fencing_token'] > 0, native
            else:
                assert response.status_code == 200 and response.json()['proposal_ref'] is None, response.text
                assert response.json()['reason_code'] == 'opportunity_plan_daily_limit', response.text
                assert len(calls) == before and source_calls == before_sources
                assert await durable_job_repository.inference_accounting_snapshot() == before_cost
                async with factory.accounting_sessions() as db:
                    after_counts = [await db.scalar(select(func.count()).select_from(model))
                        for model in (WorkBoardTask, WorkBoardProposal, InferenceCostReservation)]
                    unchanged = await db.get(GuardianOpportunity, opportunity.id)
                    assert (unchanged.id, unchanged.revision, unchanged.goal_id, unchanged.original_root_id,
                        unchanged.expires_at, unchanged.assessment_deadline_at) == original_identity
                    assert after_counts == before_counts and unchanged.proposal_id is None
                offer = await client.get('/api/guardian/opportunities', params={'goal_id': goal['id']})
                assert offer.status_code == 200
                original_offer = next(item for item in offer.json()['items'] if item['opportunity_id'] == opportunity.id)
                assert original_offer['plan_offer']['generation_block_reason'] == 'opportunity_plan_daily_limit', offer.text
                assert original_offer['plan_offer']['available_blueprint_ids']
                assert original_offer['plan_offer']['can_generate'] is False
        assert len(set(actual_plan_jobs)) == 2 and len(source_calls) == 6
        assert len([body for body in calls if len(body['messages']) == 2
            and 'offered_blueprints' in json.loads(body['messages'][1]['content'])]) == 2
        assert (root / 'seraph.db').is_file()
