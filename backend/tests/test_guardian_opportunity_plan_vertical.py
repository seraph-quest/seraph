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
        response = await client.post(f'/api/guardian/opportunities/{opportunity.id}/plan',json=request)
        assert response.status_code == 200, response.text
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
