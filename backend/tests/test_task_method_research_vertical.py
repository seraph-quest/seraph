"""Actual completed dossier -> signed method -> actual Discovery consumer.

Provider replies alone are scripted. Public network transport/DNS terminate in
an owned ASGI application that serves physical local source bytes. No successful
Task, Attempt, native proof, Root, source token or method authority is seeded.
"""
import hashlib
import json

import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.responses import FileResponse, HTMLResponse
from sqlalchemy import select

from config.settings import settings
from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth, ResponseBytes
from src.auth.middleware import OperatorAuthMiddleware
from src.workflows.job_runtime import DurableJobRepository
from src.db.task_method_models import TaskMethodActive


@pytest.mark.asyncio
@pytest.mark.parametrize("termination", ["default_baseline", "completed", "tombstone", "programme_revoked", "identity_revoked"])
async def test_genuine_research_producer_signed_adoption_discovery_and_pinned_rollback(accounting_db, real_auth, monkeypatch, termination):
    from src.api import auth, goals, memory, work_board
    from src.work_board.contracts import WorkBoardOwner
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.db.models import Memory, MemoryProposal, WorkBoardTask, WorkBoardAttempt
    from src.memory.task_lessons import ResearchStrategy
    from src.memory.task_methods import CurrentMethod, TaskMethodStrategyResolver
    from src.guardian.goal_programmes import goal_programme_service
    from src.guardian.goal_discovery import GoalDiscoveryService
    from src.guardian.discovery_search import DiscoverySearch
    from src.workflows.research_sources import physical_discovery_inputs
    from tests.test_general_task_planner import prepare
    from tests.test_task_methods import review

    root, _engine, factory = accounting_db
    root.chmod(0o700)
    sessions = factory.accounting_sessions
    monkeypatch.setattr("src.memory.m5.get_session", sessions)
    monkeypatch.setattr("src.memory.repository.get_session", sessions)
    monkeypatch.setattr(settings, "browser_site_allowlist", "example.com")
    monkeypatch.setattr(settings, "browser_site_blocklist", "")
    source_path = root / "owned-public-source.txt"
    source_path.write_bytes(b"Official release evidence from the owned public-source application.\n"
        b"2026-10-09: finite attributed release statement.\n"
        b"A fixture verifies mechanics only; semantic truth is not established.\n")
    public_app = FastAPI()
    public_requests = []
    @public_app.get("/release")
    async def release(request: Request):
        assert request.headers["host"] == "example.com"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        public_requests.append(("GET", "/release"))
        return FileResponse(source_path, media_type="text/plain")

    @public_app.get("/official-release")
    async def official_release(request: Request):
        assert request.headers["host"] == "example.com"
        public_requests.append(("GET", "/official-release"))
        return FileResponse(source_path, media_type="text/plain")

    @public_app.post("/html/")
    async def search(request: Request):
        from urllib.parse import parse_qs
        assert request.headers["host"] == "html.duckduckgo.com"
        fields = parse_qs((await request.body()).decode(), keep_blank_values=True)
        assert set(fields) == {"q", "b", "kl"}
        public_requests.append(("POST", "/html/", fields["q"]))
        return HTMLResponse('<html><a class="result__a" href="https://example.com/release">Release evidence</a>'
            '<a class="result__a" href="https://example.com/official-release">Official dated release</a></html>')

    original_client = httpx.AsyncClient
    class PublicBoundary(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert request.url.scheme == "https"
            assert request.headers["host"] in {"example.com", "html.duckduckgo.com"}
            assert request.extensions["sni_hostname"] == request.headers["host"]
            async with original_client(transport=httpx.ASGITransport(app=public_app), trust_env=False) as client:
                result = await client.request(request.method, "https://" + request.headers["host"] + request.url.raw_path.decode(),
                    headers=request.headers, content=request.content)
            return httpx.Response(result.status_code, request=request, headers=result.headers,
                stream=ResponseBytes(result.content))
    public_transport = PublicBoundary()
    def public_dns(host, port):
        assert host in {"example.com", "html.duckduckgo.com"} and port == 443
        return ["93.184.216.34"]
    monkeypatch.setattr("src.browser.pinned_transport._blocking_default_resolver", public_dns)
    calls = []
    discovery_inputs = []
    class FinalProviderBoundary(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.host != "openrouter.ai":
                return await public_transport.handle_async_request(request)
            assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
            body = json.loads(request.content)
            calls.append(body)
            supplied = json.loads(body["messages"][1]["content"])
            if "untrusted_public_data" in supplied:
                supplied = supplied["untrusted_public_data"]
                discovery_inputs.append(supplied)
                method = supplied.get("research_strategy")
                if supplied.get("task") == "plan_queries":
                    answer = {"queries": method["query_templates"] if method else ["public release evidence"]}
                elif "manifest_ref" in supplied:
                    chosen = next((item for item in supplied["results"] if method and item["title"] == "Official dated release"),
                        supplied["results"][0])
                    answer = {"selected_result_ids": [chosen["result_id"]]}
                else:
                    source = supplied["untrusted_quoted_sources"][0]
                    answer = {"schema_version": 1, "perspective": "Prepare a public Goal discovery brief",
                        "claims": [{"text": (method["draft_sections"][0] + ": " if method else "") + "The supplied public release evidence is attributed.",
                            "citations": [{key: source[key] for key in ("source_id", "first_line", "last_line", "span_sha256")}]}],
                        "uncertainty": method["stop_conditions"] if method else ["Fixture mechanics only."],
                        "contradictions": [], "no_learning": True}
            else:
                source = supplied["untrusted_quoted_sources"][0]
                answer = {"schema_version": 1, "perspective": supplied["perspective_instruction"],
                    "claims": [{"text": "The supplied release source is attributed.",
                        "citations": [{key: source[key] for key in ("source_id", "first_line", "last_line", "span_sha256")}]}],
                    "uncertainty": ["Fixture mechanics only."], "contradictions": [], "no_learning": True}
            payload = {"id": "functional-research-" + str(len(calls)), "usage": {"cost": "0"},
                "choices": [{"message": {"role": "assistant", "content": json.dumps(answer)}}]}
            return httpx.Response(200, request=request, stream=ResponseBytes(json.dumps(payload).encode()))
    def owned_clients(*args, **kwargs):
        if "transport" not in kwargs:
            kwargs["transport"] = FinalProviderBoundary()
        return original_client(*args, **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", owned_clients)
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router, prefix="/api/auth")
    for router in (goals.router, memory.router, work_board.router):
        app.include_router(router, prefix="/api")
    jobs = DurableJobRepository()
    current = CurrentMethod()
    await current.start()
    await goal_programme_service.start()
    discovery = GoalDiscoveryService(jobs=jobs, search=DiscoverySearch(resolver=public_dns, transport=public_transport),
        resolver=public_dns, transport=public_transport, strategy_resolver=TaskMethodStrategyResolver(current))
    await discovery.start()
    from src.work_board.dispatcher import _dispatcher
    monkeypatch.setattr(_dispatcher, "goal_discovery", discovery)
    try:
        async with original_client(transport=httpx.ASGITransport(app=app), base_url="http://test",
                headers={"origin": "http://localhost:3001"}) as client:
            login = await client.post("/api/auth/login", json={"password": "research-vertical-private-secret"})
            assert login.status_code == 200, login.text
            owner_data = login.json()
            assert (await client.post("/api/auth/ownership/enroll")).status_code == 200
            owner = WorkBoardOwner(principal_id=owner_data["principal_id"], session_id=owner_data["session_id"])
            # Literal disposable capability metadata only; no canary execution.
            await prepare((root, _engine, sessions), monkeypatch, existing_owner=owner)
            goal = await client.post("/api/goals", json={"title": "Private owned research Goal",
                "admission_budget": {"reviewed_grant": True, "grant_id": "owned-research-grant",
                    "max_outstanding_jobs": 1, "max_attempts": 1, "max_runtime_seconds": 300}})
            assert goal.status_code == 200, goal.text
            goal_id = goal.json()["id"]
            if termination == "default_baseline":
                request = {"expected_goal_revision": 1, "expected_grant_revision": 0,
                    "public_brief": "Track public release evidence", "budget": {"max_inference_microusd": 500}}
                base = "/api/goals/" + goal_id + "/programmes"
                preview = await client.post(base + "/preview", json=request)
                assert preview.status_code == 200, preview.text
                accepted = await client.post(base + "/accept", json=request | {
                    "review_digest": preview.json()["review_digest"], "public_web_acknowledged": True,
                    "local_artifacts_acknowledged": True, "inference_ceiling_acknowledged": True})
                assert accepted.status_code == 200, accepted.text
                programme = accepted.json()
                job = await discovery.admit(goal_id=goal_id, programme_id=programme["id"], grant_revision=1)
                witness = await physical_discovery_inputs(jobs, job["job_id"])
                assert witness.plan.strategy_binding.status == "none"
                assert witness.plan.strategy_binding.reason == "baseline"
                result = await discovery.run(job["job_id"])
                assert result["status"] == "succeeded", result
                assert len(calls) == len(discovery_inputs) == 3
                assert all(item["strategy_ref"]["status"] == "none" for item in discovery_inputs[:2])
                assert "strategy_ref" not in discovery_inputs[2]
                assert all("research_strategy" not in item for item in discovery_inputs)
                snapshot = await jobs.inference_accounting_snapshot()
                assert len(snapshot["operations"]) == 3
                (root / "research-method-default-proof.json").write_text(json.dumps({"job": await jobs.get_job(job["job_id"]),
                    "discovery_inputs": discovery_inputs, "public_requests": public_requests, "accounting": snapshot}, indent=2))
                return
            inputs = {"schema_version": 1, "question": "Describe the selected public release evidence",
                "perspectives": [{"instruction": "Summarize selected evidence", "source_slots": [0]}],
                "sources": [{"kind": "public_https_text", "url": "https://example.com/release", "first_line": 1, "last_line": 3}],
                "source_egress_acknowledged": True, "no_learning": True}
            artifact = await client.post("/api/work-board/input-artifacts", json={"schema_version": 1,
                "capability_id": "work.research-dossier.v1", "goal_id": goal_id, "goal_revision": 1,
                "input": inputs, "idempotency_key": "actual-method-research-input"})
            assert artifact.status_code == 200, artifact.text
            created = await client.post("/api/work-board/tasks", json={"title": "Actual source for reviewed strategy",
                "goal_id": goal_id, "goal_revision": 1, "status": "todo", "capability_id": "work.research-dossier.v1",
                "input_artifact_id": artifact.json()["artifact_id"], "idempotency_key": "actual-method-research-task"})
            assert created.status_code == 200, created.text
            task_id = created.json()["task"]["task_id"]
            dispatcher = WorkBoardDispatcher(jobs=jobs, session_provider=sessions, strategy_resolver=TaskMethodStrategyResolver(current))
            monkeypatch.setattr(work_board, "dispatcher", dispatcher)
            receipt = await dispatcher.run_pass()
            assert receipt["completed"] == 1, receipt
            async with sessions() as db:
                task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
                attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task_id))
                assert attempt.ended_at is not None
                attempt_id = attempt.attempt_id
                if task.status.value == "review":
                    revision = task.task_revision
                else:
                    assert task.status.value == "done"
                    revision = None
            if revision is not None:
                completed = await client.post("/api/work-board/tasks/" + task_id + "/actions",
                    json={"action": "complete_review", "expected_revision": revision, "attempt_id": attempt_id})
                assert completed.status_code == 200, completed.text
            source = await client.get("/api/memory/task-lessons/sources/" + task_id)
            assert source.status_code == 200 and source.json()["source_refs"], source.text
            strategy = ResearchStrategy(query_templates=["official dated release evidence"], source_preferences=["official", "dated"],
                required_evidence_fields=["url", "date", "excerpt", "limitation"], draft_sections=["Evidence", "Limitations"],
                stop_conditions=["Stop without attributed evidence"])
            source_data = source.json()
            candidate = await client.post("/api/memory/research-methods", json={
                key: source_data[key] for key in ("task_id", "attempt_id", "source_refs", "scope", "expected_revision")} | {
                    "strategy": strategy.model_dump(mode="json")})
            assert candidate.status_code == 201, candidate.text
            proposal_id = candidate.json()["proposal_id"]
            preview = await client.get("/api/memory/task-methods/" + proposal_id)
            assert preview.status_code == 200, preview.text
            accepted = await client.post("/api/memory/task-methods/actions", json=review(preview.json(), "accept", "actual-research-adopt").model_dump(mode="json"))
            assert accepted.status_code == 200, accepted.text
            binding = await current.resolve(owner, goal_id, "guardian.goal-discovery.v1")
            assert binding.status == "active" and binding.typed_data == strategy.model_dump(mode="json")
            assert (await current.resolve(owner, goal_id, "work.research-dossier.v1")).status == "none"
            async with sessions() as db:
                proposal = await db.get(MemoryProposal, proposal_id)
                canonical = await db.get(Memory, binding.version)
                pointer = await db.scalar(select(TaskMethodActive))
                assert proposal.accepted_memory_id == canonical.id == binding.version
                assert json.loads(pointer.binding_json)["digest"] == binding.digest
                assert current._key is not None
            request = {"expected_goal_revision": 1, "expected_grant_revision": 0,
                "public_brief": "Track public product release evidence", "budget": {"max_inference_microusd": 500}}
            base = "/api/goals/" + goal_id + "/programmes"
            programme_preview = await client.post(base + "/preview", json=request)
            assert programme_preview.status_code == 200, programme_preview.text
            programme_response = await client.post(base + "/accept", json=request | {
                "review_digest": programme_preview.json()["review_digest"], "public_web_acknowledged": True,
                "local_artifacts_acknowledged": True, "inference_ceiling_acknowledged": True})
            assert programme_response.status_code == 200, programme_response.text
            programme = programme_response.json()
            job = await discovery.admit(goal_id=goal_id, programme_id=programme["id"], grant_revision=1)
            original_job = await jobs.get_job(job["job_id"])
            rollback_preview = await client.get("/api/memory/task-methods/" + proposal_id)
            rolled_back = await client.post("/api/memory/task-methods/actions",
                json=review(rollback_preview.json(), "rollback", "actual-research-rollback").model_dump(mode="json"))
            assert rolled_back.status_code == 200, rolled_back.text
            assert (await current.resolve(owner, goal_id, "guardian.goal-discovery.v1")).status == "none"
            from src.work_board.research_parent import discovery_authority
            await discovery._validate_pinned_strategy(
                discovery_authority(original_job["declared_authority"]).programme_binding, binding)
            if termination != "completed":
                before_contacts = list(public_requests)
                if termination == "tombstone":
                    from src.memory.repository import memory_repository
                    await memory_repository.mark_memory_tombstoned(binding.version,
                        actor="operator", reason="Actual owned method deletion")
                elif termination == "programme_revoked":
                    response = await client.post(base + "/" + programme["id"] + "/revoke",
                        json={"expected_grant_revision": 1, "recover_owner_acknowledged": False})
                    assert response.status_code == 200, response.text
                else:
                    response = await client.post("/api/auth/ownership/revoke")
                    assert response.status_code == 204, response.text
                with pytest.raises(Exception):
                    await discovery.run(job["job_id"])
                assert len(calls) == 1 and not discovery_inputs
                assert public_requests == before_contacts
                denied_job = await jobs.get_job(job["job_id"])
                assert denied_job["status"] != "succeeded"
                assert denied_job["deadline_at"] == original_job["deadline_at"]
                (root / "research-method-negative-proof.json").write_text(json.dumps({
                    "termination": termination, "source": source_data, "binding": binding.model_dump(mode="json"),
                    "original_job": original_job, "denied_job": denied_job,
                    "accounting": await jobs.inference_accounting_snapshot(), "public_requests": public_requests}, indent=2))
                return
            result = await discovery.run(job["job_id"])
            assert result["status"] == "succeeded", result
            assert len(discovery_inputs) == 3
            expected_ref = {"status": "active", "method_id": binding.method_id, "version": binding.version, "digest": binding.digest}
            expected_fields = [("query_templates",), ("source_preferences", "required_evidence_fields"),
                ("draft_sections", "required_evidence_fields", "stop_conditions")]
            for supplied, fields in zip(discovery_inputs, expected_fields):
                assert supplied["strategy_ref"] == expected_ref
                assert supplied["research_strategy"] == {"schema_version": "ResearchStrategy.v1",
                    **{key: binding.typed_data[key] for key in fields}}
            assert ("POST", "/html/", strategy.query_templates) in public_requests
            assert ("GET", "/official-release") in public_requests
            witness = await physical_discovery_inputs(jobs, job["job_id"])
            output = next(item for item in witness.artifacts.values() if item["kind"] == "brief")
            assert output["parsed"]["findings"] and output["parsed"]["coverage"]["source_spans"]
            assert "Evidence:" in json.dumps(output["parsed"])
            completed_job = await jobs.get_job(job["job_id"])
            assert completed_job["declared_authority"] == original_job["declared_authority"]
            assert completed_job["deadline_at"] == original_job["deadline_at"]
            snapshot = await jobs.inference_accounting_snapshot()
            assert len(snapshot["operations"]) == len(calls) == 4
            assert all(item["state"] == "settled" and item["actual_cost_microusd"] == 0 for item in snapshot["operations"])
            from datetime import timedelta
            clock = goal_programme_service._clock()
            monkeypatch.setattr(goal_programme_service, "_clock", lambda: clock + timedelta(days=1))
            future = await discovery.admit(goal_id=goal_id, programme_id=programme["id"], grant_revision=1)
            assert future["job_id"] != job["job_id"]
            future_witness = await physical_discovery_inputs(jobs, future["job_id"])
            assert future_witness.plan.strategy_binding.status == "none"
            assert future_witness.plan.strategy_binding.reason == "baseline"
            assert len(calls) == 4
            (root / "research-method-vertical-proof.json").write_text(json.dumps({"task_id": task_id,
                "attempt_id": attempt_id, "source": source_data, "proposal": candidate.json(),
                "accepted": accepted.json(), "binding": binding.model_dump(mode="json"),
                "original_job": original_job, "completed_job": completed_job,
                "future_baseline_job": await jobs.get_job(future["job_id"]),
                "discovery_inputs": discovery_inputs, "accounting": snapshot,
                "public_requests": public_requests, "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest()}, indent=2))
    finally:
        await discovery.stop()
        await goal_programme_service.stop()
        await current.stop()
