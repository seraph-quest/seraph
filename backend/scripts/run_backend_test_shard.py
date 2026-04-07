"""Run one backend test shard as isolated per-file pytest subprocesses."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from scripts.backend_test_shards import shard_for_index
except ModuleNotFoundError:  # pragma: no cover - CI script entrypoint fallback
    from backend_test_shards import shard_for_index


def run_shard_files(
    root: Path,
    files: list[str],
    *,
    pytest_args: list[str] | None = None,
    file_timeout_seconds: int | None = None,
) -> int:
    if not files:
        print("No backend tests assigned to this shard.")
        return 0

    extra_args = list(pytest_args or [])
    for path in files:
        command = [sys.executable, "-m", "pytest", "-q", path, *extra_args]
        started_at = time.perf_counter()
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                check=False,
                timeout=file_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            duration_s = time.perf_counter() - started_at
            print(
                f"[backend-shard] {path} -> 124 ({duration_s:.2f}s) timed out after "
                f"{file_timeout_seconds}s"
            )
            return 124
        duration_s = time.perf_counter() - started_at
        print(f"[backend-shard] {path} -> {completed.returncode} ({duration_s:.2f}s)")
        if completed.returncode != 0:
            return completed.returncode
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard-count", type=int, required=True)
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--file-timeout-seconds", type=int, default=None)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    files = shard_for_index(root, shard_count=args.shard_count, shard_index=args.shard_index)
    extra_args = list(args.pytest_args)
    if extra_args[:1] == ["--"]:
        extra_args = extra_args[1:]
    return run_shard_files(
        root,
        files,
        pytest_args=extra_args,
        file_timeout_seconds=args.file_timeout_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
