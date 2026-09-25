"""Focused M4 blocker proofs for executor lanes and typed input recovery."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from config.settings import settings
from src.db.models import Goal, WorkBoardAttempt, WorkBoardStatus, WorkBoardTask
from src.work_board.contracts import (
    WorkBoardOwner,
    WorkBoardTaskCreate,
    WorkBoardTaskPatch,
)
from src.work_board.dispatcher import (
    WorkBoardDispatcher,
    _parse_typed_input,
    registered_executor_id,
)
from src.work_board.repository import BoardError, WorkBoardRepository
from src.work_board import triage as triage_service


OWNER = WorkBoardOwner(principal_id="operator:test-bypass", session_id="test-auth-bypass")
CAPABILITY = "guardian.research-watch.v1"


def _typed_input(tmp_path: Path) -> tuple[str, str]:
    path = tmp_path / "inputs" / "task.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(
        {
            "schema_version": 1,
            "capability_id": CAPABILITY,
            "input": {"watch_id": "watch-1", "expected_plan_revision": 1},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    path.write_bytes(raw)
    return "workspace-json:inputs/task.json", sha256(raw).hexdigest()


async def _goal(db) -> Goal:
    goal = Goal(
        id="goal-m4-lane",
        title="M4 lane goal",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        revision=1,
        status="active",
    )
    db.add(goal)
    await db.flush()
    return goal


@pytest.mark.asyncio
async def test_registered_executor_lane_is_derived_on_create_and_patch(async_db):
    expected = registered_executor_id(CAPABILITY)
    assert expected == triage_service.registered_executor_id(CAPABILITY)

    async with async_db() as db:
        await _goal(db)
        request = WorkBoardTaskCreate(
            title="Server lane",
            goal_id="goal-m4-lane",
            goal_revision=1,
            status=WorkBoardStatus.todo,
            capability_id=CAPABILITY,
            typed_input_ref="workspace-json:inputs/task.json",
            typed_input_digest="a" * 64,
            idempotency_key="lane-derived",
        )
        created = await WorkBoardRepository().create_task(db, OWNER, request)
        assert created.task.executor_id == expected

        with pytest.raises(BoardError) as create_failure:
            await WorkBoardRepository().create_task(
                db,
                OWNER,
                request.model_copy(
                    update={
                        "idempotency_key": "lane-forged-create",
                        "executor_id": "executor:forged",
                    }
                ),
            )
        assert create_failure.value.code == "executor_lane_mismatch"

        with pytest.raises(BoardError) as patch_failure:
            await WorkBoardRepository().patch_task(
                db,
                OWNER,
                created.task.task_id,
                WorkBoardTaskPatch(
                    expected_revision=created.task.task_revision,
                    executor_id="executor:forged",
                ),
            )
        assert patch_failure.value.code == "executor_lane_mismatch"

        with pytest.raises(BoardError) as no_capability_failure:
            await WorkBoardRepository().create_task(
                db,
                OWNER,
                WorkBoardTaskCreate(
                    title="Unbound lane",
                    goal_id="goal-m4-lane",
                    goal_revision=1,
                    executor_id="executor:forged",
                    idempotency_key="lane-without-capability",
                ),
            )
        assert no_capability_failure.value.code == "executor_requires_capability"


@pytest.mark.asyncio
async def test_decompose_source_rejects_forged_executor_lane(async_db, monkeypatch, tmp_path):
    reference, digest = _typed_input(tmp_path)
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))

    async with async_db() as db:
        await _goal(db)
        task = WorkBoardTask(
            task_id="decompose-forged-lane",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            origin_session_id=OWNER.session_id,
            goal_id="goal-m4-lane",
            goal_revision=1,
            title="Decompose source",
            idempotency_key="decompose-forged-lane",
            status=WorkBoardStatus.todo,
            capability_id=CAPABILITY,
            typed_input_ref=reference,
            typed_input_digest=digest,
            executor_id="executor:forged",
        )
        db.add(task)
        await db.flush()

        with pytest.raises(BoardError) as failure:
            await triage_service._validate_decompose_source(
                db,
                repository=WorkBoardRepository(),
                owner=OWNER,
                task=task,
            )
        assert failure.value.code == "executor_lane_mismatch"


@pytest.mark.asyncio
async def test_missing_workspace_root_is_a_visible_typed_input_block(async_db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path / "removed-root"))
    async with async_db() as db:
        goal = await _goal(db)
        task = WorkBoardTask(
            task_id="readiness-missing-root",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            origin_session_id=OWNER.session_id,
            goal_id=goal.id,
            goal_revision=1,
            title="Missing root",
            idempotency_key="readiness-missing-root",
            status=WorkBoardStatus.todo,
            capability_id=CAPABILITY,
            executor_id=registered_executor_id(CAPABILITY),
            typed_input_ref="workspace-json:inputs/task.json",
            typed_input_digest="a" * 64,
        )
        db.add(task)
        await db.flush()

        dispatcher = WorkBoardDispatcher(session_provider=async_db)
        async def _preflight(*_args):
            return None, None

        dispatcher._capability_preflight = _preflight
        await db.commit()
        code, reason = await dispatcher._readiness(task)

    assert code == "typed_input_unavailable"
    assert reason


@pytest.mark.asyncio
async def test_typed_input_disappearance_removes_absent_claim_and_allows_retry(
    async_db,
    monkeypatch,
    tmp_path,
):
    reference, digest = _typed_input(tmp_path)
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))

    async with async_db() as db:
        goal = await _goal(db)
        task = WorkBoardTask(
            task_id="post-readiness-input-disappears",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            origin_session_id=OWNER.session_id,
            goal_id=goal.id,
            goal_revision=1,
            title="Input disappears",
            idempotency_key="post-readiness-input-disappears",
            status=WorkBoardStatus.ready,
            capability_id=CAPABILITY,
            executor_id=registered_executor_id(CAPABILITY),
            typed_input_ref=reference,
            typed_input_digest=digest,
        )
        db.add(task)
        await db.flush()
        claim = await WorkBoardRepository().claim_ready_task(
            db,
            task.task_id,
            expected_revision=task.task_revision,
            lease_owner="service:work-board",
        )
        assert claim is not None
        await db.commit()

    (tmp_path / "inputs" / "task.json").unlink()
    dispatcher = WorkBoardDispatcher(session_provider=async_db)
    async def _preflight(*_args):
        return None, None

    dispatcher._capability_preflight = _preflight
    outcome = await dispatcher._admit_execute_project(claim)
    assert outcome["blocked"] is True
    assert outcome["admitted"] is False

    async with async_db() as db:
        detail = await WorkBoardRepository().get_detail(db, OWNER, task.task_id)
        blocked = detail["task"]
        assert blocked.status is WorkBoardStatus.blocked
        assert blocked.block_kind == "capability"
        assert blocked.block_reason == "typed_input_missing"
        assert detail["attempts"] == []
        assert (blocked.workflow_run_id if hasattr(blocked, "workflow_run_id") else None) is None
        revision = blocked.task_revision

    (tmp_path / "inputs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "inputs" / "task.json").write_bytes(
        json.dumps(
            {
                "schema_version": 1,
                "capability_id": CAPABILITY,
                "input": {"watch_id": "watch-1", "expected_plan_revision": 1},
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    async with async_db() as db:
        repaired = await WorkBoardRepository().get_task(db, OWNER, task.task_id)
        assert _parse_typed_input(repaired)["watch_id"] == "watch-1"
    async with async_db() as db:
        retried = await WorkBoardRepository().retry_task(
            db,
            OWNER,
            task.task_id,
            expected_revision=revision,
        )
        assert retried.task.status is WorkBoardStatus.todo
        assert retried.task.task_revision == revision + 1
