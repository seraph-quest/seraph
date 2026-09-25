"""Focused provider-free proofs for M4 review and handoff boundaries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from config.settings import settings
from src.db.models import (
    Goal,
    Session,
    WorkBoardAttempt,
    WorkBoardEvent,
    WorkBoardHandoff,
    WorkBoardLink,
    WorkBoardReviewIntent,
    WorkBoardStatus,
    WorkBoardTask,
    WorkflowRunState,
)
from src.work_board import review as review_service
from src.work_board.contracts import WorkBoardAction, WorkBoardActionRequest, WorkBoardOwner
from src.work_board.dispatcher import (
    GOAL_SNAPSHOT_CAPABILITY,
    WorkBoardDispatcher,
    _safe_digest,
    registered_executor_id,
)
from src.work_board.repository import BoardError, WorkBoardRepository
from src.work_board.tools import WorkBoardWorkerTools


OWNER = WorkBoardOwner(principal_id="operator:review-hardening", session_id="session:review-hardening")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


async def _goal(db, goal_id: str = "goal-review-hardening") -> Goal:
    goal = Goal(
        id=goal_id,
        title="Review hardening goal",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        revision=1,
        status="active",
    )
    db.add(goal)
    await db.flush()
    return goal


@pytest.mark.asyncio
async def test_dispatcher_service_root_can_complete_review_and_materialize_handoff(
    async_db,
    monkeypatch,
    tmp_path,
):
    """The real Work Board service root remains review/handoff authority-bound."""

    input_path = tmp_path / "inputs" / "review.json"
    input_path.parent.mkdir(parents=True)
    raw = json.dumps(
        {
            "schema_version": 1,
            "capability_id": GOAL_SNAPSHOT_CAPABILITY,
            "input": {"file_path": "artifacts/review.md"},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    input_path.write_bytes(raw)
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))

    async with async_db() as db:
        goal = await _goal(db, "goal-dispatcher-service-root")
        parent = WorkBoardTask(
            task_id="dispatcher-service-parent",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            origin_session_id=OWNER.session_id,
            goal_id=goal.id,
            goal_revision=1,
            title="Dispatcher parent",
            idempotency_key="dispatcher-service-parent-key",
            status=WorkBoardStatus.review,
            requires_review=True,
            reviewer_id=OWNER.principal_id,
            capability_id=GOAL_SNAPSHOT_CAPABILITY,
            typed_input_ref="workspace-json:inputs/review.json",
            typed_input_digest=hashlib.sha256(raw).hexdigest(),
            executor_id=registered_executor_id(GOAL_SNAPSHOT_CAPABILITY),
            task_revision=1,
        )
        child = WorkBoardTask(
            task_id="dispatcher-service-child",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            origin_session_id=OWNER.session_id,
            goal_id=goal.id,
            goal_revision=1,
            title="Dispatcher child",
            idempotency_key="dispatcher-service-child-key",
            status=WorkBoardStatus.todo,
            task_revision=1,
        )
        link = WorkBoardLink(
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            parent_task_id=parent.task_id,
            child_task_id=child.task_id,
        )
        attempt = WorkBoardAttempt(
            attempt_id="dispatcher-service-attempt",
            task_id=parent.task_id,
            workflow_run_id=f"work-board:{parent.task_id}:dispatcher-service-attempt",
            task_revision_at_claim=1,
            fencing_token=9,
            executor_id=parent.executor_id or "",
            started_at=_now() - timedelta(minutes=1),
            ended_at=_now(),
            outcome="succeeded",
        )
        db.add_all([parent, child])
        await db.flush()
        db.add_all([link, attempt])
        await db.flush()
        dispatcher = WorkBoardDispatcher(session_provider=async_db)
        spec, _inputs, expected_job_id, _owner, _runtime = dispatcher._build_spec(parent, attempt)
        # Use the exact service-owned durable shape emitted by _build_spec.
        # The fixture's StaticPool deliberately avoids opening a second DB
        # session here; admission identity is still produced by the real
        # dispatcher root builder.
        db.add(Session(id=OWNER.session_id, owner_principal_id=OWNER.principal_id))
        db.add(
            WorkflowRunState(
                run_identity=expected_job_id,
                root_run_identity=expected_job_id,
                workflow_name=spec.identity.job_kind,
                tool_name=spec.identity.job_kind,
                session_id=spec.session_id,
                conversation_id=spec.conversation_id,
                operator_session_id=spec.operator_session_id,
                status="succeeded",
                run_fingerprint=spec.run_fingerprint or _safe_digest(spec.inputs),
                arguments_json=json.dumps(
                    {
                        "redacted": True,
                        "keys": sorted(spec.inputs),
                        "shape": "dict",
                    },
                    sort_keys=True,
                ),
                record_schema_version=2,
                job_kind=spec.identity.job_kind,
                owner_kind=spec.identity.owner_kind,
                owner_principal_id=spec.identity.owner_principal_id,
                service_id=spec.service_id,
                goal_id=spec.goal_id,
                goal_revision=spec.goal_revision,
                capability_version=spec.identity.capability_version,
                input_digest=_safe_digest(spec.inputs),
                authority_digest=_safe_digest(spec.declared_authority),
                idempotency_scope=spec.identity.idempotency_scope,
                idempotency_key=spec.identity.idempotency_key,
                idempotency_binding="dispatcher-service-root-test-binding",
                declared_authority_json=json.dumps(spec.declared_authority, sort_keys=True),
                max_attempts=spec.max_attempts,
            )
        )
        digest = _digest("dispatcher-service-output")
        attempt.receipt_refs_json = json.dumps(
            [
                {
                    "receipt_kind": "readback",
                    "workflow_run_id": expected_job_id,
                    "status": "succeeded",
                    "verified": True,
                    "readback_id": "dispatcher-service-readback",
                    "content_sha256": digest,
                    "verified_at": "2026-09-25T00:00:00+00:00",
                }
            ]
        )
        intent = WorkBoardReviewIntent(
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            task_id=parent.task_id,
            attempt_id=attempt.attempt_id,
            workflow_run_id=expected_job_id,
            fencing_token=attempt.fencing_token,
            task_revision=parent.task_revision,
            request_digest="dispatcher-service-review-intent",
            evidence_refs_json='["dispatcher-service-readback"]',
            status="projected",
        )
        parent.review_request_attempt_id = attempt.attempt_id
        parent.review_request_fence = attempt.fencing_token
        parent.review_request_revision = parent.task_revision
        parent.review_request_digest = intent.request_digest
        parent.review_request_evidence_json = intent.evidence_refs_json
        db.add(intent)
        await db.flush()

        mutation = await review_service.complete_review(
            db,
            OWNER,
            parent.task_id,
            expected_revision=parent.task_revision,
            attempt_id=attempt.attempt_id,
        )
        assert mutation.task.status is WorkBoardStatus.done
        refreshed_link = (
            await db.execute(
                select(WorkBoardLink).where(WorkBoardLink.link_id == link.link_id)
            )
        ).scalar_one()
        assert refreshed_link.current_handoff_id
        handoff = (
            await db.execute(
                select(WorkBoardHandoff).where(
                    WorkBoardHandoff.handoff_id == refreshed_link.current_handoff_id,
                )
            )
        ).scalar_one()
        assert handoff.workflow_run_id == expected_job_id
        assert await review_service.current_handoff_is_verified(
            db,
            OWNER,
            parent,
            child,
            refreshed_link,
        )


def _proof(run_id: str, label: str, *, readback_id: str = "readback-1") -> dict[str, object]:
    return {
        "workflow_run_id": run_id,
        "receipt_kind": "readback",
        "status": "succeeded",
        "verified": True,
        "readback_id": readback_id,
        "content_sha256": _digest(label),
        "verified_at": "2026-09-25T00:00:00+00:00",
    }


def test_workflow_readback_requires_typed_run_bound_proof():
    digest = _digest("goal-snapshot-output")
    receipt = {
        "receipt_kind": "readback",
        "effect_type": "workflow_invocation",
        "status": "succeeded",
        "effect_id": "workspace-write-1",
        "readback_id": "artifact-goal-snapshot",
        "verified_at": "2026-09-25T00:00:00+00:00",
        "content_sha256": digest,
        "details": {"verified": True},
    }
    proof = WorkBoardDispatcher._workflow_readback(
        {"status": "succeeded", "run_identity": "run-review-proof", "effects": [receipt]},
        "run-review-proof",
    )
    assert proof is not None
    assert proof["receipt_kind"] == "readback"
    assert proof["workflow_run_id"] == "run-review-proof"
    assert proof["content_sha256"] == digest
    assert proof["readback_id"] == "artifact-goal-snapshot"

    # The real producer may retain the digest in the target/details fields;
    # those are accepted only on an explicitly typed readback receipt.
    details_digest = {
        **receipt,
        "content_sha256": None,
        "target_digest": digest,
    }
    assert WorkBoardDispatcher._workflow_readback(
        {"run_identity": "run-review-proof", "effects": [details_digest]},
        "run-review-proof",
    )["content_sha256"] == digest

    generic_verified = {**receipt, "receipt_kind": "effect"}
    assert WorkBoardDispatcher._workflow_readback(
        {"run_identity": "run-review-proof", "effects": [generic_verified]},
        "run-review-proof",
    ) is None
    generic_details = {**receipt, "receipt_kind": None, "details": {"verified": True}}
    assert WorkBoardDispatcher._workflow_readback(
        {"run_identity": "run-review-proof", "effects": [generic_details]},
        "run-review-proof",
    ) is None
    generic_result = {"verified": True, "digest": digest, "readback_id": "result-proof"}
    assert WorkBoardDispatcher._workflow_readback(
        {"run_identity": "run-review-proof", "result": generic_result},
        "run-review-proof",
    ) is None
    wrong_run = {**receipt, "workflow_run_id": "other-run"}
    assert WorkBoardDispatcher._workflow_readback(
        {"run_identity": "run-review-proof", "effects": [{**wrong_run, "workflow_run_id": "other-run"}]},
        "run-review-proof",
    ) is None
    bad_digest = {**receipt, "content_sha256": "not-a-sha256"}
    assert WorkBoardDispatcher._workflow_readback(
        {"run_identity": "run-review-proof", "effects": [bad_digest]},
        "run-review-proof",
    ) is None
    unsafe_id = {**receipt, "readback_id": "../private/secret"}
    assert WorkBoardDispatcher._workflow_readback(
        {"run_identity": "run-review-proof", "effects": [unsafe_id]},
        "run-review-proof",
    ) is None
    missing_time = {**receipt, "verified_at": None}
    assert WorkBoardDispatcher._workflow_readback(
        {"run_identity": "run-review-proof", "effects": [missing_time]},
        "run-review-proof",
    ) is None
    assert WorkBoardDispatcher._workflow_readback(
        {"effects": [receipt]},
        "run-review-proof",
    ) is None
    assert WorkBoardDispatcher._workflow_readback(
        {"run_identity": "other-run", "effects": [receipt]},
        "run-review-proof",
    ) is None


def test_request_review_contract_allows_active_intent_without_evidence():
    request = WorkBoardActionRequest(
        action=WorkBoardAction.request_review,
        expected_revision=1,
        attempt_id="active-attempt",
    )
    assert request.evidence_refs == []


@pytest.mark.asyncio
async def test_running_worker_request_binds_owner_review_intent(async_db):
    async with async_db() as db:
        goal = await _goal(db)
        task = WorkBoardTask(
            task_id="ordinary-running-review",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            origin_session_id=OWNER.session_id,
            goal_id=goal.id,
            goal_revision=1,
            title="Ordinary running task",
            idempotency_key="ordinary-running-review-key",
            status=WorkBoardStatus.running,
            requires_review=False,
            reviewer_id=None,
            task_revision=1,
        )
        run = WorkflowRunState(
            run_identity="ordinary-running-review-run",
            root_run_identity="ordinary-running-review-run",
            workflow_name="review-hardening",
            operator_session_id=OWNER.session_id,
            status="running",
            owner_kind="user",
            owner_principal_id=OWNER.principal_id,
            goal_id=goal.id,
            goal_revision=1,
        )
        attempt = WorkBoardAttempt(
            attempt_id="ordinary-running-review-attempt",
            task_id=task.task_id,
            workflow_run_id=run.run_identity,
            task_revision_at_claim=1,
            lease_owner="executor:deterministic",
            lease_expires_at=_now() + timedelta(minutes=5),
            heartbeat_at=_now(),
            fencing_token=4,
            executor_id="executor:deterministic",
            started_at=_now(),
            receipt_refs_json=json.dumps([{"artifact_id": "artifact-review-intent"}]),
        )
        db.add(task)
        await db.flush()
        db.add_all([run, attempt])
        await db.flush()
        task_id = task.task_id
        attempt_id = attempt.attempt_id
        run_id = run.run_identity

        with pytest.raises(BoardError) as generic_evidence:
            await review_service.request_review(
                db,
                OWNER,
                task_id,
                expected_revision=1,
                attempt_id=attempt_id,
                evidence_refs=["artifact-review-intent"],
            )
        assert generic_evidence.value.code == "evidence_ref_unavailable"
        await db.rollback()

        mutation = await review_service.request_review(
            db,
            OWNER,
            task_id,
            expected_revision=1,
            attempt_id=attempt_id,
            evidence_refs=[],
        )
        assert mutation.task.status is WorkBoardStatus.running
        assert mutation.task.requires_review is True
        assert mutation.task.reviewer_id == OWNER.principal_id
        intent = (
            await db.execute(
                select(WorkBoardReviewIntent).where(
                    WorkBoardReviewIntent.task_id == task_id,
                    WorkBoardReviewIntent.attempt_id == attempt_id,
                )
            )
        ).scalar_one()
        assert intent.owner_principal_id == OWNER.principal_id
        assert intent.owner_session_id == OWNER.session_id
        assert intent.status == "pending"
        assert intent.workflow_run_id == run_id
        assert intent.evidence_refs_json == "[]"
        fresh_task = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
        ).scalar_one()
        fresh_attempt = (
            await db.execute(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == attempt_id))
        ).scalar_one()
        assert await review_service._verified_workflow_readback(db, fresh_task, fresh_attempt) is None

        # A generic effect/result flag is execution output, not a terminal
        # readback.  The active intent must remain Running until the durable
        # projection supplies the typed receipt later.
        generic_projection = {
            "run_identity": run_id,
            "status": "succeeded",
            "result": {"verified": True, "readback_id": "invented-proof"},
            "effects": [{"status": "succeeded", "details": {"verified": True}}],
        }
        assert WorkBoardDispatcher._workflow_readback(generic_projection, run_id) is None


@pytest.mark.asyncio
async def test_post_link_verdict_uses_persisted_intent_revision(async_db):
    """A coordination revision after claim must not invalidate a valid intent."""
    async with async_db() as db:
        goal = await _goal(db, "goal-review-intent-revision")
        task = WorkBoardTask(
            task_id="review-intent-revision-task",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            origin_session_id=OWNER.session_id,
            goal_id=goal.id,
            goal_revision=1,
            title="Review intent revision",
            idempotency_key="review-intent-revision-key",
            status=WorkBoardStatus.running,
            task_revision=2,
            requires_review=False,
            reviewer_id=None,
        )
        run_id = "review-intent-revision-run"
        run = WorkflowRunState(
            run_identity=run_id,
            root_run_identity=run_id,
            workflow_name="review-hardening",
            operator_session_id=OWNER.session_id,
            status="running",
            owner_kind="user",
            owner_principal_id=OWNER.principal_id,
            goal_id=goal.id,
            goal_revision=1,
        )
        attempt = WorkBoardAttempt(
            attempt_id="review-intent-revision-attempt",
            task_id=task.task_id,
            workflow_run_id=run_id,
            task_revision_at_claim=1,
            lease_owner="executor:deterministic",
            lease_expires_at=_now() + timedelta(minutes=5),
            heartbeat_at=_now(),
            fencing_token=6,
            executor_id="executor:deterministic",
            started_at=_now(),
            receipt_refs_json="[]",
        )
        db.add(task)
        await db.flush()
        db.add_all([run, attempt])
        await db.flush()

        requested = await review_service.request_review(
            db,
            OWNER,
            task.task_id,
            expected_revision=2,
            attempt_id=attempt.attempt_id,
            evidence_refs=[],
        )
        assert requested.task.status is WorkBoardStatus.running
        assert requested.task.review_request_revision == 2

    # The durable run links and settles independently.  Close the request
    # transaction before the dispatcher opens its own writer transaction.
    async with async_db() as db:
        run = (
            await db.execute(
                select(WorkflowRunState).where(WorkflowRunState.run_identity == run_id)
            )
        ).scalar_one()
        attempt = (
            await db.execute(
                select(WorkBoardAttempt).where(
                    WorkBoardAttempt.attempt_id == "review-intent-revision-attempt"
                )
            )
        ).scalar_one()
        run.status = "succeeded"
        attempt.receipt_refs_json = json.dumps([_proof(run_id, "revision-output")])
        await db.flush()

    dispatcher = WorkBoardDispatcher(
        repository=WorkBoardRepository(),
        jobs=object(),
        session_provider=async_db,
    )
    projection_task = WorkBoardTask(task_id="review-intent-revision-task", task_revision=2)
    projection_attempt = WorkBoardAttempt(
        attempt_id="review-intent-revision-attempt",
        task_id=projection_task.task_id,
        fencing_token=6,
        lease_owner="executor:deterministic",
    )
    projection_proof = {"source": "workflow_run", **_proof(run_id, "revision-output")}
    await dispatcher._project(
        projection_task,
        projection_attempt,
        board_revision=2,
        status=WorkBoardStatus.review,
        outcome="verified",
        proof=projection_proof,
        lease_owner=projection_attempt.lease_owner,
    )

    async with async_db() as review_db:
        projected = (
            await review_db.execute(
                select(WorkBoardTask).where(WorkBoardTask.task_id == "review-intent-revision-task")
            )
        ).scalar_one()
        assert projected.status is WorkBoardStatus.review
        assert projected.task_revision == 3
        verdict = await review_service.complete_review(
            review_db,
            OWNER,
            projected.task_id,
            expected_revision=projected.task_revision,
            attempt_id="review-intent-revision-attempt",
        )
        assert verdict.task.status is WorkBoardStatus.done


@pytest.mark.asyncio
async def test_done_projection_promotes_exact_pending_review_intent(async_db):
    """A worker review request wins over a stale ordinary-task Done snapshot."""

    task_id = "ordinary-done-review-race"
    attempt_id = "ordinary-done-review-attempt"
    run_id = "ordinary-done-review-run"
    async with async_db() as db:
        goal = await _goal(db, "goal-ordinary-done-review-race")
        task = WorkBoardTask(
            task_id=task_id,
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            origin_session_id=OWNER.session_id,
            goal_id=goal.id,
            goal_revision=1,
            title="Ordinary task with dynamic review",
            idempotency_key="ordinary-done-review-race-key",
            status=WorkBoardStatus.running,
            requires_review=False,
            task_revision=1,
        )
        run = WorkflowRunState(
            run_identity=run_id,
            root_run_identity=run_id,
            workflow_name="review-hardening",
            operator_session_id=OWNER.session_id,
            status="running",
            owner_kind="user",
            owner_principal_id=OWNER.principal_id,
            goal_id=goal.id,
            goal_revision=1,
        )
        attempt = WorkBoardAttempt(
            attempt_id=attempt_id,
            task_id=task_id,
            workflow_run_id=run_id,
            task_revision_at_claim=1,
            lease_owner="executor:deterministic",
            lease_expires_at=_now() + timedelta(minutes=5),
            heartbeat_at=_now(),
            fencing_token=12,
            executor_id="executor:deterministic",
            started_at=_now(),
            receipt_refs_json="[]",
        )
        db.add(task)
        await db.flush()
        db.add_all([run, attempt])
        await db.flush()
        requested = await review_service.request_review(
            db,
            OWNER,
            task_id,
            expected_revision=1,
            attempt_id=attempt_id,
            evidence_refs=[],
        )
        assert requested.task.requires_review is True

    async with async_db() as db:
        run = (
            await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == run_id))
        ).scalar_one()
        attempt = (
            await db.execute(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == attempt_id))
        ).scalar_one()
        run.status = "succeeded"
        attempt.receipt_refs_json = json.dumps([_proof(run_id, "ordinary-done-review-output")])
        await db.commit()

    dispatcher = WorkBoardDispatcher(
        repository=WorkBoardRepository(),
        jobs=object(),
        session_provider=async_db,
    )
    # This is the ordinary dispatcher snapshot captured before the worker
    # request.  It deliberately remains requires_review=False.
    stale_task = SimpleNamespace(
        task_id=task_id,
        task_revision=1,
        requires_review=False,
    )
    stale_attempt = SimpleNamespace(
        attempt_id=attempt_id,
        fencing_token=12,
        lease_owner="executor:deterministic",
    )
    await dispatcher._project(
        stale_task,
        stale_attempt,
        board_revision=1,
        status=WorkBoardStatus.done,
        outcome="verified",
        proof={"source": "workflow_run", **_proof(run_id, "ordinary-done-review-output")},
        lease_owner=stale_attempt.lease_owner,
    )

    async with async_db() as db:
        projected = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
        ).scalar_one()
        intent = (
            await db.execute(
                select(WorkBoardReviewIntent).where(
                    WorkBoardReviewIntent.task_id == task_id,
                    WorkBoardReviewIntent.attempt_id == attempt_id,
                )
            )
        ).scalar_one()
        assert projected.status is WorkBoardStatus.review
        assert projected.requires_review is True
        assert projected.task_revision == 2
        assert intent.workflow_run_id == run_id
        assert intent.fencing_token == 12
        assert intent.task_revision == 1
        assert intent.status == "projected"


@pytest.mark.asyncio
async def test_wrong_fence_review_intent_cannot_promote_done_projection(async_db):
    """A pending intent from another fence cannot alter the terminal verdict."""

    task_id = "wrong-fence-review-intent"
    attempt_id = "wrong-fence-review-attempt"
    run_id = "wrong-fence-review-run"
    async with async_db() as db:
        goal = await _goal(db, "goal-wrong-fence-review-intent")
        task = WorkBoardTask(
            task_id=task_id,
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            origin_session_id=OWNER.session_id,
            goal_id=goal.id,
            goal_revision=1,
            title="Wrong fence review intent",
            idempotency_key=f"{task_id}-key",
            status=WorkBoardStatus.running,
            requires_review=False,
            task_revision=1,
        )
        run = WorkflowRunState(
            run_identity=run_id,
            root_run_identity=run_id,
            workflow_name="review-hardening",
            operator_session_id=OWNER.session_id,
            status="succeeded",
            owner_kind="user",
            owner_principal_id=OWNER.principal_id,
            goal_id=goal.id,
            goal_revision=1,
        )
        attempt = WorkBoardAttempt(
            attempt_id=attempt_id,
            task_id=task_id,
            workflow_run_id=run_id,
            task_revision_at_claim=1,
            lease_owner="executor:deterministic",
            fencing_token=15,
            executor_id="executor:deterministic",
            started_at=_now(),
        )
        intent = WorkBoardReviewIntent(
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            task_id=task_id,
            attempt_id=attempt_id,
            workflow_run_id=run_id,
            fencing_token=99,
            task_revision=1,
            request_digest="wrong-fence-digest",
            status="pending",
        )
        db.add(task)
        await db.flush()
        db.add_all([run, attempt, intent])
        await db.flush()
        attempt.receipt_refs_json = json.dumps([_proof(run_id, "wrong-fence-output")])
        await db.commit()

    dispatcher = WorkBoardDispatcher(
        repository=WorkBoardRepository(),
        jobs=object(),
        session_provider=async_db,
    )
    await dispatcher._project(
        SimpleNamespace(task_id=task_id, task_revision=1, requires_review=False),
        SimpleNamespace(
            attempt_id=attempt_id,
            fencing_token=15,
            lease_owner="executor:deterministic",
        ),
        board_revision=1,
        status=WorkBoardStatus.done,
        outcome="verified",
        proof={"source": "workflow_run", **_proof(run_id, "wrong-fence-output")},
        lease_owner="executor:deterministic",
    )

    async with async_db() as db:
        projected = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
        ).scalar_one()
        stored_intent = (
            await db.execute(
                select(WorkBoardReviewIntent).where(
                    WorkBoardReviewIntent.task_id == task_id,
                    WorkBoardReviewIntent.attempt_id == attempt_id,
                )
            )
        ).scalar_one()
        assert projected.status is WorkBoardStatus.done
        assert projected.requires_review is False
        assert stored_intent.status == "superseded"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("block_kind", "reason", "expected_code"),
    [
        ("capability", "Missing capability grant", "typed_reconcile_required"),
        ("transient", "Provider is unavailable", "typed_reconcile_required"),
        ("needs_input", "Approval is required", "typed_reconcile_required"),
        ("cancelled", "Cancellation needs reconciliation", "typed_reconcile_required"),
        ("dependency", "A parent is still running", "typed_reconcile_required"),
        ("unknown_effect", "External effect is uncertain", "reconcile_external_effect"),
        ("review_expired", "The reviewer timed out", "review_renewal_required"),
    ],
)
async def test_generic_unblock_requires_typed_recovery(
    async_db,
    block_kind,
    reason,
    expected_code,
):
    task_id = f"typed-unblock-{block_kind}"
    async with async_db() as db:
        goal = await _goal(db, f"goal-typed-unblock-{block_kind}")
        task = WorkBoardTask(
            task_id=task_id,
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            origin_session_id=OWNER.session_id,
            goal_id=goal.id,
            goal_revision=1,
            title="Typed recovery test",
            idempotency_key=f"{task_id}-key",
            status=WorkBoardStatus.blocked,
            block_kind=block_kind,
            block_reason=reason,
            block_source_status=WorkBoardStatus.todo.value,
            task_revision=1,
        )
        db.add(task)
        await db.commit()
        with pytest.raises(BoardError) as raised:
            await review_service.unblock_task(
                db,
                OWNER,
                task_id,
                expected_revision=1,
                resolution="retry after operator inspection",
            )
        assert raised.value.code == expected_code


@pytest.mark.asyncio
async def test_generic_unblock_rejects_active_or_pending_attempt(async_db):
    task_id = "typed-unblock-active-attempt"
    async with async_db() as db:
        goal = await _goal(db, "goal-typed-unblock-active-attempt")
        task = WorkBoardTask(
            task_id=task_id,
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            origin_session_id=OWNER.session_id,
            goal_id=goal.id,
            goal_revision=1,
            title="Operator recovery with pending attempt",
            idempotency_key=f"{task_id}-key",
            status=WorkBoardStatus.blocked,
            block_kind="operator",
            block_reason="Operator recovery",
            block_source_status=WorkBoardStatus.todo.value,
            task_revision=1,
        )
        db.add(task)
        await db.flush()
        db.add(
            WorkBoardAttempt(
                attempt_id=f"{task_id}-attempt",
                task_id=task_id,
                workflow_run_id=None,
                task_revision_at_claim=1,
                lease_owner="executor:deterministic",
                fencing_token=1,
                executor_id="executor:deterministic",
                outcome="pending_admission",
            )
        )
        await db.commit()
        with pytest.raises(BoardError) as raised:
            await review_service.unblock_task(
                db,
                OWNER,
                task_id,
                expected_revision=1,
                resolution="restore the prior safe phase",
            )
        assert raised.value.code == "attempt_reconcile_required"


@pytest.mark.asyncio
async def test_operator_unblock_remains_supported_without_attempt(async_db):
    task_id = "operator-unblock-supported"
    async with async_db() as db:
        goal = await _goal(db, "goal-operator-unblock-supported")
        db.add(
            WorkBoardTask(
                task_id=task_id,
                owner_principal_id=OWNER.principal_id,
                owner_session_id=OWNER.session_id,
                origin_session_id=OWNER.session_id,
                goal_id=goal.id,
                goal_revision=1,
                title="Operator recovery",
                idempotency_key=f"{task_id}-key",
                status=WorkBoardStatus.blocked,
                block_kind="operator",
                block_reason="Operator recovery",
                block_source_status=WorkBoardStatus.todo.value,
                task_revision=1,
            )
        )
        await db.commit()
        mutation = await review_service.unblock_task(
            db,
            OWNER,
            task_id,
            expected_revision=1,
            resolution="restore the prior safe phase",
        )
        assert mutation.task.status is WorkBoardStatus.todo
        assert mutation.task.block_kind is None


@pytest.mark.asyncio
async def test_ready_unblock_recomputes_current_readiness_and_restores_ready(async_db, monkeypatch):
    """A formerly Ready card is restored only after live admission passes."""
    from src.work_board.dispatcher import _dispatcher

    calls: list[str] = []

    async def readiness(task):
        calls.append(task.task_id)
        return None, None

    monkeypatch.setattr(_dispatcher, "_readiness", readiness)
    task_id = "operator-unblock-ready-pass"
    async with async_db() as db:
        goal = await _goal(db, "goal-operator-unblock-ready-pass")
        task = WorkBoardTask(
            task_id=task_id,
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            origin_session_id=OWNER.session_id,
            goal_id=goal.id,
            goal_revision=1,
            title="Ready recovery pass",
            idempotency_key=f"{task_id}-key",
            status=WorkBoardStatus.blocked,
            block_kind="operator",
            block_reason="Operator recovery",
            block_source_status=WorkBoardStatus.ready.value,
            scheduled_at=_now() - timedelta(minutes=1),
            task_revision=7,
        )
        db.add(task)
        await db.commit()

        mutation = await review_service.unblock_task(
            db,
            OWNER,
            task_id,
            expected_revision=7,
            resolution="revalidated current readiness",
        )

        assert mutation.task.status is WorkBoardStatus.ready
        assert mutation.task.task_revision == 8
        assert mutation.task.block_kind is None
        assert calls == [task_id]


@pytest.mark.asyncio
async def test_ready_unblock_reopens_todo_when_current_readiness_fails(async_db, monkeypatch):
    """M4's fixed safe phase contract sends a failed Ready recovery to Todo."""
    from src.work_board.dispatcher import _dispatcher

    async def readiness(_task):
        return "external_mutation_grant_required", "The current grant is unavailable"

    monkeypatch.setattr(_dispatcher, "_readiness", readiness)
    task_id = "operator-unblock-ready-fail"
    async with async_db() as db:
        goal = await _goal(db, "goal-operator-unblock-ready-fail")
        task = WorkBoardTask(
            task_id=task_id,
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            origin_session_id=OWNER.session_id,
            goal_id=goal.id,
            goal_revision=1,
            title="Ready recovery fail",
            idempotency_key=f"{task_id}-key",
            status=WorkBoardStatus.blocked,
            block_kind="operator",
            block_reason="Operator recovery",
            block_source_status=WorkBoardStatus.ready.value,
            task_revision=11,
        )
        db.add(task)
        await db.commit()

        mutation = await review_service.unblock_task(
            db,
            OWNER,
            task_id,
            expected_revision=11,
            resolution="reopen for fresh dispatcher admission",
        )

        assert mutation.task.status is WorkBoardStatus.todo
        assert mutation.task.task_revision == 12
        assert mutation.task.block_kind is None
        event_metadata = json.loads(mutation.event.metadata_json)
        assert event_metadata["readiness_code"] == "external_mutation_grant_required"
        assert event_metadata["readiness_reason_digest"] == hashlib.sha256(
            "The current grant is unavailable".encode()
        ).hexdigest()[:16]


@pytest.mark.asyncio
async def test_owner_review_api_can_complete_even_when_executor_identifier_matches(async_db):
    async with async_db() as db:
        goal = await _goal(db, "goal-owner-review")
        run_id = "owner-review-run"
        task = WorkBoardTask(
            task_id="owner-review-task",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            origin_session_id=OWNER.session_id,
            goal_id=goal.id,
            goal_revision=1,
            title="Owner review",
            idempotency_key="owner-review-key",
            status=WorkBoardStatus.review,
            requires_review=True,
            reviewer_id=OWNER.principal_id,
            review_expires_at=_now() + timedelta(days=1),
            review_request_attempt_id="owner-review-attempt",
            review_request_fence=8,
            review_request_revision=1,
            review_request_digest="owner-review-digest",
            task_revision=1,
        )
        run = WorkflowRunState(
            run_identity=run_id,
            root_run_identity=run_id,
            workflow_name="review-hardening",
            operator_session_id=OWNER.session_id,
            status="succeeded",
            owner_kind="user",
            owner_principal_id=OWNER.principal_id,
            goal_id=goal.id,
            goal_revision=1,
        )
        attempt = WorkBoardAttempt(
            attempt_id="owner-review-attempt",
            task_id=task.task_id,
            workflow_run_id=run_id,
            task_revision_at_claim=1,
            lease_owner="executor:deterministic",
            fencing_token=8,
            executor_id=OWNER.principal_id,
            started_at=_now() - timedelta(seconds=10),
            ended_at=_now(),
            outcome="succeeded",
            receipt_refs_json=json.dumps([_proof(run_id, "owner-review-output", readback_id="owner-readback")]),
        )
        intent = WorkBoardReviewIntent(
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            task_id=task.task_id,
            attempt_id=attempt.attempt_id,
            workflow_run_id=run_id,
            fencing_token=8,
            task_revision=1,
            request_digest="owner-review-digest",
            evidence_refs_json='["owner-readback"]',
            status="projected",
        )
        db.add(task)
        await db.flush()
        db.add_all([run, attempt, intent])
        await db.flush()

        mutation = await review_service.complete_review(
            db,
            OWNER,
            task.task_id,
            expected_revision=1,
            attempt_id=attempt.attempt_id,
        )
        assert mutation.task.status is WorkBoardStatus.done
        worker = WorkBoardWorkerTools(
            repository=WorkBoardRepository(),
            jobs=object(),
            session_provider=async_db,
        )
        assert not hasattr(worker, "complete_review")


async def _handoff_fixture(db, *, proof_available: bool):
    goal = await _goal(db, "goal-handoff-recovery")
    parent = WorkBoardTask(
        task_id="handoff-recovery-parent",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        origin_session_id=OWNER.session_id,
        goal_id=goal.id,
        goal_revision=1,
        title="Completed parent",
        idempotency_key="handoff-recovery-parent-key",
        status=WorkBoardStatus.done,
        task_revision=3,
    )
    child = WorkBoardTask(
        task_id="handoff-recovery-child",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        origin_session_id=OWNER.session_id,
        goal_id=goal.id,
        goal_revision=1,
        title="Blocked child",
        idempotency_key="handoff-recovery-child-key",
        status=WorkBoardStatus.blocked,
        block_kind="dependency",
        block_reason=review_service._HANDOFF_RECONCILIATION_REASON,
        block_source_status=WorkBoardStatus.todo.value,
        task_revision=2,
    )
    run_id = "handoff-recovery-run"
    run = WorkflowRunState(
        run_identity=run_id,
        root_run_identity=run_id,
        workflow_name="review-hardening",
        operator_session_id=OWNER.session_id,
        status="succeeded",
        owner_kind="user",
        owner_principal_id=OWNER.principal_id,
        goal_id=goal.id,
        goal_revision=1,
    )
    receipt = _proof(run_id, "handoff-output", readback_id="handoff-readback") if proof_available else {}
    attempt = WorkBoardAttempt(
        attempt_id="handoff-recovery-attempt",
        task_id=parent.task_id,
        workflow_run_id=run_id,
        task_revision_at_claim=3,
        fencing_token=9,
        executor_id="executor:deterministic",
        started_at=_now() - timedelta(minutes=1),
        ended_at=_now() - timedelta(seconds=30),
        outcome="succeeded",
        receipt_refs_json=json.dumps([receipt] if receipt else []),
        created_at=_now() - timedelta(minutes=1),
    )
    link = WorkBoardLink(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        parent_task_id=parent.task_id,
        child_task_id=child.task_id,
    )
    db.add_all([parent, child])
    await db.flush()
    db.add(run)
    await db.flush()
    db.add_all([attempt, link])
    await db.flush()
    return parent, child, attempt, link


@pytest.mark.asyncio
async def test_handoff_recovery_stays_blocked_without_proof_then_restores_safely(async_db):
    async with async_db() as db:
        parent, child, attempt, link = await _handoff_fixture(db, proof_available=False)
        with pytest.raises(BoardError) as blocked:
            await review_service.unblock_task(
                db,
                OWNER,
                child.task_id,
                expected_revision=child.task_revision,
                resolution="reconcile parent readback",
            )
        assert blocked.value.code == "handoff_materialization_required"
        assert child.status is WorkBoardStatus.blocked
        assert link.current_handoff_id is None

        attempt.receipt_refs_json = json.dumps([_proof(
            attempt.workflow_run_id or "",
            "handoff-output",
            readback_id="handoff-readback",
        )])
        await db.flush()
        mutation = await review_service.unblock_task(
            db,
            OWNER,
            child.task_id,
            expected_revision=child.task_revision,
            resolution="parent readback reconciled",
        )
        assert mutation.task.status is WorkBoardStatus.todo
        assert mutation.task.block_reason is None
        assert link.current_handoff_id
        handoff_events = await db.scalar(
            select(func.count(WorkBoardEvent.event_id)).where(
                WorkBoardEvent.task_id == child.task_id,
                WorkBoardEvent.kind == "task.unblocked",
            )
        )
        assert handoff_events == 1


@pytest.mark.asyncio
async def test_startup_backfill_does_not_repeat_proofless_block_or_starve_recovery(async_db):
    async with async_db() as db:
        parent, child, attempt, link = await _handoff_fixture(db, proof_available=False)
        # Simulate the first legacy migration pass on an otherwise Todo child.
        child.status = WorkBoardStatus.todo
        child.block_reason = None
        child.block_kind = None
        child.block_source_status = None
        await db.flush()
        assert await review_service.backfill_verified_handoffs(db) == 0
        await db.flush()
        assert child.status is WorkBoardStatus.blocked
        initial_event_count = await db.scalar(
            select(func.count(WorkBoardEvent.event_id)).where(
                WorkBoardEvent.task_id == child.task_id,
                WorkBoardEvent.kind == "task.handoff_reconciliation_required",
            )
        )
        assert initial_event_count == 1
        assert await review_service.backfill_verified_handoffs(db) == 0
        repeated_event_count = await db.scalar(
            select(func.count(WorkBoardEvent.event_id)).where(
                WorkBoardEvent.task_id == child.task_id,
                WorkBoardEvent.kind == "task.handoff_reconciliation_required",
            )
        )
        assert repeated_event_count == initial_event_count

        attempt.receipt_refs_json = json.dumps([_proof(
            attempt.workflow_run_id or "",
            "handoff-output",
            readback_id="handoff-readback",
        )])
        await db.flush()
        assert await review_service.backfill_verified_handoffs(db) == 1
        assert child.status is WorkBoardStatus.blocked
        assert child.block_source_status == WorkBoardStatus.todo.value
        assert link.current_handoff_id
        await db.commit()
        mutation = await review_service.unblock_task(
            db,
            OWNER,
            child.task_id,
            expected_revision=child.task_revision,
            resolution="operator reviewed the newly materialized parent handoff",
        )
        assert mutation.task.status is WorkBoardStatus.todo
        assert mutation.task.block_reason is None
        assert await review_service.backfill_verified_handoffs(db) == 0


@pytest.mark.asyncio
async def test_startup_handoff_backfill_keeps_ready_child_blocked_until_live_unblock(async_db):
    async with async_db() as db:
        parent, child, attempt, link = await _handoff_fixture(db, proof_available=False)
        child.block_source_status = WorkBoardStatus.ready.value
        await db.flush()

        attempt.receipt_refs_json = json.dumps([_proof(
            attempt.workflow_run_id or "",
            "handoff-output",
            readback_id="handoff-readback",
        )])
        await db.flush()

        assert await review_service.backfill_verified_handoffs(db) == 1
        assert link.current_handoff_id
        assert child.status is WorkBoardStatus.blocked
        assert child.block_source_status == WorkBoardStatus.ready.value

        await db.commit()
        mutation = await review_service.unblock_task(
            db,
            OWNER,
            child.task_id,
            expected_revision=child.task_revision,
            resolution="recheck current authority before dispatch",
        )
        assert mutation.task.status is WorkBoardStatus.todo
        assert mutation.task.block_reason is None
