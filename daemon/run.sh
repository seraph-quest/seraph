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
#   ./daemon/run.sh                  # Default: poll every 5s, backend at :8004
#   ./daemon/run.sh --verbose        # Log every context POST
#   ./daemon/run.sh --interval 3     # Poll every 3 seconds
#   ./daemon/run.sh --help           # Show all options
#
# Prerequisites:
#   - macOS 13+ with Python 3.12+
#   - Accessibility permission for your terminal app
#     (System Settings > Privacy & Security > Accessibility)
#
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Sync dependencies via uv
echo "Syncing dependencies..."
uv pip install -q -r "$SCRIPT_DIR/requirements.txt" --directory "$SCRIPT_DIR"

# Run the daemon, passing through all arguments
exec uv run --directory "$SCRIPT_DIR" python "$SCRIPT_DIR/seraph_daemon.py" "$@"
