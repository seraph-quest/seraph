"""ADR-035 #1159: Work and guardian verticals on the NEAR TEE text slot.

OpenRouter stays retired (no legacy marker). Each journey drives one vertical
family through its real owner to its model step on a ready NEAR TEE text slot
and proves:

- the model step reaches the intercepted NEAR gateway with the request
  contract (canonical model id, ``x-no-aliasing: true``, bearer NEAR key,
  identity encoding, the vertical's request shape);
- accounting settles from NEAR ``/v1/billing/costs`` inside the lease (the
  NEAR billing cost, never an OpenRouter usage field);
- the vertical's durable outcome is adopted;
- zero OpenRouter contact: the fixture refuses every other host and the real
  sync/async transports and litellm are backstopped.

No live, paid or catalog network call: every request is intercepted.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from config.settings import settings
from src.model_fabric import retirement
from tests.near_tee_support import (
    NEAR_TEE_TEXT_MODEL,
    NEAR_TEE_VERTICAL_KEY,
    near_inference_requests,
    near_settled_rows,
    prepare_near_tee_vertical,
)
from tests.test_inference_accounting import accounting_db  # noqa: F401
from tests.test_general_task_planner import descriptor, forbid_external_inference  # noqa: F401
from tests.test_general_task_planner import task_input as _legacy_task_input


NEAR_COST_MICROUSD = 1500  # the fixture's 1_500_000 nano-USD billing record


def task_input(**changes):
    """The planner fixture input with a ceiling above the NEAR slot reserve."""
    from src.work_board.contracts import TaskLimits
    return _legacy_task_input(**{"limits": TaskLimits(max_cost_microusd=50_000), **changes})


@pytest.fixture(autouse=True)
def isolated_broker(monkeypatch):
    """A fresh durable broker per journey; an Unknown lease never leaks."""
    from src import llm_runtime
    from src.model_fabric import execution, remote_inference_admission
    broker = remote_inference_admission.RemoteInferenceAdmissionBroker(durable_accounting=True)
    monkeypatch.setattr(remote_inference_admission, "remote_inference_admission_broker", broker)
    monkeypatch.setattr(llm_runtime, "gpu_admission_broker", broker)
    monkeypatch.setattr(execution, "gpu_admission_broker", broker)
    return broker


def assert_near_contract(requests, *, expected_paths=("/v1/chat/completions", "/v1/billing/costs")):
    """The NEAR request contract on every inference and billing request."""
    assert [request.url.raw_path.decode() for request in requests] == list(expected_paths)
    for request in requests:
        assert request.url.host == "cloud-api.near.ai"
        assert request.headers["x-no-aliasing"] == "true"
        assert request.headers["authorization"] == f"Bearer {NEAR_TEE_VERTICAL_KEY}"
        assert request.headers["accept-encoding"] == "identity"
    body = json.loads(requests[0].content)
    assert body["model"] == NEAR_TEE_TEXT_MODEL and body["stream"] is False and body["n"] == 1
    return body


async def operator_owner(monkeypatch, secret="near-vertical-auth"):
    from src.auth.service import create_session
    from src.work_board.contracts import WorkBoardOwner
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", secret)
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    _token, operator = await create_session()
    return operator, WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)


# ── General task / repository work / specialist: the shared planner ──


async def _planner_world(accounting_db, monkeypatch, **near_options):
    from src.db.models import Goal, GoalLevel, GoalStatus
    from src.workflows.job_runtime import DurableJobRepository
    operator, owner = await operator_owner(monkeypatch)
    jobs = DurableJobRepository()
    near = await prepare_near_tee_vertical(monkeypatch, jobs=jobs, **near_options)
    async with accounting_db[2]() as db:
        db.add(Goal(id="goal:fixture", title="Disposable planning goal", level=GoalLevel.daily,
            status=GoalStatus.active, revision=1, owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id))
        await db.commit()
    assert retirement.openrouter_retired() is True
    assert retirement.inactive_route_reason("general_task_planner") is None
    return jobs, owner, near


PLAN = {"schema_version": 1, "revision": 1, "steps": [{"step_id": "read", "tool_id": "fixture.read",
    "input": {"path": "notes.txt"}, "depends_on": [], "output_contract": {"type": "string"}}]}


@pytest.mark.asyncio
async def test_general_task_planning_reaches_near_and_settles_from_near_billing(accounting_db, monkeypatch):
    from src.db.models import WorkBoardTask, WorkflowRunState
    from src.work_board.contracts import TaskProposalGroupV1
    from src.work_board.general_task_planner import GeneralTaskPlanner
    from src.work_board.general_task_proposal import group_entry, proposal_provenance
    from src.work_board.repository import BoardError
    jobs, owner, near = await _planner_world(accounting_db, monkeypatch)
    near.answer = {"role": "assistant", "content": json.dumps(PLAN),
        "reasoning_content": "PRIVATE-REASONING-MUST-NOT-LEAK"}
    async with accounting_db[2]() as db:
        plan = await GeneralTaskPlanner().propose(db, owner, task_input(), [descriptor()], 1, "intent")
    assert plan.steps[0].tool_id == "fixture.read"
    body = assert_near_contract(near_inference_requests(near))
    assert body["temperature"] == 0 and body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == 1024  # the slot's output bound caps the planner's 4096
    assert body["messages"][0]["role"] == "system" and "PlanSpec" in body["messages"][0]["content"]
    # Private evidence identity never leaves; only the public planning envelope does.
    assert "private/evidence" not in requests_text(near) and "secret-ref" not in requests_text(near)
    snapshot = await jobs.inference_accounting_snapshot()
    rows = near_settled_rows(snapshot, "general_task_planner")
    assert len(rows) == 1 and rows[0]["state"] == "settled" and rows[0]["profile_id"] == "near_tee.text"
    assert rows[0]["actual_cost_microusd"] == NEAR_COST_MICROUSD and snapshot["unknown_microusd"] == 0
    # The durable group/provenance contract is unchanged on NEAR.
    group = TaskProposalGroupV1.model_validate(group_entry(rows[0])["group"])
    assert proposal_provenance(rows[0], group).reservation_sequence == rows[0]["sequence"]
    async with accounting_db[2]() as db:
        assert list((await db.execute(select(WorkBoardTask))).scalars()) == []
        roots = list((await db.execute(select(WorkflowRunState))).scalars())
    assert [(root.job_kind, root.status) for root in roots] == [("model_inference_ephemeral_v1", "succeeded")]
    # A replay cannot renew the contacted operation.
    with pytest.raises(BoardError, match="Original planning operation exists"):
        async with accounting_db[2]() as db:
            await GeneralTaskPlanner().propose(db, owner, task_input(), [descriptor()], 1, "intent")
    assert len(near_inference_requests(near)) == 2


def requests_text(near):
    return "".join(request.content.decode("utf-8", "replace") for request in near.requests)


@pytest.mark.asyncio
async def test_general_task_planning_near_echo_mismatch_settles_and_adopts_nothing(accounting_db, monkeypatch):
    from src.work_board.general_task_planner import GeneralTaskPlanner
    from src.work_board.repository import BoardError
    jobs, owner, near = await _planner_world(accounting_db, monkeypatch)
    near.answer = {"role": "assistant", "content": json.dumps(PLAN)}
    near.echo_model = "Qwen/Qwen3-30B-A3B-Instruct-2507"
    with pytest.raises(BoardError) as failed:
        async with accounting_db[2]() as db:
            await GeneralTaskPlanner().propose(db, owner, task_input(), [descriptor()], 1, "intent")
    assert failed.value.code == "general_task_planning_output_not_adopted"
    assert failed.value.extra["reason_code"] == "near_tee_model_mismatch"
    snapshot = await jobs.inference_accounting_snapshot()
    rows = near_settled_rows(snapshot, "general_task_planner")
    # Contact happened: the known NEAR cost settled and the lane is free.
    assert len(rows) == 1 and rows[0]["state"] == "settled"
    assert rows[0]["actual_cost_microusd"] == NEAR_COST_MICROUSD and snapshot["unknown_microusd"] == 0


@pytest.mark.asyncio
async def test_general_task_planning_without_structured_output_proof_is_sealed_precontact(accounting_db,
        monkeypatch):
    from src.db.models import WorkflowRunState
    from src.work_board.general_task_planner import GeneralTaskPlanner
    from src.work_board.repository import BoardError
    jobs, owner, near = await _planner_world(accounting_db, monkeypatch, capabilities=("text",))
    with pytest.raises(BoardError) as denied:
        async with accounting_db[2]() as db:
            await GeneralTaskPlanner().propose(db, owner, task_input(), [descriptor()], 1, "intent")
    assert denied.value.code == "general_task_planning_route_denied"
    assert denied.value.extra["reason_code"] == "proof_missing:structured_output"
    assert near_inference_requests(near) == []
    assert (await jobs.inference_accounting_snapshot())["operations"] == []
    async with accounting_db[2]() as db:
        assert list((await db.execute(select(WorkflowRunState))).scalars()) == []


@pytest.mark.asyncio
async def test_general_task_planning_missing_near_billing_is_unknown_and_blocks_the_lane(accounting_db,
        monkeypatch, isolated_broker):
    from src.model_fabric.gpu_admission import GpuAdmissionUncertainError
    from src.work_board.general_task_planner import GeneralTaskPlanner
    from src.work_board.repository import BoardError
    jobs, owner, near = await _planner_world(accounting_db, monkeypatch)
    near.answer = {"role": "assistant", "content": json.dumps(PLAN)}
    near.billing_missing = True
    with pytest.raises(BoardError) as uncertain:
        async with accounting_db[2]() as db:
            await GeneralTaskPlanner().propose(db, owner, task_input(), [descriptor()], 1, "intent")
    # The existing planner mapping for an uncertain broker outcome; the cause
    # is the NEAR Unknown liability (no authoritative cost arrived).
    assert uncertain.value.code == "general_task_planning_admission_blocked"
    assert isinstance(uncertain.value.__cause__, GpuAdmissionUncertainError)
    assert uncertain.value.__cause__.__cause__.code == "near_tee_cost_liability"
    snapshot = await jobs.inference_accounting_snapshot()
    rows = near_settled_rows(snapshot, "general_task_planner")
    assert len(rows) == 1 and rows[0]["state"] == "unknown" and snapshot["unknown_microusd"] > 0
    receipt = isolated_broker.receipt_for(rows[0]["operation_id"])
    assert receipt.status == "blocked" and receipt.active_operation_id == rows[0]["operation_id"]


# ── Work Board card proposals (triage) ───────────────────────────


def specify_answer():
    """A valid one-card Specify answer whose typed input exists in the workspace."""
    import hashlib
    import os
    from pathlib import Path
    from src.work_board.dispatcher import GOAL_SNAPSHOT_CAPABILITY, REGISTERED_CAPABILITIES
    from src.work_board import triage as triage_service
    raw = json.dumps({"schema_version": 1, "capability_id": GOAL_SNAPSHOT_CAPABILITY,
        "input": {"file_path": "artifacts/specified.md"}}, sort_keys=True, separators=(",", ":")).encode()
    input_path = Path(settings.workspace_dir) / "inputs" / "specified.json"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(settings.workspace_dir, 0o700)
    input_path.write_bytes(raw)
    answer = {"title": "Specified operator task", "body": "A complete bounded execution specification.",
        "capability_id": GOAL_SNAPSHOT_CAPABILITY,
        "capability_version": REGISTERED_CAPABILITIES[GOAL_SNAPSHOT_CAPABILITY].version,
        "typed_input_ref": "workspace-json:inputs/specified.json",
        "typed_input_digest": hashlib.sha256(raw).hexdigest(),
        "executor_id": triage_service.registered_executor_id(GOAL_SNAPSHOT_CAPABILITY)}
    return answer


async def _card_proposal_world(accounting_db, monkeypatch, *, key="card-near", **near_options):
    from src.db.models import WorkBoardStatus
    from src.work_board.contracts import WorkBoardTaskCreate
    from src.work_board.repository import WorkBoardRepository
    from src.workflows.job_runtime import DurableJobRepository
    from tests.test_work_board_m6_provider_free_journey import _goal

    sessions = accounting_db[2].accounting_sessions
    monkeypatch.setattr("src.work_board.triage.get_session", sessions)
    operator, owner = await operator_owner(monkeypatch)
    jobs = DurableJobRepository()
    near = await prepare_near_tee_vertical(monkeypatch, jobs=jobs, **near_options)
    assert retirement.inactive_route_reason("strategist_agent") is None
    goal = _goal("goal-card-near", "Specify a card on NEAR TEE")
    goal.owner_principal_id, goal.owner_session_id = owner.principal_id, owner.session_id
    async with sessions() as db:
        db.add(goal)
    async with sessions() as db:
        created = await WorkBoardRepository().create_task(db, owner, WorkBoardTaskCreate(
            title="Card awaiting a specification", goal_id=goal.id, goal_revision=1,
            status=WorkBoardStatus.triage, idempotency_key=key))
        task = created.task
    answer = specify_answer()
    near.answer = {"role": "assistant", "content": json.dumps(answer)}
    return sessions, jobs, operator, owner, task, near


@pytest.mark.asyncio
async def test_work_board_card_proposal_reaches_near_and_settles_from_near_billing(accounting_db, monkeypatch):
    from src.db.models import WorkBoardProposal, WorkBoardStatus, WorkBoardTask, WorkflowRunState
    from src.work_board.contracts import WorkBoardProposalRequest
    from src.work_board import triage as triage_service
    sessions, jobs, operator, owner, task, near = await _card_proposal_world(accounting_db, monkeypatch)
    request = WorkBoardProposalRequest(expected_revision=task.task_revision, idempotency_key="near-specify")
    result = await triage_service.create_proposal(owner, task.task_id, kind="specify", request=request,
        operator=operator)
    assert result["status"] == "proposed", result
    assert result["provider_contact_state"] == "succeeded"
    assert result["proposed_tasks"][0]["title"] == "Specified operator task"
    body = assert_near_contract(near_inference_requests(near))
    assert body["max_tokens"] == 1024 and body["messages"][0]["role"] == "system"
    assert "Seraph work-board proposal" in body["messages"][0]["content"]
    assert json.loads(body["messages"][1]["content"])["kind"] == "specify"
    snapshot = await jobs.inference_accounting_snapshot()
    rows = near_settled_rows(snapshot, "strategist_agent")
    assert len(rows) == 1 and rows[0]["state"] == "settled" and rows[0]["profile_id"] == "near_tee.text"
    assert rows[0]["actual_cost_microusd"] == NEAR_COST_MICROUSD and snapshot["unknown_microusd"] == 0
    async with sessions() as db:
        proposal = (await db.execute(select(WorkBoardProposal))).scalar_one()
        cards = list((await db.execute(select(WorkBoardTask))).scalars())
        runs = list((await db.execute(select(WorkflowRunState))).scalars())
    # The durable proposal job is bound to the NEAR slot contract and succeeded.
    assert proposal.capability_version == retirement_near_contract()
    assert proposal.provider_contact_started is True and proposal.status == "proposed"
    assert [(run.job_kind, run.status) for run in runs] == [("work_board_proposal", "succeeded")]
    assert rows[0]["job_id"] == proposal.admission_job_id
    # Advisory only: the parent card is unchanged until explicit acceptance.
    assert [(card.task_id, card.status) for card in cards] == [(task.task_id, WorkBoardStatus.triage)]
    # The idempotent replay returns the same proposal without a second contact.
    replay = await triage_service.create_proposal(owner, task.task_id, kind="specify", request=request,
        operator=operator)
    assert replay["proposal_id"] == result["proposal_id"] and replay["status"] == "proposed"
    assert len(near_inference_requests(near)) == 2


def retirement_near_contract():
    from src.model_fabric.near_tee_vertical import near_tee_vertical_route
    return near_tee_vertical_route("strategist_agent").profile_contract_hash


@pytest.mark.asyncio
async def test_work_board_card_proposal_near_echo_mismatch_blocks_with_settled_cost(accounting_db, monkeypatch):
    from src.work_board.contracts import WorkBoardProposalRequest
    from src.work_board import triage as triage_service
    sessions, jobs, operator, owner, task, near = await _card_proposal_world(accounting_db, monkeypatch)
    near.echo_model = "Qwen/Qwen3-30B-A3B-Instruct-2507"
    result = await triage_service.create_proposal(owner, task.task_id, kind="specify",
        request=WorkBoardProposalRequest(expected_revision=task.task_revision, idempotency_key="near-echo"),
        operator=operator)
    assert result["status"] == "blocked" and result["blocked_reason"] == "near_tee_model_mismatch", result
    assert result["proposed_tasks"] == []
    rows = near_settled_rows(await jobs.inference_accounting_snapshot(), "strategist_agent")
    assert len(rows) == 1 and rows[0]["state"] == "settled"
    assert rows[0]["actual_cost_microusd"] == NEAR_COST_MICROUSD


@pytest.mark.asyncio
async def test_work_board_card_proposal_missing_near_proof_is_sealed_precontact(accounting_db, monkeypatch):
    from src.db.models import WorkflowRunState
    from src.work_board.contracts import WorkBoardProposalRequest
    from src.work_board import triage as triage_service
    # The strategist route requires structured output; without its proof the
    # sealed precontact gate refuses before any job, reservation or contact.
    sessions, jobs, operator, owner, task, near = await _card_proposal_world(accounting_db, monkeypatch,
        capabilities=("text",))
    result = await triage_service.create_proposal(owner, task.task_id, kind="specify",
        request=WorkBoardProposalRequest(expected_revision=task.task_revision, idempotency_key="near-proof"),
        operator=operator)
    assert result["status"] == "blocked" and result["blocked_reason"] == "proof_missing:structured_output", result
    assert result["provider_contact_state"] == "not_started"
    assert near_inference_requests(near) == []
    assert (await jobs.inference_accounting_snapshot())["operations"] == []
    async with sessions() as db:
        assert list((await db.execute(select(WorkflowRunState))).scalars()) == []
