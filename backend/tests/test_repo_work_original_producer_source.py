"""Actual original Source admissions and registered native producer journeys."""
import pytest

from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_repo_work_task_publication import _actual_source_callback_journey
from src.workflows.repo_repair_source import read_repository_inventory
from src.workflows.repo_repair_source_recovery import read_registered_repository_producer


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
