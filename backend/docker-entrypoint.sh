#!/bin/sh
set -eu

load_secret() {
    variable="$1"
    file_variable="${variable}_FILE"
    eval "file_path=\${$file_variable:-}"
    eval "current_value=\${$variable:-}"
    if [ -n "$file_path" ]; then
        [ -r "$file_path" ] || { echo "required secret file for $variable is unreadable" >&2; exit 78; }
        current_value=$(sed -e 's/[[:space:]]*$//' "$file_path")
        export "$variable=$current_value"
    fi
    unset "$file_variable"
}

load_secret OPERATOR_AUTH_SECRET
load_secret OPERATOR_AUTH_SECRET_HASH
load_secret LOCAL_LLM_API_KEY
load_secret SERAPH_VLM_API_KEY

credential_count=0
[ -n "${OPERATOR_AUTH_SECRET:-}" ] && credential_count=$((credential_count + 1))
[ -n "${OPERATOR_AUTH_SECRET_HASH:-}" ] && credential_count=$((credential_count + 1))
if [ "$credential_count" -ne 1 ]; then
    echo "production requires exactly one raw or hashed operator credential" >&2
    exit 78
fi

exec "$@"
