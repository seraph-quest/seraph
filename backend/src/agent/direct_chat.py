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
from src.model_fabric.contracts import NoCompliantModelRouteError
from src.workflows.inference_accounting import InferenceAccountingError


OPENROUTER_CHAT_ROUTE_BLOCKED_MESSAGE = (
    "OpenRouter chat is blocked before any provider request. "
    "Open Settings → Models & OpenRouter and save an allowed model, upstream, "
    "cloud-egress consent, and a finite spend ceiling. No model call was made."
)

_SETTINGS = "Settings → Models & OpenRouter"
_NO_CALL = "No model call was made."
# An agent turn can be denied after earlier steps already called the model.
_MID_TURN = (
    "Earlier steps of this turn had already called the model, so those calls may be billed "
    "and any tool actions they took stand; the reply is incomplete. Check the result before resending."
)
PROVIDER_OVERRUN_CODE = "provider_charge_exceeded_reservation"


def openrouter_route_block_reason(error: BaseException) -> str:
    """Return the first specific selector rejection carried by a route error."""
    for code in getattr(error, "reason_codes", ()) or ():
        if code and code != "no_compliant_route":
            return code
    return "no_compliant_route"


def microusd_dollars(value: int) -> str:
    """Exact dollar text for an integer micro-USD amount, e.g. 61234 -> 0.061234."""
    whole, fraction = divmod(int(value), 1_000_000)
    text = f"{fraction:06d}".rstrip("0")
    return f"{whole}.{text}" if text else str(whole)


async def route_block_overrun_minimum(error: BaseException) -> int | None:
    """Return the bound that reviews the current overrun, for an overrun block only.

    The value is the largest unreviewed settled charge from the accounting
    owner: a bounded, non-sensitive amount the operator needs to recover.
    """
    if openrouter_route_block_reason(error).partition(":")[0] != PROVIDER_OVERRUN_CODE:
        return None
    try:
        from src.workflows.job_runtime import durable_job_repository

        snapshot = await durable_job_repository.inference_accounting_snapshot()
    except Exception:
        return None
    value = snapshot.get("overrun_max_cost_microusd")
    return value if type(value) is int and 0 < value <= 1_000_000_000_000 else None


def _route_block_body(reason: str, minimum_bound_microusd: int | None = None) -> tuple[str, str]:
    code, _, detail = reason.partition(":")
    capability = detail.replace("_", " ") if detail else ""
    if code == PROVIDER_OVERRUN_CODE:
        target = (
            f"at least ${microusd_dollars(minimum_bound_microusd)} ({minimum_bound_microusd:,} µUSD, "
            "the largest unreviewed charge)"
            if minimum_bound_microusd else "at least the billed charge shown there"
        )
        return (
            "OpenRouter is paused: an earlier call was billed above its request cost bound, and Seraph "
            "blocks every further paid call until that overrun is reviewed. Open "
            f"{_SETTINGS}, check the overrun under Inference accounting, raise the request cost bound to "
            f"{target} and save the setup to record the review. The new bound changes the route, so "
            "press Prove route again afterwards (each canary is one real, billed provider call).",
            "review_cost_overrun",
        )
    if code == "proof_expired":
        return (
            f"OpenRouter chat is blocked: the {capability} capability proof for the text route expired. "
            f"Open {_SETTINGS} and press Prove route (each canary is one real, billed provider call), "
            "or choose a longer proof lifetime.",
            "prove_route",
        )
    if code == "proof_missing":
        return (
            f"OpenRouter chat is blocked: the text route has no {capability} capability proof yet. "
            f"Open {_SETTINGS} and press Prove route (each canary is one real, billed provider call).",
            "prove_route",
        )
    if code == "capability_not_declared":
        return (
            "OpenRouter chat is blocked: this turn needs a capability the text route does not declare "
            f"(chat needs streaming; agent turns need tool use). Enable it in {_SETTINGS}, save, "
            "then Prove route.",
            "declare_capability",
        )
    if code == "profile_limit_unknown_or_insufficient" and detail == "context_tokens":
        return (
            "OpenRouter chat is blocked: this conversation is larger than the text route's context window. "
            f"Raise the context window in {_SETTINGS} (then Prove route) or start a new session.",
            "raise_context_window",
        )
    if code == "profile_limit_unknown_or_insufficient":
        return (
            f"OpenRouter chat is blocked: the text route's {capability} limit is too low for this turn. "
            f"Adjust it in {_SETTINGS}, save, then Prove route.",
            "adjust_route_limits",
        )
    if code == "requirement_noncompliant":
        return (
            f"OpenRouter chat is blocked: the last {capability} proof did not meet this route's requirement. "
            f"Prove route again in {_SETTINGS}, or raise the timeout.",
            "prove_route",
        )
    if code.startswith("profile_cost"):
        return (
            "OpenRouter chat is blocked: the text route's spend metadata is missing, stale, or above the ceiling. "
            f"Review the ceiling and request bound in {_SETTINGS} and save.",
            "review_spend_ceiling",
        )
    return (
        f"OpenRouter chat is blocked. Open {_SETTINGS} and save an allowed model, upstream, "
        "cloud-egress consent, and a finite spend ceiling.",
        "configure_openrouter",
    )


def openrouter_route_block_recovery(
    reason: str,
    *,
    provider_contacted: bool = False,
    minimum_bound_microusd: int | None = None,
) -> tuple[str, str]:
    """Map one selector rejection code to an operator message and recovery action.

    The codes come from the canonical selector or the accounting owner; only the
    bounded code and the capability name are echoed, never provider or
    credential detail. ``provider_contacted`` marks a denial after earlier calls
    of the same turn reached the provider, so the message must not claim that
    no model call was made.
    """
    body, action = _route_block_body(reason, minimum_bound_microusd)
    if provider_contacted:
        return f"{body} {_MID_TURN}", action
    if action == "configure_openrouter":
        return OPENROUTER_CHAT_ROUTE_BLOCKED_MESSAGE, action
    return f"{body} {_NO_CALL}", action


def openrouter_route_blocked_message(
    error: BaseException,
    *,
    provider_contacted: bool = False,
    minimum_bound_microusd: int | None = None,
) -> str:
    return openrouter_route_block_recovery(
        openrouter_route_block_reason(error),
        provider_contacted=provider_contacted,
        minimum_bound_microusd=minimum_bound_microusd,
    )[0]


def route_error_from(error: BaseException) -> NoCompliantModelRouteError | None:
    """Find a route denial wrapped by the agent framework's generation error.

    Only explicit causes (``raise ... from``) are followed: an implicit
    ``__context__`` can be a route error that was already handled earlier in the
    turn and must not be reported as the reason for this failure.
    """
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(seen) < 8:
        if isinstance(current, NoCompliantModelRouteError):
            return current
        seen.add(id(current))
        current = current.__cause__
    return None


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


def _uses_openrouter_profile(runtime_path: str) -> bool:
    """Return whether the bounded direct-chat path uses the active provider.

    The helper keeps the historical function names used by the API and tests,
    but the active phase no longer treats a local Gemma process as a valid
    direct-chat dependency.  Route selection and authority checks still happen
    in the shared model-fabric completion path.
    """
    resolved = resolve_runtime_profile(runtime_path=runtime_path).strip().lower()
    return resolved == "openrouter" or (
        resolved == "default" and bool(getattr(settings, "openrouter_provider_only", True))
    )


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
    """Return whether a lightweight turn may use the bounded direct path.

    ``should_use_direct_local_chat`` is retained as a compatibility name for
    the existing REST/WebSocket callers.  Active direct turns are admitted only
    on the governed OpenRouter profile; local model profiles are never revived
    by this shortcut.
    """
    if not _uses_openrouter_profile(runtime_path):
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
    """Run one bounded governed completion without invoking the tool loop."""
    messages = _direct_local_chat_messages(message, is_onboarding=is_onboarding)
    from src.agent.session import session_manager
    task_context = await session_manager.get_task_continuity_context(session_id)
    if task_context:
        messages[0]["content"] += "\n\n" + task_context
    response = await asyncio.to_thread(
        completion_with_fallback_sync,
        messages=messages,
        temperature=settings.model_temperature,
        max_tokens=min(settings.model_max_tokens, 512),
        runtime_path=runtime_path,
        request_id=request_id,
        local_runtime_only=False,
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
    """Yield governed OpenRouter token deltas for a lightweight turn."""
    if not _uses_openrouter_profile(runtime_path):
        raise RuntimeError(
            f"Runtime path '{runtime_path}' is not configured for the active OpenRouter chat profile"
        )

    messages = _direct_local_chat_messages(message, is_onboarding=is_onboarding)
    from src.agent.session import session_manager
    task_context = await session_manager.get_task_continuity_context(session_id)
    if task_context:
        messages[0]["content"] += "\n\n" + task_context
    context = build_canonical_inference_context(
        runtime_path,
        payload=messages,
        output_tokens=min(settings.model_max_tokens, 512),
        timeout_seconds=settings.agent_chat_timeout,
        session_id=session_id,
        streaming=True,
    )
    parts: list[str] = []
    try:
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
    except InferenceAccountingError as exc:
        # An unreviewed overrun is refused at reservation, before the
        # transport runs; report it as the no-contact block it is.
        if parts or exc.code != PROVIDER_OVERRUN_CODE:
            raise
        raise NoCompliantModelRouteError((exc.code,)) from exc

    if not parts:
        fallback = "I am here. What should we focus on first?"
        yield fallback
