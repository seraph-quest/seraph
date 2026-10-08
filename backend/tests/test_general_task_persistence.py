"""Literal local persistence/readback; no inference credentials or transports."""
import socket
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

from config.settings import settings
from src.db.models import Goal, WorkBoardStatus, WorkBoardTask, WorkflowRunState
from src.work_board.contracts import WorkBoardOwner
from src.work_board.general_task import GeneralTaskService
from src.work_board.repository import BoardError
from tests.test_general_task_contract import Registry, request
from tests.test_work_board_m6_provider_free_journey import isolated_runtime, OWNER, SESSION


@pytest.fixture
def task_runtime(isolated_runtime):
    sessions, workspace = isolated_runtime
    @asynccontextmanager
    async def extended_sessions():
        async with sessions() as db:
            # The shared literal SQLite adapter predates input-artifact
            # reservations; provide the real transaction method they require.
            @asynccontextmanager
            async def begin():
                with db._session.begin():
                    yield db
            db.begin = begin
            yield db
    return extended_sessions, workspace


@pytest.fixture(autouse=True)
def deny_provider(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("general task test attempted external contact")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(settings, "openrouter_api_key", "")


@pytest.mark.asyncio
async def test_one_canonical_task_immutable_plan_and_owner_isolation(task_runtime):
    sessions, workspace = task_runtime
    owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)
    async with sessions() as db:
        db.add(Goal(id="goal-1", title="General task", owner_principal_id=OWNER,
                    owner_session_id=SESSION, revision=1))
    registry = Registry()
    service = GeneralTaskService(registry)
    service.start()
    async with sessions() as db:
        created = await service.create(db, owner, request(registry))
        task_id = created.task.task_id
        assert created.task.status == WorkBoardStatus.triage
        assert created.task.input_artifact_id
    async with sessions() as db:
        replay = await service.create(db, owner, request(registry))
        assert replay.task.task_id == task_id
        assert replay.idempotent_replay
    async with sessions() as db:
        projection = await service.plan(db, owner, task_id)
        assert projection["plan"]["steps"][0]["input"] == {"text": "hello"}
        assert projection["accepted"] is False
        assert len((await db.execute(select(WorkBoardTask))).scalars().all()) == 1
        assert len((await db.execute(select(WorkflowRunState))).scalars().all()) == 0
    async with sessions() as db:
        with pytest.raises(BoardError):
            await service.plan(db, WorkBoardOwner(principal_id="other", session_id=SESSION), task_id)
    assert registry.calls == []


@pytest.mark.asyncio
async def test_invalid_tool_and_changed_schema_publish_no_task(task_runtime):
    sessions, workspace = task_runtime
    owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)
    async with sessions() as db:
        db.add(Goal(id="goal-1", title="General task", owner_principal_id=OWNER,
                    owner_session_id=SESSION, revision=1))
    registry = Registry()
    service = GeneralTaskService(registry)
    service.start()
    bad = request(registry)
    bad = bad.model_copy(update={"plan": bad.plan.model_copy(update={"steps": [
        bad.plan.steps[0].model_copy(update={"tool_id": "unknown.tool"})]})})
    async with sessions() as db:
        with pytest.raises(BoardError, match="registered tool"):
            await service.create(db, owner, bad)
        assert len((await db.execute(select(WorkBoardTask))).scalars().all()) == 0
        assert len((await db.execute(select(WorkflowRunState))).scalars().all()) == 0
