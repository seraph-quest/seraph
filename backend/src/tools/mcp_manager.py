"""MCP (Model Context Protocol) server integration.

Manages connections to external MCP servers (e.g. Things3, GitHub) and exposes
their tools for use by the smolagents ToolCallingAgent.

Server configuration loaded from mcp-servers.json at startup. Servers can be
added/removed/toggled at runtime via the MCP API endpoints.
"""

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import os
import re
from pathlib import Path

from smolagents import MCPClient

from src.audit.runtime import log_integration_event_sync
from src.vault.repository import vault_repository

logger = logging.getLogger(__name__)

_ENV_VAR_RE = re.compile(r"\$\{(\w+)\}")
_VAULT_SECRET_RE = re.compile(r"\$\{vault:([A-Za-z0-9_.:-]+)\}")


class MCPManager:
    """Connects to multiple named MCP servers and provides their tools."""

    def __init__(self) -> None:
        self._clients: dict[str, MCPClient] = {}
        self._tools: dict[str, list] = {}
        self._config_path: str | None = None
        self._config: dict[str, dict] = {}
        self._status: dict[str, dict] = {}
        # Each: {"status": "connected"|"disconnected"|"auth_required"|"error", "error": str|None}

    # --- Config loading ---

    def load_config(self, config_path: str) -> None:
        """Load MCP server config from JSON file and connect enabled servers."""
        path = Path(config_path)
        self._config_path = config_path
        if not path.exists():
            logger.info("No MCP config at %s — skipping", config_path)
            return
        with open(path) as f:
            data = json.load(f)
        for name, server in data.get("mcpServers", {}).items():
            self._config[name] = server
            if not server.get("enabled", True):
                logger.info("MCP server '%s' disabled — skipping", name)
                continue
            self.connect(name, server["url"], headers=server.get("headers"))

    def _save_config(self) -> None:
        """Write current config back to the JSON file."""
        if not self._config_path:
            return
        path = Path(self._config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"mcpServers": self._config}
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")

    # --- Connection management ---

    @staticmethod
    def _resolve_env_vars(value: str) -> str:
        """Replace ${VAR} patterns with environment variable values."""
        return re.sub(
            _ENV_VAR_RE,
            lambda m: os.environ.get(m.group(1), m.group(0)),
            value,
        )

    @staticmethod
    def _run_async(coro):
        """Run an async coroutine from sync context, even under an active event loop."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()

    @staticmethod
    def _flatten_exception_text(exc: BaseException) -> str:
        """Collect str() of exc and all chained/grouped sub-exceptions into one lowercase string."""
        parts = [str(exc)]
        if isinstance(exc, BaseExceptionGroup):
            for sub in exc.exceptions:
                parts.append(MCPManager._flatten_exception_text(sub))
        if exc.__cause__:
            parts.append(MCPManager._flatten_exception_text(exc.__cause__))
        elif exc.__context__:
            parts.append(MCPManager._flatten_exception_text(exc.__context__))
        return " ".join(parts).lower()

    @staticmethod
    def _check_unresolved_vars(headers: dict[str, str] | None) -> list[str]:
        """Return list of env var names that are still unresolved after _resolve_env_vars."""
        if not headers:
            return []
        missing: list[str] = []
        for v in headers.values():
            resolved = MCPManager._resolve_env_vars(v)
            for m in re.finditer(_ENV_VAR_RE, resolved):
                missing.append(m.group(1))
        return missing

    @staticmethod
    def _check_missing_vault_secrets(headers: dict[str, str] | None) -> list[str]:
        """Return vault-backed secret keys referenced by headers that are missing."""
        if not headers:
            return []
        missing: list[str] = []
        checked: set[str] = set()
        for value in headers.values():
            if not isinstance(value, str):
                continue
            resolved = MCPManager._resolve_env_vars(value)
            for match in re.finditer(_VAULT_SECRET_RE, resolved):
                key = match.group(1)
                if key in checked:
                    continue
                checked.add(key)
                exists = MCPManager._run_async(vault_repository.exists(key))
                if not exists:
                    missing.append(key)
        return missing

    @staticmethod
    def inspect_headers(headers: dict[str, str] | None) -> tuple[list[str], list[str], list[str]]:
        """Inspect headers for missing env/vault credentials without resolving them."""
        if not headers:
            return [], [], []

        missing_env_vars = MCPManager._check_unresolved_vars(headers)
        missing_vault_keys = MCPManager._check_missing_vault_secrets(headers)
        credential_sources: set[str] = set()
        for key, value in headers.items():
            if not isinstance(value, str):
                continue
            resolved = MCPManager._resolve_env_vars(value)
            if re.search(_ENV_VAR_RE, value):
                credential_sources.add("env")
            if re.search(_VAULT_SECRET_RE, value) or re.search(_VAULT_SECRET_RE, resolved):
                credential_sources.add("vault")
            if (
                isinstance(key, str)
                and key.strip().lower() == "authorization"
                and not re.search(_ENV_VAR_RE, value)
                and not re.search(_VAULT_SECRET_RE, value)
                and not re.search(_VAULT_SECRET_RE, resolved)
            ):
                credential_sources.add("inline")

        return missing_env_vars, missing_vault_keys, sorted(credential_sources)

    @staticmethod
    def resolve_headers(
        headers: dict[str, str] | None,
    ) -> tuple[dict[str, str] | None, list[str], list[str], list[str]]:
        """Resolve env vars and vault placeholders inside headers.

        Returns: resolved headers, missing env vars, missing vault keys, credential sources.
        """
        if not headers:
            return None, [], [], []

        missing_env_vars, missing_vault_keys, credential_sources = MCPManager.inspect_headers(headers)
        if missing_env_vars or missing_vault_keys:
            return dict(headers), missing_env_vars, missing_vault_keys, credential_sources
        resolved_headers: dict[str, str] = {}

        for key, value in headers.items():
            if not isinstance(value, str):
                continue
            resolved = MCPManager._resolve_env_vars(value)

            def _replace_vault(match: re.Match[str]) -> str:
                secret_key = match.group(1)
                secret_value = MCPManager._run_async(vault_repository.get(secret_key))
                if secret_value is None:
                    return match.group(0)
                return secret_value

            resolved = re.sub(_VAULT_SECRET_RE, _replace_vault, resolved)
            resolved_headers[key] = resolved

        return resolved_headers, missing_env_vars, missing_vault_keys, credential_sources

    @staticmethod
    def _token_secret_key(name: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", name.strip()).strip("._-")
        digest = hashlib.sha1(name.strip().encode("utf-8")).hexdigest()[:10]
        return f"mcp.server.{normalized or 'default'}.{digest}.bearer_token"

    def connect(self, name: str, url: str, headers: dict[str, str] | None = None) -> None:
        """Connect to a named MCP server via HTTP/SSE. Fails gracefully."""
        try:
            resolved_headers, missing_vars, missing_vault_keys, credential_sources = self.resolve_headers(headers)
            missing_details: list[str] = []
            if missing_vars:
                missing_details.append(f"Missing environment variables: {', '.join(missing_vars)}")
            if missing_vault_keys:
                missing_details.append(f"Missing vault secrets: {', '.join(missing_vault_keys)}")
            if missing_details:
                msg = "; ".join(missing_details)
                self._status[name] = {"status": "auth_required", "error": msg}
                logger.warning("MCP server '%s' requires auth: %s", name, msg)
                log_integration_event_sync(
                    integration_type="mcp_server",
                    name=name,
                    outcome="auth_required",
                    details={
                        "url": url,
                        "error": msg,
                        "missing_env_vars": missing_vars,
                        "missing_vault_keys": missing_vault_keys,
                        "credential_sources": credential_sources,
                    },
                )
                return

            params: dict = {"url": url, "transport": "streamable-http"}
            if resolved_headers:
                params["headers"] = resolved_headers
            client = MCPClient(params, structured_output=False)
            tools = client.get_tools()
            self._clients[name] = client
            self._tools[name] = tools
            self._status[name] = {"status": "connected", "error": None}
            logger.info("Connected to MCP server '%s': %d tools loaded", name, len(tools))
            log_integration_event_sync(
                integration_type="mcp_server",
                name=name,
                outcome="connected",
                details={
                    "url": url,
                    "tool_count": len(tools),
                    "used_headers": bool(resolved_headers),
                    "credential_sources": credential_sources,
                },
            )
        except BaseException as exc:
            exc_str = self._flatten_exception_text(exc)
            if any(kw in exc_str for kw in ("401", "403", "unauthorized", "forbidden")):
                self._status[name] = {"status": "auth_required", "error": str(exc)}
                logger.warning("MCP server '%s' auth failed: %s", name, exc)
                log_integration_event_sync(
                    integration_type="mcp_server",
                    name=name,
                    outcome="auth_required",
                    details={
                        "url": url,
                        "error": str(exc),
                    },
                )
            else:
                self._status[name] = {"status": "error", "error": str(exc)}
                logger.warning("Failed to connect to MCP server '%s' at %s", name, url, exc_info=True)
                log_integration_event_sync(
                    integration_type="mcp_server",
                    name=name,
                    outcome="failed",
                    details={
                        "url": url,
                        "error": str(exc),
                    },
                )

    def disconnect(self, name: str) -> None:
        """Disconnect a specific named MCP server."""
        client = self._clients.pop(name, None)
        self._tools.pop(name, None)
        self._status[name] = {"status": "disconnected", "error": None}
        if client:
            try:
                client.disconnect()
            except Exception:
                logger.warning("Error disconnecting MCP client '%s'", name, exc_info=True)
        log_integration_event_sync(
            integration_type="mcp_server",
            name=name,
            outcome="disconnected",
            details={"had_client": client is not None},
        )

    def disconnect_all(self) -> None:
        """Disconnect all MCP servers."""
        for name in list(self._clients):
            self.disconnect(name)

    # --- Tool access ---

    def get_tools(self) -> list:
        """Return a flat list of tools from all connected servers."""
        tools: list = []
        for server_tools in self._tools.values():
            tools.extend(server_tools)
        return tools

    def get_server_tools(self, name: str) -> list:
        """Return tools for a specific named server."""
        return self._tools.get(name, [])

    def get_server_names(self) -> list[str]:
        """Return names of all configured servers (connected or not)."""
        return list(self._config.keys())

    def is_connected(self, name: str) -> bool:
        """Check if a server is currently connected."""
        return name in self._clients

    def get_config(self) -> list[dict]:
        """Return current server states for the API."""
        result = []
        for name, server in self._config.items():
            connected = name in self._clients
            tool_count = len(self._tools.get(name, []))
            status_info = self._status.get(name, {"status": "disconnected", "error": None})
            entry: dict = {
                "name": name,
                "url": server.get("url", ""),
                "enabled": server.get("enabled", True),
                "connected": connected,
                "tool_count": tool_count,
                "description": server.get("description", ""),
                "status": status_info["status"],
                "status_message": status_info.get("error"),
                "has_headers": "headers" in server,
                "auth_hint": server.get("auth_hint", ""),
                "extension_id": server.get("extension_id"),
                "extension_reference": server.get("extension_reference"),
                "extension_display_name": server.get("extension_display_name"),
                "source": server.get("source", "manual"),
            }
            result.append(entry)
        return result

    # --- Token management ---

    def set_token(self, name: str, token: str) -> bool:
        """Set auth token for a server. Reconnects if enabled. Returns False if not found."""
        if name not in self._config:
            return False
        server = self._config[name]
        secret_key = self._token_secret_key(name)
        self._run_async(vault_repository.store(
            secret_key,
            token,
            description=f"MCP bearer token for server '{name}'",
        ))
        if "headers" not in server:
            server["headers"] = {}
        server["headers"]["Authorization"] = f"Bearer ${{vault:{secret_key}}}"
        self._save_config()
        if server.get("enabled", True):
            self.disconnect(name)
            self.connect(name, server["url"], headers=server.get("headers"))
        log_integration_event_sync(
            integration_type="mcp_server",
            name=name,
            outcome="credential_updated",
            details={
                "credential_source": "vault",
                "header_name": "Authorization",
                "secret_key": secret_key,
                "enabled": bool(server.get("enabled", True)),
            },
        )
        return True

    # --- Runtime config mutations ---

    def add_server(self, name: str, url: str,
                   description: str = "", enabled: bool = True,
                   headers: dict[str, str] | None = None,
                   auth_hint: str = "",
                   extension_id: str | None = None,
                   extension_reference: str | None = None,
                   extension_display_name: str | None = None,
                   source: str | None = None) -> None:
        """Add a new server to config and optionally connect it."""
        self._config[name] = {
            "url": url,
            "enabled": enabled,
            "description": description,
        }
        if headers:
            self._config[name]["headers"] = headers
        if auth_hint:
            self._config[name]["auth_hint"] = auth_hint
        if extension_id:
            self._config[name]["extension_id"] = extension_id
        if extension_reference:
            self._config[name]["extension_reference"] = extension_reference
        if extension_display_name:
            self._config[name]["extension_display_name"] = extension_display_name
        if source:
            self._config[name]["source"] = source
        if enabled:
            self.connect(name, url, headers=headers)
        self._save_config()

    def update_server(self, name: str, **kwargs) -> bool:
        """Update server config. Returns False if server not found."""
        if name not in self._config:
            return False
        server = self._config[name]
        was_enabled = bool(server.get("enabled", True))
        previous_url = server.get("url")
        previous_headers = dict(server.get("headers", {})) if isinstance(server.get("headers"), dict) else None
        reconnected_from_toggle = False

        if "headers" in kwargs:
            if kwargs["headers"]:
                server["headers"] = kwargs["headers"]
            else:
                server.pop("headers", None)

        if "enabled" in kwargs:
            server["enabled"] = kwargs["enabled"]
            if kwargs["enabled"] and not was_enabled:
                self.connect(name, server["url"], headers=server.get("headers"))
                reconnected_from_toggle = True
            elif not kwargs["enabled"] and was_enabled:
                self.disconnect(name)

        if "url" in kwargs:
            server["url"] = kwargs["url"]
        if "description" in kwargs:
            server["description"] = kwargs["description"]
        if "auth_hint" in kwargs:
            if kwargs["auth_hint"]:
                server["auth_hint"] = kwargs["auth_hint"]
            else:
                server.pop("auth_hint", None)
        if "extension_id" in kwargs:
            if kwargs["extension_id"]:
                server["extension_id"] = kwargs["extension_id"]
            else:
                server.pop("extension_id", None)
        if "extension_reference" in kwargs:
            if kwargs["extension_reference"]:
                server["extension_reference"] = kwargs["extension_reference"]
            else:
                server.pop("extension_reference", None)
        if "extension_display_name" in kwargs:
            if kwargs["extension_display_name"]:
                server["extension_display_name"] = kwargs["extension_display_name"]
            else:
                server.pop("extension_display_name", None)
        if "source" in kwargs:
            if kwargs["source"]:
                server["source"] = kwargs["source"]
            else:
                server.pop("source", None)

        current_headers = dict(server.get("headers", {})) if isinstance(server.get("headers"), dict) else None
        reconnect_required = (
            bool(server.get("enabled", True))
            and not reconnected_from_toggle
            and (
                ("url" in kwargs and server.get("url") != previous_url)
                or ("headers" in kwargs and current_headers != previous_headers)
            )
        )
        if reconnect_required:
            self.disconnect(name)
            self.connect(name, server["url"], headers=server.get("headers"))

        self._save_config()
        return True

    def remove_server(self, name: str) -> bool:
        """Remove a server from config and disconnect it. Returns False if not found."""
        if name not in self._config:
            return False
        self.disconnect(name)
        del self._config[name]
        self._save_config()
        return True


mcp_manager = MCPManager()
