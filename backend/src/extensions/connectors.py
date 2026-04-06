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


@dataclass(frozen=True)
class ManagedConnectorField:
    key: str
    label: str
    secret: bool = False
    required: bool = True
    input: str = "text"

    def as_metadata(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "secret": self.secret,
            "required": self.required,
            "input": self.input,
        }


@dataclass(frozen=True)
class ManagedConnectorRuntimeRoute:
    contract: str
    tool_names: tuple[str, ...]
    result_kind: str = "external_record"
    query_param: str = "query"
    per_page_param: str = "perPage"

    def as_metadata(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "tool_names": list(self.tool_names),
            "result_kind": self.result_kind,
            "query_param": self.query_param,
            "per_page_param": self.per_page_param,
        }


@dataclass(frozen=True)
class ManagedConnectorRuntimeAdapter:
    kind: str
    server_names: tuple[str, ...]
    routes: tuple[ManagedConnectorRuntimeRoute, ...]

    def as_metadata(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "server_names": list(self.server_names),
            "routes": {
                route.contract: {
                    "tool_names": list(route.tool_names),
                    "result_kind": route.result_kind,
                    "query_param": route.query_param,
                    "per_page_param": route.per_page_param,
                }
                for route in self.routes
            },
        }


@dataclass(frozen=True)
class ManagedConnectorDefinition:
    name: str
    provider: str
    description: str = ""
    auth_kind: str = "api_key"
    enabled: bool = False
    capabilities: tuple[str, ...] = ()
    setup_steps: tuple[str, ...] = ()
    config_fields: tuple[ManagedConnectorField, ...] = ()
    runtime_adapter: ManagedConnectorRuntimeAdapter | None = None

    def as_metadata(self) -> dict[str, Any]:
        metadata = {
            "name": self.name,
            "provider": self.provider,
            "description": self.description,
            "auth_kind": self.auth_kind,
            "default_enabled": self.enabled,
            "capabilities": list(self.capabilities),
            "setup_steps": list(self.setup_steps),
            "config_fields": [field.as_metadata() for field in self.config_fields],
        }
        if self.runtime_adapter is not None:
            metadata["runtime_adapter"] = self.runtime_adapter.as_metadata()
        return metadata


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


def _parse_string_list(payload: Any, *, source: str, field_name: str) -> tuple[str, ...]:
    if payload is None:
        return ()
    if not isinstance(payload, list):
        raise ConnectorDefinitionError(f"{source}: managed connector {field_name} must be a list")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in payload:
        if not isinstance(value, str) or not value.strip():
            raise ConnectorDefinitionError(
                f"{source}: managed connector {field_name} entries must be non-empty strings"
            )
        item = value.strip()
        if item not in seen:
            normalized.append(item)
            seen.add(item)
    return tuple(normalized)


def parse_managed_connector_definition(payload: Any, *, source: str) -> ManagedConnectorDefinition:
    if not isinstance(payload, dict):
        raise ConnectorDefinitionError(f"{source}: managed connector definition must be an object")

    raw_name = payload.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ConnectorDefinitionError(f"{source}: managed connector definition must include a non-empty name")
    name = raw_name.strip()

    raw_provider = payload.get("provider")
    if not isinstance(raw_provider, str) or not raw_provider.strip():
        raise ConnectorDefinitionError(
            f"{source}: managed connector definition must include a non-empty provider"
        )
    provider = raw_provider.strip()

    raw_description = payload.get("description")
    description = raw_description.strip() if isinstance(raw_description, str) else ""

    raw_auth_kind = payload.get("auth_kind")
    if raw_auth_kind is not None and not isinstance(raw_auth_kind, str):
        raise ConnectorDefinitionError(f"{source}: managed connector auth_kind must be a string")
    auth_kind = raw_auth_kind.strip() if isinstance(raw_auth_kind, str) and raw_auth_kind.strip() else "api_key"
    if auth_kind not in {"none", "api_key", "oauth", "basic", "webhook"}:
        raise ConnectorDefinitionError(
            f"{source}: managed connector auth_kind '{auth_kind}' is not supported"
        )

    raw_enabled = payload.get("enabled")
    if raw_enabled is not None and not isinstance(raw_enabled, bool):
        raise ConnectorDefinitionError(f"{source}: managed connector enabled must be a boolean")
    enabled = False if raw_enabled is None else raw_enabled

    capabilities = _parse_string_list(payload.get("capabilities"), source=source, field_name="capabilities")
    setup_steps = _parse_string_list(payload.get("setup_steps"), source=source, field_name="setup_steps")

    raw_config_fields = payload.get("config_fields")
    if raw_config_fields is not None and not isinstance(raw_config_fields, list):
        raise ConnectorDefinitionError(f"{source}: managed connector config_fields must be a list")
    config_fields: list[ManagedConnectorField] = []
    for entry in raw_config_fields or []:
        if not isinstance(entry, dict):
            raise ConnectorDefinitionError(
                f"{source}: managed connector config_fields entries must be objects"
            )
        raw_key = entry.get("key")
        raw_label = entry.get("label")
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ConnectorDefinitionError(
                f"{source}: managed connector config field must include a non-empty key"
            )
        if not isinstance(raw_label, str) or not raw_label.strip():
            raise ConnectorDefinitionError(
                f"{source}: managed connector config field '{raw_key}' must include a non-empty label"
            )
        raw_secret = entry.get("secret")
        if raw_secret is not None and not isinstance(raw_secret, bool):
            raise ConnectorDefinitionError(
                f"{source}: managed connector config field '{raw_key}' secret must be a boolean"
            )
        if raw_secret is True:
            raise ConnectorDefinitionError(
                f"{source}: managed connector config field '{raw_key}' cannot be secret until vault-backed connector config is supported"
            )
        raw_required = entry.get("required")
        if raw_required is not None and not isinstance(raw_required, bool):
            raise ConnectorDefinitionError(
                f"{source}: managed connector config field '{raw_key}' required must be a boolean"
            )
        raw_input = entry.get("input")
        if raw_input is not None and not isinstance(raw_input, str):
            raise ConnectorDefinitionError(
                f"{source}: managed connector config field '{raw_key}' input must be a string"
            )
        input_kind = raw_input.strip() if isinstance(raw_input, str) and raw_input.strip() else "text"
        if input_kind not in {"text", "password", "url", "select"}:
            raise ConnectorDefinitionError(
                f"{source}: managed connector config field '{raw_key}' input '{input_kind}' is not supported"
            )
        config_fields.append(
            ManagedConnectorField(
                key=raw_key.strip(),
                label=raw_label.strip(),
                secret=bool(raw_secret) if raw_secret is not None else False,
                required=True if raw_required is None else raw_required,
                input=input_kind,
            )
        )

    runtime_adapter: ManagedConnectorRuntimeAdapter | None = None
    raw_runtime_adapter = payload.get("runtime_adapter")
    if raw_runtime_adapter is not None:
        if not isinstance(raw_runtime_adapter, dict):
            raise ConnectorDefinitionError(f"{source}: managed connector runtime_adapter must be an object")
        raw_kind = raw_runtime_adapter.get("kind")
        if not isinstance(raw_kind, str) or not raw_kind.strip():
            raise ConnectorDefinitionError(f"{source}: managed connector runtime_adapter kind must be a non-empty string")
        kind = raw_kind.strip()
        if kind != "mcp_server":
            raise ConnectorDefinitionError(
                f"{source}: managed connector runtime_adapter kind '{kind}' is not supported"
            )
        server_names = _parse_string_list(
            raw_runtime_adapter.get("server_names"),
            source=source,
            field_name="runtime_adapter.server_names",
        )
        if not server_names:
            raise ConnectorDefinitionError(
                f"{source}: managed connector runtime_adapter server_names must include at least one name"
            )
        raw_routes = raw_runtime_adapter.get("routes")
        if not isinstance(raw_routes, dict) or not raw_routes:
            raise ConnectorDefinitionError(
                f"{source}: managed connector runtime_adapter routes must be a non-empty object"
            )
        routes: list[ManagedConnectorRuntimeRoute] = []
        for raw_contract, raw_route in raw_routes.items():
            if not isinstance(raw_contract, str) or not raw_contract.strip():
                raise ConnectorDefinitionError(
                    f"{source}: managed connector runtime_adapter route keys must be non-empty strings"
                )
            if not isinstance(raw_route, dict):
                raise ConnectorDefinitionError(
                    f"{source}: managed connector runtime_adapter route '{raw_contract}' must be an object"
                )
            tool_names = _parse_string_list(
                raw_route.get("tool_names"),
                source=source,
                field_name=f"runtime_adapter.routes.{raw_contract}.tool_names",
            )
            if not tool_names:
                raise ConnectorDefinitionError(
                    f"{source}: managed connector runtime_adapter route '{raw_contract}' must include tool_names"
                )
            raw_result_kind = raw_route.get("result_kind")
            if raw_result_kind is not None and (
                not isinstance(raw_result_kind, str) or not raw_result_kind.strip()
            ):
                raise ConnectorDefinitionError(
                    f"{source}: managed connector runtime_adapter route '{raw_contract}' result_kind must be a non-empty string"
                )
            raw_query_param = raw_route.get("query_param")
            if raw_query_param is not None and (
                not isinstance(raw_query_param, str) or not raw_query_param.strip()
            ):
                raise ConnectorDefinitionError(
                    f"{source}: managed connector runtime_adapter route '{raw_contract}' query_param must be a non-empty string"
                )
            raw_per_page_param = raw_route.get("per_page_param")
            if raw_per_page_param is not None and (
                not isinstance(raw_per_page_param, str) or not raw_per_page_param.strip()
            ):
                raise ConnectorDefinitionError(
                    f"{source}: managed connector runtime_adapter route '{raw_contract}' per_page_param must be a non-empty string"
                )
            routes.append(
                ManagedConnectorRuntimeRoute(
                    contract=raw_contract.strip(),
                    tool_names=tool_names,
                    result_kind=(
                        raw_result_kind.strip()
                        if isinstance(raw_result_kind, str) and raw_result_kind.strip()
                        else "external_record"
                    ),
                    query_param=(
                        raw_query_param.strip()
                        if isinstance(raw_query_param, str) and raw_query_param.strip()
                        else "query"
                    ),
                    per_page_param=(
                        raw_per_page_param.strip()
                        if isinstance(raw_per_page_param, str) and raw_per_page_param.strip()
                        else "perPage"
                    ),
                )
            )
        runtime_adapter = ManagedConnectorRuntimeAdapter(
            kind=kind,
            server_names=server_names,
            routes=tuple(routes),
        )

    return ManagedConnectorDefinition(
        name=name,
        provider=provider,
        description=description,
        auth_kind=auth_kind,
        enabled=enabled,
        capabilities=capabilities,
        setup_steps=setup_steps,
        config_fields=tuple(config_fields),
        runtime_adapter=runtime_adapter,
    )


def load_mcp_server_definition(path: Path) -> MCPServerDefinition:
    payload = load_connector_payload(path)
    return parse_mcp_server_definition(payload, source=str(path))


def load_managed_connector_definition(path: Path) -> ManagedConnectorDefinition:
    payload = load_connector_payload(path)
    return parse_managed_connector_definition(payload, source=str(path))
