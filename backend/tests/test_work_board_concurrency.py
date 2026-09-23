"""SQLite concurrency receipts for the canonical work-board graph."""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel

from src.db.models import Goal, WorkBoardEvent, WorkBoardLink, WorkBoardTask
from src.work_board.contracts import WorkBoardLinkCreate, WorkBoardOwner, WorkBoardTaskCreate
from src.work_board.repository import WorkBoardRepository


OWNER = WorkBoardOwner(
    principal_id="operator:concurrency",
    session_id="concurrency-session",
)


@pytest.mark.asyncio
async def test_concurrent_same_idempotency_key_returns_one_committed_task(tmp_path: Path):
    database_path = tmp_path / "board-idempotency-race.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: SQLModel.metadata.create_all(
                sync,
                tables=[Goal.__table__, WorkBoardTask.__table__, WorkBoardEvent.__table__],
            )
        )

    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        db.add(
            Goal(
                id="goal-idempotency-race",
                title="Race goal",
                owner_principal_id=OWNER.principal_id,
                owner_session_id=OWNER.session_id,
                revision=1,
            )
        )
        await db.commit()

    request = WorkBoardTaskCreate(
        title="Concurrent task",
        goal_id="goal-idempotency-race",
        goal_revision=1,
        idempotency_scope="race",
        idempotency_key="same-key",
    )
    start = asyncio.Barrier(2)

    async def create_once():
        async with factory() as db:
            await start.wait()
            mutation = await WorkBoardRepository().create_task(db, OWNER, request)
            await db.commit()
            return mutation

    first, second = await asyncio.gather(create_once(), create_once())
    assert first.task.task_id == second.task.task_id
    assert sorted((first.idempotent_replay, second.idempotent_replay)) == [False, True]

    async with factory() as db:
        rows = list(
            (
                await db.execute(
                    select(WorkBoardTask).where(
                        WorkBoardTask.owner_principal_id == OWNER.principal_id,
                        WorkBoardTask.owner_session_id == OWNER.session_id,
                        WorkBoardTask.idempotency_scope == "race",
                        WorkBoardTask.idempotency_key == "same-key",
                    )
                )
            ).scalars().all()
        )
    await engine.dispose()
    assert [row.task_id for row in rows] == [first.task.task_id]


@pytest.mark.asyncio
async def test_opposite_dependency_writes_serialize_cycle_check_and_insert(tmp_path):
    database_path = tmp_path / "board-concurrency.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    tables = [WorkBoardTask.__table__, WorkBoardLink.__table__, WorkBoardEvent.__table__]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: SQLModel.metadata.create_all(sync, tables=tables))

    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        db.add_all(
            [
                WorkBoardTask(
                    task_id="task-a",
                    owner_principal_id=OWNER.principal_id,
                    owner_session_id=OWNER.session_id,
                    goal_id="goal-a",
                    title="A",
                    idempotency_key="task-a",
                ),
                WorkBoardTask(
                    task_id="task-b",
                    owner_principal_id=OWNER.principal_id,
                    owner_session_id=OWNER.session_id,
                    goal_id="goal-b",
                    title="B",
                    idempotency_key="task-b",
                ),
            ]
        )
        await db.commit()

    start = asyncio.Barrier(2)
    repository = WorkBoardRepository()

    async def attempt(parent_task_id: str, child_task_id: str) -> str:
        async with factory() as db:
            await start.wait()
            try:
                await repository.add_link(
                    db,
                    OWNER,
                    WorkBoardLinkCreate(
                        parent_task_id=parent_task_id,
                        child_task_id=child_task_id,
                        expected_child_revision=1,
                    ),
                )
                await db.commit()
                return "committed"
            except Exception as exc:
                await db.rollback()
                return getattr(exc, "code", type(exc).__name__)

    results = await asyncio.gather(
        attempt("task-a", "task-b"),
        attempt("task-b", "task-a"),
    )
    await engine.dispose()

    assert results.count("committed") == 1
    assert results.count("dependency_cycle") == 1


@pytest.mark.asyncio
async def test_task_list_snapshot_does_not_pair_old_tasks_with_new_event_cursor(tmp_path):
    database_path = tmp_path / "board-snapshot.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    tables = [WorkBoardTask.__table__, WorkBoardLink.__table__, WorkBoardEvent.__table__]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: SQLModel.metadata.create_all(sync, tables=tables))

    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        task = WorkBoardTask(
            task_id="snapshot-task-a",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            goal_id="goal-a",
            title="Existing task",
            idempotency_key="snapshot-a",
        )
        db.add(task)
        await db.flush()
        db.add(
            WorkBoardEvent(
                task_id=task.task_id,
                owner_principal_id=OWNER.principal_id,
                owner_session_id=OWNER.session_id,
                actor_principal_id=OWNER.principal_id,
                actor_session_id=OWNER.session_id,
                kind="task.created",
                metadata_json="{}",
            )
        )
        await db.commit()

    reader = factory()
    writer_started = asyncio.Event()
    writer_committed = asyncio.Event()
    writer_tasks: list[asyncio.Task] = []
    original_execute = reader.execute

    async def add_task_after_snapshot():
        writer_started.set()
        async with factory() as db:
            task = WorkBoardTask(
                task_id="snapshot-task-b",
                owner_principal_id=OWNER.principal_id,
                owner_session_id=OWNER.session_id,
                goal_id="goal-b",
                title="Concurrent task",
                idempotency_key="snapshot-b",
            )
            db.add(task)
            await db.flush()
            db.add(
                WorkBoardEvent(
                    task_id=task.task_id,
                    owner_principal_id=OWNER.principal_id,
                    owner_session_id=OWNER.session_id,
                    actor_principal_id=OWNER.principal_id,
                    actor_session_id=OWNER.session_id,
                    kind="task.created",
                    metadata_json="{}",
                )
            )
            await db.commit()
            writer_committed.set()

    scheduled = False

    async def execute_with_concurrent_write(statement, *args, **kwargs):
        nonlocal scheduled
        result = await original_execute(statement, *args, **kwargs)
        if not scheduled and "work_board_tasks" in str(statement):
            scheduled = True
            writer_tasks.append(asyncio.create_task(add_task_after_snapshot()))
            await writer_started.wait()
            await asyncio.wait_for(writer_committed.wait(), timeout=5)
        return result

    reader.execute = execute_with_concurrent_write
    try:
        page = await WorkBoardRepository().list_tasks(reader, OWNER)
        await reader.commit()
        await asyncio.gather(*writer_tasks)
    finally:
        await reader.close()
        await engine.dispose()

    assert [task.task_id for task in page.tasks] == ["snapshot-task-a"]
    assert page.last_event_id == 1
