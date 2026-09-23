"""Authenticated HTTP contract checks for work-board M1."""

import pytest


def _task_payload(*, key: str = "api-task"):
    return {
        "title": "API task",
        "body": "bounded intent",
        "goal_id": "goal-api",
        "goal_revision": 1,
        "idempotency_scope": "test",
        "idempotency_key": key,
    }


async def _create_goal(client, *, goal_id: str = "goal-api"):
    response = await client.post(
        "/api/goals",
        json={"title": "Board API goal", "level": "daily", "domain": "productivity"},
    )
    assert response.status_code == 200
    return response.json()["id"]


@pytest.mark.asyncio
async def test_create_and_read_task(client):
    payload = _task_payload()
    payload["goal_id"] = await _create_goal(client)
    created = await client.post("/api/work-board/tasks", json=payload)
    assert created.status_code == 200
    task = created.json()["task"]
    assert task["status"] == "triage"
    fetched = await client.get(f"/api/work-board/tasks/{task['task_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["task"]["task_id"] == task["task_id"]
    assert fetched.json()["events"][0]["kind"] == "task.created"


@pytest.mark.asyncio
async def test_duplicate_idempotency_returns_same_task(client):
    payload = _task_payload(key="duplicate")
    payload["goal_id"] = await _create_goal(client)
    first = await client.post("/api/work-board/tasks", json=payload)
    second = await client.post("/api/work-board/tasks", json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json()["task"]["task_id"] == second.json()["task"]["task_id"]
    assert second.json()["idempotent_replay"] is True


@pytest.mark.asyncio
async def test_stale_revision_returns_typed_conflict(client):
    payload = _task_payload(key="stale")
    payload["goal_id"] = await _create_goal(client)
    created = await client.post("/api/work-board/tasks", json=payload)
    task = created.json()["task"]
    response = await client.patch(
        f"/api/work-board/tasks/{task['task_id']}",
        json={"expected_revision": 99, "title": "changed"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "stale_revision"


@pytest.mark.asyncio
async def test_event_cursor_page_and_gap_shape(client):
    payload = _task_payload(key="events")
    payload["goal_id"] = await _create_goal(client)
    created = await client.post("/api/work-board/tasks", json=payload)
    assert created.status_code == 200
    events = await client.get("/api/work-board/events?after=0")
    assert events.status_code == 200
    payload = events.json()
    assert payload["gap"] is False
    assert payload["last_event_id"] >= 1
    assert payload["events"][0]["kind"] == "task.created"


@pytest.mark.asyncio
async def test_invalid_running_or_done_patch_is_rejected(client, async_db):
    payload = _task_payload(key="illegal")
    payload["goal_id"] = await _create_goal(client)
    created = await client.post("/api/work-board/tasks", json=payload)
    task_id = created.json()["task"]["task_id"]
    from src.db.models import WorkBoardStatus
    from src.db.engine import get_session
    from src.work_board.contracts import WorkBoardOwner
    from src.api.work_board import repository

    # The route does not expose a generic status patch.  This check exercises
    # the repository guard used by M2/M4 integrations.
    async with async_db() as db:
        task = await repository.get_task(
            db,
            WorkBoardOwner(principal_id="operator:test-bypass", session_id="test-auth-bypass"),
            task_id,
        )
        task.status = WorkBoardStatus.running
        await db.flush()
        from src.work_board.contracts import WorkBoardTaskPatch
        with pytest.raises(Exception):
            await repository.patch_task(
                db,
                WorkBoardOwner(principal_id="operator:test-bypass", session_id="test-auth-bypass"),
                task_id,
                WorkBoardTaskPatch(expected_revision=task.task_revision, title="blocked"),
            )
