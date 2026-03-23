"""Tests for the async DB-backed SessionManager (src/agent/session.py)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agent.session import SessionManager
from src.audit.repository import audit_repository
from src.scheduler.scheduled_jobs import scheduled_job_repository


@pytest.fixture
def sm():
    return SessionManager()


class TestGetOrCreate:
    async def test_creates_new_without_id(self, async_db, sm):
        session = await sm.get_or_create()
        assert session.id
        assert session.title == "New Conversation"

    async def test_creates_new_with_id(self, async_db, sm):
        session = await sm.get_or_create("my-sess")
        assert session.id == "my-sess"

    async def test_returns_existing(self, async_db, sm):
        s1 = await sm.get_or_create("s1")
        s2 = await sm.get_or_create("s1")
        assert s1.id == s2.id

    async def test_creates_when_id_not_found(self, async_db, sm):
        session = await sm.get_or_create("new-id")
        assert session.id == "new-id"


class TestGet:
    async def test_existing(self, async_db, sm):
        await sm.get_or_create("s1")
        assert (await sm.get("s1")) is not None

    async def test_nonexistent(self, async_db, sm):
        assert (await sm.get("nope")) is None


class TestDelete:
    async def test_success(self, async_db, sm):
        await sm.get_or_create("s1")
        assert await sm.delete("s1") is True
        assert await sm.get("s1") is None

    async def test_nonexistent(self, async_db, sm):
        assert await sm.delete("nope") is False

    async def test_deletes_messages(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "hello")
        await sm.delete("s1")
        msgs = await sm.get_messages("s1")
        assert msgs == []

    async def test_deletes_todos(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.replace_todos("s1", [{"content": "Ship Wave 1", "completed": False}])
        await sm.delete("s1")
        todos = await sm.get_todos("s1")
        assert todos == []

    async def test_deletes_scheduled_jobs(self, async_db, sm):
        await sm.get_or_create("s1")
        await scheduled_job_repository.create_job(
            name="Morning check",
            cron="0 9 * * *",
            timezone_name="UTC",
            target_type="message",
            content="Stand up and review priorities.",
            intervention_type="advisory",
            urgency=3,
            workflow_name="",
            workflow_args_json="",
            session_id="s1",
            created_by_session_id="s1",
        )
        await sm.delete("s1")
        jobs = await scheduled_job_repository.list_jobs(limit=20)
        assert jobs == []


class TestAddMessage:
    async def test_basic(self, async_db, sm):
        await sm.get_or_create("s1")
        msg = await sm.add_message("s1", "user", "hello")
        assert msg.role == "user"
        assert msg.content == "hello"
        assert msg.session_id == "s1"

    async def test_with_metadata(self, async_db, sm):
        await sm.get_or_create("s1")
        msg = await sm.add_message(
            "s1", "step", "called tool", step_number=1, tool_used="web_search"
        )
        assert msg.step_number == 1
        assert msg.tool_used == "web_search"


class TestGetHistoryText:
    async def test_empty(self, async_db, sm):
        await sm.get_or_create("s1")
        assert await sm.get_history_text("s1") == ""

    async def test_formatted(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "Hello")
        await sm.add_message("s1", "assistant", "Hi!")
        text = await sm.get_history_text("s1")
        assert "User: Hello" in text
        assert "Assistant: Hi!" in text

    async def test_excludes_step_messages(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "Hello")
        await sm.add_message("s1", "step", "Thinking...")
        await sm.add_message("s1", "assistant", "Hi!")
        text = await sm.get_history_text("s1")
        assert "Thinking..." not in text


class TestGetMessages:
    async def test_pagination(self, async_db, sm):
        await sm.get_or_create("s1")
        for i in range(5):
            await sm.add_message("s1", "user", f"msg-{i}")
        page = await sm.get_messages("s1", limit=2, offset=1)
        assert len(page) == 2
        assert page[0]["content"] == "msg-1"


class TestTodos:
    async def test_replace_and_list(self, async_db, sm):
        await sm.get_or_create("s1")
        todos = await sm.replace_todos(
            "s1",
            [
                {"content": "Clarify missing input", "completed": False},
                {"content": "Run validation", "completed": True},
            ],
        )
        assert [todo["content"] for todo in todos] == ["Clarify missing input", "Run validation"]
        assert todos[1]["completed"] is True

    async def test_append_and_complete_by_index(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.replace_todos("s1", [{"content": "First", "completed": False}])
        todos = await sm.append_todos("s1", [{"content": "Second", "completed": False}])
        assert [todo["content"] for todo in todos] == ["First", "Second"]

        updated = await sm.update_todo_completion("s1", "2", completed=True)
        assert updated[1]["completed"] is True

    async def test_remove_reorders(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.replace_todos(
            "s1",
            [
                {"content": "One", "completed": False},
                {"content": "Two", "completed": False},
                {"content": "Three", "completed": False},
            ],
        )
        remaining = await sm.remove_todo("s1", "2")
        assert [todo["content"] for todo in remaining] == ["One", "Three"]
        assert [todo["sort_order"] for todo in remaining] == [0, 1]

    async def test_invalid_todo_index_returns_none(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.replace_todos("s1", [{"content": "One", "completed": False}])
        assert await sm.update_todo_completion("s1", "0", completed=True) is None
        assert await sm.remove_todo("s1", "3") is None


class TestListSessions:
    async def test_empty(self, async_db, sm):
        result = await sm.list_sessions()
        assert result == []

    async def test_with_sessions(self, async_db, sm):
        await sm.get_or_create("a")
        await sm.get_or_create("b")
        result = await sm.list_sessions()
        assert len(result) == 2
        ids = {s["id"] for s in result}
        assert ids == {"a", "b"}

    async def test_includes_last_message(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "Hello world")
        result = await sm.list_sessions()
        assert result[0]["last_message"] == "Hello world"


class TestSearchSessions:
    async def test_matches_message_content(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "Need a weather briefing for Wroclaw")
        await sm.get_or_create("s2")
        await sm.add_message("s2", "assistant", "We reviewed the roadmap")

        results = await sm.search_sessions("weather")
        assert len(results) == 1
        assert results[0]["session_id"] == "s1"
        assert results[0]["source"] == "message"

    async def test_matches_titles_and_excludes_current(self, async_db, sm):
        await sm.get_or_create("weather-thread")
        await sm.update_title("weather-thread", "Weather planning")
        await sm.get_or_create("active-thread")
        await sm.update_title("active-thread", "Weather follow-up")

        results = await sm.search_sessions("weather", exclude_session_id="active-thread")
        assert [item["session_id"] for item in results] == ["weather-thread"]
        assert results[0]["source"] == "title"

    async def test_treats_percent_and_underscore_as_literal_text(self, async_db, sm):
        await sm.get_or_create("percent")
        await sm.add_message("percent", "assistant", "Budget hit 100% yesterday")
        await sm.get_or_create("underscore")
        await sm.add_message("underscore", "assistant", "Follow up on foo_bar details")
        await sm.get_or_create("plain")
        await sm.add_message("plain", "assistant", "No wildcard markers here")

        percent_results = await sm.search_sessions("100%")
        underscore_results = await sm.search_sessions("foo_bar")

        assert [item["session_id"] for item in percent_results] == ["percent"]
        assert [item["session_id"] for item in underscore_results] == ["underscore"]

    async def test_snippet_includes_late_match_context(self, async_db, sm):
        await sm.get_or_create("late-match")
        prefix = "x" * 220
        await sm.add_message(
            "late-match",
            "assistant",
            f"{prefix} Weather signal appears near the end of this message.",
        )

        results = await sm.search_sessions("weather")

        assert results[0]["session_id"] == "late-match"
        assert "Weather signal" in results[0]["snippet"]
        assert results[0]["snippet"].startswith("...")

    async def test_title_hits_rank_by_conversation_recency_not_todo_mutation(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.update_title("s1", "Weather planning")
        await sm.add_message("s1", "assistant", "Older weather thread")

        await sm.get_or_create("s2")
        await sm.update_title("s2", "Weather review")
        await sm.add_message("s2", "assistant", "Newer weather thread")

        await sm.replace_todos("s1", [{"content": "Unrelated checklist", "completed": False}])

        results = await sm.search_sessions("weather")

        assert [item["session_id"] for item in results[:2]] == ["s2", "s1"]


class TestRecentSessionSummary:
    async def test_orders_by_conversation_recency_not_todo_mutation(self, async_db, sm):
        await sm.get_or_create("stale")
        await sm.update_title("stale", "Older thread")
        await sm.add_message("stale", "assistant", "Older conversation")

        await sm.get_or_create("fresh")
        await sm.update_title("fresh", "Newer thread")
        await sm.add_message("fresh", "assistant", "Newer conversation")

        await sm.replace_todos("stale", [{"content": "Unrelated checklist", "completed": False}])

        summary = await sm.get_recent_sessions_summary(limit_sessions=2)

        assert summary.splitlines()[0].startswith("- Newer thread:")
        assert summary.splitlines()[1].startswith("- Older thread:")


class TestUpdateTitle:
    async def test_success(self, async_db, sm):
        await sm.get_or_create("s1")
        assert await sm.update_title("s1", "My Chat") is True

    async def test_nonexistent(self, async_db, sm):
        assert await sm.update_title("nope", "title") is False


class TestCountMessages:
    async def test_counts_user_and_assistant(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "hi")
        await sm.add_message("s1", "assistant", "hello")
        await sm.add_message("s1", "step", "thinking")
        assert await sm.count_messages("s1") == 2


class TestGenerateTitle:
    async def test_generates_title(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "Tell me about AI")
        await sm.add_message("s1", "assistant", "AI is fascinating")

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "AI Discussion"

        with patch("litellm.completion", return_value=mock_response):
            title = await sm.generate_title("s1")

        assert title == "AI Discussion"
        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "background_task_succeeded"
            and event["tool_name"] == "session_title_generation"
            and event["session_id"] == "s1"
            and event["details"]["title_length"] == len("AI Discussion")
            for event in events
        )
        assert any(
            event["event_type"] == "llm_primary_success"
            and event["tool_name"] == "llm_runtime"
            and event["session_id"] == "s1"
            and event["details"]["runtime_path"] == "session_title_generation"
            and event["details"]["request_id"]
            for event in events
        )

    async def test_generates_title_uses_session_title_runtime_path(self, async_db, sm):
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "Tell me about AI")
        await sm.add_message("s1", "assistant", "AI is fascinating")

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "AI Discussion"

        with patch("src.llm_runtime.completion_with_fallback", AsyncMock(return_value=mock_response)) as mock_completion:
            title = await sm.generate_title("s1")

        assert title == "AI Discussion"
        assert mock_completion.await_args.kwargs["runtime_path"] == "session_title_generation"

    async def test_skips_non_default_title(self, async_db, sm):
        s = await sm.get_or_create("s1")
        await sm.update_title("s1", "Custom Title")
        # Need to re-fetch since generate_title reads from DB
        title = await sm.generate_title("s1")
        assert title == "Custom Title"
        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "background_task_skipped"
            and event["tool_name"] == "session_title_generation"
            and event["session_id"] == "s1"
            and event["details"]["reason"] == "title_already_set"
            for event in events
        )
