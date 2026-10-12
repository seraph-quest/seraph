"""Original artifact transactions retain one frame, never a stale writer."""
import os
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth
from tests.test_browser_task_runtime import _input
from src.db.engine import get_session as canonical_session, override_session_factory
from src.db.models import WorkBoardInputArtifact
from src.goals.contracts import GoalAdmissionBudget
from src.goals.repository import GoalRepository
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError, MAX_BYTES
from src.runtime_plugins.ownership import begin_native_writer, initialize_fresh_deployment, DOMAINS
from src.work_board.contracts import WorkBoardOwner, WorkBoardInputArtifactCreate
from src.work_board.repository import BoardError
from src.work_board.input_artifacts import prepare_input_artifact, _begin_immediate


@pytest_asyncio.fixture
async def original_artifact_owner(accounting_db, real_auth, monkeypatch):
    from src.db import engine as original
    from src.auth.service import create_session
    from src.runtime_plugins.composition import PACKAGE_ROOT, reviewed_composition
    from src.workspace.production import ProductionWorkspace, maintenance_fence
    root, engine, factory = accounting_db
    monkeypatch.setattr(original, "engine", engine)
    monkeypatch.setattr(original, "_db_path", str(root / "seraph.db"))
    monkeypatch.setattr(original, "async_session_factory", factory)
    monkeypatch.setattr("src.auth.service.get_session", canonical_session)
    monkeypatch.setattr("src.goals.repository.get_session", canonical_session)
    with override_session_factory(factory):
        await original.init_db()
        reviewed = reviewed_composition(node_path=Path(os.environ["SERAPH_CORDIS_TEST_NODE"]),
            root=Path(os.environ.get("SERAPH_CORDIS_TEST_ROOT", str(PACKAGE_ROOT))))
        def original_stock(*, node_path=None):
            return reviewed_composition(node_path=node_path,
                root=Path(os.environ.get("SERAPH_CORDIS_TEST_ROOT", str(PACKAGE_ROOT))))
        monkeypatch.setattr("src.runtime_plugins.bridge.reviewed_composition", original_stock)
        with maintenance_fence(ProductionWorkspace(host_root=root)):
            async with canonical_session() as db:
                await begin_native_writer(db, owner="composition_maintenance", fresh=True)
                await initialize_fresh_deployment(db, composition_digests={
                    domain: reviewed.composition_digest for domain in DOMAINS})
        _, operator = await create_session()
        owner = WorkBoardOwner(principal_id=operator.principal.principal_id,
            session_id=operator.session_id)
        goal = await GoalRepository().create(title="Bounded original input transaction",
            owner_principal_id=owner.principal_id, owner_session_id=owner.session_id,
            admission_budget=GoalAdmissionBudget(reviewed_grant=True, grant_id="review-914",
                max_outstanding_jobs=1, max_attempts=2, max_runtime_seconds=300))
        request = WorkBoardInputArtifactCreate(schema_version=1, capability_id="browser.public-task.v1",
            goal_id=goal.id, goal_revision=goal.revision, input=_input(),
            idempotency_key="original-artifact-epoch")
        yield owner, request, factory, operator


@pytest.mark.asyncio
async def test_reservation_and_finalization_keep_original_frame(original_artifact_owner):
    owner, request, _, _operator = original_artifact_owner
    budget = HeaderReadBudget()
    async with canonical_session(header_budget=budget) as db:
        await begin_native_writer(db, owner="native_ingress", header_budget=budget)
        first = db.info["composition_guard"]
        remaining = budget.remaining
        artifact = await prepare_input_artifact(db, owner, request)
        assert artifact.revision >= 2 and artifact.typed_input_digest
        assert first._retention_closed and first._retention_writer_snapshot is None
        assert db.info["composition_read_guard"].header_budget is budget
        assert budget.remaining < remaining
        await begin_native_writer(db, owner="native_ingress", header_budget=budget)
        current = db.info["composition_guard"]
        assert current is not first and current.header_budget is budget
        assert current._retention_writer_snapshot[4] is budget
        assert await db.scalar(select(func.count()).select_from(WorkBoardInputArtifact)) == 1


@pytest.mark.asyncio
async def test_recertification_exhaustion_keeps_rows_absent(original_artifact_owner):
    owner, request, factory, _operator = original_artifact_owner
    budget = HeaderReadBudget()
    with pytest.raises(HeaderBoundsError):
        async with canonical_session(header_budget=budget) as db:
            budget.debit(budget.remaining, appearance=("test-original-frame-exhaustion",))
            await prepare_input_artifact(db, owner, request)
    assert budget.remaining == 0 and MAX_BYTES == 1048576
    async with factory.accounting_sessions() as db:
        assert await db.scalar(select(func.count()).select_from(WorkBoardInputArtifact)) == 0


@pytest.mark.parametrize("writer, marker", [("durable_jobs", None),
    ("native_ingress", "_native_memory_baseline_numeric"),
    ("native_ingress", "_prospective_admission_finished")])
@pytest.mark.asyncio
async def test_existing_special_writer_cannot_become_artifact_owner(original_artifact_owner, writer, marker):
    _, _, _factory, _operator = original_artifact_owner
    async with canonical_session() as db:
        await begin_native_writer(db, owner=writer)
        guard = db.info["composition_guard"]
        if marker is not None:
            # Adversarial state on a genuinely issued guard; no positive authority.
            setattr(guard, marker, True)
        transaction = db.sync_session.get_transaction()
        with pytest.raises(BoardError) as caught:
            await _begin_immediate(db)
        assert caught.value.code == "input_artifact_original_writer_required"
        assert db.info["composition_guard"] is guard
        assert db.sync_session.get_transaction() is transaction
        if marker is not None:
            assert getattr(guard, marker) is True
            delattr(guard, marker) if marker.startswith("_native_memory_") else setattr(guard, marker, False)


@pytest.mark.asyncio
async def test_flushed_caller_mutation_cannot_be_committed_by_artifact_boundary(original_artifact_owner):
    from src.work_board.contracts import WorkBoardTaskCreate
    from src.work_board.repository import WorkBoardRepository
    owner, request, _factory, _operator = original_artifact_owner
    class EndOriginalTransaction(Exception):
        pass
    with pytest.raises(EndOriginalTransaction):
        async with canonical_session() as db:
            await begin_native_writer(db, owner="native_ingress")
            guard = db.info["composition_guard"]
            await WorkBoardRepository().create_task(db, owner, WorkBoardTaskCreate(
                title="Unpublished original caller task", goal_id=request.goal_id,
                goal_revision=request.goal_revision, idempotency_key="pending-original-task"))
            await db.flush()
            assert guard.touched and not db.new and not db.dirty and not db.deleted
            transaction = db.sync_session.get_transaction()
            with pytest.raises(BoardError) as caught:
                await _begin_immediate(db)
            assert caught.value.code == "artifact_transaction_boundary"
            assert db.info["composition_guard"] is guard and not guard._retention_closed
            assert db.sync_session.get_transaction() is transaction
            assert guard.touched
            raise EndOriginalTransaction


@pytest.mark.asyncio
async def test_original_legacy_continuity_enrollment_cannot_be_retired(original_artifact_owner):
    from src.agent.session import _enroll_legacy_continuity_metadata
    from src.conversation.task_context import TaskContinuityService, ContinueTaskRequest
    from src.work_board.contracts import WorkBoardTaskCreate
    from src.work_board.repository import WorkBoardRepository
    from src.work_board.input_artifacts import _commit_artifact_writer
    owner, request, _factory, operator = original_artifact_owner
    board = WorkBoardRepository()
    async with canonical_session() as db:
        await begin_native_writer(db, owner="native_ingress")
        task = (await board.create_task(db, owner, WorkBoardTaskCreate(
            title="Original legacy continuity", goal_id=request.goal_id,
            goal_revision=request.goal_revision, idempotency_key="original-legacy-task"))).task
        await _commit_artifact_writer(db)
    continuity = TaskContinuityService(board)
    await continuity.start()
    try:
        async with canonical_session() as db:
            reply = await continuity.continue_task(db, operator, ContinueTaskRequest(
                task_id=task.task_id, expected_revision=task.task_revision,
                new_conversation_id=owner.session_id))
            assert reply["conversation_id"] == owner.session_id
            await _commit_artifact_writer(db)
    finally:
        await continuity.stop()
    class EndOriginalTransaction(Exception):
        pass
    with pytest.raises(EndOriginalTransaction):
        async with canonical_session() as db:
            await begin_native_writer(db, owner="native_ingress")
            guard = db.info["composition_guard"]
            await _enroll_legacy_continuity_metadata(db, owner.session_id)
            assert owner.session_id in guard._legacy_continuity_metadata
            original = guard._legacy_continuity_metadata[owner.session_id]
            transaction = db.sync_session.get_transaction()
            with pytest.raises(BoardError) as caught:
                await _begin_immediate(db)
            assert caught.value.code == "input_artifact_original_writer_required"
            assert db.info["composition_guard"] is guard and not guard._retention_closed
            assert db.sync_session.get_transaction() is transaction
            assert guard._legacy_continuity_metadata[owner.session_id] is original
            raise EndOriginalTransaction
