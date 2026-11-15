"""Actual original tool thread closure, without provider or model calls."""
from tests.general_task_method_lifecycle import native_admission_lifecycle
import asyncio
from dataclasses import replace
import threading

import pytest

from src.auth.service import authenticate_session
from src.native_tools.registry import ToolRegistry
from src.native_tools.task_adapters import verify_task_tool_closure
from src.work_board.contracts import GeneralTaskCreate, GeneralTaskInput, PlanSpec, WorkBoardOwner
from src.work_board.general_task import digest
from src.work_board.general_task_native import admit_native_step, publish_positive_claim
from src.workflows.general_task_guard import read_manifest
from src.workflows.job_runtime import DurableJobLeaseError
from tests.test_general_task_native_guard import running_task
from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
async def test_original_file_callback_must_exit_before_closure(interruption, task_runtime, monkeypatch, native_admission_lifecycle):
    registry = ToolRegistry()
    registry.start()
    entered, release = threading.Event(), threading.Event()
    from src.tools.filesystem_tool import read_file
    original = read_file.forward
    def held_read(file_path):
        entered.set()
        if not release.wait(3):
            raise RuntimeError("fixture original callback did not receive release")
        return original(file_path)
    monkeypatch.setattr(read_file, "forward", held_read)
    source = "Only the original file callback returning proves closure"
    (task_runtime[1] / "held-source.txt").write_text(source)
    descriptors = registry.descriptors()
    descriptor = next(item for item in descriptors if item.tool_id == "read_file")
    creation = GeneralTaskCreate(goal_revision=1, idempotency_key="held-callback", expected_plan_revision=1,
        input=GeneralTaskInput(goal_ref="goal-1", intent="Read one owned local file",
            requested_output=descriptor.output_schema,
            tool_set_digest=digest([item.model_dump(mode="json") for item in descriptors])),
        plan=PlanSpec(revision=1, steps=[{"step_id": "held-read", "tool_id": "read_file",
            "input": {"file_path": "held-source.txt"}, "output_contract": descriptor.output_schema}]))
    handle = None
    try:
        sessions, dispatcher, service, envelope, current = await running_task(task_runtime,
            creation_request=creation, registry_override=registry)
        binding, _ = await admit_native_step(dispatcher.jobs, current["job"]["job_id"],
            owner=current["job"]["lease"]["owner"], fence=current["job"]["lease"]["fencing_token"],
            step=envelope.plan.steps[0], descriptor=descriptor, inputs=envelope.plan.steps[0].input)
        await dispatcher.jobs.queue_job(binding.invocation_id)
        claimed = await dispatcher.jobs.claim_job(binding.invocation_id, owner="held-native-child")
        fence = claimed["lease"]["fencing_token"]
        await publish_positive_claim(dispatcher.jobs, binding, child_owner="held-native-child", child_fence=fence)
        operator = await authenticate_session(binding.original_root_id, touch=False)
        principal = replace(operator.principal, job_id=binding.invocation_id)
        handle = registry.begin_invocation(descriptor, envelope.plan.steps[0].input,
            principal=principal, job_id=binding.invocation_id, fencing_token=fence)
        closures = []
        handle.on_closed(lambda original_handle: closures.append(original_handle.witness))
        assert await asyncio.to_thread(entered.wait, 1)
        if interruption == "timeout":
            with pytest.raises(TimeoutError):
                await handle.wait(timeout=0.01)
        else:
            waiter = asyncio.create_task(handle.wait())
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        assert not handle.closed
        assert closures == []
        with pytest.raises(PermissionError, match="has not closed"):
            _ = handle.witness
        parent = await dispatcher.jobs.get_job(binding.parent_job_id)
        async with sessions() as db:
            manifest = read_manifest(await dispatcher.jobs._fetch(db, binding.parent_job_id))
        with pytest.raises(DurableJobLeaseError, match="native children must close"):
            await dispatcher.jobs.pause_general_task_native_parent(binding.parent_job_id,
                operator_owner=WorkBoardOwner(
                    principal_id=binding.owner_principal_id, session_id=binding.original_root_id),
                expected_task_revision=manifest.task_revision,
                expected_revision=parent["revision"],
                expected_manifest_revision=manifest.manifest_revision)
        release.set()
        output = await handle.wait(timeout=2)
        assert handle.closed and output["content"] == source
        assert len(closures) == 1 and closures[0] is handle.witness
        closure = verify_task_tool_closure(handle.witness, binding=binding, fencing_token=fence)
        assert closure.outcome == "returned" and closure.output_digest == digest(output)
        for witness, wrong_binding, wrong_fence in (
            (replace(handle.witness, _seal=object()), binding, fence),
            (handle.witness, binding.model_copy(update={"invocation_id": "foreign-child"}), fence),
            (handle.witness, binding.model_copy(update={"input_digest": "a" * 64}), fence),
            (handle.witness, binding, fence + 1),
        ):
            with pytest.raises(PermissionError):
                verify_task_tool_closure(witness, binding=wrong_binding, fencing_token=wrong_fence)
    finally:
        release.set()
        if handle is not None and not handle.closed:
            await handle.wait(timeout=2)
        registry.stop()
