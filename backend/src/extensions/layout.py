"""Canonical on-disk layout rules for Seraph extension packages."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

MANIFEST_FILENAMES = ("manifest.yaml", "manifest.yml")

CONTRIBUTION_LAYOUTS: dict[str, tuple[str, ...]] = {
    "skills": ("skills/",),
    "workflows": ("workflows/",),
    "runbooks": ("runbooks/",),
    "starter_packs": ("starter-packs/",),
    "provider_presets": ("presets/provider/",),
    "toolset_presets": ("presets/toolset/",),
    "prompt_packs": ("prompts/",),
    "context_packs": ("context/",),
    "scheduled_routines": ("routines/",),
    "mcp_servers": ("mcp/",),
    "managed_connectors": ("connectors/managed/",),
    "automation_triggers": ("automation/",),
    "browser_providers": ("connectors/browser/",),
    "messaging_connectors": ("connectors/messaging/",),
    "observer_definitions": ("observers/definitions/",),
    "observer_connectors": ("observers/connectors/",),
    "channel_adapters": ("channels/",),
    "speech_profiles": ("speech/",),
    "node_adapters": ("connectors/nodes/",),
    "workspace_adapters": ("workspace/",),
}

_CONTRIBUTION_LAYOUT_PARTS = tuple(
    tuple(PurePosixPath(prefix.rstrip("/")).parts)
    for prefixes in CONTRIBUTION_LAYOUTS.values()
    for prefix in prefixes
)


def expected_layout_prefixes(contribution_type: str) -> tuple[str, ...]:
    prefixes = CONTRIBUTION_LAYOUTS.get(contribution_type)
    if prefixes is None:
        raise KeyError(f"unknown contribution type: {contribution_type}")
    return prefixes


def validate_contribution_layout(contribution_type: str, reference: str) -> str:
    prefixes = expected_layout_prefixes(contribution_type)
    if any(reference.startswith(prefix) for prefix in prefixes):
        return reference
    expected = " or ".join(prefixes)
    raise ValueError(f"must live under {expected}")


def resolve_package_reference(package_root: Path, reference: str) -> Path:
    resolved_root = package_root.resolve()
    resolved_reference = (package_root / reference).resolve()
    if resolved_root == resolved_reference or resolved_root in resolved_reference.parents:
        return resolved_reference
    raise ValueError("resolved contribution path escapes the package root")


def is_package_manifest_path(manifest_path: Path, search_root: Path) -> bool:
    if search_root.is_file():
        return True

    try:
        relative_parent = manifest_path.parent.resolve().relative_to(search_root.resolve()).parts
    except ValueError:
        return False

    for prefix_parts in _CONTRIBUTION_LAYOUT_PARTS:
        prefix_length = len(prefix_parts)
        if prefix_length == 0 or len(relative_parent) < prefix_length:
            continue
        for start in range(0, len(relative_parent) - prefix_length + 1):
            if relative_parent[start : start + prefix_length] == prefix_parts:
                return False
    return True


def iter_extension_manifest_paths(roots: list[str]) -> list[Path]:
    discovered: dict[str, Path] = {}
    for root in roots:
        if not root:
            continue
        root_path = Path(root)
        if not root_path.exists():
            continue
        if root_path.is_file() and root_path.name in MANIFEST_FILENAMES:
            discovered[str(root_path.resolve())] = root_path
            continue
        for manifest_name in MANIFEST_FILENAMES:
            for manifest_path in root_path.rglob(manifest_name):
                if not is_package_manifest_path(manifest_path, root_path):
                    continue
                discovered[str(manifest_path.resolve())] = manifest_path
    return [discovered[key] for key in sorted(discovered)]
