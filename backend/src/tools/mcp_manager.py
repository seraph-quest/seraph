"""MCP (Model Context Protocol) server integration.

Manages connections to external MCP servers (e.g. Things3, GitHub) and exposes
their tools for use by the smolagents ToolCallingAgent.

Server configuration loaded from mcp-servers.json at startup. Servers can be
added/removed/toggled at runtime via the MCP API endpoints.
"""

import json
import logging
import os
import re
from pathlib import Path

from smolagents import MCPClient

logger = logging.getLogger(__name__)


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
            r"\$\{(\w+)\}",
            lambda m: os.environ.get(m.group(1), m.group(0)),
            value,
        )

    @staticmethod
    def _check_unresolved_vars(headers: dict[str, str] | None) -> list[str]:
        """Return list of env var names that are still unresolved after _resolve_env_vars."""
        if not headers:
            return []
        missing: list[str] = []
        for v in headers.values():
            resolved = MCPManager._resolve_env_vars(v)
            for m in re.finditer(r"\$\{(\w+)\}", resolved):
                missing.append(m.group(1))
        return missing

    def connect(self, name: str, url: str, headers: dict[str, str] | None = None) -> None:
        """Connect to a named MCP server via HTTP/SSE. Fails gracefully."""
        try:
            # Check for unresolved env vars before attempting connection
            missing_vars = self._check_unresolved_vars(headers)
            if missing_vars:
                msg = f"Missing environment variables: {', '.join(missing_vars)}"
                self._status[name] = {"status": "auth_required", "error": msg}
                logger.warning("MCP server '%s' requires auth: %s", name, msg)
                return

            params: dict = {"url": url, "transport": "streamable-http"}
            if headers:
                params["headers"] = {
                    k: self._resolve_env_vars(v) for k, v in headers.items()
                }
            client = MCPClient(params, structured_output=False)
            tools = client.get_tools()
            self._clients[name] = client
            self._tools[name] = tools
            self._status[name] = {"status": "connected", "error": None}
            logger.info("Connected to MCP server '%s': %d tools loaded", name, len(tools))
        except Exception as exc:
            exc_str = str(exc).lower()
            if any(kw in exc_str for kw in ("401", "403", "unauthorized", "forbidden")):
                self._status[name] = {"status": "auth_required", "error": str(exc)}
                logger.warning("MCP server '%s' auth failed: %s", name, exc)
            else:
                self._status[name] = {"status": "error", "error": str(exc)}
                logger.warning("Failed to connect to MCP server '%s' at %s", name, url, exc_info=True)

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
            }
            result.append(entry)
        return result

    # --- Token management ---

    def set_token(self, name: str, token: str) -> bool:
        """Set auth token for a server. Reconnects if enabled. Returns False if not found."""
        if name not in self._config:
            return False
        server = self._config[name]
        if "headers" not in server:
            server["headers"] = {}
        server["headers"]["Authorization"] = f"Bearer {token}"
        self._save_config()
        if server.get("enabled", True):
            self.disconnect(name)
            self.connect(name, server["url"], headers=server.get("headers"))
        return True

    # --- Runtime config mutations ---

    def add_server(self, name: str, url: str,
                   description: str = "", enabled: bool = True,
                   headers: dict[str, str] | None = None) -> None:
        """Add a new server to config and optionally connect it."""
        self._config[name] = {
            "url": url,
            "enabled": enabled,
            "description": description,
        }
        if headers:
            self._config[name]["headers"] = headers
        if enabled:
            self.connect(name, url, headers=headers)
        self._save_config()

    def update_server(self, name: str, **kwargs) -> bool:
        """Update server config. Returns False if server not found."""
        if name not in self._config:
            return False
        server = self._config[name]

        if "headers" in kwargs:
            if kwargs["headers"]:
                server["headers"] = kwargs["headers"]
            else:
                server.pop("headers", None)

        if "enabled" in kwargs:
            was_enabled = server.get("enabled", True)
            server["enabled"] = kwargs["enabled"]
            if kwargs["enabled"] and not was_enabled:
                self.connect(name, server["url"], headers=server.get("headers"))
            elif not kwargs["enabled"] and was_enabled:
                self.disconnect(name)

        if "url" in kwargs:
            server["url"] = kwargs["url"]
        if "description" in kwargs:
            server["description"] = kwargs["description"]

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
