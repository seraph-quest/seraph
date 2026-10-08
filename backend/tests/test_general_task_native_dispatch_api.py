"""Actual authenticated native child approval and dispatcher completion."""
import json
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
@pytest.mark.parametrize("revise_pending", [False, True])
async def test_operator_api_safe_pause_and_resume_use_same_original_attempt(task_runtime, monkeypatch, revise_pending):
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
        if revise_pending:
            revision_errors = []
            actual_revise = dispatcher.jobs.revise_general_task_operator_paused_parent
            async def observed_revise(*args, **kwargs):
                try:
                    return await actual_revise(*args, **kwargs)
                except Exception as exc:
                    revision_errors.append(exc)
                    raise
            monkeypatch.setattr(dispatcher.jobs, "revise_general_task_operator_paused_parent", observed_revise)
            revise_endpoint = f"/api/work-board/tasks/{task_id}/plan/revise"
            revision_body = {"expected_revision": card["task_revision"], "replacements": [first.model_dump(mode="json"),
                second.model_copy(update={"input": {"text": "corrected remaining input"}}).model_dump(mode="json")],
                "reason": "Correct the remaining input", "idempotency_key": "paused-correction"}
            forged = await client.post(revise_endpoint, json={**revision_body, "runtime_owner": owner})
            assert forged.status_code == 422 and await dispatcher.jobs.get_job(parent_id) == held
            frozen = await client.post(revise_endpoint, json={**revision_body,
                "replacements": [first.model_copy(update={"input": {"text": "rewrite completed input"}}).model_dump(mode="json"), second.model_dump(mode="json")]})
            assert frozen.status_code == 409 and await dispatcher.jobs.get_job(parent_id) == held
            revised = await client.post(revise_endpoint, json=revision_body)
            assert revised.status_code == 200, revised.text + "\n" + "\n".join(str(exc) for exc in revision_errors)
            card = revised.json()["task"]
            after_edit = await dispatcher.jobs.get_job(parent_id)
            assert after_edit["status"] == "paused" and after_edit["attempt_count"] == 1
            assert {key: value for key, value in after_edit["lease"].items() if key != "revision"} == {
                key: value for key, value in held["lease"].items() if key != "revision"}
            assert after_edit["revision"] == held["revision"] + 1
            assert after_edit["deadline_at"] == held["deadline_at"]
            assert len(registry.calls) == 1
            held = after_edit
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
        if revise_pending:
            assert registry.calls[-1][1] == {"text": "corrected remaining input"}
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


@pytest.mark.asyncio
@pytest.mark.parametrize("full_key_capacity,foreign_root_seal", [(False, False), (True, False), (False, True)])
async def test_native_cancel_api_fences_held_original_file_callback(accounting_db, monkeypatch, full_key_capacity, foreign_root_seal):
    import asyncio
    import threading
    import httpx
    from fastapi import FastAPI
    from sqlalchemy import select
    from src.auth.service import authenticate_session
    from src.api import work_board as api
    from src.db.models import WorkBoardAttempt, WorkBoardTask, WorkflowRunState
    from src.native_tools.registry import ToolRegistry
    from src.tools.filesystem_tool import read_file
    from config.settings import settings
    from src.work_board.general_task import GeneralTaskService
    from src.work_board.dispatcher import WorkBoardDispatcher
    from tests.test_general_task_planner import prepare
    from tests.test_work_board_m6_provider_free_journey import _goal

    jobs, owner = await prepare(accounting_db, monkeypatch)
    workspace, _engine, factory = accounting_db
    sessions = factory.accounting_sessions
    (workspace / "held-source.txt").write_text("Only the original callback may close")
    entered, release = threading.Event(), threading.Event()
    calls = []
    actual_read = read_file.forward
    def held_read(file_path):
        calls.append(file_path)
        actual_result = actual_read(file_path)
        entered.set()
        if not release.wait(15):
            raise RuntimeError("Owned callback release deadline exceeded")
        return actual_result
    monkeypatch.setattr(read_file, "forward", held_read)
    registry = ToolRegistry(); registry.start()
    service = GeneralTaskService(registry); service.start()
    if full_key_capacity:
        actual_execute = service.execute
        async def fill_original_keys(jobs, **kwargs):
            from src.work_board.general_task_native import initialize_interpreter
            await initialize_interpreter(jobs, kwargs["job_id"], owner=kwargs["owner"], fence=kwargs["fence"])
            for index in range(45):
                await jobs.record_checkpoint(kwargs["job_id"], owner=kwargs["owner"], fencing_token=kwargs["fence"],
                    checkpoint_id=f"general:owned-existing:{index}", state="owned_existing",
                    checkpoint_payload={"no_learning": True})
            return await actual_execute(jobs, **kwargs)
        monkeypatch.setattr(service, "execute", fill_original_keys)
    dispatcher = WorkBoardDispatcher(session_provider=sessions, general_tasks=service)
    monkeypatch.setattr(api, "dispatcher", dispatcher)
    goal = _goal("goal-held-cancel", "Fence the original held file callback")
    goal.owner_principal_id, goal.owner_session_id = owner.principal_id, owner.session_id
    async with sessions() as db:
        db.add(goal)
    descriptor = next(item for item in registry.descriptors() if item.tool_id == "read_file")
    _descriptors, tool_digest = service.snapshot()
    app = FastAPI()
    @app.middleware("http")
    async def operator(req, call_next):
        req.state.operator = await authenticate_session(owner.session_id, touch=False)
        return await call_next(req)
    app.include_router(api.router, prefix="/api")
    worker = None
    original_workspace = settings.workspace_dir
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            created = await client.post("/api/work-board/general-tasks", json={"goal_revision": 1,
                "idempotency_key": "held-read-cancel", "accept": True, "expected_plan_revision": 1,
                "input": {"goal_ref": goal.id, "intent": "Read held-source.txt",
                    "requested_output": descriptor.output_schema, "tool_set_digest": tool_digest}, "plan": {"revision": 1, "steps": [
                        {"step_id": "held", "tool_id": "read_file", "input": {"file_path": "held-source.txt"},
                            "output_contract": descriptor.output_schema}]}})
            assert created.status_code == 200, created.text
            task_id = created.json()["task"]["task_id"]
            worker = asyncio.create_task(dispatcher.run_pass())
            started = await asyncio.to_thread(entered.wait, 10)
            if not started:
                diagnostic = (await client.get(f"/api/work-board/tasks/{task_id}/plan")).json()
                pytest.fail(str((worker.result() if worker.done() else "original worker remains pending",
                    diagnostic.get("native_execution"), diagnostic.get("proposal_error"))))
            waiting_response = await client.get(f"/api/work-board/tasks/{task_id}/plan")
            assert waiting_response.status_code == 200, waiting_response.text
            waiting = waiting_response.json()
            assert waiting["native_execution"]["phase"] == "native_wait"
            async with sessions() as db:
                attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task_id))
                child_row = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.parent_job_id == attempt.workflow_run_id))
            parent_id, child_id = attempt.workflow_run_id, child_row.run_identity
            original_parent, original_child = await jobs.get_job(parent_id), await jobs.get_job(child_id)
            if full_key_capacity:
                assert len(original_parent["checkpoints"]) == 50
            assert original_child["status"] == "running" and original_child["attempt_count"] == 1
            endpoint = f"/api/work-board/tasks/{task_id}/actions"
            pause = await client.post(endpoint, json={"action": "pause", "expected_revision": waiting["task_revision"]})
            assert pause.status_code == 409 and not release.is_set()
            assert await jobs.get_job(parent_id) == original_parent
            cancelled = await client.post(endpoint, json={"action": "cancel", "expected_revision": waiting["task_revision"]})
            assert cancelled.status_code == 200, cancelled.text
            card = cancelled.json()["task"]
            assert card["status"] == "blocked" and card["block_kind"] == "unknown_effect"
            assert card["latest_attempt"]["attempt_id"] == attempt.attempt_id
            fenced_child = await jobs.get_job(child_id)
            assert fenced_child["lease"]["fencing_token"] > original_child["lease"]["fencing_token"]
            assert fenced_child["attempt_count"] == 1 and fenced_child["deadline_at"] == original_child["deadline_at"]
            pending = (await client.get(f"/api/work-board/tasks/{task_id}/plan")).json()
            assert pending["native_execution"]["cancellation"]["state"] == "pending"
            assert pending["native_execution"]["phase"] == "unknown_recovery"
            assert not release.is_set() and calls == ["held-source.txt"]
            replay_parent, replay_child = await jobs.get_job(parent_id), await jobs.get_job(child_id)
            replay = await client.post(endpoint, json={"action": "cancel", "expected_revision": card["task_revision"]})
            assert replay.status_code == 200, replay.text
            assert replay.json()["event_id"] == cancelled.json()["event_id"]
            assert replay.json()["revision"] == cancelled.json()["revision"]
            assert await jobs.get_job(parent_id) == replay_parent and await jobs.get_job(child_id) == replay_child
            denied = await client.post(endpoint, json={"action": "resume", "expected_revision": card["task_revision"]})
            assert denied.status_code == 409
            moved_workspace = workspace / "moved-current-root"
            moved_workspace.mkdir()
            monkeypatch.setattr(settings, "workspace_dir", str(moved_workspace))
            if foreign_root_seal:
                from src.workflows.job_runtime import _digest
                async with sessions() as db:
                    child_row = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == child_id))
                    authority = json.loads(child_row.declared_authority_json)
                    authority["general_task_child_binding"]["live_root_digest"] = "0" * 64
                    child_row.declared_authority_json = json.dumps(authority)
                    child_row.authority_digest = _digest(authority)
                denied_parent = await jobs.get_job(parent_id)
            release.set()
            await asyncio.wait_for(worker, timeout=10)
            from src.workflows.general_task_guard import read_general_task_native_cancel
            if foreign_root_seal:
                assert await jobs.get_job(parent_id) == denied_parent
            else:
                async with sessions() as db:
                    parent_row = await jobs._fetch(db, parent_id)
                    task_row = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
                    attempt_row = await db.get(WorkBoardAttempt, attempt.attempt_id)
                    assert read_general_task_native_cancel(parent_row, task_row, attempt_row)["state"] == "callback_closed_outcome_debt"
            monkeypatch.setattr(settings, "workspace_dir", original_workspace)
            closed_response = await client.get(f"/api/work-board/tasks/{task_id}/plan")
            assert closed_response.status_code == 200, closed_response.text
            closed = closed_response.json()
            assert closed["native_execution"]["cancellation"]["state"] == ("pending" if foreign_root_seal else "callback_closed_outcome_debt")
            assert closed["native_execution"]["phase"] == "unknown_recovery"
            assert closed["native_execution"]["partial_output_refs"] == []
            assert (await jobs.get_job(parent_id))["deadline_at"] == original_parent["deadline_at"]
            if full_key_capacity:
                assert len((await jobs.get_job(parent_id))["checkpoints"]) == 50
            async with sessions() as db:
                rows = list((await db.execute(select(WorkflowRunState).where(WorkflowRunState.parent_job_id == parent_id))).scalars())
                assert len(rows) == 1
                assert not any(item["checkpoint_id"].startswith(("general:artifact:", "general:verified:"))
                    for row in rows for item in json.loads(row.checkpoint_receipts_json))
            restarted = WorkBoardDispatcher(session_provider=sessions, general_tasks=service)
            await restarted.reconcile_linked_attempts()
            assert calls == ["held-source.txt"]
            if not full_key_capacity and not foreign_root_seal:
                from src.workflows.general_task_guard import cancel_checkpoint_id
                protected_id = cancel_checkpoint_id(parent_id, attempt.attempt_id)
                async def forbidden_generic(*args, **kwargs):
                    pytest.fail("Native cancellation must never invoke generic cleanup or projection")
                monkeypatch.setattr(restarted, "_cleanup_adapter", forbidden_generic)
                monkeypatch.setattr(restarted, "_project_blocked", forbidden_generic)
                monkeypatch.setattr(restarted.jobs, "cancel_job_tree", forbidden_generic)
                async with sessions() as db:
                    parent_row = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == parent_id))
                    original_receipts = json.loads(parent_row.checkpoint_receipts_json)
                for corruption in ("missing", "malformed"):
                    damaged = [item for item in original_receipts if item["checkpoint_id"] != protected_id]
                    if corruption == "malformed":
                        damaged.append({"checkpoint_id": protected_id, "state": "cancelled", "payload": {"callback_closed": True}})
                    async with sessions() as db:
                        parent_row = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == parent_id))
                        parent_row.checkpoint_receipts_json = json.dumps(damaged)
                    before_reconcile = await jobs.get_job(parent_id)
                    assert parent_id in await restarted.reconcile_linked_attempts()
                    assert await jobs.get_job(parent_id) == before_reconcile
                    unavailable = (await client.get(f"/api/work-board/tasks/{task_id}/plan")).json()
                    assert unavailable["native_execution"]["phase"] == "unknown_recovery"
                    assert unavailable["native_execution"]["cancellation"]["state"] == "pending"
                    assert unavailable["native_execution"]["cancellation"]["callback_closed"] is False
                    assert calls == ["held-source.txt"]
    finally:
        monkeypatch.setattr(settings, "workspace_dir", original_workspace)
        release.set()
        if worker is not None and not worker.done():
            await asyncio.wait_for(worker, timeout=10)
        service.stop(); registry.stop()
