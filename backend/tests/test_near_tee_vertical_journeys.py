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
