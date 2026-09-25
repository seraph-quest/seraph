"""Named M4 review acceptance cases.

The focused M4 contract tests remain in their implementation-group modules;
these named entry points keep the issue's acceptance checklist directly
discoverable and execute those same integration proofs.
"""

import pytest

from src.tools.work_board_tools import WorkBoardWorkerHost
from src.work_board.tools import WorkBoardWorkerRequest
from tests.test_work_board_m4 import (
    test_changes_requested_keeps_attempt_and_rechecks_ready as _changes_requested,
    test_changes_requested_exhausted_attempts_stays_blocked as _attempt_limit_changes_requested,
    test_changes_requested_keeps_future_scheduled_task_in_todo as _scheduled_changes_requested,
    test_changes_requested_rerun_claims_a_new_fenced_attempt as _changes_requested_rerun,
    test_review_expiry_requires_dispatcher_sweep_and_explicit_renewal as _review_expiry,
    test_review_expiry_cas_race_does_not_stop_later_rows as _review_expiry_race,
)
from tests.test_work_board_repository import (
    test_ready_child_is_demoted_when_new_parent_is_unfinished as _unfinished_parent,
)
from tests.test_work_board_m4_review_hardening import (
    test_startup_handoff_backfill_keeps_ready_child_blocked_until_live_unblock as _startup_handoff_recovery,
)


def test_worker_cannot_approve_own_review():
    request = WorkBoardWorkerRequest(
        task_id="review-worker-task",
        attempt_id="review-worker-attempt",
        expected_task_revision=3,
        board_fencing_token=2,
        workflow_run_id="work-board:review-worker-task:review-worker-attempt",
        workflow_fencing_token=4,
    )
    tool_names = {tool.name for tool in WorkBoardWorkerHost(request).tools}

    assert "work_board_request_review" in tool_names
    assert "work_board_complete_review" not in tool_names


@pytest.mark.asyncio
async def test_changes_requested_keeps_attempt_and_rechecks_ready(async_db, monkeypatch):
    await _changes_requested(async_db, monkeypatch)


@pytest.mark.asyncio
async def test_changes_requested_respects_future_schedule(async_db, monkeypatch):
    await _scheduled_changes_requested(async_db, monkeypatch)


@pytest.mark.asyncio
async def test_changes_requested_rerun_uses_new_attempt(async_db, monkeypatch):
    await _changes_requested_rerun(async_db, monkeypatch)


@pytest.mark.asyncio
async def test_changes_requested_cannot_exceed_attempt_limit(async_db, monkeypatch):
    await _attempt_limit_changes_requested(async_db, monkeypatch)


@pytest.mark.asyncio
async def test_review_expiry_blocks(async_db):
    await _review_expiry(async_db)


@pytest.mark.asyncio
async def test_review_expiry_lost_cas_continues_sweep(async_db, monkeypatch):
    await _review_expiry_race(async_db, monkeypatch)


@pytest.mark.asyncio
async def test_child_waits_for_verified_parent(async_db):
    await _unfinished_parent(async_db)


@pytest.mark.asyncio
async def test_startup_handoff_waits_for_explicit_live_recovery(async_db):
    await _startup_handoff_recovery(async_db)
