from __future__ import annotations

import asyncio
import textwrap
import time
from pathlib import Path

import pytest

from config.settings import settings
from src.agent.session import session_manager
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.audit.repository import audit_repository
from src.tools.audit import wrap_tools_for_audit
from src.tools.process_tools import (
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


def test_run_command_rejects_inline_python():
    result = run_command(command="python3", args_json='["-c","print(1)"]')
    assert result == "Error: Inline Python execution belongs in execute_code, not the process runtime."


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

    output = ""
    for _ in range(20):
        output = read_process_output(process_id=process_id)
        if "started" in output:
            break
        time.sleep(0.05)
    assert "started" in output

    stopped = stop_process(process_id=process_id)
    assert f"Stopped process '{process_id}'" in stopped


def test_stop_process_missing_returns_error():
    assert stop_process(process_id="missing-process") == "Error: Process 'missing-process' was not found."
