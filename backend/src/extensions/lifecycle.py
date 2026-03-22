"""Extension lifecycle helpers for install/inspect/configure/remove flows."""

from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any
from urllib.parse import urlparse

from config.settings import settings
from src.agent.factory import get_base_tools_and_active_skills
from src.extensions.connectors import ConnectorDefinitionError, MCPServerDefinition, load_mcp_server_definition
from src.extensions.doctor import doctor_snapshot
from src.extensions.layout import iter_extension_manifest_paths, resolve_package_reference
from src.extensions.manifest import (
    ExtensionManifest,
    ExtensionManifestError,
    load_extension_manifest,
    parse_extension_manifest,
)
from src.extensions.observers import select_active_observer_definitions
from src.extensions.registry import (
    ExtensionContributionRecord,
    ExtensionLoadErrorRecord,
    ExtensionRecord,
    ExtensionRegistry,
    bundled_manifest_root,
    default_manifest_roots_for_workspace,
)
from src.extensions.scaffold import validate_extension_package
from src.runbooks.loader import scan_runbook_paths
from src.skills.loader import parse_skill_content, scan_skill_paths
from src.runbooks.manager import runbook_manager
from src.skills.manager import skill_manager
from src.starter_packs.manager import starter_pack_manager
from src.starter_packs.loader import scan_starter_pack_paths
from src.tools.mcp_manager import mcp_manager
from src.workflows.loader import parse_workflow_content, scan_workflow_paths
from src.workflows.manager import workflow_manager


_STATE_FILE_NAME = "extensions-state.json"
_EDITABLE_STUDIO_TYPES = {"skills", "workflows"}
_STUDIO_TYPE_ORDER = {
    "manifest": 0,
    "skills": 1,
    "workflows": 2,
    "runbooks": 3,
    "starter_packs": 4,
    "mcp_servers": 5,
    "managed_connectors": 6,
    "observer_definitions": 7,
    "observer_connectors": 8,
    "channel_adapters": 9,
    "workspace_adapters": 10,
}


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


def _mcp_runtime_index() -> dict[str, dict[str, Any]]:
    return {
        str(item["name"]): item
        for item in mcp_manager.get_config()
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def _mcp_definition_for_contribution(contribution: ExtensionContributionRecord) -> MCPServerDefinition | None:
    if contribution.contribution_type != "mcp_servers":
        return None
    resolved_path = contribution.metadata.get("resolved_path")
    if isinstance(resolved_path, str) and resolved_path:
        try:
            return load_mcp_server_definition(Path(resolved_path))
        except ConnectorDefinitionError:
            return None
    return None


def _mcp_definitions_for_extension(extension: ExtensionRecord) -> dict[str, MCPServerDefinition]:
    definitions: dict[str, MCPServerDefinition] = {}
    for contribution in extension.contributions:
        definition = _mcp_definition_for_contribution(contribution)
        if definition is not None:
            definitions[definition.name] = definition
    return definitions


def _managed_connector_config(
    state_entry: dict[str, Any] | None,
    connector_name: str,
) -> dict[str, Any]:
    if not isinstance(state_entry, dict) or not connector_name:
        return {}

    config_payload = state_entry.get("config")
    if not isinstance(config_payload, dict):
        config_payload = {}
    managed_config = config_payload.get("managed_connectors")
    if not isinstance(managed_config, dict):
        managed_config = {}
    config_entry = managed_config.get(connector_name)
    if not isinstance(config_entry, dict):
        return {}
    return config_entry


def _managed_connector_config_errors(
    metadata: dict[str, Any],
    config_entry: dict[str, Any],
) -> list[str]:
    config_fields = metadata.get("config_fields")
    if not isinstance(config_fields, list):
        return []
    errors: list[str] = []
    allowed_keys: set[str] = set()
    for field in config_fields:
        if not isinstance(field, dict):
            continue
        key = field.get("key")
        if not isinstance(key, str) or not key:
            continue
        allowed_keys.add(key)
        required = bool(field.get("required", True))
        value = config_entry.get(key)
        if required and (
            key not in config_entry
            or value is None
            or (isinstance(value, str) and not value.strip())
        ):
            errors.append(f"missing required config field '{key}'")
            continue
        if key not in config_entry or value is None:
            continue
        input_kind = field.get("input")
        if input_kind in {"text", "password", "select", "url"} and not isinstance(value, str):
            errors.append(f"config field '{key}' must be a string")
            continue
        if input_kind in {"text", "password", "select"} and isinstance(value, str) and not value.strip():
            errors.append(f"config field '{key}' must not be empty")
            continue
        if input_kind == "url" and isinstance(value, str):
            parsed = urlparse(value.strip())
            if not value.strip() or parsed.scheme not in {"http", "https"} or not parsed.netloc:
                errors.append(f"config field '{key}' must be a valid http or https URL")
    unknown_keys = sorted(key for key in config_entry.keys() if key not in allowed_keys)
    for key in unknown_keys:
        errors.append(f"unknown config field '{key}'")
    return errors


def _install_mcp_servers_for_extension(extension: ExtensionRecord) -> None:
    definitions = _mcp_definitions_for_extension(extension)
    for definition in definitions.values():
        if definition.name in mcp_manager._config:
            raise FileExistsError(
                f"MCP server '{definition.name}' from extension '{extension.id}' already exists"
            )
    added: list[str] = []
    try:
        for definition in definitions.values():
            # Connector packages register safely at install time; operators enable them explicitly later.
            mcp_manager.add_server(
                name=definition.name,
                url=definition.url,
                description=definition.description,
                enabled=False,
                headers=definition.headers,
                auth_hint=definition.auth_hint,
            )
            added.append(definition.name)
    except Exception:
        for name in added:
            mcp_manager.remove_server(name)
        raise


def _remove_mcp_servers_for_extension(extension: ExtensionRecord) -> None:
    for definition in _mcp_definitions_for_extension(extension).values():
        if definition.name in mcp_manager._config:
            mcp_manager.remove_server(definition.name)


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


def _workspace_manifest_path_for_extension_id(extension_id: str) -> Path | None:
    root = Path(_workspace_extensions_root()) / _slugify(extension_id)
    for manifest_name in ("manifest.yaml", "manifest.yml"):
        candidate = root / manifest_name
        if candidate.is_file():
            return candidate
    return None


def _fallback_workspace_extension(extension_id: str) -> ExtensionRecord | None:
    manifest_path = _workspace_manifest_path_for_extension_id(extension_id)
    if manifest_path is None:
        return None
    manifest: ExtensionManifest | None = None
    display_name = extension_id
    kind = "capability-pack"
    trust = "local"
    try:
        manifest = load_extension_manifest(manifest_path)
    except ExtensionManifestError:
        manifest = None
    else:
        display_name = manifest.display_name
        kind = manifest.kind.value
        trust = manifest.trust.value
    return ExtensionRecord(
        id=extension_id,
        display_name=display_name,
        kind=kind,
        trust=trust,
        source="manifest",
        root_path=str(manifest_path.parent),
        manifest_path=str(manifest_path),
        manifest=manifest,
        contributions=[],
        metadata={},
    )


def _extension_or_workspace_fallback(snapshot: Any, extension_id: str) -> ExtensionRecord | None:
    extension = snapshot.get_extension(extension_id)
    if extension is not None:
        return extension
    return _fallback_workspace_extension(extension_id)


def _format_for_reference(reference: str) -> str:
    suffix = Path(reference).suffix.lower()
    if suffix in {".yaml", ".yml"}:
        return "yaml"
    if suffix == ".json":
        return "json"
    if suffix == ".md":
        return "markdown"
    return "text"


def _display_type_for_contribution(contribution_type: str) -> str:
    if contribution_type == "skills":
        return "skill"
    if contribution_type == "workflows":
        return "workflow"
    if contribution_type == "runbooks":
        return "runbook"
    if contribution_type == "starter_packs":
        return "starter_pack"
    if contribution_type == "mcp_servers":
        return "mcp_server"
    return contribution_type.removesuffix("s")


def _studio_file_payload(
    extension: ExtensionRecord,
    *,
    contribution: ExtensionContributionRecord | None,
    indexes: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    if contribution is None:
        manifest_reference = Path(extension.manifest_path or "manifest.yaml").name
        editable = _location_for_extension(extension) == "workspace"
        return {
            "key": f"{extension.id}:manifest",
            "role": "manifest",
            "reference": manifest_reference,
            "resolved_path": extension.manifest_path,
            "label": manifest_reference,
            "display_type": "manifest",
            "format": _format_for_reference(manifest_reference),
            "editable": editable,
            "save_supported": editable,
            "validation_supported": True,
            "loaded": True,
            "name": extension.display_name,
        }

    payload = _contribution_payload(contribution, indexes=indexes)
    editable = (
        _location_for_extension(extension) == "workspace"
        and contribution.contribution_type in _EDITABLE_STUDIO_TYPES
    )
    label = (
        payload.get("name")
        or Path(contribution.reference).stem
        or contribution.reference
    )
    return {
        "key": f"{extension.id}:{contribution.contribution_type}:{contribution.reference}",
        "role": "contribution",
        "reference": contribution.reference,
        "resolved_path": payload.get("resolved_path"),
        "label": str(label),
        "display_type": _display_type_for_contribution(contribution.contribution_type),
        "contribution_type": contribution.contribution_type,
        "format": _format_for_reference(contribution.reference),
        "editable": editable,
        "save_supported": editable,
        "validation_supported": contribution.contribution_type in {"skills", "workflows"},
        "loaded": bool(payload.get("loaded")),
        "name": payload.get("name"),
        "status": payload.get("availability"),
        "source": payload.get("source"),
    }


def _studio_files(
    extension: ExtensionRecord,
    *,
    indexes: dict[str, dict[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    files = [_studio_file_payload(extension, contribution=None, indexes=indexes)]
    for contribution in sorted(
        extension.contributions,
        key=lambda item: (
            _STUDIO_TYPE_ORDER.get(item.contribution_type, 99),
            item.reference,
        ),
    ):
        files.append(_studio_file_payload(extension, contribution=contribution, indexes=indexes))
    return files


def _resolve_extension_reference(
    extension: ExtensionRecord,
    reference: str,
) -> tuple[str, Path, ExtensionContributionRecord | None]:
    normalized_reference = reference.strip()
    if not normalized_reference:
        raise ValueError("reference is required")
    if extension.root_path is None:
        raise ValueError(f"extension '{extension.id}' does not expose a package root")
    if extension.manifest_path is None:
        raise ValueError(f"extension '{extension.id}' does not expose a manifest file")

    manifest_name = Path(extension.manifest_path).name
    if normalized_reference == manifest_name or normalized_reference == "manifest.yaml":
        return "manifest", Path(extension.manifest_path), None

    for contribution in extension.contributions:
        if contribution.reference == normalized_reference:
            return (
                contribution.contribution_type,
                resolve_package_reference(Path(extension.root_path), contribution.reference),
                contribution,
            )
    raise ValueError(f"reference '{reference}' is not part of extension '{extension.id}'")


def _validate_skill_draft(content: str, *, path: str) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    skill = parse_skill_content(content, path=path, errors=errors)
    if skill is None:
        return {
            "valid": False,
            "errors": errors,
            "runtime_ready": False,
            "missing_tools": [],
        }
    base_tools, _, _ = get_base_tools_and_active_skills()
    available_tool_names = {tool.name for tool in base_tools}
    missing_tools = [tool_name for tool_name in skill.requires_tools if tool_name not in available_tool_names]
    return {
        "valid": True,
        "errors": [],
        "runtime_ready": not missing_tools and skill.enabled,
        "missing_tools": missing_tools,
        "skill": {
            "name": skill.name,
            "description": skill.description,
            "requires_tools": skill.requires_tools,
            "user_invocable": skill.user_invocable,
            "enabled": skill.enabled,
            "file_path": skill.file_path,
        },
    }


def _validate_workflow_draft(content: str, *, path: str) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    workflow = parse_workflow_content(content, path=path, errors=errors)
    if workflow is None:
        return {
            "valid": False,
            "errors": errors,
            "runtime_ready": False,
            "missing_tools": [],
            "missing_skills": [],
        }
    base_tools, active_skill_names, mcp_mode = get_base_tools_and_active_skills()
    available_tool_names = [tool.name for tool in base_tools]
    missing_tools = [tool_name for tool_name in workflow.step_tools if tool_name not in set(available_tool_names)]
    missing_skills = [skill_name for skill_name in workflow.requires_skills if skill_name not in set(active_skill_names)]
    execution_boundaries = workflow_manager._infer_execution_boundaries(workflow)
    risk_level = workflow_manager._infer_risk_level(workflow)
    requires_approval = risk_level == "high" or ("external_mcp" in execution_boundaries and mcp_mode == "approval")
    return {
        "valid": True,
        "errors": [],
        "runtime_ready": not missing_tools and not missing_skills and workflow.enabled,
        "missing_tools": missing_tools,
        "missing_skills": missing_skills,
        "requires_approval": requires_approval,
        "workflow": {
            "name": workflow.name,
            "tool_name": workflow.tool_name,
            "description": workflow.description,
            "inputs": workflow.inputs,
            "requires_tools": workflow.requires_tools,
            "requires_skills": workflow.requires_skills,
            "user_invocable": workflow.user_invocable,
            "enabled": workflow.enabled,
            "file_path": workflow.file_path,
            "step_count": len(workflow.steps),
            "execution_boundaries": execution_boundaries,
            "risk_level": risk_level,
            "accepts_secret_refs": workflow_manager._accepts_secret_refs(workflow),
        },
    }


def _validation_for_extension_source(
    extension: ExtensionRecord,
    *,
    content: str,
    contribution_type: str,
    reference: str,
) -> dict[str, Any]:
    if contribution_type == "manifest":
        try:
            manifest = parse_extension_manifest(content, source=f"{extension.id}:{reference}")
        except ExtensionManifestError as exc:
            return {
                "valid": False,
                "errors": exc.errors or [{"loc": [], "message": exc.message, "type": "value_error"}],
            }
        if manifest.id != extension.id:
            return {
                "valid": False,
                "errors": [{
                    "loc": ["id"],
                    "message": f"manifest id must stay '{extension.id}' while editing an installed extension package",
                    "type": "value_error",
                }],
            }
        return {
            "valid": True,
            "manifest": {
                "id": manifest.id,
                "display_name": manifest.display_name,
                "version": manifest.version,
                "kind": manifest.kind.value,
                "trust": manifest.trust.value,
                "contributed_types": sorted(manifest.contributed_types()),
            },
        }
    if contribution_type == "skills":
        return _validate_skill_draft(content, path=reference)
    if contribution_type == "workflows":
        return _validate_workflow_draft(content, path=reference)
    return {"valid": True}


def _validate_extension_candidate_package(
    extension: ExtensionRecord,
    *,
    reference: str,
    resolved_path: Path,
    content: str,
) -> None:
    if extension.root_path is None or extension.manifest_path is None:
        raise ValueError(f"extension '{extension.id}' does not expose a package root")
    root_path = Path(extension.root_path)
    resolved_reference = str(resolved_path.relative_to(root_path))
    with tempfile.TemporaryDirectory(prefix="seraph-extension-package-") as temp_root:
        candidate_root = Path(temp_root) / "package"
        shutil.copytree(extension.root_path, candidate_root)
        candidate_target = candidate_root / Path(resolved_reference)
        candidate_target.parent.mkdir(parents=True, exist_ok=True)
        candidate_target.write_text(content, encoding="utf-8")
        report = validate_extension_package(candidate_root)
        if not report.ok:
            raise ValueError("extension update would make the package invalid")
        if not any(result.extension_id == extension.id for result in report.results):
            raise ValueError("extension update would make the package unloadable")

        candidate_manifest_path = candidate_root / Path(extension.manifest_path).name
        manifest = load_extension_manifest(candidate_manifest_path)
        skill_paths = [
            str(resolve_package_reference(candidate_root, reference_path))
            for reference_path in manifest.contributes.skills
        ]
        workflow_paths = [
            str(resolve_package_reference(candidate_root, reference_path))
            for reference_path in manifest.contributes.workflows
        ]
        runbook_paths = [
            str(resolve_package_reference(candidate_root, reference_path))
            for reference_path in manifest.contributes.runbooks
        ]
        starter_pack_paths = [
            str(resolve_package_reference(candidate_root, reference_path))
            for reference_path in manifest.contributes.starter_packs
        ]

        skills, skill_errors = scan_skill_paths(skill_paths)
        workflows, workflow_errors = scan_workflow_paths(workflow_paths)
        runbooks, runbook_errors = scan_runbook_paths(runbook_paths)
        starter_packs, starter_pack_errors = scan_starter_pack_paths(starter_pack_paths)
        all_errors = [*skill_errors, *workflow_errors, *runbook_errors, *starter_pack_errors]
        if all_errors:
            raise ValueError(all_errors[0]["message"])

        skill_names = {skill.name for skill in skills}
        workflow_names = {workflow.name for workflow in workflows}
        starter_pack_names = {pack.name for pack in starter_packs}
        external_skill_names = {
            str(item.get("name"))
            for item in skill_manager.list_skills()
            if isinstance(item.get("name"), str) and item.get("extension_id") != extension.id
        }
        external_workflow_names = {
            str(item.get("name"))
            for item in workflow_manager.list_workflows()
            if isinstance(item.get("name"), str) and item.get("extension_id") != extension.id
        }
        external_starter_pack_names = {
            str(item.get("name"))
            for item in starter_pack_manager.list_packs()
            if isinstance(item.get("name"), str) and item.get("extension_id") != extension.id
        }
        current_skill_names = {
            str(item.get("name"))
            for item in skill_manager.list_skills()
            if isinstance(item.get("name"), str) and item.get("extension_id") == extension.id
        }
        current_workflow_names = {
            str(item.get("name"))
            for item in workflow_manager.list_workflows()
            if isinstance(item.get("name"), str) and item.get("extension_id") == extension.id
        }
        current_starter_pack_names = {
            str(item.get("name"))
            for item in starter_pack_manager.list_packs()
            if isinstance(item.get("name"), str) and item.get("extension_id") == extension.id
        }
        allowed_skill_names = skill_names | external_skill_names
        allowed_workflow_names = workflow_names | external_workflow_names
        allowed_starter_pack_names = starter_pack_names | external_starter_pack_names
        removed_skill_names = current_skill_names - skill_names
        removed_workflow_names = current_workflow_names - workflow_names
        removed_starter_pack_names = current_starter_pack_names - starter_pack_names

        for workflow in workflows:
            missing_required_skills = [name for name in workflow.requires_skills if name not in allowed_skill_names]
            if missing_required_skills:
                raise ValueError(
                    f"workflow '{workflow.name}' references unknown skills: {', '.join(missing_required_skills)}"
                )

        for runbook in runbooks:
            if runbook.workflow and runbook.workflow not in allowed_workflow_names:
                raise ValueError(
                    f"runbook '{runbook.id}' references unknown workflow '{runbook.workflow}'"
                )
            if runbook.starter_pack and runbook.starter_pack not in allowed_starter_pack_names:
                raise ValueError(
                    f"runbook '{runbook.id}' references unknown starter pack '{runbook.starter_pack}'"
                )

        for starter_pack in starter_packs:
            missing_skills = [name for name in starter_pack.skills if name not in allowed_skill_names]
            if missing_skills:
                raise ValueError(
                    f"starter pack '{starter_pack.name}' references unknown skills: {', '.join(missing_skills)}"
                )
            missing_workflows = [name for name in starter_pack.workflows if name not in allowed_workflow_names]
            if missing_workflows:
                raise ValueError(
                    f"starter pack '{starter_pack.name}' references unknown workflows: {', '.join(missing_workflows)}"
                )

        for workflow in workflow_manager.list_workflows():
            if workflow.get("extension_id") == extension.id:
                continue
            missing_required_skills = [
                name
                for name in workflow.get("requires_skills") or []
                if isinstance(name, str) and name in removed_skill_names
            ]
            if missing_required_skills:
                workflow_name = str(workflow.get("name") or "unknown-workflow")
                raise ValueError(
                    f"workflow '{workflow_name}' depends on skills removed from extension '{extension.id}': "
                    f"{', '.join(missing_required_skills)}"
                )

        for runbook in runbook_manager.list_runbooks():
            if runbook.get("extension_id") == extension.id:
                continue
            if isinstance(runbook.get("workflow"), str) and runbook["workflow"] in removed_workflow_names:
                runbook_id = str(runbook.get("id") or "unknown-runbook")
                raise ValueError(
                    f"runbook '{runbook_id}' depends on workflows removed from extension '{extension.id}': "
                    f"{runbook['workflow']}"
                )
            if isinstance(runbook.get("starter_pack"), str) and runbook["starter_pack"] in removed_starter_pack_names:
                runbook_id = str(runbook.get("id") or "unknown-runbook")
                raise ValueError(
                    f"runbook '{runbook_id}' depends on starter packs removed from extension '{extension.id}': "
                    f"{runbook['starter_pack']}"
                )

        for starter_pack in starter_pack_manager.list_packs():
            if starter_pack.get("extension_id") == extension.id:
                continue
            blocking_skills = [
                name
                for name in starter_pack.get("skills") or []
                if isinstance(name, str) and name in removed_skill_names
            ]
            if blocking_skills:
                starter_pack_name = str(starter_pack.get("name") or "unknown-starter-pack")
                raise ValueError(
                    f"starter pack '{starter_pack_name}' depends on skills removed from extension '{extension.id}': "
                    f"{', '.join(blocking_skills)}"
                )
            blocking_workflows = [
                name
                for name in starter_pack.get("workflows") or []
                if isinstance(name, str) and name in removed_workflow_names
            ]
            if blocking_workflows:
                starter_pack_name = str(starter_pack.get("name") or "unknown-starter-pack")
                raise ValueError(
                    f"starter pack '{starter_pack_name}' depends on workflows removed from extension '{extension.id}': "
                    f"{', '.join(blocking_workflows)}"
                )


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
    mcp_servers = _mcp_runtime_index()
    observer_snapshot = _registry().snapshot()
    observer_definitions = {
        os.path.abspath(item.resolved_path): {
            "name": item.name,
            "source_type": item.source_type,
        }
        for item in select_active_observer_definitions(observer_snapshot.list_contributions("observer_definitions"))
        if item.resolved_path
    }
    return {
        "skills": skills,
        "workflows": workflows,
        "runbooks": runbooks,
        "starter_packs": starter_packs,
        "mcp_servers": mcp_servers,
        "observer_definitions": observer_definitions,
    }


def _contribution_payload(
    contribution: ExtensionContributionRecord,
    *,
    indexes: dict[str, dict[str, dict[str, Any]]],
    state_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_path = contribution.metadata.get("resolved_path")
    normalized_path = os.path.abspath(str(resolved_path)) if isinstance(resolved_path, str) and resolved_path else None
    if contribution.contribution_type == "mcp_servers":
        contribution_name = contribution.metadata.get("name")
        runtime_entry = (
            indexes.get("mcp_servers", {}).get(contribution_name)
            if isinstance(contribution_name, str) and contribution_name
            else None
        )
        payload: dict[str, Any] = {
            "type": contribution.contribution_type,
            "reference": contribution.reference,
            "resolved_path": normalized_path,
            "loaded": runtime_entry is not None,
        }
        if isinstance(contribution_name, str) and contribution_name:
            payload["name"] = contribution_name
        for field_name in ("url", "description", "auth_hint", "transport"):
            field_value = contribution.metadata.get(field_name)
            if isinstance(field_value, str) and field_value:
                payload[field_name] = field_value
        if "default_enabled" in contribution.metadata:
            payload["default_enabled"] = bool(contribution.metadata.get("default_enabled"))
        if isinstance(contribution.metadata.get("headers"), dict):
            payload["has_headers"] = bool(contribution.metadata.get("headers"))
        if isinstance(runtime_entry, dict):
            payload["loaded"] = True
            for field_name in ("name", "enabled", "status", "status_message", "connected", "tool_count", "auth_hint", "description", "url", "has_headers"):
                if field_name in runtime_entry:
                    payload[field_name] = runtime_entry[field_name]
        return payload
    if contribution.contribution_type == "managed_connectors":
        payload = {
            "type": contribution.contribution_type,
            "reference": contribution.reference,
            "resolved_path": normalized_path,
            "loaded": False,
        }
        for field_name in ("name", "provider", "description", "auth_kind"):
            field_value = contribution.metadata.get(field_name)
            if isinstance(field_value, str) and field_value:
                payload[field_name] = field_value
        for field_name in ("capabilities", "setup_steps", "config_fields"):
            field_value = contribution.metadata.get(field_name)
            if isinstance(field_value, list) and field_value:
                payload[field_name] = field_value
        connector_name = payload.get("name") if isinstance(payload.get("name"), str) else ""
        config_entry = _managed_connector_config(state_entry, connector_name)
        config_errors = _managed_connector_config_errors(contribution.metadata, config_entry)
        if config_entry:
            payload["config_keys"] = sorted(config_entry.keys())
        if config_errors:
            payload["config_errors"] = config_errors
        if not config_entry:
            payload["configured"] = False
            payload["status"] = "not_configured"
        elif config_errors:
            payload["configured"] = False
            payload["status"] = "invalid_config"
        else:
            payload["configured"] = True
            payload["status"] = "configured"
        return payload
    if contribution.contribution_type == "observer_definitions":
        active_definition = (
            indexes.get("observer_definitions", {}).get(normalized_path or "")
            if normalized_path is not None
            else None
        )
        default_enabled = bool(contribution.metadata.get("default_enabled", True))
        valid_definition = all(
            isinstance(contribution.metadata.get(field_name), str) and str(contribution.metadata.get(field_name)).strip()
            for field_name in ("name", "source_type")
        )
        payload = {
            "type": contribution.contribution_type,
            "reference": contribution.reference,
            "resolved_path": normalized_path,
            "loaded": active_definition is not None,
        }
        for field_name in ("name", "source_type", "description"):
            field_value = contribution.metadata.get(field_name)
            if isinstance(field_value, str) and field_value:
                payload[field_name] = field_value
        if "default_enabled" in contribution.metadata:
            payload["default_enabled"] = default_enabled
        if "requires_network" in contribution.metadata:
            payload["requires_network"] = bool(contribution.metadata.get("requires_network"))
        if payload["loaded"]:
            payload["status"] = "active"
        elif not valid_definition:
            payload["status"] = "invalid"
        elif not default_enabled:
            payload["status"] = "disabled"
        else:
            payload["status"] = "overridden"
        return payload
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
        _contribution_payload(contribution, indexes=indexes, state_entry=state_entry)
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
    managed_connector_present = any(
        contribution.contribution_type == "managed_connectors"
        for contribution in extension.contributions
    )
    return {
        "id": extension.id,
        "display_name": extension.display_name,
        "version": extension.manifest.version if extension.manifest is not None else None,
        "kind": extension.kind,
        "trust": extension.trust,
        "source": extension.source,
        "location": location,
        "root_path": extension.root_path,
        "manifest_path": extension.manifest_path,
        "summary": extension.manifest.summary if extension.manifest is not None else None,
        "description": extension.manifest.description if extension.manifest is not None else None,
        "compatibility": (
            {"seraph": extension.manifest.compatibility.seraph}
            if extension.manifest is not None
            else None
        ),
        "publisher": (
            {
                "name": extension.manifest.publisher.name,
                "homepage": extension.manifest.publisher.homepage,
                "support": extension.manifest.publisher.support,
            }
            if extension.manifest is not None
            else None
        ),
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
        "configurable": managed_connector_present,
        "metadata_supported": True,
        "config_scope": "metadata_and_managed_connectors" if managed_connector_present else "metadata_only",
        "enabled": enabled,
        "config": state_entry.get("config", {}),
        "contributions": contributions,
        "studio_files": _studio_files(extension, indexes=indexes),
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


def get_extension_source(extension_id: str, reference: str) -> dict[str, Any]:
    snapshot = _registry().snapshot()
    extension = _extension_or_workspace_fallback(snapshot, extension_id)
    if extension is None:
        raise KeyError(extension_id)
    contribution_type, resolved_path, _ = _resolve_extension_reference(extension, reference)
    try:
        content = resolved_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"failed to read extension source: {exc}") from exc
    validation = _validation_for_extension_source(
        extension,
        content=content,
        contribution_type=contribution_type,
        reference=reference,
    )
    indexes = _contribution_indexes()
    studio_file = next(
        (
            item
            for item in _studio_files(extension, indexes=indexes)
            if item["reference"] == reference or (item["role"] == "manifest" and reference == "manifest.yaml")
        ),
        None,
    )
    doctor = doctor_snapshot(snapshot)
    return {
        "extension": _extension_payload(
            extension,
            load_errors=snapshot.load_errors,
            doctor_by_id={result.extension_id: result for result in doctor.results},
            state_by_id=_state_payload()["extensions"],
        ),
        "reference": reference,
        "resolved_path": str(resolved_path),
        "content": content,
        "editable": bool(studio_file["editable"]) if studio_file else False,
        "save_supported": bool(studio_file["save_supported"]) if studio_file else False,
        "validation_supported": bool(studio_file["validation_supported"]) if studio_file else False,
        "format": studio_file["format"] if studio_file else _format_for_reference(reference),
        "validation": validation,
    }


def save_extension_source(extension_id: str, reference: str, content: str) -> dict[str, Any]:
    snapshot = _registry().snapshot()
    extension = _extension_or_workspace_fallback(snapshot, extension_id)
    if extension is None:
        raise KeyError(extension_id)
    if _location_for_extension(extension) != "workspace":
        raise ValueError(f"extension '{extension_id}' is read-only")

    contribution_type, resolved_path, _ = _resolve_extension_reference(extension, reference)
    if contribution_type not in {"manifest", *sorted(_EDITABLE_STUDIO_TYPES)}:
        raise ValueError(f"editing {contribution_type.replace('_', ' ')} entries is not supported yet")

    validation = _validation_for_extension_source(
        extension,
        content=content,
        contribution_type=contribution_type,
        reference=reference,
    )
    if validation.get("valid") is False:
        raise ValueError(f"{contribution_type.removesuffix('s').replace('_', ' ')} draft is invalid")
    _validate_extension_candidate_package(
        extension,
        reference=reference,
        resolved_path=resolved_path,
        content=content,
    )

    try:
        resolved_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"failed to save extension source: {exc}") from exc

    _refresh_runtime()
    return {
        "status": "saved",
        **get_extension_source(extension_id, reference),
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
    try:
        _refresh_runtime()
        installed_extension = _registry().snapshot().get_extension(manifest.id)
        if installed_extension is None:
            raise ValueError(f"extension '{manifest.id}' did not load after install")
        _install_mcp_servers_for_extension(installed_extension)
    except Exception:
        shutil.rmtree(target_root, ignore_errors=True)
        _refresh_runtime()
        raise
    return get_extension(manifest.id)


def _set_enabled(extension_id: str, enabled: bool) -> dict[str, Any]:
    snapshot = _registry().snapshot()
    extension = snapshot.get_extension(extension_id)
    if extension is None:
        raise KeyError(extension_id)
    mcp_definitions = _mcp_definitions_for_extension(extension)
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
            if not ok and enabled:
                definition = mcp_definitions.get(target_name)
                if definition is not None:
                    mcp_manager.add_server(
                        name=definition.name,
                        url=definition.url,
                        description=definition.description,
                        enabled=True,
                        headers=definition.headers,
                        auth_hint=definition.auth_hint,
                    )
                    ok = True
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
    managed_connector_configs = config.get("managed_connectors") if isinstance(config, dict) else None
    if managed_connector_configs is not None and not isinstance(managed_connector_configs, dict):
        raise ValueError("managed_connectors config must be an object keyed by connector name")
    managed_connector_names: set[str] = set()
    for contribution in extension.contributions:
        if contribution.contribution_type != "managed_connectors":
            continue
        connector_name = contribution.metadata.get("name")
        if not isinstance(connector_name, str) or not connector_name:
            continue
        managed_connector_names.add(connector_name)
        connector_config = (
            managed_connector_configs.get(connector_name)
            if isinstance(managed_connector_configs, dict)
            else None
        )
        if connector_config is None:
            continue
        if not isinstance(connector_config, dict):
            raise ValueError(f"managed connector '{connector_name}' config must be an object")
        config_errors = _managed_connector_config_errors(contribution.metadata, connector_config)
        if config_errors:
            raise ValueError("; ".join(config_errors))
    if isinstance(managed_connector_configs, dict):
        unknown_connectors = sorted(
            connector_name for connector_name in managed_connector_configs.keys() if connector_name not in managed_connector_names
        )
        if unknown_connectors:
            raise ValueError(
                "unknown managed connector config entries: " + ", ".join(unknown_connectors)
            )
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
    _remove_mcp_servers_for_extension(extension)
    shutil.rmtree(extension.root_path)
    payload = _state_payload()
    payload.get("extensions", {}).pop(extension_id, None)
    _save_state(payload)
    _refresh_runtime()
