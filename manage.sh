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
#   ./manage.sh -e [dev|prod] local up|down|status|logs     - Manage the direct local frontend/backend stack.
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
PID_DIR="$SCRIPT_DIR/pids"
LOG_DIR="$SCRIPT_DIR/logs"
LOCAL_BACKEND_PID_FILE="$PID_DIR/seraph-local-backend.pid"
LOCAL_FRONTEND_PID_FILE="$PID_DIR/seraph-local-frontend.pid"
LOCAL_BACKEND_LOG_FILE="$LOG_DIR/seraph-local-backend.log"
LOCAL_FRONTEND_LOG_FILE="$LOG_DIR/seraph-local-frontend.log"

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
    echo "  local   Manage the direct local frontend/backend stack: up, down, status, logs."
    echo "  daemon  Manage screen daemon: start, stop, status, logs."
    echo "  proxy   Manage stdio-to-HTTP MCP proxy: start, stop, status, logs."
    echo
    echo "Examples:"
    echo "  $PROG_NAME -e dev up -d"
    echo "  $PROG_NAME -e prod down"
    echo "  $PROG_NAME -e dev logs -f backend"
    echo "  $PROG_NAME -e dev local up"
    echo "  $PROG_NAME -e dev local status"
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

function ensure_runtime_dirs() {
    mkdir -p "$PID_DIR" "$LOG_DIR"
}

function pid_is_running() {
    local pid_file="$1"
    if [ -f "$pid_file" ]; then
        local pid
        pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        rm -f "$pid_file"
    fi
    return 1
}

function stop_pid() {
    local pid_file="$1"
    local label="$2"
    if ! pid_is_running "$pid_file"; then
        echo "$label is not running"
        rm -f "$pid_file"
        return 0
    fi

    local pid
    pid=$(cat "$pid_file")
    echo "Stopping $label (PID $pid)..."
    kill "$pid" 2>/dev/null

    local waited=0
    while kill -0 "$pid" 2>/dev/null && [ $waited -lt 10 ]; do
        sleep 1
        waited=$((waited + 1))
    done

    if kill -0 "$pid" 2>/dev/null; then
        echo "$label did not stop gracefully, sending SIGKILL..."
        kill -9 "$pid" 2>/dev/null
    fi

    rm -f "$pid_file"
    echo "$label stopped"
}

function require_free_port() {
    local port="$1"
    local label="$2"
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        error_exit "$label port $port is already in use"
    fi
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

# --- Local Stack Functions ---
function local_backend_is_running() {
    pid_is_running "$LOCAL_BACKEND_PID_FILE"
}

function local_frontend_is_running() {
    pid_is_running "$LOCAL_FRONTEND_PID_FILE"
}

function start_local_backend() {
    if local_backend_is_running; then
        local pid
        pid=$(cat "$LOCAL_BACKEND_PID_FILE")
        echo "Local backend already running (PID $pid)"
        return 0
    fi

    require_free_port "$LOCAL_BACKEND_PORT" "Local backend"
    mkdir -p "$LOCAL_WORKSPACE_DIR" "$LOCAL_LLM_LOG_DIR"
    echo "Starting local backend on http://127.0.0.1:$LOCAL_BACKEND_PORT ..."
    nohup /bin/bash -c "cd \"$SCRIPT_DIR/backend\" && export WORKSPACE_DIR=\"$LOCAL_WORKSPACE_DIR\" LLM_LOG_DIR=\"$LOCAL_LLM_LOG_DIR\" UV_CACHE_DIR=\"$LOCAL_UV_CACHE_DIR\" && exec uv run uvicorn src.app:create_app --factory --host 0.0.0.0 --port \"$LOCAL_BACKEND_PORT\" --reload" >> "$LOCAL_BACKEND_LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$LOCAL_BACKEND_PID_FILE"
    echo "Local backend started (PID $pid), logging to $LOCAL_BACKEND_LOG_FILE"
}

function start_local_frontend() {
    if local_frontend_is_running; then
        local pid
        pid=$(cat "$LOCAL_FRONTEND_PID_FILE")
        echo "Local frontend already running (PID $pid)"
        return 0
    fi

    require_free_port "$LOCAL_FRONTEND_PORT" "Local frontend"
    echo "Starting local frontend on http://127.0.0.1:$LOCAL_FRONTEND_PORT ..."
    nohup /bin/bash -c "cd \"$SCRIPT_DIR/frontend\" && export VITE_API_URL=\"http://127.0.0.1:$LOCAL_BACKEND_PORT\" VITE_WS_URL=\"ws://127.0.0.1:$LOCAL_BACKEND_PORT/ws/chat\" && exec npm run dev -- --host 0.0.0.0 --port \"$LOCAL_FRONTEND_PORT\"" >> "$LOCAL_FRONTEND_LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$LOCAL_FRONTEND_PID_FILE"
    echo "Local frontend started (PID $pid), logging to $LOCAL_FRONTEND_LOG_FILE"
}

function local_up() {
    ensure_runtime_dirs
    start_local_backend
    start_local_frontend
}

function local_down() {
    stop_pid "$LOCAL_FRONTEND_PID_FILE" "Local frontend"
    stop_pid "$LOCAL_BACKEND_PID_FILE" "Local backend"
}

function local_status() {
    echo "Environment: $ENV"
    echo "Env file: $ENV_FILE"
    echo "Default model: ${DEFAULT_MODEL:-openrouter/anthropic/claude-sonnet-4}"
    echo "Workspace dir: $LOCAL_WORKSPACE_DIR"
    echo "LLM log dir: $LOCAL_LLM_LOG_DIR"
    if local_backend_is_running; then
        echo "Local backend: running (PID $(cat "$LOCAL_BACKEND_PID_FILE")) -> http://127.0.0.1:$LOCAL_BACKEND_PORT"
    else
        echo "Local backend: stopped"
    fi
    if local_frontend_is_running; then
        echo "Local frontend: running (PID $(cat "$LOCAL_FRONTEND_PID_FILE")) -> http://127.0.0.1:$LOCAL_FRONTEND_PORT"
    else
        echo "Local frontend: stopped"
    fi
}

function local_logs() {
    local target="${1:-all}"
    ensure_runtime_dirs
    case "$target" in
        backend)
            touch "$LOCAL_BACKEND_LOG_FILE"
            tail -f "$LOCAL_BACKEND_LOG_FILE"
            ;;
        frontend)
            touch "$LOCAL_FRONTEND_LOG_FILE"
            tail -f "$LOCAL_FRONTEND_LOG_FILE"
            ;;
        all)
            touch "$LOCAL_BACKEND_LOG_FILE" "$LOCAL_FRONTEND_LOG_FILE"
            tail -f "$LOCAL_BACKEND_LOG_FILE" "$LOCAL_FRONTEND_LOG_FILE"
            ;;
        *)
            error_exit "Unknown local logs target '$target'. Use: backend, frontend, all"
            ;;
    esac
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

ENV_FILE="$SCRIPT_DIR/.env.$ENV"
COMPOSE_FILES=(
    -f "$SCRIPT_DIR/docker-compose.$ENV.yaml"
)

if [ ! -f "$ENV_FILE" ]; then
    error_exit "$ENV_FILE not found. Please create it by copying from $SCRIPT_DIR/env.$ENV.example and filling in the values."
fi

# Source env file for daemon config
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

LOCAL_BACKEND_PORT="${LOCAL_BACKEND_PORT:-8004}"
LOCAL_FRONTEND_PORT="${LOCAL_FRONTEND_PORT:-3001}"
LOCAL_WORKSPACE_DIR="${LOCAL_WORKSPACE_DIR:-/tmp/seraph-dev-data}"
LOCAL_LLM_LOG_DIR="${LOCAL_LLM_LOG_DIR:-/tmp/seraph-dev-logs}"
LOCAL_UV_CACHE_DIR="${LOCAL_UV_CACHE_DIR:-/tmp/uv-cache}"

# --- Execution ---

if [ "$COMMAND" = "local" ]; then
    LOCAL_SUB="${1:-}"
    case "$LOCAL_SUB" in
        up)
            local_up
            ;;
        down)
            local_down
            ;;
        status)
            local_status
            ;;
        logs)
            shift || true
            local_logs "${1:-all}"
            ;;
        *)
            error_exit "Unknown local subcommand '$LOCAL_SUB'. Use: up, down, status, logs"
            ;;
    esac
    exit 0
fi

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
