from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from src.observer.sources.goal_source import gather_goals


class TestGoalSource:
    @pytest.mark.asyncio
    async def test_no_goals(self):
        mock_repo = AsyncMock()
        mock_repo.list_goals.return_value = []

        with patch("src.goals.repository.goal_repository", mock_repo):
            result = await gather_goals()

        assert result["active_goals_summary"] == ""

    @pytest.mark.asyncio
    async def test_multiple_domains(self):
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
    async def test_exception_returns_empty(self):
        mock_repo = AsyncMock()
        mock_repo.list_goals.side_effect = RuntimeError("db error")

        with patch("src.goals.repository.goal_repository", mock_repo):
            result = await gather_goals()

        assert result["active_goals_summary"] == ""
