"""Specialist agent factories for recursive delegation architecture.

When delegation mode is enabled, the root orchestrator delegates to domain-specific
specialist agents rather than calling tools directly. Each specialist has a focused
tool set and tuned temperature for its domain.

Tier 1 (built-in): memory_keeper, goal_planner, web_researcher, file_worker
Tier 2 (dynamic): one specialist per connected MCP server
"""

import re

from smolagents import LiteLLMModel, ToolCallingAgent

from config.settings import settings
from src.plugins.loader import discover_tools
from src.tools.mcp_manager import mcp_manager

# --- Tool → domain mapping ---

TOOL_DOMAINS: dict[str, str] = {
    "view_soul": "memory",
    "update_soul": "memory",
    "create_goal": "goals",
    "update_goal": "goals",
    "get_goals": "goals",
    "get_goal_progress": "goals",
    "web_search": "research",
    "browse_webpage": "research",
    "read_file": "files",
    "write_file": "files",
    "fill_template": "files",
    "shell_execute": "files",
}

# Reverse index: domain → list of tool names
DOMAIN_TOOLS: dict[str, list[str]] = {}
for _tool, _domain in TOOL_DOMAINS.items():
    DOMAIN_TOOLS.setdefault(_domain, []).append(_tool)

# --- Specialist configurations ---

SPECIALIST_CONFIGS: dict[str, dict] = {
    "memory_keeper": {
        "domain": "memory",
        "description": (
            "Manages the user's identity file (soul). Can view and update "
            "the soul file sections (identity, values, goals, preferences)."
        ),
        "temperature": 0.5,
        "max_steps": 3,
    },
    "goal_planner": {
        "domain": "goals",
        "description": (
            "Manages the user's goal/quest system. Can create, update, list goals "
            "and check goal progress across all domains."
        ),
        "temperature": 0.4,
        "max_steps": 5,
    },
    "web_researcher": {
        "domain": "research",
        "description": (
            "Searches the web and browses webpages for information. "
            "Use for any research, fact-finding, or web content retrieval."
        ),
        "temperature": 0.3,
        "max_steps": 8,
    },
    "file_worker": {
        "domain": "files",
        "description": (
            "Reads, writes, and manages files in the workspace. "
            "Can also execute code in a sandboxed environment and fill templates."
        ),
        "temperature": 0.3,
        "max_steps": 6,
    },
}


def _sanitize_agent_name(name: str) -> str:
    """Convert a string to a valid Python identifier for use as agent name."""
    # Replace hyphens and other non-alphanumeric chars with underscores
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    # Collapse multiple underscores
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    # Prefix with mcp_ if starts with a digit
    if sanitized and sanitized[0].isdigit():
        sanitized = f"mcp_{sanitized}"
    return sanitized or "unnamed_agent"


def _create_model(temperature: float) -> LiteLLMModel:
    """Create a LiteLLMModel with specialist-specific temperature."""
    return LiteLLMModel(
        model_id=settings.default_model,
        api_key=settings.openrouter_api_key,
        api_base="https://openrouter.ai/api/v1",
        temperature=temperature,
        max_tokens=settings.model_max_tokens,
    )


def create_specialist(
    name: str,
    description: str,
    tools: list,
    temperature: float,
    max_steps: int,
) -> ToolCallingAgent:
    """Create a specialist agent with the given tools and settings."""
    model = _create_model(temperature)
    return ToolCallingAgent(
        tools=tools,
        model=model,
        name=name,
        description=description,
        max_steps=max_steps,
    )


# --- Named factories for built-in specialists ---

def create_memory_keeper(tools_by_name: dict) -> ToolCallingAgent | None:
    cfg = SPECIALIST_CONFIGS["memory_keeper"]
    tools = [tools_by_name[n] for n in DOMAIN_TOOLS["memory"] if n in tools_by_name]
    if not tools:
        return None
    return create_specialist("memory_keeper", cfg["description"], tools, cfg["temperature"], cfg["max_steps"])


def create_goal_planner(tools_by_name: dict) -> ToolCallingAgent | None:
    cfg = SPECIALIST_CONFIGS["goal_planner"]
    tools = [tools_by_name[n] for n in DOMAIN_TOOLS["goals"] if n in tools_by_name]
    if not tools:
        return None
    return create_specialist("goal_planner", cfg["description"], tools, cfg["temperature"], cfg["max_steps"])


def create_web_researcher(tools_by_name: dict) -> ToolCallingAgent | None:
    cfg = SPECIALIST_CONFIGS["web_researcher"]
    tools = [tools_by_name[n] for n in DOMAIN_TOOLS["research"] if n in tools_by_name]
    if not tools:
        return None
    return create_specialist("web_researcher", cfg["description"], tools, cfg["temperature"], cfg["max_steps"])


def create_file_worker(tools_by_name: dict) -> ToolCallingAgent | None:
    cfg = SPECIALIST_CONFIGS["file_worker"]
    tools = [tools_by_name[n] for n in DOMAIN_TOOLS["files"] if n in tools_by_name]
    if not tools:
        return None
    return create_specialist("file_worker", cfg["description"], tools, cfg["temperature"], cfg["max_steps"])


# --- MCP specialist factory ---

def create_mcp_specialist(
    server_name: str,
    tools: list,
    description: str = "",
) -> ToolCallingAgent:
    """Create a specialist agent for a single MCP server's tools."""
    name = _sanitize_agent_name(f"mcp_{server_name}")
    if not description:
        tool_names = [getattr(t, "name", str(t)) for t in tools]
        description = f"MCP server '{server_name}' with tools: {', '.join(tool_names)}"
    return create_specialist(name, description, tools, temperature=0.3, max_steps=6)


# --- Build all specialists ---

def build_all_specialists() -> list[ToolCallingAgent]:
    """Assemble the full list of specialist agents (built-in + MCP)."""
    # Build tools_by_name from discovered tools
    all_tools = discover_tools()
    tools_by_name = {t.name: t for t in all_tools}

    specialists: list[ToolCallingAgent] = []

    # Tier 1: built-in specialists
    for factory in (create_memory_keeper, create_goal_planner, create_web_researcher, create_file_worker):
        agent = factory(tools_by_name)
        if agent is not None:
            specialists.append(agent)

    # Tier 2: one specialist per connected MCP server
    server_configs = mcp_manager.get_config()
    for server_info in server_configs:
        name = server_info["name"]
        if not mcp_manager.is_connected(name):
            continue
        server_tools = mcp_manager.get_server_tools(name)
        if not server_tools:
            continue
        desc = server_info.get("description", "")
        specialists.append(create_mcp_specialist(name, server_tools, desc))

    return specialists
