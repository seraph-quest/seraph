"""Existing target jobs cannot retain stale execution authority after rebind."""

from contextlib import asynccontextmanager
import shutil
import sqlite3

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from config.settings import settings
from tests.test_inference_accounting import accounting_db, request, setup_configuration
from tests.test_inference_accounting_continuity import run_cli
from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
from src.workflows.job_runtime import DurableJobRepository, DurableJobError
from src.workspace import maintenance_fence
from src.workspace.accounting_continuity import rebind_accounting_root
from src.workspace.production import ProductionWorkspace, ProductionWorkspaceReconciliationError, read_lifecycle_receipt


async def prepare_source(accounting_db):
    root, engine, _factory = accounting_db
    setup_configuration()
    await DurableJobRepository().configure_inference_accounting(1000)
    calls = []
    async def charged():
        calls.append("settled")
        return {"usage": {"cost": "0.000017"}}
    async def unknown():
        calls.append("unknown")
        return {"usage": {"prompt_tokens": 1}}
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    await broker.execute(request("retained-spent"), charged)
    await broker.execute(request("retained-unknown"), unknown)
    await engine.dispose()
    return root, calls


def database_rows(root):
    with sqlite3.connect(root / "seraph.db") as db:
        db.row_factory = sqlite3.Row
        return {row["run_identity"]: dict(row) for row in db.execute("SELECT * FROM workflow_run_states")}


def database_dump(root):
    with sqlite3.connect(root / "seraph.db") as db:
        return list(db.iterdump())


def bind_target_sessions(monkeypatch, target):
    engine = create_async_engine(f"sqlite+aiosqlite:///{target / 'seraph.db'}", connect_args={"timeout": 5})
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    @asynccontextmanager
    async def sessions():
        async with factory() as db:
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
    monkeypatch.setattr("src.workflows.job_runtime.get_session", sessions)
    monkeypatch.setattr(settings, "workspace_dir", str(target))
    return engine


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_state", ["queued", "running", "absent"])
async def test_existing_target_jobs_are_blocked_freshly_fenced_and_retry_is_exact(accounting_db, monkeypatch, stale_state):
    from src.workspace import production
    root, calls = await prepare_source(accounting_db)
    source_rows = database_rows(root)
    target = root.parent / f"stale-{stale_state}"
    shutil.copytree(root, target)
    with sqlite3.connect(target / "seraph.db") as db:
        if stale_state == "absent":
            db.execute("DELETE FROM workflow_run_states")
        else:
            db.execute("UPDATE workflow_run_states SET status=?, attempt_count=0, finished_at=NULL, lease_owner='old-worker', lease_expires_at='2099-01-01', revision=revision+7, fencing_token=fencing_token+9, effect_receipts_json='[]', result_summary='untrusted target output'", (stale_state,))
        db.execute("CREATE TABLE retained_audit_note (job_id TEXT REFERENCES workflow_run_states(id), note TEXT)")
        for job in source_rows.values():
            db.execute("INSERT INTO retained_audit_note VALUES (?,?)", (job["id"], "retain audit and FK identity"))
    target_before = database_rows(target)
    source_workspace, target_workspace = ProductionWorkspace(host_root=root), ProductionWorkspace(host_root=target)
    writer = production.write_lifecycle_receipt
    def promotion_gap(workspace, receipt, **kwargs):
        if receipt.get("deployment_binding", {}).get("root_path_digest") == target_workspace.identity_digest:
            raise RuntimeError("injected post-retention promotion gap")
        return writer(workspace, receipt, **kwargs)
    monkeypatch.setattr(production, "write_lifecycle_receipt", promotion_gap)
    with maintenance_fence(source_workspace), maintenance_fence(target_workspace):
        with pytest.raises(RuntimeError, match="promotion gap"):
            rebind_accounting_root(active=source_workspace, target=target_workspace)
    refreshed = database_rows(target)
    for job_id, row in refreshed.items():
        assert row["status"] == "blocked" and row["lease_owner"] is None and row["lease_expires_at"] is None
        stale = target_before.get(job_id, source_rows[job_id])
        assert row["revision"] > max(source_rows[job_id]["revision"], stale["revision"])
        assert row["fencing_token"] > max(source_rows[job_id]["fencing_token"], stale["fencing_token"])
        assert row["owner_principal_id"] == source_rows[job_id]["owner_principal_id"]
        assert row["effect_receipts_json"] == source_rows[job_id]["effect_receipts_json"]
        assert row["result_summary"] == source_rows[job_id]["result_summary"]
    monkeypatch.setattr(production, "write_lifecycle_receipt", writer)
    rebound = run_cli(target, "accounting-rebind", "--from-root", str(root), "--confirm")
    assert rebound["status"] == "rebound"
    assert database_rows(target) == refreshed
    with sqlite3.connect(target / "seraph.db") as db:
        assert db.execute("SELECT COUNT(*) FROM retained_audit_note").fetchone()[0] == 2
        assert db.execute("PRAGMA foreign_key_check(retained_audit_note)").fetchall() == []
    assert run_cli(target, "accounting-rebind", "--from-root", str(root), "--confirm")["status"] == "already_rebound"
    assert database_rows(target) == refreshed
    engine = bind_target_sessions(monkeypatch, target)
    repository = DurableJobRepository()
    for job_id, prior in (target_before or source_rows).items():
        with pytest.raises(DurableJobError, match="only queued"):
            await repository.claim_job(job_id, owner="new-worker", lease_seconds=30)
        with pytest.raises(DurableJobError):
            await repository.heartbeat_job(job_id, owner="old-worker", fencing_token=prior["fencing_token"], expected_revision=prior["revision"])
        with pytest.raises(DurableJobError):
            await repository.record_checkpoint(job_id, checkpoint_id="stale-worker-write", state={"output": "must not adopt"},
                owner="old-worker", fencing_token=prior["fencing_token"], expected_revision=prior["revision"])
    snapshot = await repository.inference_accounting_snapshot()
    assert snapshot["committed_microusd"] == 17 and snapshot["unknown_microusd"] == 100
    assert snapshot["remaining_microusd"] == 883
    assert calls == ["settled", "unknown"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_immutable_target_binding_mismatch_and_missing_source_leave_transaction_unchanged(accounting_db):
    root, calls = await prepare_source(accounting_db)
    source_workspace = ProductionWorkspace(host_root=root)
    receipt_before = read_lifecycle_receipt(source_workspace)
    fields = {"root_run_identity": "foreign-root", "owner_principal_id": "operator:foreign",
        "session_id": "foreign-session", "operator_session_id": "foreign-browser-session", "goal_id": "foreign-goal",
        "input_digest": "b"*64, "capability_version": "foreign-v2", "budget_digest": "c"*64,
        "authority_digest": "d"*64, "idempotency_binding": "foreign-binding"}
    for field, value in fields.items():
        target = root.parent / f"mismatch-{field}"
        shutil.copytree(root, target)
        with sqlite3.connect(target / "seraph.db") as db:
            # Only the second linked job conflicts, proving the first row is
            # not partially refreshed when later validation rejects the copy.
            job_id = db.execute("SELECT job_id FROM inference_cost_reservations WHERE state='unknown'").fetchone()[0]
            db.execute(f'UPDATE workflow_run_states SET "{field}"=? WHERE run_identity=?', (value, job_id))
        before = database_dump(target)
        with maintenance_fence(source_workspace), maintenance_fence(ProductionWorkspace(host_root=target)):
            with pytest.raises(ProductionWorkspaceReconciliationError, match="binding mismatch"):
                rebind_accounting_root(active=source_workspace, target=ProductionWorkspace(host_root=target))
        assert database_dump(target) == before
        assert read_lifecycle_receipt(source_workspace) == receipt_before
    target = root.parent / "missing-source-job"
    shutil.copytree(root, target)
    with sqlite3.connect(root / "seraph.db") as db:
        job_id = db.execute("SELECT job_id FROM inference_cost_reservations WHERE state='unknown'").fetchone()[0]
        db.execute("DELETE FROM workflow_run_states WHERE run_identity=?", (job_id,))
    before = database_dump(target)
    with maintenance_fence(source_workspace), maintenance_fence(ProductionWorkspace(host_root=target)):
        with pytest.raises(ProductionWorkspaceReconciliationError, match="canonical job missing"):
            rebind_accounting_root(active=source_workspace, target=ProductionWorkspace(host_root=target))
    assert database_dump(target) == before and read_lifecycle_receipt(source_workspace) == receipt_before
    assert calls == ["settled", "unknown"]
