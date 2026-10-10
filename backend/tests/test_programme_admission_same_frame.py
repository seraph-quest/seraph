"""Selected current-authority seam only; no production admission is claimed.

The fixture issues original Auth/Goal/programme state and runs the actual host.
The observer replaces the existing authority boundary only to inspect the new
DB reader, then unwinds the original session with a rollback sentinel.
"""
from contextlib import contextmanager

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.engine import get_session
from src.db.models import Goal, OperatorSession
from src.guardian.goal_discovery import goal_discovery_service
from src.guardian.goal_programmes import goal_programme_service, GoalProgrammeError
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
from src.memory.composition_headers import _certify_current_memory_snapshot_on_connection
from src.runtime_plugins.bridge import cordis_host
from src.runtime_plugins.ownership import begin_native_writer
from tests.test_auth_session_composition_privacy import sql_state
from tests.test_programme_prospective_live_selection import (
    genuine_live_selection, SelectionObserved, private_outputs,
)


@contextmanager
def forbid_private_bodies(engine):
    """Count no values; fail at the SQL start of the next selected body."""
    starts = []

    def before(connection, cursor, statement, parameters, context, executemany):
        query = getattr(getattr(context, "compiled", None), "statement", None)
        mapped = any(item.get("expr") is model
            for item in getattr(query, "column_descriptions", ())
            for model in (Goal, OperatorSession))
        identity3 = " ".join(statement.lower().split()).startswith(
            'select "id","created_at","revoked_at" from operator_identities ')
        if mapped or identity3:
            starts.append("private-body")
            raise AssertionError("private body started after original seam denial")

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    try:
        yield starts
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["wrong_db", "capacity", "ended", "binding"])
async def test_selected_current_validation_denies_before_next_private_body(
        genuine_live_selection, monkeypatch, scenario):
    case = genuine_live_selection
    before, outputs = sql_state(case), private_outputs(case)
    observations = []

    async def boundary(**requested):
        assert requested["goal_id"] == case.goal_id
        budget = HeaderReadBudget()
        async with get_session(header_budget=budget) as db:
            guard = await begin_native_writer(db, owner="finite_service", header_budget=budget)
            connection = await db.connection()
            common = await connection.run_sync(lambda current:
                _certify_current_memory_snapshot_on_connection(current, budget))
            _programme, binding, common, identity = await connection.run_sync(lambda current:
                guard._select_programme_admission(current, common,
                    service=goal_discovery_service, host=cordis_host,
                    goal_id=case.goal_id, programme_id=case.programme["id"],
                    grant_revision=case.programme["grant_revision"]))
            assert identity.budget is common.budget is guard.header_budget is budget
            spent = budget.remaining
            if scenario == "capacity":
                budget.debit(spent)
            elif scenario == "ended":
                guard._finish_programme_admission_selection()
            elif scenario == "binding":
                # Negative input only: a DTO cannot replace the selected facts.
                binding = binding.model_copy(update={"grant_revision": binding.grant_revision + 1})
            error, code = ((HeaderBoundsError, "canonical_bound_not_certified")
                if scenario == "capacity" else
                (GoalProgrammeError, "programme_original_selection_unavailable"))
            with forbid_private_bodies(case.engine) as bodies:
                kwargs = dict(binding=binding, policy=case.policy,
                    original_admission_guard=guard, identity_certificate=identity)
                if scenario == "wrong_db":
                    # A separate real session is a negative, never an issuer;
                    # the original guard must reject it before it starts SQL.
                    async with AsyncSession(case.engine) as foreign_db:
                        with pytest.raises(error, match="^" + code + "$"):
                            await goal_programme_service.validate_current_binding(db=foreign_db, **kwargs)
                        assert not foreign_db.in_transaction()
                else:
                    with pytest.raises(error, match="^" + code + "$"):
                        await goal_programme_service.validate_current_binding(db=db, **kwargs)
                assert bodies == []
            assert budget.remaining == (0 if scenario == "capacity" else spent)
            observations.append("actual-current-validator-denial")
            raise SelectionObserved("current seam denied; no admission")

    monkeypatch.setattr(goal_programme_service, "assert_authority", boundary)
    with pytest.raises(SelectionObserved, match="^current seam denied; no admission$"):
        await goal_discovery_service.admit(goal_id=case.goal_id,
            programme_id=case.programme["id"], grant_revision=case.programme["grant_revision"])
    assert observations == ["actual-current-validator-denial"]
    assert sql_state(case) == before and private_outputs(case) == outputs
    assert case.contacts == [] and goal_discovery_service._tasks == set()
