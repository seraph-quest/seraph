"""Actual governed execution at a controlled persisted-readback crash seam."""
import asyncio
import json
import linecache
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, func

from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth
from tests import test_guardian_opportunity_vertical as vertical
from src.db.models import (Goal, GuardianOpportunity, GuardianIntervention, GuardianSourceWatch,
    OperatorSession, WorkflowRunState, InferenceCostReservation)
from src.guardian import opportunity_runtime as runtime
from src.guardian.opportunities import now
from src.workflows.job_runtime import durable_job_repository


@asynccontextmanager
async def persisted_result(accounting_db, real_auth, monkeypatch, *, silent=False, expect_invalid=False):
    """Pause the actual worker after artifact/effect/readback, before terminal CAS.

    This is controlled restart selection, not process death. The separate
    abrupt-process receipts cover that boundary. No execution/result is seeded.
    """
    root, _, factory = accounting_db
    paused, release = asyncio.Event(), asyncio.Event()
    original = durable_job_repository.transition_job
    intercepted = False
    contacts = []
    boundary = vertical.OpportunityHttpBoundary
    class CountingBoundary(boundary):
        async def handle_async_request(self, request):
            contacts.append(str(request.url))
            return await super().handle_async_request(request)
    monkeypatch.setattr(vertical, "OpportunityHttpBoundary", CountingBoundary)
    async def terminal(job_id, status, **kwargs):
        nonlocal intercepted
        if job_id.startswith("opportunity:") and status == "succeeded" and not intercepted:
            intercepted = True
            paused.set()
            await release.wait()
        return await original(job_id, status, **kwargs)
    monkeypatch.setattr(durable_job_repository, "transition_job", terminal)
    task = asyncio.create_task(vertical.test_actual_http_goal_watch_native_cited_inbox(
        accounting_db, real_auth, monkeypatch, scenario="silent" if silent else "completed"))
    try:
        await asyncio.wait_for(paused.wait(), 30)
        async with factory.accounting_sessions() as db:
            row = (await db.execute(select(GuardianOpportunity))).scalar_one()
            cost = (await db.execute(select(InferenceCostReservation).where(
                InferenceCostReservation.job_id == row.job_id))).scalar_one()
            assert cost.state == "settled" and cost.contact_started_at is not None
            assert row.status == "assessing" and row.assessment_json is None
        native = await durable_job_repository.get_job(row.job_id)
        assert native["status"] == "running" and native["attempt_count"] == 1
        assert len(native["artifacts"]) == 1
        assert any(item.get("receipt_kind") == "readback" and item["effect_type"] == "workspace_write"
                   for item in native["effects"])
        runtime._executions.pop(row.id, None)
        yield root, factory.accounting_sessions, row, native, cost.model_dump(mode="json"), contacts
    finally:
        release.set()
        try:
            await asyncio.wait_for(task, 20)
        except AssertionError as exc:
            # Only the existing journey's final-status assertion can differ
            # for deliberate negatives. Propagate every positive regression.
            traceback = exc.__traceback__
            while traceback.tb_next is not None:
                traceback = traceback.tb_next
            if (not expect_invalid or traceback.tb_frame.f_code.co_name != "test_actual_http_goal_watch_native_cited_inbox"
                    or linecache.getline(traceback.tb_frame.f_code.co_filename, traceback.tb_lineno).strip()
                    != "assert final.status == expected, final.reason_code"):
                raise
        finally:
            runtime._executions.clear()


@pytest.mark.parametrize("silent", [False, True])
async def test_actual_persisted_result_adopts_once_without_execution_renewal(accounting_db, real_auth, monkeypatch, silent):
    async with persisted_result(accounting_db, real_auth, monkeypatch, silent=silent) as (_, sessions, row, before, cost_before, contacts):
        contact_count = len(contacts)
        first, second = await asyncio.gather(runtime.run_opportunity_tick(), runtime.run_opportunity_tick())
        assert first["started"] == second["started"] == 0
        async with sessions() as db:
            current = await db.get(GuardianOpportunity, row.id)
            cost = (await db.execute(select(InferenceCostReservation).where(
                InferenceCostReservation.job_id == row.job_id))).scalar_one()
            assert current.status == ("silent" if silent else "proposed")
            assert current.assessment_json is not None and current.result_artifact_id is not None
            assert (await db.execute(select(func.count()).select_from(GuardianIntervention))).scalar() == (0 if silent else 1)
            assert cost.model_dump(mode="json") == cost_before
        after = await durable_job_repository.get_job(row.job_id)
        for field in ("job_id", "idempotency", "deadline_at", "attempt_count", "artifacts", "effects"):
            assert after[field] == before[field]
        assert after["lease"]["fencing_token"] == before["lease"]["fencing_token"]
        assert after["status"] == "succeeded" and after["revision"] == before["revision"] + 1
        assert "no learning" in after["result"]["summary"]
        assert len(contacts) == contact_count
        assert (await runtime.run_opportunity_tick())["started"] == 0
    # Releasing the former worker loses to the committed native terminal row.
    # It must not rewrite the winner or create a second intervention.
    assert await durable_job_repository.get_job(row.job_id) == after


@pytest.mark.parametrize("change", ["goal", "policy", "root", "source", "cancel", "deadline", "lease",
    "fence", "missing_result", "tampered_result", "missing_snapshot", "unknown_cost", "native_binding", "duplicate_artifact",
    "citation", "privacy"])
async def test_actual_persisted_result_invalidated_before_restart_never_adopts(accounting_db, real_auth, monkeypatch, change):
    async with persisted_result(accounting_db, real_auth, monkeypatch, expect_invalid=True) as (root, sessions, row, native, cost_before, contacts):
        async with sessions() as db:
            current = await db.get(GuardianOpportunity, row.id)
            run = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == row.job_id))).scalar_one()
            if change == "goal":
                (await db.get(Goal, row.goal_id)).revision += 1
            elif change == "policy":
                (await db.get(Goal, row.goal_id)).guardian_policy_revision += 1
            elif change == "root":
                (await db.get(OperatorSession, row.original_root_id)).revoked_at = now()
            elif change == "source":
                (await db.get(GuardianSourceWatch, row.watch_id)).plan_revision += 1
            elif change == "cancel":
                current.reason_code = "cancel_requested"
            elif change == "deadline":
                current.assessment_deadline_at = run.deadline_at = now() - timedelta(seconds=1)
            elif change == "lease":
                run.lease_expires_at = now() - timedelta(seconds=1)
            elif change == "fence":
                run.fencing_token += 1
            elif change == "native_binding":
                authority = json.loads(run.declared_authority_json)
                authority["source_digest"] = "0" * 64
                run.declared_authority_json = json.dumps(authority)
            elif change == "duplicate_artifact":
                artifacts = json.loads(run.artifact_receipts_json)
                run.artifact_receipts_json = json.dumps(artifacts + artifacts)
            elif change == "unknown_cost":
                reservation = (await db.execute(select(InferenceCostReservation).where(
                    InferenceCostReservation.job_id == row.job_id))).scalar_one()
                reservation.state, reservation.actual_cost_microusd = "unknown", None
            elif change in {"citation", "privacy"}:
                from src.artifacts.registry import artifact_id_for
                from src.guardian.opportunity_contracts import digest, json_bytes
                # Corrupt an actual produced result and its existing receipts
                # consistently, to isolate citation/privacy revalidation.
                artifacts, effects = json.loads(run.artifact_receipts_json), json.loads(run.effect_receipts_json)
                artifact = artifacts[0]
                payload = json.loads((root / artifact["file_path"]).read_bytes())
                if change == "citation":
                    payload["citations"][0]["span_sha256"] = "0" * 64
                else:
                    payload["summary"] = "Contact private.person@example.com"
                encoded = json_bytes(payload)
                sha = digest(encoded)
                reference = f"{runtime.PREFIX}result-{row.id}-{sha}.json"
                (root / reference).write_bytes(encoded)
                artifact.update(content_sha256=sha, size_bytes=len(encoded), file_path=reference)
                artifact["artifact_id"] = artifact_id_for(file_path=reference,
                    artifact_type="guardian_opportunity_assessment", producer=runtime.JOB_KIND,
                    run_id=row.job_id, content_sha256=sha)
                for effect in effects:
                    if effect["effect_type"] == "workspace_write":
                        effect.update(target_path=reference, target_digest=sha, content_sha256=sha,
                            readback_id=f"opportunity_readback:{row.id}:{sha}")
                run.artifact_receipts_json, run.effect_receipts_json = json.dumps(artifacts), json.dumps(effects)
        if change == "missing_result":
            (root / native["artifacts"][0]["file_path"]).unlink()
        elif change == "tampered_result":
            (root / native["artifacts"][0]["file_path"]).write_text("{}")
        elif change == "missing_snapshot":
            (root / json.loads(row.source_token_json)["artifact_id"]).unlink()
        count = len(contacts)
        assert (await runtime.run_opportunity_tick())["started"] == 0
        async with sessions() as db:
            current = await db.get(GuardianOpportunity, row.id)
            assert current.status == "unknown"
            assert current.result_artifact_id is None and current.assessment_json is None and current.intervention_id is None
            assert (await db.execute(select(func.count()).select_from(GuardianIntervention))).scalar() == 0
            reservation = (await db.execute(select(InferenceCostReservation).where(
                InferenceCostReservation.job_id == row.job_id))).scalar_one()
            assert reservation.state == ("unknown" if change == "unknown_cost" else cost_before["state"])
            assert reservation.bound_microusd == cost_before["bound_microusd"]
        assert len(contacts) == count
        assert (await durable_job_repository.get_job(row.job_id))["attempt_count"] == native["attempt_count"]


async def independent_ready_goal(sessions, row):
    """Publish another Goal through production repository, policy and watch APIs."""
    from src.api.goals import put_guardian_policy
    from src.auth.service import authenticate_session
    from src.goals.repository import goal_repository
    from src.guardian.opportunity_contracts import GuardianPolicySave
    from src.guardian.source_watch import SourceWatchService
    async with sessions() as db:
        original = await db.get(Goal, row.goal_id)
        budget = json.loads(original.admission_budget_json)
    goal = await goal_repository.create(title="Independent cited public release", proactive_enabled=True,
        owner_principal_id=row.owner_principal_id, owner_session_id=row.original_root_id, admission_budget=budget)
    watch = await SourceWatchService().create_watch(owner_principal_id=row.owner_principal_id,
        owner_session_id=row.original_root_id, goal_id=goal.id, expected_goal_revision=goal.revision,
        sources=[dict(source_key="public", kind="public_https_text", target="https://example.com/public",
            label="Independent public release", priority=1)], criteria={}, schedule=dict(cron="0 * * * *", timezone="UTC"),
        write_mode="standing_reviewed", reviewed_grant_id=budget["grant_id"])
    current = now()
    operator = await authenticate_session(row.original_root_id, touch=False)
    await put_guardian_policy(goal.id, GuardianPolicySave(expected_goal_revision=goal.revision,
        expected_policy_revision=0, idempotency_key=uuid4(), policy=dict(
            schema_version="seraph.guardian.policy.v1", assessment_enabled=True, confirmed_at=current,
            review_due_at=current + timedelta(hours=1), grant_id=budget["grant_id"], original_root_id=row.original_root_id,
            goal_revision=goal.revision, source_watch_ids=[watch["id"]], max_assessments_per_utc_day=2)),
        SimpleNamespace(state=SimpleNamespace(operator=operator)))
    versions = iter(("Previous independent public release\n", "A relevant new independent public release\n"))
    async def fetch(source):
        return next(versions), {"content_type": "text/plain"}
    service = SourceWatchService(fetcher=fetch)
    for occurrence in ("independent-baseline", "independent-material"):
        result = await service.run_watch(watch["id"], occurrence_id=occurrence,
            expected_plan_revision=1, expected_owner_session_id=row.original_root_id)
        assert result["status"] in {"baseline_initialized", "succeeded"}, result
    async with sessions() as db:
        ready = (await db.execute(select(GuardianOpportunity).where(GuardianOpportunity.goal_id == goal.id))).scalar_one()
        assert ready.status == "queued" and ready.job_id is None
        await runtime._authority(db, ready.id, execution=True)
    return ready


@pytest.mark.parametrize("ledger", ["artifact_receipts_json", "effect_receipts_json"])
async def test_actual_malformed_result_does_not_starve_independent_goal(accounting_db, real_auth, monkeypatch, ledger):
    async with persisted_result(accounting_db, real_auth, monkeypatch, expect_invalid=True) as (_, sessions, row, native, cost_before, contacts):
        ready = await independent_ready_goal(sessions, row)
        async with sessions() as db:
            run = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == row.job_id))).scalar_one()
            setattr(run, ledger, "{")  # Named canonical corruption after actual readback.
        tick = await runtime.run_opportunity_tick()
        assert tick == {"started": 1, "examined": 2}
        await asyncio.wait_for(runtime._executions[ready.id], 30)
        async with sessions() as db:
            stopped = await db.get(GuardianOpportunity, row.id)
            assert stopped.status == "unknown" and stopped.assessment_json is None and stopped.result_artifact_id is None
            handled = await db.get(GuardianOpportunity, ready.id)
            assert handled.status == "proposed" and handled.result_artifact_id is not None
            cost = (await db.execute(select(InferenceCostReservation).where(
                InferenceCostReservation.job_id == row.job_id))).scalar_one()
            assert cost.model_dump(mode="json") == cost_before
        assert (await durable_job_repository.get_job(handled.job_id))["status"] == "succeeded"
        assert (await durable_job_repository.get_job(row.job_id))["attempt_count"] == native["attempt_count"]
