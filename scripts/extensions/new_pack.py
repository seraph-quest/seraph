#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

if sys.version_info < (3, 10):
    raise SystemExit("Use Python 3.10+ or backend/.venv/bin/python to run this script.")

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from src.extensions.scaffold import scaffold_extension_package


def main() -> int:
    parser = argparse.ArgumentParser(description="Scaffold a new Seraph extension package.")
    parser.add_argument("package_root", help="Directory for the new package")
    parser.add_argument("--id", required=True, dest="extension_id", help="Extension id, e.g. seraph.example-pack")
    parser.add_argument("--name", required=True, dest="display_name", help="Display name for the package")
    parser.add_argument(
        "--trust",
        default="local",
        choices=["bundled", "local", "verified"],
        help="Package provenance to write into the manifest",
    )
    parser.add_argument(
        "--with",
        dest="contributions",
        action="append",
        default=[],
        help="Contribution type to scaffold; repeat to include multiple surfaces",
    )
    args = parser.parse_args()

    package = scaffold_extension_package(
        args.package_root,
        extension_id=args.extension_id,
        display_name=args.display_name,
        trust=args.trust,
        contributions=args.contributions or None,
    )

    print(f"Created {package.manifest_path}")
    for path in package.created_files:
        if path != package.manifest_path:
            print(f"  - {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
