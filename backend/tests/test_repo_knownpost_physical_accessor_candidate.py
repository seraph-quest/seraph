"""Actual issued knownpost physical reference; no synthetic owner or receipt."""
import asyncio
import copy
from contextlib import contextmanager

import pytest

from src.execution import repo_original_producer as producer
from src.workflows import repo_repair_source_recovery as recovery
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.test_repo_source_recovered_finalizer import _signed_ownerless_fixture, _scope, _rows, DENIED
from tests.test_repo_source_stop_knownpost_candidate import _physical_outside_sql


@pytest.mark.asyncio
async def test_actual_knownpost_getter_retains_original_physical_identity_and_scope(
        accounting_db, monkeypatch, repository_admission_signer):
    flow = await _signed_ownerless_fixture(accounting_db, monkeypatch, "test_python", unknown=True)
    jobs, service, owner = flow["jobs"], flow["service"], flow["kwargs"]["owner"]
    job_id = flow["kwargs"]["job_id"]
    async with await _scope(flow):
        pass  # The actual original publisher commits cleanup before knownpost staging.
    before = await _rows(jobs)
    issued = []
    original_stage = producer.stage_original_producer_completion

    @contextmanager
    def observe_original_physical(*args, **kwargs):
        with original_stage(*args, **kwargs) as actual:
            issued.append(actual)
            yield actual  # Return the actual original physical owner unchanged.

    monkeypatch.setattr(producer, "stage_original_producer_completion", observe_original_physical)
    with _physical_outside_sql(monkeypatch, flow) as physical:
        async with recovery.stage_repository_knownpost_completion(service, jobs,
                job_id=job_id, owner=owner, iteration_index=1) as witness:
            stage, context = recovery.repository_completion_knownpost_context(witness,
                service=service, jobs=jobs, owner=owner, job_id=job_id)
            actual = recovery.repository_knownpost_stage(stage)
            assert len(issued) == 1 and actual["physical"] is issued[0]
            assert producer.original_producer_completion_result(actual["physical"]) is actual["result"]
            with pytest.raises(TypeError):
                actual["physical"] = object()
            async with jobs._session() as db:
                await recovery.validate_repository_knownpost_projection_sql(db, service, jobs,
                    witness=witness, owner=owner, job_id=job_id)
            await recovery.recheck_repository_knownpost_physical(witness,
                service=service, jobs=jobs, owner=owner, job_id=job_id)
            for bad in (copy.copy(stage), object()):
                with pytest.raises(DENIED):
                    recovery.repository_knownpost_stage(bad)
            for bad in (copy.copy(actual["physical"]), object()):
                with pytest.raises(ValueError):
                    producer.original_producer_completion_result(bad)
            for changes in ({"jobs": object()}, {"service": object()}, {"job_id": "foreign"},
                    {"owner": owner.model_copy(update={"session_id": "foreign"})}):
                args = {"service": service, "jobs": jobs, "owner": owner, "job_id": job_id, **changes}
                with pytest.raises(DENIED):
                    await recovery.recheck_repository_knownpost_physical(witness, **args)
            with pytest.raises(DENIED):
                recovery.assert_repository_knownpost_stage(stage, service=service, jobs=jobs, fence=object())

            async def wrong_task():
                with pytest.raises(DENIED):
                    recovery.repository_knownpost_stage(stage)
                with pytest.raises(ValueError):
                    producer.original_producer_completion_result(actual["physical"])
            await asyncio.create_task(wrong_task())
    assert physical["guard_entries"] == 1
    with pytest.raises(DENIED):
        recovery.repository_knownpost_stage(stage)
    with pytest.raises(ValueError):
        producer.original_producer_completion_result(actual["physical"])
    with pytest.raises(DENIED):
        await recovery.recheck_repository_knownpost_physical(witness,
            service=service, jobs=jobs, owner=owner, job_id=job_id)
    assert await _rows(jobs) == before
