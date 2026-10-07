"""Real optional bounded Python repair execution; no installed dependencies."""
from dataclasses import replace
from pathlib import Path
import os
import time
import json
import threading
import copy

import pytest

from config.settings import RepoSandboxSettings
from src.execution.repo_sandbox import LocalRepoRepairExecutor
from src.execution.repo_publication_runtime import PROFILE, BOUNDS, RuntimeUnavailable, _read, capture, materialize, verify, resolve_entry, posture_projection
from tests.test_repo_repair_executors import _repo, _job


@pytest.fixture(scope="module")
def actual_runtime_proof():
    return capture()["proof"]


def test_actual_scalar_posture_survives_durable_authority_sanitizer(tmp_path):
    from src.workflows.job_runtime import _safe_structure
    workspace = tmp_path / "workspace"; workspace.mkdir(mode=0o700)
    runner = LocalRepoRepairExecutor(RepoSandboxSettings(enabled=True, profile=PROFILE), workspace_dir=workspace)
    preflight = runner.preflight()
    assert preflight.ok, preflight.as_receipt()
    authority = {"executor_kind": "local", "executor_profile": PROFILE, "executor_posture": preflight.posture, "executor_posture_digest": preflight.posture_digest}
    assert _safe_structure(authority) == authority
    assert all(not isinstance(value, (dict, list)) for value in preflight.posture.values())
    assert preflight.posture["publication_runtime_proof_sha256"] == posture_projection(preflight.info["runtime_identity"]["publication_runtime"], preflight.info["runtime_identity"]["publication_configuration_revision"])["publication_runtime_proof_sha256"]


@pytest.mark.parametrize("kind", ["missing", "nonfinite_bytes", "boolean_files", "changed_bound", "nonfinite_bound", "bad_digest", "bad_configuration", "loaded_library_missing"])
def test_publication_scalar_projection_rejects_missing_or_malformed_actual_proof(actual_runtime_proof, kind):
    proof = copy.deepcopy(actual_runtime_proof)
    configuration = "a" * 64
    if kind == "missing": proof = None
    elif kind == "nonfinite_bytes": proof["runtime_bytes"] = float("inf")
    elif kind == "boolean_files": proof["runtime_files"] = True
    elif kind == "changed_bound": proof["runtime_bounds"]["max_files"] += 1
    elif kind == "nonfinite_bound": proof["runtime_bounds"]["max_seconds"] = float("nan")
    elif kind == "bad_digest": proof["runtime_closure_sha256"] = "not-a-digest"
    elif kind == "bad_configuration": configuration = "not-a-digest"
    else: del proof["loaded_library"]
    with pytest.raises(RuntimeUnavailable):
        posture_projection(proof, configuration)


def test_actual_bounded_profile_repair_has_full_runtime_and_closed_environment(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    runner = LocalRepoRepairExecutor(RepoSandboxSettings(enabled=True, executor_kind="local", profile=PROFILE), workspace_dir=workspace)
    repo, patch, allowed = _repo(tmp_path)
    (repo / "pytest.py").write_text("raise RuntimeError('untrusted staged pytest')\n")
    (repo / "py.py").write_text("raise RuntimeError('untrusted staged py shim')\n")
    (repo / "tests/test_environment.py").write_text("""import os,sys,importlib.util
import pytest,py
from pathlib import Path
def test_closed_runtime():
    assert 'PYTEST_ADDOPTS' not in os.environ
    assert 'LD_PRELOAD' not in os.environ
    assert 'SERAPH_EXECUTOR_SECRET' not in os.environ
    assert sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode
    assert importlib.util.find_spec('httpx') is None
    assert '/python-runtime/packages/pytest/' in pytest.__file__
    assert '/python-runtime/packages/_pytest/_py/' in py.path.__file__
    assert all('site-packages' not in path for path in sys.path)
""")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--disable-warnings --collect-only")
    monkeypatch.setenv("LD_PRELOAD", "/untrusted/library.so")
    monkeypatch.setenv("SERAPH_EXECUTOR_SECRET", "not-exposed")
    job = _job(runner, repo, patch, allowed + ("pytest.py", "py.py", "tests/test_environment.py"), job_id="publication-profile-real", deadline=60)
    job = replace(job, test_args=("tests/test_value.py", "tests/test_environment.py"))
    from tests.test_repo_repair_local_vertical import _publication_worker_diagnostic_context
    with _publication_worker_diagnostic_context(monkeypatch):
        result = runner.execute_job(job)
    assert result["status"] == "succeeded", result
    attestation = result["manifest"]["publication_test_input"]
    assert attestation == result["readback"]["publication_test_input"]
    assert attestation["environment_unchanged"] is True
    env = attestation["environment"]
    assert env["available"] is True
    proof = env["runtime_proof"]
    assert proof["profile"] == PROFILE
    assert 1100 < proof["runtime_files"] < BOUNDS["max_files"]
    assert 50 * 1024 * 1024 < proof["runtime_bytes"] < BOUNDS["max_bytes"]
    assert env["actual_execution"]["loaded_libpython_sha256"] == proof["libpython_sha256"]
    assert env["actual_execution"]["loaded_libpython_path"].endswith(proof["libpython_name"])
    assert all("site-packages" not in item["path"] for item in env["runtime_files"])
    assert all("site-packages" not in path for path in env["actual_execution"]["trusted_runner_origins"].values())
    assert "PYTEST_ADDOPTS" not in env["effective_environment"]
    assert attestation["tested_files"] == attestation["output_files"]
    assert not any("pytest_cache" in item["path"] for item in attestation["output_files"])
    assert (repo / "src/app.py").read_text() == "VALUE = 1\n"
    assert result["cleanup"]["cleanup_proven"] is True
    receipt = tmp_path / "actual-native-runtime-receipt.json"
    receipt.write_text(json.dumps({"manifest": result["manifest"], "readback": result["readback"], "cleanup": result["cleanup"], "posture": result["effective_profile"]}, sort_keys=True, indent=2))
    print("ACTUAL_NATIVE_RUNTIME_RECEIPT=" + str(receipt))


@pytest.mark.parametrize("kind", ["byte", "mode", "extra", "link"])
def test_actual_copied_runtime_drift_is_rejected(tmp_path, kind):
    captured = capture()
    root = tmp_path / "runtime"
    deadline = time.monotonic() + 30
    materialize(root, captured, deadline_at=deadline)
    path = root / "packages/pytest/__init__.py"
    if kind == "byte":
        path.chmod(0o600); path.write_bytes(path.read_bytes() + b"\n# changed\n"); path.chmod(0o400)
    elif kind == "mode":
        path.chmod(0o500)
    elif kind == "extra":
        (root / "packages/unapproved.py").write_text("VALUE=1\n")
    else:
        (root / "packages/unapproved").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(RuntimeUnavailable):
        verify(root, captured, deadline_at=deadline)


@pytest.mark.parametrize("mode,gid,uid", [(0o666, None, None), (0o660, 999999, None), (0o600, None, 999999)])
def test_runtime_source_world_group_or_owner_trust_is_fail_closed(tmp_path, monkeypatch, mode, gid, uid):
    path = tmp_path / "source.py"; path.write_text("VALUE=1\n"); path.chmod(mode)
    if gid is not None:
        monkeypatch.setattr(os, "getgid", lambda: gid)
    if uid is not None:
        # The real opened owner is neither this declared user nor root.
        monkeypatch.setattr(os, "getuid", lambda: uid)
    with pytest.raises(RuntimeUnavailable):
        _read(path, deadline=time.monotonic() + 5)


def test_source_link_chain_escape_is_unavailable(tmp_path):
    root = tmp_path / "trusted"; root.mkdir()
    entry = root / "python"; entry.symlink_to("/outside/unattested/python")
    with pytest.raises(RuntimeUnavailable, match="link_escape"):
        resolve_entry(entry, [root])


def test_runtime_capture_reads_descriptor_size_plus_one(tmp_path, monkeypatch):
    path = tmp_path / "small.py"
    path.write_bytes(b"VALUE=1\n")
    real_fdopen = os.fdopen
    requested = []
    class Reader:
        def __init__(self, handle): self.handle = handle
        def __enter__(self): return self
        def __exit__(self, *args): self.handle.close()
        def fileno(self): return self.handle.fileno()
        def read(self, size):
            requested.append(size)
            return self.handle.read(size)
    monkeypatch.setattr(os, "fdopen", lambda descriptor, mode: Reader(real_fdopen(descriptor, mode)))
    raw, metadata = _read(path, deadline=time.monotonic() + 5)
    assert raw == b"VALUE=1\n"
    assert requested == [metadata.st_size + 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True])
async def test_stored_publication_preflight_projects_exact_profile_without_raw_authority_mutation(wrapped):
    from types import SimpleNamespace
    from src.api.workflows import _safe_repo_repair_projection
    from src.execution.repo_sandbox import executor_posture_digest
    posture = {"kind": "local", "profile": PROFILE, "isolation_claim": "none",
        "network_isolation": "not_verified", "resource_enforcement": "admission_and_wall_timeout_only",
        "image_digest": "", "limits_digest": "a" * 64,
        "local_host_execution_required": True, "runtime_proof_available": True,
        "publication_runtime_proof_sha256": "b" * 64}
    raw_digest = executor_posture_digest(posture)
    authority = {"executor_kind": "local", "executor_profile": "local:" + PROFILE,
        "executor_posture": posture, "executor_posture_digest": raw_digest,
        "sandbox_profile": PROFILE, "sandbox_image_digest": "", "sandbox_limits_digest": "a" * 64,
        "required_permissions": ["local_host_execution"], "local_host_execution_required": True}
    receipt = {"ok": True, "status": "ready", "profile": PROFILE, "posture": posture}
    checkpoint = {"checkpoint_id": "repo-repair-preflight" if not wrapped else "repo-repair-preflight:fixture-repair",
        "payload": {"receipt": receipt} if wrapped else receipt}
    job = {"declared_authority": authority, "checkpoints": [checkpoint], "status": "succeeded"}
    untouched = copy.deepcopy(job)
    operator = SimpleNamespace(principal=SimpleNamespace(principal_id="fixture-owner"), session_id="fixture-root")
    result = await _safe_repo_repair_projection("fixture-repair", job, operator=operator)
    assert result["executor_profile"] == "local:" + PROFILE
    assert result["preparation_ready"] is True and result["execution_ready"] is False
    assert result["executor_posture"]["image_digest"] is None
    assert result["executor_posture_raw"] == posture and result["executor_posture_digest"] == raw_digest
    assert result["preflight"]["ok"] is True and job == untouched


def test_optional_profile_without_loaded_library_proof_does_not_change_default(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"; workspace.mkdir(mode=0o700)
    monkeypatch.setattr("src.execution.repo_publication_runtime.sys.platform", "darwin")
    bounded = LocalRepoRepairExecutor(RepoSandboxSettings(enabled=True, profile=PROFILE), workspace_dir=workspace)
    legacy = LocalRepoRepairExecutor(RepoSandboxSettings(enabled=True), workspace_dir=workspace)
    assert bounded.preflight().ok is False
    assert legacy.preflight().ok is True


def test_actual_selected_profile_fresh_cancel_after_worker_start_has_no_adoption(tmp_path):
    workspace = tmp_path / "workspace"; workspace.mkdir(mode=0o700)
    selected = RepoSandboxSettings(enabled=True, executor_kind="local", profile=PROFILE)
    runner = LocalRepoRepairExecutor(selected, workspace_dir=workspace)
    repo, patch, allowed = _repo(tmp_path, slow=True)
    job = _job(runner, repo, patch, allowed, job_id="publication-runtime-cancel", deadline=60)
    outcome = {}
    def run():
        try:
            outcome["result"] = runner.execute_job(job)
        except BaseException as exc:
            outcome["error"] = exc
    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 30
    try:
        # Wait for the actual selected Python child and its durable PID/start
        # marker, not the earlier Git materialization process.
        while time.monotonic() < deadline:
            with runner._active_lock:
                process = (runner._active.get(job.job_id) or {}).get("process")
            marker = runner._read_job_marker(job.job_id)
            if process and process.poll() is None and str(process.args[0]).endswith("/python-runtime/bin/python") and marker and marker.get("phase") == "worker_started" and marker.get("pid") == process.pid:
                break
            time.sleep(0.01)
        else:
            pytest.fail("selected Python worker never reached durable start barrier")
        fresh = LocalRepoRepairExecutor(selected, workspace_dir=workspace)
        cancel = fresh.cancel(job_id=job.job_id, authority={"job_id": job.job_id, "authority_digest": job.authority_digest})
        assert cancel["status"] == "cancel_requested"
        thread.join(timeout=15)
        assert not thread.is_alive()
        assert "error" not in outcome, outcome
        result = outcome["result"]
        assert result["status"] == "cancelled"
        assert result["cleanup"]["cleanup_proven"] is True
        assert result["manifest"].get("publication_test_input") is None
        assert (repo / "src/app.py").read_text() == "VALUE = 1\n"
        assert not any((workspace / "artifacts/repo-sandbox/staging").iterdir())
        receipt = tmp_path / "actual-selected-profile-cancel-receipt.json"
        receipt.write_text(json.dumps({"cancel": cancel, "result": {key: result[key] for key in ("status", "manifest", "cleanup", "learning")}, "marker": runner._read_job_marker(job.job_id)}, sort_keys=True))
        print("ACTUAL_SELECTED_PROFILE_CANCEL_RECEIPT=" + str(receipt))
    finally:
        if thread.is_alive():
            runner.cancel(job_id=job.job_id, authority={"authority_digest": job.authority_digest})
            thread.join(timeout=15)


@pytest.mark.parametrize("boundary", ["authority", "attempt", "fence", "pid", "write"])
def test_cancel_binding_or_intent_failure_sends_zero_signals(tmp_path, monkeypatch, boundary):
    workspace = tmp_path / "workspace"; workspace.mkdir(mode=0o700)
    runner = LocalRepoRepairExecutor(RepoSandboxSettings(enabled=True, profile=PROFILE), workspace_dir=workspace)
    marker = {"job_id": "cancel-proof", "authority_digest": "approved", "attempt_id": "attempt-1", "fencing_token": 3, "phase": "worker_started", "status": "running", "pid": 12345, "pid_start_identity": "exact-start"}
    monkeypatch.setattr(runner, "_read_job_marker", lambda _job_id: dict(marker))
    monkeypatch.setattr(runner, "_pid_start_identity", lambda _pid: "other-start" if boundary == "pid" else "exact-start")
    signals = []
    monkeypatch.setattr(os, "killpg", lambda *args: signals.append(args))
    monkeypatch.setattr(os, "kill", lambda *args: signals.append(args))
    def write(_job_id, _value):
        if boundary == "write":
            raise OSError("fixture durable intent unavailable")
    monkeypatch.setattr(runner, "_write_job_marker", write)
    authority = {"job_id": "cancel-proof", "authority_digest": "wrong" if boundary == "authority" else "approved", "attempt_id": "other" if boundary == "attempt" else "attempt-1", "fencing_token": 4 if boundary == "fence" else 3}
    result = runner.cancel(job_id="cancel-proof", authority=authority)
    assert result["status"] == "unknown_external_effect"
    assert result["cleanup_proven"] is False
    assert signals == []
