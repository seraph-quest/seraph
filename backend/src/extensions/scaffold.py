"""Scaffolding helpers for new extension packages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import re
from typing import Callable

import yaml

from .doctor import ExtensionDoctorReport, doctor_snapshot
from .layout import CONTRIBUTION_LAYOUTS
from .manifest import ExtensionKind, ExtensionTrust
from .registry import ExtensionRegistry

_DEFAULT_CAPABILITY_CONTRIBUTIONS: tuple[str, ...] = ("skills",)
_DEFAULT_CONNECTOR_CONTRIBUTIONS: tuple[str, ...] = ("managed_connectors",)

_DEFAULT_TOOL_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "skills": (),
    "workflows": ("read_file",),
    "toolset_presets": ("read_file",),
}

_NETWORKED_SCAFFOLD_CONTRIBUTIONS = {
    "managed_connectors",
    "automation_triggers",
    "browser_providers",
    "messaging_connectors",
}

_CONNECTOR_SCAFFOLD_CONTRIBUTIONS = {
    "managed_connectors",
    "automation_triggers",
    "browser_providers",
    "messaging_connectors",
    "node_adapters",
}

_SUPPORTED_SCAFFOLD_CONTRIBUTIONS = {
    "skills",
    "workflows",
    "runbooks",
    "starter_packs",
    "provider_presets",
    "toolset_presets",
    "prompt_packs",
    "context_packs",
    "scheduled_routines",
    "managed_connectors",
    "automation_triggers",
    "browser_providers",
    "messaging_connectors",
    "speech_profiles",
    "node_adapters",
}


@dataclass(frozen=True)
class ScaffoldedExtensionPackage:
    package_root: Path
    manifest_path: Path
    created_files: list[Path]
    contributions: list[str]


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "example"


def _default_version() -> str:
    today = date.today()
    return f"{today.year}.{today.month}.{today.day}"


def _placeholder_skill(slug: str, display_name: str) -> str:
    return (
        "---\n"
        f"name: {slug}\n"
        f"description: {json.dumps(f'{display_name} skill')}\n"
        "requires:\n"
        "  tools: []\n"
        "user_invocable: true\n"
        "---\n\n"
        f"Describe when to use the {display_name} skill here.\n"
    )


def _placeholder_workflow(slug: str, display_name: str) -> str:
    return (
        "---\n"
        f"name: {slug}\n"
        f"description: {json.dumps(f'{display_name} workflow')}\n"
        "requires:\n"
        "  tools: [read_file]\n"
        "inputs:\n"
        "  file_path:\n"
        "    type: string\n"
        "    description: File to inspect\n"
        "steps:\n"
        "  - id: inspect_file\n"
        "    tool: read_file\n"
        "    arguments:\n"
        '      file_path: "{{ file_path }}"\n'
        "---\n\n"
        f"Use the {display_name} workflow as a starting point.\n"
    )


def _placeholder_yaml(payload: dict[str, object]) -> str:
    return yaml.safe_dump(payload, sort_keys=False)


def _placeholder_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, indent=2) + "\n"


def _placeholder_prompt(slug: str, display_name: str) -> str:
    return f"# {display_name}\n\nDescribe the {slug} prompt pack here.\n"


_PLACEHOLDER_BUILDERS: dict[str, tuple[str, Callable[[str, str], str]]] = {
    "skills": ("skills/{slug}.md", _placeholder_skill),
    "workflows": ("workflows/{slug}.md", _placeholder_workflow),
    "runbooks": (
        "runbooks/{slug}.yaml",
        lambda slug, display_name: _placeholder_yaml(
            {
                "title": display_name,
                "summary": "Describe this runbook.",
                "workflow": slug,
            }
        ),
    ),
    "starter_packs": (
        "starter-packs/{slug}.json",
        lambda slug, display_name: _placeholder_json(
            {
                "name": slug,
                "label": display_name,
                "description": "Describe this starter pack.",
            }
        ),
    ),
    "provider_presets": (
        "presets/provider/{slug}.yaml",
        lambda slug, display_name: _placeholder_yaml(
            {
                "name": slug,
                "label": display_name,
                "default_model": "openrouter/x-ai/grok-4.1-fast",
            }
        ),
    ),
    "toolset_presets": (
        "presets/toolset/{slug}.yaml",
        lambda slug, display_name: _placeholder_yaml(
            {
                "name": slug,
                "description": f"{display_name} toolset preset",
                "mode": "balanced",
                "include_tools": ["read_file"],
                "capabilities": ["analysis"],
            }
        ),
    ),
    "prompt_packs": ("prompts/{slug}.md", _placeholder_prompt),
    "context_packs": (
        "context/{slug}.yaml",
        lambda slug, display_name: _placeholder_yaml(
            {
                "name": slug,
                "description": f"{display_name} context pack",
                "instructions": f"Describe how {display_name} should shape guardian context.",
                "domains": [slug],
            }
        ),
    ),
    "scheduled_routines": (
        "routines/{slug}.yaml",
        lambda slug, display_name: _placeholder_yaml(
            {
                "name": slug,
                "label": display_name,
                "schedule": "0 9 * * 1-5",
            }
        ),
    ),
    "managed_connectors": (
        "connectors/managed/{slug}.yaml",
        lambda slug, display_name: _placeholder_yaml(
            {
                "name": slug,
                "provider": slug,
                "description": f"{display_name} managed connector",
                "auth_kind": "api_key",
                "config_fields": [
                    {"key": "api_base_url", "label": "API Base URL", "required": False, "input": "url"},
                ],
            }
        ),
    ),
    "automation_triggers": (
        "automation/{slug}.yaml",
        lambda slug, display_name: _placeholder_yaml(
            {
                "name": slug,
                "description": f"{display_name} automation trigger",
                "trigger_type": "cron",
                "schedule": "0 9 * * 1-5",
            }
        ),
    ),
    "browser_providers": (
        "connectors/browser/{slug}.yaml",
        lambda slug, display_name: _placeholder_yaml(
            {
                "name": slug,
                "description": f"{display_name} browser provider",
                "provider_kind": "remote_cdp",
                "config_fields": [
                    {"key": "ws_endpoint", "label": "CDP Endpoint", "input": "url"},
                ],
            }
        ),
    ),
    "messaging_connectors": (
        "connectors/messaging/{slug}.yaml",
        lambda slug, display_name: _placeholder_yaml(
            {
                "name": slug,
                "description": f"{display_name} messaging connector",
                "platform": "telegram",
                "delivery_modes": ["dm"],
                "config_fields": [
                    {"key": "bot_token", "label": "Bot Token", "input": "password"},
                ],
            }
        ),
    ),
    "speech_profiles": (
        "speech/{slug}.yaml",
        lambda slug, display_name: _placeholder_yaml(
            {
                "name": slug,
                "description": f"{display_name} speech profile",
                "provider": "openai",
                "voice": "alloy",
                "supports_tts": True,
            }
        ),
    ),
    "node_adapters": (
        "connectors/nodes/{slug}.yaml",
        lambda slug, display_name: _placeholder_yaml(
            {
                "name": slug,
                "description": f"{display_name} node adapter",
                "adapter_kind": "companion",
                "config_fields": [
                    {"key": "node_url", "label": "Node URL", "input": "url"},
                ],
            }
        ),
    ),
}


def scaffold_extension_package(
    package_root: str | Path,
    *,
    extension_id: str,
    display_name: str,
    kind: str = "capability-pack",
    trust: str = "local",
    contributions: list[str] | None = None,
    version: str | None = None,
    seraph_compatibility: str = ">=2026.3.19",
    publisher_name: str = "Seraph",
) -> ScaffoldedExtensionPackage:
    package_path = Path(package_root)
    parsed_kind = ExtensionKind(kind)
    ExtensionTrust(trust)

    default_contributions = (
        _DEFAULT_CAPABILITY_CONTRIBUTIONS
        if parsed_kind == ExtensionKind.CAPABILITY_PACK
        else _DEFAULT_CONNECTOR_CONTRIBUTIONS
    )
    selected_contributions = list(contributions or default_contributions)
    for contribution_type in selected_contributions:
        if contribution_type not in _SUPPORTED_SCAFFOLD_CONTRIBUTIONS:
            raise ValueError(f"unsupported scaffold contribution type: {contribution_type}")
    if parsed_kind == ExtensionKind.CONNECTOR_PACK and not any(
        contribution_type in _CONNECTOR_SCAFFOLD_CONTRIBUTIONS for contribution_type in selected_contributions
    ):
        supported = ", ".join(sorted(_CONNECTOR_SCAFFOLD_CONTRIBUTIONS))
        raise ValueError(f"connector-pack scaffolds must include at least one connector contribution: {supported}")

    package_path.mkdir(parents=True, exist_ok=True)
    manifest_path = package_path / "manifest.yaml"
    if manifest_path.exists():
        raise FileExistsError(f"manifest already exists: {manifest_path}")

    slug = _slugify(extension_id.split(".")[-1])
    created_files: list[Path] = []
    manifest_contributions: dict[str, list[str]] = {}
    required_tools: list[str] = []

    for contribution_type in selected_contributions:
        template_path, builder = _PLACEHOLDER_BUILDERS[contribution_type]
        reference = template_path.format(slug=slug)
        target_path = package_path / reference
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if not target_path.exists():
            target_path.write_text(builder(slug, display_name), encoding="utf-8")
            created_files.append(target_path)
        manifest_contributions[contribution_type] = [reference]
        for tool_name in _DEFAULT_TOOL_PERMISSIONS.get(contribution_type, ()):
            if tool_name not in required_tools:
                required_tools.append(tool_name)

    manifest_payload = {
        "id": extension_id,
        "version": version or _default_version(),
        "display_name": display_name,
        "kind": parsed_kind.value,
        "compatibility": {"seraph": seraph_compatibility},
        "publisher": {"name": publisher_name},
        "trust": trust,
        "contributes": manifest_contributions,
        "permissions": {
            "tools": required_tools,
            "network": any(
                contribution_type in _NETWORKED_SCAFFOLD_CONTRIBUTIONS
                for contribution_type in selected_contributions
            ),
        },
    }
    manifest_path.write_text(yaml.safe_dump(manifest_payload, sort_keys=False), encoding="utf-8")
    created_files.append(manifest_path)

    return ScaffoldedExtensionPackage(
        package_root=package_path,
        manifest_path=manifest_path,
        created_files=created_files,
        contributions=selected_contributions,
    )


def validate_extension_package(
    package_root: str | Path,
    *,
    seraph_version: str | None = None,
) -> ExtensionDoctorReport:
    snapshot_registry = ExtensionRegistry(
        manifest_roots=[str(package_root)],
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
        seraph_version=seraph_version,
    )
    snapshot = snapshot_registry.snapshot()
    if not snapshot.extensions and not snapshot.load_errors:
        raise ValueError(f"no extension manifest found under {package_root}")

    return doctor_snapshot(snapshot)
