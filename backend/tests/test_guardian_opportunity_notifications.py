"""Fixed opportunity admission uses existing outbox intents, no provider."""
from datetime import timedelta
import json

import pytest
from sqlalchemy import select, func

from tests.test_work_board_m6_provider_free_journey import isolated_runtime, OWNER, SESSION
from tests.test_guardian_opportunity_policy import publish_source
from src.db.models import Goal, GuardianOpportunity, GuardianIntervention, NativeNotificationOutbox
from src.guardian.opportunities import now
from src.guardian.opportunity_contracts import OpportunityError
from src.observer.native_notification_queue import NativeNotificationQueue


async def test_opportunity_outbox_default_opt_in_idempotency_unknown_cap(isolated_runtime):
    sessions, goal, _, _, row, _ = await publish_source(isolated_runtime)
    # This focused admission test supplies a proposed lineage; actual model
    # creation of that lineage is separately required by the vertical test.
    async with sessions() as db:
        current = await db.get(GuardianOpportunity, row.id)
        current.status = "proposed"
        current.intervention_id = "opportunity:" + row.id
        db.add(current)
        db.add(GuardianIntervention(id=current.intervention_id, intervention_type="opportunity",
            owner_principal_id=OWNER, original_root_id=SESSION, goal_id=goal.id,
            goal_revision=1, opportunity_id=row.id, delivery_status="not_requested"))
    queue = NativeNotificationQueue()
    fields = dict(intervention_id="opportunity:" + row.id, title="Guardian opportunity",
        body="Review the cited judgment in Inbox.", intervention_type="opportunity", urgency=2,
        owner_principal_id=OWNER, operator_session_id=SESSION, goal_id=goal.id, goal_revision=1,
        budget_period_key=now().date().isoformat(), budget_limit=1, idempotency_key="bounded-opportunity")
    with pytest.raises(OpportunityError, match="opportunity_notifications_disabled"):
        await queue.enqueue(**fields)
    async with sessions() as db:
        assert (await db.execute(select(func.count()).select_from(NativeNotificationOutbox))).scalar() == 0
        current = await db.get(Goal, goal.id)
        policy = json.loads(current.guardian_policy_json)
        policy["max_notification_per_utc_day"] = 1
        current.guardian_policy_json = json.dumps(policy)
        db.add(current)
    notification = await queue.enqueue(**fields)
    assert (await queue.enqueue(**fields)).id == notification.id
    async with sessions() as db:
        intent = await db.get(NativeNotificationOutbox, notification.id)
        intent.status = "unknown"
        db.add(intent)
    with pytest.raises(ValueError, match="goal_budget_notification_limit|opportunity_notification_limit"):
        await queue.enqueue(**dict(fields, idempotency_key="no-unknown-retry"))
    async with sessions() as db:
        assert (await db.execute(select(func.count()).select_from(NativeNotificationOutbox))).scalar() == 1
        from src.guardian.opportunities import project_item
        current = await db.get(GuardianOpportunity, row.id)
        projection = await project_item(db, current)
        assert projection["delivery_status"] == "unknown"
