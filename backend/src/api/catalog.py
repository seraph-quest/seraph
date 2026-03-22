"""Discover catalog API — browse and install skills/MCP servers."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any

from fastapi import APIRouter, HTTPException
import yaml

from config.settings import settings
from src.extensions.lifecycle import install_extension_path
from src.extensions.permissions import evaluate_tool_permissions
from src.extensions.registry import ExtensionRegistry, bundled_manifest_root
from src.extensions.scaffold import validate_extension_package
from src.skills.loader import parse_skill_content, scan_skill_paths
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
    """Check if a skill exists in the current runtime or workspace package roots."""
    if _skill_loaded(name):
        return True
    registry = ExtensionRegistry(
        manifest_roots=[os.path.join(settings.workspace_dir, "extensions")],
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    )
    for contribution in registry.snapshot().list_contributions("skills"):
        if str(contribution.metadata.get("name") or "") == name:
            return True
    return False


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


def _slugify(value: str) -> str:
    return "".join(char.lower() if char.isalnum() else "-" for char in value).strip("-") or "connector"


def _catalog_mcp_extension_id(name: str) -> str:
    return f"seraph.catalog-mcp-{_slugify(name)}"


def _catalog_skill_extension_id(name: str) -> str:
    return f"seraph.catalog-skill-{_slugify(name)}"


def _write_catalog_skill_package(root: str, item: dict[str, Any], source_path: str) -> str:
    name = str(item.get("name") or "").strip()
    if not name:
        raise ValueError("catalog skill item is missing a name")
    package_dir = Path(root) / _slugify(name)
    skills_dir = package_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    content = Path(source_path).read_text(encoding="utf-8")
    skill = parse_skill_content(content, path=source_path, errors=[])
    if skill is None:
        raise ValueError(f"catalog skill '{name}' could not be parsed from its bundled source")
    permission_profile = evaluate_tool_permissions(None, tool_names=list(skill.requires_tools))
    manifest_path = package_dir / "manifest.yaml"
    manifest_payload = {
        "id": _catalog_skill_extension_id(name),
        "version": "2026.3.21",
        "display_name": name,
        "kind": "capability-pack",
        "compatibility": {"seraph": ">=2026.3.19"},
        "publisher": {"name": "Seraph Catalog"},
        "trust": "local",
        "contributes": {
            "skills": [f"skills/{_slugify(name)}.md"],
        },
        "permissions": {
            "tools": list(permission_profile["required_tools"]),
            "execution_boundaries": list(permission_profile["required_execution_boundaries"]),
            "network": bool(permission_profile["requires_network"]),
        },
    }
    manifest_path.write_text(yaml.safe_dump(manifest_payload, sort_keys=False), encoding="utf-8")
    (skills_dir / f"{_slugify(name)}.md").write_text(content, encoding="utf-8")
    return str(package_dir)


def _write_catalog_mcp_package(root: str, item: dict[str, Any]) -> str:
    name = str(item.get("name") or "").strip()
    url = str(item.get("url") or "").strip()
    if not name or not url:
        raise ValueError(f"catalog MCP item '{name or 'unknown'}' is missing a name or url")
    package_dir = Path(root) / _slugify(name)
    mcp_dir = package_dir / "mcp"
    mcp_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = package_dir / "manifest.yaml"
    manifest_path.write_text(
        "\n".join([
            f"id: {_catalog_mcp_extension_id(name)}",
            "version: 2026.3.21",
            f"display_name: {name}",
            "kind: connector-pack",
            "compatibility:",
            '  seraph: ">=2026.3.19"',
            "publisher:",
            "  name: Seraph Catalog",
            "trust: local",
            "contributes:",
            f"  mcp_servers:",
            f"    - mcp/{_slugify(name)}.json",
            "permissions:",
            "  network: true",
            "",
        ]),
        encoding="utf-8",
    )
    payload = {
        "name": name,
        "url": url,
        "description": str(item.get("description") or ""),
        "headers": item.get("headers") if isinstance(item.get("headers"), dict) else None,
        "auth_hint": str(item.get("auth_hint") or ""),
        "enabled": False,
        "transport": str(item.get("transport") or "streamable-http"),
    }
    (mcp_dir / f"{_slugify(name)}.json").write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    return str(package_dir)


def _validation_detail(report: Any) -> str:
    if getattr(report, "load_errors", None):
        return str(report.load_errors[0].message)
    for result in getattr(report, "results", []) or []:
        issues = getattr(result, "issues", []) or []
        if issues:
            return str(issues[0].message)
    return "extension package failed validation"


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
        try:
            with tempfile.TemporaryDirectory(prefix="seraph-catalog-skill-") as temp_dir:
                package_path = _write_catalog_skill_package(temp_dir, catalog_skill, src)
                report = validate_extension_package(package_path)
                if not report.ok:
                    return {
                        "ok": False,
                        "status": "validation_failed",
                        "name": name,
                        "type": "skill",
                        "bundled": True,
                        "detail": _validation_detail(report),
                    }
                install_extension_path(package_path)
        except FileExistsError:
            return {
                "ok": False,
                "status": "already_installed",
                "name": name,
                "type": "skill",
                "bundled": True,
            }
        except ValueError as exc:
            return {
                "ok": False,
                "status": "validation_failed",
                "name": name,
                "type": "skill",
                "bundled": True,
                "detail": str(exc),
            }
        except OSError as exc:
            logger.warning("Failed to install catalog skill '%s'", name, exc_info=True)
            return {
                "ok": False,
                "status": "install_failed",
                "name": name,
                "type": "skill",
                "bundled": True,
                "detail": str(exc),
            }
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
            "extension_id": _catalog_skill_extension_id(name),
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
        try:
            with tempfile.TemporaryDirectory(prefix="seraph-catalog-mcp-") as temp_dir:
                package_path = _write_catalog_mcp_package(temp_dir, catalog_mcp)
                report = validate_extension_package(package_path)
                if not report.ok:
                    return {
                        "ok": False,
                        "status": "validation_failed",
                        "name": name,
                        "type": "mcp_server",
                        "bundled": bool(catalog_mcp.get("bundled", False)),
                        "detail": _validation_detail(report),
                    }
                install_extension_path(package_path)
        except FileExistsError:
            return {
                "ok": False,
                "status": "already_installed",
                "name": name,
                "type": "mcp_server",
                "bundled": bool(catalog_mcp.get("bundled", False)),
            }
        except ValueError as exc:
            return {
                "ok": False,
                "status": "validation_failed",
                "name": name,
                "type": "mcp_server",
                "bundled": bool(catalog_mcp.get("bundled", False)),
                "detail": str(exc),
            }
        except OSError as exc:
            logger.warning("Failed to install catalog MCP '%s'", name, exc_info=True)
            return {
                "ok": False,
                "status": "install_failed",
                "name": name,
                "type": "mcp_server",
                "bundled": bool(catalog_mcp.get("bundled", False)),
                "detail": str(exc),
            }
        return {
            "ok": True,
            "status": "installed",
            "name": name,
            "type": "mcp_server",
            "extension_id": _catalog_mcp_extension_id(name),
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
        payload = {"status": result["status"], "name": name, "type": result["type"]}
        if "extension_id" in result:
            payload["extension_id"] = result["extension_id"]
        return payload
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
    if result["status"] == "validation_failed":
        raise HTTPException(
            status_code=422,
            detail=result.get("detail") or f"Catalog item '{name}' failed extension validation",
        )
    if result["status"] == "install_failed":
        raise HTTPException(
            status_code=502,
            detail=result.get("detail") or f"Catalog item '{name}' could not be installed",
        )
    if result["status"] == "missing_url":
        raise HTTPException(
            status_code=400,
            detail=f"MCP server '{name}' is missing a URL in the catalog",
        )
    raise HTTPException(status_code=404, detail=f"'{name}' not found in catalog")
