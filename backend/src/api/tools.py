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
            "description": getattr(tool, "description", ""),
            "building": meta.get("building") if meta else None,
            "pixel_x": meta.get("pixel_x") if meta else None,
            "pixel_y": meta.get("pixel_y") if meta else None,
            "animation": meta.get("animation") if meta else None,
        })
    return result
