"""Actual report route stops at exact original second plan writer negatives."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from src.db.engine import get_session as canonical_session
from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth


@pytest.mark.parametrize('change', ['foreign_guard', 'closed_guard', 'invalid_type',
    'closure_tamper', 'root_revoked', 'root_expired', 'root_replaced', 'source_revision'])
async def test_exact_original_plan_second_writer_denies_without_new_card(accounting_db, real_auth, monkeypatch, change):
    from src.db.models import OperatorSession, GuardianSourceWatch, WorkBoardTask, WorkBoardProposal, WorkBoardEvent, WorkflowRunState
    from src.guardian import opportunity_plans
    from src.guardian.opportunity_contracts import OpportunityError
    from src.memory.evidence_execution import BoardError
    from src.runtime_plugins.ownership import begin_native_writer, DOMAINS
    from src.workspace.production import ProductionWorkspaceReconciliationError
    from tests.test_native_memory_report_source_vertical import test_actual_accepted_report_source_with_mocked_browser_edge

    _, engine, _ = accounting_db
    original_once = opportunity_plans._generate_plan_once
    original_database = opportunity_plans.db_engine
    entered = []
    actual_guards = []
    before = None
    async def retained_rows(db):
        return {model.__tablename__: [row.model_dump() for row in (await db.scalars(select(model))).all()]
            for model in (WorkBoardTask, WorkBoardProposal, WorkBoardEvent, WorkflowRunState)}

    async def observed_original_once(**arguments):
        @asynccontextmanager
        async def exact_original_session():
            nonlocal before
            async with canonical_session() as db:
                entered.append(db)
                if len(entered) != 2:
                    yield db
                    return
                guard = db.info['composition_read_guard']
                assert guard.db is db and not guard.closed
                actual_guards.append(guard)
                if change in {'root_revoked', 'root_expired', 'root_replaced', 'source_revision'}:
                    # Genuine writer changes current original authority after
                    # the real physical source/SafeTaskText staging, never a DTO.
                    async with canonical_session() as mutation:
                        await begin_native_writer(mutation, owner='native_ingress')
                        if change == 'source_revision':
                            watch = (await mutation.scalars(select(GuardianSourceWatch))).one()
                            watch.plan_revision += 1
                        else:
                            root = await mutation.get(OperatorSession, arguments['operator'].session_id)
                            now = datetime.now(timezone.utc)
                            if change == 'root_revoked': root.revoked_at = now
                            elif change == 'root_expired': root.idle_expires_at = now - timedelta(seconds=1)
                            else: root.replaced_by_id = 'unfollowed-plan-root'
                elif change == 'closure_tamper':
                    async with engine.begin() as connection:
                        await connection.execute(text('UPDATE runtime_composition_states SET composition_digest = :digest WHERE runtime_domain = :domain'),
                            {'digest': 'f' * 64, 'domain': DOMAINS[0]})
                async with canonical_session() as inspection:
                    before = await retained_rows(inspection)
                if change == 'closed_guard': guard.close()
                elif change == 'invalid_type': db.info['composition_read_guard'] = object()
                if change == 'foreign_guard':
                    async with canonical_session() as foreign_db:
                        foreign = foreign_db.info['composition_read_guard']
                        assert foreign.db is foreign_db and foreign.db is not db and not foreign.closed
                        actual_guards.append(foreign)
                        db.info['composition_read_guard'] = foreign
                        yield db
                else:
                    yield db
        # Only the plan module's original get_session calls are observed.
        # Every yielded session/guard is supplied by the canonical owner.
        with monkeypatch.context() as plan_scope:
            plan_scope.setattr(opportunity_plans, 'db_engine', SimpleNamespace(get_session=exact_original_session))
            return await original_once(**arguments)

    monkeypatch.setattr(opportunity_plans, '_generate_plan_once', observed_original_once)
    with pytest.raises((OpportunityError, BoardError, ProductionWorkspaceReconciliationError)) as denied:
        await test_actual_accepted_report_source_with_mocked_browser_edge(accounting_db, real_auth, monkeypatch, lambda *_: None)
    assert len(entered) == 2 and entered[0] is not entered[1]
    assert before is not None and actual_guards and all(guard.closed for guard in actual_guards)
    if change in {'foreign_guard', 'closed_guard', 'invalid_type'}:
        assert denied.value.code == 'composition_provider_invalid'
    elif change == 'closure_tamper':
        assert str(denied.value) == 'composition_continuity_unavailable'
    elif change.startswith('root_'):
        assert denied.value.code == 'evidence_owner_not_current'
    else:
        assert denied.value.code == 'source_stale'
    async with canonical_session() as db:
        assert await retained_rows(db) == before
        assert before[WorkBoardTask.__tablename__] == []
        assert before[WorkBoardProposal.__tablename__] == []
