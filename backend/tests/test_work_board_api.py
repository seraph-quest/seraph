"""Authenticated HTTP contract checks for work-board M1."""

import json

import pytest

from src.api.work_board import _attempt_payload, _task_payload as serialize_task_payload
from src.db.models import WorkBoardAttempt, WorkBoardTask


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


@pytest.mark.asyncio
async def test_http_comments_links_and_status_action_success(client):
    goal_id = await _create_goal(client, goal_id="goal-http-success")
    parent_payload = _task_payload(key="http-parent")
    parent_payload["goal_id"] = goal_id
    child_payload = _task_payload(key="http-child")
    child_payload["goal_id"] = goal_id
    parent = await client.post("/api/work-board/tasks", json=parent_payload)
    child = await client.post("/api/work-board/tasks", json=child_payload)
    assert parent.status_code == child.status_code == 200
    parent_id = parent.json()["task"]["task_id"]
    child_data = child.json()["task"]
    child_id = child_data["task_id"]

    comment = await client.post(
        f"/api/work-board/tasks/{child_id}/comments",
        json={"expected_revision": 1, "body": "operator handoff"},
    )
    assert comment.status_code == 200
    assert comment.json()["comment"]["body"] == "operator handoff"

    link = await client.post(
        "/api/work-board/links",
        json={
            "parent_task_id": parent_id,
            "child_task_id": child_id,
            "expected_child_revision": 2,
        },
    )
    assert link.status_code == 200
    assert link.json()["link"]["parent_task_id"] == parent_id

    blocked = await client.post(
        f"/api/work-board/tasks/{child_id}/actions",
        json={
            "action": "block",
            "expected_revision": 3,
            "reason": "Operator needs to revise the specification",
        },
    )
    assert blocked.status_code == 200
    assert blocked.json()["task"]["status"] == "blocked"


def test_detail_reference_serializers_drop_unknown_private_values():
    attempt = WorkBoardAttempt(
        task_id="task-safe-refs",
        executor_id="executor.local",
        workflow_run_id="/private/run-secret",
        receipt_refs_json=json.dumps(
            [
                {
                    "job_id": "job:1",
                    "workflow_run_id": "/private/run-secret",
                    "status": "succeeded",
                    "verified": True,
                    "file_path": "/private/source.txt",
                    "summary": "PRIVATE SOURCE BODY",
                    "readback_status": "verified",
                    "verification_status": "passed",
                }
            ]
        ),
    )
    task = WorkBoardTask(
        task_id="task-safe-refs",
        owner_principal_id="operator:test-bypass",
        owner_session_id="test-auth-bypass",
        goal_id="goal-safe-refs",
        title="Safe refs",
        result_refs_json=json.dumps(
            [
                {
                    "artifact_id": "artifact:1",
                    "workflow_run_id": "private/run",
                    "secret": "DO NOT SERIALIZE",
                    "body": "PRIVATE RESULT BODY",
                }
            ]
        ),
        artifact_refs_json=json.dumps(
            [
                {
                    "artifact_id": "artifact:2",
                    "file_path": "/private/artifact.txt",
                    "private_source": "PRIVATE ARTIFACT BODY",
                }
            ]
        ),
    )

    attempt_payload = _attempt_payload(attempt)
    task_payload = serialize_task_payload(task)
    serialized = json.dumps({"attempt": attempt_payload, "task": task_payload})
    assert attempt_payload["workflow_run_id"] is None
    assert attempt_payload["receipt_refs"] == [
        {
            "job_id": "job:1",
            "status": "succeeded",
            "verified": True,
            "readback_status": "verified",
            "verification_status": "passed",
        }
    ]
    assert task_payload["result_refs"] == [{"artifact_id": "artifact:1"}]
    assert task_payload["artifact_refs"] == [{"artifact_id": "artifact:2"}]
    assert "PRIVATE" not in serialized
    assert "/private" not in serialized
