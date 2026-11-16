"""Contract tests for scripts/check_test_determinism.py (#1218)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT / "scripts/check_test_determinism.py"


def load_checker():
    spec = importlib.util.spec_from_file_location("check_test_determinism", CHECK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


checker = load_checker()


def run_check(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECK), *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def make_tree(tmp_path: Path, files: dict[str, str]) -> tuple[Path, Path]:
    tests = tmp_path / "backend/tests"
    for name, source in files.items():
        path = tests / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
    return tests, tmp_path / "baseline.json"


def counts(tmp_path: Path, files: dict[str, str]) -> dict[str, dict[str, int]]:
    tests, _ = make_tree(tmp_path, files)
    result = checker.scan(tests, tmp_path)
    assert result.errors == []
    return result.counts()


def run_tree(tests: Path, root: Path, baseline: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return run_check("--tests-dir", str(tests), "--root", str(root), "--baseline", str(baseline), *extra)


def test_repository_tests_match_checked_in_baseline() -> None:
    result = run_check()
    assert result.returncode == 0, result.stderr
    assert "test determinism: ok" in result.stdout


def test_one_added_time_sleep_fails_and_names_the_line(tmp_path: Path) -> None:
    tests, baseline = make_tree(tmp_path, {"test_a.py": "def test_a():\n    assert True\n"})
    assert run_tree(tests, tmp_path, baseline, "--update").returncode == 0
    assert run_tree(tests, tmp_path, baseline).returncode == 0

    (tests / "test_a.py").write_text("import time\n\ndef test_a():\n    time.sleep(0.1)\n    assert True\n")
    result = run_tree(tests, tmp_path, baseline)

    assert result.returncode == 1
    assert "backend/tests/test_a.py: sleep count 1 is above the baseline 0" in result.stderr
    assert "backend/tests/test_a.py:4: time.sleep(0.1)" in result.stderr


def test_count_below_baseline_fails_until_lowered_and_update_never_raises(tmp_path: Path) -> None:
    two = "import time\n\ndef test_a():\n    time.sleep(1)\n    time.sleep(2)\n"
    tests, baseline = make_tree(tmp_path, {"test_a.py": two})
    assert run_tree(tests, tmp_path, baseline, "--update").returncode == 0

    (tests / "test_a.py").write_text("import time\n\ndef test_a():\n    time.sleep(1)\n")
    below = run_tree(tests, tmp_path, baseline)
    assert below.returncode == 1
    assert "sleep count 1 is below the baseline 2" in below.stderr

    assert run_tree(tests, tmp_path, baseline, "--update").returncode == 0
    assert json.loads(baseline.read_text())["files"] == {"backend/tests/test_a.py": {"sleep": 1}}
    assert run_tree(tests, tmp_path, baseline).returncode == 0

    (tests / "test_a.py").write_text(two)
    raised = run_tree(tests, tmp_path, baseline, "--update")
    assert raised.returncode == 1
    assert "never raises" in raised.stderr
    assert json.loads(baseline.read_text())["files"] == {"backend/tests/test_a.py": {"sleep": 1}}


def test_deleted_file_with_baseline_entry_fails_until_lowered(tmp_path: Path) -> None:
    tests, baseline = make_tree(tmp_path, {"test_a.py": "import time\ntime.sleep(1)\n"})
    assert run_tree(tests, tmp_path, baseline, "--update").returncode == 0
    (tests / "test_a.py").unlink()
    result = run_tree(tests, tmp_path, baseline)
    assert result.returncode == 1
    assert "backend/tests/test_a.py: has a baseline entry but no longer exists" in result.stderr


def test_sleep_rules(tmp_path: Path) -> None:
    found = counts(tmp_path, {"test_a.py": """
        import asyncio
        import time
        from time import sleep as nap
        from asyncio import sleep

        async def test_a(delay):
            await asyncio.sleep(0)
            await asyncio.sleep(0.0)
            await asyncio.sleep(0.01)
            await asyncio.sleep(delay)
            await sleep(1)
            time.sleep(0)
            nap(1)
            script = "import time; time.sleep(5)"
    """})
    # Both literal-zero asyncio yields and the child-script string are not counted.
    assert found == {"backend/tests/test_a.py": {"sleep": 5}}


def test_clock_compare_follows_names_in_scope(tmp_path: Path) -> None:
    found = counts(tmp_path, {"test_a.py": """
        import asyncio
        import time
        from time import monotonic

        def test_elapsed():
            started = time.perf_counter()
            elapsed = time.perf_counter() - started
            assert elapsed < 0.5
            assert round(elapsed, 2) <= 1

        def test_deadline():
            deadline = monotonic() + 5
            while monotonic() < deadline:
                break

        async def test_loop():
            loop_deadline = asyncio.get_running_loop().time() + 1
            assert asyncio.get_running_loop().time() < loop_deadline

        def test_not_clock():
            started = time.monotonic()
            record = {"started": started}
            assert record == {"started": started}
            assert len([1]) == 1
    """})
    # record == {...} receives a clock value but is not a clock comparison.
    assert found == {"backend/tests/test_a.py": {"clock-compare": 4}}


def test_isolation_and_process_rules(tmp_path: Path) -> None:
    found = counts(tmp_path, {
        "test_a.py": """
            import os
            import subprocess
            import pytest
            from subprocess import Popen
            from unittest.mock import patch
            from src.config.settings import settings

            def test_a(monkeypatch):
                settings.workspace_dir = "/x"
                os.environ["X"] = "1"
                monkeypatch.setenv("Y", "1")
                monkeypatch.setattr(settings, "workspace_dir", "/y")
                monkeypatch.setattr(module, "get_session", object())
                with patch("src.api.mail.get_session"):
                    pass
                subprocess.Popen(["true"])
                Popen(["true"])
                subprocess.run(["true"])
                try:
                    subprocess.run(["true"], timeout=1)
                except subprocess.TimeoutExpired:
                    pytest.skip("slow host")
                except ValueError:
                    pytest.skip("not a timeout")
        """,
        "support/spawned.py": """
            import subprocess
            from unittest.mock import patch

            def spawn():
                patch("src.api.mail.get_session")
                return subprocess.Popen(["true"])
        """,
    })
    assert found == {
        "backend/tests/test_a.py": {
            "timeout-skip": 1,
            "settings-assign": 1,
            "environ-assign": 1,
            "get-session-patch": 2,
            "popen": 2,
        }
    }


def test_realtime_marker_with_reason_is_the_only_exemption(tmp_path: Path) -> None:
    found = counts(tmp_path, {
        "test_a.py": """
            import time
            import pytest

            @pytest.mark.realtime(reason="kernel signal delivery")
            def test_exempt():
                time.sleep(1)

            @pytest.mark.realtime(reason="real browser")
            class TestExempt:
                def test_inner(self):
                    time.sleep(1)

            @pytest.mark.slow
            def test_other_marker_is_not_an_exemption():
                time.sleep(1)
        """,
        "test_b.py": """
            import time
            import pytest

            pytestmark = [pytest.mark.asyncio, pytest.mark.realtime(reason="SQLite busy handler")]

            def test_module_exempt():
                time.sleep(1)
        """,
    })
    # Each realtime marker is itself ratcheted.
    assert found == {"backend/tests/test_a.py": {"sleep": 1, "realtime": 2},
                     "backend/tests/test_b.py": {"realtime": 1}}


def test_realtime_exempts_only_timing_categories(tmp_path: Path) -> None:
    found = counts(tmp_path, {"test_a.py": """
        import os
        import subprocess
        import time
        import pytest

        @pytest.mark.realtime(reason="kernel signal delivery")
        def test_a():
            started = time.monotonic()
            time.sleep(1)
            assert time.monotonic() - started >= 1
            os.environ["X"] = "1"
            subprocess.Popen(["true"])
    """})
    assert found == {"backend/tests/test_a.py": {"environ-assign": 1, "popen": 1, "realtime": 1}}


def test_rename_moves_the_baseline_entry_and_a_missing_file_says_how(tmp_path: Path) -> None:
    tests, baseline = make_tree(tmp_path, {"test_old.py": "import time\ntime.sleep(1)\n"})
    assert run_tree(tests, tmp_path, baseline, "--update").returncode == 0
    (tests / "test_old.py").rename(tests / "test_new.py")
    missing = run_tree(tests, tmp_path, baseline)
    assert missing.returncode == 1
    assert "backend/tests/test_old.py: has a baseline entry but no longer exists" in missing.stderr
    assert "--rename backend/tests/test_old.py <new path>" in missing.stderr

    renamed = run_tree(tests, tmp_path, baseline, "--rename", "backend/tests/test_old.py", "backend/tests/test_new.py")
    assert renamed.returncode == 0, renamed.stderr
    assert json.loads(baseline.read_text())["files"] == {"backend/tests/test_new.py": {"sleep": 1}}
    assert run_tree(tests, tmp_path, baseline).returncode == 0
    unknown = run_tree(tests, tmp_path, baseline, "--rename", "backend/tests/test_gone.py",
                       "backend/tests/test_new.py")
    assert unknown.returncode == 1 and "has no baseline entry" in unknown.stderr
    absent = run_tree(tests, tmp_path, baseline, "--rename", "backend/tests/test_new.py", "backend/tests/x.py")
    assert absent.returncode == 1 and "backend/tests/x.py does not exist" in absent.stderr


def test_realtime_marker_without_reason_is_an_error(tmp_path: Path) -> None:
    tests, baseline = make_tree(tmp_path, {"test_a.py": """
        import time
        import pytest

        @pytest.mark.realtime
        def test_a():
            time.sleep(1)

        @pytest.mark.realtime(reason="  ")
        def test_b():
            time.sleep(1)
    """})
    result = checker.scan(tests, tmp_path)
    assert len(result.errors) == 2
    assert all("requires a non-empty reason" in error for error in result.errors)
    assert result.counts() == {"backend/tests/test_a.py": {"sleep": 2}}
    baseline.write_text(json.dumps({"files": {"backend/tests/test_a.py": {"sleep": 2}}}))
    assert run_tree(tests, tmp_path, baseline).returncode == 1


def test_realtime_marker_is_registered_for_strict_markers() -> None:
    pyproject = (ROOT / "backend/pyproject.toml").read_text(encoding="utf-8")
    assert '"realtime(reason):' in pyproject
    assert "--strict-markers" in pyproject


def test_unittest_and_isclose_elapsed_asserts_count_but_identity_checks_do_not(tmp_path: Path) -> None:
    found = counts(tmp_path, {"test_a.py": """
        import math
        import time
        import unittest

        class TestA(unittest.TestCase):
            def test_a(self):
                started = time.monotonic()
                self.assertLess(time.monotonic() - started, 1)
                self.assertGreaterEqual(started, 0)
                self.assertEqual(started, started)
                assert math.isclose(time.monotonic() - started, 0, abs_tol=1)
                assert started is not None
                self.assertLess(1, 2)
    """})
    # assertLess, assertGreaterEqual and isclose read the clock; assertEqual,
    # ``is not None`` and a constant assertLess do not.
    assert found == {"backend/tests/test_a.py": {"clock-compare": 3}}


def test_ambient_state_mutators_count(tmp_path: Path) -> None:
    found = counts(tmp_path, {"test_a.py": """
        import os
        from unittest.mock import patch
        from config.settings import settings as cfg

        MODULE = "src.api.mail"

        def test_a(monkeypatch):
            os.environ.update({"A": "1"})
            os.environ.setdefault("B", "1")
            os.environ.pop("A", None)
            del os.environ["B"]
            os.environ |= {"C": "1"}
            os.getenv("A")
            setattr(cfg, "workspace_dir", "/x")
            cfg.workspace_dir = "/y"
            monkeypatch.setattr(cfg, "workspace_dir", "/z")
            with patch(f"{MODULE}.get_session"):
                pass
    """})
    assert found == {"backend/tests/test_a.py": {
        "settings-assign": 2, "environ-assign": 5, "get-session-patch": 1}}


def test_update_refuses_to_write_while_a_file_fails_to_scan(tmp_path: Path) -> None:
    tests, baseline = make_tree(tmp_path, {"test_a.py": "import time\ntime.sleep(1)\n"})
    assert run_tree(tests, tmp_path, baseline, "--update").returncode == 0
    before = baseline.read_text()
    (tests / "test_broken.py").write_text("def broken(:\n")
    result = run_tree(tests, tmp_path, baseline, "--update")
    assert result.returncode == 1
    assert "refuses to write" in result.stderr
    assert baseline.read_text() == before


def test_snippet_lines_follow_ast_line_numbers(tmp_path: Path) -> None:
    tests, _ = make_tree(tmp_path, {"test_a.py": "x = ' '\nimport time\ntime.sleep(1)\n"})
    result = checker.scan(tests, tmp_path)
    assert [(finding.line, finding.snippet) for finding in result.findings] == [(3, "time.sleep(1)")]


def test_quarantine_marker_must_name_an_issue(tmp_path: Path) -> None:
    tests, _ = make_tree(tmp_path, {"test_a.py": """
        import pytest

        @pytest.mark.quarantine(issue=1234)
        def test_named():
            pass

        @pytest.mark.quarantine(issue="#1235")
        def test_named_string():
            pass

        @pytest.mark.quarantine
        def test_bare():
            pass

        @pytest.mark.quarantine(issue="soon")
        def test_not_an_issue():
            pass

        @pytest.mark.quarantine(reason="flaky")
        def test_reason_only():
            pass
    """})
    result = checker.scan(tests, tmp_path)
    assert [issue for _, _, issue in result.quarantined] == ["1234", "1235"]
    assert len(result.errors) == 3
    assert all("pytest.mark.quarantine requires issue=" in error for error in result.errors)


def test_quarantine_is_capped(tmp_path: Path) -> None:
    body = "import pytest\n\n" + "".join(
        f"@pytest.mark.quarantine(issue={1000 + index})\ndef test_{index}():\n    pass\n\n"
        for index in range(checker.QUARANTINE_CAP + 1)
    )
    tests, _ = make_tree(tmp_path, {"test_a.py": body})
    result = checker.scan(tests, tmp_path)
    assert len(result.quarantined) == checker.QUARANTINE_CAP + 1
    assert any("exceed the cap of 10" in error for error in result.errors)


def test_quarantine_only_on_a_test_function_decorator(tmp_path: Path) -> None:
    tests, _ = make_tree(tmp_path, {"test_a.py": """
        import pytest

        pytestmark = [pytest.mark.quarantine(issue=1)]
        held = pytest.mark.quarantine(issue=2)

        @pytest.mark.quarantine(issue=3)
        class TestA:
            def test_a(self):
                pass

        @pytest.mark.parametrize("x", [pytest.param(1, marks=pytest.mark.quarantine(issue=4))])
        def test_param(x):
            pass

        @pytest.mark.quarantine(issue=5)
        def test_ok():
            pass
    """})
    result = checker.scan(tests, tmp_path)
    assert sum("not a class or module" in error for error in result.errors) == 2
    assert sum("only supported as a decorator on a test function" in error for error in result.errors) == 2
    assert ("backend/tests/test_a.py", 17, "5") in result.quarantined


def test_lane_file_listings(tmp_path: Path) -> None:
    tests, baseline = make_tree(tmp_path, {
        "test_q.py": "import pytest\n\n@pytest.mark.quarantine(issue=7)\ndef test_a():\n    pass\n",
        "test_r.py": "import pytest\n\n@pytest.mark.realtime(reason='kernel signal')\ndef test_a():\n    pass\n",
        "test_plain.py": "def test_a():\n    pass\n",
    })
    quarantined = run_tree(tests, tmp_path, baseline, "--quarantined-files")
    realtime = run_tree(tests, tmp_path, baseline, "--realtime-files")
    assert quarantined.returncode == realtime.returncode == 0
    assert quarantined.stdout.split() == ["tests/test_q.py"]
    assert realtime.stdout.split() == ["tests/test_r.py"]


def test_quarantine_marker_is_registered_for_strict_markers() -> None:
    pyproject = (ROOT / "backend/pyproject.toml").read_text(encoding="utf-8")
    assert '"quarantine(issue):' in pyproject


def test_marker_aliases_are_resolved(tmp_path: Path) -> None:
    tests, _ = make_tree(tmp_path, {"test_a.py": """
        import time
        import pytest as pt
        from pytest import mark

        @pt.mark.quarantine(issue=12)
        def test_alias():
            pass

        @mark.quarantine
        def test_bare_from_import():
            pass

        held = pt.mark.quarantine(issue=13)

        @mark.realtime(reason="real browser")
        def test_realtime_alias():
            time.sleep(1)
    """})
    result = checker.scan(tests, tmp_path)
    assert [issue for _, _, issue in result.quarantined] == ["12"]
    assert sum("requires issue=" in error for error in result.errors) == 1
    assert sum("only supported as a decorator on a test function" in error for error in result.errors) == 1
    assert result.counts() == {"backend/tests/test_a.py": {"realtime": 1}}
    assert result.realtime_files == ["backend/tests/test_a.py"]


def test_clock_seam_modules_have_no_direct_clock_reads_or_timers(tmp_path: Path) -> None:
    """#1225: an adopted module that reads time or arms a timer directly fails the check."""
    assert checker.scan_clock_seam(ROOT / "backend/src", ROOT) == []
    module = tmp_path / "src/adopted.py"
    module.parent.mkdir(parents=True)
    module.write_text(textwrap.dedent("""\
        import asyncio
        import time
        from datetime import date, datetime, timezone
        from time import perf_counter

        async def work(loop, task):
            clean = await asyncio.sleep(0), await asyncio.wait({task})
            time.monotonic()
            datetime.now(timezone.utc)
            date.today()
            perf_counter()
            loop.time()
            await asyncio.sleep(1)
            await asyncio.wait({task}, timeout=2)
            async with asyncio.timeout(3):
                pass
            waiter = asyncio.wait_for
            await asyncio.wait({task}, **{"timeout": 1})
            sleeper = asyncio.sleep
            time.sleep(1)
            loop.call_later(1, print)
    """), encoding="utf-8")
    errors = checker.scan_clock_seam(tmp_path / "src", tmp_path, modules=("adopted.py",))
    assert [int(error.split(":")[1]) for error in errors] == [8, 9, 10, 11, 12, 13, 14, 15, 17, 18, 19, 20, 21]
    assert all("use src.utils.clock.get_clock()" in error for error in errors)
    missing = checker.scan_clock_seam(tmp_path / "src", tmp_path, modules=("gone.py",))
    assert len(missing) == 1 and "missing" in missing[0]
