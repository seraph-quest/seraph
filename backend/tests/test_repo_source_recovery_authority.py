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
    prefix = "artifacts/repo-repair/model/iteration-" + registration["iteration_id"]
    before_artifact = (captured["service"]._workspace() / (prefix + "-cleanup.json")).read_bytes()
    original_envelope = json.loads(before_artifact)
    assert set(original_envelope) == {"physical_projection", "source_completion_cas", "source_append_metadata"}
    assert len(original_envelope["source_append_metadata"]) == 2
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
