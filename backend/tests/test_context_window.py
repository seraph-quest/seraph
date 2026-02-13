"""Tests for the token-aware context window."""

from unittest.mock import patch, MagicMock, PropertyMock

import pytest

from src.agent.context_window import (
    build_context_window,
    _format_messages,
    _count_tokens,
    _summary_cache,
)


def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content, "created_at": "2025-01-01T00:00:00"}


class TestFormatMessages:
    def test_formats_role_content(self):
        msgs = [_msg("user", "hello"), _msg("assistant", "hi")]
        result = _format_messages(msgs)
        assert result == "User: hello\nAssistant: hi"

    def test_empty_list(self):
        assert _format_messages([]) == ""

    def test_capitalizes_role(self):
        result = _format_messages([_msg("user", "test")])
        assert result.startswith("User:")


class TestCountTokens:
    def test_nonempty_string(self):
        count = _count_tokens("hello world")
        assert isinstance(count, int)
        assert count > 0

    def test_empty_string(self):
        assert _count_tokens("") == 0

    def test_longer_string_more_tokens(self):
        short = _count_tokens("hi")
        long = _count_tokens("this is a much longer sentence with more words")
        assert long > short


class TestBuildContextWindow:
    def test_empty_messages_returns_empty(self):
        assert build_context_window([]) == ""

    def test_small_conversation_returned_as_is(self):
        msgs = [_msg("user", "hi"), _msg("assistant", "hello")]
        result = build_context_window(msgs, token_budget=50000)
        assert "User: hi" in result
        assert "Assistant: hello" in result

    def test_fits_in_budget_returns_all(self):
        msgs = [_msg("user", f"msg {i}") for i in range(10)]
        result = build_context_window(msgs, token_budget=50000)
        for i in range(10):
            assert f"msg {i}" in result

    def test_over_budget_keeps_first_and_recent(self):
        # Create messages that will exceed budget
        msgs = [_msg("user", f"message number {i} " * 50) for i in range(100)]

        result = build_context_window(
            msgs,
            token_budget=500,  # very small budget to force summarization
            keep_first=2,
            keep_recent=5,
            session_id="test-session",
        )

        # First messages should be present
        assert "message number 0" in result
        assert "message number 1" in result

        # Recent messages should be present
        assert "message number 99" in result
        assert "message number 98" in result
        assert "message number 97" in result

        # Middle messages should be summarized, not present verbatim
        assert "[Summary of" in result

    @patch("src.agent.context_window._summarize_middle")
    def test_summarize_called_for_middle(self, mock_summarize):
        mock_summarize.return_value = "A summary of the middle."
        msgs = [_msg("user", f"word " * 200) for i in range(100)]

        result = build_context_window(
            msgs,
            token_budget=500,
            keep_first=2,
            keep_recent=5,
            session_id="test-sess",
        )

        mock_summarize.assert_called_once()
        assert "A summary of the middle." in result

    def test_budget_respected_for_few_messages(self):
        # If only 3 messages and keep_first=2, keep_recent=20, no middle to summarize
        msgs = [_msg("user", "hi"), _msg("assistant", "hey"), _msg("user", "bye")]
        result = build_context_window(
            msgs, token_budget=50000, keep_first=2, keep_recent=20
        )
        assert "User: hi" in result
        assert "User: bye" in result

    def test_keep_recent_larger_than_total(self):
        msgs = [_msg("user", "only one")]
        result = build_context_window(
            msgs, token_budget=50000, keep_first=2, keep_recent=20
        )
        assert "User: only one" in result


class TestSummaryCache:
    def setup_method(self):
        _summary_cache.clear()

    def test_cache_hit_skips_llm(self):
        from src.agent.context_window import _summarize_middle

        _summary_cache["sess:2-10"] = "cached summary"
        # If cache hits, litellm is never imported, so no mock needed
        result = _summarize_middle(
            [_msg("user", "test")], session_id="sess", range_key="2-10"
        )
        assert result == "cached summary"

    def test_cache_miss_with_fallback(self):
        """When litellm.completion raises, fallback truncation is used."""
        from src.agent.context_window import _summarize_middle

        mock_litellm = MagicMock()
        mock_litellm.completion.side_effect = Exception("no api key")

        with patch.dict("sys.modules", {"litellm": mock_litellm}):
            result = _summarize_middle(
                [_msg("user", "hello world")],
                session_id="sess",
                range_key="0-1",
            )
            assert "truncated" in result or len(result) > 0


class TestSettingsIntegration:
    """Tests that build_context_window reads defaults from settings."""

    def test_uses_settings_token_budget(self):
        """Large budget from settings keeps all messages."""
        msgs = [_msg("user", f"msg {i}") for i in range(10)]
        with patch("src.agent.context_window.settings") as mock_settings:
            mock_settings.context_window_token_budget = 999999
            mock_settings.context_window_keep_first = 2
            mock_settings.context_window_keep_recent = 20
            result = build_context_window(msgs)
        for i in range(10):
            assert f"msg {i}" in result

    def test_uses_settings_keep_recent(self):
        """Small keep_recent from settings limits recent messages."""
        msgs = [_msg("user", f"message number {i} " * 50) for i in range(30)]
        with patch("src.agent.context_window.settings") as mock_settings:
            mock_settings.context_window_token_budget = 500
            mock_settings.context_window_keep_first = 1
            mock_settings.context_window_keep_recent = 3
            result = build_context_window(msgs, session_id="test")
        # Most recent 3 messages should be present
        assert "message number 29" in result
        assert "message number 28" in result
        assert "message number 27" in result
        # First message kept
        assert "message number 0" in result
        # Middle should be summarized
        assert "[Summary of" in result

    def test_explicit_args_override_settings(self):
        """Explicit kwargs take precedence over settings values."""
        msgs = [_msg("user", f"msg {i}") for i in range(10)]
        with patch("src.agent.context_window.settings") as mock_settings:
            mock_settings.context_window_token_budget = 1  # would force summarization
            mock_settings.context_window_keep_first = 1
            mock_settings.context_window_keep_recent = 1
            # Explicit large budget overrides the tiny settings value
            result = build_context_window(msgs, token_budget=999999)
        for i in range(10):
            assert f"msg {i}" in result
