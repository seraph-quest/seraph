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
NO_TESTS_COLLECTED = 5
# The pyproject watchdogs (#1218). Under N x-core contention a healthy test runs
# up to N+1 times slower, so the stress lane scales them to stay liveness
# watchdogs, not gates.
WATCHDOG_TIMEOUT_SECONDS = 420
WATCHDOG_FAULTHANDLER_SECONDS = 240
# Measured per-invocation interpreter + collection start-up under contention,
# on top of the (uncontended) measured file duration x slowdown.
CONTENDED_STARTUP_SECONDS = 8.0


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
def pytest_command(args: list[str], *, seed: int | None = None, junit: Path | None = None,
                   watchdog_scale: int = 1) -> list[str]:
    command = [sys.executable, "-m", "pytest", "-q", "--no-cov", "-p", "no:cacheprovider",
               "-p", "scripts.pytest_quarantine", "--quarantine=exclude"]
    if watchdog_scale > 1:
        command += ["-o", f"timeout={WATCHDOG_TIMEOUT_SECONDS * watchdog_scale}",
                    "-o", f"faulthandler_timeout={WATCHDOG_FAULTHANDLER_SECONDS * watchdog_scale}"]
    if seed is not None:
        command += ["-p", "scripts.pytest_shuffle", f"--shuffle-seed={seed}"]
    if junit is not None:
        command += [f"--junitxml={junit}"]
    return [*command, *args]


def run_pytest(command: list[str], *, timeout: float) -> int:
    try:
        return subprocess.run(command, cwd=BACKEND, check=False, timeout=max(1.0, timeout)).returncode
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


def invocations_for(files: list[str], *, quiet: bool = False) -> list[tuple[str, list[str]]]:
    """The same split and retirement the shard runner applies in CI."""
    result = []
    for path in files:
        if path == "tests/test_eval_harness.py":
            if not quiet:
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
    planned: int | None = None

    @property
    def verdict(self) -> str:
        if self.runs == 0:
            return "not classified: no time left before the job deadline"
        if self.planned is not None and self.runs < self.planned:
            return f"{self._verdict()} (stopped at the job deadline after {self.runs} of {self.planned} runs)"
        return self._verdict()

    def _verdict(self) -> str:
        if self.not_run:
            return (f"not classifiable: pytest could not run the node in {self.not_run} of {self.runs} runs "
                    "(missing node, usage or collection error)")
        where = "alone under CPU contention" if self.under_load else "alone without added load"
        if self.failures == self.runs:
            return f"deterministic ({self.runs} of {self.runs} failed {where})"
        if self.failures == 0:
            return f"passed {self.runs} of {self.runs} {where} (fails only with its neighbours or under other load)"
        return f"intermittent, {self.failures} of {self.runs} {where}"


def classify_nodes(nodes: list[str], *, runs: int, timeout: float, under_load: bool = False,
                   deadline: float | None = None, watchdog_scale: int = 1) -> list[Classification]:
    """Rerun each node alone. Exit 1, a crash or a kill counts as a failure;
    exits 2-5 mean the node never ran and make the verdict "not classifiable".
    ``deadline`` (a ``time.monotonic()`` value) stops reruns in time for the job
    to report; the verdict then says how many runs it is based on."""
    results = []
    for node in nodes:
        failures = not_run = done = 0
        for attempt in range(1, runs + 1):
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining < 30:
                break
            code = run_pytest(pytest_command([node], watchdog_scale=watchdog_scale),
                              timeout=timeout if remaining is None else min(timeout, remaining))
            done += 1
            if code in NOT_RUN_CODES:
                not_run += 1
            elif code != 0:
                failures += 1
            log(f"classify {node}: run {attempt}/{runs} -> {code}")
        result = Classification(node, failures, done, not_run, under_load, planned=runs)
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
    classify_nodes(nodes[: args.max_nodes], runs=args.runs, timeout=args.run_timeout_seconds,
                   deadline=time.monotonic() + args.deadline_minutes * 60)
    log("classification never turns a job green: the original failure stands")
    return 1


def plan_stress(files: list[str], *, runs: int, contention: int, budget_seconds: float,
                durations: dict[str, float]) -> tuple[list[str], list[tuple[str, float]], float]:
    """Choose files cheapest first within the budget.

    Cost per file = runs x (measured duration x (contention + 1) + start-up per
    invocation). A file whose own cost exceeds the budget is never admitted,
    even first. Returns (selected, [(skipped, cost)], estimated seconds).
    """
    unmeasured = statistics.median(durations.values()) if durations else 15.0
    slowdown = contention + 1

    def cost(path: str) -> float:
        per_run = durations.get(path, unmeasured) * slowdown + len(invocations_for([path], quiet=True)) * (
            CONTENDED_STARTUP_SECONDS)
        return per_run * runs

    selected: list[str] = []
    skipped: list[tuple[str, float]] = []
    estimate = 0.0
    for path in sorted(files, key=lambda item: (cost(item), item)):
        if estimate + cost(path) > budget_seconds:
            skipped.append((path, cost(path)))
            continue
        selected.append(path)
        estimate += cost(path)
    return selected, skipped, estimate


def lane_stress(args: argparse.Namespace) -> int:
    started_at = time.monotonic()
    # Hard stop for the stress runs; classification and reporting use the rest
    # of --deadline-minutes, which sits below the CI job timeout.
    deadline = started_at + args.deadline_minutes * 60
    runs_deadline = deadline - args.classify_reserve_minutes * 60
    files = list(args.files) or changed_test_files(args.base)
    if not files:
        log(f"no backend test file changed since {args.base}; nothing to stress")
        return 0
    selected, skipped, estimate = plan_stress(
        files, runs=args.runs, contention=args.contention, budget_seconds=args.budget_minutes * 60,
        durations=load_measured_durations(),
    )
    for path, cost in skipped:
        summary(f"NOT STRESSED {path}: estimated {cost / 60:.0f} min for {args.runs} runs, over the "
                f"{args.budget_minutes}-minute stress budget; it still runs once in its shard. "
                f"Stress it locally: determinism_lanes.py stress --files {path}")
    if not selected:
        log("no changed test file fits the stress budget; nothing stressed")
        return 0
    invocations = invocations_for(selected)
    scale = args.contention + 1
    base_seed = args.seed if args.seed is not None else random.SystemRandom().randrange(1, 2**31)
    log(f"stressing {len(selected)} file(s), {args.runs} runs, base seed {base_seed} "
        f"(replay one run with --shuffle-seed=<base+run>), estimated {estimate / 60:.0f} min: {', '.join(selected)}")
    junit_dir = Path(args.junit_dir)
    junit_dir.mkdir(parents=True, exist_ok=True)
    failing_runs: list[tuple[int, str, int]] = []
    completed_runs = 0
    stopped = False
    run_timeout = max(args.run_timeout_seconds, WATCHDOG_TIMEOUT_SECONDS * scale + 60)
    with cpu_contention(args.contention):
        for run in range(1, args.runs + 1):
            seed = base_seed + run
            for label, invocation in invocations:
                remaining = runs_deadline - time.monotonic()
                if remaining < 60:
                    stopped = True
                    break
                report = junit_dir / f"run{run:02d}-{re.sub(r'[^A-Za-z0-9_.-]+', '_', label)}.xml"
                began = time.perf_counter()
                code = run_pytest(pytest_command(invocation, seed=seed, junit=report, watchdog_scale=scale),
                                  timeout=min(run_timeout, remaining))
                log(f"run {run}/{args.runs} seed {seed} {label} -> {code} ({time.perf_counter() - began:.1f}s)")
                if code == NO_TESTS_COLLECTED:
                    log(f"{label}: no test ran (all quarantined or none selected); not a failure")
                elif code != 0:
                    failing_runs.append((run, label, seed))
            if stopped:
                break
            completed_runs = run
        if stopped:
            summary(f"STRESS STOPPED at the {args.deadline_minutes - args.classify_reserve_minutes}-minute limit "
                    f"after {completed_runs} complete run(s) of {args.runs}; runs {completed_runs + 1}-{args.runs} "
                    "did not run. The estimate was too low; this is not a full stress pass.")
        if not failing_runs:
            if not stopped:
                log(f"stress ok: {len(selected)} file(s) passed {args.runs} of {args.runs} runs")
            return 0
        for run, label, seed in failing_runs:
            log(f"FAILED run {run} {label} (replay: --shuffle-seed={seed})")
        nodes = failed_nodes(sorted(junit_dir.glob("*.xml")))
        classify_nodes(nodes[: args.max_nodes], runs=args.classify_runs, timeout=run_timeout,
                       under_load=args.contention > 0, deadline=deadline, watchdog_scale=scale)
    log(f"stress failed: {len(failing_runs)} failing run(s)")
    return 1


def junit_outcomes(junit_paths: list[Path]) -> list[tuple[str, str]]:
    """(node id, outcome) for every reported test case, collection errors included."""
    outcomes: list[tuple[str, str]] = []
    for path in junit_paths:
        try:
            root = ElementTree.parse(path).getroot()
        except (OSError, ElementTree.ParseError):
            outcomes.append((str(path), "unreadable report"))
            continue
        for case in root.iter("testcase"):
            outcome = next((kind for kind in ("failure", "error", "skipped") if case.find(kind) is not None), "passed")
            outcomes.append((node_id(case.get("classname", ""), case.get("name", "")), outcome))
    return outcomes


def lane_summarize(args: argparse.Namespace) -> int:
    """Write one line per reported node to the job summary, so a lane that broke
    at collection or setup is visible instead of an empty green-looking step."""
    directory = Path(args.junit_dir)
    outcomes = junit_outcomes(sorted(directory.rglob("*.xml")) if directory.is_dir() else [])
    lines = [f"### {args.title}", ""]
    if not outcomes:
        lines.append("No test case was reported: the lane failed before any test ran "
                     "(collection, setup or environment). See the step log.")
    else:
        lines += ["| Node | Outcome |", "| --- | --- |"]
        lines += [f"| `{node}` | {outcome} |" for node, outcome in outcomes]
    text = "\n".join(lines) + "\n"
    print(text, flush=True)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
    return 0


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
    stress.add_argument("--budget-minutes", type=int, default=50,
                        help="estimated stress time the chosen files may use")
    stress.add_argument("--deadline-minutes", type=int, default=95,
                        help="hard wall-clock limit for the whole lane, below the CI job timeout")
    stress.add_argument("--classify-reserve-minutes", type=int, default=20,
                        help="part of the deadline kept for classification and reporting")
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
    classify.add_argument("--deadline-minutes", type=int, default=25,
                          help="stop rerunning after this long, inside the step timeout; the verdict says so")
    classify.set_defaults(handler=lane_classify)

    summarize = sub.add_parser("summarize", help="write per-node outcomes from JUnit reports to the job summary")
    summarize.add_argument("--junit-dir", required=True)
    summarize.add_argument("--title", default="Test outcomes")
    summarize.set_defaults(handler=lane_summarize)

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
