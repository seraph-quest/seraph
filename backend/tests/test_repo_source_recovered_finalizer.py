"""Genuine signed ownerless completion consumers; not a SIGKILL/API receipt.

Prepared under R35/R38. Execution requires the integrated Root/critic seal.
The only disposable-owner operation closes the actual fixture lane; it never
changes canonical capacity, creates a signer, or seeds a callback/result.
"""
import asyncio
import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import event, select, update

from src.workflows import repo_repair_source as source
from src.workflows import repo_repair_source_recovery as recovery
from src.workflows.job_runtime import DurableJobLeaseError, _canonical
from src.workflows.repo_repair import RepoRepairError, RepoRepairService
from src.work_board.repository import BoardError
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.test_repo_work_task_publication import _actual_source_callback_journey


DENIED = (recovery.RepositorySourceRecoveryError, DurableJobLeaseError,
          RepoRepairError, BoardError, ValueError, TypeError)


async def _rows(jobs):
    """Literal private-DB snapshots, never passed back as authority."""
    from sqlmodel import SQLModel
    async with jobs._session() as db:
        connection = await db.connection()
        snapshots = {}
        for table in SQLModel.metadata.sorted_tables:
            rows = await connection.exec_driver_sql('SELECT * FROM "' + table.name + '"')
            snapshots[table.name] = sorted(repr(tuple(row)) for row in rows)
        return snapshots


async def _signed_ownerless_fixture(accounting_db, monkeypatch, language, *, failed=False,
                                    unknown=False):
    from src.execution import repo_original_producer as producer
    from src.workflows import general_task_guard as guard
    from src.workflows import repo_repair_stop as stop
    captured = {}

    class OriginalSignedBeforePublication(Exception):
        pass

    async def capture(service, jobs, **kwargs):
        captured.update(original=service, jobs=jobs, kwargs=kwargs)
        producer.assert_original_producer_live_completion(
            kwargs["producer_owner"], kwargs["actual_result"])
        async with jobs._session() as db:
            root = await jobs._fetch(db, kwargs["job_id"])
            registered = recovery.read_registered_repository_producer(root, iteration_index=1)
            lease_owner, fence = root.lease_owner, root.fencing_token
            captured["registration"] = registered
        callback = service._iterative_process_callbacks[registered["iteration_id"]]
        assert callback.done() and callback.result() is kwargs["actual_result"]
        if unknown:
            async with recovery._repository_recovery_fence(service, jobs,
                    job_id=kwargs["job_id"], owner=kwargs["owner"]) as held:
                context = await stop._context(service, jobs,
                    job_id=kwargs["job_id"], owner=kwargs["owner"])
                await stop._persist_repository_stop_intent_locked(service, jobs, context=context,
                    owner=kwargs["owner"], reason="operator_cancelled", fence=held)
            await source._quarantine_original_uncertainty(service, jobs,
                job_id=kwargs["job_id"], owner=kwargs["owner"], lease_owner=lease_owner,
                fencing_token=fence, reason="repository_process_closure_unproven",
                result={"no_learning": True, "operator_action": "reconcile_original_process",
                        "iteration_id": registered["iteration_id"]})
        raise OriginalSignedBeforePublication()

    with monkeypatch.context() as patch:
        patch.setattr(recovery, "publish_original_repository_completion", capture)
        with pytest.raises(OriginalSignedBeforePublication):
            await _actual_source_callback_journey(accounting_db, patch, failed, language)
    jobs, original, kwargs = captured["jobs"], captured["original"], captured["kwargs"]
    registered = captured["registration"]
    before = await _rows(jobs)
    async with jobs._session() as db:
        root = await jobs._fetch(db, kwargs["job_id"])
        assert root.status == ("unknown_external_effect" if unknown else "running")
        assert jobs._repo_repair_reservation_state(root)["status"] == "held"
        assert source._repository_record(root, "repository:cleanup:" + registered["iteration_id"]) is None
    # Simulated disposal of the original fixture backend's actual lane only.
    # No canonical Stop/capacity/lease mutation and no fake Popen/ready object.
    original._iterative_lanes.pop(kwargs["job_id"]).clear_quarantine()
    assert await _rows(jobs) == before
    cold = RepoRepairService(session_factory=accounting_db[2], jobs=jobs)
    assert cold._workspace() == original._workspace()
    assert not cold._iterative_lanes and not cold._iterative_process_callbacks
    captured.update(service=cold, registration=registered)
    return captured


async def _scope(flow):
    """Read the actual current revision; no caller reconstruction of Source."""
    async with flow["jobs"]._session() as db:
        root = await flow["jobs"]._fetch(db, flow["kwargs"]["job_id"])
        revision = root.revision
    return recovery.stage_original_repository_completion_publication(
        flow["service"], flow["jobs"], job_id=flow["kwargs"]["job_id"],
        owner=flow["kwargs"]["owner"], iteration_index=1, expected_job_revision=revision)


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["test_python", "test_node"])
async def test_signed_ownerless_completion_uses_original_factories_and_writers(
        accounting_db, monkeypatch, repository_admission_signer, language):
    from src.execution import repo_original_producer as producer
    from src.execution import repo_sandbox
    from src.workflows import general_task_guard as guard
    from src.work_board import general_task_runtime_artifacts
    from src.db.models import InferenceCostReservation, WorkBoardEvent
    from src.model_fabric.effective_policy import configuration_mutation_lock
    import os
    flow = await _signed_ownerless_fixture(accounting_db, monkeypatch, language)
    jobs, service, kwargs = flow["jobs"], flow["service"], flow["kwargs"]
    actual_result = kwargs["actual_result"]
    assert actual_result["status"] == "succeeded"
    assert actual_result["original_producer_completion"]["outcome"] == "completed_requested_checks"
    assert source._repository_command_results(actual_result["manifest"],
        node=language == "test_node") == [{"check": "test", "status": "succeeded", "exit_code": 0}]
    original_path = "calculator.js" if language == "test_node" else "calculator.py"
    original_bytes = (service._workspace() / "example" / original_path).read_bytes()
    objects, kinds, transaction = {}, [], {"immediate": False}
    engine = accounting_db[1].sync_engine

    def before_sql(connection, cursor, statement, parameters, context, executemany):
        if statement.strip().upper().startswith("BEGIN IMMEDIATE"):
            transaction["immediate"] = True

    def end_sql(connection):
        transaction["immediate"] = False

    event.listen(engine, "before_cursor_execute", before_sql)
    event.listen(engine, "commit", end_sql)
    event.listen(engine, "rollback", end_sql)
    try:
        with monkeypatch.context() as patch:
            physical_helpers = (
                (producer, "original_producer_completion_result"),
                (producer, "_current_native_host_binding"),
                (producer, "_assert_original_pid_absent"),
                (producer, "_read_registered_bundle"),
                (producer, "read_file"), (producer, "verify_completion"),
                (repo_sandbox, "load_persisted_repo_sandbox_settings"),
                (repo_sandbox, "_open_trusted_directory"),
                (general_task_runtime_artifacts, "verify_staged_task_artifact"),
                (service, "_read_private_artifact"),
                (service, "_write_private_artifact"), (service, "_workspace"),
                *((os, name) for name in ("open", "fstat", "stat", "lstat", "readlink")),
                *((Path, name) for name in ("open", "read_bytes", "read_text", "write_bytes",
                    "write_text", "resolve", "stat", "lstat", "is_file", "is_dir")),
            )
            for target, name in physical_helpers:
                real = getattr(target, name)
                def outside_sql(*args, _real=real, _name=name, **kw):
                    assert not transaction["immediate"], "physical helper inside IMMEDIATE: " + _name
                    return _real(*args, **kw)
                patch.setattr(target, name, outside_sql)
            real_gate = source.verify_recovered_repository_final_writer

            async def gate(jobs, db, run, **kw):
                assert transaction["immediate"]
                kinds.append(kw["kind"])
                with pytest.raises(DurableJobLeaseError, match="pending original write"):
                    await real_gate(jobs, db, run, **{**kw, "kind": "not_original_phase"})
                return await real_gate(jobs, db, run, **kw)
            patch.setattr(source, "verify_recovered_repository_final_writer", gate)
            real_wait = recovery.repository_completion_recovered_wait
            real_source = recovery.repository_completion_recovered_final_source
            real_final = recovery.repository_completion_recovered_child_final

            async def wait(witness):
                actual = await real_wait(witness)
                objects["wait"] = actual
                with pytest.raises(recovery.RepositorySourceRecoveryError, match="phase_changed"):
                    await real_wait(witness)
                return actual

            async def canonical(witness):
                actual = await real_source(witness)
                objects["source"] = actual
                source.assert_repository_canonical_source(actual)
                for bad in (replace(actual), replace(actual, _recovered_completion=None)):
                    with pytest.raises(DENIED):
                        source.assert_repository_canonical_source(bad)
                with pytest.raises(recovery.RepositorySourceRecoveryError, match="phase_changed"):
                    await real_source(witness)
                return actual

            async def child(witness, *, evidence):
                actual = await real_final(witness, evidence=evidence)
                objects["final"] = actual
                assert actual.wait_witness is objects["wait"]
                assert actual._source_binding._final_source is objects["source"]
                with pytest.raises(DENIED):
                    recovery.assert_repository_completion_final_source(objects["source"], replace(actual))
                with pytest.raises(recovery.RepositorySourceRecoveryError, match="phase_changed"):
                    await real_final(witness, evidence=evidence)
                return actual
            patch.setattr(recovery, "repository_completion_recovered_wait", wait)
            patch.setattr(recovery, "repository_completion_recovered_final_source", canonical)
            patch.setattr(recovery, "repository_completion_recovered_child_final", child)
            real_publish = guard.publish_repository_child_final
            publication_denials = []

            async def publication(jobs, parent_id, **kw):
                before = await _rows(jobs)
                # A copied token mismatches the actual final Source identity.
                # Neither denial may change any child, parent or accounting row.
                for bad in (None, copy.copy(kw["_repository_completion_witness"])):
                    with pytest.raises(DENIED):
                        await real_publish(jobs, parent_id,
                            **{**kw, "_repository_completion_witness": bad})
                    assert await _rows(jobs) == before
                    publication_denials.append(bad is None)
                return await real_publish(jobs, parent_id, **kw)
            patch.setattr(guard, "publish_repository_child_final", publication)
            async with await _scope(flow) as completion:
                assert configuration_mutation_lock.locked()
                assert "iteration_cleanup_witness" not in recovery.repository_completion_result(completion)
                for bad in (copy.copy(completion), {}, recovery._OriginalRepositoryProducerCompletionWitness()):
                    with pytest.raises(DENIED):
                        recovery.assert_repository_scoped_completion(bad, service=service, jobs=jobs,
                            job_id=kwargs["job_id"], owner=kwargs["owner"])
                async def inherited():
                    with pytest.raises(DENIED):
                        recovery.assert_repository_scoped_completion(completion, service=service, jobs=jobs,
                            job_id=kwargs["job_id"], owner=kwargs["owner"])
                await asyncio.create_task(inherited())
                for changed in ({"service": flow["original"]}, {"jobs": object()},
                                {"owner": copy.copy(kwargs["owner"])}, {"job_id": "wrong-root"}):
                    inputs = dict(service=service, jobs=jobs, job_id=kwargs["job_id"], owner=kwargs["owner"])
                    with pytest.raises(DENIED):
                        recovery.assert_repository_scoped_completion(completion, **{**inputs, **changed})
                def another_thread():
                    with pytest.raises((*DENIED, RuntimeError)):
                        recovery.assert_repository_scoped_completion(completion, service=service, jobs=jobs,
                            job_id=kwargs["job_id"], owner=kwargs["owner"])
                await asyncio.to_thread(another_thread)
                with pytest.raises(recovery.RepositorySourceRecoveryError, match="phase_changed"):
                    await real_source(completion)
                result = await source.finalize_recovered_repository_iteration(service, jobs,
                    job_id=kwargs["job_id"], owner=kwargs["owner"], iteration_index=1,
                    completion_witness=completion)
                assert result["child"]["status"] == "succeeded"
                assert result["child"]["result"]["digest"] == objects["final"].final_artifact_digest
                assert result["receipt"]["status"] == "verified"
                assert result["receipt"]["contact_state"] == "settled"
                assert result["receipt"]["no_learning"] is True
                assert publication_denials == [True, False]
                assert kinds == ["checkpoint", "artifact", "readback", "readback", "publish",
                    "artifact", "artifact", "artifact", "artifact", "readback", "terminal"]
                async with jobs._session() as db:
                    root = await jobs._fetch(db, kwargs["job_id"])
                    assert root.status == "succeeded"
                    assert jobs._repo_repair_reservation_state(root)["status"] == "released"
                    terminal = source._repository_record(root, "repository:terminal:v1")
                    assert terminal["no_learning"] is True
                    assert terminal["original_child_id"] == objects["wait"].native_binding.invocation_id
                    identity = flow["registration"]["iteration_id"]
                    readback = source._repository_record(root, "repository:readback:" + identity)
                    manifest = service._read_private_artifact(readback["artifact_ref"],
                        expected_digest=readback["artifact_digest"])
                    assert manifest == actual_result["outputs"]["readback.json"]
                    assert json.loads(manifest) == actual_result["manifest"]
                    for name, record in terminal["publication_artifacts"].items():
                        literal = service._read_private_artifact("workspace-json:" + record["path"],
                            expected_digest=record["sha256"])
                        assert hashlib.sha256(literal).hexdigest() == record["sha256"]
                        if name in {"readback.json", "diff.patch"}:
                            assert literal == actual_result["outputs"][name]
                    costs = list((await db.scalars(select(InferenceCostReservation))).all())
                    assert len(costs) == 1
                    assert costs[0].job_id == kwargs["job_id"]
                    assert costs[0].state == "settled" and costs[0].contact_started_at is not None
                    assert costs[0].actual_cost_microusd == 0
                    original = source.read_repository_original(root)[0]
                    events = list((await db.scalars(select(WorkBoardEvent).where(
                        WorkBoardEvent.task_id == original["repository_task_id"],
                        WorkBoardEvent.kind == "attempt.repository_verified"))).all())
                    assert len(events) == 1
                    terminal_event = events[0]
                    assert terminal_event.task_id == original["repository_task_id"]
                    assert terminal_event.kind == "attempt.repository_verified"
                    assert (terminal_event.owner_principal_id, terminal_event.owner_session_id,
                        terminal_event.actor_principal_id, terminal_event.actor_session_id) == (
                            kwargs["owner"].principal_id, kwargs["owner"].session_id,
                            kwargs["owner"].principal_id, kwargs["owner"].session_id)
                    assert json.loads(terminal_event.metadata_json) == {
                        "attempt_id": original["repository_attempt_id"],
                        "workflow_run_id": kwargs["job_id"],
                        "readback_id": "repository-final:" + identity, "no_learning": True}
                    assert terminal_event.mutation_idempotency_key is None
                    assert terminal_event.mutation_request_digest is None
                    assert type(terminal_event.event_id) is int and terminal_event.event_id > 0
                    assert terminal_event.created_at is not None
                    state = recovery.repository_completion_finalizer_state(completion)["state"]
                    assert state["phase"] == 11 and state["pending"] is None
                    captured_events = {key: raw for (model, key), raw in state["rows"].items()
                        if model is WorkBoardEvent}
                    assert captured_events == {
                        terminal_event.event_id: _canonical(terminal_event.model_dump(mode="json"))}
                assert result["repository_root"]["status"] == "succeeded"
                assert any(item.get("readback_id") == "repository-final:" + identity
                    and item["status"] == "succeeded" and item["details"]["verified"] is True
                    for item in result["repository_root"]["effects"])
                assert (service._workspace() / "example" / original_path).read_bytes() == original_bytes
                assert configuration_mutation_lock.locked()
            with pytest.raises(DENIED):
                recovery.assert_repository_completion_final_source(objects["source"], objects["final"])
            with pytest.raises(DENIED):
                recovery.repository_completion_scoped_binding(completion)
    finally:
        event.remove(engine, "before_cursor_execute", before_sql)
        event.remove(engine, "commit", end_sql)
        event.remove(engine, "rollback", end_sql)


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["test_python", "test_node"])
@pytest.mark.parametrize("negative", ["nonzero_requested_check", "unknown_root"])
async def test_signed_completion_cannot_turn_failed_checks_or_unknown_into_success(
        accounting_db, monkeypatch, repository_admission_signer, language, negative):
    flow = await _signed_ownerless_fixture(accounting_db, monkeypatch, language,
        failed=negative == "nonzero_requested_check", unknown=negative == "unknown_root")
    async with await _scope(flow) as completion:
        before = await _rows(flow["jobs"])
        with pytest.raises(DENIED):
            await source.finalize_recovered_repository_iteration(flow["service"], flow["jobs"],
                job_id=flow["kwargs"]["job_id"], owner=flow["kwargs"]["owner"],
                iteration_index=1, completion_witness=completion)
        assert await _rows(flow["jobs"]) == before
    async with flow["jobs"]._session() as db:
        root = await flow["jobs"]._fetch(db, flow["kwargs"]["job_id"])
        assert root.status == ("unknown_external_effect" if negative == "unknown_root" else "running")
        assert flow["jobs"]._repo_repair_reservation_state(root)["status"] == "held"


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["test_python", "test_node"])
async def test_original_writer_rechecks_actual_root_inside_immediate(
        accounting_db, monkeypatch, repository_admission_signer, language):
    from src.db.models import WorkflowRunState
    flow = await _signed_ownerless_fixture(accounting_db, monkeypatch, language)
    real_gate = source.verify_recovered_repository_final_writer
    reached = []

    async def changed_root(jobs, db, run, **kw):
        reached.append(kw["kind"])
        await db.execute(update(WorkflowRunState).where(
            WorkflowRunState.run_identity == flow["kwargs"]["job_id"]).values(
                priority=WorkflowRunState.priority + 1))
        return await real_gate(jobs, db, run, **kw)

    async with await _scope(flow) as completion:
        before = await _rows(flow["jobs"])
        with monkeypatch.context() as patch:
            patch.setattr(source, "verify_recovered_repository_final_writer", changed_root)
            with pytest.raises(DENIED):
                await source.finalize_recovered_repository_iteration(flow["service"], flow["jobs"],
                    job_id=flow["kwargs"]["job_id"], owner=flow["kwargs"]["owner"],
                    iteration_index=1, completion_witness=completion)
        assert reached == ["checkpoint"]
        assert await _rows(flow["jobs"]) == before
