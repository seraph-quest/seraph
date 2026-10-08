"""Provider-free mechanics for the closed general registered-tool contract."""
import socket
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from src.work_board.contracts import (
    GeneralTaskCreate, GeneralTaskInput, PlanSpec, TaskStrategyBinding,
    ToolDescriptor, WorkBoardOwner,
)
from src.work_board.general_task import GeneralTaskService, digest, resolve_input, validate_data, validate_schema, current_task_service
from src.work_board.repository import BoardError
from src.security.trust_contract import TrustPrincipal, PrincipalType


@pytest.fixture(autouse=True)
def no_provider_contacts(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("external sockets denied in general task acceptance")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setenv("OPENROUTER_API_KEY", "")


def descriptor():
    return ToolDescriptor(tool_id="fixture.read", version="1",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}},
                      "required": ["text"], "additionalProperties": False},
        output_schema={"type": "object", "properties": {"text": {"type": "string"}},
                       "required": ["text"], "additionalProperties": False},
        effects=["workspace_read"], permissions=["workspace_read"], deadline=1,
        verifier="json-schema.v1", policy_digest="a" * 64)


class Registry:
    def __init__(self):
        self.entries = [descriptor()]
        self.calls = []
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def descriptors(self):
        return self.entries

    async def invoke(self, descriptor, inputs, **kwargs):
        self.calls.append((descriptor, inputs, kwargs))
        return dict(inputs)


def request(registry=None, *, steps=None):
    registry = registry or Registry()
    schema = descriptor().output_schema
    return GeneralTaskCreate(goal_revision=1, idempotency_key="request-1",
        expected_plan_revision=1,
        input=GeneralTaskInput(goal_ref="goal-1", intent="Read and return text",
            requested_output=schema, tool_set_digest=digest([item.model_dump(mode="json") for item in registry.entries])),
        plan=PlanSpec(revision=1, steps=steps or [{"step_id": "read", "tool_id": "fixture.read",
            "input": {"text": "hello"}, "output_contract": schema}]))


def test_closed_roundtrip_and_utf8_bounds():
    model = request()
    assert GeneralTaskCreate.model_validate_json(model.model_dump_json()) == model
    with pytest.raises(ValidationError):
        GeneralTaskInput.model_validate({**model.input.model_dump(), "intent": "😀" * 3000})
    with pytest.raises(ValidationError):
        GeneralTaskCreate.model_validate({**model.model_dump(), "owner": "forged"})


def test_lifecycle_cleans_up_when_startup_fails_before_readiness():
    registry = Registry()
    dispatcher = type("Dispatcher", (), {"general_tasks": None})()
    with pytest.raises(RuntimeError, match="later startup failed"):
        with current_task_service(registry=registry, dispatcher=dispatcher, planner=object()) as service:
            assert registry.started and service.started
            raise RuntimeError("later startup failed")
    assert dispatcher.general_tasks is None
    assert not registry.started and not service.started


def test_cycles_unknown_dependencies_and_step_bound():
    schema = descriptor().output_schema
    step = {"step_id": "read", "tool_id": "fixture.read", "input": {"text": "a"},
            "output_contract": schema}
    with pytest.raises(ValidationError):
        PlanSpec(revision=1, steps=[{**step, "depends_on": ["read"]}])
    with pytest.raises(ValidationError):
        PlanSpec(revision=1, steps=[{**step, "depends_on": ["missing"]}])
    with pytest.raises(ValidationError):
        PlanSpec(revision=1, steps=[{**step, "step_id": str(index)} for index in range(17)])
    assert len(PlanSpec(revision=1, steps=[{**step, "step_id": str(index)} for index in range(16)]).steps) == 16


@pytest.mark.parametrize("value", [
    {"nested": {"approval_id": "forged"}}, {"secret_ref": "opaque"},
    {"items": [{}] * 65 + [{"owner": "forged"}]},
    {"expression": "import os"}, {"$dependency": {"step_id": "missing", "pointer": "/text"}},
])
def test_privilege_and_expression_data_fail_closed(value):
    with pytest.raises(ValueError):
        validate_data(value, dependencies=set())


def test_schema_references_never_retrieve_remote_data():
    with pytest.raises(ValueError):
        validate_schema({"$ref": "https://example.com/schema"}, {})
    with pytest.raises(ValueError):
        validate_schema({"$ref": "file:///etc/passwd"}, {})


def test_verified_dependency_pointer_is_data_only():
    value = {"text": {"$dependency": {"step_id": "first", "pointer": "/list/0/a~1b"}}}
    validate_data(value, dependencies={"first"})
    assert resolve_input(value, {"first": {"list": [{"a/b": "literal"}]}}) == {"text": "literal"}
    with pytest.raises(ValueError):
        resolve_input({"$dependency": {"step_id": "first", "pointer": "/list/00"}}, {"first": {"list": [1]}})


@pytest.mark.asyncio
async def test_validation_creates_no_execution_or_tool_contacts():
    registry = Registry()
    service = GeneralTaskService(registry)
    service.start()
    owner = WorkBoardOwner(principal_id="owner", session_id="session")
    envelope = await service.validate(owner, request(registry))
    assert envelope.strategy.status == "none"
    assert registry.calls == []
    registry.entries = []
    with pytest.raises(BoardError, match="Refresh"):
        await service.validate(owner, request())
    service.stop()
    with pytest.raises(BoardError, match="inactive"):
        service.snapshot()


@pytest.mark.asyncio
async def test_blocked_strategy_never_admits():
    registry = Registry()
    resolver = type("Resolver", (), {"resolve": lambda *args: TaskStrategyBinding(status="blocked", reason="revoked")})()
    service = GeneralTaskService(registry, strategy_resolver=resolver)
    service.start()
    with pytest.raises(BoardError, match="revoked"):
        await service.validate(WorkBoardOwner(principal_id="owner", session_id="session"), request(registry))
    assert registry.calls == []


@pytest.mark.asyncio
async def test_contract_change_and_unknown_prior_intent_never_retry():
    registry = Registry()
    service = GeneralTaskService(registry)
    service.start()
    envelope = await service.validate(WorkBoardOwner(principal_id="owner", session_id="session"), request(registry))
    registry.entries = [descriptor().model_copy(update={"version": "2"})]
    with pytest.raises(BoardError, match="Restore"):
        service.recheck(envelope)
    registry.entries = [descriptor()]
    jobs = type("Jobs", (), {"get_job": AsyncMock(return_value={"checkpoints": [{"checkpoint_id": "general:step:read"}]})})()
    with pytest.raises(BoardError, match="reconciliation"):
        await service.execute(jobs, job_id="job", owner="worker", fence=1, envelope=envelope,
            principal=TrustPrincipal(principal_id="owner", principal_type=PrincipalType.OPERATOR,
                session_id="session", operator_session_id="session"))
    assert registry.calls == []
