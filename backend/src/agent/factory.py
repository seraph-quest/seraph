from smolagents import LiteLLMModel, ToolCallingAgent

from config.settings import settings
from src.plugins.loader import discover_tools
from src.skills.manager import skill_manager
from src.tools.mcp_manager import mcp_manager


def get_model() -> LiteLLMModel:
    """Create a LiteLLMModel configured for OpenRouter."""
    return LiteLLMModel(
        model_id=settings.default_model,
        api_key=settings.openrouter_api_key,
        api_base="https://openrouter.ai/api/v1",
        temperature=settings.model_temperature,
        max_tokens=settings.model_max_tokens,
    )


def get_tools() -> list:
    """Return all auto-discovered tools + MCP tools."""
    return discover_tools() + mcp_manager.get_tools()


def create_agent(
    additional_context: str = "",
    soul_context: str = "",
    memory_context: str = "",
    observer_context: str = "",
) -> ToolCallingAgent:
    """Create a ToolCallingAgent with LiteLLM model and tools.

    Args:
        additional_context: Conversation history to include in the system prompt.
        soul_context: Soul file content (user identity, values, goals).
        memory_context: Relevant long-term memories for this conversation.
        observer_context: Current observer context (time, window, screen, etc.).
    """
    model = get_model()
    tools = get_tools()
    tool_names = [t.name for t in tools]

    instructions = (
        "You are Seraph, a proactive guardian intelligence dedicated to elevating "
        "your human counterpart. You observe, think, and act to help them achieve "
        "their highest potential across productivity, performance, health, influence, "
        "and growth. Be concise, strategic, and helpful."
    )
    if soul_context:
        instructions += f"\n\n--- USER IDENTITY ---\n{soul_context}"
    if memory_context:
        instructions += f"\n\n--- RELEVANT MEMORIES ---\n{memory_context}"
    if observer_context:
        instructions += f"\n\n--- CURRENT CONTEXT ---\n{observer_context}"

    # Inject active skills into instructions
    active_skills = skill_manager.get_active_skills(tool_names)
    if active_skills:
        skill_lines = []
        for s in active_skills:
            invocable = " [user-invocable]" if s.user_invocable else ""
            skill_lines.append(f"### Skill: {s.name}{invocable}\n{s.instructions}")
        instructions += "\n\n## Available Skills\n\n" + "\n\n".join(skill_lines)

    if additional_context:
        instructions += f"\n\n--- CONVERSATION HISTORY ---\n{additional_context}"

    agent = ToolCallingAgent(
        tools=tools,
        model=model,
        max_steps=settings.agent_max_steps,
        instructions=instructions,
    )
    return agent


def create_orchestrator(
    additional_context: str = "",
    soul_context: str = "",
    memory_context: str = "",
    observer_context: str = "",
) -> ToolCallingAgent:
    """Create an orchestrator agent that delegates to specialist sub-agents.

    The orchestrator has NO tools itself — it delegates all execution to
    specialist managed_agents (memory_keeper, goal_planner, web_researcher,
    file_worker, and one per MCP server).
    """
    from src.agent.specialists import build_all_specialists

    model = get_model()
    specialists = build_all_specialists()

    # Collect all tool names across specialists for skill gating
    all_tool_names = []
    for specialist in specialists:
        all_tool_names.extend(t.name for t in specialist.tools)

    instructions = (
        "You are Seraph, a proactive guardian intelligence dedicated to elevating "
        "your human counterpart. You observe, think, and act to help them achieve "
        "their highest potential across productivity, performance, health, influence, "
        "and growth. Be concise, strategic, and helpful.\n\n"
        "You do NOT have any tools yourself. Instead, you have a team of specialists.\n"
        "Analyze the user's request, decide which specialist(s) to delegate to, and\n"
        "synthesize their results into a coherent, helpful response.\n"
        "Guidelines:\n"
        "- For simple questions that need no tools, answer directly.\n"
        "- Delegate to ONE specialist when possible.\n"
        "- Give clear, specific task descriptions when delegating.\n"
        "- Synthesize specialist results into a natural response."
    )
    if soul_context:
        instructions += f"\n\n--- USER IDENTITY ---\n{soul_context}"
    if memory_context:
        instructions += f"\n\n--- RELEVANT MEMORIES ---\n{memory_context}"
    if observer_context:
        instructions += f"\n\n--- CURRENT CONTEXT ---\n{observer_context}"

    # Inject active skills into instructions
    active_skills = skill_manager.get_active_skills(all_tool_names)
    if active_skills:
        skill_lines = []
        for s in active_skills:
            invocable = " [user-invocable]" if s.user_invocable else ""
            skill_lines.append(f"### Skill: {s.name}{invocable}\n{s.instructions}")
        instructions += "\n\n## Available Skills\n\n" + "\n\n".join(skill_lines)

    if additional_context:
        instructions += f"\n\n--- CONVERSATION HISTORY ---\n{additional_context}"

    agent = ToolCallingAgent(
        tools=[],
        managed_agents=specialists,
        model=model,
        max_steps=settings.orchestrator_max_steps,
        instructions=instructions,
    )
    return agent


def build_agent(
    additional_context: str = "",
    soul_context: str = "",
    memory_context: str = "",
    observer_context: str = "",
) -> ToolCallingAgent:
    """Build the appropriate agent based on delegation feature flag.

    Drop-in replacement for create_agent() at call sites.
    """
    if settings.use_delegation:
        return create_orchestrator(additional_context, soul_context, memory_context, observer_context)
    return create_agent(additional_context, soul_context, memory_context, observer_context)
