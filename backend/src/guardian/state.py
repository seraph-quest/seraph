"""Explicit guardian-state synthesis for downstream agent and scheduler paths."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from src.agent.session import session_manager
from src.guardian.world_model import GuardianWorldModel, build_guardian_world_model
from src.observer.context import CurrentContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GuardianStateConfidence:
    overall: str
    observer: str
    world_model: str
    memory: str
    current_session: str
    recent_sessions: str


@dataclass(frozen=True)
class GuardianState:
    soul_context: str
    observer_context: CurrentContext
    world_model: GuardianWorldModel
    memory_context: str
    current_session_history: str
    recent_sessions_summary: str
    recent_intervention_feedback: str
    confidence: GuardianStateConfidence
    recent_execution_summary: str = ""

    @property
    def active_goals_summary(self) -> str:
        return self.observer_context.active_goals_summary

    def to_prompt_block(self) -> str:
        """Render the synthesized guardian state as one operator-facing block."""
        lines = [
            f"Overall confidence: {self.confidence.overall}",
            f"Observer confidence: {self.confidence.observer}",
            f"World-model confidence: {self.confidence.world_model}",
            f"Memory confidence: {self.confidence.memory}",
            f"Current session confidence: {self.confidence.current_session}",
            f"Recent sessions confidence: {self.confidence.recent_sessions}",
            "",
            "World model:",
            self.world_model.to_prompt_block(),
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

        if self.recent_execution_summary:
            lines.extend(["", "Recent execution:", self.recent_execution_summary])

        return "\n".join(lines)


def _summarize_recent_execution(events: list[dict[str, object]]) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for event in events:
        event_type = str(event.get("event_type") or "")
        tool_name = str(event.get("tool_name") or event_type or "runtime")
        details = event.get("details")
        detail_map = details if isinstance(details, dict) else {}
        line = ""
        if tool_name.startswith("workflow_"):
            workflow_name = str(
                detail_map.get("workflow_name")
                or tool_name.removeprefix("workflow_").replace("_", "-")
            )
            continued_error_steps = detail_map.get("continued_error_steps")
            if event_type == "tool_failed":
                line = f"Workflow {workflow_name} failed recently"
            elif isinstance(continued_error_steps, list) and continued_error_steps:
                degraded_steps = ", ".join(str(step) for step in continued_error_steps[:2])
                line = f"Workflow {workflow_name} degraded at {degraded_steps}"
        elif event_type in {"tool_failed", "integration_failed"}:
            line = f"{tool_name} failed recently"

        if not line or line in seen:
            continue
        seen.add(line)
        lines.append(line)
        if len(lines) >= 3:
            break
    return "\n".join(f"- {line}" for line in lines)


def _status_for_text(text: str, *, requested: bool = True) -> str:
    if not requested:
        return "not_requested"
    return "grounded" if text.strip() else "empty"


def _overall_confidence(
    *,
    observer_confidence: str,
    world_model_status: str,
    memory_status: str,
    current_session_status: str,
    recent_sessions_status: str,
) -> str:
    grounded_signals = sum(
        1
        for status in (
            world_model_status,
            memory_status,
            current_session_status,
            recent_sessions_status,
        )
        if status == "grounded"
    )
    if observer_confidence == "degraded":
        return "degraded"
    if observer_confidence == "partial":
        return "partial" if grounded_signals else "degraded"
    if grounded_signals >= 2 and world_model_status != "empty":
        return "grounded"
    return "partial"


def _world_model_status(world_model: GuardianWorldModel) -> str:
    if world_model.current_focus != "No clear focus signal" and world_model.active_commitments:
        return "grounded"
    if (
        world_model.current_focus != "No clear focus signal"
        or world_model.active_commitments
        or world_model.open_loops_or_pressure
    ):
        return "partial"
    return "empty"


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
    from src.audit.repository import audit_repository
    from src.guardian.feedback import guardian_feedback_repository
    from src.observer.manager import context_manager
    from src.observer.screen_repository import screen_observation_repo

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
    try:
        recent_execution_summary = _summarize_recent_execution(
            await audit_repository.list_events(limit=20, session_id=session_id)
        )
    except Exception:
        logger.debug("Failed to load recent execution summary for guardian state", exc_info=True)
        recent_execution_summary = ""

    try:
        active_projects = tuple(await screen_observation_repo.get_recent_projects(limit=3))
    except Exception:
        logger.debug("Failed to load recent projects for guardian state", exc_info=True)
        active_projects = ()

    query = user_message or memory_query or ""
    memory_requested = bool(query.strip())
    memory_context = (
        await asyncio.to_thread(search_formatted, query)
        if memory_requested
        else ""
    )
    world_model = build_guardian_world_model(
        observer_context=observer_context,
        memory_context=memory_context,
        current_session_history=current_session_history,
        recent_sessions_summary=recent_sessions_summary,
        recent_intervention_feedback=recent_intervention_feedback,
        active_projects=active_projects,
        recent_execution_summary=recent_execution_summary,
    )
    world_model_status = _world_model_status(world_model)

    confidence = GuardianStateConfidence(
        overall=_overall_confidence(
            observer_confidence=observer_context.observer_confidence,
            world_model_status=world_model_status,
            memory_status=_status_for_text(memory_context, requested=memory_requested),
            current_session_status=_status_for_text(
                current_session_history,
                requested=session_id is not None,
            ),
            recent_sessions_status=_status_for_text(recent_sessions_summary),
        ),
        observer=observer_context.observer_confidence,
        world_model=world_model_status,
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
        world_model=world_model,
        memory_context=memory_context,
        current_session_history=current_session_history,
        recent_sessions_summary=recent_sessions_summary,
        recent_intervention_feedback=recent_intervention_feedback,
        recent_execution_summary=recent_execution_summary,
        confidence=confidence,
    )
