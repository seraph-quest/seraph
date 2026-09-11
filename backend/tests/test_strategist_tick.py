"""Tests for strategist tick runtime audit coverage."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.audit.repository import audit_repository
from src.guardian.state import GuardianState, GuardianStateConfidence
from src.guardian.goal_snapshot_to_file import GoalSnapshotToFileResult
from src.guardian.web_brief_to_file import WebBriefToFileResult
from src.goals.contracts import CriterionVerifierKind, GoalAdmissionBudget, GoalSuccessCriterion
from src.db.models import Goal
from src.guardian.world_model import GuardianWorldModel
from src.observer.context import CurrentContext
from src.observer.user_state import DeliveryDecision
from src.scheduler.jobs.strategist_tick import (
    _occurrence_identity,
    _run_opted_in_goal_web_brief,
    _run_opted_in_goal_snapshot,
    _goal_budget_admission,
    _goal_work_must_not_continue,
    run_strategist_tick,
)
from src.workflows.job_runtime import durable_job_repository


class _RecordingDurableJobs:
    def __init__(self):
        self.effects = []

    async def record_effect(self, job_id, **kwargs):
        self.effects.append((job_id, kwargs))
        return {"status": kwargs["status"]}


@pytest.mark.asyncio
async def test_goal_budget_missing_expired_and_valid_admission_are_visible():
    missing = Goal(
        id="budget-missing",
        title="Missing budget",
        proactive_enabled=True,
        owner_principal_id="operator:a",
        owner_session_id="operator-session:a",
    )
    missing_receipt = await _goal_budget_admission(
        missing,
        capability_id="workflow.goal-snapshot-to-file",
    )
    assert missing_receipt["status"] == "deferred"
    assert missing_receipt["reason"] == "goal_budget_missing_reviewed_grant"
    assert missing_receipt["proposal_only"] is True

    now = datetime.now(timezone.utc)
    expired = Goal(
        id="budget-expired",
        title="Expired budget",
        proactive_enabled=True,
        owner_principal_id="operator:a",
        owner_session_id="operator-session:a",
        admission_budget_json=GoalAdmissionBudget(
            reviewed_grant=True,
            grant_id="expired-grant",
            period_started_at=now - timedelta(hours=2),
            period_expires_at=now - timedelta(hours=1),
        ).model_dump_json(),
    )
    expired_receipt = await _goal_budget_admission(
        expired,
        capability_id="workflow.goal-snapshot-to-file",
    )
    assert expired_receipt["status"] == "deferred"
    assert expired_receipt["reason"] == "goal_budget_period_expired"

    valid = Goal(
        id="budget-valid",
        title="Valid budget",
        proactive_enabled=True,
        owner_principal_id="operator:a",
        owner_session_id="operator-session:a",
        admission_budget_json=GoalAdmissionBudget(
            reviewed_grant=True,
            grant_id="valid-grant",
            period_started_at=now - timedelta(minutes=1),
            period_expires_at=now + timedelta(hours=1),
            max_attempts=2,
            max_runtime_seconds=45,
            notifications_per_day=1,
        ).model_dump_json(),
    )
    with patch.object(
        durable_job_repository,
        "list_jobs",
        new=AsyncMock(return_value=[]),
    ):
        valid_receipt = await _goal_budget_admission(
            valid,
            capability_id="workflow.goal-snapshot-to-file",
        )
    assert valid_receipt["status"] == "admitted"
    assert valid_receipt["budget"].max_attempts == 2


@pytest.mark.asyncio
async def test_goal_budget_without_owner_is_blocked_before_delivery_fallback():
    receipt = await _goal_budget_admission(
        Goal(id="unbound-goal", title="Unbound goal", proactive_enabled=True),
        capability_id="workflow.goal-snapshot-to-file",
    )

    assert receipt["status"] == "blocked"
    assert receipt["reason"] == "goal_owner_binding_missing"
    assert receipt["notification_owner_principal_id"] is None
    assert receipt["notification_operator_session_id"] is None
    assert _goal_work_must_not_continue(receipt) is True


def _make_context(**overrides) -> CurrentContext:
    defaults = dict(
        time_of_day="afternoon",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="2 active goals",
    )
    defaults.update(overrides)
    return CurrentContext(**defaults)


def _make_guardian_state() -> GuardianState:
    return GuardianState(
        soul_context="# Soul\n\n## Goals\n- Ship guardian state",
        observer_context=_make_context(),
        world_model=GuardianWorldModel(
            current_focus="2 active goals",
            focus_source="observer_goals",
            active_commitments=("2 active goals",),
            open_loops_or_pressure=("Attention budget is nearly exhausted",),
            focus_alignment="medium",
            intervention_receptivity="low",
        ),
        memory_context="- [goal] Ship guardian state",
        episodic_memory_context="",
        current_session_history="",
        recent_sessions_summary='- Prior roadmap: assistant said "Land guardian-state synthesis next"',
        recent_intervention_feedback="- advisory delivered, feedback=helpful: Stretch and refocus.",
        confidence=GuardianStateConfidence(
            overall="grounded",
            observer="good",
            world_model="grounded",
            memory="grounded",
            current_session="not_requested",
            recent_sessions="grounded",
        ),
    )


@pytest.mark.asyncio
async def test_proactive_goal_snapshot_skips_without_explicit_opt_in():
    jobs = _RecordingDurableJobs()
    with (
        patch(
            "src.scheduler.jobs.strategist_tick.goal_repository.list_goals",
            new=AsyncMock(return_value=[]),
        ),
        patch("src.scheduler.jobs.strategist_tick.durable_job_repository", jobs),
    ):
        receipt = await _run_opted_in_goal_snapshot(
            parent_job_id="parent-1",
            parent_fencing_token=2,
        )

    assert receipt == {"status": "skipped", "reason": "no_eligible_proactive_goal"}
    # No child admission occurred, so the parent records a verified no-op
    # rather than an unresolved external effect.
    assert jobs.effects[0][1]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_proactive_goal_snapshot_runs_one_enabled_goal_through_existing_service():
    criterion = GoalSuccessCriterion(
        description="Create a verified snapshot",
        verifier_kind=CriterionVerifierKind.artifact_readback,
        evidence_refs=["goal:operator-consent"],
    )
    goal = SimpleNamespace(
        id="goal-1",
        revision=3,
        proactive_enabled=True,
        success_criterion_json=criterion.model_dump_json(),
        due_date=datetime.now(timezone.utc),
        sort_order=0,
    )
    jobs = _RecordingDurableJobs()
    service_result = GoalSnapshotToFileResult(
        goal_id="goal-1",
        goal_revision=3,
        file_path="goal-snapshots/goal-1.md",
        execution_status="succeeded",
        verification="passed",
        learning="no_learning",
        job_id="child-1",
        artifact_ref="artifact-1",
        reason="goal_snapshot_executed_and_verified",
    )
    service = MagicMock()
    service.run = AsyncMock(return_value=service_result)
    with (
        patch(
            "src.scheduler.jobs.strategist_tick.goal_repository.list_goals",
            new=AsyncMock(return_value=[goal]),
        ),
        patch("src.scheduler.jobs.strategist_tick.durable_job_repository", jobs),
        patch("src.scheduler.jobs.strategist_tick.GoalSnapshotToFileService", return_value=service),
    ):
        receipt = await _run_opted_in_goal_snapshot(
            parent_job_id="parent-1",
            parent_fencing_token=2,
        )

    assert receipt["status"] == "succeeded"
    assert receipt["verification"] == "passed"
    assert jobs.effects[0][1]["status"] == "succeeded"
    request = service.run.await_args.args[0]
    assert request.goal_id == "goal-1"
    assert request.goal_revision == 3
    assert request.owner_principal_id == "service:goal-snapshot"
    assert request.parent_job_id == "parent-1"
    assert request.parent_fencing_token == 2
    assert request.session_id == "goal-snapshot:scheduler:goal-1:3"


@pytest.mark.asyncio
async def test_proactive_web_brief_requires_explicit_target_and_reuses_existing_service():
    criterion = GoalSuccessCriterion(
        description="Create a source-backed brief",
        verifier_kind=CriterionVerifierKind.artifact_readback,
        evidence_refs=["operator:source-consent"],
        target={"query": "Seraph project", "file_path": "briefs/goal-1.md"},
    )
    goal = SimpleNamespace(
        id="goal-1",
        revision=4,
        proactive_enabled=True,
        success_criterion_json=criterion.model_dump_json(),
        due_date=datetime.now(timezone.utc),
        sort_order=0,
    )
    jobs = _RecordingDurableJobs()
    service_result = WebBriefToFileResult(
        goal_id="goal-1",
        goal_revision=4,
        query="Seraph project",
        file_path="briefs/goal-1.md",
        execution_status="succeeded",
        verification="passed",
        learning="no_learning",
        source_read=True,
        query_read_back=True,
        job_id="brief-child-1",
        artifact_ref="artifact-brief-1",
        reason="web_brief_workflow_executed_and_source_readback_verified",
    )
    service = MagicMock()
    service.run = AsyncMock(return_value=service_result)
    with (
        patch(
            "src.scheduler.jobs.strategist_tick.goal_repository.list_goals",
            new=AsyncMock(return_value=[goal]),
        ),
        patch("src.scheduler.jobs.strategist_tick.durable_job_repository", jobs),
        patch("src.scheduler.jobs.strategist_tick.WebBriefToFileService", return_value=service),
    ):
        receipt = await _run_opted_in_goal_web_brief(
            parent_job_id="parent-brief-1",
            parent_fencing_token=5,
        )

    assert receipt["status"] == "succeeded"
    assert receipt["source_read"] is True
    assert jobs.effects[0][1]["status"] == "succeeded"
    request = service.run.await_args.args[0]
    assert request.query == "Seraph project"
    assert request.file_path == "briefs/goal-1.md"
    assert request.parent_job_id == "parent-brief-1"
    assert request.parent_fencing_token == 5
    assert request.session_id == "web-brief:scheduler:goal-1:4"


@pytest.mark.asyncio
async def test_malformed_explicit_web_brief_target_does_not_downgrade_to_snapshot():
    criterion = GoalSuccessCriterion(
        description="Create a source-backed brief",
        verifier_kind=CriterionVerifierKind.artifact_readback,
        evidence_refs=["operator:source-consent"],
        target={"query": "Seraph project", "file_path": "../outside.md"},
    )
    goal = SimpleNamespace(
        id="goal-1",
        revision=4,
        proactive_enabled=True,
        success_criterion_json=criterion.model_dump_json(),
        due_date=datetime.now(timezone.utc),
        sort_order=0,
    )
    jobs = _RecordingDurableJobs()
    with (
        patch(
            "src.scheduler.jobs.strategist_tick.goal_repository.list_goals",
            new=AsyncMock(return_value=[goal]),
        ),
        patch("src.scheduler.jobs.strategist_tick.durable_job_repository", jobs),
    ):
        web_receipt = await _run_opted_in_goal_web_brief(
            parent_job_id="parent-brief-2",
            parent_fencing_token=6,
        )
        snapshot_receipt = await _run_opted_in_goal_snapshot(
            parent_job_id="parent-brief-2",
            parent_fencing_token=6,
        )

    assert web_receipt == {"status": "skipped", "reason": "no_eligible_web_brief_goal"}
    assert snapshot_receipt == {"status": "skipped", "reason": "no_eligible_proactive_goal"}


@pytest.mark.asyncio
async def test_strategist_tick_logs_skip(async_db):
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(return_value=_make_context())

    with (
        patch("src.scheduler.jobs.strategist_tick.build_guardian_state", AsyncMock(return_value=_make_guardian_state())),
        patch(
            "src.scheduler.jobs.strategist_tick.run_strategist_decision_completion",
            AsyncMock(
                return_value=(
                    '{"should_intervene": false, "content": "", "intervention_type": "nudge", '
                    '"urgency": 0, "reasoning": "All good"}'
                )
            ),
        ) as mock_decision,
    ):
        await run_strategist_tick()

    mock_decision.assert_awaited_once()
    events = await audit_repository.list_events(limit=10)
    assert any(
        event["event_type"] == "scheduler_job_skipped"
        and event["tool_name"] == "strategist_tick"
        and event["details"]["reason"] == "All good"
        for event in events
    )
    durable_job = await durable_job_repository.get_job(_occurrence_identity())
    assert durable_job is not None
    assert durable_job["status"] == "succeeded"
    assert durable_job["result"]["summary"] == "no intervention required"


@pytest.mark.asyncio
async def test_strategist_tick_logs_success(async_db):
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(return_value=_make_context())

    async def assert_delivery_has_durable_intent(_message, **_kwargs):
        current = await durable_job_repository.get_job(_occurrence_identity())
        assert current is not None
        delivery_intent = next(
            item for item in current["effects"] if item.get("effect_type") == "proactive_delivery"
        )
        assert delivery_intent["status"] == "intent"
        assert delivery_intent["adapter_idempotency_key"] == f"strategist-delivery:{_occurrence_identity()}"
        return DeliveryDecision.deliver

    mock_deliver = AsyncMock(side_effect=assert_delivery_has_durable_intent)

    with (
        patch("src.scheduler.jobs.strategist_tick.build_guardian_state", AsyncMock(return_value=_make_guardian_state())),
        patch(
            "src.scheduler.jobs.strategist_tick.run_strategist_decision_completion",
            AsyncMock(
                return_value=(
                    '{"should_intervene": true, "content": "Time to refocus.", '
                    '"intervention_type": "advisory", "urgency": 3, "reasoning": "Focus drift"}'
                )
            ),
        ),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
    ):
        await run_strategist_tick()

    events = await audit_repository.list_events(limit=10)
    assert mock_deliver.await_args.kwargs["guardian_confidence"] == "grounded"
    assert any(
        event["event_type"] == "scheduler_job_unknown_external_effect"
        and event["tool_name"] == "strategist_tick"
        and event["details"]["delivery"] == "deliver"
        and event["details"]["policy_action"] is None
        for event in events
    )
    durable_job = await durable_job_repository.get_job(_occurrence_identity())
    assert durable_job is not None
    assert durable_job["status"] == "unknown_external_effect"
    delivery_effect = next(item for item in durable_job["effects"] if item["effect_type"] == "proactive_delivery")
    assert delivery_effect["status"] == "unknown"
    assert "Focus drift" not in str(durable_job["effects"])


@pytest.mark.asyncio
async def test_strategist_tick_uses_direct_decision_completion(async_db):
    decision_completion = AsyncMock(
        return_value=(
            '{"should_intervene": false, "content": "", "intervention_type": "nudge", '
            '"urgency": 0, "reasoning": "No intervention"}'
        )
    )

    with (
        patch("src.scheduler.jobs.strategist_tick.build_guardian_state", AsyncMock(return_value=_make_guardian_state())),
        patch("src.scheduler.jobs.strategist_tick.run_strategist_decision_completion", decision_completion),
    ):
        await run_strategist_tick()

    decision_completion.assert_awaited_once()
    assert decision_completion.await_args.kwargs["guardian_state"].confidence.overall == "grounded"


@pytest.mark.asyncio
async def test_strategist_tick_logs_guardian_state_failure_without_request_id(async_db):
    with patch(
        "src.scheduler.jobs.strategist_tick.build_guardian_state",
        AsyncMock(side_effect=RuntimeError("guardian unavailable")),
    ):
        await run_strategist_tick()

    events = await audit_repository.list_events(limit=10)
    assert any(
        event["event_type"] == "scheduler_job_failed"
        and event["tool_name"] == "strategist_tick"
        and event["details"]["error"] == "guardian unavailable"
        and event["details"]["request_id"] is None
        for event in events
    )
    durable_job = await durable_job_repository.get_job(_occurrence_identity())
    assert durable_job is not None
    assert durable_job["status"] == "failed"
    assert durable_job["failure_reason"] == "RuntimeError"


@pytest.mark.asyncio
async def test_strategist_tick_duplicate_occurrence_is_not_reexecuted(async_db):
    decision_completion = AsyncMock(
        return_value=(
            '{"should_intervene": false, "content": "", "intervention_type": "nudge", '
            '"urgency": 0, "reasoning": "Already handled"}'
        )
    )

    with (
        patch("src.scheduler.jobs.strategist_tick.build_guardian_state", AsyncMock(return_value=_make_guardian_state())),
        patch("src.scheduler.jobs.strategist_tick.run_strategist_decision_completion", decision_completion),
    ):
        await run_strategist_tick()
        await run_strategist_tick()

    decision_completion.assert_awaited_once()
    durable_job = await durable_job_repository.get_job(_occurrence_identity())
    assert durable_job is not None
    assert durable_job["status"] == "succeeded"


@pytest.mark.asyncio
async def test_strategist_tick_does_not_read_context_when_admission_is_not_granted(async_db):
    with (
        patch("src.scheduler.jobs.strategist_tick._admit_and_claim_tick", AsyncMock(return_value=None)),
        patch("src.scheduler.jobs.strategist_tick.build_guardian_state", AsyncMock()) as build_state,
    ):
        await run_strategist_tick()

    build_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_strategist_tick_timeout_is_terminally_recorded(async_db):
    with (
        patch("src.scheduler.jobs.strategist_tick.build_guardian_state", AsyncMock(return_value=_make_guardian_state())),
        patch(
            "src.scheduler.jobs.strategist_tick.run_strategist_decision_completion",
            AsyncMock(side_effect=asyncio.TimeoutError()),
        ),
    ):
        await run_strategist_tick()

    durable_job = await durable_job_repository.get_job(_occurrence_identity())
    assert durable_job is not None
    assert durable_job["status"] == "failed"
    assert durable_job["failure_reason"] == "strategist_timeout"
