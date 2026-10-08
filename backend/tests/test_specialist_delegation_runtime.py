"""Disposable actual Board children; scripted final inference transport only."""
from dataclasses import replace

import pytest
from sqlalchemy import select

from src.db.models import WorkBoardTask, WorkflowRunState
from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime
from tests.test_general_task_specialist_planning import specialist_fixture


@pytest.mark.asyncio
async def test_actual_specialist_board_child_output_is_read_back(task_runtime, monkeypatch):
    import src.work_board.general_task_native as native
    original_native = native.run_native_step
    async def observed_native(*args, **kwargs):
        try:
            return await original_native(*args, **kwargs)
        except BaseException:
            import traceback
            traceback.print_exc()
            raise
    monkeypatch.setattr(native, "run_native_step", observed_native)
    monkeypatch.setattr("src.workflows.job_runtime.get_session", task_runtime[0])
    sessions, dispatcher, planner, transport, owner, group, accounting, inputs, descriptors, provenance = await specialist_fixture(task_runtime, monkeypatch)
    service = dispatcher.general_tasks
    service.planner = planner
    service.delegation_jobs = dispatcher.jobs
    (task_runtime[1] / "notes.txt").write_text("literal specialist artifact source", encoding="utf-8")
    from src.auth.service import authenticate_session
    operator = await authenticate_session(owner.session_id, touch=False)
    invocation_id = accounting["delegation_invocation_id"]
    principal = replace(operator.principal, session_id=owner.session_id,
        operator_session_id=owner.session_id, job_id=invocation_id)
    from src.workflows.specialist_delegation import execute_specialist
    try:
        result = await execute_specialist(dispatcher.jobs, service=service,
            invocation_id=invocation_id, fencing_token=accounting["parent_fence"], principal=principal)
    except BaseException:
        import traceback
        traceback.print_exc()
        async with sessions() as db:
            tasks = list((await db.execute(select(WorkBoardTask))).scalars())
            runs = list((await db.execute(select(WorkflowRunState))).scalars())
            print("Literal task states:", [(item.task_id, item.status.value, item.block_reason) for item in tasks], flush=True)
            print("Literal run states:", [(item.run_identity, item.status, item.failure_reason, item.branch_depth) for item in runs], flush=True)
        raise
    assert result["unresolved"] == []
    assert result["summary_ref"] in result["artifact_refs"]
    assert len(transport["contacts"]) == 1
    async with sessions() as db:
        child = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == result["child_id"]))
        assert child.status.value == "done"
        runs = list((await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.parent_job_id == invocation_id))).scalars())
        assert len(runs) == 1
        assert runs[0].status == "succeeded"
        assert runs[0].branch_depth == 2
        assert runs[0].root_run_identity != runs[0].run_identity
    recovered = await execute_specialist(dispatcher.jobs, service=service,
        invocation_id=invocation_id, fencing_token=accounting["parent_fence"], principal=principal)
    assert recovered == result
    assert len(transport["contacts"]) == 1
