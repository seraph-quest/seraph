"""Authenticated work-board WebSocket cursor ordering checks."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocketDisconnect

from src.api.ws import websocket_work_board_events
from src.scheduler.connection_manager import ws_manager
from src.work_board.contracts import WorkBoardOwner
from src.work_board.repository import BoardEventPage
from src.work_board.time import serialize_utc_datetime


OWNER = WorkBoardOwner(
    principal_id="operator:cursor-test",
    session_id="session:cursor-test",
)
OTHER_OWNER = WorkBoardOwner(
    principal_id="operator:other",
    session_id="session:other",
)


class _FakeBoardWebSocket:
    def __init__(self, *, after: int):
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
        await asyncio.Future()


class _QueuedEvents:
    def __init__(self, events: list[dict]):
        self.events = list(events)

    async def get(self):
        if self.events:
            return self.events.pop(0)
        raise WebSocketDisconnect()


def _operator():
    return SimpleNamespace(
        principal=SimpleNamespace(principal_id=OWNER.principal_id),
        session_id=OWNER.session_id,
    )


def _event(event_id: int, owner: WorkBoardOwner = OWNER):
    return SimpleNamespace(
        event_id=event_id,
        task_id=f"task-{event_id}",
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        kind="task.updated",
        metadata_json="{}",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _payload(event_id: int):
    return {
        "event_id": event_id,
        "task_id": f"task-{event_id}",
        "kind": "task.updated",
        "metadata": {},
        "created_at": "2026-01-01T00:00:00+00:00",
    }


def _patch_websocket(monkeypatch, list_events, queue):
    @asynccontextmanager
    async def fake_session():
        yield object()

    monkeypatch.setattr("src.api.ws.authenticate_websocket", AsyncMock(return_value=_operator()))
    monkeypatch.setattr("src.api.ws.auth_enabled", lambda: False)
    monkeypatch.setattr("src.api.ws.get_session", fake_session)
    monkeypatch.setattr("src.api.work_board.repository.list_events", list_events)
    monkeypatch.setattr("src.api.ws.ws_manager.connect_work_board", lambda *_args, **_kwargs: queue)
    monkeypatch.setattr("src.api.ws.ws_manager.disconnect_work_board", lambda _websocket: None)


@pytest.mark.asyncio
async def test_live_cursor_jump_replays_committed_events_before_advancing(monkeypatch):
    """A queued 12 cannot skip committed owner event 11."""

    list_events = AsyncMock(
        side_effect=[
            BoardEventPage(events=[], last_event_id=10, gap=False),
            BoardEventPage(
                events=[_event(11), _event(12)],
                last_event_id=12,
                gap=False,
            ),
        ]
    )
    # The post-commit publisher puts 12 on this socket before 11.
    queue = _QueuedEvents([_payload(12), _payload(11)])
    _patch_websocket(monkeypatch, list_events, queue)

    websocket = _FakeBoardWebSocket(after=10)
    await websocket_work_board_events(websocket)

    assert websocket.accepted is True
    assert [item["event_id"] for item in websocket.sent] == [11, 12]
    assert [call.kwargs["after"] for call in list_events.await_args_list] == [10, 10]


@pytest.mark.asyncio
async def test_websocket_replay_keeps_other_owner_and_session_events_hidden(monkeypatch):
    """The replay query carries the authenticated owner/session scope."""

    seen_owners: list[WorkBoardOwner] = []
    owner_event = _event(12, OWNER)
    hidden_principal_event = _event(11, OTHER_OWNER)
    hidden_session_event = _event(
        13,
        WorkBoardOwner(
            principal_id=OWNER.principal_id,
            session_id="session:stale",
        ),
    )

    async def list_events(_db, owner, *, after: int, limit: int):
        del limit
        seen_owners.append(owner)
        visible = [
            event
            for event in (hidden_principal_event, owner_event, hidden_session_event)
            if event.owner_principal_id == owner.principal_id
            and event.owner_session_id == owner.session_id
            and event.event_id > after
        ]
        return BoardEventPage(events=visible, last_event_id=12, gap=False)

    queue = _QueuedEvents([])
    _patch_websocket(monkeypatch, list_events, queue)

    websocket = _FakeBoardWebSocket(after=0)
    await websocket_work_board_events(websocket)

    assert websocket.sent == [
        {
            "event_id": owner_event.event_id,
            "task_id": owner_event.task_id,
            "kind": owner_event.kind,
            "metadata": {},
            "created_at": serialize_utc_datetime(owner_event.created_at),
        }
    ]
    assert seen_owners == [OWNER]
