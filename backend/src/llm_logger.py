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

from litellm.integrations.custom_logger import CustomLogger

from config.settings import settings

logger = logging.getLogger(__name__)


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
