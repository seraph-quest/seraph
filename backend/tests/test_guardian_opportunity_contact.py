"""Actual canonical contact writer rejects stale source before its marker."""
import json
import time
from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest
from sqlalchemy import select

from tests.test_inference_accounting import accounting_db, setup_configuration
from tests.test_guardian_opportunity_policy import publish_source
from tests.test_work_board_m6_provider_free_journey import OWNER, SESSION
from src.db.models import OperatorSession, GuardianOpportunity, GuardianSourceWatch, Goal, InferenceCostReservation
from src.guardian.opportunity_runtime import admit_assessment, _authority, RUNNER
from src.workflows.job_runtime import durable_job_repository
from src.workflows.inference_accounting import InferenceAccountingError


@pytest.mark.parametrize("change", ["source_grant", "source_expiry", "root", "policy", "watch", "valid"])
async def test_actual_contact_writer_current_permissions(accounting_db, monkeypatch, change):
    from src.api import model_fabric_settings
    from src.model_fabric.configuration import write_model_fabric_configuration
    from src.model_fabric.accounting import current_inference_policy
    root, _, factory = accounting_db
    current = datetime.now(timezone.utc)
    sessions = factory.accounting_sessions
    async with sessions() as db:
        db.add(OperatorSession(id=SESSION, principal_id=OWNER, token_hash="a"*64,
            idle_expires_at=current+timedelta(hours=1), absolute_expires_at=current+timedelta(hours=2)))
    configured = setup_configuration()
    write_model_fabric_configuration(replace(model_fabric_settings._setup_configuration(
        configured.openrouter_setup, profiles=(), policies=()), egress_revision=configured.egress_revision+1))
    await durable_job_repository.configure_inference_accounting(1000)
    _, goal, watch, _, row, _ = await publish_source((sessions, root))
    queued = await admit_assessment(row.id)
    async def claim(db, run):
        current, _ = await _authority(db, row.id, execution=True)
        current.status = "assessing"
        current.revision += 1
        db.add(current)
    native = await durable_job_repository.claim_job(queued["job_id"], owner=RUNNER,
        expected_revision=queued["revision"], claim_authority_check=claim)
    policy_digest = current_inference_policy()[1]
    operation_id = "contact-guard:"+row.id
    await durable_job_repository.reserve_inference_cost(operation_id=operation_id, job_id=native["job_id"],
        owner_id=native["owner"]["principal_id"], payload_digest="b"*64, policy_digest=policy_digest, runtime_path="strategist_agent",
        profile_id="openrouter", bound_microusd=100, owner_ceiling_microusd=1000, priority=2,
        deadline_at=time.time()+60, owner=RUNNER, fencing_token=native["lease"]["fencing_token"])
    async with sessions() as db:
        if change == "source_grant":
            source = await db.get(GuardianSourceWatch, watch["id"])
            permission = json.loads(source.read_authority_json)
            permission["grant_id"] = "revoked-current-source-permission"
            source.read_authority_json = json.dumps(permission)
            db.add(source)
        elif change == "source_expiry":
            active_goal = await db.get(Goal, goal.id)
            budget = json.loads(active_goal.admission_budget_json)
            budget["period_expires_at"] = (current-timedelta(seconds=1)).isoformat()
            active_goal.admission_budget_json = json.dumps(budget)
            db.add(active_goal)
        elif change == "root":
            active_root = await db.get(OperatorSession, SESSION)
            active_root.revoked_at = current
            db.add(active_root)
        elif change == "policy":
            active_goal = await db.get(Goal, goal.id)
            active_goal.guardian_policy_revision += 1
            db.add(active_goal)
        elif change == "watch":
            source = await db.get(GuardianSourceWatch, watch["id"])
            source.plan_revision += 1
            db.add(source)
    if change == "valid":
        await durable_job_repository.contact_inference_provider(operation_id, owner=RUNNER,
            fencing_token=native["lease"]["fencing_token"], policy_digest=policy_digest)
        from types import SimpleNamespace
        from src.guardian.opportunity_runtime import _contact_limits
        from src.guardian.opportunities import policy_for
        from src.guardian.opportunity_contracts import OpportunityError
        async with sessions() as db:
            policy = policy_for(await db.get(Goal, goal.id))
            next_attempt = SimpleNamespace(owner_principal_id=OWNER, goal_id=goal.id, job_id="different-next-job")
            with pytest.raises(OpportunityError, match="assessment_minimum_gap"):
                await _contact_limits(db, next_attempt, policy)
            with pytest.raises(OpportunityError, match="assessment_daily_limit"):
                await _contact_limits(db, next_attempt, policy.model_copy(update={"max_assessments_per_utc_day": 1}))
    else:
        with pytest.raises(InferenceAccountingError):
            await durable_job_repository.contact_inference_provider(operation_id, owner=RUNNER,
                fencing_token=native["lease"]["fencing_token"], policy_digest=policy_digest)
    async with sessions() as db:
        reservation = (await db.execute(select(InferenceCostReservation).where(
            InferenceCostReservation.operation_id == operation_id))).scalar_one()
        assert (reservation.contact_started_at is not None) == (change == "valid")
        assert (await db.get(GuardianOpportunity, row.id)).assessment_json is None
    # This is a contact-writer test, not an execution proof: no HTTP callback
    # runs and no contact/settlement result is seeded or forgiven.
