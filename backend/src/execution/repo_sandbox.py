"""Trusted rootless-Docker runner for the bounded repository capability.

There is intentionally no public command API here.  Callers provide a typed
job contract; this module validates it and builds a fixed Docker argv against a
configured local Unix socket.  Missing prerequisites fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import io
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import signal
import sys
import threading
import tarfile
import tempfile
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterable, Mapping, Protocol, Literal, TYPE_CHECKING
import uuid
from urllib.parse import urlparse

from config.settings import RepoSandboxSettings, settings
if TYPE_CHECKING:
    from src.workflows.repo_repair_source import RepoIterationProcessBinding


PROFILE = "repo-python-pytest-v1"
EXECUTOR_KINDS = ("local", "docker_rootless", "docker_rootful")
IMAGE_DIGEST_RE = r"^[^@/\s]+(?:/[^@\s]+)+@sha256:[0-9a-f]{64}$"
DEFAULT_TIMEOUT_SECONDS = 30
MAX_NAME_BYTES = 96
REQUIRED_RESOURCE_CONTROLLERS = ("cpu", "memory", "pids")
MAX_PERSISTED_SETTINGS_BYTES = 16 * 1024
RESOURCE_CONTROLLER_CAPABILITIES = {
    "cpu": ("CpuCfsQuota", "CpuCfsPeriod"),
    "memory": ("MemoryLimit", "SwapLimit"),
    "pids": ("PidsLimit",),
}


def _repo_sandbox_settings_path() -> Path:
    workspace = Path(settings.workspace_dir).expanduser()
    if not workspace.is_absolute():
        workspace = Path.cwd() / workspace
    return workspace / "artifacts" / "repo-sandbox" / "settings.json"


def _blocked_repo_sandbox_settings() -> RepoSandboxSettings:
    """Return a typed disabled profile for untrusted persisted state."""

    return RepoSandboxSettings(
        executor_kind="docker_rootless",
        enabled=False,
        docker_socket="",
        worker_image_digest="",
        profile=PROFILE,
    )


def _open_trusted_directory(path: Path, *, create: bool = False) -> int:
    """Open a private directory chain without following any path symlink.

    The descriptor walk starts at the filesystem root (or the current
    directory for a relative path), so a replacement of an ancestor after a
    lexical ``lstat`` cannot redirect the settings reader or writer.
    """

    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        parent_fd = os.open(os.sep, directory_flags)
        components = candidate.parts[1:]
    else:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        parent_fd = os.open(".", directory_flags)
        components = candidate.parts
    try:
        component_count = len(components)
        for index, component in enumerate(components):
            if component in {"", "."}:
                continue
            if component == "..":
                raise OSError("repository sandbox settings path contains ..")
            try:
                next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o700, dir_fd=parent_fd)
                next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
            metadata = os.fstat(parent_fd)
            writable_ancestor = bool(metadata.st_mode & 0o022)
            sticky_system_ancestor = bool(
                index < component_count - 1
                and metadata.st_mode & stat.S_ISVTX
            )
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or (writable_ancestor and not sticky_system_ancestor)
                or (metadata.st_uid not in {0, os.getuid()} and not sticky_system_ancestor)
            ):
                raise OSError("repository sandbox settings parent is untrusted")
        return parent_fd
    except BaseException:
        try:
            os.close(parent_fd)
        except OSError:
            pass
        raise


def load_persisted_repo_sandbox_settings() -> tuple[RepoSandboxSettings, str | None]:
    """Load one trusted persisted selector file for API and runner callers.

    Missing state means the environment-backed defaults remain authoritative.
    A present file must be a private, owner-owned regular file with no symlink
    in its existing parent chain and exactly the four legacy selectors plus
    the optional executor selector written by the settings endpoint. Legacy
    files remain rootless by default. Any corruption or trust failure returns
    a disabled typed profile so stale enabled process state cannot execute a
    repair.
    """

    path = _repo_sandbox_settings_path()
    workspace = Path(settings.workspace_dir).expanduser()
    if not workspace.is_absolute():
        workspace = Path.cwd() / workspace
    parent_fd = -1
    try:
        parent_fd = _open_trusted_directory(path.parent)
    except FileNotFoundError:
        return settings.repo_sandbox, None
    except OSError:
        return _blocked_repo_sandbox_settings(), "repo_sandbox_settings_parent_untrusted"
    try:
        settings_fd = -1
        try:
            settings_fd = os.open(
                path.name,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            opened = os.fstat(settings_fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_mode & 0o077
                or opened.st_uid != os.getuid()
                or opened.st_nlink != 1
                or opened.st_size > MAX_PERSISTED_SETTINGS_BYTES
            ):
                return _blocked_repo_sandbox_settings(), "repo_sandbox_settings_untrusted"
            with os.fdopen(settings_fd, "rb") as handle:
                settings_fd = -1
                raw = handle.read(MAX_PERSISTED_SETTINGS_BYTES + 1)
            if len(raw) > MAX_PERSISTED_SETTINGS_BYTES:
                return _blocked_repo_sandbox_settings(), "repo_sandbox_settings_invalid"
            payload = json.loads(raw.decode("utf-8"))
        except FileNotFoundError:
            return settings.repo_sandbox, None
        except OSError as exc:
            if exc.errno == getattr(os, "ELOOP", 40):
                return _blocked_repo_sandbox_settings(), "repo_sandbox_settings_symlinked"
            raise
        finally:
            if settings_fd >= 0:
                os.close(settings_fd)
        if not isinstance(payload, dict) or not set(payload).issubset(
            {"executor_kind", "enabled", "docker_socket", "worker_image_digest", "profile"}
        ) or not {"enabled", "docker_socket", "worker_image_digest", "profile"}.issubset(payload):
            raise ValueError("settings selectors are not exact")
        if "executor_kind" in payload and payload["executor_kind"] not in EXECUTOR_KINDS:
            raise ValueError("settings executor kind is invalid")
        current = settings.repo_sandbox.model_dump(mode="json")
        if "executor_kind" not in payload:
            payload = {**payload, "executor_kind": "docker_rootless"}
        loaded = RepoSandboxSettings.model_validate(current | payload)
    except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return _blocked_repo_sandbox_settings(), "repo_sandbox_settings_invalid"
    finally:
        if parent_fd >= 0:
            try:
                os.close(parent_fd)
            except OSError:
                pass
    return loaded, None


def persist_repo_sandbox_settings(value: RepoSandboxSettings) -> None:
    """Atomically persist the executor selector and sandbox fields through held dirfds.

    The API and the runner must share one write/read contract.  In particular,
    a lexical ``Path.replace`` is not enough here: a parent can be swapped
    between validation and publication.  We open every ancestor with
    ``O_NOFOLLOW``, create a private 0600 temporary file in that held
    directory, and rename the directory entry only after the bytes are fsynced.
    A symlink or foreign existing settings file is rejected rather than
    overwritten.
    """

    path = _repo_sandbox_settings_path()
    workspace = Path(settings.workspace_dir).expanduser()
    if not workspace.is_absolute():
        workspace = Path.cwd() / workspace
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    parent_fd = -1
    temporary_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    temporary_fd = -1
    try:
        parent_fd = _open_trusted_directory(workspace, create=True)
        for component in ("artifacts", "repo-sandbox"):
            try:
                os.mkdir(component, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
            metadata = os.fstat(parent_fd)
            # Only a directory owned by the current operator may be repaired
            # on this write path.  Keep the canonical workspace and all
            # shared/foreign ancestors under the strict reader contract; a
            # root-owned child must be replaced by an administrator instead
            # of being chmod'ed by an unprivileged Seraph process.
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise OSError("repository sandbox settings parent is untrusted")
            if stat.S_IMODE(metadata.st_mode) != 0o700:
                os.fchmod(parent_fd, 0o700)
                metadata = os.fstat(parent_fd)
                if stat.S_IMODE(metadata.st_mode) != 0o700:
                    raise OSError("repository sandbox settings parent is untrusted")

        payload = json.dumps(
            {
                "executor_kind": value.executor_kind,
                "enabled": value.enabled,
                "docker_socket": value.docker_socket,
                "worker_image_digest": value.worker_image_digest,
                "profile": value.profile,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        with os.fdopen(temporary_fd, "wb") as handle:
            temporary_fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        try:
            existing = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            stat.S_ISLNK(existing.st_mode)
            or not stat.S_ISREG(existing.st_mode)
            or existing.st_mode & 0o077
            or existing.st_uid != os.getuid()
            or existing.st_nlink != 1
        ):
            raise OSError("repository sandbox settings destination is untrusted")
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
        temporary_name = ""
    finally:
        if temporary_fd >= 0:
            try:
                os.close(temporary_fd)
            except OSError:
                pass
        if parent_fd >= 0:
            try:
                if temporary_name:
                    os.unlink(temporary_name, dir_fd=parent_fd)
            except OSError:
                pass
            try:
                os.close(parent_fd)
            except OSError:
                pass


def _effective_repo_sandbox_settings() -> RepoSandboxSettings:
    """Load persisted selectors while failing closed on a present bad file."""

    value, _error = load_persisted_repo_sandbox_settings()
    return value


def _resource_controller_snapshot(info: Mapping[str, Any]) -> dict[str, Any]:
    """Return the sanitized typed resource evidence from ``docker info``.

    These are the documented Docker ``types.SystemInfo`` capability fields.
    A caller-supplied HostConfig or an ad-hoc controller-list field is not
    accepted as enforcement evidence.
    """

    version = str(info.get("CgroupVersion") or info.get("cgroup_version") or "").strip()
    driver = str(info.get("CgroupDriver") or info.get("cgroup_driver") or "").strip().lower()
    daemon_reported = {
        field: info.get(field) if type(info.get(field)) is bool else None
        for fields in RESOURCE_CONTROLLER_CAPABILITIES.values()
        for field in fields
    }
    warnings = info.get("Warnings") or info.get("warnings") or []
    if isinstance(warnings, str):
        warnings = [warnings]
    normalized_warnings = tuple(
        str(item).strip()
        for item in warnings
        if isinstance(item, str) and item.strip()
    ) if isinstance(warnings, (list, tuple, set, frozenset)) else ()
    return {
        "cgroup_version": version or None,
        "cgroup_driver": driver or None,
        "daemon_reported": daemon_reported,
        "support_confirmed": all(value is True for value in daemon_reported.values()),
        "daemon_warnings": list(normalized_warnings),
    }


def _missing_resource_controller(snapshot: Mapping[str, Any]) -> str | None:
    """Return the first required controller that is unavailable or unknown."""

    if snapshot.get("cgroup_version") != "2" or snapshot.get("cgroup_driver") != "systemd":
        return REQUIRED_RESOURCE_CONTROLLERS[0]
    daemon_reported = snapshot.get("daemon_reported")
    if not isinstance(daemon_reported, Mapping):
        return REQUIRED_RESOURCE_CONTROLLERS[0]
    for controller in REQUIRED_RESOURCE_CONTROLLERS:
        if any(daemon_reported.get(field) is not True for field in RESOURCE_CONTROLLER_CAPABILITIES[controller]):
            return controller
    return None


class RepoSandboxError(RuntimeError):
    """A repository sandbox operation was rejected or became uncertain."""

    def __init__(
        self,
        message: str,
        *,
        phase: str = "admitted",
        terminal_status: str = "blocked",
    ) -> None:
        super().__init__(message)
        self.phase = phase
        self.terminal_status = terminal_status
        self.checkpoint_phases: tuple[str, ...] = ()


def _descriptor_flags(*, directory: bool = False) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    return flags


def _open_directory_descriptor(path: Path) -> int:
    """Open every component of an absolute directory path without following links."""

    absolute = path.absolute()
    parent_fd = os.open(os.sep, _descriptor_flags(directory=True))
    try:
        for component in PurePosixPath(absolute).parts:
            if component == os.sep:
                continue
            next_fd = os.open(component, _descriptor_flags(directory=True), dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        return parent_fd
    except OSError as exc:
        try:
            os.close(parent_fd)
        except OSError:
            pass
        raise RepoSandboxError("repository directory changed or contains a symlink") from exc


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISREG(left.st_mode)
        and stat.S_ISREG(right.st_mode)
        and left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
    )


def _same_file_metadata(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        _same_file_identity(left, right)
        and left.st_nlink == right.st_nlink
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )


def _assert_stable_file(initial: os.stat_result, final: os.stat_result) -> None:
    if not _same_file_metadata(initial, final) or final.st_nlink != 1:
        raise RepoSandboxError("repository source changed during read")


def _open_source_regular_file(
    root: Path,
    relative: str,
    *,
    expected_stat: os.stat_result | None = None,
) -> tuple[int, os.stat_result]:
    """Open one source file through descriptor-relative no-follow traversal."""

    relative_path = PurePosixPath(str(relative))
    if relative_path.is_absolute() or not relative_path.parts or ".." in relative_path.parts:
        raise RepoSandboxError("repository source path is invalid")
    parent_fd = _open_directory_descriptor(root)
    descriptor = -1
    try:
        for component in relative_path.parts[:-1]:
            next_fd = os.open(component, _descriptor_flags(directory=True), dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        descriptor = os.open(
            relative_path.parts[-1],
            _descriptor_flags(),
            dir_fd=parent_fd,
        )
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode) or opened_stat.st_nlink != 1:
            os.close(descriptor)
            descriptor = -1
            raise RepoSandboxError("repository source is not a single-link regular file")
        if expected_stat is not None and not _same_file_metadata(expected_stat, opened_stat):
            os.close(descriptor)
            descriptor = -1
            raise RepoSandboxError("repository source identity changed before read")
        return descriptor, opened_stat
    except RepoSandboxError:
        raise
    except OSError as exc:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise RepoSandboxError("repository source descriptor could not be opened") from exc
    finally:
        try:
            os.close(parent_fd)
        except OSError:
            pass


@dataclass(frozen=True, slots=True)
class RepoSandboxLimits:
    max_files: int = 2_000
    max_directories: int = 500
    max_depth: int = 16
    max_snapshot_bytes: int = 64 * 1024 * 1024
    max_file_bytes: int = 2 * 1024 * 1024
    max_patch_bytes: int = 1 * 1024 * 1024
    max_output_bytes: int = 16 * 1024 * 1024
    max_stream_bytes: int = 1 * 1024 * 1024
    max_wall_seconds: int = 180
    max_cpu_seconds: int = 120
    max_memory_bytes: int = 512 * 1024 * 1024
    max_pids: int = 64

    @classmethod
    def from_settings(cls, value: RepoSandboxSettings) -> "RepoSandboxLimits":
        result = cls(
            max_files=int(value.max_files),
            max_directories=int(value.max_directories),
            max_depth=int(value.max_depth),
            max_snapshot_bytes=int(value.max_snapshot_bytes),
            max_file_bytes=int(value.max_file_bytes),
            max_patch_bytes=int(value.max_patch_bytes),
            max_output_bytes=int(value.max_output_bytes),
            max_stream_bytes=int(value.max_stream_bytes),
            max_wall_seconds=int(value.max_wall_seconds),
            max_cpu_seconds=int(value.max_cpu_seconds),
            max_memory_bytes=int(value.max_memory_bytes),
            max_pids=int(value.max_pids),
        )
        if any(
            number <= 0
            for number in (
                result.max_files,
                result.max_directories,
                result.max_depth,
                result.max_snapshot_bytes,
                result.max_file_bytes,
                result.max_patch_bytes,
                result.max_output_bytes,
                result.max_stream_bytes,
                result.max_wall_seconds,
                result.max_cpu_seconds,
                result.max_memory_bytes,
                result.max_pids,
            )
        ):
            raise ValueError("repository sandbox limits must be positive")
        fixed = cls()
        if any(
            getattr(result, field_name) > getattr(fixed, field_name)
            for field_name in (
                "max_files",
                "max_directories",
                "max_depth",
                "max_snapshot_bytes",
                "max_file_bytes",
                "max_patch_bytes",
                "max_output_bytes",
                "max_stream_bytes",
                "max_wall_seconds",
                "max_cpu_seconds",
                "max_memory_bytes",
                "max_pids",
            )
        ):
            raise ValueError("repository sandbox resource limits exceed the fixed profile")
        return result


@dataclass(frozen=True, slots=True)
class RepoSandboxPreflight:
    ok: bool
    status: str
    reason: str | None = None
    info: dict[str, Any] = field(default_factory=dict)
    image: dict[str, Any] = field(default_factory=dict)
    executor_kind: str = "docker_rootless"
    posture: dict[str, Any] = field(default_factory=dict)
    posture_digest: str | None = None

    def as_receipt(self) -> dict[str, Any]:
        posture = dict(self.posture)
        if not posture:
            posture = {
                "kind": self.executor_kind,
                "profile": PROFILE,
                "rootless": self.info.get("rootless"),
                "limits_digest": self.info.get("limits_digest"),
                "image_digest": self.image.get("digest"),
            }
        digest = self.posture_digest or executor_posture_digest(posture)
        return {
            "profile": posture.get("profile", PROFILE),
            "executor_kind": self.executor_kind,
            "status": self.status,
            "ok": self.ok,
            "reason": self.reason,
            "rootless": self.info.get("rootless"),
            "image_digest": self.image.get("digest"),
            "cgroup_version": self.info.get("cgroup_version"),
            "cgroup_driver": self.info.get("cgroup_driver"),
            "daemon_reported": dict(self.info.get("daemon_reported") or {}),
            "support_confirmed": self.info.get("support_confirmed") is True,
            "posture": posture,
            "posture_digest": digest,
            "operator_visible": True,
        }


def executor_posture_digest(posture: Mapping[str, Any]) -> str:
    """Digest only server-built, safe executor posture metadata."""

    encoded = json.dumps(dict(posture), sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ExecutorReceipt:
    """Common safe receipt returned by local and Docker executor adapters."""

    status: str
    executor_kind: str
    profile: str
    posture: dict[str, Any]
    posture_digest: str
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def as_receipt(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "executor_kind": self.executor_kind,
            "profile": self.profile,
            "posture": dict(self.posture),
            "posture_digest": self.posture_digest,
            "reason": self.reason,
            **dict(self.details),
            "operator_visible": True,
        }


class RepoRepairExecutor(Protocol):
    """Internal server-owned repair executor contract.

    ``authority`` and ``staged_input`` are built by the workflow.  No public
    endpoint passes a command, path, socket, image, or resource limit here.
    """

    kind: Literal["local", "docker_rootless", "docker_rootful"]

    def preflight(
        self,
        authority: Mapping[str, Any] | None = None,
        *,
        deadline_at: float | None = None,
    ) -> RepoSandboxPreflight:
        ...

    def execute_job(self, job: "RepoSandboxJob", *, before_dispatch: Callable[[], None] | None = None) -> dict[str, Any]:
        ...

    def cancel(self, **kwargs: Any) -> dict[str, Any]:
        ...

    def reconcile(self, authority: Mapping[str, Any] | None = None) -> dict[str, Any]:
        ...


ExecutorPreflight = RepoSandboxPreflight


@dataclass(frozen=True, slots=True)
class SnapshotEntry:
    relative_path: str
    size_bytes: int
    sha256: str
    kind: str = "file"


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    source_root: str
    staging_root: str
    digest: str
    entries: tuple[SnapshotEntry, ...]
    total_bytes: int

    def manifest(self) -> dict[str, Any]:
        return {
            "schema": "seraph.repo_snapshot.v1",
            "digest": self.digest,
            "total_bytes": self.total_bytes,
            "entries": [
                {
                    "path": entry.relative_path,
                    "size_bytes": entry.size_bytes,
                    "sha256": entry.sha256,
                    "kind": entry.kind,
                }
                for entry in self.entries
            ],
        }


@dataclass(frozen=True, slots=True)
class RepoSandboxJob:
    job_id: str
    repository_root: str
    patch_bytes: bytes
    allowed_paths: tuple[str, ...]
    test_args: tuple[str, ...]
    authority_digest: str
    base_digest: str
    deadline_seconds: int = 180
    worker_image_digest: str = ""
    limits_digest: str = ""
    execution_deadline_at: str | None = None
    attempt_id: str = ""
    fencing_token: int = 0
    expected_posture_digest: str = ""
    expected_worker_source_sha256: str = ""
    expected_interpreter_sha256: str = ""
    expected_pytest_executable_sha256: str = ""
    expected_pytest_package_sha256: str = ""
    iteration_binding: RepoIterationProcessBinding | None = None


def _safe_relative_path(value: object) -> str:
    text = str(value or "")
    if not text or "\x00" in text:
        raise RepoSandboxError("path is empty or contains NUL")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts:
        raise RepoSandboxError("absolute and parent paths are blocked")
    normalized = path.as_posix()
    if normalized in {"", "."} or normalized.startswith("/"):
        raise RepoSandboxError("path is not a relative POSIX path")
    return normalized


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _digest_entries(entries: Iterable[SnapshotEntry]) -> str:
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda item: item.relative_path):
        digest.update(entry.relative_path.encode("utf-8"))
        digest.update(b"\0F\0")
        digest.update(str(int(entry.size_bytes)).encode("ascii"))
        digest.update(b"\0")
        digest.update(entry.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def limits_digest(limits: RepoSandboxLimits) -> str:
    payload = {
        field_name: getattr(limits, field_name)
        for field_name in limits.__dataclass_fields__
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def validate_archive_members(payload: bytes, *, max_bytes: int) -> list[str]:
    """Validate a Docker-copy tar stream before it is extracted."""
    if len(payload) > max_bytes:
        raise RepoSandboxError("transfer exceeds its byte limit")
    paths: list[str] = []
    try:
        archive = tarfile.open(fileobj=io.BytesIO(payload), mode="r:")
    except (tarfile.TarError, OSError) as exc:
        raise RepoSandboxError("transfer is not a valid tar archive") from exc
    with archive:
        for member in archive:
            relative = _safe_relative_path(member.name.rstrip("/")) if member.name.rstrip("/") else ""
            if not relative:
                continue
            if member.issym() or member.islnk() or member.isdev() or not (member.isfile() or member.isdir()):
                raise RepoSandboxError(f"unsupported transfer entry: {relative}")
            if relative in paths:
                raise RepoSandboxError(f"duplicate transfer entry: {relative}")
            paths.append(relative)
            if member.size > max_bytes:
                raise RepoSandboxError("transfer member exceeds byte limit")
    return paths


def _patch_paths_from_diff(patch: bytes, allowed_paths: Iterable[str]) -> tuple[str, ...]:
    """Return and validate the paths named by an approved unified diff."""
    allowed = {_safe_relative_path(value) for value in allowed_paths}
    if not allowed or len(allowed) > 64:
        raise RepoSandboxError("allowed_paths is invalid")
    if len(patch) > RepoSandboxLimits().max_patch_bytes:
        raise RepoSandboxError("patch byte limit exceeded")
    try:
        text = patch.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RepoSandboxError("patch must be UTF-8") from exc
    changed: set[str] = set()
    for line in text.splitlines():
        if not (line.startswith("+++ b/") or line.startswith("--- a/")):
            continue
        raw = line[6:]
        if raw == "/dev/null":
            continue
        path = _safe_relative_path(raw)
        if path not in allowed:
            raise RepoSandboxError(f"patch path is not allowed: {path}")
        changed.add(path)
    if not changed:
        raise RepoSandboxError("patch has no supported file paths")
    return tuple(sorted(changed))


def _worker_test_args(test_args: Iterable[str], allowed_paths: Iterable[str]) -> tuple[str, ...]:
    """Normalize the fixed pytest argument subset used by the image."""
    allowed = {_safe_relative_path(value) for value in allowed_paths}
    accepted_flags = {"-q", "-x", "--maxfail=1", "--disable-warnings"}
    normalized: list[str] = []
    named_path = False
    values = tuple(test_args)
    if not values or len(values) > 16:
        raise RepoSandboxError("test_args is invalid")
    for value in values:
        item = str(value)
        if not item or len(item.encode("utf-8")) > 4096:
            raise RepoSandboxError("test argument is invalid")
        if item == "pytest":
            continue
        if item in accepted_flags:
            normalized.append(item)
            continue
        path = _safe_relative_path(item)
        if path not in allowed:
            raise RepoSandboxError("test path is outside allowed_paths")
        named_path = True
        normalized.append(path)
    if not named_path:
        raise RepoSandboxError("pytest must name an allowed test path")
    return tuple(normalized)


class RootlessDockerRepoSandbox:
    """Fixed rootless Docker profile used by ``engineering.repo-change.v1``."""

    # Keep the executor identity explicit even though this adapter preserves
    # the historical strict rootless profile.  Recovery receipts and durable
    # reconciliation use this value before any Docker contact.
    kind: Literal["docker_rootless"] = "docker_rootless"

    def __init__(
        self,
        config: RepoSandboxSettings | None = None,
        *,
        docker_binary: str = "docker",
        popen: Callable[..., subprocess.Popen[bytes]] | None = None,
    ) -> None:
        self.config = config or _effective_repo_sandbox_settings()
        self.limits = RepoSandboxLimits.from_settings(self.config)
        self.docker_binary = docker_binary
        self._popen = popen or subprocess.Popen

    @staticmethod
    def validate_socket(socket: str) -> str:
        value = str(socket or "").strip()
        if not value.startswith("unix://"):
            raise RepoSandboxError("only a local unix:// Docker socket is supported")
        parsed = urlparse(value)
        path = parsed.path
        if parsed.netloc or not path.startswith("/"):
            raise RepoSandboxError("Docker socket must be an absolute local Unix path")
        return value

    @staticmethod
    def validate_image_digest(image: str) -> str:
        value = str(image or "").strip()
        import re

        if not re.fullmatch(IMAGE_DIGEST_RE, value):
            raise RepoSandboxError("worker image must be a fully qualified sha256 digest")
        return value

    def _docker_argv(self, *args: str) -> list[str]:
        socket = self.validate_socket(self.config.docker_socket)
        if any("\x00" in str(arg) for arg in args):
            raise RepoSandboxError("Docker argument contains NUL")
        return [self.docker_binary, f"--host={socket}", *[str(arg) for arg in args]]

    @staticmethod
    def _runner_env(config_dir: str) -> dict[str, str]:
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/tmp/seraph-docker-home",
            "DOCKER_CONFIG": config_dir,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }

    @staticmethod
    def _job_deadline(job: RepoSandboxJob) -> float:
        """Translate the approved UTC deadline to one monotonic bound."""

        started = time.monotonic()
        relative = started + int(job.deadline_seconds)
        if not job.execution_deadline_at:
            return relative
        try:
            value = str(job.execution_deadline_at).replace("Z", "+00:00")
            approved_wall = datetime.fromisoformat(value).astimezone(timezone.utc).timestamp()
        except (TypeError, ValueError) as exc:
            raise RepoSandboxError("execution deadline is malformed", phase="admitted") from exc
        remaining = approved_wall - time.time()
        if remaining <= 0:
            raise RepoSandboxError(
                "execution deadline has expired",
                phase="admitted",
                terminal_status="unknown_external_effect",
            )
        return min(relative, started + remaining)

    @staticmethod
    def _remaining_timeout(deadline_at: float | None, requested: float, *, phase: str) -> float:
        if deadline_at is None:
            return max(0.0, float(requested))
        remaining = float(deadline_at) - time.monotonic()
        if remaining <= 0:
            raise RepoSandboxError(
                f"execution deadline expired during {phase}",
                phase=phase,
                terminal_status="unknown_external_effect",
            )
        return min(max(0.0, float(requested)), remaining)

    def _run_docker(
        self,
        args: list[str],
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        deadline_at: float | None = None,
    ) -> tuple[int, bytes, bytes]:
        timeout = self._remaining_timeout(deadline_at, timeout, phase="docker")
        argv = self._docker_argv(*args)
        with tempfile.TemporaryDirectory(prefix="seraph-docker-config-") as config_dir:
            process = self._popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._runner_env(config_dir),
                shell=False,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                process.communicate(timeout=self._remaining_timeout(deadline_at, 5, phase="docker_cleanup"))
                raise RepoSandboxError("Docker command timed out") from exc
        return int(process.returncode or 0), stdout or b"", stderr or b""

    def _wait_for_export_ready(
        self,
        container_name: str,
        *,
        timeout: float,
        deadline_at: float | None = None,
    ) -> None:
        """Observe the worker's durable export barrier before cleanup.

        The worker keeps its output tmpfs alive after writing the bounded
        result.  The runner must observe the explicit marker while the
        container is still running, then copy the result before asking Docker
        to wait/stop it.  A post-exit copy is not sufficient proof that the
        worker completed the hand-off protocol.
        """
        requested_deadline = time.monotonic() + max(0.0, float(timeout))
        deadline = min(requested_deadline, deadline_at) if deadline_at is not None else requested_deadline
        while time.monotonic() < deadline:
            code, stdout, stderr = self._run_docker(
                ["logs", "--tail=64", container_name], timeout=5, deadline_at=deadline_at
            )
            if b"SERAPH_EXPORT_READY" in stdout or b"SERAPH_EXPORT_READY" in stderr:
                return
            if code != 0 and b"No such object" in stderr:
                raise RepoSandboxError("output_lost", phase="output_exported", terminal_status="failed")
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        raise RepoSandboxError("output_lost", phase="output_exported", terminal_status="failed")

    def _run_docker_stream(
        self,
        args: list[str],
        *,
        stdin: bytes | None = None,
        timeout: float = 30,
        max_output_bytes: int | None = None,
        deadline_at: float | None = None,
    ) -> bytes:
        timeout = self._remaining_timeout(deadline_at, timeout, phase="docker_transfer")
        argv = self._docker_argv(*args)
        with tempfile.TemporaryDirectory(prefix="seraph-docker-config-") as config_dir:
            process = self._popen(
                argv,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._runner_env(config_dir),
                shell=False,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(input=stdin, timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.communicate(timeout=self._remaining_timeout(deadline_at, 5, phase="docker_transfer_cleanup"))
                raise RepoSandboxError("Docker transfer timed out") from exc
        if int(process.returncode or 0) != 0:
            raise RepoSandboxError(f"Docker transfer failed: {stderr[-512:].decode(errors='replace')}")
        if len(stdout or b"") > int(max_output_bytes or self.limits.max_output_bytes):
            raise RepoSandboxError("Docker transfer output exceeds limit")
        return stdout or b""

    def _run_full_argv(
        self,
        argv: list[str],
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        deadline_at: float | None = None,
    ) -> tuple[int, bytes, bytes]:
        """Run one server-built Docker argv without ever invoking a shell."""
        if not argv or argv[0] != self.docker_binary:
            raise RepoSandboxError("Docker argv was not built by the trusted runner")
        timeout = self._remaining_timeout(deadline_at, timeout, phase="docker")
        with tempfile.TemporaryDirectory(prefix="seraph-docker-config-") as config_dir:
            process = self._popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._runner_env(config_dir),
                shell=False,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                process.communicate(timeout=self._remaining_timeout(deadline_at, 5, phase="docker_cleanup"))
                raise RepoSandboxError("Docker command timed out") from exc
        return int(process.returncode or 0), stdout or b"", stderr or b""

    @staticmethod
    def _server_token(job_id: str) -> str:
        digest = hashlib.sha256(str(job_id).encode("utf-8")).hexdigest()[:24]
        return f"seraph-repo-{digest}"

    @staticmethod
    def _bundle_tar(bundle: Path) -> bytes:
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w:") as archive:
            for path in sorted(bundle.rglob("*")):
                relative = path.relative_to(bundle).as_posix()
                if path.is_symlink() or not (path.is_file() or path.is_dir()):
                    raise RepoSandboxError(f"unsupported bundle entry: {relative}")
                info = archive.gettarinfo(str(path), arcname=relative)
                if info.issym() or info.islnk() or info.isdev() or not (info.isfile() or info.isdir()):
                    raise RepoSandboxError(f"unsupported bundle archive entry: {relative}")
                if info.isfile():
                    with path.open("rb") as handle:
                        archive.addfile(info, handle)
                else:
                    archive.addfile(info)
        return payload.getvalue()

    def _verify_bundle_export(self, payload: bytes, bundle: Path) -> None:
        paths = validate_archive_members(payload, max_bytes=self.limits.max_snapshot_bytes)
        expected = sorted(path.relative_to(bundle).as_posix() for path in bundle.rglob("*"))
        if sorted(paths) != expected:
            raise RepoSandboxError("input volume contents do not match the trusted bundle")
        try:
            archive = tarfile.open(fileobj=io.BytesIO(payload), mode="r:")
        except (tarfile.TarError, OSError) as exc:
            raise RepoSandboxError("input volume export is not a tar archive") from exc
        with archive:
            for member in archive:
                relative = member.name.rstrip("/")
                if not relative or not member.isfile():
                    continue
                source = bundle / relative
                if not source.is_file() or member.size != source.stat().st_size:
                    raise RepoSandboxError("input volume file metadata changed")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise RepoSandboxError("input volume file could not be read back")
                digest = hashlib.sha256()
                while chunk := extracted.read(1024 * 1024):
                    digest.update(chunk)
                if digest.hexdigest() != _digest_file(source):
                    raise RepoSandboxError("input volume file digest changed")

    def _read_input_file(
        self,
        *,
        worker_name: str,
        relative_path: str,
        deadline_at: float | None = None,
    ) -> bytes:
        """Read one fixed input file through Docker's bounded tar stream."""
        relative = _safe_relative_path(relative_path)
        payload = self._run_docker_stream(
            ["cp", f"{worker_name}:/input/{relative}", "-"],
            timeout=30,
            max_output_bytes=self.limits.max_output_bytes,
            deadline_at=deadline_at,
        )
        paths = validate_archive_members(payload, max_bytes=self.limits.max_output_bytes)
        if paths != [relative]:
            raise RepoSandboxError(f"input readback does not match {relative}")
        try:
            archive = tarfile.open(fileobj=io.BytesIO(payload), mode="r:")
        except (tarfile.TarError, OSError) as exc:
            raise RepoSandboxError("input readback is not a tar archive") from exc
        with archive:
            member = next((item for item in archive if item.name.rstrip("/") == relative), None)
            if member is None or not member.isfile():
                raise RepoSandboxError(f"input file {relative} is missing")
            handle = archive.extractfile(member)
            if handle is None:
                raise RepoSandboxError(f"input file {relative} could not be read")
            value = handle.read(self.limits.max_output_bytes + 1)
        if len(value) > self.limits.max_output_bytes:
            raise RepoSandboxError(f"input file {relative} exceeds the readback limit")
        return value

    def _validate_recovered_input(
        self,
        *,
        job: RepoSandboxJob,
        worker_name: str,
        image: str,
        patch_paths: tuple[str, ...],
        deadline_at: float | None = None,
    ) -> None:
        """Rebind recovery to the exact approved input volume contract."""
        raw_job = self._read_input_file(
            worker_name=worker_name,
            relative_path="job.json",
            deadline_at=deadline_at,
        )
        try:
            input_job = json.loads(raw_job.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RepoSandboxError("recovered input job is invalid", phase="worker_started") from exc
        if not isinstance(input_job, dict):
            raise RepoSandboxError("recovered input job is not an object", phase="worker_started")
        normalized_test_args = _worker_test_args(job.test_args, job.allowed_paths)
        expected = {
            "profile": PROFILE,
            "job_id": job.job_id,
            "authority_digest": job.authority_digest,
            "base_digest": job.base_digest,
            "snapshot_digest": job.base_digest,
            "patch_sha256": hashlib.sha256(job.patch_bytes).hexdigest(),
            "allowed_paths": list(job.allowed_paths),
            "patch_paths": list(patch_paths),
            "test_args": list(job.test_args),
            "wall_seconds": int(job.deadline_seconds),
            "cpu_seconds": int(self.limits.max_cpu_seconds),
            "worker_image_digest": image,
            "limits_digest": job.limits_digest,
            "export_grace_seconds": 30,
        }
        if input_job.get("allowed_paths") != expected["allowed_paths"]:
            raise RepoSandboxError("recovered input allowed_paths do not match approval", phase="worker_started")
        if any(input_job.get(key) != value for key, value in expected.items() if key != "allowed_paths"):
            raise RepoSandboxError("recovered input contract does not match approval", phase="worker_started")
        if list(_worker_test_args(input_job.get("test_args") or (), input_job.get("allowed_paths") or ())) != list(normalized_test_args):
            raise RepoSandboxError("recovered input test_args are not canonical", phase="worker_started")
        input_patch = self._read_input_file(
            worker_name=worker_name,
            relative_path="patch.diff",
            deadline_at=deadline_at,
        )
        if input_patch != job.patch_bytes:
            raise RepoSandboxError("recovered input patch does not match approval", phase="worker_started")
        snapshot_manifest = self._read_input_file(
            worker_name=worker_name,
            relative_path="snapshot-manifest.json",
            deadline_at=deadline_at,
        )
        try:
            snapshot = json.loads(snapshot_manifest.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RepoSandboxError("recovered snapshot manifest is invalid", phase="worker_started") from exc
        if not isinstance(snapshot, dict) or snapshot.get("digest") != job.base_digest:
            raise RepoSandboxError("recovered snapshot digest does not match approval", phase="worker_started")

    def _validate_worker_output(
        self,
        *,
        outputs: Mapping[str, bytes],
        job: RepoSandboxJob,
        image: str,
        patch_paths: tuple[str, ...],
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        """Validate both worker manifests and return the terminal test result."""
        try:
            manifest = json.loads(outputs["manifest.json"].decode("utf-8"))
            readback_manifest = json.loads(outputs["readback.json"].decode("utf-8"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RepoSandboxError("worker output manifests are invalid", phase="output_exported") from exc
        if not isinstance(manifest, dict) or not isinstance(readback_manifest, dict):
            raise RepoSandboxError("worker output manifests are invalid", phase="output_exported")
        patch_sha256 = hashlib.sha256(job.patch_bytes).hexdigest()
        required = {
            "profile": str(self.config.profile) if self.kind == "local" else PROFILE,
            "base_digest": job.base_digest,
            "snapshot_digest": job.base_digest,
            "patch_sha256": patch_sha256,
            "worker_image_digest": image,
        }
        expected_allowed_paths = sorted(_safe_relative_path(value) for value in job.allowed_paths)
        expected_test_args = list(_worker_test_args(job.test_args, job.allowed_paths))
        exported_diff = outputs.get("diff.patch")
        if not isinstance(exported_diff, bytes):
            raise RepoSandboxError("worker output diff is missing", phase="output_exported")
        if patch_paths and not exported_diff:
            raise RepoSandboxError("worker output diff is empty", phase="output_exported")
        try:
            exported_diff_paths = _patch_paths_from_diff(exported_diff, job.allowed_paths)
        except RepoSandboxError as exc:
            raise RepoSandboxError("worker output diff paths are invalid", phase="output_exported") from exc
        if not set(patch_paths).issubset(set(exported_diff_paths)):
            raise RepoSandboxError("worker output diff is missing approved patch paths", phase="output_exported")
        for value in (manifest, readback_manifest):
            if any(value.get(key) != expected for key, expected in required.items()):
                raise RepoSandboxError("worker output integrity fields do not match approval", phase="output_exported")
            if value.get("allowed_paths") != expected_allowed_paths:
                raise RepoSandboxError("worker output allowed_paths do not match approval", phase="output_exported")
            if value.get("patch_paths") != list(patch_paths):
                raise RepoSandboxError("worker output patch paths do not match approval", phase="output_exported")
            if value.get("test_args") != expected_test_args:
                raise RepoSandboxError("worker output test_args do not match approval", phase="output_exported")
            diff_paths = value.get("diff_paths")
            if (
                not isinstance(diff_paths, list)
                or any(not isinstance(path, str) for path in diff_paths)
                or diff_paths != sorted(set(diff_paths))
            ):
                raise RepoSandboxError("worker output diff paths are invalid", phase="output_exported")
            if any(_safe_relative_path(path) not in set(expected_allowed_paths) for path in diff_paths):
                raise RepoSandboxError("worker output diff path is outside approval", phase="output_exported")
            if diff_paths != list(exported_diff_paths):
                raise RepoSandboxError("worker output diff paths do not match exported diff", phase="output_exported")
        manifest_identity = manifest.get("execution_identity")
        readback_identity = readback_manifest.get("execution_identity")
        if manifest_identity is not None or readback_identity is not None:
            if not isinstance(manifest_identity, dict) or manifest_identity != readback_identity:
                raise RepoSandboxError("worker execution identity receipt is invalid", phase="output_exported")
            expected_identity = {
                "schema": "seraph.repo_repair_execution_identity.v1",
                "profile": required["profile"],
                "job_id": job.job_id,
                "authority_digest": job.authority_digest,
                "backend_kind": str(manifest.get("backend_kind") or ""),
            }
            if any(manifest_identity.get(key) != expected for key, expected in expected_identity.items()):
                raise RepoSandboxError("worker execution identity does not match approval", phase="output_exported")
            if expected_identity["backend_kind"] not in {"docker_rootless", "docker_rootful", "local"}:
                raise RepoSandboxError("worker execution backend identity is invalid", phase="output_exported")
            if expected_identity["backend_kind"] != "local":
                if manifest_identity.get("worker_image_digest") != image or manifest_identity.get("runtime_binding") != "pinned_container_image":
                    raise RepoSandboxError("worker container identity does not match approval", phase="output_exported")
        worker_source_digest = str(manifest.get("worker_source_digest") or "")
        if (
            len(worker_source_digest) != 64
            or any(char not in "0123456789abcdef" for char in worker_source_digest.lower())
            or worker_source_digest != str(readback_manifest.get("worker_source_digest") or "")
        ):
            raise RepoSandboxError("worker source integrity receipt is invalid", phase="output_exported")
        if (
            manifest.get("diff_sha256") != readback_manifest.get("diff_sha256")
            or manifest.get("diff_sha256") != hashlib.sha256(outputs["diff.patch"]).hexdigest()
        ):
            raise RepoSandboxError("worker output integrity indicates an incomplete run", phase="output_exported")
        try:
            worker_failed = (
                int(manifest.get("exit_code", 1)) != 0
                or int(readback_manifest.get("exit_code", 1)) != 0
                or bool(manifest.get("timed_out"))
                or bool(readback_manifest.get("timed_out"))
                or bool(manifest.get("stdout_truncated"))
                or bool(manifest.get("stderr_truncated"))
                or bool(readback_manifest.get("stdout_truncated"))
                or bool(readback_manifest.get("stderr_truncated"))
                or manifest.get("status") != "succeeded"
                or readback_manifest.get("status") != "succeeded"
            )
        except (TypeError, ValueError) as exc:
            raise RepoSandboxError("worker output status is invalid", phase="output_exported") from exc
        return manifest, readback_manifest, worker_failed

    def _cleanup_container_and_volume(
        self,
        *,
        container_name: str,
        input_volume: str,
        remove_volume: bool = True,
        deadline_at: float | None = None,
    ) -> dict[str, Any]:
        """Stop/remove and prove cleanup before reporting a terminal outcome."""
        receipts: list[dict[str, Any]] = []
        operations = [
            ["stop", "--time=5", container_name],
            ["kill", container_name],
            ["rm", "--force", container_name],
        ]
        if remove_volume:
            operations.append(["volume", "rm", input_volume])
        for args in operations:
            code, _stdout, _stderr = self._run_docker(args, timeout=10, deadline_at=deadline_at)
            receipts.append({"operation": args[0], "status": "ok" if code == 0 else "not_needed_or_failed"})
        code, _stdout, stderr = self._run_docker(["inspect", container_name], timeout=10, deadline_at=deadline_at)
        container_removed = code != 0 and b"No such object" in stderr
        if remove_volume:
            code, _stdout, stderr = self._run_docker(["volume", "inspect", input_volume], timeout=10, deadline_at=deadline_at)
            volume_removed = code != 0 and b"No such volume" in stderr
        else:
            volume_removed = True
        if not (container_removed and volume_removed):
            return {
                "status": "unknown_external_effect",
                "reason": "cleanup_unproven",
                "cleanup_proven": False,
                "receipts": receipts,
            }
        return {"status": "cleanup_verified", "cleanup_proven": True, "receipts": receipts}

    def _executor_receipt_fields(self, preflight: RepoSandboxPreflight) -> dict[str, Any]:
        posture = dict(preflight.posture)
        if not posture:
            posture = {
                "kind": preflight.executor_kind or self.kind,
                "profile": PROFILE,
                "rootless": preflight.info.get("rootless"),
                "limits_digest": preflight.info.get("limits_digest"),
                "image_digest": preflight.image.get("digest"),
            }
        return {
            "executor_kind": preflight.executor_kind or self.kind,
            "profile": PROFILE,
            "posture": posture,
            "posture_digest": preflight.posture_digest or executor_posture_digest(posture),
        }

    def execute_job(
        self,
        job: RepoSandboxJob,
        *,
        before_dispatch: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Execute one already-approved repository job through the fixed profile.

        The method deliberately returns an operator-safe receipt plus bounded
        output bytes.  It never mounts the selected repository and never
        accepts caller-controlled Docker argv, image, environment, or command.
        """
        if int(job.deadline_seconds) < 30 or int(job.deadline_seconds) > self.limits.max_wall_seconds:
            raise RepoSandboxError("job deadline is outside the fixed profile")
        deadline_at = self._job_deadline(job)
        configured_image = self.validate_image_digest(self.config.worker_image_digest)
        image = self.validate_image_digest(job.worker_image_digest or configured_image)
        if image != configured_image:
            raise RepoSandboxError("approved worker image no longer matches configured image")
        if job.limits_digest and job.limits_digest != limits_digest(self.limits):
            raise RepoSandboxError("approved worker limits no longer match configured limits")
        patch_paths = _patch_paths_from_diff(job.patch_bytes, job.allowed_paths)
        _worker_test_args(job.test_args, job.allowed_paths)
        token = self._server_token(job.job_id)
        loader_name = f"{token}-loader"
        worker_name = f"{token}-worker"
        input_volume = f"{token}-input"
        cleanup_receipts: list[dict[str, Any]] = []
        phase = "admitted"
        checkpoint_phases: list[str] = [phase]

        def mark_phase(value: str) -> None:
            nonlocal phase
            phase = value
            if value not in checkpoint_phases:
                checkpoint_phases.append(value)

        worker_created = False
        loader_created = False
        volume_created = False
        # Once the dispatch callback returns, at least one Docker resource
        # operation may have happened even when Docker reports an error.  Do
        # not rely on a successful create response to decide whether cleanup
        # is required.
        docker_dispatch_attempted = False
        try:
            preflight = self.preflight(deadline_at=deadline_at)
            if not preflight.ok:
                return {
                    "status": "blocked",
                    "reason": preflight.reason,
                    "preflight": preflight.as_receipt(),
                    "checkpoint_phases": checkpoint_phases,
                    "learning": "no_learning",
                    **self._executor_receipt_fields(preflight),
                }
            receipt_fields = self._executor_receipt_fields(preflight)
            with tempfile.TemporaryDirectory(prefix="seraph-repo-job-") as temp_dir:
                root = Path(temp_dir)
                snapshot = self.snapshot_repository(job.repository_root, root / "snapshot-source")
                if snapshot.digest != job.base_digest:
                    raise RepoSandboxError("repository base changed before execution")
                mark_phase("snapshot_verified")
                patch_sha256 = hashlib.sha256(job.patch_bytes).hexdigest()
                bundle = self.write_input_bundle(
                    snapshot=snapshot,
                    patch=job.patch_bytes,
                    job={
                        "job_id": job.job_id,
                        "authority_digest": job.authority_digest,
                        "base_digest": job.base_digest,
                        "patch_sha256": patch_sha256,
                        "allowed_paths": list(job.allowed_paths),
                        "patch_paths": list(patch_paths),
                        "test_args": list(job.test_args),
                        "wall_seconds": int(job.deadline_seconds),
                        "cpu_seconds": self.limits.max_cpu_seconds,
                        "worker_image_digest": image,
                        "limits_digest": job.limits_digest,
                        "export_grace_seconds": 30,
                    },
                    staging_root=root / "bundle",
                )
                transfer = self._bundle_tar(bundle)
                if before_dispatch is not None:
                    # The caller owns the durable status/fence check.  Keep
                    # this callback immediately before the first Docker
                    # resource creation so cancellation cannot authorize a
                    # stale dispatch after its final recheck.
                    before_dispatch()
                docker_dispatch_attempted = True
                code, _stdout, stderr = self._run_docker(["volume", "create", input_volume], timeout=10, deadline_at=deadline_at)
                if code != 0:
                    raise RepoSandboxError(f"input volume creation failed: {stderr[-512:].decode(errors='replace')}")
                volume_created = True

                code, _stdout, stderr = self._run_full_argv(
                    self.build_loader_argv(name=loader_name, input_volume=input_volume), timeout=10, deadline_at=deadline_at
                )
                if code != 0:
                    raise RepoSandboxError(f"loader creation failed: {stderr[-512:].decode(errors='replace')}")
                loader_created = True
                code, _stdout, stderr = self._run_docker(["start", loader_name], timeout=10, deadline_at=deadline_at)
                if code != 0:
                    raise RepoSandboxError("loader start failed")
                self._run_docker_stream(["cp", "-", f"{loader_name}:/input/"], stdin=transfer, timeout=30, deadline_at=deadline_at)
                readback = self._run_docker_stream(
                    ["cp", f"{loader_name}:/input/.", "-"],
                    timeout=30,
                    max_output_bytes=self.limits.max_snapshot_bytes,
                    deadline_at=deadline_at,
                )
                self._verify_bundle_export(readback, bundle)
                mark_phase("input_loaded")
                loader_cleanup = self._cleanup_container_and_volume(
                    container_name=loader_name,
                    input_volume=input_volume,
                    remove_volume=False,
                    deadline_at=deadline_at,
                )
                cleanup_receipts.extend(loader_cleanup.get("receipts", []))
                if loader_cleanup.get("status") == "unknown_external_effect":
                    raise RepoSandboxError("loader cleanup could not be proven")
                loader_created = False

                code, _stdout, stderr = self._run_full_argv(
                    self.build_worker_argv(name=worker_name, input_volume=input_volume), timeout=10, deadline_at=deadline_at
                )
                if code != 0:
                    raise RepoSandboxError(f"worker creation failed: {stderr[-512:].decode(errors='replace')}")
                worker_created = True
                code, _stdout, stderr = self._run_docker(["start", worker_name], timeout=10, deadline_at=deadline_at)
                if code != 0:
                    raise RepoSandboxError("worker start failed")
                inspect_code, inspect_stdout, inspect_stderr = self._run_docker(
                    ["inspect", "--format", "{{json .}}", worker_name], timeout=10, deadline_at=deadline_at
                )
                if inspect_code != 0:
                    raise RepoSandboxError("worker profile inspection failed")
                inspected = self._json_output(inspect_stdout, operation="worker inspect")
                effective_profile = self._validate_effective_profile(
                    inspected,
                    input_volume=input_volume,
                )
                try:
                    self._wait_for_export_ready(
                        worker_name,
                        timeout=min(float(job.deadline_seconds), float(self.limits.max_wall_seconds)),
                        deadline_at=deadline_at,
                    )
                    output_exported = True
                    output_tar = self._run_docker_stream(["cp", f"{worker_name}:/out/.", "-"], timeout=30, deadline_at=deadline_at)
                    expected_output = {"manifest.json", "readback.json", "diff.patch", "pytest.stdout", "pytest.stderr"}
                    self.validate_export(output_tar, expected_files=expected_output)
                    mark_phase("output_exported")
                    code, wait_stdout, wait_stderr = self._run_docker(
                        ["wait", worker_name], timeout=int(job.deadline_seconds), deadline_at=deadline_at
                    )
                    outputs: dict[str, bytes] = {}
                    archive = tarfile.open(fileobj=io.BytesIO(output_tar), mode="r:")
                    with archive:
                        for member in archive:
                            if member.name.rstrip("/") not in expected_output or not member.isfile():
                                continue
                            handle = archive.extractfile(member)
                            if handle is not None:
                                outputs[member.name.rstrip("/")] = handle.read(self.limits.max_output_bytes + 1)
                    manifest, readback_manifest, worker_failed = self._validate_worker_output(
                        outputs=outputs,
                        job=job,
                        image=image,
                        patch_paths=patch_paths,
                    )
                    try:
                        worker_failed = worker_failed or int(wait_stdout.strip() or b"1") != 0
                    except (TypeError, ValueError) as exc:
                        raise RepoSandboxError("output_lost", phase="output_exported", terminal_status="failed") from exc
                except RepoSandboxError as exc:
                    if exc.phase == "output_exported" and exc.terminal_status == "failed":
                        raise
                    raise RepoSandboxError("output_lost", phase="output_exported", terminal_status="failed") from exc
                except (OSError, ValueError, TypeError, KeyError, tarfile.TarError) as exc:
                    raise RepoSandboxError("output_lost", phase="output_exported", terminal_status="failed") from exc
                mark_phase("tests_finished")
                worker_cleanup = self._cleanup_container_and_volume(
                    container_name=worker_name,
                    input_volume=input_volume,
                    deadline_at=deadline_at,
                )
                cleanup_receipts.extend(worker_cleanup.get("receipts", []))
                worker_created = False
                volume_created = False
                if worker_cleanup.get("status") == "unknown_external_effect":
                    return {
                        "status": "unknown_external_effect",
                        "reason": "cleanup_unproven",
                        "manifest": manifest,
                        "readback": readback_manifest,
                        "cleanup": worker_cleanup,
                        "checkpoint_phases": checkpoint_phases,
                        "learning": "no_learning",
                        **receipt_fields,
                    }
                mark_phase("cleanup_verified")
                post_snapshot = self.snapshot_repository(job.repository_root, root / "post-snapshot")
                if post_snapshot.digest != job.base_digest:
                    raise RepoSandboxError("original repository changed during execution")
                terminal = "succeeded" if not worker_failed else "failed"
                return {
                    "status": terminal,
                    "failure_reason": "tests_failed" if terminal == "failed" else None,
                    "manifest": manifest,
                    "readback": readback_manifest,
                    "outputs": outputs,
                    "effective_profile": effective_profile,
                    "output_exported": output_exported,
                    "cleanup": {"status": "cleanup_verified", "receipts": cleanup_receipts},
                    "checkpoint_phases": checkpoint_phases,
                    "learning": "no_learning",
                    "operator_visible": True,
                    **receipt_fields,
                }
        except (RepoSandboxError, OSError, ValueError, TypeError, KeyError, tarfile.TarError) as raw_exc:
            exc = raw_exc if isinstance(raw_exc, RepoSandboxError) else RepoSandboxError(
                f"worker output or cleanup is invalid: {raw_exc}",
                phase=phase,
            )
            exc.phase = phase
            exc.checkpoint_phases = tuple(checkpoint_phases)
            if docker_dispatch_attempted:
                # A timeout or daemon disconnect can create a resource while
                # withholding the successful response.  Attempt both derived
                # container identities and the volume every time after the
                # dispatch fence, then keep the effect unknown if any removal
                # cannot be proven.
                cleanup = self.cancel(
                    container_name=worker_name,
                    additional_container_names=(loader_name,),
                    input_volume=input_volume,
                    deadline_at=deadline_at,
                )
                if cleanup.get("status") == "unknown_external_effect":
                    return {
                        "status": "unknown_external_effect",
                        "reason": "cleanup_unproven",
                        "cleanup": cleanup,
                        "checkpoint_phases": checkpoint_phases,
                        "learning": "no_learning",
                        **receipt_fields,
                    }
            if isinstance(raw_exc, RepoSandboxError):
                raise
            raise exc from raw_exc

    @staticmethod
    def _json_output(payload: bytes, *, operation: str) -> dict[str, Any]:
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RepoSandboxError(f"Docker {operation} returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise RepoSandboxError(f"Docker {operation} returned a non-object")
        return value

    def preflight(
        self,
        authority: Mapping[str, Any] | None = None,
        *,
        deadline_at: float | None = None,
    ) -> RepoSandboxPreflight:
        del authority
        if not bool(self.config.enabled):
            return RepoSandboxPreflight(False, "blocked", "repo_sandbox_disabled")
        if str(self.config.profile) != PROFILE:
            return RepoSandboxPreflight(False, "blocked", "unsupported_profile")
        try:
            self.validate_socket(self.config.docker_socket)
            image = self.validate_image_digest(self.config.worker_image_digest)
        except RepoSandboxError as exc:
            return RepoSandboxPreflight(False, "blocked", str(exc))
        try:
            code, stdout, stderr = self._run_docker(
                ["info", "--format", "{{json .}}"],
                deadline_at=deadline_at,
            )
        except RepoSandboxError as exc:
            return RepoSandboxPreflight(False, "blocked", f"rootless_daemon_unavailable:{exc}")
        if code != 0:
            return RepoSandboxPreflight(False, "blocked", "rootless_daemon_unavailable")
        try:
            info = self._json_output(stdout, operation="info")
        except RepoSandboxError as exc:
            return RepoSandboxPreflight(False, "blocked", str(exc))
        security_options = info.get("SecurityOptions") or info.get("SecurityOptions", [])
        rootless = bool(info.get("ServerRootless")) or any(
            "rootless" in str(item).lower() for item in security_options if isinstance(item, str)
        )
        if str(info.get("OSType") or "").lower() != "linux" or not rootless:
            return RepoSandboxPreflight(False, "blocked", "docker_daemon_is_not_rootless_linux", info={"rootless": rootless, **info})
        resource_snapshot = _resource_controller_snapshot(info)
        missing_controller = _missing_resource_controller(resource_snapshot)
        resource_info = {"rootless": True, **resource_snapshot}
        if missing_controller is not None:
            return RepoSandboxPreflight(
                False,
                "blocked",
                f"resource_controller_unavailable:{missing_controller}",
                info=resource_info,
            )
        try:
            code, stdout, stderr = self._run_docker(
                ["image", "inspect", "--format", "{{json .}}", image],
                deadline_at=deadline_at,
            )
        except RepoSandboxError as exc:
            return RepoSandboxPreflight(False, "blocked", f"pinned_image_unavailable:{exc}", info=resource_info)
        if code != 0:
            return RepoSandboxPreflight(False, "blocked", "pinned_image_unavailable", info=resource_info)
        try:
            image_info = self._json_output(stdout, operation="image inspect")
        except RepoSandboxError as exc:
            return RepoSandboxPreflight(False, "blocked", str(exc), info=resource_info)
        repo_digests = image_info.get("RepoDigests") or []
        if image not in repo_digests and str(image_info.get("Id") or "") != f"sha256:{image.rsplit(':', 1)[-1]}":
            return RepoSandboxPreflight(False, "blocked", "pinned_image_digest_mismatch", info=resource_info, image=image_info)
        return RepoSandboxPreflight(
            True,
            "ready",
            info={"server_rootless": True, **resource_info},
            image={"digest": image, **image_info},
            executor_kind="docker_rootless",
            posture={
                "kind": "docker_rootless",
                "profile": PROFILE,
                "rootless": True,
                "image_digest": image,
                "limits_digest": limits_digest(self.limits),
                "network": "none",
                "resource_controllers": "verified",
                "worker_uid": "65532:65532",
                "limits": {
                    "pids": self.limits.max_pids,
                    "memory_bytes": self.limits.max_memory_bytes,
                    "cpus": "1.0",
                },
                "privilege_model": "rootless_nonroot_worker",
            },
            posture_digest=executor_posture_digest(
                {
                    "kind": "docker_rootless",
                    "profile": PROFILE,
                    "rootless": True,
                    "image_digest": image,
                    "limits_digest": limits_digest(self.limits),
                    "network": "none",
                    "resource_controllers": "verified",
                    "worker_uid": "65532:65532",
                    "limits": {
                        "pids": self.limits.max_pids,
                        "memory_bytes": self.limits.max_memory_bytes,
                        "cpus": "1.0",
                    },
                    "privilege_model": "rootless_nonroot_worker",
                }
            ),
        )

    def _profile_args(self, *, name: str, input_volume: str, input_readonly: bool) -> list[str]:
        if len(name.encode("utf-8")) > MAX_NAME_BYTES or not name.replace("-", "").replace("_", "").isalnum():
            raise RepoSandboxError("server-generated container name is invalid")
        image = self.validate_image_digest(self.config.worker_image_digest)
        limits = self.limits
        return [
            "--pull=never",
            f"--name={name}",
            "--network=none",
            "--read-only",
            "--init",
            "--user=65532:65532",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--pids-limit={limits.max_pids}",
            f"--memory={limits.max_memory_bytes}",
            f"--memory-swap={limits.max_memory_bytes}",
            "--cpus=1.0",
            "--ulimit=nofile=256:256",
            "--tmpfs=/workspace:rw,noexec,nosuid,nodev,size=128m,uid=65532,gid=65532,mode=0700",
            "--tmpfs=/out:rw,noexec,nosuid,nodev,size=16m,uid=65532,gid=65532,mode=0700",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m,uid=65532,gid=65532,mode=0700",
            f"--mount=type=volume,source={input_volume},target=/input"
            + (",readonly" if input_readonly else ""),
            image,
        ]

    def build_loader_argv(self, *, name: str, input_volume: str) -> list[str]:
        return self._docker_argv(
            "create",
            *self._profile_args(name=name, input_volume=input_volume, input_readonly=False)[:-1],
            self.validate_image_digest(self.config.worker_image_digest),
            "/usr/local/bin/python",
            "/opt/seraph/repo_worker.py",
            "--transfer-wait",
        )

    def build_worker_argv(self, *, name: str, input_volume: str, job_file: str = "/input/job.json") -> list[str]:
        if job_file != "/input/job.json":
            raise RepoSandboxError("worker job path is fixed")
        return self._docker_argv(
            "create",
            *self._profile_args(name=name, input_volume=input_volume, input_readonly=True)[:-1],
            self.validate_image_digest(self.config.worker_image_digest),
            "/usr/local/bin/python",
            "/opt/seraph/repo_worker.py",
            "--run",
            job_file,
        )

    def _validate_effective_profile(
        self,
        inspected: Mapping[str, Any],
        *,
        input_volume: str,
    ) -> dict[str, Any]:
        """Validate Docker's effective worker settings, not only argv intent."""
        host = inspected.get("HostConfig") if isinstance(inspected, Mapping) else {}
        host = host if isinstance(host, Mapping) else {}
        config = inspected.get("Config") if isinstance(inspected, Mapping) else {}
        config = config if isinstance(config, Mapping) else {}
        config = config if isinstance(config, Mapping) else {}
        if str(host.get("NetworkMode") or "") != "none":
            raise RepoSandboxError("worker network profile is not isolated")
        if str(config.get("User") or "") not in {"65532", "65532:65532"}:
            raise RepoSandboxError("worker effective uid is not the fixed non-root uid")
        if host.get("ReadonlyRootfs") is not True:
            raise RepoSandboxError("worker root filesystem is not read-only")
        if host.get("Privileged") is True:
            raise RepoSandboxError("worker cannot run privileged")
        if int(host.get("PidsLimit") or 0) != int(self.limits.max_pids):
            raise RepoSandboxError("worker pid limit does not match the fixed profile")
        if int(host.get("Memory") or 0) != int(self.limits.max_memory_bytes):
            raise RepoSandboxError("worker memory limit does not match the fixed profile")
        if int(host.get("MemorySwap") or 0) != int(self.limits.max_memory_bytes):
            raise RepoSandboxError("worker swap limit does not match the fixed profile")
        nano_cpus = int(host.get("NanoCpus") or 0)
        if nano_cpus != 1_000_000_000:
            raise RepoSandboxError("worker cpu limit does not match the fixed profile")
        nofile = host.get("Ulimits") or []
        nofile_values = {
            value
            for item in nofile
            if isinstance(item, Mapping) and str(item.get("Name") or "") == "nofile"
            for value in (int(item.get("Soft")), int(item.get("Hard")))
        }
        if nofile_values != {256}:
            raise RepoSandboxError("worker nofile limit does not match the fixed profile")
        cap_drop = {str(item).upper() for item in (host.get("CapDrop") or [])}
        if "ALL" not in cap_drop:
            raise RepoSandboxError("worker capabilities were not dropped")
        cap_add = host.get("CapAdd") or []
        if cap_add:
            raise RepoSandboxError("worker capabilities were added")
        security_opt = {str(item).lower() for item in (host.get("SecurityOpt") or [])}
        if "no-new-privileges" not in security_opt:
            raise RepoSandboxError("worker no-new-privileges is not enabled")
        expected_tmpfs = {
            "/workspace": "rw,noexec,nosuid,nodev,size=128m,uid=65532,gid=65532,mode=0700",
            "/out": "rw,noexec,nosuid,nodev,size=16m,uid=65532,gid=65532,mode=0700",
            "/tmp": "rw,noexec,nosuid,nodev,size=64m,uid=65532,gid=65532,mode=0700",
        }
        effective_tmpfs = host.get("Tmpfs") or {}
        if not isinstance(effective_tmpfs, Mapping) or {
            str(key): str(value) for key, value in effective_tmpfs.items()
        } != expected_tmpfs:
            raise RepoSandboxError("worker tmpfs profile does not match the fixed profile")
        binds = host.get("Binds") or []
        if binds:
            raise RepoSandboxError("worker has an unapproved bind mount")
        mounts = inspected.get("Mounts") if isinstance(inspected, Mapping) else []
        mounts = mounts if isinstance(mounts, list) else []
        mount_destinations = {
            str(item.get("Destination") or "")
            for item in mounts
            if isinstance(item, Mapping)
        }
        if mount_destinations != {"/input", "/workspace", "/out", "/tmp"}:
            raise RepoSandboxError("worker mount profile does not match the fixed profile")
        input_mounts = [
            item for item in mounts
            if isinstance(item, Mapping) and str(item.get("Destination") or "") == "/input"
        ]
        if len(input_mounts) != 1:
            raise RepoSandboxError("worker input mount is missing or duplicated")
        input_mount = input_mounts[0]
        if (
            str(input_mount.get("Type") or "") != "volume"
            or str(input_mount.get("Name") or "") != input_volume
            or input_mount.get("RW") is not False
        ):
            raise RepoSandboxError("worker input volume is not read-only")
        if any(
            str(item.get("Type") or "") == "bind"
            for item in mounts
            if isinstance(item, Mapping)
        ):
            raise RepoSandboxError("worker has a host bind mount")
        return {
            "uid": str(config.get("User") or ""),
            "network": "none",
            "readonly_rootfs": True,
            "pids_limit": int(host.get("PidsLimit") or 0),
            "memory_bytes": int(host.get("Memory") or 0),
            "nano_cpus": nano_cpus,
            "nofile": 256,
            "cap_drop_all": True,
            "no_new_privileges": True,
            "input_read_only": True,
        }

    def validate_snapshot_root(self, repository_path: str | Path) -> Path:
        workspace = Path(settings.workspace_dir).expanduser().resolve()
        candidate = Path(repository_path).expanduser()
        if not candidate.is_absolute():
            candidate = workspace / candidate
        candidate = candidate.absolute()
        try:
            candidate.relative_to(workspace)
        except ValueError as exc:
            raise RepoSandboxError("repository must be beneath the canonical workspace") from exc
        current = candidate
        while current != workspace:
            if current.is_symlink():
                raise RepoSandboxError("repository path symlinks are not allowed")
            current = current.parent
        resolved = candidate.resolve(strict=True)
        if resolved == workspace or workspace not in resolved.parents:
            raise RepoSandboxError("repository must be beneath the canonical workspace")
        if not resolved.is_dir() or resolved.is_symlink():
            raise RepoSandboxError("repository root must be a real directory")
        return resolved

    def snapshot_repository(self, repository_path: str | Path, staging_root: str | Path,
            *, preserve_source_modes: bool = False) -> RepositorySnapshot:
        source = self.validate_snapshot_root(repository_path)
        destination = Path(staging_root).absolute()
        try:
            destination_metadata = destination.lstat()
        except FileNotFoundError:
            destination_metadata = None
        if destination_metadata is not None:
            if stat.S_ISLNK(destination_metadata.st_mode) or not stat.S_ISDIR(destination_metadata.st_mode):
                raise RepoSandboxError("snapshot destination is not a real directory")
            shutil.rmtree(destination)
        destination.mkdir(parents=True, exist_ok=True)
        entries: list[SnapshotEntry] = []
        directories = 0
        total_bytes = 0
        for root, dir_names, file_names in os.walk(source, topdown=True, followlinks=False):
            root_path = Path(root)
            relative_root = root_path.relative_to(source).as_posix()
            if relative_root == ".git" or relative_root.startswith(".git/"):
                dir_names[:] = []
                continue
            depth = 0 if relative_root == "." else len(PurePosixPath(relative_root).parts)
            if depth > self.limits.max_depth:
                raise RepoSandboxError("snapshot depth limit exceeded")
            safe_dirs: list[str] = []
            for name in sorted(dir_names):
                path = root_path / name
                if path.is_symlink():
                    raise RepoSandboxError(f"repository symlink is not allowed: {path}")
                safe_dirs.append(name)
                directories += 1
            dir_names[:] = safe_dirs
            if directories > self.limits.max_directories:
                raise RepoSandboxError("snapshot directory limit exceeded")
            relative_root = "" if relative_root == "." else relative_root
            for name in sorted(file_names):
                path = root_path / name
                relative = _safe_relative_path(f"{relative_root}/{name}" if relative_root else name)
                try:
                    file_stat = path.lstat()
                except OSError as exc:
                    raise RepoSandboxError(f"repository entry cannot be inspected: {relative}") from exc
                if not stat.S_ISREG(file_stat.st_mode):
                    raise RepoSandboxError(f"repository entry is not a regular file: {relative}")
                if file_stat.st_nlink != 1:
                    raise RepoSandboxError(f"repository hardlink is not allowed: {relative}")
                size = file_stat.st_size
                if size > self.limits.max_file_bytes:
                    raise RepoSandboxError(f"file limit exceeded: {relative}")
                total_bytes += size
                if total_bytes > self.limits.max_snapshot_bytes:
                    raise RepoSandboxError("snapshot byte limit exceeded")
                if len(entries) >= self.limits.max_files:
                    raise RepoSandboxError("snapshot file limit exceeded")
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                descriptor, opened_stat = _open_source_regular_file(
                    source,
                    relative,
                    expected_stat=file_stat,
                )
                try:
                    with os.fdopen(descriptor, "rb") as source_handle:
                        with target.open("xb") as target_handle:
                            if preserve_source_modes:
                                # Only classify the already-open regular source;
                                # never copy arbitrary permissions or ownership.
                                os.fchmod(target_handle.fileno(),
                                    0o700 if opened_stat.st_mode & 0o111 else 0o600)
                            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
                        _assert_stable_file(opened_stat, os.fstat(source_handle.fileno()))
                except OSError as exc:
                    raise RepoSandboxError(f"repository source could not be copied: {relative}") from exc
                entries.append(SnapshotEntry(relative, size, _digest_file(target)))
        digest = _digest_entries(entries)
        return RepositorySnapshot(str(source), str(destination), digest, tuple(entries), total_bytes)

    def write_input_bundle(
        self,
        *,
        snapshot: RepositorySnapshot,
        patch: bytes,
        job: Mapping[str, Any],
        staging_root: str | Path,
    ) -> Path:
        if len(patch) > self.limits.max_patch_bytes:
            raise RepoSandboxError("patch byte limit exceeded")
        destination = Path(staging_root).absolute()
        try:
            destination_metadata = destination.lstat()
        except FileNotFoundError:
            destination_metadata = None
        if destination_metadata is not None and (
            stat.S_ISLNK(destination_metadata.st_mode) or not stat.S_ISDIR(destination_metadata.st_mode)
        ):
            raise RepoSandboxError("input bundle destination is not a real directory")
        destination.mkdir(parents=True, exist_ok=True)
        patch_path = destination / "patch.diff"
        patch_path.write_bytes(patch)
        manifest_path = destination / "snapshot-manifest.json"
        manifest_path.write_text(json.dumps(snapshot.manifest(), sort_keys=True), encoding="utf-8")
        job_payload = dict(job)
        job_payload.update({"profile": str(self.config.profile) if self.kind == "local" else PROFILE, "snapshot_digest": snapshot.digest})
        (destination / "job.json").write_text(json.dumps(job_payload, sort_keys=True), encoding="utf-8")
        snapshot_target = destination / "snapshot"
        try:
            snapshot_target_metadata = snapshot_target.lstat()
        except FileNotFoundError:
            snapshot_target_metadata = None
        if snapshot_target_metadata is not None:
            if stat.S_ISLNK(snapshot_target_metadata.st_mode) or not stat.S_ISDIR(snapshot_target_metadata.st_mode):
                raise RepoSandboxError("input snapshot destination is not a real directory")
            shutil.rmtree(snapshot_target)
        shutil.copytree(snapshot.staging_root, snapshot_target, symlinks=False)
        return destination

    def validate_export(self, payload: bytes, *, expected_files: set[str] | None = None) -> list[str]:
        paths = validate_archive_members(payload, max_bytes=self.limits.max_output_bytes)
        if expected_files is not None and set(paths) != expected_files:
            raise RepoSandboxError("export does not match the fixed output contract")
        return paths

    def recover_job(
        self,
        job: RepoSandboxJob,
        *,
        wait_seconds: int = 30,
        expected_container_name: str | None = None,
        expected_input_volume: str | None = None,
    ) -> dict[str, Any]:
        """Adopt one matching worker after a backend restart.

        The worker and its input volume are named from the durable job ID.  A
        recovery call never creates a replacement container or reruns tests:
        it inspects that exact worker, verifies the effective profile and
        pinned input binding, reads the explicit export barrier, then performs
        the same bounded output/readback and cleanup checks as the normal path.
        Missing or mismatched state stays operator-visible and blocked.
        """

        token = self._server_token(job.job_id)
        derived_worker_name = f"{token}-worker"
        derived_loader_name = f"{token}-loader"
        derived_input_volume = f"{token}-input"
        deadline_at: float | None = None
        receipt_fields: dict[str, Any] = {
            "executor_kind": self.kind,
            "profile": PROFILE,
            "posture": {"kind": self.kind, "profile": PROFILE},
            "posture_digest": executor_posture_digest({"kind": self.kind, "profile": PROFILE}),
        }
        def validation_failure(exc: BaseException, *, phase: str = "admitted") -> dict[str, Any]:
            try:
                cleanup = self.cancel(
                    container_name=derived_worker_name,
                    additional_container_names=(derived_loader_name,),
                    input_volume=derived_input_volume,
                    deadline_at=deadline_at,
                )
            except (OSError, RepoSandboxError, ValueError) as cleanup_exc:
                cleanup = {"status": "unknown_external_effect", "reason": "cleanup_unproven", "error": str(cleanup_exc)}
            if cleanup.get("status") == "unknown_external_effect":
                return {
                    "status": "unknown_external_effect",
                    "reason": "cleanup_unproven",
                    "reason_code": "recovery_validation_cleanup_unproven",
                    "cleanup": cleanup,
                    "checkpoint_phases": [phase],
                    "operator_action": "reconcile_or_cancel",
                    "side_effects": "unknown",
                    "operator_visible": True,
                    "learning": "no_learning",
                    **receipt_fields,
                }
            return {
                "status": "failed",
                "reason": str(exc),
                "reason_code": "recovery_validation_failed",
                "cleanup": cleanup,
                "cleanup_proven": True,
                "checkpoint_phases": [phase],
                "operator_action": "inspect_output_and_create_fresh_preview",
                "side_effects": "none",
                "operator_visible": True,
                "learning": "no_learning",
                **receipt_fields,
            }
        try:
            if int(job.deadline_seconds) < 30 or int(job.deadline_seconds) > self.limits.max_wall_seconds:
                raise RepoSandboxError("job deadline is outside the fixed profile")
            deadline_at = self._job_deadline(job)
            configured_image = self.validate_image_digest(self.config.worker_image_digest)
            image = self.validate_image_digest(job.worker_image_digest or configured_image)
            if image != configured_image:
                raise RepoSandboxError("approved worker image no longer matches configured image")
            if job.limits_digest and job.limits_digest != limits_digest(self.limits):
                raise RepoSandboxError("approved worker limits no longer match configured limits")
            preflight = self.preflight(deadline_at=deadline_at)
            if not preflight.ok:
                return validation_failure(RepoSandboxError(preflight.reason), phase="admitted")
            receipt_fields = self._executor_receipt_fields(preflight)
        except (RepoSandboxError, OSError, ValueError) as exc:
            # An already-expired approved deadline is a durable external
            # effect boundary.  Do not call cancel with ``None`` (the
            # deadline has not been assigned yet) because that would permit a
            # fresh per-command cleanup budget and potentially contact Docker
            # after the authority expired.  Leave the exact worker/container
            # for governed reconciliation instead.
            if (
                isinstance(exc, RepoSandboxError)
                and exc.phase == "admitted"
                and exc.terminal_status == "unknown_external_effect"
                and str(exc) == "execution deadline has expired"
            ):
                return {
                    "status": "unknown_external_effect",
                    "reason": "execution_deadline_expired",
                    "reason_code": "recovery_deadline_expired",
                    "cleanup": {
                        "status": "not_attempted",
                        "reason": "deadline_expired_before_recovery_contact",
                    },
                    "checkpoint_phases": ["admitted"],
                    "operator_action": "reconcile_or_cancel",
                    "side_effects": "unknown",
                    "operator_visible": True,
                    "learning": "no_learning",
                    **receipt_fields,
                }
            return validation_failure(exc)
        if expected_container_name is not None and expected_container_name != derived_worker_name:
            return validation_failure(
                RepoSandboxError("recovery worker identity does not match the durable dispatch fence", phase="worker_started"),
                phase="worker_started",
            )
        if expected_input_volume is not None and expected_input_volume != derived_input_volume:
            return validation_failure(
                RepoSandboxError("recovery volume identity does not match the durable dispatch fence", phase="worker_started"),
                phase="worker_started",
            )
        worker_name = expected_container_name or derived_worker_name
        input_volume = expected_input_volume or derived_input_volume
        try:
            code, stdout, stderr = self._run_docker(
                ["inspect", "--format", "{{json .}}", worker_name], timeout=10, deadline_at=deadline_at
            )
        except Exception as exc:
            return validation_failure(exc, phase="worker_started")
        if code != 0:
            try:
                cleanup = self.cancel(
                    container_name=worker_name,
                    additional_container_names=(f"{token}-loader",),
                    input_volume=input_volume,
                    deadline_at=deadline_at,
                )
            except (OSError, RepoSandboxError) as exc:
                return {
                    "status": "unknown_external_effect",
                    "reason": "cleanup_unproven",
                    "reason_code": "output_lost_cleanup_unproven",
                    "cleanup": {"status": "unknown_external_effect", "reason": str(exc)},
                    "checkpoint_phases": ["admitted", "worker_started"],
                    "operator_action": "reconcile_or_cancel",
                    "side_effects": "unknown",
                    "operator_visible": True,
                    "learning": "no_learning",
                    **receipt_fields,
                }
            if cleanup.get("status") == "unknown_external_effect":
                return {
                    "status": "unknown_external_effect",
                    "reason": "cleanup_unproven",
                    "reason_code": "output_lost_cleanup_unproven",
                    "cleanup": cleanup,
                    "checkpoint_phases": ["admitted", "worker_started"],
                    "operator_action": "reconcile_or_cancel",
                    "side_effects": "unknown",
                    "operator_visible": True,
                    "learning": "no_learning",
                    **receipt_fields,
                }
            return {
                "status": "failed",
                "reason": "output_lost",
                "reason_code": "output_lost",
                "checkpoint_phases": ["admitted", "worker_started"],
                "operator_action": "inspect_output_and_create_fresh_preview",
                "recovery_action": "create_fresh_preview_and_approval",
                "cleanup": cleanup,
                "side_effects": "none",
                "cleanup_proven": True,
                "operator_visible": True,
                "learning": "no_learning",
                **receipt_fields,
            }
        phases = ["admitted", "worker_started"]
        try:
            # The worker was positively found by inspect.  Every subsequent
            # identity, profile, and input validation therefore remains under
            # the cleanup-protected recovery scope; malformed metadata must
            # not strand a fenced worker or its volume.
            inspected = self._json_output(stdout, operation="worker recovery inspect")
            effective_profile = self._validate_effective_profile(
                inspected,
                input_volume=input_volume,
            )
            config = inspected.get("Config") if isinstance(inspected, Mapping) else {}
            if str(config.get("Image") or "") != image:
                raise RepoSandboxError("worker image binding changed", phase="worker_started")
            patch_paths = _patch_paths_from_diff(job.patch_bytes, job.allowed_paths)
            _worker_test_args(job.test_args, job.allowed_paths)
            self._validate_recovered_input(
                job=job,
                worker_name=worker_name,
                image=image,
                patch_paths=patch_paths,
                deadline_at=deadline_at,
            )
            phases.append("input_loaded")
            try:
                self._wait_for_export_ready(
                    worker_name,
                    timeout=min(max(1, int(wait_seconds)), int(job.deadline_seconds)),
                    deadline_at=deadline_at,
                )
                output_tar = self._run_docker_stream(
                    ["cp", f"{worker_name}:/out/.", "-"],
                    timeout=30,
                    max_output_bytes=self.limits.max_output_bytes,
                    deadline_at=deadline_at,
                )
                expected_output = {"manifest.json", "readback.json", "diff.patch", "pytest.stdout", "pytest.stderr"}
                self.validate_export(output_tar, expected_files=expected_output)
                outputs: dict[str, bytes] = {}
                archive = tarfile.open(fileobj=io.BytesIO(output_tar), mode="r:")
                with archive:
                    for member in archive:
                        name = member.name.rstrip("/")
                        if name not in expected_output or not member.isfile():
                            continue
                        handle = archive.extractfile(member)
                        if handle is not None:
                            outputs[name] = handle.read(self.limits.max_output_bytes + 1)
                manifest, readback_manifest, worker_failed = self._validate_worker_output(
                    outputs=outputs,
                    job=job,
                    image=image,
                    patch_paths=patch_paths,
                )
            except RepoSandboxError as exc:
                if exc.phase == "output_exported" and exc.terminal_status == "failed":
                    raise
                raise RepoSandboxError("output_lost", phase="output_exported", terminal_status="failed") from exc
            except (OSError, ValueError, TypeError, KeyError, tarfile.TarError) as exc:
                raise RepoSandboxError("output_lost", phase="output_exported", terminal_status="failed") from exc
            phases.append("output_exported")
            phases.append("tests_finished")
            cleanup = self._cleanup_container_and_volume(
                container_name=worker_name,
                input_volume=input_volume,
                deadline_at=deadline_at,
            )
            if cleanup.get("status") == "unknown_external_effect":
                return {
                    "status": "unknown_external_effect",
                    "reason": "cleanup_unproven",
                    "manifest": manifest,
                    "readback": readback_manifest,
                    "cleanup": cleanup,
                    "checkpoint_phases": phases,
                    "learning": "no_learning",
                    **receipt_fields,
                }
            phases.append("cleanup_verified")
            with tempfile.TemporaryDirectory(prefix="seraph-repo-recovery-") as temp_dir:
                post_snapshot = self.snapshot_repository(job.repository_root, Path(temp_dir) / "post-snapshot")
            if post_snapshot.digest != job.base_digest:
                raise RepoSandboxError("original repository changed during recovery", phase="cleanup_verified")
            terminal = "succeeded" if not worker_failed else "failed"
            return {
                "status": terminal,
                "failure_reason": "tests_failed" if terminal == "failed" else None,
                "manifest": manifest,
                "readback": readback_manifest,
                "outputs": outputs,
                "effective_profile": effective_profile,
                "cleanup": {"status": "cleanup_verified", "receipts": cleanup.get("receipts", [])},
                "checkpoint_phases": phases,
                "learning": "no_learning",
                "operator_visible": True,
                **receipt_fields,
            }
        except (RepoSandboxError, OSError, ValueError, TypeError, KeyError, tarfile.TarError) as raw_exc:
            exc = raw_exc if isinstance(raw_exc, RepoSandboxError) else RepoSandboxError(
                f"worker recovery output is invalid: {raw_exc}",
                phase=phases[-1],
            )
            if not getattr(exc, "phase", None) or exc.phase == "admitted":
                exc.phase = phases[-1]
            exc.checkpoint_phases = tuple(phases)
            try:
                cleanup = self.cancel(
                    container_name=worker_name,
                    additional_container_names=(f"{token}-loader",),
                    input_volume=input_volume,
                    deadline_at=deadline_at,
                )
            except (OSError, RepoSandboxError, ValueError) as cleanup_exc:
                return {
                    "status": "unknown_external_effect",
                    "reason": "cleanup_unproven",
                    "cleanup": {"status": "unknown_external_effect", "reason": "recovery_cleanup_unavailable", "error": str(cleanup_exc)},
                    "checkpoint_phases": phases,
                    **receipt_fields,
                    "learning": "no_learning",
                }
            if cleanup.get("status") == "unknown_external_effect":
                return {
                    "status": "unknown_external_effect",
                    "reason": "cleanup_unproven",
                    "cleanup": cleanup,
                    "checkpoint_phases": phases,
                    **receipt_fields,
                    "learning": "no_learning",
                }
            if isinstance(raw_exc, RepoSandboxError):
                raise
            raise exc from raw_exc

    def cancel(
        self,
        *,
        container_name: str,
        input_volume: str,
        output_volume: str | None = None,
        additional_container_names: tuple[str, ...] = (),
        deadline_at: float | None = None,
    ) -> dict[str, Any]:
        if not container_name or not input_volume:
            raise RepoSandboxError("server-owned container and volume IDs are required")
        container_names = tuple(dict.fromkeys((container_name, *additional_container_names)))
        receipts: list[dict[str, Any]] = []
        for name in container_names:
            for args in (
                ["stop", "--time=5", name],
                ["kill", name],
                ["rm", "--force", name],
            ):
                try:
                    code, stdout, stderr = self._run_docker(args, timeout=10, deadline_at=deadline_at)
                    receipts.append({"operation": args[0], "target": name, "status": "ok" if code == 0 else "failed"})
                except Exception as exc:
                    # Cleanup must attempt every bounded target.  A transient
                    # Docker error on stop/kill cannot prevent the later rm,
                    # loader, and volume operations from running.
                    receipts.append({"operation": args[0], "target": name, "status": "error", "error": type(exc).__name__})
        try:
            code, stdout, stderr = self._run_docker(["volume", "rm", input_volume], timeout=10, deadline_at=deadline_at)
            receipts.append({"operation": "volume_rm", "target": input_volume, "status": "ok" if code == 0 else "failed"})
        except Exception as exc:
            receipts.append({"operation": "volume_rm", "target": input_volume, "status": "error", "error": type(exc).__name__})
        if output_volume:
            try:
                code, stdout, stderr = self._run_docker(["volume", "rm", output_volume], timeout=10, deadline_at=deadline_at)
                receipts.append({"operation": "volume_rm_output", "target": output_volume, "status": "ok" if code == 0 else "failed"})
            except Exception as exc:
                receipts.append({"operation": "volume_rm_output", "target": output_volume, "status": "error", "error": type(exc).__name__})
        container_checks: list[bool] = []
        for name in container_names:
            try:
                code, _, error = self._run_docker(["inspect", name], timeout=10, deadline_at=deadline_at)
                container_checks.append(code != 0 and b"No such object" in error)
            except Exception as exc:
                container_checks.append(False)
                receipts.append({"operation": "inspect", "target": name, "status": "error", "error": type(exc).__name__})
        volume_names = [input_volume] + ([output_volume] if output_volume else [])
        volume_checks: list[bool] = []
        for volume_name in volume_names:
            try:
                volume_code, _, volume_error = self._run_docker(["volume", "inspect", volume_name], timeout=10, deadline_at=deadline_at)
                volume_checks.append(volume_code != 0 and b"No such volume" in volume_error)
            except Exception as exc:
                volume_checks.append(False)
                receipts.append({"operation": "volume_inspect", "target": volume_name, "status": "error", "error": type(exc).__name__})
        proven_removed = all(container_checks) and all(volume_checks)
        if not proven_removed:
            return {"status": "unknown_external_effect", "reason": "cleanup_unproven", "receipts": receipts}
        return {"status": "cancelled", "cleanup_proven": True, "volumes_removed": True, "receipts": receipts}


class RootfulDockerRepoSandbox(RootlessDockerRepoSandbox):
    """Docker executor for an existing rootful daemon.

    The fixed worker profile remains non-root and read-only.  The distinction
    is the daemon posture: rootful readiness is reported explicitly and never
    treated as equivalent to the strict rootless profile.
    """

    kind: Literal["docker_rootful"] = "docker_rootful"

    def preflight(
        self,
        authority: Mapping[str, Any] | None = None,
        *,
        deadline_at: float | None = None,
    ) -> RepoSandboxPreflight:
        del authority
        if not bool(self.config.enabled):
            return RepoSandboxPreflight(False, "blocked", "repo_sandbox_disabled", executor_kind="docker_rootful")
        if str(self.config.profile) != PROFILE:
            return RepoSandboxPreflight(False, "blocked", "unsupported_profile", executor_kind="docker_rootful")
        try:
            self.validate_socket(self.config.docker_socket)
            image = self.validate_image_digest(self.config.worker_image_digest)
        except RepoSandboxError as exc:
            return RepoSandboxPreflight(False, "blocked", str(exc), executor_kind="docker_rootful")
        try:
            code, stdout, _stderr = self._run_docker(
                ["info", "--format", "{{json .}}"],
                deadline_at=deadline_at,
            )
        except RepoSandboxError as exc:
            return RepoSandboxPreflight(False, "blocked", f"rootful_daemon_unavailable:{exc}", executor_kind="docker_rootful")
        if code != 0:
            return RepoSandboxPreflight(False, "blocked", "rootful_daemon_unavailable", executor_kind="docker_rootful")
        try:
            info = self._json_output(stdout, operation="info")
        except RepoSandboxError as exc:
            return RepoSandboxPreflight(False, "blocked", str(exc), executor_kind="docker_rootful")
        security_options = info.get("SecurityOptions") or []
        rootless = bool(info.get("ServerRootless")) or any(
            "rootless" in str(item).lower() for item in security_options if isinstance(item, str)
        )
        if str(info.get("OSType") or "").lower() != "linux" or rootless:
            return RepoSandboxPreflight(
                False,
                "blocked",
                "docker_daemon_is_not_rootful_linux",
                info={"rootless": rootless},
                executor_kind="docker_rootful",
            )
        resource_snapshot = _resource_controller_snapshot(info)
        missing_controller = _missing_resource_controller(resource_snapshot)
        resource_info = {"rootless": False, **resource_snapshot}
        if missing_controller is not None:
            return RepoSandboxPreflight(
                False,
                "blocked",
                f"resource_controller_unavailable:{missing_controller}",
                info=resource_info,
                executor_kind="docker_rootful",
            )
        try:
            code, image_stdout, _image_stderr = self._run_docker(
                ["image", "inspect", "--format", "{{json .}}", image],
                deadline_at=deadline_at,
            )
        except RepoSandboxError as exc:
            return RepoSandboxPreflight(False, "blocked", f"pinned_image_unavailable:{exc}", info=resource_info, executor_kind="docker_rootful")
        if code != 0:
            return RepoSandboxPreflight(False, "blocked", "pinned_image_unavailable", info=resource_info, executor_kind="docker_rootful")
        try:
            image_info = self._json_output(image_stdout, operation="image inspect")
        except RepoSandboxError as exc:
            return RepoSandboxPreflight(False, "blocked", str(exc), info=resource_info, executor_kind="docker_rootful")
        repo_digests = image_info.get("RepoDigests") or []
        if image not in repo_digests and str(image_info.get("Id") or "") != f"sha256:{image.rsplit(':', 1)[-1]}":
            return RepoSandboxPreflight(False, "blocked", "pinned_image_digest_mismatch", info=resource_info, image=image_info, executor_kind="docker_rootful")
        return RepoSandboxPreflight(
            True,
            "ready",
            info=resource_info,
            image={"digest": image, **image_info},
            executor_kind="docker_rootful",
            posture={
                "kind": "docker_rootful",
                "profile": PROFILE,
                "rootless": False,
                "image_digest": image,
                "limits_digest": limits_digest(self.limits),
                "network": "none",
                "resource_controllers": "verified",
                "worker_uid": "65532:65532",
                "limits": {
                    "pids": self.limits.max_pids,
                    "memory_bytes": self.limits.max_memory_bytes,
                    "cpus": "1.0",
                },
                "privilege_model": "rootful_daemon_nonroot_worker",
            },
            posture_digest=executor_posture_digest(
                {
                    "kind": "docker_rootful",
                    "profile": PROFILE,
                    "rootless": False,
                    "image_digest": image,
                    "limits_digest": limits_digest(self.limits),
                    "network": "none",
                    "resource_controllers": "verified",
                    "worker_uid": "65532:65532",
                    "limits": {
                        "pids": self.limits.max_pids,
                        "memory_bytes": self.limits.max_memory_bytes,
                        "cpus": "1.0",
                    },
                    "privilege_model": "rootful_daemon_nonroot_worker",
                }
            ),
        )


def _local_posture(
    limits: RepoSandboxLimits,
    runtime_identity: Mapping[str, str] | None = None,
    profile: str = PROFILE,
) -> dict[str, Any]:
    posture: dict[str, Any] = {
        "kind": "local",
        "profile": profile,
        "isolation_claim": "none",
        "network_isolation": "not_verified",
        "resource_enforcement": "admission_and_wall_timeout_only",
        "host_access": "explicit_job_approval_required",
        "limits_digest": limits_digest(limits),
    }
    if runtime_identity:
        posture.update(
            {
                "worker_source_sha256": runtime_identity.get("worker_source_sha256"),
                "interpreter_sha256": runtime_identity.get("interpreter_sha256"),
                "pytest_executable_sha256": runtime_identity.get("pytest_executable_sha256"),
                "pytest_package_sha256": runtime_identity.get("pytest_package_sha256"),
            }
        )
        if profile == "repo-python-pytest-publication-v1":
            from src.execution.repo_publication_runtime import posture_projection
            posture.update(posture_projection(runtime_identity.get("publication_runtime"), runtime_identity.get("publication_configuration_revision")))
    return posture


def _local_remaining(deadline_at: float, *, phase: str) -> float:
    """Return the one remaining wall-clock budget or fail closed."""

    remaining = float(deadline_at) - time.monotonic()
    if remaining <= 0:
        raise RepoSandboxError(
            "local executor deadline expired",
            phase=phase,
            terminal_status="unknown_external_effect",
        )
    return remaining


_ITERATION_CLEANUP_SEAL = object()


@dataclass(frozen=True)
class RepoIterationCleanupWitness:
    job_id: str
    attempt_id: str
    fencing_token: int
    iteration_id: str
    iteration_index: int
    authority_digest: str
    manifest_sha256: str
    readback_sha256: str
    _projection_json: str
    _seal: Any = field(repr=False, compare=False)

    def projection(self) -> dict[str, Any]:
        if self._seal is not _ITERATION_CLEANUP_SEAL:
            raise RepoSandboxError("original iteration cleanup issuer required")
        return json.loads(self._projection_json)


def assert_repo_iteration_cleanup_witness(witness: RepoIterationCleanupWitness, job: RepoSandboxJob) -> None:
    from src.workflows.repo_repair_source import assert_repo_iteration_process_binding
    assert_repo_iteration_process_binding(job.iteration_binding, job, allow_expired_for_cleanup=True)
    binding = job.iteration_binding
    if (type(witness) is not RepoIterationCleanupWitness or witness._seal is not _ITERATION_CLEANUP_SEAL
        or witness.job_id != job.job_id or witness.attempt_id != job.attempt_id
        or witness.fencing_token != job.fencing_token or witness.authority_digest != job.authority_digest
        or witness.iteration_id != binding.iteration_id or witness.iteration_index != binding.iteration_index):
        raise RepoSandboxError("original iteration cleanup witness changed")
    projection = witness.projection()
    if (projection.get("iteration_binding") != iteration_process_projection(binding)
        or projection.get("artifact_digests", {}).get("manifest.json") != witness.manifest_sha256
        or projection.get("artifact_digests", {}).get("readback.json") != witness.readback_sha256):
        raise RepoSandboxError("original iteration cleanup witness digests changed")


def iteration_process_projection(binding) -> dict[str, Any]:
    return binding.projection()


class LocalRepoRepairExecutor(RootlessDockerRepoSandbox):
    """Trusted local staged executor for CPU-host repository repairs.

    This class intentionally does not claim a sandbox.  It keeps the original
    checkout outside the child working directory, scrubs the environment, and
    bounds process lifetime/output while making host access explicit.
    """

    kind: Literal["local"] = "local"

    def __init__(
        self,
        config: RepoSandboxSettings | None = None,
        *,
        workspace_dir: str | Path | None = None,
        popen: Callable[..., subprocess.Popen[bytes]] | None = None,
    ) -> None:
        super().__init__(config=config, popen=popen)
        self.workspace_dir = Path(workspace_dir or settings.workspace_dir).expanduser().absolute()
        self._active: dict[str, dict[str, Any]] = {}
        self._active_lock = threading.RLock()
        self._owned_iteration_terminal: dict[tuple[str, str], object] = {}

    def _iteration_cleanup_witness(self, job: RepoSandboxJob, result: dict[str, Any]) -> RepoIterationCleanupWitness:
        from src.workflows.repo_repair_source import assert_repo_iteration_process_binding
        assert_repo_iteration_process_binding(job.iteration_binding, job, allow_expired_for_cleanup=True)
        binding = job.iteration_binding
        key = (job.job_id, binding.iteration_id)
        if self._owned_iteration_terminal.pop(key, None) is not result:
            raise RepoSandboxError("Original physical iteration producer required")
        manifest = result["manifest"]
        proof = manifest.get("process_cleanup") or {}
        transport = manifest.get("supervisor_transport") or {}
        durable_transport = transport.get("transport_kind") == "original_producer_durable_v1"
        if durable_transport:
            from src.execution.repo_original_producer import verify_completion
            body, outputs = verify_completion(result["original_producer_directory"],
                result["original_producer_ready"], result["original_producer_registration"].registration_digest,
                maximum_output=self.limits.max_output_bytes)
            if (body != result["original_producer_completion"] or outputs != result["outputs"]
                    or body["manifest"] != manifest
                    or any(transport.get(name) is not True for name in (
                        "command_output_drained", "command_descriptors_closed", "original_children_waited", "no_spawn"))):
                raise RepoSandboxError("original durable producer readback changed")
        marker = self._read_job_marker(job.job_id)
        if (manifest.get("cleanup_proven") is not True or manifest.get("stage_removed") is not True
            or manifest.get("iteration_binding") != iteration_process_projection(binding)
            or proof.get("oracle") != "linux_subreaper_waitpid_echild" or proof.get("cleanup_proven") is not True
            or (not durable_transport and any(transport.get(name) is not True for name in ("stdin_closed", "stdout_eof", "stderr_eof", "stdout_closed", "stderr_closed", "waited")))
            or marker is None or marker.get("phase") != "iteration_cleanup_verified"
            or marker.get("iteration_binding") != manifest["iteration_binding"]
            or marker.get("cleanup_proven") is not True
            or result["readback"] != manifest):
            raise RepoSandboxError("Original physical iteration closure unproven")
        outputs = result["outputs"]
        projection = {"schema": "RepoWorkIterationCleanup.v1", "job_id": job.job_id,
            "attempt_id": job.attempt_id, "fencing_token": job.fencing_token,
            "iteration_binding": manifest["iteration_binding"], "process_cleanup": proof,
            "supervisor_identity": manifest["supervisor_identity"], "supervisor_transport": transport,
            "stage_removed": True, "status": "iteration_failed_quiescent" if result["status"] == "failed" else result["status"],
            "artifact_digests": {name: hashlib.sha256(value).hexdigest() for name, value in outputs.items()}}
        if marker.get("terminal_receipt", {}).get("manifest_sha256") != projection["artifact_digests"]["manifest.json"]:
            raise RepoSandboxError("Original iteration physical readback changed")
        return RepoIterationCleanupWitness(job.job_id, job.attempt_id, job.fencing_token,
            binding.iteration_id, binding.iteration_index, job.authority_digest,
            projection["artifact_digests"]["manifest.json"], projection["artifact_digests"]["readback.json"],
            json.dumps(projection, sort_keys=True, separators=(",", ":")), _ITERATION_CLEANUP_SEAL)

    def _finish_original_producer(self, job, result):
        """Publish only a physically verified original producer's marker."""
        manifest = result["manifest"]
        marker = self._read_job_marker(job.job_id)
        if marker is None or marker.get("iteration_binding") != iteration_process_projection(job.iteration_binding):
            raise RepoSandboxError("original producer marker binding changed")
        marker.update(phase="iteration_cleanup_verified", cleanup_proven=True,
            status="iteration_failed_quiescent" if result["status"] == "failed" else result["status"],
            process_cleanup={"transport_kind": "original_producer_durable_v1",
                "completion_digest": hashlib.sha256(json.dumps(result["original_producer_completion"],
                    sort_keys=True, separators=(",", ":")).encode()).hexdigest()},
            terminal_receipt={"status": result["status"],
                "manifest_sha256": hashlib.sha256(result["outputs"]["manifest.json"]).hexdigest(),
                "readback_sha256": hashlib.sha256(result["outputs"]["readback.json"]).hexdigest()})
        self._write_job_marker(job.job_id, marker)
        self._owned_iteration_terminal[(job.job_id, job.iteration_binding.iteration_id)] = result
        result["iteration_cleanup_witness"] = self._iteration_cleanup_witness(job, result)
        return result

    def _trusted_workspace(self) -> Path:
        descriptor = _open_trusted_directory(self.workspace_dir)
        try:
            metadata = os.fstat(descriptor)
            if (
                metadata.st_uid != os.getuid()
                or not stat.S_ISDIR(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise RepoSandboxError("local workspace is not private and owner-controlled")
        finally:
            os.close(descriptor)
        return self.workspace_dir

    def _trusted_staging_directory(self) -> Path:
        staging = self.workspace_dir / "artifacts" / "repo-sandbox" / "staging"
        descriptor = _open_trusted_directory(staging, create=True)
        try:
            metadata = os.fstat(descriptor)
            if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
                raise RepoSandboxError("local staging directory is not private and owner-controlled")
        finally:
            os.close(descriptor)
        return staging

    @contextmanager
    def _job_staging_directory(self, marker_token: str) -> Iterable[Path]:
        """Create the one durable stage identity owned by this job.

        A random temporary directory is unsuitable for restart recovery: after
        the worker process disappears there is no server-owned name to bind to
        the marker.  The token is derived from the job identity and the
        directory is created with exclusive semantics.  An existing directory
        is deliberately left untouched and reported as unknown so a later run
        cannot adopt another attempt's files.
        """

        parent = self._trusted_staging_directory()
        root = parent / marker_token
        try:
            root.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise RepoSandboxError(
                "local job staging identity already exists; reconcile before retry",
                phase="admitted",
                terminal_status="unknown_external_effect",
            ) from exc
        metadata = root.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise RepoSandboxError(
                "local job staging identity is not owner-controlled",
                phase="admitted",
                terminal_status="unknown_external_effect",
            )
        # The caller removes this exact directory only after terminal
        # readback.  On an exception the context intentionally leaves it in
        # place for a durable unknown/reconciliation path.
        yield root

    @staticmethod
    def _assert_stage_identity(root: Path, expected: Mapping[str, Any]) -> None:
        """Verify the exact private stage immediately before deletion."""

        try:
            metadata = root.stat(follow_symlinks=False)
            expected_device = int(expected.get("device"))
            expected_inode = int(expected.get("inode"))
        except (OSError, TypeError, ValueError) as exc:
            raise RepoSandboxError(
                "local staging identity cannot be verified before cleanup",
                phase="cleanup",
                terminal_status="unknown_external_effect",
            ) from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_dev != expected_device
            or metadata.st_ino != expected_inode
        ):
            raise RepoSandboxError(
                "local staging identity changed before cleanup",
                phase="cleanup",
                terminal_status="unknown_external_effect",
            )

    @property
    def _job_marker_directory(self) -> Path:
        return self.workspace_dir / "artifacts" / "repo-sandbox" / "jobs"

    @staticmethod
    def _job_marker_name(job_id: str) -> str:
        return hashlib.sha256(str(job_id).encode("utf-8")).hexdigest() + ".json"

    @staticmethod
    def _job_stage_token(job: RepoSandboxJob) -> str:
        binding = {
            "executor_kind": "local",
            "job_id": job.job_id,
            "attempt_id": str(job.attempt_id or "legacy-attempt"),
            "fencing_token": int(job.fencing_token or 0),
            "authority_digest": job.authority_digest,
        }
        if job.iteration_binding is not None:
            binding["iteration_id"] = job.iteration_binding.iteration_id
        return LocalRepoRepairExecutor._stage_binding_token(binding)

    @staticmethod
    def _stage_binding_token(binding: Mapping[str, Any]) -> str:
        return hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    @staticmethod
    def _pid_start_identity(pid: int) -> str | None:
        """Return a Linux process-start token to prevent PID-reuse kills."""

        try:
            raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
            fields = raw.rsplit(")", 1)[1].split()
            return fields[19] if len(fields) > 19 else None
        except (OSError, UnicodeDecodeError, ValueError):
            return None

    @staticmethod
    def _pid_state(pid: int) -> str | None:
        """Return the Linux process state for an identity-bound PID."""

        try:
            raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
            fields = raw.rsplit(")", 1)[1].split()
            return fields[0] if fields else None
        except (OSError, UnicodeDecodeError, ValueError):
            return None

    @contextmanager
    def _job_marker_lock(self, job_id: str, *, timeout_seconds: float = 2.0):
        """Serialize marker read/check/rename and Node dispatch across processes."""
        import errno
        import fcntl

        directory = _open_trusted_directory(self._job_marker_directory, create=True)
        descriptor = -1
        name = self._job_marker_name(job_id) + ".lock"
        try:
            metadata = os.fstat(directory)
            if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
                raise RepoSandboxError("local marker lock directory is not private")
            descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid() or stat.S_IMODE(opened.st_mode) != 0o600 or opened.st_nlink != 1:
                raise RepoSandboxError("local marker lock is not a private regular file")
            deadline = time.monotonic() + max(0, min(timeout_seconds, 2.0))
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise
                    if time.monotonic() >= deadline:
                        raise RepoSandboxError("local marker lock is busy") from exc
                    time.sleep(min(.005, max(0, deadline - time.monotonic())))
            named = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (not stat.S_ISREG(named.st_mode) or named.st_uid != os.getuid() or stat.S_IMODE(named.st_mode) != 0o600
                or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino) or named.st_nlink != 1):
                raise RepoSandboxError("local marker lock identity changed")
            yield
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(directory)

    def _write_job_marker(self, job_id: str, payload: Mapping[str, Any]) -> bool:
        with self._job_marker_lock(job_id):
            return self._write_job_marker_locked(job_id, payload)

    def _write_job_marker_locked(self, job_id: str, payload: Mapping[str, Any]) -> bool:
        """Write a private marker; return true if terminal cancellation won."""

        marker_dir_fd = _open_trusted_directory(self._job_marker_directory, create=True)
        marker_fd = -1
        temporary_name = f".{self._job_marker_name(job_id)}.{uuid.uuid4().hex}.tmp"
        try:
            marker_directory = os.fstat(marker_dir_fd)
            if not stat.S_ISDIR(marker_directory.st_mode) or marker_directory.st_uid != os.getuid():
                raise RepoSandboxError("local execution marker directory is not owner-controlled")
            if stat.S_IMODE(marker_directory.st_mode) != 0o700:
                os.fchmod(marker_dir_fd, 0o700)
                marker_directory = os.fstat(marker_dir_fd)
                if stat.S_IMODE(marker_directory.st_mode) != 0o700:
                    raise RepoSandboxError("local execution marker directory is not private")
            # A late callback from the original worker must not overwrite a
            # cancellation fence written by a fresh adapter between its read
            # and this atomic rename. Terminal cancellation is the only
            # non-running state allowed to replace that fence here.
            existing_payload: dict[str, Any] | None = None
            existing_metadata: os.stat_result | None = None
            existing_fd = -1
            try:
                existing_fd = os.open(
                    self._job_marker_name(job_id),
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                    dir_fd=marker_dir_fd,
                )
                existing_metadata = os.fstat(existing_fd)
                if (not stat.S_ISREG(existing_metadata.st_mode)
                    or existing_metadata.st_uid != os.getuid()
                    or stat.S_IMODE(existing_metadata.st_mode) != 0o600
                    or existing_metadata.st_nlink != 1):
                    raise RepoSandboxError("local execution marker is not owner-controlled")
                existing_raw = os.read(existing_fd, 16 * 1024 + 1)
                if len(existing_raw) > 16 * 1024:
                    raise RepoSandboxError("local execution marker exceeds the bounded size")
                parsed_existing = json.loads(existing_raw.decode("utf-8"))
                if not isinstance(parsed_existing, dict):
                    raise RepoSandboxError("local execution marker is not an object")
                existing_payload = parsed_existing
            except FileNotFoundError:
                existing_payload = None
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                raise RepoSandboxError("local execution marker cannot be verified") from exc
            finally:
                if existing_fd >= 0:
                    os.close(existing_fd)
            if existing_payload is None and payload.get("status") == "cancellation_requested":
                raise RepoSandboxError("local cancellation marker is missing")
            if existing_payload and (existing_payload.get("cancellation_requested") is True or existing_payload.get("status") == "cancellation_requested"):
                payload = {**payload, "cancellation_requested": True}
            if (
                existing_payload is not None
                and (existing_payload.get("cancellation_requested") is True or existing_payload.get("status") == "cancellation_requested")
                and payload.get("status") != "cancellation_requested"
                and payload.get("phase") != "cleanup_verified"
                and not (payload.get("phase") == "iteration_cleanup_verified"
                    and payload.get("iteration_binding") == existing_payload.get("iteration_binding")
                    and payload.get("cleanup_proven") is True)
            ):
                payload = {**payload, "status": "cancellation_requested", "phase": "cancel_requested"}
            marker_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=marker_dir_fd,
            )
            os.fchmod(marker_fd, 0o600)
            encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
            if len(encoded) > 16 * 1024:
                raise RepoSandboxError("local execution marker exceeds the bounded size")
            payload_bytes = encoded + b"\n"
            offset = 0
            while offset < len(payload_bytes):
                offset += os.write(marker_fd, payload_bytes[offset:])
            os.fsync(marker_fd)
            os.close(marker_fd)
            marker_fd = -1
            try:
                existing = os.stat(self._job_marker_name(job_id), dir_fd=marker_dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and (
                not stat.S_ISREG(existing.st_mode)
                or existing.st_uid != os.getuid()
                or stat.S_IMODE(existing.st_mode) != 0o600
                or existing.st_nlink != 1
            ):
                raise RepoSandboxError("local execution marker is not owner-controlled")
            if existing_metadata is not None and (
                existing is None or not _same_file_metadata(existing_metadata, existing)
            ):
                raise RepoSandboxError("local execution marker identity changed")
            if (existing_payload is not None
                and payload.get("profile") in {PROFILE, "repo-python-pytest-publication-v1"}
                and payload.get("status") == "cancellation_requested"
                and (existing_payload.get("phase") == "cleanup_verified"
                     or existing_payload.get("status") == "cancelled"
                     or existing_payload.get("cleanup_proven") is True)):
                # The worker may finish after cancel sets its active flag but
                # before this locked write. Preserve only its exact, verified
                # cancellation receipt, and tell cancel not to signal a PID
                # whose original process group has already been reaped.
                immutable = ("schema", "executor_kind", "profile", "job_id",
                             "authority_digest", "attempt_id", "fencing_token",
                             "base_digest", "posture_digest", "runtime_identity",
                             "stage_binding", "stage_directory", "stage_identity")
                binding = {
                    "executor_kind": "local", "job_id": job_id,
                    "authority_digest": payload.get("authority_digest"),
                    "attempt_id": payload.get("attempt_id"),
                    "fencing_token": payload.get("fencing_token"),
                }
                terminal = existing_payload.get("terminal_receipt")
                stage_identity = payload.get("stage_identity")
                valid = (
                    all(key in payload and payload[key] == existing_payload.get(key) for key in immutable)
                    and payload.get("job_id") == job_id
                    and payload.get("schema") == "seraph.repo_repair_local_job.v1"
                    and payload.get("executor_kind") == "local"
                    and isinstance(binding["authority_digest"], str) and bool(binding["authority_digest"])
                    and isinstance(binding["attempt_id"], str) and bool(binding["attempt_id"])
                    and type(binding["fencing_token"]) is int and binding["fencing_token"] >= 0
                    and type(existing_payload.get("fencing_token")) is int
                    and payload.get("stage_binding") == binding
                    and isinstance(existing_payload.get("stage_binding"), dict)
                    and type(existing_payload["stage_binding"].get("fencing_token")) is int
                    and isinstance(stage_identity, dict) and set(stage_identity) == {"device", "inode"}
                    and all(type(stage_identity[key]) is int and stage_identity[key] >= 0 for key in stage_identity)
                    and all(type(existing_payload["stage_identity"][key]) is int for key in stage_identity)
                    and payload.get("phase") == "cancel_requested"
                    and existing_payload.get("status") == "cancelled"
                    and existing_payload.get("phase") == "cleanup_verified"
                    and existing_payload.get("cleanup_proven") is True
                    and isinstance(terminal, dict) and terminal.get("status") == "cancelled"
                    and terminal.get("attempt_id") == binding["attempt_id"]
                    and type(terminal.get("fencing_token")) is int
                    and terminal.get("fencing_token") == binding["fencing_token"]
                    and terminal.get("stage_binding") == binding
                    and isinstance(terminal.get("stage_binding"), dict)
                    and type(terminal["stage_binding"].get("fencing_token")) is int
                    and terminal.get("manifest_sha256") == terminal.get("readback_sha256")
                    and all(isinstance(terminal.get(key), str) and len(terminal[key]) == 64
                            and all(character in "0123456789abcdef" for character in terminal[key])
                            for key in ("manifest_sha256", "readback_sha256"))
                )
                if not valid:
                    raise RepoSandboxError("local terminal cancellation binding is unproven")
                token = self._stage_binding_token(binding)
                staging = self.workspace_dir / "artifacts" / "repo-sandbox" / "staging"
                expected_stage = staging / token
                if payload["stage_directory"] != str(expected_stage.relative_to(self.workspace_dir)):
                    raise RepoSandboxError("local terminal cancellation stage binding changed")
                staging_fd = _open_trusted_directory(staging)
                try:
                    parent = os.fstat(staging_fd)
                    if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
                        raise RepoSandboxError("local terminal cancellation staging parent is untrusted")
                    try:
                        os.stat(token, dir_fd=staging_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        return True
                    raise RepoSandboxError("local terminal cancellation stage cleanup is unproven")
                finally:
                    os.close(staging_fd)
            os.rename(
                temporary_name,
                self._job_marker_name(job_id),
                src_dir_fd=marker_dir_fd,
                dst_dir_fd=marker_dir_fd,
            )
            os.fsync(marker_dir_fd)
            return False
        finally:
            if marker_fd >= 0:
                os.close(marker_fd)
            try:
                os.unlink(temporary_name, dir_fd=marker_dir_fd)
            except (FileNotFoundError, OSError):
                pass
            os.close(marker_dir_fd)

    def _read_job_marker(self, job_id: str) -> dict[str, Any] | None:
        try:
            marker_dir_fd = _open_trusted_directory(self._job_marker_directory)
        except (OSError, RepoSandboxError):
            return None
        marker_fd = -1
        try:
            marker_directory = os.fstat(marker_dir_fd)
            if (
                not stat.S_ISDIR(marker_directory.st_mode)
                or marker_directory.st_uid != os.getuid()
                or stat.S_IMODE(marker_directory.st_mode) != 0o700
            ):
                return None
            marker_fd = os.open(
                self._job_marker_name(job_id),
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=marker_dir_fd,
            )
            metadata = os.fstat(marker_fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
            ):
                return None
            raw = os.read(marker_fd, 16 * 1024 + 1)
            if len(raw) > 16 * 1024:
                return None
            value = json.loads(raw.decode("utf-8"))
            return dict(value) if isinstance(value, dict) else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return None
        finally:
            if marker_fd >= 0:
                os.close(marker_fd)
            os.close(marker_dir_fd)

    def validate_snapshot_root(self, repository_path: str | Path) -> Path:
        workspace = self._trusted_workspace()
        candidate = Path(repository_path).expanduser()
        if not candidate.is_absolute():
            candidate = workspace / candidate
        candidate = candidate.absolute()
        try:
            candidate.relative_to(workspace)
        except ValueError as exc:
            raise RepoSandboxError("repository must be beneath the canonical workspace") from exc
        current = candidate
        while current != workspace:
            if current.is_symlink():
                raise RepoSandboxError("repository path symlinks are not allowed")
            current = current.parent
        resolved = candidate.resolve(strict=True)
        if resolved == workspace or workspace not in resolved.parents or not resolved.is_dir() or resolved.is_symlink():
            raise RepoSandboxError("repository root must be a real directory beneath the canonical workspace")
        return resolved

    def preflight(
        self,
        authority: Mapping[str, Any] | None = None,
        *,
        deadline_at: float | None = None,
    ) -> RepoSandboxPreflight:
        del authority, deadline_at
        posture = _local_posture(self.limits, profile=str(self.config.profile))
        if not bool(self.config.enabled):
            return RepoSandboxPreflight(
                False,
                "blocked",
                "repo_sandbox_disabled",
                info={"host_access": "explicit_job_approval_required"},
                executor_kind="local",
                posture=posture,
            )
        if str(self.config.profile) not in {PROFILE, "repo-python-pytest-publication-v1"}:
            return RepoSandboxPreflight(
                False,
                "blocked",
                "unsupported_profile",
                executor_kind="local",
                posture=posture,
            )
        try:
            self._trusted_workspace()
        except (OSError, RepoSandboxError) as exc:
            return RepoSandboxPreflight(
                False,
                "blocked",
                "local_workspace_untrusted",
                info={"host_access": "explicit_job_approval_required"},
                executor_kind="local",
                posture=posture,
            )
        try:
            git_path = Path("/usr/bin/git").resolve(strict=True)
            if not stat.S_ISREG(git_path.stat().st_mode) or not os.access(git_path, os.X_OK):
                return RepoSandboxPreflight(False, "blocked", "local_git_unavailable", executor_kind="local", posture=posture)
            runtime_identity = self._local_runtime_identity()
            posture = _local_posture(self.limits, runtime_identity, profile=str(self.config.profile))
        except (OSError, RepoSandboxError, ValueError):
            return RepoSandboxPreflight(False, "blocked", "local_runtime_unavailable", executor_kind="local", posture=posture)
        return RepoSandboxPreflight(
            True,
            "ready",
            "local_staging_available",
            info={
                "host_access": "explicit_job_approval_required",
                "isolation_claim": "none",
                "runtime_identity": runtime_identity,
                "git_sha256": _digest_file(git_path),
            },
            executor_kind="local",
            posture=posture,
            posture_digest=executor_posture_digest(posture),
        )

    @staticmethod
    def _minimal_env(root: Path) -> dict[str, str]:
        home = root / "home"
        tmp = root / "tmp"
        home.mkdir(mode=0o700)
        tmp.mkdir(mode=0o700)
        git_config = root / "gitconfig"
        git_config.touch(mode=0o600, exist_ok=True)
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "CI": "1",
            "HOME": str(home),
            "TMPDIR": str(tmp),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONPATH": str(root / "workspace"),
            "PIP_NO_INDEX": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(git_config),
        }

    @staticmethod
    def _local_pytest_executable() -> str | None:
        # Never execute the console-script wrapper: its shebang can point at
        # another worktree's virtualenv.  The absolute interpreter entrypoint
        # retains the server-selected venv prefix while bypassing that shebang.
        entry = Path(sys.executable).absolute()
        try:
            target = entry.resolve(strict=True)
            metadata = target.stat()
        except OSError:
            return None
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink < 1 or not os.access(target, os.X_OK):
            return None
        return str(entry)

    @staticmethod
    def _runtime_file_digest(path: Path, *, label: str) -> str:
        try:
            metadata = path.stat()
        except OSError as exc:
            raise RepoSandboxError(f"local {label} is unavailable") from exc
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or not os.access(path, os.X_OK):
            raise RepoSandboxError(f"local {label} is not a trusted executable")
        return _digest_file(path)

    def _local_runtime_identity(self) -> dict[str, str]:
        interpreter_entry = Path(sys.executable).absolute()
        pytest_executable = self._local_pytest_executable()
        if pytest_executable is None:
            raise RepoSandboxError("local trusted interpreter is unavailable")
        interpreter = Path(interpreter_entry).resolve(strict=True)
        worker = Path(__file__).with_name("repo_worker.py").resolve(strict=True)
        pytest_path = Path(pytest_executable).absolute()
        spec = importlib.util.find_spec("pytest")
        package_origin = str(spec.origin or "") if spec is not None else ""
        if not package_origin or package_origin in {"built-in", "frozen"}:
            raise RepoSandboxError("local pytest package is unavailable")
        package_path = Path(package_origin).resolve(strict=True)
        package_metadata = package_path.stat()
        if not stat.S_ISREG(package_metadata.st_mode) or package_metadata.st_uid != os.getuid():
            raise RepoSandboxError("local pytest package is not trusted")
        result = {
            "interpreter_entry_path": str(interpreter_entry),
            "interpreter_path": str(interpreter),
            "pytest_executable_path": str(pytest_path),
            "worker_source_path": str(worker),
            "interpreter_sha256": self._runtime_file_digest(interpreter, label="interpreter"),
            "pytest_executable_sha256": self._runtime_file_digest(pytest_path, label="pytest executable"),
            "pytest_package_path": str(package_path),
            "pytest_package_sha256": _digest_file(package_path),
            "worker_source_sha256": _digest_file(worker),
        }
        if str(self.config.profile) == "repo-python-pytest-publication-v1":
            from src.execution.repo_publication_runtime import capture
            try:
                result["publication_runtime"] = capture()["proof"]
                result["publication_configuration_revision"] = hashlib.sha256(json.dumps(self.config.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            except (OSError, ValueError) as exc:
                raise RepoSandboxError("publication_runtime_unavailable:" + str(exc)) from exc
        return result

    def iterative_preflight(self, authority: Mapping[str, Any] | None = None, *, deadline_at: float | None = None) -> RepoSandboxPreflight:
        from src.execution.repo_supervisor import platform_ready
        ordinary = self.preflight(authority, deadline_at=deadline_at)
        if not ordinary.ok:
            return ordinary
        posture = dict(ordinary.posture)
        try:
            if self.config.executor_kind != "local" or str(self.config.profile) not in {PROFILE, "repo-python-pytest-publication-v1"}:
                raise RepoSandboxError("iterative_python_profile_unsupported")
            platform_ready()
            supervisor = Path(__file__).with_name("repo_supervisor.py")
            subprocess.run([sys.executable, "-I", str(supervisor), "--probe"], check=True, capture_output=True, timeout=2, env={"PATH": "/usr/bin:/bin"})
            posture.update(process_supervision="linux_per_job_subreaper", supervisor_source_sha256=_digest_file(supervisor),
                git_sha256=_digest_file(Path("/usr/bin/git").resolve()), iterative_python=True)
            return RepoSandboxPreflight(True, "ready", "iterative_python_supervision_available", info=dict(ordinary.info),
                executor_kind="local", posture=posture, posture_digest=executor_posture_digest(posture))
        except (OSError, ValueError, subprocess.SubprocessError, RepoSandboxError) as exc:
            return RepoSandboxPreflight(False, "blocked", str(exc)[:512], executor_kind="local", posture=posture)

    def _run_supervised_python(self, job: RepoSandboxJob, *, stage: Path, runtime: dict, environment: dict,
            deadline: float, observe_process: Callable, before_spawn: Callable, posture: Mapping) -> tuple[int, dict]:
        from src.execution.repo_supervisor import PYTHON_PROFILE, finish_supervisor, exact_signal, start_identity
        projection = iteration_process_projection(job.iteration_binding)
        token = self._job_stage_token(job)
        supervisor = Path(__file__).with_name("repo_supervisor.py")
        if (posture.get("supervisor_source_sha256") != _digest_file(supervisor)
            or posture.get("git_sha256") != _digest_file(Path("/usr/bin/git").resolve())):
            raise RepoSandboxError("Original iterative supervisor or Git changed before dispatch")
        payload = {"profile": PYTHON_PROFILE, "stage": str(stage), "runtime": runtime, "job_id": job.job_id,
                   "iteration_binding": projection, "deadline_at": deadline, "token": token,
                   "supervisor_source_sha256": posture["supervisor_source_sha256"], "git_sha256": posture["git_sha256"], "environment": environment}
        request = stage / "supervisor.json"
        request.write_text(json.dumps(payload, sort_keys=True))
        request.chmod(0o600)
        before_spawn()
        process = subprocess.Popen([sys.executable, "-I", str(supervisor), str(request)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, start_new_session=True)
        pid_start = start_identity(process.pid)
        transport_complete = False
        try:
            if pid_start is None:
                raise RepoSandboxError("Python supervisor identity unavailable", terminal_status="unknown_external_effect")
            observe_process(process)
            with self._job_marker_lock(job.job_id, timeout_seconds=max(0, deadline - time.monotonic())):
                marker = self._read_job_marker(job.job_id)
                if (marker is None or marker.get("iteration_binding") != projection
                    or marker.get("pid") != process.pid or marker.get("pid_start_identity") != pid_start
                    or marker.get("authority_digest") != job.authority_digest or time.monotonic() >= deadline):
                    raise RepoSandboxError("Original Python dispatch marker changed", terminal_status="unknown_external_effect")
                cancelled = marker.get("status") == "cancellation_requested" or marker.get("cancellation_requested") is True
                process.stdin.write((("cancel:" if cancelled else "") + token + "\n").encode())
                process.stdin.flush()
            process.stdin.close()
            transport = finish_supervisor(process, deadline=deadline, stream_limit=self.limits.max_stream_bytes)
            transport_complete = True
            raw = self._read_private_output(stage / "out", "supervisor-result.json")
            result = json.loads(raw)
            proof = result.get("process_cleanup") or {}
            if (process.returncode != 0 or result.get("profile") != PYTHON_PROFILE or result.get("job_id") != job.job_id
                or result.get("iteration_binding") != projection or result.get("token") != token
                or result.get("supervisor_pid") != process.pid or result.get("supervisor_start") != pid_start
                or result.get("cleanup_proven") is not True or proof.get("cleanup_proven") is not True
                or proof.get("oracle") != "linux_subreaper_waitpid_echild"):
                raise RepoSandboxError("Original Python supervisor cleanup unproven", terminal_status="unknown_external_effect")
            receipt = {"process_cleanup": proof, "supervisor_identity": {"pid": process.pid, "start_identity": pid_start,
                "source_sha256": payload["supervisor_source_sha256"], "token": token}, "supervisor_transport": transport,
                "supervisor_result_sha256": hashlib.sha256(raw).hexdigest(), "supervisor_result": result,
                "iteration_binding": projection}
            observe_process(None)
            return int(result.get("worker_exit") if result.get("worker_exit") is not None else 2), receipt
        except (OSError, ValueError, subprocess.SubprocessError, RepoSandboxError) as exc:
            marker = self._read_job_marker(job.job_id) or {}
            self._write_job_marker(job.job_id, {**marker, "status": "unknown_external_effect", "cleanup_proven": False})
            if pid_start is not None and process.poll() is None:
                exact_signal(process.pid, pid_start, signal.SIGTERM)
            raise RepoSandboxError("Original Python supervisor closure requires reconciliation", terminal_status="unknown_external_effect") from exc
        finally:
            if transport_complete:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream and not stream.closed:
                        stream.close()

    def _read_private_output(self, output_root: Path, name: str) -> bytes:
        if not name or "/" in name or "\\" in name or "\x00" in name:
            raise RepoSandboxError("local worker output name is invalid", phase="output_exported")
        directory_fd = _open_trusted_directory(output_root)
        file_fd = -1
        try:
            file_fd = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            metadata = os.fstat(file_fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
                or metadata.st_size > self.limits.max_output_bytes
            ):
                raise RepoSandboxError("local worker output file is not private", phase="output_exported")
            payload = bytearray()
            while True:
                chunk = os.read(file_fd, min(64 * 1024, self.limits.max_output_bytes + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
                if len(payload) > self.limits.max_output_bytes:
                    raise RepoSandboxError("local worker output exceeds the fixed limit", phase="output_exported")
            return bytes(payload)
        except FileNotFoundError as exc:
            raise RepoSandboxError(f"local worker output is missing: {name}", phase="output_exported") from exc
        finally:
            if file_fd >= 0:
                os.close(file_fd)
            os.close(directory_fd)

    def execute_job(
        self,
        job: RepoSandboxJob,
        *,
        before_dispatch: Callable[[], None] | None = None,
        producer_owner=None,
    ) -> dict[str, Any]:
        """Run the shared fixed worker against trusted local staged roots."""

        if producer_owner is not None:
            from src.execution.repo_original_producer import assert_original_producer_owner
            assert_original_producer_owner(producer_owner, job)

        if int(job.deadline_seconds) < 1 or int(job.deadline_seconds) > self.limits.max_wall_seconds:
            raise RepoSandboxError("job deadline is outside the local fixed profile")
        if job.iteration_binding is not None:
            from src.workflows.repo_repair_source import assert_repo_iteration_process_binding
            assert_repo_iteration_process_binding(job.iteration_binding, job)
        preflight = self.iterative_preflight() if job.iteration_binding is not None else self.preflight()
        if not preflight.ok:
            return {
                "status": "blocked",
                "reason": preflight.reason,
                "preflight": preflight.as_receipt(),
                "learning": "no_learning",
            }
        patch_paths = _patch_paths_from_diff(job.patch_bytes, job.allowed_paths)
        normalized_args = _worker_test_args(job.test_args, job.allowed_paths)
        pytest_executable = self._local_pytest_executable()
        if pytest_executable is None:
            raise RepoSandboxError("local pytest executable is unavailable")
        posture = preflight.posture or _local_posture(self.limits, profile=str(self.config.profile))
        posture_digest = preflight.posture_digest or executor_posture_digest(posture)
        receipt_fields = {
            "executor_kind": "local",
            "profile": str(self.config.profile),
            "posture": dict(posture),
            "posture_digest": posture_digest,
        }
        runtime_identity = dict(preflight.info.get("runtime_identity") or {})
        current_identity = self._local_runtime_identity()
        if current_identity != runtime_identity:
            raise RepoSandboxError("local runtime identity changed after preflight")
        if job.expected_posture_digest and job.expected_posture_digest != posture_digest:
            raise RepoSandboxError("local posture digest does not match the approved job")
        expected_identity_fields = {
            "expected_worker_source_sha256": "worker_source_sha256",
            "expected_interpreter_sha256": "interpreter_sha256",
            "expected_pytest_executable_sha256": "pytest_executable_sha256",
            "expected_pytest_package_sha256": "pytest_package_sha256",
        }
        for job_field, identity_field in expected_identity_fields.items():
            expected_value = str(getattr(job, job_field) or "")
            if expected_value and expected_value != current_identity.get(identity_field):
                raise RepoSandboxError(f"local {identity_field} does not match the approved job")

        monotonic_started = time.monotonic()
        deadline_at = monotonic_started + int(job.deadline_seconds)
        deadline_wall = time.time() + int(job.deadline_seconds)
        if job.execution_deadline_at:
            try:
                deadline_value = str(job.execution_deadline_at).replace("Z", "+00:00")
                approved_wall = datetime.fromisoformat(deadline_value).astimezone(timezone.utc).timestamp()
            except (TypeError, ValueError) as exc:
                raise RepoSandboxError("local execution deadline is malformed") from exc
            remaining_wall = approved_wall - time.time()
            if remaining_wall <= 0:
                raise RepoSandboxError(
                    "local execution deadline has expired",
                    phase="admitted",
                    terminal_status="unknown_external_effect",
                )
            deadline_at = min(deadline_at, monotonic_started + remaining_wall)
            deadline_wall = min(deadline_wall, approved_wall)

        attempt_id = str(job.attempt_id or "legacy-attempt")
        fencing_token = int(job.fencing_token or 0)
        stage_binding = {
            "executor_kind": "local",
            "job_id": job.job_id,
            "attempt_id": attempt_id,
            "fencing_token": fencing_token,
            "authority_digest": job.authority_digest,
        }
        marker_token = self._job_stage_token(job)
        existing_marker = self._read_job_marker(job.job_id)
        if existing_marker is not None and job.iteration_binding is not None:
            prior = existing_marker.get("iteration_binding") or {}
            if (existing_marker.get("phase") != "iteration_cleanup_verified"
                or existing_marker.get("cleanup_proven") is not True
                or existing_marker.get("status") != "iteration_failed_quiescent"
                or prior.get("repository_job_id") != job.job_id
                or prior.get("iteration_index") != job.iteration_binding.iteration_index - 1
                or prior.get("repository_attempt_id") != job.attempt_id
                or prior.get("repository_fence") != job.fencing_token
                or existing_marker.get("authority_digest") != prior.get("authority_digest")):
                raise RepoSandboxError("Original prior iteration cleanup is unproven", terminal_status="unknown_external_effect")
            existing_marker = None
        if existing_marker is not None:
            raise RepoSandboxError(
                "local job already has a durable marker; reconcile before retry",
                phase=str(existing_marker.get("phase") or "admitted"),
                terminal_status="unknown_external_effect",
            )
        if job.iteration_binding is not None:
            stage_binding["iteration_id"] = job.iteration_binding.iteration_id
        supervised_receipt = None
        phases = ["admitted"]
        marker_base = {
            "schema": "seraph.repo_repair_local_job.v1",
            "job_id": job.job_id,
            "authority_digest": job.authority_digest,
            "base_digest": job.base_digest,
            "executor_kind": "local",
            "profile": str(self.config.profile),
            "posture_digest": posture_digest,
            "attempt_id": attempt_id,
            "fencing_token": fencing_token,
            "stage_binding": stage_binding,
            "runtime_identity": {
                key: current_identity[key]
                for key in (
                    "worker_source_sha256",
                    "interpreter_entry_path",
                    "interpreter_path",
                    "interpreter_sha256",
                    "pytest_executable_path",
                    "pytest_executable_sha256",
                    "pytest_package_path",
                    "pytest_package_sha256",
                )
            },
            "created_at": time.time(),
            "deadline_at": deadline_wall,
        }
        if job.iteration_binding is not None:
            marker_base["iteration_binding"] = iteration_process_projection(job.iteration_binding)
        marker_state: dict[str, Any] = {**marker_base, "phase": "admitted", "status": "running"}
        self._write_job_marker(job.job_id, marker_state)
        with self._active_lock:
            self._active[job.job_id] = {"process": None, "cancelled": False}

        def mark_phase(value: str, **extra: Any) -> None:
            if value not in phases:
                phases.append(value)
            marker_state.update({"phase": value, "status": "running", **extra})
            self._write_job_marker(job.job_id, marker_state)

        def observe_process(process: subprocess.Popen[bytes] | None) -> None:
            with self._active_lock:
                state = self._active.get(job.job_id)
                if state is None:
                    return
                if process is None:
                    state["process"] = None
                else:
                    state["process"] = process
                    state["pid_start_identity"] = self._pid_start_identity(int(process.pid))
                    state["process_group_id"] = int(process.pid)
            current = self._read_job_marker(job.job_id)
            if current is None:
                return
            update = {
                **current,
                "phase": "worker_started" if process is not None else "worker_finished",
                "status": "running",
            }
            # A fresh cancellation adapter may have written the durable fence
            # between the child exit and this observer callback. Never let a
            # late ``process=None`` callback erase that fence and allow the
            # original worker to publish a failed/succeeded outcome.
            if current.get("status") == "cancellation_requested":
                update = {**current}
            if process is not None:
                update.update(
                    {
                        "pid": int(process.pid),
                        "pid_start_identity": self._pid_start_identity(int(process.pid)),
                        "process_group_id": int(process.pid),
                    }
                )
            self._write_job_marker(job.job_id, update)

        def before_spawn() -> None:
            _local_remaining(deadline_at, phase="worker_spawn")
            if self._local_runtime_identity() != current_identity:
                raise RepoSandboxError("local runtime identity changed before process spawn")
            with self._active_lock:
                state = self._active.get(job.job_id)
                if state is not None and state.get("cancelled"):
                    raise RepoSandboxError(
                        "local cancellation fence is set",
                        phase="worker_started",
                        terminal_status="unknown_external_effect",
                    )
            marker = self._read_job_marker(job.job_id)
            if marker is None or marker.get("status") == "cancellation_requested":
                raise RepoSandboxError(
                    "local durable cancellation fence is set",
                    phase="worker_started",
                    terminal_status="unknown_external_effect",
                )

        def durable_cancellation_requested() -> bool:
            """Return true only for the current job's verified cancel fence.

            The board cancellation path can run on a fresh adapter instance.
            In that case the in-memory ``_active`` flag is unavailable, while
            the original worker may still be unwinding after its process group
            was terminated.  Treat the durable marker as a cancellation fence
            only when every immutable job binding still matches this execution;
            a malformed or foreign marker must remain an unknown effect.
            """

            current = self._read_job_marker(job.job_id)
            if not isinstance(current, dict):
                return False
            return (
                current.get("job_id") == job.job_id
                and current.get("authority_digest") == job.authority_digest
                and str(current.get("attempt_id") or "") == attempt_id
                and type(current.get("fencing_token")) is int
                and current.get("fencing_token") == fencing_token
                and current.get("stage_binding") == stage_binding
                and current.get("phase") == "cancel_requested"
                and current.get("status") == "cancellation_requested"
            )

        def cancelled_result(*, reason: str, stdout: bytes = b"", stderr: bytes = b"") -> dict[str, Any]:
            """Close a durably fenced cancellation after child quiescence.

            Cancellation has no successful repair readback.  It may therefore
            publish only a bounded no-learning cancellation receipt after the
            exact private stage is removed; any stage identity or cleanup
            failure remains an unknown external effect.
            """

            if job.iteration_binding is not None and supervised_receipt is None:
                raise RepoSandboxError("Original iterative cancellation closure is unproven", terminal_status="unknown_external_effect")
            _local_remaining(deadline_at, phase="cancel_cleanup")
            self._assert_stage_identity(root, marker_state.get("stage_identity") or {})
            shutil.rmtree(root)
            if root.exists():
                raise RepoSandboxError(
                    "local cancellation cleanup is unproven",
                    phase="cleanup",
                    terminal_status="unknown_external_effect",
                )
            cancelled_manifest = {
                "schema": "seraph.repo_repair_execution.v1",
                "profile": str(self.config.profile),
                "executor_kind": "local",
                "status": "cancelled",
                "reason": reason,
                "job_id": job.job_id,
                "authority_digest": job.authority_digest,
                "attempt_id": attempt_id,
                "fencing_token": fencing_token,
                "posture_digest": posture_digest,
                "execution_identity": {
                    "schema": "seraph.repo_repair_execution_identity.v1",
                    "backend_kind": "local",
                    "job_id": job.job_id,
                    "authority_digest": job.authority_digest,
                    "profile": str(self.config.profile),
                    "executor_kind": "local",
                    "interpreter_entry_path": current_identity["interpreter_entry_path"],
                    "interpreter_path": current_identity["interpreter_path"],
                    "worker_source_sha256": current_identity["worker_source_sha256"],
                    "interpreter_sha256": current_identity["interpreter_sha256"],
                    "pytest_executable_path": current_identity["pytest_executable_path"],
                    "pytest_executable_sha256": current_identity["pytest_executable_sha256"],
                    "pytest_package_path": current_identity["pytest_package_path"],
                    "pytest_package_sha256": current_identity["pytest_package_sha256"],
                },
                "isolation_claim": "none",
                "network_isolation": "not_verified",
                "resource_enforcement": "admission_and_wall_timeout_only",
                "source_original_unchanged": True,
                "cleanup_proven": True,
                **({**supervised_receipt, "stage_removed": True} if job.iteration_binding is not None else {}),
            }
            encoded = json.dumps(cancelled_manifest, sort_keys=True).encode("utf-8") + b"\n"
            if "cleanup_verified" not in phases:
                phases.append("cleanup_verified")
            marker_state.update(
                {
                    "phase": "iteration_cleanup_verified" if job.iteration_binding is not None else "cleanup_verified",
                    "status": "cancelled",
                    "cleanup_proven": True,
                    "terminal_receipt": {
                        "status": "cancelled",
                        "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
                        "readback_sha256": hashlib.sha256(encoded).hexdigest(),
                        "attempt_id": attempt_id,
                        "fencing_token": fencing_token,
                        "stage_binding": dict(stage_binding),
                    },
                }
            )
            self._write_job_marker(job.job_id, marker_state)
            terminal = {
                "status": "cancelled",
                "reason": reason,
                "manifest": cancelled_manifest,
                "readback": dict(cancelled_manifest),
                "outputs": {
                    "manifest.json": encoded,
                    "readback.json": encoded,
                    "diff.patch": b"",
                    "pytest.stdout": stdout,
                    "pytest.stderr": stderr,
                },
                "effective_profile": dict(posture),
                "cleanup": {"status": "cleanup_verified", "cleanup_proven": True},
                "checkpoint_phases": phases,
                "learning": "no_learning",
                "operator_visible": True,
            }
            if job.iteration_binding is not None:
                self._owned_iteration_terminal[(job.job_id, job.iteration_binding.iteration_id)] = terminal
                terminal["iteration_cleanup_witness"] = self._iteration_cleanup_witness(job, terminal)
                terminal["cleanup"] = {"status": "iteration_cleanup_verified", "cleanup_proven": True}
            return terminal

        try:
            with self._job_staging_directory(marker_token) as root:
                root_metadata = root.stat(follow_symlinks=False)
                marker_state.update(
                    {
                        "stage_directory": str(root.relative_to(self.workspace_dir)),
                        "stage_identity": {"device": root_metadata.st_dev, "inode": root_metadata.st_ino},
                    }
                )
                self._write_job_marker(job.job_id, marker_state)
                input_root = root / "input"
                output_root = root / "out"
                source_snapshot_root = root / "source-snapshot"
                input_root.mkdir(mode=0o700)
                output_root.mkdir(mode=0o700)
                _local_remaining(deadline_at, phase="snapshot")
                snapshot = self.snapshot_repository(job.repository_root, source_snapshot_root)
                _local_remaining(deadline_at, phase="snapshot")
                if snapshot.digest != job.base_digest:
                    raise RepoSandboxError("repository base changed before local execution")
                if "snapshot_verified" not in phases:
                    phases.append("snapshot_verified")
                bundle = self.write_input_bundle(
                    snapshot=snapshot,
                    patch=job.patch_bytes,
                    job={
                        "job_id": job.job_id,
                        "authority_digest": job.authority_digest,
                        "base_digest": job.base_digest,
                        "patch_sha256": hashlib.sha256(job.patch_bytes).hexdigest(),
                        "allowed_paths": list(job.allowed_paths),
                        "patch_paths": list(patch_paths),
                        "test_args": list(normalized_args),
                        "wall_seconds": int(job.deadline_seconds),
                        "cpu_seconds": int(self.limits.max_cpu_seconds),
                        "worker_image_digest": "",
                        "limits_digest": limits_digest(self.limits),
                        "export_grace_seconds": 0,
                    },
                    staging_root=input_root,
                )
                for child in input_root.rglob("*"):
                    if child.is_dir():
                        child.chmod(0o700)
                    elif child.is_file():
                        child.chmod(0o600)
                mark_phase("input_loaded")
                environment = self._minimal_env(root)
                publication_runtime = None
                if str(self.config.profile) == "repo-python-pytest-publication-v1":
                    from src.execution.repo_publication_runtime import capture, materialize
                    captured = capture(deadline_at=deadline_at)
                    if captured["proof"] != current_identity["publication_runtime"]:
                        raise RepoSandboxError("publication runtime changed before materialization")
                    runtime_root = root / "python-runtime"
                    materialize(runtime_root, captured, deadline_at=deadline_at)
                    publication_runtime = {"root": runtime_root, "captured": captured, "configuration_revision": current_identity["publication_configuration_revision"]}
                from src.execution.repo_worker import run_local_job

                # The final owner/fence callback is immediately before the
                # first shared-worker child process. Snapshot and bundle
                # preparation above is inert and never contacts an executor.
                mark_phase("dispatch_fence_pending")
                if before_dispatch is not None:
                    before_dispatch()
                mark_phase("dispatch_authorized")

                if producer_owner is not None:
                    from src.execution.repo_original_producer import run_original_producer
                    from src.execution.repo_supervisor import PYTHON_PROFILE
                    before_spawn()
                    payload = {"profile": PYTHON_PROFILE, "stage": str(root), "runtime": current_identity,
                        "job_id": job.job_id, "iteration_binding": iteration_process_projection(job.iteration_binding),
                        "deadline_at": deadline_at, "token": marker_token, "environment": environment,
                        "supervisor_source_sha256": posture["supervisor_source_sha256"],
                        "git_sha256": posture["git_sha256"]}
                    result = run_original_producer(self, job, stage=root, payload=payload, posture=posture,
                        owner=producer_owner, observe_process=observe_process)
                    return self._finish_original_producer(job, result)

                try:
                    worker_runner = run_local_job
                    if job.iteration_binding is not None:
                        def worker_runner(*_args, **_kwargs):
                            nonlocal supervised_receipt
                            code, supervised_receipt = self._run_supervised_python(job, stage=root,
                                runtime=current_identity, environment=environment, deadline=deadline_at,
                                observe_process=observe_process, before_spawn=before_spawn, posture=posture)
                            return code
                    worker_exit = worker_runner(
                        bundle / "job.json",
                        workspace_root=root / "workspace",
                        output_root=output_root,
                        pytest_executable=pytest_executable,
                        environment=environment,
                        process_observer=observe_process,
                        before_spawn=before_spawn,
                        deadline_at=deadline_at,
                        expected_identity={
                            "worker_source_sha256": current_identity["worker_source_sha256"],
                            "interpreter_sha256": current_identity["interpreter_sha256"],
                            "pytest_executable_sha256": current_identity["pytest_executable_sha256"],
                            "pytest_package_sha256": current_identity["pytest_package_sha256"],
                        },
                        publication_runtime=publication_runtime,
                    )
                except Exception as exc:
                    # A fresh API/dispatcher instance may have set the exact
                    # durable cancellation fence while the worker was
                    # terminating.  The fixed worker deliberately reports a
                    # blocked/unknown result when nested-process cleanup was
                    # involved; once its process group is gone, close the
                    # fenced cancellation without inventing a repair
                    # readback.  If the direct process is still alive, retain
                    # the unknown outcome and the stage for reconciliation.
                    if job.iteration_binding is None and durable_cancellation_requested():
                        with self._active_lock:
                            active_state = self._active.get(job.job_id) or {}
                            active_process = active_state.get("process")
                        if active_process is None or active_process.poll() is not None:
                            return cancelled_result(
                                reason="cancellation_requested_before_terminal_readback",
                            )
                    raise RepoSandboxError(
                        "local worker effect or output publication is unknown",
                        phase="worker_started",
                        terminal_status="unknown_external_effect",
                    ) from exc
                with self._active_lock:
                    state = self._active.get(job.job_id)
                    cancelled = bool(state and state.get("cancelled"))
                cancelled = cancelled or durable_cancellation_requested()
                _local_remaining(deadline_at, phase="output_exported")

                def read_output(name: str) -> bytes:
                    return self._read_private_output(output_root, name)

                raw_manifest = json.loads(read_output("manifest.json").decode("utf-8"))
                raw_readback = json.loads(read_output("readback.json").decode("utf-8"))
                if (
                    isinstance(raw_manifest, dict)
                    and isinstance(raw_readback, dict)
                    and raw_manifest.get("status") == "blocked"
                ):
                    if not cancelled:
                        raise RepoSandboxError(
                            "local worker was blocked before terminal readback",
                            phase="output_exported",
                            terminal_status="unknown_external_effect",
                        )
                    cancelled_stdout = read_output("pytest.stdout")
                    cancelled_stderr = read_output("pytest.stderr")
                    return cancelled_result(
                        reason=str(raw_manifest.get("reason") or "cancellation_requested"),
                        stdout=cancelled_stdout,
                        stderr=cancelled_stderr,
                    )
                worker_outputs = {
                    name: read_output(name)
                    for name in ("manifest.json", "readback.json", "diff.patch", "pytest.stdout", "pytest.stderr")
                }
                raw_manifest, raw_readback, worker_failed = self._validate_worker_output(
                    outputs=worker_outputs,
                    job=job,
                    image="",
                    patch_paths=patch_paths,
                )
                if raw_manifest.get("backend_kind") != "local":
                    raise RepoSandboxError("local worker backend identity is invalid", phase="output_exported")
                expected_worker_identity = {
                    "schema": "seraph.repo_repair_execution_identity.v1",
                    "backend_kind": "local",
                    "profile": str(self.config.profile),
                    "job_id": job.job_id,
                    "authority_digest": job.authority_digest,
                    "worker_source_sha256": current_identity["worker_source_sha256"],
                    "interpreter_entry_path": current_identity["interpreter_entry_path"],
                    "interpreter_path": current_identity["interpreter_path"],
                    "interpreter_sha256": current_identity["interpreter_sha256"],
                    "pytest_executable_path": current_identity["pytest_executable_path"],
                    "pytest_executable_sha256": current_identity["pytest_executable_sha256"],
                    "pytest_package_path": current_identity["pytest_package_path"],
                    "pytest_package_sha256": current_identity["pytest_package_sha256"],
                }
                if raw_manifest.get("execution_identity") != expected_worker_identity:
                    raise RepoSandboxError("local worker executable identity is invalid", phase="output_exported")
                if publication_runtime is not None:
                    from src.execution.repo_publication_runtime import verify
                    verify(publication_runtime["root"], publication_runtime["captured"], deadline_at=deadline_at)
                    if self._local_runtime_identity() != current_identity:
                        raise RepoSandboxError("publication source runtime changed after quiescence", phase="output_exported")
                    attestation = raw_manifest.get("publication_test_input")
                    if attestation != raw_readback.get("publication_test_input") or not isinstance(attestation, dict) or attestation.get("environment", {}).get("runtime_proof") != current_identity["publication_runtime"] or attestation.get("environment_unchanged") is not True:
                        raise RepoSandboxError("publication tested runtime attestation is invalid", phase="output_exported")
                diff = worker_outputs["diff.patch"]
                try:
                    original_after = self.snapshot_repository(job.repository_root, root / "original-after")
                except RepoSandboxError as exc:
                    raise RepoSandboxError(
                        "local repair readback could not be verified",
                        phase="readback",
                        terminal_status="unknown_external_effect",
                    ) from exc
                _local_remaining(deadline_at, phase="readback")
                if original_after.digest != job.base_digest:
                    raise RepoSandboxError(
                        "local repair readback differs from the approved source",
                        phase="readback",
                        terminal_status="unknown_external_effect",
                        )
                with self._active_lock:
                    state = self._active.get(job.job_id)
                    cancelled = cancelled or bool(state and state.get("cancelled"))
                cancelled = cancelled or durable_cancellation_requested()
                status = "cancelled" if cancelled else ("failed" if worker_failed else "succeeded")
                local_manifest = {
                    **raw_manifest,
                    "schema": "seraph.repo_repair_execution.v1",
                    "executor_kind": "local",
                    "status": status,
                    "job_id": job.job_id,
                    "authority_digest": job.authority_digest,
                    "posture_digest": posture_digest,
                    "execution_identity": {
                        "job_id": job.job_id,
                        "authority_digest": job.authority_digest,
                        "profile": str(self.config.profile),
                        "executor_kind": "local",
                        **expected_worker_identity,
                    },
                    "isolation_claim": "none",
                    "network_isolation": "not_verified",
                    "resource_enforcement": "admission_and_wall_timeout_only",
                    "source_original_unchanged": True,
                    "cleanup_proven": False,
                    **(supervised_receipt or {}),
                }
                encoded_manifest = json.dumps(local_manifest, sort_keys=True).encode("utf-8") + b"\n"
                encoded_readback = json.dumps(local_manifest, sort_keys=True).encode("utf-8") + b"\n"
                from src.execution.repo_worker import _write_private_output

                _write_private_output(output_root, "manifest.json", encoded_manifest)
                _write_private_output(output_root, "readback.json", encoded_readback)
                phases.extend(("tests_finished", "output_exported", "readback_verified"))
                marker_state.update({"phase": "iteration_readback_verified" if job.iteration_binding is not None else "readback_verified", "status": status, "cleanup_proven": False})
                self._write_job_marker(job.job_id, marker_state)
                result = {
                    "status": status,
                    "failure_reason": "tests_failed" if status == "failed" else None,
                    "manifest": local_manifest,
                    "readback": dict(local_manifest),
                    "outputs": {
                        "manifest.json": encoded_manifest,
                        "readback.json": encoded_readback,
                        "diff.patch": diff,
                        "pytest.stdout": worker_outputs["pytest.stdout"],
                        "pytest.stderr": worker_outputs["pytest.stderr"],
                    },
                    "effective_profile": dict(posture),
                    "cleanup": {"status": "cleanup_pending", "cleanup_proven": False},
                    "checkpoint_phases": phases,
                    "learning": "no_learning",
                    "operator_visible": True,
                    **receipt_fields,
                }
                _local_remaining(deadline_at, phase="cleanup")
                self._assert_stage_identity(root, marker_state.get("stage_identity") or {})
                shutil.rmtree(root)
                if root.exists():
                    raise RepoSandboxError(
                        "local staging cleanup is unproven",
                        phase="cleanup",
                        terminal_status="unknown_external_effect",
                    )
                local_manifest = {**local_manifest, "cleanup_proven": True,
                    **({"stage_removed": True} if job.iteration_binding is not None else {})}
                encoded_manifest = json.dumps(local_manifest, sort_keys=True).encode("utf-8") + b"\n"
                encoded_readback = json.dumps(local_manifest, sort_keys=True).encode("utf-8") + b"\n"
                result["cleanup"] = {"status": "cleanup_verified", "cleanup_proven": True}
                result["manifest"] = local_manifest
                result["readback"] = dict(local_manifest)
                result["outputs"]["manifest.json"] = encoded_manifest
                result["outputs"]["readback.json"] = encoded_readback
                marker_state.update(
                    {
                        "phase": "iteration_cleanup_verified" if job.iteration_binding is not None else "cleanup_verified",
                        "status": "iteration_failed_quiescent" if job.iteration_binding is not None and status == "failed" else status,
                        "cleanup_proven": True,
                        "terminal_receipt": {
                            "status": status,
                            "manifest_sha256": hashlib.sha256(encoded_manifest).hexdigest(),
                            "readback_sha256": hashlib.sha256(encoded_readback).hexdigest(),
                            "attempt_id": attempt_id,
                            "fencing_token": fencing_token,
                            "stage_binding": dict(stage_binding),
                        },
                    }
                )
                self._write_job_marker(job.job_id, marker_state)
                if job.iteration_binding is not None:
                    self._owned_iteration_terminal[(job.job_id, job.iteration_binding.iteration_id)] = result
                    result["iteration_cleanup_witness"] = self._iteration_cleanup_witness(job, result)
                    result["cleanup"] = {"status": "iteration_cleanup_verified", "cleanup_proven": True}
                return result
        finally:
            marker = self._read_job_marker(job.job_id) if job.iteration_binding is not None else None
            if job.iteration_binding is None or (marker is not None and marker.get("phase") == "iteration_cleanup_verified" and marker.get("cleanup_proven") is True):
                with self._active_lock:
                    self._active.pop(job.job_id, None)

    def _cancel_iterative_supervisor(self, job_id: str, authority: Mapping[str, Any]) -> dict[str, Any]:
        from src.execution.repo_supervisor import exact_signal
        with self._job_marker_lock(job_id):
            marker = self._read_job_marker(job_id)
            if (marker is None or not marker.get("iteration_binding")
                or (authority.get("authority_digest") and authority["authority_digest"] != marker.get("authority_digest"))):
                return {"status": "unknown_external_effect", "cleanup_proven": False, "reason": "original_iterative_binding_missing"}
            if marker.get("phase") == "iteration_cleanup_verified":
                return {"status": "cancel_requested", "cleanup_proven": False, "reason": "original_closed_iteration_requires_terminal_owner"}
            marker = {**marker, "status": "cancellation_requested", "phase": "cancel_requested", "cancellation_requested": True}
            self._write_job_marker_locked(job_id, marker)
            pid, start = marker.get("pid"), marker.get("pid_start_identity")
            if type(pid) is not int or not start or not exact_signal(pid, start, signal.SIGTERM):
                return {"status": "unknown_external_effect", "cleanup_proven": False, "reason": "original_supervisor_signal_unproven"}
            return {"status": "cancel_requested", "cleanup_proven": False, "job_id": job_id}

    def cancel(self, *, job_id: str | None = None, authority: Mapping[str, Any] | None = None, **_kwargs: Any) -> dict[str, Any]:
        resolved_job_id = job_id or str((authority or {}).get("job_id") or "")
        if not resolved_job_id:
            return {"status": "unknown_external_effect", "reason": "local_job_identity_missing", "cleanup_proven": False}
        marker = self._read_job_marker(resolved_job_id)
        if marker is not None and marker.get("iteration_binding"):
            return self._cancel_iterative_supervisor(resolved_job_id, authority or {})
        expected_authority = str((authority or {}).get("authority_digest") or "")
        if expected_authority and (marker is None or marker.get("authority_digest") != expected_authority):
            return {
                "status": "unknown_external_effect",
                "reason": "local_authority_mismatch",
                "cleanup_proven": False,
                "job_id": resolved_job_id,
            }
        if marker is not None:
            supplied_attempt = (authority or {}).get("attempt_id")
            marker_attempt = str(marker.get("attempt_id") or "")
            if marker_attempt and marker_attempt != "legacy-attempt" and supplied_attempt is None:
                return {
                    "status": "unknown_external_effect",
                    "reason": "local_attempt_missing",
                    "cleanup_proven": False,
                    "job_id": resolved_job_id,
                }
            if supplied_attempt is not None and str(supplied_attempt) != str(marker.get("attempt_id") or ""):
                return {
                    "status": "unknown_external_effect",
                    "reason": "local_attempt_mismatch",
                    "cleanup_proven": False,
                    "job_id": resolved_job_id,
                }
            if "fencing_token" in (authority or {}):
                supplied_fence = (authority or {}).get("fencing_token")
                if type(supplied_fence) is not int or supplied_fence != marker.get("fencing_token"):
                    return {
                        "status": "unknown_external_effect",
                        "reason": "local_fencing_token_mismatch",
                        "cleanup_proven": False,
                        "job_id": resolved_job_id,
                    }
            elif type(marker.get("fencing_token")) is int and marker.get("fencing_token") != 0:
                return {
                    "status": "unknown_external_effect",
                    "reason": "local_fencing_token_missing",
                    "cleanup_proven": False,
                    "job_id": resolved_job_id,
                }
        with self._active_lock:
            state = self._active.get(resolved_job_id)
            process = state.get("process") if state is not None else None
            active_cancellation_fence = False
            marker_pid = 0
            if process is not None:
                if process.poll() is not None:
                    # The child may have exited while the parent is still
                    # validating its output. Keep the durable cancellation
                    # fence in force so terminal publication cannot race a
                    # valid cancel request. A fresh adapter with no active
                    # state still fails closed below.
                    if state is None or marker is None or marker.get("phase") not in {"worker_started", "cancel_requested"}:
                        return {"status": "unknown_external_effect", "reason": "local_process_not_found", "cleanup_proven": False}
                    state["cancelled"] = True
                    active_cancellation_fence = True
                    process = None
                else:
                    current_identity = self._pid_start_identity(int(process.pid))
                    if current_identity is None or current_identity != state.get("pid_start_identity"):
                        return {
                            "status": "unknown_external_effect",
                            "reason": "local_process_identity_changed",
                            "cleanup_proven": False,
                            "job_id": resolved_job_id,
                        }
                if process is not None:
                    state["cancelled"] = True
            elif state is not None:
                # Between child processes there is no PID to signal, but the
                # in-process execution still owns the job.  Record the fence
                # so the next before_spawn hook cannot start another child.
                # The worker will publish a cancelled terminal receipt after
                # it observes this flag and proves stage cleanup.
                state["cancelled"] = True
                active_cancellation_fence = True
            else:
                # A fresh backend instance can still cancel a process whose
                # private marker binds the exact PID/start token.  It cannot
                # claim cleanup or terminal success; reconcile remains the
                # only path that can close the unknown effect.
                try:
                    marker_pid = int(marker.get("pid")) if marker is not None else 0
                    marker_identity = str(marker.get("pid_start_identity") or "") if marker is not None else ""
                except (TypeError, ValueError):
                    marker_pid, marker_identity = 0, ""
                if marker is None or marker.get("phase") not in {"worker_started", "cancel_requested"}:
                    return {"status": "unknown_external_effect", "reason": "local_process_not_found", "cleanup_proven": False}
                if marker_pid <= 0 or not marker_identity:
                    return {"status": "unknown_external_effect", "reason": "local_process_not_found", "cleanup_proven": False}
                current_identity = self._pid_start_identity(marker_pid)
                if current_identity is None:
                    # The direct worker may have exited while its owning
                    # thread is still unwinding a blocked/unknown result. A
                    # fresh adapter cannot signal a gone PID, but it can set
                    # the exact durable cancellation fence so that the
                    # original owner performs stage cleanup. If the PID is
                    # still present but /proc identity is unavailable, fail
                    # closed rather than treating an unverifiable process as
                    # this job.
                    try:
                        os.kill(marker_pid, 0)
                    except ProcessLookupError:
                        active_cancellation_fence = True
                        process = None
                    except OSError:
                        return {
                            "status": "unknown_external_effect",
                            "reason": "local_process_identity_unavailable",
                            "cleanup_proven": False,
                            "job_id": resolved_job_id,
                        }
                    else:
                        return {
                            "status": "unknown_external_effect",
                            "reason": "local_process_identity_unavailable",
                            "cleanup_proven": False,
                            "job_id": resolved_job_id,
                        }
                elif current_identity != marker_identity:
                    return {"status": "unknown_external_effect", "reason": "local_process_identity_changed", "cleanup_proven": False}
                else:
                    process = None
        # Persist exact cancellation intent before a signal can make the
        # original observer publish its terminal result. A fresh adapter has
        # no shared in-memory flag; signalling first loses that fence race.
        if marker is None:
            return {"status": "unknown_external_effect", "reason": "local_cancellation_intent_unavailable", "cleanup_proven": False, "job_id": resolved_job_id}
        try:
            terminal_cancelled = self._write_job_marker(
                resolved_job_id,
                {**marker, "phase": "cancel_requested", "status": "cancellation_requested"},
            )
        except (OSError, ValueError, RepoSandboxError):
            return {"status": "unknown_external_effect", "reason": "local_cancellation_intent_unavailable", "cleanup_proven": False, "job_id": resolved_job_id}
        if terminal_cancelled or active_cancellation_fence:
            return {"status": "cancel_requested", "cleanup_proven": False, "job_id": resolved_job_id}
        try:
            pid = int(process.pid) if process is not None else marker_pid
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            try:
                if process is not None:
                    process.kill()
                else:
                    os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                # The process can disappear between the identity check and
                # the signal (the native API path exercises this race). If
                # /proc now proves that the exact bound PID is gone, record
                # the durable fence anyway so the original executor thread
                # can finish verified stage cleanup. A replaced PID or an
                # inaccessible process remains unknown and is never adopted.
                expected_pid_identity = str((marker or {}).get("pid_start_identity") or "")
                current_pid_identity = self._pid_start_identity(pid)
                if expected_pid_identity and current_pid_identity is not None:
                    return {
                        "status": "unknown_external_effect",
                        "reason": "local_process_identity_changed",
                        "cleanup_proven": False,
                        "job_id": resolved_job_id,
                    }
                if current_pid_identity is None:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        active_cancellation_fence = True
                    except OSError:
                        return {
                            "status": "unknown_external_effect",
                            "reason": "local_process_cleanup_unproven",
                            "cleanup_proven": False,
                            "job_id": resolved_job_id,
                        }
                    else:
                        return {
                            "status": "unknown_external_effect",
                            "reason": "local_process_identity_unavailable",
                            "cleanup_proven": False,
                            "job_id": resolved_job_id,
                        }
                elif (
                    expected_pid_identity
                    and current_pid_identity == expected_pid_identity
                    and self._pid_state(pid) in {"Z", "X"}
                ):
                    # The exact worker PID is an unreaped zombie.  Its start
                    # identity still matches the durable marker, while the
                    # failed process-group signal proves there is no live
                    # group to terminate. Fence cancellation so the original
                    # owner can finish bounded stage cleanup. A replaced or
                    # live PID remains unknown above.
                    active_cancellation_fence = True
                else:
                    return {
                        "status": "unknown_external_effect",
                        "reason": "local_process_cleanup_unproven",
                        "cleanup_proven": False,
                        "job_id": resolved_job_id,
                    }
        return {"status": "cancel_requested", "cleanup_proven": False, "job_id": resolved_job_id}

    def reconcile(self, authority: Mapping[str, Any] | None = None) -> dict[str, Any]:
        job_id = str((authority or {}).get("job_id") or "")
        marker = self._read_job_marker(job_id) if job_id else None
        if marker is not None and marker.get("iteration_binding"):
            return {"status": "unknown_external_effect", "reason": "original_iteration_source_witness_required", "cleanup_proven": False, "learning": "no_learning"}
        supplied_authority = authority or {}
        if marker is not None and str(supplied_authority.get("authority_digest") or "") not in {
            "",
            marker.get("authority_digest"),
        }:
            return {
                "status": "unknown_external_effect",
                "reason": "local_authority_mismatch",
                "cleanup_proven": False,
                "learning": "no_learning",
            }
        if marker is not None:
            supplied_attempt = supplied_authority.get("attempt_id")
            marker_attempt = str(marker.get("attempt_id") or "")
            if marker_attempt and marker_attempt != "legacy-attempt" and supplied_attempt is None:
                return {
                    "status": "unknown_external_effect",
                    "reason": "local_attempt_missing",
                    "cleanup_proven": False,
                    "learning": "no_learning",
                }
            if supplied_attempt is not None and str(supplied_attempt) != str(marker.get("attempt_id") or ""):
                return {
                    "status": "unknown_external_effect",
                    "reason": "local_attempt_mismatch",
                    "cleanup_proven": False,
                    "learning": "no_learning",
                }
            if "fencing_token" in supplied_authority:
                if type(supplied_authority["fencing_token"]) is not int or supplied_authority["fencing_token"] != marker.get("fencing_token"):
                    return {
                        "status": "unknown_external_effect",
                        "reason": "local_fencing_token_mismatch",
                        "cleanup_proven": False,
                        "learning": "no_learning",
                    }
            elif type(marker.get("fencing_token")) is int and marker.get("fencing_token") != 0:
                return {
                    "status": "unknown_external_effect",
                    "reason": "local_fencing_token_missing",
                    "cleanup_proven": False,
                    "learning": "no_learning",
                }
            binding = marker.get("stage_binding")
            if isinstance(binding, dict):
                expected_binding = {
                    "executor_kind": "local",
                    "job_id": job_id,
                    "attempt_id": str(marker.get("attempt_id") or "legacy-attempt"),
                    "fencing_token": int(marker.get("fencing_token") or 0),
                    "authority_digest": str(marker.get("authority_digest") or ""),
                }
                if binding != expected_binding:
                    return {
                        "status": "unknown_external_effect",
                        "reason": "local_stage_binding_mismatch",
                        "cleanup_proven": False,
                        "learning": "no_learning",
                    }
            if marker.get("cleanup_proven") is True and marker.get("phase") == "cleanup_verified":
                terminal = marker.get("terminal_receipt")
                stage_directory = marker.get("stage_directory")
                stage_absent = False
                if isinstance(stage_directory, str) and stage_directory:
                    try:
                        stage_path = (self.workspace_dir / stage_directory).absolute()
                        stage_path.relative_to(self.workspace_dir)
                        stage_absent = not stage_path.exists() and not stage_path.is_symlink()
                    except (OSError, ValueError):
                        stage_absent = False
                if isinstance(terminal, dict) and stage_absent and terminal.get("stage_binding") == binding:
                    # The marker is written before the API publishes the
                    # manifest/readback bytes as canonical artifacts.  A
                    # process death after staging cleanup therefore proves
                    # cleanup and identity, but it cannot prove that a
                    # successful output remains recoverable.  Never turn a
                    # digest-only marker into success; the API must reconcile
                    # its own canonical readback or retry the same authority.
                    terminal_status = str(terminal.get("status") or marker.get("status") or "unknown_external_effect")
                    if terminal_status == "succeeded" and terminal.get("canonical_outputs_verified") is not True:
                        return {
                            "status": "unknown_external_effect",
                            "reason": "local_terminal_outputs_not_recoverable",
                            "cleanup_proven": True,
                            "receipt": {
                                "manifest_sha256": terminal.get("manifest_sha256"),
                                "readback_sha256": terminal.get("readback_sha256"),
                                "attempt_id": marker.get("attempt_id"),
                                "fencing_token": marker.get("fencing_token"),
                                "stage_binding": dict(binding) if isinstance(binding, dict) else None,
                            },
                            "marker": {
                                "phase": marker.get("phase"),
                                "status": marker.get("status"),
                                "attempt_id": marker.get("attempt_id"),
                                "fencing_token": marker.get("fencing_token"),
                            },
                            "learning": "no_learning",
                        }
                    return {
                        "status": terminal_status,
                        "cleanup_proven": True,
                        "receipt": {
                            "manifest_sha256": terminal.get("manifest_sha256"),
                            "readback_sha256": terminal.get("readback_sha256"),
                            "attempt_id": marker.get("attempt_id"),
                            "fencing_token": marker.get("fencing_token"),
                            "stage_binding": dict(binding) if isinstance(binding, dict) else None,
                        },
                        "marker": {
                            "phase": marker.get("phase"),
                            "status": marker.get("status"),
                            "attempt_id": marker.get("attempt_id"),
                            "fencing_token": marker.get("fencing_token"),
                        },
                        "learning": "no_learning",
                    }
        return {
            "status": "unknown_external_effect",
            "reason": "local_process_reconciliation_requires_durable_receipt",
            "operator_action": "reconcile_or_create_fresh_preview",
            "cleanup_proven": False,
            "marker": {
                "phase": marker.get("phase"),
                "status": marker.get("status"),
                "pid": marker.get("pid"),
                "pid_start_identity": marker.get("pid_start_identity"),
            } if marker is not None else None,
            "learning": "no_learning",
        }


def build_repo_repair_executor(config: RepoSandboxSettings | None = None) -> RepoRepairExecutor:
    """Build the server-selected executor without fallback."""

    value = config or _effective_repo_sandbox_settings()
    if value.profile == "repo-node24-npm-v1":
        from src.execution.repo_node import NodeRepoRepairExecutor

        return NodeRepoRepairExecutor(config=value)
    kind = str(value.executor_kind)
    if kind == "local":
        return LocalRepoRepairExecutor(config=value)
    if kind == "docker_rootful":
        return RootfulDockerRepoSandbox(config=value)
    if kind == "docker_rootless":
        return RootlessDockerRepoSandbox(config=value)
    raise RepoSandboxError("unsupported repository executor kind")


repo_repair_executor_for_settings = build_repo_repair_executor
