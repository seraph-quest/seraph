"""Actual ledger/transport plus independently retained deployment continuity."""

from dataclasses import replace
from pathlib import Path
import json
import os
import subprocess
import sys

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

from tests.test_inference_accounting import accounting_db, request, setup_configuration
from src.model_fabric.configuration import read_model_fabric_configuration, write_model_fabric_configuration
from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
from src.workflows.job_runtime import DurableJobRepository
from src.workspace.production import ProductionWorkspace, read_lifecycle_receipt
from tests.support.brokers import use_inference_broker
from tests.support.db import install_database

# ADR-035: pins historical OpenRouter ledger continuity and restore mechanics that still settle retained liabilities
pytestmark = pytest.mark.openrouter_legacy


CLI = Path(__file__).resolve().parents[1] / "workspace_cli.py"


def run_cli(root, *arguments):
    env = {**os.environ, "WORKSPACE_DIR": str(root), "BACKEND_DATA_PATH_PROD": str(root),
        "DEPLOYMENT_ENVIRONMENT": "production"}
    completed = subprocess.run([sys.executable, str(CLI), "--base-dir", str(root.parent), *arguments],
        env=env, capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout)


def complete_workspace(root):
    for name in ("artifacts", "extensions", "skills", "workflows", "runbooks", "plans", "reports", "notes"):
        (root / name).mkdir(exist_ok=True)
    for name in ("mcp-servers.json", "stdio-proxies.json", "extensions-state.json", "starter-packs.json", "screen-analysis-settings.json"):
        (root / name).write_text("{}")
    (root / ".vault-key").write_text("isolated-test-only")
    (root / "soul.md").write_text("canonical")


@pytest.mark.asyncio
async def test_empty_other_root_cannot_bootstrap_and_managed_rebind_retains_liability(accounting_db, monkeypatch):
    from config.settings import settings
    from src.workspace import accounting_continuity, maintenance_fence
    root, engine, _factory = accounting_db
    setup_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    await broker.execute(request("root-spent"), lambda: _charge("0.000017"))
    await broker.execute(request("root-unknown"), lambda: _charge(None))
    original = await repository.inference_accounting_snapshot()
    await engine.dispose()
    target = root.parent / "new-empty-root"
    target.mkdir()
    new_engine = create_async_engine(f"sqlite+aiosqlite:///{target / 'seraph.db'}")
    async with new_engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    await new_engine.dispose()
    # The operator now points Seraph at the new, empty root: its database
    # becomes the default for every get_session caller (#1241).
    install_database(monkeypatch, new_engine)
    monkeypatch.setattr(settings, "workspace_dir", str(target))
    calls = []
    with pytest.raises(RuntimeError, match="binding_unavailable"):
        await repository.ensure_inference_ledger()
    with pytest.raises(ValueError):
        await RemoteInferenceAdmissionBroker(durable_accounting=True).execute(request("empty-reset"), lambda: calls.append("forbidden"))
    assert calls == []
    source_workspace, target_workspace = ProductionWorkspace(host_root=root), ProductionWorkspace(host_root=target)
    from src.workspace import production
    write = production.write_lifecycle_receipt
    def crash_binding(workspace, receipt, **kwargs):
        if receipt.get("deployment_binding", {}).get("root_path_digest") == target_workspace.identity_digest:
            raise RuntimeError("injected promotion gap")
        return write(workspace, receipt, **kwargs)
    monkeypatch.setattr(production, "write_lifecycle_receipt", crash_binding)
    with maintenance_fence(source_workspace), maintenance_fence(target_workspace):
        with pytest.raises(RuntimeError, match="promotion gap"):
            accounting_continuity.rebind_accounting_root(active=source_workspace, target=target_workspace)
    assert read_lifecycle_receipt(source_workspace)["deployment_binding"]["root_path_digest"] == source_workspace.identity_digest
    monkeypatch.setattr(production, "write_lifecycle_receipt", write)
    rebound = run_cli(target, "accounting-rebind", "--from-root", str(root), "--confirm")
    assert rebound["status"] == "rebound" and rebound["liabilities_retained"] == 2
    assert run_cli(target, "accounting-rebind", "--from-root", str(root), "--confirm")["status"] == "already_rebound"
    from src.workspace.accounting_continuity import verify_accounting_generation
    receipt = read_lifecycle_receipt(target_workspace)
    account, rows = verify_accounting_generation(target / "seraph.db", receipt["inference_accounting"])
    assert account["deployment_id"] == original["deployment_id"]
    assert sum(row["actual_cost_microusd"] or 0 for row in rows) == 17
    assert [row["state"] for row in rows].count("unknown") == 1
    assert read_model_fabric_configuration().egress_revoked is True
    assert calls == []
    await new_engine.dispose()


async def _charge(cost):
    return {"usage": {"cost": cost}} if cost is not None else {"usage": {"prompt_tokens": 1}}


@pytest.mark.asyncio
async def test_revoke_old_config_copy_and_managed_restore_never_import_active_grant(accounting_db, monkeypatch):
    root, engine, _factory = accounting_db
    setup_configuration()
    await DurableJobRepository().ensure_inference_ledger()
    await RemoteInferenceAdmissionBroker(durable_accounting=True).execute(request("policy-spent"), lambda: _charge("0.000009"))
    await engine.dispose()
    complete_workspace(root)
    old = (root / "model-fabric-settings.json").read_bytes()
    archive = run_cli(root, "backup")
    config = read_model_fabric_configuration()
    write_model_fabric_configuration(replace(config, egress_revoked=True, egress_revision=config.egress_revision + 1))
    revoked_revision = read_model_fabric_configuration().egress_revision
    (root / "model-fabric-settings.json").write_bytes(old)
    stale = read_model_fabric_configuration()
    assert stale.egress_revoked and stale.egress_revision == revoked_revision
    assert stale.error_code == "provider_policy_continuity_unavailable"
    calls = []
    with pytest.raises(ValueError):
        await RemoteInferenceAdmissionBroker(durable_accounting=True).execute(request("stale-policy"), lambda: calls.append("forbidden"))
    await engine.dispose()
    restored = run_cli(root, "restore", "--archive", archive["archive_path"], "--confirm")
    config = read_model_fabric_configuration()
    assert config.egress_revoked and config.egress_revision > revoked_revision
    restored_revision = config.egress_revision
    assert (await DurableJobRepository().inference_accounting_snapshot())["reason_code"] == "general_task_group_lookup_invalid"
    from src.db.engine import _ensure_inference_group_lookup
    async with engine.begin() as connection:
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        await _ensure_inference_group_lookup(connection)
    assert (await DurableJobRepository().inference_accounting_snapshot())["spent_microusd"] == 9
    await engine.dispose()
    run_cli(root, "rollback", "--restore-id", restored["restore_id"], "--confirm")
    assert read_model_fabric_configuration().egress_revoked
    assert read_model_fabric_configuration().egress_revision > restored_revision
    assert (await DurableJobRepository().inference_accounting_snapshot())["reason_code"] == "general_task_group_lookup_invalid"
    async with engine.begin() as connection:
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        await _ensure_inference_group_lookup(connection)
    assert (await DurableJobRepository().inference_accounting_snapshot())["spent_microusd"] == 9
    assert calls == []
    from src.workspace import maintenance_fence, canonical_workspace_registry, restore_workspace, reconcile_production_restore
    from src.workspace.lifecycle import WorkspaceLifecycleError
    def tampered_reconciliation(**kwargs):
        receipt = reconcile_production_restore(**kwargs)
        staged = kwargs["stage"] / "model-fabric-settings.json"
        payload = json.loads(staged.read_text())
        payload["openrouter_setup"]["model_ids"] = ["unlisted/changed-model"]
        staged.write_text(json.dumps(payload))
        return receipt
    with maintenance_fence(ProductionWorkspace(host_root=root)):
        with pytest.raises(WorkspaceLifecycleError, match="canonical hash mismatch: model-fabric-settings.json"):
            restore_workspace(root, Path(archive["archive_path"]), registry=canonical_workspace_registry(root),
                confirm=True, reconcile_restore=tampered_reconciliation)


@pytest.mark.asyncio
async def test_policy_witness_publication_fault_maintenance_recovery_is_revoked(accounting_db, monkeypatch):
    from src.workspace import accounting_witness
    root, engine, _factory = accounting_db
    setup_configuration()
    await DurableJobRepository().ensure_inference_ledger()
    prior = read_model_fabric_configuration()
    writer = accounting_witness._write_configuration_file
    def injected_crash(path, payload):
        raise RuntimeError("injected policy publication gap")
    monkeypatch.setattr(accounting_witness, "_write_configuration_file", injected_crash)
    with pytest.raises(RuntimeError, match="publication gap"):
        write_model_fabric_configuration(replace(prior, egress_revision=prior.egress_revision + 1))
    monkeypatch.setattr(accounting_witness, "_write_configuration_file", writer)
    await engine.dispose()
    assert read_model_fabric_configuration().egress_revoked
    calls = []
    with pytest.raises(ValueError):
        await RemoteInferenceAdmissionBroker(durable_accounting=True).execute(request("pending-policy"), lambda: calls.append("forbidden"))
    recovery = run_cli(root, "accounting-reconcile", "--policy", "--confirm")
    assert recovery["status"] == "reconciled_revoked"
    assert recovery["revision"] == prior.egress_revision + 2
    assert run_cli(root, "accounting-reconcile", "--policy", "--confirm")["status"] == "already_reconciled"
    assert read_model_fabric_configuration().egress_revoked and calls == []


@pytest.mark.asyncio
async def test_authenticated_revoked_policy_copy_rejected_and_current_revision_regrant(accounting_db, monkeypatch):
    # #1284: the month and request-reserve review halves are gone; the stale
    # policy copy and current-revision regrant remain.
    import httpx
    from types import SimpleNamespace
    from config.settings import settings
    from src.app import create_app
    from src.api.auth import _reset_login_throttle_for_tests
    from src.model_fabric.effective_policy import revoke_effective_policy
    root, engine, _factory = accounting_db
    for name, value in {"operator_auth_secret": "continuity-api-password", "operator_auth_secret_hash": "",
        "operator_auth_allow_unauthenticated_tests": False, "operator_auth_allowed_hosts": "test",
        "operator_auth_allowed_origins": "http://localhost:3001", "operator_auth_cookie_secure": False,
        "openrouter_api_key": "intercepted-test-key"}.items():
        monkeypatch.setattr(settings, name, value)
    _reset_login_throttle_for_tests()
    use_inference_broker(monkeypatch, RemoteInferenceAdmissionBroker(durable_accounting=True))
    original_post = httpx.AsyncClient.post
    calls, charge = [], ["0.000101"]
    async def route_post(client, url, **kwargs):
        if str(url).startswith("https://openrouter.ai/"):
            calls.append(str(url))
            assert str(url) == "https://openrouter.ai/api/v1/chat/completions"
            return httpx.Response(200, json={"id": "gen-reviewed", "choices": [{"message": {"content": "CANARY_OK"}}],
                "usage": {"cost": charge[0]}}, request=httpx.Request("POST", url))
        return await original_post(client, url, **kwargs)
    monkeypatch.setattr(httpx.AsyncClient, "post", route_post)
    headers = {"origin": "http://localhost:3001"}
    setup = {"model_ids": ["openai/gpt-4o-mini"], "capabilities": ["text"], "allowed_upstreams": ["openai"],
        "data_collection": "deny", "data_retention_policy": "deny", "egress_class": "cloud_allowed_full",
        "cloud_egress_acknowledged": True, "spend_ceiling_microusd": 1000, "request_cost_bound_microusd": 100,
        "credential_ref": "env:OPENROUTER_API_KEY"}
    canary = {"profile_id": "openrouter", "capability": "text", "timeout_seconds": 120}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test") as client:
        login = await client.post("/api/auth/login", headers=headers, json={"password": "continuity-api-password"})
        assert login.status_code == 200, login.text
        saved = await client.put("/api/settings/model-fabric", headers=headers, json={"openrouter_setup": setup})
        assert saved.status_code == 200, saved.text
        result = await client.post("/api/settings/model-fabric/canary", headers=headers, json=canary)
        assert result.status_code == 200 and len(calls) == 1, result.text
        initial = (await client.get("/api/settings/model-fabric/accounting")).json()
        assert initial["status"] == "ready" and initial["spent_microusd"] == 101
        old = (root / "model-fabric-settings.json").read_bytes()
        current = read_model_fabric_configuration()
        principal = SimpleNamespace(principal_id=login.json()["principal_id"], authenticated=True)
        revoked = await revoke_effective_policy(SimpleNamespace(state=SimpleNamespace(operator=SimpleNamespace(principal=principal))),
            SimpleNamespace(grant_id="provider_policy:openrouter", expected_revision=current.egress_revision, idempotency_key="exact-api-revoke"))
        (root / "model-fabric-settings.json").write_bytes(old)
        rejected = await client.put("/api/settings/model-fabric", headers=headers, json={"openrouter_setup": setup})
        assert rejected.status_code == 409
        regrant = await client.put("/api/settings/model-fabric", headers=headers,
            json={"openrouter_setup": setup, "expected_policy_revision": revoked["revision"]})
        assert regrant.status_code == 200, regrant.text
        charge[0] = "0.000007"
        result = await client.post("/api/settings/model-fabric/canary", headers=headers, json=canary)
        assert result.status_code == 200 and len(calls) == 2, result.text
        final = (await client.get("/api/settings/model-fabric/accounting")).json()
        assert final["status"] == "ready" and final["spent_microusd"] == 108


def test_policy_restore_hash_exception_rejects_unlisted_mutation():
    # Covered with real configuration/witness in the async owning journey;
    # malformed or missing proof must never reach a generic hash exception.
    from src.workspace.accounting_witness import verify_restored_policy_reconciliation
    root = Path("/tmp/nonexistent-policy-proof")
    archived = {"egress_revision": 1, "openrouter_setup": {"model_ids": ["original"]}}
    current = {**archived, "egress_revision": 2, "egress_revoked": True, "egress_revocation_key": None,
        "openrouter_setup": {"model_ids": ["unlisted-mutation"]}}
    assert verify_restored_policy_reconciliation(root=root, archived=json.dumps(archived), staged=json.dumps(current)) is False
