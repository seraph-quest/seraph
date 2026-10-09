"""Disposable actual Board children; scripted final inference transport only."""
from tests.general_task_method_lifecycle import native_admission_lifecycle
from dataclasses import replace

import pytest
from sqlalchemy import select

from src.db.models import WorkBoardTask, WorkflowRunState
from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime
from tests.test_general_task_specialist_planning import specialist_fixture


@pytest.mark.asyncio
async def test_native_delegation_declared_execution_grant_is_required(task_runtime, monkeypatch, native_admission_lifecycle):
    sessions, dispatcher, planner, transport, owner, group, accounting, inputs, descriptors, provenance = await specialist_fixture(task_runtime, monkeypatch)
    from src.auth.service import authenticate_session
    from src.workflows.specialist_delegation import current_delegation
    operator = await authenticate_session(owner.session_id, touch=False)
    invocation_id = accounting["delegation_invocation_id"]
    principal = replace(operator.principal, job_id=invocation_id, grants=frozenset())
    async with sessions() as db:
        context = await current_delegation(db, invocation_id)
        descriptor = next(item for item in context.envelope.descriptors if item.tool_id == "delegate_task")
        arguments = context.request.model_dump(mode="json", exclude={"parent_task_id", "step_id"})
    with pytest.raises(PermissionError, match="capability execution permission"):
        dispatcher.general_tasks.registry.begin_invocation(descriptor, arguments,
            principal=principal, job_id=invocation_id, fencing_token=accounting["parent_fence"])
    assert transport["contacts"] == []


@pytest.mark.asyncio
async def test_actual_specialist_board_child_output_is_read_back(task_runtime, monkeypatch, native_admission_lifecycle):
    monkeypatch.setattr("src.workflows.job_runtime.get_session", task_runtime[0])
    sessions, dispatcher, planner, transport, owner, group, accounting, inputs, descriptors, provenance = await specialist_fixture(task_runtime, monkeypatch)
    service = dispatcher.general_tasks
    service.planner = planner
    service.delegation_jobs = dispatcher.jobs
    (task_runtime[1] / "notes.txt").write_text("literal specialist artifact source", encoding="utf-8")
    from src.auth.service import authenticate_session
    operator = await authenticate_session(owner.session_id, touch=False)
    invocation_id = accounting["delegation_invocation_id"]
    principal = replace(operator.principal, session_id=owner.session_id,
        operator_session_id=owner.session_id, job_id=invocation_id)
    from src.workflows.specialist_delegation import execute_specialist
    try:
        result = await execute_specialist(dispatcher.jobs, service=service,
            invocation_id=invocation_id, fencing_token=accounting["parent_fence"], principal=principal)
    except BaseException:
        import traceback
        traceback.print_exc()
        async with sessions() as db:
            tasks = list((await db.execute(select(WorkBoardTask))).scalars())
            runs = list((await db.execute(select(WorkflowRunState))).scalars())
            print("Literal task states:", [(item.task_id, item.status.value, item.block_reason) for item in tasks], flush=True)
            print("Literal run states:", [(item.run_identity, item.status, item.failure_reason, item.branch_depth) for item in runs], flush=True)
        raise
    assert result["unresolved"] == []
    assert result["summary_ref"] in result["artifact_refs"]
    assert len(transport["contacts"]) == 1

    async with sessions() as db:
        child = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == result["child_id"]))
        assert child.status.value == "done"
        runs = list((await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.parent_job_id == invocation_id))).scalars())
        assert len(runs) == 1
        assert runs[0].status == "succeeded"
        assert runs[0].branch_depth == 2
        assert runs[0].root_run_identity != runs[0].run_identity
    recovered = await execute_specialist(dispatcher.jobs, service=service,
        invocation_id=invocation_id, fencing_token=accounting["parent_fence"], principal=principal)
    assert recovered == result
    assert len(transport["contacts"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("second_failure", [None, "proposal", "tool"])
async def test_actual_parent_adopts_two_original_specialist_children(task_runtime, monkeypatch, second_failure, native_admission_lifecycle):
    from config.settings import settings
    from src.native_tools.registry import ToolRegistry
    from src.work_board.general_task import GeneralTaskService, digest
    from src.work_board.contracts import GeneralTaskCreate, GeneralTaskInput, PlanSpec, TaskLimits, WorkBoardOwner
    from tests.test_general_task_native_guard import running_task
    from tests.general_task_test_transport import prepare_literal_planner
    from src.auth.service import authenticate_session
    monkeypatch.setattr("src.workflows.job_runtime.get_session", task_runtime[0])
    monkeypatch.setattr(settings, "use_delegation", True)
    registry = ToolRegistry()
    registry.start()
    bootstrap = GeneralTaskService(registry)
    bootstrap.start()
    descriptors = registry.descriptors()
    by_id = {item.tool_id: item for item in descriptors}
    delegate = by_id["delegate_task"]
    literal = {"role": "files", "instruction": "Read explicit local notes",
        "evidence_refs": [], "allowed_tool_ids": ["read_file"],
        "limits": {"max_steps": 1, "max_inference_calls": 1,
            "max_cost_microusd": 100, "wall_seconds": 300}}
    creation = GeneralTaskCreate(goal_revision=1, idempotency_key="two-real-specialists",
        expected_plan_revision=1, input=GeneralTaskInput(goal_ref="goal-1", intent="Delegate two bounded file tasks",
            requested_output=delegate.output_schema,
            tool_set_digest=digest([item.model_dump(mode="json") for item in sorted(descriptors,key=lambda item:item.tool_id)]),
            limits=TaskLimits(max_steps=5,max_inference_calls=3,max_cost_microusd=1000,wall_seconds=600),
            inference_egress_acknowledged=True),
        plan=PlanSpec(revision=1, steps=[{"step_id": name,"tool_id":"delegate_task",
            "input": literal,"output_contract":delegate.output_schema} for name in ("first","second")]))
    bootstrap.stop()
    registry.stop()
    registry.start()
    sessions, dispatcher, service, envelope, original = await running_task(task_runtime,
        creation_request=creation, registry_override=registry)
    (task_runtime[1]/"notes.txt").write_text("literal shared child source",encoding="utf-8")
    owner = WorkBoardOwner(principal_id=envelope.proposal_group.owner_principal_id,
        session_id=envelope.proposal_group.owner_session_id)
    child_plan = PlanSpec(revision=1,steps=[{"step_id":"read","tool_id":"read_file",
        "input":{"file_path":"notes.txt"},"output_contract":by_id["read_file"].output_schema}])
    planner, transport = await prepare_literal_planner(sessions,task_runtime[1],monkeypatch,owner,child_plan)
    import json
    remaining_parent = creation.plan.model_copy(update={"revision":2,"steps":[creation.plan.steps[1]]})
    class ScriptedContacts(list):
        def append(self, body):
            super().append(body)
            continuation = any("current_plan_revision" in message["content"] for message in body["messages"])
            transport["content"] = json.dumps((remaining_parent if continuation else child_plan).model_dump(mode="json"))
            if second_failure == "proposal" and len(self) == 3:
                transport["content"] = "invalid bounded specialist proposal"
            if second_failure == "tool" and len(self) == 3:
                failed = child_plan.model_copy(update={"steps":[child_plan.steps[0].model_copy(
                    update={"input":{"file_path":"absent-owned-source.txt"}})]})
                transport["content"] = json.dumps(failed.model_dump(mode="json"))
    transport["contacts"] = ScriptedContacts()
    service.planner = planner
    operator = await authenticate_session(owner.session_id,touch=False)
    result = await service.execute(dispatcher.jobs,job_id=original["job"]["job_id"],
        owner=original["job"]["lease"]["owner"],fence=original["job"]["lease"]["fencing_token"],
        envelope=envelope,principal=operator.principal)
    assert result["verified"] is False
    for _ in range(5):
        await dispatcher.reconcile_linked_attempts()
    async with sessions() as db:
        current_parent=await dispatcher.jobs._fetch(db,original["job"]["job_id"])
    assert (current_parent.status=="succeeded") is (second_failure is None)
    assert len(transport["contacts"]) == 3  # Two children and actual parent continuation.
    async with sessions() as db:
        children = list((await db.execute(select(WorkBoardTask).where(
            WorkBoardTask.idempotency_key.like("specialist:%")))).scalars())
        assert len(children) == (1 if second_failure == "proposal" else 2)
        assert sum(child.status.value == "done" for child in children) == (2 if second_failure is None else 1)
        roots = list((await db.execute(select(WorkflowRunState).where(WorkflowRunState.branch_depth == 2))).scalars())
        assert len(roots) == (1 if second_failure == "proposal" else 2)
        assert sum(root.status == "succeeded" for root in roots) == (2 if second_failure is None else 1)
        if second_failure:
            assert current_parent.status in {"paused","blocked","unknown_external_effect"}
            from src.work_board.input_artifacts import _safe_file_bytes
            import json
            succeeded = next(root for root in roots if root.status == "succeeded")
            for artifact in json.loads(succeeded.artifact_receipts_json):
                _safe_file_bytes(task_runtime[1]/artifact["file_path"],
                    expected_digest=artifact["content_sha256"],expected_size=artifact["size_bytes"])
