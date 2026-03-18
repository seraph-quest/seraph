"""Capability overview API — aggregated tools, skills, workflows, MCP, and starter packs."""

from __future__ import annotations

import json
import os
import shutil
from typing import Any

from fastapi import APIRouter, HTTPException

from config.settings import settings
from src.agent.factory import get_base_tools_and_active_skills
from src.audit.runtime import log_integration_event
from src.observer.manager import context_manager
from src.plugins.registry import TOOL_METADATA, get_tool_metadata
from src.skills.manager import skill_manager
from src.tools.mcp_manager import mcp_manager
from src.tools.policy import (
    get_current_mcp_policy_mode,
    get_current_tool_policy_mode,
    get_tool_execution_boundaries,
    get_tool_risk_level,
    is_tool_allowed,
)
from src.workflows.manager import workflow_manager

router = APIRouter()

_DEFAULTS_DIR = os.path.join(os.path.dirname(__file__), "../defaults")
_STARTER_PACKS_PATH = os.path.join(_DEFAULTS_DIR, "starter-packs.json")
_BUNDLED_SKILLS_DIR = os.path.join(_DEFAULTS_DIR, "skills")
_BUNDLED_WORKFLOWS_DIR = os.path.join(_DEFAULTS_DIR, "workflows")


def _load_starter_packs() -> list[dict[str, Any]]:
    path = os.path.normpath(_STARTER_PACKS_PATH)
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    packs = payload.get("packs", [])
    return packs if isinstance(packs, list) else []


def _seed_bundled_skill(name: str) -> bool:
    source = os.path.join(os.path.normpath(_BUNDLED_SKILLS_DIR), f"{name}.md")
    if not os.path.isfile(source):
        return False
    destination_dir = os.path.join(settings.workspace_dir, "skills")
    os.makedirs(destination_dir, exist_ok=True)
    shutil.copy2(source, os.path.join(destination_dir, f"{name}.md"))
    return True


def _seed_bundled_workflow(name: str) -> bool:
    source = os.path.join(os.path.normpath(_BUNDLED_WORKFLOWS_DIR), f"{name}.md")
    if not os.path.isfile(source):
        return False
    destination_dir = os.path.join(settings.workspace_dir, "workflows")
    os.makedirs(destination_dir, exist_ok=True)
    shutil.copy2(source, os.path.join(destination_dir, f"{name}.md"))
    return True


def _skill_status_map(available_tool_names: list[str]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    tool_set = set(available_tool_names)
    skills: list[dict[str, Any]] = []
    by_name: dict[str, dict[str, Any]] = {}
    for skill in skill_manager.list_skills():
        missing_tools = [
            tool_name
            for tool_name in skill.get("requires_tools", [])
            if tool_name not in tool_set
        ]
        enabled = bool(skill.get("enabled", False))
        if not enabled:
            availability = "disabled"
        elif missing_tools:
            availability = "blocked"
        else:
            availability = "ready"
        enriched = {
            **skill,
            "availability": availability,
            "missing_tools": missing_tools,
        }
        skills.append(enriched)
        by_name[str(skill["name"])] = enriched
    return skills, by_name


def _workflow_status_map(
    available_tool_names: list[str],
    active_skill_names: list[str],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    workflows = workflow_manager.list_workflows(
        available_tool_names=available_tool_names,
        active_skill_names=active_skill_names,
    )
    by_name: dict[str, dict[str, Any]] = {}
    for workflow in workflows:
        enabled = bool(workflow.get("enabled", False))
        is_available = bool(workflow.get("is_available", False))
        missing_tools = list(workflow.get("missing_tools", []))
        missing_skills = list(workflow.get("missing_skills", []))
        if not enabled:
            availability = "disabled"
        elif is_available:
            availability = "ready"
        else:
            availability = "blocked"
        workflow["availability"] = availability
        workflow["missing_tools"] = missing_tools
        workflow["missing_skills"] = missing_skills
        by_name[str(workflow["name"])] = workflow
    return workflows, by_name


def _tool_status_list(tool_mode: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for tool_name in sorted(TOOL_METADATA.keys()):
        metadata = get_tool_metadata(tool_name) or {}
        allowed = is_tool_allowed(tool_name, tool_mode)
        result.append({
            "name": tool_name,
            "description": metadata.get("description", ""),
            "policy_modes": metadata.get("policy_modes", []),
            "risk_level": get_tool_risk_level(tool_name),
            "execution_boundaries": get_tool_execution_boundaries(tool_name),
            "accepts_secret_refs": bool(metadata.get("accepts_secret_refs", False)),
            "availability": "ready" if allowed else "blocked",
            "blocked_reason": None if allowed else f"tool_policy_{tool_mode}",
        })
    return result


def _mcp_status_list(mcp_mode: str) -> list[dict[str, Any]]:
    servers = []
    for server in mcp_manager.get_config():
        status = server.get("status", "disconnected")
        enabled = bool(server.get("enabled", False))
        if not enabled:
            availability = "disabled"
            blocked_reason = "server_disabled"
        elif mcp_mode == "disabled":
            availability = "blocked"
            blocked_reason = "mcp_policy_disabled"
        elif status == "connected":
            availability = "ready"
            blocked_reason = None
        elif status == "auth_required":
            availability = "blocked"
            blocked_reason = "auth_required"
        elif status == "error":
            availability = "blocked"
            blocked_reason = "connection_error"
        else:
            availability = "blocked"
            blocked_reason = "disconnected"
        servers.append({
            **server,
            "availability": availability,
            "blocked_reason": blocked_reason,
        })
    return servers


def _starter_pack_statuses(
    *,
    skills_by_name: dict[str, dict[str, Any]],
    workflows_by_name: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    packs: list[dict[str, Any]] = []
    for pack in _load_starter_packs():
        skill_names = [str(item) for item in pack.get("skills", [])]
        workflow_names = [str(item) for item in pack.get("workflows", [])]

        ready_skills = [name for name in skill_names if skills_by_name.get(name, {}).get("availability") == "ready"]
        ready_workflows = [
            name for name in workflow_names
            if workflows_by_name.get(name, {}).get("availability") == "ready"
        ]
        blocked_skills = [
            {
                "name": name,
                "availability": skills_by_name.get(name, {}).get("availability", "missing"),
                "missing_tools": skills_by_name.get(name, {}).get("missing_tools", []),
            }
            for name in skill_names
            if name not in ready_skills
        ]
        blocked_workflows = [
            {
                "name": name,
                "availability": workflows_by_name.get(name, {}).get("availability", "missing"),
                "missing_tools": workflows_by_name.get(name, {}).get("missing_tools", []),
                "missing_skills": workflows_by_name.get(name, {}).get("missing_skills", []),
            }
            for name in workflow_names
            if name not in ready_workflows
        ]

        if len(ready_skills) == len(skill_names) and len(ready_workflows) == len(workflow_names):
            availability = "ready"
        elif ready_skills or ready_workflows:
            availability = "partial"
        else:
            availability = "blocked"

        packs.append({
            "name": pack["name"],
            "label": pack.get("label", pack["name"]),
            "description": pack.get("description", ""),
            "sample_prompt": pack.get("sample_prompt", ""),
            "skills": skill_names,
            "workflows": workflow_names,
            "ready_skills": ready_skills,
            "ready_workflows": ready_workflows,
            "blocked_skills": blocked_skills,
            "blocked_workflows": blocked_workflows,
            "availability": availability,
        })
    return packs


def _build_capability_overview() -> dict[str, Any]:
    base_tools, active_skill_names, mcp_mode = get_base_tools_and_active_skills()
    tool_mode = get_current_tool_policy_mode()
    available_tool_names = [tool.name for tool in base_tools]
    native_tools = _tool_status_list(tool_mode)
    skills, skills_by_name = _skill_status_map(available_tool_names)
    workflows, workflows_by_name = _workflow_status_map(available_tool_names, active_skill_names)
    mcp_servers = _mcp_status_list(mcp_mode)
    starter_packs = _starter_pack_statuses(
        skills_by_name=skills_by_name,
        workflows_by_name=workflows_by_name,
    )
    ready_tools = sum(1 for tool in native_tools if tool["availability"] == "ready")
    ready_skills = sum(1 for skill in skills if skill["availability"] == "ready")
    ready_workflows = sum(1 for workflow in workflows if workflow["availability"] == "ready")
    ready_packs = sum(1 for pack in starter_packs if pack["availability"] == "ready")
    return {
        "tool_policy_mode": tool_mode,
        "mcp_policy_mode": mcp_mode,
        "approval_mode": context_manager.get_context().approval_mode,
        "summary": {
            "native_tools_ready": ready_tools,
            "native_tools_total": len(native_tools),
            "skills_ready": ready_skills,
            "skills_total": len(skills),
            "workflows_ready": ready_workflows,
            "workflows_total": len(workflows),
            "starter_packs_ready": ready_packs,
            "starter_packs_total": len(starter_packs),
            "mcp_servers_ready": sum(1 for server in mcp_servers if server["availability"] == "ready"),
            "mcp_servers_total": len(mcp_servers),
        },
        "native_tools": native_tools,
        "skills": skills,
        "workflows": workflows,
        "mcp_servers": mcp_servers,
        "starter_packs": starter_packs,
    }


@router.get("/capabilities/overview")
async def get_capability_overview():
    return _build_capability_overview()


@router.post("/capabilities/starter-packs/{name}/activate")
async def activate_starter_pack(name: str):
    pack = next((item for item in _load_starter_packs() if item.get("name") == name), None)
    if pack is None:
        raise HTTPException(status_code=404, detail=f"Starter pack '{name}' not found")

    changed_skills: list[str] = []
    changed_workflows: list[str] = []
    missing_entries: list[str] = []

    for skill_name in pack.get("skills", []):
        if skill_manager.get_skill(skill_name) is None:
            if not _seed_bundled_skill(str(skill_name)):
                missing_entries.append(f"skill:{skill_name}")
                continue
            skill_manager.reload()
        if skill_manager.enable(str(skill_name)):
            changed_skills.append(str(skill_name))

    for workflow_name in pack.get("workflows", []):
        if workflow_manager.get_workflow(str(workflow_name)) is None:
            if not _seed_bundled_workflow(str(workflow_name)):
                missing_entries.append(f"workflow:{workflow_name}")
                continue
            workflow_manager.reload()
        if workflow_manager.enable(str(workflow_name)):
            changed_workflows.append(str(workflow_name))

    await log_integration_event(
        integration_type="starter_pack",
        name=str(name),
        outcome="succeeded" if not missing_entries else "degraded",
        details={
            "enabled_skills": changed_skills,
            "enabled_workflows": changed_workflows,
            "missing_entries": missing_entries,
        },
    )

    overview = _build_capability_overview()
    return {
        "status": "activated" if not missing_entries else "degraded",
        "name": name,
        "enabled_skills": changed_skills,
        "enabled_workflows": changed_workflows,
        "missing_entries": missing_entries,
        "overview": overview,
    }
