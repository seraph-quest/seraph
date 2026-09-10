from __future__ import annotations

import asyncio
import os
import signal
import stat
import textwrap
import threading
import time
from pathlib import Path

import pytest

from config.settings import settings
from src.agent.session import session_manager
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.audit.repository import audit_repository
from src.tools.audit import wrap_tools_for_audit
from src.tools import process_tools as process_tools_module
from src.tools.process_tools import (
    _ProcessDescendantIdentity,
    _ProcessLeaderIdentity,
    SessionProcessCleanupError,
    list_processes,
    process_runtime_manager,
    read_process_output,
    run_command,
    start_process,
    stop_process,
)


@pytest.fixture(autouse=True)
def reset_process_runtime():
    process_runtime_manager.reset_for_tests()
    yield
    process_runtime_manager.reset_for_tests()


def _write_script(name: str, body: str) -> str:
    root = Path(settings.workspace_dir)
    root.mkdir(parents=True, exist_ok=True)
    script_path = root / name
    script_path.write_text(textwrap.dedent(body), encoding="utf-8")
    return script_path.name


def _write_workspace_file(name: str, body: str) -> str:
    root = Path(settings.workspace_dir)
    root.mkdir(parents=True, exist_ok=True)
    file_path = root / name
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(body, encoding="utf-8")
    return name


def test_run_command_success():
    script_name = _write_script(
        "wave1_process_echo.py",
        """
        print("guardian process ready")
        """,
    )

    result = run_command(
        command="python3",
        args_json=f'["{script_name}"]',
    )

    assert "guardian process ready" in result


def test_run_command_timeout_does_not_wait_for_descendant_held_pipes():
    # The child intentionally calls setsid and escapes the managed group. This
    # bounds the caller but documents the residual: a general sandbox/tree
    # kill for detached sessions is outside this process-tool contract.
    script_name = _write_script(
        "wave_process_orphaned_pipe.py",
        """
        import os
        import subprocess
        import sys
        import time

        subprocess.Popen(
            [sys.executable, "-c", "import os,time; os.setsid(); time.sleep(3)"],
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        time.sleep(30)
        """,
    )
    started = time.monotonic()

    result = process_runtime_manager.run_command(
        command="python3",
        args_json=f'["{script_name}"]',
        timeout_seconds=1,
    )

    assert result["timed_out"] is True
    assert result["cleanup_status"] == "unknown"
    retained_worker_root = Path(result["worker_root"])
    assert retained_worker_root.exists()
    assert time.monotonic() - started < 4
    time.sleep(2.2)
    assert process_tools_module._delete_runtime_dir(retained_worker_root) is True


def test_run_command_reports_unknown_after_parent_exits_before_cleanup():
    """A vanished leader leaves its descendant group recoverable but unverifiable."""
    script_name = _write_script(
        "wave_process_parent_exits_same_group.py",
        """
        import pathlib
        import subprocess
        import sys

        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import pathlib,time; time.sleep(2); pathlib.Path('same-group-survivor.marker').write_text('survived')",
            ],
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        pathlib.Path("parent-exited.marker").write_text("parent-exited")
        """,
    )
    workspace = Path(settings.workspace_dir)
    survivor_marker = workspace / "same-group-survivor.marker"
    parent_marker = workspace / "parent-exited.marker"
    survivor_marker.unlink(missing_ok=True)
    parent_marker.unlink(missing_ok=True)
    started = time.monotonic()

    result = process_runtime_manager.run_command(
        command="python3",
        args_json=f'["{script_name}"]',
        timeout_seconds=1,
    )

    assert result["timed_out"] is True
    assert result["cleanup_status"] == "unknown"
    retained_worker_root = Path(result["worker_root"])
    assert retained_worker_root.exists()
    assert parent_marker.is_file()
    assert time.monotonic() - started < 4
    time.sleep(2.2)
    assert survivor_marker.read_text(encoding="utf-8") == "survived"
    assert process_tools_module._delete_runtime_dir(retained_worker_root) is True


def test_run_command_cancel_event_kills_in_flight_process_within_bound():
    script_name = _write_script(
        "wave_process_cancellation.py",
        """
        import time
        time.sleep(30)
        """,
    )
    cancel_event = threading.Event()
    result_holder = {}

    def run():
        result_holder["result"] = process_runtime_manager.run_command(
            command="python3",
            args_json=f'["{script_name}"]',
            timeout_seconds=30,
            cancel_event=cancel_event,
        )

    worker = threading.Thread(target=run)
    worker.start()
    time.sleep(0.35)
    cancel_event.set()
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert result_holder["result"]["cancelled"] is True
    assert result_holder["result"]["ok"] is False
    assert result_holder["result"]["timed_out"] is False


def test_stop_process_reports_unknown_for_live_detached_descendant(monkeypatch):
    script_name = _write_script(
        "wave_process_live_detached_snapshot.py",
        """
        import time
        time.sleep(30)
        """,
    )

    started = start_process(command="python3", args_json=f'["{script_name}"]')
    process_id = started.split("process=")[1].split(",")[0]
    managed_payload = next(
        process
        for process in process_runtime_manager.list_processes()
        if process["process_id"] == process_id
    )
    output_path = Path(managed_payload["output_path"])
    worker_root = Path(managed_payload["worker_root"])

    current_identity = process_tools_module._read_process_identity(os.getpid())
    assert current_identity is not None
    monkeypatch.setattr(
        process_tools_module,
        "_snapshot_process_descendants",
        lambda _process, _leader: (
            _ProcessDescendantIdentity(
                pid=current_identity.pid,
                process_group_id=current_identity.process_group_id,
                start_time=current_identity.start_time,
            ),
        ),
    )

    stopped = process_runtime_manager.stop_process(process_id=process_id, force=True)

    assert stopped is not None
    assert stopped["cleanup_status"] == "unknown"
    assert stopped["stopped"] is False
    assert stopped["remaining_descendants"] is not None
    assert stopped["remaining_descendants"] >= 1
    assert stopped["registry_removed"] is False
    assert stopped["artifacts_removed"] is False
    assert any(
        process["process_id"] == process_id
        for process in process_runtime_manager.list_processes()
    )
    assert output_path.exists()
    assert worker_root.exists()


def test_run_command_uses_disposable_worker_home_and_cleans_up():
    script_name = _write_script(
        "wave1_process_worker_env.py",
        """
        import os
        print(os.environ["HOME"])
        print(os.environ["TMPDIR"])
        """,
    )

    result = run_command(
        command="python3",
        args_json=f'["{script_name}"]',
    )

    lines = [line.strip() for line in result.splitlines() if line.strip()]
    assert len(lines) == 2
    assert not lines[0].startswith(str(Path(settings.workspace_dir).resolve()))
    assert lines[0] == lines[1]
    assert not Path(lines[0]).exists()


def test_run_command_scrubs_ambient_secret_environment(monkeypatch):
    monkeypatch.setenv("SERAPH_SHOULD_NOT_LEAK", "ambient-secret")
    script_name = _write_script(
        "wave3_process_env_scrub.py",
        """
        import os
        print(os.environ.get("SERAPH_SHOULD_NOT_LEAK", "missing"))
        print(os.environ.get("SERAPH_SANDBOX_ENV", "missing"))
        """,
    )

    result = run_command(command="python3", args_json=f'["{script_name}"]')

    assert "ambient-secret" not in result
    assert "missing" in result
    assert "allowlisted" in result


def test_run_command_rejects_inline_python():
    result = run_command(command="python3", args_json='["-c","print(1)"]')
    assert result == "Error: Inline Python execution belongs in execute_code, not the process runtime."


def test_run_command_rejects_python_network_client_imports():
    script_name = _write_script(
        "wave_db_process_network_escape.py",
        """
        import socket
        print(socket.gethostname())
        """,
    )

    result = run_command(command="python3", args_json=f'["{script_name}"]')

    assert result == "Error: script network clients are blocked in the process runtime."


def test_run_command_rejects_workspace_escape():
    result = run_command(command="python3", args_json='["missing.py"]', cwd="../")
    assert result == "Error: cwd must stay within the workspace."


def test_run_command_rejects_workspace_escape_via_git_path_flag():
    result = run_command(command="git", args_json='["-C","/","status"]')
    assert result == "Error: -C path must stay within the workspace."


def test_run_command_rejects_absolute_file_argument_outside_workspace():
    result = run_command(command="cat", args_json='["/etc/passwd"]')
    assert result == "Error: path argument must stay within the workspace."


def test_run_command_rejects_grep_file_argument_outside_workspace():
    result = run_command(command="grep", args_json='["localhost","/etc/hosts"]')
    assert result == "Error: path argument must stay within the workspace."


def test_run_command_rejects_cat_secret_like_workspace_file():
    _write_workspace_file(".env", "SERAPH_TEST_SECRET=do-not-read\n")

    result = run_command(command="cat", args_json='[".env"]')

    assert result == "Error: path argument cannot target secret-like workspace files."


def test_run_command_rejects_cat_filesystem_secret_name():
    _write_workspace_file("id_rsa", "PRIVATE KEY\n")

    result = run_command(command="cat", args_json='["id_rsa"]')

    assert result == "Error: path argument cannot target secret-like workspace files."


@pytest.mark.parametrize("command", ["grep", "rg"])
def test_run_command_rejects_search_of_secret_like_workspace_file(command):
    _write_workspace_file(".env.local", "SERAPH_TEST_SECRET=do-not-search\n")

    result = run_command(command=command, args_json='["SERAPH_TEST_SECRET",".env.local"]')

    assert result == "Error: path argument cannot target secret-like workspace files."


@pytest.mark.parametrize(
    ("command", "args_json", "label"),
    [
        ("grep", '["-R","SERAPH_TEST_SECRET","."]', "path argument"),
        ("grep", '["-R","SERAPH_TEST_SECRET"]', "path argument"),
        ("rg", '["SERAPH_TEST_SECRET","."]', "path argument"),
        ("rg", '["SERAPH_TEST_SECRET"]', "path argument"),
        ("find", '[".","-name",".env","-print"]', "search path"),
        ("find", '[]', "search path"),
    ],
)
def test_run_command_rejects_recursive_search_paths_containing_secret_like_files(command, args_json, label):
    _write_workspace_file(".env", "SERAPH_TEST_SECRET=do-not-search\n")

    result = run_command(command=command, args_json=args_json)

    assert result == f"Error: {label} cannot recursively search workspace paths containing secret-like files."


def test_run_command_rejects_grep_exclude_from_outside_workspace():
    result = run_command(command="grep", args_json='["--exclude-from","/etc/hosts","localhost","hosts.txt"]')
    assert result == "Error: --exclude-from path must stay within the workspace."


def test_run_command_rejects_rg_ignore_file_outside_workspace():
    result = run_command(command="rg", args_json='["--ignore-file","/etc/hosts","localhost","hosts.txt"]')
    assert result == "Error: --ignore-file path must stay within the workspace."


def test_run_command_rejects_grep_attached_file_flag_outside_workspace():
    result = run_command(command="grep", args_json='["-f/etc/hosts","localhost","hosts.txt"]')
    assert result == "Error: -f path must stay within the workspace."


def test_run_command_rejects_rg_attached_file_flag_outside_workspace():
    result = run_command(command="rg", args_json='["-f/etc/hosts","localhost","hosts.txt"]')
    assert result == "Error: -f path must stay within the workspace."


def test_run_command_rejects_sed_attached_file_flag_outside_workspace():
    result = run_command(command="sed", args_json='["-f/etc/hosts","hosts.txt"]')
    assert result == "Error: -f path must stay within the workspace."


def test_run_command_rejects_grep_clustered_file_flag_outside_workspace():
    result = run_command(command="grep", args_json='["-rf/etc/hosts","localhost","hosts.txt"]')
    assert result == "Error: -f path must stay within the workspace."


def test_run_command_rejects_sed_clustered_file_flag_outside_workspace():
    result = run_command(command="sed", args_json='["-nf/etc/hosts","hosts.txt"]')
    assert result == "Error: -f path must stay within the workspace."


@pytest.mark.parametrize("find_action", ["-exec", "-ok"])
def test_run_command_rejects_dangerous_find_exec_and_ok_actions(find_action):
    result = run_command(command="find", args_json=f'[".","-name","*.py","{find_action}","cat","{{}}",";"]')

    assert result == f"Error: find action {find_action} is blocked in the process runtime."


@pytest.mark.parametrize(
    ("command", "args_json"),
    [
        ("cat", '["outside-link.txt"]'),
        ("grep", '["needle","outside-link.txt"]'),
    ],
)
def test_run_command_rejects_symlink_to_outside_workspace_path_arguments(tmp_path, command, args_json):
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("needle\n", encoding="utf-8")
    workspace_link = Path(settings.workspace_dir) / "outside-link.txt"
    workspace_link.unlink(missing_ok=True)
    workspace_link.symlink_to(outside_file)

    result = run_command(command=command, args_json=args_json)

    assert result == "Error: path argument must stay within the workspace."


def test_start_process_rejects_absolute_script_path_outside_workspace():
    result = start_process(command="python3", args_json='["/tmp/outside.py"]')
    assert result == "Error: script path must stay within the workspace."


@pytest.mark.asyncio
async def test_run_command_audit_redacts_command_output(async_db):
    await session_manager.get_or_create("s1")
    script_name = _write_script(
        "wave1_process_secret.py",
        """
        print("top-secret process output")
        """,
    )
    audited = wrap_tools_for_audit([run_command])[0]

    tokens = set_runtime_context("s1", "off")
    try:
        await asyncio.to_thread(
            audited,
            command="python3",
            args_json=f'["{script_name}"]',
        )
    finally:
        reset_runtime_context(tokens)

    events = await audit_repository.list_events(limit=10)
    process_events = [
        event
        for event in events
        if event["tool_name"] == "run_command"
        and event["event_type"] in {"tool_call", "tool_result"}
    ]
    assert len(process_events) == 2
    for event in process_events:
        assert "top-secret process output" not in event["summary"]
        assert "top-secret process output" not in str(event["details"])


def test_start_list_read_and_stop_process():
    script_name = _write_script(
        "wave1_process_long.py",
        """
        import time
        print("started", flush=True)
        time.sleep(30)
        """,
    )

    started = start_process(command="python3", args_json=f'["{script_name}"]')
    process_id = started.split("process=")[1].split(",")[0]

    for _ in range(20):
        listed = list_processes()
        if process_id in listed:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("process did not appear in list_processes output")

    managed_payload = next(
        process
        for process in process_runtime_manager.list_processes()
        if process["process_id"] == process_id
    )
    output_path = Path(managed_payload["output_path"])
    worker_root = Path(managed_payload["worker_root"])

    output = ""
    for _ in range(20):
        output = read_process_output(process_id=process_id)
        if "started" in output:
            break
        time.sleep(0.05)
    assert "started" in output

    stopped = stop_process(process_id=process_id)
    assert f"Stopped process '{process_id}'" in stopped
    assert "remaining_descendants=0" in stopped
    stop_receipt = stop_process.get_audit_result_payload({}, stopped)
    assert stop_receipt is not None
    assert stop_receipt[1]["stopped"] is True
    assert stop_receipt[1]["remaining_descendants"] == 0
    assert stop_receipt[1]["registry_removed"] is True
    assert stop_receipt[1]["artifacts_removed"] is True
    assert not any(
        process["process_id"] == process_id
        for process in process_runtime_manager.list_processes()
    )
    assert not output_path.exists()
    assert not worker_root.exists()


def test_stop_process_reports_unknown_after_parent_exit_and_retains_receipt(monkeypatch):
    survivor_marker = Path(settings.workspace_dir) / "process-stop-survivor.marker"
    child_ready_marker = Path(settings.workspace_dir) / "process-stop-child-ready.marker"
    child_pid_marker = Path(settings.workspace_dir) / "process-stop-child.pid"
    parent_marker = Path(settings.workspace_dir) / "process-stop-parent.marker"
    survivor_marker.unlink(missing_ok=True)
    child_ready_marker.unlink(missing_ok=True)
    child_pid_marker.unlink(missing_ok=True)
    parent_marker.unlink(missing_ok=True)
    script_name = _write_script(
        "wave_process_stop_parent_exit.py",
        """
        import pathlib
        import subprocess
        import sys
        import time

        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os,pathlib,time; pathlib.Path('process-stop-child.pid').write_text(str(os.getpid())); pathlib.Path('process-stop-child-ready.marker').write_text('ready'); pathlib.Path('process-stop-survivor.marker').write_text('survived'); time.sleep(5)",
            ],
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        for _ in range(100):
            if pathlib.Path("process-stop-child-ready.marker").is_file():
                break
            time.sleep(0.01)
        pathlib.Path("process-stop-parent.marker").write_text("parent-exited")
        time.sleep(2)
        """,
    )

    original_snapshot = process_tools_module._snapshot_process_descendants

    def snapshot_live_child(process, leader_identity):
        if process.poll() is None and child_pid_marker.is_file():
            try:
                child_pid = int(child_pid_marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                child_pid = None
            if child_pid is not None:
                child_identity = process_tools_module._read_process_identity(child_pid)
                if child_identity is not None:
                    return (
                        _ProcessDescendantIdentity(
                            pid=child_identity.pid,
                            process_group_id=child_identity.process_group_id,
                            start_time=child_identity.start_time,
                        ),
                    )
        return original_snapshot(process, leader_identity)

    monkeypatch.setattr(process_tools_module, "_snapshot_process_descendants", snapshot_live_child)
    started = start_process(command="python3", args_json=f'["{script_name}"]')
    process_id = started.split("process=")[1].split(",")[0]
    managed = process_runtime_manager._processes[process_id]
    for _ in range(100):
        process_runtime_manager.list_processes()
        if managed.descendant_identities:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("live descendant snapshot was not persisted")
    persisted_descendant = managed.descendant_identities
    assert persisted_descendant
    for _ in range(100):
        listed = process_runtime_manager.list_processes()
        if parent_marker.is_file() and any(
            process["process_id"] == process_id and process["status"] == "exited"
            for process in listed
        ):
            break
        time.sleep(0.05)
    else:
        raise AssertionError("parent did not exit while its descendant remained in the group")

    managed_payload = next(process for process in listed if process["process_id"] == process_id)
    output_path = Path(managed_payload["output_path"])
    worker_root = Path(managed_payload["worker_root"])
    stopped = process_runtime_manager.stop_process(process_id=process_id)

    assert stopped is not None
    assert stopped["stopped"] is False
    assert stopped["cleanup_status"] == "unknown"
    assert stopped["remaining_descendants"] is not None
    assert stopped["remaining_descendants"] >= 1
    assert stopped["registry_removed"] is False
    assert stopped["artifacts_removed"] is False
    assert any(
        process["process_id"] == process_id
        for process in process_runtime_manager.list_processes()
    )
    assert output_path.exists()
    assert worker_root.exists()
    child_identity = persisted_descendant[0]
    assert survivor_marker.read_text(encoding="utf-8") == "survived"
    os.kill(child_identity.pid, signal.SIGKILL)
    reconciled = None
    for _ in range(8):
        time.sleep(0.1)
        reconciled = process_runtime_manager.stop_process(process_id=process_id)
        if reconciled is not None and reconciled["cleanup_status"] == "stopped":
            break
    assert reconciled is not None
    assert reconciled["cleanup_status"] == "stopped"
    assert reconciled["stopped"] is True
    assert reconciled["registry_removed"] is True
    assert reconciled["artifacts_removed"] is True
    assert survivor_marker.read_text(encoding="utf-8") == "survived"
    assert not output_path.exists()
    assert not worker_root.exists()


def test_stop_process_rejects_stale_leader_identity_without_signaling(monkeypatch):
    script_name = _write_script(
        "wave_process_stale_identity.py",
        """
        import time
        time.sleep(30)
        """,
    )
    started = start_process(command="python3", args_json=f'["{script_name}"]')
    process_id = started.split("process=")[1].split(",")[0]
    managed = process_runtime_manager._processes[process_id]
    assert managed.leader_identity is not None
    stale_identity = _ProcessLeaderIdentity(
        pid=managed.leader_identity.pid,
        process_group_id=managed.leader_identity.process_group_id,
        start_time=managed.leader_identity.start_time + 1,
    )
    signal_calls = []
    monkeypatch.setattr(process_tools_module, "_read_process_identity", lambda _pid: stale_identity)
    monkeypatch.setattr(process_tools_module.os, "killpg", lambda *args: signal_calls.append(args))

    stopped = process_runtime_manager.stop_process(process_id=process_id, force=True)

    assert stopped is not None
    assert stopped["cleanup_status"] == "unknown"
    assert stopped["stopped"] is False
    assert stopped["remaining_descendants"] is None or stopped["remaining_descendants"] >= 1
    assert stopped["registry_removed"] is False
    assert stopped["artifacts_removed"] is False
    assert signal_calls == []
    assert managed.popen.poll() is None
    assert any(
        process["process_id"] == process_id
        for process in process_runtime_manager.list_processes()
    )


def test_stop_process_reports_failed_artifact_cleanup_and_retains_handle(monkeypatch):
    script_name = _write_script(
        "wave_process_failed_artifact_cleanup.py",
        """
        import time
        time.sleep(30)
        """,
    )
    started = start_process(command="python3", args_json=f'["{script_name}"]')
    process_id = started.split("process=")[1].split(",")[0]
    monkeypatch.setattr(process_runtime_manager, "_delete_process_artifacts", lambda _process: False)

    stopped = process_runtime_manager.stop_process(process_id=process_id, force=True)

    assert stopped is not None
    assert stopped["cleanup_status"] == "failed"
    assert stopped["stopped"] is False
    assert stopped["registry_removed"] is False
    assert stopped["artifacts_removed"] is False
    assert any(
        process["process_id"] == process_id
        for process in process_runtime_manager.list_processes()
    )


def test_concurrent_stop_returns_conflict_without_racing_signals(monkeypatch):
    script_name = _write_script(
        "wave_process_concurrent_stop.py",
        """
        import time
        time.sleep(30)
        """,
    )
    started = start_process(command="python3", args_json=f'["{script_name}"]')
    process_id = started.split("process=")[1].split(",")[0]
    entered = threading.Event()
    release = threading.Event()
    original_stop = process_runtime_manager._stop_managed_process

    def blocked_stop(process, *, force):
        entered.set()
        assert release.wait(timeout=3)
        return original_stop(process, force=force)

    monkeypatch.setattr(process_runtime_manager, "_stop_managed_process", blocked_stop)
    first_result = {}
    worker = threading.Thread(
        target=lambda: first_result.setdefault(
            "payload", process_runtime_manager.stop_process(process_id=process_id, force=True)
        )
    )
    worker.start()
    assert entered.wait(timeout=3)

    conflict = process_runtime_manager.stop_process(process_id=process_id, force=True)

    assert conflict is not None
    assert conflict["cleanup_status"] == "conflict"
    assert conflict["stopped"] is False
    assert conflict["registry_removed"] is False
    release.set()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert first_result["payload"]["cleanup_status"] == "stopped"


def test_session_cleanup_fences_new_process_and_records_receipt(monkeypatch):
    script_name = _write_script(
        "wave_process_session_fence.py",
        """
        import time
        time.sleep(30)
        """,
    )
    tokens = set_runtime_context("session-fence", "high_risk")
    try:
        started = start_process(command="python3", args_json=f'["{script_name}"]')
        process_id = started.split("process=")[1].split(",")[0]
    finally:
        reset_runtime_context(tokens)

    entered = threading.Event()
    release = threading.Event()
    original_stop = process_runtime_manager._stop_managed_process

    def blocked_stop(process, *, force):
        entered.set()
        assert release.wait(timeout=3)
        return original_stop(process, force=force)

    monkeypatch.setattr(process_runtime_manager, "_stop_managed_process", blocked_stop)
    cleanup_result = {}
    worker = threading.Thread(
        target=lambda: cleanup_result.setdefault(
            "count", process_runtime_manager.stop_processes_for_session("session-fence")
        )
    )
    worker.start()
    assert entered.wait(timeout=3)

    tokens = set_runtime_context("session-fence", "high_risk")
    try:
        with pytest.raises(ValueError, match="session cleanup is in progress"):
            process_runtime_manager.start_process(command="python3", args_json=f'["{script_name}"]')
        with pytest.raises(RuntimeError, match="session cleanup is in progress"):
            process_runtime_manager.stop_process(process_id=process_id, force=True)
    finally:
        reset_runtime_context(tokens)

    release.set()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert cleanup_result["count"] == 1
    receipt = process_runtime_manager.last_session_cleanup_receipt
    assert receipt == {
        "session_id": "session-fence",
        "requested": 1,
        "stopped": 1,
        "unknown": 0,
        "failed": 0,
        "conflict": 0,
    }
    assert not any(
        process["process_id"] == process_id
        for process in process_runtime_manager.list_all_processes()
    )


def test_session_cleanup_fails_closed_when_fence_is_already_owned():
    session_id = "session-cleanup-conflict"
    assert process_runtime_manager.begin_session_cleanup(session_id) is True
    try:
        with pytest.raises(RuntimeError, match="session cleanup is already in progress"):
            process_runtime_manager.stop_processes_for_session(session_id)
        assert process_runtime_manager.last_session_cleanup_receipt == {
            "session_id": session_id,
            "requested": 0,
            "stopped": 0,
            "unknown": 0,
            "failed": 0,
            "conflict": 1,
        }
    finally:
        process_runtime_manager.end_session_cleanup(session_id)


def test_session_cleanup_removes_process_registry_and_artifacts():
    script_name = _write_script(
        "wave_process_session_cleanup.py",
        """
        import time
        print("session-cleanup", flush=True)
        time.sleep(30)
        """,
    )
    tokens = set_runtime_context("session-cleanup", "high_risk")
    try:
        started = start_process(command="python3", args_json=f'["{script_name}"]')
        process_id = started.split("process=")[1].split(",")[0]
        managed_payload = next(
            process
            for process in process_runtime_manager.list_processes()
            if process["process_id"] == process_id
        )
    finally:
        reset_runtime_context(tokens)

    output_path = Path(managed_payload["output_path"])
    worker_root = Path(managed_payload["worker_root"])
    assert process_runtime_manager.stop_processes_for_session("session-cleanup") == 1
    assert process_runtime_manager.last_session_cleanup_receipt == {
        "session_id": "session-cleanup",
        "requested": 1,
        "stopped": 1,
        "unknown": 0,
        "failed": 0,
        "conflict": 0,
    }
    assert not any(
        process["process_id"] == process_id
        for process in process_runtime_manager.list_all_processes()
    )
    assert not output_path.exists()
    assert not worker_root.exists()


def test_session_cleanup_fail_closed_retains_unknown_process(monkeypatch):
    script_name = _write_script(
        "wave_process_session_unknown.py",
        """
        import time
        time.sleep(30)
        """,
    )
    tokens = set_runtime_context("session-cleanup-unknown", "high_risk")
    try:
        started = start_process(command="python3", args_json=f'["{script_name}"]')
        process_id = started.split("process=")[1].split(",")[0]
    finally:
        reset_runtime_context(tokens)

    current_identity = process_tools_module._read_process_identity(os.getpid())
    assert current_identity is not None
    monkeypatch.setattr(
        process_tools_module,
        "_snapshot_process_descendants",
        lambda _process, _leader: (
            _ProcessDescendantIdentity(
                pid=current_identity.pid,
                process_group_id=current_identity.process_group_id,
                start_time=current_identity.start_time,
            ),
        ),
    )

    with pytest.raises(SessionProcessCleanupError) as exc_info:
        process_runtime_manager.stop_processes_for_session(
            "session-cleanup-unknown",
            fail_closed=True,
        )

    assert exc_info.value.receipt["unknown"] == 1
    assert any(
        process["process_id"] == process_id
        for process in process_runtime_manager.list_all_processes()
    )


def test_stop_process_reconciles_exited_leader_without_signal(monkeypatch):
    script_name = _write_script(
        "wave_process_exited_leader_reconcile.py",
        """
        import time
        time.sleep(0.05)
        """,
    )
    started = start_process(command="python3", args_json=f'["{script_name}"]')
    process_id = started.split("process=")[1].split(",")[0]
    managed = process_runtime_manager._processes[process_id]
    managed.popen.wait(timeout=2)

    monkeypatch.setattr(
        process_tools_module,
        "_snapshot_process_descendants",
        lambda _process, _leader: (),
    )

    stopped = process_runtime_manager.stop_process(process_id=process_id, force=True)

    assert stopped is not None
    assert stopped["cleanup_status"] == "stopped"
    assert stopped["stopped"] is True
    assert stopped["registry_removed"] is True
    assert stopped["artifacts_removed"] is True


def test_reset_for_tests_clears_session_cleanup_state():
    assert process_runtime_manager.stop_processes_for_session("reset-state") == 0
    assert process_runtime_manager.last_session_cleanup_receipt is not None
    assert process_runtime_manager.begin_session_cleanup("reset-state") is True

    process_runtime_manager.reset_for_tests()

    assert process_runtime_manager.last_session_cleanup_receipt is None
    assert "reset-state" not in process_runtime_manager._stopping_sessions


def test_start_process_scrubs_ambient_secret_environment(monkeypatch):
    monkeypatch.setenv("SERAPH_BACKGROUND_SHOULD_NOT_LEAK", "background-secret")
    script_name = _write_script(
        "wave3_process_background_env_scrub.py",
        """
        import os
        print(os.environ.get("SERAPH_BACKGROUND_SHOULD_NOT_LEAK", "missing"), flush=True)
        print(os.environ.get("SERAPH_SANDBOX_ENV", "missing"), flush=True)
        """,
    )

    started = start_process(command="python3", args_json=f'["{script_name}"]')
    process_id = started.split("process=")[1].split(",")[0]
    output = ""
    for _ in range(20):
        output = read_process_output(process_id=process_id)
        if "allowlisted" in output:
            break
        time.sleep(0.05)

    assert "background-secret" not in output
    assert "missing" in output
    assert "allowlisted" in output
    stop_process(process_id=process_id)


def test_stop_process_missing_returns_error():
    assert stop_process(process_id="missing-process") == "Error: Process 'missing-process' was not found."


def test_process_recovery_is_scoped_to_the_starting_session():
    script_name = _write_script(
        "wave2_process_scoped.py",
        """
        import time
        print("scoped", flush=True)
        time.sleep(30)
        """,
    )

    owner_tokens = set_runtime_context("owner-session", "high_risk")
    try:
        started = start_process(command="python3", args_json=f'["{script_name}"]')
        process_id = started.split("process=")[1].split(",")[0]
    finally:
        reset_runtime_context(owner_tokens)

    other_tokens = set_runtime_context("other-session", "high_risk")
    try:
        assert process_id not in list_processes()
        assert read_process_output(process_id=process_id) == f"Error: Process '{process_id}' was not found."
        assert stop_process(process_id=process_id) == f"Error: Process '{process_id}' was not found."
    finally:
        reset_runtime_context(other_tokens)

    owner_tokens = set_runtime_context("owner-session", "high_risk")
    try:
        for _ in range(20):
            listed = list_processes()
            if process_id in listed:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("process did not appear in owner session list_processes output")

        output = ""
        for _ in range(20):
            output = read_process_output(process_id=process_id)
            if "scoped" in output:
                break
            time.sleep(0.05)
        assert "scoped" in output

        stopped = stop_process(process_id=process_id)
        assert f"Stopped process '{process_id}'" in stopped
    finally:
        reset_runtime_context(owner_tokens)


def test_list_all_processes_ignores_runtime_session_visibility_for_operator_surfaces():
    script_name = _write_script(
        "wave3_process_operator_visibility.py",
        """
        import time
        print("operator-visible", flush=True)
        time.sleep(30)
        """,
    )

    owner_tokens = set_runtime_context("owner-session", "high_risk")
    try:
        started = start_process(command="python3", args_json=f'["{script_name}"]')
        process_id = started.split("process=")[1].split(",")[0]
    finally:
        reset_runtime_context(owner_tokens)

    other_tokens = set_runtime_context("other-session", "high_risk")
    try:
        assert process_id not in list_processes()
        payload = next(
            process
            for process in process_runtime_manager.list_all_processes()
            if process["process_id"] == process_id
        )
        assert payload["session_id"] == "owner-session"
        assert payload["session_scoped"] is True
        assert payload["status"] == "running"
    finally:
        reset_runtime_context(other_tokens)


def test_process_output_logs_live_outside_the_workspace():
    script_name = _write_script(
        "wave2_process_log_location.py",
        """
        import time
        print("outside-workspace", flush=True)
        time.sleep(30)
        """,
    )

    owner_tokens = set_runtime_context("owner-session", "high_risk")
    try:
        started = start_process(command="python3", args_json=f'["{script_name}"]')
        process_id = started.split("process=")[1].split(",")[0]
        payload = next(
            process
            for process in process_runtime_manager.list_processes()
            if process["process_id"] == process_id
        )
    finally:
        reset_runtime_context(owner_tokens)

    assert not str(payload["output_path"]).startswith(str(Path(settings.workspace_dir).resolve()))
    assert stat.S_IMODE(os.stat(payload["output_path"]).st_mode) == 0o600
    assert payload["worker_disposable"] is True
    assert payload["trust_partition"] == "session_disposable_worker"
    assert not str(payload["worker_root"]).startswith(str(Path(settings.workspace_dir).resolve()))
