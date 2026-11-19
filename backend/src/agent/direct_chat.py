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


CHAT_ROUTE_BLOCKED_MESSAGE = "Chat is blocked. Nothing was sent."
# Kept for callers and tests written before #1280; the copy is provider-neutral.
OPENROUTER_CHAT_ROUTE_BLOCKED_MESSAGE = CHAT_ROUTE_BLOCKED_MESSAGE

_SETTINGS = "Settings → Model"
_NO_CALL = "Nothing was sent."
# An agent turn can be denied after earlier steps already called the model.
_MID_TURN = (
    "Earlier steps of this turn had already called the model, so those calls may be billed "
    "and any tool actions they took stand; the reply is incomplete. Check the result before resending."
)
# Settings section a blocked chat message opens (#1280): the Model card.
CHAT_BLOCK_SETTINGS_SECTION = "model"
# REST and WebSocket cut a lightweight turn at this outer bound.
DIRECT_CHAT_TURN_CAP_SECONDS = 60.0
# ADR-035: a NEAR TEE turn's own deadline ends this much before the outer cut,
# so the stream ends while the turn is still alive. The billing readback after
# it is best effort and never withholds the answer (#1284).
NEAR_TEE_OUTER_MARGIN_SECONDS = 2.0


def direct_chat_turn_timeout() -> float:
    """The outer REST/WebSocket timeout of one lightweight chat turn."""
    return float(min(settings.agent_chat_timeout, DIRECT_CHAT_TURN_CAP_SECONDS))


def direct_chat_context_timeout(runtime_path: str) -> float:
    """Inference deadline of a lightweight turn's governed context.

    On the NEAR TEE text slot the deadline sits inside the outer cut, so a
    caller timeout never lands during the stream or billing window; other
    routes keep the agent chat timeout.
    """
    from src.llm_runtime import _near_tee_runtime_controls

    if _near_tee_runtime_controls(runtime_path) is not None:
        return max(direct_chat_turn_timeout() - NEAR_TEE_OUTER_MARGIN_SECONDS, 0.1)
    return float(settings.agent_chat_timeout)


def _retired_safe_context(runtime_path: str, **kwargs):
    """Build the canonical context; a retired provider is a no-contact route block."""
    from src.model_fabric.retirement import retired_as_route_error
    with retired_as_route_error():
        return build_canonical_inference_context(runtime_path, **kwargs)


def openrouter_route_block_reason(error: BaseException) -> str:
    """Return the first specific selector rejection carried by a route error."""
    for code in getattr(error, "reason_codes", ()) or ():
        if code and code != "no_compliant_route":
            return code
    return "no_compliant_route"


_CONSENT_CODES = frozenset({"provider_policy_revoked", "near_tee_purpose_consent_required"})
_SETUP_CODES = frozenset({"near_tee_slot_unconfigured", "near_tee_slot_disabled"})
_CATALOG_CODES = frozenset({"near_tee_catalog_receipt_stale", "near_tee_catalog_receipt_missing"})
CHAT_NEEDS_TEST_MESSAGE = "Chat needs one quick test first."


def _route_block_body(reason: str) -> tuple[str, str]:
    """Plain operator copy and recovery action for one selector rejection (#1280).

    The copy names no retired provider and points at the Model card; the
    caller appends "Nothing was sent." or the mid-turn sentence.
    """
    code, _, detail = reason.partition(":")
    capability = detail.replace("_", " ") if detail else ""
    if code in _CONSENT_CODES:
        return "Chat is paused until you give your OK.", "give_consent"
    if code == "near_tee_credential_required":
        return "Chat is off: no NEAR key saved.", "save_near_key"
    if code in _SETUP_CODES:
        return f"Chat is off: no chat model is turned on. Pick one in {_SETTINGS}.", "choose_model"
    if code in _CATALOG_CODES:
        return CHAT_NEEDS_TEST_MESSAGE, "run_chat_test"
    if code == "openrouter_retired":
        return (
            "Chat is off: no chat model is set up. OpenRouter is no longer used; set up NEAR in "
            f"{_SETTINGS}. Existing OpenRouter costs can still be settled.",
            "openrouter_retired",
        )
    if code in {"proof_missing", "proof_expired"}:
        # NEAR proofs never age out (#1178); either code means the check is absent.
        return CHAT_NEEDS_TEST_MESSAGE, "prove_route"
    if code == "capability_not_declared":
        return (
            "Chat is blocked: this turn needs a capability the chat model does not have "
            "(chat needs streaming; agent turns need tool use).",
            "declare_capability",
        )
    if code == "profile_limit_unknown_or_insufficient" and detail == "context_tokens":
        return (
            "Chat is blocked: this conversation is larger than the chat model's input limit. "
            f"Start a new session, or raise the limit in {_SETTINGS}.",
            "raise_context_window",
        )
    if code == "profile_limit_unknown_or_insufficient":
        return (
            f"Chat is blocked: the chat model's {capability} limit is too low for this turn. "
            f"Adjust it in {_SETTINGS}.",
            "adjust_route_limits",
        )
    if code == "requirement_noncompliant":
        return (
            f"Chat is blocked: the last {capability} check did not meet the chat model's requirement. "
            f"Run the test again in {_SETTINGS}.",
            "prove_route",
        )
    return "Chat is blocked.", "open_model_settings"


def openrouter_route_block_recovery(
    reason: str,
    *,
    provider_contacted: bool = False,
) -> tuple[str, str]:
    """Map one selector rejection code to an operator message and recovery action.

    The codes come from the canonical selector or the accounting owner; only the
    bounded code and the capability name are echoed, never provider or
    credential detail. ``provider_contacted`` marks a denial after earlier calls
    of the same turn reached the provider, so the message must not claim that
    no model call was made.
    """
    body, action = _route_block_body(reason)
    if provider_contacted:
        return f"{body} {_MID_TURN}", action
    return f"{body} {_NO_CALL}", action


def openrouter_route_blocked_message(
    error: BaseException,
    *,
    provider_contacted: bool = False,
) -> str:
    return openrouter_route_block_recovery(
        openrouter_route_block_reason(error),
        provider_contacted=provider_contacted,
    )[0]


async def route_block_frame_fields(error: BaseException, *, provider_contacted: bool = False) -> dict[str, str]:
    """WebSocket error-frame fields for one route denial (#1280).

    Built from the same reason/action tuple the REST path returns, plus the
    Settings section the chat's "Open Settings" button opens.
    """
    reason = openrouter_route_block_reason(error)
    message, action = openrouter_route_block_recovery(
        reason,
        provider_contacted=provider_contacted,
    )
    return {"content": message, "code": reason, "recovery_action": action,
            "settings_section": CHAT_BLOCK_SETTINGS_SECTION}


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
        request_context=_retired_safe_context(
            runtime_path,
            payload=messages,
            output_tokens=min(settings.model_max_tokens, 512),
            timeout_seconds=direct_chat_context_timeout(runtime_path),
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
    context = _retired_safe_context(
        runtime_path,
        payload=messages,
        output_tokens=min(settings.model_max_tokens, 512),
        timeout_seconds=direct_chat_context_timeout(runtime_path),
        session_id=session_id,
        streaming=True,
    )
    parts: list[str] = []
    from contextlib import aclosing
    # aclosing: a turn that stops early closes the provider stream in this
    # task (a NEAR TEE stream then still reads its billing back, best effort).
    async with aclosing(stream_completion_with_fallback(
        messages=messages,
        temperature=settings.model_temperature,
        max_tokens=min(settings.model_max_tokens, 512),
        runtime_path=runtime_path,
        request_context=context,
        request_id=context.request_id,
    )) as stream:
        async for delta in stream:
            parts.append(delta)
            yield delta

    if not parts:
        fallback = "I am here. What should we focus on first?"
        yield fallback
