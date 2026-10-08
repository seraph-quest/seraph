"""Actual authenticated native child approval and dispatcher completion."""
import pytest

from tests.test_general_task_approval import approval_journey, create_and_run, get_plan
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime


def native_resume_body(plan):
    pause = plan["approval_pause"]
    return {"expected_revision": plan["task_revision"], "expected_plan_revision": plan["plan"]["revision"],
        **{key: pause[key] for key in ("workflow_run_id", "attempt_id", "fencing_token", "workflow_revision",
            "approval_id", "child_job_id", "expected_manifest_revision")}}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, "missing_native", "wrong_child", "stale_manifest"])
async def test_native_approval_api_keeps_original_child_and_finishes_with_readback(approval_journey, change):
    journey = approval_journey
    task_id, result = await create_and_run(journey)
    assert result["blocked"] == 1 and journey.tool.calls == 0
    plan = await get_plan(journey, task_id)
    pause = plan["approval_pause"]
    assert plan["native_execution"]["phase"] == "approval_wait"
    assert pause and not pause["can_resume"] and pause["approval_status"] == "pending"
    original_parent = await journey.jobs.get_job(pause["workflow_run_id"])
    original_child = await journey.jobs.get_job(pause["child_job_id"])
    assert original_child["attempt_count"] == 1 and original_child["status"] == "paused"
    approval = await journey.client.post(f"/api/approvals/{pause['approval_id']}/approve")
    assert approval.status_code == 200, approval.text
    plan = await get_plan(journey, task_id)
    assert plan["approval_pause"]["can_resume"] is True, plan
    body = native_resume_body(plan)
    endpoint = f"/api/work-board/tasks/{task_id}/plan/resume"
    if change:
        wrong = dict(body)
        if change == "missing_native":
            wrong.pop("child_job_id"); wrong.pop("expected_manifest_revision")
        elif change == "wrong_child":
            wrong["child_job_id"] = "unrelated-native-child"
        else:
            wrong["expected_manifest_revision"] += 1
        denied = await journey.client.post(endpoint, json=wrong)
        assert denied.status_code == 409, denied.text
        assert journey.tool.calls == 0
        assert await journey.jobs.get_job(pause["child_job_id"]) == original_child
    resumed = await journey.client.post(endpoint, json=body)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["task"]["status"] == "review", resumed.text
    assert resumed.json()["task"]["latest_attempt"]["attempt_id"] == pause["attempt_id"]
    assert journey.tool.calls == 1
    child = await journey.jobs.get_job(pause["child_job_id"])
    parent = await journey.jobs.get_job(pause["workflow_run_id"])
    assert child["status"] == parent["status"] == "succeeded"
    assert child["attempt_count"] == parent["attempt_count"] == 1
    assert child["deadline_at"] == original_child["deadline_at"]
    assert parent["deadline_at"] == original_parent["deadline_at"]
    final = await get_plan(journey, task_id)
    assert final["approval_pause"] is None
    assert final["native_execution"]["steps"][0]["status"] == "verified"
    assert final["native_execution"]["steps"][0]["contact_state"] == "settled"
    assert final["native_execution"]["no_learning"] is True
    assert (await journey.client.post(endpoint, json=body)).status_code == 409
    assert (await journey.dispatcher.run_pass())["completed"] == 0
    assert journey.tool.calls == 1


@pytest.mark.asyncio
async def test_operator_api_safe_pause_and_resume_use_same_original_attempt(task_runtime, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from src.auth.service import authenticate_session
    from src.api import work_board as api
    from src.work_board.contracts import PlanSpec
    from src.work_board.general_task_native import admit_native_step, run_native_step
    from tests.test_general_task_contract import Registry, request
    from tests.test_general_task_native_guard import running_task
    registry = Registry()
    creation = request(registry)
    first = creation.plan.steps[0].model_copy(update={"step_id": "first"})
    second = first.model_copy(update={"step_id": "second", "depends_on": ["first"],
        "input": {"text": {"$dependency": {"step_id": "first", "pointer": "/text"}}}})
    creation = creation.model_copy(update={"plan": PlanSpec(revision=1, steps=[first, second])})
    sessions, dispatcher, service, envelope, current = await running_task(task_runtime,
        creation_request=creation, registry_override=registry)
    monkeypatch.setattr(api, "dispatcher", dispatcher)
    monkeypatch.setattr(api, "get_session", sessions)
    parent_id = current["job"]["job_id"]
    owner, fence = current["job"]["lease"]["owner"], current["job"]["lease"]["fencing_token"]
    root = current["manifest"]["original_root_id"]
    principal = (await authenticate_session(root, touch=False)).principal
    binding, _ = await admit_native_step(dispatcher.jobs, parent_id, owner=owner, fence=fence,
        step=first, descriptor=registry.entries[0], inputs=first.input)
    await run_native_step(service, dispatcher.jobs, binding, child_owner="first-native", principal=principal)
    parent = await dispatcher.jobs.get_job(parent_id)
    from src.workflows.general_task_guard import read_manifest
    async with sessions() as db:
        manifest = read_manifest(await dispatcher.jobs._fetch(db, parent_id))
    resumed = await dispatcher.jobs.resume_general_task_native_parent(parent_id, owner=owner,
        expected_revision=parent["revision"], expected_manifest_revision=manifest.manifest_revision)
    task_id, revision = resumed["manifest"]["task_id"], resumed["manifest"]["task_revision"]
    app = FastAPI()
    @app.middleware("http")
    async def authenticated(req, call_next):
        req.state.operator = await authenticate_session(root, touch=False)
        return await call_next(req)
    app.include_router(api.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        endpoint = f"/api/work-board/tasks/{task_id}/actions"
        extra = await client.post(endpoint, json={"action": "pause", "expected_revision": revision, "attempt_id": binding.attempt_id})
        assert extra.status_code == 422 and len(registry.calls) == 1
        paused = await client.post(endpoint, json={"action": "pause", "expected_revision": revision})
        assert paused.status_code == 200, paused.text
        card = paused.json()["task"]
        assert card["status"] == "blocked" and card["block_reason"] == "general_task_operator_paused"
        assert card["latest_attempt"]["attempt_id"] == binding.attempt_id
        held = await dispatcher.jobs.get_job(parent_id)
        assert held["status"] == "paused" and held["attempt_count"] == 1
        assert held["deadline_at"] == current["job"]["deadline_at"]
        denied = await client.post(endpoint, json={"action": "resume", "expected_revision": revision})
        assert denied.status_code == 409 and len(registry.calls) == 1
        assert await dispatcher.jobs.get_job(parent_id) == held
        from sqlalchemy import update
        from src.db.models import WorkBoardTask
        async with sessions() as db:
            await db.execute(update(WorkBoardTask).where(WorkBoardTask.task_id == task_id).values(capability_id="goal.snapshot"))
        foreign = await client.post(endpoint, json={"action": "resume", "expected_revision": card["task_revision"]})
        assert foreign.status_code == 422 and len(registry.calls) == 1
        assert await dispatcher.jobs.get_job(parent_id) == held
        async with sessions() as db:
            await db.execute(update(WorkBoardTask).where(WorkBoardTask.task_id == task_id).values(capability_id="agent.task.v1"))
        completed = await client.post(endpoint, json={"action": "resume", "expected_revision": card["task_revision"]})
        assert completed.status_code == 200, completed.text
        assert completed.json()["task"]["status"] == "review"
        assert completed.json()["task"]["latest_attempt"]["attempt_id"] == binding.attempt_id
        assert len(registry.calls) == 2
        assert (await dispatcher.jobs.get_job(parent_id))["deadline_at"] == current["job"]["deadline_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("completed_first", [False, True])
async def test_dispatcher_restart_recovers_only_original_native_child(task_runtime, completed_first):
    from sqlalchemy import select
    from src.db.models import WorkflowRunState, WorkBoardTask
    from src.auth.service import authenticate_session
    from src.work_board.contracts import PlanSpec
    from src.work_board.general_task import GeneralTaskService
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.work_board.general_task_native import admit_native_step, run_native_step
    from tests.test_general_task_contract import Registry, request
    from tests.test_general_task_native_guard import running_task
    registry = Registry()
    creation = request(registry)
    first = creation.plan.steps[0].model_copy(update={"step_id": "first"})
    second = first.model_copy(update={"step_id": "second", "depends_on": ["first"],
        "input": {"text": {"$dependency": {"step_id": "first", "pointer": "/text"}}}})
    creation = creation.model_copy(update={"plan": PlanSpec(revision=1, steps=[first, second])})
    sessions, dispatcher, service, envelope, current = await running_task(task_runtime,
        creation_request=creation, registry_override=registry)
    parent_id = current["job"]["job_id"]
    binding, _ = await admit_native_step(dispatcher.jobs, parent_id,
        owner=current["job"]["lease"]["owner"], fence=current["job"]["lease"]["fencing_token"],
        step=first, descriptor=registry.entries[0], inputs=first.input)
    if completed_first:
        principal = (await authenticate_session(binding.original_root_id, touch=False)).principal
        await run_native_step(service, dispatcher.jobs, binding, child_owner="first-native", principal=principal)
    assert len(registry.calls) == int(completed_first)
    service.stop()
    restored = GeneralTaskService(registry); restored.start()
    restarted = WorkBoardDispatcher(session_provider=sessions, general_tasks=restored)
    assert parent_id in await restarted.reconcile_linked_attempts()
    parent = await dispatcher.jobs.get_job(parent_id)
    assert parent["status"] == "succeeded" and parent["attempt_count"] == 1
    assert parent["deadline_at"] == current["job"]["deadline_at"]
    original_child = await dispatcher.jobs.get_job(binding.invocation_id)
    assert original_child["status"] == "succeeded" and original_child["attempt_count"] == 1
    assert len(registry.calls) == 2
    async with sessions() as db:
        children = list((await db.execute(select(WorkflowRunState).where(WorkflowRunState.parent_job_id == parent_id))).scalars())
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
    assert len(children) == 2 and task.status.value == "review"
    await restarted.reconcile_linked_attempts()
    assert len(registry.calls) == 2
    restored.stop()
