"""Genuine forget persistence/readback; separately reviewed managed epoch only."""
import json
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth
from tests.test_native_memory_report_source_vertical import NODE

@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["archive", "redact"])
async def test_genuine_native_forget_persists_effect_and_receipts(
        accounting_db, real_auth, monkeypatch, mode):
    import src.db.engine as original
    from config.settings import settings
    from src.db.engine import get_session, override_session_factory
    from src.auth.service import create_session
    from src.auth.middleware import OperatorAuthMiddleware
    from src.agent.session import SessionManager
    from src.memory.repository import memory_repository
    from src.runtime_plugins.bridge import CordisHost
    from src.runtime_plugins.dispatch import NativeServiceDispatcher
    from src.runtime_plugins.composition import reviewed_composition
    from src.runtime_plugins.ownership import DOMAINS, begin_native_writer, initialize_fresh_deployment
    from src.workflows.job_runtime import DurableJobRepository
    from src.workspace.production import ProductionWorkspace, maintenance_fence
    from src.api.memory import router

    root, engine, factory = accounting_db
    monkeypatch.setattr(original, "engine", engine)
    monkeypatch.setattr(original, "_db_path", str(root / "seraph.db"))
    monkeypatch.setattr(original, "async_session_factory", factory)
    for target in ("src.workflows.job_runtime.get_session", "src.workflows.durable_state.get_session",
            "src.auth.service.get_session", "src.memory.repository.get_session",
            "src.audit.repository.get_session", "src.vault.repository.get_session",
            "src.approval.repository.get_session", "src.agent.session.get_session"):
        monkeypatch.setattr(target, get_session)
    await original.init_db()
    repository = DurableJobRepository()
    host = CordisHost(node_path=NODE, service_dispatch=NativeServiceDispatcher(jobs=repository))
    monkeypatch.setattr("src.runtime_plugins.bridge.cordis_host", host)
    reviewed = reviewed_composition(node_path=NODE)
    from src.db.models import Memory, AuditEvent, WorkflowRunState
    from src.workspace.accounting_witness import MEMORY_ORIGINAL_CHECKPOINT, MEMORY_CURRENT_CHECKPOINT
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(router, prefix="/api")
    with override_session_factory(factory):
        token, operator = await create_session()
        await SessionManager().get_or_create(operator.session_id,
            owner_principal_id=operator.principal.principal_id)
        memory = await memory_repository.create_memory(content="Original persisted forget record",
            source_session_id=operator.session_id, summary="Original persisted summary")
        workspace = ProductionWorkspace(host_root=root)
        with maintenance_fence(workspace):
            async with get_session() as db:
                await begin_native_writer(db, owner="composition_maintenance", fresh=True)
                await initialize_fresh_deployment(db,
                    composition_digests={domain: reviewed.composition_digest for domain in DOMAINS})
        body = {"mode": mode, "privacy_boundary": "private",
            "idempotency_key": "original-forget-persistence-" + mode}
        url = f"/api/memory/records/{memory.memory_id}/native-forget"
        try:
            assert await host.start(), host.snapshot()
            # The original HTTP caller guard must reject absence of a bearer;
            # it cannot write the selected record or fabricate a Memory Source.
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                    base_url="http://localhost", headers={"Origin": "http://localhost:3001"}) as unauthenticated:
                denied = await unauthenticated.post(url, json=body)
                assert denied.status_code in (401, 403), denied.text
            unchanged = await memory_repository.get_memory(memory.memory_id)
            assert unchanged.content == "Original persisted forget record"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                    base_url="http://localhost", cookies={settings.operator_auth_cookie_name: token},
                    headers={"Origin": "http://localhost:3001"}) as client:
                response = await client.post(url, json=body)
                assert response.status_code == 200, response.text
                completed = response.json()
                assert completed["replayed"] is False
                assert completed["job"]["status"] == "succeeded"
                assert completed["job"]["attempt_count"] == completed["job"]["max_attempts"] == 1
                result = completed["result"]
                assert result["memory_status"] == "forgotten"
                assert result["value"]["record_ref"] == memory.memory_id
                assert result["value"]["receipt_ref"].startswith("native-memory-receipt:")
                # Independent original DB read follows the committed effect and
                # completion. No private factory/ledger/observer is replaced.
                async with get_session() as db:
                    stored = await db.get(Memory, memory.memory_id)
                    assert stored is not None and stored.status.value == "archived"
                    assert stored.source_session_id == operator.session_id
                    assert stored.content == ("[forgotten by operator]" if mode == "redact"
                        else "Original persisted forget record")
                    assert stored.summary == ("[forgotten by operator]" if mode == "redact"
                        else "Original persisted summary")
                    run = (await db.execute(select(WorkflowRunState).where(
                        WorkflowRunState.run_identity == completed["job"]["job_id"]))).scalar_one()
                    journal = json.loads(run.checkpoint_receipts_json)
                    indexed = {entry["checkpoint_id"]: entry for entry in journal}
                    for checkpoint in (MEMORY_ORIGINAL_CHECKPOINT, MEMORY_CURRENT_CHECKPOINT):
                        assert indexed[checkpoint]["safe"] is True
                        assert len(indexed[checkpoint]["state_digest"]) == 64
                    context = json.loads(run.checkpoint_context_json)
                    assert context["result"]["payload"] == result
                    assert context["result"]["invocation_ref"] == run.run_identity
                    assert context["result"]["effect"]["record_id"] == memory.memory_id
                    audits = list((await db.execute(select(AuditEvent).where(
                        AuditEvent.event_type == "memory_forgotten"))).scalars())
                    assert len(audits) == 1
                    details = json.loads(audits[0].details_json)
                    assert details["memory_id"] == memory.memory_id and details["mode"] == mode
                    assert details["privacy_boundary"] == "private"
                    assert audits[0].actor == operator.principal.principal_id
                    assert audits[0].session_id == operator.session_id
                # Repeated admission returns the durable job, never a second
                # private result release or another owner effect.
                replay = await client.post(url, json=body)
                assert replay.status_code == 200, replay.text
                assert replay.json()["replayed"] is True and "result" not in replay.json()
                assert replay.json()["job"]["job_id"] == completed["job"]["job_id"]
        finally:
            await host.stop(preserve_blocked=host.state == "blocked")
            if host._cleanup_task is not None:
                await host._cleanup_task
