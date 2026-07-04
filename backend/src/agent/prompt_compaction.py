"""Deterministic prompt compaction for local runtime context limits."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
from typing import Any

from config.settings import settings
from src.agent.context_window import _count_tokens
from src.audit.runtime import log_background_task_event_sync

logger = logging.getLogger(__name__)


class PromptCompactionConfigurationError(ValueError):
    """Raised when local prompt-budget settings cannot fit a request."""


@dataclass(frozen=True)
class PromptSection:
    name: str
    content: str
    shrinkable: bool = True
    min_tokens: int = 256


@dataclass(frozen=True)
class PromptCompactionResult:
    text: str
    compacted: bool
    original_tokens: int
    compacted_tokens: int
    budget_tokens: int
    section_tokens: dict[str, int]
    compacted_sections: tuple[str, ...]


def _message_role(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("role") or "message")
    return str(getattr(message, "role", "message") or "message")


def _message_content(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(getattr(message, "content", "") or "")


def _with_message_content(message: Any, content: str) -> Any:
    if isinstance(message, dict):
        return {**message, "content": content}
    return message


def _format_message(role: str, content: str) -> str:
    return f"{role.capitalize()}: {content}".strip()


def _safe_ratio() -> float:
    return min(max(float(settings.local_runtime_prompt_safety_ratio), 0.25), 1.0)


def local_runtime_prompt_budget(*, reserved_output_tokens: int) -> int:
    """Return the text budget left after local ctx, output, and tool reserves."""
    ctx_tokens = max(int(settings.local_runtime_context_window_tokens), 1024)
    tool_reserve = max(int(settings.local_runtime_tool_reserve_tokens), 0)
    output_reserve = max(int(reserved_output_tokens), 0)
    safe_ctx = math.floor(ctx_tokens * _safe_ratio())
    budget = safe_ctx - output_reserve - tool_reserve
    min_budget = max(int(settings.local_runtime_min_section_tokens), 1)
    if budget < min_budget:
        raise PromptCompactionConfigurationError(
            "Local runtime prompt budget is too small after reserves "
            f"(ctx={ctx_tokens}, safe_ctx={safe_ctx}, output_reserve={output_reserve}, "
            f"tool_reserve={tool_reserve}, min_prompt={min_budget})."
        )
    return budget


def _truncate_to_token_budget(text: str, budget: int) -> str:
    text = text.strip()
    if not text or _count_tokens(text) <= budget:
        return text
    budget = max(int(budget), 1)

    marker = "\n[...compacted for local model context budget...]\n"
    marker_tokens = _count_tokens(marker)
    remaining = max(budget - marker_tokens, 64)
    head_budget = max(int(remaining * 0.35), 32)
    tail_budget = max(remaining - head_budget, 32)

    lines = [line for line in text.splitlines() if line.strip()]
    head: list[str] = []
    tail: list[str] = []

    for line in lines:
        candidate = "\n".join([*head, line])
        if _count_tokens(candidate) > head_budget:
            break
        head.append(line)

    for line in reversed(lines):
        candidate_lines = [line, *tail]
        candidate = "\n".join(candidate_lines)
        if _count_tokens(candidate) > tail_budget:
            break
        tail = candidate_lines

    compacted = "\n".join([*head, marker.strip(), *tail]).strip()
    while compacted and _count_tokens(compacted) > budget:
        if len(tail) > 1:
            tail = tail[1:]
        elif len(head) > 1:
            head = head[:-1]
        else:
            return _hard_truncate_to_token_budget(text, budget)
        compacted = "\n".join([*head, marker.strip(), *tail]).strip()
    return compacted


def _hard_truncate_to_token_budget(text: str, budget: int) -> str:
    """Return a prefix that fits the token budget even for pathological text."""
    text = text.strip()
    if not text:
        return ""
    budget = max(int(budget), 1)
    if _count_tokens(text) <= budget:
        return text

    low = 0
    high = len(text)
    best = ""
    while low <= high:
        mid = (low + high) // 2
        candidate = text[:mid].strip()
        if _count_tokens(candidate) <= budget:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best


def compact_prompt_sections(
    sections: list[PromptSection],
    *,
    runtime_path: str,
    runtime_profile: str,
    reserved_output_tokens: int,
    session_id: str | None = None,
) -> PromptCompactionResult:
    """Compact shrinkable prompt sections to fit the configured local budget."""
    original_parts = [section.content.strip() for section in sections if section.content.strip()]
    original_text = "\n\n".join(original_parts)
    original_tokens = _count_tokens(original_text)
    budget = local_runtime_prompt_budget(reserved_output_tokens=reserved_output_tokens)
    section_tokens = {
        section.name: _count_tokens(section.content)
        for section in sections
        if section.content.strip()
    }

    if original_tokens <= budget:
        return PromptCompactionResult(
            text=original_text,
            compacted=False,
            original_tokens=original_tokens,
            compacted_tokens=original_tokens,
            budget_tokens=budget,
            section_tokens=section_tokens,
            compacted_sections=(),
        )

    fixed_tokens = sum(
        _count_tokens(section.content)
        for section in sections
        if section.content.strip() and not section.shrinkable
    )
    shrinkable = [
        section for section in sections if section.content.strip() and section.shrinkable
    ]
    shrinkable_tokens = sum(_count_tokens(section.content) for section in shrinkable)
    available_for_shrinkable = max(
        budget - fixed_tokens,
        len(shrinkable) * int(settings.local_runtime_min_section_tokens),
    )

    compacted_sections: list[str] = []
    compacted_parts: list[str] = []
    for section in sections:
        content = section.content.strip()
        if not content:
            continue
        if not section.shrinkable or shrinkable_tokens <= 0:
            compacted_parts.append(content)
            continue

        original_section_tokens = _count_tokens(content)
        proportional_budget = int(
            available_for_shrinkable * (original_section_tokens / shrinkable_tokens)
        )
        section_budget = max(section.min_tokens, proportional_budget)
        compacted = _truncate_to_token_budget(content, section_budget)
        if compacted != content:
            compacted_sections.append(section.name)
        compacted_parts.append(compacted)

    compacted_text = "\n\n".join(part for part in compacted_parts if part.strip())
    while _count_tokens(compacted_text) > budget and compacted_parts:
        # Last-resort deterministic shrink pass over the bulkiest compacted part.
        largest_index = max(
            range(len(compacted_parts)),
            key=lambda index: _count_tokens(compacted_parts[index]),
        )
        largest = compacted_parts[largest_index]
        next_budget = max(
            int(settings.local_runtime_min_section_tokens),
            int(_count_tokens(largest) * 0.75),
        )
        compacted_parts[largest_index] = _truncate_to_token_budget(largest, next_budget)
        compacted_text = "\n\n".join(part for part in compacted_parts if part.strip())
        if _count_tokens(largest) <= next_budget:
            break

    if _count_tokens(compacted_text) > budget:
        compacted_text = _hard_truncate_to_token_budget(compacted_text, budget)
        compacted_sections.append("__total_prompt__")

    compacted_tokens = _count_tokens(compacted_text)
    details = {
        "runtime_path": runtime_path,
        "runtime_profile": runtime_profile,
        "original_tokens": original_tokens,
        "compacted_tokens": compacted_tokens,
        "budget_tokens": budget,
        "reserved_output_tokens": reserved_output_tokens,
        "context_window_tokens": settings.local_runtime_context_window_tokens,
        "tool_reserve_tokens": settings.local_runtime_tool_reserve_tokens,
        "compacted_sections": compacted_sections,
        "summarized_sections": compacted_sections,
        "preserved_sections": [
            section.name
            for section in sections
            if section.content.strip() and not section.shrinkable
        ],
        "section_tokens": section_tokens,
    }
    logger.info("Compacted local runtime prompt: %s", details)
    try:
        log_background_task_event_sync(
            task_name="local_runtime_prompt_compaction",
            session_id=session_id,
            outcome="succeeded" if compacted_tokens <= budget else "degraded",
            details=details,
        )
    except Exception:
        logger.debug("Failed to record local runtime prompt compaction receipt", exc_info=True)

    return PromptCompactionResult(
        text=compacted_text,
        compacted=True,
        original_tokens=original_tokens,
        compacted_tokens=compacted_tokens,
        budget_tokens=budget,
        section_tokens=section_tokens,
        compacted_sections=tuple(compacted_sections),
    )


def compact_messages_for_local_runtime(
    messages: list[Any],
    *,
    runtime_path: str,
    runtime_profile: str,
    reserved_output_tokens: int,
    session_id: str | None = None,
) -> tuple[list[Any], PromptCompactionResult]:
    """Compact local-runtime chat messages while preserving the current turn.

    This is a last-mile guard for direct LiteLLM calls. Agent construction can
    still perform richer section-aware compaction before this point.
    """
    message_rows: list[dict[str, Any]] = []
    last_user_index: int | None = None
    for index, message in enumerate(messages):
        role = _message_role(message)
        content = _message_content(message)
        if role.lower() == "user" and content.strip():
            last_user_index = index
        message_rows.append(
            {
                "index": index,
                "role": role,
                "content": content,
                "name": f"{role.lower() or 'message'}_{index}",
            }
        )

    original_text = "\n\n".join(
        _format_message(row["role"], row["content"])
        for row in message_rows
        if row["content"].strip()
    )
    original_tokens = _count_tokens(original_text)
    budget = local_runtime_prompt_budget(reserved_output_tokens=reserved_output_tokens)
    section_tokens = {
        row["name"]: _count_tokens(row["content"])
        for row in message_rows
        if row["content"].strip()
    }

    if original_tokens <= budget:
        return messages, PromptCompactionResult(
            text=original_text,
            compacted=False,
            original_tokens=original_tokens,
            compacted_tokens=original_tokens,
            budget_tokens=budget,
            section_tokens=section_tokens,
            compacted_sections=(),
        )

    preserve_indices = {
        index
        for index in (last_user_index, len(messages) - 1)
        if index is not None and index >= 0
    }
    # Keep the current user turn and final pending message intact first. Older
    # tail content may still be bulky enough to overflow a small local ctx.

    shrinkable_rows = [
        row
        for row in message_rows
        if row["content"].strip() and row["index"] not in preserve_indices
    ]
    fixed_tokens = sum(
        _count_tokens(row["content"])
        for row in message_rows
        if row["content"].strip() and row["index"] in preserve_indices
    )
    shrinkable_tokens = sum(_count_tokens(row["content"]) for row in shrinkable_rows)
    min_section_tokens = max(int(settings.local_runtime_min_section_tokens), 1)
    available_for_shrinkable = max(
        budget - fixed_tokens,
        len(shrinkable_rows) * min_section_tokens,
    )

    compacted_contents = [_message_content(message) for message in messages]
    compacted_sections: list[str] = []
    preserved_sections: list[str] = []
    for row in message_rows:
        content = row["content"]
        if not content.strip():
            continue
        if row["index"] in preserve_indices or shrinkable_tokens <= 0:
            preserved_sections.append(row["name"])
            continue
        original_section_tokens = _count_tokens(content)
        proportional_budget = int(
            available_for_shrinkable * (original_section_tokens / shrinkable_tokens)
        )
        section_budget = max(min_section_tokens, proportional_budget)
        compacted = _truncate_to_token_budget(content, section_budget)
        if compacted != content:
            compacted_sections.append(row["name"])
        compacted_contents[row["index"]] = compacted

    def _current_text() -> str:
        return "\n\n".join(
            _format_message(row["role"], compacted_contents[row["index"]])
            for row in message_rows
            if str(compacted_contents[row["index"]]).strip()
        )

    compacted_text = _current_text()
    while _count_tokens(compacted_text) > budget and message_rows:
        candidates = [
            row for row in message_rows if str(compacted_contents[row["index"]]).strip()
        ]
        if not candidates:
            break
        shrinkable_candidates = [
            row for row in candidates if row["index"] not in preserve_indices
        ] or candidates
        largest = max(
            shrinkable_candidates,
            key=lambda row: _count_tokens(compacted_contents[row["index"]]),
        )
        current_content = str(compacted_contents[largest["index"]])
        current_tokens = _count_tokens(current_content)
        next_budget = max(min_section_tokens, int(current_tokens * 0.7))
        compacted = _truncate_to_token_budget(current_content, next_budget)
        if compacted == current_content:
            compacted = _hard_truncate_to_token_budget(current_content, next_budget)
        if _count_tokens(compacted) >= current_tokens:
            next_budget = max(1, current_tokens - 1)
            compacted = _hard_truncate_to_token_budget(current_content, next_budget)
        if _count_tokens(compacted) >= current_tokens:
            break
        compacted_contents[largest["index"]] = compacted
        if largest["name"] not in compacted_sections:
            compacted_sections.append(largest["name"])
        compacted_text = _current_text()

    compacted_tokens = _count_tokens(compacted_text)
    new_messages = [
        _with_message_content(message, compacted_contents[index])
        for index, message in enumerate(messages)
    ]
    details = {
        "runtime_path": runtime_path,
        "runtime_profile": runtime_profile,
        "original_tokens": original_tokens,
        "compacted_tokens": compacted_tokens,
        "budget_tokens": budget,
        "reserved_output_tokens": reserved_output_tokens,
        "context_window_tokens": settings.local_runtime_context_window_tokens,
        "tool_reserve_tokens": settings.local_runtime_tool_reserve_tokens,
        "compacted_sections": compacted_sections,
        "summarized_sections": compacted_sections,
        "preserved_sections": preserved_sections,
        "section_tokens": section_tokens,
        "message_count": len(messages),
    }
    logger.info("Compacted local runtime messages: %s", details)
    try:
        log_background_task_event_sync(
            task_name="local_runtime_prompt_compaction",
            session_id=session_id,
            outcome="succeeded" if compacted_tokens <= budget else "degraded",
            details=details,
        )
    except Exception:
        logger.debug("Failed to record local runtime message compaction receipt", exc_info=True)

    return new_messages, PromptCompactionResult(
        text=compacted_text,
        compacted=True,
        original_tokens=original_tokens,
        compacted_tokens=compacted_tokens,
        budget_tokens=budget,
        section_tokens=section_tokens,
        compacted_sections=tuple(compacted_sections),
    )
