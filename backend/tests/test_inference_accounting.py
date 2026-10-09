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


@pytest_asyncio.fixture
async def accounting_db(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(settings, "workspace_dir", str(root))
    monkeypatch.setenv("SERAPH_WORKSPACE_LIFECYCLE_PATH", str(tmp_path / "deployment-lifecycle"))
    from src.workspace.production import prepare_lifecycle_directory
    prepare_lifecycle_directory(ProductionWorkspace(host_root=root))
    engine = create_async_engine(f"sqlite+aiosqlite:///{root / 'seraph.db'}", connect_args={"timeout": 5})
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    @asynccontextmanager
    async def sessions():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
    for target in ("src.workflows.job_runtime.get_session", "src.workflows.durable_state.get_session",
                   "src.db.engine.get_session", "src.auth.service.get_session",
                   "src.model_fabric.repository.get_session", "src.api.settings.get_db",
                   "src.vault.repository.get_session", "src.audit.repository.get_session",
                   "src.work_board.dispatcher.get_session", "src.api.work_board.get_session",
                   "src.api.calendar.get_session", "src.goals.repository.get_session",
                   "src.agent.session.get_session", "src.approval.repository.get_session"):
        monkeypatch.setattr(target, sessions)
    factory.accounting_sessions = sessions
    yield root, engine, factory
    await engine.dispose()


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
        runtime_path="chat_agent", data_digest="a" * 64, owner_budget_microusd=1000)


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
    await repository.configure_inference_accounting(1000)
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    calls = []
    async def transport():
        calls.append("actual intercepted callback")
        return {"id": "gen-test", "usage": {"cost": "0.0000061"}, "choices": [{"message": {"content": "result"}}]}
    result = await broker.execute(request("operation-one"), transport)
    assert result["choices"][0]["message"]["content"] == "result"
    before = await repository.inference_accounting_snapshot()
    assert before["status"] == "ready", before
    assert before["committed_microusd"] == 7
    assert before["remaining_microusd"] == 993
    assert calls == ["actual intercepted callback"]
    job = await repository.get_job(before["operations"][0]["job_id"])
    assert job["status"] == "succeeded"
    assert "no_learning" in job["result"]["summary"]
    await engine.dispose()
    restarted = RemoteInferenceAdmissionBroker(durable_accounting=True)
    assert (await DurableJobRepository().inference_accounting_snapshot())["remaining_microusd"] == 993
    with pytest.raises(ValueError):
        await restarted.execute(request("operation-one"), transport)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unknown_cost_survives_rollover_and_explicit_revision_settlement(accounting_db):
    setup_configuration(ceiling=100, bound=100)
    repository = DurableJobRepository()
    await repository.configure_inference_accounting(100)
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    async def no_cost():
        return {"id": "embd-fixture", "usage": {"prompt_tokens": 5}}
    await broker.execute(request("embedding", owner="service:embedding"), no_cost)
    snapshot = await repository.inference_accounting_snapshot(now=datetime.now(timezone.utc) + timedelta(days=70))
    row = snapshot["operations"][0]
    assert snapshot["unknown_microusd"] == 100
    assert snapshot["remaining_microusd"] == 0
    assert row["state"] == "unknown"
    with pytest.raises(ValueError):
        await RemoteInferenceAdmissionBroker(durable_accounting=True).execute(request("new-owner", owner="service:fresh-owner"), no_cost)
    with pytest.raises(InferenceAccountingError, match="revision_changed"):
        await repository.settle_inference_cost(row["operation_id"], job_id=row["job_id"],
            expected_revision=row["revision"] - 1, actual_cost_microusd=12,
            evidence_digest="b" * 64, operator_id="operator:settings-owner", idempotency_key="manual-1")
    await repository.settle_inference_cost(row["operation_id"], job_id=row["job_id"],
        expected_revision=row["revision"], actual_cost_microusd=12,
        evidence_digest="b" * 64, operator_id="operator:settings-owner", idempotency_key="manual-1")
    after = await repository.inference_accounting_snapshot()
    assert after["committed_microusd"] == 12
    assert after["unknown_microusd"] == 0
    assert after["operations"][0]["period_id"] == row["period_id"]
    # Settlement is billing authority only; the unknown job is not resumed.
    assert (await repository.get_job(row["job_id"]))["status"] == "cost_liability"


@pytest.mark.asyncio
async def test_old_database_and_settings_cannot_erase_outside_root_witness(accounting_db):
    root, engine, _factory = accounting_db
    setup_configuration()
    repository = DurableJobRepository()
    await repository.configure_inference_accounting(1000)
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
    assert snapshot["status"] == "blocked"
    calls = []
    with pytest.raises(ValueError, match="continuity_unavailable"):
        await RemoteInferenceAdmissionBroker(durable_accounting=True).execute(request("rollback-replay"), lambda: calls.append("forbidden"))
    assert calls == []
    with pytest.raises(InferenceAccountingError, match="continuity_unavailable"):
        await repository.configure_inference_accounting(1000)


@pytest.mark.asyncio
async def test_missing_witness_blocks_sync_and_stream_before_transport(accounting_db):
    root, _engine, _factory = accounting_db
    setup_configuration()
    repository = DurableJobRepository()
    await repository.configure_inference_accounting(1000)
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
    await DurableJobRepository().configure_inference_accounting(1000)
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    calls = []
    async def transport():
        calls.append("contact")
        await asyncio.sleep(0.02)
        return {"usage": {"cost": "0.000001"}}
    outcomes = await asyncio.gather(broker.execute(request("concurrent"), transport), broker.execute(request("concurrent"), transport), return_exceptions=True)
    assert len(calls) == 1, outcomes
    snapshot = await DurableJobRepository().inference_accounting_snapshot()
    assert snapshot["committed_microusd"] == 1
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
    monkeypatch.setattr("src.api.model_fabric_settings.remote_inference_admission_broker", broker)
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
        assert saved.json()["inference_accounting"]["remaining_microusd"] == 1000
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
        assert row["state"] == "unknown" and readback["unknown_microusd"] == 100
        assert row["owner_id"] == login.json()["principal_id"]
        await client.post("/api/auth/logout", headers=headers)
        relogin = await client.post("/api/auth/login", headers=headers, json={"password": "accounting-api-password"})
        assert relogin.status_code == 200
        assert relogin.json()["principal_id"] != login.json()["principal_id"]
        retained = (await client.get("/api/settings/model-fabric/accounting")).json()
        assert retained["deployment_id"] == readback["deployment_id"]
        assert retained["unknown_microusd"] == 100 and retained["remaining_microusd"] == 900
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
        assert final["committed_microusd"] == 9 and final["unknown_microusd"] == 0
        evidence = json.loads(final["operations"][0]["evidence_json"])[-1]
        assert evidence["provenance"] == "manual_externally_unverified"
        assert evidence["provider_charge_verified"] is False
        assert (await DurableJobRepository().get_job(row["job_id"]))["status"] == "cost_liability"
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
    await repository.configure_inference_accounting(1000)
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
    assert snapshot["committed_microusd"] == 13 and snapshot["remaining_microusd"] == 987
    assert snapshot["operations"][0]["owner_id"] == "service:accounting"
    assert (await repository.get_job(snapshot["operations"][0]["job_id"]))["status"] == "blocked"
    lifecycle_receipt_path(ProductionWorkspace(host_root=root)).unlink()
    with pytest.raises((InferenceAccountingError, ProductionWorkspaceReconciliationError)):
        retain_inference_accounting(active=root, target=restored, database_path="seraph.db")


@pytest.mark.asyncio
async def test_all_billable_kinds_missing_ledger_invalid_owner_never_contact(accounting_db):
    setup_configuration()
    calls = []
    async def provider():
        calls.append("forbidden")
    for runtime_path in ("chat_agent", "embedding", "capability_probe", "screen_artifact_analysis", "audio_transcription"):
        candidate = replace(request("missing:" + runtime_path), runtime_path=runtime_path)
        with pytest.raises(ValueError, match="continuity_unavailable"):
            await RemoteInferenceAdmissionBroker(durable_accounting=True).execute(candidate, provider)
    await DurableJobRepository().configure_inference_accounting(1000)
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
    await repository.configure_inference_accounting(1000)
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
        assert (await repository.inference_accounting_snapshot())["committed_microusd"] == 3
        async def revoke_after_charge():
            from src.model_fabric.accounting import capture_inference_usage
            capture_inference_usage({"usage": {"cost": "0.000005"}})
            configured = read_model_fabric_configuration()
            write_model_fabric_configuration(replace(configured, egress_revoked=True, egress_revision=configured.egress_revision + 1))
            return {"content": "must not adopt"}
        with pytest.raises((PermissionError, InferenceAccountingError)):
            await broker.execute(request("revoked-after-contact"), revoke_after_charge)
        assert (await repository.inference_accounting_snapshot())["committed_microusd"] == 8
        observe("assertions", "passed", order=order, committed_microusd=8)
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
    await repository.configure_inference_accounting(1000)
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
    assert (await repository.inference_accounting_snapshot())["status"] == "blocked"
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
    assert after["status"] == "ready" and after["unknown_microusd"] == 100
    assert after["operations"][0]["state"] == "contact_started"
    with maintenance_fence(workspace):
        assert reconcile_accounting_checkpoint(root=root, registry=registry)["status"] == "already_reconciled"
    assert (workspace.lifecycle_directory / "accounting-checkpoint.json").stat().st_mode & 0o077 == 0
    assert calls == []


@pytest.mark.asyncio
async def test_charge_above_bound_retained_and_next_admission_blocks(accounting_db):
    setup_configuration(ceiling=1000, bound=1)
    repository = DurableJobRepository()
    await repository.configure_inference_accounting(1000)
    calls = []
    async def charged():
        calls.append("provider")
        return {"usage": {"cost": "0.0000051"}}
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    await broker.execute(request("over-bound"), charged)
    snapshot = await repository.inference_accounting_snapshot()
    assert snapshot["committed_microusd"] == 6
    assert snapshot["operations"][0]["recovery_reason"] == "provider_charge_exceeded_reservation"
    with pytest.raises(ValueError, match="exceeded_reservation"):
        await broker.execute(request("over-bound-next"), charged)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_shared_sync_async_stream_broker_one_active(accounting_db):
    setup_configuration()
    repository = DurableJobRepository()
    await repository.configure_inference_accounting(1000)
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
    await started.wait()
    def sync_provider():
        order.append("sync")
        return {"usage": {"cost": "0.000001"}}
    syncing = asyncio.create_task(asyncio.to_thread(broker.execute_sync, request("mixed-sync"), sync_provider))
    async def async_provider():
        order.append("async")
        return {"usage": {"cost": "0.000001"}}
    executing = asyncio.create_task(broker.execute(request("mixed-async"), async_provider))
    for _ in range(50):
        snapshot = await repository.inference_accounting_snapshot()
        if len(snapshot.get("operations", [])) == 3:
            break
        await asyncio.sleep(0.01)
    assert order == ["stream"]
    release.set()
    await asyncio.gather(streaming, syncing, executing)
    assert sorted(order) == ["async", "stream", "sync"]
    assert (await repository.inference_accounting_snapshot())["committed_microusd"] == 3


@pytest.mark.asyncio
async def test_cancel_before_and_after_contact_retains_unknown_without_replay(accounting_db):
    _root, engine, _factory = accounting_db
    setup_configuration()
    repository = DurableJobRepository()
    await repository.configure_inference_accounting(1000)
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
    await started.wait()
    queued = asyncio.create_task(broker.execute(request("cancel-queued"), forbidden))
    for _ in range(50):
        snapshot = await repository.inference_accounting_snapshot()
        if len(snapshot.get("operations", [])) == 2:
            break
        await asyncio.sleep(0.01)
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
    assert snapshot["unknown_microusd"] == 100 and snapshot["remaining_microusd"] == 900
    await repository.configure_inference_accounting(1200)
    revised = await repository.inference_accounting_snapshot()
    assert revised["unknown_microusd"] == 100 and revised["remaining_microusd"] == 1100
    with pytest.raises((ValueError, InferenceAccountingError)):
        await recreated.execute(request("cancel-contacted"), forbidden)
    assert calls == ["contacted"]
