"""Typed connector definition helpers for extension packages."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConnectorDefinitionError(ValueError):
    """Raised when a connector contribution cannot be parsed into a typed definition."""


@dataclass(frozen=True)
class MCPServerDefinition:
    name: str
    url: str
    description: str = ""
    enabled: bool = True
    headers: dict[str, str] | None = None
    auth_hint: str = ""
    transport: str | None = None

    def as_metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "description": self.description,
            "default_enabled": self.enabled,
            "headers": dict(self.headers) if self.headers else None,
            "auth_hint": self.auth_hint,
            "transport": self.transport,
        }


def load_connector_payload(path: Path) -> Any:
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConnectorDefinitionError(f"{path}: connector file is not valid UTF-8 text") from exc
    except OSError as exc:
        raise ConnectorDefinitionError(f"{path}: failed to read connector file ({exc})") from exc

    try:
        payload = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ConnectorDefinitionError(f"{path}: connector file could not be parsed ({exc})") from exc

    if payload is None:
        raise ConnectorDefinitionError(f"{path}: connector file is empty")
    return payload


def parse_mcp_server_definition(payload: Any, *, source: str) -> MCPServerDefinition:
    if not isinstance(payload, dict):
        raise ConnectorDefinitionError(f"{source}: MCP server definition must be an object")

    raw_name = payload.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ConnectorDefinitionError(f"{source}: MCP server definition must include a non-empty name")
    name = raw_name.strip()

    raw_url = payload.get("url")
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ConnectorDefinitionError(f"{source}: MCP server definition must include a non-empty url")
    url = raw_url.strip()

    raw_description = payload.get("description")
    description = raw_description.strip() if isinstance(raw_description, str) else ""

    raw_headers = payload.get("headers")
    headers: dict[str, str] | None = None
    if raw_headers is not None:
        if not isinstance(raw_headers, dict):
            raise ConnectorDefinitionError(f"{source}: MCP server headers must be an object")
        normalized_headers: dict[str, str] = {}
        for key, value in raw_headers.items():
            if not isinstance(key, str) or not key.strip():
                raise ConnectorDefinitionError(f"{source}: MCP server header names must be non-empty strings")
            if not isinstance(value, str):
                raise ConnectorDefinitionError(f"{source}: MCP server header values must be strings")
            normalized_headers[key.strip()] = value
        headers = normalized_headers or None

    raw_auth_hint = payload.get("auth_hint")
    if raw_auth_hint is not None and not isinstance(raw_auth_hint, str):
        raise ConnectorDefinitionError(f"{source}: MCP server auth_hint must be a string")
    auth_hint = raw_auth_hint.strip() if isinstance(raw_auth_hint, str) else ""

    raw_transport = payload.get("transport")
    if raw_transport is not None and not isinstance(raw_transport, str):
        raise ConnectorDefinitionError(f"{source}: MCP server transport must be a string")
    transport = raw_transport.strip() if isinstance(raw_transport, str) and raw_transport.strip() else None
    if transport is not None and transport != "streamable-http":
        raise ConnectorDefinitionError(
            f"{source}: MCP server transport '{transport}' is not supported; use streamable-http"
        )

    raw_enabled = payload.get("enabled")
    if raw_enabled is not None and not isinstance(raw_enabled, bool):
        raise ConnectorDefinitionError(f"{source}: MCP server enabled must be a boolean")
    enabled = True if raw_enabled is None else raw_enabled

    return MCPServerDefinition(
        name=name,
        url=url,
        description=description,
        enabled=enabled,
        headers=headers,
        auth_hint=auth_hint,
        transport=transport,
    )


def load_mcp_server_definition(path: Path) -> MCPServerDefinition:
    payload = load_connector_payload(path)
    return parse_mcp_server_definition(payload, source=str(path))
