"""Actual isolated fixed-process checks; no canonical task success is seeded."""
from datetime import datetime, timedelta, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from config.settings import RepoSandboxSettings
from src.execution.repo_sandbox import LocalRepoRepairExecutor, RepoSandboxError, RepoSandboxJob
from src.execution.repo_supervisor import PYTHON_PROFILE, finish_supervisor, platform_ready
from src.workflows.repo_repair_source import RepoIterationProcessBinding


def native_platform():
    try:
        platform_ready()
    except (OSError, ValueError) as exc:
        pytest.skip("Linux native supervisor unavailable: " + str(exc))


def child(code):
    return subprocess.Popen([sys.executable, "-I", "-c", code], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={"PATH": "/usr/bin:/bin"}, start_new_session=True)


def close_child(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=3)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream and not stream.closed:
            stream.close()


def test_actual_supervisor_transport_drains_before_wait():
    native_platform()
    process = child("import sys;assert sys.stdin.read()=='token';sys.stdout.write('x'*200000);sys.stderr.write('y'*150000)")
    try:
        process.stdin.write(b"token")
        process.stdin.close()
        receipt = finish_supervisor(process, deadline=time.monotonic() + 5, stream_limit=250000)
        assert receipt["returncode"] == 0
        assert receipt["waited"] and receipt["stdout_eof"] and receipt["stderr_eof"]
        assert receipt["stdout_closed"] and receipt["stderr_closed"] and receipt["stdin_closed"]
        assert receipt["stdout_sha256"] == hashlib.sha256(b"x" * 200000).hexdigest()
        assert receipt["stderr_sha256"] == hashlib.sha256(b"y" * 150000).hexdigest()
    finally:
        close_child(process)


def test_missing_stdin_close_cannot_witness_process_closure():
    process = child("import sys;sys.stdin.read()")
    try:
        with pytest.raises(ValueError, match="stdin_closure_unproven"):
            finish_supervisor(process, deadline=time.monotonic() + 1, stream_limit=1024)
        assert process.poll() is None
    finally:
        close_child(process)


def test_live_original_output_deadline_is_unknown_and_retains_descriptors():
    process = child("import time;time.sleep(5)")
    try:
        process.stdin.close()
        with pytest.raises(ValueError, match="output_eof_unproven"):
            finish_supervisor(process, deadline=time.monotonic() + .03, stream_limit=1024)
        assert not process.stdout.closed and not process.stderr.closed
        assert process.poll() is None
    finally:
        close_child(process)


def test_output_overflow_never_returns_truncated_closure_receipt():
    process = child("print('x'*10000)")
    try:
        process.stdin.close()
        with pytest.raises(ValueError, match="output_limit"):
            finish_supervisor(process, deadline=time.monotonic() + 3, stream_limit=32)
        assert process.returncode == 0
        assert process.stdout.closed and process.stderr.closed
    finally:
        close_child(process)


def python_fixture(tmp_path, *, expected=2, test_source=None):
    native_platform()
    workspace = tmp_path / "owned"
    workspace.mkdir(mode=0o700)
    repo = workspace / "repo"
    repo.mkdir(mode=0o700)
    old, new = "VALUE = 1\n", "VALUE = 2\n"
    (repo / "value.py").write_text(old)
    (repo / "test_value.py").write_text(test_source or f"from value import VALUE\ndef test_value():\n    assert VALUE == {expected}\n")
    executor = LocalRepoRepairExecutor(RepoSandboxSettings(enabled=True), workspace_dir=workspace)
    preflight = executor.iterative_preflight()
    assert preflight.ok, preflight.reason
    stage = workspace / "stage"
    stage.mkdir(mode=0o700)
    snapshot = executor.snapshot_repository(repo, stage / "preview")
    patch = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), fromfile="a/value.py", tofile="b/value.py")).encode()
    executor.write_input_bundle(snapshot=snapshot, patch=patch, staging_root=stage / "input", job={
        "job_id": "original-repair", "authority_digest": "a" * 64, "base_digest": snapshot.digest,
        "patch_sha256": hashlib.sha256(patch).hexdigest(), "allowed_paths": ["value.py", "test_value.py"],
        "test_args": ["test_value.py", "-q"], "wall_seconds": 20, "cpu_seconds": 10,
        "worker_image_digest": "", "export_grace_seconds": 0})
    output = stage / "out"
    output.mkdir(mode=0o700)
    env = executor._minimal_env(stage)
    token = "b" * 64
    supervisor = Path(__file__).resolve().parents[1] / "src" / "execution" / "repo_supervisor.py"
    projection = {"repository_job_id": "original-repair", "repository_attempt_id": "original-attempt", "repository_fence": 1,
        "iteration_index": 1, "iteration_id": "c" * 64, "authority_digest": "a" * 64, "base_digest": snapshot.digest,
        "original_deadline_at": (datetime.now(timezone.utc) + timedelta(seconds=20)).isoformat()}
    payload = {"profile": PYTHON_PROFILE, "stage": str(stage), "runtime": preflight.info["runtime_identity"],
        "job_id": "original-repair", "iteration_binding": projection, "deadline_at": time.monotonic() + 20,
        "token": token, "supervisor_source_sha256": hashlib.sha256(supervisor.read_bytes()).hexdigest(),
        "git_sha256": preflight.posture["git_sha256"], "environment": env}
    request = stage / "supervisor.json"
    request.write_text(json.dumps(payload))
    request.chmod(0o600)
    return executor, repo, stage, token, supervisor, request, env


@pytest.mark.parametrize("expected,status", [(2, "succeeded"), (3, "failed")])
def test_fixed_python_supervisor_actual_command_and_private_readback(tmp_path, expected, status):
    executor, repo, stage, token, supervisor, request, env = python_fixture(tmp_path, expected=expected)
    process = subprocess.Popen([sys.executable, "-I", str(supervisor), str(request)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True)
    try:
        process.stdin.write((token + "\n").encode())
        process.stdin.close()
        transport = finish_supervisor(process, deadline=time.monotonic() + 20, stream_limit=100000)
        result = json.loads(executor._read_private_output(stage / "out", "supervisor-result.json"))
        manifest = json.loads(executor._read_private_output(stage / "out", "manifest.json"))
        assert transport["returncode"] == 0
        assert result["status"] == status, result
        assert result["supervisor_pid"] == process.pid
        assert result["process_cleanup"]["oracle"] == "linux_subreaper_waitpid_echild"
        assert all(command["cleanup"]["cleanup_proven"] and command["stdout_eof"] and command["stderr_eof"] and command["waited"] for command in result["commands"])
        assert manifest["status"] == status
        assert executor._read_private_output(stage / "out", "manifest.json") == executor._read_private_output(stage / "out", "readback.json")
        assert executor._read_private_output(stage / "out", "diff.patch")
        assert (repo / "value.py").read_text() == "VALUE = 1\n"
    finally:
        close_child(process)


def test_unsealed_iteration_cannot_spawn_even_with_correct_scalar_binding(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    executor = LocalRepoRepairExecutor(RepoSandboxSettings(enabled=True), workspace_dir=workspace)
    cutoff = datetime.now(timezone.utc) + timedelta(seconds=30)
    binding = RepoIterationProcessBinding("root", "attempt", 1, 1, "a" * 64, cutoff, "b" * 64, "c" * 64)
    job = RepoSandboxJob("root", str(workspace), b"", ("test_value.py",), ("test_value.py",), "b" * 64, "c" * 64,
        attempt_id="attempt", fencing_token=1, execution_deadline_at=cutoff.isoformat(), iteration_binding=binding)
    with pytest.raises(RepoSandboxError, match="source-issued"):
        executor.execute_job(job)
    assert executor._read_job_marker("root") is None


def test_fixed_python_predispatch_cancel_has_actual_empty_ancestry(tmp_path):
    executor, _repo, stage, token, supervisor, request, env = python_fixture(tmp_path)
    process = subprocess.Popen([sys.executable, "-I", str(supervisor), str(request)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True)
    try:
        process.stdin.write(("cancel:" + token + "\n").encode())
        process.stdin.close()
        receipt = finish_supervisor(process, deadline=time.monotonic() + 10, stream_limit=100000)
        result = json.loads(executor._read_private_output(stage / "out", "supervisor-result.json"))
        assert receipt["returncode"] == 0
        assert result["status"] == "cancelled" and result["commands"] == []
        assert result["process_cleanup"]["oracle"] == "linux_subreaper_waitpid_echild"
        assert result["process_cleanup"]["cleanup_proven"] is True
    finally:
        close_child(process)


def test_fixed_python_active_cancel_reaps_actual_original_command(tmp_path):
    ready = tmp_path / "ready"
    body = f"import time\nfrom pathlib import Path\ndef test_wait():\n    Path({str(ready)!r}).write_text('started')\n    time.sleep(10)\n"
    executor, _repo, stage, token, supervisor, request, env = python_fixture(tmp_path, test_source=body)
    process = subprocess.Popen([sys.executable, "-I", str(supervisor), str(request)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True)
    try:
        process.stdin.write((token + "\n").encode())
        process.stdin.close()
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), "Actual command did not reach its original fixture boundary"
        process.send_signal(signal.SIGTERM)
        receipt = finish_supervisor(process, deadline=deadline, stream_limit=100000)
        result = json.loads(executor._read_private_output(stage / "out", "supervisor-result.json"))
        assert receipt["returncode"] == 0
        assert result["status"] == "cancelled"
        assert result["process_cleanup"]["oracle"] == "linux_subreaper_waitpid_echild"
        assert result["process_cleanup"]["cleanup_proven"] is True
        assert result["commands"][-1]["cancelled"] is True
        assert result["commands"][-1]["exit_code"] == -signal.SIGKILL
        assert result["commands"][-1]["cleanup"]["wait_statuses"]
    finally:
        close_child(process)
