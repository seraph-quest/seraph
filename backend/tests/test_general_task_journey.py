"""Actual Python-owner ordinary intent journey with intercepted model HTTP.

Literal PlanSpec output tests state mechanics only. Every provider socket is
denied; existing model metadata, auth, policy, broker and accounting stay real.
The filesystem tool executes physically and its private artifact is read back.
"""
import hashlib
import json

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from tests.test_general_task_planner import accounting_db, forbid_external_inference, prepare
from tests.test_work_board_m6_provider_free_journey import _goal


@pytest.mark.asyncio
async def test_http_governed_intent_plan_accept_native_readback_and_restart(accounting_db, monkeypatch):
    from src.auth.service import authenticate_session
    from src.api import work_board as module
    from src.db.models import WorkBoardTask, WorkflowRunState
    from src.native_tools.registry import ToolRegistry
    from src.work_board.general_task import GeneralTaskService
    from src.work_board.general_task_planner import GeneralTaskPlanner
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker

    jobs, owner = await prepare(accounting_db, monkeypatch)
    workspace, _engine, factory = accounting_db
    sessions = factory.accounting_sessions
    operator = await authenticate_session(owner.session_id, touch=False)
    source = "Physical local readback from the ordinary intent journey.\n"
    (workspace / "notes.txt").write_text(source)
    source_hash = hashlib.sha256(source.encode()).hexdigest()
    goal = _goal("goal-journey", "Read local notes through one governed inert plan")
    goal.owner_principal_id, goal.owner_session_id = owner.principal_id, owner.session_id
    async with sessions() as db:
        db.add(goal)
    registry = ToolRegistry()
    registry.start()
    read_descriptor = next(item for item in registry.descriptors() if item.tool_id == "read_file")
    service = GeneralTaskService(registry, planner=GeneralTaskPlanner())
    service.start()
    dispatcher = WorkBoardDispatcher(session_provider=sessions, general_tasks=service)
    monkeypatch.setattr(module, "dispatcher", dispatcher)

    app = FastAPI()
    @app.middleware("http")
    async def current_operator(request, call_next):
        # Genuine current server-authenticated session; the browser cannot
        # provide a principal, grant, lease or execution ownership field.
        request.state.operator = await authenticate_session(operator.session_id, touch=False)
        return await call_next(request)
    app.include_router(module.router, prefix="/api")
    contacts = []
    plan = {"schema_version": 1, "revision": 1, "steps": [{"step_id": "read-notes",
        "tool_id": "read_file", "input": {"file_path": "notes.txt"}, "depends_on": [],
        "output_contract": read_descriptor.output_schema}]}
    class Bytes(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield json.dumps({"id": "gen-literal-journey", "usage": {"cost": "0"},
                "choices": [{"message": {"role": "assistant", "content": json.dumps(plan)}}]}).encode()
    class InferenceBoundary(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
            assert request.method == "POST"
            body = json.loads(request.content)
            contacts.append(body)
            assert source not in json.dumps(body)
            public = json.loads(body["messages"][1]["content"])
            assert "read_file" in [item["tool_id"] for item in public["registered_tools"]]
            return httpx.Response(200, request=request, stream=Bytes())
    original_client = httpx.AsyncClient
    def intercepted_client(*args, **kwargs):
        if "transport" not in kwargs:
            kwargs["transport"] = InferenceBoundary()
        return original_client(*args, **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", intercepted_client)
    body = {"goal_revision": 1, "idempotency_key": "ordinary-governed-journey",
        "input": {"goal_ref": goal.id, "intent": "Read notes.txt and return its content and SHA-256",
            "requested_output": read_descriptor.output_schema, "inference_egress_acknowledged": True,
            "limits": {"max_cost_microusd": 1000}}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        created = await client.post("/api/work-board/general-tasks", json=body)
        assert created.status_code == 200, created.text
        card = created.json()["task"]
        assert card["status"] == "triage"
        task_id = card["task_id"]
        assert len(contacts) == 1
        replay = await client.post("/api/work-board/general-tasks", json=body)
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"]
        assert replay.json()["task"]["task_id"] == task_id
        assert len(contacts) == 1
        proposed = await client.get(f"/api/work-board/tasks/{task_id}/plan")
        assert proposed.status_code == 200, proposed.text
        assert proposed.json()["plan"] == plan
        assert proposed.json()["accepted"] is False
        assert proposed.json()["task_input"]["limits"]["max_cost_microusd"] == 1000
        async with sessions() as db:
            runs = list((await db.execute(select(WorkflowRunState))).scalars())
            assert len(runs) == 1 and runs[0].job_kind == "model_inference_ephemeral_v1"
            assert runs[0].status == "succeeded"
        denied = await client.post(f"/api/work-board/tasks/{task_id}/actions",
            json={"action": "promote", "expected_revision": card["task_revision"] + 1})
        assert denied.status_code == 409
        accepted = await client.post(f"/api/work-board/tasks/{task_id}/actions",
            json={"action": "promote", "expected_revision": card["task_revision"]})
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["task"]["status"] == "todo"
        completed = await dispatcher.run_pass()
        assert completed["completed"] == 1, completed
        readback = await client.get(f"/api/work-board/tasks/{task_id}")
        assert readback.status_code == 200, readback.text
        assert readback.json()["task"]["status"] == "review"
        async with sessions() as db:
            rows = list((await db.execute(select(WorkBoardTask))).scalars())
            assert len(rows) == 1
            runs = list((await db.execute(select(WorkflowRunState))).scalars())
            execution = next(item for item in runs if item.job_kind == "agent.task.v1")
            assert len(runs) == 2 and execution.status == "succeeded"
            assert execution.owner_principal_id == owner.principal_id
            execution_id = execution.run_identity
        projection = await jobs.get_job(execution_id)
        verified = next(item["payload"] for item in projection["checkpoints"]
            if item["checkpoint_id"] == "general:verified:read-notes")
        artifact = workspace / verified["file_path"]
        raw = artifact.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == verified["content_sha256"]
        artifact_data = json.loads(raw)
        assert artifact_data["output"] == {"content": source, "sha256": source_hash}
        assert artifact.stat().st_mode & 0o077 == 0
        assert (workspace / "notes.txt").read_text() == source

        # Restart current Python owners while retaining the canonical database,
        # accounting witness, exact descriptors and original task identity.
        service.stop()
        registry.stop()
        restored_registry = ToolRegistry()
        restored_registry.start()
        restored = GeneralTaskService(restored_registry, planner=GeneralTaskPlanner())
        restored.start()
        restarted = WorkBoardDispatcher(session_provider=sessions, general_tasks=restored)
        monkeypatch.setattr(module, "dispatcher", restarted)
        monkeypatch.setattr("src.model_fabric.remote_inference_admission.remote_inference_admission_broker",
            RemoteInferenceAdmissionBroker(durable_accounting=True))
        replay = await client.post("/api/work-board/general-tasks", json=body)
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"]
        assert replay.json()["task"]["task_id"] == task_id
        assert replay.json()["task"]["status"] == "review"
        assert (await restarted.run_pass())["completed"] == 0
        assert artifact.read_bytes() == raw
        assert len(contacts) == 1
        snapshot = await jobs.inference_accounting_snapshot()
        assert snapshot["operation_count"] == 1
        assert snapshot["committed_microusd"] == snapshot["unknown_microusd"] == 0
        operation = snapshot["operations"][0]
        assert operation["state"] == "settled" and operation["runtime_path"] == "general_task_planner"
        print(json.dumps({"flow": "ordinary_intent_plan_accept_native_readback_restart", "task_id": task_id,
            "execution_job_id": execution_id, "planning_job_id": operation["job_id"],
            "planning_operation_id": operation["operation_id"], "private_artifact": verified["file_path"],
            "artifact_sha256": verified["content_sha256"], "intercepted_http_calls": len(contacts),
            "external_provider_contacts": 0, "committed_microusd": 0, "memory_status": "no_learning"}, sort_keys=True))
        restored.stop()
        restored_registry.stop()
