"""Physical protocol negatives and genuine Source-registered bundle staging."""
from __future__ import annotations

import json
import base64
import fcntl
import select
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import pytest

from src.execution import repo_original_producer as producer
from src.execution import repo_supervisor
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.repository_admission_lifecycle import repository_admission_signer


def test_original_producer_requires_committed_registration_before_any_command(tmp_path):
    directory = tmp_path / "durable"
    directory.mkdir(mode=0o700)
    guard = os.open(tmp_path / "guard", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    request = directory / "admission.json"
    request.write_bytes(producer.canonical({"deadline_at": time.monotonic() + 5,
        "original_producer": {"directory": str(directory), "nonce": "n" * 64,
            "sources": producer.original_producer_sources()}}))
    request.chmod(0o600)
    process = subprocess.Popen([sys.executable, "-I", producer.__file__, str(request),
        str(child.fileno()), str(guard)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, pass_fds=(child.fileno(), guard), start_new_session=True)
    child.close()
    try:
        ready = producer.receive(parent, time.monotonic() + 5)
        assert ready["kind"] == "ready" and ready["pid"] == process.pid
        assert ready["admission_digest"] == producer.digest(request.read_bytes())
        assert ready["start_identity"] == repo_supervisor.start_identity(process.pid)
        pidfd = repo_supervisor.pidfd_open(process.pid)
        poll = select.poll()
        poll.register(pidfd, select.POLLIN)
        assert not poll.poll(0)
        os.close(guard)
        guard = None
        contender = os.open(tmp_path / "guard", os.O_RDWR | os.O_NOFOLLOW)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(contender)
        # Closing before ACK exercises the real producer's conservative prefix.
        parent.close()
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode != 0
        assert poll.poll(0)
        os.close(pidfd)
        contender = os.open(tmp_path / "guard", os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(contender)
        assert b"original_producer_parent_eof" in stderr
        assert not stdout
        assert sorted(path.name for path in directory.iterdir()) == ["admission.json"]
        assert not (directory / "completion.json").exists()
        # An attacker cannot adopt this actual producer's public ready record
        # into a self-issued completion after the real signing process dies.
        projection = {key: value for key, value in ready.items() if key != "kind"}
        for name in ("directory_identity", "guard_identity"):
            projection[name] = tuple(projection[name])
        observed = producer.OriginalProducerReady(**projection)
        deadline = json.loads(request.read_bytes())["deadline_at"]
        forged = {"schema": producer.COMPLETION, "ready": observed.projection(),
            "registration_digest": "a" * 64, "admission_digest": observed.admission_digest,
            "outcome": "completed_requested_checks", "manifest": {}, "outputs": {},
            "finished_monotonic": deadline - 1, "deadline_monotonic": deadline,
            "finished_wall": "2026-01-01T00:00:00+00:00", "execution_wall_cutoff": "2026-01-01T00:00:01+00:00"}
        envelope = directory / "completion.json"
        envelope.write_bytes(producer.canonical({"completion": forged,
            "signature": base64.b64encode(b"x" * 64).decode()}))
        envelope.chmod(0o600)
        with pytest.raises(ValueError, match="original_producer_signature_invalid"):
            producer.verify_completion(directory, observed, "a" * 64,
                maximum_output=4096, expected_deadline=deadline)
        with pytest.raises(ValueError, match="original_producer_completion_binding"):
            producer.verify_completion(directory, observed, "a" * 64,
                maximum_output=4096, expected_deadline=deadline + 1)
    finally:
        parent.close()
        if guard is not None:
            os.close(guard)
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_original_command_eof_never_spawns_next_command(tmp_path, monkeypatch):
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    control = producer.ProducerControl(child, "a" * 64, time.monotonic() + 5)
    monkeypatch.setattr(repo_supervisor, "ORIGINAL_PRODUCER", control)
    marker = tmp_path / "must-not-exist"
    parent.close()
    try:
        with pytest.raises((ValueError, OSError)):
            repo_supervisor.run_command([sys.executable, "-c",
                "from pathlib import Path; Path(" + repr(str(marker)) + ").write_text('bad')"],
                tmp_path, dict(os.environ), time.monotonic() + 3, stream_limit=4096)
        assert control.interrupted
        assert not marker.exists()
    finally:
        child.close()


def test_original_command_ordinal_ack_runs_real_command(tmp_path, monkeypatch):
    repo_supervisor.enable_subreaper()
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    control = producer.ProducerControl(child, "a" * 64, time.monotonic() + 5)
    monkeypatch.setattr(repo_supervisor, "ORIGINAL_PRODUCER", control)
    requests = []
    def authorize():
        message = producer.receive(parent, time.monotonic() + 5)
        requests.append(message)
        producer.send(parent, {**message, "kind": "command_ack"})
    thread = threading.Thread(target=authorize)
    thread.start()
    try:
        result = repo_supervisor.run_command([sys.executable, "-c", "print('actual-command')"],
            tmp_path, dict(os.environ), time.monotonic() + 3, stream_limit=4096)
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert requests[0]["ordinal"] == 1
        assert result["exit_code"] == 0
        assert result["stdout"] == b"actual-command\n"
    finally:
        parent.close()
        child.close()


def test_unregistered_copied_owner_is_denied():
    from types import SimpleNamespace
    owner = producer.OriginalProducerOwner("job", "iteration", -1, lambda _: None, lambda _: True)
    with pytest.raises(ValueError, match="actual_original_producer_owner_required"):
        producer.assert_original_producer_owner(owner, SimpleNamespace(job_id="job",
            iteration_binding=SimpleNamespace(iteration_id="iteration")))
    ready = producer.OriginalProducerReady("a" * 64, "copied", "n", 1, "1", "boot", (1, 1), (1, 2))
    with pytest.raises(ValueError, match="actual_original_producer_ready_required"):
        producer.assert_original_producer_ready(owner, ready)
    with pytest.raises(ValueError, match="actual_original_producer_command_required"):
        producer.assert_original_producer_command(owner, producer.OriginalProducerCommand("a" * 64, 1, "b" * 64))
    with pytest.raises(ValueError, match="actual_original_producer_physical_completion_required"):
        producer.original_producer_completion_result(producer._OriginalProducerPhysicalCompletion())


def test_durable_reader_rejects_symlink_and_replaced_output(tmp_path):
    root = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        producer.write_once(root, "output", b"original", maximum=32)
        with pytest.raises(ValueError):
            producer.write_once(root, "output", b"replacement", maximum=32)
        (tmp_path / "link").symlink_to(tmp_path / "output")
        with pytest.raises(OSError):
            producer.read_file(root, "link", 32)
        assert producer.read_file(root, "output", 32) == b"original"
    finally:
        os.close(root)


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["test_python", "test_node"])
async def test_actual_source_bundle_staging_holds_guard_and_revokes_physical_witness(
        accounting_db, monkeypatch, language, repository_admission_signer):
    from tests.test_repo_work_task_publication import _actual_source_callback_journey
    from src.workflows.repo_repair_source_recovery import read_registered_repository_producer
    flow = await _actual_source_callback_journey(accounting_db, monkeypatch, False, language)
    async with flow["factory"]() as db:
        run = await flow["jobs"]._fetch(db, flow["root_id"])
        registration = read_registered_repository_producer(run, iteration_index=1)
    # This is the actual canonical registration and original signed file bundle.
    # Physical staging alone supplies no current Source/SQL authority.
    with producer.stage_original_producer_completion(registration) as witness:
        result = producer.original_producer_completion_result(witness)
        assert result["original_producer_completion"]["outcome"] == "completed_requested_checks"
        assert result["status"] == "succeeded"
        assert "live_parent_transport" not in result
        assert "iteration_cleanup_witness" not in result
        contender = os.open(registration["guard_path"], os.O_RDWR | os.O_NOFOLLOW)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(contender)
    with pytest.raises(ValueError, match="actual_original_producer_physical_completion_required"):
        producer.original_producer_completion_result(witness)
    # Replacing a signed original output cannot mint another physical witness.
    output = Path(registration["directory_path"]) / "pytest.stdout"
    original = output.read_bytes()
    try:
        output.write_bytes(b"tampered actual output")
        with pytest.raises(ValueError, match="original_producer_output_changed"):
            with producer.stage_original_producer_completion(registration):
                pytest.fail("changed original output was accepted")
    finally:
        output.write_bytes(original)


@pytest.mark.asyncio
async def test_actual_node_passed_check_with_unapproved_output_is_not_completed_failure(
        accounting_db, monkeypatch, repository_admission_signer):
    from tests import test_repo_work_task_publication as journey
    from src.workflows import repo_repair_source as source
    from src.workflows.repo_repair_source_recovery import read_registered_repository_producer
    original_git = journey.git
    original_execute = source.execute_repository_iteration
    captured = {}

    def initialize_original_source(repository, *args):
        if args[:1] == ("init",):
            # Original operator-owned content, BEFORE Git/source admission seals.
            # The genuine staged requested test passes but writes an unapproved
            # file, triggering the fixed supervisor's post-test diff rejection.
            (repository / "tests/calculator.test.js").write_text(
                "require('node:assert/strict').equal(require('../calculator.js').add(1, 2), 3);\n"
                "require('node:fs').writeFileSync('unexpected.js', 'unapproved');\n")
        return original_git(repository, *args)

    async def observe_actual_source_result(*args, **kwargs):
        result = await original_execute(*args, **kwargs)
        captured.update(result=result, service=args[0], jobs=args[1], job_id=kwargs["job_id"])
        return result

    monkeypatch.setattr(journey, "git", initialize_original_source)
    monkeypatch.setattr(source, "execute_repository_iteration", observe_actual_source_result)
    # The existing success-only journey stops at its success assertion; the
    # assertions below examine the actual canonical Source result and bundle.
    with pytest.raises(AssertionError):
        await journey._actual_source_callback_journey(accounting_db, monkeypatch, False, "test_node")
    assert captured["result"]["status"] == "held_partial"
    assert "repository_review" not in captured["result"]
    assert "original_child_final" not in captured["result"]
    async with captured["jobs"]._session() as db:
        run = await captured["jobs"]._fetch(db, captured["job_id"])
        registration = read_registered_repository_producer(run, iteration_index=1)
    envelope = json.loads((Path(registration["directory_path"]) / "completion.json").read_bytes())
    body = envelope["completion"]
    assert body["outcome"] == "interrupted_prefix"
    assert body["manifest"]["status"] == "unknown_external_effect"
    assert body["manifest"]["reason"] == "node_unapproved_diff_path"
    assert body["manifest"]["commands"][0]["exit_code"] == 0
    assert body["manifest"]["cleanup_proven"] is True
