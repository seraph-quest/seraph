"""Structured validation and repair diagnostics for extension packages."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.extensions.capability_contributions import (
    parse_automation_trigger_definition,
    parse_browser_provider_definition,
    parse_context_pack_definition,
    parse_messaging_connector_definition,
    parse_node_adapter_definition,
    parse_provider_preset_definition,
    parse_speech_profile_definition,
    parse_toolset_preset_definition,
)
from src.extensions.connectors import (
    ConnectorDefinitionError,
    parse_managed_connector_definition,
    parse_mcp_server_definition,
)
from src.extensions.channels import parse_channel_adapter_definition
from src.extensions.observers import parse_observer_definition
from src.extensions.permissions import evaluate_contribution_permissions, evaluate_tool_permissions
from src.extensions.registry import (
    ExtensionLoadErrorRecord,
    ExtensionRecord,
    ExtensionRegistrySnapshot,
)
from src.security.context_scan import scan_text_for_suspicious_context
from src.skills.loader import parse_skill_content
from src.workflows.loader import parse_workflow_content
import yaml

_CONNECTOR_CONTRIBUTIONS = {
    "mcp_servers",
    "managed_connectors",
    "automation_triggers",
    "browser_providers",
    "messaging_connectors",
    "observer_connectors",
    "channel_adapters",
    "node_adapters",
    "workspace_adapters",
}
_CONTEXT_SCANNED_CONTRIBUTIONS = {"skills", "workflows", "prompt_packs", "context_packs"}

_DEFINITION_PARSERS = {
    "toolset_presets": ("invalid_toolset_preset", "toolset preset", parse_toolset_preset_definition),
    "context_packs": ("invalid_context_pack", "context pack", parse_context_pack_definition),
    "automation_triggers": ("invalid_automation_trigger", "automation trigger", parse_automation_trigger_definition),
    "browser_providers": ("invalid_browser_provider", "browser provider", parse_browser_provider_definition),
    "messaging_connectors": ("invalid_messaging_connector", "messaging connector", parse_messaging_connector_definition),
    "speech_profiles": ("invalid_speech_profile", "speech profile", parse_speech_profile_definition),
    "provider_presets": ("invalid_provider_preset", "provider preset", parse_provider_preset_definition),
    "node_adapters": ("invalid_node_adapter", "node adapter", parse_node_adapter_definition),
}


@dataclass(frozen=True)
class ExtensionDoctorIssue:
    code: str
    severity: str
    message: str
    contribution_type: str | None = None
    reference: str | None = None
    suggested_fix: str | None = None


@dataclass(frozen=True)
class ExtensionDoctorResult:
    extension_id: str
    ok: bool
    issues: list[ExtensionDoctorIssue] = field(default_factory=list)


@dataclass(frozen=True)
class ExtensionDoctorReport:
    results: list[ExtensionDoctorResult]
    load_errors: list[ExtensionLoadErrorRecord]

    @property
    def ok(self) -> bool:
        return not self.load_errors and all(result.ok for result in self.results)


def _read_contribution_text(
    resolved: Path,
    *,
    contribution_type: str,
    reference: str,
) -> tuple[str | None, ExtensionDoctorIssue | None]:
    try:
        return resolved.read_text(encoding="utf-8"), None
    except UnicodeDecodeError:
        return None, ExtensionDoctorIssue(
            code="unreadable_contribution",
            severity="error",
            message=f"Contribution file is not valid UTF-8 text: {reference}",
            contribution_type=contribution_type,
            reference=reference,
            suggested_fix="save the contribution file as UTF-8 text",
        )
    except OSError as exc:
        return None, ExtensionDoctorIssue(
            code="unreadable_contribution",
            severity="error",
            message=f"Contribution file could not be read: {reference} ({exc})",
            contribution_type=contribution_type,
            reference=reference,
            suggested_fix="repair the file permissions or replace the unreadable file",
        )


def _load_connector_payload(
    content: str,
    *,
    contribution_type: str,
    reference: str,
) -> tuple[Any | None, ExtensionDoctorIssue | None]:
    try:
        payload = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        return None, ExtensionDoctorIssue(
            code="invalid_connector",
            severity="error",
            message=f"Connector definition could not be parsed: {reference} ({exc})",
            contribution_type=contribution_type,
            reference=reference,
            suggested_fix="fix the connector file so it is valid JSON or YAML",
        )

    if payload is None:
        return None, ExtensionDoctorIssue(
            code="invalid_connector",
            severity="error",
            message=f"Connector definition is empty: {reference}",
            contribution_type=contribution_type,
            reference=reference,
            suggested_fix="add a structured connector definition",
        )

    if not isinstance(payload, (dict, list)):
        return None, ExtensionDoctorIssue(
            code="invalid_connector",
            severity="error",
            message=f"Connector definition must be an object or list: {reference}",
            contribution_type=contribution_type,
            reference=reference,
            suggested_fix="make the connector file a JSON/YAML object or list",
        )

    return payload, None


def _connector_implies_network(payload: Any) -> bool:
    transport_values: set[str] = set()
    url_values: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                lowered = str(key).lower()
                if lowered == "transport" and isinstance(value, str):
                    transport_values.add(value.strip().lower())
                if lowered in {"url", "base_url", "endpoint"} and isinstance(value, str):
                    url_values.append(value.strip().lower())
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(payload)

    if transport_values.intersection({"http", "https", "sse", "streamable-http", "websocket", "ws", "wss"}):
        return True

    return any(
        url.startswith(("http://", "https://", "ws://", "wss://"))
        for url in url_values
    )


def doctor_extension(
    extension: ExtensionRecord,
    *,
    available_mcp_server_names: set[str] | None = None,
) -> ExtensionDoctorResult:
    issues: list[ExtensionDoctorIssue] = []

    if extension.source != "manifest" or extension.manifest is None:
        return ExtensionDoctorResult(extension_id=extension.id, ok=True, issues=[])
    for contribution in extension.contributions:
        resolved_path = contribution.metadata.get("resolved_path")
        resolved = Path(resolved_path) if isinstance(resolved_path, str) else None
        if resolved is None or not resolved.exists():
            issues.append(
                ExtensionDoctorIssue(
                    code="missing_reference",
                    severity="error",
                    message=f"Referenced contribution file is missing: {contribution.reference}",
                    contribution_type=contribution.contribution_type,
                    reference=contribution.reference,
                    suggested_fix="repair the manifest path or add the missing file",
                )
            )
            continue

        content, unreadable_issue = _read_contribution_text(
            resolved,
            contribution_type=contribution.contribution_type,
            reference=contribution.reference,
        )
        if unreadable_issue is not None:
            issues.append(unreadable_issue)
            continue
        assert content is not None

        if contribution.contribution_type in _CONTEXT_SCANNED_CONTRIBUTIONS:
            for finding in scan_text_for_suspicious_context(
                content,
                include_fenced_blocks=contribution.contribution_type == "context_packs",
            ):
                issues.append(
                    ExtensionDoctorIssue(
                        code="suspicious_context_content",
                        severity="error",
                        message=(
                            "Contribution contains suspicious prompt-bearing content "
                            f"({finding.description}: '{finding.excerpt}')"
                        ),
                        contribution_type=contribution.contribution_type,
                        reference=contribution.reference,
                        suggested_fix=(
                            "remove instruction-override, secret-exfiltration, or policy-bypass "
                            "language from the package content"
                        ),
                    )
                )

        if contribution.contribution_type == "skills":
            errors: list[dict[str, str]] = []
            skill = parse_skill_content(content, path=str(resolved), errors=errors)
            if skill is None:
                issues.append(
                    ExtensionDoctorIssue(
                        code="invalid_skill",
                        severity="error",
                        message=errors[0]["message"] if errors else f"Invalid skill file: {contribution.reference}",
                        contribution_type="skills",
                        reference=contribution.reference,
                        suggested_fix="fix the SKILL.md frontmatter/body so it parses cleanly",
                    )
                )
                continue
            permission_profile = evaluate_tool_permissions(extension, tool_names=skill.requires_tools)
            if permission_profile["missing_tools"]:
                issues.append(
                    ExtensionDoctorIssue(
                        code="permission_mismatch",
                        severity="error",
                        message=(
                            "Manifest permissions are missing required skill tools: "
                            f"{', '.join(permission_profile['missing_tools'])}"
                        ),
                        contribution_type="skills",
                        reference=contribution.reference,
                        suggested_fix="add the missing tools to manifest.permissions.tools",
                    )
                )
            if permission_profile["missing_execution_boundaries"]:
                issues.append(
                    ExtensionDoctorIssue(
                        code="permission_mismatch",
                        severity="error",
                        message=(
                            "Manifest permissions are missing required skill execution boundaries: "
                            f"{', '.join(permission_profile['missing_execution_boundaries'])}"
                        ),
                        contribution_type="skills",
                        reference=contribution.reference,
                        suggested_fix="add the missing boundaries to manifest.permissions.execution_boundaries",
                    )
                )
            if permission_profile["missing_network"]:
                issues.append(
                    ExtensionDoctorIssue(
                        code="permission_mismatch",
                        severity="error",
                        message=(
                            "Skill requires network access but "
                            "manifest.permissions.network is false"
                        ),
                        contribution_type="skills",
                        reference=contribution.reference,
                        suggested_fix="set manifest.permissions.network to true for networked skills",
                    )
                )
            continue

        if contribution.contribution_type == "workflows":
            errors = []
            workflow = parse_workflow_content(content, path=str(resolved), errors=errors)
            if workflow is None:
                issues.append(
                    ExtensionDoctorIssue(
                        code="invalid_workflow",
                        severity="error",
                        message=errors[0]["message"] if errors else f"Invalid workflow file: {contribution.reference}",
                        contribution_type="workflows",
                        reference=contribution.reference,
                        suggested_fix="fix the workflow frontmatter or steps so it parses cleanly",
                    )
                )
                continue
            permission_profile = evaluate_tool_permissions(extension, tool_names=workflow.step_tools)
            if permission_profile["missing_tools"]:
                issues.append(
                    ExtensionDoctorIssue(
                        code="permission_mismatch",
                        severity="error",
                        message=(
                            "Manifest permissions are missing required workflow tools: "
                            f"{', '.join(permission_profile['missing_tools'])}"
                        ),
                        contribution_type="workflows",
                        reference=contribution.reference,
                        suggested_fix="add the missing tools to manifest.permissions.tools",
                    )
                )
            if permission_profile["missing_execution_boundaries"]:
                issues.append(
                    ExtensionDoctorIssue(
                        code="permission_mismatch",
                        severity="error",
                        message=(
                            "Manifest permissions are missing required workflow execution boundaries: "
                            f"{', '.join(permission_profile['missing_execution_boundaries'])}"
                        ),
                        contribution_type="workflows",
                        reference=contribution.reference,
                        suggested_fix="add the missing boundaries to manifest.permissions.execution_boundaries",
                    )
                )
            if permission_profile["missing_network"]:
                issues.append(
                    ExtensionDoctorIssue(
                        code="permission_mismatch",
                        severity="error",
                        message=(
                            "Workflow requires network access but "
                            "manifest.permissions.network is false"
                        ),
                        contribution_type="workflows",
                        reference=contribution.reference,
                        suggested_fix="set manifest.permissions.network to true for networked workflows",
                    )
                )
            continue

        if contribution.contribution_type in _DEFINITION_PARSERS:
            issue_code, label, parser = _DEFINITION_PARSERS[contribution.contribution_type]
            payload, connector_issue = _load_connector_payload(
                content,
                contribution_type=contribution.contribution_type,
                reference=contribution.reference,
            )
            if connector_issue is not None:
                issues.append(connector_issue)
                continue
            assert payload is not None
            try:
                definition = parser(payload, source=str(resolved))
            except ConnectorDefinitionError as exc:
                issues.append(
                    ExtensionDoctorIssue(
                        code=issue_code,
                        severity="error",
                        message=str(exc),
                        contribution_type=contribution.contribution_type,
                        reference=contribution.reference,
                        suggested_fix=f"fix the {label} definition fields so the typed parser accepts the file",
                    )
                )
                continue
            if contribution.contribution_type == "toolset_presets":
                permission_profile = evaluate_contribution_permissions(
                    extension,
                    contribution_type=contribution.contribution_type,
                    metadata=definition.as_metadata(),
                )
                if permission_profile["missing_tools"]:
                    issues.append(
                        ExtensionDoctorIssue(
                            code="permission_mismatch",
                            severity="error",
                            message=(
                                "Manifest permissions are missing required toolset tools: "
                                f"{', '.join(permission_profile['missing_tools'])}"
                            ),
                            contribution_type=contribution.contribution_type,
                            reference=contribution.reference,
                            suggested_fix="add the missing tools to manifest.permissions.tools",
                        )
                    )
                missing_boundaries = list(permission_profile["missing_execution_boundaries"])
                if missing_boundaries:
                    issues.append(
                        ExtensionDoctorIssue(
                            code="permission_mismatch",
                            severity="error",
                            message=(
                                "Manifest permissions are missing required toolset execution boundaries: "
                                f"{', '.join(missing_boundaries)}"
                            ),
                            contribution_type=contribution.contribution_type,
                            reference=contribution.reference,
                            suggested_fix="add the missing boundaries to manifest.permissions.execution_boundaries",
                        )
                    )
                if permission_profile["missing_network"]:
                    issues.append(
                        ExtensionDoctorIssue(
                            code="permission_mismatch",
                            severity="error",
                            message="Toolset preset requires network access but manifest.permissions.network is false",
                            contribution_type=contribution.contribution_type,
                            reference=contribution.reference,
                            suggested_fix="set manifest.permissions.network to true for networked toolsets",
                        )
                    )
                if definition.include_mcp_servers:
                    missing_servers = sorted(
                        server_name
                        for server_name in definition.include_mcp_servers
                        if server_name not in (available_mcp_server_names or set())
                    )
                    if missing_servers:
                        issues.append(
                            ExtensionDoctorIssue(
                                code="missing_mcp_server_reference",
                                severity="error",
                                message=(
                                    "Toolset preset references MCP servers that are not available in the current "
                                    f"registry/runtime: {', '.join(missing_servers)}"
                                ),
                                contribution_type=contribution.contribution_type,
                                reference=contribution.reference,
                                suggested_fix="fix include_mcp_servers or install/configure the missing MCP server packages first",
                            )
                        )
                continue
            permission_profile = evaluate_contribution_permissions(
                extension,
                contribution_type=contribution.contribution_type,
                metadata=definition.as_metadata(),
            )
            if permission_profile["missing_network"]:
                issues.append(
                    ExtensionDoctorIssue(
                        code="permission_mismatch",
                        severity="error",
                        message=(
                            f"{label.capitalize()} requires network access but "
                            "manifest.permissions.network is false"
                        ),
                        contribution_type=contribution.contribution_type,
                        reference=contribution.reference,
                        suggested_fix="set manifest.permissions.network to true for networked connector surfaces",
                    )
                )
            continue

        if contribution.contribution_type in _CONNECTOR_CONTRIBUTIONS:
            payload, connector_issue = _load_connector_payload(
                content,
                contribution_type=contribution.contribution_type,
                reference=contribution.reference,
            )
            if connector_issue is not None:
                issues.append(connector_issue)
                continue
            if payload is not None and contribution.contribution_type == "mcp_servers":
                try:
                    parse_mcp_server_definition(payload, source=contribution.reference)
                except ConnectorDefinitionError as exc:
                    issues.append(
                        ExtensionDoctorIssue(
                            code="invalid_connector",
                            severity="error",
                            message=str(exc),
                            contribution_type=contribution.contribution_type,
                            reference=contribution.reference,
                            suggested_fix="add a valid MCP server name, url, and optional auth metadata",
                        )
                    )
                    continue
            if payload is not None and contribution.contribution_type == "managed_connectors":
                try:
                    parse_managed_connector_definition(payload, source=contribution.reference)
                except ConnectorDefinitionError as exc:
                    issues.append(
                        ExtensionDoctorIssue(
                            code="invalid_connector",
                            severity="error",
                            message=str(exc),
                            contribution_type=contribution.contribution_type,
                            reference=contribution.reference,
                            suggested_fix="add a valid managed connector name, provider, auth metadata, and config fields",
                        )
                    )
                    continue
            if (
                payload is not None
                and _connector_implies_network(payload)
                and not extension.manifest.permissions.network
            ):
                issues.append(
                    ExtensionDoctorIssue(
                        code="permission_mismatch",
                        severity="error",
                        message=(
                            "Connector definition uses a network transport but "
                            "manifest.permissions.network is false"
                        ),
                        contribution_type=contribution.contribution_type,
                        reference=contribution.reference,
                        suggested_fix="set manifest.permissions.network to true for networked connectors",
                    )
                )

        if contribution.contribution_type == "observer_definitions":
            payload, connector_issue = _load_connector_payload(
                content,
                contribution_type=contribution.contribution_type,
                reference=contribution.reference,
            )
            if connector_issue is not None:
                issues.append(connector_issue)
                continue
            try:
                definition = parse_observer_definition(payload, source=contribution.reference)
            except ConnectorDefinitionError as exc:
                issues.append(
                    ExtensionDoctorIssue(
                        code="invalid_observer_definition",
                        severity="error",
                        message=str(exc),
                        contribution_type=contribution.contribution_type,
                        reference=contribution.reference,
                        suggested_fix="add a valid observer name, source_type, and optional enabled flag",
                    )
                )
                continue
            if definition.requires_network and not extension.manifest.permissions.network:
                issues.append(
                    ExtensionDoctorIssue(
                        code="permission_mismatch",
                        severity="error",
                        message=(
                            "Observer definition requires network access but "
                            "manifest.permissions.network is false"
                        ),
                        contribution_type=contribution.contribution_type,
                        reference=contribution.reference,
                        suggested_fix="set manifest.permissions.network to true for networked observer sources",
                    )
                )

        if contribution.contribution_type == "channel_adapters":
            payload, connector_issue = _load_connector_payload(
                content,
                contribution_type=contribution.contribution_type,
                reference=contribution.reference,
            )
            if connector_issue is not None:
                issues.append(connector_issue)
                continue
            try:
                parse_channel_adapter_definition(payload, source=contribution.reference)
            except ConnectorDefinitionError as exc:
                issues.append(
                    ExtensionDoctorIssue(
                        code="invalid_channel_adapter",
                        severity="error",
                        message=str(exc),
                        contribution_type=contribution.contribution_type,
                        reference=contribution.reference,
                        suggested_fix="add a valid channel adapter name, transport, and optional enabled flag",
                    )
                )

    return ExtensionDoctorResult(extension_id=extension.id, ok=not issues, issues=issues)


def doctor_snapshot(snapshot: ExtensionRegistrySnapshot) -> ExtensionDoctorReport:
    try:
        from src.tools.mcp_manager import mcp_manager

        runtime_mcp_servers = {
            str(name).strip()
            for name in getattr(mcp_manager, "_config", {}).keys()
            if str(name).strip()
        }
    except Exception:
        runtime_mcp_servers = set()
    packaged_mcp_servers = {
        str(contribution.metadata.get("name")).strip()
        for contribution in snapshot.list_contributions("mcp_servers")
        if isinstance(contribution.metadata.get("name"), str) and str(contribution.metadata.get("name")).strip()
    }
    available_mcp_server_names = packaged_mcp_servers | runtime_mcp_servers
    return ExtensionDoctorReport(
        results=[
            doctor_extension(
                extension,
                available_mcp_server_names=available_mcp_server_names,
            )
            for extension in snapshot.extensions
        ],
        load_errors=list(snapshot.load_errors),
    )
