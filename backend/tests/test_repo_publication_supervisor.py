"""Actual fixed Git supervision lifetime tests, no account/provider contacts."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from src.execution.repo_publication import posture
from src.execution import repo_publication_supervisor as supervisor
from src.execution.repo_supervisor import start_identity, exact_signal
from tests.test_repo_publication import fixture_repo


def binding():
    return {"job_id": "publication-process-proof", "root": "original-process-root",
            "principal": "original-process-owner", "attempt": 1, "authority_digest": "a" * 64,
            "input_digest": "b" * 64, "run_fingerprint": "c" * 64, "fence": 1}


def wait_file(path, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return json.loads(path.read_text())
        time.sleep(.005)
    raise AssertionError("actual supervisor file did not appear: " + str(path))


def recovered_terminal(checkpoint):
    deadline = time.monotonic() + 5
    while True:
        try:
            with supervisor.guard(Path(checkpoint["stage"])):
                return supervisor.terminal(checkpoint)
        except ValueError as exc:
            if str(exc) != "publication_producer_still_live" or time.monotonic() >= deadline:
                raise
            # The fsynced proof is intentionally published before the helper
            # releases its guard. Wait for that actual protocol step; absence
            # of the lock itself is never accepted as terminal evidence.
            time.sleep(.005)


def test_actual_fixed_git_supervisor_reaps_drains_and_seals_output(tmp_path):
    _, source, preview, patch = fixture_repo(tmp_path)
    preview["local_posture"] = posture()
    stage = tmp_path / "producer"
    checks = []
    with supervisor.guard(stage) as (_, directory, descriptor):
        admission = supervisor.admit(stage, source, preview, patch, binding(), directory=directory, guard_fd=descriptor)
        result = supervisor.run(admission, lambda: checks.append("current canonical authorization"))
        proof = supervisor.terminal(admission.checkpoint())
        assert proof["status"] == "complete" and len(checks) == len(proof["commands"])
        assert all(row["direct_reaped"] and row["output_drained"] and row["group_empty"] for row in proof["commands"])
        assert proof["cleanup"]["oracle"] == "linux_subreaper_waitpid_echild"
        assert result["local_commit"] and proof["stage_output"]["files"] > 0
        (tmp_path / "actual-supervised-complete.json").write_text(json.dumps(proof, sort_keys=True))
        print("ACTUAL_SUPERVISED_COMPLETE=" + str(tmp_path / "actual-supervised-complete.json"))
    with supervisor.guard(stage):
        assert supervisor.terminal(admission.checkpoint()) == proof
    (stage / "app.py").write_text("changed after terminal proof\n")
    with pytest.raises(ValueError, match="publication_supervisor_output_changed"):
        supervisor.terminal(admission.checkpoint())


@pytest.mark.parametrize("seconds", [True, 0, -1, 31, float("inf"), float("nan")])
def test_admission_never_extends_original_producer_deadline(tmp_path, seconds):
    _, source, preview, patch = fixture_repo(tmp_path)
    preview["local_posture"] = posture()
    with supervisor.guard(tmp_path / "producer") as (_, directory, descriptor):
        with pytest.raises(ValueError, match="publication_supervisor_deadline"):
            supervisor.admit(tmp_path / "producer", source, preview, patch, binding(), directory=directory, guard_fd=descriptor, seconds_limit=seconds)


PARENT = r'''
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from src.execution.repo_publication import SourceGit, posture
from src.execution import repo_publication_supervisor as supervisor
request = json.loads(Path(sys.argv[2]).read_text())
stage = Path(request['stage'])
preview = request['preview']; preview['local_posture'] = posture()
counter = 0
def observed(message):
    global counter
    counter += 1
    if counter == 3:
        Path(request['observed']).write_text(json.dumps(message))
        # Test interception pauses the parent notification only. The actual
        # fixed Git child and trusted helper own their guard and input pipes.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not Path(request['continue']).exists():
            time.sleep(.005)
with supervisor.guard(stage) as (_, directory, descriptor):
    admission = supervisor.admit(stage, SourceGit(Path(request['source'])), preview,
                                 bytes.fromhex(request['patch']), request['binding'],
                                 directory=directory, guard_fd=descriptor)
    Path(request['checkpoint']).write_text(json.dumps(admission.checkpoint()))
    supervisor.run(admission, lambda: None, process_observer=observed)
'''


def start_parent(tmp_path):
    root, _, preview, patch = fixture_repo(tmp_path)
    request = {"source": str(root), "stage": str(tmp_path / "producer"), "preview": preview,
               "patch": patch.hex(), "binding": binding(), "observed": str(tmp_path / "actual-child-started.json"),
               "checkpoint": str(tmp_path / "canonical-admission-input.json"), "continue": str(tmp_path / "continue")}
    path = tmp_path / "parent-input.json"
    path.write_text(json.dumps(request)); path.chmod(0o600)
    process = subprocess.Popen([sys.executable, "-I", "-c", PARENT, str(Path(__file__).resolve().parents[1]), str(path)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        checkpoint = wait_file(Path(request["checkpoint"]))
        child = wait_file(Path(request["observed"]))
        assert start_identity(child["pid"]) == child["start"]
        with pytest.raises(ValueError, match="publication_producer_still_live"):
            with supervisor.guard(Path(request["stage"])):
                pass
        admission = json.loads(Path(checkpoint["admission_path"]).read_text())
        progress = wait_file(Path(checkpoint["admission_path"]).parent / (admission["token"] + ".progress.json"))
        return process, checkpoint, child, progress, admission
    except BaseException:
        process.kill(); process.wait(timeout=5)
        raise


def test_actual_parent_kill_child_natural_exit_retains_positive_terminal_proof(tmp_path):
    parent, checkpoint, child, progress, admission = start_parent(tmp_path)
    try:
        assert start_identity(progress["supervisor"]["pid"]) == progress["supervisor"]["start"]
        parent.kill(); assert parent.wait(timeout=5) == -signal.SIGKILL
        path = Path(checkpoint["admission_path"]).parent / (admission["token"] + ".terminal.json")
        wait_file(path)
        proof = recovered_terminal(checkpoint)
        assert proof["status"] == "prefix_complete" and proof["quiescent"] is True
        assert len(proof["commands"]) == 3
        assert proof["commands"][-1]["pid"] == child["pid"] and proof["commands"][-1]["exit_code"] == 0
        assert proof["cleanup"]["signalled"] == 0
        assert all(row["direct_reaped"] and row["output_drained"] and row["group_empty"] for row in proof["commands"])
        assert proof["result"] is None
        durable = tmp_path / "actual-parent-crash-terminal.json"
        durable.write_text(json.dumps(proof, sort_keys=True))
        print("ACTUAL_PARENT_CRASH_TERMINAL=" + str(durable))
    finally:
        if parent.poll() is None:
            parent.kill(); parent.wait(timeout=5)


def test_actual_supervisor_death_has_no_terminal_proof_even_after_lock_releases(tmp_path):
    parent, checkpoint, _, progress, _ = start_parent(tmp_path)
    try:
        # This is the real fixture helper observed during its owned command,
        # killed deliberately to test the accepted failure boundary.
        assert exact_signal(progress["supervisor"]["pid"], progress["supervisor"]["start"], signal.SIGKILL)
        parent.kill(); parent.wait(timeout=5)
        deadline = time.monotonic() + 5
        while True:
            try:
                with supervisor.guard(Path(checkpoint["stage"])):
                    with pytest.raises(ValueError, match="publication_supervisor_terminal_missing"):
                        supervisor.terminal(checkpoint)
                break
            except ValueError as exc:
                assert "publication_producer_still_live" in str(exc)
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.005)
    finally:
        if parent.poll() is None:
            parent.kill(); parent.wait(timeout=5)
