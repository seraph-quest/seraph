"""Shared LLM configuration and fallback helpers."""

from __future__ import annotations

import asyncio
import logging
from threading import Lock
from typing import Any
from uuid import uuid4

from smolagents import LiteLLMModel as BaseLiteLLMModel

from config.settings import settings
from src.audit.repository import audit_repository

logger = logging.getLogger(__name__)
_runtime_request_lock = Lock()
_runtime_requests: dict[str, bool] = {}


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


def _fallback_api_key(primary_api_key: str | None) -> str:
    return settings.fallback_llm_api_key or primary_api_key or _primary_api_key()


def _fallback_api_base(primary_api_base: str | None) -> str:
    return settings.fallback_llm_api_base or primary_api_base or settings.llm_api_base


def _safe_model_name(kwargs: dict[str, Any]) -> str:
    return str(kwargs.get("model", "unknown"))


def _register_request(request_id: str) -> None:
    with _runtime_request_lock:
        _runtime_requests[request_id] = False


def _mark_request_timed_out(request_id: str) -> None:
    with _runtime_request_lock:
        if request_id in _runtime_requests:
            _runtime_requests[request_id] = True


def _finish_request(request_id: str) -> None:
    with _runtime_request_lock:
        _runtime_requests.pop(request_id, None)


def _can_log_request(request_id: str | None) -> bool:
    if request_id is None:
        return True
    with _runtime_request_lock:
        timed_out = _runtime_requests.get(request_id)
    if timed_out is None:
        return True
    return not timed_out


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


class FallbackLiteLLMModel(BaseLiteLLMModel):
    """LiteLLM model wrapper that retries via the configured fallback model."""

    def __init__(
        self,
        model_id: str | None = None,
        api_base: str | None = None,
        api_key: str | None = None,
        custom_role_conversions: dict[str, str] | None = None,
        flatten_messages_as_text: bool | None = None,
        **kwargs,
    ):
        super().__init__(
            model_id=model_id,
            api_base=api_base,
            api_key=api_key,
            custom_role_conversions=custom_role_conversions,
            flatten_messages_as_text=flatten_messages_as_text,
            **kwargs,
        )
        self._fallback_model: BaseLiteLLMModel | None = None

        fallback_model_id = settings.fallback_model.strip()
        if not fallback_model_id:
            return

        fallback_api_key = _fallback_api_key(api_key)
        fallback_api_base = _fallback_api_base(api_base)
        fallback_target_differs = (
            fallback_model_id != self.model_id
            or (fallback_api_base or None) != self.api_base
            or (fallback_api_key or None) != self.api_key
        )
        if not fallback_target_differs:
            return

        fallback_kwargs = dict(kwargs)
        if flatten_messages_as_text is not None:
            fallback_kwargs["flatten_messages_as_text"] = flatten_messages_as_text
        self._fallback_model = BaseLiteLLMModel(
            model_id=fallback_model_id,
            api_base=fallback_api_base or None,
            api_key=fallback_api_key or None,
            custom_role_conversions=custom_role_conversions,
            **fallback_kwargs,
        )

    def generate(
        self,
        messages,
        stop_sequences=None,
        response_format=None,
        tools_to_call_from=None,
        **kwargs,
    ):
        try:
            return super().generate(
                messages,
                stop_sequences=stop_sequences,
                response_format=response_format,
                tools_to_call_from=tools_to_call_from,
                **kwargs,
            )
        except Exception:
            if self._fallback_model is None:
                raise
            logger.warning(
                "Primary agent model %s failed, retrying with fallback %s",
                self.model_id,
                self._fallback_model.model_id,
                exc_info=True,
            )
            return self._fallback_model.generate(
                messages,
                stop_sequences=stop_sequences,
                response_format=response_format,
                tools_to_call_from=tools_to_call_from,
                **kwargs,
            )


def completion_with_fallback_sync(
    *,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    model_id: str | None = None,
    request_id: str | None = None,
):
    """Execute a litellm completion with an optional fallback target."""
    import litellm

    try:
        primary_kwargs = build_completion_kwargs(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            model_id=model_id,
        )
        primary_model = _safe_model_name(primary_kwargs)
        try:
            response = litellm.completion(**primary_kwargs)
            if _can_log_request(request_id):
                _log_llm_runtime_event_sync(
                    event_type="llm_primary_success",
                    summary=f"Primary LLM completion succeeded via {primary_model}",
                    details={"primary_model": primary_model, "used_fallback": False},
                )
            return response
        except Exception as primary_error:
            if not has_fallback_model():
                if _can_log_request(request_id):
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
                if _can_log_request(request_id):
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
                if _can_log_request(request_id):
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
    finally:
        if request_id is not None:
            _finish_request(request_id)


async def completion_with_fallback(
    *,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    timeout: int | None = None,
    model_id: str | None = None,
):
    """Async wrapper around the shared completion fallback flow."""
    request_id = uuid4().hex
    _register_request(request_id)
    coro = asyncio.to_thread(
        completion_with_fallback_sync,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        model_id=model_id,
        request_id=request_id,
    )
    try:
        if timeout is None:
            return await coro
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        _mark_request_timed_out(request_id)
        raise
    finally:
        if timeout is None:
            _finish_request(request_id)
