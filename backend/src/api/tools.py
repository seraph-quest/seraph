from fastapi import APIRouter

from config.settings import settings
from src.agent.factory import get_tools
from src.plugins.registry import get_tool_metadata
from src.tools.policy import (
    get_current_mcp_policy_mode,
    get_current_tool_policy_mode,
    get_tool_execution_boundaries,
    get_tool_risk_level,
)
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
        execution_boundaries = meta.get("execution_boundaries") if meta else None
        if not execution_boundaries:
            execution_boundaries = get_tool_execution_boundaries(tool.name, is_mcp=is_mcp)
        risk_level = meta.get("risk_level") if meta else None
        if not isinstance(risk_level, str):
            risk_level = get_tool_risk_level(tool.name, is_mcp=is_mcp)
        requires_approval = (
            (is_mcp and mcp_mode == "approval")
            or ("external_mcp" in execution_boundaries and mcp_mode == "approval")
            or risk_level == "high"
        )
        if (is_mcp or "external_mcp" in execution_boundaries) and mcp_mode == "approval":
            approval_behavior = "always"
        elif risk_level == "high":
            approval_behavior = "high_risk_mode"
        else:
            approval_behavior = "never"
        result.append({
            "name": tool.name,
            "description": meta.get("description") if meta else getattr(tool, "description", ""),
            "policy_modes": policy_modes,
            "requires_approval": requires_approval,
            "approval_behavior": approval_behavior,
            "risk_level": risk_level,
            "execution_boundaries": execution_boundaries,
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
                "approval_behavior": "never",
                "risk_level": "low",
                "execution_boundaries": ["delegation"],
            })

    return result
