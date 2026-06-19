"""Local Codex command-backed operator adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from config.settings import REPO_ROOT, settings
from src.audit.repository import audit_repository

_OUTPUT_LIMIT = 24_000
_STATUS_TIMEOUT_SECONDS = 5
_ENV_ALLOWLIST = {
    "PATH",
    "HOME",
    "CODEX_HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TZ",
}
_APPROVAL_POLICIES = {"untrusted", "on-request", "never"}
_SANDBOX_MODES = {"read-only", "workspace-write"}
_SECRET_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
)


class LocalCodexConfigurationError(RuntimeError):
    """Raised when the local Codex operator is not usable."""


@dataclass(frozen=True)
class LocalCodexCommand:
    argv: list[str]
    cwd: Path
    timeout_seconds: int

    @property
    def display(self) -> str:
        return " ".join(_display_arg(arg) for arg in self.argv)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _display_arg(value: str) -> str:
    if not value:
        return "''"
    if any(char.isspace() for char in value):
        return repr(value)
    return value


def _safe_command_name(raw_command: str | None = None) -> str:
    command = (raw_command if raw_command is not None else settings.codex_local_command).strip()
    if not command:
        raise LocalCodexConfigurationError("Local Codex command is not configured.")
    if any(char in command for char in ("|", "&", ";", "<", ">", "`", "$", "\n", "\r", "\t")):
        raise LocalCodexConfigurationError("Local Codex command must be a single executable path or name.")
    return command


def _workspace_root(raw_cwd: str | None = None) -> Path:
    if raw_cwd and raw_cwd.strip():
        candidate = Path(raw_cwd.strip())
        root = candidate if candidate.is_absolute() else REPO_ROOT / candidate
    else:
        root = REPO_ROOT
    root = root.resolve()
    try:
        root.relative_to(REPO_ROOT)
    except ValueError as exc:
        raise LocalCodexConfigurationError("Local Codex workspace root must stay inside the Seraph repo.") from exc
    if not root.exists() or not root.is_dir():
        raise LocalCodexConfigurationError("Local Codex workspace root must be an existing directory.")
    return root


def _timeout_seconds(raw_timeout: int | None = None) -> int:
    timeout = raw_timeout if raw_timeout is not None else settings.codex_local_timeout_seconds
    return min(max(int(timeout or 1), 1), 3600)


def _codex_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _ENV_ALLOWLIST and value
    }
    env.setdefault("PATH", os.defpath)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env["SERAPH_LOCAL_OPERATOR"] = "codex-local"
    return env


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _truncate_output(value: str) -> tuple[str, bool]:
    redacted = _redact_output(value)
    if len(redacted) <= _OUTPUT_LIMIT:
        return redacted, False
    return redacted[:_OUTPUT_LIMIT] + "\n...[truncated]...", True


def _redact_output(value: str) -> str:
    redacted = value
    configured_secrets = (
        settings.llm_api_key,
        settings.openrouter_api_key,
        settings.openai_api_key,
        settings.anthropic_api_key,
        settings.local_llm_api_key,
        settings.fallback_llm_api_key,
    )
    for secret in configured_secrets:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    for env_name, env_value in os.environ.items():
        if env_value and any(marker in env_name.upper() for marker in _SECRET_ENV_MARKERS):
            redacted = redacted.replace(env_value, "[redacted]")
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    return redacted


def _normalize_sandbox(value: str | None = None) -> str:
    sandbox = (value if value is not None else settings.codex_local_sandbox).strip() or "workspace-write"
    if sandbox not in _SANDBOX_MODES:
        raise LocalCodexConfigurationError("Local Codex sandbox mode must be read-only or workspace-write.")
    return sandbox


def _normalize_approval_policy(value: str | None = None) -> str:
    policy = (value if value is not None else settings.codex_local_approval_policy).strip() or "never"
    if policy not in _APPROVAL_POLICIES:
        raise LocalCodexConfigurationError("Local Codex approval policy is not allowed.")
    return policy


def _normalize_model(value: str | None = None) -> str:
    return (value if value is not None else settings.codex_local_model).strip()


def local_codex_command(
    prompt: str,
    *,
    cwd: str | None = None,
    model: str | None = None,
    sandbox: str | None = None,
    approval_policy: str | None = None,
    timeout_seconds: int | None = None,
) -> LocalCodexCommand:
    if not prompt.strip():
        raise LocalCodexConfigurationError("Local Codex prompt is required.")
    command = _safe_command_name()
    workspace = _workspace_root(cwd)
    argv = [
        command,
        "--ask-for-approval",
        _normalize_approval_policy(approval_policy),
        "exec",
        "-C",
        str(workspace),
        "--sandbox",
        _normalize_sandbox(sandbox),
    ]
    selected_model = _normalize_model(model)
    if selected_model:
        argv.extend(["--model", selected_model])
    argv.append(prompt)
    return LocalCodexCommand(
        argv=argv,
        cwd=workspace,
        timeout_seconds=_timeout_seconds(timeout_seconds),
    )


def local_codex_status() -> dict[str, Any]:
    command = ""
    try:
        command = _safe_command_name()
        configured_sandbox = _normalize_sandbox()
        configured_approval_policy = _normalize_approval_policy()
        resolved_command = shutil.which(command)
    except LocalCodexConfigurationError as exc:
        return {
            "id": "codex-local",
            "operator_kind": "local_command",
            "enabled": settings.codex_local_enabled,
            "ready": False,
            "unavailable_reason": str(exc),
            "command": command or None,
            "model": settings.codex_local_model,
            "sandbox": settings.codex_local_sandbox,
            "approval_policy": settings.codex_local_approval_policy,
        }

    payload: dict[str, Any] = {
        "id": "codex-local",
        "operator_kind": "local_command",
        "enabled": settings.codex_local_enabled,
        "command": command,
        "command_path": resolved_command,
        "model": settings.codex_local_model,
        "sandbox": configured_sandbox,
        "approval_policy": configured_approval_policy,
        "timeout_seconds": settings.codex_local_timeout_seconds,
        "workspace_root": str(REPO_ROOT),
        "requires_api_key": False,
    }
    if not settings.codex_local_enabled:
        payload.update({"ready": False, "unavailable_reason": "local Codex operator is disabled"})
        return payload
    if not resolved_command:
        payload.update({"ready": False, "unavailable_reason": "codex command was not found on PATH"})
        return payload

    try:
        result = subprocess.run(
            [command, "--version"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            shell=False,
            env=_codex_env(),
            timeout=_STATUS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        payload.update({"ready": False, "unavailable_reason": "codex --version timed out"})
        return payload
    except OSError as exc:
        payload.update({"ready": False, "unavailable_reason": str(exc)})
        return payload

    version_text = (result.stdout or result.stderr or "").strip()
    payload.update({
        "ready": result.returncode == 0,
        "version": version_text,
        "last_status_exit_code": result.returncode,
    })
    if result.returncode != 0:
        payload["unavailable_reason"] = "codex --version failed"
    return payload


def local_operator_statuses() -> list[dict[str, Any]]:
    return [local_codex_status()]


async def run_local_codex(
    prompt: str,
    *,
    cwd: str | None = None,
    model: str | None = None,
    sandbox: str | None = None,
    approval_policy: str | None = None,
    timeout_seconds: int | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    status = local_codex_status()
    prompt_hash = _sha256_text(prompt)
    audit_base = {
        "operator_id": "codex-local",
        "operator_kind": "local_command",
        "prompt_sha256": prompt_hash,
        "prompt_chars": len(prompt),
        "model": model or settings.codex_local_model,
        "workspace_root": str(REPO_ROOT),
    }
    if not status.get("ready"):
        await audit_repository.log_event(
            event_type="local_operator_unavailable",
            summary="Local Codex operator unavailable",
            actor="system",
            tool_name="codex-local",
            risk_level="medium",
            session_id=session_id,
            details={**audit_base, "reason": status.get("unavailable_reason")},
        )
        raise LocalCodexConfigurationError(str(status.get("unavailable_reason") or "Local Codex unavailable."))

    command = local_codex_command(
        prompt,
        cwd=cwd,
        model=model,
        sandbox=sandbox,
        approval_policy=approval_policy,
        timeout_seconds=timeout_seconds,
    )
    started_at = _utc_now()
    await audit_repository.log_event(
        event_type="local_operator_started",
        summary="Local Codex operator started",
        actor="agent",
        tool_name="codex-local",
        risk_level="medium",
        session_id=session_id,
        details={
            **audit_base,
            "cwd": str(command.cwd),
            "arg_count": len(command.argv),
            "timeout_seconds": command.timeout_seconds,
            "sandbox": _normalize_sandbox(sandbox),
            "approval_policy": _normalize_approval_policy(approval_policy),
        },
    )
    try:
        result = await _run_codex_subprocess(command)
        timed_out = False
    except subprocess.TimeoutExpired:
        result = None
        timed_out = True
    finished_at = _utc_now()

    if timed_out:
        payload = {
            "ok": False,
            "timed_out": True,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_ms": int((finished_at - started_at).total_seconds() * 1000),
            "operator_id": "codex-local",
            "model": model or settings.codex_local_model,
            "display_command": "codex exec ...",
        }
    else:
        assert result is not None
        stdout, stdout_truncated = _truncate_output(result.stdout or "")
        stderr, stderr_truncated = _truncate_output(result.stderr or "")
        payload = {
            "ok": result.returncode == 0,
            "timed_out": False,
            "exit_code": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_ms": int((finished_at - started_at).total_seconds() * 1000),
            "operator_id": "codex-local",
            "model": model or settings.codex_local_model,
            "display_command": "codex exec ...",
        }

    await audit_repository.log_event(
        event_type="local_operator_completed",
        summary="Local Codex operator completed" if payload["ok"] else "Local Codex operator failed",
        actor="agent",
        tool_name="codex-local",
        risk_level="medium",
        session_id=session_id,
        details={
            **audit_base,
            "ok": payload["ok"],
            "timed_out": payload["timed_out"],
            "exit_code": payload["exit_code"],
            "duration_ms": payload["duration_ms"],
            "stdout_chars": len(payload["stdout"]),
            "stderr_chars": len(payload["stderr"]),
            "stdout_truncated": payload["stdout_truncated"],
            "stderr_truncated": payload["stderr_truncated"],
        },
    )
    return payload


async def _run_codex_subprocess(command: LocalCodexCommand) -> subprocess.CompletedProcess[str]:
    import asyncio

    return await asyncio.to_thread(
        subprocess.run,
        command.argv,
        cwd=str(command.cwd),
        capture_output=True,
        text=True,
        shell=False,
        env=_codex_env(),
        timeout=command.timeout_seconds,
    )
