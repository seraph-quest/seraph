"""Owned public-opportunity API/history negatives through canonical stores."""
from uuid import uuid4

import pytest
from sqlalchemy import select, func

from tests.test_work_board_m6_provider_free_journey import isolated_runtime, OWNER, SESSION
from tests.test_guardian_opportunity_policy import publish_source
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
