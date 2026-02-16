#!/bin/bash

# ==============================================================================
#
#         Unified Environment Management Script for the MESH Project
#
# ==============================================================================
#
# This script is the single entry point for managing all Docker environments
# and the native macOS screen daemon.
#
#
# Usage:
#   ./manage.sh -e [dev|prod] up -d     - Start Docker services (+ daemon if DAEMON_ENABLED=true).
#   ./manage.sh -e [dev|prod] down      - Stop Docker services + daemon.
#   ./manage.sh -e [dev|prod] logs      - View Docker logs.
#   ./manage.sh -e [dev|prod] build     - Build or rebuild Docker services.
#   ./manage.sh -e [dev|prod] daemon start|stop|status|logs - Manage screen daemon.
#   ./manage.sh -e [dev|prod] proxy start|stop|status|logs  - Manage stdio MCP proxy.
#
# Examples:
#   ./manage.sh -e dev up -d            - Starts the development stack.
#   ./manage.sh -e dev daemon status    - Check if daemon is running.
#   ./manage.sh -e dev daemon logs      - Tail daemon log file.
#   ./manage.sh -e dev proxy start      - Start stdio-to-HTTP MCP proxy.
#   ./manage.sh -e dev proxy logs       - Tail proxy log file.
#   ./manage.sh -e prod down            - Stop everything.
#
# ==============================================================================

# --- Configuration ---
PROG_NAME=$(basename "$0")
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_DIR="$SCRIPT_DIR/daemon"
DAEMON_PID_FILE="$DAEMON_DIR/.seraph-daemon.pid"
DAEMON_LOG_FILE="$DAEMON_DIR/seraph-daemon.log"
PROXY_DIR="$SCRIPT_DIR/mcp-servers/stdio-proxy"
PROXY_PID_FILE="$PROXY_DIR/.seraph-proxy.pid"
PROXY_LOG_FILE="$PROXY_DIR/seraph-proxy.log"

# --- Helper Functions ---
function display_help() {
    echo "Usage: $PROG_NAME -e [dev|prod] [COMMAND] [ARGS...]"
    echo
    echo "This script is the official entry point for managing the project's Docker environments"
    echo "and the native macOS screen daemon."
    echo
    echo "Options:"
    echo "  -e      Specify the environment (dev or prod). Required."
    echo "  -h      Display this help message."
    echo
    echo "Commands:"
    echo "  up      Create and start containers (e.g., 'up -d' to detach)."
    echo "          Also starts daemon if DAEMON_ENABLED=true in .env file."
    echo "  down    Stop and remove containers, networks, and volumes."
    echo "          Also stops daemon if running."
    echo "  logs    Follow log output (e.g., 'logs -f backend')."
    echo "  build   Build or rebuild services."
    echo "  daemon  Manage screen daemon: start, stop, status, logs."
    echo "  proxy   Manage stdio-to-HTTP MCP proxy: start, stop, status, logs."
    echo
    echo "Examples:"
    echo "  $PROG_NAME -e dev up -d"
    echo "  $PROG_NAME -e prod down"
    echo "  $PROG_NAME -e dev logs -f backend"
    echo "  $PROG_NAME -e dev daemon start"
    echo "  $PROG_NAME -e dev daemon status"
    echo "  $PROG_NAME -e dev daemon logs"
    echo "  $PROG_NAME -e dev proxy start"
    echo "  $PROG_NAME -e dev proxy status"
    echo "  $PROG_NAME -e dev proxy logs"
}

function error_exit() {
    echo "Error: $1" >&2
    echo "See '$PROG_NAME -h' for usage."
    exit 1
}

# --- Daemon Functions ---
function daemon_is_running() {
    if [ -f "$DAEMON_PID_FILE" ]; then
        local pid
        pid=$(cat "$DAEMON_PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        # Stale PID file
        rm -f "$DAEMON_PID_FILE"
    fi
    return 1
}

function start_daemon() {
    if daemon_is_running; then
        local pid
        pid=$(cat "$DAEMON_PID_FILE")
        echo "Daemon already running (PID $pid)"
        return 0
    fi

    local daemon_args="${DAEMON_ARGS:-}"
    echo "Starting screen daemon... (args: ${daemon_args:-none})"

    # shellcheck disable=SC2086
    nohup "$DAEMON_DIR/run.sh" $daemon_args >> "$DAEMON_LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$DAEMON_PID_FILE"
    echo "Daemon started (PID $pid), logging to $DAEMON_LOG_FILE"
}

function stop_daemon() {
    if ! daemon_is_running; then
        echo "Daemon is not running"
        rm -f "$DAEMON_PID_FILE"
        return 0
    fi

    local pid
    pid=$(cat "$DAEMON_PID_FILE")
    echo "Stopping daemon (PID $pid)..."
    kill "$pid" 2>/dev/null

    # Wait up to 5 seconds for graceful shutdown
    local waited=0
    while kill -0 "$pid" 2>/dev/null && [ $waited -lt 5 ]; do
        sleep 1
        waited=$((waited + 1))
    done

    if kill -0 "$pid" 2>/dev/null; then
        echo "Daemon didn't stop gracefully, sending SIGKILL..."
        kill -9 "$pid" 2>/dev/null
    fi

    rm -f "$DAEMON_PID_FILE"
    echo "Daemon stopped"
}

function daemon_status() {
    if daemon_is_running; then
        local pid
        pid=$(cat "$DAEMON_PID_FILE")
        echo "Daemon is running (PID $pid)"
    else
        echo "Daemon is not running"
    fi
}

function daemon_logs() {
    if [ ! -f "$DAEMON_LOG_FILE" ]; then
        echo "No daemon log file found at $DAEMON_LOG_FILE"
        return 1
    fi
    tail -f "$DAEMON_LOG_FILE"
}

# --- Proxy Functions ---
function proxy_is_running() {
    if [ -f "$PROXY_PID_FILE" ]; then
        local pid
        pid=$(cat "$PROXY_PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        # Stale PID file
        rm -f "$PROXY_PID_FILE"
    fi
    return 1
}

function start_proxy() {
    if proxy_is_running; then
        local pid
        pid=$(cat "$PROXY_PID_FILE")
        echo "Stdio proxy already running (PID $pid)"
        return 0
    fi

    local proxy_args="${PROXY_ARGS:-}"
    echo "Starting stdio-to-HTTP MCP proxy... (args: ${proxy_args:-none})"

    # shellcheck disable=SC2086
    nohup "$PROXY_DIR/run.sh" $proxy_args >> "$PROXY_LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PROXY_PID_FILE"
    echo "Proxy started (PID $pid), logging to $PROXY_LOG_FILE"
}

function stop_proxy() {
    if ! proxy_is_running; then
        echo "Proxy is not running"
        rm -f "$PROXY_PID_FILE"
        return 0
    fi

    local pid
    pid=$(cat "$PROXY_PID_FILE")
    echo "Stopping proxy (PID $pid)..."
    kill "$pid" 2>/dev/null

    # Wait up to 5 seconds for graceful shutdown
    local waited=0
    while kill -0 "$pid" 2>/dev/null && [ $waited -lt 5 ]; do
        sleep 1
        waited=$((waited + 1))
    done

    if kill -0 "$pid" 2>/dev/null; then
        echo "Proxy didn't stop gracefully, sending SIGKILL..."
        kill -9 "$pid" 2>/dev/null
    fi

    rm -f "$PROXY_PID_FILE"
    echo "Proxy stopped"
}

function proxy_status() {
    if proxy_is_running; then
        local pid
        pid=$(cat "$PROXY_PID_FILE")
        echo "Proxy is running (PID $pid)"
    else
        echo "Proxy is not running"
    fi
}

function proxy_logs() {
    if [ ! -f "$PROXY_LOG_FILE" ]; then
        echo "No proxy log file found at $PROXY_LOG_FILE"
        return 1
    fi
    tail -f "$PROXY_LOG_FILE"
}

# --- Main Script Logic ---

# Argument parsing
ENV=""
while getopts ":e:h" opt; do
  case ${opt} in
    e)
      ENV=$OPTARG
      ;;
    h)
      display_help
      exit 0
      ;;
    \?)
      error_exit "Invalid option: -$OPTARG"
      ;;
    :)
      error_exit "Option -$OPTARG requires an argument."
      ;;
  esac
done
shift "$((OPTIND-1))"

COMMAND=$1
if [[ -z "$COMMAND" ]]; then
    error_exit "No command specified."
fi
shift

# --- Environment Setup ---
if [ -z "$ENV" ]; then
    error_exit "No environment specified. You must use '-e dev' or '-e prod'."
fi

if [ "$ENV" != "dev" ] && [ "$ENV" != "prod" ]; then
    error_exit "Invalid environment '$ENV'. Please use 'dev' or 'prod'."
fi

ENV_FILE=".env.$ENV"
COMPOSE_FILES=(
    -f docker-compose.$ENV.yaml
)

if [ ! -f "$ENV_FILE" ]; then
    error_exit "$ENV_FILE not found. Please create it by copying from $ENV_FILE.example and filling in the values."
fi

# Source env file for daemon config
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

# --- Execution ---

# Handle proxy subcommand
if [ "$COMMAND" = "proxy" ]; then
    PROXY_SUB="${1:-}"
    case "$PROXY_SUB" in
        start)
            start_proxy
            ;;
        stop)
            stop_proxy
            ;;
        status)
            proxy_status
            ;;
        logs)
            proxy_logs
            ;;
        *)
            error_exit "Unknown proxy subcommand '$PROXY_SUB'. Use: start, stop, status, logs"
            ;;
    esac
    exit 0
fi

# Handle daemon subcommand
if [ "$COMMAND" = "daemon" ]; then
    DAEMON_SUB="${1:-}"
    case "$DAEMON_SUB" in
        start)
            start_daemon
            ;;
        stop)
            stop_daemon
            ;;
        status)
            daemon_status
            ;;
        logs)
            daemon_logs
            ;;
        *)
            error_exit "Unknown daemon subcommand '$DAEMON_SUB'. Use: start, stop, status, logs"
            ;;
    esac
    exit 0
fi

echo "=========================================================="
echo "          Running command '$COMMAND' in '$ENV' environment"
echo "=========================================================="
echo "Using env file: $ENV_FILE"
echo "Using compose files: ${COMPOSE_FILES[*]}"
echo "----------------------------------------------------------"

# Execute docker compose
docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" "$COMMAND" "$@"

# After docker compose up: optionally start daemon and proxy
if [ "$COMMAND" = "up" ]; then
    if [ "${DAEMON_ENABLED:-false}" = "true" ]; then
        echo "----------------------------------------------------------"
        start_daemon
    else
        echo "----------------------------------------------------------"
        echo "Screen daemon disabled (set DAEMON_ENABLED=true in $ENV_FILE to enable)"
    fi
    if [ "${PROXY_ENABLED:-false}" = "true" ]; then
        echo "----------------------------------------------------------"
        start_proxy
    else
        echo "----------------------------------------------------------"
        echo "Stdio proxy disabled (set PROXY_ENABLED=true in $ENV_FILE to enable)"
    fi
fi

# After docker compose down: stop daemon and proxy if running
if [ "$COMMAND" = "down" ]; then
    if daemon_is_running; then
        echo "----------------------------------------------------------"
        stop_daemon
    fi
    if proxy_is_running; then
        echo "----------------------------------------------------------"
        stop_proxy
    fi
fi
