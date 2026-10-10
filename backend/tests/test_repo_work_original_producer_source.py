"""Actual original Source admissions and registered native producer journeys."""
import pytest

from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_repo_work_task_publication import _actual_source_callback_journey
from src.workflows.repo_repair_source import read_repository_inventory
from src.workflows.repo_repair_source_recovery import read_registered_repository_producer


@pytest.mark.asyncio
async def test_knownpost_dtos_deny_before_current_source_or_sql_reads(monkeypatch):
    from src.workflows import repo_repair_source as source
    from src.workflows import repo_repair_source_recovery as recovery

    class NoSql:
        async def _fetch(self, *args, **kwargs):
            raise AssertionError("Unregistered knownpost stage cannot read SQL")

    for stage in (object(), {"root_json": "{}", "post_revision": 2},
            recovery._RepositoryKnownPostCompletionStage()):
        with pytest.raises(recovery.RepositorySourceRecoveryError,
                match="original_repository_knownpost_stage_required"):
            await source.stage_repository_knownpost_context(object(), NoSql(),
                stage=stage, owner=object(), fence=object())
        with pytest.raises(recovery.RepositorySourceRecoveryError,
                match="original_repository_knownpost_stage_required"):
            await source._validate_repository_knownpost_context_sql(object(), object(), NoSql(),
                stage=stage, context={})


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["test_python", "test_node"])
async def test_actual_knownpost_context_requires_current_source_and_active_scope(
        accounting_db, monkeypatch, language, repository_admission_signer):
    import asyncio
    import copy
    import json
    from datetime import timedelta
    from sqlalchemy import text, update
    from src.db.models import WorkflowRunState
    from src.execution import repo_original_producer as physical
    from src.workflows import repo_repair_source as source
    from src.workflows import repo_repair_source_recovery as recovery
    from src.workflows import repo_repair_stop as stop
    from src.workflows.job_runtime import DurableJobLeaseError, _canonical
    captured, scopes = {}, []
    real_publish = recovery.publish_original_repository_completion
    real_stage = source.stage_repository_knownpost_context

    class OriginalCommitted(Exception):
        pass

    async def commit_actual_unknown(service, jobs, **kwargs):
        captured.update(service=service, jobs=jobs, kwargs=kwargs)
        async with jobs._session() as db:
            root = await jobs._fetch(db, kwargs["job_id"])
            registration = read_registered_repository_producer(root, iteration_index=1)
            lease_owner, fence = root.lease_owner, root.fencing_token
        async with recovery._repository_recovery_fence(service, jobs,
                job_id=kwargs["job_id"], owner=kwargs["owner"]) as held:
            context = await stop._context(service, jobs, job_id=kwargs["job_id"], owner=kwargs["owner"])
            await stop._persist_repository_stop_intent_locked(service, jobs, context=context,
                owner=kwargs["owner"], reason="operator_cancelled", fence=held)
        await source._quarantine_original_uncertainty(service, jobs, job_id=kwargs["job_id"],
            owner=kwargs["owner"], lease_owner=lease_owner, fencing_token=fence,
            reason="repository_process_closure_unproven", result={"no_learning": True,
                "operator_action": "reconcile_original_process", "iteration_id": registration["iteration_id"]})
        await real_publish(service, jobs, **kwargs)
        raise OriginalCommitted()

    async def inspect_actual_context(service, jobs, *, stage, owner, fence):
        binding = recovery.repository_knownpost_stage(stage)
        for altered in (copy.copy(stage), object(), dict(binding)):
            with pytest.raises(recovery.RepositorySourceRecoveryError):
                await real_stage(service, jobs, stage=altered, owner=owner, fence=fence)
        for fields in ({"service": object()}, {"jobs": object()}, {"fence": object()}):
            args = {"service": service, "jobs": jobs, "stage": stage, "owner": owner, "fence": fence}
            args.update(fields)
            with pytest.raises(recovery.RepositorySourceRecoveryError):
                await real_stage(**args)
        with pytest.raises(DurableJobLeaseError):
            await real_stage(service, jobs, stage=stage, owner=copy.copy(owner), fence=fence)

        async def inherited():
            with pytest.raises(recovery.RepositorySourceRecoveryError):
                await real_stage(service, jobs, stage=stage, owner=owner, fence=fence)
        await asyncio.create_task(inherited())
        def another_thread():
            with pytest.raises(recovery.RepositorySourceRecoveryError):
                recovery.assert_repository_knownpost_stage(stage, service=service, jobs=jobs, fence=fence)
        await asyncio.to_thread(another_thread)
        context = await real_stage(service, jobs, stage=stage, owner=owner, fence=fence)
        stop.assert_repository_stop_context(context, service=service, jobs=jobs)
        with pytest.raises(DurableJobLeaseError):
            stop.assert_repository_stop_context(copy.copy(context), service=service, jobs=jobs)

        def forbidden(*args, **kwargs):
            raise AssertionError("Knownpost SQL assertion cannot perform physical reads")
        async with jobs._session() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            with monkeypatch.context() as writer:
                writer.setattr(physical, "original_producer_completion_result", forbidden)
                writer.setattr(physical.os, "fstat", forbidden)
                writer.setattr(service, "_read_private_artifact", forbidden)
                assert await source._validate_repository_knownpost_context_sql(db, service, jobs,
                    stage=stage, context=context)
            await db.rollback()
        root = context["run"]
        history = json.loads(root.checkpoint_receipts_json)
        changed_time = json.loads(root.checkpoint_receipts_json)
        changed_time[-1]["created_at"] = "2000-01-01T00:00:00+00:00"
        mutations = [(root.id, {"revision": root.revision + 1}),
            (root.id, {"updated_at": root.updated_at + timedelta(microseconds=1)}),
            (root.id, {"failure_reason": "changed noncleanup Root field"}),
            (root.id, {"checkpoint_receipts_json": _canonical(history + [history[-1]])}),
            (root.id, {"checkpoint_receipts_json": _canonical(changed_time)}),
            (context["parent"].id, {"failure_reason": "changed nonRoot static field"})]
        for row_id, values in mutations:
            async with jobs._session() as db:
                await db.execute(text("BEGIN IMMEDIATE"))
                await db.execute(update(WorkflowRunState).where(WorkflowRunState.id == row_id).values(**values))
                with pytest.raises((DurableJobLeaseError, recovery.RepositorySourceRecoveryError)):
                    await source._validate_repository_knownpost_context_sql(db, service, jobs,
                        stage=stage, context=context)
                await db.rollback()
        scopes.append((stage, context, service, jobs))
        return context

    monkeypatch.setattr(recovery, "publish_original_repository_completion", commit_actual_unknown)
    with pytest.raises(OriginalCommitted):
        await _actual_source_callback_journey(accounting_db, monkeypatch, False, language)
    service, jobs, kwargs = captured["service"], captured["jobs"], captured["kwargs"]
    monkeypatch.setattr(recovery, "publish_original_repository_completion", real_publish)
    monkeypatch.setattr(source, "stage_repository_knownpost_context", inspect_actual_context)
    async with jobs._session() as db:
        before = (await jobs._fetch(db, kwargs["job_id"])).model_dump(mode="json")
    for _ in range(2):
        async with recovery.stage_repository_knownpost_completion(service, jobs,
                job_id=kwargs["job_id"], owner=kwargs["owner"], iteration_index=1,
                expected_job_revision=before["revision"]) as witness:
            assert recovery.repository_completion_outcome(witness)["cleanup_proven"] is True
        with pytest.raises(recovery.RepositorySourceRecoveryError):
            recovery.repository_completion_outcome(witness)
        stage, context, _, _ = scopes[-1]
        with pytest.raises(recovery.RepositorySourceRecoveryError):
            recovery.repository_knownpost_stage(stage)
        with pytest.raises(recovery.RepositorySourceRecoveryError):
            stop.assert_repository_stop_context(context, service=service, jobs=jobs)
    # Corrupt only the retained actual context's static map after its original
    # Source reader. The real final SQL validator must return False, and the
    # completion owner must consume that verdict before issuing any witness.
    false_verdicts = []
    real_sql = source._validate_repository_knownpost_context_sql

    async def changed_actual_static(service, jobs, *, stage, owner, fence):
        context = await real_stage(service, jobs, stage=stage, owner=owner, fence=fence)
        binding = recovery.repository_knownpost_stage(stage)
        context["static_rows"][binding["root_key"]] = "0" * 64
        return context

    async def observe_real_verdict(*args, **kwargs):
        verdict = await real_sql(*args, **kwargs)
        if verdict is False:
            false_verdicts.append(True)
        return verdict

    monkeypatch.setattr(source, "stage_repository_knownpost_context", changed_actual_static)
    monkeypatch.setattr(source, "_validate_repository_knownpost_context_sql", observe_real_verdict)
    with pytest.raises(recovery.RepositorySourceRecoveryError):
        async with recovery.stage_repository_knownpost_completion(service, jobs,
                job_id=kwargs["job_id"], owner=kwargs["owner"], iteration_index=1,
                expected_job_revision=before["revision"]):
            pytest.fail("A real False Source verdict cannot issue a completion witness")
    assert false_verdicts == [True]
    async with jobs._session() as db:
        assert (await jobs._fetch(db, kwargs["job_id"])).model_dump(mode="json") == before
    assert len(scopes) == 2


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
    from datetime import timedelta
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
            for fields in ({"revision": run.revision + 1},
                    {"updated_at": run.updated_at + timedelta(microseconds=1)},
                    {"failure_reason": "caller changed noncleanup Root field"}):
                changed_root = run.model_copy(update=fields)
                with pytest.raises(DurableJobTransitionError):
                    original_append(changed_root, identity, payload, inventory=inventory,
                        _completion_append=pending[0])
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
        if _completion_append is pending[1]:
            with pytest.raises(DurableJobTransitionError):
                original_append(run, "repository:physical-cleanup:v1", {"third_append": True},
                    inventory=inventory, _completion_append=pending[0])
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
        assert inventory["schema"] == "repository.checkpoint_inventory.v4"
        assert len(inventory["identities"]) == 49
        registration = read_registered_repository_producer(root, iteration_index=1)
        assert registration["schema"] == "repository.original_producer.v2"
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


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [1, 2, 3])
async def test_new_original_v4_inventory_keeps_exact_v3_vector_and_bounds(
        accounting_db, monkeypatch, repository_admission_signer, cap):
    import json
    from src.workflows import repo_repair_source as source
    from src.workflows.job_runtime import DurableJobLeaseError, _canonical, _digest
    from tests.test_repo_work_original_producer_startup import undispatched_original_source
    flow = await undispatched_original_source(accounting_db, monkeypatch, work_limits={
        "max_iterations": cap, "max_total_seconds": 900, "max_cost_usd": 0.000100})
    async with flow["factory"]() as db:
        root = await flow["jobs"]._fetch(db, flow["job_id"])
        work = source.read_repository_original(root)[1]
        inventory = read_repository_inventory(root)
        assert inventory["schema"] == "repository.checkpoint_inventory.v4"
        assert len(inventory["identities"]) == 10 + 13 * cap
        assert len(set(inventory["identities"])) == len(inventory["identities"])
        assert inventory["max_records"] == 50 and inventory["max_metadata_bytes_per_record"] == 16384
        assert not any("durability" in identity for identity in inventory["identities"])
        with pytest.raises(DurableJobLeaseError, match="sealed inventory cannot be upgraded"):
            source.repository_checkpoint_inventory(root, work, _admission_schema="repository.checkpoint_inventory.v3")
        # Pure historical grammar comparison only: detached local copies do not
        # mutate a Root, issue a witness, supply startup authority or upgrade evidence.
        local = root.model_copy(deep=True)
        history = json.loads(local.checkpoint_receipts_json)
        wrapper = next(item for item in history if item["checkpoint_id"] == "repository:inventory:v1")
        wrapper["payload"]["schema"] = "repository.checkpoint_inventory.v3"
        wrapper["state_digest"] = _digest(wrapper["payload"])
        local.checkpoint_receipts_json = _canonical(history)
        assert source.repository_checkpoint_inventory(local, work) == inventory["identities"]
        assert read_repository_inventory(local)["identities"] == inventory["identities"]


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["missing_schema", "unknown_schema", "untyped_schema", "duplicate",
    "subset", "superset", "foreign", "reordered", "bool_records", "bool_metadata", "extra_key"])
async def test_original_inventory_closed_v4_reader_denies_local_tamper_without_mutation(
        accounting_db, monkeypatch, repository_admission_signer, corruption):
    import json
    from src.workflows.job_runtime import DurableJobLeaseError, _canonical, _digest
    from tests.test_repo_work_original_producer_startup import undispatched_original_source
    flow = await undispatched_original_source(accounting_db, monkeypatch)
    async with flow["factory"]() as db:
        root = await flow["jobs"]._fetch(db, flow["job_id"])
        before = root.model_dump_json()
        local = root.model_copy(deep=True)
        history = json.loads(local.checkpoint_receipts_json)
        wrapper = next(item for item in history if item["checkpoint_id"] == "repository:inventory:v1")
        payload = wrapper["payload"]
        if corruption == "missing_schema": payload.pop("schema")
        elif corruption == "unknown_schema": payload["schema"] = "repository.checkpoint_inventory.v5"
        elif corruption == "untyped_schema": payload["schema"] = {}
        elif corruption == "duplicate": payload["identities"][-1] = payload["identities"][0]
        elif corruption == "subset": payload["identities"].pop()
        elif corruption == "superset": payload["identities"].append("repository:foreign")
        elif corruption == "foreign": payload["identities"][-1] = "repository:foreign"
        elif corruption == "reordered": payload["identities"].reverse()
        elif corruption == "bool_records": payload["max_records"] = True
        elif corruption == "bool_metadata": payload["max_metadata_bytes_per_record"] = True
        else: payload["caller_version"] = 4
        wrapper["state_digest"] = _digest(payload)
        local.checkpoint_receipts_json = _canonical(history)
        with pytest.raises(DurableJobLeaseError):
            read_repository_inventory(local)
        assert root.model_dump_json() == before
