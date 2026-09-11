#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import yaml

if sys.version_info < (3, 10):
    raise SystemExit("Use Python 3.10+ or backend/.venv/bin/python to run this script.")

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from src.extensions.scaffold import validate_extension_package
from src.extensions.capability_pack import (
    CapabilityPackManifestError,
    parse_capability_pack_manifest,
    validate_capability_pack_package,
)


def _report_to_payload(report) -> dict:
    return {
        "ok": report.ok,
        "load_errors": [
            {
                "source": item.source,
                "phase": item.phase,
                "message": item.message,
                "details": item.details,
            }
            for item in report.load_errors
        ],
        "results": [
            {
                "extension_id": result.extension_id,
                "ok": result.ok,
                "issues": [
                    {
                        "code": issue.code,
                        "severity": issue.severity,
                        "message": issue.message,
                        "contribution_type": issue.contribution_type,
                        "reference": issue.reference,
                        "suggested_fix": issue.suggested_fix,
                    }
                    for issue in result.issues
                ],
            }
            for result in report.results
        ],
    }


def _v2_package_root(package_argument: str) -> Path | None:
    candidate = Path(package_argument)
    if candidate.is_file():
        if candidate.name not in {"manifest.yaml", "manifest.yml"}:
            return None
        manifest_path = candidate
    elif candidate.is_dir():
        manifest_path = next(
            (candidate / name for name in ("manifest.yaml", "manifest.yml") if (candidate / name).is_file()),
            None,
        )
        if manifest_path is None:
            return None
    else:
        return None
    try:
        payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    return manifest_path.parent if isinstance(payload, dict) and payload.get("schema_version") == 2 else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a Seraph extension package.")
    parser.add_argument("package_root", help="Package directory or manifest path")
    parser.add_argument("--seraph-version", help="Runtime version to validate against")
    args = parser.parse_args()

    v2_root = _v2_package_root(args.package_root)
    if v2_root is not None:
        try:
            manifest_path = next(
                path for path in (v2_root / "manifest.yaml", v2_root / "manifest.yml") if path.is_file()
            )
            manifest = parse_capability_pack_manifest(
                manifest_path.read_text(encoding="utf-8"),
                source=str(manifest_path),
            )
            result = validate_capability_pack_package(v2_root, manifest=manifest)
            if args.seraph_version and not manifest.compatibility.is_compatible_with(args.seraph_version):
                result["ok"] = False
                result.setdefault("errors", []).append(
                    f"pack compatibility {manifest.compatibility.seraph!r} excludes Seraph {args.seraph_version}"
                )
        except (CapabilityPackManifestError, OSError, UnicodeDecodeError, ValueError) as exc:
            result = {"ok": False, "error": str(exc)}
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1

    try:
        report = validate_extension_package(args.package_root, seraph_version=args.seraph_version)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1

    payload = _report_to_payload(report)
    print(json.dumps(payload, indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
