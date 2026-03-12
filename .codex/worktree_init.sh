#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"

if [ ! -f "$repo_root/.env.dev" ] && [ -f "$repo_root/.env.dev.example" ]; then
  cp "$repo_root/.env.dev.example" "$repo_root/.env.dev"
fi

if command -v uv >/dev/null 2>&1; then
  (
    cd "$repo_root/backend"
    uv sync --group dev
  )
else
  echo "uv not found; skipping backend dependency install" >&2
fi

if command -v npm >/dev/null 2>&1; then
  for app_dir in frontend docs editor; do
    if [ -f "$repo_root/$app_dir/package-lock.json" ]; then
      (
        cd "$repo_root/$app_dir"
        npm ci
      )
    fi
  done
else
  echo "npm not found; skipping Node dependency installs" >&2
fi
