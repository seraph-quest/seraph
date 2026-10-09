"""Negative private-owner checks; these do not claim a restart journey."""
import copy
import json

import pytest

from src.workflows.repo_repair_source_recovery import (
    RepositorySourceRecoveryError,
    _OriginalRepositoryProducerCompletionWitness,
    assert_repository_completion_witness,
    repository_completion_context,
    repository_completion_result,
    repository_completion_post_cas,
    repository_completion_outcome,
    recover_original_repository_cleanup,
    read_registered_repository_producer,
)
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_repo_work_task_publication import _actual_source_callback_journey
from src.workflows.job_runtime import _digest


@pytest.mark.parametrize("read", [assert_repository_completion_witness,
    repository_completion_context, repository_completion_result,
    repository_completion_post_cas, repository_completion_outcome])
def test_constructed_or_copied_completion_object_grants_no_authority(read):
    constructed = _OriginalRepositoryProducerCompletionWitness()
    for candidate in (constructed, copy.copy(constructed), {}, None):
        with pytest.raises(RepositorySourceRecoveryError,
                match="original_repository_completion_witness_required"):
            read(candidate)


@pytest.mark.asyncio
@pytest.mark.parametrize("revision,action", [(True, "reconcile_original_cleanup"),
    (-1, "reconcile_original_cleanup"), (0, "resume"), ("0", "reconcile_original_cleanup")])
async def test_private_owner_also_denies_invalid_request_before_any_source_read(revision, action):
    with pytest.raises(RepositorySourceRecoveryError,
            match="repository_source_recovery_request_invalid"):
        await recover_original_repository_cleanup(None, None, job_id="never-read", owner=None,
            expected_job_revision=revision, action=action)


@pytest.mark.asyncio
async def test_actual_canonical_producer_registration_rejects_rehashed_scope_tampering(
        accounting_db, monkeypatch, repository_admission_signer):
    flow = await _actual_source_callback_journey(accounting_db, monkeypatch, False, "test_python")
    async with flow["factory"]() as db:
        root = await flow["jobs"]._fetch(db, flow["root_id"])
        original = read_registered_repository_producer(root, iteration_index=1)
        registration_id = "repository:producer:" + original["iteration_id"]
        for field, value in (("iteration_index", True), ("root_fence", True),
                ("owner_session_id", "foreign"), ("original_source_digest", "0" * 64),
                ("source_artifact_digest", "0" * 64), ("execution_digest", "0" * 64),
                ("monotonic_deadline", True), ("proposal_predecessor", {"status": "execution_started",
                    "revision": True, "last_receipt_id": None})):
            history = json.loads(root.checkpoint_receipts_json)
            item = next(record for record in history if record["checkpoint_id"] == registration_id)
            item["payload"][field] = value
            # Repairing a mutable journal self-digest cannot change the
            # original canonical Source/Ready binding into recovery authority.
            item["state_digest"] = _digest(item["payload"])
            edited = root.model_copy(update={"checkpoint_receipts_json": json.dumps(history)})
            with pytest.raises(RepositorySourceRecoveryError,
                    match="original_producer_registration_changed"):
                read_registered_repository_producer(edited, iteration_index=1)


@pytest.mark.asyncio
async def test_actual_source_completion_writer_rolls_back_whole_cleanup_then_retries_same_result(
        accounting_db, monkeypatch, repository_admission_signer):
    from src.workflows import repo_repair_source as source
    from src.workflows import repo_repair_source_recovery as recovery
    captured = {}
    real_publish = recovery.publish_original_repository_completion
    real_append = source._append_repository_record

    async def capture_publish(service, jobs, **kwargs):
        captured.update(service=service, jobs=jobs, kwargs=kwargs)
        async with jobs._session() as db:
            run = await jobs._fetch(db, kwargs["job_id"])
            captured["before"] = run.model_dump(mode="json")
        return await real_publish(service, jobs, **kwargs)

    def fail_readback(run, identity, payload, **kwargs):
        if identity.startswith("repository:readback:"):
            raise RuntimeError("injected_original_readback_writer_failure")
        return real_append(run, identity, payload, **kwargs)

    monkeypatch.setattr(recovery, "publish_original_repository_completion", capture_publish)
    monkeypatch.setattr(source, "_append_repository_record", fail_readback)
    with pytest.raises(RuntimeError, match="injected_original_readback_writer_failure"):
        await _actual_source_callback_journey(accounting_db, monkeypatch, False, "test_python")
    jobs, kwargs = captured["jobs"], captured["kwargs"]
    async with jobs._session() as db:
        run = await jobs._fetch(db, kwargs["job_id"])
        assert run.model_dump(mode="json") == captured["before"]
        registration = read_registered_repository_producer(run, iteration_index=1)
        assert source._repository_record(run, "repository:cleanup:" + registration["iteration_id"]) is None
        assert source._repository_record(run, "repository:readback:" + registration["iteration_id"]) is None
        assert jobs._repo_repair_reservation_state(run)["status"] == "held"
    monkeypatch.setattr(source, "_append_repository_record", real_append)
    # Retry uses the SAME actual originally registered producer result and
    # private owner. It creates no replacement job, channel or physical run.
    witness = await real_publish(captured["service"], jobs, **kwargs)
    outcome = recovery.repository_completion_outcome(witness)
    assert outcome["status"] == "succeeded"
    async with jobs._session() as db:
        run = await jobs._fetch(db, kwargs["job_id"])
        assert run.revision == captured["before"]["revision"] + 1
        cleanup = source._repository_record(run, "repository:cleanup:" + registration["iteration_id"])
        readback = source._repository_record(run, "repository:readback:" + registration["iteration_id"])
        assert cleanup["source_completion_cas"] == readback["source_completion_cas"]
        assert jobs._repo_repair_reservation_state(run)["status"] == "held"
