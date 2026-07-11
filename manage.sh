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
    echo "  production  GPU/LAN lifecycle: start/accept, rollback/accept-rollback, restart/accept-restart, accept-restore, status, logs, stop."
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

function service_readiness() {
    local pid_file="$1"
    local port="$2"
    local service="$3"
    local marker="${4:-}"

    local pid_ready=false
    local port_ready=false
    local supervisor_pid=""
    if pid_is_running "$pid_file" "$marker"; then
        pid_ready=true
        supervisor_pid=$(cat "$pid_file")
    fi

    local listener_pid listener_pids
    listener_pids=$(pids_listening_on_port "$port")
    for listener_pid in $listener_pids; do
        if { [ -n "$supervisor_pid" ] && pid_is_descendant_of "$listener_pid" "$supervisor_pid"; } || seraph_local_port_owner "$listener_pid" "$service"; then
            port_ready=true
            break
        fi
    done

    if [ "$pid_ready" = true ] && [ "$port_ready" = true ]; then
        echo "ready"
    elif [ "$pid_ready" = true ]; then
        echo "pid_only"
    elif [ "$port_ready" = true ]; then
        echo "listener_only"
    else
        echo "stopped"
    fi
}

function print_local_service_status() {
    local label="$1"
    local pid_file="$2"
    local port="$3"
    local service="$4"
    local readiness
    readiness=$(service_readiness "$pid_file" "$port" "$service")

    case "$readiness" in
        ready)
            echo "$label: running (PID $(cat "$pid_file")) -> http://127.0.0.1:$port"
            ;;
        pid_only)
            echo "$label: degraded (PID $(cat "$pid_file"), port $port not listening)"
            ;;
        listener_only)
            echo "$label: degraded (port $port has Seraph listener, PID file missing/stale)"
            ;;
        *)
            echo "$label: stopped"
            ;;
    esac
}

function verify_local_stack_ready() {
    local backend_ready frontend_ready
    backend_ready=$(service_readiness "$LOCAL_BACKEND_PID_FILE" "$LOCAL_BACKEND_PORT" backend)
    frontend_ready=$(service_readiness "$LOCAL_FRONTEND_PID_FILE" "$LOCAL_FRONTEND_PORT" frontend)

    if [ "$backend_ready" = ready ] && [ "$frontend_ready" = ready ]; then
        return 0
    fi

    echo "Local stack readiness check failed after startup:" >&2
    echo "  backend=$backend_ready (pid_file=$LOCAL_BACKEND_PID_FILE, port=$LOCAL_BACKEND_PORT)" >&2
    echo "  frontend=$frontend_ready (pid_file=$LOCAL_FRONTEND_PID_FILE, port=$LOCAL_FRONTEND_PORT)" >&2
    echo "Use './manage.sh -e $ENV local run' for managed-shell live observation." >&2
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

    if [ "${DAEMON_ENABLED:-false}" != "true" ]; then
        echo "Daemon configured off: set DAEMON_ENABLED=true in $ENV_FILE to enable native desktop presence."
        return 0
    fi

    if [ -f "$SERAPH_DAEMON_STATUS_FILE" ]; then
        local status_summary
        status_summary=$(grep -E '"(state|last_error_kind|last_error|updated_at)"' "$SERAPH_DAEMON_STATUS_FILE" | sed 's/^[[:space:]]*//' || true)
        if [ -n "$status_summary" ]; then
            echo "Status file: $SERAPH_DAEMON_STATUS_FILE"
            echo "$status_summary"
        fi
        if grep -Eiq '(-1743|-10827|not authori[sz]ed to send apple events|system events got an error)' "$SERAPH_DAEMON_STATUS_FILE"; then
            echo "Recovery: grant Automation permission for the terminal running Seraph to control System Events in System Settings > Privacy & Security > Automation, then restart with ./manage.sh -e $ENV daemon start."
        fi
    elif [ -f "$DAEMON_LOG_FILE" ] && grep -Eiq '(-1743|-10827|not authori[sz]ed to send apple events|system events got an error)' "$DAEMON_LOG_FILE"; then
        echo "Recovery: daemon logs show macOS Automation permission denial for System Events."
        echo "Grant Automation permission for the terminal running Seraph, then restart with ./manage.sh -e $ENV daemon start."
    else
        echo "Status file: $SERAPH_DAEMON_STATUS_FILE (not found yet)"
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
    if ! verify_local_stack_ready; then
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
    print_local_service_status "Local backend" "$LOCAL_BACKEND_PID_FILE" "$LOCAL_BACKEND_PORT" backend
    print_local_service_status "Local frontend" "$LOCAL_FRONTEND_PID_FILE" "$LOCAL_FRONTEND_PORT" frontend
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

# --- Production GPU/LAN Stack Functions ---
function production_require_file() {
    local variable="$1"
    local value="${!variable:-}"
    [ -n "$value" ] || error_exit "$variable is required for production"
    [ -r "$value" ] || error_exit "$variable does not name a readable file: $value"
}

function production_config_validate() {
    [ "$ENV" = "prod" ] || error_exit "production lifecycle requires -e prod"
    production_require_file SERAPH_TLS_CERT_FILE
    production_require_file SERAPH_TLS_KEY_FILE
    production_require_file SERAPH_MAC_PROBE_KEY_FILE
    [ -s "$SERAPH_MAC_PROBE_KEY_FILE" ] || error_exit "SERAPH_MAC_PROBE_KEY_FILE must be nonempty"
    [ -n "${SERAPH_LAN_IP:-}" ] || error_exit "SERAPH_LAN_IP is required"
    [ -s "${SERAPH_VLM_API_KEY_FILE:-/dev/null}" ] || error_exit "SERAPH_VLM_API_KEY_FILE must be nonempty"
    [ -n "${SERAPH_EXPECTED_HTTPS_ORIGIN:-}" ] || error_exit "SERAPH_EXPECTED_HTTPS_ORIGIN is required"
    [ -n "${SERAPH_MAC_PROBE_CLIENT_ID:-}" ] || error_exit "SERAPH_MAC_PROBE_CLIENT_ID is required"
    local credential_count=0
    if [ -n "${OPERATOR_AUTH_SECRET_FILE:-}" ] && [ -s "$OPERATOR_AUTH_SECRET_FILE" ]; then credential_count=$((credential_count + 1)); fi
    if [ -n "${OPERATOR_AUTH_SECRET_HASH_FILE:-}" ] && [ -s "$OPERATOR_AUTH_SECRET_HASH_FILE" ]; then credential_count=$((credential_count + 1)); fi
    [ "$credential_count" -eq 1 ] || error_exit "exactly one non-empty operator raw-secret or hash file is required"
    [ "${DEPLOYMENT_ENVIRONMENT:-}" = "production" ] || error_exit "DEPLOYMENT_ENVIRONMENT must be production"
    [ "${OPERATOR_AUTH_COOKIE_SECURE:-}" = "true" ] || error_exit "OPERATOR_AUTH_COOKIE_SECURE must be true"
    [ "${OPERATOR_AUTH_BACKEND_WORKERS:-}" = "1" ] || error_exit "OPERATOR_AUTH_BACKEND_WORKERS must be 1"
    [ "${OPERATOR_AUTH_TRUSTED_PROXY_IPS:-}" = "172.30.0.10" ] || error_exit "trusted proxy must be exactly 172.30.0.10"
    [ -n "${OPERATOR_AUTH_ALLOWED_HOSTS:-}" ] || error_exit "OPERATOR_AUTH_ALLOWED_HOSTS must be exact and non-empty"
    [ -n "${OPERATOR_AUTH_ALLOWED_ORIGINS:-}" ] || error_exit "OPERATOR_AUTH_ALLOWED_ORIGINS must be exact and non-empty"
    case "${OPERATOR_AUTH_ALLOWED_HOSTS},${OPERATOR_AUTH_ALLOWED_ORIGINS}" in
        *'*'*|*example.invalid*) error_exit "replace wildcard/example auth hosts and origins" ;;
    esac
    case "${OPERATOR_AUTH_ALLOWED_ORIGINS}" in https://*) ;; *) error_exit "OPERATOR_AUTH_ALLOWED_ORIGINS must use https" ;; esac
    local current_revision
    current_revision=$(git -C "$SCRIPT_DIR" rev-parse HEAD) || error_exit "cannot determine source revision"
    [[ "${SERAPH_IMAGE_TAG:-}" =~ ^[0-9a-f]{40}$ ]] || error_exit "SERAPH_IMAGE_TAG must be an exact full git SHA"
    if [ "${SERAPH_VALIDATING_ROLLBACK:-false}" != "true" ]; then
        [ "$SERAPH_IMAGE_TAG" = "$current_revision" ] || error_exit "SERAPH_IMAGE_TAG must equal current HEAD $current_revision"
        [ -z "$(git -C "$SCRIPT_DIR" status --porcelain)" ] || error_exit "production builds require a clean working tree"
    fi
    [[ "${SERAPH_VLM_IMAGE:-}" =~ ^([^[:space:]@]+@sha256:[0-9a-fA-F]{64}|sha256:[0-9a-fA-F]{64})$ ]] || error_exit "SERAPH_VLM_IMAGE must use a registry RepoDigest or exact local sha256 image ID"
    [ -n "${SERAPH_VLM_INTERFACE_CONTRACT:-}" ] || error_exit "SERAPH_VLM_INTERFACE_CONTRACT is required"
    [ -n "${SERAPH_GPU_EXPECTED_HOSTNAME:-}" ] || error_exit "SERAPH_GPU_EXPECTED_HOSTNAME is required"
    [[ "${SERAPH_GPU_MACHINE_IDENTITY_SHA256:-}" =~ ^[0-9a-fA-F]{64}$ ]] || error_exit "SERAPH_GPU_MACHINE_IDENTITY_SHA256 must be a SHA-256 digest"
    local config_file
    config_file=$(mktemp /tmp/seraph-prod-compose.XXXXXX.json)
    if ! docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" config --format json >"$config_file"; then
        rm -f "$config_file"
        return 1
    fi
    python3 "$SCRIPT_DIR/scripts/validate_production_topology.py" <"$config_file" || { rm -f "$config_file"; return 1; }
    rm -f "$config_file"
    echo "compose config valid; host inference privacy is not asserted until the separate inventory gate passes"
}

function production_host_inventory_validate() {
    local receipt="${1:-}"
    local vlm_image="${2:-${SERAPH_VLM_IMAGE:-}}"
    [ -r "$receipt" ] || error_exit "host inventory receipt is unreadable: $receipt"
    SERAPH_VLM_IMAGE="$vlm_image" python3 "$SCRIPT_DIR/scripts/validate_gpu_host_inventory.py" <"$receipt"
}

function production_status() {
    production_config_validate
    ensure_runtime_dirs
    if [ -r "$PID_DIR/seraph-prod-restore-release" ]; then
        production_read_validate_accepted_state "$PID_DIR/seraph-prod-restore-release" || return 1
        echo "Release state: restore awaiting LAN acceptance (previous accepted evidence retained, not current)"
    elif [ -r "$PID_DIR/seraph-prod-rollback-release" ]; then
        production_read_validate_accepted_state "$PID_DIR/seraph-prod-rollback-release" || return 1
        echo "Release state: rollback awaiting LAN acceptance (previous accepted tuple retained, not current)"
    elif [ -r "$PID_DIR/seraph-prod-restart-release" ]; then
        production_read_validate_accepted_state "$PID_DIR/seraph-prod-restart-release" || return 1
        echo "Release state: restart awaiting LAN acceptance (prior accepted receipt is stale)"
    elif [ -r "$PID_DIR/seraph-prod-candidate-release" ]; then
        production_read_validate_accepted_state "$PID_DIR/seraph-prod-candidate-release" || return 1
        if [ -r "$PID_DIR/seraph-prod-active-release" ]; then echo "Release state: candidate awaiting Mac LAN acceptance (previous accepted tuple retained for rollback)"; else echo "Release state: candidate awaiting Mac LAN acceptance"; fi
    elif [ -r "$PID_DIR/seraph-prod-active-release" ]; then
        production_read_validate_accepted_state "$PID_DIR/seraph-prod-active-release" || return 1
        echo "Release state: accepted"
    else
        echo "Release state: none"
    fi
    docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" ps
    echo "Published TLS listener owned by this compose project:"
    docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" port ingress 443 2>/dev/null || echo "ingress not running"
    echo "Docker container listener ownership:"
    production_docker_inventory_validate true
}

function production_cleanup_candidate_state() {
    local stage="${1:-candidate}"
    rm -f "$PID_DIR/seraph-prod-$stage-release" "$PID_DIR/seraph-prod-$stage-challenge.json" "$PID_DIR/seraph-prod-final-attestation.json"
    [ "$stage" = candidate ] && rm -f "$PID_DIR/seraph-prod-candidate-attestation.json"
}

function production_lock_run() {
    ensure_runtime_dirs
    exec 9>"$PID_DIR/seraph-prod-lifecycle.lock"
    flock -n 9 || { echo "another production lifecycle mutation is running" >&2; return 1; }
    "$@"
}

function production_any_staged_state() {
    [ -r "$PID_DIR/seraph-prod-candidate-release" ] || [ -r "$PID_DIR/seraph-prod-rollback-release" ] || [ -r "$PID_DIR/seraph-prod-restore-release" ] || [ -r "$PID_DIR/seraph-prod-restart-release" ]
}

function production_validate_mac_receipt() {
    local receipt="$1"
    local challenge_file="${2:-}"
    [ -r "$challenge_file" ] || { echo "acceptance challenge is missing" >&2; return 1; }
    SERAPH_ACCEPTANCE_CHALLENGE_FILE="$challenge_file" python3 "$SCRIPT_DIR/scripts/validate_mac_lan_negative.py" <"$receipt" || return 1
    MAC_RECEIPT_NONCE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["nonce"])' "$receipt") || return 1
    MAC_CHALLENGE_NONCE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["challenge"]["server_nonce"])' "$receipt") || return 1
    local consumed_file="$PID_DIR/seraph-prod-consumed-acceptance"
    if [ -r "$consumed_file" ] && grep -Fx "nonce:$MAC_RECEIPT_NONCE" "$consumed_file" >/dev/null; then
        echo "Mac acceptance nonce was already used" >&2
        return 1
    fi
    if [ -r "$consumed_file" ] && grep -Fx "challenge:$MAC_CHALLENGE_NONCE" "$consumed_file" >/dev/null; then
        echo "server acceptance challenge was already used" >&2
        return 1
    fi
}

function production_issue_challenge() {
    local stage="$1" app="$2" vlm="$3" attestation="$4" output="$PID_DIR/seraph-prod-$1-challenge.json" tmp="$output.tmp.$$"
    python3 "$SCRIPT_DIR/scripts/generate_acceptance_challenge.py" --stage "$stage" --app-sha "$app" --vlm-image "$vlm" --local-attestation "$attestation" --origin "$SERAPH_EXPECTED_HTTPS_ORIGIN" --lan-host "$SERAPH_LAN_HOST" --lan-ip "$SERAPH_LAN_IP" --client-identity "$SERAPH_MAC_PROBE_CLIENT_ID" >"$tmp" || return 1
    chmod 0444 "$tmp" && mv "$tmp" "$output"
}

function production_docker_inventory_validate() {
    local require_nonempty="${1:-false}"
    local inventory
    inventory=$(mktemp /tmp/seraph-docker-inventory.XXXXXX)
    if ! docker ps --format '{{json .}}' >"$inventory"; then rm -f "$inventory"; return 1; fi
    if [ "$require_nonempty" = true ] && [ ! -s "$inventory" ]; then rm -f "$inventory"; echo "Docker inventory is unexpectedly empty" >&2; return 1; fi
    python3 "$SCRIPT_DIR/scripts/validate_production_listeners.py" <"$inventory"
    local rc=$?
    rm -f "$inventory"
    return "$rc"
}

function production_compose_state_validate() {
    local state
    state=$(mktemp /tmp/seraph-compose-state.XXXXXX)
    if ! docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" ps --format json >"$state"; then rm -f "$state"; return 1; fi
    python3 "$SCRIPT_DIR/scripts/validate_production_compose_state.py" <"$state"
    local rc=$?
    rm -f "$state"
    return "$rc"
}

function production_generate_local_attestation() {
    local output="$1" vlm_image="$2"
    local ingress_id backend_id vlm_id
    ingress_id=$(docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" ps -q ingress) || return 1
    backend_id=$(docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" ps -q backend) || return 1
    vlm_id=$(docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" ps -q vlm-wrapper) || return 1
    python3 "$SCRIPT_DIR/scripts/generate_local_gpu_inventory.py" --expected-vlm-image "$vlm_image" --ingress-container "$ingress_id" --backend-container "$backend_id" --vlm-container "$vlm_id" --vlm-api-key-file "$SERAPH_VLM_API_KEY_FILE" >"$output"
}

function production_restore_previous() {
    local previous_tag="$1"
    local previous_vlm="$2"
    local previous_receipt="$3"
    local previous_local_receipt="$previous_receipt" previous_schema
    if [ -r "$previous_receipt" ]; then
        previous_schema=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("schema",""))' "$previous_receipt" 2>/dev/null || true)
        if [ "$previous_schema" = "seraph.production-acceptance.v1" ]; then previous_local_receipt=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["final_attestation"]["path"])' "$previous_receipt") || return 2; fi
    fi
    if [ -n "$previous_tag" ] && [ -n "$previous_vlm" ] && [ -r "$previous_local_receipt" ] && docker image inspect "seraph/backend:$previous_tag" >/dev/null 2>&1 && docker image inspect "seraph/frontend-ingress:$previous_tag" >/dev/null 2>&1 && docker image inspect "$previous_vlm" >/dev/null 2>&1 && SERAPH_ALLOW_PREVIOUS_ACCEPTED_RECEIPT=true production_host_inventory_validate "$previous_local_receipt" "$previous_vlm"; then
        echo "restoring previous production release tuple $previous_tag + $previous_vlm" >&2
        if ! SERAPH_IMAGE_TAG="$previous_tag" SERAPH_VLM_IMAGE="$previous_vlm" docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" up -d --no-build --wait --wait-timeout 120; then
            echo "CATASTROPHIC: previous production release tuple could not be restored; diagnostics retained in $LOG_DIR" >&2
            return 2
        fi
        if ! SERAPH_IMAGE_TAG="$previous_tag" SERAPH_VLM_IMAGE="$previous_vlm" docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" exec -T backend uv run python production_preflight.py; then
            docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" logs --no-color --tail 500 >"$LOG_DIR/seraph-prod-failed-restore.log" 2>&1 || true
            echo "CATASTROPHIC: previous release restored containers but failed inference preflight" >&2
            return 2
        fi
        ensure_runtime_dirs
        local restore_attestation="$PID_DIR/seraph-prod-restore-attestation.json"
        if ! SERAPH_IMAGE_TAG="$previous_tag" SERAPH_VLM_IMAGE="$previous_vlm" production_generate_local_attestation "$restore_attestation" "$previous_vlm" || ! SERAPH_VLM_IMAGE="$previous_vlm" production_host_inventory_validate "$restore_attestation" "$previous_vlm" || ! SERAPH_IMAGE_TAG="$previous_tag" SERAPH_VLM_IMAGE="$previous_vlm" production_write_accepted_state "$PID_DIR/seraph-prod-restore-release" "$restore_attestation"; then
            echo "CATASTROPHIC: restored tuple failed fresh local attestation" >&2
            return 2
        fi
        production_issue_challenge restore "$previous_tag" "$previous_vlm" "$restore_attestation" || return 2
        echo "previous tuple restored locally; restore awaiting fresh Mac LAN acceptance" >&2
        return 0
    else
        echo "no verified previous release is available; stopping failed first deployment" >&2
        docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" down
        return 1
    fi
}

function production_restore_after_failure() {
    production_restore_previous "$1" "$2" "$3"
    local restore_rc=$?
    [ "$restore_rc" -eq 2 ] && return 2
    return 1
}

function production_prepare_app_images() {
    local present=0
    local image revision
    for image in "seraph/backend:$SERAPH_IMAGE_TAG" "seraph/frontend-ingress:$SERAPH_IMAGE_TAG"; do
        if docker image inspect "$image" >/dev/null 2>&1; then
            revision=$(docker image inspect "$image" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')
            [ "$revision" = "$SERAPH_IMAGE_TAG" ] || error_exit "existing $image has mismatched build identity; refusing overwrite"
            present=$((present + 1))
        fi
    done
    [ "$present" -ne 1 ] || error_exit "partial release image tuple exists; refusing overwrite"
    if [ "$present" -eq 0 ]; then
        docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" build ingress backend
    else
        echo "reusing verified application images for $SERAPH_IMAGE_TAG"
    fi
}

function production_start() {
    production_config_validate
    ensure_runtime_dirs
    if production_any_staged_state; then
        echo "a production stage already awaits LAN acceptance; complete that exact stage before starting another mutation" >&2
        return 1
    fi
    local predeploy_receipt="$PID_DIR/seraph-gpu-predeploy.json"
    python3 "$SCRIPT_DIR/scripts/generate_local_gpu_predeploy.py" >"$predeploy_receipt" || return 1
    python3 "$SCRIPT_DIR/scripts/validate_gpu_predeploy.py" <"$predeploy_receipt" || return 1
    production_docker_inventory_validate false || return 1
    local active_tag_file="$PID_DIR/seraph-prod-active-release"
    local previous_tag=""
    local previous_vlm=""
    local previous_receipt=""
    local running_backend_id running_vlm_id
    running_backend_id=$(docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" ps -q backend 2>/dev/null || true)
    running_vlm_id=$(docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" ps -q vlm-wrapper 2>/dev/null || true)
    if { [ -n "$running_backend_id" ] || [ -n "$running_vlm_id" ]; } && [ ! -r "$active_tag_file" ]; then
        echo "running production containers lack an accepted release tuple; explicit adoption is required" >&2
        return 1
    fi
    if [ -r "$active_tag_file" ]; then
        production_read_validate_accepted_state "$active_tag_file" || { echo "accepted release state is invalid; explicit adoption is required" >&2; return 1; }
        previous_tag="$ACCEPTED_APP_TAG"
        previous_vlm="$ACCEPTED_VLM_IMAGE"
        previous_receipt="$ACCEPTED_INVENTORY_RECEIPT"
    fi
    production_prepare_app_images || return 1
    if [[ "$SERAPH_VLM_IMAGE" =~ ^sha256:[0-9a-fA-F]{64}$ ]]; then
        docker image inspect "$SERAPH_VLM_IMAGE" >/dev/null 2>&1 || { echo "pinned local VLM image ID is missing" >&2; return 1; }
    else
        docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" pull vlm-wrapper || return 1
    fi
    python3 "$SCRIPT_DIR/scripts/generate_local_gpu_predeploy.py" >"$predeploy_receipt" || return 1
    python3 "$SCRIPT_DIR/scripts/validate_gpu_predeploy.py" <"$predeploy_receipt" || return 1
    if ! docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" up -d --no-build --wait --wait-timeout 120; then
        echo "candidate containers did not become healthy" >&2
        docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" logs --no-color --tail 500 >"$LOG_DIR/seraph-prod-failed-candidate.log" 2>&1 || true
        production_restore_after_failure "$previous_tag" "$previous_vlm" "$previous_receipt"; return $?
    fi
    if ! docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" exec -T backend uv run python production_preflight.py; then
        echo "candidate GPU model/VLM preflight failed" >&2
        docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" logs --no-color --tail 500 >"$LOG_DIR/seraph-prod-failed-candidate.log" 2>&1 || true
        production_restore_after_failure "$previous_tag" "$previous_vlm" "$previous_receipt"; return $?
    fi
    local candidate_receipt="$PID_DIR/seraph-prod-candidate-attestation.json"
    local candidate_state="$PID_DIR/seraph-prod-candidate-release"
    local vlm_container
    vlm_container=$(docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" ps -q vlm-wrapper) || return 1
    if ! production_generate_local_attestation "$candidate_receipt" "$SERAPH_VLM_IMAGE" || ! production_host_inventory_validate "$candidate_receipt" "$SERAPH_VLM_IMAGE" || ! production_docker_inventory_validate true || ! production_compose_state_validate; then
        echo "final production acceptance gate failed" >&2
        production_restore_after_failure "$previous_tag" "$previous_vlm" "$previous_receipt"; return $?
    fi
    if ! production_write_accepted_state "$candidate_state" "$candidate_receipt"; then
        echo "failed to persist candidate release state" >&2
        production_restore_after_failure "$previous_tag" "$previous_vlm" "$previous_receipt"; return $?
    fi
    production_issue_challenge candidate "$SERAPH_IMAGE_TAG" "$SERAPH_VLM_IMAGE" "$candidate_receipt" || return 1
    echo "candidate awaiting Mac LAN acceptance; run: ./manage.sh -e prod production accept <mac-negative-receipt>"
}

function production_accept() {
    production_accept_staged "candidate" "$1"
}

function production_abort() {
    local stage="${1:-}"
    if [ "$stage" != candidate ] && [ "$stage" != rollback ] && [ "$stage" != restore ] && [ "$stage" != restart ]; then
        error_exit "production abort requires one exact stage: candidate, rollback, restore, or restart"
    fi
    local staged="$PID_DIR/seraph-prod-$stage-release" active="$PID_DIR/seraph-prod-active-release"
    [ -r "$staged" ] || error_exit "production $stage stage does not exist"
    local previous_tag="" previous_vlm="" previous_receipt=""
    if [ -r "$active" ]; then
        production_read_validate_accepted_state "$active" || return 1
        previous_tag="$ACCEPTED_APP_TAG"; previous_vlm="$ACCEPTED_VLM_IMAGE"; previous_receipt="$ACCEPTED_INVENTORY_RECEIPT"
    fi
    production_cleanup_candidate_state "$stage"
    if [ -n "$previous_tag" ]; then
        production_restore_previous "$previous_tag" "$previous_vlm" "$previous_receipt" || return $?
        echo "production $stage aborted; previous accepted tuple restored locally and awaits a fresh restore acceptance"
    else
        docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" down || return 1
        echo "production $stage aborted; no previously accepted tuple existed, so production was stopped"
    fi
}

function production_accept_staged() {
    local stage="$1"
    local mac_receipt="${1:-}"
    mac_receipt="${2:-}"
    [ -r "$mac_receipt" ] || error_exit "production accept requires a readable Mac negative receipt"
    ensure_runtime_dirs
    local candidate_state="$PID_DIR/seraph-prod-$stage-release"
    local active_state="$PID_DIR/seraph-prod-active-release"
    production_read_validate_accepted_state "$candidate_state" || return 1
    production_validate_mac_receipt "$mac_receipt" "$PID_DIR/seraph-prod-$stage-challenge.json" || return 1
    local prior_binding fresh_binding fresh_receipt challenged_receipt
    challenged_receipt="$ACCEPTED_INVENTORY_RECEIPT"
    prior_binding=$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["compose_observation"]["containers"],sort_keys=True))' "$challenged_receipt") || return 1
    fresh_receipt="$PID_DIR/seraph-prod-final-attestation.json"
    production_generate_local_attestation "$fresh_receipt" "$ACCEPTED_VLM_IMAGE" || return 1
    fresh_binding=$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["compose_observation"]["containers"],sort_keys=True))' "$fresh_receipt") || return 1
    [ "$prior_binding" = "$fresh_binding" ] || { echo "$stage container/network binding changed before acceptance" >&2; return 1; }
    SERAPH_VLM_IMAGE="$ACCEPTED_VLM_IMAGE" production_host_inventory_validate "$fresh_receipt" "$ACCEPTED_VLM_IMAGE" || return 1
    production_compose_state_validate || return 1
    SERAPH_IMAGE_TAG="$ACCEPTED_APP_TAG" SERAPH_VLM_IMAGE="$ACCEPTED_VLM_IMAGE" production_write_acceptance_bundle "$active_state" "$challenged_receipt" "$fresh_receipt" "$mac_receipt" "$stage" || return 1
    production_cleanup_candidate_state "$stage"
    echo "production $stage release accepted with fresh local and Mac evidence"
}

function production_restart() {
    production_config_validate
    ensure_runtime_dirs
    production_any_staged_state && error_exit "restart refused while another stage awaits LAN acceptance"
    local active="$PID_DIR/seraph-prod-active-release"
    production_read_validate_accepted_state "$active" || error_exit "restart requires an accepted release"
    [ "$ACCEPTED_APP_TAG" = "$SERAPH_IMAGE_TAG" ] && [ "$ACCEPTED_VLM_IMAGE" = "$SERAPH_VLM_IMAGE" ] || error_exit "restart only supports the identical accepted tuple; use start for a new candidate"
    production_cleanup_candidate_state restart
    docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" up -d --no-build --force-recreate --wait --wait-timeout 120 || return 1
    docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" exec -T backend uv run python production_preflight.py || return 1
    local receipt="$PID_DIR/seraph-prod-restart-attestation.json"
    production_generate_local_attestation "$receipt" "$SERAPH_VLM_IMAGE" && production_host_inventory_validate "$receipt" "$SERAPH_VLM_IMAGE" && production_write_accepted_state "$PID_DIR/seraph-prod-restart-release" "$receipt" || return 1
    production_issue_challenge restart "$SERAPH_IMAGE_TAG" "$SERAPH_VLM_IMAGE" "$receipt" || return 1
    echo "restart locally attested; restart awaiting fresh Mac LAN acceptance"
}

function production_write_accepted_state() {
    local state_file="$1"
    local receipt="$2"
    local release_dir="$PID_DIR/releases"
    mkdir -p "$release_dir"
    local receipt_hash receipt_copy receipt_tmp state_tmp
    receipt_hash=$(sha256sum "$receipt" | awk '{print $1}') || return 1
    receipt_copy="$release_dir/$SERAPH_IMAGE_TAG-$receipt_hash.json"
    receipt_tmp="$receipt_copy.tmp.$$"
    cp "$receipt" "$receipt_tmp" || return 1
    chmod 0444 "$receipt_tmp" || return 1
    sync "$receipt_tmp" 2>/dev/null || true
    mv "$receipt_tmp" "$receipt_copy" || return 1
    state_tmp="$state_file.tmp.$$"
    printf '%s\n%s\n%s\n' "$SERAPH_IMAGE_TAG" "$SERAPH_VLM_IMAGE" "$receipt_copy" >"$state_tmp" || return 1
    chmod 0600 "$state_tmp" || return 1
    sync "$state_tmp" 2>/dev/null || true
    mv "$state_tmp" "$state_file"
}

function production_write_acceptance_bundle() {
    local state_file="$1" challenged_receipt="$2" final_receipt="$3" mac_receipt="$4" stage="$5"
    local release_dir="$PID_DIR/releases" challenged_hash final_hash mac_hash challenged_copy final_copy mac_copy bundle_tmp bundle_hash bundle_copy state_tmp
    local consumed_file="$PID_DIR/seraph-prod-consumed-acceptance" consumed_tmp="$PID_DIR/seraph-prod-consumed-acceptance.tmp.$$"
    mkdir -p "$release_dir"
    if [ -r "$consumed_file" ] && grep -Fx "nonce:$MAC_RECEIPT_NONCE" "$consumed_file" >/dev/null; then echo "Mac acceptance nonce was already used" >&2; return 1; fi
    if [ -r "$consumed_file" ] && grep -Fx "challenge:$MAC_CHALLENGE_NONCE" "$consumed_file" >/dev/null; then echo "server acceptance challenge was already used" >&2; return 1; fi
    challenged_hash=$(sha256sum "$challenged_receipt" | awk '{print $1}') || return 1
    final_hash=$(sha256sum "$final_receipt" | awk '{print $1}') || return 1
    mac_hash=$(sha256sum "$mac_receipt" | awk '{print $1}') || return 1
    challenged_copy="$release_dir/$SERAPH_IMAGE_TAG-challenged-$challenged_hash.json"; final_copy="$release_dir/$SERAPH_IMAGE_TAG-final-$final_hash.json"; mac_copy="$release_dir/$SERAPH_IMAGE_TAG-mac-$mac_hash.json"
    cp "$challenged_receipt" "$challenged_copy" && cp "$final_receipt" "$final_copy" && cp "$mac_receipt" "$mac_copy" && chmod 0444 "$challenged_copy" "$final_copy" "$mac_copy" || return 1
    local ingress_id backend_id vlm_id network_id
    ingress_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["compose_observation"]["containers"]["ingress"]["container_id"])' "$final_receipt") || return 1
    backend_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["compose_observation"]["containers"]["backend"]["container_id"])' "$final_receipt") || return 1
    vlm_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["compose_observation"]["containers"]["vlm-wrapper"]["container_id"])' "$final_receipt") || return 1
    network_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["compose_observation"]["containers"]["ingress"]["network_id"])' "$final_receipt") || return 1
    bundle_tmp="$release_dir/bundle.tmp.$$"
    python3 -c 'import json,sys; print(json.dumps({"schema":"seraph.production-acceptance.v1","stage":sys.argv[15],"app_sha":sys.argv[1],"vlm_image":sys.argv[2],"compose_project":"seraph-prod","network_name":"seraph-core-prod","network_id":sys.argv[3],"container_ids":{"ingress":sys.argv[4],"backend":sys.argv[5],"vlm-wrapper":sys.argv[6]},"challenged_attestation":{"path":sys.argv[7],"sha256":sys.argv[8]},"final_attestation":{"path":sys.argv[9],"sha256":sys.argv[10]},"mac_receipt":{"path":sys.argv[11],"sha256":sys.argv[12]},"acceptance_challenge":json.load(open(sys.argv[11]))["challenge"],"expected_origin":sys.argv[13],"client_identity":sys.argv[14]},sort_keys=True,separators=(",",":")))' "$SERAPH_IMAGE_TAG" "$SERAPH_VLM_IMAGE" "$network_id" "$ingress_id" "$backend_id" "$vlm_id" "$challenged_copy" "$challenged_hash" "$final_copy" "$final_hash" "$mac_copy" "$mac_hash" "$SERAPH_EXPECTED_HTTPS_ORIGIN" "$SERAPH_MAC_PROBE_CLIENT_ID" "$stage" >"$bundle_tmp" || return 1
    SERAPH_BUNDLE_APP_SHA="$SERAPH_IMAGE_TAG" SERAPH_BUNDLE_VLM_IMAGE="$SERAPH_VLM_IMAGE" python3 "$SCRIPT_DIR/scripts/validate_acceptance_bundle.py" <"$bundle_tmp" || return 1
    bundle_hash=$(sha256sum "$bundle_tmp" | awk '{print $1}'); bundle_copy="$release_dir/$SERAPH_IMAGE_TAG-$bundle_hash.json"
    state_tmp="$state_file.tmp.$$"; printf '%s\n%s\n%s\n' "$SERAPH_IMAGE_TAG" "$SERAPH_VLM_IMAGE" "$bundle_copy" >"$state_tmp" || return 1
    { [ -r "$consumed_file" ] && cat "$consumed_file"; printf 'nonce:%s\nchallenge:%s\n' "$MAC_RECEIPT_NONCE" "$MAC_CHALLENGE_NONCE"; } >"$consumed_tmp" || return 1
    chmod 0444 "$bundle_tmp" && chmod 0600 "$state_tmp" "$consumed_tmp" || return 1
    sync "$challenged_copy" "$final_copy" "$mac_copy" "$bundle_tmp" "$state_tmp" "$consumed_tmp" 2>/dev/null || true
    mv "$bundle_tmp" "$bundle_copy" || return 1
    mv "$consumed_tmp" "$consumed_file" || return 1
    mv "$state_tmp" "$state_file"
}

function production_read_validate_accepted_state() {
    local state_file="$1"
    [ -r "$state_file" ] || { echo "accepted release state is missing" >&2; return 1; }
    [ "$(awk 'END {print NR}' "$state_file")" -eq 3 ] || { echo "accepted release state must contain exactly three lines" >&2; return 1; }
    local tag vlm receipt hash expected_name mode revision image
    tag=$(sed -n '1p' "$state_file")
    vlm=$(sed -n '2p' "$state_file")
    receipt=$(sed -n '3p' "$state_file")
    [[ "$tag" =~ ^[0-9a-f]{40}$ ]] || { echo "accepted application SHA is invalid" >&2; return 1; }
    [[ "$vlm" =~ ^([^[:space:]@]+@sha256:[0-9a-fA-F]{64}|sha256:[0-9a-fA-F]{64})$ ]] || { echo "accepted VLM immutable reference is invalid" >&2; return 1; }
    [ -r "$receipt" ] || { echo "accepted inventory copy is unreadable" >&2; return 1; }
    hash=$(sha256sum "$receipt" | awk '{print $1}') || return 1
    expected_name="$tag-$hash.json"
    [ "$(basename "$receipt")" = "$expected_name" ] || { echo "accepted inventory copy is not hash-bound" >&2; return 1; }
    mode=$(stat -c '%a' "$receipt" 2>/dev/null || stat -f '%Lp' "$receipt")
    [ "$mode" = "444" ] || { echo "accepted inventory copy must be mode 0444" >&2; return 1; }
    for image in "seraph/backend:$tag" "seraph/frontend-ingress:$tag"; do
        docker image inspect "$image" >/dev/null 2>&1 || { echo "accepted image missing: $image" >&2; return 1; }
        revision=$(docker image inspect "$image" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')
        [ "$revision" = "$tag" ] || { echo "accepted image label mismatch: $image" >&2; return 1; }
    done
    docker image inspect "$vlm" >/dev/null 2>&1 || { echo "accepted VLM image missing" >&2; return 1; }
    local receipt_schema challenged_attestation final_attestation
    receipt_schema=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("schema",""))' "$receipt") || return 1
    if [ "$receipt_schema" = "seraph.production-acceptance.v1" ]; then
        SERAPH_BUNDLE_APP_SHA="$tag" SERAPH_BUNDLE_VLM_IMAGE="$vlm" python3 "$SCRIPT_DIR/scripts/validate_acceptance_bundle.py" <"$receipt" || return 1
        challenged_attestation=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["challenged_attestation"]["path"])' "$receipt") || return 1
        final_attestation=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["final_attestation"]["path"])' "$receipt") || return 1
        SERAPH_ALLOW_PREVIOUS_ACCEPTED_RECEIPT=true production_host_inventory_validate "$challenged_attestation" "$vlm" || return 1
        SERAPH_ALLOW_PREVIOUS_ACCEPTED_RECEIPT=true production_host_inventory_validate "$final_attestation" "$vlm" || return 1
    else
        SERAPH_ALLOW_PREVIOUS_ACCEPTED_RECEIPT=true production_host_inventory_validate "$receipt" "$vlm" || return 1
    fi
    ACCEPTED_APP_TAG="$tag"
    ACCEPTED_VLM_IMAGE="$vlm"
    ACCEPTED_INVENTORY_RECEIPT="$receipt"
}

function production_rollback() {
    local tag="${1:-}"
    local vlm_image="${2:-}"
    [ -n "$tag" ] && [ -n "$vlm_image" ] || error_exit "rollback requires application SHA and VLM digest"
    ensure_runtime_dirs
    production_any_staged_state && error_exit "rollback refused while another stage awaits LAN acceptance"
    [[ "$tag" =~ ^[0-9a-f]{40}$ ]] || error_exit "rollback application tag must be a full git SHA"
    [[ "$vlm_image" =~ ^([^[:space:]@]+@sha256:[0-9a-fA-F]{64}|sha256:[0-9a-fA-F]{64})$ ]] || error_exit "rollback VLM image must use a registry RepoDigest or exact local sha256 image ID"
    docker image inspect "seraph/backend:$tag" >/dev/null 2>&1 || error_exit "missing seraph/backend:$tag"
    docker image inspect "seraph/frontend-ingress:$tag" >/dev/null 2>&1 || error_exit "missing seraph/frontend-ingress:$tag"
    docker image inspect "$vlm_image" >/dev/null 2>&1 || error_exit "missing $vlm_image"
    SERAPH_IMAGE_TAG="$tag" SERAPH_VLM_IMAGE="$vlm_image" SERAPH_VALIDATING_ROLLBACK=true production_config_validate
    ensure_runtime_dirs
    local active_tag_file="$PID_DIR/seraph-prod-active-release"
    local original_tag=""
    local original_vlm=""
    local original_receipt=""
    local running_backend_id running_vlm_id
    running_backend_id=$(docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" ps -q backend 2>/dev/null || true)
    running_vlm_id=$(docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" ps -q vlm-wrapper 2>/dev/null || true)
    if { [ -n "$running_backend_id" ] || [ -n "$running_vlm_id" ]; } && [ ! -r "$active_tag_file" ]; then echo "running production containers require explicit adoption before rollback" >&2; return 1; fi
    if [ -r "$active_tag_file" ]; then
        production_read_validate_accepted_state "$active_tag_file" || { echo "accepted release state is invalid; refusing rollback cutover" >&2; return 1; }
        original_tag="$ACCEPTED_APP_TAG"; original_vlm="$ACCEPTED_VLM_IMAGE"; original_receipt="$ACCEPTED_INVENTORY_RECEIPT"
    fi
    if ! SERAPH_IMAGE_TAG="$tag" SERAPH_VLM_IMAGE="$vlm_image" docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" up -d --no-build --wait --wait-timeout 120 || ! SERAPH_IMAGE_TAG="$tag" SERAPH_VLM_IMAGE="$vlm_image" docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" exec -T backend uv run python production_preflight.py; then
        docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" logs --no-color --tail 500 >"$LOG_DIR/seraph-prod-failed-rollback.log" 2>&1 || true
        echo "requested rollback tuple failed; restoring original active tuple" >&2
        production_restore_after_failure "$original_tag" "$original_vlm" "$original_receipt"; return $?
    fi
    local target_attestation="$PID_DIR/seraph-prod-rollback-attestation.json" target_container
    target_container=$(SERAPH_IMAGE_TAG="$tag" SERAPH_VLM_IMAGE="$vlm_image" docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" ps -q vlm-wrapper) || return 1
    if ! production_generate_local_attestation "$target_attestation" "$vlm_image" || ! SERAPH_VLM_IMAGE="$vlm_image" production_host_inventory_validate "$target_attestation" "$vlm_image"; then
        production_restore_after_failure "$original_tag" "$original_vlm" "$original_receipt"; return $?
    fi
    if ! SERAPH_IMAGE_TAG="$tag" SERAPH_VLM_IMAGE="$vlm_image" production_write_accepted_state "$PID_DIR/seraph-prod-rollback-release" "$target_attestation"; then
        production_restore_after_failure "$original_tag" "$original_vlm" "$original_receipt"; return $?
    fi
    production_issue_challenge rollback "$tag" "$vlm_image" "$target_attestation" || { production_restore_after_failure "$original_tag" "$original_vlm" "$original_receipt"; return $?; }
    echo "rollback deployed and locally attested; rollback awaiting fresh Mac LAN acceptance"
}

if [ "${SERAPH_MANAGE_SOURCE_ONLY:-false}" = "true" ]; then
    return 0 2>/dev/null || exit 0
fi

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

if grep -E "^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=[^\"'][^#]*;" "$ENV_FILE" >/dev/null 2>&1; then
    error_exit "Env values containing semicolons must be quoted in $ENV_FILE because manage.sh sources the env file."
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
LOCAL_DEFAULT_MODEL="${LOCAL_DEFAULT_MODEL:-${DEFAULT_MODEL:-openrouter/anthropic/claude-sonnet-4}}"
SCREEN_CAPTURE_ARCHIVE_DIR="${SCREEN_CAPTURE_ARCHIVE_DIR:-$LOCAL_WORKSPACE_DIR/artifacts/screen-captures}"
SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR="${SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR:-$SCREEN_CAPTURE_ARCHIVE_DIR}"
SERAPH_DAEMON_STATUS_FILE="${SERAPH_DAEMON_STATUS_FILE:-$LOCAL_WORKSPACE_DIR/daemon-status.json}"
REPORT_ARCHIVE_DIR="${REPORT_ARCHIVE_DIR:-$LOCAL_WORKSPACE_DIR/artifacts/reports}"
export SCREEN_CAPTURE_ARCHIVE_DIR SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR SERAPH_DAEMON_STATUS_FILE REPORT_ARCHIVE_DIR
if [ "$COMMAND" = "local" ]; then
    DEFAULT_MODEL="$LOCAL_DEFAULT_MODEL"
fi

# --- Execution ---

if [ "$COMMAND" = "production" ]; then
    PROD_SUB="${1:-}"
    case "$PROD_SUB" in
        config-validate) production_config_validate ;;
        start) production_lock_run production_start ;;
        stop) production_lock_run docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" down ;;
        restart) production_lock_run production_restart ;;
        status) production_status ;;
        accept) production_lock_run production_accept "${2:-}" ;;
        accept-rollback) production_lock_run production_accept_staged "rollback" "${2:-}" ;;
        accept-restore) production_lock_run production_accept_staged "restore" "${2:-}" ;;
        accept-restart) production_lock_run production_accept_staged "restart" "${2:-}" ;;
        abort) production_lock_run production_abort "${2:-}" ;;
        logs) shift || true; docker compose --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" logs "$@" ;;
        rollback) production_lock_run production_rollback "${2:-}" "${3:-}" ;;
        *) error_exit "Unknown production subcommand '$PROD_SUB'. Use: config-validate, start, accept, rollback, accept-rollback, restart, accept-restart, accept-restore, abort, status, logs, stop" ;;
    esac
    exit 0
fi

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
