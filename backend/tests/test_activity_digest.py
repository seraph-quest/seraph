"""Tests for the daily activity digest scheduler job."""

import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestActivityDigest:
    @pytest.mark.asyncio
    async def test_skips_when_no_observations(self):
        """Digest should skip when there are no observations today."""
        mock_repo = MagicMock()
        mock_repo.get_daily_summary = AsyncMock(return_value={
            "date": date.today().isoformat(),
            "total_observations": 0,
        })

        with patch(
            "src.observer.screen_repository.screen_observation_repo",
            mock_repo,
        ):
            from src.scheduler.jobs.activity_digest import run_activity_digest
            await run_activity_digest()

        # LLM should NOT be called
        mock_repo.get_daily_summary.assert_called_once()

    @pytest.mark.asyncio
    async def test_happy_path(self):
        """Digest should generate and deliver when observations exist."""
        mock_repo = MagicMock()
        mock_repo.get_daily_summary = AsyncMock(return_value={
            "date": date.today().isoformat(),
            "total_observations": 15,
            "total_tracked_minutes": 120,
            "switch_count": 15,
            "by_activity": {"coding": 5400, "browsing": 1800},
            "by_project": {"seraph": 3600},
            "longest_streaks": [
                {"activity": "coding", "duration_minutes": 45, "started_at": "2025-01-01T09:00"},
            ],
        })

        mock_soul = "Test soul content"
        mock_llm_response = MagicMock()
        mock_llm_response.choices = [MagicMock()]
        mock_llm_response.choices[0].message.content = "Your daily digest text here."

        mock_deliver = AsyncMock()

        with patch("src.observer.screen_repository.screen_observation_repo", mock_repo), \
             patch("src.memory.soul.read_soul", return_value=mock_soul), \
             patch("litellm.completion", return_value=mock_llm_response), \
             patch("src.observer.delivery.deliver_or_queue", mock_deliver):

            from src.scheduler.jobs.activity_digest import run_activity_digest
            await run_activity_digest()

        mock_deliver.assert_called_once()
        call_args = mock_deliver.call_args
        assert call_args[0][0].content == "Your daily digest text here."
        assert call_args[1]["is_scheduled"] is True

    @pytest.mark.asyncio
    async def test_llm_timeout(self):
        """Digest should handle LLM timeout gracefully."""
        mock_repo = MagicMock()
        mock_repo.get_daily_summary = AsyncMock(return_value={
            "date": date.today().isoformat(),
            "total_observations": 5,
            "total_tracked_minutes": 60,
            "switch_count": 5,
            "by_activity": {"coding": 3600},
            "by_project": {},
            "longest_streaks": [],
        })

        async def slow_completion(*args, **kwargs):
            await asyncio.sleep(999)

        mock_deliver = AsyncMock()

        with patch("src.observer.screen_repository.screen_observation_repo", mock_repo), \
             patch("src.memory.soul.read_soul", return_value="soul"), \
             patch("litellm.completion", side_effect=slow_completion), \
             patch("src.observer.delivery.deliver_or_queue", mock_deliver), \
             patch("config.settings.settings.agent_briefing_timeout", 0.01):

            from src.scheduler.jobs.activity_digest import run_activity_digest
            await run_activity_digest()

        # Should NOT deliver on timeout
        mock_deliver.assert_not_called()
