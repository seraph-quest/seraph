from fastapi import APIRouter

from src.agent.factory import get_tools
from src.plugins.registry import get_all_metadata

router = APIRouter()


@router.get("/tools")
async def list_tools():
    """List all available tools with their metadata."""
    tools = get_tools()
    metadata = get_all_metadata()

    result = []
    for tool in tools:
        meta = metadata.get(tool.name, {})
        result.append({
            "name": tool.name,
            "description": getattr(tool, "description", ""),
            "building": meta.get("building"),
            "pixel_x": meta.get("pixel_x"),
            "pixel_y": meta.get("pixel_y"),
            "animation": meta.get("animation"),
        })
    return result
