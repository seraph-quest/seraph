"""Trusted rootless-Docker runner for the bounded repository capability.

There is intentionally no public command API here.  Callers provide a typed
job contract; this module validates it and builds a fixed Docker argv against a
configured local Unix socket.  Missing prerequisites fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlparse

from config.settings import RepoSandboxSettings, settings


PROFILE = "repo-python-pytest-v1"
IMAGE_DIGEST_RE = r"^[^@/\s]+(?:/[^@\s]+)+@sha256:[0-9a-f]{64}$"
DEFAULT_TIMEOUT_SECONDS = 30
MAX_NAME_BYTES = 96


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

    def as_receipt(self) -> dict[str, Any]:
        return {
            "profile": PROFILE,
            "status": self.status,
            "ok": self.ok,
            "reason": self.reason,
            "rootless": self.info.get("rootless"),
            "image_digest": self.image.get("digest"),
            "operator_visible": True,
        }


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

    def __init__(
        self,
        config: RepoSandboxSettings | None = None,
        *,
        docker_binary: str = "docker",
        popen: Callable[..., subprocess.Popen[bytes]] | None = None,
    ) -> None:
        self.config = config or settings.repo_sandbox
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

    def _run_docker(self, args: list[str], *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> tuple[int, bytes, bytes]:
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
                process.communicate(timeout=5)
                raise RepoSandboxError("Docker command timed out") from exc
        return int(process.returncode or 0), stdout or b"", stderr or b""

    def _wait_for_export_ready(self, container_name: str, *, timeout: float) -> None:
        """Observe the worker's durable export barrier before cleanup.

        The worker keeps its output tmpfs alive after writing the bounded
        result.  The runner must observe the explicit marker while the
        container is still running, then copy the result before asking Docker
        to wait/stop it.  A post-exit copy is not sufficient proof that the
        worker completed the hand-off protocol.
        """
        deadline = time.monotonic() + max(1.0, float(timeout))
        while time.monotonic() < deadline:
            code, stdout, stderr = self._run_docker(
                ["logs", "--tail=64", container_name], timeout=5
            )
            if b"SERAPH_EXPORT_READY" in stdout or b"SERAPH_EXPORT_READY" in stderr:
                return
            if code != 0 and b"No such object" in stderr:
                raise RepoSandboxError("output_lost", phase="output_exported", terminal_status="failed")
            time.sleep(0.1)
        raise RepoSandboxError("output_lost", phase="output_exported", terminal_status="failed")

    def _run_docker_stream(
        self,
        args: list[str],
        *,
        stdin: bytes | None = None,
        timeout: float = 30,
        max_output_bytes: int | None = None,
    ) -> bytes:
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
                process.communicate(timeout=5)
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
    ) -> tuple[int, bytes, bytes]:
        """Run one server-built Docker argv without ever invoking a shell."""
        if not argv or argv[0] != self.docker_binary:
            raise RepoSandboxError("Docker argv was not built by the trusted runner")
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
                process.communicate(timeout=5)
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

    def _read_input_file(self, *, worker_name: str, relative_path: str) -> bytes:
        """Read one fixed input file through Docker's bounded tar stream."""
        relative = _safe_relative_path(relative_path)
        payload = self._run_docker_stream(
            ["cp", f"{worker_name}:/input/{relative}", "-"],
            timeout=30,
            max_output_bytes=self.limits.max_output_bytes,
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
    ) -> None:
        """Rebind recovery to the exact approved input volume contract."""
        raw_job = self._read_input_file(worker_name=worker_name, relative_path="job.json")
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
        input_patch = self._read_input_file(worker_name=worker_name, relative_path="patch.diff")
        if input_patch != job.patch_bytes:
            raise RepoSandboxError("recovered input patch does not match approval", phase="worker_started")
        snapshot_manifest = self._read_input_file(worker_name=worker_name, relative_path="snapshot-manifest.json")
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
            "profile": PROFILE,
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
            code, _stdout, _stderr = self._run_docker(args, timeout=10)
            receipts.append({"operation": args[0], "status": "ok" if code == 0 else "not_needed_or_failed"})
        code, _stdout, stderr = self._run_docker(["inspect", container_name], timeout=10)
        container_removed = code != 0 and b"No such object" in stderr
        if remove_volume:
            code, _stdout, stderr = self._run_docker(["volume", "inspect", input_volume], timeout=10)
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
            preflight = self.preflight()
            if not preflight.ok:
                return {
                    "status": "blocked",
                    "reason": preflight.reason,
                    "preflight": preflight.as_receipt(),
                    "checkpoint_phases": checkpoint_phases,
                    "learning": "no_learning",
                }
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
                code, _stdout, stderr = self._run_docker(["volume", "create", input_volume], timeout=10)
                if code != 0:
                    raise RepoSandboxError(f"input volume creation failed: {stderr[-512:].decode(errors='replace')}")
                volume_created = True

                code, _stdout, stderr = self._run_full_argv(
                    self.build_loader_argv(name=loader_name, input_volume=input_volume), timeout=10
                )
                if code != 0:
                    raise RepoSandboxError(f"loader creation failed: {stderr[-512:].decode(errors='replace')}")
                loader_created = True
                code, _stdout, stderr = self._run_docker(["start", loader_name], timeout=10)
                if code != 0:
                    raise RepoSandboxError("loader start failed")
                self._run_docker_stream(["cp", "-", f"{loader_name}:/input/"], stdin=transfer, timeout=30)
                readback = self._run_docker_stream(
                    ["cp", f"{loader_name}:/input/.", "-"],
                    timeout=30,
                    max_output_bytes=self.limits.max_snapshot_bytes,
                )
                self._verify_bundle_export(readback, bundle)
                mark_phase("input_loaded")
                loader_cleanup = self._cleanup_container_and_volume(
                    container_name=loader_name,
                    input_volume=input_volume,
                    remove_volume=False,
                )
                cleanup_receipts.extend(loader_cleanup.get("receipts", []))
                if loader_cleanup.get("status") == "unknown_external_effect":
                    raise RepoSandboxError("loader cleanup could not be proven")
                loader_created = False

                code, _stdout, stderr = self._run_full_argv(
                    self.build_worker_argv(name=worker_name, input_volume=input_volume), timeout=10
                )
                if code != 0:
                    raise RepoSandboxError(f"worker creation failed: {stderr[-512:].decode(errors='replace')}")
                worker_created = True
                code, _stdout, stderr = self._run_docker(["start", worker_name], timeout=10)
                if code != 0:
                    raise RepoSandboxError("worker start failed")
                inspect_code, inspect_stdout, inspect_stderr = self._run_docker(
                    ["inspect", "--format", "{{json .}}", worker_name], timeout=10
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
                    )
                    output_exported = True
                    output_tar = self._run_docker_stream(["cp", f"{worker_name}:/out/.", "-"], timeout=30)
                    expected_output = {"manifest.json", "readback.json", "diff.patch", "pytest.stdout", "pytest.stderr"}
                    self.validate_export(output_tar, expected_files=expected_output)
                    mark_phase("output_exported")
                    code, wait_stdout, wait_stderr = self._run_docker(
                        ["wait", worker_name], timeout=int(job.deadline_seconds) + 40
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
                )
                if cleanup.get("status") == "unknown_external_effect":
                    return {
                        "status": "unknown_external_effect",
                        "reason": "cleanup_unproven",
                        "cleanup": cleanup,
                        "checkpoint_phases": checkpoint_phases,
                        "learning": "no_learning",
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

    def preflight(self) -> RepoSandboxPreflight:
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
            code, stdout, stderr = self._run_docker(["info", "--format", "{{json .}}"])
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
        try:
            code, stdout, stderr = self._run_docker(["image", "inspect", "--format", "{{json .}}", image])
        except RepoSandboxError as exc:
            return RepoSandboxPreflight(False, "blocked", f"pinned_image_unavailable:{exc}", info={"rootless": True})
        if code != 0:
            return RepoSandboxPreflight(False, "blocked", "pinned_image_unavailable", info={"rootless": True})
        try:
            image_info = self._json_output(stdout, operation="image inspect")
        except RepoSandboxError as exc:
            return RepoSandboxPreflight(False, "blocked", str(exc), info={"rootless": True})
        repo_digests = image_info.get("RepoDigests") or []
        if image not in repo_digests and str(image_info.get("Id") or "") != f"sha256:{image.rsplit(':', 1)[-1]}":
            return RepoSandboxPreflight(False, "blocked", "pinned_image_digest_mismatch", info={"rootless": True}, image=image_info)
        return RepoSandboxPreflight(True, "ready", info={"rootless": True, "server_rootless": True}, image={"digest": image, **image_info})

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
        security_opt = {str(item).lower() for item in (host.get("SecurityOpt") or [])}
        if "no-new-privileges" not in security_opt:
            raise RepoSandboxError("worker no-new-privileges is not enabled")
        binds = host.get("Binds") or []
        if binds:
            raise RepoSandboxError("worker has an unapproved bind mount")
        mounts = inspected.get("Mounts") if isinstance(inspected, Mapping) else []
        mounts = mounts if isinstance(mounts, list) else []
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

    def snapshot_repository(self, repository_path: str | Path, staging_root: str | Path) -> RepositorySnapshot:
        source = self.validate_snapshot_root(repository_path)
        destination = Path(staging_root).resolve()
        if destination.exists():
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
        destination = Path(staging_root).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        patch_path = destination / "patch.diff"
        patch_path.write_bytes(patch)
        manifest_path = destination / "snapshot-manifest.json"
        manifest_path.write_text(json.dumps(snapshot.manifest(), sort_keys=True), encoding="utf-8")
        job_payload = dict(job)
        job_payload.update({"profile": PROFILE, "snapshot_digest": snapshot.digest})
        (destination / "job.json").write_text(json.dumps(job_payload, sort_keys=True), encoding="utf-8")
        snapshot_target = destination / "snapshot"
        if snapshot_target.exists():
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
        def validation_failure(exc: BaseException, *, phase: str = "admitted") -> dict[str, Any]:
            try:
                cleanup = self.cancel(
                    container_name=derived_worker_name,
                    additional_container_names=(derived_loader_name,),
                    input_volume=derived_input_volume,
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
            }
        try:
            if int(job.deadline_seconds) < 30 or int(job.deadline_seconds) > self.limits.max_wall_seconds:
                raise RepoSandboxError("job deadline is outside the fixed profile")
            configured_image = self.validate_image_digest(self.config.worker_image_digest)
            image = self.validate_image_digest(job.worker_image_digest or configured_image)
            if image != configured_image:
                raise RepoSandboxError("approved worker image no longer matches configured image")
            if job.limits_digest and job.limits_digest != limits_digest(self.limits):
                raise RepoSandboxError("approved worker limits no longer match configured limits")
            preflight = self.preflight()
            if not preflight.ok:
                return validation_failure(RepoSandboxError(preflight.reason), phase="admitted")
        except (RepoSandboxError, OSError, ValueError) as exc:
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
        code, stdout, stderr = self._run_docker(
            ["inspect", "--format", "{{json .}}", worker_name], timeout=10
        )
        if code != 0:
            try:
                cleanup = self.cancel(
                    container_name=worker_name,
                    additional_container_names=(f"{token}-loader",),
                    input_volume=input_volume,
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
            )
            phases.append("input_loaded")
            try:
                self._wait_for_export_ready(
                    worker_name,
                    timeout=min(max(1, int(wait_seconds)), int(job.deadline_seconds)),
                )
                output_tar = self._run_docker_stream(
                    ["cp", f"{worker_name}:/out/.", "-"],
                    timeout=30,
                    max_output_bytes=self.limits.max_output_bytes,
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
                )
            except (OSError, RepoSandboxError, ValueError) as cleanup_exc:
                return {
                    "status": "unknown_external_effect",
                    "reason": "cleanup_unproven",
                    "cleanup": {"status": "unknown_external_effect", "reason": "recovery_cleanup_unavailable", "error": str(cleanup_exc)},
                    "checkpoint_phases": phases,
                    "learning": "no_learning",
                }
            if cleanup.get("status") == "unknown_external_effect":
                return {
                    "status": "unknown_external_effect",
                    "reason": "cleanup_unproven",
                    "cleanup": cleanup,
                    "checkpoint_phases": phases,
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
                code, stdout, stderr = self._run_docker(args, timeout=10)
                receipts.append({"operation": args[0], "target": name, "status": "ok" if code == 0 else "failed"})
        code, stdout, stderr = self._run_docker(["volume", "rm", input_volume], timeout=10)
        receipts.append({"operation": "volume_rm", "target": input_volume, "status": "ok" if code == 0 else "failed"})
        if output_volume:
            code, stdout, stderr = self._run_docker(["volume", "rm", output_volume], timeout=10)
            receipts.append({"operation": "volume_rm_output", "target": output_volume, "status": "ok" if code == 0 else "failed"})
        container_checks: list[bool] = []
        for name in container_names:
            code, _, error = self._run_docker(["inspect", name], timeout=10)
            container_checks.append(code != 0 and b"No such object" in error)
        volume_names = [input_volume] + ([output_volume] if output_volume else [])
        volume_checks: list[bool] = []
        for volume_name in volume_names:
            volume_code, _, volume_error = self._run_docker(["volume", "inspect", volume_name], timeout=10)
            volume_checks.append(volume_code != 0 and b"No such volume" in volume_error)
        proven_removed = all(container_checks) and all(volume_checks)
        if not proven_removed:
            return {"status": "unknown_external_effect", "reason": "cleanup_unproven", "receipts": receipts}
        return {"status": "cancelled", "cleanup_proven": True, "volumes_removed": True, "receipts": receipts}
