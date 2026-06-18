from fastapi import APIRouter

from config.settings import settings
from src.agent.factory import get_tools
from src.agent.specialists import list_specialist_descriptors
from src.native_tools.registry import get_tool_metadata
from src.tools.policy import (
    get_tool_approval_behavior,
    get_current_mcp_policy_mode,
    get_current_tool_policy_mode,
    get_tool_credential_egress_policy,
    get_tool_execution_boundaries,
    get_tool_risk_level,
    get_tool_secret_ref_fields,
    get_tool_source_context,
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
        source_context = get_tool_source_context(tool)
        policy_modes = meta.get("policy_modes") if meta else ([mcp_mode] if is_mcp else [mode])
        execution_boundaries = meta.get("execution_boundaries") if meta else None
        if not execution_boundaries:
            execution_boundaries = get_tool_execution_boundaries(tool.name, is_mcp=is_mcp, tool=tool)
        risk_level = meta.get("risk_level") if meta else None
        if not isinstance(risk_level, str):
            risk_level = get_tool_risk_level(tool.name, is_mcp=is_mcp)
        if (is_mcp or "external_mcp" in execution_boundaries) and mcp_mode == "approval":
            approval_behavior = "always"
        else:
            approval_behavior = get_tool_approval_behavior(tool.name, is_mcp=is_mcp)
        secret_ref_fields = get_tool_secret_ref_fields(tool.name, is_mcp=is_mcp, tool=tool)
        requires_approval = approval_behavior != "never"
        result.append({
            "name": tool.name,
            "description": meta.get("description") if meta else getattr(tool, "description", ""),
            "policy_modes": policy_modes,
            "requires_approval": requires_approval,
            "approval_behavior": approval_behavior,
            "risk_level": risk_level,
            "execution_boundaries": execution_boundaries,
            "accepts_secret_refs": bool(secret_ref_fields) if is_mcp else bool(meta.get("accepts_secret_refs", False)) if meta else False,
            "secret_ref_fields": secret_ref_fields,
            **({
                "authenticated_source": bool(
                    isinstance(source_context, dict)
                    and source_context.get("authenticated_source")
                ),
                "credential_egress_policy": get_tool_credential_egress_policy(
                    tool.name,
                    is_mcp=True,
                    tool=tool,
                ),
            } if is_mcp else {}),
        })

    # When delegation is active, also include specialist names so the frontend
    # toolRegistry recognizes them for animation triggers.
    if settings.use_delegation:
        for specialist in list_specialist_descriptors(
            tools,
            tool_mode=mode,
            mcp_mode=mcp_mode,
        ):
            result.append({
                "name": specialist["name"],
                "description": specialist["description"],
                "policy_modes": [mode],
                "requires_approval": False,
                "approval_behavior": "never",
                "risk_level": "low",
                "execution_boundaries": ["delegation"],
                "accepts_secret_refs": False,
                "secret_ref_fields": [],
            })

    return result
