"""#1207 (A3.3-4): the passive provider-worker entry, ``python -m src.runtime_plugins.worker``.

Component tests spawn the real entry with the closed launch and talk to it
over its framed stdio. Linux-only assertions (``/proc`` and PDEATHSIG) run in
Linux CI and skip elsewhere with the reason.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import sys

import pytest

from src.runtime_plugins import inventory
from src.runtime_plugins.protocol import MAX_PENDING, ProtocolError
from src.runtime_plugins.worker import DEGRADABLE, MANDATORY, PROVIDERS
from src.runtime_plugins.worker_entry import (CONTROL_RESERVED, EXIT_CODES, INTERPRETER_FLAGS, ORDINARY_LIMIT,
                                              REQUIRED_ENV, WORKER_BOOTSTRAP, WORKER_ENV, FrameOutbox, SlotAdmission,
                                              encode, validate_frame, worker_launch)
from src.workspace.production import DuplicateWorkspaceOwnerError, runtime_workspace_owner
from tests.support import worker_entry_1207 as harness

LINUX = sys.platform == "linux"
ALL_INACTIVE = [{"provider": name, "state": "inactive", "reason_code": None} for name in PROVIDERS]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def _lock_is_free(workspace: Path) -> bool:
    try:
        with runtime_workspace_owner(workspace):
            return True
    except DuplicateWorkspaceOwnerError:
        return False


# ------------------------------------------------------------------ launch spec

def test_launch_is_a_pinned_interpreter_with_a_closed_argv_and_env(workspace):
    spec = harness.launch(workspace)
    assert spec.argv == (os.path.abspath(harness.INTERPRETER), *INTERPRETER_FLAGS, "-c", WORKER_BOOTSTRAP,
                         str(harness.BACKEND))
    assert INTERPRETER_FLAGS == ("-I", "-B")
    assert set(spec.env) == {"SERAPH_WORKER_PARENT_PID", "SERAPH_SETTINGS_ENV_FILE", "PATH", "WORKSPACE_DIR"}
    assert spec.env["SERAPH_WORKER_PARENT_PID"] == str(os.getpid()) and spec.env["SERAPH_SETTINGS_ENV_FILE"] == ""
    # Nothing is inherited from the caller's environment.
    assert not set(spec.env) & (set(os.environ) - set(WORKER_ENV))
    with pytest.raises(ValueError, match="worker_interpreter_not_pinned"):
        worker_launch(interpreter="python3", parent_pid=os.getpid(), settings_env_file="")
    with pytest.raises(ValueError, match="worker_env_invalid"):
        worker_launch(interpreter=os.path.abspath(harness.INTERPRETER), parent_pid=os.getpid(),
                      settings_env_file="relative/.env")
    with pytest.raises(ValueError, match="worker_parent_pid_invalid"):
        worker_launch(interpreter=os.path.abspath(harness.INTERPRETER), parent_pid=1, settings_env_file="")


def test_the_closed_env_lines_up_with_the_source_inventory():
    """Every allowed name is classified; none is a credential or a composition flag (#1206)."""
    reads, _ = inventory.scan_env_reads()
    flags = {flag.env_name for flag in inventory.ENV_FLAGS}
    for name, (kind, reason) in WORKER_ENV.items():
        assert kind in inventory.ENV_READ_KINDS and kind != "credential", name
        assert reason.strip() and "\n" not in reason
        assert name not in flags, name
        if name in reads:
            assert inventory.ENV_READS[name][0] == kind, name
    assert REQUIRED_ENV <= WORKER_ENV.keys()
    # The entry's own reads are classified rows of the inventory.
    # (SERAPH_SETTINGS_ENV_FILE is read by config/settings.py, outside backend/src's scan.)
    for name in ("SERAPH_WORKER_PARENT_PID",):
        assert any(site.startswith("src.runtime_plugins.worker_entry:") for site in reads[name]), name
        assert inventory.ENV_READS[name][0] == WORKER_ENV[name][0]


# ------------------------------------------------------------------ slots and priority

def test_control_slots_are_reserved_from_ordinary_requests():
    admission = SlotAdmission()
    assert (admission.total, admission.reserved, ORDINARY_LIMIT) == (MAX_PENDING, CONTROL_RESERVED, MAX_PENDING - 4)
    for _ in range(ORDINARY_LIMIT):
        admission.admit(control=False)
    with pytest.raises(ProtocolError, match="ordinary slots exhausted"):
        admission.admit(control=False)
    for _ in range(CONTROL_RESERVED):
        admission.admit(control=True)
    with pytest.raises(ProtocolError, match="pending slots exhausted"):
        admission.admit(control=True)
    admission.release(control=False)
    admission.admit(control=True)


async def test_a_queued_control_frame_is_written_before_queued_ordinary_frames():
    written: list[bytes] = []
    gate, entered = asyncio.Event(), asyncio.Event()

    async def sink(data: bytes) -> None:
        entered.set()
        await gate.wait()
        written.append(data)

    outbox = FrameOutbox(sink)
    writer = asyncio.create_task(outbox.run())
    outbox.put(b"ordinary-1", control=False)
    await entered.wait()  # the writer took ordinary-1 and is blocked in the sink
    outbox.put(b"ordinary-2", control=False)
    outbox.put(b"ordinary-3", control=False)
    outbox.put(b"control-1", control=True)
    gate.set()
    await outbox.flush()
    outbox.close()
    await writer
    assert written == [b"ordinary-1", b"control-1", b"ordinary-2", b"ordinary-3"]


async def test_a_broken_pipe_drops_the_queue_and_flush_returns():
    async def sink(data: bytes) -> None:
        raise BrokenPipeError

    outbox = FrameOutbox(sink)
    writer = asyncio.create_task(outbox.run())
    outbox.put(b"a", control=False)
    outbox.put(b"b", control=True)
    await outbox.flush()
    outbox.close()
    await writer
    assert outbox.broken


def test_frames_are_a_closed_method_set():
    nonce = "a" * 64
    good = {"protocol": 1, "boot_nonce": nonce, "request_id": "r1", "seq": 1, "kind": "request",
            "method": "worker.hello", "payload": {}}
    assert validate_frame(dict(good), kind="request") == good
    for bad in ({**good, "method": "provider.activate"}, {**good, "payload": {"key": "x"}},
                {**good, "extra": 1}, {**good, "kind": "response"}, {**good, "protocol": 2},
                {**good, "method": "worker.readiness", "payload": {"probe_writes": "yes"}}):
        with pytest.raises(ProtocolError):
            validate_frame(bad, kind="request")
    with pytest.raises(ProtocolError):
        encode({**good, "kind": "response", "payload": {"state": "ready"}}, kind="response")


# ------------------------------------------------------------------ component: the spawned entry

def test_entry_owns_the_lock_activates_mandatory_first_and_refuses_a_second_worker(workspace):
    first = harness.spawn(workspace)
    try:
        hello = first.call("worker.hello")
        assert hello["state"] == "ready" and hello["workspace_lock"] == "held" and hello["pid"] == first.pid
        assert hello["activation_order"] == [*MANDATORY, *DEGRADABLE]
        assert hello["mandatory_ready"] is True
        assert {item["provider"]: item["state"] for item in hello["providers"]} == dict.fromkeys(PROVIDERS, "active")
        # The closed environment, as the worker itself sees it (names only).
        expected = set(harness.launch(workspace).env) | ({"LC_CTYPE"} if "LC_CTYPE" in hello["env_names"] else set())
        expected |= {"__CF_USER_TEXT_ENCODING"} if sys.platform == "darwin" else set()
        assert set(hello["env_names"]) == expected
        # Lock ownership: the workspace owner lease is held by the worker process.
        assert not _lock_is_free(workspace)
        # A second worker on the same workspace fails closed at the lock, before any activation.
        code, record = harness.run_refused(workspace)
        assert (code, record["exit"], record["reason_code"]) == (73, "workspace_owner_busy",
                                                                 "production_workspace_owner_busy")
        assert record["workspace_lock"] == "never_held" and record["providers"] == ALL_INACTIVE
        # The first worker is unaffected.
        assert first.call("worker.heartbeat") == {"state": "alive"}
        assert first.call("worker.status")["mandatory_ready"] is True
        assert first.call("worker.shutdown") == {"state": "stopped", "workspace_lock": "released"}
    finally:
        code, record = first.finish()
    assert (code, record["exit"], record["reason_code"]) == (0, "stopped", "worker_shutdown_requested")
    assert record["workspace_lock"] == "released"
    assert {item["state"] for item in record["providers"]} == {"deactivated"}
    assert _lock_is_free(workspace)


def test_a_mandatory_failure_aborts_before_any_degradable_provider(workspace):
    key = workspace / ".vault-key"
    key.write_bytes(b"not-a-key\n")
    key.chmod(0o644)  # unsafe: authority refuses it (authority_vault_key_unsafe)
    code, record = harness.spawn(workspace).finish()
    assert (code, record["exit"]) == (78, "mandatory_provider_failed")
    assert record["reason_code"] == "authority_vault_key_unsafe" and record["details"] == {"provider": "authority"}
    states = {item["provider"]: item["state"] for item in record["providers"]}
    assert states["state"] == "deactivated" and states["authority"] == "failed"
    assert {states[name] for name in ("audit", "tasks", "vault", *DEGRADABLE)} == {"inactive"}
    assert record["workspace_lock"] == "released" and _lock_is_free(workspace)


@pytest.mark.parametrize("change, reason", [
    ({"EXTRA_SETTING": "1"}, "worker_env_not_closed"),
    ({"LD_PRELOAD": "/tmp/x.so"}, "worker_env_not_closed"),
    ({"PYTHONPATH": "/tmp"}, "worker_env_not_closed"),
    ({"OPENROUTER_API_KEY": "redacted-test-value"}, "worker_env_not_closed"),
    ({"SERAPH_WORKER_PARENT_PID": None}, "worker_env_incomplete"),
    ({"WORKSPACE_DIR": "relative"}, "worker_env_invalid"),
])
def test_an_unclosed_env_is_refused_before_settings_or_the_lock(workspace, change, reason):
    env = dict(harness.launch(workspace).env)
    for name, value in change.items():
        if value is None:
            env.pop(name)
        else:
            env[name] = value
    code, record = harness.run_refused(workspace, env=env)
    assert (code, record["exit"], record["reason_code"]) == (64, "invocation_refused", reason)
    assert record["workspace_lock"] == "never_held" and record["providers"] is None
    # Names only: no value from the environment reaches the record.
    assert "redacted-test-value" not in str(record) and "/tmp/x.so" not in str(record)
    assert list(workspace.iterdir()) == []


@pytest.mark.parametrize("variant", ["no_isolation", "extra_argv", "module_form"])
def test_an_unclosed_argv_or_interpreter_is_refused(workspace, variant):
    spec = harness.launch(workspace)
    interpreter, *_rest = spec.argv
    argv = {"no_isolation": [interpreter, "-c", WORKER_BOOTSTRAP, str(harness.BACKEND)],
            "extra_argv": [*spec.argv, "--debug"],
            "module_form": [interpreter, "-m", "src.runtime_plugins.worker"]}[variant]
    code, record = harness.run_refused(workspace, argv=argv)
    assert (code, record["exit"]) == (64, "invocation_refused")
    assert record["reason_code"] == ("worker_argv_not_closed" if variant == "extra_argv"
                                     else "worker_interpreter_flags_invalid")
    assert list(workspace.iterdir()) == []


def test_a_frame_before_hello_or_an_unknown_method_fences_the_worker(workspace):
    worker = harness.spawn(workspace)
    worker.send("worker.status")
    code, record = worker.finish()
    assert (code, record["reason_code"]) == (65, "worker_hello_required")
    assert record["workspace_lock"] == "released" and _lock_is_free(workspace)

    worker = harness.spawn(workspace)
    worker.call("worker.hello")
    worker.seq += 1
    frame = harness.request(worker.nonce, worker.seq, "worker.hello")
    worker.process.stdin.write(frame.replace(b'"worker.hello"', b'"worker.helloo"'))
    worker.process.stdin.flush()
    code, record = worker.finish()
    assert (code, record["reason_code"]) == (65, "worker_frame_invalid")
    assert record["workspace_lock"] == "released"


def test_sigterm_is_an_orderly_stop(workspace):
    worker = harness.spawn(workspace)
    worker.call("worker.hello")
    worker.process.send_signal(signal.SIGTERM)
    code, record = worker.finish()
    assert (code, record["exit"], record["reason_code"]) == (0, "stopped", "worker_signal_terminate")
    assert record["workspace_lock"] == "released" and _lock_is_free(workspace)


def test_stdin_eof_is_parent_loss_with_a_clean_shutdown(workspace):
    worker = harness.spawn(workspace)
    worker.call("worker.hello")
    code, record = worker.finish()  # closes stdin: the parent's end of the channel is gone
    assert (code, record["exit"], record["reason_code"]) == (71, "parent_lost", "worker_stdin_eof")
    assert record["workspace_lock"] == "released" and _lock_is_free(workspace)


def _orphan_exit(workspace: Path, *, test_holds_stdin: bool) -> dict:
    orphan = harness.spawn_with_parent(workspace, test_holds_stdin=test_holds_stdin)
    try:
        hello = harness.read_response(orphan.stdout_r)
        assert hello is not None and hello["payload"]["pid"] == orphan.worker_pid
        assert not _lock_is_free(workspace)
        orphan.kill_parent()
        record = harness.read_exit(orphan.stderr_r)
    finally:
        orphan.close()
    assert _lock_is_free(workspace)
    return record


def test_parent_death_closes_stdin_and_the_worker_shuts_down_cleanly(workspace):
    record = _orphan_exit(workspace, test_holds_stdin=False)
    assert (record["exit_code"], record["exit"]) == (71, "parent_lost")
    # Linux also delivers PDEATHSIG; whichever arrives first names the reason.
    allowed = {"worker_stdin_eof", "worker_parent_death_signal"} if LINUX else {"worker_stdin_eof",
                                                                                "worker_parent_exited"}
    assert record["reason_code"] in allowed
    assert record["workspace_lock"] == "released"
    assert {item["state"] for item in record["providers"]} == {"deactivated"}


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="parent-death hooks exist on Linux and macOS only")
def test_parent_death_without_stdin_eof_is_seen_by_the_platform_hook(workspace):
    """A leaked stdin write end cannot keep an orphan alive: PDEATHSIG (Linux) or kqueue NOTE_EXIT (macOS)."""
    record = _orphan_exit(workspace, test_holds_stdin=True)
    assert (record["exit_code"], record["exit"]) == (71, "parent_lost")
    assert record["reason_code"] == ("worker_parent_death_signal" if LINUX else "worker_parent_exited")
    assert record["workspace_lock"] == "released"


def test_a_start_whose_parent_is_already_gone_is_refused(workspace):
    env = dict(harness.launch(workspace).env, SERAPH_WORKER_PARENT_PID=str(os.getpid() + 7919))
    code, record = harness.run_refused(workspace, env=env)
    assert (code, record["reason_code"]) == (71, "worker_parent_gone_at_start")
    assert list(workspace.iterdir()) == []


@pytest.mark.skipif(not LINUX, reason="/proc and prctl(PR_SET_DUMPABLE) are Linux-only; runs in Linux CI")
@pytest.mark.skipif(LINUX and os.geteuid() == 0, reason="root bypasses the non-dumpable /proc ownership check")
def test_proc_of_the_worker_is_unreadable_on_linux(workspace):
    worker = harness.spawn(workspace)
    try:
        hello = worker.call("worker.hello")
        assert hello["hardening"] == {"platform": "linux", "dumpable": 0,
                                      "parent_death_signal": int(signal.SIGHUP),
                                      "parent_watch": ["pdeathsig", "stdin_eof"]}
        for name in ("environ", "mem"):
            with pytest.raises(PermissionError):
                with open(f"/proc/{worker.pid}/{name}", "rb") as handle:
                    handle.read(1)
        # A non-dumpable process's /proc entries belong to root.
        assert os.stat(f"/proc/{worker.pid}/environ").st_uid == 0
        assert worker.call("worker.shutdown")["workspace_lock"] == "released"
    finally:
        worker.finish()


@pytest.mark.skipif(LINUX, reason="macOS reports no Linux hardening; Linux asserts its own")
def test_non_linux_reports_no_linux_hardening(workspace):
    worker = harness.spawn(workspace)
    try:
        hardening = worker.call("worker.hello")["hardening"]
        assert hardening["dumpable"] is None and hardening["parent_death_signal"] is None
        assert hardening["parent_watch"] == (["kqueue", "stdin_eof"] if sys.platform == "darwin" else ["stdin_eof"])
        worker.call("worker.shutdown")
    finally:
        worker.finish()


def test_readiness_is_ordinary_and_answered_after_control(workspace):
    worker = harness.spawn(workspace)
    try:
        worker.call("worker.hello")
        worker.send("worker.readiness", {"probe_writes": True})
        response = harness.read_response(worker.process.stdout.fileno())
        assert response["method"] == "worker.readiness" and response["payload"]["mandatory_ready"] is True
        assert worker.call("worker.shutdown")["state"] == "stopped"
    finally:
        worker.finish()


def test_exit_code_table_is_fixed():
    assert dict(EXIT_CODES) == {"stopped": 0, "invocation_refused": 64, "protocol_violation": 65,
                                "composition_refused": 66, "hardening_unavailable": 69, "internal_error": 70,
                                "parent_lost": 71, "workspace_owner_busy": 73, "workspace_lock_unavailable": 74,
                                "mandatory_provider_failed": 78}
