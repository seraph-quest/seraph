"""Shared LLM configuration and fallback helpers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from config.settings import settings
from src.audit.repository import audit_repository

logger = logging.getLogger(__name__)


def _primary_api_key() -> str:
    return settings.llm_api_key or settings.openrouter_api_key


def build_model_kwargs(
    *,
    temperature: float,
    max_tokens: int,
    model_id: str | None = None,
) -> dict[str, Any]:
    """Build LiteLLMModel kwargs from the shared runtime settings."""
    kwargs: dict[str, Any] = {
        "model_id": model_id or settings.default_model,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    api_key = _primary_api_key()
    if api_key:
        kwargs["api_key"] = api_key
    if settings.llm_api_base:
        kwargs["api_base"] = settings.llm_api_base
    return kwargs


def build_completion_kwargs(
    *,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    model_id: str | None = None,
    use_fallback: bool = False,
) -> dict[str, Any]:
    """Build litellm.completion kwargs for either the primary or fallback path."""
    if use_fallback:
        fallback_model = settings.fallback_model
        if not fallback_model:
            raise ValueError("Fallback model is not configured")
        kwargs: dict[str, Any] = {
            "model": model_id or fallback_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        api_key = settings.fallback_llm_api_key or _primary_api_key()
        api_base = settings.fallback_llm_api_base or settings.llm_api_base
    else:
        kwargs = {
            "model": model_id or settings.default_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        api_key = _primary_api_key()
        api_base = settings.llm_api_base

    if api_key:
        kwargs["api_key"] = api_key
    if api_base:
        kwargs["api_base"] = api_base
    return kwargs


def has_fallback_model() -> bool:
    """Return whether a fallback completion target is configured."""
    return bool(settings.fallback_model.strip())


def _safe_model_name(kwargs: dict[str, Any]) -> str:
    return str(kwargs.get("model", "unknown"))


async def _log_llm_runtime_event(
    *,
    event_type: str,
    summary: str,
    details: dict[str, Any],
) -> None:
    try:
        await audit_repository.log_event(
            event_type=event_type,
            actor="system",
            tool_name="llm_runtime",
            risk_level="low",
            policy_mode="full",
            summary=summary,
            details=details,
        )
    except Exception:
        logger.debug("Failed to record LLM runtime audit event", exc_info=True)


def _log_llm_runtime_event_sync(
    *,
    event_type: str,
    summary: str,
    details: dict[str, Any],
) -> None:
    try:
        asyncio.run(_log_llm_runtime_event(
            event_type=event_type,
            summary=summary,
            details=details,
        ))
    except Exception:
        logger.debug("Failed to run LLM runtime audit logger", exc_info=True)


def completion_with_fallback_sync(
    *,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    model_id: str | None = None,
):
    """Execute a litellm completion with an optional fallback target."""
    import litellm

    primary_kwargs = build_completion_kwargs(
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        model_id=model_id,
    )
    primary_model = _safe_model_name(primary_kwargs)
    try:
        response = litellm.completion(**primary_kwargs)
        _log_llm_runtime_event_sync(
            event_type="llm_primary_success",
            summary=f"Primary LLM completion succeeded via {primary_model}",
            details={"primary_model": primary_model, "used_fallback": False},
        )
        return response
    except Exception as primary_error:
        if not has_fallback_model():
            _log_llm_runtime_event_sync(
                event_type="llm_primary_failure",
                summary=f"Primary LLM completion failed via {primary_model}",
                details={
                    "primary_model": primary_model,
                    "used_fallback": False,
                    "error": str(primary_error),
                },
            )
            raise
        fallback_kwargs = build_completion_kwargs(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            use_fallback=True,
        )
        fallback_model = _safe_model_name(fallback_kwargs)
        logger.warning(
            "Primary LLM completion failed for model %s, retrying with fallback %s",
            primary_model,
            fallback_model,
            exc_info=True,
        )
        try:
            response = litellm.completion(**fallback_kwargs)
            _log_llm_runtime_event_sync(
                event_type="llm_fallback_success",
                summary=f"Fallback LLM completion succeeded via {fallback_model}",
                details={
                    "primary_model": primary_model,
                    "fallback_model": fallback_model,
                    "used_fallback": True,
                    "primary_error": str(primary_error),
                },
            )
            return response
        except Exception as fallback_error:
            _log_llm_runtime_event_sync(
                event_type="llm_fallback_failure",
                summary=f"Fallback LLM completion failed via {fallback_model}",
                details={
                    "primary_model": primary_model,
                    "fallback_model": fallback_model,
                    "used_fallback": True,
                    "primary_error": str(primary_error),
                    "fallback_error": str(fallback_error),
                },
            )
            raise


async def completion_with_fallback(
    *,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    timeout: int | None = None,
    model_id: str | None = None,
):
    """Async wrapper around the shared completion fallback flow."""
    coro = asyncio.to_thread(
        completion_with_fallback_sync,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        model_id=model_id,
    )
    if timeout is None:
        return await coro
    return await asyncio.wait_for(coro, timeout=timeout)
