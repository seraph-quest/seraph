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


def _npm_env(node: Path) -> dict[str, str]:
    # npm loads configuration even for --version. Keep both commands private.
    return {**CHILD_ENV, "PATH": str(node.parent) + os.pathsep + os.defpath,
            "HOME": str(PACKAGE_ROOT / ".build-home"),
            "npm_config_userconfig": str(PACKAGE_ROOT / ".absent-user-npmrc"),
            "npm_config_globalconfig": str(PACKAGE_ROOT / ".absent-global-npmrc"),
            "npm_config_update_notifier": "false", "npm_config_audit": "false"}


def _check_npm_configuration() -> None:
    # --prefix fixes project lookup here; none of these config files is reviewed.
    # lstat also rejects broken symlinks; inspection errors must fail closed.
    for name in (".npmrc", ".absent-user-npmrc", ".absent-global-npmrc"):
        try:
            (PACKAGE_ROOT / name).lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise CompositionBlocked("npm_configuration_unreviewed") from exc
        raise CompositionBlocked("npm_configuration_unreviewed")


def reviewed_npm(node: Path) -> Path:
    """Prefer an exact bundled pin, then an independently installed trusted CLI."""
    _check_npm_configuration()
    bundled = node.parent.parent / "lib/node_modules/npm/bin/npm-cli.js"
    found = shutil.which("npm")
    candidates = [bundled, *([Path(found)] if found else [])]
    present = False
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            npm = candidate.resolve(strict=True)
        except FileNotFoundError:
            continue
        if npm in seen:
            continue
        seen.add(npm)
        present = True
        _trusted_file(npm)
        if npm.suffix != ".js":
            raise CompositionBlocked("unreviewed_runtime_path")
        try:
            result = subprocess.run([str(node), str(npm), "--prefix", str(PACKAGE_ROOT), "--version"], env=_npm_env(node), cwd=PACKAGE_ROOT,
                                    close_fds=True, capture_output=True, check=True, timeout=2)
            if result.stdout.decode("ascii").strip() == NPM_VERSION:
                return npm
        except (OSError, UnicodeError, subprocess.SubprocessError):
            continue
    raise CompositionBlocked("npm_unsupported" if present else "npm_missing")


def build(node_path: Path | None) -> int:
    try:
        node, _ = reviewed_node(node_path)
        npm = reviewed_npm(node)
        # Build never installs dependencies or reads operator npm configuration.
        if not (PACKAGE_ROOT / "node_modules/typescript/bin/tsc").is_file():
            raise CompositionBlocked("build_dependencies_missing")
        _check_npm_configuration()
        result = subprocess.run([str(node), str(npm), "--prefix", str(PACKAGE_ROOT), "run", "build"], cwd=PACKAGE_ROOT,
                                env=_npm_env(node), close_fds=True, timeout=60)
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
