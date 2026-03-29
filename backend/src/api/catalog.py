"""Discover catalog API — browse and install skills, MCP servers, and packages."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
import tempfile
from typing import Any

from fastapi import APIRouter, HTTPException
from packaging.version import InvalidVersion, Version
import yaml

from config.settings import settings
from src.extensions.doctor import doctor_snapshot
from src.extensions.lifecycle import install_extension_path, update_extension_path, validate_extension_path
from src.extensions.permissions import evaluate_tool_permissions
from src.extensions.registry import (
    ExtensionRecord,
    ExtensionRegistry,
    bundled_manifest_root,
    default_manifest_roots_for_workspace,
)
from src.extensions.scaffold import validate_extension_package
from src.skills.loader import parse_skill_content, scan_skill_paths
from src.skills.manager import skill_manager
from src.tools.mcp_manager import mcp_manager

logger = logging.getLogger(__name__)

router = APIRouter()

_DEFAULTS_DIR = os.path.join(os.path.dirname(__file__), "../defaults")
_CATALOG_PATH = os.path.join(_DEFAULTS_DIR, "skill-catalog.json")
_CATALOG_EXTENSION_ROOT = os.path.join(_DEFAULTS_DIR, "catalog-extensions")


def load_catalog_items() -> dict[str, list[dict[str, Any]]]:
    """Load catalog JSON and normalize top-level collections."""
    path = os.path.normpath(_CATALOG_PATH)
    if not os.path.isfile(path):
        payload: dict[str, Any] = {}
    else:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    return {
        "skills": payload.get("skills", []) if isinstance(payload.get("skills"), list) else [],
        "mcp_servers": payload.get("mcp_servers", []) if isinstance(payload.get("mcp_servers"), list) else [],
        "extension_packages": _load_catalog_extension_packages(),
    }


def _load_catalog() -> dict[str, list[dict[str, Any]]]:
    return load_catalog_items()


def _catalog_extension_registry() -> ExtensionRegistry:
    return ExtensionRegistry(
        manifest_roots=[_CATALOG_EXTENSION_ROOT],
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    )


def _runtime_extension_registry() -> ExtensionRegistry:
    return ExtensionRegistry(
        manifest_roots=default_manifest_roots_for_workspace(settings.workspace_dir),
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    )


def _catalog_extension_records() -> list[ExtensionRecord]:
    if not os.path.isdir(_CATALOG_EXTENSION_ROOT):
        return []
    return _catalog_extension_registry().snapshot().extensions


def _contribution_types_for_extension(extension: ExtensionRecord) -> list[str]:
    if extension.manifest is None:
        return sorted({item.contribution_type for item in extension.contributions})
    return sorted(extension.manifest.contributed_types())


def _safe_version(value: str | None) -> Version | None:
    if not value:
        return None
    try:
        return Version(value)
    except InvalidVersion:
        return None


def _extension_catalog_entry(
    extension: ExtensionRecord,
    *,
    doctor_result: Any | None = None,
    load_errors: list[Any] | None = None,
) -> dict[str, Any]:
    installed = _runtime_extension_registry().snapshot().get_extension(extension.id)
    installed_version = (
        installed.manifest.version
        if installed is not None and installed.manifest is not None
        else None
    )
    catalog_version = extension.manifest.version if extension.manifest is not None else None
    update_available = False
    if installed_version and catalog_version:
        installed_parsed = _safe_version(installed_version)
        catalog_parsed = _safe_version(catalog_version)
        update_available = (
            installed_parsed is not None
            and catalog_parsed is not None
            and catalog_parsed > installed_parsed
        )

    contribution_types = _contribution_types_for_extension(extension)
    issues = [asdict(issue) for issue in getattr(doctor_result, "issues", [])] if doctor_result is not None else []
    related_load_errors = [
        {
            "source": getattr(error, "source", ""),
            "phase": getattr(error, "phase", ""),
            "message": getattr(error, "message", ""),
            "details": list(getattr(error, "details", []) or []),
        }
        for error in (load_errors or [])
        if extension.root_path
        and getattr(error, "source", None)
        and (
            str(getattr(error, "source", "")) == str(extension.manifest_path)
            or (
                os.path.commonpath([os.path.abspath(str(getattr(error, "source", ""))), os.path.abspath(extension.root_path)])
                == os.path.abspath(extension.root_path)
            )
        )
    ]
    status = "ready" if not issues and not related_load_errors else "degraded"
    return {
        "name": extension.display_name,
        "catalog_id": extension.id,
        "type": "extension_pack",
        "description": (
            extension.manifest.summary
            if extension.manifest is not None and extension.manifest.summary
            else extension.manifest.description
            if extension.manifest is not None and extension.manifest.description
            else ""
        ),
        "category": extension.kind,
        "requires_tools": [],
        "installed": installed is not None,
        "bundled": True,
        "extension_id": extension.id,
        "version": catalog_version,
        "installed_version": installed_version,
        "update_available": update_available,
        "publisher": (
            extension.manifest.publisher.name
            if extension.manifest is not None
            else ""
        ),
        "trust": extension.trust,
        "contribution_types": contribution_types,
        "kind": extension.kind,
        "status": status,
        "doctor_ok": status == "ready",
        "issues": issues,
        "load_errors": related_load_errors,
    }


def _load_catalog_extension_packages() -> list[dict[str, Any]]:
    registry = _catalog_extension_registry()
    snapshot = registry.snapshot()
    doctor = doctor_snapshot(snapshot)
    doctor_by_id = {result.extension_id: result for result in doctor.results}
    return [
        _extension_catalog_entry(
            extension,
            doctor_result=doctor_by_id.get(extension.id),
            load_errors=snapshot.load_errors,
        )
        for extension in snapshot.extensions
    ]


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


def catalog_extension_by_id() -> dict[str, dict[str, Any]]:
    return {
        str(item["catalog_id"]): item
        for item in _load_catalog().get("extension_packages", [])
        if isinstance(item, dict) and isinstance(item.get("catalog_id"), str)
    }


def _find_catalog_extension(identifier: str) -> dict[str, Any] | None:
    candidate = identifier.strip()
    if not candidate:
        return None
    by_id = catalog_extension_by_id()
    if candidate in by_id:
        return by_id[candidate]
    lowered = candidate.casefold()
    for item in by_id.values():
        display_name = str(item.get("name") or "").strip()
        if display_name and display_name.casefold() == lowered:
            return item
    return None


def _slugify(value: str) -> str:
    return "".join(char.lower() if char.isalnum() else "-" for char in value).strip("-") or "connector"


def _catalog_mcp_extension_id(name: str) -> str:
    return f"seraph.catalog-mcp-{_slugify(name)}"


def _catalog_skill_extension_id(name: str) -> str:
    return f"seraph.catalog-skill-{_slugify(name)}"


def _catalog_extension_source_path(extension_id: str) -> str | None:
    for extension in _catalog_extension_records():
        if extension.id == extension_id and extension.root_path:
            return extension.root_path
    return None


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


def _stable_catalog_install_preview(preview: dict[str, Any], *, reference: str) -> dict[str, Any]:
    stable_preview = dict(preview)
    stable_preview["path"] = reference
    stable_preview["root_path"] = reference
    return stable_preview


def _catalog_install_approval_preview(name: str) -> tuple[str, dict[str, Any]] | None:
    catalog_extension = _find_catalog_extension(name)
    if catalog_extension is not None:
        lifecycle_action = _catalog_extension_lifecycle_action(catalog_extension)
        if lifecycle_action is None:
            return None
        action, package_path = lifecycle_action
        preview = validate_extension_path(package_path)
        if not preview.get("ok"):
            return None
        return action, preview

    catalog_skill = catalog_skill_by_name().get(name)
    if catalog_skill is not None:
        if _skill_loaded(name) or _skill_installed(name) or not bool(catalog_skill.get("bundled", False)):
            return None
        source = _bundled_skill_source_by_name(name)
        if not source or not os.path.isfile(source):
            return None
        with tempfile.TemporaryDirectory(prefix="seraph-catalog-skill-preview-") as temp_dir:
            try:
                package_path = _write_catalog_skill_package(temp_dir, catalog_skill, source)
            except ValueError:
                return None
            preview = validate_extension_path(package_path)
        if not preview.get("ok"):
            return None
        return "install", _stable_catalog_install_preview(
            preview,
            reference=f"catalog://skill/{name}",
        )

    catalog_mcp = catalog_mcp_by_name().get(name)
    if catalog_mcp is not None:
        if _mcp_installed(name):
            return None
        with tempfile.TemporaryDirectory(prefix="seraph-catalog-mcp-preview-") as temp_dir:
            try:
                package_path = _write_catalog_mcp_package(temp_dir, catalog_mcp)
            except ValueError:
                return None
            preview = validate_extension_path(package_path)
        if not preview.get("ok"):
            return None
        return "install", _stable_catalog_install_preview(
            preview,
            reference=f"catalog://mcp_server/{name}",
        )

    return None


async def require_catalog_install_approval(name: str, *, consume: bool = True) -> None:
    lifecycle_preview = _catalog_install_approval_preview(name)
    if lifecycle_preview is None:
        return
    action, preview = lifecycle_preview
    from src.api.extensions import _require_extension_lifecycle_approval

    await _require_extension_lifecycle_approval(action, preview, consume=consume)


def install_catalog_item_by_name(name: str) -> dict[str, Any]:
    """Install a bundled skill or MCP server from the catalog."""
    catalog_extension = _find_catalog_extension(name)
    if catalog_extension is not None:
        extension_id = str(catalog_extension.get("catalog_id") or name)
        package_path = _catalog_extension_source_path(extension_id)
        if not package_path:
            return {
                "ok": False,
                "status": "missing_bundle",
                "name": str(catalog_extension.get("name") or extension_id),
                "type": "extension_pack",
                "bundled": True,
            }
        installed = _runtime_extension_registry().snapshot().get_extension(extension_id)
        try:
            if installed is not None and installed.root_path and installed.source == "manifest":
                catalog_version = _safe_version(str(catalog_extension.get("version") or ""))
                installed_version = _safe_version(
                    installed.manifest.version if installed.manifest is not None else None,
                )
                if (
                    catalog_version is not None
                    and installed_version is not None
                    and catalog_version > installed_version
                ):
                    update_extension_path(package_path)
                    return {
                        "ok": True,
                        "status": "updated",
                        "name": str(catalog_extension.get("name") or extension_id),
                        "type": "extension_pack",
                        "extension_id": extension_id,
                        "bundled": True,
                    }
                return {
                    "ok": False,
                    "status": "already_installed",
                    "name": str(catalog_extension.get("name") or extension_id),
                    "type": "extension_pack",
                    "extension_id": extension_id,
                    "bundled": True,
                }
            install_extension_path(package_path)
            return {
                "ok": True,
                "status": "installed",
                "name": str(catalog_extension.get("name") or extension_id),
                "type": "extension_pack",
                "extension_id": extension_id,
                "bundled": True,
            }
        except FileExistsError:
            return {
                "ok": False,
                "status": "already_installed",
                "name": str(catalog_extension.get("name") or extension_id),
                "type": "extension_pack",
                "extension_id": extension_id,
                "bundled": True,
            }
        except ValueError as exc:
            return {
                "ok": False,
                "status": "validation_failed",
                "name": str(catalog_extension.get("name") or extension_id),
                "type": "extension_pack",
                "extension_id": extension_id,
                "bundled": True,
                "detail": str(exc),
            }
        except OSError as exc:
            logger.warning("Failed to install catalog extension '%s'", name, exc_info=True)
            return {
                "ok": False,
                "status": "install_failed",
                "name": str(catalog_extension.get("name") or extension_id),
                "type": "extension_pack",
                "extension_id": extension_id,
                "bundled": True,
                "detail": str(exc),
            }

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


def _catalog_extension_lifecycle_action(catalog_extension: dict[str, Any]) -> tuple[str, str] | None:
    extension_id = str(catalog_extension.get("catalog_id") or catalog_extension.get("name") or "")
    if not extension_id:
        return None
    package_path = _catalog_extension_source_path(extension_id)
    if not package_path:
        return None
    installed = _runtime_extension_registry().snapshot().get_extension(extension_id)
    if installed is not None and installed.root_path and installed.source == "manifest":
        catalog_version = _safe_version(str(catalog_extension.get("version") or ""))
        installed_version = _safe_version(
            installed.manifest.version if installed.manifest is not None else None,
        )
        if (
            catalog_version is not None
            and installed_version is not None
            and catalog_version > installed_version
        ):
            return "update", package_path
        return None
    return "install", package_path


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

    for extension in catalog.get("extension_packages", []):
        items.append(dict(extension))

    return {"items": items}


@router.post("/catalog/install/{name}", status_code=201)
async def install_item(name: str):
    """Install a skill or MCP server from the catalog."""
    await require_catalog_install_approval(name)
    result = install_catalog_item_by_name(name)
    if result["ok"]:
        payload = {"status": result["status"], "name": name, "type": result["type"]}
        if "extension_id" in result:
            payload["extension_id"] = result["extension_id"]
        return payload
    if result["status"] == "already_installed":
        if result["type"] == "skill":
            detail = f"Skill '{name}' is already installed"
        elif result["type"] == "mcp_server":
            detail = f"MCP server '{name}' is already installed"
        else:
            detail = f"Extension package '{name}' is already installed"
        raise HTTPException(status_code=409, detail=detail)
    if result["status"] == "not_bundled":
        raise HTTPException(
            status_code=400,
            detail=f"Skill '{name}' is not bundled and cannot be auto-installed",
        )
    if result["status"] == "missing_bundle":
        missing_label = (
            "skill file"
            if result["type"] == "skill"
            else "extension package"
            if result["type"] == "extension_pack"
            else "connector package"
        )
        raise HTTPException(status_code=404, detail=f"Bundled {missing_label} for '{name}' not found")
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
