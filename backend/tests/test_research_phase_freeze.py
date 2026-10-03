"""Real SQLite barriers for stale native phase freeze; no success fixture proof."""
import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text

from tests.test_inference_accounting import accounting_db
from tests.test_research_native_kernel import create_kernel, configured_real_auth
from src.db.models import WorkBoardAttempt, WorkBoardStatus, WorkBoardTask, WorkflowRunState
from src.workflows.research_coordinator import continue_parent, freeze_quiescent, _PhaseCompletion, _binding_tuple
from src.workflows.research_waits import pause_parent
from src.work_board.research_contracts import WAIT_SOURCES


async def closed_phase(accounting_db):
    jobs, task, attempt, spec, parent, creation = await create_kernel(accounting_db)
    binding = await pause_parent(jobs, parent_id=spec.identity.job_id, owner="research-kernel",
        job_fence=1, board_fence=1, board_revision=1, reason=WAIT_SOURCES)
    binding["creation_digest"] = creation["creation_digest"]
    # This focused kernel has no production input artifact row. The actual
    # native path refuses it before source/provider use and returns a sealed
    # completion result; no callback/admission is replaced by a fixture.
    with pytest.raises(Exception):
        await continue_parent(jobs, parent_id=spec.identity.job_id, owner="research-kernel", phase_binding=binding)
    assert isinstance(binding.get("_completion"), _PhaseCompletion)
    revision = (await jobs.get_job(spec.identity.job_id))["revision"]
    return jobs, task, attempt, spec, binding, revision


@pytest.mark.asyncio
async def test_actual_closed_phase_freeze_commits_and_reopens(accounting_db):
    jobs, task, attempt, spec, binding, revision = await closed_phase(accounting_db)
    assert await freeze_quiescent(jobs, parent_id=spec.identity.job_id, owner="research-kernel",
        phase_binding=binding, expected_parent_revision=revision, reason="research_input_recovery") is True
    await accounting_db[1].dispose()
    async with accounting_db[2].accounting_sessions() as db:
        rows = list((await db.scalars(select(WorkflowRunState))).all())
        assert len(rows) == 3 and all(row.status == "blocked" and row.lease_owner is None for row in rows)
        board = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task.task_id))
        assert board.status == WorkBoardStatus.blocked and board.block_reason == "research_input_recovery"
        original = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == attempt.attempt_id))
        assert original.ended_at is None and original.workflow_run_id == spec.identity.job_id


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["terminal", "revision"])
async def test_committed_concurrent_terminal_or_revision_wins_over_old_freeze(accounting_db, change):
    jobs, task, attempt, spec, binding, revision = await closed_phase(accounting_db)
    entered, release = asyncio.Event(), asyncio.Event()

    async def operator_writer():
        async with accounting_db[2].accounting_sessions() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            board = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task.task_id))
            board.task_revision += 1
            if change == "terminal":
                # Explicit terminal race-state mutation, solely to verify the
                # stale writer refuses it. It is never a dossier/readback or
                # capability-success acceptance substitute.
                board.status = WorkBoardStatus.done
                parent = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == spec.identity.job_id))
                parent.status = "succeeded"
                parent.revision += 1
                parent.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
                original = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == attempt.attempt_id))
                original.ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
                db.add_all([parent, original])
            db.add(board)
            await db.flush()
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=5)

    writer = asyncio.create_task(operator_writer())
    freezer = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        freezer = asyncio.create_task(freeze_quiescent(jobs, parent_id=spec.identity.job_id,
            owner="research-kernel", phase_binding=binding, expected_parent_revision=revision, reason="stale_writer"))
        await asyncio.sleep(0.03)
        assert not freezer.done()  # actual shared SQLite writer exclusion
    finally:
        release.set()
        await asyncio.wait_for(writer, timeout=5)
    assert await asyncio.wait_for(freezer, timeout=5) is False
    await accounting_db[1].dispose()
    async with accounting_db[2].accounting_sessions() as db:
        board = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task.task_id))
        parent = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == spec.identity.job_id))
        assert board.block_reason != "stale_writer" and board.task_revision == binding["task_revision"]+1
        assert parent.status == ("succeeded" if change == "terminal" else "paused")


@pytest.mark.asyncio
async def test_forged_completion_cannot_freeze_current_phase(accounting_db):
    jobs, task, attempt, spec, binding, revision = await closed_phase(accounting_db)
    binding["_completion"] = _PhaseCompletion(spec.identity.job_id, "research-kernel", _binding_tuple(binding), asyncio.current_task())
    assert await freeze_quiescent(jobs, parent_id=spec.identity.job_id, owner="research-kernel",
        phase_binding=binding, expected_parent_revision=revision, reason="forged_completion") is False
    assert (await jobs.get_job(spec.identity.job_id))["status"] == "paused"
