"""Native execution is not mocked as successful by transport negatives."""
import asyncio
from types import SimpleNamespace
import time
import os
from pathlib import Path
from unittest.mock import patch, AsyncMock
from contextlib import asynccontextmanager

import pytest

from src.runtime_plugins.bridge import CordisHost, Pending
from src.runtime_plugins.dispatch import OriginalServiceInvocation, CalledServiceInvocation
from src.runtime_plugins.protocol import encode_frame


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unscoped", "boot", "sequence", "package", "epoch", "future_deadline", "capacity", "ambiguous", "completed_parent", "already_forwarded"])
async def test_child_request_requires_exact_live_original_parent_scope_before_dispatch(failure):
    calls = []
    class Native:
        async def dispatch(self, frame, *, original_scope):
            calls.append(frame)
            raise AssertionError("untrusted crossing must not reach native owner")
    host = CordisHost(service_dispatch=Native())
    host.boot_nonce = "a" * 64
    host.reviewed = SimpleNamespace(package_digest="b" * 64, composition_digest="c" * 64)
    host.state = "ready"
    host._cleanup_state = "pending"
    reader = asyncio.StreamReader()
    host.process = SimpleNamespace(returncode=None, stdout=reader)
    host._fence = lambda reason: setattr(host, "reason", reason)
    deadline = int(time.time() * 1000) + 1000
    parent = {"protocol": 1, "boot_nonce": host.boot_nonce, "request_id": "r-1", "seq": 1,
        "kind": "request", "method": "goals.read", "invocation_ref": "job:original",
        "composition_epoch": 1, "composition_digest": "c" * 64,
        "package_digest": "b" * 64, "deadline_at": deadline, "payload": {}}
    future = asyncio.get_running_loop().create_future()
    if failure != "unscoped":
        host._pending["r-1"] = Pending(parent, future, OriginalServiceInvocation({}, 1, deadline))
    if failure == "ambiguous":
        host._pending["r-2"] = Pending({**parent, "request_id": "r-2"}, future,
            OriginalServiceInvocation({}, 1, deadline))
    if failure == "completed_parent":
        future.set_result(None)
    if failure == "already_forwarded":
        host._pending["r-1"].native_forwarded = True
    child = {**parent, "request_id": "c-1"}
    if failure == "boot": child["boot_nonce"] = "d" * 64
    if failure == "sequence": child.update(seq=2, request_id="c-2")
    if failure == "package": child["package_digest"] = "d" * 64
    if failure == "epoch": child["composition_epoch"] = 2
    if failure == "future_deadline": child["deadline_at"] = deadline + 10000
    if failure == "capacity": host._unresolved = 32
    reader.feed_data(encode_frame(child))
    reader.feed_eof()
    await host._read_loop()
    assert calls == []
    assert host.reason == "protocol_rejected_or_pipe_lost"
    assert not any(key.startswith("service-") for key in host._tasks)
    future.cancel()


@pytest.mark.asyncio
async def test_lost_native_ack_fences_boot_without_retry_or_adopting_result():
    calls = []
    class Native:
        async def dispatch(self, request, *, original_scope):
            calls.append(request["invocation_ref"])
            await asyncio.sleep(1)
    host = CordisHost(service_dispatch=Native())
    host.boot_nonce = "a" * 64
    host._unresolved = 1
    host._fence = lambda reason: setattr(host, "reason", reason)
    request = {"method": "goals.read", "invocation_ref": "job:original", "deadline_at": int(time.time()*1000) + 20}
    scope = CalledServiceInvocation(OriginalServiceInvocation({}, None, host.boot_nonce),
        request["method"], 1, request["deadline_at"], "r-1", host.boot_nonce)
    future = asyncio.get_running_loop().create_future()
    host._pending["r-1"] = Pending({"request_id": "r-1"}, future, scope)
    await host._serve_native(request, "service-1", scope)
    future.cancel()
    assert calls == ["job:original"]
    assert host.reason == "native_service_failed_or_ack_lost"
    assert host._unresolved == 0


def _original_transport_scope(host):
    """Transport-negative fixture only; this does not fabricate native success."""
    from types import MappingProxyType
    from src.runtime_plugins.ownership import (RuntimeCompositionBinding, CompositionDependency,
        method_closure, method_dependencies)
    origin, branch = "conversation.accept", "direct_turn"
    methods = method_closure(origin, branch)
    domains = set(method_dependencies(origin, native_branch=branch))
    for method in methods:
        domains.update(method_dependencies(method))
    binding = RuntimeCompositionBinding("seraph.conversation.v1", origin, branch, methods,
        tuple(CompositionDependency(domain, "cordis", index + 1, "d" * 64)
            for index, domain in enumerate(sorted(domains))),
        host.reviewed.package_digest, host.reviewed.composition_digest)
    witness = MappingProxyType({"invocation_ref": "job:original", "package_digest": host.reviewed.package_digest,
        "host_composition_digest": host.reviewed.composition_digest,
        "original_deadline_at": int(time.time() * 1000) + 4000})
    return OriginalServiceInvocation(witness, binding, host.boot_nonce)


@pytest.mark.asyncio
async def test_called_method_uses_original_child_epoch_and_serializes_original_invocation():
    from src.runtime_plugins.bridge import HostBlocked
    host = CordisHost(service_dispatch=object())
    host.boot_nonce = "a" * 64
    host.reviewed = SimpleNamespace(package_digest="b" * 64, composition_digest="c" * 64)
    host.state, host._cleanup_state = "ready", "pending"
    host.process = SimpleNamespace(returncode=None)
    scope = _original_transport_scope(host)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def controlled_rpc(method, **kwargs):
        calls.append((method, kwargs))
        entered.set()
        await release.wait()
        return {"payload": {"status": "blocked", "reason_code": "native_fixture_denied", "memory_status": "no_learning"}}
    with patch.object(host, "_rpc", controlled_rpc):
        original = asyncio.create_task(host.request_service("audit.append", {"event_ref": "candidate:original"}, original_scope=scope))
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(HostBlocked, match="native_original_scope_already_pending"):
            await host.request_service("audit.append", {"event_ref": "candidate:original"}, original_scope=scope)
        release.set()
        assert (await original)["status"] == "blocked"
    assert len(calls) == 1
    assert calls[0][1]["composition_epoch"] == scope.binding.epoch_for("audit.append")
    assert calls[0][1]["composition_epoch"] != scope.binding.called_epoch
    assert calls[0][1]["native_binding"] is scope
    assert host._native_inflight == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["lease", "authentication"])
async def test_native_precondition_denial_returns_redacted_block_without_lost_ack(failure):
    from src.runtime_plugins.dispatch import NativeServiceDispatcher
    from src.workflows.job_runtime import DurableJobLeaseError
    from src.auth.service import AuthFailure
    host = CordisHost()
    host.boot_nonce = "a" * 64
    host.reviewed = SimpleNamespace(package_digest="b" * 64, composition_digest="c" * 64)
    scope = _original_transport_scope(host)
    @asynccontextmanager
    async def no_contact_session():
        yield object()
    native = NativeServiceDispatcher(jobs=SimpleNamespace(_session=no_contact_session))
    error = DurableJobLeaseError("private-owner-detail") if failure == "lease" else AuthFailure("private-owner-detail")
    call = CalledServiceInvocation(scope, "conversation.accept", scope.binding.epoch_for("conversation.accept"),
        int(time.time() * 1000) + 1000, "r-1", host.boot_nonce)
    frame = {"method": call.method, "payload": {"turn_ref": "job:original"},
        "invocation_ref": "job:original", "composition_epoch": call.composition_epoch,
        "package_digest": "b" * 64, "composition_digest": "c" * 64,
        "boot_nonce": host.boot_nonce, "deadline_at": call.deadline_at}
    with patch.object(native, "_current_in_db", AsyncMock(side_effect=error)):
        result = await native.dispatch(frame, original_scope=call)
    assert result == {"status": "blocked", "reason_code": "native_original_job_authority_changed", "memory_status": "no_learning"}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["boot", "package", "composition", "method"])
async def test_original_scope_changes_block_before_frame_write(change):
    from src.runtime_plugins.bridge import HostBlocked
    host = CordisHost(service_dispatch=object())
    host.boot_nonce = "a" * 64
    host.reviewed = SimpleNamespace(package_digest="b" * 64, composition_digest="c" * 64)
    host.state, host._cleanup_state = "ready", "pending"
    host.process = SimpleNamespace(returncode=None)
    scope = _original_transport_scope(host)
    method, payload = "audit.append", {"event_ref": "candidate:original"}
    if change == "boot": host.boot_nonce = "e" * 64
    if change == "package": host.reviewed.package_digest = "e" * 64
    if change == "composition": host.reviewed.composition_digest = "e" * 64
    if change == "method": method, payload = "artifacts.adopt", {"request_ref": "candidate:other"}
    with patch.object(host, "_rpc") as rpc:
        with pytest.raises(HostBlocked):
            await host.request_service(method, payload, original_scope=scope)
    rpc.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown_ack", [True, False])
async def test_actual_host_post_ack_wait_timeout_requires_original_ack_and_positive_reap(shutdown_ack):
    from src.runtime_plugins.bridge import HostBlocked
    host = CordisHost(node_path=Path(os.environ["SERAPH_CORDIS_TEST_NODE"]))
    try:
        assert await host.start(), host.snapshot()
        process = host.process
        pid = process.pid
        real_wait = process.wait
        real_rpc = host._rpc
        waits = []
        acks = []
        async def injected_wait():
            waits.append(True)
            if shutdown_ack and len(waits) == 1:
                # Inject only the post-ACK observation timeout. The original
                # child is real, and fallback must actually positively reap it.
                raise asyncio.TimeoutError
            return await real_wait()
        async def observed_rpc(method, **kwargs):
            if method == "runtime.shutdown" and not shutdown_ack:
                raise HostBlocked("injected_missing_original_shutdown_ack")
            response = await real_rpc(method, **kwargs)
            if method == "runtime.shutdown":
                acks.append(response["payload"])
            return response
        with patch.object(process, "wait", injected_wait), patch.object(host, "_rpc", observed_rpc):
            await host.stop()
        receipt = host.snapshot()["cleanup"]
        assert receipt["state"] == "clean"
        assert receipt["process_reaped"] is True
        assert receipt["resources_remaining"] == 0
        assert receipt["cordis_disposal"] == ("confirmed" if shutdown_ack else "unconfirmed")
        assert bool(acks) is shutdown_ack
        if shutdown_ack:
            assert acks[0]["cordis_disposal"] == "confirmed"
            assert acks[0]["resources_remaining"] == 0
            assert len(waits) >= 2
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        await host.stop(preserve_blocked=host.state == "blocked")


def test_managed_constructor_is_pure_and_default_host_keeps_explicit_dispatch():
    with patch("src.runtime_plugins.bridge.reviewed_composition") as preflight, \
         patch("src.runtime_plugins.dispatch.NativeServiceDispatcher") as dispatcher:
        managed = CordisHost(native_services=True)
        explicit = object()
        ordinary = CordisHost(service_dispatch=explicit)
        assert managed.service_dispatch is None and managed.process is None
        assert ordinary.service_dispatch is explicit
        preflight.assert_not_called()
        dispatcher.assert_not_called()


@pytest.mark.asyncio
async def test_actual_managed_singleton_lazily_installs_native_owner_and_reaps(monkeypatch):
    from src.runtime_plugins.bridge import cordis_host
    from src.runtime_plugins.dispatch import NativeServiceDispatcher
    assert cordis_host._native_services is True
    assert cordis_host.process is None
    monkeypatch.setattr(cordis_host, "node_path", Path(os.environ["SERAPH_CORDIS_TEST_NODE"]))
    try:
        assert await cordis_host.start(), cordis_host.snapshot()
        assert type(cordis_host.service_dispatch) is NativeServiceDispatcher
        assert cordis_host.reviewed is not None and cordis_host.boot_nonce
        assert cordis_host.admitting
        services = cordis_host.snapshot()["native_services"]
        assert services["state"] == "partial"
        assert set(services["supported_methods"]) == NativeServiceDispatcher.supported_methods
        assert "memory.propose" in services["blocked_methods"]
        assert len(services["supported_methods"]) + len(services["blocked_methods"]) == 34
    finally:
        await cordis_host.stop(preserve_blocked=cordis_host.state == "blocked")
        if cordis_host._cleanup_task is not None:
            await cordis_host._cleanup_task
        cleanup = cordis_host.snapshot()["cleanup"]
        assert cleanup["process_reaped"] is True
        assert cleanup["resources_remaining"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["job:original", {"invocation_ref": "job:original"},
    OriginalServiceInvocation({"method": "tasks.inspect", "invocation_ref": "job:original"}, 1, 2**53)])
async def test_service_request_cannot_refresh_scope_from_job_locator_or_change_method(scope):
    calls = []
    class Native:
        async def frame_binding(self, *args):
            calls.append(args)
            raise AssertionError("transport cannot refresh original native authority")
    host = CordisHost(service_dispatch=Native())
    host.state = "ready"
    host._cleanup_state = "pending"
    host.process = SimpleNamespace(returncode=None)
    from src.runtime_plugins.bridge import HostBlocked
    with pytest.raises(HostBlocked, match="native_original_scope_missing_or_method_changed"):
        await host.request_service("goals.read", {}, original_scope=scope)
    assert calls == []
    assert host._pending == {}
    assert host._unresolved == 0
