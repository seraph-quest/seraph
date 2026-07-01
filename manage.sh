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
#   ./manage.sh -e [dev|prod] local up|down|status|logs|run - Manage the direct local frontend/backend stack.
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
    echo "  local   Manage the direct local frontend/backend stack: up, down, status, logs, run."
    echo "  daemon  Manage screen daemon: start, stop, status, logs."
    echo "  proxy   Manage stdio-to-HTTP MCP proxy: start, stop, status, logs."
    echo
    echo "Examples:"
    echo "  $PROG_NAME -e dev up -d"
    echo "  $PROG_NAME -e prod down"
    echo "  $PROG_NAME -e dev logs -f backend"
    echo "  $PROG_NAME -e dev local up"
    echo "  $PROG_NAME -e dev local run"
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

function process_command() {
    local pid="$1"
    ps -p "$pid" -o command= 2>/dev/null || true
}

function process_command_with_env() {
    local pid="$1"
    ps eww -p "$pid" -o command= 2>/dev/null || true
}

function process_cwd() {
    local pid="$1"
    lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1
}

function command_matches_marker() {
    local pid="$1"
    local marker="$2"
    if echo "$marker" | grep -F "cwd:" >/dev/null 2>&1; then
        local expected_cwd="${marker#cwd:}"
        [ "$(process_cwd "$pid")" = "$expected_cwd" ]
        return $?
    fi
    local command
    command=$(process_command "$pid")
    [ -n "$marker" ] && echo "$command" | grep -F "$marker" >/dev/null 2>&1
}

function pid_is_running() {
    local pid_file="$1"
    local marker="${2:-}"
    if [ -f "$pid_file" ]; then
        local pid
        pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            if [ -z "$marker" ] || command_matches_marker "$pid" "$marker"; then
                return 0
            fi
            echo "Ignoring stale PID file $pid_file: PID $pid is not $marker"
            rm -f "$pid_file"
            return 1
        fi
        rm -f "$pid_file"
    fi
    return 1
}

function collect_process_tree() {
    local root_pid="$1"
    local child
    echo "$root_pid"
    for child in $(pgrep -P "$root_pid" 2>/dev/null || true); do
        collect_process_tree "$child"
    done
}

function any_pid_running() {
    local pid
    for pid in "$@"; do
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    done
    return 1
}

function kill_process_tree() {
    local root_pid="$1"
    local label="$2"
    local signal="${3:-TERM}"
    local pids
    pids=$(collect_process_tree "$root_pid" | awk '!seen[$0]++')
    if [ -z "$pids" ]; then
        return 0
    fi

    local pid
    # Children first, then the recorded parent. This catches uvicorn --reload and Vite workers
    # before they can become orphaned listeners.
    for pid in $(echo "$pids" | awk '{ values[NR] = $0 } END { for (idx = NR; idx >= 1; idx--) print values[idx] }'); do
        if kill -0 "$pid" 2>/dev/null; then
            kill "-$signal" "$pid" 2>/dev/null || true
        fi
    done

    if [ "$signal" = "KILL" ]; then
        echo "$label process tree force-killed: $(echo "$pids" | tr '\n' ' ')"
    fi
}

function stop_pid() {
    local pid_file="$1"
    local label="$2"
    local marker="${3:-}"
    if ! pid_is_running "$pid_file" "$marker"; then
        echo "$label is not running"
        rm -f "$pid_file"
        return 0
    fi

    local pid
    pid=$(cat "$pid_file")
    echo "Stopping $label (PID $pid)..."
    local captured_pids
    captured_pids=$(collect_process_tree "$pid" | awk '!seen[$0]++')
    kill_process_tree "$pid" "$label" TERM

    local waited=0
    while any_pid_running $captured_pids && [ $waited -lt 10 ]; do
        sleep 1
        waited=$((waited + 1))
    done

    if any_pid_running $captured_pids; then
        echo "$label did not stop gracefully, sending SIGKILL..."
        local child_pid
        for child_pid in $captured_pids; do
            if kill -0 "$child_pid" 2>/dev/null; then
                kill -KILL "$child_pid" 2>/dev/null || true
            fi
        done
        sleep 1
        if any_pid_running $captured_pids; then
            echo "$label may still have live process(es): $(echo "$captured_pids" | tr '\n' ' ')" >&2
            return 1
        fi
    fi

    rm -f "$pid_file"
    echo "$label stopped"
}

function pids_listening_on_port() {
    local port="$1"
    lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | awk '!seen[$0]++'
}

function seraph_local_port_owner() {
    local pid="$1"
    local service="$2"
    process_command_with_env "$pid" | grep -F "SERAPH_LOCAL_SERVICE=$service" >/dev/null 2>&1
}

function pid_is_descendant_of() {
    local needle="$1"
    local root="$2"
    collect_process_tree "$root" | awk -v needle="$needle" '$0 == needle { found = 1 } END { exit found ? 0 : 1 }'
}

function stop_port_listeners() {
    local port="$1"
    local label="$2"
    local service="$3"
    local pids
    pids=$(pids_listening_on_port "$port")
    if [ -z "$pids" ]; then
        return 0
    fi

    local owned_pids=""
    local pid
    for pid in $pids; do
        if seraph_local_port_owner "$pid" "$service"; then
            owned_pids="$owned_pids $pid"
        fi
    done

    if [ -z "$owned_pids" ]; then
        echo "$label port $port is still in use by a non-Seraph process; leaving it untouched." >&2
        lsof -nP -iTCP:"$port" -sTCP:LISTEN >&2 || true
        return 1
    fi

    echo "Stopping stale $label listener(s) on port $port:$owned_pids"
    for pid in $owned_pids; do
        kill_process_tree "$pid" "$label listener" TERM
    done

    local waited=0
    while [ -n "$(pids_listening_on_port "$port")" ] && [ $waited -lt 10 ]; do
        sleep 1
        waited=$((waited + 1))
    done

    pids=$(pids_listening_on_port "$port")
    if [ -n "$pids" ]; then
        echo "Stale $label listener(s) did not stop gracefully, sending SIGKILL..."
        for pid in $pids; do
            if seraph_local_port_owner "$pid" "$service"; then
                kill_process_tree "$pid" "$label listener" KILL
            fi
        done
    fi

    if [ -n "$(pids_listening_on_port "$port")" ]; then
        echo "$label port $port is still in use after cleanup." >&2
        lsof -nP -iTCP:"$port" -sTCP:LISTEN >&2 || true
        return 1
    fi
}

function require_free_port() {
    local port="$1"
    local label="$2"
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        error_exit "$label port $port is already in use"
    fi
}

function wait_for_pid_and_port() {
    local pid_file="$1"
    local port="$2"
    local label="$3"
    local log_file="$4"
    local marker="$5"
    local service="$6"
    local timeout="${7:-20}"
    local waited=0

    while [ $waited -lt "$timeout" ]; do
        if ! pid_is_running "$pid_file" "$marker"; then
            echo "$label exited during startup. Last log lines:" >&2
            tail -n 60 "$log_file" >&2 2>/dev/null || true
            return 1
        fi
        local supervisor_pid listener_pid listener_pids owned_listener_found
        supervisor_pid=$(cat "$pid_file")
        listener_pids=$(pids_listening_on_port "$port")
        owned_listener_found=false
        for listener_pid in $listener_pids; do
            if pid_is_descendant_of "$listener_pid" "$supervisor_pid" || seraph_local_port_owner "$listener_pid" "$service"; then
                owned_listener_found=true
            else
                echo "$label port $port was taken by an unrelated process during startup:" >&2
                lsof -nP -iTCP:"$port" -sTCP:LISTEN >&2 || true
                return 1
            fi
        done
        if [ "$owned_listener_found" = true ]; then
            echo "$label is listening on http://127.0.0.1:$port"
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done

    echo "$label did not begin listening on port $port within ${waited}s. Last log lines:" >&2
    tail -n 60 "$log_file" >&2 2>/dev/null || true
    return 1
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
    sleep 1
    if ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$DAEMON_PID_FILE"
        echo "Daemon failed to stay running; recent log output:"
        tail -n 40 "$DAEMON_LOG_FILE" 2>/dev/null || true
        return 1
    fi
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
    nohup /bin/bash -c '
        cd "$1" || exit 1
        export WORKSPACE_DIR="$2" LLM_LOG_DIR="$3" UV_CACHE_DIR="$4" DEFAULT_MODEL="$5" SERAPH_LOCAL_SERVICE=backend
        exec uv run uvicorn src.app:create_app --factory --host 0.0.0.0 --port "$6"
    ' seraph-local-backend "$SCRIPT_DIR/backend" "$LOCAL_WORKSPACE_DIR" "$LOCAL_LLM_LOG_DIR" "$LOCAL_UV_CACHE_DIR" "$LOCAL_DEFAULT_MODEL" "$LOCAL_BACKEND_PORT" </dev/null >> "$LOCAL_BACKEND_LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$LOCAL_BACKEND_PID_FILE"
    disown "$pid" 2>/dev/null || true
    if wait_for_pid_and_port "$LOCAL_BACKEND_PID_FILE" "$LOCAL_BACKEND_PORT" "Local backend" "$LOCAL_BACKEND_LOG_FILE" "" backend 75; then
        echo "Local backend started (PID $pid), logging to $LOCAL_BACKEND_LOG_FILE"
        return 0
    fi
    stop_pid "$LOCAL_BACKEND_PID_FILE" "Local backend"
    return 1
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
    nohup /bin/bash -c '
        cd "$1" || exit 1
        export VITE_API_URL="$2" VITE_WS_URL="$3" SERAPH_LOCAL_SERVICE=frontend
        if [ -x ./node_modules/.bin/vite ]; then
            exec ./node_modules/.bin/vite --host 0.0.0.0 --port "$4"
        fi
        exec npm run dev -- --host 0.0.0.0 --port "$4"
    ' seraph-local-frontend "$SCRIPT_DIR/frontend" "/api" "ws://127.0.0.1:$LOCAL_BACKEND_PORT/ws/chat" "$LOCAL_FRONTEND_PORT" </dev/null >> "$LOCAL_FRONTEND_LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$LOCAL_FRONTEND_PID_FILE"
    disown "$pid" 2>/dev/null || true
    if wait_for_pid_and_port "$LOCAL_FRONTEND_PID_FILE" "$LOCAL_FRONTEND_PORT" "Local frontend" "$LOCAL_FRONTEND_LOG_FILE" "" frontend; then
        echo "Local frontend started (PID $pid), logging to $LOCAL_FRONTEND_LOG_FILE"
        return 0
    fi
    stop_pid "$LOCAL_FRONTEND_PID_FILE" "Local frontend"
    return 1
}

function local_up() {
    ensure_runtime_dirs
    if ! start_local_backend; then
        echo "Local stack failed: backend did not start cleanly." >&2
        return 1
    fi
    if ! start_local_frontend; then
        echo "Local stack failed: frontend did not start cleanly; stopping backend." >&2
        stop_pid "$LOCAL_BACKEND_PID_FILE" "Local backend"
        stop_port_listeners "$LOCAL_BACKEND_PORT" "Local backend" backend || true
        return 1
    fi
    echo "Local stack is running: frontend http://127.0.0.1:$LOCAL_FRONTEND_PORT, backend http://127.0.0.1:$LOCAL_BACKEND_PORT"
    if [ "${DAEMON_ENABLED:-false}" = "true" ]; then
        if ! start_daemon; then
            echo "Local stack warning: screen daemon did not start; Settings will show daemon offline." >&2
        fi
    else
        echo "Screen daemon disabled (set DAEMON_ENABLED=true in $ENV_FILE to enable)"
    fi
}

function local_down() {
    stop_daemon
    stop_pid "$LOCAL_FRONTEND_PID_FILE" "Local frontend"
    stop_pid "$LOCAL_BACKEND_PID_FILE" "Local backend"
    stop_port_listeners "$LOCAL_FRONTEND_PORT" "Local frontend" frontend
    stop_port_listeners "$LOCAL_BACKEND_PORT" "Local backend" backend
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
    if daemon_is_running; then
        echo "Screen daemon: running (PID $(cat "$DAEMON_PID_FILE"))"
    else
        echo "Screen daemon: stopped"
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
        daemon)
            touch "$DAEMON_LOG_FILE"
            tail -f "$DAEMON_LOG_FILE"
            ;;
        all)
            touch "$LOCAL_BACKEND_LOG_FILE" "$LOCAL_FRONTEND_LOG_FILE" "$DAEMON_LOG_FILE"
            tail -f "$LOCAL_BACKEND_LOG_FILE" "$LOCAL_FRONTEND_LOG_FILE" "$DAEMON_LOG_FILE"
            ;;
        *)
            error_exit "Unknown local logs target '$target'. Use: backend, frontend, daemon, all"
            ;;
    esac
}

function local_run() {
    if ! local_up; then
        return 1
    fi
    local_logs all
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

# Source env file for daemon config without leaking values when bash xtrace is enabled.
TRACE_WAS_ENABLED=false
case "$-" in
    *x*)
        TRACE_WAS_ENABLED=true
        set +x
        ;;
esac
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
if [ "$TRACE_WAS_ENABLED" = true ]; then
    set -x
fi

LOCAL_BACKEND_PORT="${LOCAL_BACKEND_PORT:-8004}"
LOCAL_FRONTEND_PORT="${LOCAL_FRONTEND_PORT:-3001}"
if [ "$COMMAND" = "local" ]; then
    LOCAL_WORKSPACE_DIR="${LOCAL_WORKSPACE_DIR:-/tmp/seraph-dev-data}"
else
    LOCAL_WORKSPACE_DIR="${LOCAL_WORKSPACE_DIR:-${BACKEND_DATA_PATH_DEV:-/tmp/seraph-dev-data}}"
fi
if [[ "$LOCAL_WORKSPACE_DIR" != /* ]]; then
    LOCAL_WORKSPACE_DIR="$SCRIPT_DIR/$LOCAL_WORKSPACE_DIR"
fi
LOCAL_LLM_LOG_DIR="${LOCAL_LLM_LOG_DIR:-/tmp/seraph-dev-logs}"
LOCAL_UV_CACHE_DIR="${LOCAL_UV_CACHE_DIR:-/tmp/uv-cache}"
LOCAL_DEFAULT_MODEL="${LOCAL_DEFAULT_MODEL:-codex-local}"
SCREEN_CAPTURE_ARCHIVE_DIR="${SCREEN_CAPTURE_ARCHIVE_DIR:-$LOCAL_WORKSPACE_DIR/artifacts/screen-captures}"
SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR="${SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR:-$SCREEN_CAPTURE_ARCHIVE_DIR}"
SERAPH_DAEMON_STATUS_FILE="${SERAPH_DAEMON_STATUS_FILE:-$LOCAL_WORKSPACE_DIR/daemon-status.json}"
REPORT_ARCHIVE_DIR="${REPORT_ARCHIVE_DIR:-$LOCAL_WORKSPACE_DIR/artifacts/reports}"
export SCREEN_CAPTURE_ARCHIVE_DIR SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR SERAPH_DAEMON_STATUS_FILE REPORT_ARCHIVE_DIR
if [ "$COMMAND" = "local" ]; then
    DEFAULT_MODEL="$LOCAL_DEFAULT_MODEL"
fi

# --- Execution ---

if [ "$COMMAND" = "local" ]; then
    LOCAL_SUB="${1:-}"
    case "$LOCAL_SUB" in
        up)
            local_up
            ;;
        run)
            local_run
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
            error_exit "Unknown local subcommand '$LOCAL_SUB'. Use: up, down, status, logs, run"
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
