"""Focused proof for bounded, reversible goal strategy correction."""

from datetime import datetime, timezone
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.goals import (
    GoalStrategyCorrection,
    GoalStrategyRollback,
    apply_goal_strategy_correction,
    _build_strategy_target,
    _canonical_strategy_target,
    _strategy_delta_id,
    rollback_goal_strategy_correction,
)
from src.db.models import Goal, StrategyDelta
from src.goals.contracts import CriterionVerifierKind, GoalSuccessCriterion
from src.memory.control import StrategyDeltaReceipt
from src.memory.control import record_strategy_delta_proposal
from src.scheduler.jobs.strategist_tick import _run_opted_in_goal_web_brief, _web_brief_target
from src.guardian.web_brief_to_file import WebBriefToFileResult


def _criterion(target: dict) -> GoalSuccessCriterion:
    return GoalSuccessCriterion(
        description="Create a source-backed brief",
        verifier_kind=CriterionVerifierKind.artifact_readback,
        evidence_refs=["operator:source-consent"],
        target=target,
    )


def test_strategy_target_is_canonical_and_correction_is_bounded():
    criterion = _criterion({"query": "old query", "file_path": "briefs/old.md"})
    correction = GoalStrategyCorrection(
        correction_id="corr-1",
        expected_revision=1,
        query="new query",
        priority=80,
        reason="The old source set is stale.",
    )

    before, after = _build_strategy_target("goal-1", criterion, correction, "delta-1")

    assert before == {
        "query": "old query",
        "file_path": "briefs/old.md",
        "priority": 0,
    }
    assert after == {
        "query": "new query",
        "file_path": "briefs/old.md",
        "priority": 80,
        "strategy_delta_id": "delta-1",
    }
    assert _canonical_strategy_target("goal-1", criterion) == before


def test_strategy_target_rejects_unknown_fields_and_unsafe_paths():
    with pytest.raises(ValueError, match="unsupported strategy fields"):
        _canonical_strategy_target(
            "goal-1",
            _criterion({"query": "q", "file_path": "brief.md", "authority": "all"}),
        )
    with pytest.raises(ValueError, match="file_path"):
        GoalStrategyCorrection(
            correction_id="corr-2",
            expected_revision=1,
            file_path="../outside.md",
            reason="unsafe path should be rejected",
        )


def test_scheduler_rejects_malformed_legacy_web_brief_targets():
    goal = SimpleNamespace(id="goal-1")
    for target in (
        {"query": "q", "file_path": "brief.md", "unexpected": True},
        {"query": 42, "file_path": "brief.md"},
        {"query": "q", "file_path": "brief.md", "priority": "90"},
        {"query": "q", "file_path": "brief.md", "strategy_delta_id": 7},
    ):
        assert _web_brief_target(goal, _criterion(target)) is None


def test_strategy_delta_table_is_registered_with_unique_replay_fence():
    assert StrategyDelta.__table__.name == "strategy_deltas"
    assert any(
        index.unique and index.name == "ux_strategy_deltas_source_event_id"
        for index in StrategyDelta.__table__.indexes
    )


@pytest.mark.asyncio
async def test_strategy_delta_persistence_receipt_keeps_supplied_deterministic_id():
    captured = []

    class _Result:
        def scalars(self):
            return self

        def first(self):
            return None

    class _DB:
        async def execute(self, _statement):
            return _Result()

        def add(self, delta):
            captured.append(delta)

        async def flush(self):
            return None

        def expunge(self, _delta):
            return None

    @asynccontextmanager
    async def _session():
        yield _DB()

    with patch("src.memory.control.get_session", _session):
        receipt = await record_strategy_delta_proposal(
            goal_id="goal-1",
            source_event_id="corr-1",
            before={"query": "old"},
            after={"query": "new"},
            author_id="operator:test",
            goal_revision_before=1,
            reason="operator correction",
            delta_id="deterministic-delta",
        )

    assert captured[0].delta_id == "deterministic-delta"
    assert receipt.delta_id == "deterministic-delta"


def _goal(goal_id: str, revision: int, target: dict) -> Goal:
    return Goal(
        id=goal_id,
        title="Research goal",
        revision=revision,
        proactive_enabled=True,
        success_criterion_json=_criterion(target).model_dump_json(),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _delta(status: str, *, revision_after: int | None = None) -> StrategyDeltaReceipt:
    delta_id = _strategy_delta_id("goal-1", "corr-1")
    return StrategyDeltaReceipt(
        delta_id=delta_id,
        goal_id="goal-1",
        scope="goal",
        field_name="web_brief_target",
        before={"query": "old query", "file_path": "briefs/old.md", "priority": 0},
        after={
            "query": "new query",
            "file_path": "briefs/old.md",
            "priority": 80,
            "strategy_delta_id": delta_id,
        },
        source_event_id="corr-1",
        author_id="operator:test-bypass",
        evaluator_id=None,
        goal_revision_before=1,
        goal_revision_after=revision_after,
        status=status,
        rollback_target_id=None,
        reason="The old source set is stale.",
        created_at="2026-09-09T00:00:00+00:00",
        updated_at="2026-09-09T00:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_correction_apply_replay_rollback_with_cas_and_receipts():
    from src.api import goals as goals_api

    initial = _goal("goal-1", 1, {"query": "old query", "file_path": "briefs/old.md"})
    corrected = _goal(
        "goal-1",
        2,
        {
            "query": "new query",
            "file_path": "briefs/old.md",
            "priority": 80,
            "strategy_delta_id": _strategy_delta_id("goal-1", "corr-1"),
        },
    )
    restored = _goal("goal-1", 3, {"query": "old query", "file_path": "briefs/old.md", "priority": 0})
    proposed = _delta("proposed")
    applied = _delta("applied", revision_after=2)
    rolled_back = _delta("rolled_back", revision_after=3)
    operator = SimpleNamespace(
        principal=SimpleNamespace(principal_id="operator:test-bypass"),
        session_id="test-auth-bypass",
    )
    body = GoalStrategyCorrection(
        correction_id="corr-1",
        expected_revision=1,
        query="new query",
        priority=80,
        reason="The old source set is stale.",
    )
    request = SimpleNamespace()

    with (
        patch.object(goals_api, "_require_authenticated_operator", return_value=operator),
        patch.object(goals_api.goal_repository, "get", new=AsyncMock(return_value=initial)),
        patch.object(goals_api, "get_strategy_delta_by_source_event", new=AsyncMock(return_value=None)),
        patch.object(goals_api, "record_strategy_delta_proposal", new=AsyncMock(return_value=proposed)),
        patch.object(goals_api.goal_repository, "update", new=AsyncMock(return_value=corrected)),
        patch.object(goals_api, "update_strategy_delta", new=AsyncMock(return_value=applied)),
        patch.object(goals_api.audit_repository, "log_event", new=AsyncMock(return_value=SimpleNamespace(id="audit-1"))),
    ):
        response = await apply_goal_strategy_correction("goal-1", body, request)
    assert response["status"] == "applied"
    assert response["delta"]["status"] == "applied"
    assert response["goal"]["revision"] == 2

    with (
        patch.object(goals_api, "_require_authenticated_operator", return_value=operator),
        patch.object(goals_api.goal_repository, "get", new=AsyncMock(return_value=corrected)),
        patch.object(goals_api, "get_strategy_delta_by_source_event", new=AsyncMock(return_value=applied)),
    ):
        replay = await apply_goal_strategy_correction("goal-1", body, request)
    assert replay["status"] == "replayed"

    rollback_body = GoalStrategyRollback(expected_revision=2, reason="The correction was not useful.")
    with (
        patch.object(goals_api, "_require_authenticated_operator", return_value=operator),
        patch.object(goals_api.goal_repository, "get", new=AsyncMock(return_value=corrected)),
        patch.object(goals_api, "get_strategy_delta", new=AsyncMock(return_value=applied)),
        patch.object(goals_api.goal_repository, "update", new=AsyncMock(return_value=restored)),
        patch.object(goals_api, "update_strategy_delta", new=AsyncMock(return_value=rolled_back)),
        patch.object(goals_api.audit_repository, "log_event", new=AsyncMock(return_value=SimpleNamespace(id="audit-2"))),
    ):
        rollback = await rollback_goal_strategy_correction(
            "goal-1", _strategy_delta_id("goal-1", "corr-1"), rollback_body, request
        )
    assert rollback["status"] == "rolled_back"
    assert rollback["goal"]["revision"] == 3
    assert rollback["delta"]["status"] == "rolled_back"


@pytest.mark.asyncio
async def test_web_brief_scheduler_uses_corrected_priority_and_evidence():
    criterion_low = _criterion(
        {
            "query": "low priority",
            "file_path": "briefs/low.md",
            "priority": 10,
            "strategy_delta_id": "delta-low",
        }
    )
    criterion_high = _criterion(
        {
            "query": "high priority",
            "file_path": "briefs/high.md",
            "priority": 90,
            "strategy_delta_id": "delta-high",
        }
    )
    low = SimpleNamespace(
        id="goal-low",
        revision=2,
        proactive_enabled=True,
        success_criterion_json=criterion_low.model_dump_json(),
        due_date=None,
        sort_order=0,
    )
    high = SimpleNamespace(
        id="goal-high",
        revision=2,
        proactive_enabled=True,
        success_criterion_json=criterion_high.model_dump_json(),
        due_date=None,
        sort_order=1,
    )
    jobs = MagicMock()
    jobs.record_effect = AsyncMock()
    service_result = WebBriefToFileResult(
        goal_id="goal-high",
        goal_revision=2,
        query="high priority",
        file_path="briefs/high.md",
        execution_status="succeeded",
        verification="passed",
        learning="no_learning",
        source_read=True,
        query_read_back=True,
        job_id="child-high",
        artifact_ref="artifact-high",
        strategy_delta_id="delta-high",
        strategy_delta_provenance="verified",
        reason="verified",
    )
    service = MagicMock()
    service.run = AsyncMock(return_value=service_result)

    with (
        patch(
            "src.scheduler.jobs.strategist_tick.goal_repository.list_goals",
            new=AsyncMock(return_value=[low, high]),
        ),
        patch("src.scheduler.jobs.strategist_tick.durable_job_repository", jobs),
        patch("src.scheduler.jobs.strategist_tick.WebBriefToFileService", return_value=service),
    ):
        receipt = await _run_opted_in_goal_web_brief(
            parent_job_id="parent-1",
            parent_fencing_token=2,
        )

    assert receipt["goal_id"] == "goal-high"
    assert receipt["priority"] == 90
    assert receipt["strategy_delta_id"] == "delta-high"
    request = service.run.await_args.args[0]
    assert request.priority == 90
    assert "strategy-delta:delta-high" in request.evidence_refs
    assert jobs.record_effect.await_args.kwargs["details"]["priority"] == 90
    assert jobs.record_effect.await_args.kwargs["details"]["strategy_delta_id"] == "delta-high"
    assert jobs.record_effect.await_args.kwargs["details"]["strategy_delta_provenance"] == "verified"


@pytest.mark.asyncio
async def test_web_brief_scheduler_yields_after_unresolved_correction_and_redacts_id():
    high_criterion = _criterion(
        {
            "query": "high corrected priority",
            "file_path": "briefs/high.md",
            "priority": 90,
            "strategy_delta_id": "delta-unresolved",
        }
    )
    low_criterion = _criterion(
        {
            "query": "lower valid priority",
            "file_path": "briefs/low.md",
            "priority": 10,
        }
    )
    high = SimpleNamespace(
        id="goal-high-unresolved",
        revision=2,
        proactive_enabled=True,
        success_criterion_json=high_criterion.model_dump_json(),
        due_date=None,
        sort_order=0,
    )
    low = SimpleNamespace(
        id="goal-low-valid",
        revision=2,
        proactive_enabled=True,
        success_criterion_json=low_criterion.model_dump_json(),
        due_date=None,
        sort_order=1,
    )
    jobs = MagicMock()
    jobs.record_effect = AsyncMock()
    blocked = WebBriefToFileResult(
        goal_id=high.id,
        goal_revision=2,
        query="high corrected priority",
        file_path="briefs/high.md",
        execution_status="blocked",
        verification="unknown",
        learning="no_learning",
        strategy_delta_id="delta-unresolved",
        strategy_delta_provenance="unresolved",
        reason="strategy_delta_unresolved",
    )
    succeeded = WebBriefToFileResult(
        goal_id=low.id,
        goal_revision=2,
        query="lower valid priority",
        file_path="briefs/low.md",
        execution_status="succeeded",
        verification="passed",
        learning="no_learning",
        source_read=True,
        query_read_back=True,
        job_id="child-low",
        artifact_ref="artifact-low",
        reason="verified",
    )
    service = MagicMock()
    service.run = AsyncMock(side_effect=[blocked, succeeded])

    with (
        patch(
            "src.scheduler.jobs.strategist_tick.goal_repository.list_goals",
            new=AsyncMock(return_value=[low, high]),
        ),
        patch("src.scheduler.jobs.strategist_tick.durable_job_repository", jobs),
        patch("src.scheduler.jobs.strategist_tick.WebBriefToFileService", return_value=service),
    ):
        receipt = await _run_opted_in_goal_web_brief(
            parent_job_id="parent-correction-fallback",
            parent_fencing_token=3,
        )

    assert receipt["goal_id"] == low.id
    assert [call.args[0].query for call in service.run.await_args_list] == [
        "high corrected priority",
        "lower valid priority",
    ]
    first_details = jobs.record_effect.await_args_list[0].kwargs["details"]
    assert first_details["status"] == "blocked"
    assert first_details["reason"] == "strategy_delta_unresolved"
    assert first_details["strategy_delta_id"] is None
    assert first_details["strategy_delta_provenance"] == "unresolved"
    assert first_details["strategy_delta_evidence_ref"] is None
    assert len(jobs.record_effect.await_args_list) == 2


@pytest.mark.asyncio
async def test_web_brief_scheduler_caps_correction_fallback_at_two_candidates():
    goals = []
    blocked_results = []
    for index, priority in enumerate((90, 80, 70), start=1):
        goal_id = f"goal-unresolved-{index}"
        query = f"unresolved priority {priority}"
        criterion = _criterion(
            {
                "query": query,
                "file_path": f"briefs/unresolved-{index}.md",
                "priority": priority,
                "strategy_delta_id": f"delta-unresolved-{index}",
            }
        )
        goal = SimpleNamespace(
            id=goal_id,
            revision=2,
            proactive_enabled=True,
            success_criterion_json=criterion.model_dump_json(),
            due_date=None,
            sort_order=index,
        )
        goals.append(goal)
        blocked_results.append(
            WebBriefToFileResult(
                goal_id=goal_id,
                goal_revision=2,
                query=query,
                file_path=f"briefs/unresolved-{index}.md",
                execution_status="blocked",
                verification="unknown",
                learning="no_learning",
                strategy_delta_id=f"delta-unresolved-{index}",
                strategy_delta_provenance="unresolved",
                reason="strategy_delta_unresolved",
            )
        )

    jobs = MagicMock()
    jobs.record_effect = AsyncMock()
    service = MagicMock()
    service.run = AsyncMock(side_effect=blocked_results)

    with (
        patch(
            "src.scheduler.jobs.strategist_tick.goal_repository.list_goals",
            new=AsyncMock(return_value=goals),
        ),
        patch("src.scheduler.jobs.strategist_tick.durable_job_repository", jobs),
        patch("src.scheduler.jobs.strategist_tick.WebBriefToFileService", return_value=service),
    ):
        receipt = await _run_opted_in_goal_web_brief(
            parent_job_id="parent-correction-cap",
            parent_fencing_token=4,
        )

    assert receipt["goal_id"] == goals[1].id
    assert [call.args[0].query for call in service.run.await_args_list] == [
        "unresolved priority 90",
        "unresolved priority 80",
    ]
    assert len(jobs.record_effect.await_args_list) == 2
    assert all(
        call.kwargs["details"]["reason"] == "strategy_delta_unresolved"
        for call in jobs.record_effect.await_args_list
    )
