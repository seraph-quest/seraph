"""Actual SQLite writer ordering and contact-time authority changes."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.test_inference_accounting import accounting_db, request, setup_configuration
from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
from src.workflows.inference_accounting import InferenceAccountingError, InferenceProviderContactDenied
from src.workflows.job_runtime import DurableJobRepository
from src.workspace.production import ProductionWorkspace, read_accounting_checkpoint


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["policy", "period"])
async def test_prefunded_contact_rechecks_actual_current_policy_and_period(accounting_db, monkeypatch, change):
    configuration = setup_configuration()
    repository = DurableJobRepository()
    await repository.configure_inference_accounting(1000)
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    handle = await broker._prepare_accounting(request("current-authority"))
    if change == "policy":
        from dataclasses import replace
        from src.model_fabric.configuration import write_model_fabric_configuration
        write_model_fabric_configuration(replace(configuration,
            egress_revision=configuration.egress_revision+1,
            openrouter_setup=replace(configuration.openrouter_setup, request_cost_bound_microusd=101)))
        expected = "provider_policy_revision_changed"
    else:
        await repository.inference_accounting_snapshot(now=datetime.now(timezone.utc)+timedelta(days=70))
        expected = "accounting_clock_correction_required"
    async def held(_request):
        return handle
    monkeypatch.setattr(broker, "_prepare_accounting", held)
    calls = []
    async def forbidden():
        calls.append("provider")
    with pytest.raises(InferenceProviderContactDenied, match=expected):
        await broker.execute(handle.request, forbidden)
    assert calls == [] and broker._active_operation_id is None
    await accounting_db[1].dispose()
    snapshot = await repository.inference_accounting_snapshot()
    row = snapshot["operations"][0]
    assert snapshot["accounting_continuity_verified"] is True
    assert row["state"] == "reserved" and row["contact_started_at"] is None
    assert row["bound_microusd"] == 100
    assert any(item.get("kind") == "provider_contact_denied" and item["reason"] == expected
        for item in json.loads(row["evidence_json"]))


@pytest.mark.asyncio
async def test_settlement_writer_barrier_precedes_prefunded_sibling_contact(accounting_db, monkeypatch):
    setup_configuration()
    repository = DurableJobRepository()
    await repository.configure_inference_accounting(1000)
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    first = await broker._prepare_accounting(request("writer-first"))
    sibling = await broker._prepare_accounting(request("writer-sibling"))
    await broker._contact_accounting(first)
    entered, release = asyncio.Event(), asyncio.Event()
    persist = repository._persist_accounting_witness
    async def blocked_persist(db, workspace, account, rows):
        if any(row.operation_id == first.request.operation_id and row.state == "settled" for row in rows):
            entered.set()
            await asyncio.wait_for(release.wait(), 3)
        await persist(db, workspace, account, rows)
    # Both real handles refer to the same canonical repository instance here.
    first.repository = sibling.repository = repository
    monkeypatch.setattr(repository, "_persist_accounting_witness", blocked_persist)
    settle = asyncio.create_task(broker._finish_accounting(first, payload={"usage":{"cost":"0.000150"}}))
    contact = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        contact = asyncio.create_task(broker._contact_accounting(sibling))
        await asyncio.sleep(0.05)
        assert not contact.done(), "second writer must wait for actual settlement transaction"
        release.set()
        await asyncio.wait_for(settle, 5)
        with pytest.raises(InferenceProviderContactDenied, match="provider_charge_exceeded_reservation"):
            await asyncio.wait_for(contact, 5)
    finally:
        release.set()
        await asyncio.gather(settle, *([contact] if contact is not None else []), return_exceptions=True)
    snapshot = await repository.inference_accounting_snapshot()
    assert snapshot["accounting_continuity_verified"] is True
    assert snapshot["committed_microusd"] == 150 and snapshot["reserved_microusd"] == 100
    assert next(row for row in snapshot["operations"] if row["operation_id"] == "writer-sibling")["contact_started_at"] is None


@pytest.mark.asyncio
async def test_denial_witness_commit_failure_cannot_issue_completion_authority(accounting_db, monkeypatch):
    root, engine, _factory = accounting_db
    setup_configuration()
    repository = DurableJobRepository()
    await repository.configure_inference_accounting(1000)
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    first = await broker._prepare_accounting(request("crash-first"))
    sibling = await broker._prepare_accounting(request("crash-sibling"))
    await broker._contact_accounting(first)
    await broker._finish_accounting(first, payload={"usage":{"cost":"0.000150"}})
    original = AsyncSession.commit
    fail_once = True
    async def fail_commit(db):
        nonlocal fail_once
        checkpoint = read_accounting_checkpoint(ProductionWorkspace(host_root=root))
        if fail_once and checkpoint and any(row.get("recovery_reason") == "provider_contact_denied" for row in checkpoint["operations"]):
            fail_once = False
            raise RuntimeError("injected denial witness before commit")
        await original(db)
    monkeypatch.setattr(AsyncSession, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="injected denial witness"):
        await broker._contact_accounting(sibling)
    assert fail_once is False and sibling.committed_denial is None and sibling.contacted is False
    await engine.dispose()
    snapshot = await repository.inference_accounting_snapshot()
    assert snapshot["status"] == "blocked" and snapshot["reason_code"] == "accounting_continuity_unavailable"
    with pytest.raises(InferenceAccountingError, match="continuity_unavailable"):
        await repository.settle_inference_cost(sibling.request.operation_id, reason="cancelled_before_contact")
