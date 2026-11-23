"""Actual authenticated Board/native/SQLite/source/artifact vertical.

OpenRouter HTTP and one named public-source DNS/HTTP boundary are intercepted.
Source reads retain the existing pinned transport and stream a finite labeled
fixture, never the live upstream license. This is backend acceptance, not a
managed UI or live-source semantic receipt.
"""
import json
import ipaddress
import socket
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
from tests.support.brokers import use_inference_broker

# ADR-035: pins the native research vertical whose setup saves the historical OpenRouter route
pytestmark = pytest.mark.openrouter_legacy


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
    # Isolate the named research fixture from a copied managed example.com
    # allowlist. Opportunity/browser fixtures explicitly override their host.
    monkeypatch.setattr(settings, "browser_site_allowlist", SOURCE_HOST)
    monkeypatch.setattr(settings, "openrouter_api_key", "intercepted-provider-boundary-only")
    # Each accounting_db fixture is a distinct deployment/Root. Its real
    # process-local broker must also be distinct, as on an actual fresh
    # launcher. The canonical financial ledger/witness is never reset or
    # forgiven; every broker still uses production durable accounting.
    from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    use_inference_broker(monkeypatch, broker)
    yield
    _reset_login_throttle_for_tests()


class ResponseBytes(httpx.AsyncByteStream):
    def __init__(self, content):
        self.content = content

    async def __aiter__(self):
        for offset in range(0, len(self.content), 512):
            yield self.content[offset:offset+512]


SOURCE_HOST = "raw.githubusercontent.com"
SOURCE_PIN = "93.184.216.34"
SOURCE_PATH = "/python/cpython/v3.12.8/LICENSE"
SOURCE_FIXTURE = b"\n".join([
    b"Finite named-source HTTP fixture, not the upstream license.",
    b"Native public transport and parser proof only; no live-source claim.",
    b"Fixture line three: selected public text.",
    b"Fixture line four: attributed bounded evidence.",
    b"Fixture line five: source prose is data, not authority.",
    b"Fixture line six: no external writes are requested.",
    b"Fixture line seven: retain independent native readback.",
    b"Fixture line eight: preserve explicit no_learning.",
    b"Fixture line nine: deterministic end of selected span.",
]) + b"\n"


class ProviderBoundary(httpx.AsyncBaseTransport):
    def __init__(self, calls, controls):
        self.calls = calls
        self.controls = controls

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
            usage = {"cost": "0.000150" if len(self.calls) == 4 and self.controls.get("overrun") else "0.000002",
                "prompt_tokens": 10, "completion_tokens": 10}
            if self.controls.get("unpriced") and len(body["messages"]) != 1:
                del usage["cost"]  # answered, but the provider has not priced it (#1295)
            payload = {"id": "intercepted-"+str(len(self.calls)),
                "choices": [{"message": {"role": "assistant", "content": content}}], "usage": usage}
            return httpx.Response(200, request=request, headers={"content-type": "application/json"},
                stream=ResponseBytes(json.dumps(payload).encode()))
        # Intercept the HTTP response after production DNS/IP policy and
        # pinning have constructed its numeric URL, Host and TLS SNI.
        assert request.method == "GET" and request.url.scheme == "https"
        assert request.url.host == SOURCE_PIN and request.url.port in {None, 443}
        assert request.url.path == SOURCE_PATH and not request.url.query
        assert request.headers["host"] == SOURCE_HOST
        assert request.extensions.get("sni_hostname") == SOURCE_HOST
        self.controls.setdefault("named_source_http", []).append(str(request.url))
        return httpx.Response(200, request=request, headers={"content-type": "text/plain;charset=utf-8"},
            stream=ResponseBytes(SOURCE_FIXTURE))

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_authenticated_parent_two_children_real_public_source_and_dossier(accounting_db, real_auth, monkeypatch, *, scenario="completed"):
    from src.api import auth, work_board, model_fabric_settings, goals
    from src.model_fabric.configuration import write_model_fabric_configuration
    from src.browser import pinned_transport
    def named_source_dns(host, port):
        assert host == SOURCE_HOST and port == 443
        return [SOURCE_PIN]
    monkeypatch.setattr(pinned_transport, "_blocking_default_resolver", named_source_dns)
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    def local_connect(sock, address):
        if isinstance(address, tuple) and not ipaddress.ip_address(address[0]).is_loopback:
            raise AssertionError("research fixture denies every nonlocal socket")
        return original_connect(sock, address)
    monkeypatch.setattr(socket.socket, "connect", local_connect)
    def local_connect_ex(sock, address):
        if isinstance(address, tuple) and not ipaddress.ip_address(address[0]).is_loopback:
            raise AssertionError("research fixture denies every nonlocal socket")
        return original_connect_ex(sock, address)
    monkeypatch.setattr(socket.socket, "connect_ex", local_connect_ex)
    root, engine, factory = accounting_db
    configured = setup_configuration()
    write_model_fabric_configuration(replace(model_fabric_settings._setup_configuration(replace(configured.openrouter_setup, timeout_seconds=30),
        profiles=(), policies=()), egress_revision=configured.egress_revision+1))
    jobs = DurableJobRepository()
    await jobs.ensure_inference_ledger()
    calls = []
    controls = {"overrun": scenario == "large_first_charge", "unpriced": scenario in {"restart_written_outputs_unpriced", "cancel_after_unpriced_child"}}
    written_outputs = {"restart_written_outputs", "restart_written_outputs_unpriced"}
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
        if scenario == "cancel_policy_denial":
            # The provider policy reads as changed exactly at the prefunded
            # sibling's contact recheck: it is durably denied and never
            # contacted (ADR-037 keeps consent/policy as the only contact denial).
            from src.model_fabric import effective_policy
            original_contact = DurableJobRepository.contact_inference_provider
            contacts = []
            async def contact_after_policy_change(self, *args, **kwargs):
                contacts.append(args)
                if len(contacts) < 2:
                    return await original_contact(self, *args, **kwargs)
                current = effective_policy.current_inference_policy
                with monkeypatch.context() as changed:
                    changed.setattr(effective_policy, "current_inference_policy",
                        lambda *a, **k: (current(*a, **k)[0], "f" * 64))
                    return await original_contact(self, *args, **kwargs)
            monkeypatch.setattr(DurableJobRepository, "contact_inference_provider", contact_after_policy_change)
        dispatcher = WorkBoardDispatcher(jobs=jobs, session_provider=factory.accounting_sessions)
        if scenario == "cancel_active_provider":
            import asyncio
            contacted = asyncio.Event()
            completed = asyncio.Event()
            async def hold_actual_provider_contact():
                contacted.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    completed.set()
            controls["after_first_contact"] = hold_actual_provider_contact
            monkeypatch.setattr(work_board, "dispatcher", dispatcher)
            # The cancel endpoint answers the proven result only after the
            # cancelled worker actually returns; past its 5s wall-clock bound
            # it answers cancellation_pending instead. A loaded runner can
            # exceed that bound while the worker quiesces (#1196), so wait on
            # the worker's completion itself, not on the clock.
            from src.work_board import dispatcher as dispatcher_module
            monkeypatch.setattr(dispatcher_module, "_RESEARCH_CANCEL_WORKER_WAIT_SECONDS", None)
            running = asyncio.create_task(dispatcher.run_pass())
            try:
                await asyncio.wait_for(contacted.wait(), timeout=40)
                state = await client.get("/api/work-board/tasks/"+task_id+"/research")
                assert state.status_code == 200, state.text
                original = state.json()
                assert any(child["lease_present"] for child in original["children"])
                result = await client.post("/api/work-board/tasks/"+task_id+"/research/cancel",
                    json={"expected_revision": original["task_revision"], "idempotency_key": "actual-contact-cancel"})
                assert result.status_code == 200, result.text
                assert completed.is_set() and running.done()
                assert result.json()["cancellation"]["cancelled"] is False
                assert result.json()["cancellation"]["unknown_liability_preserved"] is True
                assert len(calls) == 4  # three canaries and exactly one real research POST
                await engine.dispose()
                current = await jobs.inference_accounting_snapshot()
                costs = [row for row in current["operations"] if row["operation_id"].startswith("remote:research:")]
                assert current["accounting_continuity_verified"] is True and len(costs) == 2
                contact = next(row for row in costs if row["contact_started_at"] is not None)
                assert contact["state"] == "unknown" and contact["actual_cost_microusd"] is None
                recovery = await client.post("/api/work-board/tasks/"+task_id+"/research/recover",
                    json={"expected_revision": result.json()["research"]["task_revision"], "idempotency_key": "no-contact-replay"})
                assert recovery.status_code == 409 and len(calls) == 4
                (root/"research-contact-cancel-readback.json").write_text(json.dumps({"original": original,
                    "cancellation": result.json(), "accounting": current, "provider_post_count": len(calls),
                    "actual_callback_completed": completed.is_set(), "recovery_status": recovery.status_code}, indent=2))
            finally:
                if not running.done():
                    running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            return
        snapshot_scenarios = {"snapshot_missing_group", "snapshot_stale_goal", "snapshot_expired"}
        recovery_scenarios = {"restart_before_sources", "restart_prompt_ready", "restart_funded_queued", *written_outputs, "cancel_funded_queued", "cancel_after_unpriced_child", *snapshot_scenarios}
        if scenario in recovery_scenarios:
            from src.workflows import research_coordinator as coordinator
            from src.workflows.research_accounting import fund_fixed_group
            from src.workflows.research_native import checkpoint
            from src.workflows.research_waits import resume_parent, pause_parent
            from src.workflows import research_provider as provider
            original_continue = coordinator.continue_parent
            async def interrupted_process(jobs, *, parent_id, owner, phase_binding):
                creation = checkpoint(await jobs.get_job(parent_id), "research:creation")
                if scenario != "restart_before_sources" and scenario not in snapshot_scenarios:
                    for child_id in creation["child_ids"]:
                        await coordinator._prepare_child(jobs, child_id, owner, {}, phase_binding)
                if scenario in {"restart_funded_queued", *written_outputs, "cancel_funded_queued", "cancel_after_unpriced_child"}:
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
                    if scenario == "cancel_after_unpriced_child":
                        # The first child is answered and succeeds; NEAR has
                        # not priced it yet. The second stays funded, queued.
                        first_child = creation["child_ids"][0]
                        claimed = await jobs.claim_job(first_child, owner=owner, lease_seconds=120,
                            continue_existing_attempt=True, claim_authority_check=coordinator._claim_guard(jobs, phase_binding))
                        await provider.execute_funded_child(jobs, child_id=first_child, owner=owner,
                            fence=claimed["lease"]["fencing_token"])
                        assert (await jobs.get_job(first_child))["status"] == "succeeded"
                    if scenario in written_outputs:
                        original_adopt = provider._adopt_child
                        async def interrupted_adoption(*args, **kwargs):
                            raise RuntimeError("declared crash boundary after actual reserved child output")
                        monkeypatch.setattr(provider, "_adopt_child", interrupted_adoption)
                        try:
                            for child_id in creation["child_ids"]:
                                # The holder keeps a live lease for the whole
                                # child execution: every fenced write inside
                                # it (contact, receipt, reserved output)
                                # requires lease_expires_at > now. A 1 s lease
                                # raced those writes on a loaded runner (#1230).
                                claimed = await jobs.claim_job(child_id, owner=owner, lease_seconds=120,
                                    continue_existing_attempt=True, claim_authority_check=coordinator._claim_guard(jobs, phase_binding))
                                with pytest.raises(RuntimeError, match="declared crash boundary"):
                                    await provider.execute_funded_child(jobs, child_id=child_id, owner=owner,
                                        fence=claimed["lease"]["fencing_token"])
                            # The crashed process never releases its child
                            # leases; they lapse. Lapse them in the past instead
                            # of waiting out a wall-clock lease (#1230).
                            async with factory.accounting_sessions() as db:
                                lapsed = datetime.now(timezone.utc).replace(tzinfo=None)-__import__("datetime").timedelta(seconds=1)
                                for row in (await db.scalars(select(WorkflowRunState).where(
                                        WorkflowRunState.run_identity.in_(creation["child_ids"])))).all():
                                    assert row.status == "running" and row.lease_owner == owner and row.lease_expires_at > lapsed
                                    row.lease_expires_at = lapsed
                                    db.add(row)
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
            use_inference_broker(monkeypatch, fresh_broker)
            monkeypatch.setattr(work_board, "dispatcher", WorkBoardDispatcher(jobs=jobs, session_provider=factory.accounting_sessions))
            if scenario in snapshot_scenarios:
                if scenario == "snapshot_stale_goal":
                    changed = await client.patch("/api/goals/actual-research-goal", json={"title": "Current Goal changed", "expected_revision": 1})
                    assert changed.status_code == 200, changed.text
                else:
                    async with factory.accounting_sessions() as db:
                        row = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind == (
                            "research_dossier" if scenario == "snapshot_expired" else "readonly_research_child")))
                        if scenario == "snapshot_expired":
                            row.deadline_at = datetime.now(timezone.utc).replace(tzinfo=None)-__import__("datetime").timedelta(seconds=1)
                        else:
                            row.parent_job_id = None  # actual incomplete queried group; immutable creation still names both
                        db.add(row)
                async with factory.accounting_sessions() as db:
                    before = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
                    before_revision = before.task_revision
                state = await client.get("/api/work-board/tasks/"+task_id+"/research")
                assert state.status_code == 200 and state.json()["recoverable"] is False, state.text
                async with factory.accounting_sessions() as db:
                    after = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
                    assert after.task_revision == before_revision  # GET does not mutate admission
                rejected = await client.post("/api/work-board/tasks/"+task_id+"/research/recover",
                    json={"expected_revision": before_revision, "idempotency_key": "ineligible-original-recovery"})
                assert rejected.status_code == 409 and len(calls) == 3
                (root/"research-snapshot-ineligible-readback.json").write_text(json.dumps({"scenario": scenario,
                    "state": state.json(), "recovery_status": rejected.status_code, "provider_post_count": len(calls)}, indent=2))
                return
            state = await client.get("/api/work-board/tasks/"+task_id+"/research")
            assert state.status_code == 200, state.text
            original_state = state.json()
            if scenario in written_outputs:
                assert original_state["recoverable"] is True  # exact reserved physical output is visible to the Inspector
            if scenario == "restart_written_outputs_unpriced":
                # #1295: both answered requests finished unpriced. They are not
                # outstanding, so they do not block recovery (ADR-037).
                unpriced = [cost for cost in original_state["costs"] if cost["contact_started"]]
                assert len(unpriced) == 2 and all(cost["state"] == "unknown" for cost in unpriced)
            action = "cancel" if scenario in {"cancel_funded_queued", "cancel_after_unpriced_child"} else "recover"
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
            if scenario == "cancel_after_unpriced_child":
                # #1295: the answered child's unpriced request is finished, so
                # it does not turn cancellation into an unresolved contact.
                cancellation = recovered.json()["cancellation"]
                assert cancellation["cancelled"] is True and cancellation["unknown_liability_preserved"] is False
                by_job = {child["job_id"]: child["status"] for child in current_state["children"]}
                assert sorted(by_job.values()) == ["cancelled", "succeeded"]
                answered = next(cost for cost in current_state["costs"] if cost["contact_started"])
                assert by_job[answered["job_id"]] == "succeeded" and answered["state"] == "unknown"
                untouched = next(cost for cost in current_state["costs"] if not cost["contact_started"])
                assert untouched["state"] == "released" and by_job[untouched["job_id"]] == "cancelled"
                assert current_state["status"] == "cancelled" and len(calls) == 4
                return
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
            if scenario == "restart_written_outputs_unpriced":
                # At most once: recovery adopted the written outputs and sent
                # neither contacted request again; both records stay unpriced.
                final = await client.get("/api/work-board/tasks/"+task_id+"/research")
                assert final.status_code == 200, final.text
                assert sorted((cost["operation_id"], cost["state"]) for cost in final.json()["costs"] if cost["contact_started"]) == sorted(
                    (cost["operation_id"], "unknown") for cost in unpriced)
            receipt = {"completed": 1}
        detail = await client.get("/api/work-board/tasks/"+task_id)
        (root/"research-vertical-private-readback.json").write_text(json.dumps({"label": "real public source; provider HTTP interception only",
            "scenario": scenario, "task_id": task_id, "receipt": receipt, "detail": detail.json()}, indent=2))
        if scenario not in {"completed", "large_first_charge"} and scenario not in recovery_scenarios:
            assert receipt["completed"] == 0 and receipt["blocked"] >= 1
            assert len(calls) == 4  # real first contact; held sibling makes zero provider POSTs
            await engine.dispose()
            reopened = await jobs.inference_accounting_snapshot()
            assert reopened["accounting_continuity_verified"] is True
            children = [row for row in reopened["operations"] if row["operation_id"].startswith("remote:research:")]
            assert len(children) == 2
            contacted = [row for row in children if row["contact_started_at"] is not None]
            assert len(contacted) == 1 and contacted[0]["state"] == "settled"
            assert contacted[0]["actual_cost_microusd"] == 2
            async with factory.accounting_sessions() as db:
                rows = list((await db.scalars(select(WorkflowRunState).where(WorkflowRunState.job_kind.in_(
                    ["research_dossier", "readonly_research_child"])))).all())
                assert len(rows) == 3
                completed = [row for row in rows if row.status == "succeeded"]
                if scenario == "cancel_policy_denial":
                    # The first child answered under an unchanged policy; the
                    # denial of its sibling does not erase that result.
                    assert len(completed) == 1 and completed[0].job_kind == "readonly_research_child"
                    assert completed[0].run_identity == contacted[0]["job_id"]
                else:
                    assert not completed
                assert next(row for row in rows if row.job_kind == "research_dossier").status != "succeeded"
                board = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
                assert board.status.value == "blocked"
            (root/"research-negative-accounting-readback.json").write_text(json.dumps({"scenario": scenario,
                "provider_post_count": len(calls), "snapshot": reopened}, indent=2))
            if scenario == "cancel_policy_denial":
                sibling = next(row for row in children if row["contact_started_at"] is None)
                assert sibling["state"] == "reserved" and sibling["recovery_reason"] == "provider_contact_denied"
                monkeypatch.setattr(work_board, "dispatcher", dispatcher)
                original = (await client.get("/api/work-board/tasks/"+task_id+"/research")).json()
                result = await client.post("/api/work-board/tasks/"+task_id+"/research/cancel",
                    json={"expected_revision": original["task_revision"], "idempotency_key": "policy-denial-cancel"})
                assert result.status_code == 200, result.text
                assert result.json()["cancellation"]["cancelled"] is True
                await engine.dispose()
                settled = await jobs.inference_accounting_snapshot()
                current_costs = [row for row in settled["operations"] if row["operation_id"].startswith("remote:research:")]
                assert settled["accounting_continuity_verified"] is True
                assert next(row for row in current_costs if row["contact_started_at"] is not None)["actual_cost_microusd"] == 2
                released = next(row for row in current_costs if row["contact_started_at"] is None)
                assert released["state"] == "released" and released["recovery_reason"] == "cancelled_before_contact"
                assert len(calls) == 4
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
        if scenario == "large_first_charge":
            # #1284: a large first charge is recorded; it never holds the
            # prefunded sibling or withholds adoption.
            children = [row for row in reopened["operations"] if row["operation_id"].startswith("remote:research:")]
            assert sorted(row["actual_cost_microusd"] for row in children) == [2, 150]
            assert all(row["state"] == "settled" and row["recovery_reason"] is None for row in children)
        async with factory.accounting_sessions() as db:
            rows = list((await db.scalars(select(WorkflowRunState).where(WorkflowRunState.job_kind.in_(
                ["research_dossier", "readonly_research_child"])))).all())
            assert len(rows) == 3 and all(row.status == "succeeded" for row in rows)
            attempts = list((await db.scalars(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task_id))).all())
            assert len(attempts) == 1 and attempts[0].ended_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["revoke_first_response", "goal_change_first_response",
    "tamper_source_first_response"])
async def test_current_authority_blocks_prefunded_sibling_and_adoption(accounting_db, real_auth, monkeypatch, scenario):
    await test_authenticated_parent_two_children_real_public_source_and_dossier(accounting_db, real_auth, monkeypatch, scenario=scenario)


@pytest.mark.asyncio
async def test_large_first_charge_never_holds_prefunded_sibling_or_adoption(accounting_db, real_auth, monkeypatch):
    await test_authenticated_parent_two_children_real_public_source_and_dossier(accounting_db, real_auth, monkeypatch, scenario="large_first_charge")


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["restart_before_sources", "restart_prompt_ready", "restart_funded_queued",
    "restart_written_outputs", "restart_written_outputs_unpriced", "cancel_funded_queued", "cancel_after_unpriced_child"])
async def test_explicit_native_recovery_keeps_original_attempt_deadline_and_call_rows(accounting_db, real_auth, monkeypatch, scenario):
    await test_authenticated_parent_two_children_real_public_source_and_dossier(accounting_db, real_auth, monkeypatch, scenario=scenario)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["cancel_active_provider", "cancel_policy_denial"])
async def test_actual_research_cancel_preserves_contacted_debt_and_releases_policy_denial(accounting_db, real_auth, monkeypatch, scenario):
    await test_authenticated_parent_two_children_real_public_source_and_dossier(accounting_db, real_auth, monkeypatch, scenario=scenario)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["snapshot_missing_group", "snapshot_stale_goal", "snapshot_expired"])
async def test_research_snapshot_requires_exact_group_and_current_authority(accounting_db, real_auth, monkeypatch, scenario):
    await test_authenticated_parent_two_children_real_public_source_and_dossier(accounting_db, real_auth, monkeypatch, scenario=scenario)
