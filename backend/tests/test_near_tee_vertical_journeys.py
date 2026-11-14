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


# ── Guardian opportunity assessment ──────────────────────────────


from tests.test_work_board_m6_provider_free_journey import isolated_runtime  # noqa: E402,F401


async def _guardian_world(isolated_runtime, monkeypatch, tmp_path, *, plans_per_day=0):
    from pathlib import Path
    from src.guardian.opportunity_runtime import read_snapshot
    from src.workspace.production import ProductionWorkspace, prepare_lifecycle_directory
    from tests import test_guardian_opportunity_policy as policy_fixture
    if plans_per_day:
        # The reviewed policy also permits plan proposals for this Goal.
        original = policy_fixture.setup_policy

        async def setup_policy(runtime, **kwargs):
            sessions, goal, watch, request, body = await original(runtime, **kwargs)
            policy = body.policy.model_copy(update={"max_plan_proposals_per_utc_day": plans_per_day})
            return sessions, goal, watch, request, body.model_copy(update={"policy": policy})
        monkeypatch.setattr(policy_fixture, "setup_policy", setup_policy)
    sessions, goal, _watch, _request, row, packet = await policy_fixture.publish_source(isolated_runtime)
    # The model-fabric receipt/proof store binds its session at import.
    monkeypatch.setattr("src.model_fabric.repository.get_session", sessions)
    monkeypatch.setenv("SERAPH_WORKSPACE_LIFECYCLE_PATH", str(tmp_path / "guardian-lifecycle"))
    prepare_lifecycle_directory(ProductionWorkspace(host_root=Path(settings.workspace_dir)))
    offered = read_snapshot(packet.opportunity_snapshot_artifact_id, packet.opportunity_snapshot_sha256)
    return sessions, row, offered


def assessment_answer(offered):
    source = offered.sources[0]
    return {"role": "assistant", "content": json.dumps({"schema_version": "seraph.opportunity.assessment.v1",
        "relevance": 3, "confidence": "medium", "summary": "A relevant cited public release is available.",
        "reason": "The supplied public release changed and warrants an operator review.",
        "citations": [{"source_id": source.source_key, "start_line": 1,
            "end_line": len(source.excerpt.split("\n")), "span_sha256": source.excerpt_sha256}],
        "suggested_blueprint": "public-browser-check", "abstain_reason": None})}


@pytest.mark.asyncio
async def test_guardian_assessment_reaches_near_and_settles_from_near_billing(isolated_runtime, monkeypatch,
        tmp_path):
    from src.db.models import GuardianOpportunity
    from src.guardian import opportunity_runtime
    from src.workflows.job_runtime import DurableJobRepository
    sessions, row, offered = await _guardian_world(isolated_runtime, monkeypatch, tmp_path)
    jobs = DurableJobRepository()
    near = await prepare_near_tee_vertical(monkeypatch, jobs=jobs)
    assert opportunity_runtime.assessment_route_block() is None
    near.answer = assessment_answer(offered)
    await opportunity_runtime.execute_assessment(row.id)
    async with sessions() as db:
        final = await db.get(GuardianOpportunity, row.id)
    assert final.status == "proposed", (final.status, final.reason_code)
    body = assert_near_contract(near_inference_requests(near))
    assert body["temperature"] == 0 and body["response_format"] == {"type": "json_object"}
    assert "seraph.opportunity.assessment.v1" in body["messages"][0]["content"]
    rows = near_settled_rows(await jobs.inference_accounting_snapshot(), "strategist_agent")
    assert len(rows) == 1 and rows[0]["state"] == "settled" and rows[0]["profile_id"] == "near_tee.text"
    assert rows[0]["actual_cost_microusd"] == NEAR_COST_MICROUSD and rows[0]["job_id"] == final.job_id
    job = await jobs.get_job(final.job_id)
    # The broker's admission receipt is adopted into the assessment job, then
    # the verified assessment write.
    assert job["status"] == "succeeded" and [effect["effect_type"] for effect in job["effects"]] == [
        "remote_inference_admission", "workspace_write"]


@pytest.mark.asyncio
async def test_guardian_assessment_near_echo_mismatch_blocks_with_settled_cost(isolated_runtime, monkeypatch,
        tmp_path):
    from src.db.models import GuardianOpportunity
    from src.guardian import opportunity_runtime
    from src.workflows.job_runtime import DurableJobRepository
    sessions, row, offered = await _guardian_world(isolated_runtime, monkeypatch, tmp_path)
    jobs = DurableJobRepository()
    near = await prepare_near_tee_vertical(monkeypatch, jobs=jobs)
    near.answer = assessment_answer(offered)
    near.echo_model = "Qwen/Qwen3-30B-A3B-Instruct-2507"
    await opportunity_runtime.execute_assessment(row.id)
    async with sessions() as db:
        final = await db.get(GuardianOpportunity, row.id)
    # Contact happened and its known NEAR cost settled: an honest block.
    assert (final.status, final.reason_code) == ("blocked", "near_tee_model_mismatch")
    rows = near_settled_rows(await jobs.inference_accounting_snapshot(), "strategist_agent")
    assert len(rows) == 1 and rows[0]["state"] == "settled"


@pytest.mark.asyncio
async def test_guardian_held_candidate_resumes_on_near_route_and_blocks_at_deadline(isolated_runtime,
        monkeypatch, tmp_path):
    """#1156 follow-up: a candidate held queued for a route block resumes once
    the NEAR TEE route exists; a held candidate still closes at its deadline."""
    import asyncio
    from datetime import datetime, timedelta, timezone
    from src.db.models import GuardianOpportunity
    from src.guardian import opportunity_runtime
    from src.workflows.job_runtime import DurableJobRepository
    sessions, row, offered = await _guardian_world(isolated_runtime, monkeypatch, tmp_path)
    # No provider: held queued with the visible reason, no durable job.
    tick = await opportunity_runtime.run_opportunity_tick()
    async with sessions() as db:
        held = await db.get(GuardianOpportunity, row.id)
    assert tick["started"] == 0
    assert (held.status, held.reason_code, held.job_id) == ("queued", retirement.NO_ACTIVE_PROVIDER_REASON, None)
    # A NEAR TEE route appears: the next tick starts the held candidate.
    jobs = DurableJobRepository()
    near = await prepare_near_tee_vertical(monkeypatch, jobs=jobs)
    near.answer = assessment_answer(offered)
    tick = await opportunity_runtime.run_opportunity_tick()
    assert tick["started"] == 1
    await asyncio.gather(*list(opportunity_runtime._executions.values()))
    async with sessions() as db:
        resumed = await db.get(GuardianOpportunity, row.id)
    assert resumed.status == "proposed", (resumed.status, resumed.reason_code)
    assert len(near_inference_requests(near)) == 2


@pytest.mark.asyncio
async def test_guardian_held_candidate_blocks_at_deadline_while_held(isolated_runtime, monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone
    from src.db.models import GuardianOpportunity
    from src.guardian import opportunity_runtime
    sessions, row, _offered = await _guardian_world(isolated_runtime, monkeypatch, tmp_path)
    await opportunity_runtime.run_opportunity_tick()
    async with sessions() as db:
        held = await db.get(GuardianOpportunity, row.id)
        assert (held.status, held.reason_code) == ("queued", retirement.NO_ACTIVE_PROVIDER_REASON)
        held.assessment_deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.add(held)
    tick = await opportunity_runtime.run_opportunity_tick()
    async with sessions() as db:
        closed = await db.get(GuardianOpportunity, row.id)
    assert tick["started"] == 0
    assert (closed.status, closed.reason_code, closed.job_id) == ("blocked", "assessment_deadline_expired", None)


async def _assessed_opportunity(isolated_runtime, monkeypatch, tmp_path):
    """A proposed opportunity assessed on NEAR, ready for plan generation."""
    from src.db.models import GuardianOpportunity
    from src.guardian import opportunity_runtime
    from src.workflows.job_runtime import DurableJobRepository
    from tests.test_work_board_m6_provider_free_journey import SESSION
    monkeypatch.setattr(settings, "browser_site_allowlist", "example.com")
    sessions, row, offered = await _guardian_world(isolated_runtime, monkeypatch, tmp_path, plans_per_day=1)
    monkeypatch.setattr("src.work_board.triage.get_session", sessions)
    async with sessions() as db:
        adapter = type(db)

    async def scalars(self, statement, *args, **kwargs):
        # The isolated synchronous-SQLite adapter mirrors AsyncSession.scalars.
        return self._session.scalars(statement, *args, **kwargs)
    monkeypatch.setattr(adapter, "scalars", scalars, raising=False)

    def begin(self):
        # ... and AsyncSession.begin (the Work input-artifact writer uses it).
        return self._NestedTransaction(self._session) if self._session.in_transaction() else _Begin(self._session)
    monkeypatch.setattr(adapter, "begin", begin, raising=False)
    jobs = DurableJobRepository()
    near = await prepare_near_tee_vertical(monkeypatch, jobs=jobs)
    near.answer = assessment_answer(offered)
    await opportunity_runtime.execute_assessment(row.id)
    async with sessions() as db:
        opportunity = await db.get(GuardianOpportunity, row.id)
    assert opportunity.status == "proposed", (opportunity.status, opportunity.reason_code)
    from src.auth.service import authenticate_token
    # The isolated root's bearer: plan generation rechecks the exact token.
    operator = await authenticate_token("m6-provider-free-root", touch=False)
    assert operator.session_id == SESSION
    source = offered.sources[0]
    near.answer = {"role": "assistant", "content": json.dumps({"schema_version": "seraph.opportunity.plan.v1",
        "blueprint_id": "public-browser-check", "title": "Review the cited public release",
        "reason": "Read the bound public source and retain a local result.",
        "citations": [{"source_id": source.source_key, "start_line": 1,
            "end_line": len(source.excerpt.split("\n")), "span_sha256": source.excerpt_sha256}]})}
    request = {"expected_opportunity_revision": opportunity.revision,
        "expected_goal_revision": opportunity.goal_revision, "idempotency_key": "4d6f1f0e-5b7a-4c1e-9f1d-1159a0000001"}
    return sessions, jobs, near, operator, opportunity, request


class _Begin:
    def __init__(self, session):
        self._session = session
        self._transaction = None

    async def __aenter__(self):
        self._transaction = self._session.begin()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            self._transaction.commit()
        else:
            self._transaction.rollback()
        return None


@pytest.mark.asyncio
async def test_guardian_plan_generation_reaches_near_and_settles_from_near_billing(isolated_runtime, monkeypatch,
        tmp_path):
    from src.db.models import WorkBoardProposal
    from src.guardian import opportunity_plans
    sessions, jobs, near, operator, opportunity, request = await _assessed_opportunity(isolated_runtime,
        monkeypatch, tmp_path)
    result = await opportunity_plans.generate_plan(operator=operator, opportunity_id=opportunity.id, request=request)
    assert result["reason_code"] is None, result
    plan_requests = near_inference_requests(near)[2:]
    body = assert_near_contract(plan_requests)
    assert body["response_format"] == {"type": "json_object"} and body["max_tokens"] == 1024
    assert "seraph.opportunity.plan.v1" in body["messages"][0]["content"]
    async with sessions() as db:
        proposal = (await db.execute(select(WorkBoardProposal))).scalar_one()
    assert proposal.status == "proposed" and proposal.provider_contact_state == "succeeded", (
        proposal.status, proposal.provider_contact_state, proposal.proposal_json)
    assert json.loads(proposal.proposal_json)["blueprint_id"] == "public-browser-check"
    rows = [row for row in near_settled_rows(await jobs.inference_accounting_snapshot(), "strategist_agent")
        if row["job_id"] == proposal.admission_job_id]
    assert len(rows) == 1 and rows[0]["state"] == "settled"
    assert rows[0]["actual_cost_microusd"] == NEAR_COST_MICROUSD
    assert (await jobs.get_job(proposal.admission_job_id))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_guardian_plan_route_denial_is_an_honest_block_not_outcome_unknown(isolated_runtime, monkeypatch,
        tmp_path):
    """#1156 open risk: a route denial before admission names its reason."""
    from src.db.models import WorkBoardProposal
    from src.guardian import opportunity_plans
    from src.model_fabric import caller_context
    from src.model_fabric.contracts import NoCompliantModelRouteError
    sessions, jobs, near, operator, opportunity, request = await _assessed_opportunity(isolated_runtime,
        monkeypatch, tmp_path)
    before = len(near.requests)

    def denied(*_args, **_kwargs):
        raise NoCompliantModelRouteError(("near_tee_receipt_stale",))
    monkeypatch.setattr(caller_context, "build_canonical_inference_context", denied)
    result = await opportunity_plans.generate_plan(operator=operator, opportunity_id=opportunity.id, request=request)
    async with sessions() as db:
        proposal = (await db.execute(select(WorkBoardProposal))).scalar_one()
    assert json.loads(proposal.proposal_json)["blocked_reason"] == "near_tee_receipt_stale", proposal.proposal_json
    assert proposal.provider_contact_started is False and len(near.requests) == before
    assert result["reason_code"] in (None, "near_tee_receipt_stale")


# ── Research dossier (two children, one public source) ───────────


class ResearchNearFixture:
    """The NEAR fixture plus the one named public source behind its pin."""

    def __new__(cls):
        from tests.test_near_tee_text_slot import NearFixture
        from tests.test_research_native_vertical import SOURCE_FIXTURE, SOURCE_HOST, SOURCE_PATH, SOURCE_PIN

        class _Fixture(NearFixture):
            def __init__(self):
                super().__init__()
                self.source_reads = []

            def handler(self, request):
                import httpx
                if request.url.host == SOURCE_PIN:
                    assert request.method == "GET" and request.url.path == SOURCE_PATH
                    assert request.headers["host"] == SOURCE_HOST
                    self.source_reads.append(str(request.url))
                    return httpx.Response(200, headers={"content-type": "text/plain;charset=utf-8"},
                        content=SOURCE_FIXTURE)
                if request.url.path == "/v1/chat/completions":
                    # Answer each perspective with exact citations of its quote.
                    supplied = json.loads(json.loads(request.content)["messages"][1]["content"])
                    source = supplied["untrusted_quoted_sources"][0]
                    self.answer = {"role": "assistant", "content": json.dumps({"schema_version": 1,
                        "perspective": supplied["perspective_instruction"],
                        "claims": [{"text": "The selected public text establishes this attributed evidence.",
                            "citations": [{key: source[key] for key in
                                ("source_id", "first_line", "last_line", "span_sha256")}]}],
                        "uncertainty": ["Mechanical citation validation does not establish semantic truth."],
                        "contradictions": [], "no_learning": True})}
                    self.body_id = "chatcmpl-research-" + str(len(self.requests))
                return super().handler(request)
        return _Fixture()


def install_research_source(monkeypatch):
    """The named public source resolves to its fixed pin (no live DNS)."""
    from src.browser import pinned_transport
    from tests.test_research_native_vertical import SOURCE_HOST, SOURCE_PIN
    monkeypatch.setattr(settings, "browser_site_allowlist", SOURCE_HOST)

    def named_source_dns(host, port):
        assert host == SOURCE_HOST and port == 443
        return [SOURCE_PIN]
    monkeypatch.setattr(pinned_transport, "_blocking_default_resolver", named_source_dns)


@pytest.mark.asyncio
async def test_research_dossier_children_reach_near_and_settle_from_near_billing(accounting_db, monkeypatch):
    from src.db.models import WorkBoardStatus, WorkBoardTask, WorkflowRunState
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.work_board.repository import WorkBoardRepository
    from src.workflows.job_runtime import DurableJobRepository
    from tests.test_vertical_no_provider_journeys import _seed_research, _todo_capability_task
    install_research_source(monkeypatch)
    sessions = accounting_db[2].accounting_sessions
    operator, owner = await operator_owner(monkeypatch)
    jobs = DurableJobRepository()
    near = await prepare_near_tee_vertical(monkeypatch, jobs=jobs, capabilities=("text",),
        near=ResearchNearFixture())
    assert retirement.inactive_route_reason("readonly_research_child") is None
    goal_id, typed_input = await _seed_research(sessions, owner)
    task_id = await _todo_capability_task(sessions, owner, capability="work.research-dossier.v1",
        goal_id=goal_id, typed_input=typed_input, key="research-near")
    dispatcher = WorkBoardDispatcher(repository=WorkBoardRepository(), jobs=jobs, session_provider=sessions,
        runner_id="service:work-board")
    receipt = await dispatcher.run_pass()
    async with sessions() as db:
        task = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))).scalar_one()
        runs = list((await db.execute(select(WorkflowRunState))).scalars())
    assert near.source_reads, "the public source was read through its pin"
    inference = near_inference_requests(near)
    assert inference, (receipt, task.status, task.block_reason)
    # Two perspectives: one NEAR completion + billing readback per child.
    assert_near_contract(inference[:2])
    assert [request.url.raw_path.decode() for request in inference] == [
        "/v1/chat/completions", "/v1/billing/costs"] * 2
    bodies = [json.loads(request.content) for request in inference[::2]]
    assert all(body["max_tokens"] == 1024 and "response_format" not in body for body in bodies)
    snapshot = await jobs.inference_accounting_snapshot()
    rows = near_settled_rows(snapshot, "readonly_research_child")
    assert len(rows) == 2 and {row["state"] for row in rows} == {"settled"}, rows
    assert all(row["profile_id"] == "near_tee.text" and row["actual_cost_microusd"] == NEAR_COST_MICROUSD
        for row in rows)
    assert snapshot["unknown_microusd"] == 0
    children = [run for run in runs if run.job_kind == "readonly_research_child"]
    parent = next(run for run in runs if run.job_kind == "research_dossier")
    # Each child's stored prompt digest is the canonical digest of the exact
    # NEAR body sent, and the reservation binds the same digest.
    from src.security.trust_contract import canonical_digest
    from src.workflows.job_runtime import _serialize
    from src.workflows.research_native import checkpoint
    sent = {canonical_digest(body) for body in bodies}
    ready = [checkpoint(_serialize(child), "research:prompt-ready") for child in children]
    assert {item["payload_digest"] for item in ready} == sent and len(sent) == 2
    assert {row["payload_digest"] for row in rows} == sent
    assert [run.status for run in children] == ["succeeded", "succeeded"], [(r.job_kind, r.status) for r in runs]
    assert parent.status == "succeeded", parent.status
    # The verified dossier is adopted and the card completes.
    assert receipt.get("completed", 0) == 1 and task.status is WorkBoardStatus.done, (receipt, task.status)


# ── Mail reply draft ─────────────────────────────────────────────


MAIL_OUTPUT = {"subject": "Re: Architecture review", "body": "Thanks — I will follow up next week.",
    "caveats": ["Review before sending."]}


async def _mail_world(accounting_db, monkeypatch, key):
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.work_board.repository import WorkBoardRepository
    from src.workflows.job_runtime import DurableJobRepository
    from tests import test_mail_reply_vertical as mail
    monkeypatch.setattr(mail.vault_crypto, "_fernet", None)
    sessions = accounting_db[2].accounting_sessions
    await mail._seed(sessions, monkeypatch)
    jobs = DurableJobRepository()
    near = await prepare_near_tee_vertical(monkeypatch, jobs=jobs)
    near.answer = {"role": "assistant", "content": json.dumps(MAIL_OUTPUT)}
    created = await mail.mail_api.create_reply_task(mail._request(mail._reply_body(key)))
    assert created.status_code == 201, created.body
    task_id = json.loads(created.body)["task_id"]

    class SourceAdapter:
        reads = 0

        def __init__(self, _connection, *, contact_observer=None, **_kwargs):
            self.contact_observer = contact_observer

        async def get_message_full(self, _provider_message_id):
            type(self).reads += 1
            if self.contact_observer:
                self.contact_observer()
            return mail._body(type(self).reads)
    monkeypatch.setattr("src.integrations.gmail_read.GoogleGmailReadonlyAdapter", SourceAdapter)
    dispatcher = WorkBoardDispatcher(repository=WorkBoardRepository(), jobs=jobs, session_provider=sessions,
        runner_id="service:work-board")
    return sessions, jobs, near, task_id, dispatcher, SourceAdapter


async def _task(sessions, task_id):
    from src.db.models import WorkBoardTask
    async with sessions() as db:
        return (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))).scalar_one()


@pytest.mark.asyncio
async def test_mail_reply_reaches_near_and_settles_from_near_billing(accounting_db, monkeypatch):
    from src.db.models import WorkBoardStatus
    sessions, jobs, near, task_id, dispatcher, SourceAdapter = await _mail_world(accounting_db, monkeypatch,
        "reply-near")
    receipt = await dispatcher.run_pass()
    task = await _task(sessions, task_id)
    assert receipt["completed"] == 1 and task.status is WorkBoardStatus.done, (receipt, task.block_reason)
    assert SourceAdapter.reads == 2
    body = assert_near_contract(near_inference_requests(near))
    assert body["response_format"] == {"type": "json_object"}
    assert "Draft a safe email reply" in body["messages"][0]["content"]
    rows = near_settled_rows(await jobs.inference_accounting_snapshot(), "strategist_agent")
    assert len(rows) == 1 and rows[0]["state"] == "settled" and rows[0]["profile_id"] == "near_tee.text"
    assert rows[0]["actual_cost_microusd"] == NEAR_COST_MICROUSD
    job = await jobs.get_job(rows[0]["job_id"])
    assert job["job_kind"] == "mail_reply_draft" and job["status"] == "succeeded"


@pytest.mark.asyncio
async def test_mail_reply_truncated_near_answer_is_named_and_settled(accounting_db, monkeypatch):
    """A JSON draft cut at the slot's output bound is near_tee_output_truncated:
    contact happened, the NEAR cost settled, nothing is adopted."""
    from src.db.models import WorkBoardStatus
    sessions, jobs, near, task_id, dispatcher, _source = await _mail_world(accounting_db, monkeypatch,
        "reply-near-truncated")
    near.finish_reason = "length"
    near.answer = {"role": "assistant", "content": '{"subject": "Re: Archi'}
    receipt = await dispatcher.run_pass()
    task = await _task(sessions, task_id)
    rows = near_settled_rows(await jobs.inference_accounting_snapshot(), "strategist_agent")
    assert len(rows) == 1 and rows[0]["state"] == "settled", rows
    assert task.status is WorkBoardStatus.blocked and task.block_reason == "near_tee_output_truncated"


@pytest.mark.asyncio
async def test_mail_reply_missing_near_billing_is_unknown_and_blocks_the_lane(accounting_db, monkeypatch,
        isolated_broker):
    sessions, jobs, near, task_id, dispatcher, _source = await _mail_world(accounting_db, monkeypatch,
        "reply-near-unknown")
    near.billing_missing = True
    receipt = await dispatcher.run_pass()
    task = await _task(sessions, task_id)
    assert task.block_kind == "unknown_effect"
    assert_unknown_liability_blocks_lane(await jobs.inference_accounting_snapshot(), isolated_broker,
        "strategist_agent")


@pytest.mark.asyncio
async def test_mail_reply_near_reservation_resumes_after_crash_before_contact(accounting_db, monkeypatch,
        isolated_broker):
    from datetime import datetime, timedelta, timezone
    from src.db.models import WorkBoardStatus, WorkflowRunState
    from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.work_board.repository import WorkBoardRepository
    from src.workflows.job_runtime import DurableJobRepository
    sessions, jobs, near, task_id, dispatcher, SourceAdapter = await _mail_world(accounting_db, monkeypatch,
        "reply-near-crash")

    class WorkerCrash(BaseException):
        pass
    original_prepare = isolated_broker._prepare_accounting

    async def crash_after_reservation(request):
        await original_prepare(request)
        raise WorkerCrash()
    monkeypatch.setattr(isolated_broker, "_prepare_accounting", crash_after_reservation)
    with pytest.raises(WorkerCrash):
        await dispatcher.run_pass()
    before = (await DurableJobRepository().inference_accounting_snapshot())["operations"]
    assert len(before) == 1 and before[0]["state"] == "reserved" and before[0]["profile_id"] == "near_tee.text"
    assert near_inference_requests(near) == []
    async with sessions() as db:
        run = (await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == before[0]["job_id"]))).scalar_one()
        run.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await accounting_db[1].dispose()
    recovered = await DurableJobRepository().recover_stale_jobs()
    assert recovered[0]["status"] == "queued", recovered
    restarted = RemoteInferenceAdmissionBroker(durable_accounting=True)
    from src import llm_runtime
    from src.model_fabric import execution, remote_inference_admission
    monkeypatch.setattr(llm_runtime, "gpu_admission_broker", restarted)
    monkeypatch.setattr(execution, "gpu_admission_broker", restarted)
    monkeypatch.setattr(remote_inference_admission, "remote_inference_admission_broker", restarted)
    dispatcher = WorkBoardDispatcher(repository=WorkBoardRepository(), jobs=DurableJobRepository(),
        session_provider=sessions, runner_id="service:work-board")
    reconciled = await dispatcher.reconcile_linked_attempts()
    assert before[0]["job_id"] in reconciled, reconciled
    task = await _task(sessions, task_id)
    assert task.status is WorkBoardStatus.done, (task.status, task.block_reason)
    assert len(near_inference_requests(near)) == 2  # one NEAR call + its billing readback
    rows = near_settled_rows(await DurableJobRepository().inference_accounting_snapshot(), "strategist_agent")
    assert len(rows) == 1 and rows[0]["state"] == "settled"
    assert (rows[0]["operation_id"], rows[0]["sequence"]) == (before[0]["operation_id"], before[0]["sequence"])


def assert_unknown_liability_blocks_lane(snapshot, broker, runtime_path):
    """Contact happened and no NEAR cost arrived: an Unknown liability that
    keeps the serial lease blocked (never settled, never released)."""
    rows = near_settled_rows(snapshot, runtime_path)
    assert len(rows) == 1 and rows[0]["state"] == "unknown" and snapshot["unknown_microusd"] > 0, rows
    receipt = broker.receipt_for(rows[0]["operation_id"])
    assert receipt.status == "blocked" and receipt.active_operation_id == rows[0]["operation_id"]


# ── Calendar meeting prep ────────────────────────────────────────


def _find_key(value, key):
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for item in value.values():
            if (found := _find_key(item, key)) is not None:
                return found
    return None


class CalendarNearFixture:
    """The NEAR fixture answering meeting prep for the offered event identity."""

    def __new__(cls):
        from tests.test_near_tee_text_slot import NearFixture

        class _Fixture(NearFixture):
            def handler(self, request):
                if request.url.path == "/v1/chat/completions":
                    offered = json.loads(json.loads(request.content)["messages"][-1]["content"])
                    self.answer = {"role": "assistant", "content": json.dumps({"schema_version": 1,
                        "event_key": _find_key(offered, "event_key"),
                        "event_revision": _find_key(offered, "event_revision"),
                        "summary": "Review the architecture decisions and open risks.",
                        "agenda": ["Decisions", "Risks"], "questions": ["What remains unresolved?"],
                        "risks": ["Unbounded scope"], "preparation_steps": ["Read the current design notes"]})}
                return super().handler(request)
        return _Fixture()


async def _calendar_world(accounting_db, monkeypatch, key="calendar-near"):
    from types import SimpleNamespace
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.work_board.repository import WorkBoardRepository
    from src.workflows.job_runtime import DurableJobRepository
    from tests.test_calendar_manual_vertical import _provider_event
    from tests.test_vertical_no_provider_journeys import _seed_calendar, _todo_capability_task
    sessions = accounting_db[2].accounting_sessions
    operator, owner = await operator_owner(monkeypatch)
    jobs = DurableJobRepository()
    near = await prepare_near_tee_vertical(monkeypatch, jobs=jobs, near=CalendarNearFixture())
    goal_id, typed_input = await _seed_calendar(sessions, owner)
    task_id = await _todo_capability_task(sessions, owner, capability="calendar.meeting-prep.v1",
        goal_id=goal_id, typed_input=typed_input, key=key)
    calendar_reads = []
    from src.integrations.google_calendar import vault_repository
    vault_read = vault_repository.get

    async def vault_get(key, *args, **kwargs):
        # The shared vault object also serves the NEAR key; only the Calendar
        # OAuth secret is a fixture.
        if key != "vault:calendar-no-provider":
            return await vault_read(key, *args, **kwargs)
        return json.dumps({"client_id": "calendar-client", "refresh_token": "calendar-refresh"})

    async def calendar_request(url, **kwargs):
        calendar_reads.append((str(kwargs.get("method", "GET")), url))
        if kwargs.get("method") == "POST":
            return SimpleNamespace(status_code=200, headers={"content-type": "application/json"},
                content=b'{"access_token":"calendar-access"}')
        return SimpleNamespace(status_code=200, headers={"content-type": "application/json"},
            content=json.dumps(_provider_event()).encode())
    monkeypatch.setattr("src.integrations.google_calendar.vault_repository.get", vault_get)
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", calendar_request)
    repository = WorkBoardRepository()
    # The Calendar capability executes through the direct durable adapter
    # once its card is claimed (as the managed Calendar schedule does).
    async with sessions() as db:
        current = await repository.get_task(db, owner, task_id)
        promoted = await repository.promote_task_ready(db, task_id, expected_revision=current.task_revision,
            actor_principal_id=owner.principal_id, actor_session_id=owner.session_id)
        await db.commit()
    async with sessions() as db:
        claim = await repository.claim_ready_task(db, task_id, expected_revision=promoted.task.task_revision,
            lease_owner="service:work-board", lease_seconds=300, actor_principal_id="service:work-board",
            actor_session_id="service-session:work-board")
        await db.commit()
    dispatcher = WorkBoardDispatcher(repository=repository, jobs=jobs, session_provider=sessions)
    return sessions, jobs, near, task_id, claim, typed_input, dispatcher, calendar_reads


def assert_calendar_on_near(near, calendar_reads, snapshot, *, event_reads=2):
    # Two event reads around the model (plus any read before a crash).
    assert len([read for read in calendar_reads if read[0] == "GET"]) == event_reads
    body = assert_near_contract(near_inference_requests(near))
    assert body["response_format"] == {"type": "json_object"} and body["max_tokens"] == 1024
    rows = near_settled_rows(snapshot, "strategist_agent")
    assert len(rows) == 1 and rows[0]["state"] == "settled" and rows[0]["profile_id"] == "near_tee.text"
    assert rows[0]["actual_cost_microusd"] == NEAR_COST_MICROUSD
    return rows[0]


@pytest.mark.asyncio
async def test_calendar_meeting_prep_reaches_near_and_settles_from_near_billing(accounting_db, monkeypatch):
    from src.db.models import WorkBoardStatus
    sessions, jobs, near, task_id, claim, typed_input, dispatcher, calendar_reads = await _calendar_world(
        accounting_db, monkeypatch)
    result = await dispatcher._admit_execute_direct(claim, typed_input, runtime_seconds=120)
    task = await _task(sessions, task_id)
    assert result["completed"] is True and task.status is WorkBoardStatus.done, (result, task.block_reason)
    row = assert_calendar_on_near(near, calendar_reads, await jobs.inference_accounting_snapshot())
    job = await jobs.get_job(row["job_id"])
    assert job["job_kind"] == "calendar_meeting_prep" and job["status"] == "succeeded"


@pytest.mark.asyncio
async def test_calendar_meeting_prep_missing_near_billing_is_unknown_and_blocks_the_lane(accounting_db,
        monkeypatch, isolated_broker):
    sessions, jobs, near, task_id, claim, typed_input, dispatcher, _reads = await _calendar_world(
        accounting_db, monkeypatch, key="calendar-near-unknown")
    near.billing_missing = True
    result = await dispatcher._admit_execute_direct(claim, typed_input, runtime_seconds=120)
    task = await _task(sessions, task_id)
    assert result["completed"] is False and task.block_kind == "unknown_effect", (result, task.block_reason)
    assert_unknown_liability_blocks_lane(await jobs.inference_accounting_snapshot(), isolated_broker,
        "strategist_agent")


@pytest.mark.asyncio
async def test_calendar_meeting_prep_near_reservation_resumes_after_crash_before_contact(accounting_db,
        monkeypatch, isolated_broker):
    """A worker crash after the NEAR reservation and before contact resumes
    the same reservation after restart (inference_accounting typed resume):
    one NEAR call, the original operation and sequence, settled from NEAR."""
    from datetime import datetime, timedelta, timezone
    from src.db.models import WorkflowRunState
    from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.workflows.job_runtime import DurableJobRepository
    sessions, jobs, near, task_id, claim, typed_input, dispatcher, calendar_reads = await _calendar_world(
        accounting_db, monkeypatch, key="calendar-near-crash")

    class WorkerCrash(BaseException):
        pass
    original_prepare = isolated_broker._prepare_accounting

    async def crash_after_reservation(request):
        await original_prepare(request)
        raise WorkerCrash()
    monkeypatch.setattr(isolated_broker, "_prepare_accounting", crash_after_reservation)
    with pytest.raises(WorkerCrash):
        await dispatcher._admit_execute_direct(claim, typed_input, runtime_seconds=120)
    before = (await DurableJobRepository().inference_accounting_snapshot())["operations"]
    assert len(before) == 1 and before[0]["state"] == "reserved" and before[0]["profile_id"] == "near_tee.text"
    assert near_inference_requests(near) == []
    async with sessions() as db:
        run = (await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == before[0]["job_id"]))).scalar_one()
        run.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await accounting_db[1].dispose()
    recovered = await DurableJobRepository().recover_stale_jobs()
    assert recovered[0]["status"] == "queued", recovered
    restarted = RemoteInferenceAdmissionBroker(durable_accounting=True)
    from src import llm_runtime
    from src.model_fabric import execution, remote_inference_admission
    monkeypatch.setattr(llm_runtime, "gpu_admission_broker", restarted)
    monkeypatch.setattr(execution, "gpu_admission_broker", restarted)
    monkeypatch.setattr(remote_inference_admission, "remote_inference_admission_broker", restarted)
    dispatcher = WorkBoardDispatcher(repository=dispatcher.repository, jobs=DurableJobRepository(),
        session_provider=sessions)
    reconciled = await dispatcher.reconcile_linked_attempts()
    assert before[0]["job_id"] in reconciled
    after = await DurableJobRepository().inference_accounting_snapshot()
    # The crashed attempt read the event once before its reservation.
    row = assert_calendar_on_near(near, calendar_reads, after, event_reads=3)
    assert (row["operation_id"], row["sequence"]) == (before[0]["operation_id"], before[0]["sequence"])


def test_every_route_block_reason_has_work_board_text():
    """#1156 follow-up: each stable route-block code a card can carry has its
    cockpit explanation (provider_policy_revoked and the NEAR slot codes)."""
    from pathlib import Path
    panel = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "cockpit"
        / "WorkBoardPanel.tsx").read_text()
    block = panel[panel.index("const BLOCK_REASON_TEXT"):panel.index("const blockReasonText")]
    missing = sorted(code for code in retirement.ROUTE_BLOCK_REASONS if f"  {code}: \"{code} · " not in block)
    assert missing == []


# ── Communication preparation (Mail source inside a general task) ─


from tests.test_document_build_native_capacity import build_admission_lifecycle  # noqa: E402,F401


@pytest.mark.asyncio
async def test_communication_preparation_mail_source_reaches_near_and_settles(accounting_db, monkeypatch,
        build_admission_lifecycle):
    """The selected Mail source of a communications task is published and
    drafted on NEAR TEE (communication_preparation._publish_source)."""
    import base64
    from types import SimpleNamespace
    import httpx
    from fastapi import FastAPI
    from src.api import work_board as board_api
    from src.auth import service as auth_service
    from src.auth.middleware import OperatorAuthMiddleware
    from src.db.models import InferenceCostReservation
    from src.integrations.gmail_read import GMAIL_READONLY_SCOPE, _metadata_from_payload
    from src.native_tools.registry import ToolRegistry
    from src.vault import crypto
    from src.work_board.communication_contracts import CommunicationCreate
    from src.work_board.communication_preparation import read_plan
    from src.work_board.contracts import CommunicationSelection, WorkBoardOwner
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.work_board.general_task import GeneralTaskService
    from src.workflows.job_runtime import DurableJobRepository
    from tests import test_mail_reply_vertical as mail
    monkeypatch.setattr(crypto, "_fernet", None)
    sessions = accounting_db[2].accounting_sessions
    message = {"id": "provider-message-1", "threadId": "provider-thread-1", "historyId": "stable-history",
        "labelIds": ["INBOX", "UNREAD"], "snippet": "Private preview", "payload": {"mimeType": "text/plain",
            "headers": [{"name": "Subject", "value": "Architecture review"}],
            "body": {"data": base64.urlsafe_b64encode(b"Private source body stable").decode()}}}
    monkeypatch.setattr(mail, "MESSAGE_REVISION", _metadata_from_payload(message).message_revision)
    issued = []
    create_session = auth_service.create_session

    async def capture_session(*args, **kwargs):
        result = await create_session(*args, **kwargs)
        issued.append(result[0])
        return result
    monkeypatch.setattr(auth_service, "create_session", capture_session)
    await mail._seed(sessions, monkeypatch)
    monkeypatch.setattr(settings, "deployment_environment", "test")
    jobs = DurableJobRepository()
    near = await prepare_near_tee_vertical(monkeypatch, jobs=jobs)
    near.answer = {"role": "assistant", "content": json.dumps(
        {"subject": "Private reply", "body": "Review this exact reply", "caveats": []})}
    await build_admission_lifecycle.start()
    operator = mail._operator()
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    source_reads = []

    async def source_transport(url, **kwargs):
        await kwargs["authority_check"]()
        if kwargs["method"] == "POST":
            value = {"access_token": "mail-access", "scope": GMAIL_READONLY_SCOPE}
        else:
            source_reads.append(url)
            value = message
        return SimpleNamespace(status_code=200, headers={"content-type": "application/json"},
            content=json.dumps(value).encode())
    from src.integrations.gmail_read import vault_repository
    vault_read = vault_repository.get

    async def vault_get(key, *args, **kwargs):
        if key != "mail-reply-vertical-secret":
            return await vault_read(key, *args, **kwargs)
        return json.dumps({"client_id": "fixture-client", "refresh_token": "fixture-refresh"})
    monkeypatch.setattr("src.integrations.gmail_read.request_pinned_https", source_transport)
    monkeypatch.setattr("src.integrations.gmail_read.vault_repository.get", vault_get)
    registry = ToolRegistry()
    service = GeneralTaskService(registry)
    dispatcher = WorkBoardDispatcher(session_provider=sessions, general_tasks=service)
    registry.communication_dispatcher = dispatcher
    registry.start()
    service.start()
    try:
        monkeypatch.setattr(board_api, "dispatcher", dispatcher)
        app = FastAPI()
        app.add_middleware(OperatorAuthMiddleware)
        app.include_router(board_api.router, prefix="/api")
        creation = CommunicationCreate(goal_id=mail.GOAL, goal_revision=1, selection=CommunicationSelection(
            reply_inputs=[{key: value for key, value in mail._reply_body("communication-near").items()
                if key != "idempotency_key"}], meeting_inputs=[], reschedule_inputs=[],
            acknowledge_private_review=True), max_cost_microusd=25_000, wall_seconds=120,
            idempotency_key="communication-near-mail")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8004",
                cookies={mail.settings.operator_auth_cookie_name: issued[0]},
                headers={"Origin": "http://127.0.0.1:3001"}) as client:
            response = await client.post("/api/work-board/general-tasks/communications",
                json=creation.model_dump(mode="json"))
            assert response.status_code == 200, response.text
            task_id = response.json()["task"]["task_id"]
        result = await dispatcher.run_pass()
        assert result["completed"] == 1, result
        async with sessions() as db:
            plan = await read_plan(db, owner, task_id, service=service)
            paid = list((await db.execute(select(InferenceCostReservation).where(
                InferenceCostReservation.runtime_path == "strategist_agent"))).scalars())
    finally:
        service.stop()
        registry.stop()
    assert len(plan.reply_drafts) == 1 and plan.reply_drafts[0].body == "Review this exact reply"
    assert len(source_reads) == 2
    body = assert_near_contract(near_inference_requests(near))
    assert "Draft a safe email reply" in body["messages"][0]["content"]
    assert len(paid) == 1 and paid[0].state == "settled" and paid[0].profile_id == "near_tee.text"
    assert paid[0].actual_cost_microusd == NEAR_COST_MICROUSD


# ── Unknown liability (no NEAR billing readback) per vertical ────


@pytest.mark.asyncio
async def test_work_board_card_proposal_missing_near_billing_is_unknown(accounting_db, monkeypatch,
        isolated_broker):
    from src.work_board.contracts import WorkBoardProposalRequest
    from src.work_board import triage as triage_service
    sessions, jobs, operator, owner, task, near = await _card_proposal_world(accounting_db, monkeypatch)
    near.billing_missing = True
    result = await triage_service.create_proposal(owner, task.task_id, kind="specify",
        request=WorkBoardProposalRequest(expected_revision=task.task_revision, idempotency_key="near-unknown"),
        operator=operator)
    assert result["status"] == "blocked" and result["provider_contact_state"] == "unknown", result
    assert result["proposed_tasks"] == []
    assert_unknown_liability_blocks_lane(await jobs.inference_accounting_snapshot(), isolated_broker,
        "strategist_agent")


@pytest.mark.asyncio
async def test_guardian_assessment_missing_near_billing_is_unknown(isolated_runtime, monkeypatch, tmp_path,
        isolated_broker):
    from src.db.models import GuardianOpportunity
    from src.guardian import opportunity_runtime
    from src.workflows.job_runtime import DurableJobRepository
    sessions, row, offered = await _guardian_world(isolated_runtime, monkeypatch, tmp_path)
    jobs = DurableJobRepository()
    near = await prepare_near_tee_vertical(monkeypatch, jobs=jobs)
    near.answer = assessment_answer(offered)
    near.billing_missing = True
    await opportunity_runtime.execute_assessment(row.id)
    async with sessions() as db:
        final = await db.get(GuardianOpportunity, row.id)
    # Contacted uncertainty is retained liability, never a replay.
    assert final.status == "unknown", (final.status, final.reason_code)
    assert (await jobs.get_job(final.job_id))["status"] == "unknown_external_effect"
    assert_unknown_liability_blocks_lane(await jobs.inference_accounting_snapshot(), isolated_broker,
        "strategist_agent")


@pytest.mark.asyncio
async def test_research_missing_near_billing_is_unknown_and_adopts_nothing(accounting_db, monkeypatch,
        isolated_broker):
    from src.db.models import WorkBoardStatus, WorkflowRunState
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.work_board.repository import WorkBoardRepository
    from src.workflows.job_runtime import DurableJobRepository
    from tests.test_vertical_no_provider_journeys import _seed_research, _todo_capability_task
    install_research_source(monkeypatch)
    sessions = accounting_db[2].accounting_sessions
    operator, owner = await operator_owner(monkeypatch)
    jobs = DurableJobRepository()
    near = await prepare_near_tee_vertical(monkeypatch, jobs=jobs, capabilities=("text",),
        near=ResearchNearFixture())
    near.billing_missing = True
    goal_id, typed_input = await _seed_research(sessions, owner)
    task_id = await _todo_capability_task(sessions, owner, capability="work.research-dossier.v1",
        goal_id=goal_id, typed_input=typed_input, key="research-near-unknown")
    dispatcher = WorkBoardDispatcher(repository=WorkBoardRepository(), jobs=jobs, session_provider=sessions,
        runner_id="service:work-board")
    receipt = await dispatcher.run_pass()
    task = await _task(sessions, task_id)
    async with sessions() as db:
        runs = list((await db.execute(select(WorkflowRunState))).scalars())
    snapshot = await jobs.inference_accounting_snapshot()
    rows = near_settled_rows(snapshot, "readonly_research_child")
    contacted = [row for row in rows if row["contact_started_at"] is not None]
    # Exactly one contact: the serial lane stays blocked by its Unknown cost,
    # so the sibling is never contacted and no dossier is adopted.
    assert len(contacted) == 1 and contacted[0]["state"] == "unknown" and snapshot["unknown_microusd"] > 0
    assert len([r for r in near_inference_requests(near) if r.url.path == "/v1/chat/completions"]) == 1
    receipt_for = isolated_broker.receipt_for(contacted[0]["operation_id"])
    assert receipt_for.status == "blocked"
    assert receipt.get("completed", 0) == 0 and task.status is not WorkBoardStatus.done
    assert not any(run.job_kind == "readonly_research_child" and run.status == "succeeded" for run in runs)


@pytest.mark.asyncio
async def test_research_near_body_mismatch_fails_closed_before_contact(accounting_db, monkeypatch):
    """A NEAR request body that differs from the stored prompt is refused
    before admission: no NEAR chat request and no settled research cost."""
    from src.db.models import WorkBoardStatus
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.work_board.repository import WorkBoardRepository
    from src.workflows import research_provider
    from src.workflows.job_runtime import DurableJobRepository
    from tests.test_vertical_no_provider_journeys import _seed_research, _todo_capability_task
    install_research_source(monkeypatch)
    sessions = accounting_db[2].accounting_sessions
    operator, owner = await operator_owner(monkeypatch)
    jobs = DurableJobRepository()
    near = await prepare_near_tee_vertical(monkeypatch, jobs=jobs, capabilities=("text",),
        near=ResearchNearFixture())
    original = research_provider._near_call

    async def drifted(context, body, **kwargs):
        from dataclasses import replace as dc_replace
        call = await original(context, body, **kwargs)
        return dc_replace(call, body={**call.body, "temperature": float(call.body["temperature"]) + 0.5})
    monkeypatch.setattr(research_provider, "_near_call", drifted)
    goal_id, typed_input = await _seed_research(sessions, owner)
    task_id = await _todo_capability_task(sessions, owner, capability="work.research-dossier.v1",
        goal_id=goal_id, typed_input=typed_input, key="research-near-drift")
    dispatcher = WorkBoardDispatcher(repository=WorkBoardRepository(), jobs=jobs, session_provider=sessions,
        runner_id="service:work-board")
    receipt = await dispatcher.run_pass()
    task = await _task(sessions, task_id)
    assert receipt.get("completed", 0) == 0 and task.status is not WorkBoardStatus.done
    assert [r for r in near_inference_requests(near) if r.url.path == "/v1/chat/completions"] == []
    rows = near_settled_rows(await jobs.inference_accounting_snapshot(), "readonly_research_child")
    assert all(row["contact_started_at"] is None for row in rows), rows


# ── Repository repair stays blocked on NEAR TEE (no NEAR adapter) ─


@pytest.mark.asyncio
async def test_repository_repair_is_blocked_on_near_before_planning_or_contact(accounting_db, monkeypatch):
    """The repair iteration model step has no NEAR TEE adapter: a repository
    intent is refused with near_tee_route_unsupported before any planning
    group, job, reservation or NEAR contact, so no planning money is spent."""
    from src.db.models import InferenceCostReservation, WorkBoardTask, WorkflowRunState
    from src.native_tools.task_adapters import ToolRegistry, _repository_work_block_reason, repository_work_descriptor
    from src.work_board.contracts import GeneralTaskCreate
    from src.work_board.general_task import GeneralTaskService
    from src.work_board.general_task_planner import GeneralTaskPlanner
    from src.work_board.repository import BoardError
    from tests.test_work_board_m6_provider_free_journey import _goal
    from tests.test_vertical_no_provider_journeys import _general_task_client
    sessions = accounting_db[2].accounting_sessions
    operator, owner = await operator_owner(monkeypatch)
    near = await prepare_near_tee_vertical(monkeypatch)
    reason = retirement.NEAR_TEE_ROUTE_UNSUPPORTED_REASON
    # General planning is served by NEAR; repository repair is not.
    assert retirement.inactive_route_reason("general_task_planner") is None
    goal = _goal("goal-repository-near", "Fix the failing parser test")
    goal.owner_principal_id, goal.owner_session_id = owner.principal_id, owner.session_id
    async with sessions() as db:
        db.add(goal)
    # 1. The stock selector never offers repository_work, with the stable reason.
    #    (Tool policy and a local executor allow it; only the route blocks.)
    monkeypatch.setattr(settings, "repo_sandbox", settings.repo_sandbox.model_copy(update={"enabled": True}))
    with monkeypatch.context() as scoped:
        scoped.setattr("src.tools.policy.is_tool_allowed", lambda tool, mode: True)
        assert _repository_work_block_reason("full") == reason

    # 2. A frozen tool set that still names repository_work: planning refuses
    #    it at the HTTP boundary with the stable reason, before any contact.
    class FrozenRepositoryRegistry(ToolRegistry):
        def descriptors(self):
            return [repository_work_descriptor()]
    monkeypatch.setattr("src.native_tools.registry.ToolRegistry", FrozenRepositoryRegistry)
    async with _general_task_client(accounting_db, monkeypatch, owner) as (client, registry, dispatcher):
        descriptor = registry.descriptors()[0]
        body = {"goal_revision": 1, "idempotency_key": "repository-near",
            "input": {"goal_ref": goal.id, "intent": "Fix the bug in parser.py so tests pass",
                "requested_output": descriptor.output_schema, "inference_egress_acknowledged": True,
                "limits": {"max_cost_microusd": 50_000}}}
        for _ in range(2):  # a retry gets the same block
            created = await client.post("/api/work-board/general-tasks", json=body)
            assert created.status_code == 409, created.text
            detail = created.json()["detail"]
            assert detail["code"] == reason and detail["message"] == retirement.inactive_route_label(reason)
    # 3. An explicit repository plan is refused before inspection or staging.
    service = GeneralTaskService(ToolRegistry(), planner=GeneralTaskPlanner())
    request = GeneralTaskCreate.model_validate({"goal_revision": 1, "idempotency_key": "repository-near-plan",
        "expected_plan_revision": 1,
        "input": {"goal_ref": goal.id, "intent": "Fix the parser", "requested_output": descriptor.output_schema,
            "inference_egress_acknowledged": True, "limits": {"max_cost_microusd": 50_000}},
        "plan": {"schema_version": 1, "revision": 1, "steps": [{"step_id": "repair", "tool_id": "repository_work",
            "input": {}, "depends_on": [], "output_contract": {"type": "object"}}]}})
    with pytest.raises(BoardError) as blocked:
        async with sessions() as db:
            await service.create(db, owner, request)
    assert blocked.value.code == reason and blocked.value.extra["provider_contact"] == "not_started"
    # Nothing was contacted, reserved, admitted or published.
    assert near_inference_requests(near) == []
    async with sessions() as db:
        assert list((await db.execute(select(InferenceCostReservation))).scalars()) == []
        assert list((await db.execute(select(WorkflowRunState))).scalars()) == []
        assert list((await db.execute(select(WorkBoardTask))).scalars()) == []
