"""Genuine forget dispatch regression; Source-only until the test release."""

import httpx
import pytest
from fastapi import FastAPI

from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth
from tests.test_native_memory_report_source_vertical import NODE


@pytest.mark.asyncio
async def test_genuine_forget_dispatch_preserves_absent_report_capture(
        accounting_db, real_auth, monkeypatch):
    import src.db.engine as original
    from config.settings import settings
    from src.db.engine import get_session, override_session_factory
    from src.auth.service import create_session
    from src.auth.middleware import OperatorAuthMiddleware
    from src.agent.session import SessionManager
    from src.memory import m5
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
    actual_prepare = m5.prepare_native_memory_mutation
    observed = []

    class OriginalPreparationObserved(RuntimeError):
        pass

    async def observe_original_preparation(db, **kwargs):
        admission = kwargs["admission"]
        assert admission.report_source is None
        assert kwargs["native_memory_report_source"] is None
        assert kwargs["native_memory_source"] is None
        # The genuine dispatcher and its original Source/authority validators
        # have already run. Execute the original M5 owner/preparation too;
        # stop before the later operation, reservation or any owner effect.
        plan = await actual_prepare(db, **kwargs)
        assert not plan.consumed
        observed.append(plan)
        raise OriginalPreparationObserved("original forget preparation observed")

    monkeypatch.setattr(m5, "prepare_native_memory_mutation", observe_original_preparation)
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(router, prefix="/api")
    with override_session_factory(factory):
        token, operator = await create_session()
        await SessionManager().get_or_create(operator.session_id,
            owner_principal_id=operator.principal.principal_id)
        memory = await memory_repository.create_memory(content="Original dispatch forget record",
            source_session_id=operator.session_id, summary="Original summary")
        workspace = ProductionWorkspace(host_root=root)
        with maintenance_fence(workspace):
            async with get_session() as db:
                await begin_native_writer(db, owner="composition_maintenance", fresh=True)
                await initialize_fresh_deployment(db,
                    composition_digests={domain: reviewed.composition_digest for domain in DOMAINS})
        try:
            assert await host.start(), host.snapshot()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                    base_url="http://localhost", cookies={settings.operator_auth_cookie_name: token},
                    headers={"Origin": "http://localhost:3001"}) as client:
                await client.post(f"/api/memory/records/{memory.memory_id}/native-forget",
                    json={"mode": "archive", "privacy_boundary": "private",
                        "idempotency_key": "original-forget-capture-regression"})
            assert len(observed) == 1
            assert not observed[0].consumed
            current = await memory_repository.get_memory(memory.memory_id)
            assert current.content == "Original dispatch forget record"
        finally:
            await host.stop(preserve_blocked=host.state == "blocked")
            if host._cleanup_task is not None:
                await host._cleanup_task
