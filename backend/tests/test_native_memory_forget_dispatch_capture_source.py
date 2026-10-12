"""Genuine forget dispatch regression; Source-only until the test release."""

import httpx
import pytest
from fastapi import FastAPI

from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth
from tests.test_native_memory_report_source_vertical import NODE


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_boundary", ["preparation", "complete_numeric_owner"])
async def test_genuine_forget_dispatch_preserves_absent_report_capture(
        accounting_db, real_auth, monkeypatch, stop_boundary):
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
    owner_outputs = []

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
        source = kwargs["native_memory_owner_numeric_source"]
        assert source.admission is admission and source.report_source is None
        assert source._original_owner_numeric_closed is True
        lineage = source._original_owner_numeric_lineage
        assert lineage[0] is source and lineage[1] is admission
        assert lineage[2] is db and lineage[6] is admission.header_budget
        assert lineage[7] == "generic-owner"
        assert source._original_owner_numeric_branches[0] == "memory.forget"
        assert any(pair[0] == ("native-memory-forget-marker", "json-parser") for pair in lineage[8])
        # Copied or child-task resource custody must fail before another
        # candidate parse, header read, marker parse or numerical debit.
        from copy import copy
        import asyncio
        from src.memory.header_bounds import HeaderBoundsError
        from src.runtime_plugins.memory_producer import validate_original_memory_owner
        remaining = admission.header_budget.remaining
        with pytest.raises(HeaderBoundsError):
            await validate_original_memory_owner(db, admission, _resource_source=copy(source))
        async def child():
            with pytest.raises(HeaderBoundsError):
                await validate_original_memory_owner(db, admission, _resource_source=source)
        await asyncio.create_task(child())
        assert admission.header_budget.remaining == remaining
        observed.append(plan)
        if stop_boundary == "complete_numeric_owner":
            return plan
        raise OriginalPreparationObserved("original forget preparation observed")

    monkeypatch.setattr(m5, "prepare_native_memory_mutation", observe_original_preparation)
    if stop_boundary == "complete_numeric_owner":
        from src.workspace import accounting_witness
        from src.runtime_plugins import memory_producer
        from src.memory.header_bounds import HeaderBoundsError
        async def observe_complete_owner(db, run, operation, prepared, *, existing_references,
                before_row, certificate):
            guard = db.info["composition_guard"]
            facts = guard._native_memory_source_capacity_facts[3]
            widths = guard._native_memory_retention_numeric_facts[3]
            kwargs = dict(certificate=certificate, facts=facts, retention_widths=widths)
            helper = memory_producer.original_native_memory_source_authority_numeric_schedule
            before = certificate.budget.remaining
            capacity = await helper(operation, prepared, **kwargs, _construction_only=True)
            assert type(capacity) is int and capacity > 0 and certificate.budget.remaining == before
            certificate.budget.debit(capacity, appearance=("test-forget-owner", "constructor"))
            vector = await helper(operation, prepared, **kwargs)
            assert memory_producer._native_memory_source_authority_numeric_issuance(
                operation, prepared, vector, **kwargs) is vector
            assert len(vector) == 14 + 3 * len(operation.source._original_owner_numeric_lineage[8])
            assert vector[0][0] == ("native-memory-source-trace", "collector-construction")
            assert any(pair[0] == ("native-memory-forget-marker", "json-parser") for pair in vector)
            assert operation.source.report_source is None and not prepared.consumed
            with pytest.raises(HeaderBoundsError):
                memory_producer._native_memory_source_authority_numeric_issuance(
                    operation, prepared, tuple(list(vector)), **kwargs)
            owner_outputs.append(vector)
            raise HeaderBoundsError("test_complete_forget_owner_observed_before_effect")
        monkeypatch.setattr(accounting_witness, "_native_memory_effect_numeric_schedule", observe_complete_owner)
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
            assert len(owner_outputs) == (1 if stop_boundary == "complete_numeric_owner" else 0)
            assert not observed[0].consumed
            current = await memory_repository.get_memory(memory.memory_id)
            assert current.content == "Original dispatch forget record"
        finally:
            await host.stop(preserve_blocked=host.state == "blocked")
            if host._cleanup_task is not None:
                await host._cleanup_task
