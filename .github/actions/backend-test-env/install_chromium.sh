#!/usr/bin/env bash
# Chromium for the backend test lanes (#1290), run from backend/.
#
# The browser build comes from the actions/cache entry keyed on the locked
# Playwright version; only a cache miss downloads it from Playwright's CDN.
# Chromium's runtime libraries are checked against the runner image, and apt
# is used only for a package the image lacks. `--with-deps` is not used: it
# also pulls ~32 MB of optional fonts and upgrades already-present Mesa
# libraries from the Ubuntu mirror in every job, which is what kept timing
# out (exit 124) before any test ran.
#
# Every network or image failure here is recorded in $SERAPH_CI_INFRA_FILE so
# the "Classify failures" step reports it as infrastructure, not as a test
# failure. Nothing is retried.
set -euo pipefail

infra_file="${SERAPH_CI_INFRA_FILE:?}"
infra() {
  echo "$1" >> "$infra_file"
  echo "::error title=CI infrastructure (not a test failure)::$1"
  exit 1
}

# 1. Browser build. With a warm cache this verifies the restored build and
#    downloads nothing.
if [ "${PLAYWRIGHT_CACHE_HIT:-}" = "true" ]; then
  echo "Playwright browsers restored from cache"
else
  echo "Playwright browser cache miss: downloading from the Playwright CDN"
fi
timeout 300 uv run playwright install chromium \
  || infra "Playwright Chromium download failed or timed out (exit $?)"

# 2. Chromium's runtime packages, as Playwright itself lists them for this
#    distribution (the 'chromium' list, not the fonts/xvfb 'tools' list).
. /etc/os-release
platform="${ID}${VERSION_ID}-x64"
native_deps="$(uv run python -c 'import pathlib, playwright; print(pathlib.Path(playwright.__file__).parent / "driver/package/lib/server/registry/nativeDeps.js")')"
mapfile -t packages < <(node -e '
  const deps = require(process.argv[1]).deps[process.argv[2]];
  if (!deps) { console.error("no Playwright dependency list for " + process.argv[2]); process.exit(2); }
  for (const name of deps.chromium) console.log(name);
' "$native_deps" "$platform")
[ "${#packages[@]}" -gt 0 ] || infra "Playwright lists no Chromium runtime packages for $platform"

missing=()
for package in "${packages[@]}"; do
  status="$(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true)"
  [ "$status" = "installed" ] || missing+=("$package")
done

if [ "${#missing[@]}" -eq 0 ]; then
  echo "All ${#packages[@]} Chromium runtime packages are present on the runner image; no apt download"
else
  # Only the packages the image lacks; present packages are never upgraded.
  echo "Runner image lacks Chromium runtime packages: ${missing[*]}"
  timeout 120 sudo apt-get update -q \
    || infra "apt-get update failed or timed out (exit $?) while fetching missing Chromium packages: ${missing[*]}"
  timeout 300 sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends --no-upgrade "${missing[@]}" \
    || infra "apt-get install failed or timed out (exit $?) for missing Chromium packages: ${missing[*]}"
fi

# 3. Every installed Chromium executable must resolve all of its libraries,
#    so a browser that cannot start is caught here, not by a browser test.
browsers="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"
mapfile -t executables < <(find "$browsers" -maxdepth 3 -type f \( -name chrome -o -name chrome-headless-shell \) | sort)
[ "${#executables[@]}" -ge 2 ] || infra "expected Chromium and its headless shell under $browsers, found: ${executables[*]:-none}"
unresolved="$(for executable in "${executables[@]}"; do ldd "$executable"; done | awk '/not found/ {print $1}' | sort -u | tr '\n' ' ')"
[ -z "$unresolved" ] || infra "Chromium libraries unresolved on this runner image: $unresolved"
for executable in "${executables[@]}"; do echo "verified: $executable"; done
