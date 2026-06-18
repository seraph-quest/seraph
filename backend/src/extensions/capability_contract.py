"""Canonical capability contract helpers for runtime-visible Seraph surfaces."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.extensions.registry import ExtensionRecord

CONTRACT_VERSION = "m1.2026-05"
CAPABILITY_CONTRACT_SCHEMA_VERSION = "2026-05-04.m1"

_REJECTED_CONTRIBUTION_TYPES = {
    "skills",
    "workflows",
    "toolset_presets",
    "mcp_servers",
}

CAPABILITY_FAMILY_BY_CONTRIBUTION_TYPE = {
    "skills": "skill",
    "workflows": "workflow",
    "runbooks": "runbook",
    "starter_packs": "starter_pack",
    "provider_presets": "provider_preset",
    "toolset_presets": "toolset_preset",
    "prompt_packs": "prompt_pack",
    "context_packs": "context_pack",
    "scheduled_routines": "automation",
    "mcp_servers": "mcp_server",
    "managed_connectors": "connector",
    "memory_providers": "memory_provider",
    "automation_triggers": "automation_trigger",
    "browser_providers": "browser_provider",
    "messaging_connectors": "messaging_connector",
    "observer_definitions": "observer_source",
    "observer_connectors": "observer_connector",
    "channel_adapters": "channel_adapter",
    "canvas_outputs": "canvas_output",
    "workflow_runtimes": "workflow_runtime",
    "speech_profiles": "speech_profile",
    "node_adapters": "node_adapter",
    "workspace_adapters": "workspace_adapter",
}

NATIVE_TOOL_FAMILY = "native_tool"

TRUST_CLASS_BY_EXTENSION_TRUST = {
    "bundled": "seraph_bundled",
    "verified": "verified_pack",
    "local": "local_pack",
}

READ_BOUNDARIES = {
    "workspace_read",
    "external_read",
    "guardian_state_read",
    "conversation_history_read",
    "container_process_read",
}

WRITE_BOUNDARIES = {
    "workspace_write",
    "guardian_state_write",
    "automation_state",
    "secret_management",
    "connector_mutation",
}

EXECUTE_BOUNDARIES = {
    "sandbox_execution",
    "container_process_execution",
    "container_process_management",
    "background_execution",
    "external_mcp",
    "delegation",
}

SECRET_BOUNDARIES = {"secret_read", "secret_injection", "secret_management"}

QUARANTINE_STATES = {"invalid", "conflict", "overridden", "requires_config", "invalid_config"}


def _dedupe_strings(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
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


def _operator_description(
    *,
    family: str,
    name: str,
    description: str,
    extension: ExtensionRecord | None = None,
) -> str:
    if description:
        return description
    if extension is not None and extension.manifest is not None:
        manifest_description = extension.manifest.summary or extension.manifest.description
        if manifest_description:
            return manifest_description
    return f"{family.replace('_', ' ')} capability {name}".strip()


def _declared_permissions_payload(extension: ExtensionRecord | None) -> dict[str, Any]:
    if extension is None or extension.manifest is None:
        return {
            "tools": [],
            "execution_boundaries": [],
            "network": None,
            "data_access": [],
            "mutation_rights": [],
            "audit_events": [],
            "secrets": [],
            "env": [],
        }
    permissions = extension.manifest.permissions
    return {
        "tools": _dedupe_strings(permissions.tools),
        "execution_boundaries": _dedupe_strings(permissions.execution_boundaries),
        "network": bool(permissions.network),
        "data_access": _dedupe_strings(getattr(permissions, "data_access", [])),
        "mutation_rights": _dedupe_strings(getattr(permissions, "mutation_rights", [])),
        "audit_events": _dedupe_strings(getattr(permissions, "audit_events", [])),
        "secrets": _dedupe_strings(permissions.secrets),
        "env": _dedupe_strings(permissions.env),
    }


def _capability_enforcement(
    *,
    contribution_type: str,
    permission_profile: Mapping[str, Any],
) -> dict[str, Any]:
    missing_tools = _dedupe_strings(permission_profile.get("missing_tools"))
    missing_boundaries = _dedupe_strings(permission_profile.get("missing_execution_boundaries"))
    missing_network = bool(permission_profile.get("missing_network"))
    missing: dict[str, Any] = {
        "tools": missing_tools,
        "execution_boundaries": missing_boundaries,
        "network": missing_network,
    }
    if not missing_tools and not missing_boundaries and not missing_network:
        return {
            "status": "accepted",
            "action": "allow",
            "reason": "",
            "missing": missing,
            "runtime_ready": True,
        }

    action = "reject" if contribution_type in _REJECTED_CONTRIBUTION_TYPES else "quarantine"
    status = "rejected" if action == "reject" else "quarantined"
    reasons: list[str] = []
    if missing_tools:
        reasons.append(f"undeclared tools: {', '.join(missing_tools)}")
    if missing_boundaries:
        reasons.append(f"undeclared execution boundaries: {', '.join(missing_boundaries)}")
    if missing_network:
        reasons.append("undeclared network access")
    return {
        "status": status,
        "action": action,
        "reason": "; ".join(reasons),
        "missing": missing,
        "runtime_ready": False,
    }


def _mutation_rights(boundaries: list[str]) -> dict[str, bool]:
    boundary_set = set(boundaries)
    return {
        "reads_workspace": "workspace_read" in boundary_set,
        "writes_workspace": "workspace_write" in boundary_set,
        "reads_external": "external_read" in boundary_set or "external_mcp" in boundary_set,
        "mutates_external": "connector_mutation" in boundary_set or "external_mcp" in boundary_set,
        "executes_code": bool(boundary_set.intersection(EXECUTE_BOUNDARIES)),
        "reads_guardian_state": "guardian_state_read" in boundary_set,
        "mutates_guardian_state": "guardian_state_write" in boundary_set,
        "uses_secrets": bool(boundary_set.intersection(SECRET_BOUNDARIES)),
    }


def _audit_contract(permission_profile: Mapping[str, Any], boundaries: list[str]) -> dict[str, Any]:
    risk_level = str(permission_profile.get("risk_level") or "low")
    approval_behavior = str(permission_profile.get("approval_behavior") or "never")
    requires_approval = bool(permission_profile.get("requires_approval"))
    return {
        "event_scope": "capability",
        "required": risk_level != "low" or requires_approval or bool(boundaries),
        "risk_level": risk_level,
        "approval_behavior": approval_behavior,
        "requires_approval": requires_approval,
        "execution_boundaries": boundaries,
    }


def _permission_contract(permission_profile: Mapping[str, Any]) -> dict[str, Any]:
    required_boundaries = _dedupe_strings(permission_profile.get("required_execution_boundaries"))
    return {
        "status": str(permission_profile.get("status") or "unknown"),
        "declared_tools": _dedupe_strings(permission_profile.get("declared_tools")),
        "required_tools": _dedupe_strings(permission_profile.get("required_tools")),
        "missing_tools": _dedupe_strings(permission_profile.get("missing_tools")),
        "declared_execution_boundaries": _dedupe_strings(
            permission_profile.get("declared_execution_boundaries")
        ),
        "required_execution_boundaries": required_boundaries,
        "missing_execution_boundaries": _dedupe_strings(
            permission_profile.get("missing_execution_boundaries")
        ),
        "network": permission_profile.get("network"),
        "requires_network": bool(permission_profile.get("requires_network")),
        "missing_network": bool(permission_profile.get("missing_network")),
        "secrets": _dedupe_strings(permission_profile.get("secrets")),
        "env": _dedupe_strings(permission_profile.get("env")),
    }


def _quarantine_contract(
    *,
    permission_profile: Mapping[str, Any],
    health: Mapping[str, Any] | None,
    status: str,
) -> dict[str, Any]:
    reasons: list[str] = []
    if not bool(permission_profile.get("ok", True)):
        if _dedupe_strings(permission_profile.get("missing_tools")):
            reasons.append("undeclared_tools")
        if _dedupe_strings(permission_profile.get("missing_execution_boundaries")):
            reasons.append("undeclared_execution_boundaries")
        if bool(permission_profile.get("missing_network")):
            reasons.append("undeclared_network")
    if status in QUARANTINE_STATES:
        reasons.append(status)
    if isinstance(health, Mapping) and health.get("ready") is False:
        state = str(health.get("state") or "").strip()
        if state and state not in reasons and state in QUARANTINE_STATES:
            reasons.append(state)
    return {
        "state": "quarantined" if reasons else "active",
        "reasons": reasons,
    }


def build_native_tool_contract(
    *,
    name: str,
    description: str,
    policy_modes: list[str],
    risk_level: str,
    execution_boundaries: list[str],
    availability: str,
    blocked_reason: str | None,
    execution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    permission_profile = {
        "ok": availability == "ready",
        "status": "granted" if availability == "ready" else "policy_blocked",
        "declared_tools": [name],
        "required_tools": [name],
        "missing_tools": [],
        "declared_execution_boundaries": execution_boundaries,
        "required_execution_boundaries": execution_boundaries,
        "missing_execution_boundaries": [],
        "network": "external_read" in execution_boundaries or "external_mcp" in execution_boundaries,
        "requires_network": "external_read" in execution_boundaries or "external_mcp" in execution_boundaries,
        "missing_network": False,
        "risk_level": risk_level,
        "approval_behavior": "high_risk" if risk_level == "high" else "never",
        "requires_approval": risk_level == "high",
    }
    health = {
        "state": availability,
        "ready": availability == "ready",
        "summary": blocked_reason or "Native tool is available under the current policy mode.",
    }
    permission_contract = _permission_contract(permission_profile)
    required_boundaries = permission_contract["required_execution_boundaries"]
    return {
        "schema_version": CAPABILITY_CONTRACT_SCHEMA_VERSION,
        "version": CONTRACT_VERSION,
        "capability_id": f"native_tool:{name}",
        "family": NATIVE_TOOL_FAMILY,
        "name": name,
        "operator_description": _operator_description(
            family=NATIVE_TOOL_FAMILY,
            name=name,
            description=description,
        ),
        "trust_class": "seraph_bundled",
        "provenance": {
            "source": "native",
            "registry": "src.native_tools.registry.TOOL_METADATA",
        },
        "permissions": {
            "declared": {
                "tools": permission_contract["declared_tools"],
                "execution_boundaries": permission_contract["declared_execution_boundaries"],
                "network": permission_contract["network"],
                "secrets": permission_contract["secrets"],
                "env": permission_contract["env"],
            },
            "required": {
                "tools": permission_contract["required_tools"],
                "execution_boundaries": required_boundaries,
                "network": permission_contract["requires_network"],
                "accepts_secret_refs": False,
            },
            "missing": {
                "tools": permission_contract["missing_tools"],
                "execution_boundaries": permission_contract["missing_execution_boundaries"],
                "network": permission_contract["missing_network"],
            },
        },
        "mutation_rights": _mutation_rights(required_boundaries),
        "execution": dict(execution or {}),
        "audit": _audit_contract(permission_profile, required_boundaries),
        "health": health,
        "preflight": {
            "status": availability,
            "ready": availability == "ready",
            "blocking_reasons": [blocked_reason] if blocked_reason else [],
            "policy_modes": policy_modes,
        },
        "compatibility": {"seraph": "runtime-native", "compatible": True},
        "quarantine": _quarantine_contract(
            permission_profile=permission_profile,
            health=health,
            status=availability,
        ),
    }


def build_capability_contract(
    extension: ExtensionRecord | None,
    *,
    contribution_type: str,
    reference: str,
    metadata: dict[str, Any],
    permission_profile: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the canonical operator-visible contract for an extension contribution."""
    declared = _declared_permissions_payload(extension)
    manifest = extension.manifest if extension is not None else None
    family = CAPABILITY_FAMILY_BY_CONTRIBUTION_TYPE.get(contribution_type, contribution_type)
    name = str(metadata.get("name") or metadata.get("tool_name") or reference)
    required_boundaries = _dedupe_strings(permission_profile.get("required_execution_boundaries"))
    provenance = {
        "source": extension.source if extension is not None else "unmanaged",
        "extension_id": extension.id if extension is not None else None,
        "extension_display_name": extension.display_name if extension is not None else None,
        "manifest_path": extension.manifest_path if extension is not None else None,
        "root_path": extension.root_path if extension is not None else None,
        "publisher": manifest.publisher.name if manifest is not None else None,
        "version": manifest.version if manifest is not None else None,
        "compatibility": manifest.compatibility.seraph if manifest is not None else None,
        "trust_class": (
            TRUST_CLASS_BY_EXTENSION_TRUST.get(extension.trust, extension.trust)
            if extension is not None
            else "unmanaged"
        ),
        "reference": reference,
        "resolved_path": metadata.get("resolved_path"),
    }
    enforcement = _capability_enforcement(
        contribution_type=contribution_type,
        permission_profile=permission_profile,
    )
    return {
        "schema_version": CAPABILITY_CONTRACT_SCHEMA_VERSION,
        "version": CONTRACT_VERSION,
        "capability_id": (
            f"{extension.id}:{contribution_type}:{reference}"
            if extension is not None
            else f"unmanaged:{contribution_type}:{reference}"
        ),
        "extension_id": extension.id if extension is not None else None,
        "contribution_type": contribution_type,
        "family": family,
        "reference": reference,
        "name": name,
        "trust_class": provenance["trust_class"],
        "operator": {
            "display_name": extension.display_name if extension is not None else name,
            "description": _operator_description(
                family=family,
                name=name,
                description=str(metadata.get("description") or ""),
                extension=extension,
            ),
        },
        "operator_description": _operator_description(
            family=family,
            name=name,
            description=str(metadata.get("description") or ""),
            extension=extension,
        ),
        "provenance": provenance,
        "permissions": {
            "declared": declared,
            "required": {
                "tools": _dedupe_strings(permission_profile.get("required_tools")),
                "execution_boundaries": required_boundaries,
                "network": bool(permission_profile.get("requires_network")),
                "accepts_secret_refs": bool(permission_profile.get("accepts_secret_refs")),
            },
            "missing": {
                "tools": _dedupe_strings(permission_profile.get("missing_tools")),
                "execution_boundaries": _dedupe_strings(
                    permission_profile.get("missing_execution_boundaries")
                ),
                "network": bool(permission_profile.get("missing_network")),
            },
        },
        "mutation_rights": _mutation_rights(required_boundaries),
        "audit": _audit_contract(permission_profile, required_boundaries),
        "runtime": {
            "risk_level": str(permission_profile.get("risk_level") or "low"),
            "approval_behavior": str(permission_profile.get("approval_behavior") or "never"),
            "requires_approval": bool(permission_profile.get("requires_approval")),
            "lifecycle_approval_boundaries": _dedupe_strings(
                permission_profile.get("lifecycle_approval_boundaries")
            ),
        },
        "health": {
            "state": "ready" if bool(permission_profile.get("ok", True)) else enforcement["status"],
            "ready": bool(permission_profile.get("ok", True)),
        },
        "preflight": {
            "status": "ready" if bool(permission_profile.get("ok", True)) else enforcement["status"],
            "ready": bool(permission_profile.get("ok", True)),
            "blocking_reasons": [enforcement["reason"]] if enforcement["reason"] else [],
        },
        "compatibility": {
            "seraph": manifest.compatibility.seraph if manifest is not None else None,
            "compatible": True,
        },
        "enforcement": enforcement,
        "quarantine": {
            "state": "active" if enforcement["status"] == "accepted" else enforcement["status"],
            "reasons": [enforcement["reason"]] if enforcement["reason"] else [],
        },
    }
