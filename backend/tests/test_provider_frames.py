"""#1024 A3.1: mandatory provider facades, fixed dependencies and host-to-child provider frames."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import time
from types import SimpleNamespace

import pytest

from src.runtime_plugins.bridge import CordisHost
from src.runtime_plugins.composition import (KNOWN_PLUGIN, MANDATORY_PLUGINS, PACKAGE_ROOT, PLUGIN_DEPENDENCIES,
                                             validate_profile)
from src.runtime_plugins.protocol import ProtocolError, encode_frame, validate_frame, validate_provider_report
from src.runtime_plugins.worker import DEPENDENCIES
from tests.test_cordis_services_core import NODE, needs_host, service_db  # noqa: F401


def _profile():
    return json.loads((PACKAGE_ROOT / "profile.json").read_text())


def test_profile_cannot_drop_or_relax_the_python_hosted_facades():
    assert MANDATORY_PLUGINS == (KNOWN_PLUGIN, "seraph.state.v1", "seraph.authority.v1", "seraph.audit.v1")
    for identifier in MANDATORY_PLUGINS[1:]:
        missing = _profile()
        missing["plugins"] = [spec for spec in missing["plugins"] if spec["id"] != identifier]
        with pytest.raises(ProtocolError, match="required provider missing"):
            validate_profile(missing)
        optional = _profile()
        next(spec for spec in optional["plugins"] if spec["id"] == identifier)["required"] = False
        with pytest.raises(ProtocolError, match="required provider missing"):
            validate_profile(optional)
    # Other providers may still be optional; their methods return typed blocks.
    relaxed = _profile()
    next(spec for spec in relaxed["plugins"] if spec["id"] == "seraph.memory.v1")["required"] = False
    assert validate_profile(relaxed) is relaxed


@pytest.mark.parametrize("identifier, dependencies", [
    ("seraph.memory.v1", []), ("seraph.authority.v1", ["seraph.state.v1", "seraph.audit.v1"]),
    ("seraph.state.v1", ["seraph.authority.v1"]), ("seraph.research.v1", ["seraph.state.v1"]),
    ("seraph.audit.v1", [])])
def test_provider_dependencies_are_declared_and_fixed(identifier, dependencies):
    changed = _profile()
    next(spec for spec in changed["plugins"] if spec["id"] == identifier)["dependencies"] = dependencies
    with pytest.raises(ProtocolError):
        validate_profile(changed)


def test_profile_dependencies_mirror_the_worker_activation_order():
    declared = {spec["id"]: tuple(spec["dependencies"]) for spec in _profile()["plugins"]}
    assert {key: value for key, value in declared.items() if value} == PLUGIN_DEPENDENCIES
    for provider, required in DEPENDENCIES.items():
        assert declared[f"seraph.{provider}.v1"] == tuple(f"seraph.{name}.v1" for name in required)


def _frame(method="provider.readiness", kind="request", payload=None):
    return {"protocol": 1, "boot_nonce": "a" * 64, "request_id": "r-2", "seq": 2, "kind": kind, "method": method,
            "invocation_ref": None, "composition_epoch": None, "composition_digest": "b" * 64,
            "package_digest": "c" * 64, "deadline_at": 2_000_000_000_000,
            "payload": payload if payload is not None else {
                "domain": "seraph.state.v1", "state": "active", "epoch": 1, "composition_digest": "d" * 64,
                "reason_code": None}}


@pytest.mark.parametrize("change", [{"database_handle": "x"}, {"vault_key": "k"}, {"credentials": "c"},
                                    {"domain": "seraph.research.v1"}, {"state": "quiescing"}, {"epoch": 0},
                                    {"reason_code": "has spaces"}, {"composition_digest": "short"}])
def test_provider_frames_are_closed_and_carry_no_handle_key_or_credential(change):
    payload = {**_frame()["payload"], **change}
    with pytest.raises(ProtocolError):
        validate_frame(_frame(payload=payload))


def test_provider_frame_directions_and_acknowledgements():
    assert validate_frame(_frame())["method"] == "provider.readiness"
    quiesce = {**_frame()["payload"], "state": "quiescing"}
    assert validate_frame(_frame("provider.quiesce", payload=quiesce))["method"] == "provider.quiesce"
    with pytest.raises(ProtocolError):
        validate_frame(_frame("provider.quiesce"))
    assert validate_frame(_frame(kind="response", payload={"domain": "seraph.state.v1", "state": "recorded"}))
    with pytest.raises(ProtocolError):
        validate_frame(_frame(kind="response", payload={"domain": "seraph.state.v1", "state": "active"}))
    assert validate_provider_report(_frame()["payload"], "provider.readiness")["domain"] == "seraph.state.v1"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["provider.readiness", "provider.quiesce"])
async def test_a_child_sent_provider_frame_fences_the_host(method):
    """Frames flow host to child only; a child provider request never reaches a provider."""
    host = CordisHost(service_dispatch=SimpleNamespace(dispatch=None))
    host.boot_nonce = "a" * 64
    host.reviewed = SimpleNamespace(package_digest="c" * 64, composition_digest="b" * 64)
    host.state, host._cleanup_state = "ready", "pending"
    reader = asyncio.StreamReader()
    host.process = SimpleNamespace(returncode=None, stdout=reader)
    host._fence = lambda reason: setattr(host, "reason", reason)
    payload = _frame()["payload"] if method == "provider.readiness" else {**_frame()["payload"], "state": "quiescing"}
    reader.feed_data(encode_frame({**_frame(method, payload=payload), "seq": 1, "request_id": "c-1",
                                   "deadline_at": int(time.time() * 1000) + 1000}))
    reader.feed_eof()
    await host._read_loop()
    assert host.reason == "protocol_rejected_or_pipe_lost"


@needs_host
@pytest.mark.asyncio
async def test_real_child_records_host_reports_and_quiesces_them_at_stop(service_db, tmp_path):
    from pathlib import Path
    from src.runtime_plugins.worker import ProviderWorker
    worker = ProviderWorker()
    for name in worker.status:
        worker.status[name].state = "active"
    worker.status["memory"].state, worker.status["memory"].reason_code = "degraded", "memory_tables_missing"
    reports = await worker.provider_reports()
    assert {item["domain"] for item in reports} == {
        "seraph.state.v1", "seraph.authority.v1", "seraph.audit.v1", "seraph.tasks.v1", "seraph.goals.v1",
        "seraph.memory.v1", "seraph.artifacts.v1"}
    assert all(set(item) == {"domain", "state", "epoch", "composition_digest", "reason_code"} for item in reports)
    host = CordisHost(node_path=Path(NODE))
    sent = []
    original = host._rpc

    async def observe(method, **kwargs):
        sent.append((method, deepcopy(kwargs.get("payload"))))
        return await original(method, **kwargs)
    host._rpc = observe
    try:
        assert await host.start(), host.snapshot()
        acknowledged = await host.report_providers(reports)
        assert acknowledged == [{"domain": item["domain"], "state": "recorded"} for item in reports]
        # A report outside the closed schema never leaves the host.
        with pytest.raises(ProtocolError):
            await host.report_providers([{**reports[0], "vault_key": "secret"}])
    finally:
        await host.stop()
    assert host.snapshot()["cleanup"]["process_reaped"] is True
    quiesced = [payload for method, payload in sent if method == "provider.quiesce"]
    assert {item["domain"] for item in quiesced} == {item["domain"] for item in reports}
    assert all(item["state"] == "quiescing" and item["reason_code"] == "host_stopping" for item in quiesced)
    order = [method for method, _ in sent]
    assert order.index("runtime.quiesce") > max(i for i, method in enumerate(order) if method == "provider.quiesce")
