"""Ordinary intent/card/edit/accept HTTP journey on private SQLite artifacts."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

from src.auth.service import AuthenticatedOperator
from src.db.models import WorkBoardTask, WorkflowRunState
from src.security.trust_contract import TrustPrincipal, PrincipalType, AuthorityGrant
from src.work_board.contracts import GeneralTaskInput, WorkBoardOwner
from src.work_board.general_task import GeneralTaskService
from src.work_board.repository import BoardError
from tests.test_general_task_contract import Registry, descriptor, request, no_provider_contacts
from tests.test_general_task_persistence import task_runtime
from tests.general_task_method_lifecycle import native_admission_lifecycle
from tests.test_work_board_m6_provider_free_journey import isolated_runtime, OWNER, SESSION, _goal


@pytest_asyncio.fixture
async def api(task_runtime, monkeypatch):
    sessions, workspace = task_runtime
    from src.api import work_board as module
    registry = Registry()
    registry.blocked_tools = lambda: []
    from tests.general_task_test_transport import prepare_literal_planner
    planner, transport = await prepare_literal_planner(sessions, workspace, monkeypatch,
        WorkBoardOwner(principal_id=OWNER, session_id=SESSION), request(registry).plan)
    service = GeneralTaskService(registry, planner=planner)
    service.start()
    from src.work_board.dispatcher import WorkBoardDispatcher
    dispatcher = WorkBoardDispatcher(session_provider=sessions, general_tasks=service)
    monkeypatch.setattr(module, "dispatcher", dispatcher)
    monkeypatch.setattr(module, "get_session", sessions)
    principal = TrustPrincipal(principal_id=OWNER, principal_type=PrincipalType.OPERATOR,
        session_id=SESSION, operator_session_id=SESSION,
        grants=(AuthorityGrant.CAPABILITY_EXECUTE, AuthorityGrant.MODEL_INFERENCE))
    now = datetime.now(timezone.utc)
    operator = AuthenticatedOperator(session_id=SESSION, principal=principal,
        idle_expires_at=now+timedelta(hours=1), absolute_expires_at=now+timedelta(hours=2))
    app = FastAPI()
    @app.middleware("http")
    async def authenticate(req, call_next):
        req.state.operator = operator
        return await call_next(req)
    app.include_router(module.router, prefix="/api")
    return app, service, transport, dispatcher, sessions


def intent_request():
    return {"goal_revision": 1, "idempotency_key": "ordinary-intent",
        "input": {"goal_ref": "goal-1", "intent": "Read and return text",
            "requested_output": descriptor().output_schema,
            "inference_egress_acknowledged": True,
            "limits": {"max_cost_microusd": 1000}}}


@pytest.mark.asyncio
async def test_http_intent_proposal_replay_exact_plan_acceptance_and_work_readback(api, native_admission_lifecycle):
    app, service, planner, dispatcher, sessions = api
    async with sessions() as db:
        db.add(_goal("goal-1", "Ordinary intent"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        created = await client.post("/api/work-board/general-tasks", json=intent_request())
        assert created.status_code == 200, created.text
        task = created.json()["task"]
        assert task["status"] == "triage"
        task_id = task["task_id"]
        replay = await client.post("/api/work-board/general-tasks", json=intent_request())
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"]
        assert len(planner["contacts"]) == 1
        plan = await client.get(f"/api/work-board/tasks/{task_id}/plan")
        assert plan.status_code == 200, plan.text
        assert plan.json()["plan"]["revision"] == 1
        assert plan.json()["task_input"]["limits"]["max_cost_microusd"] == 1000
        rejected = await client.post(f"/api/work-board/tasks/{task_id}/actions",
            json={"action": "promote", "expected_revision": task["task_revision"] + 1})
        assert rejected.status_code == 409
        accepted = await client.post(f"/api/work-board/tasks/{task_id}/actions",
            json={"action": "promote", "expected_revision": task["task_revision"]})
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["task"]["status"] == "todo"
    result = await dispatcher.run_pass()
    assert result["completed"] == 1, result
    async with sessions() as db:
        assert len((await db.execute(select(WorkBoardTask))).scalars().all()) == 1
        roots = (await db.execute(select(WorkflowRunState))).scalars().all()
        assert len([row for row in roots if row.job_kind == "agent.task.v1"]) == 1
        assert len([row for row in roots if row.job_kind == "model_inference_ephemeral_v1"]) == 1


@pytest.mark.asyncio
async def test_invalid_model_plan_is_editable_triage_and_cannot_be_accepted(api):
    app, service, planner, dispatcher, sessions = api
    planner["content"] = "not JSON"
    async with sessions() as db:
        db.add(_goal("goal-1", "Invalid proposal remains editable"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        created = await client.post("/api/work-board/general-tasks", json=intent_request())
        assert created.status_code == 200, created.text
        task = created.json()["task"]
        task_id = task["task_id"]
        proposal = await client.get(f"/api/work-board/tasks/{task_id}/plan")
        assert proposal.status_code == 200, proposal.text
        assert proposal.json()["plan"] is None
        assert proposal.json()["proposal_error"] == "general_task_plan_invalid"
        denied = await client.post(f"/api/work-board/tasks/{task_id}/actions",
            json={"action": "promote", "expected_revision": task["task_revision"]})
        assert denied.status_code == 409, denied.text
        saved = await client.post(f"/api/work-board/tasks/{task_id}/plan", json={
            "expected_revision": task["task_revision"], "expected_plan_revision": 0,
            "idempotency_key": "repair-plan", "plan": request().plan.model_dump(mode="json")})
        assert saved.status_code == 200, saved.text
        repaired = await client.get(f"/api/work-board/tasks/{task_id}/plan")
        assert repaired.json()["plan"]["revision"] == 1
        assert repaired.json()["proposal_error"] is None
    async with sessions() as db:
        assert len((await db.execute(select(WorkBoardTask))).scalars().all()) == 1
        roots = (await db.execute(select(WorkflowRunState))).scalars().all()
        assert not [row for row in roots if row.job_kind == "agent.task.v1"]
        assert len([row for row in roots if row.job_kind == "model_inference_ephemeral_v1"]) == 1
