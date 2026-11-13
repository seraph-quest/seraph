#!/bin/bash
# ==============================================================================
#
#         Seraph Native macOS Screen Daemon — Quick Start
#
# ==============================================================================
#
# Captures the active window (app name + title) and posts to the Seraph backend.
# Runs natively on macOS — outside Docker.
#
# Usage:
#   ./daemon/run.sh                     # Default: window polling every 5s
#   ./daemon/run.sh --verbose           # Log every context POST
#   ./daemon/run.sh --interval 3        # Poll every 3 seconds
#
# Screen analysis (opt-in, captures on context switch, requires Screen Recording permission):
#   ./daemon/run.sh --ocr --verbose                        # On-device Apple Vision OCR (no cloud call)
#   ./daemon/run.sh --ocr --blocklist-file ~/blocklist.json  # With a custom app blocklist
#   Set SERAPH_EDGE_DEVICE_ID, SERAPH_EDGE_PAIRING_ID and SERAPH_EDGE_CREDENTIAL_FILE
#   to upload allowed captures to the local backend (ADR-035).
#
#   ./daemon/run.sh --help              # Show all options
#
# Prerequisites:
#   - macOS 13+ with Python 3.12+
#   - Accessibility permission for your terminal app
#     (System Settings > Privacy & Security > Accessibility)
#   - Screen Recording permission (only if using --ocr)
#
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
export UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/seraph-uv-cache}"

# Create venv if it doesn't exist
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    uv venv "$VENV_DIR"
fi

# Sync dependencies
echo "Syncing dependencies..."
uv pip install -q -r "$SCRIPT_DIR/requirements.txt" -p "$VENV_DIR/bin/python"

# Run the daemon, passing through all arguments
exec "$VENV_DIR/bin/python" "$SCRIPT_DIR/seraph_daemon.py" "$@"
