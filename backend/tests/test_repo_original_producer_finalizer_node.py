"""The original producer's own finalizer rejects Node supervisor readback drift.

The live Source journey runs this finalizer inside a separate supervised
producer process, where a parent monkeypatch cannot reach it. These cases call
the same fixed finalizer on a private stage with genuine output bytes and one
changed supervisor-result field each.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from config.settings import RepoSandboxSettings
from src.execution import repo_original_producer_finalizer as finalizer
from src.execution.repo_node import NodeRepoRepairExecutor, PROFILE
from src.execution.repo_sandbox import SnapshotEntry, _digest_entries


class ReachedOriginalSnapshot(Exception):
    """Every Node readback check passed; the source re-snapshot comes next."""


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _private(path, raw):
    path.write_bytes(raw)
    path.chmod(0o600)


def _stage(tmp_path, drift):
    workspace = tmp_path / "workspace"
    stage = workspace / "artifacts" / "repo-sandbox" / "staging" / ("token-" + drift)
    for directory in (workspace, *reversed(stage.parents[:3]), stage, stage / "out"):
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    patch = b"--- a/calculator.js\n+++ b/calculator.js\n@@ -1 +1 @@\n-a - b\n+a + b\n"
    _private(stage / "patch.diff", patch)
    outputs = {"diff.patch": patch, "pytest.stdout": b"# pass 1\n", "pytest.stderr": b"",
               "build.stdout": b"", "build.stderr": b""}
    for name, raw in outputs.items():
        _private(stage / "out" / name, raw)
    tested = [{"path": "calculator.js", "size_bytes": 32, "sha256": "a" * 64},
              {"path": "tests/calculator.test.js", "size_bytes": 64, "sha256": "b" * 64}]
    argv = ["npm", "test"]
    iteration = {"iteration_id": "i" * 64, "iteration_index": 1}
    result = {"job_id": "job-node-finalizer", "token": "t" * 64, "iteration_binding": iteration,
        "supervisor_pid": os.getpid(), "supervisor_start": "start-identity",
        "process_cleanup": {"cleanup_proven": True, "oracle": "linux_subreaper_waitpid_echild"},
        "status": "succeeded", "reason": None, "diff_sha256": _sha(patch),
        "tested_file_hash_metadata": tested,
        "after_digest": _digest_entries(SnapshotEntry(item["path"], item["size_bytes"], item["sha256"])
                                        for item in tested),
        "commands": [{"argv": argv, "script": "test", "exit_code": 0, "stdout_eof": True,
            "stderr_eof": True, "waited": True, "cleanup": {"cleanup_proven": True},
            "stdout_sha256": _sha(outputs["pytest.stdout"]), "stderr_sha256": _sha(outputs["pytest.stderr"])}]}
    if drift == "missing":
        result.pop("tested_file_hash_metadata")
    elif drift == "tampered":
        result["tested_file_hash_metadata"][0]["sha256"] = "f" * 64
    elif drift == "body_hash":
        result["diff_sha256"] = "f" * 64
    elif drift == "copied":
        result["token"] = "0" * 64
    elif drift == "output":
        result["commands"][0]["stdout_sha256"] = "f" * 64
    _private(stage / "out" / "supervisor-result.json", json.dumps(result).encode())
    identity = stage.stat()
    config = RepoSandboxSettings(enabled=True, executor_kind="local", profile=PROFILE)
    payload = {"deadline_at": time.monotonic() + 120, "stage": str(stage), "profile": PROFILE,
        "job_id": "job-node-finalizer",
        "token": "t" * 64, "iteration_binding": iteration,
        "plan": {"commands": [{"argv": argv, "script": "test"}]}, "runtime": {},
        "original_producer": {"config": config.model_dump(mode="json"),
            "stage_identity": [identity.st_dev, identity.st_ino], "patch_sha256": _sha(patch),
            "posture": {"supervisor_source_sha256": "s" * 64}, "max_output_bytes": 1 << 20,
            "job": {"job_id": "job-node-finalizer", "repository_root": str(workspace / "example"),
                "allowed_paths": ["calculator.js"], "test_args": argv, "authority_digest": "d" * 64,
                "base_digest": "e" * 64}}}
    return payload


@pytest.mark.parametrize("drift,reason", [
    ("missing", "original_producer_tested_tree_changed"),
    ("tampered", "original_producer_tested_tree_changed"),
    ("body_hash", "original_producer_diff_changed"),
    ("copied", "original_producer_quiescence_unproven"),
    ("output", "original_producer_command_output_changed"),
])
def test_original_finalizer_rejects_node_supervisor_readback_drift(tmp_path, monkeypatch, drift, reason):
    snapshots = []
    def reached(self, *args, **kwargs):
        snapshots.append(args)
        raise ReachedOriginalSnapshot()
    monkeypatch.setattr(NodeRepoRepairExecutor, "snapshot_repository", reached)
    control = SimpleNamespace(interrupted=False, authorized_commands=1, no_spawn=True)
    payload = _stage(tmp_path, drift)
    with pytest.raises(ValueError, match=reason):
        finalizer.finalize_original_outputs(payload, control)
    # Rejection precedes any source re-snapshot, stage removal or publication.
    assert snapshots == []
    assert Path(payload["stage"]).is_dir()


def test_original_finalizer_accepts_unchanged_node_supervisor_readback(tmp_path, monkeypatch):
    def reached(self, *args, **kwargs):
        raise ReachedOriginalSnapshot()
    monkeypatch.setattr(NodeRepoRepairExecutor, "snapshot_repository", reached)
    control = SimpleNamespace(interrupted=False, authorized_commands=1, no_spawn=True)
    with pytest.raises(ReachedOriginalSnapshot):
        finalizer.finalize_original_outputs(_stage(tmp_path, "unchanged"), control)
