"""Original artifact phases on full initialized, persistent canonical SQLite."""
import os
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from config.settings import settings
from src.db.engine import get_session as canonical_session, override_session_factory
from src.db.models import Goal, WorkBoardInputArtifact
from src.memory.evidence_execution import _current_operator
from src.runtime_plugins.ownership import DOMAINS, begin_native_writer, initialize_fresh_deployment
from src.work_board import input_artifacts as artifacts
from src.work_board.contracts import WorkBoardOwner
from src.work_board.repository import BoardError
from src.workspace.production import ProductionWorkspace, maintenance_fence
from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth
from tests.test_work_board_m3_browser import _artifact_request


@pytest_asyncio.fixture
async def native_inputs(accounting_db, real_auth, monkeypatch):
    from src.auth import service
    from src.db import engine as original
    from src.runtime_plugins.composition import reviewed_composition
    root, engine, factory = accounting_db
    monkeypatch.setattr(original, "engine", engine)
    monkeypatch.setattr(original, "_db_path", str(root / "seraph.db"))
    monkeypatch.setattr(original, "async_session_factory", factory)
    monkeypatch.setattr(service, "get_session", canonical_session)
    await original.init_db()
    with override_session_factory(factory):
        _, operator = await service.create_session()
        owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
        async with canonical_session() as db:
            db.add(Goal(id="goal-artifact", title="Original bounded plan input", status="active", revision=1,
                owner_principal_id=owner.principal_id, owner_session_id=owner.session_id))
        node = Path(os.environ["SERAPH_CORDIS_TEST_NODE"])
        reviewed = reviewed_composition(node_path=node)
        with maintenance_fence(ProductionWorkspace(host_root=root)):
            async with canonical_session() as db:
                await begin_native_writer(db, owner="composition_maintenance", fresh=True)
                await initialize_fresh_deployment(db,
                    composition_digests={domain: reviewed.composition_digest for domain in DOMAINS})
        async with canonical_session() as db:
            names = set((await db.execute(text("SELECT name FROM sqlite_schema"))).scalars())
            assert {"operator_principal_required_insert", "operator_principal_required_update"} <= names
            assert len([name for name in names if name.startswith("session_recall_")]) == 15
        yield root, owner, operator


async def reserve(owner, operator, request):
    async with canonical_session() as db:
        await begin_native_writer(db, owner="native_ingress")
        await _current_operator(db, owner, operator)
        return await artifacts._reserve_plan_input_artifact(db, owner, request)


async def finalize(owner, operator, request, reservation, staged):
    async with canonical_session() as db:
        await begin_native_writer(db, owner="native_ingress")
        await _current_operator(db, owner, operator)
        return await artifacts._finalize_plan_input_artifact(db, owner, request,
            reservation=reservation, staged=staged)


async def test_original_reserve_stage_finalize_replay_and_conflict(native_inputs, monkeypatch):
    root, owner, operator = native_inputs
    request = _artifact_request()
    # These physical owners may run only while neither artifact SQL helper runs.
    active = False
    original_write, original_read = artifacts._write_payload, artifacts._safe_file_bytes
    def write(*args, **kwargs):
        assert not active
        return original_write(*args, **kwargs)
    def read(*args, **kwargs):
        assert not active
        return original_read(*args, **kwargs)
    monkeypatch.setattr(artifacts, "_write_payload", write)
    monkeypatch.setattr(artifacts, "_safe_file_bytes", read)
    active = True
    reservation = await reserve(owner, operator, request)
    active = False
    assert reservation.requires_write
    staged = artifacts._stage_plan_input_artifact(reservation)
    active = True
    metadata = await finalize(owner, operator, request, reservation, staged)
    active = False
    assert metadata.revision == 2 and metadata.state == "pending"
    path = root / metadata.typed_input_ref.removeprefix("workspace-json:")
    assert path.read_bytes() == staged
    replay = await reserve(owner, operator, request)
    assert not replay.requires_write
    replay_staged = artifacts._stage_plan_input_artifact(replay)
    assert await finalize(owner, operator, request, replay, replay_staged) == metadata
    conflicting = request.model_copy(update={"input": {**request.input, "start_url": "https://public.example/docs/changed"}})
    with pytest.raises(BoardError, match="idempotency"):
        await reserve(owner, operator, conflicting)
    async with canonical_session() as db:
        row = await db.get(WorkBoardInputArtifact, metadata.artifact_id)
        assert row.metadata_digest == artifacts._metadata_digest(row)
        assert len((await db.scalars(select(WorkBoardInputArtifact))).all()) == 1


@pytest.mark.parametrize("failure", ["write", "tamper", "symlink", "fifo", "goal", "root", "row"])
async def test_original_pending_failure_and_negative_transitions(native_inputs, monkeypatch, failure):
    root, owner, operator = native_inputs
    request = _artifact_request()
    reservation = await reserve(owner, operator, request)
    if failure == "write":
        # A real no-clobber collision makes the original physical write fail.
        path = root / reservation.metadata.typed_input_ref.removeprefix("workspace-json:")
        path.parent.mkdir(parents=True, mode=0o700)
        path.write_bytes(b"preexisting incompatible private payload")
        path.chmod(0o600)
        with pytest.raises(BoardError, match="could not be written"):
            artifacts._stage_plan_input_artifact(reservation)
        assert path.read_bytes() == b"preexisting incompatible private payload"
    else:
        staged = artifacts._stage_plan_input_artifact(reservation)
        if failure in {"tamper", "symlink", "fifo"}:
            # Finalize once, then replay must read the actual changed file.
            await finalize(owner, operator, request, reservation, staged)
            replay = await reserve(owner, operator, request)
            path = root / replay.metadata.typed_input_ref.removeprefix("workspace-json:")
            path.unlink()
            if failure == "tamper":
                path.write_bytes(b"bad")
                path.chmod(0o600)
            elif failure == "symlink":
                path.symlink_to(root / "seraph.db")
            else:
                os.mkfifo(path, 0o600)
            with pytest.raises((BoardError, OSError)):
                artifacts._stage_plan_input_artifact(replay)
            return
        if failure == "root":
            from src.auth.service import revoke_session
            await revoke_session(owner.session_id)
            with pytest.raises(BoardError) as error:
                await finalize(owner, operator, request, reservation, staged)
            assert error.value.code == "evidence_owner_not_current"
        else:
            async with canonical_session() as db:
                await begin_native_writer(db, owner="native_ingress")
                if failure == "goal":
                    row = await db.get(Goal, request.goal_id)
                    row.revision += 1
                else:
                    row = await db.get(WorkBoardInputArtifact, reservation.metadata.artifact_id)
                    row.state = "revoked"
                db.add(row)
            with pytest.raises(BoardError):
                await finalize(owner, operator, request, reservation, staged)
    async with canonical_session() as db:
        row = await db.get(WorkBoardInputArtifact, reservation.metadata.artifact_id)
        assert row.metadata_digest is None and row.revision == 1


@pytest.mark.parametrize("state", ["expired", "revoked", "deleted"])
async def test_original_terminal_replay_has_no_physical_effect(native_inputs, monkeypatch, state):
    _, owner, operator = native_inputs
    request = _artifact_request()
    original = await reserve(owner, operator, request)
    async with canonical_session() as db:
        await begin_native_writer(db, owner="native_ingress")
        row = await db.get(WorkBoardInputArtifact, original.metadata.artifact_id)
        row.state = state
        db.add(row)
    def forbidden(*args, **kwargs):
        raise AssertionError("terminal replay cannot touch physical bytes")
    monkeypatch.setattr(artifacts, "_write_payload", forbidden)
    monkeypatch.setattr(artifacts, "_safe_file_bytes", forbidden)
    replay = await reserve(owner, operator, request)
    assert not replay.requires_write
    assert artifacts._stage_plan_input_artifact(replay) is None
    metadata = await finalize(owner, operator, request, replay, None)
    assert metadata.state == state and metadata.revision == 1


async def test_original_helpers_require_native_writer(native_inputs):
    _, owner, _ = native_inputs
    async with canonical_session() as db:
        with pytest.raises(BoardError, match="native writer"):
            await artifacts._reserve_plan_input_artifact(db, owner, _artifact_request())
