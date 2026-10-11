"""Real issued maintenance frame reaches original pre-yield inventory payer.

Observational cutpoint stops before body/snapshot; no Memory authority is granted.
"""
import copy
import json
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text

from src.db.engine import get_session as original_engine_session, override_session_factory
from src.db.models import WorkflowRunState
from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES
from src.workflows.job_runtime import (
    DurableJobRepository, DurableJobLeaseError, get_session as original_job_session,
    _original_memory_maintenance_entry, _original_memory_maintenance_scope,
)
from tests.test_inference_accounting import accounting_db


class InventoryObserved(Exception):
    """Test-only stop after actual original inventory debit, before caller yield."""


@pytest.mark.parametrize("operation", ["writer", "reader"])
@pytest.mark.parametrize("exhausted", [False, True])
@pytest.mark.asyncio
async def test_issued_maintenance_payer_reaches_original_pre_yield_probe(
        accounting_db, monkeypatch, operation, exhausted):
    _root, _engine, factory = accounting_db
    from src.workflows import durable_state, job_runtime
    from src.workspace import accounting_witness
    monkeypatch.setattr(durable_state, "get_session", original_engine_session)
    monkeypatch.setattr(job_runtime, "get_session", original_job_session)
    async with factory.accounting_sessions() as db:
        db.add(WorkflowRunState(run_identity="issued-frame", root_run_identity="issued-frame",
            workflow_name="numeric", job_kind="runtime_service_memory_v1", status="queued"))
    async with factory.accounting_sessions() as db:
        before_rows = (await db.execute(text("SELECT * FROM workflow_run_states"))).all()
    repository = DurableJobRepository()
    original_probe = accounting_witness.prepare_composition_read_session
    seen = []
    caller_body = []

    async def observe(db, *, header_budget=None):
        scope = _original_memory_maintenance_scope(repository)
        if scope is None:
            return await original_probe(db, header_budget=header_budget)
        assert header_budget is scope.header_budget
        before = header_budget.remaining
        seen.append(header_budget)
        # The original two bounded SELECTs run before their result is charged.
        # This test deliberately makes no zero-SQL-before-yield claim.
        await original_probe(db, header_budget=header_budget)
        assert not exhausted
        assert header_budget.remaining == before - len(
            json.dumps([1, None], separators=(",", ":")).encode())
        raise InventoryObserved

    monkeypatch.setattr(accounting_witness, "prepare_composition_read_session", observe)

    @_original_memory_maintenance_entry
    async def entry(repo):
        scope = _original_memory_maintenance_scope(repo)
        budget = scope.header_budget
        assert budget is not None
        if exhausted:
            budget.debit(MAX_BYTES)
        error = HeaderBoundsError if exhausted else InventoryObserved
        with pytest.raises(error):
            if operation == "writer":
                async with repo._writer_session():
                    caller_body.append("writer")
            else:
                await repo.get_job("issued-frame")
                caller_body.append("reader")
        assert seen == [budget]
        assert budget.references == set() and budget.future_references == set()
        if exhausted:
            assert budget.remaining == 0
        return scope

    with override_session_factory(factory):
        scope = await entry(repository)
    assert not scope.live and caller_body == []
    async with factory.accounting_sessions() as db:
        assert (await db.execute(text("SELECT * FROM workflow_run_states"))).all() == before_rows



@pytest.mark.parametrize("operation", ["writer", "reader"])
@pytest.mark.asyncio
async def test_issued_maintenance_rejects_different_frame_before_session(
        accounting_db, monkeypatch, operation):
    _root, _engine, factory = accounting_db
    repository = DurableJobRepository()
    async with factory.accounting_sessions() as db:
        db.add(WorkflowRunState(run_identity="frame-mismatch", root_run_identity="frame-mismatch",
            workflow_name="numeric", job_kind="runtime_service_memory_v1", status="queued"))

    async with factory.accounting_sessions() as db:
        before_rows = (await db.execute(text("SELECT * FROM workflow_run_states"))).all()

    @_original_memory_maintenance_entry
    async def entry(repo):
        budget = _original_memory_maintenance_scope(repo).header_budget
        assert budget is not None
        different = copy.copy(budget)  # Negative data; never an issued frame.
        before = budget.remaining
        def forbidden_session(**kwargs):
            raise AssertionError("different frame reached a session or caller body")
        monkeypatch.setattr(repo, "_session", forbidden_session)
        with pytest.raises(DurableJobLeaseError, match="original_memory_maintenance_frame_changed"):
            if operation == "writer":
                async with repo._writer_session(header_budget=different):
                    pytest.fail("different frame reached writer body")
            else:
                await repo.get_job("frame-mismatch", header_budget=different)
        assert budget.remaining == before

    await entry(repository)
    async with factory.accounting_sessions() as db:
        assert (await db.execute(text("SELECT * FROM workflow_run_states"))).all() == before_rows



@pytest.mark.parametrize("operation", ["writer", "reader"])
@pytest.mark.asyncio
async def test_no_payer_preserves_zero_argument_session_factory(
        accounting_db, monkeypatch, operation):
    _root, _engine, factory = accounting_db
    repository = DurableJobRepository()
    calls = []
    @asynccontextmanager
    async def sessions():
        calls.append("session")
        async with factory.accounting_sessions() as db:
            yield db
    monkeypatch.setattr(repository, "_session", sessions)
    if operation == "writer":
        async with repository._writer_session() as db:
            assert (await db.execute(text("SELECT 1"))).scalar_one() == 1
    else:
        assert await repository.get_job("absent") is None
    assert calls == ["session"]
