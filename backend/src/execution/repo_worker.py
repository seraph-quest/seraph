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
import resource
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
from typing import Any


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


def _walk_tree(root: Path) -> list[tuple[str, Path, bool]]:
    """Return regular files/directories, refusing links and special files."""
    root = root.resolve(strict=True)
    entries: list[tuple[str, Path, bool]] = []
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
                stat = child.lstat()
            except OSError as exc:
                raise WorkerInputError(f"snapshot stat failed: {exc}") from exc
            if child.is_symlink() or not (child.is_file() or child.is_dir()):
                raise WorkerInputError(f"unsupported snapshot entry: {relative}")
            if child.is_dir():
                directory_count += 1
                if directory_count > MAX_DIRECTORIES:
                    raise WorkerInputError("snapshot directory limit exceeded")
                entries.append((relative, child, True))
                stack.append((child, relative, depth + 1))
                continue
            file_count += 1
            if file_count > MAX_FILES:
                raise WorkerInputError("snapshot file limit exceeded")
            if stat.st_size > MAX_FILE_BYTES:
                raise WorkerInputError(f"file limit exceeded: {relative}")
            total_bytes += stat.st_size
            if total_bytes > MAX_SNAPSHOT_BYTES:
                raise WorkerInputError("snapshot byte limit exceeded")
            entries.append((relative, child, False))
    return sorted(entries, key=lambda item: item[0])


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for relative, path, is_dir in _walk_tree(root):
        if relative == ".git" or relative.startswith(".git/"):
            continue
        if is_dir:
            continue
        file_digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                file_digest.update(chunk)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0F\0")
        digest.update(str(path.stat().st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_digest.hexdigest().encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _copy_snapshot(source: Path, target: Path) -> None:
    entries = _walk_tree(source)
    target.mkdir(parents=True, exist_ok=True)
    for relative, path, is_dir in entries:
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if is_dir:
            destination.mkdir(exist_ok=True)
        else:
            with path.open("rb") as source_handle, destination.open("xb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)


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
        diff_code, diff, diff_error, diff_timed_out = _git(workspace, "diff", "--binary", "--no-ext-diff", "--no-color", timeout=30)
        if diff_code != 0 or diff_timed_out or len(diff) > MAX_OUTPUT_BYTES:
            raise WorkerInputError("diff export failed or exceeded limit")
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
            "patch_paths": patch_paths,
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
