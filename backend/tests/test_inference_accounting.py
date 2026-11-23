"""Real file-backed ledger and intercepted governed transport receipts."""

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import asyncio
import json
import shutil
import time

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from config.settings import settings
from src.model_fabric.configuration import ModelFabricConfiguration, OpenRouterSetup, write_model_fabric_configuration
from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker, RemoteInferenceAdmissionRequest, RemoteInferencePriority
from src.security.trust_contract import EgressClass
from src.workflows.job_runtime import DurableJobRepository
from src.workflows.inference_accounting import InferenceAccountingError, account_charge_microusd
from src.workspace.production import ProductionWorkspace, lifecycle_receipt_path
from tests.support.db import install_database
from tests.support.sync import Gate, settle, started_or_raise, wait_until, within
from src.utils.background import drain_tracked_tasks, join_tracked_threads
from tests.support.brokers import use_inference_broker

# ADR-035: these pin the historical OpenRouter broker, ledger, settlement and
# witness mechanics that still settle retained liabilities; retired-route
# denial is covered in test_openrouter_retirement.py.
pytestmark = pytest.mark.openrouter_legacy


# Database and lifecycle plumbing shared by many files (#1284): it lives in
# tests/support/accounting.py and is re-exported here for existing imports.
from tests.support.accounting import accounting_db  # noqa: E402,F401


@pytest.mark.asyncio
async def test_accounting_db_materializes_all_canonical_model_tables(accounting_db):
    from sqlalchemy import inspect
    from src.db.task_method_models import TaskMethodActive

    async with accounting_db[1].connect() as connection:
        tables = await connection.run_sync(lambda db: set(inspect(db).get_table_names()))
    assert TaskMethodActive.__tablename__ in tables
    assert set(SQLModel.metadata.tables).issubset(tables)


def setup_configuration(*, ceiling=1000, bound=100):
    setup = OpenRouterSetup(model_ids=("openai/gpt-4o-mini",), capabilities=("text",),
        allowed_upstreams=("openai",), egress_class=EgressClass.CLOUD_ALLOWED_FULL,
        cloud_egress_acknowledged=True, spend_ceiling_microusd=ceiling,
        request_cost_bound_microusd=bound, credential_ref="env:OPENROUTER_API_KEY")
    configuration = ModelFabricConfiguration(status="ready", openrouter_setup=setup)
    write_model_fabric_configuration(configuration)
    return configuration


def request(operation_id, *, owner="service:accounting", priority=RemoteInferencePriority.INTERACTIVE_CHAT):
    return RemoteInferenceAdmissionRequest(operation_id=operation_id, job_id=operation_id,
        owner_id=owner, priority=priority, deadline_at=time.time() + 120,
        runtime_path="chat_agent", data_digest="a" * 64)


@pytest.mark.parametrize("value, expected", [("0.0000001", 1), ("0.0000061", 7), (0, 0), ("-1", None), ("NaN", None), ("Infinity", None), (True, None), ({}, None)])
def test_account_charge_uses_decimal_upward_rounding(value, expected):
    assert account_charge_microusd({"usage": {"cost": value, "cost_details": {"upstream_inference_cost": 99}}})[0] == expected
    assert account_charge_microusd({"usage": {"prompt_tokens": 100}})[0] is None


def test_container_accounting_requires_managed_mount_proof(tmp_path, monkeypatch):
    from pathlib import Path
    from src.workflows import inference_accounting
    from src.workspace.production import prepare_lifecycle_directory
    monkeypatch.setenv("SERAPH_WORKSPACE_LIFECYCLE_PATH", str(tmp_path.parent / f"{tmp_path.name}-descriptor"))
    prepare_lifecycle_directory(ProductionWorkspace(host_root=tmp_path))
    monkeypatch.setattr(inference_accounting, "CANONICAL_CONTAINER_WORKSPACE", str(tmp_path))
    read_text = Path.read_text
    def without_mount(path, *args, **kwargs):
        return "" if str(path) == "/proc/self/mountinfo" else read_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", without_mount)
    with pytest.raises(InferenceAccountingError, match="accounting_continuity_unavailable"):
        with inference_accounting._continuity_lock(tmp_path, initialize=True):
            pytest.fail("unmounted container directory must not admit accounting")


@pytest.mark.asyncio
async def test_actual_broker_execution_settles_and_reopens_file_db(accounting_db):
    root, engine, _factory = accounting_db
    setup_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    calls = []
    async def transport():
        calls.append("actual intercepted callback")
        return {"id": "gen-test", "usage": {"cost": "0.0000061"}, "choices": [{"message": {"content": "result"}}]}
    result = await broker.execute(request("operation-one"), transport)
    assert result["choices"][0]["message"]["content"] == "result"
    before = await repository.inference_accounting_snapshot()
    assert before["status"] == "ready", before
    assert before["spent_microusd"] == 7
    assert before["unpriced_requests"] == 0
    assert calls == ["actual intercepted callback"]
    job = await repository.get_job(before["operations"][0]["job_id"])
    assert job["status"] == "succeeded"
    assert "no_learning" in job["result"]["summary"]
    await engine.dispose()
    restarted = RemoteInferenceAdmissionBroker(durable_accounting=True)
    assert (await DurableJobRepository().inference_accounting_snapshot())["spent_microusd"] == 7
    with pytest.raises(ValueError):
        await restarted.execute(request("operation-one"), transport)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unknown_cost_explicit_revision_settlement(accounting_db):
    setup_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    async def no_cost():
        return {"id": "embd-fixture", "usage": {"prompt_tokens": 5}}
    await broker.execute(request("embedding", owner="service:embedding"), no_cost)
    snapshot = await repository.inference_accounting_snapshot()
    row = snapshot["operations"][0]
    assert snapshot["unpriced_requests"] == 1
    assert row["state"] == "unknown"
    assert row["recovery_reason"] == "provider_cost_readback_required"
    with pytest.raises(InferenceAccountingError, match="revision_changed"):
        await repository.settle_inference_cost(row["operation_id"], job_id=row["job_id"],
            expected_revision=row["revision"] - 1, actual_cost_microusd=12,
            evidence_digest="b" * 64, operator_id="operator:settings-owner", idempotency_key="manual-1")
    await repository.settle_inference_cost(row["operation_id"], job_id=row["job_id"],
        expected_revision=row["revision"], actual_cost_microusd=12,
        evidence_digest="b" * 64, operator_id="operator:settings-owner", idempotency_key="manual-1")
    after = await repository.inference_accounting_snapshot()
    assert after["spent_microusd"] == 12
    assert after["unpriced_requests"] == 0
    assert after["operations"][0]["period_id"] == row["period_id"]
    # #1284: the answer was adopted while unpriced; settlement is billing
    # history only and does not change the job.
    assert (await repository.get_job(row["job_id"]))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_restart_recovery_leaves_an_answered_unpriced_request_finished(accounting_db):
    # #1295: startup recovery reclassifies a stale running job's request only
    # when no answer was recorded. An answered request NEAR has not priced yet
    # is finished, like a settled one; a call that failed after contact still
    # becomes a lost outcome. Neither is ever sent again.
    from src.db.models import WorkflowRunState
    root, engine, factory = accounting_db
    setup_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    calls = []
    async def no_cost():
        calls.append("answered")
        return {"id": "gen-unpriced", "usage": {"prompt_tokens": 5}, "choices": [{"message": {"content": "ok"}}]}
    async def failing_after_contact():
        calls.append("failed")
        raise RuntimeError("provider connection dropped after the request was sent")
    await broker.execute(request("answered-unpriced"), no_cost)
    with pytest.raises(RuntimeError, match="dropped"):
        await broker.execute(request("failed-after-contact"), failing_after_contact, uncertain_on_error=False)
    before = {row["operation_id"]: row for row in (await repository.inference_accounting_snapshot())["operations"]}
    assert before["answered-unpriced"]["recovery_reason"] == "provider_cost_readback_required"
    assert before["failed-after-contact"]["recovery_reason"] == "provider_call_failed"
    # Both jobs look like a crashed worker: running with a lapsed lease.
    lapsed = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
    async with factory.accounting_sessions() as db:
        for operation_id in before:
            from sqlalchemy import select
            run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == before[operation_id]["job_id"]))
            run.status, run.lease_owner, run.lease_expires_at = "running", "crashed-worker", lapsed
            db.add(run)
    recovered = await DurableJobRepository().recover_inference_accounting()
    assert [item["operation_id"] for item in recovered] == ["failed-after-contact"]
    after = {row["operation_id"]: row for row in (await repository.inference_accounting_snapshot())["operations"]}
    assert after["answered-unpriced"] == before["answered-unpriced"]
    assert (after["failed-after-contact"]["state"], after["failed-after-contact"]["recovery_reason"]) == (
        "unknown", "provider_outcome_unknown")
    failed = await repository.get_job(before["failed-after-contact"]["job_id"])
    assert failed["status"] == "unknown_external_effect"
    assert (await repository.get_job(before["answered-unpriced"]["job_id"]))["status"] == "running"
    assert calls == ["answered", "failed"]


@pytest.mark.asyncio
async def test_restart_recovery_does_not_treat_a_pre_1284_classified_row_as_answered(accounting_db):
    # #1302 item 2: before #1284, restart recovery labelled a lost outcome
    # ``provider_cost_readback_required`` and recorded a ``restart_recovery``
    # evidence entry. That row is not "answered, not priced", so the #1295
    # skip does not apply to it: it is reclassified as a lost outcome and
    # nothing is sent again.
    import json
    from sqlalchemy import select
    from src.db.models import InferenceCostReservation, WorkflowRunState
    root, engine, factory = accounting_db
    setup_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    calls = []
    async def no_cost():
        calls.append("answered")
        return {"id": "gen-legacy", "usage": {"prompt_tokens": 5}, "choices": [{"message": {"content": "ok"}}]}
    await broker.execute(request("legacy-classified"), no_cost)
    lapsed = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
    async with factory.accounting_sessions() as db:
        row = await db.scalar(select(InferenceCostReservation).where(InferenceCostReservation.operation_id == "legacy-classified"))
        assert (row.state, row.recovery_reason) == ("unknown", "provider_cost_readback_required")
        # The pre-#1284 shape, written as an older build left it.
        row.evidence_json = json.dumps([*json.loads(row.evidence_json), {"kind": "restart_recovery", "state": "unknown",
            "reason": "provider_cost_readback_required", "recorded_at": lapsed.isoformat(), "memory_status": "no_learning"}])
        db.add(row)
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == row.job_id))
        run.status, run.lease_owner, run.lease_expires_at = "running", "crashed-worker", lapsed
        db.add(run)
    recovered = await DurableJobRepository().recover_inference_accounting()
    assert [item["operation_id"] for item in recovered] == ["legacy-classified"]
    after = next(item for item in (await repository.inference_accounting_snapshot())["operations"]
        if item["operation_id"] == "legacy-classified")
    assert (after["state"], after["recovery_reason"]) == ("unknown", "provider_outcome_unknown")
    assert (await repository.get_job(after["job_id"]))["status"] == "unknown_external_effect"
    assert calls == ["answered"]


@pytest.mark.asyncio
async def test_startup_recovery_reseals_every_reclassified_row(accounting_db):
    # #1301: startup recovery that reclassifies two or more rows reseals every
    # changed row's digest. Each iteration's read autoflushes the row the
    # previous one changed, so a changed set read from ``db.dirty`` alone
    # missed it. The shadow full-ledger check (on for every test) then saw
    # ``ledger row digest drifted``; in production the next verification
    # resealed it with a false history gap.
    import json
    from sqlalchemy import select
    from src.db.models import InferenceAccountingOwner, WorkflowRunState
    from src.workflows import inference_ledger
    assert inference_ledger.SHADOW_FULL_LEDGER is True
    root, engine, factory = accounting_db
    setup_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    calls = []
    async def failing_after_contact():
        calls.append("failed")
        raise RuntimeError("provider connection dropped after the request was sent")
    operations = ("lost-one", "lost-two", "lost-three")
    for operation_id in operations:
        with pytest.raises(RuntimeError, match="dropped"):
            await broker.execute(request(operation_id), failing_after_contact, uncertain_on_error=False)
    before = {row["operation_id"]: row for row in (await repository.inference_accounting_snapshot())["operations"]}
    lapsed = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
    async with factory.accounting_sessions() as db:
        for operation_id in operations:
            run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == before[operation_id]["job_id"]))
            run.status, run.lease_owner, run.lease_expires_at = "running", "crashed-worker", lapsed
            db.add(run)
    async with factory.accounting_sessions() as db:
        gaps_before = [entry for entry in json.loads((await db.get(InferenceAccountingOwner, "deployment")).settings_history_json or "[]")
            if entry.get("kind") == "history_gap"]
    recovered = await DurableJobRepository().recover_inference_accounting()
    assert sorted(item["operation_id"] for item in recovered) == sorted(operations)
    after = await repository.inference_accounting_snapshot()
    assert after["history_verified"] is True and not after.get("history_gap_since")
    for operation_id in operations:
        row = next(item for item in after["operations"] if item["operation_id"] == operation_id)
        assert (row["state"], row["recovery_reason"]) == ("unknown", "provider_outcome_unknown")
    async with factory.accounting_sessions() as db:
        gaps_after = [entry for entry in json.loads((await db.get(InferenceAccountingOwner, "deployment")).settings_history_json or "[]")
            if entry.get("kind") == "history_gap"]
    assert gaps_after == gaps_before
    assert calls == ["failed"] * 3  # nothing was sent again


@pytest.mark.asyncio
async def test_failed_call_after_contact_is_recorded_as_no_answer_not_succeeded(accounting_db):
    # #1284: an unpriced record from a call that raised after contact says
    # "no answer"; its ephemeral job is never reported as succeeded. The
    # failure is declared certain here, so the serial lane (whose uncertain
    # result hold is unchanged) is free and the next request is answered.
    setup_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    async def failing_after_contact():
        raise RuntimeError("provider connection dropped after the request was sent")
    with pytest.raises(RuntimeError, match="dropped"):
        await broker.execute(request("failed-after-contact"), failing_after_contact, uncertain_on_error=False)
    snapshot = await repository.inference_accounting_snapshot()
    row = next(item for item in snapshot["operations"] if item["operation_id"] == "failed-after-contact")
    assert row["state"] == "unknown" and row["contact_started_at"] is not None
    assert row["recovery_reason"] == "provider_call_failed"
    assert snapshot["unpriced_requests"] == 1
    job = await repository.get_job(row["job_id"])
    assert job["status"] != "succeeded"
    async def answered():
        return {"id": "gen-after-failure", "usage": {"cost": "0.000002"}, "choices": [{"message": {"content": "ok"}}]}
    assert (await broker.execute(request("answered-after-failure"), answered))["choices"][0]["message"]["content"] == "ok"
    after = await repository.inference_accounting_snapshot()
    answered_row = next(item for item in after["operations"] if item["operation_id"] == "answered-after-failure")
    assert answered_row["state"] == "settled" and answered_row["recovery_reason"] is None
    assert (await repository.get_job(answered_row["job_id"]))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_old_database_and_settings_cannot_erase_outside_root_witness(accounting_db):
    root, engine, _factory = accounting_db
    setup_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    await engine.dispose()
    old_db = root.parent / "old.db"
    shutil.copy2(root / "seraph.db", old_db)
    old_metadata = (root / "model-fabric-settings.json").read_bytes()
    async def contacted():
        return {"usage": {"cost": "0.000010"}}
    await RemoteInferenceAdmissionBroker(durable_accounting=True).execute(request("new-contact"), contacted)
    await engine.dispose()
    shutil.copy2(old_db, root / "seraph.db")
    (root / "model-fabric-settings.json").write_bytes(old_metadata)
    snapshot = await repository.inference_accounting_snapshot()
    assert snapshot["status"] == "unavailable"
    calls = []
    with pytest.raises(ValueError, match="continuity_unavailable"):
        await RemoteInferenceAdmissionBroker(durable_accounting=True).execute(request("rollback-replay"), lambda: calls.append("forbidden"))
    assert calls == []
    with pytest.raises(InferenceAccountingError, match="continuity_unavailable"):
        await repository.ensure_inference_ledger()


@pytest.mark.asyncio
async def test_missing_witness_blocks_sync_and_stream_before_transport(accounting_db):
    root, _engine, _factory = accounting_db
    setup_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    lifecycle_receipt_path(ProductionWorkspace(host_root=root)).unlink()
    calls = []
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    with pytest.raises(ValueError, match="continuity_unavailable"):
        broker.execute_sync(request("sync-no-witness"), lambda: calls.append("sync"))
    async def stream():
        calls.append("stream")
        yield "delta"
    with pytest.raises(ValueError, match="continuity_unavailable"):
        async for _delta in broker.stream(request("stream-no-witness"), stream):
            pass
    assert calls == []


@pytest.mark.asyncio
async def test_duplicate_concurrent_reservations_execute_at_most_once(accounting_db):
    setup_configuration()
    await DurableJobRepository().ensure_inference_ledger()
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    calls = []
    in_contact = Gate("first contact")
    async def transport():
        calls.append("contact")
        await in_contact.arrive()
        return {"usage": {"cost": "0.000001"}}
    duplicates = [asyncio.create_task(broker.execute(request("concurrent"), transport)) for _ in range(2)]
    async with in_contact:
        # Hold the first contact open until the duplicate has been decided
        # (refused) or has itself reached the provider.
        await in_contact.wait_reached()
        await wait_until(lambda: any(task.done() for task in duplicates) or in_contact.arrivals > 1,
                         what="duplicate reservation decided while the first contact is open",
                         state=lambda: (calls, [task.done() for task in duplicates]))
        assert len(calls) == 1
    outcomes = await asyncio.gather(*duplicates, return_exceptions=True)
    assert len(calls) == 1, outcomes
    snapshot = await DurableJobRepository().inference_accounting_snapshot()
    assert snapshot["spent_microusd"] == 1
    assert len(snapshot["operations"]) == 1


@pytest.mark.asyncio
async def test_authenticated_settings_canary_unknown_manual_readback(accounting_db, monkeypatch):
    root, _engine, _factory = accounting_db
    from src.app import create_app
    from src.api.auth import _reset_login_throttle_for_tests
    monkeypatch.setattr(settings, "operator_auth_secret", "accounting-api-password")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)
    monkeypatch.setattr(settings, "openrouter_api_key", "intercepted-test-key")
    _reset_login_throttle_for_tests()
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    use_inference_broker(monkeypatch, broker)
    calls = []
    async def intercepted_post(client, url, **kwargs):
        calls.append(str(url))
        assert str(url) == "https://openrouter.ai/api/v1/chat/completions"
        return httpx.Response(200, json={"id": "gen-api", "choices": [{"message": {"content": "CANARY_OK"}}],
            "usage": {"prompt_tokens": 1}}, request=httpx.Request("POST", url))
    # ASGI requests use a separate method; patch only the final provider call.
    original_post = httpx.AsyncClient.post
    async def route_post(client, url, **kwargs):
        if str(url).startswith("https://openrouter.ai/"):
            return await intercepted_post(client, url, **kwargs)
        return await original_post(client, url, **kwargs)
    monkeypatch.setattr(httpx.AsyncClient, "post", route_post)
    headers = {"origin": "http://localhost:3001"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test") as client:
        login = await client.post("/api/auth/login", headers=headers, json={"password": "accounting-api-password"})
        assert login.status_code == 200, login.text
        setup = {"model_ids": ["openai/gpt-4o-mini"], "capabilities": ["text"], "allowed_upstreams": ["openai"],
            "data_collection": "deny", "data_retention_policy": "deny", "egress_class": "cloud_allowed_full",
            "cloud_egress_acknowledged": True, "spend_ceiling_microusd": 1000,
            "request_cost_bound_microusd": 100, "credential_ref": "env:OPENROUTER_API_KEY"}
        saved = await client.put("/api/settings/model-fabric", headers=headers, json={"openrouter_setup": setup})
        assert saved.status_code == 200, saved.text
        assert saved.json()["inference_accounting"]["status"] == "ready"
        from src.api.model_fabric_settings import model_fabric_runtime_status
        runtime = await model_fabric_runtime_status("openrouter")
        assert runtime["inference_accounting"]["status"] == "ready"
        assert "operations" not in runtime["inference_accounting"]
        canary = await client.post("/api/settings/model-fabric/canary", headers=headers,
            json={"profile_id": "openrouter", "capability": "text", "timeout_seconds": 120})
        assert canary.status_code == 200, canary.text
        assert len(calls) == 1, canary.text
        readback = (await client.get("/api/settings/model-fabric/accounting")).json()
        row = readback["operations"][0]
        assert row["state"] == "unknown" and readback["unpriced_requests"] == 1
        assert row["owner_id"] == login.json()["principal_id"]
        await client.post("/api/auth/logout", headers=headers)
        relogin = await client.post("/api/auth/login", headers=headers, json={"password": "accounting-api-password"})
        assert relogin.status_code == 200
        assert relogin.json()["principal_id"] != login.json()["principal_id"]
        retained = (await client.get("/api/settings/model-fabric/accounting")).json()
        assert retained["deployment_id"] == readback["deployment_id"]
        assert retained["unpriced_requests"] == 1
        assert retained["operations"][0]["owner_id"] == login.json()["principal_id"]
        body = {"operation_id": row["operation_id"], "job_id": row["job_id"], "expected_revision": row["revision"],
            "actual_cost_microusd": 9, "evidence_digest": "e" * 64, "idempotency_key": "api-manual-1"}
        foreign = await client.post(row["controls"][0]["endpoint"], headers=headers, json={**body, "job_id": "foreign-job"})
        assert foreign.status_code == 409
        stale = await client.post(row["controls"][0]["endpoint"], headers=headers, json={**body, "expected_revision": row["revision"] - 1})
        assert stale.status_code == 409
        settled = await client.post(row["controls"][0]["endpoint"], headers=headers, json=body)
        assert settled.status_code == 200, settled.text
        assert settled.json()["job_authority_changed"] is False
        repeated = await client.post(row["controls"][0]["endpoint"], headers=headers, json=body)
        assert repeated.json() == settled.json()
        final = (await client.get("/api/settings/model-fabric/accounting")).json()
        assert final["spent_microusd"] == 9 and final["unpriced_requests"] == 0
        evidence = json.loads(final["operations"][0]["evidence_json"])[-1]
        assert evidence["provenance"] == "manual_externally_unverified"
        assert evidence["provider_charge_verified"] is False
        assert (await DurableJobRepository().get_job(row["job_id"]))["status"] == "succeeded"
        lifecycle_receipt_path(ProductionWorkspace(host_root=root)).unlink()
        blocked_runtime = await model_fabric_runtime_status("openrouter")
        assert blocked_runtime["inference_readiness"]["status"] == "blocked"
        assert blocked_runtime["openrouter_setup"]["status"] == "blocked"


@pytest.mark.asyncio
async def test_restore_carries_latest_ledger_and_missing_witness_refuses(accounting_db):
    import sqlite3
    from src.workspace.accounting_continuity import retain_inference_accounting
    from src.workspace.production import ProductionWorkspaceReconciliationError
    root, engine, _factory = accounting_db
    setup_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    await engine.dispose()
    restored = root.parent / "staged"
    restored.mkdir()
    shutil.copy2(root / "seraph.db", restored / "seraph.db")
    async def provider():
        return {"usage": {"cost": "0.000013"}}
    await RemoteInferenceAdmissionBroker(durable_accounting=True).execute(request("restore-latest"), provider)
    await engine.dispose()
    receipt = retain_inference_accounting(active=root, target=restored, database_path="seraph.db")
    assert receipt["status"] == "retained_latest" and receipt["operations"] == 1
    # Repeat after a migration crash is idempotent. Promote only the staged
    # DB into the same descriptor-owned root; witness stays outside it.
    assert retain_inference_accounting(active=root, target=restored, database_path="seraph.db") == receipt
    shutil.copy2(restored / "seraph.db", root / "seraph.db")
    assert (await repository.inference_accounting_snapshot())["reason_code"] == "general_task_group_lookup_invalid"
    from src.db.engine import _ensure_inference_group_lookup
    async with engine.begin() as connection:
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        await _ensure_inference_group_lookup(connection)
    snapshot = await repository.inference_accounting_snapshot()
    assert snapshot["spent_microusd"] == 13
    assert snapshot["operations"][0]["owner_id"] == "service:accounting"
    assert (await repository.get_job(snapshot["operations"][0]["job_id"]))["status"] == "blocked"
    lifecycle_receipt_path(ProductionWorkspace(host_root=root)).unlink()
    with pytest.raises((InferenceAccountingError, ProductionWorkspaceReconciliationError)):
        retain_inference_accounting(active=root, target=restored, database_path="seraph.db")


@pytest.mark.asyncio
async def test_retired_audio_and_invalid_owner_never_contact(accounting_db):
    # #1284: a missing ledger is created on the first request, so it no
    # longer denies; the retired audio route and owner authority still do.
    setup_configuration()
    calls = []
    async def provider():
        calls.append("forbidden")
    candidate = replace(request("missing:audio_transcription"), runtime_path="audio_transcription")
    with pytest.raises(ValueError, match="audio_route_retired"):  # #1179
        await RemoteInferenceAdmissionBroker(durable_accounting=True).execute(candidate, provider)
    from src.db.models import WorkflowRunState, InferenceCostReservation
    from sqlalchemy import select
    async with accounting_db[2].accounting_sessions() as db:
        assert (await db.execute(select(WorkflowRunState))).scalars().all() == []
        assert (await db.execute(select(InferenceCostReservation))).scalars().all() == []
    await DurableJobRepository().ensure_inference_ledger()
    with pytest.raises(ValueError, match="authority_invalid"):
        await RemoteInferenceAdmissionBroker(durable_accounting=True).execute(request("invalid-owner", owner="unbound-principal"), provider)
    assert calls == []


@pytest.mark.asyncio
async def test_priority_serial_execution_cancel_and_revocation_retain_cost(accounting_db, monkeypatch):
    from sqlalchemy import event, text
    import sys
    from src.model_fabric.configuration import read_model_fabric_configuration
    root, engine, _factory = accounting_db
    setup_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    started, release = asyncio.Event(), asyncio.Event()
    changed = asyncio.Event()
    loop = asyncio.get_running_loop()
    requests = {
        "active": request("active"),
        "background": request("background", priority=RemoteInferencePriority.SCREENSHOT_BACKGROUND),
        "foreground": request("foreground"),
    }
    # Convert the earliest original deadline once; preparation receives no extra time.
    barrier_deadline = loop.time() + min(item.deadline_at for item in requests.values()) - time.time()
    prepared, enqueued, failures, trace, tasks = {}, {}, [], [], []
    def observe(phase, outcome, **details):
        task = asyncio.current_task()
        trace.append({"monotonic": loop.time(), "phase": phase, "outcome": outcome,
                      "task": task.get_name() if task else None, **details})
    def broker_observer(phase, original, completions):
        async def observed(candidate, **kwargs):
            if candidate.operation_id not in requests:
                return await original(candidate, **kwargs)
            observe(phase, "enter", operation_id=candidate.operation_id)
            try:
                actual = await original(candidate, **kwargs)
            except BaseException as exc:
                failures.append(exc)
                observe(phase, "error", operation_id=candidate.operation_id, error=type(exc).__name__)
                changed.set()
                raise
            # Retain the actual result solely as a completion notification.
            completions[candidate.operation_id] = actual
            observe(phase, "return", operation_id=candidate.operation_id)
            changed.set()
            return actual
        return observed
    monkeypatch.setattr(broker, "_prepare_accounting", broker_observer("prepare", broker._prepare_accounting, prepared))
    monkeypatch.setattr(broker, "enqueue", broker_observer("enqueue", broker.enqueue, enqueued))
    # Observe delegated original durable phases, without changing their inputs/results.
    for phase in ("admit_job", "queue_job", "claim_job", "reserve_inference_cost"):
        original = getattr(DurableJobRepository, phase)
        def repository_observer(phase, original):
            async def observed(self, *args, **kwargs):
                observe(phase, "enter")
                try:
                    actual = await original(self, *args, **kwargs)
                except BaseException as exc:
                    observe(phase, "error", error=type(exc).__name__)
                    raise
                observe(phase, "return")
                return actual
            return observed
        monkeypatch.setattr(DurableJobRepository, phase, repository_observer(phase, original))
    def statement_observer(connection, cursor, statement, parameters, context, executemany):
        verb = statement.split(None, 1)[0].upper() if statement.strip() else ""
        if verb in {"BEGIN", "COMMIT", "ROLLBACK"}:
            observe("sql", verb, connection=id(connection))
    def commit_observer(connection):
        observe("sql", "COMMIT", connection=id(connection))
    def rollback_observer(connection):
        observe("sql", "ROLLBACK", connection=id(connection))
    listeners = (("before_cursor_execute", statement_observer), ("commit", commit_observer),
                 ("rollback", rollback_observer))
    for name, listener in listeners:
        event.listen(engine.sync_engine, name, listener)
    async def wait_for(predicate):
        while not predicate():
            changed.clear()
            if failures:
                raise failures[0]
            await changed.wait()
        if failures:
            raise failures[0]
    order = []
    async def active():
        order.append("active")
        started.set()
        changed.set()
        await release.wait()
        return {"usage": {"cost": "0.000001"}}
    async def queued(name):
        order.append(name)
        return {"usage": {"cost": "0.000001"}}
    succeeded = False
    try:
        async with asyncio.timeout_at(barrier_deadline):
            tasks.append(asyncio.create_task(broker.execute(requests["active"], active), name="accounting-active"))
            await wait_for(started.is_set)
            tasks.append(asyncio.create_task(broker.execute(requests["background"], lambda: queued("background")), name="accounting-background"))
            tasks.append(asyncio.create_task(broker.execute(requests["foreground"], lambda: queued("foreground")), name="accounting-foreground"))
            await wait_for(lambda: {"background", "foreground"} <= prepared.keys())
            await wait_for(lambda: {"background", "foreground"} <= enqueued.keys())
            observe("canonical_snapshot", "enter")
            snapshot = await repository.inference_accounting_snapshot()
            observe("canonical_snapshot", "return", status=snapshot["status"],
                    operations=[{"operation_id": row["operation_id"], "job_id": row["job_id"],
                                 "state": row["state"]} for row in snapshot["operations"]])
            assert snapshot["status"] == "ready"
            assert len(snapshot["operations"]) == 3
            assert {row["operation_id"]: row["job_id"] for row in snapshot["operations"]} == {
                operation_id: prepared[operation_id].job_id for operation_id in requests}
            assert {row["operation_id"]: row["state"] for row in snapshot["operations"]} == {
                "active": "contact_started", "background": "reserved", "foreground": "reserved"}
            release.set()
            # Observer timeout must not cancel children before our single cleanup cancel.
            await asyncio.wait(tasks)
            for task in tasks:
                task.result()
        assert order == ["active", "foreground", "background"]
        assert (await repository.inference_accounting_snapshot())["spent_microusd"] == 3
        async def revoke_after_charge():
            from src.model_fabric.accounting import capture_inference_usage
            capture_inference_usage({"usage": {"cost": "0.000005"}})
            configured = read_model_fabric_configuration()
            write_model_fabric_configuration(replace(configured, egress_revoked=True, egress_revision=configured.egress_revision + 1))
            return {"content": "must not adopt"}
        with pytest.raises((PermissionError, InferenceAccountingError)):
            await broker.execute(request("revoked-after-contact"), revoke_after_charge)
        assert (await repository.inference_accounting_snapshot())["spent_microusd"] == 8
        observe("assertions", "passed", order=order, spent_microusd=8)
        succeeded = True
    finally:
        primary = sys.exception()
        release.set()
        for task in tasks:
            if not succeeded and not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        observe("cleanup", "return", tasks=[{"name": task.get_name(), "done": task.done(),
            "cancelled": task.cancelled(), "error": type(result).__name__ if isinstance(result, BaseException) else None}
            for task, result in zip(tasks, results)])
        if primary is not None:
            for task, result in zip(tasks, results):
                if isinstance(result, BaseException) and result is not primary:
                    primary.add_note(f"Accounting owned task {task.get_name()}: {type(result).__name__}: {result}")
        # Partial preparation rows are diagnostic only; cleanup never transitions them.
        if not succeeded:
            try:
                async with engine.connect() as connection:
                    rows = (await connection.execute(text("SELECT operation_id, state FROM inference_cost_reservations"))).all()
                    jobs = (await connection.execute(text("SELECT id, status FROM workflow_run_states"))).all()
                    observe("failure_rows", "read", operations=[list(row) for row in rows],
                            jobs=[list(row) for row in jobs])
            except Exception as exc:
                observe("failure_rows", "error", error=type(exc).__name__)
                if primary is not None:
                    primary.add_note(f"Accounting cleanup readback failed: {type(exc).__name__}: {exc}")
        for name, listener in listeners:
            event.remove(engine.sync_engine, name, listener)
        try:
            (root.parent / "accounting-observation-trace.json").write_text(json.dumps(trace, indent=2) + "\n")
        except Exception as exc:
            if primary is None:
                raise
            primary.add_note(f"Accounting trace write failed: {type(exc).__name__}: {exc}")


@pytest.mark.asyncio
async def test_witness_before_commit_crash_explicit_maintenance_retains_contact(accounting_db, monkeypatch):
    from src.workspace.production import maintenance_fence, read_accounting_checkpoint
    from src.workspace import canonical_workspace_registry
    from src.workspace.accounting_continuity import reconcile_accounting_checkpoint
    root, engine, _factory = accounting_db
    setup_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    workspace = ProductionWorkspace(host_root=root)
    original_commit = AsyncSession.commit
    fail_once = True
    async def injected_commit(db):
        nonlocal fail_once
        checkpoint = read_accounting_checkpoint(workspace)
        if fail_once and checkpoint and any(row["state"] == "contact_started" for row in checkpoint["operations"]):
            fail_once = False
            raise RuntimeError("injected crash after witness before DB commit")
        await original_commit(db)
    monkeypatch.setattr(AsyncSession, "commit", injected_commit)
    calls = []
    async def forbidden():
        calls.append("provider")
    with pytest.raises(Exception):
        await RemoteInferenceAdmissionBroker(durable_accounting=True).execute(request("commit-gap"), forbidden)
    assert not fail_once and calls == []
    await engine.dispose()
    assert (await repository.inference_accounting_snapshot())["status"] == "unavailable"
    with pytest.raises(ValueError, match="continuity_unavailable"):
        await RemoteInferenceAdmissionBroker(durable_accounting=True).execute(request("commit-gap-new"), forbidden)
    registry = canonical_workspace_registry(root)
    with maintenance_fence(workspace):
        receipt = reconcile_accounting_checkpoint(root=root, registry=registry)
    assert receipt["status"] == "reconciled" and receipt["execution_authority_changed"] is False
    # Dependency-free maintenance does not classify the private projection.
    # Canonical startup must backfill it before accounting/group use resumes.
    assert (await repository.inference_accounting_snapshot())["reason_code"] == "general_task_group_lookup_invalid"
    from src.db.engine import _ensure_inference_group_lookup
    async with engine.begin() as connection:
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        await _ensure_inference_group_lookup(connection)
    after = await repository.inference_accounting_snapshot()
    assert after["status"] == "ready"
    assert after["operations"][0]["state"] == "contact_started"
    with maintenance_fence(workspace):
        assert reconcile_accounting_checkpoint(root=root, registry=registry)["status"] == "already_reconciled"
    assert (workspace.lifecycle_directory / "accounting-checkpoint.json").stat().st_mode & 0o077 == 0
    assert calls == []


@pytest.mark.asyncio
async def test_charge_above_legacy_bound_settles_and_next_admission_proceeds(accounting_db):
    # #1284: a legacy stored bound is read past; no overrun latch exists.
    setup_configuration(ceiling=1000, bound=1)
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    calls = []
    async def charged():
        calls.append("provider")
        return {"usage": {"cost": "0.0000051"}}
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    await broker.execute(request("over-bound"), charged)
    snapshot = await repository.inference_accounting_snapshot()
    assert snapshot["spent_microusd"] == 6
    assert snapshot["operations"][0]["state"] == "settled"
    assert snapshot["operations"][0]["recovery_reason"] is None
    await broker.execute(request("over-bound-next"), charged)
    assert len(calls) == 2
    assert (await repository.inference_accounting_snapshot())["spent_microusd"] == 12


@pytest.mark.asyncio
async def test_shared_sync_async_stream_broker_one_active(accounting_db):
    setup_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    started, release = asyncio.Event(), asyncio.Event()
    order = []
    async def stream_provider():
        order.append("stream")
        started.set()
        await release.wait()
        yield {"usage": {"cost": "0.000001"}}
    async def consume():
        return [part async for part in broker.stream(request("mixed-stream"), stream_provider)]
    streaming = asyncio.create_task(consume())
    await started_or_raise(started, streaming)
    def sync_provider():
        order.append("sync")
        return {"usage": {"cost": "0.000001"}}
    syncing = asyncio.create_task(asyncio.to_thread(broker.execute_sync, request("mixed-sync"), sync_provider))
    async def async_provider():
        order.append("async")
        return {"usage": {"cost": "0.000001"}}
    executing = asyncio.create_task(broker.execute(request("mixed-async"), async_provider))
    # Both later callers are parked in the one shared admission queue behind
    # the active stream (their durable reservations precede enqueue).
    await wait_until(lambda: set(broker._queue) == {"mixed-sync", "mixed-async"},
                     what="sync and async callers queued behind the stream", task=executing,
                     state=lambda: (list(broker._queue), order))
    await settle()
    assert order == ["stream"]
    assert len((await repository.inference_accounting_snapshot())["operations"]) == 3
    release.set()
    await asyncio.gather(streaming, syncing, executing)
    assert sorted(order) == ["async", "stream", "sync"]
    assert (await repository.inference_accounting_snapshot())["spent_microusd"] == 3


@pytest.mark.asyncio
async def test_cancel_before_and_after_contact_retains_unknown_without_replay(accounting_db):
    _root, engine, _factory = accounting_db
    setup_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    started = asyncio.Event()
    calls = []
    async def contacted():
        calls.append("contacted")
        started.set()
        await asyncio.Event().wait()
    async def forbidden():
        calls.append("must not contact")
    active = asyncio.create_task(broker.execute(request("cancel-contacted"), contacted))
    await started_or_raise(started, active)
    queued = asyncio.create_task(broker.execute(request("cancel-queued"), forbidden))
    await wait_until(lambda: broker._queue == ["cancel-queued"], what="second request queued behind the contact",
                     task=queued, state=lambda: list(broker._queue))
    snapshot = await repository.inference_accounting_snapshot()
    assert len(snapshot["operations"]) == 2
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    active.cancel()
    from src.model_fabric.gpu_admission import GpuAdmissionUncertainError
    with pytest.raises((asyncio.CancelledError, GpuAdmissionUncertainError)):
        await active
    await engine.dispose()
    recreated = RemoteInferenceAdmissionBroker(durable_accounting=True)
    snapshot = await repository.inference_accounting_snapshot()
    rows = {row["operation_id"]: row for row in snapshot["operations"]}
    assert rows["cancel-queued"]["state"] == "released"
    assert rows["cancel-contacted"]["state"] == "unknown"
    assert snapshot["unpriced_requests"] == 1
    await repository.ensure_inference_ledger()
    assert (await repository.inference_accounting_snapshot())["unpriced_requests"] == 1
    with pytest.raises((ValueError, InferenceAccountingError)):
        await recreated.execute(request("cancel-contacted"), forbidden)
    assert calls == ["contacted"]


@pytest.mark.asyncio
async def test_status_snapshot_never_takes_the_writer_lock_or_writes(accounting_db, monkeypatch):
    """#702: status polls ran BEGIN IMMEDIATE and queued behind any writer."""
    import sqlite3
    import threading
    from sqlalchemy import event
    from src.workspace.production import read_lifecycle_receipt, write_lifecycle_receipt
    root, engine, _factory = accounting_db
    setup_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    statements = []
    event.listen(engine.sync_engine, "before_cursor_execute",
        lambda _conn, _cursor, statement, *_args: statements.append(" ".join(statement.split()[:2]).upper()))

    ready = await repository.inference_accounting_snapshot()
    assert ready["status"] == "ready" and ready["accounting_continuity_verified"] is True
    assert "BEGIN IMMEDIATE" not in statements
    assert not [item for item in statements if item.split()[0] in {"INSERT", "UPDATE", "DELETE"}]

    held, release = threading.Event(), threading.Event()
    def foreign_writer():
        connection = sqlite3.connect(root / "seraph.db", isolation_level=None, timeout=30)
        connection.execute("BEGIN IMMEDIATE")
        held.set()
        release.wait(10)
        connection.execute("COMMIT")
        connection.close()
    writer = threading.Thread(target=foreign_writer, daemon=True)
    writer.start()
    assert held.wait(5)
    try:
        statements.clear()
        # The foreign writer is released only after this read returns, so a
        # read that queued for the writer lock could never finish here; the
        # liveness bound turns that into a loud failure, not a timing oracle.
        contended = await within(repository.inference_accounting_snapshot(),
                                 what="status snapshot while a foreign writer holds the lock")
        assert not release.is_set() and writer.is_alive()
        assert "BEGIN IMMEDIATE" not in statements
        assert not [item for item in statements if item.split()[0] in {"INSERT", "UPDATE", "DELETE"}]
        assert contended["status"] == "ready" and contended["revision"] == ready["revision"]
    finally:
        release.set()
        writer.join(10)

    # #1284: a new UTC month is display only; a read never writes a period.
    statements.clear()
    rolled = await repository.inference_accounting_snapshot(now=datetime.now(timezone.utc) + timedelta(days=400))
    assert rolled["status"] == "ready" and rolled["reason_code"] is None
    assert rolled["revision"] == ready["revision"] and rolled["period_id"] != ready["period_id"]
    assert "BEGIN IMMEDIATE" not in statements
    assert not [item for item in statements if item.split()[0] in {"INSERT", "UPDATE", "DELETE"}]

    # A witness ahead of the snapshot (a writer between witness and commit)
    # is settled by the serialized path, which keeps failing closed if real.
    workspace = ProductionWorkspace(host_root=root)
    receipt = read_lifecycle_receipt(workspace)
    receipt["inference_accounting"] = {**receipt["inference_accounting"], "revision": ready["revision"] + 1}
    write_lifecycle_receipt(workspace, receipt)
    statements.clear()
    diverged = await repository.inference_accounting_snapshot()
    assert diverged["status"] == "unavailable" and diverged["reason_code"] == "accounting_continuity_unavailable"
    assert "BEGIN IMMEDIATE" in statements


@pytest.mark.asyncio
@pytest.mark.parametrize("ahead_on_read", [1, 2])
async def test_status_snapshot_falls_back_to_serialized_read_when_witness_moves(accounting_db, monkeypatch, ahead_on_read):
    """#702 review: an unserialized read can see a witness persisted ahead of its commit.

    Whichever receipt read observes the moved witness, the snapshot must not
    report broken continuity without the serialized read confirming it.
    """
    from sqlalchemy import event
    from src.workflows import inference_accounting
    root, engine, _factory = accounting_db
    setup_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    ready = await repository.inference_accounting_snapshot()
    statements = []
    event.listen(engine.sync_engine, "before_cursor_execute",
        lambda _conn, _cursor, statement, *_args: statements.append(" ".join(statement.split()[:2]).upper()))
    real_read = inference_accounting.read_lifecycle_receipt
    reads = []

    def witness_ahead_once(workspace):
        receipt = real_read(workspace)
        reads.append(receipt)
        if len(reads) == ahead_on_read:
            # The unserialized read observes a writer between witness and commit.
            return {**receipt, "inference_accounting": {**receipt["inference_accounting"],
                "revision": receipt["inference_accounting"]["revision"] + 1}}
        return receipt

    monkeypatch.setattr(inference_accounting, "read_lifecycle_receipt", witness_ahead_once)
    settled = await repository.inference_accounting_snapshot()
    assert settled["status"] == "ready" and settled["revision"] == ready["revision"]
    # Read 1 is the unserialized verification; a moved witness there escalates.
    assert ("BEGIN IMMEDIATE" in statements) is (ahead_on_read == 1)
