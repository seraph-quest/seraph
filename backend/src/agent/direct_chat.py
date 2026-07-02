"""Bounded direct chat completions for local conversational turns."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from typing import Any

from config.settings import settings
from src.llm_runtime import (
    build_completion_kwargs,
    completion_with_fallback_sync,
    is_local_runtime_profile,
    resolve_runtime_profile,
)


_LIGHTWEIGHT_PREFIXES = (
    "hello",
    "hi",
    "hey",
    "yo",
    "good morning",
    "good afternoon",
    "good evening",
    "thanks",
    "thank you",
)
_EXPLICIT_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_BARE_DOMAIN_RE = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s<>()]*)?\b", re.IGNORECASE)
_TOOL_INTENT_TERMS = (
    "check",
    "inspect",
    "open",
    "browse",
    "visit",
    "read",
    "analyze",
    "analyse",
    "website",
    "site",
    "url",
    "page",
    "link",
    "goals",
)
_LIGHTWEIGHT_EDGE_PUNCTUATION_RE = re.compile(r"^[\s,.;:!?]+|[\s,.;:!?]+$")


def _message_content(response: Any) -> str:
    choices = response["choices"] if isinstance(response, dict) else getattr(response, "choices", [])
    if not choices:
        return ""
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, dict) else getattr(choice, "message", None)
    if isinstance(message, dict):
        return str(message.get("content") or "").strip()
    return str(getattr(message, "content", "") or "").strip()


def _stream_chunk_delta(chunk: Any) -> str:
    choices = chunk["choices"] if isinstance(chunk, dict) else getattr(chunk, "choices", [])
    if not choices:
        return ""
    choice = choices[0]
    delta = choice.get("delta") if isinstance(choice, dict) else getattr(choice, "delta", None)
    if isinstance(delta, dict):
        return str(delta.get("content") or "")
    if delta is not None:
        return str(getattr(delta, "content", "") or "")
    text = choice.get("text") if isinstance(choice, dict) else getattr(choice, "text", "")
    return str(text or "")


def _uses_local_gemma_profile(runtime_path: str) -> bool:
    return is_local_runtime_profile(resolve_runtime_profile(runtime_path=runtime_path))


def _looks_like_tool_or_web_request(message: str) -> bool:
    normalized = " ".join((message or "").strip().lower().split())
    if _EXPLICIT_URL_RE.search(normalized) or _BARE_DOMAIN_RE.search(normalized):
        return True
    return any(term in normalized for term in _TOOL_INTENT_TERMS)


def _normalize_lightweight_chat_text(message: str) -> str:
    normalized = " ".join((message or "").strip().lower().split())
    return _LIGHTWEIGHT_EDGE_PUNCTUATION_RE.sub("", normalized)


def should_use_direct_local_chat(message: str, *, runtime_path: str, is_onboarding: bool) -> bool:
    """Return whether this turn should bypass tool orchestration on the local GPU."""
    if not _uses_local_gemma_profile(runtime_path):
        return False
    if _looks_like_tool_or_web_request(message):
        return False
    if is_onboarding:
        return True

    normalized = _normalize_lightweight_chat_text(message)
    if len(normalized) > 160:
        return False
    return normalized in _LIGHTWEIGHT_PREFIXES or normalized.startswith(
        tuple(f"{prefix} " for prefix in _LIGHTWEIGHT_PREFIXES)
    )


def _direct_local_chat_messages(message: str, *, is_onboarding: bool) -> list[dict[str, str]]:
    if is_onboarding:
        system_prompt = (
            "You are Seraph in onboarding mode. Reply naturally and briefly. "
            "Ask one useful next question to learn the user's name, role, priorities, "
            "or operating context. Do not call tools."
        )
    else:
        system_prompt = (
            "You are Seraph. Reply naturally, briefly, and directly. "
            "This is a lightweight conversational turn. Do not claim that tools were used."
        )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": message},
    ]


async def run_direct_local_chat(
    message: str,
    *,
    runtime_path: str,
    is_onboarding: bool,
    request_id: str | None = None,
) -> str:
    """Run a single bounded local completion without invoking the tool agent loop."""
    response = await asyncio.to_thread(
        completion_with_fallback_sync,
        messages=_direct_local_chat_messages(message, is_onboarding=is_onboarding),
        temperature=settings.model_temperature,
        max_tokens=min(settings.model_max_tokens, 512),
        runtime_path=runtime_path,
        request_id=request_id,
        local_runtime_only=True,
    )
    content = _message_content(response)
    return content or "I am here. What should we focus on first?"


async def stream_direct_local_chat(
    message: str,
    *,
    runtime_path: str,
    is_onboarding: bool,
) -> AsyncIterator[str]:
    """Yield local chat token deltas and return the full response when complete."""
    if not _uses_local_gemma_profile(runtime_path):
        raise RuntimeError(f"Runtime path '{runtime_path}' is not configured for local Gemma chat streaming")

    kwargs = build_completion_kwargs(
        messages=_direct_local_chat_messages(message, is_onboarding=is_onboarding),
        temperature=settings.model_temperature,
        max_tokens=min(settings.model_max_tokens, 512),
        runtime_path=runtime_path,
    )
    kwargs["stream"] = True

    queue: asyncio.Queue[str | BaseException | object] = asyncio.Queue()
    done = object()
    loop = asyncio.get_running_loop()

    def _run_stream() -> None:
        try:
            import litellm

            for chunk in litellm.completion(**kwargs):
                delta = _stream_chunk_delta(chunk)
                if delta:
                    loop.call_soon_threadsafe(queue.put_nowait, delta)
        except BaseException as exc:
            loop.call_soon_threadsafe(queue.put_nowait, exc)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, done)

    loop.run_in_executor(None, _run_stream)

    parts: list[str] = []
    while True:
        item = await queue.get()
        if item is done:
            break
        if isinstance(item, BaseException):
            raise item
        delta = str(item)
        parts.append(delta)
        yield delta

    if not parts:
        fallback = "I am here. What should we focus on first?"
        yield fallback
