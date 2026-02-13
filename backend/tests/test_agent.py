from unittest.mock import MagicMock, patch

from src.agent.factory import create_agent, get_model, get_tools
from src.skills.loader import Skill


class TestAgentFactory:
    @patch("src.agent.factory.mcp_manager")
    def test_get_tools_returns_list(self, mock_mcp):
        mock_mcp.get_tools.return_value = []
        tools = get_tools()
        assert isinstance(tools, list)
        assert len(tools) >= 10
        tool_names = [t.name for t in tools]
        for expected in ["read_file", "write_file", "web_search", "fill_template",
                         "view_soul", "update_soul", "create_goal", "shell_execute"]:
            assert expected in tool_names

    @patch("src.agent.factory.LiteLLMModel")
    def test_get_model(self, mock_litellm_cls):
        mock_litellm_cls.return_value = MagicMock()
        model = get_model()
        mock_litellm_cls.assert_called_once()
        assert model is not None

    @patch("src.agent.factory.ToolCallingAgent")
    @patch("src.agent.factory.get_model")
    def test_create_agent(self, mock_get_model, mock_agent_cls):
        mock_get_model.return_value = MagicMock()
        mock_agent_cls.return_value = MagicMock()

        agent = create_agent()
        mock_agent_cls.assert_called_once()
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
