"""Schema admission mechanics and physical-file nonexecution; no inference."""
import socket
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from config.settings import settings
from src.db.models import WorkBoardTask, WorkBoardStatus, WorkflowRunState
from src.native_tools.registry import ToolRegistry
from src.work_board.contracts import (
    GeneralTaskCreate, GeneralTaskInput, GeneralTaskPlanUpdate, PlanSpec, WorkBoardOwner,
)
from src.work_board.general_task import GeneralTaskService, digest
from src.work_board.general_task_schema import schema_accepts_output
from src.work_board.repository import BoardError
from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime, OWNER, SESSION, _goal


@pytest.fixture(autouse=True)
def no_external_contacts(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("external sockets forbidden in schema admission tests")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")


_OBJECT = {"type": "object", "properties": {"text": {"type": "string"}},
           "required": ["text"], "additionalProperties": False}

_EMPTY_PRODUCERS = [
    {"type": "string", "const": 1},
    {"type": "string", "enum": [1, False, None]},
    {"type": "number", "minimum": 2, "maximum": 1},
    {"type": "integer", "minimum": 0.1, "maximum": 0.9},
    {"type": "number", "minimum": 1, "exclusiveMaximum": 1},
    {"type": "string", "minLength": 2, "maxLength": 1},
    {"type": "object", "properties": {"text": {"type": "string", "const": 1}},
     "required": ["text"], "additionalProperties": False},
    {"type": "object", "required": ["missing"], "additionalProperties": False},
    {"type": "array", "minItems": 1, "items": {"type": "string", "enum": [1]}},
    {"type": "array", "minItems": 2, "maxItems": 1},
    {"type": "integer", "minimum": 1, "maximum": 4, "multipleOf": 2.5},
    {"type": "array", "minItems": 2, "uniqueItems": True, "items": {"enum": [1, 1.0]}},
    {"type": "array", "minItems": 3, "uniqueItems": True, "items": {"type": "boolean"}},
]


@pytest.mark.parametrize("source", _EMPTY_PRODUCERS)
def test_empty_producer_never_proves_exact_or_permissive_contract(source):
    assert not schema_accepts_output(source, source)
    assert not schema_accepts_output(source, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("source", _EMPTY_PRODUCERS)
async def test_empty_descriptor_rejected_by_full_admission_without_contact(source):
    from tests.test_general_task_contract import Registry, request
    registry = Registry()
    registry.entries = [registry.entries[0].model_copy(update={"output_schema": source})]
    value = request(registry)
    value = value.model_copy(update={
        "input": value.input.model_copy(update={"requested_output": source}),
        "plan": value.plan.model_copy(update={"steps": [value.plan.steps[0].model_copy(
            update={"output_contract": source})]}),
    })
    service = GeneralTaskService(registry)
    service.start()
    try:
        with pytest.raises(BoardError, match="registered tool contract"):
            await service.validate(WorkBoardOwner(principal_id="owner", session_id="session"), value)
        assert registry.calls == []
    finally:
        service.stop()


@pytest.mark.parametrize("schema", [
    {"type": "string", "enum": [1, "valid"]},
    {"type": "integer", "minimum": 0.1, "maximum": 1},
    {"type": "number", "minimum": 1, "maximum": 1},
    {"type": "array", "maxItems": 0, "items": False},
    {"type": "object", "properties": {"optional": False}, "additionalProperties": False},
    {"type": "integer", "minimum": 1, "maximum": 5, "multipleOf": 2.5},
    {"type": "array", "minItems": 2, "uniqueItems": True, "items": {"enum": [1, True]}},
])
def test_nonempty_exact_producers_remain_compatible(schema):
    assert schema_accepts_output(schema, schema)


@pytest.mark.parametrize("source,target,compatible", [
    (_OBJECT, _OBJECT, True),
    (_OBJECT, {"type": "object"}, True),
    (_OBJECT, {"type": "string"}, False),
    ({"type": "integer"}, {"type": "number"}, True),
    ({"type": "number"}, {"type": "integer"}, False),
    ({"type": "object"}, _OBJECT, False),
    ({**_OBJECT, "required": []}, _OBJECT, False),
    ({**_OBJECT, "additionalProperties": True}, _OBJECT, False),
    (_OBJECT, {"type": "object", "additionalProperties": False}, False),
    (_OBJECT, {**_OBJECT, "required": [], "additionalProperties": True}, True),
    ({"type": "object", "additionalProperties": {"type": "string"}},
     {"type": "object", "additionalProperties": {"type": "number"}}, False),
    ({"type": "object", "additionalProperties": False},
     {"type": "object", "properties": {"optional": {"type": "string"}}}, True),
    ({**_OBJECT, "properties": {"text": {"type": "number"}}}, _OBJECT, False),
    ({"type": "string", "maxLength": 3}, {"type": "string", "maxLength": 3}, True),
    ({"type": "string", "maxLength": 4}, {"type": "string", "maxLength": 3}, False),
    ({"type": "string"}, {"type": "string", "pattern": "^safe$"}, False),
    ({"type": "integer", "minimum": 0}, {"type": "integer", "minimum": 1}, False),
    ({"type": "string", "enum": ["abc", "ab"]}, {"type": "string", "maxLength": 3}, True),
    ({"type": "string", "enum": ["abc", "abcd"]}, {"type": "string", "maxLength": 3}, False),
    ({"const": {"text": "safe"}}, _OBJECT, True),
    ({"const": {"text": 1}}, _OBJECT, False),
    ({"type": "boolean", "const": True}, {"const": 1}, False),
    ({"type": "object", "allOf": [_OBJECT]}, {"type": "object"}, False),
    ({"type": "string"}, {"type": ["string", "null"]}, False),
    ({"type": "array", "items": {"type": "string"}}, {"type": "array"}, False),
    ({"type": "invalid"}, {"type": "invalid"}, False),
])
def test_conservative_compatibility(source, target, compatible):
    assert schema_accepts_output(source, target) is compatible


def test_exact_supported_and_unsupported_contracts_remain_available():
    schema = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert schema_accepts_output(schema, schema)
    # Object/property order is irrelevant; required-array order is conservatively
    # compared through its supported set semantics when the schemas differ.
    reordered = {"additionalProperties": False, "required": ["text"],
                 "properties": {"text": {"type": "string"}}, "type": "object"}
    assert schema_accepts_output(_OBJECT, reordered)


def native_request(registry, *, step_schema=None, requested_schema=None, key="schema-mismatch"):
    descriptors = registry.descriptors()
    descriptor = next(item for item in descriptors if item.tool_id == "write_file")
    return GeneralTaskCreate(goal_revision=1, idempotency_key=key, expected_plan_revision=1,
        accept=True,
        input=GeneralTaskInput(goal_ref="goal-1", intent="Write the bounded local result",
            requested_output=requested_schema or descriptor.output_schema,
            tool_set_digest=digest([item.model_dump(mode="json") for item in descriptors])),
        plan=PlanSpec(revision=1, steps=[{"step_id": "write", "tool_id": "write_file",
            "input": {"file_path": "schema-result.txt", "content": "physical bounded result"},
            "output_contract": step_schema or descriptor.output_schema}]))


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["step", "requested"])
async def test_native_write_mismatch_rejected_without_root_or_physical_effect(task_runtime, monkeypatch, mismatch):
    sessions, workspace = task_runtime
    owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)
    async with sessions() as db:
        db.add(_goal("goal-1", "Reject incompatible write before physical effects"))
    registry = ToolRegistry()
    registry.start()
    invoke = AsyncMock(wraps=registry.invoke)
    monkeypatch.setattr(registry, "invoke", invoke)
    service = GeneralTaskService(registry)
    service.start()
    bad = native_request(registry, **{mismatch + "_schema": {"type": "string"}})
    async with sessions() as db:
        with pytest.raises(BoardError, match="registered tool contract"):
            await service.create(db, owner, bad)
        assert list((await db.execute(select(WorkBoardTask))).scalars()) == []
        assert list((await db.execute(select(WorkflowRunState))).scalars()) == []
    from src.work_board.dispatcher import WorkBoardDispatcher
    result = await WorkBoardDispatcher(session_provider=sessions, general_tasks=service).run_pass()
    assert result["admitted"] == 0
    assert invoke.await_count == 0
    assert not (workspace / "schema-result.txt").exists()
    assert not list((workspace / "artifacts/work-board/general-tasks").glob("*.json"))
    service.stop()
    registry.stop()


@pytest.mark.asyncio
async def test_generated_mismatch_stays_editable_without_root_or_write(task_runtime, monkeypatch):
    sessions, workspace = task_runtime
    owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)
    async with sessions() as db:
        db.add(_goal("goal-1", "Retain rejected generated contract for correction"))
    registry = ToolRegistry()
    registry.start()
    invoke = AsyncMock(wraps=registry.invoke)
    monkeypatch.setattr(registry, "invoke", invoke)
    bad = native_request(registry, step_schema={"type": "string"})
    planner = type("Planner", (), {"propose": AsyncMock(return_value=bad.plan)})()
    service = GeneralTaskService(registry, planner=planner)
    service.start()
    generated = GeneralTaskCreate(goal_revision=bad.goal_revision,
        idempotency_key=bad.idempotency_key, input=bad.input)
    async with sessions() as db:
        task = (await service.create(db, owner, generated)).task
        assert task.status == WorkBoardStatus.triage
        plan = await service.plan(db, owner, task.task_id)
        assert plan["plan"] is None
        assert plan["proposal_error"] == "general_task_plan_invalid"
        assert list((await db.execute(select(WorkflowRunState))).scalars()) == []
    valid = native_request(registry)
    async with sessions() as db:
        edited = await service.update_plan(db, owner, task.task_id, GeneralTaskPlanUpdate(
            expected_revision=task.task_revision, expected_plan_revision=0,
            idempotency_key="repair-generated-contract", plan=valid.plan))
        repaired = await service.plan(db, owner, task.task_id)
        assert repaired["plan"] == valid.plan.model_dump(mode="json")
        assert repaired["proposal_error"] is None
        assert edited.status == WorkBoardStatus.triage
    assert invoke.await_count == 0
    assert not (workspace / "schema-result.txt").exists()
    service.stop()
    registry.stop()


@pytest.mark.asyncio
async def test_edit_and_preflight_reject_mismatch_preserving_original_plan(task_runtime):
    sessions, workspace = task_runtime
    owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)
    async with sessions() as db:
        db.add(_goal("goal-1", "Reject invalid edited and persisted output contracts"))
    registry = ToolRegistry()
    registry.start()
    service = GeneralTaskService(registry)
    service.start()
    valid = native_request(registry).model_copy(update={"accept": False})
    envelope = await service.validate(owner, valid)
    invalid_step = valid.plan.steps[0].model_copy(update={"output_contract": {"type": "string"}})
    invalid_plan = valid.plan.model_copy(update={"revision": 2, "steps": [invalid_step]})
    with pytest.raises(BoardError, match="registered tool schema"):
        service.recheck(envelope.model_copy(update={"plan": invalid_plan}))
    with pytest.raises(BoardError, match="registered tool schema"):
        service.recheck(envelope.model_copy(update={"task_input": envelope.task_input.model_copy(
            update={"requested_output": {"type": "string"}})}))
    async with sessions() as db:
        original = (await service.create(db, owner, valid)).task
        original_artifact = original.input_artifact_id
        revision = original.task_revision
    async with sessions() as db:
        with pytest.raises(BoardError, match="registered tool contract"):
            await service.update_plan(db, owner, original.task_id, GeneralTaskPlanUpdate(
                expected_revision=revision, expected_plan_revision=1,
                idempotency_key="reject-edit", plan=invalid_plan))
        current = await service.repository.get_task(db, owner, original.task_id)
        assert current.task_revision == revision
        assert current.input_artifact_id == original_artifact
        assert (await service.plan(db, owner, original.task_id))["plan"] == valid.plan.model_dump(mode="json")
        assert list((await db.execute(select(WorkflowRunState))).scalars()) == []
    assert not (workspace / "schema-result.txt").exists()
    service.stop()
    registry.stop()


@pytest.mark.asyncio
async def test_existing_permissive_object_contract_accepts_and_rechecks(task_runtime, monkeypatch):
    sessions, workspace = task_runtime
    owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)
    async with sessions() as db:
        db.add(_goal("goal-1", "Keep provable object superset contracts usable"))
    registry = ToolRegistry()
    registry.start()
    invoke = AsyncMock(wraps=registry.invoke)
    monkeypatch.setattr(registry, "invoke", invoke)
    service = GeneralTaskService(registry)
    service.start()
    accepted = native_request(registry, step_schema={"type": "object"},
        requested_schema={"type": "object"}, key="object-contract-positive")
    envelope = await service.validate(owner, accepted)
    service.recheck(envelope)
    async with sessions() as db:
        task = (await service.create(db, owner, accepted)).task
    assert invoke.await_count == 0
    assert not (workspace / "schema-result.txt").exists()
    async with sessions() as db:
        current = await service.repository.get_task(db, owner, task.task_id)
        assert current.status == WorkBoardStatus.todo
        roots = list((await db.execute(select(WorkflowRunState))).scalars())
        assert roots == []
    service.stop()
    registry.stop()
