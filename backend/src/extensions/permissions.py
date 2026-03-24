"""Derived permission helpers for manifest-backed extensions."""

from __future__ import annotations

from typing import Any

from src.extensions.registry import ExtensionRecord
from src.native_tools.registry import canonical_tool_name
from src.tools.policy import (
    get_tool_execution_boundaries,
    get_tool_risk_level,
    tool_accepts_secret_refs,
)

NETWORK_BOUNDARIES = {"external_read", "external_mcp"}
LIFECYCLE_APPROVAL_BOUNDARIES = {
    "workspace_write",
    "sandbox_execution",
    "secret_management",
    "secret_read",
    "secret_injection",
    "external_mcp",
}


def _dedupe_strings(values: list[str] | tuple[str, ...] | set[str] | None) -> list[str]:
    if not values:
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        item = value.strip()
        if not item or item in seen:
            continue
        normalized.append(item)
        seen.add(item)
    return normalized


def _canonicalize_tools(tool_names: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for tool_name in tool_names:
        canonical_name = canonical_tool_name(tool_name)
        if not canonical_name or canonical_name in seen:
            continue
        normalized.append(canonical_name)
        seen.add(canonical_name)
    return normalized


def _risk_rank(risk_level: str) -> int:
    if risk_level == "high":
        return 3
    if risk_level == "medium":
        return 2
    return 1


def _max_risk_level(levels: list[str]) -> str:
    highest = "low"
    for level in levels:
        if _risk_rank(level) > _risk_rank(highest):
            highest = level
    return highest


def _tool_boundaries(tool_names: list[str]) -> list[str]:
    boundaries: list[str] = []
    for tool_name in tool_names:
        if not isinstance(tool_name, str) or not tool_name.strip():
            continue
        canonical_name = canonical_tool_name(tool_name)
        is_mcp = canonical_name.startswith("mcp_")
        for boundary in get_tool_execution_boundaries(canonical_name, is_mcp=is_mcp):
            if boundary not in boundaries:
                boundaries.append(boundary)
    return boundaries


def _tool_risk_level(tool_names: list[str]) -> str:
    levels: list[str] = []
    for tool_name in tool_names:
        if not isinstance(tool_name, str) or not tool_name.strip():
            continue
        canonical_name = canonical_tool_name(tool_name)
        levels.append(get_tool_risk_level(canonical_name, is_mcp=canonical_name.startswith("mcp_")))
    return _max_risk_level(levels)


def _tools_accept_secret_refs(tool_names: list[str]) -> bool:
    for tool_name in tool_names:
        if not isinstance(tool_name, str) or not tool_name.strip():
            continue
        canonical_name = canonical_tool_name(tool_name)
        if tool_accepts_secret_refs(canonical_name, is_mcp=canonical_name.startswith("mcp_")):
            return True
    return False


def _password_config_fields_present(metadata: dict[str, Any]) -> bool:
    config_fields = metadata.get("config_fields")
    if not isinstance(config_fields, list):
        return False
    for field in config_fields:
        if not isinstance(field, dict):
            continue
        if str(field.get("input") or "") == "password":
            return True
    return False


def _runtime_approval_behavior(*, boundaries: list[str], risk_level: str) -> tuple[str, bool]:
    if "external_mcp" in boundaries:
        return "mcp_policy", True
    if risk_level == "high":
        return "high_risk", True
    return "never", False


def _merge_explicit_boundaries_into_profile(
    profile: dict[str, Any],
    *,
    explicit_boundaries: list[str],
) -> dict[str, Any]:
    required_boundaries = _dedupe_strings(
        list(profile.get("required_execution_boundaries", [])) + explicit_boundaries
    )
    declared_boundaries = _dedupe_strings(profile.get("declared_execution_boundaries"))
    missing_boundaries = [
        boundary
        for boundary in required_boundaries
        if declared_boundaries and boundary not in declared_boundaries
    ]
    requires_network = bool(profile.get("requires_network")) or any(
        boundary in NETWORK_BOUNDARIES for boundary in required_boundaries
    )
    network_declared = profile.get("network")
    missing_network = bool(profile.get("missing_network")) or bool(
        network_declared is False and requires_network
    )
    lifecycle_boundaries = [
        boundary
        for boundary in required_boundaries
        if boundary in LIFECYCLE_APPROVAL_BOUNDARIES
    ]
    risk_level = str(profile.get("risk_level") or "low")
    if lifecycle_boundaries and _risk_rank(risk_level) < _risk_rank("high"):
        risk_level = "high"
    approval_behavior, runtime_requires_approval = _runtime_approval_behavior(
        boundaries=required_boundaries,
        risk_level=risk_level,
    )
    requires_approval = runtime_requires_approval or bool(lifecycle_boundaries)
    if not runtime_requires_approval and lifecycle_boundaries:
        approval_behavior = "lifecycle"
    profile.update(
        {
            "required_execution_boundaries": required_boundaries,
            "missing_execution_boundaries": missing_boundaries,
            "requires_network": requires_network,
            "missing_network": missing_network,
            "risk_level": risk_level,
            "approval_behavior": approval_behavior,
            "requires_approval": requires_approval,
            "lifecycle_approval_boundaries": lifecycle_boundaries,
        }
    )
    profile["ok"] = not profile["missing_tools"] and not missing_boundaries and not missing_network
    profile["status"] = "granted" if profile["ok"] else "insufficient"
    return profile


def evaluate_tool_permissions(
    extension: ExtensionRecord | None,
    *,
    tool_names: list[str],
) -> dict[str, Any]:
    required_tools = _canonicalize_tools(_dedupe_strings(tool_names))
    required_boundaries = _tool_boundaries(required_tools)
    requires_network = any(boundary in NETWORK_BOUNDARIES for boundary in required_boundaries)
    risk_level = _tool_risk_level(required_tools)
    approval_behavior, requires_approval = _runtime_approval_behavior(
        boundaries=required_boundaries,
        risk_level=risk_level,
    )
    lifecycle_boundaries = [
        boundary
        for boundary in required_boundaries
        if boundary in LIFECYCLE_APPROVAL_BOUNDARIES
    ]

    if extension is None or extension.manifest is None:
        return {
            "status": "unmanaged",
            "ok": True,
            "declared_tools": [],
            "required_tools": required_tools,
            "missing_tools": [],
            "declared_execution_boundaries": [],
            "required_execution_boundaries": required_boundaries,
            "missing_execution_boundaries": [],
            "network": None,
            "requires_network": requires_network,
            "missing_network": False,
            "secrets": [],
            "env": [],
            "accepts_secret_refs": _tools_accept_secret_refs(required_tools),
            "risk_level": risk_level,
            "approval_behavior": approval_behavior,
            "requires_approval": requires_approval,
            "lifecycle_approval_boundaries": lifecycle_boundaries,
        }

    manifest_permissions = extension.manifest.permissions
    declared_tools = _dedupe_strings(manifest_permissions.tools)
    declared_boundaries = _dedupe_strings(manifest_permissions.execution_boundaries)
    missing_tools = [tool_name for tool_name in required_tools if tool_name not in declared_tools]
    missing_boundaries = [
        boundary
        for boundary in required_boundaries
        if declared_boundaries and boundary not in declared_boundaries
    ]
    missing_network = requires_network and not manifest_permissions.network
    ok = not missing_tools and not missing_boundaries and not missing_network
    return {
        "status": "granted" if ok else "insufficient",
        "ok": ok,
        "declared_tools": declared_tools,
        "required_tools": required_tools,
        "missing_tools": missing_tools,
        "declared_execution_boundaries": declared_boundaries,
        "required_execution_boundaries": required_boundaries,
        "missing_execution_boundaries": missing_boundaries,
        "network": bool(manifest_permissions.network),
        "requires_network": requires_network,
        "missing_network": missing_network,
        "secrets": _dedupe_strings(manifest_permissions.secrets),
        "env": _dedupe_strings(manifest_permissions.env),
        "accepts_secret_refs": _tools_accept_secret_refs(required_tools),
        "risk_level": risk_level,
        "approval_behavior": approval_behavior,
        "requires_approval": requires_approval,
        "lifecycle_approval_boundaries": lifecycle_boundaries,
    }


def evaluate_contribution_permissions(
    extension: ExtensionRecord | None,
    *,
    contribution_type: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    if contribution_type == "skills":
        return evaluate_tool_permissions(
            extension,
            tool_names=_dedupe_strings(metadata.get("requires_tools")),
        )

    if contribution_type == "workflows":
        step_tools = _dedupe_strings(metadata.get("step_tools"))
        if not step_tools:
            step_tools = _dedupe_strings(metadata.get("requires_tools"))
        return evaluate_tool_permissions(extension, tool_names=step_tools)

    if contribution_type == "toolset_presets":
        profile = evaluate_tool_permissions(
            extension,
            tool_names=_dedupe_strings(metadata.get("include_tools")),
        )
        explicit_boundaries = _dedupe_strings(metadata.get("execution_boundaries"))
        if _dedupe_strings(metadata.get("include_mcp_servers")) and "external_mcp" not in explicit_boundaries:
            explicit_boundaries.append("external_mcp")
        return _merge_explicit_boundaries_into_profile(
            profile,
            explicit_boundaries=explicit_boundaries,
        )

    if contribution_type == "mcp_servers":
        tool_profile = evaluate_tool_permissions(extension, tool_names=["mcp_extension_connector"])
        tool_profile.update(
            {
                "status": "granted" if extension is None or extension.manifest is None or extension.manifest.permissions.network else "insufficient",
                "ok": extension is None or extension.manifest is None or extension.manifest.permissions.network,
                "declared_tools": tool_profile["declared_tools"],
                "required_tools": [],
                "missing_tools": [],
                "required_execution_boundaries": ["external_mcp"],
                "missing_execution_boundaries": [
                    "external_mcp"
                    for _ in [0]
                    if extension is not None
                    and extension.manifest is not None
                    and extension.manifest.permissions.execution_boundaries
                    and "external_mcp" not in extension.manifest.permissions.execution_boundaries
                ],
                "requires_network": True,
                "missing_network": bool(
                    extension is not None
                    and extension.manifest is not None
                    and not extension.manifest.permissions.network
                ),
                "risk_level": "high",
                "approval_behavior": "mcp_policy",
                "requires_approval": True,
                "lifecycle_approval_boundaries": ["external_mcp"],
            }
        )
        tool_profile["ok"] = not tool_profile["missing_execution_boundaries"] and not tool_profile["missing_network"]
        tool_profile["status"] = "granted" if tool_profile["ok"] else "insufficient"
        return tool_profile

    if contribution_type in {
        "managed_connectors",
        "automation_triggers",
        "browser_providers",
        "messaging_connectors",
        "node_adapters",
    }:
        profile = {
            "status": "granted",
            "ok": True,
            "declared_tools": (
                _dedupe_strings(extension.manifest.permissions.tools)
                if extension is not None and extension.manifest is not None
                else []
            ),
            "required_tools": [],
            "missing_tools": [],
            "declared_execution_boundaries": (
                _dedupe_strings(extension.manifest.permissions.execution_boundaries)
                if extension is not None and extension.manifest is not None
                else []
            ),
            "required_execution_boundaries": [],
            "missing_execution_boundaries": [],
            "network": (
                bool(extension.manifest.permissions.network)
                if extension is not None and extension.manifest is not None
                else None
            ),
            "requires_network": bool(metadata.get("requires_network")),
            "missing_network": bool(
                extension is not None
                and extension.manifest is not None
                and metadata.get("requires_network")
                and not extension.manifest.permissions.network
            ),
            "secrets": (
                _dedupe_strings(extension.manifest.permissions.secrets)
                if extension is not None and extension.manifest is not None
                else []
            ),
            "env": (
                _dedupe_strings(extension.manifest.permissions.env)
                if extension is not None and extension.manifest is not None
                else []
            ),
            "accepts_secret_refs": False,
            "risk_level": "low",
            "approval_behavior": "never",
            "requires_approval": False,
            "lifecycle_approval_boundaries": [],
        }
        explicit_boundaries: list[str] = []
        if _password_config_fields_present(metadata):
            explicit_boundaries.append("secret_management")
        return _merge_explicit_boundaries_into_profile(
            profile,
            explicit_boundaries=explicit_boundaries,
        )

    requires_network = bool(metadata.get("requires_network"))
    missing_network = bool(
        extension is not None
        and extension.manifest is not None
        and requires_network
        and not extension.manifest.permissions.network
    )
    return {
        "status": "granted" if not missing_network else "insufficient",
        "ok": not missing_network,
        "declared_tools": (
            _dedupe_strings(extension.manifest.permissions.tools)
            if extension is not None and extension.manifest is not None
            else []
        ),
        "required_tools": [],
        "missing_tools": [],
        "declared_execution_boundaries": (
            _dedupe_strings(extension.manifest.permissions.execution_boundaries)
            if extension is not None and extension.manifest is not None
            else []
        ),
        "required_execution_boundaries": [],
        "missing_execution_boundaries": [],
        "network": (
            bool(extension.manifest.permissions.network)
            if extension is not None and extension.manifest is not None
            else None
        ),
        "requires_network": requires_network,
        "missing_network": missing_network,
        "secrets": (
            _dedupe_strings(extension.manifest.permissions.secrets)
            if extension is not None and extension.manifest is not None
            else []
        ),
        "env": (
            _dedupe_strings(extension.manifest.permissions.env)
            if extension is not None and extension.manifest is not None
            else []
        ),
        "accepts_secret_refs": False,
        "risk_level": "low",
        "approval_behavior": "never",
        "requires_approval": False,
        "lifecycle_approval_boundaries": [],
    }


def summarize_extension_permissions(
    extension: ExtensionRecord,
    *,
    contribution_profiles: list[dict[str, Any]],
) -> dict[str, Any]:
    declared_tools = (
        _dedupe_strings(extension.manifest.permissions.tools)
        if extension.manifest is not None
        else []
    )
    declared_boundaries = (
        _dedupe_strings(extension.manifest.permissions.execution_boundaries)
        if extension.manifest is not None
        else []
    )
    declared_secrets = (
        _dedupe_strings(extension.manifest.permissions.secrets)
        if extension.manifest is not None
        else []
    )
    declared_env = (
        _dedupe_strings(extension.manifest.permissions.env)
        if extension.manifest is not None
        else []
    )
    required_tools = _dedupe_strings(
        [tool_name for profile in contribution_profiles for tool_name in profile.get("required_tools", [])]
    )
    required_boundaries = _dedupe_strings(
        [boundary for profile in contribution_profiles for boundary in profile.get("required_execution_boundaries", [])]
    )
    missing_tools = _dedupe_strings(
        [tool_name for profile in contribution_profiles for tool_name in profile.get("missing_tools", [])]
    )
    missing_boundaries = _dedupe_strings(
        [boundary for profile in contribution_profiles for boundary in profile.get("missing_execution_boundaries", [])]
    )
    lifecycle_approval_boundaries = _dedupe_strings(
        [
            boundary
            for profile in contribution_profiles
            for boundary in profile.get("lifecycle_approval_boundaries", [])
        ]
    )
    requires_network = any(bool(profile.get("requires_network")) for profile in contribution_profiles)
    missing_network = any(bool(profile.get("missing_network")) for profile in contribution_profiles)
    accepts_secret_refs = any(bool(profile.get("accepts_secret_refs")) for profile in contribution_profiles)
    risk_level = _max_risk_level(
        [str(profile.get("risk_level") or "low") for profile in contribution_profiles]
    )
    runtime_approval = _runtime_approval_behavior(
        boundaries=required_boundaries,
        risk_level=risk_level,
    )
    ok = not missing_tools and not missing_boundaries and not missing_network
    return {
        "status": "granted" if ok else "insufficient",
        "ok": ok,
        "declared": {
            "tools": declared_tools,
            "execution_boundaries": declared_boundaries,
            "network": (
                bool(extension.manifest.permissions.network)
                if extension.manifest is not None
                else None
            ),
            "secrets": declared_secrets,
            "env": declared_env,
        },
        "required": {
            "tools": required_tools,
            "execution_boundaries": required_boundaries,
            "network": requires_network,
            "accepts_secret_refs": accepts_secret_refs,
        },
        "missing": {
            "tools": missing_tools,
            "execution_boundaries": missing_boundaries,
            "network": missing_network,
        },
        "risk_level": risk_level,
        "approval_profile": {
            "requires_runtime_approval": runtime_approval[1],
            "runtime_behavior": runtime_approval[0],
            "requires_lifecycle_approval": bool(lifecycle_approval_boundaries),
            "lifecycle_boundaries": lifecycle_approval_boundaries,
            "risk_level": risk_level,
        },
    }
