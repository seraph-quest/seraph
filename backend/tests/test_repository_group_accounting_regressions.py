"""Actual original group rows; repository-source positive proof is separate."""
import pytest
from sqlalchemy import select, update

from src.db.models import InferenceCostReservation, WorkflowRunState
from src.model_fabric.accounting import DurableInferenceBrokerMixin
from src.workflows.general_task_accounting import entry_for, validate_recovered_entry
from src.workflows.inference_accounting import InferenceAccountingError
from src.work_board.repository import BoardError
from tests.test_general_task_continuation_accounting import continuation_fixture
from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime


@pytest.mark.asyncio
async def test_released_precontact_calls_do_not_renew_original_group_allowance(task_runtime, monkeypatch):
    sessions, dispatcher, planner, transport, owner, parent, task, attempt, manifest, envelope = await continuation_fixture(task_runtime, monkeypatch, max_calls=1)
    async def deny_before_contact(_broker, _handle):
        raise InferenceAccountingError("literal_precontact_denial")
    monkeypatch.setattr(DurableInferenceBrokerMixin, "_contact_accounting", deny_before_contact)
    async with sessions() as db:
        with pytest.raises(BoardError):
            await planner.continue_plan(db, owner, parent=parent, task=task, attempt=attempt,
                manifest=manifest, envelope=envelope, request_key="precontact-one")
    async with sessions() as db:
        rows = list((await db.execute(select(InferenceCostReservation))).scalars())
        assert len(rows) == envelope.proposal_group.max_inference_calls == 1
        assert all(row.state == "released" and row.contact_started_at is None for row in rows)
        assert [entry_for(row)["call_ordinal"] for row in rows] == [1]
        assert all(entry_for(row)["group"] == envelope.proposal_group.model_dump(mode="json") for row in rows)
        with pytest.raises(BoardError, match="call_limit"):
            await planner.continue_plan(db, owner, parent=parent, task=task, attempt=attempt,
                manifest=manifest, envelope=envelope, request_key="release-cannot-refund-call")
        assert len(list((await db.execute(select(InferenceCostReservation))).scalars())) == 1
    assert transport["contacts"] == []


@pytest.mark.asyncio
async def test_original_row_recovery_rechecks_role_binding_and_current_parent(task_runtime, monkeypatch):
    sessions, dispatcher, planner, transport, owner, parent, task, attempt, manifest, envelope = await continuation_fixture(task_runtime, monkeypatch)
    async with sessions() as db:
        await planner.continue_plan(db, owner, parent=parent, task=task, attempt=attempt,
            manifest=manifest, envelope=envelope, request_key="recovery-authority-probe")
    async with sessions() as db:
        rows = list((await db.execute(select(InferenceCostReservation))).scalars())
        assert len(rows) == 1
        run = await dispatcher.jobs._fetch(db, rows[0].job_id)
        original = entry_for(rows[0])
        binding = {key: original[key] for key in ("role", "task_id", "task_attempt_id", "plan_revision",
            "selected_grant_digest", "parent_owner", "parent_fence")}
        binding["group"] = envelope.proposal_group
        for changed in ({"role": "repository_iteration"}, {"parent_fence": parent.fencing_token + 1},
            {"task_id": "another-task"}):
            with pytest.raises(InferenceAccountingError, match="binding_invalid"):
                await validate_recovered_entry(db, run, rows[0], rows, {**binding, **changed},
                    runtime_path="general_task_planner")
        await db.execute(update(WorkflowRunState).where(WorkflowRunState.run_identity == parent.run_identity)
            .values(lease_owner="foreign-parent-owner"))
        with pytest.raises(InferenceAccountingError, match="continuation_not_bound"):
            await validate_recovered_entry(db, run, rows[0], rows, binding, runtime_path="general_task_planner")
        assert entry_for(rows[0])["role"] == "continuation"
        assert rows[0].operation_id == original["original_operation_id"]
    assert len(transport["contacts"]) == 1
