"""Container-scoped shell and process runtime tools."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import resource
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
# The process manager already applies tighter display/timeout limits. These
# resource ceilings are the hard local profile carried by every child. The
# lower display limit remains an intentional operator/UI bound.
_PROCESS_CPU_SECONDS = 300
_PROCESS_MEMORY_BYTES = 512 * 1024 * 1024
_PROCESS_PID_LIMIT = 64
_PROCESS_OUTPUT_BYTES = 1 * 1024 * 1024
_PROCESS_PIPE_CHUNK_BYTES = 64 * 1024
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
_GIT_NETWORK_SUBCOMMANDS = frozenset(
    {
        "clone",
        "fetch",
        "pull",
        "push",
        "remote",
        "submodule",
        "fetch-pack",
        "receive-pack",
        "send-pack",
    }
)
_NPM_NETWORK_SUBCOMMANDS = frozenset(
    {
        "install",
        "ci",
        "update",
        "publish",
        "pack",
        "link",
        "outdated",
        "audit",
        "view",
        "info",
        "search",
        "exec",
        "run",
        "test",
        "start",
        "restart",
        "uninstall",
        "unpublish",
        "deprecate",
        "dist-tag",
        "access",
        "owner",
        "team",
        "token",
        "profile",
        "whoami",
        "login",
        "logout",
        "adduser",
        "fund",
    }
)
_UV_NETWORK_SUBCOMMANDS = frozenset(
    {
        "run",
        "pip",
        "sync",
        "lock",
        "add",
        "remove",
        "tool",
        "publish",
        "build",
        "python",
    }
)
_NETWORK_CAPABLE_FLAGS = {
    "--registry",
    "--proxy",
    "--https-proxy",
    "--http-proxy",
    "--fetch-retries",
    "--fetch-retry-factor",
    "--fetch-retry-maxtimeout",
    "--index-url",
    "--extra-index-url",
    "--default-index",
    "--find-links",
    "--allow-insecure-host",
    "--upload-pack",
    "--receive-pack",
    "--exec-path",
    "--config",
    "--config-env",
    "--userconfig",
    "--globalconfig",
}
_NETWORK_CAPABLE_FLAG_PREFIXES = tuple(f"{flag}=" for flag in _NETWORK_CAPABLE_FLAGS)
_NETWORK_ARGUMENT_PREFIXES = ("http://", "https://", "ssh://", "git@")
_GIT_SAFE_INLINE_CONFIG_KEYS = frozenset({"user.name", "user.email"})
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


def _reject_network_capable_package_args(command_name: str, args: list[str]) -> None:
    """Reject package/VCS operations that can contact a remote service."""
    if command_name not in {"git", "npm", "uv"}:
        return

    for index, arg in enumerate(args):
        lowered = arg.strip().lower()
        if command_name == "git" and arg == "-c":
            config_value = args[index + 1].strip().lower() if index + 1 < len(args) else ""
            # Native fixture commits need identity settings. Every other
            # inline setting remains denied because aliases, hooks, remote
            # rewrites, and proxy values can add an egress or shell path.
            config_key = config_value.split("=", 1)[0].strip()
            if config_key not in _GIT_SAFE_INLINE_CONFIG_KEYS:
                raise ValueError("git network-capable config is blocked in the process runtime.")
            continue
        if lowered.startswith(_NETWORK_ARGUMENT_PREFIXES):
            raise ValueError(f"{command_name} network destinations are blocked in the process runtime.")
        if lowered in _NETWORK_CAPABLE_FLAGS or lowered.startswith(_NETWORK_CAPABLE_FLAG_PREFIXES):
            raise ValueError(f"{command_name} network-capable flags are blocked in the process runtime.")

    # Options that take a value must be skipped while locating the subcommand;
    # otherwise ``git -C workspace status`` would mistake the path for it.
    value_options = {
        "git": {"-C", "-c", "--config-env", "--upload-pack", "--receive-pack", "--exec-path"},
        "npm": {"--registry", "--proxy", "--https-proxy", "--userconfig", "--globalconfig"},
        "uv": {"--index-url", "--extra-index-url", "--default-index", "--find-links", "--proxy"},
    }[command_name]
    subcommand: str | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        lowered = arg.lower()
        if lowered in value_options:
            index += 2
            continue
        if lowered == "--":
            if index + 1 < len(args):
                subcommand = args[index + 1].lower()
            break
        if lowered.startswith("-"):
            index += 1
            continue
        subcommand = lowered
        break

    blocked = {
        "git": _GIT_NETWORK_SUBCOMMANDS,
        "npm": _NPM_NETWORK_SUBCOMMANDS,
        "uv": _UV_NETWORK_SUBCOMMANDS,
    }[command_name]
    if subcommand in blocked:
        raise ValueError(f"{command_name} subcommand '{subcommand}' is blocked in the process runtime.")


def _reject_network_script_markers(script_path: Path) -> None:
    try:
        body = script_path.read_text(encoding="utf-8", errors="ignore").lower()
    except OSError as exc:
        raise ValueError("script path must point to a readable workspace file.") from exc
    if any(marker in body for marker in _NETWORK_SCRIPT_MARKERS):
        raise ValueError("script network clients are blocked in the process runtime.")


def _validate_workspace_scoped_args(executable: str, args: list[str], cwd: Path) -> None:
    command_name = Path(executable).name

    _reject_network_capable_package_args(command_name, args)

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
    # Never load an unbounded background log into memory. The process profile
    # caps the file itself, and this seek keeps reads bounded even if an older
    # process predates that cap.
    max_bytes = max(1, max_chars * 4)
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            if size > max_bytes:
                stream.seek(-max_bytes, os.SEEK_END)
                data = stream.read(max_bytes).decode("utf-8", errors="replace")
                return "...[truncated]...\n" + data[-max_chars:], True
            data = stream.read().decode("utf-8", errors="replace")
    except OSError:
        return "", False
    if len(data) <= max_chars:
        return data, False
    return "...[truncated]...\n" + data[-max_chars:], True


def _apply_process_limits() -> None:
    """Apply the local process profile in the child before it executes."""
    # The runtime is currently POSIX-only. Keep the import/module guard so a
    # future non-POSIX test can still import the process tool module.
    if os.name != "posix":
        return
    for limit_name, requested in (
        (resource.RLIMIT_CPU, _PROCESS_CPU_SECONDS),
        (resource.RLIMIT_AS, _PROCESS_MEMORY_BYTES),
        (resource.RLIMIT_NPROC, _PROCESS_PID_LIMIT),
        (resource.RLIMIT_FSIZE, _PROCESS_OUTPUT_BYTES),
    ):
        try:
            current_soft, current_hard = resource.getrlimit(limit_name)
            if limit_name == resource.RLIMIT_NPROC:
                # Preserve an existing finite host limit. Linux may account
                # the runner's threads toward RLIMIT_NPROC, so replacing a
                # higher inherited limit with 64 can make a child unable to
                # fork its own bounded helper. An unlimited host profile gets
                # a finite fallback instead of losing the process ceiling.
                requested = max(requested, current_soft) if current_soft != resource.RLIM_INFINITY else 1024
            hard = current_hard if current_hard != resource.RLIM_INFINITY else requested
            soft = min(requested, hard)
            resource.setrlimit(limit_name, (soft, hard))
        except (AttributeError, OSError, ValueError):
            # A container may disallow one profile limit. The caller still
            # retains the explicit timeout/output/process-tree safeguards.
            logger.debug("Unable to apply child resource limit", exc_info=True)


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
class _ProcessTreeEntry:
    pid: int
    parent_pid: int
    process_group_id: int
    start_time: int
    state: str


@dataclass(frozen=True)
class _ProcessDescendantIdentity:
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
    descendant_identities: tuple[_ProcessDescendantIdentity, ...] | None


def _read_process_stat(pid: int) -> _ProcessTreeEntry | None:
    """Read the process identity fields needed for a bounded descendant scan."""
    try:
        stat_text = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    closing_paren = stat_text.rfind(")")
    if closing_paren < 0:
        return None
    fields = stat_text[closing_paren + 2 :].split()
    # After comm, state is field 0, ppid field 1, process-group ID field 2,
    # and kernel start time (field 22 in /proc documentation) is field 19.
    if len(fields) <= 19:
        return None
    try:
        parent_pid = int(fields[1])
        process_group_id = int(fields[2])
        start_time = int(fields[19])
    except (IndexError, ValueError):
        return None
    # Kernel threads report process-group ID 0.  Retain those entries in a
    # complete scan so their expected, non-descendant presence does not make
    # every ordinary user-process snapshot permanently unknown.
    if parent_pid < 0 or process_group_id < 0 or start_time < 0:
        return None
    return _ProcessTreeEntry(
        pid=pid,
        parent_pid=parent_pid,
        process_group_id=process_group_id,
        start_time=start_time,
        state=fields[0],
    )


def _read_process_identity(pid: int) -> _ProcessLeaderIdentity | None:
    """Read the Linux process start time and process group for a leader PID."""
    entry = _read_process_stat(pid)
    if entry is None:
        return None
    return _ProcessLeaderIdentity(
        pid=pid,
        process_group_id=entry.process_group_id,
        start_time=entry.start_time,
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


def _snapshot_process_descendants(
    process: subprocess.Popen[Any],
    leader_identity: _ProcessLeaderIdentity | None,
) -> tuple[_ProcessDescendantIdentity, ...] | None:
    """Capture descendant PID/start-time identities immediately before stop.

    A process-group scan cannot see a child that calls ``setsid``.  This
    bounded process-tree snapshot lets cleanup check those children after the
    original group is signaled.  Any incomplete scan is treated as unknown so
    a disappearing ``/proc`` entry cannot turn into a false success receipt.
    """
    if _verified_process_group_id(process, leader_identity) is None:
        return None
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return None

    for attempt in range(_PROCESS_IDENTITY_RETRY_ATTEMPTS):
        try:
            entries = tuple(proc_root.iterdir())
        except OSError:
            return None
        process_entries: dict[int, _ProcessTreeEntry] = {}
        complete = True
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            try:
                pid = int(entry.name)
            except ValueError:
                complete = False
                continue
            stat_entry = _read_process_stat(pid)
            if stat_entry is None:
                # A proc entry can disappear between the directory listing
                # and stat read.  Treat that race as gone; keep the snapshot
                # unknown only when the PID is still live but unreadable.
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    continue
                except PermissionError:
                    complete = False
                except OSError:
                    continue
                else:
                    complete = False
                continue
            process_entries[pid] = stat_entry

        root_entry = process_entries.get(process.pid)
        if root_entry is None:
            return None
        if (
            root_entry.start_time != leader_identity.start_time
            or root_entry.process_group_id != leader_identity.process_group_id
        ):
            return None
        if not complete:
            if attempt + 1 == _PROCESS_IDENTITY_RETRY_ATTEMPTS:
                return None
            time.sleep(_PROCESS_IDENTITY_RETRY_DELAY_SECONDS)
            continue

        children_by_parent: dict[int, list[_ProcessTreeEntry]] = {}
        for stat_entry in process_entries.values():
            children_by_parent.setdefault(stat_entry.parent_pid, []).append(stat_entry)
        descendants: list[_ProcessDescendantIdentity] = []
        pending = [process.pid]
        seen = {process.pid}
        while pending:
            parent_pid = pending.pop()
            for child in children_by_parent.get(parent_pid, []):
                if child.pid in seen:
                    continue
                seen.add(child.pid)
                descendants.append(
                    _ProcessDescendantIdentity(
                        pid=child.pid,
                        process_group_id=child.process_group_id,
                        start_time=child.start_time,
                    )
                )
                pending.append(child.pid)
        return tuple(sorted(descendants, key=lambda item: item.pid))
    return None


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
    descendant_identities: tuple[_ProcessDescendantIdentity, ...] | None,
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
    # Keep the originally captured PGID for bounded liveness reconciliation
    # even when the leader disappeared before the ownership check.  This value
    # is used only for observation here; no signal is sent without a fresh
    # leader identity match.
    return _GroupTerminationResult(
        process_group_id=group_id or (leader_identity.process_group_id if leader_identity else None),
        ownership_verified=ownership_verified,
        group_signal_sent=group_signal_sent,
        group_signal_failed=group_signal_failed,
        group_missing=group_missing,
        parent_reaped=process.poll() is not None,
        descendant_identities=descendant_identities,
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
                        try:
                            os.kill(int(entry.name), 0)
                        except ProcessLookupError:
                            continue
                        except PermissionError:
                            member_count = None
                            break
                        except OSError:
                            continue
                        member_count = None
                        break

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


def _remaining_process_descendants(
    descendant_identities: tuple[_ProcessDescendantIdentity, ...] | None,
    *,
    wait_timeout: float = _PROCESS_STOP_WAIT_SECONDS,
) -> int | None:
    """Count captured descendants that still have the same PID/start time."""
    if descendant_identities is None:
        return None
    deadline = time.monotonic() + max(0.01, float(wait_timeout))
    while True:
        remaining = 0
        for identity in descendant_identities:
            current = _read_process_identity(identity.pid)
            if current is None:
                try:
                    os.kill(identity.pid, 0)
                except ProcessLookupError:
                    continue
                except PermissionError:
                    return None
                except OSError:
                    continue
                return None
            if current.start_time == identity.start_time:
                remaining += 1
        if remaining == 0:
            return 0
        if time.monotonic() >= deadline:
            return remaining
        time.sleep(0.01)


def _termination_cleanup_status(
    termination: _GroupTerminationResult,
) -> tuple[str, int | None]:
    """Translate verified group/tree termination into status and survivor count."""
    if not termination.ownership_verified:
        # The leader may have exited between the pre-stop snapshot and the
        # ownership check.  A no-signal path can still complete safely when
        # the captured descendant identities and the original group both
        # prove empty.  Keep the result unknown while either observation is
        # unavailable or a captured identity remains live.
        group_remaining = _remaining_process_group_members(termination.process_group_id)
        descendant_remaining = _remaining_process_descendants(termination.descendant_identities)
        if group_remaining is None or descendant_remaining is None:
            return "unknown", None
        remaining = max(group_remaining, descendant_remaining)
        if termination.parent_reaped and remaining == 0:
            return "stopped", 0
        return "unknown", remaining
    if termination.group_signal_failed:
        return "failed", _remaining_process_group_members(termination.process_group_id)
    if not (termination.group_signal_sent or termination.group_missing):
        return "failed", None
    group_remaining = _remaining_process_group_members(termination.process_group_id)
    descendant_remaining = _remaining_process_descendants(termination.descendant_identities)
    if group_remaining is None or descendant_remaining is None:
        return "unknown", None
    remaining = max(group_remaining, descendant_remaining)
    if termination.parent_reaped and remaining == 0:
        return "stopped", 0
    if termination.parent_reaped and termination.group_signal_sent:
        return "unknown", remaining
    return "failed", remaining


class _BoundedPipeCapture:
    """Drain one child pipe while retaining only a bounded prefix."""

    def __init__(self, stream: Any, *, limit: int = _PROCESS_OUTPUT_BYTES) -> None:
        self._stream = stream
        self._limit = max(1, int(limit))
        self._buffer = bytearray()
        self.truncated = False
        self._done = threading.Event()
        if stream is None:
            self._thread: threading.Thread | None = None
            self._done.set()
            return
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        try:
            while True:
                chunk = self._stream.read(_PROCESS_PIPE_CHUNK_BYTES)
                if not chunk:
                    return
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                remaining = self._limit - len(self._buffer)
                if remaining > 0:
                    self._buffer.extend(chunk[:remaining])
                if len(chunk) > max(0, remaining):
                    self.truncated = True
        except (OSError, ValueError):
            # The parent closes a descendant-held pipe after bounded cleanup.
            return
        finally:
            self._done.set()

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def finish(self, *, timeout: float = 1.0) -> None:
        if self._thread is None:
            return
        self._thread.join(timeout=max(0.0, timeout))
        if self._thread.is_alive():
            try:
                self._stream.close()
            except (OSError, ValueError):
                pass
            self._thread.join(timeout=0.05)

    def text(self) -> str:
        return bytes(self._buffer).decode("utf-8", errors="replace")


def _bounded_reap_process(
    process: subprocess.Popen[Any],
    *,
    captures: tuple[_BoundedPipeCapture, _BoundedPipeCapture] | None = None,
    timeout: float = 1.0,
) -> tuple[str, str]:
    """Reap bounded captures without reading an unbounded child pipe."""
    if captures is not None:
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        deadline = time.monotonic() + max(0.0, timeout)
        for capture in captures:
            capture.finish(timeout=max(0.0, deadline - time.monotonic()))
        return captures[0].text(), captures[1].text()

    # Defensive callers that do not yet own capture threads still get the same
    # bounded behavior; never fall back to ``communicate`` here.
    fallback_captures = (
        _BoundedPipeCapture(process.stdout),
        _BoundedPipeCapture(process.stderr),
    )
    return _bounded_reap_process(process, captures=fallback_captures, timeout=timeout)


def _command_env(*, worker_root: Path | None = None) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _ENV_ALLOWLIST
    }
    # Resolve allowlisted helper commands (for example ``pytest``) from the
    # same interpreter environment that owns this runtime.  A caller's PATH
    # can put a user-level wrapper ahead of the active virtualenv; that wrapper
    # may point at a different Python installation and make an otherwise valid
    # deterministic workflow fail before it reaches its governed handler.
    # Preserve the virtualenv wrapper directory. ``sys.executable`` commonly
    # points through a symlink into a shared interpreter installation, while
    # sibling entry points such as ``pytest`` live beside the wrapper itself.
    interpreter_bin = str(Path(sys.executable).absolute().parent)
    caller_path = env.get("PATH", "")
    env["PATH"] = os.pathsep.join(
        entry for entry in (interpreter_bin, caller_path) if entry
    )
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
    # Persist the last complete pre-stop process-tree snapshot.  If the
    # leader exits before a later stop/reconciliation call, this lets cleanup
    # check the original descendants by PID and start time without signaling
    # an unverified, potentially recycled process group.
    descendant_identities: tuple[_ProcessDescendantIdentity, ...] | None = None

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


class SessionProcessCleanupError(RuntimeError):
    """Raised when session teardown cannot prove all owned processes stopped."""

    def __init__(self, receipt: dict[str, Any]):
        self.receipt = dict(receipt)
        super().__init__(
            "session process cleanup did not complete: "
            f"unknown={self.receipt.get('unknown', 0)}, "
            f"failed={self.receipt.get('failed', 0)}, "
            f"conflict={self.receipt.get('conflict', 0)}"
        )


class SessionCleanupInProgressError(RuntimeError):
    """Raised when a direct process stop races session teardown."""


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
        current_descendant_identities = _snapshot_process_descendants(
            process.popen,
            process.leader_identity,
        )
        if current_descendant_identities is not None:
            process.descendant_identities = current_descendant_identities
        descendant_identities = (
            current_descendant_identities
            if current_descendant_identities is not None
            else process.descendant_identities
        )
        group_signal_sent = False
        group_signal_failed = False
        ownership_verified = False
        group_missing = False
        if force:
            termination = _kill_process_group(
                process.popen,
                leader_identity=process.leader_identity,
                descendant_identities=descendant_identities,
            )
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
                    descendant_identities=descendant_identities,
                )
                process_group_id = process_group_id or termination.process_group_id
                ownership_verified = ownership_verified or termination.ownership_verified
                group_signal_sent = group_signal_sent or termination.group_signal_sent
                group_signal_failed = group_signal_failed or termination.group_signal_failed
                group_missing = group_missing or termination.group_missing
                termination = _GroupTerminationResult(
                    process_group_id=process_group_id,
                    ownership_verified=ownership_verified,
                    group_signal_sent=group_signal_sent,
                    group_signal_failed=group_signal_failed,
                    group_missing=group_missing,
                    parent_reaped=termination.parent_reaped,
                    descendant_identities=descendant_identities,
                )
            else:
                termination = _GroupTerminationResult(
                    process_group_id=process_group_id,
                    ownership_verified=ownership_verified,
                    group_signal_sent=group_signal_sent,
                    group_signal_failed=group_signal_failed,
                    group_missing=group_missing,
                    parent_reaped=process.popen.poll() is not None,
                    descendant_identities=descendant_identities,
                )

        cleanup_status, remaining_descendants = _termination_cleanup_status(termination)
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
        if process.popen.poll() is None:
            descendant_identities = _snapshot_process_descendants(
                process.popen,
                process.leader_identity,
            )
            if descendant_identities is not None:
                process.descendant_identities = descendant_identities

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
        process: subprocess.Popen[bytes] | None = None
        leader_identity: _ProcessLeaderIdentity | None = None
        captures: tuple[_BoundedPipeCapture, _BoundedPipeCapture] | None = None
        runtime_cleanup_status = "not_requested"
        remaining_descendants: int | None = None
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
                text=False,
                shell=False,
                env=_command_env(worker_root=worker_root),
                start_new_session=True,
                preexec_fn=_apply_process_limits if os.name == "posix" else None,
            )
            # start_new_session makes the child PID the process-group leader.
            # Retain its start time and PGID so a later timeout cannot signal a
            # recycled PID's process group.
            leader_identity = _capture_process_identity(process)
            captures = (
                _BoundedPipeCapture(process.stdout),
                _BoundedPipeCapture(process.stderr),
            )
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired([executable, *args], timeout)
                if process.poll() is not None and captures[0].done and captures[1].done:
                    break
                if cancel_event is not None and cancel_event.is_set():
                    descendant_identities = _snapshot_process_descendants(
                        process,
                        leader_identity,
                    )
                    termination = _kill_process_group(
                        process,
                        leader_identity=leader_identity,
                        descendant_identities=descendant_identities,
                    )
                    runtime_cleanup_status, remaining_descendants = _termination_cleanup_status(termination)
                    stdout, stderr = _bounded_reap_process(process, captures=captures)
                    return {
                        "ok": False,
                        "cancelled": True,
                        "timed_out": False,
                        "exit_code": process.returncode,
                        "stdout": stdout,
                        "stderr": stderr,
                        "display_command": _display_command([executable, *args]),
                        "cwd": str(resolved_cwd),
                        "timeout_seconds": timeout,
                        "cleanup_status": runtime_cleanup_status,
                        "remaining_descendants": remaining_descendants,
                        "worker_root": (
                            str(worker_root)
                            if runtime_cleanup_status not in {"not_requested", "stopped"}
                            else None
                        ),
                    }
                time.sleep(min(0.05, remaining))
        except subprocess.TimeoutExpired as exc:
            if process is not None:
                descendant_identities = _snapshot_process_descendants(
                    process,
                    leader_identity,
                )
                termination = _kill_process_group(
                    process,
                    leader_identity=leader_identity,
                    descendant_identities=descendant_identities,
                )
                runtime_cleanup_status, remaining_descendants = _termination_cleanup_status(termination)
                if captures is not None:
                    stdout, stderr = _bounded_reap_process(process, captures=captures)
                else:
                    stdout, stderr = _bounded_reap_process(process)
            else:
                stdout, stderr = exc.stdout or "", exc.stderr or ""
            return {
                "ok": False,
                "cancelled": False,
                "timed_out": True,
                "exit_code": process.returncode if process is not None else None,
                "stdout": stdout,
                "stderr": stderr,
                "display_command": _display_command([executable, *args]),
                "cwd": str(resolved_cwd),
                "timeout_seconds": timeout,
                "cleanup_status": runtime_cleanup_status,
                "remaining_descendants": remaining_descendants,
                "worker_root": (
                    str(worker_root)
                    if runtime_cleanup_status not in {"not_requested", "stopped"}
                    else None
                ),
            }
        except OSError:
            raise
        finally:
            # Preserve the worker root when cleanup is unknown or failed so an
            # operator can inspect the retained state while the detached
            # descendant remains alive.  Successful bounded cleanup reclaims
            # the invocation-scoped root.
            if runtime_cleanup_status in {"not_requested", "stopped"}:
                _delete_runtime_dir(worker_root)

        return {
            "ok": process.returncode == 0,
            "cancelled": bool(cancel_event is not None and cancel_event.is_set()),
            "timed_out": False,
            "exit_code": process.returncode,
            "stdout": captures[0].text() if captures is not None else "",
            "stderr": captures[1].text() if captures is not None else "",
            "display_command": _display_command([executable, *args]),
            "cwd": str(resolved_cwd),
            "timeout_seconds": timeout,
            "cleanup_status": runtime_cleanup_status,
            "remaining_descendants": remaining_descendants,
            "worker_root": (
                str(worker_root)
                if runtime_cleanup_status not in {"not_requested", "stopped"}
                else None
            ),
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
                    preexec_fn=_apply_process_limits if os.name == "posix" else None,
                )
            leader_identity = _capture_process_identity(popen)
            descendant_identities = _snapshot_process_descendants(popen, leader_identity)
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
                descendant_identities=descendant_identities,
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
            if (
                process.owner_session_id is not None
                and process.owner_session_id in self._stopping_sessions
            ):
                raise SessionCleanupInProgressError("session cleanup is in progress")
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
        fail_closed: bool = False,
    ) -> int:
        """Stop session-owned processes while retaining a teardown fence.

        ``SessionManager.delete`` holds the fence across memory flush and the
        database transaction, so it passes ``cleanup_fence_held=True``.  Direct
        callers acquire and release their own fence.  ``fail_closed`` is used
        by session deletion to turn an unknown, failed, or conflicting process
        stop into a transaction failure rather than deleting the session while
        a retained handle may still be live.  The optional arguments are
        intentionally keyword-only to preserve the existing public call shape.
        """
        fence_acquired = False
        if cleanup_fence_held:
            with self._lock:
                if session_id not in self._stopping_sessions:
                    raise RuntimeError("session cleanup fence must be held by the caller")
        else:
            fence_acquired = self.begin_session_cleanup(session_id)
            if not fence_acquired:
                with self._lock:
                    self._last_session_cleanup_receipt = {
                        "session_id": session_id,
                        "requested": 0,
                        "stopped": 0,
                        "unknown": 0,
                        "failed": 0,
                        "conflict": 1,
                    }
                raise RuntimeError("session cleanup is already in progress")

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
        if fail_closed and (unknown_count or failed_count or conflicts):
            raise SessionProcessCleanupError(receipt)
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
                "remaining_descendants": result.get("remaining_descendants"),
                "worker_root": result.get("worker_root"),
                "stdout_chars": len(result["stdout"]),
                "stderr_chars": len(result["stderr"]),
                "output_truncated": truncated,
            },
        ))

        if result["timed_out"]:
            if result.get("cleanup_status") not in {"not_requested", "stopped"}:
                return (
                    f"Error: command timed out after {result['timeout_seconds']}s; "
                    f"cleanup_status={result.get('cleanup_status', 'unknown')} "
                    f"(worker state retained at {result.get('worker_root', 'unknown')})."
                )
            return f"Error: command timed out after {result['timeout_seconds']}s."
        if result.get("cancelled") and result.get("cleanup_status") not in {"not_requested", "stopped"}:
            return (
                "Error: command cancellation cleanup is "
                f"{result.get('cleanup_status', 'unknown')} "
                f"(worker state retained at {result.get('worker_root', 'unknown')})."
            )
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
        try:
            payload = process_runtime_manager.stop_process(**arguments)
        except SessionCleanupInProgressError as exc:
            _stop_process_audit_payload.set((
                f"stop_process conflict {arguments['process_id']}",
                {
                    "process_id": arguments["process_id"],
                    "forced": bool(arguments.get("force", False)),
                    "stopped": False,
                    "cleanup_status": "conflict",
                    "cleanup_conflict": "session_cleanup_in_progress",
                    "remaining_descendants": None,
                    "registry_removed": False,
                    "artifacts_removed": False,
                },
            ))
            return f"Stop for process '{arguments['process_id']}' blocked: {exc}."
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
                "cleanup_conflict": payload.get("cleanup_conflict"),
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
