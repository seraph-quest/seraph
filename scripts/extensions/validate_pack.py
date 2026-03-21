#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if sys.version_info < (3, 10):
    raise SystemExit("Use Python 3.10+ or backend/.venv/bin/python to run this script.")

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from src.extensions.scaffold import validate_extension_package


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a Seraph extension package.")
    parser.add_argument("package_root", help="Package directory or manifest path")
    parser.add_argument("--seraph-version", help="Runtime version to validate against")
    args = parser.parse_args()

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
