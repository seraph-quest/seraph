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


RUNTIME_HEAVY_FILE_TIMEOUTS: dict[str, int] = {
    "tests/test_delivery.py": 1_200,
    "tests/test_eval_harness.py": 1_500,
    "tests/test_observer_api.py": 1_200,
    "tests/test_workflows.py": 1_500,
}

SPECIALIZED_TEST_INVOCATIONS: dict[str, list[tuple[str, list[str]]]] = {
    "tests/test_delivery.py": [
        (
            "tests/test_delivery.py::channel_and_bundle",
            [
                "tests/test_delivery.py",
                "-k",
                "native_channel or channel_routing or queued_bundle",
            ],
        ),
        (
            "tests/test_delivery.py::remaining",
            [
                "tests/test_delivery.py",
                "-k",
                "not (native_channel or channel_routing or queued_bundle)",
            ],
        ),
    ],
    "tests/test_eval_harness.py": [
        (
            "tests/test_eval_harness.py::test_run_runtime_evals_passes_all_scenarios",
            ["tests/test_eval_harness.py::test_run_runtime_evals_passes_all_scenarios"],
        ),
        (
            "tests/test_eval_harness.py::remaining",
            [
                "tests/test_eval_harness.py",
                "-k",
                "not test_run_runtime_evals_passes_all_scenarios",
            ],
        ),
    ],
    "tests/test_observer_api.py": [
        (
            "tests/test_observer_api.py::continuity_and_notifications",
            [
                "tests/test_observer_api.py",
                "-k",
                "continuity or native_notification or intervention_feedback",
            ],
        ),
        (
            "tests/test_observer_api.py::remaining",
            [
                "tests/test_observer_api.py",
                "-k",
                "not (continuity or native_notification or intervention_feedback)",
            ],
        ),
    ],
    "tests/test_workflows.py": [
        (
            "tests/test_workflows.py::boundary_drift",
            [
                "tests/test_workflows.py",
                "-k",
                "approval_context or authenticated_source or delegated_specialist or delegated_tool_inventory or legacy_checkpoint",
            ],
        ),
        (
            "tests/test_workflows.py::remaining",
            [
                "tests/test_workflows.py",
                "-k",
                "not (approval_context or authenticated_source or delegated_specialist or delegated_tool_inventory or legacy_checkpoint)",
            ],
        ),
    ],
}


def timeout_for_file(path: str, default_timeout_seconds: int | None) -> int | None:
    hinted_timeout = RUNTIME_HEAVY_FILE_TIMEOUTS.get(path)
    if default_timeout_seconds is None:
        return hinted_timeout
    if hinted_timeout is None:
        return default_timeout_seconds
    return max(default_timeout_seconds, hinted_timeout)


def pytest_invocations_for_target(path: str) -> list[tuple[str, list[str]]]:
    return SPECIALIZED_TEST_INVOCATIONS.get(path, [(path, [path])])


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
        for label, invocation_args in pytest_invocations_for_target(path):
            command = [sys.executable, "-m", "pytest", "-q", *invocation_args, *extra_args]
            timeout_seconds = timeout_for_file(path, file_timeout_seconds)
            started_at = time.perf_counter()
            try:
                completed = subprocess.run(
                    command,
                    cwd=root,
                    check=False,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                duration_s = time.perf_counter() - started_at
                print(
                    f"[backend-shard] {label} -> 124 ({duration_s:.2f}s) timed out after "
                    f"{timeout_seconds}s"
                )
                return 124
            duration_s = time.perf_counter() - started_at
            print(
                f"[backend-shard] {label} -> {completed.returncode} ({duration_s:.2f}s)"
                f"{f' timeout={timeout_seconds}s' if timeout_seconds is not None else ''}"
            )
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
