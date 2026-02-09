from smolagents import LiteLLMModel, ToolCallingAgent

from config.settings import settings
from src.plugins.loader import discover_tools
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
) -> ToolCallingAgent:
    """Create a ToolCallingAgent with LiteLLM model and tools.

    Args:
        additional_context: Conversation history to include in the system prompt.
        soul_context: Soul file content (user identity, values, goals).
        memory_context: Relevant long-term memories for this conversation.
    """
    model = get_model()
    tools = get_tools()

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
    if additional_context:
        instructions += f"\n\n--- CONVERSATION HISTORY ---\n{additional_context}"

    agent = ToolCallingAgent(
        tools=tools,
        model=model,
        max_steps=settings.agent_max_steps,
        instructions=instructions,
    )
    return agent
