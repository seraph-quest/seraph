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
from tests.test_document_build_native_capacity import build_admission_lifecycle


@pytest.mark.asyncio
async def test_http_governed_intent_plan_accept_native_readback_and_restart(accounting_db, monkeypatch, build_admission_lifecycle):
    from src.auth.service import authenticate_session
    from src.api import work_board as module
    from src.db.models import WorkBoardTask, WorkflowRunState
    from src.native_tools.registry import ToolRegistry
    from src.work_board.general_task import GeneralTaskService
    from src.work_board.contracts import GENERAL_TASK_NATIVE_CHILD_KIND
    from src.work_board.general_task_planner import GeneralTaskPlanner
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker

    jobs, owner = await prepare(accounting_db, monkeypatch)
    await build_admission_lifecycle.start()
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
            children = [item for item in runs if item.job_kind == GENERAL_TASK_NATIVE_CHILD_KIND]
            assert len(runs) == 3 and execution.status == "succeeded"
            assert len(children) == 1 and children[0].status == "succeeded"
            assert children[0].parent_job_id == execution.run_identity and children[0].attempt_count == 1
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
        final_plan = await client.get(f"/api/work-board/tasks/{task_id}/plan")
        assert final_plan.status_code == 200, final_plan.text
        native = final_plan.json()["native_execution"]
        assert native["steps"][0]["status"] == "verified" and native["steps"][0]["contact_state"] == "settled"
        assert native["steps"][0]["invocation_id"] == children[0].run_identity
        assert len(native["partial_output_refs"]) == 1 and native["no_learning"] is True

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


@pytest.mark.asyncio
async def test_opted_in_general_task_actual_missing_read_creates_no_automatic_lesson(accounting_db, monkeypatch, build_admission_lifecycle):
    """Actual failed general-task contact remains no-learning under the optional hook."""
    from uuid import uuid4
    from src.auth import service as auth_service
    from src.auth.service import authenticate_token
    from src.db.models import MemoryProposal, WorkflowRunState, WorkflowStepState
    from src.memory import task_lessons
    from src.native_tools.registry import ToolRegistry
    from src.work_board.contracts import GeneralTaskCreate
    from src.work_board.general_task import GeneralTaskService
    from src.work_board.dispatcher import WorkBoardDispatcher

    issued_tokens = []
    actual_create_session = auth_service.create_session
    async def capture_actual_session(*args, **kwargs):
        issued = await actual_create_session(*args, **kwargs)
        issued_tokens.append(issued[0])
        return issued
    monkeypatch.setattr(auth_service, "create_session", capture_actual_session)
    jobs, owner = await prepare(accounting_db, monkeypatch)
    await build_admission_lifecycle.start()
    workspace, _engine, factory = accounting_db
    sessions = factory.accounting_sessions
    assert len(issued_tokens) == 1
    operator = await authenticate_token(issued_tokens[0], touch=False)
    goal = _goal("goal-no-learning", "Read an absent local source")
    goal.owner_principal_id, goal.owner_session_id = owner.principal_id, owner.session_id
    async with sessions() as db:
        db.add(goal)
    registry = ToolRegistry()
    registry.start()
    service = GeneralTaskService(registry)
    service.start()
    try:
        descriptors, tool_digest = service.snapshot()
        descriptor = next(item for item in descriptors if item.tool_id == "read_file")
        request = GeneralTaskCreate.model_validate({"goal_revision": 1,
            "idempotency_key": "actual-missing-source-no-learning", "accept": True, "expected_plan_revision": 1,
            "input": {"goal_ref": goal.id, "intent": "Read absent.txt",
                "requested_output": descriptor.output_schema, "tool_set_digest": tool_digest},
            "plan": {"revision": 1, "steps": [{"step_id": "read-absent", "tool_id": "read_file",
                "input": {"file_path": "absent.txt"}, "output_contract": descriptor.output_schema}]}})
        async with sessions() as db:
            task = (await service.create(db, owner, request)).task
        source = await task_lessons.eligible_lesson_source(operator, task.task_id)
        enabled = await task_lessons.set_automatic_lesson_policy(operator, task.task_id,
            task_lessons.LessonAutoPolicyRequest(enabled=True, expected_revision=task.task_revision,
                expected_policy_revision=source["automatic_policy"]["policy_revision"], mutation_uuid=str(uuid4())))
        assert enabled["enabled"] is True
        observed = []
        actual_hook = task_lessons.maybe_propose_automatic_lesson
        async def observe_actual_hook(current_task, attempt_id):
            result = await actual_hook(current_task, attempt_id)
            observed.append(result)
            return result
        monkeypatch.setattr(task_lessons, "maybe_propose_automatic_lesson", observe_actual_hook)
        dispatcher = WorkBoardDispatcher(session_provider=sessions, general_tasks=service)
        outcome = await dispatcher.run_pass()
        assert outcome["blocked"] == 1, outcome
        assert not (workspace / "absent.txt").exists()
        async with sessions() as db:
            run = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.job_kind == "agent.task.v1"))).scalar_one()
            assert (await db.execute(select(WorkflowStepState).where(WorkflowStepState.run_identity == run.run_identity))).scalars().all() == []
            assert (await db.execute(select(MemoryProposal))).scalars().all() == []
        projection = await jobs.get_job(run.run_identity)
        assert projection["status"] == "paused" and projection["failure_reason"] == "general_task_native_wait"
        async with sessions() as db:
            children = list((await db.execute(select(WorkflowRunState).where(
                WorkflowRunState.parent_job_id == run.run_identity))).scalars())
        assert len(children) == 1 and children[0].status == "unknown_external_effect"
        child = await jobs.get_job(children[0].run_identity)
        assert child["effects"] and all(item["details"]["no_learning"] is True for item in child["effects"])
        restarted = WorkBoardDispatcher(session_provider=sessions, general_tasks=service)
        for _ in range(2):
            await restarted.reconcile_linked_attempts()
            assert await jobs.get_job(children[0].run_identity) == child
            assert await jobs.get_job(run.run_identity) == projection
        async with sessions() as db:
            assert len(list((await db.execute(select(WorkflowRunState).where(
                WorkflowRunState.parent_job_id == run.run_identity))).scalars())) == 1
        # The native original attempt remains open for exact reconciliation;
        # a held child is not a terminal automatic-learning source.
        assert observed == []
        source = await task_lessons.eligible_lesson_source(operator, task.task_id)
        assert source["eligible"] is False
    finally:
        service.stop()
        registry.stop()
