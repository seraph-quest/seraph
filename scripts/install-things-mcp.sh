#!/usr/bin/env bash
# Install things-mcp as a macOS LaunchAgent so it starts automatically at login
# and has its own Full Disk Access grant (no need to give FDA to iTerm/Terminal).
set -euo pipefail

PLIST_NAME="com.seraph.things-mcp"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$PLIST_NAME.plist"
LOG_DIR="$HOME/Library/Logs/things-mcp"
UVX_PATH="$(command -v uvx 2>/dev/null || true)"

if [ -z "$UVX_PATH" ]; then
  echo "Error: uvx not found. Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi

echo "Creating LaunchAgent for things-mcp..."
echo "  uvx path:  $UVX_PATH"
echo "  plist:     $PLIST_PATH"
echo "  logs:      $LOG_DIR/"

mkdir -p "$PLIST_DIR" "$LOG_DIR"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$PLIST_NAME</string>

  <key>ProgramArguments</key>
  <array>
    <string>$UVX_PATH</string>
    <string>things-mcp</string>
  </array>

  <key>EnvironmentVariables</key>
  <dict>
    <key>THINGS_MCP_TRANSPORT</key>
    <string>http</string>
    <key>THINGS_MCP_PORT</key>
    <string>9100</string>
  </dict>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>StandardOutPath</key>
  <string>$LOG_DIR/stdout.log</string>

  <key>StandardErrorPath</key>
  <string>$LOG_DIR/stderr.log</string>
</dict>
</plist>
EOF

# Unload if already loaded, then load
launchctl bootout "gui/$(id -u)/$PLIST_NAME" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"

echo ""
echo "Done! things-mcp is now running on port 9100."
echo ""
echo "IMPORTANT — Grant Full Disk Access to uvx:"
echo "  1. Open System Settings → Privacy & Security → Full Disk Access"
echo "  2. Click '+' and press Cmd+Shift+G to type a path"
echo "  3. Enter: $UVX_PATH"
echo "  4. Toggle it ON"
echo "  5. Restart the service:"
echo "     launchctl kickstart -k gui/$(id -u)/$PLIST_NAME"
echo ""
echo "Verify it works:"
echo "  curl -s http://localhost:9100/mcp -X POST \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -H 'Accept: application/json, text/event-stream' \\"
echo "    -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{},\"clientInfo\":{\"name\":\"test\",\"version\":\"1.0\"}}}'"
echo ""
echo "Logs:  tail -f $LOG_DIR/stderr.log"
echo "Stop:  launchctl bootout gui/$(id -u)/$PLIST_NAME"
