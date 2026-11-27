"""#1024 A3.1: facade activation of Cordis-owned domains and the in-process maintenance handoff.

Facade activation: while ``seraph.goals/memory/artifacts.v1`` is Cordis-owned,
the facade's answer to a host-sent ``provider.activate`` at the owner epoch is
the decision and the Python provider the mechanism. Maintenance: the running
backend closes its state gate, drains, and runs a backup under the owner lease
it already holds. Temporary state only; external network and inference are
never contacted (the real-child tests deny transport through ``service_db``).
"""
from __future__ import annotations

import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace
import zipfile

import pytest
from sqlalchemy import select, update

from config.settings import settings
from src.db import engine as db_engine
from src.db.engine import StateProviderUnavailable, get_session, set_state_gate, state_gate
from src.db.models import AuditEvent, Goal, Memory, RuntimeCompositionState
from src.runtime_plugins import worker as worker_module
from src.runtime_plugins.bridge import CordisHost
from src.runtime_plugins.ownership import (CompositionDependency, composition_writer, inventory,
                                           seed_legacy_inventory_sync, transition_owner)
from src.runtime_plugins.protocol import (FACADE_DOMAINS, ProtocolError, encode_frame, validate_facade_response,
                                          validate_frame)
from src.runtime_plugins.worker import (FACADE_PROVIDERS, MANDATORY, ProviderWorker, StateMaintenanceRefused, attach,
                                        domain_provider_unavailable)
from src.workspace.lifecycle import _LIFECYCLE_FENCE_DEPTH, lifecycle_fence_marker, restore_workspace
from tests.support.db import create_test_database, install_database, remove_test_database
from tests.test_cordis_services_core import (_MEMORY_READ, _PRIVATE_TEXT, _operator, _persisted_copies,  # noqa: F401
                                             _private_memory, live_host, needs_host, service_db)

DIGEST = "d" * 64
OTHER = "e" * 64


@pytest.fixture(autouse=True)
def _detached_worker():
    set_state_gate(None)
    worker_module._set_current(None)
    yield
    set_state_gate(None)
    worker_module._set_current(None)


def _frame(method, payload, kind="request"):
    return {"protocol": 1, "boot_nonce": "a" * 64, "request_id": "r-2", "seq": 2, "kind": kind, "method": method,
            "invocation_ref": None, "composition_epoch": None, "composition_digest": "b" * 64,
            "package_digest": "c" * 64, "deadline_at": 2_000_000_000_000, "payload": payload}


_ACTIVATE = {"domain": "seraph.memory.v1", "owner_kind": "cordis", "epoch": 2, "composition_digest": DIGEST}


# ------------------------------------------------------------------ closed facade frames

@pytest.mark.parametrize("change", [
    {"domain": "seraph.state.v1"}, {"domain": "seraph.authority.v1"}, {"domain": "seraph.audit.v1"},
    {"domain": "seraph.tasks.v1"}, {"domain": "seraph.vault.v1"}, {"domain": "seraph.research.v1"},
    {"owner_kind": "legacy"}, {"epoch": 0}, {"composition_digest": "short"},
    {"database_handle": "x"}, {"vault_key": "k"}, {"credentials": "c"}])
def test_facade_activation_frames_are_closed_and_never_name_a_pinned_domain(change):
    """No handle, key or credential crosses IPC, and no pinned trust provider is activatable."""
    assert FACADE_DOMAINS == {f"seraph.{name}.v1" for name in FACADE_PROVIDERS}
    assert validate_frame(_frame("provider.activate", _ACTIVATE))["method"] == "provider.activate"
    with pytest.raises(ProtocolError):
        validate_frame(_frame("provider.activate", {**_ACTIVATE, **change}))


def test_facade_answers_are_closed():
    activated = {"domain": "seraph.memory.v1", "state": "activated", "epoch": 2, "plugin_digest": DIGEST,
                 "reason_code": None}
    assert validate_facade_response(activated, "provider.activate") == activated
    unavailable = {**activated, "state": "unavailable", "plugin_digest": None, "reason_code": "facade_not_composed"}
    assert validate_facade_response(unavailable, "provider.activate") == unavailable
    for bad in ({**activated, "reason_code": "x"}, {**activated, "plugin_digest": None},
                {**unavailable, "reason_code": None}, {**activated, "state": "active"},
                {**activated, "domain": "seraph.state.v1"}, {**activated, "vault_key": "k"}):
        with pytest.raises(ProtocolError):
            validate_facade_response(bad, "provider.activate")
    deactivate = {"domain": "seraph.goals.v1", "epoch": 3, "reason_code": "host_stopping"}
    assert validate_frame(_frame("provider.deactivate", deactivate))["payload"] == deactivate
    with pytest.raises(ProtocolError):
        validate_frame(_frame("provider.deactivate", {**deactivate, "domain": "seraph.audit.v1"}))
    assert validate_frame(_frame("provider.deactivate", {"domain": "seraph.goals.v1", "state": "deactivated",
                                                         "epoch": 3}, kind="response"))


@pytest.mark.asyncio
@pytest.mark.parametrize("method, payload", [
    ("provider.activate", _ACTIVATE),
    ("provider.deactivate", {"domain": "seraph.memory.v1", "epoch": 2, "reason_code": "x"})])
async def test_a_child_sent_facade_frame_fences_the_host(method, payload):
    """Activation frames flow host to child only; a child request never drives a provider."""
    host = CordisHost(service_dispatch=SimpleNamespace(dispatch=None))
    host.boot_nonce = "a" * 64
    host.reviewed = SimpleNamespace(package_digest="c" * 64, composition_digest="b" * 64)
    host.state, host._cleanup_state = "ready", "pending"
    reader = asyncio.StreamReader()
    host.process = SimpleNamespace(returncode=None, stdout=reader)
    host._fence = lambda reason: setattr(host, "reason", reason)
    reader.feed_data(encode_frame({**_frame(method, payload), "seq": 1, "request_id": "c-1",
                                   "deadline_at": int(time.time() * 1000) + 1000}))
    reader.feed_eof()
    await host._read_loop()
    assert host.reason == "protocol_rejected_or_pipe_lost"


# ------------------------------------------------------------------ facade decision (temporary DB)

class _FakeHost:
    """The facade side of one boot: answers ``provider.activate`` as configured."""

    def __init__(self, *, digest=DIGEST, answer="activated", nonce="1" * 64):
        self.boot_nonce, self.state = nonce, "starting"
        self.reviewed = SimpleNamespace(plugin_digests={domain: digest for domain in FACADE_DOMAINS})
        self.answer, self.requests, self.deactivated = answer, [], []

    async def activate_facade(self, request):
        self.requests.append(dict(request))
        if self.answer == "activated":
            return {"domain": request["domain"], "state": "activated", "epoch": request["epoch"],
                    "plugin_digest": self.answer_digest if hasattr(self, "answer_digest") else request["composition_digest"],
                    "reason_code": None}
        return {"domain": request["domain"], "state": "unavailable", "epoch": request["epoch"], "plugin_digest": None,
                "reason_code": "facade_not_composed"}

    async def deactivate_facade(self, domain, epoch, reason_code):
        self.deactivated.append((domain, epoch, reason_code))


async def _cordis_owned(db, name, *, epoch=2, digest=DIGEST, state="ready"):
    async with db() as session:
        await session.execute(update(RuntimeCompositionState).where(
            RuntimeCompositionState.runtime_domain == f"seraph.{name}.v1").values(
                owner_kind="cordis", epoch=epoch, composition_digest=digest, state=state))


def _dependency(name, *, epoch=2, digest=DIGEST, owner="cordis"):
    return CompositionDependency(f"seraph.{name}.v1", owner, epoch, digest)


@pytest.mark.asyncio
async def test_a_cordis_owned_facade_domain_activates_only_on_its_facade_answer(db):
    worker = attach(ProviderWorker())
    await worker.activate()
    await _cordis_owned(db, "memory")
    await worker.readiness()
    status = {item["provider"]: item for item in worker.snapshot()["providers"]}
    # Legacy-owned facade domains need no facade: Python decides.
    assert status["goals"]["state"] == "active" and status["goals"]["facade"] == "not_required"
    assert status["memory"]["state"] == "awaiting_facade" and status["memory"]["facade"] == "inactive"
    assert worker.snapshot()["mandatory_ready"] is True, "a facade domain never gates the mandatory set"
    dep = (_dependency("memory"),)
    assert domain_provider_unavailable("seraph.memory.v1", vector=dep, boot_nonce="1" * 64) == \
        "native_provider_memory_unavailable"
    host = _FakeHost()
    results = await worker.activate_facades(host)
    assert [item["domain"] for item in results] == ["seraph.memory.v1"], "only the Cordis-owned domain is asked"
    assert host.requests == [{"domain": "seraph.memory.v1", "owner_kind": "cordis", "epoch": 2,
                              "composition_digest": DIGEST}]
    assert worker.status["memory"].state == "active"
    assert domain_provider_unavailable("seraph.memory.v1", vector=dep, boot_nonce=host.boot_nonce) is None
    # The receipt is this boot's, at exactly the bound epoch and digest.
    assert domain_provider_unavailable("seraph.memory.v1", vector=dep, boot_nonce="2" * 64) == \
        "native_provider_memory_facade_inactive"
    assert domain_provider_unavailable("seraph.memory.v1", vector=(_dependency("memory", epoch=3),),
                                       boot_nonce=host.boot_nonce) == "native_provider_memory_facade_inactive"
    # The answering boot stops: the provider awaits a fresh activation.
    host.state = "stopped"
    assert {item["provider"]: item["state"] for item in worker.snapshot()["providers"]}["memory"] == "awaiting_facade"
    host.state = "ready"
    await worker.deactivate_facades("provider_deactivated")
    assert host.deactivated == [("seraph.memory.v1", 2, "provider_deactivated")]
    assert worker.status["memory"].state == "awaiting_facade"
    assert domain_provider_unavailable("seraph.memory.v1", vector=dep, boot_nonce=host.boot_nonce) == \
        "native_provider_memory_unavailable"
    # Mandatory providers are never asked or touched by facade activation.
    assert all(worker.status[name].state == "active" for name in MANDATORY)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unavailable", "foreign_digest", "unreviewed_build", "owner_not_ready"])
async def test_a_facade_answer_that_does_not_name_the_owner_row_never_activates(db, case):
    worker = attach(ProviderWorker())
    await worker.activate()
    await _cordis_owned(db, "artifacts", state="blocked" if case == "owner_not_ready" else "ready")
    host = _FakeHost(answer="unavailable" if case == "unavailable" else "activated",
                     digest=OTHER if case == "unreviewed_build" else DIGEST)
    if case == "foreign_digest":
        host.answer_digest = OTHER
    await worker.activate_facades(host)
    assert worker.status["artifacts"].state == "awaiting_facade"
    assert worker.status["artifacts"].reason_code == {
        "unavailable": "provider_facade_not_composed", "foreign_digest": "provider_facade_digest_mismatch",
        "unreviewed_build": "provider_facade_digest_mismatch",
        "owner_not_ready": "provider_facade_owner_not_ready"}[case]
    assert domain_provider_unavailable("seraph.artifacts.v1", vector=(_dependency("artifacts"),),
                                       boot_nonce=host.boot_nonce) == "native_provider_artifacts_unavailable"
    if case in {"foreign_digest", "unreviewed_build"}:
        # A facade that activated against the wrong digest is told to stop forwarding.
        assert host.deactivated == [("seraph.artifacts.v1", 2, "facade_digest_mismatch")]
    if case == "owner_not_ready":
        assert host.requests == [], "a blocked owner row is never offered to the facade"


@pytest.mark.asyncio
async def test_a_rollback_to_legacy_returns_the_decision_to_python(db):
    worker = attach(ProviderWorker())
    await worker.activate()
    await _cordis_owned(db, "goals")
    await worker.readiness()
    assert worker.status["goals"].state == "awaiting_facade"
    async with db() as session:
        await session.execute(update(RuntimeCompositionState).where(
            RuntimeCompositionState.runtime_domain == "seraph.goals.v1").values(owner_kind="legacy", epoch=3))
    host = _FakeHost()
    assert await worker.activate_facades(host) == [] and host.requests == []
    assert worker.status["goals"].state == "active"
    await worker.readiness()
    assert worker.status["goals"].state == "active"


@pytest.mark.asyncio
async def test_a_facade_provider_whose_predicate_fails_is_never_offered(db, monkeypatch):
    async def missing():
        raise worker_module.ProviderActivationError("memory", "memory_tables_missing")
    monkeypatch.setitem(worker_module._PREDICATES, "memory", missing)
    worker = attach(ProviderWorker())
    await worker.activate()
    await _cordis_owned(db, "memory")
    host = _FakeHost()
    await worker.activate_facades(host)
    assert host.requests == [] and worker.status["memory"].state == "degraded"


# ------------------------------------------------------------------ facade decision (real child)

async def _cut_memory_over(host):
    """Move the memory owner to the running child's reviewed plugin through the fenced CAS."""
    digest = host.reviewed.plugin_digests["seraph.memory.v1"]
    with lifecycle_fence_marker():
        for step in ("draining", "blocked", "ready"):
            async with get_session() as session:
                async with composition_writer(session):
                    row = (await inventory(session))["seraph.memory.v1"]
                    expected = CompositionDependency(row.runtime_domain, row.owner_kind, row.epoch,
                                                     row.composition_digest)
                    target = ((expected.owner_kind, expected.epoch, expected.composition_digest) if step == "draining"
                              else ("cordis", expected.epoch + 1, digest) if step == "blocked"
                              else ("cordis", expected.epoch, digest))
                    await transition_owner(session, runtime_domain="seraph.memory.v1", expected=expected,
                                           expected_state=row.state, owner_kind=target[0], epoch=target[1],
                                           composition_digest=target[2], state=step, boot_host=host)
    return digest


def _active_worker():
    worker = attach(ProviderWorker())
    for name in worker.status:
        worker.status[name].state = "active"
    worker._activated = True
    return worker


@needs_host
@pytest.mark.asyncio
async def test_real_child_facade_decides_a_cordis_owned_memory_read_in_both_processes(live_host):
    from src.runtime_plugins.dispatch import NativeServiceBlocked
    from src.runtime_plugins.service_jobs import execute_native_read
    host, jobs, calls = live_host
    operator = await _operator()
    await _private_memory(operator)
    await _cut_memory_over(host)
    worker = _active_worker()
    await worker._read_facade_rows()
    worker._facade_passed("memory")
    assert worker.status["memory"].state == "awaiting_facade"
    # Python: no activation in this boot yet, so the read is refused before any bytes.
    with pytest.raises(NativeServiceBlocked) as denied:
        await execute_native_read(operator=operator, candidate=_MEMORY_READ, idempotency_key="no-facade",
                                  host=host, jobs=jobs)
    assert denied.value.reason_code == "native_provider_memory_unavailable"
    # The real facade answers at the owner epoch with the reviewed digest.
    results = await worker.activate_facades(host)
    assert [(item["domain"], item["state"]) for item in results] == [("seraph.memory.v1", "active")]
    result = await execute_native_read(operator=operator, candidate=_MEMORY_READ, idempotency_key="facade-on",
                                       host=host, jobs=jobs)
    assert result is not None
    # Child: a deactivated facade forwards nothing, so no callback reaches Python.
    await worker.deactivate_facades("provider_deactivated")
    before = len(calls)
    with pytest.raises(NativeServiceBlocked) as denied:
        await execute_native_read(operator=operator, candidate=_MEMORY_READ, idempotency_key="facade-off",
                                  host=host, jobs=jobs)
    assert denied.value.reason_code == "provider_facade_inactive"
    assert len(calls) == before, "the child refused before any callback"
    # Python: the child's facade is active again but Python dropped its receipt.
    await worker.activate_facades(host)
    worker._facades.clear()
    with pytest.raises(NativeServiceBlocked) as denied:
        await execute_native_read(operator=operator, candidate=_MEMORY_READ, idempotency_key="receipt-gone",
                                  host=host, jobs=jobs)
    assert denied.value.reason_code == "native_provider_memory_facade_inactive"
    assert _persisted_copies(_PRIVATE_TEXT) == []


@needs_host
@pytest.mark.asyncio
async def test_a_child_restart_needs_a_fresh_facade_activation_before_admission(live_host):
    host, jobs, calls = live_host
    await _cut_memory_over(host)
    worker = _active_worker()
    await worker.activate_facades(host)
    first_boot = host.boot_nonce
    assert worker.status["memory"].state == "active"
    seen = []
    original = worker.activate_facades

    async def observe(target):
        seen.append((target.state, target.admitting))
        return await original(target)
    host.facade_activation_source = observe
    await host.stop()
    assert {item["provider"]: item["state"] for item in worker.snapshot()["providers"]}["memory"] == "awaiting_facade"
    assert await host.start(), host.snapshot()
    assert host.boot_nonce != first_boot
    assert seen == [("starting", False)], "activation runs before the new boot admits anything"
    assert worker._facades["memory"]["boot_nonce"] == host.boot_nonce
    assert worker.status["memory"].state == "active"


# ------------------------------------------------------------------ maintenance handoff

@pytest.mark.asyncio
async def test_maintenance_refuses_typed_before_closing_anything(db):
    worker = attach(ProviderWorker())
    with pytest.raises(StateMaintenanceRefused, match="state_maintenance_unavailable"):
        async with worker.maintenance("backup"):
            pass
    await worker.activate()
    for operation, reason in (("restore", "state_maintenance_operation_unsupported"),
                              ("rollback", "state_maintenance_operation_unsupported")):
        with pytest.raises(StateMaintenanceRefused, match=reason):
            async with worker.maintenance(operation):
                pass
    production = attach(ProviderWorker(hold_workspace_lock=True))
    production._activated = True
    for name in production.status:
        production.status[name].state = "active"
    with pytest.raises(StateMaintenanceRefused, match="state_maintenance_lease_missing"):
        async with production.maintenance("backup"):
            pass
    attach(worker)
    async with worker.maintenance("backup"):
        with pytest.raises(StateMaintenanceRefused, match="state_maintenance_in_progress"):
            async with worker.maintenance("backup"):
                pass
    assert state_gate() == ("active", None)
    worker.status["audit"].state = "failed"
    with pytest.raises(StateMaintenanceRefused, match="state_maintenance_unavailable"):
        async with worker.maintenance("backup"):
            pass
    assert state_gate() == ("active", None), "a refusal closes nothing"


@pytest.mark.asyncio
async def test_maintenance_closes_the_gate_drains_open_sessions_then_reopens(db, monkeypatch):
    worker = attach(ProviderWorker())
    await worker.activate()
    held, release, drain_waiting, ran = asyncio.Event(), asyncio.Event(), asyncio.Event(), []
    original_drain = db_engine.drain_open_sessions

    async def observed_drain(wake=None):
        drain_waiting.set()
        await original_drain(wake)
    monkeypatch.setattr(db_engine, "drain_open_sessions", observed_drain)

    async def holder():
        async with get_session() as session:
            await session.execute(select(Goal.id).limit(1))
            held.set()
            await release.wait()

    async def maintain():
        async with worker.maintenance("backup"):
            ran.append((state_gate(), _LIFECYCLE_FENCE_DEPTH.get() > 0,
                        domain_provider_unavailable("seraph.memory.v1", "seraph.state.v1")))
            with pytest.raises(StateProviderUnavailable) as refused:
                async with get_session():
                    pass
            ran.append(refused.value.reason_code)

    holding = asyncio.create_task(holder())
    await held.wait()
    maintaining = asyncio.create_task(maintain())
    await drain_waiting.wait()
    # The gate closed before the drain: new work is refused typed, never queued.
    assert state_gate() == ("maintenance", "state_provider_maintenance")
    with pytest.raises(StateProviderUnavailable) as refused:
        async with get_session():
            pass
    assert refused.value.reason_code == "state_provider_maintenance"
    assert ran == [], "the step waits for the open session"
    release.set()
    await holding
    await maintaining
    assert ran == [(("maintenance", "state_provider_maintenance"), True, "native_provider_state_unavailable"),
                   "state_provider_maintenance"]
    assert state_gate() == ("active", None)
    assert worker.snapshot()["mandatory_ready"] is True and worker.snapshot()["maintenance"] is None
    async with get_session() as session:
        assert (await session.execute(select(Goal.id).limit(1))).all() == []


@pytest.mark.asyncio
async def test_a_mandatory_provider_failing_after_maintenance_leaves_the_gate_closed(db, monkeypatch):
    worker = attach(ProviderWorker())
    await worker.activate()

    async def audit_gone():
        raise worker_module.ProviderActivationError("audit", "audit_write_unavailable")
    async with worker.maintenance("backup"):
        monkeypatch.setitem(worker_module._PREDICATES, "audit", audit_gone)
    assert state_gate() == ("failed", "audit_write_unavailable")
    assert worker.status["audit"].state == "failed"
    with pytest.raises(StateProviderUnavailable):
        async with get_session():
            pass
    assert domain_provider_unavailable("seraph.memory.v1", "seraph.audit.v1") == "native_provider_audit_unavailable"


def _canonical_workspace(root: Path) -> None:
    for name in ("artifacts", "extensions", "skills", "workflows", "runbooks", "plans", "reports", "notes"):
        (root / name).mkdir(exist_ok=True)
    (root / "soul.md").write_text("soul\n", encoding="utf-8")
    for name in ("mcp-servers.json", "stdio-proxies.json", "extensions-state.json", "starter-packs.json",
                 "model-fabric-settings.json", "screen-analysis-settings.json"):
        (root / name).write_text("{}\n", encoding="utf-8")
    from cryptography.fernet import Fernet
    key = root / ".vault-key"
    key.write_bytes(Fernet.generate_key())
    os.chmod(key, 0o600)


@pytest.fixture
async def workspace_db(tmp_path, _db_template_directory, monkeypatch):
    """A temporary canonical workspace whose ``seraph.db`` is the default database."""
    from src.utils.background import drain_tracked_tasks, join_tracked_threads
    root = tmp_path / "workspace"
    root.mkdir()
    _canonical_workspace(root)
    monkeypatch.setattr(settings, "workspace_dir", str(root))
    monkeypatch.setattr(settings, "vault_encryption_key", "")
    database = await create_test_database(root / "seraph.db", template_directory=_db_template_directory)
    install_database(monkeypatch, database.engine)
    async with database.engine.begin() as connection:
        await connection.run_sync(seed_legacy_inventory_sync)
    try:
        yield root, database
    finally:
        try:
            await drain_tracked_tasks(timeout_seconds=5.0)
            await asyncio.to_thread(join_tracked_threads, timeout_seconds=5.0)
        finally:
            await database.engine.dispose()
            remove_test_database(root / "seraph.db")


def _ids(path: Path) -> dict:
    with closing(sqlite3.connect(path)) as connection:
        return {table: sorted(row[0] for row in connection.execute(f'SELECT {key} FROM "{table}"'))
                for table, key in (("memories", "id"), ("goals", "id"), ("audit_events", "id"),
                                   ("runtime_composition_states", "runtime_domain || ':' || epoch"))}


@pytest.mark.asyncio
async def test_in_process_backup_and_host_restore_keep_canonical_ids_and_history(workspace_db):
    root, database = workspace_db
    # The production owner lease is held for the worker's life; the backup runs under it.
    worker = attach(ProviderWorker(hold_workspace_lock=True, workspace_dir=str(root)))
    worker.acquire_workspace()
    await worker.activate()
    async with get_session() as session:
        session.add(Memory(id="memory-1024", content="kept memory", summary="kept"))
        session.add(Goal(id="goal-1024", title="kept goal", revision=1, status="active"))
    await _cordis_owned(database, "goals", epoch=4)
    before = _ids(root / "seraph.db")
    receipt = await worker.maintenance_backup()
    assert receipt["status"] == "created" and receipt["secret_values_included"] is False
    archive = Path(receipt["archive_path"])
    assert archive.is_file() and state_gate() == ("active", None)
    with zipfile.ZipFile(archive) as packed:
        names = packed.namelist()
        assert not any(name.endswith(".vault-key") for name in names), "no Vault key in the archive"
        copy = root.parent / "archived.db"
        copy.write_bytes(packed.read(next(name for name in names if name.endswith("/seraph.db"))))
    archived = _ids(copy)
    assert archived["memories"] == before["memories"] and archived["goals"] == before["goals"]
    # Composition rows and their epochs are part of the database, archived as they are.
    assert archived["runtime_composition_states"] == before["runtime_composition_states"]
    assert "seraph.goals.v1:4" in archived["runtime_composition_states"]
    async with get_session() as session:
        events = (await session.execute(select(AuditEvent.details_json).where(
            AuditEvent.event_type == "runtime_state_maintenance"))).scalars().all()
    assert len(events) == 1 and json.loads(events[0])["outcome"] == "completed"
    assert worker.last_maintenance["archive_manifest_sha256"] == receipt["archive_manifest_sha256"]
    assert sorted(path.name for path in root.glob("seraph.db*") if not path.name.endswith(("-wal", "-shm"))) == \
        ["seraph.db"], "no second database"
    # Work after the backup, then the host-side restore against the stopped backend.
    async with get_session() as session:
        session.add(Memory(id="memory-after", content="after", summary="after"))
    # While the backend holds its lease, the host CLI's fence cannot be taken.
    from src.workspace import canonical_workspace_registry, canonical_workspace_root
    from src.workspace.production import (DuplicateWorkspaceOwnerError, ProductionWorkspace, maintenance_fence,
                                          reconcile_production_restore)
    workspace = ProductionWorkspace(host_root=canonical_workspace_root(root))
    with pytest.raises(DuplicateWorkspaceOwnerError):
        with maintenance_fence(workspace):
            pass
    worker.close_gate()
    await worker.close_state()
    worker.release_workspace()
    worker.detach()
    with maintenance_fence(workspace):
        # As the host CLI does against the stopped backend.
        restore_workspace(root, archive, registry=canonical_workspace_registry(root), confirm=True,
                          reconcile_restore=reconcile_production_restore)
    restored = _ids(root / "seraph.db")
    assert restored["memories"] == before["memories"] and restored["goals"] == before["goals"]
    assert restored["runtime_composition_states"] == before["runtime_composition_states"]


@pytest.mark.asyncio
async def test_a_cancelled_backup_keeps_the_gate_closed_until_the_archive_step_ends(workspace_db, monkeypatch):
    worker = attach(ProviderWorker())
    await worker.activate()
    started, release = threading.Event(), threading.Event()
    from src.workspace import lifecycle

    def slow_backup(root, *, registry):
        started.set()
        release.wait()
        return {"status": "created", "archive_manifest_sha256": "f" * 64, "member_count": 0,
                "secret_values_included": False}
    monkeypatch.setattr(lifecycle, "backup_workspace", slow_backup)
    task = asyncio.create_task(worker.maintenance_backup())
    await asyncio.to_thread(started.wait)
    task.cancel()
    await asyncio.to_thread(lambda: None)
    # The cancel reached the task, which still waits for the archive step.
    assert task.cancelling() == 1 and not task.done()
    assert state_gate() == ("maintenance", "state_provider_maintenance"), "a cancel never reopens mid-copy"
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state_gate() == ("active", None)
    # The archive step itself finished; only its caller was cancelled.
    assert worker.last_maintenance["outcome"] == "completed" and worker.last_maintenance["audited"] is True


@pytest.mark.asyncio
async def test_a_held_wal_refuses_the_copy_typed_and_audits_it(workspace_db, monkeypatch):
    root, _database = workspace_db
    worker = attach(ProviderWorker())
    await worker.activate()
    from src.workspace import lifecycle
    copied = []
    monkeypatch.setattr(lifecycle, "backup_workspace", lambda *args, **kwargs: copied.append(args))
    async with get_session() as session:
        session.add(Memory(id="memory-wal", content="wal", summary="wal"))
    with closing(sqlite3.connect(root / "seraph.db")) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM memories").fetchall()
        with pytest.raises(StateMaintenanceRefused, match="state_maintenance_checkpoint_busy"):
            await worker.maintenance_backup()
        reader.execute("COMMIT")
    assert copied == [], "a busy checkpoint never copies a partial file"
    assert state_gate() == ("active", None)
    assert worker.last_maintenance["outcome"] == "failed" and worker.last_maintenance["audited"] is True


@pytest.mark.asyncio
async def test_a_resume_interrupted_by_an_untyped_failure_leaves_the_gate_failed(db, monkeypatch):
    worker = attach(ProviderWorker())
    await worker.activate()
    seen = []

    async def broken_tasks():
        seen.append(state_gate())
        raise RuntimeError("unexpected")
    with pytest.raises(RuntimeError):
        async with worker.maintenance("backup"):
            monkeypatch.setitem(worker_module._PREDICATES, "tasks", broken_tasks)
    # The predicate ran while the gate was still closed to everyone else.
    assert seen == [("maintenance", "state_provider_maintenance")]
    assert state_gate() == ("failed", "state_maintenance_resume_failed")
    assert worker.snapshot()["maintenance"] is None and worker.last_maintenance["audited"] is False
    with pytest.raises(StateProviderUnavailable):
        async with get_session():
            pass


@pytest.mark.asyncio
async def test_shutdown_waits_for_a_running_maintenance_step(db, monkeypatch):
    worker = attach(ProviderWorker())
    await worker.activate()
    inside, leave = asyncio.Event(), asyncio.Event()

    async def maintain():
        async with worker.maintenance("backup"):
            inside.set()
            await leave.wait()
    task = asyncio.create_task(maintain())
    await inside.wait()
    waiter = asyncio.create_task(worker.wait_maintenance())
    await asyncio.to_thread(lambda: None)
    assert not waiter.done(), "shutdown does not release the lease under a running step"
    leave.set()
    await task
    await waiter
    worker.close_gate()
    assert state_gate() == ("deactivating", "state_provider_deactivating")


@pytest.mark.asyncio
async def test_the_backup_route_requires_an_operator_and_maps_refusals(db):
    from fastapi import HTTPException
    from src.api.runtime_composition import MaintenanceBackup, maintenance_backup
    from src.auth.service import AuthenticatedOperator
    request = SimpleNamespace(state=SimpleNamespace(operator=None), app=SimpleNamespace(state=SimpleNamespace()))
    with pytest.raises(HTTPException) as denied:
        await maintenance_backup(request, MaintenanceBackup(confirm="backup"))
    assert denied.value.status_code == 401
    operator = AuthenticatedOperator.__new__(AuthenticatedOperator)
    principal = SimpleNamespace(authenticated=True, revoked=False, principal_id="operator:root")
    object.__setattr__(operator, "principal", principal)
    object.__setattr__(operator, "ownership_continuity", "stable")
    request.state.operator = operator
    with pytest.raises(HTTPException) as unavailable:
        await maintenance_backup(request, MaintenanceBackup(confirm="backup"))
    assert (unavailable.value.status_code, unavailable.value.detail["code"]) == (409, "state_maintenance_unavailable")
    request.app.state.provider_worker = attach(ProviderWorker())
    with pytest.raises(HTTPException) as refused:
        await maintenance_backup(request, MaintenanceBackup(confirm="backup"))
    assert (refused.value.status_code, refused.value.detail["code"]) == (409, "state_maintenance_unavailable")
    object.__setattr__(operator, "ownership_continuity", "recovering")
    with pytest.raises(HTTPException) as recovering:
        await maintenance_backup(request, MaintenanceBackup(confirm="backup"))
    assert recovering.value.status_code == 403


@pytest.mark.asyncio
async def test_shutdown_aborts_a_step_still_draining_and_keeps_the_gate_closed(db, monkeypatch):
    draining = asyncio.Event()
    original_drain = db_engine.drain_open_sessions

    async def observed_drain(wake=None):
        draining.set()
        await original_drain(wake)
    monkeypatch.setattr(db_engine, "drain_open_sessions", observed_drain)
    worker = attach(ProviderWorker())
    await worker.activate()
    held, release = asyncio.Event(), asyncio.Event()

    async def holder():
        async with get_session() as session:
            await session.execute(select(Goal.id).limit(1))
            held.set()
            await release.wait()
    holding = asyncio.create_task(holder())
    await held.wait()
    ran = []

    async def maintain():
        async with worker.maintenance("backup"):
            ran.append("copied")
    task = asyncio.create_task(maintain())
    await draining.wait()
    assert worker._maintenance_phase == "draining"
    await worker.wait_maintenance()
    with pytest.raises(StateMaintenanceRefused, match="state_maintenance_shutdown"):
        await task
    assert ran == [], "a step woken during its drain copies nothing"
    assert state_gate() == ("maintenance", "state_provider_maintenance"), "shutdown keeps the gate closed"
    with pytest.raises(StateMaintenanceRefused, match="state_maintenance_unavailable"):
        async with worker.maintenance("backup"):
            pass
    worker.close_gate()
    release.set()
    await holding
    assert state_gate() == ("deactivating", "state_provider_deactivating")
