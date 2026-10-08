"""Actual authenticated native outcomes and provider-free opportunity learning.

Named source/provider HTTP is intercepted by the existing real Chromium journey.
No native result, Work state, authority or successful readback is manufactured.
Managed cadence evidence is separate and uses actual elapsed time.
"""
import json
from uuid import uuid4

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from config.settings import settings
from src.auth.middleware import OperatorAuthMiddleware
from src.db.models import GuardianOpportunity, MemoryProposal, WorkBoardTask, WorkBoardAttempt
from src.work_board.dispatcher import WorkBoardDispatcher
from src.workflows.job_runtime import durable_job_repository
from tests.test_guardian_opportunity_plan_vertical import _actual_plan_journey, PlanHttpBoundary
from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth


async def test_one_actual_verified_report_helpful_cpu_abstains_without_memory_adoption(
        accounting_db, real_auth, monkeypatch):
    """One real Helpful outcome cannot satisfy the two-outcome preference rule."""
    from src.api import auth, goals
    _, _, factory = accounting_db
    # These imported providers must share the real auth/native file-backed DB.
    for target in ('src.guardian.feedback.get_session', 'src.memory.m5.get_session',
            'src.memory.repository.get_session'):
        monkeypatch.setattr(target, factory.accounting_sessions)
    original_client = httpx.AsyncClient
    original_create = auth.create_session
    original_provider = PlanHttpBoundary.handle_async_request
    provider_requests = []
    async def observe_original_provider(self, request):
        provider_requests.append(request)
        return await original_provider(self, request)
    monkeypatch.setattr(PlanHttpBoundary, 'handle_async_request', observe_original_provider)
    sessions = []
    async def observe_original_session(*args, **kwargs):
        result = await original_create(*args, **kwargs)
        sessions.append(result)
        return result
    monkeypatch.setattr(auth, 'create_session', observe_original_session)
    await _actual_plan_journey(accounting_db, real_auth, monkeypatch, 'public-evidence-report')
    assert len(sessions) == 1
    token, operator = sessions[0]
    root, _, factory = accounting_db
    async with factory.accounting_sessions() as db:
        opportunity = (await db.scalars(select(GuardianOpportunity))).one()
        opportunity_id = opportunity.id
        before_tasks = list((await db.scalars(select(WorkBoardTask))).all())
        assert len(before_tasks) == 3
        assert not list((await db.scalars(select(MemoryProposal))).all())
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router, prefix='/api/auth')
    app.include_router(goals.router, prefix='/api')
    before_cost = await durable_job_repository.inference_accounting_snapshot()
    before_provider_count = len(provider_requests)
    async with original_client(transport=httpx.ASGITransport(app=app), base_url='http://test',
            headers={'origin': 'http://localhost:3001'}) as client:
        client.cookies.set(settings.operator_auth_cookie_name, token)
        current = await client.get('/api/auth/session')
        assert current.status_code == 200 and current.json()['session_id'] == operator.session_id
        feedback = await client.post(f'/api/guardian/opportunities/{opportunity_id}/feedback', json=dict(
            expected_feedback_revision=0, feedback_type='helpful', reason='The verified report is useful.',
            idempotency_key=str(uuid4())))
        assert feedback.status_code == 200, feedback.text
        assert feedback.json()['feedback_revision'] == 1
        assert feedback.json()['memory_status'] == 'no_learning'
        async with factory.accounting_sessions() as db:
            current_opportunity = await db.get(GuardianOpportunity, opportunity_id)
            opportunity_revision = current_opportunity.revision
        request = dict(expected_opportunity_revision=opportunity_revision,
            expected_feedback_revision=1, idempotency_key=str(uuid4()))
        response = await client.post(f'/api/guardian/opportunities/{opportunity_id}/recommendation', json=request)
        assert response.status_code == 200, response.text
        task_id = response.json()['task_id']
        dispatcher = WorkBoardDispatcher(jobs=durable_job_repository, session_provider=factory.accounting_sessions)
        for _ in range(4):
            await dispatcher.run_pass()
            response = await client.get(f'/api/guardian/opportunities/{opportunity_id}/recommendation',
                params={'idempotency_key': request['idempotency_key']})
            assert response.status_code == 200, response.text
            if response.json()['status'] == 'no_learning':
                break
        receipt = response.json()
        assert receipt['status'] == 'no_learning', receipt
        assert receipt['task_id'] == task_id and receipt['proposal_id'] is None
        assert receipt['attempt_id'] and receipt['job_id'] and receipt['bundle_digest']
        async with factory.accounting_sessions() as db:
            tasks = list((await db.scalars(select(WorkBoardTask))).all())
            attempts = list((await db.scalars(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task_id))).all())
            assert len(tasks) == 4 and len(attempts) == 1
            assert attempts[0].attempt_id == receipt['attempt_id']
            assert not list((await db.scalars(select(MemoryProposal))).all())
        native = await durable_job_repository.get_job(receipt['job_id'])
        assert native['status'] == 'succeeded' and native['attempt_count'] == 1, native
        assert any(item.get('exists') is True for item in native['artifacts']), native
        assert any(item.get('receipt_kind') == 'readback' and item.get('status') == 'succeeded'
            and item.get('details', {}).get('verified') is True for item in native['effects']), native
        assert await durable_job_repository.inference_accounting_snapshot() == before_cost
        assert len(provider_requests) == before_provider_count
        replay = await client.post(f'/api/guardian/opportunities/{opportunity_id}/recommendation', json=request)
        assert replay.status_code == 200 and replay.json()['task_id'] == task_id
        assert replay.json()['job_id'] == receipt['job_id'] and replay.json()['status'] == 'no_learning'
        assert (root / 'seraph.db').is_file()
        (root / 'one-helpful-native-cpu.json').write_text(json.dumps(dict(receipt=receipt,
            native=native, signed_memory_proposals=0, provider_cost_unchanged=True), indent=2, default=str))
