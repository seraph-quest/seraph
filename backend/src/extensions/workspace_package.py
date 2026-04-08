"""Helpers for the managed editable workspace capability package."""

from __future__ import annotations

from datetime import date
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
import tomllib
from typing import Any

import yaml

from config.settings import settings
from src.extensions.layout import expected_layout_prefixes, resolve_package_reference
from src.extensions.manifest import load_extension_manifest, parse_extension_manifest
from src.extensions.permissions import evaluate_tool_permissions
from src.runbooks.loader import parse_runbook_content
from src.skills.loader import parse_skill_content
from src.starter_packs.loader import parse_starter_pack_payload
from src.workflows.loader import parse_workflow_content
from src.extensions.capability_contributions import parse_prompt_pack_definition

WORKSPACE_CAPABILITY_PACKAGE_ID = "seraph.workspace-capabilities"
WORKSPACE_CAPABILITY_PACKAGE_DIRNAME = "workspace-capabilities"
WORKSPACE_CAPABILITY_DISPLAY_NAME = "Workspace capabilities"


def _current_seraph_version() -> str:
    try:
        return package_version("backend")
    except PackageNotFoundError:
        pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
        try:
            with pyproject_path.open("rb") as handle:
                payload = tomllib.load(handle)
            return str(payload.get("project", {}).get("version") or "0")
        except OSError:
            return "0"


def _today_version() -> str:
    today = date.today()
    return f"{today.year}.{today.month}.{today.day}"


def workspace_capability_package_root(workspace_dir: str | None = None) -> Path:
    root = Path(workspace_dir or settings.workspace_dir)
    return root / "extensions" / WORKSPACE_CAPABILITY_PACKAGE_DIRNAME


def workspace_capability_manifest_path(workspace_dir: str | None = None) -> Path:
    return workspace_capability_package_root(workspace_dir) / "manifest.yaml"


def _default_manifest_payload() -> dict[str, Any]:
    current_version = _current_seraph_version()
    return {
        "id": WORKSPACE_CAPABILITY_PACKAGE_ID,
        "version": _today_version(),
        "display_name": WORKSPACE_CAPABILITY_DISPLAY_NAME,
        "kind": "capability-pack",
        "compatibility": {"seraph": f">={current_version}"},
        "publisher": {"name": "Workspace"},
        "trust": "local",
        "contributes": {},
        "permissions": {
            "tools": [],
            "execution_boundaries": [],
            "network": False,
            "secrets": [],
            "env": [],
        },
    }


def _load_or_create_manifest_payload(package_root: Path) -> dict[str, Any]:
    manifest_path = package_root / "manifest.yaml"
    if not manifest_path.exists():
        payload = _default_manifest_payload()
        package_root.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        return payload

    payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"managed workspace manifest at {manifest_path} is invalid")
    payload.setdefault("contributes", {})
    payload.setdefault("permissions", {})
    return payload


def _write_manifest_payload(package_root: Path, payload: dict[str, Any]) -> None:
    manifest_path = package_root / "manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    parse_extension_manifest(manifest_path.read_text(encoding="utf-8"), source=str(manifest_path))


def _recompute_permissions(package_root: Path, payload: dict[str, Any]) -> None:
    manifest = load_extension_manifest(package_root / "manifest.yaml")
    tool_names: list[str] = []
    execution_boundaries: list[str] = []
    requires_network = False

    for skill_reference in manifest.contributes.skills:
        resolved_path = resolve_package_reference(package_root, skill_reference)
        try:
            content = resolved_path.read_text(encoding="utf-8")
        except OSError:
            continue
        errors: list[dict[str, str]] = []
        skill = parse_skill_content(content, path=str(resolved_path), errors=errors)
        if skill is None:
            continue
        permission_profile = evaluate_tool_permissions(None, tool_names=list(skill.requires_tools))
        for tool_name in permission_profile["required_tools"]:
            if tool_name not in tool_names:
                tool_names.append(tool_name)
        for boundary in permission_profile["required_execution_boundaries"]:
            if boundary not in execution_boundaries:
                execution_boundaries.append(boundary)
        requires_network = requires_network or bool(permission_profile["requires_network"])

    for workflow_reference in manifest.contributes.workflows:
        resolved_path = resolve_package_reference(package_root, workflow_reference)
        try:
            content = resolved_path.read_text(encoding="utf-8")
        except OSError:
            continue
        errors = []
        workflow = parse_workflow_content(
            content,
            path=str(resolved_path),
            errors=errors,
        )
        if workflow is None:
            continue
        permission_profile = evaluate_tool_permissions(None, tool_names=list(workflow.step_tools))
        for tool_name in permission_profile["required_tools"]:
            if tool_name not in tool_names:
                tool_names.append(tool_name)
        for boundary in permission_profile["required_execution_boundaries"]:
            if boundary not in execution_boundaries:
                execution_boundaries.append(boundary)
        requires_network = requires_network or bool(permission_profile["requires_network"])

    permissions = payload.setdefault("permissions", {})
    permissions["tools"] = tool_names
    permissions["execution_boundaries"] = execution_boundaries
    permissions["network"] = requires_network
    permissions.setdefault("secrets", [])
    permissions.setdefault("env", [])


def save_workspace_contribution(
    contribution_type: str,
    *,
    file_name: str,
    content: str,
    workspace_dir: str | None = None,
) -> Path:
    if contribution_type not in {"skills", "workflows", "runbooks", "starter_packs", "prompt_packs"}:
        raise ValueError(f"unsupported managed workspace contribution type: {contribution_type}")

    package_root = workspace_capability_package_root(workspace_dir)
    payload = _load_or_create_manifest_payload(package_root)
    relative_reference = f"{expected_layout_prefixes(contribution_type)[0]}{file_name}"
    contribution_bucket = payload.setdefault("contributes", {}).setdefault(contribution_type, [])
    if relative_reference not in contribution_bucket:
        contribution_bucket.append(relative_reference)
        contribution_bucket.sort()
    payload["version"] = _today_version()

    target_path = package_root / relative_reference
    _validate_workspace_contribution(contribution_type, target_path, content)

    manifest_path = package_root / "manifest.yaml"
    original_manifest = manifest_path.read_text(encoding="utf-8") if manifest_path.exists() else None
    target_existed = target_path.exists()
    original_target = target_path.read_text(encoding="utf-8") if target_existed else None

    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding="utf-8")

        _write_manifest_payload(package_root, payload)
        payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"managed workspace manifest at {manifest_path} is invalid")
        _recompute_permissions(package_root, payload)
        _write_manifest_payload(package_root, payload)
        return target_path
    except Exception:
        if original_manifest is None:
            manifest_path.unlink(missing_ok=True)
        else:
            manifest_path.write_text(original_manifest, encoding="utf-8")
        if target_existed:
            target_path.write_text(original_target or "", encoding="utf-8")
        else:
            target_path.unlink(missing_ok=True)
        raise


def _validate_workspace_contribution(contribution_type: str, target_path: Path, content: str) -> None:
    if contribution_type == "skills":
        errors: list[dict[str, str]] = []
        if parse_skill_content(content, path=str(target_path), errors=errors) is None:
            raise ValueError(f"skill draft is invalid: {errors[0]['message'] if errors else target_path}")
        return
    if contribution_type == "workflows":
        errors = []
        if parse_workflow_content(content, path=str(target_path), errors=errors) is None:
            raise ValueError(f"workflow draft is invalid: {errors[0]['message'] if errors else target_path}")
        return
    if contribution_type == "runbooks":
        errors = []
        if parse_runbook_content(content, path=str(target_path), errors=errors) is None:
            raise ValueError(f"runbook draft is invalid: {errors[0]['message'] if errors else target_path}")
        return
    if contribution_type == "starter_packs":
        try:
            payload = yaml.safe_load(content)
        except yaml.YAMLError:
            payload = None
        if payload is None:
            import json
            payload = json.loads(content)
        errors = []
        if parse_starter_pack_payload(payload, path=str(target_path), errors=errors) is None:
            raise ValueError(f"starter pack draft is invalid: {errors[0]['message'] if errors else target_path}")
        return
    if contribution_type == "prompt_packs":
        parse_prompt_pack_definition(content, source=str(target_path))
