"""#1065 phase B: repository work runs inside the real OS sandbox.

Every test here requests ``real_repo_isolation`` (the conftest default keeps
other repository tests on the declared not-isolated path). Isolated journeys
skip cleanly when this host cannot provide isolation.
"""
from __future__ import annotations

import difflib
import json
import os
import sys
from pathlib import Path

import pytest

from tests.repo_isolation_support import isolation_unavailable

from config.settings import RepoSandboxSettings
from src.execution import repo_isolation
from src.execution.repo_sandbox import LocalRepoRepairExecutor, RepoSandboxError, RepoSandboxJob
from src.workflows.repo_repair_source import REPOSITORY_PATCH_APPROVAL_SUMMARY, repository_patch_approval_summary


def _settings(**overrides) -> RepoSandboxSettings:
    return RepoSandboxSettings(**{"executor_kind": "local", "enabled": True, "profile": "repo-python-pytest-v1",
                                  **overrides})


def _python_repo(tmp_path: Path, test_body: str):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    (workspace / "seraph.db").write_text("SQLITE FIXTURE")
    (workspace / ".vault-key").write_text("VAULT KEY FIXTURE")
    repo = workspace / "source"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "app.py").write_text("VALUE = 1\n")
    (repo / "tests" / "test_value.py").write_text(test_body)
    patch = "".join(difflib.unified_diff(["VALUE = 1\n"], ["VALUE = 2\n"], fromfile="a/src/app.py",
                                         tofile="b/src/app.py")).encode()
    return workspace, repo, patch, ("src/app.py", "tests/test_value.py")


def _job(runner, repo, patch, allowed, posture_digest, job_id="isolated-job"):
    return RepoSandboxJob(job_id=job_id, repository_root=str(repo), patch_bytes=patch, allowed_paths=allowed,
                          test_args=("tests/test_value.py",), authority_digest="authority-1",
                          base_digest=runner.snapshot_repository(repo, runner.workspace_dir / "preview").digest,
                          deadline_seconds=60, expected_posture_digest=posture_digest)


def _isolated(preflight):
    if preflight.posture.get("isolation_variant") != "isolated":
        isolation_unavailable(preflight.posture.get("isolation_unavailable_reason"))


PROBING_TEST = '''
import errno, os, socket
from pathlib import Path
from src.app import VALUE

def test_sandbox_denies_network_and_secrets_then_checks_value():
    for secret in ({ssh!r}, {db!r}, {vault!r}):
        assert not os.path.exists(secret), secret
    sock = socket.socket()
    sock.settimeout(2)
    try:
        sock.connect(("192.0.2.1", 80))
        raise AssertionError("network reachable")
    except OSError as exc:
        assert exc.errno == errno.ENETUNREACH, exc
    assert sorted(name for _, name in socket.if_nameindex()) == ["lo"]
    assert VALUE == 2
'''


def test_python_repository_journey_runs_real_pytest_inside_the_sandbox(tmp_path, real_repo_isolation):
    workspace = tmp_path / "workspace"
    body = PROBING_TEST.format(ssh=str(Path.home() / ".ssh"), db=str(workspace / "seraph.db"),
                               vault=str(workspace / ".vault-key"))
    workspace, repo, patch, allowed = _python_repo(tmp_path, body)
    runner = LocalRepoRepairExecutor(_settings(), workspace_dir=workspace)
    preflight = runner.preflight()
    assert preflight.ok, preflight.reason
    _isolated(preflight)
    assert preflight.posture["isolation_claim"] == "bwrap_userns"
    assert preflight.posture["network_isolation"] == "netns_loopback_only_verified"
    assert preflight.posture["host_access"] == "snapshot_bind_only"
    result = runner.execute_job(_job(runner, repo, patch, allowed, preflight.posture_digest))
    # The repository's own test asserted the denials; success is the proof.
    assert result["status"] == "succeeded", result.get("failure_reason")
    assert result["manifest"]["source_original_unchanged"] is True
    assert b"1 passed" in result["outputs"]["pytest.stdout"]


def test_python_repository_test_cannot_reach_network_or_secrets_even_when_it_tries(tmp_path, real_repo_isolation):
    workspace = tmp_path / "workspace"
    body = ("import os, socket\nfrom src.app import VALUE\n\ndef test_exfiltrate():\n"
            f"    open({str(workspace / 'seraph.db')!r}).read()\n"
            "    socket.create_connection(('192.0.2.1', 80), timeout=2)\n    assert VALUE == 2\n")
    workspace, repo, patch, allowed = _python_repo(tmp_path, body)
    runner = LocalRepoRepairExecutor(_settings(), workspace_dir=workspace)
    preflight = runner.preflight()
    _isolated(preflight)
    result = runner.execute_job(_job(runner, repo, patch, allowed, preflight.posture_digest))
    assert result["status"] != "succeeded"
    assert b"FileNotFoundError" in result["outputs"]["pytest.stdout"]


def test_node_repository_journey_runs_real_npm_inside_the_sandbox(tmp_path, real_repo_isolation):
    from tests import test_repo_node
    script = ("const fs=require('node:fs'),net=require('node:net'),assert=require('node:assert/strict');"
              f"assert.equal(fs.existsSync({json.dumps(str(Path.home() / '.ssh'))}),false);"
              "const {VALUE}=require('../src/app.js'); assert.equal(VALUE,2);"
              "const s=net.connect(80,'192.0.2.1');"
              "s.on('connect',()=>{console.log('NET_REACHABLE');process.exit(3);});"
              "s.on('error',e=>{console.log('NET_DENIED '+e.code);});\n")
    executor, repo, job = test_repo_node.make_fixture(tmp_path, script=script)
    preflight = executor.preflight({"repository_ref": str(repo), "test_args": job.test_args,
                                    "allowed_paths": job.allowed_paths})
    _isolated(preflight)
    assert preflight.posture["unenforced_limits"] == ["memory"]
    assert job.expected_posture_digest == preflight.posture_digest
    result = executor.execute_job(job)
    assert result["status"] == "succeeded", result.get("failure_reason")
    assert b"NET_DENIED ENETUNREACH" in result["outputs"]["pytest.stdout"]
    assert result["manifest"]["cleanup_proven"] is True


def test_posture_variant_change_invalidates_an_earlier_approval(tmp_path, monkeypatch, real_repo_isolation):
    workspace, repo, patch, allowed = _python_repo(tmp_path, "def test_ok():\n    pass\n")
    runner = LocalRepoRepairExecutor(_settings(), workspace_dir=workspace)
    isolated = runner.preflight()
    _isolated(isolated)
    repo_isolation.clear_probe_cache()
    monkeypatch.setenv("PATH", str(tmp_path / "no-bwrap"))  # host loses bubblewrap
    degraded = runner.preflight()
    assert degraded.posture["isolation_variant"] == "not_isolated"
    assert degraded.posture["isolation_unavailable_reason"] == "bwrap_missing"
    assert degraded.posture_digest != isolated.posture_digest
    with pytest.raises(RepoSandboxError, match="posture digest"):
        runner.execute_job(_job(runner, repo, patch, allowed, isolated.posture_digest))


def test_require_isolation_refuses_on_a_host_without_the_sandbox(tmp_path, monkeypatch, real_repo_isolation):
    workspace, *_ = _python_repo(tmp_path, "def test_ok():\n    pass\n")
    monkeypatch.setenv("PATH", str(tmp_path / "no-bwrap"))
    repo_isolation.clear_probe_cache()
    strict = LocalRepoRepairExecutor(_settings(require_isolation=True), workspace_dir=workspace).preflight()
    assert not strict.ok and strict.reason == "isolation_required_unavailable"
    lenient = LocalRepoRepairExecutor(_settings(), workspace_dir=workspace).preflight()
    assert lenient.ok and lenient.posture["isolation_variant"] == "not_isolated"


def test_require_isolation_refuses_the_node_profile_too(tmp_path, monkeypatch, real_repo_isolation):
    from tests import test_repo_node
    executor, repo, job = test_repo_node.make_fixture(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "no-bwrap"))
    repo_isolation.clear_probe_cache()
    executor.config = executor.config.model_copy(update={"require_isolation": True})
    preflight = executor.preflight({"repository_ref": str(repo), "test_args": job.test_args,
                                    "allowed_paths": job.allowed_paths})
    assert not preflight.ok and preflight.reason == "isolation_required_unavailable"


def test_approval_text_states_the_bound_posture():
    not_isolated = repo_isolation.executor_posture_fields(
        repo_isolation._not_isolated("userns_denied", repo_isolation.IsolationLimits()))
    text = repository_patch_approval_summary(not_isolated)
    assert text == "NOT ISOLATED (userns_denied): " + REPOSITORY_PATCH_APPROVAL_SUMMARY
    assert "no isolation" in text and "~/.ssh" in text
    assert repository_patch_approval_summary({}).startswith("NOT ISOLATED (isolation_not_probed): ")
    isolated = {"isolation_variant": "isolated", "isolation_claim": "bwrap_userns"}
    sandboxed = repository_patch_approval_summary(isolated)
    assert not sandboxed.startswith("NOT ISOLATED") and "OS sandbox" in sandboxed and "no network" in sandboxed
    assert "outlive" not in sandboxed  # detached processes end with the sandbox


def test_persisted_isolation_fields_are_closed_per_variant():
    limits = repo_isolation.IsolationLimits()
    fields = repo_isolation.executor_posture_fields(repo_isolation._not_isolated("bwrap_missing", limits))
    assert repo_isolation.valid_posture_fields(fields)
    assert not repo_isolation.valid_posture_fields({**fields, "isolation_unavailable_reason": "made_up"})
    assert not repo_isolation.valid_posture_fields({**fields, "isolation_probe_sha256": "a" * 64})
    forged = {**fields, "isolation_variant": "isolated", "isolation_bwrap_sha256": "a" * 64,
              "isolation_probe_sha256": "b" * 64}
    forged.pop("isolation_unavailable_reason")
    # Claiming isolation while keeping not-isolated flat claims is rejected.
    assert not repo_isolation.valid_posture_fields(forged)
    assert repo_isolation.valid_posture_fields({})  # legacy postures carry no isolation fields


def test_a_command_naming_a_hidden_secret_is_refused(tmp_path, real_repo_isolation):
    secret = tmp_path / "home" / ".ssh" / "id_test"
    secret.parent.mkdir(parents=True)
    secret.write_text("KEY")
    runtime = repo_isolation.IsolationRuntime.for_python()
    limits = repo_isolation.IsolationLimits()
    posture = repo_isolation.probe(runtime, limits, hidden_paths=[str(secret.parent)], use_cache=False)
    if not posture.isolated:
        isolation_unavailable(posture.reason)
    stage = tmp_path / "stage"
    (stage / "workspace").mkdir(parents=True)
    isolation = repo_isolation.CommandIsolation(posture, runtime, limits, stage, (), (str(secret.parent),))
    with pytest.raises(repo_isolation.IsolationUnavailable):
        isolation.command(["/usr/bin/cat", str(secret)], stage / "workspace", {})
    argv, env = isolation.command(["/usr/bin/true"], stage / "workspace", {"CI": "1"})
    assert argv[0] == posture.prlimit_path and env == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}


@pytest.mark.asyncio
async def test_require_isolation_round_trips_through_the_settings_api(client, tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir(mode=0o700)
    monkeypatch.setattr(settings, "repo_sandbox", settings.repo_sandbox.model_copy(update={"enabled": False}))
    assert (await client.get("/api/settings/repo-sandbox")).json()["require_isolation"] is False
    saved = await client.put("/api/settings/repo-sandbox", json={"require_isolation": True})
    assert saved.status_code == 200 and saved.json()["require_isolation"] is True
    assert (await client.get("/api/settings/repo-sandbox")).json()["require_isolation"] is True
    rejected = await client.put("/api/settings/repo-sandbox", json={"require_isolation": "yes please"})
    assert rejected.status_code == 422


# ---------------------------------------------------------------- runtime layouts under $HOME (#1065 review P2-1)

def _fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".local" / "share" / "keyrings").mkdir(parents=True)
    (home / ".local" / "share" / "keyrings" / "login.keyring").write_text("KEYRING SECRET")
    (home / ".config").mkdir()
    (home / ".config" / "secret.json").write_text("CONFIG SECRET")
    (home / "notes.txt").write_text("HOME SECRET")
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.mark.parametrize("layout", [".local/bin/node", "bin/node", "node"])
def test_home_node_layouts_bind_only_the_executable_and_npm(tmp_path, monkeypatch, layout, real_repo_isolation):
    from tests.repo_node_runtime import required_node24_runtime
    real_node = required_node24_runtime()
    home = _fake_home(tmp_path, monkeypatch)
    link = home / layout
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(real_node)
    # A Python user site next to the Node prefix must never be bound for Node.
    (home / ".local" / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
    if layout == ".local/bin/node":
        (home / ".local" / "lib" / "node_modules" / "npm").mkdir(parents=True)
    runtime = repo_isolation.runtime_for([link])
    bound = set(runtime.read_only_paths)
    for forbidden in (home, home / ".local", home / ".local" / "share", home / ".config", home / "bin",
                      home / ".local" / "lib", home / "lib"):
        assert forbidden not in bound
    assert link in bound and real_node.resolve() in bound
    ancestors, trees = repo_isolation.host_forbidden_roots(tmp_path / "workspace")
    posture = repo_isolation.probe(runtime, repo_isolation.IsolationLimits(), forbidden_ancestors=ancestors,
                                   forbidden_trees=trees, use_cache=False)
    if not posture.isolated:
        isolation_unavailable(posture.reason)
    assert str(link) in posture.runtime_roots.split(";")
    stage = tmp_path / "stage"
    (stage / "workspace").mkdir(parents=True)
    isolation = repo_isolation.CommandIsolation(posture, runtime, repo_isolation.IsolationLimits(), stage)
    argv, env = isolation.command([str(link), "-e", (
        "const fs=require('fs');for(const p of process.argv.slice(1))"
        "{try{fs.readFileSync(p);console.log('READ '+p)}catch(e){console.log(e.code)}}"),
        str(home / ".local/share/keyrings/login.keyring"), str(home / ".config/secret.json"),
        str(home / "notes.txt")], stage / "workspace", {})
    import subprocess
    result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["ENOENT", "ENOENT", "ENOENT"]


def test_a_runtime_root_that_is_or_contains_home_is_refused(tmp_path, monkeypatch, real_repo_isolation):
    home = _fake_home(tmp_path, monkeypatch)
    ancestors, trees = repo_isolation.host_forbidden_roots(tmp_path / "workspace")
    limits = repo_isolation.IsolationLimits()
    for unsafe in (home, home / ".local", home.parent, home / ".config" / "secret.json", tmp_path / "workspace"):
        runtime = repo_isolation.IsolationRuntime(Path(sys.executable), (Path(unsafe),), "unsafe")
        posture = repo_isolation.probe(runtime, limits, forbidden_ancestors=ancestors, forbidden_trees=trees,
                                       use_cache=False)
        assert posture.variant == "not_isolated" and posture.reason == "runtime_root_unsafe", unsafe
    fields = repo_isolation.executor_posture_fields(repo_isolation._not_isolated("runtime_root_unsafe", limits))
    assert repo_isolation.valid_posture_fields(fields)
    assert repository_patch_approval_summary(fields).startswith("NOT ISOLATED (runtime_root_unsafe): ")


# ---------------------------------------------------------------- trusted post-run reads (#1065 review P3-1)

@pytest.mark.parametrize("swap", ["dev_zero_symlink", "fifo"])
def test_node_supervisor_refuses_swapped_package_json_without_hanging(tmp_path, swap):
    import time
    from tests import test_repo_node
    action = ("fs.unlinkSync('package.json');fs.symlinkSync('/dev/zero','package.json');" if swap == "dev_zero_symlink"
              else "fs.unlinkSync('package.json');require('node:child_process').execFileSync('/usr/bin/mkfifo',['package.json']);")
    script = ("const fs=require('node:fs');" + action +
              "const {VALUE}=require('../src/app.js');require('node:assert/strict').equal(VALUE,2);\n")
    executor, repo, job = test_repo_node.make_fixture(tmp_path, script=script)
    started = time.monotonic()
    result = executor.execute_job(job)
    assert time.monotonic() - started < 25
    assert result["status"] != "succeeded"
    assert "node_immutable_input" in str(result), str(result)[:2000]
