"""Extension lifecycle helpers for install/inspect/configure/remove flows."""

from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any

from config.settings import settings
from src.agent.factory import get_base_tools_and_active_skills
from src.extensions.doctor import doctor_snapshot
from src.extensions.layout import iter_extension_manifest_paths
from src.extensions.manifest import ExtensionManifest, ExtensionManifestError, load_extension_manifest
from src.extensions.registry import (
    ExtensionContributionRecord,
    ExtensionLoadErrorRecord,
    ExtensionRecord,
    ExtensionRegistry,
    bundled_manifest_root,
    default_manifest_roots_for_workspace,
)
from src.extensions.scaffold import validate_extension_package
from src.runbooks.manager import runbook_manager
from src.skills.manager import skill_manager
from src.starter_packs.manager import starter_pack_manager
from src.tools.mcp_manager import mcp_manager
from src.workflows.manager import workflow_manager


_STATE_FILE_NAME = "extensions-state.json"


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "extension"


def _workspace_root() -> str:
    return settings.workspace_dir


def _workspace_extensions_root() -> str:
    return os.path.join(_workspace_root(), "extensions")


def _state_path() -> str:
    return os.path.join(_workspace_root(), _STATE_FILE_NAME)


def _ensure_manifest_roots() -> list[str]:
    roots: list[str] = []
    for candidate in (
        getattr(skill_manager, "_manifest_roots", []),
        getattr(workflow_manager, "_manifest_roots", []),
        getattr(runbook_manager, "_manifest_roots", []),
        getattr(starter_pack_manager, "_manifest_roots", []),
        default_manifest_roots_for_workspace(_workspace_root()),
    ):
        for root in candidate:
            if root and root not in roots:
                roots.append(root)
    workspace_root = _workspace_extensions_root()
    bundled_root = bundled_manifest_root()
    if workspace_root not in roots:
        roots.insert(0, workspace_root)
    if bundled_root not in roots:
        roots.append(bundled_root)
    return roots


def _refresh_runtime() -> None:
    manifest_roots = _ensure_manifest_roots()
    skills_dir = getattr(skill_manager, "_skills_dir", "") or os.path.join(_workspace_root(), "skills")
    workflows_dir = getattr(workflow_manager, "_workflows_dir", "") or os.path.join(_workspace_root(), "workflows")
    runbooks_dir = getattr(runbook_manager, "_runbooks_dir", "") or os.path.join(_workspace_root(), "runbooks")
    starter_packs_path = getattr(starter_pack_manager, "_legacy_path", "") or os.path.join(_workspace_root(), "starter-packs.json")
    os.makedirs(skills_dir, exist_ok=True)
    os.makedirs(workflows_dir, exist_ok=True)
    os.makedirs(runbooks_dir, exist_ok=True)
    os.makedirs(_workspace_extensions_root(), exist_ok=True)
    skill_manager.init(skills_dir, manifest_roots=manifest_roots)
    workflow_manager.init(workflows_dir, manifest_roots=manifest_roots)
    runbook_manager.init(runbooks_dir, manifest_roots=manifest_roots)
    starter_pack_manager.init(starter_packs_path, manifest_roots=manifest_roots)


def _registry() -> ExtensionRegistry:
    return ExtensionRegistry(
        manifest_roots=_ensure_manifest_roots(),
        skill_dirs=[getattr(skill_manager, "_skills_dir", "")] if getattr(skill_manager, "_skills_dir", "") else [],
        workflow_dirs=[getattr(workflow_manager, "_workflows_dir", "")] if getattr(workflow_manager, "_workflows_dir", "") else [],
        mcp_runtime=mcp_manager,
    )


def _state_payload() -> dict[str, Any]:
    path = _state_path()
    if not os.path.isfile(path):
        return {"extensions": {}}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"extensions": {}}
    extensions = payload.get("extensions")
    if not isinstance(extensions, dict):
        return {"extensions": {}}
    return {"extensions": extensions}


def _save_state(payload: dict[str, Any]) -> None:
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _serialize_load_error(error: ExtensionLoadErrorRecord) -> dict[str, Any]:
    return {
        "source": error.source,
        "message": error.message,
        "phase": error.phase,
        "details": list(error.details),
    }


def _location_for_extension(extension: ExtensionRecord) -> str:
    root_path = Path(extension.root_path).resolve() if extension.root_path else None
    workspace_root = Path(_workspace_extensions_root()).resolve()
    bundled_root = Path(bundled_manifest_root()).resolve()
    if root_path is not None and (root_path == workspace_root or workspace_root in root_path.parents):
        return "workspace"
    if root_path is not None and (root_path == bundled_root or bundled_root in root_path.parents):
        return "bundled"
    return extension.source


def _load_manifest_from_path(path: str) -> tuple[Path, ExtensionManifest]:
    candidate = Path(path).expanduser().resolve()
    if not candidate.exists():
        raise ValueError(f"extension path does not exist: {path}")
    manifest_paths = iter_extension_manifest_paths([str(candidate)])
    if not manifest_paths:
        raise ValueError(f"no extension manifest found under {path}")
    if len(manifest_paths) > 1:
        raise ValueError(f"expected one extension package at {path}, found {len(manifest_paths)} manifests")
    manifest_path = manifest_paths[0]
    try:
        manifest = load_extension_manifest(manifest_path)
    except ExtensionManifestError as exc:
        raise ValueError(exc.message) from exc
    return manifest_path.parent, manifest


def _contribution_indexes() -> dict[str, dict[str, dict[str, Any]]]:
    base_tools, active_skill_names, _ = get_base_tools_and_active_skills()
    available_tool_names = [tool.name for tool in base_tools]
    skills = {
        os.path.abspath(str(item.get("file_path") or "")): item
        for item in skill_manager.list_skills()
        if isinstance(item.get("file_path"), str)
    }
    workflows = {
        os.path.abspath(str(item.get("file_path") or "")): item
        for item in workflow_manager.list_workflows(
            available_tool_names=available_tool_names,
            active_skill_names=active_skill_names,
        )
        if isinstance(item.get("file_path"), str)
    }
    runbooks = {
        os.path.abspath(str(item.get("file_path") or "")): item
        for item in runbook_manager.list_runbooks()
        if isinstance(item.get("file_path"), str)
    }
    starter_packs = {
        os.path.abspath(str(item.get("file_path") or "")): item
        for item in starter_pack_manager.list_packs()
        if isinstance(item.get("file_path"), str)
    }
    return {
        "skills": skills,
        "workflows": workflows,
        "runbooks": runbooks,
        "starter_packs": starter_packs,
    }


def _contribution_payload(
    contribution: ExtensionContributionRecord,
    *,
    indexes: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    resolved_path = contribution.metadata.get("resolved_path")
    normalized_path = os.path.abspath(str(resolved_path)) if isinstance(resolved_path, str) and resolved_path else None
    item = (
        indexes.get(contribution.contribution_type, {}).get(normalized_path or "")
        if normalized_path is not None
        else None
    )
    payload: dict[str, Any] = {
        "type": contribution.contribution_type,
        "reference": contribution.reference,
        "resolved_path": normalized_path,
        "loaded": item is not None,
    }
    if isinstance(item, dict):
        payload["name"] = item.get("name") or item.get("id") or item.get("label")
        if "enabled" in item:
            payload["enabled"] = bool(item.get("enabled"))
        if "availability" in item:
            payload["availability"] = item.get("availability")
        if "source" in item:
            payload["source"] = item.get("source")
        if "extension_id" in item:
            payload["extension_id"] = item.get("extension_id")
    return payload


def _toggle_targets(extension: ExtensionRecord) -> list[dict[str, str]]:
    indexes = _contribution_indexes()
    targets: list[dict[str, str]] = []
    for contribution in extension.contributions:
        payload = _contribution_payload(contribution, indexes=indexes)
        target_name = payload.get("name")
        if not isinstance(target_name, str) or not target_name:
            continue
        if contribution.contribution_type == "skills":
            targets.append({"type": "skill", "name": target_name})
        elif contribution.contribution_type == "workflows":
            targets.append({"type": "workflow", "name": target_name})
        elif contribution.contribution_type == "mcp_servers":
            targets.append({"type": "mcp_server", "name": target_name})
    return targets


def _toggleable_contribution_types(extension: ExtensionRecord) -> list[str]:
    types: list[str] = []
    for contribution in extension.contributions:
        if contribution.contribution_type in {"skills", "workflows", "mcp_servers"} and contribution.contribution_type not in types:
            types.append(contribution.contribution_type)
    return types


def _passive_contribution_types(extension: ExtensionRecord) -> list[str]:
    types: list[str] = []
    for contribution in extension.contributions:
        if contribution.contribution_type not in {"skills", "workflows", "mcp_servers"} and contribution.contribution_type not in types:
            types.append(contribution.contribution_type)
    return types


def _extension_payload(
    extension: ExtensionRecord,
    *,
    load_errors: list[ExtensionLoadErrorRecord],
    doctor_by_id: dict[str, Any],
    state_by_id: dict[str, Any],
) -> dict[str, Any]:
    indexes = _contribution_indexes()
    doctor_result = doctor_by_id.get(extension.id)
    issues = []
    if doctor_result is not None:
        issues = [asdict(issue) for issue in doctor_result.issues]
    extension_load_errors = [
        _serialize_load_error(error)
        for error in load_errors
        if extension.root_path
        and (
            error.source == extension.manifest_path
            or (
                os.path.commonpath([os.path.abspath(error.source), os.path.abspath(extension.root_path)])
                == os.path.abspath(extension.root_path)
            )
        )
    ]
    toggles = _toggle_targets(extension)
    state_entry = state_by_id.get(extension.id, {}) if isinstance(state_by_id.get(extension.id), dict) else {}
    location = _location_for_extension(extension)
    contributions = [
        _contribution_payload(contribution, indexes=indexes)
        for contribution in extension.contributions
    ]
    enabled_values = [
        bool(item["enabled"])
        for item in contributions
        if isinstance(item, dict) and "enabled" in item
    ]
    enabled: bool | None = None
    if enabled_values:
        enabled = all(enabled_values)
    toggleable_types = _toggleable_contribution_types(extension)
    passive_types = _passive_contribution_types(extension)
    return {
        "id": extension.id,
        "display_name": extension.display_name,
        "kind": extension.kind,
        "trust": extension.trust,
        "source": extension.source,
        "location": location,
        "root_path": extension.root_path,
        "manifest_path": extension.manifest_path,
        "doctor_ok": bool(getattr(doctor_result, "ok", True)),
        "issues": issues,
        "load_errors": extension_load_errors,
        "status": "ready" if not issues and not extension_load_errors else "degraded",
        "toggle_targets": toggles,
        "toggleable_contribution_types": toggleable_types,
        "passive_contribution_types": passive_types,
        "enable_supported": bool(toggles),
        "disable_supported": bool(toggles),
        "removable": location == "workspace",
        "enabled_scope": "toggleable_contributions" if toggles else "none",
        "configurable": False,
        "metadata_supported": True,
        "config_scope": "metadata_only",
        "enabled": enabled,
        "config": state_entry.get("config", {}),
        "contributions": contributions,
    }


def list_extensions() -> dict[str, Any]:
    snapshot = _registry().snapshot()
    doctor = doctor_snapshot(snapshot)
    doctor_by_id = {result.extension_id: result for result in doctor.results}
    state_by_id = _state_payload()["extensions"]
    extensions = [
        _extension_payload(
            extension,
            load_errors=snapshot.load_errors,
            doctor_by_id=doctor_by_id,
            state_by_id=state_by_id,
        )
        for extension in snapshot.extensions
    ]
    return {
        "extensions": extensions,
        "load_errors": [_serialize_load_error(error) for error in snapshot.load_errors],
        "summary": {
            "total": len(extensions),
            "ready": sum(1 for extension in extensions if extension["status"] == "ready"),
            "degraded": sum(1 for extension in extensions if extension["status"] == "degraded"),
            "bundled": sum(1 for extension in extensions if extension["location"] == "bundled"),
            "workspace": sum(1 for extension in extensions if extension["location"] == "workspace"),
        },
    }


def get_extension(extension_id: str) -> dict[str, Any]:
    payload = list_extensions()
    extension = next((item for item in payload["extensions"] if item["id"] == extension_id), None)
    if extension is None:
        raise KeyError(extension_id)
    return extension


def validate_extension_path(path: str) -> dict[str, Any]:
    package_root, manifest = _load_manifest_from_path(path)
    report = validate_extension_package(package_root)
    return {
        "path": str(package_root),
        "manifest_path": str(package_root / "manifest.yaml"),
        "extension_id": manifest.id,
        "display_name": manifest.display_name,
        "ok": report.ok,
        "load_errors": [_serialize_load_error(error) for error in report.load_errors],
        "results": [
            {
                "extension_id": result.extension_id,
                "ok": result.ok,
                "issues": [asdict(issue) for issue in result.issues],
            }
            for result in report.results
        ],
    }


def install_extension_path(path: str) -> dict[str, Any]:
    package_root, manifest = _load_manifest_from_path(path)
    report = validate_extension_package(package_root)
    if not report.ok:
        raise ValueError("extension package failed validation")

    existing = _registry().snapshot().get_extension(manifest.id)
    if existing is not None:
        if _location_for_extension(existing) == "workspace":
            raise FileExistsError(f"extension '{manifest.id}' is already installed")
        raise ValueError(f"extension id '{manifest.id}' conflicts with existing {existing.source} package")

    target_root = Path(_workspace_extensions_root()) / _slugify(manifest.id)
    if target_root.exists():
        raise FileExistsError(f"extension install target already exists: {target_root}")
    target_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(package_root, target_root)
    _refresh_runtime()
    return get_extension(manifest.id)


def _set_enabled(extension_id: str, enabled: bool) -> dict[str, Any]:
    snapshot = _registry().snapshot()
    extension = snapshot.get_extension(extension_id)
    if extension is None:
        raise KeyError(extension_id)
    changed: list[dict[str, Any]] = []
    for target in _toggle_targets(extension):
        target_name = target["name"]
        ok = False
        if target["type"] == "skill":
            ok = skill_manager.enable(target_name) if enabled else skill_manager.disable(target_name)
        elif target["type"] == "workflow":
            ok = workflow_manager.enable(target_name) if enabled else workflow_manager.disable(target_name)
        elif target["type"] == "mcp_server":
            ok = mcp_manager.update_server(target_name, enabled=enabled)
        changed.append({
            "type": target["type"],
            "name": target_name,
            "enabled": enabled,
            "ok": ok,
        })
    return {
        "extension": get_extension(extension_id),
        "changed": changed,
    }


def enable_extension(extension_id: str) -> dict[str, Any]:
    return _set_enabled(extension_id, True)


def disable_extension(extension_id: str) -> dict[str, Any]:
    return _set_enabled(extension_id, False)


def configure_extension(extension_id: str, config: dict[str, Any]) -> dict[str, Any]:
    snapshot = _registry().snapshot()
    extension = snapshot.get_extension(extension_id)
    if extension is None:
        raise KeyError(extension_id)
    payload = _state_payload()
    extensions = payload.setdefault("extensions", {})
    entry = extensions.setdefault(extension_id, {})
    entry["config"] = config
    _save_state(payload)
    return get_extension(extension_id)


def remove_extension(extension_id: str) -> None:
    snapshot = _registry().snapshot()
    extension = snapshot.get_extension(extension_id)
    if extension is None:
        raise KeyError(extension_id)
    if _location_for_extension(extension) != "workspace" or not extension.root_path:
        raise ValueError(f"extension '{extension_id}' is not removable")
    skill_config_changed = False
    workflow_config_changed = False
    for target in _toggle_targets(extension):
        if target["type"] == "skill" and target["name"] in skill_manager._disabled:
            skill_manager._disabled.discard(target["name"])
            skill_config_changed = True
        if target["type"] == "workflow" and target["name"] in workflow_manager._disabled:
            workflow_manager._disabled.discard(target["name"])
            workflow_config_changed = True
    if skill_config_changed:
        skill_manager._save_config()
    if workflow_config_changed:
        workflow_manager._save_config()
    shutil.rmtree(extension.root_path)
    payload = _state_payload()
    payload.get("extensions", {}).pop(extension_id, None)
    _save_state(payload)
    _refresh_runtime()
