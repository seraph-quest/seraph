"""Deterministic prompt compaction for local runtime context limits."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math

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
