"""Genuine canonical acceptance negatives over original native plan staging."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text, update

from src.db.engine import get_session as canonical_session
from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth
from tests.test_native_memory_report_source_vertical import test_actual_accepted_report_source_with_mocked_browser_edge as _actual_report_journey


@pytest.mark.parametrize('kind', ['browser', 'report'])
@pytest.mark.parametrize('change', ['foreign_guard', 'closed_guard', 'invalid_type',
    'closure_tamper', 'root_revoked', 'goal_revision', 'goal_budget',
    'source_revision', 'late_opportunity_cas'])
async def test_actual_plan_acceptance_original_owner_denies_atomically(accounting_db, real_auth, monkeypatch, kind, change):
    from src.db.models import (OperatorSession, Goal, GuardianSourceWatch,
        WorkBoardTask, WorkBoardAttempt, WorkBoardInputArtifact, WorkBoardProposal,
        WorkBoardEvent, WorkBoardLink, GuardianOpportunity, WorkflowRunState)
    from src.guardian import opportunity_plans
    from src.guardian.opportunity_contracts import OpportunityError
    from src.memory.evidence_execution import BoardError
    from src.runtime_plugins.ownership import begin_native_writer, DOMAINS
    from src.workspace.production import ProductionWorkspaceReconciliationError
    from tests import test_native_memory_report_source_vertical as report_fixture

    _, engine, _ = accounting_db
    target = 'accept_browser_plan' if kind == 'browser' else 'accept_report_plan'
    original = getattr(opportunity_plans, target)
    entered, guards, failures, late_effects = [], [], [], []
    before = None

    # The same original journey supports both offered blueprints. Its final
    # HTTP transport supplies the selected blueprint; no Source or Task is made
    # by this observer. Negative acceptance stops before report-only readback.
    if kind == 'browser':
        journey = report_fixture._actual_plan_journey
        async def browser_journey(db, auth, patch, blueprint, **kwargs):
            return await journey(db, auth, patch, 'public-browser-check', **kwargs)
        monkeypatch.setattr(report_fixture, '_actual_plan_journey', browser_journey)

    async def rows(db):
        return {model.__tablename__: [row.model_dump() for row in (await db.scalars(select(model))).all()]
            for model in (WorkBoardTask, WorkBoardAttempt, WorkBoardInputArtifact,
                WorkBoardProposal, WorkBoardEvent, WorkBoardLink, GuardianOpportunity,
                WorkflowRunState)}

    async def observe_owner(**arguments):
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
                guards.append(guard)
                if change in {'root_revoked', 'goal_revision', 'goal_budget', 'source_revision'}:
                    async with canonical_session() as mutation:
                        await begin_native_writer(mutation, owner='native_ingress')
                        if change == 'root_revoked':
                            root = await mutation.get(OperatorSession, arguments['operator'].session_id)
                            root.revoked_at = datetime.now(timezone.utc)
                        elif change == 'source_revision':
                            watch = (await mutation.scalars(select(GuardianSourceWatch))).one()
                            watch.plan_revision += 1
                        else:
                            goal = (await mutation.scalars(select(Goal))).one()
                            if change == 'goal_revision': goal.revision += 1
                            else: goal.admission_budget_json = None
                elif change == 'closure_tamper':
                    async with engine.begin() as connection:
                        await connection.execute(text('UPDATE runtime_composition_states SET composition_digest = :digest WHERE runtime_domain = :domain'),
                            {'digest': 'f' * 64, 'domain': DOMAINS[0]})
                async with canonical_session() as inspection:
                    before = await rows(inspection)
                if change == 'closed_guard': guard.close()
                elif change == 'invalid_type': db.info['composition_read_guard'] = object()
                if change == 'foreign_guard':
                    async with canonical_session() as foreign_db:
                        foreign = foreign_db.info['composition_read_guard']
                        assert foreign.db is foreign_db and not foreign.closed
                        guards.append(foreign)
                        db.info['composition_read_guard'] = foreign
                        yield db
                else:
                    yield db
        with monkeypatch.context() as scope:
            scope.setattr(opportunity_plans, 'db_engine', SimpleNamespace(get_session=exact_original_session))
            if change == 'late_opportunity_cas':
                mark = opportunity_plans._mark_planned
                async def lose_original_cas(db, opportunity, proposal):
                    tasks = list((await db.scalars(select(WorkBoardTask))).all())
                    assert len(tasks) == (1 if kind == 'browser' else 3)
                    assert any(task.status == 'todo' for task in tasks)
                    late_effects.append(len(tasks))
                    await db.execute(update(GuardianOpportunity).where(GuardianOpportunity.id == opportunity.id)
                        .values(status='dismissed').execution_options(synchronize_session=False))
                    await mark(db, opportunity, proposal)
                scope.setattr(opportunity_plans, '_mark_planned', lose_original_cas)
            try:
                return await original(**arguments)
            except (OpportunityError, BoardError, ProductionWorkspaceReconciliationError) as error:
                failures.append(error)
                async with canonical_session() as inspection:
                    assert await rows(inspection) == before
                raise

    monkeypatch.setattr(opportunity_plans, target, observe_owner)
    with pytest.raises((OpportunityError, BoardError, ProductionWorkspaceReconciliationError,
            AssertionError, pytest.fail.Exception)) as journey_denied:
        await _actual_report_journey(accounting_db, real_auth, monkeypatch, lambda *_: None)
    if not entered:
        raise journey_denied.value
    assert len(entered) == 2 and before is not None
    assert guards and all(guard.closed for guard in guards)
    assert len(failures) == 1
    error = failures[0]
    if change in {'foreign_guard', 'closed_guard', 'invalid_type'}:
        assert error.code == 'composition_provider_invalid'
    elif change == 'closure_tamper':
        assert str(error) == 'composition_continuity_unavailable'
    elif change == 'root_revoked':
        assert error.code == 'evidence_owner_not_current'
    elif change.startswith('goal_'):
        assert error.code == 'goal_review_required'
    elif change == 'source_revision':
        assert error.code == 'source_stale'
    else:
        assert error.code == 'proposal_stale'
        assert late_effects == [1 if kind == 'browser' else 3]
    async with canonical_session() as db:
        assert await rows(db) == before
        assert len(before[WorkBoardTask.__tablename__]) == 1
        assert len(before[WorkBoardProposal.__tablename__]) == 1
        assert before[WorkBoardLink.__tablename__] == []
