"""LiteLLM global callback logger — writes JSONL to a rotating log file.

Registers a `CustomLogger` with `litellm.callbacks` so that every
`litellm.completion()` call (direct or via smolagents) is captured
without touching any call site.
"""

import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

from config.settings import settings
from src.approval.runtime import get_current_session_id
from src.llm_runtime import get_current_llm_request_id

logger = logging.getLogger(__name__)


def _infer_request_origin(request_id: str | None, session_id: str | None = None) -> tuple[str, str]:
    if request_id:
        if request_id.startswith("agent-rest:"):
            return "user_request", "rest_chat"
        if request_id.startswith("agent-ws:"):
            return "user_request", "websocket_chat"
        if request_id.startswith("strategist_tick:"):
            return "autonomous", "strategist_tick"
    if session_id:
        return "threaded_runtime", "session_runtime"
    return "autonomous", "background"


def _coerce_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _infer_session_id(entry: dict[str, Any]) -> str | None:
    session_id = entry.get("session_id")
    if isinstance(session_id, str) and session_id:
        return session_id
    request_id = entry.get("request_id")
    if isinstance(request_id, str):
        parts = request_id.split(":")
        if len(parts) >= 3 and parts[0] in {"agent-rest", "agent-ws"} and parts[1]:
            return parts[1]
    return None


def _log_file_candidates() -> list[Path]:
    base_path = Path(settings.llm_log_dir) / "llm_calls.jsonl"
    candidates = [base_path]
    for index in range(1, settings.llm_log_backup_count + 1):
        candidates.append(base_path.with_name(f"{base_path.name}.{index}"))
    return candidates


def list_recent_llm_calls(
    *,
    limit: int = 100,
    session_id: str | None = None,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return recent LLM call records from the rotating JSONL log, newest first."""
    entries: list[dict[str, Any]] = []
    remaining = max(limit, 1)
    for path in _log_file_candidates():
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            inferred_session_id = _infer_session_id(entry)
            if session_id and inferred_session_id != session_id:
                continue
            timestamp = _coerce_timestamp(entry.get("timestamp"))
            if since and timestamp and timestamp < since:
                continue
            request_id = entry.get("request_id") if isinstance(entry.get("request_id"), str) else None
            actor, source = _infer_request_origin(request_id, inferred_session_id)
            entry["session_id"] = inferred_session_id
            entry["actor"] = actor
            entry["source"] = source
            entries.append(entry)
            remaining -= 1
            if remaining <= 0:
                return entries
    return entries


class SeraphLLMLogger(CustomLogger):
    """Writes one JSON line per LLM call to a dedicated rotating log."""

    def __init__(self) -> None:
        self._log = logging.getLogger("seraph.llm_calls")
        self._log.propagate = False
        self._log.setLevel(logging.INFO)

        os.makedirs(settings.llm_log_dir, exist_ok=True)
        path = os.path.join(settings.llm_log_dir, "llm_calls.jsonl")
        handler = RotatingFileHandler(
            path,
            maxBytes=settings.llm_log_max_bytes,
            backupCount=settings.llm_log_backup_count,
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._log.addHandler(handler)

    # ------------------------------------------------------------------
    # Sync callbacks (called by LiteLLM for non-async completions)
    # ------------------------------------------------------------------

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            entry = self._build_entry(kwargs, response_obj, start_time, end_time, success=True)
            self._log.info(json.dumps(entry, default=str))
        except Exception:
            logger.debug("llm_logger: failed to log success event", exc_info=True)

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):
        try:
            entry = self._build_entry(kwargs, response_obj, start_time, end_time, success=False)
            self._log.info(json.dumps(entry, default=str))
        except Exception:
            logger.debug("llm_logger: failed to log failure event", exc_info=True)

    # ------------------------------------------------------------------
    # Async callbacks
    # ------------------------------------------------------------------

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self.log_success_event(kwargs, response_obj, start_time, end_time)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self.log_failure_event(kwargs, response_obj, start_time, end_time)

    # ------------------------------------------------------------------
    # Entry builder
    # ------------------------------------------------------------------

    def _build_entry(self, kwargs, response_obj, start_time, end_time, *, success: bool) -> dict:
        slo = kwargs.get("standard_logging_object") or {}
        session_id = get_current_session_id()
        request_id = get_current_llm_request_id()
        actor, source = _infer_request_origin(request_id, session_id)

        latency_ms = 0.0
        if start_time and end_time:
            delta = end_time - start_time
            latency_ms = round(delta.total_seconds() * 1000, 2)

        entry: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "success" if success else "failure",
            "model": slo.get("model") or kwargs.get("model", ""),
            "call_type": slo.get("call_type", "completion"),
            "provider": slo.get("custom_llm_provider") or kwargs.get("custom_llm_provider", ""),
            "tokens": {
                "input": slo.get("prompt_tokens", 0),
                "output": slo.get("completion_tokens", 0),
                "total": slo.get("total_tokens", 0),
            },
            "cost_usd": slo.get("response_cost", 0),
            "latency_ms": latency_ms,
            "stream": kwargs.get("stream", False),
            "api_base": slo.get("api_base") or kwargs.get("api_base", ""),
            "session_id": session_id,
            "request_id": request_id,
            "actor": actor,
            "source": source,
        }

        if not success:
            exc = kwargs.get("exception") or kwargs.get("traceback_exception")
            entry["error"] = str(exc) if exc else slo.get("error_str", "")

        if settings.llm_log_content:
            messages = kwargs.get("messages")
            if messages:
                entry["messages"] = messages
            if response_obj:
                try:
                    choices = getattr(response_obj, "choices", None)
                    if choices and len(choices) > 0:
                        msg = choices[0].message
                        entry["response"] = msg.content if msg else None
                except Exception:
                    pass

        return entry


def init_llm_logging() -> None:
    """Register the LLM logger callback if enabled in settings."""
    if not settings.llm_log_enabled:
        logger.info("LLM call logging disabled")
        return

    import litellm

    try:
        callback = SeraphLLMLogger()
    except OSError:
        logger.warning("LLM call logging failed — cannot create log dir %s", settings.llm_log_dir)
        return

    litellm.callbacks.append(callback)
    logger.info("LLM call logging enabled → %s/llm_calls.jsonl", settings.llm_log_dir)
