"""Actual stock IPC/native transcript ownership; scripted model boundary only."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from smolagents import ToolCallingAgent
from smolagents.models import Model, ChatMessage, ChatMessageToolCall, ChatMessageToolCallFunction
from sqlalchemy import event, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from config.settings import settings
from src.auth.service import create_session
from src.db.engine import get_session, override_session_factory
from src.db.models import Goal, Message, OperatorSession, RuntimeCompositionState, Session, WorkBoardTask, WorkflowRunState
from src.runtime_plugins.bridge import CordisHost
from src.runtime_plugins.dispatch import NativeServiceDispatcher
from tests.test_runtime_composition_ownership import composition_db


NODE = Path('/home/pawel/repos/seraph/.agent-worktrees/986-a1-host/.agent-evidence/986/a1-upstream/node-v24.13.1-linux-x64/bin/node')


def _scripted_accounted_result(result, *, cost="0"):
    """Actual existing owners; only their final model transport is scripted."""
    from src.agent.controlled_origin import _current_execution
    execution = _current_execution.get()
    if execution is None:
        return result
    from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
    request = _scripted_accounting_request(execution)
    payload = {"id": "owned-provider-" + request.operation_id, "usage": {"cost": cost}}
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    return broker.execute_sync(request, lambda: (result, payload))[0]


def _scripted_accounting_request(execution):
    from uuid import uuid4
    from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionRequest, RemoteInferencePriority
    principal = execution.admission.principal
    operation_id = "owned-scripted-" + uuid4().hex
    return RemoteInferenceAdmissionRequest(operation_id=operation_id, job_id=operation_id,
        owner_id=principal.principal_id, session_id=principal.operator_session_id,
        priority=RemoteInferencePriority.INTERACTIVE_CHAT,
        deadline_at=execution.admission.deadline_at.timestamp(), runtime_path="chat_agent",
        data_digest="a" * 64, owner_budget_microusd=1000)


class ScriptedModel(Model):
    """Owned deterministic model transport, no inference/provider request."""
    def __init__(self):
        super().__init__(model_id="owned-scripted-transport")
        self.calls = 0

    def generate(self, messages, **kwargs):
        self.calls += 1
        return _scripted_accounted_result(ChatMessage(role="assistant", content="", tool_calls=[ChatMessageToolCall(
            id="owned-final", type="function", function=ChatMessageToolCallFunction(
                name="final_answer", arguments={"answer": "Scripted native reply"}))]), cost=getattr(self, "cost", "0"))


class ExhaustedScriptedModel(Model):
    def __init__(self, max_calls=2, following_cost="0.0000021"):
        super().__init__(model_id="owned-two-operation-script")
        self.calls, self.max_calls = 0, max_calls
        self.following_cost = following_cost

    def generate(self, messages, **kwargs):
        self.calls += 1
        assert self.calls <= self.max_calls
        message = ChatMessage(role="assistant", content="Scripted native reply", tool_calls=[])
        return _scripted_accounted_result(message, cost="0" if self.calls == 1 else self.following_cost)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["direct_turn", "generic_turn"])
async def test_actual_native_claim_empty_coroutine_cannot_publish(native_transport, route):
    from src.agent.session import session_manager
    from src.agent.turn_execution import NativeTurnAdmission, NativeTurnBlocked, claim_native_turn
    from src.api.chat import _bind_chat_principal, build_chat_ingress_envelope, chat_ingress_metadata, persist_turn_output
    host, _, operator, _, _ = native_transport
    conversation = await session_manager.get_or_create(owner_principal_id=operator.principal.principal_id)
    principal = _bind_chat_principal(conversation.id, operator=operator)
    ingress = build_chat_ingress_envelope(message="No model call", session_id=conversation.id,
        principal=principal, operator_session_id=operator.session_id, transport="rest",
        client_message_id="owned-empty-callback-" + route)
    admission = NativeTurnAdmission.capture(ingress, principal=principal,
        reviewed_composition=host.reviewed, native_route=route)
    _, _, job = await session_manager.reserve_native_turn_message(conversation.id, "No model call",
        message_id=ingress.message_id, metadata_json=chat_ingress_metadata(ingress), admission=admission)
    execution = await claim_native_turn(admission, host, job)
    model = ScriptedModel()
    if route == "generic_turn":
        execution.prepare_agent(ToolCallingAgent(tools=[], model=model, max_steps=1, verbosity_level=0))
    async def unowned_callback():
        return "Unowned reply"
    assert await execution.execute(unowned_callback()) == "Unowned reply"
    assert execution.worker.done() and not execution.worker.cancelled()
    with pytest.raises(NativeTurnBlocked, match="native_turn_family_callback_completion_unproven"):
        await persist_turn_output(execution, conversation.id, "assistant", "Unowned reply",
            message_id="owned-empty-output-" + route)
    async with get_session() as db:
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == admission.job_id))
        assert run.status == "running" and run.attempt_count == 1
        assert next(item["payload"] for item in json.loads(run.checkpoint_receipts_json)
            if item["checkpoint_id"] == "conversation:operation-family")["operations"] == []
        assert await db.scalar(select(Message).where(Message.role == "assistant")) is None
    assert model.calls == 0 and host.admitting


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing_accounting", "foreign_model", "replaced_callback", "copied_witness"])
async def test_actual_stock_native_original_call_identity_denies_output(native_transport, monkeypatch, mode):
    from copy import copy
    from src.app import create_app
    from src.db.models import InferenceCostReservation
    from src.workflows.job_runtime import durable_job_repository
    contacts = []
    class ForeignModel(Model):
        def generate(self, messages, **kwargs):
            return _scripted_accounted_result(ChatMessage(role="assistant", content="foreign", tool_calls=[]))
    foreign = ForeignModel(model_id="owned-foreign-model")
    class IdentityModel(ScriptedModel):
        def generate(self, messages, **kwargs):
            if mode == "foreign_model":
                self.calls += 1
                return foreign.generate(messages, **kwargs)
            if mode == "missing_accounting":
                self.calls += 1
                return ChatMessage(role="assistant", content="", tool_calls=[ChatMessageToolCall(
                    id="owned-unaccounted", type="function", function=ChatMessageToolCallFunction(
                        name="final_answer", arguments={"answer": "Forbidden unaccounted reply"}))])
            return super().generate(messages, **kwargs)
    model = IdentityModel()
    agent = ToolCallingAgent(tools=[], model=model, max_steps=1, verbosity_level=0)
    if mode == "replaced_callback":
        original_run = agent.run
        agent.run = lambda *args, **kwargs: original_run(*args, **kwargs)
    if mode == "copied_witness":
        original_contact = durable_job_repository.contact_inference_provider
        async def substitute_private_witness(*args, **kwargs):
            witness = kwargs["native_turn_operation_witness"]
            contacts.append(witness.operation_id)
            kwargs["native_turn_operation_witness"] = copy(witness)
            return await original_contact(*args, **kwargs)
        monkeypatch.setattr(durable_job_repository, "contact_inference_provider", substitute_private_witness)
    monkeypatch.setattr("src.api.chat.create_onboarding_agent", lambda message: agent)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: native_transport[1]}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        response = await client.post("/api/chat", json={"message": "Inspect this short answer",
            "message_id": "owned-family-identity-" + mode})
        assert response.status_code == 503, response.text
    async with get_session() as db:
        assert await db.scalar(select(Message).where(Message.role == "assistant")) is None
        turn = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind == "conversation_turn_v1"))
        assert turn.status != "succeeded" and turn.attempt_count == 1
        family = next(item["payload"] for item in json.loads(turn.checkpoint_receipts_json)
            if item["checkpoint_id"] == "conversation:operation-family")
        assert family["operations"] == []
        rows = list((await db.execute(select(InferenceCostReservation))).scalars())
        if mode == "copied_witness":
            assert len(rows) == 1 and contacts == [rows[0].operation_id]
            assert rows[0].state != "settled" and rows[0].contact_started_at is None
        else:
            assert rows == []
    assert model.calls == (0 if mode == "replaced_callback" else 1)
    assert native_transport[0].admitting


@pytest.mark.asyncio
@pytest.mark.parametrize("steps", [True, 0, 65])
async def test_actual_stock_native_generic_step_budget_denies_before_contact(native_transport, monkeypatch, steps):
    from src.app import create_app
    from src.db.models import InferenceCostReservation
    model = ScriptedModel()
    agent = ToolCallingAgent(tools=[], model=model, max_steps=1, verbosity_level=0)
    agent.max_steps = steps
    monkeypatch.setattr("src.api.chat.create_onboarding_agent", lambda message: agent)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: native_transport[1]}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        response = await client.post("/api/chat", json={"message": "Inspect this short answer", "message_id": "owned-invalid-native-steps"})
        assert response.status_code == 503 and response.json()["detail"]["code"] == "native_turn_family_sdk_budget_unsupported", response.text
    assert model.calls == 0 and native_transport[4] == []
    async with get_session() as db:
        assert await db.scalar(select(InferenceCostReservation)) is None
        assert await db.scalar(select(Message).where(Message.role == "assistant")) is None


@pytest.mark.asyncio
async def test_actual_stock_native_foreign_registry_denies_before_first_model(native_transport, monkeypatch):
    from smolagents import Tool
    from src.app import create_app
    from src.db.models import InferenceCostReservation
    effects = []
    class ForeignExecutable(Tool):
        name, description, inputs, output_type = "foreign_executable", "Unwrapped unsupported fixture", {}, "string"
        def forward(self):
            effects.append(True)
            return "forbidden"
    model = ScriptedModel()
    agent = ToolCallingAgent(tools=[ForeignExecutable()], model=model, max_steps=1, verbosity_level=0)
    monkeypatch.setattr("src.api.chat.create_onboarding_agent", lambda message: agent)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: native_transport[1]}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        response = await client.post("/api/chat", json={"message": "Inspect this short answer", "message_id": "owned-foreign-registry"})
        assert response.status_code == 503 and response.json()["detail"]["code"] == "native_turn_family_tool_registry_unsupported", response.text
    assert model.calls == 0 and effects == [] and native_transport[4] == []
    async with get_session() as db:
        assert await db.scalar(select(InferenceCostReservation)) is None
        assert await db.scalar(select(Message).where(Message.role == "assistant")) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["audit", "authority"])
async def test_actual_stock_native_wrapped_effect_denies_before_tool_contact(native_transport, monkeypatch, wrapper):
    from smolagents import Tool
    from src.app import create_app
    from src.tools.audit import AuditedTool
    from src.tools.approval import AuthorityTool
    from src.db.models import InferenceCostReservation
    effects = []
    class ForeignEffect(Tool):
        name, description, output_type = "owned_foreign_effect", "Unsupported actual effect boundary", "string"
        inputs = {"question": {"type": "string", "description": "Question"},
            "reason": {"type": "string", "description": "Reason"},
            "options": {"type": "string", "description": "Options"}}
        def forward(self, question: str, reason: str, options: str) -> str:
            effects.append(True)
            return "forbidden"
    producer = AuditedTool(ForeignEffect()) if wrapper == "audit" else AuthorityTool(ForeignEffect())
    model = ControlledScriptedModel(producer.name)
    agent = ToolCallingAgent(tools=[producer], model=model, max_steps=2, verbosity_level=0)
    monkeypatch.setattr("src.api.chat.create_onboarding_agent", lambda message: agent)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: native_transport[1]}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        response = await client.post("/api/chat", json={"message": "Inspect this short answer",
            "message_id": "owned-unsupported-wrapper-" + wrapper})
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] == "native_turn_family_executable_tool_unsupported"
    assert effects == [] and model.calls == 1
    async with get_session() as db:
        row = await db.scalar(select(InferenceCostReservation))
        assert row.state == "settled" and row.actual_cost_microusd == 0
        turn = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind == "conversation_turn_v1"))
        assert turn.status != "succeeded"
        assert await db.scalar(select(Message).where(Message.role == "assistant")) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["none", "cancel", "expired_lease"])
async def test_actual_stock_native_unknown_cost_keeps_owner_debt_and_denies_output(native_transport, monkeypatch, recovery):
    from src.app import create_app
    from src.db.models import InferenceCostReservation
    model = ScriptedModel()
    model.cost = None
    agent = ToolCallingAgent(tools=[], model=model, max_steps=1, verbosity_level=0)
    monkeypatch.setattr("src.api.chat.create_onboarding_agent", lambda message: agent)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: native_transport[1]}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        response = await client.post("/api/chat", json={"message": "Inspect this short answer", "message_id": "owned-unknown-family-cost"})
        assert response.status_code == 503, response.text
    async with get_session() as db:
        row = await db.scalar(select(InferenceCostReservation))
        assert row.state == "unknown" and row.actual_cost_microusd is None
        owner = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == row.job_id))
        assert owner.status == "cost_liability" and owner.attempt_count == 1
        turn = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind == "conversation_turn_v1"))
        assert turn.status != "succeeded"
        family = next(item["payload"] for item in json.loads(turn.checkpoint_receipts_json)
            if item["checkpoint_id"] == "conversation:operation-family")
        assert len(family["operations"]) == 1 and family["operations"][0]["job_id"] == owner.run_identity
        assert await db.scalar(select(Message).where(Message.role == "assistant")) is None
        original_family = family
        turn_id, revision, lease_owner, fence, lease_until = turn.run_identity, turn.revision, turn.lease_owner, turn.fencing_token, turn.lease_expires_at
        owner_id, operation_id = owner.run_identity, row.operation_id
    if recovery != "none":
        from datetime import timedelta, timezone
        from src.workflows.job_runtime import durable_job_repository, DurableJobTransitionError
        if recovery == "cancel":
            recovered = await durable_job_repository.cancel_job(turn_id, owner=lease_owner,
                fencing_token=fence, expected_revision=revision)
        else:
            observed = lease_until.replace(tzinfo=timezone.utc) if lease_until.tzinfo is None else lease_until
            recovered = await durable_job_repository.recover_stale_job(turn_id, now=observed + timedelta(seconds=1))
        assert recovered["status"] == "cost_liability"
        async with get_session() as db:
            turn = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == turn_id))
            assert turn.failure_reason == "native_turn_family_cost_liability"
            assert next(item["payload"] for item in json.loads(turn.checkpoint_receipts_json)
                if item["checkpoint_id"] == "conversation:operation-family") == original_family
            assert (await db.get(InferenceCostReservation, operation_id)).state == "unknown"
            owner = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == owner_id))
            assert owner.status == "cost_liability"
            assert await db.scalar(select(Message).where(Message.role == "assistant")) is None
        with pytest.raises(DurableJobTransitionError):
            await durable_job_repository.retry_job(turn_id, owner_kind="user",
                owner_principal_id=native_transport[2].principal.principal_id,
                reconciled=True, reconciliation_receipt={"effect_id": "job-failure:" + turn_id,
                    "effect_type": "job_failure", "target_path": "job:" + turn_id,
                    "status": "read_back", "outcome": "no_external_effect"})
    assert model.calls == 1 and native_transport[0].admitting


@pytest.mark.asyncio
@pytest.mark.parametrize("steps", [1, 64])
async def test_actual_stock_sdk_exhaustion_collects_original_owner_operations(native_transport, monkeypatch, steps):
    from src.app import create_app
    from src.db.models import InferenceCostReservation
    if steps == 64:
        # The bound proof includes 65 full existing owner transactions and
        # closure readbacks. Capture the existing configured deadline before
        # admission; every original lease/policy/deadline check remains active.
        monkeypatch.setattr(settings, "agent_chat_timeout", 600)
    model = ExhaustedScriptedModel(max_calls=steps + 1, following_cost="0" if steps == 64 else "0.0000021")
    agent = ToolCallingAgent(tools=[], model=model, max_steps=steps, verbosity_level=0)
    monkeypatch.setattr("src.api.chat.create_onboarding_agent", lambda message: agent)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: native_transport[1]}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        response = await client.post("/api/chat", json={"message": "Inspect this short answer", "message_id": "owned-two-operation-input"})
        assert response.status_code == 200 and response.json()["response"] == "Scripted native reply", response.text
    async with get_session() as db:
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind == "conversation_turn_v1"))
        family = next(item["payload"] for item in json.loads(run.checkpoint_receipts_json)
            if item["checkpoint_id"] == "conversation:operation-family")
        assert run.status == "succeeded" and family["sdk_steps"] == steps and family["max_inference_operations"] == steps + 1
        assert len(family["operations"]) == steps + 1 and model.calls == steps + 1
        assert len({item["operation_id"] for item in family["operations"]}) == steps + 1
        assert len({item["job_id"] for item in family["operations"]}) == steps + 1
        costs = []
        for captured in family["operations"]:
            owner_row = await db.get(InferenceCostReservation, captured["operation_id"])
            owner_job = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == captured["job_id"]))
            assert owner_row.state == "settled" and owner_job.status == "succeeded"
            assert owner_job.composition_binding_json is None
            assert owner_job.run_identity != run.run_identity and owner_row.job_id == owner_job.run_identity
            assert owner_row.job_fencing_token == owner_job.fencing_token == captured["fencing_token"]
            assert owner_job.attempt_count == captured["attempt_count"] == 1
            costs.append(owner_row.actual_cost_microusd)
        assert costs == [0] + [0 if steps == 64 else 3] * steps


@asynccontextmanager
async def _stock_transport(monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "isolated-native-transport")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "agent_chat_timeout", 120)
    token, operator = await create_session()
    async with get_session() as db:
        composed = await db.scalar(select(RuntimeCompositionState.runtime_domain)) is not None
    if composed:
        from tests.test_inference_accounting import setup_configuration
        from src.workflows.job_runtime import durable_job_repository
        setup_configuration()
        await durable_job_repository.configure_inference_accounting(1000)
    host = CordisHost(node_path=NODE, service_dispatch=NativeServiceDispatcher())
    monkeypatch.setattr("src.runtime_plugins.bridge.cordis_host", host)
    monkeypatch.setattr("src.api.chat.direct_local_chat_route_error", lambda **kwargs: _no_route_error())
    monkeypatch.setattr("src.api.ws.direct_local_chat_route_error", lambda **kwargs: _no_route_error())
    model = ScriptedModel()
    agent = ToolCallingAgent(tools=[], model=model, max_steps=1, verbosity_level=0)
    monkeypatch.setattr("src.api.chat.create_onboarding_agent", lambda message: agent)
    monkeypatch.setattr("src.api.ws.create_onboarding_agent", lambda message: agent)
    direct_calls = []
    def scripted_completion(**kwargs):
        direct_calls.append(kwargs)
        return _scripted_accounted_result({"choices": [{"message": {"content": "Scripted native reply"}}]})
    monkeypatch.setattr("src.agent.direct_chat.completion_with_fallback_sync", scripted_completion)
    async def scripted_stream(message=None, **kwargs):
        from src.agent.controlled_origin import _current_execution
        from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
        execution = _current_execution.get()
        if execution is None:
            yield "Scripted "
            yield "native reply"
            return
        request = _scripted_accounting_request(execution)
        async def final_transport():
            yield {"delta": "Scripted "}
            yield {"delta": "native reply", "id": "owned-stream-" + request.operation_id, "usage": {"cost": "0"}}
        broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
        async for item in broker.stream(request, final_transport):
            yield item["delta"]
    monkeypatch.setattr("src.agent.direct_chat._uses_openrouter_profile", lambda runtime_path: True)
    monkeypatch.setattr("src.agent.direct_chat.stream_completion_with_fallback", scripted_stream)
    await host.start()
    assert host.admitting and len((await host.refresh_status())["plugins"]) == 15
    from src import app as app_module
    from src.agent.native_turn_controls import NativeTurnResourceOwner
    original_create_app = app_module.create_app
    app_owners = []
    def create_owned_app(*args, **kwargs):
        app = original_create_app(*args, **kwargs)
        owner = NativeTurnResourceOwner()
        app.state.native_turn_resources = owner
        app_owners.append(owner)
        return app
    monkeypatch.setattr(app_module, "create_app", create_owned_app)
    try:
        yield host, token, operator, model, direct_calls
    finally:
        for owner in app_owners:
            await owner.shutdown()
        await host.stop()
        cleanup = host.snapshot()["cleanup"]
        assert cleanup["state"] == "clean"
        assert cleanup["process_reaped"] is True and cleanup["resources_remaining"] == 0
        assert cleanup["cordis_disposal"] == "confirmed"


@pytest_asyncio.fixture
async def native_transport(composition_db, monkeypatch):
    async with _stock_transport(monkeypatch) as transport:
        yield transport


@pytest_asyncio.fixture
async def fresh_native_transport(tmp_path, monkeypatch):
    root = tmp_path / "fresh-cpu"
    root.mkdir()
    monkeypatch.setattr(settings, "workspace_dir", str(root))
    monkeypatch.setenv("SERAPH_WORKSPACE_LIFECYCLE_PATH", str(tmp_path / "fresh-deployment"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{root / 'seraph.db'}")
    @event.listens_for(engine.sync_engine, "connect")
    def foreign_keys(connection, record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    try:
        with override_session_factory(factory):
            async with _stock_transport(monkeypatch) as transport:
                yield transport
    finally:
        await engine.dispose()


async def _no_route_error():
    return None


async def assert_native_result(operator, data, branch):
    async with get_session() as db:
        messages = list((await db.execute(select(Message).where(
            Message.session_id == data["session_id"]).order_by(Message.created_at))).scalars())
        assert [message.role for message in messages] == ["user", "assistant"]
        job = await db.scalar(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == "conversation-turn:" + messages[0].id))
        assert job.status == "succeeded" and job.attempt_count == 1 and job.max_attempts == 1
        assert json.loads(job.composition_binding_json)["native_branch"] == branch + "_turn"
        assert job.operator_session_id == operator.session_id == job.session_id
        assert job.conversation_id == messages[0].session_id != operator.session_id
        assert job.lease_owner is None and job.lease_expires_at is None
        assert messages[1].id == data["message_id"]
        receipts = json.loads(job.checkpoint_receipts_json)
        output = next(item for item in receipts if item["checkpoint_id"] == "conversation:assistant-message")
        assert output["safe"] is True and output["payload"] == {
            "schema_version": 1, "message_ref": messages[1].id,
            "input_message_ref": messages[0].id, "no_learning": True}
        assert sum(item["checkpoint_id"].startswith("runtime-service-invocation") for item in receipts) == 1
        return messages[0].id, job.run_fingerprint


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["direct", "generic"])
async def test_real_stock_host_rest_claim_loop_output_and_duplicate(native_transport, branch):
    from src.app import create_app
    host, token, operator, model, direct_calls = native_transport
    body = {"message": "Hello" if branch == "direct" else "Inspect this short answer",
        "message_id": "owned-rest-" + branch}
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: token}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        response = await client.post("/api/chat", json=body)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["response"] == "Scripted native reply"
        _, fingerprint = await assert_native_result(operator, data, branch)
        body["session_id"] = data["session_id"]
        replay = await client.post("/api/chat", json=body)
        assert replay.status_code == 409 and replay.json()["detail"]["code"] == "chat_message_duplicate"
        _, repeated = await assert_native_result(operator, data, branch)
        assert fingerprint == repeated
    assert len(direct_calls) == (1 if branch == "direct" else 0)
    assert model.calls == (1 if branch == "generic" else 0)
    assert host.admitting


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["direct", "generic"])
async def test_real_stock_host_asgi_ws_claim_frames_and_output(native_transport, branch):
    from src.app import create_app
    host, token, operator, model, direct_calls = native_transport
    incoming = asyncio.Queue()
    frames = []
    final = asyncio.get_running_loop().create_future()
    async def send(message):
        if message["type"] == "websocket.send":
            value = json.loads(message["text"])
            frames.append(value)
            if value["type"] in {"final", "error"} and not final.done():
                final.set_result(value)
    scope = {"type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws",
        "path": "/ws/chat", "raw_path": b"/ws/chat", "query_string": b"",
        "root_path": "", "server": ("127.0.0.1", 8004), "client": ("127.0.0.1", 12345),
        "headers": [(b"host", b"127.0.0.1:8004"), (b"origin", b"http://127.0.0.1:3001"),
            (b"cookie", (settings.operator_auth_cookie_name + "=" + token).encode())], "subprotocols": []}
    await incoming.put({"type": "websocket.connect"})
    await incoming.put({"type": "websocket.receive", "text": json.dumps({"type": "message",
        "message": "Hello" if branch == "direct" else "Inspect this short answer",
        "message_id": "owned-ws-" + branch})})
    task = asyncio.create_task(create_app()(scope, incoming.get, send))
    try:
        result = await asyncio.wait_for(asyncio.shield(final), timeout=30)
        assert result["type"] == "final", frames
        assert result["content"] == "Scripted native reply"
        assert any(frame["type"] == "status" for frame in frames)
        if branch == "direct":
            assert "".join(frame["content"] for frame in frames if frame["type"] == "delta") == result["content"]
        assert [frame["seq"] for frame in frames] == sorted({frame["seq"] for frame in frames})
        await assert_native_result(operator, result, branch)
    finally:
        await incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, timeout=5)
    assert model.calls == (1 if branch == "generic" else 0)
    assert host.admitting


@pytest.mark.asyncio
async def test_real_stock_native_rest_cancel_retains_pending_claim_and_fences_output(native_transport, monkeypatch):
    from threading import Event
    from src.app import create_app
    from src.workflows.job_runtime import durable_job_repository as jobs
    host, token, operator, _, _ = native_transport
    started, release, completed = Event(), Event(), Event()
    def held_completion(**kwargs):
        started.set()
        try:
            assert release.wait(10)
            return {"choices": [{"message": {"content": "Late callback reply"}}]}
        finally:
            completed.set()
    monkeypatch.setattr("src.agent.direct_chat.completion_with_fallback_sync", held_completion)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: token}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        task = asyncio.create_task(client.post("/api/chat", json={"message": "Hello",
            "message_id": "owned-cancel-native-turn"}))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with get_session() as db:
                run = await db.scalar(select(WorkflowRunState).where(
                    WorkflowRunState.job_kind == "conversation_turn_v1"))
                job_id = run.run_identity
                original = (run.attempt_count, run.fencing_token, run.deadline_at, run.lease_owner)
                assert run.status == "running" and run.attempt_count == 1
                assert not completed.is_set()
            pending = await jobs.get_job(job_id)
            assert pending["native_turn_execution"] == {
                "phase": "possibly_started", "physical_completion": "unproven", "replay": "denied"}
        finally:
            release.set()
            assert await asyncio.to_thread(completed.wait, 5)
            if not task.done():
                await task
        async with get_session() as db:
            run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))
            assert (run.attempt_count, run.fencing_token, run.deadline_at, run.lease_owner) == original
            assert run.status == "running"
            messages = list((await db.execute(select(Message).where(
                Message.session_id == run.conversation_id))).scalars())
            assert [message.role for message in messages] == ["user"]
            assert not any(item["checkpoint_id"] == "conversation:assistant-message"
                for item in json.loads(run.checkpoint_receipts_json))
    assert host.admitting


@pytest.mark.asyncio
async def test_real_stock_host_restart_rejects_original_turn_output(native_transport, monkeypatch):
    from threading import Event
    from src.app import create_app
    host, token, operator, _, _ = native_transport
    started, release, completed = Event(), Event(), Event()
    def held_completion(**kwargs):
        started.set()
        try:
            assert release.wait(10)
            return {"choices": [{"message": {"content": "Reply from original callback"}}]}
        finally:
            completed.set()
    monkeypatch.setattr("src.agent.direct_chat.completion_with_fallback_sync", held_completion)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: token}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        task = asyncio.create_task(client.post("/api/chat", json={"message": "Hello",
            "message_id": "owned-restart-native-turn"}))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            old_boot = host.boot_nonce
            await host.stop()
            assert host.snapshot()["cleanup"]["process_reaped"] is True
            await host.start()
            assert host.admitting and host.boot_nonce != old_boot
            release.set()
            response = await task
            assert response.status_code == 503
            assert response.json()["detail"]["code"] == "native_turn_output_authority_changed"
        finally:
            release.set()
            assert await asyncio.to_thread(completed.wait, 5)
            if not task.done():
                await task
    async with get_session() as db:
        run = await db.scalar(select(WorkflowRunState).where(
            WorkflowRunState.job_kind == "conversation_turn_v1"))
        assert run.status == "running" and run.attempt_count == 1
        messages = list((await db.execute(select(Message).where(
            Message.session_id == run.conversation_id))).scalars())
        assert [message.role for message in messages] == ["user"]
        assert not any(item["checkpoint_id"] == "conversation:assistant-message"
            for item in json.loads(run.checkpoint_receipts_json))
        claim = next(item for item in json.loads(run.checkpoint_receipts_json)
            if item["checkpoint_id"].startswith("runtime-service-invocation"))
        assert claim["payload"]["host_boot_nonce"] == old_boot


@pytest.mark.asyncio
async def test_real_stock_turn_original_root_revocation_rejects_late_output(native_transport, monkeypatch):
    from threading import Event
    from src.app import create_app
    from src.auth.service import revoke_session
    host, token, operator, _, _ = native_transport
    started, release, completed = Event(), Event(), Event()
    def held_completion(**kwargs):
        started.set()
        try:
            assert release.wait(10)
            return {"choices": [{"message": {"content": "Revoked original callback reply"}}]}
        finally:
            completed.set()
    monkeypatch.setattr("src.agent.direct_chat.completion_with_fallback_sync", held_completion)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: token}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        task = asyncio.create_task(client.post("/api/chat", json={"message": "Hello",
            "message_id": "owned-revoked-native-turn"}))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            await revoke_session(operator.session_id)
            release.set()
            response = await task
            assert response.status_code == 401
        finally:
            release.set()
            assert await asyncio.to_thread(completed.wait, 5)
            if not task.done():
                await task
    async with get_session() as db:
        run = await db.scalar(select(WorkflowRunState).where(
            WorkflowRunState.job_kind == "conversation_turn_v1"))
        assert run.status == "running" and run.attempt_count == 1
        assert run.operator_session_id == operator.session_id
        messages = list((await db.execute(select(Message).where(
            Message.session_id == run.conversation_id))).scalars())
        assert [message.role for message in messages] == ["user"]
        assert not any(item["checkpoint_id"] == "conversation:assistant-message"
            for item in json.loads(run.checkpoint_receipts_json))
    assert host.admitting


@pytest.mark.asyncio
async def test_oversize_utf8_plain_turn_keeps_original_legacy_ingress_with_healthy_host(native_transport, monkeypatch):
    from unittest.mock import AsyncMock
    from src.app import create_app
    host, token, operator, model, direct_calls = native_transport
    content = "界" * 25000
    assert len(content) < 50000 and len(content.encode()) > 65536
    # Existing legacy consolidation is outside this ingress/IPC proof.
    monkeypatch.setattr("src.memory.flush.flush_session_memory", AsyncMock())
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: token}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        response = await client.post("/api/chat", json={"message": content,
            "message_id": "owned-unsupported-native-size"})
        assert response.status_code == 200, response.text
        data = response.json()
    async with get_session() as db:
        messages = list((await db.execute(select(Message).where(
            Message.session_id == data["session_id"]).order_by(Message.created_at))).scalars())
        assert [message.role for message in messages] == ["user", "assistant"]
        assert messages[0].content == content and messages[1].id == data["message_id"]
        assert await db.scalar(select(WorkflowRunState).where(
            WorkflowRunState.job_kind == "conversation_turn_v1")) is None
    assert host.admitting and len(direct_calls) == 1 and model.calls == 0


@pytest.mark.asyncio
async def test_stopped_optional_stock_host_keeps_core_and_legacy_plain_turn_usable(native_transport, monkeypatch):
    from unittest.mock import AsyncMock
    from src.app import create_app
    host, token, operator, model, direct_calls = native_transport
    await host.stop()
    assert host.snapshot()["cleanup"]["process_reaped"] is True
    monkeypatch.setattr("src.app.cordis_host", host)
    monkeypatch.setattr("src.memory.flush.flush_session_memory", AsyncMock())
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: token}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        assert (await client.get("/health")).status_code == 200
        status = await client.get("/api/runtime/status")
        assert status.status_code == 200
        assert status.json()["cordis_runtime"]["state"] == "stopped"
        response = await client.post("/api/chat", json={"message": "Hello",
            "message_id": "owned-stopped-host-legacy-turn"})
        assert response.status_code == 200, response.text
        data = response.json()
    async with get_session() as db:
        messages = list((await db.execute(select(Message).where(
            Message.session_id == data["session_id"]))).scalars())
        assert sorted(message.role for message in messages) == ["assistant", "user"]
        assert await db.scalar(select(WorkflowRunState).where(
            WorkflowRunState.job_kind == "conversation_turn_v1")) is None
    assert not host.admitting and len(direct_calls) == 1 and model.calls == 0


@pytest.mark.asyncio
async def test_actual_stock_fresh_cpu_absent_inventory_uses_unbound_legacy(fresh_native_transport, monkeypatch):
    from unittest.mock import AsyncMock
    from src.app import create_app
    host, token, _, model, direct_calls = fresh_native_transport
    monkeypatch.setattr("src.memory.flush.flush_session_memory", AsyncMock())
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: token}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        response = await client.post("/api/chat", json={"message": "Hello",
            "message_id": "owned-fresh-cpu-legacy"})
        assert response.status_code == 200, response.text
        data = response.json()
    async with get_session() as db:
        messages = list((await db.execute(select(Message).where(Message.session_id == data["session_id"]))).scalars())
        assert sorted(message.role for message in messages) == ["assistant", "user"]
        assert (await db.execute(select(RuntimeCompositionState))).scalars().all() == []
        assert await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind == "conversation_turn_v1")) is None
    assert host.admitting and len(direct_calls) == 1 and model.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["partial", "stale", "empty_retained"])
@pytest.mark.parametrize("task_linked", [False, True])
async def test_actual_stock_damaged_inventory_denies_before_message_or_contact(native_transport, composition_db, damage, task_linked):
    from src.app import create_app
    from src.agent.session import session_manager
    from src.workspace.production import read_lifecycle_receipt, read_accounting_checkpoint
    host, token, operator, model, direct_calls = native_transport
    conversation = await session_manager.get_or_create(owner_principal_id=operator.principal.principal_id)
    if task_linked:
        await _link_existing_task(conversation.id, operator, factory=composition_db[2])
    _, engine, _, workspace = composition_db
    receipt, checkpoint = read_lifecycle_receipt(workspace), read_accounting_checkpoint(workspace)
    async with engine.begin() as connection:
        if damage == "partial":
            await connection.execute(text("DELETE FROM runtime_composition_states WHERE runtime_domain='seraph.memory.v1'"))
        elif damage == "stale":
            await connection.execute(update(RuntimeCompositionState).values(epoch=2))
        else:
            await connection.execute(text("DELETE FROM runtime_composition_states"))
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: token}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        response = await client.post("/api/chat", json={"message": "Hello", "session_id": conversation.id,
            "message_id": "owned-damaged-inventory-" + damage})
        assert response.status_code == 503, response.text
        expected = {"partial": "inventory_incomplete", "stale": "inventory_stale", "empty_retained": "retained_inventory_missing"}[damage]
        assert response.json()["detail"]["code"] == "composition_" + expected
    async with get_session() as db:
        assert await db.scalar(select(Message).where(Message.session_id == conversation.id)) is None
        assert await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind == "conversation_turn_v1")) is None
    assert read_lifecycle_receipt(workspace) == receipt and read_accounting_checkpoint(workspace) == checkpoint
    assert host.admitting and direct_calls == [] and model.calls == 0


async def _link_existing_task(conversation_id, operator, *, factory=None):
    task_id = "owned-task-context-" + conversation_id
    # Seed actual FK-on rows before ingress; this is fixture setup, not a writer grant.
    async with (factory() if factory is not None else get_session()) as db:
        db.add(WorkBoardTask(task_id=task_id, owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id, goal_id="owned-no-execution-goal",
            title="Existing task context", idempotency_key=task_id))
        await db.flush()
        conversation = await db.get(Session, conversation_id)
        conversation.continuity_task_id = task_id
        if factory is not None:
            await db.commit()
    return task_id


async def _assert_task_linked_legacy(transport, monkeypatch, channel, *, factory=None,
                                    conversation=None, task_id=None, app=None):
    from unittest.mock import AsyncMock
    from src.app import create_app
    from src.agent.session import session_manager
    host, token, operator, model, direct_calls = transport
    monkeypatch.setattr("src.memory.flush.flush_session_memory", AsyncMock())
    if conversation is None:
        conversation = await session_manager.get_or_create(owner_principal_id=operator.principal.principal_id)
        task_id = await _link_existing_task(conversation.id, operator, factory=factory)
    app = app or create_app()
    if factory is not None:
        from src.workspace.accounting_witness import composition_closure, native_composition_files
        async with factory() as db:
            connection = await db.connection()
            projection, members = await connection.run_sync(
                lambda conn: composition_closure(conn, verify_files=native_composition_files))
            assert projection is not None
            assert ("sessions", conversation.id) not in members
            assert ("work_board_tasks", task_id) not in members
            assert await db.scalar(select(WorkflowRunState).where(
                WorkflowRunState.composition_binding_json.is_not(None))) is None
            print("Task-linked legacy fixture before ingress: selected Session=False, selected Task=False, bound jobs=0")
    payload = {"message": "Hello", "session_id": conversation.id,
        "message_id": "owned-task-linked-unbound"}
    if channel == "rest":
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8004",
            cookies={settings.operator_auth_cookie_name: token}, headers={"origin": "http://127.0.0.1:3001"}) as client:
            response = await client.post("/api/chat", json=payload)
            assert response.status_code == 200, response.text
    else:
        incoming, frames = asyncio.Queue(), []
        final = asyncio.get_running_loop().create_future()
        async def send(message):
            if message["type"] == "websocket.send":
                value = json.loads(message["text"])
                frames.append(value)
                if value["type"] in {"final", "error"} and not final.done():
                    final.set_result(value)
        scope = {"type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws",
            "path": "/ws/chat", "raw_path": b"/ws/chat", "query_string": b"",
            "root_path": "", "server": ("127.0.0.1", 8004), "client": ("127.0.0.1", 12345),
            "headers": [(b"host", b"127.0.0.1:8004"), (b"origin", b"http://127.0.0.1:3001"),
                (b"cookie", (settings.operator_auth_cookie_name + "=" + token).encode())], "subprotocols": []}
        await incoming.put({"type": "websocket.connect"})
        await incoming.put({"type": "websocket.receive", "text": json.dumps({"type": "message", **payload})})
        worker = asyncio.create_task(app(scope, incoming.get, send))
        try:
            result = await asyncio.wait_for(asyncio.shield(final), timeout=30)
            assert result["type"] == "final", frames
            assert result["content"] == "Scripted native reply"
            assert "".join(frame["content"] for frame in frames if frame["type"] == "delta") == result["content"]
            assert [frame["seq"] for frame in frames] == sorted({frame["seq"] for frame in frames})
        finally:
            await incoming.put({"type": "websocket.disconnect", "code": 1000})
            await asyncio.wait_for(worker, timeout=5)
    async with get_session() as db:
        messages = list((await db.execute(select(Message).where(Message.session_id == conversation.id))).scalars())
        assert sorted(message.role for message in messages) == ["assistant", "user"]
        assert (await db.get(Session, conversation.id)).continuity_task_id == task_id
        assert await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind == "conversation_turn_v1")) is None
    assert host.admitting and len(direct_calls) == (1 if channel == "rest" else 0) and model.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["rest", "ws"])
async def test_actual_stock_task_linked_healthy_inventory_keeps_unbound_legacy(native_transport, composition_db, monkeypatch, channel):
    await _assert_task_linked_legacy(native_transport, monkeypatch, channel, factory=composition_db[2])


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["rest", "ws"])
async def test_actual_stock_task_linked_absent_inventory_keeps_unbound_legacy(fresh_native_transport, monkeypatch, channel):
    await _assert_task_linked_legacy(fresh_native_transport, monkeypatch, channel)


@asynccontextmanager
async def _canonical_task_factory(native_transport, composition_db):
    from src.app import create_app
    from src.agent.session import session_manager
    from src.conversation.task_context import TaskContinuityService
    from src.work_board.repository import WorkBoardRepository
    _, token, operator, _, _ = native_transport
    task_id, goal_id = "owned-canonical-factory-task", "owned-canonical-factory-goal"
    async with (composition_db[2]() if composition_db is not None else get_session()) as db:
        db.add(Goal(id=goal_id, title="Existing owned goal",
            owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id))
        db.add(WorkBoardTask(task_id=task_id, goal_id=goal_id,
            owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id,
            title="Existing owned task", idempotency_key=task_id))
        await db.commit()
    service = TaskContinuityService(WorkBoardRepository())
    await service.start()
    app = create_app()
    app.state.task_continuity = service
    session_manager.bind_task_continuity(service)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8004",
            cookies={settings.operator_auth_cookie_name: token}, headers={"origin": "http://127.0.0.1:3001"}) as client:
            yield app, client, service, task_id, operator
    finally:
        session_manager.bind_task_continuity(None)
        await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["rest", "ws"])
async def test_actual_stock_canonical_task_factory_wholly_absent(fresh_native_transport, monkeypatch, channel):
    async with _canonical_task_factory(fresh_native_transport, None) as (app, client, service, task_id, operator):
        payload = {"task_id": task_id, "expected_revision": 1, "new_conversation_id": "owned-absent-factory"}
        first = await client.post("/api/sessions/continue-task", json=payload)
        assert first.status_code == 200 and first.json()["idempotent_replay"] is False, first.text
        replay = await client.post("/api/sessions/continue-task", json=payload)
        assert replay.status_code == 200 and replay.json()["idempotent_replay"] is True
        context = await client.get("/api/sessions/task-context/" + task_id)
        assert context.status_code == 200 and context.json()["memory_status"] == "no_learning"
        async with get_session() as db:
            conversation = await db.get(Session, payload["new_conversation_id"])
            assert conversation.owner_principal_id == operator.principal.principal_id
            assert conversation.continuity_task_id == task_id
        await _assert_task_linked_legacy(fresh_native_transport, monkeypatch, channel,
            conversation=conversation, task_id=task_id, app=app)


@pytest.mark.asyncio
@pytest.mark.parametrize("retained_missing", [False, True])
async def test_actual_stock_canonical_task_factory_competing_native_owner(fresh_native_transport, monkeypatch, retained_missing):
    from src.runtime_plugins.ownership import DOMAINS, begin_native_writer, initialize_fresh_deployment
    from src.workspace.production import (ProductionWorkspace, maintenance_fence, prepare_lifecycle_directory,
        read_lifecycle_receipt, read_accounting_checkpoint)
    from src.conversation import task_context
    async with _canonical_task_factory(fresh_native_transport, None) as (app, client, service, task_id, operator):
        workspace = ProductionWorkspace(host_root=Path(settings.workspace_dir))
        original_begin, original_packet = task_context._begin_sqlite_immediate, service.packet
        packet_calls, committed = [], []
        async def observe_packet(*args):
            packet_calls.append(True)
            return await original_packet(*args)
        async def competing_owner(db):
            # End only the empty preflight read snapshot, then let the real native
            # maintenance owner commit before the original legacy writer locks.
            await db.rollback()
            prepare_lifecycle_directory(workspace)
            with maintenance_fence(workspace):
                async with get_session() as other:
                    await begin_native_writer(other, owner="composition_maintenance", fresh=True)
                    await initialize_fresh_deployment(other, composition_digests={domain: "a" * 64 for domain in DOMAINS})
            async with get_session() as other:
                assert await other.scalar(select(RuntimeCompositionState.runtime_domain)) is not None
            if retained_missing:
                # Corrupt actual committed SQL only; retain original fsynced receipts.
                damage_engine = create_async_engine(f"sqlite+aiosqlite:///{Path(settings.workspace_dir) / 'seraph.db'}")
                try:
                    async with damage_engine.begin() as connection:
                        await connection.execute(text("DELETE FROM runtime_composition_states"))
                finally:
                    await damage_engine.dispose()
            committed.append((read_lifecycle_receipt(workspace), read_accounting_checkpoint(workspace)))
            await original_begin(db)
        monkeypatch.setattr(task_context, "_begin_sqlite_immediate", competing_owner)
        monkeypatch.setattr(service, "packet", observe_packet)
        response = await client.post("/api/sessions/continue-task", json={"task_id": task_id,
            "expected_revision": 1, "new_conversation_id": "owned-competing-factory"})
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] == (
            "composition_retained_inventory_missing" if retained_missing else "composition_continuity_unavailable")
        assert packet_calls == [] and len(committed) == 1
        async with get_session() as db:
            assert await db.get(Session, "owned-competing-factory") is None
            assert await db.scalar(select(Message)) is None
            assert await db.scalar(select(WorkflowRunState)) is None
        assert (read_lifecycle_receipt(workspace), read_accounting_checkpoint(workspace)) == committed[0]
        assert fresh_native_transport[3].calls == 0 and fresh_native_transport[4] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["naked_bound_job", "malformed_metadata"])
async def test_actual_stock_canonical_task_factory_absence_signals(native_transport, composition_db, monkeypatch, damage):
    from src.runtime_plugins.ownership import begin_native_writer, bind_invocation
    from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec, durable_job_repository
    from src.workspace.production import lifecycle_receipt_path
    async with _canonical_task_factory(native_transport, composition_db) as (app, client, service, task_id, operator):
        if damage == "naked_bound_job":
            async with get_session() as db:
                await begin_native_writer(db, owner="durable_jobs")
                binding = await bind_invocation(db, method="tasks.admit", native_branch="workflow",
                    reviewed_composition=native_transport[0].reviewed)
                spec = DurableJobSpec(identity=DurableJobIdentity(job_id="owned-naked-factory-job",
                    owner_kind="user", owner_principal_id=operator.principal.principal_id,
                    job_kind="workflow", capability_version="factory-negative.v1",
                    idempotency_scope="factory-negative", idempotency_key="naked-bound-job"),
                    inputs={"fixture": "no_execution"}, session_id=operator.session_id,
                    operator_session_id=operator.session_id,
                    declared_authority={"principal": operator.principal.principal_id}, composition_binding=binding)
                assert (await durable_job_repository._admit_in_session(db, spec))["status"] == "accepted"
        async with composition_db[1].begin() as connection:
            await connection.execute(text("DELETE FROM runtime_composition_states"))
        workspace = composition_db[3]
        lifecycle_receipt_path(workspace).unlink()
        (workspace.lifecycle_directory / "accounting-checkpoint.json").unlink()
        if damage == "malformed_metadata":
            lifecycle_receipt_path(workspace).write_bytes(b"malformed")
        retained_files = {path.name: path.read_bytes() for path in workspace.lifecycle_directory.iterdir() if path.is_file()}
        async def forbidden_packet(*args):
            pytest.fail("absence denial must precede Root/task packet reads")
        monkeypatch.setattr(service, "packet", forbidden_packet)
        response = await client.post("/api/sessions/continue-task", json={"task_id": task_id,
            "expected_revision": 1, "new_conversation_id": "owned-absence-signals"})
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] == (
            "composition_retained_inventory_missing" if damage == "naked_bound_job" else "production_workspace_invalid")
        async with composition_db[2]() as db:
            assert await db.get(Session, "owned-absence-signals") is None
            assert await db.scalar(select(Message)) is None
            assert len(list((await db.execute(select(WorkflowRunState))).scalars())) == (1 if damage == "naked_bound_job" else 0)
        assert {path.name: path.read_bytes() for path in workspace.lifecycle_directory.iterdir() if path.is_file()} == retained_files
        assert native_transport[3].calls == 0 and native_transport[4] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["rest", "ws"])
async def test_actual_stock_canonical_task_factory_replay_context_and_legacy(native_transport, composition_db, monkeypatch, channel):
    async with _canonical_task_factory(native_transport, composition_db) as (app, client, service, task_id, operator):
        payload = {"task_id": task_id, "expected_revision": 1, "new_conversation_id": "owned-factory-conversation"}
        first = await client.post("/api/sessions/continue-task", json=payload)
        assert first.status_code == 200, first.text
        assert first.headers["cache-control"] == "no-store"
        assert first.json()["idempotent_replay"] is False
        second = await client.post("/api/sessions/continue-task", json=payload)
        assert second.status_code == 200 and second.json()["idempotent_replay"] is True
        context = await client.get("/api/sessions/task-context/" + task_id)
        assert context.status_code == 200 and context.json()["memory_status"] == "no_learning"
        assert context.json()["conversation_ids"] == [payload["new_conversation_id"]]
        async with get_session() as db:
            conversation = await db.get(Session, payload["new_conversation_id"])
            assert conversation.owner_principal_id == operator.principal.principal_id
            assert conversation.continuity_task_id == task_id
            assert conversation.title == "Continue task"
        await _assert_task_linked_legacy(native_transport, monkeypatch, channel,
            factory=composition_db[2], conversation=conversation, task_id=task_id, app=app)
        transcript = await client.get("/api/sessions/" + conversation.id + "/messages")
        assert transcript.status_code == 200
        assert sorted(row["role"] for row in transcript.json()) == ["assistant", "user"]


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["stale_revision", "foreign_task", "revoked_root", "owner_collision", "link_collision"])
async def test_actual_stock_canonical_task_factory_denials_roll_back(native_transport, composition_db, denial):
    from datetime import datetime, timezone
    from src.workspace.production import read_lifecycle_receipt, read_accounting_checkpoint
    async with _canonical_task_factory(native_transport, composition_db) as (app, client, service, task_id, operator):
        payload = {"task_id": task_id, "expected_revision": 2 if denial == "stale_revision" else 1,
            "new_conversation_id": "owned-factory-denied"}
        async with composition_db[2]() as db:
            if denial == "foreign_task":
                task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
                task.owner_principal_id = "different-principal"
            elif denial == "revoked_root":
                root = await db.get(OperatorSession, operator.session_id)
                root.revoked_at = datetime.now(timezone.utc)
            elif denial in {"owner_collision", "link_collision"}:
                db.add(Session(id=payload["new_conversation_id"],
                    owner_principal_id="different-principal" if denial == "owner_collision" else operator.principal.principal_id))
            await db.commit()
        workspace = composition_db[3]
        before = read_lifecycle_receipt(workspace), read_accounting_checkpoint(workspace)
        response = await client.post("/api/sessions/continue-task", json=payload)
        assert response.status_code in {401, 403, 404, 409}, response.text
        async with composition_db[2]() as db:
            conversation = await db.get(Session, payload["new_conversation_id"])
            if denial in {"owner_collision", "link_collision"}:
                assert conversation is not None and conversation.continuity_task_id is None
            else:
                assert conversation is None
            assert await db.scalar(select(Message).where(Message.session_id == payload["new_conversation_id"])) is None
            assert await db.scalar(select(WorkflowRunState).where(WorkflowRunState.composition_binding_json.is_not(None))) is None
        assert (read_lifecycle_receipt(workspace), read_accounting_checkpoint(workspace)) == before
        assert native_transport[3].calls == 0 and native_transport[4] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["partial", "stale", "empty_retained"])
async def test_actual_stock_canonical_task_factory_damaged_inventory_denies(native_transport, composition_db, damage):
    from src.workspace.production import read_lifecycle_receipt, read_accounting_checkpoint
    async with _canonical_task_factory(native_transport, composition_db) as (app, client, service, task_id, operator):
        async with composition_db[1].begin() as connection:
            if damage == "partial":
                await connection.execute(text("DELETE FROM runtime_composition_states WHERE runtime_domain='seraph.memory.v1'"))
            elif damage == "stale":
                await connection.execute(update(RuntimeCompositionState).values(epoch=2))
            else:
                await connection.execute(text("DELETE FROM runtime_composition_states"))
        workspace = composition_db[3]
        before = read_lifecycle_receipt(workspace), read_accounting_checkpoint(workspace)
        response = await client.post("/api/sessions/continue-task", json={"task_id": task_id,
            "expected_revision": 1, "new_conversation_id": "owned-damaged-factory"})
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] in {
            "production_workspace_reconciliation_required", "composition_retained_inventory_missing"}
        async with composition_db[2]() as db:
            assert await db.get(Session, "owned-damaged-factory") is None
            assert await db.scalar(select(WorkflowRunState).where(WorkflowRunState.composition_binding_json.is_not(None))) is None
        assert (read_lifecycle_receipt(workspace), read_accounting_checkpoint(workspace)) == before
        assert native_transport[3].calls == 0 and native_transport[4] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["root", "revision", "task_owner"])
async def test_actual_stock_canonical_task_factory_current_authority_races_roll_back(native_transport, composition_db, monkeypatch, race):
    from datetime import datetime, timezone
    from src.workspace.production import read_lifecycle_receipt, read_accounting_checkpoint
    async with _canonical_task_factory(native_transport, composition_db) as (app, client, service, task_id, operator):
        original = service.packet
        async def change_current_row(db, *args):
            if race == "root":
                root = await db.get(OperatorSession, operator.session_id)
                root.revoked_at = datetime.now(timezone.utc)
            else:
                task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
                if race == "revision":
                    task.task_revision += 1
                else:
                    task.owner_principal_id = "different-principal"
            await db.flush()
            return await original(db, *args)
        monkeypatch.setattr(service, "packet", change_current_row)
        workspace = composition_db[3]
        before = read_lifecycle_receipt(workspace), read_accounting_checkpoint(workspace)
        response = await client.post("/api/sessions/continue-task", json={"task_id": task_id,
            "expected_revision": 1, "new_conversation_id": "owned-raced-factory"})
        assert response.status_code in {401, 403, 404, 409}, response.text
        async with composition_db[2]() as db:
            assert await db.get(Session, "owned-raced-factory") is None
            assert (await db.get(OperatorSession, operator.session_id)).revoked_at is None
            task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
            assert task.task_revision == 1 and task.owner_principal_id == operator.principal.principal_id
        assert (read_lifecycle_receipt(workspace), read_accounting_checkpoint(workspace)) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["existing", "same_writer"])
async def test_actual_stock_canonical_task_factory_native_job_provenance_denies(native_transport, composition_db, monkeypatch, timing):
    from src.runtime_plugins.ownership import begin_native_writer, bind_invocation
    from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec, durable_job_repository
    from src.workspace.production import read_lifecycle_receipt, read_accounting_checkpoint
    async with _canonical_task_factory(native_transport, composition_db) as (app, client, service, task_id, operator):
        identifier = "owned-native-provenance-factory"
        async def admit_bound_job(db):
            binding = await bind_invocation(db, method="tasks.admit", native_branch="workflow",
                reviewed_composition=native_transport[0].reviewed)
            spec = DurableJobSpec(identity=DurableJobIdentity(job_id="owned-factory-bound-job",
                owner_kind="user", owner_principal_id=operator.principal.principal_id,
                job_kind="workflow", capability_version="factory-negative.v1",
                idempotency_scope="factory-negative", idempotency_key="native-provenance"),
                inputs={"fixture": "no_execution"}, session_id=operator.session_id,
                operator_session_id=operator.session_id, conversation_id=identifier,
                declared_authority={"principal": operator.principal.principal_id}, composition_binding=binding)
            result = await durable_job_repository._admit_in_session(db, spec)
            assert result["status"] == "accepted"
        if timing == "existing":
            async with get_session() as db:
                await begin_native_writer(db, owner="durable_jobs")
                await admit_bound_job(db)
        else:
            original = service.packet
            calls = 0
            async def admit_after_current_packet(db, *args):
                nonlocal calls
                packet = await original(db, *args)
                calls += 1
                if calls == 1:
                    await admit_bound_job(db)
                return packet
            monkeypatch.setattr(service, "packet", admit_after_current_packet)
        workspace = composition_db[3]
        before = read_lifecycle_receipt(workspace), read_accounting_checkpoint(workspace)
        payload = {"task_id": task_id, "expected_revision": 1, "new_conversation_id": identifier}
        if timing == "same_writer":
            # The original native FK owner guard denies this cross-owner race earlier.
            with pytest.raises(RuntimeError, match="retained native Session FK writer required"):
                await client.post("/api/sessions/continue-task", json=payload)
        else:
            response = await client.post("/api/sessions/continue-task", json=payload)
            assert response.status_code == 409, response.text
            assert response.json()["detail"]["code"] == "task_conversation_conflict"
        async with composition_db[2]() as db:
            assert await db.get(Session, identifier) is None
            job = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == "owned-factory-bound-job"))
            assert (job is not None) == (timing == "existing")
            assert await db.scalar(select(Message).where(Message.session_id == identifier)) is None
        assert (read_lifecycle_receipt(workspace), read_accounting_checkpoint(workspace)) == before
        assert native_transport[3].calls == 0 and native_transport[4] == []


@pytest.mark.asyncio
async def test_actual_stock_canonical_task_factory_native_admission_race_rolls_back(native_transport, composition_db, monkeypatch):
    from src.agent.session import session_manager
    from src.agent.turn_execution import NativeTurnAdmission, NativeTurnBlocked, native_turn_spec
    from src.api.chat import _bind_chat_principal, build_chat_ingress_envelope, chat_ingress_metadata
    from src.workflows.job_runtime import durable_job_repository
    from src.workspace.production import read_lifecycle_receipt, read_accounting_checkpoint
    async with _canonical_task_factory(native_transport, composition_db) as (app, client, service, task_id, operator):
        identifier = "owned-factory-admission-race"
        principal = _bind_chat_principal(identifier, operator=operator)
        ingress = build_chat_ingress_envelope(message="No execution", session_id=identifier,
            principal=principal, operator_session_id=operator.session_id, transport="rest",
            client_message_id="owned-factory-admission-race-input")
        admission = NativeTurnAdmission.capture(ingress, principal=principal,
            reviewed_composition=native_transport[0].reviewed, native_route="direct_turn")
        original, calls = service.packet, 0
        async def admit_after_actual_session_created(db, *args):
            nonlocal calls
            calls += 1
            if calls == 2:
                assert (await db.get(Session, identifier)).continuity_task_id == task_id
                spec = await native_turn_spec(db, admission)
                await session_manager._add_message_in_db(db, identifier, "user", "No execution",
                    message_id=ingress.message_id, metadata_json=chat_ingress_metadata(ingress), attachment_refs=[])
                await durable_job_repository._admit_in_session(db, spec, native_turn_admission=admission)
                pytest.fail("nonnull native Task context must deny original admission")
            return await original(db, *args)
        monkeypatch.setattr(service, "packet", admit_after_actual_session_created)
        workspace = composition_db[3]
        before = read_lifecycle_receipt(workspace), read_accounting_checkpoint(workspace)
        with pytest.raises(NativeTurnBlocked, match="native_turn_continuity_context_unsupported"):
            await client.post("/api/sessions/continue-task", json={"task_id": task_id,
                "expected_revision": 1, "new_conversation_id": identifier})
        async with composition_db[2]() as db:
            assert await db.get(Session, identifier) is None
            assert await db.get(Message, ingress.message_id) is None
            assert await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == admission.job_id)) is None
        assert (read_lifecycle_receipt(workspace), read_accounting_checkpoint(workspace)) == before


class ControlledScriptedModel(Model):
    def __init__(self, name):
        super().__init__(model_id="owned-controlled-script")
        self.name, self.calls = name, 0

    def generate(self, messages, **kwargs):
        self.calls += 1
        assert self.calls == 1, "controlled outcome must stop before another model call"
        return _scripted_accounted_result(ChatMessage(role="assistant", content="", tool_calls=[ChatMessageToolCall(
            id="owned-controlled", type="function", function=ChatMessageToolCallFunction(
                name=self.name, arguments={"question": "Which local item?", "reason": "", "options": ""}))]))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["clarification", "approval"])
async def test_actual_stock_sdk_canonical_controlled_pauses_original_native_rest(native_transport, monkeypatch, kind):
    from src.app import create_app
    from src.tools.clarify_tool import clarify
    host, token, _, _, _ = native_transport
    model = ControlledScriptedModel("clarify")
    from src.tools.approval import ApprovalTool
    producer = ApprovalTool(clarify, force_approval=True) if kind == "approval" else clarify
    agent = ToolCallingAgent(tools=[producer], model=model, max_steps=2, verbosity_level=0)
    monkeypatch.setattr("src.api.chat.create_onboarding_agent", lambda message: agent)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: token}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        response = await client.post("/api/chat", json={"message": "Inspect this short answer",
            "message_id": "owned-genuine-" + kind})
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["type"] == kind + "_required"
    async with get_session() as db:
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind == "conversation_turn_v1"))
        assert run.status == ("awaiting_approval" if kind == "approval" else "paused") and run.attempt_count == 1
        messages = list((await db.execute(select(Message).where(Message.session_id == run.conversation_id))).scalars())
        assert sorted(message.role for message in messages) == (["user"] if kind == "approval" else ["assistant", "user"])
        receipt = next(item for item in json.loads(run.checkpoint_receipts_json) if item["checkpoint_id"] == "conversation:controlled-outcome")
        assert receipt["safe"] is True and receipt["payload"]["no_learning"] is True
    assert model.calls == 1 and host.admitting


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["sdk_component", "integrated"])
@pytest.mark.parametrize("mode", ["public_clarification", "public_approval", "nested_real", "nested_cause", "copied_origin", "registry_replaced"])
async def test_actual_stock_sdk_foreign_controlled_origin_denied_before_publication(native_transport, monkeypatch, mode, boundary):
    from dataclasses import replace
    from smolagents import Tool
    from src.agent.exceptions import ClarificationRequired
    from src.approval.exceptions import ApprovalRequired
    from src.app import create_app
    from src.tools.clarify_tool import clarify
    host, token, _, _, _ = native_transport
    class ForeignTool(Tool):
        name = "foreign_controlled"
        description = "Owned adversarial controlled-outcome fixture."
        inputs = {"question": {"type": "string", "description": "Question"},
            "reason": {"type": "string", "description": "Reason"},
            "options": {"type": "string", "description": "Options"}}
        output_type = "string"
        def forward(self, question: str, reason: str, options: str) -> str:
            if mode == "public_clarification":
                raise ClarificationRequired(question=question)
            if mode == "public_approval":
                raise ApprovalRequired(approval_id="fabricated", session_id=None,
                    tool_name=self.name, risk_level="high", summary="fabricated")
            try:
                clarify(question=question, reason=reason, options=options)
            except ClarificationRequired as caught:
                if mode == "nested_cause":
                    raise RuntimeError("foreign nested cause") from caught
                if mode == "copied_origin":
                    copied = ClarificationRequired(question=question)
                    copied._canonical_controlled_origin = replace(caught._canonical_controlled_origin,
                        issued_exception=copied)
                    raise copied
                if mode == "registry_replaced":
                    agent.tools[self.name] = ForeignTool()
                raise caught
    class ComponentModel(ControlledScriptedModel):
        def generate(self, messages, **kwargs):
            self.calls += 1
            assert self.calls == 1
            return ChatMessage(role="assistant", content="", tool_calls=[ChatMessageToolCall(
                id="owned-component-controlled", type="function", function=ChatMessageToolCallFunction(
                    name=self.name, arguments={"question": "Which local item?", "reason": "", "options": ""}))])
    model = ComponentModel("foreign_controlled") if boundary == "sdk_component" else ControlledScriptedModel("foreign_controlled")
    agent = ToolCallingAgent(tools=[ForeignTool()], model=model, max_steps=2, verbosity_level=0)
    monkeypatch.setattr("src.api.chat.create_onboarding_agent", lambda message: agent)
    if boundary == "sdk_component":
        # This exercises the actual SDK controlled-origin component. The claim
        # has no initialized family and cannot publish a native turn result.
        from src.agent.controlled_origin import install_controlled_callback, original_execution_context
        from src.agent.session import session_manager
        from src.agent.turn_execution import NativeTurnAdmission, NativeTurnBlocked, claim_native_turn
        from src.api.chat import _bind_chat_principal, build_chat_ingress_envelope, chat_ingress_metadata
        operator = native_transport[2]
        conversation = await session_manager.get_or_create(owner_principal_id=operator.principal.principal_id)
        principal = _bind_chat_principal(conversation.id, operator=operator)
        ingress = build_chat_ingress_envelope(message="Inspect this short answer", session_id=conversation.id,
            principal=principal, operator_session_id=operator.session_id, transport="rest",
            client_message_id="owned-component-foreign-" + mode)
        admission = NativeTurnAdmission.capture(ingress, principal=principal,
            reviewed_composition=host.reviewed, native_route="generic_turn")
        _, _, job = await session_manager.reserve_native_turn_message(conversation.id, "Inspect this short answer",
            message_id=ingress.message_id, metadata_json=chat_ingress_metadata(ingress), admission=admission)
        execution = await claim_native_turn(admission, host, job)
        install_controlled_callback(execution, agent)
        def run_component():
            with original_execution_context(execution):
                return agent.run("Inspect this short answer")
        with pytest.raises(NativeTurnBlocked) as rejected:
            await asyncio.to_thread(run_component)
        assert rejected.value.reason_code in {
            "native_turn_controlled_origin_unproven", "native_turn_controlled_indirect_outcome"}
        assert not getattr(execution, "_family_initialized", False)
    else:
        async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
            cookies={settings.operator_auth_cookie_name: token}, headers={"origin": "http://127.0.0.1:3001"}) as client:
            response = await client.post("/api/chat", json={"message": "Inspect this short answer",
                "message_id": "owned-foreign-" + mode})
            assert response.status_code == 503, response.text
            assert response.json()["detail"]["code"] == "native_turn_family_tool_registry_unsupported"
    async with get_session() as db:
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind == "conversation_turn_v1"))
        assert run.status == "running" and run.attempt_count == 1
        messages = list((await db.execute(select(Message).where(Message.session_id == run.conversation_id))).scalars())
        assert [message.role for message in messages] == ["user"]
        assert not any(item["checkpoint_id"] == "conversation:controlled-outcome"
            for item in json.loads(run.checkpoint_receipts_json))
    assert model.calls == (1 if boundary == "sdk_component" else 0) and host.admitting


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["clarification", "approval"])
async def test_actual_stock_sdk_controlled_ws_frames_and_original_completion(native_transport, monkeypatch, kind):
    from src.app import create_app
    from src.tools.clarify_tool import clarify
    from src.tools.approval import ApprovalTool
    host, token, _, _, _ = native_transport
    model = ControlledScriptedModel("clarify")
    producer = ApprovalTool(clarify, force_approval=True) if kind == "approval" else clarify
    agent = ToolCallingAgent(tools=[producer], model=model, max_steps=2, verbosity_level=0)
    monkeypatch.setattr("src.api.ws.create_onboarding_agent", lambda message: agent)
    incoming, frames = asyncio.Queue(), []
    outcome = asyncio.get_running_loop().create_future()
    async def send(message):
        if message["type"] == "websocket.send":
            value = json.loads(message["text"])
            frames.append(value)
            if value["type"] in {"approval_required", "clarification_required", "error", "final"} and not outcome.done():
                outcome.set_result(value)
    scope = {"type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws",
        "path": "/ws/chat", "raw_path": b"/ws/chat", "query_string": b"", "root_path": "",
        "server": ("127.0.0.1", 8004), "client": ("127.0.0.1", 12345), "subprotocols": [],
        "headers": [(b"host", b"127.0.0.1:8004"), (b"origin", b"http://127.0.0.1:3001"),
            (b"cookie", (settings.operator_auth_cookie_name + "=" + token).encode())]}
    await incoming.put({"type": "websocket.connect"})
    await incoming.put({"type": "websocket.receive", "text": json.dumps({"type": "message",
        "message": "Inspect this short answer", "message_id": "owned-ws-controlled-" + kind})})
    task = asyncio.create_task(create_app()(scope, incoming.get, send))
    try:
        result = await asyncio.wait_for(asyncio.shield(outcome), timeout=30)
        assert result["type"] == kind + "_required", frames
        assert not any(frame["type"] == "final" for frame in frames)
        assert [frame["seq"] for frame in frames] == sorted({frame["seq"] for frame in frames})
        async with get_session() as db:
            run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind == "conversation_turn_v1"))
            assert run.status == ("awaiting_approval" if kind == "approval" else "paused")
            assert any(item["checkpoint_id"] == "conversation:controlled-outcome"
                for item in json.loads(run.checkpoint_receipts_json))
    finally:
        await incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, timeout=5)
    assert model.calls == 1 and host.admitting
