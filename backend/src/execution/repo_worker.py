"""Fixed worker entrypoint for the ``repo-python-pytest-v1`` image.

This module deliberately has no generic command or network interface.  The
trusted backend writes one bounded job.json, a patch, and a repository snapshot
to the read-only input volume.  The container entrypoint accepts only that
fixed file or the loader's inert transfer mode.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import resource
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import time
from typing import Any, Iterable


PROFILE = "repo-python-pytest-v1"
MAX_FILES = 2_000
MAX_DIRECTORIES = 500
MAX_DEPTH = 16
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_PATCH_BYTES = 1 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_STREAM_BYTES = 1 * 1024 * 1024
MAX_WALL_SECONDS = 180


class WorkerInputError(ValueError):
    """Input was not part of the fixed worker contract."""


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
) -> tuple[int, bytes, bytes, bool]:
    if not argv or argv[0] not in {"/usr/bin/git", "/usr/local/bin/pytest"}:
        raise WorkerInputError("worker command is not in the fixed profile")
    env = {
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
    def _limit_cpu() -> None:
        if cpu_seconds is not None:
            bounded = max(1, min(int(cpu_seconds), 120))
            resource.setrlimit(resource.RLIMIT_CPU, (bounded, bounded))

    process = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=True,
        preexec_fn=_limit_cpu,
    )
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
    try:
        process.wait(timeout=timeout)
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        return int(process.returncode or 0), bytes(stdout_buffer), bytes(stderr_buffer), False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        return 124, bytes(stdout_buffer), bytes(stderr_buffer), True


def _git(cwd: Path, *args: str, timeout: float = 20) -> tuple[int, bytes, bytes, bool]:
    return _run_fixed(["/usr/bin/git", *args], cwd=cwd, timeout=timeout)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise WorkerInputError("output JSON limit exceeded")
    path.write_bytes(encoded + b"\n")


def run_job(job_file: Path) -> int:
    try:
        job = json.loads(job_file.read_text(encoding="utf-8"))
        if not isinstance(job, dict) or job.get("profile") != PROFILE:
            raise WorkerInputError("unsupported worker profile")
        input_root = job_file.parent
        snapshot_root = input_root / "snapshot"
        patch_path = input_root / "patch.diff"
        workspace = Path("/workspace")
        output = Path("/out")
        allowed_paths = {_safe_relative(value) for value in job.get("allowed_paths", [])}
        if not allowed_paths or len(allowed_paths) > 64:
            raise WorkerInputError("allowed_paths is invalid")
        patch = patch_path.read_bytes()
        patch_paths = _validate_patch_paths(patch, allowed_paths)
        test_args = _validate_test_args(job.get("test_args"), allowed_paths)
        shutil.rmtree(workspace, ignore_errors=True)
        workspace.mkdir(parents=True, exist_ok=True)
        output.mkdir(parents=True, exist_ok=True)
        _copy_snapshot(snapshot_root, workspace)
        base_digest = tree_digest(workspace)
        expected_snapshot_digest = str(job.get("snapshot_digest") or "").strip()
        if not expected_snapshot_digest or expected_snapshot_digest != base_digest:
            raise WorkerInputError("snapshot digest does not match the approved preview")
        expected_base_digest = str(job.get("base_digest") or "").strip()
        if expected_base_digest and expected_base_digest != base_digest:
            raise WorkerInputError("base digest does not match the approved preview")
        worker_image_digest = str(job.get("worker_image_digest") or "").strip()
        if not worker_image_digest:
            raise WorkerInputError("worker image digest is required")
        expected_patch_sha256 = str(job.get("patch_sha256") or "").strip().lower()
        actual_patch_sha256 = hashlib.sha256(patch).hexdigest()
        if expected_patch_sha256 and expected_patch_sha256 != actual_patch_sha256:
            raise WorkerInputError("patch digest does not match the approved artifact")
        cpu_seconds = max(1, min(int(job.get("cpu_seconds", 120)), 120))
        code, _, git_error, timed_out = _git(workspace, "init", "--initial-branch=main")
        if code != 0 or timed_out:
            raise WorkerInputError("git init failed")
        _git(workspace, "config", "user.email", "seraph-worker@localhost")
        _git(workspace, "config", "user.name", "Seraph worker")
        code, _, git_error, timed_out = _git(workspace, "add", "--all")
        if code != 0 or timed_out:
            raise WorkerInputError("git add failed")
        code, _, git_error, timed_out = _git(workspace, "commit", "--allow-empty", "-m", "seraph snapshot")
        if code != 0 or timed_out:
            raise WorkerInputError("git baseline failed")
        patch_file = input_root / "patch.diff"
        code, _, git_error, timed_out = _git(workspace, "apply", "--check", str(patch_file))
        if code != 0 or timed_out:
            raise WorkerInputError("patch check failed")
        code, _, git_error, timed_out = _git(workspace, "apply", "--whitespace=nowarn", str(patch_file))
        if code != 0 or timed_out:
            raise WorkerInputError("patch apply failed")
        code, stdout, stderr, timed_out = _run_fixed(
            ["/usr/local/bin/pytest", *test_args],
            cwd=workspace,
            timeout=min(int(job.get("wall_seconds", MAX_WALL_SECONDS)), MAX_WALL_SECONDS),
            cpu_seconds=cpu_seconds,
        )
        stdout, stdout_truncated = _bounded_bytes(stdout, MAX_STREAM_BYTES)
        stderr, stderr_truncated = _bounded_bytes(stderr, MAX_STREAM_BYTES)
        (output / "pytest.stdout").write_bytes(stdout)
        (output / "pytest.stderr").write_bytes(stderr)
        after_digest = tree_digest(workspace)
        # ``git diff`` omits untracked files.  Stage the bounded post-test
        # workspace before exporting so an approved new file cannot silently
        # disappear from the durable artifact.  The sandbox validates the
        # resulting path set against both the allowlist and patch paths.
        stage_code, _, stage_error, stage_timed_out = _git(workspace, "add", "--all", timeout=30)
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
            timeout=30,
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
            timeout=30,
        )
        if diff_code != 0 or diff_timed_out or len(diff) > MAX_OUTPUT_BYTES:
            raise WorkerInputError("diff export failed or exceeded limit")
        if patch_paths and not diff:
            raise WorkerInputError("diff export is missing approved patch paths")
        (output / "diff.patch").write_bytes(diff)
        diff_sha256 = hashlib.sha256(diff).hexdigest()
        manifest = {
            "profile": PROFILE,
            "status": "succeeded" if code == 0 and not timed_out and not stdout_truncated and not stderr_truncated else "failed",
            "exit_code": code,
            "timed_out": timed_out,
            "base_digest": base_digest,
            "after_digest": after_digest,
            "snapshot_digest": expected_snapshot_digest or base_digest,
            "patch_sha256": actual_patch_sha256,
            "worker_image_digest": worker_image_digest,
            "worker_source_digest": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "diff_sha256": diff_sha256,
            "allowed_paths": sorted(allowed_paths),
            "patch_paths": patch_paths,
            "diff_paths": changed_paths,
            "test_args": test_args,
            "cpu_seconds": cpu_seconds,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }
        _write_json(output / "manifest.json", manifest)
        _write_json(output / "readback.json", {**manifest})
        print("SERAPH_EXPORT_READY", flush=True)
        time.sleep(min(float(job.get("export_grace_seconds", 30)), 30.0))
        return 0 if manifest["status"] == "succeeded" else 1
    except (OSError, ValueError, WorkerInputError, json.JSONDecodeError) as exc:
        output = Path("/out")
        output.mkdir(parents=True, exist_ok=True)
        _write_json(output / "manifest.json", {"profile": PROFILE, "status": "blocked", "reason": str(exc)})
        print(f"SERAPH_WORKER_BLOCKED: {type(exc).__name__}", file=sys.stderr, flush=True)
        return 2


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
