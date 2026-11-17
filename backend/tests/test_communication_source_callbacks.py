"""Original callback closure mechanics; no source/provider success is fabricated."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from src.work_board.dispatcher import _await_communication_model_call
from src.work_board.dispatcher import WorkBoardDispatcher
from src.work_board.repository import BoardError
from tests.test_inference_accounting import accounting_db
from tests.support.sync import assert_blocked, suspended_in, wait_until


def _holding_after_deadline(producer):
    """The producer has left its bounded ``wait_for`` (deadline or cancel
    delivered) and is parked holding the original worker."""
    return suspended_in(producer, "_await_communication_model_call", without=("wait_for",))


@pytest.mark.asyncio
@pytest.mark.parametrize("late_failure", [False, True])
async def test_timeout_holds_original_worker_until_thread_closes(late_failure):
    started = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    calls = []

    def original_call():
        calls.append("original")
        started.set()
        try:
            if not release.wait(5):
                raise RuntimeError("test did not release original call")
            if late_failure:
                raise ValueError("late callback failure")
            return "late output must not be adopted"
        finally:
            closed.set()

    producer = asyncio.create_task(_await_communication_model_call(
        asyncio.to_thread(original_call), timeout=0.02))
    try:
        await wait_until(started.is_set, what="original call started", task=producer)
        # The 0.02 s production deadline has fired and the producer still
        # holds the original thread: observed, not assumed after a sleep.
        await assert_blocked(producer, at=lambda: _holding_after_deadline(producer),
                             what="timed-out producer holds the original worker")
        assert not producer.done()
        assert not closed.is_set()
        release.set()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(producer, 2)
        assert closed.is_set()
        assert calls == ["original"]
    finally:
        release.set()


@pytest.mark.asyncio
async def test_repeated_cancellation_holds_same_thread_and_preserves_cancel():
    started = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    calls = []

    def original_call():
        calls.append("original")
        started.set()
        try:
            assert release.wait(5)
            return "late output must not be adopted"
        finally:
            closed.set()

    producer = asyncio.create_task(_await_communication_model_call(
        asyncio.to_thread(original_call), timeout=2))
    try:
        await wait_until(started.is_set, what="original call started", task=producer)
        for _ in range(3):
            producer.cancel()
            # assert_blocked settles the loop, which delivers the cancel.
            await assert_blocked(producer, at=lambda: _holding_after_deadline(producer),
                                 what="cancelled producer holds the original worker")
            assert not producer.done()
            assert not closed.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await producer
        assert closed.is_set()
        assert calls == ["original"]
    finally:
        release.set()


@pytest.mark.asyncio
async def test_on_time_original_callback_returns_after_actual_close():
    closed = threading.Event()

    def original_call():
        closed.set()
        return "original result"

    assert await _await_communication_model_call(
        asyncio.to_thread(original_call), timeout=2) == "original result"
    assert closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["work.mail-reply-draft.v1", "calendar.meeting-prep.v1"])
async def test_source_adapter_rejects_unsealed_preparation_before_native_admission(
    accounting_db, capability,
):
    _root, _engine, factory = accounting_db
    dispatcher = WorkBoardDispatcher(session_provider=factory.accounting_sessions)
    with pytest.raises(BoardError) as failure:
        await dispatcher._execute_direct_adapter(
            SimpleNamespace(capability_id=capability), SimpleNamespace(), {},
            runtime_seconds=120, admission_only=True,
            communication_binding={"parent_job_id": "forged-parent"},
        )
    assert failure.value.code == "communication_preparation_seal_required"
