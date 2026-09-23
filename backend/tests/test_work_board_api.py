"""Authenticated HTTP contract checks for work-board M1."""

import json

import pytest

from src.api.work_board import (
    _attempt_payload,
    _event_payload,
    _task_payload as serialize_task_payload,
)
from src.db.models import WorkBoardAttempt, WorkBoardEvent, WorkBoardStatus, WorkBoardTask


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
async def test_http_patch_validation_returns_422_for_malformed_inputs(client):
    payload = _task_payload(key="invalid-patch")
    payload["goal_id"] = await _create_goal(client)
    created = await client.post("/api/work-board/tasks", json=payload)
    task = created.json()["task"]

    one_sided = await client.patch(
        f"/api/work-board/tasks/{task['task_id']}",
        json={
            "expected_revision": task["task_revision"],
            "typed_input_ref": "workspace-json:inputs/task.json",
            "typed_input_digest": None,
        },
    )
    assert one_sided.status_code == 422

    unsafe_reference = await client.patch(
        f"/api/work-board/tasks/{task['task_id']}",
        json={
            "expected_revision": task["task_revision"],
            "capability_id": "guardian/research",
        },
    )
    assert unsafe_reference.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ["capability_id", "executor_id", "assignee_id", "reviewer_id", "origin_thread_id"],
)
async def test_http_create_rejects_path_shaped_opaque_identifiers(client, field):
    payload = _task_payload(key=f"invalid-create-{field}")
    payload["goal_id"] = await _create_goal(client)
    payload[field] = "guardian/research"

    rejected = await client.post("/api/work-board/tasks", json=payload)
    assert rejected.status_code == 422

    payload[field] = "guardian.research"
    accepted = await client.post("/api/work-board/tasks", json=payload)
    assert accepted.status_code == 200


@pytest.mark.asyncio
async def test_http_typed_input_relative_path_round_trips(client):
    payload = _task_payload(key="typed-http")
    payload["goal_id"] = await _create_goal(client)
    payload.update(
        {
            "capability_id": "guardian.research",
            "typed_input_ref": "workspace-json:inputs/task.json",
            "typed_input_digest": "c" * 64,
        }
    )

    created = await client.post("/api/work-board/tasks", json=payload)
    assert created.status_code == 200
    task = created.json()["task"]
    assert task["typed_input_ref"] == payload["typed_input_ref"]
    assert task["typed_input_digest"] == payload["typed_input_digest"]

    fetched = await client.get(f"/api/work-board/tasks/{task['task_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["task"]["typed_input_ref"] == payload["typed_input_ref"]


@pytest.mark.asyncio
async def test_http_todo_creation_requires_capability_but_not_executor(client):
    payload = _task_payload(key="todo-capability-gate")
    payload["goal_id"] = await _create_goal(client)
    payload.update(
        {
            "status": "todo",
            "typed_input_ref": "workspace-json:inputs/todo.json",
            "typed_input_digest": "d" * 64,
        }
    )

    missing_capability = await client.post("/api/work-board/tasks", json=payload)
    assert missing_capability.status_code == 422

    payload["capability_id"] = "capability.local"
    created = await client.post("/api/work-board/tasks", json=payload)
    assert created.status_code == 200
    assert created.json()["task"]["status"] == "todo"
    assert created.json()["task"]["executor_id"] is None


@pytest.mark.asyncio
async def test_http_create_rejects_unsafe_origin_thread_reference(client):
    payload = _task_payload(key="unsafe-origin-thread")
    payload["goal_id"] = await _create_goal(client)
    payload["origin_thread_id"] = "../private/thread"

    response = await client.post("/api/work-board/tasks", json=payload)

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_http_manual_block_accepts_only_operator_kind(client):
    payload = _task_payload(key="invalid-block-kind")
    payload["goal_id"] = await _create_goal(client)
    created = await client.post("/api/work-board/tasks", json=payload)
    task = created.json()["task"]

    response = await client.post(
        f"/api/work-board/tasks/{task['task_id']}/actions",
        json={
            "action": "block",
            "expected_revision": task["task_revision"],
            "block_kind": "unknown_effect",
            "reason": "This category belongs to internal reconciliation",
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_http_stale_comment_link_and_action_return_typed_conflicts(client):
    goal_id = await _create_goal(client, goal_id="goal-http-stale-actions")
    parent_payload = _task_payload(key="stale-parent")
    parent_payload["goal_id"] = goal_id
    child_payload = _task_payload(key="stale-child")
    child_payload["goal_id"] = goal_id
    parent = await client.post("/api/work-board/tasks", json=parent_payload)
    child = await client.post("/api/work-board/tasks", json=child_payload)
    parent_id = parent.json()["task"]["task_id"]
    child_data = child.json()["task"]
    child_id = child_data["task_id"]

    comment = await client.post(
        f"/api/work-board/tasks/{child_id}/comments",
        json={"expected_revision": 99, "body": "stale comment"},
    )
    assert comment.status_code == 409
    assert comment.json()["detail"]["code"] == "stale_revision"

    action = await client.post(
        f"/api/work-board/tasks/{child_id}/actions",
        json={
            "action": "block",
            "expected_revision": 99,
            "reason": "stale action",
        },
    )
    assert action.status_code == 409
    assert action.json()["detail"]["code"] == "stale_revision"

    link = await client.post(
        "/api/work-board/links",
        json={
            "parent_task_id": parent_id,
            "child_task_id": child_id,
            "expected_child_revision": 99,
        },
    )
    assert link.status_code == 409
    assert link.json()["detail"]["code"] == "stale_revision"


@pytest.mark.asyncio
async def test_http_link_to_running_child_returns_typed_conflict(client, async_db):
    goal_id = await _create_goal(client, goal_id="goal-http-running-link")
    parent_payload = _task_payload(key="running-parent")
    parent_payload["goal_id"] = goal_id
    child_payload = _task_payload(key="running-child")
    child_payload["goal_id"] = goal_id
    parent = await client.post("/api/work-board/tasks", json=parent_payload)
    child = await client.post("/api/work-board/tasks", json=child_payload)
    parent_id = parent.json()["task"]["task_id"]
    child_id = child.json()["task"]["task_id"]

    from src.api.work_board import repository
    from src.work_board.contracts import WorkBoardOwner

    async with async_db() as db:
        running = await repository.get_task(
            db,
            WorkBoardOwner(principal_id="operator:test-bypass", session_id="test-auth-bypass"),
            child_id,
        )
        running.status = WorkBoardStatus.running
        await db.commit()

    response = await client.post(
        "/api/work-board/links",
        json={
            "parent_task_id": parent_id,
            "child_task_id": child_id,
            "expected_child_revision": 1,
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "running_task_dependency"


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
            "block_kind": "operator",
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
                    "artifact_id": "/private/run",
                    "effect_id": "runs/effect",
                    "child_job_id": "child/jobs",
                    "artifact_type": "goal_snapshot",
                    "effect_type": "readback",
                    "workflow_run_id": "/private/run-secret",
                    "status": "succeeded",
                    "verified": True,
                    "file_path": "/private/source.txt",
                    "target_path": "artifacts/result.txt",
                    "summary": "PRIVATE SOURCE BODY",
                    "readback_status": "verified",
                    "verification_status": "passed",
                },
                {
                    "artifact_id": ".",
                    "effect_id": ".",
                    "job_id": "..",
                    "child_job_id": "..",
                    "artifact_type": "private/type",
                    "effect_type": "effects/type",
                    "status": "succeeded",
                },
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
                    "file_path": "artifacts/result.txt",
                    "target_path": "reports/result.txt",
                    "secret": "DO NOT SERIALIZE",
                    "body": "PRIVATE RESULT BODY",
                }
            ]
        ),
        artifact_refs_json=json.dumps(
            [
                {
                    "artifact_id": "artifact:2",
                    "effect_id": "/private/run",
                    "job_id": "jobs/run",
                    "child_job_id": "child/jobs",
                    "file_path": "/private/artifact.txt",
                    "target_path": "derived/result.txt",
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
            "artifact_type": "goal_snapshot",
            "effect_type": "readback",
            "target_path": "artifacts/result.txt",
            "status": "succeeded",
            "verified": True,
            "readback_status": "verified",
            "verification_status": "passed",
        },
        {"status": "succeeded"},
    ]
    assert task_payload["result_refs"] == [
        {
            "artifact_id": "artifact:1",
            "file_path": "artifacts/result.txt",
            "target_path": "reports/result.txt",
        }
    ]
    assert task_payload["artifact_refs"] == [
        {"artifact_id": "artifact:2", "target_path": "derived/result.txt"}
    ]
    assert "PRIVATE" not in serialized
    assert "/private" not in serialized


def test_task_and_attempt_serializers_filter_unsafe_operator_references():
    unsafe_task = WorkBoardTask(
        task_id="task-unsafe-fields",
        owner_principal_id="operator:test-bypass",
        owner_session_id="test-auth-bypass",
        goal_id="goal-unsafe-fields",
        title="Unsafe fields",
        origin_session_id="/private/session",
        origin_thread_id="/private/thread",
        capability_id="../capability",
        typed_input_ref="/private/input.json",
        typed_input_digest="not-a-digest",
        executor_id="runs/executor",
        assignee_id="~/assignee",
        reviewer_id="../reviewer",
        block_kind="private block prose",
        block_source_status="/private/status",
    )
    unsafe_attempt = WorkBoardAttempt(
        task_id=unsafe_task.task_id,
        lease_owner="/private/lease",
        executor_id="../executor",
    )

    unsafe_payload = serialize_task_payload(unsafe_task)
    unsafe_attempt_payload = _attempt_payload(unsafe_attempt)
    assert unsafe_payload["origin_thread_id"] is None
    assert unsafe_payload["origin_session_id"] is None
    assert unsafe_payload["capability_id"] is None
    assert unsafe_payload["typed_input_ref"] is None
    assert unsafe_payload["typed_input_digest"] is None
    assert unsafe_payload["executor_id"] is None
    assert unsafe_payload["assignee_id"] is None
    assert unsafe_payload["reviewer_id"] is None
    assert unsafe_payload["block_kind"] is None
    assert unsafe_payload["block_source_status"] is None
    assert unsafe_attempt_payload["lease_owner"] is None
    assert unsafe_attempt_payload["executor_id"] is None

    safe_task = WorkBoardTask(
        task_id="task-safe-fields",
        owner_principal_id="operator:test-bypass",
        owner_session_id="test-auth-bypass",
        goal_id="goal-safe-fields",
        title="Safe fields",
        origin_session_id="session:42",
        origin_thread_id="thread:42",
        capability_id="guardian.research",
        typed_input_ref="workspace-json:inputs/task.json",
        typed_input_digest="b" * 64,
        executor_id="executor.local",
        assignee_id="operator:worker",
        reviewer_id="operator:reviewer",
        block_kind="unknown_effect",
        block_source_status="todo",
    )
    safe_attempt = WorkBoardAttempt(
        task_id=safe_task.task_id,
        lease_owner="worker:1",
        executor_id="executor.local",
    )
    safe_payload = serialize_task_payload(safe_task)
    safe_attempt_payload = _attempt_payload(safe_attempt)
    assert safe_payload["origin_thread_id"] == "thread:42"
    assert safe_payload["origin_session_id"] == "session:42"
    assert safe_payload["capability_id"] == "guardian.research"
    assert safe_payload["typed_input_ref"] == "workspace-json:inputs/task.json"
    assert safe_payload["typed_input_digest"] == "b" * 64
    assert safe_payload["executor_id"] == "executor.local"
    assert safe_payload["assignee_id"] == "operator:worker"
    assert safe_payload["reviewer_id"] == "operator:reviewer"
    assert safe_payload["block_kind"] == "unknown_effect"
    assert safe_payload["block_source_status"] == "todo"
    assert safe_attempt_payload["lease_owner"] == "worker:1"
    assert safe_attempt_payload["executor_id"] == "executor.local"


def test_event_and_attempt_serializers_drop_legacy_prose_secrets_and_paths():
    event = WorkBoardEvent(
        event_id=7,
        task_id="task-safe-event",
        kind="/private/event-kind",
        metadata_json=json.dumps(
            {
                "status": "triage",
                "task_revision": 3,
                "body": "PRIVATE EVENT BODY",
                "path": "/private/source.txt",
                "reason_code": "PRIVATE SECRET",
                "parent_task_id": "/private/parent",
                "changed_fields": ["title", "private_field"],
                "body_digest": "A" * 64,
            }
        ),
    )
    attempt = WorkBoardAttempt(
        task_id="task-safe-event",
        outcome="PRIVATE OUTCOME",
        workflow_run_id="run:7",
    )

    event_payload = _event_payload(event)
    attempt_payload = _attempt_payload(attempt)
    serialized = json.dumps({"event": event_payload, "attempt": attempt_payload})

    assert event_payload["kind"] == "event.unknown"
    assert event_payload["metadata"] == {
        "status": "triage",
        "task_revision": 3,
        "changed_fields": ["title"],
        "body_digest": "a" * 64,
    }
    assert attempt_payload["outcome"] is None
    assert attempt_payload["workflow_run_id"] == "run:7"
    assert "PRIVATE" not in serialized
    assert "/private" not in serialized
