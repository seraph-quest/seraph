"""Structural negatives only; genuine private producer proof lives in the journey."""
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.model_fabric.accounting import bind_general_task_accounting, _task_group
from src.work_board.contracts import TaskProposalGroupV1
from src.work_board.general_task import digest
from src.workflows.general_task_accounting import GeneralTaskGroupReservationEvidenceV1
from src.workflows.inference_accounting import InferenceAccountingError


def group():
    now = datetime.now(timezone.utc)
    return TaskProposalGroupV1(group_id="a" * 64, owner_principal_id="operator:fixture",
        owner_session_id="root-fixture", goal_id="goal-fixture", goal_revision=1,
        creation_request_key="fixture", initial_input_digest="b" * 64,
        planning_snapshot_digest="c" * 64, intent_egress_ack_digest="d" * 64,
        limits_digest="e" * 64, max_inference_calls=2, max_cost_microusd=1000,
        max_steps=1, issued_at=now, original_deadline_at=now + timedelta(seconds=60))


@pytest.mark.parametrize("candidate", [None, {}, {"_seal": True}, object()])
def test_preparation_binder_rejects_public_or_unsealed_authority(candidate):
    with pytest.raises(InferenceAccountingError, match="binding_invalid"):
        with bind_general_task_accounting(group(), role="communication_preparation", preparation_binding=candidate):
            pytest.fail("unsealed authority entered accounting context")
    assert _task_group.get() is None


@pytest.mark.parametrize("role", ["initial_proposal", "continuation", "caller_selected_source"])
def test_preparation_cannot_be_smuggled_through_another_role(role):
    with pytest.raises(InferenceAccountingError, match="binding_invalid"):
        with bind_general_task_accounting(group(), role=role, preparation_binding={}):
            pytest.fail("source authority entered an ordinary planner role")


def test_existing_context_is_restored_after_rejected_source_binding():
    original = group()
    with bind_general_task_accounting(original):
        context = _task_group.get()
        with pytest.raises(InferenceAccountingError):
            with bind_general_task_accounting(original, role="communication_preparation", preparation_binding={}):
                pytest.fail("malformed source accepted")
        assert _task_group.get() is context
    assert _task_group.get() is None


def test_closed_evidence_cannot_relabel_initial_reservation_as_source_preparation():
    original = group()
    evidence = dict(group=original, group_digest=digest(original.model_dump(mode="json")),
        role="initial_proposal", call_ordinal=1, original_operation_id="operation-fixture",
        original_job_id="job-fixture", initial_proposal_operation_id="operation-fixture",
        task_id=None, task_attempt_id=None, plan_revision=0, selected_grant_digest=None,
        parent_owner=None, parent_fence=None)
    assert GeneralTaskGroupReservationEvidenceV1.model_validate(evidence).role == "initial_proposal"
    for changed in ({"role": "communication_preparation"}, {"preparation": {}},
        {"role": "source_preparation"}, {"caller_authorizes_contact": True}):
        with pytest.raises(ValidationError):
            GeneralTaskGroupReservationEvidenceV1.model_validate({**evidence, **changed})
