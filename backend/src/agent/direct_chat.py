"""Bounded direct chat completions for local conversational turns."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from typing import Any

from config.settings import settings
from src.llm_runtime import (
    completion_with_fallback_sync,
    is_local_runtime_profile,
    resolve_runtime_profile,
    stream_completion_with_fallback,
)
from src.model_fabric.caller_context import build_canonical_inference_context


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
_TOOL_INTENT_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(term) for term in _TOOL_INTENT_TERMS) + r")\b",
    re.IGNORECASE,
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


def looks_like_tool_or_web_request(message: str) -> bool:
    """Return whether a turn needs the agent/tool path instead of direct chat."""
    normalized = " ".join((message or "").strip().lower().split())
    if _EXPLICIT_URL_RE.search(normalized) or _BARE_DOMAIN_RE.search(normalized):
        return True
    return bool(_TOOL_INTENT_RE.search(normalized))


def _normalize_lightweight_chat_text(message: str) -> str:
    normalized = " ".join((message or "").strip().lower().split())
    return _LIGHTWEIGHT_EDGE_PUNCTUATION_RE.sub("", normalized)


def should_use_direct_local_chat(message: str, *, runtime_path: str, is_onboarding: bool) -> bool:
    """Return whether this turn should bypass tool orchestration on the local GPU."""
    if not _uses_local_gemma_profile(runtime_path):
        return False
    if looks_like_tool_or_web_request(message):
        return False
    if is_onboarding:
        return True

    normalized = _normalize_lightweight_chat_text(message)
    if len(normalized) > 160:
        return False
    return any(
        normalized == prefix
        or normalized.startswith(f"{prefix} ")
        or normalized.startswith(f"{prefix},")
        or normalized.startswith(f"{prefix}.")
        or normalized.startswith(f"{prefix}!")
        or normalized.startswith(f"{prefix}?")
        for prefix in _LIGHTWEIGHT_PREFIXES
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
    session_id: str = "",
    request_id: str | None = None,
) -> str:
    """Run a single bounded local completion without invoking the tool agent loop."""
    messages = _direct_local_chat_messages(message, is_onboarding=is_onboarding)
    response = await asyncio.to_thread(
        completion_with_fallback_sync,
        messages=messages,
        temperature=settings.model_temperature,
        max_tokens=min(settings.model_max_tokens, 512),
        runtime_path=runtime_path,
        request_id=request_id,
        local_runtime_only=True,
        request_context=build_canonical_inference_context(
            runtime_path,
            payload=messages,
            output_tokens=min(settings.model_max_tokens, 512),
            timeout_seconds=settings.agent_chat_timeout,
            session_id=session_id,
            request_id=request_id or "",
        ),
    )
    content = _message_content(response)
    return content or "I am here. What should we focus on first?"


async def stream_direct_local_chat(
    message: str,
    *,
    runtime_path: str,
    is_onboarding: bool,
    session_id: str = "",
) -> AsyncIterator[str]:
    """Yield local chat token deltas and return the full response when complete."""
    if not _uses_local_gemma_profile(runtime_path):
        raise RuntimeError(f"Runtime path '{runtime_path}' is not configured for local Gemma chat streaming")

    messages = _direct_local_chat_messages(message, is_onboarding=is_onboarding)
    context = build_canonical_inference_context(
        runtime_path,
        payload=messages,
        output_tokens=min(settings.model_max_tokens, 512),
        timeout_seconds=settings.agent_chat_timeout,
        session_id=session_id,
        streaming=True,
    )
    parts: list[str] = []
    async for delta in stream_completion_with_fallback(
        messages=messages,
        temperature=settings.model_temperature,
        max_tokens=min(settings.model_max_tokens, 512),
        runtime_path=runtime_path,
        request_context=context,
        request_id=context.request_id,
    ):
        parts.append(delta)
        yield delta

    if not parts:
        fallback = "I am here. What should we focus on first?"
        yield fallback
