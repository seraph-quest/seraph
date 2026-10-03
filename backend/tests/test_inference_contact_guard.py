"""Canonical prefunded calls cannot evade a later overrun at contact."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_inference_accounting import accounting_db, request, setup_configuration
from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
from src.workflows.inference_accounting import InferenceAccountingError
from src.workflows.job_runtime import DurableJobRepository
from src.workspace.production import ProductionWorkspace, read_lifecycle_receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["async", "sync", "stream"])
async def test_prefunded_ordinary_broker_denial_survives_finally_reopen_and_recovery(accounting_db, monkeypatch, mode):
    root, engine, factory = accounting_db
    setup_configuration()
    repository = DurableJobRepository()
    await repository.configure_inference_accounting(1000)
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    # Both handles come from the actual canonical broker admission/reservation,
    # before the first settlement. No admission or accounting fixture receipts.
    first = await broker._prepare_accounting(request("first-slot"))
    sibling = await broker._prepare_accounting(request("prefunded-sibling"))
    await broker._contact_accounting(first)
    await broker._finish_accounting(first, payload={"usage": {"cost": "0.000150"}})
    calls = []
    prepare = broker._prepare_accounting
    async def held_handle(value):
        assert value.operation_id == sibling.request.operation_id
        return sibling
    # Replay the already-created canonical handle through the real broker's
    # execution/finally paths; preparation cannot reserve the same row twice.
    monkeypatch.setattr(broker, "_prepare_accounting", held_handle)
    async def callback():
        calls.append("forbidden")
        return {"usage": {"cost": "0.000001"}}
    async def stream():
        calls.append("forbidden")
        yield {"usage": {"cost": "0.000001"}}
    with pytest.raises(InferenceAccountingError, match="provider_charge_exceeded_reservation"):
        if mode == "sync":
            await asyncio.to_thread(broker.execute_sync, sibling.request, lambda: calls.append("forbidden"))
        elif mode == "stream":
            async for _ in broker.stream(sibling.request, stream):
                pass
        else:
            await broker.execute(sibling.request, callback)
    monkeypatch.setattr(broker, "_prepare_accounting", prepare)
    assert calls == []
    assert broker._active_operation_id is None
    await engine.dispose()
    snapshot = await repository.inference_accounting_snapshot()
    assert snapshot["accounting_continuity_verified"] is True
    assert snapshot["committed_microusd"] == 150
    assert snapshot["reserved_microusd"] == 100
    row = next(item for item in snapshot["operations"] if item["operation_id"] == "prefunded-sibling")
    assert row["state"] == "reserved" and row["contact_started_at"] is None
    assert row["recovery_reason"] == "provider_contact_denied"
    assert row["bound_microusd"] == 100 and row["payload_digest"] == sibling.request.data_digest
    denial = next(item for item in json.loads(row["evidence_json"]) if item["kind"] == "provider_contact_denied")
    assert denial["reason"] == "provider_charge_exceeded_reservation"
    assert read_lifecycle_receipt(ProductionWorkspace(host_root=root))["inference_accounting"]["revision"] == snapshot["revision"]
    assert (await repository.get_job(sibling.job_id))["status"] == "blocked"
    # Actual stale-running recovery must also retain this held canonical row.
    from sqlalchemy import text
    async with factory.accounting_sessions() as db:
        await db.execute(text("UPDATE workflow_run_states SET status='running', lease_expires_at=:expired WHERE run_identity=:job"),
            {"expired": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10), "job": sibling.job_id})
    recovered = await repository.recover_inference_accounting()
    assert recovered == [{"job_id": sibling.job_id, "operation_id": "prefunded-sibling", "status": "blocked", "reason": "provider_contact_denied"}]
    after = await repository.inference_accounting_snapshot()
    assert after["accounting_continuity_verified"] is True
    assert after["reserved_microusd"] == 100 and after["committed_microusd"] == 150
    assert next(item for item in after["operations"] if item["operation_id"] == "prefunded-sibling") == row


@pytest.mark.asyncio
async def test_normal_contact_still_settles_under_current_policy(accounting_db):
    setup_configuration()
    repository = DurableJobRepository()
    await repository.configure_inference_accounting(1000)
    calls = []
    async def callback():
        calls.append("contact")
        return {"usage": {"cost": "0.000002"}}
    await RemoteInferenceAdmissionBroker(durable_accounting=True).execute(request("normal-contact"), callback)
    snapshot = await repository.inference_accounting_snapshot()
    assert calls == ["contact"]
    assert snapshot["accounting_continuity_verified"] is True
    assert snapshot["committed_microusd"] == 2 and snapshot["reserved_microusd"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("lookalike", [False, True])
async def test_contacted_or_forged_denial_preserves_unknown_lane_and_debt(accounting_db, lookalike):
    from src.workflows.inference_accounting import InferenceProviderContactDenied
    from src.model_fabric.gpu_admission import GpuAdmissionUncertainError
    setup_configuration()
    repository = DurableJobRepository()
    await repository.configure_inference_accounting(1000)
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    calls = []
    async def callback():
        calls.append("actual callback started")
        error = InferenceProviderContactDenied("provider_charge_exceeded_reservation") if lookalike else RuntimeError("provider_contact_denied")
        raise error
    with pytest.raises(GpuAdmissionUncertainError):
        await broker.execute(request("forged-denial"), callback)
    snapshot = await repository.inference_accounting_snapshot()
    assert calls == ["actual callback started"]
    row = snapshot["operations"][0]
    assert row["state"] == "unknown" and row["contact_started_at"] is not None
    assert snapshot["unknown_microusd"] == 100
    assert broker._active_operation_id == "forged-denial"
