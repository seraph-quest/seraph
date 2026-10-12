"""Authenticated direct finite REPORT provenance; Source tests remain unrun.

This is original pipeline admission proof, not a terminal native Memory success
fixture. Browser/execution/terminal Source acquisition still needs its original
vertical fixture and independent release.
"""
import os

import pytest

from tests.test_artifact_pipelines import setup_operation
from tests.test_research_native_vertical import real_auth


@pytest.fixture
async def authenticated_direct_report_operation(async_db, real_auth, monkeypatch, tmp_path):
    from src.auth import service
    from src.work_board.contracts import WorkBoardOwner
    # Use the original configured-auth Root issuer and this real SQLAlchemy
    # test database. No fabricated principal, Root, Identity or certificate.
    monkeypatch.setattr(service, "get_session", async_db)
    _bearer, operator = await service.create_session()
    assert operator.operator_identity_id is None
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    return await setup_operation(async_db, monkeypatch, tmp_path, owner=owner)


@pytest.mark.asyncio
async def test_authenticated_direct_finite_report_is_conditional_identity_absent(
        async_db, authenticated_direct_report_operation):
    from src.db.models import OperatorSession
    from src.work_board import pipelines
    owner, repository, accepted = authenticated_direct_report_operation
    async with async_db() as db:
        root = await db.get(OperatorSession, owner.session_id)
        assert root.principal_id == owner.principal_id and root.operator_identity_id is None
        operation, _value = await pipelines.owned(db, owner, accepted["operation_id"])
        assert operation.opportunity_id is None and operation.status == "accepted"
        report = await repository.get_task(db, owner, accepted["steps"][2]["task_id"])
        assert report.capability_id == "work.local-evidence-report.v1"
        assert report.pipeline_operation_id == operation.proposal_id


@pytest.mark.asyncio
async def test_actual_direct_report_root_does_not_supply_owned_original_memory_session(
        async_db, authenticated_direct_report_operation):
    """An accepted pipeline Root alone cannot satisfy the original M5 owner."""
    from src.db.models import Session
    from src.memory.m5 import _require_original_m5_session
    owner, _repository, _accepted = authenticated_direct_report_operation
    async with async_db() as db:
        original = await db.get(Session, owner.session_id)
        assert original is None or original.owner_principal_id is None
        with pytest.raises(PermissionError, match="original memory Session owner is required"):
            await _require_original_m5_session(db, owner.principal_id, owner.session_id)


@pytest.mark.asyncio
async def test_actual_direct_report_cannot_acquire_initial_input_after_writer(
        async_db, authenticated_direct_report_operation):
    from src.work_board.review import _stage_terminal_report_input
    from src.work_board.repository import BoardError
    from src.memory.header_bounds import HeaderReadBudget
    owner, repository, accepted = authenticated_direct_report_operation
    async with async_db() as db:
        report = await repository.get_task(db, owner, accepted["steps"][2]["task_id"])
        db.info["native_writer_started"] = True
        with pytest.raises(BoardError) as stopped:
            await _stage_terminal_report_input(db, report, header_budget=HeaderReadBudget())
        assert stopped.value.code == "pipeline_output_unverified"


@pytest.mark.asyncio
async def test_actual_direct_report_operation_rejects_wrong_root_and_workspace(
        async_db, authenticated_direct_report_operation):
    from src.auth import service
    from src.work_board import pipelines
    from src.work_board.contracts import WorkBoardOwner
    from src.work_board.repository import BoardError
    owner, _repository, accepted = authenticated_direct_report_operation
    _bearer, other = await service.create_session()
    wrong = WorkBoardOwner(principal_id=other.principal.principal_id, session_id=other.session_id)
    async with async_db() as db:
        with pytest.raises(BoardError) as denied:
            await pipelines.owned(db, wrong, accepted["operation_id"])
        assert denied.value.code == "pipeline_not_found"
        with pytest.raises(BoardError) as changed:
            await pipelines.owned(db, owner, accepted["operation_id"], workspace_identity=b"{}")
        assert changed.value.code == "pipeline_root_changed"


@pytest.mark.skipif(os.environ.get("SERAPH_RUN_REAL_BROWSER_VERTICAL_SLICE") != "1",
    reason="original real Chromium vertical requires explicit opt-in")
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.asyncio
async def test_authentic_terminal_direct_report_retains_stage_and_rejects_copy_task_frame(
        async_db, authenticated_direct_report_operation, monkeypatch, tmp_path):
    """Reuse the original real execution journey with its actual issued Root.

    External HTTPS bytes belong to that original declared browser fixture;
    terminal rows, private report bytes/stage, certificates and proof are real.
    This Source remains unrun and does not claim native Memory effect success.
    """
    import asyncio
    from dataclasses import replace
    from tests import test_artifact_pipelines as vertical
    from src.runtime_plugins.memory_producer import source_binding, _checked_memory_report_input_stage
    from src.runtime_plugins.dispatch import NativeServiceBlocked
    from src.memory.header_bounds import HeaderReadBudget
    owner, repository, accepted = authenticated_direct_report_operation

    async def original_authenticated_operation(*args, **kwargs):
        return owner, repository, accepted

    # Replace only the historical fixture's bypass-owner setup. Its actual
    # browser→dossier→REPORT execution, claim/CAS/readback owners stay intact.
    monkeypatch.setattr(vertical, "setup_operation", original_authenticated_operation)
    await vertical.test_real_chromium_native_browser_to_cpu_dossier_to_plain_report(
        async_db, monkeypatch, tmp_path, "original")
    async with async_db() as db:
        from sqlalchemy import select
        from src.db.models import WorkBoardAttempt, WorkBoardStatus
        report = await repository.get_task(db, owner, accepted["steps"][2]["task_id"])
        assert report.status == WorkBoardStatus.done
        attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == report.task_id)
            .order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc()).limit(1))
        frame = HeaderReadBudget()
        _binding, proof = await source_binding(db, principal_id=owner.principal_id,
            session_id=owner.session_id, task_id=report.task_id, revision=report.task_revision,
            attempt_id=attempt.attempt_id, header_budget=frame)
        stage = proof._native_memory_report_input_stage
        assert _checked_memory_report_input_stage(stage, frame, report.task_id,
            attempt.attempt_id, preparing=True) is stage
        with pytest.raises(NativeServiceBlocked):
            _checked_memory_report_input_stage(replace(stage), frame, report.task_id,
                attempt.attempt_id, preparing=True)
        with pytest.raises(NativeServiceBlocked):
            _checked_memory_report_input_stage(stage, HeaderReadBudget(), report.task_id,
                attempt.attempt_id, preparing=True)

        async def original_stage_from_wrong_task():
            with pytest.raises(NativeServiceBlocked):
                _checked_memory_report_input_stage(stage, frame, report.task_id,
                    attempt.attempt_id, preparing=True)

        await asyncio.create_task(original_stage_from_wrong_task())
