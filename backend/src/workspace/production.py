"""Production workspace ownership and maintenance fencing.

The production deployment has one host bind and one container mount.  The
host-side lifecycle commands resolve ``BACKEND_DATA_PATH_PROD`` and the
container preflight requires the corresponding ``WORKSPACE_DIR=/app/data``
mount.  Backup and restore sidecars are derived from the resolved host bind;
they are never accepted as active workspace roots.

This module intentionally has no application, provider, or database-engine
dependencies.  It is safe to use from the managed shell before the backend is
started and from deterministic tests.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import stat
from typing import Iterator, Mapping

from src.workspace.lifecycle import workspace_backup_dir, workspace_restore_staging_dir
from src.workspace.state_registry import WorkspaceStateError, canonical_workspace_root


CANONICAL_CONTAINER_WORKSPACE = "/app/data"
PRODUCTION_BIND_ENV = "BACKEND_DATA_PATH_PROD"
WORKSPACE_ENV = "WORKSPACE_DIR"
MAINTENANCE_LOCK_NAME = ".seraph-workspace-maintenance.lock"


class ProductionWorkspaceError(WorkspaceStateError):
    """Base error for an unsafe or ambiguous production workspace."""

    reason_code = "production_workspace_invalid"


class ProductionWorkspaceConfigurationError(ProductionWorkspaceError):
    """Raised when the canonical production bind is not configured."""

    reason_code = "production_workspace_configuration_required"


class ProductionWorkspaceMountError(ProductionWorkspaceError):
    """Raised when the container mount is not the canonical mount."""

    reason_code = "production_workspace_mount_invalid"


class DuplicateWorkspaceOwnerError(ProductionWorkspaceError):
    """Raised when another maintenance owner already holds the fence."""

    reason_code = "production_workspace_owner_busy"


def _env_value(env: Mapping[str, str], name: str) -> str:
    value = str(env.get(name, "") or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    return value


def _assert_no_symlink_components(path: Path, *, label: str) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError as exc:
            raise ProductionWorkspaceError(f"{label} is not present") from exc
        except OSError as exc:
            raise ProductionWorkspaceError(f"{label} is not readable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ProductionWorkspaceError(f"{label} must not contain symlink components")


def _absolute_path(raw: str, *, base_dir: Path, label: str) -> Path:
    if not raw:
        raise ProductionWorkspaceConfigurationError(f"{label} is required")
    if "\x00" in raw or "$" in raw:
        raise ProductionWorkspaceConfigurationError(f"{label} contains unresolved path syntax")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    try:
        return candidate.absolute()
    except OSError as exc:
        raise ProductionWorkspaceConfigurationError(f"{label} cannot be resolved") from exc


def _existing_directory(path: Path, *, label: str) -> Path:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ProductionWorkspaceConfigurationError(f"{label} is missing") from exc
    except OSError as exc:
        raise ProductionWorkspaceConfigurationError(f"{label} is not readable") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ProductionWorkspaceConfigurationError(f"{label} must not be a symlink")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ProductionWorkspaceConfigurationError(f"{label} must be a directory")
    _assert_no_symlink_components(path, label=label)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ProductionWorkspaceConfigurationError(f"{label} cannot be resolved") from exc
    if resolved != path:
        # Parent symlinks are rejected above.  Keep this comparison as a
        # defense if pathlib resolution behavior changes on another host.
        raise ProductionWorkspaceConfigurationError(f"{label} resolves ambiguously")
    if not os.access(resolved, os.R_OK | os.W_OK | os.X_OK):
        raise ProductionWorkspaceConfigurationError(f"{label} is not readable and writable")
    return resolved


@dataclass(frozen=True)
class ProductionWorkspace:
    """Resolved production identity and derived maintenance locations."""

    host_root: Path
    container_root: Path = Path(CANONICAL_CONTAINER_WORKSPACE)

    @property
    def backup_root(self) -> Path:
        return workspace_backup_dir(self.host_root)

    @property
    def restore_staging_root(self) -> Path:
        return workspace_restore_staging_dir(self.host_root)

    @property
    def maintenance_lock_path(self) -> Path:
        return self.host_root.parent / f".{self.host_root.name}{MAINTENANCE_LOCK_NAME}"

    @property
    def identity_digest(self) -> str:
        return hashlib.sha256(str(self.host_root).encode("utf-8")).hexdigest()[:24]

    def receipt(self) -> dict[str, object]:
        """Return operator-safe ownership metadata without exposing host paths."""
        return {
            "schema_version": "seraph.production-workspace.v1",
            "status": "ready",
            "workspace_id": "workspace-runtime",
            "root_kind": "production",
            "canonical_container_mount": str(self.container_root),
            "host_root_digest": self.identity_digest,
            "backup_location": "derived-sibling",
            "restore_staging_location": "derived-sibling",
            "active_root_is_host_bind": True,
            "sidecars_are_active_roots": False,
            "secret_values_included": False,
        }


def resolve_production_workspace(
    env: Mapping[str, str] | None = None,
    *,
    base_dir: str | os.PathLike[str] | None = None,
) -> ProductionWorkspace:
    """Resolve the one host bind allowed to own production state.

    ``WORKSPACE_DIR`` may repeat the host path in a host ``.env.prod`` file,
    or be the container target ``/app/data`` when passed through Compose.  A
    different host path is rejected as an ambiguous owner.  The resolver
    requires an existing, writable, non-symlink directory because backup and
    restore must never guess which root should be active.
    """
    values = env if env is not None else os.environ
    raw_bind = _env_value(values, PRODUCTION_BIND_ENV)
    root_base = Path(base_dir or Path.cwd()).expanduser().resolve()
    lexical_bind = _absolute_path(raw_bind, base_dir=root_base, label=PRODUCTION_BIND_ENV)
    host_root = _existing_directory(lexical_bind, label=PRODUCTION_BIND_ENV)

    raw_workspace = _env_value(values, WORKSPACE_ENV)
    if raw_workspace and raw_workspace != CANONICAL_CONTAINER_WORKSPACE:
        lexical_workspace = _absolute_path(raw_workspace, base_dir=root_base, label=WORKSPACE_ENV)
        workspace_root = _existing_directory(lexical_workspace, label=WORKSPACE_ENV)
        if workspace_root != host_root:
            raise ProductionWorkspaceError(
                "BACKEND_DATA_PATH_PROD and WORKSPACE_DIR identify different roots"
            )

    return ProductionWorkspace(host_root=host_root)


def validate_container_workspace_mount(
    env: Mapping[str, str] | None = None,
    *,
    mounted_root: str | os.PathLike[str] | None = None,
) -> dict[str, object]:
    """Require the backend's effective production workspace to be ``/app/data``.

    ``mounted_root`` is injectable for deterministic tests; production calls
    use the actual container path and therefore verify the real mount.
    """
    values = env if env is not None else os.environ
    configured = _env_value(values, WORKSPACE_ENV)
    if configured != CANONICAL_CONTAINER_WORKSPACE:
        raise ProductionWorkspaceMountError(
            "production WORKSPACE_DIR must be exactly /app/data"
        )
    path = Path(mounted_root or CANONICAL_CONTAINER_WORKSPACE)
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ProductionWorkspaceMountError("production /app/data mount is missing") from exc
    except OSError as exc:
        raise ProductionWorkspaceMountError("production /app/data mount is unreadable") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ProductionWorkspaceMountError("production /app/data mount must not be a symlink")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ProductionWorkspaceMountError("production /app/data mount must be a directory")
    _assert_no_symlink_components(path, label="production /app/data mount")
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        raise ProductionWorkspaceMountError("production /app/data mount is not writable")
    return {
        "schema_version": "seraph.production-workspace-mount.v1",
        "status": "ready",
        "workspace_dir": CANONICAL_CONTAINER_WORKSPACE,
        "canonical_mount": True,
        "secret_values_included": False,
    }


@contextmanager
def maintenance_fence(workspace: ProductionWorkspace) -> Iterator[None]:
    """Acquire an exclusive process-wide maintenance fence.

    The lock is a derived sibling and contains no owner or secret data.  A
    second backup/restore command fails closed instead of attempting a mixed
    generation.  Application writers must still adopt the broader maintenance
    protocol tracked by the remaining #742 work.
    """
    lock_path = workspace.maintenance_lock_path
    try:
        if lock_path.is_symlink():
            raise DuplicateWorkspaceOwnerError("maintenance lock path is a symlink")
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except DuplicateWorkspaceOwnerError:
        raise
    except OSError as exc:
        raise ProductionWorkspaceError("production maintenance lock is unavailable") from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            raise DuplicateWorkspaceOwnerError(
                "another production workspace maintenance owner is active"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


__all__ = [
    "CANONICAL_CONTAINER_WORKSPACE",
    "PRODUCTION_BIND_ENV",
    "WORKSPACE_ENV",
    "DuplicateWorkspaceOwnerError",
    "ProductionWorkspace",
    "ProductionWorkspaceConfigurationError",
    "ProductionWorkspaceError",
    "ProductionWorkspaceMountError",
    "maintenance_fence",
    "resolve_production_workspace",
    "validate_container_workspace_mount",
]
