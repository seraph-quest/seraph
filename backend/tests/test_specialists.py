"""Tests for specialist agent factories and tool domain mapping."""

from unittest.mock import MagicMock, patch

from src.agent.specialists import (
    DOMAIN_TOOLS,
    SPECIALIST_CONFIGS,
    TOOL_DOMAINS,
    _sanitize_agent_name,
    build_all_specialists,
    create_file_worker,
    create_goal_planner,
    create_mcp_specialist,
    create_memory_keeper,
    create_specialist,
    create_web_researcher,
)


class TestToolDomainMapping:
    def test_all_builtin_tools_have_domains(self):
        expected_tools = {
            "view_soul", "update_soul",
            "store_secret", "get_secret", "list_secrets", "delete_secret",
            "create_goal", "update_goal", "get_goals", "get_goal_progress",
            "web_search", "browse_webpage",
            "read_file", "write_file", "fill_template", "shell_execute",
        }
        assert set(TOOL_DOMAINS.keys()) == expected_tools

    def test_reverse_index_covers_all_domains(self):
        assert set(DOMAIN_TOOLS.keys()) == {"memory", "goals", "research", "files"}

    def test_reverse_index_matches_forward(self):
        for tool, domain in TOOL_DOMAINS.items():
            assert tool in DOMAIN_TOOLS[domain]

    def test_configs_cover_all_domains(self):
        config_domains = {cfg["domain"] for cfg in SPECIALIST_CONFIGS.values()}
        assert config_domains == set(DOMAIN_TOOLS.keys())


class TestSanitizeAgentName:
    def test_simple_name(self):
        assert _sanitize_agent_name("github") == "github"

    def test_hyphens_to_underscores(self):
        assert _sanitize_agent_name("my-server") == "my_server"

    def test_numeric_prefix(self):
        result = _sanitize_agent_name("123abc")
        assert result == "mcp_123abc"
        assert result.isidentifier()

    def test_special_chars(self):
        result = _sanitize_agent_name("my@server!v2")
        assert result == "my_server_v2"
        assert result.isidentifier()

    def test_empty_string(self):
        assert _sanitize_agent_name("") == "unnamed_agent"

    def test_collapses_multiple_underscores(self):
        assert _sanitize_agent_name("a---b") == "a_b"


class TestCreateSpecialist:
    @patch("src.agent.specialists.ToolCallingAgent")
    @patch("src.agent.specialists.LiteLLMModel")
    def test_creates_agent_with_correct_params(self, mock_model_cls, mock_agent_cls):
        mock_model_cls.return_value = MagicMock()
        mock_agent = MagicMock()
        mock_agent.name = "test_agent"
        mock_agent.max_steps = 3
        mock_agent_cls.return_value = mock_agent
        tool = MagicMock()
        tool.name = "test_tool"

        agent = create_specialist(
            name="test_agent",
            description="A test agent",
            tools=[tool],
            temperature=0.5,
            max_steps=3,
        )

        assert agent.name == "test_agent"
        assert agent.max_steps == 3
        mock_model_cls.assert_called_once()
        call_kwargs = mock_model_cls.call_args[1]
        assert call_kwargs["temperature"] == 0.5
        mock_agent_cls.assert_called_once()
        agent_kwargs = mock_agent_cls.call_args[1]
        assert agent_kwargs["name"] == "test_agent"
        assert agent_kwargs["max_steps"] == 3


class TestNamedFactories:
    def _make_tools_by_name(self):
        tools = {}
        for name in TOOL_DOMAINS:
            tool = MagicMock()
            tool.name = name
            tools[name] = tool
        return tools

    @patch("src.agent.specialists.ToolCallingAgent")
    @patch("src.agent.specialists.LiteLLMModel")
    def test_memory_keeper_filters_tools(self, mock_model_cls, mock_agent_cls):
        mock_model_cls.return_value = MagicMock()
        mock_agent_cls.return_value = MagicMock()
        tools_by_name = self._make_tools_by_name()
        create_memory_keeper(tools_by_name)
        agent_kwargs = mock_agent_cls.call_args[1]
        tool_names = {t.name for t in agent_kwargs["tools"]}
        assert tool_names == {"view_soul", "update_soul", "store_secret", "get_secret", "list_secrets", "delete_secret"}

    @patch("src.agent.specialists.ToolCallingAgent")
    @patch("src.agent.specialists.LiteLLMModel")
    def test_goal_planner_filters_tools(self, mock_model_cls, mock_agent_cls):
        mock_model_cls.return_value = MagicMock()
        mock_agent_cls.return_value = MagicMock()
        tools_by_name = self._make_tools_by_name()
        create_goal_planner(tools_by_name)
        agent_kwargs = mock_agent_cls.call_args[1]
        tool_names = {t.name for t in agent_kwargs["tools"]}
        assert tool_names == {"create_goal", "update_goal", "get_goals", "get_goal_progress"}

    @patch("src.agent.specialists.ToolCallingAgent")
    @patch("src.agent.specialists.LiteLLMModel")
    def test_web_researcher_filters_tools(self, mock_model_cls, mock_agent_cls):
        mock_model_cls.return_value = MagicMock()
        mock_agent_cls.return_value = MagicMock()
        tools_by_name = self._make_tools_by_name()
        create_web_researcher(tools_by_name)
        agent_kwargs = mock_agent_cls.call_args[1]
        tool_names = {t.name for t in agent_kwargs["tools"]}
        assert tool_names == {"web_search", "browse_webpage"}

    @patch("src.agent.specialists.ToolCallingAgent")
    @patch("src.agent.specialists.LiteLLMModel")
    def test_file_worker_filters_tools(self, mock_model_cls, mock_agent_cls):
        mock_model_cls.return_value = MagicMock()
        mock_agent_cls.return_value = MagicMock()
        tools_by_name = self._make_tools_by_name()
        create_file_worker(tools_by_name)
        agent_kwargs = mock_agent_cls.call_args[1]
        tool_names = {t.name for t in agent_kwargs["tools"]}
        assert tool_names == {"read_file", "write_file", "fill_template", "shell_execute"}

    def test_factory_returns_none_with_no_tools(self):
        agent = create_memory_keeper({})
        assert agent is None

    @patch("src.agent.specialists.ToolCallingAgent")
    @patch("src.agent.specialists.LiteLLMModel")
    def test_memory_keeper_temperature(self, mock_model_cls, mock_agent_cls):
        mock_model_cls.return_value = MagicMock()
        mock_agent_cls.return_value = MagicMock()
        tools_by_name = self._make_tools_by_name()
        create_memory_keeper(tools_by_name)
        call_kwargs = mock_model_cls.call_args[1]
        assert call_kwargs["temperature"] == 0.5

    @patch("src.agent.specialists.ToolCallingAgent")
    @patch("src.agent.specialists.LiteLLMModel")
    def test_web_researcher_max_steps(self, mock_model_cls, mock_agent_cls):
        mock_model_cls.return_value = MagicMock()
        mock_agent_cls.return_value = MagicMock()
        tools_by_name = self._make_tools_by_name()
        create_web_researcher(tools_by_name)
        agent_kwargs = mock_agent_cls.call_args[1]
        assert agent_kwargs["max_steps"] == 8


class TestMcpSpecialist:
    @patch("src.agent.specialists.ToolCallingAgent")
    @patch("src.agent.specialists.LiteLLMModel")
    def test_name_sanitization(self, mock_model_cls, mock_agent_cls):
        mock_model_cls.return_value = MagicMock()
        mock_agent_cls.return_value = MagicMock()
        tool = MagicMock()
        tool.name = "do_thing"
        create_mcp_specialist("my-server", [tool])
        agent_kwargs = mock_agent_cls.call_args[1]
        assert agent_kwargs["name"] == "mcp_my_server"

    @patch("src.agent.specialists.ToolCallingAgent")
    @patch("src.agent.specialists.LiteLLMModel")
    def test_auto_generated_description(self, mock_model_cls, mock_agent_cls):
        mock_model_cls.return_value = MagicMock()
        mock_agent_cls.return_value = MagicMock()
        tool = MagicMock()
        tool.name = "list_tasks"
        create_mcp_specialist("things3", [tool])
        agent_kwargs = mock_agent_cls.call_args[1]
        assert "list_tasks" in agent_kwargs["description"]
        assert "things3" in agent_kwargs["description"]

    @patch("src.agent.specialists.ToolCallingAgent")
    @patch("src.agent.specialists.LiteLLMModel")
    def test_custom_description(self, mock_model_cls, mock_agent_cls):
        mock_model_cls.return_value = MagicMock()
        mock_agent_cls.return_value = MagicMock()
        tool = MagicMock()
        tool.name = "list_tasks"
        create_mcp_specialist("things3", [tool], description="Task manager")
        agent_kwargs = mock_agent_cls.call_args[1]
        assert agent_kwargs["description"] == "Task manager"


class TestBuildAllSpecialists:
    @patch("src.agent.specialists.ToolCallingAgent")
    @patch("src.agent.specialists.LiteLLMModel")
    @patch("src.agent.specialists.mcp_manager")
    @patch("src.agent.specialists.discover_tools")
    def test_without_mcp_returns_builtin(self, mock_discover, mock_mcp, mock_model_cls, mock_agent_cls):
        mock_model_cls.return_value = MagicMock()
        mock_mcp.get_config.return_value = []

        # Track created agents
        created = []

        def _make_agent(**kwargs):
            agent = MagicMock()
            agent.name = kwargs.get("name", "unknown")
            created.append(agent)
            return agent

        mock_agent_cls.side_effect = _make_agent

        # Create mock tools for all built-in domains
        tools = []
        for name in TOOL_DOMAINS:
            tool = MagicMock()
            tool.name = name
            tools.append(tool)
        mock_discover.return_value = tools

        specialists = build_all_specialists()
        assert len(specialists) == 4
        names = {s.name for s in specialists}
        assert names == {"memory_keeper", "goal_planner", "web_researcher", "file_worker"}

    @patch("src.agent.specialists.ToolCallingAgent")
    @patch("src.agent.specialists.LiteLLMModel")
    @patch("src.agent.specialists.mcp_manager")
    @patch("src.agent.specialists.discover_tools")
    def test_with_mcp_returns_builtin_plus_mcp(self, mock_discover, mock_mcp, mock_model_cls, mock_agent_cls):
        mock_model_cls.return_value = MagicMock()

        created = []

        def _make_agent(**kwargs):
            agent = MagicMock()
            agent.name = kwargs.get("name", "unknown")
            created.append(agent)
            return agent

        mock_agent_cls.side_effect = _make_agent

        # Built-in tools
        tools = []
        for name in TOOL_DOMAINS:
            tool = MagicMock()
            tool.name = name
            tools.append(tool)
        mock_discover.return_value = tools

        # MCP server
        mcp_tool = MagicMock()
        mcp_tool.name = "list_tasks"
        mock_mcp.get_config.return_value = [
            {"name": "things3", "url": "http://...", "enabled": True, "connected": True, "tool_count": 1, "description": "Task manager"},
        ]
        mock_mcp.is_connected.return_value = True
        mock_mcp.get_server_tools.return_value = [mcp_tool]

        specialists = build_all_specialists()
        assert len(specialists) == 5
        names = {s.name for s in specialists}
        assert "mcp_things3" in names

    @patch("src.agent.specialists.ToolCallingAgent")
    @patch("src.agent.specialists.LiteLLMModel")
    @patch("src.agent.specialists.mcp_manager")
    @patch("src.agent.specialists.discover_tools")
    def test_skips_disconnected_mcp(self, mock_discover, mock_mcp, mock_model_cls, mock_agent_cls):
        mock_model_cls.return_value = MagicMock()

        created = []

        def _make_agent(**kwargs):
            agent = MagicMock()
            agent.name = kwargs.get("name", "unknown")
            created.append(agent)
            return agent

        mock_agent_cls.side_effect = _make_agent

        tools = []
        for name in TOOL_DOMAINS:
            tool = MagicMock()
            tool.name = name
            tools.append(tool)
        mock_discover.return_value = tools

        mock_mcp.get_config.return_value = [
            {"name": "offline", "url": "http://...", "enabled": True, "connected": False, "tool_count": 0, "description": ""},
        ]
        mock_mcp.is_connected.return_value = False

        specialists = build_all_specialists()
        assert len(specialists) == 4  # only built-in
