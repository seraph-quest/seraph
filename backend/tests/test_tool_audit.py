import asyncio
from unittest.mock import patch

import pytest
from smolagents import Tool

from config.settings import settings
from src.agent.onboarding import create_onboarding_agent
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.audit.repository import audit_repository
from src.observer.context import CurrentContext
from src.tools.audit import AuditedTool, wrap_tools_for_audit


class DummyEchoTool(Tool):
    name = "shell_execute"
    description = "Dummy echo tool"
    inputs = {"code": {"type": "string", "description": "Code to run"}}
    output_type = "string"

    def forward(self, code: str) -> str:
        return f"ran:{code}"


class DummyFailTool(Tool):
    name = "shell_execute"
    description = "Dummy failing tool"
    inputs = {"code": {"type": "string", "description": "Code to run"}}
    output_type = "string"

    def forward(self, code: str) -> str:
        raise RuntimeError(f"failed:{code}")


def _tool_context() -> CurrentContext:
    return CurrentContext(tool_policy_mode="full", mcp_policy_mode="full")


def test_audited_tool_logs_call_and_result(async_db):
    tool = wrap_tools_for_audit([DummyEchoTool()])[0]
    tokens = set_runtime_context("s1", "high_risk")

    try:
        with patch("src.tools.policy.context_manager.get_context", return_value=_tool_context()):
            assert tool(code="print('hi')") == "ran:print('hi')"
    finally:
        reset_runtime_context(tokens)

    async def _fetch():
        events = await audit_repository.list_events(limit=10)
        return events

    events = asyncio.run(_fetch())
    tool_calls = [e for e in events if e["event_type"] == "tool_call"]
    tool_results = [e for e in events if e["event_type"] == "tool_result"]
    assert len(tool_calls) == 1
    assert len(tool_results) == 1
    assert tool_calls[0]["tool_name"] == "shell_execute"
    assert tool_results[0]["details"]["content_redacted"] is True


def test_audited_tool_logs_failure(async_db):
    tool = wrap_tools_for_audit([DummyFailTool()])[0]
    tokens = set_runtime_context("s1", "high_risk")

    try:
        with patch("src.tools.policy.context_manager.get_context", return_value=_tool_context()):
            with pytest.raises(RuntimeError, match="failed:print"):
                tool(code="print('hi')")
    finally:
        reset_runtime_context(tokens)

    async def _fetch():
        events = await audit_repository.list_events(limit=10)
        return events

    events = asyncio.run(_fetch())
    tool_calls = [e for e in events if e["event_type"] == "tool_call"]
    tool_failures = [e for e in events if e["event_type"] == "tool_failed"]
    assert len(tool_calls) == 1
    assert len(tool_failures) == 1
    assert tool_failures[0]["tool_name"] == "shell_execute"
    assert tool_failures[0]["details"]["error"] == "failed:print('hi')"


def test_audited_tool_logging_fails_open(async_db):
    tool = wrap_tools_for_audit([DummyEchoTool()])[0]
    tokens = set_runtime_context("s1", "high_risk")

    try:
        with patch("src.tools.policy.context_manager.get_context", return_value=_tool_context()):
            with patch("src.tools.audit.audit_repository.log_event", side_effect=RuntimeError("audit down")):
                assert tool(code="print('hi')") == "ran:print('hi')"
    finally:
        reset_runtime_context(tokens)


def test_onboarding_agent_uses_audited_tools():
    agent = create_onboarding_agent()

    for tool_name in ("view_soul", "update_soul", "create_goal", "get_goals"):
        assert isinstance(agent.tools[tool_name], AuditedTool)


@patch("src.agent.onboarding.LiteLLMModel")
def test_onboarding_agent_uses_local_profile_runtime_path(mock_model_cls):
    mock_model_cls.return_value = object()
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", ""),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "local_runtime_paths", "onboarding_agent"),
    ):
        create_onboarding_agent()

    call_kwargs = mock_model_cls.call_args[1]
    assert call_kwargs["model_id"] == "ollama/llama3.2"
    assert call_kwargs["api_base"] == "http://localhost:11434/v1"
