"""Tests for the async DB-backed SessionManager (src/agent/session.py)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agent.session import SessionManager
from src.audit.repository import audit_repository


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
