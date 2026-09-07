"""Focused receipts and revision safety for the goal-conditioned v1 slice."""

from src.goals.contracts import (
    GoalCandidateAction,
    GoalCandidateRequest,
    GoalExecutionResult,
    GoalSuccessCriterion,
)
from src.goals.repository import goal_repository
from src.goals.repository import deserialize_success_criterion, serialize_success_criterion
from src.guardian.goal_conditioned_loop import (
    build_goal_candidate_decision,
    dispatch_goal_candidate,
    list_goal_loop_receipts,
    propose_goal_candidate,
)


def test_malformed_stored_criterion_can_never_act():
    from src.db.models import Goal

    goal = Goal(id="legacy", title="Legacy", success_criterion_json="not-json")
    decision = build_goal_candidate_decision(
        goal,
        GoalCandidateRequest(capability_id="workflow.goal-snapshot-to-file", evidence_refs=["obs-1"]),
    )
    assert decision.action is GoalCandidateAction.clarify
    assert decision.reason == "missing_success_criterion"


def test_repository_and_strategist_seams_share_typed_criterion():
    from src.agent.strategist import build_goal_conditioned_candidate
    from src.db.models import Goal

    criterion = GoalSuccessCriterion(
        criterion_id="file-present",
        description="A readable file is present",
        verifier_kind="artifact_readback",
    )
    goal = Goal(id="goal-1", title="Write a brief")
    goal.success_criterion_json = serialize_success_criterion(criterion)

    assert deserialize_success_criterion(goal) == criterion
    decision = build_goal_conditioned_candidate(
        goal,
        GoalCandidateRequest(
            capability_id="workflow.goal-snapshot-to-file",
            evidence_refs=["observation-1"],
        ),
    )
    assert decision.action is GoalCandidateAction.act
    assert decision.goal_revision == 1


def test_goals_api_registers_inspection_and_candidate_routes(app):
    paths = {route.path for route in app.routes}
    assert "/api/goals/{goal_id}/loop" in paths
    assert "/api/goals/{goal_id}/candidates" in paths


async def test_missing_criterion_clarifies_and_records_no_learning(async_db):
    goal = await goal_repository.create("Write a brief")

    decision = await propose_goal_candidate(
        goal.id,
        GoalCandidateRequest(capability_id="workflow.goal-snapshot-to-file"),
    )

    assert decision.action is GoalCandidateAction.clarify
    assert decision.reason == "missing_success_criterion"
    receipts = await list_goal_loop_receipts(goal.id)
    assert {item["event_type"] for item in receipts} == {
        "goal_loop_candidate",
        "goal_loop_no_learning",
    }


async def test_duplicate_evidence_produces_one_candidate_receipt(async_db):
    goal = await goal_repository.create(
        "Write a brief",
        success_criterion=GoalSuccessCriterion(
            description="A readable file is present",
            verifier_kind="artifact_readback",
        ),
    )
    request = GoalCandidateRequest(
        capability_id="workflow.goal-snapshot-to-file",
        evidence_refs=["obs-1", "obs-1"],
    )

    first = await propose_goal_candidate(goal.id, request)
    second = await propose_goal_candidate(
        goal.id,
        request.model_copy(update={"evidence_refs": ["obs-1"]}),
    )

    assert first.candidate_id == second.candidate_id
    receipts = await list_goal_loop_receipts(goal.id)
    assert sum(item["event_type"] == "goal_loop_candidate" for item in receipts) == 1


async def test_stale_revision_and_pause_block_adapter(async_db):
    goal = await goal_repository.create(
        "Write a brief",
        success_criterion=GoalSuccessCriterion(
            description="A readable file is present",
            verifier_kind="artifact_readback",
        ),
    )
    candidate = await propose_goal_candidate(
        goal.id,
        GoalCandidateRequest(capability_id="workflow.goal-snapshot-to-file", evidence_refs=["obs-1"]),
    )
    await goal_repository.update(goal.id, title="Revised", expected_revision=1)

    calls = 0

    async def adapter(**_kwargs):
        nonlocal calls
        calls += 1
        return GoalExecutionResult(
            verification="passed",
            artifact_ref="reports/brief.md",
            evidence_refs=["readback-1"],
        )

    stale = await dispatch_goal_candidate(candidate, adapter=adapter)
    assert stale.execution_status == "blocked"
    assert stale.reason == "stale_goal_revision"
    assert calls == 0

    paused_goal = await goal_repository.create(
        "Another brief",
        success_criterion=GoalSuccessCriterion(
            description="A readable file is present",
            verifier_kind="artifact_readback",
        ),
    )
    paused_candidate = await propose_goal_candidate(
        paused_goal.id,
        GoalCandidateRequest(capability_id="workflow.goal-snapshot-to-file", evidence_refs=["obs-2"]),
    )
    await goal_repository.update(paused_goal.id, status="paused", expected_revision=1)
    paused = await dispatch_goal_candidate(paused_candidate, adapter=adapter)
    assert paused.execution_status == "blocked"
    assert paused.reason == "goal_not_active"
    assert calls == 0


async def test_success_requires_readback_and_receipt_redacts_inputs(async_db):
    goal = await goal_repository.create(
        "Write a brief",
        success_criterion=GoalSuccessCriterion(
            description="A readable file is present",
            verifier_kind="artifact_readback",
        ),
    )
    candidate = await propose_goal_candidate(
        goal.id,
        GoalCandidateRequest(
            capability_id="workflow.goal-snapshot-to-file",
            evidence_refs=["obs-1"],
            inputs={"api_key": "do-not-store", "file_path": "reports/brief.md"},
        ),
    )

    async def adapter(**_kwargs):
        # A claimed pass without artifact readback must stay unknown.
        return {"verification": "passed", "evidence_refs": ["readback-1"]}

    outcome = await dispatch_goal_candidate(candidate, adapter=adapter)
    assert outcome.verification == "unknown"
    assert outcome.learning == "no_learning"
    receipts = await list_goal_loop_receipts(goal.id)
    candidate_receipt = next(item for item in receipts if item["event_type"] == "goal_loop_candidate")
    assert candidate_receipt["input_keys"] == ["api_key", "file_path"]
    assert "do-not-store" not in str(candidate_receipt)
