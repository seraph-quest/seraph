"""Authenticated HTTP -> actual stock host -> original native read owner."""
import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import select

from config.settings import settings
from src.auth.middleware import OperatorAuthMiddleware
from src.auth.service import create_session
from src.db.engine import get_session
from src.db.models import Memory, GoogleServiceConnection, WorkflowRunState, Secret, AuditEvent
from src.runtime_plugins.bridge import CordisHost
from src.runtime_plugins.dispatch import NativeServiceDispatcher
from src.runtime_plugins.ownership import begin_native_writer
from src.workflows.job_runtime import DurableJobRepository
from tests.test_runtime_composition_ownership import composition_db


@pytest_asyncio.fixture
async def actual_reads(composition_db, monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "isolated-real-read-auth")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "localhost")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    token, operator = await create_session()
    repo = DurableJobRepository()
    dispatcher = NativeServiceDispatcher(jobs=repo)
    calls = []
    original = dispatcher.dispatch
    async def observe(frame, **kwargs):
        calls.append(json.loads(json.dumps(frame)))
        return await original(frame, **kwargs)
    monkeypatch.setattr(dispatcher, "dispatch", observe)
    host = CordisHost(node_path=Path(os.environ["SERAPH_CORDIS_TEST_NODE"]), service_dispatch=dispatcher)
    monkeypatch.setattr("src.runtime_plugins.bridge.cordis_host", host)
    monkeypatch.setattr("src.workflows.job_runtime.durable_job_repository", repo)
    from src.api.capabilities import router as capabilities
    from src.api.calendar import router as calendar
    from src.api.memory import router as memory
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    for router in (capabilities, calendar, memory):
        app.include_router(router, prefix="/api")
    try:
        assert await host.start(), host.snapshot()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
            cookies={settings.operator_auth_cookie_name: token}, headers={"Origin": "http://localhost:3001"}) as client:
            yield client, operator, repo, host, calls
    finally:
        await host.stop(preserve_blocked=host.state == "blocked")
        if host._cleanup_task is not None:
            await host._cleanup_task
        assert host.snapshot()["cleanup"]["process_reaped"] is True


@pytest.mark.asyncio
async def test_actual_all_four_authenticated_native_reads_and_exact_retry(actual_reads, monkeypatch):
    client, operator, repo, host, calls = actual_reads
    vault_key = Fernet.generate_key()
    monkeypatch.setattr(settings, "vault_encryption_key", vault_key.decode())
    async with get_session() as db:
        await begin_native_writer(db, owner="native_ingress")
        db.add(Secret(key="actual-read-fixture", encrypted_value=Fernet(vault_key).encrypt(b"known-private-secret").decode()))
        db.add(Memory(id="read-owner-memory", content="raw never enters child", summary="known-private-secret café summary",
            source_session_id=operator.session_id))
        db.add(Memory(id="read-foreign-memory", content="foreign", summary="foreign", source_session_id="foreign-root"))
        db.add(GoogleServiceConnection(connection_id="read-connection", owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id, vault_secret_key="unread-vault-reference", state="ready", revision=3))
    requests = [
        ("/api/capabilities/native-read", {"method": "capabilities.list", "limit": 2, "idempotency_key": "list-1"}, "capabilities.list"),
        ("/api/capabilities/native-read", {"method": "capabilities.describe", "capability_id": "native_tool:read_file", "idempotency_key": "describe-1"}, "capabilities.describe"),
        ("/api/calendar/connections/read-connection/native-read", {"expected_connection_revision": 3, "idempotency_key": "connection-1"}, "connections.inspect"),
        ("/api/memory/records/native-read", {"query": "café", "limit": 2, "idempotency_key": "memory-1"}, "memory.retrieve"),
    ]
    for path, body, method in requests:
        first_call = len(calls)
        response = await client.post(path, json=body)
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["job"]["status"] == "succeeded", value
        assert value["result"]["status"] == "succeeded"
        assert value["result"]["memory_status"] == "no_learning"
        assert [item["method"] for item in calls[first_call:]] == [method,
            "artifacts.stage", "artifacts.adopt", "artifacts.read", "audit.append"]
        assert json.loads(value["artifact_readback"]["value"]["content"]) == value["result"]
        assert hashlib.sha256(value["artifact_readback"]["value"]["content"].encode()).hexdigest() == value["artifact_readback"]["value"]["digest"]
        assert len(value["job"]["artifacts"]) == 1
        row = await repo.get_job(value["job"]["job_id"])
        assert row["attempt_count"] == 1 and row["max_attempts"] == 1
        assert "café" not in json.dumps(row, ensure_ascii=False)
        async with get_session() as db:
            run = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == row["job_id"]))).scalar_one()
            from src.runtime_plugins.read_journal import read_context
            context = read_context(run)
            assert len(context["operations"]) == 4
            assert all(slot["state"] == "settled" for slot in context["operations"])
            from src.workspace import canonical_workspace_root
            physical = canonical_workspace_root(settings.workspace_dir) / row["artifacts"][0]["file_path"]
            assert physical.read_bytes() == value["artifact_readback"]["value"]["content"].encode()
            assert physical.stat().st_mode & 0o777 == 0o600 and physical.stat().st_nlink == 1
            assert all(slot["artifact_record"]["trust"]["state"] == "governed"
                and slot["artifact_record"]["trust"]["egress_class"] == "local_only"
                and slot["artifact_record"]["run_id"] == row["job_id"] for slot in context["operations"][:3])
            event = await db.get(AuditEvent, context["operations"][3]["audit_event_ref"])
            assert event.session_id == operator.session_id and event.event_type == "runtime_service_observed"
            assert context["operations"][3]["result"]["value"]["revision"] == context["operations"][3]["revision"]
        if method == "memory.retrieve":
            records = value["result"]["value"]["records"]
            assert records == [{"record_ref": "read-owner-memory", "text": "[redacted secret] café summary",
                "text_digest": hashlib.sha256("[redacted secret] café summary".encode()).hexdigest()}]
            assert "known-private-secret" not in response.text
            assert calls[first_call]["payload"] == {"query_ref": value["job"]["job_id"], "limit": 2}
            assert "café" not in json.dumps(calls, ensure_ascii=False)
        count = len(calls)
        retried = await client.post(path, json=body)
        assert retried.status_code == 200, retried.text
        assert retried.json()["job"]["job_id"] == value["job"]["job_id"]
        assert retried.json()["replayed"] is True
        assert "result" not in retried.json()
        assert len(calls) == count
    assert len(calls) == 20
    async with get_session() as db:
        assert len((await db.execute(select(AuditEvent))).scalars().all()) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("path,body", [
    ("/api/capabilities/native-read", {"method": "capabilities.describe", "capability_id": "native_tool:not-bundled", "idempotency_key": "bad"}),
    ("/api/capabilities/native-read", {"method": "capabilities.list", "cursor": "native_tool:wrong", "idempotency_key": "bad"}),
    ("/api/calendar/connections/missing/native-read", {"expected_connection_revision": 1, "idempotency_key": "bad"}),
    ("/api/memory/records/native-read", {"query": "é" * 101, "idempotency_key": "bad"}),
])
async def test_bad_original_inputs_never_admit_or_contact_native_child(actual_reads, path, body):
    client, _, _, _, calls = actual_reads
    response = await client.post(path, json=body)
    assert response.status_code in {409, 422}, response.text
    assert calls == []
    async with get_session() as db:
        assert list((await db.execute(select(WorkflowRunState))).scalars()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method,changed_payload", [
    ("capabilities.list", {"cursor": None, "limit": 1}),
    ("capabilities.describe", {"capability_id": "native_tool:write_file"}),
    ("connections.inspect", {"connection_ref": "other-owned-connection"}),
    ("memory.retrieve", {"query_ref": "other-read-job", "limit": 2}),
])
async def test_actual_stock_same_method_frame_cannot_change_private_original_candidate(actual_reads, monkeypatch, method, changed_payload):
    client, operator, repo, host, calls = actual_reads
    if method == "connections.inspect":
        async with get_session() as db:
            await begin_native_writer(db, owner="native_ingress")
            for connection in ("original-connection", "other-owned-connection"):
                db.add(GoogleServiceConnection(connection_id=connection, owner_principal_id=operator.principal.principal_id,
                    owner_session_id=operator.session_id, vault_secret_key="private-" + connection,
                    setup_idempotency_key=connection, state="ready", revision=1))
    original = host.request_service
    async def changed(name, payload, **kwargs):
        assert name == method
        return await original(name, changed_payload, **kwargs)
    monkeypatch.setattr(host, "request_service", changed)
    if method.startswith("capabilities."):
        path = "/api/capabilities/native-read"
        body = {"method": method, "idempotency_key": "changed"}
        body.update({"limit": 2} if method.endswith("list") else {"capability_id": "native_tool:read_file"})
    elif method == "memory.retrieve":
        path, body = "/api/memory/records/native-read", {"query": "private", "limit": 2, "idempotency_key": "changed"}
    else:
        path, body = "/api/calendar/connections/original-connection/native-read", {"expected_connection_revision": 1, "idempotency_key": "changed"}
    response = await client.post(path, json=body)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "native_read_original_inputs_changed"
    assert len(calls) == 1 and calls[0]["method"] == method
    from src.runtime_plugins.read_journal import read_context
    async with get_session() as db:
        rows = list((await db.execute(select(WorkflowRunState))).scalars())
        assert len(rows) == 1 and rows[0].status == "blocked"
        assert read_context(rows[0])["result"] is None


@pytest.mark.asyncio
async def test_memory_exact_retry_candidate_drift_and_unauthenticated_admission_deny(actual_reads):
    client, _, _, _, calls = actual_reads
    path = "/api/memory/records/native-read"
    body = {"query": "original private query", "limit": 2, "idempotency_key": "original"}
    first = await client.post(path, json=body)
    assert first.status_code == 200, first.text
    changed = await client.post(path, json={**body, "query": "other private query"})
    assert changed.status_code == 409, changed.text
    assert changed.json()["detail"]["code"] == "native_read_idempotency_candidate_changed"
    assert len(calls) == 5
    client.cookies.clear()
    denied = await client.post(path, json={**body, "idempotency_key": "unauthenticated"})
    assert denied.status_code == 401
    assert len(calls) == 5


@pytest.mark.asyncio
async def test_actual_revocation_after_admission_blocks_before_private_projection(actual_reads, monkeypatch):
    from datetime import datetime, timezone
    from src.db.models import OperatorSession
    from src.runtime_plugins.read_journal import read_context
    client, operator, _, host, calls = actual_reads
    original = host.service_dispatch.dispatch
    async def revoke_before_read(frame, **kwargs):
        async with get_session() as db:
            await begin_native_writer(db, owner="native_ingress")
            root = await db.get(OperatorSession, operator.session_id)
            root.revoked_at = datetime.now(timezone.utc)
        return await original(frame, **kwargs)
    monkeypatch.setattr(host.service_dispatch, "dispatch", revoke_before_read)
    response = await client.post("/api/memory/records/native-read", json={"query": "private", "idempotency_key": "revoked"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "native_original_root_inactive"
    assert len(calls) == 1
    async with get_session() as db:
        run = (await db.execute(select(WorkflowRunState))).scalar_one()
        assert run.status != "succeeded"
        assert read_context(run)["result"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary,mutation", [
    ("artifacts.adopt", "bytes"), ("artifacts.read", "symlink"),
    ("audit.append", "bytes"), ("completion", "bytes"),
])
async def test_real_private_artifact_tamper_denies_success_and_exact_retry(actual_reads, monkeypatch, boundary, mutation):
    from src.workspace import canonical_workspace_root
    client, _, repo, host, calls = actual_reads
    original = host.request_service
    changed = False
    async def mutate_at_boundary(method, inputs, **kwargs):
        nonlocal changed
        if method == boundary or (boundary == "completion" and method == "audit.append"):
            if boundary == "completion":
                result = await original(method, inputs, **kwargs)
            job = await repo.get_job(kwargs["original_scope"].witness["invocation_ref"])
            path = canonical_workspace_root(settings.workspace_dir) / job["artifacts"][0]["file_path"]
            if mutation == "symlink":
                target = path.with_suffix(".foreign")
                target.write_bytes(path.read_bytes())
                target.chmod(0o600)
                path.unlink()
                path.symlink_to(target)
            else:
                path.write_bytes(b'{"changed":true}')
            changed = True
            if boundary == "completion":
                return result
        return await original(method, inputs, **kwargs)
    monkeypatch.setattr(host, "request_service", mutate_at_boundary)
    body = {"method": "capabilities.list", "limit": 1, "idempotency_key": "tamper"}
    response = await client.post("/api/capabilities/native-read", json=body)
    assert response.status_code == 409, response.text
    assert changed
    count = len(calls)
    retry = await client.post("/api/capabilities/native-read", json=body)
    assert retry.status_code == 409
    assert "result" not in retry.json() and "artifact_readback" not in retry.json()
    assert len(calls) == count


@pytest.mark.asyncio
async def test_physical_write_then_interruption_keeps_original_intent_and_never_replays(actual_reads, monkeypatch):
    from src.runtime_plugins.read_journal import read_context
    client, _, _, _, calls = actual_reads
    # The existing private primitive actually writes before the simulated failure.
    from src.work_board import input_artifacts
    write = input_artifacts._write_payload
    def interrupted(path, content):
        write(path, content)
        assert path.read_bytes() == content
        raise OSError("fixture interruption after physical publication")
    monkeypatch.setattr(input_artifacts, "_write_payload", interrupted)
    body = {"method": "capabilities.list", "limit": 1, "idempotency_key": "physical-unknown"}
    response = await client.post("/api/capabilities/native-read", json=body)
    assert response.status_code == 409
    async with get_session() as db:
        run = (await db.execute(select(WorkflowRunState))).scalar_one()
        context = read_context(run)
        assert run.status != "succeeded"
        assert context["operations"][0]["state"] == "intent"
        assert context["operations"][0]["result"] is None
        effects = json.loads(run.effect_receipts_json)
        assert effects[0]["status"] == "intent"
    count = len(calls)
    retry = await client.post("/api/capabilities/native-read", json=body)
    assert retry.status_code == 200 and retry.json()["job"]["status"] != "succeeded"
    assert len(calls) == count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,changed_inputs", [
    ("artifacts.stage", {"request_ref": "foreign-stage"}),
    ("artifacts.adopt", {"request_ref": "foreign-adopt"}),
    ("artifacts.read", {"artifact_ref": "foreign-artifact", "max_bytes": 65536}),
    ("audit.append", {"event_ref": "foreign-event"}),
])
async def test_stock_operation_wire_refs_never_substitute_private_candidate(actual_reads, monkeypatch, operation, changed_inputs):
    from src.runtime_plugins.read_journal import read_context
    client, _, _, host, _ = actual_reads
    original = host.request_service
    async def substitute(method, inputs, **kwargs):
        return await original(method, changed_inputs if method == operation else inputs, **kwargs)
    monkeypatch.setattr(host, "request_service", substitute)
    response = await client.post("/api/capabilities/native-read", json={"method": "capabilities.list", "idempotency_key": "wire-drift"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "native_read_artifact_wire_changed"
    async with get_session() as db:
        run = (await db.execute(select(WorkflowRunState))).scalar_one()
        slot = read_context(run)["operations"][-1]
        assert slot["method"] == operation and slot["state"] == "prepared"
        assert run.status != "succeeded"
        assert not (await db.execute(select(AuditEvent))).scalars().all()


@pytest.mark.asyncio
async def test_revoked_original_root_after_stage_denies_adoption(actual_reads, monkeypatch):
    from datetime import datetime, timezone
    from src.db.models import OperatorSession
    client, operator, _, host, calls = actual_reads
    original = host.request_service
    async def revoke(method, inputs, **kwargs):
        if method == "artifacts.adopt":
            async with get_session() as db:
                await begin_native_writer(db, owner="native_ingress")
                root = await db.get(OperatorSession, operator.session_id)
                root.revoked_at = datetime.now(timezone.utc)
        return await original(method, inputs, **kwargs)
    monkeypatch.setattr(host, "request_service", revoke)
    response = await client.post("/api/capabilities/native-read", json={"method": "capabilities.list", "idempotency_key": "revoke-after-stage"})
    assert response.status_code == 409, response.text
    assert calls[-1]["method"] == "artifacts.adopt"
    async with get_session() as db:
        run = (await db.execute(select(WorkflowRunState))).scalar_one()
        assert run.status != "succeeded"
        assert not (await db.execute(select(AuditEvent))).scalars().all()


@pytest.mark.asyncio
async def test_lost_stage_ack_cannot_replay_or_promote_old_success(actual_reads, monkeypatch):
    from src.runtime_plugins.bridge import HostBlocked
    from src.runtime_plugins.read_journal import read_context
    client, _, _, host, calls = actual_reads
    original = host.request_service
    async def lose_ack(method, inputs, **kwargs):
        result = await original(method, inputs, **kwargs)
        if method == "artifacts.stage":
            assert result["status"] == "succeeded"
            raise HostBlocked("fixture_lost_stage_ack")
        return result
    monkeypatch.setattr(host, "request_service", lose_ack)
    body = {"method": "capabilities.list", "idempotency_key": "lost-stage-ack"}
    response = await client.post("/api/capabilities/native-read", json=body)
    assert response.status_code == 409
    async with get_session() as db:
        run = (await db.execute(select(WorkflowRunState))).scalar_one()
        context = read_context(run)
        assert run.status != "succeeded"
        assert len(context["operations"]) == 1 and context["operations"][0]["state"] == "settled"
    count = len(calls)
    retry = await client.post("/api/capabilities/native-read", json=body)
    assert retry.status_code == 200 and retry.json()["replayed"] is True
    assert retry.json()["job"]["status"] != "succeeded" and "result" not in retry.json()
    assert len(calls) == count == 2


@pytest.mark.asyncio
async def test_actual_stage_call_expiry_never_acquires_fresh_window(actual_reads, monkeypatch):
    import asyncio
    from src.runtime_plugins.read_journal import read_context
    client, _, _, host, calls = actual_reads
    original = host.service_dispatch.dispatch
    async def expire(frame, **kwargs):
        if frame["method"] == "artifacts.stage":
            await asyncio.sleep(5.2)
        return await original(frame, **kwargs)
    monkeypatch.setattr(host.service_dispatch, "dispatch", expire)
    response = await client.post("/api/capabilities/native-read", json={"method": "capabilities.list", "idempotency_key": "expired-call"})
    assert response.status_code == 409, response.text
    async with get_session() as db:
        run = (await db.execute(select(WorkflowRunState))).scalar_one()
        assert run.status != "succeeded" and run.attempt_count == 1
        context = read_context(run)
        assert context["operations"][0]["state"] == "prepared"
        assert json.loads(run.artifact_receipts_json) == []
    assert len(calls) == 1  # the expired handler never reaches the observed owner
