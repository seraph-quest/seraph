"""Shared LLM configuration and fallback helpers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from config.settings import settings

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
    try:
        return litellm.completion(**primary_kwargs)
    except Exception:
        if not has_fallback_model():
            raise
        fallback_kwargs = build_completion_kwargs(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            use_fallback=True,
        )
        logger.warning(
            "Primary LLM completion failed for model %s, retrying with fallback %s",
            primary_kwargs["model"],
            fallback_kwargs["model"],
            exc_info=True,
        )
        return litellm.completion(**fallback_kwargs)


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
