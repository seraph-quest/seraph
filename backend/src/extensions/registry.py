"""Extension registry that bridges manifest packages and current legacy sources."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
from importlib.metadata import PackageNotFoundError, version as package_version
import os
from pathlib import Path
import re
import tomllib
from typing import Any

from config.settings import settings
from src.extensions.capability_contributions import (
    load_automation_trigger_definition,
    load_browser_provider_definition,
    load_canvas_output_definition,
    load_context_pack_definition,
    load_messaging_connector_definition,
    load_node_adapter_definition,
    load_prompt_pack_definition,
    load_provider_preset_definition,
    load_speech_profile_definition,
    load_toolset_preset_definition,
    load_workflow_runtime_definition,
)
from src.extensions.channels import load_channel_adapter_definition
from src.extensions.connectors import load_managed_connector_definition, load_mcp_server_definition
from src.extensions.layout import iter_extension_manifest_paths, resolve_package_reference
from src.extensions.manifest import ExtensionManifest, ExtensionManifestError, load_extension_manifest
from src.extensions.observers import load_observer_definition
from src.skills.loader import parse_skill_content
from src.workflows.loader import parse_workflow_content
from src.skills.loader import scan_skills
from src.tools.mcp_manager import mcp_manager
from src.workflows.loader import scan_workflows

_MCP_RUNTIME_UNSET = object()


def _slugify(value: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return sanitized or "root"


def _legacy_extension_id(kind: str, source_path: str) -> str:
    fingerprint = hashlib.sha1(source_path.encode("utf-8")).hexdigest()[:10]
    return f"legacy.{kind}.{_slugify(source_path)}-{fingerprint}"


def bundled_manifest_root() -> str:
    return str(Path(__file__).resolve().parents[1] / "defaults" / "extensions")


def default_manifest_roots_for_workspace(workspace_dir: str | None = None) -> list[str]:
    workspace_root = workspace_dir or settings.workspace_dir
    roots = [os.path.join(workspace_root, "extensions"), bundled_manifest_root()]
    deduped: list[str] = []
    for root in roots:
        if root not in deduped:
            deduped.append(root)
    return deduped


def _default_manifest_roots() -> list[str]:
    return default_manifest_roots_for_workspace(settings.workspace_dir)


def _default_skill_dirs() -> list[str]:
    return [os.path.join(settings.workspace_dir, "skills")]


def _default_workflow_dirs() -> list[str]:
    return [os.path.join(settings.workspace_dir, "workflows")]


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


@dataclass(frozen=True)
class ExtensionContributionRecord:
    extension_id: str
    contribution_type: str
    reference: str
    source: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtensionRecord:
    id: str
    display_name: str
    kind: str
    trust: str
    source: str
    root_path: str | None
    manifest_path: str | None
    manifest: ExtensionManifest | None = None
    contributions: list[ExtensionContributionRecord] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtensionLoadErrorRecord:
    source: str
    message: str
    phase: str
    details: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ExtensionRegistrySnapshot:
    extensions: list[ExtensionRecord]
    load_errors: list[ExtensionLoadErrorRecord]

    def get_extension(self, extension_id: str) -> ExtensionRecord | None:
        matches = [item for item in self.extensions if item.id == extension_id]
        if not matches:
            return None
        return min(matches, key=_extension_priority)

    def list_contributions(self, contribution_type: str | None = None) -> list[ExtensionContributionRecord]:
        contributions = [
            contribution
            for extension in self.extensions
            for contribution in extension.contributions
        ]
        if contribution_type is None:
            return contributions
        return [item for item in contributions if item.contribution_type == contribution_type]


class ExtensionRegistry:
    """Enumerate manifest-backed extensions and current legacy capability sources."""

    def __init__(
        self,
        *,
        manifest_roots: list[str] | None = None,
        skill_dirs: list[str] | None = None,
        workflow_dirs: list[str] | None = None,
        mcp_runtime: Any = _MCP_RUNTIME_UNSET,
        seraph_version: str | None = None,
    ) -> None:
        self._manifest_roots = manifest_roots if manifest_roots is not None else _default_manifest_roots()
        self._skill_dirs = skill_dirs if skill_dirs is not None else _default_skill_dirs()
        self._workflow_dirs = workflow_dirs if workflow_dirs is not None else _default_workflow_dirs()
        self._mcp_runtime = mcp_manager if mcp_runtime is _MCP_RUNTIME_UNSET else mcp_runtime
        self._seraph_version = seraph_version or _current_seraph_version()

    def snapshot(self) -> ExtensionRegistrySnapshot:
        extensions: list[ExtensionRecord] = []
        load_errors: list[ExtensionLoadErrorRecord] = []

        manifest_extensions, manifest_errors = self._scan_manifest_extensions()
        manifest_extensions, manifest_conflict_errors = self._annotate_named_contribution_conflicts(
            manifest_extensions
        )
        manifest_extensions = self._enrich_workflow_contribution_metadata(manifest_extensions)
        extensions.extend(manifest_extensions)
        load_errors.extend(manifest_errors)
        load_errors.extend(manifest_conflict_errors)

        manifest_claims = self._manifest_claims(manifest_extensions)

        skill_extensions, skill_errors = self._scan_legacy_skill_extensions(manifest_claims)
        extensions.extend(skill_extensions)
        load_errors.extend(skill_errors)

        workflow_extensions, workflow_errors = self._scan_legacy_workflow_extensions(manifest_claims)
        extensions.extend(workflow_extensions)
        load_errors.extend(workflow_errors)

        mcp_extensions, mcp_errors = self._scan_legacy_mcp_extensions(manifest_claims)
        extensions.extend(mcp_extensions)
        load_errors.extend(mcp_errors)

        extensions.sort(key=lambda item: (item.source, item.display_name.lower(), item.id))
        return ExtensionRegistrySnapshot(extensions=extensions, load_errors=load_errors)

    def list_extensions(self) -> list[ExtensionRecord]:
        return self.snapshot().extensions

    def list_contributions(self, contribution_type: str | None = None) -> list[ExtensionContributionRecord]:
        return self.snapshot().list_contributions(contribution_type)

    def get_extension(self, extension_id: str) -> ExtensionRecord | None:
        return self.snapshot().get_extension(extension_id)

    def _iter_manifest_paths(self) -> list[Path]:
        return iter_extension_manifest_paths(self._manifest_roots)

    def _manifest_root_index(self, manifest_path: Path) -> int:
        resolved_manifest = manifest_path.resolve()
        for index, root in enumerate(self._manifest_roots):
            if not root:
                continue
            root_path = Path(root)
            if not root_path.exists():
                continue
            resolved_root = root_path.resolve()
            if resolved_root == resolved_manifest:
                return index
            if resolved_root in resolved_manifest.parents:
                return index
        return len(self._manifest_roots)

    def _scan_manifest_extensions(self) -> tuple[list[ExtensionRecord], list[ExtensionLoadErrorRecord]]:
        extensions: list[ExtensionRecord] = []
        errors: list[ExtensionLoadErrorRecord] = []
        for manifest_path in self._iter_manifest_paths():
            try:
                manifest = load_extension_manifest(manifest_path)
            except ExtensionManifestError as exc:
                errors.append(
                    ExtensionLoadErrorRecord(
                        source=str(manifest_path),
                        message=exc.message,
                        phase="manifest",
                        details=exc.errors,
                    )
                )
                continue
            if not manifest.is_compatible_with(self._seraph_version):
                errors.append(
                    ExtensionLoadErrorRecord(
                        source=str(manifest_path),
                        message=(
                            f"manifest requires Seraph {manifest.compatibility.seraph}, "
                            f"current runtime is {self._seraph_version}"
                        ),
                        phase="compatibility",
                        details=[{"contributed_types": sorted(manifest.contributed_types())}],
                    )
                )
                continue
            try:
                extensions.append(self._record_from_manifest(manifest, manifest_path))
            except ValueError as exc:
                errors.append(
                    ExtensionLoadErrorRecord(
                        source=str(manifest_path),
                        message=str(exc),
                        phase="layout",
                        details=[{"contributed_types": sorted(manifest.contributed_types())}],
                    )
                )
        return extensions, errors

    def _record_from_manifest(self, manifest: ExtensionManifest, manifest_path: Path) -> ExtensionRecord:
        root_path = str(manifest_path.parent)
        manifest_root_index = self._manifest_root_index(manifest_path)
        contributions: list[ExtensionContributionRecord] = []
        for contribution_type in sorted(manifest.contributed_types()):
            for reference in getattr(manifest.contributes, contribution_type):
                resolved_path = resolve_package_reference(manifest_path.parent, reference)
                metadata: dict[str, Any] = {
                    "resolved_path": str(resolved_path),
                    "manifest_root_index": manifest_root_index,
                    "trust": manifest.trust.value,
                }
                if contribution_type == "mcp_servers":
                    try:
                        metadata.update(load_mcp_server_definition(resolved_path).as_metadata())
                    except Exception:
                        pass
                if contribution_type == "skills":
                    try:
                        skill = parse_skill_content(
                            resolved_path.read_text(encoding="utf-8"),
                            path=str(resolved_path),
                            errors=[],
                        )
                        if skill is not None:
                            metadata.update(
                                {
                                    "name": skill.name,
                                    "description": skill.description,
                                    "requires_tools": list(skill.requires_tools),
                                    "user_invocable": skill.user_invocable,
                                    "default_enabled": skill.enabled,
                                }
                            )
                    except Exception:
                        pass
                if contribution_type == "workflows":
                    try:
                        workflow = parse_workflow_content(
                            resolved_path.read_text(encoding="utf-8"),
                            path=str(resolved_path),
                            errors=[],
                        )
                        if workflow is not None:
                                metadata.update(
                                    {
                                        "name": workflow.name,
                                        "description": workflow.description,
                                        "requires_tools": list(workflow.requires_tools),
                                        "requires_skills": list(workflow.requires_skills),
                                        "step_tools": list(workflow.step_tools),
                                        "runtime_profile": workflow.runtime_profile,
                                        "output_surface": workflow.output_surface,
                                        "user_invocable": workflow.user_invocable,
                                        "default_enabled": workflow.enabled,
                                        "tool_name": workflow.tool_name,
                                    }
                                )
                    except Exception:
                        pass
                if contribution_type == "managed_connectors":
                    try:
                        metadata.update(load_managed_connector_definition(resolved_path).as_metadata())
                    except Exception:
                        pass
                if contribution_type == "toolset_presets":
                    try:
                        metadata.update(load_toolset_preset_definition(resolved_path).as_metadata())
                    except Exception:
                        pass
                if contribution_type == "context_packs":
                    try:
                        metadata.update(load_context_pack_definition(resolved_path).as_metadata())
                    except Exception:
                        pass
                if contribution_type == "prompt_packs":
                    try:
                        metadata.update(load_prompt_pack_definition(resolved_path).as_metadata())
                    except Exception:
                        pass
                if contribution_type == "automation_triggers":
                    try:
                        metadata.update(load_automation_trigger_definition(resolved_path).as_metadata())
                    except Exception:
                        pass
                if contribution_type == "browser_providers":
                    try:
                        metadata.update(load_browser_provider_definition(resolved_path).as_metadata())
                    except Exception:
                        pass
                if contribution_type == "messaging_connectors":
                    try:
                        metadata.update(load_messaging_connector_definition(resolved_path).as_metadata())
                    except Exception:
                        pass
                if contribution_type == "observer_definitions":
                    try:
                        metadata.update(load_observer_definition(resolved_path).as_metadata())
                    except Exception:
                        pass
                if contribution_type == "channel_adapters":
                    try:
                        metadata.update(load_channel_adapter_definition(resolved_path).as_metadata())
                    except Exception:
                        pass
                if contribution_type == "canvas_outputs":
                    try:
                        metadata.update(load_canvas_output_definition(resolved_path).as_metadata())
                    except Exception:
                        pass
                if contribution_type == "workflow_runtimes":
                    try:
                        metadata.update(load_workflow_runtime_definition(resolved_path).as_metadata())
                    except Exception:
                        pass
                if contribution_type == "speech_profiles":
                    try:
                        metadata.update(load_speech_profile_definition(resolved_path).as_metadata())
                    except Exception:
                        pass
                if contribution_type == "provider_presets":
                    try:
                        metadata.update(load_provider_preset_definition(resolved_path).as_metadata())
                    except Exception:
                        pass
                if contribution_type == "node_adapters":
                    try:
                        metadata.update(load_node_adapter_definition(resolved_path).as_metadata())
                    except Exception:
                        pass
                contributions.append(
                    ExtensionContributionRecord(
                        extension_id=manifest.id,
                        contribution_type=contribution_type,
                        reference=reference,
                        source="manifest",
                        metadata=metadata,
                    )
                )
        return ExtensionRecord(
            id=manifest.id,
            display_name=manifest.display_name,
            kind=manifest.kind.value,
            trust=manifest.trust.value,
            source="manifest",
            root_path=root_path,
            manifest_path=str(manifest_path),
            manifest=manifest,
            contributions=contributions,
            metadata={
                "publisher": manifest.publisher.name,
                "version": manifest.version,
                "compatibility": manifest.compatibility.seraph,
                "manifest_root_index": manifest_root_index,
            },
        )

    def _manifest_claims(self, manifest_extensions: list[ExtensionRecord]) -> dict[str, set[str]]:
        claims: dict[str, set[str]] = {}
        for extension in manifest_extensions:
            for contribution in extension.contributions:
                claim_bucket = claims.setdefault(contribution.contribution_type, set())
                resolved_path = contribution.metadata.get("resolved_path")
                if isinstance(resolved_path, str) and resolved_path:
                    claim_bucket.add(os.path.abspath(resolved_path))
                if contribution.contribution_type == "mcp_servers":
                    connector_name = contribution.metadata.get("name")
                    if isinstance(connector_name, str) and connector_name:
                        claims.setdefault("mcp_server_names", set()).add(connector_name)
        return claims

    def _enrich_workflow_contribution_metadata(
        self,
        extensions: list[ExtensionRecord],
    ) -> list[ExtensionRecord]:
        runtime_defaults_by_name: dict[str, str] = {}
        canvas_metadata_by_name: dict[str, dict[str, Any]] = {}
        for extension in extensions:
            for contribution in extension.contributions:
                if isinstance(contribution.metadata.get("registry_conflict"), dict):
                    continue
                if contribution.contribution_type == "workflow_runtimes":
                    name = contribution.metadata.get("name")
                    default_output_surface = contribution.metadata.get("default_output_surface")
                    if (
                        isinstance(name, str)
                        and name.strip()
                        and isinstance(default_output_surface, str)
                        and default_output_surface.strip()
                    ):
                        runtime_defaults_by_name[name.strip()] = default_output_surface.strip()
                elif contribution.contribution_type == "canvas_outputs":
                    name = contribution.metadata.get("name")
                    if not isinstance(name, str) or not name.strip():
                        continue
                    raw_sections = contribution.metadata.get("sections")
                    raw_artifact_types = contribution.metadata.get("artifact_types")
                    canvas_metadata_by_name[name.strip()] = {
                        "title": str(contribution.metadata.get("title") or ""),
                        "sections": [
                            str(item).strip()
                            for item in raw_sections
                            if isinstance(item, str) and item.strip()
                        ] if isinstance(raw_sections, list) else [],
                        "artifact_types": [
                            str(item).strip()
                            for item in raw_artifact_types
                            if isinstance(item, str) and item.strip()
                        ] if isinstance(raw_artifact_types, list) else [],
                    }

        updated_extensions: list[ExtensionRecord] = []
        for extension in extensions:
            updated_contributions: list[ExtensionContributionRecord] = []
            for contribution in extension.contributions:
                if contribution.contribution_type != "workflows":
                    updated_contributions.append(contribution)
                    continue
                metadata = dict(contribution.metadata)
                runtime_profile = metadata.get("runtime_profile")
                declared_output_surface = (
                    str(metadata.get("output_surface") or "").strip()
                    if metadata.get("output_surface") is not None
                    else ""
                )
                effective_output_surface = declared_output_surface
                if (
                    not effective_output_surface
                    and isinstance(runtime_profile, str)
                    and runtime_profile.strip()
                ):
                    effective_output_surface = runtime_defaults_by_name.get(runtime_profile.strip(), "")
                if effective_output_surface:
                    metadata["output_surface"] = effective_output_surface
                    metadata["effective_output_surface"] = effective_output_surface
                    if declared_output_surface:
                        metadata["declared_output_surface"] = declared_output_surface
                    canvas_metadata = canvas_metadata_by_name.get(effective_output_surface, {})
                    if canvas_metadata:
                        metadata["output_surface_title"] = str(canvas_metadata.get("title") or "")
                        metadata["output_surface_sections"] = list(canvas_metadata.get("sections") or [])
                        metadata["output_surface_artifact_types"] = list(canvas_metadata.get("artifact_types") or [])
                updated_contributions.append(replace(contribution, metadata=metadata))
            updated_extensions.append(replace(extension, contributions=updated_contributions))
        return updated_extensions

    def _annotate_named_contribution_conflicts(
        self,
        extensions: list[ExtensionRecord],
    ) -> tuple[list[ExtensionRecord], list[ExtensionLoadErrorRecord]]:
        unique_name_types = {
            "automation_triggers": {"label": "automation trigger", "conflict_mode": "all"},
            "browser_providers": {"label": "browser provider", "conflict_mode": "winner"},
            "workflow_runtimes": {"label": "workflow runtime", "conflict_mode": "winner"},
            "canvas_outputs": {"label": "canvas output", "conflict_mode": "winner"},
        }
        grouped: dict[tuple[str, str], list[tuple[ExtensionRecord, ExtensionContributionRecord]]] = {}
        prioritized_extensions = sorted(extensions, key=_extension_priority)
        for extension in prioritized_extensions:
            for contribution in extension.contributions:
                policy = unique_name_types.get(contribution.contribution_type)
                if policy is None:
                    continue
                name = contribution.metadata.get("name")
                if not isinstance(name, str) or not name.strip():
                    continue
                key = (contribution.contribution_type, name.strip().casefold())
                grouped.setdefault(key, []).append((extension, contribution))

        updated_extensions: list[ExtensionRecord] = []
        load_errors: list[ExtensionLoadErrorRecord] = []
        for extension in extensions:
            updated_contributions: list[ExtensionContributionRecord] = []
            for contribution in extension.contributions:
                policy = unique_name_types.get(contribution.contribution_type)
                if policy is None:
                    updated_contributions.append(contribution)
                    continue
                name = contribution.metadata.get("name")
                if not isinstance(name, str) or not name.strip():
                    updated_contributions.append(contribution)
                    continue
                duplicates = grouped.get((contribution.contribution_type, name.strip().casefold()), [])
                if len(duplicates) <= 1:
                    updated_contributions.append(contribution)
                    continue
                label = str(policy["label"])
                conflict_mode = str(policy["conflict_mode"])
                winner_extension, winner_contribution = duplicates[0]
                if (
                    conflict_mode == "winner"
                    and (winner_extension.id, winner_contribution.reference)
                    == (extension.id, contribution.reference)
                ):
                    updated_contributions.append(contribution)
                    continue
                conflicting_peers = [
                    {
                        "extension_id": peer_extension.id,
                        "reference": peer_contribution.reference,
                        "display_name": peer_extension.display_name,
                    }
                    for peer_extension, peer_contribution in duplicates
                    if (peer_extension.id, peer_contribution.reference)
                    != (extension.id, contribution.reference)
                ]
                metadata = dict(contribution.metadata)
                metadata["registry_conflict"] = {
                    "kind": "duplicate_name",
                    "name": name.strip(),
                    "winner_extension_id": winner_extension.id,
                    "winner_reference": winner_contribution.reference,
                    "winner_display_name": winner_extension.display_name,
                    "conflict_mode": conflict_mode,
                    "conflicting_peers": conflicting_peers,
                }
                updated_contributions.append(replace(contribution, metadata=metadata))
                if conflict_mode == "all":
                    message = (
                        f"Duplicate {label} name '{name.strip()}' in {contribution.reference}; "
                        "all matching definitions are disabled until the collision is removed"
                    )
                else:
                    message = (
                        f"Duplicate {label} name '{name.strip()}' in {contribution.reference}; "
                        f"keeping {winner_extension.display_name} ({winner_contribution.reference})"
                    )
                load_errors.append(
                    ExtensionLoadErrorRecord(
                        source=str(extension.manifest_path or metadata.get("resolved_path") or contribution.reference),
                        message=message,
                        phase=f"duplicate-{contribution.contribution_type.removesuffix('s')}-name",
                        details=[
                            {
                                "name": name.strip(),
                                "winner_extension_id": winner_extension.id,
                                "winner_reference": winner_contribution.reference,
                                "conflict_mode": conflict_mode,
                            }
                        ],
                    )
                )
            updated_extensions.append(replace(extension, contributions=updated_contributions))
        return updated_extensions, load_errors

    def _scan_legacy_skill_extensions(
        self,
        manifest_claims: dict[str, set[str]],
    ) -> tuple[list[ExtensionRecord], list[ExtensionLoadErrorRecord]]:
        records: list[ExtensionRecord] = []
        load_errors: list[ExtensionLoadErrorRecord] = []
        claimed_skills = manifest_claims.get("skills", set())
        for skills_dir in self._skill_dirs:
            if not skills_dir:
                continue
            skills, errors = scan_skills(skills_dir)
            if not skills and not errors:
                continue
            for error in errors:
                load_errors.append(
                    ExtensionLoadErrorRecord(
                        source=str(error.get("file_path") or skills_dir),
                        message=str(error.get("message") or "legacy skill load error"),
                        phase="legacy-skills",
                    )
                )
            extension_id = _legacy_extension_id("skills", skills_dir)
            contributions: list[ExtensionContributionRecord] = []
            for skill in skills:
                if os.path.abspath(skill.file_path) in claimed_skills:
                    continue
                contributions.append(
                    ExtensionContributionRecord(
                        extension_id=extension_id,
                        contribution_type="skills",
                        reference=skill.file_path,
                        source="legacy",
                        metadata={
                            "name": skill.name,
                            "user_invocable": skill.user_invocable,
                            "enabled": skill.enabled,
                            "requires_tools": list(skill.requires_tools),
                        },
                    )
                )
            if not contributions:
                continue
            records.append(
                ExtensionRecord(
                    id=extension_id,
                    display_name=f"Legacy skills ({Path(skills_dir).name})",
                    kind="capability-pack",
                    trust="local",
                    source="legacy",
                    root_path=skills_dir,
                    manifest_path=None,
                    manifest=None,
                    contributions=contributions,
                    metadata={"load_errors": errors},
                )
            )
        return records, load_errors

    def _scan_legacy_workflow_extensions(
        self,
        manifest_claims: dict[str, set[str]],
    ) -> tuple[list[ExtensionRecord], list[ExtensionLoadErrorRecord]]:
        records: list[ExtensionRecord] = []
        load_errors: list[ExtensionLoadErrorRecord] = []
        claimed_workflows = manifest_claims.get("workflows", set())
        for workflows_dir in self._workflow_dirs:
            if not workflows_dir:
                continue
            workflows, errors = scan_workflows(workflows_dir)
            if not workflows and not errors:
                continue
            for error in errors:
                load_errors.append(
                    ExtensionLoadErrorRecord(
                        source=str(error.get("file_path") or workflows_dir),
                        message=str(error.get("message") or "legacy workflow load error"),
                        phase="legacy-workflows",
                    )
                )
            extension_id = _legacy_extension_id("workflows", workflows_dir)
            contributions: list[ExtensionContributionRecord] = []
            for workflow in workflows:
                if os.path.abspath(workflow.file_path) in claimed_workflows:
                    continue
                contributions.append(
                    ExtensionContributionRecord(
                        extension_id=extension_id,
                        contribution_type="workflows",
                        reference=workflow.file_path,
                        source="legacy",
                        metadata={
                            "name": workflow.name,
                            "tool_name": workflow.tool_name,
                            "enabled": workflow.enabled,
                            "requires_tools": list(workflow.requires_tools),
                            "requires_skills": list(workflow.requires_skills),
                        },
                    )
                )
            if not contributions:
                continue
            records.append(
                ExtensionRecord(
                    id=extension_id,
                    display_name=f"Legacy workflows ({Path(workflows_dir).name})",
                    kind="capability-pack",
                    trust="local",
                    source="legacy",
                    root_path=workflows_dir,
                    manifest_path=None,
                    manifest=None,
                    contributions=contributions,
                    metadata={"load_errors": errors},
                )
            )
        return records, load_errors

    def _scan_legacy_mcp_extensions(
        self,
        manifest_claims: dict[str, set[str]],
    ) -> tuple[list[ExtensionRecord], list[ExtensionLoadErrorRecord]]:
        runtime = self._mcp_runtime
        if runtime is None:
            return [], []

        config_entries: list[dict[str, Any]]
        try:
            config_entries = runtime.get_config()
        except Exception as exc:
            config_path = getattr(runtime, "_config_path", None)
            return [], [
                ExtensionLoadErrorRecord(
                    source=str(config_path or "mcp-runtime"),
                    message=f"failed to inspect legacy MCP runtime: {exc}",
                    phase="legacy-mcp",
                )
            ]

        if not config_entries:
            return [], []

        config_path = getattr(runtime, "_config_path", None)
        extension_id = _legacy_extension_id("mcp-runtime", config_path or "runtime")
        claimed_servers = manifest_claims.get("mcp_servers", set())
        claimed_server_names = manifest_claims.get("mcp_server_names", set())
        contributions = []
        for entry in config_entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                continue
            if entry["name"] in claimed_server_names or entry["name"] in claimed_servers:
                continue
            contributions.append(
                ExtensionContributionRecord(
                    extension_id=extension_id,
                    contribution_type="mcp_servers",
                    reference=str(entry.get("name") or ""),
                    source="legacy",
                    metadata={
                        "url": entry.get("url"),
                        "enabled": entry.get("enabled"),
                        "connected": entry.get("connected"),
                        "status": entry.get("status"),
                        "tool_count": entry.get("tool_count"),
                    },
                )
            )
        if not contributions:
            return [], []
        return ([
            ExtensionRecord(
                id=extension_id,
                display_name="Legacy MCP runtime config",
                kind="connector-pack",
                trust="local",
                source="legacy",
                root_path=str(config_path) if config_path else None,
                manifest_path=None,
                manifest=None,
                contributions=contributions,
                metadata={"server_count": len(contributions)},
            )
        ], [])


extension_registry = ExtensionRegistry()


def _extension_priority(extension: ExtensionRecord) -> tuple[int, int, str]:
    if extension.source == "manifest":
        root_index = extension.metadata.get("manifest_root_index")
        if isinstance(root_index, int):
            return (0, root_index, extension.display_name.lower())
    workspace_root = Path(settings.workspace_dir).resolve() / "extensions"
    bundled_root = Path(bundled_manifest_root()).resolve()
    root_path = Path(extension.root_path).resolve() if extension.root_path else None
    if root_path is not None and (root_path == workspace_root or workspace_root in root_path.parents):
        return (1, 0, extension.display_name.lower())
    if root_path is not None and (root_path == bundled_root or bundled_root in root_path.parents):
        return (2, 0, extension.display_name.lower())
    if extension.source == "manifest":
        return (3, 0, extension.display_name.lower())
    return (4, 0, extension.display_name.lower())
