"""Committed board mutations reach only their authenticated live session."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

import src.db.engine as db_engine
from src.db.models import Goal, WorkBoardTask
from src.scheduler.connection_manager import ws_manager
from src.work_board.contracts import WorkBoardOwner, WorkBoardTaskCreate
from src.work_board.repository import WorkBoardRepository


@pytest.mark.asyncio
async def test_committed_dispatcher_and_worker_events_are_session_scoped(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_engine, "async_session_factory", factory)

    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)

    canonical_get_session = db_engine.get_session
    matching_socket = object()
    stale_session_socket = object()
    other_owner_socket = object()
    owner = WorkBoardOwner(principal_id="operator:event-test", session_id="session:event-test")
    matching_queue = ws_manager.connect_work_board(
        matching_socket,
        owner_principal_id=owner.principal_id,
        operator_session_id=owner.session_id,
    )
    stale_session_queue = ws_manager.connect_work_board(
        stale_session_socket,
        owner_principal_id=owner.principal_id,
        operator_session_id="session:stale",
    )
    other_owner_queue = ws_manager.connect_work_board(
        other_owner_socket,
        owner_principal_id="operator:other",
        operator_session_id=owner.session_id,
    )

    repository = WorkBoardRepository()
    try:
        async with canonical_get_session() as db:
            db.add(
                Goal(
                    id="goal:event-test",
                    title="Board event delivery",
                    owner_principal_id=owner.principal_id,
                    owner_session_id=owner.session_id,
                    revision=1,
                )
            )
            await db.flush()
            task_mutation = await repository.create_task(
                db,
                owner,
                WorkBoardTaskCreate(
                    title="Exercise event publication",
                    goal_id="goal:event-test",
                    goal_revision=1,
                    idempotency_key="event-publication-task",
                ),
            )
            task_id = task_mutation.task.task_id

        created = matching_queue.get_nowait()
        assert created["kind"] == "task.created"
        assert stale_session_queue.empty()
        assert other_owner_queue.empty()

        async with canonical_get_session() as db:
            task = (
                await db.execute(
                    select(WorkBoardTask).where(WorkBoardTask.task_id == task_id)
                )
            ).scalar_one()
            assert task is not None
            await repository._event(
                db,
                task,
                owner,
                kind="task.claimed",
                metadata={"status": "running"},
            )
            # A live event is never visible before its transaction commits.
            assert matching_queue.empty()

        dispatcher_event = matching_queue.get_nowait()
        assert dispatcher_event["kind"] == "task.claimed"
        assert stale_session_queue.empty()
        assert other_owner_queue.empty()

        async with canonical_get_session() as db:
            task = (
                await db.execute(
                    select(WorkBoardTask).where(WorkBoardTask.task_id == task_id)
                )
            ).scalar_one()
            assert task is not None
            await repository._event(
                db,
                task,
                owner,
                kind="task.comment",
                metadata={"body_digest": "a" * 64},
            )

        worker_event = matching_queue.get_nowait()
        assert worker_event["kind"] == "task.comment"
        assert worker_event["metadata"] == {"body_digest": "a" * 64}
        assert stale_session_queue.empty()
        assert other_owner_queue.empty()

        with pytest.raises(RuntimeError, match="rollback test"):
            async with canonical_get_session() as db:
                task = (
                    await db.execute(
                        select(WorkBoardTask).where(WorkBoardTask.task_id == task_id)
                    )
                ).scalar_one()
                assert task is not None
                await repository._event(
                    db,
                    task,
                    owner,
                    kind="task.blocked",
                    metadata={"status": "blocked"},
                )
                raise RuntimeError("rollback test")
        assert matching_queue.empty()
        assert stale_session_queue.empty()
        assert other_owner_queue.empty()
    finally:
        for socket in (matching_socket, stale_session_socket, other_owner_socket):
            ws_manager.disconnect_work_board(socket)
        await engine.dispose()
