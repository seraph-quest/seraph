"""Discover catalog API — browse and install skills/MCP servers."""

import json
import logging
import os
import shutil

from fastapi import APIRouter, HTTPException

from config.settings import settings
from src.skills.manager import skill_manager
from src.tools.mcp_manager import mcp_manager

logger = logging.getLogger(__name__)

router = APIRouter()

_DEFAULTS_DIR = os.path.join(os.path.dirname(__file__), "../defaults")
_CATALOG_PATH = os.path.join(_DEFAULTS_DIR, "skill-catalog.json")
_BUNDLED_SKILLS_DIR = os.path.join(_DEFAULTS_DIR, "skills")


def _load_catalog() -> dict:
    """Load the catalog JSON file."""
    path = os.path.normpath(_CATALOG_PATH)
    if not os.path.isfile(path):
        return {"skills": [], "mcp_servers": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _skill_installed(name: str) -> bool:
    """Check if a skill .md file exists in the workspace skills directory."""
    skills_dir = os.path.join(settings.workspace_dir, "skills")
    return os.path.isfile(os.path.join(skills_dir, f"{name}.md"))


def _mcp_installed(name: str) -> bool:
    """Check if an MCP server exists in the current config."""
    return name in mcp_manager._config


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
    catalog = _load_catalog()

    # Check skills first
    for skill in catalog.get("skills", []):
        if skill["name"] == name:
            if _skill_installed(name):
                raise HTTPException(status_code=409, detail=f"Skill '{name}' is already installed")

            if not skill.get("bundled", False):
                raise HTTPException(
                    status_code=400,
                    detail=f"Skill '{name}' is not bundled and cannot be auto-installed",
                )

            # Copy from bundled skills dir to workspace
            src = os.path.join(os.path.normpath(_BUNDLED_SKILLS_DIR), f"{name}.md")
            if not os.path.isfile(src):
                raise HTTPException(
                    status_code=404,
                    detail=f"Bundled skill file for '{name}' not found",
                )

            dst_dir = os.path.join(settings.workspace_dir, "skills")
            os.makedirs(dst_dir, exist_ok=True)
            shutil.copy2(src, os.path.join(dst_dir, f"{name}.md"))
            skill_manager.reload()
            return {"status": "installed", "name": name, "type": "skill"}

    # Check MCP servers
    for server in catalog.get("mcp_servers", []):
        if server["name"] == name:
            if _mcp_installed(name):
                raise HTTPException(status_code=409, detail=f"MCP server '{name}' is already installed")

            mcp_manager.add_server(
                name=name,
                url=server["url"],
                description=server.get("description", ""),
                enabled=False,
            )
            return {"status": "installed", "name": name, "type": "mcp_server"}

    raise HTTPException(status_code=404, detail=f"'{name}' not found in catalog")
