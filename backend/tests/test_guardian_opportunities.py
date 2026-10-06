"""Owned public-opportunity API/history negatives through canonical stores."""
from uuid import uuid4

import pytest
from sqlalchemy import select, func

from tests.test_work_board_m6_provider_free_journey import isolated_runtime, OWNER, SESSION
from tests.test_guardian_opportunity_policy import publish_source
from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth
from src.db.models import GuardianOpportunity, GuardianDecisionPacket
from src.guardian.opportunity_contracts import OpportunityError, OpportunityCancel, VerifiedSourcePacket
from src.guardian.opportunities import cancel_opportunity, publish_verified_packet, save_policy
from src.auth.service import authenticate_session


async def test_silent_history_feedback_is_409_and_foreign_inbox_is_hidden(isolated_runtime, monkeypatch):
    from src.guardian import feedback, inbox
    sessions, _, _, _, row, _ = await publish_source(isolated_runtime)
    monkeypatch.setattr(feedback, "get_session", sessions)
    with pytest.raises(OpportunityError) as rejected:
        await feedback.guardian_feedback_repository.record_feedback("opportunity:"+row.id,
            feedback_type="helpful", owner_principal_id=OWNER, original_root_id=SESSION)
    assert rejected.value.code == "opportunity_not_proposed" and rejected.value.status_code == 409
    with pytest.raises(inbox.InboxError) as foreign:
        await inbox.get_owned_item(owner_principal_id="operator:root:foreign", owner_session_id=SESSION, item_id=row.id)
    assert foreign.value.status_code == 404
    with pytest.raises(inbox.InboxError):
        await inbox.get_owned_item(owner_principal_id=OWNER, owner_session_id="foreign-root", item_id=row.id)


async def test_owned_queued_cancel_cas_and_idempotent_actual_readback(isolated_runtime):
    sessions, _, _, _, row, _ = await publish_source(isolated_runtime)
    operator = await authenticate_session(SESSION, touch=False)
    stale = OpportunityCancel(expected_opportunity_revision=row.revision+1, idempotency_key=uuid4())
    with pytest.raises(OpportunityError, match="opportunity_revision_stale"):
        await cancel_opportunity(operator=operator, opportunity_id=row.id, request=stale)
    request = OpportunityCancel(expected_opportunity_revision=row.revision, idempotency_key=uuid4())
    result = await cancel_opportunity(operator=operator, opportunity_id=row.id, request=request)
    assert result["status"] == "cancelled" and result["quiescent"] is True
    assert await cancel_opportunity(operator=operator, opportunity_id=row.id, request=request) == result
    async with sessions() as db:
        final = await db.get(GuardianOpportunity, row.id)
        assert final.status == "cancelled" and final.job_id is None


async def test_policy_review_does_not_backfill_old_packet_or_reconstruct_snapshot(isolated_runtime, monkeypatch):
    from src.db.models import Goal
    from src.guardian.opportunity_contracts import GuardianPolicySave
    from src.guardian.opportunities import policy_for
    from src.guardian import opportunity_runtime
    sessions, goal, _, _, row, packet = await publish_source(isolated_runtime)
    operator = await authenticate_session(SESSION, touch=False)
    async with sessions() as db:
        current_goal = await db.get(Goal, goal.id)
        policy = policy_for(current_goal)
    await save_policy(operator=operator, goal_id=goal.id, request=GuardianPolicySave(
        expected_goal_revision=goal.revision, expected_policy_revision=1, idempotency_key=uuid4(), policy=policy))
    async with sessions() as db:
        old_packet = await db.get(GuardianDecisionPacket, packet.id)
        old_packet.opportunity_snapshot_artifact_id = None
        old_packet.opportunity_snapshot_sha256 = None
        db.add(old_packet)
    def forbid_reconstruction(*args, **kwargs):
        raise AssertionError("old/recovered packet must not read or reconstruct evidence")
    monkeypatch.setattr(opportunity_runtime, "read_snapshot", forbid_reconstruction)
    assert await publish_verified_packet(VerifiedSourcePacket(packet_id=packet.id,
        watch_revision=packet.plan_revision, goal_revision=packet.goal_revision)) is None
    async with sessions() as db:
        assert (await db.execute(select(func.count()).select_from(GuardianOpportunity))).scalar() == 1
        assert (await db.get(GuardianDecisionPacket, packet.id)).status == "succeeded"


async def test_authenticated_blocked_history_preserves_publication_reason(isolated_runtime, real_auth):
    import httpx
    from fastapi import FastAPI
    from src.api import goals, guardian_inbox
    from src.auth.middleware import OperatorAuthMiddleware
    from config.settings import settings
    from tests.test_guardian_opportunity_policy import setup_policy
    from src.api.goals import put_guardian_policy
    from src.guardian.source_watch import SourceWatchService

    sessions, goal, watch, request, body = await setup_policy(isolated_runtime,
        source_key="reviewer.person@example.com")
    await put_guardian_policy(goal.id, body, request)
    versions = iter(("Prior public release\n", "A different relevant new public release\n"))
    async def fetch(source):
        return next(versions), {"content_type": "text/plain"}
    service = SourceWatchService(fetcher=fetch)
    for occurrence, expected in (("history-baseline", "baseline_initialized"), ("history-material", "succeeded")):
        result = await service.run_watch(watch["id"], occurrence_id=occurrence,
            expected_plan_revision=1, expected_owner_session_id=SESSION)
        assert result["status"] == expected, result
    async with sessions() as db:
        row = (await db.execute(select(GuardianOpportunity))).scalar_one()
        assert row.status == "blocked" and row.reason_code == "source_excerpt_unavailable"
        packet = await db.get(GuardianDecisionPacket, row.source_packet_id)
        assert packet.opportunity_snapshot_artifact_id is None and row.job_id is None
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(goals.router, prefix="/api")
    app.include_router(guardian_inbox.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        denied = await client.get("/api/guardian/opportunities")
        assert denied.status_code == 401
        client.cookies.set(settings.operator_auth_cookie_name, "m6-provider-free-root")
        listed = await client.get("/api/guardian/opportunities", params={"goal_id": goal.id})
        assert listed.status_code == 200, listed.text
        detail = await client.get(f"/api/guardian/inbox/{row.id}")
        assert detail.status_code == 200, detail.text
        for item in (listed.json()["items"][0], detail.json()):
            assert item["reason_code"] == "source_excerpt_unavailable"
            assert item["policy_reason"] == "source_stale"
            assert item["allowed_actions"] == [] and item["evidence_status"] == "unavailable"
            assert item["recovery_action"] == "review_goal_and_watch"
        assert detail.json()["evidence_previews"] == []
    async with sessions() as db:
        assert (await db.get(GuardianOpportunity, row.id)).reason_code == "source_excerpt_unavailable"


@pytest.mark.parametrize("change,expected", [("goal", "goal_review_required"), ("source", "source_stale")])
async def test_proposed_null_history_retains_live_recovery_reason(accounting_db, real_auth, monkeypatch, change, expected):
    from tests.test_guardian_opportunity_vertical import test_actual_http_goal_watch_native_cited_inbox
    from src.db.models import Goal, GuardianSourceWatch
    from src.guardian import inbox
    from src.guardian.opportunities import list_history

    await test_actual_http_goal_watch_native_cited_inbox(accounting_db, real_auth, monkeypatch, scenario="completed")
    sessions = accounting_db[2].accounting_sessions
    async with sessions() as db:
        row = (await db.execute(select(GuardianOpportunity))).scalar_one()
        assert row.status == "proposed" and row.reason_code is None
        if change == "goal":
            current = await db.get(Goal, row.goal_id)
            current.revision += 1
        else:
            current = await db.get(GuardianSourceWatch, row.watch_id)
            current.plan_revision += 1
        db.add(current)
    history = await list_history(owner=row.owner_principal_id, root_id=row.original_root_id)
    detail = await inbox.get_owned_item(owner_principal_id=row.owner_principal_id,
        owner_session_id=row.original_root_id, item_id=row.id)
    for item in (history["items"][0], detail):
        assert item["reason_code"] == item["policy_reason"] == expected
        assert item["allowed_actions"] == [] and item["evidence_status"] == "unavailable"
        assert item["recovery_action"] == "review_goal_and_watch"
    assert detail["evidence_previews"] == []
    async with sessions() as db:
        assert (await db.get(GuardianOpportunity, row.id)).reason_code is None


async def test_silent_history_preserves_judgment_reason_on_detail_readback_failure(accounting_db, real_auth, monkeypatch):
    from tests.test_guardian_opportunity_vertical import test_actual_http_goal_watch_native_cited_inbox
    from src.guardian import inbox, opportunities, opportunity_runtime

    await test_actual_http_goal_watch_native_cited_inbox(accounting_db, real_auth, monkeypatch, scenario="silent")
    sessions = accounting_db[2].accounting_sessions
    async with sessions() as db:
        row = (await db.execute(select(GuardianOpportunity))).scalar_one()
        assert row.status == "silent" and row.reason_code == "assessment_abstained"
    history = await opportunities.list_history(owner=row.owner_principal_id, root_id=row.original_root_id)
    current_detail = await inbox.get_owned_item(owner_principal_id=row.owner_principal_id,
        owner_session_id=row.original_root_id, item_id=row.id)
    for item in (history["items"][0], current_detail):
        assert item["reason_code"] == "assessment_abstained" and item["policy_reason"] is None
    authority_checked = []
    original_current = opportunities.assert_opportunity_current
    async def check_current(*args, **kwargs):
        result = await original_current(*args, **kwargs)
        authority_checked.append(True)
        return result
    def unreadable_snapshot(*args, **kwargs):
        assert authority_checked
        raise OpportunityError("source_excerpt_unavailable")
    monkeypatch.setattr(opportunities, "assert_opportunity_current", check_current)
    monkeypatch.setattr(opportunity_runtime, "read_snapshot", unreadable_snapshot)
    detail = await inbox.get_owned_item(owner_principal_id=row.owner_principal_id,
        owner_session_id=row.original_root_id, item_id=row.id)
    assert detail["reason_code"] == "assessment_abstained"
    assert detail["policy_reason"] == "source_excerpt_unavailable"
    assert detail["evidence_status"] == "unavailable" and detail["evidence_previews"] == []
    assert detail["allowed_actions"] == [] and detail["recovery_action"] == "review_goal_and_watch"
    async with sessions() as db:
        assert (await db.get(GuardianOpportunity, row.id)).reason_code == "assessment_abstained"
