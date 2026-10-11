"""Original owner pointer capacity reads; these rows confer no method authority."""
from dataclasses import replace

import pytest
from sqlalchemy import event

from src.db.engine import get_session
from src.db.task_method_models import TaskMethodActive
from src.guardian.goal_discovery import goal_discovery_service
from src.guardian.goal_programmes import goal_programme_service
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
from src.memory.composition_headers import (
    _certify_current_memory_snapshot_on_connection, read_current_method_pointer,
)
from src.runtime_plugins.bridge import cordis_host
from src.runtime_plugins.ownership import begin_native_writer
from tests.test_programme_prospective_live_selection import (
    genuine_live_selection, SelectionObserved,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["absent", "selected", "foreign", "copy", "capacity"])
async def test_current_pointer_read_uses_original_selection_and_spent_frame(
        genuine_live_selection, monkeypatch, scenario):
    case = genuine_live_selection
    identity = case.operator.operator_identity_id
    pointer_id = "original-pointer-header-test"
    if scenario in {"selected", "foreign"}:
        # Deliberately unsigned test row: the header reader must not purport to
        # authenticate it. CurrentMethod's unchanged MAC check is a later owner.
        async with get_session() as db:
            db.add(TaskMethodActive(id=pointer_id,
                owner_identity_id=identity if scenario == "selected" else "foreign-owner",
                goal_id=case.goal_id, goal_revision=case.programme["goal_revision"],
                family="research", binding_json="null", previous_binding_json="null",
                signature_key_id="unsigned-test-row", signature_mac="invalid"))
    observations = []

    async def boundary(**requested):
        budget = HeaderReadBudget()
        async with get_session(header_budget=budget) as db:
            guard = await begin_native_writer(db, owner="finite_service", header_budget=budget)
            connection = await db.connection()
            common = await connection.run_sync(lambda current:
                _certify_current_memory_snapshot_on_connection(current, budget))
            await connection.run_sync(lambda current: guard._select_programme_admission(
                current, common, service=goal_discovery_service, host=cordis_host,
                goal_id=case.goal_id, programme_id=case.programme["id"],
                grant_revision=case.programme["grant_revision"]))
            selection = guard._prospective_admission[0]
            spent = budget.remaining
            body_starts = []

            def before(_connection, _cursor, statement, _parameters, _context, _many):
                if 'SELECT "id","owner_identity_id","goal_id","goal_revision"' in statement:
                    body_starts.append("pointer-body")

            event.listen(case.engine.sync_engine, "before_cursor_execute", before)
            try:
                if scenario == "copy":
                    with pytest.raises(HeaderBoundsError, match="task_method_pointer_owner_unavailable"):
                        await read_current_method_pointer(db, original_selection=replace(selection))
                    assert budget.remaining == spent
                elif scenario == "capacity":
                    budget.debit(budget.remaining)
                    with pytest.raises(HeaderBoundsError, match="canonical_bound_not_certified"):
                        await read_current_method_pointer(db, original_selection=selection)
                    assert budget.remaining == 0
                else:
                    pointer, foreign = await read_current_method_pointer(db, original_selection=selection)
                    assert foreign is (scenario == "foreign")
                    assert (pointer.id if pointer else None) == (
                        pointer_id if scenario == "selected" else None)
                    assert budget.remaining < spent
                    first_spent = budget.remaining
                    again, again_foreign = await read_current_method_pointer(db, original_selection=selection)
                    assert (again.id if again else None) == (pointer.id if pointer else None)
                    assert again_foreign is foreign and budget.remaining < first_spent
                    assert guard.header_budget is common.budget is budget
                assert len(body_starts) == (2 if scenario == "selected" else 0)
            finally:
                event.remove(case.engine.sync_engine, "before_cursor_execute", before)
            observations.append(scenario)
            raise SelectionObserved("pointer capacity read; no admission")

    monkeypatch.setattr(goal_programme_service, "assert_authority", boundary)
    with pytest.raises(SelectionObserved, match="pointer capacity read; no admission"):
        await goal_discovery_service.admit(goal_id=case.goal_id,
            programme_id=case.programme["id"], grant_revision=case.programme["grant_revision"])
    assert observations == [scenario]
    assert case.contacts == [] and goal_discovery_service._tasks == set()
