"""Actual original Source admissions and registered native producer journeys."""
import pytest

from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_repo_work_task_publication import _actual_source_callback_journey
from src.workflows.repo_repair_source import read_repository_inventory
from src.workflows.repo_repair_source_recovery import read_registered_repository_producer


@pytest.mark.asyncio
async def test_completion_append_dtos_deny_before_optional_stage_dependency(monkeypatch):
    from src.workflows import repo_repair_source as source
    from src.workflows import repo_repair_source_recovery as recovery
    from src.workflows.job_runtime import DurableJobLeaseError

    def forbidden(*args, **kwargs):
        raise AssertionError("Forged ticket must deny before optional stage dependency")

    monkeypatch.setattr(recovery, "assert_repository_completion_append_stage", forbidden, raising=False)
    for dto in (object(), {"checkpoint_id": "repository:cleanup:" + "0" * 64,
            "safe": True, "created_at": "2026-10-09T00:00:00+00:00"},
            source._RepositoryCompletionAppendPending()):
        with pytest.raises(DurableJobLeaseError):
            source._completion_append_state(dto)
        with pytest.raises(DurableJobLeaseError):
            source._append_repository_record(None, "repository:cleanup:" + "0" * 64,
                {}, inventory=[], _completion_append=dto)


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["test_python", "test_node"])
async def test_original_completion_pending_appends_are_exact_source_tickets(
        accounting_db, monkeypatch, language, repository_admission_signer):
    import asyncio
    import copy
    import json
    from contextlib import asynccontextmanager
    from src.workflows import repo_repair_source as source
    from src.workflows import repo_repair_source_recovery as recovery
    from src.workflows.job_runtime import DurableJobLeaseError, DurableJobTransitionError, _canonical
    observed = {}
    original_stage = source.stage_repository_completion_appends
    original_bind = source.bind_repository_completion_append_payloads
    original_append = source._append_repository_record

    @asynccontextmanager
    async def actual_stage(service, jobs, *, stage, owner, fence):
        async with original_stage(service, jobs, stage=stage, owner=owner, fence=fence) as pending:
            observed.update(service=service, jobs=jobs, stage=stage, owner=owner, fence=fence, pending=pending)
            metadata = source.repository_completion_append_metadata(pending,
                service=service, jobs=jobs, fence=fence)
            observed["metadata"] = tuple(dict(item) for item in metadata)
            with pytest.raises(TypeError):
                metadata[0]["created_at"] = "caller timestamp"
            with pytest.raises(DurableJobLeaseError):
                source.repository_completion_append_metadata((copy.copy(pending[0]), pending[1]),
                    service=service, jobs=jobs, fence=fence)
            with pytest.raises(DurableJobLeaseError):
                source.repository_completion_append_metadata(pending, service=service, jobs=jobs, fence=object())
            with pytest.raises(DurableJobLeaseError):
                source.repository_completion_append_metadata(pending, service=object(), jobs=jobs, fence=fence)
            with pytest.raises(DurableJobLeaseError):
                source.repository_completion_append_metadata(pending, service=service, jobs=object(), fence=fence)
            with pytest.raises(recovery.RepositorySourceRecoveryError):
                async with original_stage(service, jobs, stage=copy.copy(stage), owner=owner, fence=fence):
                    pytest.fail("Copied stage cannot issue append tickets")
            with pytest.raises(TypeError):
                async with original_stage(service, jobs, stage=stage, owner=owner, fence=fence,
                        created_at=observed["metadata"][0]["created_at"]):
                    pytest.fail("Caller cannot select constructor time")
            with pytest.raises(DurableJobLeaseError):
                async with original_stage(service, jobs, stage=stage, owner=owner, fence=fence):
                    pytest.fail("A stage cannot issue another constructor epoch")

            async def inherited():
                with pytest.raises(DurableJobLeaseError):
                    source.repository_completion_append_metadata(pending, service=service, jobs=jobs, fence=fence)
            await asyncio.create_task(inherited())

            def another_thread():
                with pytest.raises(DurableJobLeaseError):
                    source.repository_completion_append_metadata(pending, service=service, jobs=jobs, fence=fence)
            await asyncio.to_thread(another_thread)
            original_write = service._write_private_artifact

            def actual_write(ref, raw):
                if ref.endswith("-cleanup.json"):
                    envelope = json.loads(raw)
                    assert envelope["source_append_metadata"] == list(observed["metadata"])
                    observed["metadata_before_file"] = True
                return original_write(ref, raw)
            with monkeypatch.context() as files:
                files.setattr(service, "_write_private_artifact", actual_write)
                yield pending

    def actual_bind(stage, pending, *, service, jobs, fence):
        with pytest.raises(DurableJobLeaseError):
            original_bind(object(), pending, service=service, jobs=jobs, fence=fence)
        payloads = original_bind(stage, pending, service=service, jobs=jobs, fence=fence)
        observed["payloads"] = payloads
        with pytest.raises(DurableJobLeaseError):
            original_bind(stage, pending, service=service, jobs=jobs, fence=fence)
        return payloads

    def actual_append(run, identity, payload, *, inventory, _completion_append=None):
        if _completion_append is None:
            return original_append(run, identity, payload, inventory=inventory)
        pending = observed["pending"]
        if _completion_append is pending[0]:
            with pytest.raises(DurableJobTransitionError):
                original_append(run, observed["metadata"][1]["checkpoint_id"], observed["payloads"][1],
                    inventory=inventory, _completion_append=pending[1])
            with pytest.raises(DurableJobTransitionError):
                original_append(run, identity, {**payload, "status": "caller selected"},
                    inventory=inventory, _completion_append=pending[0])
            with pytest.raises(DurableJobLeaseError):
                original_append(run, identity, payload, inventory=inventory, _completion_append=copy.copy(pending[0]))
            foreign = run.model_copy(update={"run_identity": "foreign-original-root"})
            with pytest.raises(DurableJobTransitionError):
                original_append(foreign, identity, payload, inventory=inventory, _completion_append=pending[0])
            changed_prefix = run.model_copy(update={"checkpoint_receipts_json": _canonical([])})
            with pytest.raises(DurableJobTransitionError):
                original_append(changed_prefix, identity, payload, inventory=inventory, _completion_append=pending[0])
            with pytest.raises(DurableJobTransitionError):
                original_append(run, identity, payload, inventory=list(reversed(inventory)), _completion_append=pending[0])
        from src.execution import repo_original_producer as physical
        def forbidden(*args, **kwargs):
            raise AssertionError("Append SQL assertion cannot perform physical reads")
        with monkeypatch.context() as writer:
            writer.setattr(physical, "original_producer_completion_result", forbidden)
            writer.setattr(physical.os, "fstat", forbidden)
            writer.setattr(observed["service"], "_read_private_artifact", forbidden)
            changed = original_append(run, identity, payload, inventory=inventory, _completion_append=_completion_append)
        with pytest.raises(DurableJobTransitionError):
            original_append(run, identity, payload, inventory=inventory, _completion_append=_completion_append)
        return changed

    monkeypatch.setattr(source, "stage_repository_completion_appends", actual_stage)
    monkeypatch.setattr(source, "bind_repository_completion_append_payloads", actual_bind)
    monkeypatch.setattr(source, "_append_repository_record", actual_append)
    flow = await _actual_source_callback_journey(accounting_db, monkeypatch, False, language)
    assert observed["metadata_before_file"] is True
    with pytest.raises(DurableJobLeaseError):
        source.repository_completion_append_metadata(observed["pending"], service=observed["service"],
            jobs=observed["jobs"], fence=observed["fence"])
    async with flow["factory"]() as db:
        root = await flow["jobs"]._fetch(db, flow["root_id"])
        journal = json.loads(root.checkpoint_receipts_json)
        for metadata, payload in zip(observed["metadata"], observed["payloads"]):
            wrapper = next(item for item in journal if item["checkpoint_id"] == metadata["checkpoint_id"])
            assert {key: wrapper[key] for key in metadata} == metadata
            assert wrapper["payload"] == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["test_python", "test_node"])
async def test_original_stop_holds_actual_scope_through_writer_and_revokes(
        accounting_db, monkeypatch, language, repository_admission_signer):
    import asyncio
    import copy
    from contextlib import asynccontextmanager
    from src.execution import repo_original_producer as producer
    from src.work_board.contracts import TaskLimits
    from src.workflows import repo_repair_source as source, repo_repair_stop as stop
    from src.workflows.repo_repair_source_recovery import (
        assert_repository_original_stop_completion, RepositorySourceRecoveryError)
    observed = {}
    original_scope = source.stage_repository_stop_original_producer_witnesses
    original_positive = stop._positive_witness

    async def positive(*args, **kwargs):
        try:
            return await original_positive(*args, **kwargs)
        except Exception as error:
            observed["denial"] = (type(error).__name__, str(error))
            raise

    @asynccontextmanager
    async def actual_scope(service, jobs, *, context, fence):
        try:
            async with original_scope(service, jobs, context=context, fence=fence) as witness:
                assert witness is not None
                observed.update(witness=witness, service=service, jobs=jobs, context=context, fence=fence)
                assert_repository_original_stop_completion(witness, service=service,
                    jobs=jobs, context=context, fence=fence)
                with pytest.raises(RepositorySourceRecoveryError):
                    assert_repository_original_stop_completion(copy.copy(witness), service=service, jobs=jobs)

                async def inherited_child():
                    with pytest.raises(RepositorySourceRecoveryError):
                        assert_repository_original_stop_completion(witness, service=service, jobs=jobs)
                await asyncio.create_task(inherited_child())
                original_cancel = jobs.cancel_general_task_native_parent

                def forbidden(*args, **kwargs):
                    raise AssertionError("Terminal writer cannot inspect physical producer descriptors or artifacts")

                async def actual_cancel(*args, **kwargs):
                    with monkeypatch.context() as writer:
                        writer.setattr(producer, "original_producer_completion_result", forbidden)
                        writer.setattr(producer.os, "fstat", forbidden)
                        writer.setattr(service, "_read_private_artifact", forbidden)
                        assert_repository_original_stop_completion(witness, service=service,
                            jobs=jobs, context=context, fence=fence)
                        observed["writer_entered"] = True
                        return await original_cancel(*args, **kwargs)

                with monkeypatch.context() as actual:
                    actual.setattr(jobs, "cancel_general_task_native_parent", actual_cancel)
                    yield witness
        except Exception as error:
            observed["denial"] = (type(error).__name__, str(error))
            raise

    monkeypatch.setattr(source, "stage_repository_stop_original_producer_witnesses", actual_scope)
    monkeypatch.setattr(stop, "_positive_witness", positive)
    try:
        await _actual_source_callback_journey(accounting_db, monkeypatch, True, language,
            stop_at="cost_exhausted", task_limits=TaskLimits(max_inference_calls=5,
                max_cost_microusd=500, wall_seconds=900),
            work_limits={"max_iterations": 3, "max_total_seconds": 900, "max_cost_usd": 0.000100},
            actual_model_cost_microusd=100)
    except AssertionError:
        pytest.fail("Actual Source Stop denial: " + repr(observed.get("denial")))
    assert observed["writer_entered"] is True
    with pytest.raises(RepositorySourceRecoveryError):
        assert_repository_original_stop_completion(observed["witness"], service=observed["service"],
            jobs=observed["jobs"], context=observed["context"], fence=observed["fence"])


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["test_python", "test_node"])
async def test_original_source_registers_actual_producer_before_native_command(
        accounting_db, monkeypatch, language, repository_admission_signer):
    flow = await _actual_source_callback_journey(accounting_db, monkeypatch, False, language)
    async with flow["factory"]() as db:
        root = await flow["jobs"]._fetch(db, flow["root_id"])
        inventory = read_repository_inventory(root)
        assert inventory["schema"] == "repository.checkpoint_inventory.v3"
        assert len(inventory["identities"]) == 49
        registration = read_registered_repository_producer(root, iteration_index=1)
        assert registration["ready"]["pid"] > 0
        assert registration["ready"]["public_key"]


@pytest.mark.asyncio
async def test_selected_original_producer_prerequisites_block_new_root_visibly(
        accounting_db, monkeypatch, repository_admission_signer):
    from sqlalchemy import select
    from src.auth.service import authenticate_session
    from src.db.models import WorkflowRunState
    from src.workflows.repo_repair import RepoRepairError
    from src.workflows.repo_repair_source import prepare_repository_native_source
    from src.execution import repo_supervisor
    from tests.test_repo_work_task_publication import actual_native_source
    factory, owner, service, jobs, binding, _ = await actual_native_source(
        accounting_db, monkeypatch, goal_capacity=2, claim_child=False)
    operator = await authenticate_session(owner.session_id, touch=False)

    def unsupported_host():
        raise ValueError("native supervision unavailable")

    monkeypatch.setattr(repo_supervisor, "platform_ready", unsupported_host)
    with pytest.raises(RepoRepairError) as failure:
        await prepare_repository_native_source(service, jobs, binding,
            child_owner="selected-original-mode-worker", principal=operator.principal)
    assert failure.value.code == "repository_original_producer_prerequisites_blocked"
    assert failure.value.status_code == 503
    async with factory() as db:
        assert list((await db.scalars(select(WorkflowRunState).where(
            WorkflowRunState.job_kind == "engineering.repo-repair.v1"))).all()) == []
