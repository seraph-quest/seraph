from fastapi import APIRouter

from config.settings import settings
from src.agent.factory import get_tools
from src.plugins.registry import get_tool_metadata
from src.tools.policy import get_current_mcp_policy_mode, get_current_tool_policy_mode, get_tool_risk_level
from src.workflows.manager import workflow_manager

router = APIRouter()


@router.get("/tools")
async def list_tools():
    """List all available tools with their metadata (including dynamic MCP tools)."""
    tools = get_tools()
    mode = get_current_tool_policy_mode()
    mcp_mode = get_current_mcp_policy_mode()

    result = []
    for tool in tools:
        meta = get_tool_metadata(tool.name)
        if meta is None:
            meta = workflow_manager.get_tool_metadata(tool.name)
        is_mcp = tool.name.startswith("mcp_")
        policy_modes = meta.get("policy_modes") if meta else ([mcp_mode] if is_mcp else [mode])
        if meta:
            requires_approval = (
                (is_mcp and mcp_mode == "approval")
                or policy_modes == ["full"]
            )
        else:
            requires_approval = (
                is_mcp and mcp_mode == "approval"
            ) or get_tool_risk_level(tool.name, is_mcp=is_mcp) == "high"
        result.append({
            "name": tool.name,
            "description": meta.get("description") if meta else getattr(tool, "description", ""),
            "policy_modes": policy_modes,
            "requires_approval": requires_approval,
        })

    # When delegation is active, also include specialist names so the frontend
    # toolRegistry recognizes them for animation triggers.
    if settings.use_delegation:
        from src.agent.specialists import build_all_specialists

        for specialist in build_all_specialists():
            result.append({
                "name": specialist.name,
                "description": specialist.description,
                "policy_modes": [mode],
                "requires_approval": False,
            })

    return result
