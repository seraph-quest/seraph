"""Provider-free M5 journeys over canonical board, workflow, and memory rows.

These tests deliberately build the smallest durable receipt that the existing
work-board review verifier accepts.  They do not replace that verifier with a
mock or insert an already accepted M5 preference.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import delete
from sqlmodel import select

from src.api import goals as goals_api
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.auth.service import test_bypass_operator as make_test_bypass_operator
from src.db.models import (
    AuditEvent,
    Goal,
    Memory,
    MemoryProposal,
    MemoryProposalDecisionEffect,
    MemoryProposalStatus,
    MemoryKind,
    MemoryStatus,
    MemorySource,
    Session,
    WorkBoardAttempt,
    WorkBoardDecisionReceipt,
    WorkBoardDecisionReceiptStage,
    WorkBoardStatus,
    WorkBoardTask,
    WorkflowRunState,
)
from src.goals.contracts import (
    GoalCandidateRequest,
    GoalCandidateSetRequest,
    GoalSuccessCriterion,
)
from src.guardian.goal_conditioned_loop import propose_goal_candidate_set
from src.memory import m5
from src.memory.repository import memory_repository


OWNER = SimpleNamespace(principal_id="operator:m5-test", session_id="session:m5-test")
OTHER = SimpleNamespace(principal_id="operator:other", session_id="session:other")
SOURCE_CAPABILITY = "workflow.goal-snapshot-to-file"
ALTERNATE_CAPABILITY = "guardian.research-watch.v1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def _runtime_operator(operator):
    tokens = set_runtime_context(
        operator.session_id,
        "off",
        trust_principal=operator.principal,
    )
    try:
        yield
    finally:
        reset_runtime_context(tokens)


def _typed_input_digest(inputs: dict[str, object]) -> str:
    return m5.m5_digest({"version": m5.M5_DIGEST_VERSION, "inputs": inputs})


async def _goal_and_tasks(db, *, goal_id: str = "m5-goal"):
    task_prefix = goal_id.removesuffix("-goal")
    goal = Goal(
        id=goal_id,
        title="M5 verified goal",
        revision=1,
        status="active",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        success_criterion_json=GoalSuccessCriterion(
            description="A verified artifact exists",
            verifier_kind="artifact_readback",
            evidence_refs=["operator:m5"],
        ).model_dump_json(),
    )
    source_inputs = {"file_path": "reports/m5-source.md"}
    source = WorkBoardTask(
        task_id=f"{task_prefix}-source-task",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        origin_session_id=OWNER.session_id,
        goal_id=goal.id,
        goal_revision=goal.revision,
        title="M5 comparable task",
        body="Same bounded task context",
        capability_id=SOURCE_CAPABILITY,
        typed_input_ref="input:m5-source",
        typed_input_digest=_typed_input_digest(source_inputs),
        executor_id=f"seraph-work-board:{SOURCE_CAPABILITY}",
        idempotency_key=f"{task_prefix}-source-key",
        task_revision=1,
        status=WorkBoardStatus.done,
        completed_at=_now(),
    )
    later = WorkBoardTask(
        task_id=f"{task_prefix}-later-task",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        origin_session_id=OWNER.session_id,
        goal_id=goal.id,
        goal_revision=goal.revision,
        title=source.title,
        body=source.body,
        capability_id=SOURCE_CAPABILITY,
        typed_input_ref="input:m5-later",
        typed_input_digest=source.typed_input_digest,
        executor_id=f"seraph-work-board:{SOURCE_CAPABILITY}",
        idempotency_key=f"{task_prefix}-later-key",
        task_revision=1,
        status=WorkBoardStatus.todo,
    )
    db.add(goal)
    db.add(source)
    db.add(later)
    if await db.get(Session, OWNER.session_id) is None:
        db.add(Session(id=OWNER.session_id, owner_principal_id=OWNER.principal_id))
    await db.flush()
    return goal, source, later


async def _verified_attempt(db, task: WorkBoardTask) -> WorkBoardAttempt:
    """Persist the durable proof consumed by review._verified_workflow_readback."""

    task_prefix = task.task_id.removesuffix("-task")
    attempt_id = f"{task_prefix}-attempt"
    run_id = f"{task_prefix}-run"
    attempt = WorkBoardAttempt(
        attempt_id=attempt_id,
        task_id=task.task_id,
        workflow_run_id=run_id,
        task_revision_at_claim=task.task_revision,
        lease_owner="seraph-work-board",
        fencing_token=4,
        started_at=_now(),
        ended_at=_now(),
        outcome="succeeded",
        receipt_refs_json=json.dumps(
            [
                {
                    "receipt_kind": "readback",
                    "status": "succeeded",
                    "verified": True,
                    "workflow_run_id": run_id,
                    "readback_id": "readback:m5-source",
                    "artifact_id": "artifact:m5-source",
                    "content_sha256": "a" * 64,
                    "verified_at": _now().isoformat(),
                }
            ],
            separators=(",", ":"),
        ),
    )
    run = WorkflowRunState(
        run_identity=run_id,
        root_run_identity=run_id,
        workflow_name="goal-snapshot-to-file",
        tool_name="goal-snapshot-to-file",
        session_id=task.owner_session_id,
        operator_session_id=task.owner_session_id,
        owner_kind="user",
        owner_principal_id=task.owner_principal_id,
        goal_id=task.goal_id,
        goal_revision=task.goal_revision,
        status="succeeded",
        job_kind=SOURCE_CAPABILITY,
        capability_version="1",
        idempotency_scope="work-board-attempt",
        idempotency_key=f"{task.task_id}:{attempt_id}",
        arguments_json=json.dumps(
            {"redacted": True, "shape": "dict", "keys": ["file_path"]},
            separators=(",", ":"),
        ),
        input_digest="b" * 64,
        run_fingerprint="c" * 64,
        started_at=_now(),
        finished_at=_now(),
    )
    db.add(attempt)
    db.add(run)
    await db.flush()
    return attempt


async def _unverified_attempt(db, task: WorkBoardTask) -> WorkBoardAttempt:
    """Persist a real attempt/run whose independent readback is absent."""

    task_prefix = task.task_id.removesuffix("-task")
    attempt_id = f"{task_prefix}-unverified-attempt"
    run_id = f"{task_prefix}-unverified-run"
    attempt = WorkBoardAttempt(
        attempt_id=attempt_id,
        task_id=task.task_id,
        workflow_run_id=run_id,
        task_revision_at_claim=task.task_revision,
        fencing_token=7,
        started_at=_now(),
        ended_at=_now(),
        outcome="succeeded",
        receipt_refs_json="[]",
    )
    run = WorkflowRunState(
        run_identity=run_id,
        root_run_identity=run_id,
        workflow_name="goal-snapshot-to-file",
        session_id=task.owner_session_id,
        operator_session_id=task.owner_session_id,
        owner_kind="user",
        owner_principal_id=task.owner_principal_id,
        goal_id=task.goal_id,
        goal_revision=task.goal_revision,
        status="succeeded",
        job_kind=SOURCE_CAPABILITY,
        capability_version="1",
        idempotency_scope="work-board-attempt",
        idempotency_key=f"{task.task_id}:{attempt_id}",
        arguments_json=json.dumps({"redacted": True, "shape": "dict"}),
        input_digest="d" * 64,
        run_fingerprint="e" * 64,
        started_at=_now(),
        finished_at=_now(),
    )
    db.add(attempt)
    db.add(run)
    await db.flush()
    return attempt


def _patch_m5_sessions(monkeypatch, async_db) -> None:
    monkeypatch.setattr(m5, "get_session", async_db)
    monkeypatch.setattr("src.guardian.goal_conditioned_loop.get_session", async_db)
    monkeypatch.setattr("src.goals.repository.get_session", async_db)


def _candidate(capability_id: str) -> GoalCandidateRequest:
    if capability_id == SOURCE_CAPABILITY:
        inputs = {"file_path": "reports/m5-later.md"}
    else:
        inputs = {"watch_id": "watch:m5", "expected_plan_revision": 1}
    return GoalCandidateRequest(
        capability_id=capability_id,
        capability_version="1",
        inputs=inputs,
        evidence_refs=["evidence:m5"],
    )


@pytest.mark.asyncio
async def test_fresh_conversation_uses_accepted_correction(async_db, monkeypatch):
    async with async_db() as db:
        goal, source, later = await _goal_and_tasks(db)
        attempt = await _verified_attempt(db, source)
        prior_memory = Memory(
            id="m5-prior-memory",
            content="Prior operator-reviewed preference",
            kind=MemoryKind.fact,
            status=MemoryStatus.active,
            source_session_id=OWNER.session_id,
            scope_key="m5-prior-memory-scope",
            metadata_json="{}",
        )
        db.add(prior_memory)
        await db.flush()
    _patch_m5_sessions(monkeypatch, async_db)

    proposal = await m5.create_memory_proposal(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        task_id=source.task_id,
        expected_task_revision=source.task_revision,
        attempt_id=attempt.attempt_id,
        candidate_text="The verified source supports the reviewed research route.",
        decision_effect=MemoryProposalDecisionEffect.require_operator_confirmation,
    )
    assert proposal["status"] == MemoryProposalStatus.proposed.value
    assert proposal["artifact_ref"] == "artifact:m5-source"
    assert proposal["artifact_digest"] == "a" * 64
    assert proposal["readback_ref"] == "readback:m5-source"
    assert proposal["source_attempt_fence"] == 4

    accepted = await m5.apply_memory_proposal_action(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        proposal_id=proposal["proposal_id"],
        action="edit_accept",
        expected_revision=proposal["revision"],
        expected_preview_text_digest=proposal["proposed_text_digest"],
        expected_task_revision=source.task_revision,
        expected_goal_revision=goal.revision,
        edited_text="Edited operator correction: choose the reviewed research route when it is a current candidate.",
        decision_effect=MemoryProposalDecisionEffect.require_operator_confirmation,
        preferred_capability_id=ALTERNATE_CAPABILITY,
        corrects_memory_id=prior_memory.id,
    )
    assert accepted["status"] == MemoryProposalStatus.accepted.value
    assert accepted["preferred_capability_id"] == ALTERNATE_CAPABILITY
    assert accepted["accepted_memory_id"]

    async with async_db() as db:
        memory = (
            await db.execute(select(Memory).where(Memory.id == accepted["accepted_memory_id"]))
        ).scalar_one()
        assert memory.status is MemoryStatus.active
        assert memory.content.startswith("Edited operator correction:")
        prior_memory = (
            await db.execute(select(Memory).where(Memory.id == "m5-prior-memory"))
        ).scalar_one()
        assert prior_memory.status is MemoryStatus.superseded
        events = (
            await db.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.session_id == OWNER.session_id,
                    AuditEvent.event_type == "memory_corrected",
                )
            )
        ).scalars().all()
        assert len(events) == 1

    replay = await m5.apply_memory_proposal_action(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        proposal_id=proposal["proposal_id"],
        action="edit_accept",
        expected_revision=proposal["revision"],
        expected_preview_text_digest=proposal["proposed_text_digest"],
        expected_task_revision=source.task_revision,
        expected_goal_revision=goal.revision,
        edited_text="Edited operator correction: choose the reviewed research route when it is a current candidate.",
        decision_effect=MemoryProposalDecisionEffect.require_operator_confirmation,
        preferred_capability_id=ALTERNATE_CAPABILITY,
        corrects_memory_id="m5-prior-memory",
    )
    assert replay["idempotent_replay"] is True
    assert replay["accepted_memory_id"] == accepted["accepted_memory_id"]

    later_result = await propose_goal_candidate_set(
        goal_id=goal.id,
        task_id=later.task_id,
        candidates=[_candidate(SOURCE_CAPABILITY), _candidate(ALTERNATE_CAPABILITY)],
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        expected_task_revision=later.task_revision,
        expected_goal_revision=goal.revision,
    )
    decision = later_result["decision"]
    assert decision["decision_status"] == "changed"
    assert decision["before_selected_capability_id"] == SOURCE_CAPABILITY
    assert decision["after_selected_capability_id"] == ALTERNATE_CAPABILITY
    assert later_result["selected"]["capability_id"] == ALTERNATE_CAPABILITY

    async with async_db() as db:
        receipt = (
            await db.execute(
                select(WorkBoardDecisionReceipt).where(
                    WorkBoardDecisionReceipt.receipt_id == decision["receipt_id"]
                )
            )
        )
        receipt = receipt.scalar_one()
        assert receipt.source_proposal_id == proposal["proposal_id"]
        assert receipt.source_attempt_id == attempt.attempt_id
        assert receipt.before_selected_capability_id == SOURCE_CAPABILITY
        baseline = (
            await db.execute(
                select(WorkBoardDecisionReceipt).where(
                    WorkBoardDecisionReceipt.receipt_id == receipt.source_baseline_receipt_id
                )
            )
        ).scalar_one()
        assert baseline.receipt_stage.value == "source_baseline"
        assert receipt.before_input_digest == baseline.before_input_digest
        assert receipt.candidate_set_digest == decision["candidate_set_digest"]
        assert receipt.after_selected_capability_id == ALTERNATE_CAPABILITY
        assert receipt.accepted_memory_id == accepted["accepted_memory_id"]
        assert receipt.retrieval_evidence_ids_json != "[]"

    rolled_back = await m5.apply_memory_proposal_action(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        proposal_id=accepted["proposal_id"],
        action="rollback",
        expected_revision=accepted["revision"],
        expected_task_revision=source.task_revision,
        expected_goal_revision=goal.revision,
        reason="The reviewed correction was intentionally withdrawn.",
    )
    assert rolled_back["status"] == MemoryProposalStatus.rolled_back.value
    assert rolled_back["audit_event_id"]

    async with async_db() as db:
        memory = (
            await db.execute(select(Memory).where(Memory.id == accepted["accepted_memory_id"]))
        ).scalar_one()
        assert memory.status is MemoryStatus.archived
        prior_memory = (
            await db.execute(select(Memory).where(Memory.id == "m5-prior-memory"))
        ).scalar_one()
        assert prior_memory.status is MemoryStatus.active
        events = (
            await db.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.session_id == OWNER.session_id,
                    AuditEvent.event_type.in_(("memory_corrected", "memory_learning_rolled_back")),
                )
                .order_by(AuditEvent.created_at.asc())
            )
        ).scalars().all()
        assert [event.event_type for event in events] == [
            "memory_corrected",
            "memory_learning_rolled_back",
        ]
        receipt = (
            await db.execute(
                select(WorkBoardDecisionReceipt).where(
                    WorkBoardDecisionReceipt.receipt_id == decision["receipt_id"]
                )
            )
        ).scalar_one()
        assert receipt.decision_status.value == "blocked"
        assert receipt.reason == "memory_rolled_back"


@pytest.mark.asyncio
async def test_m5_export_restore_preserves_scope_for_later_decision(async_db, monkeypatch):
    bypass = make_test_bypass_operator()
    operator = replace(
        bypass,
        session_id=OWNER.session_id,
        principal=replace(
            bypass.principal,
            principal_id=OWNER.principal_id,
            session_id=OWNER.session_id,
            operator_session_id=OWNER.session_id,
        ),
    )
    async with async_db() as db:
        goal, source, later = await _goal_and_tasks(db, goal_id="m5-recovery-goal")
        attempt = await _verified_attempt(db, source)
    _patch_m5_sessions(monkeypatch, async_db)

    proposal = await m5.create_memory_proposal(
        owner_principal_id=operator.principal.principal_id,
        owner_session_id=operator.session_id,
        task_id=source.task_id,
        expected_task_revision=source.task_revision,
        attempt_id=attempt.attempt_id,
        candidate_text="The verified source supports the reviewed research route.",
        decision_effect=MemoryProposalDecisionEffect.require_operator_confirmation,
    )
    accepted = await m5.apply_memory_proposal_action(
        owner_principal_id=operator.principal.principal_id,
        owner_session_id=operator.session_id,
        proposal_id=proposal["proposal_id"],
        action="accept",
        expected_revision=proposal["revision"],
        expected_preview_text_digest=proposal["proposed_text_digest"],
        expected_task_revision=source.task_revision,
        expected_goal_revision=goal.revision,
        decision_effect=MemoryProposalDecisionEffect.require_operator_confirmation,
        preferred_capability_id=ALTERNATE_CAPABILITY,
    )

    with _runtime_operator(operator):
        archive = await memory_repository.export_canonical_memory_state(
            actor=operator.principal.principal_id,
            owner_session_id=operator.session_id,
            authenticated_session_id=operator.session_id,
        )

    proposal_archive = next(
        item for item in archive["m5_proposals"] if item["proposal_id"] == proposal["proposal_id"]
    )
    assert proposal_archive["memory_scope"]["goal_id"] == goal.id
    assert proposal_archive["memory_scope"]["goal_revision"] == goal.revision
    assert proposal_archive["memory_scope"]["preferred_capability_id"] == ALTERNATE_CAPABILITY
    assert proposal_archive["source_evidence_ids"]

    async with async_db() as db:
        await db.execute(
            delete(WorkBoardDecisionReceipt).where(
                WorkBoardDecisionReceipt.owner_session_id == operator.session_id
            )
        )
        await db.execute(
            delete(MemoryProposal).where(
                MemoryProposal.owner_session_id == operator.session_id
            )
        )
        await db.flush()
        await db.execute(
            delete(MemorySource).where(MemorySource.memory_id == accepted["accepted_memory_id"])
        )
        await db.flush()
        await db.execute(
            delete(Memory).where(Memory.id == accepted["accepted_memory_id"])
        )
        await db.flush()

    with _runtime_operator(operator):
        restored = await memory_repository.restore_canonical_memory_state(
            archive,
            actor=operator.principal.principal_id,
            owner_session_id=operator.session_id,
            authenticated_session_id=operator.session_id,
        )
    assert restored["m5_restored_proposal_ids"] == [proposal["proposal_id"]]
    assert restored["m5_restored_receipt_ids"]

    restored_decision = await propose_goal_candidate_set(
        goal_id=goal.id,
        task_id=later.task_id,
        candidates=[_candidate(SOURCE_CAPABILITY), _candidate(ALTERNATE_CAPABILITY)],
        owner_principal_id=operator.principal.principal_id,
        owner_session_id=operator.session_id,
        expected_task_revision=later.task_revision,
        expected_goal_revision=goal.revision,
    )
    decision = restored_decision["decision"]
    assert decision["decision_status"] == "changed"
    assert decision["after_selected_capability_id"] == ALTERNATE_CAPABILITY
    assert set(proposal_archive["source_evidence_ids"]).issubset(set(decision["evidence_ids"]))


@pytest.mark.asyncio
async def test_stale_source_fence_rejects_edit_accept(async_db, monkeypatch):
    async with async_db() as db:
        goal, source, _later = await _goal_and_tasks(db, goal_id="m5-stale-goal")
        attempt = await _verified_attempt(db, source)
    _patch_m5_sessions(monkeypatch, async_db)

    proposal = await m5.create_memory_proposal(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        task_id=source.task_id,
        expected_task_revision=source.task_revision,
        attempt_id=attempt.attempt_id,
        decision_effect=MemoryProposalDecisionEffect.require_operator_confirmation,
    )
    async with async_db() as db:
        current_attempt = (
            await db.execute(
                select(WorkBoardAttempt).where(
                    WorkBoardAttempt.attempt_id == attempt.attempt_id
                )
            )
        ).scalar_one()
        current_attempt.fencing_token = 5
        db.add(current_attempt)
        await db.flush()

    with pytest.raises(ValueError, match="stale_source_evidence"):
        await m5.apply_memory_proposal_action(
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            proposal_id=proposal["proposal_id"],
            action="edit_accept",
            expected_revision=proposal["revision"],
            expected_preview_text_digest=proposal["proposed_text_digest"],
            expected_task_revision=source.task_revision,
            expected_goal_revision=goal.revision,
            edited_text="This stale source must not be accepted.",
            decision_effect=MemoryProposalDecisionEffect.require_operator_confirmation,
            preferred_capability_id=ALTERNATE_CAPABILITY,
        )


@pytest.mark.parametrize(
    ("stale_entity", "error_code"),
    (("goal", "stale_goal_revision"), ("task", "stale_task_revision")),
)
@pytest.mark.asyncio
async def test_stale_goal_or_task_revision_rejects_accept(
    async_db,
    monkeypatch,
    stale_entity: str,
    error_code: str,
):
    async with async_db() as db:
        goal, source, _later = await _goal_and_tasks(
            db, goal_id=f"m5-stale-{stale_entity}-goal"
        )
        attempt = await _verified_attempt(db, source)
    _patch_m5_sessions(monkeypatch, async_db)
    proposal = await m5.create_memory_proposal(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        task_id=source.task_id,
        expected_task_revision=source.task_revision,
        attempt_id=attempt.attempt_id,
    )
    async with async_db() as db:
        if stale_entity == "goal":
            current = (await db.execute(select(Goal).where(Goal.id == goal.id))).scalar_one()
            current.revision += 1
        else:
            current = (
                await db.execute(
                    select(WorkBoardTask).where(WorkBoardTask.task_id == source.task_id)
                )
            ).scalar_one()
            current.task_revision += 1
        db.add(current)
        await db.flush()

    with pytest.raises(ValueError, match=error_code):
        await m5.apply_memory_proposal_action(
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            proposal_id=proposal["proposal_id"],
            action="accept",
            expected_revision=proposal["revision"],
            expected_preview_text_digest=proposal["proposed_text_digest"],
            expected_task_revision=source.task_revision,
            expected_goal_revision=goal.revision,
            decision_effect=MemoryProposalDecisionEffect.none,
        )


@pytest.mark.asyncio
async def test_cross_owner_or_session_denied(async_db, monkeypatch):
    async with async_db() as db:
        goal, source, _later = await _goal_and_tasks(db, goal_id="m5-owner-goal")
        attempt = await _verified_attempt(db, source)
    _patch_m5_sessions(monkeypatch, async_db)

    with pytest.raises(PermissionError):
        await m5.create_memory_proposal(
            owner_principal_id=OTHER.principal_id,
            owner_session_id=OTHER.session_id,
            task_id=source.task_id,
            expected_task_revision=source.task_revision,
            attempt_id=attempt.attempt_id,
        )

    proposal = await m5.create_memory_proposal(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        task_id=source.task_id,
        expected_task_revision=source.task_revision,
        attempt_id=attempt.attempt_id,
    )
    with pytest.raises(PermissionError):
        await m5.apply_memory_proposal_action(
            owner_principal_id=OTHER.principal_id,
            owner_session_id=OTHER.session_id,
            proposal_id=proposal["proposal_id"],
            action="accept",
            expected_revision=proposal["revision"],
            expected_preview_text_digest=proposal["proposed_text_digest"],
            expected_task_revision=source.task_revision,
            expected_goal_revision=goal.revision,
            decision_effect=MemoryProposalDecisionEffect.require_operator_confirmation,
        )


@pytest.mark.asyncio
async def test_unverified_readback_records_no_learning(async_db, monkeypatch):
    async with async_db() as db:
        _goal, source, _later = await _goal_and_tasks(db, goal_id="m5-no-learning-goal")
        attempt = await _unverified_attempt(db, source)
    _patch_m5_sessions(monkeypatch, async_db)

    result = await m5.create_memory_proposal(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        task_id=source.task_id,
        expected_task_revision=source.task_revision,
        attempt_id=attempt.attempt_id,
    )
    assert result["status"] == MemoryProposalStatus.no_learning.value
    assert result["reason_code"] == "readback_not_verified"
    assert result["recovery_action"] == "complete_verified_readback_then_request_again"
    assert result["accepted_memory_id"] is None

    async with async_db() as db:
        memories = (await db.execute(select(Memory))).scalars().all()
        assert memories == []
        proposal = (
            await db.execute(
                select(MemoryProposal).where(
                    MemoryProposal.proposal_id == result["proposal_id"]
                )
            )
        ).scalar_one()
        assert proposal.status is MemoryProposalStatus.no_learning


@pytest.mark.asyncio
async def test_verified_outcome_proposes_correction(async_db, monkeypatch):
    async with async_db() as db:
        _goal, source, _later = await _goal_and_tasks(db, goal_id="m5-proposal-goal")
        attempt = await _verified_attempt(db, source)
    _patch_m5_sessions(monkeypatch, async_db)

    proposal = await m5.create_memory_proposal(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        task_id=source.task_id,
        expected_task_revision=source.task_revision,
        attempt_id=attempt.attempt_id,
        candidate_text="The verified source supports a reviewed correction.",
        decision_effect=MemoryProposalDecisionEffect.require_operator_confirmation,
    )
    assert proposal["status"] == MemoryProposalStatus.proposed.value
    assert proposal["artifact_ref"] == "artifact:m5-source"
    assert proposal["artifact_digest"] == "a" * 64
    assert proposal["readback_ref"] == "readback:m5-source"
    assert proposal["evidence_digest"]

    async with async_db() as db:
        baseline = (
            await db.execute(
                select(WorkBoardDecisionReceipt).where(
                    WorkBoardDecisionReceipt.receipt_stage
                    == WorkBoardDecisionReceiptStage.source_baseline,
                    WorkBoardDecisionReceipt.source_proposal_id == proposal["proposal_id"],
                )
            )
        ).scalar_one()
        assert baseline.before_selected_capability_id == SOURCE_CAPABILITY
        assert baseline.after_selected_capability_id == SOURCE_CAPABILITY


@pytest.mark.asyncio
async def test_reject_and_rollback_preserve_audit(async_db, monkeypatch):
    async with async_db() as db:
        goal, source, _later = await _goal_and_tasks(db, goal_id="m5-reject-goal")
        attempt = await _verified_attempt(db, source)
    _patch_m5_sessions(monkeypatch, async_db)

    rejected_proposal = await m5.create_memory_proposal(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        task_id=source.task_id,
        expected_task_revision=source.task_revision,
        attempt_id=attempt.attempt_id,
        candidate_text="A reviewed candidate that the operator rejects.",
    )
    rejected = await m5.apply_memory_proposal_action(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        proposal_id=rejected_proposal["proposal_id"],
        action="reject",
        expected_revision=rejected_proposal["revision"],
        expected_preview_text_digest=rejected_proposal["proposed_text_digest"],
        expected_task_revision=source.task_revision,
        expected_goal_revision=goal.revision,
        reason="not useful",
    )
    assert rejected["status"] == MemoryProposalStatus.rejected.value
    assert rejected["audit_event_id"]

    async with async_db() as db:
        next_goal, next_source, _next_later = await _goal_and_tasks(
            db, goal_id="m5-rollback-goal"
        )
        next_attempt = await _verified_attempt(db, next_source)
    accepted_proposal = await m5.create_memory_proposal(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        task_id=next_source.task_id,
        expected_task_revision=next_source.task_revision,
        attempt_id=next_attempt.attempt_id,
        candidate_text="A separate reviewed candidate for rollback proof.",
    )
    accepted = await m5.apply_memory_proposal_action(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        proposal_id=accepted_proposal["proposal_id"],
        action="accept",
        expected_revision=accepted_proposal["revision"],
        expected_preview_text_digest=accepted_proposal["proposed_text_digest"],
        expected_task_revision=next_source.task_revision,
        expected_goal_revision=next_goal.revision,
        decision_effect=MemoryProposalDecisionEffect.none,
    )
    with pytest.raises(ValueError, match="rollback_reason_invalid"):
        await m5.apply_memory_proposal_action(
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            proposal_id=accepted["proposal_id"],
            action="rollback",
            expected_revision=accepted["revision"],
            reason="   ",
        )
    with pytest.raises(ValueError, match="rollback_reason_invalid"):
        await m5.apply_memory_proposal_action(
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            proposal_id=accepted["proposal_id"],
            action="rollback",
            expected_revision=accepted["revision"],
            reason="x" * 501,
        )
    rollback_reason = "The verified source was superseded by a newer reviewed outcome."
    rolled_back = await m5.apply_memory_proposal_action(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        proposal_id=accepted["proposal_id"],
        action="rollback",
        expected_revision=accepted["revision"],
        reason=rollback_reason,
    )
    assert rolled_back["status"] == MemoryProposalStatus.rolled_back.value
    assert rolled_back["rollback_reason"] == rollback_reason
    assert rolled_back["audit_event_id"]

    async with async_db() as db:
        events = (
            await db.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.session_id == OWNER.session_id,
                    AuditEvent.event_type.in_(
                        (
                            "memory_learning_rejected",
                            "memory_learning_accepted",
                            "memory_learning_rolled_back",
                        )
                    ),
                )
                .order_by(AuditEvent.created_at.asc())
            )
        ).scalars().all()
        assert [event.event_type for event in events] == [
            "memory_learning_rejected",
            "memory_learning_accepted",
            "memory_learning_rolled_back",
        ]
        restored_memory = (
            await db.execute(
                select(Memory).where(Memory.id == accepted["accepted_memory_id"])
            )
        ).scalar_one()
        assert restored_memory.status is MemoryStatus.archived
        rolled_back_proposal = (
            await db.execute(
                select(MemoryProposal).where(
                    MemoryProposal.proposal_id == accepted["proposal_id"]
                )
            )
        ).scalar_one()
        assert rolled_back_proposal.rollback_reason == rollback_reason
        rollback_event = events[-1]
        assert json.loads(rollback_event.details_json)["rollback_reason"] == rollback_reason


@pytest.mark.asyncio
async def test_unknown_effect_records_no_learning(async_db, monkeypatch):
    async with async_db() as db:
        _goal, source, _later = await _goal_and_tasks(db, goal_id="m5-unknown-effect-goal")
        attempt = await _unverified_attempt(db, source)
        run = (
            await db.execute(
                select(WorkflowRunState).where(
                    WorkflowRunState.run_identity == attempt.workflow_run_id
                )
            )
        ).scalar_one()
        run.status = "unknown_external_effect"
        source.status = WorkBoardStatus.blocked
        source.block_kind = "unknown_effect"
        source.block_reason = "External effect requires operator reconciliation."
        source.block_source_status = WorkBoardStatus.running.value
        db.add(run)
        db.add(source)
        await db.flush()
    _patch_m5_sessions(monkeypatch, async_db)

    result = await m5.create_memory_proposal(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        task_id=source.task_id,
        expected_task_revision=source.task_revision,
        attempt_id=attempt.attempt_id,
    )
    assert result["status"] == MemoryProposalStatus.no_learning.value
    assert result["reason_code"] == "unknown_effect"
    assert result["recovery_action"] == "reconcile_effect_before_retry"
    assert result["preview_text"] is None
    assert result["accepted_memory_id"] is None

    async with async_db() as db:
        memories = (await db.execute(select(Memory))).scalars().all()
        assert memories == []
        proposal = (
            await db.execute(
                select(MemoryProposal).where(
                    MemoryProposal.proposal_id == result["proposal_id"]
                )
            )
        ).scalar_one()
        assert proposal.status is MemoryProposalStatus.no_learning
        assert proposal.memory_kind is None
        assert proposal.memory_scope_json is None
        assert proposal.confidence is None
        assert proposal.corrects_memory_id is None


@pytest.mark.asyncio
async def test_candidate_set_api_binds_authenticated_owner_session(monkeypatch):
    operator = make_test_bypass_operator()
    goal = SimpleNamespace(
        id="m5-api-goal",
        owner_principal_id=operator.principal.principal_id,
        owner_session_id=operator.session_id,
    )
    captured: dict[str, object] = {}

    async def fake_get(goal_id: str):
        assert goal_id == goal.id
        return goal

    async def fake_propose(**kwargs):
        captured.update(kwargs)
        return {"receipt_id": "receipt:m5-api-test"}

    monkeypatch.setattr(goals_api.goal_repository, "get", fake_get)
    monkeypatch.setattr(goals_api, "propose_goal_candidate_set", fake_propose)
    request = SimpleNamespace(state=SimpleNamespace(operator=operator))
    body = GoalCandidateSetRequest(
        task_id="task:m5-api-test",
        expected_task_revision=3,
        expected_goal_revision=7,
        candidates=[_candidate(SOURCE_CAPABILITY)],
    )

    result = await goals_api.propose_goal_loop_candidate_set(goal.id, body, request)

    assert result == {"receipt_id": "receipt:m5-api-test"}
    assert captured == {
        "goal_id": goal.id,
        "task_id": body.task_id,
        "candidates": body.candidates,
        "owner_principal_id": operator.principal.principal_id,
        "owner_session_id": operator.session_id,
        "expected_task_revision": body.expected_task_revision,
        "expected_goal_revision": body.expected_goal_revision,
    }


@pytest.mark.asyncio
async def test_conflicting_accepted_memories_do_not_change_later_candidate(async_db, monkeypatch):
    async with async_db() as db:
        goal, source, later = await _goal_and_tasks(
            db, goal_id="m5-conflict-goal"
        )
        second_source = WorkBoardTask(
            task_id="m5-conflict-second-source-task",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            origin_session_id=OWNER.session_id,
            goal_id=goal.id,
            goal_revision=goal.revision,
            title=source.title,
            body=source.body,
            capability_id=source.capability_id,
            typed_input_ref="input:m5-conflict-second-source",
            typed_input_digest=source.typed_input_digest,
            executor_id=source.executor_id,
            idempotency_key="m5-conflict-second-source-key",
            task_revision=1,
            status=WorkBoardStatus.done,
            completed_at=_now(),
        )
        db.add(second_source)
        await db.flush()
        first_attempt = await _verified_attempt(db, source)
        second_attempt = await _verified_attempt(db, second_source)
    _patch_m5_sessions(monkeypatch, async_db)

    for task, attempt, candidate_text in (
        (source, first_attempt, "First verified preference candidate."),
        (second_source, second_attempt, "Second verified preference candidate."),
    ):
        proposal = await m5.create_memory_proposal(
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            task_id=task.task_id,
            expected_task_revision=task.task_revision,
            attempt_id=attempt.attempt_id,
            candidate_text=candidate_text,
            decision_effect=MemoryProposalDecisionEffect.require_operator_confirmation,
        )
        accepted = await m5.apply_memory_proposal_action(
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            proposal_id=proposal["proposal_id"],
            action="accept",
            expected_revision=proposal["revision"],
            expected_preview_text_digest=proposal["proposed_text_digest"],
            expected_task_revision=task.task_revision,
            expected_goal_revision=goal.revision,
            decision_effect=MemoryProposalDecisionEffect.require_operator_confirmation,
            preferred_capability_id=ALTERNATE_CAPABILITY,
        )
        assert accepted["status"] == MemoryProposalStatus.accepted.value

    result = await propose_goal_candidate_set(
        goal_id=goal.id,
        task_id=later.task_id,
        candidates=[_candidate(SOURCE_CAPABILITY), _candidate(ALTERNATE_CAPABILITY)],
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        expected_task_revision=later.task_revision,
        expected_goal_revision=goal.revision,
    )
    assert result["decision"]["decision_status"] == "blocked"
    assert result["decision"]["reason"] == "ambiguous_accepted_memory"
    assert result["decision"]["after_selected_capability_id"] == SOURCE_CAPABILITY
    assert result["selected"]["capability_id"] == SOURCE_CAPABILITY
