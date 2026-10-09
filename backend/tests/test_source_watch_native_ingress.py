"""Original SourceWatch ingress against real auth, full schema and composition."""
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from config.settings import settings
from src.db.engine import get_session as canonical_session, override_session_factory
from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth


@pytest.mark.parametrize('change', ['valid', 'setup_replay', 'missing_root', 'principal', 'revoked', 'replacement',
    'tombstone', 'idle', 'absolute', 'goal_owner', 'revision', 'inactive', 'proactive', 'grant',
    'budget', 'private_source', 'foreign_guard', 'closed_guard', 'closure_tamper',
    'concurrent_setup'])
async def test_original_source_watch_creation_current_writer_authority(accounting_db, real_auth, monkeypatch, change):
    from src.db import engine as original
    from src.db.models import OperatorSession, Session, Goal, GuardianSourceWatch, ScheduledJob
    from src.auth.service import create_session
    from src.goals.repository import goal_repository
    from src.goals.contracts import GoalAdmissionBudget
    from src.guardian.source_watch import SourceWatchService, SourceWatchError
    from src.runtime_plugins.ownership import DOMAINS, begin_native_writer, initialize_fresh_deployment
    from src.runtime_plugins.composition import reviewed_composition
    from src.workspace.production import ProductionWorkspace, maintenance_fence
    from src.workspace.production import ProductionWorkspaceReconciliationError
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
        _token, operator = await create_session()
        other_operator = None
        if change == 'principal':
            _, other_operator = await create_session()
        now = datetime.now(timezone.utc)
        goal = await goal_repository.create(title='Original reviewed watch', proactive_enabled=True,
            owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id,
            admission_budget=GoalAdmissionBudget(reviewed_grant=True, grant_id='original-watch-grant',
                max_outstanding_jobs=2, max_attempts=2, max_runtime_seconds=300,
                period_started_at=now, period_expires_at=now+timedelta(hours=1)))
        # This is real preexisting conversation data, not an auth substitute.
        async with canonical_session() as db:
            session = await db.get(Session, operator.session_id)
            if session is None:
                session = Session(id=operator.session_id)
                db.add(session)
            session.title = 'Preserve original private conversation'
            session.owner_principal_id = operator.principal.principal_id
        reviewed = reviewed_composition(node_path=NODE)
        with maintenance_fence(ProductionWorkspace(host_root=root)):
            async with canonical_session() as db:
                await begin_native_writer(db, owner='composition_maintenance', fresh=True)
                await initialize_fresh_deployment(db, composition_digests={d:reviewed.composition_digest for d in DOMAINS})
        async with canonical_session() as db:
            await begin_native_writer(db, owner='native_ingress')
            auth_root = await db.get(OperatorSession, operator.session_id)
            actual_goal = await db.get(Goal, goal.id)
            if change == 'missing_root':
                await db.delete(auth_root)
            elif change == 'principal': actual_goal.owner_principal_id = other_operator.principal.principal_id
            elif change == 'revoked': auth_root.revoked_at = now
            elif change == 'replacement': auth_root.replaced_by_id = 'unfollowed-replacement'
            elif change == 'tombstone': auth_root.is_bearer_tombstone = True
            elif change == 'idle': auth_root.idle_expires_at = now-timedelta(seconds=1)
            elif change == 'absolute': auth_root.absolute_expires_at = now-timedelta(seconds=1)
            elif change == 'goal_owner': actual_goal.owner_principal_id = 'wrong-goal-owner'
            elif change == 'revision': actual_goal.revision += 1
            elif change == 'inactive': actual_goal.status = 'completed'
            elif change == 'proactive': actual_goal.proactive_enabled = False
            elif change == 'budget': actual_goal.admission_budget_json = None
        if change == 'closure_tamper':
            # Real retained bytes diverge from the original published closure.
            # No guard, authority receipt or replacement closure is manufactured.
            async with engine.begin() as connection:
                await connection.execute(text('UPDATE runtime_composition_states SET composition_digest = :digest WHERE runtime_domain = :domain'),
                    {'digest': 'f' * 64, 'domain': DOMAINS[0]})
        async with canonical_session() as db:
            before_session = (await db.get(Session, operator.session_id)).model_dump()
            record = await db.get(OperatorSession, operator.session_id)
            before_root = record.model_dump() if record else None
            before_goal = (await db.get(Goal, goal.id)).model_dump()
        call = dict(owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id,
            goal_id=goal.id, expected_goal_revision=goal.revision, sources=[dict(source_key='public',
                kind='public_https_text', target='https://example.com/public', priority=1)], criteria={},
            schedule=dict(cron='0 * * * *', timezone='UTC'), write_mode='standing_reviewed',
            reviewed_grant_id='wrong-grant' if change == 'grant' else 'original-watch-grant')
        if change == 'principal':
            call['owner_principal_id'] = other_operator.principal.principal_id
        if change == 'private_source':
            call['sources'] = [dict(source_key='private', kind='workspace_text', target='notes/private.txt', priority=1)]
        success = change in {'valid', 'setup_replay', 'concurrent_setup'}
        if change in {'setup_replay', 'concurrent_setup'}:
            call['setup_watch_id'] = 'setup-original-watch'
        if change in {'foreign_guard', 'closed_guard'}:
            observed = []
            @asynccontextmanager
            async def invalid_original_session():
                async with canonical_session() as db:
                    actual = db.info['composition_read_guard']
                    observed.append(actual)
                    if change == 'closed_guard':
                        actual.close()
                        yield db
                    else:
                        async with canonical_session() as other:
                            foreign = other.info['composition_read_guard']
                            assert foreign.db is other and foreign.db is not db and not foreign.closed
                            # Negative pointer corruption of two genuine guards,
                            # never a fake provider or an authority grant.
                            db.info['composition_read_guard'] = foreign
                            observed.append(foreign)
                            yield db
            with monkeypatch.context() as ingress_patch:
                ingress_patch.setattr(original, 'get_session', invalid_original_session)
                with pytest.raises(SourceWatchError) as denied:
                    await SourceWatchService().create_watch(**call)
                assert denied.value.code == 'composition_provider_invalid'
            assert observed and all(guard.closed for guard in observed)
        elif change == 'closure_tamper':
            with pytest.raises(ProductionWorkspaceReconciliationError, match='composition_continuity_unavailable'):
                await SourceWatchService().create_watch(**call)
        elif change == 'concurrent_setup':
            from src.runtime_plugins import ownership
            writer_held, release_writer, second_finished = (asyncio.Event() for _ in range(3))
            entered = []
            guards = []
            original_begin = ownership.begin_native_writer
            async def controlled_original_writer(db, **kwargs):
                entered.append(db)
                if len(entered) == 1:
                    guards.append(db.info['composition_read_guard'])
                    guard = await original_begin(db, **kwargs)
                    writer_held.set()
                    await asyncio.wait_for(release_writer.wait(), timeout=10)
                    return guard
                try:
                    return await original_begin(db, **kwargs)
                finally:
                    second_finished.set()
            with monkeypatch.context() as ingress_patch:
                ingress_patch.setattr(ownership, 'begin_native_writer', controlled_original_writer)
                first = asyncio.create_task(SourceWatchService().create_watch(**call))
                try:
                    await asyncio.wait_for(writer_held.wait(), timeout=10)
                    second = asyncio.create_task(SourceWatchService().create_watch(**call))
                    try:
                        await asyncio.wait_for(second_finished.wait(), timeout=10)
                        with pytest.raises(ProductionWorkspaceReconciliationError, match='accounting continuity busy'):
                            await second
                    finally:
                        release_writer.set()
                    watch = await asyncio.wait_for(first, timeout=10)
                finally:
                    release_writer.set()
                    if not first.done():
                        first.cancel()
                        await asyncio.gather(first, return_exceptions=True)
            # Winner audit publication may acquire another original writer.
            assert len(entered) >= 2 and entered[0] is not entered[1]
            assert guards and all(guard.closed for guard in guards)
            from src.workspace.accounting_witness import maintenance_accounting_lock
            with maintenance_accounting_lock(root):
                pass  # Genuine original maintenance ownership is released.
            replay = await SourceWatchService().create_watch(**call)
            assert replay['id'] == watch['id'] == 'setup-original-watch'
            with pytest.raises(SourceWatchError) as conflict:
                await SourceWatchService().create_watch(**{**call, 'sources': [dict(source_key='public',
                    kind='public_https_text', target='https://example.com/different', priority=1)]})
            assert conflict.value.code == 'setup_journey_payload_conflict'
        elif success:
            watch = await SourceWatchService().create_watch(**call)
            assert watch['owner_session_id'] == operator.session_id
            if change == 'setup_replay':
                replay = await SourceWatchService().create_watch(**call)
                assert replay['id'] == watch['id']
                with pytest.raises(SourceWatchError) as conflict:
                    await SourceWatchService().create_watch(**{**call, 'sources': [dict(source_key='public',
                        kind='public_https_text', target='https://example.com/different', priority=1)]})
                assert conflict.value.code == 'setup_journey_payload_conflict'
        else:
            with pytest.raises((SourceWatchError, HTTPException)) as denied:
                await SourceWatchService().create_watch(**call)
            codes = {'missing_root':'authentication_required', 'principal':'authentication_required',
                'revoked':'session_revoked', 'replacement':'session_revoked', 'tombstone':'session_revoked',
                'idle':'session_expired', 'absolute':'session_expired'}
            if change in codes:
                assert denied.value.status_code == 401 and denied.value.detail == {'code':codes[change]}
        async with canonical_session() as db:
            assert (await db.get(Session, operator.session_id)).model_dump() == before_session
            record = await db.get(OperatorSession, operator.session_id)
            assert (record.model_dump() if record else None) == before_root
            assert (await db.get(Goal, goal.id)).model_dump() == before_goal
            assert len(list((await db.scalars(select(GuardianSourceWatch))).all())) == int(success)
            assert len(list((await db.scalars(select(ScheduledJob))).all())) == int(success)
