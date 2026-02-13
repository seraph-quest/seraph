"""Token-aware context window for conversation history."""

import logging
from functools import lru_cache

import tiktoken

from config.settings import settings

logger = logging.getLogger(__name__)

_summary_cache: dict[str, str] = {}


@lru_cache(maxsize=1)
def _get_encoding():
    return tiktoken.get_encoding("cl100k_base")


def _count_tokens(text: str) -> int:
    return len(_get_encoding().encode(text))


def _format_messages(messages: list[dict]) -> str:
    lines = []
    for msg in messages:
        role = msg.get("role", "unknown").capitalize()
        content = msg.get("content", "")
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _summarize_middle(messages: list[dict], session_id: str, range_key: str) -> str:
    """Summarize the middle section of conversation history via LLM."""
    cache_key = f"{session_id}:{range_key}"
    if cache_key in _summary_cache:
        return _summary_cache[cache_key]

    text = _format_messages(messages)
    try:
        import litellm

        response = litellm.completion(
            model=settings.default_model,
            messages=[{
                "role": "user",
                "content": (
                    "Summarize this conversation excerpt in one concise paragraph. "
                    "Focus on key topics, decisions, and any commitments made.\n\n"
                    f"{text[:8000]}"
                ),
            }],
            api_key=settings.openrouter_api_key,
            api_base="https://openrouter.ai/api/v1",
            temperature=0.3,
            max_tokens=200,
        )
        summary = response.choices[0].message.content.strip()
    except Exception:
        logger.warning("Failed to summarize middle section, using truncation fallback")
        summary = text[:500] + "\n[...earlier conversation truncated...]"

    _summary_cache[cache_key] = summary
    # Keep cache bounded
    if len(_summary_cache) > 50:
        oldest = next(iter(_summary_cache))
        del _summary_cache[oldest]

    return summary


def build_context_window(
    messages: list[dict],
    token_budget: int | None = None,
    keep_recent: int | None = None,
    keep_first: int | None = None,
    session_id: str = "",
) -> str:
    """Build a token-aware context window from message history.

    Strategy:
    1. Always keep first `keep_first` messages (initial context)
    2. Always keep last `keep_recent` messages (recent conversation)
    3. If total fits in budget, return all
    4. Otherwise, summarize the middle section

    When arguments are None, values are read from settings.
    """
    token_budget = token_budget if token_budget is not None else settings.context_window_token_budget
    keep_recent = keep_recent if keep_recent is not None else settings.context_window_keep_recent
    keep_first = keep_first if keep_first is not None else settings.context_window_keep_first

    if not messages:
        return ""

    full_text = _format_messages(messages)
    if _count_tokens(full_text) <= token_budget:
        logger.info(
            "Context window: %d messages, all kept (within %d token budget)",
            len(messages), token_budget,
        )
        return full_text

    n = len(messages)
    first = messages[:keep_first]
    recent = messages[max(keep_first, n - keep_recent):]
    middle = messages[keep_first:max(keep_first, n - keep_recent)]

    parts = []

    if first:
        parts.append(_format_messages(first))

    if middle:
        range_key = f"{keep_first}-{n - keep_recent}"
        summary = _summarize_middle(middle, session_id, range_key)
        parts.append(f"[Summary of {len(middle)} earlier messages: {summary}]")

    if recent:
        parts.append(_format_messages(recent))

    result = "\n".join(parts)
    logger.info(
        "Context window: %d messages in, %d kept (%d first + %d recent), %d summarized, %d result tokens",
        n, len(first) + len(recent), len(first), len(recent), len(middle),
        _count_tokens(result),
    )
    return result
