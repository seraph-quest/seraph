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
    yield
    _reset_login_throttle_for_tests()


class ResponseBytes(httpx.AsyncByteStream):
    def __init__(self, content):
        self.content = content

    async def __aiter__(self):
        for offset in range(0, len(self.content), 512):
            yield self.content[offset:offset+512]


class ProviderBoundary(httpx.AsyncBaseTransport):
    def __init__(self, calls):
        self.calls = calls
        self.public = httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        if request.url.host == "openrouter.ai":
            assert request.method == "POST" and request.url.path == "/api/v1/chat/completions"
            body = json.loads(request.content)
            self.calls.append(body)
            if len(body["messages"]) == 1:
                content = "CANARY_OK"
            else:
                supplied = json.loads(body["messages"][1]["content"])
                source = supplied["untrusted_quoted_sources"][0]
                content = json.dumps({"schema_version": 1, "perspective": supplied["perspective_instruction"],
                    "claims": [{"text": "The selected public text establishes this attributed evidence.",
                        "citations": [{key: source[key] for key in ("source_id", "first_line", "last_line", "span_sha256")}]}],
                    "uncertainty": ["Mechanical citation validation does not establish semantic truth."],
                    "contradictions": [], "no_learning": True})
            payload = {"id": "intercepted-"+str(len(self.calls)),
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"cost": "0.000002", "prompt_tokens": 10, "completion_tokens": 10}}
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
async def test_authenticated_parent_two_children_real_public_source_and_dossier(accounting_db, real_auth, monkeypatch):
    from src.api import auth, work_board, model_fabric_settings
    from src.model_fabric.configuration import write_model_fabric_configuration
    root, engine, factory = accounting_db
    configured = setup_configuration()
    write_model_fabric_configuration(replace(model_fabric_settings._setup_configuration(replace(configured.openrouter_setup, timeout_seconds=30),
        profiles=(), policies=()), egress_revision=configured.egress_revision+1))
    jobs = DurableJobRepository()
    await jobs.configure_inference_accounting(1000)
    calls = []
    original_client = httpx.AsyncClient
    def clients(**kwargs):
        if "transport" not in kwargs:
            kwargs["transport"] = ProviderBoundary(calls)
        return original_client(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", clients)
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(work_board.router, prefix="/api")
    app.include_router(model_fabric_settings.router, prefix="/api")
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
        dispatcher = WorkBoardDispatcher(jobs=jobs, session_provider=factory.accounting_sessions)
        receipt = await dispatcher.run_pass()
        detail = await client.get("/api/work-board/tasks/"+task_id)
        (root/"research-vertical-private-readback.json").write_text(json.dumps({"label": "real public source; provider HTTP interception only",
            "task_id": task_id, "receipt": receipt, "detail": detail.json()}, indent=2))
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
