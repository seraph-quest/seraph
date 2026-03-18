"""Workflows API — list, toggle, reload reusable multi-step workflows."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.audit.runtime import log_integration_event
from src.tools.policy import get_current_mcp_policy_mode
from src.workflows.manager import workflow_manager

router = APIRouter()


class UpdateWorkflowRequest(BaseModel):
    enabled: bool


@router.get("/workflows")
async def list_workflows():
    mcp_mode = get_current_mcp_policy_mode()
    workflows = []
    for workflow in workflow_manager.list_workflows():
        boundaries = workflow.get("execution_boundaries", [])
        risk_level = workflow.get("risk_level", "low")
        requires_approval = (
            risk_level == "high"
            or ("external_mcp" in boundaries and mcp_mode == "approval")
        )
        if "external_mcp" in boundaries and mcp_mode == "approval":
            approval_behavior = "always"
        elif risk_level == "high":
            approval_behavior = "high_risk_mode"
        else:
            approval_behavior = "never"
        workflows.append({
            **workflow,
            "requires_approval": requires_approval,
            "approval_behavior": approval_behavior,
        })
    return {"workflows": workflows}


@router.put("/workflows/{name}")
async def update_workflow(name: str, req: UpdateWorkflowRequest):
    ok = workflow_manager.enable(name) if req.enabled else workflow_manager.disable(name)
    if not ok:
        await log_integration_event(
            integration_type="workflow",
            name=name,
            outcome="failed",
            details={
                "status": "not_found",
                "enabled": req.enabled,
            },
        )
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")
    await log_integration_event(
        integration_type="workflow",
        name=name,
        outcome="succeeded",
        details={"enabled": req.enabled},
    )
    return {"status": "updated", "name": name, "enabled": req.enabled}


@router.post("/workflows/reload")
async def reload_workflows():
    workflows = workflow_manager.reload()
    await log_integration_event(
        integration_type="workflows",
        name="reload",
        outcome="succeeded",
        details={
            "count": len(workflows),
            "enabled_count": sum(1 for workflow in workflows if workflow.get("enabled", False)),
            "workflow_names": [workflow["name"] for workflow in workflows],
        },
    )
    return {"status": "reloaded", "count": len(workflows), "workflows": workflows}
