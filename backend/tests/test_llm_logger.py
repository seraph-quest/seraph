"""Tests for LLM call logging (src/llm_logger.py)."""

import json
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture()
def log_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


def _make_settings(log_dir, *, enabled=True, content=False):
    mock = MagicMock()
    mock.llm_log_enabled = enabled
    mock.llm_log_content = content
    mock.llm_log_dir = log_dir
    mock.llm_log_max_bytes = 10_000_000
    mock.llm_log_backup_count = 1
    return mock


def _make_kwargs(*, messages=None, stream=False, exception=None):
    """Build a minimal kwargs dict resembling what LiteLLM passes to callbacks."""
    slo = {
        "model": "openrouter/anthropic/claude-sonnet-4",
        "call_type": "completion",
        "custom_llm_provider": "openrouter",
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "response_cost": 0.0012,
        "api_base": "https://openrouter.ai/api/v1",
    }
    if exception:
        slo["error_str"] = str(exception)
    kwargs = {
        "model": "openrouter/anthropic/claude-sonnet-4",
        "stream": stream,
        "standard_logging_object": slo,
    }
    if messages:
        kwargs["messages"] = messages
    if exception:
        kwargs["exception"] = exception
    return kwargs


def _make_response():
    msg = MagicMock()
    msg.content = "Hello, world!"
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def _read_log(log_dir):
    path = os.path.join(log_dir, "llm_calls.jsonl")
    with open(path) as f:
        lines = [json.loads(line) for line in f if line.strip()]
    return lines


class TestSuccessEvent:
    def test_writes_valid_jsonl(self, log_dir):
        """Success event writes a JSONL line with expected fields."""
        with patch("src.llm_logger.settings", _make_settings(log_dir)):
            from src.llm_logger import SeraphLLMLogger

            lg = SeraphLLMLogger()
            start = datetime.now(timezone.utc)
            end = datetime.now(timezone.utc)
            lg.log_success_event(
                _make_kwargs(),
                _make_response(),
                start,
                end,
            )

        entries = _read_log(log_dir)
        assert len(entries) == 1
        e = entries[0]
        assert e["status"] == "success"
        assert e["model"] == "openrouter/anthropic/claude-sonnet-4"
        assert e["tokens"]["input"] == 100
        assert e["tokens"]["output"] == 50
        assert e["tokens"]["total"] == 150
        assert e["cost_usd"] == 0.0012
        assert "latency_ms" in e
        assert "error" not in e

    def test_content_logging_off(self, log_dir):
        """When llm_log_content=False, messages and response are excluded."""
        with patch("src.llm_logger.settings", _make_settings(log_dir, content=False)):
            from src.llm_logger import SeraphLLMLogger

            lg = SeraphLLMLogger()
            lg.log_success_event(
                _make_kwargs(messages=[{"role": "user", "content": "hi"}]),
                _make_response(),
                datetime.now(timezone.utc),
                datetime.now(timezone.utc),
            )

        e = _read_log(log_dir)[0]
        assert "messages" not in e
        assert "response" not in e

    def test_content_logging_on(self, log_dir):
        """When llm_log_content=True, messages and response are included."""
        with patch("src.llm_logger.settings", _make_settings(log_dir, content=True)):
            from src.llm_logger import SeraphLLMLogger

            lg = SeraphLLMLogger()
            lg.log_success_event(
                _make_kwargs(messages=[{"role": "user", "content": "hi"}]),
                _make_response(),
                datetime.now(timezone.utc),
                datetime.now(timezone.utc),
            )

        e = _read_log(log_dir)[0]
        assert e["messages"] == [{"role": "user", "content": "hi"}]
        assert e["response"] == "Hello, world!"


class TestFailureEvent:
    def test_includes_error(self, log_dir):
        """Failure event includes error string."""
        with patch("src.llm_logger.settings", _make_settings(log_dir)):
            from src.llm_logger import SeraphLLMLogger

            lg = SeraphLLMLogger()
            lg.log_failure_event(
                _make_kwargs(exception=RuntimeError("rate limited")),
                None,
                datetime.now(timezone.utc),
                datetime.now(timezone.utc),
            )

        e = _read_log(log_dir)[0]
        assert e["status"] == "failure"
        assert "rate limited" in e["error"]


class TestErrorSafety:
    def test_malformed_data_does_not_raise(self, log_dir):
        """Callback never raises even with broken data."""
        with patch("src.llm_logger.settings", _make_settings(log_dir)):
            from src.llm_logger import SeraphLLMLogger

            lg = SeraphLLMLogger()
            # Pass completely empty/None args — should not raise
            lg.log_success_event({}, None, None, None)
            lg.log_failure_event({}, None, None, None)


class TestInitLLMLogging:
    def test_disabled_does_not_register(self, log_dir):
        """init_llm_logging() with disabled setting doesn't append callback."""
        import litellm

        before = len(litellm.callbacks)
        with patch("src.llm_logger.settings", _make_settings(log_dir, enabled=False)):
            from src.llm_logger import init_llm_logging

            init_llm_logging()

        assert len(litellm.callbacks) == before

    def test_enabled_registers_callback(self, log_dir):
        """init_llm_logging() appends a SeraphLLMLogger to litellm.callbacks."""
        import litellm

        before = len(litellm.callbacks)
        with patch("src.llm_logger.settings", _make_settings(log_dir, enabled=True)):
            from src.llm_logger import init_llm_logging

            init_llm_logging()

        assert len(litellm.callbacks) == before + 1
        # Clean up so other tests aren't affected
        litellm.callbacks.pop()
