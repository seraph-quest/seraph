"""Focused M3 browser lane and public policy contract checks."""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import src.browser.task_runner as task_runner_module
import src.work_board.dispatcher as dispatcher_module
from config.settings import settings
from src.artifacts.registry import artifact_id_for
from src.api.work_board import (
    _browser_execution_payload,
    _browser_cleanup_projection,
    _browser_projection_digest,
    _browser_task_policy,
    _input_artifact_payload,
    _safe_task_payload,
)
from src.browser import task_lane as task_lane_module
from src.browser.task_lane import (
    BrowserTaskLaneError,
    browser_task_lane_wait_reason,
    try_acquire_browser_task_lane,
)
from src.db.models import Goal, WorkBoardAttempt, WorkBoardInputArtifact, WorkBoardStatus, WorkBoardTask
from src.goals.contracts import GoalAdmissionBudget
from src.goals.repository import serialize_admission_budget
from src.work_board.contracts import WorkBoardInputArtifactCreate, WorkBoardOwner, WorkBoardTaskCreate
from src.work_board.dispatcher import (
    GOAL_SNAPSHOT_CAPABILITY,
    CapabilitySpec,
    REGISTERED_CAPABILITIES,
    TypedInputError,
    WorkBoardDispatcher,
    registered_executor_id,
    validate_capability_input,
)
from src.work_board.input_artifacts import (
    bind_input_artifact,
    consume_input_artifact,
    delete_input_artifact,
    expire_input_artifacts,
    prepare_input_artifact,
    resolve_input_artifact_for_task,
)
from src.work_board.repository import BoardError, WorkBoardRepository
from sqlalchemy import select


OWNER = WorkBoardOwner(principal_id="operator:test-bypass", session_id="test-auth-bypass")
OTHER_OWNER = WorkBoardOwner(principal_id="operator:other", session_id="other-auth")


def _browser_input() -> dict:
    return {
        "schema_version": 1,
        "start_url": "https://public.example/docs",
        "allowed_hosts": ["public.example"],
        "approved_url_prefixes": ["https://public.example/docs"],
        "actions": [
            {
                "kind": "extract",
                "selector": "main h1",
                "max_chars": 128,
                "expected_checks": [
                    {"kind": "text_contains", "selector": "main h1", "value": "docs"},
                ],
            }
        ],
        "final_expected_checks": [
            {"kind": "url_host", "value": "public.example"},
        ],
    }


def _artifact_request(
    *,
    goal_id: str = "goal-artifact",
    idempotency_key: str = "artifact-key",
    capability_id: str = "browser.public-task.v1",
    input: dict | None = None,
) -> WorkBoardInputArtifactCreate:
    return WorkBoardInputArtifactCreate(
        schema_version=1,
        capability_id=capability_id,
        goal_id=goal_id,
        goal_revision=1,
        input=input if input is not None else _browser_input(),
        idempotency_key=idempotency_key,
    )


def _artifact_path(metadata) -> Path:
    return Path(settings.workspace_dir) / metadata.typed_input_ref.removeprefix("workspace-json:")


async def _browser_goal(db, *, goal_id: str, budget: GoalAdmissionBudget) -> Goal:
    goal = Goal(
        id=goal_id,
        title="Browser budget test",
        status="active",
        revision=1,
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        admission_budget_json=serialize_admission_budget(budget),
    )
    db.add(goal)
    await db.flush()
    return goal


def _browser_task(*, task_id: str, goal_id: str, status: WorkBoardStatus) -> WorkBoardTask:
    return WorkBoardTask(
        task_id=task_id,
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        origin_session_id=OWNER.session_id,
        goal_id=goal_id,
        goal_revision=1,
        title=task_id,
        capability_id="browser.public-task.v1",
        executor_id=registered_executor_id("browser.public-task.v1"),
        idempotency_key=task_id,
        status=status,
        task_revision=1,
    )


def test_browser_lane_is_nonblocking_and_keeps_a_private_lock(tmp_path: Path):
    first = try_acquire_browser_task_lane(tmp_path)
    assert first is not None
    try:
        assert try_acquire_browser_task_lane(tmp_path) is None
        assert (tmp_path / ".browser-task-lane.lock").stat().st_mode & 0o777 == 0o600
    finally:
        first.release()
    second = try_acquire_browser_task_lane(tmp_path)
    assert second is not None
    second.release()


def test_browser_lane_releases_after_owner_process_death(tmp_path: Path):
    """The single browser slot must recover when its worker dies abruptly."""

    holder = """
from pathlib import Path
import sys
from src.browser.task_lane import try_acquire_browser_task_lane

lane = try_acquire_browser_task_lane(Path(sys.argv[1]))
if lane is None:
    print('BUSY', flush=True)
    raise SystemExit(2)
print('READY', flush=True)
sys.stdin.read()
lane.release()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", holder, str(tmp_path)],
        env=os.environ.copy(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "READY"
        assert try_acquire_browser_task_lane(tmp_path) is None
        process.kill()
        process.wait(timeout=5)
        recovered = try_acquire_browser_task_lane(tmp_path)
        assert recovered is not None
        recovered.release()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdin is not None:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def test_browser_lane_rejects_lock_path_inode_replacement_after_open(tmp_path: Path, monkeypatch):
    """A replaced directory entry must not receive the opened lock's lease."""

    original_flock = task_lane_module.fcntl.flock
    replaced = False

    def race_after_lock(descriptor, operation):
        nonlocal replaced
        result = original_flock(descriptor, operation)
        if not replaced and operation & task_lane_module.fcntl.LOCK_EX:
            replacement = tmp_path / ".replacement"
            replacement.write_bytes(b"replacement")
            os.chmod(replacement, 0o600)
            os.replace(replacement, tmp_path / ".browser-task-lane.lock")
            replaced = True
        return result

    monkeypatch.setattr(task_lane_module.fcntl, "flock", race_after_lock)
    with pytest.raises(BrowserTaskLaneError, match="identity"):
        try_acquire_browser_task_lane(tmp_path)


def test_browser_lane_quarantine_retains_descriptor_until_owner_death(tmp_path: Path):
    lane = try_acquire_browser_task_lane(tmp_path)
    assert lane is not None
    lane.quarantine("browser-task:cleanup-unknown")
    assert lane.quarantined is True
    assert try_acquire_browser_task_lane(tmp_path) is None
    lane.release()
    assert try_acquire_browser_task_lane(tmp_path) is None


@pytest.mark.asyncio
async def test_browser_lane_quarantine_is_a_read_only_ready_wait_projection(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    task = _browser_task(
        task_id="browser-ready-waiter",
        goal_id="goal-ready-waiter",
        status=WorkBoardStatus.ready,
    )
    lane = try_acquire_browser_task_lane(tmp_path)
    assert lane is not None
    lane.quarantine("browser-task:cleanup-unknown")
    try:
        assert browser_task_lane_wait_reason(tmp_path) == "browser_cleanup_required"
        payload = await _safe_task_payload(task)
        assert payload["status"] == "ready"
        assert payload["dispatch_wait_reason"] == "browser_cleanup_required"
        assert payload["task_revision"] == task.task_revision
    finally:
        task_lane_module._QUARANTINED_LANES.pop(str(lane.workspace_root), None)
        lane._quarantine_job_id = None
        lane.release()
    assert browser_task_lane_wait_reason(tmp_path) is None


@pytest.mark.asyncio
async def test_dispatch_pass_keeps_ready_task_when_lane_identity_fails(monkeypatch):
    task = SimpleNamespace(
        task_id="browser-lane-error",
        status=WorkBoardStatus.ready,
        capability_id="browser.public-task.v1",
        task_revision=1,
    )
    claimed = False

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Repository:
        async def list_dispatch_candidates(self, _db, **_kwargs):
            return [task]

        async def claim_ready_task(self, *_args, **_kwargs):
            nonlocal claimed
            claimed = True
            raise AssertionError("lane failure must be handled before claim")

    async def no_rows(*_args, **_kwargs):
        return 0

    async def no_reconcile(*_args, **_kwargs):
        return []

    dispatcher = WorkBoardDispatcher(
        repository=Repository(),
        session_provider=lambda: Session(),
    )
    dispatcher._expire_review_windows = no_rows
    dispatcher.reconcile_pending_attempts = no_reconcile
    dispatcher.reconcile_linked_attempts = no_reconcile

    async def readiness(_task):
        return None, None

    dispatcher._readiness = readiness

    async def empty_inbox(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(dispatcher_module, "expire_inbox_items", empty_inbox)
    monkeypatch.setattr(dispatcher_module, "repair_inbox_dispositions", empty_inbox)

    def raise_lane(_workspace):
        raise BrowserTaskLaneError("lock identity changed")

    monkeypatch.setattr(task_lane_module, "try_acquire_browser_task_lane", raise_lane)
    receipt = await dispatcher.run_pass()

    assert claimed is False
    assert task.status is WorkBoardStatus.ready
    assert receipt["wait_reasons"] == [
        {
            "task_id": task.task_id,
            "reason_code": "browser_lane_unavailable",
            "recovery_action": "restore_browser_lane",
        }
    ]


def test_capability_input_rejects_nested_authority_but_legacy_parser_keeps_generic_code():
    raw = _browser_input()
    raw["actions"][0]["metadata"] = {"owner_id": "forged"}
    with pytest.raises(TypedInputError) as exc_info:
        validate_capability_input("browser.public-task.v1", raw)
    assert exc_info.value.code == "typed_input_authority_field"


def test_browser_cleanup_projection_requires_typed_success_or_explicit_no_context():
    assert _browser_cleanup_projection(
        {
            "effects": [
                {
                    "receipt_kind": "effect",
                    "effect_type": "browser_context_cleanup",
                    "status": "succeeded",
                    "details": {"cleanup_status": "cleanup_verified", "memory_status": "no_learning"},
                }
            ]
        }
    ) == ("cleanup_verified", "no_learning")
    assert _browser_cleanup_projection(
        {
            "effects": [
                {
                    "receipt_kind": "effect",
                    "effect_type": "browser_context_cleanup",
                    "status": "succeeded",
                    "details": {"cleanup_status": "not_needed", "memory_status": "no_learning"},
                }
            ]
        }
    ) == ("unknown", "no_learning")
    assert _browser_cleanup_projection(
        {
            "effects": [
                {
                    "receipt_kind": "effect",
                    "effect_type": "browser_context_cleanup",
                    "status": "unknown",
                    "details": {"cleanup_status": "cleanup_verified", "memory_status": "no_learning"},
                }
            ]
        }
    ) == ("unknown", "no_learning")


def test_unregistered_and_non_task_capabilities_cannot_enter_artifact_storage(monkeypatch):
    with pytest.raises(TypedInputError) as exc_info:
        validate_capability_input("capability.unknown", {})
    assert exc_info.value.code == "capability_unregistered"

    monkeypatch.setitem(
        REGISTERED_CAPABILITIES,
        "capability.source-only",
        CapabilitySpec("capability.source-only", "1", input_category="source", secret_like=False),
    )
    with pytest.raises(TypedInputError) as exc_info:
        validate_capability_input("capability.source-only", {})
    assert exc_info.value.code == "typed_input_category_invalid"


@pytest.mark.asyncio
async def test_artifact_owner_digest_file_and_expiry_fences(async_db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    observed_at = datetime(2026, 9, 30, tzinfo=timezone.utc)
    before_expiry = observed_at + timedelta(hours=1)
    async with async_db() as db:
        await _browser_goal(
            db,
            goal_id="goal-artifact",
            budget=GoalAdmissionBudget(max_outstanding_jobs=1, max_attempts=1, max_runtime_seconds=120),
        )
        request = _artifact_request()
        metadata = await prepare_input_artifact(db, OWNER, request, now=observed_at)
        assert _artifact_path(metadata).is_file()

        with pytest.raises(BoardError) as owner_error:
            await resolve_input_artifact_for_task(
                db,
                OTHER_OWNER,
                artifact_id=metadata.artifact_id,
                goal_id=request.goal_id,
                goal_revision=1,
                capability_id=request.capability_id,
                now=before_expiry,
            )
        assert owner_error.value.code == "input_artifact_not_found"

        payload = bytearray(_artifact_path(metadata).read_bytes())
        payload[0] ^= 1
        _artifact_path(metadata).write_bytes(payload)
        with pytest.raises(BoardError) as digest_error:
            await resolve_input_artifact_for_task(
                db,
                OWNER,
                artifact_id=metadata.artifact_id,
                goal_id=request.goal_id,
                goal_revision=1,
                capability_id=request.capability_id,
                now=before_expiry,
            )
        assert digest_error.value.code == "input_artifact_digest_mismatch"

        # A fresh artifact proves the expiry branch independently of the file
        # tamper branch above.
        expiry_request = _artifact_request(idempotency_key="expiry-key")
        expiry_metadata = await prepare_input_artifact(
            db,
            OWNER,
            expiry_request,
            now=observed_at,
        )
        with pytest.raises(BoardError) as expiry_error:
            await resolve_input_artifact_for_task(
                db,
                OWNER,
                artifact_id=expiry_metadata.artifact_id,
                goal_id=expiry_request.goal_id,
                goal_revision=1,
                capability_id=expiry_request.capability_id,
                now=expiry_metadata.expires_at,
            )
        assert expiry_error.value.code == "input_artifact_expired"
        # The first artifact was created with the same fixed clock and is also
        # due at this exact boundary; both rows must be tombstoned and both
        # payload files removed.
        assert await expire_input_artifacts(db, now=expiry_metadata.expires_at) == 2
        assert not _artifact_path(metadata).exists()
        assert not _artifact_path(expiry_metadata).exists()


@pytest.mark.asyncio
async def test_artifact_secret_like_rejected_and_legacy_pending_replay_recovers(async_db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    async with async_db() as db:
        await _browser_goal(
            db,
            goal_id="goal-artifact",
            budget=GoalAdmissionBudget(max_outstanding_jobs=1, max_attempts=1, max_runtime_seconds=120),
        )
        with pytest.raises(BoardError) as secret_error:
            await prepare_input_artifact(
                db,
                OWNER,
                _artifact_request(
                    capability_id="engineering.repo-change.v1",
                    input={"candidate_id": "candidate", "repository_path": "repo", "patch_artifact_id": "patch", "patch_sha256": "a" * 64, "allowed_paths": ["file.txt"], "test_args": ["pytest"]},
                ),
            )
        assert secret_error.value.code == "secret_like_capability_blocked"

        request = _artifact_request(idempotency_key="pending-replay")
        metadata = await prepare_input_artifact(db, OWNER, request)
        row = (
            await db.execute(
                select(WorkBoardInputArtifact).where(
                    WorkBoardInputArtifact.artifact_id == metadata.artifact_id,
                )
            )
        ).scalar_one()
        row.metadata_digest = None
        row.revision = 1
        await db.flush()
        await db.commit()
        _artifact_path(metadata).unlink()

        recovered = await prepare_input_artifact(db, OWNER, request)
        assert recovered.artifact_id == metadata.artifact_id
        assert recovered.state == "pending"
        assert recovered.revision == 2
        assert _artifact_path(recovered).is_file()

        # Replay after terminal consumption must still validate canonical bytes
        # and return the same owner-bound receipt.
        resolved = await resolve_input_artifact_for_task(
            db,
            OWNER,
            artifact_id=recovered.artifact_id,
            goal_id=request.goal_id,
            goal_revision=1,
            capability_id=request.capability_id,
        )
        await bind_input_artifact(db, OWNER, artifact=resolved, task_id="task-replay", task_revision=1)
        await consume_input_artifact(
            db,
            OWNER,
            task_id="task-replay",
            task_revision=1,
            artifact_id=recovered.artifact_id,
        )
        replayed = await prepare_input_artifact(db, OWNER, request)
        assert replayed.artifact_id == recovered.artifact_id
        assert replayed.state == "consumed"


@pytest.mark.asyncio
async def test_artifact_delete_requires_terminal_bound_task_and_binding_cas(async_db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    async with async_db() as db:
        await _browser_goal(
            db,
            goal_id="goal-artifact",
            budget=GoalAdmissionBudget(max_outstanding_jobs=1, max_attempts=1, max_runtime_seconds=120),
        )
        request = _artifact_request(idempotency_key="delete-bound")
        metadata = await prepare_input_artifact(db, OWNER, request)
        resolved = await resolve_input_artifact_for_task(
            db,
            OWNER,
            artifact_id=metadata.artifact_id,
            goal_id=request.goal_id,
            goal_revision=1,
            capability_id=request.capability_id,
        )
        task = _browser_task(task_id="task-bound", goal_id=request.goal_id, status=WorkBoardStatus.todo)
        db.add(task)
        await db.flush()
        await bind_input_artifact(db, OWNER, artifact=resolved, task_id=task.task_id, task_revision=task.task_revision)
        bound = await resolve_input_artifact_for_task(
            db,
            OWNER,
            artifact_id=metadata.artifact_id,
            goal_id=request.goal_id,
            goal_revision=1,
            capability_id=request.capability_id,
            expected_task_id=task.task_id,
        )
        with pytest.raises(BoardError) as active_error:
            await delete_input_artifact(db, OWNER, artifact_id=metadata.artifact_id, expected_revision=bound.row.revision)
        assert active_error.value.code == "input_artifact_task_active"

        task.status = WorkBoardStatus.done
        await db.flush()
        stale_revision = int(bound.row.revision)
        deleted = await delete_input_artifact(db, OWNER, artifact_id=metadata.artifact_id, expected_revision=stale_revision)
        assert deleted.state == "deleted"
        assert not _artifact_path(metadata).exists()
        with pytest.raises(BoardError) as stale_delete:
            await delete_input_artifact(
                db,
                OWNER,
                artifact_id=metadata.artifact_id,
                expected_revision=stale_revision,
            )
        assert stale_delete.value.code == "input_artifact_revision_stale"

        # A detached pre-CAS view cannot bind a row changed by another writer.
        request2 = _artifact_request(idempotency_key="cas-bound")
        metadata2 = await prepare_input_artifact(db, OWNER, request2)
        stale = await resolve_input_artifact_for_task(
            db,
            OWNER,
            artifact_id=metadata2.artifact_id,
            goal_id=request2.goal_id,
            goal_revision=1,
            capability_id=request2.capability_id,
        )
        await db.commit()

    async with async_db() as db:
        current = await resolve_input_artifact_for_task(
            db,
            OWNER,
            artifact_id=metadata2.artifact_id,
            goal_id=request2.goal_id,
            goal_revision=1,
            capability_id=request2.capability_id,
        )
        await bind_input_artifact(db, OWNER, artifact=current, task_id="winner", task_revision=1)

    async with async_db() as db:
        with pytest.raises(BoardError) as cas_error:
            await bind_input_artifact(db, OWNER, artifact=stale, task_id="loser", task_revision=1)
        assert cas_error.value.code == "input_artifact_task_conflict"


@pytest.mark.asyncio
async def test_artifact_task_creation_rejects_stale_goal_before_binding(async_db, tmp_path, monkeypatch):
    """A goal revision change cannot bind a pending artifact to a task."""

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    async with async_db() as db:
        goal = await _browser_goal(
            db,
            goal_id="goal-artifact-stale",
            budget=GoalAdmissionBudget(max_outstanding_jobs=1, max_attempts=1, max_runtime_seconds=120),
        )
        request = _artifact_request(goal_id=goal.id, idempotency_key="stale-goal-artifact")
        metadata = await prepare_input_artifact(db, OWNER, request)
        await db.commit()

        goal.revision = 2
        await db.commit()

        task_request = WorkBoardTaskCreate(
            title="stale browser task",
            goal_id=goal.id,
            goal_revision=1,
            status=WorkBoardStatus.todo,
            capability_id=request.capability_id,
            input_artifact_id=metadata.artifact_id,
            idempotency_key="stale-goal-task",
        )
        with pytest.raises(BoardError) as stale_goal:
            await WorkBoardRepository().create_task(db, OWNER, task_request)
        assert stale_goal.value.code == "stale_goal_revision"
        await db.rollback()

        row = (
            await db.execute(
                select(WorkBoardInputArtifact).where(
                    WorkBoardInputArtifact.artifact_id == metadata.artifact_id,
                )
            )
        ).scalar_one()
        assert row.state == "pending"
        assert row.bound_task_id is None


@pytest.mark.asyncio
async def test_browser_readiness_rejects_draft_goal_before_claim(async_db, monkeypatch):
    async with async_db() as db:
        goal = await _browser_goal(
            db,
            goal_id="goal-browser-draft",
            budget=GoalAdmissionBudget(max_outstanding_jobs=1, max_attempts=1, max_runtime_seconds=120),
        )
        goal.status = "draft"
        task = _browser_task(
            task_id="browser-draft-task",
            goal_id=goal.id,
            status=WorkBoardStatus.todo,
        )
        db.add(task)
        await db.flush()
        await db.commit()

    async def authenticated(*_args, **_kwargs):
        return SimpleNamespace(principal=SimpleNamespace(principal_id=OWNER.principal_id))

    monkeypatch.setattr("src.work_board.dispatcher.authenticate_session", authenticated)
    dispatcher = WorkBoardDispatcher(session_provider=async_db)
    code, reason = await dispatcher._readiness(task)
    assert code == "goal_not_admitted"
    assert reason == "The browser task goal is not active"


@pytest.mark.asyncio
async def test_post_claim_readiness_allows_exact_final_attempt(async_db, monkeypatch, tmp_path):
    """The newly claimed final attempt is not rejected by its own budget."""

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    async with async_db() as db:
        goal = await _browser_goal(
            db,
            goal_id="goal-browser-final-attempt",
            budget=GoalAdmissionBudget(max_outstanding_jobs=1, max_attempts=1, max_runtime_seconds=120),
        )
        request = _artifact_request(
            goal_id=goal.id,
            idempotency_key="final-attempt-input",
        )
        metadata = await prepare_input_artifact(db, OWNER, request)
        task = _browser_task(
            task_id="browser-final-attempt",
            goal_id=goal.id,
            status=WorkBoardStatus.running,
        )
        task.task_revision = 2
        task.input_artifact_id = metadata.artifact_id
        task.typed_input_ref = metadata.typed_input_ref
        task.typed_input_digest = metadata.typed_input_digest
        db.add(task)
        await db.flush()
        resolved = await resolve_input_artifact_for_task(
            db,
            OWNER,
            artifact_id=metadata.artifact_id,
            goal_id=goal.id,
            goal_revision=1,
            capability_id=request.capability_id,
        )
        await bind_input_artifact(
            db,
            OWNER,
            artifact=resolved,
            task_id=task.task_id,
            task_revision=1,
        )
        attempt = WorkBoardAttempt(
            task_id=task.task_id,
            task_revision_at_claim=1,
            lease_owner="service:work-board",
            lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
            heartbeat_at=datetime.now(timezone.utc),
            fencing_token=1,
            executor_id=task.executor_id or "",
            started_at=datetime.now(timezone.utc),
            outcome="pending_admission",
        )
        db.add(attempt)
        await db.commit()

    async def authenticated(*_args, **_kwargs):
        return SimpleNamespace(principal=SimpleNamespace(principal_id=OWNER.principal_id))

    monkeypatch.setattr("src.work_board.dispatcher.authenticate_session", authenticated)
    dispatcher = WorkBoardDispatcher(session_provider=async_db)

    async def capability_preflight(_task, _goal, _inputs):
        return None, None

    dispatcher._capability_preflight = capability_preflight
    claim = SimpleNamespace(task=task, attempt=attempt)
    error, reason = await dispatcher._post_claim_readiness(claim)
    assert (error, reason) == (None, None)


@pytest.mark.asyncio
async def test_post_claim_readiness_rejects_forged_attempt_context(async_db, monkeypatch, tmp_path):
    """A different attempt or fence cannot consume the final-attempt allowance."""

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    async with async_db() as db:
        goal = await _browser_goal(
            db,
            goal_id="goal-browser-forged-attempt",
            budget=GoalAdmissionBudget(max_outstanding_jobs=1, max_attempts=1, max_runtime_seconds=120),
        )
        request = _artifact_request(
            goal_id=goal.id,
            idempotency_key="forged-attempt-input",
        )
        metadata = await prepare_input_artifact(db, OWNER, request)
        task = _browser_task(
            task_id="browser-forged-attempt",
            goal_id=goal.id,
            status=WorkBoardStatus.running,
        )
        task.task_revision = 2
        task.input_artifact_id = metadata.artifact_id
        task.typed_input_ref = metadata.typed_input_ref
        task.typed_input_digest = metadata.typed_input_digest
        db.add(task)
        await db.flush()
        resolved = await resolve_input_artifact_for_task(
            db,
            OWNER,
            artifact_id=metadata.artifact_id,
            goal_id=goal.id,
            goal_revision=1,
            capability_id=request.capability_id,
        )
        await bind_input_artifact(db, OWNER, artifact=resolved, task_id=task.task_id, task_revision=1)
        current_attempt = WorkBoardAttempt(
            attempt_id="current-final-attempt",
            task_id=task.task_id,
            task_revision_at_claim=1,
            lease_owner="service:work-board",
            lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
            heartbeat_at=datetime.now(timezone.utc),
            fencing_token=7,
            executor_id=task.executor_id or "",
            started_at=datetime.now(timezone.utc),
            outcome="pending_admission",
        )
        db.add(current_attempt)
        await db.commit()

    async def authenticated(*_args, **_kwargs):
        return SimpleNamespace(principal=SimpleNamespace(principal_id=OWNER.principal_id))

    monkeypatch.setattr("src.work_board.dispatcher.authenticate_session", authenticated)
    dispatcher = WorkBoardDispatcher(session_provider=async_db)

    async def capability_preflight(_task, _goal, _inputs):
        return None, None

    dispatcher._capability_preflight = capability_preflight
    forged = WorkBoardAttempt(
        attempt_id="forged-attempt",
        task_id=task.task_id,
        task_revision_at_claim=1,
        lease_owner="service:work-board",
        lease_expires_at=current_attempt.lease_expires_at,
        fencing_token=7,
    )
    error, reason = await dispatcher._post_claim_readiness(SimpleNamespace(task=task, attempt=forged))
    assert error == "attempt_limit"
    assert reason == "The board attempt limit has been exhausted"


def test_only_reviewed_capabilities_opt_into_public_artifact_storage():
    approved_public_capabilities = {
        "browser.public-task.v1",
        "calendar.meeting-prep.v1",
        "calendar.observe_due_events.v1",
        "guardian-routine.v2",
        "workflow.goal-snapshot-to-file",
        "guardian.research-watch.v1",
        "gmail.scan_metadata.v1",
        "work.mail-reply-draft.v1",
    }
    assert {
        capability_id
        for capability_id, spec in REGISTERED_CAPABILITIES.items()
        if spec.secret_like is False
    } == approved_public_capabilities
    assert all(
        spec.secret_like
        for capability_id, spec in REGISTERED_CAPABILITIES.items()
        if capability_id not in approved_public_capabilities
    )


def test_reviewed_procedure_input_rejects_malformed_executor_and_permission_fields():
    valid = {
        "routine_id": "routine-1",
        "version": 1,
        "expected_routine_revision": 1,
        "goal_id": "goal-1",
        "expected_goal_revision": 1,
        "parameters": {},
        "invocation_uuid": "01234567-89ab-cdef-0123-456789abcdef",
    }
    assert validate_capability_input("guardian-routine.v2", valid)["version"] == 1

    with pytest.raises(TypedInputError) as malformed:
        validate_capability_input("guardian-routine.v2", {**valid, "version": "1"})
    assert malformed.value.code == "typed_input_invalid"

    with pytest.raises(TypedInputError) as executor:
        validate_capability_input("guardian-routine.v2", {**valid, "executor_id": "seraph-work-board:forged"})
    assert executor.value.code == "typed_input_authority_field"

    with pytest.raises(TypedInputError) as permission:
        validate_capability_input("guardian-routine.v2", {**valid, "permissions": ["workspace_write"]})
    # ``permissions`` is rejected by the strict v2 envelope grammar. It is
    # not an accepted authority key, so the parser reports the generic
    # malformed-input code rather than treating it as a server-owned field.
    assert permission.value.code == "typed_input_invalid"


def test_browser_policy_dto_is_bounded_normalized_and_truthful(monkeypatch):
    monkeypatch.setattr(
        settings,
        "browser_site_allowlist",
        "HTTPS://Example.com, *.example.org, example.com",
    )
    monkeypatch.setattr(settings, "browser_site_blocklist", "localhost, .internal")
    confirmed = _browser_task_policy(effective_runtime_seconds=300)
    assert confirmed["policy_state"] == "confirmed"
    assert confirmed["allowlist"]["rules"] == ["example.com", "example.org"]
    assert confirmed["blocklist"]["rules"] == ["localhost", "internal"]
    assert confirmed["limits"]["max_runtime_seconds"] == 180
    assert confirmed["limits"]["hard_max_runtime_seconds"] == 180
    assert confirmed["limits"]["max_actions"] == 8
    assert confirmed["limits"]["max_navigations"] == 8
    assert confirmed["limits"]["max_requests"] == 32
    assert confirmed["limits"]["max_extract_bytes"] == 65_536
    assert confirmed["limits"]["max_browser_contexts"] == 1
    assert confirmed["limits"]["ready_capacity"] == 8
    assert confirmed["limits"]["max_attempts"] == 2
    assert confirmed["limits"]["max_outstanding_jobs"] == 8
    assert confirmed["limits"]["inference"] == "none"

    monkeypatch.setattr(settings, "browser_site_allowlist", None)
    unknown = _browser_task_policy(effective_runtime_seconds=30)
    assert unknown["policy_state"] == "unknown"
    assert unknown["policy_source"] is None
    assert unknown["allowlist"]["known"] is False


def test_browser_policy_rule_list_marks_truncation(monkeypatch):
    monkeypatch.setattr(
        settings,
        "browser_site_allowlist",
        ",".join(f"site-{index}.example" for index in range(55)),
    )
    monkeypatch.setattr(settings, "browser_site_blocklist", "")
    policy = _browser_task_policy(effective_runtime_seconds=30)
    assert len(policy["allowlist"]["rules"]) == 50
    assert policy["allowlist"]["truncated"] is True
    assert policy["blocklist"]["truncated"] is False


def test_input_artifact_post_projection_is_minimal_and_detail_reads_are_explicit():
    metadata = SimpleNamespace(
        artifact_id="artifact-1",
        typed_input_ref="workspace-json:artifacts/work-board/inputs/artifact-1.json",
        typed_input_digest="a" * 64,
        capability_id="browser.public-task.v1",
        goal_id="goal-1",
        goal_revision=1,
        expires_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
        state="pending",
        size_bytes=128,
        bound_task_id=None,
        bound_task_revision=None,
        revision=1,
    )
    post = _input_artifact_payload(metadata)
    assert set(post) == {
        "artifact_id",
        "typed_input_ref",
        "typed_input_digest",
        "capability_id",
        "goal_id",
        "goal_revision",
        "expires_at",
    }
    detail = _input_artifact_payload(metadata, include_details=True)
    assert detail["state"] == "pending"
    assert detail["size_bytes"] == 128


@pytest.mark.asyncio
async def test_browser_execution_projection_requires_exact_durable_binding(monkeypatch):
    task = _browser_task(
        task_id="browser-progress-task",
        goal_id="goal-progress",
        status=WorkBoardStatus.running,
    )
    task.input_artifact_id = "artifact-progress"
    task.typed_input_digest = "a" * 64
    task.priority = 40
    task.task_revision = 2
    attempt = WorkBoardAttempt(
        attempt_id="attempt-progress",
        task_id=task.task_id,
        workflow_run_id=f"browser-task:{task.task_id}:attempt-progress",
        task_revision_at_claim=1,
        fencing_token=3,
    )
    projection = {
        "job_id": attempt.workflow_run_id,
        "run_identity": attempt.workflow_run_id,
        "root_run_identity": attempt.workflow_run_id,
        "parent_run_identity": None,
        "parent_job_id": None,
        "input_digest": "e" * 64,
        "run_fingerprint": "e" * 64,
        "status": "running",
        "job_kind": "browser_public_task",
        "capability_version": "1",
        "session_id": OWNER.session_id,
        "operator_session_id": OWNER.session_id,
        "goal_id": task.goal_id,
        "goal_revision": task.goal_revision,
        "owner": {
            "kind": "service",
            "principal_id": "service:browser-task",
            "service_id": "service:browser-task",
        },
        "idempotency": {
            "scope": "work-board-attempt",
            "key": f"{task.task_id}:{attempt.attempt_id}",
        },
        "declared_authority": {
            "principal": "service:browser-task",
            "owner_kind": "service",
            "service_id": "service:browser-task",
            "operator_owner_principal_id": OWNER.principal_id,
            "operator_owner_session_id": OWNER.session_id,
            "goal_owner_principal_id": OWNER.principal_id,
            "goal_owner_session_id": OWNER.session_id,
            "goal_id": task.goal_id,
            "goal_revision": task.goal_revision,
                "board_task_revision": attempt.task_revision_at_claim + 1,
            "board_fencing_token": attempt.fencing_token,
            "priority": task.priority,
            "input_artifact_id": task.input_artifact_id,
            "input_artifact_digest": task.typed_input_digest,
            "input_envelope_digest": task.typed_input_digest,
            "browser_input_digest": "b" * 64,
            "action_consent_digest": "c" * 64,
            "capability_id": "browser.public-task.v1",
            "capability_version": "1",
            "action_count": 2,
        },
        "checkpoints": [
            {
                "checkpoint_id": "action-0-pre-dispatch",
                "payload": {"action_index": 0, "phase": "pre_dispatch"},
            },
            {
                "checkpoint_id": "network-progress-2",
                "payload": {"action_index": 0, "request_count": 2, "phase": "network_progress"},
            },
        ],
        "artifacts": [
            {
                "artifact_id": "art_progress",
                "artifact_type": "browser_public_task_result",
                    "file_path": "artifacts/work-board/browser/result-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json",
                "content_sha256": "d" * 64,
                "exists": True,
            }
        ],
        "effects": [
            {
                "receipt_kind": "readback",
                "effect_type": "browser_public_task_result",
                "status": "succeeded",
                    "target_path": "artifacts/work-board/browser/result-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json",
                "content_sha256": "d" * 64,
                "readback_id": "readback_progress",
                "verified_at": "2026-09-30T12:00:00+00:00",
                "details": {"verified": True},
            },
            {
                "receipt_kind": "effect",
                "effect_type": "browser_context_cleanup",
                "status": "succeeded",
                "details": {"cleanup_status": "cleanup_verified", "memory_status": "no_learning"},
            },
        ],
    }
    projection["authority_digest"] = _browser_projection_digest(projection["declared_authority"])

    async def get_job(job_id: str):
        assert job_id == attempt.workflow_run_id
        return projection

    monkeypatch.setattr("src.api.work_board.durable_job_repository.get_job", get_job)
    payload = await _browser_execution_payload(task, attempt)
    assert payload == {
        "capability_id": "browser.public-task.v1",
        "job_id": attempt.workflow_run_id,
        "durable_status": "running",
        "action_index": 0,
        "action_count": 2,
        "request_count": 2,
        "cleanup_status": "cleanup_verified",
        "memory_status": "no_learning",
        "readback_id": "readback_progress",
        "artifact_id": "art_progress",
        "file_path": "artifacts/work-board/browser/result-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json",
        "content_sha256": "d" * 64,
    }

    projection["declared_authority"]["goal_owner_session_id"] = "foreign-session"
    assert await _browser_execution_payload(task, attempt) is None


@pytest.mark.asyncio
async def test_browser_dispatcher_consumes_successful_terminal_receipt_without_reconciliation(monkeypatch):
    """The production adapter must project a runner success through Done.

    This covers the glue after ``BrowserTaskRunner.run`` returns: cleanup and
    typed readback are checked from the durable projection, then the bound
    input is consumed and the same board attempt is settled.  A previous
    call-site passed a default argument to the one-argument dispatcher
    ``_text`` helper, so every otherwise-valid execution fell into generic
    unknown-effect reconciliation before this boundary.
    """

    task = _browser_task(
        task_id="browser-terminal-glue",
        goal_id="goal-terminal-glue",
        status=WorkBoardStatus.running,
    )
    task.task_revision = 2
    task.input_artifact_id = "artifact-terminal-glue"
    task.typed_input_digest = "a" * 64
    attempt = WorkBoardAttempt(
        attempt_id="attempt-terminal-glue",
        task_id=task.task_id,
        task_revision_at_claim=1,
        fencing_token=3,
        lease_owner="service:work-board",
    )
    claim = SimpleNamespace(task=task, attempt=attempt, event=None)
    job_id = f"browser-task:{task.task_id}:{attempt.attempt_id}"
    digest = "b" * 64
    artifact_path = task_runner_module.browser_artifact_path_for_job(job_id)
    canonical_artifact_id = artifact_id_for(
        file_path=artifact_path,
        artifact_type="browser_public_task_result",
        producer="browser_public_task",
        run_id=job_id,
        content_sha256=digest,
    )
    readback_id = "readback-terminal-glue"
    projection = {
        "job_id": job_id,
        "run_identity": job_id,
        "root_run_identity": job_id,
        "status": "accepted",
        "effects": [],
        "artifacts": [],
    }
    completed_projection = {
        **projection,
        "status": "succeeded",
        "artifacts": [
            {
                "artifact_id": canonical_artifact_id,
                "artifact_type": "browser_public_task_result",
                "producer": "browser_public_task",
                "file_path": artifact_path,
                "content_sha256": digest,
                "exists": True,
            }
        ],
        "effects": [
            {
                "effect_type": "browser_public_task_result",
                "receipt_kind": "readback",
                "status": "succeeded",
                "target_path": artifact_path,
                "target_digest": digest,
                "content_sha256": digest,
                "readback_id": readback_id,
                "verified_at": "2026-09-30T12:00:00+00:00",
                "details": {"verified": True},
            },
            {
                "effect_type": "browser_context_cleanup",
                "receipt_kind": "effect",
                "status": "succeeded",
                "details": {"cleanup_status": "cleanup_verified", "memory_status": "no_learning"},
            },
        ],
    }

    class Jobs:
        def __init__(self):
            self.calls = 0

        async def get_job(self, requested_job_id):
            assert requested_job_id == job_id
            self.calls += 1
            return projection if self.calls == 1 else completed_projection

    class Lane:
        def __init__(self):
            self.released = False
            self.quarantined = False

        def release(self):
            self.released = True

        def quarantine(self, _job_id):
            self.quarantined = True

    class Runner:
        def __init__(self):
            self.calls = 0

        async def run(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"status": "admitted", "job_id": job_id}
            return {
                "status": "succeeded",
                "job_id": job_id,
                "cleanup_status": "cleanup_verified",
                "artifact_ref": artifact_path,
                "artifact_sha256": digest,
                "readback_id": readback_id,
            }

    jobs = Jobs()
    runner = Runner()
    dispatcher = WorkBoardDispatcher(repository=SimpleNamespace(), jobs=jobs)
    linked_attempt = WorkBoardAttempt(
        attempt_id=attempt.attempt_id,
        task_id=task.task_id,
        workflow_run_id=job_id,
        task_revision_at_claim=attempt.task_revision_at_claim,
        fencing_token=attempt.fencing_token,
        lease_owner=attempt.lease_owner,
    )
    linked_task = SimpleNamespace(**task.__dict__)
    linked_task.task_revision = 3
    linked = SimpleNamespace(task=linked_task, attempt=linked_attempt)
    projected = []
    lane = Lane()

    monkeypatch.setattr(task_runner_module, "BrowserTaskRunner", lambda **_kwargs: runner)
    monkeypatch.setattr(dispatcher_module, "_parse_typed_input", lambda _task: _browser_input())
    monkeypatch.setattr(dispatcher, "_browser_expected_identity", lambda *_args, **_kwargs: {})

    async def link(_claim, *_args, **_kwargs):
        return linked

    async def project(*_args, **kwargs):
        projected.append(kwargs)

    async def resolve(*_args, **_kwargs):
        return SimpleNamespace(row=SimpleNamespace(bound_task_revision=1, artifact_id=task.input_artifact_id))

    async def consume(*_args, **_kwargs):
        return None

    monkeypatch.setattr(dispatcher, "_link_browser_attempt", link)
    monkeypatch.setattr(dispatcher, "_project", project)
    monkeypatch.setattr("src.work_board.input_artifacts.resolve_input_artifact_for_task", resolve)
    monkeypatch.setattr("src.work_board.input_artifacts.consume_input_artifact", consume)

    result = await dispatcher._admit_execute_browser(
        claim,
        runtime_seconds=180,
        max_attempts=2,
        max_outstanding_jobs=1,
        browser_lane=lane,
    )

    assert result == {"admitted": True, "completed": True, "blocked": False}
    assert runner.calls == 2
    assert jobs.calls == 2
    assert lane.released is True
    assert lane.quarantined is False
    assert projected and projected[0]["status"] is WorkBoardStatus.done
    assert projected[0]["outcome"] == "verified"
    assert projected[0]["artifact_refs"] == [
        {
            "artifact_id": canonical_artifact_id,
            "file_path": artifact_path,
            "content_sha256": digest,
            "workflow_run_id": job_id,
            "readback_id": readback_id,
            "verified": True,
            "verified_at": "2026-09-30T12:00:00+00:00",
        }
    ]


@pytest.mark.asyncio
async def test_browser_goal_budget_one_blocks_sibling_promotion(async_db):
    repository = WorkBoardRepository()
    async with async_db() as db:
        await _browser_goal(
            db,
            goal_id="goal-browser-capacity",
            budget=GoalAdmissionBudget(
                reviewed_grant=False,
                max_outstanding_jobs=1,
                max_attempts=1,
                max_runtime_seconds=120,
            ),
        )
        first = _browser_task(
            task_id="browser-capacity-first",
            goal_id="goal-browser-capacity",
            status=WorkBoardStatus.ready,
        )
        second = _browser_task(
            task_id="browser-capacity-second",
            goal_id="goal-browser-capacity",
            status=WorkBoardStatus.todo,
        )
        db.add_all([first, second])
        await db.flush()

        mutation = await repository.promote_task_ready(
            db,
            second.task_id,
            expected_revision=second.task_revision,
            actor_principal_id="service:work-board",
        )

        assert mutation is None
        assert second.status is WorkBoardStatus.todo
        assert second.block_reason == "browser_goal_outstanding_capacity"

        first.status = WorkBoardStatus.done
        await db.flush()
        promoted = await repository.promote_task_ready(
            db,
            second.task_id,
            expected_revision=second.task_revision,
            actor_principal_id="service:work-board",
        )
        assert promoted is not None
        assert promoted.task.status is WorkBoardStatus.ready
        assert promoted.task.block_reason is None


@pytest.mark.asyncio
async def test_browser_goal_budget_one_blocks_second_attempt(async_db):
    repository = WorkBoardRepository()
    async with async_db() as db:
        await _browser_goal(
            db,
            goal_id="goal-browser-attempt",
            budget=GoalAdmissionBudget(
                reviewed_grant=False,
                max_outstanding_jobs=2,
                max_attempts=1,
                max_runtime_seconds=120,
            ),
        )
        task = _browser_task(
            task_id="browser-attempt-limit",
            goal_id="goal-browser-attempt",
            status=WorkBoardStatus.ready,
        )
        db.add(task)
        await db.flush()
        db.add(
            WorkBoardAttempt(
                task_id=task.task_id,
                task_revision_at_claim=1,
                executor_id=task.executor_id or "",
                started_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                ended_at=datetime.now(timezone.utc),
                outcome="failed",
            )
        )
        await db.flush()

        claim = await repository.claim_ready_task(
            db,
            task.task_id,
            expected_revision=task.task_revision,
            lease_owner="service:work-board",
        )

        assert claim is None
        assert task.status is WorkBoardStatus.blocked
        assert task.block_kind == "attempt_limit"


@pytest.mark.asyncio
async def test_browser_claim_rechecks_current_goal_outstanding_budget(async_db):
    repository = WorkBoardRepository()
    async with async_db() as db:
        await _browser_goal(
            db,
            goal_id="goal-browser-claim-capacity",
            budget=GoalAdmissionBudget(
                reviewed_grant=False,
                max_outstanding_jobs=1,
                max_attempts=2,
                max_runtime_seconds=120,
            ),
        )
        sibling = _browser_task(
            task_id="browser-claim-capacity-sibling",
            goal_id="goal-browser-claim-capacity",
            status=WorkBoardStatus.ready,
        )
        task = _browser_task(
            task_id="browser-claim-capacity-current",
            goal_id="goal-browser-claim-capacity",
            status=WorkBoardStatus.ready,
        )
        db.add_all([sibling, task])
        await db.flush()

        claim = await repository.claim_ready_task(
            db,
            task.task_id,
            expected_revision=task.task_revision,
            lease_owner="service:work-board",
        )

        assert claim is None
        assert task.status is WorkBoardStatus.ready
        assert task.task_revision == 1
