"""Fixed worker entrypoint for the ``repo-python-pytest-v1`` image.

This module deliberately has no generic command or network interface.  The
trusted backend writes one bounded job.json, a patch, and a repository snapshot
to the read-only input volume.  The container entrypoint accepts only that
fixed file or the loader's inert transfer mode.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import time
from typing import Any, Callable, Iterable, Mapping


PROFILE = "repo-python-pytest-v1"
MAX_FILES = 2_000
MAX_DIRECTORIES = 500
MAX_DEPTH = 16
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_PATCH_BYTES = 1 * 1024 * 1024
MAX_JOB_BYTES = 2 * MAX_PATCH_BYTES
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_STREAM_BYTES = 1 * 1024 * 1024
MAX_WALL_SECONDS = 180
MAX_ALLOWED_PATHS = 64
MAX_ALLOWED_PATH_BYTES = 4096

# The local executor invokes the trusted interpreter with ``-I`` and imports
# pytest before adding the staged checkout to sys.path.  This prevents a
# staged ``pytest.py``/``pytest`` package from replacing the server-owned test
# runner while still allowing the tests to import their staged project.
_LOCAL_PYTEST_BOOTSTRAP = (
    "import os,sys; import pytest; sys.path.insert(0, os.getcwd()); "
    "raise SystemExit(pytest.main(sys.argv[1:]))"
)


class WorkerInputError(ValueError):
    """Input was not part of the fixed worker contract."""


def _proc_start_identity(pid: int) -> str | None:
    """Return Linux's process-start token for PID-reuse protection."""

    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
        fields = raw.rsplit(")", 1)[1].split()
        return fields[19] if len(fields) > 19 else None
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _process_group_members(pgid: int) -> dict[int, str | None]:
    """Read the current members of a server-created process group."""

    members: dict[int, str | None] = {}
    proc_root = Path("/proc")
    try:
        entries = tuple(proc_root.iterdir())
    except OSError as exc:
        raise WorkerInputError("process-group inspection is unavailable") from exc
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            raw = (entry / "stat").read_text(encoding="utf-8")
            fields = raw.rsplit(")", 1)[1].split()
            # After the command name: state, ppid, pgrp, session.
            if len(fields) < 4 or int(fields[2]) != int(pgid):
                continue
            members[pid] = fields[19] if len(fields) > 19 else None
        except (OSError, UnicodeDecodeError, ValueError, IndexError):
            # A process can disappear between /proc enumeration and stat read.
            # Re-read on the next bounded pass; an unreadable live member is
            # conservatively treated as quiescence failure by the caller.
            continue
    return members


def _wait_process_group_quiescent(pgid: int, *, deadline_at: float | None) -> None:
    """Require every ordinary child in the fixed process group to exit."""

    while True:
        members = _process_group_members(pgid)
        if not members:
            return
        if deadline_at is None:
            time.sleep(0.01)
            continue
        remaining = float(deadline_at) - time.monotonic()
        if remaining <= 0:
            raise WorkerInputError("worker process-group cleanup exceeded the wall deadline")
        time.sleep(min(0.01, remaining))


def _terminate_and_reap_process(
    process: subprocess.Popen[bytes],
    *,
    deadline_at: float | None,
) -> None:
    """Kill and prove quiescence without adding a fixed post-deadline wait."""

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    remaining = 0.25 if deadline_at is None else max(0.0, min(0.25, float(deadline_at) - time.monotonic()))
    try:
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        raise WorkerInputError("worker process cleanup is unproven") from exc
    _wait_process_group_quiescent(process.pid, deadline_at=deadline_at)


def _reject_nested_process_group(
    process: subprocess.Popen[bytes],
    *,
    deadline_at: float | None,
) -> None:
    """Kill ordinary descendants after the direct worker exits.

    A successful direct exit is insufficient: a test can leave a child in the
    same process group holding stage files or output pipes.  Kill that group
    immediately and preserve an unknown outcome if quiescence cannot be
    proven within the original deadline.
    """

    if not _process_group_members(process.pid):
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _wait_process_group_quiescent(process.pid, deadline_at=deadline_at)
    raise WorkerInputError("worker left a nested process running")


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
        raise WorkerInputError("snapshot directory changed or contains a symlink") from exc


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISREG(left.st_mode)
        and stat.S_ISREG(right.st_mode)
        and left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
    )


def _pytest_package_identity() -> tuple[str, str]:
    """Resolve the server-owned pytest package without staged imports."""

    spec = importlib.util.find_spec("pytest")
    origin = str(spec.origin or "") if spec is not None else ""
    if not origin or origin in {"built-in", "frozen"}:
        raise WorkerInputError("trusted pytest package is unavailable")
    package_path = Path(origin).resolve(strict=True)
    metadata = package_path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise WorkerInputError("trusted pytest package is not a regular file")
    return str(package_path), hashlib.sha256(package_path.read_bytes()).hexdigest()


def _same_file_metadata(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        _same_file_identity(left, right)
        and left.st_nlink == right.st_nlink
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )


def _open_source_regular_file(
    root: Path,
    relative: str,
    *,
    expected_stat: os.stat_result | None = None,
) -> tuple[int, os.stat_result]:
    """Open one snapshot file through descriptor-relative no-follow traversal."""

    relative_path = PurePosixPath(str(relative))
    if relative_path.is_absolute() or not relative_path.parts or ".." in relative_path.parts:
        raise WorkerInputError("snapshot source path is invalid")
    parent_fd = _open_directory_descriptor(root)
    descriptor = -1
    try:
        for component in relative_path.parts[:-1]:
            next_fd = os.open(component, _descriptor_flags(directory=True), dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        descriptor = os.open(relative_path.parts[-1], _descriptor_flags(), dir_fd=parent_fd)
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode) or opened_stat.st_nlink != 1:
            os.close(descriptor)
            descriptor = -1
            raise WorkerInputError("snapshot source is not a single-link regular file")
        if expected_stat is not None and not _same_file_metadata(expected_stat, opened_stat):
            os.close(descriptor)
            descriptor = -1
            raise WorkerInputError("snapshot source identity changed before read")
        return descriptor, opened_stat
    except WorkerInputError:
        raise
    except OSError as exc:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise WorkerInputError("snapshot source descriptor could not be opened") from exc
    finally:
        try:
            os.close(parent_fd)
        except OSError:
            pass


def _read_bounded_job_json(job_file: Path) -> dict[str, Any]:
    """Read the fixed job descriptor through a bounded, no-follow descriptor."""
    descriptor, _job_stat = _open_source_regular_file(job_file.parent, job_file.name)
    try:
        with os.fdopen(descriptor, "rb") as job_handle:
            payload = job_handle.read(MAX_JOB_BYTES + 1)
    except OSError as exc:
        raise WorkerInputError("job input could not be read") from exc
    if len(payload) > MAX_JOB_BYTES:
        raise WorkerInputError("job input exceeds the fixed input limit")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerInputError("job input is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise WorkerInputError("job input must be an object")
    return value


def _assert_stable_file(initial: os.stat_result, final: os.stat_result) -> None:
    if not _same_file_metadata(initial, final) or final.st_nlink != 1:
        raise WorkerInputError("snapshot source changed during read")


def _safe_relative(value: object) -> str:
    text = str(value or "")
    if not text or "\x00" in text:
        raise WorkerInputError("empty or NUL path")
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        raise WorkerInputError("path traversal is blocked")
    normalized = path.as_posix()
    if normalized in {"", "."} or normalized.startswith("/"):
        raise WorkerInputError("invalid relative path")
    return normalized


def _walk_tree(root: Path) -> list[tuple[str, Path, bool, os.stat_result]]:
    """Return regular files/directories, refusing links and special files."""
    root = root.resolve(strict=True)
    entries: list[tuple[str, Path, bool, os.stat_result]] = []
    file_count = 0
    directory_count = 0
    total_bytes = 0
    stack: list[tuple[Path, str, int]] = [(root, "", 0)]
    seen: set[str] = set()
    while stack:
        current, prefix, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise WorkerInputError("snapshot depth limit exceeded")
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name, reverse=True)
        except OSError as exc:
            raise WorkerInputError(f"snapshot read failed: {exc}") from exc
        for child in children:
            relative = _safe_relative(f"{prefix}/{child.name}" if prefix else child.name)
            if relative in seen:
                raise WorkerInputError("duplicate snapshot path")
            seen.add(relative)
            try:
                file_stat = child.lstat()
            except OSError as exc:
                raise WorkerInputError(f"snapshot stat failed: {exc}") from exc
            if not (stat.S_ISREG(file_stat.st_mode) or stat.S_ISDIR(file_stat.st_mode)):
                raise WorkerInputError(f"unsupported snapshot entry: {relative}")
            if stat.S_ISDIR(file_stat.st_mode):
                directory_count += 1
                if directory_count > MAX_DIRECTORIES:
                    raise WorkerInputError("snapshot directory limit exceeded")
                entries.append((relative, child, True, file_stat))
                stack.append((child, relative, depth + 1))
                continue
            file_count += 1
            if file_count > MAX_FILES:
                raise WorkerInputError("snapshot file limit exceeded")
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
                raise WorkerInputError(f"hardlinked or non-regular snapshot entry: {relative}")
            if file_stat.st_size > MAX_FILE_BYTES:
                raise WorkerInputError(f"file limit exceeded: {relative}")
            total_bytes += file_stat.st_size
            if total_bytes > MAX_SNAPSHOT_BYTES:
                raise WorkerInputError("snapshot byte limit exceeded")
            entries.append((relative, child, False, file_stat))
    return sorted(entries, key=lambda item: item[0])


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for relative, path, is_dir, expected_stat in _walk_tree(root):
        if relative == ".git" or relative.startswith(".git/"):
            continue
        if is_dir:
            continue
        file_digest = hashlib.sha256()
        descriptor, opened_stat = _open_source_regular_file(
            root,
            relative,
            expected_stat=expected_stat,
        )
        try:
            with os.fdopen(descriptor, "rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    file_digest.update(chunk)
                _assert_stable_file(opened_stat, os.fstat(handle.fileno()))
        except OSError as exc:
            raise WorkerInputError("snapshot source could not be read") from exc
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0F\0")
        digest.update(str(opened_stat.st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_digest.hexdigest().encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _publication_files(root: Path) -> list[dict[str, Any]]:
    """Attest actual materialized test inputs, including effective Git modes."""
    result = []
    for relative, _, directory, metadata in _walk_tree(root):
        if directory or relative == ".git" or relative.startswith(".git/"):
            continue
        descriptor, opened = _open_source_regular_file(root, relative, expected_stat=metadata)
        with os.fdopen(descriptor, "rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
            _assert_stable_file(opened, os.fstat(handle.fileno()))
        if len(raw) > MAX_FILE_BYTES:
            raise WorkerInputError("publication materialization exceeds file bound")
        result.append({"path": relative, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "mode": "100755" if opened.st_mode & 0o111 else "100644"})
    return sorted(result, key=lambda item: item["path"])


def _copy_snapshot(source: Path, target: Path) -> None:
    entries = _walk_tree(source)
    target.mkdir(parents=True, exist_ok=True)
    for relative, path, is_dir, expected_stat in entries:
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if is_dir:
            destination.mkdir(exist_ok=True)
        else:
            descriptor, opened_stat = _open_source_regular_file(
                source,
                relative,
                expected_stat=expected_stat,
            )
            try:
                with os.fdopen(descriptor, "rb") as source_handle:
                    with destination.open("xb") as target_handle:
                        shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
                    _assert_stable_file(opened_stat, os.fstat(source_handle.fileno()))
            except OSError as exc:
                raise WorkerInputError("snapshot source could not be copied") from exc


def _bounded_bytes(value: bytes, limit: int) -> tuple[bytes, bool]:
    return value[:limit], len(value) > limit


def _validate_test_args(raw: object, allowed_paths: set[str]) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise WorkerInputError("test_args must be a non-empty list")
    if len(raw) > 16:
        raise WorkerInputError("test argument limit exceeded")
    normalized: list[str] = []
    accepted_flags = {"pytest", "-q", "-x", "--maxfail=1", "--disable-warnings"}
    for item in raw:
        if not isinstance(item, str) or not item or len(item.encode("utf-8")) > 4096:
            raise WorkerInputError("invalid test argument")
        if item in accepted_flags:
            if item == "pytest":
                continue
            normalized.append(item)
            continue
        path = _safe_relative(item)
        if path not in allowed_paths:
            raise WorkerInputError("test path is outside allowed_paths")
        normalized.append(path)
    if not any(item in allowed_paths for item in normalized):
        raise WorkerInputError("pytest must name an allowed test path")
    return normalized


def _validate_allowed_paths(raw: object) -> set[str]:
    """Validate the fixed, canonical allowlist before constructing a set."""
    if not isinstance(raw, list) or not raw:
        raise WorkerInputError("allowed_paths must be a non-empty list")
    if len(raw) > MAX_ALLOWED_PATHS:
        raise WorkerInputError("allowed_paths limit exceeded")
    normalized: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item:
            raise WorkerInputError("allowed_paths entries must be non-empty strings")
        if len(item.encode("utf-8")) > MAX_ALLOWED_PATH_BYTES:
            raise WorkerInputError("allowed_paths entry exceeds the fixed length limit")
        path = _safe_relative(item)
        if len(path.encode("utf-8")) > MAX_ALLOWED_PATH_BYTES:
            raise WorkerInputError("allowed_paths entry exceeds the fixed length limit")
        if path in normalized:
            raise WorkerInputError("allowed_paths contains duplicate entries")
        normalized.append(path)
    return set(normalized)


def _validate_patch_paths(patch: bytes, allowed_paths: set[str]) -> list[str]:
    if len(patch) > MAX_PATCH_BYTES:
        raise WorkerInputError("patch byte limit exceeded")
    changed: set[str] = set()
    try:
        text = patch.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkerInputError("patch must be UTF-8") from exc
    for line in text.splitlines():
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            raw = line[6:]
            if raw == "/dev/null":
                continue
            path = _safe_relative(raw)
            if path not in allowed_paths:
                raise WorkerInputError(f"patch path is not allowed: {path}")
            changed.add(path)
    if not changed:
        raise WorkerInputError("patch has no supported file paths")
    return sorted(changed)


def _validate_changed_paths(
    payload: bytes,
    allowed_paths: set[str],
    *,
    required_paths: Iterable[str] = (),
) -> list[str]:
    """Validate the paths reported by git for the exported working diff."""
    normalized_required = tuple(required_paths)
    if len(payload) > MAX_OUTPUT_BYTES:
        raise WorkerInputError("changed path output exceeded limit")
    if not payload:
        if normalized_required:
            raise WorkerInputError("git changed path output is missing approved patch paths")
        return []
    if not payload.endswith(b"\0"):
        raise WorkerInputError("git changed path output is malformed")
    changed: set[str] = set()
    for raw in payload[:-1].split(b"\0"):
        if not raw:
            raise WorkerInputError("git changed path output contains an empty path")
        try:
            path = _safe_relative(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise WorkerInputError("git changed path is not UTF-8") from exc
        if path not in allowed_paths:
            raise WorkerInputError(f"worker changed path is not allowed: {path}")
        changed.add(path)
    required_set = {_safe_relative(path) for path in normalized_required}
    if not required_set.issubset(changed):
        raise WorkerInputError("git changed path output is missing approved patch paths")
    return sorted(changed)


def _run_fixed(
    argv: list[str],
    *,
    cwd: Path,
    timeout: float,
    cpu_seconds: int | None = None,
    allowed_executables: tuple[str, ...] = ("/usr/bin/git", "/usr/local/bin/pytest"),
    environment: dict[str, str] | None = None,
    deadline_at: float | None = None,
    apply_cpu_limit: bool = True,
    process_observer: Callable[[subprocess.Popen[bytes] | None], None] | None = None,
    before_spawn: Callable[[], None] | None = None,
) -> tuple[int, bytes, bytes, bool]:
    if not argv or argv[0] not in set(allowed_executables):
        raise WorkerInputError("worker command is not in the fixed profile")
    env = environment or {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "CI": "1",
        "HOME": "/tmp/seraph-home",
        "TMPDIR": "/tmp",
        "PYTHONNOUSERSITE": "1",
        "PIP_NO_INDEX": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    # CPU limits are enforced by the selected executor's admission/posture
    # contract (Docker cgroups where available).  Do not use ``preexec_fn``:
    # this worker can be invoked from a threaded backend, and running Python
    # code between fork and exec can deadlock while holding interpreter locks.
    # Keep the parameters for the fixed worker call shape and manifest, but
    # make wall deadline the only per-process enforcement performed here.
    _ = cpu_seconds, apply_cpu_limit
    popen_kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
        "start_new_session": True,
    }
    if deadline_at is not None and time.monotonic() >= float(deadline_at):
        raise WorkerInputError("worker wall deadline expired before process spawn")
    if before_spawn is not None:
        try:
            before_spawn()
        except BaseException as exc:
            raise WorkerInputError("worker dispatch fence rejected process spawn") from exc
    process = subprocess.Popen(argv, **popen_kwargs)
    if process_observer is not None:
        try:
            process_observer(process)
        except BaseException as exc:
            _terminate_and_reap_process(process, deadline_at=deadline_at)
            raise WorkerInputError("worker process identity observation failed") from exc
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()

    def _drain(stream: Any, target: bytearray) -> None:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            if len(target) < MAX_STREAM_BYTES:
                target.extend(chunk[: MAX_STREAM_BYTES - len(target)])

    stdout_thread = threading.Thread(target=_drain, args=(process.stdout, stdout_buffer), daemon=True)
    stderr_thread = threading.Thread(target=_drain, args=(process.stderr, stderr_buffer), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    def _join_output_threads() -> None:
        """Drain child output without extending the trusted wall deadline."""

        def _remaining() -> float:
            if deadline_at is None:
                return 5.0
            return max(0.0, min(5.0, float(deadline_at) - time.monotonic()))

        stdout_thread.join(timeout=_remaining())
        stderr_thread.join(timeout=_remaining())
        if stdout_thread.is_alive() or stderr_thread.is_alive():
            raise WorkerInputError("worker output cleanup exceeded the wall deadline")

    try:
        if deadline_at is not None:
            remaining = float(deadline_at) - time.monotonic()
            if remaining <= 0:
                _terminate_and_reap_process(process, deadline_at=deadline_at)
                raise WorkerInputError("worker wall deadline expired before process completion")
            timeout = min(float(timeout), remaining)
        process.wait(timeout=max(0.0, float(timeout)))
        _reject_nested_process_group(process, deadline_at=deadline_at)
        _join_output_threads()
        _wait_process_group_quiescent(process.pid, deadline_at=deadline_at)
        if process_observer is not None:
            try:
                process_observer(None)
            except BaseException as exc:
                raise WorkerInputError("worker terminal identity observation failed") from exc
        return int(process.returncode or 0), bytes(stdout_buffer), bytes(stderr_buffer), False
    except subprocess.TimeoutExpired:
        _terminate_and_reap_process(process, deadline_at=deadline_at)
        _join_output_threads()
        _wait_process_group_quiescent(process.pid, deadline_at=deadline_at)
        if process_observer is not None:
            try:
                process_observer(None)
            except BaseException as exc:
                raise WorkerInputError("worker terminal identity observation failed") from exc
        return 124, bytes(stdout_buffer), bytes(stderr_buffer), True


def _git(
    cwd: Path,
    *args: str,
    timeout: float = 20,
    environment: dict[str, str] | None = None,
    deadline_at: float | None = None,
    apply_cpu_limit: bool = True,
    process_observer: Callable[[subprocess.Popen[bytes] | None], None] | None = None,
    before_spawn: Callable[[], None] | None = None,
) -> tuple[int, bytes, bytes, bool]:
    return _run_fixed(
        ["/usr/bin/git", *args],
        cwd=cwd,
        timeout=timeout,
        environment=environment,
        deadline_at=deadline_at,
        apply_cpu_limit=apply_cpu_limit,
        process_observer=process_observer,
        before_spawn=before_spawn,
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise WorkerInputError("output JSON limit exceeded")
    path.write_bytes(encoded + b"\n")


def _open_private_output_directory(path: Path) -> int:
    descriptor = _open_directory_descriptor(path)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise WorkerInputError("local worker output directory is not private")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _write_private_output(path: Path, name: str, payload: bytes) -> None:
    """Write one local output through a held no-follow directory descriptor."""

    if not name or "/" in name or "\\" in name or "\x00" in name:
        raise WorkerInputError("local worker output name is invalid")
    directory_fd = _open_private_output_directory(path)
    file_fd = -1
    try:
        file_fd = os.open(
            name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_TRUNC
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        metadata = os.fstat(file_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise WorkerInputError("local worker output file is not private")
        offset = 0
        while offset < len(payload):
            offset += os.write(file_fd, payload[offset:])
        os.fsync(file_fd)
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.fsync(directory_fd)
        os.close(directory_fd)


def _write_worker_output_json(output: Path, payload: dict[str, Any], *, private: bool) -> None:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise WorkerInputError("output JSON limit exceeded")
    if private:
        _write_private_output(output, "manifest.json", encoded)
    else:
        _write_json(output / "manifest.json", payload)


def run_job(
    job_file: Path,
    *,
    workspace_root: Path | None = None,
    output_root: Path | None = None,
    backend_kind: str = "docker_rootless",
    pytest_executable: str = "/usr/local/bin/pytest",
    environment: dict[str, str] | None = None,
    process_observer: Callable[[subprocess.Popen[bytes] | None], None] | None = None,
    before_spawn: Callable[[], None] | None = None,
    deadline_at: float | None = None,
    expected_identity: Mapping[str, str] | None = None,
    publication_runtime: Mapping[str, Any] | None = None,
) -> int:
    output = Path(output_root or "/out")
    try:
        job = _read_bounded_job_json(job_file)
        selected_profile = str(job.get("profile") or "")
        if selected_profile != PROFILE and not (selected_profile == "repo-python-pytest-publication-v1" and backend_kind == "local" and publication_runtime is not None):
            raise WorkerInputError("unsupported worker profile")
        if backend_kind not in {"local", "docker_rootless", "docker_rootful"}:
            raise WorkerInputError("worker backend kind is invalid")
        if backend_kind != "local" and pytest_executable != "/usr/local/bin/pytest":
            raise WorkerInputError("Docker worker executable is fixed")
        if backend_kind == "local" and Path(pytest_executable).absolute() != Path(sys.executable).absolute():
            raise WorkerInputError("local worker executable is invalid")
        input_root = job_file.parent
        snapshot_root = input_root / "snapshot"
        patch_path = input_root / "patch.diff"
        workspace = Path(workspace_root or "/workspace")
        if not workspace.is_absolute() or not output.is_absolute():
            raise WorkerInputError("worker roots must be absolute")
        allowed_paths = _validate_allowed_paths(job.get("allowed_paths"))
        patch_descriptor, _patch_stat = _open_source_regular_file(input_root, "patch.diff")
        try:
            with os.fdopen(patch_descriptor, "rb") as patch_handle:
                patch = patch_handle.read(MAX_PATCH_BYTES + 1)
        except OSError as exc:
            raise WorkerInputError("patch input could not be read") from exc
        if len(patch) > MAX_PATCH_BYTES:
            raise WorkerInputError("patch exceeds the fixed input limit")
        patch_paths = _validate_patch_paths(patch, allowed_paths)
        test_args = _validate_test_args(job.get("test_args"), allowed_paths)
        shutil.rmtree(workspace, ignore_errors=True)
        workspace.mkdir(parents=True, exist_ok=True)
        output.mkdir(parents=True, exist_ok=True)
        wall_seconds = max(1, min(int(job.get("wall_seconds", MAX_WALL_SECONDS)), MAX_WALL_SECONDS))
        if deadline_at is None:
            deadline_at = time.monotonic() + wall_seconds
        elif time.monotonic() >= float(deadline_at):
            raise WorkerInputError("worker wall deadline expired before staging")
        _copy_snapshot(snapshot_root, workspace)
        if time.monotonic() >= deadline_at:
            raise WorkerInputError("worker wall deadline expired during staging")
        base_digest = tree_digest(workspace)
        publication_base_files = _publication_files(workspace)
        expected_snapshot_digest = str(job.get("snapshot_digest") or "").strip()
        if not expected_snapshot_digest or expected_snapshot_digest != base_digest:
            raise WorkerInputError("snapshot digest does not match the approved preview")
        expected_base_digest = str(job.get("base_digest") or "").strip()
        if expected_base_digest and expected_base_digest != base_digest:
            raise WorkerInputError("base digest does not match the approved preview")
        worker_image_digest = str(job.get("worker_image_digest") or "").strip()
        if backend_kind != "local" and not worker_image_digest:
            raise WorkerInputError("worker image digest is required")
        expected_patch_sha256 = str(job.get("patch_sha256") or "").strip().lower()
        actual_patch_sha256 = hashlib.sha256(patch).hexdigest()
        if expected_patch_sha256 and expected_patch_sha256 != actual_patch_sha256:
            raise WorkerInputError("patch digest does not match the approved artifact")
        cpu_seconds = max(1, min(int(job.get("cpu_seconds", 120)), 120))
        worker_source_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        interpreter_entry = Path(sys.executable).absolute()
        interpreter_target = interpreter_entry.resolve(strict=True)
        interpreter_digest = hashlib.sha256(interpreter_target.read_bytes()).hexdigest()
        pytest_package_path = ""
        pytest_package_digest = ""
        if backend_kind == "local":
            pytest_package_path, pytest_package_digest = _pytest_package_identity()
        pytest_digest = hashlib.sha256(Path(pytest_executable).resolve().read_bytes()).hexdigest()
        if expected_identity is not None:
            expected = {
                "worker_source_sha256": worker_source_digest,
                "interpreter_sha256": interpreter_digest,
                "pytest_executable_sha256": pytest_digest,
                "pytest_package_sha256": pytest_package_digest,
            }
            if any(str(expected_identity.get(key) or "") != value for key, value in expected.items()):
                raise WorkerInputError("local worker runtime identity changed")

        def _remaining_timeout(phase: str) -> float:
            remaining = float(deadline_at) - time.monotonic()
            if remaining <= 0:
                raise WorkerInputError(f"worker wall deadline expired during {phase}")
            return remaining

        code, _, git_error, timed_out = _git(
            workspace,
            "init",
            "--initial-branch=main",
            environment=environment,
            deadline_at=deadline_at,
            apply_cpu_limit=backend_kind != "local",
            process_observer=process_observer,
            before_spawn=before_spawn,
        )
        if code != 0 or timed_out:
            raise WorkerInputError("git init failed")
        _git(workspace, "config", "user.email", "seraph-worker@localhost", timeout=_remaining_timeout("git config"), environment=environment, deadline_at=deadline_at, apply_cpu_limit=backend_kind != "local", process_observer=process_observer, before_spawn=before_spawn)
        _git(workspace, "config", "user.name", "Seraph worker", timeout=_remaining_timeout("git config"), environment=environment, deadline_at=deadline_at, apply_cpu_limit=backend_kind != "local", process_observer=process_observer, before_spawn=before_spawn)
        code, _, git_error, timed_out = _git(
            workspace,
            "add",
            "--all",
            environment=environment,
            deadline_at=deadline_at,
            apply_cpu_limit=backend_kind != "local",
            process_observer=process_observer,
            before_spawn=before_spawn,
        )
        if code != 0 or timed_out:
            raise WorkerInputError("git add failed")
        code, _, git_error, timed_out = _git(
            workspace,
            "commit",
            "--allow-empty",
            "-m",
            "seraph snapshot",
            environment=environment,
            deadline_at=deadline_at,
            apply_cpu_limit=backend_kind != "local",
            process_observer=process_observer,
            before_spawn=before_spawn,
        )
        if code != 0 or timed_out:
            raise WorkerInputError("git baseline failed")
        patch_file = input_root / "patch.diff"
        code, _, git_error, timed_out = _git(
            workspace,
            "apply",
            "--check",
            str(patch_file),
            environment=environment,
            deadline_at=deadline_at,
            apply_cpu_limit=backend_kind != "local",
            process_observer=process_observer,
            before_spawn=before_spawn,
        )
        if code != 0 or timed_out:
            raise WorkerInputError("patch check failed")
        code, _, git_error, timed_out = _git(
            workspace,
            "apply",
            "--whitespace=nowarn",
            str(patch_file),
            environment=environment,
            deadline_at=deadline_at,
            apply_cpu_limit=backend_kind != "local",
            process_observer=process_observer,
            before_spawn=before_spawn,
        )
        if code != 0 or timed_out:
            raise WorkerInputError("patch apply failed")
        pytest_argv = [pytest_executable, *test_args]
        pytest_environment = environment
        if backend_kind == "local":
            pytest_argv = [pytest_executable, "-I", "-B", "-c", _LOCAL_PYTEST_BOOTSTRAP, *test_args]
            pytest_environment = dict(environment or {})
            pytest_environment.pop("PYTHONPATH", None)
        publication_tested_files = _publication_files(workspace)
        # Legacy command exposure is unchanged. Full ambient environments are
        # not silently certified; only a reviewed explicitly selected bounded
        # runtime can provide publication-grade environment evidence.
        publication_env = {"available": False, "reason": "publication_runtime_profile_unavailable"}
        if publication_runtime is not None:
            from src.execution.repo_publication_runtime import argv, child_environment, verify
            runtime_root = publication_runtime["root"]
            captured = publication_runtime["captured"]
            verify(runtime_root, captured, deadline_at=deadline_at)
            pytest_environment = child_environment(workspace.parent)
            runtime_readback_path = output / "python-runtime-readback.json"
            pytest_argv = argv(runtime_root, captured, runtime_readback_path, test_args)
            publication_env = {"available": True, "profile": selected_profile, "configuration_revision": publication_runtime["configuration_revision"], "runtime_binding": "bounded_exposed_python_closure", "runtime_proof": captured["proof"], "runtime_files": sorted([record["file"] for record in captured["records"]], key=lambda item: item["path"]), "effective_environment": pytest_environment, "effective_argv": pytest_argv}
        code, stdout, stderr, timed_out = _run_fixed(
            pytest_argv,
            cwd=workspace,
            timeout=max(0.01, deadline_at - time.monotonic()),
            cpu_seconds=cpu_seconds,
            allowed_executables=("/usr/local/bin/pytest", pytest_executable, pytest_argv[0]),
            environment=pytest_environment,
            deadline_at=deadline_at,
            apply_cpu_limit=backend_kind != "local",
            process_observer=process_observer,
            before_spawn=before_spawn,
        )
        stdout, stdout_truncated = _bounded_bytes(stdout, MAX_STREAM_BYTES)
        stderr, stderr_truncated = _bounded_bytes(stderr, MAX_STREAM_BYTES)
        if backend_kind == "local":
            _write_private_output(output, "pytest.stdout", stdout)
            _write_private_output(output, "pytest.stderr", stderr)
        else:
            (output / "pytest.stdout").write_bytes(stdout)
            (output / "pytest.stderr").write_bytes(stderr)
        after_digest = tree_digest(workspace)
        # ``git diff`` omits untracked files.  Stage the bounded post-test
        # workspace before exporting so an approved new file cannot silently
        # disappear from the durable artifact.  The sandbox validates the
        # resulting path set against both the allowlist and patch paths.
        stage_code, _, stage_error, stage_timed_out = _git(
            workspace,
            "add",
            "--all",
            timeout=max(0.01, deadline_at - time.monotonic()),
            deadline_at=deadline_at,
            apply_cpu_limit=backend_kind != "local",
            process_observer=process_observer,
            before_spawn=before_spawn,
        )
        if stage_code != 0 or stage_timed_out:
            raise WorkerInputError("changed path staging failed")
        changed_code, changed_paths_raw, changed_error, changed_timed_out = _git(
            workspace,
            "diff",
            "--cached",
            "--name-only",
            "-z",
            "--no-renames",
            "--no-ext-diff",
            "--no-color",
            timeout=max(0.01, deadline_at - time.monotonic()),
            deadline_at=deadline_at,
            apply_cpu_limit=backend_kind != "local",
            process_observer=process_observer,
            before_spawn=before_spawn,
        )
        if changed_code != 0 or changed_timed_out:
            raise WorkerInputError("changed path export failed")
        changed_paths = _validate_changed_paths(changed_paths_raw, allowed_paths, required_paths=patch_paths)
        diff_code, diff, diff_error, diff_timed_out = _git(
            workspace,
            "diff",
            "--cached",
            "--binary",
            "--no-ext-diff",
            "--no-color",
            timeout=max(0.01, deadline_at - time.monotonic()),
            deadline_at=deadline_at,
            apply_cpu_limit=backend_kind != "local",
            process_observer=process_observer,
            before_spawn=before_spawn,
        )
        if diff_code != 0 or diff_timed_out or len(diff) > MAX_OUTPUT_BYTES:
            raise WorkerInputError("diff export failed or exceeded limit")
        if patch_paths and not diff:
            raise WorkerInputError("diff export is missing approved patch paths")
        if backend_kind == "local":
            _write_private_output(output, "diff.patch", diff)
        else:
            (output / "diff.patch").write_bytes(diff)
        diff_sha256 = hashlib.sha256(diff).hexdigest()
        manifest = {
            "profile": selected_profile,
            "backend_kind": backend_kind,
            "status": "succeeded" if code == 0 and not timed_out and not stdout_truncated and not stderr_truncated else "failed",
            "exit_code": code,
            "timed_out": timed_out,
            "base_digest": base_digest,
            "after_digest": after_digest,
            "snapshot_digest": expected_snapshot_digest or base_digest,
            "patch_sha256": actual_patch_sha256,
            "worker_image_digest": worker_image_digest,
            "worker_source_digest": worker_source_digest,
            "diff_sha256": diff_sha256,
            "allowed_paths": sorted(allowed_paths),
            "patch_paths": patch_paths,
            "diff_paths": changed_paths,
            "test_args": test_args,
            "cpu_seconds": cpu_seconds,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }
        publication_env_after = {"available": False, "reason": "publication_runtime_profile_unavailable"}
        if publication_runtime is not None:
            verify(runtime_root, captured, deadline_at=deadline_at)
            descriptor, initial = _open_source_regular_file(output, "python-runtime-readback.json")
            with os.fdopen(descriptor, "rb") as handle:
                runtime_readback = json.loads(handle.read(64 * 1024))
                _assert_stable_file(initial, os.fstat(handle.fileno()))
            if runtime_readback.get("effective_environment") != pytest_environment or runtime_readback.get("bootstrap_sha256") != captured["proof"]["bootstrap_sha256"] or runtime_readback.get("loaded_libpython_sha256") != captured["proof"]["libpython_sha256"] or runtime_readback.get("loaded_libpython_path") != str(runtime_root / "lib" / captured["proof"]["libpython_name"]):
                raise WorkerInputError("actual bounded runtime execution proof invalid")
            publication_env["actual_execution"] = runtime_readback
            publication_env_after = dict(publication_env)
        manifest["publication_test_input"] = {
            "schema": "seraph.repo-publication.tested-input.v1",
            "job_id": str(job.get("job_id") or ""),
            "authority_digest": str(job.get("authority_digest") or ""),
            "base_digest": base_digest,
            "patch_sha256": actual_patch_sha256,
            "base_files": publication_base_files,
            "tested_files": publication_tested_files,
            "output_files": _publication_files(workspace),
            "environment": publication_env,
            "environment_unchanged": publication_env.get("available") is True and publication_env == publication_env_after,
            "test_args": test_args,
            "exit_code": code,
        }
        execution_identity = {
            "schema": "seraph.repo_repair_execution_identity.v1",
            "backend_kind": backend_kind,
            "profile": selected_profile,
            "job_id": str(job.get("job_id") or ""),
            "authority_digest": str(job.get("authority_digest") or ""),
            "worker_source_sha256": worker_source_digest,
        }
        if backend_kind == "local":
            execution_identity.update(
                {
                    "interpreter_entry_path": str(interpreter_entry),
                    "interpreter_path": str(interpreter_target),
                    "interpreter_sha256": interpreter_digest,
                    "pytest_executable_path": str(Path(pytest_executable).absolute()),
                    "pytest_executable_sha256": pytest_digest,
                    "pytest_package_path": pytest_package_path,
                    "pytest_package_sha256": pytest_package_digest,
                }
            )
        else:
            execution_identity.update(
                {
                    "worker_image_digest": worker_image_digest,
                    "runtime_binding": "pinned_container_image",
                }
            )
        manifest["execution_identity"] = execution_identity
        _write_worker_output_json(output, manifest, private=backend_kind == "local")
        if backend_kind == "local":
            encoded_readback = json.dumps({**manifest}, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
            _write_private_output(output, "readback.json", encoded_readback)
        else:
            _write_json(output / "readback.json", {**manifest})
        print("SERAPH_EXPORT_READY", flush=True)
        time.sleep(min(float(job.get("export_grace_seconds", 30)), 30.0))
        return 0 if manifest["status"] == "succeeded" else 1
    except (OSError, ValueError, WorkerInputError, json.JSONDecodeError) as exc:
        output.mkdir(parents=True, exist_ok=True)
        blocked = {"profile": PROFILE, "status": "blocked", "reason": str(exc)}
        _write_worker_output_json(output, blocked, private=backend_kind == "local")
        if backend_kind == "local":
            encoded = json.dumps(blocked, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
            _write_private_output(output, "readback.json", encoded)
            _write_private_output(output, "diff.patch", b"")
            _write_private_output(output, "pytest.stdout", b"")
            _write_private_output(output, "pytest.stderr", b"")
        print(f"SERAPH_WORKER_BLOCKED: {type(exc).__name__}", file=sys.stderr, flush=True)
        return 2


def run_local_job(
    job_file: Path,
    *,
    workspace_root: Path,
    output_root: Path,
    pytest_executable: str,
    environment: dict[str, str],
    process_observer: Callable[[subprocess.Popen[bytes] | None], None] | None = None,
    before_spawn: Callable[[], None] | None = None,
    deadline_at: float | None = None,
    expected_identity: Mapping[str, str] | None = None,
    publication_runtime: Mapping[str, Any] | None = None,
) -> int:
    """Run the fixed worker against server-owned local staged roots.

    This is intentionally a narrow internal wrapper.  Local execution does
    not invent a Docker image or pretend that this process is isolated; the
    caller must have already admitted the trusted local posture and supplied
    private staged roots/environment.
    """

    return run_job(
        job_file,
        workspace_root=workspace_root,
        output_root=output_root,
        backend_kind="local",
        pytest_executable=pytest_executable,
        environment=environment,
        process_observer=process_observer,
        before_spawn=before_spawn,
        deadline_at=deadline_at,
        expected_identity=expected_identity,
        publication_runtime=publication_runtime,
    )


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("mode", nargs="?")
    parser.add_argument("job_file", nargs="?")
    args = parser.parse_args()
    if args.mode == "--transfer-wait" and args.job_file is None:
        time.sleep(30)
        return 0
    if args.mode == "--run" and args.job_file:
        return run_job(Path(args.job_file))
    if args.mode and args.mode.startswith("/") and args.job_file is None:
        return run_job(Path(args.mode))
    if args.mode != "--run" or not args.job_file:
        return 2
    return run_job(Path(args.job_file))


if __name__ == "__main__":
    raise SystemExit(main())
