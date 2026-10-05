"""Actual native guard registration and source-bound inputs, no model call."""
from dataclasses import replace

import pytest
from sqlalchemy import select, func

from tests.test_work_board_m6_provider_free_journey import isolated_runtime
from tests.test_guardian_opportunity_policy import publish_source
from src.db.models import GuardianOpportunity, InferenceCostReservation
from src.guardian.opportunity_runtime import admit_assessment, _authority, RUNNER
from src.workflows.job_runtime import (
    durable_job_repository, DurableJobAdmissionDenied, DurableJobLeaseError, DurableJobTransitionError,
)


async def test_new_packet_coalesces_existing_uncontacted_native_queue(isolated_runtime):
    from src.guardian.source_watch import SourceWatchService
    from tests.test_work_board_m6_provider_free_journey import SESSION
    sessions, _, watch, _, old, _ = await publish_source(isolated_runtime)
    queued = await admit_assessment(old.id)
    async def fetch(source):
        return "Stable public line\nA newer relevant public release with material detail\n", {"content_type": "text/plain"}
    newer = await SourceWatchService(fetcher=fetch).run_watch(watch["id"], occurrence_id="newer-material",
        expected_plan_revision=1, expected_owner_session_id=SESSION)
    assert newer["status"] == "succeeded", newer
    async with sessions() as db:
        rows = list((await db.execute(select(GuardianOpportunity).order_by(GuardianOpportunity.created_at))).scalars())
        assert len(rows) == 2
        assert rows[0].status == "silent" and rows[0].reason_code == "coalesced"
        assert rows[1].status == "queued"
    assert (await durable_job_repository.get_job(queued["job_id"]))["status"] == "cancelled"


async def test_dismissed_semantics_never_requeue_and_supersede_other_pending_packets(isolated_runtime):
    from src.guardian.source_watch import SourceWatchService
    from tests.test_work_board_m6_provider_free_journey import SESSION
    sessions, _, watch, _, original, _ = await publish_source(isolated_runtime)
    # This is a disposition/dedupe fixture, not a fabricated native success.
    async with sessions() as db:
        dismissed = await db.get(GuardianOpportunity, original.id)
        dismissed.status = "dismissed"
        db.add(dismissed)
    versions = iter(("Stable public line\nA different relevant public release with detail\n",
                     "Stable public line\nA relevant new public release\n"))
    async def fetch(source):
        return next(versions), {"content_type": "text/plain"}
    service = SourceWatchService(fetcher=fetch)
    for occurrence in ("different-material", "same-dismissed-material"):
        result = await service.run_watch(watch["id"], occurrence_id=occurrence,
            expected_plan_revision=1, expected_owner_session_id=SESSION)
        assert result["status"] == "succeeded", result
        if occurrence == "different-material":
            async with sessions() as db:
                pending = (await db.execute(select(GuardianOpportunity).where(
                    GuardianOpportunity.status == "queued"))).scalar_one()
            queued = await admit_assessment(pending.id)
    async with sessions() as db:
        rows = list((await db.execute(select(GuardianOpportunity))).scalars())
        assert len(rows) == 2  # New packet UUID does not alter semantic identity.
        assert (await db.get(GuardianOpportunity, original.id)).status == "dismissed"
        assert (await db.get(GuardianOpportunity, pending.id)).reason_code == "coalesced"
        assert not any(row.status in {"queued", "assessing"} for row in rows)
    assert (await durable_job_repository.get_job(queued["job_id"]))["status"] == "cancelled"


@pytest.mark.parametrize("capacity,count", [("goal", 1), ("owner", 2), ("host", 16)])
async def test_canonical_pending_capacity_blocks_before_native_admission(isolated_runtime, capacity, count):
    from uuid import uuid4
    from src.guardian.source_watch import SourceWatchService
    from tests.test_work_board_m6_provider_free_journey import OWNER, SESSION
    sessions, goal, watch, _, original, _ = await publish_source(isolated_runtime)
    async with sessions() as db:
        historical = await db.get(GuardianOpportunity, original.id)
        historical.status = "silent"
        db.add(historical)
        # Pending-row capacity fixture only: no successful job, accounting
        # reservation or intervention is seeded. Execution remains separately
        # subject to Root/Goal/watch/source admission and its native fence.
        for ordinal in range(count):
            occupied = GuardianOpportunity.model_validate(original.model_dump())
            occupied.id, occupied.dedupe_key = str(uuid4()), "occupied:"+str(ordinal)
            occupied.watch_id = str(uuid4())
            occupied.goal_id = goal.id if capacity == "goal" else str(uuid4())
            occupied.owner_principal_id = OWNER if capacity != "host" else "operator:capacity:"+str(ordinal)
            db.add(occupied)
    async def fetch(source):
        return "Stable public line\nA distinct new bounded material release\n", {"content_type": "text/plain"}
    result = await SourceWatchService(fetcher=fetch).run_watch(watch["id"], occurrence_id="capacity-material",
        expected_plan_revision=1, expected_owner_session_id=SESSION)
    assert result["status"] == "succeeded", result
    async with sessions() as db:
        denied = (await db.execute(select(GuardianOpportunity).where(
            GuardianOpportunity.reason_code == "opportunity_capacity_exhausted"))).scalar_one()
        assert denied.status == "blocked" and denied.job_id is None
        pending = list((await db.execute(select(GuardianOpportunity).where(
            GuardianOpportunity.status.in_(("queued", "assessing"))))).scalars())
        assert len(pending) == count
        assert (await db.execute(select(func.count()).select_from(InferenceCostReservation))).scalar() == 0


async def test_expired_never_contacted_native_lease_recovers_same_job_bounded(isolated_runtime, monkeypatch):
    import asyncio
    import json
    from datetime import datetime, timedelta, timezone
    from src.db.models import Goal, WorkflowRunState
    from src.guardian import opportunity_runtime
    import httpx
    sessions, goal, _, _, row, _ = await publish_source(isolated_runtime)
    async with sessions() as db:
        current_goal = await db.get(Goal, goal.id)
        budget = json.loads(current_goal.admission_budget_json)
        budget["max_attempts"] = 2
        current_goal.admission_budget_json = json.dumps(budget)
        db.add(current_goal)
    queued = await admit_assessment(row.id)
    async def claim(db, run):
        current, _ = await _authority(db, row.id, execution=True)
        current.status = "assessing"
        current.revision += 1
        db.add(current)
    claimed = await durable_job_repository.claim_job(queued["job_id"], owner=RUNNER,
        expected_revision=queued["revision"], claim_authority_check=claim)
    async with sessions() as db:
        native = (await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == queued["job_id"]))).scalar_one()
        native.lease_expires_at = datetime.now(timezone.utc)-timedelta(seconds=1)
        db.add(native)
    def forbid_http(*args, **kwargs):
        raise AssertionError("unverified recovery must not reach HTTP")
    monkeypatch.setattr(httpx, "AsyncClient", forbid_http)
    tick = await opportunity_runtime.run_opportunity_tick()
    assert tick["started"] == 1
    task = opportunity_runtime._executions[row.id]
    await asyncio.wait_for(task, timeout=20)
    recovered = await durable_job_repository.get_job(queued["job_id"])
    assert recovered["job_id"] == claimed["job_id"]
    assert recovered["attempt_count"] == 2
    assert recovered["lease"]["fencing_token"] > claimed["lease"]["fencing_token"]
    assert (await opportunity_runtime.run_opportunity_tick())["started"] == 0
    async with sessions() as db:
        assert (await db.execute(select(func.count()).select_from(InferenceCostReservation))).scalar() == 0


async def test_fixed_native_kind_requires_all_current_authority_callbacks(isolated_runtime, monkeypatch):
    sessions, goal, watch, request, row, packet = await publish_source(isolated_runtime)
    original_admit = durable_job_repository.admit_job
    captured = []
    async def capture(spec, **kwargs):
        captured.append(spec)
        return await original_admit(spec, **kwargs)
    monkeypatch.setattr(durable_job_repository, "admit_job", capture)
    queued = await admit_assessment(row.id)
    assert queued["status"] == "queued"
    assert queued["capability_version"] == "guardian.opportunity-assess.v1"
    spec = captured[0]
    fresh_identity = replace(spec.identity, job_id=spec.identity.job_id + ":missing", idempotency_key="missing-callback")
    with pytest.raises(DurableJobAdmissionDenied, match="fixed_native_admission_required"):
        await original_admit(replace(spec, identity=fresh_identity))
    assert await durable_job_repository.get_job(fresh_identity.job_id) is None
    with pytest.raises(DurableJobLeaseError, match="fixed native authority"):
        await durable_job_repository.claim_job(queued["job_id"], owner=RUNNER, expected_revision=queued["revision"])
    assert (await durable_job_repository.get_job(queued["job_id"]))["status"] == "queued"

    async def actual_guard(db, run):
        current, _ = await _authority(db, row.id, execution=True)
        assert current.job_id == run.run_identity
        current.status = "assessing"
        current.revision += 1
        db.add(current)
    claimed = await durable_job_repository.claim_job(queued["job_id"], owner=RUNNER,
        expected_revision=queued["revision"], claim_authority_check=actual_guard)
    with pytest.raises(DurableJobTransitionError, match="fixed native authority"):
        await durable_job_repository.transition_job(queued["job_id"], "succeeded", owner=RUNNER,
            fencing_token=claimed["lease"]["fencing_token"], expected_revision=claimed["revision"])
    assert (await durable_job_repository.get_job(queued["job_id"]))["status"] == "running"
    async with sessions() as db:
        assert (await db.execute(select(func.count()).select_from(InferenceCostReservation))).scalar() == 0
        assert (await db.get(GuardianOpportunity, row.id)).assessment_json is None
