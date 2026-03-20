"""Capability overview API — aggregated tools, skills, workflows, MCP, and starter packs."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import shutil
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import select

from config.settings import settings
from src.agent.factory import get_base_tools_and_active_skills
from src.audit.runtime import log_integration_event
from src.db.engine import get_session as get_db
from src.db.models import UserProfile
from src.observer.manager import context_manager
from src.plugins.registry import TOOL_METADATA, get_tool_metadata
from src.skills.manager import skill_manager
from src.tools.mcp_manager import mcp_manager
from src.tools.policy import (
    MCP_POLICY_MODES,
    TOOL_POLICY_MODES,
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
_CATALOG_PATH = os.path.join(_DEFAULTS_DIR, "skill-catalog.json")
_BUNDLED_SKILLS_DIR = os.path.join(_DEFAULTS_DIR, "skills")
_BUNDLED_WORKFLOWS_DIR = os.path.join(_DEFAULTS_DIR, "workflows")
_SAFE_AUTOREPAIR_ACTION_TYPES = {
    "toggle_skill",
    "toggle_workflow",
    "toggle_mcp_server",
    "set_tool_policy",
    "set_mcp_policy",
    "install_catalog_item",
    "activate_starter_pack",
}
_BOOTSTRAP_ACTION_PRIORITY = {
    "install_catalog_item": 0,
    "toggle_skill": 1,
    "toggle_workflow": 2,
    "toggle_mcp_server": 3,
    "activate_starter_pack": 4,
    "set_mcp_policy": 5,
    "set_tool_policy": 6,
}


class CapabilityBootstrapRequest(BaseModel):
    target_type: str
    name: str


def _load_starter_packs() -> list[dict[str, Any]]:
    path = os.path.normpath(_STARTER_PACKS_PATH)
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    packs = payload.get("packs", [])
    return packs if isinstance(packs, list) else []


def _load_catalog_items() -> dict[str, list[dict[str, Any]]]:
    path = os.path.normpath(_CATALOG_PATH)
    if not os.path.isfile(path):
        return {"skills": [], "mcp_servers": []}
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return {
        "skills": payload.get("skills", []) if isinstance(payload.get("skills"), list) else [],
        "mcp_servers": (
            payload.get("mcp_servers", [])
            if isinstance(payload.get("mcp_servers"), list)
            else []
        ),
    }


async def _persist_runtime_mode(column: str, mode: str) -> None:
    async with get_db() as db:
        result = await db.execute(select(UserProfile).where(UserProfile.id == "singleton"))
        profile = result.scalars().first()
        if profile is None:
            profile = UserProfile(id="singleton")
        setattr(profile, column, mode)
        profile.updated_at = datetime.now(timezone.utc)
        db.add(profile)


async def _set_tool_policy_mode(mode: str) -> bool:
    if mode not in TOOL_POLICY_MODES:
        return False
    context_manager.update_tool_policy_mode(mode)
    await _persist_runtime_mode("tool_policy_mode", mode)
    return True


async def _set_mcp_policy_mode(mode: str) -> bool:
    if mode not in MCP_POLICY_MODES:
        return False
    context_manager.update_mcp_policy_mode(mode)
    await _persist_runtime_mode("mcp_policy_mode", mode)
    return True


def _catalog_skill_by_name() -> dict[str, dict[str, Any]]:
    return {
        str(item["name"]): item
        for item in _load_catalog_items().get("skills", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def _catalog_mcp_by_name() -> dict[str, dict[str, Any]]:
    return {
        str(item["name"]): item
        for item in _load_catalog_items().get("mcp_servers", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


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


def _install_catalog_item_by_name(name: str) -> tuple[bool, str]:
    catalog_skill = _catalog_skill_by_name().get(name)
    if catalog_skill is not None:
        if skill_manager.get_skill(name) is not None:
            return False, "already_installed"
        if not bool(catalog_skill.get("bundled", False)):
            return False, "not_bundled"
        if not _seed_bundled_skill(name):
            return False, "missing_bundle"
        skill_manager.reload()
        return True, "installed"

    catalog_mcp = _catalog_mcp_by_name().get(name)
    if catalog_mcp is not None:
        if any(server.get("name") == name for server in mcp_manager.get_config()):
            return False, "already_installed"
        url = catalog_mcp.get("url")
        if not isinstance(url, str) or not url.strip():
            return False, "missing_url"
        mcp_manager.add_server(
            name=name,
            url=url,
            description=str(catalog_mcp.get("description") or ""),
            enabled=False,
            headers=catalog_mcp.get("headers"),
            auth_hint=str(catalog_mcp.get("auth_hint") or ""),
        )
        return True, "installed"

    return False, "not_found"


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


def _workflow_draft(workflow: dict[str, Any]) -> str:
    inputs = []
    for input_name, spec in (workflow.get("inputs") or {}).items():
        if not isinstance(spec, dict):
            continue
        default = spec.get("default")
        if input_name == "file_path" and not default:
            default = "notes/output.md"
        if default is None or default == "":
            default = f"<{input_name}>"
        inputs.append(f'{input_name}={json.dumps(default)}')
    if inputs:
        return f'Run workflow "{workflow["name"]}" with {", ".join(inputs)}.'
    return f'Run workflow "{workflow["name"]}".'


def _workflow_blocking_reasons(workflow: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    availability = str(workflow.get("availability") or "unknown")
    if availability == "disabled":
        reasons.append("workflow disabled")
    for skill_name in workflow.get("missing_skills", []) or []:
        reasons.append(f"missing skill: {skill_name}")
    for tool_name in workflow.get("blocked_skill_tools", []) or []:
        reasons.append(f"blocked skill tool: {tool_name}")
    for tool_name in workflow.get("missing_tools", []) or []:
        reasons.append(f"missing tool: {tool_name}")
    return reasons


def _starter_pack_blocking_reasons(pack: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for blocked in pack.get("blocked_skills", []) or []:
        if not isinstance(blocked, dict):
            continue
        name = str(blocked.get("name") or "unknown")
        availability = str(blocked.get("availability") or "missing")
        if availability == "disabled":
            reasons.append(f"skill disabled: {name}")
        for tool_name in blocked.get("missing_tools", []) or []:
            reasons.append(f"skill {name} missing tool: {tool_name}")
    for blocked in pack.get("blocked_workflows", []) or []:
        if not isinstance(blocked, dict):
            continue
        name = str(blocked.get("name") or "unknown")
        availability = str(blocked.get("availability") or "missing")
        if availability == "disabled":
            reasons.append(f"workflow disabled: {name}")
        for skill_name in blocked.get("missing_skills", []) or []:
            reasons.append(f"workflow {name} missing skill: {skill_name}")
        for tool_name in blocked.get("missing_tools", []) or []:
            reasons.append(f"workflow {name} missing tool: {tool_name}")
    return reasons


def _suggested_tool_policy_mode(blocked_reason: str | None) -> str | None:
    if blocked_reason == "tool_policy_safe":
        return "balanced"
    if blocked_reason == "tool_policy_balanced":
        return "full"
    return None


def _next_tool_policy_mode(current_mode: str) -> str | None:
    if current_mode == "safe":
        return "balanced"
    if current_mode == "balanced":
        return "full"
    return None


def _recommended_tool_policy_mode(*, current_mode: str, blocked_reason: str | None) -> str | None:
    return _suggested_tool_policy_mode(blocked_reason) or _next_tool_policy_mode(current_mode)


def _starter_pack_index() -> dict[str, dict[str, Any]]:
    return {
        str(pack["name"]): pack
        for pack in _load_starter_packs()
        if isinstance(pack, dict) and isinstance(pack.get("name"), str)
    }


def _starter_pack_activation_would_change_state(
    pack: dict[str, Any],
    *,
    skills_by_name: dict[str, dict[str, Any]],
    workflows_by_name: dict[str, dict[str, Any]],
) -> bool:
    for skill_name in pack.get("skills", []):
        skill = skills_by_name.get(str(skill_name))
        if skill is None or not bool(skill.get("enabled", False)):
            return True
    for workflow_name in pack.get("workflows", []):
        workflow = workflows_by_name.get(str(workflow_name))
        if workflow is None or not bool(workflow.get("enabled", False)):
            return True
    return False


def _recommended_actions(
    *,
    skills_by_name: dict[str, dict[str, Any]],
    workflows_by_name: dict[str, dict[str, Any]],
    starter_packs: list[dict[str, Any]],
    native_tools: list[dict[str, Any]],
    mcp_servers: list[dict[str, Any]],
    tool_mode: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    tool_status = {str(tool["name"]): tool for tool in native_tools}
    starter_pack_index = _starter_pack_index()
    catalog = _load_catalog_items()
    catalog_skills = _catalog_skill_by_name()
    catalog_items: list[dict[str, Any]] = []
    recommendations: list[dict[str, Any]] = []
    seen_recommendations: set[str] = set()

    def add_recommendation(item: dict[str, Any]) -> None:
        key = str(item.get("id") or item.get("label") or len(recommendations))
        if key in seen_recommendations:
            return
        seen_recommendations.add(key)
        recommendations.append(item)

    for skill in catalog.get("skills", []):
        if not isinstance(skill, dict) or not isinstance(skill.get("name"), str):
            continue
        name = str(skill["name"])
        installed = name in skills_by_name
        missing_tools = [
            tool_name
            for tool_name in skill.get("requires_tools", [])
            if tool_name not in tool_status
        ]
        catalog_items.append({
            "name": name,
            "type": "skill",
            "description": skill.get("description", ""),
            "category": skill.get("category", ""),
            "bundled": bool(skill.get("bundled", False)),
            "installed": installed,
            "missing_tools": missing_tools,
            "recommended_actions": (
                []
                if installed
                else [{"type": "install_catalog_item", "label": "Install skill", "name": name}]
            ),
        })
        if not installed and bool(skill.get("bundled", False)):
            add_recommendation({
                "id": f"catalog-skill:{name}",
                "label": f"Install skill {name}",
                "description": skill.get("description", ""),
                "action": {"type": "install_catalog_item", "label": "Install skill", "name": name},
            })

    mcp_status = {str(server["name"]): server for server in mcp_servers}
    for server in catalog.get("mcp_servers", []):
        if not isinstance(server, dict) or not isinstance(server.get("name"), str):
            continue
        name = str(server["name"])
        installed = name in mcp_status
        catalog_items.append({
            "name": name,
            "type": "mcp_server",
            "description": server.get("description", ""),
            "category": server.get("category", ""),
            "bundled": bool(server.get("bundled", False)),
            "installed": installed,
            "missing_tools": [],
            "recommended_actions": (
                []
                if installed
                else [{"type": "install_catalog_item", "label": "Install MCP server", "name": name}]
            ),
        })
        if not installed:
            add_recommendation({
                "id": f"catalog-mcp:{name}",
                "label": f"Install MCP server {name}",
                "description": server.get("description", ""),
                "action": {"type": "install_catalog_item", "label": "Install MCP server", "name": name},
            })

    for workflow_name, workflow in workflows_by_name.items():
        for missing_skill in workflow.get("missing_skills", []):
            if missing_skill in skills_by_name:
                continue
            catalog_skill = catalog_skills.get(str(missing_skill))
            if catalog_skill is None:
                continue
            add_recommendation({
                "id": f"catalog-skill:{missing_skill}",
                "label": f"Install skill {missing_skill}",
                "description": catalog_skill.get("description", ""),
                "action": {"type": "install_catalog_item", "label": "Install skill", "name": missing_skill},
            })
        pack = next(
            (
                item
                for item in starter_pack_index.values()
                if workflow_name in [str(value) for value in item.get("workflows", [])]
            ),
            None,
        )
        if (
            workflow.get("availability") == "blocked"
            and pack is not None
            and _starter_pack_activation_would_change_state(
                pack,
                skills_by_name=skills_by_name,
                workflows_by_name=workflows_by_name,
            )
        ):
            add_recommendation({
                "id": f"starter-pack:{pack['name']}",
                "label": f"Activate {pack.get('label', pack['name'])}",
                "description": pack.get("description", ""),
                "action": {
                    "type": "activate_starter_pack",
                    "label": "Activate pack",
                    "name": pack["name"],
                },
            })
        for missing_tool in workflow.get("missing_tools", []):
            blocked_tool = tool_status.get(str(missing_tool))
            suggested_mode = _recommended_tool_policy_mode(
                current_mode=tool_mode,
                blocked_reason=None if blocked_tool is None else blocked_tool.get("blocked_reason"),
            )
            if suggested_mode is not None:
                add_recommendation({
                    "id": f"tool-policy:{missing_tool}:{suggested_mode}",
                    "label": f"Allow {missing_tool}",
                    "description": f"Change tool policy to {suggested_mode} so {workflow_name} can run.",
                    "action": {
                        "type": "set_tool_policy",
                        "label": f"Set tool policy to {suggested_mode}",
                        "mode": suggested_mode,
                    },
                })

    runbooks: list[dict[str, Any]] = []
    packs_by_name = {
        str(pack["name"]): pack
        for pack in starter_packs
        if isinstance(pack, dict) and isinstance(pack.get("name"), str)
    }
    for pack_name, pack in packs_by_name.items():
        sample_prompt = str(pack.get("sample_prompt") or "").strip()
        if not sample_prompt:
            continue
        runbooks.append({
            "id": f"starter-pack:{pack_name}",
            "name": pack_name,
            "label": str(pack.get("label") or pack_name),
            "description": pack.get("description", ""),
            "source": "starter_pack",
            "command": sample_prompt,
            "availability": pack.get("availability", "blocked"),
            "blocking_reasons": _starter_pack_blocking_reasons(pack),
            "recommended_actions": pack.get("recommended_actions", []),
            "parameter_schema": {},
            "risk_level": "medium",
            "execution_boundaries": ["capability_activation"],
            "action": {"type": "activate_starter_pack", "label": "Activate pack", "name": pack_name},
        })

    for workflow in workflows_by_name.values():
        if not bool(workflow.get("user_invocable", False)):
            continue
        runbooks.append({
            "id": f"workflow:{workflow['name']}",
            "name": workflow["name"],
            "label": f"Run {workflow['name']}",
            "description": workflow.get("description", ""),
            "source": "workflow",
            "command": _workflow_draft(workflow),
            "availability": workflow.get("availability", "blocked"),
            "blocking_reasons": _workflow_blocking_reasons(workflow),
            "recommended_actions": workflow.get("recommended_actions", []),
            "parameter_schema": workflow.get("inputs", {}),
            "risk_level": workflow.get("risk_level", "high"),
            "execution_boundaries": workflow.get("execution_boundaries", []),
            "action": {"type": "draft_workflow", "label": "Draft workflow", "name": workflow["name"]},
        })

    return catalog_items, recommendations, runbooks


def _starter_pack_recommended_actions(
    pack: dict[str, Any],
    *,
    skills_by_name: dict[str, dict[str, Any]],
    native_tools: list[dict[str, Any]],
    tool_mode: str,
    workflows_by_name: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    catalog_skills = _catalog_skill_by_name()
    if pack.get("availability") != "ready" and _starter_pack_activation_would_change_state(
        pack,
        skills_by_name=skills_by_name,
        workflows_by_name=workflows_by_name,
    ):
        actions.append({
            "type": "activate_starter_pack",
            "label": "Activate pack",
            "name": pack["name"],
        })

    tool_status = {str(tool["name"]): tool for tool in native_tools}
    seen_tool_modes: set[tuple[str, str]] = set()
    for blocked in [*pack.get("blocked_skills", []), *pack.get("blocked_workflows", [])]:
        if not isinstance(blocked, dict):
            continue
        blocked_name = str(blocked.get("name") or "")
        if blocked_name and str(blocked.get("availability") or "missing") == "missing":
            catalog_skill = catalog_skills.get(blocked_name)
            if catalog_skill is not None:
                actions.append({
                    "type": "install_catalog_item",
                    "label": f"Install {blocked_name}",
                    "name": blocked_name,
                })
        for missing_tool in blocked.get("missing_tools", []) or []:
            blocked_tool = tool_status.get(str(missing_tool))
            suggested_mode = _recommended_tool_policy_mode(
                current_mode=tool_mode,
                blocked_reason=None if blocked_tool is None else blocked_tool.get("blocked_reason"),
            )
            if suggested_mode is None:
                continue
            key = (str(missing_tool), suggested_mode)
            if key in seen_tool_modes:
                continue
            seen_tool_modes.add(key)
            actions.append({
                "type": "set_tool_policy",
                "label": f"Allow {missing_tool}",
                "mode": suggested_mode,
            })
    return actions


async def _activate_starter_pack_by_name(name: str) -> dict[str, Any]:
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
    return {
        "status": "activated" if not missing_entries else "degraded",
        "name": name,
        "enabled_skills": changed_skills,
        "enabled_workflows": changed_workflows,
        "missing_entries": missing_entries,
    }


def _action_key(action: dict[str, Any]) -> tuple[str, str | None, str | None, bool | None, str | None]:
    return (
        str(action.get("type") or ""),
        str(action.get("name")) if action.get("name") is not None else None,
        str(action.get("mode")) if action.get("mode") is not None else None,
        bool(action.get("enabled")) if action.get("enabled") is not None else None,
        str(action.get("target")) if action.get("target") is not None else None,
    )


def _ordered_bootstrap_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        actions,
        key=lambda action: (
            _BOOTSTRAP_ACTION_PRIORITY.get(str(action.get("type") or ""), 99),
            str(action.get("label") or ""),
        ),
    )


async def _apply_safe_capability_action(action: dict[str, Any]) -> dict[str, Any]:
    action_type = str(action.get("type") or "")
    label = str(action.get("label") or action_type or "action")
    name = str(action.get("name") or "") or None
    mode = str(action.get("mode") or "") or None

    if action_type not in _SAFE_AUTOREPAIR_ACTION_TYPES:
        return {"type": action_type, "label": label, "status": "unsupported"}

    match action_type:
        case "toggle_skill":
            if not name:
                return {"type": action_type, "label": label, "status": "invalid"}
            enabled = bool(action.get("enabled", True))
            ok = skill_manager.enable(name) if enabled else skill_manager.disable(name)
            return {
                "type": action_type,
                "label": label,
                "name": name,
                "enabled": enabled,
                "status": "applied" if ok else "failed",
            }
        case "toggle_workflow":
            if not name:
                return {"type": action_type, "label": label, "status": "invalid"}
            enabled = bool(action.get("enabled", True))
            ok = workflow_manager.enable(name) if enabled else workflow_manager.disable(name)
            return {
                "type": action_type,
                "label": label,
                "name": name,
                "enabled": enabled,
                "status": "applied" if ok else "failed",
            }
        case "toggle_mcp_server":
            if not name:
                return {"type": action_type, "label": label, "status": "invalid"}
            enabled = bool(action.get("enabled", True))
            ok = mcp_manager.update_server(name, enabled=enabled)
            return {
                "type": action_type,
                "label": label,
                "name": name,
                "enabled": enabled,
                "status": "applied" if ok else "failed",
            }
        case "set_tool_policy":
            ok = await _set_tool_policy_mode(str(mode or ""))
            return {
                "type": action_type,
                "label": label,
                "mode": mode,
                "status": "applied" if ok else "failed",
            }
        case "set_mcp_policy":
            ok = await _set_mcp_policy_mode(str(mode or ""))
            return {
                "type": action_type,
                "label": label,
                "mode": mode,
                "status": "applied" if ok else "failed",
            }
        case "install_catalog_item":
            if not name:
                return {"type": action_type, "label": label, "status": "invalid"}
            ok, detail = _install_catalog_item_by_name(name)
            return {
                "type": action_type,
                "label": label,
                "name": name,
                "status": "applied" if ok else "noop",
                "detail": detail,
            }
        case "activate_starter_pack":
            if not name:
                return {"type": action_type, "label": label, "status": "invalid"}
            result = await _activate_starter_pack_by_name(name)
            return {
                "type": action_type,
                "label": label,
                "name": name,
                "status": result["status"],
                "detail": {
                    "enabled_skills": result["enabled_skills"],
                    "enabled_workflows": result["enabled_workflows"],
                    "missing_entries": result["missing_entries"],
                },
            }
        case _:
            return {"type": action_type, "label": label, "status": "unsupported"}


def _manual_bootstrap_actions(preflight: dict[str, Any], *, seen: set[tuple[str, str | None, str | None, bool | None, str | None]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for action in preflight.get("recommended_actions", []) or []:
        if not isinstance(action, dict):
            continue
        if _action_key(action) in seen:
            continue
        if str(action.get("type") or "") in _SAFE_AUTOREPAIR_ACTION_TYPES:
            continue
        results.append(action)
    return results


def _action_made_progress(action: dict[str, Any]) -> bool:
    status = str(action.get("status") or "")
    return status in {"applied", "activated"}


def _attach_skill_actions(
    skills: list[dict[str, Any]],
    *,
    native_tools: list[dict[str, Any]],
    tool_mode: str,
) -> None:
    tool_status = {str(tool["name"]): tool for tool in native_tools}
    for skill in skills:
        actions: list[dict[str, Any]] = []
        if not skill.get("enabled", False):
            actions.append({
                "type": "toggle_skill",
                "label": "Enable skill",
                "name": skill["name"],
                "enabled": True,
            })
        for missing_tool in skill.get("missing_tools", []):
            blocked_tool = tool_status.get(str(missing_tool))
            suggested_mode = _recommended_tool_policy_mode(
                current_mode=tool_mode,
                blocked_reason=None if blocked_tool is None else blocked_tool.get("blocked_reason"),
            )
            if suggested_mode is not None:
                actions.append({
                    "type": "set_tool_policy",
                    "label": f"Allow {missing_tool}",
                    "mode": suggested_mode,
                })
        skill["recommended_actions"] = actions


def _attach_workflow_actions(
    workflows: list[dict[str, Any]],
    *,
    native_tools: list[dict[str, Any]],
    skills_by_name: dict[str, dict[str, Any]],
    tool_mode: str,
) -> None:
    tool_status = {str(tool["name"]): tool for tool in native_tools}
    catalog_skills = _catalog_skill_by_name()
    for workflow in workflows:
        actions: list[dict[str, Any]] = []
        blocked_skill_tools: list[str] = []
        if not workflow.get("enabled", False):
            actions.append({
                "type": "toggle_workflow",
                "label": "Enable workflow",
                "name": workflow["name"],
                "enabled": True,
            })
        for missing_skill in workflow.get("missing_skills", []):
            skill = skills_by_name.get(str(missing_skill))
            if skill is not None:
                if not skill.get("enabled", False):
                    actions.append({
                        "type": "toggle_skill",
                        "label": f"Enable {missing_skill}",
                        "name": missing_skill,
                        "enabled": True,
                    })
                blocked_skill_tools.extend(
                    str(tool_name)
                    for tool_name in skill.get("missing_tools", []) or []
                    if isinstance(tool_name, str)
                )
                continue
            if str(missing_skill) in catalog_skills:
                actions.append({
                    "type": "install_catalog_item",
                    "label": f"Install {missing_skill}",
                    "name": str(missing_skill),
                })
        for missing_tool in [
            *workflow.get("missing_tools", []),
            *blocked_skill_tools,
        ]:
            blocked_tool = tool_status.get(str(missing_tool))
            suggested_mode = _recommended_tool_policy_mode(
                current_mode=tool_mode,
                blocked_reason=None if blocked_tool is None else blocked_tool.get("blocked_reason"),
            )
            if suggested_mode is not None:
                actions.append({
                    "type": "set_tool_policy",
                    "label": f"Allow {missing_tool}",
                    "mode": suggested_mode,
                })
        workflow["blocked_skill_tools"] = sorted(set(blocked_skill_tools))
        if workflow.get("availability") == "ready" and bool(workflow.get("user_invocable", False)):
            actions.append({
                "type": "draft_workflow",
                "label": "Draft workflow",
                "name": workflow["name"],
            })
        workflow["recommended_actions"] = actions


def _attach_tool_actions(native_tools: list[dict[str, Any]]) -> None:
    for tool in native_tools:
        actions: list[dict[str, Any]] = []
        suggested_mode = _suggested_tool_policy_mode(tool.get("blocked_reason"))
        if suggested_mode is not None:
            actions.append({
                "type": "set_tool_policy",
                "label": f"Set tool policy to {suggested_mode}",
                "mode": suggested_mode,
            })
        tool["recommended_actions"] = actions


def _attach_mcp_actions(mcp_servers: list[dict[str, Any]], *, mcp_mode: str) -> None:
    for server in mcp_servers:
        actions: list[dict[str, Any]] = []
        if not server.get("enabled", False):
            actions.append({
                "type": "toggle_mcp_server",
                "label": "Enable server",
                "name": server["name"],
                "enabled": True,
            })
        if server.get("availability") == "blocked":
            if server.get("blocked_reason") == "mcp_policy_disabled":
                actions.append({
                    "type": "set_mcp_policy",
                    "label": "Allow MCP with approval",
                    "mode": "approval" if mcp_mode == "disabled" else "full",
                })
            elif server.get("blocked_reason") in {"auth_required", "connection_error", "disconnected"}:
                actions.append({
                    "type": "test_mcp_server",
                    "label": "Test connection",
                    "name": server["name"],
                })
                actions.append({
                    "type": "open_settings",
                    "label": "Open settings",
                    "target": "mcp",
                })
        server["recommended_actions"] = actions


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
    native_tools: list[dict[str, Any]],
    tool_mode: str,
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
            "recommended_actions": _starter_pack_recommended_actions(
                {
                    "name": pack["name"],
                    "skills": skill_names,
                    "workflows": workflow_names,
                    "availability": availability,
                    "blocked_skills": blocked_skills,
                    "blocked_workflows": blocked_workflows,
                },
                skills_by_name=skills_by_name,
                native_tools=native_tools,
                tool_mode=tool_mode,
                workflows_by_name=workflows_by_name,
            ),
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
        native_tools=native_tools,
        tool_mode=tool_mode,
    )
    _attach_tool_actions(native_tools)
    _attach_skill_actions(skills, native_tools=native_tools, tool_mode=tool_mode)
    _attach_workflow_actions(
        workflows,
        native_tools=native_tools,
        skills_by_name=skills_by_name,
        tool_mode=tool_mode,
    )
    _attach_mcp_actions(mcp_servers, mcp_mode=mcp_mode)
    catalog_items, recommendations, runbooks = _recommended_actions(
        skills_by_name=skills_by_name,
        workflows_by_name=workflows_by_name,
        starter_packs=starter_packs,
        native_tools=native_tools,
        mcp_servers=mcp_servers,
        tool_mode=tool_mode,
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
        "catalog_items": catalog_items,
        "recommendations": recommendations,
        "runbooks": runbooks,
    }


@router.get("/capabilities/overview")
async def get_capability_overview():
    return _build_capability_overview()


def _capability_preflight_payload(
    *,
    overview: dict[str, Any],
    target_type: str,
    name: str,
) -> dict[str, Any]:
    availability = "unknown"
    label = name
    description = ""
    command: str | None = None
    parameter_schema: dict[str, Any] = {}
    risk_level: str | None = None
    execution_boundaries: list[str] = []
    recommended_actions: list[dict[str, Any]] = []
    blocking_reasons: list[str] = []

    if target_type == "workflow":
        item = next(
            (workflow for workflow in overview["workflows"] if workflow.get("name") == name),
            None,
        )
        if item is None:
            raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")
        availability = str(item.get("availability") or "unknown")
        label = str(item.get("name") or name)
        description = str(item.get("description") or "")
        command = _workflow_draft(item)
        parameter_schema = (
            item.get("inputs") if isinstance(item.get("inputs"), dict) else {}
        )
        risk_level = str(item.get("risk_level") or "unknown")
        execution_boundaries = [
            str(value)
            for value in item.get("execution_boundaries", []) or []
            if isinstance(value, str)
        ]
        recommended_actions = [
            action for action in item.get("recommended_actions", []) or []
            if isinstance(action, dict)
        ]
        blocking_reasons = _workflow_blocking_reasons(item)
    elif target_type == "starter_pack":
        item = next(
            (pack for pack in overview["starter_packs"] if pack.get("name") == name),
            None,
        )
        if item is None:
            raise HTTPException(status_code=404, detail=f"Starter pack '{name}' not found")
        availability = str(item.get("availability") or "unknown")
        label = str(item.get("label") or name)
        description = str(item.get("description") or "")
        command = str(item.get("sample_prompt") or "") or None
        recommended_actions = [
            action for action in item.get("recommended_actions", []) or []
            if isinstance(action, dict)
        ]
        blocking_reasons = _starter_pack_blocking_reasons(item)
        risk_level = "medium"
        execution_boundaries = ["capability_activation"]
    elif target_type == "runbook":
        item = next(
            (runbook for runbook in overview["runbooks"] if runbook.get("id") == name),
            None,
        )
        if item is None:
            raise HTTPException(status_code=404, detail=f"Runbook '{name}' not found")
        availability = str(item.get("availability") or "unknown")
        label = str(item.get("label") or name)
        description = str(item.get("description") or "")
        command = str(item.get("command") or "") or None
        parameter_schema = (
            item.get("parameter_schema")
            if isinstance(item.get("parameter_schema"), dict)
            else {}
        )
        risk_level = str(item.get("risk_level") or "unknown")
        execution_boundaries = [
            str(value)
            for value in item.get("execution_boundaries", []) or []
            if isinstance(value, str)
        ]
        recommended_actions = [
            action for action in item.get("recommended_actions", []) or []
            if isinstance(action, dict)
        ]
        blocking_reasons = [
            str(value)
            for value in item.get("blocking_reasons", []) or []
            if isinstance(value, str)
        ]
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported target_type '{target_type}'")

    autorepair_actions = [
        action
        for action in recommended_actions
        if isinstance(action.get("type"), str) and action["type"] in _SAFE_AUTOREPAIR_ACTION_TYPES
    ]

    return {
        "target_type": target_type,
        "name": name,
        "label": label,
        "description": description,
        "availability": availability,
        "blocking_reasons": blocking_reasons,
        "recommended_actions": recommended_actions,
        "command": command,
        "parameter_schema": parameter_schema,
        "risk_level": risk_level,
        "execution_boundaries": execution_boundaries,
        "autorepair_actions": autorepair_actions,
        "can_autorepair": bool(autorepair_actions),
        "ready": availability == "ready",
    }


@router.get("/capabilities/preflight")
async def get_capability_preflight(
    target_type: str = Query(...),
    name: str = Query(...),
):
    overview = _build_capability_overview()
    return _capability_preflight_payload(
        overview=overview,
        target_type=target_type,
        name=name,
    )


@router.post("/capabilities/starter-packs/{name}/activate")
async def activate_starter_pack(name: str):
    result = await _activate_starter_pack_by_name(name)
    overview = _build_capability_overview()
    return {
        **result,
        "overview": overview,
    }


@router.post("/capabilities/bootstrap")
async def bootstrap_capability(body: CapabilityBootstrapRequest):
    overview_before = _build_capability_overview()
    preflight = _capability_preflight_payload(
        overview=overview_before,
        target_type=body.target_type,
        name=body.name,
    )
    seen_actions: set[tuple[str, str | None, str | None, bool | None, str | None]] = set()
    applied_actions: list[dict[str, Any]] = []
    manual_actions: list[dict[str, Any]] = []

    for _ in range(6):
        if preflight["ready"]:
            break
        safe_actions = [
            action
            for action in preflight.get("autorepair_actions", []) or []
            if isinstance(action, dict) and _action_key(action) not in seen_actions
        ]
        if not safe_actions:
            break
        for action in _ordered_bootstrap_actions(safe_actions):
            seen_actions.add(_action_key(action))
            applied_actions.append(await _apply_safe_capability_action(action))
        refreshed = _build_capability_overview()
        preflight = _capability_preflight_payload(
            overview=refreshed,
            target_type=body.target_type,
            name=body.name,
        )

    manual_actions = _manual_bootstrap_actions(preflight, seen=seen_actions)
    outcome = (
        "ready"
        if preflight["ready"]
        else ("partially_repaired" if any(_action_made_progress(action) for action in applied_actions) else "blocked")
    )
    await log_integration_event(
        integration_type="capability_bootstrap",
        name=f"{body.target_type}:{body.name}",
        outcome="succeeded" if preflight["ready"] else "degraded",
        details={
            "target_type": body.target_type,
            "ready_before": overview_before.get("summary", {}),
            "availability_after": preflight["availability"],
            "blocking_reasons_after": preflight["blocking_reasons"],
            "applied_actions": applied_actions,
            "manual_actions": manual_actions,
            "command_ready": bool(preflight.get("command")) and preflight["ready"],
        },
    )
    return {
        "target_type": body.target_type,
        "name": body.name,
        "label": preflight["label"],
        "status": outcome,
        "ready": preflight["ready"],
        "availability": preflight["availability"],
        "blocking_reasons": preflight["blocking_reasons"],
        "applied_actions": applied_actions,
        "manual_actions": manual_actions,
        "command": preflight["command"] if preflight["ready"] else None,
        "parameter_schema": preflight["parameter_schema"],
        "risk_level": preflight["risk_level"],
        "execution_boundaries": preflight["execution_boundaries"],
        "overview": _build_capability_overview(),
    }
