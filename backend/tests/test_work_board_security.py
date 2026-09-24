"""Security and cursor-boundary checks for the authenticated work board."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from config.settings import settings
from src.api.work_board import _safe_task_payload
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
from src.work_board.dispatcher import WorkBoardDispatcher


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
                    resolution="reconcile operator specification",
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
                    resolution="restore the prior safe phase",
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
                    resolution="restore the prior safe phase",
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
                resolution="restore the prior safe phase",
            ),
        )
        assert mutation.task.status is WorkBoardStatus.todo


@pytest.mark.asyncio
async def test_generic_unblock_restores_review_phase_for_named_reviewer(async_db, monkeypatch):
    task_id = await _seed_task(async_db, OWNER, key_suffix="review-phase")
    repository = WorkBoardRepository()
    async with async_db() as db:
        task = await repository.get_task(db, OWNER, task_id)
        task.status = WorkBoardStatus.blocked
        task.block_source_status = WorkBoardStatus.review.value
        task.block_kind = "operator"
        task.block_reason = "Reviewer needs the artifact link restored"
        task.requires_review = True
        task.reviewer_id = "operator:reviewer"
        db.add(
            WorkBoardAttempt(
                task_id=task_id,
                workflow_run_id="review-phase-run",
                task_revision_at_claim=task.task_revision,
                lease_owner="executor.local",
                fencing_token=1,
                executor_id="executor.local",
                started_at=datetime.now(timezone.utc),
                ended_at=datetime.now(timezone.utc),
                outcome="succeeded",
                receipt_refs_json=json.dumps(
                    [
                        {
                            "status": "succeeded",
                            "verified": True,
                            "workflow_run_id": "review-phase-run",
                            "content_sha256": "a" * 64,
                            "readback_status": "verified",
                        }
                    ]
                ),
            )
        )
        await db.commit()

    async def authenticated_session(session_id: str, *, touch: bool = False):
        assert session_id == OWNER.session_id
        assert touch is False
        return SimpleNamespace(principal=SimpleNamespace(principal_id=OWNER.principal_id))

    # The production API runs this live preflight before its repository CAS.
    monkeypatch.setattr("src.work_board.dispatcher.authenticate_session", authenticated_session)
    dispatcher = WorkBoardDispatcher(session_provider=async_db)
    await dispatcher.validate_unblock(OWNER, task_id, expected_revision=1)

    async with async_db() as db:
        current = await repository.get_task(db, OWNER, task_id)
        mutation = await repository.action_task(
            db,
            OWNER,
            task_id,
            WorkBoardActionRequest(
                action=WorkBoardAction.unblock,
                expected_revision=current.task_revision,
                resolution="The artifact link was restored and checked.",
            ),
        )

        assert mutation.task.status is WorkBoardStatus.review
        assert mutation.task.requires_review is True
        assert mutation.task.reviewer_id == "operator:reviewer"
        assert mutation.task.block_source_status is None
        assert mutation.task.block_kind is None
        assert mutation.event.kind == "task.unblock"
        assert mutation.event.metadata_json is not None
        detail = await repository.get_detail(db, OWNER, task_id)
        assert len(detail["attempts"]) == 1
        assert json.loads(detail["attempts"][0].receipt_refs_json) == [
            {
                "status": "succeeded",
                "verified": True,
                "workflow_run_id": "review-phase-run",
                "content_sha256": "a" * 64,
                "readback_status": "verified",
            }
        ]


@pytest.mark.asyncio
async def test_review_unblock_preflight_requires_review_contract(async_db, monkeypatch):
    task_id = await _seed_task(async_db, OWNER, key_suffix="review-not-required")
    async with async_db() as db:
        task = await WorkBoardRepository().get_task(db, OWNER, task_id)
        task.status = WorkBoardStatus.blocked
        task.block_source_status = WorkBoardStatus.review.value
        task.block_kind = "operator"
        task.requires_review = False
        task.reviewer_id = "operator:reviewer"
        db.add(
            WorkBoardAttempt(
                task_id=task_id,
                workflow_run_id="review-not-required-run",
                task_revision_at_claim=task.task_revision,
                lease_owner="executor.local",
                fencing_token=1,
                executor_id="executor.local",
                started_at=datetime.now(timezone.utc),
                ended_at=datetime.now(timezone.utc),
                outcome="succeeded",
                receipt_refs_json=json.dumps([{"status": "succeeded", "verified": True}]),
            )
        )
        await db.commit()

    async def authenticated_session(session_id: str, *, touch: bool = False):
        return SimpleNamespace(principal=SimpleNamespace(principal_id=OWNER.principal_id))

    monkeypatch.setattr("src.work_board.dispatcher.authenticate_session", authenticated_session)
    dispatcher = WorkBoardDispatcher(session_provider=async_db)
    with pytest.raises(BoardError, match="required named reviewer") as raised:
        await dispatcher.validate_unblock(OWNER, task_id, expected_revision=1)

    assert raised.value.extra["recovery_action"] == "restore_prerequisite"


async def _seed_blocked_review_task(
    async_db,
    *,
    key_suffix: str,
    requires_review: bool = True,
    reviewer_id: str | None = "operator:reviewer",
    attempt_ended: bool = True,
    receipt_refs: list[dict[str, object]] | None = None,
    add_attempt: bool = True,
) -> str:
    task_id = await _seed_task(async_db, OWNER, key_suffix=key_suffix)
    workflow_run_id = f"review-{key_suffix}-run"
    async with async_db() as db:
        task = await WorkBoardRepository().get_task(db, OWNER, task_id)
        task.status = WorkBoardStatus.blocked
        task.block_source_status = WorkBoardStatus.review.value
        task.block_kind = "operator"
        task.block_reason = "Operator recovery"
        task.requires_review = requires_review
        task.reviewer_id = reviewer_id
        if add_attempt:
            db.add(
                WorkBoardAttempt(
                    task_id=task_id,
                    workflow_run_id=workflow_run_id,
                    task_revision_at_claim=task.task_revision,
                    lease_owner="executor.local" if not attempt_ended else None,
                    fencing_token=1,
                    executor_id="executor.local",
                    started_at=datetime.now(timezone.utc),
                    ended_at=datetime.now(timezone.utc) if attempt_ended else None,
                    outcome="succeeded" if attempt_ended else "running",
                    receipt_refs_json=json.dumps(
                        receipt_refs
                        if receipt_refs is not None
                        else [
                            {
                                "status": "succeeded",
                                "verified": True,
                                "workflow_run_id": workflow_run_id,
                                "content_sha256": "a" * 64,
                            }
                        ]
                    ),
                )
            )
        await db.commit()
    return task_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "receipt_refs",
    [
        [{"status": "succeeded", "verified": True}],
        [
            {
                "status": "succeeded",
                "verified": True,
                "workflow_run_id": "review-other-run",
                "content_sha256": "a" * 64,
            }
        ],
    ],
    ids=["missing-digest", "wrong-workflow-run"],
)
async def test_api_projection_does_not_advertise_review_unblock_without_attempt_bound_digest(
    async_db,
    monkeypatch,
    receipt_refs,
):
    task_id = await _seed_blocked_review_task(
        async_db,
        key_suffix=f"api-projection-{len(receipt_refs[0])}",
        receipt_refs=receipt_refs,
    )

    async def authenticated_session(session_id: str, *, touch: bool = False):
        assert session_id == OWNER.session_id
        assert touch is False
        return SimpleNamespace(principal=SimpleNamespace(principal_id=OWNER.principal_id))

    monkeypatch.setattr("src.work_board.dispatcher.authenticate_session", authenticated_session)
    monkeypatch.setattr(
        "src.api.work_board.dispatcher",
        WorkBoardDispatcher(session_provider=async_db),
    )

    async with async_db() as db:
        detail = await WorkBoardRepository().get_detail(db, OWNER, task_id)
        payload = await _safe_task_payload(
            detail["task"],
            latest_attempt=detail["attempts"][0],
            attempt_count=1,
        )

    assert payload["status"] == WorkBoardStatus.blocked.value
    assert payload["recovery_action"] == "restore_prerequisite"
    assert payload["recovery_action"] != "unblock"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "requires_review", "reviewer_id", "attempt_ended", "receipt_refs", "add_attempt", "code"),
    [
        ("missing-flag", False, "operator:reviewer", True, None, True, "reviewer_required"),
        ("missing-reviewer", True, None, True, None, True, "reviewer_required"),
        ("invalid-reviewer", True, "operator/reviewer", True, None, True, "invalid_reference"),
        ("unfinished-attempt", True, "operator:reviewer", False, None, True, "attempt_reconcile_required"),
        (
            "missing-readback-digest",
            True,
            "operator:reviewer",
            True,
            [{"status": "succeeded", "verified": True}],
            True,
            "verified_readback_required",
        ),
        (
            "readback-for-wrong-run",
            True,
            "operator:reviewer",
            True,
            [
                {
                    "status": "succeeded",
                    "verified": True,
                    "workflow_run_id": "review-other-run",
                    "content_sha256": "a" * 64,
                }
            ],
            True,
            "verified_readback_required",
        ),
    ],
)
async def test_repository_review_unblock_requires_attempt_bound_verified_readback(
    async_db,
    case,
    requires_review,
    reviewer_id,
    attempt_ended,
    receipt_refs,
    add_attempt,
    code,
):
    task_id = await _seed_blocked_review_task(
        async_db,
        key_suffix=f"contract-{case}",
        requires_review=requires_review,
        reviewer_id=reviewer_id,
        attempt_ended=attempt_ended,
        receipt_refs=receipt_refs,
        add_attempt=add_attempt,
    )
    repository = WorkBoardRepository()
    async with async_db() as db:
        with pytest.raises(BoardError) as raised:
            await repository.action_task(
                db,
                OWNER,
                task_id,
                WorkBoardActionRequest(
                    action=WorkBoardAction.unblock,
                    expected_revision=1,
                    resolution="restore review recovery",
                ),
            )
        assert raised.value.code == code
        current = await repository.get_task(db, OWNER, task_id)
        assert current.status is WorkBoardStatus.blocked
        assert current.task_revision == 1


@pytest.mark.asyncio
async def test_repository_review_unblock_preserves_attempt_and_revision_cas(async_db):
    task_id = await _seed_blocked_review_task(async_db, key_suffix="contract-positive")
    repository = WorkBoardRepository()
    async with async_db() as db:
        mutation = await repository.action_task(
            db,
            OWNER,
            task_id,
            WorkBoardActionRequest(
                action=WorkBoardAction.unblock,
                expected_revision=1,
                resolution="restore review recovery",
            ),
        )
        assert mutation.task.status is WorkBoardStatus.review
        assert mutation.task.task_revision == 2
        assert mutation.task.requires_review is True
        assert mutation.task.reviewer_id == "operator:reviewer"
        assert mutation.task.block_source_status is None
        assert mutation.task.block_kind is None
        detail = await repository.get_detail(db, OWNER, task_id)
        assert len(detail["attempts"]) == 1


@pytest.mark.asyncio
async def test_projected_review_persists_attempt_bound_readback_for_recovery(async_db):
    task_id = await _seed_task(async_db, OWNER, key_suffix="projected-review")
    repository = WorkBoardRepository()
    workflow_run_id = "projected-review-run"
    digest = "b" * 64
    async with async_db() as db:
        task = await repository.get_task(db, OWNER, task_id)
        task.status = WorkBoardStatus.running
        task.requires_review = True
        task.reviewer_id = "operator:reviewer"
        db.add(
            WorkBoardAttempt(
                task_id=task_id,
                workflow_run_id=workflow_run_id,
                task_revision_at_claim=task.task_revision,
                lease_owner="executor.local",
                fencing_token=1,
                executor_id="executor.local",
                started_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()

    async with async_db() as db:
        projected = await repository.project_attempt(
            db,
            task_id,
            (
                await db.execute(select(WorkBoardAttempt.attempt_id).where(WorkBoardAttempt.task_id == task_id))
            ).scalar_one(),
            expected_revision=1,
            board_fence=1,
            lease_owner="executor.local",
            status=WorkBoardStatus.review,
            outcome="verified",
            receipt_refs=[
                {"workflow_run_id": workflow_run_id, "status": "succeeded", "verified": True}
            ],
            verified_readback={
                "source": "workflow_run",
                "status": "succeeded",
                "verified": True,
                "workflow_run_id": workflow_run_id,
                "content_sha256": digest,
            },
        )
        assert projected.task.status is WorkBoardStatus.review
        assert projected.attempt.ended_at is not None
        assert json.loads(projected.attempt.receipt_refs_json) == [
            {
                "workflow_run_id": workflow_run_id,
                "content_sha256": digest,
                "status": "succeeded",
                "verified": True,
                "readback_status": "verified",
                "verification_status": "passed",
            },
            {"workflow_run_id": workflow_run_id, "status": "succeeded", "verified": True},
        ]
        await db.commit()

    async with async_db() as db:
        current = await repository.get_task(db, OWNER, task_id)
        blocked = await repository.action_task(
            db,
            OWNER,
            task_id,
            WorkBoardActionRequest(
                action=WorkBoardAction.block,
                expected_revision=current.task_revision,
                block_kind="operator",
                reason="Reviewer requested a bounded retry of the review step",
            ),
        )
        await db.commit()
        assert blocked.task.status is WorkBoardStatus.blocked

    async with async_db() as db:
        current = await repository.get_task(db, OWNER, task_id)
        recovered = await repository.action_task(
            db,
            OWNER,
            task_id,
            WorkBoardActionRequest(
                action=WorkBoardAction.unblock,
                expected_revision=current.task_revision,
                resolution="The reviewed artifact remains valid.",
            ),
        )
        assert recovered.task.status is WorkBoardStatus.review
        assert recovered.task.task_revision == 4


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
                    resolution="restore the prior safe phase",
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
