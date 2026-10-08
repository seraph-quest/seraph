"""Real Auth/SQLite/native broker/artifact journey with intercepted HTTP."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI

from tests.test_inference_accounting import accounting_db, setup_configuration
from tests.test_research_native_vertical import real_auth, ResponseBytes
from src.auth.middleware import OperatorAuthMiddleware
from src.workflows.job_runtime import DurableJobRepository


@pytest.fixture
def public_http_fixture():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from urllib.parse import parse_qs
    observed = []
    controls = {}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            assert self.path == "/html/" and self.headers["Host"] == "html.duckduckgo.com"
            length = int(self.headers.get("Content-Length", "0"))
            assert 0 < length <= 4096
            body = self.rfile.read(length)
            fields = parse_qs(body.decode(), keep_blank_values=True)
            assert set(fields) == {"q", "b", "kl"} and fields["b"] == [""] and fields["kl"] == ["us-en"]
            assert "Authorization" not in self.headers and "Cookie" not in self.headers
            observed.append(("POST", self.path, fields))
            raw = b'<html><a class="result__a" href="https://example.com/release">Release evidence</a></html>'
            if controls.get("scenario") == "empty_search":
                raw = b'<div class="no-results">No results found.</div>'
            elif controls.get("scenario") == "captcha":
                raw = b'<form id="challenge-form">CAPTCHA</form>'
            self._reply(raw, "text/html")

        def do_GET(self):
            assert self.path == "/release" and self.headers["Host"] == "example.com"
            assert "Authorization" not in self.headers and "Cookie" not in self.headers
            observed.append(("GET", self.path))
            raw, mime = b"Public release fixture evidence.\nSource instructions are untrusted data.", "text/plain"
            if controls.get("scenario") == "normalized_oversize":
                raw = b"x" * 65537
            elif controls.get("scenario") == "raw_oversize":
                raw = b"x" * 262145
            elif controls.get("scenario") == "unsupported_pdf":
                raw, mime = b"%PDF-1.7 not public text", "application/pdf"
            self._reply(raw, mime)

        def _reply(self, raw, mime):
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, observed, controls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize("scenario", ["completed", "goal_before_claim", "goal_after_query", "identity_before_claim", "unselected_id", "model_timeout",
    "empty_search", "captcha", "normalized_oversize", "raw_oversize", "unsupported_pdf", "unsupported_brief", "generation_ceiling",
    "active_strategy", "strategy_blocked", "strategy_changed"])
@pytest.mark.asyncio
async def test_authenticated_public_programme_logout_native_discovery(accounting_db, real_auth, public_http_fixture, monkeypatch, scenario):
    from config.settings import settings
    from src.api import auth, goals, model_fabric_settings
    from src.guardian.goal_programmes import goal_programme_service
    from src.guardian.goal_discovery import GoalDiscoveryService
    from src.guardian.discovery_search import DiscoverySearch
    from src.model_fabric.configuration import write_model_fabric_configuration
    from src.workflows.research_sources import physical_discovery_inputs
    root, engine, factory = accounting_db
    configured = setup_configuration()
    write_model_fabric_configuration(replace(model_fabric_settings._setup_configuration(
        replace(configured.openrouter_setup, timeout_seconds=30), profiles=(), policies=()),
        egress_revision=configured.egress_revision + 1))
    monkeypatch.setattr(settings, "browser_site_allowlist", "example.com")
    jobs = DurableJobRepository()
    await jobs.configure_inference_accounting(1000)
    calls, contacts = [], []
    class ModelBoundary(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert request.url.host == "openrouter.ai" and request.method == "POST"
            body = json.loads(request.content)
            if len(body["messages"]) == 1:
                answer = "CANARY_OK"
            else:
                calls.append(body)
                if scenario == "model_timeout":
                    raise httpx.ReadTimeout("owned scripted contact response lost", request=request)
                supplied = json.loads(body["messages"][1]["content"])["untrusted_public_data"]
                if supplied.get("task") == "plan_queries":
                    answer = json.dumps({"queries": ["public product release evidence"]})
                    if scenario == "goal_after_query":
                        from src.goals.repository import GoalRepository
                        await GoalRepository().update(goal_id, description="Changed current Goal after contact", expected_revision=1)
                elif "manifest_ref" in supplied:
                    answer = json.dumps({"selected_result_ids": ["0" * 32 if scenario == "unselected_id" else supplied["results"][0]["result_id"]]})
                else:
                    source = supplied["untrusted_quoted_sources"][0]
                    answer = json.dumps({"schema_version": 1, "perspective": "Prepare a public Goal discovery brief",
                        "claims": [{"text": "The public fixture describes the selected release.",
                            "citations": [{key: source[key] for key in ("source_id", "first_line", "last_line", "span_sha256")}]}],
                        "uncertainty": ["Fixture execution is not a semantic truth claim."], "contradictions": [], "no_learning": True})
            payload = {"id": "fixture-discovery", "choices": [{"message": {"role": "assistant", "content": answer}}],
                "usage": {"cost": "0.000002", "prompt_tokens": 10, "completion_tokens": 10}}
            return httpx.Response(200, request=request, headers={"content-type": "application/json"},
                stream=ResponseBytes(json.dumps(payload).encode()))
    original_client = httpx.AsyncClient
    def clients(**kwargs):
        if "transport" not in kwargs:
            kwargs["transport"] = ModelBoundary()
        return original_client(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", clients)
    port, physical_contacts, physical_controls = public_http_fixture
    physical_controls["scenario"] = scenario
    class PublicFixtureBoundary(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            contacts.append((request.method, request.headers["host"], str(request.url)))
            assert request.url.scheme == "https" and request.url.host == request.headers["host"]
            assert request.extensions["sni_hostname"] == request.headers["host"]
            assert request.headers["host"] in {"html.duckduckgo.com", "example.com"}
            # Literal owned TCP fixture after production public DNS/pin checks.
            # No real DDG, source TLS or external reachability is claimed.
            async with original_client(transport=httpx.AsyncHTTPTransport(), trust_env=False,
                    follow_redirects=False, timeout=5) as fixture:
                response = await fixture.request(request.method, f"http://127.0.0.1:{port}{request.url.raw_path.decode()}",
                    headers=request.headers, content=request.content)
            return httpx.Response(response.status_code, request=request, headers=response.headers,
                stream=ResponseBytes(response.content))
    def resolver(host, port):
        assert host in {"html.duckduckgo.com", "example.com"} and port == 443
        return ["93.184.216.34"]
    transport = PublicFixtureBoundary()
    strategy_state = {"changed": False}
    resolver_calls = []
    class AcceptedStrategy:
        def resolve(self, owner, goal_ref, family, programme_grant=None):
            from src.work_board.contracts import TaskStrategyBinding
            assert owner.principal_id == "service:guardian-goal-programmes" and owner.session_id == ""
            assert family == "guardian.goal-discovery.v1" and programme_grant.goal_id == goal_ref
            resolver_calls.append((goal_ref, programme_grant.programme_id))
            if scenario == "strategy_blocked":
                return TaskStrategyBinding(status="blocked", reason="method_review_required")
            return TaskStrategyBinding(status="active", method_id="accepted-public-method", version="1",
                digest=("f" if strategy_state["changed"] else "e") * 64,
                typed_data={"kind": "public_research_method", "origin": "operator_accepted"})
    service = GoalDiscoveryService(jobs=jobs, search=DiscoverySearch(resolver=resolver, transport=transport),
        resolver=resolver, transport=transport,
        strategy_resolver=AcceptedStrategy() if scenario in {"active_strategy", "strategy_blocked", "strategy_changed"} else None)
    monkeypatch.setattr("src.guardian.goal_discovery.goal_discovery_service", service)
    from src.work_board.dispatcher import _dispatcher
    monkeypatch.setattr(_dispatcher, "goal_discovery", service)
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(goals.router, prefix="/api")
    app.include_router(model_fabric_settings.router, prefix="/api")
    await goal_programme_service.start()
    await service.start()
    try:
        async with original_client(transport=httpx.ASGITransport(app=app), base_url="http://test",
                headers={"origin": "http://localhost:3001"}) as client:
            login = await client.post("/api/auth/login", json={"password": "research-vertical-private-secret"})
            assert login.status_code == 200, login.text
            enrolled = await client.post("/api/auth/ownership/enroll")
            assert enrolled.status_code == 200, enrolled.text
            for capability in ("text", "latency_ms", "health"):
                canary = await client.post("/api/settings/model-fabric/canary", json={"profile_id": "openrouter",
                    "capability": capability, "timeout_seconds": 30})
                assert canary.status_code == 200 and canary.json()["outcome"] == "passed", canary.text
            created = await client.post("/api/goals", json={"title": "PRIVATE_GOAL_TITLE_CANARY", "description": "PRIVATE_GOAL_DESCRIPTION_CANARY"})
            assert created.status_code == 200, created.text
            goal_id = created.json()["id"]
            request = {"expected_goal_revision": 1, "expected_grant_revision": 0,
                "public_brief": "é" * 1500 if scenario == "unsupported_brief" else "Track public product release evidence",
                "budget": {"max_inference_microusd": 105 if scenario == "generation_ceiling" else 500}}
            base = f"/api/goals/{goal_id}/programmes"
            preview = await client.post(base + "/preview", json=request)
            assert preview.status_code == 200, preview.text
            accepted = await client.post(base + "/accept", json={**request, "review_digest": preview.json()["review_digest"],
                "public_web_acknowledged": True, "local_artifacts_acknowledged": True, "inference_ceiling_acknowledged": True})
            assert accepted.status_code == 200, accepted.text
            programme = accepted.json()
            logout = await client.post("/api/auth/logout")
            assert logout.status_code == 204
            if scenario == "strategy_blocked":
                with pytest.raises(ValueError, match="programme_strategy_blocked"):
                    await service.admit(goal_id=goal_id, programme_id=programme["id"], grant_revision=1)
                assert calls == [] and contacts == [] and physical_contacts == []
                assert resolver_calls
                return
            job = await service.admit(goal_id=goal_id, programme_id=programme["id"], grant_revision=1)
            assert job["status"] == "queued", job
            deadline = job["deadline_at"]
            if scenario == "strategy_changed":
                strategy_state["changed"] = True
                with pytest.raises(ValueError, match="programme accepted strategy binding changed"):
                    await service.run(job["job_id"])
                assert calls == [] and contacts == [] and physical_contacts == []
                return
            if scenario in {"goal_before_claim", "identity_before_claim"}:
                if scenario == "goal_before_claim":
                    from src.goals.repository import GoalRepository
                    await GoalRepository().update(goal_id, description="Changed before first native contact", expected_revision=1)
                else:
                    from src.db.models import OperatorIdentity
                    async with factory.accounting_sessions() as db:
                        identity = await db.get(OperatorIdentity, enrolled.json()["operator_identity_id"])
                        identity.revoked_at = datetime.now(timezone.utc)
                with pytest.raises(ValueError):
                    await service.run(job["job_id"])
                assert calls == [] and contacts == [] and physical_contacts == []
                assert (await jobs.get_job(job["job_id"]))["deadline_at"] == deadline
                return
            if scenario in {"goal_after_query", "unselected_id", "model_timeout", "captcha", "raw_oversize"}:
                with pytest.raises(Exception):
                    await service.run(job["job_id"])
                blocked = await jobs.get_job(job["job_id"])
                assert blocked["status"] in {"running", "blocked"} and blocked["deadline_at"] == deadline
                expected_calls = 2 if scenario in {"unselected_id", "raw_oversize"} else 1
                assert len(calls) == expected_calls
                assert len(contacts) == (2 if scenario == "raw_oversize" else 1 if scenario in {"unselected_id", "captcha"} else 0)
                if scenario != "raw_oversize":
                    assert all(item[0] != "GET" for item in physical_contacts)
                original_effects = json.dumps(blocked["effects"], sort_keys=True)
                clock = goal_programme_service._clock()
                monkeypatch.setattr(goal_programme_service, "_clock", lambda: clock + timedelta(days=1))
                with pytest.raises(Exception):
                    await service.admit(goal_id=goal_id, programme_id=programme["id"], grant_revision=1)
                assert json.dumps((await jobs.get_job(job["job_id"]))["effects"], sort_keys=True) == original_effects
                assert len(calls) == expected_calls
                if scenario == "model_timeout":
                    debt = await jobs.inference_accounting_snapshot(job_id=job["job_id"])
                    assert debt["operations"][0]["state"] == "unknown" and debt["unknown_microusd"] > 0
                    logged = await client.post("/api/auth/login", json={"password": "research-vertical-private-secret"})
                    assert logged.status_code == 200
                    select_goal = {"selections": [{"kind": "goal", "record_id": goal_id}]}
                    reviewed = await client.post("/api/auth/ownership/recovery/preview", json=select_goal)
                    assert reviewed.status_code == 200
                    confirmed = await client.post("/api/auth/ownership/recovery/confirm", json={**select_goal,
                        "preview_digest": reviewed.json()["preview_digest"], "idempotency_key": "unknown-original-selected",
                        "acknowledge_read_only": True})
                    assert confirmed.status_code == 200
                    fresh = await client.post(f"/api/auth/ownership/recovery/{confirmed.json()['journal_id']}/fresh-work")
                    assert fresh.status_code == 200, fresh.text
                    new_goal = fresh.json()["fresh_work"]["goals"][0]["id"]
                    new_base = f"/api/goals/{new_goal}/programmes"
                    new_request = {**request, "expected_goal_revision": 1, "expected_grant_revision": 0}
                    new_preview = await client.post(new_base + "/preview", json=new_request)
                    assert new_preview.status_code == 200, new_preview.text
                    renewed = await client.post(new_base + "/accept", json={**new_request,
                        "review_digest": new_preview.json()["review_digest"], "public_web_acknowledged": True,
                        "local_artifacts_acknowledged": True, "inference_ceiling_acknowledged": True})
                    assert renewed.status_code == 200, renewed.text
                    with pytest.raises(ValueError, match="programme_outstanding_occurrence_requires_recovery"):
                        await service.admit(goal_id=new_goal, programme_id=renewed.json()["id"], grant_revision=1)
                    assert len(calls) == 1 and physical_contacts == []
                    still = await jobs.get_job(job["job_id"])
                    assert still["effects"] == blocked["effects"] and still["declared_authority"] == blocked["declared_authority"]
                return
            done = await service.run(job["job_id"])
            if scenario in {"empty_search", "normalized_oversize", "unsupported_pdf", "unsupported_brief"}:
                assert done["status"] == "degraded", done
                witness = await physical_discovery_inputs(jobs, job["job_id"])
                output = next(a for a in witness.artifacts.values() if a["kind"] == "brief")["parsed"]
                assert output["findings"] == [] and output["coverage"]["outcome_state"] == "empty"
                assert output["coverage"]["original_public_brief_byte_count"] == len(request["public_brief"].encode())
                assert len(calls) == (0 if scenario == "unsupported_brief" else 1 if scenario == "empty_search" else 2)
                assert len(contacts) == (0 if scenario == "unsupported_brief" else 1 if scenario == "empty_search" else 2)
                if scenario == "unsupported_brief":
                    assert output["coverage"]["status"] == "unsupported" and output["coverage"]["public_brief_fully_represented"] is False
                if scenario in {"normalized_oversize", "unsupported_pdf"}:
                    assert output["coverage"]["unavailable"] and output["coverage"]["source_spans"] == []
                return
            assert done["status"] == "succeeded", done
            assert done["deadline_at"] == deadline and done["session_id"] is None
            witness = await physical_discovery_inputs(jobs, job["job_id"])
            brief = next(a for a in witness.artifacts.values() if a["kind"] == "brief")["parsed"]
            assert brief["coverage"]["outcome_state"] == "findings" and brief["findings"][0]["evidence_status"] == "mechanically_verified"
            assert brief["coverage"]["no_learning"] is True and brief["coverage"]["semantic_truth_verified"] is False
            assert len(brief["prepared_artifact_refs"]) == 1 and brief["proposed_next_steps"][0]["inert"] is True
            assert len(calls) == 3 and len(contacts) == 2
            if scenario == "active_strategy":
                assert witness.plan.strategy_binding.status == "active" and witness.plan.strategy_binding.digest == "e" * 64
                assert len(resolver_calls) > 3
            assert "PRIVATE_GOAL" not in json.dumps(calls)
            for file in root.rglob("*.json"):
                if "goal-programmes" in file.parts:
                    assert "PRIVATE_GOAL" not in file.read_text()
            same = await service.admit(goal_id=goal_id, programme_id=programme["id"], grant_revision=1)
            assert same["job_id"] == job["job_id"] and len(calls) == 3
            snapshot = await jobs.inference_accounting_snapshot(job_id=job["job_id"])
            assert len(snapshot["operations"]) == 3 and all(row["state"] == "settled" for row in snapshot["operations"])
            if scenario == "generation_ceiling":
                # Arrange the already-actually-settled canonical rows in a
                # previous period through the existing accounting witness
                # writer. This is a period rollover fixture, not elapsed host
                # time or provider spending; native IDs/actual charges stay exact.
                from pathlib import Path
                from src.workflows.inference_accounting import _continuity_lock
                async with jobs._session() as db:
                    await jobs._accounting_begin(db)
                    account, rows = await jobs._accounting_rows(db)
                    with _continuity_lock(Path(settings.workspace_dir).resolve()) as workspace:
                        jobs._assert_accounting_continuity(workspace, account, rows)
                        for row in rows:
                            if row.job_id == job["job_id"]:
                                row.period_id = "2026-09"
                                db.add(row)
                        await jobs._persist_accounting_witness(db, workspace, account, rows)
            original_clock = goal_programme_service._clock
            future = original_clock() + timedelta(days=1)
            monkeypatch.setattr(goal_programme_service, "_clock", lambda: future)
            second = await service.admit(goal_id=goal_id, programme_id=programme["id"], grant_revision=1)
            assert second["job_id"] != job["job_id"]
            if scenario == "generation_ceiling":
                with pytest.raises(Exception):
                    await service.run(second["job_id"])
                assert len(calls) == 3 and len(contacts) == 2
                blocked = await jobs.get_job(second["job_id"])
                assert blocked["status"] == "blocked"
                total = await jobs.inference_accounting_snapshot()
                assert len([row for row in total["operations"] if row["job_id"] == job["job_id"]]) == 3
                assert not any(row["job_id"] == second["job_id"] for row in total["operations"])
                return
            second_done = await service.run(second["job_id"])
            assert second_done["status"] == "succeeded"
            second_witness = await physical_discovery_inputs(jobs, second["job_id"])
            quiet = next(a for a in second_witness.artifacts.values() if a["kind"] == "brief")["parsed"]
            assert quiet["coverage"]["outcome_state"] == "quiet" and quiet["findings"] == []
            assert len(calls) == 5 and len(contacts) == 4
            assert len(physical_contacts) == 4 and [item[0] for item in physical_contacts] == ["POST", "GET", "POST", "GET"]
            # Fresh login + explicit existing read-only owner recovery grants
            # selected local inspection only; neither generation is renewed.
            logged = await client.post("/api/auth/login", json={"password": "research-vertical-private-secret"})
            assert logged.status_code == 200
            before_recovery = await client.get(base + "/discovery")
            assert before_recovery.status_code == 409
            selection = {"selections": [{"kind": "goal", "record_id": goal_id}]}
            recovery_preview = await client.post("/api/auth/ownership/recovery/preview", json=selection)
            assert recovery_preview.status_code == 200, recovery_preview.text
            recovery = await client.post("/api/auth/ownership/recovery/confirm", json={**selection,
                "preview_digest": recovery_preview.json()["preview_digest"], "idempotency_key": "public-discovery-reader",
                "acknowledge_read_only": True})
            assert recovery.status_code == 200, recovery.text
            history = await client.get(base + "/discovery")
            assert history.status_code == 200 and len(history.json()["runs"]) == 2, history.text
            selected_brief = await client.get(base + f"/{programme['id']}/discovery/{job['job_id']}/brief")
            assert selected_brief.status_code == 200 and selected_brief.json()["physical_readback"] is True, selected_brief.text
            assert len(selected_brief.json()["prepared_artifacts"]) == 1
            assert selected_brief.json()["prepared_artifacts"][0]["content"]["inert"] is True
            assert len(calls) == 5 and len(contacts) == 4
    finally:
        await service.stop()
        await goal_programme_service.stop()
