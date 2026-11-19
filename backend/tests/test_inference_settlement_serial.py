"""Deterministic settlement boundaries against the real file-backed ledger.

Provider callbacks are scripted local functions; no model transport is opened.
The barrier instruments settlement, never admission or canonical ownership.
"""

import asyncio
import json
import threading
import time

import pytest

from tests.test_inference_accounting import accounting_db, request, setup_configuration
from src.model_fabric.gpu_admission import GpuAdmissionLeaseError, GpuAdmissionUncertainError
from src.model_fabric.remote_inference_admission import (
    RemoteInferenceAdmissionBroker,
    RemoteInferenceBindingError,
    RemoteInferencePriority,
)
from src.workflows.job_runtime import DurableJobRepository
from src.workflows.inference_accounting import InferenceAccountingError

# ADR-035: pins historical OpenRouter broker, ledger and settlement mechanics that still settle retained liabilities
pytestmark = pytest.mark.openrouter_legacy


async def bounded(awaitable):
    return await asyncio.wait_for(awaitable, timeout=10)


def retain_readback(accounting_db, name, snapshot):
    root, _engine, _factory = accounting_db
    fields = ("operation_id", "job_id", "state", "actual_cost_microusd", "bound_microusd",
              "recovery_reason", "revision")
    receipt = {key: snapshot.get(key) for key in (
        "status", "accounting_continuity_verified", "spent_microusd", "unpriced_requests")}
    receipt["operations"] = [{key: row.get(key) for key in fields} for row in snapshot["operations"]]
    (root / f"settlement-{name}-readback.json").write_text(json.dumps(receipt, indent=2) + "\n")


def observe_enqueues(broker, monkeypatch, operation_ids):
    """Signal only after the real broker accepted each queued operation."""
    events = {operation_id: asyncio.Event() for operation_id in operation_ids}
    original = broker.enqueue

    async def enqueue(candidate, **kwargs):
        receipt = await original(candidate, **kwargs)
        if candidate.operation_id in events:
            events[candidate.operation_id].set()
        return receipt

    monkeypatch.setattr(broker, "enqueue", enqueue)
    return events


def settlement_barrier(broker, monkeypatch, operation_id):
    entered, release = threading.Event(), threading.Event()
    original = broker._finish_accounting

    async def finish(handle, **kwargs):
        if handle.request.operation_id == operation_id:
            entered.set()
            assert await asyncio.to_thread(release.wait, 10), "settlement barrier was not released"
        return await original(handle, **kwargs)

    monkeypatch.setattr(broker, "_finish_accounting", finish)
    return entered, release


async def run_provider(broker, candidate, kind, contacts, payload):
    if kind == "sync":
        def provider():
            contacts.append(candidate.operation_id)
            return payload
        return await asyncio.to_thread(broker.execute_sync, candidate, provider)
    if kind == "stream":
        async def provider():
            contacts.append(candidate.operation_id)
            yield payload
        return [item async for item in broker.stream(candidate, provider)]

    async def provider():
        contacts.append(candidate.operation_id)
        return payload
    return await broker.execute(candidate, provider)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["async", "sync", "stream"])
async def test_completed_response_holds_serial_lease_until_large_charge_settles(
    accounting_db, monkeypatch, kind,
):
    # #1284: the legacy stored bound (100) is read past; a larger charge
    # settles normally and the queued sibling then proceeds.
    setup_configuration(ceiling=1000, bound=100)
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    contacts = []
    entered, release = settlement_barrier(broker, monkeypatch, "first")
    queued = observe_enqueues(broker, monkeypatch, ["sibling"])
    first = asyncio.create_task(run_provider(broker, request("first"), kind, contacts,
        {"usage": {"cost": "0.000150"}}))
    sibling = None
    try:
        assert await bounded(asyncio.to_thread(entered.wait, 10))
        receipt = broker.receipt_for("first")
        assert receipt.callback_completed is True
        assert receipt.active_operation_id == "first"
        before = await repository.inference_accounting_snapshot()
        assert before["spent_microusd"] == 0
        assert before["operations"][0]["state"] == "contact_started"
        sibling = asyncio.create_task(run_provider(broker, request("sibling"), "async",
            contacts, {"usage": {"cost": "0.000001"}}))
        await bounded(queued["sibling"].wait())
        assert broker.receipt_for("sibling").status == "queued"
        assert broker.receipt_for("sibling").active_operation_id == "first"
        assert contacts == ["first"]
        release.set()
        await bounded(first)
        await bounded(sibling)
        after = await repository.inference_accounting_snapshot()
        rows = {row["operation_id"]: row for row in after["operations"]}
        assert after["spent_microusd"] == 151
        assert rows["first"]["actual_cost_microusd"] == 150
        assert rows["first"]["recovery_reason"] is None
        assert rows["sibling"]["state"] == "settled"
        assert contacts == ["first", "sibling"]
        retain_readback(accounting_db, f"large-charge-{kind}", after)
    finally:
        release.set()
        pending = [task for task in (first, sibling) if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_settlement_release_preserves_priority_and_background_progress(accounting_db, monkeypatch):
    setup_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    contacts = []
    entered, release = settlement_barrier(broker, monkeypatch, "first")
    events = observe_enqueues(broker, monkeypatch, ["background", "interactive"])
    first = asyncio.create_task(run_provider(broker, request("first"), "async", contacts,
        {"usage": {"cost": "0.000001"}}))
    tasks = [first]
    try:
        assert await bounded(asyncio.to_thread(entered.wait, 10))
        for name, priority in (("background", RemoteInferencePriority.SCREENSHOT_BACKGROUND),
                               ("interactive", RemoteInferencePriority.INTERACTIVE_CHAT)):
            tasks.append(asyncio.create_task(run_provider(broker, request(name, priority=priority),
                "async", contacts, {"usage": {"cost": "0.000001"}})))
            await bounded(events[name].wait())
        assert contacts == ["first"]
        assert broker.receipt_for("first").active_operation_id == "first"
        release.set()
        await bounded(asyncio.gather(*tasks))
        assert contacts == ["first", "interactive", "background"]
        snapshot = await repository.inference_accounting_snapshot()
        assert snapshot["spent_microusd"] == 3
        assert all(row["state"] == "settled" for row in snapshot["operations"])
        assert broker.receipt_for("background").active_operation_id is None
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind, failure", [
    (kind, failure) for kind in ("async", "sync", "stream")
    for failure in ("provider_error", "unknown")
] + [("async", "cancel")])
async def test_contacted_failure_is_durable_before_serial_slot_can_change(
    accounting_db, monkeypatch, kind, failure,
):
    setup_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    entered, release = settlement_barrier(broker, monkeypatch, "first")
    contacted = threading.Event()
    events = observe_enqueues(broker, monkeypatch, ["sibling"])
    contacts = []

    async def provider():
        contacts.append("first")
        contacted.set()
        if failure == "cancel":
            await asyncio.Event().wait()
        if failure == "provider_error":
            raise RuntimeError("scripted provider failure")
        return {"usage": {"prompt_tokens": 7}}

    if kind == "sync":
        def sync_provider():
            contacts.append("first")
            contacted.set()
            if failure == "provider_error":
                raise RuntimeError("scripted provider failure")
            return {"usage": {"prompt_tokens": 7}}
        execution = asyncio.to_thread(broker.execute_sync, request("first"), sync_provider)
    elif kind == "stream":
        async def stream_provider():
            yield await provider()
        async def consume():
            return [part async for part in broker.stream(request("first"), stream_provider)]
        execution = consume()
    else:
        execution = broker.execute(request("first"), provider)
    first = asyncio.create_task(execution)
    sibling = None
    try:
        assert await bounded(asyncio.to_thread(contacted.wait, 10))
        if failure == "cancel":
            first.cancel()
        assert await bounded(asyncio.to_thread(entered.wait, 10))
        assert broker.receipt_for("first").callback_completed is True
        assert broker.receipt_for("first").active_operation_id == "first"
        sibling = asyncio.create_task(run_provider(broker, request("sibling"), "async", contacts,
            {"usage": {"cost": "0.000001"}}))
        await bounded(events["sibling"].wait())
        assert contacts == ["first"]
        release.set()
        outcome = (await bounded(asyncio.gather(first, return_exceptions=True)))[0]
        if failure != "unknown":
            assert isinstance(outcome, (RuntimeError, asyncio.CancelledError, GpuAdmissionUncertainError))
        # #1284: the returned callback's uncertain outcome is durable as an
        # unpriced row before the lane is freed; the sibling then proceeds.
        await bounded(sibling)
        snapshot = await repository.inference_accounting_snapshot()
        first_row = next(row for row in snapshot["operations"] if row["operation_id"] == "first")
        assert first_row["state"] == "unknown"
        assert snapshot["unpriced_requests"] == 1
        sibling_row = next(row for row in snapshot["operations"] if row["operation_id"] == "sibling")
        assert sibling_row["state"] == "settled"
        assert sibling_row["actual_cost_microusd"] == 1
        assert contacts == ["first", "sibling"]
        if failure != "unknown":
            receipt = broker.receipt_for("first")
            assert receipt.status == "blocked"
            assert receipt.reconciliation_required is False
    finally:
        release.set()
        pending = [task for task in (first, sibling) if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["settlement", "readback"])
@pytest.mark.parametrize("kind", ["async", "sync", "stream"])
async def test_failed_settlement_or_readback_frees_the_lane_and_never_replays_the_contact(
    accounting_db, monkeypatch, failure, kind,
):
    setup_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    contacts = []
    original_snapshot = DurableJobRepository.inference_accounting_snapshot
    if failure == "settlement":
        original_settle = DurableJobRepository.settle_inference_cost

        async def fail_settlement(self, operation_id, **kwargs):
            if operation_id == "first":
                raise RuntimeError("scripted settlement storage failure")
            return await original_settle(self, operation_id, **kwargs)
        monkeypatch.setattr(DurableJobRepository, "settle_inference_cost", fail_settlement)
    else:
        async def fail_readback(self, **kwargs):
            snapshot = await original_snapshot(self, **kwargs)
            if kwargs.get("job_id") and any(row["operation_id"] == "first" and row["state"] == "settled"
                                            for row in snapshot.get("operations", [])):
                return {**snapshot, "accounting_continuity_verified": False}
            return snapshot
        monkeypatch.setattr(DurableJobRepository, "inference_accounting_snapshot", fail_readback)
    with pytest.raises(GpuAdmissionUncertainError):
        await bounded(run_provider(broker, request("first"), kind, contacts,
            {"usage": {"cost": "0.000007"}}))
    # ADR-037 (#1284): neither a failed settlement write nor a failed
    # readback of a committed settlement holds the single lane. The request
    # record (``contact_started``, or ``settled`` when only the readback
    # failed) keeps the operation from being reserved or contacted again.
    receipt = broker.receipt_for("first")
    assert receipt.callback_completed is True
    assert receipt.status == "blocked"
    assert receipt.reason_code == "accounting_settlement_unavailable"
    assert receipt.reconciliation_required is False
    assert receipt.active_operation_id is None
    persisted = await original_snapshot(repository)
    row = persisted["operations"][0]
    assert row["state"] == ("contact_started" if failure == "settlement" else "settled")
    if failure == "readback":
        assert row["actual_cost_microusd"] == 7
    retain_readback(accounting_db, f"{failure}-{kind}", persisted)
    # The freed lane admits the next request.
    await bounded(run_provider(broker, request("sibling"), "async", contacts,
        {"usage": {"cost": "0.000001"}}))
    assert contacts == ["first", "sibling"]
    # The failed operation is never contacted a second time.
    with pytest.raises(RemoteInferenceBindingError, match="accounting_admission_blocked"):
        await bounded(run_provider(broker, request("first"), kind, contacts,
            {"usage": {"cost": "0.000007"}}))
    assert contacts == ["first", "sibling"]
    final = {item["operation_id"]: item for item in (await original_snapshot(repository))["operations"]}
    assert final["first"]["state"] == row["state"] and final["first"]["revision"] == row["revision"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sync", "stream"])
async def test_broker_cancel_during_settlement_keeps_real_callback_and_charge(accounting_db, monkeypatch, kind):
    setup_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    entered, release = settlement_barrier(broker, monkeypatch, "first")
    contacts = []
    first = asyncio.create_task(run_provider(broker, request("first"), kind, contacts,
        {"usage": {"cost": "0.000007"}}))
    try:
        assert await bounded(asyncio.to_thread(entered.wait, 10))
        receipt = broker.receipt_for("first")
        assert receipt.callback_completed is True
        cancelled = await broker.cancel("first", owner_id=receipt.owner_id, fencing_token=receipt.fencing_token)
        assert cancelled.cancel_requested is True
        assert cancelled.active_operation_id == "first"
        assert not first.done()
        before = await repository.inference_accounting_snapshot()
        assert before["spent_microusd"] == 0
        release.set()
        outcome = (await bounded(asyncio.gather(first, return_exceptions=True)))[0]
        assert isinstance(outcome, (asyncio.CancelledError, GpuAdmissionUncertainError))
        after = await repository.inference_accounting_snapshot()
        assert after["spent_microusd"] == 7
        assert after["operations"][0]["state"] == "settled"
        assert contacts == ["first"]
        # #1284: the returned callback no longer holds the lane.
        assert broker.receipt_for("first").active_operation_id is None
    finally:
        release.set()
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_exact_settlement_before_return(
    accounting_db, monkeypatch,
):
    setup_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    entered, release = settlement_barrier(broker, monkeypatch, "first")
    contacts = []
    first = asyncio.create_task(run_provider(broker, request("first"), "async", contacts,
        {"usage": {"cost": "0.000007"}}))
    try:
        assert await bounded(asyncio.to_thread(entered.wait, 10))
        for _ in range(2):
            first.cancel()
            # A ready-loop marker proves the cancellation was delivered without
            # relying on a wall-clock delay or allowing settlement to proceed.
            marker = asyncio.Event()
            asyncio.get_running_loop().call_soon(marker.set)
            await bounded(marker.wait())
            assert not first.done()
            assert broker.receipt_for("first").callback_completed is True
            assert broker.receipt_for("first").active_operation_id == "first"
            snapshot = await repository.inference_accounting_snapshot()
            assert snapshot["spent_microusd"] == 0
            assert snapshot["operations"][0]["state"] == "contact_started"
        release.set()
        outcome = (await bounded(asyncio.gather(first, return_exceptions=True)))[0]
        assert isinstance(outcome, (asyncio.CancelledError, GpuAdmissionUncertainError))
        after = await repository.inference_accounting_snapshot()
        assert after["spent_microusd"] == 7
        assert after["operations"][0]["state"] == "settled"
        assert contacts == ["first"]
    finally:
        release.set()
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_original_deadline_is_not_renewed_while_settlement_is_held(accounting_db, monkeypatch):
    setup_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    now = [time.time()]
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True, clock=lambda: now[0])
    candidate = request("first")
    entered, release = settlement_barrier(broker, monkeypatch, "first")
    contacts = []
    first = asyncio.create_task(run_provider(broker, candidate, "async", contacts,
        {"usage": {"cost": "0.000007"}}))
    try:
        assert await bounded(asyncio.to_thread(entered.wait, 10))
        with broker._condition:
            assert broker._operations["first"].request.deadline_at == candidate.deadline_at
        now[0] = candidate.deadline_at + 1
        assert broker.receipt_for("first").active_operation_id == "first"
        release.set()
        with pytest.raises(GpuAdmissionUncertainError):
            await bounded(first)
        receipt = broker.receipt_for("first")
        with broker._condition:
            assert broker._operations["first"].request.deadline_at == candidate.deadline_at
        # #1284: the deadline passed after the callback returned; the caller
        # still adopts nothing, but the lane is freed without reconciliation.
        assert receipt.active_operation_id is None
        assert receipt.reconciliation_required is False
        snapshot = await repository.inference_accounting_snapshot()
        assert snapshot["spent_microusd"] == 7
        assert snapshot["operations"][0]["state"] == "settled"
        assert contacts == ["first"]
    finally:
        release.set()
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_lost_response_frees_lane_and_manual_charge_record_never_retries(accounting_db):
    setup_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    contacts = []

    async def provider():
        contacts.append("first")
        raise RuntimeError("scripted lost provider response")

    with pytest.raises(GpuAdmissionUncertainError):
        await bounded(broker.execute(request("first"), provider))
    snapshot = await repository.inference_accounting_snapshot()
    row = snapshot["operations"][0]
    assert row["state"] == "unknown"
    receipt = broker.receipt_for("first")
    # #1284: the returned lost response frees the lane at once; there is
    # nothing left to reconcile on the broker.
    assert receipt.status == "blocked" and receipt.reconciliation_required is False
    assert receipt.active_operation_id is None
    with pytest.raises(GpuAdmissionLeaseError):
        await broker.reconcile("first", owner_id=receipt.owner_id, job_id=receipt.job_id,
            fencing_token=receipt.fencing_token)
    await repository.settle_inference_cost(row["operation_id"], job_id=row["job_id"],
        expected_revision=row["revision"], actual_cost_microusd=7,
        evidence_digest="b" * 64, operator_id="operator:settings-owner", idempotency_key="recovery-first")
    settled = await repository.inference_accounting_snapshot()
    assert settled["spent_microusd"] == 7
    assert settled["unpriced_requests"] == 0
    await bounded(run_provider(broker, request("sibling"), "async", contacts,
        {"usage": {"cost": "0.000001"}}))
    assert contacts == ["first", "sibling"]
    assert (await repository.inference_accounting_snapshot())["spent_microusd"] == 8
    retain_readback(accounting_db, "recovery", await repository.inference_accounting_snapshot())
    with pytest.raises((ValueError, InferenceAccountingError)):
        await broker.execute(request("first"), provider)
    assert contacts == ["first", "sibling"]
