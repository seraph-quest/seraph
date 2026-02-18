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
#   ./daemon/run.sh --ocr --verbose                        # Local Apple Vision (free, offline, ~200ms)
#   ./daemon/run.sh --ocr --ocr-provider openrouter        # Cloud via Gemini (structured JSON, ~$1.30/mo)
#   ./daemon/run.sh --ocr --ocr-provider openrouter \
#     --blocklist-file ~/blocklist.json --verbose           # Cloud with custom app blocklist
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

# Create venv if it doesn't exist
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    uv venv "$VENV_DIR"
fi

# Sync dependencies
echo "Syncing dependencies..."
uv pip install -q -r "$SCRIPT_DIR/requirements.txt" -p "$VENV_DIR/bin/python"

# Forward OPENROUTER_API_KEY if set (used by --ocr-provider openrouter)
export OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}"

# Run the daemon, passing through all arguments
exec "$VENV_DIR/bin/python" "$SCRIPT_DIR/seraph_daemon.py" "$@"
