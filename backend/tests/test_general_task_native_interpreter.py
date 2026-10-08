"""Canonical native execution/restart/revision mechanics, no inference."""
import pytest

from src.auth.service import authenticate_session
from src.work_board.contracts import PlanSpec, PlanRevisionRequest, WorkBoardOwner
from src.work_board.general_task import GeneralTaskService
from src.work_board.general_task_native import admit_native_step, run_native_step, publish_plan_revision
from src.workflows.general_task_guard import read_manifest
from tests.test_general_task_contract import Registry, request
from tests.test_general_task_native_guard import running_task
from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["execute", "restart", "crash_before_assembly", "crash_before_claim", "crash_queued", "revision", "pause"])
async def test_native_execute_preserves_completed_step_across_restart_and_revision(task_runtime, operation):
    registry = Registry()
    descriptor = registry.entries[0]
    creation = request(registry)
    first = creation.plan.steps[0].model_copy(update={"step_id": "first"})
    second = first.model_copy(update={"step_id": "second", "depends_on": ["first"],
        "input": {"text": {"$dependency": {"step_id": "first", "pointer": "/text"}}}})
    creation = creation.model_copy(update={"plan": PlanSpec(revision=1, steps=[first, second])})
    sessions, dispatcher, service, envelope, current = await running_task(task_runtime,
        creation_request=creation, registry_override=registry)
    parent_id = current["job"]["job_id"]
    owner, fence = current["job"]["lease"]["owner"], current["job"]["lease"]["fencing_token"]
    principal = (await authenticate_session(current["manifest"]["original_root_id"], touch=False)).principal
    original_deadline = current["job"]["deadline_at"]
    if operation != "execute":
        binding, _ = await admit_native_step(dispatcher.jobs, parent_id, owner=owner, fence=fence,
            step=first, descriptor=descriptor, inputs=first.input)
        if operation == "crash_queued":
            await dispatcher.jobs.queue_job(binding.invocation_id)
        if operation not in {"crash_before_claim", "crash_queued"}:
            await run_native_step(service, dispatcher.jobs, binding, child_owner="first-native", principal=principal)
        async with sessions() as db:
            parent = await dispatcher.jobs._fetch(db, parent_id)
            manifest = read_manifest(parent)
        if operation not in {"crash_before_assembly", "crash_before_claim", "crash_queued"}:
            resumed = await dispatcher.jobs.resume_general_task_native_parent(parent_id, owner=owner,
                expected_revision=parent.revision, expected_manifest_revision=manifest.manifest_revision)
            owner, fence = resumed["job"]["lease"]["owner"], resumed["job"]["lease"]["fencing_token"]
        assert len(registry.calls) == (0 if operation in {"crash_before_claim", "crash_queued"} else 1)
        if operation == "revision":
            await publish_plan_revision(service, dispatcher.jobs, parent_id, owner=owner, fence=fence,
                request=PlanRevisionRequest(expected_revision=resumed["manifest"]["task_revision"],
                    replacements=[first, second.model_copy(update={"input": {"text": "revised after real readback"}})],
                    reason="Revise only the unadmitted step", idempotency_key="after-first"))
        elif operation == "pause":
            paused = await dispatcher.jobs.pause_general_task_native_parent(parent_id,
                operator_owner=WorkBoardOwner(principal_id=principal.principal_id,
                    session_id=principal.operator_session_id),
                expected_task_revision=resumed["manifest"]["task_revision"],
                expected_revision=resumed["job"]["revision"],
                expected_manifest_revision=resumed["manifest"]["manifest_revision"])
            resumed = await dispatcher.jobs.resume_general_task_native_parent(parent_id, owner=owner,
                expected_revision=paused["job"]["revision"],
                expected_manifest_revision=paused["manifest"]["manifest_revision"])
            owner, fence = resumed["job"]["lease"]["owner"], resumed["job"]["lease"]["fencing_token"]
        service = GeneralTaskService(registry); service.start()
    outcome = await service.execute(dispatcher.jobs, job_id=parent_id, owner=owner, fence=fence,
        envelope=envelope, principal=principal)
    assert outcome["verified"] and outcome["native_execution"] and outcome["no_learning"]
    assert outcome["step_count"] == 2 and len(registry.calls) == 2
    projection = await dispatcher.jobs.get_job(parent_id)
    assert projection["deadline_at"] == original_deadline and projection["attempt_count"] == 1
    async with sessions() as db:
        manifest = read_manifest(await dispatcher.jobs._fetch(db, parent_id))
    assert manifest.step_ids == ["first", "second"]
    active = envelope.model_copy(update={"plan": envelope.plan.model_copy(update={
        "revision": 2, "steps": [first, second.model_copy(update={"input": {"text": "revised after real readback"}})]})}) if operation == "revision" else envelope
    outputs, artifacts = service.recovered_outputs(projection, active)
    expected = "revised after real readback" if operation == "revision" else first.input["text"]
    assert outputs["second"] == {"text": expected}
    assert all(artifacts[key]["content_sha256"] for key in outputs)
