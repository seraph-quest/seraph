"""Contract tests for the #1220 stress, order, quarantine and classification lanes."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import scripts.determinism_lanes as lanes

BACKEND = Path(__file__).resolve().parents[1]


def run_plugin_pytest(tmp_path: Path, source: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run pytest on a scratch module with only this repo's lane plugins importable."""
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    quarantine(issue): quarantined\n", encoding="utf-8"
    )
    (tmp_path / "test_scratch.py").write_text(textwrap.dedent(source), encoding="utf-8")
    environment = {**os.environ, "PYTHONPATH": str(BACKEND)}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-v", "-rs", "test_scratch.py", *args],
        cwd=tmp_path, env=environment, capture_output=True, text=True, check=False,
    )


ORDERED = "".join(f"def test_{index:02d}():\n    pass\n\n" for index in range(12))


def collected_order(output: str) -> list[str]:
    return [line.split("::", 1)[1].split()[0] for line in output.splitlines()
            if line.startswith("test_scratch.py::")]


def test_shuffle_seed_is_printed_and_replays_the_same_order(tmp_path: Path) -> None:
    first = run_plugin_pytest(tmp_path, ORDERED, "-p", "scripts.pytest_shuffle", "--shuffle-seed=7")
    again = run_plugin_pytest(tmp_path, ORDERED, "-p", "scripts.pytest_shuffle", "--shuffle-seed=7")
    other = run_plugin_pytest(tmp_path, ORDERED, "-p", "scripts.pytest_shuffle", "--shuffle-seed=8")
    plain = run_plugin_pytest(tmp_path, ORDERED, "-p", "scripts.pytest_shuffle")

    assert first.returncode == again.returncode == other.returncode == plain.returncode == 0
    assert "shuffle seed: 7 (replay with --shuffle-seed=7)" in first.stdout
    assert collected_order(first.stdout) == collected_order(again.stdout)
    assert collected_order(first.stdout) != collected_order(other.stdout)
    assert collected_order(plain.stdout) == [f"test_{index:02d}" for index in range(12)]
    assert sorted(collected_order(first.stdout)) == collected_order(plain.stdout)


QUARANTINED = """
    import pytest

    @pytest.mark.quarantine(issue=1234)
    def test_quarantined():
        assert False, "intermittent"

    def test_ordinary():
        pass
"""


def test_quarantine_exclude_skips_with_the_issue_and_runs_everything_else(tmp_path: Path) -> None:
    result = run_plugin_pytest(tmp_path, QUARANTINED, "-p", "scripts.pytest_quarantine", "--quarantine=exclude")
    assert result.returncode == 0, result.stdout
    assert "test_ordinary PASSED" in result.stdout
    assert "quarantined (#1234); runs in the non-blocking quarantine lane" in result.stdout


def test_quarantine_only_runs_just_the_quarantined_tests(tmp_path: Path) -> None:
    result = run_plugin_pytest(tmp_path, QUARANTINED, "-p", "scripts.pytest_quarantine", "--quarantine=only")
    assert result.returncode == 1
    assert "test_quarantined FAILED" in result.stdout
    assert "test_ordinary" not in result.stdout.split("short test summary")[0].replace("deselected", "")
    assert "1 deselected" in result.stdout


def test_without_a_mode_quarantined_tests_run_like_any_other(tmp_path: Path) -> None:
    result = run_plugin_pytest(tmp_path, QUARANTINED, "-p", "scripts.pytest_quarantine")
    assert result.returncode == 1
    assert "test_quarantined FAILED" in result.stdout and "test_ordinary PASSED" in result.stdout


def test_quarantine_without_an_issue_is_a_usage_error(tmp_path: Path) -> None:
    source = "import pytest\n\n@pytest.mark.quarantine\ndef test_a():\n    pass\n"
    result = run_plugin_pytest(tmp_path, source, "-p", "scripts.pytest_quarantine", "--quarantine=exclude")
    assert result.returncode == 4
    assert "pytest.mark.quarantine requires issue=<number>" in result.stderr + result.stdout


def test_node_id_rebuilds_module_class_and_parameter() -> None:
    assert lanes.node_id("tests.test_check_test_determinism", "test_sleep_rules") == (
        "tests/test_check_test_determinism.py::test_sleep_rules"
    )
    assert lanes.node_id("tests.test_chat_api.TestChatAPI", "test_x[a-b]") == (
        "tests/test_chat_api.py::TestChatAPI::test_x[a-b]"
    )


def test_failed_nodes_reads_failures_and_errors_once_and_skips_runner_kills(tmp_path: Path) -> None:
    report = tmp_path / "report.xml"
    report.write_text(
        '<testsuites><testsuite name="s">'
        '<testcase classname="tests.test_chat_api.TestChatAPI" name="test_a"><failure message="x"/></testcase>'
        '<testcase classname="tests.test_chat_api.TestChatAPI" name="test_a"><error message="teardown"/></testcase>'
        '<testcase classname="tests.test_chat_api" name="test_ok"/>'
        '<testcase classname="tests/test_slow.py" name="shard-runner-timeout"><error message="killed"/></testcase>'
        '<testcase classname="tests/test_capabilities_api.py" name="invocation-crashed"><error message="x"/></testcase>'
        '<testcase classname="" name="tests.test_chat_api"><error message="collection error"/></testcase>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    assert lanes.failed_nodes([report]) == ["tests/test_chat_api.py::TestChatAPI::test_a", "tests/test_chat_api.py"]


@pytest.mark.parametrize(
    ("failures", "not_run", "under_load", "verdict"),
    [(20, 0, False, "deterministic (20 of 20 failed alone without added load)"),
     (3, 0, True, "intermittent, 3 of 20 alone under CPU contention"),
     (0, 0, False, "passed 20 of 20 alone without added load (fails only with its neighbours or under other load)"),
     (0, 20, False, "not classifiable: pytest could not run the node in 20 of 20 runs "
                    "(missing node, usage or collection error)")],
)
def test_classification_verdicts(failures: int, not_run: int, under_load: bool, verdict: str) -> None:
    assert lanes.Classification("tests/test_a.py::test_a", failures, 20, not_run, under_load).verdict == verdict


def test_classify_counts_only_real_failures(monkeypatch) -> None:
    codes = iter([1, 124, -9, 0, 4])
    monkeypatch.setattr(lanes, "run_pytest", lambda command, *, timeout: next(codes))
    [result] = lanes.classify_nodes(["tests/test_a.py::test_a"], runs=5, timeout=1)
    assert (result.failures, result.not_run) == (3, 1)
    assert result.verdict.startswith("not classifiable")


def test_classify_reruns_each_node_alone_and_never_turns_the_job_green(tmp_path: Path, monkeypatch, capsys) -> None:
    (tmp_path / "r.xml").write_text(
        '<testsuites><testsuite name="s"><testcase classname="tests.test_chat_api" name="test_a">'
        '<failure message="x"/></testcase></testsuite></testsuites>',
        encoding="utf-8",
    )
    codes = iter([1, 0, 1, 0, 0])
    commands: list[list[str]] = []

    def fake_run(command: list[str], *, timeout: int) -> int:
        commands.append(command)
        return next(codes)

    monkeypatch.setattr(lanes, "run_pytest", fake_run)
    assert lanes.main(["classify", "--junit-dir", str(tmp_path), "--runs", "5"]) == 1
    assert len(commands) == 5
    assert all(command[-1] == "tests/test_chat_api.py::test_a" for command in commands)
    assert "CLASSIFIED tests/test_chat_api.py::test_a: intermittent, 2 of 5 alone without added load" in (
        capsys.readouterr().out)


def test_classify_with_no_failed_case_keeps_an_already_failed_job_red(tmp_path: Path) -> None:
    assert lanes.main(["classify", "--junit-dir", str(tmp_path), "--expect-failure"]) == 1
    assert lanes.main(["classify", "--junit-dir", str(tmp_path)]) == 0


def test_stress_runs_every_file_n_times_with_printed_seeds_and_fails_on_one_red_run(
        tmp_path: Path, monkeypatch, capsys) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, timeout: int) -> int:
        calls.append(command)
        return 1 if len(calls) == 3 else 0

    monkeypatch.setattr(lanes, "run_pytest", fake_run)
    monkeypatch.setattr(lanes, "failed_nodes", lambda reports: [])
    code = lanes.main(["stress", "--files", "tests/test_check_test_determinism.py", "--runs", "4",
                       "--seed", "100", "--contention", "0", "--junit-dir", str(tmp_path)])
    output = capsys.readouterr().out
    assert code == 1
    assert len(calls) == 4
    assert [next(arg for arg in call if arg.startswith("--shuffle-seed=")) for call in calls] == [
        "--shuffle-seed=101", "--shuffle-seed=102", "--shuffle-seed=103", "--shuffle-seed=104"]
    assert all("--quarantine=exclude" in call for call in calls)
    assert "base seed 100" in output
    assert "FAILED run 3 tests/test_check_test_determinism.py (replay: --shuffle-seed=103)" in output


def test_stress_with_no_changed_test_file_passes(monkeypatch, capsys) -> None:
    monkeypatch.setattr(lanes, "changed_test_files", lambda base: [])
    assert lanes.main(["stress", "--base", "origin/develop"]) == 0
    assert "nothing to stress" in capsys.readouterr().out


def test_stress_applies_the_shard_runner_retirement(monkeypatch) -> None:
    invocations = lanes.invocations_for(["tests/test_eval_harness.py", "tests/test_operator_api.py"])
    labels = [label for label, _ in invocations]
    assert not any(label.startswith("tests/test_eval_harness.py") for label in labels)
    assert all(label.startswith("tests/test_operator_api.py") for label in labels)
    assert any(arg.startswith("--deselect=tests/test_operator_api.py::") for _, args in invocations for arg in args)


def test_changed_test_files_lists_added_and_modified_backend_tests_only(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "ci@example.invalid")
    git("config", "user.name", "ci")
    tests = tmp_path / "backend/tests"
    tests.mkdir(parents=True)
    (tests / "test_kept.py").write_text("x = 1\n")
    (tests / "test_removed.py").write_text("x = 1\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    git("checkout", "-qb", "change")
    (tests / "test_kept.py").write_text("x = 2\n")
    (tests / "test_new.py").write_text("x = 1\n")
    (tests / "helper.py").write_text("x = 1\n")
    (tests / "test_removed.py").unlink()
    git("add", "-A")
    git("commit", "-qm", "change")
    assert lanes.changed_test_files("main", repo_root=tmp_path) == ["tests/test_kept.py", "tests/test_new.py"]


def test_cpu_contention_starts_and_stops_its_busy_processes() -> None:
    with lanes.cpu_contention(0) as count:
        assert count == 0


def test_stress_budget_accounts_for_contention_and_reports_skipped_files(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(lanes, "load_measured_durations", lambda: {"tests/test_a.py": 30.0, "tests/test_b.py": 30.0})
    monkeypatch.setattr(lanes, "invocations_for", lambda files, **_: [(path, [path]) for path in files])
    monkeypatch.setattr(lanes, "run_pytest", lambda command, *, timeout: 0)
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    # (30 s x (2 + 1) + 8 s start-up) x 20 runs = 33 minutes per file: the second exceeds 45.
    code = lanes.main(["stress", "--files", "tests/test_a.py", "tests/test_b.py", "--budget-minutes", "45",
                       "--contention", "2", "--seed", "1", "--junit-dir", str(tmp_path / "j")])
    output = capsys.readouterr().out
    assert code == 0
    assert "::warning::NOT STRESSED tests/test_b.py" in output
    assert "NOT STRESSED tests/test_b.py" in summary_file.read_text()


def test_every_node_of_a_fully_retired_invocation_is_individually_retired() -> None:
    """The single-process lane relies on this to apply the same retirement (#1220)."""
    from scripts.run_backend_test_shard import (
        CANCELLED_EVAL_CONSUMER_NODES, FULLY_RETIRED_EVAL_INVOCATIONS, SPECIALIZED_TEST_INVOCATIONS,
    )
    for path, label in FULLY_RETIRED_EVAL_INVOCATIONS:
        args = next(args for name, args in SPECIALIZED_TEST_INVOCATIONS[path] if name == label)
        collected = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-cov", "-p", "no:cacheprovider", *args],
            cwd=BACKEND, capture_output=True, text=True, check=False,
        ).stdout
        nodes = {line.split("[", 1)[0] for line in collected.splitlines() if "::" in line}
        assert nodes, label
        assert nodes <= set(CANCELLED_EVAL_CONSUMER_NODES[path]), label


def test_stress_budget_stresses_the_cheapest_files_first(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(lanes, "load_measured_durations",
                        lambda: {"tests/test_heavy.py": 600.0, "tests/test_light.py": 5.0, "tests/test_mid.py": 10.0})
    monkeypatch.setattr(lanes, "invocations_for", lambda files, **_: [(path, [path]) for path in files])
    monkeypatch.setattr(lanes, "run_pytest", lambda command, *, timeout: 0)
    code = lanes.main(["stress", "--files", "tests/test_heavy.py", "tests/test_light.py", "tests/test_unmeasured.py",
                       "--budget-minutes", "60", "--contention", "2", "--seed", "1", "--junit-dir", str(tmp_path)])
    output = capsys.readouterr().out
    assert code == 0
    # light: (5 x 3 + 8) x 20 = 8 min; unmeasured uses the median (10 s): 13 min; heavy: 600 min.
    assert "stressing 2 file(s)" in output and "tests/test_light.py, tests/test_unmeasured.py" in output
    assert "NOT STRESSED tests/test_heavy.py" in output


def test_a_file_bigger_than_the_whole_budget_is_never_admitted_even_first(monkeypatch) -> None:
    monkeypatch.setattr(lanes, "invocations_for", lambda files, **_: [(path, [path]) for path in files])
    selected, skipped, estimate = lanes.plan_stress(
        ["tests/test_repo_work_task_publication.py"], runs=20, contention=2, budget_seconds=50 * 60,
        durations={"tests/test_repo_work_task_publication.py": 410.0},
    )
    assert selected == [] and estimate == 0.0
    assert skipped == [("tests/test_repo_work_task_publication.py", (410.0 * 3 + 8.0) * 20)]


def test_start_up_is_charged_per_invocation(monkeypatch) -> None:
    monkeypatch.setattr(lanes, "invocations_for",
                        lambda files, **_: [(f"{path}::{part}", [path]) for path in files for part in "abc"])
    _, _, estimate = lanes.plan_stress(["tests/test_a.py"], runs=20, contention=2, budget_seconds=10**6,
                                       durations={"tests/test_a.py": 10.0})
    assert estimate == (10.0 * 3 + 3 * 8.0) * 20


def test_stress_stops_at_the_wall_clock_limit_and_says_what_did_not_run(tmp_path: Path, monkeypatch, capsys) -> None:
    clock = iter(range(0, 10**6, 1000))  # every reading is 1000 s later
    monkeypatch.setattr(lanes.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(lanes, "run_pytest", lambda command, *, timeout: 0)
    code = lanes.main(["stress", "--files", "tests/test_check_test_determinism.py", "--runs", "20",
                       "--contention", "0", "--seed", "1", "--deadline-minutes", "95",
                       "--classify-reserve-minutes", "20", "--junit-dir", str(tmp_path)])
    output = capsys.readouterr().out
    assert code == 0
    assert "::warning::STRESS STOPPED at the 75-minute limit" in output
    assert "this is not a full stress pass" in output
    assert "stress ok" not in output


def test_stress_treats_no_tests_collected_as_not_a_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(lanes, "run_pytest", lambda command, *, timeout: lanes.NO_TESTS_COLLECTED)
    code = lanes.main(["stress", "--files", "tests/test_check_test_determinism.py", "--runs", "2",
                       "--contention", "0", "--seed", "1", "--junit-dir", str(tmp_path)])
    assert code == 0
    assert "no test ran (all quarantined or none selected)" in capsys.readouterr().out


def test_stress_scales_the_watchdogs_with_contention(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[list[str], float]] = []
    monkeypatch.setattr(lanes, "run_pytest", lambda command, *, timeout: calls.append((command, timeout)) or 0)
    monkeypatch.setattr(lanes, "cpu_contention", lambda factor: __import__("contextlib").nullcontext(0))
    lanes.main(["stress", "--files", "tests/test_check_test_determinism.py", "--runs", "1",
                "--contention", "2", "--seed", "1", "--junit-dir", str(tmp_path)])
    [(command, timeout)] = calls
    assert command[command.index("-o") + 1] == "timeout=1260"
    assert "faulthandler_timeout=720" in command
    assert timeout >= 1260 + 60 or timeout < 1260  # either the scaled bound or the remaining deadline


def test_classification_stops_at_its_deadline_and_says_so(monkeypatch) -> None:
    clock = iter([0, 10**6])  # one reading per attempt
    monkeypatch.setattr(lanes.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(lanes, "run_pytest", lambda command, *, timeout: 1)
    [result] = lanes.classify_nodes(["tests/test_a.py::test_a"], runs=20, timeout=60, deadline=600)
    assert result.runs == 1
    assert result.verdict == "deterministic (1 of 1 failed alone without added load) (stopped at the job deadline after 1 of 20 runs)"


def test_summarize_lists_each_node_and_flags_a_lane_that_reported_nothing(tmp_path: Path, monkeypatch, capsys) -> None:
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    empty = tmp_path / "empty"
    assert lanes.main(["summarize", "--junit-dir", str(empty), "--title", "Quarantine lane"]) == 0
    assert "No test case was reported" in summary_file.read_text()

    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "q.xml").write_text(
        '<testsuites><testsuite name="s">'
        '<testcase classname="tests.test_chat_api" name="test_a"><failure message="x"/></testcase>'
        '<testcase classname="tests.test_chat_api" name="test_b"/>'
        '<testcase classname="" name="tests.test_chat_api"><error message="collection"/></testcase>'
        "</testsuite></testsuites>", encoding="utf-8")
    assert lanes.main(["summarize", "--junit-dir", str(reports), "--title", "Quarantine lane"]) == 0
    text = summary_file.read_text()
    assert "| `tests/test_chat_api.py::test_a` | failure |" in text
    assert "| `tests/test_chat_api.py::test_b` | passed |" in text
    assert "| `tests/test_chat_api.py` | error |" in text
