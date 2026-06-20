"""Local Codex image analysis provider for the native daemon."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
import re
import signal
import tempfile
import time
from typing import Any

from ocr.base import AnalysisResult, OCRProvider, OCRResult

logger = logging.getLogger("seraph_daemon")

_DEFAULT_MODEL = "gpt-5.5"
_DEFAULT_REASONING_EFFORT = "low"
_DEFAULT_TIMEOUT_SECONDS = 60
_DEFAULT_ARCHIVE_DIR = "/tmp/seraph-screen-captures"
_OUTPUT_LIMIT = 12_000
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
_SECRET_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
)
_VALID_ACTIVITIES = {
    "coding",
    "browsing",
    "communication",
    "reading",
    "design",
    "terminal",
    "entertainment",
    "other",
}

_PROMPT = """\
Analyze this desktop screenshot for Seraph's private local activity tracking.

Return ONLY valid JSON with this exact shape:
{{
  "activity": "coding|browsing|communication|reading|design|terminal|entertainment|other",
  "project": "project name or null",
  "summary": "one sentence, max 100 chars",
  "details": ["notable non-sensitive items, max 5"],
  "sensitive_detected": true,
  "confidence": 0.0
}}

Rules:
- Do not copy passwords, API keys, tokens, private message bodies, financial details, or customer data.
- If sensitive content is visible, set sensitive_detected=true and keep summary/details generic.
- Prefer activity/project/focus patterns over verbatim screen text.
- The frontmost app is: {app_name}
"""


class CodexLocalProvider(OCRProvider):
    """Screen analysis using the local Codex CLI image input."""

    def __init__(
        self,
        *,
        command: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int | None = None,
        temp_dir: str | None = None,
        preserve_captures: bool | None = None,
        archive_dir: str | None = None,
    ) -> None:
        self._command = _safe_command_name(command or os.environ.get("CODEX_LOCAL_COMMAND") or "codex")
        self._model = model or os.environ.get("CODEX_LOCAL_MODEL") or _DEFAULT_MODEL
        self._reasoning_effort = (
            reasoning_effort
            or os.environ.get("CODEX_LOCAL_REASONING_EFFORT")
            or _DEFAULT_REASONING_EFFORT
        )
        self._timeout_seconds = _timeout_seconds(timeout_seconds)
        self._temp_dir = Path(temp_dir).resolve() if temp_dir else None
        self._preserve_captures = (
            _env_bool("SERAPH_PRESERVE_SCREEN_CAPTURES", False)
            if preserve_captures is None
            else preserve_captures
        )
        self._archive_dir = Path(
            archive_dir
            or os.environ.get("SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR")
            or os.environ.get("SCREEN_CAPTURE_ARCHIVE_DIR")
            or _DEFAULT_ARCHIVE_DIR
        ).expanduser().resolve()

    @property
    def name(self) -> str:
        return "codex-local"

    def is_available(self) -> bool:
        try:
            import shutil

            return shutil.which(self._command) is not None
        except Exception:
            return False

    async def extract_text(self, png_bytes: bytes) -> OCRResult:
        start = time.monotonic()
        analysis = await self.analyze_screen(png_bytes, "Unknown")
        return OCRResult(
            text=str(analysis.data.get("summary") or ""),
            provider=self.name,
            duration_ms=int((time.monotonic() - start) * 1000),
            success=analysis.success,
            error=analysis.error,
        )

    async def analyze_screen(self, png_bytes: bytes, app_name: str) -> AnalysisResult:
        start = time.monotonic()
        image_path: Path | None = None
        output_path: Path | None = None
        try:
            image_path = _write_temp_png(png_bytes, temp_dir=self._temp_dir)
            output_path = _make_temp_output(temp_dir=self._temp_dir)
            raw_output = await self._run_codex(
                image_path=image_path,
                output_path=output_path,
                prompt=_PROMPT.format(app_name=app_name),
            )
            parsed = _parse_json_output(raw_output)
            data = _normalize_analysis(parsed)
            if self._preserve_captures:
                data["capture_artifacts"] = _archive_capture(
                    archive_dir=self._archive_dir,
                    png_bytes=png_bytes,
                    app_name=app_name,
                    raw_output=raw_output,
                    normalized=data,
                )
            return AnalysisResult(
                success=True,
                data=data,
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except Exception as exc:
            logger.debug("codex-local analyze_screen failed: %s", exc)
            return AnalysisResult(
                success=False,
                data={},
                duration_ms=int((time.monotonic() - start) * 1000),
                error=str(exc),
            )
        finally:
            _cleanup_file(image_path)
            _cleanup_file(output_path)

    async def _run_codex(self, *, image_path: Path, output_path: Path, prompt: str) -> str:
        argv = [
            self._command,
            "--ask-for-approval",
            "never",
            "exec",
            "-C",
            str(_repo_root()),
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--ignore-rules",
            "--ignore-user-config",
            "--color",
            "never",
            "--image",
            str(image_path),
        ]
        if self._model:
            argv.extend(["--model", self._model])
        if self._reasoning_effort:
            argv.extend(["-c", f'model_reasoning_effort="{self._reasoning_effort}"'])
        argv.extend(["--output-last-message", str(output_path), prompt])

        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(_repo_root()),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_codex_env(),
            start_new_session=True,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(input=b""),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            await _terminate_process_group(process)
            raise TimeoutError("codex-local screen analysis timed out") from exc

        output_text = _read_output(output_path) or stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")
        if process.returncode != 0:
            raise RuntimeError(_truncate(_redact(stderr_text or output_text or "codex-local failed")))
        return _truncate(_redact(output_text))


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _safe_command_name(command: str) -> str:
    normalized = command.strip()
    if not normalized:
        raise ValueError("Codex command is required.")
    if any(char in normalized for char in ("|", "&", ";", "<", ">", "`", "$", "\n", "\r", "\t")):
        raise ValueError("Codex command must be a single executable path or name.")
    return normalized


def _timeout_seconds(value: int | None) -> int:
    if value is None:
        value = int(os.environ.get("CODEX_LOCAL_SCREEN_TIMEOUT_SECONDS") or _DEFAULT_TIMEOUT_SECONDS)
    return min(max(int(value or 1), 1), 600)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _codex_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in _ENV_ALLOWLIST and value}
    env.setdefault("PATH", os.defpath)
    env["SERAPH_LOCAL_OPERATOR"] = "codex-local-screen"
    return env


def _write_temp_png(png_bytes: bytes, *, temp_dir: Path | None = None) -> Path:
    fd, name = tempfile.mkstemp(prefix="seraph-screen-", suffix=".png", dir=str(temp_dir) if temp_dir else None)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(png_bytes)
        return Path(name)
    except Exception:
        with suppress(OSError):
            os.close(fd)
        _cleanup_file(Path(name))
        raise


def _make_temp_output(*, temp_dir: Path | None = None) -> Path:
    fd, name = tempfile.mkstemp(prefix="seraph-codex-screen-", suffix=".txt", dir=str(temp_dir) if temp_dir else None)
    os.close(fd)
    return Path(name)


def _cleanup_file(path: Path | None) -> None:
    if path is not None:
        with suppress(OSError):
            path.unlink()


def _read_output(output_path: Path) -> str:
    with suppress(OSError, UnicodeDecodeError):
        return output_path.read_text(encoding="utf-8").strip()
    return ""


def _archive_capture(
    *,
    archive_dir: Path,
    png_bytes: bytes,
    app_name: str,
    raw_output: str,
    normalized: dict[str, Any],
) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    day_dir = archive_dir / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(png_bytes + raw_output.encode("utf-8", errors="replace")).hexdigest()[:16]
    stem = f"{now.strftime('%H%M%S')}-{_slug(app_name)}-{digest}"
    image_path = day_dir / f"{stem}.png"
    raw_output_path = day_dir / f"{stem}.codex.txt"
    normalized_path = day_dir / f"{stem}.analysis.json"
    image_path.write_bytes(png_bytes)
    raw_output_path.write_text(raw_output, encoding="utf-8")
    normalized_path.write_text(json.dumps(normalized, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "id": digest,
        "image_path": str(image_path),
        "codex_output_path": str(raw_output_path),
        "analysis_path": str(normalized_path),
        "created_at": now.isoformat(),
    }


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-")
    return slug[:40] or "screen"


async def _terminate_process_group(process: Any) -> None:
    pid = int(process.pid)
    with suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
        return
    except asyncio.TimeoutError:
        pass
    with suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)
    with suppress(Exception):
        await asyncio.wait_for(process.wait(), timeout=2)


def _parse_json_output(raw_output: str) -> dict[str, Any]:
    text = raw_output.strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("codex-local did not return valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("codex-local JSON output must be an object")
    return parsed


def _normalize_analysis(parsed: dict[str, Any]) -> dict[str, Any]:
    activity = str(parsed.get("activity") or "other").strip().lower()
    if activity not in _VALID_ACTIVITIES:
        activity = "other"
    details = parsed.get("details")
    if not isinstance(details, list):
        details = []
    return {
        "activity": activity,
        "project": _safe_optional_text(parsed.get("project"), max_chars=80),
        "summary": _safe_optional_text(parsed.get("summary"), max_chars=200) or "",
        "details": [_safe_optional_text(item, max_chars=120) for item in details[:5] if _safe_optional_text(item, max_chars=120)],
        "sensitive_detected": bool(parsed.get("sensitive_detected")),
        "confidence": _safe_confidence(parsed.get("confidence")),
    }


def _safe_optional_text(value: Any, *, max_chars: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "null":
        return None
    return _redact(text)[:max_chars]


def _safe_confidence(value: Any) -> float:
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return 0.0


def _redact(value: str) -> str:
    redacted = value
    for env_name, env_value in os.environ.items():
        if env_value and any(marker in env_name.upper() for marker in _SECRET_ENV_MARKERS):
            redacted = redacted.replace(env_value, "[redacted]")
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    return redacted


def _truncate(value: str) -> str:
    if len(value) <= _OUTPUT_LIMIT:
        return value
    return value[:_OUTPUT_LIMIT] + "\n...[truncated]..."
