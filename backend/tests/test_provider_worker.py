"""#1024 A3.1: Python-hosted mandatory providers, the state gate and startup cleanup."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from config.settings import settings
from src.db import engine as db_engine
from src.db.engine import StateProviderUnavailable, set_state_gate, state_gate
from src.runtime_plugins import worker as worker_module
from src.runtime_plugins.worker import (DEGRADABLE, MANDATORY, ProviderActivationError, ProviderWorker, attach,
                                        current_worker, domain_provider_unavailable)

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _detached_gate():
    set_state_gate(None)
    worker_module._set_current(None)
    yield
    set_state_gate(None)
    worker_module._set_current(None)


# ------------------------------------------------------------------ import side effects

def test_importing_the_worker_and_app_starts_no_consumer(tmp_path):
    """No connection, DB file, task, thread, scheduler job or observer at import."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = r"""
import asyncio, json, sys, threading
before = set(threading.enumerate())
import src.runtime_plugins.worker as worker
import src.runtime_plugins.bridge, src.runtime_plugins.dispatch, src.runtime_plugins.ownership
import src.app
from src.db import engine
from src.scheduler import engine as scheduler_engine
scheduler = getattr(scheduler_engine, "_scheduler", None) or getattr(scheduler_engine, "scheduler", None)
jobs = len(scheduler.get_jobs()) if scheduler is not None and hasattr(scheduler, "get_jobs") else 0
def library_timer(thread):
    # slowapi's in-memory rate-limit storage (``limits``) starts its expiry
    # Timer when ``src.app`` builds the module-level Limiter. It is not a
    # Seraph consumer and touches no workspace state.
    function = getattr(thread, "function", None)
    return (type(thread) is threading.Timer
            and getattr(function, "__module__", "").startswith("limits."))
new_threads = [thread.name for thread in set(threading.enumerate()) - before if not library_timer(thread)]
print(json.dumps({
    "threads": len(new_threads),
    "checked_out": engine.engine.pool.checkedout(),
    "loop_running": asyncio._get_running_loop() is not None,
    "scheduler_running": bool(getattr(scheduler, "running", False)),
    "scheduler_jobs": jobs,
    "worker": worker.current_worker() is not None,
    "gate": list(engine.state_gate()),
}))
"""
    env = {**os.environ, "WORKSPACE_DIR": str(workspace), "PYTHONPATH": str(BACKEND)}
    completed = subprocess.run([sys.executable, "-c", script], cwd=BACKEND, env=env, capture_output=True,
                               text=True, timeout=120, check=False)
    assert completed.returncode == 0, completed.stderr[-2000:]
    observed = json.loads(completed.stdout.strip().splitlines()[-1])
    assert observed == {"threads": 0, "checked_out": 0, "loop_running": False, "scheduler_running": False,
                        "scheduler_jobs": 0, "worker": False, "gate": [None, None]}
    created = {path.name for path in workspace.rglob("*")}
    assert not created & {"seraph.db", "seraph.db-wal", "seraph.db-shm"}, created


def test_constructing_a_worker_has_no_side_effects(tmp_path):
    worker = ProviderWorker(hold_workspace_lock=True, workspace_dir=str(tmp_path))
    assert state_gate() == (None, None)
    assert worker.snapshot()["mandatory_ready"] is False
    assert list(tmp_path.iterdir()) == []


def test_only_named_bypasses_construct_engines_or_session_factories():
    """Every session goes through ``get_session``'s gate except reviewed bypasses."""
    allowed = {"db/engine.py", "evals/harness.py"}
    offenders = []
    for path in sorted((BACKEND / "src").rglob("*.py")):
        relative = path.relative_to(BACKEND / "src").as_posix()
        if relative in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            name = (node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None)
            if name in {"async_session_factory", "create_async_engine", "sessionmaker"}:
                offenders.append(f"{relative}:{node.lineno}:{name}")
    assert offenders == []


# ------------------------------------------------------------------ gate

@pytest.mark.asyncio
async def test_get_session_gate_is_open_unattached_and_active_and_closed_otherwise():
    from sqlalchemy import text
    for state in (None, "activating", "active"):
        set_state_gate(state)
        async with db_engine.get_session() as db:
            assert (await db.execute(text("SELECT 1"))).scalar_one() == 1
    for state, reason in (("failed", "audit_write_unavailable"), ("deactivating", "state_provider_deactivating")):
        set_state_gate(state, reason)
        with pytest.raises(StateProviderUnavailable) as excinfo:
            async with db_engine.get_session():
                pass
        assert excinfo.value.reason_code == reason
    # An explicit isolated-session override is a reviewed bypass.
    set_state_gate("failed", "state_init_db_failed")
    with db_engine.override_session_factory(db_engine.async_session_factory):
        async with db_engine.get_session() as db:
            assert (await db.execute(text("SELECT 1"))).scalar_one() == 1


# ------------------------------------------------------------------ activation

@pytest.mark.asyncio
async def test_activation_runs_every_predicate_and_opens_the_gate():
    worker = attach(ProviderWorker())
    await worker.activate()
    assert state_gate() == ("active", None)
    snapshot = worker.snapshot()
    assert snapshot["mandatory_ready"] is True
    assert {item["provider"]: item["state"] for item in snapshot["providers"]} == {
        name: "active" for name in (*MANDATORY, *DEGRADABLE)}
    # Activation is idempotent; readiness re-runs predicates without activating.
    await worker.activate()
    assert (await worker.readiness())["mandatory_ready"] is True
    from sqlalchemy import select
    from src.db.models import AuditEvent
    async with db_engine.get_session() as db:
        probes = (await db.execute(select(AuditEvent).where(AuditEvent.event_type == "runtime_audit_ready"))).all()
    assert probes == [], "the audit probe write is always rolled back"
    worker.close_gate()
    with pytest.raises(StateProviderUnavailable):
        async with db_engine.get_session():
            pass
    worker.detach()
    assert state_gate() == (None, None) and current_worker() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("provider, reason", [
    ("authority", "authority_configuration_invalid"), ("audit", "audit_write_unavailable"),
    ("tasks", "tasks_tables_missing")])
async def test_mandatory_provider_failure_aborts_and_closes_the_gate(monkeypatch, provider, reason):
    async def fail():
        raise ProviderActivationError(provider, reason)
    monkeypatch.setitem(worker_module._PREDICATES, provider, fail)
    worker = attach(ProviderWorker())
    with pytest.raises(ProviderActivationError) as excinfo:
        await worker.activate()
    assert excinfo.value.reason_code == reason
    assert worker.status[provider].state == "failed"
    assert state_gate() == ("failed", reason)
    with pytest.raises(StateProviderUnavailable):
        async with db_engine.get_session():
            pass


@pytest.mark.asyncio
async def test_state_init_failure_is_typed(monkeypatch):
    monkeypatch.setattr(worker_module, "init_db", AsyncMock(side_effect=RuntimeError("disk")))
    worker = attach(ProviderWorker())
    with pytest.raises(ProviderActivationError) as excinfo:
        await worker.activate()
    assert (excinfo.value.provider, excinfo.value.reason_code) == ("state", "state_init_db_failed")
    assert state_gate() == ("failed", "state_init_db_failed")


@pytest.mark.asyncio
async def test_authority_requires_a_resolvable_vault_key(monkeypatch, tmp_path):
    from src.workspace import canonical_workspace_root
    monkeypatch.setattr(settings, "vault_encryption_key", "")
    key = canonical_workspace_root(settings.workspace_dir) / ".vault-key"
    existed = key.exists()
    original = key.read_bytes() if existed else None
    try:
        key.write_text("not-a-fernet-key")
        os.chmod(key, 0o600)
        worker = attach(ProviderWorker())
        with pytest.raises(ProviderActivationError) as excinfo:
            await worker.activate()
        assert excinfo.value.reason_code == "authority_vault_key_unavailable"
        os.chmod(key, 0o644)
        assert worker_module._vault_key_resolves() == "authority_vault_key_unsafe"
    finally:
        if existed:
            key.write_bytes(original)
            os.chmod(key, 0o600)
        else:
            key.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_degradable_provider_failure_blocks_only_its_domain(monkeypatch):
    async def fail():
        raise ProviderActivationError("memory", "memory_tables_missing")
    monkeypatch.setitem(worker_module._PREDICATES, "memory", fail)
    worker = attach(ProviderWorker())
    await worker.activate()
    assert worker.status["memory"].state == "degraded"
    assert worker.snapshot()["mandatory_ready"] is True
    assert domain_provider_unavailable("seraph.memory.v1") == "native_provider_memory_unavailable"
    assert domain_provider_unavailable("seraph.goals.v1") is None
    assert domain_provider_unavailable("seraph.research.v1") is None


# ------------------------------------------------------------------ lock and lifespan cleanup

def test_one_workspace_owner_per_host(tmp_path):
    from src.workspace.production import DuplicateWorkspaceOwnerError, runtime_workspace_owner
    root = tmp_path / "data"
    (root / "artifacts").mkdir(parents=True)
    worker = ProviderWorker(hold_workspace_lock=True, workspace_dir=str(root))
    worker.acquire_workspace()
    worker.acquire_workspace()  # idempotent in one process
    try:
        with pytest.raises(DuplicateWorkspaceOwnerError):
            with runtime_workspace_owner(root):
                pass
    finally:
        worker.release_workspace()
    with runtime_workspace_owner(root):
        pass


def _isolate_startup(app_module, monkeypatch, root):
    monkeypatch.setattr(settings, "workspace_dir", str(root))
    monkeypatch.setattr(settings, "deployment_environment", "production")
    monkeypatch.setattr(settings, "operator_auth_secret", "isolated-provider-fixture")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", True)
    monkeypatch.setattr(settings, "operator_auth_backend_workers", 1)
    calls = []
    monkeypatch.setattr(worker_module, "init_db", AsyncMock())
    monkeypatch.setattr(worker_module, "close_db", AsyncMock(side_effect=lambda: calls.append("close_db")))
    monkeypatch.setattr(app_module.mcp_manager, "disconnect_all", Mock(side_effect=lambda: calls.append("disconnect")))
    monkeypatch.setattr(app_module, "drain_tracked_tasks", AsyncMock(side_effect=lambda **_: calls.append("drain")))
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["init_db", "audit", "tasks", "service_start"])
async def test_every_startup_failure_releases_the_lock_engine_and_mcp(monkeypatch, tmp_path, async_db, failure):
    import src.app as app_module
    from src.workspace.production import runtime_workspace_owner
    root = tmp_path / "data"
    (root / "artifacts").mkdir(parents=True)
    calls = _isolate_startup(app_module, monkeypatch, root)
    if failure == "init_db":
        monkeypatch.setattr(worker_module, "init_db", AsyncMock(side_effect=RuntimeError("init")))
    elif failure in {"audit", "tasks"}:
        async def fail():
            raise ProviderActivationError(failure, f"{failure}_probe_failed")
        monkeypatch.setitem(worker_module._PREDICATES, failure, fail)
    else:
        monkeypatch.setattr("src.integrations.connection_sync.ConnectionSyncService.start",
                            AsyncMock(side_effect=RuntimeError("service")))
        monkeypatch.setattr("src.integrations.connection_sync.ConnectionSyncService.stop", AsyncMock())
    app = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises((ProviderActivationError, RuntimeError)):
        async with app_module.lifespan(app):
            pass
    assert calls == ["disconnect", "drain", "close_db"]
    assert state_gate() == (None, None) and current_worker() is None
    with runtime_workspace_owner(root):
        pass
