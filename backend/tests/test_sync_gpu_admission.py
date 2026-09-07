"""Focused proof for synchronous model-fabric GPU admission."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from config.settings import settings
from src.llm_runtime import (
    _execute_sync_with_gpu_admission,
    completion_with_fallback_sync,
)
from src.model_fabric.gpu_admission import (
    GpuAdmissionBroker,
    GpuAdmissionCapacityError,
    GpuAdmissionCancelledError,
    GpuAdmissionExpiredError,
    GpuAdmissionIdentityError,
    GpuAdmissionLeaseError,
    GpuAdmissionReceipt,
    GpuAdmissionRequest,
    GpuAdmissionUncertainError,
    GpuPriority,
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
    deadline_at: float | None = None,
    clock: _Clock | None = None,
) -> GpuAdmissionRequest:
    current = clock() if clock is not None else time.time()
    return GpuAdmissionRequest(
        operation_id=operation_id,
        job_id=f"job-{operation_id}",
        owner_id="operator:test",
        priority=GpuPriority.INTERACTIVE_CHAT,
        deadline_at=deadline_at if deadline_at is not None else current + 10,
        runtime_path="chat_agent",
    )


def _canonical_context(
    *,
    request_id: str = "sync-request",
    principal: object | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        request_id=request_id,
        job_id=f"job-{request_id}",
        owner_id="operator:test",
        principal=principal
        if principal is not None
        else SimpleNamespace(principal_id="operator:test"),
        deadline_at=time.time() + 30,
        runtime_path="chat_agent",
        workload=SimpleNamespace(value="interactive"),
        egress_class=SimpleNamespace(value="local_only"),
    )


def _capacity_error(operation_id: str = "denied") -> GpuAdmissionCapacityError:
    receipt = GpuAdmissionReceipt(
        operation_id=operation_id,
        job_id=f"job-{operation_id}",
        owner_id="operator:test",
        priority=GpuPriority.INTERACTIVE_CHAT,
        status="rejected",
        queue_position=None,
        active_operation_id="active-operation",
        fencing_token=None,
        reason_code="capacity_exhausted",
        queued=1,
        max_queued=1,
        runtime_path="chat_agent",
    )
    return GpuAdmissionCapacityError("GPU admission queue is full", receipt=receipt)


def test_execute_sync_serializes_blocking_callbacks_across_worker_threads():
    broker = GpuAdmissionBroker()
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    def run(index: int) -> int:
        nonlocal active, max_active

        def provider() -> int:
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.02)
            with state_lock:
                active -= 1
            return index

        return broker.execute_sync(_request(f"sync-{index}"), provider)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, range(4)))

    assert sorted(results) == [0, 1, 2, 3]
    assert max_active == 1
    assert broker._active_operation_id is None


@pytest.mark.parametrize("terminal", ["cancelled", "expired"])
def test_execute_sync_terminal_admission_never_invokes_provider(terminal: str):
    clock = _Clock()
    broker = GpuAdmissionBroker(clock=clock)
    request = _request("terminal", deadline_at=110.0, clock=clock)
    asyncio.run(broker.enqueue(request))

    if terminal == "cancelled":
        asyncio.run(broker.cancel(request.operation_id, owner_id=request.owner_id))
        expected = GpuAdmissionCancelledError
    else:
        clock.advance(11.0)
        expected = GpuAdmissionExpiredError

    provider = Mock()
    with pytest.raises(expected):
        broker.execute_sync(request, provider)

    provider.assert_not_called()


def test_execute_sync_capacity_denial_never_invokes_provider():
    broker = GpuAdmissionBroker(max_queued=1)
    entered = threading.Event()
    release = threading.Event()
    provider_calls: list[str] = []

    def active_provider() -> None:
        entered.set()
        release.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        active_future = pool.submit(
            broker.execute_sync,
            _request("active"),
            active_provider,
        )
        assert entered.wait(timeout=5)
        queued = _request("queued")
        asyncio.run(broker.enqueue(queued))

        def rejected_provider() -> None:
            provider_calls.append("rejected")

        with pytest.raises(GpuAdmissionCapacityError):
            broker.execute_sync(_request("rejected"), rejected_provider)

        release.set()
        assert active_future.result(timeout=5) is None

    assert provider_calls == []


@pytest.mark.asyncio
async def test_execute_sync_waits_behind_async_operation_on_shared_gpu_lane():
    broker = GpuAdmissionBroker()
    async_entered = asyncio.Event()
    release_async = asyncio.Event()
    sync_entered = threading.Event()
    release_sync = threading.Event()
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    async def async_provider() -> None:
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        async_entered.set()
        await release_async.wait()
        with state_lock:
            active -= 1

    def sync_provider() -> None:
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        sync_entered.set()
        assert release_sync.wait(timeout=5)
        with state_lock:
            active -= 1

    async_task = asyncio.create_task(
        broker.execute(_request("async-active"), async_provider)
    )
    await async_entered.wait()
    with ThreadPoolExecutor(max_workers=1) as pool:
        sync_future = pool.submit(
            broker.execute_sync,
            _request("sync-waiter"),
            sync_provider,
        )
        for _ in range(10):
            if [item["operation_id"] for item in (await broker.status())["queued"]] == [
                "sync-waiter"
            ]:
                break
            await asyncio.sleep(0)
        assert sync_entered.is_set() is False

        release_async.set()
        await async_task
        assert sync_entered.wait(timeout=5)
        release_sync.set()
        for _ in range(100):
            if sync_future.done():
                break
            await asyncio.sleep(0.01)
        assert sync_future.done()
        sync_future.result()
    assert max_active == 1


@pytest.mark.asyncio
async def test_async_cancel_marks_running_sync_operation_and_blocks_success():
    broker = GpuAdmissionBroker()
    entered = threading.Event()
    release = threading.Event()

    def provider() -> str:
        entered.set()
        assert release.wait(timeout=5)
        return "provider-result"

    with ThreadPoolExecutor(max_workers=1) as pool:
        sync_future = pool.submit(
            broker.execute_sync,
            _request("sync-cancel"),
            provider,
        )
        assert entered.wait(timeout=5)
        active = (await broker.status())["active"]
        cancelled = await broker.cancel(
            "sync-cancel",
            owner_id="operator:test",
            fencing_token=active["fencing_token"],
        )
        assert cancelled.cancel_requested is True
        release.set()

        for _ in range(100):
            if sync_future.done():
                break
            await asyncio.sleep(0.01)
        assert sync_future.done()
        with pytest.raises(GpuAdmissionCancelledError):
            sync_future.result()
    assert (await broker.status())["active"] is None


def test_late_sync_callback_holds_gpu_until_provider_result_is_reconciled():
    clock = _Clock()
    broker = GpuAdmissionBroker(clock=clock)
    late_request = _request("late-sync-callback", deadline_at=101.0, clock=clock)
    follow_up_request = _request("sync-follow-up", clock=clock)
    follow_up_started = threading.Event()

    def late_callback() -> str:
        clock.advance(1.0)
        return "late-result"

    with pytest.raises(GpuAdmissionUncertainError) as error:
        broker.execute_sync(late_request, late_callback, now=clock())

    assert error.value.receipt.status == "blocked"
    assert error.value.receipt.reason_code == "deadline_expired_after_callback"
    active = asyncio.run(broker.status())["active"]
    assert active["operation_id"] == late_request.operation_id
    assert active["reconciliation_required"] is True

    def follow_up_callback() -> str:
        follow_up_started.set()
        return "follow-up-result"

    with ThreadPoolExecutor(max_workers=1) as pool:
        follow_up = pool.submit(
            broker.execute_sync,
            follow_up_request,
            follow_up_callback,
        )
        for _ in range(50):
            queued = asyncio.run(broker.status())["queued"]
            if any(item["operation_id"] == follow_up_request.operation_id for item in queued):
                break
            time.sleep(0.01)
        assert follow_up_started.is_set() is False

        reconciled = asyncio.run(
            broker.reconcile(
                late_request.operation_id,
                owner_id=late_request.owner_id,
                fencing_token=active["fencing_token"],
                outcome="failed",
                reason_code="provider_result_reconciled_failed",
            )
        )
        assert reconciled.status == "failed"
        assert follow_up.result(timeout=1.0) == "follow-up-result"

    assert follow_up_started.is_set() is True
    assert asyncio.run(broker.status())["active"] is None


def test_active_deadline_watchdog_blocks_before_sync_callback_returns():
    clock = _Clock()
    broker = GpuAdmissionBroker(clock=clock)
    request = _request("watchdog-sync-active", deadline_at=101.0, clock=clock)
    follow_up_request = _request("watchdog-sync-follow-up", clock=clock)
    callback_started = threading.Event()
    release_callback = threading.Event()
    follow_up_started = threading.Event()

    def provider() -> str:
        callback_started.set()
        assert release_callback.wait(timeout=5)
        return "late-result"

    with ThreadPoolExecutor(max_workers=2) as pool:
        task = pool.submit(broker.execute_sync, request, provider, now=clock())
        assert callback_started.wait(timeout=5)
        clock.advance(1.0)
        active = asyncio.run(broker.status())["active"]
        assert active["status"] == "blocked"
        assert active["reason_code"] == "deadline_expired_active"
        assert active["deadline_exceeded"] is True
        assert active["callback_completed"] is False
        assert active["cancel_requested"] is False

        with pytest.raises(GpuAdmissionLeaseError) as reconciliation_error:
            asyncio.run(
                broker.reconcile(
                    request.operation_id,
                    owner_id=request.owner_id,
                    fencing_token=active["fencing_token"],
                )
            )
        assert reconciliation_error.value.receipt.reason_code == "provider_callback_still_running"

        def follow_up_provider() -> str:
            follow_up_started.set()
            return "follow-up-result"

        follow_up = pool.submit(
            broker.execute_sync,
            follow_up_request,
            follow_up_provider,
        )
        for _ in range(50):
            queued = asyncio.run(broker.status())["queued"]
            if any(item["operation_id"] == follow_up_request.operation_id for item in queued):
                break
            time.sleep(0.01)
        assert follow_up_started.is_set() is False

        release_callback.set()
        with pytest.raises(GpuAdmissionUncertainError):
            task.result(timeout=5)
        reconciled = asyncio.run(
            broker.reconcile(
                request.operation_id,
                owner_id=request.owner_id,
                fencing_token=active["fencing_token"],
                outcome="failed",
                reason_code="provider_result_reconciled_failed",
            )
        )
        assert reconciled.status == "failed"
        assert follow_up.result(timeout=5) == "follow-up-result"

    assert follow_up_started.is_set() is True
    assert asyncio.run(broker.status())["active"] is None


def test_canonical_sync_completion_admits_provider_and_finishes_receipt():
    broker = GpuAdmissionBroker()
    context = _canonical_context(request_id="sync-success")
    decision = MagicMock(allowed=True, attempt_id="attempt-sync-success")
    session = MagicMock()
    session.workload = None
    session.finalize = AsyncMock(return_value=SimpleNamespace(
        persisted=True,
        status="persisted",
        receipt_id="route-sync-success",
        error_code=None,
    ))
    response = MagicMock()

    with (
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", ""),
        patch("src.llm_runtime._can_log_request", return_value=False),
        patch("src.model_fabric.bind_final_inference_payload", side_effect=lambda value, _payload: value),
        patch("src.llm_runtime._governed_preflight_target", return_value=(decision, ("a" * 64,))),
        patch("src.llm_runtime._new_route_receipt_session", return_value=session),
        patch("src.llm_runtime._governed_openai_chat_completion", return_value=(response, {})) as provider,
        patch("src.llm_runtime.gpu_admission_broker", broker),
    ):
        result = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.2,
            max_tokens=64,
            runtime_path="chat_agent",
            request_context=context,
        )

    assert result is response
    provider.assert_called_once()
    assert broker._operations["attempt-sync-success"].status == "succeeded"
    session.attempt_started.assert_called_once_with(
        decision,
        capability_proof_hashes=("a" * 64,),
    )
    session.attempt_finished.assert_called_once()
    session.finalize.assert_awaited_once_with(
        outcome="succeeded",
        fallback_reason_code=None,
        degradation_codes=(),
    )


def test_canonical_sync_admission_denial_stops_fallback_and_persists_denial():
    broker = MagicMock()
    broker.execute_sync.side_effect = _capacity_error("attempt-denied")
    context = _canonical_context(request_id="sync-denied")
    decision = MagicMock(allowed=True, attempt_id="attempt-denied")

    with (
        patch.object(settings, "fallback_model", "ollama/fallback"),
        patch.object(settings, "fallback_models", ""),
        patch("src.llm_runtime._can_log_request", return_value=False),
        patch("src.model_fabric.bind_final_inference_payload", side_effect=lambda value, _payload: value),
        patch("src.llm_runtime._governed_preflight_target", return_value=(decision, ())),
        patch("src.llm_runtime._new_route_receipt_session", return_value=None),
        patch("src.llm_runtime._governed_openai_chat_completion") as provider,
        patch("src.llm_runtime._persist_denied_route_sync") as denied,
        patch("src.llm_runtime.gpu_admission_broker", broker),
    ):
        with pytest.raises(GpuAdmissionCapacityError):
            completion_with_fallback_sync(
                messages=[{"role": "user", "content": "hello"}],
                temperature=0.2,
                max_tokens=64,
                runtime_path="chat_agent",
                request_context=context,
            )

    provider.assert_not_called()
    assert broker.execute_sync.call_count == 1
    denied.assert_called_once()
    assert denied.call_args.args[2] == (
        "gpu_admission_rejected",
        "gpu_admission_capacity_exhausted",
    )
    assert denied.call_args.kwargs["fallback_reason_code"] == "gpu_admission_rejected"


def test_canonical_sync_uncertain_completion_keeps_route_outcome_out_of_denial_receipt():
    clock = _Clock()
    broker = GpuAdmissionBroker(clock=clock)
    context = _canonical_context(request_id="sync-uncertain")
    context.deadline_at = 101.0
    provider = Mock()

    def late_provider() -> str:
        clock.advance(1.0)
        return "late-result"

    provider.side_effect = late_provider
    denied = Mock()

    with (
        patch("src.llm_runtime._persist_sync_gpu_admission_denial", denied),
        patch("src.llm_runtime.gpu_admission_broker", broker),
    ):
        with pytest.raises(GpuAdmissionUncertainError) as error:
            _execute_sync_with_gpu_admission(
                context=context,
                decision=MagicMock(allowed=True),
                operation_id="attempt-sync-uncertain",
                operation=provider,
            )

    provider.assert_called_once()
    assert error.value.receipt.status == "blocked"
    assert error.value.receipt.reconciliation_required is True
    assert asyncio.run(broker.status())["active"]["operation_id"] == "attempt-sync-uncertain"
    denied.assert_not_called()


def test_canonical_sync_missing_owner_fails_closed_before_broker_or_provider():
    context = _canonical_context(request_id="sync-missing-owner", principal=None)
    context.principal = None
    provider = Mock()
    broker = MagicMock()
    denied = Mock()

    with (
        patch("src.llm_runtime._persist_denied_route_sync", denied),
        patch("src.llm_runtime.gpu_admission_broker", broker),
    ):
        with pytest.raises(GpuAdmissionIdentityError):
            _execute_sync_with_gpu_admission(
                context=context,
                decision=MagicMock(allowed=True),
                operation_id="attempt-missing-owner",
                operation=provider,
            )

    provider.assert_not_called()
    broker.execute_sync.assert_not_called()
    denied.assert_called_once()


def test_legacy_sync_context_keeps_transitional_provider_compatibility():
    broker = MagicMock()
    provider = Mock(return_value="legacy-result")

    with patch("src.llm_runtime.gpu_admission_broker", broker):
        result = _execute_sync_with_gpu_admission(
            context=None,
            decision=None,
            operation_id="legacy-operation",
            operation=provider,
        )

    assert result == "legacy-result"
    provider.assert_called_once()
    broker.execute_sync.assert_not_called()
