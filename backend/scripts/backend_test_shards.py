"""Deterministically split backend pytest files into balanced CI shards."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TestFile:
    path: str
    weight: int


TestFile.__test__ = False


# File size alone underestimates a few high-latency suites that dominate backend CI.
RUNTIME_HINT_WEIGHTS: dict[str, int] = {
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


def hinted_test_weight(relative_path: str, base_weight: int) -> int:
    return max(base_weight, RUNTIME_HINT_WEIGHTS.get(relative_path, 0))


def discover_test_files(root: Path) -> list[TestFile]:
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
                weight=hinted_test_weight(relative_path, max(weight, 1)),
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
    shards = assign_test_shards(discover_test_files(root), shard_count)
    return shards[shard_index - 1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard-count", type=int, required=True)
    parser.add_argument("--shard-index", type=int, required=True)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    for path in shard_for_index(root, shard_count=args.shard_count, shard_index=args.shard_index):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
