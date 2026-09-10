"""Container-scoped shell and process runtime tools."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from smolagents import Tool

from config.settings import settings
from src.approval.runtime import get_current_session_id
from src.tools.policy import get_tool_execution_boundaries, get_tool_risk_level

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
_PROCESS_STOP_WAIT_SECONDS = 1.0
_PROCESS_IDENTITY_RETRY_ATTEMPTS = 5
_PROCESS_IDENTITY_RETRY_DELAY_SECONDS = 0.01
_SECRET_FILE_NAMES = {
    ".env",
    ".envrc",
    ".dockerconfigjson",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "google_credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "known_hosts",
    "private_key",
    "secrets.json",
}
_SECRET_FILE_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
)
_SECRET_PATH_PARTS = {
    ".aws",
    ".azure",
    ".config/gcloud",
    ".docker",
    ".gnupg",
    ".ssh",
}
_DANGEROUS_FIND_ACTIONS = {
    "-exec",
    "-execdir",
    "-ok",
    "-okdir",
}
_NETWORK_SCRIPT_MARKERS = (
    "import socket",
    "from socket import",
    "import http.client",
    "from http.client import",
    "import urllib.request",
    "from urllib.request import",
    "import requests",
    "from requests import",
    "import httpx",
    "from httpx import",
    "fetch(",
    "net.connect",
    "http.request",
    "https.request",
    "require('net')",
    'require("net")',
    "require('http')",
    'require("http")',
    "require('https')",
    'require("https")',
)
_ENV_ALLOWLIST = {
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TZ",
    "CI",
    "PYTHONPATH",
    "VIRTUAL_ENV",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _workspace_root() -> Path:
    return Path(settings.workspace_dir).resolve()


def _runtime_root() -> Path:
    workspace_tag = uuid.uuid5(uuid.NAMESPACE_URL, str(_workspace_root()))
    path = Path(tempfile.gettempdir()) / "seraph_runtime" / str(workspace_tag)
    path.mkdir(parents=True, exist_ok=True)
    for candidate in (path.parent, path):
        try:
            candidate.chmod(0o700)
        except OSError:
            logger.debug("Failed to tighten permissions on %s", candidate, exc_info=True)
    return path


def _prepare_runtime_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for candidate in (path.parent, path):
        try:
            candidate.chmod(0o700)
        except OSError:
            logger.debug("Failed to tighten permissions on %s", candidate, exc_info=True)
    return path


def _process_runtime_root() -> Path:
    return _prepare_runtime_dir(_runtime_root() / "processes")


def _worker_runtime_root(worker_id: str) -> Path:
    return _prepare_runtime_dir(_runtime_root() / "workers" / worker_id)


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


def _is_secret_like_workspace_path(path: Path) -> bool:
    workspace_relative = path.relative_to(_workspace_root())
    normalized_parts = tuple(part.lower() for part in workspace_relative.parts)
    normalized_posix = "/".join(normalized_parts)
    if any(
        normalized_posix == secret_part or normalized_posix.startswith(f"{secret_part}/")
        for secret_part in _SECRET_PATH_PARTS
    ):
        return True

    name = path.name.lower()
    if name in _SECRET_FILE_NAMES or name.startswith(".env."):
        return True
    if any(name.endswith(suffix) for suffix in _SECRET_FILE_SUFFIXES):
        return True
    return any(token in name for token in ("credential", "secret", "token"))


def _directory_contains_secret_like_workspace_path(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    for candidate in path.rglob("*"):
        try:
            resolved = candidate.resolve()
            resolved.relative_to(_workspace_root())
        except (OSError, ValueError):
            return True
        if _is_secret_like_workspace_path(resolved):
            return True
    return False


def _ensure_process_accessible_path(raw_path: str, cwd: Path, *, label: str) -> None:
    _ensure_workspace_scoped_path(raw_path, cwd, label=label)
    candidate = (raw_path or "").strip()
    resolved = (Path(candidate) if Path(candidate).is_absolute() else (cwd / candidate)).resolve()
    if _is_secret_like_workspace_path(resolved):
        raise ValueError(f"{label} cannot target secret-like workspace files.")


def _ensure_process_search_path(raw_path: str, cwd: Path, *, label: str) -> None:
    _ensure_process_accessible_path(raw_path, cwd, label=label)
    candidate = (raw_path or "").strip()
    resolved = (Path(candidate) if Path(candidate).is_absolute() else (cwd / candidate)).resolve()
    if _directory_contains_secret_like_workspace_path(resolved):
        raise ValueError(f"{label} cannot recursively search workspace paths containing secret-like files.")


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
    if lowered in {"python", "python3"}:
        return sys.executable
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


def _reject_network_script_markers(script_path: Path) -> None:
    try:
        body = script_path.read_text(encoding="utf-8", errors="ignore").lower()
    except OSError as exc:
        raise ValueError("script path must point to a readable workspace file.") from exc
    if any(marker in body for marker in _NETWORK_SCRIPT_MARKERS):
        raise ValueError("script network clients are blocked in the process runtime.")


def _validate_workspace_scoped_args(executable: str, args: list[str], cwd: Path) -> None:
    command_name = Path(executable).name

    if command_name == "git":
        index = 0
        while index < len(args):
            arg = args[index]
            if arg in {"-C", "--git-dir", "--work-tree"}:
                if index + 1 >= len(args):
                    raise ValueError(f"{arg} requires a path argument.")
                _ensure_process_accessible_path(args[index + 1], cwd, label=f"{arg} path")
                index += 2
                continue
            if arg.startswith("--git-dir="):
                _ensure_process_accessible_path(arg.split("=", 1)[1], cwd, label="--git-dir path")
            elif arg.startswith("--work-tree="):
                _ensure_process_accessible_path(arg.split("=", 1)[1], cwd, label="--work-tree path")
            index += 1
        return

    if command_name in {"python", "python3", "node"}:
        for arg in args:
            if arg == "--":
                break
            if arg.startswith("-"):
                continue
            _ensure_process_accessible_path(arg, cwd, label="script path")
            _reject_network_script_markers((Path(arg) if Path(arg).is_absolute() else (cwd / arg)).resolve())
            break
        return

    if command_name == "find":
        for arg in args:
            if arg in _DANGEROUS_FIND_ACTIONS:
                raise ValueError(f"find action {arg} is blocked in the process runtime.")
        search_path_checked = False
        for arg in args:
            if arg.startswith("-") or arg in {"!", "(", ")"}:
                break
            _ensure_process_search_path(arg, cwd, label="search path")
            search_path_checked = True
        if not search_path_checked:
            _ensure_process_search_path(".", cwd, label="search path")
        return

    if command_name in {"cat", "ls"}:
        for arg in args:
            if arg.startswith("-"):
                continue
            _ensure_process_accessible_path(arg, cwd, label="path argument")
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
            _ensure_process_accessible_path(arg, cwd, label="path argument")
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
            _ensure_process_accessible_path(arg, cwd, label="path argument")
            index += 1
        return

    if command_name in {"grep", "rg", "sed"}:
        pattern_consumed = False
        path_argument_checked = False
        recursive_search = command_name == "rg" or any(
            arg in {"-r", "-R", "--recursive", "--dereference-recursive"}
            or (arg.startswith("-") and not arg.startswith("--") and any(flag in arg[1:] for flag in ("r", "R")))
            for arg in args
        )
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
                _ensure_process_accessible_path(args[index + 1], cwd, label=f"{arg} path")
                pattern_consumed = True
                index += 2
                continue
            if arg.startswith("-f") and arg != "-f" and not arg.startswith("--"):
                _ensure_process_accessible_path(arg[2:], cwd, label="-f path")
                pattern_consumed = True
                index += 1
                continue
            if arg.startswith("-") and not arg.startswith("--"):
                short_flag_operand_index = arg.find("f", 1)
                if 1 <= short_flag_operand_index < len(arg) - 1:
                    _ensure_process_accessible_path(arg[short_flag_operand_index + 1 :], cwd, label="-f path")
                    pattern_consumed = True
                    index += 1
                    continue
            if arg.startswith("--file="):
                _ensure_process_accessible_path(arg.split("=", 1)[1], cwd, label="--file path")
                pattern_consumed = True
                index += 1
                continue
            if command_name == "grep" and arg in {"--exclude-from"}:
                if index + 1 >= len(args):
                    raise ValueError(f"{arg} requires a path argument.")
                _ensure_process_accessible_path(args[index + 1], cwd, label=f"{arg} path")
                index += 2
                continue
            if command_name == "grep" and arg.startswith("--exclude-from="):
                _ensure_process_accessible_path(arg.split("=", 1)[1], cwd, label="--exclude-from path")
                index += 1
                continue
            if command_name == "rg" and arg in {"--ignore-file"}:
                if index + 1 >= len(args):
                    raise ValueError(f"{arg} requires a path argument.")
                _ensure_process_accessible_path(args[index + 1], cwd, label=f"{arg} path")
                index += 2
                continue
            if command_name == "rg" and arg.startswith("--ignore-file="):
                _ensure_process_accessible_path(arg.split("=", 1)[1], cwd, label="--ignore-file path")
                index += 1
                continue
            if arg.startswith("-"):
                index += 1
                continue
            if not pattern_consumed and command_name in {"grep", "rg", "sed"}:
                pattern_consumed = True
            else:
                if recursive_search:
                    _ensure_process_search_path(arg, cwd, label="path argument")
                else:
                    _ensure_process_accessible_path(arg, cwd, label="path argument")
                path_argument_checked = True
            index += 1
        if recursive_search and not path_argument_checked:
            _ensure_process_search_path(".", cwd, label="path argument")


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


def _process_approval_context(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    persistent_background_execution: bool,
) -> dict[str, Any]:
    worker_scope = "per_process" if persistent_background_execution else "per_invocation"
    trust_partition = "session_disposable_worker" if persistent_background_execution else "invocation_disposable_worker"
    context = {
        "risk_level": get_tool_risk_level(tool_name),
        "execution_boundaries": get_tool_execution_boundaries(tool_name),
        "accepts_secret_refs": False,
        "command_allowlist_enforced": True,
        "workspace_scoped_paths_only": True,
        "secret_like_path_arguments_blocked": True,
        "recursive_secret_like_search_paths_blocked": True,
        "dangerous_find_actions_blocked": True,
        "script_network_client_markers_blocked": True,
        "runtime_log_storage": "temp_runtime_outside_workspace",
        "runtime_worker_storage": "temp_runtime_outside_workspace",
        "disposable_worker_runtime": True,
        "worker_scope": worker_scope,
        "trust_partition": trust_partition,
        "persistent_background_execution": persistent_background_execution,
    }
    if tool_name in {"start_process", "list_processes", "read_process_output", "stop_process"}:
        context["session_process_partition"] = True
    if tool_name in {"start_process", "stop_process"}:
        context["confirmation_scope"] = "background_process_lifecycle"
    elif tool_name in {"list_processes", "read_process_output"}:
        context["confirmation_scope"] = "process_visibility"
    else:
        context["confirmation_scope"] = "bounded_command_execution"
    if tool_name in {"run_command", "start_process"}:
        context["command"] = _sanitized_process_arguments(arguments).get("command")
    return context


@dataclass(frozen=True)
class _ProcessLeaderIdentity:
    pid: int
    process_group_id: int
    start_time: int


@dataclass(frozen=True)
class _GroupTerminationResult:
    process_group_id: int | None
    ownership_verified: bool
    group_signal_sent: bool
    group_signal_failed: bool
    group_missing: bool
    parent_reaped: bool


def _read_process_identity(pid: int) -> _ProcessLeaderIdentity | None:
    """Read the Linux process start time and process group for a leader PID."""
    try:
        stat_text = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    closing_paren = stat_text.rfind(")")
    if closing_paren < 0:
        return None
    fields = stat_text[closing_paren + 2 :].split()
    # After comm, state is field 0, process-group ID is field 2, and the
    # kernel start time (field 22 in /proc documentation) is field 19.
    if len(fields) <= 19:
        return None
    try:
        process_group_id = int(fields[2])
        start_time = int(fields[19])
    except (IndexError, ValueError):
        return None
    if process_group_id <= 0:
        return None
    return _ProcessLeaderIdentity(
        pid=pid,
        process_group_id=process_group_id,
        start_time=start_time,
    )


def _capture_process_identity(process: subprocess.Popen[Any]) -> _ProcessLeaderIdentity | None:
    """Bound the launch race while waiting for the leader's /proc record."""
    for attempt in range(_PROCESS_IDENTITY_RETRY_ATTEMPTS):
        identity = _read_process_identity(process.pid)
        if identity is not None:
            return identity
        if process.poll() is not None or attempt + 1 == _PROCESS_IDENTITY_RETRY_ATTEMPTS:
            break
        time.sleep(_PROCESS_IDENTITY_RETRY_DELAY_SECONDS)
    return None


def _verified_process_group_id(
    process: subprocess.Popen[Any],
    leader_identity: _ProcessLeaderIdentity | None,
) -> int | None:
    """Return the group only while the original leader identity still owns it."""
    if leader_identity is None or leader_identity.pid != process.pid:
        return None
    # Popen.poll() also reaps a zombie leader.  A reaped/exited leader can no
    # longer prove ownership of a retained PGID, even if /proc still exposes a
    # short-lived zombie record with the same start time.
    if process.poll() is not None:
        return None
    current_identity = _read_process_identity(process.pid)
    if current_identity is None:
        return None
    if (
        current_identity.start_time != leader_identity.start_time
        or current_identity.process_group_id != leader_identity.process_group_id
    ):
        return None
    return current_identity.process_group_id


def _signal_verified_process_group(
    process: subprocess.Popen[Any],
    *,
    leader_identity: _ProcessLeaderIdentity | None,
    signal_number: signal.Signals,
) -> tuple[int | None, bool, bool, bool, bool]:
    """Signal a group only after checking the original leader identity."""
    group_id = _verified_process_group_id(process, leader_identity)
    if group_id is None:
        return None, False, False, False, False
    try:
        os.killpg(group_id, signal_number)
    except ProcessLookupError:
        # The group disappeared after the ownership check; no unverified PID
        # can be reused here, and the caller can report a safe empty group.
        return group_id, True, False, False, True
    except (AttributeError, PermissionError, OSError):
        return group_id, True, False, True, False
    return group_id, True, True, False, False


def _kill_parent_if_verified(
    process: subprocess.Popen[Any],
    leader_identity: _ProcessLeaderIdentity | None,
) -> bool:
    """Kill the parent only when the same leader identity is still present."""
    if process.poll() is not None or _verified_process_group_id(process, leader_identity) is None:
        return False
    try:
        process.kill()
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def _delete_runtime_dir(path: Path) -> bool:
    if not path.exists():
        return True
    removed = True
    for candidate in sorted(path.rglob("*"), reverse=True):
        try:
            if candidate.is_dir():
                candidate.rmdir()
            else:
                candidate.unlink(missing_ok=True)
        except OSError:
            removed = False
            logger.debug("Failed to delete runtime artifact %s", candidate, exc_info=True)
    try:
        path.rmdir()
    except OSError:
        removed = False
        logger.debug("Failed to delete runtime directory %s", path, exc_info=True)
    return removed


def _kill_process_group(
    process: subprocess.Popen[Any],
    *,
    leader_identity: _ProcessLeaderIdentity | None,
    wait_timeout: float = _PROCESS_STOP_WAIT_SECONDS,
) -> _GroupTerminationResult:
    """Stop a command and every same-group child within a bound.

    The original leader's Linux start time and current process-group ID are
    checked immediately before every group signal.  If the leader exited or
    its identity cannot be read, the group is unverifiable and no signal is
    sent.  A bounded wait/retry keeps a broken child from hanging cleanup, but
    the caller must retain a handle when ownership is unknown or signaling
    fails.  A descendant that calls ``setsid`` intentionally escapes this
    process-group boundary.
    """
    group_id, ownership_verified, group_signal_sent, group_signal_failed, group_missing = (
        _signal_verified_process_group(
            process,
            leader_identity=leader_identity,
            signal_number=signal.SIGKILL,
        )
    )
    if process.poll() is None:
        _kill_parent_if_verified(process, leader_identity)

    try:
        process.wait(timeout=max(0.01, float(wait_timeout)))
    except subprocess.TimeoutExpired:
        # Recheck ownership before the second KILL; never reuse a stale PGID.
        (
            retry_group_id,
            retry_verified,
            retry_sent,
            retry_failed,
            retry_missing,
        ) = _signal_verified_process_group(
            process,
            leader_identity=leader_identity,
            signal_number=signal.SIGKILL,
        )
        group_id = group_id or retry_group_id
        ownership_verified = ownership_verified or retry_verified
        group_signal_sent = group_signal_sent or retry_sent
        group_signal_failed = group_signal_failed or retry_failed
        group_missing = group_missing or retry_missing
        _kill_parent_if_verified(process, leader_identity)
        try:
            process.wait(timeout=max(0.01, float(wait_timeout)))
        except subprocess.TimeoutExpired:
            pass
    return _GroupTerminationResult(
        process_group_id=group_id,
        ownership_verified=ownership_verified,
        group_signal_sent=group_signal_sent,
        group_signal_failed=group_signal_failed,
        group_missing=group_missing,
        parent_reaped=process.poll() is not None,
    )


def _remaining_process_group_members(
    process_group_id: int | None,
    *,
    wait_timeout: float = _PROCESS_STOP_WAIT_SECONDS,
) -> int | None:
    """Return the number of same-group processes still alive within a bound."""
    if process_group_id is None or process_group_id <= 0:
        return 0

    deadline = time.monotonic() + max(0.01, float(wait_timeout))
    proc_root = Path("/proc")
    while True:
        member_count: int | None = None
        if proc_root.is_dir():
            member_count = 0
            try:
                entries = tuple(proc_root.iterdir())
            except OSError:
                member_count = None
            else:
                for entry in entries:
                    if not entry.name.isdecimal():
                        continue
                    try:
                        stat_text = (entry / "stat").read_text(encoding="utf-8")
                        closing_paren = stat_text.rfind(")")
                        if closing_paren < 0:
                            continue
                        fields = stat_text[closing_paren + 2 :].split()
                        # After the comm field, state is field 0, ppid field 1,
                        # and process-group ID field 2.
                        if len(fields) >= 3 and fields[0] != "Z" and int(fields[2]) == process_group_id:
                            member_count += 1
                    except (OSError, ValueError):
                        continue

        if member_count == 0:
            return 0
        if member_count is None:
            try:
                os.killpg(process_group_id, 0)
            except (AttributeError, ProcessLookupError):
                return 0
            except PermissionError:
                return None
            except OSError:
                return 0
            member_count = 1
        if time.monotonic() >= deadline:
            return member_count
        time.sleep(0.01)


def _termination_cleanup_status(termination: _GroupTerminationResult) -> str:
    """Translate verified group termination into an operator-facing status."""
    if not termination.ownership_verified:
        return "unknown"
    if termination.group_signal_failed:
        return "failed"
    if not (termination.group_signal_sent or termination.group_missing):
        return "failed"
    remaining = _remaining_process_group_members(termination.process_group_id)
    if termination.parent_reaped and remaining == 0:
        return "stopped"
    if termination.parent_reaped and termination.group_signal_sent:
        return "unknown"
    return "failed"


def _bounded_reap_process(process: subprocess.Popen[Any], *, timeout: float = 1.0) -> tuple[str, str]:
    """Reap pipes without allowing a descendant-held pipe to hang the caller."""
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return stdout or "", stderr or ""
    except subprocess.TimeoutExpired as exc:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        return exc.stdout or "", exc.stderr or ""


def _command_env(*, worker_root: Path | None = None) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _ENV_ALLOWLIST
    }
    env.setdefault("PYTHONUNBUFFERED", "1")
    env["SERAPH_SANDBOX_ENV"] = "allowlisted"
    if worker_root is None:
        env["HOME"] = str(_workspace_root())
        return env
    worker_root_str = str(worker_root)
    env["HOME"] = worker_root_str
    env["TMPDIR"] = worker_root_str
    env["TMP"] = worker_root_str
    env["TEMP"] = worker_root_str
    env["XDG_CACHE_HOME"] = worker_root_str
    env["XDG_STATE_HOME"] = worker_root_str
    env["XDG_DATA_HOME"] = worker_root_str
    env["PIP_CACHE_DIR"] = worker_root_str
    env["NPM_CONFIG_CACHE"] = worker_root_str
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
    worker_root: Path
    started_at: datetime
    owner_session_id: str | None
    process_group_id: int | None
    leader_identity: _ProcessLeaderIdentity | None
    stop_claimed: bool = False
    stop_requested: bool = False
    cleanup_status: str | None = None

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
            "worker_root": str(self.worker_root),
            "worker_disposable": True,
            "trust_partition": "session_disposable_worker",
            "session_scoped": self.owner_session_id is not None,
            "session_id": self.owner_session_id,
            "leader_identity_verified": self.leader_identity is not None,
            "stop_requested": self.stop_requested,
            "cleanup_status": self.cleanup_status,
        }


class ProcessRuntimeManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[str, ManagedProcess] = {}
        self._stopping_sessions: set[str] = set()
        self._last_session_cleanup_receipt: dict[str, Any] | None = None

    @property
    def last_session_cleanup_receipt(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._last_session_cleanup_receipt) if self._last_session_cleanup_receipt else None

    @staticmethod
    def _sorted_process_payloads(processes: list[ManagedProcess]) -> list[dict[str, Any]]:
        return [
            process.status_payload()
            for process in sorted(
                processes,
                key=lambda item: item.started_at,
                reverse=True,
            )
        ]

    @staticmethod
    def _is_visible_to_session(process: ManagedProcess, session_id: str | None) -> bool:
        if process.owner_session_id is None:
            return session_id is None
        return process.owner_session_id == session_id

    @staticmethod
    def _stop_managed_process(process: ManagedProcess, *, force: bool) -> dict[str, Any]:
        process_group_id = process.process_group_id
        group_signal_sent = False
        group_signal_failed = False
        ownership_verified = False
        group_missing = False
        if force:
            termination = _kill_process_group(
                process.popen,
                leader_identity=process.leader_identity,
            )
            group_signal_sent = termination.group_signal_sent
            group_signal_failed = termination.group_signal_failed
            ownership_verified = termination.ownership_verified
            group_missing = termination.group_missing
            parent_reaped = termination.parent_reaped
        else:
            (
                term_group_id,
                term_verified,
                term_sent,
                term_failed,
                term_missing,
            ) = _signal_verified_process_group(
                process.popen,
                leader_identity=process.leader_identity,
                signal_number=signal.SIGTERM,
            )
            process_group_id = process_group_id or term_group_id
            ownership_verified = term_verified
            group_signal_sent = term_sent
            group_signal_failed = term_failed
            group_missing = term_missing
            if term_failed:
                # Direct parent termination is allowed only after the same
                # leader-identity check used for group signaling.
                _kill_parent_if_verified(process.popen, process.leader_identity)
            try:
                process.popen.wait(timeout=_PROCESS_STOP_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                termination = _kill_process_group(
                    process.popen,
                    leader_identity=process.leader_identity,
                )
                process_group_id = process_group_id or termination.process_group_id
                ownership_verified = ownership_verified or termination.ownership_verified
                group_signal_sent = group_signal_sent or termination.group_signal_sent
                group_signal_failed = group_signal_failed or termination.group_signal_failed
                group_missing = group_missing or termination.group_missing
                parent_reaped = termination.parent_reaped
            else:
                parent_reaped = process.popen.poll() is not None

        if not ownership_verified:
            cleanup_status = "unknown"
            remaining_descendants = None
        elif group_signal_failed:
            cleanup_status = "failed"
            remaining_descendants = _remaining_process_group_members(process_group_id)
        else:
            remaining_descendants = _remaining_process_group_members(process_group_id)
            if remaining_descendants == 0 and (group_signal_sent or group_missing):
                cleanup_status = "stopped"
            elif parent_reaped and group_signal_sent:
                # The leader disappeared before a safe follow-up KILL. Keep
                # the handle because the surviving group is unverifiable.
                cleanup_status = "unknown"
            else:
                cleanup_status = "failed"
        process.cleanup_status = cleanup_status
        payload = process.status_payload()
        payload.update(
            {
                "stop_requested": True,
                "stopped": cleanup_status == "stopped",
                "cleanup_status": cleanup_status,
                "remaining_descendants": remaining_descendants,
            }
        )
        return payload

    @staticmethod
    def _delete_process_artifacts(process: ManagedProcess) -> bool:
        removed = True
        try:
            process.output_path.unlink(missing_ok=True)
        except OSError:
            removed = False
            logger.debug("Failed to delete process log %s", process.output_path, exc_info=True)
        return _delete_runtime_dir(process.worker_root) and removed

    @staticmethod
    def _cleanup_worker_if_exited(process: ManagedProcess) -> None:
        # Keep the worker root while its handle remains registered.  A leader
        # can exit before stop recovery runs, and deleting the root here would
        # make an identity-unknown handle less recoverable.
        process.popen.poll()

    def begin_session_cleanup(self, session_id: str) -> bool:
        with self._lock:
            if session_id in self._stopping_sessions:
                return False
            self._stopping_sessions.add(session_id)
            return True

    def end_session_cleanup(self, session_id: str) -> None:
        with self._lock:
            self._stopping_sessions.discard(session_id)

    def run_command(
        self,
        *,
        command: str,
        args_json: str = "",
        cwd: str = "",
        timeout_seconds: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        executable, args, resolved_cwd = _normalize_command_invocation(
            command=command,
            args_json=args_json,
            cwd=cwd,
        )
        timeout = _normalize_timeout_seconds(timeout_seconds)
        worker_root = _worker_runtime_root(uuid.uuid4().hex)
        process: subprocess.Popen[str] | None = None
        leader_identity: _ProcessLeaderIdentity | None = None
        runtime_cleanup_status = "not_requested"
        try:
            if cancel_event is not None and cancel_event.is_set():
                return {
                    "ok": False,
                    "cancelled": True,
                    "timed_out": False,
                    "exit_code": None,
                    "stdout": "",
                    "stderr": "",
                    "display_command": _display_command([executable, *args]),
                    "cwd": str(resolved_cwd),
                    "timeout_seconds": timeout,
                }
            process = subprocess.Popen(
                [executable, *args],
                cwd=str(resolved_cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                env=_command_env(worker_root=worker_root),
                start_new_session=True,
            )
            # start_new_session makes the child PID the process-group leader.
            # Retain its start time and PGID so a later timeout cannot signal a
            # recycled PID's process group.
            leader_identity = _capture_process_identity(process)
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired([executable, *args], timeout)
                try:
                    stdout, stderr = process.communicate(timeout=min(remaining, 0.25))
                    break
                except subprocess.TimeoutExpired:
                    if cancel_event is not None and cancel_event.is_set():
                        termination = _kill_process_group(
                            process,
                            leader_identity=leader_identity,
                        )
                        runtime_cleanup_status = _termination_cleanup_status(termination)
                        stdout, stderr = _bounded_reap_process(process)
                        return {
                            "ok": False,
                            "cancelled": True,
                            "timed_out": False,
                            "exit_code": process.returncode,
                            "stdout": stdout or "",
                            "stderr": stderr or "",
                            "display_command": _display_command([executable, *args]),
                            "cwd": str(resolved_cwd),
                            "timeout_seconds": timeout,
                            "cleanup_status": runtime_cleanup_status,
                        }
        except subprocess.TimeoutExpired as exc:
            if process is not None:
                termination = _kill_process_group(
                    process,
                    leader_identity=leader_identity,
                )
                runtime_cleanup_status = _termination_cleanup_status(termination)
                stdout, stderr = _bounded_reap_process(process)
            else:
                stdout, stderr = exc.stdout or "", exc.stderr or ""
            return {
                "ok": False,
                "cancelled": False,
                "timed_out": True,
                "exit_code": process.returncode if process is not None else None,
                "stdout": stdout or "",
                "stderr": stderr or "",
                "display_command": _display_command([executable, *args]),
                "cwd": str(resolved_cwd),
                "timeout_seconds": timeout,
                "cleanup_status": runtime_cleanup_status,
            }
        except OSError:
            raise
        finally:
            # run_command has no durable recovery handle.  Its bounded result
            # remains operator-visible even when ownership is unknown, while
            # the invocation-scoped worker root is still reclaimed here.
            _delete_runtime_dir(worker_root)

        return {
            "ok": process.returncode == 0,
            "cancelled": bool(cancel_event is not None and cancel_event.is_set()),
            "timed_out": False,
            "exit_code": process.returncode,
            "stdout": stdout or "",
            "stderr": stderr or "",
            "display_command": _display_command([executable, *args]),
            "cwd": str(resolved_cwd),
            "timeout_seconds": timeout,
            "cleanup_status": runtime_cleanup_status,
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
        owner_session_id = get_current_session_id()
        with self._lock:
            if owner_session_id is not None and owner_session_id in self._stopping_sessions:
                raise ValueError("session cleanup is in progress; start_process is temporarily unavailable.")
            process_id = uuid.uuid4().hex
            output_path = _process_runtime_root() / f"{process_id}.log"
            worker_root = _worker_runtime_root(process_id)
            output_fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(output_fd, "w", encoding="utf-8", errors="replace") as output_stream:
                popen = subprocess.Popen(
                    [executable, *args],
                    cwd=str(resolved_cwd),
                    stdout=output_stream,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    shell=False,
                    env=_command_env(worker_root=worker_root),
                    start_new_session=True,
                )
            leader_identity = _capture_process_identity(popen)
            managed = ManagedProcess(
                process_id=process_id,
                popen=popen,
                command=executable,
                args=args,
                cwd=str(resolved_cwd),
                output_path=output_path,
                worker_root=worker_root,
                started_at=_utc_now(),
                owner_session_id=owner_session_id,
                process_group_id=leader_identity.process_group_id if leader_identity else None,
                leader_identity=leader_identity,
            )
            self._processes[process_id] = managed
        return managed.status_payload()

    def list_processes(self) -> list[dict[str, Any]]:
        session_id = get_current_session_id()
        with self._lock:
            visible = [
                process
                for process in self._processes.values()
                if self._is_visible_to_session(process, session_id)
            ]
        for process in visible:
            self._cleanup_worker_if_exited(process)
        return self._sorted_process_payloads(visible)

    def list_all_processes(self) -> list[dict[str, Any]]:
        with self._lock:
            processes = list(self._processes.values())
        for process in processes:
            self._cleanup_worker_if_exited(process)
        return self._sorted_process_payloads(processes)

    def read_process_output(self, process_id: str, *, max_chars: int = _PROCESS_OUTPUT_DEFAULT) -> dict[str, Any] | None:
        session_id = get_current_session_id()
        with self._lock:
            process = self._processes.get(process_id)
        if process is None or not self._is_visible_to_session(process, session_id):
            return None
        self._cleanup_worker_if_exited(process)
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
        session_id = get_current_session_id()
        with self._lock:
            process = self._processes.get(process_id)
            if process is None or not self._is_visible_to_session(process, session_id):
                return None
            if process.stop_claimed:
                payload = process.status_payload()
                payload.update(
                    {
                        "stop_requested": True,
                        "stopped": False,
                        "cleanup_status": "conflict",
                        "remaining_descendants": None,
                        "registry_removed": False,
                        "artifacts_removed": False,
                    }
                )
                return payload
            process.stop_claimed = True
            process.stop_requested = True

        try:
            payload = self._stop_managed_process(process, force=force)
        except Exception as exc:
            logger.exception("Managed process cleanup failed for %s", process_id)
            process.cleanup_status = "failed"
            payload = process.status_payload()
            payload.update(
                {
                    "stop_requested": True,
                    "stopped": False,
                    "cleanup_status": "failed",
                    "remaining_descendants": None,
                    "cleanup_error": type(exc).__name__,
                }
            )

        artifacts_removed = False
        if payload["cleanup_status"] == "stopped":
            try:
                artifacts_removed = self._delete_process_artifacts(process)
            except Exception as exc:
                logger.exception("Managed process artifact cleanup failed for %s", process_id)
                payload["cleanup_error"] = type(exc).__name__
            if not artifacts_removed:
                process.cleanup_status = "failed"
                payload.update(
                    {
                        "stopped": False,
                        "cleanup_status": "failed",
                    }
                )

        if payload["cleanup_status"] == "stopped" and artifacts_removed:
            with self._lock:
                removed = self._processes.pop(process_id, None) is process
                process.stop_claimed = False
            payload["registry_removed"] = removed
            payload["artifacts_removed"] = True
            return payload

        with self._lock:
            process.stop_claimed = False
        payload["registry_removed"] = False
        payload["artifacts_removed"] = artifacts_removed
        return payload

    def reset_for_tests(self) -> None:
        with self._lock:
            processes = list(self._processes.values())
        for process in processes:
            try:
                self._stop_managed_process(process, force=True)
            except Exception:
                logger.debug("Failed to stop test process %s", process.process_id, exc_info=True)
            self._delete_process_artifacts(process)
        with self._lock:
            self._processes.clear()
            self._stopping_sessions.clear()
            self._last_session_cleanup_receipt = None

    def stop_processes_for_session(
        self,
        session_id: str,
        *,
        cleanup_fence_held: bool = False,
    ) -> int:
        """Stop session-owned processes while retaining a teardown fence.

        ``SessionManager.delete`` holds the fence across memory flush and the
        database transaction, so it passes ``cleanup_fence_held=True``.  Direct
        callers acquire and release their own fence.  The optional argument is
        intentionally keyword-only to preserve the existing public call shape.
        """
        fence_acquired = False
        if cleanup_fence_held:
            with self._lock:
                if session_id not in self._stopping_sessions:
                    raise RuntimeError("session cleanup fence must be held by the caller")
        else:
            fence_acquired = self.begin_session_cleanup(session_id)

        processes: list[ManagedProcess] = []
        claimed: list[ManagedProcess] = []
        conflicts = 0
        stopped_count = 0
        unknown_count = 0
        failed_count = 0
        processed_count = 0
        try:
            with self._lock:
                processes = [
                    process
                    for process in self._processes.values()
                    if process.owner_session_id == session_id
                ]
                for process in processes:
                    if process.stop_claimed:
                        conflicts += 1
                        continue
                    process.stop_claimed = True
                    process.stop_requested = True
                    claimed.append(process)

            for process in claimed:
                try:
                    payload = self._stop_managed_process(process, force=True)
                except Exception as exc:
                    logger.exception("Session process cleanup failed for %s", process.process_id)
                    process.cleanup_status = "failed"
                    payload = {
                        "cleanup_status": "failed",
                        "cleanup_error": type(exc).__name__,
                    }

                artifacts_removed = False
                if payload["cleanup_status"] == "stopped":
                    try:
                        artifacts_removed = self._delete_process_artifacts(process)
                    except Exception:
                        logger.exception("Session process artifact cleanup failed for %s", process.process_id)
                    if not artifacts_removed:
                        process.cleanup_status = "failed"
                        payload["cleanup_status"] = "failed"

                if payload["cleanup_status"] == "stopped" and artifacts_removed:
                    with self._lock:
                        self._processes.pop(process.process_id, None)
                        process.stop_claimed = False
                    stopped_count += 1
                else:
                    if payload["cleanup_status"] == "unknown":
                        unknown_count += 1
                    else:
                        failed_count += 1
                    with self._lock:
                        process.stop_claimed = False
                processed_count += 1
        except Exception:
            # Keep the receipt truthful if an unexpected manager failure stops
            # the bounded loop before all claims were visited.
            failed_count += max(0, len(claimed) - processed_count)
            raise
        finally:
            with self._lock:
                for process in claimed:
                    if process.process_id in self._processes:
                        process.stop_claimed = False
                receipt = {
                    "session_id": session_id,
                    "requested": len(processes),
                    "stopped": stopped_count,
                    "unknown": unknown_count,
                    "failed": failed_count,
                    "conflict": conflicts,
                }
                self._last_session_cleanup_receipt = receipt
            if fence_acquired:
                self.end_session_cleanup(session_id)
        return len(processes)


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
                "cleanup_status": result["cleanup_status"],
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

    def get_approval_context(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return _process_approval_context(
            "run_command",
            arguments,
            persistent_background_execution=False,
        )

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
                "leader_identity_verified": payload["leader_identity_verified"],
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

    def get_approval_context(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return _process_approval_context(
            "start_process",
            arguments,
            persistent_background_execution=True,
        )

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

    def get_approval_context(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        return _process_approval_context(
            "list_processes",
            {},
            persistent_background_execution=False,
        )


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

    def get_approval_context(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return _process_approval_context(
            "read_process_output",
            arguments,
            persistent_background_execution=False,
        )

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
            f"stop_process {payload['cleanup_status']} {payload['process_id']}",
            {
                "process_id": payload["process_id"],
                "pid": payload["pid"],
                "exit_code": payload["exit_code"],
                "forced": bool(arguments.get("force", False)),
                "stopped": payload["stopped"],
                "cleanup_status": payload["cleanup_status"],
                "remaining_descendants": payload["remaining_descendants"],
                "registry_removed": payload["registry_removed"],
                "artifacts_removed": payload["artifacts_removed"],
            },
        ))
        if payload["cleanup_status"] == "conflict":
            return f"Stop for process '{payload['process_id']}' is already in progress."
        if payload["cleanup_status"] != "stopped":
            return (
                f"Stop requested for process '{payload['process_id']}' but cleanup_status="
                f"{payload['cleanup_status']} (handle and artifacts retained)."
            )
        return (
            f"Stopped process '{payload['process_id']}' with exit_code={payload['exit_code']} "
            f"(remaining_descendants={payload['remaining_descendants']})."
        )

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

    def get_approval_context(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return _process_approval_context(
            "stop_process",
            arguments,
            persistent_background_execution=False,
        )

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
