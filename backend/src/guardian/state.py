"""Explicit guardian-state synthesis for downstream agent and scheduler paths."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from src.agent.session import session_manager
from src.observer.context import CurrentContext


@dataclass(frozen=True)
class GuardianStateConfidence:
    overall: str
    observer: str
    memory: str
    current_session: str
    recent_sessions: str


@dataclass(frozen=True)
class GuardianState:
    soul_context: str
    observer_context: CurrentContext
    memory_context: str
    current_session_history: str
    recent_sessions_summary: str
    recent_intervention_feedback: str
    confidence: GuardianStateConfidence

    @property
    def active_goals_summary(self) -> str:
        return self.observer_context.active_goals_summary

    def to_prompt_block(self) -> str:
        """Render the synthesized guardian state as one operator-facing block."""
        lines = [
            f"Overall confidence: {self.confidence.overall}",
            f"Observer confidence: {self.confidence.observer}",
            f"Memory confidence: {self.confidence.memory}",
            f"Current session confidence: {self.confidence.current_session}",
            f"Recent sessions confidence: {self.confidence.recent_sessions}",
            "",
            "Observer snapshot:",
            self.observer_context.to_prompt_block(),
        ]

        if self.active_goals_summary:
            lines.extend(["", "Active goals:", self.active_goals_summary])

        if self.memory_context:
            lines.extend(["", "Relevant memories:", self.memory_context])

        if self.recent_sessions_summary:
            lines.extend(["", "Recent sessions:", self.recent_sessions_summary])

        if self.recent_intervention_feedback:
            lines.extend(["", "Recent intervention feedback:", self.recent_intervention_feedback])

        return "\n".join(lines)


def _status_for_text(text: str, *, requested: bool = True) -> str:
    if not requested:
        return "not_requested"
    return "grounded" if text.strip() else "empty"


def _overall_confidence(
    *,
    observer_confidence: str,
    memory_status: str,
    current_session_status: str,
    recent_sessions_status: str,
) -> str:
    grounded_signals = sum(
        1
        for status in (memory_status, current_session_status, recent_sessions_status)
        if status == "grounded"
    )
    if observer_confidence == "degraded":
        return "degraded"
    if observer_confidence == "partial":
        return "partial" if grounded_signals else "degraded"
    if grounded_signals >= 2:
        return "grounded"
    return "partial"


async def build_guardian_state(
    *,
    session_id: str | None = None,
    user_message: str | None = None,
    memory_query: str | None = None,
    refresh_observer: bool = False,
) -> GuardianState:
    """Build one explicit guardian-state object from current repo surfaces."""
    from src.memory.soul import read_soul
    from src.memory.vector_store import search_formatted
    from src.guardian.feedback import guardian_feedback_repository
    from src.observer.manager import context_manager

    observer_context = (
        await context_manager.refresh() if refresh_observer else context_manager.get_context()
    )
    soul_context = read_soul()

    current_session_history = (
        await session_manager.get_history_text(session_id)
        if session_id is not None
        else ""
    )
    recent_sessions_summary = await session_manager.get_recent_sessions_summary(
        exclude_session_id=session_id
    )
    recent_intervention_feedback = await guardian_feedback_repository.summarize_recent(limit=5)

    query = user_message or memory_query or ""
    memory_requested = bool(query.strip())
    memory_context = (
        await asyncio.to_thread(search_formatted, query)
        if memory_requested
        else ""
    )

    confidence = GuardianStateConfidence(
        overall=_overall_confidence(
            observer_confidence=observer_context.observer_confidence,
            memory_status=_status_for_text(memory_context, requested=memory_requested),
            current_session_status=_status_for_text(
                current_session_history,
                requested=session_id is not None,
            ),
            recent_sessions_status=_status_for_text(recent_sessions_summary),
        ),
        observer=observer_context.observer_confidence,
        memory=_status_for_text(memory_context, requested=memory_requested),
        current_session=_status_for_text(
            current_session_history,
            requested=session_id is not None,
        ),
        recent_sessions=_status_for_text(recent_sessions_summary),
    )

    return GuardianState(
        soul_context=soul_context,
        observer_context=observer_context,
        memory_context=memory_context,
        current_session_history=current_session_history,
        recent_sessions_summary=recent_sessions_summary,
        recent_intervention_feedback=recent_intervention_feedback,
        confidence=confidence,
    )
