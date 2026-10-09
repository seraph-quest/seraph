"""Full substitution and canonical lifecycle regressions, without inference."""
import pytest
from src.native_tools.registry import ToolRegistry
from src.memory import task_methods as methods
from src.work_board.contracts import GeneralTaskEnvelope, GeneralTaskInput, TaskStrategyBinding
from src.work_board.general_task import GeneralTaskService, digest
from src.work_board.repository import BoardError
from src.workflows.procedure_contracts import (ProcedureCandidateV3, ProcedurePlanV3,
    instantiate_procedure_plan, procedure_tool_pin, procedure_permissions_digest)
from tests.test_general_task_methods import setup_method
from tests.test_task_methods import review
from tests.test_task_lessons import no_inference


def template(registry):
    descriptor = next(item for item in registry.descriptors() if item.tool_id == "read_file")
    pin = procedure_tool_pin(descriptor)
    plan = ProcedurePlanV3(source_task_id="structural-source", source_attempt="structural-attempt",
        steps=[{"step_id": "read", "tool_id": "read_file", "input": {"file_path": {"$parameter": "path"}},
            "output_contract": descriptor.output_schema}],
        parameters=[{"name": "path", "step_id": "read", "input_pointer": "/file_path",
            "schema": descriptor.procedure_inputs.classifications[0].schema,
            "producer_id": pin.producer_id, "producer_contract_digest": pin.producer_contract_digest}],
        tool_contract_versions=[pin], output_contract=descriptor.output_schema,
        permissions_digest=procedure_permissions_digest([pin]))
    return descriptor, plan


def test_consumer_compares_full_plan_and_keeps_fixed_c1_wire():
    registry = ToolRegistry(); registry.start()
    service = GeneralTaskService(registry); service.start()
    try:
        descriptor, plan = template(registry)
        candidate = ProcedureCandidateV3(plan=plan)
        binding = TaskStrategyBinding(status="active", method_id="structural-proposal", version="structural-version",
            digest=digest(candidate.model_dump(mode="json")), typed_data=candidate.model_dump(mode="json"))
        actual = instantiate_procedure_plan(plan, {"path": "fresh.txt"})
        envelope = GeneralTaskEnvelope(task_input=GeneralTaskInput(goal_ref="goal", intent="Read fresh source",
            requested_output=plan.output_contract, tool_set_digest=service.snapshot()[1]),
            plan=actual, descriptors=[descriptor], strategy=binding)
        service.validate_method_plan(envelope)
        assert "parameters" not in envelope.task_input.model_dump(mode="json")
        assert "parameters" not in envelope.model_dump(mode="json")
        for alteration in (
            actual.model_copy(update={"revision": 2}),
            actual.model_copy(update={"steps": [actual.steps[0].model_copy(update={"step_id": "different"})]}),
            actual.model_copy(update={"steps": [actual.steps[0].model_copy(update={"input": {"file_path": "fresh.txt", "extra": "drift"}})]}),
            actual.model_copy(update={"steps": [actual.steps[0].model_copy(update={"output_contract": {"type": "object"}})]}),
        ):
            with pytest.raises(BoardError):
                service.validate_method_plan(envelope.model_copy(update={"plan": alteration}))
        with pytest.raises(BoardError) as missing:
            service.validate_method_plan(envelope.model_copy(update={"plan": None}))
        assert missing.value.code == "procedure_parameter_input_required"
        with pytest.raises(ValueError):
            instantiate_procedure_plan(plan, {"path": True})
        with pytest.raises(ValueError):
            instantiate_procedure_plan(plan, {"path": "fresh.txt", "extra": "bad"})
    finally:
        service.stop(); registry.stop()


@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.asyncio
async def test_existing_review_owner_disable_activate_and_canonical_delete(async_db, monkeypatch, tmp_path, no_inference):
    from src.db.models import MemoryTombstone
    from src.memory.repository import memory_repository
    operator, current, owner, registry, service, dispatcher = await setup_method(async_db, monkeypatch, tmp_path)
    try:
        original = await current.resolve(owner, "goal", "work.general-task.v1")
        disabled = await methods.review_method(operator, review(await methods.inspect_method(operator, original.method_id),
            "disable", "disable-general-family"))
        assert disabled["configured_baseline"] is True
        assert (await current.resolve(owner, "goal", "work.general-task.v1")).status == "none"
        assert await current.validate_pinned(owner, "goal", original) == original
        activated = await methods.review_method(operator, review(await methods.inspect_method(operator, original.method_id),
            "activate", "activate-exact-prior"))
        assert activated["active_binding"]["version"] == original.version
        assert await current.resolve(owner, "goal", "work.general-task.v1") == original
        deleted = await methods.review_method(operator, review(await methods.inspect_method(operator, original.method_id),
            "delete", "delete-canonical-method"))
        assert deleted["configured_baseline"] is True
        async with async_db() as db:
            from sqlalchemy import select
            assert await db.scalar(select(MemoryTombstone).where(MemoryTombstone.memory_id == original.version)) is not None
        with pytest.raises(BoardError) as revoked:
            await current.validate_pinned(owner, "goal", original)
        assert revoked.value.code == "method_version_unavailable"
        assert await memory_repository.list_memories(for_model_context=True) == []
    finally:
        service.stop(); registry.stop(); await current.stop()


@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.asyncio
async def test_ordinary_original_no_pin_task_executes_without_source_upgrade(async_db, monkeypatch, tmp_path, no_inference):
    """Create a real no-pin envelope, then restore today's producer descriptor."""
    from config.settings import settings
    from pathlib import Path
    from tests.test_general_task_methods import read_request
    from src.work_board.dispatcher import _parse_typed_input
    from src.work_board.review import complete_review
    from src.memory.task_lessons import eligible_procedure_source
    from src.db.models import WorkBoardTask, WorkBoardAttempt
    from sqlalchemy import select
    operator, current, owner, registry, service, dispatcher = await setup_method(async_db, monkeypatch, tmp_path)
    modern_descriptors = registry.descriptors
    def historical_descriptors():
        return [descriptor.model_copy(update={"procedure_inputs": None}) for descriptor in modern_descriptors()]
    monkeypatch.setattr(registry, "descriptors", historical_descriptors)
    try:
        (Path(settings.workspace_dir) / "selected.txt").write_text("Actual original no-pin ordinary task")
        async with async_db() as db:
            created = await service.create(db, owner, read_request(registry, "ordinary-before-classification"))
        monkeypatch.setattr(registry, "descriptors", modern_descriptors)
        result = await dispatcher.run_pass()
        assert result["completed"] == 1, result
        async with async_db() as db:
            task = await db.get(WorkBoardTask, created.task.task_id)
            original = _parse_typed_input(task)
            assert all("procedure_inputs" not in descriptor for descriptor in original["descriptors"])
            attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task.task_id))
            reviewed = await complete_review(db, owner, task.task_id, expected_revision=task.task_revision,
                attempt_id=attempt.attempt_id)
            assert reviewed.task.status.value == "done"
        eligibility = await eligible_procedure_source(operator, created.task.task_id)
        assert eligibility["eligible"] is False
        assert eligibility["reason_code"] == "source_contract_review_required"
    finally:
        service.stop(); registry.stop(); await current.stop()
