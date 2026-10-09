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
    ProductionWorkspace,
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
    reconcile_production_rollback,
    resolve_production_workspace,
    rollback_workspace,
    restore_workspace,
    write_lifecycle_receipt,
)
from src.workspace.production import prepare_lifecycle_directory


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
    identity = subparsers.add_parser(
        "identity",
        help="print the redacted host bind identity for container preflight",
    )
    identity.set_defaults(command="identity")
    rollback = subparsers.add_parser("rollback", help="rollback a promoted restore")
    rollback.add_argument("--restore-id", required=True)
    rollback.add_argument(
        "--confirm",
        action="store_true",
        help="explicitly authorize rollback of the named restore",
    )
    accounting = subparsers.add_parser("accounting-reconcile", help="repair one retained accounting commit checkpoint under maintenance")
    accounting.add_argument("--confirm", action="store_true")
    accounting.add_argument("--period", help="acknowledge only the exact observed current UTC month")
    accounting.add_argument("--expected-revision", type=int)
    accounting.add_argument("--policy", action="store_true", help="repair interrupted policy publication as revoked")
    rebind = subparsers.add_parser("accounting-rebind", help="retain deployment accounting before adopting a different canonical root")
    rebind.add_argument("--from-root", type=Path, required=True)
    rebind.add_argument("--confirm", action="store_true")
    programme = subparsers.add_parser("programme-envelope-transition",
        help="explicit stopped empty-programme continuity envelope upgrade")
    programme.add_argument("--confirm", action="store_true")
    programme.add_argument("--recover-pending", action="store_true",
        help="repair only the exact interrupted empty-programme transition")
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
    last_status = str(last.get("status") or "").strip().lower() if isinstance(last, dict) else ""
    current_bind_identity = None
    if active_root_present:
        try:
            current_bind_identity = workspace.bind_identity_digest
        except ProductionWorkspaceError:
            current_bind_identity = None
    recorded_bind_identity = (
        last.get("workspace_ownership", {}).get("host_bind_identity_digest")
        if isinstance(last, dict) and isinstance(last.get("workspace_ownership"), dict)
        else None
    )
    root_identity_match = (
        recorded_bind_identity == current_bind_identity
        if recorded_bind_identity is not None and current_bind_identity is not None
        else None
    )
    root_changed = (
        active_root_present
        and last is not None
        and last_status in {"created", "restored", "rolled_back", "ready"}
        and root_identity_match is False
    )
    if not active_root_present or last_status == "blocked" or root_changed:
        status = "blocked"
    elif last_status in {"created", "restored", "rolled_back", "ready"}:
        status = "ready"
    elif last is None:
        status = "unknown"
    else:
        status = "unknown"
    return {
        "schema_version": "seraph.production-workspace-lifecycle.v1",
        "status": status,
        "operator_status": (
            "production_workspace_lifecycle_active_root_missing"
            if not active_root_present
            else "production_workspace_root_changed"
            if root_changed
            else "production_workspace_lifecycle_blocked"
            if status == "blocked"
            else "production_workspace_lifecycle_last_result"
            if last is not None
            else "production_workspace_lifecycle_no_result"
        ),
        "workspace_ownership": workspace.receipt(),
        "active_root_present": active_root_present,
        "root_identity_match": root_identity_match,
        "reason_code": "production_workspace_root_changed" if root_changed else None,
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
    os.environ["SERAPH_WORKSPACE_LIFECYCLE_PATH"] = str(workspace.lifecycle_directory)
    try:
        if args.command == "status":
            return _status_receipt(workspace)
        if args.command == "identity":
            try:
                lifecycle_directory = prepare_lifecycle_directory(workspace)
                from src.workspace.accounting_witness import assert_deployment_binding
                assert_deployment_binding(workspace)
                accounting_status = "ready"
            except ProductionWorkspaceError:
                lifecycle_directory, accounting_status = workspace.lifecycle_directory, "blocked"
            return {
                "schema_version": "seraph.production-workspace-bind-identity.v1",
                "status": "ready",
                "bind_identity": workspace.bind_identity_digest,
                "lifecycle_directory": str(lifecycle_directory),
                "accounting_continuity_status": accounting_status,
                "identity_basis": "resolved_configured_path_plus_device_inode",
                "secret_values_included": False,
            }
        registry = canonical_workspace_registry(workspace.host_root)
        prepare_lifecycle_directory(workspace)
        with maintenance_fence(workspace):
            if args.command == "programme-envelope-transition":
                if not args.confirm:
                    raise WorkspaceLifecycleError("programme envelope transition requires explicit confirm=True")
                from src.memory.header_bounds import HeaderReadBudget
                from src.workspace.accounting_continuity import (transition_programme_envelope,
                    reconcile_programme_envelope_transition)
                operation = reconcile_programme_envelope_transition if args.recover_pending else transition_programme_envelope
                # The original stopped owner publishes its exact final receipt
                # while this fence, accounting lock and actual handle are held.
                return operation(workspace=workspace, budget=HeaderReadBudget())
            elif args.command == "backup":
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
                    registry=registry,
                    reconcile_rollback=reconcile_production_rollback,
                )
            elif args.command == "accounting-reconcile":
                if not args.confirm:
                    raise WorkspaceLifecycleError("accounting reconciliation requires explicit confirm=True")
                if args.period:
                    from src.workspace.accounting_continuity import acknowledge_accounting_period
                    receipt = acknowledge_accounting_period(root=workspace.host_root, period=args.period,
                        expected_revision=args.expected_revision, actor="managed_maintenance")
                elif args.policy:
                    from src.workspace.accounting_witness import reconcile_policy_checkpoint
                    receipt = reconcile_policy_checkpoint(workspace.host_root)
                else:
                    from src.workspace.accounting_continuity import reconcile_accounting_checkpoint
                    receipt = reconcile_accounting_checkpoint(root=workspace.host_root, registry=registry)
            elif args.command == "accounting-rebind":
                if not args.confirm:
                    raise WorkspaceLifecycleError("accounting rebind requires explicit confirm=True")
                from src.workspace.accounting_continuity import rebind_accounting_root
                source = ProductionWorkspace(host_root=args.from_root.resolve())
                with maintenance_fence(source):
                    receipt = rebind_accounting_root(active=source, target=workspace)
            else:  # pragma: no cover - argparse enforces the command set.
                raise WorkspaceLifecycleError("unsupported workspace lifecycle command")
        receipt["workspace_ownership"] = workspace.receipt()
        if args.command in {"restore", "rollback"}:
            # Atomic promotion/rollback changes the active directory inode.
            # Return the post-operation digest so direct Compose operators can
            # refresh their env file; managed ``up`` derives it automatically.
            receipt["bind_identity_refresh"] = {
                "status": "ready",
                "bind_identity": workspace.bind_identity_digest,
                "next_managed_start": "auto_refreshes",
            }
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
