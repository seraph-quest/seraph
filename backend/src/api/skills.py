"""Skills API — list, toggle, reload, validate, and save SKILL.md skills."""

import os
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config.settings import settings
from src.audit.runtime import log_integration_event
from src.agent.factory import get_base_tools_and_active_skills
from src.extensions.registry import default_manifest_roots_for_workspace
from src.extensions.workspace_package import save_workspace_contribution
from src.skills.loader import parse_skill_content
from src.skills.manager import skill_manager

router = APIRouter()

_SKILL_FILENAME_RE = re.compile(r"[^a-zA-Z0-9_-]+")


class UpdateSkillRequest(BaseModel):
    enabled: bool


class SkillDraftRequest(BaseModel):
    content: str
    file_name: str | None = None


def _safe_markdown_filename(name: str) -> str:
    value = _SKILL_FILENAME_RE.sub("-", name.strip()).strip("-_").lower()
    return f"{value or 'skill'}.md"


def _resolve_skill_file_name(file_name: str | None, *, default_name: str) -> str:
    if not file_name:
        return default_name
    candidate = file_name.strip()
    normalized = os.path.normpath(candidate)
    if (
        not candidate
        or os.path.isabs(candidate)
        or normalized.startswith("..")
        or os.path.basename(normalized) != normalized
    ):
        raise HTTPException(status_code=400, detail="Skill file name must stay within the managed workspace package")
    stem, _ = os.path.splitext(normalized)
    return _safe_markdown_filename(stem or normalized)


def _ensure_skill_manager_workspace_extensions_loaded() -> None:
    skills_dir = skill_manager._skills_dir or os.path.join(settings.workspace_dir, "skills")
    manifest_roots = list(skill_manager._manifest_roots or [])
    changed = not bool(skill_manager._skills_dir)
    for root in default_manifest_roots_for_workspace(settings.workspace_dir):
        if root not in manifest_roots:
            manifest_roots.append(root)
            changed = True
    if changed:
        skill_manager.init(skills_dir, manifest_roots=manifest_roots)


def _validate_skill_content(content: str, *, path: str = "<draft>") -> dict[str, object]:
    errors: list[dict[str, str]] = []
    skill = parse_skill_content(content, path=path, errors=errors)
    if skill is None:
        return {
            "valid": False,
            "errors": errors,
            "skill": None,
            "runtime_ready": False,
            "missing_tools": [],
        }

    base_tools, _, _ = get_base_tools_and_active_skills()
    available_tool_names = {tool.name for tool in base_tools}
    missing_tools = [
        tool_name for tool_name in skill.requires_tools
        if tool_name not in available_tool_names
    ]
    return {
        "valid": True,
        "errors": [],
        "skill": {
            "name": skill.name,
            "description": skill.description,
            "requires_tools": skill.requires_tools,
            "user_invocable": skill.user_invocable,
            "enabled": skill.enabled,
            "file_path": skill.file_path,
        },
        "runtime_ready": not missing_tools and skill.enabled,
        "missing_tools": missing_tools,
    }


@router.get("/skills")
async def list_skills():
    """List all loaded skills with status."""
    return {"skills": skill_manager.list_skills()}


@router.get("/skills/diagnostics")
async def get_skill_diagnostics():
    return skill_manager.get_diagnostics()


@router.get("/skills/{name}/source")
async def get_skill_source(name: str):
    skill = skill_manager.get_skill(name)
    if skill is None or not skill.file_path:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    try:
        with open(skill.file_path, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read skill source: {exc}") from exc
    validation = _validate_skill_content(content, path=skill.file_path)
    return {
        "name": name,
        "file_path": skill.file_path,
        "content": content,
        **validation,
    }


@router.post("/skills/validate")
async def validate_skill_draft(req: SkillDraftRequest):
    return _validate_skill_content(req.content, path=req.file_name or "<draft>")


@router.post("/skills/save")
async def save_skill_draft(req: SkillDraftRequest):
    validation = _validate_skill_content(req.content, path=req.file_name or "<draft>")
    if not bool(validation["valid"]) or not isinstance(validation["skill"], dict):
        raise HTTPException(status_code=400, detail={"message": "Skill draft is invalid", **validation})
    file_name = _resolve_skill_file_name(
        req.file_name,
        default_name=_safe_markdown_filename(str(validation["skill"]["name"])),
    )
    _ensure_skill_manager_workspace_extensions_loaded()
    target_path = str(save_workspace_contribution("skills", file_name=file_name, content=req.content))
    skills = skill_manager.reload()
    await log_integration_event(
        integration_type="skill",
        name=str(validation["skill"]["name"]),
        outcome="succeeded",
        details={
            "saved_path": target_path,
            "validation": validation,
        },
    )
    return {
        "status": "saved",
        "file_path": target_path,
        "skills": skills,
        **_validate_skill_content(req.content, path=target_path),
    }


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
