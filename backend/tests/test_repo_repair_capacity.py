from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from src.approval.metadata import approval_wire_metadata
from src.work_board.dispatcher import _repo_repair_execution_deadline_at
from src.workflows.job_runtime import (
    DurableJobAdmissionDenied,
    DurableJobIdentity,
    DurableJobSpec,
    DurableJobTransitionError,
    _bounded_checkpoint_receipts,
    _digest,
    durable_job_repository,
)
from src.db.models import WorkflowRunState
from src.workflows.repair_capacity import (
    REPO_REPAIR_EXECUTION_LANE_LOCK_NAME,
    RepoRepairCapacityError,
    try_acquire_repo_repair_capacity,
)


def test_approval_metadata_requires_explicit_host_permission():
    assert approval_wire_metadata({"summary": "run on the local host"}) == {}
    assert approval_wire_metadata(
        {
            "local_host_execution_required": True,
            "required_permissions": ["workspace_write"],
            "executor_kind": "local",
        }
    )["local_host_execution_required"] is False
    payload = approval_wire_metadata(
        {
            "local_host_execution_required": True,
            "required_permissions": ["local_host_execution", "workspace_write"],
            "executor_kind": "local",
            "executor_profile": "local:repo-python-pytest-v1",
            "executor_posture_digest": "A" * 64,
        }
    )
    assert payload["local_host_execution_required"] is True
    assert payload["required_permissions"] == ["local_host_execution", "workspace_write"]
    assert payload["executor_posture_digest"] == "a" * 64


def test_repair_capacity_quarantine_blocks_other_jobs_but_allows_same_job(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    lane = try_acquire_repo_repair_capacity(root, job_id="job-a")
    assert lane is not None
    try:
        lane.quarantine("job-a")
        assert try_acquire_repo_repair_capacity(root, job_id="job-b") is None
        assert try_acquire_repo_repair_capacity(root, job_id="job-a") is lane
        lane.release_after_denied()
        replacement = try_acquire_repo_repair_capacity(root, job_id="job-b")
        assert replacement is not None
        replacement.release()
    finally:
        lane.release_after_denied()


def test_repair_capacity_rejects_preexisting_unsafe_lock_without_chmod(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    lock_path = root / REPO_REPAIR_EXECUTION_LANE_LOCK_NAME
    lock_path.touch(mode=0o600)
    lock_path.chmod(0o644)

    with pytest.raises(RepoRepairCapacityError, match="safe regular file"):
        try_acquire_repo_repair_capacity(root, job_id="job-a")
    assert lock_path.stat().st_mode & 0o777 == 0o644


def test_repair_capacity_clear_after_durable_settlement_releases_normal_lane(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    lane = try_acquire_repo_repair_capacity(root, job_id="job-a")
    assert lane is not None
    lane.clear_quarantine()
    replacement = try_acquire_repo_repair_capacity(root, job_id="job-b")
    assert replacement is not None
    replacement.release()


def test_repair_deadline_is_bounded_by_goal_leases_and_approval():
    now = datetime.now(timezone.utc)
    deadline = datetime.fromisoformat(
        _repo_repair_execution_deadline_at(
            projection={
                "deadline_at": now + timedelta(seconds=80),
                "lease": {"expires_at": now + timedelta(seconds=70)},
            },
            authority={
                "deadline_seconds": 90,
                "deadline_at": now + timedelta(seconds=60),
            },
            approval_expires_at=now + timedelta(seconds=50),
            max_wall_seconds=180,
            board_lease_expires_at=now + timedelta(seconds=40),
            goal_window_at=now + timedelta(seconds=30),
        )
    )
    assert now < deadline <= now + timedelta(seconds=31)


def test_checkpoint_trim_retains_exact_repair_hold_after_history_churn():
    held = {
        "checkpoint_id": "repo-repair-execution-reservation",
        "payload": {
            "kind": "repo_repair_execution_reservation",
            "status": "held",
            "job_id": "repair-job",
            "attempt_id": "attempt-1",
            "fence": 3,
            "authority_digest": "a" * 64,
        },
    }
    retained = _bounded_checkpoint_receipts(
        [held, *({"checkpoint_id": f"step-{index}"} for index in range(51))]
    )
    assert len(retained) == 50
    assert any(item.get("checkpoint_id") == held["checkpoint_id"] for item in retained)


async def _claim_repair_capacity_job(job_id: str):
    authority = {
        "principal": "service:repair-capacity-malformed-test",
        "service_id": "service:repair-capacity-malformed-test",
        "session_id": "repair-capacity-malformed-session",
    }
    spec = DurableJobSpec(
        identity=DurableJobIdentity(
            job_id=job_id,
            owner_kind="service",
            owner_principal_id="service:repair-capacity-malformed-test",
            job_kind="engineering.repo-repair.v1",
            capability_version="1",
            idempotency_scope="repair-capacity-malformed-test",
            idempotency_key=job_id,
        ),
        inputs={"schema_version": 1, "capability_id": "engineering.repo-repair.v1"},
        session_id="repair-capacity-malformed-session",
        operator_session_id="repair-capacity-malformed-session",
        resource_claims=("repo-repair-execution",),
        declared_authority=authority,
        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        max_attempts=1,
        service_id="service:repair-capacity-malformed-test",
        budget_microusd=0,
        budget_digest=_digest({"budget_microusd": 0}),
    )
    admitted = await durable_job_repository.admit_job(spec)
    await durable_job_repository.queue_job(admitted["job_id"])
    claimed = await durable_job_repository.claim_job(
        admitted["job_id"], owner=f"repair-capacity-malformed-runner:{job_id}", lease_seconds=600
    )
    return authority, claimed


async def _corrupt_checkpoint_history(async_db, job_id: str) -> str:
    malformed = '[{"checkpoint_id":"repo-repair-execution-reservation","payload":]'
    async with async_db() as db:
        row = (
            await db.execute(
                select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id)
            )
        ).scalar_one_or_none()
        assert row is not None
        row.checkpoint_receipts_json = malformed
        await db.flush()
    return malformed


async def _checkpoint_history(async_db, job_id: str) -> str:
    async with async_db() as db:
        row = (
            await db.execute(
                select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id)
            )
        ).scalar_one_or_none()
        assert row is not None
        return str(row.checkpoint_receipts_json)


@pytest.mark.asyncio
async def test_malformed_repair_history_rejects_generic_checkpoint_and_successor(async_db):
    authority, claimed = await _claim_repair_capacity_job("repair-malformed-generic")
    fence = int(claimed["lease"]["fencing_token"])
    held = await durable_job_repository.reserve_repo_repair_execution(
        claimed["job_id"],
        owner=claimed["lease"]["owner"],
        fencing_token=fence,
        attempt_id="attempt-malformed-generic",
        authority_digest=str(claimed["authority_digest"]),
        execution_deadline_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        expected_revision=claimed["revision"],
    )
    malformed = await _corrupt_checkpoint_history(async_db, claimed["job_id"])

    with pytest.raises(DurableJobTransitionError, match="reservation history is malformed"):
        await durable_job_repository.record_checkpoint(
            claimed["job_id"],
            checkpoint_id="repair-malformed-generic-followup",
            state={"phase": "repair"},
            owner=claimed["lease"]["owner"],
            fencing_token=fence,
            expected_revision=held["revision"],
        )
    assert await _checkpoint_history(async_db, claimed["job_id"]) == malformed

    _successor_authority, successor = await _claim_repair_capacity_job("repair-malformed-generic-successor")
    with pytest.raises(DurableJobTransitionError, match="reservation history is malformed"):
        await durable_job_repository.reserve_repo_repair_execution(
            successor["job_id"],
            owner=successor["lease"]["owner"],
            fencing_token=int(successor["lease"]["fencing_token"]),
            attempt_id="attempt-malformed-generic-successor",
            authority_digest=str(successor["authority_digest"]),
            expected_revision=successor["revision"],
        )


@pytest.mark.asyncio
async def test_malformed_repair_history_rejects_recovery_checkpoint_and_successor(async_db):
    authority, claimed = await _claim_repair_capacity_job("repair-malformed-recovery")
    fence = int(claimed["lease"]["fencing_token"])
    held = await durable_job_repository.reserve_repo_repair_execution(
        claimed["job_id"],
        owner=claimed["lease"]["owner"],
        fencing_token=fence,
        attempt_id="attempt-malformed-recovery",
        authority_digest=str(claimed["authority_digest"]),
        execution_deadline_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        expected_revision=claimed["revision"],
    )
    blocked = await durable_job_repository.transition_job(
        claimed["job_id"],
        "blocked",
        owner=claimed["lease"]["owner"],
        fencing_token=fence,
        expected_revision=held["revision"],
        reason="repair_malformed_recovery_test",
    )
    malformed = await _corrupt_checkpoint_history(async_db, claimed["job_id"])

    with pytest.raises(DurableJobTransitionError, match="reservation history is malformed"):
        await durable_job_repository.record_recovery_checkpoint(
            claimed["job_id"],
            owner_kind="service",
            owner_principal_id=authority["principal"],
            checkpoint_id="repair-malformed-recovery-followup",
            state={"phase": "recovery"},
            checkpoint_payload={"safe": True},
            expected_revision=blocked["revision"],
        )
    assert await _checkpoint_history(async_db, claimed["job_id"]) == malformed

    _successor_authority, successor = await _claim_repair_capacity_job("repair-malformed-recovery-successor")
    with pytest.raises(DurableJobTransitionError, match="reservation history is malformed"):
        await durable_job_repository.reserve_repo_repair_execution(
            successor["job_id"],
            owner=successor["lease"]["owner"],
            fencing_token=int(successor["lease"]["fencing_token"]),
            attempt_id="attempt-malformed-recovery-successor",
            authority_digest=str(successor["authority_digest"]),
            expected_revision=successor["revision"],
        )


@pytest.mark.asyncio
async def test_unresolved_repair_reservation_survives_checkpoint_churn_and_blocks_successor(async_db):
    authority = {
        "principal": "service:repair-capacity-test",
        "service_id": "service:repair-capacity-test",
        "session_id": "repair-capacity-session",
    }

    def make_spec(job_id: str) -> DurableJobSpec:
        return DurableJobSpec(
            identity=DurableJobIdentity(
                job_id=job_id,
                owner_kind="service",
                owner_principal_id="service:repair-capacity-test",
                job_kind="engineering.repo-repair.v1",
                capability_version="1",
                idempotency_scope="repair-capacity-test",
                idempotency_key=job_id,
            ),
            inputs={"schema_version": 1, "capability_id": "engineering.repo-repair.v1"},
            session_id="repair-capacity-session",
            operator_session_id="repair-capacity-session",
            resource_claims=("repo-repair-execution",),
            declared_authority=authority,
            deadline_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            max_attempts=1,
            service_id="service:repair-capacity-test",
            budget_microusd=0,
            budget_digest=_digest({"budget_microusd": 0}),
        )

    admitted = await durable_job_repository.admit_job(make_spec("repair-capacity-held"))
    await durable_job_repository.queue_job(admitted["job_id"])
    claimed = await durable_job_repository.claim_job(
        admitted["job_id"], owner="repair-capacity-runner", lease_seconds=600
    )
    fence = int(claimed["lease"]["fencing_token"])
    authority_digest = str(claimed["authority_digest"])
    current = await durable_job_repository.reserve_repo_repair_execution(
        admitted["job_id"],
        owner="repair-capacity-runner",
        fencing_token=fence,
        attempt_id="attempt-1",
        authority_digest=authority_digest,
        execution_deadline_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        expected_revision=claimed["revision"],
    )
    for index in range(55):
        current = await durable_job_repository.record_checkpoint(
            admitted["job_id"],
            checkpoint_id=f"repair-churn-{index}",
            state={"phase": "repair", "index": index},
            owner="repair-capacity-runner",
            fencing_token=fence,
            expected_revision=current["revision"],
        )

    reservation = next(
        item
        for item in reversed(current["checkpoints"])
        if item.get("checkpoint_id") == "repo-repair-execution-reservation"
    )
    assert reservation["payload"]["status"] == "held"
    recovered = await durable_job_repository.reserve_repo_repair_execution(
        admitted["job_id"],
        owner="repair-capacity-runner",
        fencing_token=fence,
        attempt_id="attempt-1",
        authority_digest=authority_digest,
        expected_revision=current["revision"],
    )
    assert recovered["receipt"]["status"] == "deduped"

    successor = await durable_job_repository.admit_job(make_spec("repair-capacity-successor"))
    await durable_job_repository.queue_job(successor["job_id"])
    successor_claim = await durable_job_repository.claim_job(
        successor["job_id"], owner="repair-capacity-successor-runner", lease_seconds=600
    )
    with pytest.raises(DurableJobAdmissionDenied, match="repo_repair_execution_busy"):
        await durable_job_repository.reserve_repo_repair_execution(
            successor["job_id"],
            owner="repair-capacity-successor-runner",
            fencing_token=int(successor_claim["lease"]["fencing_token"]),
            attempt_id="attempt-successor",
            authority_digest=str(successor_claim["authority_digest"]),
            expected_revision=successor_claim["revision"],
        )
