"""Closed specialist accounting proof; original budget/debt is never reset."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from src.work_board.contracts import WorkBoardOwner
from src.work_board.general_task import digest
from src.work_board.general_task_proposal import new_group
from src.workflows.general_task_accounting import entry_for
from src.workflows.inference_accounting import InferenceAccountingError
from tests.test_general_task_planner import descriptor, task_input
from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime


def group():
    return new_group(WorkBoardOwner(principal_id="operator:fixture", session_id="original-root"),
        task_input(), [descriptor()], goal_revision=1, request_key="original-intent",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10))


def entry(original):
    return {"kind": "general_task_group_reservation.v1", "group": original.model_dump(mode="json"),
        "group_digest": digest(original.model_dump(mode="json")), "role": "specialist",
        "call_ordinal": 2, "original_operation_id": "planning-specialist:original",
        "original_job_id": "inference:original", "initial_proposal_operation_id": None,
        "task_id": "task:original", "task_attempt_id": "attempt:original", "plan_revision": 1,
        "selected_grant_digest": "a" * 64, "parent_owner": "delegate:original", "parent_fence": 1,
        "delegation_invocation_id": "delegate-invocation:original", "delegation_request_digest": "b" * 64}


def row(original, evidence):
    return SimpleNamespace(evidence_json=json.dumps([evidence]), operation_id=evidence["original_operation_id"],
        job_id=evidence["original_job_id"], owner_id=original.owner_principal_id,
        goal_id=original.goal_id, goal_revision=original.goal_revision)


@pytest.mark.parametrize("corrupt", ["missing_callback", "missing_request", "extra_field", "wrong_role", "foreign_job"])
def test_malformed_specialist_evidence_cannot_authorize_another_role_or_row(corrupt):
    original = group()
    evidence = entry(original)
    canonical = row(original, evidence)
    if corrupt == "missing_callback":
        evidence.pop("delegation_invocation_id")
    elif corrupt == "missing_request":
        evidence.pop("delegation_request_digest")
    elif corrupt == "extra_field":
        evidence["callback_authorizes_contact"] = True
    elif corrupt == "wrong_role":
        evidence["role"] = "continuation"
    else:
        evidence["original_job_id"] = "foreign-inference-job"
    canonical.evidence_json = json.dumps([evidence])
    with pytest.raises(InferenceAccountingError, match="group_evidence_invalid"):
        entry_for(canonical)


@pytest.mark.parametrize("role", ["initial_proposal", "continuation", "research", "files", "arbitrary_native_callback"])
def test_delegation_proof_cannot_change_existing_planning_roles(role):
    from src.model_fabric.accounting import bind_general_task_accounting
    with pytest.raises(InferenceAccountingError, match="group_binding_invalid"):
        with bind_general_task_accounting(group(), role=role, delegation_invocation_id="delegate:original",
                delegation_request_digest="b" * 64):
            pytest.fail("foreign role gained delegation planning context")


def test_specialist_context_retains_original_group_and_resets_on_exception():
    from src.model_fabric.accounting import bind_general_task_accounting, _task_group
    original = group()
    binding = {key: value for key, value in entry(original).items() if key in {
        "task_id", "task_attempt_id", "plan_revision", "selected_grant_digest", "parent_owner", "parent_fence",
        "delegation_invocation_id", "delegation_request_digest"}}
    with bind_general_task_accounting(original):
        outer = _task_group.get()
        with pytest.raises(RuntimeError, match="owner closure"):
            with bind_general_task_accounting(original, role="specialist", **binding):
                current = _task_group.get()
                assert current["group"] is original
                assert current["group"].original_deadline_at == original.original_deadline_at
                assert current["group"].max_inference_calls == original.max_inference_calls
                assert current["delegation_invocation_id"] == binding["delegation_invocation_id"]
                raise RuntimeError("owner closure")
        assert _task_group.get() is outer
    assert _task_group.get() is None


@pytest.mark.parametrize("state,cost,reason", [
    ("reserved", 30, "specialist_cost_limit"), ("contact_started", 30, "specialist_cost_limit"),
    ("settled", 30, "specialist_cost_limit"), ("settled", None, "group_unknown"),
    ("unknown", None, "group_unknown"),
])
def test_specialist_request_caps_preserve_original_liability(state, cost, reason):
    from src.workflows.general_task_accounting import _validate_specialist_limits
    original = group()
    evidence = entry(original)
    canonical = row(original, evidence)
    canonical.state, canonical.bound_microusd, canonical.actual_cost_microusd = state, 30, cost
    limits = SimpleNamespace(max_inference_calls=2, max_cost_microusd=40)
    before = canonical.evidence_json
    with pytest.raises(InferenceAccountingError, match=reason):
        _validate_specialist_limits([canonical], evidence, limits, new_bound=20)
    assert canonical.evidence_json == before and canonical.state == state


def test_specialist_sibling_limits_and_released_calls_do_not_mint_new_allowance():
    from src.workflows.general_task_accounting import _validate_specialist_limits
    original = group()
    own_evidence = entry(original)
    own = row(original, own_evidence)
    own.state, own.bound_microusd, own.actual_cost_microusd = "released", 30, None
    foreign_evidence = {**own_evidence, "delegation_invocation_id": "delegate:other",
        "delegation_request_digest": "c" * 64, "original_operation_id": "planning:other", "original_job_id": "inference:other"}
    sibling = row(original, foreign_evidence)
    sibling.state, sibling.bound_microusd, sibling.actual_cost_microusd = "reserved", 100, None
    limits = SimpleNamespace(max_inference_calls=2, max_cost_microusd=20)
    _validate_specialist_limits([own, sibling], own_evidence, limits, new_bound=20)
    with pytest.raises(InferenceAccountingError, match="specialist_call_limit"):
        _validate_specialist_limits([own], own_evidence,
            SimpleNamespace(max_inference_calls=1, max_cost_microusd=20), new_bound=0)
    assert original.max_cost_microusd == group().max_cost_microusd


@pytest.mark.asyncio
async def test_nested_root_cannot_borrow_original_continuation_allowance(task_runtime, monkeypatch):
    from sqlalchemy import update, select
    from src.db.models import WorkflowRunState, InferenceCostReservation
    from src.workflows.general_task_accounting import validate_continuation
    from tests.test_general_task_continuation_accounting import continuation_fixture
    sessions, dispatcher, _, transport, _, parent, task, attempt, manifest, envelope = await continuation_fixture(task_runtime, monkeypatch)
    async with sessions() as db:
        await db.execute(update(WorkflowRunState).where(WorkflowRunState.run_identity == parent.run_identity)
            .values(parent_job_id="delegate-invocation:foreign", branch_depth=2))
        await db.commit()
    binding = {"task_id": task.task_id, "task_attempt_id": attempt.attempt_id,
        "plan_revision": manifest.plan_revision, "selected_grant_digest": manifest.selected_grant_digest,
        "parent_owner": parent.lease_owner, "parent_fence": parent.fencing_token}
    async with sessions() as db:
        with pytest.raises(InferenceAccountingError, match="continuation_not_bound"):
            await validate_continuation(db, envelope.proposal_group, binding, None)
        assert list((await db.execute(select(InferenceCostReservation))).scalars()) == []
        current = await dispatcher.jobs._fetch(db, parent.run_identity)
        assert current.fencing_token == parent.fencing_token
    assert transport["contacts"] == []
