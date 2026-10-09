"""Actual stock host + original canonical native claim; no effect dispatcher."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event, text

from config.settings import settings
from src.auth.service import create_session
from src.db.engine import get_session
from src.db.models import OperatorSession
from src.runtime_plugins.bridge import CordisHost, HostBlocked
from src.runtime_plugins.dispatch import NativeServiceDispatcher, capture_original_scope, NativeServiceBlocked
from src.runtime_plugins.ownership import bind_invocation
from src.workflows.job_runtime import DurableJobRepository
from tests.test_durable_job_runtime import _spec
from tests.test_runtime_composition_ownership import composition_db


@pytest_asyncio.fixture
async def foreign_key_composition_db(composition_db):
    engine = composition_db[1]
    def enforce_foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
    event.listen(engine.sync_engine, "connect", enforce_foreign_keys)
    await engine.dispose()
    try:
        async with get_session() as db:
            assert await db.scalar(text("PRAGMA foreign_keys")) == 1
        yield composition_db
    finally:
        event.remove(engine.sync_engine, "connect", enforce_foreign_keys)


@pytest.mark.asyncio
async def test_actual_host_original_native_claim_read_and_restart_rejects_old_scope(foreign_key_composition_db, monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "isolated-actual-service-claim")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    _, operator = await create_session()
    repo = DurableJobRepository()
    host = CordisHost(node_path=Path(os.environ["SERAPH_CORDIS_TEST_NODE"]),
        service_dispatch=NativeServiceDispatcher(jobs=repo))
    try:
        assert await host.start(), host.snapshot()
        async with get_session() as db:
            binding = await bind_invocation(db, method="tasks.admit", native_branch="workflow",
                reviewed_composition=host.reviewed)
        original = _spec(job_id="actual-native-service-read", dedupe_key="actual-native-service-read")
        spec = replace(original, identity=replace(original.identity, job_kind="workflow", owner_kind="user",
            owner_principal_id=operator.principal.principal_id), service_id=None, session_id=operator.session_id,
            operator_session_id=operator.session_id, composition_binding=binding,
            declared_authority={"principal": operator.principal.principal_id, "owner_kind": "user", "session_id": operator.session_id},
            deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5))
        await repo.admit_job(spec)
        await repo.transition_job(spec.identity.job_id, "queued")
        claim = await repo.claim_service_job(spec.identity.job_id, host=host, owner="actual-native-reader")
        scope = capture_original_scope(claim, host)
        result = await host.request_service("tasks.inspect", {"job_ref": spec.identity.job_id}, original_scope=scope)
        assert result["status"] == "succeeded", result
        assert result["value"]["job_ref"] == spec.identity.job_id
        assert result["value"]["state"] == "running"
        assert result["memory_status"] == "no_learning"
        assert scope.binding.origin_method == "tasks.admit"
        assert scope.binding.epoch_for("tasks.inspect") > 0
        assert (await repo.get_job(spec.identity.job_id))["effects"] == []
        denied = await host.request_service("tasks.inspect", {"job_ref": "other-owned-or-private-job"}, original_scope=scope)
        assert denied["status"] == "blocked"
        assert denied["reason_code"] == "native_job_reference_not_bound"
        async with get_session() as db:
            root = await db.get(OperatorSession, operator.session_id)
            root.revoked_at = datetime.now(timezone.utc)
        denied = await host.request_service("tasks.inspect", {"job_ref": spec.identity.job_id}, original_scope=scope)
        assert denied["status"] == "blocked"
        assert denied["reason_code"] == "native_original_root_inactive"
        assert (await repo.get_job(spec.identity.job_id))["effects"] == []
        boot = scope.host_boot_nonce
        await host.stop()
        assert host.snapshot()["cleanup"]["process_reaped"] is True
        assert await host.start(), host.snapshot()
        assert host.boot_nonce != boot
        before = host._out_seq
        with pytest.raises(HostBlocked, match="native_original_host_changed"):
            await host.request_service("tasks.inspect", {"job_ref": spec.identity.job_id}, original_scope=scope)
        with pytest.raises(NativeServiceBlocked, match="native_original_host_unavailable"):
            capture_original_scope(claim, host)
        assert host._out_seq == before
        assert (await repo.get_job(spec.identity.job_id))["effects"] == []
    finally:
        await host.stop(preserve_blocked=host.state == "blocked")
        if host._cleanup_task is not None:
            await host._cleanup_task
        assert host.snapshot()["cleanup"]["process_reaped"] is True


@pytest.mark.asyncio
async def test_actual_plain_turn_same_writer_ingress_claim_and_native_observers(foreign_key_composition_db, monkeypatch):
    import httpx
    from src.agent.direct_chat import run_direct_local_chat
    from src.agent.session import SessionManager
    from src.agent.turn_execution import NativeTurnAdmission, claim_native_turn
    from src.agent.native_turn_controls import NativeTurnResourceOwner
    from src.api.chat import (_bind_chat_principal, build_chat_ingress_envelope, chat_ingress_metadata,
        assistant_message_id_for_ingress, chat_assistant_metadata)
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "isolated-actual-turn-observer")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    token, operator = await create_session()
    from src.workflows.job_runtime import durable_job_repository as repo
    host = CordisHost(node_path=Path(os.environ["SERAPH_CORDIS_TEST_NODE"]),
        service_dispatch=NativeServiceDispatcher(jobs=repo))
    monkeypatch.setattr("src.runtime_plugins.bridge.cordis_host", host)
    monkeypatch.setattr(settings, "openrouter_api_key", "intercepted-test-key")
    monkeypatch.setattr(settings, "openrouter_allowed_upstreams", "openai")
    monkeypatch.setattr(settings, "default_model", "openrouter/openai/gpt-4o-mini")
    resources = NativeTurnResourceOwner()
    callbacks = []
    original_post = httpx.AsyncClient.post
    async def scripted_route_probe(client, url, **kwargs):
        if not str(url).startswith("https://openrouter.ai/"):
            return await original_post(client, url, **kwargs)
        assert str(url) == "https://openrouter.ai/api/v1/chat/completions"
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "id": "owned-plain-turn-route-probe", "choices": [{"message": {"role": "assistant", "content": "CANARY_OK"}}],
            "usage": {"cost": "0", "prompt_tokens": 1, "completion_tokens": 1}})
    def scripted_completion_transport(client, url, **kwargs):
        assert str(url) == "https://openrouter.ai/api/v1/chat/completions"
        callbacks.append(ingress.message_id)
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "id": "owned-plain-turn-completion", "choices": [{"message": {"role": "assistant",
                "content": "Controlled native callback output; no inference."}}],
            "usage": {"cost": "0", "prompt_tokens": 1, "completion_tokens": 1}})
    monkeypatch.setattr(httpx.AsyncClient, "post", scripted_route_probe)
    monkeypatch.setattr(httpx.Client, "post", scripted_completion_transport)
    try:
        assert await host.start(), host.snapshot()
        from src.app import create_app
        # Use the original settings API and readiness owner; only final HTTP is scripted.
        from httpx import ASGITransport, AsyncClient
        async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
            cookies={settings.operator_auth_cookie_name: token}, headers={"origin": "http://127.0.0.1:3001"}) as client:
            configured = await client.put("/api/settings/model-fabric", json={"openrouter_setup": {
                "model_ids": ["openai/gpt-4o-mini"], "capabilities": ["text"], "allowed_upstreams": ["openai"],
                "data_collection": "deny", "data_retention_policy": "deny", "egress_class": "cloud_allowed_full",
                "cloud_egress_acknowledged": True, "spend_ceiling_microusd": 1000,
                "request_cost_bound_microusd": 100, "credential_ref": "env:OPENROUTER_API_KEY"}})
            assert configured.status_code == 200, configured.text
            for capability in ("text", "health", "latency_ms"):
                verified = await client.post("/api/settings/model-fabric/canary",
                    json={"profile_id": "openrouter", "capability": capability, "timeout_seconds": 120})
                assert verified.status_code == 200 and verified.json()["outcome"] == "passed", verified.text
        manager = SessionManager()
        conversation = await manager.get_for_ingress(None, owner_principal_id=operator.principal.principal_id)
        principal = _bind_chat_principal(conversation.id, operator=operator)
        text = "Controlled original plain turn; no provider execution."
        ingress = build_chat_ingress_envelope(message=text, session_id=conversation.id,
            principal=principal, operator_session_id=operator.session_id, transport="rest",
            client_message_id="actual-stock-host-native-turn")
        admission = NativeTurnAdmission.capture(ingress, principal=principal,
            reviewed_composition=host.reviewed, native_route="direct_turn")
        resource = resources.reserve(admission)
        message, duplicate, job = await manager.reserve_native_turn_message(conversation.id, text,
            message_id=ingress.message_id, metadata_json=chat_ingress_metadata(ingress), admission=admission)
        assert not duplicate and message.id == ingress.message_id
        execution = await claim_native_turn(admission, host, job, resource=resource)
        claim, scope = execution.claim, execution.scope
        accepted = await host.request_service("conversation.accept", {"turn_ref": job["job_id"]}, original_scope=scope)
        assert accepted["status"] == "succeeded", accepted
        assert accepted["value"] == {"turn_ref": job["job_id"], "job_ref": job["job_id"], "replayed": False}
        started = await host.request_service("agent-loop.startTurn", {"turn_ref": job["job_id"]}, original_scope=scope)
        assert started["status"] == "succeeded", started
        assert started["value"]["state"] == "running"
        denied = await host.request_service("conversation.accept", {"turn_ref": "other-turn"}, original_scope=scope)
        assert denied["status"] == "blocked" and denied["reason_code"] == "native_turn_reference_not_bound"
        assert (await repo.get_job(job["job_id"]))["effects"] == []
        from sqlalchemy import func, select
        from src.db.models import Message
        async with get_session() as db:
            assert await db.scalar(select(func.count()).select_from(Message)) == 1
        # Original direct owner, broker and output seal run with scripted HTTP;
        # this proves mechanics, not provider availability or inference quality.
        from src.approval.runtime import get_current_approval_mode, set_runtime_context, reset_runtime_context
        tokens = set_runtime_context(conversation.id, get_current_approval_mode(), trust_principal=principal)
        try:
            response = await execution.execute(run_direct_local_chat(text, runtime_path="chat_agent",
                is_onboarding=False, session_id=conversation.id, request_id=ingress.message_id))
        finally:
            reset_runtime_context(tokens)
        assert execution.worker.done() and not execution.worker.cancelled()
        output_id = assistant_message_id_for_ingress(ingress)
        output = await manager.add_native_turn_result(conversation.id, response, message_id=output_id,
            metadata_json=chat_assistant_metadata(ingress, message_id=output_id), execution=execution)
        await execution.observe_resource()
        assert callbacks == [ingress.message_id] and output.id == output_id
        settled = await repo.get_job(job["job_id"])
        assert settled["status"] == "succeeded" and settled["lease"]["owner"] is None
        from src.db.models import WorkflowRunState, InferenceCostReservation
        async with get_session() as db:
            run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == job["job_id"]))
            family = next(item["payload"] for item in json.loads(run.checkpoint_receipts_json)
                if item["checkpoint_id"] == "conversation:operation-family")
            assert len(family["operations"]) == 1
            operation = family["operations"][0]
            owner = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == operation["job_id"]))
            reservation = await db.get(InferenceCostReservation, operation["operation_id"])
            assert owner.status == "succeeded" and owner.lease_owner is None
            assert reservation.state == "settled" and reservation.actual_cost_microusd == 0
            outputs = [item for item in json.loads(owner.checkpoint_receipts_json)
                if item["checkpoint_id"] == "inference:owned-output.v1"]
            assert len(outputs) == 1 and outputs[0]["safe"] is True
            payload = outputs[0]["payload"]
            import hashlib
            retained_output = (Path(settings.workspace_dir) / payload["file_ref"]).read_bytes()
            assert hashlib.sha256(retained_output).hexdigest() == payload["content_sha256"]
            assert len(retained_output) == payload["size_bytes"]
            assert response.encode() in retained_output
        appended = await host.request_service("conversation.append", {"message_ref": output.id}, original_scope=scope)
        assert appended["status"] == "succeeded", appended
        assert appended["value"]["message_ref"] == output.id
        assert appended["memory_status"] == "no_learning"
        denied = await host.request_service("conversation.append", {"message_ref": ingress.message_id}, original_scope=scope)
        assert denied["status"] == "blocked" and denied["reason_code"] == "native_turn_output_reference_not_bound"
        # Terminal output proof cannot reopen the generic running-job observer.
        denied = await host.request_service("agent-loop.startTurn", {"turn_ref": job["job_id"]}, original_scope=scope)
        assert denied["status"] == "blocked"
        async with get_session() as db:
            assert await db.scalar(select(func.count()).select_from(Message)) == 2
        assert (await repo.get_job(job["job_id"]))["effects"] == []
    finally:
        await resources.shutdown()
        await host.stop(preserve_blocked=host.state == "blocked")
        if host._cleanup_task is not None:
            await host._cleanup_task
        assert host.snapshot()["cleanup"]["process_reaped"] is True
