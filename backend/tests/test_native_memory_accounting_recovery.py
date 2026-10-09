"""Actual FILESQLite liabilities are protected; no native Source/Effect grant."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from tests.test_inference_accounting import accounting_db, request, setup_configuration
from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
from src.workflows.inference_accounting import InferenceAccountingError
from src.workflows.job_runtime import DurableJobRepository
from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget


async def raw_rows(factory, job_id):
    async with factory.accounting_sessions() as db:
        run = (await db.execute(text("SELECT * FROM workflow_run_states WHERE run_identity=:job"),
            {"job": job_id})).one()
        liability = (await db.execute(text("SELECT * FROM inference_cost_reservations WHERE job_id=:job"),
            {"job": job_id})).one()
        return tuple(run), tuple(liability)


async def stale_memory_fixture(factory, handle):
    # Historical negative fixture only. The actual accounting owner produced
    # the job and liability; this fixture classifies a retained Memory link.
    # It neither creates protected Original/Current nor claims native validity.
    async with factory.accounting_sessions() as db:
        await db.execute(text("UPDATE workflow_run_states SET job_kind='runtime_service_memory_v1', "
            "status='running',lease_expires_at=:expired WHERE run_identity=:job"),
            {"expired": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10),
             "job": handle.job_id})


@pytest.mark.asyncio
@pytest.mark.parametrize("liability_state", ["reserved", "contact_started", "unknown"])
@pytest.mark.parametrize("targeted", [False, True])
async def test_actual_memory_linked_liability_is_never_fetched_or_mutated(
        accounting_db, monkeypatch, liability_state, targeted):
    _root, _engine, factory = accounting_db
    setup_configuration()
    repository = DurableJobRepository()
    await repository.configure_inference_accounting(1000)
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    handle = await broker._prepare_accounting(request("protected-memory"))
    if liability_state != "reserved":
        # Original accounting intent only; no provider callback is executed.
        await broker._contact_accounting(handle)
    if liability_state == "unknown":
        await broker._finish_accounting(handle, reason="negative_fixture_no_transport")
    await stale_memory_fixture(factory, handle)
    before = await raw_rows(factory, handle.job_id)
    frames = []
    original_init = HeaderReadBudget.__init__
    def init(frame):
        original_init(frame)
        frames.append(frame)
    monkeypatch.setattr(HeaderReadBudget, "__init__", init)
    async def forbidden_fetch(*args, **kwargs):
        raise AssertionError("Memory WRS body must not enter accounting recovery")
    monkeypatch.setattr(repository, "_fetch", forbidden_fetch)
    assert await repository.recover_inference_accounting(
        **({"job_id": handle.job_id} if targeted else {})) == []
    assert len(frames) == 1 and frames[0].remaining < 1048576
    assert await raw_rows(factory, handle.job_id) == before


@pytest.mark.asyncio
async def test_targeted_nonmemory_recovery_uses_global_memory_frame_and_preserves_memory(accounting_db, monkeypatch):
    _root, _engine, factory = accounting_db
    setup_configuration()
    repository = DurableJobRepository()
    await repository.configure_inference_accounting(1000)
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    memory = await broker._prepare_accounting(request("protected-memory"))
    ordinary = await broker._prepare_accounting(request("ordinary-reservation"))
    await stale_memory_fixture(factory, memory)
    async with factory.accounting_sessions() as db:
        await db.execute(text("UPDATE workflow_run_states SET lease_expires_at=:expired WHERE run_identity=:job"),
            {"expired": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10),
             "job": ordinary.job_id})
    protected = await raw_rows(factory, memory.job_id)
    frames = []
    original_init = HeaderReadBudget.__init__
    def init(frame):
        original_init(frame)
        frames.append(frame)
    monkeypatch.setattr(HeaderReadBudget, "__init__", init)
    result = await repository.recover_inference_accounting(job_id=ordinary.job_id)
    assert result == [{"job_id": ordinary.job_id, "operation_id": ordinary.request.operation_id,
        "status": "blocked", "reason": "never_contacted_callback_unavailable"}]
    assert len(frames) == 1
    assert await raw_rows(factory, memory.job_id) == protected
    run, liability = await raw_rows(factory, ordinary.job_id)
    async with factory.accounting_sessions() as db:
        assert (await db.execute(text("SELECT state FROM inference_cost_reservations WHERE job_id=:job"),
            {"job": ordinary.job_id})).scalar_one() == "released"


@pytest.mark.asyncio
async def test_memory_appearing_after_scalar_route_denies_before_accounting_body(accounting_db, monkeypatch):
    _root, _engine, factory = accounting_db
    setup_configuration()
    repository = DurableJobRepository()
    await repository.configure_inference_accounting(1000)
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    handle = await broker._prepare_accounting(request("racing-memory"))
    before_begin = repository._accounting_begin
    async def begin(db, **kwargs):
        assert kwargs["header_budget"] is None
        # Independent real committed writer between entry classification and
        # this original BEGIN; no fake classifier or header certificate.
        await stale_memory_fixture(factory, handle)
        await before_begin(db, **kwargs)
    monkeypatch.setattr(repository, "_accounting_begin", begin)
    async def forbidden_bodies(*args, **kwargs):
        raise AssertionError("route change must deny before canonical bodies")
    monkeypatch.setattr(repository, "_accounting_rows", forbidden_bodies)
    with pytest.raises(InferenceAccountingError, match="accounting_recovery_memory_route_changed"):
        await repository.recover_inference_accounting()
    before = await raw_rows(factory, handle.job_id)
    assert await raw_rows(factory, handle.job_id) == before


@pytest.mark.asyncio
async def test_original_nonmemory_accounting_recovery_keeps_its_existing_path(accounting_db, monkeypatch):
    _root, _engine, factory = accounting_db
    setup_configuration()
    repository = DurableJobRepository()
    await repository.configure_inference_accounting(1000)
    handle = await RemoteInferenceAdmissionBroker(durable_accounting=True)._prepare_accounting(request("ordinary-only"))
    async with factory.accounting_sessions() as db:
        await db.execute(text("UPDATE workflow_run_states SET lease_expires_at=:expired WHERE run_identity=:job"),
            {"expired": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10),
             "job": handle.job_id})
    def unexpected_frame(*args, **kwargs):
        raise AssertionError("ordinary recovery must not receive a native maintenance bound")
    monkeypatch.setattr(HeaderReadBudget, "__init__", unexpected_frame)
    assert await repository.recover_inference_accounting() == [{
        "job_id": handle.job_id, "operation_id": handle.request.operation_id,
        "status": "blocked", "reason": "never_contacted_callback_unavailable"}]


@pytest.mark.asyncio
async def test_memory_recovery_schema_denies_before_accounting_bodies_or_writes(accounting_db, monkeypatch):
    _root, _engine, factory = accounting_db
    setup_configuration()
    repository = DurableJobRepository()
    await repository.configure_inference_accounting(1000)
    handle = await RemoteInferenceAdmissionBroker(durable_accounting=True)._prepare_accounting(request("schema-memory"))
    await stale_memory_fixture(factory, handle)
    before = await raw_rows(factory, handle.job_id)
    async with factory.accounting_sessions() as db:
        await db.execute(text("CREATE TABLE unsupported_memory_recovery_schema(value TEXT)"))
    async def forbidden_bodies(*args, **kwargs):
        raise AssertionError("full33 schema failure must precede canonical bodies")
    monkeypatch.setattr(repository, "_accounting_rows", forbidden_bodies)
    with pytest.raises(HeaderBoundsError, match="header_schema_object_unavailable"):
        await repository.recover_inference_accounting()
    assert await raw_rows(factory, handle.job_id) == before
