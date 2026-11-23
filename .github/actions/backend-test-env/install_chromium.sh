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
# Failures are recorded for the "Classify failures" step, never as test
# failures: network/runner-image failures in $SERAPH_CI_INFRA_FILE
# (infrastructure), and Playwright layout/parse failures, which are a CI
# config defect (for example after a Playwright upgrade), in
# $SERAPH_CI_CONFIG_FILE. This script has no retry loop of its own; the only
# retries are the ones Playwright's downloader makes internally. A restored
# cache that fails its checksum is discarded and installed once from scratch.
set -euo pipefail

infra_file="${SERAPH_CI_INFRA_FILE:?}"
config_file="${SERAPH_CI_CONFIG_FILE:?}"
infra() {
  echo "$1" >> "$infra_file"
  echo "::error title=CI infrastructure (not a test failure)::$1"
  exit 1
}
config() {
  echo "$1" >> "$config_file"
  echo "::error title=CI setup/config defect (not a test failure)::$1"
  exit 1
}

browsers="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"
# Written into the cached directory on a miss, so it travels with the cache.
manifest_name="seraph-chromium.sha256"
find_executables() {
  find "$browsers" -maxdepth 3 -type f \( -name chrome -o -name chrome-headless-shell \) | sort
}

# 1. Browser build. A restored cache must match the checksums recorded when
#    it was saved; otherwise it is discarded and installed fresh (once).
fresh_install=true
if [ "${PLAYWRIGHT_CACHE_HIT:-}" = "true" ]; then
  if (cd "$browsers" && sha256sum --check --strict --quiet "$manifest_name"); then
    echo "Playwright browsers restored from cache; checksums match $manifest_name"
    fresh_install=false
  else
    echo "::warning::Restored Playwright cache failed its checksum check; discarding it and installing fresh"
    rm -rf "${browsers:?}"
  fi
else
  echo "Playwright browser cache miss: downloading from the Playwright CDN"
fi
timeout 300 uv run playwright install chromium \
  || infra "Playwright Chromium download failed or timed out (exit $?)"

# 2. Chromium's runtime packages, as Playwright itself lists them for this
#    distribution (the 'chromium' list, not the fonts/xvfb 'tools' list).
#    The architecture matches the cache key's runner.arch.
case "$(uname -m)" in
  x86_64) arch=x64 ;;
  aarch64 | arm64) arch=arm64 ;;
  *) config "unsupported runner architecture $(uname -m) for Playwright's dependency lists" ;;
esac
. /etc/os-release
platform="${ID}${VERSION_ID}-${arch}"
native_deps="$(uv run python -c 'import pathlib, playwright; print(pathlib.Path(playwright.__file__).parent / "driver/package/lib/server/registry/nativeDeps.js")')" \
  || config "could not locate Playwright's nativeDeps.js (exit $?)"
[ -f "$native_deps" ] || config "Playwright's nativeDeps.js is not at $native_deps; its package layout changed"
# A missing key (for example a new runner image) fails loudly here.
package_list="$(node -e '
  const deps = require(process.argv[1]).deps;
  if (!deps || !deps[process.argv[2]]) { console.error("no Playwright dependency list for " + process.argv[2]); process.exit(2); }
  const chromium = deps[process.argv[2]].chromium;
  if (!Array.isArray(chromium) || chromium.length === 0) { console.error("empty Chromium dependency list for " + process.argv[2]); process.exit(3); }
  for (const name of chromium) console.log(name);
' "$native_deps" "$platform")" \
  || config "could not read Playwright's Chromium dependency list for $platform from nativeDeps.js (exit $?)"
mapfile -t packages <<< "$package_list"

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
mapfile -t executables < <(find_executables)
[ "${#executables[@]}" -ge 2 ] || infra "expected Chromium and its headless shell under $browsers, found: ${executables[*]:-none}"
unresolved="$(for executable in "${executables[@]}"; do ldd "$executable"; done | awk '/not found/ {print $1}' | sort -u | tr '\n' ' ')"
[ -z "$unresolved" ] || infra "Chromium libraries unresolved on this runner image: $unresolved"
for executable in "${executables[@]}"; do echo "verified: $executable"; done

# 4. After a fresh install, record the checksums the next restore verifies.
if [ "$fresh_install" = true ]; then
  (cd "$browsers" && find . -maxdepth 3 -type f \( -name chrome -o -name chrome-headless-shell \) | sort | xargs sha256sum > "$manifest_name")
  echo "recorded checksums for the cache:"
  cat "$browsers/$manifest_name"
fi
