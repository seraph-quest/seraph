"""Actual authenticated discovery -> digest -> native C1 local output.

Only final inference HTTP and public HTTP routing are fixture boundaries. Native
owners, actual SQLite, private artifacts, Task acceptance and write_file stay real.
"""
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from tests.test_goal_discovery_vertical import public_http_fixture
from tests.test_inference_accounting import accounting_db, setup_configuration
from tests.test_research_native_vertical import real_auth, ResponseBytes


@pytest.mark.asyncio
@pytest.mark.parametrize("expire_during_publication", [False, True], ids=["complete", "publication-freshness-race"])
async def test_authenticated_digest_finding_native_task_physical_output(accounting_db, real_auth, public_http_fixture, monkeypatch, expire_during_publication):
    from config.settings import settings
    from src.api import auth, goals, guardian_inbox, model_fabric_settings, work_board
    from src.auth.middleware import OperatorAuthMiddleware
    from src.guardian import programme_digest as digest
    from src.guardian.discovery_search import DiscoverySearch
    from src.guardian.goal_discovery import GoalDiscoveryService
    from src.guardian.goal_programmes import goal_programme_service
    from src.model_fabric.configuration import write_model_fabric_configuration
    from src.native_tools.registry import ToolRegistry
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.work_board.general_task import GeneralTaskService
    from src.workflows.job_runtime import DurableJobRepository
    from src.workflows.research_sources import physical_discovery_inputs

    workspace, engine, factory = accounting_db
    configured = setup_configuration()
    write_model_fabric_configuration(replace(model_fabric_settings._setup_configuration(
        replace(configured.openrouter_setup, timeout_seconds=30), profiles=(), policies=()),
        egress_revision=configured.egress_revision + 1))
    monkeypatch.setattr(settings, "browser_site_allowlist", "example.com")
    monkeypatch.setattr(settings, "user_timezone", "UTC")
    jobs = DurableJobRepository()
    await jobs.configure_inference_accounting(1000)
    calls, public_routes = [], []
    original_client = httpx.AsyncClient

    class InferenceBoundary(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert request.url.host == "openrouter.ai" and request.method == "POST"
            body = json.loads(request.content)
            if len(body["messages"]) == 1:
                answer = "CANARY_OK"  # Existing route-health mechanical prerequisite.
            else:
                calls.append(body)
                assert len(request.content) <= 8192
                supplied = json.loads(body["messages"][1]["content"])["untrusted_public_data"]
                if supplied.get("task") == "plan_queries":
                    answer = json.dumps({"queries": ["public product release evidence"]})
                elif "manifest_ref" in supplied:
                    answer = json.dumps({"selected_result_ids": [supplied["results"][0]["result_id"]]})
                else:
                    source = supplied["untrusted_quoted_sources"][0]
                    answer = json.dumps({"schema_version": 1, "perspective": "Prepare a public Goal discovery brief",
                        "claims": [{"text": "The public fixture describes the selected release.", "citations": [
                            {key: source[key] for key in ("source_id", "first_line", "last_line", "span_sha256")}]}],
                        "uncertainty": ["Fixture mechanics do not establish semantic truth."], "contradictions": [], "no_learning": True})
            payload = {"id": "owned-programme-digest", "choices": [{"message": {"role": "assistant", "content": answer}}],
                "usage": {"cost": "0.000002", "prompt_tokens": 10, "completion_tokens": 10}}
            return httpx.Response(200, request=request, headers={"content-type": "application/json"}, stream=ResponseBytes(json.dumps(payload).encode()))

    def clients(**kwargs):
        if "transport" not in kwargs:
            kwargs["transport"] = InferenceBoundary()
        return original_client(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", clients)
    port, physical_contacts, controls = public_http_fixture

    class PublicBoundary(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert request.url.scheme == "https" and request.url.host == request.headers["host"]
            assert request.extensions["sni_hostname"] == request.headers["host"]
            assert request.headers["host"] in {"html.duckduckgo.com", "example.com"}
            public_routes.append((request.method, str(request.url)))
            async with original_client(transport=httpx.AsyncHTTPTransport(), trust_env=False, follow_redirects=False, timeout=5) as client:
                response = await client.request(request.method, f"http://127.0.0.1:{port}{request.url.raw_path.decode()}", headers=request.headers, content=request.content)
            return httpx.Response(response.status_code, request=request, headers=response.headers, stream=ResponseBytes(response.content))

    def resolver(host, port):
        assert host in {"html.duckduckgo.com", "example.com"} and port == 443
        return ["93.184.216.34"]

    transport = PublicBoundary()
    discovery = GoalDiscoveryService(jobs=jobs, search=DiscoverySearch(resolver=resolver, transport=transport), resolver=resolver, transport=transport)
    registry = ToolRegistry(); registry.start()
    tasks = GeneralTaskService(registry); tasks.start()
    dispatcher = WorkBoardDispatcher(session_provider=factory.accounting_sessions, general_tasks=tasks)
    monkeypatch.setattr("src.work_board.dispatcher._dispatcher", dispatcher)
    monkeypatch.setattr(work_board, "dispatcher", dispatcher)
    monkeypatch.setattr("src.guardian.goal_discovery.goal_discovery_service", discovery)
    dispatcher.goal_discovery = discovery
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router, prefix="/api/auth")
    for router in (goals.router, guardian_inbox.router, model_fabric_settings.router, work_board.router):
        app.include_router(router, prefix="/api")
    await goal_programme_service.start(); await discovery.start()
    try:
        async with original_client(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"origin": "http://localhost:3001"}) as client:
            login = await client.post("/api/auth/login", json={"password": "research-vertical-private-secret"})
            assert login.status_code == 200, login.text
            assert (await client.post("/api/auth/ownership/enroll")).status_code == 200
            for capability in ("text", "latency_ms", "health"):
                proof = await client.post("/api/settings/model-fabric/canary", json={"profile_id": "openrouter", "capability": capability, "timeout_seconds": 30})
                assert proof.status_code == 200 and proof.json()["outcome"] == "passed", proof.text
            created = await client.post("/api/goals", json={"title": "PRIVATE_GOAL_TITLE_CANARY", "description": "PRIVATE_GOAL_DESCRIPTION_CANARY"})
            assert created.status_code == 200, created.text
            goal_id = created.json()["id"]
            request = {"expected_goal_revision": 1, "expected_grant_revision": 0, "public_brief": "Track public release evidence", "budget": {"max_inference_microusd": 500}}
            base = f"/api/goals/{goal_id}/programmes"
            preview = await client.post(base + "/preview", json=request)
            assert preview.status_code == 200, preview.text
            accepted = await client.post(base + "/accept", json={**request, "review_digest": preview.json()["review_digest"], "public_web_acknowledged": True, "local_artifacts_acknowledged": True, "inference_ceiling_acknowledged": True})
            assert accepted.status_code == 200, accepted.text
            programme = accepted.json()
            job = await discovery.admit(goal_id=goal_id, programme_id=programme["id"], grant_revision=1)
            done = await discovery.run(job["job_id"])
            assert done["status"] == "succeeded", done
            witness = await physical_discovery_inputs(jobs, job["job_id"])
            assert {a["kind"] for a in witness.artifacts.values()} >= {"queries", "manifest", "selection", "snapshot", "snapshots", "brief", "draft"}
            assert len(calls) == 3 and len(public_routes) == len(physical_contacts) == 2
            assert "PRIVATE_GOAL" not in json.dumps(calls)
            # Current local day after08, no fake workflow or artifact projection.
            now = datetime.now(timezone.utc)
            local = digest.local_clock(now)
            digest_now = max(now, local.replace(hour=8, minute=0, second=0, microsecond=0).astimezone(timezone.utc))
            await digest.tick(digest_now)
            endpoint = "/api/guardian/inbox/programme-digests"
            projected = await client.get(endpoint)
            assert projected.status_code == 200, projected.text
            snapshot = projected.json()
            assert snapshot["notifications"]["enabled"] is False
            assert snapshot["programmes"][0]["sources_checked"] == 1
            assert snapshot["programmes"][0]["next_source_state"] == "unavailable"
            assert snapshot["programmes"][0]["next_source_eligible_at"] is None
            assert snapshot["programmes"][0]["next_source_reason"] is not None
            entry = snapshot["digests"][0]
            finding = entry["findings"][0]
            assert finding["job_id"] == job["job_id"] and finding["actionable"] is True
            assert finding["id"] in entry["digest"]["finding_ids"]
            original_brief = await client.get(base + f"/{programme['id']}/discovery/{job['job_id']}/brief")
            assert original_brief.status_code == 200 and original_brief.json()["physical_readback"] is True
            action_path = f"/api/guardian/inbox/programme-findings/{finding['id']}/actions"
            body = {"action": "accept_followup", "desired_outcome": "Prepare a cited local checklist", "idempotency_key": "actual-digest-followthrough"}
            if expire_during_publication:
                from types import SimpleNamespace
                import src.work_board.input_artifacts as actual_inputs
                original_prepare = actual_inputs.prepare_input_artifact
                original_monotonic = digest.time.monotonic
                elapsed = [0]
                monkeypatch.setattr(digest, "time", SimpleNamespace(monotonic=lambda: original_monotonic() + elapsed[0]))
                async def expire_after_actual_staging(*args, **kwargs):
                    artifact = await original_prepare(*args, **kwargs)
                    elapsed[0] = 48 * 3600 + 1
                    return artifact
                monkeypatch.setattr(actual_inputs, "prepare_input_artifact", expire_after_actual_staging)
            prepared = await client.post(action_path, json=body)
            if expire_during_publication:
                assert prepared.status_code == 409 and prepared.json()["detail"]["code"] == "programme_finding_refresh_required", prepared.text
                from src.db.models import WorkBoardTask, WorkBoardProposal, WorkBoardEvent, WorkBoardInputArtifact, ProgrammeFollowThrough
                async with factory.accounting_sessions() as db:
                    assert list((await db.execute(select(WorkBoardTask))).scalars()) == []
                    assert list((await db.execute(select(WorkBoardProposal))).scalars()) == []
                    assert list((await db.execute(select(WorkBoardEvent))).scalars()) == []
                    assert list((await db.execute(select(ProgrammeFollowThrough))).scalars()) == []
                    retained_inputs = list((await db.execute(select(WorkBoardInputArtifact).where(WorkBoardInputArtifact.capability_id == "agent.task.v1"))).scalars())
                    assert len(retained_inputs) == 1
                    retained_input = retained_inputs[0]
                    assert retained_input.bound_task_id is None and retained_input.state == "pending"
                    actual_payload = actual_inputs._safe_file_bytes(actual_inputs._payload_path(retained_input),
                        expected_digest=retained_input.payload_sha256, expected_size=retained_input.size_bytes)
                    assert 0 < len(actual_payload) == retained_input.size_bytes <= 65536
                    assert hashlib.sha256(actual_payload).hexdigest() == retained_input.payload_sha256
                    assert timedelta(0) < retained_input.expires_at.replace(tzinfo=timezone.utc) - retained_input.created_at.replace(tzinfo=timezone.utc) <= timedelta(hours=24)
                reread = await client.get(endpoint)
                assert reread.status_code == 200, reread.text
                retained_finding = next(f for d in reread.json()["digests"] for f in d["findings"] if f["id"] == finding["id"])
                assert retained_finding["task_id"] is None and retained_finding["follow_through"] is None
                assert not (workspace / "programme-followthrough" / f"{finding['id']}.md").exists()
                assert len(calls) == 3 and len(physical_contacts) == 2
                print(json.dumps({"flow": "actual_source_freshness_expired_during_c1_publication", "finding_id": finding["id"],
                    "published_tasks": 0, "published_proposals": 0, "bound_inputs": 0, "retained_unbound_private_inputs": 1,
                    "retained_input_sha256": retained_input.payload_sha256, "physical_public_http_contacts": 2,
                    "scripted_final_inference_requests": 3, "real_provider_contacts": 0, "real_spend": 0}, sort_keys=True))
                return
            assert prepared.status_code == 200, prepared.text
            task_id = prepared.json()["task_id"]
            detail = await client.get(f"/api/work-board/tasks/{task_id}")
            assert detail.status_code == 200, detail.text
            card = detail.json()["task"]
            assert card["status"] == "triage"
            from src.db.models import WorkBoardTask
            from src.work_board.contracts import GeneralTaskEnvelope
            from src.work_board.dispatcher import _parse_typed_input
            async with factory.accounting_sessions() as db:
                staged_task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
                original_envelope = GeneralTaskEnvelope.model_validate(_parse_typed_input(staged_task))
            original_group_id = original_envelope.proposal_group.group_id
            assert original_group_id and original_envelope.plan.revision == 1
            output_path = workspace / "programme-followthrough" / f"{finding['id']}.md"
            assert not output_path.exists()
            replay = await client.post(action_path, json=body)
            assert replay.status_code == 200 and replay.json()["task_id"] == task_id
            promoted = await client.post(f"/api/work-board/tasks/{task_id}/actions", json={"action": "promote", "expected_revision": card["task_revision"]})
            assert promoted.status_code == 200, promoted.text
            run = await dispatcher.run_pass()
            current = await client.get(f"/api/work-board/tasks/{task_id}")
            assert current.status_code == 200, current.text
            card = current.json()["task"]
            assert card["status"] == "review", (run, current.json())
            attempt = current.json()["attempts"][0]
            assert attempt["readback_status"] == "verified"
            parent = await dispatcher.jobs.get_job(attempt["workflow_run_id"])
            assert parent["status"] == "succeeded" and parent["goal_id"] == goal_id
            async with factory.accounting_sessions() as db:
                native_task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
                completed_envelope = GeneralTaskEnvelope.model_validate(_parse_typed_input(native_task))
            assert completed_envelope.proposal_group.group_id == original_group_id
            completed = await client.post(f"/api/work-board/tasks/{task_id}/actions", json={"action": "complete_review", "expected_revision": card["task_revision"], "attempt_id": attempt["attempt_id"]})
            assert completed.status_code == 200, completed.text
            assert completed.json()["task"]["status"] == "done"
            assert output_path.exists()
            actual = output_path.read_bytes(); physical_sha = hashlib.sha256(actual).hexdigest()
            assert b"Prepare a cited local checklist" in actual and finding["id"].encode() in actual
            assert output_path.stat().st_mode & 0o077 == 0
            reload = await client.get(endpoint)
            assert reload.status_code == 200, reload.text
            retained = next(f for d in reload.json()["digests"] for f in d["findings"] if f["id"] == finding["id"])
            assert retained["task_id"] == task_id and retained["follow_through"]["status"] == "completed"
            output = next(o for o in retained["prepared_outputs"] if o["artifact_id"] == "board-output:" + task_id)
            assert output["digest"] == physical_sha
            assert len(calls) == 3 and len(physical_contacts) == 2
            accounting = await jobs.inference_accounting_snapshot(job_id=job["job_id"])
            assert len(accounting["operations"]) == 3 and all(row["state"] == "settled" for row in accounting["operations"])
            print(json.dumps({"flow": "authenticated_programme_discovery_digest_c1_write_file_completed_readback", "goal_id": goal_id, "programme_id": programme["id"], "discovery_job_id": job["job_id"], "finding_id": finding["id"], "task_id": task_id, "proposal_group_id": original_group_id, "native_parent_job_id": attempt["workflow_run_id"], "attempt_id": attempt["attempt_id"], "physical_output_sha256": physical_sha, "physical_public_http_contacts": len(physical_contacts), "scripted_final_inference_requests": len(calls), "real_provider_contacts": 0, "real_spend": 0, "no_learning": True}, sort_keys=True))
    finally:
        await discovery.stop(); await goal_programme_service.stop(); tasks.stop(); registry.stop()


@pytest.fixture
def cited_deadline_http_fixture():
    """Fresh literal source bytes, served over real owned TCP, never patched artifacts."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    deadline = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec="seconds")
    source = f"Public grants deadline: {deadline}\nSource instructions remain untrusted data.".encode()
    observed = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            assert self.path == "/html/" and self.headers["Host"] == "html.duckduckgo.com"
            body = self.rfile.read(int(self.headers["Content-Length"]))
            observed.append(("POST", hashlib.sha256(body).hexdigest()))
            self.reply(b'<html><a class="result__a" href="https://example.com/grants">Public grants source</a></html>', "text/html")
        def do_GET(self):
            assert self.path == "/grants" and self.headers["Host"] == "example.com"
            assert "Authorization" not in self.headers and "Cookie" not in self.headers
            observed.append(("GET", hashlib.sha256(source).hexdigest()))
            self.reply(source, "text/plain")
        def reply(self, body, content_type):
            self.send_response(200); self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield server.server_port, observed, source
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.asyncio
async def test_actual_cited_deadline_owner_day_two_notice_cap_and_daemon_claim(accounting_db, real_auth, cited_deadline_http_fixture, monkeypatch):
    from config.settings import settings
    from src.api import auth, goals, guardian_inbox, model_fabric_settings, observer
    from src.auth.middleware import OperatorAuthMiddleware
    from src.db.models import NativeNotificationOutbox, OperatorSession
    from src.guardian import programme_digest as digest
    from src.guardian.discovery_search import DiscoverySearch
    from src.guardian.goal_discovery import GoalDiscoveryService
    from src.guardian.goal_programmes import goal_programme_service
    from src.model_fabric.configuration import write_model_fabric_configuration
    from src.workflows.job_runtime import DurableJobRepository
    from src.workflows.research_sources import physical_discovery_inputs

    workspace, engine, factory = accounting_db
    configured = setup_configuration()
    write_model_fabric_configuration(replace(model_fabric_settings._setup_configuration(
        replace(configured.openrouter_setup, timeout_seconds=30), profiles=(), policies=()),
        egress_revision=configured.egress_revision + 1))
    monkeypatch.setattr(settings, "browser_site_allowlist", "example.com")
    monkeypatch.setattr(settings, "user_timezone", "UTC")
    # This original fixture Root must remain valid at the same day's08local
    # delivery clock. Configure its lifetime before login; never renew old rows.
    monkeypatch.setattr(settings, "operator_auth_idle_seconds", 86400)
    monkeypatch.setattr(settings, "operator_auth_absolute_seconds", 86400)
    jobs = DurableJobRepository(); await jobs.configure_inference_accounting(1000)
    original_client = httpx.AsyncClient
    calls = []
    class InferenceBoundary(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert request.url.host == "openrouter.ai" and request.method == "POST"
            body = json.loads(request.content)
            if len(body["messages"]) == 1:
                answer = "CANARY_OK"
            else:
                calls.append(body)
                supplied = json.loads(body["messages"][1]["content"])["untrusted_public_data"]
                if supplied.get("task") == "plan_queries":
                    answer = json.dumps({"queries": ["public grants deadline evidence"]})
                elif "manifest_ref" in supplied:
                    answer = json.dumps({"selected_result_ids": [supplied["results"][0]["result_id"]]})
                else:
                    source = supplied["untrusted_quoted_sources"][0]
                    answer = json.dumps({"schema_version": 1, "perspective": "Prepare public evidence",
                        "claims": [{"text": "The cited physical public source describes grants.", "citations": [
                            {key: source[key] for key in ("source_id", "first_line", "last_line", "span_sha256")}]}],
                        "uncertainty": ["Literal fixture mechanics only."], "contradictions": [], "no_learning": True})
            payload = {"id": "owned-deadline", "choices": [{"message": {"role": "assistant", "content": answer}}],
                "usage": {"cost": "0.000002", "prompt_tokens": 10, "completion_tokens": 10}}
            return httpx.Response(200, request=request, headers={"content-type": "application/json"}, stream=ResponseBytes(json.dumps(payload).encode()))
    def clients(**kwargs):
        if "transport" not in kwargs:
            kwargs["transport"] = InferenceBoundary()
        return original_client(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", clients)
    port, physical_contacts, literal_source = cited_deadline_http_fixture
    class PublicBoundary(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert request.url.scheme == "https" and request.url.host == request.headers["host"]
            assert request.extensions["sni_hostname"] == request.headers["host"]
            assert request.headers["host"] in {"html.duckduckgo.com", "example.com"}
            async with original_client(transport=httpx.AsyncHTTPTransport(), trust_env=False, follow_redirects=False, timeout=5) as local:
                response = await local.request(request.method, f"http://127.0.0.1:{port}{request.url.raw_path.decode()}", headers=request.headers, content=request.content)
            return httpx.Response(response.status_code, request=request, headers=response.headers, stream=ResponseBytes(response.content))
    def resolver(host, port):
        assert host in {"html.duckduckgo.com", "example.com"} and port == 443
        return ["93.184.216.34"]
    transport = PublicBoundary()
    discovery = GoalDiscoveryService(jobs=jobs, search=DiscoverySearch(resolver=resolver, transport=transport), resolver=resolver, transport=transport)
    monkeypatch.setattr("src.guardian.goal_discovery.goal_discovery_service", discovery)
    app = FastAPI(); app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router, prefix="/api/auth")
    for router in (goals.router, guardian_inbox.router, model_fabric_settings.router, observer.router):
        app.include_router(router, prefix="/api")
    await goal_programme_service.start(); await discovery.start()
    try:
        async with original_client(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"origin": "http://localhost:3001"}) as client:
            login = await client.post("/api/auth/login", json={"password": "research-vertical-private-secret"})
            assert login.status_code == 200, login.text
            assert (await client.post("/api/auth/ownership/enroll")).status_code == 200
            for capability in ("text", "latency_ms", "health"):
                proof = await client.post("/api/settings/model-fabric/canary", json={"profile_id": "openrouter", "capability": capability, "timeout_seconds": 30})
                assert proof.status_code == 200 and proof.json()["outcome"] == "passed", proof.text
            bindings = []
            for index in range(2):
                created = await client.post("/api/goals", json={"title": f"Private grants goal {index}"})
                assert created.status_code == 200, created.text
                goal_id = created.json()["id"]
                request = {"expected_goal_revision": 1, "expected_grant_revision": 0, "public_brief": "Track public grants deadlines",
                    "budget": {"max_inference_microusd": 500}, "notification_limits": {"per_day": 2}}
                base = f"/api/goals/{goal_id}/programmes"
                preview = await client.post(base + "/preview", json=request)
                assert preview.status_code == 200, preview.text
                accepted = await client.post(base + "/accept", json={**request, "review_digest": preview.json()["review_digest"], "public_web_acknowledged": True, "local_artifacts_acknowledged": True, "inference_ceiling_acknowledged": True})
                assert accepted.status_code == 200, accepted.text
                programme = accepted.json()
                job = await discovery.admit(goal_id=goal_id, programme_id=programme["id"], grant_revision=1)
                assert (await discovery.run(job["job_id"]))["status"] == "succeeded"
                witness = await physical_discovery_inputs(jobs, job["job_id"])
                snapshot = next(a["parsed"] for a in witness.artifacts.values() if a["kind"] == "snapshot")
                assert "grants deadline:" in "\n".join(snapshot.lines)
                brief = next(a["parsed"] for a in witness.artifacts.values() if a["kind"] == "brief")
                now = datetime.now(timezone.utc)
                assert len(digest.deterministic_deadlines(witness, brief, now)) == 1
                bindings.append((goal_id, programme["id"], job["job_id"]))
            assert len(calls) == 6 and len(physical_contacts) == 4
            now = datetime.now(timezone.utc)
            local = digest.local_clock(now)
            delivery_now = max(now, local.replace(hour=8, minute=0, second=0, microsecond=0).astimezone(timezone.utc))
            import src.observer.native_notification_queue as native_queue
            monkeypatch.setattr(native_queue, "_utc_now", lambda: delivery_now)
            preferences = "/api/guardian/inbox/programme-notifications"
            # Explicit opt-in with nonmatching category cannot deliver urgency.
            opted = await client.post(preferences, json={"enabled": True, "deadline_categories": ["research"]})
            assert opted.status_code == 200, opted.text
            import src.workflows.research_sources as actual_sources
            original_physical_read = actual_sources.physical_discovery_inputs
            physical_reads = []
            async def count_physical_reads(*args, **kwargs):
                physical_reads.append(args[1] if len(args) > 1 else kwargs.get("job_id"))
                return await original_physical_read(*args, **kwargs)
            monkeypatch.setattr(actual_sources, "physical_discovery_inputs", count_physical_reads)
            await digest.tick(delivery_now)
            async with factory.accounting_sessions() as db:
                notices = list((await db.execute(select(NativeNotificationOutbox))).scalars())
                assert len(notices) == 1 and notices[0].intervention_type == "programme_digest"
                issuer = await db.get(OperatorSession, notices[0].operator_session_id)
                assert issuer.principal_id == notices[0].owner_principal_id
                root_id, principal_id = issuer.id, issuer.principal_id
            unchanged_category_reads = len(physical_reads)
            touched_root = await client.get("/api/auth/session")
            assert touched_root.status_code == 200 and touched_root.json()["session_id"] == root_id
            await digest.deliver_notices(delivery_now + timedelta(seconds=15))
            await digest.deliver_notices(delivery_now + timedelta(seconds=30))
            assert len(physical_reads) == unchanged_category_reads
            assert len(calls) == 6 and len(physical_contacts) == 4
            opted = await client.post(preferences, json={"enabled": True, "deadline_categories": ["grants"]})
            assert opted.status_code == 200, opted.text
            await digest.deliver_notices(delivery_now)
            assert len(physical_reads) > unchanged_category_reads
            await digest.tick(delivery_now + timedelta(minutes=1))
            await digest.deliver_notices(delivery_now + timedelta(minutes=2))
            projected = await client.get("/api/guardian/inbox/programme-digests")
            assert projected.status_code == 200, projected.text
            receipt = projected.json()
            assert len(receipt["digests"]) == 1 and len(receipt["digests"][0]["digest"]["programme_ids"]) == 2
            assert receipt["notifications"]["digest_slots_remaining"] == receipt["notifications"]["deadline_slots_remaining"] == 0
            async with factory.accounting_sessions() as db:
                notices = list((await db.execute(select(NativeNotificationOutbox))).scalars())
                assert len(notices) == 2 and {n.intervention_type for n in notices} == {"programme_digest", "programme_deadline"}
                assert all(n.operator_session_id == root_id and n.owner_principal_id == principal_id and n.max_attempts == 1 for n in notices)
            claimed_ids = []
            for index in range(2):
                worker = f"owned-deadline-daemon-{index}"
                response = await client.get("/api/observer/notifications/next", params={"worker_id": worker}, headers={"X-Seraph-Daemon-Id": worker})
                assert response.status_code == 200, response.text
                claimed = response.json()["notification"]
                assert claimed is not None and claimed["delivery_status"] == "claimed" and claimed["fencing_token"] > 0
                claimed_ids.append(claimed["id"])
            assert len(set(claimed_ids)) == 2
            no_third = await client.get("/api/observer/notifications/next", params={"worker_id": "owned-third-daemon"}, headers={"X-Seraph-Daemon-Id": "owned-third-daemon"})
            assert no_third.status_code == 200 and no_third.json()["notification"] is None
            actual_findings = receipt["digests"][0]["findings"]
            assert len(actual_findings) == 2 and all(f["actionable"] for f in actual_findings)
            deferred, dismissed = actual_findings
            from src.auth.service import authenticate_session
            from src.db.models import ProgrammeFindingAction, ProgrammeFollowThrough
            from src.guardian.goal_programmes import GoalProgrammeError
            operator = await authenticate_session(root_id, touch=False)
            stale_clock = datetime.now(timezone.utc) + timedelta(hours=49)
            for action_kind in ("snooze", "dismiss"):
                with pytest.raises(GoalProgrammeError, match="programme_finding_refresh_required"):
                    await digest.action(operator, deferred["id"], digest.FindingAction(action=action_kind,
                        until=stale_clock + timedelta(hours=1) if action_kind == "snooze" else None,
                        idempotency_key="stale-source-" + action_kind), now=stale_clock)
            async with factory.accounting_sessions() as db:
                assert list((await db.execute(select(ProgrammeFindingAction))).scalars()) == []
                assert list((await db.execute(select(ProgrammeFollowThrough))).scalars()) == []
            deferred_path = f"/api/guardian/inbox/programme-findings/{deferred['id']}/actions"
            dismissed_path = f"/api/guardian/inbox/programme-findings/{dismissed['id']}/actions"
            deferred_body = {"action": "snooze", "until": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                "desired_outcome": "Review the original cited grants evidence tomorrow", "idempotency_key": "actual-deadline-defer"}
            dismissed_body = {"action": "dismiss", "desired_outcome": "Suppress this reviewed finding", "idempotency_key": "actual-deadline-dismiss"}
            for path, action in ((deferred_path, deferred_body), (dismissed_path, dismissed_body)):
                changed = await client.post(path, json=action)
                assert changed.status_code == 200, changed.text
                replay = await client.post(path, json=action)
                assert replay.status_code == 200 and replay.json() == changed.json()
            reloaded = await client.get("/api/guardian/inbox/programme-digests")
            assert reloaded.status_code == 200, reloaded.text
            retained = {f["id"]: f for d in reloaded.json()["digests"] for f in d["findings"]}
            assert retained[deferred["id"]]["follow_through"]["status"] == "deferred"
            assert datetime.fromisoformat(retained[deferred["id"]]["follow_through"]["due_at"].replace("Z", "+00:00")) == datetime.fromisoformat(deferred_body["until"])
            assert retained[dismissed["id"]]["follow_through"]["status"] == "dismissed"
            assert not retained[deferred["id"]]["actionable"] and not retained[dismissed["id"]]["actionable"]
            for path in (deferred_path, dismissed_path):
                blocked = await client.post(path, json={"action": "accept_followup", "desired_outcome": "Try a new task", "idempotency_key": "must-not-restore-suppressed"})
                assert blocked.status_code == 409, blocked.text
            assert len(calls) == 6 and len(physical_contacts) == 4
            # Admit the real next UTC occurrence without executing it. Yesterday's
            # actual completed physical source must never satisfy today's digest.
            next_day = (delivery_now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
            monkeypatch.setattr(goal_programme_service, "_clock", lambda: next_day)
            held = await discovery.admit(goal_id=bindings[0][0], programme_id=bindings[0][1], grant_revision=1)
            assert held["job_id"] != bindings[0][2] and held["status"] in {"accepted", "queued"}
            pending_read = await client.get("/api/guardian/inbox/programme-digests")
            assert pending_read.status_code == 200, pending_read.text
            pending_status = next(p for p in pending_read.json()["programmes"] if p["id"] == bindings[0][1])
            completed_status = next(p for p in receipt["programmes"] if p["id"] == bindings[0][1])
            assert pending_status["current_run_status"] == held["status"]
            assert pending_status["next_source_state"] == "held"
            assert pending_status["next_source_eligible_at"] is None
            assert pending_status["next_source_reason"] == "source_run_in_progress"
            assert pending_status["current_admitted_at"] is not None
            assert pending_status["last_run"] == completed_status["last_run"] is not None
            assert pending_status["current_admitted_at"] != pending_status["last_run"]
            assert pending_status["sources_checked"] == completed_status["sources_checked"] == 1
            assert pending_status["output"] == completed_status["output"] is not None
            async def forbid_previous_physical_source(*args, **kwargs):
                raise AssertionError("A current held/missing occurrence must not reopen yesterday's physical source")
            monkeypatch.setattr(actual_sources, "physical_discovery_inputs", forbid_previous_physical_source)
            await digest.tick(next_day)
            await digest.tick(next_day + timedelta(minutes=6))
            from src.db.models import ProgrammeDigestReceipt
            async with factory.accounting_sessions() as db:
                next_receipt = await db.scalar(select(ProgrammeDigestReceipt).where(
                    ProgrammeDigestReceipt.local_date == next_day.date().isoformat()))
                assert next_receipt is not None and next_receipt.phase == "finalized"
                next_digest = json.loads(next_receipt.digest_json)
                assert next_digest["finding_ids"] == next_digest["prepared_outputs"] == []
                assert "programme_current_output_unresolved" in next_digest["blocked_reasons"]
            assert len(calls) == 6 and len(physical_contacts) == 4
            # A real fresh Root for the same identity has no selected Goal recovery.
            # Read projection must not expose the previous Root's private sources.
            fresh_login = await client.post("/api/auth/login", json={"password": "research-vertical-private-secret"})
            assert fresh_login.status_code == 200, fresh_login.text
            unselected = await client.get("/api/guardian/inbox/programme-digests")
            assert unselected.status_code == 200, unselected.text
            assert unselected.json()["digests"] == unselected.json()["programmes"] == []
            assert len(calls) == 6 and len(physical_contacts) == 4
            print(json.dumps({"flow": "actual_cited_deadline_two_programmes_owner_day_daemon_claim", "programme_jobs": bindings,
                "source_http_sha256": hashlib.sha256(literal_source).hexdigest(), "native_claim_ids": claimed_ids, "owner_root": root_id,
                "digest_notices": 1, "deadline_notices": 1, "nonmatching_category_deadline_notices": 0,
                "actual_public_http_contacts": 4, "scripted_inference_requests": 6, "real_provider_contacts": 0, "real_spend": 0}, sort_keys=True))
    finally:
        await discovery.stop(); await goal_programme_service.stop()
