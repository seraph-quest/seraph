"""Proof that source-watch durable roots can enter the canonical handoff path."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

import pytest

from src.db.models import (
    Goal,
    Session,
    WorkBoardAttempt,
    WorkBoardLink,
    WorkBoardStatus,
    WorkBoardTask,
    WorkflowRunState,
)
from src.work_board import review as review_service
from src.work_board.contracts import WorkBoardOwner


@pytest.mark.asyncio
async def test_verified_source_watch_root_materializes_bounded_parent_handoff(async_db):
    owner = WorkBoardOwner(
        principal_id="operator:m6-source-watch",
        session_id="session:m6-source-watch",
    )
    run_id = "source-watch:watch-m6:occurrence-m6"
    attempt_id = "attempt-m6-source-watch"
    content_digest = hashlib.sha256(b"verified fixture artifact").hexdigest()
    input_digest = hashlib.sha256(b"redacted source-watch job inputs").hexdigest()
    now = datetime.now(timezone.utc)

    async with async_db() as db:
        db.add(Session(id=owner.session_id, owner_principal_id=owner.principal_id))
        db.add(
            Goal(
                id="goal-m6-source-watch",
                title="M6 source-watch handoff",
                owner_principal_id=owner.principal_id,
                owner_session_id=owner.session_id,
                revision=2,
                status="active",
            )
        )
        await db.flush()

        parent = WorkBoardTask(
            task_id="m6-source-watch-parent",
            owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,
            origin_session_id=owner.session_id,
            goal_id="goal-m6-source-watch",
            goal_revision=2,
            title="Verified source-watch result",
            body="Private source body and credential=never-forward",
            capability_id="guardian.research-watch.v1",
            executor_id="seraph-work-board:guardian.research-watch.v1",
            idempotency_key="m6-source-watch-parent-key",
            status=WorkBoardStatus.done,
            task_revision=4,
            result_refs_json=json.dumps(
                [{"job_id": run_id, "workflow_run_id": run_id, "status": "succeeded", "verified": True}]
            ),
            artifact_refs_json=json.dumps(
                [
                    {
                        "artifact_id": "art_m6_source",
                        "artifact_type": "guardian_decision_dossier",
                        "content_sha256": content_digest,
                        "file_path": "guardian/source-watches/watch-m6/packet.md",
                        "verified": True,
                    }
                ]
            ),
        )
        child = WorkBoardTask(
            task_id="m6-source-watch-child",
            owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,
            origin_session_id=owner.session_id,
            goal_id="goal-m6-source-watch",
            goal_revision=2,
            title="Follow through from verified source",
            idempotency_key="m6-source-watch-child-key",
            status=WorkBoardStatus.todo,
            task_revision=1,
        )
        db.add_all([parent, child])
        await db.flush()

        authority = {
            "principal": "service:guardian-source-watch",
            "owner_kind": "service",
            "service_id": "guardian-source-watch",
            "session_id": owner.session_id,
            "goal_id": "goal-m6-source-watch",
            "goal_revision": 2,
            "plan_revision": 3,
            "goal_owner_principal_id": owner.principal_id,
            "goal_owner_session_id": owner.session_id,
            "capability_id": "guardian.research-watch.v1",
            "permissions": ["source_observation", "workspace_write"],
            "budget_microusd": 0,
            "source_set_digest": hashlib.sha256(b"source set").hexdigest(),
            "criteria_digest": hashlib.sha256(b"criteria").hexdigest(),
        }
        run = WorkflowRunState(
            run_identity=run_id,
            root_run_identity=run_id,
            workflow_name="guardian_source_watch",
            tool_name="guardian_source_watch",
            session_id=owner.session_id,
            operator_session_id=owner.session_id,
            status="succeeded",
            run_fingerprint=input_digest,
            input_digest=input_digest,
            arguments_json=json.dumps(
                {"redacted": True, "shape": "dict", "keys": ["occurrence_id", "watch_id"]},
                sort_keys=True,
            ),
            job_kind="guardian_source_watch",
            owner_kind="service",
            owner_principal_id="service:guardian-source-watch",
            service_id="guardian-source-watch",
            goal_id="goal-m6-source-watch",
            goal_revision=2,
            plan_revision=3,
            capability_version="1",
            idempotency_scope="work-board-attempt",
            idempotency_key=f"{parent.task_id}:{attempt_id}",
            declared_authority_json=json.dumps(authority, sort_keys=True),
            finished_at=now,
        )
        attempt = WorkBoardAttempt(
            attempt_id=attempt_id,
            task_id=parent.task_id,
            workflow_run_id=run_id,
            task_revision_at_claim=2,
            fencing_token=1,
            executor_id="seraph-work-board:guardian.research-watch.v1",
            started_at=now,
            ended_at=now,
            outcome="verified",
            readback_status="verified",
            verification_status="passed",
            receipt_refs_json=json.dumps(
                [
                    {
                        "receipt_kind": "readback",
                        "effect_id_digest": "ac8dbfa1983c4cbb",
                        "readback_id": "guardian_readback:m6-source-readback",
                        "verified_at": now.isoformat(),
                        "verified": True,
                        "verification_status": "passed",
                        "content_sha256": content_digest,
                        "readback_status": "verified",
                        "workflow_run_id": run_id,
                        "status": "succeeded",
                    }
                ]
            ),
        )
        link = WorkBoardLink(
            owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,
            parent_task_id=parent.task_id,
            child_task_id=child.task_id,
        )
        db.add_all([run, attempt, link])
        await db.flush()

        proof = await review_service._verified_workflow_readback(db, parent, attempt)
        assert proof is not None
        handoff = await review_service.materialize_handoff_for_link(
            db,
            owner,
            parent,
            child,
            link,
        )
        assert link.current_handoff_id == handoff.handoff_id
        assert handoff.workflow_run_id == run_id
        assert "Private source body" not in handoff.summary
        assert "credential" not in handoff.summary
        assert json.loads(handoff.summary)["result"]["verification"] == "independent_readback_passed"

        # A source-watch service row with a different task/attempt binding is
        # not accepted as proof for this card even when it is terminal.
        run.idempotency_key = "other-task:other-attempt"
        await db.flush()
        assert await review_service._verified_workflow_readback(db, parent, attempt) is None
