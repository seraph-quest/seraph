"""Deterministically split backend pytest files into balanced CI shards."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import re
from pathlib import Path


@dataclass(frozen=True)
class TestFile:
    path: str
    weight: int


TestFile.__test__ = False


# File size alone underestimates a few high-latency suites that dominate backend CI.
RUNTIME_HINT_WEIGHTS: dict[str, int] = {
    "tests/test_approvals_api.py": 170_000,
    "tests/test_context_window.py": 160_000,
    "tests/test_workflows.py": 260_000,
    "tests/test_extensions_api.py": 230_000,
    "tests/test_llm_runtime.py": 220_000,
    "tests/test_guardian_state.py": 210_000,
    "tests/test_eval_harness.py": 240_000,
    "tests/test_delivery.py": 180_000,
    "tests/test_activity_api.py": 150_000,
    "tests/test_observer_api.py": 145_000,
    "tests/test_operator_api.py": 140_000,
    "tests/test_memory_providers.py": 135_000,
}


# Measured hosted-CI wall seconds per test file (#1180). Weights are in
# milliseconds; file size in bytes is a rough stand-in (median ~1 ms per byte
# on hosted runners) used only for files without a measurement. Size alone
# let one shard reach the 40-minute step cap while others ran under 15.
DURATIONS_FILE = Path(__file__).resolve().with_name("backend_test_durations.json")
_SHARD_RESULT = re.compile(r"\[backend-shard\] (tests/\S+?\.py)(?:::\S+)? -> (\d+) \(([0-9.]+)s\)")


def load_measured_durations(path: Path = DURATIONS_FILE) -> dict[str, float]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    durations = payload.get("seconds") if isinstance(payload, dict) else None
    if not isinstance(durations, dict):
        return {}
    return {name: float(value) for name, value in durations.items()
            if isinstance(name, str) and isinstance(value, (int, float)) and value > 0}


def durations_from_shard_logs(texts: list[str]) -> dict[str, float]:
    """Sum per-file seconds from passing ``run_backend_test_shard`` lines.

    A file with any failing or timed-out invocation is left out: a red file
    usually stops early and would understate its passing runtime.
    """
    totals: dict[str, float] = {}
    failed: set[str] = set()
    for text in texts:
        for match in _SHARD_RESULT.finditer(text):
            path, code, seconds = match.group(1), int(match.group(2)), float(match.group(3))
            if code != 0:
                failed.add(path)
            totals[path] = totals.get(path, 0.0) + seconds
    return {path: round(seconds, 2) for path, seconds in sorted(totals.items()) if path not in failed}


def hinted_test_weight(relative_path: str, base_weight: int, measured: dict[str, float] | None = None) -> int:
    if measured and relative_path in measured:
        return max(1, round(measured[relative_path] * 1000))
    return max(base_weight, RUNTIME_HINT_WEIGHTS.get(relative_path, 0))


def discover_test_files(root: Path, measured: dict[str, float] | None = None) -> list[TestFile]:
    tests_dir = root / "tests"
    discovered: list[TestFile] = []
    for path in sorted(tests_dir.glob("test_*.py")):
        try:
            weight = path.stat().st_size
        except OSError:
            continue
        relative_path = str(path.relative_to(root))
        discovered.append(
            TestFile(
                path=relative_path,
                weight=hinted_test_weight(relative_path, max(weight, 1), measured),
            )
        )
    return discovered


def assign_test_shards(files: list[TestFile], shard_count: int) -> list[list[str]]:
    if shard_count <= 0:
        raise ValueError("shard_count must be positive")
    shard_paths: list[list[str]] = [[] for _ in range(shard_count)]
    shard_weights = [0 for _ in range(shard_count)]
    for file in sorted(files, key=lambda item: (-item.weight, item.path)):
        index = min(
            range(shard_count),
            key=lambda shard_index: (shard_weights[shard_index], len(shard_paths[shard_index]), shard_index),
        )
        shard_paths[index].append(file.path)
        shard_weights[index] += file.weight
    return [sorted(paths) for paths in shard_paths]


def shard_for_index(root: Path, *, shard_count: int, shard_index: int) -> list[str]:
    if shard_index <= 0 or shard_index > shard_count:
        raise ValueError("shard_index must be between 1 and shard_count")
    shards = assign_test_shards(discover_test_files(root, load_measured_durations()), shard_count)
    return shards[shard_index - 1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard-count", type=int)
    parser.add_argument("--shard-index", type=int)
    parser.add_argument("--update-durations-from", nargs="+", metavar="SHARD_LOG",
                        help="refresh the measured durations from hosted shard job logs and exit")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    if args.update_durations_from:
        texts = [Path(item).read_text(encoding="utf-8", errors="replace") for item in args.update_durations_from]
        measured = {**load_measured_durations(), **durations_from_shard_logs(texts)}
        DURATIONS_FILE.write_text(json.dumps({"source": "hosted backend-tests shard logs", "seconds": measured},
                                             indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"{len(measured)} measured test files")
        return 0
    if args.shard_count is None or args.shard_index is None:
        parser.error("--shard-count and --shard-index are required")
    for path in shard_for_index(root, shard_count=args.shard_count, shard_index=args.shard_index):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
