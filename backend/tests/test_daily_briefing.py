"""Tests for daily briefing job."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.usefixtures("mocked_canonical_inference_context")

from src.audit.repository import audit_repository
from src.memory.hybrid_retrieval import HybridMemoryRetrievalResult
from src.security.trust_contract import canonical_digest
from src.observer.context import CurrentContext
from src.scheduler.jobs.daily_briefing import _get_relevant_memories, run_daily_briefing


def _make_context(**overrides) -> CurrentContext:
    defaults = dict(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="3 active goals",
    )
    defaults.update(overrides)
    return CurrentContext(**defaults)


def _mock_litellm_response(text: str):
    """Create a mock litellm completion response."""
    mock_choice = MagicMock()
    mock_choice.message.content = text
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    return mock_response


def _hybrid_memories(
    texts: tuple[str, ...] = (),
    *,
    degraded: bool = False,
    reason: str | None = None,
) -> HybridMemoryRetrievalResult:
    diagnostics = (
        ({"reason": reason, "status": "degraded_no_learning"},)
        if reason
        else ()
    )
    return HybridMemoryRetrievalResult(
        context="\n".join(f"- [fact] {text}" for text in texts),
        buckets={"fact": texts} if texts else {},
        degraded=degraded,
        hits=(),
        diagnostics=diagnostics,
    )


@pytest.mark.asyncio
async def test_daily_briefing_happy_path():
    ctx = _make_context()
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(return_value=ctx)

    mock_deliver = AsyncMock()

    with (
        patch("src.observer.manager.context_manager", mock_cm),
        patch("src.memory.soul.read_soul", return_value="# Soul\nName: Hero"),
        patch(
            "src.scheduler.jobs.daily_briefing.retrieve_hybrid_memory",
            new=AsyncMock(return_value=_hybrid_memories(("User likes mornings",))),
        ),
        patch("litellm.completion", return_value=_mock_litellm_response("Good morning, Hero! Here's your briefing...")),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
    ):
        await run_daily_briefing()

        mock_deliver.assert_called_once()
        call_args = mock_deliver.call_args
        msg = call_args[0][0]
        assert msg.type == "proactive"
        assert msg.intervention_type == "advisory"
        assert "Good morning" in msg.content
        # is_scheduled=True
        assert call_args[1]["is_scheduled"] is True


@pytest.mark.asyncio
async def test_daily_briefing_logs_success(async_db):
    ctx = _make_context()
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(return_value=ctx)

    with (
        patch("src.observer.manager.context_manager", mock_cm),
        patch("src.memory.soul.read_soul", return_value="# Soul\nName: Hero"),
        patch(
            "src.scheduler.jobs.daily_briefing.retrieve_hybrid_memory",
            new=AsyncMock(return_value=_hybrid_memories(("User likes mornings",))),
        ),
        patch("litellm.completion", return_value=_mock_litellm_response("Good morning, Hero! Here's your briefing...")),
        patch("src.observer.delivery.deliver_or_queue", AsyncMock()),
    ):
        await run_daily_briefing()

    events = await audit_repository.list_events(limit=10)
    assert any(
        event["event_type"] == "scheduler_job_succeeded"
        and event["tool_name"] == "daily_briefing"
        for event in events
    )


@pytest.mark.asyncio
async def test_daily_briefing_uses_named_runtime_path():
    ctx = _make_context()
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(return_value=ctx)
    mock_response = _mock_litellm_response("Good morning, Hero! Here's your briefing...")

    with (
        patch("src.observer.manager.context_manager", mock_cm),
        patch("src.memory.soul.read_soul", return_value="# Soul\nName: Hero"),
        patch(
            "src.scheduler.jobs.daily_briefing.retrieve_hybrid_memory",
            new=AsyncMock(return_value=_hybrid_memories(("User likes mornings",))),
        ),
        patch(
            "src.scheduler.jobs.daily_briefing.completion_with_fallback",
            new=AsyncMock(return_value=mock_response),
        ) as mock_completion,
        patch("src.observer.delivery.deliver_or_queue", AsyncMock()),
    ):
        await run_daily_briefing()

    assert mock_completion.await_args.kwargs["runtime_path"] == "daily_briefing"
    call = mock_completion.await_args.kwargs
    assert call["request_context"].data_digest == canonical_digest(call["messages"])


@pytest.mark.asyncio
async def test_daily_briefing_context_refresh_failure():
    """Context refresh failure → early return (exception caught)."""
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(side_effect=Exception("DB down"))

    mock_deliver = AsyncMock()

    with (
        patch("src.observer.manager.context_manager", mock_cm),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
    ):
        # Should not raise — exception is caught internally
        await run_daily_briefing()
        mock_deliver.assert_not_called()


@pytest.mark.asyncio
async def test_daily_briefing_llm_failure():
    """LLM failure → early return (exception caught)."""
    ctx = _make_context()
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(return_value=ctx)

    mock_deliver = AsyncMock()

    with (
        patch("src.observer.manager.context_manager", mock_cm),
        patch("src.memory.soul.read_soul", return_value="# Soul"),
        patch(
            "src.scheduler.jobs.daily_briefing.retrieve_hybrid_memory",
            new=AsyncMock(return_value=_hybrid_memories()),
        ),
        patch("litellm.completion", side_effect=Exception("LLM API error")),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
    ):
        await run_daily_briefing()
        mock_deliver.assert_not_called()


@pytest.mark.asyncio
async def test_daily_briefing_empty_calendar_goals():
    """Empty calendar/goals → still generates briefing."""
    ctx = _make_context(upcoming_events=[], active_goals_summary="")
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(return_value=ctx)

    mock_deliver = AsyncMock()

    with (
        patch("src.observer.manager.context_manager", mock_cm),
        patch("src.memory.soul.read_soul", return_value="# Soul"),
        patch(
            "src.scheduler.jobs.daily_briefing.retrieve_hybrid_memory",
            new=AsyncMock(return_value=_hybrid_memories()),
        ),
        patch("litellm.completion", return_value=_mock_litellm_response("A quiet morning ahead.")),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
    ):
        await run_daily_briefing()
        mock_deliver.assert_called_once()
        msg = mock_deliver.call_args[0][0]
        assert "quiet morning" in msg.content


@pytest.mark.asyncio
async def test_daily_briefing_with_events():
    """Calendar events are included in the prompt."""
    ctx = _make_context(upcoming_events=[
        {"summary": "Team standup", "start": "09:00"},
        {"summary": "1:1 with manager", "start": "14:00"},
    ])
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(return_value=ctx)

    captured_prompt = {}

    def mock_completion(**kwargs):
        captured_prompt["messages"] = kwargs.get("messages", [])
        return _mock_litellm_response("Briefing with events...")

    mock_deliver = AsyncMock()

    with (
        patch("src.observer.manager.context_manager", mock_cm),
        patch("src.memory.soul.read_soul", return_value="# Soul"),
        patch(
            "src.scheduler.jobs.daily_briefing.retrieve_hybrid_memory",
            new=AsyncMock(return_value=_hybrid_memories()),
        ),
        patch("litellm.completion", side_effect=mock_completion),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
    ):
        await run_daily_briefing()

        # Check that events were included in the prompt
        prompt_text = captured_prompt["messages"][0]["content"]
        assert "Team standup" in prompt_text
        assert "1:1 with manager" in prompt_text


@pytest.mark.asyncio
async def test_daily_briefing_logs_degraded_runtime_details(async_db):
    ctx = _make_context()
    mock_cm = MagicMock()
    mock_cm.refresh = AsyncMock(return_value=ctx)

    with (
        patch("src.observer.manager.context_manager", mock_cm),
        patch("src.memory.soul.read_soul", return_value="# Soul\nName: Hero"),
        patch(
            "src.scheduler.jobs.daily_briefing.retrieve_hybrid_memory",
            new=AsyncMock(
                return_value=_hybrid_memories(
                    degraded=True,
                    reason="canonical_vector_search_unavailable",
                )
            ),
        ),
        patch("litellm.completion", return_value=_mock_litellm_response("Good morning, Hero! Here's your briefing...")),
        patch("src.observer.delivery.deliver_or_queue", AsyncMock()),
    ):
        await run_daily_briefing()

    events = await audit_repository.list_events(limit=20)
    assert any(
        event["event_type"] == "background_task_degraded"
        and event["tool_name"] == "daily_briefing_inputs"
        and event["details"]["source"] == "relevant_memories"
        and event["details"]["error"] == "canonical_vector_search_unavailable"
        for event in events
    )
    assert any(
        event["event_type"] == "scheduler_job_succeeded"
        and event["tool_name"] == "daily_briefing"
        and event["details"]["data_quality"] == "degraded"
        and event["details"]["degraded_inputs"] == ["relevant_memories"]
        for event in events
    )


@pytest.mark.asyncio
async def test_daily_briefing_does_not_use_raw_vector_rows_after_canonical_delete():
    """Briefing context must come from the canonical tombstone-filtered lane."""

    with (
        patch(
            "src.scheduler.jobs.daily_briefing.retrieve_hybrid_memory",
            new=AsyncMock(return_value=_hybrid_memories()),
        ),
        patch(
            "src.memory.vector_store.search_with_status",
            side_effect=AssertionError("daily briefing bypassed canonical retrieval"),
        ),
    ):
        memories, degraded = await _get_relevant_memories()

    assert memories == "No relevant memories yet."
    assert degraded is False
