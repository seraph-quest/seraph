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
import hmac
import json
import os
from pathlib import Path
import stat
from typing import Any, Iterator, Mapping

from src.workspace.lifecycle import (
    _require_production_fence,
    lifecycle_fence_marker,
    workspace_backup_dir,
    workspace_restore_staging_dir,
)
from src.workspace.state_registry import (
    WORK_BOARD_INPUT_ARTIFACT_TABLE,
    WORK_BOARD_ROUTINE_BINDING_TABLE,
    WorkspaceStateError,
    canonical_workspace_root,
    canonical_workspace_root_identity,
    work_board_input_artifact_contract,
    work_board_routine_binding_contract,
)


CANONICAL_CONTAINER_WORKSPACE = "/app/data"
PRODUCTION_BIND_ENV = "BACKEND_DATA_PATH_PROD"
WORKSPACE_ENV = "WORKSPACE_DIR"
MOUNT_SOURCE_ENV = "SERAPH_PRODUCTION_MOUNT_SOURCE"
BIND_IDENTITY_ENV = "SERAPH_PRODUCTION_BIND_IDENTITY"
LIFECYCLE_PATH_ENV = "SERAPH_WORKSPACE_LIFECYCLE_PATH"
MAINTENANCE_LOCK_NAME = ".seraph-workspace-maintenance.lock"
MOUNTINFO_PATH = "/proc/self/mountinfo"
MAX_LIFECYCLE_RECEIPT_BYTES = 64 * 1024
MAX_ACCOUNTING_CHECKPOINT_BYTES = 1024 * 1024


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


class ProductionWorkspaceRootChangedError(ProductionWorkspaceError):
    """Raised when the configured root changes while acquiring the owner lock."""

    reason_code = "production_workspace_root_changed"


class ProductionWorkspaceReconciliationError(ProductionWorkspaceError):
    """Raised when restore cannot make derived and authority state safe."""

    reason_code = "production_workspace_reconciliation_required"


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


def _bind_identity_digest(configured_path: Path, observed_root: Path) -> str:
    """Bind a configured host path to the inode visible at the mountpoint.

    The path component prevents a second directory on the same filesystem from
    satisfying the receipt. The device/inode component is stable across a
    Linux bind mount, allowing the container to compare its effective
    ``/app/data`` directory with the host-generated identity without exposing a
    host path or trusting the mount source label alone.
    """
    try:
        metadata = os.stat(observed_root, follow_symlinks=False)
    except OSError as exc:
        raise ProductionWorkspaceMountError("production bind identity stat is unavailable") from exc
    try:
        normalized = str(configured_path.expanduser().resolve(strict=False))
    except OSError as exc:
        raise ProductionWorkspaceMountError("production bind path identity is unresolved") from exc
    material = f"{normalized}\x00{metadata.st_dev}:{metadata.st_ino}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class ProductionWorkspace:
    """Resolved production identity and derived maintenance locations."""

    host_root: Path
    container_root: Path = Path(CANONICAL_CONTAINER_WORKSPACE)
    lifecycle_path: Path | None = None

    @property
    def backup_root(self) -> Path:
        return workspace_backup_dir(self.host_root)

    @property
    def restore_staging_root(self) -> Path:
        return workspace_restore_staging_dir(self.host_root)

    @property
    def lifecycle_directory(self) -> Path:
        # This directory is intentionally outside every restorable root.
        # Docker binds the directory, so receipt replacement remains atomic.
        if self.host_root == Path(CANONICAL_CONTAINER_WORKSPACE):
            return Path("/app/workspace-lifecycle")
        if self.lifecycle_path is not None:
            return self.lifecycle_path
        if os.environ.get(LIFECYCLE_PATH_ENV):
            return Path(os.environ[LIFECYCLE_PATH_ENV])
        # Legacy lifecycle inspection only. Billable runtime explicitly
        # requires the managed descriptor and never initializes this fallback.
        return self.host_root.with_name(self.host_root.name + ".lifecycle")

    @property
    def maintenance_lock_path(self) -> Path:
        # Keep one lock inode inside the bind so the host maintenance command
        # and the container backend coordinate through the same mount.  The
        # registry classifies this file as derived and never archives it.
        return self.host_root / MAINTENANCE_LOCK_NAME

    @property
    def identity_digest(self) -> str:
        return hashlib.sha256(str(self.host_root).encode("utf-8")).hexdigest()[:24]

    @property
    def bind_identity_digest(self) -> str:
        """Return the host path plus device/inode identity used by preflight."""
        return _bind_identity_digest(self.host_root, self.host_root)

    def receipt(self) -> dict[str, object]:
        """Return operator-safe ownership metadata without exposing host paths."""
        try:
            bind_identity = self.bind_identity_digest
            bind_identity_status = "configured_path_and_stat_digest"
        except ProductionWorkspaceMountError:
            # ``status`` remains useful after an interrupted rename or missing
            # root, but must not claim that an unavailable inode was verified.
            bind_identity = None
            bind_identity_status = "configured_path_digest_only_unverified"
        return {
            "schema_version": "seraph.production-workspace.v1",
            "status": "ready" if bind_identity is not None else "blocked",
            "workspace_id": "workspace-runtime",
            "root_kind": "production",
            "canonical_container_mount": str(self.container_root),
            "host_root_digest": self.identity_digest,
            "backup_location": "derived-sibling",
            "restore_staging_location": "derived-sibling",
            # Resolving the host path proves which directory the maintenance
            # command owns; it does not prove what a container mounted at
            # /app/data.  The preflight supplies that separate mount receipt.
            "active_root_is_host_bind": False,
            "host_bind_identity": bind_identity_status,
            "host_bind_identity_digest": bind_identity,
            "operator_status": (
                "production_workspace_bind_identity_verified"
                if bind_identity is not None
                else "production_workspace_bind_identity_unavailable"
            ),
            "sidecars_are_active_roots": False,
            "secret_values_included": False,
            # This ownership receipt does not open SQLite.  Inventory is the
            # authoritative place where presence is verified.
            "database_operator_contracts": {
                "work_board_routine_binding": work_board_routine_binding_contract(
                    present=None
                ),
                "work_board_input_artifact": work_board_input_artifact_contract(
                    present=None
                ),
                "inventory_source": "workspace_state_registry",
                "table_name": WORK_BOARD_ROUTINE_BINDING_TABLE,
                "input_artifact_table_name": WORK_BOARD_INPUT_ARTIFACT_TABLE,
            },
        }


def resolve_production_workspace(
    env: Mapping[str, str] | None = None,
    *,
    base_dir: str | os.PathLike[str] | None = None,
    allow_missing: bool = False,
) -> ProductionWorkspace:
    """Resolve the one host bind allowed to own production state.

    ``WORKSPACE_DIR`` may repeat the host path in a host ``.env.prod`` file,
    or be the container target ``/app/data`` when passed through Compose.  A
    different host path is rejected as an ambiguous owner.  The resolver
    requires an existing, writable, non-symlink directory because backup and
    restore must never guess which root should be active.  ``allow_missing``
    is reserved for the read-only status command after an interrupted rename;
    it never authorizes backup, restore, or rollback.
    """
    values = env if env is not None else os.environ
    raw_bind = _env_value(values, PRODUCTION_BIND_ENV)
    root_base = Path(base_dir or Path.cwd()).expanduser().resolve()
    lexical_bind = _absolute_path(raw_bind, base_dir=root_base, label=PRODUCTION_BIND_ENV)
    try:
        host_root = _existing_directory(lexical_bind, label=PRODUCTION_BIND_ENV)
    except ProductionWorkspaceConfigurationError:
        if not allow_missing or lexical_bind.exists() or lexical_bind.is_symlink():
            raise
        host_root = lexical_bind

    raw_workspace = _env_value(values, WORKSPACE_ENV)
    if raw_workspace and raw_workspace != CANONICAL_CONTAINER_WORKSPACE:
        lexical_workspace = _absolute_path(raw_workspace, base_dir=root_base, label=WORKSPACE_ENV)
        if allow_missing and not lexical_workspace.exists():
            return ProductionWorkspace(host_root=host_root, lifecycle_path=Path(_env_value(values, LIFECYCLE_PATH_ENV) or root_base / "docker-data/prod/workspace-lifecycle"))
        workspace_root = _existing_directory(lexical_workspace, label=WORKSPACE_ENV)
        if workspace_root != host_root:
            raise ProductionWorkspaceError(
                "BACKEND_DATA_PATH_PROD and WORKSPACE_DIR identify different roots"
            )

    return ProductionWorkspace(host_root=host_root, lifecycle_path=Path(_env_value(values, LIFECYCLE_PATH_ENV) or root_base / "docker-data/prod/workspace-lifecycle"))


def validate_container_workspace_mount(
    env: Mapping[str, str] | None = None,
    *,
    mounted_root: str | os.PathLike[str] | None = None,
    mountinfo: str | os.PathLike[str] | None = None,
) -> dict[str, object]:
    """Require the backend's effective production workspace to be ``/app/data``.

    ``mounted_root`` and ``mountinfo`` are injectable for deterministic tests;
    production calls use the actual container path and ``/proc/self/mountinfo``
    so an image directory cannot be mistaken for the host bind mount.
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
    raw_bind = _env_value(values, PRODUCTION_BIND_ENV)
    if not raw_bind:
        raise ProductionWorkspaceMountError(
            "production configured bind path identity is required"
        )
    if "\x00" in raw_bind or "$" in raw_bind:
        raise ProductionWorkspaceMountError(
            "production configured bind path contains unresolved syntax"
        )
    configured_bind = Path(raw_bind).expanduser()
    if not configured_bind.is_absolute():
        raise ProductionWorkspaceMountError(
            "production configured bind path must be absolute"
        )
    expected_bind_identity = _env_value(values, BIND_IDENTITY_ENV)
    if not expected_bind_identity:
        raise ProductionWorkspaceMountError(
            "production configured bind identity is required"
        )
    observed_bind_identity = _bind_identity_digest(configured_bind, path)
    if not hmac.compare_digest(observed_bind_identity, expected_bind_identity):
        raise ProductionWorkspaceMountError(
            "production configured bind identity does not match /app/data"
        )
    expected_source = _env_value(values, MOUNT_SOURCE_ENV)
    mount_receipt = _mountinfo_receipt(mountinfo, expected_source=expected_source)
    mount_receipt["bind_identity_digest"] = observed_bind_identity
    mount_receipt["bind_identity_verified"] = True
    return {
        "schema_version": "seraph.production-workspace-mount.v1",
        "status": "ready",
        "workspace_dir": CANONICAL_CONTAINER_WORKSPACE,
        "canonical_mount": True,
        "bind_mount_verified": True,
        "mount_evidence": mount_receipt,
        "secret_values_included": False,
    }


def _decode_mountinfo_path(value: str) -> str:
    """Decode the octal escapes used for paths in Linux mountinfo."""
    decoded: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 3 < len(value):
            token = value[index + 1 : index + 4]
            if all(character in "01234567" for character in token):
                decoded.append(chr(int(token, 8)))
                index += 4
                continue
        decoded.append(value[index])
        index += 1
    return "".join(decoded)


def _mountinfo_receipt(
    mountinfo: str | os.PathLike[str] | None,
    *,
    expected_source: str,
) -> dict[str, object]:
    if not expected_source:
        raise ProductionWorkspaceMountError(
            "production configured mount source identity is required"
        )
    source = Path(mountinfo) if mountinfo is not None else Path(MOUNTINFO_PATH)
    try:
        raw = source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProductionWorkspaceMountError("production mount evidence is unavailable") from exc
    for line in raw.splitlines():
        pre, separator, post = line.partition(" - ")
        fields = pre.split()
        post_fields = post.split()
        if separator and len(fields) >= 6 and len(post_fields) >= 2:
            if _decode_mountinfo_path(fields[4]) != CANONICAL_CONTAINER_WORKSPACE:
                continue
            filesystem = post_fields[0]
            mount_source = post_fields[1]
            # An overlay root or ordinary image directory is not evidence of
            # the configured host bind.  Docker bind mounts expose a distinct
            # mountpoint in mountinfo; preserve only non-sensitive evidence.
            if filesystem == "overlay" or mount_source == "overlay":
                continue
            if _decode_mountinfo_path(mount_source) != expected_source:
                continue
            return {
                "source": "proc_mountinfo",
                "mountpoint": CANONICAL_CONTAINER_WORKSPACE,
                "filesystem": filesystem,
                "mount_source_present": bool(mount_source),
                "mount_source_identity_verified": True,
                "mount_source_digest": hashlib.sha256(mount_source.encode("utf-8")).hexdigest()[:24],
            }
    raise ProductionWorkspaceMountError(
        "production /app/data has no dedicated bind mount evidence"
    )


def _open_fenced_lock(workspace: ProductionWorkspace, *, exclusive: bool) -> int:
    try:
        initial_identity = canonical_workspace_root_identity(workspace.host_root)
    except WorkspaceStateError as exc:
        raise ProductionWorkspaceRootChangedError(
            "production workspace root identity is unavailable"
        ) from exc
    lock_path = workspace.maintenance_lock_path
    try:
        _assert_no_symlink_components(workspace.host_root, label="production workspace root")
        if lock_path.is_symlink():
            raise DuplicateWorkspaceOwnerError("workspace owner lock path is a symlink")
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except DuplicateWorkspaceOwnerError:
        raise
    except OSError as exc:
        raise ProductionWorkspaceError("production workspace owner lock is unavailable") from exc
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        os.close(descriptor)
        raise DuplicateWorkspaceOwnerError(
            "another production workspace owner is active"
        ) from exc
    try:
        current_identity = canonical_workspace_root_identity(workspace.host_root)
    except WorkspaceStateError as exc:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
        raise ProductionWorkspaceRootChangedError(
            "production workspace root identity changed while acquiring owner lock"
        ) from exc
    if (
        current_identity["device"] != initial_identity["device"]
        or current_identity["inode"] != initial_identity["inode"]
    ):
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
        raise ProductionWorkspaceRootChangedError(
            "production workspace root identity changed while acquiring owner lock"
        )
    return descriptor


@contextmanager
def runtime_workspace_owner(root: str | os.PathLike[str]) -> Iterator[None]:
    """Hold the exclusive workspace owner lease for the backend lifetime.

    The lease uses the same lock inode as backup/restore.  A second backend,
    scheduler owner, or maintenance command therefore fails closed instead of
    reading or replacing a live generation.
    """
    resolved = canonical_workspace_root(root)
    workspace = ProductionWorkspace(host_root=resolved)
    descriptor = _open_fenced_lock(workspace, exclusive=True)
    try:
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def lifecycle_receipt_path(workspace: ProductionWorkspace) -> Path:
    """Return the durable redacted receipt path beside the canonical root."""
    return workspace.lifecycle_directory / "receipt.json"


def prepare_lifecycle_directory(workspace: ProductionWorkspace) -> Path:
    """Migrate the exact legacy receipt under the managed ownership fence.

    No archive contains this directory. An existing legacy receipt is retained
    if a crash interrupts migration, and differing generations fail closed.
    """
    with maintenance_fence(workspace):
        directory = workspace.lifecycle_directory
        if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
            raise ProductionWorkspaceError("workspace lifecycle directory is unsafe")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        old_directory = workspace.host_root.with_name(workspace.host_root.name + ".lifecycle")
        if old_directory != directory and old_directory.exists():
            if old_directory.is_symlink() or not old_directory.is_dir():
                raise ProductionWorkspaceError("legacy lifecycle directory is unsafe")
            allowed = {"receipt.json", "accounting-checkpoint.json", "accounting.lock", "provider-policy-checkpoint.json"}
            if any(item.name not in allowed or item.is_symlink() or not item.is_file() for item in old_directory.iterdir()):
                raise ProductionWorkspaceError("legacy lifecycle migration requires reconciliation")
            source = ProductionWorkspace(host_root=workspace.host_root, lifecycle_path=old_directory)
            prior = read_lifecycle_receipt(source)
            current = read_lifecycle_receipt(workspace)
            source_digest = hashlib.sha256(json.dumps(prior, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            migrated = current and current.get("legacy_lifecycle_migration", {}).get("source_receipt_digest") == source_digest
            if current is not None and current != prior and not migrated:
                raise ProductionWorkspaceError("legacy lifecycle migration generations differ")
            if current is None and prior is not None:
                # Maintenance ownership excludes any live legacy writer.
                for name in ("accounting-checkpoint.json", "provider-policy-checkpoint.json"):
                    path = old_directory / name
                    if path.exists():
                        payload = _read_private_checkpoint(path)
                        _write_private_checkpoint(directory / name, payload)
                write_lifecycle_receipt(workspace, prior)
                current = read_lifecycle_receipt(workspace)
            if prior is not None and not migrated:
                current["legacy_lifecycle_migration"] = {"revision": 1, "source_receipt_digest": source_digest,
                    "source_directory_digest": hashlib.sha256(str(old_directory).encode()).hexdigest()}
                write_lifecycle_receipt(workspace, current)
        legacy = workspace.host_root.parent / f".{workspace.host_root.name}.workspace-lifecycle.json"
        if legacy.exists() or legacy.is_symlink():
            if legacy.is_symlink() or not legacy.is_file() or legacy.stat().st_size > MAX_LIFECYCLE_RECEIPT_BYTES:
                raise ProductionWorkspaceError("legacy workspace lifecycle receipt is unsafe")
            try:
                prior = json.loads(legacy.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ProductionWorkspaceError("legacy workspace lifecycle receipt is invalid") from exc
            if not isinstance(prior, dict) or prior.get("secret_values_included") is not False:
                raise ProductionWorkspaceError("legacy workspace lifecycle receipt is invalid")
            current = read_lifecycle_receipt(workspace)
            if current is not None and current != prior:
                raise ProductionWorkspaceError("legacy lifecycle receipt requires reconciliation")
            if current is None:
                write_lifecycle_receipt(workspace, prior)
            legacy.unlink()
            parent = os.open(legacy.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        receipt = read_lifecycle_receipt(workspace) or {"secret_values_included": False}
        if "deployment_binding" not in receipt:
            # An existing ledger must prove its current generation before a
            # legacy descriptor is bound. Missing proof is never bootstrap.
            if receipt.get("inference_accounting"):
                from src.workspace.accounting_continuity import verify_accounting_generation
                verify_accounting_generation(workspace.host_root / "seraph.db", receipt["inference_accounting"])
            receipt["deployment_binding"] = {"revision": 1, "root_path_digest": workspace.identity_digest,
                "host_bind_identity": workspace.bind_identity_digest}
            if "provider_policy" not in receipt:
                receipt["provider_policy"] = {"revision": 0, "configuration_digest": None, "state": "uninitialized"}
            write_lifecycle_receipt(workspace, receipt)
            if receipt.get("inference_accounting") and receipt["provider_policy"]["revision"] == 0:
                from src.workspace.accounting_witness import revoke_restored_policy
                revoke_restored_policy(active=workspace, target=workspace.host_root)
        elif receipt["deployment_binding"].get("root_path_digest") == workspace.identity_digest:
            # Supported same-path restore changes the inode, not deployment.
            binding = receipt["deployment_binding"]
            if binding.get("host_bind_identity") != workspace.bind_identity_digest:
                receipt["deployment_binding"] = {**binding, "revision": binding["revision"] + 1,
                    "host_bind_identity": workspace.bind_identity_digest}
                write_lifecycle_receipt(workspace, receipt)
        return directory


def write_lifecycle_receipt(workspace: ProductionWorkspace, receipt: Mapping[str, Any], *, _accounting_lock_held: bool = False) -> Path:
    """Atomically persist a bounded operator receipt without secret values."""
    directory = workspace.lifecycle_directory
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ProductionWorkspaceError("workspace lifecycle directory is unsafe")
    if not directory.exists():
        if workspace.host_root == Path(CANONICAL_CONTAINER_WORKSPACE):
            raise ProductionWorkspaceError("accounting_continuity_unavailable")
        directory.mkdir(mode=0o700, parents=False)
    if _accounting_lock_held:
        return _write_lifecycle_receipt_locked(workspace, receipt)
    descriptor = os.open(directory / "accounting.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ProductionWorkspaceError("workspace accounting continuity is busy") from exc
        return _write_lifecycle_receipt_locked(workspace, receipt)
    finally:
        os.close(descriptor)


def _write_lifecycle_receipt_locked(workspace: ProductionWorkspace, receipt: Mapping[str, Any]) -> Path:
    path = lifecycle_receipt_path(workspace)
    payload = dict(receipt)
    # Lifecycle status updates must not erase the accounting high-water mark.
    prior = read_lifecycle_receipt(workspace)
    for name in ("provider_policy", "deployment_binding", "legacy_lifecycle_migration"):
        if prior is None or name not in prior:
            continue
        previous = prior[name]
        if name not in payload:
            payload[name] = previous
        else:
            incoming = payload[name]
            if (not isinstance(previous, dict) or not isinstance(incoming, dict)
                or type(incoming.get("revision")) is not int or type(previous.get("revision")) is not int
                or incoming["revision"] < previous["revision"]
                or (incoming["revision"] == previous["revision"] and incoming != previous)):
                raise ProductionWorkspaceError("workspace continuity witness cannot regress")
    if prior is not None and "inference_accounting" in prior:
        if "inference_accounting" not in payload:
            payload["inference_accounting"] = prior["inference_accounting"]
        else:
            previous, incoming = prior["inference_accounting"], payload["inference_accounting"]
            if (not isinstance(previous, dict) or not isinstance(incoming, dict)
                or incoming.get("deployment_id") != previous.get("deployment_id")
                or type(incoming.get("revision")) is not int or type(previous.get("revision")) is not int
                or incoming["revision"] < previous["revision"]
                or (incoming["revision"] == previous["revision"] and incoming != previous)):
                raise ProductionWorkspaceError("workspace accounting witness cannot regress")
    payload["secret_values_included"] = False
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if len(encoded) > MAX_LIFECYCLE_RECEIPT_BYTES:
        raise ProductionWorkspaceError("workspace lifecycle receipt exceeds bounded size")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ProductionWorkspaceError("workspace lifecycle receipt could not be written") from exc
    return path


def read_lifecycle_receipt(workspace: ProductionWorkspace) -> dict[str, Any] | None:
    """Read the durable operator receipt, failing closed on tampering."""
    path = lifecycle_receipt_path(workspace)
    if workspace.lifecycle_directory.is_symlink():
        raise ProductionWorkspaceError("workspace lifecycle directory is unsafe")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ProductionWorkspaceError("workspace lifecycle receipt is unreadable") from exc
    if stat.S_ISLNK(metadata.st_mode) or metadata.st_size > MAX_LIFECYCLE_RECEIPT_BYTES:
        raise ProductionWorkspaceError("workspace lifecycle receipt is invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductionWorkspaceError("workspace lifecycle receipt is invalid") from exc
    if not isinstance(value, dict) or value.get("secret_values_included") is not False:
        raise ProductionWorkspaceError("workspace lifecycle receipt is invalid")
    return value


def write_accounting_checkpoint(workspace: ProductionWorkspace, payload: Mapping[str, Any]) -> None:
    """Retain one content-free transaction delta under the accounting lock."""
    path = workspace.lifecycle_directory / "accounting-checkpoint.json"
    prior = read_accounting_checkpoint(workspace)
    if prior is not None and prior.get("witness", {}).get("revision", 0) > payload.get("witness", {}).get("revision", 0):
        raise ProductionWorkspaceError("accounting checkpoint cannot regress")
    _write_private_checkpoint(path, payload)


def _write_private_checkpoint(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    if len(encoded) > MAX_ACCOUNTING_CHECKPOINT_BYTES or payload.get("secret_values_included") is not False:
        raise ProductionWorkspaceError("continuity checkpoint exceeds bounded contract")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def read_accounting_checkpoint(workspace: ProductionWorkspace) -> dict[str, Any] | None:
    path = workspace.lifecycle_directory / "accounting-checkpoint.json"
    return _read_private_checkpoint(path)


def _read_private_checkpoint(path: Path) -> dict[str, Any] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_ACCOUNTING_CHECKPOINT_BYTES or metadata.st_mode & 0o077:
        raise ProductionWorkspaceError("accounting checkpoint is unsafe")
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise ProductionWorkspaceError("accounting checkpoint is invalid") from exc
    if not isinstance(payload, dict) or payload.get("secret_values_included") is not False:
        raise ProductionWorkspaceError("accounting checkpoint is invalid")
    return payload


def _invalidate_transport_credentials(connection) -> int:
    """Exact legacy edge/Telegram generations; no provider/config secrets."""
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(secrets)")}
    if not {"key", "encrypted_value"}.issubset(columns):
        return 0
    revoked = ", revoked_at = CURRENT_TIMESTAMP" if "revoked_at" in columns else ""
    before = connection.total_changes
    connection.execute(
        "UPDATE secrets SET encrypted_value = 'revoked:workspace-restore'" + revoked +
        " WHERE (substr(key,1,20) = 'seraph-node-pairing-' AND length(key) = 60 AND substr(key,21) NOT GLOB '*[^0-9a-f]*') "
        "OR (substr(key,1,25) = 'telegram.transport.token:' AND length(key) = 89 AND substr(key,26) NOT GLOB '*[^0-9a-f]*')"
    )
    return connection.total_changes - before


def _invalidate_continuity_credentials(connection) -> int:
    """A restored proof snapshot must never resurrect consumed/revoked hashes."""
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(operator_continuity_credentials)")}
    if not columns:
        return 0
    if not {"token_hash", "kind", "revoked_at"}.issubset(columns):
        raise ProductionWorkspaceReconciliationError("continuity credential invalidation schema is unavailable")
    count = int(connection.execute("SELECT COUNT(*) FROM operator_continuity_credentials WHERE revoked_at IS NULL").fetchone()[0])
    connection.execute("UPDATE operator_continuity_credentials SET revoked_at = CURRENT_TIMESTAMP WHERE revoked_at IS NULL")
    return count


def reconcile_production_restore(
    *,
    root: Path,
    stage: Path,
    source_manifest: Mapping[str, Any],
    registry: Any,
) -> dict[str, Any]:
    """Reconcile staged authority state before production promotion.

    Derived regular files are refused because their concrete index format is
    owned by the runtime.  Empty declared derived directories are recreated as
    clean rebuild targets.  Known operator sessions and durable workflow
    authority rows are invalidated in the staged SQLite database; the optional
    calendar credential is dropped so it must be re-provisioned.
    """
    _require_production_fence(registry)
    import sqlite3

    entries = source_manifest.get("entries")
    if not isinstance(entries, list):
        raise ProductionWorkspaceReconciliationError("restore source entries are unavailable")
    derived_files: list[str] = []
    derived_directories: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping) or entry.get("state_class") != "derived":
            continue
        logical_path = entry.get("logical_path")
        if not isinstance(logical_path, str):
            raise ProductionWorkspaceReconciliationError("derived restore path is invalid")
        if logical_path == MAINTENANCE_LOCK_NAME:
            # The owner lock is handed off by lifecycle code as a hard link;
            # it is never treated as archive payload or authority state.
            continue
        if entry.get("file_type") == "directory":
            derived_directories.append(logical_path)
        else:
            derived_files.append(logical_path)
    if derived_files:
        raise ProductionWorkspaceReconciliationError(
            "derived index rebuild is unavailable for stored files"
        )
    rebuilt_directories: list[str] = []
    for logical_path in sorted(derived_directories):
        target = stage.joinpath(*logical_path.split("/"))
        if not target.is_dir() or target.is_symlink():
            raise ProductionWorkspaceReconciliationError(
                "derived restore directory could not be reset"
            )
        rebuilt_directories.append(logical_path)

    database_path = stage / registry.config.database_path
    if not database_path.is_file() or database_path.is_symlink():
        raise ProductionWorkspaceReconciliationError("staged authority database is unavailable")
    from src.workspace.accounting_continuity import retain_inference_accounting
    accounting = retain_inference_accounting(active=root, target=stage, database_path=registry.config.database_path)
    from src.workspace.accounting_witness import revoke_restored_policy
    revoke_restored_policy(active=ProductionWorkspace(host_root=root), target=stage)
    invalidated_sessions = 0
    invalidated_continuity = 0
    invalidated_authority = 0
    tables_present: list[str] = []
    try:
        connection = sqlite3.connect(database_path)
        try:
            table_names = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if "operator_sessions" in table_names:
                tables_present.append("operator_sessions")
                columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(operator_sessions)").fetchall()
                }
                if "revoked_at" not in columns:
                    raise ProductionWorkspaceReconciliationError(
                        "operator session invalidation schema is unavailable"
                    )
                invalidated_sessions = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM operator_sessions WHERE revoked_at IS NULL"
                    ).fetchone()[0]
                )
                connection.execute(
                    "UPDATE operator_sessions SET revoked_at = CURRENT_TIMESTAMP "
                    "WHERE revoked_at IS NULL"
                )
            invalidated_continuity = _invalidate_continuity_credentials(connection)
            _invalidate_transport_credentials(connection)
            if "production_workflow_authority_states" in table_names:
                tables_present.append("production_workflow_authority_states")
                columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(production_workflow_authority_states)"
                    ).fetchall()
                }
                required = {"workflow_phase", "safe_replay_decision", "blocked_replay_reason"}
                if not required.issubset(columns):
                    raise ProductionWorkspaceReconciliationError(
                        "workflow authority invalidation schema is unavailable"
                    )
                invalidated_authority = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM production_workflow_authority_states "
                        "WHERE workflow_phase NOT IN ('blocked', 'cancelled', 'failed')"
                    ).fetchone()[0]
                )
                connection.execute(
                    "UPDATE production_workflow_authority_states SET "
                    "workflow_phase = 'blocked', safe_replay_decision = 'unsafe', "
                    "blocked_replay_reason = 'workspace_restore_requires_reconciliation' "
                    "WHERE workflow_phase NOT IN ('blocked', 'cancelled', 'failed')"
                )
            connection.commit()
        finally:
            connection.close()
    except ProductionWorkspaceReconciliationError:
        raise
    except sqlite3.Error as exc:
        raise ProductionWorkspaceReconciliationError(
            "staged authority state could not be invalidated"
        ) from exc

    optional_tokens_invalidated: list[str] = []
    optional_token = stage / "google_calendar_token.json"
    if optional_token.exists() or optional_token.is_symlink():
        if optional_token.is_symlink() or not optional_token.is_file():
            raise ProductionWorkspaceReconciliationError("optional token state is unsafe")
        optional_token.unlink()
        optional_tokens_invalidated.append("google_calendar_token.json")
    return {
        "status": "ready",
        "inference_accounting": accounting,
        "derived_rebuild": {
            "status": "clean_targets_recreated",
            "rebuilt_directories": rebuilt_directories,
            "stored_derived_files": 0,
        },
        "authority_invalidation": {
            "status": "applied",
            "tables_present": tables_present,
            "operator_sessions_invalidated": invalidated_sessions,
            "continuity_credentials_invalidated": invalidated_continuity,
            "workflow_authority_rows_blocked": invalidated_authority,
        },
        "token_invalidation": {
            "status": "applied",
            "optional_credentials_invalidated": optional_tokens_invalidated,
        },
        "secret_values_included": False,
    }


def reconcile_production_rollback(
    *,
    active: Path,
    target: Path,
    registry: Any,
) -> dict[str, Any]:
    """Carry restore-time safety state into the retained rollback generation.

    The retained root predates the restore and can therefore contain bearer
    sessions or executable workflow authority that the staged generation has
    already revoked or blocked.  Rollback keeps the requested canonical data,
    while reapplying those safety transitions and unioning durable tombstones,
    revocations, configuration history, and unresolved cost liabilities.
    """
    _require_production_fence(registry)
    import sqlite3

    active_db = active / registry.config.database_path
    target_db = target / registry.config.database_path
    from src.workspace.accounting_continuity import retain_inference_accounting
    accounting = retain_inference_accounting(active=active, target=target, database_path=registry.config.database_path)
    from src.workspace.accounting_witness import revoke_restored_policy
    revoke_restored_policy(active=ProductionWorkspace(host_root=active), target=target)
    if (
        not active_db.is_file()
        or active_db.is_symlink()
        or not target_db.is_file()
        or target_db.is_symlink()
    ):
        raise ProductionWorkspaceReconciliationError("rollback authority databases are unavailable")

    def table_names(connection: sqlite3.Connection) -> set[str]:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    def table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
        return [
            str(row[1])
            for row in connection.execute(
                f'PRAGMA table_info("{table.replace(chr(34), chr(34) * 2)}")'
            ).fetchall()
        ]

    def quote(identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    merged_rows: dict[str, int] = {}
    preserved_rows: dict[str, int] = {}
    invalidated_sessions = 0
    invalidated_continuity = 0
    blocked_authority = 0
    try:
        source = sqlite3.connect(active_db)
        target_connection = sqlite3.connect(target_db)
        try:
            source_tables = table_names(source)
            target_tables = table_names(target_connection)
            target_connection.execute("BEGIN IMMEDIATE")
            for table in ("tombstones", "revocations", "config_versions", "cost_liabilities"):
                if table not in source_tables or table not in target_tables:
                    continue
                source_columns = table_columns(source, table)
                target_columns = set(table_columns(target_connection, table))
                columns = [column for column in source_columns if column in target_columns]
                if not columns:
                    continue
                values = source.execute(
                    f"SELECT {', '.join(quote(column) for column in columns)} "
                    f"FROM {quote(table)}"
                ).fetchall()
                placeholders = ", ".join("?" for _ in columns)
                before_changes = target_connection.total_changes
                target_connection.executemany(
                    f"INSERT OR IGNORE INTO {quote(table)} "
                    f"({', '.join(quote(column) for column in columns)}) VALUES ({placeholders})",
                    values,
                )
                inserted = target_connection.total_changes - before_changes
                merged_rows[table] = inserted
                preserved_rows[table] = max(0, len(values) - inserted)

            if "operator_sessions" in target_tables:
                columns = set(table_columns(target_connection, "operator_sessions"))
                if "revoked_at" not in columns:
                    raise ProductionWorkspaceReconciliationError(
                        "rollback session invalidation schema is unavailable"
                    )
                invalidated_sessions = int(
                    target_connection.execute(
                        "SELECT COUNT(*) FROM operator_sessions WHERE revoked_at IS NULL"
                    ).fetchone()[0]
                )
                target_connection.execute(
                    "UPDATE operator_sessions SET revoked_at = CURRENT_TIMESTAMP "
                    "WHERE revoked_at IS NULL"
                )

            invalidated_continuity = _invalidate_continuity_credentials(target_connection)
            _invalidate_transport_credentials(target_connection)
            if "production_workflow_authority_states" in target_tables:
                columns = set(
                    table_columns(target_connection, "production_workflow_authority_states")
                )
                required = {"workflow_phase", "safe_replay_decision", "blocked_replay_reason"}
                if not required.issubset(columns):
                    raise ProductionWorkspaceReconciliationError(
                        "rollback workflow authority schema is unavailable"
                    )
                blocked_authority = int(
                    target_connection.execute(
                        "SELECT COUNT(*) FROM production_workflow_authority_states "
                        "WHERE workflow_phase NOT IN ('blocked', 'cancelled', 'failed')"
                    ).fetchone()[0]
                )
                target_connection.execute(
                    "UPDATE production_workflow_authority_states SET "
                    "workflow_phase = 'blocked', safe_replay_decision = 'unsafe', "
                    "blocked_replay_reason = 'workspace_restore_requires_reconciliation' "
                    "WHERE workflow_phase NOT IN ('blocked', 'cancelled', 'failed')"
                )
            target_connection.commit()
        finally:
            source.close()
            target_connection.close()
    except ProductionWorkspaceReconciliationError:
        raise
    except sqlite3.Error as exc:
        raise ProductionWorkspaceReconciliationError(
            "rollback authority state could not be reconciled"
        ) from exc

    optional_token = target / "google_calendar_token.json"
    optional_token_invalidated = False
    if optional_token.exists() or optional_token.is_symlink():
        if optional_token.is_symlink() or not optional_token.is_file():
            raise ProductionWorkspaceReconciliationError("rollback optional token state is unsafe")
        optional_token.unlink()
        optional_token_invalidated = True
    return {
        "status": "ready",
        "rows_merged": merged_rows,
        "inference_accounting": accounting,
        "rows_preserved": preserved_rows,
        "operator_sessions_invalidated": invalidated_sessions,
            "continuity_credentials_invalidated": invalidated_continuity,
        "workflow_authority_rows_blocked": blocked_authority,
        "optional_credentials_invalidated": ["google_calendar_token.json"]
        if optional_token_invalidated
        else [],
        "secret_values_included": False,
    }


@contextmanager
def maintenance_fence(workspace: ProductionWorkspace) -> Iterator[None]:
    """Acquire an exclusive process-wide maintenance fence.

    The lock is a derived file inside the canonical bind and contains no owner
    or secret data.  A second backup/restore command or a live backend owner
    fails closed instead of attempting a mixed generation.
    """
    descriptor = _open_fenced_lock(workspace, exclusive=True)
    try:
        try:
            with lifecycle_fence_marker():
                yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


__all__ = [
    "CANONICAL_CONTAINER_WORKSPACE",
    "MOUNT_SOURCE_ENV",
    "PRODUCTION_BIND_ENV",
    "WORKSPACE_ENV",
    "DuplicateWorkspaceOwnerError",
    "ProductionWorkspaceRootChangedError",
    "ProductionWorkspaceReconciliationError",
    "ProductionWorkspace",
    "ProductionWorkspaceConfigurationError",
    "ProductionWorkspaceError",
    "ProductionWorkspaceMountError",
    "maintenance_fence",
    "runtime_workspace_owner",
    "lifecycle_receipt_path",
    "read_lifecycle_receipt",
    "reconcile_production_restore",
    "reconcile_production_rollback",
    "resolve_production_workspace",
    "validate_container_workspace_mount",
    "write_lifecycle_receipt",
]
