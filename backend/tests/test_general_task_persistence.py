"""Literal local persistence/readback; no inference credentials or transports."""
from tests.general_task_method_lifecycle import native_admission_lifecycle
import socket
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

from config.settings import settings
from src.db.models import Goal, WorkBoardStatus, WorkBoardTask, WorkflowRunState
from src.work_board.contracts import WorkBoardOwner, GeneralTaskPlanUpdate
from src.work_board.general_task import GeneralTaskService
from src.work_board.repository import BoardError
from tests.test_general_task_contract import Registry, request
from tests.test_work_board_m6_provider_free_journey import isolated_runtime, OWNER, SESSION, _goal


def actual_output_checkpoints(run):
    """Authenticate reserved capacity separately from physically produced output."""
    import json
    from types import SimpleNamespace
    from src.workflows.general_task_guard import read_native_checkpoint_reservation
    records = run["checkpoints"] if isinstance(run, dict) else json.loads(run.checkpoint_receipts_json)
    parent = SimpleNamespace(run_identity=run["job_id"] if isinstance(run, dict) else run.run_identity,
        checkpoint_receipts_json=json.dumps(records))
    outputs = []
    for record in records:
        if not record["checkpoint_id"].startswith(("general:verified:", "general:artifact:")):
            continue
        if record.get("payload", {}).get("schema_version") == "general_task.checkpoint_reservation.v1":
            reservation = read_native_checkpoint_reservation(parent, record["checkpoint_id"])
            assert reservation.invocation_id is not None and reservation.binding_digest is not None
        else:
            outputs.append(record)
    return outputs


@pytest.fixture
def task_runtime(isolated_runtime):
    sessions, workspace = isolated_runtime
    @asynccontextmanager
    async def extended_sessions():
        from src.work_board.channel_capture import staged_captured_source_identity
        with staged_captured_source_identity():
            async with sessions() as db:
                # The shared literal SQLite adapter predates input-artifact
                # reservations; provide the real transaction method they require.
                @asynccontextmanager
                async def begin():
                    with db._session.begin():
                        yield db
                db.begin = begin
                yield db
    return extended_sessions, workspace


@pytest.fixture(autouse=True)
def deny_provider(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("general task test attempted external contact")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(settings, "openrouter_api_key", "")


@pytest.mark.asyncio
async def test_one_canonical_task_immutable_plan_and_owner_isolation(task_runtime):
    sessions, workspace = task_runtime
    owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)
    async with sessions() as db:
        db.add(Goal(id="goal-1", title="General task", owner_principal_id=OWNER,
                    owner_session_id=SESSION, revision=1))
    registry = Registry()
    service = GeneralTaskService(registry)
    service.start()
    async with sessions() as db:
        created = await service.create(db, owner, request(registry))
        task_id = created.task.task_id
        assert created.task.status == WorkBoardStatus.triage
        assert created.task.input_artifact_id
    async with sessions() as db:
        replay = await service.create(db, owner, request(registry))
        assert replay.task.task_id == task_id
        assert replay.idempotent_replay
    async with sessions() as db:
        projection = await service.plan(db, owner, task_id)
        assert projection["plan"]["steps"][0]["input"] == {"text": "hello"}
        assert projection["accepted"] is False
        assert len((await db.execute(select(WorkBoardTask))).scalars().all()) == 1
        assert len((await db.execute(select(WorkflowRunState))).scalars().all()) == 0
    async with sessions() as db:
        with pytest.raises(BoardError):
            await service.plan(db, WorkBoardOwner(principal_id="other", session_id=SESSION), task_id)
    assert registry.calls == []


@pytest.mark.asyncio
async def test_plan_edit_keeps_one_task_revokes_prior_artifact_and_fences_stale_revision(task_runtime):
    sessions, workspace = task_runtime
    owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)
    async with sessions() as db:
        db.add(_goal("goal-1", "Editable general task"))
    registry = Registry()
    service = GeneralTaskService(registry)
    service.start()
    initial = request(registry)
    async with sessions() as db:
        mutation = await service.create(db, owner, initial)
        task_id, task_revision, original_artifact = mutation.task.task_id, mutation.task.task_revision, mutation.task.input_artifact_id
    revised = initial.plan.model_copy(update={"revision": 2,
        "steps": [initial.plan.steps[0].model_copy(update={"input": {"text": "changed"}})]})
    update = GeneralTaskPlanUpdate(expected_revision=task_revision, expected_plan_revision=1,
        idempotency_key="edit-1", plan=revised)
    async with sessions() as db:
        task = await service.update_plan(db, owner, task_id, update)
        assert task.task_id == task_id
        assert task.task_revision == task_revision + 1
        assert task.input_artifact_id != original_artifact
    async with sessions() as db:
        from src.db.models import WorkBoardInputArtifact
        assert (await db.get(WorkBoardInputArtifact, original_artifact)).state == "revoked"
        plan = await service.plan(db, owner, task_id)
        assert plan["plan"]["revision"] == 2
        assert plan["plan"]["steps"][0]["input"] == {"text": "changed"}
        assert len((await db.execute(select(WorkBoardTask))).scalars().all()) == 1
    async with sessions() as db:
        with pytest.raises(BoardError):
            await service.update_plan(db, owner, task_id, update)


@pytest.mark.asyncio
async def test_accepted_task_executes_real_durable_root_and_private_artifact_readback(task_runtime, native_admission_lifecycle):
    sessions, workspace = task_runtime
    owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)
    async with sessions() as db:
        db.add(_goal("goal-1", "Execute literal general task"))
    registry = Registry()
    service = GeneralTaskService(registry)
    service.start()
    accepted = request(registry).model_copy(update={"accept": True})
    async with sessions() as db:
        task = (await service.create(db, owner, accepted)).task
        task_id = task.task_id
    from src.work_board.dispatcher import WorkBoardDispatcher
    dispatcher = WorkBoardDispatcher(session_provider=sessions, general_tasks=service)
    result = await dispatcher.run_pass()
    assert result["completed"] == 1, result
    async with sessions() as db:
        task = await service.repository.get_task(db, owner, task_id)
        assert task.status == WorkBoardStatus.review
        runs = (await db.execute(select(WorkflowRunState))).scalars().all()
        assert len(runs) == 2
        roots = [run for run in runs if run.job_kind == "agent.task.v1"]
        children = [run for run in runs if run.job_kind == "general_task_native_tool_v1"]
        assert len(roots) == len(children) == 1
        assert roots[0].status == children[0].status == "succeeded"
        assert roots[0].owner_principal_id == children[0].owner_principal_id == OWNER
        assert children[0].parent_job_id == roots[0].run_identity and children[0].attempt_count == 1
    assert len(registry.calls) == 1
    assert registry.calls[0][2]["principal"].job_id == children[0].run_identity
    assert list((workspace / "artifacts/work-board/general-tasks").glob("*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("step_count", [8, 16])
async def test_dependency_chain_retains_artifacts_and_stops_before_reserved_capacity(task_runtime, step_count, native_admission_lifecycle):
    sessions, workspace = task_runtime
    owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)
    async with sessions() as db:
        db.add(_goal("goal-1", "Sixteen bounded dependent local steps"))
    registry = Registry()
    service = GeneralTaskService(registry)
    service.start()
    from src.work_board.contracts import PlanSpec
    steps = [{"step_id": "s0", "tool_id": "fixture.read", "input": {"text": "literal"},
        "output_contract": registry.entries[0].output_schema}]
    for index in range(1, step_count):
        steps.append({"step_id": f"s{index}", "tool_id": "fixture.read",
            "input": {"text": {"$dependency": {"step_id": f"s{index-1}", "pointer": "/text"}}},
            "depends_on": [f"s{index-1}"], "output_contract": registry.entries[0].output_schema})
    accepted = request(registry).model_copy(update={"accept": True, "plan": PlanSpec(revision=1, steps=steps)})
    async with sessions() as db:
        await service.create(db, owner, accepted)
    from src.work_board.dispatcher import WorkBoardDispatcher
    dispatcher = WorkBoardDispatcher(session_provider=sessions, general_tasks=service)
    result = await dispatcher.run_pass()
    expected_children = 8 if step_count == 8 else 9
    assert result["completed"] == (1 if step_count == 8 else 0), result
    if step_count == 16:
        assert result["blocked"] == 1, result
    assert len(registry.calls) == expected_children
    assert all(call[1] == {"text": "literal"} for call in registry.calls)
    async with sessions() as db:
        runs = list((await db.execute(select(WorkflowRunState))).scalars().all())
        roots = [run for run in runs if run.job_kind == "agent.task.v1"]
        children = [run for run in runs if run.job_kind == "general_task_native_tool_v1"]
        assert len(roots) == 1 and len(children) == expected_children
        run = roots[0]
        assert all(child.status == "succeeded" and child.attempt_count == 1
            and child.parent_job_id == run.run_identity for child in children)
        import json
        checkpoints = json.loads(run.checkpoint_receipts_json)
        if step_count == 16:
            assert len({item["checkpoint_id"] for item in checkpoints}) == 47
            verified = [item for child in children for item in json.loads(child.checkpoint_receipts_json)
                if item["checkpoint_id"].startswith("general:artifact:")]
        else:
            verified = [item for item in checkpoints if item["checkpoint_id"].startswith("general:verified:")]
        assert len(verified) == expected_children
        paths = {item["payload"]["file_path"] for item in verified}
        assert len(paths) == expected_children and all((workspace / path).is_file() for path in paths)
        for path in paths:
            assert json.loads((workspace / path).read_text())["output"] == {"text": "literal"}
    if step_count == 16:
        assert (await dispatcher.run_pass())["completed"] == 0
        assert len(registry.calls) == expected_children


@pytest.mark.asyncio
async def test_sixteen_step_dependency_chain_has_independent_verified_artifacts(task_runtime, native_admission_lifecycle):
    """All sixteen actual production read callbacks fit their proved capacity."""
    import hashlib
    import json
    from src.native_tools.task_adapters import ToolRegistry
    from src.work_board.contracts import GeneralTaskCreate, GeneralTaskInput, PlanSpec
    from src.work_board.general_task import digest
    from src.work_board.dispatcher import WorkBoardDispatcher
    sessions, workspace = task_runtime
    owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)
    async with sessions() as db:
        db.add(_goal("goal-1", "Sixteen actual dependent workspace reads"))
    filename = "sixteen-source.txt"
    (workspace / filename).write_text(filename)
    registry = ToolRegistry()
    registry.start()
    service = GeneralTaskService(registry)
    service.start()
    try:
        descriptors, tool_digest = service.snapshot()
        selected = next(item for item in descriptors if item.tool_id == "read_file")
        steps = [{"step_id": "s0", "tool_id": selected.tool_id,
            "input": {"file_path": filename}, "output_contract": selected.output_schema}]
        for index in range(1, 16):
            steps.append({"step_id": f"s{index}", "tool_id": selected.tool_id,
                "input": {"file_path": {"$dependency": {"step_id": f"s{index-1}", "pointer": "/content"}}},
                "depends_on": [f"s{index-1}"], "output_contract": selected.output_schema})
        accepted = GeneralTaskCreate(goal_revision=1, idempotency_key="actual-sixteen",
            expected_plan_revision=1, accept=True,
            input=GeneralTaskInput(goal_ref="goal-1", intent="Read sixteen bounded dependent workspace files",
                requested_output=selected.output_schema, tool_set_digest=tool_digest),
            plan=PlanSpec(revision=1, steps=steps))
        async with sessions() as db:
            await service.create(db, owner, accepted)
        dispatcher = WorkBoardDispatcher(session_provider=sessions, general_tasks=service)
        assert (await dispatcher.run_pass())["completed"] == 1
        async with sessions() as db:
            runs = list((await db.execute(select(WorkflowRunState))).scalars())
            roots = [run for run in runs if run.job_kind == "agent.task.v1"]
            children = [run for run in runs if run.job_kind == "general_task_native_tool_v1"]
            assert len(roots) == 1 and len(children) == 16
            parent = roots[0]
            assert parent.status == "succeeded" and parent.attempt_count == 1
            assert len(json.loads(parent.checkpoint_receipts_json)) == 50
            expected = {"content": filename, "sha256": hashlib.sha256(filename.encode()).hexdigest()}
            paths = set()
            for child in children:
                assert child.status == "succeeded" and child.attempt_count == 1
                assert child.parent_job_id == parent.run_identity
                artifact, = actual_output_checkpoints(child)
                artifact = artifact["payload"]
                path = workspace / artifact["file_path"]
                raw = path.read_bytes()
                assert hashlib.sha256(raw).hexdigest() == artifact["content_sha256"]
                assert json.loads(raw)["output"] == expected
                assert any(effect["effect_type"] == "general_tool_call" and effect["status"] == "succeeded"
                    and effect["receipt_kind"] == "readback" and effect["content_sha256"] == artifact["content_sha256"]
                    for effect in json.loads(child.effect_receipts_json))
                paths.add(path)
            assert len(paths) == 16
            assert len(actual_output_checkpoints(parent)) == 32
            from src.db.models import AuditEvent, MemoryProposal
            # The exact adopted filesystem host owns its integration audit;
            # AuthorityTool routes through that host instead of AuditedTool.
            calls = list((await db.execute(select(AuditEvent).where(
                AuditEvent.tool_name == "filesystem:workspace", AuditEvent.event_type == "integration_succeeded"))).scalars())
            assert len(calls) == 16
            assert all(json.loads(event.details_json)["operation"] == "read" for event in calls)
            assert list((await db.execute(select(MemoryProposal))).scalars()) == []
        assert (await dispatcher.run_pass())["completed"] == 0
        async with sessions() as db:
            assert len(list((await db.execute(select(WorkflowRunState))).scalars())) == 17
    finally:
        service.stop()
        registry.stop()


@pytest.mark.asyncio
async def test_step_schema_violation_and_removed_tool_block_before_execution_admission(task_runtime, native_admission_lifecycle):
    sessions, workspace = task_runtime
    owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)
    async with sessions() as db:
        db.add(_goal("goal-1", "Removed tool cannot dispatch"))
    registry = Registry()
    service = GeneralTaskService(registry)
    service.start()
    async with sessions() as db:
        await service.create(db, owner, request(registry).model_copy(update={"accept": True}))
    registry.entries = []
    from src.work_board.dispatcher import WorkBoardDispatcher
    result = await WorkBoardDispatcher(session_provider=sessions, general_tasks=service).run_pass()
    assert result["admitted"] == 0, result
    assert registry.calls == []
    async with sessions() as db:
        assert list((await db.execute(select(WorkflowRunState))).scalars()) == []


@pytest.mark.asyncio
async def test_invalid_tool_and_changed_schema_publish_no_task(task_runtime):
    sessions, workspace = task_runtime
    owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)
    async with sessions() as db:
        db.add(Goal(id="goal-1", title="General task", owner_principal_id=OWNER,
                    owner_session_id=SESSION, revision=1))
    registry = Registry()
    service = GeneralTaskService(registry)
    service.start()
    bad = request(registry)
    bad = bad.model_copy(update={"plan": bad.plan.model_copy(update={"steps": [
        bad.plan.steps[0].model_copy(update={"tool_id": "unknown.tool"})]})})
    async with sessions() as db:
        with pytest.raises(BoardError, match="registered tool"):
            await service.create(db, owner, bad)
        assert len((await db.execute(select(WorkBoardTask))).scalars().all()) == 0
        assert len((await db.execute(select(WorkflowRunState))).scalars().all()) == 0
