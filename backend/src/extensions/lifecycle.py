"""Extension lifecycle helpers for install/inspect/configure/remove flows."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any
from urllib.parse import urlparse

from config.settings import settings
from packaging.version import Version
from src.agent.factory import get_base_tools_and_active_skills
from src.extensions.channels import select_active_channel_adapters
from src.extensions.connector_health import (
    ConnectorHealthSnapshot,
    managed_connector_health,
    mcp_server_health,
    planned_configurable_connector_health,
    planned_connector_health,
    static_connector_health,
)
from src.extensions.connectors import ConnectorDefinitionError, MCPServerDefinition, load_mcp_server_definition
from src.extensions.doctor import doctor_snapshot
from src.extensions.layout import iter_extension_manifest_paths, resolve_package_reference
from src.extensions.manifest import (
    ExtensionManifest,
    ExtensionManifestError,
    load_extension_manifest,
    parse_extension_manifest,
)
from src.extensions.permissions import (
    evaluate_contribution_permissions,
    evaluate_tool_permissions,
    summarize_extension_permissions,
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
from src.extensions.state import (
    connector_enabled_override,
    connector_enabled_overrides,
    load_extension_state_payload,
    save_extension_state_payload,
    set_connector_enabled_override,
    state_path,
)
from src.runbooks.loader import scan_runbook_paths
from src.skills.loader import parse_skill_content, scan_skill_paths
from src.runbooks.manager import runbook_manager
from src.skills.manager import skill_manager
from src.starter_packs.manager import starter_pack_manager
from src.starter_packs.loader import scan_starter_pack_paths
from src.tools.mcp_manager import mcp_manager
from src.workflows.loader import parse_workflow_content, scan_workflow_paths
from src.workflows.manager import workflow_manager


_EDITABLE_STUDIO_TYPES = {"skills", "workflows"}
_STUDIO_TYPE_ORDER = {
    "manifest": 0,
    "skills": 1,
    "workflows": 2,
    "runbooks": 3,
    "starter_packs": 4,
    "toolset_presets": 5,
    "context_packs": 6,
    "prompt_packs": 7,
    "speech_profiles": 8,
    "provider_presets": 9,
    "mcp_servers": 10,
    "managed_connectors": 11,
    "memory_providers": 12,
    "automation_triggers": 13,
    "browser_providers": 14,
    "messaging_connectors": 15,
    "observer_definitions": 16,
    "observer_connectors": 17,
    "channel_adapters": 18,
    "canvas_outputs": 19,
    "workflow_runtimes": 20,
    "node_adapters": 21,
    "workspace_adapters": 22,
}
_CONNECTOR_CONTRIBUTION_TYPES = {
    "mcp_servers",
    "managed_connectors",
    "memory_providers",
    "automation_triggers",
    "browser_providers",
    "messaging_connectors",
    "observer_definitions",
    "observer_connectors",
    "channel_adapters",
    "node_adapters",
    "workspace_adapters",
}
_REDACTED_CONFIG_SENTINEL = "__SERAPH_STORED_SECRET__"
_PLANNED_CONNECTOR_CONTRIBUTION_TYPES = {
    "memory_providers",
    "automation_triggers",
    "browser_providers",
    "messaging_connectors",
    "node_adapters",
}
_PASSIVE_TYPED_CONTRIBUTION_FIELDS = {
    "toolset_presets": (
        "name",
        "description",
        "mode",
        "include_tools",
        "exclude_tools",
        "capabilities",
        "execution_boundaries",
        "default_enabled",
    ),
    "prompt_packs": ("name", "title", "description", "instructions"),
    "context_packs": ("name", "description", "instructions", "memory_tags", "profile_fields", "prompt_refs", "domains"),
    "speech_profiles": ("name", "description", "provider", "voice", "supports_tts", "supports_stt", "wake_word"),
    "provider_presets": ("name", "label", "default_model", "notes"),
    "canvas_outputs": ("name", "title", "description", "surface_kind", "sections", "artifact_types", "preferred_panel"),
    "workflow_runtimes": (
        "name",
        "engine_kind",
        "description",
        "delegation_mode",
        "checkpoint_policy",
        "structured_output",
        "default_output_surface",
    ),
}

_PLANNED_CONNECTOR_REQUIRED_FIELDS = {
    "memory_providers": ("name", "provider_kind"),
    "automation_triggers": ("name", "trigger_type"),
    "browser_providers": ("name", "provider_kind"),
    "messaging_connectors": ("name", "platform"),
    "node_adapters": ("name", "adapter_kind"),
}


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "extension"


def _hash_extension_directory(root: Path) -> str:
    hasher = hashlib.sha256()
    for file_path in sorted(path for path in root.rglob("*") if path.is_file()):
        relative_path = file_path.relative_to(root).as_posix()
        hasher.update(relative_path.encode("utf-8"))
        hasher.update(b"\0")
        with file_path.open("rb") as handle:
            while True:
                chunk = handle.read(8192)
                if not chunk:
                    break
                hasher.update(chunk)
    return hasher.hexdigest()


def _extension_package_digest(root_path: str | None) -> str | None:
    if not root_path:
        return None
    root = Path(root_path)
    if not root.exists() or not root.is_dir():
        return None
    return _hash_extension_directory(root)


def _version_relation(candidate_version: str, current_version: str | None) -> str:
    if not current_version:
        return "new"
    candidate = Version(candidate_version)
    current = Version(current_version)
    if candidate > current:
        return "upgrade"
    if candidate < current:
        return "downgrade"
    return "same"


def _extension_lifecycle_plan(
    manifest: ExtensionManifest,
    existing: ExtensionRecord | None,
    *,
    candidate_digest: str,
) -> dict[str, Any]:
    if existing is None:
        return {
            "mode": "new_install",
            "recommended_action": "install",
            "install_allowed": True,
            "update_supported": False,
            "current_location": None,
            "current_version": None,
            "current_source": None,
            "candidate_version": manifest.version,
            "version_relation": "new",
            "package_changed": True,
        }

    current_location = _location_for_extension(existing)
    current_version = existing.manifest.version if existing.manifest is not None else None
    current_digest = _extension_package_digest(existing.root_path)
    package_changed = candidate_digest != current_digest
    version_relation = _version_relation(manifest.version, current_version)
    if current_location == "workspace":
        return {
            "mode": "update_workspace" if package_changed else "up_to_date",
            "recommended_action": "update" if package_changed else "none",
            "install_allowed": False,
            "update_supported": package_changed,
            "current_location": current_location,
            "current_version": current_version,
            "current_source": existing.source,
            "candidate_version": manifest.version,
            "version_relation": version_relation,
            "package_changed": package_changed,
        }
    return {
        "mode": "workspace_override",
        "recommended_action": "install",
        "install_allowed": True,
        "update_supported": False,
        "current_location": current_location,
        "current_version": current_version,
        "current_source": existing.source,
        "candidate_version": manifest.version,
        "version_relation": version_relation,
        "package_changed": package_changed,
    }


def _workspace_root() -> str:
    return settings.workspace_dir


def _workspace_extensions_root() -> str:
    return os.path.join(_workspace_root(), "extensions")


def _state_path() -> str:
    return state_path()


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


def _mcp_definition_items_for_extension(
    extension: ExtensionRecord,
) -> list[tuple[ExtensionContributionRecord, MCPServerDefinition]]:
    items: list[tuple[ExtensionContributionRecord, MCPServerDefinition]] = []
    for contribution in extension.contributions:
        definition = _mcp_definition_for_contribution(contribution)
        if definition is not None:
            items.append((contribution, definition))
    return items


def _managed_connector_config(
    state_entry: dict[str, Any] | None,
    connector_name: str,
) -> dict[str, Any]:
    return _contribution_config(state_entry, "managed_connectors", connector_name)


def _config_section_key(contribution_type: str) -> str:
    return contribution_type


def _contribution_config(
    state_entry: dict[str, Any] | None,
    contribution_type: str,
    contribution_name: str,
) -> dict[str, Any]:
    if not isinstance(state_entry, dict) or not contribution_name:
        return {}

    config_payload = state_entry.get("config")
    if not isinstance(config_payload, dict):
        config_payload = {}
    section_key = _config_section_key(contribution_type)
    section_payload = config_payload.get(section_key)
    if not isinstance(section_payload, dict):
        section_payload = {}
    config_entry = section_payload.get(contribution_name)
    if not isinstance(config_entry, dict):
        return {}
    return config_entry


def _sensitive_config_field_keys(metadata: dict[str, Any]) -> set[str]:
    config_fields = metadata.get("config_fields")
    if not isinstance(config_fields, list):
        return set()
    keys: set[str] = set()
    for field in config_fields:
        if not isinstance(field, dict):
            continue
        if str(field.get("input") or "") != "password":
            continue
        key = field.get("key")
        if isinstance(key, str) and key.strip():
            keys.add(key)
    return keys


def _redact_config_entry(metadata: dict[str, Any], config_entry: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(config_entry, dict):
        return {}
    redacted = deepcopy(config_entry)
    for key in _sensitive_config_field_keys(metadata):
        value = redacted.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value:
            continue
        redacted[key] = _REDACTED_CONFIG_SENTINEL
    return redacted


def _redact_extension_config(extension: ExtensionRecord, raw_config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw_config, dict):
        return {}
    redacted = deepcopy(raw_config)
    for contribution in extension.contributions:
        contribution_type = contribution.contribution_type
        contribution_name = _contribution_name(contribution)
        if contribution_type not in _CONNECTOR_CONTRIBUTION_TYPES or not contribution_name:
            continue
        type_config = redacted.get(contribution_type)
        if not isinstance(type_config, dict):
            continue
        config_entry = type_config.get(contribution_name)
        if not isinstance(config_entry, dict):
            continue
        type_config[contribution_name] = _redact_config_entry(contribution.metadata, config_entry)
    return redacted


def _preserve_secret_placeholders(
    extension: ExtensionRecord,
    *,
    incoming_config: dict[str, Any],
    existing_config: dict[str, Any],
) -> dict[str, Any]:
    merged = deepcopy(incoming_config)
    for contribution in extension.contributions:
        contribution_type = contribution.contribution_type
        contribution_name = _contribution_name(contribution)
        if contribution_type not in _CONNECTOR_CONTRIBUTION_TYPES or not contribution_name:
            continue
        sensitive_keys = _sensitive_config_field_keys(contribution.metadata)
        if not sensitive_keys:
            continue
        type_config = merged.get(contribution_type)
        if not isinstance(type_config, dict):
            continue
        contribution_config = type_config.get(contribution_name)
        if not isinstance(contribution_config, dict):
            continue
        existing_type_config = existing_config.get(contribution_type)
        existing_entry = (
            existing_type_config.get(contribution_name)
            if isinstance(existing_type_config, dict)
            else None
        )
        for key in sensitive_keys:
            if contribution_config.get(key) != _REDACTED_CONFIG_SENTINEL:
                continue
            if isinstance(existing_entry, dict) and key in existing_entry:
                contribution_config[key] = existing_entry[key]
            else:
                contribution_config.pop(key, None)
    return merged


def _contribution_config_errors(
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


def _managed_connector_config_errors(
    metadata: dict[str, Any],
    config_entry: dict[str, Any],
) -> list[str]:
    return _contribution_config_errors(metadata, config_entry)


def _contribution_name(contribution: ExtensionContributionRecord) -> str:
    name = contribution.metadata.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else ""


def _configurable_connector_types(extension: ExtensionRecord) -> set[str]:
    configurable: set[str] = set()
    for contribution in extension.contributions:
        if contribution.contribution_type == "managed_connectors":
            configurable.add(contribution.contribution_type)
            continue
        if (
            contribution.contribution_type in _PLANNED_CONNECTOR_CONTRIBUTION_TYPES
            and isinstance(contribution.metadata.get("config_fields"), list)
        ):
            configurable.add(contribution.contribution_type)
    return configurable


def _connector_default_enabled(contribution: ExtensionContributionRecord) -> bool:
    return bool(contribution.metadata.get("default_enabled", True))


def _connector_enabled(
    contribution: ExtensionContributionRecord,
    state_entry: dict[str, Any] | None,
) -> bool:
    enabled_override = connector_enabled_override(state_entry, contribution.reference)
    if enabled_override is not None:
        return enabled_override
    return _connector_default_enabled(contribution)


def _managed_connector_package_enable_target(
    extension: ExtensionRecord,
    contribution: ExtensionContributionRecord,
) -> bool:
    if contribution.contribution_type not in {"managed_connectors", *_PLANNED_CONNECTOR_CONTRIBUTION_TYPES}:
        return True
    has_non_managed_toggleables = any(
        item.contribution_type in {
            "skills",
            "workflows",
            "mcp_servers",
            "observer_definitions",
            "channel_adapters",
        }
        for item in extension.contributions
    )
    if not has_non_managed_toggleables:
        return True
    return _connector_default_enabled(contribution)


def _install_mcp_servers_for_extension(extension: ExtensionRecord) -> None:
    definition_items = _mcp_definition_items_for_extension(extension)
    for _, definition in definition_items:
        if definition.name in mcp_manager._config:
            raise FileExistsError(
                f"MCP server '{definition.name}' from extension '{extension.id}' already exists"
            )
    added: list[str] = []
    try:
        for contribution, definition in definition_items:
            # Connector packages register safely at install time; operators enable them explicitly later.
            mcp_manager.add_server(
                name=definition.name,
                url=definition.url,
                description=definition.description,
                enabled=False,
                headers=definition.headers,
                auth_hint=definition.auth_hint,
                extension_id=extension.id,
                extension_reference=contribution.reference,
                extension_display_name=extension.display_name,
                source="extension",
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


def _sync_mcp_servers_for_updated_extension(
    previous_extension: ExtensionRecord,
    updated_extension: ExtensionRecord,
) -> None:
    previous_definitions = _mcp_definitions_for_extension(previous_extension)
    updated_definition_items = _mcp_definition_items_for_extension(updated_extension)
    updated_definitions = {definition.name: definition for _, definition in updated_definition_items}
    updated_references = {definition.name: contribution.reference for contribution, definition in updated_definition_items}

    for removed_name in sorted(set(previous_definitions) - set(updated_definitions)):
        if removed_name in mcp_manager._config:
            mcp_manager.remove_server(removed_name)

    for name, definition in updated_definitions.items():
        previous_state = mcp_manager._config.get(name, {})
        enabled = bool(previous_state.get("enabled", False))
        headers = definition.headers
        auth_hint = definition.auth_hint
        if name in mcp_manager._config:
            mcp_manager.update_server(
                name,
                url=definition.url,
                description=definition.description,
                headers=headers,
                enabled=enabled,
                auth_hint=auth_hint,
                extension_id=updated_extension.id,
                extension_reference=updated_references.get(name),
                extension_display_name=updated_extension.display_name,
                source="extension",
            )
        else:
            mcp_manager.add_server(
                name=definition.name,
                url=definition.url,
                description=definition.description,
                enabled=enabled,
                headers=headers,
                auth_hint=auth_hint,
                extension_id=updated_extension.id,
                extension_reference=updated_references.get(name),
                extension_display_name=updated_extension.display_name,
                source="extension",
            )


def _registry() -> ExtensionRegistry:
    return ExtensionRegistry(
        manifest_roots=_ensure_manifest_roots(),
        skill_dirs=[getattr(skill_manager, "_skills_dir", "")] if getattr(skill_manager, "_skills_dir", "") else [],
        workflow_dirs=[getattr(workflow_manager, "_workflows_dir", "")] if getattr(workflow_manager, "_workflows_dir", "") else [],
        mcp_runtime=mcp_manager,
    )


def _state_payload() -> dict[str, Any]:
    return load_extension_state_payload()


def _save_state(payload: dict[str, Any]) -> None:
    save_extension_state_payload(payload)


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

    payload = _contribution_payload(extension, contribution, indexes=indexes)
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


def _validate_skill_draft(
    content: str,
    *,
    path: str,
    extension: ExtensionRecord | None = None,
) -> dict[str, Any]:
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
    permission_profile = evaluate_tool_permissions(extension, tool_names=skill.requires_tools)
    return {
        "valid": True,
        "errors": [],
        "runtime_ready": not missing_tools and skill.enabled and permission_profile["ok"],
        "missing_tools": missing_tools,
        "permission_status": permission_profile["status"],
        "missing_manifest_tools": list(permission_profile["missing_tools"]),
        "missing_manifest_execution_boundaries": list(permission_profile["missing_execution_boundaries"]),
        "requires_network": bool(permission_profile["requires_network"]),
        "missing_manifest_network": bool(permission_profile["missing_network"]),
        "risk_level": permission_profile["risk_level"],
        "approval_behavior": permission_profile["approval_behavior"],
        "requires_approval": bool(permission_profile["requires_approval"]),
        "skill": {
            "name": skill.name,
            "description": skill.description,
            "requires_tools": skill.requires_tools,
            "user_invocable": skill.user_invocable,
            "enabled": skill.enabled,
            "file_path": skill.file_path,
        },
    }


def _validate_workflow_draft(
    content: str,
    *,
    path: str,
    extension: ExtensionRecord | None = None,
) -> dict[str, Any]:
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
    permission_profile = evaluate_tool_permissions(extension, tool_names=workflow.step_tools)
    requires_approval = risk_level == "high" or ("external_mcp" in execution_boundaries and mcp_mode == "approval")
    return {
        "valid": True,
        "errors": [],
        "runtime_ready": not missing_tools and not missing_skills and workflow.enabled and permission_profile["ok"],
        "missing_tools": missing_tools,
        "missing_skills": missing_skills,
        "requires_approval": requires_approval,
        "permission_status": permission_profile["status"],
        "missing_manifest_tools": list(permission_profile["missing_tools"]),
        "missing_manifest_execution_boundaries": list(permission_profile["missing_execution_boundaries"]),
        "requires_network": bool(permission_profile["requires_network"]),
        "missing_manifest_network": bool(permission_profile["missing_network"]),
        "approval_behavior": permission_profile["approval_behavior"],
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
        return _validate_skill_draft(content, path=reference, extension=extension)
    if contribution_type == "workflows":
        return _validate_workflow_draft(content, path=reference, extension=extension)
    return {"valid": True}


def _validate_extension_candidate_root(extension: ExtensionRecord, candidate_root: Path) -> ExtensionManifest:
    if extension.root_path is None or extension.manifest_path is None:
        raise ValueError(f"extension '{extension.id}' does not expose a package root")
    report = validate_extension_package(candidate_root)
    if not report.ok:
        raise ValueError("extension update would make the package invalid")
    if not any(result.extension_id == extension.id for result in report.results):
        raise ValueError("extension update would make the package unloadable")

    candidate_manifest_path = candidate_root / Path(extension.manifest_path).name
    manifest = load_extension_manifest(candidate_manifest_path)
    if manifest.id != extension.id:
        raise ValueError(f"extension update must keep manifest id '{extension.id}'")
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
    return manifest


def _validate_extension_candidate_package(
    extension: ExtensionRecord,
    *,
    reference: str,
    resolved_path: Path,
    content: str,
) -> None:
    if extension.root_path is None:
        raise ValueError(f"extension '{extension.id}' does not expose a package root")
    root_path = Path(extension.root_path)
    resolved_reference = str(resolved_path.relative_to(root_path))
    with tempfile.TemporaryDirectory(prefix="seraph-extension-package-") as temp_root:
        candidate_root = Path(temp_root) / "package"
        shutil.copytree(extension.root_path, candidate_root)
        candidate_target = candidate_root / Path(resolved_reference)
        candidate_target.parent.mkdir(parents=True, exist_ok=True)
        candidate_target.write_text(content, encoding="utf-8")
        _validate_extension_candidate_root(extension, candidate_root)


def _contribution_indexes(
    *,
    state_by_id: dict[str, Any] | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
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
    enabled_overrides = connector_enabled_overrides(state_by_id)
    channel_adapters = {
        os.path.abspath(item.resolved_path): {
            "name": item.name,
            "transport": item.transport,
        }
        for item in select_active_channel_adapters(
            observer_snapshot.list_contributions("channel_adapters"),
            enabled_overrides=enabled_overrides,
        )
        if item.resolved_path
    }
    observer_definitions = {
        os.path.abspath(item.resolved_path): {
            "name": item.name,
            "source_type": item.source_type,
        }
        for item in select_active_observer_definitions(
            observer_snapshot.list_contributions("observer_definitions"),
            enabled_overrides=enabled_overrides,
        )
        if item.resolved_path
    }
    return {
        "skills": skills,
        "workflows": workflows,
        "runbooks": runbooks,
        "starter_packs": starter_packs,
        "mcp_servers": mcp_servers,
        "channel_adapters": channel_adapters,
        "observer_definitions": observer_definitions,
    }


def _planned_connector_definition_errors(contribution: ExtensionContributionRecord) -> list[str]:
    errors: list[str] = []
    required_fields = _PLANNED_CONNECTOR_REQUIRED_FIELDS.get(contribution.contribution_type, ())
    for field_name in required_fields:
        value = contribution.metadata.get(field_name)
        if not isinstance(value, str) or not value.strip():
            errors.append(
                f"{contribution.contribution_type.removesuffix('s').replace('_', ' ')} definition is invalid and cannot load"
            )
            break
    conflict = contribution.metadata.get("registry_conflict")
    if isinstance(conflict, dict):
        name = str(conflict.get("name") or contribution.metadata.get("name") or contribution.reference)
        winner = str(conflict.get("winner_display_name") or conflict.get("winner_extension_id") or "another extension")
        errors.append(
            f"{contribution.contribution_type.removesuffix('s').replace('_', ' ')} '{name}' conflicts with {winner}"
        )
    return errors


def _contribution_payload(
    extension: ExtensionRecord,
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
        payload["permission_profile"] = evaluate_contribution_permissions(
            extension,
            contribution_type=contribution.contribution_type,
            metadata=contribution.metadata,
        )
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
        health = mcp_server_health(contribution.metadata, runtime_entry)
        payload["health"] = health.as_payload()
        payload.setdefault("status", health.state)
        return payload
    if contribution.contribution_type == "managed_connectors":
        default_enabled = _connector_default_enabled(contribution)
        enabled = _connector_enabled(contribution, state_entry)
        payload = {
            "type": contribution.contribution_type,
            "reference": contribution.reference,
            "resolved_path": normalized_path,
            "loaded": False,
            "default_enabled": default_enabled,
            "enabled": enabled,
        }
        payload["permission_profile"] = evaluate_contribution_permissions(
            extension,
            contribution_type=contribution.contribution_type,
            metadata=contribution.metadata,
        )
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
            payload["status"] = "requires_config"
        elif config_errors:
            payload["configured"] = False
            payload["status"] = "invalid_config"
        elif not enabled:
            payload["configured"] = True
            payload["status"] = "disabled"
        else:
            payload["configured"] = True
            payload["status"] = "ready"
        payload["health"] = managed_connector_health(
            contribution.metadata,
            config_entry,
            config_errors,
            enabled=enabled,
        ).as_payload()
        payload["status"] = str(payload["health"].get("state") or payload["status"])
        return payload
    if contribution.contribution_type in _PLANNED_CONNECTOR_CONTRIBUTION_TYPES:
        default_enabled = _connector_default_enabled(contribution)
        enabled = _connector_enabled(contribution, state_entry)
        payload = {
            "type": contribution.contribution_type,
            "reference": contribution.reference,
            "resolved_path": normalized_path,
            "loaded": False,
            "default_enabled": default_enabled,
            "enabled": enabled,
        }
        payload["permission_profile"] = evaluate_contribution_permissions(
            extension,
            contribution_type=contribution.contribution_type,
            metadata=contribution.metadata,
        )
        for field_name in (
            "name",
            "description",
            "trigger_type",
            "schedule",
            "endpoint",
            "topic",
            "provider_kind",
            "platform",
            "adapter_kind",
        ):
            field_value = contribution.metadata.get(field_name)
            if isinstance(field_value, str) and field_value:
                payload[field_name] = field_value
        for field_name in ("capabilities", "config_fields", "delivery_modes"):
            field_value = contribution.metadata.get(field_name)
            if isinstance(field_value, list) and field_value:
                payload[field_name] = field_value
        for field_name in ("requires_network", "requires_daemon"):
            if field_name in contribution.metadata:
                payload[field_name] = bool(contribution.metadata.get(field_name))
        contribution_name = _contribution_name(contribution)
        config_entry = _contribution_config(state_entry, contribution.contribution_type, contribution_name)
        config_errors = _contribution_config_errors(contribution.metadata, config_entry) if config_entry else []
        definition_errors = _planned_connector_definition_errors(contribution)
        requires_config = bool(contribution.metadata.get("config_fields"))
        configured = bool(config_entry) if requires_config else True
        if config_entry:
            payload["config_keys"] = sorted(config_entry.keys())
        if config_errors:
            payload["config_errors"] = config_errors
        if definition_errors:
            payload["configured"] = False
            payload["health"] = ConnectorHealthSnapshot(
                state="invalid",
                summary=definition_errors[0],
                ready=False,
                enabled=enabled,
                configured=False,
                connected=False,
                error="; ".join(definition_errors),
                supports_test=False,
                supports_configure=True,
                supports_enable=True,
                supports_disable=True,
            ).as_payload()
            payload["status"] = "invalid"
        else:
            health = planned_configurable_connector_health(
                "Runtime support for this connector surface lands in the later capability-reach waves.",
                enabled=enabled,
                configured=configured,
                config_errors=config_errors,
                supports_test=False,
            )
            payload["configured"] = configured and not config_errors
            payload["health"] = health.as_payload()
            payload["status"] = str(payload["health"].get("state") or "planned")
        return payload
    if contribution.contribution_type == "observer_definitions":
        active_definition = (
            indexes.get("observer_definitions", {}).get(normalized_path or "")
            if normalized_path is not None
            else None
        )
        default_enabled = bool(contribution.metadata.get("default_enabled", True))
        enabled = _connector_enabled(contribution, state_entry)
        valid_definition = all(
            isinstance(contribution.metadata.get(field_name), str) and str(contribution.metadata.get(field_name)).strip()
            for field_name in ("name", "source_type")
        )
        payload = {
            "type": contribution.contribution_type,
            "reference": contribution.reference,
            "resolved_path": normalized_path,
            "loaded": active_definition is not None,
            "enabled": enabled,
        }
        payload["permission_profile"] = evaluate_contribution_permissions(
            extension,
            contribution_type=contribution.contribution_type,
            metadata=contribution.metadata,
        )
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
        elif not enabled:
            payload["status"] = "disabled"
        else:
            payload["status"] = "overridden"
        payload["health"] = static_connector_health(
            active=payload["loaded"],
            valid=valid_definition,
            default_enabled=enabled,
            active_summary="Observer source is active in the runtime selection.",
            invalid_summary="Observer definition is invalid and cannot load.",
            disabled_summary="Observer source is disabled in extension lifecycle state.",
            overridden_summary="Another extension currently owns this observer source.",
            supports_enable=True,
            supports_disable=True,
            supports_test=True,
        ).as_payload()
        return payload
    if contribution.contribution_type == "channel_adapters":
        active_adapter = (
            indexes.get("channel_adapters", {}).get(normalized_path or "")
            if normalized_path is not None
            else None
        )
        default_enabled = bool(contribution.metadata.get("default_enabled", True))
        enabled = _connector_enabled(contribution, state_entry)
        requires_daemon = bool(contribution.metadata.get("requires_daemon"))
        valid_adapter = all(
            isinstance(contribution.metadata.get(field_name), str) and str(contribution.metadata.get(field_name)).strip()
            for field_name in ("name", "transport")
        )
        payload = {
            "type": contribution.contribution_type,
            "reference": contribution.reference,
            "resolved_path": normalized_path,
            "loaded": active_adapter is not None,
            "enabled": enabled,
        }
        payload["permission_profile"] = evaluate_contribution_permissions(
            extension,
            contribution_type=contribution.contribution_type,
            metadata=contribution.metadata,
        )
        for field_name in ("name", "transport", "description"):
            field_value = contribution.metadata.get(field_name)
            if isinstance(field_value, str) and field_value:
                payload[field_name] = field_value
        if "default_enabled" in contribution.metadata:
            payload["default_enabled"] = default_enabled
        if "requires_daemon" in contribution.metadata:
            payload["requires_daemon"] = requires_daemon
        if payload["loaded"]:
            payload["status"] = "active"
        elif not valid_adapter:
            payload["status"] = "invalid"
        elif not enabled:
            payload["status"] = "disabled"
        else:
            payload["status"] = "overridden"
        daemon_connected: bool | None = None
        if requires_daemon:
            from src.observer.manager import context_manager

            daemon_connected = context_manager.is_daemon_connected()
        health = static_connector_health(
            active=payload["loaded"],
            valid=valid_adapter,
            default_enabled=enabled,
            active_summary="Channel adapter is active in the delivery runtime.",
            invalid_summary="Channel adapter is invalid and cannot load.",
            disabled_summary="Channel adapter is disabled in extension lifecycle state.",
            overridden_summary="Another extension currently owns this channel transport.",
            supports_enable=True,
            supports_disable=True,
            supports_test=True,
        )
        if payload["loaded"] and requires_daemon and daemon_connected is False:
            payload["status"] = "degraded"
            payload["health"] = ConnectorHealthSnapshot(
                state="degraded",
                summary="Channel adapter owns the transport, but the native daemon is offline.",
                ready=False,
                enabled=enabled,
                configured=True,
                connected=False,
                supports_test=True,
                supports_enable=True,
                supports_disable=True,
            ).as_payload()
        else:
            payload["health"] = health.as_payload()
            if daemon_connected is not None:
                payload["health"]["connected"] = daemon_connected
        return payload
    if contribution.contribution_type in {"observer_connectors", "workspace_adapters"}:
        payload = {
            "type": contribution.contribution_type,
            "reference": contribution.reference,
            "resolved_path": normalized_path,
            "loaded": False,
            "permission_profile": evaluate_contribution_permissions(
                extension,
                contribution_type=contribution.contribution_type,
                metadata=contribution.metadata,
            ),
            "status": "planned",
            "health": planned_connector_health(
                "Runtime support for this connector surface is not wired yet.",
            ).as_payload(),
        }
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
        "permission_profile": evaluate_contribution_permissions(
            extension,
            contribution_type=contribution.contribution_type,
            metadata=contribution.metadata,
        ),
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
        for field_name in (
            "requires_tools",
            "requires_skills",
            "policy_modes",
            "execution_boundaries",
            "risk_level",
            "accepts_secret_refs",
            "permission_status",
            "missing_manifest_tools",
            "missing_manifest_execution_boundaries",
            "requires_network",
            "missing_manifest_network",
            "approval_behavior",
            "requires_approval",
        ):
            if field_name in item:
                payload[field_name] = item[field_name]
    if contribution.contribution_type in {"skills", "workflows"}:
        for field_name in (
            "name",
            "description",
            "requires_tools",
            "requires_skills",
            "step_tools",
            "tool_name",
            "runtime_profile",
            "output_surface",
            "declared_output_surface",
            "effective_output_surface",
            "output_surface_title",
            "output_surface_sections",
            "output_surface_artifact_types",
            "user_invocable",
            "default_enabled",
        ):
            field_value = contribution.metadata.get(field_name)
            if field_value not in (None, "", [], {}):
                payload.setdefault(field_name, field_value)
    if contribution.contribution_type in _PASSIVE_TYPED_CONTRIBUTION_FIELDS:
        for field_name in _PASSIVE_TYPED_CONTRIBUTION_FIELDS[contribution.contribution_type]:
            field_value = contribution.metadata.get(field_name)
            if field_value not in (None, "", [], {}):
                payload.setdefault(field_name, field_value)
    payload.setdefault("permission_status", payload["permission_profile"]["status"])
    payload.setdefault("missing_manifest_tools", list(payload["permission_profile"]["missing_tools"]))
    payload.setdefault(
        "missing_manifest_execution_boundaries",
        list(payload["permission_profile"]["missing_execution_boundaries"]),
    )
    payload.setdefault("requires_network", bool(payload["permission_profile"]["requires_network"]))
    payload.setdefault("missing_manifest_network", bool(payload["permission_profile"]["missing_network"]))
    payload.setdefault("approval_behavior", payload["permission_profile"]["approval_behavior"])
    payload.setdefault("requires_approval", bool(payload["permission_profile"]["requires_approval"]))
    return payload


def _toggle_targets(extension: ExtensionRecord) -> list[dict[str, str]]:
    indexes = _contribution_indexes()
    targets: list[dict[str, str]] = []
    for contribution in extension.contributions:
        payload = _contribution_payload(extension, contribution, indexes=indexes)
        target_name = payload.get("name")
        if not isinstance(target_name, str) or not target_name:
            continue
        if contribution.contribution_type == "skills":
            targets.append({"type": "skill", "name": target_name})
        elif contribution.contribution_type == "workflows":
            targets.append({"type": "workflow", "name": target_name})
        elif contribution.contribution_type == "mcp_servers":
            targets.append({"type": "mcp_server", "name": target_name})
        elif contribution.contribution_type == "managed_connectors":
            targets.append({"type": "managed_connector", "name": target_name, "reference": contribution.reference})
        elif contribution.contribution_type == "memory_providers":
            targets.append({"type": "memory_provider", "name": target_name, "reference": contribution.reference})
        elif contribution.contribution_type == "automation_triggers":
            targets.append({"type": "automation_trigger", "name": target_name, "reference": contribution.reference})
        elif contribution.contribution_type == "browser_providers":
            targets.append({"type": "browser_provider", "name": target_name, "reference": contribution.reference})
        elif contribution.contribution_type == "messaging_connectors":
            targets.append({"type": "messaging_connector", "name": target_name, "reference": contribution.reference})
        elif contribution.contribution_type == "observer_definitions":
            targets.append({"type": "observer_definition", "name": target_name, "reference": contribution.reference})
        elif contribution.contribution_type == "channel_adapters":
            targets.append({"type": "channel_adapter", "name": target_name, "reference": contribution.reference})
        elif contribution.contribution_type == "node_adapters":
            targets.append({"type": "node_adapter", "name": target_name, "reference": contribution.reference})
    return targets


def _toggleable_contribution_types(extension: ExtensionRecord) -> list[str]:
    allowed_types = {
        "skills",
        "workflows",
        "mcp_servers",
        "managed_connectors",
        "memory_providers",
        "automation_triggers",
        "browser_providers",
        "messaging_connectors",
        "observer_definitions",
        "channel_adapters",
        "node_adapters",
    }
    discovered = {contribution.contribution_type for contribution in extension.contributions if contribution.contribution_type in allowed_types}
    return sorted(discovered, key=lambda item: (_STUDIO_TYPE_ORDER.get(item, 999), item))


def _passive_contribution_types(extension: ExtensionRecord) -> list[str]:
    toggleable_types = {
        "skills",
        "workflows",
        "mcp_servers",
        "managed_connectors",
        "memory_providers",
        "automation_triggers",
        "browser_providers",
        "messaging_connectors",
        "observer_definitions",
        "channel_adapters",
        "node_adapters",
    }
    discovered = {contribution.contribution_type for contribution in extension.contributions if contribution.contribution_type not in toggleable_types}
    return sorted(discovered, key=lambda item: (_STUDIO_TYPE_ORDER.get(item, 999), item))


def _connector_summary(contributions: list[dict[str, Any]]) -> dict[str, Any]:
    connector_items = [
        contribution
        for contribution in contributions
        if str(contribution.get("type") or "") in _CONNECTOR_CONTRIBUTION_TYPES
    ]
    state_counts: dict[str, int] = {}
    for contribution in connector_items:
        health = contribution.get("health")
        state = str(health.get("state") if isinstance(health, dict) else contribution.get("status") or "unknown")
        state_counts[state] = state_counts.get(state, 0) + 1
    return {
        "total": len(connector_items),
        "ready": sum(
            1
            for contribution in connector_items
            if isinstance(contribution.get("health"), dict) and bool(contribution["health"].get("ready"))
        ),
        "states": state_counts,
    }


def _extension_load_errors_for_extension(
    extension: ExtensionRecord,
    load_errors: list[ExtensionLoadErrorRecord],
) -> list[dict[str, Any]]:
    return [
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


def _extension_payload(
    extension: ExtensionRecord,
    *,
    load_errors: list[ExtensionLoadErrorRecord],
    doctor_by_id: dict[str, Any],
    state_by_id: dict[str, Any],
) -> dict[str, Any]:
    indexes = _contribution_indexes(state_by_id=state_by_id)
    doctor_result = doctor_by_id.get(extension.id)
    issues = []
    if doctor_result is not None:
        issues = [asdict(issue) for issue in doctor_result.issues]
    extension_load_errors = _extension_load_errors_for_extension(extension, load_errors)
    toggles = _toggle_targets(extension)
    state_entry = state_by_id.get(extension.id, {}) if isinstance(state_by_id.get(extension.id), dict) else {}
    location = _location_for_extension(extension)
    contributions = [
        _contribution_payload(extension, contribution, indexes=indexes, state_entry=state_entry)
        for contribution in extension.contributions
    ]
    permission_summary = summarize_extension_permissions(
        extension,
        contribution_profiles=[
            contribution.get("permission_profile", {})
            for contribution in contributions
            if isinstance(contribution.get("permission_profile"), dict)
        ],
    )
    toggleable_types = _toggleable_contribution_types(extension)
    non_managed_toggleable_types = {
        contribution_type
        for contribution_type in toggleable_types
        if contribution_type != "managed_connectors"
    }
    enabled_values = [
        bool(item["enabled"])
        for item in contributions
        if isinstance(item, dict)
        and "enabled" in item
        and (
            (
                isinstance(item.get("type"), str)
                and item["type"] in non_managed_toggleable_types
            )
            if non_managed_toggleable_types
            else (
                isinstance(item.get("type"), str)
                and item["type"] in toggleable_types
            )
        )
    ]
    enabled: bool | None = None
    if enabled_values:
        enabled = all(enabled_values)
    passive_types = _passive_contribution_types(extension)
    configurable_types = _configurable_connector_types(extension)
    connector_summary = _connector_summary(contributions)
    return {
        "id": extension.id,
        "display_name": extension.display_name,
        "version": extension.manifest.version if extension.manifest is not None else None,
        "kind": extension.kind,
        "trust": extension.trust,
        "source": extension.source,
        "location": location,
        "root_path": extension.root_path,
        "package_digest": _extension_package_digest(extension.root_path),
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
        "permissions": permission_summary["declared"],
        "permission_summary": {
            "status": permission_summary["status"],
            "ok": permission_summary["ok"],
            "required": permission_summary["required"],
            "missing": permission_summary["missing"],
            "risk_level": permission_summary["risk_level"],
        },
        "approval_profile": permission_summary["approval_profile"],
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
        "configurable": bool(configurable_types),
        "metadata_supported": True,
        "config_scope": (
            "metadata_and_managed_connectors"
            if configurable_types == {"managed_connectors"}
            else (
                "metadata_and_connector_configs"
                if configurable_types
                else "metadata_only"
            )
        ),
        "enabled": enabled,
        "config": _redact_extension_config(extension, state_entry.get("config", {})),
        "connector_summary": connector_summary,
        "contributions": contributions,
        "studio_files": _studio_files(extension, indexes=indexes),
    }


def list_extensions() -> dict[str, Any]:
    snapshot = _registry().snapshot()
    doctor = doctor_snapshot(snapshot)
    doctor_by_id = {result.extension_id: result for result in doctor.results}
    state_by_id = _state_payload()["extensions"]
    raw_extensions = [
        _extension_payload(
            extension,
            load_errors=snapshot.load_errors,
            doctor_by_id=doctor_by_id,
            state_by_id=state_by_id,
        )
        for extension in snapshot.extensions
    ]
    deduped_by_id: dict[str, dict[str, Any]] = {}
    for extension in raw_extensions:
        existing = deduped_by_id.get(extension["id"])
        if existing is None or _extension_payload_priority(extension) < _extension_payload_priority(existing):
            deduped_by_id[extension["id"]] = extension
    extensions = sorted(
        deduped_by_id.values(),
        key=lambda item: (item["display_name"].lower(), item["id"]),
    )
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


def _extension_payload_priority(payload: dict[str, Any]) -> tuple[int, str]:
    location = str(payload.get("location") or "")
    if location == "workspace":
        return (0, str(payload.get("display_name") or "").lower())
    if location == "bundled":
        return (1, str(payload.get("display_name") or "").lower())
    return (2, str(payload.get("display_name") or "").lower())


def get_extension(extension_id: str) -> dict[str, Any]:
    payload = list_extensions()
    matches = [item for item in payload["extensions"] if item["id"] == extension_id]
    if not matches:
        raise KeyError(extension_id)
    return min(matches, key=_extension_payload_priority)


def list_extension_connectors(extension_id: str) -> dict[str, Any]:
    extension = get_extension(extension_id)
    connectors = [
        contribution
        for contribution in extension["contributions"]
        if str(contribution.get("type") or "") in _CONNECTOR_CONTRIBUTION_TYPES
    ]
    return {
        "extension_id": extension["id"],
        "display_name": extension["display_name"],
        "connectors": connectors,
        "summary": extension.get("connector_summary") or _connector_summary(connectors),
    }


def get_extension_connector(extension_id: str, reference: str) -> dict[str, Any]:
    connectors_payload = list_extension_connectors(extension_id)
    for connector in connectors_payload["connectors"]:
        if connector.get("reference") == reference:
            return {
                **connector,
                "extension_id": connectors_payload["extension_id"],
                "display_name": connectors_payload["display_name"],
            }
    raise KeyError(reference)


def _set_managed_connector_enabled(
    extension: ExtensionRecord,
    contribution: ExtensionContributionRecord,
    *,
    enabled: bool,
) -> dict[str, Any]:
    connector_name = contribution.metadata.get("name")
    if not isinstance(connector_name, str) or not connector_name:
        raise ValueError(
            f"connector '{contribution.reference}' in extension '{extension.id}' is not a valid managed connector definition"
        )
    state_payload = _state_payload()
    state_by_id = state_payload.get("extensions")
    state_entry = state_by_id.get(extension.id, {}) if isinstance(state_by_id, dict) and isinstance(state_by_id.get(extension.id), dict) else {}
    config_entry = _managed_connector_config(state_entry, connector_name)
    config_errors = _managed_connector_config_errors(contribution.metadata, config_entry)
    if enabled and (not config_entry or config_errors):
        raise ValueError(
            f"managed connector '{connector_name}' requires valid configuration before enable"
        )
    set_connector_enabled_override(
        state_payload,
        extension.id,
        contribution.reference,
        enabled=enabled,
    )
    _save_state(state_payload)
    return {
        "extension": get_extension(extension.id),
        "connector": get_extension_connector(extension.id, contribution.reference),
        "changed": {
            "type": "managed_connector",
            "name": connector_name,
            "reference": contribution.reference,
            "enabled": enabled,
            "ok": True,
        },
    }


def _set_planned_connector_enabled(
    extension: ExtensionRecord,
    contribution: ExtensionContributionRecord,
    *,
    enabled: bool,
    changed_type: str,
) -> dict[str, Any]:
    contribution_name = _contribution_name(contribution)
    if not contribution_name:
        raise ValueError(
            f"connector '{contribution.reference}' in extension '{extension.id}' is not a valid {changed_type} definition"
        )
    definition_errors = _planned_connector_definition_errors(contribution)
    if definition_errors:
        raise ValueError(definition_errors[0])
    state_payload = _state_payload()
    state_by_id = state_payload.get("extensions")
    state_entry = (
        state_by_id.get(extension.id)
        if isinstance(state_by_id, dict) and isinstance(state_by_id.get(extension.id), dict)
        else {}
    )
    config_entry = _contribution_config(state_entry, contribution.contribution_type, contribution_name)
    config_errors = _contribution_config_errors(contribution.metadata, config_entry)
    if enabled and contribution.metadata.get("config_fields") and (not config_entry or config_errors):
        raise ValueError(
            f"{changed_type.replace('_', ' ')} '{contribution_name}' requires valid configuration before enable"
        )
    set_connector_enabled_override(
        state_payload,
        extension.id,
        contribution.reference,
        enabled=enabled,
    )
    _save_state(state_payload)
    return {
        "extension": get_extension(extension.id),
        "connector": get_extension_connector(extension.id, contribution.reference),
        "changed": {
            "type": changed_type,
            "name": contribution_name,
            "reference": contribution.reference,
            "enabled": enabled,
            "ok": True,
        },
    }


def _set_runtime_selector_contribution_enabled(
    extension: ExtensionRecord,
    contribution: ExtensionContributionRecord,
    *,
    enabled: bool,
    changed_type: str,
) -> dict[str, Any]:
    contribution_name = contribution.metadata.get("name")
    if not isinstance(contribution_name, str) or not contribution_name:
        raise ValueError(
            f"connector '{contribution.reference}' in extension '{extension.id}' is not a valid {changed_type} definition"
        )
    state_payload = _state_payload()
    set_connector_enabled_override(
        state_payload,
        extension.id,
        contribution.reference,
        enabled=enabled,
    )
    _save_state(state_payload)
    return {
        "extension": get_extension(extension.id),
        "connector": get_extension_connector(extension.id, contribution.reference),
        "changed": {
            "type": changed_type,
            "name": contribution_name,
            "reference": contribution.reference,
            "enabled": enabled,
            "ok": True,
        },
    }


def set_extension_connector_enabled(
    extension_id: str,
    reference: str,
    *,
    enabled: bool,
) -> dict[str, Any]:
    snapshot = _registry().snapshot()
    extension = snapshot.get_extension(extension_id)
    if extension is None:
        raise KeyError(extension_id)

    contribution = next((item for item in extension.contributions if item.reference == reference), None)
    if contribution is None:
        raise KeyError(reference)
    if contribution.contribution_type == "managed_connectors":
        return _set_managed_connector_enabled(extension, contribution, enabled=enabled)
    if contribution.contribution_type == "memory_providers":
        return _set_planned_connector_enabled(
            extension,
            contribution,
            enabled=enabled,
            changed_type="memory_provider",
        )
    if contribution.contribution_type == "automation_triggers":
        return _set_planned_connector_enabled(
            extension,
            contribution,
            enabled=enabled,
            changed_type="automation_trigger",
        )
    if contribution.contribution_type == "browser_providers":
        return _set_planned_connector_enabled(
            extension,
            contribution,
            enabled=enabled,
            changed_type="browser_provider",
        )
    if contribution.contribution_type == "messaging_connectors":
        return _set_planned_connector_enabled(
            extension,
            contribution,
            enabled=enabled,
            changed_type="messaging_connector",
        )
    if contribution.contribution_type == "observer_definitions":
        return _set_runtime_selector_contribution_enabled(
            extension,
            contribution,
            enabled=enabled,
            changed_type="observer_definition",
        )
    if contribution.contribution_type == "channel_adapters":
        return _set_runtime_selector_contribution_enabled(
            extension,
            contribution,
            enabled=enabled,
            changed_type="channel_adapter",
        )
    if contribution.contribution_type == "node_adapters":
        return _set_planned_connector_enabled(
            extension,
            contribution,
            enabled=enabled,
            changed_type="node_adapter",
        )
    if contribution.contribution_type != "mcp_servers":
        raise ValueError(
            f"connector '{reference}' in extension '{extension_id}' does not support enable or disable yet"
        )

    definition = _mcp_definition_for_contribution(contribution)
    if definition is None:
        raise ValueError(
            f"connector '{reference}' in extension '{extension_id}' is not a valid MCP server definition"
        )

    existing_config = mcp_manager._config.get(definition.name)
    if existing_config is None:
        mcp_manager.add_server(
            name=definition.name,
            url=definition.url,
            description=definition.description,
            enabled=enabled,
            headers=definition.headers,
            auth_hint=definition.auth_hint,
            extension_id=extension.id,
            extension_reference=contribution.reference,
            extension_display_name=extension.display_name,
            source="extension",
        )
    else:
        updated = mcp_manager.update_server(
            definition.name,
            enabled=enabled,
            extension_id=extension.id,
            extension_reference=contribution.reference,
            extension_display_name=extension.display_name,
            source="extension",
        )
        if not updated:
            raise ValueError(
                f"connector '{reference}' in extension '{extension_id}' could not be updated in the MCP runtime"
            )

    return {
        "extension": get_extension(extension_id),
        "connector": get_extension_connector(extension_id, reference),
        "changed": {
            "type": "mcp_server",
            "name": definition.name,
            "reference": contribution.reference,
            "enabled": enabled,
            "ok": True,
        },
    }


def validate_extension_path(path: str) -> dict[str, Any]:
    package_root, manifest = _load_manifest_from_path(path)
    snapshot = ExtensionRegistry(
        manifest_roots=[str(package_root)],
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    ).snapshot()
    extension = snapshot.get_extension(manifest.id)
    existing = _registry().snapshot().get_extension(manifest.id)
    report = validate_extension_package(package_root)
    package_digest = _hash_extension_directory(package_root)
    permission_summary = summarize_extension_permissions(
        extension,
        contribution_profiles=[
            evaluate_contribution_permissions(
                extension,
                contribution_type=contribution.contribution_type,
                metadata=contribution.metadata,
            )
            for contribution in (extension.contributions if extension is not None else [])
        ],
    ) if extension is not None else {
        "declared": None,
        "status": "unknown",
        "ok": True,
        "required": {},
        "missing": {},
        "risk_level": "low",
        "approval_profile": {
            "requires_runtime_approval": False,
            "runtime_behavior": "never",
            "requires_lifecycle_approval": False,
            "lifecycle_boundaries": [],
            "risk_level": "low",
        },
    }
    return {
        "path": str(package_root),
        "package_digest": package_digest,
        "manifest_path": str(package_root / "manifest.yaml"),
        "extension_id": manifest.id,
        "display_name": manifest.display_name,
        "version": manifest.version,
        "lifecycle_plan": _extension_lifecycle_plan(
            manifest,
            existing,
            candidate_digest=package_digest,
        ),
        "permissions": permission_summary["declared"],
        "permission_summary": {
            "status": permission_summary["status"],
            "ok": permission_summary["ok"],
            "required": permission_summary["required"],
            "missing": permission_summary["missing"],
            "risk_level": permission_summary["risk_level"],
        },
        "approval_profile": permission_summary["approval_profile"],
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


def update_extension_path(path: str) -> dict[str, Any]:
    package_root, manifest = _load_manifest_from_path(path)
    report = validate_extension_package(package_root)
    if not report.ok:
        raise ValueError("extension package failed validation")

    snapshot = _registry().snapshot()
    existing = snapshot.get_extension(manifest.id)
    if existing is None:
        raise KeyError(manifest.id)
    if _location_for_extension(existing) != "workspace" or not existing.root_path:
        raise ValueError(
            f"extension '{manifest.id}' is bundled; install the package to create a workspace override"
        )

    current_digest = _extension_package_digest(existing.root_path)
    candidate_digest = _hash_extension_directory(package_root)
    if current_digest == candidate_digest:
        raise ValueError(f"extension '{manifest.id}' is already up to date")

    _validate_extension_candidate_root(existing, package_root)

    target_root = Path(existing.root_path)
    with tempfile.TemporaryDirectory(prefix="seraph-extension-update-") as temp_root:
        staging_root = Path(temp_root) / "package"
        backup_root = Path(temp_root) / "backup"
        shutil.copytree(package_root, staging_root)
        shutil.move(target_root, backup_root)
        shutil.move(staging_root, target_root)
        try:
            _refresh_runtime()
            updated_extension = _registry().snapshot().get_extension(manifest.id)
            if updated_extension is None:
                raise ValueError(f"extension '{manifest.id}' did not load after update")
            _sync_mcp_servers_for_updated_extension(existing, updated_extension)
        except Exception:
            if target_root.exists():
                shutil.rmtree(target_root, ignore_errors=True)
            shutil.move(backup_root, target_root)
            _refresh_runtime()
            raise
    return get_extension(manifest.id)


def _set_enabled(extension_id: str, enabled: bool) -> dict[str, Any]:
    snapshot = _registry().snapshot()
    extension = snapshot.get_extension(extension_id)
    if extension is None:
        raise KeyError(extension_id)
    if enabled:
        doctor_report = doctor_snapshot(snapshot)
        doctor_result = next(
            (result for result in doctor_report.results if result.extension_id == extension_id),
            None,
        )
        extension_load_errors = _extension_load_errors_for_extension(extension, snapshot.load_errors)
        doctor_issues = list(doctor_result.issues if doctor_result is not None else [])
        if doctor_issues or extension_load_errors:
            raise ValueError(
                f"extension '{extension_id}' is degraded and cannot be enabled until validation issues are fixed"
            )
    mcp_definitions = _mcp_definitions_for_extension(extension)
    mcp_references_by_name = {
        definition.name: contribution.reference
        for contribution, definition in _mcp_definition_items_for_extension(extension)
    }
    targets = _toggle_targets(extension)
    if enabled:
        state_payload = _state_payload()
        state_by_id = state_payload.get("extensions")
        state_entry = (
            state_by_id.get(extension.id)
            if isinstance(state_by_id, dict) and isinstance(state_by_id.get(extension.id), dict)
            else {}
        )
        for target in targets:
            if target["type"] not in {
                "managed_connector",
                "memory_provider",
                "automation_trigger",
                "browser_provider",
                "messaging_connector",
                "node_adapter",
            }:
                continue
            contribution = next(
                (
                    item
                    for item in extension.contributions
                    if item.reference == target.get("reference")
                    and (
                        (target["type"] == "managed_connector" and item.contribution_type == "managed_connectors")
                        or (target["type"] == "memory_provider" and item.contribution_type == "memory_providers")
                        or (target["type"] == "automation_trigger" and item.contribution_type == "automation_triggers")
                        or (target["type"] == "browser_provider" and item.contribution_type == "browser_providers")
                        or (target["type"] == "messaging_connector" and item.contribution_type == "messaging_connectors")
                        or (target["type"] == "node_adapter" and item.contribution_type == "node_adapters")
                    )
                ),
                None,
            )
            if contribution is None:
                continue
            connector_name = _contribution_name(contribution)
            if not isinstance(connector_name, str) or not connector_name:
                raise ValueError(
                    f"connector '{contribution.reference}' in extension '{extension_id}' is not a valid {target['type'].replace('_', ' ')} definition"
                )
            target_enabled = _managed_connector_package_enable_target(extension, contribution)
            if not target_enabled:
                continue
            config_entry = _contribution_config(state_entry, contribution.contribution_type, connector_name)
            config_errors = _contribution_config_errors(contribution.metadata, config_entry)
            if contribution.metadata.get("config_fields") and (not config_entry or config_errors):
                raise ValueError(
                    f"{target['type'].replace('_', ' ')} '{connector_name}' requires valid configuration before enable"
                )
    changed: list[dict[str, Any]] = []
    state_payload: dict[str, Any] | None = None
    for target in targets:
        target_name = target["name"]
        ok = False
        if target["type"] == "skill":
            ok = skill_manager.enable(target_name) if enabled else skill_manager.disable(target_name)
        elif target["type"] == "workflow":
            ok = workflow_manager.enable(target_name) if enabled else workflow_manager.disable(target_name)
        elif target["type"] == "mcp_server":
            definition = mcp_definitions.get(target_name)
            extension_reference = mcp_references_by_name.get(target_name)
            ok = mcp_manager.update_server(
                target_name,
                enabled=enabled,
                extension_id=extension.id,
                extension_reference=extension_reference,
                extension_display_name=extension.display_name,
                source="extension",
            )
            if not ok and enabled:
                if definition is not None:
                    mcp_manager.add_server(
                        name=definition.name,
                        url=definition.url,
                        description=definition.description,
                        enabled=True,
                        headers=definition.headers,
                        auth_hint=definition.auth_hint,
                        extension_id=extension.id,
                        extension_reference=extension_reference,
                        extension_display_name=extension.display_name,
                        source="extension",
                    )
                    ok = True
        elif target["type"] in {
            "managed_connector",
            "memory_provider",
            "automation_trigger",
            "browser_provider",
            "messaging_connector",
            "node_adapter",
        }:
            contribution = next(
                (
                    item
                    for item in extension.contributions
                    if item.reference == target.get("reference")
                    and (
                        (target["type"] == "managed_connector" and item.contribution_type == "managed_connectors")
                        or (target["type"] == "memory_provider" and item.contribution_type == "memory_providers")
                        or (target["type"] == "automation_trigger" and item.contribution_type == "automation_triggers")
                        or (target["type"] == "browser_provider" and item.contribution_type == "browser_providers")
                        or (target["type"] == "messaging_connector" and item.contribution_type == "messaging_connectors")
                        or (target["type"] == "node_adapter" and item.contribution_type == "node_adapters")
                    )
                ),
                None,
            )
            if contribution is None:
                ok = False
            else:
                connector_name = _contribution_name(contribution)
                if not isinstance(connector_name, str) or not connector_name:
                    raise ValueError(
                        f"connector '{contribution.reference}' in extension '{extension_id}' is not a valid {target['type'].replace('_', ' ')} definition"
                    )
                if state_payload is None:
                    state_payload = _state_payload()
                state_by_id = state_payload.get("extensions")
                state_entry = (
                    state_by_id.get(extension.id)
                    if isinstance(state_by_id, dict) and isinstance(state_by_id.get(extension.id), dict)
                    else {}
                )
                target_enabled = enabled
                if enabled:
                    target_enabled = _managed_connector_package_enable_target(extension, contribution)
                config_entry = _contribution_config(state_entry, contribution.contribution_type, connector_name)
                config_errors = _contribution_config_errors(contribution.metadata, config_entry)
                if target_enabled and contribution.metadata.get("config_fields") and (not config_entry or config_errors):
                    raise ValueError(
                        f"{target['type'].replace('_', ' ')} '{connector_name}' requires valid configuration before enable"
                    )
                set_connector_enabled_override(
                    state_payload,
                    extension.id,
                    contribution.reference,
                    enabled=target_enabled,
                )
                ok = True
        elif target["type"] in {"observer_definition", "channel_adapter"}:
            contribution = next(
                (
                    item
                    for item in extension.contributions
                    if item.reference == target.get("reference")
                    and (
                        (target["type"] == "observer_definition" and item.contribution_type == "observer_definitions")
                        or (target["type"] == "channel_adapter" and item.contribution_type == "channel_adapters")
                    )
                ),
                None,
            )
            if contribution is None:
                ok = False
            else:
                if state_payload is None:
                    state_payload = _state_payload()
                set_connector_enabled_override(
                    state_payload,
                    extension.id,
                    contribution.reference,
                    enabled=enabled,
                )
                ok = True
        changed.append({
            "type": target["type"],
            "name": target_name,
            "enabled": target_enabled if target["type"] in {"managed_connector", "memory_provider", "automation_trigger", "browser_provider", "messaging_connector", "node_adapter"} and ok else enabled,
            "ok": ok,
        })
    if state_payload is not None:
        _save_state(state_payload)
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
    if not isinstance(config, dict):
        raise ValueError("extension config must be an object")
    configurable_types = {
        "managed_connectors",
        "memory_providers",
        "automation_triggers",
        "browser_providers",
        "messaging_connectors",
        "node_adapters",
    }
    known_config_names: dict[str, set[str]] = {contribution_type: set() for contribution_type in configurable_types}
    for contribution_type in configurable_types:
        type_config = config.get(contribution_type)
        if type_config is not None and not isinstance(type_config, dict):
            raise ValueError(f"{contribution_type} config must be an object keyed by contribution name")
    for contribution in extension.contributions:
        if contribution.contribution_type not in configurable_types:
            continue
        contribution_name = _contribution_name(contribution)
        if not contribution_name:
            continue
        known_config_names[contribution.contribution_type].add(contribution_name)
        type_config = config.get(contribution.contribution_type)
        contribution_config = type_config.get(contribution_name) if isinstance(type_config, dict) else None
        if contribution_config is None:
            continue
        if not isinstance(contribution_config, dict):
            raise ValueError(
                f"{contribution.contribution_type.removesuffix('s').replace('_', ' ')} '{contribution_name}' config must be an object"
            )
        config_errors = _contribution_config_errors(contribution.metadata, contribution_config)
        if config_errors:
            raise ValueError("; ".join(config_errors))
    for contribution_type, known_names in known_config_names.items():
        type_config = config.get(contribution_type)
        if not isinstance(type_config, dict):
            continue
        unknown_names = sorted(name for name in type_config.keys() if name not in known_names)
        if unknown_names:
            raise ValueError(
                f"unknown {contribution_type} config entries: " + ", ".join(unknown_names)
            )
    payload = _state_payload()
    extensions = payload.setdefault("extensions", {})
    entry = extensions.setdefault(extension_id, {})
    existing_config = entry.get("config")
    if not isinstance(existing_config, dict):
        existing_config = {}
    entry["config"] = _preserve_secret_placeholders(
        extension,
        incoming_config=config,
        existing_config=existing_config,
    )
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
