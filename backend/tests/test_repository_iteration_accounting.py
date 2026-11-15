"""Structural accounting negatives; never counterfeit a positive source seal."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.model_fabric.accounting import (bind_general_task_accounting,
    bind_repository_iteration_accounting, _task_group)
from src.model_fabric.remote_inference_admission import (bind_remote_inference_receipt,
    stable_remote_inference_operation_id)
from src.work_board.contracts import TaskProposalGroupV1
from src.workflows.general_task_accounting import (GeneralTaskGroupReservationEvidenceV1,
    RepositoryIterationEvidenceV1, reservation_liability)
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
def test_repository_binder_rejects_public_authority(candidate):
    with pytest.raises(InferenceAccountingError, match="witness"):
        with bind_repository_iteration_accounting(candidate):
            pytest.fail("public authority entered original accounting context")
    assert _task_group.get() is None


def test_public_planner_binder_retains_its_two_roles_and_outer_context():
    original = group()
    with bind_general_task_accounting(original):
        context = _task_group.get()
        with pytest.raises(InferenceAccountingError, match="binding_invalid"):
            with bind_general_task_accounting(original, role="repository_iteration"):
                pytest.fail("ordinary caller selected repository authority")
        with pytest.raises(InferenceAccountingError, match="witness"):
            with bind_repository_iteration_accounting({"group": original}):
                pytest.fail("dict minted source authority")
        assert _task_group.get() is context
    assert _task_group.get() is None


def test_receipt_binder_rejects_public_operation_override_and_preserves_legacy_id():
    with pytest.raises(InferenceAccountingError, match="witness"):
        with bind_remote_inference_receipt(repository=object(), job_id="original-repo",
            owner="source-owner", fencing_token=1, repository_iteration_witness={"operation_id": "caller-op"}):
            pytest.fail("public operation override accepted")
    with bind_remote_inference_receipt(repository=object(), job_id="legacy-job", owner="owner", fencing_token=1):
        assert stable_remote_inference_operation_id(None, profile_id="openrouter.text", fallback="fallback") == "remote:legacy-job"


def test_closed_repository_evidence_binds_exact_operation_and_original_cutoff():
    original = group()
    payload = dict(group=original, repository_job_id="repo-job", repository_attempt_id="repo-attempt",
        repository_fence=1, parent_task_id="parent-task", parent_attempt_id="parent-attempt",
        native_invocation_id="native-child", iteration_index=1, iteration_id="f" * 64,
        operation_id="remote:repo-work:" + "f" * 64, original_deadline_at=original.original_deadline_at,
        original_max_cost_microusd=1000, source_checkpoint_digest="e" * 64)
    # Schema roundtrip is structural; this DTO has no producer seal.
    assert RepositoryIterationEvidenceV1.model_validate(payload).iteration_index == 1
    serialized = RepositoryIterationEvidenceV1.model_validate(payload).model_dump(mode="json")
    assert RepositoryIterationEvidenceV1.model_validate(serialized).original_deadline_at == original.original_deadline_at
    for changed in ({"operation_id": "caller-op"}, {"iteration_index": 4}, {"caller_budget": 10000},
        {"original_deadline_at": original.original_deadline_at + timedelta(seconds=1)}):
        with pytest.raises(ValidationError):
            RepositoryIterationEvidenceV1.model_validate({**payload, **changed})
    from src.work_board.general_task import digest
    evidence = dict(group=original, group_digest=digest(original.model_dump(mode="json")),
        role="repository_iteration", call_ordinal=1, original_operation_id=payload["operation_id"],
        original_job_id="repo-job", initial_proposal_operation_id=None, task_id="parent-task",
        task_attempt_id="parent-attempt", plan_revision=0, selected_grant_digest=None,
        parent_owner=None, parent_fence=None, repository_binding=payload)
    for changed in ({"repository_binding": None}, {"role": "continuation"},
        {"original_operation_id": "different-op"}, {"plan_revision": 1},
        {"original_job_id": "different-root"}):
        with pytest.raises(ValidationError):
            GeneralTaskGroupReservationEvidenceV1.model_validate({**evidence, **changed})


@pytest.mark.parametrize("state,actual,contact,expected", [
    ("reserved", None, None, 100), ("contact_started", None, True, 100),
    ("unknown", None, True, 100), ("settled", 17, True, 17), ("released", None, None, 0),
])
def test_original_liability_math_ignores_period_rollover(state, actual, contact, expected):
    row = SimpleNamespace(state=state, bound_microusd=100, actual_cost_microusd=actual,
        contact_started_at=contact, period_id="2000-01")
    assert reservation_liability(row) == expected


@pytest.mark.parametrize("changed", [{"state": "unrecognized"}, {"bound_microusd": 0},
    {"bound_microusd": True}, {"state": "settled", "actual_cost_microusd": None},
    {"state": "settled", "actual_cost_microusd": -1},
    {"state": "settled", "actual_cost_microusd": 101},
    {"state": "released", "contact_started_at": True}])
def test_unproven_release_or_malformed_charge_never_erases_liability(changed):
    row = SimpleNamespace(**{**dict(state="reserved", bound_microusd=100,
        actual_cost_microusd=None, contact_started_at=None), **changed})
    with pytest.raises(InferenceAccountingError):
        reservation_liability(row)
