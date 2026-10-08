"""Existing owned local MCP callback, exact HTTP approval and one native child."""
from dataclasses import replace

import httpx
from fastapi import FastAPI
import pytest

from src.auth.service import authenticate_session
from src.native_tools.registry import ToolRegistry
from src.work_board.contracts import GeneralTaskCreate, GeneralTaskInput, PlanSpec, WorkBoardOwner
from src.work_board.general_task import digest
from src.work_board.general_task_native import admit_native_step, run_native_step
from src.workflows.general_task_guard import read_manifest
from src.workflows.job_runtime import DurableJobLeaseError
from tests.test_general_task_adapters import mcp_registry
from tests.test_general_task_native_guard import running_task
from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("postcontact_failure", [False, True])
async def test_actual_mcp_approval_resumes_same_native_child_once(task_runtime, postcontact_failure):
    registry = ToolRegistry()
    registry.start()
    registry, manager, tool, _, _ = mcp_registry.__wrapped__(task_runtime[1], registry)
    descriptors = registry.descriptors()
    descriptor = next(item for item in descriptors if item.tool_id == "mcp:local:repo_read")
    creation = GeneralTaskCreate(goal_revision=1, idempotency_key="native-mcp-approved", expected_plan_revision=1,
        input=GeneralTaskInput(goal_ref="goal-1", intent="Read using the explicitly approved local MCP tool",
            requested_output=descriptor.output_schema,
            tool_set_digest=digest([item.model_dump(mode="json") for item in descriptors])),
        plan=PlanSpec(revision=1, steps=[{"step_id": "mcp-read", "tool_id": descriptor.tool_id,
            "input": {"query": "literal owned repository"}, "output_contract": descriptor.output_schema}]))
    try:
        sessions, dispatcher, service, envelope, current = await running_task(task_runtime,
            creation_request=creation, registry_override=registry)
        binding, _ = await admit_native_step(dispatcher.jobs, current["job"]["job_id"],
            owner=current["job"]["lease"]["owner"], fence=current["job"]["lease"]["fencing_token"],
            step=envelope.plan.steps[0], descriptor=descriptor, inputs=envelope.plan.steps[0].input)
        operator = await authenticate_session(binding.original_root_id, touch=False)
        waiting, artifact, reference = await run_native_step(service, dispatcher.jobs, binding,
            child_owner="general-task-native:" + binding.invocation_id, principal=operator.principal)
        assert waiting["awaiting_approval"] and artifact is reference is None
        assert tool.calls == 0
        original = await dispatcher.jobs.get_job(binding.invocation_id)
        assert original["status"] == "paused" and original["attempt_count"] == 1
        async with sessions() as db:
            parent = await dispatcher.jobs._fetch(db, binding.parent_job_id)
            manifest = read_manifest(parent)
            parent_revision = parent.revision
        assert manifest.phase == "approval_wait"
        owner = WorkBoardOwner(principal_id=binding.owner_principal_id, session_id=binding.original_root_id)
        arguments = dict(operator_owner=owner, expected_task_revision=manifest.task_revision,
            expected_parent_revision=parent_revision, expected_manifest_revision=manifest.manifest_revision,
            approval_id=waiting["approval_id"])
        with pytest.raises(DurableJobLeaseError):
            await dispatcher.jobs.resume_general_task_native_approval(binding.invocation_id, **arguments)
        assert (await dispatcher.jobs.get_job(binding.invocation_id))["revision"] == original["revision"]
        from src.api.approvals import router
        app = FastAPI()
        @app.middleware("http")
        async def authenticated(request, call_next):
            request.state.operator = await authenticate_session(binding.original_root_id, touch=False)
            return await call_next(request)
        app.include_router(router, prefix="/api")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            approved = await client.post(f"/api/approvals/{waiting['approval_id']}/approve")
            assert approved.status_code == 200, approved.text
        resumed = await dispatcher.jobs.resume_general_task_native_approval(binding.invocation_id, **arguments)
        assert resumed["child"]["attempt_count"] == 1
        assert resumed["child"]["lease"]["fencing_token"] > original["lease"]["fencing_token"]
        assert resumed["child"]["deadline_at"] == original["deadline_at"]
        if postcontact_failure:
            from src.approval.exceptions import ApprovalRequired
            tool.failure = ApprovalRequired(approval_id="foreign-after-contact",
                session_id=binding.original_root_id, tool_name=tool.name,
                risk_level="high", summary="Actual contacted local tool refuses completion")
            with pytest.raises(ApprovalRequired) as contacted:
                await run_native_step(service, dispatcher.jobs, binding,
                    child_owner=resumed["runtime_owner"], principal=operator.principal, approved_resume=True)
            assert type(contacted.value) is ApprovalRequired
            assert tool.calls == 1
            invocation = service._native_invocations[binding.invocation_id]
            assert invocation.closed and invocation.witness.outcome == "unknown"
            child = await dispatcher.jobs.get_job(binding.invocation_id)
            assert child["status"] == "running" and child["attempt_count"] == 1
            async with sessions() as db:
                manifest = read_manifest(await dispatcher.jobs._fetch(db, binding.parent_job_id))
            parent = await dispatcher.jobs.get_job(binding.parent_job_id)
            with pytest.raises(DurableJobLeaseError):
                await dispatcher.jobs.pause_general_task_native_parent(binding.parent_job_id,
                    operator_owner=owner, expected_task_revision=manifest.task_revision,
                    expected_revision=parent["revision"], expected_manifest_revision=manifest.manifest_revision)
            from src.work_board.general_task import GeneralTaskService
            restarted = GeneralTaskService(registry); restarted.start()
            try:
                with pytest.raises(DurableJobLeaseError):
                    await run_native_step(restarted, dispatcher.jobs, binding,
                        child_owner=resumed["runtime_owner"], principal=operator.principal, approved_resume=True)
                assert tool.calls == 1
            finally:
                restarted.stop()
            return
        output, artifact, reference = await run_native_step(service, dispatcher.jobs, binding,
            child_owner=resumed["runtime_owner"], principal=operator.principal, approved_resume=True)
        assert output == {"value": "local repository readback"} and tool.calls == 1
        assert artifact["content_sha256"] == reference.digest
        child = await dispatcher.jobs.get_job(binding.invocation_id)
        assert child["status"] == "succeeded" and child["attempt_count"] == 1
        with pytest.raises(DurableJobLeaseError):
            await dispatcher.jobs.resume_general_task_native_approval(binding.invocation_id, **arguments)
        assert tool.calls == 1
    finally:
        registry.stop()
