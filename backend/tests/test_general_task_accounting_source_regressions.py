"""Canonical continuation rows establish shared-group failure/recovery mechanics."""
import pytest
from sqlalchemy import select, update

from src.db.models import InferenceCostReservation, WorkflowRunState
from src.workflows.general_task_accounting import entry_for, validate_recovered_entry
from src.workflows.inference_accounting import InferenceAccountingError
from tests.test_general_task_continuation_accounting import continuation_fixture
from tests.test_general_task_persistence import task_runtime
from tests.general_task_method_lifecycle import native_admission_lifecycle
from tests.test_work_board_m6_provider_free_journey import isolated_runtime


@pytest.mark.asyncio
async def test_recovered_group_row_requires_original_role_and_current_parent(task_runtime, monkeypatch, native_admission_lifecycle):
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
        for changed in ({"role": "communication_preparation"}, {"parent_fence": parent.fencing_token + 1},
            {"task_id": "another-task"}):
            with pytest.raises(InferenceAccountingError, match="binding_invalid"):
                await validate_recovered_entry(db, run, rows[0], rows, {**binding, **changed},
                    runtime_path="general_task_planner")
        await db.execute(update(WorkflowRunState).where(WorkflowRunState.run_identity == parent.run_identity)
            .values(lease_owner="foreign-parent-owner"))
        with pytest.raises(InferenceAccountingError, match="continuation_not_bound"):
            await validate_recovered_entry(db, run, rows[0], rows, binding, runtime_path="general_task_planner")
        # This is validator proof on an actual original row, not a new inference
        # operation or a fabricated communication preparation success.
        assert entry_for(rows[0])["role"] == "continuation"
    assert len(transport["contacts"]) == 1
