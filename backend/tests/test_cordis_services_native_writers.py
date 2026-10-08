"""Actual SQLite negative writes; these do not establish bridge readiness."""
import pytest
from sqlalchemy import select, func

from tests.test_inference_accounting import accounting_db
from src.db.models import AuditEvent, Memory, MemoryProposal, GovernedScheduleBinding
from src.runtime_plugins.ownership import CompositionBindingError


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["audit", "memory_propose", "memory_apply", "memory_control", "scheduler_create"])
async def test_native_composition_denial_runs_in_writer_before_any_owned_row_mutation(accounting_db, monkeypatch, owner):
    _, _, factory = accounting_db
    calls = []
    async def deny(db):
        assert db.in_transaction()
        calls.append(db)
        raise CompositionBindingError("composition_epoch_stale")
    with pytest.raises(CompositionBindingError, match="composition_epoch_stale"):
        if owner == "audit":
            from src.audit.repository import audit_repository
            await audit_repository.log_event(event_type="runtime_service_observed", summary="safe projection",
                composition_authority_check=deny)
        elif owner in {"memory_propose", "memory_apply"}:
            from src.memory import m5
            monkeypatch.setattr(m5, "get_session", factory.accounting_sessions)
            if owner == "memory_propose":
                await m5.create_memory_proposal(owner_principal_id="principal:original", owner_session_id="session:original",
                    task_id="task:original", expected_task_revision=1, attempt_id="attempt:original",
                    composition_authority_check=deny)
            else:
                await m5.apply_memory_proposal_action(owner_principal_id="principal:original", owner_session_id="session:original",
                    proposal_id="proposal:original", action="accept", expected_revision=1,
                    composition_authority_check=deny)
        elif owner == "memory_control":
            from src.memory import repository
            monkeypatch.setattr(repository, "get_session", factory.accounting_sessions)
            await repository.memory_repository.update_memory_control_metadata("memory:original",
                metadata_updates={"pinned": False}, composition_authority_check=deny)
        else:
            from src.scheduler.governed_schedules import create_binding
            from src.work_board.contracts import WorkBoardOwner
            async with factory.accounting_sessions() as db:
                await create_binding(db, WorkBoardOwner(principal_id="principal:original", session_id="session:original"),
                    {}, composition_authority_check=deny)
    assert len(calls) == 1
    async with factory.accounting_sessions() as db:
        for model in (AuditEvent, Memory, MemoryProposal, GovernedScheduleBinding):
            assert await db.scalar(select(func.count()).select_from(model)) == 0
