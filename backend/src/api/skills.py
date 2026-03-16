"""Skills API — list, toggle, reload SKILL.md plugins."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.audit.runtime import log_integration_event
from src.skills.manager import skill_manager

router = APIRouter()


class UpdateSkillRequest(BaseModel):
    enabled: bool


@router.get("/skills")
async def list_skills():
    """List all loaded skills with status."""
    return {"skills": skill_manager.list_skills()}


@router.put("/skills/{name}")
async def update_skill(name: str, req: UpdateSkillRequest):
    """Enable or disable a skill."""
    if req.enabled:
        ok = skill_manager.enable(name)
    else:
        ok = skill_manager.disable(name)
    if not ok:
        await log_integration_event(
            integration_type="skill",
            name=name,
            outcome="failed",
            details={
                "status": "not_found",
                "enabled": req.enabled,
            },
        )
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    await log_integration_event(
        integration_type="skill",
        name=name,
        outcome="succeeded",
        details={
            "enabled": req.enabled,
        },
    )
    return {"status": "updated", "name": name, "enabled": req.enabled}


@router.post("/skills/reload")
async def reload_skills():
    """Re-scan the skills directory."""
    skills = skill_manager.reload()
    await log_integration_event(
        integration_type="skills",
        name="reload",
        outcome="succeeded",
        details={
            "count": len(skills),
            "enabled_count": sum(1 for skill in skills if skill.get("enabled", False)),
            "skill_names": [skill["name"] for skill in skills],
        },
    )
    return {"status": "reloaded", "count": len(skills), "skills": skills}
