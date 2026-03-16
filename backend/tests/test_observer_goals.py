from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from src.audit.repository import audit_repository
from src.observer.sources.goal_source import gather_goals


class TestGoalSource:
    @pytest.mark.asyncio
    async def test_no_goals(self, async_db):
        mock_repo = AsyncMock()
        mock_repo.list_goals.return_value = []

        with patch("src.goals.repository.goal_repository", mock_repo):
            result = await gather_goals()

        assert result["active_goals_summary"] == ""
        events = await audit_repository.list_events(limit=5)
        assert any(
            event["event_type"] == "integration_empty_result"
            and event["tool_name"] == "observer_source:goals"
            and event["details"]["goal_count"] == 0
            for event in events
        )

    @pytest.mark.asyncio
    async def test_multiple_domains(self, async_db):
        goal1 = MagicMock(domain="productivity", title="Write docs")
        goal2 = MagicMock(domain="productivity", title="Review PR")
        goal3 = MagicMock(domain="health", title="Exercise")

        mock_repo = AsyncMock()
        mock_repo.list_goals.return_value = [goal1, goal2, goal3]

        with patch("src.goals.repository.goal_repository", mock_repo):
            result = await gather_goals()

        summary = result["active_goals_summary"]
        assert "productivity" in summary
        assert "health" in summary
        assert "Write docs" in summary
        assert "Exercise" in summary
        events = await audit_repository.list_events(limit=5)
        assert any(
            event["event_type"] == "integration_succeeded"
            and event["tool_name"] == "observer_source:goals"
            and event["details"]["goal_count"] == 3
            and event["details"]["domain_count"] == 2
            for event in events
        )

    @pytest.mark.asyncio
    async def test_truncates_per_domain(self):
        goals = [MagicMock(domain="work", title=f"Task {i}") for i in range(5)]

        mock_repo = AsyncMock()
        mock_repo.list_goals.return_value = goals

        with patch("src.goals.repository.goal_repository", mock_repo):
            result = await gather_goals()

        summary = result["active_goals_summary"]
        assert "(+2 more)" in summary

    @pytest.mark.asyncio
    async def test_exception_returns_empty(self, async_db):
        mock_repo = AsyncMock()
        mock_repo.list_goals.side_effect = RuntimeError("db error")

        with patch("src.goals.repository.goal_repository", mock_repo):
            result = await gather_goals()

        assert result["active_goals_summary"] == ""
        events = await audit_repository.list_events(limit=5)
        assert any(
            event["event_type"] == "integration_failed"
            and event["tool_name"] == "observer_source:goals"
            and event["details"]["error"] == "db error"
            for event in events
        )
