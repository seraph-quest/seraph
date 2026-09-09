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
    read_lifecycle_receipt,
    reconcile_production_restore,
    resolve_production_workspace,
    rollback_workspace,
    restore_workspace,
    write_lifecycle_receipt,
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
    status = subparsers.add_parser("status", help="show the durable last lifecycle result")
    status.set_defaults(command="status")
    rollback = subparsers.add_parser("rollback", help="rollback a promoted restore")
    rollback.add_argument("--restore-id", required=True)
    rollback.add_argument(
        "--confirm",
        action="store_true",
        help="explicitly authorize rollback of the named restore",
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


def _validate_restore_archive(workspace, archive: Path) -> Path:
    candidate = archive.expanduser()
    raw = str(candidate)
    if not raw or "\x00" in raw or "$" in raw:
        raise ProductionWorkspaceError("restore archive path contains unresolved syntax")
    backup_root = workspace.backup_root.resolve(strict=False)
    if not candidate.is_absolute():
        candidate = workspace.backup_root / candidate
        try:
            candidate.resolve(strict=False).relative_to(backup_root)
        except (OSError, ValueError) as exc:
            raise ProductionWorkspaceError(
                "relative restore archives must remain under the derived backup sibling"
            ) from exc
    else:
        resolved = candidate.resolve(strict=False)
        for forbidden in (workspace.host_root.resolve(strict=False), workspace.restore_staging_root.resolve(strict=False)):
            try:
                resolved.relative_to(forbidden)
            except ValueError:
                continue
            raise ProductionWorkspaceError("restore archive cannot be inside an active workspace sidecar")
    current = Path(candidate.anchor)
    for component in candidate.parts[1:-1]:
        current /= component
        try:
            if current.is_symlink():
                raise ProductionWorkspaceError("restore archive path contains a symlink component")
        except OSError as exc:
            raise ProductionWorkspaceError("restore archive path is unreadable") from exc
    return candidate


def _validate_backup_destination(workspace, archive: Path | None) -> Path | None:
    if archive is None:
        return None
    candidate = archive.expanduser()
    if "\x00" in str(candidate) or "$" in str(candidate):
        raise ProductionWorkspaceError("backup archive path contains unresolved syntax")
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


def _status_receipt(workspace) -> dict[str, Any]:
    last = read_lifecycle_receipt(workspace)
    active_root_present = workspace.host_root.is_dir() and not workspace.host_root.is_symlink()
    return {
        "schema_version": "seraph.production-workspace-lifecycle.v1",
        "status": "ready" if last is not None and active_root_present else "blocked" if not active_root_present else "unknown",
        "operator_status": (
            "production_workspace_lifecycle_active_root_missing"
            if not active_root_present
            else "production_workspace_lifecycle_last_result"
            if last is not None
            else "production_workspace_lifecycle_no_result"
        ),
        "workspace_ownership": workspace.receipt(),
        "active_root_present": active_root_present,
        "last_result": last,
        "rollback_available": bool(last and last.get("rollback_available")),
        "secret_values_included": False,
    }


def _persist_result(workspace, operation: str, receipt: dict[str, Any]) -> None:
    durable = dict(receipt)
    durable["operation"] = operation
    durable["workspace_ownership"] = workspace.receipt()
    durable["secret_values_included"] = False
    write_lifecycle_receipt(workspace, durable)


def _run(args: argparse.Namespace) -> dict[str, Any]:
    workspace = resolve_production_workspace(
        os.environ,
        base_dir=args.base_dir,
        allow_missing=args.command == "status",
    )
    try:
        if args.command == "status":
            return _status_receipt(workspace)
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
                archive = _validate_restore_archive(workspace, args.archive)
                receipt = restore_workspace(
                    workspace.host_root,
                    archive,
                    registry=registry,
                    confirm=bool(args.confirm),
                    reconcile_restore=reconcile_production_restore,
                )
            elif args.command == "rollback":
                if not args.confirm:
                    raise WorkspaceLifecycleError("rollback requires explicit confirm=True")
                receipt = rollback_workspace(
                    workspace.host_root,
                    args.restore_id,
                )
            else:  # pragma: no cover - argparse enforces the command set.
                raise WorkspaceLifecycleError("unsupported workspace lifecycle command")
        receipt["workspace_ownership"] = workspace.receipt()
        receipt["secret_values_included"] = False
        _persist_result(workspace, args.command, receipt)
        return receipt
    except Exception as exc:
        blocked = _blocked_receipt(exc)
        blocked["operation"] = args.command
        try:
            _persist_result(workspace, args.command, blocked)
        except Exception:
            pass
        raise


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
