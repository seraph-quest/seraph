"""Real SQLite browser revision races, with durable authority left intact."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from src.browser.task_runner import BrowserTaskInput, BrowserTaskRunner, _ExecutionState
from src.db.models import Goal
from src.workflows.job_runtime import DurableJobRepository
from tests.test_browser_task_runtime import _ARTIFACT_DIGEST, _input


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.parametrize("delayed_method", ["assert_active_lease", "record_artifact", "record_readback"])
async def test_browser_revision_snapshot_and_writers_serialize(async_db, tmp_path, delayed_method):
    async with async_db() as db:
        db.add(Goal(id="revision-goal", title="Revision race", status="active", revision=1,
                    owner_principal_id="revision-operator", owner_session_id="revision-session"))
    jobs = DurableJobRepository()
    runner = BrowserTaskRunner(jobs=jobs, runtime_controls=lambda **_: True, workspace_root=tmp_path)
    admitted = await runner.run(
        task_id="revision-task", attempt_id="revision-attempt",
        owner_principal_id="revision-operator", owner_session_id="revision-session",
        goal_id="revision-goal", goal_revision=1, board_task_revision=2, board_fencing_token=3,
        input_artifact_id="revision-input", input_artifact_digest=_ARTIFACT_DIGEST,
        inputs=_input(), runtime_seconds=180, admission_only=True, task_priority=50,
    )
    assert admitted["status"] == "admitted", admitted
    row = await jobs.get_job(admitted["job_id"])
    queued = await jobs.queue_job(row["job_id"], expected_revision=row["revision"])
    claimed = await jobs.claim_job(row["job_id"], owner="revision-worker", lease_seconds=180,
                                  expected_revision=queued["revision"],
                                  expected_fencing_token=queued["lease"]["fencing_token"])
    authority = claimed["declared_authority"]
    state = _ExecutionState(
        task_id="revision-task", attempt_id="revision-attempt", job_id=row["job_id"],
        owner_principal_id="revision-operator", owner_session_id="revision-session",
        goal_id="revision-goal", goal_revision=1, board_task_revision=2,
        admission_board_task_revision=2, board_fencing_token=3, input_artifact_id="revision-input",
        input_artifact_digest=_ARTIFACT_DIGEST, input_envelope_digest=authority["input_envelope_digest"],
        input_model_digest=authority["browser_input_digest"],
        action_consent_digest=authority["action_consent_digest"], action_count=authority["action_count"],
        task_priority=50, lease_owner="revision-worker", fencing_token=claimed["lease"]["fencing_token"],
        revision=claimed["revision"], action_deadline_monotonic=time.monotonic() + 120,
        execution_deadline_monotonic=time.monotonic() + 180,
    )
    snapshot_ready, release_snapshot = asyncio.Event(), asyncio.Event()

    class DelayedRepository:
        def __getattr__(self, name):
            actual = getattr(jobs, name)
            if name != delayed_method:
                return actual

            async def delayed(*args, **kwargs):
                result = await actual(*args, **kwargs)
                snapshot_ready.set()
                await release_snapshot.wait()
                return result
            return delayed

    runner.jobs = DelayedRepository()
    if delayed_method == "assert_active_lease":
        operation = runner._assert_current(state)
    else:
        operation = runner._write_and_readback(state, SimpleNamespace(url="https://fixture.example/docs"),
                                               BrowserTaskInput.model_validate(_input()))
    producer = asyncio.create_task(operation)
    competitor = None
    try:
        await asyncio.wait_for(snapshot_ready.wait(), 5)

        async def checkpoint():
            async with state.lock:
                await runner._checkpoint(state, checkpoint_id="concurrent-progress", payload={"phase": "progress"})

        competitor = asyncio.create_task(checkpoint())
        # The genuine returned snapshot is held while an actual strict-CAS
        # checkpoint tries to advance it. Without serialization it completes;
        # with serialization it waits until the snapshot has been applied.
        try:
            await asyncio.wait_for(asyncio.shield(competitor), 0.5)
        except asyncio.TimeoutError:
            pass
        release_snapshot.set()
        await asyncio.wait_for(asyncio.gather(producer, competitor), 5)
        async with state.lock:
            await runner._checkpoint(state, checkpoint_id="subsequent-progress", payload={"phase": "verified"})
        persisted = await jobs.get_job(state.job_id)
        assert state.revision == persisted["revision"]
    finally:
        release_snapshot.set()
        for task in (producer, competitor):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (producer, competitor) if task is not None), return_exceptions=True)
