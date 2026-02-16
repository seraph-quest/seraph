#!/bin/bash
# ==============================================================================
#
#         Seraph Stdio-to-HTTP MCP Proxy — Quick Start
#
# ==============================================================================
#
# Reads data/stdio-proxies.json and spawns each enabled stdio MCP server as
# an HTTP endpoint. Runs natively on macOS so tools can access system APIs.
#
# Usage:
#   ./mcp-servers/stdio-proxy/run.sh                  # Default
#   ./mcp-servers/stdio-proxy/run.sh --verbose         # Verbose logging
#   ./mcp-servers/stdio-proxy/run.sh --config /path    # Custom config path
#
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# Create venv if it doesn't exist
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    uv venv "$VENV_DIR"
fi

# Sync dependencies
echo "Syncing dependencies..."
uv pip install -q -r "$SCRIPT_DIR/requirements.txt" -p "$VENV_DIR/bin/python"

# Run the proxy, passing through all arguments
exec "$VENV_DIR/bin/python" "$SCRIPT_DIR/proxy.py" "$@"
