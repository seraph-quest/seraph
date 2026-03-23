"""Container-scoped shell and process runtime tools."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import shlex
import subprocess
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smolagents import Tool

from config.settings import settings

logger = logging.getLogger(__name__)

_run_command_audit_payload: contextvars.ContextVar[tuple[str, dict[str, Any]] | None] = contextvars.ContextVar(
    "run_command_audit_payload",
    default=None,
)
_start_process_audit_payload: contextvars.ContextVar[tuple[str, dict[str, Any]] | None] = contextvars.ContextVar(
    "start_process_audit_payload",
    default=None,
)
_list_processes_audit_payload: contextvars.ContextVar[tuple[str, dict[str, Any]] | None] = contextvars.ContextVar(
    "list_processes_audit_payload",
    default=None,
)
_read_process_output_audit_payload: contextvars.ContextVar[tuple[str, dict[str, Any]] | None] = contextvars.ContextVar(
    "read_process_output_audit_payload",
    default=None,
)
_stop_process_audit_payload: contextvars.ContextVar[tuple[str, dict[str, Any]] | None] = contextvars.ContextVar(
    "stop_process_audit_payload",
    default=None,
)

_COMMAND_NAME_ALLOWLIST = {
    "pwd",
    "ls",
    "find",
    "cat",
    "head",
    "tail",
    "wc",
    "grep",
    "rg",
    "sed",
    "git",
    "python",
    "python3",
    "uv",
    "pytest",
    "npm",
    "node",
}
_COMMAND_NAME_BLOCKLIST = {
    "bash",
    "sh",
    "zsh",
    "fish",
    "ksh",
    "sudo",
    "su",
    "ssh",
    "scp",
    "sftp",
    "curl",
    "wget",
    "nc",
    "ncat",
    "netcat",
    "telnet",
    "rm",
    "rmdir",
    "mkfs",
    "dd",
    "reboot",
    "shutdown",
    "launchctl",
    "systemctl",
    "service",
    "killall",
    "pkill",
    "osascript",
    "open",
}
_EXECUTABLE_META_CHARS = set("|&;<>()`$\n\r\t ")
_OUTPUT_CHAR_LIMIT = 12_000
_PROCESS_OUTPUT_DEFAULT = 4_000
_PROCESS_OUTPUT_MAX = 24_000
_COMMAND_TIMEOUT_MAX = 120


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _workspace_root() -> Path:
    return Path(settings.workspace_dir).resolve()


def _process_runtime_root() -> Path:
    path = _workspace_root() / ".seraph_runtime" / "processes"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _normalize_cwd(raw_cwd: str | None) -> Path:
    candidate = (raw_cwd or "").strip()
    if not candidate:
        return _workspace_root()

    raw_path = Path(candidate)
    resolved = (raw_path if raw_path.is_absolute() else (_workspace_root() / raw_path)).resolve()
    try:
        resolved.relative_to(_workspace_root())
    except ValueError as exc:
        raise ValueError("cwd must stay within the workspace.") from exc
    if not resolved.exists() or not resolved.is_dir():
        raise ValueError("cwd must point to an existing workspace directory.")
    return resolved


def _ensure_workspace_scoped_path(raw_path: str, cwd: Path, *, label: str) -> None:
    candidate = (raw_path or "").strip()
    if not candidate:
        raise ValueError(f"{label} is required.")
    resolved = (Path(candidate) if Path(candidate).is_absolute() else (cwd / candidate)).resolve()
    try:
        resolved.relative_to(_workspace_root())
    except ValueError as exc:
        raise ValueError(f"{label} must stay within the workspace.") from exc


def _normalize_command(command: str) -> str:
    normalized = (command or "").strip()
    if not normalized:
        raise ValueError("command is required.")
    if any(char in _EXECUTABLE_META_CHARS for char in normalized):
        raise ValueError("command must be a single executable token without shell metacharacters.")

    if "/" in normalized:
        resolved = (_workspace_root() / normalized).resolve() if not Path(normalized).is_absolute() else Path(normalized).resolve()
        try:
            resolved.relative_to(_workspace_root())
        except ValueError as exc:
            raise ValueError("command paths must stay within the workspace.") from exc
        if not resolved.exists() or resolved.is_dir():
            raise ValueError("command path must point to an existing executable file.")
        return str(resolved)

    lowered = normalized.lower()
    if lowered in _COMMAND_NAME_BLOCKLIST:
        raise ValueError(f"command '{normalized}' is blocked in the process runtime.")
    if lowered not in _COMMAND_NAME_ALLOWLIST:
        raise ValueError(f"command '{normalized}' is not allowed in the process runtime.")
    return normalized


def _parse_args_json(raw_args_json: str | None) -> list[str]:
    normalized = (raw_args_json or "").strip()
    if not normalized:
        return []
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise ValueError("args_json must be valid JSON.") from exc
    if not isinstance(payload, list):
        raise ValueError("args_json must decode to an array.")
    args: list[str] = []
    for item in payload:
        if isinstance(item, (dict, list)):
            raise ValueError("args_json entries must be scalar values.")
        arg = str(item)
        if "\n" in arg or "\r" in arg:
            raise ValueError("args_json entries cannot contain newlines.")
        args.append(arg)
    return args


def _validate_interpreter_args(executable: str, args: list[str]) -> None:
    command_name = Path(executable).name
    if command_name in {"python", "python3"} and args[:1] and args[0] in {"-c", "-m"}:
        raise ValueError("Inline Python execution belongs in execute_code, not the process runtime.")
    if command_name == "node" and args[:1] and args[0] == "-e":
        raise ValueError("Inline Node execution is not allowed in the process runtime.")
    if command_name == "uv" and len(args) >= 2 and args[0] == "run" and args[1] == "-m":
        raise ValueError("uv run -m is not allowed in the process runtime.")


def _validate_workspace_scoped_args(executable: str, args: list[str], cwd: Path) -> None:
    command_name = Path(executable).name

    if command_name == "git":
        index = 0
        while index < len(args):
            arg = args[index]
            if arg in {"-C", "--git-dir", "--work-tree"}:
                if index + 1 >= len(args):
                    raise ValueError(f"{arg} requires a path argument.")
                _ensure_workspace_scoped_path(args[index + 1], cwd, label=f"{arg} path")
                index += 2
                continue
            if arg.startswith("--git-dir="):
                _ensure_workspace_scoped_path(arg.split("=", 1)[1], cwd, label="--git-dir path")
            elif arg.startswith("--work-tree="):
                _ensure_workspace_scoped_path(arg.split("=", 1)[1], cwd, label="--work-tree path")
            index += 1
        return

    if command_name in {"python", "python3", "node"}:
        for arg in args:
            if arg == "--":
                break
            if arg.startswith("-"):
                continue
            _ensure_workspace_scoped_path(arg, cwd, label="script path")
            break
        return

    if command_name == "find":
        for arg in args:
            if arg.startswith("-") or arg in {"!", "(", ")"}:
                break
            _ensure_workspace_scoped_path(arg, cwd, label="search path")
        return

    if command_name in {"cat", "ls"}:
        for arg in args:
            if arg.startswith("-"):
                continue
            _ensure_workspace_scoped_path(arg, cwd, label="path argument")
        return

    if command_name in {"head", "tail"}:
        index = 0
        while index < len(args):
            arg = args[index]
            if arg in {"-n", "--lines", "-c", "--bytes"}:
                index += 2
                continue
            if arg.startswith("-"):
                index += 1
                continue
            _ensure_workspace_scoped_path(arg, cwd, label="path argument")
            index += 1
        return

    if command_name == "wc":
        index = 0
        while index < len(args):
            arg = args[index]
            if arg in {"-L", "--max-line-length"}:
                index += 1
                continue
            if arg.startswith("-"):
                index += 1
                continue
            _ensure_workspace_scoped_path(arg, cwd, label="path argument")
            index += 1
        return

    if command_name in {"grep", "rg", "sed"}:
        pattern_consumed = False
        index = 0
        while index < len(args):
            arg = args[index]
            if arg in {"-e", "--regexp"}:
                if index + 1 >= len(args):
                    raise ValueError(f"{arg} requires a value.")
                pattern_consumed = True
                index += 2
                continue
            if arg in {"-f", "--file"}:
                if index + 1 >= len(args):
                    raise ValueError(f"{arg} requires a path argument.")
                _ensure_workspace_scoped_path(args[index + 1], cwd, label=f"{arg} path")
                pattern_consumed = True
                index += 2
                continue
            if arg.startswith("-f") and arg != "-f" and not arg.startswith("--"):
                _ensure_workspace_scoped_path(arg[2:], cwd, label="-f path")
                pattern_consumed = True
                index += 1
                continue
            if arg.startswith("-") and not arg.startswith("--"):
                short_flag_operand_index = arg.find("f", 1)
                if 1 <= short_flag_operand_index < len(arg) - 1:
                    _ensure_workspace_scoped_path(arg[short_flag_operand_index + 1 :], cwd, label="-f path")
                    pattern_consumed = True
                    index += 1
                    continue
            if arg.startswith("--file="):
                _ensure_workspace_scoped_path(arg.split("=", 1)[1], cwd, label="--file path")
                pattern_consumed = True
                index += 1
                continue
            if command_name == "grep" and arg in {"--exclude-from"}:
                if index + 1 >= len(args):
                    raise ValueError(f"{arg} requires a path argument.")
                _ensure_workspace_scoped_path(args[index + 1], cwd, label=f"{arg} path")
                index += 2
                continue
            if command_name == "grep" and arg.startswith("--exclude-from="):
                _ensure_workspace_scoped_path(arg.split("=", 1)[1], cwd, label="--exclude-from path")
                index += 1
                continue
            if command_name == "rg" and arg in {"--ignore-file"}:
                if index + 1 >= len(args):
                    raise ValueError(f"{arg} requires a path argument.")
                _ensure_workspace_scoped_path(args[index + 1], cwd, label=f"{arg} path")
                index += 2
                continue
            if command_name == "rg" and arg.startswith("--ignore-file="):
                _ensure_workspace_scoped_path(arg.split("=", 1)[1], cwd, label="--ignore-file path")
                index += 1
                continue
            if arg.startswith("-"):
                index += 1
                continue
            if not pattern_consumed and command_name in {"grep", "rg", "sed"}:
                pattern_consumed = True
            else:
                _ensure_workspace_scoped_path(arg, cwd, label="path argument")
            index += 1


def _normalize_timeout_seconds(raw_timeout: int | None) -> int:
    timeout = 30 if raw_timeout is None else int(raw_timeout)
    return max(1, min(timeout, _COMMAND_TIMEOUT_MAX))


def _truncate_output(text: str, *, limit: int = _OUTPUT_CHAR_LIMIT) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n...[truncated]...", True


def _tail_text(path: Path, *, max_chars: int) -> tuple[str, bool]:
    if not path.exists():
        return "", False
    data = path.read_text(encoding="utf-8", errors="replace")
    if len(data) <= max_chars:
        return data, False
    return "...[truncated]...\n" + data[-max_chars:], True


def _display_command(argv: list[str]) -> str:
    return shlex.join(argv)


def _sanitized_process_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    args = _parse_args_json(str(arguments.get("args_json", "") or ""))
    payload = {
        "command": str(arguments.get("command", "") or "").strip() or None,
        "arg_count": len(args),
        "cwd": str(arguments.get("cwd", "") or "").strip() or ".",
        "timeout_seconds": arguments.get("timeout_seconds"),
        "process_id": str(arguments.get("process_id", "") or "").strip() or None,
        "max_chars": arguments.get("max_chars"),
        "force": bool(arguments.get("force", False)),
    }
    return {key: value for key, value in payload.items() if value not in {None, ""}}


def _command_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env["HOME"] = str(_workspace_root())
    return env


def _normalize_command_invocation(
    *,
    command: str,
    args_json: str = "",
    cwd: str = "",
) -> tuple[str, list[str], Path]:
    executable = _normalize_command(command)
    args = _parse_args_json(args_json)
    resolved_cwd = _normalize_cwd(cwd)
    _validate_interpreter_args(executable, args)
    _validate_workspace_scoped_args(executable, args, resolved_cwd)
    return executable, args, resolved_cwd


@dataclass
class ManagedProcess:
    process_id: str
    popen: subprocess.Popen[str]
    command: str
    args: list[str]
    cwd: str
    output_path: Path
    started_at: datetime

    def status_payload(self) -> dict[str, Any]:
        exit_code = self.popen.poll()
        return {
            "process_id": self.process_id,
            "pid": self.popen.pid,
            "command": self.command,
            "args": list(self.args),
            "cwd": self.cwd,
            "status": "running" if exit_code is None else "exited",
            "exit_code": exit_code,
            "started_at": self.started_at.isoformat(),
            "output_path": str(self.output_path),
        }


class ProcessRuntimeManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[str, ManagedProcess] = {}

    def run_command(
        self,
        *,
        command: str,
        args_json: str = "",
        cwd: str = "",
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        executable, args, resolved_cwd = _normalize_command_invocation(
            command=command,
            args_json=args_json,
            cwd=cwd,
        )
        timeout = _normalize_timeout_seconds(timeout_seconds)
        try:
            result = subprocess.run(
                [executable, *args],
                cwd=str(resolved_cwd),
                capture_output=True,
                text=True,
                shell=False,
                env=_command_env(),
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "timed_out": True,
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "display_command": _display_command([executable, *args]),
                "cwd": str(resolved_cwd),
                "timeout_seconds": timeout,
            }

        return {
            "ok": result.returncode == 0,
            "timed_out": False,
            "exit_code": result.returncode,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "display_command": _display_command([executable, *args]),
            "cwd": str(resolved_cwd),
            "timeout_seconds": timeout,
        }

    def start_process(
        self,
        *,
        command: str,
        args_json: str = "",
        cwd: str = "",
    ) -> dict[str, Any]:
        executable, args, resolved_cwd = _normalize_command_invocation(
            command=command,
            args_json=args_json,
            cwd=cwd,
        )
        process_id = uuid.uuid4().hex
        output_path = _process_runtime_root() / f"{process_id}.log"
        with output_path.open("w", encoding="utf-8", errors="replace") as output_stream:
            popen = subprocess.Popen(
                [executable, *args],
                cwd=str(resolved_cwd),
                stdout=output_stream,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                shell=False,
                env=_command_env(),
                start_new_session=True,
            )

        managed = ManagedProcess(
            process_id=process_id,
            popen=popen,
            command=executable,
            args=args,
            cwd=str(resolved_cwd),
            output_path=output_path,
            started_at=_utc_now(),
        )
        with self._lock:
            self._processes[process_id] = managed
        return managed.status_payload()

    def list_processes(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                process.status_payload()
                for process in sorted(
                    self._processes.values(),
                    key=lambda item: item.started_at,
                    reverse=True,
                )
            ]

    def read_process_output(self, process_id: str, *, max_chars: int = _PROCESS_OUTPUT_DEFAULT) -> dict[str, Any] | None:
        with self._lock:
            process = self._processes.get(process_id)
        if process is None:
            return None
        bounded = max(1, min(max_chars, _PROCESS_OUTPUT_MAX))
        output, truncated = _tail_text(process.output_path, max_chars=bounded)
        payload = process.status_payload()
        payload.update(
            {
                "output": output,
                "truncated": truncated,
                "output_chars": len(output),
            }
        )
        return payload

    def stop_process(self, process_id: str, *, force: bool = False) -> dict[str, Any] | None:
        with self._lock:
            process = self._processes.get(process_id)
        if process is None:
            return None

        if process.popen.poll() is None:
            if force:
                process.popen.kill()
            else:
                process.popen.terminate()
                try:
                    process.popen.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.popen.kill()
                    process.popen.wait(timeout=5)
        payload = process.status_payload()
        payload["stopped"] = True
        return payload

    def reset_for_tests(self) -> None:
        with self._lock:
            process_ids = list(self._processes.keys())
        for process_id in process_ids:
            try:
                self.stop_process(process_id, force=True)
            except Exception:
                logger.debug("Failed to stop test process %s", process_id, exc_info=True)
        with self._lock:
            self._processes.clear()


process_runtime_manager = ProcessRuntimeManager()


class RunCommandTool(Tool):
    skip_forward_signature_validation = True

    def __init__(self) -> None:
        super().__init__()
        self.name = "run_command"
        self.description = (
            "Run an approved workspace-scoped command inside the Seraph runtime container and return its output."
        )
        self.inputs = {
            "command": {"type": "string", "description": "Executable name or workspace-relative script path."},
            "args_json": {"type": "string", "description": "JSON array of command arguments.", "nullable": True},
            "cwd": {"type": "string", "description": "Workspace-relative working directory.", "nullable": True},
            "timeout_seconds": {"type": "integer", "description": "Execution timeout in seconds.", "nullable": True},
        }
        self.output_type = "string"
        self.is_initialized = True

    def forward(self, command: str, args_json: str = "", cwd: str = "", timeout_seconds: int = 30) -> str:
        return self.__call__(command=command, args_json=args_json, cwd=cwd, timeout_seconds=timeout_seconds)

    def __call__(self, *args, sanitize_inputs_outputs: bool = False, **kwargs):
        arguments = self._normalize_invocation(args, kwargs)
        try:
            result = process_runtime_manager.run_command(**arguments)
        except ValueError as exc:
            _run_command_audit_payload.set(None)
            return f"Error: {exc}"

        output = result["stdout"]
        if result["stderr"]:
            output = output + ("\n" if output and not output.endswith("\n") else "") + "--- stderr ---\n" + result["stderr"]
        rendered_output, truncated = _truncate_output(output)

        _run_command_audit_payload.set((
            f"run_command finished with exit_code={result['exit_code'] if result['exit_code'] is not None else 'timeout'}",
            {
                "command": result["display_command"],
                "cwd": result["cwd"],
                "exit_code": result["exit_code"],
                "timed_out": result["timed_out"],
                "stdout_chars": len(result["stdout"]),
                "stderr_chars": len(result["stderr"]),
                "output_truncated": truncated,
            },
        ))

        if result["timed_out"]:
            return f"Error: command timed out after {result['timeout_seconds']}s."
        if result["exit_code"] == 0:
            return rendered_output if rendered_output else "(no output)"
        return (
            f"Exit code {result['exit_code']}:\n{rendered_output}"
            if rendered_output
            else f"Execution failed with exit code {result['exit_code']}."
        )

    def get_audit_result_payload(self, _arguments: dict[str, Any], _result: Any) -> tuple[str, dict[str, Any]] | None:
        payload = _run_command_audit_payload.get()
        _run_command_audit_payload.set(None)
        return payload

    def get_audit_call_payload(self, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        sanitized = self.get_audit_arguments(arguments)
        return (
            f"Calling tool: run_command(command={sanitized.get('command', 'unknown')}, argc={sanitized.get('arg_count', 0)})",
            {"arguments": sanitized},
        )

    def get_audit_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return _sanitized_process_arguments(arguments)

    def _normalize_invocation(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        if len(args) == 1 and not kwargs and isinstance(args[0], dict):
            payload = dict(args[0])
        else:
            payload = dict(kwargs)
        return {
            "command": str(payload.get("command", "") or "").strip(),
            "args_json": str(payload.get("args_json", "") or ""),
            "cwd": str(payload.get("cwd", "") or ""),
            "timeout_seconds": payload.get("timeout_seconds", 30),
        }


class StartProcessTool(Tool):
    skip_forward_signature_validation = True

    def __init__(self) -> None:
        super().__init__()
        self.name = "start_process"
        self.description = "Start an approved workspace-scoped background process inside the Seraph runtime container."
        self.inputs = {
            "command": {"type": "string", "description": "Executable name or workspace-relative script path."},
            "args_json": {"type": "string", "description": "JSON array of command arguments.", "nullable": True},
            "cwd": {"type": "string", "description": "Workspace-relative working directory.", "nullable": True},
        }
        self.output_type = "string"
        self.is_initialized = True

    def forward(self, command: str, args_json: str = "", cwd: str = "") -> str:
        return self.__call__(command=command, args_json=args_json, cwd=cwd)

    def __call__(self, *args, sanitize_inputs_outputs: bool = False, **kwargs):
        arguments = self._normalize_invocation(args, kwargs)
        try:
            payload = process_runtime_manager.start_process(**arguments)
        except ValueError as exc:
            _start_process_audit_payload.set(None)
            return f"Error: {exc}"

        _start_process_audit_payload.set((
            f"start_process launched {Path(payload['command']).name} as {payload['process_id']}",
            {
                "process_id": payload["process_id"],
                "pid": payload["pid"],
                "command": payload["command"],
                "cwd": payload["cwd"],
            },
        ))
        return (
            f"Started process '{Path(payload['command']).name}' "
            f"(process={payload['process_id']}, pid={payload['pid']})."
        )

    def get_audit_result_payload(self, _arguments: dict[str, Any], _result: Any) -> tuple[str, dict[str, Any]] | None:
        payload = _start_process_audit_payload.get()
        _start_process_audit_payload.set(None)
        return payload

    def get_audit_call_payload(self, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        sanitized = self.get_audit_arguments(arguments)
        return (
            f"Calling tool: start_process(command={sanitized.get('command', 'unknown')}, argc={sanitized.get('arg_count', 0)})",
            {"arguments": sanitized},
        )

    def get_audit_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return _sanitized_process_arguments(arguments)

    def _normalize_invocation(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        if len(args) == 1 and not kwargs and isinstance(args[0], dict):
            payload = dict(args[0])
        else:
            payload = dict(kwargs)
        return {
            "command": str(payload.get("command", "") or "").strip(),
            "args_json": str(payload.get("args_json", "") or ""),
            "cwd": str(payload.get("cwd", "") or ""),
        }


class ListProcessesTool(Tool):
    skip_forward_signature_validation = True

    def __init__(self) -> None:
        super().__init__()
        self.name = "list_processes"
        self.description = "List background processes started through the Seraph runtime process manager."
        self.inputs = {}
        self.output_type = "string"
        self.is_initialized = True

    def forward(self) -> str:
        return self.__call__()

    def __call__(self, *args, sanitize_inputs_outputs: bool = False, **kwargs):
        processes = process_runtime_manager.list_processes()
        _list_processes_audit_payload.set((
            f"list_processes returned {len(processes)} processes",
            {"process_count": len(processes)},
        ))
        if not processes:
            return "No managed processes."
        lines: list[str] = []
        for index, process in enumerate(processes, start=1):
            line = (
                f"{index}. {Path(process['command']).name} "
                f"(process={process['process_id']}, pid={process['pid']}, status={process['status']}"
            )
            if process["exit_code"] is not None:
                line += f", exit_code={process['exit_code']}"
            line += f", cwd={process['cwd']})"
            lines.append(line)
        return "\n".join(lines)

    def get_audit_result_payload(self, _arguments: dict[str, Any], _result: Any) -> tuple[str, dict[str, Any]] | None:
        payload = _list_processes_audit_payload.get()
        _list_processes_audit_payload.set(None)
        return payload


class ReadProcessOutputTool(Tool):
    skip_forward_signature_validation = True

    def __init__(self) -> None:
        super().__init__()
        self.name = "read_process_output"
        self.description = "Read the recent combined stdout/stderr output for a managed background process."
        self.inputs = {
            "process_id": {"type": "string", "description": "Managed process id returned by start_process."},
            "max_chars": {"type": "integer", "description": "Maximum number of characters to read.", "nullable": True},
        }
        self.output_type = "string"
        self.is_initialized = True

    def forward(self, process_id: str, max_chars: int = _PROCESS_OUTPUT_DEFAULT) -> str:
        return self.__call__(process_id=process_id, max_chars=max_chars)

    def __call__(self, *args, sanitize_inputs_outputs: bool = False, **kwargs):
        arguments = self._normalize_invocation(args, kwargs)
        payload = process_runtime_manager.read_process_output(**arguments)
        if payload is None:
            _read_process_output_audit_payload.set(None)
            return f"Error: Process '{arguments['process_id']}' was not found."

        _read_process_output_audit_payload.set((
            f"read_process_output returned {payload['output_chars']} chars for {payload['process_id']}",
            {
                "process_id": payload["process_id"],
                "status": payload["status"],
                "exit_code": payload["exit_code"],
                "output_chars": payload["output_chars"],
                "truncated": payload["truncated"],
            },
        ))

        if not payload["output"]:
            return f"Process '{payload['process_id']}' has no output yet."
        return payload["output"]

    def get_audit_result_payload(self, _arguments: dict[str, Any], _result: Any) -> tuple[str, dict[str, Any]] | None:
        payload = _read_process_output_audit_payload.get()
        _read_process_output_audit_payload.set(None)
        return payload

    def get_audit_call_payload(self, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        sanitized = self.get_audit_arguments(arguments)
        return (
            f"Calling tool: read_process_output(process={sanitized.get('process_id', 'unknown')})",
            {"arguments": sanitized},
        )

    def get_audit_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return _sanitized_process_arguments(arguments)

    def _normalize_invocation(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        if len(args) == 1 and not kwargs and isinstance(args[0], dict):
            payload = dict(args[0])
        else:
            payload = dict(kwargs)
        return {
            "process_id": str(payload.get("process_id", "") or "").strip(),
            "max_chars": int(payload.get("max_chars", _PROCESS_OUTPUT_DEFAULT) or _PROCESS_OUTPUT_DEFAULT),
        }


class StopProcessTool(Tool):
    skip_forward_signature_validation = True

    def __init__(self) -> None:
        super().__init__()
        self.name = "stop_process"
        self.description = "Stop a managed background process inside the Seraph runtime container."
        self.inputs = {
            "process_id": {"type": "string", "description": "Managed process id returned by start_process."},
            "force": {"type": "boolean", "description": "Kill the process immediately instead of terminating it.", "nullable": True},
        }
        self.output_type = "string"
        self.is_initialized = True

    def forward(self, process_id: str, force: bool = False) -> str:
        return self.__call__(process_id=process_id, force=force)

    def __call__(self, *args, sanitize_inputs_outputs: bool = False, **kwargs):
        arguments = self._normalize_invocation(args, kwargs)
        payload = process_runtime_manager.stop_process(**arguments)
        if payload is None:
            _stop_process_audit_payload.set(None)
            return f"Error: Process '{arguments['process_id']}' was not found."

        _stop_process_audit_payload.set((
            f"stop_process stopped {payload['process_id']}",
            {
                "process_id": payload["process_id"],
                "pid": payload["pid"],
                "exit_code": payload["exit_code"],
                "forced": bool(arguments.get("force", False)),
            },
        ))
        return f"Stopped process '{payload['process_id']}' with exit_code={payload['exit_code']}."

    def get_audit_result_payload(self, _arguments: dict[str, Any], _result: Any) -> tuple[str, dict[str, Any]] | None:
        payload = _stop_process_audit_payload.get()
        _stop_process_audit_payload.set(None)
        return payload

    def get_audit_call_payload(self, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        sanitized = self.get_audit_arguments(arguments)
        return (
            f"Calling tool: stop_process(process={sanitized.get('process_id', 'unknown')})",
            {"arguments": sanitized},
        )

    def get_audit_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return _sanitized_process_arguments(arguments)

    def _normalize_invocation(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        if len(args) == 1 and not kwargs and isinstance(args[0], dict):
            payload = dict(args[0])
        else:
            payload = dict(kwargs)
        return {
            "process_id": str(payload.get("process_id", "") or "").strip(),
            "force": bool(payload.get("force", False)),
        }


run_command = RunCommandTool()
start_process = StartProcessTool()
list_processes = ListProcessesTool()
read_process_output = ReadProcessOutputTool()
stop_process = StopProcessTool()
