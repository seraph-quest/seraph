from fastapi import APIRouter

from config.settings import settings
from src.agent.factory import get_tools
from src.plugins.registry import get_tool_metadata

router = APIRouter()


@router.get("/tools")
async def list_tools():
    """List all available tools with their metadata (including dynamic MCP tools)."""
    tools = get_tools()

    result = []
    for tool in tools:
        meta = get_tool_metadata(tool.name)
        result.append({
            "name": tool.name,
            "description": meta.get("description") if meta else getattr(tool, "description", ""),
        })

    # When delegation is active, also include specialist names so the frontend
    # toolRegistry recognizes them for animation triggers.
    if settings.use_delegation:
        from src.agent.specialists import SPECIALIST_CONFIGS, _sanitize_agent_name
        from src.tools.mcp_manager import mcp_manager

        for name, cfg in SPECIALIST_CONFIGS.items():
            result.append({"name": name, "description": cfg["description"]})

        for server_info in mcp_manager.get_config():
            if mcp_manager.is_connected(server_info["name"]):
                result.append({
                    "name": _sanitize_agent_name(f"mcp_{server_info['name']}"),
                    "description": server_info.get("description", ""),
                })

    return result
