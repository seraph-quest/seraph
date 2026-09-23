"""Security and cursor-boundary checks for the authenticated work board."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocketDisconnect

from config.settings import settings
from src.api.ws import websocket_work_board_events
from src.auth.service import AuthFailure
from src.db.models import Goal, WorkBoardAttempt, WorkBoardStatus
from src.scheduler.connection_manager import ws_manager
from src.work_board.contracts import (
    WorkBoardAction,
    WorkBoardActionRequest,
    WorkBoardCommentCreate,
    WorkBoardOwner,
    WorkBoardTaskCreate,
    WorkBoardTaskPatch,
)
from src.work_board.repository import (
    BoardError,
    BoardGoalRevisionConflict,
    BoardOwnerMismatch,
    WorkBoardRepository,
)
from src.work_board.repository import BoardEventPage


OWNER = WorkBoardOwner(
    principal_id="operator:test-bypass",
    session_id="test-auth-bypass",
)
OTHER_OWNER = WorkBoardOwner(
    principal_id="operator:other",
    session_id="other-session",
)


async def _seed_task(
    async_db,
    owner: WorkBoardOwner = OWNER,
    *,
    key_suffix: str = "a",
    assignee_id: str | None = None,
) -> str:
    async with async_db() as db:
        goal_id = f"goal-{owner.principal_id}"
        if await db.get(Goal, goal_id) is None:
            db.add(
                Goal(
                    id=goal_id,
                    title="Board security goal",
                    owner_principal_id=owner.principal_id,
                    owner_session_id=owner.session_id,
                    revision=1,
                )
            )
            await db.flush()
        mutation = await WorkBoardRepository().create_task(
            db,
            owner,
            WorkBoardTaskCreate(
                title="Owner scoped task",
                goal_id=goal_id,
                goal_revision=1,
                idempotency_key=f"security-{owner.principal_id}-{key_suffix}",
                assignee_id=assignee_id,
            ),
        )
        return mutation.task.task_id


@pytest.mark.asyncio
async def test_different_principal_cannot_read_or_write_task(async_db):
    task_id = await _seed_task(async_db, OWNER)
    repository = WorkBoardRepository()
    async with async_db() as db:
        with pytest.raises(BoardOwnerMismatch):
            await repository.get_task(db, OTHER_OWNER, task_id)
        with pytest.raises(BoardOwnerMismatch):
            await repository.patch_task(
                db,
                OTHER_OWNER,
                task_id,
                WorkBoardTaskPatch(expected_revision=1, title="cross-owner write"),
            )
        with pytest.raises(BoardOwnerMismatch):
            await repository.add_comment(
                db,
                OTHER_OWNER,
                task_id,
                WorkBoardCommentCreate(expected_revision=1, body="cross-owner comment"),
            )


@pytest.mark.asyncio
async def test_assignee_filter_is_owner_scoped(async_db):
    await _seed_task(async_db, OWNER, key_suffix="assignee-a", assignee_id="operator:a")
    await _seed_task(async_db, OWNER, key_suffix="assignee-b", assignee_id="operator:b")
    repository = WorkBoardRepository()
    async with async_db() as db:
        page = await repository.list_tasks(db, OWNER, assignee_id="operator:a")
        assert len(page.tasks) == 1
        assert page.tasks[0].assignee_id == "operator:a"


@pytest.mark.asyncio
async def test_http_assignee_filter_returns_only_matching_owner_tasks(client, async_db):
    await _seed_task(async_db, OWNER, key_suffix="http-assignee-a", assignee_id="operator:a")
    await _seed_task(async_db, OWNER, key_suffix="http-assignee-b", assignee_id="operator:b")

    response = await client.get("/api/work-board/tasks?assignee_id=operator%3Aa")

    assert response.status_code == 200
    assert [task["assignee_id"] for task in response.json()["tasks"]] == ["operator:a"]


@pytest.mark.asyncio
async def test_generic_unblock_requires_operator_block_and_current_goal(async_db):
    task_id = await _seed_task(async_db, OWNER)
    repository = WorkBoardRepository()
    async with async_db() as db:
        task = await repository.get_task(db, OWNER, task_id)
        task.status = WorkBoardStatus.blocked
        task.block_source_status = "triage"
        task.block_kind = "unknown_effect"
        task.block_reason = "External effect needs reconciliation"
        await db.commit()
        with pytest.raises(BoardError, match="typed recovery"):
            await repository.action_task(
                db,
                OWNER,
                task_id,
                WorkBoardActionRequest(
                    action=WorkBoardAction.unblock,
                    expected_revision=task.task_revision,
                ),
            )

    async with async_db() as db:
        task = await repository.get_task(db, OWNER, task_id)
        task.block_kind = "operator"
        expected_revision = task.task_revision
        await db.flush()
        goal = await db.get(Goal, "goal-operator:test-bypass")
        assert goal is not None
        goal.revision = 2
        await db.commit()

    async with async_db() as db:
        with pytest.raises(BoardGoalRevisionConflict):
            await repository.action_task(
                db,
                OWNER,
                task_id,
                WorkBoardActionRequest(
                    action=WorkBoardAction.unblock,
                    expected_revision=expected_revision,
                ),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [None, "corrupt", "running", "done", "archived"])
async def test_generic_unblock_rejects_unsafe_restorable_phase(async_db, source):
    task_id = await _seed_task(async_db, OWNER, key_suffix=f"unsafe-phase-{source}")
    repository = WorkBoardRepository()
    async with async_db() as db:
        task = await repository.get_task(db, OWNER, task_id)
        task.status = WorkBoardStatus.blocked
        task.block_source_status = source
        task.block_kind = "operator"
        task.block_reason = "Operator recovery"
        await db.commit()

    async with async_db() as db:
        current = await repository.get_task(db, OWNER, task_id)
        with pytest.raises(BoardError) as raised:
            await repository.action_task(
                db,
                OWNER,
                task_id,
                WorkBoardActionRequest(
                    action=WorkBoardAction.unblock,
                    expected_revision=current.task_revision,
                ),
            )
        assert raised.value.code == "invalid_recovery_phase"
        assert current.status is WorkBoardStatus.blocked


@pytest.mark.asyncio
async def test_generic_unblock_demotes_ready_phase_for_fresh_admission(async_db):
    task_id = await _seed_task(async_db, OWNER, key_suffix="ready-phase")
    repository = WorkBoardRepository()
    async with async_db() as db:
        task = await repository.get_task(db, OWNER, task_id)
        task.status = WorkBoardStatus.blocked
        task.block_source_status = WorkBoardStatus.ready.value
        task.block_kind = "operator"
        task.block_reason = "Operator recovery"
        await db.commit()

    async with async_db() as db:
        current = await repository.get_task(db, OWNER, task_id)
        mutation = await repository.action_task(
            db,
            OWNER,
            task_id,
            WorkBoardActionRequest(
                action=WorkBoardAction.unblock,
                expected_revision=current.task_revision,
            ),
        )
        assert mutation.task.status is WorkBoardStatus.todo


@pytest.mark.asyncio
async def test_generic_unblock_refuses_operator_block_with_existing_attempt(async_db):
    task_id = await _seed_task(async_db, OWNER)
    repository = WorkBoardRepository()
    async with async_db() as db:
        blocked = await repository.action_task(
            db,
            OWNER,
            task_id,
            WorkBoardActionRequest(
                action=WorkBoardAction.block,
                expected_revision=1,
                block_kind="operator",
                reason="Operator needs to revise the specification",
            ),
        )
        db.add(
            WorkBoardAttempt(
                task_id=task_id,
                executor_id="executor.test",
                fencing_token=1,
            )
        )
        await db.commit()

    async with async_db() as db:
        with pytest.raises(BoardError, match="execution attempt"):
            await repository.action_task(
                db,
                OWNER,
                task_id,
                WorkBoardActionRequest(
                    action=WorkBoardAction.unblock,
                    expected_revision=blocked.task.task_revision,
                ),
            )


@pytest.mark.asyncio
async def test_http_cross_owner_read_and_write_are_denied(client, async_db):
    task_id = await _seed_task(async_db, OTHER_OWNER)

    read = await client.get(f"/api/work-board/tasks/{task_id}")
    assert read.status_code == 403
    assert read.json()["detail"]["code"] == "task_owner_mismatch"

    write = await client.patch(
        f"/api/work-board/tasks/{task_id}",
        json={"expected_revision": 1, "title": "cross-owner write"},
    )
    assert write.status_code == 403
    assert write.json()["detail"]["code"] == "task_owner_mismatch"


@pytest.mark.asyncio
async def test_http_board_rejects_anonymous_and_revoked_session(client, monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_secret", "board-test-secret")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://test")
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)

    anonymous = await client.get("/api/work-board/tasks", headers={"host": "test"})
    assert anonymous.status_code == 401
    assert anonymous.json()["detail"]["code"] == "authentication_required"

    login = await client.post(
        "/api/auth/login",
        json={"password": "board-test-secret"},
        headers={"host": "test", "origin": "http://test"},
    )
    assert login.status_code == 200
    token = login.cookies.get(settings.operator_auth_cookie_name)
    assert token
    client.cookies.set(settings.operator_auth_cookie_name, token)

    authenticated = await client.get("/api/work-board/tasks", headers={"host": "test"})
    assert authenticated.status_code == 200

    logout = await client.post(
        "/api/auth/logout",
        headers={"host": "test", "origin": "http://test"},
    )
    assert logout.status_code == 204
    # Keep presenting the server-issued token after logout so the middleware
    # proves revocation, rather than merely proving a missing cookie.
    client.cookies.set(settings.operator_auth_cookie_name, token)
    revoked = await client.get("/api/work-board/tasks", headers={"host": "test"})
    assert revoked.status_code == 401
    assert revoked.json()["detail"]["code"] == "session_revoked"


class _FakeBoardWebSocket:
    def __init__(self, after: int = 0):
        self.query_params = {"after": str(after)}
        self.headers = {"host": "test"}
        self.accepted = False
        self.closed: tuple[int, str] | None = None
        self.sent: list[dict] = []

    async def accept(self):
        self.accepted = True

    async def close(self, *, code: int, reason: str):
        self.closed = (code, reason)

    async def send_json(self, payload: dict):
        self.sent.append(payload)

    async def receive(self):
        """Keep the server-side disconnect watcher pending for replay tests."""
        await asyncio.Future()


class _IdleDisconnectBoardWebSocket(_FakeBoardWebSocket):
    async def receive(self):
        await asyncio.sleep(0)
        raise WebSocketDisconnect()


class _PendingReceiveBoardWebSocket(_FakeBoardWebSocket):
    def __init__(self, after: int = 0):
        super().__init__(after=after)
        self.receive_cancelled = False
        self.fail_send = False

    async def receive(self):
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.receive_cancelled = True
            raise

    async def send_json(self, payload: dict):
        if self.fail_send:
            raise WebSocketDisconnect()
        await super().send_json(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_code", ["authentication_required", "session_revoked"])
async def test_work_board_websocket_rejects_unauthorized_or_stale_session(
    monkeypatch,
    failure_code: str,
):
    monkeypatch.setattr(
        "src.api.ws.authenticate_websocket",
        AsyncMock(side_effect=AuthFailure(failure_code)),
    )
    websocket = _FakeBoardWebSocket()

    await websocket_work_board_events(websocket)

    assert websocket.accepted is False
    assert websocket.closed == (4401, failure_code)


class _StopAfterReplayQueue:
    async def get(self):
        raise WebSocketDisconnect()


def _event(event_id: int, kind: str):
    return SimpleNamespace(
        event_id=event_id,
        task_id="task-cursor",
        kind=kind,
        metadata_json="{}",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_work_board_websocket_replays_after_cursor_and_marks_gap_on_reconnect(
    monkeypatch,
):
    operator = SimpleNamespace(
        principal=SimpleNamespace(principal_id=OWNER.principal_id),
        session_id=OWNER.session_id,
    )
    pages = [
        BoardEventPage(
            events=[_event(12, "task.created"), _event(13, "task.updated")],
            last_event_id=13,
            gap=False,
        ),
        BoardEventPage(events=[], last_event_id=21, gap=True),
    ]
    list_events = AsyncMock(side_effect=pages)

    @asynccontextmanager
    async def fake_session():
        yield object()

    monkeypatch.setattr("src.api.ws.authenticate_websocket", AsyncMock(return_value=operator))
    monkeypatch.setattr("src.api.ws.auth_enabled", lambda: False)
    monkeypatch.setattr("src.api.ws.get_session", fake_session)
    monkeypatch.setattr("src.api.work_board.repository.list_events", list_events)
    monkeypatch.setattr(
        "src.api.ws.ws_manager.connect_work_board",
        lambda *_args, **_kwargs: _StopAfterReplayQueue(),
    )
    monkeypatch.setattr(
        "src.api.ws.ws_manager.disconnect_work_board",
        lambda _websocket: None,
    )

    replay = _FakeBoardWebSocket(after=11)
    await websocket_work_board_events(replay)
    assert replay.accepted is True
    assert [item["event_id"] for item in replay.sent] == [12, 13]
    assert list_events.await_args_list[0].kwargs["after"] == 11

    reconnect = _FakeBoardWebSocket(after=13)
    await websocket_work_board_events(reconnect)
    assert reconnect.accepted is True
    assert reconnect.sent == [{"type": "cursor_gap", "last_event_id": 21}]
    assert list_events.await_args_list[1].kwargs["after"] == 13


@pytest.mark.asyncio
async def test_work_board_websocket_paginates_persisted_backlog_before_live_queue(
    monkeypatch,
):
    operator = SimpleNamespace(
        principal=SimpleNamespace(principal_id=OWNER.principal_id),
        session_id=OWNER.session_id,
    )
    pages = [
        BoardEventPage(
            events=[_event(event_id, "task.updated") for event_id in range(1, 101)],
            last_event_id=100,
            gap=False,
        ),
        BoardEventPage(
            events=[_event(event_id, "task.updated") for event_id in range(101, 201)],
            last_event_id=200,
            gap=False,
        ),
        BoardEventPage(
            events=[_event(201, "task.done")],
            last_event_id=201,
            gap=False,
        ),
    ]
    list_events = AsyncMock(side_effect=pages)

    @asynccontextmanager
    async def fake_session():
        yield object()

    monkeypatch.setattr("src.api.ws.authenticate_websocket", AsyncMock(return_value=operator))
    monkeypatch.setattr("src.api.ws.auth_enabled", lambda: False)
    monkeypatch.setattr("src.api.ws.get_session", fake_session)
    monkeypatch.setattr("src.api.work_board.repository.list_events", list_events)
    monkeypatch.setattr(
        "src.api.ws.ws_manager.connect_work_board",
        lambda *_args, **_kwargs: _StopAfterReplayQueue(),
    )
    monkeypatch.setattr(
        "src.api.ws.ws_manager.disconnect_work_board",
        lambda _websocket: None,
    )

    websocket = _FakeBoardWebSocket(after=0)
    await websocket_work_board_events(websocket)

    assert [item["event_id"] for item in websocket.sent] == list(range(1, 202))
    assert [call.kwargs["after"] for call in list_events.await_args_list] == [0, 100, 200]


@pytest.mark.asyncio
async def test_work_board_websocket_idle_disconnect_unregisters_queue_and_binding(
    monkeypatch,
):
    operator = SimpleNamespace(
        principal=SimpleNamespace(principal_id=OWNER.principal_id),
        session_id=OWNER.session_id,
    )

    @asynccontextmanager
    async def fake_session():
        yield object()

    monkeypatch.setattr("src.api.ws.authenticate_websocket", AsyncMock(return_value=operator))
    monkeypatch.setattr("src.api.ws.auth_enabled", lambda: False)
    monkeypatch.setattr("src.api.ws.get_session", fake_session)
    monkeypatch.setattr(
        "src.api.work_board.repository.list_events",
        AsyncMock(return_value=BoardEventPage(events=[], last_event_id=0, gap=False)),
    )

    websocket = _IdleDisconnectBoardWebSocket()
    await websocket_work_board_events(websocket)

    assert websocket not in ws_manager._work_board_connections
    assert websocket not in ws_manager._work_board_bindings
    assert websocket not in ws_manager._work_board_queues


@pytest.mark.asyncio
async def test_work_board_websocket_revocation_cleans_receive_and_event_waiters(
    monkeypatch,
):
    operator = SimpleNamespace(
        principal=SimpleNamespace(principal_id=OWNER.principal_id),
        session_id=OWNER.session_id,
    )

    @asynccontextmanager
    async def fake_session():
        yield object()

    queue_holder: dict[str, asyncio.Queue] = {}
    original_connect = ws_manager.connect_work_board

    def connect_work_board(*args, **kwargs):
        queue = original_connect(*args, **kwargs)
        queue_holder["queue"] = queue
        return queue

    watcher_cancelled = asyncio.Event()

    async def fake_watch(websocket, _session_id, revoked_event, _revocation_guard):
        revoked_event.set()
        await websocket.close(code=4401, reason="session_revoked")
        websocket.fail_send = True
        queue_holder["queue"].put_nowait({"event_id": 1, "type": "task.updated"})
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            watcher_cancelled.set()
            raise

    monkeypatch.setattr("src.api.ws.authenticate_websocket", AsyncMock(return_value=operator))
    monkeypatch.setattr("src.api.ws.auth_enabled", lambda: True)
    monkeypatch.setattr("src.api.ws.watch_operator_session", fake_watch)
    monkeypatch.setattr("src.api.ws.get_session", fake_session)
    monkeypatch.setattr("src.api.ws.ws_manager.connect_work_board", connect_work_board)
    monkeypatch.setattr(
        "src.api.work_board.repository.list_events",
        AsyncMock(return_value=BoardEventPage(events=[], last_event_id=0, gap=False)),
    )

    websocket = _PendingReceiveBoardWebSocket()
    await websocket_work_board_events(websocket)

    assert websocket.closed == (4401, "session_revoked")
    assert websocket.receive_cancelled is True
    assert watcher_cancelled.is_set()
    assert websocket not in ws_manager._work_board_connections
    assert websocket not in ws_manager._work_board_bindings
    assert websocket not in ws_manager._work_board_queues
    assert not any(
        task.get_name() in {"work-board-disconnect-wait", "work-board-event-wait"}
        and not task.done()
        for task in asyncio.all_tasks()
    )
