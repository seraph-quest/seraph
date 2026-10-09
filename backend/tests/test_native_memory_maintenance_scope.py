"""FILE SQLite numeric scope mechanics; these fixtures grant no Memory authority."""
import asyncio
import copy

import pytest
from sqlalchemy import select, text, event

from src.db.models import WorkflowRunState
from src.memory.header_bounds import HeaderReadBudget
from src.workflows.job_runtime import (DurableJobRepository, DurableJobLeaseError,
    _CURRENT_MEMORY_MAINTENANCE, _original_memory_maintenance_entry,
    _original_memory_maintenance_scope, _original_memory_maintenance_snapshot)


@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.asyncio
async def test_registered_scope_same_call_task_and_repository_only(async_db, monkeypatch):
    repository = DurableJobRepository()
    monkeypatch.setattr(repository, "_session", async_db)
    observed = []
    @_original_memory_maintenance_entry
    async def inner(repo):
        observed.append(_original_memory_maintenance_scope(repo))
    @_original_memory_maintenance_entry
    async def outer(repo):
        scope = _original_memory_maintenance_scope(repo)
        assert scope.header_budget is None
        await inner(repo)
        with pytest.raises(DurableJobLeaseError):
            _original_memory_maintenance_scope(DurableJobRepository())
        async def inherited():
            with pytest.raises(DurableJobLeaseError):
                _original_memory_maintenance_scope(repo)
        await asyncio.create_task(inherited())
        token = _CURRENT_MEMORY_MAINTENANCE.set(copy.copy(scope))
        try:
            with pytest.raises(DurableJobLeaseError):
                _original_memory_maintenance_scope(repo)
        finally:
            _CURRENT_MEMORY_MAINTENANCE.reset(token)
        return scope
    scope = await outer(repository)
    assert observed == [scope]
    assert not scope.live and _original_memory_maintenance_scope(repository) is None


@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.asyncio
async def test_memory_scope_keeps_one_frame_and_charges_repeated_snapshots(async_db, monkeypatch):
    # Raw historical routing fixture, no native candidate/Original/Source.
    async with async_db() as db:
        db.add(WorkflowRunState(run_identity="scope-memory", root_run_identity="scope-memory",
            workflow_name="numeric", job_kind="runtime_service_memory_v1", status="queued"))
    repository = DurableJobRepository()
    monkeypatch.setattr(repository, "_session", async_db)
    frames = []
    original_init = HeaderReadBudget.__init__
    def init(frame):
        original_init(frame)
        frames.append(frame)
    monkeypatch.setattr(HeaderReadBudget, "__init__", init)
    @_original_memory_maintenance_entry
    async def original_entry(repo):
        scope = _original_memory_maintenance_scope(repo)
        async with repo._writer_session() as db:
            connection = await db.connection()
            driver = (await connection.get_raw_connection()).driver_connection
            assert driver.in_transaction
            before = scope.header_budget.remaining
            changes = (await db.execute(text("SELECT total_changes()"))).scalar_one()
            await _original_memory_maintenance_snapshot(repo, db, writer=True)
            middle = scope.header_budget.remaining
            await _original_memory_maintenance_snapshot(repo, db, writer=True)
            assert scope.header_budget.remaining < middle < before
            assert (await db.execute(text("SELECT total_changes()"))).scalar_one() == changes
        first = scope.header_budget.remaining
        await repo.get_job("scope-memory")
        assert scope.header_budget.remaining < first
        return scope
    statements = []
    # Session factory retains the actual async SQLite engine; trace its driver.
    async with async_db() as db:
        engine = db.get_bind()
    def trace(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", trace)
    try:
        scope = await original_entry(repository)
    finally:
        event.remove(engine, "before_cursor_execute", trace)
    assert frames == [scope.header_budget]
    assert "BEGIN IMMEDIATE" in statements
    writer_begin = statements.index("BEGIN IMMEDIATE")
    full_headers = next(i for i in range(writer_begin + 1, len(statements))
        if "FROM sqlite_schema" in statements[i])
    body = next(i for i in range(full_headers + 1, len(statements))
        if statements[i].startswith("SELECT workflow_run_states."))
    assert writer_begin < full_headers < body


@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.asyncio
async def test_no_memory_scope_route_race_denies_before_body(async_db, monkeypatch):
    repository = DurableJobRepository()
    monkeypatch.setattr(repository, "_session", async_db)
    @_original_memory_maintenance_entry
    async def original_entry(repo):
        assert _original_memory_maintenance_scope(repo).header_budget is None
        async with async_db() as other:
            other.add(WorkflowRunState(run_identity="racing-memory", root_run_identity="racing-memory",
                workflow_name="numeric", job_kind="runtime_service_memory_v1", status="queued"))
        with pytest.raises(DurableJobLeaseError, match="route_changed"):
            await repo.get_job("racing-memory")
    await original_entry(repository)
    async with async_db() as db:
        assert (await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == "racing-memory"))).scalar_one().status == "queued"


@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("method,kwargs", [
    ("transfer_lease", {"from_owner": "old", "fencing_token": 1, "to_owner": "new"}),
    ("retry_job", {"owner_kind": "user", "owner_principal_id": "owner", "reconciliation_receipt": {}}),
    ("record_checkpoint", {"checkpoint_id": "plain", "state": {}, "owner": None, "fencing_token": None}),
    ("record_artifact", {"artifact_type": "file", "file_path": "fixture.txt"}),
    ("record_effect", {"effect_type": "plain", "target_path": "fixture"}),
    ("record_recovery_checkpoint", {"owner_kind": "user", "owner_principal_id": "owner",
        "checkpoint_id": "plain", "state": {}}),
    ("record_recovery_artifact", {"owner_kind": "user", "owner_principal_id": "owner",
        "file_path": "fixture.txt"}),
    ("record_readback", {"target_path": "fixture", "status": "verified"}),
    ("record_remote_inference_intent", {"operation_id": "denied-operation", "owner_id": "owner"}),
    ("record_remote_inference_receipt", {"operation_id": "denied-operation", "owner_id": "owner",
        "status": "queued"}),
])
async def test_generic_memory_entry_scalar_denies_before_any_body(async_db, monkeypatch, method, kwargs):
    async with async_db() as db:
        db.add(WorkflowRunState(run_identity="denied-memory", root_run_identity="denied-memory",
            workflow_name="numeric", job_kind="runtime_service_memory_v1", status="queued"))
    repository = DurableJobRepository()
    monkeypatch.setattr(repository, "_session", async_db)
    async def body_forbidden(*args, **kwargs):
        raise AssertionError("generic Memory entry reached original body")
    monkeypatch.setattr(repository, "_fetch", body_forbidden)
    monkeypatch.setattr(repository, "_writer_session", body_forbidden)
    monkeypatch.setattr(repository, "get_job", body_forbidden)
    with pytest.raises(DurableJobLeaseError, match="generic_mutation_unavailable"):
        if method == "record_remote_inference_intent":
            await repository.record_remote_inference_intent(job_id="denied-memory", **kwargs)
        elif method == "record_remote_inference_receipt":
            await repository.record_remote_inference_receipt({"job_id": "denied-memory", **kwargs})
        else:
            await getattr(repository, method)("denied-memory", **kwargs)
