"""Finite owned policy saves execute real SQL and never admit work."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update, func

from tests.test_work_board_m6_provider_free_journey import isolated_runtime, _goal, OWNER, SESSION, GRANT
from src.api.goals import put_guardian_policy
from src.auth.service import authenticate_session
from src.db.models import Goal, GuardianSourceWatch, GuardianOpportunity, WorkflowRunState, OperatorSession, GuardianDecisionPacket
from src.guardian.opportunity_contracts import GuardianPolicySave
from src.guardian.opportunities import policy_projection, assert_opportunity_current
from src.guardian.source_watch import SourceWatchService


async def setup_policy(isolated_runtime):
    sessions, _ = isolated_runtime
    goal = _goal(str(uuid4()), "Explicit reviewed public goal")
    async with sessions() as db:
        db.add(goal)
    watch = await SourceWatchService().create_watch(owner_principal_id=OWNER, owner_session_id=SESSION,
        goal_id=goal.id, expected_goal_revision=1,
        sources=[dict(source_key="public", kind="public_https_text", target="https://example.com/public",
            label="public", priority=1)], criteria={}, schedule={"cron": "0 * * * *", "timezone": "UTC"},
        write_mode="standing_reviewed", reviewed_grant_id=GRANT)
    current = datetime.now(timezone.utc)
    operator = await authenticate_session(SESSION, touch=False)
    request = SimpleNamespace(state=SimpleNamespace(operator=operator))
    body = GuardianPolicySave(expected_goal_revision=1, expected_policy_revision=0, idempotency_key=uuid4(),
        policy=dict(schema_version="seraph.guardian.policy.v1", assessment_enabled=True, confirmed_at=current,
            review_due_at=current + timedelta(days=7), grant_id=GRANT, original_root_id=SESSION,
            goal_revision=1, source_watch_ids=[watch["id"]], max_assessments_per_utc_day=2))
    return sessions, goal, watch, request, body


async def test_policy_server_caps_expiry_idempotent_save_and_no_job(isolated_runtime):
    sessions, goal, _, request, body = await setup_policy(isolated_runtime)
    result = await put_guardian_policy(goal.id, body, request)
    assert result["guardian_policy_revision"] == 1
    assert datetime.fromisoformat(result["guardian_policy"]["review_due_at"]) <= datetime.now(timezone.utc) + timedelta(hours=1)
    assert await put_guardian_policy(goal.id, body, request) == result
    async with sessions() as db:
        persisted = await db.get(Goal, goal.id)
        assert persisted.revision == 1
        assert persisted.guardian_policy_revision == 1
        assert (await db.execute(select(func.count()).select_from(GuardianOpportunity))).scalar() == 0
        assert (await db.execute(select(func.count()).select_from(WorkflowRunState))).scalar() == 0
        persisted.revision += 1
        db.add(persisted)
    async with sessions() as db:
        assert policy_projection(await db.get(Goal, goal.id))["guardian_assessment_state"] == "goal_review_required"


@pytest.mark.parametrize("change", ["root", "goal", "watch", "private", "ack", "expired"])
async def test_policy_current_authority_and_separate_opt_in_negatives(isolated_runtime, change):
    sessions, goal, watch, request, body = await setup_policy(isolated_runtime)
    if change == "root":
        async with sessions() as db:
            await db.execute(update(OperatorSession).where(OperatorSession.id == SESSION).values(revoked_at=datetime.now(timezone.utc)))
    elif change == "goal":
        body.policy.goal_revision = 2
    elif change in {"watch", "private"}:
        async with sessions() as db:
            await db.execute(update(GuardianSourceWatch).where(GuardianSourceWatch.id == watch["id"]).values(
                **({"goal_revision": 2} if change == "watch" else {"sources_json":
                    '[{"source_key":"private","kind":"workspace_text","target":"notes/private.txt","label":"private","priority":1}]'})))
    elif change == "ack":
        body.policy.max_notification_per_utc_day = 1
    else:
        async with sessions() as db:
            persisted = await db.get(Goal, goal.id)
            import json
            budget = json.loads(persisted.admission_budget_json)
            budget["period_expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            persisted.admission_budget_json = json.dumps(budget)
            db.add(persisted)
    with pytest.raises(HTTPException) as rejected:
        await put_guardian_policy(goal.id, body, request)
    assert rejected.value.status_code in {403, 409, 422}
    async with sessions() as db:
        assert (await db.get(Goal, goal.id)).guardian_policy_json is None
        assert (await db.execute(select(func.count()).select_from(WorkflowRunState))).scalar() == 0


async def publish_source(isolated_runtime):
    sessions, goal, watch, request, body = await setup_policy(isolated_runtime)
    await put_guardian_policy(goal.id, body, request)
    versions = iter(("Stable public line\nPrevious release\n", "Stable public line\nA relevant new public release\n"))
    async def fetch(source):
        assert source.kind == "public_https_text"
        return next(versions), {"content_type": "text/plain"}
    service = SourceWatchService(fetcher=fetch)
    baseline = await service.run_watch(watch["id"], occurrence_id="opportunity-baseline",
        expected_plan_revision=1, expected_owner_session_id=SESSION)
    assert baseline["status"] == "baseline_initialized", baseline
    result = await service.run_watch(watch["id"], occurrence_id="opportunity-material",
        expected_plan_revision=1, expected_owner_session_id=SESSION)
    assert result["status"] == "succeeded", result
    async with sessions() as db:
        rows = list((await db.execute(select(GuardianOpportunity))).scalars().all())
        assert len(rows) == 1
        opportunity = rows[0]
        packet = await db.get(GuardianDecisionPacket, opportunity.source_packet_id)
    return sessions, goal, watch, request, opportunity, packet


async def test_new_material_publication_stages_immutable_snapshot_and_unique_row(isolated_runtime):
    from src.guardian.opportunity_runtime import read_snapshot
    from src.guardian.opportunity_contracts import VerifiedSourcePacket
    from src.guardian.opportunities import publish_verified_packet
    sessions, goal, watch, request, opportunity, packet = await publish_source(isolated_runtime)
    assert opportunity.status == "queued"
    assert opportunity.job_id is None  # No model is run in the publication path.
    offered = read_snapshot(packet.opportunity_snapshot_artifact_id, packet.opportunity_snapshot_sha256)
    assert str(offered.packet_id) == packet.id
    assert offered.sources[0].source_key == "public"
    assert "relevant new public release" in offered.sources[0].excerpt
    assert opportunity.source_digest == packet.opportunity_snapshot_sha256
    assert await publish_verified_packet(VerifiedSourcePacket(packet_id=packet.id, watch_revision=1, goal_revision=1)) == opportunity.id
    async with sessions() as db:
        await assert_opportunity_current(db, opportunity, evidence=offered)
        assert (await db.execute(select(func.count()).select_from(GuardianOpportunity))).scalar() == 1


@pytest.mark.parametrize("change", ["source_permission", "source_generation", "goal", "policy", "root"])
async def test_queued_publication_current_authority_fails_closed(isolated_runtime, change):
    import json
    from src.guardian.opportunity_contracts import OpportunityError
    sessions, goal, watch, request, opportunity, packet = await publish_source(isolated_runtime)
    async with sessions() as db:
        if change == "source_permission":
            current = await db.get(GuardianSourceWatch, watch["id"])
            permission = json.loads(current.read_authority_json)
            permission["grant_id"] = "different-reviewed-grant"
            current.read_authority_json = json.dumps(permission)
            db.add(current)
        elif change == "source_generation":
            await db.execute(update(GuardianSourceWatch).where(GuardianSourceWatch.id == watch["id"]).values(plan_revision=2))
        elif change == "goal":
            await db.execute(update(Goal).where(Goal.id == goal.id).values(revision=2))
        elif change == "policy":
            await db.execute(update(Goal).where(Goal.id == goal.id).values(guardian_policy_revision=2))
        else:
            await db.execute(update(OperatorSession).where(OperatorSession.id == SESSION).values(revoked_at=datetime.now(timezone.utc)))
    async with sessions() as db:
        with pytest.raises(OpportunityError):
            await assert_opportunity_current(db, opportunity)
