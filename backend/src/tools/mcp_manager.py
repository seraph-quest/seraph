"""MCP (Model Context Protocol) server integration.

Manages connections to external MCP servers (e.g. Things3, GitHub) and exposes
their tools for use by the smolagents ToolCallingAgent.

Server configuration loaded from mcp-servers.json at startup. Servers can be
added/removed/toggled at runtime via the MCP API endpoints.
"""

import json
import logging
from pathlib import Path

from smolagents import MCPClient

logger = logging.getLogger(__name__)


class MCPManager:
    """Connects to multiple named MCP servers and provides their tools."""

    def __init__(self) -> None:
        self._clients: dict[str, MCPClient] = {}
        self._tools: dict[str, list] = {}
        self._buildings: dict[str, str] = {}
        self._config_path: str | None = None
        self._config: dict[str, dict] = {}

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
            self.connect(name, server["url"])
            if server.get("building"):
                self._buildings[name] = server["building"]

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

    def connect(self, name: str, url: str) -> None:
        """Connect to a named MCP server via HTTP/SSE. Fails gracefully."""
        try:
            client = MCPClient({"url": url, "transport": "streamable-http"})
            tools = client.get_tools()
            self._clients[name] = client
            self._tools[name] = tools
            logger.info("Connected to MCP server '%s': %d tools loaded", name, len(tools))
        except Exception:
            logger.warning("Failed to connect to MCP server '%s' at %s", name, url, exc_info=True)

    def disconnect(self, name: str) -> None:
        """Disconnect a specific named MCP server."""
        client = self._clients.pop(name, None)
        self._tools.pop(name, None)
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

    # --- Building / metadata ---

    def get_server_building(self, name: str) -> str | None:
        """Get the village building assigned to a server."""
        return self._buildings.get(name)

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
            result.append({
                "name": name,
                "url": server.get("url", ""),
                "enabled": server.get("enabled", True),
                "connected": connected,
                "tool_count": tool_count,
                "building": server.get("building"),
                "description": server.get("description", ""),
            })
        return result

    # --- Runtime config mutations ---

    def add_server(self, name: str, url: str, building: str | None = None,
                   description: str = "", enabled: bool = True) -> None:
        """Add a new server to config and optionally connect it."""
        self._config[name] = {
            "url": url,
            "enabled": enabled,
            "description": description,
        }
        if building:
            self._config[name]["building"] = building
            self._buildings[name] = building
        if enabled:
            self.connect(name, url)
        self._save_config()

    def update_server(self, name: str, **kwargs) -> bool:
        """Update server config. Returns False if server not found."""
        if name not in self._config:
            return False
        server = self._config[name]

        if "enabled" in kwargs:
            was_enabled = server.get("enabled", True)
            server["enabled"] = kwargs["enabled"]
            if kwargs["enabled"] and not was_enabled:
                self.connect(name, server["url"])
            elif not kwargs["enabled"] and was_enabled:
                self.disconnect(name)

        if "building" in kwargs:
            server["building"] = kwargs["building"]
            if kwargs["building"]:
                self._buildings[name] = kwargs["building"]
            else:
                self._buildings.pop(name, None)

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
        self._buildings.pop(name, None)
        self._save_config()
        return True


mcp_manager = MCPManager()
