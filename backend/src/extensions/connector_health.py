"""Shared connector health contracts for extension lifecycle surfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ConnectorHealthSnapshot:
    state: str
    summary: str
    ready: bool
    enabled: bool | None = None
    configured: bool | None = None
    connected: bool | None = None
    runtime_status: str | None = None
    error: str | None = None
    supports_test: bool = False
    supports_configure: bool = False
    supports_enable: bool = False
    supports_disable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "state": self.state,
            "summary": self.summary,
            "ready": self.ready,
            "supports_test": self.supports_test,
            "supports_configure": self.supports_configure,
            "supports_enable": self.supports_enable,
            "supports_disable": self.supports_disable,
        }
        if self.enabled is not None:
            payload["enabled"] = self.enabled
        if self.configured is not None:
            payload["configured"] = self.configured
        if self.connected is not None:
            payload["connected"] = self.connected
        if self.runtime_status is not None:
            payload["runtime_status"] = self.runtime_status
        if self.error is not None:
            payload["error"] = self.error
        if self.details:
            payload["details"] = dict(self.details)
        return payload


def mcp_server_health(
    metadata: dict[str, Any],
    runtime_entry: dict[str, Any] | None,
) -> ConnectorHealthSnapshot:
    default_enabled = bool(metadata.get("default_enabled", True))
    auth_hint = str(metadata.get("auth_hint") or "")
    if runtime_entry is None:
        if not default_enabled:
            return ConnectorHealthSnapshot(
                state="disabled",
                summary="Packaged connector is disabled by default.",
                ready=False,
                enabled=False,
                configured=True,
                connected=False,
                runtime_status="disabled",
                supports_test=True,
                supports_enable=True,
                supports_disable=True,
            )
        return ConnectorHealthSnapshot(
            state="inactive",
            summary="Packaged connector is not registered in the MCP runtime yet.",
            ready=False,
            enabled=None,
            configured=True,
            connected=False,
            runtime_status="inactive",
            supports_test=True,
            supports_enable=True,
            supports_disable=True,
        )

    enabled = bool(runtime_entry.get("enabled", False))
    connected = bool(runtime_entry.get("connected", False))
    raw_status = str(runtime_entry.get("status") or ("connected" if connected else "disconnected"))
    status_message = str(runtime_entry.get("status_message") or "")

    if raw_status == "connected":
        return ConnectorHealthSnapshot(
            state="ready",
            summary="Connected and ready for runtime use.",
            ready=True,
            enabled=enabled,
            configured=True,
            connected=True,
            runtime_status=raw_status,
            supports_test=True,
            supports_enable=True,
            supports_disable=True,
            details={"tool_count": int(runtime_entry.get("tool_count") or 0)},
        )
    if raw_status == "auth_required":
        return ConnectorHealthSnapshot(
            state="requires_auth",
            summary=status_message or auth_hint or "Authentication is required before the connector can connect.",
            ready=False,
            enabled=enabled,
            configured=True,
            connected=False,
            runtime_status=raw_status,
            error=status_message or None,
            supports_test=True,
            supports_enable=True,
            supports_disable=True,
        )
    if raw_status == "error":
        return ConnectorHealthSnapshot(
            state="degraded",
            summary=status_message or "Connector failed to connect.",
            ready=False,
            enabled=enabled,
            configured=True,
            connected=False,
            runtime_status=raw_status,
            error=status_message or None,
            supports_test=True,
            supports_enable=True,
            supports_disable=True,
        )
    if not enabled:
        return ConnectorHealthSnapshot(
            state="disabled",
            summary="Connector is installed but disabled.",
            ready=False,
            enabled=False,
            configured=True,
            connected=False,
            runtime_status=raw_status,
            supports_test=True,
            supports_enable=True,
            supports_disable=True,
        )
    return ConnectorHealthSnapshot(
        state="inactive",
        summary="Connector is enabled but not currently connected.",
        ready=False,
        enabled=enabled,
        configured=True,
        connected=connected,
        runtime_status=raw_status,
        error=status_message or None,
        supports_test=True,
        supports_enable=True,
        supports_disable=True,
    )


def managed_connector_health(
    metadata: dict[str, Any],
    config_entry: dict[str, Any],
    config_errors: list[str],
    *,
    enabled: bool | None = None,
) -> ConnectorHealthSnapshot:
    if not config_entry:
        return ConnectorHealthSnapshot(
            state="requires_config",
            summary="Connector configuration is required before runtime use.",
            ready=False,
            enabled=enabled,
            configured=False,
            connected=False,
            supports_test=True,
            supports_configure=True,
            supports_enable=False if enabled is None else True,
            supports_disable=False if enabled is None else True,
        )
    if config_errors:
        return ConnectorHealthSnapshot(
            state="invalid",
            summary=config_errors[0],
            ready=False,
            enabled=enabled,
            configured=False,
            connected=False,
            error="; ".join(config_errors),
            supports_test=True,
            supports_configure=True,
            supports_enable=False if enabled is None else True,
            supports_disable=False if enabled is None else True,
        )
    if enabled is False:
        return ConnectorHealthSnapshot(
            state="disabled",
            summary="Managed connector is configured but disabled.",
            ready=False,
            enabled=False,
            configured=True,
            connected=False,
            supports_test=True,
            supports_configure=True,
            supports_enable=True,
            supports_disable=True,
        )
    return ConnectorHealthSnapshot(
        state="ready",
        summary="Managed connector configuration is valid and ready.",
        ready=True,
        enabled=enabled,
        configured=True,
        connected=False,
        supports_test=True,
        supports_configure=True,
        supports_enable=False if enabled is None else True,
        supports_disable=False if enabled is None else True,
        details={"provider": str(metadata.get("provider") or "")},
    )


def static_connector_health(
    *,
    active: bool,
    valid: bool,
    default_enabled: bool,
    active_summary: str,
    invalid_summary: str,
    disabled_summary: str,
    overridden_summary: str,
    supports_enable: bool = False,
    supports_disable: bool = False,
    supports_test: bool = False,
) -> ConnectorHealthSnapshot:
    if active:
        return ConnectorHealthSnapshot(
            state="ready",
            summary=active_summary,
            ready=True,
            enabled=True,
            configured=True,
            connected=None,
            supports_test=supports_test,
            supports_enable=supports_enable,
            supports_disable=supports_disable,
        )
    if not valid:
        return ConnectorHealthSnapshot(
            state="invalid",
            summary=invalid_summary,
            ready=False,
            enabled=default_enabled,
            configured=False,
            connected=None,
            supports_test=supports_test,
            supports_enable=supports_enable,
            supports_disable=supports_disable,
        )
    if not default_enabled:
        return ConnectorHealthSnapshot(
            state="disabled",
            summary=disabled_summary,
            ready=False,
            enabled=False,
            configured=True,
            connected=None,
            supports_test=supports_test,
            supports_enable=supports_enable,
            supports_disable=supports_disable,
        )
    return ConnectorHealthSnapshot(
        state="overridden",
        summary=overridden_summary,
        ready=False,
        enabled=True,
        configured=True,
        connected=None,
        supports_test=supports_test,
        supports_enable=supports_enable,
        supports_disable=supports_disable,
    )


def planned_connector_health(summary: str) -> ConnectorHealthSnapshot:
    return ConnectorHealthSnapshot(
        state="planned",
        summary=summary,
        ready=False,
        supports_test=False,
        supports_configure=False,
        supports_enable=False,
        supports_disable=False,
    )


def planned_configurable_connector_health(
    summary: str,
    *,
    enabled: bool,
    configured: bool,
    config_errors: list[str] | None = None,
    supports_test: bool = False,
) -> ConnectorHealthSnapshot:
    if config_errors:
        return ConnectorHealthSnapshot(
            state="invalid",
            summary=config_errors[0],
            ready=False,
            enabled=enabled,
            configured=False,
            connected=False,
            error="; ".join(config_errors),
            supports_test=supports_test,
            supports_configure=True,
            supports_enable=True,
            supports_disable=True,
        )
    if not configured:
        return ConnectorHealthSnapshot(
            state="requires_config",
            summary="Connector configuration is required before this surface can be activated.",
            ready=False,
            enabled=enabled,
            configured=False,
            connected=False,
            supports_test=supports_test,
            supports_configure=True,
            supports_enable=True,
            supports_disable=True,
        )
    if not enabled:
        return ConnectorHealthSnapshot(
            state="disabled",
            summary="Connector surface is configured but disabled.",
            ready=False,
            enabled=False,
            configured=True,
            connected=False,
            supports_test=supports_test,
            supports_configure=True,
            supports_enable=True,
            supports_disable=True,
        )
    return ConnectorHealthSnapshot(
        state="planned",
        summary=summary,
        ready=False,
        enabled=True,
        configured=True,
        connected=False,
        supports_test=supports_test,
        supports_configure=True,
        supports_enable=True,
        supports_disable=True,
    )
