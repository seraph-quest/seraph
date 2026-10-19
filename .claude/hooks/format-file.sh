#!/usr/bin/env bash
# PostToolUse hook (Edit|Write): format the one edited file with the project's Prettier.
# Covers only what `npm run format` covers (see package.json): AGENTS.md, package.json,
# and .claude/**/*.{json,md}. Never blocks: always exits 0.

root="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
prettier="${PRETTIER_BIN:-$root/node_modules/.bin/prettier}"

file="$(jq -r '.tool_input.file_path // empty' 2>/dev/null)"
[ -n "$file" ] || exit 0

root="$(cd "$root" 2>/dev/null && pwd -P)" || exit 0
case "$file" in
  /*) abs="$file" ;;
  *) abs="$root/$file" ;;
esac
dir="$(cd "$(dirname "$abs")" 2>/dev/null && pwd -P)" || exit 0
abs="$dir/$(basename "$abs")"

case "$abs" in
  "$root"/*) rel="${abs#"$root"/}" ;;
  *) exit 0 ;;
esac

case "$rel" in
  AGENTS.md | package.json) ;;
  .claude/settings.local.json) exit 0 ;;
  .claude/*.json | .claude/*.md) ;;
  *) exit 0 ;;
esac

[ -f "$abs" ] && [ -x "$prettier" ] || exit 0

cd "$root" && "$prettier" --write "$rel" >/dev/null 2>&1
exit 0
