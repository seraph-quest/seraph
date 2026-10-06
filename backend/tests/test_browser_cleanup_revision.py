"""Cleanup acknowledges an observed close under the original native lease."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlmodel import select

from src.browser.task_runner import BrowserTaskRunner, BrowserTaskError, _ExecutionState
from src.db.models import Goal, WorkflowRunState, InferenceCostReservation
from src.workflows.job_runtime import DurableJobRepository, DurableJobLeaseError
from tests.test_browser_task_runtime import _input, _ARTIFACT_DIGEST


async def _cost_inventory(async_db):
    async with async_db() as db:
        rows = (await db.execute(select(InferenceCostReservation))).scalars().all()
        return sorted(json.dumps(row.model_dump(mode="json"), sort_keys=True) for row in rows)


async def _admitted(async_db, tmp_path, *, launcher=None, controls=None):
    args = dict(task_id="cleanup-task", attempt_id="cleanup-attempt",
        owner_principal_id="cleanup-operator", owner_session_id="cleanup-session",
        goal_id="cleanup-goal", goal_revision=1, board_task_revision=2,
        board_fencing_token=3, input_artifact_id="cleanup-input",
        input_artifact_digest=_ARTIFACT_DIGEST, inputs=_input(),
        runtime_seconds=30, task_priority=50)
    async with async_db() as db:
        db.add(Goal(id=args["goal_id"], title="Cleanup", status="active", revision=1,
            owner_principal_id=args["owner_principal_id"], owner_session_id=args["owner_session_id"]))
    jobs = DurableJobRepository()
    runner = BrowserTaskRunner(jobs=jobs, workspace_root=tmp_path,
        browser_launcher=launcher, runtime_controls=controls or (lambda **_: True))
    receipt = await runner.run(**args, admission_only=True)
    assert receipt["status"] == "admitted"
    return jobs, runner, args, receipt["job_id"]


async def _claimed(async_db, tmp_path):
    jobs, runner, args, job_id = await _admitted(async_db, tmp_path)
    queued = await jobs.queue_job(job_id)
    row = await jobs.claim_job(job_id, owner="cleanup-worker", lease_seconds=30,
        expected_revision=queued["revision"], expected_fencing_token=0)
    authority = row["declared_authority"]
    state = _ExecutionState(**{key: args[key] for key in (
        "task_id", "attempt_id", "owner_principal_id", "owner_session_id", "goal_id",
        "goal_revision", "board_task_revision", "board_fencing_token", "input_artifact_id",
        "input_artifact_digest", "task_priority")}, job_id=job_id,
        admission_board_task_revision=args["board_task_revision"],
        input_envelope_digest=authority["input_envelope_digest"],
        input_model_digest=authority["browser_input_digest"],
        action_consent_digest=authority["action_consent_digest"], action_count=authority["action_count"],
        lease_owner="cleanup-worker", fencing_token=row["lease"]["fencing_token"],
        revision=row["revision"], runtime_seconds=30,
        execution_deadline_monotonic=time.monotonic()+30)
    return jobs, runner, state, row


@pytest.mark.asyncio
async def test_real_native_stale_cancel_checkpoint_cleanup_refresh(async_db, tmp_path):
    jobs, runner, state, before = await _claimed(async_db, tmp_path)
    original_cost = await _cost_inventory(async_db)
    changed = await jobs.record_checkpoint(state.job_id, checkpoint_id="cancel_requested",
        state={"phase": "cancel_requested"}, owner=state.lease_owner,
        fencing_token=state.fencing_token, expected_revision=state.revision)
    assert changed["revision"] > state.revision
    # This is the old cleanup write against the real native CAS, before refresh.
    with pytest.raises(DurableJobLeaseError, match="revision is stale"):
        await jobs.record_effect(state.job_id, effect_id="browser-cleanup:"+state.job_id,
            effect_type="browser_context_cleanup", status="succeeded",
            details={"cleanup_status":"cleanup_verified", "context_not_started":False,
                "memory_status":"no_learning"}, owner=state.lease_owner,
            fencing_token=state.fencing_token, expected_revision=state.revision)
    assert (await jobs.get_job(state.job_id))["effects"] == []
    control_calls = []
    def revoked_control(**kwargs):
        control_calls.append(kwargs)
        return False
    runner.runtime_controls = revoked_control
    assert await runner._record_cleanup_effect(state, cleanup_status="cleanup_verified", context_not_started=False)
    after = await jobs.get_job(state.job_id)
    assert after["effects"][0]["status"] == "succeeded"
    assert after["declared_authority"] == before["declared_authority"]
    assert after["input_digest"] == before["input_digest"]
    assert after["deadline_at"] == before["deadline_at"]
    assert {k: v for k, v in after["lease"].items() if k != "revision"} == {k: v for k, v in before["lease"].items() if k != "revision"}
    assert after["attempt_count"] == before["attempt_count"]
    assert await _cost_inventory(async_db) == original_cost
    assert control_calls == []
    with pytest.raises(BrowserTaskError) as denied:
        await runner._assert_current(state)
    assert denied.value.code == "board_fence_stale"
    assert len(control_calls) == 1


@pytest.mark.asyncio
async def test_prelaunch_cleanup_uses_same_fresh_binding(async_db, tmp_path):
    jobs, runner, state, before = await _claimed(async_db, tmp_path)
    await jobs.record_checkpoint(state.job_id, checkpoint_id="cancel_requested",
        state={"phase":"cancel_requested"}, owner=state.lease_owner, fencing_token=state.fencing_token)
    assert await runner._record_prelaunch_cleanup_effect(job_id=state.job_id,
        lease_owner=state.lease_owner, fencing_token=state.fencing_token,
        binding=runner._cleanup_binding(state))
    after = await jobs.get_job(state.job_id)
    assert after["effects"][0]["details"]["context_not_started"] is True
    assert after["deadline_at"] == before["deadline_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["owner", "fence", "binding", "routine_parent", "lease_expiry", "deadline", "terminal", "cas_loss"])
async def test_cleanup_refresh_never_weakens_native_guards(async_db, tmp_path, mutation, monkeypatch):
    jobs, runner, state, before = await _claimed(async_db, tmp_path)
    if mutation == "owner":
        state.lease_owner = "foreign-worker"
    elif mutation == "fence":
        state.fencing_token += 1
    elif mutation == "cas_loss":
        original = jobs.record_effect
        async def raced(job_id, **kwargs):
            await jobs.record_checkpoint(job_id, checkpoint_id="racing-checkpoint", state={"phase":"race"},
                owner=kwargs["owner"], fencing_token=kwargs["fencing_token"])
            return await original(job_id, **kwargs)
        monkeypatch.setattr(jobs, "record_effect", raced)
    else:
        async with async_db() as db:
            row = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == state.job_id))).scalar_one()
            if mutation in {"binding", "routine_parent"}:
                value = json.loads(row.declared_authority_json)
                if mutation == "binding":
                    value["input_artifact_id"] = "foreign-input"
                else:
                    value["routine_parent_job_id"] = "foreign-parent"
                    value["routine_parent_fencing_token"] = 1
                row.declared_authority_json = json.dumps(value)
            elif mutation == "lease_expiry":
                row.lease_expires_at = datetime.now(timezone.utc)-timedelta(seconds=1)
            elif mutation == "deadline":
                row.deadline_at = datetime.now(timezone.utc)-timedelta(seconds=1)
            else:
                row.status = "cancelled"
    assert not await runner._record_cleanup_effect(state, cleanup_status="cleanup_verified", context_not_started=False)
    assert (await jobs.get_job(state.job_id))["effects"] == []


@pytest.mark.asyncio
async def test_failed_physical_close_cannot_publish_positive_cleanup(async_db, tmp_path):
    from src.browser.task_runner import _BrowserLaunchResources
    class BrokenContext:
        async def close(self):
            raise RuntimeError("close failed")
    resources = _BrowserLaunchResources(launch_attempted=True, context=BrokenContext())
    jobs, runner, state, _ = await _claimed(async_db, tmp_path)
    assert not await runner._close_launch_resources_bounded(resources, timeout_seconds=1)
    assert await runner._record_cleanup_effect(state, cleanup_status="cleanup_unknown", context_not_started=False)
    effects = (await jobs.get_job(state.job_id))["effects"]
    assert len(effects) == 1 and effects[0]["status"] == "unknown"


@pytest.mark.asyncio
async def test_actual_chromium_context_cancel_checkpoint_records_original_cleanup(async_db, tmp_path):
    from playwright.async_api import async_playwright
    entered = asyncio.Event()
    closed = asyncio.Event()
    handle = None
    class Context:
        def __init__(self, context):
            self.context = context
        async def new_page(self):
            entered.set()
            await asyncio.Event().wait()
        async def close(self):
            await self.context.close()
            closed.set()
    class Browser:
        def __init__(self, playwright, browser):
            self.playwright, self.browser = playwright, browser
        async def new_context(self, **kwargs):
            return Context(await self.browser.new_context(**kwargs))
        async def close(self):
            await self.browser.close()
            await self.playwright.stop()
    async def launch():
        nonlocal handle
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        handle = Browser(pw, browser)
        return handle
    jobs, runner, args, job_id = await _admitted(async_db, tmp_path, launcher=launch)
    execution = asyncio.create_task(runner.run(**args, admission_only=False))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        before = await jobs.get_job(job_id)
        original_cost = await _cost_inventory(async_db)
        lease = before["lease"]
        await jobs.record_checkpoint(job_id, checkpoint_id="cancel_requested", state={"phase":"cancel_requested"},
            owner=lease["owner"], fencing_token=lease["fencing_token"], expected_revision=before["revision"])
        runner.runtime_controls = lambda **_: False
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
        assert closed.is_set()
        after = await jobs.get_job(job_id)
        cleanup = [effect for effect in after["effects"] if effect["effect_type"] == "browser_context_cleanup"]
        assert len(cleanup) == 1 and cleanup[0]["status"] == "succeeded"
        assert cleanup[0]["details"]["cleanup_status"] == "cleanup_verified"
        assert cleanup[0]["details"]["context_not_started"] is False
        assert after["job_id"] == before["job_id"]
        assert {k: v for k, v in after["lease"].items() if k != "revision"} == {k: v for k, v in before["lease"].items() if k != "revision"}
        assert after["deadline_at"] == before["deadline_at"]
        assert after["attempt_count"] == before["attempt_count"]
        assert after["declared_authority"] == before["declared_authority"]
        assert after["input_digest"] == before["input_digest"]
        assert await _cost_inventory(async_db) == original_cost
        assert after["status"] == "running"
    finally:
        if not execution.done():
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
        if handle is not None:
            await handle.close()
