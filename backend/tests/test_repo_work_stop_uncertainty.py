"""Exact original uncertainty successor, real writer rollback and read-only drift."""
import asyncio
import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.workflows.job_runtime import DurableJobLeaseError, _digest
from src.workflows import repo_repair_source as source_module
from src.workflows.repo_repair_stop import stop_repository_root
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_repo_work_stop_metadata import stopped_original, forbid_private_metadata_reads


async def original_and_owners(accounting_db, monkeypatch):
    factory, owner, source, jobs, binding, root_id, service = await stopped_original(
        accounting_db, monkeypatch, prepared=True)
    validator = source_module.validate_repository_stop_witness
    async def rollback_terminal(*args, **kwargs):
        await validator(*args, **kwargs)
        raise DurableJobLeaseError("actual terminal writer retained Pending")
    monkeypatch.setattr(source_module, "validate_repository_stop_witness", rollback_terminal)
    async with factory() as db:
        run = await jobs._fetch(db, root_id)
        original = source_module.read_repository_original(run)[0]
        lease_owner, fencing_token = run.lease_owner, run.fencing_token
    iteration_id = source_module.iteration_identity(root_id, original["repository_attempt_id"],
        source_module._source_digest(original["original_input"]), 1)
    async def stop():
        return await stop_repository_root(source, jobs, job_id=root_id, owner=owner,
            general_task_service=service, reason="operator_cancelled")
    async def uncertain():
        return await source_module._quarantine_original_uncertainty(source, jobs, job_id=root_id, owner=owner,
            lease_owner=lease_owner, fencing_token=fencing_token,
            reason="repository_process_closure_unproven", result={"no_learning": True,
                "operator_action": "reconcile_original_process", "iteration_id": iteration_id})
    return factory, owner, source, jobs, root_id, service, stop, uncertain


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["stop", "uncertain"])
async def test_original_uncertainty_and_stop_actual_lock_race(accounting_db, monkeypatch, repository_admission_signer, first):
    factory, owner, source, jobs, root_id, service, stop, uncertain = await original_and_owners(accounting_db, monkeypatch)
    from src.model_fabric.effective_policy import configuration_mutation_lock
    await configuration_mutation_lock.acquire()
    try:
        owners = [stop, uncertain] if first == "stop" else [uncertain, stop]
        tasks = [asyncio.create_task(operation()) for operation in owners]
        await asyncio.sleep(0)
    finally:
        configuration_mutation_lock.release()
    await asyncio.gather(*tasks)
    forbid_private_metadata_reads(monkeypatch, source)
    projection = await source_module.repository_operator_projection(source, jobs, job_id=root_id, owner=owner)
    assert projection["status"] == "unknown_external_effect" and projection["repository_stop"]["pending"] is True
    async with factory() as db:
        run = await jobs._fetch(db, root_id)
        assert jobs._repo_repair_reservation_state(run)["status"] == "held"
        assert (source_module._repository_record(run, "repository:stop-uncertainty-successor:v1") is not None) == (first == "stop")


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["stop_digest", "predecessor_digest", "successor_digest", "fencing_token", "foreign_key"])
async def test_original_successor_tamper_denies_without_private_read(accounting_db, monkeypatch, repository_admission_signer, drift):
    factory, owner, source, jobs, root_id, service, stop, uncertain = await original_and_owners(accounting_db, monkeypatch)
    await stop()
    await uncertain()
    async with factory() as db:
        run = await jobs._fetch(db, root_id)
        journal = json.loads(run.checkpoint_receipts_json)
        record = next(item for item in journal if item["checkpoint_id"] == "repository:stop-uncertainty-successor:v1")
        record["payload"][drift] = "0" * 64
        record["state_digest"] = _digest(record["payload"])
        run.checkpoint_receipts_json = json.dumps(journal)
        await db.commit()
    forbid_private_metadata_reads(monkeypatch, source)
    with pytest.raises(DurableJobLeaseError):
        await source_module.repository_operator_projection(source, jobs, job_id=root_id, owner=owner)
    async with factory() as db:
        run = await jobs._fetch(db, root_id)
        assert run.status == "unknown_external_effect"
        assert jobs._repo_repair_reservation_state(run)["status"] == "held"


@pytest.mark.asyncio
async def test_original_uncertainty_real_sql_rollback_has_no_partial_successor(accounting_db, monkeypatch, repository_admission_signer):
    factory, owner, source, jobs, root_id, service, stop, uncertain = await original_and_owners(accounting_db, monkeypatch)
    await stop()
    async with factory() as db:
        run = await jobs._fetch(db, root_id)
        before = run.model_dump(mode="json")
        await db.execute(text("CREATE TRIGGER reject_original_uncertainty BEFORE UPDATE OF status ON workflow_run_states "
            "WHEN NEW.run_identity = '" + root_id + "' AND NEW.status = 'unknown_external_effect' "
            "BEGIN SELECT RAISE(ABORT, 'disposable original transition rollback'); END"))
        await db.commit()
    with pytest.raises(IntegrityError):
        await uncertain()
    async with factory() as db:
        run = await jobs._fetch(db, root_id)
        assert run.model_dump(mode="json") == before
        assert source_module._repository_record(run, "repository:stop-uncertainty-successor:v1") is None
        assert jobs._repo_repair_reservation_state(run)["status"] == "held"
