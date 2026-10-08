"""Ordinary functional state transitions; no external model contact or spend."""
from dataclasses import replace
from datetime import datetime, timezone
import json
import socket
import time

import httpx
import pytest

from config.settings import settings
from tests.test_inference_accounting import accounting_db, setup_configuration
from src.work_board.contracts import GeneralTaskInput, TaskLimits, ToolDescriptor, WorkBoardOwner
from src.work_board.general_task_planner import GeneralTaskPlanner, planner_messages, public_schema
from src.work_board.repository import BoardError


def descriptor():
    return ToolDescriptor(tool_id="fixture.read", version="1",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False},
        output_schema={"type": "string"}, effects=["read"], permissions=["workspace_read"],
        credential_refs=["secret-ref:private-server"], deadline=30, verifier="json_schema",
        policy_digest="a" * 64)


def task_input(**changes):
    return GeneralTaskInput(goal_ref="goal:fixture", intent="Read notes.txt and return its text",
        evidence_refs=["private/evidence.json"], requested_output={"type": "string"},
        tool_set_digest="a" * 64, limits=TaskLimits(max_cost_microusd=500),
        inference_egress_acknowledged=True).model_copy(update=changes)


async def propose(accounting_db, owner, inputs=None):
    async with accounting_db[2]() as db:
        return await GeneralTaskPlanner().propose(db, owner, inputs or task_input(), [descriptor()], 1, "intent")


@pytest.fixture(autouse=True)
def forbid_external_inference(monkeypatch):
    # Both sockets fail before any address is contacted; AF_UNIX remains usable
    # for event-loop internals and disposable SQLite uses no provider sockets.
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex
    def denied(sock, address):
        if isinstance(address, tuple):
            raise AssertionError("Every network inference socket is forbidden")
        return connect(sock, address)
    def denied_ex(sock, address):
        if isinstance(address, tuple):
            raise AssertionError("Every network inference socket is forbidden")
        return connect_ex(sock, address)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied_ex)
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LLM_API_KEY", "LOCAL_LLM_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(settings, "openrouter_api_key", "fixture-never-sent")


def test_public_schema_and_payload_do_not_export_private_context():
    schema = {"description": "private annotation", "properties": {"description": {"type": "string", "default": "secret"}}}
    assert public_schema(schema) == {"properties": {"description": {"type": "string"}}}
    payload = json.loads(planner_messages(task_input(), [descriptor()])[1]["content"])
    assert payload["registered_tools"][0]["tool_id"] == "fixture.read"
    serialized = json.dumps(payload)
    assert "private/evidence" not in serialized
    assert "secret-ref" not in serialized
    assert "goal:fixture" not in serialized
    assert "credential_refs" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("changes,reason", [
    ({"inference_egress_acknowledged": False}, "general_task_planning_consent_required"),
    ({"limits": TaskLimits(max_cost_microusd=0)}, "general_task_planning_budget_required"),
    ({"limits": TaskLimits(max_cost_microusd=500, max_inference_calls=0)}, "general_task_planning_budget_required"),
])
async def test_missing_authority_denies_before_policy_and_contact(changes, reason):
    with pytest.raises(BoardError) as error:
        await GeneralTaskPlanner().propose(None, WorkBoardOwner(principal_id="operator:fixture", session_id="session"),
            task_input(**changes), [descriptor()], 1, "intent")
    assert error.value.code == reason


async def prepare(accounting_db, monkeypatch, *, existing_owner=None):
    from src.auth.service import create_session
    from src.api.model_fabric_settings import _setup_configuration
    from src.model_fabric.configuration import write_model_fabric_configuration
    from src.model_fabric import candidate_from_profile
    from src.model_fabric.proofs import build_model_route_proof
    from src.model_fabric.repository import model_fabric_repository
    from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
    from src.workflows.job_runtime import DurableJobRepository
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "planner-disposable-password")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    configured = setup_configuration(ceiling=1000, bound=100)
    configured = _setup_configuration(replace(configured.openrouter_setup,
        capabilities=("text", "structured_output"), timeout_seconds=30), profiles=(), policies=())
    configured = replace(configured, egress_revision=2)
    write_model_fabric_configuration(configured)
    jobs = DurableJobRepository()
    await jobs.configure_inference_accounting(1000)
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    monkeypatch.setattr("src.model_fabric.remote_inference_admission.remote_inference_admission_broker", broker)
    if existing_owner is None:
        _, operator = await create_session()
    else:
        from src.auth.service import authenticate_session
        operator = await authenticate_session(existing_owner.session_id, touch=False)
        assert operator.principal.principal_id == existing_owner.principal_id
    from src.llm_runtime import _provider_profile
    profile = _provider_profile("openrouter")
    profile = replace(profile, routing_model=profile.routing_model or profile.model)
    candidate = candidate_from_profile(profile)
    from src.db.models import ModelRouteReceiptRecord, Goal, GoalLevel, GoalStatus
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with accounting_db[2]() as db:
        db.add(Goal(id="goal:fixture", title="Disposable planning goal", level=GoalLevel.daily,
            status=GoalStatus.active, revision=1, owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id))
        db.add(ModelRouteReceiptRecord(receipt_id="literal-fixture", receipt_hash="b"*64,
            request_id="literal-fixture", route_decision_id="literal-fixture", runtime_path="functional_fixture",
            workload="interactive", outcome="succeeded", egress_class="cloud_allowed_full",
            started_at=now, finished_at=now, latency_ms=1))
        await db.commit()
    # Literal disposable capability metadata exercises exact proof lookup and
    # selection. These are not generated by model calls or a quality campaign.
    for capability, value in {"text": "supported", "structured_output": "supported",
        "health": "healthy", "latency_ms": 30000}.items():
        proof = build_model_route_proof(profile=profile, endpoint_class=candidate.endpoint_class,
            adapter=candidate.adapter, capability=capability, canary_version="functional-fixture-v1",
            outcome="passed", checked_at=time.time()-1, expires_at=time.time()+300,
            probe_receipt_id="literal-fixture", probe_receipt_hash="b"*64,
            proven_value=value)
        assert (await model_fabric_repository.persist_capability_proof(proof)).persisted
    return jobs, WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("content,valid", [
    (json.dumps({"schema_version": 1, "revision": 1, "steps": [{"step_id": "read", "tool_id": "fixture.read",
        "input": {"path": "notes.txt"}, "depends_on": [], "output_contract": {"type": "string"}}]}), True),
    ("not JSON", False),
])
async def test_real_serial_broker_records_planning_contact_without_task_execution(accounting_db, monkeypatch, content, valid):
    from sqlalchemy import select
    from src.db.models import WorkflowRunState, WorkBoardTask
    jobs, owner = await prepare(accounting_db, monkeypatch)
    calls = []
    class Bytes(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield json.dumps({"id": "gen-planner-fixture", "usage": {"cost": "0"},
                "choices": [{"message": {"role": "assistant", "content": content}}]}).encode()
    class Boundary(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
            snapshot = await jobs.inference_accounting_snapshot()
            assert len(snapshot["operations"]) == 1
            entry = json.loads(snapshot["operations"][0]["evidence_json"])[1]
            assert entry["kind"] == "general_task_group_reservation.v1"
            assert entry["role"] == "initial_proposal" and entry["call_ordinal"] == 1
            assert entry["group"]["owner_session_id"] == owner.session_id
            assert entry["group"]["goal_id"] == "goal:fixture"
            assert entry["group"]["max_inference_calls"] == 12
            assert entry["selected_grant_digest"] is None
            calls.append(json.loads(request.content))
            return httpx.Response(200, request=request, stream=Bytes())
    original = httpx.AsyncClient
    def intercepted(*args, **kwargs):
        kwargs["transport"] = Boundary()
        return original(*args, **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", intercepted)
    if valid:
        plan = await propose(accounting_db, owner)
        assert plan.steps[0].tool_id == "fixture.read"
    else:
        with pytest.raises(BoardError, match="Planner output is invalid"):
            await propose(accounting_db, owner)
    assert len(calls) == 1
    # A restart cannot renew a contacted planning operation if task publication
    # was lost: durable canonical accounting rejects its original identity.
    from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
    monkeypatch.setattr("src.model_fabric.remote_inference_admission.remote_inference_admission_broker",
        RemoteInferenceAdmissionBroker(durable_accounting=True))
    with pytest.raises(BoardError, match="Original planning operation exists"):
        await propose(accounting_db, owner)
    with pytest.raises(BoardError, match="Original planning operation exists"):
        await propose(accounting_db, owner, task_input(intent="Changed unpublished intent must not renew the allowance"))
    assert len(calls) == 1
    snapshot = await jobs.inference_accounting_snapshot()
    assert snapshot["committed_microusd"] == 0
    assert snapshot["unknown_microusd"] == 0
    assert snapshot["operations"][0]["state"] == "settled"
    assert snapshot["operations"][0]["runtime_path"] == "general_task_planner"
    from src.work_board.general_task_proposal import group_entry, proposal_provenance
    from src.work_board.contracts import TaskProposalGroupV1
    operation = snapshot["operations"][0]
    group = TaskProposalGroupV1.model_validate(group_entry(operation)["group"])
    provenance = proposal_provenance(operation, group)
    assert provenance.original_deadline_at == group.original_deadline_at
    assert provenance.reservation_sequence == operation["sequence"]
    async with accounting_db[2]() as db:
        assert list((await db.execute(select(WorkBoardTask))).scalars()) == []
        roots = list((await db.execute(select(WorkflowRunState))).scalars())
        assert len(roots) == 1
        assert roots[0].job_kind == "model_inference_ephemeral_v1"
        assert roots[0].status == "succeeded"


@pytest.mark.asyncio
async def test_finite_operator_limit_cannot_lower_actual_reserve(accounting_db, monkeypatch):
    jobs, owner = await prepare(accounting_db, monkeypatch)
    with pytest.raises(BoardError) as error:
        await GeneralTaskPlanner().propose(None, owner,
            task_input(limits=TaskLimits(max_cost_microusd=99)), [descriptor()], 1, "intent")
    assert error.value.code == "general_task_planning_budget_insufficient"
    assert (await jobs.inference_accounting_snapshot())["operations"] == []


@pytest.mark.asyncio
async def test_missing_structured_output_proof_creates_no_contact_or_job(accounting_db, monkeypatch):
    from sqlalchemy import delete, select
    from src.db.models import ModelCapabilityProofRecord, WorkflowRunState
    jobs, owner = await prepare(accounting_db, monkeypatch)
    async with accounting_db[2]() as db:
        await db.execute(delete(ModelCapabilityProofRecord).where(ModelCapabilityProofRecord.capability == "structured_output"))
        await db.commit()
    with pytest.raises(BoardError) as error:
        await propose(accounting_db, owner)
    assert error.value.code == "general_task_planning_route_denied"
    assert "proof_missing:structured_output" in str(error.value)
    assert (await jobs.inference_accounting_snapshot())["operations"] == []
    async with accounting_db[2]() as db:
        assert list((await db.execute(select(WorkflowRunState))).scalars()) == []


@pytest.mark.asyncio
async def test_revoked_policy_creates_no_contact_or_job(accounting_db, monkeypatch):
    from src.model_fabric.configuration import read_model_fabric_configuration, write_model_fabric_configuration
    jobs, owner = await prepare(accounting_db, monkeypatch)
    configured = read_model_fabric_configuration()
    write_model_fabric_configuration(replace(configured, egress_revoked=True, egress_revision=3))
    with pytest.raises(BoardError, match="provider_policy_revoked_or_unavailable"):
        await GeneralTaskPlanner().propose(None, owner, task_input(), [descriptor()], 1, "intent")
    assert (await jobs.inference_accounting_snapshot())["operations"] == []


@pytest.mark.asyncio
async def test_unavailable_vault_redaction_blocks_before_contact(accounting_db, monkeypatch):
    jobs, owner = await prepare(accounting_db, monkeypatch)
    with pytest.raises(BoardError) as error:
        await GeneralTaskPlanner().propose(None, owner, task_input(), [descriptor()], 1, "intent")
    assert error.value.code == "general_task_planning_secret_input"
    assert (await jobs.inference_accounting_snapshot())["operations"] == []
