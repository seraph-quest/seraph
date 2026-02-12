from fastapi import APIRouter

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
    return result
