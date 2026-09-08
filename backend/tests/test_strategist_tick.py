"""Tests for strategist tick runtime audit coverage."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.audit.repository import audit_repository
from src.guardian.state import GuardianState, GuardianStateConfidence
from src.guardian.world_model import GuardianWorldModel
from src.observer.context import CurrentContext
from src.observer.user_state import DeliveryDecision
from src.scheduler.jobs.strategist_tick import _occurrence_identity, run_strategist_tick
from src.workflows.job_runtime import durable_job_repository


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
    mock_deliver = AsyncMock(return_value=DeliveryDecision.deliver)

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
        event["event_type"] == "scheduler_job_succeeded"
        and event["tool_name"] == "strategist_tick"
        and event["details"]["delivery"] == "deliver"
        and event["details"]["policy_action"] is None
        for event in events
    )
    durable_job = await durable_job_repository.get_job(_occurrence_identity())
    assert durable_job is not None
    assert durable_job["status"] == "succeeded"
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
