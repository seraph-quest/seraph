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
from fastapi import HTTPException
from sqlalchemy import delete
from sqlmodel import select

from src.api import goals as goals_api
from src.api import memory as memory_api
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
    MemoryEdge,
    MemoryStatus,
    MemorySource,
    Session,
    WorkBoardAttempt,
    WorkBoardDecisionReceipt,
    WorkBoardDecisionStatus,
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
from src.memory import repository as memory_repository_module
from src.memory.repository import (
    _memory_export_artifact_payload,
    _memory_export_integrity_payload,
    _m5_receipt_binding_matches,
    _m5_receipt_integrity_matches,
    _recovery_json_hash,
    memory_repository,
)


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


async def _accepted_m5_memory_with_later_receipt(async_db, monkeypatch, *, goal_id: str):
    async with async_db() as db:
        goal, source, later = await _goal_and_tasks(db, goal_id=goal_id)
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
    accepted = await m5.apply_memory_proposal_action(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        proposal_id=proposal["proposal_id"],
        action="accept",
        expected_revision=proposal["revision"],
        expected_preview_text_digest=proposal["proposed_text_digest"],
        expected_task_revision=source.task_revision,
        expected_goal_revision=goal.revision,
        decision_effect=MemoryProposalDecisionEffect.require_operator_confirmation,
        preferred_capability_id=ALTERNATE_CAPABILITY,
    )
    decision = await propose_goal_candidate_set(
        goal_id=goal.id,
        task_id=later.task_id,
        candidates=[_candidate(SOURCE_CAPABILITY), _candidate(ALTERNATE_CAPABILITY)],
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        expected_task_revision=later.task_revision,
        expected_goal_revision=goal.revision,
    )
    assert decision["decision"]["decision_status"] == "changed"
    return goal, source, accepted, decision["decision"]


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
        assert _m5_receipt_integrity_matches(receipt)


@pytest.mark.asyncio
async def test_rotated_m5_key_blocks_learning_with_recovery_reason(async_db, monkeypatch):
    async with async_db() as db:
        goal, source, later = await _goal_and_tasks(db, goal_id="m5-rotated-key-goal")
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
    accepted = await m5.apply_memory_proposal_action(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        proposal_id=proposal["proposal_id"],
        action="accept",
        expected_revision=proposal["revision"],
        expected_preview_text_digest=proposal["proposed_text_digest"],
        expected_task_revision=source.task_revision,
        expected_goal_revision=goal.revision,
        decision_effect=MemoryProposalDecisionEffect.require_operator_confirmation,
        preferred_capability_id=ALTERNATE_CAPABILITY,
    )
    assert accepted["status"] == MemoryProposalStatus.accepted.value

    monkeypatch.setattr(memory_repository_module, "_effect_mac_key", lambda: b"rotated-m5-key")
    later_result = await propose_goal_candidate_set(
        goal_id=goal.id,
        task_id=later.task_id,
        candidates=[_candidate(SOURCE_CAPABILITY), _candidate(ALTERNATE_CAPABILITY)],
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        expected_task_revision=later.task_revision,
        expected_goal_revision=goal.revision,
    )
    assert later_result["decision"]["decision_status"] == "blocked"
    assert later_result["decision"]["reason"] == "accepted_memory_binding_unverifiable"

    async with async_db() as db:
        blocked = (
            await db.execute(
                select(MemoryProposal).where(MemoryProposal.proposal_id == proposal["proposal_id"])
            )
        ).scalar_one()
    assert blocked.status is MemoryProposalStatus.blocked
    assert blocked.reason_code == "accepted_memory_binding_unverifiable"
    assert blocked.recovery_action == "verify_source_and_reaccept"


@pytest.mark.asyncio
async def test_rotated_m5_key_can_reverify_source_and_require_fresh_acceptance(async_db, monkeypatch):
    async with async_db() as db:
        goal, source, later = await _goal_and_tasks(db, goal_id="m5-rotated-key-recovery-goal")
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
    accepted = await m5.apply_memory_proposal_action(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        proposal_id=proposal["proposal_id"],
        action="accept",
        expected_revision=proposal["revision"],
        expected_preview_text_digest=proposal["proposed_text_digest"],
        expected_task_revision=source.task_revision,
        expected_goal_revision=goal.revision,
        decision_effect=MemoryProposalDecisionEffect.require_operator_confirmation,
        preferred_capability_id=ALTERNATE_CAPABILITY,
    )
    monkeypatch.setattr(memory_repository_module, "_effect_mac_key", lambda: b"rotated-m5-key")
    blocked = await propose_goal_candidate_set(
        goal_id=goal.id,
        task_id=later.task_id,
        candidates=[_candidate(SOURCE_CAPABILITY), _candidate(ALTERNATE_CAPABILITY)],
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        expected_task_revision=later.task_revision,
        expected_goal_revision=goal.revision,
    )
    assert blocked["decision"]["decision_status"] == "blocked"

    stored = await m5.list_memory_proposals(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        task_id=source.task_id,
    )
    blocked_proposal = next(row for row in stored if row["proposal_id"] == proposal["proposal_id"])
    assert blocked_proposal["status"] == MemoryProposalStatus.blocked.value

    # A restored/archive recovery uses the same bounded source re-verification
    # path.  Exercise the second advertised recovery code explicitly rather
    # than relying only on the source-baseline variant below.
    async with async_db() as db:
        stored_row = (
            await db.execute(
                select(MemoryProposal).where(MemoryProposal.proposal_id == proposal["proposal_id"])
            )
        ).scalar_one()
        stored_row.recovery_action = "request_verified_proposal_again"
        db.add(stored_row)
        await db.flush()
    stored = await m5.list_memory_proposals(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        task_id=source.task_id,
    )
    blocked_proposal = next(row for row in stored if row["proposal_id"] == proposal["proposal_id"])
    assert blocked_proposal["recovery_action"] == "request_verified_proposal_again"

    reverified = await m5.apply_memory_proposal_action(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        proposal_id=proposal["proposal_id"],
        action="recover",
        expected_revision=blocked_proposal["revision"],
        expected_preview_text_digest=blocked_proposal["preview_text_digest"],
        expected_task_revision=source.task_revision,
        expected_goal_revision=goal.revision,
    )
    assert reverified["proposal_id"] != proposal["proposal_id"]
    assert reverified["recovered_from_proposal_id"] == proposal["proposal_id"]
    assert reverified["status"] == MemoryProposalStatus.proposed.value
    assert reverified["reason_code"] == "verified_source_reverified"
    assert reverified["accepted_memory_id"] is None
    assert reverified["corrects_memory_id"] == accepted["accepted_memory_id"]
    assert reverified["decision_effect"] == MemoryProposalDecisionEffect.none.value
    assert reverified["proposed_text_digest"] == proposal["proposed_text_digest"]
    old_projection = await m5.list_memory_proposals(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        task_id=source.task_id,
    )
    old_projection = next(row for row in old_projection if row["proposal_id"] == proposal["proposal_id"])
    assert old_projection["status"] == MemoryProposalStatus.blocked.value

    replay_recovery = await m5.apply_memory_proposal_action(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        proposal_id=proposal["proposal_id"],
        action="recover",
        expected_revision=old_projection["revision"],
        expected_preview_text_digest=old_projection["preview_text_digest"],
        expected_task_revision=source.task_revision,
        expected_goal_revision=goal.revision,
    )
    assert replay_recovery["proposal_id"] == reverified["proposal_id"]

    reaccepted = await m5.apply_memory_proposal_action(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        proposal_id=reverified["proposal_id"],
        action="accept",
        expected_revision=reverified["revision"],
        expected_preview_text_digest=reverified["proposed_text_digest"],
        expected_task_revision=source.task_revision,
        expected_goal_revision=goal.revision,
        decision_effect=MemoryProposalDecisionEffect.require_operator_confirmation,
        preferred_capability_id=ALTERNATE_CAPABILITY,
    )
    assert reaccepted["status"] == MemoryProposalStatus.accepted.value
    assert reaccepted["accepted_memory_id"] != accepted["accepted_memory_id"]

    async with async_db() as db:
        source_projection = (
            await db.execute(
                select(MemoryProposal).where(MemoryProposal.proposal_id == proposal["proposal_id"])
            )
        ).scalar_one()
        recovered_projection = (
            await db.execute(
                select(MemoryProposal).where(
                    MemoryProposal.proposal_id == reverified["proposal_id"]
                )
            )
        ).scalar_one()
        recovered_memory = (
            await db.execute(select(Memory).where(Memory.id == reaccepted["accepted_memory_id"]))
        ).scalar_one()
        assert m5._m5_authenticated_recovery_link(
            source_projection,
            recovered_projection,
            recovered_memory,
        )
        original_recovery_parent = recovered_projection.recovered_from_proposal_id
        recovered_projection.recovered_from_proposal_id = "forged-recovery-parent"
        assert not m5._m5_authenticated_recovery_link(
            source_projection,
            recovered_projection,
            recovered_memory,
        )
        recovered_projection.recovered_from_proposal_id = original_recovery_parent

    recovered_decision = await propose_goal_candidate_set(
        goal_id=goal.id,
        task_id=later.task_id,
        candidates=[_candidate(SOURCE_CAPABILITY), _candidate(ALTERNATE_CAPABILITY)],
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        expected_task_revision=later.task_revision,
        expected_goal_revision=goal.revision,
    )
    assert recovered_decision["decision"]["decision_status"] == "changed"
    assert recovered_decision["decision"]["after_selected_capability_id"] == ALTERNATE_CAPABILITY
    async with async_db() as db:
        old_memory = (
            await db.execute(select(Memory).where(Memory.id == accepted["accepted_memory_id"]))
        ).scalar_one()
    assert old_memory.status is MemoryStatus.superseded


@pytest.mark.asyncio
async def test_unverified_decision_receipt_is_redacted_then_recomputed(async_db, monkeypatch):
    goal, source, accepted, decision = await _accepted_m5_memory_with_later_receipt(
        async_db,
        monkeypatch,
        goal_id="m5-tampered-later-receipt-goal",
    )
    async with async_db() as db:
        receipt = (
            await db.execute(
                select(WorkBoardDecisionReceipt).where(
                    WorkBoardDecisionReceipt.receipt_id == decision["receipt_id"]
                )
            )
        ).scalar_one()
        receipt.after_selected_capability_id = SOURCE_CAPABILITY
        db.add(receipt)
        await db.flush()

    listed = await m5.list_work_board_decision_receipts(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        task_id=f"{goal.id.removesuffix('-goal')}-later-task",
    )
    quarantined = next(row for row in listed if row["receipt_id"] == decision["receipt_id"])
    assert quarantined["integrity_status"] == "signature_mismatch"
    assert quarantined["decision_status"] == WorkBoardDecisionStatus.blocked.value
    assert quarantined["after_selected_capability_id"] is None
    assert quarantined["evidence_ids"] == []
    assert quarantined["before_action_id"] == ""
    assert quarantined["after_action_id"] == ""
    assert quarantined["confirmed_action_id"] == ""

    refreshed = await propose_goal_candidate_set(
        goal_id=goal.id,
        task_id=f"{goal.id.removesuffix('-goal')}-later-task",
        candidates=[_candidate(SOURCE_CAPABILITY), _candidate(ALTERNATE_CAPABILITY)],
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        expected_task_revision=1,
        expected_goal_revision=goal.revision,
    )
    assert refreshed["decision"]["decision_status"] == "changed"
    assert refreshed["decision"]["after_selected_capability_id"] == ALTERNATE_CAPABILITY
    after = await m5.list_work_board_decision_receipts(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        task_id=f"{goal.id.removesuffix('-goal')}-later-task",
    )
    repaired = next(row for row in after if row["receipt_id"] == decision["receipt_id"])
    assert repaired["integrity_status"] == "verified"
    assert repaired["decision_status"] == WorkBoardDecisionStatus.changed.value


@pytest.mark.asyncio
async def test_authenticated_receipt_listing_quarantines_missing_source_proposal(async_db, monkeypatch):
    _goal, _source, accepted, decision = await _accepted_m5_memory_with_later_receipt(
        async_db,
        monkeypatch,
        goal_id="m5-missing-source-proposal-goal",
    )
    async with async_db() as db:
        await db.execute(
            delete(MemoryProposal).where(
                MemoryProposal.proposal_id == accepted["proposal_id"]
            )
        )
        await db.flush()

    monkeypatch.setattr(
        memory_api,
        "authenticated_memory_context",
        lambda _request: SimpleNamespace(actor=OWNER.principal_id, session_id=OWNER.session_id),
    )
    result = await memory_api.get_memory_task_decisions(
        SimpleNamespace(),
        task_id="m5-missing-source-proposal-later-task",
    )
    quarantined = next(
        row for row in result["receipts"] if row["receipt_id"] == decision["receipt_id"]
    )
    assert quarantined["integrity_status"] == "source_proposal_missing"
    assert quarantined["decision_status"] == WorkBoardDecisionStatus.blocked.value
    assert quarantined["before_action_id"] == ""
    assert quarantined["after_action_id"] == ""
    assert quarantined["evidence_ids"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption,expected_reason",
    [
        ("missing_memory", "accepted_memory_binding_unverifiable"),
        ("changed_content", "accepted_memory_binding_mismatch"),
        ("missing_source", "accepted_memory_binding_mismatch"),
        ("changed_provenance", "accepted_memory_binding_mismatch"),
    ],
)
async def test_corrupt_accepted_memory_blocks_later_decision_with_recovery(
    async_db,
    monkeypatch,
    corruption,
    expected_reason,
):
    goal, _source, accepted, _decision = await _accepted_m5_memory_with_later_receipt(
        async_db,
        monkeypatch,
        goal_id=f"m5-corrupt-canonical-{corruption}-goal",
    )
    later_task_id = f"{goal.id.removesuffix('-goal')}-later-task"
    async with async_db() as db:
        await db.execute(
            delete(WorkBoardDecisionReceipt).where(
                WorkBoardDecisionReceipt.later_task_id == later_task_id
            )
        )
        memory = (
            await db.execute(select(Memory).where(Memory.id == accepted["accepted_memory_id"]))
        ).scalar_one_or_none()
        if corruption == "missing_memory":
            await db.execute(
                delete(MemorySource).where(MemorySource.memory_id == accepted["accepted_memory_id"])
            )
            await db.flush()
            await db.execute(delete(Memory).where(Memory.id == accepted["accepted_memory_id"]))
        elif corruption == "changed_content":
            assert memory is not None
            memory.content = "Canonical content changed without a reviewed correction."
            db.add(memory)
        elif corruption == "missing_source":
            assert memory is not None
            await db.execute(
                delete(MemorySource).where(MemorySource.memory_id == accepted["accepted_memory_id"])
            )
        elif corruption == "changed_provenance":
            assert memory is not None
            memory.metadata_json = json.dumps({"work_board_provenance": {"proposal_id": "forged"}})
            db.add(memory)
        await db.flush()

    blocked = await propose_goal_candidate_set(
        goal_id=goal.id,
        task_id=later_task_id,
        candidates=[_candidate(SOURCE_CAPABILITY), _candidate(ALTERNATE_CAPABILITY)],
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        expected_task_revision=1,
        expected_goal_revision=goal.revision,
    )
    assert blocked["decision"]["decision_status"] == "blocked"
    assert blocked["decision"]["reason"] == expected_reason
    async with async_db() as db:
        proposal = (
            await db.execute(
                select(MemoryProposal).where(
                    MemoryProposal.proposal_id == accepted["proposal_id"]
                )
            )
        ).scalar_one()
        if corruption != "missing_memory":
            quarantined_memory = (
                await db.execute(
                    select(Memory).where(Memory.id == accepted["accepted_memory_id"])
                )
            ).scalar_one()
            assert quarantined_memory.status is MemoryStatus.archived
    assert proposal.status is MemoryProposalStatus.blocked
    assert proposal.reason_code == expected_reason
    assert proposal.recovery_action in {
        "verify_source_and_reaccept",
        "request_verified_proposal_again",
    }


@pytest.mark.asyncio
async def test_receipt_source_proposal_revision_must_not_exceed_linked_proposal(async_db, monkeypatch):
    _goal, _source, accepted, decision = await _accepted_m5_memory_with_later_receipt(
        async_db,
        monkeypatch,
        goal_id="m5-proposal-revision-binding-goal",
    )
    async with async_db() as db:
        proposal = (
            await db.execute(
                select(MemoryProposal).where(
                    MemoryProposal.proposal_id == accepted["proposal_id"]
                )
            )
        ).scalar_one()
        receipt = (
            await db.execute(
                select(WorkBoardDecisionReceipt).where(
                    WorkBoardDecisionReceipt.receipt_id == decision["receipt_id"]
                )
            )
        ).scalar_one()
        receipt.source_proposal_revision = proposal.revision + 1
        receipt.receipt_binding_digest = memory_repository_module._m5_receipt_binding_digest(
            receipt,
            proposal,
        )
        receipt.receipt_integrity_mac = m5._m5_receipt_integrity_mac(receipt)
        await db.flush()
        assert _m5_receipt_integrity_matches(receipt)
        assert not _m5_receipt_binding_matches(receipt, proposal)


def test_non_ascii_receipt_mac_is_classified_without_raising():
    assert m5._m5_receipt_integrity_state(SimpleNamespace(receipt_integrity_mac="é")) == "signature_malformed"


@pytest.mark.asyncio
async def test_tampered_source_baseline_blocks_later_decision(async_db, monkeypatch):
    async with async_db() as db:
        goal, source, later = await _goal_and_tasks(db, goal_id="m5-tampered-baseline-goal")
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
    accepted = await m5.apply_memory_proposal_action(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        proposal_id=proposal["proposal_id"],
        action="accept",
        expected_revision=proposal["revision"],
        expected_preview_text_digest=proposal["proposed_text_digest"],
        expected_task_revision=source.task_revision,
        expected_goal_revision=goal.revision,
        decision_effect=MemoryProposalDecisionEffect.require_operator_confirmation,
        preferred_capability_id=ALTERNATE_CAPABILITY,
    )

    async with async_db() as db:
        baseline = (
            await db.execute(
                select(WorkBoardDecisionReceipt).where(
                    WorkBoardDecisionReceipt.receipt_stage == WorkBoardDecisionReceiptStage.source_baseline,
                    WorkBoardDecisionReceipt.source_proposal_id == proposal["proposal_id"],
                )
            )
        ).scalar_one()
        baseline.before_selected_capability_id = ALTERNATE_CAPABILITY
        db.add(baseline)
        await db.flush()

    later_result = await propose_goal_candidate_set(
        goal_id=goal.id,
        task_id=later.task_id,
        candidates=[_candidate(SOURCE_CAPABILITY), _candidate(ALTERNATE_CAPABILITY)],
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        expected_task_revision=later.task_revision,
        expected_goal_revision=goal.revision,
    )
    assert later_result["decision"]["decision_status"] == "blocked"
    assert later_result["decision"]["reason"] == "source_baseline_integrity_unverifiable"
    assert later_result["decision"]["after_selected_capability_id"] == SOURCE_CAPABILITY

    stored = await m5.list_memory_proposals(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        task_id=source.task_id,
    )
    blocked_proposal = next(row for row in stored if row["proposal_id"] == proposal["proposal_id"])
    assert blocked_proposal["recovery_action"] == "verify_source_and_reaccept"
    recovered = await m5.apply_memory_proposal_action(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        proposal_id=proposal["proposal_id"],
        action="recover",
        expected_revision=blocked_proposal["revision"],
        expected_task_revision=source.task_revision,
        expected_goal_revision=goal.revision,
    )
    assert recovered["proposal_id"] != proposal["proposal_id"]
    assert recovered["recovered_from_proposal_id"] == proposal["proposal_id"]
    assert recovered["status"] == MemoryProposalStatus.proposed.value
    assert recovered["reason_code"] == "verified_source_reverified"
    accepted_again = await m5.apply_memory_proposal_action(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        proposal_id=recovered["proposal_id"],
        action="accept",
        expected_revision=recovered["revision"],
        expected_preview_text_digest=recovered["proposed_text_digest"],
        expected_task_revision=source.task_revision,
        expected_goal_revision=goal.revision,
        decision_effect=MemoryProposalDecisionEffect.require_operator_confirmation,
        preferred_capability_id=ALTERNATE_CAPABILITY,
    )
    assert accepted_again["status"] == MemoryProposalStatus.accepted.value
    assert accepted_again["accepted_memory_id"] != accepted["accepted_memory_id"]
    recovered_decision = await propose_goal_candidate_set(
        goal_id=goal.id,
        task_id=later.task_id,
        candidates=[_candidate(SOURCE_CAPABILITY), _candidate(ALTERNATE_CAPABILITY)],
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        expected_task_revision=later.task_revision,
        expected_goal_revision=goal.revision,
    )
    assert recovered_decision["decision"]["decision_status"] == "changed"
    assert recovered_decision["decision"]["after_selected_capability_id"] == ALTERNATE_CAPABILITY


@pytest.mark.asyncio
async def test_missing_m5_key_returns_recovery_http_error_after_quarantine(async_db, monkeypatch):
    from src.extensions.capability_execution import CapabilityJournalError

    async with async_db() as db:
        goal, source, _later = await _goal_and_tasks(db, goal_id="m5-missing-key-goal")
        attempt = await _verified_attempt(db, source)
    _patch_m5_sessions(monkeypatch, async_db)
    proposal = await m5.create_memory_proposal(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        task_id=source.task_id,
        expected_task_revision=source.task_revision,
        attempt_id=attempt.attempt_id,
        decision_effect=MemoryProposalDecisionEffect.none,
    )
    monkeypatch.setattr(
        memory_api,
        "authenticated_memory_context",
        lambda _request: SimpleNamespace(actor=OWNER.principal_id, session_id=OWNER.session_id),
    )

    def missing_key():
        raise CapabilityJournalError("execution journal MAC key unavailable")

    monkeypatch.setattr(memory_repository_module, "_effect_mac_key", missing_key)
    request = memory_api.MemoryTaskProposalActionRequest(
        action="accept",
        expected_revision=proposal["revision"],
        expected_preview_text_digest=proposal["proposed_text_digest"],
        expected_task_revision=source.task_revision,
        expected_goal_revision=goal.revision,
        decision_effect=MemoryProposalDecisionEffect.none.value,
    )
    with pytest.raises(HTTPException) as captured:
        await memory_api.act_on_memory_task_proposal(SimpleNamespace(), proposal["proposal_id"], request)
    assert captured.value.status_code == 503
    assert captured.value.detail["code"] == "accepted_binding_unavailable"
    assert captured.value.detail["proposal"]["status"] == MemoryProposalStatus.blocked.value
    assert captured.value.detail["proposal"]["recovery_action"] == "verify_source_and_reaccept"


@pytest.mark.asyncio
async def test_missing_m5_key_blocks_and_persists_new_source_and_decision_receipts(async_db, monkeypatch):
    from src.extensions.capability_execution import CapabilityJournalError

    async with async_db() as db:
        goal, source, later = await _goal_and_tasks(db, goal_id="m5-create-receipt-key-goal")
        attempt = await _verified_attempt(db, source)
    _patch_m5_sessions(monkeypatch, async_db)
    monkeypatch.setattr(
        memory_api,
        "authenticated_memory_context",
        lambda _request: SimpleNamespace(actor=OWNER.principal_id, session_id=OWNER.session_id),
    )

    def missing_key():
        raise CapabilityJournalError("execution journal MAC key unavailable")

    monkeypatch.setattr(memory_repository_module, "_effect_mac_key", missing_key)
    with pytest.raises(HTTPException) as captured:
        await memory_api.create_memory_task_proposal(
            SimpleNamespace(),
            memory_api.MemoryTaskProposalRequest(
                task_id=source.task_id,
                expected_task_revision=source.task_revision,
                attempt_id=attempt.attempt_id,
            ),
        )
    assert captured.value.status_code == 503
    assert captured.value.detail["code"] == "accepted_binding_unavailable"
    assert captured.value.detail["proposal"]["status"] == MemoryProposalStatus.blocked.value

    decision = await propose_goal_candidate_set(
        goal_id=goal.id,
        task_id=later.task_id,
        candidates=[_candidate(SOURCE_CAPABILITY), _candidate(ALTERNATE_CAPABILITY)],
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        expected_task_revision=later.task_revision,
        expected_goal_revision=goal.revision,
    )
    assert decision["decision"]["decision_status"] == "blocked"
    assert decision["decision"]["reason"] == "decision_receipt_signing_unavailable"

    receipts = await m5.list_work_board_decision_receipts(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
    )
    assert receipts
    for receipt in receipts:
        assert receipt["integrity_status"] == "signature_missing"
        assert receipt["decision_status"] == WorkBoardDecisionStatus.blocked.value
        assert receipt["before_action_id"] == ""
        assert receipt["after_action_id"] == ""
        assert receipt["evidence_ids"] == []


@pytest.mark.asyncio
async def test_rollback_completes_when_receipt_signing_key_is_unavailable(async_db, monkeypatch):
    from src.extensions.capability_execution import CapabilityJournalError

    goal, source, accepted, decision = await _accepted_m5_memory_with_later_receipt(
        async_db,
        monkeypatch,
        goal_id="m5-rollback-without-key-goal",
    )

    def missing_key():
        raise CapabilityJournalError("execution journal MAC key unavailable")

    monkeypatch.setattr(memory_repository_module, "_effect_mac_key", missing_key)
    result = await m5.apply_memory_proposal_action(
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        proposal_id=accepted["proposal_id"],
        action="rollback",
        expected_revision=accepted["revision"],
        expected_task_revision=source.task_revision,
        expected_goal_revision=goal.revision,
        reason="Withdraw the correction after the signing key became unavailable.",
    )

    assert result["status"] == MemoryProposalStatus.rolled_back.value
    async with async_db() as db:
        memory = (
            await db.execute(select(Memory).where(Memory.id == accepted["accepted_memory_id"]))
        ).scalar_one()
        receipt = (
            await db.execute(
                select(WorkBoardDecisionReceipt).where(
                    WorkBoardDecisionReceipt.receipt_id == decision["receipt_id"]
                )
            )
        ).scalar_one()
    assert memory.status is MemoryStatus.archived
    assert receipt.decision_status.value == "blocked"
    assert receipt.reason == "memory_rolled_back"
    assert receipt.receipt_integrity_mac is None


@pytest.mark.asyncio
async def test_memory_deletion_completes_when_receipt_signing_key_is_unavailable(async_db, monkeypatch):
    from src.extensions.capability_execution import CapabilityJournalError

    _goal, _source, accepted, decision = await _accepted_m5_memory_with_later_receipt(
        async_db,
        monkeypatch,
        goal_id="m5-delete-without-key-goal",
    )

    def missing_key():
        raise CapabilityJournalError("execution journal MAC key unavailable")

    monkeypatch.setattr(memory_repository_module, "_effect_mac_key", missing_key)
    deleted = await memory_repository.mark_memory_tombstoned(
        accepted["accepted_memory_id"],
        actor=OWNER.principal_id,
        reason="Operator requested verified-memory deletion.",
    )

    assert deleted.memory.status is MemoryStatus.archived
    async with async_db() as db:
        proposal = (
            await db.execute(
                select(MemoryProposal).where(MemoryProposal.proposal_id == accepted["proposal_id"])
            )
        ).scalar_one()
        receipt = (
            await db.execute(
                select(WorkBoardDecisionReceipt).where(
                    WorkBoardDecisionReceipt.receipt_id == decision["receipt_id"]
                )
            )
        ).scalar_one()
    assert proposal.preview_text is None
    assert proposal.privacy_state.value == "redacted"
    assert receipt.decision_status.value == "blocked"
    assert receipt.reason == "memory_deleted_or_export_redacted"
    assert receipt.receipt_integrity_mac is None


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
async def test_m5_export_restore_preserves_recovery_generation_for_later_decision(
    async_db, monkeypatch
):
    """A recovered accepted proposal survives restore beside its blocked parent."""
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
        goal, source, later = await _goal_and_tasks(
            db, goal_id="m5-recovery-roundtrip-goal"
        )
        attempt = await _verified_attempt(db, source)
    _patch_m5_sessions(monkeypatch, async_db)

    proposal = await m5.create_memory_proposal(
        owner_principal_id=operator.principal.principal_id,
        owner_session_id=operator.session_id,
        task_id=source.task_id,
        expected_task_revision=source.task_revision,
        attempt_id=attempt.attempt_id,
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

    # Corrupt the original source-baseline receipt so the accepted proposal
    # enters the supported source re-verification path.
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
        baseline.before_selected_capability_id = ALTERNATE_CAPABILITY
        db.add(baseline)
        await db.flush()

    blocked_decision = await propose_goal_candidate_set(
        goal_id=goal.id,
        task_id=later.task_id,
        candidates=[_candidate(SOURCE_CAPABILITY), _candidate(ALTERNATE_CAPABILITY)],
        owner_principal_id=operator.principal.principal_id,
        owner_session_id=operator.session_id,
        expected_task_revision=later.task_revision,
        expected_goal_revision=goal.revision,
    )
    assert blocked_decision["decision"]["decision_status"] == "blocked"
    assert blocked_decision["decision"]["reason"] == "source_baseline_integrity_unverifiable"

    stored = await m5.list_memory_proposals(
        owner_principal_id=operator.principal.principal_id,
        owner_session_id=operator.session_id,
        task_id=source.task_id,
    )
    blocked_parent = next(
        row for row in stored if row["proposal_id"] == proposal["proposal_id"]
    )
    recovered = await m5.apply_memory_proposal_action(
        owner_principal_id=operator.principal.principal_id,
        owner_session_id=operator.session_id,
        proposal_id=proposal["proposal_id"],
        action="recover",
        expected_revision=blocked_parent["revision"],
        expected_task_revision=source.task_revision,
        expected_goal_revision=goal.revision,
    )
    accepted_recovery = await m5.apply_memory_proposal_action(
        owner_principal_id=operator.principal.principal_id,
        owner_session_id=operator.session_id,
        proposal_id=recovered["proposal_id"],
        action="accept",
        expected_revision=recovered["revision"],
        expected_preview_text_digest=recovered["proposed_text_digest"],
        expected_task_revision=source.task_revision,
        expected_goal_revision=goal.revision,
        decision_effect=MemoryProposalDecisionEffect.require_operator_confirmation,
        preferred_capability_id=ALTERNATE_CAPABILITY,
    )
    assert accepted_recovery["status"] == MemoryProposalStatus.accepted.value
    assert accepted_recovery["accepted_memory_id"] != accepted["accepted_memory_id"]

    with _runtime_operator(operator):
        archive = await memory_repository.export_canonical_memory_state(
            actor=operator.principal.principal_id,
            owner_session_id=operator.session_id,
            authenticated_session_id=operator.session_id,
        )
    proposal_archives = {
        item["proposal_id"]: item for item in archive["m5_proposals"]
    }
    assert proposal_archives[proposal["proposal_id"]]["status"] == MemoryProposalStatus.blocked.value
    assert proposal_archives[recovered["proposal_id"]]["status"] == MemoryProposalStatus.accepted.value
    assert (
        proposal_archives[proposal["proposal_id"]]["preview_text_digest"]
        == proposal_archives[recovered["proposal_id"]]["preview_text_digest"]
    )

    memory_ids = [item["id"] for item in archive["memories"]]
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
        if memory_ids:
            await db.execute(
                delete(MemoryEdge).where(
                    MemoryEdge.from_memory_id.in_(memory_ids)
                    | MemoryEdge.to_memory_id.in_(memory_ids)
                )
            )
            await db.execute(
                delete(MemorySource).where(MemorySource.memory_id.in_(memory_ids))
            )
            await db.flush()
            await db.execute(delete(Memory).where(Memory.id.in_(memory_ids)))
            await db.flush()

    with _runtime_operator(operator):
        restored = await memory_repository.restore_canonical_memory_state(
            archive,
            actor=operator.principal.principal_id,
            owner_session_id=operator.session_id,
            authenticated_session_id=operator.session_id,
        )
    assert set(restored["m5_restored_proposal_ids"]) >= {
        proposal["proposal_id"],
        recovered["proposal_id"],
    }

    async with async_db() as db:
        restored_parent = (
            await db.execute(
                select(MemoryProposal).where(
                    MemoryProposal.proposal_id == proposal["proposal_id"]
                )
            )
        ).scalar_one()
        restored_child = (
            await db.execute(
                select(MemoryProposal).where(
                    MemoryProposal.proposal_id == recovered["proposal_id"]
                )
            )
        ).scalar_one()
        assert restored_parent.status is MemoryProposalStatus.blocked
        assert restored_child.status is MemoryProposalStatus.accepted
        assert restored_child.accepted_memory_id == accepted_recovery["accepted_memory_id"]
        after_restore = WorkBoardTask(
            task_id="m5-recovery-roundtrip-after-restore-task",
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
            origin_session_id=operator.session_id,
            goal_id=goal.id,
            goal_revision=goal.revision,
            title=source.title,
            body=source.body,
            capability_id=SOURCE_CAPABILITY,
            typed_input_ref="input:m5-recovery-roundtrip-after-restore",
            typed_input_digest=source.typed_input_digest,
            executor_id=f"seraph-work-board:{SOURCE_CAPABILITY}",
            idempotency_key="m5-recovery-roundtrip-after-restore-key",
            task_revision=1,
            status=WorkBoardStatus.todo,
        )
        db.add(after_restore)
        await db.flush()

    later_result = await propose_goal_candidate_set(
        goal_id=goal.id,
        task_id="m5-recovery-roundtrip-after-restore-task",
        candidates=[_candidate(SOURCE_CAPABILITY), _candidate(ALTERNATE_CAPABILITY)],
        owner_principal_id=operator.principal.principal_id,
        owner_session_id=operator.session_id,
        expected_task_revision=1,
        expected_goal_revision=goal.revision,
    )
    assert later_result["decision"]["decision_status"] == "changed"
    assert later_result["decision"]["after_selected_capability_id"] == ALTERNATE_CAPABILITY
    assert later_result["decision"]["accepted_memory_id"] == accepted_recovery["accepted_memory_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper_canonical_provenance",
    [False, True],
    ids=["proposal-only", "proposal-and-memory-provenance"],
)
async def test_m5_restore_blocks_rehashed_forged_selection_binding(
    async_db, monkeypatch, tamper_canonical_provenance
):
    """A public archive digest cannot change an operator-accepted choice."""

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
        goal, source, later = await _goal_and_tasks(db, goal_id="m5-forged-binding-goal")
        attempt = await _verified_attempt(db, source)
    _patch_m5_sessions(monkeypatch, async_db)

    proposal = await m5.create_memory_proposal(
        owner_principal_id=operator.principal.principal_id,
        owner_session_id=operator.session_id,
        task_id=source.task_id,
        expected_task_revision=source.task_revision,
        attempt_id=attempt.attempt_id,
        candidate_text="The verified source was accepted without changing the later choice.",
        decision_effect=MemoryProposalDecisionEffect.none,
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
        decision_effect=MemoryProposalDecisionEffect.none,
    )

    with _runtime_operator(operator):
        archive = await memory_repository.export_canonical_memory_state(
            actor=operator.principal.principal_id,
            owner_session_id=operator.session_id,
            authenticated_session_id=operator.session_id,
        )

    tampered = json.loads(json.dumps(archive))
    forged_proposal = next(
        item for item in tampered["m5_proposals"] if item["proposal_id"] == proposal["proposal_id"]
    )
    forged_proposal["decision_effect"] = MemoryProposalDecisionEffect.require_operator_confirmation.value
    forged_proposal["memory_scope"]["preferred_capability_id"] = ALTERNATE_CAPABILITY
    forged_proposal["memory_scope"]["preferred_capability_version"] = "1"
    forged_proposal["memory_scope"]["candidate_capability_ids"] = [ALTERNATE_CAPABILITY]
    forged_proposal["source_task_id"] = "forged-source-task"
    forged_proposal["source_attempt_id"] = "forged-source-attempt"
    forged_proposal["workflow_run_id"] = "forged-workflow-run"
    forged_proposal["evidence_digest"] = _recovery_json_hash({"forged": "evidence"})
    forged_proposal["readback_digest"] = _recovery_json_hash({"forged": "readback"})
    forged_proposal["artifact_digest"] = _recovery_json_hash({"forged": "artifact"})
    forged_proposal["source_evidence_ids"] = ["forged-evidence"]
    if tamper_canonical_provenance:
        forged_memory = next(
            item for item in tampered["memories"] if item["id"] == accepted["accepted_memory_id"]
        )
        forged_provenance = forged_memory["metadata"]["work_board_provenance"]
        forged_provenance["decision_effect"] = forged_proposal["decision_effect"]
        forged_provenance["memory_scope"]["preferred_capability_id"] = ALTERNATE_CAPABILITY
        forged_provenance["memory_scope"]["preferred_capability_version"] = "1"
        forged_provenance["memory_scope"]["candidate_capability_ids"] = [ALTERNATE_CAPABILITY]
        forged_source = forged_provenance["verified_source_binding"]
        forged_source["source_task_id"] = forged_proposal["source_task_id"]
        forged_source["source_attempt_id"] = forged_proposal["source_attempt_id"]
        forged_source["workflow_run_id"] = forged_proposal["workflow_run_id"]
        forged_source["evidence_digest"] = forged_proposal["evidence_digest"]
        forged_source["readback_digest"] = forged_proposal["readback_digest"]
        forged_source["artifact_digest"] = forged_proposal["artifact_digest"]
        forged_source["source_evidence_ids"] = list(forged_proposal["source_evidence_ids"])
    tampered["export_hash"] = _recovery_json_hash(_memory_export_integrity_payload(tampered))
    tampered["artifact_path"] = f"artifacts/memory-recovery/export-{tampered['export_hash'][:24]}.json"
    tampered["artifact_sha256"] = _recovery_json_hash(_memory_export_artifact_payload(tampered))

    async with async_db() as db:
        await db.execute(
            delete(WorkBoardDecisionReceipt).where(
                WorkBoardDecisionReceipt.owner_session_id == operator.session_id
            )
        )
        await db.execute(
            delete(MemoryProposal).where(MemoryProposal.owner_session_id == operator.session_id)
        )
        await db.execute(
            delete(MemorySource).where(MemorySource.memory_id == accepted["accepted_memory_id"])
        )
        await db.execute(delete(Memory).where(Memory.id == accepted["accepted_memory_id"]))
        await db.flush()

    with _runtime_operator(operator):
        restored = await memory_repository.restore_canonical_memory_state(
            tampered,
            actor=operator.principal.principal_id,
            owner_session_id=operator.session_id,
            authenticated_session_id=operator.session_id,
        )
    assert restored["m5_blocked_proposal_ids"] == [proposal["proposal_id"]]
    assert restored["m5_restored_proposal_ids"] == [proposal["proposal_id"]]
    assert restored["m5_blocked_receipt_ids"] == [tampered["m5_decision_receipts"][0]["receipt_id"]]

    async with async_db() as db:
        restored_proposal = (
            await db.execute(
                select(MemoryProposal).where(MemoryProposal.proposal_id == proposal["proposal_id"])
            )
        ).scalar_one()
    assert restored_proposal.status is MemoryProposalStatus.blocked
    assert restored_proposal.reason_code == "source_baseline_binding_mismatch"

    decision = await propose_goal_candidate_set(
        goal_id=goal.id,
        task_id=later.task_id,
        candidates=[_candidate(SOURCE_CAPABILITY), _candidate(ALTERNATE_CAPABILITY)],
        owner_principal_id=operator.principal.principal_id,
        owner_session_id=operator.session_id,
        expected_task_revision=later.task_revision,
        expected_goal_revision=goal.revision,
    )
    assert decision["decision"]["after_selected_capability_id"] == SOURCE_CAPABILITY
    assert ALTERNATE_CAPABILITY not in decision["decision"]["evidence_ids"]


@pytest.mark.asyncio
async def test_rehashed_correction_target_cannot_change_acceptance_or_rollback(async_db, monkeypatch):
    """Correction and rollback targets remain covered by the keyed binding."""

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
        goal, source, later = await _goal_and_tasks(db, goal_id="m5-correction-target-binding-goal")
        attempt = await _verified_attempt(db, source)
        prior = Memory(
            id="m5-correction-target",
            content="Prior reviewed preference",
            kind=MemoryKind.fact,
            status=MemoryStatus.active,
            source_session_id=OWNER.session_id,
            scope_key="m5-correction-target-scope",
            metadata_json="{}",
        )
        db.add(prior)
        await db.flush()
    _patch_m5_sessions(monkeypatch, async_db)

    proposal = await m5.create_memory_proposal(
        owner_principal_id=operator.principal.principal_id,
        owner_session_id=operator.session_id,
        task_id=source.task_id,
        expected_task_revision=source.task_revision,
        attempt_id=attempt.attempt_id,
        candidate_text="A verified correction with a fixed rollback target.",
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
        corrects_memory_id=prior.id,
    )
    accepted_memory_id = accepted["accepted_memory_id"]

    async with async_db() as db:
        memory = (await db.execute(select(Memory).where(Memory.id == accepted_memory_id))).scalar_one()
        original_metadata_json = memory.metadata_json
        metadata = json.loads(memory.metadata_json)
        metadata["work_board_provenance"]["corrects_memory_id"] = "m5-forged-correction-target"
        metadata["work_board_provenance"]["corrected_memory_previous_status"] = "active"
        memory.metadata_json = json.dumps(metadata, sort_keys=True)
        db.add(memory)
        await db.flush()

    with pytest.raises(ValueError, match="correction_target_binding_mismatch"):
        await m5.apply_memory_proposal_action(
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
            proposal_id=proposal["proposal_id"],
            action="rollback",
            expected_revision=accepted["revision"],
            expected_task_revision=source.task_revision,
            expected_goal_revision=goal.revision,
            reason="Reject forged correction target.",
        )
    async with async_db() as db:
        memory = (await db.execute(select(Memory).where(Memory.id == accepted_memory_id))).scalar_one()
        assert memory.status is MemoryStatus.active
        memory.metadata_json = original_metadata_json
        db.add(memory)
        await db.flush()

    with _runtime_operator(operator):
        archive = await memory_repository.export_canonical_memory_state(
            actor=operator.principal.principal_id,
            owner_session_id=operator.session_id,
            authenticated_session_id=operator.session_id,
        )
    tampered = json.loads(json.dumps(archive))
    accepted_record = next(item for item in tampered["memories"] if item["id"] == accepted_memory_id)
    accepted_provenance = accepted_record["metadata"]["work_board_provenance"]
    accepted_provenance["corrects_memory_id"] = "m5-forged-correction-target"
    accepted_provenance["corrected_memory_previous_status"] = "active"
    tampered["export_hash"] = _recovery_json_hash(_memory_export_integrity_payload(tampered))
    tampered["artifact_path"] = f"artifacts/memory-recovery/export-{tampered['export_hash'][:24]}.json"
    tampered["artifact_sha256"] = _recovery_json_hash(_memory_export_artifact_payload(tampered))

    async with async_db() as db:
        await db.execute(
            delete(WorkBoardDecisionReceipt).where(
                WorkBoardDecisionReceipt.owner_session_id == operator.session_id
            )
        )
        await db.execute(
            delete(MemoryProposal).where(MemoryProposal.owner_session_id == operator.session_id)
        )
        await db.execute(delete(MemoryEdge).where(MemoryEdge.from_memory_id == accepted_memory_id))
        await db.execute(delete(MemorySource).where(MemorySource.memory_id == accepted_memory_id))
        await db.execute(delete(Memory).where(Memory.id == accepted_memory_id))
        await db.flush()

    with _runtime_operator(operator):
        restored = await memory_repository.restore_canonical_memory_state(
            tampered,
            actor=operator.principal.principal_id,
            owner_session_id=operator.session_id,
            authenticated_session_id=operator.session_id,
        )
    assert restored["m5_blocked_proposal_ids"] == [proposal["proposal_id"]]
    async with async_db() as db:
        restored_proposal = (
            await db.execute(select(MemoryProposal).where(MemoryProposal.proposal_id == proposal["proposal_id"]))
        ).scalar_one()
        assert restored_proposal.status is MemoryProposalStatus.blocked
        assert restored_proposal.reason_code == "accepted_memory_binding_mismatch"
        assert restored_proposal.recovery_action == "request_verified_proposal_again"

    decision = await propose_goal_candidate_set(
        goal_id=goal.id,
        task_id=later.task_id,
        candidates=[_candidate(SOURCE_CAPABILITY), _candidate(ALTERNATE_CAPABILITY)],
        owner_principal_id=operator.principal.principal_id,
        owner_session_id=operator.session_id,
        expected_task_revision=later.task_revision,
        expected_goal_revision=goal.revision,
    )
    assert decision["decision"]["after_selected_capability_id"] == SOURCE_CAPABILITY
    assert ALTERNATE_CAPABILITY not in decision["decision"]["evidence_ids"]


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
