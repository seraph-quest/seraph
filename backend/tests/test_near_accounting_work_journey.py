"""Actual authenticated NEAR tasks and shared billing; only named HTTP is intercepted."""
import asyncio
import hashlib
import json
from uuid import uuid4, uuid5, NAMESPACE_DNS

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

from src.auth.middleware import OperatorAuthMiddleware
from src.db.models import WorkBoardTask, WorkBoardAttempt, WorkflowRunState, InferenceCostReservation, WorkBoardStatus
from src.work_board.dispatcher import WorkBoardDispatcher
from src.workflows.job_runtime import DurableJobRepository
from src.workflows.inference_accounting import InferenceAccountingError
from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth
from tests.test_near_text_work_journey import create_actual_near_task
from tests.test_near_text_setup_api import near_payload
from tests.test_openrouter_setup_api import _v2_setup_payload
from tests.support.sync import wait_until


class Bytes(httpx.AsyncByteStream):
    def __init__(self, raw):
        self.raw = raw
    async def __aiter__(self):
        for offset in range(0, len(self.raw), 31):
            yield self.raw[offset:offset + 31]


@pytest_asyncio.fixture
async def actual_billing_journey(accounting_db, real_auth, monkeypatch):
    from src.api import auth, work_board, model_fabric_settings, goals
    from src.model_fabric import remote_inference_admission
    root, engine, factory = accounting_db
    controls = {"nano": 1001, "calls": [], "order": [], "active": 0, "peak": 0,
        "or_started": asyncio.Event(), "or_release": asyncio.Event(), "or_count": 0}
    body_id = "chatcmpl-billing-actual-boundary"
    provider_id = str(uuid5(NAMESPACE_DNS, body_id))
    async def provider(request):
        current_body_id = controls.get('body_id', body_id)
        current_provider_id = str(uuid5(NAMESPACE_DNS, current_body_id))
        assert request.method == "POST" and request.url.scheme == "https"
        host, path = request.url.host, request.url.path
        assert (host, path) in {("cloud-api.near.ai", "/v1/chat/completions"),
            ("cloud-api.near.ai", "/v1/billing/costs"), ("openrouter.ai", "/api/v1/chat/completions")}
        controls["active"] += 1
        controls["peak"] = max(controls["peak"], controls["active"])
        controls["calls"].append((host, path))
        try:
            body = json.loads(request.content)
            if host == "openrouter.ai":
                assert request.headers["authorization"] == "Bearer private-intercepted-or-key"
                assert body["model"] == "fixture/text"
                controls["or_count"] += 1
                index = controls["or_count"]
                controls["order"].append(f"or-{index}")
                if index == controls.get("hold_or_index", 1) and controls.get("hold_or"):
                    controls["or_started"].set()
                    await asyncio.wait_for(controls["or_release"].wait(), 15)
                payload = {"id": f"gen-canary-{index}", "model": "fixture/text", "provider": "fixture",
                    "choices": [{"message": {"role": "assistant", "content": "CANARY_OK"}}],
                    "usage": {"cost": "0.000003", "prompt_tokens": 1, "completion_tokens": 1}}
            elif path == "/v1/chat/completions":
                controls["order"].append("near")
                assert request.headers["authorization"] == "Bearer private-intercepted-near-key"
                assert body["model"] == "z-ai/glm-5.3-flash" and body["n"] == 1 and body["stream"] is False
                assert body["messages"] == [{"role": "user", "content": controls.get('question', "Private bounded fixture question")}]
                if controls.get("after_post"):
                    await controls["after_post"]()
                payload = {"id": current_body_id, "model": "z-ai/glm-5.3-flash",
                    "choices": [{"message": {"role": "assistant", "content": controls.get('answer', "Unreleased private answer")}, "finish_reason": "stop"}]}
            else:
                assert request.headers["authorization"] == "Bearer private-intercepted-near-key"
                assert body == {"requestIds": [current_provider_id]}
                payload = {"requests": [{"requestId": current_provider_id, "costNanoUsd": controls["nano"]}]}
                if controls.get("missing"):
                    payload["warning"] = "not ready"
            raw = json.dumps(payload, separators=(",", ":")).encode()
            if host == "cloud-api.near.ai" and path == "/v1/billing/costs":
                controls["billing_raw"] = raw
            return httpx.Response(200, request=request, stream=Bytes(raw), headers={
                "content-type": "application/json", "content-encoding": "identity",
                **({"inference-id": current_provider_id} if host == "cloud-api.near.ai" and path == "/v1/chat/completions" else {})})
        finally:
            controls["active"] -= 1
    original = httpx.AsyncClient
    def clients(**kwargs):
        if "transport" not in kwargs:
            kwargs["transport"] = httpx.MockTransport(provider)
        return original(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", clients)
    # Each fresh SQLite deployment uses the same actual canonical broker at every imported alias.
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    for router, prefix in ((auth.router, "/api/auth"), (work_board.router, "/api"),
            (model_fabric_settings.router, "/api"), (goals.router, "/api")):
        app.include_router(router, prefix=prefix)
    async with original(transport=httpx.ASGITransport(app=app), base_url="http://test",
            headers={"origin": "http://localhost:3001"}) as client:
        jobs = DurableJobRepository()
        dispatcher = WorkBoardDispatcher(jobs=jobs, session_provider=factory.accounting_sessions)
        yield client, factory, jobs, dispatcher, controls, engine
        controls["or_release"].set()


async def drive(dispatcher):
    return [await dispatcher.run_pass() for _ in range(4)]


async def state(factory, task_id):
    async with factory.accounting_sessions() as db:
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
        attempts = list((await db.scalars(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task_id))).all())
        assert len(attempts) == 1
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempts[0].workflow_run_id))
        assert run is not None and run.attempt_count == 1
        row = await db.scalar(select(InferenceCostReservation).where(InferenceCostReservation.job_id == run.run_identity))
        return task, run, row


def no_answer(task, run):
    assert task.status is WorkBoardStatus.blocked
    assert run.status != "succeeded"
    assert json.loads(run.artifact_receipts_json) == []
    public = json.dumps(run.model_dump(mode="json"))
    assert "Private bounded fixture question" not in public and "Unreleased private answer" not in public


@pytest.mark.asyncio
async def test_actual_large_charge_settles_and_delivers_the_answer(actual_billing_journey):
    # #1284: a charge above the legacy stored bound is recorded and the answer
    # is delivered; nothing is withheld or latched for an amount.
    client, factory, jobs, dispatcher, controls, _engine = actual_billing_journey
    controls["nano"] = 2_000_001
    task_id, _owner = await create_actual_near_task(client, factory)
    await drive(dispatcher)
    task, run, row = await state(factory, task_id)
    assert task.status is WorkBoardStatus.review and run.status == "succeeded"
    assert row.state == "settled" and row.actual_cost_microusd == 2001 and row.recovery_reason is None
    history = json.loads(row.evidence_json)
    assert history[-1]["provenance"] == "near_billing_costs" and history[-1]["cost_nano_usd"] == 2_000_001
    snapshot = await jobs.inference_accounting_snapshot()
    assert snapshot["spent_microusd"] == 2001
    assert snapshot["status"] == "ready" and snapshot["reason_code"] is None
    output = await client.get(f"/api/work-board/tasks/{task_id}/near-text/output")
    assert output.status_code == 200 and output.json()["text"] == "Unreleased private answer", output.text
    await drive(dispatcher)
    assert controls["calls"].count(("cloud-api.near.ai", "/v1/chat/completions")) == 1


@pytest.mark.asyncio
async def test_unpriced_answer_is_delivered_and_manual_charge_record_survives_reopen(actual_billing_journey):
    # #1284: billing that reports nothing leaves the row "not priced yet"; the
    # answer is still delivered and recording the charge later changes only
    # the spend history, never the job or the provider contact count.
    client, factory, jobs, dispatcher, controls, engine = actual_billing_journey
    controls["missing"] = True
    task_id, _owner = await create_actual_near_task(client, factory)
    await drive(dispatcher)
    task, run, row = await state(factory, task_id)
    assert task.status is WorkBoardStatus.review and run.status == "succeeded", (task.block_reason, run.status, run.failure_reason, row.state, row.recovery_reason)
    assert row.state == "unknown" and row.actual_cost_microusd is None
    assert row.recovery_reason == "provider_cost_readback_required"
    output = await client.get(f"/api/work-board/tasks/{task_id}/near-text/output")
    assert output.status_code == 200 and output.json()["text"] == "Unreleased private answer", output.text
    original = (run.run_identity, run.deadline_at, row.operation_id, row.bound_microusd)
    snapshot = await jobs.inference_accounting_snapshot()
    assert snapshot["unpriced_requests"] == 1 and snapshot["spent_microusd"] == 0
    await engine.dispose()
    fresh_jobs = DurableJobRepository()
    await fresh_jobs.recover_inference_accounting(job_id=run.run_identity)
    fresh_dispatcher = WorkBoardDispatcher(jobs=fresh_jobs, session_provider=factory.accounting_sessions)
    await drive(fresh_dispatcher)
    task, run, row = await state(factory, task_id)
    assert (run.run_identity, run.deadline_at, row.operation_id, row.bound_microusd) == original
    assert run.status == "succeeded"
    assert (await fresh_jobs.inference_accounting_snapshot())["unpriced_requests"] == 1
    settled = await client.post("/api/settings/model-fabric/accounting/settle", json={
        "operation_id": row.operation_id, "job_id": run.run_identity, "expected_revision": row.revision,
        "actual_cost_microusd": 7, "evidence_digest": hashlib.sha256(b"manual statement fixture").hexdigest(),
        "idempotency_key": str(uuid4())})
    assert settled.status_code == 200, settled.text
    task, run, row = await state(factory, task_id)
    assert task.status is WorkBoardStatus.review and run.status == "succeeded"
    assert row.state == "settled" and row.actual_cost_microusd == 7
    assert json.loads(row.evidence_json)[-1]["provenance"] == "manual_externally_unverified"
    assert json.loads(row.evidence_json)[-1]["provider_charge_verified"] is False
    after = await fresh_jobs.inference_accounting_snapshot()
    assert after["unpriced_requests"] == 0 and after["spent_microusd"] == 7
    assert (await client.get(f"/api/work-board/tasks/{task_id}/near-text/output")).status_code == 200
    await drive(fresh_dispatcher)
    assert controls["calls"].count(("cloud-api.near.ai", "/v1/chat/completions")) == 1
    task, run, row = await state(factory, task_id)
    assert (run.run_identity, run.deadline_at, row.operation_id, row.bound_microusd) == original
    # A separately requested question is a new admission, never a replay of old text.
    goal = await client.post('/api/goals', json={'title': 'Fresh question after liability reconciliation',
        'admission_budget': {'reviewed_grant': True, 'grant_id': 'fresh-question-explicit-review',
            'max_outstanding_jobs': 1, 'max_attempts': 1, 'max_runtime_seconds': 120}})
    assert goal.status_code == 200, goal.text
    fresh_uuid = str(uuid4())
    fresh_input = await client.post('/api/work-board/input-artifacts', json={'schema_version': 1,
        'capability_id': 'inference.near-text.v1', 'goal_id': goal.json()['id'], 'goal_revision': 1,
        'idempotency_key': fresh_uuid, 'input': {'schema_version': 'seraph.near.text.input.v1',
            'question': 'Fresh explicitly requested question', 'max_output_tokens': 32}})
    assert fresh_input.status_code == 200, fresh_input.text
    fresh = await client.post('/api/work-board/tasks', json={'title': 'Fresh NEAR question',
        'goal_id': goal.json()['id'], 'goal_revision': 1, 'status': 'todo', 'requires_review': True,
        'capability_id': 'inference.near-text.v1', 'input_artifact_id': fresh_input.json()['artifact_id'],
        'idempotency_key': fresh_uuid})
    assert fresh.status_code == 200, fresh.text
    fresh_id = fresh.json()['task']['task_id']
    assert fresh_id != task_id
    controls.update(missing=False, body_id='chatcmpl-new-question', question='Fresh explicitly requested question',
        answer='New explicitly admitted answer')
    await drive(fresh_dispatcher)
    new_task, new_run, new_row = await state(factory, fresh_id)
    assert new_run.run_identity != original[0] and new_row.operation_id != original[2]
    assert new_task.status is WorkBoardStatus.review and new_run.status == 'succeeded'
    assert new_row.actual_cost_microusd == 2
    output = await client.get(f'/api/work-board/tasks/{fresh_id}/near-text/output')
    assert output.status_code == 200 and output.json()['text'] == 'New explicitly admitted answer', output.text
    assert (await client.get(f'/api/work-board/tasks/{task_id}/near-text/output')).json()['text'] == 'Unreleased private answer'
    old_task, old_run, old_row = await state(factory, task_id)
    assert (old_run.run_identity, old_run.deadline_at, old_row.operation_id, old_row.bound_microusd) == original
    assert old_row.actual_cost_microusd == 7 and controls['calls'].count(('cloud-api.near.ai', '/v1/chat/completions')) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['unsettled', 'wrong_revision', 'wrong_job', 'foreign_root', 'revoked_root', 'current'])
async def test_lane_recovery_keeps_owner_root_and_revision_checks(actual_billing_journey, case):
    # Reduced from the pre-#1284 negative-boundary test: while
    # reconcile_settled_near_operation stays live it still refuses a foreign
    # job, a stale revision, an unpriced row, another Root and a revoked Root,
    # and never touches the delivered answer or contacts NEAR again.
    from config.settings import settings
    from src.auth.service import authenticate_token, revoke_session
    from src.model_fabric.remote_inference_admission import get_remote_inference_admission_broker
    client, factory, jobs, dispatcher, controls, _engine = actual_billing_journey
    controls['missing'] = case == 'unsettled'
    task_id, _owner = await create_actual_near_task(client, factory)
    await drive(dispatcher)
    task, run, row = await state(factory, task_id)
    assert task.status is WorkBoardStatus.review and run.status == 'succeeded'
    assert row.state == ('unknown' if case == 'unsettled' else 'settled')
    operator = await authenticate_token(client.cookies.get(settings.operator_auth_cookie_name), touch=False)
    job_id, revision = run.run_identity, row.revision
    if case == 'wrong_job':
        job_id = 'foreign-native-job'
    elif case == 'wrong_revision':
        revision += 1
    elif case == 'foreign_root':
        login = await client.post('/api/auth/login', json={'password': 'research-vertical-private-secret', 'start_new_scope': True})
        assert login.status_code == 200, login.text
        operator = await authenticate_token(client.cookies.get(settings.operator_auth_cookie_name), touch=False)
        assert operator.principal.principal_id != row.owner_id
    elif case == 'revoked_root':
        await revoke_session(operator.session_id)
    result = await get_remote_inference_admission_broker().reconcile_settled_near_operation(
        operation_id=row.operation_id, job_id=job_id, expected_revision=revision, operator=operator)
    expected = {'unsettled': 'settled_charge_required', 'wrong_revision': 'settled_operation_changed',
        'wrong_job': 'settled_operation_unavailable', 'foreign_root': 'original_owner_required',
        'revoked_root': 'original_root_inactive', 'current': 'broker_reconciliation_unavailable'}
    assert result == {'status': 'deferred', 'reason_code': expected[case]}
    final_task, final_run, final_row = await state(factory, task_id)
    assert final_run.status == 'succeeded' and final_row.revision == row.revision
    assert final_row.actual_cost_microusd == row.actual_cost_microusd
    assert controls['calls'].count(('cloud-api.near.ai', '/v1/chat/completions')) == 1


@pytest.mark.asyncio
async def test_disable_after_post_still_settles_original_charge_without_adoption(actual_billing_journey):
    client, factory, jobs, dispatcher, controls, _engine = actual_billing_journey
    task_id, _owner = await create_actual_near_task(client, factory)
    async def disable():
        response = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 3,
            "near_text": near_payload(enabled=False, plaintext_provider_egress_acknowledged=False)})
        assert response.status_code == 200, response.text
    controls["after_post"] = disable
    await drive(dispatcher)
    task, run, row = await state(factory, task_id)
    no_answer(task, run)
    assert row.state == "settled" and row.actual_cost_microusd == 2
    assert json.loads(row.evidence_json)[-1]["provider_charge_verified"] is True
    assert (await jobs.inference_accounting_snapshot())["spent_microusd"] == 2
    await drive(dispatcher)
    assert controls["calls"] == [("cloud-api.near.ai", "/v1/chat/completions"), ("cloud-api.near.ai", "/v1/billing/costs")]
    assert (await client.get(f"/api/work-board/tasks/{task_id}/near-text/output")).status_code == 409


@pytest.mark.asyncio
async def test_foreign_sealed_evidence_cannot_rewrite_actual_near_settlement(actual_billing_journey):
    from src.model_fabric.near_text_billing import derive_near_inference_id, parse_near_billing_evidence
    client, factory, jobs, dispatcher, controls, _engine = actual_billing_journey
    task_id, _owner = await create_actual_near_task(client, factory)
    await drive(dispatcher)
    task, run, row = await state(factory, task_id)
    assert task.status is WorkBoardStatus.review and run.status == "succeeded" and row.actual_cost_microusd == 2
    before = await jobs.inference_accounting_snapshot()
    evidence = parse_near_billing_evidence(response_body=controls["billing_raw"], original_operation_id="other-operation",
        inference_identity=derive_near_inference_id(body_id="chatcmpl-billing-actual-boundary"))
    with pytest.raises(InferenceAccountingError, match="near_billing_evidence_invalid"):
        await jobs.settle_inference_cost(row.operation_id, near_billing_evidence=evidence)
    after = await jobs.inference_accounting_snapshot()
    assert after["ledger_digest"] == before["ledger_digest"] and after["revision"] == before["revision"]
    assert after["spent_microusd"] == 2
    assert (await client.get(f"/api/work-board/tasks/{task_id}/near-text/output")).status_code == 200
    assert len(controls["calls"]) == 2


@pytest.mark.openrouter_legacy  # ADR-035: OpenRouter canary/contender legs of the shared NEAR/OpenRouter serial ledger
@pytest.mark.asyncio
async def test_actual_or_canaries_and_near_native_share_serial_priority_and_ledger(actual_billing_journey):
    client, factory, jobs, dispatcher, controls, _engine = actual_billing_journey
    login = await client.post("/api/auth/login", json={"password": "research-vertical-private-secret"})
    assert login.status_code == 200
    saved = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1,
        "openrouter_setup": _v2_setup_payload(api_key="private-intercepted-or-key")})
    assert saved.status_code == 200, saved.text
    task_id, _owner = await create_actual_near_task(client, factory, expected_policy_revision=3)
    controls["hold_or"] = True
    canary = {"profile_id": "openrouter.text", "capability": "text", "timeout_seconds": 45}
    first = asyncio.create_task(client.post("/api/settings/model-fabric/canary", json=canary))
    workers = [first]
    try:
        await asyncio.wait_for(controls["or_started"].wait(), 10)
        second = asyncio.create_task(client.post("/api/settings/model-fabric/canary", json=canary))
        native = asyncio.create_task(drive(dispatcher))
        workers.extend((second, native))
        def native_queued():
            from src.model_fabric.remote_inference_admission import get_remote_inference_admission_broker
            broker = get_remote_inference_admission_broker()
            with broker._condition:
                return any(broker._operations[item].request.runtime_path == "near_text_native" for item in broker._queue)
        # The native reservation precedes its enqueue behind the held canary.
        await wait_until(native_queued, what="native request queued behind the held canary", task=native)
        async with factory.accounting_sessions() as db:
            queued_rows = list((await db.scalars(select(InferenceCostReservation))).all())
        async with factory.accounting_sessions() as db:
            observed_task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
        assert len(queued_rows) == 2, {
            "rows": [(row.runtime_path, row.state, row.priority) for row in queued_rows],
            "native_done": native.done(), "task_status": observed_task.status,
            "block_reason": observed_task.block_reason,
        }
        assert controls["order"] == ["or-1"] and controls["peak"] == 1
        near_row = next(row for row in queued_rows if row.runtime_path == "near_text_native")
        or_rows = [row for row in queued_rows if row.runtime_path == "capability_probe"]
        assert len(or_rows) == 1 and all(near_row.priority > row.priority for row in or_rows)
        controls["or_release"].set()
        await asyncio.wait_for(asyncio.gather(*workers), 30)
        assert second.result().status_code == 409
        assert second.result().json()["detail"] == "Another manual model canary is already running"
        later_canary = await client.post("/api/settings/model-fabric/canary", json=canary)
        for response in (first.result(), later_canary):
            assert response.status_code == 200 and response.json()["outcome"] == "passed", response.text
            assert response.json()["proof"] is not None
            assert response.json()["proof_persistence"] == "persisted"
            assert response.json()["receipt_persistence"] == "persisted"
        task, run, row = await state(factory, task_id)
        assert task.status is WorkBoardStatus.review and run.status == "succeeded" and row.actual_cost_microusd == 2
        assert controls["order"] == ["or-1", "near", "or-2"] and controls["peak"] == 1
        snapshot = await jobs.inference_accounting_snapshot()
        assert snapshot["operation_count"] == 3 and snapshot["spent_microusd"] == 8
        assert snapshot["unpriced_requests"] == 0 and snapshot["accounting_continuity_verified"] is True
    finally:
        controls["or_release"].set()
        for worker in workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


@pytest.mark.asyncio
async def test_manual_debt_commit_cannot_release_physically_running_callback(actual_billing_journey):
    from src.model_fabric.remote_inference_admission import get_remote_inference_admission_broker
    client, factory, jobs, dispatcher, controls, _engine = actual_billing_journey
    task_id, _owner = await create_actual_near_task(client, factory)
    entered, release = asyncio.Event(), asyncio.Event()
    async def held_physical_post():
        entered.set()
        await asyncio.wait_for(release.wait(), 15)
    controls['after_post'] = held_physical_post
    worker = asyncio.create_task(drive(dispatcher))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        _task, run, row = await state(factory, task_id)
        before = get_remote_inference_admission_broker().receipt_for(row.operation_id)
        assert before.status == 'running' and not before.callback_completed
        settled = await client.post('/api/settings/model-fabric/accounting/settle', json={
            'operation_id': row.operation_id, 'job_id': run.run_identity, 'expected_revision': row.revision,
            'actual_cost_microusd': 7, 'evidence_digest': hashlib.sha256(b'explicit pending-callback statement').hexdigest(),
            'idempotency_key': str(uuid4())})
        assert settled.status_code == 200 and settled.json()['status'] == 'settled', settled.text
        assert settled.json()['lane_recovery'] == {'status': 'deferred', 'reason_code': 'provider_callback_running'}
        current = get_remote_inference_admission_broker().receipt_for(row.operation_id)
        assert current.status == 'running' and not current.callback_completed
        assert current.active_operation_id == row.operation_id and current.fencing_token == before.fencing_token
        assert controls['calls'] == [('cloud-api.near.ai', '/v1/chat/completions')]
        release.set()
        await asyncio.wait_for(worker, 20)
        task, run, final_row = await state(factory, task_id)
        no_answer(task, run)
        assert final_row.actual_cost_microusd == 7
        assert get_remote_inference_admission_broker().receipt_for(row.operation_id).callback_completed
    finally:
        release.set()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.openrouter_legacy  # ADR-035: OpenRouter canary/contender legs of the shared NEAR/OpenRouter serial ledger
@pytest.mark.asyncio
async def test_near_lane_recovery_cannot_reconcile_real_openrouter_operation(actual_billing_journey):
    from config.settings import settings
    from src.auth.service import authenticate_token
    from src.model_fabric.remote_inference_admission import get_remote_inference_admission_broker
    client, factory, jobs, _dispatcher, _controls, _engine = actual_billing_journey
    assert (await client.post('/api/auth/login', json={'password': 'research-vertical-private-secret'})).status_code == 200
    saved = await client.put('/api/settings/model-fabric', json={'expected_policy_revision': 1,
        'openrouter_setup': _v2_setup_payload(api_key='private-intercepted-or-key')})
    assert saved.status_code == 200, saved.text
    canary = await client.post('/api/settings/model-fabric/canary', json={
        'profile_id': 'openrouter.text', 'capability': 'text', 'timeout_seconds': 45})
    assert canary.status_code == 200 and canary.json()['outcome'] == 'passed', canary.text
    snapshot = await jobs.inference_accounting_snapshot()
    assert len(snapshot['operations']) == 1
    row = snapshot['operations'][0]
    operator = await authenticate_token(client.cookies.get(settings.operator_auth_cookie_name), touch=False)
    result = await get_remote_inference_admission_broker().reconcile_settled_near_operation(
        operation_id=row['operation_id'], job_id=row['job_id'], expected_revision=row['revision'], operator=operator)
    assert result == {'status': 'deferred', 'reason_code': 'settled_operation_changed'}
    after = await jobs.inference_accounting_snapshot()
    assert after['ledger_digest'] == snapshot['ledger_digest'] and after['spent_microusd'] == 3


@pytest.mark.openrouter_legacy  # ADR-035: OpenRouter canary/contender legs of the shared NEAR/OpenRouter serial ledger
@pytest.mark.asyncio
async def test_two_ready_ordinary_or_contenders_and_near_choose_highest_priority(actual_billing_journey, monkeypatch):
    from config.settings import settings
    from src.auth.service import authenticate_token
    from src.llm_runtime import completion_with_fallback, preflight_governed_completion_target_async
    from src.model_fabric.caller_context import build_canonical_inference_context
    from src.model_fabric.remote_inference_admission import get_remote_inference_admission_broker
    client, factory, jobs, dispatcher, controls, _engine = actual_billing_journey
    assert (await client.post('/api/auth/login', json={'password': 'research-vertical-private-secret'})).status_code == 200
    saved = await client.put('/api/settings/model-fabric', json={'expected_policy_revision': 1,
        'openrouter_setup': _v2_setup_payload(api_key='private-intercepted-or-key', max_outstanding_per_owner=16)})
    assert saved.status_code == 200, saved.text
    task_id, _owner = await create_actual_near_task(client, factory, expected_policy_revision=3)
    canary = {'profile_id': 'openrouter.text', 'capability': 'text', 'timeout_seconds': 45}
    for capability in ('text', 'health', 'latency_ms'):
        proof = await client.post('/api/settings/model-fabric/canary', json={**canary, 'capability': capability})
        assert proof.status_code == 200 and proof.json()['outcome'] == 'passed', proof.text
        assert proof.json()['proof_persistence'] == 'persisted'
    operator = await authenticate_token(client.cookies.get(settings.operator_auth_cookie_name), touch=False)
    original_sync = httpx.Client
    def ordinary_provider(request):
        assert request.method == 'POST' and str(request.url) == 'https://openrouter.ai/api/v1/chat/completions'
        assert request.headers['authorization'] == 'Bearer private-intercepted-or-key'
        body = json.loads(request.content)
        assert body['model'] == 'fixture/text'
        label = body['messages'][0]['content']
        assert label in ('ordinary-low', 'ordinary-high')
        controls['active'] += 1
        controls['peak'] = max(controls['peak'], controls['active'])
        controls['order'].append(label)
        try:
            return httpx.Response(200, request=request, json={'id': label, 'model': 'fixture/text', 'provider': 'fixture',
                'choices': [{'message': {'role': 'assistant', 'content': label}}],
                'usage': {'cost': '0.000003', 'prompt_tokens': 1, 'completion_tokens': 1}})
        finally:
            controls['active'] -= 1
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: original_sync(**{**kwargs, 'transport': httpx.MockTransport(ordinary_provider)}))
    async def ordinary(route, label):
        messages = [{'role': 'user', 'content': label}]
        context = build_canonical_inference_context(route, payload=messages, output_tokens=32, timeout_seconds=45,
            principal=operator.principal, session_id=operator.session_id)
        no_contact_reason = await preflight_governed_completion_target_async(
            runtime_path=route, profile=None, request_context=context)
        assert no_contact_reason is None, (route, no_contact_reason)
        return await completion_with_fallback(messages=messages, temperature=0, max_tokens=32, timeout=45,
            runtime_path=route, request_context=context)
    controls['hold_or'] = True
    controls['hold_or_index'] = 4
    held = asyncio.create_task(client.post('/api/settings/model-fabric/canary', json=canary))
    workers = [held]
    try:
        await asyncio.wait_for(controls['or_started'].wait(), 10)
        def queued_routes():
            broker = get_remote_inference_admission_broker()
            with broker._condition:
                return [broker._operations[item].request.runtime_path for item in broker._queue]
        async def wait_ready(route, worker):
            await wait_until(lambda: route in queued_routes(), what=f'{route} queued in the shared lane',
                             task=worker, state=queued_routes)
        low = asyncio.create_task(ordinary('session_title_generation', 'ordinary-low'))
        workers.append(low)
        await wait_ready('session_title_generation', low)
        high = asyncio.create_task(ordinary('chat_agent', 'ordinary-high'))
        workers.append(high)
        await wait_ready('chat_agent', high)
        native = asyncio.create_task(drive(dispatcher))
        workers.append(native)
        await wait_ready('near_text_native', native)
        # Each durable reservation precedes its enqueue, and all three are
        # observed queued behind the held contact, so their rows exist now.
        async with factory.accounting_sessions() as db:
            rows = list((await db.scalars(select(InferenceCostReservation))).all())
        pending = [row for row in rows if row.state == 'reserved']
        assert len(pending) == 3, [(row.runtime_path, row.state, row.priority) for row in rows]
        priorities = {row.runtime_path: row.priority for row in pending}
        assert priorities == {'session_title_generation': 2, 'chat_agent': 5, 'near_text_native': 5}
        assert get_remote_inference_admission_broker().max_outstanding_per_owner == 16
        assert controls['order'] == ['or-1', 'or-2', 'or-3', 'or-4'] and controls['peak'] == 1
        controls['or_release'].set()
        await asyncio.wait_for(asyncio.gather(*workers), 35)
        assert held.result().status_code == 200 and held.result().json()['outcome'] == 'passed'
        assert low.result().choices[0].message.content == 'ordinary-low'
        assert high.result().choices[0].message.content == 'ordinary-high'
        after_hold = controls['order'][4:]
        assert set(after_hold[:2]) == {'ordinary-high', 'near'} and after_hold[2] == 'ordinary-low'
        assert controls['peak'] == 1
        task, run, row = await state(factory, task_id)
        assert task.status is WorkBoardStatus.review and run.status == 'succeeded' and row.actual_cost_microusd == 2
        snapshot = await jobs.inference_accounting_snapshot()
        assert snapshot['operation_count'] == 7 and snapshot['spent_microusd'] == 20
        assert snapshot['unpriced_requests'] == 0
    finally:
        controls['or_release'].set()
        for worker in workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
