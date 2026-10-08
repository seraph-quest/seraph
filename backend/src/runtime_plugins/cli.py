"""Finite managed package build/status and keyless owned-host lifecycle probe."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import os
import shutil
import subprocess
import sys

from .bridge import CordisHost
from .composition import CHILD_ENV, CORDIS_VERSION, NPM_VERSION, PACKAGE_ROOT, CompositionBlocked, _trusted_file, reviewed_composition, reviewed_node


def build(node_path: Path | None) -> int:
    try:
        node, _ = reviewed_node(node_path)
        # Prefer the explicitly chosen Node distribution's bundled npm.
        bundled = node.parent.parent / "lib/node_modules/npm/bin/npm-cli.js"
        found = shutil.which("npm")
        npm = bundled if bundled.is_file() else Path(found).resolve(strict=True) if found else None
        if npm is None:
            raise CompositionBlocked("npm_missing")
        _trusted_file(npm)
        result = subprocess.run([str(node), str(npm), "--version"], env=CHILD_ENV,
                                close_fds=True, capture_output=True, check=True, timeout=2)
        if result.stdout.decode("ascii").strip() != NPM_VERSION:
            raise CompositionBlocked("npm_unsupported")
        # Build never installs dependencies or reads operator npm configuration.
        if not (PACKAGE_ROOT / "node_modules/typescript/bin/tsc").is_file():
            raise CompositionBlocked("build_dependencies_missing")
        build_env = {**CHILD_ENV, "PATH": str(node.parent) + os.pathsep + os.defpath,
                     "HOME": str(PACKAGE_ROOT / ".build-home"),
                     "npm_config_userconfig": str(PACKAGE_ROOT / ".absent-user-npmrc"),
                     "npm_config_globalconfig": str(PACKAGE_ROOT / ".absent-global-npmrc"),
                     "npm_config_update_notifier": "false", "npm_config_audit": "false"}
        result = subprocess.run([str(node), str(npm), "run", "build"], cwd=PACKAGE_ROOT,
                                env=build_env, close_fds=True, timeout=60)
        if result.returncode:
            return result.returncode
        reviewed_composition(node_path=node)
        return 0
    except (CompositionBlocked, OSError, UnicodeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"state": "blocked", "reason": exc.reason if isinstance(exc, CompositionBlocked) else "build_failed"}))
        return 1


async def probe(node_path: Path | None) -> int:
    host = CordisHost(node_path=node_path)
    try:
        if not await host.start():
            print(json.dumps(host.snapshot()))
            return 1
        await host.request()
    finally:
        await host.stop(preserve_blocked=host.state == "blocked")
    snapshot = host.snapshot()
    print(json.dumps(snapshot))
    return 0 if snapshot["cleanup"]["state"] == "clean" and snapshot["cleanup"]["process_reaped"] and snapshot["cleanup"]["cordis_disposal"] == "confirmed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "build", "probe"))
    parser.add_argument("--node", type=Path, help="explicit reviewed Node executable; never persisted as configuration")
    args = parser.parse_args()
    if args.node is not None and not args.node.is_absolute():
        parser.error("--node must be an absolute path")
    if args.command == "build":
        return build(args.node)
    if args.command == "probe":
        return asyncio.run(probe(args.node))
    try:
        reviewed = reviewed_composition(node_path=args.node)
        print(json.dumps({"state": "available", "runtime_role": "lifecycle_host", "reason": None,
                          "cordis_version": CORDIS_VERSION, "node_version": reviewed.node_version,
                          "profile_id": reviewed.profile["profile_id"], "process_started": False,
                          "note": "Package preflight only; live readiness is /api/runtime/status.cordis_runtime"}))
        return 0
    except CompositionBlocked as exc:
        print(json.dumps({"state": "blocked", "runtime_role": "lifecycle_host", "reason": exc.reason,
                          "cordis_version": CORDIS_VERSION, "process_started": False}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
