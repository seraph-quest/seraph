"""Stress, classification and triage lanes for backend tests (#1220).

Run from ``backend/``. Every lane prints the seeds it used and never retries a
test to turn a job green: a rerun only classifies a failure that already
failed the job.

``stress``
    Run the test files a pull request touches ``--runs`` times (20 by default)
    under CPU contention (twice as many busy processes as cores), each run in
    a new seeded random order. Every run must pass. Failing nodes are then
    classified.
``classify``
    Read JUnit reports, rerun each failed node ``--runs`` times alone, and
    print ``deterministic`` or ``intermittent, k of N``. Always exits non-zero
    when there was a failure to classify.
``triage``
    The step before an issue is titled "Flaky": run one node ``--runs`` times
    (50 by default) alone and once in its original file order, and print the
    counts to record in the issue.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import multiprocessing
import os
import random
import re
import statistics
import subprocess
import sys
import time
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from scripts.backend_test_shards import load_measured_durations
    from scripts.run_backend_test_shard import (
        CANCELLED_EVAL_CONSUMER_NODES,
        FULLY_RETIRED_EVAL_INVOCATIONS,
        pytest_invocations_for_target,
    )
except ModuleNotFoundError:  # pragma: no cover - CI script entrypoint fallback
    from backend_test_shards import load_measured_durations
    from run_backend_test_shard import (
        CANCELLED_EVAL_CONSUMER_NODES,
        FULLY_RETIRED_EVAL_INVOCATIONS,
        pytest_invocations_for_target,
    )

TEST_FILE = re.compile(r"^backend/(tests/(?:.+/)?test_[^/]+\.py)$")
DEFAULT_RUN_TIMEOUT_SECONDS = 900
# Placeholder cases the shard runner writes when pytest left no report.
SYNTHETIC_CASES = frozenset({"shard-runner-timeout", "invocation-crashed"})
# pytest exit codes that mean the node did not run (usage, internal, interrupted, none collected).
NOT_RUN_CODES = frozenset({2, 3, 4, 5})


def log(message: str) -> None:
    print(f"[determinism] {message}", flush=True)


def summary(message: str) -> None:
    """Also surface ``message`` where reviewers look: the job summary and a warning annotation."""
    log(message)
    print(f"::warning::{message}", flush=True)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"- {message}\n")


# -- CPU contention -----------------------------------------------------------------
def _burn() -> None:  # pragma: no cover - runs in a child process
    while True:
        pass


@contextlib.contextmanager
def cpu_contention(factor: int) -> Iterator[int]:
    """Keep ``factor`` x cores busy processes running for the duration."""
    count = max(0, factor) * (os.cpu_count() or 1)
    context = multiprocessing.get_context("spawn")
    workers = [context.Process(target=_burn, daemon=True) for _ in range(count)]
    for worker in workers:
        worker.start()
    log(f"CPU contention: {count} busy processes on {os.cpu_count()} cores")
    try:
        yield count
    finally:
        for worker in workers:
            worker.terminate()
        for worker in workers:
            worker.join()


# -- pytest invocation --------------------------------------------------------------
def pytest_command(args: list[str], *, seed: int | None = None, junit: Path | None = None) -> list[str]:
    command = [sys.executable, "-m", "pytest", "-q", "--no-cov", "-p", "no:cacheprovider",
               "-p", "scripts.pytest_quarantine", "--quarantine=exclude"]
    if seed is not None:
        command += ["-p", "scripts.pytest_shuffle", f"--shuffle-seed={seed}"]
    if junit is not None:
        command += [f"--junitxml={junit}"]
    return [*command, *args]


def run_pytest(command: list[str], *, timeout: int) -> int:
    try:
        return subprocess.run(command, cwd=BACKEND, check=False, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        log(f"pytest killed after {timeout}s: {' '.join(command)}")
        return 124


# -- changed files ------------------------------------------------------------------
def changed_test_files(base: str, repo_root: Path = BACKEND.parent) -> list[str]:
    """Backend test modules added, modified or renamed since the merge base with ``base``."""
    output = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=AMR", f"{base}...HEAD"],
        cwd=repo_root, check=True, capture_output=True, text=True,
    ).stdout
    files = []
    for line in output.splitlines():
        match = TEST_FILE.match(line.strip())
        if match and (repo_root / "backend" / match.group(1)).is_file():
            files.append(match.group(1))
    return sorted(files)


def invocations_for(files: list[str]) -> list[tuple[str, list[str]]]:
    """The same split and retirement the shard runner applies in CI."""
    result = []
    for path in files:
        if path == "tests/test_eval_harness.py":
            log("not stressed: tests/test_eval_harness.py is a cancelled module the shard runner never runs")
            continue
        deselect = [f"--deselect={node}" for node in CANCELLED_EVAL_CONSUMER_NODES.get(path, [])]
        for label, args in pytest_invocations_for_target(path):
            if (path, label) in FULLY_RETIRED_EVAL_INVOCATIONS:
                continue
            result.append((label, [*args, *deselect]))
    return result


# -- JUnit --------------------------------------------------------------------------
def node_id(classname: str, name: str, backend: Path = BACKEND) -> str:
    """Rebuild a pytest node id from a JUnit testcase (``tests.test_x.TestY`` + ``test_z``).

    A collection error has an empty classname and the module as its name; the
    node is then the module itself.
    """
    if not classname:
        classname, name = name, ""
    parts = classname.split(".")
    for split in range(len(parts), 0, -1):
        module = Path(*parts[:split]).with_suffix(".py")
        if (backend / module).is_file():
            return "::".join([module.as_posix(), *parts[split:], *([name] if name else [])])
    return "::".join([classname.replace(".", "/") + ".py", *([name] if name else [])])


def failed_nodes(junit_paths: list[Path]) -> list[str]:
    nodes: list[str] = []
    for path in junit_paths:
        try:
            root = ElementTree.parse(path).getroot()
        except (OSError, ElementTree.ParseError) as error:
            log(f"unreadable JUnit report {path}: {error}")
            continue
        for case in root.iter("testcase"):
            if case.find("failure") is None and case.find("error") is None:
                continue
            if case.get("name") in SYNTHETIC_CASES:
                log(f"{case.get('classname')}: {case.get('name')}; no single node to classify")
                continue
            node = node_id(case.get("classname", ""), case.get("name", ""))
            if node not in nodes:
                nodes.append(node)
    return nodes


# -- lanes --------------------------------------------------------------------------
@dataclass
class Classification:
    node: str
    failures: int
    runs: int
    not_run: int = 0
    under_load: bool = False

    @property
    def verdict(self) -> str:
        if self.not_run:
            return (f"not classifiable: pytest could not run the node in {self.not_run} of {self.runs} runs "
                    "(missing node, usage or collection error)")
        where = "alone under CPU contention" if self.under_load else "alone without added load"
        if self.failures == self.runs:
            return f"deterministic ({self.runs} of {self.runs} failed {where})"
        if self.failures == 0:
            return f"passed {self.runs} of {self.runs} {where} (fails only with its neighbours or under other load)"
        return f"intermittent, {self.failures} of {self.runs} {where}"


def classify_nodes(nodes: list[str], *, runs: int, timeout: int, under_load: bool = False) -> list[Classification]:
    """Rerun each node alone. Exit 1, a crash or a kill counts as a failure;
    exits 2-5 mean the node never ran and make the verdict "not classifiable"."""
    results = []
    for node in nodes:
        failures = not_run = 0
        for attempt in range(1, runs + 1):
            code = run_pytest(pytest_command([node]), timeout=timeout)
            if code in NOT_RUN_CODES:
                not_run += 1
            elif code != 0:
                failures += 1
            log(f"classify {node}: run {attempt}/{runs} -> {code}")
        result = Classification(node, failures, runs, not_run, under_load)
        log(f"CLASSIFIED {node}: {result.verdict}")
        results.append(result)
    return results


def lane_classify(args: argparse.Namespace) -> int:
    reports = sorted(Path(args.junit_dir).rglob("*.xml")) if Path(args.junit_dir).is_dir() else []
    nodes = failed_nodes(reports)
    if not nodes:
        log("no failed test case in the JUnit reports; nothing to classify")
        return 1 if args.expect_failure else 0
    if len(nodes) > args.max_nodes:
        log(f"{len(nodes)} failed nodes; classifying the first {args.max_nodes}: the rest are listed only")
        for node in nodes[args.max_nodes:]:
            log(f"NOT CLASSIFIED {node}")
    classify_nodes(nodes[: args.max_nodes], runs=args.runs, timeout=args.run_timeout_seconds)
    log("classification never turns a job green: the original failure stands")
    return 1


def lane_stress(args: argparse.Namespace) -> int:
    files = list(args.files) or changed_test_files(args.base)
    if not files:
        log(f"no backend test file changed since {args.base}; nothing to stress")
        return 0
    durations = load_measured_durations()
    # A file with no measurement yet is assumed to be a typical (median) file.
    unmeasured = statistics.median(durations.values()) if durations else 15.0
    budget = args.budget_minutes * 60
    # Measured durations are uncontended; under N x-core contention pytest gets
    # roughly 1/(N+1) of the CPU.
    slowdown = args.contention + 1

    def cost(path: str) -> float:
        return durations.get(path, unmeasured) * args.runs * slowdown

    # Cheapest first, so a heavy file cannot crowd out every other change.
    selected, skipped, estimate = [], [], 0.0
    for path in sorted(files, key=lambda item: (cost(item), item)):
        if selected and estimate + cost(path) > budget:
            skipped.append(path)
            continue
        selected.append(path)
        estimate += cost(path)
    for path in skipped:
        summary(f"NOT STRESSED {path}: over the {args.budget_minutes}-minute stress budget; "
                "it still runs once in its shard. Stress it locally: determinism_lanes.py stress --files " + path)
    invocations = invocations_for(selected)
    base_seed = args.seed if args.seed is not None else random.SystemRandom().randrange(1, 2**31)
    log(f"stressing {len(selected)} file(s), {args.runs} runs, base seed {base_seed} "
        f"(replay one run with --shuffle-seed=<base+run>): {', '.join(selected)}")
    junit_dir = Path(args.junit_dir)
    junit_dir.mkdir(parents=True, exist_ok=True)
    failing_runs: list[tuple[int, str, int]] = []
    with cpu_contention(args.contention):
        for run in range(1, args.runs + 1):
            seed = base_seed + run
            for label, invocation in invocations:
                report = junit_dir / f"run{run:02d}-{re.sub(r'[^A-Za-z0-9_.-]+', '_', label)}.xml"
                started = time.perf_counter()
                code = run_pytest(pytest_command(invocation, seed=seed, junit=report),
                                  timeout=args.run_timeout_seconds)
                log(f"run {run}/{args.runs} seed {seed} {label} -> {code} ({time.perf_counter() - started:.1f}s)")
                if code != 0:
                    failing_runs.append((run, label, seed))
        if not failing_runs:
            log(f"stress ok: {len(selected)} file(s) passed {args.runs} of {args.runs} runs")
            return 0
        for run, label, seed in failing_runs:
            log(f"FAILED run {run} {label} (replay: --shuffle-seed={seed})")
        nodes = failed_nodes(sorted(junit_dir.glob("*.xml")))
        classify_nodes(nodes[: args.max_nodes], runs=args.classify_runs, timeout=args.run_timeout_seconds,
                       under_load=args.contention > 0)
    log(f"stress failed: {len(failing_runs)} failing run(s) of {args.runs * len(invocations)}")
    return 1


def lane_triage(args: argparse.Namespace) -> int:
    log(f"triage {args.node} at {current_commit()}: {args.runs} runs alone, then once in file order")
    isolated = classify_nodes([args.node], runs=args.runs, timeout=args.run_timeout_seconds)[0]
    module = args.node.split("::", 1)[0]
    file_order = run_pytest(pytest_command([module]), timeout=args.run_timeout_seconds)
    summary = {
        "node": args.node,
        "commit": current_commit(),
        "alone": f"{isolated.failures} failed of {isolated.runs}"
                 + (f", {isolated.not_run} could not run" if isolated.not_run else ""),
        "file_order": "passed" if file_order == 0 else f"failed (exit {file_order})",
        "verdict": isolated.verdict,
    }
    print(json.dumps(summary, indent=2))
    log("record these counts in the issue before titling it Flaky")
    return 0


def current_commit() -> str:
    return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=BACKEND, capture_output=True,
                          text=True, check=False).stdout.strip() or "unknown"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="lane", required=True)

    stress = sub.add_parser("stress", help="stress the test files a change touches")
    stress.add_argument("--base", default="origin/develop", help="diff base for changed files")
    stress.add_argument("--files", nargs="*", default=[], help="explicit test files (relative to backend/)")
    stress.add_argument("--runs", type=int, default=20)
    stress.add_argument("--contention", type=int, default=2, help="busy processes per core")
    stress.add_argument("--seed", type=int, default=None, help="base seed; run N uses base+N")
    stress.add_argument("--budget-minutes", type=int, default=60)
    stress.add_argument("--classify-runs", type=int, default=20)
    stress.add_argument("--max-nodes", type=int, default=5)
    stress.add_argument("--junit-dir", default="test-results/stress")
    stress.add_argument("--run-timeout-seconds", type=int, default=DEFAULT_RUN_TIMEOUT_SECONDS)
    stress.set_defaults(handler=lane_stress)

    classify = sub.add_parser("classify", help="classify failed nodes from JUnit reports")
    classify.add_argument("--junit-dir", required=True)
    classify.add_argument("--runs", type=int, default=20)
    classify.add_argument("--max-nodes", type=int, default=5)
    classify.add_argument("--expect-failure", action="store_true",
                          help="exit non-zero even when no failed test case is found (the job already failed)")
    classify.add_argument("--run-timeout-seconds", type=int, default=DEFAULT_RUN_TIMEOUT_SECONDS)
    classify.set_defaults(handler=lane_classify)

    triage = sub.add_parser("triage", help="the counts to record before titling an issue Flaky")
    triage.add_argument("node")
    triage.add_argument("--runs", type=int, default=50)
    triage.add_argument("--run-timeout-seconds", type=int, default=DEFAULT_RUN_TIMEOUT_SECONDS)
    triage.set_defaults(handler=lane_triage)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
