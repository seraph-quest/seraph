"""Skills API — list, toggle, reload SKILL.md plugins."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

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
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    return {"status": "updated", "name": name, "enabled": req.enabled}


@router.post("/skills/reload")
async def reload_skills():
    """Re-scan the skills directory."""
    skills = skill_manager.reload()
    return {"status": "reloaded", "count": len(skills), "skills": skills}
