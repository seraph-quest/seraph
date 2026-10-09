"""Actual original Source admissions and registered native producer journeys."""
import pytest

from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_repo_work_task_publication import _actual_source_callback_journey
from src.workflows.repo_repair_source import read_repository_inventory
from src.workflows.repo_repair_source_recovery import read_registered_repository_producer


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
