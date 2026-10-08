"""Only actual unselected legacy transcript metadata may use the exception."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select
from sqlalchemy import text, update

from tests.test_runtime_composition_ownership import composition_db, bound_spec
from src.db.engine import get_session as canonical_session
from src.db.models import Session, WorkBoardTask, Message, WorkflowRunState
from src.runtime_plugins.ownership import begin_native_writer
from src.agent.session import SessionManager, _enroll_legacy_continuity_metadata
from src.workflows.job_runtime import DurableJobRepository
from src.workspace.production import ProductionWorkspaceReconciliationError, read_lifecycle_receipt, read_accounting_checkpoint


async def task_session():
    async with canonical_session() as db:
        await begin_native_writer(db, owner="finite_service")
        db.add(WorkBoardTask(task_id="legacy-context", owner_principal_id="owner", owner_session_id="operator", goal_id="goal", idempotency_key="context", idempotency_binding="fixture-context"))
        await db.flush()
        db.add(Session(id="legacy-session", owner_principal_id="owner", continuity_task_id="legacy-context", title="Task conversation"))


@pytest.mark.asyncio
async def test_actual_public_legacy_transcript_keeps_task_link_without_native_job(composition_db):
    await task_session()
    message = await SessionManager().add_message("legacy-session", "user", "Existing task context.")
    async with canonical_session() as db:
        assert (await db.get(Session, "legacy-session")).continuity_task_id == "legacy-context"
        assert (await db.get(Message, message.id)).content == "Existing task context."
        assert list((await db.execute(select(WorkflowRunState))).scalars()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["link_clear", "link_replace", "owner", "created", "delete"])
async def test_enrolled_legacy_metadata_cannot_mutate_identity_link_or_delete(composition_db, mutation):
    _, _, _, workspace = composition_db
    await task_session()
    receipt, checkpoint = read_lifecycle_receipt(workspace), read_accounting_checkpoint(workspace)
    with pytest.raises(ProductionWorkspaceReconciliationError, match="composition_session_continuity_unsupported"):
        async with canonical_session() as db:
            await begin_native_writer(db, owner="native_ingress")
            await _enroll_legacy_continuity_metadata(db, "legacy-session")
            record = await db.get(Session, "legacy-session")
            if mutation == "delete":
                await db.delete(record)
            elif mutation == "created":
                record.created_at = record.created_at + timedelta(seconds=1)
            elif mutation == "owner":
                record.owner_principal_id = "other"
            else:
                record.continuity_task_id = None if mutation == "link_clear" else "other-task"
            await db.flush()
    async with canonical_session() as db:
        record = await db.get(Session, "legacy-session")
        assert record is not None and record.owner_principal_id == "owner" and record.continuity_task_id == "legacy-context"
    assert read_lifecycle_receipt(workspace) == receipt
    assert read_accounting_checkpoint(workspace) == checkpoint


@pytest.mark.asyncio
async def test_enrollment_cannot_race_a_native_job_into_linked_session(composition_db):
    _, _, _, workspace = composition_db
    await task_session()
    original = await bound_spec(job_id="native-accidental-enrollment")
    spec = replace(original, session_id="legacy-session", conversation_id="legacy-session")
    receipt, checkpoint = read_lifecycle_receipt(workspace), read_accounting_checkpoint(workspace)
    with pytest.raises(ProductionWorkspaceReconciliationError, match="composition_session_continuity_unsupported"):
        async with canonical_session() as db:
            await begin_native_writer(db, owner="native_ingress")
            await _enroll_legacy_continuity_metadata(db, "legacy-session")
            record = await db.get(Session, "legacy-session")
            record.updated_at = datetime.now(timezone.utc)
            await DurableJobRepository()._admit_in_session(db, spec)
    async with canonical_session() as db:
        assert list((await db.execute(select(WorkflowRunState))).scalars()) == []
        assert (await db.get(Session, "legacy-session")).continuity_task_id == "legacy-context"
    assert read_lifecycle_receipt(workspace) == receipt
    assert read_accounting_checkpoint(workspace) == checkpoint


@pytest.mark.asyncio
async def test_internal_message_helper_does_not_enroll_from_absent_native_execution(composition_db):
    await task_session()
    with pytest.raises(ProductionWorkspaceReconciliationError, match="composition_session_continuity_unsupported"):
        async with canonical_session() as db:
            await begin_native_writer(db, owner="native_ingress")
            await SessionManager()._add_message_in_db(db, "legacy-session", "user", "No automatic exception.")
    async with canonical_session() as db:
        assert list((await db.execute(select(Message))).scalars()) == []
        assert (await db.get(Session, "legacy-session")).continuity_task_id == "legacy-context"


@pytest.mark.asyncio
@pytest.mark.parametrize("style", ["core", "raw"])
async def test_enrolled_metadata_is_not_bulk_or_raw_sql_permission(composition_db, style):
    await task_session()
    with pytest.raises(ProductionWorkspaceReconciliationError, match="composition_unhooked_bulk_sql"):
        async with canonical_session() as db:
            await begin_native_writer(db, owner="native_ingress")
            await _enroll_legacy_continuity_metadata(db, "legacy-session")
            if style == "core":
                await db.execute(update(Session).where(Session.id == "legacy-session").values(continuity_task_id=None))
            else:
                await db.execute(text("UPDATE sessions SET continuity_task_id=NULL WHERE id='legacy-session'"))
    async with canonical_session() as db:
        assert (await db.get(Session, "legacy-session")).continuity_task_id == "legacy-context"
