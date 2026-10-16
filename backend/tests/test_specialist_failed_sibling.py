"""Actual failed specialist retains its completed sibling without replay."""
import json

import pytest
from sqlalchemy import select
from tests.general_task_method_lifecycle import native_admission_lifecycle

from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime
from tests.test_specialist_parent_synthesis import genuine_mcp_registry, charged_parent


@pytest.mark.asyncio
async def test_failed_child_retains_successful_sibling_and_holds_parent(task_runtime, monkeypatch, native_admission_lifecycle):
    from src.db.models import WorkBoardTask, WorkBoardAttempt, WorkflowRunState
    from src.workflows.general_task_guard import read_manifest

    async with genuine_mcp_registry(task_runtime[1], monkeypatch) as (registry, descriptor, received, protocol):
        sessions, workspace, owner, service, dispatcher, planner, transport, request, task_id = await charged_parent(
            task_runtime, monkeypatch, registry, descriptor)
        # Stock write_file will encounter a real filesystem error after the
        # original specialist Task, Attempt, job and native call are admitted.
        (workspace / "child-B.txt").mkdir()
        await dispatcher.run_pass()
        for _ in range(8):
            await dispatcher.reconcile_linked_attempts()
            async with sessions() as db:
                children = list((await db.execute(select(WorkBoardTask).where(
                    WorkBoardTask.idempotency_key.like("specialist:%")))).scalars())
                if len(children) == 2 and any(child.status.value == "done" for child in children):
                    jobs = list((await db.execute(select(WorkflowRunState).where(
                        WorkflowRunState.job_kind == "agent.task.v1",
                        WorkflowRunState.parent_job_id.is_not(None)))).scalars())
                    if any(job.failure_reason and job.status != "accepted" for job in jobs):
                        break
        async with sessions() as db:
            children = list((await db.execute(select(WorkBoardTask).where(
                WorkBoardTask.idempotency_key.like("specialist:%")))).scalars())
            assert len(children) == 2
            parent = await db.scalar(select(WorkflowRunState).where(
                WorkflowRunState.job_kind == "agent.task.v1", WorkflowRunState.parent_job_id.is_(None)))
            assert parent.status == "paused" and read_manifest(parent).phase == "native_wait"
            successful = [child for child in children if child.status.value == "done"]
            assert len(successful) == 1
            failed = next(child for child in children if child.task_id != successful[0].task_id)
            attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == failed.task_id))
            assert attempt is not None and attempt.workflow_run_id
            failed_job = await dispatcher.jobs._fetch(db, attempt.workflow_run_id)
            assert failed_job.status != "succeeded"
            native_calls = list((await db.execute(select(WorkflowRunState).where(
                WorkflowRunState.parent_job_id == failed_job.run_identity))).scalars())
            assert len(native_calls) == 1
            assert native_calls[0].attempt_count == 1
            assert json.loads(native_calls[0].effect_receipts_json)
            identities = sorted((child.task_id, child.idempotency_key) for child in children)
            callback_identity = native_calls[0].run_identity
            callback_attempt = native_calls[0].attempt_count
            completed_plan = await service.plan(db, owner, successful[0].task_id)
            assert completed_plan["native_execution"]["phase"] == "complete"
        assert (workspace / "child-A.txt").read_bytes() == b"physical first\n"
        assert (workspace / "child-B.txt").is_dir()
        assert not (workspace / "parent.txt").exists() and not received
        contacts = len(transport["contacts"])
        for _ in range(2):
            await dispatcher.reconcile_linked_attempts()
        async with sessions() as db:
            children = list((await db.execute(select(WorkBoardTask).where(
                WorkBoardTask.idempotency_key.like("specialist:%")))).scalars())
            assert sorted((child.task_id, child.idempotency_key) for child in children) == identities
            callback = await dispatcher.jobs._fetch(db, callback_identity)
            assert callback.attempt_count == callback_attempt
            parent = await dispatcher.jobs._fetch(db, parent.run_identity)
            assert parent.status == "paused" and read_manifest(parent).phase == "native_wait"
        assert len(transport["contacts"]) == contacts
        assert (workspace / "child-A.txt").read_bytes() == b"physical first\n"
