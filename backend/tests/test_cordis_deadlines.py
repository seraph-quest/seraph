"""Deadline classes and original pipe identity; no inference producer fixture."""
import asyncio
from dataclasses import replace
import os
from pathlib import Path
import time
from types import MappingProxyType

import pytest

from src.runtime_plugins.bridge import CordisHost, HostBlocked
from src.runtime_plugins.protocol import CONTROL_METHODS, ProtocolError, rpc_deadline


@pytest.mark.parametrize("method", sorted(CONTROL_METHODS))
def test_controls_keep_five_seconds_and_earlier_original_cutoff(method):
    assert rpc_deadline(method, now=1000, original_deadline=100000) == 6000
    assert rpc_deadline(method, now=1000, original_deadline=2000) == 2000


def test_ordinary_deadline_and_inference_purpose_are_distinct():
    assert rpc_deadline("conversation.read", now=1000, original_deadline=100000) == 31000
    assert rpc_deadline("conversation.read", now=1000, original_deadline=12000) == 12000
    # Pure arithmetic, explicitly not a native candidate/producer proof.
    assert rpc_deadline("inference.request", now=1000, original_deadline=100000,
        purpose_deadlines=(90000, 80000, 100000)) == 80000
    with pytest.raises(ProtocolError, match="purpose"):
        rpc_deadline("inference.request", now=1000, original_deadline=100000)
    with pytest.raises(ProtocolError, match="turn deadline changed"):
        rpc_deadline("inference.request", now=1000, original_deadline=100000,
            purpose_deadlines=(90000, 80000, 110000))
    with pytest.raises(ProtocolError, match="expired"):
        rpc_deadline("inference.request", now=1000, original_deadline=100000,
            purpose_deadlines=(1000, 80000, 100000))
    with pytest.raises(ProtocolError, match="original service deadline"):
        rpc_deadline("conversation.read", now=1000)
    with pytest.raises(ProtocolError, match="integer"):
        rpc_deadline("inference.request", now=1000, original_deadline=100000,
            purpose_deadlines=(True, 80000, 100000))


@pytest.mark.asyncio
async def test_actual_stock_ordinary_call_can_remain_held_beyond_control_window():
    from tests.test_cordis_services_bridge import _original_transport_scope
    entered, release = asyncio.Event(), asyncio.Event()
    frames = []
    class Native:
        async def dispatch(self, frame, *, original_scope):
            frames.append(frame)
            entered.set()
            await release.wait()
            return {"status": "blocked", "reason_code": "transport_fixture_no_authority",
                "memory_status": "no_learning"}
    host = CordisHost(node_path=Path(os.environ["SERAPH_CORDIS_TEST_NODE"]), service_dispatch=Native())
    waiting = None
    try:
        assert await host.start(), host.snapshot()
        original_loop = host.get_original_owner_loop()
        assert original_loop is asyncio.get_running_loop()
        scope = _original_transport_scope(host)
        scope = replace(scope, witness=MappingProxyType({**scope.witness,
            "original_deadline_at": int(time.time() * 1000) + 60000}))
        issued = int(time.time() * 1000)
        waiting = asyncio.create_task(host.request_service("conversation.read",
            {"conversation_ref": "original-conversation", "limit": 1, "before_message_ref": None},
            original_scope=scope))
        await asyncio.wait_for(entered.wait(), 3)
        assert issued + 29000 <= frames[0]["deadline_at"] <= issued + 30500
        await asyncio.sleep(5.1)  # One real held pipe call; no 30-second sleep.
        assert not waiting.done() and host.admitting
        release.set()
        assert (await waiting)["reason_code"] == "transport_fixture_no_authority"
        assert len(frames) == 1
        original_deadline = frames[0]["deadline_at"]
        await host.request_service("conversation.read",
            {"conversation_ref": "original-conversation", "limit": 1, "before_message_ref": None},
            original_scope=scope)
        assert frames[1]["deadline_at"] == original_deadline
        await host.request_service("conversation.read",
            {"conversation_ref": "original-conversation", "limit": 2, "before_message_ref": None},
            original_scope=scope)
        assert frames[2]["deadline_at"] > original_deadline + 4000
        with pytest.raises(HostBlocked, match="deadline_source_changed"):
            await host.request_service("conversation.read",
                {"conversation_ref": "original-conversation", "limit": 3, "before_message_ref": None},
                original_scope=replace(scope))
        assert len(frames) == 3
        await host.stop()
        assert host.snapshot()["cleanup"]["process_reaped"] is True
        assert host._native_owner_loop is None
        assert host._native_deadline_seals == {}
        with pytest.raises(HostBlocked, match="owner_loop_unavailable"):
            host.get_original_owner_loop()
    finally:
        release.set()
        if waiting is not None:
            await asyncio.gather(waiting, return_exceptions=True)
        await host.stop(preserve_blocked=host.state == "blocked")


@pytest.mark.asyncio
async def test_timing_capacity_and_expired_call_cannot_mint_a_new_frame():
    from tests.test_cordis_services_bridge import _original_transport_scope
    from src.runtime_plugins.contracts import SERVICE_METHODS
    from src.runtime_plugins.protocol import MAX_PENDING
    host = CordisHost(node_path=Path(os.environ["SERAPH_CORDIS_TEST_NODE"]), service_dispatch=object())
    try:
        assert await host.start(), host.snapshot()
        scope = _original_transport_scope(host)
        scope = replace(scope, witness=MappingProxyType({**scope.witness,
            "original_deadline_at": int(time.time() * 1000) + 60000}))
        now = int(time.time() * 1000)
        payload = {"conversation_ref": "original-conversation", "limit": 1, "before_message_ref": None}
        # Existing timing evidence only; none of these entries is an authority.
        for index in range(MAX_PENDING * len(SERVICE_METHODS)):
            host._native_deadline_seals[(host.boot_nonce, str(index), "conversation.read", "purpose")] = (scope, scope.deadline_at, now + 30000)
        before = host._out_seq
        with pytest.raises(HostBlocked, match="deadline_capacity_exhausted"):
            await host.request_service("conversation.read", payload, original_scope=scope)
        assert host._out_seq == before and len(host._native_deadline_seals) == MAX_PENDING * len(SERVICE_METHODS)
        host._native_deadline_seals.clear()
        host._ordinary_original_deadline("conversation.read", payload, scope, now + 1000, now=now)
        with pytest.raises(HostBlocked, match="call_deadline_expired"):
            host._ordinary_original_deadline("conversation.read", payload, scope, now + 5000, now=now + 1001)
        assert len(host._native_deadline_seals) == 1
        with pytest.raises(HostBlocked, match="call_deadline_expired"):
            host._ordinary_original_deadline("conversation.read", payload, scope, scope.deadline_at + 1,
                now=scope.deadline_at)
    finally:
        await host.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate", [None, "request-ref-only", object()])
async def test_inference_never_falls_back_to_ordinary_or_reference_authority(candidate):
    from tests.test_cordis_services_bridge import _original_transport_scope
    host = CordisHost(node_path=Path(os.environ["SERAPH_CORDIS_TEST_NODE"]), service_dispatch=object())
    try:
        assert await host.start(), host.snapshot()
        before = host._out_seq
        with pytest.raises(HostBlocked, match="native_inference_original_candidate"):
            await host.request_service("inference.request", {"request_ref": "opaque-source-ref"},
                original_scope=_original_transport_scope(host), native_inference=candidate)
        assert host._out_seq == before and host._pending == {}
    finally:
        await host.stop()
