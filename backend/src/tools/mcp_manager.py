"""MCP (Model Context Protocol) server integration.

Manages connections to external MCP servers (e.g. Things3) and exposes
their tools for use by the smolagents ToolCallingAgent.
"""

import logging

from smolagents import MCPClient

logger = logging.getLogger(__name__)


class MCPManager:
    """Connects to MCP servers and provides their tools."""

    def __init__(self) -> None:
        self._client: MCPClient | None = None
        self._tools: list = []

    def connect(self, url: str) -> None:
        """Connect to an MCP server via HTTP/SSE. Fails gracefully."""
        try:
            self._client = MCPClient({"url": url, "transport": "streamable-http"})
            self._tools = self._client.get_tools()
            logger.info(f"Connected to MCP server: {len(self._tools)} tools loaded")
        except Exception:
            logger.warning("Failed to connect to MCP server at %s", url, exc_info=True)
            self._client = None
            self._tools = []

    def disconnect(self) -> None:
        """Disconnect from the MCP server."""
        if self._client:
            try:
                self._client.disconnect()
            except Exception:
                logger.warning("Error disconnecting MCP client", exc_info=True)
            self._client = None
            self._tools = []

    def get_tools(self) -> list:
        """Return tools loaded from the MCP server."""
        return self._tools


mcp_manager = MCPManager()
