"""Generic durable lifecycle cannot waive an exact task approval precontact wait."""
import socket
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from config.settings import settings
from src.db.models import WorkflowRunState
from src.workflows.job_runtime import DurableJobTransitionError, durable_job_repository
from tests.test_work_board_m6_provider_free_journey import isolated_runtime


@pytest.fixture(autouse=True)
def no_external_contacts(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("external sockets forbidden in approval resume guard tests")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")


async def seed_root(sessions, *, status="paused", reason="general_task_approval_required"):
    now = datetime.now(timezone.utc)
    async with sessions() as db:
        db.add(WorkflowRunState(run_identity="guard-root", root_run_identity="guard-root",
            workflow_name="agent.task.v1", job_kind="agent.task.v1", status=status,
            failure_reason=reason, revision=7, fencing_token=3, attempt_count=1,
            lease_owner="guard-worker" if status == "running" else None,
            lease_expires_at=now + timedelta(minutes=1) if status == "running" else None,
            deadline_at=now + timedelta(minutes=2)))


async def unchanged_root(sessions):
    async with sessions() as db:
        root = (await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == "guard-root"))).scalar_one()
        assert root.status == "paused"
        assert root.failure_reason == "general_task_approval_required"
        assert root.revision == 7 and root.fencing_token == 3
        assert root.attempt_count == 1
        assert root.lease_owner is None and root.lease_expires_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["resume_job", "queue_job", "transition_job"])
async def test_generic_queue_paths_cannot_resume_task_approval_wait(isolated_runtime, method):
    sessions, _workspace = isolated_runtime
    await seed_root(sessions)
    call = getattr(durable_job_repository, method)
    arguments = ("guard-root", "queued") if method == "transition_job" else ("guard-root",)
    with pytest.raises(DurableJobTransitionError, match="current validated authority"):
        await call(*arguments, expected_revision=7)
    await unchanged_root(sessions)


@pytest.mark.asyncio
@pytest.mark.parametrize("forgery", [{"approved": True}, object(), "unsealed_typed_witness"])
async def test_forged_witness_cannot_queue_task_approval_wait(isolated_runtime, monkeypatch, forgery):
    from src.work_board import general_task_approval
    if forgery == "unsealed_typed_witness":
        forgery = general_task_approval._ResumeWitness(
            service=None, owner=None, request=None, task_id="guard-task", seal=object())
    recheck = AsyncMock(wraps=general_task_approval.recheck_resume_witness)
    monkeypatch.setattr(general_task_approval, "recheck_resume_witness", recheck)
    sessions, _workspace = isolated_runtime
    await seed_root(sessions)
    with pytest.raises(DurableJobTransitionError, match="current validated authority"):
        await durable_job_repository.transition_job("guard-root", "queued",
            expected_revision=7, expected_fencing_token=3,
            _general_task_resume_witness=forgery)
    assert recheck.await_count == 1
    assert recheck.await_args.args[2] is forgery
    await unchanged_root(sessions)


@pytest.mark.asyncio
async def test_exact_native_pause_reason_survives_release_of_worker_lease(isolated_runtime):
    sessions, _workspace = isolated_runtime
    await seed_root(sessions, status="running", reason=None)
    paused = await durable_job_repository.pause_job("guard-root", owner="guard-worker",
        fencing_token=3, expected_revision=7, reason="general_task_approval_required")
    assert paused["status"] == "paused"
    assert paused["failure_reason"] == "general_task_approval_required"
    assert paused["revision"] == 8
    assert paused["lease"]["owner"] is None
    with pytest.raises(DurableJobTransitionError, match="current validated authority"):
        await durable_job_repository.resume_job("guard-root", expected_revision=8)


@pytest.mark.asyncio
async def test_ordinary_operator_pause_still_resumes_without_approval_witness(isolated_runtime):
    sessions, _workspace = isolated_runtime
    await seed_root(sessions, reason="operator_paused")
    resumed = await durable_job_repository.resume_job("guard-root", expected_revision=7)
    assert resumed["status"] == "queued"
    assert resumed["revision"] == 8
    assert resumed["lease"]["fencing_token"] == 3
    assert resumed["attempt_count"] == 1


@pytest.mark.asyncio
async def test_generic_block_transition_cannot_erase_checkpoint_approval_guard(isolated_runtime):
    sessions, _workspace = isolated_runtime
    await seed_root(sessions)
    async with sessions() as db:
        root = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == "guard-root"))
        root.checkpoint_receipts_json = json.dumps([{"checkpoint_id": "general:step:read",
            "payload": {"phase": "approval_precontact", "approval_id": "exact-approval"}}])
    blocked = await durable_job_repository.transition_job("guard-root", "blocked",
        expected_revision=7, reason="operator_blocked")
    assert blocked["status"] == "blocked"
    with pytest.raises(DurableJobTransitionError, match="current validated authority"):
        await durable_job_repository.resume_job("guard-root", expected_revision=blocked["revision"])
    assert (await durable_job_repository.get_job("guard-root"))["status"] == "blocked"


def test_no_contact_readback_cannot_prove_task_output():
    from src.workflows.job_runtime import _verified_readback_exists
    from src.work_board.dispatcher import WorkBoardDispatcher
    receipt = {"receipt_kind": "readback", "effect_type": "general_tool_call",
        "status": "succeeded", "target_path": "general-step:" + "a" * 64,
        "content_sha256": "b" * 64, "readback_id": "general-precontact:exact",
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "details": {"verified": True, "never_contacted": True, "approval_precontact": True}}
    assert _verified_readback_exists([receipt]) is False
    assert WorkBoardDispatcher._workflow_readback({"run_identity": "guard-root",
        "effects": [receipt]}, "guard-root") is None
