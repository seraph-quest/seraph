"""Original policy writer through actual full-init/auth/composition owners."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from src.db.engine import get_session as canonical_session, override_session_factory
from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth


@pytest.mark.parametrize('change', ['valid', 'missing_root', 'revoked', 'replacement', 'tombstone',
    'idle', 'absolute', 'goal_owner', 'revision', 'inactive', 'proactive', 'budget',
    'policy_revision', 'notification_ack', 'auto_stage_ack', 'foreign_guard', 'closed_guard',
    'closure_tamper', 'identity_revoked'])
async def test_policy_original_canonical_native_ingress(accounting_db, real_auth, monkeypatch, change):
    from src.db import engine as original
    from src.db.models import OperatorSession, OperatorIdentity, Goal, AuditEvent, WorkflowRunState
    from src.auth.service import create_session
    from src.goals.repository import goal_repository
    from src.goals.contracts import GoalAdmissionBudget
    from src.guardian.source_watch import SourceWatchService
    from src.guardian.opportunities import save_policy
    from src.guardian.opportunity_contracts import GuardianPolicySave, OpportunityError
    from src.runtime_plugins.ownership import DOMAINS, begin_native_writer, initialize_fresh_deployment
    from src.runtime_plugins.composition import reviewed_composition
    from src.workspace.production import ProductionWorkspace, maintenance_fence, ProductionWorkspaceReconciliationError
    from tests.test_native_memory_report_source_vertical import NODE

    root, engine, factory = accounting_db
    monkeypatch.setattr(original, 'engine', engine)
    monkeypatch.setattr(original, '_db_path', str(root / 'seraph.db'))
    monkeypatch.setattr(original, 'async_session_factory', factory)
    for target in ('src.db.engine.get_session', 'src.auth.service.get_session',
                   'src.goals.repository.get_session', 'src.audit.repository.get_session'):
        monkeypatch.setattr(target, canonical_session)
    await original.init_db()
    with override_session_factory(factory):
        _, operator = await create_session()
        now = datetime.now(timezone.utc)
        goal = await goal_repository.create(title='Real bounded policy owner', proactive_enabled=True,
            owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id,
            admission_budget=GoalAdmissionBudget(reviewed_grant=True, grant_id='real-policy-grant',
                max_outstanding_jobs=2, max_attempts=2, max_runtime_seconds=300,
                period_started_at=now, period_expires_at=now + timedelta(hours=1)))
        identity_id = None
        if change == 'identity_revoked':
            from src.auth.ownership import enroll
            identity_id, _, _ = await enroll(operator)
        reviewed = reviewed_composition(node_path=NODE)
        with maintenance_fence(ProductionWorkspace(host_root=root)):
            async with canonical_session() as db:
                await begin_native_writer(db, owner='composition_maintenance', fresh=True)
                await initialize_fresh_deployment(db, composition_digests={d: reviewed.composition_digest for d in DOMAINS})
        watch = await SourceWatchService().create_watch(owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id, goal_id=goal.id, expected_goal_revision=goal.revision,
            sources=[dict(source_key='public', kind='public_https_text', target='https://example.com/public', priority=1)],
            criteria={}, schedule=dict(cron='0 * * * *', timezone='UTC'), write_mode='standing_reviewed',
            reviewed_grant_id='real-policy-grant')
        body = GuardianPolicySave(expected_goal_revision=goal.revision, expected_policy_revision=0,
            idempotency_key=uuid4(), policy=dict(schema_version='seraph.guardian.policy.v1',
                assessment_enabled=True, confirmed_at=now, review_due_at=now + timedelta(days=7),
                grant_id='real-policy-grant', original_root_id=operator.session_id,
                goal_revision=goal.revision, source_watch_ids=[watch['id']], max_assessments_per_utc_day=2))
        async with canonical_session() as db:
            await begin_native_writer(db, owner='native_ingress')
            auth_root, actual_goal = await db.get(OperatorSession, operator.session_id), await db.get(Goal, goal.id)
            if change == 'missing_root': await db.delete(auth_root)
            elif change == 'revoked': auth_root.revoked_at = now
            elif change == 'replacement': auth_root.replaced_by_id = 'unfollowed-replacement'
            elif change == 'tombstone': auth_root.is_bearer_tombstone = True
            elif change == 'idle': auth_root.idle_expires_at = now - timedelta(seconds=1)
            elif change == 'absolute': auth_root.absolute_expires_at = now - timedelta(seconds=1)
            elif change == 'goal_owner': actual_goal.owner_principal_id = 'foreign-goal-owner'
            elif change == 'revision': actual_goal.revision += 1
            elif change == 'inactive': actual_goal.status = 'completed'
            elif change == 'proactive': actual_goal.proactive_enabled = False
            elif change == 'budget': actual_goal.admission_budget_json = None
            elif change == 'policy_revision': actual_goal.guardian_policy_revision = 1
            elif change == 'identity_revoked':
                identity = await db.get(OperatorIdentity, identity_id)
                identity.revoked_at = now
        if change == 'notification_ack': body.policy.max_notification_per_utc_day = 1
        if change == 'auto_stage_ack':
            body.policy.auto_stage_plan = True
            body.policy.max_plan_proposals_per_utc_day = 1
        if change == 'closure_tamper':
            async with engine.begin() as connection:
                await connection.execute(text('UPDATE runtime_composition_states SET composition_digest = :digest WHERE runtime_domain = :domain'),
                    {'digest': 'f' * 64, 'domain': DOMAINS[0]})
        async with canonical_session() as db:
            before_goal = (await db.get(Goal, goal.id)).model_dump()
            record = await db.get(OperatorSession, operator.session_id)
            before_root = record.model_dump() if record else None
            before_audit = [row.model_dump() for row in (await db.scalars(select(AuditEvent).order_by(AuditEvent.id))).all()]
        arguments = dict(operator=operator, goal_id=goal.id, request=body)
        if change in {'foreign_guard', 'closed_guard'}:
            observed = []
            @asynccontextmanager
            async def invalid_original_session():
                async with canonical_session() as db:
                    guard = db.info['composition_read_guard']
                    observed.append(guard)
                    if change == 'closed_guard':
                        guard.close()
                        yield db
                    else:
                        async with canonical_session() as other:
                            actual_foreign = other.info['composition_read_guard']
                            observed.append(actual_foreign)
                            db.info['composition_read_guard'] = actual_foreign
                            yield db
            with monkeypatch.context() as ingress_patch:
                ingress_patch.setattr(original, 'get_session', invalid_original_session)
                with pytest.raises(OpportunityError) as denied:
                    await save_policy(**arguments)
                assert denied.value.code == 'composition_provider_invalid'
            assert all(guard.closed for guard in observed)
        elif change == 'closure_tamper':
            with pytest.raises(ProductionWorkspaceReconciliationError, match='composition_continuity_unavailable'):
                await save_policy(**arguments)
        elif change == 'valid':
            saved = await save_policy(**arguments)
            assert saved['guardian_policy_revision'] == 1
            assert await save_policy(**arguments) == saved
            body.policy.max_assessments_per_utc_day = 3
            with pytest.raises(OpportunityError) as denied:
                await save_policy(**arguments)
            assert denied.value.code == 'policy_idempotency_conflict'
        else:
            with pytest.raises(OpportunityError) as denied:
                await save_policy(**arguments)
            expected = {'missing_root': 'original_root_unavailable', 'revoked': 'original_root_unavailable',
                'replacement': 'original_root_unavailable', 'tombstone': 'original_root_unavailable',
                'idle': 'original_root_unavailable', 'absolute': 'original_root_unavailable',
                'goal_owner': 'goal_owner_mismatch', 'revision': 'goal_review_required',
                'inactive': 'goal_review_required', 'proactive': 'goal_review_required', 'budget': 'goal_review_required',
                'policy_revision': 'guardian_policy_revision_stale', 'notification_ack': 'notification_acknowledgment_required',
                'auto_stage_ack': 'auto_stage_acknowledgment_required'}
            expected['identity_revoked'] = 'original_root_unavailable'
            assert denied.value.code == expected[change]
        async with canonical_session() as db:
            record = await db.get(OperatorSession, operator.session_id)
            assert (record.model_dump() if record else None) == before_root
            actual_goal = await db.get(Goal, goal.id)
            audit = [row.model_dump() for row in (await db.scalars(select(AuditEvent).order_by(AuditEvent.id))).all()]
            if change == 'valid':
                assert actual_goal.revision == before_goal['revision']
                assert actual_goal.guardian_policy_revision == 1
                assert len(audit) == len(before_audit) + 1
                assert sum(row['event_type'] == 'guardian_policy_saved' for row in audit) == 1
            else:
                assert actual_goal.model_dump() == before_goal
                assert audit == before_audit
            assert not list((await db.scalars(select(WorkflowRunState))).all())
