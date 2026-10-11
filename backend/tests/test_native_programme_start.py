"""Real registered admission, native claim and protected START; no inference.

ADR026 §1/§7 gives this later applicable native operation one retained frame.
ADR034 final paragraphs keep admission and later persisted Source selectors
distinct. These tests never manufacture a claim, Source or publication receipt.
"""
import asyncio
import copy
import json

import pytest
from sqlalchemy import select

from tests.test_programme_prospective_live_selection import genuine_live_selection


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_ack", [False, True])
async def test_original_programme_start_commit_delivery_and_source_negatives(
    genuine_live_selection, monkeypatch, lost_ack,
):
    from config.settings import settings
    from src.db.models import WorkflowRunState
    from src.guardian.goal_discovery import goal_discovery_service as service
    from src.scheduler import engine as scheduler_engine
    from src.runtime_plugins.bridge import cordis_host
    from src.runtime_plugins.dispatch import NativeServiceBlocked
    from src.runtime_plugins.programme_producer import START_PREFIX

    queued, executed, sources = [], [], []
    original_run = service.run
    async def retain_real_admission(job_id):
        queued.append(job_id)
    monkeypatch.setattr(service, "run", retain_real_admission)
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    scheduler = scheduler_engine.init_scheduler()
    assert scheduler is not None
    scheduler.pause()
    try:
        await scheduler.get_job("goal_public_discovery").func()
        assert len(queued) == 1
    finally:
        scheduler_engine.shutdown_scheduler()
    monkeypatch.setattr(service, "run", original_run)

    original_claim = service.jobs.claim_service_job
    async def inspect_real_source(*args, **kwargs):
        source = kwargs["native_programme_source"]
        sources.append(source)
        assert source.validate(owner=True) is source
        copied = copy.copy(source)
        with pytest.raises(NativeServiceBlocked, match="native_programme_source_changed"):
            copied.validate(owner=True)
        async def wrong_task():
            with pytest.raises(NativeServiceBlocked, match="native_programme_original_task_changed"):
                source.validate(owner=True)
        await asyncio.create_task(wrong_task())
        return await original_claim(*args, **kwargs)
    monkeypatch.setattr(service.jobs, "claim_service_job", inspect_real_source)

    async def observe_execution(job_id, owner, fence, *, native_programme_source):
        source = native_programme_source
        assert source.validate(owner=True) is source
        assert source.task is asyncio.current_task()
        assert source.state == "executing" and source.publication.committed
        assert source.publication.checked and source.response == source.publication.response
        assert source.header_budget.remaining < 1_048_576
        executed.append(job_id)
    monkeypatch.setattr(service, "_execute", observe_execution)
    original_request = cordis_host.request_service
    async def lose_only_delivered_ack(*args, **kwargs):
        result = await original_request(*args, **kwargs)
        assert result["status"] == "succeeded"
        if lost_ack:
            raise RuntimeError("test_lost_delivered_ack")
        return result
    monkeypatch.setattr(cordis_host, "request_service", lose_only_delivered_ack)
    if lost_ack:
        with pytest.raises(RuntimeError, match="test_lost_delivered_ack"):
            await original_run(queued[0])
    else:
        result = await original_run(queued[0])
        assert result["status"] == "succeeded" and result["memory_status"] == "no_learning"
    assert executed == ([] if lost_ack else queued)
    assert len(sources) == 1 and sources[0].closed
    assert service._native_programme_sources == {} and not service._tasks
    async with service.jobs._session() as db:
        row = await db.scalar(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == queued[0]))
        entries = [item for item in json.loads(row.checkpoint_receipts_json)
            if item["checkpoint_id"].startswith(START_PREFIX)]
        assert len(entries) == 1 and entries[0] == sources[0].publication.receipt
        assert row.status == "running" and row.attempt_count == 1
    with pytest.raises(NativeServiceBlocked, match="native_programme_source_changed"):
        sources[0].validate(owner=True)
    assert genuine_live_selection.contacts == []


@pytest.mark.asyncio
async def test_native_programme_start_missing_original_forecast_stops_before_receipt(
    genuine_live_selection, monkeypatch,
):
    from config.settings import settings
    from src.db.models import WorkflowRunState
    from src.guardian.goal_discovery import goal_discovery_service as service
    from src.scheduler import engine as scheduler_engine
    from src.runtime_plugins.dispatch import NativeServiceBlocked
    from src.runtime_plugins.programme_producer import START_PREFIX
    from src.workspace.accounting_witness import CompositionSessionGuard

    # This is the actual absent original producer, never a fixture fee list.
    assert "_reserve_original_native_programme_start" not in CompositionSessionGuard.__dict__
    queued, executed = [], []
    original_run = service.run
    async def retain_real_admission(job_id):
        queued.append(job_id)
    monkeypatch.setattr(service, "run", retain_real_admission)
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    scheduler = scheduler_engine.init_scheduler()
    assert scheduler is not None
    scheduler.pause()
    try:
        await scheduler.get_job("goal_public_discovery").func()
        assert len(queued) == 1
    finally:
        scheduler_engine.shutdown_scheduler()
    monkeypatch.setattr(service, "run", original_run)
    async def forbidden_execution(*args, **kwargs):
        executed.append(True)
        pytest.fail("missing original capacity forecast reached execution")
    monkeypatch.setattr(service, "_execute", forbidden_execution)
    with pytest.raises(NativeServiceBlocked, match="native_programme_start_response_unavailable"):
        await original_run(queued[0])
    async with service.jobs._session() as db:
        row = await db.scalar(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == queued[0]))
        assert not any(item["checkpoint_id"].startswith(START_PREFIX)
            for item in json.loads(row.checkpoint_receipts_json))
        assert row.status == "running" and row.attempt_count == 1
    assert executed == [] and genuine_live_selection.contacts == []
    assert service._native_programme_sources == {} and not service._tasks
