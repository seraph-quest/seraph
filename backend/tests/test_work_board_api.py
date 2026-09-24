"""Authenticated HTTP contract checks for work-board M1."""

import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.api.work_board import (
    _attempt_payload,
    _event_payload,
    _recovery_action,
    _task_payload as serialize_task_payload,
)
from src.api.workflows import _bounded_lineage_expansion, _safe_board_job_projection
from src.db.models import (
    WorkBoardAttempt,
    WorkBoardEvent,
    WorkBoardStatus,
    WorkBoardTask,
    WorkflowRunState,
)


def test_running_task_keeps_cancel_control_visible_while_cancellation_is_pending():
    task = WorkBoardTask(
        task_id="cancel-pending-task",
        owner_principal_id="operator:test",
        owner_session_id="session:test",
        goal_id="goal:test",
        goal_revision=1,
        title="Cancel pending",
        idempotency_key="cancel-pending-task",
        status=WorkBoardStatus.running,
    )
    attempt = WorkBoardAttempt(
        attempt_id="cancel-pending-attempt",
        task_id=task.task_id,
        workflow_run_id="workflow:cancel-pending",
        task_revision_at_claim=1,
        lease_owner="executor:test",
        fencing_token=1,
        executor_id="executor.test",
        cancel_requested_at=datetime.now(timezone.utc),
    )

    assert _recovery_action(task, latest_attempt=attempt, attempt_count=1) == "cancel"


def test_board_job_projection_redacts_unsafe_receipt_ids_paths_and_types():
    safe_artifact_id = "art_" + "a" * 24
    run = WorkflowRunState(
        run_identity="work-board:projection-safe",
        root_run_identity="work-board:projection-safe",
        workflow_name="board-projection",
        artifact_receipts_json=json.dumps(
            [
                {
                    "artifact_id": safe_artifact_id,
                    "artifact_type": "markdown_document",
                    "file_path": "artifacts/result.md",
                    "content_sha256": "a" * 64,
                    "exists": True,
                },
                {
                    "artifact_id": "private/artifact",
                    "artifact_type": "private_payload",
                    "file_path": "private/secret.md",
                    "content_sha256": "not-a-digest",
                },
            ]
        ),
        effect_receipts_json=json.dumps(
            [
                {
                    "effect_id": "provider/private/effect",
                    "effect_type": "private_secret_payload",
                    "receipt_kind": "readback",
                    "target_path": "artifacts/result.md",
                    "target_digest": "b" * 64,
                    "child_job_id": "child/private/job",
                    "status": "succeeded",
                },
                {
                    "effect_id": "public-effect-1",
                    "effect_type": "board_child_readback",
                    "receipt_kind": "readback",
                    "target_path": "artifacts/result.md",
                    "target_digest": "b" * 64,
                    "status": "succeeded",
                },
            ]
        ),
    )

    projection = _safe_board_job_projection(run)

    assert projection["artifacts"] == [
        {
            "artifact_id": safe_artifact_id,
            "artifact_type": "markdown_document",
            "file_path": "artifacts/result.md",
            "content_sha256": "a" * 64,
            "exists": True,
        }
    ]
    effects = projection["effects"]
    assert len(effects) == 2
    assert all("effect_id" not in effect for effect in effects)
    assert all("child_job_id" not in effect for effect in effects)
    assert all("private_secret_payload" not in str(effect) for effect in effects)
    assert effects[0]["effect_id_digest"] == hashlib.sha256(
        b"provider/private/effect"
    ).hexdigest()[:16]
    assert effects[1]["effect_type"] == "board_child_readback"


def test_bound_workflow_lineage_cap_counts_owned_roots_and_descendants_together():
    roots = {f"work-board:root-{index}" for index in range(256)}

    assert _bounded_lineage_expansion(roots, ["goal-snapshot:child"]) is None
    assert _bounded_lineage_expansion(
        {f"work-board:root-{index}" for index in range(255)},
        ["goal-snapshot:child", "goal-snapshot:child"],
    ) == ["goal-snapshot:child"]


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
async def test_bound_workflow_job_denies_same_principal_from_a_different_operator_session(
    client,
    async_db,
    monkeypatch,
):
    """Board evidence stays bound to the task's authenticated owner session."""

    run_identity = "foreign-session:workflow:board-evidence:attempt-1"
    foreign_session = "foreign-operator-session"
    task = WorkBoardTask(
        task_id="foreign-session-board-task",
        owner_principal_id="operator:test-bypass",
        owner_session_id="test-auth-bypass",
        goal_id="goal-foreign-session",
        title="Foreign session board evidence",
        idempotency_key="foreign-session-board-task",
        status=WorkBoardStatus.done,
    )
    attempt = WorkBoardAttempt(
        task_id=task.task_id,
        attempt_id="foreign-session-board-attempt",
        workflow_run_id=run_identity,
        task_revision_at_claim=1,
        outcome="verified",
    )
    run = WorkflowRunState(
        run_identity=run_identity,
        root_run_identity=run_identity,
        workflow_name="board-evidence",
        owner_kind="user",
        owner_principal_id="operator:test-bypass",
        operator_session_id=foreign_session,
        status="succeeded",
    )
    child_identity = "foreign-session:workflow:board-evidence:child"
    child = WorkflowRunState(
        run_identity=child_identity,
        root_run_identity=run_identity,
        parent_run_identity=run_identity,
        parent_job_id=run_identity,
        workflow_name="goal-snapshot-to-file",
        owner_kind="service",
        owner_principal_id="service:goal-snapshot",
        operator_session_id=None,
        status="succeeded",
    )
    monkeypatch.setattr(
        "src.api.workflows._require_authenticated_capability_operator",
        lambda _request: SimpleNamespace(
            principal=SimpleNamespace(principal_id="operator:test-bypass"),
            session_id="test-auth-bypass",
        ),
    )
    monkeypatch.setattr("src.api.workflows.get_session", async_db)
    async with async_db() as db:
        db.add(task)
        await db.flush()
        db.add_all([attempt, run, child])
        await db.commit()

    response = await client.get(f"/api/workflows/jobs/{run_identity}")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "workflow_job_not_found"
    child_response = await client.get(f"/api/workflows/jobs/{child_identity}")
    assert child_response.status_code == 404
    assert child_response.json()["detail"]["code"] == "workflow_job_not_found"


@pytest.mark.asyncio
async def test_bound_workflow_job_missing_root_cannot_authorize_descendant(
    client,
    async_db,
    monkeypatch,
):
    missing_root_identity = "work-board:missing-root"
    child_identity = "goal-snapshot:orphan-child"
    task = WorkBoardTask(
        task_id="orphan-board-task",
        owner_principal_id="operator:test-bypass",
        owner_session_id="test-auth-bypass",
        goal_id="goal-orphan-board",
        title="Orphan board evidence",
        idempotency_key="orphan-board-task",
        status=WorkBoardStatus.done,
    )
    attempt = WorkBoardAttempt(
        task_id=task.task_id,
        attempt_id="orphan-board-attempt",
        workflow_run_id=missing_root_identity,
        task_revision_at_claim=1,
        outcome="verified",
    )
    child = WorkflowRunState(
        run_identity=child_identity,
        root_run_identity=missing_root_identity,
        parent_run_identity=missing_root_identity,
        parent_job_id=missing_root_identity,
        workflow_name="goal-snapshot-to-file",
        owner_kind="service",
        owner_principal_id="service:goal-snapshot",
        operator_session_id=None,
        status="succeeded",
    )
    monkeypatch.setattr(
        "src.api.workflows._require_authenticated_capability_operator",
        lambda _request: SimpleNamespace(
            principal=SimpleNamespace(principal_id="operator:test-bypass"),
            session_id="test-auth-bypass",
        ),
    )
    monkeypatch.setattr("src.api.workflows.get_session", async_db)
    async with async_db() as db:
        db.add(task)
        await db.flush()
        db.add_all([attempt, child])
        await db.commit()

    response = await client.get(f"/api/workflows/jobs/{child_identity}")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "workflow_job_not_found"


@pytest.mark.asyncio
async def test_bound_workflow_job_rejects_parent_identity_mismatch(
    client,
    async_db,
    monkeypatch,
):
    root_identity = "work-board:consistent-root"
    child_identity = "goal-snapshot:mismatched-child"
    task = WorkBoardTask(
        task_id="mismatched-board-task",
        owner_principal_id="operator:test-bypass",
        owner_session_id="test-auth-bypass",
        goal_id="goal-mismatched-board",
        title="Mismatched board evidence",
        idempotency_key="mismatched-board-task",
        status=WorkBoardStatus.done,
    )
    attempt = WorkBoardAttempt(
        task_id=task.task_id,
        attempt_id="mismatched-board-attempt",
        workflow_run_id=root_identity,
        task_revision_at_claim=1,
        outcome="verified",
    )
    root = WorkflowRunState(
        run_identity=root_identity,
        root_run_identity=root_identity,
        workflow_name="work-board-root",
        owner_kind="service",
        owner_principal_id="service:work-board",
        operator_session_id="test-auth-bypass",
        status="succeeded",
    )
    child = WorkflowRunState(
        run_identity=child_identity,
        root_run_identity=root_identity,
        parent_run_identity="other-parent",
        parent_job_id=root_identity,
        workflow_name="goal-snapshot-to-file",
        owner_kind="service",
        owner_principal_id="service:goal-snapshot",
        operator_session_id=None,
        status="succeeded",
    )
    foreign_root_child_identity = "goal-snapshot:foreign-root-child"
    foreign_root_child = WorkflowRunState(
        run_identity=foreign_root_child_identity,
        root_run_identity="missing-foreign-root",
        parent_run_identity=root_identity,
        parent_job_id=root_identity,
        workflow_name="goal-snapshot-to-file",
        owner_kind="service",
        owner_principal_id="service:goal-snapshot",
        operator_session_id=None,
        status="succeeded",
    )
    monkeypatch.setattr(
        "src.api.workflows._require_authenticated_capability_operator",
        lambda _request: SimpleNamespace(
            principal=SimpleNamespace(principal_id="operator:test-bypass"),
            session_id="test-auth-bypass",
        ),
    )
    monkeypatch.setattr("src.api.workflows.get_session", async_db)
    async with async_db() as db:
        db.add(task)
        await db.flush()
        db.add_all([attempt, root, child, foreign_root_child])
        await db.commit()

    response = await client.get(f"/api/workflows/jobs/{child_identity}")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "workflow_job_not_found"
    foreign_root_response = await client.get(f"/api/workflows/jobs/{foreign_root_child_identity}")
    assert foreign_root_response.status_code == 404
    assert foreign_root_response.json()["detail"]["code"] == "workflow_job_not_found"


@pytest.mark.asyncio
async def test_bound_workflow_job_allows_null_session_m2_descendant(
    client,
    async_db,
    monkeypatch,
):
    """A child adapter row may omit operator_session_id after owned lineage is proven."""

    root_identity = "work-board:owned-root"
    child_identity = "goal-snapshot:owned-child"
    task = WorkBoardTask(
        task_id="owned-board-task",
        owner_principal_id="operator:test-bypass",
        owner_session_id="test-auth-bypass",
        goal_id="goal-owned-board",
        title="Owned board evidence",
        idempotency_key="owned-board-task",
        status=WorkBoardStatus.done,
    )
    attempt = WorkBoardAttempt(
        task_id=task.task_id,
        attempt_id="owned-board-attempt",
        workflow_run_id=root_identity,
        task_revision_at_claim=1,
        outcome="verified",
    )
    root = WorkflowRunState(
        run_identity=root_identity,
        root_run_identity=root_identity,
        workflow_name="work-board-root",
        owner_kind="service",
        owner_principal_id="service:work-board",
        operator_session_id="test-auth-bypass",
        status="succeeded",
    )
    child = WorkflowRunState(
        run_identity=child_identity,
        root_run_identity=root_identity,
        parent_run_identity=root_identity,
        parent_job_id=root_identity,
        workflow_name="goal-snapshot-to-file",
        owner_kind="service",
        owner_principal_id="service:goal-snapshot",
        operator_session_id=None,
        status="succeeded",
    )
    mismatched_root_child = WorkflowRunState(
        run_identity="goal-snapshot:foreign-root-child",
        root_run_identity="work-board:unproven-root",
        parent_run_identity=root_identity,
        parent_job_id=root_identity,
        workflow_name="goal-snapshot-to-file",
        owner_kind="service",
        owner_principal_id="service:goal-snapshot",
        operator_session_id=None,
        status="succeeded",
    )
    monkeypatch.setattr(
        "src.api.workflows._require_authenticated_capability_operator",
        lambda _request: SimpleNamespace(
            principal=SimpleNamespace(principal_id="operator:test-bypass"),
            session_id="test-auth-bypass",
        ),
    )
    monkeypatch.setattr("src.api.workflows.get_session", async_db)
    async with async_db() as db:
        db.add(task)
        await db.flush()
        db.add_all([attempt, root, child, mismatched_root_child])
        await db.commit()

    response = await client.get(f"/api/workflows/jobs/{child_identity}")

    assert response.status_code == 200
    assert response.json()["job"]["job_id"] == child_identity
    assert response.json()["job"]["parent_job_id"] == root_identity
    mismatched_root_response = await client.get(
        "/api/workflows/jobs/goal-snapshot:foreign-root-child"
    )
    assert mismatched_root_response.status_code == 404
    assert mismatched_root_response.json()["detail"]["code"] == "workflow_job_not_found"


@pytest.mark.asyncio
async def test_bound_workflow_job_denies_explicit_foreign_session_descendant(
    client,
    async_db,
    monkeypatch,
):
    """Lineage alone cannot override an explicit child operator session."""

    root_identity = "work-board:owned-root-explicit-child"
    child_identity = "goal-snapshot:foreign-child"
    grandchild_identity = "goal-snapshot:foreign-grandchild"
    task = WorkBoardTask(
        task_id="owned-root-explicit-child-task",
        owner_principal_id="operator:test-bypass",
        owner_session_id="test-auth-bypass",
        goal_id="goal-owned-explicit-child",
        title="Foreign child evidence",
        idempotency_key="owned-root-explicit-child-task",
        status=WorkBoardStatus.done,
    )
    attempt = WorkBoardAttempt(
        task_id=task.task_id,
        attempt_id="owned-root-explicit-child-attempt",
        workflow_run_id=root_identity,
        task_revision_at_claim=1,
        outcome="verified",
    )
    root = WorkflowRunState(
        run_identity=root_identity,
        root_run_identity=root_identity,
        workflow_name="work-board-root",
        owner_kind="service",
        owner_principal_id="service:work-board",
        operator_session_id="test-auth-bypass",
        status="succeeded",
    )
    child = WorkflowRunState(
        run_identity=child_identity,
        root_run_identity=root_identity,
        parent_run_identity=root_identity,
        parent_job_id=root_identity,
        workflow_name="goal-snapshot-to-file",
        owner_kind="service",
        owner_principal_id="service:goal-snapshot",
        operator_session_id="foreign-operator-session",
        status="succeeded",
    )
    grandchild = WorkflowRunState(
        run_identity=grandchild_identity,
        root_run_identity=root_identity,
        parent_run_identity=child_identity,
        parent_job_id=child_identity,
        workflow_name="goal-snapshot-to-file",
        owner_kind="service",
        owner_principal_id="service:goal-snapshot",
        operator_session_id=None,
        status="succeeded",
    )
    monkeypatch.setattr(
        "src.api.workflows._require_authenticated_capability_operator",
        lambda _request: SimpleNamespace(
            principal=SimpleNamespace(principal_id="operator:test-bypass"),
            session_id="test-auth-bypass",
        ),
    )
    monkeypatch.setattr("src.api.workflows.get_session", async_db)
    async with async_db() as db:
        db.add(task)
        await db.flush()
        db.add_all([attempt, root, child, grandchild])
        await db.commit()

    response = await client.get(f"/api/workflows/jobs/{child_identity}")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "workflow_job_not_found"
    grandchild_response = await client.get(f"/api/workflows/jobs/{grandchild_identity}")
    assert grandchild_response.status_code == 404
    assert grandchild_response.json()["detail"]["code"] == "workflow_job_not_found"


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
    blocked_body = blocked.json()
    assert blocked_body["task"]["status"] == "blocked"
    assert {
        "task_id",
        "status",
        "revision",
        "attempt_id",
        "reason_code",
        "recovery_action",
        "event_id",
    } <= blocked_body.keys()
    assert blocked_body["task_id"] == child_id
    assert blocked_body["status"] == "blocked"
    assert blocked_body["revision"] == 4
    assert blocked_body["attempt_id"] is None
    assert blocked_body["reason_code"] == "operator"
    assert blocked_body["recovery_action"] == blocked_body["task"]["recovery_action"]
    assert isinstance(blocked_body["event_id"], int)

    detail = await client.get(f"/api/work-board/tasks/{child_id}")
    assert detail.status_code == 200
    assert detail.json()["events"][-1]["event_id"] == blocked_body["event_id"]


@pytest.mark.asyncio
async def test_http_cancel_action_returns_authoritative_event_receipt(client, monkeypatch):
    task = WorkBoardTask(
        task_id="api-cancel-task",
        owner_principal_id="operator:test-bypass",
        owner_session_id="test-auth-bypass",
        goal_id="goal-api-cancel",
        title="Cancel receipt",
        idempotency_key="api-cancel-task",
        status=WorkBoardStatus.running,
        task_revision=5,
    )
    attempt = WorkBoardAttempt(
        task_id=task.task_id,
        attempt_id="api-cancel-attempt",
        workflow_run_id="workflow:api-cancel",
        task_revision_at_claim=4,
        lease_owner="service:work-board",
        fencing_token=3,
        executor_id="executor.local",
    )
    event = WorkBoardEvent(
        event_id=91,
        task_id=task.task_id,
        owner_principal_id=task.owner_principal_id,
        owner_session_id=task.owner_session_id,
        actor_principal_id=task.owner_principal_id,
        actor_session_id=task.owner_session_id,
        kind="attempt.cancel_requested",
    )

    async def fake_cancel(owner, task_id, *, expected_revision):
        assert owner.principal_id == task.owner_principal_id
        assert owner.session_id == task.owner_session_id
        assert task_id == task.task_id
        assert expected_revision == 4
        return SimpleNamespace(task=task, attempt=attempt, event=event)

    monkeypatch.setattr("src.api.work_board.dispatcher.cancel_task", fake_cancel)
    response = await client.post(
        f"/api/work-board/tasks/{task.task_id}/actions",
        json={"action": "cancel", "expected_revision": 4},
    )

    assert response.status_code == 200
    body = response.json()
    assert {
        "task_id",
        "status",
        "revision",
        "attempt_id",
        "reason_code",
        "recovery_action",
        "event_id",
    } <= body.keys()
    assert body["task_id"] == task.task_id
    assert body["status"] == "running"
    assert body["revision"] == 5
    assert body["attempt_id"] == attempt.attempt_id
    assert body["reason_code"] is None
    assert body["recovery_action"] == "cancel"
    assert body["event_id"] == event.event_id
    assert body["task"]["task_id"] == task.task_id


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
                    "effect_id": "public-effect",
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
                    "artifact_id": "artifact/path",
                    "effect_id": "effect/path",
                    "job_id": "..",
                    "child_job_id": "..",
                    "artifact_type": "artifact/type",
                    "effect_type": "effect/type",
                    "file_path": "reports/private.txt",
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
    task_payload = serialize_task_payload(task, dependency_counts=(2, 1))
    serialized = json.dumps({"attempt": attempt_payload, "task": task_payload})
    assert attempt_payload["workflow_run_id"] is None
    assert attempt_payload["receipt_refs"] == [
        {
            "job_id": "job:1",
            "artifact_type": "goal_snapshot",
            "effect_type": "readback",
            "effect_id_digest": hashlib.sha256(b"public-effect").hexdigest()[:16],
            "target_path": "artifacts/result.txt",
            "status": "succeeded",
            "verified": True,
            "readback_status": "verified",
            "verification_status": "passed",
        },
        {"status": "succeeded"},
    ]
    round_tripped = _attempt_payload(
        WorkBoardAttempt(
            task_id="task-safe-refs",
            receipt_refs_json=json.dumps(attempt_payload["receipt_refs"]),
        )
    )
    assert round_tripped["receipt_refs"] == attempt_payload["receipt_refs"]
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
    assert task_payload["dependency_count"] == 2
    assert task_payload["completed_dependency_count"] == 1
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


def test_attempt_serializer_preserves_verified_outcome():
    attempt = WorkBoardAttempt(task_id="task-verified", outcome="verified")

    assert _attempt_payload(attempt)["outcome"] == "verified"
