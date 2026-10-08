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
@pytest.mark.parametrize("operation", ["execute", "restart", "crash_before_assembly", "crash_before_claim", "crash_queued", "revision", "pause", "paused_revision"])
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
        elif operation in {"pause", "paused_revision"}:
            paused = await dispatcher.jobs.pause_general_task_native_parent(parent_id,
                operator_owner=WorkBoardOwner(principal_id=principal.principal_id,
                    session_id=principal.operator_session_id),
                expected_task_revision=resumed["manifest"]["task_revision"],
                expected_revision=resumed["job"]["revision"],
                expected_manifest_revision=resumed["manifest"]["manifest_revision"])
            if operation == "paused_revision":
                edited = await dispatcher.jobs.revise_general_task_operator_paused_parent(parent_id,
                    operator_owner=WorkBoardOwner(principal_id=principal.principal_id,
                        session_id=principal.operator_session_id), service=service,
                    request=PlanRevisionRequest(expected_revision=paused["manifest"]["task_revision"],
                        replacements=[first, second.model_copy(update={"input": {"text": "revised after real readback"}})],
                        reason="Edit unadmitted step while paused", idempotency_key="paused-after-first"))
                assert edited["job"]["status"] == "paused" and edited["job"]["attempt_count"] == 1
                assert edited["job"]["deadline_at"] == paused["job"]["deadline_at"]
                assert {key: edited["job"]["lease"][key] for key in ("owner", "expires_at", "fencing_token")} == {
                    key: paused["job"]["lease"][key] for key in ("owner", "expires_at", "fencing_token")}
                assert edited["manifest"]["step_receipt_digests"] == paused["manifest"]["step_receipt_digests"]
                paused = edited
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
        "revision": 2, "steps": [first, second.model_copy(update={"input": {"text": "revised after real readback"}})]})}) if operation in {"revision", "paused_revision"} else envelope
    outputs, artifacts = service.recovered_outputs(projection, active)
    expected = "revised after real readback" if operation in {"revision", "paused_revision"} else first.input["text"]
    assert outputs["second"] == {"text": expected}
    assert all(artifacts[key]["content_sha256"] for key in outputs)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["restart", "exhausted", "unknown"])
async def test_interpreter_governed_continuation_uses_real_result_and_never_egresses_private_bytes(task_runtime, monkeypatch, mode):
    import json
    from tests.general_task_test_transport import prepare_literal_planner
    from src.work_board import general_task_native as native
    registry = Registry()
    creation = request(registry)
    first = creation.plan.steps[0].model_copy(update={"step_id": "first",
        "input": {"text": "PRIVATE-FIRST-OUTPUT-NEVER-EGRESS"}})
    second = first.model_copy(update={"step_id": "second", "depends_on": ["first"],
        "input": {"text": {"$dependency": {"step_id": "first", "pointer": "/text"}}}})
    creation = creation.model_copy(update={"plan": PlanSpec(revision=1, steps=[first, second]),
        "input": creation.input.model_copy(update={"inference_egress_acknowledged": True,
            "limits": creation.input.limits.model_copy(update={"wall_seconds": 600,
                "max_inference_calls": 2 if mode == "restart" else 1, "max_cost_microusd": 1000})})})
    sessions, dispatcher, service, envelope, current = await running_task(task_runtime,
        creation_request=creation, registry_override=registry)
    parent_id = current["job"]["job_id"]
    owner, fence = current["job"]["lease"]["owner"], current["job"]["lease"]["fencing_token"]
    principal = (await authenticate_session(current["manifest"]["original_root_id"], touch=False)).principal
    operator = WorkBoardOwner(principal_id=principal.principal_id, session_id=principal.operator_session_id)
    revised = second.model_copy(update={"input": {"text": "revised by bounded continuation"}})
    planner, transport = await prepare_literal_planner(sessions, task_runtime[1], monkeypatch, operator,
        PlanSpec(revision=2, steps=[revised.model_copy(update={"depends_on": []})]))
    # The returned partial step can refer to a completed predecessor; only the
    # local parser merges original private inputs before PlanSpec validation.
    transport["content"] = json.dumps({"schema_version": 1, "revision": 2,
        "steps": [revised.model_dump(mode="json")]})
    service.planner = planner
    if mode != "restart":
        from sqlalchemy import select
        from src.db.models import InferenceCostReservation
        from src.workflows.inference_accounting import InferenceAccountingError
        from src.work_board.general_task_native import current_interpreter
        parent, task, attempt, original, manifest = await current_interpreter(dispatcher.jobs,
            parent_id, owner=owner, fence=fence)
        transport["content"] = json.dumps(envelope.plan.model_copy(update={"revision": 2}).model_dump(mode="json"))
        async with sessions() as db:
            await planner.continue_plan(db, operator, parent=parent, task=task, attempt=attempt,
                manifest=manifest, envelope=original, request_key="consume-original-call-slot")
        assert len(transport["contacts"]) == 1
        if mode == "unknown":
            async with sessions() as db:
                reservation = await db.scalar(select(InferenceCostReservation))
                reservation.state = "unknown"
                await db.commit()
            with pytest.raises(InferenceAccountingError, match="group_unknown"):
                await service.execute(dispatcher.jobs, job_id=parent_id, owner=owner, fence=fence,
                    envelope=envelope, principal=principal)
            assert len(registry.calls) == 1 and len(transport["contacts"]) == 1
            return
        outcome = await service.execute(dispatcher.jobs, job_id=parent_id, owner=owner, fence=fence,
            envelope=envelope, principal=principal)
        assert outcome["verified"] and len(registry.calls) == 2 and len(transport["contacts"]) == 1
        projection = await dispatcher.jobs.get_job(parent_id)
        assert any(item["checkpoint_id"].startswith("general:continuation-budget:")
            and item["payload"]["reason"] == "general_task_continuation_budget_exhausted"
            for item in projection["checkpoints"])
        return
    original_admit = native.admit_native_step
    async def crash_before_second(*args, **kwargs):
        if kwargs["step"].step_id == "second":
            raise RuntimeError("simulated restart after immutable continuation publication")
        return await original_admit(*args, **kwargs)
    monkeypatch.setattr(native, "admit_native_step", crash_before_second)
    with pytest.raises(RuntimeError, match="simulated restart"):
        await service.execute(dispatcher.jobs, job_id=parent_id, owner=owner, fence=fence,
            envelope=envelope, principal=principal)
    assert len(registry.calls) == 1 and len(transport["contacts"]) == 1
    monkeypatch.setattr(native, "admit_native_step", original_admit)
    projection = await dispatcher.jobs.get_job(parent_id)
    service = GeneralTaskService(registry, planner=planner); service.start()
    outcome = await service.execute(dispatcher.jobs,
        job_id=parent_id, owner=projection["lease"]["owner"], fence=projection["lease"]["fencing_token"],
        envelope=envelope, principal=principal)
    assert outcome["verified"] and len(registry.calls) == 2 and len(transport["contacts"]) == 1
    contacted = json.dumps(transport["contacts"])
    assert "PRIVATE-FIRST-OUTPUT-NEVER-EGRESS" not in contacted
    assert "artifact:" in contacted and "step_statuses" in contacted
    from src.work_board.general_task_native import current_interpreter, current_plan
    projection = await dispatcher.jobs.get_job(parent_id)
    parent, task, attempt, original, manifest = await current_interpreter(dispatcher.jobs,
        parent_id, owner=projection["lease"]["owner"], fence=projection["lease"]["fencing_token"])
    plan = current_plan(manifest, original)
    assert plan.revision == 2 and plan.steps[0] == first
    outputs, _ = service.recovered_outputs(projection, original.model_copy(update={"plan": plan}))
    assert outputs["second"] == {"text": "revised by bounded continuation"}
    assert len(transport["contacts"]) == 1
