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
    # ADR-037 (#1284): an ``unknown`` row is finished, not priced yet; it
    # counts its recorded charge, if any, and never its old bound.
    ("unknown", None, True, 0), ("unknown", 9, True, 9),
    ("settled", 17, True, 17), ("settled", 101, True, 101), ("released", None, None, 0),
])
def test_recorded_spend_is_display_only_and_ignores_period_rollover(state, actual, contact, expected):
    row = SimpleNamespace(state=state, bound_microusd=100, actual_cost_microusd=actual,
        contact_started_at=contact, period_id="2000-01")
    assert reservation_liability(row) == expected


@pytest.mark.parametrize("changed", [{"state": "unrecognized"},
    {"state": "released", "contact_started_at": True}])
def test_unproven_release_never_erases_a_request(changed):
    row = SimpleNamespace(**{**dict(state="reserved", bound_microusd=100,
        actual_cost_microusd=None, contact_started_at=None), **changed})
    with pytest.raises(InferenceAccountingError):
        reservation_liability(row)


def _no_dollar_group(max_calls):
    now = datetime.now(timezone.utc)
    return TaskProposalGroupV1(group_id="a" * 64, owner_principal_id="operator:fixture",
        owner_session_id="root-fixture", goal_id="goal-fixture", goal_revision=1,
        creation_request_key="fixture", initial_input_digest="b" * 64,
        planning_snapshot_digest="c" * 64, intent_egress_ack_digest="d" * 64,
        limits_digest="e" * 64, max_inference_calls=max_calls,
        max_steps=1, issued_at=now, original_deadline_at=now + timedelta(seconds=60))


def _iteration_row(original, index, state):
    import json
    from src.work_board.general_task import digest
    iteration = f"{index:064x}"
    operation = "remote:repo-work:" + iteration
    binding = dict(group=original.model_dump(mode="json"), repository_job_id="repo-job",
        repository_attempt_id="repo-attempt", repository_fence=1, parent_task_id="parent-task",
        parent_attempt_id="parent-attempt", native_invocation_id="native-child", iteration_index=index,
        iteration_id=iteration, operation_id=operation,
        original_deadline_at=original.original_deadline_at.isoformat(), source_checkpoint_digest="e" * 64)
    entry = GeneralTaskGroupReservationEvidenceV1.model_validate(dict(group=original,
        group_digest=digest(original.model_dump(mode="json")), role="repository_iteration",
        call_ordinal=index, original_operation_id=operation, original_job_id="repo-job",
        initial_proposal_operation_id=None, task_id="parent-task", task_attempt_id="parent-attempt",
        plan_revision=0, selected_grant_digest=None, parent_owner=None, parent_fence=None,
        repository_binding=binding)).model_dump(mode="json")
    return SimpleNamespace(operation_id=operation, job_id="repo-job", owner_id=original.owner_principal_id,
        goal_id=original.goal_id, goal_revision=original.goal_revision, state=state, bound_microusd=0,
        actual_cost_microusd=None, contact_started_at=True, evidence_json=json.dumps([entry]))


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _query):
        return SimpleNamespace(scalars=lambda: list(self.rows))


@pytest.mark.asyncio
async def test_repository_repair_without_dollar_fields_runs_to_its_call_ceiling():
    """Design test 13 (#1284), repository part, at the accounting level.

    The real repository repair journeys need native Linux supervision and are
    environment-blocked on macOS, so this pins the accounting gate itself: a
    repair whose limits and group carry no dollar field keeps calling while
    calls remain, an ``unknown`` (unpriced) iteration does not block, and the
    call ceiling is the only stop.
    """
    from src.workflows.repo_repair import RepoWorkLimits
    from src.workflows.repo_repair_source import _repository_remaining
    from src.work_board.contracts import RepositoryNativeLimitEvidenceV1
    limits = RepoWorkLimits.model_validate({"max_iterations": 3, "max_total_seconds": 900})
    assert limits.max_cost_usd == 0 and limits.max_cost_microusd == 0
    original = _no_dollar_group(2)
    assert original.max_cost_microusd == 0
    evidence = RepositoryIterationEvidenceV1.model_validate(dict(group=original, repository_job_id="repo-job",
        repository_attempt_id="repo-attempt", repository_fence=1, parent_task_id="parent-task",
        parent_attempt_id="parent-attempt", native_invocation_id="native-child", iteration_index=1,
        iteration_id="f" * 64, operation_id="remote:repo-work:" + "f" * 64,
        original_deadline_at=original.original_deadline_at, source_checkpoint_digest="e" * 64))
    assert evidence.original_max_cost_microusd == 0
    context = {"group": original, "run": SimpleNamespace(run_identity="repo-job"), "work": SimpleNamespace(limits=limits)}
    assert await _repository_remaining(_Rows([]), context) == {
        "remaining_inference_calls": 2, "remaining_cost_microusd": 0}
    first = _iteration_row(original, 1, "unknown")
    assert (await _repository_remaining(_Rows([first]), context))["remaining_inference_calls"] == 1
    rows = [first, _iteration_row(original, 2, "settled")]
    with pytest.raises(InferenceAccountingError, match="repository_original_call_limit"):
        await _repository_remaining(_Rows(rows), context)
    assert (await _repository_remaining(_Rows(rows), context, allow_exhausted_readback=True))[
        "remaining_inference_calls"] == 0
    # A new stop records a 0 legacy server bound; it parses and is never compared.
    recorded = RepositoryNativeLimitEvidenceV1(original_limits_digest="a" * 64,
        original_deadline_at=original.original_deadline_at, original_server_bound_microusd=0,
        root_liability_microusd=0, group_liability_microusd=0, group_calls=2,
        original_root_max_cost_microusd=0, original_group_max_cost_microusd=0,
        original_group_max_calls=2, cause="shared_group_exhausted")
    assert recorded.original_server_bound_microusd == 0


@pytest.mark.asyncio
async def test_iteration_charge_above_the_old_allowance_never_stops_the_repair():
    """Overspend during a repair (#1284): a group stored with a legacy dollar
    allowance whose first iteration was charged far above it still has its
    remaining call, and the stop's member read does not refuse on spend."""
    from src.workflows.repo_repair import RepoWorkLimits
    from src.workflows.repo_repair_source import _repository_remaining
    legacy = _no_dollar_group(2).model_copy(update={"max_cost_microusd": 500})
    limits = RepoWorkLimits.model_validate({"max_iterations": 3, "max_total_seconds": 900, "max_cost_usd": 0.0005})
    context = {"group": legacy, "run": SimpleNamespace(run_identity="repo-job"), "work": SimpleNamespace(limits=limits)}
    overspent = _iteration_row(legacy, 1, "settled")
    overspent.actual_cost_microusd = 10_000
    assert overspent.actual_cost_microusd > legacy.max_cost_microusd
    assert await _repository_remaining(_Rows([overspent]), context) == {
        "remaining_inference_calls": 1, "remaining_cost_microusd": 0}
    # Only the call ceiling ends it.
    second = _iteration_row(legacy, 2, "settled")
    second.actual_cost_microusd = 10_000
    assert (await _repository_remaining(_Rows([overspent, second]), context,
        allow_exhausted_readback=True))["remaining_inference_calls"] == 0
