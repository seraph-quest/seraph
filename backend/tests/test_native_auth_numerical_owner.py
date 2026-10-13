"""Numerical scope identity never substitutes an original issuing call chain."""
from copy import copy

import pytest

from tests.test_input_artifact_native_writer_epochs import original_artifact_owner
from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth
from src.db.engine import get_session
from src.memory import composition_headers as headers
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError


@pytest.mark.parametrize("foreign", ["new", "copy"])
def test_foreign_frame_cannot_reuse_original_numeric_scope(foreign):
    budget = HeaderReadBudget()
    other = HeaderReadBudget() if foreign == "new" else copy(budget)
    with headers._memory_current_snapshot_scope(budget):
        with pytest.raises(HeaderBoundsError, match="memory_snapshot_scope_unavailable"):
            headers._checked_current_snapshot_scope(other)
    assert budget.remaining == other.remaining == 1048576


def test_sessionless_scope_cannot_select_either_protected_projection():
    budget = HeaderReadBudget()
    with headers._memory_current_snapshot_scope(budget):
        assert headers._original_locator_schema_width(None, None, None, budget) is None
        assert headers._original_common_header_single_row(None, None, None, budget) is False
    assert budget.remaining == 1048576


@pytest.mark.asyncio
async def test_original_factory_retains_connection_across_real_boundary(original_artifact_owner):
    budget = HeaderReadBudget()
    async with get_session(header_budget=budget) as db:
        scope = headers._checked_current_snapshot_scope(budget)
        assert scope.session is db and scope.budget is budget
        first = await db.connection()
        await first.run_sync(lambda connection: headers._check_snapshot_scope_connection(scope, connection))
        before = budget.remaining
        await db.rollback()
        second = await db.connection()
        await second.run_sync(lambda connection: headers._check_snapshot_scope_connection(scope, connection))
        assert second is first and scope.connection is first.sync_connection
        assert budget.remaining == before < 1048576


@pytest.mark.asyncio
async def test_foreign_connection_rejected_by_real_factory_scope(original_artifact_owner):
    _owner, _request, factory, _operator = original_artifact_owner
    budget = HeaderReadBudget()
    async with get_session(header_budget=budget) as db:
        scope = headers._checked_current_snapshot_scope(budget)
        async with factory.kw["bind"].connect() as foreign:
            with pytest.raises(HeaderBoundsError, match="memory_snapshot_scope_unavailable"):
                await foreign.run_sync(lambda connection: headers._check_snapshot_scope_connection(scope, connection))
        assert scope.session is db and scope.budget is budget


@pytest.mark.asyncio
async def test_unknown_wrapper_cannot_select_original_projection(original_artifact_owner):
    budget = HeaderReadBudget()
    async with get_session(header_budget=budget) as db:
        def intrusive_wrapper(connection):
            assert headers._original_locator_schema_width(connection, None, None, budget) is None
            assert headers._original_common_header_single_row(connection, None, None, budget) is False
        await (await db.connection()).run_sync(intrusive_wrapper)
        assert headers._checked_current_snapshot_scope(budget).session is db


@pytest.mark.asyncio
async def test_caller_cannot_attach_arbitrary_session_to_numeric_scope(original_artifact_owner):
    budget = HeaderReadBudget()
    _owner, _request, factory, _operator = original_artifact_owner
    async with factory() as db:
        with pytest.raises(HeaderBoundsError, match="memory_snapshot_scope_unavailable"):
            with headers._memory_current_snapshot_scope(budget, _session=db):
                raise AssertionError("unissued session scope entered")
        assert budget.remaining == 1048576


@pytest.mark.asyncio
@pytest.mark.parametrize("reader_state", ["absent", "closed"])
async def test_publication_recertifies_with_conservative_bound_after_reader_retirement(
        original_artifact_owner, reader_state):
    from src.runtime_plugins.ownership import begin_native_writer
    from src.work_board.contracts import WorkBoardTaskCreate
    from src.work_board.repository import WorkBoardRepository
    owner, request, _factory, _operator = original_artifact_owner
    budget = HeaderReadBudget()
    async with get_session(header_budget=budget) as db:
        reader = db.info["composition_read_guard"]
        await begin_native_writer(db, owner="native_ingress", header_budget=budget)
        guard = db.info["composition_guard"]
        assert reader.closed and "composition_read_guard" not in db.info
        if reader_state == "closed":
            # Retain the real retired reader solely to cover the closed marker.
            db.info["composition_read_guard"] = reader
        before = budget.remaining
        task = await WorkBoardRepository().create_task(db, owner, WorkBoardTaskCreate(
            title="Original publication recertification", goal_id=request.goal_id,
            goal_revision=request.goal_revision,
            idempotency_key="original-publication-" + reader_state))
        await db.flush()
        assert guard.touched
        original_connection = headers._checked_current_snapshot_scope(budget).connection
        # The original factory exit performs the sole publication and commit.
    assert guard._publication_common.budget is budget
    assert guard._publication_common.connection is original_connection
    assert budget.remaining < before
    assert task.task.task_id and guard.header_budget is budget
    if reader_state == "closed":
        assert db.info.pop("composition_read_guard") is reader and reader.closed
