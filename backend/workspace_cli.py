"""Managed production backup/restore commands.

This entry point is intentionally dependency-free.  ``manage.sh`` invokes it
on the host so a backup or staged restore can be performed without starting a
provider, GPU, or backend process.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Sequence

from src.workspace import (
    InvalidWorkspaceArchiveError,
    MissingSecretMaterialError,
    ProductionWorkspaceError,
    WorkspaceLifecycleError,
    WorkspaceStateError,
    backup_workspace,
    canonical_workspace_registry,
    maintenance_fence,
    resolve_production_workspace,
    restore_workspace,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help="base directory for relative BACKEND_DATA_PATH_PROD values",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("backup", help="create a verified workspace archive")
    backup.add_argument(
        "--archive",
        type=Path,
        help="optional archive name/path; relative paths stay under the derived backup sibling",
    )

    restore = subparsers.add_parser("restore", help="stage and promote a workspace archive")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument(
        "--confirm",
        action="store_true",
        help="explicitly authorize promotion after staging and verification",
    )
    return parser


def _blocked_receipt(exc: BaseException) -> dict[str, object]:
    if isinstance(exc, ProductionWorkspaceError):
        reason_code = exc.reason_code
    elif isinstance(exc, MissingSecretMaterialError):
        reason_code = "required_secret_missing"
    elif isinstance(exc, InvalidWorkspaceArchiveError):
        reason_code = "invalid_workspace_archive"
    elif isinstance(exc, WorkspaceStateError):
        reason_code = "workspace_state_invalid"
    elif isinstance(exc, OSError):
        reason_code = "workspace_io_failed"
    else:
        reason_code = "workspace_lifecycle_failed"
    return {
        "schema_version": "seraph.production-workspace-lifecycle.v1",
        "status": "blocked",
        "operator_status": "production_workspace_lifecycle_blocked",
        "reason_code": reason_code,
        "secret_values_included": False,
    }


def _validate_backup_destination(workspace, archive: Path | None) -> Path | None:
    if archive is None:
        return None
    candidate = archive.expanduser()
    if not candidate.is_absolute():
        candidate = workspace.backup_root / candidate
    try:
        resolved = candidate.resolve(strict=False)
        backup_root = workspace.backup_root.resolve(strict=False)
        resolved.relative_to(backup_root)
    except (OSError, ValueError) as exc:
        raise ProductionWorkspaceError(
            "managed backup archives must remain under the derived backup sibling"
        ) from exc
    if candidate.exists() and candidate.is_dir():
        raise ProductionWorkspaceError("backup archive destination must be a file")
    return candidate


def _run(args: argparse.Namespace) -> dict[str, Any]:
    workspace = resolve_production_workspace(os.environ, base_dir=args.base_dir)
    registry = canonical_workspace_registry(workspace.host_root)
    with maintenance_fence(workspace):
        if args.command == "backup":
            destination = _validate_backup_destination(workspace, args.archive)
            receipt = backup_workspace(
                workspace.host_root,
                registry=registry,
                archive_path=destination,
            )
        elif args.command == "restore":
            receipt = restore_workspace(
                workspace.host_root,
                args.archive.expanduser(),
                registry=registry,
                confirm=bool(args.confirm),
            )
        else:  # pragma: no cover - argparse enforces the command set.
            raise WorkspaceLifecycleError("unsupported workspace lifecycle command")
    receipt["workspace_ownership"] = workspace.receipt()
    receipt["secret_values_included"] = False
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = _run(args)
    except Exception as exc:
        print(json.dumps(_blocked_receipt(exc), sort_keys=True))
        return 78
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
