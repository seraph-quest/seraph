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


@pytest.fixture
async def authenticated_direct_report_with_original_memory_session(async_db, real_auth, monkeypatch, tmp_path):
    """Use the existing continuity service before any pipeline/job admission."""
    from config.settings import settings
    from src.auth import service
    from src.goals import repository as goals
    from src.work_board.contracts import WorkBoardOwner, WorkBoardTaskCreate
    from src.work_board.repository import WorkBoardRepository
    from src.conversation.task_context import TaskContinuityService, ContinueTaskRequest
    from src.db.models import Session, WorkflowRunState
    from sqlalchemy import select
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(service, "get_session", async_db)
    monkeypatch.setattr(goals, "get_session", async_db)
    _bearer, operator = await service.create_session()
    assert operator.operator_identity_id is None
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    # Original service creation, with the authenticated owner: no seeded Goal,
    # Session, Source, bytes, certificate or ownerless-row ownership update.
    goal = await goals.GoalRepository().create(title="Original conversation prerequisite",
        owner_principal_id=owner.principal_id, owner_session_id=owner.session_id)
    repository = WorkBoardRepository()
    continuity = TaskContinuityService(repository)
    await continuity.start()
    try:
        async with async_db() as db:
            mutation = await repository.create_task(db, owner, WorkBoardTaskCreate(
                title="Choose an original task conversation", goal_id=goal.id, goal_revision=goal.revision,
                idempotency_key="native-memory-original-session-prerequisite"), origin_session_id=owner.session_id)
            task_id, task_revision = mutation.task.task_id, mutation.task.task_revision
            await db.commit()
            assert await db.get(Session, owner.session_id) is None
            assert not (await db.scalars(select(WorkflowRunState))).all()
            await db.rollback()
            reply = await continuity.continue_task(db, operator, ContinueTaskRequest(
                task_id=task_id, expected_revision=task_revision,
                new_conversation_id=operator.session_id))
            assert reply["conversation_id"] == operator.session_id and reply["idempotent_replay"] is False
            await db.commit()
            original = await db.get(Session, operator.session_id)
            assert original.owner_principal_id == operator.principal.principal_id
            assert original.continuity_task_id == task_id
    finally:
        await continuity.stop()
    return await setup_operation(async_db, monkeypatch, tmp_path, owner=owner)


@pytest.mark.asyncio
async def test_original_continuity_session_satisfies_original_m5_owner_before_report(
        async_db, authenticated_direct_report_with_original_memory_session):
    from src.memory.m5 import _require_original_m5_session
    from src.db.models import Session
    owner, _repository, accepted = authenticated_direct_report_with_original_memory_session
    assert len(accepted["steps"]) == 3
    async with async_db() as db:
        await _require_original_m5_session(db, owner.principal_id, owner.session_id)
        retained = await db.get(Session, owner.session_id)
        assert retained.owner_principal_id == owner.principal_id


@pytest.mark.asyncio
async def test_unissued_report_input_stage_cannot_issue_numeric_facts():
    from src.runtime_plugins.memory_producer import (_OriginalMemoryReportInputStage,
        _memory_report_decoder_numeric_owner)
    from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
    budget = HeaderReadBudget()
    before = budget.remaining
    # Uninitialized negative witness: the registry must deny before reading
    # any owner field, allocating dimensions or debiting the real payer.
    unissued = object.__new__(_OriginalMemoryReportInputStage)
    with pytest.raises(HeaderBoundsError, match="memory_report_input_numeric_owner_changed"):
        _memory_report_decoder_numeric_owner(unissued, None, b"", None, budget)
    assert budget.remaining == before


def test_legacy_verification_receipt_preserves_falsy_verified_at_conversion():
    from src.work_board.review import _safe_verification_receipt
    # Legacy proof conversion uses str(raw_verified_at), even for numeric0.
    # This ordinary-path regression needs no Source/certificate fabrication.
    proof = {"receipt_kind": "readback", "workflow_run_id": "original-report-root",
        "content_sha256": "a" * 64, "verified_at": 0}
    result = _safe_verification_receipt(proof)
    assert result["verified_at"] == "0"


def test_unissued_report_admission_scope_denies_before_owner_fields_or_debit():
    from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
    from src.runtime_plugins.task_capability import _report_memory_admission_payer
    from src.work_board.review import (
        _MEMORY_REPORT_PRINCIPAL_SCOPE, _MemoryReportDependencyNumericInventory,
    )
    budget = HeaderReadBudget()
    before = budget.remaining
    with pytest.raises(HeaderBoundsError, match="memory_report_admission_numeric_owner_changed"):
        _report_memory_admission_payer(budget)
    token = _MEMORY_REPORT_PRINCIPAL_SCOPE.set(object.__new__(_MemoryReportDependencyNumericInventory))
    try:
        with pytest.raises(HeaderBoundsError, match="memory_report_admission_numeric_owner_changed"):
            _report_memory_admission_payer(budget)
    finally:
        _MEMORY_REPORT_PRINCIPAL_SCOPE.reset(token)
    assert budget.remaining == before


def test_unissued_report_row_inventory_cannot_pay_or_grant_rows():
    from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
    from src.work_board.review import (_MemoryReportDependencyNumericInventory,
        _memory_report_row_certificate)
    budget = HeaderReadBudget()
    before = budget.remaining
    # Exact-class construction alone is not the original eight-cert issuance.
    # Registry denial precedes reads of uninitialized owner/certificate fields.
    budget._native_memory_report_dependency_numeric = object.__new__(_MemoryReportDependencyNumericInventory)
    with pytest.raises(HeaderBoundsError, match="memory_report_row_numeric_owner_changed"):
        _memory_report_row_certificate(None, None, budget, "work_board_events")
    assert budget.remaining == before


@pytest.mark.asyncio
async def test_original_continuity_cannot_claim_an_existing_audit_placeholder(
        async_db, real_auth, monkeypatch, tmp_path):
    from config.settings import settings
    from src.auth import service
    from src.audit import repository as audit
    from src.goals import repository as goals
    from src.work_board.contracts import WorkBoardOwner, WorkBoardTaskCreate
    from src.work_board.repository import WorkBoardRepository, BoardError
    from src.conversation.task_context import TaskContinuityService, ContinueTaskRequest
    from src.db.models import Session
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(service, "get_session", async_db)
    monkeypatch.setattr(goals, "get_session", async_db)
    monkeypatch.setattr(audit, "get_session", async_db)
    _bearer, operator = await service.create_session()
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    goal = await goals.GoalRepository().create(title="Existing placeholder denial",
        owner_principal_id=owner.principal_id, owner_session_id=owner.session_id)
    repository = WorkBoardRepository()
    async with async_db() as db:
        mutation = await repository.create_task(db, owner, WorkBoardTaskCreate(title="Original task",
            goal_id=goal.id, goal_revision=goal.revision, idempotency_key="native-memory-placeholder-denial"))
        task_id, revision = mutation.task.task_id, mutation.task.task_revision
    # The original Audit owner creates its own FK placeholder through
    # ensure_sessions_exist; no test-seeded Session or ownership assignment.
    await audit.AuditRepository().log_event(session_id=operator.session_id,
        actor="operator", event_type="task.continuity.requested", summary="Original placeholder denial")
    continuity = TaskContinuityService(repository)
    await continuity.start()
    try:
        async with async_db() as db:
            existing = await db.get(Session, operator.session_id)
            assert existing is not None and existing.owner_principal_id is None
            await db.rollback()
            with pytest.raises(BoardError) as denied:
                await continuity.continue_task(db, operator, ContinueTaskRequest(task_id=task_id,
                    expected_revision=revision, new_conversation_id=operator.session_id))
            assert denied.value.code == "task_conversation_conflict"
        async with async_db() as db:
            existing = await db.get(Session, operator.session_id)
            assert existing.owner_principal_id is None
    finally:
        await continuity.stop()


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
