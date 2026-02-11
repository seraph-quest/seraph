#!/usr/bin/env bash
#
# mcp.sh — Manage MCP servers for Seraph
#
# Usage:
#   ./mcp.sh list                                        List configured servers
#   ./mcp.sh add <name> <url> [--building B] [--desc D]  Add a server
#   ./mcp.sh remove <name>                               Remove a server
#   ./mcp.sh enable <name>                               Enable a server
#   ./mcp.sh disable <name>                              Disable a server
#   ./mcp.sh test <name>                                 Test connection (needs backend)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Config file locations — try Docker volume first, then local
if [[ -d "$SCRIPT_DIR/docker-data/dev/backend/data" ]]; then
    CONFIG_DIR="$SCRIPT_DIR/docker-data/dev/backend/data"
else
    CONFIG_DIR="$SCRIPT_DIR/backend/data"
fi
CONFIG_FILE="$CONFIG_DIR/mcp-servers.json"

API_URL="${SERAPH_API_URL:-http://localhost:8004}"

VALID_BUILDINGS=("house-1" "church" "house-2" "forge" "tower" "clock" "mailbox")

# --- Helpers ---

ensure_jq() {
    if ! command -v jq &>/dev/null; then
        echo "Error: jq is required. Install it with: brew install jq" >&2
        exit 1
    fi
}

ensure_config() {
    mkdir -p "$CONFIG_DIR"
    if [[ ! -f "$CONFIG_FILE" ]]; then
        echo '{"mcpServers": {}}' > "$CONFIG_FILE"
    fi
}

hot_reload() {
    # If backend is running, notify it to refresh tool registry
    if curl -sf "$API_URL/health" &>/dev/null; then
        echo "  ↳ Backend is running — changes will take effect on next restart"
        echo "    (or use the Settings UI / API for immediate hot-reload)"
    fi
}

validate_building() {
    local building="$1"
    for b in "${VALID_BUILDINGS[@]}"; do
        if [[ "$b" == "$building" ]]; then
            return 0
        fi
    done
    echo "Error: Invalid building '$building'. Choose from: ${VALID_BUILDINGS[*]}" >&2
    exit 1
}

# --- Commands ---

cmd_list() {
    ensure_jq
    ensure_config

    if [[ ! -f "$CONFIG_FILE" ]] || [[ "$(jq '.mcpServers | length' "$CONFIG_FILE")" == "0" ]]; then
        echo "No MCP servers configured."
        echo ""
        echo "Add one with: ./mcp.sh add <name> <url>"
        echo "Example:      ./mcp.sh add things3 http://host.docker.internal:9100/mcp --building church"
        return
    fi

    printf "%-15s %-8s %-10s %s\n" "NAME" "ENABLED" "BUILDING" "URL"
    printf "%-15s %-8s %-10s %s\n" "----" "-------" "--------" "---"

    jq -r '.mcpServers | to_entries[] | [.key, (if .value.enabled == false then "no" else "yes" end), (.value.building // "-"), .value.url] | @tsv' "$CONFIG_FILE" |
        while IFS=$'\t' read -r name enabled building url; do
            printf "%-15s %-8s %-10s %s\n" "$name" "$enabled" "$building" "$url"
        done

    echo ""
    # Show description if any
    local has_desc
    has_desc=$(jq '[.mcpServers | to_entries[] | select(.value.description and .value.description != "")] | length' "$CONFIG_FILE")
    if [[ "$has_desc" -gt 0 ]]; then
        jq -r '.mcpServers | to_entries[] | select(.value.description and .value.description != "") | "  \(.key): \(.value.description)"' "$CONFIG_FILE"
        echo ""
    fi

    echo "Config: $CONFIG_FILE"
}

cmd_add() {
    ensure_jq
    ensure_config

    if [[ $# -lt 2 ]]; then
        echo "Usage: ./mcp.sh add <name> <url> [--building B] [--desc D]" >&2
        exit 1
    fi

    local name="$1" url="$2"
    shift 2

    local building="" description=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --building|-b)
                building="$2"
                validate_building "$building"
                shift 2
                ;;
            --desc|--description|-d)
                description="$2"
                shift 2
                ;;
            *)
                echo "Unknown option: $1" >&2
                exit 1
                ;;
        esac
    done

    # Check if server already exists
    if jq -e ".mcpServers[\"$name\"]" "$CONFIG_FILE" &>/dev/null; then
        echo "Error: Server '$name' already exists. Remove it first with: ./mcp.sh remove $name" >&2
        exit 1
    fi

    # Build the server object
    local server_json
    server_json=$(jq -n \
        --arg url "$url" \
        --arg building "$building" \
        --arg description "$description" \
        '{url: $url, enabled: true} +
         (if $building != "" then {building: $building} else {} end) +
         (if $description != "" then {description: $description} else {} end)')

    # Add to config
    jq --arg name "$name" --argjson server "$server_json" \
        '.mcpServers[$name] = $server' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && \
        mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"

    echo "Added MCP server '$name' → $url"
    [[ -n "$building" ]] && echo "  Building: $building"
    [[ -n "$description" ]] && echo "  Description: $description"
    hot_reload
}

cmd_remove() {
    ensure_jq
    ensure_config

    if [[ $# -lt 1 ]]; then
        echo "Usage: ./mcp.sh remove <name>" >&2
        exit 1
    fi

    local name="$1"

    if ! jq -e ".mcpServers[\"$name\"]" "$CONFIG_FILE" &>/dev/null; then
        echo "Error: Server '$name' not found." >&2
        exit 1
    fi

    jq --arg name "$name" 'del(.mcpServers[$name])' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && \
        mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"

    echo "Removed MCP server '$name'"
    hot_reload
}

cmd_enable() {
    ensure_jq
    ensure_config

    if [[ $# -lt 1 ]]; then
        echo "Usage: ./mcp.sh enable <name>" >&2
        exit 1
    fi

    local name="$1"

    if ! jq -e ".mcpServers[\"$name\"]" "$CONFIG_FILE" &>/dev/null; then
        echo "Error: Server '$name' not found." >&2
        exit 1
    fi

    jq --arg name "$name" '.mcpServers[$name].enabled = true' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && \
        mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"

    echo "Enabled MCP server '$name'"
    hot_reload
}

cmd_disable() {
    ensure_jq
    ensure_config

    if [[ $# -lt 1 ]]; then
        echo "Usage: ./mcp.sh disable <name>" >&2
        exit 1
    fi

    local name="$1"

    if ! jq -e ".mcpServers[\"$name\"]" "$CONFIG_FILE" &>/dev/null; then
        echo "Error: Server '$name' not found." >&2
        exit 1
    fi

    jq --arg name "$name" '.mcpServers[$name].enabled = false' "$CONFIG_FILE" > "$CONFIG_FILE.tmp" && \
        mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"

    echo "Disabled MCP server '$name'"
    hot_reload
}

cmd_test() {
    if [[ $# -lt 1 ]]; then
        echo "Usage: ./mcp.sh test <name>" >&2
        exit 1
    fi

    local name="$1"

    if ! curl -sf "$API_URL/health" &>/dev/null; then
        echo "Error: Backend not reachable at $API_URL" >&2
        echo "Start the backend first with: ./manage.sh -e dev up -d" >&2
        exit 1
    fi

    echo "Testing connection to '$name'..."
    local response
    response=$(curl -sf -X POST "$API_URL/api/mcp/servers/$name/test" 2>&1) || {
        echo "Error: Test failed — server '$name' not found or connection refused" >&2
        exit 1
    }

    local status tool_count
    status=$(echo "$response" | jq -r '.status // "unknown"')
    tool_count=$(echo "$response" | jq -r '.tool_count // 0')

    if [[ "$status" == "ok" ]]; then
        echo "OK — $tool_count tools available"
        echo "$response" | jq -r '.tools[]' 2>/dev/null | head -20 | sed 's/^/  · /'
        local total
        total=$(echo "$response" | jq '.tools | length')
        if [[ "$total" -gt 20 ]]; then
            echo "  ... and $((total - 20)) more"
        fi
    else
        echo "Failed: $response" >&2
        exit 1
    fi
}

cmd_help() {
    cat <<'HELP'
mcp.sh — Manage MCP servers for Seraph

Commands:
  list                                        List configured servers
  add <name> <url> [--building B] [--desc D]  Add a new server
  remove <name>                               Remove a server
  enable <name>                               Enable a disabled server
  disable <name>                              Disable a server (keeps config)
  test <name>                                 Test connection (backend must be running)

Buildings:
  house-1   (at-well)      house-2  (at-signpost)    church  (at-bench)
  forge     (at-forge)     tower    (at-tower)        clock   (at-clock)
  mailbox   (at-mailbox)

Examples:
  ./mcp.sh add things3 http://host.docker.internal:9100/mcp --building church --desc "Things3 task manager"
  ./mcp.sh add github http://github-mcp:8090/mcp --building tower
  ./mcp.sh disable github
  ./mcp.sh test things3
  ./mcp.sh list

Environment:
  SERAPH_API_URL  Backend URL for 'test' command (default: http://localhost:8004)

Requires: jq (brew install jq)
HELP
}

# --- Main ---

case "${1:-help}" in
    list)     shift; cmd_list "$@" ;;
    add)      shift; cmd_add "$@" ;;
    remove)   shift; cmd_remove "$@" ;;
    enable)   shift; cmd_enable "$@" ;;
    disable)  shift; cmd_disable "$@" ;;
    test)     shift; cmd_test "$@" ;;
    help|-h|--help) cmd_help ;;
    *)
        echo "Unknown command: $1" >&2
        echo "Run ./mcp.sh help for usage." >&2
        exit 1
        ;;
esac
