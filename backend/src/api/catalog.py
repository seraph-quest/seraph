"""Discover catalog API — browse and install skills/MCP servers."""

from __future__ import annotations

import json
import logging
import os
import shutil
from typing import Any

from fastapi import APIRouter, HTTPException

from config.settings import settings
from src.extensions.registry import ExtensionRegistry, bundled_manifest_root
from src.skills.loader import scan_skill_paths
from src.skills.manager import skill_manager
from src.tools.mcp_manager import mcp_manager

logger = logging.getLogger(__name__)

router = APIRouter()

_DEFAULTS_DIR = os.path.join(os.path.dirname(__file__), "../defaults")
_CATALOG_PATH = os.path.join(_DEFAULTS_DIR, "skill-catalog.json")


def load_catalog_items() -> dict[str, list[dict[str, Any]]]:
    """Load catalog JSON and normalize top-level collections."""
    path = os.path.normpath(_CATALOG_PATH)
    if not os.path.isfile(path):
        return {"skills": [], "mcp_servers": []}
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return {
        "skills": payload.get("skills", []) if isinstance(payload.get("skills"), list) else [],
        "mcp_servers": payload.get("mcp_servers", []) if isinstance(payload.get("mcp_servers"), list) else [],
    }


def _load_catalog() -> dict[str, list[dict[str, Any]]]:
    return load_catalog_items()


def _skill_installed(name: str) -> bool:
    """Check if a skill .md file exists in the workspace skills directory."""
    skills_dir = os.path.join(settings.workspace_dir, "skills")
    return _skill_loaded(name) or os.path.isfile(os.path.join(skills_dir, f"{name}.md"))


def _skill_loaded(name: str) -> bool:
    return skill_manager.get_skill(name) is not None


def _mcp_installed(name: str) -> bool:
    """Check if an MCP server exists in the current config."""
    return name in mcp_manager._config


def _bundled_skill_source_by_name(name: str) -> str | None:
    registry = ExtensionRegistry(
        manifest_roots=[bundled_manifest_root()],
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    )
    contribution_paths = [
        str(resolved_path)
        for contribution in registry.snapshot().list_contributions("skills")
        if isinstance((resolved_path := contribution.metadata.get("resolved_path")), str) and resolved_path
    ]
    skills, _ = scan_skill_paths(contribution_paths)
    for skill in skills:
        if skill.name == name:
            return skill.file_path
    return None


def catalog_skill_by_name() -> dict[str, dict[str, Any]]:
    return {
        str(item["name"]): item
        for item in _load_catalog().get("skills", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def catalog_mcp_by_name() -> dict[str, dict[str, Any]]:
    return {
        str(item["name"]): item
        for item in _load_catalog().get("mcp_servers", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def install_catalog_item_by_name(name: str) -> dict[str, Any]:
    """Install a bundled skill or MCP server from the catalog."""
    catalog_skill = catalog_skill_by_name().get(name)
    if catalog_skill is not None:
        if _skill_loaded(name):
            return {
                "ok": False,
                "status": "already_installed",
                "name": name,
                "type": "skill",
                "bundled": bool(catalog_skill.get("bundled", False)),
            }
        if _skill_installed(name):
            skill_manager.reload()
            if _skill_loaded(name):
                return {
                    "ok": False,
                    "status": "already_installed",
                    "name": name,
                    "type": "skill",
                    "bundled": bool(catalog_skill.get("bundled", False)),
                }
            return {
                "ok": False,
                "status": "installed_file_invalid",
                "name": name,
                "type": "skill",
                "bundled": bool(catalog_skill.get("bundled", False)),
            }
        if not bool(catalog_skill.get("bundled", False)):
            return {
                "ok": False,
                "status": "not_bundled",
                "name": name,
                "type": "skill",
                "bundled": False,
            }
        skill_manager.reload()
        if _skill_loaded(name):
            return {
                "ok": True,
                "status": "installed",
                "name": name,
                "type": "skill",
                "bundled": True,
            }
        src = _bundled_skill_source_by_name(name)
        if not src or not os.path.isfile(src):
            return {
                "ok": False,
                "status": "missing_bundle",
                "name": name,
                "type": "skill",
                "bundled": True,
            }

        dst_dir = os.path.join(settings.workspace_dir, "skills")
        os.makedirs(dst_dir, exist_ok=True)
        shutil.copy2(src, os.path.join(dst_dir, f"{name}.md"))
        skill_manager.reload()
        if not _skill_loaded(name):
            return {
                "ok": False,
                "status": "installed_file_invalid",
                "name": name,
                "type": "skill",
                "bundled": True,
            }
        return {
            "ok": True,
            "status": "installed",
            "name": name,
            "type": "skill",
            "bundled": True,
        }

    catalog_mcp = catalog_mcp_by_name().get(name)
    if catalog_mcp is not None:
        if _mcp_installed(name):
            return {
                "ok": False,
                "status": "already_installed",
                "name": name,
                "type": "mcp_server",
                "bundled": bool(catalog_mcp.get("bundled", False)),
            }
        url = catalog_mcp.get("url")
        if not isinstance(url, str) or not url.strip():
            return {
                "ok": False,
                "status": "missing_url",
                "name": name,
                "type": "mcp_server",
                "bundled": bool(catalog_mcp.get("bundled", False)),
            }
        mcp_manager.add_server(
            name=name,
            url=url,
            description=str(catalog_mcp.get("description") or ""),
            enabled=False,
            headers=catalog_mcp.get("headers"),
            auth_hint=str(catalog_mcp.get("auth_hint") or ""),
        )
        return {
            "ok": True,
            "status": "installed",
            "name": name,
            "type": "mcp_server",
            "bundled": bool(catalog_mcp.get("bundled", False)),
        }

    return {
        "ok": False,
        "status": "not_found",
        "name": name,
        "type": "unknown",
        "bundled": False,
    }


@router.get("/catalog")
async def get_catalog():
    """Return catalog items enriched with install status."""
    catalog = _load_catalog()
    items = []

    for skill in catalog.get("skills", []):
        items.append({
            "name": skill["name"],
            "type": "skill",
            "description": skill.get("description", ""),
            "category": skill.get("category", ""),
            "requires_tools": skill.get("requires_tools", []),
            "installed": _skill_installed(skill["name"]),
            "bundled": skill.get("bundled", False),
        })

    for server in catalog.get("mcp_servers", []):
        items.append({
            "name": server["name"],
            "type": "mcp_server",
            "description": server.get("description", ""),
            "category": server.get("category", ""),
            "requires_tools": [],
            "installed": _mcp_installed(server["name"]),
            "bundled": server.get("bundled", False),
        })

    return {"items": items}


@router.post("/catalog/install/{name}", status_code=201)
async def install_item(name: str):
    """Install a skill or MCP server from the catalog."""
    result = install_catalog_item_by_name(name)
    if result["ok"]:
        return {"status": result["status"], "name": name, "type": result["type"]}
    if result["status"] == "already_installed":
        detail = (
            f"Skill '{name}' is already installed"
            if result["type"] == "skill"
            else f"MCP server '{name}' is already installed"
        )
        raise HTTPException(status_code=409, detail=detail)
    if result["status"] == "not_bundled":
        raise HTTPException(
            status_code=400,
            detail=f"Skill '{name}' is not bundled and cannot be auto-installed",
        )
    if result["status"] == "missing_bundle":
        raise HTTPException(
            status_code=404,
            detail=f"Bundled skill file for '{name}' not found",
        )
    if result["status"] == "installed_file_invalid":
        raise HTTPException(
            status_code=422,
            detail=f"Installed skill '{name}' could not be loaded; fix or replace the skill file before retrying",
        )
    if result["status"] == "missing_url":
        raise HTTPException(
            status_code=400,
            detail=f"MCP server '{name}' is missing a URL in the catalog",
        )
    raise HTTPException(status_code=404, detail=f"'{name}' not found in catalog")
