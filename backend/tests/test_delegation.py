"""Tests for the delegation architecture feature flag and orchestrator."""

from unittest.mock import MagicMock, patch

from config.settings import settings
from src.agent.factory import build_agent, create_orchestrator
from src.api.ws import _format_tool_step


class TestFeatureFlag:
    @patch("src.agent.factory.create_agent")
    def test_flag_off_calls_create_agent(self, mock_create):
        mock_create.return_value = MagicMock()
        with patch.object(settings, "use_delegation", False):
            agent = build_agent(soul_context="test soul")
        mock_create.assert_called_once_with("", "test soul", "")
        assert agent is mock_create.return_value

    @patch("src.agent.factory.create_orchestrator")
    def test_flag_on_calls_create_orchestrator(self, mock_create_orch):
        mock_create_orch.return_value = MagicMock()
        with patch.object(settings, "use_delegation", True):
            agent = build_agent(soul_context="test soul")
        mock_create_orch.assert_called_once_with("", "test soul", "")
        assert agent is mock_create_orch.return_value


class TestCreateOrchestrator:
    @patch("src.agent.factory.ToolCallingAgent")
    @patch("src.agent.factory.get_model")
    @patch("src.agent.factory.skill_manager")
    @patch("src.agent.specialists.ToolCallingAgent")
    @patch("src.agent.specialists.LiteLLMModel")
    @patch("src.agent.specialists.mcp_manager")
    @patch("src.agent.specialists.discover_tools")
    def test_orchestrator_has_no_tools(
        self, mock_discover, mock_mcp, mock_spec_model, mock_spec_agent,
        mock_skill_mgr, mock_get_model, mock_agent_cls,
    ):
        # Set up specialist mocks
        mock_spec_model.return_value = MagicMock()
        mock_mcp.get_config.return_value = []
        mock_skill_mgr.get_active_skills.return_value = []

        from src.agent.specialists import TOOL_DOMAINS
        tools = []
        for name in TOOL_DOMAINS:
            tool = MagicMock()
            tool.name = name
            tools.append(tool)
        mock_discover.return_value = tools

        specialist_agents = []

        def _make_specialist(**kwargs):
            agent = MagicMock()
            agent.name = kwargs.get("name", "unknown")
            agent.tools = kwargs.get("tools", [])
            specialist_agents.append(agent)
            return agent

        mock_spec_agent.side_effect = _make_specialist

        # Set up orchestrator mock
        mock_get_model.return_value = MagicMock()
        mock_orch = MagicMock()
        mock_agent_cls.return_value = mock_orch

        create_orchestrator(soul_context="test soul")

        # Orchestrator should be created with tools=[]
        orch_kwargs = mock_agent_cls.call_args[1]
        assert orch_kwargs["tools"] == []

    @patch("src.agent.factory.ToolCallingAgent")
    @patch("src.agent.factory.get_model")
    @patch("src.agent.factory.skill_manager")
    @patch("src.agent.specialists.ToolCallingAgent")
    @patch("src.agent.specialists.LiteLLMModel")
    @patch("src.agent.specialists.mcp_manager")
    @patch("src.agent.specialists.discover_tools")
    def test_orchestrator_receives_managed_agents(
        self, mock_discover, mock_mcp, mock_spec_model, mock_spec_agent,
        mock_skill_mgr, mock_get_model, mock_agent_cls,
    ):
        mock_spec_model.return_value = MagicMock()
        mock_mcp.get_config.return_value = []
        mock_skill_mgr.get_active_skills.return_value = []

        from src.agent.specialists import TOOL_DOMAINS
        tools = []
        for name in TOOL_DOMAINS:
            tool = MagicMock()
            tool.name = name
            tools.append(tool)
        mock_discover.return_value = tools

        def _make_specialist(**kwargs):
            agent = MagicMock()
            agent.name = kwargs.get("name", "unknown")
            agent.tools = kwargs.get("tools", [])
            return agent

        mock_spec_agent.side_effect = _make_specialist
        mock_get_model.return_value = MagicMock()
        mock_agent_cls.return_value = MagicMock()

        create_orchestrator()

        orch_kwargs = mock_agent_cls.call_args[1]
        assert len(orch_kwargs["managed_agents"]) == 4

    @patch("src.agent.factory.ToolCallingAgent")
    @patch("src.agent.factory.get_model")
    @patch("src.agent.factory.skill_manager")
    @patch("src.agent.specialists.ToolCallingAgent")
    @patch("src.agent.specialists.LiteLLMModel")
    @patch("src.agent.specialists.mcp_manager")
    @patch("src.agent.specialists.discover_tools")
    def test_orchestrator_includes_soul_and_memory(
        self, mock_discover, mock_mcp, mock_spec_model, mock_spec_agent,
        mock_skill_mgr, mock_get_model, mock_agent_cls,
    ):
        mock_spec_model.return_value = MagicMock()
        mock_mcp.get_config.return_value = []
        mock_skill_mgr.get_active_skills.return_value = []

        from src.agent.specialists import TOOL_DOMAINS
        tools = []
        for name in TOOL_DOMAINS:
            tool = MagicMock()
            tool.name = name
            tools.append(tool)
        mock_discover.return_value = tools

        def _make_specialist(**kwargs):
            agent = MagicMock()
            agent.name = kwargs.get("name", "unknown")
            agent.tools = kwargs.get("tools", [])
            return agent

        mock_spec_agent.side_effect = _make_specialist
        mock_get_model.return_value = MagicMock()
        mock_agent_cls.return_value = MagicMock()

        create_orchestrator(
            soul_context="user soul content",
            memory_context="relevant memories",
        )

        orch_kwargs = mock_agent_cls.call_args[1]
        assert "USER IDENTITY" in orch_kwargs["instructions"]
        assert "user soul content" in orch_kwargs["instructions"]
        assert "RELEVANT MEMORIES" in orch_kwargs["instructions"]
        assert "relevant memories" in orch_kwargs["instructions"]

    @patch("src.agent.factory.ToolCallingAgent")
    @patch("src.agent.factory.get_model")
    @patch("src.agent.factory.skill_manager")
    @patch("src.agent.specialists.ToolCallingAgent")
    @patch("src.agent.specialists.LiteLLMModel")
    @patch("src.agent.specialists.mcp_manager")
    @patch("src.agent.specialists.discover_tools")
    def test_orchestrator_uses_settings_max_steps(
        self, mock_discover, mock_mcp, mock_spec_model, mock_spec_agent,
        mock_skill_mgr, mock_get_model, mock_agent_cls,
    ):
        mock_spec_model.return_value = MagicMock()
        mock_mcp.get_config.return_value = []
        mock_skill_mgr.get_active_skills.return_value = []

        from src.agent.specialists import TOOL_DOMAINS
        tools = []
        for name in TOOL_DOMAINS:
            tool = MagicMock()
            tool.name = name
            tools.append(tool)
        mock_discover.return_value = tools

        def _make_specialist(**kwargs):
            agent = MagicMock()
            agent.name = kwargs.get("name", "unknown")
            agent.tools = kwargs.get("tools", [])
            return agent

        mock_spec_agent.side_effect = _make_specialist
        mock_get_model.return_value = MagicMock()
        mock_agent_cls.return_value = MagicMock()

        create_orchestrator()

        orch_kwargs = mock_agent_cls.call_args[1]
        from config.settings import settings
        assert orch_kwargs["max_steps"] == settings.orchestrator_max_steps

    @patch("src.agent.factory.ToolCallingAgent")
    @patch("src.agent.factory.get_model")
    @patch("src.agent.factory.skill_manager")
    @patch("src.agent.specialists.ToolCallingAgent")
    @patch("src.agent.specialists.LiteLLMModel")
    @patch("src.agent.specialists.mcp_manager")
    @patch("src.agent.specialists.discover_tools")
    def test_orchestrator_delegation_preamble(
        self, mock_discover, mock_mcp, mock_spec_model, mock_spec_agent,
        mock_skill_mgr, mock_get_model, mock_agent_cls,
    ):
        mock_spec_model.return_value = MagicMock()
        mock_mcp.get_config.return_value = []
        mock_skill_mgr.get_active_skills.return_value = []

        from src.agent.specialists import TOOL_DOMAINS
        tools = []
        for name in TOOL_DOMAINS:
            tool = MagicMock()
            tool.name = name
            tools.append(tool)
        mock_discover.return_value = tools

        def _make_specialist(**kwargs):
            agent = MagicMock()
            agent.name = kwargs.get("name", "unknown")
            agent.tools = kwargs.get("tools", [])
            return agent

        mock_spec_agent.side_effect = _make_specialist
        mock_get_model.return_value = MagicMock()
        mock_agent_cls.return_value = MagicMock()

        create_orchestrator()

        orch_kwargs = mock_agent_cls.call_args[1]
        instructions = orch_kwargs["instructions"]
        assert "You do NOT have any tools yourself" in instructions
        assert "team of specialists" in instructions


class TestStepFormatting:
    def test_specialist_delegation_format(self):
        names = {"web_researcher", "memory_keeper"}
        result = _format_tool_step("web_researcher", {"task": "search for AI news"}, names)
        assert result == "Delegating to web_researcher: search for AI news"

    def test_regular_tool_format(self):
        names = {"web_researcher", "memory_keeper"}
        result = _format_tool_step("web_search", {"query": "AI news"}, names)
        assert "Calling tool: web_search" in result
        assert '"query": "AI news"' in result

    def test_empty_specialist_set(self):
        result = _format_tool_step("web_researcher", {"task": "search"}, set())
        assert result.startswith("Calling tool:")
