"""Focused proof for the OpenRouter-only remote admission contract."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.model_fabric.remote_inference_admission import (
    REMOTE_INFERENCE_ADMISSION_SCHEMA_VERSION,
    REMOTE_INFERENCE_OWNER_COST_UNKNOWN_REASON,
    RemoteInferenceAdmissionBroker,
    RemoteInferenceAdmissionOwnerBudgetError,
    RemoteInferenceAdmissionOwnerCapacityError,
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
    estimated_cost_microusd: int | None = None,
    owner_budget_microusd: int | None = None,
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
        estimated_cost_microusd=estimated_cost_microusd,
        owner_budget_microusd=owner_budget_microusd,
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
async def test_budget_reservation_and_unknown_cost_fail_closed_before_dispatch():
    broker = RemoteInferenceAdmissionBroker(clock=_Clock(), max_owner_cost_microusd=100)
    first = _request("cost-1", estimated_cost_microusd=60)
    await broker.enqueue(first)

    with pytest.raises(RemoteInferenceAdmissionOwnerBudgetError) as error:
        await broker.enqueue(_request("cost-2", estimated_cost_microusd=50))
    assert error.value.receipt.reason_code == "owner_budget_exhausted"

    with pytest.raises(RemoteInferenceAdmissionOwnerBudgetError) as unknown:
        await broker.enqueue(_request("unknown-cost"))
    assert unknown.value.receipt.reason_code == REMOTE_INFERENCE_OWNER_COST_UNKNOWN_REASON

    cancelled = await broker.cancel(first.operation_id, owner_id=first.owner_id)
    assert cancelled.status == "cancelled"


@pytest.mark.asyncio
async def test_server_budget_resolver_is_authoritative_over_request_metadata():
    broker = RemoteInferenceAdmissionBroker(
        clock=_Clock(),
        owner_budget_resolver=lambda owner_id: 100 if owner_id == "owner-a" else 25,
    )

    with pytest.raises(RemoteInferenceAdmissionOwnerBudgetError):
        await broker.enqueue(
            _request(
                "forged-wider-budget",
                estimated_cost_microusd=101,
                owner_budget_microusd=10_000,
            )
        )

    with pytest.raises(RemoteInferenceAdmissionOwnerBudgetError):
        await broker.enqueue(_request("stricter-request-budget", estimated_cost_microusd=30, owner_budget_microusd=25))


@pytest.mark.asyncio
async def test_uncertain_remote_result_retains_cost_until_fenced_reconciliation():
    clock = _Clock()
    broker = RemoteInferenceAdmissionBroker(clock=clock, max_owner_cost_microusd=100)
    request = _request("late", deadline_at=110.0, estimated_cost_microusd=80)
    await broker.enqueue(request)
    lease = await broker.acquire(request.operation_id)

    clock.advance(20)
    blocked = await broker.release(lease)
    assert blocked.status == "blocked"
    assert blocked.reconciliation_required is True
    assert blocked.cost_reserved_microusd == 80
    assert (await broker.status())["capacity"]["owners"]["owner-a"]["outstanding"] == 1

    with pytest.raises(ValueError, match="actual_cost_microusd"):
        await broker.cancel(
            request.operation_id,
            owner_id=request.owner_id,
            fencing_token=lease.fencing_token,
        )
    with pytest.raises(ValueError, match="non-negative"):
        await broker.cancel(
            request.operation_id,
            owner_id=request.owner_id,
            fencing_token=lease.fencing_token,
            actual_cost_microusd=-1,
        )
    assert (await broker.status())["capacity"]["owners"]["owner-a"]["cost_reserved_microusd"] == 80

    settled = await broker.reconcile(
        request.operation_id,
        owner_id=request.owner_id,
        fencing_token=lease.fencing_token,
        outcome="succeeded",
        actual_cost_microusd=70,
    )
    assert settled.status == "succeeded"
    assert settled.cost_settled_microusd == 70
    assert (await broker.status())["capacity"]["owners"] == {}

    with pytest.raises(GpuAdmissionLeaseError):
        await broker.reconcile(
            request.operation_id,
            owner_id=request.owner_id,
            fencing_token=lease.fencing_token,
            outcome="succeeded",
            actual_cost_microusd=70,
        )


@pytest.mark.asyncio
async def test_remote_callback_failure_is_uncertain_until_actual_cost_reconciliation():
    broker = RemoteInferenceAdmissionBroker(clock=_Clock())

    async def provider() -> None:
        raise RuntimeError("transport lost after dispatch")

    request = _request("transport-lost", estimated_cost_microusd=20)
    with pytest.raises(RemoteInferenceAdmissionUncertainError) as error:
        await broker.execute(request, provider)
    assert error.value.receipt.status == "blocked"
    assert error.value.receipt.reconciliation_required is True
    assert error.value.receipt.cost_reserved_microusd == 20

    with pytest.raises(ValueError, match="actual_cost_microusd"):
        await broker.reconcile(
            request.operation_id,
            owner_id=request.owner_id,
            fencing_token=error.value.receipt.fencing_token or 0,
        )
    settled = await broker.reconcile(
        request.operation_id,
        owner_id=request.owner_id,
        fencing_token=error.value.receipt.fencing_token or 0,
        actual_cost_microusd=18,
    )
    assert settled.cost_settled_microusd == 18


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
