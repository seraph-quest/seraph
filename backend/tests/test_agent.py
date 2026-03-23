import asyncio
from unittest.mock import MagicMock, patch

import pytest

from src.agent.factory import create_agent, get_model, get_tools
from src.approval.exceptions import ApprovalRequired
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.audit.repository import audit_repository
from src.observer.context import CurrentContext
from src.skills.loader import Skill


class TestAgentFactory:
    @patch("src.agent.factory.mcp_manager")
    @patch("src.tools.policy.context_manager.get_context", return_value=CurrentContext(tool_policy_mode="full", mcp_policy_mode="full"))
    def test_get_tools_returns_list(self, _mock_context, mock_mcp):
        mock_mcp.get_tools.return_value = []
        with patch("src.agent.factory.settings.use_delegation", False):
            tools = get_tools()
        assert isinstance(tools, list)
        assert len(tools) >= 10
        tool_names = [t.name for t in tools]
        for expected in ["read_file", "write_file", "web_search", "fill_template",
                         "view_soul", "update_soul", "create_goal", "execute_code", "clarify", "todo", "session_search"]:
            assert expected in tool_names
        assert "delegate_task" not in tool_names

    @patch("src.agent.factory.mcp_manager")
    @patch("src.tools.policy.context_manager.get_context", return_value=CurrentContext(tool_policy_mode="full", mcp_policy_mode="full"))
    def test_get_tools_includes_delegate_task_when_delegation_is_enabled(self, _mock_context, mock_mcp):
        mock_mcp.get_tools.return_value = []
        with patch("src.agent.factory.settings.use_delegation", True):
            tools = get_tools()

        assert "delegate_task" in {tool.name for tool in tools}

    @patch("src.agent.factory.LiteLLMModel")
    def test_get_model(self, mock_litellm_cls):
        mock_litellm_cls.return_value = MagicMock()
        model = get_model()
        mock_litellm_cls.assert_called_once()
        assert model is not None

    @patch("src.agent.factory.LiteLLMModel")
    def test_get_model_uses_local_profile_for_chat_runtime_path(self, mock_litellm_cls):
        mock_litellm_cls.return_value = MagicMock()

        from config.settings import settings
        with (
            patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
            patch.object(settings, "llm_api_key", "primary-key"),
            patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
            patch.object(settings, "local_model", "ollama/llama3.2"),
            patch.object(settings, "local_llm_api_key", ""),
            patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
            patch.object(settings, "local_runtime_paths", "chat_agent"),
        ):
            get_model()

        call_kwargs = mock_litellm_cls.call_args[1]
        assert call_kwargs["model_id"] == "ollama/llama3.2"
        assert call_kwargs["api_base"] == "http://localhost:11434/v1"

    @patch("src.agent.factory.mcp_manager")
    @patch("src.tools.policy.context_manager.get_context", return_value=CurrentContext(tool_policy_mode="full", mcp_policy_mode="full"))
    def test_get_tools_wraps_execute_code_for_approval(self, _mock_context, mock_mcp, async_db):
        mock_mcp.get_tools.return_value = []
        tools = {tool.name: tool for tool in get_tools()}
        tokens = set_runtime_context("s1", "high_risk")
        try:
            with pytest.raises(ApprovalRequired):
                tools["execute_code"](code="print('hi')")
        finally:
            reset_runtime_context(tokens)

    @patch("src.tools.shell_tool.httpx.Client")
    @patch("src.agent.factory.mcp_manager")
    @patch("src.tools.policy.context_manager.get_context", return_value=CurrentContext(tool_policy_mode="full", mcp_policy_mode="full"))
    def test_get_tools_runs_execute_code_through_audit_wrapper(self, _mock_context, mock_mcp, MockClient, async_db):
        mock_mcp.get_tools.return_value = []
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"stdout": "wrapped\n", "returncode": 0}
        mock_resp.raise_for_status = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(
            return_value=MagicMock(post=MagicMock(return_value=mock_resp))
        )
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        tools = {tool.name: tool for tool in get_tools()}
        tokens = set_runtime_context("s1", "off")
        try:
            assert "wrapped" in tools["execute_code"](code="print('wrapped')")
        finally:
            reset_runtime_context(tokens)

        async def _fetch():
            events = await audit_repository.list_events(limit=10)
            return [event for event in events if event["tool_name"] == "execute_code"]

        events = asyncio.run(_fetch())
        event_types = {event["event_type"] for event in events}
        assert "tool_call" in event_types
        assert "tool_result" in event_types

    @patch("src.agent.factory.mcp_manager")
    @patch("src.tools.policy.context_manager.get_context", return_value=CurrentContext(tool_policy_mode="full", mcp_policy_mode="full"))
    def test_get_tools_todo_invalid_action_does_not_reuse_stale_audit_payload(self, _mock_context, mock_mcp, async_db):
        mock_mcp.get_tools.return_value = []
        tools = {tool.name: tool for tool in get_tools()}
        tokens = set_runtime_context("s1", "off")
        try:
            tools["todo"](action="set", items="First")
            error = tools["todo"](action="bogus")
        finally:
            reset_runtime_context(tokens)

        assert error == "Error: Unsupported todo action. Use list, set, add, complete, reopen, remove, or clear."

        async def _fetch():
            events = await audit_repository.list_events(limit=10)
            return [event for event in events if event["tool_name"] == "todo" and event["event_type"] == "tool_result"]

        results = asyncio.run(_fetch())
        assert len(results) == 2
        set_result = next(result for result in results if result["details"].get("action") == "set")
        assert set_result["details"]["items"][0]["completed"] is False
        assert "content" not in set_result["details"]["items"][0]
        latest = results[0]
        assert latest["summary"].startswith("todo returned")
        assert "action" not in latest["details"]

    @patch("src.agent.factory.ToolCallingAgent")
    @patch("src.agent.factory.get_model")
    def test_create_agent(self, mock_get_model, mock_agent_cls):
        mock_get_model.return_value = MagicMock()
        mock_agent_cls.return_value = MagicMock()

        agent = create_agent()
        mock_get_model.assert_called_once_with(runtime_path="chat_agent")
        mock_agent_cls.assert_called_once()
        instructions = mock_agent_cls.call_args[1]["instructions"]
        assert "productivity, performance, health, influence, and growth" in instructions
        assert "relationships, and growth" not in instructions
        assert "Treat relationship or collaboration priorities as influence" in instructions
        assert agent is not None

    @patch("src.agent.factory.ToolCallingAgent")
    @patch("src.agent.factory.get_model")
    def test_create_agent_with_context(self, mock_get_model, mock_agent_cls):
        mock_get_model.return_value = MagicMock()
        mock_agent_cls.return_value = MagicMock()

        create_agent(additional_context="User: Hello\nAssistant: Hi!")
        call_kwargs = mock_agent_cls.call_args[1]
        assert "CONVERSATION HISTORY" in call_kwargs["instructions"]
        assert "User: Hello" in call_kwargs["instructions"]

    @patch("src.agent.factory.skill_manager")
    @patch("src.agent.factory.ToolCallingAgent")
    @patch("src.agent.factory.get_model")
    def test_create_agent_with_skills(self, mock_get_model, mock_agent_cls, mock_skill_mgr):
        mock_get_model.return_value = MagicMock()
        mock_agent_cls.return_value = MagicMock()
        mock_skill_mgr.get_active_skills.return_value = [
            Skill(
                name="test-skill",
                description="A test skill",
                instructions="Do the thing.",
                requires_tools=["web_search"],
                user_invocable=True,
                enabled=True,
            )
        ]

        create_agent()
        call_kwargs = mock_agent_cls.call_args[1]
        assert "Available Skills" in call_kwargs["instructions"]
        assert "### Skill: test-skill [user-invocable]" in call_kwargs["instructions"]
        assert "Do the thing." in call_kwargs["instructions"]

    @patch("src.agent.factory.skill_manager")
    @patch("src.agent.factory.ToolCallingAgent")
    @patch("src.agent.factory.get_model")
    def test_create_agent_no_skills(self, mock_get_model, mock_agent_cls, mock_skill_mgr):
        mock_get_model.return_value = MagicMock()
        mock_agent_cls.return_value = MagicMock()
        mock_skill_mgr.get_active_skills.return_value = []

        create_agent()
        call_kwargs = mock_agent_cls.call_args[1]
        assert "Available Skills" not in call_kwargs["instructions"]

    @patch("src.agent.factory.ToolCallingAgent")
    @patch("src.agent.factory.get_model")
    def test_create_agent_with_observer_context(self, mock_get_model, mock_agent_cls):
        mock_get_model.return_value = MagicMock()
        mock_agent_cls.return_value = MagicMock()

        create_agent(observer_context="User is in: VS Code — main.py\nTime: morning (Monday)")
        call_kwargs = mock_agent_cls.call_args[1]
        assert "CURRENT CONTEXT" in call_kwargs["instructions"]
        assert "VS Code" in call_kwargs["instructions"]

    @patch("src.agent.factory.ToolCallingAgent")
    @patch("src.agent.factory.get_model")
    def test_create_agent_empty_observer_context_omitted(self, mock_get_model, mock_agent_cls):
        mock_get_model.return_value = MagicMock()
        mock_agent_cls.return_value = MagicMock()

        create_agent(observer_context="")
        call_kwargs = mock_agent_cls.call_args[1]
        assert "CURRENT CONTEXT" not in call_kwargs["instructions"]
