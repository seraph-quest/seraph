"""Selected genuine REPORT authority owner; prospective, provider-free observer."""
import asyncio
from copy import copy
import pytest
import pytest_asyncio
from src.memory.header_bounds import HeaderBoundsError
from src.runtime_plugins import memory_producer
from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth
from tests.test_native_memory_genuine_numeric_prefix import _terminal_report


@pytest_asyncio.fixture
async def actual_report_memory(accounting_db, real_auth, monkeypatch):
    """Original R478 terminal owner; missing sandbox/browser fails visibly."""
    import os
    from pathlib import Path
    import httpx
    from fastapi import FastAPI
    from config.settings import settings
    from src.db import engine as original
    from src.db.engine import get_session, override_session_factory
    from src.auth.service import create_session
    from src.auth.middleware import OperatorAuthMiddleware
    from src.runtime_plugins.bridge import CordisHost
    from src.runtime_plugins.dispatch import NativeServiceDispatcher
    from src.runtime_plugins.composition import reviewed_composition
    from src.runtime_plugins.ownership import DOMAINS, begin_native_writer, initialize_fresh_deployment
    from src.workspace.production import ProductionWorkspace, maintenance_fence
    from src.api.memory import router
    root, engine, factory = accounting_db
    monkeypatch.setattr(original, "engine", engine)
    monkeypatch.setattr(original, "_db_path", str(root / "seraph.db"))
    monkeypatch.setattr(original, "async_session_factory", factory)
    for target in ("src.db.engine.get_session", "src.auth.service.get_session", "src.goals.repository.get_session",
            "src.agent.session.get_session", "src.work_board.dispatcher.get_session", "src.workflows.job_runtime.get_session",
            "src.workflows.durable_state.get_session", "src.memory.repository.get_session", "src.audit.repository.get_session",
            "src.vault.repository.get_session", "src.approval.repository.get_session"):
        monkeypatch.setattr(target, get_session)
    with override_session_factory(factory):
        await original.init_db()
        token, operator = await create_session()
        task, attempt, jobs = await _terminal_report(get_session, operator, monkeypatch, root)
        assert task.capability_id == memory_producer.REPORT_SOURCE_CAPABILITY
        monkeypatch.setattr("src.workflows.job_runtime.durable_job_repository", jobs)
        node = Path(os.environ["SERAPH_CORDIS_TEST_NODE"])
        reviewed = reviewed_composition(node_path=node)
        workspace = ProductionWorkspace(host_root=root)
        with maintenance_fence(workspace):
            async with get_session() as db:
                await begin_native_writer(db, owner="composition_maintenance", fresh=True)
                await initialize_fresh_deployment(db, composition_digests={
                    domain: reviewed.composition_digest for domain in DOMAINS})
        host = CordisHost(node_path=node, service_dispatch=NativeServiceDispatcher(jobs=jobs))
        monkeypatch.setattr("src.runtime_plugins.bridge.cordis_host", host)
        app = FastAPI()
        app.add_middleware(OperatorAuthMiddleware)
        app.include_router(router, prefix="/api")
        try:
            assert await host.start(), host.snapshot()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                    cookies={settings.operator_auth_cookie_name: token},
                    headers={"Origin": "http://localhost:3001"}) as client:
                yield client, operator, jobs, host, task, attempt
        finally:
            await host.stop(preserve_blocked=host.state == "blocked")
            if host._cleanup_task is not None:
                await host._cleanup_task

@pytest.mark.asyncio
async def test_genuine_complete_owner_keeps_custody_and_original_order(actual_report_memory, monkeypatch):
    client, operator, jobs, host, task, attempt = actual_report_memory
    from src.workspace import accounting_witness
    observations = []

    async def inspect(db, run, operation, prepared, *, existing_references, before_row, certificate):
        guard = db.info["composition_guard"]
        facts = guard._native_memory_source_capacity_facts[3]
        widths = guard._native_memory_retention_numeric_facts[3]
        helper = memory_producer.original_native_memory_source_authority_numeric_schedule
        kwargs = dict(certificate=certificate, facts=facts, retention_widths=widths)
        frame = certificate.budget
        before = frame.remaining
        for foreign in (copy(operation),):
            with pytest.raises(HeaderBoundsError):
                await helper(foreign, prepared, **kwargs, _construction_only=True)
        for foreign_kwargs in ({**kwargs, "facts": dict(facts)}, {**kwargs, "retention_widths": dict(widths)}):
            with pytest.raises(HeaderBoundsError):
                await helper(operation, prepared, **foreign_kwargs, _construction_only=True)
        async def child():
            with pytest.raises(HeaderBoundsError):
                await helper(operation, prepared, **kwargs, _construction_only=True)
        await asyncio.create_task(child())
        assert frame.remaining == before
        capacity = await helper(operation, prepared, **kwargs, _construction_only=True)
        assert type(capacity) is int and capacity > 0 and frame.remaining == before
        frame.debit(capacity, appearance=("test-complete-source-owner", "constructor"))
        vector = await helper(operation, prepared, **kwargs)
        assert type(vector) is tuple and vector
        issued = memory_producer._native_memory_source_authority_numeric_issuance(
            operation, prepared, vector, **kwargs)
        assert issued is vector
        with pytest.raises(HeaderBoundsError):
            memory_producer._native_memory_source_authority_numeric_issuance(
                operation, prepared, tuple(list(vector)), **kwargs)
        assert all(type(pair) is tuple and len(pair) == 2 and type(pair[1]) is int for pair in vector)
        labels = [pair[0] for pair in vector]
        assert labels[0] == ("native-memory-source", "candidate", "json-parser")
        assert labels[-1] == ("native-memory-report-stage", "source-binding-success-slot")
        # Exactly one original candidate formatter and one complete Vault consume.
        assert labels.count(("native-memory-report-stage", "candidate-format")) == 1
        assert labels.count(("native-memory-report-trace", "source-vault-success-slot",
            len(memory_producer._native_memory_source_vault_numeric_lineage(
                operation, prepared, **kwargs)[8]))) == 1
        observations.append(vector)
        raise HeaderBoundsError("test_complete_source_owner_observed_before_effect")

    monkeypatch.setattr(accounting_witness, "_native_memory_effect_numeric_schedule", inspect)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "complete-source-owner-observer"})
    assert response.status_code == 409 and len(observations) == 1
