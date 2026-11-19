"""Focused proof for the OpenRouter-only remote admission contract."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from src.model_fabric.remote_inference_admission import (
    REMOTE_INFERENCE_ADMISSION_SCHEMA_VERSION,
    REMOTE_INFERENCE_OWNER_REVOCATION_REASON,
    RemoteInferenceAdmissionBroker,
    RemoteInferenceAdmissionCancelledError,
    RemoteInferenceAdmissionExpiredError,
    RemoteInferenceAdmissionOwnerCapacityError,
    RemoteInferenceAdmissionOwnerRevokedError,
    RemoteInferenceAdmissionRequest,
    RemoteInferenceAdmissionUncertainError,
    RemoteInferencePriority,
)
from src.model_fabric.gpu_admission import GpuAdmissionLeaseError


class _Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _request(
    operation_id: str,
    *,
    owner_id: str = "owner-a",
    priority: RemoteInferencePriority = RemoteInferencePriority.REPORTS_RESEARCH_MEMORY,
    deadline_at: float = 1_000.0,
) -> RemoteInferenceAdmissionRequest:
    return RemoteInferenceAdmissionRequest(
        operation_id=operation_id,
        job_id=f"job-{operation_id}",
        owner_id=owner_id,
        priority=priority,
        deadline_at=deadline_at,
        runtime_path="openrouter_text",
        session_id="session-a",
        capability_version="text.v1",
    )


@pytest.mark.asyncio
async def test_remote_defaults_are_serial_and_bounded_without_gpu_metadata():
    broker = RemoteInferenceAdmissionBroker(clock=_Clock())

    status = await broker.status()

    assert status["schema_version"] == REMOTE_INFERENCE_ADMISSION_SCHEMA_VERSION
    assert status["resource_class"] == "remote_inference"
    assert status["serial_gpu"] is False
    assert status["serial_remote_inference"] is True
    assert status["max_active"] == 1
    assert status["capacity"]["max_queued"] == 64
    assert status["capacity"]["max_outstanding_per_owner"] == 16


@pytest.mark.asyncio
async def test_remote_priority_and_fifo_order_are_preserved():
    broker = RemoteInferenceAdmissionBroker(clock=_Clock())
    requests = (
        _request("low-1"),
        _request("low-2"),
        _request("goal", priority=RemoteInferencePriority.ACCEPTED_SCHEDULED_GOAL),
        _request("chat", priority=RemoteInferencePriority.INTERACTIVE_CHAT),
    )
    for request in requests:
        await broker.enqueue(request)

    order: list[str] = []
    for operation_id in ("chat", "goal", "low-1", "low-2"):
        lease = await broker.acquire(operation_id)
        order.append(lease.operation_id)
        await broker.release(lease)

    assert order == ["chat", "goal", "low-1", "low-2"]


@pytest.mark.asyncio
async def test_owner_revocation_is_scoped_and_cancels_queued_work_before_dispatch():
    broker = RemoteInferenceAdmissionBroker(clock=_Clock())
    active_request = _request(
        "owner-b-active",
        owner_id="owner-b",
        priority=RemoteInferencePriority.REPORTS_RESEARCH_MEMORY,
    )
    owner_a_low = _request(
        "owner-a-low",
        owner_id="owner-a",
        priority=RemoteInferencePriority.REPORTS_RESEARCH_MEMORY,
    )
    owner_a_high = _request(
        "owner-a-high",
        owner_id="owner-a",
        priority=RemoteInferencePriority.INTERACTIVE_CHAT,
    )
    owner_b_follow_up = _request(
        "owner-b-follow-up",
        owner_id="owner-b",
        priority=RemoteInferencePriority.INTERACTIVE_CHAT,
    )
    active_started = asyncio.Event()
    release_active = asyncio.Event()
    provider_calls: list[str] = []

    async def active_provider():
        provider_calls.append(active_request.operation_id)
        active_started.set()
        await release_active.wait()
        return "active"

    async def follow_up_provider():
        provider_calls.append(owner_b_follow_up.operation_id)
        return "follow-up"

    active_task = asyncio.create_task(broker.execute(active_request, active_provider))
    await active_started.wait()
    await broker.enqueue(owner_a_low)
    await broker.enqueue(owner_a_high)
    follow_up_task = asyncio.create_task(broker.execute(owner_b_follow_up, follow_up_provider))
    for _ in range(10):
        if [item["operation_id"] for item in (await broker.status())["queued"]] == [
            owner_b_follow_up.operation_id,
            owner_a_high.operation_id,
            owner_a_low.operation_id,
        ]:
            break
        await asyncio.sleep(0)

    revoked = await broker.cancel_owner("owner-a")

    assert [receipt.operation_id for receipt in revoked] == [
        owner_a_low.operation_id,
        owner_a_high.operation_id,
    ]
    assert all(receipt.status == "cancelled" for receipt in revoked)
    assert all(receipt.reason_code == "owner_revoked" for receipt in revoked)
    assert provider_calls == [active_request.operation_id]
    status = await broker.status()
    assert status["active"]["operation_id"] == active_request.operation_id
    assert status["active"]["cancel_requested"] is False
    assert [item["operation_id"] for item in status["queued"]] == [
        owner_b_follow_up.operation_id,
    ]

    # Repeating the same owner revocation is idempotent and cannot affect the
    # other owner's queued operation or its priority ordering.
    repeated = await broker.cancel_owner("owner-a")
    assert [receipt.operation_id for receipt in repeated] == [
        owner_a_low.operation_id,
        owner_a_high.operation_id,
    ]
    assert all(receipt.status == "cancelled" for receipt in repeated)
    assert [item["operation_id"] for item in (await broker.status())["queued"]] == [
        owner_b_follow_up.operation_id,
    ]

    release_active.set()
    assert await active_task == "active"
    assert await follow_up_task == "follow-up"
    assert provider_calls == [active_request.operation_id, owner_b_follow_up.operation_id]


@pytest.mark.asyncio
async def test_owner_revocation_holds_the_lane_only_while_the_callback_runs():
    broker = RemoteInferenceAdmissionBroker(clock=_Clock())
    recovery_token = broker.register_recovery_authority("recovery-service")
    request = _request("owner-revoked-active")
    callback_started = asyncio.Event()
    callback_cancelled = asyncio.Event()
    follow_up_started = False

    async def provider():
        callback_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            callback_cancelled.set()
            raise

    async def follow_up_provider():
        nonlocal follow_up_started
        follow_up_started = True
        return "follow-up"

    task = asyncio.create_task(broker.execute(request, provider))
    await callback_started.wait()

    with pytest.raises(RuntimeError, match="bootstrap-only"):
        broker.register_recovery_authority("late-attacker")

    requested = await broker.cancel_owner(request.owner_id)
    assert len(requested) == 1
    assert requested[0].status == "running"
    assert requested[0].cancel_requested is True
    assert requested[0].reason_code == "owner_revoked"

    # A repeated revocation reports the same in-flight operation without
    # releasing its lease or changing its fencing identity.
    repeated = await broker.cancel_owner(request.owner_id)
    assert repeated[0].status == "running"
    assert repeated[0].fencing_token == requested[0].fencing_token

    follow_up_task = asyncio.create_task(
        broker.execute(_request("after-owner-revocation", owner_id="owner-b"), follow_up_provider)
    )
    await asyncio.sleep(0)
    assert follow_up_started is False

    with pytest.raises(RemoteInferenceAdmissionUncertainError) as error:
        await task
    assert callback_cancelled.is_set()
    assert error.value.receipt.status == "blocked"
    assert error.value.receipt.reason_code == "owner_revoked"
    # ADR-037 (#1284): the returned callback frees the lane; no charge has to
    # be supplied first. The revoked owner's result stays uncertain (blocked)
    # and adopts nothing, and the other owner's queued work now runs.
    assert error.value.receipt.reconciliation_required is False
    assert error.value.receipt.callback_completed is True
    assert await follow_up_task == "follow-up"
    assert follow_up_started is True

    blocked_repeat = await broker.cancel_owner(request.owner_id)
    assert [item.status for item in blocked_repeat] == ["blocked"]
    assert blocked_repeat[0].reconciliation_required is False
    with pytest.raises(GpuAdmissionLeaseError):
        await broker.reconcile(
            request.operation_id,
            owner_id=request.owner_id,
            job_id=request.job_id,
            fencing_token=error.value.receipt.fencing_token or 0,
            recovery_authority_token=recovery_token,
            outcome="cancelled",
            reason_code="owner_revoked_reconciled",
        )


@pytest.mark.asyncio
async def test_owner_revocation_rejects_empty_scope_without_touching_other_owners():
    broker = RemoteInferenceAdmissionBroker(clock=_Clock())
    other = _request("other-owner-queued", owner_id="owner-b")
    await broker.enqueue(other)

    assert await broker.cancel_owner("owner-a") == ()

    with pytest.raises(ValueError, match="owner_id is required"):
        await broker.cancel_owner(" ")

    status = await broker.status()
    assert [item["operation_id"] for item in status["queued"]] == [other.operation_id]


@pytest.mark.asyncio
async def test_revoked_owner_cannot_reenter_and_other_owner_still_runs():
    broker = RemoteInferenceAdmissionBroker(clock=_Clock())
    await broker.cancel_owner("owner-a")
    revoked_request = _request("revoked-owner-reentry", owner_id="owner-a")
    revoked_provider_called = False

    async def revoked_provider():
        nonlocal revoked_provider_called
        revoked_provider_called = True

    with pytest.raises(RemoteInferenceAdmissionOwnerRevokedError) as rejected:
        await broker.execute(revoked_request, revoked_provider)
    assert rejected.value.code == REMOTE_INFERENCE_OWNER_REVOCATION_REASON
    assert rejected.value.receipt.status == "rejected"
    assert rejected.value.receipt.reason_code == REMOTE_INFERENCE_OWNER_REVOCATION_REASON
    assert revoked_provider_called is False

    # The terminal receipt remains idempotent, and acquire reports the same
    # stable owner-revoked denial if a caller retries the operation identity.
    terminal = await broker.enqueue(revoked_request)
    assert terminal.status == "rejected"
    with pytest.raises(RemoteInferenceAdmissionOwnerRevokedError) as acquire_error:
        await broker.acquire(revoked_request.operation_id)
    assert acquire_error.value.receipt.reason_code == REMOTE_INFERENCE_OWNER_REVOCATION_REASON

    other_request = _request("owner-b-after-revocation", owner_id="owner-b")
    other_called = False

    async def other_provider():
        nonlocal other_called
        other_called = True
        return "owner-b-result"

    assert await broker.execute(other_request, other_provider) == "owner-b-result"
    assert other_called is True


@pytest.mark.asyncio
async def test_owner_revocation_reason_is_strictly_allowlisted_and_redacted():
    broker = RemoteInferenceAdmissionBroker(clock=_Clock())
    secret_like_reason = "provider_key=sk-live-secret"

    with pytest.raises(ValueError, match="reason_code must be owner_revoked") as error:
        await broker.cancel_owner("owner-a", reason_code=secret_like_reason)
    assert secret_like_reason not in str(error.value)

    status = await broker.status()
    assert status["degraded"] is False
    assert secret_like_reason not in str(status)

    # The rejected custom reason did not create a tombstone or alter the
    # admission lane; the owner can still be revoked with the safe token.
    receipts = await broker.cancel_owner("owner-a")
    assert receipts == ()
    assert receipts == await broker.cancel_owner("owner-a")


@pytest.mark.asyncio
async def test_owner_outstanding_cap_rejects_before_provider_and_is_owner_scoped():
    broker = RemoteInferenceAdmissionBroker(clock=_Clock(), max_outstanding_per_owner=1)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    provider_calls: list[str] = []

    async def first() -> None:
        provider_calls.append("first")
        first_entered.set()
        await release_first.wait()

    first_task = asyncio.create_task(broker.execute(_request("first"), first))
    await first_entered.wait()

    async def rejected() -> None:
        provider_calls.append("rejected")

    with pytest.raises(RemoteInferenceAdmissionOwnerCapacityError) as error:
        await broker.execute(_request("same-owner"), rejected)
    assert error.value.receipt.reason_code == "owner_capacity_exhausted"
    assert provider_calls == ["first"]

    other_owner = await broker.enqueue(_request("other-owner", owner_id="owner-b"))
    assert other_owner.status == "queued"
    await broker.cancel(other_owner.operation_id, owner_id="owner-b")

    release_first.set()
    await first_task


@pytest.mark.asyncio
async def test_no_owner_spend_budget_or_cost_estimate_gates_admission():
    """ADR-037 (#1284): only the owner's outstanding count is bounded."""
    broker = RemoteInferenceAdmissionBroker(clock=_Clock(), max_outstanding_per_owner=2)
    await broker.enqueue(_request("no-cost-1"))
    await broker.enqueue(_request("no-cost-2"))
    with pytest.raises(RemoteInferenceAdmissionOwnerCapacityError):
        await broker.enqueue(_request("no-cost-3"))
    status = await broker.status()
    assert "max_owner_cost_microusd" not in status["capacity"]
    assert status["capacity"]["owners"]["owner-a"] == {"outstanding": 2}


@pytest.mark.asyncio
async def test_late_remote_result_is_uncertain_but_frees_the_lane_without_a_charge():
    clock = _Clock()
    broker = RemoteInferenceAdmissionBroker(clock=clock)
    request = _request("late", deadline_at=110.0)
    await broker.enqueue(request)
    lease = await broker.acquire(request.operation_id)

    clock.advance(20)
    released = await broker.release(lease)
    # Still reported as an uncertain outcome, but nothing is held (#1284).
    assert released.status == "blocked"
    assert released.reason_code == "deadline_expired_after_callback"
    assert released.reconciliation_required is False
    status = await broker.status()
    assert status["active"] is None
    assert status["capacity"]["owners"] == {}
    follow_up = _request("after-late", deadline_at=1_000.0)
    await broker.enqueue(follow_up)
    assert (await broker.acquire(follow_up.operation_id)).operation_id == follow_up.operation_id


@pytest.mark.asyncio
async def test_remote_callback_failure_is_uncertain_and_the_next_request_runs():
    broker = RemoteInferenceAdmissionBroker(clock=_Clock())

    async def provider() -> None:
        raise RuntimeError("transport lost after dispatch")

    request = _request("transport-lost")
    with pytest.raises(RemoteInferenceAdmissionUncertainError) as error:
        await broker.execute(request, provider)
    assert error.value.receipt.status == "blocked"
    assert error.value.receipt.reconciliation_required is False

    async def next_provider():
        return "next"

    assert await broker.execute(_request("after-transport-lost"), next_provider) == "next"


@pytest.mark.asyncio
async def test_stale_lease_identity_still_cannot_release_an_operation():
    broker = RemoteInferenceAdmissionBroker(clock=_Clock())
    request = _request("lease-job-fence")
    await broker.enqueue(request)
    lease = await broker.acquire(request.operation_id)

    with pytest.raises(GpuAdmissionLeaseError) as wrong_job:
        await broker.release(replace(lease, job_id="job-from-another-attempt"))
    assert wrong_job.value.receipt.reason_code == "stale_owner_or_fencing_token"
    with pytest.raises(GpuAdmissionLeaseError) as wrong_fence:
        await broker.release(replace(lease, fencing_token=lease.fencing_token + 1))
    assert wrong_fence.value.receipt.reason_code == "stale_owner_or_fencing_token"
    assert (await broker.status())["active"]["operation_id"] == request.operation_id

    released = await broker.release(lease, uncertain=True)
    assert released.status == "blocked"
    assert (await broker.status())["active"] is None
    with pytest.raises(GpuAdmissionLeaseError):
        await broker.release(lease)


@pytest.mark.asyncio
async def test_queued_cancellation_and_expiry_never_dispatch_a_remote_callback():
    clock = _Clock()
    broker = RemoteInferenceAdmissionBroker(clock=clock)

    cancelled = await broker.enqueue(_request("cancel-before-dispatch"))
    await broker.cancel(cancelled.operation_id, owner_id=cancelled.owner_id)
    with pytest.raises(RemoteInferenceAdmissionCancelledError):
        await broker.acquire(cancelled.operation_id)

    expired = _request("expire-before-dispatch", deadline_at=110.0)
    await broker.enqueue(expired)
    clock.advance(10)
    with pytest.raises(RemoteInferenceAdmissionExpiredError):
        await broker.acquire(expired.operation_id)

    status = await broker.status()
    assert status["active"] is None
    assert status["queued"] == []


@pytest.mark.asyncio
async def test_receipt_persistence_is_explicit_and_uses_the_typed_repository_seam():
    broker = RemoteInferenceAdmissionBroker(clock=_Clock())
    receipt = await broker.enqueue(_request("persisted-receipt"))
    recorded: list[dict[str, object]] = []

    class RecordingRepository:
        async def record_remote_inference_receipt(self, payload, *, owner=None, fencing_token=None):
            recorded.append(
                {
                    "payload": payload,
                    "owner": owner,
                    "fencing_token": fencing_token,
                }
            )
            return {"persisted": True, "status": "recorded"}

    result = await broker.persist_receipt(
        receipt,
        repository=RecordingRepository(),
        owner="durable-runner",
        fencing_token=7,
    )

    assert result["persisted"] is True
    assert recorded[0]["payload"]["status"] == "queued"
    assert recorded[0]["owner"] == "durable-runner"
    assert "prompt" not in recorded[0]["payload"]


@pytest.mark.asyncio
async def test_receipts_are_redacted_and_do_not_probe_local_services():
    broker = RemoteInferenceAdmissionBroker(clock=_Clock())
    receipt = await broker.enqueue(_request("redacted"))
    payload = receipt.as_dict()

    assert payload["resource_class"] == "remote_inference"
    assert payload["schema_version"] == REMOTE_INFERENCE_ADMISSION_SCHEMA_VERSION
    assert payload["capability_version"] == "text.v1"
    assert "prompt" not in payload
    assert "messages" not in payload
    assert "api_key" not in payload

    source = Path(__file__).parents[1] / "src/model_fabric/remote_inference_admission.py"
    source_text = source.read_text(encoding="utf-8")
    assert "192.168.1.26" not in source_text
    assert "httpx" not in source_text
    assert "import torch" not in source_text
    assert "import requests" not in source_text
