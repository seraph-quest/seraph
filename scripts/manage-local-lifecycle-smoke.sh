#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

export SERAPH_MANAGE_SOURCE_ONLY=true
# shellcheck disable=SC1091
source "$ROOT_DIR/manage.sh"

LOCAL_BACKEND_PID_FILE="$TMP_DIR/backend.pid"
LOCAL_BACKEND_PORT=18004
MOCK_LISTENER_PIDS=""
MOCK_SERAPH_OWNER=false

function pids_listening_on_port() {
    local _port="$1"
    echo "$MOCK_LISTENER_PIDS"
}

function seraph_local_port_owner() {
    local _pid="$1"
    local _service="$2"
    [ "$MOCK_SERAPH_OWNER" = true ]
}

function assert_eq() {
    local expected="$1"
    local actual="$2"
    local label="$3"
    if [ "$expected" != "$actual" ]; then
        echo "FAIL: $label: expected '$expected', got '$actual'" >&2
        exit 1
    fi
}

assert_eq "stopped" "$(service_readiness "$LOCAL_BACKEND_PID_FILE" "$LOCAL_BACKEND_PORT" backend)" "missing pid and listener"

echo "999999" > "$LOCAL_BACKEND_PID_FILE"
assert_eq "stopped" "$(service_readiness "$LOCAL_BACKEND_PID_FILE" "$LOCAL_BACKEND_PORT" backend)" "stale pid"
if [ -f "$LOCAL_BACKEND_PID_FILE" ]; then
    echo "FAIL: stale PID file was not removed" >&2
    exit 1
fi

echo "$$" > "$LOCAL_BACKEND_PID_FILE"
assert_eq "pid_only" "$(service_readiness "$LOCAL_BACKEND_PID_FILE" "$LOCAL_BACKEND_PORT" backend)" "pid without listener"

rm -f "$LOCAL_BACKEND_PID_FILE"
MOCK_LISTENER_PIDS="4242"
MOCK_SERAPH_OWNER=true
assert_eq "listener_only" "$(service_readiness "$LOCAL_BACKEND_PID_FILE" "$LOCAL_BACKEND_PORT" backend)" "listener without pid"

echo "$$" > "$LOCAL_BACKEND_PID_FILE"
MOCK_LISTENER_PIDS="$$"
MOCK_SERAPH_OWNER=false
assert_eq "ready" "$(service_readiness "$LOCAL_BACKEND_PID_FILE" "$LOCAL_BACKEND_PORT" backend)" "pid and owned listener"

echo "manage-local-lifecycle-smoke: ok"
