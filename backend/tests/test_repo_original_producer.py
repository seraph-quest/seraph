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


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["test_python", "test_node"])
async def test_actual_three_iteration_group_keeps_same_live_guard_and_scope(
        accounting_db, monkeypatch, language, repository_admission_signer):
    import asyncio
    from dataclasses import replace
    from src.workflows import repo_repair_source_recovery as recovery
    from tests.test_repo_work_task_publication import _actual_source_callback_journey
    original_publish = recovery.publish_original_repository_completion
    checked = []

    async def observe_group(service, jobs, **kwargs):
        if kwargs["iteration_index"] == 3:
            result = kwargs["actual_result"]
            owner = producer.original_producer_live_owner(service, jobs, result)
            assert owner is kwargs["producer_owner"]
            with pytest.raises(ValueError):
                producer.original_producer_live_owner(service, jobs, dict(result))
            with pytest.raises(ValueError):
                producer.original_producer_live_owner(service, object(), result)
            async with jobs._session() as db:
                run = await jobs._fetch(db, kwargs["job_id"])
                registrations = [recovery.read_registered_repository_producer(run, iteration_index=index)
                    for index in (1, 2, 3)]
            guard_path = Path(registrations[2]["guard_path"])
            retained_path = guard_path.with_name(guard_path.name + ".retained-test")
            original_identity = os.fstat(owner.guard_fd)
            guard_path.rename(retained_path)
            try:
                for replacement in ("missing", "regular", "symlink"):
                    if replacement == "regular":
                        descriptor = os.open(guard_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                        os.close(descriptor)
                    elif replacement == "symlink":
                        guard_path.symlink_to(retained_path)
                    try:
                        with pytest.raises(ValueError, match="original_producer_guard_changed"):
                            producer.original_producer_live_owner(service, jobs, result)
                        with pytest.raises(ValueError, match="original_producer_guard_changed"):
                            with producer.stage_original_producer_completion(registrations[2], owner=owner, result=result):
                                pytest.fail("changed named guard was accepted")
                        assert os.fstat(owner.guard_fd).st_ino == original_identity.st_ino
                    finally:
                        if replacement != "missing":
                            guard_path.unlink()
            finally:
                retained_path.rename(guard_path)
            assert producer.original_producer_live_owner(service, jobs, result) is owner
            with pytest.raises(ValueError):
                with producer.stage_original_producer_completion(registrations[2], owner=replace(owner), result=result):
                    pytest.fail("copied original owner was accepted")
            with producer.stage_original_producer_completion(registrations[2], owner=owner, result=result) as primary:
                def forbidden(*args, **kwargs):
                    raise AssertionError("filesystem/second flock used by the wrong phase")
                with monkeypatch.context() as scoped:
                    scoped.setattr(os, "fstat", forbidden)
                    producer.assert_original_producer_completion_scope(primary)
                async def borrowed_task():
                    with pytest.raises(ValueError, match="completion_scope_owner_changed"):
                        producer.assert_original_producer_completion_scope(primary)
                await asyncio.create_task(borrowed_task())
                wrong = json.loads(producer.canonical(registrations[0]))
                wrong["job_id"] += ":foreign"
                with pytest.raises(ValueError, match="related_registration_changed"):
                    producer.stage_original_producer_related_completion(primary, wrong)
                wrong = json.loads(producer.canonical(registrations[0]))
                wrong["ready"]["pid"] = os.getpid()
                with pytest.raises(ValueError, match="pending_original_producer"):
                    producer.stage_original_producer_related_completion(primary, wrong)
                wrong = json.loads(producer.canonical(registrations[0]))
                wrong["directory_path"] = registrations[2]["directory_path"]
                with pytest.raises(ValueError):
                    producer.stage_original_producer_related_completion(primary, wrong)
                envelope_path = Path(registrations[0]["directory_path"]) / "completion.json"
                original = envelope_path.read_bytes()
                try:
                    envelope = json.loads(original)
                    envelope["signature"] = base64.b64encode(b"x" * 64).decode()
                    envelope_path.write_bytes(producer.canonical(envelope))
                    with pytest.raises(ValueError, match="signature_invalid"):
                        producer.stage_original_producer_related_completion(primary, registrations[0])
                finally:
                    envelope_path.write_bytes(original)
                with monkeypatch.context() as scoped:
                    scoped.setattr(fcntl, "flock", forbidden)
                    related = [producer.stage_original_producer_related_completion(primary, registration)
                        for registration in registrations[:2]]
                for witness in related:
                    assert producer.original_producer_completion_result(witness)["original_producer_completion"]["outcome"] == "completed_requested_check_failure"
                    with monkeypatch.context() as scoped:
                        scoped.setattr(os, "fstat", forbidden)
                        producer.assert_original_producer_completion_scope(witness)
                with pytest.raises(ValueError, match="related_registration_changed"):
                    producer.stage_original_producer_related_completion(primary, registrations[0])
                fourth = json.loads(producer.canonical(registrations[0]))
                fourth["iteration_index"] = 4
                with pytest.raises(ValueError, match="related_registration_changed"):
                    producer.stage_original_producer_related_completion(primary, fourth)
                with pytest.raises(ValueError, match="physical_completion_required"):
                    producer.stage_original_producer_related_completion(related[0], registrations[2])
                contender = os.open(registrations[2]["guard_path"], os.O_RDWR | os.O_NOFOLLOW)
                try:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(contender)
            # Closing the primary closes only its duplicate, not the Source lane.
            assert os.fstat(owner.guard_fd).st_ino == registrations[2]["ready"]["guard_identity"][1]
            for witness in [primary, *related]:
                with pytest.raises(ValueError, match="physical_completion_required"):
                    producer.assert_original_producer_completion_scope(witness)
            checked.append(language)
        return await original_publish(service, jobs, **kwargs)

    monkeypatch.setattr(recovery, "publish_original_repository_completion", observe_group)
    await _actual_source_callback_journey(accounting_db, monkeypatch, True, language)
    assert checked == [language]


@pytest.mark.parametrize('now', [8.0, 8.5, 9.0, 9.9])
def test_original_command_requires_cleanup_and_finalization_margin_before_dispatch(tmp_path, monkeypatch, now):
    from types import SimpleNamespace
    # Original D=10, physical cleanup D-1=9, command cutoff D-2=8.
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    control = producer.ProducerControl(child, 'a' * 64, 9.0)
    monkeypatch.setattr(repo_supervisor, 'ORIGINAL_PRODUCER', control)
    monkeypatch.setattr(repo_supervisor, 'CANCELLED', False)
    monkeypatch.setattr(repo_supervisor, 'time', SimpleNamespace(monotonic=lambda: now))
    spawned = []
    def forbidden_spawn(*args, **kwargs):
        spawned.append(True)
        raise AssertionError('insufficient-margin command must never dispatch')
    monkeypatch.setattr(repo_supervisor.subprocess, 'Popen', forbidden_spawn)
    try:
        with pytest.raises(ValueError, match='original_producer_command_reserve_exhausted'):
            repo_supervisor.run_command(['/usr/bin/git', '--version'], tmp_path, {}, 10.0, stream_limit=4096)
        assert spawned == [] and control.ordinal == 0 and control.authorized_commands == 0
    finally:
        parent.close()
        child.close()


def test_original_ack_cannot_consume_command_cleanup_margin(monkeypatch):
    from types import SimpleNamespace
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    control = producer.ProducerControl(child, 'a' * 64, 9.0)
    clock = {'now': 7.5}
    monkeypatch.setattr(producer, 'time', SimpleNamespace(monotonic=lambda: clock['now']))
    receive = producer.receive
    deadlines = []
    def acknowledge_then_expire(channel, deadline):
        assert channel is child
        deadlines.append(deadline)
        request = receive(parent, deadline)
        producer.send(parent, {**request, 'kind': 'command_ack'})
        reply = receive(channel, deadline)
        clock['now'] = 8.0
        return reply
    monkeypatch.setattr(producer, 'receive', acknowledge_then_expire)
    try:
        with pytest.raises(ValueError, match='original_producer_command_reserve_exhausted'):
            control.authorize(['/usr/bin/git', '--version'])
        assert deadlines == [8.0]
        assert control.no_spawn and control.interrupted and control.authorized_commands == 0
    finally:
        parent.close()
        child.close()


def test_original_post_ack_schedule_delay_never_spawns(tmp_path, monkeypatch):
    from types import SimpleNamespace
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    control = producer.ProducerControl(child, 'a' * 64, 9.0)
    monkeypatch.setattr(producer, 'time', SimpleNamespace(monotonic=lambda: 7.5))
    supervisor_times = iter([7.5, 8.0])
    monkeypatch.setattr(repo_supervisor, 'time', SimpleNamespace(monotonic=lambda: next(supervisor_times)))
    monkeypatch.setattr(repo_supervisor, 'ORIGINAL_PRODUCER', control)
    monkeypatch.setattr(repo_supervisor, 'CANCELLED', False)
    receive = producer.receive
    def acknowledge(channel, deadline):
        request = receive(parent, deadline)
        producer.send(parent, {**request, 'kind': 'command_ack'})
        return receive(channel, deadline)
    monkeypatch.setattr(producer, 'receive', acknowledge)
    spawned = []
    def forbidden_spawn(*args, **kwargs):
        spawned.append(True)
        raise AssertionError('expired post-ACK window must never dispatch')
    monkeypatch.setattr(repo_supervisor.subprocess, 'Popen', forbidden_spawn)
    try:
        with pytest.raises(ValueError, match='original_producer_command_reserve_exhausted'):
            repo_supervisor.run_command(['/usr/bin/git', '--version'], tmp_path, {}, 10.0, stream_limit=4096)
        assert control.authorized_commands == 1 and spawned == []
    finally:
        parent.close()
        child.close()


@pytest.mark.parametrize('now', [9.0, 9.5, 9.9])
def test_original_finalizer_cannot_start_in_serialization_reserve(monkeypatch, now):
    from types import SimpleNamespace
    from src.execution import repo_original_producer_finalizer as finalizer
    monkeypatch.setattr(finalizer, 'time', SimpleNamespace(monotonic=lambda: now))
    # No stage/runtime DTO can authorize physical work after D-1.
    with pytest.raises(ValueError, match='original_producer_cleanup_deadline'):
        finalizer.finalize_original_outputs({'deadline_at': 10.0}, None)
