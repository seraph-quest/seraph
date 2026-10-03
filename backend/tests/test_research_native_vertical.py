"""Actual authenticated Board/native/SQLite/source/artifact vertical.

Only OpenRouter HTTP requests are intercepted; source GETs use the real
existing pinned public transport. This is backend acceptance, not managed UI.
"""
import json
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from config.settings import settings
from tests.test_inference_accounting import accounting_db, setup_configuration
from src.auth.middleware import OperatorAuthMiddleware
from src.db.models import Goal, WorkBoardAttempt, WorkBoardTask, WorkflowRunState
from src.goals.contracts import GoalAdmissionBudget
from src.goals.repository import serialize_admission_budget
from src.work_board.dispatcher import WorkBoardDispatcher
from src.workflows.job_runtime import DurableJobRepository


@pytest.fixture
def real_auth(monkeypatch):
    from src.api.auth import _reset_login_throttle_for_tests
    _reset_login_throttle_for_tests()
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "research-vertical-private-secret")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test,localhost,127.0.0.1")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    monkeypatch.setattr(settings, "operator_auth_idle_seconds", 300)
    monkeypatch.setattr(settings, "operator_auth_absolute_seconds", 3600)
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)
    monkeypatch.setattr(settings, "openrouter_api_key", "intercepted-provider-boundary-only")
    # Each accounting_db fixture is a distinct deployment/Root. Its real
    # process-local broker must also be distinct, as on an actual fresh
    # launcher. The canonical financial ledger/witness is never reset or
    # forgiven; every broker still uses production durable accounting.
    from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    monkeypatch.setattr("src.model_fabric.remote_inference_admission.remote_inference_admission_broker", broker)
    monkeypatch.setattr("src.llm_runtime.gpu_admission_broker", broker)
    monkeypatch.setattr("src.api.model_fabric_settings.remote_inference_admission_broker", broker)
    yield
    _reset_login_throttle_for_tests()


class ResponseBytes(httpx.AsyncByteStream):
    def __init__(self, content):
        self.content = content

    async def __aiter__(self):
        for offset in range(0, len(self.content), 512):
            yield self.content[offset:offset+512]


class ProviderBoundary(httpx.AsyncBaseTransport):
    def __init__(self, calls, controls):
        self.calls = calls
        self.controls = controls
        self.public = httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        if request.url.host == "openrouter.ai":
            assert request.method == "POST" and request.url.path == "/api/v1/chat/completions"
            body = json.loads(request.content)
            self.calls.append(body)
            if len(body["messages"]) == 1:
                content = "CANARY_OK"
            else:
                if len(self.calls) == 4 and self.controls.get("after_first_contact"):
                    await self.controls["after_first_contact"]()
                supplied = json.loads(body["messages"][1]["content"])
                source = supplied["untrusted_quoted_sources"][0]
                content = json.dumps({"schema_version": 1, "perspective": supplied["perspective_instruction"],
                    "claims": [{"text": "The selected public text establishes this attributed evidence.",
                        "citations": [{key: source[key] for key in ("source_id", "first_line", "last_line", "span_sha256")}]}],
                    "uncertainty": ["Mechanical citation validation does not establish semantic truth."],
                    "contradictions": [], "no_learning": True})
            payload = {"id": "intercepted-"+str(len(self.calls)),
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"cost": "0.000150" if len(self.calls) == 4 and self.controls.get("overrun") else "0.000002",
                    "prompt_tokens": 10, "completion_tokens": 10}}
            return httpx.Response(200, request=request, headers={"content-type": "application/json"},
                stream=ResponseBytes(json.dumps(payload).encode()))
        # Every actual provider contact stays intercepted. The only real
        # external action permitted by this test is its explicitly selected
        # finite public source; DNS/IP pinning remains production code.
        assert request.method == "GET" and request.url.scheme == "https"
        return await self.public.handle_async_request(request)

    async def aclose(self):
        await self.public.aclose()


@pytest.mark.asyncio
async def test_authenticated_parent_two_children_real_public_source_and_dossier(accounting_db, real_auth, monkeypatch, *, scenario="completed"):
    from src.api import auth, work_board, model_fabric_settings, goals
    from src.model_fabric.configuration import write_model_fabric_configuration
    root, engine, factory = accounting_db
    configured = setup_configuration()
    write_model_fabric_configuration(replace(model_fabric_settings._setup_configuration(replace(configured.openrouter_setup, timeout_seconds=30),
        profiles=(), policies=()), egress_revision=configured.egress_revision+1))
    jobs = DurableJobRepository()
    await jobs.configure_inference_accounting(1000)
    calls = []
    controls = {"overrun": scenario == "overrun_first_response"}
    original_client = httpx.AsyncClient
    def clients(**kwargs):
        if "transport" not in kwargs:
            kwargs["transport"] = ProviderBoundary(calls, controls)
        return original_client(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", clients)
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(work_board.router, prefix="/api")
    app.include_router(model_fabric_settings.router, prefix="/api")
    app.include_router(goals.router, prefix="/api")
    headers = {"origin": "http://localhost:3001"}
    async with original_client(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers) as client:
        denied = await client.get("/api/work-board/tasks")
        assert denied.status_code == 401
        login = await client.post("/api/auth/login", json={"password": "research-vertical-private-secret"})
        assert login.status_code == 200, login.text
        owner = login.json()
        assert owner["principal_id"].startswith("operator:root:")
        for capability in ("text", "latency_ms", "health"):
            proof = await client.post("/api/settings/model-fabric/canary", json={
                "profile_id": "openrouter", "capability": capability, "timeout_seconds": 30})
            assert proof.status_code == 200, proof.text
            assert proof.json()["outcome"] == "passed" and proof.json()["proof_persistence"] == "persisted", proof.json()
        async with factory.accounting_sessions() as db:
            db.add(Goal(id="actual-research-goal", title="Finite public evidence research", status="active", revision=1,
                owner_principal_id=owner["principal_id"], owner_session_id=owner["session_id"],
                admission_budget_json=serialize_admission_budget(GoalAdmissionBudget(reviewed_grant=True,
                    grant_id="research-native-review", max_outstanding_jobs=1, max_attempts=1, max_runtime_seconds=300))))
        inputs = {"schema_version": 1, "question": "What does the selected software license state?",
            "perspectives": [{"instruction": "Summarize supplied evidence", "source_slots": [0]},
                {"instruction": "Describe uncertainty", "source_slots": [0]}],
            "sources": [{"kind": "public_https_text", "url": "https://raw.githubusercontent.com/python/cpython/v3.12.8/LICENSE",
                "first_line": 3, "last_line": 8}], "source_egress_acknowledged": True, "no_learning": True}
        artifact = await client.post("/api/work-board/input-artifacts", json={"schema_version": 1,
            "capability_id": "work.research-dossier.v1", "goal_id": "actual-research-goal", "goal_revision": 1,
            "input": inputs, "idempotency_key": "actual-research-input"})
        assert artifact.status_code == 200, artifact.text
        created = await client.post("/api/work-board/tasks", json={"title": "Actual bounded research",
            "goal_id": "actual-research-goal", "goal_revision": 1, "status": "todo",
            "capability_id": "work.research-dossier.v1", "input_artifact_id": artifact.json()["artifact_id"],
            "idempotency_key": "actual-research-task"})
        assert created.status_code == 200, created.text
        task_id = created.json()["task"]["task_id"]
        async def change_current_authority():
            if scenario == "revoke_first_response":
                revoked = await client.post("/api/auth/logout")
                assert revoked.status_code == 204
            elif scenario == "goal_change_first_response":
                changed = await client.patch("/api/goals/actual-research-goal", json={"title": "Changed current Goal", "expected_revision": 1})
                assert changed.status_code == 200, changed.text
            elif scenario == "tamper_source_first_response":
                from src.workflows.research_native import checkpoint
                async with factory.accounting_sessions() as db:
                    producer = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind == "readonly_research_child",
                        WorkflowRunState.run_identity.like("%:child:0")))
                    from src.workflows.job_runtime import _serialize
                    source = checkpoint(_serialize(producer), "research:artifact:source:0")
                # Actual disposable-fixture attack on the already verified
                # source file; no canonical hash/receipt is altered to match it.
                (root/source["file_path"]).write_bytes(b"tampered source after first provider contact")
        controls["after_first_contact"] = change_current_authority
        dispatcher = WorkBoardDispatcher(jobs=jobs, session_provider=factory.accounting_sessions)
        recovery_scenarios = {"restart_before_sources", "restart_prompt_ready", "restart_funded_queued", "restart_written_outputs", "cancel_funded_queued"}
        if scenario in recovery_scenarios:
            from src.workflows import research_coordinator as coordinator
            from src.workflows.research_accounting import fund_fixed_group
            from src.workflows.research_native import checkpoint
            from src.workflows.research_waits import resume_parent, pause_parent
            from src.workflows import research_provider as provider
            original_continue = coordinator.continue_parent
            async def interrupted_process(jobs, *, parent_id, owner, phase_binding):
                creation = checkpoint(await jobs.get_job(parent_id), "research:creation")
                if scenario != "restart_before_sources":
                    for child_id in creation["child_ids"]:
                        await coordinator._prepare_child(jobs, child_id, owner, {}, phase_binding)
                if scenario in {"restart_funded_queued", "restart_written_outputs", "cancel_funded_queued"}:
                    phase = await resume_parent(jobs, parent_id=parent_id, owner=owner,
                        phase="research_funding", expected_binding=phase_binding)
                    phase_binding.update(phase)
                    await fund_fixed_group(jobs, parent_id=parent_id, owner=owner, fencing_token=phase["job_fence"])
                    phase_binding.update(await pause_parent(jobs, parent_id=parent_id, owner=owner,
                        job_fence=phase["job_fence"], board_fence=phase["board_fence"],
                        board_revision=phase["task_revision"], reason="research_wait_children"))
                    for child_id in creation["child_ids"]:
                        child = await jobs.get_job(child_id)
                        await jobs.resume_job(child_id, expected_revision=child["revision"], reason="research_group_funded")
                    if scenario == "restart_written_outputs":
                        original_adopt = provider._adopt_child
                        async def interrupted_adoption(*args, **kwargs):
                            raise RuntimeError("declared crash boundary after actual reserved child output")
                        monkeypatch.setattr(provider, "_adopt_child", interrupted_adoption)
                        try:
                            for child_id in creation["child_ids"]:
                                claimed = await jobs.claim_job(child_id, owner=owner, lease_seconds=1,
                                    continue_existing_attempt=True, claim_authority_check=coordinator._claim_guard(jobs, phase_binding))
                                with pytest.raises(RuntimeError, match="declared crash boundary"):
                                    await provider.execute_funded_child(jobs, child_id=child_id, owner=owner,
                                        fence=claimed["lease"]["fencing_token"])
                            await __import__("asyncio").sleep(1.1)
                        finally:
                            monkeypatch.setattr(provider, "_adopt_child", original_adopt)
                # A declared process interruption leaves the existing canonical
                # rows as written. It issues no native quiescence completion seal.
                raise RuntimeError("declared restart boundary")
            monkeypatch.setattr(coordinator, "continue_parent", interrupted_process)
        receipt = await dispatcher.run_pass()
        if scenario in recovery_scenarios:
            monkeypatch.setattr(coordinator, "continue_parent", original_continue)
            await engine.dispose()
            from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
            fresh_broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
            monkeypatch.setattr("src.model_fabric.remote_inference_admission.remote_inference_admission_broker", fresh_broker)
            monkeypatch.setattr("src.llm_runtime.gpu_admission_broker", fresh_broker)
            monkeypatch.setattr(work_board, "dispatcher", WorkBoardDispatcher(jobs=jobs, session_provider=factory.accounting_sessions))
            state = await client.get("/api/work-board/tasks/"+task_id+"/research")
            assert state.status_code == 200, state.text
            original_state = state.json()
            if scenario == "restart_written_outputs":
                assert original_state["recoverable"] is True  # exact reserved physical output is visible to the Inspector
            action = "cancel" if scenario == "cancel_funded_queued" else "recover"
            if scenario == "restart_funded_queued":
                from src.work_board.research_control import reserve_recovery
                from src.work_board.research_contracts import ResearchControlRequest
                from src.work_board.contracts import WorkBoardOwner
                from src.workflows.job_runtime import DurableJobLeaseError
                original_request = ResearchControlRequest(expected_revision=original_state["task_revision"],
                    idempotency_key="original-research-control")
                current_owner = WorkBoardOwner(principal_id=owner["principal_id"], session_id=owner["session_id"])
                first = await reserve_recovery(jobs, current_owner, task_id, original_request)
                second = await reserve_recovery(jobs, current_owner, task_id, original_request)
                assert second["binding"]["task_revision"] > first["binding"]["task_revision"]
                child_id = original_state["children"][0]["job_id"]
                with pytest.raises(DurableJobLeaseError, match="phase reservation changed"):
                    await jobs.claim_job(child_id, owner="stale-same-owner", lease_seconds=1,
                        continue_existing_attempt=True, claim_authority_check=coordinator._claim_guard(jobs, first["binding"]))
                assert len(calls) == 3  # stale phase produced no provider request
            recovered = await client.post("/api/work-board/tasks/"+task_id+"/research/"+action,
                json={"expected_revision": original_state["task_revision"], "idempotency_key": "original-research-control"})
            assert recovered.status_code == 200, recovered.text
            current_state = recovered.json()["research"]
            assert current_state["parent_id"] == original_state["parent_id"]
            assert current_state["attempt_id"] == original_state["attempt_id"]
            assert current_state["deadline_at"] == original_state["deadline_at"]
            (root/"research-restart-private-readback.json").write_text(json.dumps({"scenario": scenario,
                "original": original_state, "control": recovered.json(), "provider_post_count": len(calls)}, indent=2))
            if action == "cancel":
                assert recovered.json()["cancellation"]["cancelled"] is True
                assert current_state["status"] == "cancelled" and all(child["status"] == "cancelled" for child in current_state["children"])
                assert len(calls) == 3 and all(cost["state"] == "released" for cost in current_state["costs"])
                replay = await client.post("/api/work-board/tasks/"+task_id+"/research/cancel",
                    json={"expected_revision": original_state["task_revision"], "idempotency_key": "original-research-control"})
                assert replay.status_code == 200 and replay.json()["cancellation"]["replayed"] is True
                generic = await client.post("/api/work-board/tasks/"+task_id+"/actions",
                    json={"action": "unblock", "expected_revision": current_state["task_revision"], "resolution": "Explicitly check original research recovery"})
                assert generic.status_code == 409 and "research_original_attempt_required" in generic.text
                assert len(calls) == 3
                return
            assert recovered.json()["recovery"]["completed"] is True, recovered.text
            replay = await client.post("/api/work-board/tasks/"+task_id+"/research/recover",
                json={"expected_revision": original_state["task_revision"], "idempotency_key": "original-research-control"})
            assert replay.status_code == 200 and replay.json()["recovery"]["completed"] is True
            assert len(calls) == 5  # uncertain-response replay never adds another POST
            receipt = {"completed": 1}
        detail = await client.get("/api/work-board/tasks/"+task_id)
        (root/"research-vertical-private-readback.json").write_text(json.dumps({"label": "real public source; provider HTTP interception only",
            "scenario": scenario, "task_id": task_id, "receipt": receipt, "detail": detail.json()}, indent=2))
        if scenario != "completed" and scenario not in recovery_scenarios:
            assert receipt["completed"] == 0 and receipt["blocked"] >= 1
            assert len(calls) == 4  # real first contact; held sibling makes zero provider POSTs
            await engine.dispose()
            reopened = await jobs.inference_accounting_snapshot()
            assert reopened["accounting_continuity_verified"] is True
            children = [row for row in reopened["operations"] if row["operation_id"].startswith("remote:research:")]
            assert len(children) == 2
            contacted = [row for row in children if row["contact_started_at"] is not None]
            assert len(contacted) == 1 and contacted[0]["state"] == "settled"
            assert contacted[0]["actual_cost_microusd"] == (150 if scenario == "overrun_first_response" else 2)
            sibling = next(row for row in children if row["contact_started_at"] is None)
            if scenario == "overrun_first_response":
                assert sibling["state"] == "reserved" and sibling["bound_microusd"] == 100
                assert sibling["recovery_reason"] == "provider_contact_denied"
            async with factory.accounting_sessions() as db:
                rows = list((await db.scalars(select(WorkflowRunState).where(WorkflowRunState.job_kind.in_(
                    ["research_dossier", "readonly_research_child"])))).all())
                assert len(rows) == 3
                completed = [row for row in rows if row.status == "succeeded"]
                if scenario == "overrun_first_response":
                    # The first exact output completed with known actual debt.
                    # Overrun freezes unfinished work; it must not erase that
                    # already completed child's result or unclipped charge.
                    assert len(completed) == 1 and completed[0].job_kind == "readonly_research_child"
                    assert completed[0].run_identity.endswith(":child:0") and completed[0].finished_at is not None
                else:
                    assert not completed
                assert next(row for row in rows if row.job_kind == "research_dossier").status != "succeeded"
                board = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
                assert board.status.value == "blocked"
            (root/"research-negative-accounting-readback.json").write_text(json.dumps({"scenario": scenario,
                "provider_post_count": len(calls), "snapshot": reopened}, indent=2))
            return
        assert receipt["completed"] == 1, detail.json()
        report = await client.get("/api/work-board/tasks/"+task_id+"/research-report")
        assert report.status_code == 200, report.text
        assert report.headers["content-type"].startswith("text/plain") and report.headers["x-content-type-options"] == "nosniff"
        assert "Memory: no_learning" in report.text and "Perspective 2" in report.text
        assert len(calls) == 5  # three real canary admissions, two child HTTP POSTs
        await engine.dispose()
        reopened = await jobs.inference_accounting_snapshot()
        assert reopened["accounting_continuity_verified"] is True
        async with factory.accounting_sessions() as db:
            rows = list((await db.scalars(select(WorkflowRunState).where(WorkflowRunState.job_kind.in_(
                ["research_dossier", "readonly_research_child"])))).all())
            assert len(rows) == 3 and all(row.status == "succeeded" for row in rows)
            attempts = list((await db.scalars(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task_id))).all())
            assert len(attempts) == 1 and attempts[0].ended_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["revoke_first_response", "goal_change_first_response",
    "tamper_source_first_response", "overrun_first_response"])
async def test_current_authority_or_overrun_blocks_prefunded_sibling_and_adoption(accounting_db, real_auth, monkeypatch, scenario):
    await test_authenticated_parent_two_children_real_public_source_and_dossier(accounting_db, real_auth, monkeypatch, scenario=scenario)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["restart_before_sources", "restart_prompt_ready", "restart_funded_queued",
    "restart_written_outputs", "cancel_funded_queued"])
async def test_explicit_native_recovery_keeps_original_attempt_deadline_and_call_rows(accounting_db, real_auth, monkeypatch, scenario):
    await test_authenticated_parent_two_children_real_public_source_and_dossier(accounting_db, real_auth, monkeypatch, scenario=scenario)
