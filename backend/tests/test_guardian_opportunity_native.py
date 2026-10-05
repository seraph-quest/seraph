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


@pytest.mark.parametrize("missing_binding", [False, True])
async def test_unowned_claim_keeps_latest_candidate_waiting_without_replay(isolated_runtime, monkeypatch, missing_binding):
    import httpx
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import delete
    from src.db.models import WorkflowRunState
    from src.guardian import opportunity_runtime
    from src.guardian.opportunity_contracts import OpportunityError
    from src.guardian.source_watch import SourceWatchService
    from tests.test_work_board_m6_provider_free_journey import SESSION
    sessions, _, watch, _, old, _ = await publish_source(isolated_runtime)
    queued = await admit_assessment(old.id)
    async def claim(db, run):
        current, _ = await _authority(db, old.id, execution=True)
        current.status, current.revision = "assessing", current.revision + 1
        db.add(current)
    claimed = await durable_job_repository.claim_job(queued["job_id"], owner=RUNNER,
        expected_revision=queued["revision"], claim_authority_check=claim)
    assert old.id not in opportunity_runtime._executions
    async def fetch(source):
        return "Stable public line\nA newer relevant public release with material detail\n", {"content_type": "text/plain"}
    newer = await SourceWatchService(fetcher=fetch).run_watch(watch["id"], occurrence_id="unowned-newest",
        expected_plan_revision=1, expected_owner_session_id=SESSION)
    assert newer["status"] == "succeeded", newer
    async with sessions() as db:
        old = await db.get(GuardianOpportunity, old.id)
        latest = (await db.execute(select(GuardianOpportunity).where(GuardianOpportunity.status == "queued"))).scalar_one()
        assert old.status == "silent" and old.reason_code == "coalesced"
    native = await durable_job_repository.get_job(queued["job_id"])
    assert native["status"] == "running" and native["attempt_count"] == claimed["attempt_count"]
    assert await opportunity_runtime.quiesce_opportunity(old, coalesced=True) is False
    if missing_binding:
        # Canonical corruption negative only: a linked missing native row
        # cannot serve as a proof that its former transfer closed.
        async with sessions() as db:
            await db.execute(delete(WorkflowRunState).where(WorkflowRunState.run_identity == old.job_id))
        assert await opportunity_runtime.quiesce_opportunity(old, coalesced=True) is False
    def forbid_http(*args, **kwargs):
        raise AssertionError("unproved old closure must never reach HTTP")
    monkeypatch.setattr(httpx, "AsyncClient", forbid_http)
    reason = "coalesced_binding_missing" if missing_binding else "coalesced_execution_waiting"
    with pytest.raises(OpportunityError, match=reason):
        await admit_assessment(latest.id)
    for _ in range(2):
        assert (await opportunity_runtime.run_opportunity_tick())["started"] == 0
        async with sessions() as db:
            current = await db.get(GuardianOpportunity, latest.id)
            assert current.status == "queued" and current.reason_code == reason and current.job_id is None
            assert current.revision == latest.revision + 1
            assert (await db.execute(select(func.count()).select_from(InferenceCostReservation))).scalar() == 0
    async with sessions() as db:
        current = await db.get(GuardianOpportunity, latest.id)
        current.assessment_deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.add(current)
    assert (await opportunity_runtime.run_opportunity_tick())["started"] == 0
    async with sessions() as db:
        current = await db.get(GuardianOpportunity, latest.id)
        assert current.status == "blocked" and current.reason_code == "assessment_deadline_expired"
        assert current.revision == latest.revision + 2 and current.job_id is None
    if not missing_binding:
        assert (await durable_job_repository.get_job(old.job_id))["status"] == "running"


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


@pytest.mark.parametrize("recovery_owner", ["targeted", "startup"])
async def test_expired_never_contacted_native_lease_recovers_same_job_bounded(isolated_runtime, monkeypatch, recovery_owner):
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
    if recovery_owner == "startup":
        recovered = await durable_job_repository.recover_stale_jobs()
        native = next(job for job in recovered if job["job_id"] == queued["job_id"])
        assert native["status"] == "blocked" and native["failure_reason"] == "stale_lease_requires_reconciliation"
        assert native["lease"]["owner"] is None and native["lease"]["expires_at"] is None
        assert native["lease"]["fencing_token"] > claimed["lease"]["fencing_token"]
        async with sessions() as db:
            assert (await db.get(GuardianOpportunity, row.id)).status == "assessing"
            assert (await db.execute(select(func.count()).select_from(InferenceCostReservation))).scalar() == 0
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


async def _startup_recovered_candidate(isolated_runtime):
    """Actual publication, admission, claim and global restart reconciliation."""
    import json
    from datetime import datetime, timedelta, timezone
    from src.db.models import Goal, WorkflowRunState
    sessions, goal, watch, _, row, _ = await publish_source(isolated_runtime)
    async with sessions() as db:
        current = await db.get(Goal, goal.id)
        budget = json.loads(current.admission_budget_json)
        budget["max_attempts"] = 2
        current.admission_budget_json = json.dumps(budget)
        db.add(current)
    queued = await admit_assessment(row.id)
    async def claim(db, run):
        current, _ = await _authority(db, row.id, execution=True)
        current.status, current.revision = "assessing", current.revision + 1
        db.add(current)
    claimed = await durable_job_repository.claim_job(queued["job_id"], owner=RUNNER,
        expected_revision=queued["revision"], claim_authority_check=claim)
    async with sessions() as db:
        native = (await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == queued["job_id"]))).scalar_one()
        native.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.add(native)
    recovered = next(item for item in await durable_job_repository.recover_stale_jobs()
        if item["job_id"] == queued["job_id"])
    assert recovered["status"] == "blocked"
    assert recovered["failure_reason"] == "stale_lease_requires_reconciliation"
    assert recovered["lease"]["owner"] is None
    assert recovered["lease"]["fencing_token"] > claimed["lease"]["fencing_token"]
    async with sessions() as db:
        row = await db.get(GuardianOpportunity, row.id)
        assert row.status == "assessing"
        assert (await db.execute(select(func.count()).select_from(InferenceCostReservation))).scalar() == 0
    return sessions, goal, watch, row, recovered


def _reservation_negative(row, native, state):
    """Accounting-row corruption negative, never an execution/success receipt."""
    from datetime import datetime, timedelta, timezone
    return InferenceCostReservation(operation_id="negative:"+row.id, deployment_id="negative",
        job_id=row.job_id, owner_id=row.owner_principal_id, goal_id=row.goal_id,
        goal_revision=row.goal_revision, payload_digest="0"*64, policy_digest="0"*64,
        runtime_path="strategist_agent", profile_id="negative", period_id="negative",
        settings_revision=1, ceiling_microusd=1, bound_microusd=1, sequence=1,
        priority=2, deadline_at=datetime.now(timezone.utc)+timedelta(minutes=1),
        state=state, job_fencing_token=native["lease"]["fencing_token"])


@pytest.mark.parametrize("change", ["root", "goal", "policy", "source", "grant", "deadline", "attempts",
    "reserved", "released", "unknown", "reason", "malformed", "shape", "principal", "owner_kind",
    "service_id", "capability_id", "permissions", "budget_grant_id"])
async def test_actual_startup_queue_rechecks_current_authority_and_exact_binding(isolated_runtime, monkeypatch, change):
    import httpx
    import json
    from datetime import datetime, timedelta, timezone
    from src.db.models import Goal, OperatorSession, GuardianSourceWatch, WorkflowRunState
    from src.guardian.opportunity_contracts import OpportunityError
    sessions, goal, watch, row, native = await _startup_recovered_candidate(isolated_runtime)
    expected_attempt_count = native["attempt_count"]
    async with sessions() as db:
        run = (await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == row.job_id))).scalar_one()
        if change == "root":
            root = await db.get(OperatorSession, row.original_root_id)
            root.revoked_at = datetime.now(timezone.utc)
            db.add(root)
        elif change in {"goal", "policy", "grant"}:
            current = await db.get(Goal, goal.id)
            if change == "goal":
                current.revision += 1
            elif change == "policy":
                current.guardian_policy_revision += 1
            else:
                policy = json.loads(current.guardian_policy_json)
                policy["grant_id"] = "different-review"
                current.guardian_policy_json = json.dumps(policy)
            db.add(current)
        elif change == "source":
            current = await db.get(GuardianSourceWatch, watch["id"])
            current.plan_revision += 1
            db.add(current)
        elif change == "deadline":
            run.deadline_at = datetime.now(timezone.utc)-timedelta(seconds=1)
        elif change == "attempts":
            run.attempt_count = run.max_attempts
            expected_attempt_count = run.attempt_count
        elif change in {"reserved", "released", "unknown"}:
            db.add(_reservation_negative(row, native, change))
        elif change == "reason":
            run.failure_reason = "operator_review_required"
        elif change in {"malformed", "shape"}:
            run.declared_authority_json = "{" if change == "malformed" else "[]"
        else:
            authority = json.loads(run.declared_authority_json)
            authority[change] = [] if change == "permissions" else "different-binding"
            run.declared_authority_json = json.dumps(authority)
        db.add(run)
    def forbid_http(*args, **kwargs):
        raise AssertionError("invalid restart must remain before HTTP")
    monkeypatch.setattr(httpx, "AsyncClient", forbid_http)
    with pytest.raises(OpportunityError):
        await durable_job_repository.queue_job(row.job_id, expected_revision=native["revision"],
            expected_fencing_token=native["lease"]["fencing_token"])
    latest = await durable_job_repository.get_job(row.job_id)
    assert latest["status"] == "blocked" and latest["revision"] == native["revision"]
    assert latest["attempt_count"] == expected_attempt_count
    assert latest["lease"]["fencing_token"] == native["lease"]["fencing_token"]
    async with sessions() as db:
        current = await db.get(GuardianOpportunity, row.id)
        assert current.status == "assessing" and current.revision == row.revision
        assert not any(item.contact_started_at for item in (await db.execute(select(InferenceCostReservation))).scalars())


@pytest.mark.parametrize("stale", ["revision", "fence"])
async def test_startup_queue_failed_native_cas_rolls_back_opportunity_transition(isolated_runtime, stale):
    sessions, _, _, row, native = await _startup_recovered_candidate(isolated_runtime)
    with pytest.raises(DurableJobLeaseError):
        await durable_job_repository.queue_job(row.job_id,
            expected_revision=native["revision"]-(stale == "revision"),
            expected_fencing_token=native["lease"]["fencing_token"]-(stale == "fence"))
    latest = await durable_job_repository.get_job(row.job_id)
    assert latest["status"] == "blocked" and latest["revision"] == native["revision"]
    async with sessions() as db:
        current = await db.get(GuardianOpportunity, row.id)
        assert current.status == "assessing" and current.revision == row.revision
        assert (await db.execute(select(func.count()).select_from(InferenceCostReservation))).scalar() == 0


@pytest.mark.parametrize("change", ["exact", "reason", "reserved", "unknown", "malformed"])
async def test_verified_publication_handles_recovered_blocked_history_without_guessing_closure(isolated_runtime, monkeypatch, change):
    import httpx
    from datetime import datetime, timedelta, timezone
    from src.db.models import WorkflowRunState
    from src.guardian import opportunity_runtime
    from src.guardian.source_watch import SourceWatchService
    from tests.test_work_board_m6_provider_free_journey import SESSION
    sessions, _, watch, row, native = await _startup_recovered_candidate(isolated_runtime)
    if change != "exact":
        async with sessions() as db:
            run = (await db.execute(select(WorkflowRunState).where(
                WorkflowRunState.run_identity == row.job_id))).scalar_one()
            if change == "reason":
                run.failure_reason = "operator_review_required"
            elif change == "malformed":
                run.declared_authority_json = "[]"
            else:
                db.add(_reservation_negative(row, native, change))
            db.add(run)
    async def fetch(source):
        return "Stable public line\nA third distinct relevant public release\n", {"content_type": "text/plain"}
    result = await SourceWatchService(fetcher=fetch).run_watch(watch["id"], occurrence_id="recovered-newest",
        expected_plan_revision=1, expected_owner_session_id=SESSION)
    assert result["status"] == "succeeded", result
    async with sessions() as db:
        old = await db.get(GuardianOpportunity, row.id)
        latest = (await db.execute(select(GuardianOpportunity).where(GuardianOpportunity.id != row.id))).scalar_one()
        if change != "exact":
            assert old.status == "assessing" and old.revision == row.revision
            assert latest.status == "blocked" and latest.reason_code == "opportunity_capacity_exhausted"
            assert latest.job_id is None
        else:
            assert old.status == "silent" and old.reason_code == "coalesced"
            assert latest.status == "queued" and latest.job_id is None
    assert (await durable_job_repository.get_job(row.job_id))["status"] == "blocked"
    if change != "exact":
        return
    assert await opportunity_runtime.quiesce_opportunity(old, coalesced=True) is False
    def forbid_http(*args, **kwargs):
        raise AssertionError("unowned recovered execution has no Task closure proof")
    monkeypatch.setattr(httpx, "AsyncClient", forbid_http)
    for _ in range(2):
        assert (await opportunity_runtime.run_opportunity_tick())["started"] == 0
        async with sessions() as db:
            current = await db.get(GuardianOpportunity, latest.id)
            assert current.status == "queued" and current.reason_code == "coalesced_execution_waiting"
            assert current.revision == latest.revision+1
    async with sessions() as db:
        current = await db.get(GuardianOpportunity, latest.id)
        current.assessment_deadline_at = datetime.now(timezone.utc)-timedelta(seconds=1)
        db.add(current)
    assert (await opportunity_runtime.run_opportunity_tick())["started"] == 0
    async with sessions() as db:
        current = await db.get(GuardianOpportunity, latest.id)
        assert current.status == "blocked" and current.reason_code == "assessment_deadline_expired"
        assert current.revision == latest.revision+2
        assert (await db.execute(select(func.count()).select_from(InferenceCostReservation))).scalar() == 0
    residual = await durable_job_repository.get_job(row.job_id)
    assert residual["status"] == "blocked" and residual["attempt_count"] == native["attempt_count"]
    assert residual["lease"]["fencing_token"] == native["lease"]["fencing_token"]


async def _publish_independent_ready_goal(isolated_runtime):
    from tests.test_guardian_opportunity_policy import setup_policy
    from src.api.goals import put_guardian_policy
    from src.guardian.source_watch import SourceWatchService
    from tests.test_work_board_m6_provider_free_journey import SESSION
    sessions, goal, watch, request, body = await setup_policy(isolated_runtime)
    await put_guardian_policy(goal.id, body, request)
    versions = iter(("Older independent release\n", "A relevant independent public release\n"))
    async def fetch(source):
        return next(versions), {"content_type": "text/plain"}
    service = SourceWatchService(fetcher=fetch)
    for occurrence in ("independent-baseline", "independent-material"):
        result = await service.run_watch(watch["id"], occurrence_id=occurrence,
            expected_plan_revision=1, expected_owner_session_id=SESSION)
        assert result["status"] in {"baseline_initialized", "succeeded"}, result
    async with sessions() as db:
        row = (await db.execute(select(GuardianOpportunity).where(GuardianOpportunity.goal_id == goal.id))).scalar_one()
        assert row.status == "queued" and row.job_id is None
    return row


@pytest.mark.parametrize("invalid", ["goal", "source", "snapshot"])
async def test_invalid_recovered_goal_does_not_starve_independent_ready_goal(isolated_runtime, monkeypatch, invalid):
    import asyncio
    import json
    import httpx
    from src.db.models import Goal, GuardianSourceWatch
    from src.guardian import opportunity_runtime
    sessions, goal, watch, old, native = await _startup_recovered_candidate(isolated_runtime)
    ready = await _publish_independent_ready_goal(isolated_runtime)
    if invalid == "snapshot":
        reference = json.loads(old.source_token_json)["artifact_id"]
        assert reference.startswith(opportunity_runtime.PREFIX)
        (isolated_runtime[1]/reference).unlink()
    else:
        async with sessions() as db:
            if invalid == "goal":
                current = await db.get(Goal, goal.id)
                current.revision += 1
            else:
                current = await db.get(GuardianSourceWatch, watch["id"])
                current.plan_revision += 1
            db.add(current)
    def forbid_http(*args, **kwargs):
        raise AssertionError("provider-free scheduler proof must not reach HTTP")
    monkeypatch.setattr(httpx, "AsyncClient", forbid_http)
    tick = await opportunity_runtime.run_opportunity_tick()
    assert tick == {"started": 1, "examined": 2}
    await asyncio.wait_for(opportunity_runtime._executions[ready.id], timeout=20)
    async with sessions() as db:
        stopped = await db.get(GuardianOpportunity, old.id)
        assert stopped.status == "blocked" and stopped.revision == old.revision+1
        assert stopped.reason_code == {"goal":"goal_review_required", "source":"source_stale",
            "snapshot":"source_excerpt_unavailable"}[invalid]
        handled = await db.get(GuardianOpportunity, ready.id)
        assert handled.job_id == f"opportunity:{ready.id}"
        assert (await db.execute(select(func.count()).select_from(InferenceCostReservation))).scalar() == 0
    original = await durable_job_repository.get_job(old.job_id)
    assert original["status"] == "blocked" and original["revision"] == native["revision"]
    assert original["attempt_count"] == native["attempt_count"]
    assert original["deadline_at"] == native["deadline_at"]
    # The independent Goal reaches its actual native claim. This deliberately
    # provider-free fixture lacks model readiness, so no useful judgment or
    # successful native/Inbox outcome is seeded or claimed here.
    assert (await durable_job_repository.get_job(handled.job_id))["attempt_count"] == 1


async def test_recovery_queue_cas_loss_preserves_actual_winner_for_next_tick(isolated_runtime, monkeypatch):
    import asyncio
    import httpx
    from src.guardian import opportunity_runtime
    sessions, _, _, row, native = await _startup_recovered_candidate(isolated_runtime)
    queue = durable_job_repository.queue_job
    lost = False
    async def actual_winner_then_stale_caller(job_id, **kwargs):
        nonlocal lost
        if job_id == row.job_id and not lost:
            lost = True
            assert (await queue(job_id, **kwargs))["status"] == "queued"
            # A second real call carries the old recovered revision/fence.
            # The canonical native owner itself rejects this caller's CAS.
        return await queue(job_id, **kwargs)
    monkeypatch.setattr(durable_job_repository, "queue_job", actual_winner_then_stale_caller)
    def forbid_http(*args, **kwargs):
        raise AssertionError("provider-free CAS proof must not reach HTTP")
    monkeypatch.setattr(httpx, "AsyncClient", forbid_http)
    assert (await opportunity_runtime.run_opportunity_tick())["started"] == 0
    async with sessions() as db:
        current = await db.get(GuardianOpportunity, row.id)
        assert current.status == "queued" and current.reason_code is None
    winner = await durable_job_repository.get_job(row.job_id)
    assert winner["status"] == "queued" and winner["attempt_count"] == native["attempt_count"]
    assert winner["deadline_at"] == native["deadline_at"]
    assert (await opportunity_runtime.run_opportunity_tick())["started"] == 1
    await asyncio.wait_for(opportunity_runtime._executions[row.id], timeout=20)
    assert (await durable_job_repository.get_job(row.job_id))["attempt_count"] == 2


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
