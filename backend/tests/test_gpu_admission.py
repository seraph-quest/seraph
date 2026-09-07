"""Deterministic proof for the bounded #744 one-GPU admission seam."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.model_fabric.gpu_admission import (
    GpuAdmissionBroker,
    GpuAdmissionCapacityError,
    GpuAdmissionCancelledError,
    GpuAdmissionExpiredError,
    GpuAdmissionIdentityError,
    GpuAdmissionLease,
    GpuAdmissionLeaseError,
    GpuAdmissionUncertainError,
    GpuAdmissionRequest,
    GpuPriority,
    priority_for_inference_context,
)


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
    priority: GpuPriority = GpuPriority.REPORTS_RESEARCH_MEMORY,
    deadline_at: float = 1_000.0,
    owner_id: str = "owner-a",
    job_id: str | None = None,
    parent_job_id: str | None = None,
) -> GpuAdmissionRequest:
    return GpuAdmissionRequest(
        operation_id=operation_id,
        job_id=job_id or f"job-{operation_id}",
        owner_id=owner_id,
        parent_job_id=parent_job_id,
        priority=priority,
        deadline_at=deadline_at,
        runtime_path="chat_agent",
    )


@pytest.mark.asyncio
async def test_priority_selection_and_fifo_within_class():
    clock = _Clock()
    broker = GpuAdmissionBroker(clock=clock)
    low_first = _request("low-1")
    low_second = _request("low-2")
    scheduled = _request("scheduled", priority=GpuPriority.ACCEPTED_SCHEDULED_GOAL)
    operator = _request("operator", priority=GpuPriority.APPROVED_OPERATOR)
    interactive = _request("interactive", priority=GpuPriority.INTERACTIVE_CHAT)
    screenshot = _request("screenshot", priority=GpuPriority.SCREENSHOT_BACKGROUND)

    for request in (low_first, low_second, scheduled, operator, interactive, screenshot):
        receipt = await broker.enqueue(request)
        assert receipt.status == "queued"

    order = []
    for operation_id in ("interactive", "operator", "scheduled", "low-1", "low-2", "screenshot"):
        lease = await broker.acquire(operation_id)
        order.append(lease.operation_id)
        await broker.release(lease)

    assert order == ["interactive", "operator", "scheduled", "low-1", "low-2", "screenshot"]


@pytest.mark.asyncio
async def test_one_active_operation_and_release_between_sequential_callbacks():
    broker = GpuAdmissionBroker(clock=_Clock())
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    active_counts: list[int] = []
    calls: list[str] = []

    async def first():
        calls.append("first")
        active_counts.append(1)
        first_entered.set()
        await release_first.wait()
        active_counts.pop()
        return "first-result"

    async def second():
        calls.append("second")
        active_counts.append(1)
        assert len(active_counts) == 1
        active_counts.pop()
        return "second-result"

    first_task = asyncio.create_task(broker.execute(_request("first"), first))
    await first_entered.wait()
    second_task = asyncio.create_task(broker.execute(_request("second"), second))
    await asyncio.sleep(0)

    status = await broker.status()
    assert status["serial_gpu"] is True
    assert status["max_active"] == 1
    assert status["active"]["operation_id"] == "first"
    assert [item["operation_id"] for item in status["queued"]] == ["second"]
    assert calls == ["first"]

    release_first.set()
    assert await first_task == "first-result"
    assert await second_task == "second-result"
    assert calls == ["first", "second"]
    assert (await broker.status())["active"] is None


@pytest.mark.asyncio
async def test_bounded_capacity_rejects_without_invoking_provider_and_recovers():
    broker = GpuAdmissionBroker(max_queued=1, clock=_Clock())
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    provider_calls: list[str] = []

    async def first():
        first_entered.set()
        await release_first.wait()

    async def queued():
        provider_calls.append("queued")

    first_task = asyncio.create_task(broker.execute(_request("first"), first))
    await first_entered.wait()
    queued_task = asyncio.create_task(broker.execute(_request("queued"), queued))
    await asyncio.sleep(0)

    async def rejected():
        provider_calls.append("rejected")

    with pytest.raises(GpuAdmissionCapacityError) as error:
        await broker.execute(_request("rejected"), rejected)
    assert error.value.receipt.status == "rejected"
    assert error.value.receipt.reason_code == "capacity_exhausted"
    assert provider_calls == []
    degraded = await broker.status()
    assert degraded["status"] == "degraded"
    assert degraded["degradation_code"] == "capacity_exhausted"

    release_first.set()
    await first_task
    await queued_task
    assert provider_calls == ["queued"]
    assert (await broker.status())["status"] == "ready"


@pytest.mark.asyncio
async def test_cancelled_queued_operation_never_reaches_provider():
    broker = GpuAdmissionBroker(clock=_Clock())
    request = _request("cancel-me")
    await broker.enqueue(request)
    cancelled = await broker.cancel(request.operation_id, owner_id=request.owner_id)

    assert cancelled.status == "cancelled"
    assert cancelled.reason_code == "cancelled"
    provider_called = False

    async def provider():
        nonlocal provider_called
        provider_called = True

    with pytest.raises(GpuAdmissionCancelledError) as error:
        await broker.execute(request, provider)
    assert error.value.receipt.status == "cancelled"
    assert provider_called is False


@pytest.mark.asyncio
async def test_expired_queued_operation_never_reaches_provider():
    clock = _Clock()
    broker = GpuAdmissionBroker(clock=clock)
    request = _request("expire-me", deadline_at=101.0)
    await broker.enqueue(request)
    clock.advance(2.0)
    provider_called = False

    async def provider():
        nonlocal provider_called
        provider_called = True

    with pytest.raises(GpuAdmissionExpiredError) as error:
        await broker.execute(request, provider)
    assert error.value.receipt.status == "expired"
    assert error.value.receipt.reason_code == "deadline_expired"
    assert provider_called is False


@pytest.mark.asyncio
async def test_late_callback_holds_gpu_until_provider_result_is_reconciled():
    clock = _Clock()
    broker = GpuAdmissionBroker(clock=clock)
    late_request = _request("late-callback", deadline_at=101.0)
    follow_up_request = _request("follow-up")
    follow_up_called = False

    async def late_callback():
        clock.advance(1.0)
        return "late-result"

    async def follow_up_callback():
        nonlocal follow_up_called
        follow_up_called = True
        return "follow-up-result"

    with pytest.raises(GpuAdmissionUncertainError) as error:
        await broker.execute(late_request, late_callback, now=clock())

    assert error.value.receipt.status == "blocked"
    assert error.value.receipt.reason_code == "deadline_expired_after_callback"
    assert error.value.receipt.reconciliation_required is True
    active = (await broker.status())["active"]
    assert active["operation_id"] == late_request.operation_id
    assert active["recovery_action"]

    follow_up = asyncio.create_task(broker.execute(follow_up_request, follow_up_callback))
    await asyncio.sleep(0)
    assert follow_up_called is False
    blocked_status = await broker.status()
    assert blocked_status["status"] == "degraded"
    assert blocked_status["degradation_code"] == "deadline_expired_after_callback"

    reconciled = await broker.reconcile(
        late_request.operation_id,
        owner_id=late_request.owner_id,
        fencing_token=active["fencing_token"],
        outcome="failed",
        reason_code="provider_result_reconciled_failed",
    )
    assert reconciled.status == "failed"
    assert await follow_up == "follow-up-result"
    assert (await broker.status())["active"] is None


@pytest.mark.asyncio
async def test_active_deadline_watchdog_blocks_before_async_callback_returns():
    clock = _Clock()
    broker = GpuAdmissionBroker(clock=clock)
    request = _request("watchdog-active", deadline_at=101.0)
    follow_up_request = _request("watchdog-follow-up")
    callback_started = asyncio.Event()
    callback_cancelled = asyncio.Event()
    release_callback = asyncio.Event()
    follow_up_called = False

    async def provider():
        callback_started.set()
        try:
            await release_callback.wait()
        except asyncio.CancelledError:
            callback_cancelled.set()
            await release_callback.wait()
        return "late-result"

    task = asyncio.create_task(broker.execute(request, provider, now=clock()))
    await callback_started.wait()
    clock.advance(1.0)
    active = (await broker.status())["active"]
    assert active["status"] == "blocked"
    assert active["reason_code"] == "deadline_expired_active"
    assert active["deadline_exceeded"] is True
    assert active["callback_completed"] is False
    assert active["cancel_requested"] is True
    await callback_cancelled.wait()

    with pytest.raises(GpuAdmissionLeaseError) as reconciliation_error:
        await broker.reconcile(
            request.operation_id,
            owner_id=request.owner_id,
            fencing_token=active["fencing_token"],
        )
    assert reconciliation_error.value.receipt.reason_code == "provider_callback_still_running"

    async def follow_up_provider():
        nonlocal follow_up_called
        follow_up_called = True
        return "follow-up-result"

    follow_up = asyncio.create_task(broker.execute(follow_up_request, follow_up_provider))
    await asyncio.sleep(0)
    assert follow_up_called is False

    release_callback.set()
    with pytest.raises(GpuAdmissionUncertainError) as error:
        await task
    assert error.value.receipt.status == "blocked"
    assert error.value.receipt.callback_completed is True

    reconciled = await broker.reconcile(
        request.operation_id,
        owner_id=request.owner_id,
        fencing_token=active["fencing_token"],
        outcome="failed",
        reason_code="provider_result_reconciled_failed",
    )
    assert reconciled.status == "failed"
    assert await follow_up == "follow-up-result"
    assert follow_up_called is True


@pytest.mark.asyncio
async def test_stale_owner_and_fencing_token_cannot_release_active_gpu():
    broker = GpuAdmissionBroker(clock=_Clock())
    request = _request("lease", owner_id="owner-a")
    await broker.enqueue(request)
    lease = await broker.acquire(request.operation_id)
    stale = replace(lease, owner_id="owner-b")

    with pytest.raises(GpuAdmissionLeaseError) as error:
        await broker.release(stale)
    assert error.value.receipt.reason_code == "stale_owner_or_fencing_token"
    assert (await broker.status())["active"]["operation_id"] == "lease"

    stale_token = replace(lease, fencing_token=lease.fencing_token + 1)
    with pytest.raises(GpuAdmissionLeaseError):
        await broker.release(stale_token)
    await broker.release(lease)


@pytest.mark.asyncio
async def test_running_cancel_requires_owner_and_fencing_token():
    broker = GpuAdmissionBroker(clock=_Clock())
    request = _request("cancel-running", owner_id="owner-a")
    await broker.enqueue(request)
    lease = await broker.acquire(request.operation_id)

    with pytest.raises(GpuAdmissionLeaseError) as missing_auth:
        await broker.cancel(request.operation_id)
    assert missing_auth.value.receipt.reason_code == "stale_owner_or_fencing_token"
    assert (await broker.status())["active"]["cancel_requested"] is False

    with pytest.raises(GpuAdmissionLeaseError):
        await broker.cancel(
            request.operation_id,
            owner_id="owner-b",
            fencing_token=lease.fencing_token,
        )
    with pytest.raises(GpuAdmissionLeaseError):
        await broker.cancel(
            request.operation_id,
            owner_id=request.owner_id,
            fencing_token=lease.fencing_token + 1,
        )
    assert (await broker.status())["active"]["cancel_requested"] is False

    cancelled = await broker.cancel(
        request.operation_id,
        owner_id=request.owner_id,
        fencing_token=lease.fencing_token,
    )
    assert cancelled.status == "running"
    assert cancelled.cancel_requested is True
    await broker.release(lease, outcome="cancelled", reason_code="cancelled")


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.asyncio
async def test_cancelled_waiter_is_removed_before_provider_invocation(streaming):
    broker = GpuAdmissionBroker(clock=_Clock())
    first_request = _request("waiter-first")
    second_request = _request("waiter-second")
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    provider_calls: list[str] = []

    async def first_stream():
        first_entered.set()
        await release_first.wait()
        yield "first"

    async def first_execute():
        first_entered.set()
        await release_first.wait()

    async def second_stream():
        provider_calls.append("stream")
        yield "unexpected"

    async def second_execute():
        provider_calls.append("execute")
        return "unexpected"

    async def run_first():
        if streaming:
            async for _item in broker.stream(first_request, first_stream):
                pass
        else:
            await broker.execute(first_request, first_execute)

    async def run_second():
        if streaming:
            async for _item in broker.stream(second_request, second_stream):
                pass
        else:
            await broker.execute(second_request, second_execute)

    first_task = asyncio.create_task(run_first())
    await first_entered.wait()
    second_task = asyncio.create_task(run_second())
    for _ in range(10):
        if [item["operation_id"] for item in (await broker.status())["queued"]] == ["waiter-second"]:
            break
        await asyncio.sleep(0)
    assert [item["operation_id"] for item in (await broker.status())["queued"]] == ["waiter-second"]

    second_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second_task
    assert (await broker.status())["queued"] == []
    cancelled = await broker.enqueue(second_request)
    assert cancelled.status == "cancelled"
    assert cancelled.reason_code == "caller_cancelled"
    assert provider_calls == []

    release_first.set()
    await first_task


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.asyncio
async def test_active_cancel_stops_callback_and_never_reports_success(streaming):
    broker = GpuAdmissionBroker(clock=_Clock())
    request = _request("active-cancel")
    callback_started = asyncio.Event()
    callback_cancelled = asyncio.Event()
    stream_closed = asyncio.Event()
    hold_callback = asyncio.Event()
    received: list[str] = []

    async def execute_provider():
        callback_started.set()
        try:
            await hold_callback.wait()
        except asyncio.CancelledError:
            callback_cancelled.set()
            return "ignored-cancellation"
        return "unexpected-success"

    async def stream_provider():
        callback_started.set()
        try:
            yield "partial"
            await hold_callback.wait()
        except asyncio.CancelledError:
            callback_cancelled.set()
            return
        finally:
            stream_closed.set()

    async def run():
        if streaming:
            async for item in broker.stream(request, stream_provider):
                received.append(item)
        else:
            return await broker.execute(request, execute_provider)

    task = asyncio.create_task(run())
    await callback_started.wait()
    active = (await broker.status())["active"]
    cancelled = await broker.cancel(
        request.operation_id,
        owner_id=request.owner_id,
        fencing_token=active["fencing_token"],
    )
    assert cancelled.cancel_requested is True

    with pytest.raises(GpuAdmissionCancelledError) as error:
        await task
    assert error.value.receipt.status == "cancelled"
    assert callback_cancelled.is_set()
    assert stream_closed.is_set() is streaming
    assert received == (["partial"] if streaming else [])
    assert (await broker.status())["active"] is None
    terminal = await broker.enqueue(request)
    assert terminal.status == "cancelled"
    assert terminal.reason_code == "cancelled"


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.asyncio
async def test_caller_cancellation_releases_active_operation_and_closes_provider(streaming):
    broker = GpuAdmissionBroker(clock=_Clock())
    request = _request("caller-cancel-active")
    callback_waiting = asyncio.Event()
    callback_cancelled = asyncio.Event()
    stream_closed = asyncio.Event()
    hold_callback = asyncio.Event()
    received: list[str] = []

    async def execute_provider():
        callback_waiting.set()
        try:
            await hold_callback.wait()
        except asyncio.CancelledError:
            callback_cancelled.set()
            return "ignored-cancellation"

    class BlockingStream:
        def __init__(self):
            self._emitted = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._emitted:
                self._emitted = True
                return "partial"
            callback_waiting.set()
            try:
                await hold_callback.wait()
            except asyncio.CancelledError:
                callback_cancelled.set()
                raise StopAsyncIteration
            raise StopAsyncIteration

        async def aclose(self):
            stream_closed.set()

    def stream_provider():
        return BlockingStream()

    async def run():
        if streaming:
            async for item in broker.stream(request, stream_provider):
                received.append(item)
        else:
            await broker.execute(request, execute_provider)

    task = asyncio.create_task(run())
    await callback_waiting.wait()
    task.cancel()
    with pytest.raises(GpuAdmissionCancelledError) as error:
        await task

    assert error.value.receipt.status == "cancelled"
    assert callback_cancelled.is_set()
    assert stream_closed.is_set() is streaming
    assert received == (["partial"] if streaming else [])
    assert (await broker.status())["active"] is None
    terminal = await broker.enqueue(request)
    assert terminal.status == "cancelled"
    assert terminal.reason_code == "caller_cancelled"


@pytest.mark.asyncio
async def test_identity_conflict_and_operator_safe_status_receipt():
    broker = GpuAdmissionBroker(clock=_Clock())
    request = _request("stable", job_id="job-stable", parent_job_id="job-parent")
    await broker.enqueue(request)
    conflicting = _request("stable", owner_id="other-owner")

    with pytest.raises(GpuAdmissionIdentityError):
        await broker.enqueue(conflicting)
    status = await broker.status()
    assert status["schema_version"] == "seraph.gpu-admission.v1"
    assert status["operator_visible"] is True
    assert status["claim_boundary"].startswith("process_local_admission_lease")
    item = status["queued"][0]
    assert item["job_id"] == "job-stable"
    assert item["parent_job_id"] == "job-parent"
    assert "prompt" not in item
    assert "payload" not in item


@pytest.mark.asyncio
async def test_stream_holds_gpu_lease_until_final_delta():
    broker = GpuAdmissionBroker(clock=_Clock())
    first_started = asyncio.Event()
    allow_first_finish = asyncio.Event()
    second_called = False
    received: list[str] = []

    async def first_stream():
        first_started.set()
        yield "one"
        await allow_first_finish.wait()
        yield "two"

    async def second():
        nonlocal second_called
        second_called = True
        return "second"

    async def collect_first():
        async for delta in broker.stream(_request("stream-1"), first_stream):
            received.append(delta)

    first_task = asyncio.create_task(collect_first())
    await first_started.wait()
    second_task = asyncio.create_task(broker.execute(_request("stream-2"), second))
    await asyncio.sleep(0)
    assert second_called is False
    assert (await broker.status())["active"]["operation_id"] == "stream-1"
    allow_first_finish.set()
    await first_task
    assert await second_task == "second"
    assert received == ["one", "two"]
    assert second_called is True


@pytest.mark.asyncio
async def test_inference_context_derives_interactive_identity_and_priority():
    from src.model_fabric.caller_context import build_canonical_inference_context
    from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal

    principal = TrustPrincipal(
        principal_id="operator-1",
        principal_type=PrincipalType.OPERATOR,
        grants=(AuthorityGrant.MODEL_INFERENCE,),
        session_id="session-1",
    )
    context = build_canonical_inference_context(
        "onboarding_agent",
        payload={"message": "hello"},
        output_tokens=64,
        timeout_seconds=10,
        principal=principal,
        session_id="session-1",
        request_id="request-onboarding",
    )
    request = GpuAdmissionRequest.from_inference_context(
        context,
        operation_id="attempt-onboarding",
    )
    assert request.operation_id == "attempt-onboarding"
    assert request.job_id == "request-onboarding"
    assert request.owner_id == "operator-1"
    assert request.priority is GpuPriority.INTERACTIVE_CHAT


@pytest.mark.parametrize(
    ("runtime_path", "workload", "expected"),
    [
        ("chat_agent", "interactive", GpuPriority.INTERACTIVE_CHAT),
        ("strategist_agent", "background", GpuPriority.ACCEPTED_SCHEDULED_GOAL),
        ("daily_briefing", "report", GpuPriority.REPORTS_RESEARCH_MEMORY),
        ("session_consolidation", "background", GpuPriority.REPORTS_RESEARCH_MEMORY),
        ("screenshot_image_analysis", "vision", GpuPriority.SCREENSHOT_BACKGROUND),
    ],
)
def test_existing_runtime_paths_map_into_typed_priority_classes(runtime_path, workload, expected):
    context = SimpleNamespace(runtime_path=runtime_path, workload=workload)
    assert priority_for_inference_context(context) is expected
