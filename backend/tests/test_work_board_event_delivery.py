"""Committed board mutations reach only their authenticated live session."""

import pytest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

import src.db.engine as db_engine
from src.db.models import Goal, WorkBoardAttempt, WorkBoardStatus, WorkBoardTask
from src.scheduler.connection_manager import ws_manager
from src.work_board.contracts import WorkBoardOwner, WorkBoardTaskCreate
from src.work_board.dispatcher import WorkBoardDispatcher
from src.work_board.repository import WorkBoardRepository
from src.work_board.tools import WorkBoardWorkerComment, WorkBoardWorkerTools


@pytest.mark.asyncio
async def test_canonical_commit_publishes_redacted_events_only_after_commit(monkeypatch):
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


@pytest.mark.asyncio
async def test_dispatcher_and_worker_producers_publish_through_canonical_sessions(monkeypatch):
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
    owner = WorkBoardOwner(principal_id="operator:producer-test", session_id="session:producer-test")
    matching_socket = object()
    stale_session_socket = object()
    other_owner_socket = object()
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
    now = datetime.now(timezone.utc)
    dispatcher_task_id = "dispatcher-event-task"
    worker_task_id = "worker-event-task"
    worker_attempt_id = "worker-event-attempt"
    worker_run_id = "work-board:worker-event-task:worker-event-attempt"

    async with canonical_get_session() as db:
        db.add_all(
            [
                Goal(
                    id="goal:dispatcher-event",
                    title="Dispatcher event delivery",
                    owner_principal_id=owner.principal_id,
                    owner_session_id=owner.session_id,
                    revision=1,
                ),
                Goal(
                    id="goal:worker-event",
                    title="Worker event delivery",
                    owner_principal_id=owner.principal_id,
                    owner_session_id=owner.session_id,
                    revision=1,
                ),
                WorkBoardTask(
                    task_id=dispatcher_task_id,
                    owner_principal_id=owner.principal_id,
                    owner_session_id=owner.session_id,
                    goal_id="goal:dispatcher-event",
                    goal_revision=1,
                    title="Dispatcher event producer",
                    capability_id="unregistered.event-test",
                    typed_input_ref="workspace-json:inputs/dispatcher.json",
                    typed_input_digest="a" * 64,
                    executor_id="executor.event-test",
                    idempotency_key=dispatcher_task_id,
                    status=WorkBoardStatus.todo,
                ),
                WorkBoardTask(
                    task_id=worker_task_id,
                    owner_principal_id=owner.principal_id,
                    owner_session_id=owner.session_id,
                    goal_id="goal:worker-event",
                    goal_revision=1,
                    title="Worker event producer",
                    capability_id="workflow.goal-snapshot-to-file",
                    executor_id="executor.worker-event",
                    idempotency_key=worker_task_id,
                    status=WorkBoardStatus.triage,
                ),
            ]
        )

    async def authenticate_owner(_session_id: str, *, touch: bool = False):
        assert touch is False
        return SimpleNamespace(principal=SimpleNamespace(principal_id=owner.principal_id))

    monkeypatch.setattr("src.work_board.dispatcher.authenticate_session", authenticate_owner)
    dispatcher = WorkBoardDispatcher(
        repository=WorkBoardRepository(),
        session_provider=canonical_get_session,
        runner_id="service:dispatcher-event-test",
        now=lambda: now,
    )
    try:
        receipt = await dispatcher.run_pass()
        assert receipt["blocked"] == 1
        dispatch_event = matching_queue.get_nowait()
        assert dispatch_event["kind"] == "task.dispatch_blocked"
        assert dispatch_event["task_id"] == dispatcher_task_id
        assert stale_session_queue.empty()
        assert other_owner_queue.empty()

        async with canonical_get_session() as db:
            worker_task = (
                await db.execute(
                    select(WorkBoardTask).where(WorkBoardTask.task_id == worker_task_id)
                )
            ).scalar_one()
            worker_task.status = WorkBoardStatus.running
            worker_task.task_revision = 7
            db.add(
                WorkBoardAttempt(
                    attempt_id=worker_attempt_id,
                    task_id=worker_task_id,
                    workflow_run_id=worker_run_id,
                    task_revision_at_claim=6,
                    lease_owner="executor.worker-event",
                    lease_expires_at=now + timedelta(minutes=5),
                    heartbeat_at=now,
                    fencing_token=3,
                    executor_id="executor.worker-event",
                    started_at=now,
                )
            )
            await db.commit()

        class _Jobs:
            async def get_job(self, job_id):
                assert job_id == worker_run_id
                return {
                    "job_id": worker_run_id,
                    "status": "running",
                    "revision": 4,
                    "lease": {
                        "owner": "workflow-worker",
                        "fencing_token": 11,
                        "expires_at": (now + timedelta(minutes=5)).isoformat(),
                    },
                }

        worker = WorkBoardWorkerTools(
            repository=WorkBoardRepository(),
            jobs=_Jobs(),
            session_provider=canonical_get_session,
        )
        await worker.comment(
            WorkBoardWorkerComment(
                task_id=worker_task_id,
                attempt_id=worker_attempt_id,
                expected_task_revision=7,
                board_fencing_token=3,
                workflow_run_id=worker_run_id,
                workflow_fencing_token=11,
                body="The deterministic worker reached its checkpoint.",
            )
        )
        worker_event = matching_queue.get_nowait()
        assert worker_event["kind"] == "comment.created"
        assert worker_event["task_id"] == worker_task_id
        assert set(worker_event["metadata"]) == {"body_digest", "comment_id"}
        assert "The deterministic worker reached its checkpoint." not in str(worker_event)
        assert stale_session_queue.empty()
        assert other_owner_queue.empty()
    finally:
        for socket in (matching_socket, stale_session_socket, other_owner_socket):
            ws_manager.disconnect_work_board(socket)
        await engine.dispose()
