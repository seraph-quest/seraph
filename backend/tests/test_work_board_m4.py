"""Provider-free contract tests for the M4 review, handoff, and triage paths.

These tests intentionally construct durable board and workflow rows directly.
That keeps the assertions focused on the M4 authority boundaries and avoids
contacting OpenRouter, GitHub, or any other external transport.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from config.settings import settings
from src.db import engine as db_engine
from src.db.models import (
    Goal,
    Session,
    WorkBoardAttempt,
    WorkBoardHandoff,
    WorkBoardLink,
    WorkBoardProposal,
    WorkBoardReviewIntent,
    WorkBoardStatus,
    WorkBoardTask,
    WorkflowRunState,
)
from src.work_board import review as review_service
from src.work_board import triage as triage_service
from src.work_board.contracts import (
    WorkBoardLinkCreate,
    WorkBoardOwner,
    WorkBoardProposalAccept,
    WorkBoardProposalRequest,
)
from src.work_board.dispatcher import (
    TypedInputError,
    WorkBoardDispatcher,
    registered_executor_id,
)
from src.work_board.repository import (
    BoardError,
    BoardRevisionConflict,
    WorkBoardRepository,
)


OWNER = WorkBoardOwner(principal_id="operator:m4", session_id="session:m4")
FRESH_SESSION = WorkBoardOwner(principal_id=OWNER.principal_id, session_id="session:fresh")
OTHER_OWNER = WorkBoardOwner(principal_id="operator:other", session_id="session:other")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _goal(db, *, goal_id: str = "goal-m4", owner: WorkBoardOwner = OWNER, revision: int = 1):
    goal = Goal(
        id=goal_id,
        title="M4 contract goal",
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        revision=revision,
        status="active",
    )
    db.add(goal)
    await db.flush()
    return goal


async def _task(
    db,
    *,
    task_id: str,
    owner: WorkBoardOwner = OWNER,
    status: WorkBoardStatus = WorkBoardStatus.review,
    task_revision: int = 1,
    goal_id: str = "goal-m4",
    requires_review: bool = True,
    reviewer_id: str | None = None,
    capability_id: str | None = None,
    executor_id: str | None = None,
    body: str = "",
    review_expires_at: datetime | None = None,
) -> WorkBoardTask:
    task = WorkBoardTask(
        task_id=task_id,
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        origin_session_id=owner.session_id,
        goal_id=goal_id,
        goal_revision=1,
        title=f"Task {task_id}",
        body=body,
        capability_id=capability_id,
        executor_id=executor_id,
        idempotency_key=f"key-{task_id}",
        status=status,
        requires_review=requires_review,
        reviewer_id=reviewer_id,
        task_revision=task_revision,
        review_expires_at=review_expires_at,
    )
    db.add(task)
    await db.flush()
    return task


async def _verified_execution(
    db,
    task: WorkBoardTask,
    *,
    attempt_id: str,
    run_id: str,
    worker: str = "executor:m4",
    status: str = "succeeded",
    verified: bool = True,
) -> WorkBoardAttempt:
    digest = hashlib.sha256(f"artifact:{attempt_id}".encode()).hexdigest()
    db.add(
        WorkflowRunState(
            run_identity=run_id,
            root_run_identity=run_id,
            workflow_name="m4-test-workflow",
            operator_session_id=task.owner_session_id,
            status=status,
            owner_kind="user",
            owner_principal_id=task.owner_principal_id,
            goal_id=task.goal_id,
            goal_revision=task.goal_revision,
        )
    )
    attempt = WorkBoardAttempt(
        attempt_id=attempt_id,
        task_id=task.task_id,
        workflow_run_id=run_id,
        task_revision_at_claim=task.task_revision,
        lease_owner=worker,
        fencing_token=7,
        executor_id=worker,
        started_at=_now() - timedelta(seconds=3),
        ended_at=_now(),
        outcome="succeeded" if status == "succeeded" else "failed",
        receipt_refs_json=json.dumps(
            [
                {
                    "receipt_kind": "readback",
                    "artifact_id": f"artifact-{attempt_id}",
                    "readback_id": f"readback-{attempt_id}",
                    "workflow_run_id": run_id,
                    "status": "succeeded",
                    "verified": verified,
                    "content_sha256": digest,
                    "verified_at": _now().isoformat(),
                }
            ]
        ),
    )
    db.add(attempt)
    await db.flush()
    return attempt


async def _project_review_intent(db, task: WorkBoardTask, attempt: WorkBoardAttempt) -> None:
    task.review_request_attempt_id = attempt.attempt_id
    task.review_request_fence = attempt.fencing_token
    task.review_request_revision = task.task_revision
    task.review_request_digest = "review-intent-digest"
    task.review_request_evidence_json = json.dumps([f"artifact-{attempt.attempt_id}"])
    db.add(
        WorkBoardReviewIntent(
            owner_principal_id=task.owner_principal_id,
            owner_session_id=task.owner_session_id,
            task_id=task.task_id,
            attempt_id=attempt.attempt_id,
            workflow_run_id=attempt.workflow_run_id or "",
            fencing_token=attempt.fencing_token,
            task_revision=task.task_revision,
            request_digest=task.review_request_digest,
            evidence_refs_json=task.review_request_evidence_json,
            status="projected",
        )
    )
    await db.flush()


@pytest.mark.asyncio
async def test_review_requires_authoritative_run_and_readback(async_db):
    async with async_db() as db:
        await _goal(db)
        task = await _task(
            db,
            task_id="review-authority",
            status=WorkBoardStatus.running,
            reviewer_id=OWNER.principal_id,
            executor_id="executor:m4",
        )
        attempt = await _verified_execution(
            db,
            task,
            attempt_id="attempt-review-authority",
            run_id="run-review-authority",
            status="failed",
        )

        with pytest.raises(BoardError, match="authoritative successful workflow readback") as failed:
            await review_service.request_review(
                db,
                OWNER,
                task.task_id,
                expected_revision=task.task_revision,
                attempt_id=attempt.attempt_id,
                evidence_refs=[f"artifact-{attempt.attempt_id}"],
            )
        assert failed.value.code == "verified_readback_required"
        assert task.status is WorkBoardStatus.running

        run = (
            await db.execute(
                select(WorkflowRunState).where(
                    WorkflowRunState.run_identity == attempt.workflow_run_id
                )
            )
        ).scalar_one()
        run.status = "succeeded"
        await db.flush()
        mutation = await review_service.request_review(
            db,
            OWNER,
            task.task_id,
            expected_revision=task.task_revision,
            attempt_id=attempt.attempt_id,
            evidence_refs=[f"artifact-{attempt.attempt_id}"],
        )
        assert mutation.task.status is WorkBoardStatus.running
        assert mutation.task.review_request_attempt_id == attempt.attempt_id
        assert mutation.task.review_expires_at is None


@pytest.mark.asyncio
async def test_owner_review_is_authenticated_and_fresh_session_is_denied(async_db):
    async with async_db() as db:
        await _goal(db)
        task = await _task(
            db,
            task_id="review-separation",
            reviewer_id=OWNER.principal_id,
            executor_id=OWNER.principal_id,
        )
        attempt = await _verified_execution(
            db,
            task,
            attempt_id="attempt-review-separation",
            run_id="run-review-separation",
            worker=OWNER.principal_id,
        )
        await _project_review_intent(db, task, attempt)
        task.review_expires_at = _now() + timedelta(days=1)
        await db.flush()

        with pytest.raises(BoardError) as fresh_session:
            await review_service.complete_review(
                db,
                FRESH_SESSION,
                task.task_id,
                expected_revision=task.task_revision,
                attempt_id=attempt.attempt_id,
            )
        assert fresh_session.value.code == "task_owner_mismatch"

        # Executor and authenticated reviewer IDs are separate namespaces.
        # The worker has no complete_review tool; the named owner performs
        # the verdict through the authenticated review API.
        completed = await review_service.complete_review(
            db,
            OWNER,
            task.task_id,
            expected_revision=task.task_revision,
            attempt_id=attempt.attempt_id,
        )
        assert completed.task.status is WorkBoardStatus.done


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation_name", ["request_changes", "complete_review", "renew_review"])
async def test_review_mutations_recheck_goal_at_serialized_mutation_boundary(
    async_db,
    monkeypatch,
    mutation_name,
):
    """A goal revision changed at the review lock cannot win a verdict CAS."""

    async with async_db() as db:
        goal = await _goal(db, goal_id=f"goal-review-boundary-{mutation_name}")
        initial_status = (
            WorkBoardStatus.blocked
            if mutation_name == "renew_review"
            else WorkBoardStatus.review
        )
        task = await _task(
            db,
            task_id=f"review-boundary-{mutation_name}",
            goal_id=goal.id,
            status=initial_status,
            reviewer_id=OWNER.principal_id,
            requires_review=True,
            review_expires_at=(
                _now() - timedelta(minutes=1)
                if mutation_name == "renew_review"
                else _now() + timedelta(days=1)
            ),
        )
        task.block_kind = "review_expired" if mutation_name == "renew_review" else None
        task.block_source_status = WorkBoardStatus.review.value if mutation_name == "renew_review" else None
        task.block_reason = "Review expired" if mutation_name == "renew_review" else None
        attempt = await _verified_execution(
            db,
            task,
            attempt_id=f"attempt-review-boundary-{mutation_name}",
            run_id=f"run-review-boundary-{mutation_name}",
        )
        await _project_review_intent(db, task, attempt)
        await db.flush()

        original_begin = review_service._begin_sqlite_immediate

        async def revoke_goal_at_boundary(session):
            await original_begin(session)
            goal.revision = 2
            await session.flush()

        monkeypatch.setattr(review_service, "_begin_sqlite_immediate", revoke_goal_at_boundary)
        mutation = getattr(review_service, mutation_name)
        kwargs = {
            "db": db,
            "owner": OWNER,
            "task_id": task.task_id,
            "expected_revision": task.task_revision,
        }
        if mutation_name == "complete_review":
            kwargs["attempt_id"] = attempt.attempt_id
        elif mutation_name == "request_changes":
            kwargs["reason"] = "The review needs one bounded correction."

        with pytest.raises(BoardError) as blocked:
            await mutation(**kwargs)

        assert blocked.value.code == "stale_goal_revision"
        assert task.status is initial_status


@pytest.mark.asyncio
async def test_changes_requested_keeps_attempt_and_rechecks_ready(async_db, monkeypatch):
    async with async_db() as db:
        await _goal(db)
        task = await _task(
            db,
            task_id="review-changes",
            reviewer_id=OWNER.principal_id,
            executor_id="executor:m4",
        )
        attempt = await _verified_execution(
            db,
            task,
            attempt_id="attempt-review-changes",
            run_id="run-review-changes",
        )
        await _project_review_intent(db, task, attempt)
        task.review_expires_at = _now() + timedelta(days=1)
        await db.flush()

        monkeypatch.setattr(
            "src.work_board.dispatcher._dispatcher._readiness",
            AsyncMock(return_value=(None, None)),
        )
        with pytest.raises(BoardRevisionConflict):
            await review_service.request_changes(
                db,
                OWNER,
                task.task_id,
                expected_revision=99,
                reason="stale review verdict",
            )

        mutation = await review_service.request_changes(
            db,
            OWNER,
            task.task_id,
            expected_revision=task.task_revision,
            reason="Please include the independent readback receipt.",
        )
        assert mutation.task.status is WorkBoardStatus.ready
        assert mutation.task.task_revision == 2
        assert mutation.task.review_expires_at is None
        retained = await db.get(WorkBoardAttempt, attempt.attempt_id)
        assert retained is not None
        assert retained.ended_at is not None
        assert retained.workflow_run_id == "run-review-changes"


@pytest.mark.asyncio
async def test_changes_requested_keeps_future_scheduled_task_in_todo(async_db, monkeypatch):
    async with async_db() as db:
        await _goal(db)
        task = await _task(
            db,
            task_id="review-changes-future-schedule",
            reviewer_id=OWNER.principal_id,
            executor_id="executor:m4",
        )
        attempt = await _verified_execution(
            db,
            task,
            attempt_id="attempt-review-changes-future-schedule",
            run_id="run-review-changes-future-schedule",
        )
        await _project_review_intent(db, task, attempt)
        task.review_expires_at = _now() + timedelta(days=1)
        task.scheduled_at = _now() + timedelta(days=1)
        await db.flush()

        monkeypatch.setattr(
            "src.work_board.dispatcher._dispatcher._readiness",
            AsyncMock(return_value=(None, None)),
        )
        mutation = await review_service.request_changes(
            db,
            OWNER,
            task.task_id,
            expected_revision=task.task_revision,
            reason="Keep this task in Todo until its schedule is eligible.",
        )

        assert mutation.task.status is WorkBoardStatus.todo
        assert mutation.task.scheduled_at == task.scheduled_at
        retained = await db.get(WorkBoardAttempt, attempt.attempt_id)
        assert retained is not None
        assert retained.workflow_run_id == "run-review-changes-future-schedule"


@pytest.mark.asyncio
async def test_changes_requested_exhausted_attempts_stays_blocked(async_db, monkeypatch):
    async with async_db() as db:
        await _goal(db)
        task = await _task(
            db,
            task_id="review-changes-attempt-limit",
            reviewer_id=OWNER.principal_id,
            executor_id="executor:m4",
        )
        first = await _verified_execution(
            db,
            task,
            attempt_id="attempt-review-changes-limit-1",
            run_id="run-review-changes-limit-1",
        )
        _second = await _verified_execution(
            db,
            task,
            attempt_id="attempt-review-changes-limit-2",
            run_id="run-review-changes-limit-2",
        )
        current_attempt = (
            await db.execute(
                select(WorkBoardAttempt)
                .where(WorkBoardAttempt.task_id == task.task_id)
                .order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc())
                .limit(1)
            )
        ).scalar_one()
        await _project_review_intent(db, task, current_attempt)
        task.review_expires_at = _now() + timedelta(days=1)
        await db.flush()

        monkeypatch.setattr(
            "src.work_board.dispatcher._dispatcher._readiness",
            AsyncMock(return_value=(None, None)),
        )
        mutation = await review_service.request_changes(
            db,
            OWNER,
            task.task_id,
            expected_revision=task.task_revision,
            reason="The bounded attempt budget is exhausted.",
        )

        assert mutation.task.status is WorkBoardStatus.blocked
        assert mutation.task.block_kind == "attempt_limit"
        assert mutation.task.block_source_status == WorkBoardStatus.review.value
        assert "create a new linked task" in mutation.task.block_reason
        assert first.ended_at is not None


@pytest.mark.asyncio
async def test_changes_requested_rerun_claims_a_new_fenced_attempt(async_db, monkeypatch):
    capability_id = "workflow.goal-snapshot-to-file"
    executor_id = registered_executor_id(capability_id)
    async with async_db() as db:
        await _goal(db)
        task = await _task(
            db,
            task_id="review-changes-rerun-attempt",
            reviewer_id=OWNER.principal_id,
            capability_id=capability_id,
            executor_id=executor_id,
        )
        prior = await _verified_execution(
            db,
            task,
            attempt_id="attempt-review-changes-rerun-prior",
            run_id="run-review-changes-rerun-prior",
        )
        prior_run = (
            await db.execute(
                select(WorkflowRunState).where(
                    WorkflowRunState.run_identity == prior.workflow_run_id
                )
            )
        ).scalar_one()
        db.add(Session(id=OWNER.session_id, owner_principal_id=OWNER.principal_id))
        await db.flush()
        prior_run.job_kind = capability_id
        prior_run.capability_version = "1"
        prior_run.session_id = task.owner_session_id
        prior_run.idempotency_scope = "work-board-attempt"
        prior_run.idempotency_key = f"{task.task_id}:{prior.attempt_id}"
        prior_run.arguments_json = json.dumps({"redacted": True, "shape": "dict"})
        prior_run.input_digest = "a" * 64
        prior_run.run_fingerprint = "b" * 64
        await _project_review_intent(db, task, prior)
        task.review_expires_at = _now() + timedelta(days=1)
        await db.flush()

        monkeypatch.setattr(
            "src.work_board.dispatcher._dispatcher._readiness",
            AsyncMock(return_value=(None, None)),
        )
        reopened = await review_service.request_changes(
            db,
            OWNER,
            task.task_id,
            expected_revision=task.task_revision,
            reason="Add the missing output receipt.",
        )
        assert reopened.task.status is WorkBoardStatus.ready

        claim = await WorkBoardRepository().claim_ready_task(
            db,
            task.task_id,
            expected_revision=reopened.task.task_revision,
            lease_owner="service:work-board",
            actor_principal_id="service:work-board",
            actor_session_id="service-session:work-board",
        )
        assert claim is not None
        assert claim.attempt.attempt_id != prior.attempt_id
        assert claim.attempt.fencing_token > prior.fencing_token
        # Admission is a separate durable step; a crashed claim is intentionally
        # left pending and cannot pretend to have a workflow run already.
        assert claim.attempt.workflow_run_id is None


@pytest.mark.asyncio
async def test_review_expiry_requires_dispatcher_sweep_and_explicit_renewal(async_db):
    expired_at = _now() - timedelta(minutes=1)
    async with async_db() as db:
        await _goal(db)
        task = await _task(
            db,
            task_id="review-expiry",
            reviewer_id=OWNER.principal_id,
            executor_id="executor:m4",
            review_expires_at=expired_at,
        )
        attempt = await _verified_execution(
            db,
            task,
            attempt_id="attempt-review-expiry",
            run_id="run-review-expiry",
        )
        await _project_review_intent(db, task, attempt)
        task.review_expires_at = expired_at
        await db.flush()

    dispatcher = WorkBoardDispatcher(session_provider=async_db)
    assert await dispatcher._expire_review_windows(now=_now()) == 1

    async with async_db() as db:
        task = (
            await db.execute(
                select(WorkBoardTask).where(WorkBoardTask.task_id == "review-expiry")
            )
        ).scalar_one()
        assert task.status is WorkBoardStatus.blocked
        assert task.block_kind == "review_expired"
        assert task.block_source_status == WorkBoardStatus.review.value
        expired_revision = task.task_revision

        with pytest.raises(BoardError) as generic:
            await review_service.unblock_task(
                db,
                OWNER,
                task.task_id,
                expected_revision=expired_revision,
                resolution="retry the review",
            )
        assert generic.value.code == "review_renewal_required"

        renewed = await review_service.renew_review(
            db,
            OWNER,
            task.task_id,
            expected_revision=expired_revision,
        )
        assert renewed.task.status is WorkBoardStatus.review
        assert renewed.task.review_expires_at is not None
        renewed_expiry = renewed.task.review_expires_at
        if renewed_expiry.tzinfo is None:
            renewed_expiry = renewed_expiry.replace(tzinfo=timezone.utc)
        assert renewed_expiry > _now()
        assert renewed.task.task_revision == expired_revision + 1
        assert len(
            (
                await db.execute(
                    select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task.task_id)
                )
            ).scalars().all()
        ) == 1


@pytest.mark.asyncio
async def test_review_expiry_cas_race_does_not_stop_later_rows(async_db, monkeypatch):
    expired_at = _now() - timedelta(minutes=1)
    async with async_db() as db:
        await _goal(db)
        await _task(
            db,
            task_id="review-expiry-cas-loser",
            status=WorkBoardStatus.review,
            review_expires_at=expired_at,
        )
        await _task(
            db,
            task_id="review-expiry-after-cas-loser",
            status=WorkBoardStatus.review,
            review_expires_at=expired_at,
        )

    real_expire_review = review_service.expire_review

    async def lose_first_cas_then_continue(db, owner, task_id, *, repository=None):
        if task_id == "review-expiry-cas-loser":
            raise BoardRevisionConflict(task_id, 1, 2)
        return await real_expire_review(
            db,
            owner,
            task_id,
            repository=repository,
        )

    monkeypatch.setattr(review_service, "expire_review", lose_first_cas_then_continue)
    dispatcher = WorkBoardDispatcher(session_provider=async_db)
    assert await dispatcher._expire_review_windows(now=_now()) == 1

    async with async_db() as db:
        states = {
            task.task_id: task.status
            for task in (
                await db.execute(
                    select(WorkBoardTask).where(
                        WorkBoardTask.task_id.in_(
                            ["review-expiry-cas-loser", "review-expiry-after-cas-loser"]
                        )
                    )
                )
            ).scalars().all()
        }
        assert states["review-expiry-cas-loser"] is WorkBoardStatus.review
        assert states["review-expiry-after-cas-loser"] is WorkBoardStatus.blocked


@pytest.mark.asyncio
async def test_verified_done_parent_materializes_bounded_current_handoff(async_db):
    async with async_db() as db:
        await _goal(db)
        parent = await _task(
            db,
            task_id="handoff-parent",
            status=WorkBoardStatus.done,
            requires_review=False,
            body="private source body and credential=do-not-forward",
        )
        child = await _task(
            db,
            task_id="handoff-child",
            status=WorkBoardStatus.todo,
            requires_review=False,
        )
        parent.result_refs_json = json.dumps([{"artifact_id": "artifact-handoff"}])
        parent.artifact_refs_json = json.dumps([{"readback_id": "readback-handoff"}])
        attempt = await _verified_execution(
            db,
            parent,
            attempt_id="attempt-handoff",
            run_id="run-handoff",
        )
        link = WorkBoardLink(
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            parent_task_id=parent.task_id,
            child_task_id=child.task_id,
        )
        db.add(link)
        await db.flush()

        handoff = await review_service.materialize_handoff_for_link(
            db,
            OWNER,
            parent,
            child,
            link,
        )
        assert link.current_handoff_id == handoff.handoff_id
        assert handoff.source_attempt_id == attempt.attempt_id
        assert handoff.source_task_revision == parent.task_revision
        assert len(handoff.verification_json.encode()) <= 4096
        assert "private source body" not in handoff.summary
        summary = json.loads(handoff.summary)
        assert summary["result"]["workflow_status"] == "succeeded"
        assert summary["result"]["verification"] == "independent_readback_passed"
        assert summary["result"]["capability_id"] == "legacy.untyped"
        assert summary["result"]["result_summary"] == "Workflow output completed and passed independent readback."
        assert summary["result"]["artifact_count"] == 1
        payload = await review_service.parent_handoffs(db, OWNER, child)
        assert payload[0]["handoff_id"] == handoff.handoff_id
        assert payload[0]["verification_receipt"]["status"] == "verified"
        assert payload[0]["source_attempt_id"] == attempt.attempt_id


@pytest.mark.asyncio
async def test_fenced_attempt_delivers_verified_parent_context_to_capability_adapter(
    async_db,
    monkeypatch,
):
    awaitable_context: dict[str, object] = {}
    capability_id = "guardian.research-watch.v1"
    async with async_db() as db:
        await _goal(db)
        parent = await _task(
            db,
            task_id="handoff-input-parent",
            status=WorkBoardStatus.done,
            requires_review=False,
            body="private source body credential=never-forward",
        )
        child = await _task(
            db,
            task_id="handoff-input-child",
            status=WorkBoardStatus.ready,
            requires_review=False,
            capability_id=capability_id,
            executor_id=registered_executor_id(capability_id),
        )
        parent_attempt = await _verified_execution(
            db,
            parent,
            attempt_id="handoff-input-parent-attempt",
            run_id="handoff-input-parent-run",
        )
        link = WorkBoardLink(
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            parent_task_id=parent.task_id,
            child_task_id=child.task_id,
        )
        db.add(link)
        await db.flush()
        await review_service.materialize_handoff_for_link(
            db,
            OWNER,
            parent,
            child,
            link,
        )
        repository = WorkBoardRepository()
        claim = await repository.claim_ready_task(
            db,
            child.task_id,
            expected_revision=child.task_revision,
            lease_owner="dispatcher:test",
            actor_principal_id="dispatcher:test",
            actor_session_id="session:dispatcher",
        )
        assert claim is not None
        captured = json.loads(claim.attempt.parent_handoff_context_json)
        assert captured == list(claim.parent_handoffs)
        assert captured[0]["handoff_id"] == link.current_handoff_id
        assert captured[0]["source_attempt_id"] == parent_attempt.attempt_id
        assert "private source body" not in json.dumps(captured)
        assert "credential=never-forward" not in json.dumps(captured)
        assert claim.attempt.parent_handoff_digest == hashlib.sha256(
            json.dumps(captured, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    async def capture_run_watch(_watch_id, **kwargs):
        awaitable_context.update(kwargs)
        return {"status": "accepted", "job_id": "source-watch:test"}

    from src.guardian.source_watch import source_watch_service

    monkeypatch.setattr(source_watch_service, "run_watch", capture_run_watch)
    result = await WorkBoardDispatcher(session_provider=async_db)._execute_direct_adapter(
        claim.task,
        claim.attempt,
        {"watch_id": "watch:test", "expected_plan_revision": 1},
        runtime_seconds=300,
        admission_only=True,
    )
    assert result["status"] == "accepted"
    assert awaitable_context["work_board_task_id"] == child.task_id
    assert awaitable_context["work_board_parent_handoff_context"] == captured
    assert awaitable_context["work_board_parent_handoff_digest"] == claim.attempt.parent_handoff_digest


def test_child_attempt_rejects_parent_handoff_without_immutable_source_attempt():
    attempt = WorkBoardAttempt(
        task_id="child-task",
        parent_handoff_context_json=json.dumps(
            [
                {
                    "handoff_id": "handoff-1",
                    "schema_version": "work_board_handoff.v1",
                    "parent_task_id": "parent-task",
                    "child_task_id": "child-task",
                    "status": "verified",
                    "summary": "bounded",
                    "artifact_refs": [],
                    "result_refs": [],
                    "verification_receipt": {"status": "verified"},
                    "source_task_revision": 1,
                    "risks": [],
                }
            ]
        ),
    )
    context = json.loads(attempt.parent_handoff_context_json)
    attempt.parent_handoff_digest = hashlib.sha256(
        json.dumps(context, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    with pytest.raises(TypedInputError, match="binding is incomplete"):
        WorkBoardDispatcher._attempt_parent_handoffs(attempt)


@pytest.mark.asyncio
async def test_unverified_done_parent_cannot_be_linked(async_db):
    repository = WorkBoardRepository()
    async with async_db() as db:
        await _goal(db)
        parent = await _task(
            db,
            task_id="unverified-parent",
            status=WorkBoardStatus.done,
            requires_review=False,
        )
        child = await _task(
            db,
            task_id="unverified-child",
            status=WorkBoardStatus.todo,
            requires_review=False,
        )
        parent_id = parent.task_id
        child_id = child.task_id
        with pytest.raises(BoardError) as blocked:
            await repository.add_link(
                db,
                OWNER,
                WorkBoardLinkCreate(
                    parent_task_id=parent.task_id,
                    child_task_id=child.task_id,
                    expected_child_revision=child.task_revision,
                ),
            )
        assert blocked.value.code == "handoff_materialization_required"
        await db.rollback()
        assert (
            await db.scalar(
                select(WorkBoardLink.link_id).where(
                    WorkBoardLink.parent_task_id == parent_id,
                    WorkBoardLink.child_task_id == child_id,
                )
            )
            is None
        )


@pytest.mark.asyncio
async def test_missing_openrouter_route_blocks_without_provider_call(async_db, monkeypatch):
    async with async_db() as db:
        task = await _task(
            db,
            task_id="triage-no-provider",
            status=WorkBoardStatus.triage,
            requires_review=False,
        )

    def no_route():
        raise BoardError("openrouter_route_unavailable", "route unavailable", status_code=409)

    provider_call = AsyncMock()
    admit_call = AsyncMock()
    monkeypatch.setattr(triage_service, "_route_binding", no_route)
    monkeypatch.setattr(triage_service, "_invoke_governed_proposal", provider_call)
    monkeypatch.setattr(triage_service, "_admit_proposal_job", admit_call)

    result = await triage_service.create_proposal(
        OWNER,
        task.task_id,
        kind="specify",
        request=WorkBoardProposalRequest(expected_revision=task.task_revision, idempotency_key="no-provider"),
        operator=SimpleNamespace(),
    )
    assert result["status"] == "blocked"
    assert result["blocked_reason"] == "openrouter_route_unavailable"
    provider_call.assert_not_awaited()
    admit_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_openrouter_policy_lookup_failure_returns_blocked_proposal(async_db, monkeypatch):
    async with async_db() as db:
        task = await _task(
            db,
            task_id="triage-policy-unavailable",
            status=WorkBoardStatus.triage,
            requires_review=False,
        )

    def unavailable(_route_id):
        raise RuntimeError("policy store unavailable")

    provider_call = AsyncMock()
    admit_call = AsyncMock()
    monkeypatch.setattr(triage_service, "effective_workload_policy", unavailable)
    monkeypatch.setattr(triage_service, "_invoke_governed_proposal", provider_call)
    monkeypatch.setattr(triage_service, "_admit_proposal_job", admit_call)

    result = await triage_service.create_proposal(
        OWNER,
        task.task_id,
        kind="specify",
        request=WorkBoardProposalRequest(
            expected_revision=task.task_revision,
            idempotency_key="policy-unavailable",
        ),
        operator=SimpleNamespace(),
    )

    assert result["status"] == "blocked"
    assert result["blocked_reason"] == "openrouter_policy_unavailable"
    provider_call.assert_not_awaited()
    admit_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_started_duplicate_proposal_reconciles_without_second_job_or_provider(
    async_db,
    monkeypatch,
):
    async with async_db() as db:
        task = await _task(
            db,
            task_id="triage-unknown-provider",
            status=WorkBoardStatus.triage,
            requires_review=False,
        )
        route_id = "strategist_agent"
        capability_version = "1"
        proposal = WorkBoardProposal(
            proposal_id="proposal-unknown-provider",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            parent_task_id=task.task_id,
            parent_revision=task.task_revision,
            goal_revision=task.goal_revision,
            kind="specify",
            idempotency_key="unknown-provider",
            request_digest=triage_service._proposal_request_digest(
                task=task,
                kind="specify",
                idempotency_key="unknown-provider",
            ),
            capability_id=triage_service._PROPOSAL_CAPABILITY,
            capability_version=capability_version,
            authority_digest=triage_service._authority_digest(
                OWNER,
                task,
                route_id,
                capability_version,
            ),
            grant_revision=task.goal_revision,
            input_digest=triage_service._proposal_input_digest(task=task, kind="specify"),
            route_id=route_id,
            admission_job_id="work-board-proposal:proposal-unknown-provider",
            effect_id_digest="a" * 16,
            provider_contact_started=True,
            provider_contact_state="started",
            status="pending_inference",
            proposal_json=json.dumps({"proposed_tasks": [], "proposed_links": []}),
            expires_at=_now() + timedelta(minutes=10),
        )
        db.add(proposal)
        await db.flush()

    monkeypatch.setattr(triage_service, "_route_binding", lambda: ("strategist_agent", "1"))
    get_job = AsyncMock(return_value={"status": "failed"})
    admit_call = AsyncMock()
    provider_call = AsyncMock()
    monkeypatch.setattr(triage_service.durable_job_repository, "get_job", get_job)
    monkeypatch.setattr(triage_service, "_admit_proposal_job", admit_call)
    monkeypatch.setattr(triage_service, "_invoke_governed_proposal", provider_call)

    result = await triage_service.create_proposal(
        OWNER,
        task.task_id,
        kind="specify",
        request=WorkBoardProposalRequest(
            expected_revision=task.task_revision,
            idempotency_key="unknown-provider",
        ),
        operator=SimpleNamespace(),
    )
    assert result["status"] == "blocked"
    assert result["blocked_reason"] == "proposal_provider_contact_reconciliation_required"
    get_job.assert_awaited_once_with("work-board-proposal:proposal-unknown-provider")
    admit_call.assert_not_awaited()
    provider_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_proposal_accept_is_revision_goal_and_owner_fenced(async_db):
    async with async_db() as db:
        await _goal(db)
        task = await _task(
            db,
            task_id="triage-accept-fence",
            status=WorkBoardStatus.triage,
            requires_review=False,
        )
        capability = "goal.snapshot.v1"
        capability_version = "1"
        child_input_digest = hashlib.sha256(b"child-input").hexdigest()
        proposal = WorkBoardProposal(
            proposal_id="proposal-accept-fence",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            parent_task_id=task.task_id,
            parent_revision=task.task_revision,
            goal_revision=task.goal_revision,
            kind="specify",
            idempotency_key="accept-fence",
            request_digest="request-digest",
            capability_id=triage_service._PROPOSAL_CAPABILITY,
            capability_version=capability_version,
            authority_digest=triage_service._authority_digest(
                OWNER,
                task,
                triage_service._PROPOSAL_ROUTE,
                capability_version,
            ),
            grant_revision=task.goal_revision,
            input_digest=triage_service._proposal_input_digest(task=task, kind="specify"),
            route_id=triage_service._PROPOSAL_ROUTE,
            admission_job_id="work-board-proposal:proposal-accept-fence",
            effect_id_digest="b" * 16,
            status="proposed",
            proposal_json=json.dumps(
                {
                    "proposed_tasks": [
                        {
                            "task_id": "proposal-child-1",
                            "title": "Bounded child",
                            "body": "Child from accepted proposal",
                            "capability_id": capability,
                            "capability_version": capability_version,
                            "typed_input_ref": "workspace-json:inputs/child.json",
                            "typed_input_digest": child_input_digest,
                            "executor_id": "executor:m4",
                        }
                    ],
                    "proposed_links": [
                        {
                            "parent_task_id": task.task_id,
                            "child_task_id": "proposal-child-1",
                        }
                    ],
                }
            ),
            expires_at=_now() + timedelta(minutes=10),
        )
        db.add(proposal)
        await db.flush()
        proposal_revision = proposal.revision
        parent_revision = task.task_revision

    with pytest.raises(BoardRevisionConflict):
        await triage_service.accept_proposal(
            OWNER,
            "proposal-accept-fence",
            WorkBoardProposalAccept(
                expected_proposal_revision=proposal_revision,
                expected_parent_revision=parent_revision + 1,
            ),
        )

    with pytest.raises(BoardError) as foreign:
        await triage_service.accept_proposal(
            OTHER_OWNER,
            "proposal-accept-fence",
            WorkBoardProposalAccept(
                expected_proposal_revision=proposal_revision,
                expected_parent_revision=parent_revision,
            ),
        )
    assert foreign.value.code == "proposal_not_found"

    async with async_db() as db:
        goal = await db.get(Goal, "goal-m4")
        goal.revision = 2
        await db.flush()

    with pytest.raises(BoardError) as stale_goal:
        await triage_service.accept_proposal(
            OWNER,
            "proposal-accept-fence",
            WorkBoardProposalAccept(
                expected_proposal_revision=proposal_revision,
                expected_parent_revision=parent_revision,
            ),
        )
    assert stale_goal.value.code == "goal_revision_stale"


@pytest.mark.asyncio
async def test_unknown_external_effect_never_retries(async_db):
    repository = WorkBoardRepository()
    async with async_db() as db:
        await _goal(db)
        task = await _task(
            db,
            task_id="unknown-effect-retry",
            status=WorkBoardStatus.blocked,
            requires_review=False,
        )
        task.block_kind = "unknown_effect"
        task.block_reason = "External write outcome is unknown."
        task.block_source_status = WorkBoardStatus.running.value
        await db.flush()

        with pytest.raises(BoardError) as retry:
            await repository.retry_task(
                db,
                OWNER,
                task.task_id,
                expected_revision=task.task_revision,
            )
        assert retry.value.code == "typed_reconcile_required"
        assert task.status is WorkBoardStatus.blocked


@pytest.mark.asyncio
async def test_legacy_review_expiry_backfill_is_one_time(tmp_path):
    database_path = tmp_path / "legacy-review.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "CREATE TABLE work_board_tasks ("
                "task_id TEXT PRIMARY KEY, status TEXT NOT NULL, review_expires_at DATETIME)"
            )
            await conn.exec_driver_sql(
                "INSERT INTO work_board_tasks(task_id, status, review_expires_at) "
                "VALUES ('legacy-review', 'review', NULL)"
            )
            await db_engine._ensure_work_board_columns(conn)
            first = (
                await conn.exec_driver_sql(
                    "SELECT review_expires_at FROM work_board_tasks WHERE task_id = 'legacy-review'"
                )
            ).scalar_one()
        async with engine.begin() as conn:
            await db_engine._ensure_work_board_columns(conn)
            second = (
                await conn.exec_driver_sql(
                    "SELECT review_expires_at FROM work_board_tasks WHERE task_id = 'legacy-review'"
                )
            ).scalar_one()
    finally:
        await engine.dispose()

    assert first is not None
    assert second == first
