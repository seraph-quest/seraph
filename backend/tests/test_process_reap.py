"""Owner-side reap after kill at every supervisor kill site (#1212, #1224).

These run on macOS and Linux with a plain child process. The exact-identity
signal (``repo_supervisor.exact_signal``) needs Linux ``/proc`` and pidfd, so
it is replaced by a recording ``os.kill`` sender; the reap itself, the typed
outcome and the marker record are the production code. Readiness is by pipe:
the child installs its signal disposition before it writes its ready byte, so
no signal can arrive before the child is in the state the test names.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from config.settings import RepoSandboxSettings
from src.execution import repo_supervisor, tool_package_runner
from src.execution import process_reap
from src.execution.process_reap import ProcessReapError, ReapOutcome, kill_then_reap, terminate_then_reap
from src.execution.repo_node import PROFILE as NODE_PROFILE, NodeRepoRepairExecutor
from src.execution.repo_sandbox import LocalRepoRepairExecutor
from tests.support.processes import SpawnedProcesses, spawned  # noqa: F401 - fixture

pytestmark = [
    pytest.mark.filterwarnings("error::ResourceWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]

# The child ignores (or honours) SIGTERM, then signals ready on the inherited
# descriptor and blocks until it is killed.
CHILD = """
import os, signal, sys
mode, fd = sys.argv[1], int(sys.argv[2])
if mode == "ignore_term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
elif mode == "flood_on_term":
    # Cleanup that writes more than a pipe buffer, then exits cleanly.
    def flood(signum, frame):
        sys.stdout.buffer.write(b"x" * (1 << 20))
        sys.stdout.buffer.flush()
        os._exit(0)
    signal.signal(signal.SIGTERM, flood)
os.write(fd, b"R")
os.close(fd)
while True:
    signal.pause()
"""


def _start(spawned: SpawnedProcesses, mode: str) -> subprocess.Popen:
    ready = spawned.ready_pipe()
    process = spawned.popen([sys.executable, "-I", "-c", CHILD, mode, str(ready.child_fd)], ready=ready)
    assert ready.wait() == b"R"
    return process


def _assert_reaped(pid: int) -> None:
    # The owner collected the exit status: no zombie remains to wait for.
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


@pytest.fixture
def recorded_exact_signal(monkeypatch):
    """Deliver through os.kill and record (pid, start, signum) for each call."""

    calls: list[tuple[int, str, int]] = []
    state = {"deliver": True}

    def exact_signal(pid: int, start: str, signum: int) -> bool:
        calls.append((pid, start, signum))
        if not state["deliver"] or not start:
            return False
        os.kill(pid, signum)
        return True

    monkeypatch.setattr(repo_supervisor, "exact_signal", exact_signal)
    return calls, state


# -- the shared helper ----------------------------------------------------


def test_terminate_then_reap_cooperative_child(spawned):
    process = _start(spawned, "honour_term")
    sent: list[int] = []
    outcome = terminate_then_reap(process, send_signal=lambda s: sent.append(s) or os.kill(process.pid, s) or True,
                                  grace_seconds=30)
    assert outcome == ReapOutcome(process.pid, "exited_after_terminate", -signal.SIGTERM, True)
    assert sent == [signal.SIGTERM]
    _assert_reaped(process.pid)


def test_terminate_then_reap_kills_and_reaps_term_ignoring_child(spawned):
    process = _start(spawned, "ignore_term")
    sent: list[int] = []
    outcome = terminate_then_reap(process, send_signal=lambda s: sent.append(s) or os.kill(process.pid, s) or True,
                                  grace_seconds=0)
    assert outcome == ReapOutcome(process.pid, "killed_and_reaped", -signal.SIGKILL, True, True)
    assert outcome.reaped and outcome.as_record()["reaped"] is True
    assert sent == [signal.SIGTERM, signal.SIGKILL]
    _assert_reaped(process.pid)


def test_kill_then_reap_stopped_child(spawned):
    """A SIGSTOPped child (the cleanup_deadline case) is still reapable after SIGKILL."""

    process = _start(spawned, "ignore_term")
    os.kill(process.pid, signal.SIGSTOP)
    os.waitpid(process.pid, os.WUNTRACED)  # the stop is observed, not slept for
    outcome = kill_then_reap(process, send_signal=lambda s: os.kill(process.pid, s) or True)
    assert outcome.status == "killed_and_reaped" and outcome.returncode == -signal.SIGKILL
    _assert_reaped(process.pid)


def test_kill_not_delivered_never_blocks(spawned):
    process = _start(spawned, "ignore_term")
    outcome = terminate_then_reap(process, send_signal=lambda s: False, grace_seconds=0)
    assert outcome == ReapOutcome(process.pid, "kill_not_delivered", None, False, False)
    assert not outcome.reaped and process.poll() is None
    # The fixture owns the child and reaps it at teardown.


def test_already_exited_child_is_not_signalled(spawned):
    process = spawned.popen([sys.executable, "-I", "-c", "pass"])
    assert process.wait() == 0
    sent: list[int] = []
    outcome = terminate_then_reap(process, send_signal=lambda s: sent.append(s) or True, grace_seconds=0)
    assert outcome == ReapOutcome(process.pid, "already_exited", 0)
    assert sent == []
    _assert_reaped(process.pid)


def test_reap_failure_is_typed(spawned, monkeypatch):
    process = _start(spawned, "ignore_term")

    def failing_wait(timeout=None):
        raise OSError("injected wait failure")

    monkeypatch.setattr(process, "wait", failing_wait)
    with pytest.raises(ProcessReapError) as error:
        kill_then_reap(process, send_signal=lambda s: os.kill(process.pid, s) or True)
    assert error.value.outcome.status == "reap_failed"
    assert error.value.outcome.kill_delivered is True
    monkeypatch.undo()  # the fixture's real wait() reaps the killed child


def _unreapable_after_kill(monkeypatch, process: subprocess.Popen) -> list[float | None]:
    """Make the post-SIGKILL wait time out, as for a child in uninterruptible sleep."""

    timeouts: list[float | None] = []
    real_wait = process.wait

    def wait(timeout=None):
        if timeout == process_reap.KILL_REAP_LIVENESS_SECONDS:
            timeouts.append(timeout)
            raise subprocess.TimeoutExpired(process.args, timeout)
        return real_wait(timeout=timeout)

    monkeypatch.setattr(process, "wait", wait)
    return timeouts


def test_kill_then_reap_is_bounded_for_an_unreapable_child(spawned, monkeypatch):
    process = _start(spawned, "ignore_term")
    timeouts = _unreapable_after_kill(monkeypatch, process)
    outcome = kill_then_reap(process, send_signal=lambda s: os.kill(process.pid, s) or True)
    assert outcome == ReapOutcome(process.pid, "kill_delivered_unreaped", None, None, True)
    assert not outcome.reaped and timeouts == [process_reap.KILL_REAP_LIVENESS_SECONDS]
    monkeypatch.undo()  # the fixture's real wait() reaps the killed child


def test_signal_oserror_counts_as_not_delivered(spawned):
    process = _start(spawned, "ignore_term")

    def send(signum):
        raise PermissionError(1, "Operation not permitted")

    outcome = terminate_then_reap(process, send_signal=send, grace_seconds=0)
    assert outcome.status == "kill_not_delivered" and outcome.terminate_delivered is False
    assert outcome.kill_delivered is False and process.poll() is None
    assert [error.split(":")[0] for error in outcome.signal_errors] == ["SIGTERM", "SIGKILL"]
    assert outcome.as_record()["signal_errors"] == list(outcome.signal_errors)


# -- site: LocalRepoRepairExecutor (repo_sandbox iterative supervisor) and
# -- NodeRepoRepairExecutor (repo_node deadline path) share the owner method.


def _executor(kind: str, tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    if kind == "python":
        return LocalRepoRepairExecutor(RepoSandboxSettings(executor_kind="local", enabled=True,
                                                           profile="repo-python-pytest-v1"), workspace_dir=workspace)
    return NodeRepoRepairExecutor(RepoSandboxSettings(profile=NODE_PROFILE), workspace_dir=workspace)


@pytest.mark.parametrize("kind", ["python", "node"])
def test_supervisor_site_kills_reaps_and_records_outcome(kind, tmp_path, spawned, recorded_exact_signal):
    calls, _ = recorded_exact_signal
    executor = _executor(kind, tmp_path)
    process = _start(spawned, "ignore_term")
    executor._write_job_marker("reap-job", {"job_id": "reap-job", "status": "unknown_external_effect",
                                            "cleanup_proven": False, "pid": process.pid})
    record = executor._reap_supervisor_after_failure("reap-job", process, "recorded-start")
    assert record["status"] == "killed_and_reaped" and record["reaped"] is True
    assert record["returncode"] == -signal.SIGKILL and record["marker_recorded"] is True
    assert calls == [(process.pid, "recorded-start", signal.SIGTERM), (process.pid, "recorded-start", signal.SIGKILL)]
    _assert_reaped(process.pid)
    marker = executor._read_job_marker("reap-job")
    assert marker["supervisor_reap"] == {key: value for key, value in record.items() if key != "marker_recorded"}
    # The reap never upgrades the failure: cleanup stays unproven.
    assert marker["status"] == "unknown_external_effect" and marker["cleanup_proven"] is False


@pytest.mark.parametrize("kind", ["python", "node"])
def test_supervisor_site_without_identity_never_signals_or_blocks(kind, tmp_path, spawned, recorded_exact_signal):
    executor = _executor(kind, tmp_path)
    process = _start(spawned, "ignore_term")
    executor._write_job_marker("reap-job", {"job_id": "reap-job", "status": "unknown_external_effect"})
    # An empty recorded start identity is never signalled by exact_signal.
    record = executor._reap_supervisor_after_failure("reap-job", process, "")
    assert record["status"] == "kill_not_delivered" and record["reaped"] is False
    assert process.poll() is None
    assert record["marker_recorded"] is True
    assert executor._read_job_marker("reap-job")["supervisor_reap"]["status"] == "kill_not_delivered"


@pytest.mark.parametrize("kind", ["python", "node"])
def test_supervisor_site_records_signal_error_without_replacing_failure(kind, tmp_path, spawned, monkeypatch):
    """EPERM or a pidfd error from exact_signal is a typed kill_not_delivered, never a new exception."""

    def exact_signal(pid, start, signum):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(repo_supervisor, "exact_signal", exact_signal)
    executor = _executor(kind, tmp_path)
    process = _start(spawned, "ignore_term")
    executor._write_job_marker("reap-job", {"job_id": "reap-job", "status": "unknown_external_effect"})
    record = executor._reap_supervisor_after_failure("reap-job", process, "recorded-start")
    assert record["status"] == "kill_not_delivered" and record["reaped"] is False
    assert len(record["signal_errors"]) == 2 and record["marker_recorded"] is True
    assert executor._read_job_marker("reap-job")["supervisor_reap"]["status"] == "kill_not_delivered"


# -- site: tool_package_runner ------------------------------------------


def test_tool_package_deadline_kill_reaps_supervisor(spawned, recorded_exact_signal):
    calls, _ = recorded_exact_signal
    process = _start(spawned, "ignore_term")
    outcome = tool_package_runner.kill_and_reap_supervisor(process, "recorded-start")
    assert outcome.status == "killed_and_reaped" and outcome.returncode == -signal.SIGKILL
    assert calls == [(process.pid, "recorded-start", signal.SIGKILL)]
    _assert_reaped(process.pid)


def test_tool_package_failure_terminates_then_kills_and_reaps(spawned, recorded_exact_signal):
    calls, _ = recorded_exact_signal
    process = _start(spawned, "ignore_term")
    # The kill moment has passed (deadline_at + grace), so SIGKILL follows SIGTERM.
    outcome = tool_package_runner.terminate_and_reap_supervisor(process, "recorded-start", kill_at=time.time())
    assert outcome.status == "killed_and_reaped" and outcome.terminate_delivered is True
    assert [signum for _, _, signum in calls] == [signal.SIGTERM, signal.SIGKILL]
    _assert_reaped(process.pid)


def test_tool_package_failure_reaps_cooperative_supervisor(spawned, recorded_exact_signal):
    process = _start(spawned, "honour_term")
    outcome = tool_package_runner.terminate_and_reap_supervisor(process, "recorded-start",
                                                                kill_at=time.time() + 30)
    assert outcome.status == "exited_after_terminate" and outcome.returncode == -signal.SIGTERM
    _assert_reaped(process.pid)


def test_tool_package_without_identity_never_signals(spawned, recorded_exact_signal):
    calls, _ = recorded_exact_signal
    process = _start(spawned, "ignore_term")
    assert tool_package_runner.kill_and_reap_supervisor(process, None).status == "kill_not_delivered"
    assert tool_package_runner.terminate_and_reap_supervisor(process, None, kill_at=time.time()).status == "kill_not_delivered"
    assert calls == [] and process.poll() is None


@pytest.mark.asyncio
async def test_tool_package_deadline_unreapable_supervisor_returns_unproven(spawned, recorded_exact_signal,
                                                                            monkeypatch, tmp_path):
    """An unreapable killed supervisor never hangs execute(): the result is unknown, cleanup unproven."""

    started: list[subprocess.Popen] = []
    make = _supervisor_popen(spawned, started)
    timeouts: list[float | None] = []

    def supervisor_popen(args, **kwargs):
        process = make(args, **kwargs)
        timeouts.append(_unreapable_after_kill(monkeypatch, process))
        return process

    monkeypatch.setattr(tool_package_runner.subprocess, "Popen", supervisor_popen)
    monkeypatch.setattr(repo_supervisor, "start_identity", lambda pid: "recorded-start")

    def not_started(stage, name, limit):
        raise FileNotFoundError(name)

    monkeypatch.setattr(tool_package_runner, "read_private", not_started)
    request = {"deadline_at": time.time() - 1, "job_id": "job", "fence": 1, "token": "t"}

    async def before_dispatch(identity):
        raise AssertionError("package work must never be released")

    result = await tool_package_runner.execute(tmp_path, request, before_dispatch=before_dispatch)
    assert result["status"] == "unknown" and result["reason"] == "tool_package_cleanup_unproven"
    assert result["supervisor_reap"]["status"] == "kill_delivered_unreaped"
    assert result["supervisor_reap"]["kill_delivered"] is True and result["supervisor_reap"]["reaped"] is False
    assert timeouts == [[process_reap.KILL_REAP_LIVENESS_SECONDS]]
    monkeypatch.undo()  # restore the real wait() so the spawned owner reaps the killed child


@pytest.mark.asyncio
async def test_tool_package_execute_failure_reaps_supervisor_in_owner(spawned, recorded_exact_signal, monkeypatch, tmp_path):
    """execute()'s failure path: SIGTERM, SIGKILL at deadline_at + grace, reap, typed outcome."""

    calls, _ = recorded_exact_signal
    started: list[subprocess.Popen] = []

    monkeypatch.setattr(tool_package_runner.subprocess, "Popen", _supervisor_popen(spawned, started))
    monkeypatch.setattr(repo_supervisor, "start_identity", lambda pid: "recorded-start")
    # A changed process identity raises inside execute() while the supervisor runs.
    monkeypatch.setattr(tool_package_runner, "read_private",
                        lambda stage, name, limit: b'{"job_id": "someone-else"}')
    request = {"deadline_at": time.time(), "job_id": "job", "fence": 1, "token": "t"}

    async def before_dispatch(identity):
        raise AssertionError("package work must never be released")

    with pytest.raises(tool_package_runner.ToolPackageBlocked) as error:
        await tool_package_runner.execute(tmp_path, request, before_dispatch=before_dispatch)
    (process,) = started
    record = error.value.supervisor_reap
    assert record["status"] == "killed_and_reaped" and record["reaped"] is True
    assert record["pid"] == process.pid and record["returncode"] == -signal.SIGKILL
    assert [signum for _, _, signum in calls] == [signal.SIGTERM, signal.SIGKILL]
    _assert_reaped(process.pid)
    assert process.stdout.closed and process.stderr.closed


def _supervisor_popen(spawned, started, mode="ignore_term"):
    def supervisor_popen(args, **kwargs):
        ready = spawned.ready_pipe()
        process = spawned.popen([sys.executable, "-I", "-c", CHILD, mode, str(ready.child_fd)], ready=ready,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert ready.wait() == b"R"
        started.append(process)
        return process
    return supervisor_popen


@pytest.mark.asyncio
async def test_tool_package_execute_deadline_kill_reaps_and_stays_unknown(spawned, recorded_exact_signal, monkeypatch,
                                                                          tmp_path):
    """execute()'s deadline path: SIGTERM, SIGKILL past deadline_at + grace, reap; cleanup stays unproven."""

    calls, _ = recorded_exact_signal
    started: list[subprocess.Popen] = []
    monkeypatch.setattr(tool_package_runner.subprocess, "Popen", _supervisor_popen(spawned, started))
    monkeypatch.setattr(repo_supervisor, "start_identity", lambda pid: "recorded-start")

    def not_started(stage, name, limit):
        raise FileNotFoundError(name)

    monkeypatch.setattr(tool_package_runner, "read_private", not_started)
    # The kill moment (deadline_at + grace) has already passed.
    request = {"deadline_at": time.time() - 1, "job_id": "job", "fence": 1, "token": "t"}

    async def before_dispatch(identity):
        raise AssertionError("package work must never be released")

    result = await tool_package_runner.execute(tmp_path, request, before_dispatch=before_dispatch)
    (process,) = started
    assert result["status"] == "unknown" and result["reason"] == "tool_package_cleanup_unproven"
    assert result["cleanup_proven"] is False
    record = result["supervisor_reap"]
    assert record["status"] == "killed_and_reaped" and record["pid"] == process.pid
    assert record["returncode"] == -signal.SIGKILL and record["reaped"] is True
    assert [signum for _, _, signum in calls] == [signal.SIGTERM, signal.SIGKILL]
    _assert_reaped(process.pid)


@pytest.mark.asyncio
async def test_tool_package_execute_failure_drains_output_so_cleanup_exits_cleanly(spawned, recorded_exact_signal,
                                                                                   monkeypatch, tmp_path):
    """A supervisor writing more than a pipe buffer during SIGTERM cleanup is not killed for it."""

    calls, _ = recorded_exact_signal
    started: list[subprocess.Popen] = []
    monkeypatch.setattr(tool_package_runner.subprocess, "Popen", _supervisor_popen(spawned, started, "flood_on_term"))
    monkeypatch.setattr(repo_supervisor, "start_identity", lambda pid: "recorded-start")
    monkeypatch.setattr(tool_package_runner, "read_private",
                        lambda stage, name, limit: b'{"job_id": "someone-else"}')
    # The kill moment is far enough away that only an undrained pipe could reach it.
    request = {"deadline_at": time.time() + 60, "job_id": "job", "fence": 1, "token": "t"}

    async def before_dispatch(identity):
        raise AssertionError("package work must never be released")

    with pytest.raises(tool_package_runner.ToolPackageBlocked) as error:
        await tool_package_runner.execute(tmp_path, request, before_dispatch=before_dispatch)
    (process,) = started
    record = error.value.supervisor_reap
    assert record["status"] == "exited_after_terminate" and record["returncode"] == 0, record
    assert [signum for _, _, signum in calls] == [signal.SIGTERM]
    _assert_reaped(process.pid)


@pytest.mark.asyncio
async def test_tool_package_failure_keeps_typed_error_when_cancelled_during_reap(spawned, recorded_exact_signal,
                                                                                 monkeypatch, tmp_path):
    """A cancel during the failure-path reap is recorded on the typed failure, never swapped for it."""

    import asyncio
    import threading

    started: list[subprocess.Popen] = []
    monkeypatch.setattr(tool_package_runner.subprocess, "Popen", _supervisor_popen(spawned, started))
    monkeypatch.setattr(repo_supervisor, "start_identity", lambda pid: "recorded-start")
    monkeypatch.setattr(tool_package_runner, "read_private",
                        lambda stage, name, limit: b'{"job_id": "someone-else"}')
    entered, release = threading.Event(), threading.Event()
    real_reap = tool_package_runner.terminate_and_reap_supervisor

    def gated_reap(*args, **kwargs):
        entered.set()
        assert release.wait(30), "test never released the reap"
        return real_reap(*args, **kwargs)

    monkeypatch.setattr(tool_package_runner, "terminate_and_reap_supervisor", gated_reap)
    request = {"deadline_at": time.time(), "job_id": "job", "fence": 1, "token": "t"}

    async def before_dispatch(identity):
        raise AssertionError("package work must never be released")

    task = asyncio.ensure_future(tool_package_runner.execute(tmp_path, request, before_dispatch=before_dispatch))
    await asyncio.to_thread(entered.wait, 30)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(tool_package_runner.ToolPackageBlocked) as error:
        await task
    (process,) = started
    assert error.value.cancel_requested is True
    assert error.value.supervisor_reap["status"] == "killed_and_reaped"
    _assert_reaped(process.pid)


@pytest.mark.asyncio
async def test_reap_in_owner_outlives_repeated_cancellation():
    """A cancel during the reap is remembered; the owner returns only after the reap finished."""

    import asyncio
    import threading

    entered, release = threading.Event(), threading.Event()
    outcome = ReapOutcome(1, "killed_and_reaped", -signal.SIGKILL, True, True)

    def blocking_reap():
        entered.set()
        assert release.wait(30), "test never released the reap"
        return outcome

    task = asyncio.ensure_future(tool_package_runner._reap_in_owner(blocking_reap))
    await asyncio.to_thread(entered.wait, 30)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    assert await task == (outcome, True)


# -- the spawned fixture itself -----------------------------------------


def test_spawned_owner_kills_and_reaps_at_teardown():
    owner = SpawnedProcesses()
    ready = owner.ready_pipe()
    process = owner.popen([sys.executable, "-I", "-c", CHILD, "ignore_term", str(ready.child_fd)], ready=ready,
                          stdout=subprocess.PIPE)
    assert ready.wait() == b"R"
    outcomes = owner.reap_all()
    assert [outcome.status for outcome in outcomes] == ["killed_and_reaped"]
    assert process.stdout.closed and owner.processes == []
    _assert_reaped(process.pid)


def test_ready_pipe_reports_child_exit_without_signal(spawned):
    ready = spawned.ready_pipe()
    spawned.popen([sys.executable, "-I", "-c", "pass", str(ready.child_fd)], ready=ready)
    with pytest.raises(AssertionError, match="without signalling ready"):
        ready.wait()
