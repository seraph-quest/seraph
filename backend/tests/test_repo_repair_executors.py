from __future__ import annotations

import difflib
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import subprocess
import threading
import time
from dataclasses import replace

import pytest

from config.settings import RepoSandboxSettings
import src.execution.repo_sandbox as repo_sandbox
import src.execution.repo_worker as repo_worker
from src.execution.repo_sandbox import (
    LocalRepoRepairExecutor,
    RepoSandboxError,
    RepoSandboxJob,
    RootfulDockerRepoSandbox,
    RootlessDockerRepoSandbox,
    build_repo_repair_executor,
    executor_posture_digest,
    load_persisted_repo_sandbox_settings,
    persist_repo_sandbox_settings,
)


IMAGE = "ghcr.io/operator/seraph-repo-python-pytest@sha256:" + "a" * 64


def _local_settings(**overrides: object) -> RepoSandboxSettings:
    values: dict[str, object] = {
        "executor_kind": "local",
        "enabled": True,
        "profile": "repo-python-pytest-v1",
    }
    values.update(overrides)
    return RepoSandboxSettings(**values)


def _repo(tmp_path: Path, *, slow: bool = False) -> tuple[Path, bytes, tuple[str, ...]]:
    repo = tmp_path / "workspace" / "source"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    test_body = (
        "import time\n"
        "from src.app import VALUE\n"
        "\n"
        "def test_value():\n"
        "    time.sleep(30)\n"
        "    assert VALUE == 2\n"
        if slow
        else "from src.app import VALUE\n\n"
        "def test_value():\n"
        "    assert VALUE == 2\n"
    )
    (repo / "tests" / "test_value.py").write_text(test_body, encoding="utf-8")
    old = "VALUE = 1\n"
    new = "VALUE = 2\n"
    patch = "".join(
        difflib.unified_diff(
            old.splitlines(True),
            new.splitlines(True),
            fromfile="a/src/app.py",
            tofile="b/src/app.py",
        )
    ).encode("utf-8")
    return repo, patch, ("src/app.py", "tests/test_value.py")


def _job(runner: LocalRepoRepairExecutor, repo: Path, patch: bytes, allowed: tuple[str, ...], *, job_id: str = "job-1", deadline: int = 30) -> RepoSandboxJob:
    before = runner.snapshot_repository(repo, runner.workspace_dir / "preview").digest
    return RepoSandboxJob(
        job_id=job_id,
        repository_root=str(repo),
        patch_bytes=patch,
        allowed_paths=allowed,
        test_args=("tests/test_value.py",),
        authority_digest="authority-1",
        base_digest=before,
        deadline_seconds=deadline,
    )


def test_factory_selects_explicit_executor_without_fallback(tmp_path: Path):
    local = build_repo_repair_executor(_local_settings())
    assert isinstance(local, LocalRepoRepairExecutor)
    rootless = build_repo_repair_executor(
        RepoSandboxSettings(executor_kind="docker_rootless", enabled=False)
    )
    rootful = build_repo_repair_executor(
        RepoSandboxSettings(executor_kind="docker_rootful", enabled=False)
    )
    assert isinstance(rootless, RootlessDockerRepoSandbox)
    assert isinstance(rootful, RootfulDockerRepoSandbox)
    assert rootless.preflight().reason == "repo_sandbox_disabled"
    assert rootful.preflight().reason == "repo_sandbox_disabled"


def test_local_preflight_requires_enabled_private_workspace_and_fixed_runtime(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    runner = LocalRepoRepairExecutor(_local_settings(), workspace_dir=workspace)
    receipt = runner.preflight().as_receipt()
    assert receipt["ok"] is True
    assert receipt["executor_kind"] == "local"
    assert receipt["posture"]["isolation_claim"] == "none"
    assert receipt["posture"]["resource_enforcement"] == "admission_and_wall_timeout_only"
    assert receipt["posture_digest"] == executor_posture_digest(receipt["posture"])

    os.chmod(workspace, 0o770)
    assert runner.preflight().reason == "local_workspace_untrusted"
    assert LocalRepoRepairExecutor(_local_settings(enabled=False), workspace_dir=workspace).preflight().reason == "repo_sandbox_disabled"


def test_local_executor_runs_real_staged_patch_and_preserves_source(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    runner = LocalRepoRepairExecutor(_local_settings(), workspace_dir=workspace)
    repo, patch, allowed = _repo(tmp_path)
    before_bytes = (repo / "src" / "app.py").read_bytes()
    job = _job(runner, repo, patch, allowed)
    callback_phases: list[str] = []

    def before_dispatch() -> None:
        marker = runner._read_job_marker(job.job_id)
        callback_phases.append(str((marker or {}).get("phase")))

    result = runner.execute_job(job, before_dispatch=before_dispatch)
    assert result["status"] == "succeeded"
    assert callback_phases == ["dispatch_fence_pending"]
    assert (repo / "src" / "app.py").read_bytes() == before_bytes
    assert result["manifest"]["source_original_unchanged"] is True
    assert result["manifest"]["executor_kind"] == "local"
    identity = result["manifest"]["execution_identity"]
    assert identity["schema"] == "seraph.repo_repair_execution_identity.v1"
    assert identity["backend_kind"] == "local"
    assert identity["job_id"] == job.job_id
    assert identity["authority_digest"] == job.authority_digest
    assert identity["interpreter_entry_path"] == str(Path(repo_worker.sys.executable).absolute())
    assert len(identity["interpreter_sha256"]) == 64
    assert len(identity["pytest_package_sha256"]) == 64
    assert result["outputs"]["manifest.json"] == (json.dumps(result["manifest"], sort_keys=True).encode("utf-8") + b"\n")
    assert result["outputs"]["readback.json"] == (json.dumps(result["readback"], sort_keys=True).encode("utf-8") + b"\n")
    assert result["manifest"]["isolation_claim"] == "none"
    assert result["cleanup"] == {"status": "cleanup_verified", "cleanup_proven": True}
    marker = runner._read_job_marker(job.job_id)
    assert marker is not None
    assert marker["status"] == "succeeded"
    assert marker["cleanup_proven"] is True
    assert stat.S_IMODE((workspace / "artifacts" / "repo-sandbox" / "jobs" / runner._job_marker_name(job.job_id)).stat().st_mode) == 0o600


def test_local_executor_does_not_inherit_parent_secret_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    runner = LocalRepoRepairExecutor(_local_settings(), workspace_dir=workspace)
    repo, patch, allowed = _repo(tmp_path)
    (repo / "tests" / "test_environment.py").write_text(
        "import os\n\n"
        "def test_parent_secret_is_not_inherited():\n"
        "    assert 'SERAPH_EXECUTOR_SECRET' not in os.environ\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SERAPH_EXECUTOR_SECRET", "must-not-cross-executor-boundary")
    job = _job(runner, repo, patch, allowed + ("tests/test_environment.py",), job_id="env-job")
    job = replace(job, test_args=("tests/test_value.py", "tests/test_environment.py"))
    result = runner.execute_job(job)
    assert result["status"] == "succeeded"


def test_local_worker_does_not_import_a_staged_pytest_shadow(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    runner = LocalRepoRepairExecutor(_local_settings(), workspace_dir=workspace)
    repo, patch, allowed = _repo(tmp_path)
    (repo / "pytest.py").write_text("raise RuntimeError('staged pytest shadow imported')\n", encoding="utf-8")
    (repo / "tests" / "test_pytest_runtime.py").write_text(
        "import pytest\n"
        "from pathlib import Path\n\n"
        "def test_trusted_pytest_is_loaded():\n"
        "    assert Path(pytest.__file__).resolve().parent != Path.cwd()\n",
        encoding="utf-8",
    )
    job = _job(
        runner,
        repo,
        patch,
        allowed + ("pytest.py", "tests/test_pytest_runtime.py"),
        job_id="pytest-shadow-job",
    )
    job = replace(job, test_args=("tests/test_pytest_runtime.py",))
    result = runner.execute_job(job)
    assert result["status"] == "succeeded"


def test_local_executor_rejects_and_reaps_nested_processes(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    runner = LocalRepoRepairExecutor(_local_settings(), workspace_dir=workspace)
    repo, patch, allowed = _repo(tmp_path)
    (repo / "tests" / "test_value.py").write_text(
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n\n"
        "def test_value():\n"
        "    child = subprocess.Popen([sys.executable, '-c', "
        "\"from pathlib import Path; import os, time; Path('child.pid').write_text(str(os.getpid())); time.sleep(30)\"])\n"
        "    time.sleep(0.2)\n"
        "    assert child.poll() is None\n",
        encoding="utf-8",
    )
    job = _job(
        runner,
        repo,
        patch,
        allowed + ("child.pid",),
        job_id="nested-child-job",
        deadline=10,
    )
    with pytest.raises(RepoSandboxError) as error:
        runner.execute_job(job)
    assert error.value.terminal_status == "unknown_external_effect"
    stage = workspace / "artifacts" / "repo-sandbox" / "staging" / runner._job_stage_token(job) / "workspace"
    pid_file = stage / "child.pid"
    assert pid_file.is_file()
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("nested child survived the executor cleanup boundary")


def test_local_executor_rejects_child_created_output_symlink(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    runner = LocalRepoRepairExecutor(_local_settings(), workspace_dir=workspace)
    repo, patch, allowed = _repo(tmp_path)
    external = tmp_path / "outside-output.json"
    external.write_text("keep-this-file", encoding="utf-8")
    (repo / "tests" / "test_value.py").write_text(
        "from pathlib import Path\n\n"
        "def test_value():\n"
        f"    Path('../out/manifest.json').symlink_to({str(external)!r})\n"
        "    assert Path('../out/manifest.json').is_symlink()\n",
        encoding="utf-8",
    )
    job = _job(runner, repo, patch, allowed, job_id="output-symlink-job", deadline=10)
    with pytest.raises(RepoSandboxError) as error:
        runner.execute_job(job)
    assert error.value.terminal_status == "unknown_external_effect"
    assert external.read_text(encoding="utf-8") == "keep-this-file"


def test_local_cancel_requires_authority_and_reconciles_unknown(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    runner = LocalRepoRepairExecutor(_local_settings(), workspace_dir=workspace)
    repo, patch, allowed = _repo(tmp_path, slow=True)
    job = _job(runner, repo, patch, allowed, job_id="cancel-job")
    result_holder: dict[str, object] = {}

    def run() -> None:
        try:
            result_holder["result"] = runner.execute_job(job)
        except BaseException as exc:  # pragma: no cover - assertion below reports unexpected worker errors
            result_holder["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        marker = runner._read_job_marker(job.job_id)
        with runner._active_lock:
            active = runner._active.get(job.job_id)
            process = active.get("process") if active else None
        if marker and marker.get("phase") == "worker_started" and process is not None and process.poll() is None and "pytest" in " ".join(map(str, process.args)):
            break
        time.sleep(0.02)
    assert runner.cancel(job_id=job.job_id, authority={"authority_digest": "wrong"})["status"] == "unknown_external_effect"
    cancel = runner.cancel(job_id=job.job_id, authority={"authority_digest": job.authority_digest})
    assert cancel["status"] == "cancel_requested"
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert "error" not in result_holder
    assert result_holder["result"]["status"] == "cancelled"  # type: ignore[index]
    reconciliation = runner.reconcile({"job_id": job.job_id, "authority_digest": job.authority_digest})
    assert reconciliation["status"] == "cancelled"
    assert reconciliation["cleanup_proven"] is True
    assert reconciliation["receipt"]["attempt_id"] == "legacy-attempt"  # type: ignore[index]
    assert reconciliation["marker"]["status"] == "cancelled"  # type: ignore[index]


def test_local_fresh_executor_cancel_fences_original_worker_and_cleans_stage(tmp_path: Path):
    """A restart-like adapter must fence the original worker before cleanup."""

    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    runner = LocalRepoRepairExecutor(_local_settings(), workspace_dir=workspace)
    repo, patch, allowed = _repo(tmp_path, slow=True)
    job = _job(runner, repo, patch, allowed, job_id="fresh-cancel-job")
    result_holder: dict[str, object] = {}

    def run() -> None:
        try:
            result_holder["result"] = runner.execute_job(job)
        except BaseException as exc:  # pragma: no cover - assertion below reports unexpected worker errors
            result_holder["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        marker = runner._read_job_marker(job.job_id)
        with runner._active_lock:
            active = runner._active.get(job.job_id)
            process = active.get("process") if active else None
        if marker and marker.get("phase") == "worker_started" and process is not None and process.poll() is None:
            break
        time.sleep(0.02)
    assert marker and marker.get("phase") == "worker_started", marker

    fresh = LocalRepoRepairExecutor(_local_settings(), workspace_dir=workspace)
    cancel = fresh.cancel(
        job_id=job.job_id,
        authority={"job_id": job.job_id, "authority_digest": job.authority_digest},
    )
    assert cancel["status"] == "cancel_requested"
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert "error" not in result_holder
    assert result_holder["result"]["status"] == "cancelled"  # type: ignore[index]
    assert result_holder["result"]["cleanup"] == {  # type: ignore[index]
        "status": "cleanup_verified",
        "cleanup_proven": True,
    }
    stage = workspace / "artifacts" / "repo-sandbox" / "staging"
    assert not any(stage.iterdir())
    marker = runner._read_job_marker(job.job_id)
    assert marker is not None
    assert marker["status"] == "cancelled"
    assert marker["cleanup_proven"] is True


def test_local_fresh_executor_fences_exact_unreaped_worker_pid(tmp_path: Path):
    """An exact unreaped worker PID can be fenced after its group exits."""

    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    runner = LocalRepoRepairExecutor(_local_settings(), workspace_dir=workspace)
    process = subprocess.Popen(["/bin/sh", "-c", "exit 0"], start_new_session=True)
    try:
        identity = runner._pid_start_identity(process.pid)
        assert identity
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and runner._pid_state(process.pid) != "Z":
            time.sleep(0.01)
        assert runner._pid_state(process.pid) == "Z"
        binding = {
            "executor_kind": "local",
            "job_id": "zombie-cancel-job",
            "attempt_id": "attempt-zombie",
            "fencing_token": 7,
            "authority_digest": "authority-zombie",
        }
        runner._write_job_marker(
            "zombie-cancel-job",
            {
                "schema": "seraph.repo_repair_local_job.v1",
                **binding,
                "phase": "worker_started",
                "status": "running",
                "pid": process.pid,
                "pid_start_identity": identity,
                "stage_binding": binding,
            },
        )
        fresh = LocalRepoRepairExecutor(_local_settings(), workspace_dir=workspace)
        result = fresh.cancel(
            job_id="zombie-cancel-job",
            authority={
                "job_id": "zombie-cancel-job",
                "authority_digest": "authority-zombie",
                "attempt_id": "attempt-zombie",
                "fencing_token": 7,
            },
        )
        assert result == {
            "status": "cancel_requested",
            "cleanup_proven": False,
            "job_id": "zombie-cancel-job",
        }
        marker = runner._read_job_marker("zombie-cancel-job")
        assert marker is not None
        assert marker["phase"] == "cancel_requested"
        assert marker["status"] == "cancellation_requested"
    finally:
        process.wait(timeout=2.0)


def test_local_cancel_between_worker_processes_sets_durable_fence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A cancel arriving before the first child spawn must prevent that spawn."""

    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    runner = LocalRepoRepairExecutor(_local_settings(), workspace_dir=workspace)
    repo, patch, allowed = _repo(tmp_path)
    job = _job(runner, repo, patch, allowed, job_id="between-process-cancel")
    original = repo_worker.run_local_job
    observed: list[str] = []

    def cancel_before_spawn(*args: object, **kwargs: object) -> int:
        observed.append(runner.cancel(job_id=job.job_id, authority={"authority_digest": job.authority_digest})["status"])
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(repo_worker, "run_local_job", cancel_before_spawn)
    result = runner.execute_job(job)
    assert observed == ["cancel_requested"]
    assert result["status"] == "cancelled"
    assert result["cleanup"] == {"status": "cleanup_verified", "cleanup_proven": True}


def test_local_fresh_cancel_requires_attempt_and_fence_for_nonlegacy_marker(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    runner = LocalRepoRepairExecutor(_local_settings(), workspace_dir=workspace)
    runner._write_job_marker(
        "bound-cancel",
        {
            "schema": "seraph.repo_repair_local_job.v1",
            "job_id": "bound-cancel",
            "authority_digest": "authority-1",
            "attempt_id": "attempt-1",
            "fencing_token": 4,
            "phase": "worker_started",
            "status": "running",
            "pid": 0,
            "pid_start_identity": "",
        },
    )

    missing = runner.cancel(job_id="bound-cancel", authority={"authority_digest": "authority-1"})
    assert missing["status"] == "unknown_external_effect"
    assert missing["reason"] == "local_attempt_missing"
    wrong = runner.cancel(
        job_id="bound-cancel",
        authority={"authority_digest": "authority-1", "attempt_id": "attempt-1", "fencing_token": 3},
    )
    assert wrong["status"] == "unknown_external_effect"
    assert wrong["reason"] == "local_fencing_token_mismatch"


def test_local_reconcile_does_not_promote_digest_only_success_after_stage_cleanup(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    runner = LocalRepoRepairExecutor(_local_settings(), workspace_dir=workspace)
    binding = {
        "executor_kind": "local",
        "job_id": "terminal-success",
        "attempt_id": "attempt-terminal",
        "fencing_token": 8,
        "authority_digest": "authority-1",
    }
    runner._write_job_marker(
        "terminal-success",
        {
            "schema": "seraph.repo_repair_local_job.v1",
            "job_id": "terminal-success",
            "authority_digest": "authority-1",
            "attempt_id": "attempt-terminal",
            "fencing_token": 8,
            "phase": "cleanup_verified",
            "status": "succeeded",
            "cleanup_proven": True,
            "stage_directory": "artifacts/repo-sandbox/staging/terminal-success/workspace",
            "stage_binding": binding,
            "terminal_receipt": {
                "status": "succeeded",
                "manifest_sha256": "a" * 64,
                "readback_sha256": "b" * 64,
                "attempt_id": "attempt-terminal",
                "fencing_token": 8,
                "stage_binding": binding,
            },
        },
    )

    reconciled = runner.reconcile(
        {
            "job_id": "terminal-success",
            "authority_digest": "authority-1",
            "attempt_id": "attempt-terminal",
            "fencing_token": 8,
        }
    )
    assert reconciled["status"] == "unknown_external_effect"
    assert reconciled["reason"] == "local_terminal_outputs_not_recoverable"
    assert reconciled["cleanup_proven"] is True


def test_local_late_cancel_preserves_verified_terminal_without_signalling(tmp_path: Path, monkeypatch):
    """Force terminal cleanup to win between the active flag and cancel write."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    config = _local_settings()
    runner = LocalRepoRepairExecutor(config, workspace_dir=workspace)
    repo, patch, allowed = _repo(tmp_path)
    job = replace(_job(runner, repo, patch, allowed), attempt_id="late-cancel-attempt", fencing_token=7)
    ready, cancel_paused, release_cancel = threading.Event(), threading.Event(), threading.Event()
    outcomes: dict[str, object] = {}
    signals: list[tuple] = []
    write = runner._write_job_marker

    class ObservedProcess:
        pid = 12345

        def poll(self):
            return None

    monkeypatch.setattr(runner, "_pid_start_identity", lambda _pid: "exact-observed-start")
    monkeypatch.setattr(os, "killpg", lambda *args: signals.append(args))
    monkeypatch.setattr(os, "kill", lambda *args: signals.append(args))

    def blocked_worker(*args, **kwargs):
        # Control worker timing only. The real executor still owns staging,
        # validates cancellation, removes the stage and writes its terminal.
        kwargs["process_observer"](ObservedProcess())
        ready.set()
        assert cancel_paused.wait(10), "cancel did not reach the late-write barrier"
        output = kwargs["output_root"]
        for name, data in {"manifest.json": b'{"status":"blocked"}', "readback.json": b'{}',
                           "pytest.stdout": b"", "pytest.stderr": b""}.items():
            repo_worker._write_private_output(output, name, data)
        kwargs["process_observer"](None)
        return 2

    def pause_cancel(job_id, payload):
        if payload.get("status") == "cancellation_requested":
            cancel_paused.set()
            assert release_cancel.wait(10), "terminal cleanup did not release cancellation"
        return write(job_id, payload)

    def execute():
        try:
            outcomes["execution"] = runner.execute_job(job)
        except BaseException as exc:
            outcomes["execution_error"] = exc

    def cancel():
        try:
            outcomes["cancel"] = runner.cancel(job_id=job.job_id, authority={
                "authority_digest": job.authority_digest, "attempt_id": job.attempt_id,
                "fencing_token": job.fencing_token,
            })
        except BaseException as exc:
            outcomes["cancel_error"] = exc

    monkeypatch.setattr(repo_worker, "run_local_job", blocked_worker)
    monkeypatch.setattr(runner, "_write_job_marker", pause_cancel)
    worker = threading.Thread(target=execute)
    cancelling = threading.Thread(target=cancel)
    worker.start()
    try:
        assert ready.wait(10)
        cancelling.start()
        assert cancel_paused.wait(10)
        worker.join(10)
        assert not worker.is_alive(), outcomes
        assert "execution_error" not in outcomes, outcomes
        terminal = runner._read_job_marker(job.job_id)
        assert terminal["status"] == "cancelled"
        assert terminal["phase"] == "cleanup_verified"
        release_cancel.set()
        cancelling.join(10)
        assert not cancelling.is_alive(), outcomes
        assert "cancel_error" not in outcomes, outcomes
        assert outcomes["cancel"]["status"] == "cancel_requested"
        assert outcomes["execution"]["status"] == "cancelled"
        assert outcomes["execution"]["cleanup"]["cleanup_proven"] is True
        assert runner._read_job_marker(job.job_id) == terminal
        fresh = LocalRepoRepairExecutor(config, workspace_dir=workspace)
        recovered = fresh.reconcile({"job_id": job.job_id, "authority_digest": job.authority_digest,
                                     "attempt_id": job.attempt_id, "fencing_token": job.fencing_token})
        assert recovered["status"] == "cancelled"
        assert recovered["cleanup_proven"] is True
        assert recovered["receipt"]["stage_binding"] == terminal["stage_binding"]
        assert not any((workspace / "artifacts/repo-sandbox/staging").iterdir())
        assert (repo / "src/app.py").read_text() == "VALUE = 1\n"
        assert signals == [], "late cancel signalled a process after terminal cleanup"
    finally:
        release_cancel.set()
        worker.join(10)
        if cancelling.ident is not None:
            cancelling.join(10)
        assert not worker.is_alive() and not cancelling.is_alive()


def test_publication_cancel_writer_preserves_same_bound_terminal(tmp_path: Path):
    """Writer-level fixture only; real publication runtime stays a separate gate."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    runner = LocalRepoRepairExecutor(_local_settings(profile="repo-python-pytest-publication-v1"), workspace_dir=workspace)
    binding = {"executor_kind": "local", "job_id": "publication-cancel", "authority_digest": "approved",
               "attempt_id": "publication-attempt", "fencing_token": 7}
    stage = runner._trusted_staging_directory() / runner._stage_binding_token(binding)
    marker = {"schema": "seraph.repo_repair_local_job.v1", "executor_kind": "local",
              "profile": runner.config.profile, **binding, "base_digest": "base", "posture_digest": "posture",
              "runtime_identity": {"worker_source_sha256": "a" * 64}, "stage_binding": binding,
              "stage_directory": str(stage.relative_to(workspace)), "stage_identity": {"device": 1, "inode": 2},
              "phase": "cleanup_verified", "status": "cancelled", "cleanup_proven": True,
              "terminal_receipt": {"status": "cancelled", "attempt_id": binding["attempt_id"], "fencing_token": 7,
                                   "stage_binding": binding, "manifest_sha256": "a" * 64, "readback_sha256": "a" * 64}}
    runner._write_job_marker(binding["job_id"], marker)
    marker_path = runner._job_marker_directory / runner._job_marker_name(binding["job_id"])
    original = marker_path.read_bytes()
    assert runner._write_job_marker(binding["job_id"], {**marker, "phase": "cancel_requested",
                                    "status": "cancellation_requested", "cleanup_proven": False}) is True
    assert marker_path.read_bytes() == original
    recovered = runner.reconcile(binding)
    assert recovered["status"] == "cancelled"
    assert recovered["cleanup_proven"] is True
    assert recovered["receipt"]["stage_binding"] == binding


@pytest.mark.parametrize("boundary", ["authority", "attempt", "fence", "stage", "unproven",
                                      "terminal_binding", "stage_present", "stage_symlink", "mode", "hardlink",
                                      "digest_mismatch", "oversize", "foreign_profile", "bool_fence",
                                      "malformed", "nonmapping", "read_failure", "missing", "fifo"])
def test_local_late_cancel_rejects_unproven_or_foreign_terminal(tmp_path: Path, monkeypatch, boundary: str):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    runner = LocalRepoRepairExecutor(_local_settings(), workspace_dir=workspace)
    binding = {"executor_kind": "local", "job_id": "cancelled", "authority_digest": "approved",
               "attempt_id": "attempt-1", "fencing_token": 7}
    stage = runner._trusted_staging_directory() / runner._stage_binding_token(binding)
    marker = {"schema": "seraph.repo_repair_local_job.v1", "executor_kind": "local",
              "profile": runner.config.profile, **binding, "base_digest": "base", "posture_digest": "posture",
              "runtime_identity": {"worker_source_sha256": "a" * 64}, "stage_binding": binding,
              "stage_directory": str(stage.relative_to(workspace)), "stage_identity": {"device": 1, "inode": 2},
              "phase": "cleanup_verified", "status": "cancelled", "cleanup_proven": True,
              "terminal_receipt": {"status": "cancelled", "attempt_id": "attempt-1", "fencing_token": 7,
                                   "stage_binding": binding, "manifest_sha256": "a" * 64, "readback_sha256": "a" * 64}}
    incoming = {**marker, "phase": "cancel_requested", "status": "cancellation_requested",
                "cleanup_proven": False}
    if boundary in {"authority", "attempt", "fence"}:
        key = {"authority": "authority_digest", "attempt": "attempt_id", "fence": "fencing_token"}[boundary]
        incoming[key] = 8 if boundary == "fence" else "foreign"
    elif boundary == "stage":
        incoming["stage_directory"] = "elsewhere"
    elif boundary == "unproven":
        marker["cleanup_proven"] = False
    elif boundary == "terminal_binding":
        marker["terminal_receipt"] = {**marker["terminal_receipt"], "attempt_id": "foreign"}
    elif boundary == "digest_mismatch":
        marker["terminal_receipt"] = {**marker["terminal_receipt"], "readback_sha256": "b" * 64}
    elif boundary == "foreign_profile":
        marker["profile"] = "repo-node24-npm-v1"
    elif boundary == "bool_fence":
        incoming["fencing_token"] = True
    elif boundary == "stage_present":
        stage.mkdir(mode=0o700)
    elif boundary == "stage_symlink":
        stage.symlink_to(tmp_path / "absent")
    runner._write_job_marker("cancelled", marker)
    marker_path = runner._job_marker_directory / runner._job_marker_name("cancelled")
    if boundary == "mode":
        marker_path.chmod(0o644)
    elif boundary == "hardlink":
        os.link(marker_path, marker_path.with_suffix(".extra"))
    elif boundary == "oversize":
        with marker_path.open("ab") as stream:
            stream.write(b" " * (16 * 1024))
    elif boundary == "malformed":
        marker_path.write_bytes(b"not-json")
    elif boundary == "nonmapping":
        marker_path.write_bytes(b"[]")
    elif boundary in {"missing", "fifo"}:
        marker_path.unlink()
        if boundary == "fifo":
            os.mkfifo(marker_path, mode=0o600)
    original = marker_path.read_bytes() if boundary not in {"missing", "fifo"} else None
    fifo_identity = marker_path.stat() if boundary == "fifo" else None
    if boundary == "read_failure":
        def fail_read(*args):
            raise OSError("fixture marker read unavailable")
        monkeypatch.setattr(os, "read", fail_read)
    with pytest.raises(RepoSandboxError):
        runner._write_job_marker("cancelled", incoming)
    if boundary == "missing":
        assert not marker_path.exists()
    elif boundary == "fifo":
        current = marker_path.stat()
        assert stat.S_ISFIFO(current.st_mode)
        assert (current.st_dev, current.st_ino) == (fifo_identity.st_dev, fifo_identity.st_ino)
    else:
        assert marker_path.read_bytes() == original


def test_docker_deadline_is_single_bounded_budget():
    runner = RootlessDockerRepoSandbox(
        RepoSandboxSettings(
            enabled=True,
            docker_socket="unix:///run/user/1000/docker.sock",
            worker_image_digest=IMAGE,
            profile="repo-python-pytest-v1",
        )
    )
    deadline = time.monotonic() + 0.25
    bounded = runner._remaining_timeout(deadline, 30, phase="test")
    assert 0 < bounded <= 0.25
    with pytest.raises(RepoSandboxError, match="deadline expired"):
        runner._remaining_timeout(time.monotonic() - 1, 30, phase="test")


def _rootless_recovery_job(*, execution_deadline_at: str | None = None) -> RepoSandboxJob:
    return RepoSandboxJob(
        job_id="rootless-recovery-job",
        repository_root="/tmp/seraph-recovery-repository",
        patch_bytes=b"",
        allowed_paths=("tests/test_value.py",),
        test_args=("tests/test_value.py",),
        authority_digest="authority-1",
        base_digest="b" * 64,
        deadline_seconds=30,
        worker_image_digest=IMAGE,
        execution_deadline_at=execution_deadline_at,
    )


def _rootless_recovery_settings() -> RepoSandboxSettings:
    return RepoSandboxSettings(
        executor_kind="docker_rootless",
        enabled=False,
        docker_socket="unix:///run/user/1000/docker.sock",
        worker_image_digest=IMAGE,
        profile="repo-python-pytest-v1",
    )


def test_rootless_recovery_uses_canonical_kind_before_contacting_worker(monkeypatch: pytest.MonkeyPatch):
    runner = RootlessDockerRepoSandbox(_rootless_recovery_settings())
    cancel_calls: list[dict[str, object]] = []

    def fake_cancel(**kwargs: object) -> dict[str, object]:
        cancel_calls.append(kwargs)
        return {"status": "cancelled", "cleanup_proven": True}

    monkeypatch.setattr(runner, "cancel", fake_cancel)
    result = runner.recover_job(_rootless_recovery_job())

    assert runner.kind == "docker_rootless"
    assert result["status"] == "failed"
    assert result["executor_kind"] == "docker_rootless"
    assert result["posture"]["kind"] == "docker_rootless"
    assert len(cancel_calls) == 1
    assert cancel_calls[0]["deadline_at"] is not None


def test_rootless_recovery_expired_deadline_does_not_contact_docker_or_restart_cleanup(
    monkeypatch: pytest.MonkeyPatch,
):
    runner = RootlessDockerRepoSandbox(_rootless_recovery_settings())
    cancel_calls: list[dict[str, object]] = []
    docker_calls: list[object] = []

    def fake_cancel(**kwargs: object) -> dict[str, object]:
        cancel_calls.append(kwargs)
        raise AssertionError("expired recovery must not start cleanup with a fresh budget")

    def fake_docker(*args: object, **kwargs: object) -> tuple[int, bytes, bytes]:
        docker_calls.append((args, kwargs))
        raise AssertionError("expired recovery must not contact Docker")

    monkeypatch.setattr(runner, "cancel", fake_cancel)
    monkeypatch.setattr(runner, "_run_docker", fake_docker)
    expired = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    result = runner.recover_job(_rootless_recovery_job(execution_deadline_at=expired))

    assert result["status"] == "unknown_external_effect"
    assert result["reason_code"] == "recovery_deadline_expired"
    assert result["cleanup"] == {
        "status": "not_attempted",
        "reason": "deadline_expired_before_recovery_contact",
    }
    assert result["executor_kind"] == "docker_rootless"
    assert cancel_calls == []
    assert docker_calls == []


@pytest.mark.parametrize(
    ("executor_kind", "rootless", "privilege_model"),
    (
        ("docker_rootless", True, "rootless_nonroot_worker"),
        ("docker_rootful", False, "rootful_daemon_nonroot_worker"),
    ),
)
def test_docker_preflight_receipt_binds_selected_posture(
    executor_kind: str,
    rootless: bool,
    privilege_model: str,
    monkeypatch: pytest.MonkeyPatch,
):
    image = IMAGE
    config = RepoSandboxSettings(
        executor_kind=executor_kind,  # type: ignore[arg-type]
        enabled=True,
        docker_socket="unix:///run/user/1000/docker.sock",
        worker_image_digest=image,
        profile="repo-python-pytest-v1",
    )
    runner = (
        RootlessDockerRepoSandbox(config)
        if rootless
        else RootfulDockerRepoSandbox(config)
    )

    def fake_docker(args: list[str], **_kwargs: object) -> tuple[int, bytes, bytes]:
        if args[:2] == ["info", "--format"]:
            info = {
                "OSType": "linux",
                "ServerRootless": rootless,
                "SecurityOptions": ["rootless"] if rootless else [],
                "CgroupVersion": "2",
                "CgroupDriver": "systemd",
                "CpuCfsQuota": True,
                "CpuCfsPeriod": True,
                "MemoryLimit": True,
                "SwapLimit": True,
                "PidsLimit": True,
            }
            return 0, json.dumps(info).encode(), b""
        if args[:3] == ["image", "inspect", "--format"]:
            return 0, json.dumps({"RepoDigests": [image], "Id": f"sha256:{'a' * 64}"}).encode(), b""
        raise AssertionError(args)

    monkeypatch.setattr(runner, "_run_docker", fake_docker)
    receipt = runner.preflight().as_receipt()
    assert receipt["ok"] is True
    assert receipt["executor_kind"] == executor_kind
    assert receipt["posture"]["privilege_model"] == privilege_model
    assert receipt["posture"]["rootless"] is rootless
    assert receipt["posture_digest"] == executor_posture_digest(receipt["posture"])


def test_local_deadline_cannot_be_replaced_by_a_new_phase_budget(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    runner = LocalRepoRepairExecutor(_local_settings(), workspace_dir=workspace)
    repo, patch, allowed = _repo(tmp_path, slow=True)
    job = _job(runner, repo, patch, allowed, job_id="deadline-job", deadline=1)
    with pytest.raises(RepoSandboxError) as error:
        runner.execute_job(job)
    assert error.value.terminal_status == "unknown_external_effect"
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    marker = runner._read_job_marker(job.job_id)
    assert marker is not None
    assert marker["status"] == "running"
    stage = workspace / "artifacts" / "repo-sandbox" / "staging" / runner._job_stage_token(job)
    assert stage.is_dir()
    assert marker["stage_directory"] == str(stage.relative_to(workspace))
    with pytest.raises(RepoSandboxError) as replay:
        runner.execute_job(job)
    assert replay.value.terminal_status == "unknown_external_effect"


def test_persisted_selector_is_backward_compatible_and_round_trips_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "settings-workspace"
    workspace.mkdir(mode=0o700)
    monkeypatch.setattr(repo_sandbox.settings, "workspace_dir", str(workspace))
    settings_dir = workspace / "artifacts" / "repo-sandbox"
    settings_dir.mkdir(parents=True, mode=0o700)
    (workspace / "artifacts").chmod(0o700)
    legacy = {
        "enabled": False,
        "docker_socket": "",
        "worker_image_digest": "",
        "profile": "repo-python-pytest-v1",
    }
    path = settings_dir / "settings.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    path.chmod(0o600)
    loaded, error = load_persisted_repo_sandbox_settings()
    assert error is None
    assert loaded.executor_kind == "docker_rootless"

    value = _local_settings(enabled=True)
    persist_repo_sandbox_settings(value)
    loaded, error = load_persisted_repo_sandbox_settings()
    assert error is None
    assert loaded.executor_kind == "local"
    assert loaded.enabled is True
    assert json.loads(path.read_text(encoding="utf-8"))["executor_kind"] == "local"


def test_local_marker_corruption_fails_closed(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    runner = LocalRepoRepairExecutor(_local_settings(), workspace_dir=workspace)
    marker_dir = workspace / "artifacts" / "repo-sandbox" / "jobs"
    marker_dir.mkdir(parents=True, mode=0o700)
    marker = marker_dir / runner._job_marker_name("corrupt")
    marker.write_text("not-json", encoding="utf-8")
    marker.chmod(0o600)
    assert runner.reconcile({"job_id": "corrupt"})["marker"] is None
