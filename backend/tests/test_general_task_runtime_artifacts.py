"""Immutable local readback and canonical child binding, without contact."""
from dataclasses import replace

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update

from src.db.models import WorkBoardTask, WorkBoardAttempt, WorkflowRunState
from src.work_board.contracts import GeneralTaskToolInputV1
from src.work_board.general_task import digest
from src.work_board.general_task_runtime_artifacts import (
    initial_native_manifest, compile_native_child_binding, stage_task_artifact,
    verify_staged_task_artifact, read_native_artifact_reference,
)
from src.work_board.repository import BoardError
from src.workflows.inference_accounting import InferenceAccountingError
from src.work_board.dispatcher import validate_capability_input, TypedInputError
from tests.test_general_task_native_guard import running_task
from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime


@pytest.mark.asyncio
async def test_revision_sixteen_is_last_adoptable_immutable_plan(task_runtime):
    sessions, dispatcher, service, envelope, current = await running_task(task_runtime)
    from src.work_board.contracts import PlanRevisionRequest
    from src.work_board.general_task_native import publish_plan_revision, current_plan
    from src.workflows.general_task_guard import read_manifest
    parent_id = current["job"]["job_id"]
    owner = current["job"]["lease"]["owner"]
    fence = current["job"]["lease"]["fencing_token"]
    original_creation = current["manifest"]["creation_digest"]
    for revision in range(2, 17):
        request = PlanRevisionRequest(expected_revision=current["manifest"]["task_revision"],
            replacements=envelope.plan.steps, reason="Keep bounded selected steps",
            idempotency_key=f"revision-{revision}")
        await publish_plan_revision(service, dispatcher.jobs, parent_id, owner=owner,
            fence=fence, request=request)
        async with sessions() as db:
            parent = await dispatcher.jobs._fetch(db, parent_id)
            manifest = read_manifest(parent)
            assert manifest.plan_revision == revision
            assert current_plan(manifest, envelope).revision == revision
            assert manifest.creation_digest == original_creation
            assert manifest.original_deadline_at == envelope.proposal_group.original_deadline_at
            assert len(manifest.revision_artifact_ids) == revision
    before = await dispatcher.jobs.get_job(parent_id)
    replay = await publish_plan_revision(service, dispatcher.jobs, parent_id, owner=owner,
        fence=fence, request=request)
    assert replay["job"]["revision"] == before["revision"]
    with pytest.raises(BoardError) as conflict:
        await publish_plan_revision(service, dispatcher.jobs, parent_id, owner=owner,
            fence=fence, request=request.model_copy(update={"reason": "Changed same key"}))
    assert conflict.value.code == "general_task_idempotency_conflict"
    with pytest.raises(BoardError) as error:
        await publish_plan_revision(service, dispatcher.jobs, parent_id, owner=owner,
            fence=fence, request=PlanRevisionRequest(
                expected_revision=current["manifest"]["task_revision"],
                replacements=envelope.plan.steps, reason="Seventeenth must be rejected",
                idempotency_key="revision-17"))
    assert error.value.code == "general_task_plan_revision_stale"
    after = await dispatcher.jobs.get_job(parent_id)
    assert after["revision"] == before["revision"]
    assert after["attempt_count"] == before["attempt_count"] == 1
    assert after["effects"] == []


@pytest.mark.asyncio
async def test_manifest_and_child_compilers_use_original_canonical_binding(task_runtime):
    sessions, _dispatcher, _service, envelope, current = await running_task(task_runtime)
    async with sessions() as db:
        parent = await db.scalar(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == current["job"]["job_id"]))
        task = await db.scalar(select(WorkBoardTask).where(
            WorkBoardTask.task_id == current["manifest"]["task_id"]))
        attempt = await db.scalar(select(WorkBoardAttempt).where(
            WorkBoardAttempt.attempt_id == current["manifest"]["attempt_id"]))
        manifest = initial_native_manifest(parent, task, attempt, envelope)
        assert manifest.model_dump(mode="json") == current["manifest"]
        descriptor, step = envelope.descriptors[0], envelope.plan.steps[0]
        binding = compile_native_child_binding(parent, task, attempt, manifest, step, descriptor, step.input)
        assert binding.parent_authority_digest == parent.authority_digest
        assert binding.creation_digest == manifest.creation_digest
        assert binding.original_root_id == task.owner_session_id
        assert binding.input_digest == digest(step.input)
        assert binding == compile_native_child_binding(parent, task, attempt, manifest, step, descriptor, step.input)


@pytest.mark.asyncio
async def test_private_input_staging_requires_exact_seal_creation_and_readback(task_runtime):
    _sessions, _workspace = task_runtime
    payload = GeneralTaskToolInputV1(parent_job_id="parent-original", creation_digest="a" * 64,
        invocation_id="child-original", tool_id="read_file", descriptor_digest="b" * 64,
        input_digest=digest({"file_path": "private-local.txt"}), inputs={"file_path": "private-local.txt"})
    staged = stage_task_artifact(parent_job_id=payload.parent_job_id,
        creation_digest=payload.creation_digest, payload=payload)
    parsed, metadata = verify_staged_task_artifact(staged, parent_job_id=payload.parent_job_id,
        creation_digest=payload.creation_digest)
    assert parsed == payload and metadata["content_sha256"] == staged.reference.digest
    assert read_native_artifact_reference(staged.reference, parent_job_id=payload.parent_job_id,
        creation_digest=payload.creation_digest) == payload
    for forged in (replace(staged, seal=object()), replace(staged, producer_ref="other"),
        replace(staged, creation_digest="c" * 64), replace(staged, payload=b"changed")):
        with pytest.raises(BoardError):
            verify_staged_task_artifact(forged, parent_job_id=payload.parent_job_id,
                creation_digest=payload.creation_digest)
    with pytest.raises((BoardError, OSError)):
        read_native_artifact_reference(staged.reference, parent_job_id="foreign-parent",
            creation_digest=payload.creation_digest)


@pytest.mark.asyncio
async def test_public_typed_input_cannot_supply_server_group_or_publication_witness(task_runtime):
    sessions, _dispatcher, _service, envelope, current = await running_task(task_runtime)
    from src.work_board.contracts import WorkBoardOwner
    from src.work_board.general_task_proposal import seal_proposal_publication, recheck_proposal_publication
    raw = envelope.model_dump(mode="json")
    # Even a genuine readback is not a client authority-bearing publication.
    with pytest.raises(TypedInputError):
        validate_capability_input("agent.task.v1", raw)
    for field in ("proposal_group", "proposal_provenance"):
        for value in (None, {}, {"owner_principal_id": "forged"}):
            public = {key: item for key, item in raw.items() if key not in {"proposal_group", "proposal_provenance"}}
            public[field] = value
            with pytest.raises(TypedInputError):
                validate_capability_input("agent.task.v1", public)
    owner = WorkBoardOwner(principal_id=current["manifest"]["owner_principal_id"],
        session_id=current["manifest"]["original_root_id"])
    async with sessions() as db:
        witness = await seal_proposal_publication(db, owner, envelope, goal_revision=1)
        for forged in (replace(witness, seal=object()), replace(witness, original_root_id="foreign-root"),
            replace(witness, owner_principal_id="operator:foreign")):
            with pytest.raises(BoardError):
                await recheck_proposal_publication(db, owner, forged)
    with pytest.raises((BoardError, ValidationError)):
        validate_capability_input("agent.task.v1", {**raw, "unexpected": "injected"}, general_task_publication=witness)


@pytest.mark.asyncio
async def test_interpreter_admission_retains_canonical_identity_and_private_readback(task_runtime):
    sessions, dispatcher, service, envelope, current = await running_task(task_runtime)
    from src.work_board.general_task_native import admit_native_step, publish_positive_claim
    from src.work_board.general_task_runtime_artifacts import read_current_native_tool_input
    binding, admitted = await admit_native_step(dispatcher.jobs, current["job"]["job_id"],
        owner=current["job"]["lease"]["owner"], fence=current["job"]["lease"]["fencing_token"],
        step=envelope.plan.steps[0], descriptor=envelope.descriptors[0], inputs=envelope.plan.steps[0].input)
    assert admitted["job_id"] == binding.invocation_id
    assert admitted["attempt_count"] == 0
    await dispatcher.jobs.queue_job(binding.invocation_id)
    claimed = await dispatcher.jobs.claim_job(binding.invocation_id, owner="fixture-native-owner")
    assert claimed["attempt_count"] == 1
    await publish_positive_claim(dispatcher.jobs, binding, child_owner="fixture-native-owner",
        child_fence=claimed["lease"]["fencing_token"])
    async with sessions() as db:
        child = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == binding.invocation_id))
        private = await read_current_native_tool_input(db, child)
        assert private.inputs == envelope.plan.steps[0].input
        assert private.invocation_id == binding.invocation_id
        assert service.registry.calls == []


@pytest.mark.asyncio
async def test_native_success_receipt_and_parent_assembly_share_original_attempt(task_runtime):
    sessions, dispatcher, service, envelope, current = await running_task(task_runtime)
    from dataclasses import replace
    from src.auth.service import authenticate_session
    from src.work_board.general_task_native import admit_native_step, run_native_step
    from src.workflows.general_task_guard import read_manifest
    binding, _admitted = await admit_native_step(dispatcher.jobs, current["job"]["job_id"],
        owner=current["job"]["lease"]["owner"], fence=current["job"]["lease"]["fencing_token"],
        step=envelope.plan.steps[0], descriptor=envelope.descriptors[0], inputs=envelope.plan.steps[0].input)
    operator = await authenticate_session(binding.original_root_id, touch=False)
    principal = replace(operator.principal, session_id=binding.original_root_id,
        operator_session_id=binding.original_root_id, job_id=binding.invocation_id)
    output, artifact, reference = await run_native_step(service, dispatcher.jobs, binding,
        child_owner="native-positive-owner", principal=principal)
    assert output == {"text": "hello"}
    assert artifact["content_sha256"] == reference.digest
    assert len(service.registry.calls) == 1
    child = await dispatcher.jobs.get_job(binding.invocation_id)
    assert child["status"] == "succeeded" and child["attempt_count"] == 1
    parent = await dispatcher.jobs.get_job(binding.parent_job_id)
    async with sessions() as db:
        manifest = read_manifest(await dispatcher.jobs._fetch(db, binding.parent_job_id))
    resumed = await dispatcher.jobs.resume_general_task_native_parent(binding.parent_job_id,
        owner=current["job"]["lease"]["owner"], expected_revision=parent["revision"],
        expected_manifest_revision=manifest.manifest_revision)
    assert resumed["manifest"]["attempt_id"] == current["manifest"]["attempt_id"]
    assert resumed["manifest"]["phase"] == "assembly"
    assert resumed["manifest"]["job_fence"] > current["manifest"]["job_fence"]
    assert resumed["manifest"]["step_ids"] == [binding.step_id]
    async with sessions() as db:
        from src.work_board.contracts import WorkBoardOwner
        read = await service.plan(db, WorkBoardOwner(principal_id=binding.owner_principal_id,
            session_id=binding.original_root_id), binding.task_id)
    assert read["native_execution"]["phase"] == "assembly"
    assert read["native_execution"]["remaining_steps"] == []
    assert read["native_execution"]["partial_output_refs"] == [reference.model_dump(mode="json")]

    # Terminal inspection retains local references without an open attempt.
    from datetime import datetime, timezone
    async with sessions() as db:
        await db.execute(update(WorkflowRunState).where(WorkflowRunState.run_identity == binding.parent_job_id)
            .values(status="succeeded", lease_owner=None, lease_expires_at=None))
        await db.execute(update(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == binding.attempt_id)
            .values(ended_at=datetime.now(timezone.utc)))
        await db.commit()
    async with sessions() as db:
        retained = await service.plan(db, WorkBoardOwner(principal_id=binding.owner_principal_id,
            session_id=binding.original_root_id), binding.task_id)
        assert retained["native_execution"]["partial_output_refs"] == [reference.model_dump(mode="json")]
        from src.work_board.general_task_runtime_artifacts import verify_general_task_manifest
        parent_row = await dispatcher.jobs._fetch(db, binding.parent_job_id)
        task_row = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
        attempt_row = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == binding.attempt_id))
        with pytest.raises(BoardError):
            await verify_general_task_manifest(db, parent_row, task_row, attempt_row, read_manifest(parent_row))


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_id", ["read_file", "write_file"])
async def test_real_filesystem_native_child_reads_physical_workspace(task_runtime, tool_id):
    from dataclasses import replace
    from src.auth.service import authenticate_session
    from src.native_tools.registry import ToolRegistry
    from src.work_board.contracts import GeneralTaskCreate, GeneralTaskInput, PlanSpec
    from src.work_board.general_task_native import admit_native_step, run_native_step
    registry = ToolRegistry()
    registry.start()
    try:
        source = "actual native child source readback"
        (task_runtime[1] / "native-source.txt").write_text(source)
        descriptors = registry.descriptors()
        descriptor = next(item for item in descriptors if item.tool_id == tool_id)
        inputs = {"file_path": "native-source.txt"} if tool_id == "read_file" else {
            "file_path": "native-written.txt", "content": source}
        creation = GeneralTaskCreate(goal_revision=1, idempotency_key="native-physical-read", expected_plan_revision=1,
            input=GeneralTaskInput(goal_ref="goal-1", intent="Read the bounded local source",
                requested_output=descriptor.output_schema, tool_set_digest=digest([item.model_dump(mode="json") for item in descriptors])),
            plan=PlanSpec(revision=1, steps=[{"step_id": "physical-step", "tool_id": tool_id,
                "input": inputs, "output_contract": descriptor.output_schema}]))
        _sessions, dispatcher, service, envelope, current = await running_task(task_runtime,
            creation_request=creation, registry_override=registry)
        binding, _admitted = await admit_native_step(dispatcher.jobs, current["job"]["job_id"],
            owner=current["job"]["lease"]["owner"], fence=current["job"]["lease"]["fencing_token"],
            step=envelope.plan.steps[0], descriptor=descriptor, inputs=envelope.plan.steps[0].input)
        operator = await authenticate_session(binding.original_root_id, touch=False)
        output, artifact, reference = await run_native_step(service, dispatcher.jobs, binding,
            child_owner="native-physical-owner", principal=replace(operator.principal,
                session_id=binding.original_root_id, operator_session_id=binding.original_root_id))
        if tool_id == "read_file":
            assert output["content"] == source
        else:
            assert output["bytes_written"] == len(source.encode())
            assert (task_runtime[1] / "native-written.txt").read_text() == source
        assert artifact["content_sha256"] == reference.digest
        assert (await dispatcher.jobs.get_job(binding.invocation_id))["status"] == "succeeded"
    finally:
        registry.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["expired", "unknown"])
async def test_retained_readonly_projection_has_no_current_execution_authority(task_runtime, monkeypatch, state):
    sessions, dispatcher, service, envelope, current = await running_task(task_runtime)
    from datetime import datetime, timedelta, timezone
    from src.work_board.contracts import WorkBoardOwner
    from src.work_board.general_task_runtime_artifacts import verify_readonly_native_projection, verify_general_task_manifest
    from src.workflows.general_task_guard import read_manifest
    owner = WorkBoardOwner(principal_id=current["manifest"]["owner_principal_id"],
        session_id=current["manifest"]["original_root_id"])
    if state == "expired":
        import src.workflows.general_task_accounting as accounting
        class Future(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(days=2)
        monkeypatch.setattr(accounting, "datetime", Future)
    else:
        async with sessions() as db:
            await db.execute(update(WorkflowRunState).where(WorkflowRunState.run_identity == current["job"]["job_id"])
                .values(status="unknown_external_effect", lease_owner=None, lease_expires_at=None))
            await db.execute(update(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == current["manifest"]["attempt_id"])
                .values(cancel_requested_at=datetime.now(timezone.utc)))
            await db.commit()
    async with sessions() as db:
        parent = await dispatcher.jobs._fetch(db, current["job"]["job_id"])
        task = await service.repository.get_task(db, owner, current["manifest"]["task_id"])
        attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == current["manifest"]["attempt_id"]))
        manifest = read_manifest(parent)
        retained = await verify_readonly_native_projection(db, owner, parent, task, attempt, manifest)
        assert retained.plan == envelope.plan
        with pytest.raises((BoardError, InferenceAccountingError)):
            await verify_general_task_manifest(db, parent, task, attempt, manifest)
        with pytest.raises(BoardError):
            await verify_readonly_native_projection(db, WorkBoardOwner(principal_id="operator:foreign", session_id="foreign-root"),
                parent, task, attempt, manifest)
        with pytest.raises(BoardError):
            await verify_readonly_native_projection(db, owner, parent, task, attempt,
                manifest.model_copy(update={"original_envelope_artifact_id": "foreign-source"}))
