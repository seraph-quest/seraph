"""Tests for agent execution timeouts (Phase 3.5.6)."""

import time
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.settings import settings
from src.observer.context import CurrentContext
from src.scheduler.jobs.daily_briefing import run_daily_briefing
from src.scheduler.jobs.evening_review import run_evening_review


def _slow_run(*args, **kwargs):
    time.sleep(5)
    return "too late"


def _slow_litellm(*args, **kwargs):
    time.sleep(5)
    return MagicMock()


def _make_context(**overrides) -> CurrentContext:
    defaults = dict(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="3 active goals",
    )
    defaults.update(overrides)
    return CurrentContext(**defaults)


@pytest.mark.asyncio
class TestRestChatTimeout:
    @patch("src.memory.vector_store.search_formatted", return_value="")
    @patch("src.api.chat.build_agent")
    @patch("src.api.chat.create_onboarding_agent")
    async def test_returns_504_on_timeout(
        self, mock_onboarding, mock_create_agent, mock_search, client
    ):
        mock_agent = MagicMock()
        mock_agent.run.side_effect = _slow_run
        mock_onboarding.return_value = mock_agent

        original = settings.agent_chat_timeout
        settings.agent_chat_timeout = 0.1
        try:
            response = await client.post("/api/chat", json={"message": "Hello"})
            assert response.status_code == 504
            assert "timed out" in response.json()["detail"]
        finally:
            settings.agent_chat_timeout = original

    @patch("src.api.chat.get_current_tool_policy_mode", return_value="safe")
    @patch("src.api.chat.log_agent_run_event", new_callable=AsyncMock)
    @patch("src.api.chat.get_or_create_profile", new_callable=AsyncMock)
    @patch("src.api.chat.build_guardian_state", new_callable=AsyncMock)
    @patch("src.api.chat.build_agent")
    @patch("src.api.chat.should_use_direct_local_chat", return_value=False)
    async def test_guardian_state_timeout_falls_back_to_minimal_agent(
        self,
        mock_should_use_direct,
        mock_build_agent,
        mock_build_guardian_state,
        mock_profile,
        mock_log_agent_run_event,
        mock_policy,
        client,
    ):
        mock_profile.return_value = SimpleNamespace(onboarding_completed=True)
        mock_build_guardian_state.side_effect = asyncio.TimeoutError
        mock_agent = MagicMock()
        mock_agent.run.return_value = MagicMock(output="hello back")
        mock_build_agent.return_value = mock_agent

        response = await client.post("/api/chat", json={"message": "Hello"})

        assert response.status_code == 200
        assert response.json()["response"] == "hello back"
        mock_build_agent.assert_called_once_with()


@pytest.mark.asyncio
class TestDailyBriefingTimeout:
    async def test_llm_timeout_no_delivery(self):
        ctx = _make_context()
        mock_cm = MagicMock()
        mock_cm.refresh = AsyncMock(return_value=ctx)

        mock_deliver = AsyncMock()

        original = settings.agent_briefing_timeout
        settings.agent_briefing_timeout = 0.1
        try:
            with (
                patch("src.observer.manager.context_manager", mock_cm),
                patch("src.memory.soul.read_soul", return_value="# Soul"),
                patch("src.memory.vector_store.search_formatted", return_value=""),
                patch("litellm.completion", side_effect=_slow_litellm),
                patch("src.observer.delivery.deliver_or_queue", mock_deliver),
            ):
                await run_daily_briefing()
                mock_deliver.assert_not_called()
        finally:
            settings.agent_briefing_timeout = original


@pytest.mark.asyncio
class TestEveningReviewTimeout:
    async def test_llm_timeout_no_delivery(self):
        ctx = _make_context()
        mock_cm = MagicMock()
        mock_cm.refresh = AsyncMock(return_value=ctx)

        mock_deliver = AsyncMock()

        original = settings.agent_briefing_timeout
        settings.agent_briefing_timeout = 0.1
        try:
            with (
                patch("src.observer.manager.context_manager", mock_cm),
                patch("src.memory.soul.read_soul", return_value="# Soul"),
                patch("src.memory.vector_store.search_formatted", return_value=""),
                patch(
                    "src.scheduler.jobs.evening_review._count_messages_today",
                    new_callable=AsyncMock,
                    return_value=(5, False),
                ),
                patch(
                    "src.scheduler.jobs.evening_review._get_completed_goals_today",
                    new_callable=AsyncMock,
                    return_value=([], False),
                ),
                patch("litellm.completion", side_effect=_slow_litellm),
                patch("src.observer.delivery.deliver_or_queue", mock_deliver),
            ):
                await run_evening_review()
                mock_deliver.assert_not_called()
        finally:
            settings.agent_briefing_timeout = original


@pytest.mark.asyncio
class TestConsolidationTimeout:
    async def test_llm_timeout_no_memories(self, async_db):
        from src.agent.session import SessionManager

        sm = SessionManager()
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "Tell me about quantum computing and its applications.")
        await sm.add_message("s1", "assistant", "Quantum computing uses qubits for complex calculations.")

        original = settings.consolidation_llm_timeout
        settings.consolidation_llm_timeout = 0.1
        try:
            with (
                patch("litellm.completion", side_effect=_slow_litellm),
                patch("src.memory.consolidator.add_memory") as mock_add,
            ):
                from src.memory.consolidator import consolidate_session

                await consolidate_session("s1")
                mock_add.assert_not_called()
        finally:
            settings.consolidation_llm_timeout = original


@pytest.mark.asyncio
class TestWebSearchTimeout:
    async def test_timeout_returns_error_string(self):
        from src.tools.web_search_tool import web_search

        original = settings.web_search_timeout
        settings.web_search_timeout = 1
        try:
            with patch("src.tools.web_search_tool.DDGS") as MockDDGS:
                mock_instance = MagicMock()
                mock_instance.__enter__ = MagicMock(return_value=mock_instance)
                mock_instance.__exit__ = MagicMock(return_value=False)
                mock_instance.text.side_effect = Exception("Timed out")
                MockDDGS.return_value = mock_instance

                result = web_search("test query")
                assert "error" in result.lower() or "Error" in result
        finally:
            settings.web_search_timeout = original
