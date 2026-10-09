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
    repository_completion_cleanup_envelope,
    repository_original_stop_completion_cleanup_envelope,
    recover_original_repository_cleanup,
    read_registered_repository_producer,
    _OriginalRepositoryStopCompletionWitness,
    assert_repository_original_stop_completion,
    repository_original_stop_completion_result,
)
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_repo_work_task_publication import _actual_source_callback_journey
from src.workflows.job_runtime import _digest


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["test_python", "test_node"])
async def test_actual_ownerless_bundle_derives_exact_existing_native_projection(
        accounting_db, monkeypatch, repository_admission_signer, language):
    from src.execution import repo_original_producer as producer
    from src.workflows import repo_repair_source as source, repo_repair_source_recovery as recovery
    from src.workflows.job_runtime import _canonical
    flow = await _actual_source_callback_journey(accounting_db, monkeypatch, False, language)
    async with flow["factory"]() as db:
        root = await flow["jobs"]._fetch(db, flow["root_id"])
        registration = read_registered_repository_producer(root, iteration_index=1)
        cleanup = source._repository_record(root, "repository:cleanup:" + registration["iteration_id"])
    envelope = json.loads(flow["service"].repository_source_service._read_private_artifact(cleanup["artifact_ref"],
        expected_digest=cleanup["artifact_digest"]))
    # Actual original completion and actual released guard, not an edited
    # ready/result fixture. This is physical staging, not backend restart.
    with producer.stage_original_producer_completion(registration) as physical:
        result = producer.original_producer_completion_result(physical)
        assert "iteration_cleanup_witness" not in result
        assert "live_parent_transport" not in result
        assert _canonical(recovery._original_repository_cleanup_projection(registration, result)) == _canonical(envelope["physical_projection"])
        assert set(envelope["physical_projection"]) == {"schema", "job_id", "attempt_id", "fencing_token",
            "iteration_binding", "process_cleanup", "supervisor_identity", "supervisor_transport",
            "stage_removed", "status", "artifact_digests"}


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["test_python", "test_node"])
async def test_actual_knownpost_literal_anchor_and_current_ledger_deny_tamper(
        accounting_db, monkeypatch, repository_admission_signer, language):
    from sqlalchemy import update
    from src.db.models import RepoRepairProposal, ApprovalRequest, InferenceCostReservation
    from src.workflows import repo_repair_source as source, repo_repair_stop as stop
    from src.workflows import repo_repair_source_recovery as recovery
    from src.workflows.job_runtime import DurableJobLeaseError, _canonical
    from src.work_board.repository import BoardError
    from src.workflows.repo_repair import RepoRepairError
    captured = {}
    real_publish = recovery.publish_original_repository_completion

    class OriginalCommitted(Exception):
        pass

    async def capture_original(service, jobs, **kwargs):
        captured.update(service=service, jobs=jobs, kwargs=kwargs)
        async with jobs._session() as db:
            root = await jobs._fetch(db, kwargs["job_id"])
            registered = read_registered_repository_producer(root, iteration_index=1)
            lease_owner, fence = root.lease_owner, root.fencing_token
        async with recovery._repository_recovery_fence(service, jobs,
                job_id=kwargs["job_id"], owner=kwargs["owner"]) as held:
            context = await stop._context(service, jobs, job_id=kwargs["job_id"], owner=kwargs["owner"])
            await stop._persist_repository_stop_intent_locked(service, jobs, context=context,
                owner=kwargs["owner"], reason="operator_cancelled", fence=held)
        await source._quarantine_original_uncertainty(service, jobs, job_id=kwargs["job_id"],
            owner=kwargs["owner"], lease_owner=lease_owner, fencing_token=fence,
            reason="repository_process_closure_unproven", result={"no_learning": True,
                "operator_action": "reconcile_original_process", "iteration_id": registered["iteration_id"]})
        await real_publish(service, jobs, **kwargs)
        raise OriginalCommitted()

    monkeypatch.setattr(recovery, "publish_original_repository_completion", capture_original)
    with pytest.raises(OriginalCommitted):
        await _actual_source_callback_journey(accounting_db, monkeypatch, False, language)
    monkeypatch.setattr(recovery, "publish_original_repository_completion", real_publish)
    service, jobs, kwargs = captured["service"], captured["jobs"], captured["kwargs"]
    async with jobs._session() as db:
        root = await jobs._fetch(db, kwargs["job_id"])
        before = root.model_dump(mode="json")
        registered = read_registered_repository_producer(root, iteration_index=1)
        identity = registered["iteration_id"]
        execution = source._repository_record(root, "repository:execution:" + identity)
        stop_record = source._repository_record(root, stop.STOP_ID)

    async def enter(*, deny=False):
        async with recovery.stage_repository_knownpost_completion(service, jobs,
                job_id=kwargs["job_id"], owner=kwargs["owner"], iteration_index=1,
                expected_job_revision=before["revision"]) as witness:
            if deny:
                pytest.fail("Tampered current facts yielded a recovery witness")
            assert recovery.repository_completion_cleanup_envelope(witness)["source_completion_cas"]["post_revision"] == before["revision"]

    await enter()
    path = service._workspace() / ("artifacts/repo-repair/model/iteration-" + identity + "-cleanup.json")
    original_bytes = path.read_bytes()
    original_envelope = json.loads(original_bytes)
    # This mapping is used only by the pure hash codec, never as an issuer.
    # The result and signed bundle came from the actual original callback.
    result = service._iterative_process_callbacks[identity].result()
    projection = recovery._original_repository_cleanup_projection(registered, result)
    assert _canonical(projection) == _canonical(result["iteration_cleanup_witness"].projection())
    assert _canonical(projection) == _canonical(original_envelope["physical_projection"])
    cold_result = {key: value for key, value in result.items() if key != "iteration_cleanup_witness"}
    assert recovery._verify_repository_knownpost_root(root, registered, cold_result, original_envelope)
    def deny_closed_projection(altered):
        # Negative pure-codec input only: keep both suffix payload hashes
        # consistent with the changed envelope so the closed projection gate,
        # rather than an earlier file/suffix hash mismatch, must reject it.
        # No stage, context or completion authority is issued from this row.
        original_journal = root.checkpoint_receipts_json
        history = json.loads(original_journal)
        payloads = recovery._knownpost_original_payloads(root, registered, cold_result, altered)
        for wrapper, payload in zip(history[-2:], payloads):
            wrapper["payload"] = payload
            wrapper["state_digest"] = _digest(payload)
        root.checkpoint_receipts_json = _canonical(history)
        try:
            with pytest.raises(RepositorySourceRecoveryError, match="original_repository_knownpost_physical_changed"):
                recovery._verify_repository_knownpost_root(root, registered, cold_result, altered)
        finally:
            root.checkpoint_receipts_json = original_journal
    for field in projection:
        for omit in (True, False):
            altered = json.loads(original_bytes)
            if omit:
                altered["physical_projection"].pop(field)
            else:
                altered["physical_projection"][field] = None
            deny_closed_projection(altered)
    old_three = json.loads(original_bytes)
    old_three["physical_projection"] = {key: projection[key] for key in (
        "iteration_binding", "process_cleanup", "artifact_digests")}
    deny_closed_projection(old_three)
    for field, value in (("stage_removed", 1), ("fencing_token", True)):
        altered = json.loads(original_bytes)
        altered["physical_projection"][field] = value
        deny_closed_projection(altered)
    for field in ("cleanup_proven",):
        altered = json.loads(original_bytes)
        altered["physical_projection"]["process_cleanup"][field] = 1
        deny_closed_projection(altered)
    for field in ("command_output_drained", "command_descriptors_closed", "original_children_waited", "no_spawn"):
        altered = json.loads(original_bytes)
        altered["physical_projection"]["supervisor_transport"][field] = 1
        deny_closed_projection(altered)
    altered_result = {**cold_result, "manifest": json.loads(_canonical(cold_result["manifest"]))}
    altered_result["manifest"]["iteration_binding"]["repository_fence"] = True
    # An altered negative input is not claimed to have a valid signature and
    # cannot issue a physical scope. It isolates the pure codec's typed
    # manifest-to-authentic-registration binding comparison.
    original_journal = root.checkpoint_receipts_json
    altered_history = json.loads(original_journal)
    for wrapper, payload in zip(altered_history[-2:], recovery._knownpost_original_payloads(
            root, registered, altered_result, original_envelope)):
        wrapper["payload"] = payload
        wrapper["state_digest"] = _digest(payload)
    root.checkpoint_receipts_json = _canonical(altered_history)
    try:
        with pytest.raises(RepositorySourceRecoveryError, match="original_repository_knownpost_physical_changed"):
            recovery._verify_repository_knownpost_root(root, registered, altered_result, original_envelope)
    finally:
        root.checkpoint_receipts_json = original_journal
    variants = []
    for field, value in (("before_revision", original_envelope["source_completion_cas"]["before_revision"] - 1),
            ("rows_digest", "0" * 64), ("unknown_projection_digest", "0" * 64)):
        changed = json.loads(original_bytes)
        changed["source_completion_cas"][field] = value
        variants.append(changed)
    changed = json.loads(original_bytes)
    changed["source_append_metadata"][0]["created_at"] = "2000-01-01T00:00:00+00:00"
    variants.append(changed)
    changed = json.loads(original_bytes)
    changed["physical_projection"]["process_cleanup"]["cleanup_proven"] = False
    variants.append(changed)
    variants.append({**original_envelope, "copied_authority": True})
    for changed in variants:
        path.write_bytes(_canonical(changed).encode())
        try:
            with pytest.raises(RepositorySourceRecoveryError):
                await enter(deny=True)
        finally:
            path.write_bytes(original_bytes)
    snapshot_path = service._workspace() / stop_record["snapshot_artifact_ref"].removeprefix("workspace-json:")
    snapshot_bytes = snapshot_path.read_bytes()
    snapshot_path.write_bytes(b"{}")
    try:
        with pytest.raises((RepositorySourceRecoveryError, DurableJobLeaseError, BoardError, RepoRepairError)):
            await enter(deny=True)
    finally:
        snapshot_path.write_bytes(snapshot_bytes)
    mutations = [(RepoRepairProposal, "proposal_id", execution["proposal_id"], "revision", lambda value: value + 1),
        (ApprovalRequest, "id", execution["approval_id"], "status", lambda value: "denied"),
        (InferenceCostReservation, "operation_id", "remote:repo-work:" + identity, "actual_cost_microusd", lambda value: value + 1),
        (InferenceCostReservation, "operation_id", "remote:repo-work:" + identity, "job_id", lambda value: "foreign-root"),
        (InferenceCostReservation, "operation_id", "remote:repo-work:" + identity, "state", lambda value: "unknown")]
    for model, key_name, key, field, change in mutations:
        async with jobs._session() as db:
            row = await db.get(model, key)
            original_value = getattr(row, field)
            await db.execute(update(model).where(getattr(model, key_name) == key).values(**{field: change(original_value)}))
            await db.commit()
        try:
            with pytest.raises((RepositorySourceRecoveryError, DurableJobLeaseError, BoardError)):
                await enter(deny=True)
        finally:
            async with jobs._session() as db:
                await db.execute(update(model).where(getattr(model, key_name) == key).values(**{field: original_value}))
                await db.commit()
        async with jobs._session() as db:
            assert (await jobs._fetch(db, kwargs["job_id"])).model_dump(mode="json") == before
    await enter()
    assert path.read_bytes() == original_bytes


@pytest.mark.parametrize("read", [assert_repository_completion_witness,
    repository_completion_context, repository_completion_result,
    repository_completion_post_cas, repository_completion_outcome, repository_completion_cleanup_envelope])
def test_constructed_or_copied_completion_object_grants_no_authority(read):
    constructed = _OriginalRepositoryProducerCompletionWitness()
    for candidate in (constructed, copy.copy(constructed), {}, None):
        with pytest.raises(RepositorySourceRecoveryError,
                match="original_repository_completion_witness_required"):
            read(candidate)


def test_forged_or_copied_stop_scope_grants_no_cleanup_authority():
    constructed = _OriginalRepositoryStopCompletionWitness()
    for candidate in (constructed, copy.copy(constructed), {}, None):
        with pytest.raises(RepositorySourceRecoveryError, match="original_repository_stop_completion_required"):
            assert_repository_original_stop_completion(candidate, service=None, jobs=None)
        with pytest.raises(RepositorySourceRecoveryError, match="original_repository_stop_completion_required"):
            repository_original_stop_completion_result(candidate, iteration_id="0" * 64)
        with pytest.raises(RepositorySourceRecoveryError, match="original_repository_stop_completion_required"):
            repository_original_stop_completion_cleanup_envelope(candidate, iteration_id="0" * 64)


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
@pytest.mark.parametrize("language", ["python", "node"])
async def test_actual_source_completion_writer_rolls_back_whole_cleanup_then_retries_same_result(
        accounting_db, monkeypatch, repository_admission_signer, language):
    from src.workflows.job_runtime import _canonical
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
        await _actual_source_callback_journey(accounting_db, monkeypatch, False, "test_" + language)
    jobs, kwargs = captured["jobs"], captured["kwargs"]
    async with jobs._session() as db:
        run = await jobs._fetch(db, kwargs["job_id"])
        assert run.model_dump(mode="json") == captured["before"]
        registration = read_registered_repository_producer(run, iteration_index=1)
        assert source._repository_record(run, "repository:cleanup:" + registration["iteration_id"]) is None
        assert source._repository_record(run, "repository:readback:" + registration["iteration_id"]) is None
        assert jobs._repo_repair_reservation_state(run)["status"] == "held"
    prefix = "artifacts/repo-repair/model/iteration-" + registration["iteration_id"]
    before_artifact = (captured["service"]._workspace() / (prefix + "-cleanup.json")).read_bytes()
    original_envelope = json.loads(before_artifact)
    assert set(original_envelope) == {"physical_projection", "source_completion_cas", "source_append_metadata"}
    assert len(original_envelope["source_append_metadata"]) == 2
    monkeypatch.setattr(source, "_append_repository_record", real_append)
    artifact_path = captured["service"]._workspace() / (prefix + "-cleanup.json")
    for revision_field in ("before_revision", "post_revision"):
        altered_envelope = json.loads(before_artifact)
        actual_revision = altered_envelope["source_completion_cas"][revision_field]
        assert type(actual_revision) is int
        altered_envelope["source_completion_cas"][revision_field] = float(actual_revision)
        # Numerically equal JSON floats cannot authorize reuse of an original
        # Source integer CAS. The actual signed producer and owner stay intact.
        altered_bytes = _canonical(altered_envelope).encode()
        artifact_path.write_bytes(altered_bytes)
        try:
            with pytest.raises(RepositorySourceRecoveryError,
                    match="^original_producer_orphan_epoch_changed$"):
                await real_publish(captured["service"], jobs, **kwargs)
            async with jobs._session() as db:
                run = await jobs._fetch(db, kwargs["job_id"])
                assert run.model_dump(mode="json") == captured["before"]
                assert jobs._repo_repair_reservation_state(run)["status"] == "held"
            assert artifact_path.read_bytes() == altered_bytes
        finally:
            artifact_path.write_bytes(before_artifact)
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
        assert (captured["service"]._workspace() / (prefix + "-cleanup.json")).read_bytes() == before_artifact
        assert recovery.repository_completion_cleanup_envelope(witness) == original_envelope
        wrappers = [item for item in json.loads(run.checkpoint_receipts_json)
            if item["checkpoint_id"] in {"repository:cleanup:" + registration["iteration_id"],
                "repository:readback:" + registration["iteration_id"]}]
        assert [{key: item[key] for key in ("checkpoint_id", "safe", "created_at")}
            for item in wrappers] == original_envelope["source_append_metadata"]
        after = run.model_dump(mode="json")
        assert {key: value for key, value in after.items() if key not in {"revision", "checkpoint_receipts_json"}} == {
            key: value for key, value in captured["before"].items() if key not in {"revision", "checkpoint_receipts_json"}}
        assert jobs._repo_repair_reservation_state(run)["status"] == "held"


@pytest.mark.parametrize("kind", ["fifo", "symlink", "hardlink", "directory", "oversize"])
def test_existing_cleanup_reader_denies_actual_unsafe_entry_without_blocking(tmp_path, kind):
    import os
    import time
    from types import SimpleNamespace
    from src.workflows.repo_repair_source_recovery import _read_original_cleanup_envelope_if_present
    folder = tmp_path / "artifacts" / "repo-repair" / "model"
    folder.mkdir(parents=True, mode=0o700)
    for path in (tmp_path, folder.parent.parent, folder.parent, folder):
        path.chmod(0o700)
    path = folder / "iteration-original-cleanup.json"
    if kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind == "directory":
        path.mkdir(mode=0o700)
    elif kind in {"symlink", "hardlink"}:
        other = folder / "other.json"
        other.write_bytes(b"{}")
        other.chmod(0o600)
        if kind == "symlink":
            path.symlink_to(other.name)
        else:
            os.link(other, path)
    else:
        path.write_bytes(b"x" * (1048576 + 1))
        path.chmod(0o600)
    service = SimpleNamespace(_workspace=lambda: tmp_path)
    started = time.monotonic()
    with pytest.raises(RepositorySourceRecoveryError, match="original_producer_artifact_changed"):
        _read_original_cleanup_envelope_if_present(service,
            "artifacts/repo-repair/model/iteration-original-cleanup.json")
    assert time.monotonic() - started < 2


def test_existing_cleanup_reader_denies_actual_named_replacement_during_read(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace
    from src.workflows import repo_repair_source_recovery as recovery
    folder = tmp_path / "artifacts" / "repo-repair" / "model"
    folder.mkdir(parents=True, mode=0o700)
    for path in (tmp_path, folder.parent.parent, folder.parent, folder):
        path.chmod(0o700)
    target = folder / "iteration-original-cleanup.json"
    raw = b'{"physical_projection":{},"source_append_metadata":[],"source_completion_cas":{}}'
    target.write_bytes(raw)
    target.chmod(0o600)
    real_fdopen = os.fdopen

    class ReadThenReplace:
        def __init__(self, handle):
            self.handle = handle
        def __enter__(self):
            self.handle.__enter__()
            return self
        def __exit__(self, *args):
            return self.handle.__exit__(*args)
        def read(self, size):
            value = self.handle.read(size)
            replacement = folder / "replacement.json"
            replacement.write_bytes(raw)
            replacement.chmod(0o600)
            os.replace(replacement, target)
            return value

    monkeypatch.setattr(os, "fdopen", lambda *args, **kwargs: ReadThenReplace(real_fdopen(*args, **kwargs)))
    with pytest.raises(RepositorySourceRecoveryError, match="original_producer_artifact_changed"):
        recovery._read_original_cleanup_envelope_if_present(SimpleNamespace(_workspace=lambda: tmp_path),
            "artifacts/repo-repair/model/iteration-original-cleanup.json")


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["status", "cost", "bound", "missing", "foreign_root", "membership"])
async def test_actual_source_completion_denies_changed_original_reservation_facts(
        accounting_db, monkeypatch, repository_admission_signer, mutation):
    from sqlalchemy import select
    from src.db.models import InferenceCostReservation, RepoRepairProposal, ApprovalRequest, WorkflowRunState
    from src.workflows import repo_repair_source_recovery as recovery
    captured = {}
    real_publish = recovery.publish_original_repository_completion

    async def hold_before_publication(service, jobs, **kwargs):
        captured.update(service=service, jobs=jobs, kwargs=kwargs)
        raise RuntimeError("held_actual_original_before_publication")

    monkeypatch.setattr(recovery, "publish_original_repository_completion", hold_before_publication)
    with pytest.raises(RuntimeError, match="held_actual_original_before_publication"):
        await _actual_source_callback_journey(accounting_db, monkeypatch, False, "test_python")
    jobs, kwargs = captured["jobs"], captured["kwargs"]
    async with jobs._session() as db:
        root = await jobs._fetch(db, kwargs["job_id"])
        registration = read_registered_repository_producer(root, iteration_index=1)
        operation = "remote:repo-work:" + registration["iteration_id"]
        cost = await db.get(InferenceCostReservation, operation)
        assert cost is not None and cost.state == "settled"
        if mutation == "status":
            cost.state = "unknown"
        elif mutation == "cost":
            cost.actual_cost_microusd += 1
        elif mutation == "bound":
            cost.bound_microusd += 1
        elif mutation == "missing":
            await db.delete(cost)
        elif mutation == "foreign_root":
            cost.job_id = "foreign-root"
        else:
            cost.group_lookup_key = "none"
        await db.commit()

    async def exact_rows():
        async with jobs._session() as db:
            return {model.__tablename__: sorted(row.model_dump_json() for row in
                (await db.scalars(select(model))).all()) for model in (
                    InferenceCostReservation, RepoRepairProposal, ApprovalRequest, WorkflowRunState)}

    before = await exact_rows()
    monkeypatch.setattr(recovery, "publish_original_repository_completion", real_publish)
    from src.work_board.repository import BoardError
    with pytest.raises((RepositorySourceRecoveryError, BoardError),
            match="accounting_changed|Original proposal accounting binding changed"):
        await real_publish(captured["service"], jobs, **kwargs)
    assert await exact_rows() == before
    async with jobs._session() as db:
        root = await jobs._fetch(db, kwargs["job_id"])
        assert jobs._repo_repair_reservation_state(root)["status"] == "held"
        assert _source_record_absent(root, registration["iteration_id"])


def _source_record_absent(root, iteration):
    from src.workflows import repo_repair_source as source
    return (source._repository_record(root, "repository:cleanup:" + iteration) is None
        and source._repository_record(root, "repository:readback:" + iteration) is None)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_cost", [False, True])
async def test_actual_unknown_orphan_reuses_original_metadata_after_real_due_auth_touch(
        accounting_db, monkeypatch, repository_admission_signer, changed_cost):
    import asyncio
    from datetime import datetime, timezone
    from src.auth import service as auth
    from src.db.models import OperatorSession, InferenceCostReservation
    from src.workflows import repo_repair_source as source
    from src.workflows import repo_repair_source_recovery as recovery
    from src.workflows.job_runtime import _as_utc
    real_publish, real_append = recovery.publish_original_repository_completion, source._append_repository_record
    captured = {}

    async def quarantine_actual_before_first_publication(service, jobs, **kwargs):
        captured.update(service=service, jobs=jobs, kwargs=kwargs)
        async with jobs._session() as db:
            root = await jobs._fetch(db, kwargs["job_id"])
            registered = read_registered_repository_producer(root, iteration_index=kwargs["iteration_index"])
            lease_owner, fence = root.lease_owner, root.fencing_token
        # Establish the original Stop intent through its actual typed owner
        # before the real original uncertainty writer records its successor.
        from src.workflows import repo_repair_stop as stop
        async with recovery._repository_recovery_fence(service, jobs,
                job_id=kwargs["job_id"], owner=kwargs["owner"]) as held:
            actual_stop = await stop._context(service, jobs, job_id=kwargs["job_id"], owner=kwargs["owner"])
            await stop._persist_repository_stop_intent_locked(service, jobs,
                context=actual_stop, owner=kwargs["owner"], reason="operator_cancelled", fence=held)
        await source._quarantine_original_uncertainty(service, jobs, job_id=kwargs["job_id"],
            owner=kwargs["owner"], lease_owner=lease_owner, fencing_token=fence,
            reason="repository_process_closure_unproven", result={"no_learning": True,
                "operator_action": "reconcile_original_process", "iteration_id": registered["iteration_id"]})
        async with jobs._session() as db:
            root = await jobs._fetch(db, kwargs["job_id"])
            assert root.status == "unknown_external_effect"
            captured["before_root"] = root.model_dump(mode="json")
        return await real_publish(service, jobs, **kwargs)

    def rollback_original_readback(root, identity, payload, **kwargs):
        if identity.startswith("repository:readback:"):
            raise RuntimeError("rollback_actual_unknown_original_readback")
        return real_append(root, identity, payload, **kwargs)

    monkeypatch.setattr(recovery, "publish_original_repository_completion", quarantine_actual_before_first_publication)
    monkeypatch.setattr(source, "_append_repository_record", rollback_original_readback)
    with pytest.raises(RuntimeError, match="rollback_actual_unknown_original_readback"):
        await _actual_source_callback_journey(accounting_db, monkeypatch, False, "test_python")
    jobs, kwargs, service = captured["jobs"], captured["kwargs"], captured["service"]
    async with jobs._session() as db:
        root = await jobs._fetch(db, kwargs["job_id"])
        assert root.model_dump(mode="json") == captured["before_root"]
        registration = read_registered_repository_producer(root, iteration_index=1)
        session = await db.get(OperatorSession, kwargs["owner"].session_id)
        old_auth = session.model_dump(mode="json")
        until_due = max(0, (auth._AUTH_TOUCH_INTERVAL - (datetime.now(timezone.utc)
            - _as_utc(session.last_seen_at))).total_seconds())
        assert until_due <= 30
        assert (_as_utc(root.deadline_at) - datetime.now(timezone.utc)).total_seconds() > until_due + 5
    path = service._workspace() / ("artifacts/repo-repair/model/iteration-" + registration["iteration_id"] + "-cleanup.json")
    original_bytes = path.read_bytes()
    original_envelope = json.loads(original_bytes)
    await asyncio.sleep(until_due + 0.02)
    operator = await auth.authenticate_session(kwargs["owner"].session_id, touch=True)
    assert operator.session_id == kwargs["owner"].session_id
    async with jobs._session() as db:
        session = await db.get(OperatorSession, kwargs["owner"].session_id)
        assert _as_utc(session.last_seen_at) > datetime.fromisoformat(old_auth["last_seen_at"]).replace(tzinfo=timezone.utc)
        assert session.model_dump(mode="json")["absolute_expires_at"] == old_auth["absolute_expires_at"]
        cost = await db.get(InferenceCostReservation, "remote:repo-work:" + registration["iteration_id"])
        if changed_cost:
            cost.actual_cost_microusd += 1
            await db.commit()
        current_cost = cost.model_dump(mode="json")
    monkeypatch.setattr(source, "_append_repository_record", real_append)
    monkeypatch.setattr(recovery, "publish_original_repository_completion", real_publish)
    if changed_cost:
        with pytest.raises(RepositorySourceRecoveryError, match="original_producer_accounting_changed"):
            await real_publish(service, jobs, **kwargs)
    else:
        witness = await real_publish(service, jobs, **kwargs)
        assert recovery.repository_completion_cleanup_envelope(witness) == original_envelope
    assert path.read_bytes() == original_bytes
    async with jobs._session() as db:
        root = await jobs._fetch(db, kwargs["job_id"])
        assert root.status == "unknown_external_effect"
        assert jobs._repo_repair_reservation_state(root)["status"] == "held"
        assert root.revision == captured["before_root"]["revision"] + (0 if changed_cost else 1)
        after = root.model_dump(mode="json")
        assert {key: value for key, value in after.items() if key not in {"revision", "checkpoint_receipts_json"}} == {
            key: value for key, value in captured["before_root"].items() if key not in {"revision", "checkpoint_receipts_json"}}
        assert (await db.get(InferenceCostReservation, "remote:repo-work:" + registration["iteration_id"])).model_dump(mode="json") == current_cost
        if changed_cost:
            assert root.model_dump(mode="json") == captured["before_root"]
        else:
            recovery._assert_source_completion_append_metadata(root, registration["iteration_id"],
                original_envelope["source_append_metadata"])
