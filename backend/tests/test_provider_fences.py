"""#1024 A3.1: late reads after an owner change or provider loss, and provider
restart over unresolved effects, through the real reviewed Cordis child.

Forget, revoke and tombstone-reapply during an in-flight ``memory.retrieve``
(and the no-cache second read) are covered by ``test_cordis_services_core``
(#1117); this file adds the owner-change and provider-readiness fences and the
restart proof. Network and inference stay denied (``service_db``).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest
from sqlalchemy import select

from src.db.engine import get_session
from src.db.models import RuntimeCompositionState, WorkflowRunState
from src.runtime_plugins import worker as worker_module
from src.runtime_plugins.dispatch import NativeServiceBlocked
from src.runtime_plugins.ownership import CompositionDependency, composition_writer, transition_owner
from src.runtime_plugins.service_jobs import execute_native_read
from src.runtime_plugins.worker import ProviderWorker, attach
from src.workspace.lifecycle import lifecycle_fence_marker
from tests.test_cordis_services_core import (_MEMORY_READ, _PRIVATE_TEXT, _operator, _persisted_copies,  # noqa: F401
                                             _private_memory, live_host, needs_host, service_db)


@pytest.fixture(autouse=True)
def _detached_worker():
    worker_module._set_current(None)
    yield
    worker_module._set_current(None)


async def _drain(domain):
    async with get_session() as db:
        row = await db.get(RuntimeCompositionState, domain, populate_existing=True)
        expected = CompositionDependency(row.runtime_domain, row.owner_kind, row.epoch, row.composition_digest)
    with lifecycle_fence_marker():
        async with get_session() as db:
            async with composition_writer(db):
                await transition_owner(db, runtime_domain=domain, expected=expected, expected_state="ready",
                                       owner_kind=expected.owner_kind, epoch=expected.epoch,
                                       composition_digest=expected.composition_digest, state="draining")


@needs_host
@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["seraph.memory.v1", "seraph.state.v1", "seraph.audit.v1"])
async def test_owner_change_during_an_in_flight_read_denies_it(live_host, domain):
    """The composition vector is rechecked in the transaction that serves bytes."""
    host, jobs, calls = live_host
    operator = await _operator()
    await _private_memory(operator)
    real = host.request_service

    async def change_owner_after_read(method, payload, *, original_scope):
        result = await real(method, payload, original_scope=original_scope)
        if method == "memory.retrieve":
            await _drain(domain)
        return result
    host.request_service = change_owner_after_read
    with pytest.raises(NativeServiceBlocked) as denied:
        await execute_native_read(operator=operator, candidate=_MEMORY_READ, idempotency_key="owner-" + domain,
                                  host=host, jobs=jobs)
    assert denied.value.reason_code == "composition_dependency_changed"
    async with get_session() as db:
        run = (await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.idempotency_key == "owner-" + domain))).scalar_one()
    # Nothing completed: the composition fence also refuses the job CAS while the
    # owner drains, so the attempt stays leased until stale recovery (#1007).
    assert run.status != "succeeded" and run.result_digest is None
    assert _persisted_copies(_PRIVATE_TEXT) == []


@needs_host
@pytest.mark.asyncio
async def test_a_degraded_memory_provider_blocks_its_reads_typed(live_host):
    host, jobs, calls = live_host
    operator = await _operator()
    await _private_memory(operator)
    worker = attach(ProviderWorker())
    for name in worker.status:
        worker.status[name].state = "active"
    worker.status["memory"].state, worker.status["memory"].reason_code = "degraded", "memory_tables_missing"
    with pytest.raises(NativeServiceBlocked) as denied:
        await execute_native_read(operator=operator, candidate=_MEMORY_READ, idempotency_key="degraded",
                                  host=host, jobs=jobs)
    assert denied.value.reason_code == "native_provider_memory_unavailable"
    assert [call["method"] for call in calls][-1:] == ["memory.retrieve"]
    assert _persisted_copies(_PRIVATE_TEXT) == []


def _unresolved(run_identity, status):
    effect = {"effect_id": "cordis:effect-1", "receipt_kind": "intent", "effect_type": "external_write",
              "target_path": "external:target", "status": "intent", "details": {}, "recorded_at":
              datetime.now(timezone.utc).isoformat(), "fencing_token": 3}
    return WorkflowRunState(run_identity=run_identity, root_run_identity=run_identity, workflow_name="unresolved",
                            tool_name="unresolved", status=status, job_kind="runtime_service_read_v1",
                            capability_version="1", effect_receipts_json=json.dumps([effect]), revision=7,
                            fencing_token=3, attempt_count=1, started_at=datetime.now(timezone.utc))


async def _snapshot(identities):
    async with get_session() as db:
        rows = (await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.run_identity.in_(identities)).execution_options(populate_existing=True))).scalars().all()
    return {row.run_identity: (row.status, row.revision, row.fencing_token, row.attempt_count,
                               row.effect_receipts_json) for row in rows}


@needs_host
@pytest.mark.asyncio
async def test_provider_and_child_restart_retain_unresolved_effects_without_replay(live_host, monkeypatch):
    host, jobs, calls = live_host
    identities = ["unknown-effect", "cost-liability"]
    async with get_session() as db:
        db.add(_unresolved(identities[0], "unknown_external_effect"))
        db.add(_unresolved(identities[1], "cost_liability"))
    before = await _snapshot(identities)
    # The worker's state provider runs init_db against the bound test database.
    async def bound_init():
        return None
    monkeypatch.setattr(worker_module, "init_db", bound_init)
    for _ in range(2):
        worker = attach(ProviderWorker())
        await worker.activate()
        reports = await worker.provider_reports()
        await host.report_providers(reports)
        # Restart recovery runs as at boot; it never touches unresolved work.
        await jobs.recover_stale_jobs()
        await host.stop()
        assert host.snapshot()["cleanup"]["process_reaped"] is True
        assert await host.start(), host.snapshot()
        worker.close_gate()
        worker.detach()
    assert await _snapshot(identities) == before
    assert [call for call in calls if call.get("invocation_ref") in identities] == []
