"""Actual Goal/watch/native/SQLite/Inbox journey; HTTP boundaries intercepted."""
import json
import asyncio
import pytest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from fastapi import FastAPI
from sqlalchemy import select, func

from tests.test_inference_accounting import accounting_db, setup_configuration
from tests.test_research_native_vertical import real_auth, ResponseBytes
from src.auth.middleware import OperatorAuthMiddleware
from src.db.models import GuardianOpportunity, GuardianIntervention, InferenceCostReservation, NativeNotificationOutbox
from src.guardian.opportunity_runtime import execute_assessment, admit_assessment, assessment_context, read_snapshot
from src.workflows.job_runtime import durable_job_repository


class OpportunityHttpBoundary(httpx.AsyncBaseTransport):
    def __init__(self, calls, controls):
        self.calls = calls
        self.controls = controls

    async def handle_async_request(self, request):
        assert request.url.host == "openrouter.ai"
        assert request.method == "POST" and request.url.path == "/api/v1/chat/completions"
        body = json.loads(request.content)
        self.calls.append(body)
        if len(body["messages"]) == 1:
            if self.controls.get("hold_canary"):
                await self.controls["hold_canary"]()
            content = '{"ok":true}' if "response_format" in body else "CANARY_OK"
        else:
            if self.controls.get("after_contact"):
                await self.controls["after_contact"]()
            offered = json.loads(body["messages"][1]["content"])
            source = offered["public_evidence"]["sources"][0]
            content = json.dumps(dict(schema_version="seraph.opportunity.assessment.v1",
                relevance=3, confidence="medium", summary="A relevant cited public release is available.",
                reason="The supplied public release changed and warrants an operator review.",
                citations=[dict(source_id=source["source_key"], start_line=1,
                    end_line=len(source["excerpt"].split("\n")), span_sha256=source["excerpt_sha256"])],
                suggested_blueprint="public-browser-check", abstain_reason=None))
            model = json.loads(content)
            scenario = self.controls.get("scenario")
            if scenario == "silent":
                model["relevance"] = 2
            elif scenario == "citation_tampered":
                model["citations"][0]["span_sha256"] = "0" * 64
            elif scenario == "invented_reference":
                model["reason"] = "Execute https://invented.example/action now."
            elif scenario == "pii_output":
                model["summary"] = "Contact private.person@example.com now."
            elif scenario == "secret_output":
                model["summary"] = "Secret value is correcthorse956batterystaple."
            content = json.dumps(model)
        payload = dict(id="intercepted-opportunity-"+str(len(self.calls)),
            choices=[dict(message=dict(role="assistant", content=content))],
            usage=dict(cost="0.000002", prompt_tokens=10, completion_tokens=10))
        return httpx.Response(200, request=request, headers={"content-type": "application/json"},
            stream=ResponseBytes(json.dumps(payload).encode()))


@pytest.mark.parametrize("scenario", ["completed", "silent", "citation_tampered", "invented_reference",
    "pii_output", "secret_output", "missing_snapshot", "cancel_contacted", "coalesce_uncontacted", "outstanding_one", "cancel_uncontacted", "startup_recovery"])
async def test_actual_http_goal_watch_native_cited_inbox(accounting_db, real_auth, monkeypatch, scenario):
    from src.api import auth, goals, model_fabric_settings
    from src.guardian import source_watch, inbox
    from src.db.models import Goal
    from src.model_fabric.configuration import write_model_fabric_configuration
    from src.security.http_transport import fetch_pinned_https
    from src.security.trust_contract import ContentOrigin
    from src.model_fabric.gpu_admission import GpuPriority
    from src.model_fabric import remote_inference_admission
    root, _, factory = accounting_db
    monkeypatch.setattr("src.model_fabric.execution.gpu_admission_broker",
        remote_inference_admission.remote_inference_admission_broker)
    configured = setup_configuration()
    write_model_fabric_configuration(replace(model_fabric_settings._setup_configuration(
        replace(configured.openrouter_setup, timeout_seconds=30, capabilities=("text", "structured_output")),
        profiles=(), policies=()), egress_revision=configured.egress_revision+1))
    await durable_job_repository.configure_inference_accounting(1000)
    calls, source_calls = [], []
    controls = {"scenario": scenario}
    if scenario == "completed":
        # The reviewed snooze must fit a real finite Root. The shared research
        # fixture's five-minute Root deliberately cannot grant a 20min snooze.
        from config.settings import settings
        monkeypatch.setattr(settings, "operator_auth_idle_seconds", 3600)
    original_client = httpx.AsyncClient
    def clients(**kwargs):
        if kwargs.get("transport") is None:
            kwargs["transport"] = OpportunityHttpBoundary(calls, controls)
        return original_client(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", clients)
    versions = iter(("Stable public line\nPrevious public release\n",
        "Stable public line\nA relevant new public release\n",
        "Stable public line\nA newer relevant public release with material detail\n"))
    async def public_http(request):
        assert request.method == "GET" and request.url == "https://example.com/public"
        source_calls.append(str(request.url))
        return httpx.Response(200, request=request, headers={"content-type": "text/plain"},
            stream=ResponseBytes(next(versions).encode()))
    async def pinned_http(url, **kwargs):
        # Supply only the production constructor seams; URL validation,
        # current authority, byte bounds and readback remain actual code.
        return await fetch_pinned_https(url, resolver=lambda host, port: ["93.184.216.34"],
            transport=httpx.MockTransport(public_http), **kwargs)
    monkeypatch.setattr(source_watch, "fetch_pinned_https", pinned_http)
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(goals.router, prefix="/api")
    app.include_router(model_fabric_settings.router, prefix="/api")
    app.include_router(source_watch.source_watch_router, prefix="/api")
    async with original_client(transport=httpx.ASGITransport(app=app), base_url="http://test",
            headers={"origin": "http://localhost:3001"}) as client:
        login = await client.post("/api/auth/login", json={"password": "research-vertical-private-secret"})
        assert login.status_code == 200, login.text
        owner = login.json()
        if scenario == "secret_output":
            from src.vault.repository import vault_repository
            await vault_repository.store("956-private-boundary", "correcthorse956batterystaple",
                owner_principal_id=owner["principal_id"])
        for capability in ("text", "structured_output", "latency_ms", "health"):
            proof = await client.post("/api/settings/model-fabric/canary", json=dict(
                profile_id="openrouter", capability=capability, timeout_seconds=30))
            assert proof.status_code == 200, proof.text
            assert proof.json()["outcome"] == "passed", proof.json()
        current = datetime.now(timezone.utc)
        created = await client.post("/api/goals", json=dict(title="Review cited public release changes",
            proactive_enabled=True, admission_budget=dict(reviewed_grant=True, grant_id="opportunity-review",
                max_outstanding_jobs=1 if scenario == "outstanding_one" else 2, max_attempts=2, max_runtime_seconds=300,
                period_started_at=current.isoformat(), period_expires_at=(current+timedelta(hours=1)).isoformat())))
        assert created.status_code == 200, created.text
        goal = created.json()
        watch_response = await client.post("/api/capabilities/source-watches", json=dict(goal_id=goal["id"],
            expected_goal_revision=goal["revision"], sources=[dict(source_key="public", kind="public_https_text",
                target="https://example.com/public", label="Public release", priority=1)], criteria={},
            schedule=dict(cron="0 * * * *", timezone="UTC"), write_mode="standing_reviewed",
            reviewed_grant_id="opportunity-review"))
        assert watch_response.status_code == 200, watch_response.text
        watch = watch_response.json()
        saved = await client.put(f"/api/goals/{goal['id']}/guardian-policy", json=dict(
            expected_goal_revision=goal["revision"], expected_policy_revision=0, idempotency_key=str(uuid4()),
            policy=dict(schema_version="seraph.guardian.policy.v1", assessment_enabled=True,
                confirmed_at=current.isoformat(), review_due_at=(current+timedelta(hours=1)).isoformat(),
                grant_id="opportunity-review", original_root_id=owner["session_id"], goal_revision=goal["revision"],
                source_watch_ids=[watch["id"]], max_assessments_per_utc_day=2)))
        assert saved.status_code == 200, saved.text
        service = source_watch.SourceWatchService()
        baseline = await service.run_watch(watch["id"], occurrence_id="actual-opportunity-baseline",
            expected_plan_revision=1, expected_owner_session_id=owner["session_id"])
        assert baseline["status"] == "baseline_initialized", baseline
        material = await service.run_watch(watch["id"], occurrence_id="actual-opportunity-material",
            expected_plan_revision=1, expected_owner_session_id=owner["session_id"])
        assert material["status"] == "succeeded", material
        async with factory.accounting_sessions() as db:
            row = (await db.execute(select(GuardianOpportunity))).scalar_one()
            current_goal = await db.get(Goal, goal["id"])
        offered = read_snapshot(json.loads(row.source_token_json)["artifact_id"], row.source_digest)
        from src.guardian.opportunity_contracts import VerifiedSourcePacket
        from src.guardian.opportunities import publish_verified_packet
        repeated = await asyncio.gather(*(publish_verified_packet(VerifiedSourcePacket(
            packet_id=row.source_packet_id, watch_revision=row.watch_revision,
            goal_revision=row.goal_revision)) for _ in range(2)))
        assert repeated == [row.id, row.id]
        admissions = await asyncio.gather(admit_assessment(row.id), admit_assessment(row.id))
        assert admissions[0]["job_id"] == admissions[1]["job_id"]
        async with factory.accounting_sessions() as db:
            row = await db.get(GuardianOpportunity, row.id)
        messages, context = assessment_context(row, current_goal, offered)
        assert {p.origin for p in context.provenance} == {ContentOrigin.CANONICAL_MEMORY, ContentOrigin.EXTERNAL_UNTRUSTED}
        assert all(p.instruction_authority is False for p in context.provenance)
        if scenario == "startup_recovery":
            from src.guardian.opportunity_runtime import _authority, RUNNER, run_opportunity_tick, _executions
            from src.db.models import WorkflowRunState
            queued = await durable_job_repository.get_job(row.job_id)
            async def claim(db, run):
                pending, _ = await _authority(db, row.id, execution=True)
                pending.status, pending.revision = "assessing", pending.revision + 1
                db.add(pending)
            claimed = await durable_job_repository.claim_job(row.job_id, owner=RUNNER,
                expected_revision=queued["revision"], claim_authority_check=claim)
            async with factory.accounting_sessions() as db:
                native = (await db.execute(select(WorkflowRunState).where(
                    WorkflowRunState.run_identity == row.job_id))).scalar_one()
                native.lease_expires_at = datetime.now(timezone.utc)-timedelta(seconds=1)
                db.add(native)
            recovered = next(job for job in await durable_job_repository.recover_stale_jobs()
                if job["job_id"] == row.job_id)
            assert recovered["status"] == "blocked" and recovered["effects"] == []
            assert recovered["lease"]["fencing_token"] > claimed["lease"]["fencing_token"]
            assert (await run_opportunity_tick())["started"] == 1
            await asyncio.wait_for(_executions[row.id], timeout=30)
            retried = await durable_job_repository.get_job(row.job_id)
            assert retried["attempt_count"] == 2 and retried["deadline_at"] == recovered["deadline_at"]
            assert retried["lease"]["fencing_token"] > recovered["lease"]["fencing_token"]
        if scenario in {"coalesce_uncontacted", "outstanding_one", "cancel_uncontacted"}:
            from src.guardian.opportunity_runtime import run_opportunity_tick, _executions
            entered, release = asyncio.Event(), asyncio.Event()
            async def hold_canary():
                entered.set()
                await release.wait()
            controls["hold_canary"] = hold_canary
            canary = asyncio.create_task(client.post("/api/settings/model-fabric/canary", json=dict(
                profile_id="openrouter", capability="health", timeout_seconds=30)))
            await asyncio.wait_for(entered.wait(), timeout=20)
            assert (await run_opportunity_tick())["started"] == 1
            active = _executions[row.id]
            async def wait_for_native_queue():
                while True:
                    state = await remote_inference_admission.remote_inference_admission_broker.status()
                    if any(item["job_id"] == row.job_id for item in state["queued"]):
                        return
                    await asyncio.sleep(0.02)
            await asyncio.wait_for(wait_for_native_queue(), timeout=20)
            async with factory.accounting_sessions() as db:
                assert (await db.get(GuardianOpportunity, row.id)).status == "assessing"
                old_cost = (await db.execute(select(InferenceCostReservation).where(
                    InferenceCostReservation.job_id == row.job_id))).scalar_one()
                assert old_cost.contact_started_at is None
            assert (await durable_job_repository.get_job(row.job_id))["status"] == "running"
            if scenario == "cancel_uncontacted":
                async with factory.accounting_sessions() as db:
                    pending = await db.get(GuardianOpportunity, row.id)
                request = dict(expected_opportunity_revision=pending.revision, idempotency_key=str(uuid4()))
                cancelled = await client.post(f"/api/guardian/opportunities/{row.id}/cancel", json=request)
                assert cancelled.status_code == 200, cancelled.text
                assert cancelled.json()["quiescent"] is True and cancelled.json()["status"] == "cancelled"
                replay = await client.post(f"/api/guardian/opportunities/{row.id}/cancel", json=request)
                assert replay.status_code == 200 and replay.json() == cancelled.json()
                assert active.done() and row.id not in _executions
                assert (await durable_job_repository.get_job(row.job_id))["status"] == "cancelled"
                controls.pop("hold_canary")
                release.set()
                proof = await asyncio.wait_for(canary, timeout=20)
                assert proof.status_code == 200 and proof.json()["outcome"] == "passed", proof.text
                assert (await run_opportunity_tick())["started"] == 0
                async with factory.accounting_sessions() as db:
                    cost = (await db.execute(select(InferenceCostReservation).where(
                        InferenceCostReservation.job_id == row.job_id))).scalar_one()
                    assert cost.contact_started_at is None and cost.state == "released"
                    assert (await db.execute(select(func.count()).select_from(GuardianIntervention))).scalar() == 0
                    assert (await db.execute(select(func.count()).select_from(NativeNotificationOutbox))).scalar() == 0
                assert len(source_calls) == 2 and len(calls) == 5
                assert all(len(call["messages"]) == 1 for call in calls)
                (root/"actual-opportunity-uncontacted-cancel-readback.json").write_text(json.dumps(dict(
                    cancellation=cancelled.json(), cost_state=cost.state, provider_contacts=len(calls),
                    assessment_contacts=0, source_contacts=len(source_calls)), default=str))
                return
            newer = await service.run_watch(watch["id"], occurrence_id="actual-opportunity-newest",
                expected_plan_revision=1, expected_owner_session_id=owner["session_id"])
            if scenario == "outstanding_one":
                # The existing source-watch owner projects the native denial
                # class; the finite cap must still stop before any source GET.
                assert newer["status"] == "blocked" and newer["reason_code"] == "DurableJobAdmissionDenied", newer
                assert await durable_job_repository.get_job(newer["job_id"]) is None
                assert len(source_calls) == 2 and not active.done()
            else:
                assert newer["status"] == "succeeded", newer
                assert active.done() and row.id not in _executions
                assert (await durable_job_repository.get_job(row.job_id))["status"] == "cancelled"
                async with factory.accounting_sessions() as db:
                    old = await db.get(GuardianOpportunity, row.id)
                    assert old.status == "silent" and old.reason_code == "coalesced"
                    old_cost = (await db.execute(select(InferenceCostReservation).where(
                        InferenceCostReservation.job_id == row.job_id))).scalar_one()
                    assert old_cost.contact_started_at is None and old_cost.state == "released"
                    row = (await db.execute(select(GuardianOpportunity).where(
                        GuardianOpportunity.status == "queued"))).scalar_one()
                    offered = read_snapshot(json.loads(row.source_token_json)["artifact_id"], row.source_digest)
            controls.pop("hold_canary")
            release.set()
            proof = await asyncio.wait_for(canary, timeout=20)
            assert proof.status_code == 200 and proof.json()["outcome"] == "passed", proof.text
            if scenario == "outstanding_one":
                await asyncio.wait_for(active, timeout=30)
            else:
                assert (await run_opportunity_tick())["started"] == 1
                await asyncio.wait_for(_executions[row.id], timeout=30)
        if scenario == "missing_snapshot":
            async def remove_immutable_evidence():
                (root/json.loads(row.source_token_json)["artifact_id"]).unlink()
            controls["after_contact"] = remove_immutable_evidence
        if scenario == "cancel_contacted":
            from src.guardian.opportunity_runtime import run_opportunity_tick, _executions
            contacted, closed = asyncio.Event(), asyncio.Event()
            async def hold_transfer():
                contacted.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    closed.set()
            controls["after_contact"] = hold_transfer
            assert (await run_opportunity_tick())["started"] == 1
            await asyncio.wait_for(contacted.wait(), timeout=40)
            active = _executions[row.id]
            async with factory.accounting_sessions() as db:
                pending = await db.get(GuardianOpportunity, row.id)
            newer = await service.run_watch(watch["id"], occurrence_id="actual-contacted-newer",
                expected_plan_revision=1, expected_owner_session_id=owner["session_id"])
            assert newer["status"] == "succeeded", newer
            async with factory.accounting_sessions() as db:
                old = await db.get(GuardianOpportunity, row.id)
                latest = (await db.execute(select(GuardianOpportunity).where(GuardianOpportunity.id != row.id))).scalar_one()
                assert old.status == "assessing" and old.revision == pending.revision
                assert latest.status == "blocked" and latest.reason_code == "opportunity_capacity_exhausted"
            response = await client.post(f"/api/guardian/opportunities/{row.id}/cancel", json=dict(
                expected_opportunity_revision=pending.revision, idempotency_key=str(uuid4())))
            assert response.status_code == 200, response.text
            assert response.json()["quiescent"] is True and response.json()["status"] == "unknown"
            assert closed.is_set() and active.done()
            assert (await run_opportunity_tick())["started"] == 0 and len(calls) == 5
        elif scenario == "completed":
            from src.scheduler.engine import _async_job_wrapper
            from src.guardian.opportunity_runtime import run_opportunity_tick, _executions
            wrapper = _async_job_wrapper(run_opportunity_tick, asyncio.get_running_loop(),
                job_id="guardian_opportunity_assessment", allow_model_inference=True)
            await wrapper()
            await asyncio.wait_for(_executions[row.id], timeout=50)
        elif scenario not in {"coalesce_uncontacted", "outstanding_one", "startup_recovery"}:
            await execute_assessment(row.id)
        async with factory.accounting_sessions() as db:
            final = await db.get(GuardianOpportunity, row.id)
            expected = "silent" if scenario == "silent" else "unknown" if scenario == "cancel_contacted" else (
                "proposed" if scenario in {"completed", "coalesce_uncontacted", "outstanding_one", "startup_recovery"} else "blocked")
            assert final.status == expected, final.reason_code
            if scenario not in {"completed", "coalesce_uncontacted", "outstanding_one", "startup_recovery"}:
                assert final.intervention_id is None
                assert (await db.execute(select(func.count()).select_from(GuardianIntervention))).scalar() == 0
                assert (await db.execute(select(func.count()).select_from(NativeNotificationOutbox))).scalar() == 0
                cost = (await db.execute(select(InferenceCostReservation).where(
                    InferenceCostReservation.job_id == final.job_id))).scalar_one()
                assert cost.state == ("unknown" if scenario == "cancel_contacted" else "settled")
                assert cost.contact_started_at is not None
                detail = await inbox.get_owned_item(owner_principal_id=owner["principal_id"],
                    owner_session_id=owner["session_id"], item_id=row.id)
                assert detail["allowed_actions"] == []
                if scenario == "cancel_contacted":
                    assert detail["quiescent"] is True and detail["cancel_requested"] is True
                    assert detail["cancel_allowed"] is False
                assert len(source_calls) == (3 if scenario == "cancel_contacted" else 2) and len(calls) == 5
                (root/f"actual-opportunity-{scenario}-readback.json").write_text(json.dumps(dict(
                    detail=detail, state=cost.state, contacts=len(calls)), default=str))
                return
            intervention = await db.get(GuardianIntervention, final.intervention_id)
            assert intervention.opportunity_id == row.id and intervention.delivery_status == "not_requested"
            costs = list((await db.execute(select(InferenceCostReservation).where(
                InferenceCostReservation.job_id == final.job_id))).scalars())
            assert len(costs) == 1 and costs[0].priority == GpuPriority.REPORTS_RESEARCH_MEMORY.rank
            assert costs[0].state == "settled" and costs[0].contact_started_at is not None
            assert (await db.execute(select(func.count()).select_from(NativeNotificationOutbox))).scalar() == 0
        native = await durable_job_repository.get_job(final.job_id)
        assert native["status"] == "succeeded"
        assert "no learning" in native["result"]["summary"]
        detail = await inbox.get_owned_item(owner_principal_id=owner["principal_id"],
            owner_session_id=owner["session_id"], item_id=row.id)
        assert detail["source_kind"] == "guardian_opportunity" and detail["verification_status"] == "passed"
        assert detail["assessment"]["citations"][0]["span_sha256"] == offered.sources[0].excerpt_sha256
        assert len(source_calls) == (3 if scenario == "coalesce_uncontacted" else 2)
        assert len(calls) == (6 if scenario in {"coalesce_uncontacted", "outstanding_one"} else 5)
        assert calls[-1]["stream"] is False and calls[-1]["max_tokens"] == 1024 and "tools" not in calls[-1]
        (root/"actual-opportunity-readback.json").write_text(json.dumps(dict(native=native,
            inbox=detail, source_contacts=len(source_calls), provider_contacts=len(calls)), default=str))
        if scenario in {"coalesce_uncontacted", "outstanding_one", "startup_recovery"}:
            assert sum(len(call["messages"]) == 2 for call in calls) == 1
            assert detail["assessment"]["citations"][0]["span_sha256"] == offered.sources[0].excerpt_sha256
            return
        from src.guardian import feedback
        monkeypatch.setattr(feedback, "get_session", factory.accounting_sessions)
        async def forbid_memory_refresh(**kwargs):
            raise AssertionError("opportunity feedback cannot refresh canonical memory")
        monkeypatch.setattr(feedback.guardian_feedback_repository, "_refresh_learning_memories", forbid_memory_refresh)
        await feedback.guardian_feedback_repository.record_feedback(final.intervention_id,
            feedback_type="helpful", owner_principal_id=owner["principal_id"], original_root_id=owner["session_id"])
        learning = await feedback.guardian_feedback_repository.get_learning_signal(intervention_type="opportunity")
        assert learning.helpful_count == 0 and learning.not_helpful_count == 0
        snooze_request = dict(owner_principal_id=owner["principal_id"], owner_session_id=owner["session_id"],
            item_id=detail["id"], action="snooze", expected_revision=detail["revision"],
            idempotency_key="actual-opportunity-snooze", until=datetime.now(timezone.utc)+timedelta(minutes=20))
        snoozed = await inbox.apply_action(**snooze_request)
        assert snoozed["state"] == "snoozed"
        assert await inbox.apply_action(**snooze_request) == snoozed
        detail = await inbox.get_owned_item(owner_principal_id=owner["principal_id"],
            owner_session_id=owner["session_id"], item_id=row.id)
        assert detail["allowed_actions"] == [] and detail["snoozed_until"] is not None
        assert len(detail["action_history"]) == 1
        assert len(calls) == 5 and len(source_calls) == 2
